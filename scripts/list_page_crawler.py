"""把「首页能打开但没有 RSS/Sitemap」的站点变成可采集入口。

为什么需要这一层
----------------
归因 395 家无入口信源，224 家的结论是 HTML_NO_FEED：
域名活着、首页能 200、返回正常 HTML，但 17 条标准路径下
既没有 RSS 也没有 Sitemap。

这不是「源坏了」，是「这个站只有网页，没做订阅接口」。
绝大多数地方媒体、政府网站都属于这一类。它们的内容照样能采，
只是要按网页来采：从列表页抓文章链接。

为什么不能直接爬整站
--------------------
irishtimes.com 首页 10 MB，edition.cnn.com 6.9 MB。
整站爬既慢又容易被封，而且我们每天只需要「新出的那几篇」。
所以这一层只做**发现**：从首页取候选文章链接，交给正文层。

怎么判断一个链接是文章
----------------------
不做逐站规则 —— 200 多家站点写不起，也没法维护。
用可迁移的统计特征（都在真实站点上验过）：

  是文章的迹象    路径有日期段 /2026/11/20/
                  标题段里有 ≥3 个连字符（长 slug）
                  路径深度 ≥ 2
                  查询串里有 id= 或 p=
  不是文章的迹象  路径在白名单黑名单里（about/contact/privacy/tag/
                  author/category/search/login/subscribe...）
                  有文件扩展名（.pdf/.jpg/.css/.js）
                  路径段极短（/world、/news、/cn）
                  是外域

CNN 的 /2026/11/20/investing/us-stocks-anthropic-ai 会被选中；
/section/business 和 /about-us 会被排除。

局限（必须说清楚）
------------------
· 首页只反映「此刻编辑在推什么」，不等于当天全部新稿。
  这是发现层的固有缺口，靠多抓几个栏目页缓解，不能消除。
· 判断是启发式的，会有漏（标题短的稿件）和误（专题页像文章）。
  所以这一层产出的只是**候选链接**，正文层还要再过一遍。
· 不做 JS 渲染。列表靠前端渲染的站（部分 SPA）会取不到链接。
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

# 这些路径段说明是导航页不是文章
BAD_SEG = {
    "about", "about-us", "contact", "contact-us", "privacy", "privacy-policy",
    "terms", "terms-of-service", "cookie", "cookies", "legal", "disclaimer",
    "advertise", "advertising", "subscribe", "subscription", "newsletter",
    "login", "signin", "sign-in", "register", "signup", "account", "profile",
    "search", "sitemap", "rss", "feed", "tag", "tags", "topic", "topics",
    "author", "authors", "category", "categories", "section", "sections",
    "archive", "archives", "page", "pages", "index", "home", "help", "faq",
    "careers", "jobs", "shop", "store", "cart", "donate", "membership",
    "video", "videos", "photo", "photos", "gallery", "podcast", "podcasts",
    "live", "weather", "sport", "sports", "opinion", "editorial", "blog",
    "wp-content", "wp-admin", "wp-json", "wp-login", "static", "assets",
    "images", "img", "css", "js", "fonts", "api", "o", "e", "r",
}

# 新闻站的栏目页：抓这些页面能拿到更多当天条目。
# 只用全世界新闻站都叫得出的栏目名，不猜站点特有写法。
SECTION_GUESSES = [
    "/world", "/news", "/national", "/politics", "/security",
    "/world/asia", "/world/africa", "/world/middle-east", "/latest",
]

BAD_EXT = re.compile(
    r"\.(?:pdf|jpe?g|png|gif|webp|svg|ico|css|js|json|xml|zip|rar|"
    r"mp[34]|avi|mov|wav|docx?|xlsx?|pptx?|csv|txt)(?:\?|$)", re.I)

DATE_SEG = re.compile(r"/(?:19|20)\d{2}(?:/\d{1,2}){0,2}/")
LONG_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){2,}", re.I)
CJK_SLUG = re.compile(r"[\u4e00-\u9fff]{6,}")
NUM_ID = re.compile(r"[?&](?:id|p|aid|articleid|storyid|newsid)=\d+", re.I)
# 无连字符的长 ID 型 slug：BBC 的 /news/articles/c1234567890o、
# 印度站的 /india-news-101700000000001。要求「长度≥12 且含数字」，
# 这样 /world/asia-pacific 这种栏目页不会被误判成文章。
ID_SLUG = re.compile(r"^(?=[a-z0-9]*\d)[a-z0-9]{12,}$", re.I)

A_HREF = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']""", re.I)


