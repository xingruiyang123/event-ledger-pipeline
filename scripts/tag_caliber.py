#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给台账每行加「死亡数口径」说明 —— 让人不用读文档就知道这个数字的边界。

为什么必须加
------------
上一轮实测出两个事实：

  1. UCDP 和 ACLED 对同一事件的死亡数比值 1.51（UCDP 偏高约 50%）
  2. 差异主要来自口径：ACLED 不采信「击毙 N 名武装分子」的战报记 0，
     UCDP 照收（369 对是「UCDP 有、ACLED 报 0」）

这意味着台账的「死亡人数」列**不是同一个口径下的数字**。交付时只给一个
看起来很精确的总数、不说是谁算的，是误导。

做法
----
不动 19 列表头（用户硬性要求），把口径写进「备注」列末尾：

  [源]UCDP ｜ [口径]含交战方战报，可能高于实际
  [源]ACLED ｜ [口径]不采信交战方战报，可能低于实际
  [源]双源 ｜ [口径]两库均有记录，一致性见双源比对报告

用法
----
  python scripts/tag_caliber.py --in 台账.xlsx --out 台账_带口径.xlsx \\\\
      --ucdp ucdp.jsonl --acled acled.jsonl
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

# 各源的死亡数口径说明。这几句话是上一轮实测的结论，不是推测：
#   UCDP 采信交战方战报（俄乌战场尤其明显），全月死亡合计比 ACLED 高 51%
#   ACLED 不采信「击毙 N 名武装分子」，记为 0，因此系统性偏低
CALIBER = {
    "UCDP": "含交战方战报（如「击毙N名武装分子」），可能高于实际",
    "ACLED": "不采信交战方战报，此类记0，可能低于实际",
    "双源": "两库均有记录，口径差异见双源比对报告",
    "信源表": "来自新闻报道，无权威库背书",
    "未遂": "未遂/被挫败事件，两个权威库都不收，来自定向检索",
}


def load_keys(path):
    """从候选文件抽出「能匹配上台账」的键集合。

    台账和候选的字段语言不同（国家 中/英 混杂），所以用
    「国家 + 月 + 日 + 死亡数」的宽松组合，并额外记录摘要前缀。
    """
    if not path or not os.path.isfile(path):
        return set(), set()
    keys, summ = set(), set()
    with io.open(path, encoding="utf-8") as f:
        txt = f.read().strip()
    rows = json.loads(txt) if txt.startswith("[") else \
        [json.loads(l) for l in txt.split("\n") if l.strip()]
    for r in rows:
        try:
            ds = str(r.get("date") or "")
            m = re.match(r"^\d{4}-(\d{2})-(\d{2})", ds)
            mo = int(m.group(1)) if m else int(r.get("月份") or 0)
            dy = int(m.group(2)) if m else int(r.get("日期") or 0)
        except Exception:  # noqa: BLE001
            mo = dy = 0
        de = r.get("deaths")
        if de in (None, ""):
            de = r.get("死亡人数")
        try:
            de = int(float(str(de)))
        except Exception:  # noqa: BLE001
            de = 0
        keys.add((mo, dy, de))
        s = str(r.get("summary") or r.get("事件摘要") or "")[:30].strip().lower()
        if s:
            summ.add(s)
    return keys, summ


