#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""漏斗对账：从原始记录到台账，每一条的去向都要能说清。

用户的核心要求是「数据全无遗漏」。但「无遗漏」不是「全都进台账」——
判定链本来就要排除大量不符合口径的事件（国家间战争、帮派火并、执法行动）。
真正要保证的是：**排除了多少、为什么排除，加起来必须等于输入**。

差一条对不上，就是数据静默丢失。

本脚本做两件事
--------------
1. **算术对账**：输入 = 计入 + 保留 + 需人工判 + 不计入。差一条就报红。
2. **原因盘点**：每一类排除各多少条，按数量排序，让人一眼看出
   「这一版主要排掉了什么」。

顺带会指出一个容易搞错的点
--------------------------
流水线里有两条链路容易混淆：

  01_候选.jsonl        UCDP + ACLED + 未遂，**判定链的输入**
  01x_合并事件.jsonl    cross_verify 的输出，用于算「独立事件数/多源印证」

拿 01x（15058）去和 02_判定（2568）对账会得到「丢了 12490 条」这个假警报 ——
它们不是同一条链路。本脚本只对 01_候选 做算术对账。

用法
----
  python scripts/audit_funnel.py --cand 01_候选.jsonl \\\\
      --ledger 02_判定.json --manual 02_需人工判.json \\\\
      --excluded 02b_排除.json --keep 02c_保留.json
"""
import argparse
import io
import json
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def load(p):
    if not p or not os.path.isfile(p):
        return []
    with io.open(p, encoding="utf-8") as f:
        txt = f.read().strip()
    if not txt:
        return []
    if txt.startswith("["):
        return json.loads(txt)
    return [json.loads(l) for l in txt.split("\n") if l.strip()]


def reason_of(rec):
    """从备注里抽判定依据。备注格式：[判定]<依据> ｜ ..."""
    note = str(rec.get("备注") or "")
    if "[判定]" in note:
        return note.split("[判定]")[1].split("｜")[0].strip()
    return "(无判定标记)"


def main():
    ap = argparse.ArgumentParser(description="漏斗对账：每条数据的去向必须说得清")
    ap.add_argument("--cand", required=True, help="判定输入（01_候选.jsonl）")
    ap.add_argument("--ledger", required=True, help="计入（02_判定.json）")
    ap.add_argument("--manual", default=None, help="需人工判")
    ap.add_argument("--excluded", default=None, help="不计入")
    ap.add_argument("--keep", default=None, help="保留作对照")
    ap.add_argument("--dedup-to", type=int, default=0,
                    help="judge 内部去重后的条数（不传则用 cand 条数）")
    ap.add_argument("--out", default=None, help="markdown 报告")
    a = ap.parse_args()

    cand = load(a.cand)
    led = load(a.ledger)
    man = load(a.manual)
    exc = load(a.excluded)
    keep = load(a.keep)

    # 去重后的条数优先从 judge 写的 sidecar 读。
    # 拿原始条数当分母会永远差一个「去重掉多少」，表现为假的对账失败。
    base = a.dedup_to
    if not base:
        _meta_p = os.path.join(os.path.dirname(os.path.abspath(a.ledger)),
                               os.path.basename(a.ledger) + ".meta.json")
        if os.path.isfile(_meta_p):
            try:
                _m = json.load(io.open(_meta_p, encoding="utf-8"))
                base = int(_m.get("after_dedup") or 0)
                print("  从 %s 读到去重后条数 %d"
                      % (os.path.basename(_meta_p), base))
            except Exception:  # noqa: BLE001
                base = 0
    if not base:
        base = len(cand)
    total = len(led) + len(man) + len(exc) + len(keep)

    print("=" * 62)
    print("漏斗对账")
    print("=" * 62)
    print()
    print("  判定输入（原始）    %6d" % len(cand))
    if a.dedup_to and a.dedup_to != len(cand):
        print("  内部去重后          %6d  （去重 %d）"
              % (base, len(cand) - base))
    print()
    print("  计入                %6d" % len(led))
    print("  保留作对照          %6d" % len(keep))
    print("  需人工判            %6d" % len(man))
    print("  不计入              %6d" % len(exc))
    print("  " + "-" * 30)
    print("  四项合计            %6d" % total)
    print()

    ok = (total == base)
    if ok:
        print("  ✓ 对账通过：四项合计 = 输入，一条不少。")
    else:
        print("  ✗ 对账失败：差 %+d 条。" % (total - base))
        print("    这不是「排除」，是有数据没走完判定链，必须查。")
    print()

    # 原因盘点
    if exc:
        print("=" * 62)
        print("不计入的 %d 条：按原因排序" % len(exc))
        print("=" * 62)
        c = Counter(reason_of(r) for r in exc)
        for k, v in c.most_common(20):
            print("  %-52s %5d" % (k[:52], v))
        if len(c) > 20:
            print("  … 另有 %d 类" % (len(c) - 20))
        print()

    if man:
        print("=" * 62)
        print("需人工判的 %d 条：按原因排序" % len(man))
        print("=" * 62)
        c = Counter(reason_of(r) for r in man)
        for k, v in c.most_common(12):
            print("  %-52s %5d" % (k[:52], v))
        print()
        print("  「需人工判」不是「待定」，是明确的待办：逐条看原文判完写回。")
        print()

    if a.out:
        lines = ["# 漏斗对账", ""]
        lines.append("| 层 | 条数 |")
        lines.append("|---|---|")
        lines.append("| 判定输入（原始） | %d |" % len(cand))
        if a.dedup_to and a.dedup_to != len(cand):
            lines.append("| 内部去重后 | %d |" % base)
        lines.append("| 计入 | %d |" % len(led))
        lines.append("| 保留作对照 | %d |" % len(keep))
        lines.append("| 需人工判 | %d |" % len(man))
        lines.append("| 不计入 | %d |" % len(exc))
        lines.append("| **四项合计** | **%d** |" % total)
        lines.append("")
        lines.append("对账结论：%s" % ("通过（一条不少）" if ok else
                                     "**失败，差 %+d 条**" % (total - base)))
        lines.append("")
        if exc:
            lines.append("## 不计入的原因（按数量）")
            lines.append("")
            lines.append("| 原因 | 条数 |")
            lines.append("|---|---|")
            for k, v in Counter(reason_of(r) for r in exc).most_common(30):
                lines.append("| %s | %d |" % (k[:70], v))
            lines.append("")
        if man:
            lines.append("## 需人工判的原因（按数量）")
            lines.append("")
            lines.append("| 原因 | 条数 |")
            lines.append("|---|---|")
            for k, v in Counter(reason_of(r) for r in man).most_common(20):
                lines.append("| %s | %d |" % (k[:70], v))
            lines.append("")
        d = os.path.dirname(os.path.abspath(a.out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print("已写出 → %s" % a.out)

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
