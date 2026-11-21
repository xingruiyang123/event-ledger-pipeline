"""用 Google News RSS 给被 Cloudflare 挡住的源建代理入口。

问题
----
404 个无入口信源里，高价值的那批（reuters、apnews、arabnews、
tolonews、pna.gov.ph、theprint、newarab…）直接访问全是 403，
响应体是 Cloudflare 的「Just a moment...」。

试过并排除的路：
  · 完整浏览器请求头（含 Sec-Fetch-*、Sec-Ch-Ua）→ 仍 403
  · curl_cffi 三种指纹（chrome / safari / firefox）→ 仍 403
  → 不是 UA 或 TLS 指纹问题，是 **IP 级别判定**。改请求头没用。

可行的路：Google News RSS
------------------------
按「站点 + 关键词」查，Google 已经帮我们抓过一遍：

  https://news.google.com/rss/search?q=site:arabnews.com+(attack OR ...)+when:2d

实测四家全部 200，且拿到的就是相关条目：
  arabnews.com   100 条
  reuters.com    100 条
  pna.gov.ph      18 条
  tolonews.com     9 条

能力边界（必须写清楚，不能假装等同原站 feed）
-------------------------------------------
  拿到：标题、发布时间、Google 的跳转链接
  拿不到：原文 URL、摘要正文

Google 的跳转链接是 protobuf base64，三种解码都试过：
  ① base64 直接解 —— 里面没有明文 URL
  ② 跟 302 —— 跳回自己
  ③ batchexecute 接口 —— 返回 null
所以**这一条通道只能做发现层，不能做证据层**。

用法定位：
  · 标题命中袭击词 → 作为「发现」记进候选池，标注 source_type=aggregator
  · 正文入库要另走搜索补充通道（Tavily 能拿到摘要和原文 URL）
  · 台账里这类来源的置信度不能给「高」

这正是用户定的那条原则：政府公告/聚合平台的条目
不等于原发媒体报道，来源类型要留着区分。
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 查询式：站点 + 袭击词。词表与正文层的初筛保持一致，
# 但这里只能靠标题，所以用更宽的词面（标题常常不含「terrorism」）。
QUERY_TERMS = [
    "attack", "killed", "bombing", "bomb", "militant", "terror",
    "shooting", "gunmen", "blast", "suicide", "explosion", "ambush",
    "kidnap", "abduct", "assassinat", "insurgent", "extremist",
]

# 这些词表明文章与该站无关（Google 会混入别的站），按标题尾部信源名过滤
BAD_TITLE = re.compile(
    r"^(?:video|photos?|watch|live updates?|explainer|opinion|analysis)\b",
    re.I)

# 本地词表在 main() 里 import；模块级别留个占位，
# 避免调用 _keep() 时报 NameError。
FETCH = None


def build_query(domain, days=2):
    """Bing News RSS —— 它给的是**原文 URL**，Google 不给。

    两条聚合通道的实测差异（2026-11-21）：

      Google News RSS   site:xxx 能拿 100 条标题，但链接是
                        news.google.com/rss/articles/<base64>，
                        跟到底还是 Google 自己的页面，解不出原文。
                        三种解码都试过：base64 里没有明文 URL、
                        302 跳回自己、batchexecute 返回 null。
      Bing News RSS     site:xxx 拿 12 条，链接是
                        bing.com/news/apiclick.aspx?...&url=<原文URL>，
                        跟一次 302 就落到原文页，**能拿到原文 URL**。

    所以走 Bing。Bing 只给 12 条，但给的是真链接 ——
    台账里必须存原文 URL，这一条更重要。
    """
    q = "site:%s" % domain
    return ("https://www.bing.com/news/search?q=%s&format=rss"
            % urllib.parse.quote(q))


def unwrap_bing(link):
    """把 bing 的 apiclick 链接拆成原文 URL。

    URL 就在查询参数的 url= 里，不用发请求就能解出来。
    实测解出来的形如
      https://www.vanguardngr.com/2026/11/tinubus-re-election...
    跟一次 302 也确认落到同一个地址。
    """
    if not link:
        return ""
    if "bing.com/news/apiclick" not in link:
        return link
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        u = (qs.get("url") or [""])[0]
        return u or ""
    except Exception:  # noqa: BLE001
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=r"D:\event-ledger-data\state")
    ap.add_argument("--store", default=r"D:\event-ledger-data")
    ap.add_argument("--out", default=r"D:\event-ledger-data\extracted\gnews_feed.jsonl")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    import scheduler as S
    import fetch_articles as FETCH
    S.detect_proxy(None)

    # 哪些源需要走这条通道：主表里无入口 + 证据显示是 403
    import glob
    need = []
    for f in glob.glob(os.path.join(a.state_dir, "*.json")):
        try:
            j = json.load(io.open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        err = str(j.get("last_error") or "")
        if "403" in err or "401" in err:
            need.append(j.get("domain"))
    need = sorted(set(d for d in need if d))
    if a.limit:
        need = need[:a.limit]

    print("")
    print("  需要走 Google News 通道的源 %d 家（被 401/403 挡住的）" % len(need))
    print("")

    rows = []
    n_ok = n_item = 0
    for i, dom in enumerate(need, 1):
        url = build_query(dom, a.days)
        code, body, enc, final = S.http_get(url, timeout=25, retries=1)
        if code != 200 or not body:
            print("  %3d/%d  %-26s HTTP %s" % (i, len(need), dom[:24], code))
            time.sleep(a.sleep)
            continue
        ents = S.parse_feed(body)
        # 用本地袭击词表过标题。词表来自 fetch_articles.KW ——
        # 多语种、已经在十几万条条目上跑过，比让搜索引擎猜关键词可靠。
        kept = []
        for e in ents:
            t = (e.get("title") or "").strip()
            if not t or BAD_TITLE.match(t):
                continue
            real = unwrap_bing(e.get("link") or "")
            if not real:
                continue
            if not FETCH.looks_like_attack(t, "", ""):
                continue
            e = dict(e)
            e["real_url"] = real
            kept.append(e)
        n_ok += 1
        n_item += len(kept)
        for e in kept:
            rows.append({
                "domain": dom,
                "title": e.get("title"),
                # 存原文 URL，不是 bing 的跳转链接 ——
                # 台账里「信息来源URL」必须是能直接打开的原文地址
                "link": e.get("real_url"),
                "agg_link": e.get("link"),
                "published": e.get("published"),
                "fetched_at": S.iso(),
                "query": url,
                "source_type": "aggregator",
                "engine": "bing_news_rss",
                # 能力边界：本通道给到的是「标题 + 原文 URL」，
                # 没有摘要正文。正文要另走正文层去抓。
                "evidence_kind": "title_only",
                "note": "原站 feed 被 401/403 挡住；经聚合通道发现，正文需另行抓取",
            })
        print("  %3d/%d  %-26s %d 条" % (i, len(need), dom[:24], len(kept)))
        time.sleep(a.sleep)

    print("")
    print("  通了 %d/%d 家 ｜ 累计标题 %d 条" % (n_ok, len(need), n_item))

    if rows:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with io.open(a.out, "a", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("  写入 → %s" % a.out)

    if a.json:
        print(json.dumps({"sources": len(need), "ok": n_ok,
                          "titles": n_item}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())