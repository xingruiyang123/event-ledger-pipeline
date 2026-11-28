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
import io
import json
import os
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

# 政府军行动的迹象词：出现这些，说明大概率是国家力量在打，不是非国家方袭击
GOV_ACTION = re.compile(
    r"(?i)(junta|regime forces|army (?:strike|airstrike|shelling|operation)|"
    r"airstrike|air strike|drone strike|drone attack|shelling|artillery|"
    r"military (?:operation|offensive|campaign)|troops (?:kill|raid|shell)|"
    r"security forces (?:kill|raid)|state forces|government forces)")
# 社区/民族/家族冲突（N1：私人或群体纠纷，不是恐怖主义）
# 实测踩到：UCDP 把「farmers-herders clash」归入 type=2 单方面暴力，
# 被当成恐袭计入（尼日利亚 7-01 死 48 那条）。农牧民冲突是社区暴力，不是恐袭。
COMMUNAL = re.compile(
    r"(?i)\b(?:farmers?[- ]herders?|herders?|herdsmen|communal (?:clash|violence|conflict)|"
    r"inter-?communal|ethnic (?:clash|violence|conflict)|sectarian (?:clash|violence)|"
    r"cattle (?:raid|rustl)|land dispute|family (?:feud|dispute|infighting)|"
    r"clan (?:feud|clash|conflict)|tribal (?:clash|conflict|fighting|violence)|Fulani (?:settlement|village|herders?)|reprisal attack|vigilante (?:clash|group)|border clash(?:es)?|Oromia|Sidama|Amhara|Benishangul|Afar)\b")

# 拉美：UCDP 的 type=2「单方面暴力」在该区域大量用于记录帮派/卡特尔杀人，
# 不是恐怖主义。实测 2026-07：巴西 Comando Vermelho 9 条、墨西哥 Jalisco Cartel 106 条、
# 厄瓜多尔 Los Choneros、哥伦比亚 La Nueva Generación —— 全是街头火并和凶杀，
# 摘要形如「Man shot dead in front of his wife」「拒绝交手机被枪杀」。
# 只有明确的政治性武装组织（ELN/FARC 等）才算。
LATAM = {"巴西", "哥伦比亚", "墨西哥", "厄瓜多尔", "危地马拉", "洪都拉斯",
         "萨尔瓦多", "委内瑞拉", "秘鲁", "玻利维亚", "海地", "多米尼加",
         "巴拿马", "哥斯达黎加", "尼加拉瓜", "阿根廷", "巴拉圭", "智利"}

# 同一批国家的英文/西语写法。踩过的坑：台账里国名有时是英文（如「Costa Rica」），
# 只用中文名集合会让拉美包容规则漏掉整个国家。
LATAM_EN = {"costa rica", "brazil", "colombia", "mexico", "ecuador", "guatemala",
            "honduras", "el salvador", "venezuela", "peru", "bolivia", "haiti",
            "dominican republic", "panama", "nicaragua", "argentina", "paraguay", "chile"}
POLITICAL_ARMED = re.compile(
    r"(?i)(\bELN\b|\bFARC\b|\bEPL\b|dissiden|guerrilla|民族解放军|"
    r"Autodefensas Unidas|\bAUC\b|Sendero Luminoso|光辉道路)")


# ---- 中文行为体识别 ----
# 为什么必须单列一套：英文正则里的 \b 在中文文本里失效 ——
# 中文字符也算 \w，「疑似IS关联」中的 IS 前后没有词边界，\bIS\b 匹配不到。
# 2026-07 真值集实测：只有英文正则时 110 条有明确袭击者的样本，
# 漏报率 97.3%（85 条落「需人工判」）；补上中文正则后见下方测试。
TERROR_ZH = re.compile(
    r"(伊斯兰国|ISIS|ISIL|ISKP|ISSP|ISWAP|ISGS|IS-?K省|IS-?S |"
    # 裸 IS 两字母单独加会误中英文短词，必须绑上下文
    r"疑似\s*IS|IS\s*(?:关联|细胞|成员|武装|分支|本体|宣称|组织|的)|（IS）|\bIS\b(?=\s*关联)|"
    r"基地组织|青年党|博科圣地|塔利班|努斯拉|虔诚军|东伊运|东突|"
    r"圣战|恐怖分子|恐怖组织|武装分子|武装人员|武装突击|武装袭击|"
    r"叛乱分子|反政府武装|分离主义|极端组织|极端分子|"
    r"真主党|哈马斯|圣城旅|圣战者|"
    r"JNIM|JNIM-?FLA|AQIM|Ansar|保卫伊斯兰|"
    r"民族解放军|光辉道路|伏击人员|枪手|袭击者|突击人员)")

STATE_ZH = re.compile(
    r"(政府军|政府部队|军方|军队|安全部队|警察|警方|公安|武警|"
    r"国防部|内政部|国民警卫队|边防|宪兵|维和|军政府|"
    r"当局|安全部门|反恐部门|安全机关|情报部门|NISA|BCIJ|"
    r"部队|官兵|士兵|军人|巡逻队|哨所)")

CRIMINAL_ZH = re.compile(
    r"(贩毒|毒枭|卡特尔|黑帮|帮派|犯罪集团|走私|匪徒|土匪|劫匪|"
    r"偷牛|偷牛贼|绑匪|人贩)")

# ---- 主宾判断：谁打谁 ----
# 为什么必须有这一组：UCDP 的 side_a 是「冲突一方」，通常是政府方，
# 而台账的「袭击者」列直接照抄了它，于是 110 条样本里有 60 多条
# actor_a 写成「Government of Iraq」这类 —— 政府方其实是受害者。
# 拿它当施害者会让整条判定链判反。只能从摘要的主宾关系重新判断。
MILITARY_OP = re.compile(
    r"(?i)(retak(?:e|es|ing)|recaptur|counter-?offensive|offensive to |"
    r"operation to (?:retake|liberate|clear)|liberat(?:e|ed|ion)|clearance operation|"
    r"mopping-?up|sweep operation|"
    r"清剿|围剿|搜剿|收复|夺回|解放|反攻|军事行动|作战行动|空袭毙|击毙.{0,6}名武装分子)")

LAW_ENFORCE = re.compile(
    r"(?i)(arrest(?:ed|s)?|detain(?:ed|s)?|apprehend(?:ed)?|captur(?:ed|es) a suspect|"
    r"foil(?:ed|s)?|thwart(?:ed|s)?|bust(?:ed)?|dismantl(?:ed|es)|"
    r"seized (?:a )?(?:cache|weapons|explosives)|crackdown|"
    r"逮捕|抓捕|抓获|拘捕|拘留|查获|缴获|挫败|破获|打掉|捣毁|抓获|抓获归案)")

