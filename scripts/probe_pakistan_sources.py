"""补俾路支本地源 + 巴基斯坦多语种媒体，并探测可用入口。

背景
----
用户要求：「巴基斯坦必须单独补俾路支本地源，同时补多语种地名体系。
来源应包括：俾路支本地媒体；巴基斯坦全国性英语媒体；乌尔都语媒体；
警方、地方政府等官方通报渠道。」

为什么单独补
------------
实测（2026-11-19）中文搜索引擎搜巴基斯坦恐袭，覆盖率只有 4-8%。
根子是地名体系断层：基准写「俾路支 Gwadar 专区 Jiwani Panwan 海岸警卫队
哨所」，中文源写「巴基斯坦西南部某检查站」。

而且中文搜索引擎完全处理不了乌尔都文：
  گوادر بلوچستان → 返回「乌戈·布兰科·加尔多斯」
  جیوانی         → 返回「钢琴诗人乔瓦尼」（把 Jiwani 读成 Giovanni）

所以乌尔都语内容必须直接抓当地源，不能靠中文搜索代劳。

候选清单
--------
按用户要求分四类登记：本地媒体 / 全国英语 / 乌尔都语 / 官方通报。
每个都探 sitemap 或 RSS，记下能不能采。
"""
import argparse
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 四类来源。字段：域名、名称、类别、语种、覆盖区域
CANDIDATES = [
    # ---- 一、俾路支本地媒体 ----
    ("thebalochistanpost.com", "The Balochistan Post", "本地", "en", "俾路支"),
    ("balochistantimes.com", "Balochistan Times", "本地", "en", "俾路支"),
    ("quettavoice.com", "Quetta Voice", "本地", "en", "奎达"),
    ("thebalochistanpoint.com", "The Balochistan Point", "本地", "en", "俾路支"),
    ("balochistanexpress.com", "Balochistan Express", "本地", "en", "俾路支"),
    ("makranpost.com", "Makran Post", "本地", "en", "马克兰"),
    ("awaz-e-balochistan.com", "Awaz-e-Balochistan", "本地", "ur", "俾路支"),
    ("balochistanaffairs.com", "Balochistan Affairs", "本地", "en", "俾路支"),
    ("gwadartimes.com", "Gwadar Times", "本地", "en", "瓜达尔"),
    ("bolanvoice.com", "Bolan Voice", "本地", "en", "博兰"),
    ("turbatnews.com", "Turbat News", "本地", "en", "图尔巴特"),
    ("panjgurtimes.com", "Panjgur Times", "本地", "en", "潘杰古尔"),

    # ---- 二、巴基斯坦全国性英语媒体 ----
    ("dawn.com", "Dawn", "全国英语", "en", "全国"),
    ("tribune.com.pk", "The Express Tribune", "全国英语", "en", "全国"),
    ("thenews.com.pk", "The News International", "全国英语", "en", "全国"),
    ("nation.com.pk", "The Nation", "全国英语", "en", "全国"),
    ("dailytimes.com.pk", "Daily Times", "全国英语", "en", "全国"),
    ("pakistantoday.com.pk", "Pakistan Today", "全国英语", "en", "全国"),
    ("brecorder.com", "Business Recorder", "全国英语", "en", "全国"),
    ("app.com.pk", "Associated Press of Pakistan", "全国英语", "en", "全国"),
    ("dnd.com.pk", "Dispatch News Desk", "全国英语", "en", "全国"),
    ("centerline.com.pk", "Centreline", "全国英语", "en", "全国"),

    # ---- 三、乌尔都语媒体 ----
    ("jang.com.pk", "Jang", "乌尔都", "ur", "全国"),
    ("nawaiwaqt.com.pk", "Nawa-i-Waqt", "乌尔都", "ur", "全国"),
    ("express.pk", "Express Urdu", "乌尔都", "ur", "全国"),
    ("dunyanews.tv", "Dunya News", "乌尔都", "ur", "全国"),
    ("geo.tv", "Geo TV", "乌尔都", "ur", "全国"),
    ("samaa.tv", "Samaa TV", "乌尔都", "ur", "全国"),
    ("arynews.tv", "ARY News", "乌尔都", "ur", "全国"),
    ("bolnews.com", "BOL News", "乌尔都", "ur", "全国"),
    ("92newshd.tv", "92 News", "乌尔都", "ur", "全国"),
    ("bbc.com/urdu", "BBC Urdu", "乌尔都", "ur", "全国"),
    ("urduvoa.com", "VOA Urdu", "乌尔都", "ur", "全国"),
    ("urdu.geo.tv", "Geo Urdu", "乌尔都", "ur", "全国"),

    # ---- 四、官方通报 ----
    ("interior.gov.pk", "Ministry of Interior", "官方", "en", "全国"),
    ("balochistan.gov.pk", "Government of Balochistan", "官方", "en", "俾路支"),
    ("ispr.gov.pk", "ISPR (军方公共关系)", "官方", "en", "全国"),
    ("pid.gov.pk", "Press Information Department", "官方", "en", "全国"),
    ("balochistanpolice.gov.pk", "Balochistan Police", "官方", "en", "俾路支"),
    ("fcbalochistan.gov.pk", "Frontier Corps Balochistan", "官方", "en", "俾路支"),
    ("nadra.gov.pk", "NADRA", "官方", "en", "全国"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    import probe_feeds as P
    from concurrent.futures import ThreadPoolExecutor

    print("")
    print("  探测 %d 个巴基斯坦来源（四类）" % len(CANDIDATES))

    def work(item):
        dom, name, cat, lang, area = item
        try:
            r = P.probe_one(dom, a.timeout)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "url": "", "kind": "", "items": 0,
                 "latest": "", "via": "", "err": type(e).__name__}
        r.update({"domain": dom, "name": name, "category": cat,
                  "lang": lang, "area": area})
        return r

    out = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(work, CANDIDATES):
            out.append(r)
            mark = "✓" if r.get("kind") else "✗"
            print("  %s %-12s %-30s %-8s %s"
                  % (mark, r["category"], r["domain"][:28],
                     r.get("kind") or "—", (r.get("url") or "")[:44]))

    json.dump(out, io.open(a.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 按类别统计
    print("")
    by = {}
    for r in out:
        c = r["category"]
        by.setdefault(c, [0, 0])
        by[c][1] += 1
        if r.get("kind"):
            by[c][0] += 1
    for c, (ok, tot) in by.items():
        print("  %-10s %2d / %2d 可用" % (c, ok, tot))
    print("")
    print("  合计 %d / %d 可用" % (sum(v[0] for v in by.values()), len(out)))
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
