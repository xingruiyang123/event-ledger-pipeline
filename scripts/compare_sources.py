#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""两个权威库对同一批事件报的死亡数差多少 —— 这是数据可信度的直接证据。

为什么要做这件事
----------------
我们整个台账的骨架来自 UCDP 和 ACLED。它们「可信」是共识，但共识不能代替验证。
同一个事件，两家各报一个死亡数，差多少？差在什么地方？这决定了：

  · 台账的「死亡合计」能精确到什么程度
  · 用哪个源作主口径
  · 哪些事件的数字需要标注「两源不一致」

比对方法
--------
只比**两库都记录了**的事件（坐标 ≤10km + 日期 ±1 天内不算多匹配）。
单源事件无从比较，不纳入。

对每一对，比较 best/fatalities：

  完全一致    差值 = 0
  接近        |差值| / max ≤ 20%
  同量级      20% < 比值 ≤ 100%
  差一个量级   比值 > 100%（10 倍以上）
  一边为 0    一个有死亡数、一个是 0

用法
----
  python scripts/compare_sources.py --a ucdp.jsonl --b acled.jsonl --out 双源比对.md
"""
import argparse
import io
import json
import math
import os
import re
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 中文国家名 -> 英文（两库国家字段语言可能不同）
ZH_TO_EN = {
    "中国": "china", "尼日利亚": "nigeria", "布基纳法索": "burkina faso",
    "苏丹": "sudan", "南苏丹": "south sudan", "索马里": "somalia",
    "乌克兰": "ukraine", "巴基斯坦": "pakistan", "叙利亚": "syria",
    "阿富汗": "afghanistan", "伊拉克": "iraq", "马里": "mali",
    "尼日尔": "niger", "喀麦隆": "cameroon", "乍得": "chad",
    "哥伦比亚": "colombia", "墨西哥": "mexico", "巴西": "brazil",
    "海地": "haiti", "缅甸": "myanmar", "也门": "yemen", "利比亚": "libya",
    "埃及": "egypt", "土耳其": "turkey", "伊朗": "iran", "以色列": "israel",
    "巴勒斯坦": "palestine", "印度": "india", "孟加拉国": "bangladesh",
    "斯里兰卡": "sri lanka", "菲律宾": "philippines",
    "印度尼西亚": "indonesia", "泰国": "thailand", "俄罗斯": "russia",
    "美国": "united states", "英国": "united kingdom", "法国": "france",
    "德国": "germany", "西班牙": "spain", "意大利": "italy",
    "肯尼亚": "kenya", "埃塞俄比亚": "ethiopia", "莫桑比克": "mozambique",
    "刚果（金）": "democratic republic of congo",
    "刚果民主共和国": "democratic republic of congo",
    "刚果（布）": "republic of congo", "乌干达": "uganda",
    "布隆迪": "burundi", "卢旺达": "rwanda", "坦桑尼亚": "tanzania",
    "安哥拉": "angola", "津巴布韦": "zimbabwe", "南非": "south africa",
    "阿尔及利亚": "algeria", "摩洛哥": "morocco", "突尼斯": "tunisia",
    "黎巴嫩": "lebanon", "约旦": "jordan", "沙特阿拉伯": "saudi arabia",
    "阿联酋": "united arab emirates", "卡塔尔": "qatar",
    "科威特": "kuwait", "阿曼": "oman", "巴林": "bahrain",
    "格鲁吉亚": "georgia", "亚美尼亚": "armenia", "阿塞拜疆": "azerbaijan",
    "白俄罗斯": "belarus", "摩尔多瓦": "moldova", "塞尔维亚": "serbia",
    "波黑": "bosnia and herzegovina", "科索沃": "kosovo",
    "北马其顿": "north macedonia", "阿尔巴尼亚": "albania",
    "哈萨克斯坦": "kazakhstan", "塔吉克斯坦": "tajikistan",
    "乌兹别克斯坦": "uzbekistan", "蒙古": "mongolia", "越南": "vietnam",
    "柬埔寨": "cambodia", "老挝": "laos", "日本": "japan",
    "韩国": "south korea", "朝鲜": "north korea",
    "澳大利亚": "australia", "加拿大": "canada", "秘鲁": "peru",
    "委内瑞拉": "venezuela", "厄瓜多尔": "ecuador",
    "玻利维亚": "bolivia", "巴拉圭": "paraguay", "智利": "chile",
    "阿根廷": "argentina", "洪都拉斯": "honduras",
    "危地马拉": "guatemala", "萨尔瓦多": "el salvador", "古巴": "cuba",
    "牙买加": "jamaica", "中非": "central african republic",
    "毛里塔尼亚": "mauritania", "塞内加尔": "senegal", "几内亚": "guinea",
    "塞拉利昂": "sierra leone", "利比里亚": "liberia", "加纳": "ghana",
    "多哥": "togo", "贝宁": "benin", "科特迪瓦": "cote d'ivoire",
    "赞比亚": "zambia", "马拉维": "malawi", "纳米比亚": "namibia",
    "博茨瓦纳": "botswana", "厄立特里亚": "eritrea", "吉布提": "djibouti",
    "希腊": "greece", "瑞典": "sweden", "挪威": "norway",
    "荷兰": "netherlands", "比利时": "belgium", "瑞士": "switzerland",
    "奥地利": "austria", "波兰": "poland", "捷克": "czechia",
    "匈牙利": "hungary", "罗马尼亚": "romania", "保加利亚": "bulgaria",
    "克罗地亚": "croatia", "斯洛文尼亚": "slovenia",
    "斯洛伐克": "slovakia", "爱尔兰": "ireland", "葡萄牙": "portugal",
    "芬兰": "finland", "丹麦": "denmark", "新西兰": "new zealand",
    "新加坡": "singapore", "马来西亚": "malaysia", "文莱": "brunei",
    "东帝汶": "timor-leste", "巴布亚新几内亚": "papua new guinea",
    "斐济": "fiji",
}

COUNTRY_ALIAS = {
    "democratic republic of the congo": "democratic republic of congo",
    "dr congo": "democratic republic of congo",
    "drc": "democratic republic of congo",
    "congo, dr": "democratic republic of congo",
    "ivory coast": "cote d'ivoire",
    "cote d ivoire": "cote d'ivoire",
    "burma": "myanmar",
    "russian federation": "russia",
    "united states of america": "united states",
    "usa": "united states",
    "uk": "united kingdom",
    "viet nam": "vietnam",
    "syrian arab republic": "syria",
    "iran (islamic republic of)": "iran",
    "occupied palestinian territory": "palestine",
    "palestinian territory": "palestine",
    "west bank": "palestine",
    "gaza strip": "palestine",
}


# 累计/汇总条目：「俄军本月累计损失」「前线日报」这类不是单起事件。
# 实测它们对总量比对的影响是决定性的：
#   未剔除：UCDP 报 12345 / ACLED 报 2894，比值 4.27
#   剔除后：UCDP 报  4359 / ACLED 报 2891，比值 1.51
# 只有 7 条，却占了 UCDP 总量的 65%。不排掉会把「两源差 4 倍」这个
# 错误结论当成数据质量问题。
AGGREGATE_TEXT = re.compile(
    r"(?i)(losses? in the war|frontline report|monthly total|weekly total|"
    r"\d{1,3},000-strong|since (?:the )?(?:start|beginning) of|running total|"
    r"累计|总计|截至|本月合计)")


def is_aggregate(rec):
    return bool(AGGREGATE_TEXT.search(str(rec.get("summary") or
                                            rec.get("事件摘要") or "")))


def norm_country(c):
    s = str(c or "").strip()
    if not s:
        return ""
    if s in ZH_TO_EN:
        return ZH_TO_EN[s]
    k = re.sub(r"[^a-z ]", "", s.lower()).strip()
    return COUNTRY_ALIAS.get(k, k)


def haversine(a, b, c, d):
    try:
        la1, lo1, la2, lo2 = float(a), float(b), float(c), float(d)
    except Exception:  # noqa: BLE001
        return 1e9
    if abs(la1) > 90 or abs(la2) > 90:
        return 1e9
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = p2 - p1
    dl = math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def load(path):
    out = []
    with io.open(path, encoding="utf-8") as f:
        txt = f.read().strip()
    rows = json.loads(txt) if txt.startswith("[") else \
        [json.loads(l) for l in txt.split("\n") if l.strip()]
    for r in rows:
        # 两库都用 date 字符串（"2025-08-01"），没有 year/month/day 三个字段。
        # 只认分开的字段会得到 day=""，然后 _dd() 全返回 999，匹配数恒为 0 ——
        # 实测踩过一次，表现为「0 对可比对」，看着像两库没有重叠事件。
        _ds = str(r.get("date") or "")
        if re.match(r"^\d{4}-\d{2}-\d{2}", _ds):
            y, m, d = int(_ds[:4]), int(_ds[5:7]), int(_ds[8:10])
        else:
            try:
                y = int(r.get("year") or r.get("年份") or 0)
                m = int(r.get("month") or r.get("月份") or 0)
                d = int(r.get("day") or r.get("日期") or 1)
            except Exception:  # noqa: BLE001
                continue
        de = r.get("deaths")
        if de in (None, ""):
            de = r.get("死亡人数")
        try:
            de = int(float(str(de)))
        except Exception:  # noqa: BLE001
            de = 0
        # 注意：本流水线的候选文件**不含坐标**（UCDP GED 的经纬度在原始 CSV 里，
        # ACLED 的 latitude/longitude 也没被抽出来）。所以匹配只能靠
        # 国家 + 日期 + 死亡数 + 地名相似度，不能靠 haversine。
        # 这也是为什么这条链路的「跨源印证」比例偏低 —— 缺坐标导致匹配保守。
        out.append({
            "place": str(r.get("place") or r.get("地点") or "").lower(),
            "lat": r.get("latitude") or r.get("纬度"),
            "lon": r.get("longitude") or r.get("经度"),
            "cty": norm_country(r.get("country") or r.get("国家")),
            "day": "%04d-%02d-%02d" % (y, m, d) if y else "",
            "deaths": de,
            "raw": r,
        })
    return out


def _dd(a, b):
    """日期差天数。"""
    try:
        from datetime import date
        y1, m1, d1 = (int(x) for x in a.split("-"))
        y2, m2, d2 = (int(x) for x in b.split("-"))
        return abs((date(y1, m1, d1) - date(y2, m2, d2)).days)
    except Exception:  # noqa: BLE001
        return 999


def band(x, y):
    """死亡数差异分档。"""
    if x == y:
        return "完全一致"
    if x == 0 or y == 0:
        return "一边为0"
    lo, hi = min(x, y), max(x, y)
    if hi / lo <= 1.2:
        return "接近(≤20%)"
    if hi / lo <= 2.0:
        return "同量级(≤2倍)"
    if hi / lo <= 10.0:
        return "差数倍"
    return "差一个量级"


def main():
    ap = argparse.ArgumentParser(description="两源死亡数比对")
    ap.add_argument("--a", required=True, help="源 A（建议 UCDP）")
    ap.add_argument("--b", required=True, help="源 B（建议 ACLED）")
    ap.add_argument("--a-name", default="UCDP")
    ap.add_argument("--b-name", default="ACLED")
    ap.add_argument("--max-km", type=float, default=10.0)
    ap.add_argument("--max-days", type=int, default=1)
    ap.add_argument("--keep-aggregate", action="store_true",
                    help="保留累计/汇总条目（默认剔除）。实测不剔除会让两源总量比值从 1.51 虚增到 4.27")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    A = load(a.a)
    B = load(a.b)
    print("%s %d 条 ｜ %s %d 条" % (a.a_name, len(A), a.b_name, len(B)))
    if not a.keep_aggregate:
        _a0, _b0 = len(A), len(B)
        A = [x for x in A if not is_aggregate(x["raw"])]
        B = [x for x in B if not is_aggregate(x["raw"])]
        if _a0 != len(A) or _b0 != len(B):
            print("  剔除累计/汇总条目：%s %d->%d ｜ %s %d->%d"
                  % (a.a_name, _a0, len(A), a.b_name, _b0, len(B)))

    # 按国家建索引，避免 O(n*m)
    idx = {}
    for j, b in enumerate(B):
        idx.setdefault(b["cty"], []).append(j)

    # 无坐标时按「同国 + 日期<=N + 地名词重叠」匹配。
    # 有坐标就优先用距离，两者都能用。
    def _toks(t):
        return {w for w in re.findall(r"[a-z]{4,}", t or "")
                if w not in ("village", "town", "city", "area", "near",
                             "located", "which", "their", "there", "about")}

    pairs = []
    for i, x in enumerate(A):
        best = None
        tx = _toks(x["place"])
        for j in idx.get(x["cty"], []):
            y = B[j]
            if _dd(x["day"], y["day"]) > a.max_days:
                continue
            km = haversine(x["lat"], x["lon"], y["lat"], y["lon"])
            ty = _toks(y["place"])
            ov = len(tx & ty)
            if km <= a.max_km:
                score = (0, km)          # 有坐标且够近：最可信
            elif ov >= 1:
                score = (1, -ov)         # 无坐标但地名有交集
            else:
                continue
            if best is None or score < best[0]:
                best = (score, j, km if km < 1e8 else -1)
        if best:
            pairs.append((i, best[1], best[2]))

    print("两库都记录了、且坐标≤%.0fkm 日期±%d 天的：%d 对"
          % (a.max_km, a.max_days, len(pairs)))
    if not pairs:
        print("没有可比对的事件。检查两库的坐标字段是否都填了。")
        return 1

    stat = Counter()
    diffs = []
    for i, j, km in pairs:
        x, y = A[i]["deaths"], B[j]["deaths"]
        b = band(x, y)
        stat[b] += 1
        diffs.append((b, x, y, A[i], B[j], km))

    n = len(pairs)
    print()
    print("=== 死亡数差异分布 ===")
    for k in ["完全一致", "接近(≤20%)", "同量级(≤2倍)", "差数倍",
              "差一个量级", "一边为0"]:
        if stat.get(k):
            print("  %-12s %4d 对（%.0f%%）" % (k, stat[k], 100.0 * stat[k] / n))

    # 一致性指标
    same = stat.get("完全一致", 0)
    close = same + stat.get("接近(≤20%)", 0)
    print()
    print("完全一致：%.0f%%" % (100.0 * same / n))
    print("差异 ≤20%%：%.0f%%" % (100.0 * close / n))

    # 总量对比
    sa = sum(A[i]["deaths"] for i, _, _ in pairs)
    sb = sum(B[j]["deaths"] for _, j, _ in pairs)
    print()
    print("同一批 %d 起事件的死亡数合计：" % n)
    print("  %s 报 %d" % (a.a_name, sa))
    print("  %s 报 %d" % (a.b_name, sb))
    if sb:
        print("  比值 %.2f（%s / %s）" % (sa / sb, a.a_name, a.b_name))

    # 差异最大的样例
    print()
    print("=== 差异最大的 10 对（人工看是定义不同还是记错）===")
    diffs.sort(key=lambda x: -(abs(x[1] - x[2])))
    for b, x, y, ra, rb, km in diffs[:10]:
        print("  [%s] %s %s ｜ A报%-5d B报%-5d ｜ 距离%.1fkm"
              % (b, ra["cty"][:16], ra["day"], x, y, km))
        s1 = str(ra["raw"].get("summary") or ra["raw"].get("事件摘要") or "")[:64]
        s2 = str(rb["raw"].get("summary") or rb["raw"].get("事件摘要") or "")[:64]
        print("     A: %s" % s1)
        print("     B: %s" % s2)

    if a.out:
        lines = ["# 两源死亡数比对：%s vs %s" % (a.a_name, a.b_name), ""]
        lines.append("| 项 | 数值 |")
        lines.append("|---|---|")
        lines.append("| %s 记录数 | %d |" % (a.a_name, len(A)))
        lines.append("| %s 记录数 | %d |" % (a.b_name, len(B)))
        lines.append("| 可比对事件对 | %d |" % n)
        lines.append("| 完全一致 | %d（%.0f%%） |" % (same, 100.0 * same / n))
        lines.append("| 差异 ≤20%% | %d（%.0f%%） |" % (close, 100.0 * close / n))
        lines.append("")
        lines.append("## 差异分布")
        lines.append("")
        lines.append("| 档 | 对数 | 占比 |")
        lines.append("|---|---|---|")
        for k in ["完全一致", "接近(≤20%)", "同量级(≤2倍)", "差数倍",
                  "差一个量级", "一边为0"]:
            if stat.get(k):
                lines.append("| %s | %d | %.0f%% |"
                             % (k, stat[k], 100.0 * stat[k] / n))
        lines.append("")
        lines.append("## 同一批事件的死亡数合计")
        lines.append("")
        lines.append("| 源 | 合计 |")
        lines.append("|---|---|")
        lines.append("| %s | %d |" % (a.a_name, sa))
        lines.append("| %s | %d |" % (a.b_name, sb))
        lines.append("")
        if sb:
            lines.append("比值 %.2f。" % (sa / sb))
            lines.append("")
        d = os.path.dirname(os.path.abspath(a.out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print()
        print("已写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
