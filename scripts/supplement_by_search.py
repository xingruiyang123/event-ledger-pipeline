"""搜索通道补充：抓不到正文时，用检索拿标题+摘要+链接，并保住证据链。

用户的判断（2026-11-20）
----------------------
「不建议现在为了绕过 403 而立即采购代理或更换出口 IP。IP 级封锁和
 Cloudflare 挑战确实可能导致请求失败，但更换出口也不保证成功，
 还会增加成本、维护和合规方面的工作。先把高价值源的搜索补充链路跑通，
 之后再根据实际缺失情况决定是否值得投入。」

以及证据链要求：
「每次通过搜索通道获得摘要时，都保留搜索查询、查询时间、搜索结果 URL、
 摘要原文和对应信源 ID。以后即使原网页恢复访问，也能把摘要证据与
 全文证据关联起来，而不是产生两条孤立记录。」

所以这个脚本的输出不是"一篇新文章"，而是**同一条记录的一种证据形态**：

  doc_id          沿用原始文章 URL 算出来的同一个 id
  evidence_kind   "search_snippet"（区别于 "full_html"）
  query           用了什么检索式
  queried_at      什么时候查的
  result_url      搜索结果指向的 URL
  snippet         摘要原文
  source_domain   信源 ID（域名）

这样以后原网页能打开了，`raw_store.put()` 用同一个 doc_id 存全文，
两批证据自动挂在同一条记录下，而不是变成两条孤立记录。

三档优先级
----------
  优先补充   timesofisrael.com / mindanews.com
             中东与菲律宾南部的重要来源，值得花检索预算
  低成本观察 thewirenews.in / nytimes.com / albawaba.com / ppcms.co.uk
             每日重试即可；只有发现具体候选事件才触发搜索
  暂不投入   fox8.com / independent.ie
             内容相关性低，只保留状态

检索只在「有候选事件」时触发，不做全量搜索 —— 全量搜索的成本
（元宝 0.014 元/次 × 数千条）与收益不成比例。
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 三档。key 是域名，value 是档位。
TIERS = {
    # 优先补充：高价值区域，值得花检索预算
    "timesofisrael.com": "P1",
    "mindanews.com": "P1",
    # 低成本观察：每日重试，只在有候选事件时触发搜索
    "thewirenews.in": "P2",
    "nytimes.com": "P2",
    "albawaba.com": "P2",
    "ppcms.co.uk": "P2",
    # 暂不投入：只保留状态
    "fox8.com": "P3",
    "independent.ie": "P3",
}


def now():
    return datetime.now(timezone.utc)


def clean_env():
    """调本地检索工具要清代理（与抓站点的逻辑相反）。"""
    e = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        e.pop(k, None)
    e["NO_PROXY"] = "*"
    e["no_proxy"] = "*"
    return e


def search_json(cmd_tpl, q, timeout=90):
    """跑一条检索通道，返回 [{title, url, content}]。

    两种输出形态都要认，因为本机三条通道形态不同：

      元宝 WSA     JSON（results[].{title,url,content}），但 2026-11-20 欠费
      Tavily CLI   JSON（results[].{title,url,content}，content 有正文段落）
      TinyFish     纯文本块：``[1] 标题`` 换行 缩进 URL，**没有摘要**

    通道不可用（欠费/额度）必须显式报出来，不能静默返回空 ——
    实测教训：元宝欠费时 17 条全部返回 0，差一点被记成
    「搜不到同事件报道」，把通道故障误判成数据事实。
    """
    cmd = cmd_tpl.replace("{q}", q.replace('"', " "))
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=clean_env(), timeout=timeout)
        out = r.stdout or ""
    except Exception:  # noqa: BLE001
        return []

    low = out.lower()
    if ("欠费" in out or "resourceunavailable" in low
            or "insufficient" in low or "no longer updated" in low and False):
        print("      ! 检索通道不可用（欠费/额度）：%s"
              % out.strip().split("\n")[0][:60])
        return []

    i = out.find("{")
    if i >= 0:
        try:
            j = json.loads(out[i:])
            return (j or {}).get("results") or []
        except Exception:  # noqa: BLE001
            pass

    return _parse_text_results(out)


def search_multi(cmds, q, timeout=60):
    """按顺序试多条检索通道，返回 (results, engine, has_snippet)。

    为什么需要多通道，且必须优先选「能给摘要」的那条
    -----------------------------------------------
    实测 2026-11-20 三条通道的能力差异：

      元宝 WSA      给摘要，但账号欠费 → ResourceUnavailable
      Tavily CLI    给摘要（content 字段有正文段落）
      TinyFish CLI  **只给标题 + URL，不给摘要**

    一开始用元宝，17 条全部返回 0，差点记成「搜不到同事件报道」，
    实际是欠费。改用 TinyFish 拿到 11 条但摘要全空 ——
    只有标题和链接，证明得了「这件事有人报了」，
    证明不了「内容是什么」，下游做字段抽取时不能当依据。

    用户 2026-11-20 明确要求「保留摘要原文」，所以排序是：
    能给摘要的优先；拿不到摘要也留着（标 evidence_strength=title_only），
    但不假装它等同于摘要。
    """
    best = ([], "", False)
    for cmd in cmds:
        cmd = (cmd or "").strip()
        if not cmd:
            continue
        res = search_json(cmd, q, timeout)
        if not res:
            continue
        has_snip = any((r.get("content") or r.get("passage") or "").strip()
                       for r in res)
        engine = os.path.basename(str(cmd).split()[0]).replace(".cmd", "")
        if has_snip:
            return res, engine, True      # 有摘要，直接用
        if not best[0]:
            best = (res, engine, False)   # 先留着，继续试下一条
    return best


def _parse_text_results(out):
    """解析 [n] 标题 / 缩进 URL 的纯文本结果。"""
    res = []
    lines = out.split("\n")
    cur_title = None
    for ln in lines:
        m = re.match(r"^\s*\[(\d+)\]\s*(.+?)\s*$", ln)
        if m:
            cur_title = m.group(2)
            continue
        m2 = re.match(r"^\s+(https?://\S+)\s*$", ln)
        if m2 and cur_title:
            res.append({"title": cur_title, "url": m2.group(1),
                        "content": "", "site": host_of(m2.group(1))})
            cur_title = None
    return res


def host_of(u):
    try:
        return str(u).split("/")[2].lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def slug_from_url(u):
    """从 URL 提取标题词，用于构造检索式。"""
    p = re.sub(r"^https?://[^/]+", "", str(u or ""))
    p = re.sub(r"\.(html?|php|aspx?)$", "", p)
    words = [w for w in re.split(r"[/_\-]+", p) if len(w) > 2 and not w.isdigit()]
    return " ".join(words[-8:])


def main():
    ap = argparse.ArgumentParser(
        description="搜索通道补充被反爬挡住的高价值信源")
    ap.add_argument("--store", default=r"D:\event-ledger-data")
    ap.add_argument("--search-cmd", action="append", required=True,
                    help="可给多次，按顺序试。能给摘要的通道优先会自动选")
    ap.add_argument("--tiers", default="P1", help="跑哪些档，逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多补几条")
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--out", default=None, help="补充结果 jsonl")
    a = ap.parse_args()

    from raw_store import RawStore
    import fetch_articles as F

    st = RawStore(a.store)
    cands, n_feed, n_entry = F.gather_candidates(st)

    # 判断「已有全文」不能只看有没有 article 记录。
    # 抓取失败时 raw_store.put() 也会写一条 meta（body 为空），
    # 那些记录 evidence_kind 不是 full_html。只看 kind=article 会把
    # 失败过的当成"已抓"，导致待补清单是空的 —— 实测正是如此：
    # 待抓 133 条全是失败过的，却被算作"已有"。
    have_full = set()
    for mp in __import__("glob").glob(
            os.path.join(st.root, "raw/meta", "*", "*.json")):
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") != "article":
            continue
        if m.get("evidence_kind") == "search_snippet":
            continue                      # 只有摘要，不算有全文
        if int(m.get("n_bytes") or m.get("versions", [{}])[-1].get("bytes") or 0) < 500:
            continue                      # 空 body，抓取失败
        have_full.add(m.get("url"))

    # 已经有摘要证据的，也不重复补
    have_snip = set()
    snip_path = os.path.join(st.root, "extracted", "search_snippets.jsonl")
    if os.path.isfile(snip_path):
        for line in io.open(snip_path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    have_snip.add(json.loads(line).get("url"))
                except Exception:  # noqa: BLE001
                    pass

    want = set(x.strip().upper() for x in a.tiers.split(",") if x.strip())
    todo = [c for c in cands
            if c["url"] not in have_full
            and c["url"] not in have_snip
            and TIERS.get(host_of(c["url"])) in want]
    if a.limit:
        todo = todo[:a.limit]

    print("")
    print("  待补 %d 条（档位 %s）" % (len(todo), a.tiers))
    if not todo:
        print("  无需补充。")
        return 0

    out_path = a.out or os.path.join(
        st.root, "extracted", "search_snippets.jsonl")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    n_ok = n_miss = 0
    t0 = time.time()
    with io.open(out_path, "a", encoding="utf-8") as f:
        for i, c in enumerate(todo, 1):
            u = c["url"]
            dom = host_of(u)
            tier = TIERS.get(dom, "?")
            # 检索式：优先用原标题，没有标题就用 URL slug
            q = (c.get("title") or "").strip()
            if len(q) < 12:
                q = slug_from_url(u)
            if not q:
                n_miss += 1
                continue

            res, engine, has_snip = search_multi(a.search_cmd, q)

            # 判据分两级，因为搜索结果的性质不同：
            #
            # 一级  同域名同路径 —— 最理想，就是原文被抓到了
            # 二级  别的媒体报了同一件事 —— 这也算"发现成功、内容获取不完整"
            #
            # 为什么必须有二级：实测 Times of Israel 被 403 挡住时，
            # 搜「confirms houthi attack damaged riyadhs tallest building」
            # 返回的是 New Indian Express、Times Now、中国网的同事件报道，
            # 没有 ToI 自己的页面（它整个站都挡境外 IP）。
            # 只认一级的话一条也补不上，而用户的判断标准是
            # 「发现了标题但没有正文，记录为发现成功、内容获取不完整」。
            same_dom = [r for r in res if host_of(r.get("url")) == dom]
            same_dom = [r for r in same_dom if _similar_path(r.get("url"), u)]
            if same_dom:
                best, match_level = same_dom[0], "same_source"
            else:
                # 二级：用标题词重合度确认是同一事件，不靠域名
                best, match_level = _pick_same_event(res, q), "other_source"
            mine = [best] if best else []
            if not mine:
                n_miss += 1
                if i % 10 == 0 or i == len(todo):
                    print("    %d/%d  命中 %d ｜ 未命中 %d"
                          % (i, len(todo), n_ok, n_miss))
                if a.sleep:
                    time.sleep(a.sleep)
                continue

            best = mine[0]
            snippet = (best.get("content") or best.get("passage") or "")[:2000]
            rec = {
                # doc_id 与全文证据共用同一个 id，保证证据能关联
                "doc_id": st.put(u, b"", 0, "", u,
                                 extra={"kind": "article",
                                        "evidence_kind": "search_snippet",
                                        "title": c.get("title"),
                                        "from_domain": dom,
                                        "blocked": True})[0],
                "url": u,
                "source_domain": dom,
                "tier": tier,
                "query": q,
                "queried_at": now().isoformat(),
                "match_level": match_level,
                "result_url": best.get("url"),
                "result_title": best.get("title"),
                "result_site": best.get("site"),
                "result_domain": host_of(best.get("url")),
                "snippet": snippet,
                # 证据强度要标出来。只有标题+链接的，能证明「这件事有人报了」，
                # 不能证明「内容是什么」—— 下游做字段抽取时不能拿它当依据。
                # 用户 2026-11-20 要的是「保留摘要原文」，
                # 拿不到摘要时要如实标注，不能假装有。
                "evidence_strength": "snippet" if snippet else "title_only",
                "engine": engine,
                "evidence_kind": "search_snippet",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_ok += 1
            print("    [%s/%s] %s" % (tier, match_level[:4], str(best.get("title"))[:52]))
            if a.sleep:
                time.sleep(a.sleep)

    print("")
    print("  补充成功 %d 条，未命中 %d 条，耗时 %.0f 秒"
          % (n_ok, n_miss, time.time() - t0))
    print("  证据已写入 → %s" % out_path)
    print("")
    print("  说明：这些记录 evidence_kind=search_snippet，与日后抓到的")
    print("  全文共用同一个 doc_id，不会变成两条孤立记录。")
    return 0 if n_ok else 2


def _pick_same_event(results, query):
    """从搜索结果里挑「报道同一事件」的那条。

    判据是标题词重合度 + 专名验证，不看域名。因为原文被反爬挡住时，
    同域名的结果根本不会出现，只能靠其他媒体的同事件报道做旁证。

    为什么光靠「2 个实词重合」不够 —— 实测的两类误配：
      「arrested connection with fatal shooting arara」
        → 配到「Two people arrested ... shooting near Aurora」
          Aurora（美国科罗拉多）≠ Arara（以色列），只差一个字母
      「palestinian student activist custody over alleged terror charges」
        → 配到「Report outlines alleged extremist ties...」
          摘要讲的是美国国情咨文，完全无关

    所以加两道：
      一、实词重合门槛提到 3 个
      二、必须有一个「专名」也对上 —— 专名指地名/组织名/人名，
          判断方式是：该词首字母大写（在原标题里）且不是句首词。
          纯小写词（arrested/shooting/fatal）重合再多也不能确认是同一事件。
    """
    if not results:
        return None
    STOP = set((
        "the a an and or of in on at to for with from by as is are was "
        "were be been being this that these those it its his her their our your "
        "says said say after before over under about into during while "
        "news update live blog report reports breaking latest video watch "
        "killed kill kills killing dead death deaths died attack attacks "
        "bombing bomb blast explosion shooting shot gunmen militant militants "
        "terror terrorist terrorism police officer officers official officials "
        "people person man woman men women child children year years old "
        "world today yesterday tomorrow week month year time "
        "liveblog entry entries strikes strike struck "
        # 这几类是法律/新闻套话，区分度为零，实测会造成误配：
        # 「alleged terror charges」配到「alleged extremist ties」
        # 结果摘要讲的是美国国情咨文，与巴勒斯坦学生完全无关。
        "alleged allegedly suspect suspected probe investigation "
        "court judge trial charges accused case ruling ruled "
        "ties link links outline outlines report reports new fresh "
        "amid amid set fire says say").split())

    def words(t):
        return set(w for w in re.findall(r"[A-Za-z]{3,}", str(t or "").lower())
                   if w not in STOP)

    # 专名：原标题里首字母大写、且不是第一个词
    raw_words = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}", str(query or ""))
    proper = set()
    for idx, w in enumerate(raw_words):
        if idx == 0:
            continue
        if w[0].isupper() and w.lower() not in STOP:
            proper.add(w.lower())

    qw = words(query)
    if not qw:
        return None

    best, best_score = None, 0
    for r in results:
        title = str(r.get("title") or "")
        rw = words(title)
        n = len(qw & rw)
        if n < 3:
            continue
        # 专名验证：结果的标题里必须出现原查询里的某个专名
        tl = title.lower()
        has_proper = any(p in tl for p in proper) if proper else False
        if proper and not has_proper:
            continue
        score = n + (2 if has_proper else 0)
        if score > best_score:
            best, best_score = r, score
    return best


def _similar_path(a_url, b_url):
    """判断两个 URL 是否指向同一篇文章。

    判据：路径最后一段相同，或互相包含。
    搜标题会返回大量同主题但不同事件的报道（比如都讲加沙袭击），
    只靠域名+标题相似度会把它们误认为同一条。
    """
    def tail(u):
        p = re.sub(r"^https?://[^/]+", "", str(u or "")).split("?")[0]
        seg = [x for x in p.split("/") if x]
        return seg[-1] if seg else ""
    ta, tb = tail(a_url), tail(b_url)
    if not ta or not tb:
        return False
    ta = re.sub(r"\.(html?|php|aspx?)$", "", ta).lower()
    tb = re.sub(r"\.(html?|php|aspx?)$", "", tb).lower()
    if ta == tb:
        return True
    # 长尾 slug 常有截断差异
    return len(ta) > 12 and len(tb) > 12 and (ta in tb or tb in ta)


if __name__ == "__main__":
    sys.exit(main())