GOV_VICTIM = re.compile(
    r"(?i)((?:policemen|police officers?|police|cops?|soldiers?|troops?|officers?|"
    r"security (?:forces?|personnel|officers?)|army personnel|military personnel|"
    r"checkpoint|patrol|barracks|outpost|base|convoy)\s+"
    r"(?:were\s+|was\s+)?(?:killed|shot|attacked|ambushed|injured|wounded|hit|targeted)"
    r"|(?:killed|kills|killing)\s+(?:\d+\s+)?(?:policemen|police|soldiers?|troops?|officers?)"
    r"|(?:attack|ambush|raid|assault|bombing|blast)\s+(?:on|at|against)\s+(?:a\s+)?"
    r"(?:police|military|army|security|checkpoint|base|barracks|patrol|convoy|outpost)"
    r"|terrorist attack|suicide attack|suicide bombing"
    r"|(?:军警|警察|民警|士兵|官兵|哨所|检查站|巡逻队|基地|军营|营地|车队)"
    r"(?:.{0,10})?(?:遭到?|被|受)(?:.{0,8})?(?:袭击|攻击|爆炸|伏击|突袭|枪击|冲击)"
    r"|(?:军警|警察|士兵|官兵)(?:.{0,6})?(?:死亡|阵亡|遇害|受伤|伤亡)"
    r"|(?:袭击|攻击|伏击|突袭)(?:了|了这)?(?:.{0,12})?(?:军警|哨所|检查站|巡逻|基地|军营|车队))")

# 明确施害者信号（英文为主，中文的武装分子等在 TERROR_ZH 里）
ATTACKER_SIGNAL = re.compile(
    r"(?i)(terrorists?|militants?|insurgents?|jihadists?|extremists?|gunmen|"
    r"assailants?|attackers?|perpetrators?|bombers?|suicide bomber|"
    r"suspected militants|armed men|unknown gunmen)")

# 被挫败的恐怖图谋。口径规定「图谋」和「反恐执法行动」是分列的两类，
# 前者进统计、后者不进。实测踩坑：不加这一条时，UCDP 一个非致命事件都不给，
# 未遂专项辛苦检索来的 22 条全被判成 N5 反恐执法、计入 0 条（2025-08 实测）。
FOILED_PLOT = re.compile(
    r"(?i)((?:foil|thwart|bust|prevent|disrupt)\w*\s+(?:a\s+|an\s+)?"
    r"(?:major\s+|deadly\s+|another\s+)?"
    r"(?:terror(?:ist)?|bomb|suicide|attack|plot|conspiracy|plan)|"
    r"(?:plot|plan|conspiracy)\s+(?:was\s+|has\s+been\s+)?(?:foiled|thwarted|busted|disrupted)|"
    r"挫败.{0,30}(?:图谋|恐袭|袭击|爆炸|阴谋|恐攻)|破获.{0,30}(?:图谋|恐怖|恐袭)|粉碎.{0,30}(?:图谋|阴谋|袭击)|阻止.{0,20}(?:恐袭|恐怖袭击)|(?:terror(?:ist)?\s+)?(?:attack|plot|bombing|assassination)\s+(?:was\s+|has\s+been\s+)?(?:foiled|thwarted|prevented|disrupted))")

# 「恐怖分子被击毙」是反恐战果，不是恐袭
TERROR_KILLED = re.compile(
    r"(?i)(?:terrorists?|militants?|insurgents?|gunmen|extremists?)\s+"
    r"(?:were\s+|was\s+|have been\s+)?(?:killed|shot dead|shot|neutralized|neutralised|"
    r"eliminated|arrested|detained)")

# ---- 词袋式受害者判断 ----
# 为什么不用相邻匹配：英文摘要常把被袭者名字夹在中间
# （「Police officer Inam Ali was killed after being targeted」），
# 角色词与死亡词隔了 3 个词，相邻正则全漏。
# 改用「角色词 + 死亡词都在 + 没有主动击杀信号」三条件。
GOV_ROLE = re.compile(
    r"(?i)(policemen|police officers?|police|cops?|soldiers?|troops?|officers?|"
    r"security (?:forces?|personnel|officers?)|army personnel|military personnel|"
    r"paramilitar|checkpoint|patrol|barracks|outpost|convoy|"
    r"polic[ií]a|policias|militares|soldados|agentes|fuerzas militares|"
    r"军警|警察|民警|士兵|官兵|军方|军队|部队|政府军|哨所|检查站|巡逻队|车队|守备|驻军|基地)")

VICTIM_WORD = re.compile(
    r"(?i)(killed|shot dead|shot|attacked|ambushed|injured|wounded|dead|deaths?|"
    r"assassinat|asesinad|muertos?|heridos?|atacad[oa]s?|"
    r"遇袭|遭袭|被杀|遇害|阵亡|死亡|伤亡|受伤|牺牲)")

# 主动击杀：主语是政府方在打人，不是被打
# 英文数字词：正则里只写 \d+ 会漏 "killed four militants" 这类。
_NUMW = (r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
         r"twelve|fifteen|twenty|thirty|fifty|several|many|dozens|scores)")

# 政府方主体：含常用缩写。实测漏掉 SFs 会让
# 「Security Forces (SFs) killed four Fitna al-Hindustan」走到 P5 被计入。
_GOV_SUBJ = (r"(?:army|security|forces?|troops?|police|military|soldiers?|officers?|"
             r"SFs?|SNA|NISA|BDF|FDS|FACA|ISF|RSF|armée|militaires)")

GOV_AGGRESSIVE = re.compile(
    r"(?i)("
    # 政府方名词 + 主动动词（含「killed N 个」）
    # 允许中间夹括号缩写："Security Forces (SFs) killed four ..."
    r"(?:" + _GOV_SUBJ + r")\s*(?:\([^)]{0,15}\)\s*)?"
    # kill 必须带词边界：不加 \b 时 "kill" 会匹配 "killed" 的前四个字母，
    # 把「officers killed」（被袭）误判成「officers kill」（主动击杀）。
    # 实测因此误伤 3 条真恐袭（印度/肯尼亚/巴基斯坦各 1 条）。
    # 主动过去时「Army killed six」由下面的 killed+NUMW 分支兜住。
    r"(?:kills?\b|raid|raided|foil|foiled|arrest|arrested|captur|capture|"
    r"repel|repelled|liberat|rescue|rescued|seiz|seized|"
    r"killed\s+(?:at\s+least\s+)?" + _NUMW + r")"
    # 标题式：ELIMINATED: / KILLED: / NEUTRALIZED: 开头
    r"|^(?:ELIMINATED|KILLED|NEUTRALI[SZ]ED|TARGETED)\s*:"
    r"|neutraliz"
    r"|(?:击毙|击伤|清剿|围剿|逮捕|抓捕|抓获|缴获|挫败|收复|夺回|解救|救出)"
    r")")

