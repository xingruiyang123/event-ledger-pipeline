"""给「探不到入口」的源做归因，而不是继续盲探。

为什么要先归因
--------------
404 家无入口源，两段式探测（17 条路径）只回收 9 家。
剩下 395 家如果继续「猜更多路径」，是在赌运气。
先把它们分成互斥的几类，才能知道该建什么能力：

  HTTP_403 / 401     被 Cloudflare 或 IP 判定挡住 → 需要代理或聚合通道
  DNS_FAIL           域名解析不了 → 源已经死了，改主表
  NO_HTML            返回非 HTML（JSON/图片/跳转环）→ 源不适合采
  HTML_NO_FEED       **首页能打开但没有 RSS/Sitemap** → 需要爬列表页
  HTML_HAS_FEED      首页里其实有 <link rel=alternate> 但探测逻辑没走这条路
                     → 直接能补，这是白捡的

最后一类是本脚本的主要目标。标准做法叫「feed 自动发现」：
首页 <head> 里若写了
    <link rel="alternate" type="application/rss+xml" href="/feed">
浏览器和阅读器都靠它找订阅源，而我们的探测只试了固定路径。

用法：
  python scripts/diagnose_no_entry.py --in 无入口.json --out 归因.json
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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# RSS/Atom 的 MIME 与常见 rel 写法
FEED_LINK = re.compile(
    r"<link[^>]+>", re.I)
ATTR = re.compile(r'(\w[\w-]*)\s*=\s*["\']([^"\']*)["\']')

FEED_TYPES = ("application/rss+xml", "application/atom+xml",
              "application/rdf+xml", "application/feed+json",
              "application/json")


def parse_links(html):
    """从 HTML 里挑出 rel=alternate 且 type 是 feed 的 <link>。"""
    out = []
    for tag in FEED_LINK.findall(html[:200000]):
        attrs = dict((k.lower(), v) for k, v in ATTR.findall(tag))
        rel = (attrs.get("rel") or "").lower()
        typ = (attrs.get("type") or "").lower().split(";")[0].strip()
        href = attrs.get("href") or ""
        if "alternate" not in rel:
            continue
        if typ not in FEED_TYPES:
            continue
        if not href:
            continue
        out.append({"href": href, "type": typ,
                    "title": attrs.get("title") or ""})
    return out


def diagnose(sess, item, timeout):
    """对一家源归因。返回带 reason 的记录。"""
    dom = item["domain"]
    url = item.get("url") or ("https://" + dom)
    rec = {"domain": dom, "name": item.get("name"), "url": url}
    try:
        r = sess.get(url, timeout=timeout, verify=False, allow_redirects=True)
    except Exception as e:  # noqa: BLE001
        rec["reason"] = "DNS_FAIL" if "NameResolution" in type(e).__name__ \
            or "Name or service" in str(e) or "getaddrinfo" in str(e) \
            else "CONN_FAIL"
        rec["detail"] = type(e).__name__ + ": " + str(e)[:120]
        return rec

    rec["http"] = r.status_code
    rec["final_url"] = r.url[:200]
    if r.status_code in (401, 403, 429):
        rec["reason"] = "HTTP_%d" % r.status_code
        return rec
    if r.status_code >= 400:
        rec["reason"] = "HTTP_%d" % r.status_code
        return rec

    ctype = (r.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "xml" not in ctype:
        rec["reason"] = "NO_HTML"
        rec["detail"] = ctype[:60]
        return rec

    html = r.text or ""
    rec["html_len"] = len(html)
    links = parse_links(html)
    if links:
        rec["reason"] = "HTML_HAS_FEED"
        rec["feeds"] = links[:5]
        return rec

    # 再看看首页里有没有 sitemap 的 robots 声明
    m = re.search(r'(?i)Sitemap:\s*(\S+)', html)
    if m:
        rec["reason"] = "HTML_HAS_FEED"
        rec["feeds"] = [{"href": m.group(1), "type": "robots-sitemap"}]
        return rec

    rec["reason"] = "HTML_NO_FEED"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--proxy", default=None)
    a = ap.parse_args()

    todo = json.load(io.open(a.inp, encoding="utf-8"))
    print("")
    print("  待归因 %d 家" % len(todo))

    import requests
    import urllib3
    from concurrent.futures import ThreadPoolExecutor
    urllib3.disable_warnings()

    px = a.proxy
    if not px:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion"
                               r"\Internet Settings")
            en, _ = winreg.QueryValueEx(k, "ProxyEnable")
            srv, _ = winreg.QueryValueEx(k, "ProxyServer")
            k.Close()
            if en and srv:
                px = "http://" + str(srv).strip()
        except Exception:  # noqa: BLE001
            pass
    print("  代理：%s" % (px or "无"))

    def mk():
        s = requests.Session()
        s.trust_env = False
        if px:
            s.proxies = {"http": px, "https": px}
        s.headers.update({"User-Agent": UA})
        return s

    results = []
    lock = __import__("threading").Lock()
    t0 = time.time()

    def work(x):
        s = mk()
        try:
            return diagnose(s, x, a.timeout)
        except Exception as e:  # noqa: BLE001
            return {"domain": x["domain"], "reason": "UNKNOWN",
                    "detail": type(e).__name__ + ": " + str(e)[:100]}

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(work, todo), 1):
            results.append(r)
            if i % 30 == 0 or i == len(todo):
                with lock:
                    io.open(a.out, "w", encoding="utf-8").write(
                        json.dumps(results, ensure_ascii=False, indent=1))
                print("    %d/%d ｜ %.0f 秒" % (i, len(todo), time.time() - t0))

    from collections import Counter
    cnt = Counter(r["reason"] for r in results)
    print("")
    print("  -- 归因 --")
    for k, v in cnt.most_common():
        print("    %-18s %4d" % (k, v))
    n_feed = cnt.get("HTML_HAS_FEED", 0)
    print("")
    print("  可白捡的（首页里有 feed 声明）：%d 家" % n_feed)
    print("  → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())