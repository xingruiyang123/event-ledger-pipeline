#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""交付前「合理性」门禁：判结果量级是否可信。

为什么必须有这一道
------------------
真实事故：一次月度台账只做出 **17 条**，而该国官方口径同月就有 **500 起安全事件**。
流程全部「通过」—— 表头对、数字自洽、三份报告齐全 —— 但结果是严重低估。
所有既有门禁都在查「内部一致性」，**没有一个在查「这个量级合不合理」**。

内部自洽 100% 通过、外部量级差两个数量级，是这类工作最危险的失败方式：
它不会报错，只会安静地交出一份没法用的东西。

本脚本做三件事
--------------
1. **下限检查**：事件数低于 --min-events（默认 40）直接 fail。
2. **国别凹陷检查**：对每个「高发国家」，若台账条数为 0 或远低于参照，
   报「疑似漏检」。参照值来自 --baseline 文件或内置的保守经验值。
3. **量级比对**：若给了独立总量参照（如官方月度通报、ACLED、机构指数），
   计算覆盖率；覆盖率过低（默认 < 15%）直接 fail。

退出码：0 = 量级合理 | 2 = 量级可疑（不许交付） | 1 = 无法判定

用法：
  python scripts/gate_plausibility.py 台账.xlsx
  python scripts/gate_plausibility.py 台账.xlsx --min-events 40
  python scripts/gate_plausibility.py 台账.xlsx --baseline 对照.json
  python scripts/gate_plausibility.py 台账.xlsx --reference-total 500 --reference-label "尼日利亚官方月度安全事件"
  python scripts/gate_plausibility.py 台账.xlsx --json

对照文件 baseline 格式（可选，用于逐国比对）：
  {"Nigeria": 30, "Somalia": 20, "Sudan": 25, "Mali": 10}
  值是「该月该国按本库口径大致应有多少条」的保守估计。