# 国家单方军事行动：国家军队/边防/警察对平民或其他方动用武力。
# 判据是「主语为国家武装力量」且「动词为动用武力」，
# 且不是「被袭击」（被动语态由后面的正面判据处理）。
STATE_UNILATERAL = re.compile(
    r"(?i)("
    # 俄军打击 / 以军开火 / 土耳其宪兵
    r"\b(?:Russian|Israeli|Turkish|Iranian|Syrian|Iraqi|Saudi|Emirati|"
    r"Egyptian|Ethiopian|Sudanese|Burmese|Chinese|Indian|Pakistani|"
    r"American|US)\s+"
    r"(?:troops?|forces?|army|military|soldiers?|jandarma|gendarmerie|"
    r"airstrikes?|air\s+strikes?|warplanes?|jets?|drones?)\b"
    r"[^.;]{0,40}?"
    r"\b(?:strikes?|struck|strike|bomb(?:ed|ing)?|shell(?:ed|ing)?|"
    r"fire|fired|kill(?:ed|s)?|raid(?:ed)?|attack(?:ed)?)\b"
    # 中文
    r"|(?:俄军|以军|土军|美军|政府军|边防军|宪兵)[^。；]{0,30}"
    r"(?:空袭|炮击|打击|开火|射杀|击毙)"
    r")")

# 中文「我方被袭」语境：出现这些词说明事件本体是恐袭，
# 政府方击毙只是反击结果，不该整条判成反恐行动。
# 实测误伤：「Cross Kauwa ISWAP未遂渗透中，军方击毙ISWAP宣传组组长」——
# 用户判定该条 include（未遂恐袭成立）。
ZH_VICTIM_CTX = re.compile(
    r"(未遂|渗透|遭袭|遇袭|伏击|袭击中|被袭|阵亡|遇难|丧生|被打死)")

# 摘要没有实质内容：只有话题标签、只有角色词、或极短。
# 实测 2026-07：50 条抽样里有 4 条是 "#Mali" / "#Mali;#Mali;#Mali" /
# "A police officer" 这种 —— 摘要抽取失败，把标签或残句当成了事件描述。
# 这类无法核实，不该计入台账。
EMPTY_SUMMARY = re.compile(
    r"^(?:[#@][\w\u4e00-\u9fff]+[;；,\s]*)+$"          # 纯标签
    r"|^[A-Za-z ]{0,30}(?:officer|soldier|policeman|person|man|woman)"
    r"\s*[.,]?$"                                          # 只有角色词
    r"|^[\s;；,，.。\-#@]*$"                              # 空白
)


def is_empty_summary(txt):
    """摘要是否没有实质内容（只有标签/残句/空白）。"""
    t = str(txt or "").strip()
    if len(t) < 12:
        return True
    if not EMPTY_SUMMARY.match(t):
        return False
    # "#Mali;#Mali" 这类去掉标签后基本为空
    stripped = re.sub(r"[#@][\w\u4e00-\u9fff]+", "", t)
    stripped = re.sub(r"[;；,，\s\-]+", "", stripped)
    return len(stripped) < 8


# 纯匿名：只写「不明」「未知」，没给任何组织线索
ANON_ZH = re.compile(r"^(不明|未知|身份不明|待查|不详|无|—|-|－)")

# ---- 摘要兜底信号 ----
# 行为体被脱敏（XXX100）或分方死亡缺失时，用摘要关键词做二次判定。
# 实测：2026-07 有 140 条非洲条目摘要里有完整事件描述（如
# 「IS claims killing 17 soldiers」「Nine civilians killed in an attack」），
# 但因为没有 actor 名称全被扔进「需人工判」。这些其实能判。
SRC_ATTACK = re.compile(
    r"(?i)("
    r"(?:militants?|insurgents?|terrorists?|jihadists?|gunmen|assailants|"
    r"extremists?|bandits?|IS|ISWAP|ISIS|Boko Haram|JNIM|al-?Shabaab|"
    r"ADF|M23|AQAP|ISKP|TTP|BLA)\s+"
    r"(?:claims?|claimed|kills?|killed|attacks?|attacked|opened fire|raided|stormed)"
    r"|(?:killed|kills|killing|murdered|abducted|massacred)\s+(?:by|in an? attack)"
    r"|(?:suicide|car bomb|roadside bomb|IED|grenade|rocket)\s+(?:attack|blast|explosion)"
    r"|(?:attack|raid|ambush|massacre)\s+(?:on|kills?|leaves?)"
    r"|killing of .{0,40}? by (?:gunmen|militants?|attackers?|assailants?|terrorists?|insurgents?|unknown)"
    r")")
SRC_GOV_OP = re.compile(
    r"(?i)(neutraliz|(?:forces?|troops?|army|police|security|military)\s+(?:kill|killed|kills|raid|raided|foil|foiled|arrest|arrested|capture|captured|repel|repelled|rescue|rescued|launch|launches|manhunt)|rescue|repel|manhunt|foil|arrest|operation\s+(?:against|targeting)|airstrike|air strike|drone strike|offensive|combat)")

# 社区/农牧冲突信号（尼日利亚 Fulani、Plateau 等地高发）
SRC_COMMUNAL = re.compile(
    r"(?i)(Fulani|herders?|herdsmen|farmers?[- ]herders?|Plateau|community (?:attack|clash)|communal|cattle|livestock|land (?:conflict|dispute))")

# 汇总条目（不是单起事件）
# 综述/简报/周报类：不是单起事件。
# 原来只认 "Timeline ... Terrorist" 和 "monthly report"，实测漏掉：
#   CIMP WEEKLY HIGHLIGHTS 16 - 22 JULY 2026
#   Congo War Security Review: July 24, 2026
#   Africa Research Bulletin: Political, Social and Cultural Series Volume 63
#   WRAPUP 13-US strikes Iran ...
# 恐怖主义线索：组织名 / 明确行为体词。用于配合结构化字段判断
# 「平民死亡」到底是恐袭还是普通刑案。
TERROR_ORG = re.compile(
    r"(?i)(\bISIS\b|\bISIL\b|\bIS\b|ISWAP|ISKP|IS-K|Boko\s*Haram|"
    r"al-?Shabaab|al-?Shabab|JNIM|AQAP|AQIM|\bTTP\b|Tehreek-?e-?Taliban|"
    r"\bBLA\b|Baloch|\bADF\b|\bM23\b|\bFDLR\b|\bCODECO\b|"
    r"\bASWJ\b|\bMAC\b|\bRDF\b|\bSNA\b|\bNISA\b|"
    r"Hayat Tahrir|HTS|PKK|YPG|Hezbollah|Hamas|Jihad|Jihadist|"
    r"Taliban|Houthi|Ansar|Mahdi|RSF|Wagner|"
    r"terroris|militant|insurgent|jihadi|extremist|gunmen|assailant|"
    r"恐怖|武装分子|极端|叛乱| jihad)"
)


