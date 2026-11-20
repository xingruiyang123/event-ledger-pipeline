"""从 feed 快照里取条目，按标题初筛，抓正文并存快照。

为什么需要这一层
----------------
`scheduler.py` 抓的是订阅源（feed），存下来的是**目录**：标题、链接、时间。
目录不是文章。文章删了、网站改版了，目录里的链接就是死的。

从第一性原理看，根本问题是「把新出现的文章完整、可追溯地存下来」，
所以正文必须落盘。

为什么按标题初筛，而 feed 层不过滤
----------------------------------
这是两个不同性质的过滤，不能混为一谈：

  feed 层   一个源一篇文章，不加筛选全量入库。
            因为入口是「这个源今天有什么」，筛掉就等于放弃召回，
            而且筛掉之后没法再找回 —— 订阅源窗口会滚过去。

  正文层    40 万条目录，逐条抓正文（每篇 50-60 KB）就是 20 GB 以上，
            而且大量重复（同一篇被几十家转载）。
            但目录已经在库里，**任何时候都能重来** —— 这一层的筛选
            不损失可恢复性，所以可以按优先级抓。

换句话说：feed 层不过滤是因为"丢不起"，正文层筛选是因为"存得下、可重来"。

筛选标准
--------
标题命中任一语种的袭击词就算候选。宽松筛，不追求精确 ——
精确判定是下游的事，这一层只负责别把明显无关的（体育、娱乐、财经）捞进来。

实测（2026-11-19）：通用新闻 RSS 的恐袭密度约 10%，所以 40 万条里
大约 4 万条候选，按每篇 60 KB 算约 2.4 GB，可接受。

去重
----
按 URL 去重（跨源转载的同一篇会被重复抓）。doc_id = sha256(url)，
raw_store 天然幂等。但同一篇被 20 家转载时 URL 不同，这里不做内容去重 ——
那是处理层的事，原始层保留每个来源的版本，才能看出"谁转载了谁"。
"""
import argparse
import io
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 多语种袭击词。实测乌尔都文命中率 90% 但精确率低，
# 所以这里只用来「捞候选」，不用来「判定」。
KW = {
    "en": re.compile(
        r"\b(attack|attacks|attacked|killed|kill|killings|dead|deaths|died|"
        r"bomb|bombing|bomber|blast|explosion|explode|gunmen|gunman|"
        r"militant|militants|terror|terrorist|terrorism|insurgent|"
        r"shooting|shot|ambush|abduct|abduction|kidnap|hostage|"
        r"ied|suicide|roadside|grenade|rocket|mortar|shelling|"
        r"massacre|raid|offensive|clashes|assassinat|execute|behead)", re.I),
    "zh": re.compile(
        r"(袭击|恐怖|爆炸|炸弹|自杀式|枪击|枪杀|伏击|武装分子|"
        r"击毙|打死|身亡|遇难|绑架|劫持|屠杀|交火|扫射|"
        r"简易爆炸装置|路边炸弹|火箭弹|迫击炮|暗杀|斩首)"),
    "ur": re.compile(
        r"(حملہ|حملے|ہلاک|مارا گیا|قتل|دھماکہ|دھماکے|بم|فائرنگ|"
        r"عسکریت پسند|دہشت گرد|شدت پسند|فوج|پولیس|چوکی|"
        r"اغوا|جبری لاپتہ|زخمی|لاشیں|کارروائی)"),
    "ar": re.compile(
        r"(هجوم|هجمات|قتل|قتلى|مقتل|انفجار|تفجير|مسلح|مسلحون|"
        r"إرهابي|ارهابي|إرهاب|اعتداء|اختطاف|اشتباك|غارة)"),
    "fr": re.compile(
        r"\b(attaque|attaques|tué|tués|morts|mort|bombardement|"
        r"explosion|explosif|tir|fusillade|embuscade|enlèvement|"
        r"terroriste|terrorisme|insurgé|djihadiste|massacre)\b", re.I),
    "es": re.compile(
        r"\b(ataque|ataques|muerto|muertos|asesinato|bomba|"
        r"explosión|explosivo|disparo|tiroteo|emboscada|secuestro|"
        r"terrorista|terrorismo|insurgente|masacre)\b", re.I),
    "pt": re.compile(
        r"\b(ataque|ataques|morto|mortos|assassinato|bomba|"
        r"explosão|explosivo|tiro|tiroteio|emboscada|sequestro|"
        r"terrorista|terrorismo|massacre)\b", re.I),
}


