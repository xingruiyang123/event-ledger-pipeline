#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""改表后门禁：全量逐格对比「改前 / 改后」两份 xlsx。

解决的真实事故：只取字符串字段解析单元格，数字型单元格返回 None -> 整列被写成空白，
全程无报错，所有「行数对不对 / 某列条数对不对」的自检都通过。

退出码：0 = 无意外差异 | 2 = 发现意外差异 | 1 = 无法判定（文件不可读）

用法：
    python scripts/gate_table_roundtrip.py 改前.xlsx 改后.xlsx
    python scripts/gate_table_roundtrip.py 改前.xlsx 改后.xlsx --allow "M列:新增URL" --allow "N列:填第七组"
    python scripts/gate_table_roundtrip.py 改前.xlsx 改后.xlsx --json

判据：
    * None / 空串 / 纯空白 视为等价
    * 数值 2026 与字符串 "2026" 视为等价（避免误报）
    * --allow 指定「预期差异」的列（列字母或列名），该列的差异不计为失败，
      但仍会列出数量，供你在报告里交代
"""
import argparse
import json
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

try:
    import openpyxl
except Exception as e:  # noqa: BLE001
    print("无法导入 openpyxl：%s" % e)
    print("请先执行： python -m pip install openpyxl")
    sys.exit(1)


def norm(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def eq(a, b):
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    # 数值 vs 数字字符串
    try:
        return float(na) == float(nb)
    except Exception:  # noqa: BLE001
        return False


def col_letter(i):
    s = ""
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def load(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    return wb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--allow", action="append", default=[],
                    help="预期有差异的列（列字母如 M，或列名），可多次给")
    ap.add_argument("--sheet", default=None, help="只比这个 sheet（默认比全部）")
    ap.add_argument("--max-report", type=int, default=40, help="最多打印多少条差异明细")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        wb1, wb2 = load(a.before), load(a.after)
    except Exception as e:  # noqa: BLE001
        print("无法读取文件：%s" % e)
        return 1

    sheets = [a.sheet] if a.sheet else sorted(set(wb1.sheetnames) & set(wb2.sheetnames))
    if not sheets:
        print("两份文件没有同名 sheet，无法对比。before=%s after=%s"
              % (wb1.sheetnames, wb2.sheetnames))
        return 1

    allow_cols, allow_names = set(), set()
    for raw in a.allow:
        # 只按逗号/分号切。不再按空格切：列名本身可能含空格或中文，
        # 切成两个 token 会把无关的列也一起放过（实测踩过）。
        for t in re.split(r"[,，;；]+", raw):
            t = t.strip()
            if not t:
                continue
            # isascii() 才算列字母。原来用 isalpha()，两字中文列名（如「备注」）
            # 会被误判成列字母塞进 allow_cols，导致真差异仍报失败。
            if len(t) <= 2 and t.isascii() and t.isalpha():
                allow_cols.add(t.upper())
            else:
                allow_names.add(t)

    total_cells = 0
    diffs = []          # (sheet, cell, before, after, colkey, allowed)
    per_col = {}        # (sheet, colkey) -> count
    fails = []

    for sn in sheets:
        ws1, ws2 = wb1[sn], wb2[sn]
        r1, c1 = ws1.max_row or 0, ws1.max_column or 0
        r2, c2 = ws2.max_row or 0, ws2.max_column or 0
        rows, cols = max(r1, r2), max(c1, c2)

        header = {}
        for c in range(1, cols + 1):
            v = ws1.cell(1, c).value if r1 >= 1 else None
            header[col_letter(c)] = norm(v)

        if r1 != r2 or c1 != c2:
            fails.append("%s: 尺寸变了 %dx%d -> %dx%d" % (sn, r1, c1, r2, c2))

        for r in range(1, rows + 1):
            for c in range(1, cols + 1):
                total_cells += 1
                v1 = ws1.cell(r, c).value if r <= r1 and c <= c1 else None
                v2 = ws2.cell(r, c).value if r <= r2 and c <= c2 else None
                if eq(v1, v2):
                    continue
                cl = col_letter(c)
                name = header.get(cl, "")
                cell = "%s!%s%d" % (sn, cl, r)
                allowed = cl in allow_cols or (name and name in allow_names)
                diffs.append((sn, cell, v1, v2, cl, allowed))
                key = (sn, "%s %s" % (cl, name))
                per_col[key] = per_col.get(key, 0) + 1

    unexpected = [d for d in diffs if not d[5]]

    # ---- 表头必须一字不动 ----
    for sn in sheets:
        ws1, ws2 = wb1[sn], wb2[sn]
        n = max((ws1.max_column or 0), (ws2.max_column or 0))
        for c in range(1, n + 1):
            if not eq(ws1.cell(1, c).value if (ws1.max_row or 0) >= 1 else None,
                      ws2.cell(1, c).value if (ws2.max_row or 0) >= 1 else None):
                fails.append("%s: 表头第 %d 列被改动" % (sn, c))

    if unexpected:
        fails.append("有 %d 处非预期差异" % len(unexpected))

    exit_code = 0 if not fails else 2

    if a.json:
        print(json.dumps({
            "before": a.before, "after": a.after,
            "cells_compared": total_cells,
            "total_diffs": len(diffs),
            "unexpected_diffs": len(unexpected),
            "per_column": {"%s|%s" % k: v for k, v in per_col.items()},
            "fails": fails,
            "diff_sample": [{"cell": d[1], "before": norm(d[2]), "after": norm(d[3]),
                             "allowed": d[5]} for d in diffs[:a.max_report]],
            "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 66)
    print(" 表格改前/改后 · 全量逐格对比")
    print("=" * 66)
    print("before : %s" % a.before)
    print("after  : %s" % a.after)
    print("sheets : %s" % ", ".join(sheets))
    print("比对格数: %d" % total_cells)
    print()

    if per_col:
        print("-- 差异分布（按列）----------------------------------------------")
        allowed_keys = set()
        for sn_, cell_, v1_, v2_, cl_, ok_ in diffs:
            if ok_:
                allowed_keys.add((sn_, cl_))
        for (sn, key), n in sorted(per_col.items(), key=lambda x: -x[1]):
            cl = key.split(" ")[0]
            mark = "  [预期]" if (sn, cl) in allowed_keys else "  [!!意外]"
            print("  %-22s %-26s %5d 格%s" % (sn, key, n, mark))
        print()

    if diffs:
        print("-- 差异明细（最多 %d 条）-----------------------------------------" % a.max_report)
        for sn, cell, v1, v2, cl, allowed in diffs[:a.max_report]:
            tag = " [预期]" if allowed else " [!!意外]"
            print("  %-28s %-38s -> %-38s%s"
                  % (cell, norm(v1)[:36] or "(空)", norm(v2)[:36] or "(空)", tag))
        if len(diffs) > a.max_report:
            print("  ... 另有 %d 条未列出" % (len(diffs) - a.max_report))
        print()

    if fails:
        print("-- 失败项 -------------------------------------------------------")
        for f in fails:
            print("  ✗ %s" % f)
        print()
        print("结论：不通过。上面这些差异要么改回来，要么用 --allow 明确标为预期。")
        print("      特别注意‘整列变成空白’——那是解析时丢了数值型单元格的症状。")
        print("=" * 66)
        return 2

    print("结论：通过。全表 %d 格逐一比对，除预期差异外无改动，表头未变。" % total_cells)
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