# ---- 性质可信度（terror certainty）----
# 衡量「这条像不像恐怖袭击」，与「置信度 A/B」（衡量多源印证）是两回事。
# 为什么需要它：整体精确率约 40-60%，规则式方法已到极限（见 SKILL.md）。
# 但错误不是随机分布的 —— 有组织名、分方死亡方向明确、多源印证的可靠得多。
# 实测 2026-07：A+B 档精确率 68-82%，C 档 26-52%。
ORG_STRICT = re.compile(
    r"(?i)\b(ISIS|ISIL|ISWAP|ISKP|Boko\s*Haram|al-?Shabaab|JNIM|AQAP|AQIM|"
    r"TTP|Tehreek-?e-?Taliban|BLA|ADF|M23|FDLR|CODECO|Houthi|Hezbollah|"
    r"Hamas|HTS|PKK|FARC|ELN)\b")
ACTOR_ACT = re.compile(
    r"(?i)\b(terrorists?|militants?|insurgents?|jihadists?|extremists?|"
    r"gunmen|assailants?)\b[^.;]{0,30}?"
    r"\b(kill|killed|kills|attack|attacked|stormed|raided|abduct|bomb|shot)")
ZH_ACTOR_ACT = re.compile(
    r"(恐怖分子|武装分子|极端分子)[^。；]{0,12}(袭击|杀害|打死|绑架|爆炸)")


def terror_score(summary, extra):
    """0-4 分。分越高越像恐怖袭击。

    S1 +1  摘要含具体恐怖组织名
    S2 +1  含 terrorist/militant 等作为施动方
    S3 +1  分方死亡方向明确（只有政府方死）
    S4 +1  多源印证（n_sources >= 2）
    """
    sc, why = 0, []
    t = str(summary or "")
    if ORG_STRICT.search(t):
        sc += 1
        why.append("S1组织名")
    if ACTOR_ACT.search(t) or ZH_ACTOR_ACT.search(t):
        sc += 1
        why.append("S2施动词")
    e = extra or {}
    try:
        a = int(e.get("deaths_a") or 0)
        b = int(e.get("deaths_b") or 0)
    except Exception:  # noqa: BLE001
        a = b = 0
    if a > 0 and b == 0:
        sc += 1
        why.append("S3方向明确")
    try:
        if int(e.get("n_sources") or 0) >= 2:
            sc += 1
            why.append("S4多源")
    except Exception:  # noqa: BLE001
        pass
    return sc, why


def terror_tier(sc):
    """0-4 分 → A/B/C 档。"""
    return "A" if sc >= 3 else ("B" if sc == 2 else "C")


def _extra_of(rec):
    """取 extra 字典（可能是 dict 或 json 字符串）。"""
    e = rec.get("extra") if isinstance(rec, dict) else None
    if isinstance(e, dict):
        return e
    if isinstance(e, str) and e.strip().startswith("{"):
        try:
            return json.loads(e)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _i(v):
    try:
        return int(float(str(v)))
    except Exception:  # noqa: BLE001
        return 0


# 结构化闸门开关：被 judge() 调用时同步
SRC_AGGREGATE = re.compile(
    r"(?i)(Timeline\s*.{0,8}Terrorist"
    r"|monthly (?:report|newsletter|roundup|summary)"
    r"|WEEKLY HIGHLIGHTS|weekly (?:roundup|review|summary|digest)"
    r"|(?:war|security)\s+review"
    r"|Research Bulletin|Political, Social and Cultural Series"
    r"|\bWRAPUP\b|\bROUNDUP\b"
    r"|(?:monthly|weekly)\s+(?:highlights|brief|monitor)"
    r")")

# 其他排除信号（土地冲突/普通刑案/交通事故）
SRC_EXCLUDE = re.compile(
    r"(?i)(land conflict|land dispute|clan conflict|tribal|drug|narcotics|smuggled|domestic (?:dispute|violence)|road accident|traffic (?:accident|crash)|robbery)")

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
    t = (s or "").strip()
    if not t:
        return False
    if ANON.match(t):
        return True
    if ANON_ZH.match(t):
        # 「不明（疑似IS关联细胞）」同时命中 ANON_ZH 和 TERROR_ZH。
        # 有组织线索时不该当纯匿名处理 —— 否则 140 条脱敏样本又被搁置。
        if TERROR_ZH.search(t) or STATE_ZH.search(t) or CRIMINAL_ZH.search(t):
            return False
        return True
    return False


def side_kind(s):
    """判定行为体性质。英文 + 中文两套正则都要查。"""
    if not s:
        return "unknown"
    if TERROR.search(s) or TERROR_ZH.search(s):
        return "terror"
    if STATE.search(s) or STATE_ZH.search(s):
        return "state"
    if CRIMINAL.search(s) or CRIMINAL_ZH.search(s):
        return "criminal"
    return "unknown"


def _num_from_text(txt):
    """从摘要里提取「N killed/killed N/N dead」的数字。"""
    m = re.search(r"(?i)(\d{1,4})\s*(?:people\s*)?(?:were\s*)?(?:killed|dead|died|die)", txt)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)(?:killed|kills|leaves?|left)\s+(?:at least\s+)?(\d{1,4})", txt)
    if m:
        return int(m.group(1))
    return None


def _from_text(txt, civ, deaths, cty, t, act_hint=""):
    """行为体判不了时，用摘要做兜底判定。返回 (verdict, chain, note) 或 None。

    顺序很重要：排除信号 > 政府行动 > 袭击信号。
    因为「军队击毙武装分子」同时含 attack 和 gov_op 词，必须判成反恐行动。
    """
    if SRC_AGGREGATE.search(txt):
        return ("不计入", "兜底", "汇总结算条目，不是单起事件")

    if SRC_EXCLUDE.search(txt) or SRC_COMMUNAL.search(txt):
        return ("不计入", "N1",
                "摘要显示为社区/农牧/土地冲突或普通刑案，非恐怖主义暴力")

    # 采集层已从标题抽出动作类型（如「自杀式袭击」「绑架」）时也算袭击信号。
    # 新闻标题不是 UCDP 那种长句，光靠 SRC_ATTACK 覆盖不到：
    # 实测 7 条典型标题有 4 条匹配不上、直接落「不计入」。
    has_atk = bool(SRC_ATTACK.search(txt)) or bool(act_hint)
    has_gov = bool(SRC_GOV_OP.search(txt))

    # 两者都有 -> 大概率是政府行动（袭击者被击毙之类）
    if has_gov and not (has_atk and re.search(r"(?i)(claims?|claimed|responsibility)", txt)):
        return ("保留", "X1",
                "摘要显示为政府/军警主导的打击行动（反恐行动），作对照、不计入统计")

    if has_atk:
        n = _num_from_text(txt)
        dd = max(civ or 0, deaths or 0, n or 0)
        if civ > 0:
            return ("计入", "P3+P4", "摘要显示武装袭击致平民死亡 %d" % civ)
        if dd > 0:
            return ("计入", "P5", "摘要显示武装袭击，死亡 %d" % dd)
        return ("计入", "P5", "摘要显示武装袭击（未给死亡数）")

    if has_gov:
        return ("保留", "X1", "摘要显示为政府/军警行动，作对照、不计入统计")

    return None