# 明显不是文章的路径段。实测 sitemap 的 URL slug 匹配会捞进大量非新闻：
#   /people/matt-killingsworth  人名里带 killings
#   /podcast/terrorist-s-wife   播客
#   /video/president-xi-...     视频
#   /tag/attack-drones          标签页
# 这些页面本身能打开（200、十几万字节），但里面没有事件，抓下来纯浪费。
NOT_ARTICLE = re.compile(
    r"/(people|person|author|authors|staff|profile|profiles|bio|"
    r"tag|tags|category|categories|topic|topics|section|sections|"
    r"podcast|podcasts|video|videos|photo|photos|gallery|galleries|"
    r"about|contact|subscribe|subscription|login|signin|register|"
    r"privacy|terms|faq|help|advertise|careers|jobs|team|"
    r"search|archive|archives|index|feed|rss|sitemap)(/|$|\?)", re.I)

# 像文章的路径特征。命中任一条加分。
ARTICLE_HINT = re.compile(
    r"(/\d{4}/\d{1,2}(/|$)|"
    r"/(article|articles|news|story|stories|post|posts|report|reports|"
    r"world|politics|security|conflict|crime|investigation)/|"
    r"\.(html?|shtml|php)(\?|$))", re.I)


def url_slug(url):
    """把 URL 路径转成可比对的词串。

    为什么需要：sitemap 类入口的条目只有 URL、没有标题（实测 95 个源、
    382,952 条全无标题）。只按标题筛，这 38 万条一条也进不来。
    而 URL 里其实有 slug，`/explosion-heard-in-north-of-saudi-arabia`
    这种信息量和标题差不多。
    """
    u = str(url or "").lower()
    u = re.sub(r"^https?://[^/]+", "", u)     # 去掉域名，域名里的词不算
    return re.sub(r"[^a-z0-9]+", " ", u)


def looks_like_attack(title, summary="", url=""):
    """标题或 URL slug 命中任一语种袭击词 → 候选。返回命中的语种列表。

    三个来源都看：标题最准，摘要次之，URL slug 兜住 sitemap 那种无标题条目。
    """
    t = "%s %s" % (title or "", summary or "")
    hits = [k for k, p in KW.items() if p.search(t)]
    if not hits and url:
        s = url_slug(url)
        hits = [k for k, p in KW.items() if p.search(s)]
    return hits


def is_probably_article(url, title=""):
    """判断一个 URL 是不是文章页。

    只在「没有标题」（sitemap 来源）时严格判断 ——
    有标题的条目说明 feed 明确给了文章标题，不用再猜。

    实测的坏 URL 类型（都真实抓到过）：
      http://default/en/faqs/...      主机名解析错了，sitemap 里常见
      .../mortar-board.pdf            sitemap 收录了 PDF 和图片
      .../people/matt-killingsworth   人名里带 killings
    """
    if (title or "").strip():
        return True
    u = str(url or "")
    if not u.startswith("http"):
        return False
    # 主机名异常：default / localhost / 单段名，是 sitemap 生成错误
    try:
        host = u.split("/")[2].lower()
    except Exception:  # noqa: BLE001
        return False
    if host in ("default", "localhost", "example.com") or "." not in host:
        return False
    # 非 HTML 资源
    if re.search(r"\.(pdf|jpe?g|png|gif|webp|svg|zip|rar|mp[34]|avi|mov|"
                 r"docx?|xlsx?|pptx?|csv|json|xml)(\?|$)", u, re.I):
        return False
    if NOT_ARTICLE.search(u):
        return False
    # 路径太短通常是栏目页（如 /world、/news）
    path = re.sub(r"^https?://[^/]+", "", u).split("?")[0]
    if len(path.strip("/")) < 8:
        return False
    return True


def gather_candidates(store, extractions=None):
    """从 feed 快照里取所有条目，筛出候选。"""
    import glob
    metas = glob.glob(os.path.join(store.root, "raw/meta", "*", "*.json"))
    seen = set()
    out = []
    n_feed = n_entry = 0
    for mp in metas:
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") != "feed":
            continue
        n_feed += 1
        html = store.get_html(m["doc_id"])
        if not html:
            continue
        try:
            from scheduler import parse_feed
            entries = parse_feed(html)
        except Exception:  # noqa: BLE001
            continue
        for e in entries:
            link = e.get("link") or ""
            if not link.startswith("http"):
                continue
            n_entry += 1
            if link in seen:
                continue
            seen.add(link)
            langs = looks_like_attack(e.get("title"), e.get("summary"), link)
            if not langs:
                continue
            # sitemap 来源没有标题，URL 里的词容易误中（/people/matt-killingsworth），
            # 加一道「是不是文章页」的判定，把人物页、播客、视频挡在外面。
            if not is_probably_article(link, e.get("title")):
                continue
            out.append({
                "url": link,
                "title": e.get("title"),
                "published": e.get("published"),
                "langs": langs,
                "from_domain": m.get("domain"),
                "via": "sitemap" if not (e.get("title") or "").strip() else "rss",
            })
    return out, n_feed, n_entry


