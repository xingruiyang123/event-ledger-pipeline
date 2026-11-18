#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从人工审核清单建金标准测试集。

真值从哪来
----------
必须是**人工已经逐条判过**的清单。不能拿判定链自己的输出当真值，那是循环论证，
测出来的准确率只说明它和自己一致。

本项目用的真值文件是 `审核清单_111起_20261103.xlsx` 的 `01_逐条审核` sheet，
111 条逐条有审核结论：

    已确认 54  → include（确属恐怖袭击，进台账、进统计）
    改数   19  → include（事件成立，只是伤亡数字修正）
    退回   18  → exclude（明确不是恐袭：国家执法 / 组织间战争 / 汇总结算）
    存疑   20  → manual （证据不足，单列统计，不算对错）

用法
----
  python tests/build_golden.py --review 审核清单.xlsx --out tests/fixtures/golden_2026-07.jsonl
  python tests/build_golden.py --review 审核清单.xlsx --dry-run
"""
import argparse
import io
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 审核结论 → 金标准标签
VERDICT_MAP = {
    "已确认": ("include", "人工确认为恐怖袭击"),
    "改数": ("include", "事件成立，仅伤亡数字修正"),
    "退回": ("exclude", "人工判定不属恐怖袭击"),
    "存疑": ("manual", "证据不足，待补"),
}

NUM_ZH = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_deaths(v):
    """把「至少 10」「约12」「—」这类转成 int 或 None。返回 (数值, 是否下限)。"""
    if v is None:
        return None, False
    s = str(v).strip()
    if not s or s in ("—", "-", "－", "无", "未知", "n/a", "N/A"):
        return None, False
    if s == "0":
        return 0, False
    lower = any(k in s for k in ("至少", "约", "余", "+")) or ("起" in s)
    m = re.search(r"(\d[\d,]*)", s)
    if m:
        return int(m.group(1).replace(",", "")), lower
    for ch, n in NUM_ZH.items():
        if ch in s:
            return n, lower
    return None, lower


def parse_date(year, month, day, raw_date=None):
    """拼 YYYY-MM-DD。台账日期列常有「7月2日」「约7月19日」「7月13-14日」。"""
    src = str(raw_date or "")
    y = re.search(r"(20\d{2})", str(year or ""))
    yr = y.group(1) if y else "2026"
    mo = re.search(r"(\d{1,2})\s*月", src)
    if not mo:
        mo = re.search(r"(\d{1,2})", str(month or "") + "月")
    mm = int(mo.group(1)) if mo else 1
    dy = re.search(r"月\s*(\d{1,2})", src)
    if not dy:
        dy = re.search(r"^(\d{1,2})", src.strip())
    dd = int(dy.group(1)) if dy else 1
    return "%s-%02d-%02d" % (yr, mm, dd)


def main():
    ap = argparse.ArgumentParser(description="从人工审核清单建金标准测试集")
    ap.add_argument("--review", required=True, help="审核清单 xlsx")
    ap.add_argument("--sheet", default=None, help="逐条审核 sheet 名")
    ap.add_argument("--out", default="tests/fixtures/golden.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import openpyxl
    wb = openpyxl.load_workbook(a.review, data_only=True)
    ws = wb[a.sheet] if a.sheet and a.sheet in wb.sheetnames else None
    if ws is None:
        for w in wb.worksheets:
            hdr0 = [str(c or "") for c in next(w.iter_rows(values_only=True), [])]
            if any("审核结论" in h for h in hdr0):
                ws = w
                break
    if ws is None:
        print("找不到含「审核结论」的 sheet", file=sys.stderr)
        return 1

    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}

    def col(*names):
        for n in names:
            for h, i in ix.items():
                if n in h:
                    return i
        return None

    i_ver = col("审核结论")
    i_sum = col("事件摘要")
    i_cty = col("国家")
    i_day = col("日期")
    i_yr = col("年份")
    i_mo = col("月份")
    i_dead = col("死亡")
    i_act = col("袭击者", "行为体")
    i_url = col("URL", "链接")
    i_gid = col("GED")

    if i_ver is None or i_sum is None:
        print("缺关键列。表头：%s" % " / ".join(hdr), file=sys.stderr)
        return 1

    out, skipped = [], {}
    for n, r in enumerate(rows[1:], 1):
        if not r or not r[i_sum]:
            continue
        verdict = str(r[i_ver] or "").strip()
        if verdict not in VERDICT_MAP:
            skipped[verdict] = skipped.get(verdict, 0) + 1
            continue
        label, why = VERDICT_MAP[verdict]
        deaths, lower = parse_deaths(r[i_dead] if i_dead is not None else None)
        out.append({
            "id": "G%03d" % n,
            "summary": str(r[i_sum]).strip(),
            "country": str(r[i_cty] or "").strip() if i_cty is not None else "",
            "date": parse_date(r[i_yr] if i_yr is not None else None,
                               r[i_mo] if i_mo is not None else None,
                               None, r[i_day] if i_day is not None else None),
            "deaths": deaths,
            "deaths_is_lower_bound": lower,
            "actor_a": (str(r[i_act]).strip()
                        if i_act is not None and r[i_act] and str(r[i_act]) != "—" else ""),
            "url": str(r[i_url] or "") if i_url is not None else "",
            "ged_id": str(r[i_gid] or "") if i_gid is not None else "",
            "label": label,
            "label_reason": why,
            "label_verdict": verdict,
        })

    from collections import Counter
    print("读入 %d 行，产出 %d 条标注" % (len(rows) - 1, len(out)))
    print("标签分布:", dict(Counter(x["label"] for x in out)))
    print("审核结论分布:", dict(Counter(x["label_verdict"] for x in out)))
    if skipped:
        print("跳过（无法映射的结论）:", skipped)

    if a.dry_run:
        print()
        for x in out[:5]:
            print("  %s [%s] %s | %s | d=%s" % (x["id"], x["label"], x["country"],
                                                x["summary"][:44], x["deaths"]))
        return 0

    d = os.path.dirname(os.path.abspath(a.out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        for x in out:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print()
    print("写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