def judge(rec, min_deaths=0):
    # 默认 0，不是 1。口径写明「预谋被破获、未遂、零伤亡的案子照常计入」，
    # 默认 1 会把所有零死亡事件挡在门外 —— 实测 2025-08 全量：ACLED 提供了
    # 11043 条零死亡事件，默认 1 时台账里一条都没进来（死亡=0 的行数 = 0）。
    # 真值集对比：min_deaths=0 准确率 81.3%/漏报 17.8%，=1 时 80.2%/19.2%。
    """返回 (verdict, chain, note)。verdict ∈ {计入, 不计入, 需人工判}。"""
    a = rec.get("actor_a", "") or ""
    b = rec.get("actor_b", "") or ""
    txt = " ".join([rec.get("summary", "") or "", rec.get("place", "") or "",
                    a, b, str(rec.get("extra", {}).get("conflict", "") or "")])
    deaths_raw = rec.get("deaths")
    # 未知 ≠ 0。新闻标题常不写死亡数，把它当 0 会让整批候选被判「无死亡记录」。
    deaths_unknown = deaths_raw is None or str(deaths_raw).strip() == ""
    try:
        deaths = int(deaths_raw)
    except Exception:  # noqa: BLE001
        deaths = 0
    civ = rec.get("extra", {}).get("deaths_civilians")
    try:
        civ = int(civ)
    except Exception:  # noqa: BLE001
        civ = 0
    t = str(rec.get("extra", {}).get("type_of_violence") or "")
    cty = rec.get("country", "") or ""

    # 拉美街头凶杀：必须在兜底判定之前拦，否则被 _from_text 判成袭击
    LATAM_STREET = re.compile(
        r"(?i)(store employee|shop employee|hardware store|found dead|"
        r"shot dead in|killed in front of|inside (?:his|her|a) (?:home|house|car)|"
        r"settling of scores|barber|street vendor|hitman)")
    if ((cty in LATAM or cty.strip().lower() in LATAM_EN) and not POLITICAL_ARMED.search(a + " " + b + " " + txt)
            and (t == "2" or LATAM_STREET.search(txt))):
        return "不计入", "N2", (
            "拉美区域的有组织犯罪/街头凶杀：UCDP 在该区域把帮派与刑案记为单方面暴力，"
            "不属恐怖主义")

    ka, kb = side_kind(a), side_kind(b)
    kinds = {ka, kb}

    # ===== 主宾判断：不依赖 actor 字段的语义 =====
    # UCDP 的 side_a 是「冲突一方」，多数时候是政府方。台账的「袭击者」列
    # 直接抄了它，于是 actor_a 写成「Government of Iraq」—— 政府其实是受害者。
    # 拿它当施害者会把整条判定链判反。这一段只从摘要的主宾关系判断谁打谁。
    # ---- 被挫败的恐怖图谋 = 口径里的「图谋」类，计入并标未遂 ----
    # 顺序放在军事/执法判断之前：否则「安全部队挫败袭击图谋」会被 LAW_ENFORCE 判成
    # 执法行动、计入 0 条。同时出现的「击毙/抓获」是战果，不改变图谋本身成立。
    if FOILED_PLOT.search(txt):
        if GOV_AGGRESSIVE.search(txt):
            return "保留", "X1", ("图谋被挫败且伴随反恐战果，性质=未遂恐袭+反恐成果，"
                                  "保留作对照、不计入统计")
        if deaths == 0:
            return "计入", "P6", "恐怖图谋被挫败（未遂、零死亡），按口径计入并标未遂"
        return "计入", "P6", "恐怖图谋被挫败（未遂），按口径计入并标未遂"

    # 汇总结算条目必须最先拦：「Timeline (Terrorist Activities)」这类月度汇总
    # 里含 terrorists/killed 字样，晚于施害者信号检查就会被当成一起事件。
    if SRC_AGGREGATE.search(txt):
        return "不计入", "N6", "汇总结算条目（月度汇总/时间线），不是单起事件"

    txt_mil = bool(MILITARY_OP.search(txt))
    txt_law = bool(LAW_ENFORCE.search(txt))
    txt_victim = bool(GOV_VICTIM.search(txt)) or bool(
        GOV_ROLE.search(txt) and VICTIM_WORD.search(txt)
        and not GOV_AGGRESSIVE.search(txt))

    # N5 军事作战：收复/清剿/进攻。有军警伤亡也是交战，不是恐袭。
    if txt_mil:
        if txt_victim:
            return "保留", "X2", ("交战/军事作战（双方均有动作），"
                                  "性质=交战，保留作对照、不计入统计")
        return "不计入", "N5", "军事作战/清剿/收复行动（政府方为施动方），非恐怖袭击"

    # N5 执法行动：逮捕/挫败/查获。没有军警伤亡时就是执法本身。
    if txt_law and not txt_victim:
        return "不计入", "N5", "国家力量的执法行动（逮捕/挫败/查获），非恐怖袭击"

    # 政府方为受害方 -> 施害者按定义就是非国家行为体 = 恐怖袭击
    #
    # 这里不再额外要求「恐怖主义信号」。理由：政府方既然是被打的一方，
    # 打它的要么是另一个国家（国家间战争，已由 INTERSTATE 排除），
    # 要么是非国家武装（就是恐袭），要么是罪犯/事故（下面三条先拦掉）。
    # 早前版本在这里要求必须命中 SRC_ATTACK，于是
    # 「Police officer was killed after being targeted by unidentified assailants」
    # 这类标准恐袭全被挡在门外（2026-07 真值集 44 条）。
    # ---- 国家单方军事行动（X3）----
    # 国家军队/边防对另一国或本国平民动用武力，是「国家行为」，
    # 不属于「非国家行为体实施的恐怖袭击」。
    # 已有 M0/INTERSTATE 只认「双方均为国家力量」的表述，漏了单方行动：
    #   Russian troops strike KABs on Kharkiv - one person killed
    #   Israeli fire kills seven people in Gaza
    #   Turkish Jandarma kill young man while attempting to cross
    # 这些在 50 条抽样里占 3 条。
    if STATE_UNILATERAL.search(txt):
        return ("不计入", "X3",
                "国家单方军事行动（军队/边防动用武力），非非国家行为体恐袭")

    # ---- 结构化闸门（S1/S2）----
    # 用 UCDP 的分方死亡判方向，而不是只看摘要措辞。
    # 这些字段一直存在但判定链没用过，是精确率低的根因。
    _e = _extra_of(rec)
    if _e:
        _a = _i(_e.get("deaths_a"))
        _b = _i(_e.get("deaths_b"))
        _civ = _i(_e.get("deaths_civilians"))
        _unk = _i(_e.get("deaths_unknown"))
        _tv = str(_e.get("type_of_violence") or "")

        # S1：只有非国家方死亡 → 政府方战果，反恐行动
        if _b > 0 and _a == 0 and _civ == 0:
            return ("保留", "S1",
                    "UCDP 分方死亡仅非国家方（a=0,b=%d），系政府方战果，保留作对照"
                    % _b)

        # S2：分方死亡全 0，但摘要声称有死亡 → 数据自相矛盾，不硬判
        if _a == 0 and _b == 0 and _civ == 0 and _unk == 0 and deaths > 0:
            if not TERROR_ORG.search(txt):
                return ("需人工判", "-",
                        "UCDP 分方死亡全 0 但摘要称死 %d 人，且无组织线索，需看原文"
                        % deaths)

    # ---- 无内容条目闸门（X0）----
    # 摘要只有标签（"#Mali"）或只有角色词（"A police officer"），
    # 说明抽取失败。这类无法核实，不计入。
    if is_empty_summary(txt):
        return ("不计入", "X0", "摘要无实质内容（只有标签/残句），无法核实")

    # ---- 反恐战果闸门（X2）----
    # 「安全部队击毙/抓获 N 名恐怖分子」是反恐行动，不是恐怖袭击。
    # 本库口径：计入的是「非国家方袭击政府/军警/平民」；政府方主动出击
    # 击毙武装分子的，保留作对照、不计入统计。
    #
    # 为什么单独设闸门：GOV_AGGRESSIVE / SRC_GOV_OP 这两个正则早就定义了，
    # 但只在个别分支里作辅助判断，没当成否决条件。于是
    # 「Benin army kills six militants」直接落到 P1「明确非国家施害者」→ 计入。
    # 实测 2026-07：527 条计入里 148 条是这类，占 28%。
    #
    # 顺序放在最前：这是主语方向的判断（谁打的谁），比后面的受害者词判断更基础。
    # GOV_AGGRESSIVE 要求「政府方名词 + 主动击杀动词」，不会误伤
    # 「Police officer was killed」（被动语态）这类真恐袭。
    if GOV_AGGRESSIVE.search(txt) or SRC_GOV_OP.search(txt):
        # 中文语境下再确认一次：如果句子在讲「我方被袭」，
        # 那里的「击毙」是反击结果，不是反恐行动，不能整条排除。
        if (ZH_VICTIM_CTX.search(txt)
                and not re.search(r"(?:清剿|围剿|收复|夺回)", txt)):
            pass          # 放行，交给后面的正面判据
        else:
            return ("保留", "X2",
                    "安全部队击毙/抓获/清剿（反恐行动，不计入恐袭统计）")

    if txt_victim:
        if ("criminal" in kinds or CRIMINAL.search(txt) or CRIMINAL_ZH.search(txt)):
            return "不计入", "N2", "犯罪/惯匪作案（政府方虽为受害方），非恐怖主义"
        if ACCIDENT.search(txt):
            return "不计入", "N3", "事故/非故意事件，非恐怖袭击"
        if COMMUNAL.search(txt):
            return "不计入", "N1", "社区/民族/部落冲突，非恐怖主义暴力"
        if SRC_AGGREGATE.search(txt):
            return "不计入", "N6", "汇总结算条目，不是单起事件"
        return "计入", "P5", "非国家方袭击军警/政府目标（政府方为受害方）"

    # ---- 明确施害者信号 ----
    # 摘要里出现 terrorists/militants/gunmen 这类词，且有人死伤，就是恐袭。
    # 不要求受害者的职业词能被 GOV_ROLE 认出来 —— 真值集里有
    # 「Security official killed」「Four Frontier Corps personnel were killed」
    # 「A customs personnel was killed」这类，职业词千变万化，列表永远补不全。
    #
    # 但必须先挡住反向表述：「At least five terrorists were killed」
    # 「ISKP terrorists killed one」是反恐战果或已计入条目的重复表述，不是新一起恐袭。
    if TERROR_KILLED.search(txt):
        return "不计入", "N5", "恐怖分子被击毙/被抓获（反恐战果表述），非恐怖袭击"
    if ATTACKER_SIGNAL.search(txt) and (VICTIM_WORD.search(txt) or deaths > 0 or civ > 0):
        if not (ACCIDENT.search(txt) or COMMUNAL.search(txt)):
            return "计入", "P1", "明确非国家施害者（terrorists/militants/gunmen）实施并造成伤亡"

    # 脱敏行为体 / 行为体全空 / 分方死亡缺失：先用摘要兜底判定，判不了才交人
    # 行为体空串必须和脱敏同等对待 —— 逐家遍历来的新闻候选两个字段都是空的，
    # 漏掉这个分支会让整批候选跳过兜底、直接落「需人工判」（实测 109/186）。
    if is_anon(a) or is_anon(b) or (not a.strip() and not b.strip()):
        v = _from_text(txt, civ, deaths, cty, t, rec.get("act_type") or "")
        if v:
            return v
        if deaths == 0 and civ == 0 and deaths_unknown:
            return "需人工判", "-", "行为体脱敏、未披露死亡数，需看原文"
        if deaths == 0 and civ == 0:
            return "不计入", "兜底", "行为体脱敏且无死亡记录"
        return "需人工判", "-", ("行为体被 UCDP 脱敏（%s/%s），摘要无足够信号；"
                                "需看原文确认" % (a or "?", b or "?"))

    # ===== 步0 M0：双方都是国家力量 =====
    if ka == "state" and kb == "state":
        return "不计入", "M0", "双方均为国家/政府力量（%s vs %s）" % (a, b)

    # ===== 步1 反证 =====
    # N3 事故
    if ACCIDENT.search(txt):
        return "不计入", "N3", "疑似事故/非故意事件"
    # N1 社区/民族/家族冲突
    if COMMUNAL.search(txt):
        return "不计入", "N1", "社区/民族/家族冲突（非恐怖主义暴力）"

    # N2 拉美有组织犯罪：该区域 type=2 默认是帮派/卡特尔暴力，不是恐怖主义

    # N2 犯罪组织
    if "criminal" in kinds:
        return "不计入", "N2", "涉犯罪/贩毒集团（%s vs %s），属以经济目的的帮派暴力" % (a, b)
    # N4 双方都是武装/恐怖组织互相打（如 JAS vs ISWAP）
    if ka == "terror" and kb == "terror":
        return "不计入", "N4", "两个武装/恐怖组织之间交战（%s vs %s）" % (a, b)

    # N4 组织间交战
    if t == "3":
        return "不计入", "N4", "非国家武装团体之间交战（%s vs %s）" % (a, b)
    # N4 国家间战争
    if INTERSTATE.search(txt):
        return "不计入", "N4", "国家间战争语境（%s）" % cty
    # type=1 = 至少一方是国家/政府的冲突。按「委托方口径」分类，不再一律排除：
    #
    #   委托方台账的实际做法（从 2026-07 已交付台账提炼）：
    #     · 尼日尔「ISSP 伏击军事巡逻，16 士兵死」        -> 性质:恐怖袭击（计入）
    #     · 巴基斯坦「海岸警卫队哨所遭 VBIED+突击」        -> 性质:恐怖袭击（计入）
    #     · 马里「军方车队遭 JNIM-FLA 伏击」               -> 性质:恐怖袭击（计入）
    #     · 叙当局「逮捕 IS 关联小组」                     -> 性质:反恐行动（对照，不计入伤亡统计）
    #     · 摩洛哥「挫败七城市连环图谋」                   -> 性质:反恐行动（对照，不计入）
    #
    # 也就是：**非国家方打政府方 = 恐怖袭击；政府方打非国家方 = 反恐行动**。
    # 这个区分用 UCDP 的 deaths_a / deaths_b 可以自动判，不需要人工。
    if t == "1":
        d_a = rec.get("extra", {}).get("deaths_a")
        d_b = rec.get("extra", {}).get("deaths_b")
        try:
            d_a = int(d_a) if d_a is not None else None
            d_b = int(d_b) if d_b is not None else None
        except Exception:  # noqa: BLE001
            d_a = d_b = None

        # side_a 通常记为政府方，据此换算「政府方伤亡 / 非国家方伤亡」
        if ka == "state":
            gov_loss, ins_loss = d_a, d_b
        elif kb == "state":
            gov_loss, ins_loss = d_b, d_a
        else:
            gov_loss = ins_loss = None

        # A. 政府方 0 死、非国家方有死 -> 政府主导打击 = 反恐行动，保留但单独标性质
        if gov_loss == 0 and (ins_loss or 0) > 0:
            return "保留", "X1", (
                "政府主导打击（政府方0死/对方死%s），性质=反恐行动，保留作对照、不计入恐袭统计"
                % ins_loss)

        # B. 非国家方单方面袭击政府方（政府方有死、对方无死）= 恐怖袭击
        if (gov_loss or 0) > 0 and (ins_loss or 0) == 0:
            if civ > 0:
                return "计入", "P5+P4", (
                    "非国家方袭击政府/军警目标致政府方死%s，并伤及平民%d（%s vs %s）"
                    % (gov_loss, civ, a, b))
            return "计入", "P5", (
                "非国家方袭击政府/军警目标（检查站/哨所/巡逻队/基地），政府方死%s（%s vs %s）"
                % (gov_loss, a, b))

        # C. 双方都有死 = 交火/交战，保留但标性质
        if (gov_loss or 0) > 0 and (ins_loss or 0) > 0:
            return "保留", "X2", (
                "双方交火（政府方%s/对方%s），性质=交战，保留作对照、不计入统计"
                % (gov_loss, ins_loss))

        # D. 分方死亡都是 0、只有平民死 -> 判不了是谁干的，不能算恐袭。
        #
        # 实测踩坑：缅甸「Junta Drone Attack Kills Two Civilians」、
        # 苏丹「Sudanese army drone strike kills civilians」这类条目，
        # deaths_a=0 且 deaths_b=0（双方战斗人员都没死、只有平民死），
        # side_a 却是「Government of Myanmar/Sudan」—— 这是政府军杀平民（国家暴力），
        # 早前版本落到下面「有平民死亡就计入 P4」，把政府军空袭算成了恐怖袭击。
        if civ > 0:
            if GOV_ACTION.search(txt):
                return "保留", "X1", (
                    "疑似国家力量行动致平民死亡 %d（%s）：性质=国家暴力，保留作对照、不计入"
                    % (civ, a))
            return "需人工判", "-", (
                "平民死亡 %d 但分方死亡均为 0、实施方不明（%s vs %s），需看原文"
                % (civ, a, b))
        return "需人工判", "-", "国家参与的冲突但分方死亡缺失（%s vs %s）" % (a, b)


    # ===== 步2 正面判据 =====
    # P1：主体属恐怖组织且有实施证据
    if "terror" in kinds and (deaths >= min_deaths or deaths_unknown):
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
    if deaths == 0 and deaths_unknown:
        return "需人工判", "-", "未披露死亡数（新闻标题常见），需看原文补"
    if deaths == 0:
        return "不计入", "兜底", "无死亡记录"
    # 有死亡但性质判不出来 → 交人
    return "需人工判", "-", "有死亡（%d）但行为体性质与动作类型均判不出来，需人工看原文" % deaths


