"""判定层：候选行 → 恐怖袭击事件（或排除）。

上一步 build_ledger_rows.py 抽出的是「这篇文章写了什么」。
这一步判的是「这算不算一起恐怖袭击」，并按编码规范填字段。

判定依据见 reference/terrorism_judgement_spec.md。
本脚本只做**机器初判**，输出三档：计入 / 存疑 / 排除。
存疑的一律进人工队列，附「哪条条件没确认」。

为什么判定要和抽取分开
----------------------
判定标准会改。改一次标准就重跑一遍抽取，等于把 15,000 篇正文重新读一遍，
几分钟到几十分钟，而且抽取的中间结果全作废。
分开之后改标准只重跑判定，输入是已经抽好的候选行，几秒钟。

输出
----
--out 判定结果（带判定理由，可审计）
     每行含：19 列 + 序号 + 事件ID + 事件类型 + 判定 + 未确认条件
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ============================================================
# 一、袭击方式：8 类，有优先级
# ============================================================
# 优先级按规范给的顺序：暗杀 > 武装袭击 > 爆炸 > 绑架 > 劫持 > 基础设施 > 非武装 > 不详
#
# 两条细则来自规范原文：
#   · 「利用爆炸进行暗杀」记暗杀（1 优先于 3）
#   · 「爆炸物 + 火器/燃烧物」记武装袭击（2 优先于 3）
# 所以不是简单地「谁先匹配用谁」，而是先判出所有命中的类别，再按优先级取。
ACT_ORDER = ["暗杀", "武装袭击", "爆炸袭击", "绑架", "劫持",
             "攻击基础设施", "非武装袭击", "不详"]

# 暗杀：必须是**特定的、显著的个人**。
# 规范明确：「警察被枪杀，但没有证据表明该警察是被特定选中的高级人物，
# 则不算作暗杀，而应分类为武装袭击」。
# 所以这里要求同时出现「明确的官职/身份」和「针对个人的杀害动作」。
ASSASSINATION_TARGET = re.compile(
    r"(?i)\b(?:governor|mayor|minister|president|prime minister|senator|"
    r"mp\b|member of parliament|lawmaker|legislator|ambassador|consul|"
    r"judge|prosecutor|police chief|army chief|general|colonel|commander|"
    r"chief of staff|spokesman|spokesperson|cleric|imam|priest|bishop|"
    r"journalist|activist|blogger|candidate|councilman|deputy|"
    r"district head|village head|traditional ruler|emir|sheikh)\b")
ASSASSINATION_ACT = re.compile(
    r"(?i)\b(?:assassinat\w*|shot dead|shot and killed|gunned down|"
    r"ambushed and killed|targeted killing|killed by gunmen|"
    r"stabbed to death|murdered)\b|暗杀|刺杀|遇刺")

ARMED_ACT = re.compile(
    r"(?i)\b(?:firearms?|rifles?|pistols?|revolvers?|AK-?47|machine guns?|"
    r"shotguns?|shoot(?:ing|out|s)?|shot|opened fire|fired (?:at|on|upon)|"
    r"sprayed (?:bullets|gunfire)|bullets?|"
    r"machetes?|knife|knives|stab(?:bing|bed|s)?|slash(?:ed|ing)?|"
    r"hack(?:ed|ing)?|axe|arson|set (?:fire|ablaze)|burn(?:ed|t|ing)|"
    r"torch(?:ed)?)\b|"
    r"枪击|开枪|射击|砍杀|刀砍|纵火|焚烧")

BOMBING_ACT = re.compile(
    r"(?i)\b(?:bomb(?:ing|ed|s)?|blast(?:ed|s)?|explos(?:ion|ive|ives|ed)|"
    r"IED\b|improvised explosive|grenade|landmine|land mine|mine\b|"
    r"suicide attack|suicide bombing|car bomb|truck bomb|vbied|"
    r"rocket|mortar|artillery shell|missile strike|detonat\w*)\b|"
    r"爆炸|炸弹|引爆|自杀式")

KIDNAP_ACT = re.compile(
    r"(?i)\b(?:kidnap(?:ped|ping|s)?|abduct(?:ed|ion|ions|ing)?|"
    r"taken hostage|seized|snatched|captured \d+|held captive|"
    r"ransom)\b|绑架|劫持人质|掳走")

# 劫持：公开控制人质、发生在目标地点、时间较短。
# 与绑架的区别是「地点」和「时长」，不是「有没有人质」。
HIJACK_ACT = re.compile(
    r"(?i)\b(?:hijack(?:ed|ing|s)?|hostage (?:crisis|situation|standoff|drama)|"
    r"stormed (?:the )?(?:building|hotel|cafe|restaurant|school|"
    r"hospital|mall|airport)|siege|barricaded|took over|"
    r"held \d+ (?:people )?hostage)\b|劫持|围攻|占领")

INFRA_ACT = re.compile(
    r"(?i)\b(?:pipeline|power (?:grid|plant|station|line)|electricity (?:grid|pylon)|"
    r"railway|rail(?:road)? (?:line|track)|train station|bridge|dam\b|"
    r"telecom(?:munications)? (?:tower|mast)|cell tower|water (?:plant|supply)|"
    r"oil (?:field|refinery|facility)|gas (?:pipeline|facility)|"
    r"government building|municipal building|school building|"
    r"transmission tower|substation)\b|"
    r"基础设施|输油管道|电网|铁路|通信塔")

UNARMED_ACT = re.compile(
    r"(?i)\b(?:beat(?:en|ing)?|bludgeon\w*|fists?|punched|kicked to death|"
    r"stoned to death|clubbed|chemical (?:weapon|agent|attack)|"
    r"poison(?:ed|ing)?|radiological|biological weapon|"
    r"nuclear (?:weapon|material))\b|徒手|殴打|石刑|化学武器")

# 火器/燃烧物 —— 用来实现「爆炸物 + 火器 → 武装袭击」这条细则
FIREARM_OR_BURN = re.compile(
    r"(?i)\b(?:firearms?|rifles?|pistols?|revolvers?|AK-?47|"
    r"shoot(?:ing|out|s)?|shot|opened fire|bullet|"
    r"arson|set (?:fire|ablaze)|burn(?:ed|t|ing)|torch(?:ed)?|"
    r"molotov|incendiary)\b|枪击|开枪|纵火|焚烧")

# 判断有没有人员伤亡 —— 用来区分「炸设施」和「炸人」。
# 规范对「攻击基础设施」的定义是「主要目的是损害设施，
# 而人员伤亡通常是附带的结果」，所以没有伤亡时它是主要目的。
CASUALTY_HINT = re.compile(
    r"(?i)\b(?:kill(?:ed|s|ing)?|dead|death|died|fatalit\w*|"
    r"injur(?:ed|es|y)|wound(?:ed|s)?|casualt\w*|bodies|corpse)\b|"
    r"死亡|受伤|伤亡|丧生|遇难")

# 「no casualties」「without casualties」「no one was hurt」这类是否定式，
# 不能当成「有伤亡」。
# 实测：「Militants bombed the pipeline, no casualties reported」
# 因为 casualties 命中了 CASUALTY_HINT，被判成爆炸袭击，
# 而这句的主要目的明明是毁设施。
NO_CASUALTY = re.compile(
    r"(?i)(?:no|without|zero)\s+(?:\w+\s+){0,2}"
    r"(?:casualt\w*|injur\w*|death|deaths|victims?|fatalit\w*|"
    r"loss of life)"
    r"|(?:did not|didn't|no one was|nobody was)\s+(?:\w+\s+){0,2}"
    r"(?:hurt|injured|killed|wounded)"
    r"|未造成.{0,6}(?:伤亡|死亡|受伤)"
    r"|无人员伤亡")


def pick_act(text):
    """8 类袭击方式，按优先级取。

    返回 (类别, 命中的全部类别)。后者写进备注，方便人工复核时看
    「机器其实还看到了什么」。
    """
    t = text or ""
    hit = {}
    if ASSASSINATION_TARGET.search(t) and ASSASSINATION_ACT.search(t):
        hit["暗杀"] = True
    if ARMED_ACT.search(t):
        hit["武装袭击"] = True
    if BOMBING_ACT.search(t):
        hit["爆炸袭击"] = True
    if KIDNAP_ACT.search(t):
        hit["绑架"] = True
    if HIJACK_ACT.search(t):
        hit["劫持"] = True
    if INFRA_ACT.search(t):
        hit["攻击基础设施"] = True
    if UNARMED_ACT.search(t):
        hit["非武装袭击"] = True

    # 细则一：「爆炸物 + 火器/燃烧物」→ 武装袭击。
    # 实现上是把爆炸从候选里去掉，让它落到武装袭击那一档。
    if hit.get("爆炸袭击") and hit.get("武装袭击") \
            and FIREARM_OR_BURN.search(t):
        del hit["爆炸袭击"]

    # 细则二：设施是主要目标、没有人员伤亡 → 攻击基础设施。
    # 规范对「攻击基础设施」的定义是「主要目的是损害设施或基础设施，
    # 而人员伤亡通常是附带的结果」。所以「炸毁电网、没人死」的性质是
    # 攻击基础设施，不是爆炸袭击 —— 尽管手段是爆炸。
    # 有伤亡时仍按优先级走，因为那时候「对人」才是主要目的。
    if hit.get("攻击基础设施"):
        # 先把否定式整段抹掉，再看还有没有伤亡描述
        t2 = NO_CASUALTY.sub(" ", t)
        if not CASUALTY_HINT.search(t2):
            return "攻击基础设施", sorted(hit)

    for k in ACT_ORDER[:-1]:
        if hit.get(k):
            return k, sorted(hit)
    return "不详", sorted(hit)


# ============================================================
# 二、袭击武器：9 类，多武器全记
# ============================================================
# 与袭击方式不同：这里**不取优先级，全记**。规范原文：
# 「若袭击者明确采用多种武器发动袭击，则需记录所有武器类型。」
WEAPON_PATTERNS = [
    ("生化放核武器", re.compile(
        r"(?i)\b(?:chemical (?:weapons?|agents?|attacks?)|sarin|chlorine gas|"
        r"mustard gas|nerve agents?|biological weapons?|bioweapons?|anthrax|"
        r"pathogens?|radiological|dirty bombs?|nuclear (?:weapons?|materials?)|"
        r"radioactive)\b|化学武器|生物武器|放射性|核武器")),
    ("枪支武器", re.compile(
        r"(?i)\b(?:guns?|firearms?|rifles?|pistols?|revolvers?|handguns?|"
        r"AK-?47s?|machine guns?|shotguns?|snipers?)\b|枪支|步枪|手枪")),
    ("爆炸物武器", re.compile(
        r"(?i)\b(?:bombs?|IEDs?|improvised explosives?|landmines?|land mines?|"
        r"mines?|grenades?|TNT|dynamite|explosive devices?|suicide vests?|"
        r"suicide bombers?|car bombs?|truck bombs?|vbieds?|"
        r"package bombs?|letter bombs?|parcel bombs?|"
        r"mortars?|rockets?|RPGs?|missiles?|artillery shells?|"
        r"remote[- ]controlled (?:bombs?|devices?)|time bombs?|"
        r"explosives?)\b|"
        r"炸弹|爆炸装置|地雷|手榴弹|火箭弹|迫击炮|炸药")),
    ("燃烧武器", re.compile(
        r"(?i)\b(?:molotovs?|incendiary|firebombs?|flamethrowers?|"
        r"petrol bombs?|gasoline|kerosene|burned (?:alive|to death))\b|"
        r"燃烧弹|燃烧瓶|汽油")),
    ("近身攻击武器", re.compile(
        r"(?i)\b(?:machetes?|knife|knives|blades?|daggers?|swords?|"
        r"axes?|hatchets?|clubs?|bats?|sticks?|stones?|rocks?|"
        r"blunt (?:objects?|instruments?)|ropes?|strangul\w*|garrotes?|"
        r"fists?|kicked|punched)\b|"
        r"刀具|砍刀|棍棒|石块|绳索|徒手")),
    ("交通工具袭击", re.compile(
        r"(?i)\b(?:ramm(?:ed|ing)|drove (?:a )?(?:car|truck|van|vehicle) into|"
        r"vehicle[- ]ramming|car ramming|truck attacks?|"
        r"ploughed into|plowed into|mowed down|"
        r"plane crash(?:ed)? into|aircraft (?:was )?flown into)\b|"
        r"驾车冲撞|车辆冲撞")),
    ("新兴袭击武器", re.compile(
        r"(?i)\b(?:drones?|UAVs?|unmanned (?:aerial|air) vehicles?|quadcopters?|"
        r"electromagnetic|laser weapons?|cyberattacks?|cyber attacks?)\b|"
        r"无人机|电磁")),
]


def pick_weapons(text):
    """9 类武器，多武器全记。

    无人机单列一条规则：规范原文最终统一为
    「只要涉及到无人机，均记录为新兴袭击武器」——
    所以命中无人机时，武器里至少有「新兴袭击武器」这一项。

    名词全部写成 `s?` 复数兼容。实测漏例：
    「AK-47 rifles and grenades」只抽出枪支，
    「beat him with sticks and stones」抽出「不详」——
    都是单数正则碰不到复数。
    """
    t = text or ""
    got = []
    for name, rx in WEAPON_PATTERNS:
        if rx.search(t):
            got.append(name)
    # 无人机规则：命中新兴武器时不额外加，避免重复
    if not got:
        got = ["不详"]
    return got


# ============================================================
# 三、五条必选条件 + 两条补充条件
# ============================================================
# 判定主体：非国家行为体
NONSTATE_ACTOR = re.compile(
    r"(?i)\b(?:al-?shabaab|al-?shabab|boko haram|iswap|isw?ap|isis|isil|"
    r"daesh|islamic state|jnim|ansar(?:u|ou)?l|aqap|aqim|taliban|ttp|"
    r"tehrik-i-taliban|bla\b|baloch (?:liberation|separatist)|"
    r"jamaat-ul-ahrar|lashkar|hizbul|adf\b|allied democratic forces|"
    r"rsf\b|rapid support forces|janjaweed|far[cd]\b|eln\b|farc|"
    r"militants?|insurgents?|gunmen|gunman|assailants?|attackers?|"
    r"terrorists?|extremists?|jihadis?ts?|bandits?|armed (?:group|men|gang)|"
    r"separatists?|rebels?|vigilantes?|suicide bomber|bomber|"
    r"unidentified (?:gunmen|assailants|attackers)|unknown gunmen)\b|"
    r"武装分子|恐怖分子|袭击者|枪手|叛乱分子|极端分子")

# 判定主体：国家力量（用它来排除「纯国家军事行动」）
STATE_ACTOR = re.compile(
    r"(?i)\b(?:army|armed forces|military|air ?force|navy|police|"
    r"security forces|soldiers?|troops?|national guard|border guard|"
    r"gendarmerie|peacekeepers?|unmiss|unifil|government forces|"
    r"state forces|paramilitary)\b|政府军|安全部队|军警|警察")

# 政治目的：报道里常不写，写了也常是记者的推断 → 可靠性低，只加分不否决
POLITICAL_MOTIVE = re.compile(
    r"(?i)\b(?:topple|overthrow|regime change|secede|secession|independence|"
    r"separatist|autonomy|self-rule|establish (?:an? )?(?:islamic|caliphate)|"
    r"sharia|jihad|holy war|political (?:motive|goal|aim)|"
    r"in retaliation for|revenge for|protest against|"
    r"demand(?:ing)? (?:the )?release|policy change|"
    r"destabiliz|sow (?:fear|panic)|spread fear|terroriz)\b|"
    r"政治目的|推翻|独立|分裂|建立政权")

# 对象：平民 / 非战斗状态军警
CIVILIAN_TARGET = re.compile(
    r"(?i)\b(?:civilians?|villagers?|residents?|passers?-?by|bystanders?|"
    r"worshippers?|churchgoers?|pilgrims?|students?|pupils?|teachers?|"
    r"market(?:\s*goers)?|shoppers?|commuters?|passengers?|"
    r"journalists?|aid workers?|humanitarian workers?|refugees?|"
    r"displaced (?:people|persons)|women|children|elderly|"
    r"wedding|funeral|worshippers|"
    r"off-?duty (?:soldiers?|police)|retired (?:soldiers?|police)|"
    r"checkpoint|police station|military post|security post|"
    r"base|barracks|convoy|patrol)\b|"
    r"平民|村民|居民|礼拜者|学生|集市|检查站|哨所")

# 补充条件 A：被认定的恐怖组织
# 这一步用 NONSTATE_ACTOR 里的具名组织名单，泛称（militants 等）不算
NAMED_TERROR_ORG = re.compile(
    r"(?i)\b(?:al-?shabaab|al-?shabab|boko haram|iswap|isw?ap|isis|isil|"
    r"daesh|islamic state|jnim|ansar(?:u|ou)?l|aqap|aqim|taliban|ttp|"
    r"tehrik-i-taliban|bla\b|baloch (?:liberation|separatist)|"
    r"jamaat-ul-ahrar|lashkar|hizbul|adf\b|allied democratic forces|"
    r"janjaweed|farc|eln\b)\b")

# 补充条件 B：所在国政府定性
GOVT_DESIGNATION = re.compile(
    r"(?i)(?:"
    r"(?:was|were|has been|have been|is|are)\s+(?:officially\s+)?"
    r"(?:designated|classified|declared|labelled|labeled|branded|"
    r"treated|categorized)\s+(?:as\s+)?(?:an?\s+)?"
    r"(?:act of )?(?:terrorism|terrorist attack|terror attack)"
    r"|"
    r"(?:authorities|police|government|officials|prosecutors?)\s+"
    r"(?:said|say|stated|described|called|termed)\s+(?:it\s+)?"
    r"(?:as\s+)?(?:an?\s+)?(?:act of )?terror"
    r"|"
    r"terror(?:ism)?\s+(?:probe|investigation|case|charges?)\s+"
    r"(?:has been |was |were )?(?:opened|filed|launched|registered)"
    r"|"
    r"认定为恐怖|定性为恐怖|恐怖袭击事件立案|按恐怖主义"
    r")")

# 反例：这些东西出现时，多半不是「一起具体的恐怖袭击事件」
NOT_AN_EVENT = re.compile(
    r"(?i)(?:"
    r"\b(?:analysis|opinion|commentary|editorial|explainer|"
    r"what we know|timeline of|a history of|"
    r"report (?:says|finds|warns)|study (?:says|finds|shows)|"
    r"survey (?:says|finds|shows))\b"
    r"|"
    r"(?:分析|评论|社论|综述|盘点|回顾|深度|解读)"
    r")")

# 反例：军工/装备/军演报道。
# 判定器第一版把这些全算成了恐怖袭击 —— 「U.S. Deploys B-52H
# Stratofortress Bombers」「Boeing to keep AGM-86B nuclear cruise
# missiles combat-ready」都进了计入，因为文里有 strike / missile / attack。
# 这类不是事件报道，是装备新闻。
HARDWARE_NEWS = re.compile(
    r"(?i)(?:"
    r"\b(?:deploys?|deployed|deployment|commission(?:ed|ing)?|"
    r"refuel(?:ing)?|hot-pit|sortie|aircraft carrier|destroyer|frigate|"
    r"submarine|fighter jets?|bombers?|stealth|"
    r"combat-ready|readiness|deterrence|warhead|ballistic missile|"
    r"cruise missile|air defense system|missile defense|"
    r"arms (?:deal|sale|contract)|defense contract|procurement|"
    r"manufactur(?:e|er|ing)|to (?:keep|build|upgrade|deliver))\b"
    r")")

# 反例：网络安全报道。
# 「Both critical PHP flaws land on the agency's exploited list」被判成
# 恐怖袭击 —— 因为文里有 attackers / exploit / attack。
# 网络攻击不是规范定义的「暴力或以暴力相威胁」。
CYBER_NEWS = re.compile(
    r"(?i)\b(?:php|vulnerabilit\w*|exploit(?:ed|s)?|malware|ransomware|"
    r"phishing|zero-?day|patch(?:ed|es)?|CVE-\d|infosec|cybersecurity|"
    r"data breach|credential|firewall|botnet|spyware|"
    r"hackers? (?:are|were|have)|account compromise)\b")

# 硬门槛：必须有明确的伤亡或实体破坏。
# 「attack」「strike」「bomb」这些词在非恐袭语境里到处都是，
# 只靠它们判「使用暴力」，760 条「计入」里多数是噪音。
# 要求出现具体的死伤或毁损描述。
HARD_HARM = re.compile(
    r"(?i)(?:"
    r"\b\d{1,5}(?:,\d{3})*\s+(?:people\s+|civilians?\s+|soldiers?\s+|"
    r"persons?\s+)?(?:were\s+)?(?:killed|dead|died|slain|injured|"
    r"wounded|hurt)\b"
    r"|"
    r"\b(?:killed|killed and injured|left \d+ dead|death toll|"
    r"bodies (?:were )?(?:found|recovered)|massacre|slaughter|"
    r"behead\w*|executed|burned alive|shot dead|gunned down|"
    r"abducted|kidnapped|taken hostage|held hostage|hostage)\b"
    r"|"
    r"\b(?:was|were) (?:killed|injured|wounded|abducted|kidnapped|"
    r"shot|stabbed|beheaded)\b"
    r"|"
    r"\b(?:destroyed|damaged|razed|torched|set ablaze|blown up|"
    r"bombed|shelled)\b.{0,30}\b(?:building|market|mosque|church|"
    r"school|hospital|home|house|shop|vehicle|bridge|tower)\b"
    r"|"
    r"(?:死亡|受伤|丧生|遇难|身亡|遇害)\s*\d*\s*人?"
    r"|"
    r"\d+\s*(?:人|名)[^。，,]{0,6}(?:死亡|受伤|丧生|遇难|被绑|被劫)"
    r")")

# 泛称必须收紧：单个 "attackers" / "militants" 出现就判非国家主体，
# 会让任何提到「袭击者」的文章都过关。要求它和具体暴力动作同现。
VAGUE_ACTOR_ONLY = re.compile(
    r"(?i)^(?:militants?|insurgents?|gunmen|gunman|assailants?|"
    r"attackers?|terrorists?|extremists?|unknown gunmen)$")

# 反例：国家对自己境内武装的纯军事行动，不算恐怖袭击
# （但国家力量作为**受害方**时不排除 —— 所以这条只在
#   施害方=国家、受害方=武装分子 时才生效，见 judge()）
STATE_OFFENSIVE = re.compile(
    r"(?i)\b(?:air ?strikes?|airstrike|drone strike|shelling|artillery|"
    r"offensive|operation|raid|sweep|clearance operation|"
    r"military operation|troops killed|soldiers killed|"
    r"security forces killed|eliminated|neutralized)\b|"
    r"空袭|军事行动|清剿")


def build_text_index(store):
    """URL → 正文。判定必须看全文，不能看摘要。

    实测教训：第一版用「事件摘要 + 备注 + 袭击者」判定，
    22 条计入里 17 条袭击方式是「不详」、21 条武器「不详」——
    因为摘要只有一句话，写不下武器型号。
    判定看全文，摘要只是给人看的。
    """
    import glob
    import gzip
    import json
    import build_ledger_rows as B

    idx = {}
    files = glob.glob(os.path.join(store, "raw", "meta", "*", "*.json"))
    for p in files:
        try:
            m = json.load(io.open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") != "article":
            continue
        url = (m.get("url") or "").strip()
        if not url:
            continue
        v = m.get("versions") or []
        if not v:
            continue
        fp = os.path.join(store, v[-1]["file"].replace("/", os.sep))
        try:
            raw = gzip.open(fp, "rb").read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        body = B.html_to_body(raw)
        if body:
            idx[url] = body
    return idx


def judge(row, fulltext=None):
    """一行候选 → (判定, 事件类型, 未确认条件, 判定理由)。

    判定三档：
      计入 —— 五条必选全满足，或补充条件成立
      存疑 —— 部分条件机器确认不了，进人工队列
      排除 —— 明确不满足某条必选

    条件 2（主观故意）和条件 4（政治目的）机器**永远判不了**，
    所以「计入」不是「机器确认是恐怖袭击」，而是
    「机器没找到排除理由，且要件齐全」。真正的定案在人工。

    fulltext 给了就用全文判，没给才退回摘要。
    """
    if fulltext:
        t = fulltext
    else:
        t = " ".join(str(row.get(k) or "")
                     for k in ("事件摘要", "备注", "袭击者"))

    country = row.get("国家") or ""
    deaths = row.get("死亡人数") or ""
    act = row.get("袭击方式") or ""

    # ---- 排除一：明确不是事件报道 ----
    if NOT_AN_EVENT.search(t):
        return "排除", "5", "", "评论/分析/综述类，不是事件报道"

    # ---- 排除二：军工/装备/军演报道 ----
    if HARDWARE_NEWS.search(t):
        return "排除", "5", "", "军工/装备/军演报道，不是事件"

    # ---- 排除三：网络安全报道 ----
    if CYBER_NEWS.search(t):
        return "排除", "5", "", "网络安全事件，不属于暴力行为"

    # ---- 排除四：没有明确的伤亡或实体破坏 ----
    # 这是最要紧的一道。不加它，「attack/strike/bomb」这些词会把
    # 军工新闻、政策评论、装备采购全放进来。
    if not HARD_HARM.search(t):
        return "排除", "5", "", "未见明确伤亡或实体破坏描述，不构成暴力事件"

    # ---- 排除五：无暴力行为 ----
    if act in ("", "不详") and not pick_act(t)[1]:
        return "排除", "5", "", "未识别到暴力行为"

    has_nonstate = bool(NONSTATE_ACTOR.search(t))
    has_state = bool(STATE_ACTOR.search(t))
    has_civilian = bool(CIVILIAN_TARGET.search(t))
    has_motive = bool(POLITICAL_MOTIVE.search(t))
    named_org = bool(NAMED_TERROR_ORG.search(t))
    govt_says = bool(GOVT_DESIGNATION.search(t))

    # ---- 排除三：国家力量的纯军事行动 ----
    # 施害方=国家、且没有非国家主体、也没有平民受害 → 不是恐怖袭击。
    # 注意顺序：先看有没有非国家主体，有就不排除（国家可能是受害方）。
    if has_state and not has_nonstate and not has_civilian:
        if STATE_OFFENSIVE.search(t):
            return "排除", "5", "", "国家军事行动，施害方为国家力量"

    # ---- 条件逐条记 ----
    unconfirmed = []
    if not has_nonstate:
        unconfirmed.append("条件1施害方未确认为非国家行为体")
    if not has_civilian:
        unconfirmed.append("条件5对象未确认为平民/非战斗军警")
    # 条件 2 和 4 机器判不了，但文本有证据时可以记「有证据」
    if not has_motive:
        unconfirmed.append("条件4政治目的未见文本依据")
    unconfirmed.append("条件2主观故意需人工判断")

    # ---- 补充条件 ----
    if govt_says:
        return ("计入", "1", "；".join(unconfirmed),
                "依所在国政府定性（补充条件B）")
    if named_org and has_nonstate:
        # 具名组织 + 非国家主体 → 补充条件 A 成立
        if has_civilian:
            return ("计入", "1", "；".join(unconfirmed),
                    "施害方为具名认定的恐怖组织（补充条件A），对象为平民/非战斗军警")
        return ("存疑", "2", "；".join(unconfirmed),
                "施害方为具名恐怖组织，但对象是否为平民/非战斗状态待确认")

    # ---- 五条必选：机器只能确认 1、3、5 ----
    if has_nonstate and has_civilian:
        return ("计入", "1", "；".join(unconfirmed),
                "施害方为非国家行为体，对象为平民/非战斗军警，含暴力行为")

    if has_nonstate and not has_civilian:
        # 有非国家主体、有暴力，但对象不明 → 可能是武装组织之间交战
        if re.search(r"(?i)\b(?:clashes?|fighting|battle|gunbattle|"
                     r"firefight)\b.{0,40}\b(?:between|against)\b", t):
            return "排除", "4", "", "武装组织之间交战"
        return "存疑", "3", "；".join(unconfirmed), \
            "施害方为非国家行为体，但袭击对象无法确认"

    return "存疑", "3", "；".join(unconfirmed), "施害方身份不明"


# ============================================================
# 四、序号与事件 ID
# ============================================================
def assign_ids(rows):
    """按国家+日期排定当天顺序，生成序号（10 位）和事件 ID（12 位）。

    排序键用（日期, 摘要）而不是文件顺序 —— 否则同一批数据
    换一次输入顺序，ID 就全变了，没法引用。
    """
    from collections import defaultdict

    buckets = defaultdict(list)
    for r in rows:
        y, m, d = str(r.get("年份") or ""), str(r.get("月份") or ""), \
            str(r.get("日期") or "")
        if y and m and d:
            key = "%04d%02d%02d" % (int(y), int(m), int(d))
        else:
            key = "00000000"
        buckets[key].append(r)

    for day, items in buckets.items():
        items.sort(key=lambda x: (str(x.get("事件摘要") or ""),
                                  str(x.get("信息来源URL") or "")))
        for i, r in enumerate(items, 1):
            if day == "00000000":
                r["序号"] = ""
                r["事件ID"] = ""
            else:
                r["序号"] = "%s%02d" % (day, i)
                r["事件ID"] = "%s%04d" % (day, i)
    return rows


# ============================================================
# 五、CLI
# ============================================================
OUT_COLS_EXTRA = ["序号", "事件ID", "事件类型", "判定", "未确认条件",
                  "判定理由", "袭击方式_全部命中", "袭击武器_全部"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True,
                    help="build_ledger_rows.py 的输出")
    ap.add_argument("--store", default="",
                    help="数据目录；给了就按 URL 取全文判定（强烈建议）")
    ap.add_argument("--out", default="")
    ap.add_argument("--only-included", action="store_true",
                    help="只输出判定=计入的行")
    a = ap.parse_args()

    rows = list(csv.DictReader(io.open(a.inp, encoding="utf-8-sig")))
    print("")
    print("  读入候选 %d 行" % len(rows))

    text_idx = {}
    if a.store:
        text_idx = build_text_index(a.store)
        print("  建立全文索引 %d 篇" % len(text_idx))

    out = []
    cnt = Counter()
    type_cnt = Counter()
    n_no_full = 0
    for r in rows:
        full = text_idx.get((r.get("信息来源URL") or "").strip())
        if a.store and not full:
            n_no_full += 1
        verdict, etype, unconf, reason = judge(r, full)
        # 用规范口径重算袭击方式和武器 —— 抽取层给的是旧分类
        base = full or " ".join(str(r.get(k) or "")
                                for k in ("事件摘要", "备注", "袭击者"))
        act8, act_all = pick_act(base)
        wpn = pick_weapons(base)
        nr = dict(r)
        nr["事件类型"] = etype
        nr["判定"] = verdict
        nr["未确认条件"] = unconf
        nr["判定理由"] = reason
        nr["袭击方式"] = act8
        nr["袭击方式_全部命中"] = "|".join(act_all) if act_all else ""
        nr["袭击武器"] = "|".join(wpn)
        nr["袭击武器_全部"] = "|".join(wpn)
        cnt[verdict] += 1
        if verdict != "排除":
            type_cnt[etype] += 1
        out.append(nr)

    if a.store:
        print("  未取到全文 %d 行（退回摘要判定）" % n_no_full)

    out = assign_ids(out)

    if a.only_included:
        out = [r for r in out if r["判定"] == "计入"]

    print("")
    print("  判定分布：%s" % dict(cnt))
    print("  事件类型（非排除）：%s" % dict(type_cnt))
    print("")

    if out:
        inc = [r for r in out if r["判定"] == "计入"]
        print("  计入 %d 条，其中" % len(inc))
        print("    有日期 %d ｜ 有国家 %d ｜ 有死亡数 %d ｜ 有组织 %d" % (
            sum(1 for r in inc if r.get("年份")),
            sum(1 for r in inc if r.get("国家")),
            sum(1 for r in inc if r.get("死亡人数")),
            sum(1 for r in inc if r.get("袭击者"))))
        print("  国家前 10：%s" % dict(
            Counter(r.get("国家") or "(未识别)" for r in inc).most_common(10)))
        print("  袭击方式：%s" % dict(
            Counter(r.get("袭击方式") or "(空)" for r in inc).most_common()))
        print("  袭击武器：%s" % dict(
            Counter(w for r in inc for w in (r.get("袭击武器") or "").split("|")
                    if w).most_common(8)))

    if a.out:
        cols = list(rows[0].keys()) if rows else []
        cols = cols + [c for c in OUT_COLS_EXTRA if c not in cols]
        with io.open(a.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(out)
        print("")
        print("  → %s（%d 行 × %d 列）" % (a.out, len(out), len(cols)))


if __name__ == "__main__":
    main()
