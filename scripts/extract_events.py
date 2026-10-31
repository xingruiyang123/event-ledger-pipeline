#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「含暴力词的百科条目」抽成结构化事件：{actor, victim, act, deaths, injured, place, date}。

为什么必须有这一层（实测依据）
------------------------------
上游直接给的是自由散文，例如：
    Around 14 people are reported to be killed following an attack by
    RSF militants on the North Kordofan town of Umm Arda.

如果不抽成字段，下游只能用关键词正则「猜」，会产生两类系统性错误：
  1. 误收：「army kills 38 militants」（反恐战果）、「police kill gunman」（执法）
     与「militants kill 38 soldiers」（真袭击）在词表上完全一样。
  2. 误杀：只写「raided a village and set it ablaze」的条目没有杀伤词，被丢掉。

抽取出来的 actor/victim 一填，「谁打谁」就能判了 —— 这才是可判定的结构。

设计原则
--------
  · 纯规则 + 句式模板，不依赖大模型（保证在任何人的机器上都能跑）
  · 抽不出就明确标 unknown，不许猜（猜出来的字段会污染判定）
  · 主语是「国家力量」还是「非国家武装」，是最关键的一个字段

用法：
  python scripts/extract_events.py --in 候选.jsonl --out 事件.jsonl
  python scripts/extract_events.py --in 候选.jsonl --out 事件.jsonl --json
