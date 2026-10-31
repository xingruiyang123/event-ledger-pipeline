#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数字零漂移门禁：台账逐行加总 vs 对外公布合计，并对派生配置做一致性检查。

解决的真实事故：报告写 925，管线实际公布 916 —— 两个数都能自圆其说，只有一个是对的。

退出码：0 = 全部一致 | 2 = 有漂移 | 1 = 无法判定（读不到/找不到列）

用法：
    # 1) 台账自检（逐行加总 vs --expect，或 vs 表内合计行）
    python scripts/gate_number_drift.py 台账.xlsx
    python scripts/gate_number_drift.py 台账.xlsx --expect 790:238
    python scripts/gate_number_drift.py 台账.xlsx --deaths 死亡人数 --injuries 受伤人数

    # 2) 加一份派生配置做交叉核对（如 report_values.json）
    python scripts/gate_number_drift.py 台账.xlsx --expect 790:238 --config report_values.json

    # 3) 机读
    python scripts/gate_number_drift.py 台账.xlsx --json
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
    print("无法导入 openpyxl：%s  / 处置： python -m pip install openpyxl" % e)
    sys.exit(1)

DEATH_HINTS = ["死亡", "死亡人数", "deaths", "killed", "dead", "death"]
INJ_HINTS = ["受伤", "受伤人数", "injuries", "injury", "wounded", "injured"]


def pick_col(header, hints):
    for i, h in enumerate(header, start=1):
        hs = str(h or "").strip().lower()
        for k in hints:
            if k.lower() in hs:
                return i
    return None


# 中文/英文模糊量词折算表 —— 与 reference/00-口径先立.md 第五节的赋值规则一致。
# 这是硬要求：口径说「数十→50」，代码就必须算成 50，不能静默变成 0。
QUANT = [
    ("数十", 50), ("几十", 50), ("dozens", 50), ("scores", 20),
    ("十数", 15), ("十余", 15), ("十几", 15),
    ("数名", 5), ("数个", 5), ("数人", 5), ("several", 5), ("a few", 5),
]
# 这些值说明「有伤亡但没数字」，按口径应记 1（不是 0）
MENTION_ONLY = ["不详", "未知", "不明", "多", "若干", "several people", "unknown"]
# 这些是真正的「没提」，记 0 才对
ZERO_OK = ["", "-", "—", "–", "0", "无", "无人员死亡", "none", "n/a", "na", "null"]


def to_num(v, strict=True):
    """把单元格转成整数。返回 (数值, 诊断)。

    诊断用于区分四种情况，避免「几十人」被静默算成 0：
      ok       正常数字
      quant    模糊量词折算（数十→50 等）
      mention  提到有伤亡但通篇无数字 -> 按口径记 1
      zero     确实没提 -> 0
      bad      解析不了（strict 时由调用方报错，不许静默当 0）
    """
    if v is None:
        return 0, "zero"
    if isinstance(v, bool):
        return int(v), "ok"
    if isinstance(v, (int, float)):
        return int(v), "ok"

    s = str(v).strip()
    low = s.lower().replace(",", "").replace("，", "")
    if low in ZERO_OK:
        return 0, "zero"

    # 区间「27-32」「27–32」「27~32」：取左边的（口径：取已采用的值），但要点出这是区间
    mrange = re.match(r"^\s*(\d+)\s*[–—~-]\s*(\d+)\s*$", s)
    if mrange:
        return int(mrange.group(1)), "range"

    for kw, val in QUANT:
        if kw.lower() in low:
            # 量词后面若还跟了具体数字（如「数十人中的 30」），以数字为准
            m2 = re.search(r"\d+", low)
            if m2:
                try:
                    return int(m2.group()), "ok"
                except Exception:  # noqa: BLE001
                    pass
            return val, "quant"

    m = re.search(r"-?\d+(?:\.\d+)?", low)
    if m:
        return int(float(m.group())), "ok"

    if any(k in low for k in MENTION_ONLY):
        return 1, "mention"

    return 0, "bad"