"""
import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

try:
    import openpyxl
except Exception as e:  # noqa: BLE001
    print("无法导入 openpyxl：%s\n处置： python -m pip install openpyxl" % e)
    sys.exit(1)

# 高发国家 → 单月「至少应该有这么多条」的保守经验下限。
# 这些数字不是真理，是「低于它几乎一定有漏检」的警戒线。
# 依据：这些国家每月都有成规模的武装袭击报道，只有个位数几乎总是检索不足。
HIGH_RISK = {
    "尼日利亚": 15, "Nigeria": 15,
    "索马里": 10, "Somalia": 10,
    "苏丹": 10, "Sudan": 10,
    "马里": 6, "Mali": 6,
    "布基纳法索": 5, "Burkina Faso": 5,
    "尼日尔": 5, "Niger": 5,
    "刚果（金）": 5, "刚果金": 5, "DR Congo": 5,
    "莫桑比克": 3, "Mozambique": 3,
    "喀麦隆": 3, "Cameroon": 3,
    "巴基斯坦": 8, "Pakistan": 8,
    "阿富汗": 6, "Afghanistan": 6,
    "叙利亚": 4, "Syria": 4,
    "伊拉克": 4, "Iraq": 4,
    "也门": 4, "Yemen": 4,
    "海地": 2, "Haiti": 2,
    "哥伦比亚": 3, "Colombia": 3,
    "缅甸": 3, "Myanmar": 3,
    "泰国": 2, "Thailand": 2,
    "菲律宾": 2, "Philippines": 2,
}


# 同国的中英文别名归一（只用于比对，不改台账内容）
ALIAS = {
    "Nigeria": "尼日利亚", "Somalia": "索马里", "Sudan": "苏丹", "South Sudan": "南苏丹",
    "Mali": "马里", "Burkina Faso": "布基纳法索", "Niger": "尼日尔",
    "DR Congo": "刚果（金）", "Congo": "刚果（金）", "刚果金": "刚果（金）", "Mozambique": "莫桑比克",
    "Cameroon": "喀麦隆", "Pakistan": "巴基斯坦", "Afghanistan": "阿富汗",
    "Syria": "叙利亚", "Iraq": "伊拉克", "Yemen": "也门", "Haiti": "海地",
    "Colombia": "哥伦比亚", "Myanmar": "缅甸", "Thailand": "泰国",
    "Philippines": "菲律宾", "Chad": "乍得", "Ethiopia": "埃塞俄比亚",
    "Kenya": "肯尼亚", "Libya": "利比亚", "Algeria": "阿尔及利亚",
    "Egypt": "埃及", "India": "印度", "Turkey": "土耳其", "Russia": "俄罗斯",
    "Ukraine": "乌克兰", "Mexico": "墨西哥", "Venezuela": "委内瑞拉",
    "Indonesia": "印度尼西亚", "Lebanon": "黎巴嫩", "Israel": "以色列",
    "Iran": "伊朗",
}


def canon(cty):
    return ALIAS.get(cty.strip(), cty.strip())


def read_ledger(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    header = [("" if c.value is None else str(c.value).strip()) for c in ws[1]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n) + 1
        return None

    c_cty = col("国家", "国家/地区")
    c_d = col("死亡人数", "死亡")
    c_sum = col("事件摘要", "摘要")
    if not c_cty:
        return None, None, "台账里找不到「国家」列，表头是：%s" % " | ".join(header)

    rows = []
    for r in range(2, (ws.max_row or 1) + 1):
        if all(ws.cell(r, c).value is None for c in range(1, (ws.max_column or 1) + 1)):
            continue
        first = str(ws.cell(r, 1).value or "")
        if re.match(r"^(合计|总计|小计|total)", first, re.I):
            continue
        cty = str(ws.cell(r, c_cty).value or "").strip()
        if not cty:
            continue
        def num(c):
            v = ws.cell(r, c).value if c else None
            if v is None:
                return 0
            try:
                return int(float(str(v).replace(",", "")))
            except Exception:  # noqa: BLE001
                m = re.search(r"-?\d+", str(v))
                return int(m.group()) if m else 0
        rows.append({"row": r, "country": cty, "deaths": num(c_d),
                     "summary": str(ws.cell(r, c_sum).value or "") if c_sum else ""})
    return rows, header, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--min-events", type=int, default=40,
                    help="事件数下限（默认 40）。月度全域台账低于此值几乎一定有漏检")
    ap.add_argument("--baseline", default=None, help="逐国参照 json")
    ap.add_argument("--reference-total", type=int, default=None,
                    help="独立来源给出的同月总量（如官方「单月 500 起安全事件」）")
    ap.add_argument("--reference-label", default="外部参照", help="参照来源名称")
    ap.add_argument("--min-coverage", type=float, default=0.15,
                    help="相对参照总量的最低覆盖率（默认 0.15）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.xlsx):
        print("文件不存在：%s" % a.xlsx)
        return 1

    rows, header, err = read_ledger(a.xlsx)
    if err:
        print(err)
        return 1
    if not rows:
        print("台账里没有数据行 —— 空表不许通过。")
        return 2

    n = len(rows)
    tot_d = sum(r["deaths"] for r in rows)
    by_cty = {}
    for r in rows:
        k = canon(r["country"])
        by_cty[k] = by_cty.get(k, 0) + 1

    fails, warns = [], []

    # ---- 1. 总量下限 ----
    if n < a.min_events:
        fails.append("事件数 %d 低于下限 %d。月度全域台账到不了这个量级，几乎一定是漏检，"
                     "不是这个月没事件。" % (n, a.min_events))

    # ---- 2. 逐国凹陷 ----
    expect = {canon(k): v for k, v in HIGH_RISK.items()}
    if a.baseline:
        try:
            user = json.load(open(a.baseline, encoding="utf-8"))
            if isinstance(user, dict):
                expect.update({canon(k): v for k, v in user.items()})
        except Exception as e:  # noqa: BLE001
            fails.append("无法读取 --baseline：%s" % e)

    hollow = []
    for cty, need in sorted(expect.items(), key=lambda kv: -kv[1]):
        got = by_cty.get(cty, 0)
        if got < need:
            hollow.append((cty, got, need))

    critical = [(c, g, nd) for c, g, nd in hollow if g == 0]
    warning = [(c, g, nd) for c, g, nd in hollow if g > 0]

    if critical:
        fails.append("以下高发国家本月 0 条：%s —— 这些国家每月都有成规模的武装袭击报道，"
                     "0 条几乎必然是漏检。"
                     % "、".join("%s(应≥%d)" % (c, nd) for c, g, nd in critical[:8]))
    if warning:
        warns.append("以下高发国家明显偏低：%s"
                     % "、".join("%s %d条/应≥%d" % (c, g, nd) for c, g, nd in warning[:8]))

    # ---- 3. 量级比对 ----
    coverage = None
    if a.reference_total:
        coverage = n / float(a.reference_total)
        if coverage < a.min_coverage:
            fails.append("覆盖率过低：台账 %d 条 / %s %d 条 = %.1f%%（下限 %.0f%%）。"
                         "两者口径可能不同（如『安全事件』含非恐袭），但差这么多必须先解释清楚。"
                         % (n, a.reference_label, a.reference_total,
                            coverage * 100, a.min_coverage * 100))

    exit_code = 0 if not fails else 2

    if a.json:
        print(json.dumps({
            "file": a.xlsx, "events": n, "deaths": tot_d,
            "min_events": a.min_events, "by_country": by_cty,
            "hollow_zero": [{"country": c, "got": g, "need": nd} for c, g, nd in critical],
            "hollow_low": [{"country": c, "got": g, "need": nd} for c, g, nd in warning],
            "coverage": coverage, "reference_total": a.reference_total,
            "fails": fails, "warns": warns, "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 70)
    print(" 合理性门禁 · 这个量级可信吗")
    print("=" * 70)
    print("文件      : %s" % a.xlsx)
    print("事件数    : %d（下限 %d）" % (n, a.min_events))
    print("死亡合计  : %d" % tot_d)
    print("覆盖国家  : %d" % len(by_cty))
    print()
    print("-- 国别分布 ------------------------------------------------------")
    for c, v in sorted(by_cty.items(), key=lambda kv: -kv[1]):
        print("  %-22s %3d" % (c, v))
    print()

    if coverage is not None:
        print("-- 量级比对 ------------------------------------------------------")
        print("  %s：%d 条" % (a.reference_label, a.reference_total))
        print("  台账 %d 条 → 覆盖率 %.1f%%" % (n, coverage * 100))
        print()

    if warns:
        print("-- 提醒 ----------------------------------------------------------")
        for w in warns:
            print("  ! %s" % w)
        print()

    if fails:
        print("-- 失败项 --------------------------------------------------------")
        for f in fails:
            print("  ✗ %s" % f)
        print()
        print("结论：不通过。这份台账在「内部一致性」上可能全绿，但量级不可信。")
        print("      处置：先补检索（逐国点名本地媒体 / 结构化日历源），不要直接交付。")
        print("=" * 70)
        return 2

    print("结论：量级合理（%d 条，覆盖 %d 国，高发国家无零值凹陷）。" % (n, len(by_cty)))
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