def main():
    ap = argparse.ArgumentParser(
        description="从 feed 目录里取候选，抓正文并存原始快照")
    ap.add_argument("--store", default=r"D:\event-ledger-data")
    ap.add_argument("--limit", type=int, default=0, help="最多抓几篇（0=全部）")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--candidates-out", default=None,
                    help="候选清单写这里（jsonl），便于检查筛得对不对")
    ap.add_argument("--report", default=None)
    a = ap.parse_args()

    from raw_store import RawStore
    store = RawStore(a.store)

    # 代理：与 scheduler 同一套探测
    import scheduler as S
    if a.no_proxy:
        S.PROXY_DISABLED = True
        globals()["_NO_PROXY"] = True
    else:
        p, sec = S.detect_proxy(a.proxy)
        print("  代理：%s（%.1fs）" % (p or "无，走直连", sec))

    print("")
    print("  从 feed 快照里取候选...")
    cands, n_feed, n_entry = gather_candidates(store)
    print("    feed 快照 %d 个，条目 %d 条，候选 %d 条（%.0f%%）"
          % (n_feed, n_entry, len(cands),
             100.0 * len(cands) / max(1, n_entry)))

    if a.candidates_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.candidates_out)),
                    exist_ok=True)
        with io.open(a.candidates_out, "w", encoding="utf-8") as f:
            for c in cands:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print("    候选清单 → %s" % a.candidates_out)

    # 已经抓过正文的跳过。
    #
    # 这里必须区分「抓到了」和「试过但失败了」：
    # 抓取失败时 raw_store 也会写一条 meta（body 为空），如果只看
    # kind=article 就把它当成功，会漏掉该重试的；
    # 如果完全不看，又会每轮把同一批死链重新抓一遍 ——
    # 实测每轮 600 篇里稳定有 134 篇失败，就是同一批 403 死循环占配额。
    #
    # 所以：成功的跳过；失败的记进 fail_counts，超过阈值就不再排。
    import glob
    have = set()
    fail_counts = {}
    for mp in glob.glob(os.path.join(store.root, "raw/meta", "*", "*.json")):
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") != "article":
            continue
        url = m.get("url")
        if not url:
            continue
        # 有实际内容的才算成功
        nbytes = int(m.get("n_bytes") or 0)
        if not nbytes:
            vers = m.get("versions") or []
            if vers:
                nbytes = int(vers[-1].get("bytes") or 0)
        if nbytes >= 500:
            have.add(url)
        else:
            fail_counts[url] = int(m.get("fail_count") or 1)

    # 失败次数已达上限的不再排 —— 留着让人工或搜索通道处理，
    # 不再每轮白占 600 篇的配额。
    MAX_FAIL = int(os.environ.get("ARTICLE_MAX_FAIL", "3"))
    skipped_dead = [c for c in cands
                    if c["url"] not in have
                    and fail_counts.get(c["url"], 0) >= MAX_FAIL]
    todo = [c for c in cands
            if c["url"] not in have
            and fail_counts.get(c["url"], 0) < MAX_FAIL]
    print("    已抓过 %d 篇，待抓 %d 篇" % (len(have), len(todo)))
    if skipped_dead:
        print("    弃抓 %d 篇（失败已达 %d 次，转搜索通道或人工）"
              % (len(skipped_dead), MAX_FAIL))

    # 限流必须在去重之后。
    # 原来写成先 cands[:limit] 再去重，后果是每轮只取候选清单的前 300 条，
    # 减去已抓的之后只剩个位数，后面的 1 万条永远轮不到 ——
    # 手工跑 --limit 40 时因为候选多没暴露，定时任务跑几轮才现形：
    # 日志里「候选 10556 / 已抓 293 / 本次要抓 7」，正是这个 bug。
    if a.limit and len(todo) > a.limit:
        print("    本轮限量 %d 篇（待抓 %d 篇，剩余下轮继续）"
              % (a.limit, len(todo)))
        todo = todo[:a.limit]
    print("")

    if not todo:
        print("  无需抓取。")
        return 0

    # 正文抽取用 extract_article_fields 里的 html_to_text
    try:
        import extract_article_fields as E
        to_text = E.html_to_text
    except Exception:  # noqa: BLE001
        def to_text(h):
            h = h.decode("utf-8", "replace") if isinstance(h, bytes) else h
            h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
            return " ".join(re.sub(r"<[^>]+>", " ", h).split())

    from concurrent.futures import ThreadPoolExecutor

    n_ok = n_fail = n_new = 0
    t0 = time.time()

    def work(c):
        code, body, enc, final = S.http_get(c["url"], a.timeout, retries=1)
        if code != 200 or len(body) < 500:
            # 失败也要记账 —— 否则同一批死链会被无限重试。
            # 写一条 body 为空、带 fail_count 的 meta，下一轮的
            # 待抓清单里就能识别出「这个 URL 已经失败 N 次了」。
            # 实测教训：不记账时每轮 600 篇里稳定 134 篇是同一批
            # 403/404 死链，永远抓不到，却每轮占满配额。
            try:
                from raw_store import doc_id_of
                prev = store.get_meta(doc_id_of(c["url"])) or {}
                fc = int(prev.get("fail_count") or 0) + 1
                store.put(c["url"], b"", code, enc or "", final or c["url"],
                          extra={"kind": "article",
                                 "title": c.get("title"),
                                 "from_domain": c.get("from_domain"),
                                 "failed": True,
                                 "fail_count": fc,
                                 "last_code": code})
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "url": c["url"], "code": code}
        try:
            store.put(c["url"], body, code, enc, final,
                      extra={"kind": "article", "title": c["title"],
                             "from_domain": c["from_domain"],
                             "langs": c["langs"],
                             "published": c["published"]})
        except Exception:  # noqa: BLE001
            return {"ok": False, "url": c["url"], "code": -1}
        html = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        try:
            txt = to_text(html) if callable(to_text) else ""
        except Exception:  # noqa: BLE001
            txt = ""
        return {"ok": True, "url": c["url"], "bytes": len(body),
                "text_len": len(txt or "")}

    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(work, todo), 1):
            results.append(r)
            if r["ok"]:
                n_ok += 1
            else:
                n_fail += 1
            if i % 50 == 0 or i == len(todo):
                el = time.time() - t0
                print("    %d/%d  成功 %d ｜ 失败 %d ｜ %.0f 秒 ｜ %.1f 篇/分"
                      % (i, len(todo), n_ok, n_fail, el,
                         i / max(el / 60, 0.01)))

    # 失败明细要记全，不能只留 30 条样本。
    # 原来写的是 [:30]，实测失败 133 条时只留了 30 条，
    # 导致按失败码统计出来的分布完全失真 —— 想查「403 占多少」都查不准。
    # 现在按码聚合全量，明细只留前 200 条供抽查。
    fails = [r for r in results if not r["ok"]]
    by_code = {}
    for r in fails:
        k = str(r.get("code"))
        by_code[k] = by_code.get(k, 0) + 1

    rep = {
        "feed_snapshots": n_feed, "feed_entries": n_entry,
        "candidates": len(cands), "attempted": len(todo),
        "ok": n_ok, "failed": n_fail,
        "success_rate": round(100.0 * n_ok / max(1, len(todo)), 1),
        "elapsed_s": round(time.time() - t0, 1),
        "failed_by_code": by_code,
        "sample_failures": fails[:200],
    }
    if a.report:
        io.open(a.report, "w", encoding="utf-8").write(
            json.dumps(rep, ensure_ascii=False, indent=1))

    print("")
    print("  ── 汇总 ──")
    print("    抓取 %d 篇，成功 %d，失败 %d，成功率 %.1f%%"
          % (len(todo), n_ok, n_fail, 100.0 * n_ok / max(1, len(todo))))
    print("    耗时 %.0f 秒" % rep["elapsed_s"])
    if a.report:
        print("    报告 → %s" % a.report)
    # 退出码判定：全失败才报错，但要排除「队列已到底」的情况。
    # 队列末尾本来就是抓不到的硬尾巴（403/404），全失败是正常状态。
    # 实测教训：待抓只剩 4 篇、4 篇全失败 → 返回 2 → 整条流水线 exit=2，
    # 计划任务看起来像挂了，实际是「今天该抓的都抓完了」。
    # 判据改为：样本量 ≥ 20 且成功率为 0 才算系统性故障。
    if len(todo) >= 20 and n_ok == 0:
        print("    ! 全部失败且样本量 ≥20，疑似代理/网络故障")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
