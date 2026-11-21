"""重新探测「入口不是 feed」的源 —— 验证是真假阳性还是入口真失效。

监控里这一栏是这么来的：源抓取成功（HTTP 200）但 parse_feed 解析出 0 条。
两种可能完全不同的原因：

  假阳性    入口 URL 能返回内容，但不是 feed / sitemap 结构（比如返回 HTML 首页）
  真失效    入口曾经是 feed，现在改版或下线了

这两件事的修法相反：假阳性要重新找入口，真失效要换域名或放弃。
不分开就会一直挂在「入口不是 feed」这一栏里，谁也不动。

做法：直接取一次，看 content-type 和前 200 字节的实际内容，
再按真实结构判断它到底是什么。
"""
import argparse
import io
import json
import re
import sys

sys.path.insert(0, r"D:\shared-skills\event-ledger-pipeline\scripts")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def main():
    import scheduler as S

    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    S.detect_proxy(None)
    rows = S.load_registry(a.registry, None, None)
    state = {}
    import glob
    import os
    for f in glob.glob(r"D:\event-ledger-data\state\*.json"):
        try:
            j = json.load(io.open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        state.setdefault(j.get("domain"), j)

    targets = []
    for r in rows:
        u = (r.get("feed_url") or "").strip()
        if not u:
            continue
        j = state.get(r["domain"])
        if not j:
            continue
        if "解析无条目" in str(j.get("last_error") or ""):
            targets.append((r["domain"], u, r.get("name")))

    print("")
    print("  待复探 %d 家" % len(targets))
    print("")

    out = []
    for dom, url, name in targets:
        res = {"domain": dom, "url": url, "name": name}
        code, raw, enc, final = S.http_get(url, timeout=20, retries=1)
        if code != 200:
            res["verdict"] = "入口不可达"
            res["detail"] = "HTTP %s %s" % (code, str(final)[:40])
        else:
            head = raw[:400].decode("utf-8", "replace").lower()
            n_url = len(re.findall(rb"<url>", raw))
            n_sitemap = len(re.findall(rb"<sitemap>", raw))
            n_item = len(re.findall(rb"<item[\s>]", raw))
            n_entry = len(re.findall(rb"<entry[\s>]", raw))
            has_xml = head.lstrip().startswith("<?xml") or "<rss" in head \
                or "<feed" in head or "<urlset" in head \
                or "<sitemapindex" in head
            res["counts"] = {"url": n_url, "sitemap": n_sitemap,
                             "item": n_item, "entry": n_entry}
            res["bytes"] = len(raw)
            if n_url or n_sitemap or n_item or n_entry:
                res["verdict"] = "其实能解析"      # 可能是代理/瞬时故障
            elif "<html" in head or "<!doctype html" in head:
                res["verdict"] = "返回 HTML 页面"
            elif has_xml:
                res["verdict"] = "XML 但无条目"
            else:
                res["verdict"] = "非 feed 内容"
            res["head"] = raw[:120].decode("utf-8", "replace").replace("\n", " ")
        out.append(res)
        print("  %-26s %-16s %s" % (dom[:24], res["verdict"],
                                    str(res.get("detail") or res.get("counts"))[:70]))

    print("")
    from collections import Counter
    for k, n in Counter(x["verdict"] for x in out).most_common():
        print("    %-14s %d 家" % (k, n))

    if a.out:
        io.open(a.out, "w", encoding="utf-8").write(
            json.dumps(out, ensure_ascii=False, indent=1))
        print("")
        print("  明细 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())