"""
import argparse
import json
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------- 国家/冲突归一
# 同一场战争/同一个国家有多种写法，不归一会把统计彻底打散（实测：Russia 17 条 与 俄乌 1 条并列）
COUNTRY_MAP = {
    "russia": "俄罗斯", "俄乌": "俄罗斯/乌克兰", "ukraine": "乌克兰",
    "israel": "以色列", "gaza": "巴勒斯坦", "palestine": "巴勒斯坦",
    "west bank": "巴勒斯坦", "lebanon": "黎巴嫩", "syria": "叙利亚",
    "iran": "伊朗", "iraq": "伊拉克", "yemen": "也门", "houthi": "也门",
    "saudi arabia": "沙特阿拉伯",
    "sudan": "苏丹", "south sudan": "南苏丹", "darfur": "苏丹",
    "somalia": "索马里", "al-shabaab": "索马里", "al-shabab": "索马里",
    "mali": "马里", "niger": "尼日尔", "burkina faso": "布基纳法索",
    "chad": "乍得", "cameroon": "喀麦隆", "nigeria": "尼日利亚",
    "mozambique": "莫桑比克", "democratic republic of the congo": "刚果（金）",
    "dr congo": "刚果（金）", "congo": "刚果（金）",
    "ethiopia": "埃塞俄比亚", "kenya": "肯尼亚", "uganda": "乌干达",
    "pakistan": "巴基斯坦", "afghanistan": "阿富汗", "india": "印度",
    "bangladesh": "孟加拉国", "myanmar": "缅甸", "thailand": "泰国",
    "philippines": "菲律宾", "indonesia": "印度尼西亚", "malaysia": "马来西亚",
    "colombia": "哥伦比亚", "mexico": "墨西哥", "haiti": "海地",
    "venezuela": "委内瑞拉", "ecuador": "厄瓜多尔", "peru": "秘鲁",
    "brazil": "巴西", "honduras": "洪都拉斯", "guatemala": "危地马拉",
    "libya": "利比亚", "algeria": "阿尔及利亚", "egypt": "埃及",
    "morocco": "摩洛哥", "tunisia": "突尼斯", "turkey": "土耳其",
    "united states": "美国", "france": "法国", "germany": "德国",
    "united kingdom": "英国", "spain": "西班牙", "canada": "加拿大",
    "australia": "澳大利亚", "nepal": "尼泊尔", "sri lanka": "斯里兰卡",
}

# 交战语境：这些国家的条目默认属「国家间战争/内战」，按 M0 或 N4 处理，
# 除非证据显示是明确的非国家主体袭击平民
WAR_CONTEXT = {"俄罗斯", "乌克兰", "俄罗斯/乌克兰", "以色列", "巴勒斯坦", "伊朗",
               "黎巴嫩", "也门", "沙特阿拉伯"}

# ---------------------------------------------------------------- 行为体识别
# 非国家武装（袭击方常见）：命中即倾向「非国家主体」
NONSTATE = re.compile(
    r"(?i)\b(?:al-?shabaab|al-?shabab|boko haram|iswap|isw?ap|isis|isil|daesh|"
    r"jnim|ansar(?:u|ou)?l|aqap|aqim|taliban|ttp|tehrik-i-taliban|bla|"
    r"baloch (?:liberation|separatist)|jamaat-ul-ahrar|lashkar|hizbul|"
    r"adf|allied democratic forces|rsf|rapid support forces|janjaweed|"
    r"far[cd]|eln|eln rebels|farc|dissident|"
    r"militants?|insurgents?|gunmen|gunman|assailants?|attackers?|terrorists?|"
    r"extremists?|jihadis?ts?|bandits?|armed (?:group|men|gang)|gang|"
    r"separatists?|rebels?|vigilantes?|pdf|people's defence force)\b"
)

# 国家力量（袭击方常见，或作为受害方）
STATE_FORCE = re.compile(
    r"(?i)\b(?:army|armed forces|military|air ?force|navy|police|"
    r"security forces|soldiers?|troops?|officers?|constables?|"
    r"national guard|border guard|gendarmerie|peacekeepers?|unmiss|unifil)\b"
)

# 动作类型
ACT_TYPES = [
    ("suicide", r"(?i)suicide (?:bomb|attack|blast)"),
    ("vbied", r"(?i)(?:car|truck|vehicle)[- ]born[ei]|VBIED"),
    ("ied", r"(?i)\bIED\b|improvised explosive|roadside bomb|planted (?:bomb|device)"),
    ("bombing", r"(?i)\b(?:bomb|bombing|blast|explosion|explosive device|grenade|missile|rocket)\b"),
    ("ambush", r"(?i)\bambush"),
    ("armed_attack", r"(?i)\b(?:attack|raid|assault|offensive|storm(?:ed|ing))\b"),
    ("abduction", r"(?i)\b(?:abduct|kidnap|hostage|taken captive)\b"),
    ("arson", r"(?i)\b(?:arson|set (?:fire|ablaze)|burn(?:ed|t)|torch(?:ed)?)\b"),
    ("shooting", r"(?i)\b(?:shooting|shot (?:dead|at)|opened fire|gunfire)\b"),
    ("stabbing", r"(?i)\b(?:stab(?:bed|bing)?|machete|knife)\b"),
    ("drone", r"(?i)\bdrone"),
    ("airstrike", r"(?i)\bair ?strikes?|aerial (?:strike|bombardment)"),
]

# 受害对象
VICTIM_PATTERNS = [
    ("civilians", r"(?i)\bcivilians?\b|\bresidents?\b|\bvillagers?\b|\bcommunity\b|\bcitizens?\b"),
    ("security", r"(?i)\b(?:soldiers?|troops?|police|officers?|army|military personnel|"
                 r"security forces|peacekeepers?)\b"),
    ("children", r"(?i)\bchildren?\b|\bpupils?\b|\bstudents?\b"),
    ("religious", r"(?i)\bchurch\b|\bmosque\b|\bworshipers?\b|\bchristians?\b|\bmuslims?\b|\bnuns?\b"),
    ("journalists", r"(?i)\bjournalists?\b|\breporters?\b"),
    ("officials", r"(?i)\b(?:governors?|mayors?|ministers?|officials?|administrators?)\b"),
]


def canon_country(text, hint=""):
    """从条目文本里找国家，返回归一后的中文国名。"""
    low = (text or "").lower()
    # 先按地名关键词
    PLACE_HINT = [
        (r"norte de santander|cúcuta|cucuta|algeciras|huila|los patios|ojocaliente|"
         r"zacatecas|michoac|guerrero|chihuahua", "哥伦比亚" if "colombia" not in low else "哥伦比亚"),
        (r"kordofan|obeid|khartoum|darfur|blue nile|gezira", "苏丹"),
        (r"marib|hodeidah|sanaa|aden|taiz", "也门"),
        (r"ituri|kivu|beni|mambasa|north kivu|south kivu", "刚果（金）"),
        (r"borno|kaduna|zamfara|sokoto|katsina|kebbi|plateau|benue|taraba|yobe|"
         r"niger state|ogun|imo|enugu|abuja", "尼日利亚"),
        (r"bajaur|waziristan|baloch|swat|kp|khyber|lahore|karachi|peshawar", "巴基斯坦"),
        (r"badakhshan|kabul|kandahar|helmand|nangarhar", "阿富汗"),
        (r"gaza|rafah|khan younis|jabalia|west bank|jenin", "巴勒斯坦"),
        (r"donetsk|kharkiv|luhansk|zaporizhzhia|kherson|belgorod|kyiv|moscow|kursk", "俄罗斯/乌克兰"),
        (r"port-au-prince|kenscoff|ouest", "海地"),
        (r"narathiwat|pattani|yala|songkhla", "泰国"),
        (r"sagaing|rakhine|mandalay|magway|chin state", "缅甸"),
        (r"cab(o|) delgado|muidumbe|n380", "莫桑比克"),
        (r"tillab|diffa|doss|niamey|agadez", "尼日尔"),
        (r"bamako|gao|kidal|mopti|segou|menaka", "马里"),
        (r"mogadishu|lower shabelle|middle shabelle|galmudug|puntland|baidoa", "索马里"),
    ]
    for pat, name in PLACE_HINT:
        if re.search(pat, low):
            return name
    # 再按国名
    for k, v in sorted(COUNTRY_MAP.items(), key=lambda kv: -len(kv[0])):
        if k in low:
            return v
    if hint:
        return COUNTRY_MAP.get(hint.lower(), hint)
    return ""


WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
    "dozens of": 50, "scores of": 20, "several": 5,
}


def _word_to_num(txt):
    """把 "ten" / "dozens of" 之类换成数字，便于后续统一抽取。"""
    low = txt.lower()

    def repl(m):
        k = m.group(0).lower()
        return str(WORD_NUM.get(k, k))
    pat = r"\b(?:" + "|".join(sorted(map(lambda x: re.escape(x), WORD_NUM),
                                        key=len, reverse=True)) + r")\b"
    return re.sub(pat, repl, low)


def extract_numbers(text):
    """抽死亡/受伤。返回 (deaths, injured, basis)。basis 说明这个数怎么来的。"""
    deaths = injured = None
    basis = []
    text = _word_to_num(text)     # "ten soldiers are killed" -> "10 soldiers are killed"

    # 死亡：多种句式
    pats_d = [
        r"(?:at least\s+|around\s+|about\s+|some\s+|over\s+|more than\s+)?(\d{1,4})\s+"
        r"(?:people|civilians|villagers|residents|soldiers|troops|police officers|"
        r"security personnel|workers|miners|children|persons|congolese|"
        r"mali(?:an)? soldiers|mali(?:an)? troops|nigerien soldiers|"
        r"government soldiers|army personnel|members of the security forces)?\s*"
        r"(?:are|were|have been|has been|had been)?\s*"
        r"(?:reported to be\s+|reportedly\s+)?"
        r"(?:killed|dead|died|reported killed|found dead)",
        r"(?:kills?|killed|killing)\s+(?:at least\s+|around\s+|about\s+|some\s+)?(\d{1,4})",
        r"(?:death toll[^.]{0,40}?)(\d{1,4})",
    ]
    for p in pats_d:
        m = re.search(p, text, re.I)
        if not m:
            continue
        n = int(m.group(1))
        # 防误抽 1：1900-2100 的 4 位数是年份，不是死亡数
        if 1900 <= n <= 2100:
            continue
        # 防误抽 2：单起袭击死亡极少超过 500，超过多为累计统计
        if n > 500:
            deaths = "cumulative?"
            basis.append("数字 %d 疑为累计统计，非单起事件，需人工核" % n)
            break
        deaths = n
        basis.append("死亡数来自句式匹配")
        break

    # 受伤
    pats_i = [
        r"(?:at least\s+|around\s+|about\s+|over\s+)?(\d{1,4})\s+"
        r"(?:others?\s+|people\s+|more\s+)?(?:were\s+|are\s+|have been\s+)?"
        r"(?:injured|wounded|hurt)",
        r"(?:injured|wounded)\s+(?:at least\s+)?(\d{1,4})",
    ]
    for p in pats_i:
        m = re.search(p, text, re.I)
        if m:
            injured = int(m.group(1))
            break

    if deaths is None:
        if re.search(r"(?i)\b(killed|dead|died|death|fatalit|massacre)\b", text):
            deaths = "mention_only"      # 提及但无数字 —— 按口径应记 1
            basis.append("提及死亡但无数字")
        else:
            basis.append("未见死亡表述")

    return deaths, injured, "；".join(basis)


def pick_act(text):
    for name, pat in ACT_TYPES:
        if re.search(pat, text):
            return name
    return "unknown"


def pick_victims(text):
    hits = [n for n, p in VICTIM_PATTERNS if re.search(p, text)]
    return hits or []


def judge_side(text):
    """判断「加害方」是国家力量还是非国家武装。这一步决定判定链往哪走。"""
    non = bool(NONSTATE.search(text))
    st = bool(STATE_FORCE.search(text))

    # 语义线索：谁在「做」动词
    # 「X kills Y」/「X attack on Y」里 X 是加害方
    m = re.search(r"(?i)^\s*(?:the\s+)?([A-Za-z\-'\s]{3,50}?)\s+(?:kills?|killed|attacks?|"
                  r"attacked|bombs?|bombed|raids?|raided|ambush(?:es|ed)?)\b", text)
    subj = m.group(1) if m else ""

    subj_non = bool(NONSTATE.search(subj)) if subj else False
    subj_state = bool(STATE_FORCE.search(subj)) if subj else False

    if subj_non:
        return "nonstate", subj.strip()
    if subj_state:
        return "state", subj.strip()
    # 主语判不出来时用整体词频，但标记为不确定
    if non and not st:
        return "nonstate_guess", ""
    if st and not non:
        return "state_guess", ""
    if non and st:
        # 两句都有：「military kills 38 militants」这种
        if re.search(r"(?i)(?:kills|killed|kill)\s+(?:\d+\s+)?(?:militants?|insurgents?|terrorists?|gunmen)", text):
            return "state_counterinsurgency", ""
        return "mixed", ""
    return "unknown", ""


def extract_one(rec):
    # UCDP 候选只有 summary 没有 text，两个都要认，否则整池抽取为空
    text = rec.get("text") or rec.get("summary") or ""
    cty = canon_country(text, rec.get("country_guess") or "")
    deaths, injured, basis = extract_numbers(text)
    act = pick_act(text)
    victims = pick_victims(text)
    side, subj = judge_side(text)
    war = cty in WAR_CONTEXT

    out = dict(rec)
    out.update({
        "country_canon": cty,
        "deaths": deaths,
        "injured": injured,
        "num_basis": basis,
        "act_type": act,
        "victims": victims,
        "perpetrator_side": side,
        "perpetrator_subject": subj,
        "war_context": war,
    })

    # 要素齐全度打分：用于排序与「先核哪些」的决策
    score = 0
    if cty:
        score += 1
    if isinstance(deaths, int) and deaths > 0:
        score += 2
    elif deaths == "mention_only":
        score += 1
    if act != "unknown":
        score += 1
    if victims:
        score += 1
    if side in ("nonstate", "state", "state_counterinsurgency"):
        score += 2
    elif side in ("nonstate_guess", "state_guess", "mixed"):
        score += 1
    out["completeness"] = score
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = []
    with open(a.inp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    out = [extract_one(r) for r in rows]

    with open(a.out, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    sides = Counter(r["perpetrator_side"] for r in out)
    acts = Counter(r["act_type"] for r in out)
    ctys = Counter(r["country_canon"] or "(未识别)" for r in out)
    good = [r for r in out if r["completeness"] >= 5]

    if a.json:
        print(json.dumps({
            "in": a.inp, "out": a.out, "total": len(out),
            "completeness>=5": len(good),
            "by_side": dict(sides), "by_act": dict(acts),
            "by_country_top": ctys.most_common(15),
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 70)
    print(" 结构化事件抽取")
    print("=" * 70)
    print("输入 %d 条 → 输出 %d 条" % (len(rows), len(out)))
    print("要素齐全度 ≥5（可直接判定）：%d 条" % len(good))
    print()
    print("-- 加害方性质（决定判定链走哪条）--------------------------------")
    for k, v in sides.most_common():
        print("  %-28s %3d" % (k, v))
    print()
    print("-- 动作类型 ------------------------------------------------------")
    for k, v in acts.most_common():
        print("  %-28s %3d" % (k, v))
    print()
    print("-- 国家（归一后，前 20）-------------------------------------------")
    for k, v in ctys.most_common(20):
        print("  %-20s %3d" % (k, v))
    print()
    print("→", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