def score_url(u, base_host, require_dated=False):
    """给一个候选 URL 打分。返回 0 表示不像文章。

    require_dated=True 时，没有日期段的一律 0 分。
    用于那些确定用日期型 URL 的站（CNN / 很多报纸）。
    """
    try:
        p = urllib.parse.urlparse(u)
    except Exception:  # noqa: BLE001
        return 0
    if p.scheme not in ("http", "https"):
        return 0
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host.endswith(base_host.lower().lstrip("www.")):
        return 0
    path = p.path or "/"
    if BAD_EXT.search(path) or BAD_EXT.search(p.query or ""):
        return 0
    segs = [s for s in path.split("/") if s]
    if not segs:
        return 0
    for s in segs:
        if s.lower() in BAD_SEG:
            return 0
    if require_dated and not DATE_SEG.search(path):
        return 0
    s = 0
    if DATE_SEG.search(path):
        s += 4
    if LONG_SLUG.search(segs[-1] if segs else ""):
        s += 3
    if segs and ID_SLUG.match(segs[-1]):
        s += 3
    if len(segs) >= 2:
        s += 1
    if len(segs) >= 3:
        s += 1
    if NUM_ID.search("?" + (p.query or "")):
        s += 2
    if CJK_SLUG.search(path):
        s += 2
    last = (segs[-1] if segs else "")
    if len(last) < 10 and not DATE_SEG.search(path):
        s -= 3
    return max(0, s)


def extract_links(html, page_url, base_host, min_score=4):
    """从一页 HTML 里取候选文章链接。

    站点级自适应：先看这一页有多少链接带日期段。
    带日期段的超过 20 条 → 这站用日期型 URL，那就**只认日期型**，
    别的一律排除。否则像 CNN 这种站，`/politics/president-donald-trump-47`
    这种人物标签页的 slug 也很长，通用判据分不清。

    实测就是这么错的：CNN 首页 71 条候选里混进了
    /politics/president-donald-trump-47、/travel/food-and-drink、
    /interactive/call-to-earth —— 全是导航页。
    """
    raw_links = []
    seen = set()
    for raw in A_HREF.findall(html or ""):
        h = (raw or "").strip()
        if not h or h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = urllib.parse.urljoin(page_url, h)
        p = urllib.parse.urlparse(u)
        u = urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
        if u in seen:
            continue
        seen.add(u)
        raw_links.append(u)

    # 站点级判据：这站用不用日期型 URL
    n_dated = sum(1 for u in raw_links if DATE_SEG.search(u))
    date_style = n_dated >= 20

    out = []
    for u in raw_links:
        if date_style:
            if not DATE_SEG.search(u):
                continue
            # 日期型站：只要有日期段 + 同域 + 非黑名单，就是文章
            if score_url(u, base_host, require_dated=True) >= 4:
                out.append(u)
        else:
            if score_url(u, base_host) >= min_score:
                out.append(u)
    return out