def deep_find_numbers(obj, keys):
    """在嵌套 dict/list 里找第一个匹配 keys 的数值。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keys and isinstance(v, (int, float)):
                return int(v)
            r = deep_find_numbers(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_find_numbers(v, keys)
            if r is not None:
                return r
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--deaths", default=None, help="死亡列名（默认按关键词自动找）")
    ap.add_argument("--injuries", default=None)
    ap.add_argument("--expect", default=None, help="公布合计 死:伤，如 790:238")
    ap.add_argument("--config", default=None, help="派生配置 json（如 report_values.json）")
    ap.add_argument("--rows", default=None,
                    help="只统计这个行区间（如 2-38）。表格里混入附录/原始材料/对照表时必须给，"
                         "否则附录会被一起加总")
    ap.add_argument("--stop-at-blank", action="store_true",
                    help="遇到连续 3 个全空行即视为数据区结束（附录区之前常有空行分隔）")
    ap.add_argument("--max-report", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        wb = openpyxl.load_workbook(a.xlsx, data_only=True)
    except Exception as e:  # noqa: BLE001
        print("无法读取 %s：%s" % (a.xlsx, e))
        return 1

    sn = a.sheet or wb.sheetnames[0]
    if sn not in wb.sheetnames:
        print("没有 sheet %s，可选：%s" % (sn, wb.sheetnames))
        return 1
    ws = wb[sn]

    header = [c.value for c in ws[1]] if (ws.max_row or 0) >= 1 else []
    # --deaths/--injuries 一旦显式指定，找不到就必须报错。
    # 原实现会静默回退到关键词自动匹配，导致「以为查的是 A 列，其实查的是 B 列」。
    if a.deaths and a.deaths not in [str(h).strip() for h in header]:
        print("--deaths 指定的列名 %r 不存在。表头 = %s" % (a.deaths, [str(h) for h in header]))
        return 1
    if a.injuries and a.injuries not in [str(h).strip() for h in header]:
        print("--injuries 指定的列名 %r 不存在。表头 = %s" % (a.injuries, [str(h) for h in header]))
        return 1
    cd = ([i for i, h in enumerate(header, 1) if str(h).strip() == a.deaths][0]
          if a.deaths else pick_col(header, DEATH_HINTS))
    ci = ([i for i, h in enumerate(header, 1) if str(h).strip() == a.injuries][0]
          if a.injuries else pick_col(header, INJ_HINTS))

    if not cd or not ci:
        print("找不到伤亡列。表头 = %s" % [str(h) for h in header])
        print("请用 --deaths/--injuries 指定列名。")
        return 1

    if a.rows:
        try:
            lo, hi = [int(x) for x in a.rows.replace("：", "-").split("-")]
        except Exception:  # noqa: BLE001
            print("--rows 格式应为 起-止，如 2-38")
            return 1
    else:
        lo, hi = 2, (ws.max_row or 1)

    rows = []
    blank_run = 0
    for r in range(lo, hi + 1):
        is_blank = all(ws.cell(r, c).value is None for c in range(1, (ws.max_column or 1) + 1))
        if is_blank:
            blank_run += 1
            if a.stop_at_blank and blank_run >= 3:
                warns_stop = r
                break
            continue
        blank_run = 0
        d = ws.cell(r, cd).value
        i = ws.cell(r, ci).value
        # 合计/小计行识别
        first = str(ws.cell(r, 1).value or "")
        if re.search(r"^(合计|总计|小计|总表|total)", first, re.I):
            continue
        nd, kd = to_num(d)
        ni, ki = to_num(i)
        rows.append({"row": r, "deaths": nd, "injuries": ni,
                     "raw_deaths": d, "raw_inj": i, "kind_d": kd, "kind_i": ki})

    tot_d = sum(x["deaths"] for x in rows)
    tot_i = sum(x["injuries"] for x in rows)
    empty_d = [x["row"] for x in rows if x["raw_deaths"] is None or str(x["raw_deaths"]).strip() == ""]
    empty_i = [x["row"] for x in rows if x["raw_inj"] is None or str(x["raw_inj"]).strip() == ""]

    fails, warns = [], []

    # ---- A1. 空表断言：全部自检都通过的空表，是最危险的假通过 ----
    if not rows:
        fails.append("数据区 %d-%d 内没有任何数据行 —— 空表不许通过" % (lo, hi))

    # ---- A1b. 伤亡全 0 断言 ----
    # 实测：134 行、死亡全填 0 的「恐怖袭击台账」能同时通过三个门禁。
    # 恐怖袭击台账死亡合计为 0，几乎一定是数据没赋值或抽取失败。
    if rows and all((x["deaths"] or 0) == 0 and (x["injuries"] or 0) == 0 for x in rows):
        fails.append("全表 %d 行的死亡与受伤都是 0 —— 恐怖袭击台账不该全为 0，"
                     "多半是伤亡列没赋值或抽取失败" % len(rows))

    # ---- A2. 解析失败断言：解析不了的值不许静默当 0 ----
    bad_cells = [(x["row"], x["raw_deaths"], "死亡") for x in rows if x["kind_d"] == "bad"]
    bad_cells += [(x["row"], x["raw_inj"], "受伤") for x in rows if x["kind_i"] == "bad"]
    if bad_cells:
        sample = "; ".join("行%d 死亡列=%r" % (r, v) if c == "死亡" else "行%d 受伤列=%r" % (r, v)
                           for r, v, c in bad_cells[:8])
        fails.append("有 %d 个单元格无法解析成数字（已按 0 回退，会污染合计）：%s"
                     % (len(bad_cells), sample))

    # ---- A3. 模糊量词折算提示（不是错，但必须让人知道算过折算）----
    quant_rows = [x["row"] for x in rows if "quant" in (x["kind_d"], x["kind_i"])]
    range_rows = [x["row"] for x in rows if "range" in (x["kind_d"], x["kind_i"])]
    mention_rows = [x["row"] for x in rows if "mention" in (x["kind_d"], x["kind_i"])]
    if quant_rows:
        warns.append("行 %s 用了模糊量词，已按口径折算（数十→50 / 十数→15 / 数→5）"
                     % quant_rows[:12])
    if range_rows:
        warns.append("行 %s 是区间值，已取左侧数字；必须在「备注」列出原始区间"
                     % range_rows[:12])
    if mention_rows:
        warns.append("行 %s 提到有伤亡但通篇无数字，已按口径记 1" % mention_rows[:12])

    # ---- A4. 表内「合计/总计」行对比（docstring 承诺过，之前其实没实现）----
    sum_row_d = sum_row_i = None
    sum_row_no = None
    for r in range(lo, hi + 1):
        first = str(ws.cell(r, 1).value or "")
        if re.match(r"^(合计|总计|小计|total)", first, re.I):
            sum_row_no = r
            sum_row_d = to_num(ws.cell(r, cd).value)[0]
            sum_row_i = to_num(ws.cell(r, ci).value)[0]
            break
    if sum_row_no is not None:
        if sum_row_d != tot_d:
            fails.append("表内合计行（第 %d 行）死亡写 %d，逐行加总 %d（差 %+d）"
                         % (sum_row_no, sum_row_d, tot_d, tot_d - sum_row_d))
        if sum_row_i != tot_i:
            fails.append("表内合计行（第 %d 行）受伤写 %d，逐行加总 %d（差 %+d）"
                         % (sum_row_no, sum_row_i, tot_i, tot_i - sum_row_i))
        warns.append("已比对表内合计行（第 %d 行）" % sum_row_no)

    if a.rows is None:
        warns.append("未指定 --rows，按全文 %d-%d 行统计。若表内混有附录/对照表，"
                     "请用 --rows 2-N 限定数据区，否则合计会偏大。" % (lo, hi))
    if empty_d:
        fails.append("死亡列有 %d 行留空（口径要求每行必须有数）：行 %s"
                     % (len(empty_d), empty_d[:15]))
    if empty_i:
        fails.append("受伤列有 %d 行留空：行 %s" % (len(empty_i), empty_i[:15]))

    exp_d = exp_i = None
    if a.expect:
        try:
            p = a.expect.replace("：", ":").split(":")
            exp_d, exp_i = int(p[0]), int(p[1])
        except Exception:  # noqa: BLE001
            print("--expect 格式应为 死:伤，如 790:238")
            return 1
        if exp_d != tot_d:
            fails.append("死亡合计漂移：公布 %d vs 逐行加总 %d（差 %+d）"
                         % (exp_d, tot_d, tot_d - exp_d))
        if exp_i != tot_i:
            fails.append("受伤合计漂移：公布 %d vs 逐行加总 %d（差 %+d）"
                         % (exp_i, tot_i, tot_i - exp_i))

    cfg_d = cfg_i = None
    if a.config:
        try:
            cfg = json.load(open(a.config, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            fails.append("无法读取配置 %s：%s" % (a.config, e))
            cfg = None
        if isinstance(cfg, dict):
            cfg_d = deep_find_numbers(cfg.get("published_totals", cfg),
                                      {"deaths", "death", "死亡", "死亡人数"})
            cfg_i = deep_find_numbers(cfg.get("published_totals", cfg),
                                      {"injuries", "injury", "injuries_min", "受伤", "受伤人数"})
            if cfg_d is not None and cfg_d != tot_d:
                fails.append("配置死亡数 %d ≠ 台账逐行加总 %d（差 %+d）"
                             % (cfg_d, tot_d, tot_d - cfg_d))
            if cfg_i is not None and cfg_i != tot_i:
                fails.append("配置受伤数 %d ≠ 台账逐行加总 %d（差 %+d）"
                             % (cfg_i, tot_i, tot_i - cfg_i))
            if cfg_d is None and cfg_i is None:
                fails.append("--config 给了，但一个伤亡数字都没匹配到（检查键名）。"
                             "静默跳过等于没对账。")

    exit_code = 0 if not fails else 2

    if a.json:
        print(json.dumps({
            "file": a.xlsx, "sheet": sn, "data_rows": len(rows),
            "deaths_sum": tot_d, "injuries_sum": tot_i,
            "expect": {"deaths": exp_d, "injuries": exp_i},
            "config": {"deaths": cfg_d, "injuries": cfg_i},
            "empty_deaths_rows": empty_d, "empty_injuries_rows": empty_i,
            "row_range": [lo, hi], "warns": warns,
            "quant_rows": quant_rows, "range_rows": range_rows,
            "mention_rows": mention_rows,
            "unparsable_cells": len(bad_cells),
            "sum_row": sum_row_no,
            "fails": fails, "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 66)
    print(" 数字零漂移门禁 · 逐行加总 vs 公布合计")
    print("=" * 66)
    print("文件      : %s [%s]" % (a.xlsx, sn))
    print("数据行数  : %d  （区间 %d-%d）" % (len(rows), lo, hi))
    print("死亡列    : 第 %d 列 %s" % (cd, header[cd - 1] if len(header) >= cd else ""))
    print("受伤列    : 第 %d 列 %s" % (ci, header[ci - 1] if len(header) >= ci else ""))
    print()
    print("逐行加总  : 死亡 %-6d 受伤 %d" % (tot_d, tot_i))
    if exp_d is not None:
        print("公布合计  : 死亡 %-6d 受伤 %d" % (exp_d, exp_i))
    if cfg_d is not None:
        print("派生配置  : 死亡 %-6s 受伤 %s" % (cfg_d, cfg_i))
    print()

    if warns:
        print("-- 提醒 ---------------------------------------------------------")
        for w in warns:
            print("  ! %s" % w)
        print()

    if empty_d or empty_i:
        print("-- 留空行（口径要求每行必须有数）----------------------------------")
        for r in (empty_d + empty_i)[:a.max_report]:
            print("  行 %d" % r)
        print()

    if fails:
        print("-- 失败项 -------------------------------------------------------")
        for f in fails:
            print("  ✗ %s" % f)
        print()
        print("结论：不通过。先查是不是有行漏赋值、有汇总行被重复计入、或派生配置没跟着重算。")
        print("=" * 66)
        return 2

    if exp_d is None and cfg_d is None:
        print("结论：通过（但本次未做合计对账 —— 没给 --expect，也没有 --config）。")
        print("      建议补上：--expect 死亡:受伤")
    else:
        print("结论：通过。逐行加总与公布合计一致%s。"
              % ("，派生配置也一致" if cfg_d is not None else ""))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
