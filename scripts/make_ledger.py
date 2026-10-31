#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""台账骨架：生成一份与被委托方模板**表头完全一致**的 xlsx，并把候选事件填进去。

为什么需要它：委托方（老师/甲方）给的采集表表头是硬约束——一列都不许增删。
「凭记忆写 19 个字段名」是最容易犯的错（少一个字、多了空格、中文括号写成半角），
这个脚本把表头固化在代码里，任何环节都不再手打。

退出码：0 = 生成成功且校验通过 | 2 = 校验失败 | 1 = 无法判定

用法：
  # 1) 只生成空表（把表头铺好，人工/agent 往里填）
  python scripts/make_ledger.py --template 19列 --out 台账.xlsx

  # 2) 从 CSV/JSONL 候选批量填充
  python scripts/make_ledger.py --template 19列 --out 台账.xlsx --rows events.csv

  # 3) 校验一份已有台账的表头是否与官方模板一致
  python scripts/make_ledger.py --check 台账.xlsx

  # 4) 列出所有内置模板
  python scripts/make_ledger.py --list
"""
import argparse
import csv
import io
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

try:
    import openpyxl
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except Exception as e:  # noqa: BLE001
    print("无法导入 openpyxl：%s\n处置： python -m pip install openpyxl" % e)
    sys.exit(1)

# ============================================================
# 模板定义
# ============================================================
# 恐怖袭击事件采集表（反恐怖基础理论知识库官方模板，19 列，顺序固定）
T19 = [
    ("事件摘要", 46),
    ("国家", 12),
    ("年份", 8),
    ("月份", 8),
    ("日期", 8),
    ("地点", 26),
    ("死亡人数", 10),
    ("受伤人数", 10),
    ("袭击方式", 14),
    ("袭击武器", 14),
    ("袭击者", 30),
    ("负责人/组织", 26),
    ("信息来源URL", 60),
    ("采集人/组别", 12),
    ("录入日期", 14),
    ("置信度", 14),
    ("审核状态", 12),
    ("关联知识点", 24),
    ("备注", 50),
]

TEMPLATES = {
    "19列": {
        "name": "恐怖袭击事件采集表（官方模板 19 列）",
        "cols": T19,
        "note": "表头一列不增不减。判定信息写进「置信度/审核状态/关联知识点/备注」等既有列，不要加列。",
    },
}

# 取值约束：便于 --check 与填充时给出提示
# 台账「置信度」列填 A/B/C/D（对应 reference/00-口径先立.md 的四档）。
# 注意：说「置信度不进对外数字」指的是不进 PPT/报告的数字口径，不是不填这一列。
# 台账里这一列必须填，否则老师看到 19 列里有一列全空。
# 「待核实」是被明令禁止的第三态，不要写进这一列。
ENUMS = {
    "置信度": ["A", "B", "C", "D"],
    "审核状态": ["待审核", "已确认", "已退回", "重复"],
}

HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
HEADER_FONT = Font(bold=True, size=10)


def build_sheet(cols, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "恐怖袭击事件采集表"
    for i, (name, width) in enumerate(cols, start=1):
        c = ws.cell(1, i, name)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    names = [c[0] for c in cols]
    for r, row in enumerate(rows, start=2):
        for i, field in enumerate(names, start=1):
            v = row.get(field, "")
            if v is None:
                v = ""
            # 数字列强转
            if field in ("年份", "月份", "日期", "死亡人数", "受伤人数"):
                try:
                    v = int(float(v)) if str(v).strip() != "" else ""
                except Exception:  # noqa: BLE001
                    pass
            cell = ws.cell(r, i, v)
            cell.alignment = Alignment(vertical="top", wrap_text=(field in
                                  ("事件摘要", "地点", "袭击者", "负责人/组织",
                                   "信息来源URL", "备注", "关联知识点")))
    return wb


def read_rows(path):
    if not os.path.isfile(path):
        raise SystemExit("找不到输入文件：%s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".jsonl":
        out = []
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out
    if ext == ".json":
        d = json.load(io.open(path, encoding="utf-8"))
        return d if isinstance(d, list) else d.get("events", [])
    # csv / tsv
    delim = "\t" if ext in (".tsv", ".tab") else ","
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=delim))


def check_header(path, expect_names):
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb[wb.sheetnames[0]]
    got = []
    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
        got = list(row)
        break
    got = [("" if g is None else str(g).strip()) for g in got]
    # 去掉尾部空列
    while got and got[-1] == "":
        got.pop()
    problems = []
    if len(got) != len(expect_names):
        problems.append("列数不符：期望 %d，实际 %d" % (len(expect_names), len(got)))
    for i, (g, e) in enumerate(zip(got, expect_names), start=1):
        if g != e:
            problems.append("第 %d 列不符：期望 %r，实际 %r" % (i, e, g))
    for j in range(len(got), len(expect_names)):
        problems.append("缺第 %d 列：%r" % (j + 1, expect_names[j]))
    return got, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default="19列")
    ap.add_argument("--out", default=None)
    ap.add_argument("--rows", default=None, help="csv/json/jsonl 候选文件")
    ap.add_argument("--check", default=None, help="校验已有 xlsx 表头")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.list:
        for k, v in TEMPLATES.items():
            print("%-8s %s  (%d 列)" % (k, v["name"], len(v["cols"])))
            print("         " + " | ".join(c[0] for c in v["cols"]))
            print("         " + v["note"])
        return 0

    if a.template not in TEMPLATES:
        print("未知模板 %r，可选：%s" % (a.template, list(TEMPLATES)))
        return 1
    tpl = TEMPLATES[a.template]
    names = [c[0] for c in tpl["cols"]]

    # ---- 校验模式 ----
    if a.check:
        if not os.path.isfile(a.check):
            print("文件不存在：%s" % a.check)
            return 1
        got, problems = check_header(a.check, names)
        if a.json:
            print(json.dumps({"file": a.check, "ok": not problems,
                              "got": got, "expect": names,
                              "problems": problems}, ensure_ascii=False, indent=2))
            return 0 if not problems else 2
        print("=" * 66)
        print(" 表头校验 · 与官方模板 %s 比对" % a.template)
        print("=" * 66)
        print("文件：%s" % a.check)
        print("实际 %d 列：%s" % (len(got), " | ".join(got)))
        print()
        if problems:
            for p in problems:
                print("  ✗ %s" % p)
            print()
            print("结论：表头不合格。委托方模板是硬约束，表头必须一字不差、顺序一致。")
            print("=" * 66)
            return 2
        print("结论：表头与官方模板完全一致（%d 列）。" % len(names))
        print("=" * 66)
        return 0

    # ---- 生成模式 ----
    if not a.out:
        print("需要 --out 指定输出路径，或用 --check 校验已有文件。")
        return 1
    rows = read_rows(a.rows) if a.rows else []
    wb = build_sheet(tpl["cols"], rows)
    try:
        wb.save(a.out)
    except PermissionError:
        print("无法写入 %s（文件被占用或只读）。关掉 Excel 后重试，或先 chmod u+w。" % a.out)
        return 1

    # 写完立刻自校验表头
    got, problems = check_header(a.out, names)
    if a.json:
        print(json.dumps({"out": a.out, "rows": len(rows), "cols": len(names),
                          "header_ok": not problems, "problems": problems},
                         ensure_ascii=False, indent=2))
        return 0 if not problems else 2

    print("=" * 66)
    print(" 台账骨架已生成")
    print("=" * 66)
    print("输出：%s" % a.out)
    print("模板：%s（%d 列，表头一列不增不减）" % (tpl["name"], len(names)))
    print("数据行：%d" % len(rows))
    if problems:
        print()
        for p in problems:
            print("  ✗ 表头自校验失败：%s" % p)
        return 2
    print("表头自校验：通过（%d 列逐字一致）" % len(names))
    print()
    print("下一步：")
    print("  1. 逐行填充；判定结果写进既有列，不要加列")
    print("  2. 填完跑门禁（这三条都能直接跑通）：")
    print("     python scripts/make_ledger.py --check %s          # 表头仍等于 19 列" % a.out)
    print("     python scripts/gate_number_drift.py %s --rows 2-<行数> --expect <死亡>:<受伤>" % a.out)
    print("       （把 <行数>/<死亡>/<受伤> 换成实际数字，例如 --rows 2-143 --expect 878:0）")
    print("     python scripts/gate_single_file.py 交付/index.html --pages N   # 出 PPT 后")
    print()
    print("  gate_table_roundtrip 只用于「同一份表改前 vs 改后」，")
    print("  拿空模板和填好的表对比必然报尺寸变化 —— 那不是你的台账填错了。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
