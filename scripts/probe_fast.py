"""快速筛入口：只试主路径，优先把「有 RSS 的源」捞出来。

为什么换掉原来的 17 路径全试
--------------------------
实测：775 家 × 17 条路径 = 13,175 次请求，16 线程跑了 32 分钟才到 200 家，
按这个速度要 2 小时。而结果只有 15% 的源能探到。

症结是顺序不对：绝大多数源在第 1-4 条路径就出结果了（命中或明确不是），
剩下 13 条只对少数"路径很怪"的站有用，却让每个源都多花 13 次请求。

所以改成两段：
  第一段  4 条主路径（/rss /feed /sitemap.xml /news-sitemap.xml）
          90% 的源在这里就能定性，快速过一遍
  第二段  只有第一段全失败的才试扩展路径，且并发调高

第二段的道理：路径怪的站终归是少数，让少数站多花时间是合理的；
让全部 775 家都陪跑 17 条路径才是不合理的。

增量保存
--------
每探完 40 家就落盘一次。原版只在脚本末尾 dump，
进程一被杀就全丢（实测丢了 200 家的结果，白跑 32 分钟）。
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

FAST = ["/rss", "/feed", "/sitemap.xml", "/news-sitemap.xml"]
SLOW = ["/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/rss/",
        "/feeds/", "/feeds/all.atom.xml", "/rss/all.rss", "/news/rss",
        "/rss/all", "/.rss", "/sitemap_index.xml", "/sitemap-index.xml"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
ITEM = re.compile(rb"<item\b|<entry\b|<url>", re.I)
SITEMAP = re.compile(rb"<urlset|<sitemapindex", re.I)


def get(session, url, timeout):
    try:
        r = session.get(url, timeout=timeout, verify=False)
        return r.status_code, r.content
    except Exception:  # noqa: BLE001
        return 0, b""


def looks_feed(body):
    """判断响应体是不是 feed/sitemap。

    必须在**整个响应体**里找标记，不能只看前 3000 字节。
    实测教训：首页 HTML 的 <head> 里常有 <link rel="alternate"
    type="application/rss+xml">；如果站点还用了 <urlset> 这类
    微数据或 JSON-LD，SITEMAP 正则就会在 HTML 里命中，
    把整个首页判成 sitemap。

    这次就是这么错的：404 家全量重探报出「可用 12 家」，
    逐个打开看 **12 家全是首页 HTML**（bolnews / yahoo /
    alsumaria / un.org / heraldscotland …），items 却报 0 条。
    假阳性率 100%。

    正确做法：HTML 一律先排除，再看有没有真实条目。
    """
    head = body[:4000].lower()
    if b"<!doctype html" in head or b"<html" in head[:400]:
        return ""
    if SITEMAP.search(body[:20000]):
        return "sitemap"
    if (b"<rss" in head or b"<feed" in head or b"<rdf" in head
            or b"<?xml" in head):
        # 有 xml 声明但一条条目都没有的，也不是能用的入口
        return "rss" if len(ITEM.findall(body)) >= 2 else ""
    n = len(ITEM.findall(body[:20000]))
    return "rss" if n >= 2 else ""


def probe(session, domain, paths, timeout):
    base = "https://" + domain
    for p in paths:
        u = base + p
        code, body = get(session, u, timeout)
        if code == 200 and len(body) > 400:
            kind = looks_feed(body)
            if kind:
                n = len(ITEM.findall(body))
                return {"kind": kind, "url": u, "items": n}
    return {"kind": "", "url": "", "items": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--timeout", type=int, default=6)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--skip-slow", action="store_true",
                    help="只跑第一段，不试扩展路径")
    a = ap.parse_args()

    todo = json.load(io.open(a.inp, encoding="utf-8"))
    done = {}
    if os.path.isfile(a.out):
        try:
            for r in json.load(io.open(a.out, encoding="utf-8")):
                done[r["domain"]] = r
        except Exception:  # noqa: BLE001
            done = {}
    todo = [x for x in todo if x["domain"] not in done]
    print("")
    print("  待探 %d 家（已完成 %d 家，跳过）" % (len(todo), len(done)))

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
    print("  第一段 %d 条主路径，%d 线程" % (len(FAST), a.workers))

    def mk():
        s = requests.Session()
        s.trust_env = False
        if px:
            s.proxies = {"http": px, "https": px}
        s.headers.update({"User-Agent": UA})
        return s

    results = list(done.values())
    n_ok = sum(1 for r in results if r.get("kind"))
    t0 = time.time()
    lock = __import__("threading").Lock()

    def work(x):
        s = mk()
        r = probe(s, x["domain"], FAST, a.timeout)
        if not r["kind"] and not a.skip_slow:
            r = probe(s, x["domain"], SLOW, a.timeout)
        # r.update(x) 会把 x 里的 url 覆盖掉探到的入口地址 ——
        # x 的 url 是站点根，探到的是 /sitemap.xml 之类。
        # 结果 12 家「可用」全显示成 https://域名，回头验证时
        # 打开的是首页，看起来像 100% 假阳性。改成按字段合并。
        for k, v in x.items():
            if k not in ("url", "kind", "items"):
                r[k] = v
        return r

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(work, todo), 1):
            results.append(r)
            if r.get("kind"):
                n_ok += 1
            if i % 40 == 0 or i == len(todo):
                io.open(a.out, "w", encoding="utf-8").write(
                    json.dumps(results, ensure_ascii=False, indent=1))
                print("    %d/%d  可用 %d（%.0f%%）｜ %.0f 秒"
                      % (i, len(todo), n_ok, 100.0 * n_ok / len(results),
                         time.time() - t0), flush=True)

    io.open(a.out, "w", encoding="utf-8").write(
        json.dumps(results, ensure_ascii=False, indent=1))
    print("")
    print("  完成：%d 家，可用 %d（%.0f%%），耗时 %.0f 秒"
          % (len(results), n_ok, 100.0 * n_ok / max(1, len(results)),
             time.time() - t0))
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
