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
    """只查站点 + 时间窗，**不拼关键词**。

    Google News 的 q 是语义匹配不是布林表达式。实测把
    「(attack OR bombing OR killed OR militant OR terror …)」拼进去之后，
    返回的还是一整站的最新 100 条 —— 里面有比特币新闻、体育新闻、
    Jimmy Kimmel 的段子，跟袭击毫无关系。Google 把那些词当成了
    「话题倾向」而不是过滤条件。

    所以正确做法是：先无条件拿站点最新条目，再用本地词表过滤标题。
    本地词表和正文层初筛用的是同一套（fetch_articles.KW），
    多语种、已经过实测，比让 Google 猜要可靠。
    """
    q = "site:%s when:%dd" % (domain, days)
    return "https://news.google.com/rss/search?q=%s&hl=en" % (
        urllib.parse.quote(q))


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
        # 多语种、已经在十几万条条目上跑过，比让 Google 猜关键词可靠。
        kept = []
        for e in ents:
            t = (e.get("title") or "").strip()
            if not t or BAD_TITLE.match(t):
                continue
            if not FETCH.looks_like_attack(t, "", ""):
                continue
            kept.append(e)
        n_ok += 1
        n_item += len(kept)
        for e in kept:
            rows.append({
                "domain": dom,
                "title": e.get("title"),
                "link": e.get("link"),
                "published": e.get("published"),
                "fetched_at": S.iso(),
                "query": url,
                "source_type": "aggregator",
                "engine": "google_news_rss",
                # 关键：这条记录只能证明「Google 索引到了这条标题」，
                # 不能当正文证据。字段显式标出来，下游不能混用。
                "evidence_kind": "title_only",
                "note": "原文被 401/403 挡住；本通道只做发现，不做证据",
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