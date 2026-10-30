#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日期门禁：只认 published_time / JSON-LD / URL 内嵌日期，判定候选是否属于目标月份。

解决的真实事故：检索 API 的 --start-date/--end-date 不约束内容发布时间，只影响排序。
限定 7 月检索，返回里混进 8 月甚至 10 月事件，被直接登记成 7 月。

退出码：0 = 全部可判定且与声明月份一致 | 2 = 有属别月 / 无法判定 | 1 = 无法判定（输入问题）

用法：
    # 输入 jsonl，每行至少有 url；可选 title、claimed_month（如 "2026-07"）
    python scripts/gate_date_published.py 候选池.jsonl --month 2026-07
    python scripts/gate_date_published.py 候选池.jsonl --month 2026-07 --workers 16
    python scripts/gate_date_published.py 候选池.jsonl --month 2026-07 --fetch off
    python scripts/gate_date_published.py 候选池.jsonl --month 2026-07 --json

--fetch off 时只用 URL / 标题里的日期（免联网，适合先粗筛一遍）。
"""
import argparse
import json
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

META_PATS = [
    (r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)', "meta:published_time"),
    (r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time', "meta:published_time"),
    (r'name=["\']pubdate["\'][^>]*content=["\']([^"\']+)', "meta:pubdate"),
    (r'name=["\']publish-date["\'][^>]*content=["\']([^"\']+)', "meta:publish-date"),
    (r'"datePublished"\s*:\s*"([^"]+)"', "jsonld:datePublished"),
    (r'"publishedAt"\s*:\s*"([^"]+)"', "json:publishedAt"),
    (r'"publishTime"\s*:\s*"([^"]+)"', "json:publishTime"),
]
URL_PATS = [
    r'/(20\d{2})/(\d{1,2})/(\d{1,2})/',          # /2026/07/02/
    r'/(20\d{2})/(\d{1,2})-(\d{1,2})',           # /2026/07-02   （中新网等）
    r'/(20\d{2})-(\d{1,2})-(\d{1,2})',           # /2026-07-02
    r'/(20\d{2})(\d{2})(\d{2})/',                # /20260702/
    r'[?&]date=(20\d{2})-(\d{1,2})-(\d{1,2})',
]
TITLE_PATS = [
    r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})',
    r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(20\d{2})',
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})',
]
MON = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def ym_of(y, m):
    try:
        y, m = int(y), int(m)
    except Exception:  # noqa: BLE001
        return None
    if 1 <= m <= 12 and 1990 <= y <= 2100:
        return "%04d-%02d" % (y, m)
    return None


def date_from_url(url):
    for p in URL_PATS:
        m = re.search(p, url or "")
        if m:
            r = ym_of(m.group(1), m.group(2))
            if r:
                return r, "url"
    return None, None


def date_from_title(title):
    t = title or ""
    for p in TITLE_PATS:
        m = re.search(p, t, re.I)
        if not m:
            continue
        g = m.groups()
        if len(g) == 3 and str(g[0]).isdigit() and len(str(g[0])) == 4:
            r = ym_of(g[0], g[1])
            if r:
                return r, "title"
        if len(g) == 3 and str(g[2]).isdigit():
            if str(g[1])[:3].lower() in MON:
                r = ym_of(g[2], MON[str(g[1])[:3].lower()])
                if r:
                    return r, "title"
            elif str(g[0])[:3].lower() in MON:
                r = ym_of(g[2], MON[str(g[0])[:3].lower()])
                if r:
                    return r, "title"
    return None, None


def date_from_html(html):
    for p, tag in META_PATS:
        m = re.search(p, html, re.I)
        if not m:
            continue
        raw = m.group(1)
        mm = re.search(r'(20\d{2})[-/](\d{1,2})', raw)
        if mm:
            r = ym_of(mm.group(1), mm.group(2))
            if r:
                return r, tag, raw
        mm = re.search(r'(20\d{2})(\d{2})(\d{2})', raw)
        if mm:
            r = ym_of(mm.group(1), mm.group(2))
            if r:
                return r, tag, raw
    return None, None, None


def fetch(session, url, timeout):
    """返回 (html, 诊断)。诊断用于区分『抓不到』与『抓到但无 meta』——
    这正是『渠道不通』与『未报道』的界线，不许合成一个未知。

    代理兜底：本机常设了 HTTP_PROXY 但代理没开，会把国内站也一起拖成 ProxyError。
    此类失败自动用「忽略环境代理」直连重试一次。
    """
    try:
        r = session.get(url, timeout=timeout, verify=False, allow_redirects=True)
        html = r.text or ""
        return html, {"http": r.status_code, "bytes": len(html)}
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        if "Proxy" in name or "Connect" in name:
            try:
                s2 = _direct_session()
                r = s2.get(url, timeout=timeout, verify=False, allow_redirects=True)
                html = r.text or ""
                return html, {"http": r.status_code, "bytes": len(html),
                              "retry": "direct-no-proxy"}
            except Exception as e2:  # noqa: BLE001
                return "", {"http": None, "error": "%s->%s" % (name, type(e2).__name__)}
        return "", {"http": None, "error": name}


def _direct_session():
    import requests as _rq
    s = _rq.Session()
    s.trust_env = False          # 忽略 HTTP_PROXY / HTTPS_PROXY
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def one(session, rec, month, do_fetch, timeout):
    url = rec.get("url") or ""
    title = rec.get("title") or ""
    got, how, raw = None, None, None
    diag = {}
    alt = []          # 备选日期来源，用于冲突检测

    # 顺序严格按 reference/03-核验.md：meta / JSON-LD  >  URL 内嵌  >  标题。
    # 原实现是 URL > 标题 > meta，会把「URL 里是旧日期、正文其实是新月」的稿子漏放。
    if do_fetch and url:
        html, diag = fetch(session, url, timeout)
        if html:
            got, how, raw = date_from_html(html)

    u_m, u_how = date_from_url(url)
    if u_m:
        alt.append((u_m, u_how))
    t_m, t_how = date_from_title(title)
    if t_m:
        alt.append((t_m, t_how))

    if not got and alt:
        got, how = alt[0]

    status = "unknown"
    if got:
        status = "ok" if got == month else "other_month"

    out = dict(rec)
    out.update({"published_month": got, "date_source": how, "status": status})
    if raw:
        out["date_raw"] = raw
    if alt:
        out["alt_dates"] = [{"month": m, "via": w} for m, w in alt]

    # 冲突：权威源（meta）与 URL/标题 指向不同月份 -> 必须报出来，不许静默采信一个
    conflicts = sorted({m for m, _ in alt if m and got and m != got})
    if conflicts:
        out["date_conflict"] = conflicts

    if diag:
        out["fetch"] = diag
        if not got:
            if diag.get("http") == 404:
                out["reason"] = "dead_404"
            elif diag.get("bytes", 0) > 2000:
                out["reason"] = "no_meta_in_html"
            else:
                out["reason"] = "blocked_or_empty"
    elif not got:
        out["reason"] = "no_date_in_url_or_title"

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="jsonl（每行含 url）/ 或 json 数组")
    ap.add_argument("--month", required=True, help="目标月份 YYYY-MM")
    ap.add_argument("--fetch", choices=["on", "off"], default="on",
                    help="off = 只从 URL/标题取日期，不联网")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out", default=None, help="结果写回 jsonl 路径")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        if a.input.lower().endswith(".json"):
            recs = json.load(open(a.input, encoding="utf-8"))
        else:
            recs = [json.loads(l) for l in open(a.input, encoding="utf-8") if l.strip()]
    except Exception as e:  # noqa: BLE001
        print("无法读取输入：%s" % e)
        return 1

    if not isinstance(recs, list) or not recs:
        print("输入为空或不合法。")
        return 1

    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    except Exception as e:  # noqa: BLE001
        if a.fetch == "on":
            print("缺少 requests，无法联网取 meta：%s" % e)
            print("  处置：python -m pip install requests  或  加 --fetch off 只用地里/标题日期")
            return 1
        session = None

    results = []
    if a.fetch == "off" or session is None:
        for r in recs:
            results.append(one(None, r, a.month, False, a.timeout))
    else:
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            futs = {ex.submit(one, session, r, a.month, True, a.timeout): i
                    for i, r in enumerate(recs)}
            buf = [None] * len(recs)
            for f in as_completed(futs):
                buf[futs[f]] = f.result()
            results = buf

    ok = [r for r in results if r["status"] == "ok"]
    other = [r for r in results if r["status"] == "other_month"]
    unknown = [r for r in results if r["status"] == "unknown"]

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    exit_code = 0 if (not other and not unknown) else 2

    reasons = {}
    for r in unknown:
        reasons[r.get("reason", "?")] = reasons.get(r.get("reason", "?"), 0) + 1

    if a.json:
        print(json.dumps({
            "input": a.input, "month": a.month, "total": len(results),
            "ok": len(ok), "other_month": len(other), "unknown": len(unknown),
            "other_month_items": [{"url": r.get("url"), "got": r["published_month"],
                                   "via": r["date_source"]} for r in other[:30]],
            "unknown_reasons": reasons,
            # 判定依据必须能复核：来源 + 备选日期 + 冲突
            "date_sources": {r.get("date_source") or "none": 1 for r in results},
            "conflicts": [{"url": r.get("url"),
                           "used": r.get("published_month"),
                           "via": r.get("date_source"),
                           "also_says": r.get("date_conflict")}
                          for r in results if r.get("date_conflict")],
            "alt_dates": [{"url": r.get("url"), "alt": r.get("alt_dates")}
                          for r in results if r.get("alt_dates")],
            "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 66)
    print(" 发布日期门禁 · 只认 published_time / JSON-LD / URL 内嵌日期")
    print("=" * 66)
    print("目标月份 : %s" % a.month)
    print("候选条数 : %d" % len(results))
    print("月份一致 : %d" % len(ok))
    print("**属别月 : %d  <- 这些不许登记为 %s**" % (len(other), a.month))
    print("无法判定 : %d  <- 这些不许当‘已确证为 %s’" % (len(unknown), a.month))
    print()

    if other:
        print("-- 属别月的条目（前 30）-----------------------------------------")
        for r in other[:30]:
            print("  %s  实为 %s  via %-22s %s"
                  % (r["published_month"], r["published_month"], r["date_source"],
                     (r.get("url") or "")[:70]))
        print()

    if unknown:
        print("-- 无法判定的条目（前 20）---------------------------------------")
        for r in unknown[:20]:
            f = r.get("fetch") or {}
            extra = ""
            if "error" in f:
                extra = "抓取失败(%s)" % f["error"]
            elif "bytes" in f:
                extra = "http=%s bytes=%s %s" % (f.get("http"), f["bytes"],
                                                 "抓到但无 meta" if f["bytes"] > 2000 else "内容过薄")
            print("  [%-18s] %s  %s" % (r.get("reason", "?"), (r.get("url") or "")[:68], extra))
        print()
        print("  三类的处置完全不同，不许合成一句‘无法判定’：")
        print("   · blocked_or_empty / 抓取失败 -> 渠道不通：标‘渠道不通’，不得记成‘未报道’")
        print("   · no_meta_in_html             -> 抓到正文但页面无日期元数据：改 --fetch off")
        print("                                    用标题/URL 日期，或读正文里的日期")
        print("   · no_date_in_url_or_title     -> 三处都没有日期：必须人工确认，不能默认当月")
        print("   · dead_404                    -> 只有 404 才算死链，必须补证或换源")
        print()

    if a.out:
        print("结果已写回：%s" % a.out)
    print("结论：%s" % ("通过。所有条目均可确证为目标月份。"
                    if exit_code == 0 else
                    "不通过。属别月的条目必须剔除；无法判定的必须单列，不能当已确证。"))
    print("=" * 66)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