def main():
    ap = argparse.ArgumentParser(description="给台账每行标注死亡数口径")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--ucdp", default=None, help="UCDP 候选 jsonl")
    ap.add_argument("--acled", default=None, help="ACLED 候选 jsonl")
    ap.add_argument("--note-col", default="备注")
    a = ap.parse_args()

    try:
        import openpyxl
    except ImportError:
        print("需要 openpyxl", file=sys.stderr)
        return 1

    uk, us = load_keys(a.ucdp)
    ak, as_ = load_keys(a.acled)
    print("UCDP 键 %d 个 ｜ ACLED 键 %d 个" % (len(uk), len(ak)))

    # 支持 json/jsonl 输入：流水线在「判定后、出表前」调用，
    # 那时还没有 xlsx。两种形态走同一套标注逻辑。
    if a.inp.lower().endswith((".json", ".jsonl")):
        with io.open(a.inp, encoding="utf-8") as f:
            txt = f.read().strip()
        recs = json.loads(txt) if txt.startswith("[") else \
            [json.loads(l) for l in txt.split("\n") if l.strip()]
        stat = {}
        for r in recs:
            try:
                mo = int(r.get("月份") or 0)
                dy = int(r.get("日期") or 0)
                de = int(float(str(r.get("死亡人数") or 0)))
            except Exception:  # noqa: BLE001
                mo = dy = de = 0
            k = (mo, dy, de)
            s0 = str(r.get("事件摘要") or "")[:30].strip().lower()
            su = bool(s0 and s0 in us)
            sa = bool(s0 and s0 in as_)
            if su or sa:
                in_u, in_a = su, sa
            elif de > 0:
                in_u, in_a = (k in uk), (k in ak)
            else:
                in_u = in_a = False
            if in_u and in_a:
                src = "双源"
            elif in_u:
                src = "UCDP"
            elif in_a:
                src = "ACLED"
            else:
                note = str(r.get("备注") or "")
                if "未遂" in note:
                    src = "未遂"
                elif "acled" in note.lower():
                    src = "ACLED"
                elif "ucdp" in note.lower():
                    src = "UCDP"
                else:
                    src = "信源表"
            old_n = str(r.get("备注") or "")
            if "[源]" in old_n:
                continue
            r["备注"] = (old_n + " ｜ [源]%s ｜ [口径]%s"
                          % (src, CALIBER.get(src, ""))).strip()
            stat[src] = stat.get(src, 0) + 1
        print()
        print("标注结果：")
        for k2, v2 in sorted(stat.items(), key=lambda x: -x[1]):
            print("  %-6s %5d 行" % (k2, v2))
        with io.open(a.out, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False)
        print()
        print("已写出 → %s" % a.out)
        return 0

    wb = openpyxl.load_workbook(a.inp)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}
    i_note = ix.get(a.note_col)
    if i_note is None:
        print("找不到备注列 %s" % a.note_col, file=sys.stderr)
        return 1

    stat = {}
    for ridx, r in enumerate(rows[1:], start=2):
        if not r or not r[0]:
            continue
        try:
            mo = int(r[ix["月份"]] or 0)
            dy = int(r[ix["日期"]] or 0)
            de = int(float(str(r[ix["死亡人数"]] or 0)))
        except Exception:  # noqa: BLE001
            mo = dy = de = 0
        k = (mo, dy, de)
        s = str(r[ix["事件摘要"]] or "")[:30].strip().lower()

        # 只用 (月,日,死亡数) 当键太宽松，会大面积误判：
        # 实测 ACLED 里 (8,28,0) 这个键出现 453 次、(8,31,0) 400 次 ——
        # 「某天死了 0 人」根本不具辨识度。
        # 结果会把 2415 行标成「双源」，而实际两库重叠只有 1213 对。
        # 改成：摘要前 30 字必须命中（强证据），日期作辅助。
        # 摘要命中（强）或「日期+死亡数」同时命中（中）。
        # 只用日期+死亡数会误判（ACLED 里 (8,28,0) 出现 453 次）；
        # 只用摘要会漏（台账摘要被截断到 30 字，与候选原文未必逐字一致）。
        # 两个条件取并集：摘要优先，摘要不中再看日期+死亡数是否都对。
        # 摘要命中优先；摘要完全不命中时才退到「日期+死亡数」，
        # 且要求死亡数 > 0 —— 死亡数为 0 的键毫无辨识度
        # （ACLED 里 (8,28,0) 出现 453 次、(8,31,0) 400 次）。
        su = bool(s and s in us)
        sa = bool(s and s in as_)
        if su or sa:
            in_u, in_a = su, sa
        elif de > 0:
            in_u, in_a = (k in uk), (k in ak)
        else:
            in_u = in_a = False

        if in_u and in_a:
            src = "双源"
        elif in_u:
            src = "UCDP"
        elif in_a:
            src = "ACLED"
        else:
            # 键匹配不上，看备注里有没有采集端留的 [src] 线索判断来源
            note = str(r[i_note] or "")
            if "未遂" in note:
                src = "未遂"
            elif "acled" in note.lower():
                src = "ACLED"
            elif "ucdp" in note.lower():
                src = "UCDP"
            else:
                src = "信源表"

        old = str(r[i_note] or "")
        if "[源]" in old:
            continue
        tag = "[源]%s ｜ [口径]%s" % (src, CALIBER.get(src, ""))
        ws.cell(ridx, i_note + 1, (old + " ｜ " + tag).strip())
        stat[src] = stat.get(src, 0) + 1

    print()
    print("标注结果：")
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print("  %-6s %5d 行" % (k, v))

    wb.save(a.out)
    print()
    print("已写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
