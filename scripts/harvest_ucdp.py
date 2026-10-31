#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 UCDP 抽取某月事件，输出统一的候选格式。

为什么单独做一个脚本（而不是塞进多源采集）
------------------------------------------
UCDP 是月度台账的**主力源**，它的字段语义决定了判定链怎么走
（`type_of_violence` 直接对应「国家间 / 针对平民 / 非国家冲突」）。
把这个映射做对，比多接几个弱源有用得多。

两个数据集必须分清（这是实测踩过的坑）
-------------------------------------
  · 正式 GED（ged261-csv.zip，39MB，覆盖 1989 至去年）→ **查历史月份用它**
  · 候选集（GEDEvent_v26_0_9.csv，1.6MB）→ **只含还在编码队列的月份**

实测：2026 年候选集共 2014 条，9 月 1984 条、8 月仅 27 条。
用候选集查 8 月会得出「8 月只有 27 起」的错误结论 —— 数据早就移进正式 GED 了。

用法
----
  # 从正式 GED 抽某月（推荐）
  python scripts/harvest_ucdp.py --ged ged261.zip --month 2026-08 --out 候选.jsonl

  # 从候选集抽最新月份
  python scripts/harvest_ucdp.py --candidate GEDEvent_v26_0_9.csv --month 2026-09 --out 候选.jsonl

  # 只看统计
  python scripts/harvest_ucdp.py --ged ged261.zip --month 2026-08 --json

  # 只保留「针对平民的单方面暴力」（恐袭主体来源）
  python scripts/harvest_ucdp.py --ged ged261.zip --month 2026-08 --types 2 --out 候选.jsonl

退出码：0 = 有产出且达到下限 | 2 = 产出过少（该月可能未收录）| 1 = 无法判定
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

COUNTRY_ZH = {
    "Nigeria": "尼日利亚", "Somalia": "索马里", "Sudan": "苏丹",
    "South Sudan": "南苏丹", "Mali": "马里", "Burkina Faso": "布基纳法索",
    "Niger": "尼日尔", "Chad": "乍得", "Cameroon": "喀麦隆",
    "Democratic Republic of the Congo": "刚果（金）", "DR Congo (Zaire)": "刚果（金）",
    "Congo": "刚果（布）", "Mozambique": "莫桑比克", "Ethiopia": "埃塞俄比亚",
    "Kenya": "肯尼亚", "Uganda": "乌干达", "Burundi": "布隆迪",
    "Rwanda": "卢旺达", "Tanzania": "坦桑尼亚", "Angola": "安哥拉",
    "Zimbabwe": "津巴布韦", "South Africa": "南非", "Libya": "利比亚",
    "Algeria": "阿尔及利亚", "Egypt": "埃及", "Morocco": "摩洛哥",
    "Tunisia": "突尼斯", "Pakistan": "巴基斯坦", "Afghanistan": "阿富汗",
    "India": "印度", "Bangladesh": "孟加拉国", "Sri Lanka": "斯里兰卡",
    "Nepal": "尼泊尔", "Myanmar (Burma)": "缅甸", "Myanmar": "缅甸",
    "Thailand": "泰国", "Philippines": "菲律宾", "Indonesia": "印度尼西亚",
    "Malaysia": "马来西亚", "Syria": "叙利亚", "Iraq": "伊拉克",
    "Yemen (North Yemen)": "也门", "Yemen": "也门", "Lebanon": "黎巴嫩",
    "Israel": "以色列", "Palestine": "巴勒斯坦", "Jordan": "约旦",
    "Turkey": "土耳其", "Iran": "伊朗", "Saudi Arabia": "沙特阿拉伯",
    "Colombia": "哥伦比亚", "Venezuela": "委内瑞拉", "Mexico": "墨西哥",
    "Haiti": "海地", "Ecuador": "厄瓜多尔", "Peru": "秘鲁", "Brazil": "巴西",
    "Honduras": "洪都拉斯", "Guatemala": "危地马拉", "El Salvador": "萨尔瓦多",
    "Russia": "俄罗斯", "Ukraine": "乌克兰", "United States of America": "美国",
    "United States": "美国", "United Kingdom": "英国", "France": "法国",
    "Germany": "德国", "Spain": "西班牙", "Italy": "意大利",
    "Central African Republic": "中非", "Benin": "贝宁", "Togo": "多哥",
    "Ghana": "加纳", "Senegal": "塞内加尔", "Guinea": "几内亚",
    "Sierra Leone": "塞拉利昂", "Liberia": "利比里亚", "Ivory Coast": "科特迪瓦",
    "Côte d'Ivoire": "科特迪瓦", "Mauritania": "毛里塔尼亚",
    "Tanzania": "坦桑尼亚", "Zambia": "赞比亚", "Malawi": "马拉维",
}

TYPE_MEANING = {
    "1": "state_based",       # 国家间武装冲突（含政府 vs 反政府武装）
    "2": "one_sided",         # 单方面暴力（有组织团体针对平民）
    "3": "non_state",         # 非国家冲突（武装团体之间）
}