def _src_hint(rec):
    """从 UCDP 记录里拼出「媒体名 + 标题」，给补链接脚本用。"""
    ex = rec.get("extra", {}) or {}
    office = (ex.get("source_office") or "").split(";")[0].strip()
    hl = (ex.get("source_headline") or "").strip()
    if ";" in hl:
        hl = hl.split(";")[0].strip()
    if "|" in hl:
        hl = re.split(r"[|｜]", hl)[-1].strip()
    if not office or not hl:
        art = (rec.get("source_citation") or ex.get("source_citation") or "").strip().strip('"')
        parts = [p.strip().strip('"') for p in art.split(",")]
        if len(parts) >= 3:
            office = office or parts[0]
            if not hl:
                hl = parts[-1]
                if ";" in hl:
                    hl = hl.split(";")[0].strip()
                if "|" in hl:
                    hl = re.split(r"[|｜]", hl)[-1].strip()
    hint = ("%s %s" % (office, hl)).strip()[:70]
    return hint or "无来源线索"


# 跨源印证映射：raw_id -> {n: 几个源印证, agg: 是否累计条目}
# 由 cross_verify.py 产出，judge_events.py --verify-map 传入。
# 判定链自己看不到「别的源有没有记这件事」，只能靠这份映射。
VERIFY_MAP = {}


