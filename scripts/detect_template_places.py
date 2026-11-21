"""识别「站点模板地名」—— 同一个站所有文章都判成同一个地点时，那个地名来自页脚。

问题（2026-11-21 全量实测）
--------------------------
14,824 篇正文跑抽取，国家分布明显失真：

  美国      2,554 篇   其中 civilsociety.co.uk 306 篇，地点全是「托皮卡，Kansas」
  科特迪瓦  1,611 篇   其中 koaci.com        1,575 篇
  突尼斯    1,136 篇   其中 semafor.com       731 篇，地点全是「马赫迪耶，Mahdia」
  西班牙      907 篇   其中 armyrecognition   857 篇，地点全是「格拉纳达，Andalusia」

抽查三篇内容完全不相关的文章（以色列加沙、乌克兰袭击、拜登竞选广告），
地点都是「马赫迪耶」—— 突尼斯一个省。这只能是页面模板里的地名，
不是文章讲的地方。

判据为什么必须是「两条件同时满足」
----------------------------------
只看集中度会把**地方媒体**全判成模板：
  reviewjournal.com  拉斯维加斯 17/24 = 71%   —— 它就是拉斯维加斯的报纸，对的
  seattletimes.com   西雅图     15/21 = 71%   —— 对的
  fox6now.com        密尔沃基   17/17 = 100%  —— 对的
这些不能动，否则误伤正常报道。

真正的模板长这样：
  civilsociety.co.uk（英国慈善行业媒体）→「托皮卡，Kansas」306 篇全中
  semafor.com（美国媒体）→「马赫迪耶，突尼斯」731 篇全中
  armyrecognition.com（法国军事媒体）→「格拉纳达，西班牙」857 篇全中
共同点是：**判定出的地点不属于该站所在国家**。

所以两个条件同时满足才算模板：
  ① 该地点的国家 ≠ 站点所属国家（主表的「国家/区域」）
  ② 站内集中度 ≥ 55%

只满足①不满足②的是正常跨国报道（英国媒体报道美国事件）。
只满足②不满足①的是地方媒体（报了本地新闻）。

用法：
  # 第一步：统计（读台账 CSV + 主表拿站点国家）
  python scripts/detect_template_places.py --ledger 台账.csv --registry 主表.xlsx --out 模板地名.json

  # 第二步：抽取时带上黑名单
  python scripts/build_ledger_rows.py --template-places 模板地名.json

注意：黑名单是**按站点**生效的，不是全局。同一个地名在不同站待遇不同。
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import Counter, defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 判为模板的比例门槛
TEMPLATE_RATIO = 0.55
# 该站至少要有这么多篇才做统计，样本太小没有统计意义
MIN_SAMPLES = 15


def host_of(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) if m else "").lower().lstrip("www.")


def same_country(a, b):
    """两个国家/区域写法是不是同一个地方。

    主表里的「国家/区域」是人工填的，写法不统一：
      「英国/苏格兰」「阿富汗；巴基」「中国香港」「巴基斯坦、印度」
    直接字符串相等会把它们全判成「跨国」，于是
      thestandard.com.hk 报「香港」→ 误判成模板
      scotsman.com 报「爱丁堡」→ 误判成模板
      jpost.com 报「耶路撒冷」→ 误判成模板
    这四类都是对的，不能动。

    改法：双向子串包含即算同一国；再补一张常见别名表。
    """
    if not a or not b:
        return False
    a = a.strip()
    b = b.strip()
    if a == b:
        return True
    if a in b or b in a:
        return True
    for grp in ALIAS_GROUPS:
        hit_a = any(x in a for x in grp)
        hit_b = any(x in b for x in grp)
        if hit_a and hit_b:
            return True
    return False


ALIAS_GROUPS = [
    ("英国", "苏格兰", "英格兰", "威尔士", "北爱尔兰", "UK", "Britain"),
    ("中国", "香港", "澳门", "台湾", "China"),
    ("美国", "USA", "United States"),
    ("俄罗斯", "Russia"),
    ("韩国", "南韩", "Korea"),
    ("朝鲜", "北韩"),
    ("巴基斯坦", "Pakistan"),
    ("阿富汗", "Afghanistan"),
    ("印度", "India"),
    ("伊朗", "Iran"),
    ("以色列", "Israel"),
    ("巴勒斯坦", "Palestine", "加沙", "Gaza"),
    ("叙利亚", "Syria"),
    ("土耳其", "Turkey", "Türkiye"),
    ("乌克兰", "Ukraine"),
    ("比利时", "Belgium"),
    ("德国", "Germany"),
    ("法国", "France"),
    ("西班牙", "Spain"),
    ("突尼斯", "Tunisia"),
    ("索马里", "Somalia"),
    ("马里", "Mali"),
    ("尼日利亚", "Nigeria"),
    ("日本", "Japan"),
    ("捷克", "Czech"),
    ("亚美尼亚", "Armenia"),
    ("阿尔及利亚", "Algeria"),
    ("澳大利亚", "Australia"),
    ("加拿大", "Canada"),
    ("埃及", "Egypt"),
    ("沙特", "Saudi"),
    ("也门", "Yemen"),
    ("伊拉克", "Iraq"),
]


def load_registry_country(path):
    """主表：主域名 → 国家/区域。"""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    try:
        import openpyxl
    except Exception:  # noqa: BLE001
        return out
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb["信源主表"] if "信源主表" in wb.sheetnames else wb.active
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        hdr = [str(h or "").strip() for h in rows[0]]
        ix = {h: i for i, h in enumerate(hdr)}
        dcol = ix.get("主域名")
        ccol = ix.get("国家/区域")
        if dcol is None or ccol is None:
            return out
        for r in rows[1:]:
            d = str(r[dcol] or "").strip().lower()
            c = str(r[ccol] or "").strip()
            if d:
                out[d] = c
    except Exception:  # noqa: BLE001
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--registry", default="",
                    help="信源主表，用来拿站点所属国家；不给则跳过归属校验")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ratio", type=float, default=TEMPLATE_RATIO)
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    a = ap.parse_args()

    # site -> Counter(地点), site -> Counter(国家)
    per_site = defaultdict(Counter)
    per_site_cty = defaultdict(Counter)
    n = 0
    with io.open(a.ledger, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            n += 1
            site = host_of(row.get("信息来源URL") or "")
            place = (row.get("地点") or "").strip()
            cty = (row.get("国家") or "").strip()
            if site and place:
                per_site[site][place] += 1
                per_site_cty[site][cty] += 1

    reg = load_registry_country(a.registry)

    print("")
    print("  读过台账 %d 行 ｜ %d 个站点 ｜ 主表国家 %d 条"
          % (n, len(per_site), len(reg)))
    print("")

    templates = {}
    for site, cnt in per_site.items():
        total = sum(cnt.values())
        if total < a.min_samples:
            continue
        place, k = cnt.most_common(1)[0]
        ratio = k / float(total)
        if ratio < a.ratio:
            continue
        # ① 归属校验：这个地点属于该站所在国家吗
        site_cty = reg.get(site, "")
        if reg and not site_cty:
            # 主表里没这个站（可能是新补的），保守起见不判模板
            continue
        place_cty = per_site_cty[site].most_common(1)[0][0] if per_site_cty[site] else ""
        belongs = same_country(site_cty, place_cty)
        if belongs:
            # 地方媒体报本地，正常
            continue
        templates[site] = {
            "place": place,
            "hits": k,
            "total": total,
            "ratio": round(ratio, 3),
            "site_country": site_cty,
            "place_country": place_cty,
        }

    print("  -- 判为模板地名（占比 ≥ %.0f%%，样本 ≥ %d，且地点不属于该站所在国）--"
          % (a.ratio * 100, a.min_samples))
    for site, t in sorted(templates.items(), key=lambda kv: -kv[1]["hits"]):
        print("    %-28s %-20s %4d/%4d %3.0f%%  %s站→%s"
              % (site[:26], t["place"][:18], t["hits"], t["total"],
                 t["ratio"] * 100, t["site_country"][:6] or "?",
                 t["place_country"][:6] or "?"))
    print("")
    print("  共 %d 个站点的模板地名" % len(templates))

    import json
    io.open(a.out, "w", encoding="utf-8").write(
        json.dumps({"ratio": a.ratio, "min_samples": a.min_samples,
                    "templates": templates}, ensure_ascii=False, indent=1))
    print("  → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())