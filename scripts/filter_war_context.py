#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""战争语境过滤器：把「国家间战争/内战」条目在判定前就分离出来。

为什么必须单独一层（实测依据）
------------------------------
2026-08 全量实测中，99 条候选里有 60 条 `perpetrator_side=unknown`。
抽样一看，绝大多数是俄乌、以巴、伊朗-以色列的战争条目：

    8/1  Israeli airstrikes kill three Palestinians in Gaza City
    8/1  Kyiv mayor says an overnight Russian missile attack has killed at least nine
    8/3  The IDF claim that they have killed Palestinian Islamic Jihad commander
    8/3  At least seven people are killed when drone debris falls ...

这些不是「非国家主体实施的恐怖袭击」，是国与国战争行为。
按判定链它们应落 **M0（国家行为体）** 或 **N4（已交战状态）**，
而词表正则永远分不清「army kills 38 militants」和「militants kill 38 soldiers」，
所以必须在判定之前，按**冲突语境**整体分流。

设计要点
--------
  · 用「国家/冲突组合」而不是单个国名判断（以色列打加沙是战争，
    以色列境内发生枪击案不是）—— 单看国名会把两者一起误杀
  · 分流结果三种：war_context（战争）/ nonstate_attack（疑似恐袭）/ unclear
  · 分不清的**保留**，交人工或模型判，不擅自删除

用法：
  python scripts/filter_war_context.py --in 结构化.jsonl --out 分流.jsonl
"""
import argparse
import json
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 明确的战争/大规模冲突语境：(国家或地区, 触发词)
# 命中说明这条属「国家间战争 / 大规模内战」，按 M0 或 N4 处理
WAR_CASES = [
    # 俄乌
    ("俄罗斯/乌克兰", r"(?i)russia|russian|ukrain|kyiv|kharkiv|donetsk|luhansk|"
                    r"zaporizhzhia|kherson|crimea|belgorod|kursk|moscow|"
                    r"putin|zelensk"),
    ("俄罗斯", r"(?i)russia|russian|moscow|putin"),
    ("乌克兰", r"(?i)ukrain|kyiv|kharkiv|odesa"),
    # 以巴 / 以黎 / 伊朗-以色列
    ("巴勒斯坦", r"(?i)gaza|palestin|west bank|rafah|khan younis|jabalia|hamas|"
              r"jenin|nablus|hebron"),
    ("以色列", r"(?i)israel|idf|netanyahu|mossad"),
    ("黎巴嫩", r"(?i)lebanon|hezbollah|beirut"),
    ("伊朗", r"(?i)iran|irgc|tehran|khamenei"),
    # 也门（胡塞 vs 沙特联军属长期战争）
    ("也门", r"(?i)yemen|houthi|sanaa|marib|hodeidah|aden|taiz"),
    # 苏丹内战
    ("苏丹", r"(?i)sudan|rsf|rapid support|khartoum|darfur|obeid|kordofan|"
           r"el fasher|gezira|splm"),
    # 叙利亚（多国介入的长期战争）
    ("叙利亚", r"(?i)syria|damascus|idlib|aleppo|homs|assad|hTS|hayat tahrir"),
    # 缅甸内战
    ("缅甸", r"(?i)myanmar|burma|tatmadaw|rakhine|sagaing|junta|pdf\b"),
    # 刚果（金）东部（多武装并存的长期冲突，但含大量非国家袭击 → 需逐条看）
]

# 「明确指向平民/软目标」的特征：即使处在战争国家，这类条目也可能是恐袭
CIVILIAN_TARGET = re.compile(
    r"(?i)\b(?:civilians?|villagers?|residents?|worshippers?|church|mosque|"
    r"market|wedding|funeral|school|hospital|cafe|restaurant|bus|"
    r"commuters?|farmers?|traders?|miners?)\b"
)

# 明确的「非国家主体袭击」特征：命中则倾向不是国家间战争
NONSTATE_ATTACK = re.compile(
    r"(?i)\b(?:al-?shabaab|al-?shabab|boko haram|iswap|isis|isil|daesh|jnim|"
    r"aqap|aqim|taliban|ttp|tehrik-i-taliban|bla\b|baloch liberation|"
    r"jamaat-ul-ahrar|lashkar|hizbul|adf\b|allied democratic forces|"
    r"farc|eln\b|dissidents?|"
    r"militants?|insurgents?|gunmen|gunman|assailants?|terrorists?|"
    r"extremists?|jihadis?ts?|bandits?|armed (?:group|men|gang)|"
    r"separatists?|rebels?|vigilantes?)\b"
)

# 明确的国家军事行动（M0 特征）
STATE_OP = re.compile(
    r"(?i)\b(?:airstrikes?|air ?force|drone (?:strike|attack)s?|missile (?:strike|attack)s?|"
    r"artillery|shelling|offensive|military operation|armed forces|"
    r"the (?:army|military|idf|irgc|rsf) (?:says|announces|claims|launches|conducts))\b"
)


def classify(rec):
    text = rec.get("text") or ""
    cty = rec.get("country_canon") or ""

    war_hit = None
    for name, pat in WAR_CASES:
        if name == cty and re.search(pat, text):
            war_hit = name
            break
    if war_hit is None:
        # 国名没对上但文本里明显是那场战争
        for name, pat in WAR_CASES:
            if re.search(pat, text) and name in ("俄罗斯/乌克兰", "巴勒斯坦", "苏丹"):
                war_hit = name
                break

    has_civilian = bool(CIVILIAN_TARGET.search(text))
    has_nonstate = bool(NONSTATE_ATTACK.search(text))
    has_stateop = bool(STATE_OP.search(text))

    if war_hit and has_nonstate and has_civilian and not has_stateop:
        # 战争国家境内，但明确是非国家武装打平民 → 可能是恐袭，保留待判
        return "nonstate_attack", "战争国境内，非国家武装袭击平民"
    if war_hit and (has_stateop or not has_nonstate):
        return "war_context", "战争语境（%s）：国家军事行动或交战行为" % war_hit
    if has_nonstate and has_civilian:
        return "nonstate_attack", "非国家武装袭击平民/软目标"
    if has_nonstate:
        return "nonstate_attack", "非国家武装实施"
    if has_stateop:
        return "war_context", "国家军事行动（M0/N4 倾向）"
    return "unclear", "要素不足，需人工或模型判"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.inp, encoding="utf-8") if l.strip()]
    out = []
    for r in rows:
        cls, why = classify(r)
        r2 = dict(r)
        r2["war_filter"] = cls
        r2["war_filter_reason"] = why
        out.append(r2)

    with open(a.out, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    c = Counter(r["war_filter"] for r in out)
    cand = [r for r in out if r["war_filter"] in ("nonstate_attack", "unclear")]

    if a.json:
        print(json.dumps({
            "in": a.inp, "out": a.out, "total": len(out),
            "by_class": dict(c),
            "candidates_for_ledger": len(cand),
            "by_country": Counter(r["country_canon"] or "?" for r in cand).most_common(20),
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 70)
    print(" 战争语境分流")
    print("=" * 70)
    print("输入 %d 条" % len(rows))
    for k, v in c.most_common():
        print("  %-18s %3d" % (k, v))
    print()
    print("进入台账判定的候选：%d 条" % len(cand))
    print()
    print("-- 这些候选的国家分布 --------------------------------------------")
    for k, v in Counter(r["country_canon"] or "(未识别)" for r in cand).most_common(20):
        print("  %-18s %3d" % (k, v))
    print()
    print("→", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