def _n_sources(rec):
    """这条记录被几个源印证。优先查映射，查不到就退回自带的 n_sources。"""
    rid = str(rec.get("raw_id") or "")
    if rid and rid in VERIFY_MAP:
        return int(VERIFY_MAP[rid].get("n") or 1)
    ex = rec.get("extra") or {}
    for k in ("acled_event_id", "ged_id"):
        v = ex.get(k)
        if v and str(v) in VERIFY_MAP:
            return int(VERIFY_MAP[str(v)].get("n") or 1)
    ns = ex.get("n_sources") or 0
    try:
        return int(ns)
    except Exception:  # noqa: BLE001
        return 0


def _weapons_of(rec):
    """按《判定与编码规范》第 2.7 节填「袭击武器」。

    19 列里这一列原来一直是空的 —— 因为上游没有武器分类。
    现在调 act_classify.pick_weapons()：9 类、多武器全记、
    涉及无人机一律记「新兴袭击武器」。

    取文本的范围用摘要 + 备注：正文在判定阶段已经不在了，
    而摘要通常在首句就写了手段（「suicide bombing」「gunmen opened fire」）。
    这一段抽不到的，留「不详」，不猜。
    """
    txt = " ".join(str(rec.get(k) or "") for k in ("summary", "note", "extra"))
    try:
        import act_classify as AC
        return AC.weapons_str(txt)
    except Exception:  # noqa: BLE001
        return ""


