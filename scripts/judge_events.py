#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用判定器：把候选事件按判定链判成「计入 / 不计入」，输出 19 列台账数据。

为什么必须有这一步（开箱即用的最大断点）
------------------------------------
实测：陌生人拿到候选池后，最卡的地方不是采集、不是出表，而是**判定**：
  · 判定链写在文档里，但文档不会自动执行
  · 让模型逐条判 2000 条，token 烧光且不稳定
  · 每条都要写「命中哪条判据、排除了哪几条反证」，手写不现实

本脚本把**可规则化的部分**做成代码，剩下的明确标「需人工判」，不假装全能。

判定链（与 reference/00-口径先立.md 一致）
----------------------------------------
  步0 M0 硬门槛：实施主体是国家/政府力量 → 不计入
  步1 反证：N1私人 / N2索财 / N3事故 / N4已交战 / N5反恐执法 / N6汇总 → 不计入
  步2 正面判据：P1 / P1b / P3 / P4 / P7 命中任一 → 计入
  步3 兜底 → 不计入

输出三种结论（不是两种！）
------------------------
  计入 / 不计入 / **需人工判**（要素不足，规则判不了）
  —— 「需人工判」不是「待定」，它是「交给人」的明确动作，不是第三种判定结果。

用法
----
  python scripts/judge_events.py --in 候选.jsonl --out 台账数据.json
  python scripts/judge_events.py --in 候选.jsonl --out 台账数据.json --min-deaths 1
  python scripts/judge_events.py --in 候选.jsonl --json      # 只看统计