# type_of_violence → 判定链起点提示
TYPE_HINT = {
    "1": "M0倾向（涉国家正规军），需查是否有针对平民的单方面行为",
    "2": "直接进 P3/P4/P7 —— 恐袭的主体来源",
    "3": "需判反证 N4（双方是否已处于交战状态）",
}


def zh(name):
    n = (name or "").strip()
    if n in COUNTRY_ZH:
        return COUNTRY_ZH[n]
    for k, v in COUNTRY_ZH.items():
        if k.lower() == n.lower():
            return v
    return n


def open_rows(path):
    """支持直接给 csv、或给 zip（自动找里面的 csv）。"""
    if path.lower().endswith(".zip"):
        z = zipfile.ZipFile(path)
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise SystemExit("zip 里没有 csv：%s" % z.namelist())
        f = z.open(names[0])
        return csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"))
    return csv.DictReader(io.open(path, encoding="utf-8", errors="replace"))


def num(v):
    try:
        return int(float(v))
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--ged", help="正式 GED 的 csv 或 zip（覆盖历史月份）")
    src.add_argument("--candidate", help="月度候选集 csv（只含最新在编月份）")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out", default=None)
    ap.add_argument("--types", default="1,2,3", help="只保留这些 type_of_violence，默认全部")
    ap.add_argument("--min-items", type=int, default=20,
                    help="低于此条数视为该月未收录（默认 20）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    path = a.ged or a.candidate
    if not os.path.isfile(path):
        print("文件不存在：%s" % path)
        return 1

    keep_types = {t.strip() for t in a.types.split(",") if t.strip()}

    try:
        rows = open_rows(path)
    except SystemExit as e:
        print(e)
        return 1
    except Exception as e:  # noqa: BLE001
        print("无法读取：%s" % e)
        return 1

    out = []
    total = 0
    for r in rows:
        total += 1
        ds = (r.get("date_start") or "")[:10]
        if not ds.startswith(a.month):
            continue
        t = (r.get("type_of_violence") or "").strip()
        if t and t not in keep_types:
            continue

        best = num(r.get("best"))
        civ = num(r.get("deaths_civilians"))
        da = num(r.get("deaths_a"))
        db = num(r.get("deaths_b"))
        ns = num(r.get("number_of_sources"))

        out.append({
            "source": "ucdp-ged" if a.ged else "ucdp-candidate",
            "date": ds,
            "country": zh(r.get("country")),
            "place": (r.get("where_description") or "").strip(),
            "summary": (r.get("source_headline") or r.get("conflict_name") or "").strip()[:300],
            "deaths": best,
            "injured": None,
            "actor_a": (r.get("side_a") or "").strip(),
            "actor_b": (r.get("side_b") or "").strip(),
            "act_type": TYPE_MEANING.get(t, "unknown"),
            "url": (r.get("source_article") or "").strip(),
            "raw_id": r.get("id"),
            "extra": {
                "type_of_violence": t,
                "type_hint": TYPE_HINT.get(t, ""),
                "deaths_civilians": civ,
                "deaths_a": da,
                "deaths_b": db,
                "deaths_unknown": num(r.get("deaths_unknown")),
                "dyad": (r.get("dyad_name") or "").strip(),
                "conflict": (r.get("conflict_name") or "").strip(),
                "n_sources": ns,
                "adm1": (r.get("adm_1") or "").strip(),
                "lat": r.get("latitude"), "lon": r.get("longitude"),
                "date_prec": r.get("date_prec"),
            },
        })

    if a.out:
        with io.open(a.out, "w", encoding="utf-8") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    by_t = Counter(r["extra"]["type_of_violence"] for r in out)
    by_c = Counter(r["country"] for r in out)

    ok = len(out) >= a.min_items
    exit_code = 0 if ok else 2

    if a.json:
        print(json.dumps({
            "file": path, "month": a.month, "scanned": total, "matched": len(out),
            "by_type": dict(by_t), "by_country": by_c.most_common(20),
            "min_items": a.min_items, "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 70)
    print(" UCDP 抽取 · %s" % a.month)
    print("=" * 70)
    print("文件      : %s" % path)
    print("扫描行数  : %d" % total)
    print("该月命中  : %d 条（下限 %d）" % (len(out), a.min_items))
    print()
    if by_t:
        print("-- 按 type_of_violence（判定链起点）------------------------------")
        for k, v in sorted(by_t.items()):
            print("  type=%s %-14s %4d 条   %s"
                  % (k, TYPE_MEANING.get(k, "?"), v, TYPE_HINT.get(k, "")))
        print()
    if by_c:
        print("-- 国家分布（前 20）----------------------------------------------")
        for k, v in by_c.most_common(20):
            print("  %-18s %3d" % (k, v))
        print()

    if not ok:
        print("  ✗ 只有 %d 条，低于下限 %d。" % (len(out), a.min_items))
        if a.candidate:
            print("    你可能用候选集查了历史月份。候选集只保留还在编码队列的月份，")
            print("    历史月份请用正式 GED：--ged ged261-csv.zip")
        else:
            print("    该月可能确实未收录（GED 滞后约一年）。可换候选集查最新月份。")

    print("→", a.out or "(未指定 --out)")
    print("=" * 70)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