def casualty_for_ledger(rec, key):
    value = rec.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if value is not None and str(value).strip():
        text = str(value).strip()
        return int(text) if text.isdecimal() else text
    raw = _extra_of(rec).get("casualty_source_values")
    return raw.get(key, "") if isinstance(raw, dict) else ""


def to_row(rec, verdict, chain, note):
    d = rec.get("date") or ""
    try:
        y, mo, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
    except Exception:  # noqa: BLE001
        y = mo = dd = ""
    ns = _n_sources(rec)
    # 性质可信度：0-4 分，A/B = 高置信，C = 需人工复核
    try:
        _sc, _why = terror_score(rec.get("summary") or "", _extra_of(rec))
    except Exception:  # noqa: BLE001
        _sc, _why = 0, []
    _tier = terror_tier(_sc)
    return {
        "事件摘要": (rec.get("summary") or "")[:200],
        "国家": rec.get("country") or "",
        "年份": y, "月份": mo, "日期": dd,
        "地点": rec.get("place") or "",
        "死亡人数": casualty_for_ledger(rec, "deaths"),
        "受伤人数": casualty_for_ledger(rec, "injured"),
        "袭击方式": rec.get("act_type") or "",
        "袭击武器": _weapons_of(rec),
        "袭击者": rec.get("actor_a") or "",
        "负责人/组织": rec.get("actor_a") or "",
        "信息来源URL": rec.get("url") or "",
        "采集人/组别": "",
        "录入日期": "",
        # A = 两个及以上独立源都记了这件事；B = 只有一个源。
        # 不是「我们有多相信自己的判断」，而是「这件事有几个独立来源」。
        "置信度": "A" if ns >= 2 else "B",
        "审核状态": "待审核",
        "关联知识点": chain,
        # 备注里带上「媒体名 + 标题」：UCDP 不给 URL，后续 enrich_urls 靠这个反查
        # 另带「性质分档」：A/B = 高置信子集（实测精确率 68-82%），
        # C = 需人工复核（26-52%）。19 列表头不能动，所以放备注。
        "备注": "[判定]%s ｜ [性质]%s%d分 ｜ [src]%s"
                % (note, _tier, _sc, _src_hint(rec)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", default=None, help="计入的台账数据（json 数组）")
    ap.add_argument("--out-excluded", default=None, help="不计入的（json 数组）")
    ap.add_argument("--verify-map", default=None,
                    help="cross_verify.py 产出的印证映射（*.idx.json）。"
                         "给了才能把「几个源印证」写进置信度列")
    ap.add_argument("--out-manual", default=None, help="需人工判的（json 数组）")
    ap.add_argument("--out-keep", default=None, help="保留作对照的（反恐行动/交战，不计入统计）")
    ap.add_argument("--min-deaths", type=int, default=0,
                    help="计入的最低死亡数（默认 0 = 不按死亡数设门槛）。"
                         "口径要求未遂、零伤亡照常计入，设成 1 会把它们全挡掉")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    global VERIFY_MAP
    if a.verify_map and os.path.isfile(a.verify_map):
        try:
            with io.open(a.verify_map, encoding="utf-8") as f:
                VERIFY_MAP = json.load(f) or {}
            print("印证映射：%d 条 ID（跨源核验结果）" % len(VERIFY_MAP))
        except Exception as e:  # noqa: BLE001
            print("印证映射读取失败：%s" % str(e)[:100])

    rows = []
    with open(a.inp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    # 先去重：同国 + 同日 + 同死亡数 -> 同一事件被多次记录
    seen = set()
    deduped = []
    for r in rows:
        # 保守去重：国家+日期+死亡数+地点 都相同才算同一事件。
        # 只按 国+日+死 会把「同一天同国两起都死1人」误合并（实测去掉 872 条，过激）。
        _cty, _pl = r.get("country"), r.get("place")
        _summ = re.sub(r"\W+", "", (r.get("summary") or ""))[:30].lower()
        # 只有「国家 + 日期 + 死亡数 + 地点」四者齐全时才敢用这四元组去重。
        # 缺任何一个都会误合并：
        #   · 国家地点都缺（逐家遍历候选）→ 实测 148 条被并成 53 条
        #   · 有国家缺地点（矩阵遍历候选）→ 实测 1092 条被并成 338 条，
        #     20 起已知靶子只命中 1 起，看起来像「遍历没用」，其实是去重吃掉了
        # 所以两种情形都退到「摘要前缀」兜底。
        if _cty and _pl:
            key = (_cty, r.get("date"), r.get("deaths"), _pl)
        else:
            key = ("__src__", _cty, _pl, r.get("date"),
                   r.get("deaths"), _summ)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    if len(deduped) != len(rows):
        print("去重：%d -> %d 条（同国+同日+同死亡数视为同一事件）" % (len(rows), len(deduped)))
    # 去重后的条数要落盘：audit_funnel 做算术对账时必须用这个数当分母。
    # 不落盘的话对账会拿原始条数去比四项合计，永远差一个「去重掉多少」，
    # 表现为「对账失败：差 -178 条」这种假警报 —— 实测踩过。
    try:
        _meta = {"input": len(rows), "after_dedup": len(deduped),
                 "deduped": len(rows) - len(deduped)}
        if a.out:
            with io.open(a.out + ".meta.json", "w", encoding="utf-8") as _mf:
                json.dump(_meta, _mf, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    rows = deduped

    led, exc, man, keep = [], [], [], []   # keep=保留作对照（反恐行动/交战），不计入统计
    reasons = Counter()
    for r in rows:
        v, chain, note = judge(r, a.min_deaths)
        row = to_row(r, v, chain, note)
        if v == "计入":
            led.append(row)
        elif v == "保留":
            row["备注"] = "[对照]%s ｜ %s" % (note, row.get("备注", ""))
            keep.append(row)
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
    dump(a.out_keep, keep)

    exit_code = 0 if led else 2

    if a.json:
        print(json.dumps({
            "in": a.inp, "total": len(rows),
            "included": len(led), "excluded": len(exc),
            "manual": len(man), "keep_as_control": len(keep),
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
    print("保留作对照: %d 条（反恐行动/交战，不计入恐袭统计）" % len(keep))
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
        known = [r["死亡人数"] for r in led if isinstance(r["死亡人数"], int)]
        print("-- 已知死亡数合计：%d（有数值 %d/%d 条，不含保留作对照的 %d 条）"
              % (sum(known), len(known), len(led), len(keep)))
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
