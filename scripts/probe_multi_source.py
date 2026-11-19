"""改正多源验证：解析 JSON，区分独立来源与转载。

两个错误
--------
一、原脚本用正则抓 URL，把结果里的任何链接都算"额外来源"。
   实测元宝每次返回 10 条，全带同一条 CDN 图标链接
   （cdn-yb.icon.qq.com）和百科、门户聚合页，于是得到 100% 的假阳性。
   加黑名单后又变成 0% —— 因为正则在 JSON 上抓不到干净的域名。

二、就算域名不同，也不等于独立来源。
   实测搜「阿富汗情报官被杀」，返回的 10 条是同一篇环球网报道
   被今日头条、网易、搜狐分别转载，标题一模一样。
   这在 GTD 的标准里只能算 1 个来源。

元宝的返回结构（实测）
----------------------
  {"results":[{"passage":..,"title":..,"url":..,"site":..,
               "date":..,"content":"完整正文..."}]}
  → content 直接给正文，不用再抓网页。

判据
----
同一条搜索结果算"独立来源"必须：
  1. 域名与原始来源不同
  2. 标题与已有来源不重复（转载的标题往往一字不差）
  3. 域名不在噪音黑名单里
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

NOISE = re.compile(
    r"icon\.|cdn-|static\.|/favicon|"
    r"baike\.|wikipedia|wikiwand|kiddle|academic\.|books\.google|"
    r"facebook|twitter|x\.com|youtube|instagram|tiktok|t\.me|"
    r"google\.|bing\.|baidu\.com|so\.com|sogou|yandex", re.I)


def clean_env():
    e = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        e.pop(k, None)
    e["NO_PROXY"] = "*"
    e["no_proxy"] = "*"
    return e


def search_json(cmd_tpl, q, timeout=45):
    """跑检索，解析 JSON。失败回退纯文本。"""
    cmd = cmd_tpl.replace("{q}", q.replace('"', " "))
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=clean_env(), timeout=timeout)
        out = r.stdout or ""
    except Exception:  # noqa: BLE001
        return []
    i = out.find("{")
    if i < 0:
        return []
    try:
        d = json.loads(out[i:])
        return d.get("results") or []
    except Exception:  # noqa: BLE001
        return []


def dom_of(u):
    try:
        return str(u).split("/")[2].lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def norm_title(t):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", str(t or ""))[:40]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judged", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--search-cmd", required=True)
    ap.add_argument("--n", type=int, default=15)
    a = ap.parse_args()

    rows = json.load(io.open(a.judged, encoding="utf-8"))
    todo = [r for r in rows if str(r.get("信息来源URL") or "").startswith("http")]
    todo = todo[:a.n]

    print("")
    print("  验证 %d 条事件能否找到额外独立来源" % len(todo))

    res = []
    for i, r in enumerate(todo, 1):
        summ = str(r.get("事件摘要") or "").strip()
        ctry = str(r.get("国家") or "")
        own_dom = dom_of(r.get("信息来源URL"))
        own_title = norm_title(summ)

        words = re.findall(r"[A-Za-z\u4e00-\u9fff]{3,}", summ)[:5]
        q = " ".join(words[:4]) if words else "%s July 2026 attack" % ctry
        hits = search_json(a.search_cmd, q)

        indep, dup, noise = [], 0, 0
        seen_titles = {own_title} if own_title else set()
        for h in hits:
            d = dom_of(h.get("url"))
            if not d or NOISE.search(d):
                noise += 1
                continue
            if d == own_dom:
                dup += 1
                continue
            t = norm_title(h.get("title"))
            if t and t in seen_titles:
                dup += 1            # 同一标题 = 转载，不算独立来源
                continue
            if t:
                seen_titles.add(t)
            indep.append({"domain": d, "title": str(h.get("title"))[:70],
                          "site": h.get("site"), "url": h.get("url")})

        res.append({
            "summary": summ, "country": ctry, "own_domain": own_dom,
            "query": q, "n_results": len(hits),
            "n_independent": len(indep),
            "n_repost": dup, "n_noise": noise,
            "independent": indep[:6],
        })
        print("  [%d/%d] 独立 %d ｜ 转载 %d ｜ 噪音 %d ｜ %s"
              % (i, len(todo), len(indep), dup, noise, summ[:38]))

    json.dump(res, io.open(a.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    g1 = sum(1 for x in res if x["n_independent"] >= 1)
    g2 = sum(1 for x in res if x["n_independent"] >= 2)
    print("")
    print("  >=1 个独立来源: %d / %d = %.0f%%"
          % (g1, len(res), 100.0 * g1 / max(1, len(res))))
    print("  >=2 个独立来源: %d / %d = %.0f%%"
          % (g2, len(res), 100.0 * g2 / max(1, len(res))))
    print("  （GTD 标准是 >=3 个独立来源）")
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