退出码：0 = 判定完成 | 2 = 判定后计入条数为 0（数据或口径有问题）| 1 = 无法判定
"""
import argparse
import json
import re
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------- 行为体性质
STATE = re.compile(
    r"(?i)\b(?:government of|armed forces|military forces?|national army|"
    r"\barmy\b|\bair ?force\b|\bnavy\b|police forces?|\bpolice\b|"
    r"security forces?|national guard|border guard|gendarmerie|"
    r"ministry of (?:defence|defense|interior)|coalition|nato|"
    r"united nations|peacekeepers?|unmiss|unifil|monusco|paramilitary)\b"
)
CRIMINAL = re.compile(
    r"(?i)\b(?:cartel|carteles|narcos?|mafia|gang|gangs|maras?|"
    r"drug|trafficking|smuggling|los mayos|sinaloa|jalisco|zetas|"
    r"bandits?|banditry|cattle rustlers?|armed robbers?)\b"
)
TERROR = re.compile(
    r"(?i)\b(?:al-?shabaab|al-?shabab|boko haram|iswap|isis|isil|daesh|"
    r"islamic state|jihadist|jnim|ansar(?:u|ou)?l|aqap|aqim|nusrat|"
    r"taliban|ttp|tehrik-i-taliban|bla\b|baloch liberation|"
    r"jamaat-ul-ahrar|lashkar|hizbul|adf\b|allied democratic forces|"
    r"farc|eln\b|national liberation army|dissidents?|"
    r"rsf|rapid support|janjaweed|"
    r"militants?|insurgents?|terrorists?|extremists?|"
    r"separatists?|rebels?|self-defen[cs]e|vigilantes?)\b"
)
INTERSTATE = re.compile(
    r"(?i)\b(?:ukraine|russia|israel|palestine|gaza|iran|lebanon|hezbollah|"
    r"houthi|saudi|yemen|coalition forces|nato|"
    r"ukrainian armed forces|russian armed forces|israel defense forces|idf)\b"
)
# 事故/灾害（N3）
ACCIDENT = re.compile(
    r"(?i)\b(?:accident|collision|crash|explosion at a (?:factory|mine)|"
    r"gas leak|electrical|structural failure|stampede|fire at a (?:factory|market))\b"
)


ANON = re.compile(r"^XXX\d*$", re.I)


def is_anon(s):
    """UCDP 候选集会把未确认的行为体脱敏成 XXX100 这类占位符。

    实测：1984 条候选里 552 条是脱敏的。这些条目**无法用行为体判定**，
    必须降级为「需人工判」，不能靠国家/摘要硬猜 —— 否则哥伦比亚 190 条
    贩毒集团火并会被当成恐袭计入。
    """
    return bool(ANON.match((s or "").strip()))


def side_kind(s):
    if not s:
        return "unknown"
    if TERROR.search(s):
        return "terror"
    if STATE.search(s):
        return "state"
    if CRIMINAL.search(s):
        return "criminal"
    return "unknown"


def judge(rec, min_deaths=1):
    """返回 (verdict, chain, note)。verdict ∈ {计入, 不计入, 需人工判}。"""
    a = rec.get("actor_a", "") or ""
    b = rec.get("actor_b", "") or ""
    txt = " ".join([rec.get("summary", "") or "", rec.get("place", "") or "",
                    a, b, str(rec.get("extra", {}).get("conflict", "") or "")])
    deaths = rec.get("deaths")
    try:
        deaths = int(deaths)
    except Exception:  # noqa: BLE001
        deaths = 0
    civ = rec.get("extra", {}).get("deaths_civilians")
    try:
        civ = int(civ)
    except Exception:  # noqa: BLE001
        civ = 0
    t = str(rec.get("extra", {}).get("type_of_violence") or "")
    cty = rec.get("country", "") or ""

    # 脱敏行为体：无法判性质，直接交人（不猜）
    if is_anon(a) or is_anon(b):
        if deaths == 0 and civ == 0:
            return "不计入", "兜底", "行为体脱敏且无死亡记录"
        return "需人工判", "-", ("行为体被 UCDP 脱敏（%s/%s），无法判性质；"
                                "需看原文确认是恐袭还是帮派/交战" % (a or "?", b or "?"))

    ka, kb = side_kind(a), side_kind(b)
    kinds = {ka, kb}

    # ===== 步0 M0：双方都是国家力量 =====
    if ka == "state" and kb == "state":
        return "不计入", "M0", "双方均为国家/政府力量（%s vs %s）" % (a, b)

    # ===== 步1 反证 =====
    # N3 事故
    if ACCIDENT.search(txt):
        return "不计入", "N3", "疑似事故/非故意事件"
    # N2 犯罪组织
    if "criminal" in kinds:
        return "不计入", "N2", "涉犯罪/贩毒集团（%s vs %s），属以经济目的的帮派暴力" % (a, b)
    # N4 组织间交战
    if t == "3":
        return "不计入", "N4", "非国家武装团体之间交战（%s vs %s）" % (a, b)
    # N4 国家间战争
    if INTERSTATE.search(txt):
        return "不计入", "N4", "国家间战争语境（%s）" % cty
    # N4 政府 vs 叛乱武装的常规交战（有平民死亡则另判）
    if "state" in kinds and civ == 0 and t == "1":
        return "不计入", "N4", "政府与叛乱武装的武装冲突，属交战状态"

    # ===== 步2 正面判据 =====
    # P1：主体属恐怖组织且有实施证据
    if "terror" in kinds and deaths >= min_deaths:
        if civ > 0:
            return "计入", "P1+P4", "恐怖组织袭击致平民死亡 %d" % civ
        return "计入", "P1", "恐怖组织袭击，死亡 %d" % deaths

    # P3/P4：单方面暴力针对平民
    if t == "2" and civ > 0:
        return "计入", "P3+P4", "单方面暴力针对平民，平民死亡 %d" % civ
    if t == "2" and deaths >= min_deaths:
        return "计入", "P3", "单方面暴力，死亡 %d（受害者多为军警/非战斗人员）" % deaths

    # P3：武装冲突中平民死亡
    if civ > 0:
        return "计入", "P4", "武装冲突中平民死亡 %d" % civ

    # ===== 步3 兜底 =====
    if deaths == 0:
        return "不计入", "兜底", "无死亡记录"
    # 有死亡但性质判不出来 → 交人
    return "需人工判", "-", "有死亡（%d）但行为体性质与动作类型均判不出来，需人工看原文" % deaths


def to_row(rec, verdict, chain, note):
    d = rec.get("date") or ""
    try:
        y, mo, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
    except Exception:  # noqa: BLE001
        y = mo = dd = ""
    ns = rec.get("extra", {}).get("n_sources") or 0
    return {
        "事件摘要": (rec.get("summary") or "")[:200],
        "国家": rec.get("country") or "",
        "年份": y, "月份": mo, "日期": dd,
        "地点": rec.get("place") or "",
        "死亡人数": rec.get("deaths") if isinstance(rec.get("deaths"), int) else 0,
        "受伤人数": rec.get("injured") if isinstance(rec.get("injured"), int) else 0,
        "袭击方式": rec.get("act_type") or "",
        "袭击武器": "",
        "袭击者": rec.get("actor_a") or "",
        "负责人/组织": rec.get("actor_a") or "",
        "信息来源URL": rec.get("url") or "",
        "采集人/组别": "",
        "录入日期": "",
        "置信度": "A" if ns >= 2 else "B",
        "审核状态": "已确认" if verdict == "计入" else ("待审核" if verdict == "需人工判" else "已退回"),
        "关联知识点": chain,
        "备注": "[判定]%s" % note,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", default=None, help="计入的台账数据（json 数组）")
    ap.add_argument("--out-excluded", default=None, help="不计入的（json 数组）")
    ap.add_argument("--out-manual", default=None, help="需人工判的（json 数组）")
    ap.add_argument("--min-deaths", type=int, default=1,
                    help="计入的最低死亡数（默认 1；口径要求死亡≥1 或受伤≥3）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = []
    with open(a.inp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    led, exc, man = [], [], []
    reasons = Counter()
    for r in rows:
        v, chain, note = judge(r, a.min_deaths)
        row = to_row(r, v, chain, note)
        if v == "计入":
            led.append(row)
        elif v == "需人工判":
            man.append(row)
        else:
            exc.append(row)
        reasons["%s %s" % (v, chain)] += 1

    def dump(path, data):
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)

    dump(a.out, led)
    dump(a.out_excluded, exc)
    dump(a.out_manual, man)

    exit_code = 0 if led else 2

    if a.json:
        print(json.dumps({
            "in": a.inp, "total": len(rows),
            "included": len(led), "excluded": len(exc), "manual": len(man),
            "reasons": dict(reasons), "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 70)
    print(" 判定结果")
    print("=" * 70)
    print("输入      : %d 条" % len(rows))
    print("计入      : %d 条" % len(led))
    print("不计入    : %d 条" % len(exc))
    print("需人工判  : %d 条" % len(man))
    print()
    print("-- 判定链落点分布 ------------------------------------------------")
    for k, v in reasons.most_common():
        print("  %-22s %5d" % (k, v))
    print()
    if led:
        ctys = Counter(r["国家"] for r in led)
        print("-- 计入的国家分布（前 15）-----------------------------------------")
        for k, v in ctys.most_common(15):
            print("  %-18s %4d" % (k, v))
        print()
        print("-- 死亡合计：%d" % sum(r["死亡人数"] for r in led))
        print()

    if not led:
        print("  ✗ 计入条数为 0。可能原因：")
        print("    · 候选池里没有非国家主体袭击（源选错了）")
        print("    · --min-deaths 设得太高")
        print("    · 行为体字段全为空（UCDP 的 side_a/side_b 没读进来）")

    if man:
        print("  ℹ️  %d 条需人工判 —— 这不是「待定」，是明确的动作：" % len(man))
        print("     逐条看原文，判完写回台账。可用 --out-manual 导出这份清单。")

    print()
    print("→", a.out or "(未指定 --out)")
    print("=" * 70)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