def crawl_site(sess, item, timeout, max_pages=3, min_score=4):
    """一个站：先首页，再栏目页，合并候选。"""
    dom = item["domain"]
    base = "https://" + dom
    rec = {"domain": dom, "name": item.get("name"), "base": base,
           "candidates": [], "pages_tried": 0, "reason": ""}

    pages = [base]
    # 栏目页只在首页拿不到足够候选时才试，省请求
    all_links = []
    first_status = 0
    sec_status = []
    for i, pg in enumerate(pages):
        try:
            r = sess.get(pg, timeout=timeout, verify=False,
                         allow_redirects=True)
            rec["pages_tried"] += 1
        except Exception as e:  # noqa: BLE001
            # 首页连不上才算 CONN_FAIL；栏目页连不上不算
            if not first_status and i == 0:
                rec["reason"] = "CONN_FAIL:" + type(e).__name__
            continue
        if i == 0:
            first_status = r.status_code
        else:
            sec_status.append(r.status_code)
        if r.status_code >= 400:
            continue
        html = r.text or ""
        if len(html) < 2000:
            continue
        links = extract_links(html, r.url, dom, min_score)
        all_links.extend(links)
        # 候选不够就试栏目页
        if len(set(all_links)) < 20 and len(pages) < max_pages:
            for sec in SECTION_GUESSES:
                if len(pages) >= max_pages:
                    break
                pages.append(urllib.parse.urljoin(r.url, sec))
        if len(set(all_links)) >= 60:
            break

    # 同一域名下按路径去重（允许同一篇文章的不同 URL 变体）
    uniq = []
    seen = set()
    for u in all_links:
        key = urllib.parse.urlparse(u).path.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(u)
    rec["candidates"] = uniq[:300]
    rec["http"] = first_status
    rec["sec_http"] = sec_status[:5]
    if not rec["reason"]:
        # 归因只按**首页**状态来写。
        # 之前每一页都写一次 reason，结果首页 200、栏目页 404 的站
        # 被标成 HTTP_404 —— 实测 136 家「404」里 sabanew.net、
        # english.news.cn 手工一开都是 200，全是这么错标的。
        if first_status == 0:
            rec["reason"] = "CONN_FAIL"
        elif first_status >= 400:
            rec["reason"] = "HTTP_%d" % first_status
        elif uniq:
            rec["reason"] = "OK"
        else:
            rec["reason"] = "NO_LINKS"
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--proxy", default=None)
    a = ap.parse_args()

    todo = json.load(io.open(a.inp, encoding="utf-8"))
    if a.limit:
        todo = todo[:a.limit]
    print("")
    print("  待爬列表页 %d 家" % len(todo))

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
    print("  代理：%s ｜ min_score=%d" % (px or "无", a.min_score))

    def mk():
        s = requests.Session()
        s.trust_env = False
        if px:
            s.proxies = {"http": px, "https": px}
        s.headers.update({"User-Agent": UA,
                          "Accept-Language": "en,zh;q=0.9,ar;q=0.8"})
        return s

    results = []
    t0 = time.time()

    def work(x):
        s = mk()
        try:
            return crawl_site(s, x, a.timeout, min_score=a.min_score)
        except Exception as e:  # noqa: BLE001
            return {"domain": x["domain"], "candidates": [],
                    "reason": "ERROR:" + type(e).__name__}

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, r in enumerate(ex.map(work, todo), 1):
            results.append(r)
            if i % 20 == 0 or i == len(todo):
                io.open(a.out, "w", encoding="utf-8").write(
                    json.dumps(results, ensure_ascii=False, indent=1))
                n_ok = sum(1 for x in results if x["candidates"])
                print("    %d/%d ｜ 有候选 %d 家 ｜ %.0f 秒"
                      % (i, len(todo), n_ok, time.time() - t0))

    from collections import Counter
    cnt = Counter(r["reason"].split(":")[0] for r in results)
    n_ok = sum(1 for r in results if r["candidates"])
    tot = sum(len(r["candidates"]) for r in results)
    print("")
    print("  -- 结果 --")
    for k, v in cnt.most_common():
        print("    %-14s %4d" % (k, v))
    print("")
    print("  有候选链接 %d/%d 家 ｜ 候选合计 %d 条 ｜ 均 %.1f 条/家"
          % (n_ok, len(results), tot, (tot / n_ok) if n_ok else 0))
    print("  → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())