"""探测信源站的机器入口：sitemap / RSS 是否可用。

为什么要这个
------------
信源表 1178 个条目里，1151 个的「入口类型」「回溯能力」还是「待探测」，
只有 12 个探明了入口 URL。清单变成可采集的池子，第一步就是把这 1151 个探出来。

对每个域名试四个常见入口，记录状态码、内容类型、条目数、最新条目日期。
有了「最新条目日期」才能判断这个站值不值得每天采 —— 三年没更新的站不必占额度。

用法
----
  python probe_feeds.py --registry 信源总表.xlsx --out 入口探测.json
  python probe_feeds.py --domains a.com,b.com --out t.json     # 只测几个
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 常见入口路径，按命中概率排序（多数站点至少有一个）
PATHS = [
    "/sitemap.xml",
    "/rss",
    "/feed",
    "/sitemap_index.xml",
]


USE_PROXY = True    # 采集境外媒体必须走代理。实测直连 1/6 成功，走代理 4/6。


def _clean_env():
    """curl 兜底用的环境。

    注意：这里**不清代理**。调用本地检索工具（yb.cmd 等）时才需要清，
    而抓境外站点的 sitemap/RSS 恰恰需要代理 —— 实测直连 6 个站点只成功 1 个，
    走代理成功 4 个。
    """
    return dict(os.environ)


def fetch(url, timeout=6):
    """取内容。先 requests，失败退 curl。"""
    try:
        import requests
        s = requests.Session()
        s.trust_env = USE_PROXY
        r = s.get(url, timeout=timeout, verify=False,
                  headers={"User-Agent": UA})
        return r.status_code, r.headers.get("content-type", ""), r.text
    except Exception:  # noqa: BLE001
        pass
    if __import__("shutil").which("curl"):
        try:
            r = subprocess.run(
                ["curl", "-sL", "-m", str(timeout), "-A", UA, url],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=_clean_env())
            if r.stdout:
                head = r.stdout[:400].lower()
                ct = ("xml" if ("<urlset" in head or "<rss" in head
                                or "<feed" in head) else "html")
                return 200, ct, r.stdout
        except Exception:  # noqa: BLE001
            pass
    return 0, "", ""


def count_items(txt, ct):
    """粗算条目数：sitemap 数 <url>，RSS 数 <item>，Atom 数 <entry>。"""
    t = txt or ""
    # 必须取最大值，不能写 a or b or c —— Python 的 or 返回第一个真值，
    # 不是最大的那个。实测 aljazeera 的 RSS 里 "<url>" 恰好出现 1 次
    # （在命名空间声明里），"<item>" 有 25 个，写 or 会得 1，
    # 于是这份完全正常的 feed 被判成"条目太少"丢掉。
    # 用正则数，不能字符串计数 —— 很多 RSS 的条目是 <item rdf:about="...">，
    # 字符串 "<item>" 数不到。dawn.com 的 feed 有 27 个条目，
    # 字符串计数得 0，整份 feed 被判成空的。
    return max(len(re.findall(r"<url\b", t, re.I)),
               len(re.findall(r"<item\b", t, re.I)),
               len(re.findall(r"<entry\b", t, re.I)))


def latest_date(txt):
    """从条目里抽最新日期（YYYY-MM-DD），用于判断站点是否还活着。"""
    ds = re.findall(r"(20\d{2})-(\d{2})-(\d{2})", txt or "")
    if not ds:
        return ""
    return "%s-%s-%s" % max(ds)


LINK_RSS = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
LINK_HREF = re.compile(r'href=["\']([^"\']+)', re.I)
SITEMAP_HINT = re.compile(r'["\']([^"\']*sitemap[^"\']*\.xml)["\']', re.I)


def discover_from_home(domain, timeout=6):
    """从首页 HTML 里找 RSS / sitemap 线索。

    实测：aljazeera 首页声明 /xml/rss/all.xml，dawn 声明 /feeds/home，
    这两个路径都不在通用路径表里。读首页一次就能拿到，比暴力试准得多。
    """
    base = domain if domain.startswith("http") else "https://" + domain
    base = base.rstrip("/")
    cands = []
    for home in (base + "/", base + "/en/", base + "/index.html"):
        code, ct, txt = fetch(home, timeout)
        if code != 200 or not txt or len(txt) < 500:
            continue
        for tag in LINK_RSS.findall(txt):
            m = LINK_HREF.search(tag)
            if m:
                h = m.group(1).strip()
                if h.startswith("/"):
                    cands.append(base + h)
                elif h.startswith("http"):
                    cands.append(h)
        for m in SITEMAP_HINT.finditer(txt):
            h = m.group(1).strip()
            if h.startswith("/"):
                cands.append(base + h)
            elif h.startswith("http"):
                cands.append(h)
        if cands:
            break
    # 去重保序
    out, seen = [], set()
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:6]


def _validate(url, timeout=6):
    """验证一个候选 URL 是不是可用的 feed。"""
    code, ct, txt = fetch(url, timeout)
    if code != 200 or not txt:
        return None
    head = txt[:600].lower()
    if not any(k in head for k in
               ("<urlset", "<sitemapindex", "<rss", "<feed", "<rdf")):
        return None
    n = count_items(txt, ct)
    if n < 5:
        return None
    kind = ("sitemap" if ("sitemap" in url or "<urlset" in head
                          or "<sitemapindex" in head)
            else ("rss" if ("<rss" in head or "<rdf" in head) else "atom"))
    return {"ok": True, "url": url, "kind": kind, "items": n,
            "latest": latest_date(txt), "bytes": len(txt)}


def probe_one(domain, timeout=6):
    """对一个域名试所有入口，返回第一个能用的。"""
    base = domain if domain.startswith("http") else "https://" + domain
    base = base.rstrip("/")

    # 第一步：首页声明的地址（最准）
    for url in discover_from_home(base, timeout):
        r = _validate(url, timeout)
        if r:
            r["via"] = "home-link"
            return r

    # 第二步：通用路径保底
    for path in PATHS:
        r = _validate(base + path, timeout)
        if r:
            r["via"] = "common-path"
            return r

    return {"ok": False, "url": "", "kind": "", "items": 0, "latest": "",
            "bytes": 0, "via": ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", default=None, help="信源总表 xlsx")
    ap.add_argument("--domains", default=None, help="逗号分隔，只测这些")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="只测前 N 个")
    ap.add_argument("--timeout", type=int, default=6)
    ap.add_argument("--no-proxy", action="store_true",
                    help="不走代理直连。仅当目标站国内可直连时用")
    ap.add_argument("--workers", type=int, default=16,
                    help="并发线程数（默认 16）。1178 个域名约 15 分钟")
    ap.add_argument("--resume", action="store_true",
                    help="跳过 --out 里已测过的域名")
    a = ap.parse_args()
    global USE_PROXY
    USE_PROXY = not a.no_proxy

    jobs = []      # (id, name, domain, tier)
    if a.domains:
        for d in a.domains.split(","):
            d = d.strip()
            if d:
                jobs.append(("", "", d, ""))
    else:
        import openpyxl
        wb = openpyxl.load_workbook(a.registry, data_only=True)
        ws = wb["信源总表"] if "信源总表" in wb.sheetnames else wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        hdr = [str(h or "").strip() for h in rows[0]]
        ix = {h: i for i, h in enumerate(hdr)}
        for r in rows[1:]:
            if not r or not r[0]:
                continue
            dom = str(r[ix.get("主域名", 2)] or "").strip()
            if not dom:
                continue
            jobs.append((str(r[0]), str(r[ix.get("信源名", 1)] or ""),
                         dom, str(r[ix.get("圈层", 7)] or "")))

    done = {}
    if a.resume and os.path.isfile(a.out):
        try:
            done = json.load(io.open(a.out, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            done = {}

    if a.limit:
        jobs = jobs[:a.limit]

    print("")
    print("  探测 %d 个域名的 sitemap / RSS 入口（%d 线程）"
          % (len(jobs), a.workers))
    out = dict(done)
    todo = []
    for sid, name, dom, tier in jobs:
        if dom in out and out[dom].get("kind"):
            continue
        todo.append((sid, name, dom, tier))

    ok = 0
    done_n = 0
    def _one(item):
        sid, name, dom, tier = item
        r = probe_one(dom, a.timeout)
        r.update({"id": sid, "name": name, "tier": tier, "domain": dom})
        return r

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(_one, it): it for it in todo}
        for fu in as_completed(futs):
            done_n += 1
            try:
                r = fu.result()
            except Exception:  # noqa: BLE001
                continue
            out[r["domain"]] = r
            if r["ok"]:
                ok += 1
                print("  [%d/%d] %-24s %-8s %5d 条 %-10s %s"
                      % (done_n, len(todo), r["domain"][:24], r["kind"],
                         r["items"], r.get("via", ""), r["latest"] or "?"))
            if done_n % 20 == 0:
                json.dump(out, io.open(a.out, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)

    json.dump(out, io.open(a.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    tot = sum(1 for v in out.values() if v.get("kind"))
    print("")
    print("  有可用入口 %d / %d = %.0f%%（本次新探 %d）"
          % (tot, len(out), 100.0 * tot / max(1, len(out)), ok))
    by = {}
    for v in out.values():
        if v.get("kind"):
            by[v["kind"]] = by.get(v["kind"], 0) + 1
    for k, n in sorted(by.items(), key=lambda x: -x[1]):
        print("    %-9s %d" % (k, n))
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
