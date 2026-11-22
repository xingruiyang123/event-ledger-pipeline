"""按《判定与编码规范》给事件分类：袭击方式 8 类、袭击武器 9 类、序号。

这是 judge_events.py 的**辅助模块**，不是替代品。
judge_events.py 负责判「算不算恐怖袭击」（判定链 M0/N1-N6/P1-P7），
本模块只负责「判进来之后，袭击方式和武器怎么填」。

为什么单独拆出来
----------------
判定链要改口径，分类表也要改口径，但两者改动节奏完全不同 ——
判定链跟着事件定义走，分类表跟着武器清单走。
写在一起，改一个动另一个，回归测试全得重跑。

## 袭击方式：8 类，**有优先级**
规范原文：「当一个事件包含多个袭击方式时，通常会选择最合适的类别，
优先级按照上面 8 个类别的先后顺序决定。」

  暗杀 > 武装袭击 > 爆炸袭击 > 绑架 > 劫持 > 攻击基础设施 > 非武装袭击 > 不详

规范还给了两条细则，实现上不能靠「谁先匹配用谁」：
  · 「利用爆炸进行暗杀」记暗杀（1 优先于 3）
  · 「爆炸物 + 火器/燃烧物」记武装袭击（2 优先于 3）
所以先判出所有命中的类别，再按优先级取。

## 袭击武器：9 类，**多武器全记**
规范原文：「若袭击者明确采用多种武器发动袭击，则需记录所有武器类型。」
这一点与袭击方式相反，不能取优先级。

无人机特例：规范最终统一为「只要涉及到无人机，均记录为新兴袭击武器」。
"""
import re

# ============================================================
# 袭击方式
# ============================================================
ACT_ORDER = ["暗杀", "武装袭击", "爆炸袭击", "绑架", "劫持",
             "攻击基础设施", "非武装袭击", "不详"]

# 暗杀：必须是**特定的、显著的个人**。
# 规范原文：「若警察被枪杀，但没有证据表明该警察是被特定选中的高级人物，
# 则不算作暗杀，而应分类为武装袭击」。
# 所以要求「明确的官职/身份」和「针对个人的杀害动作」同时出现。
_ASSASSINATION_TARGET = re.compile(
    r"(?i)\b(?:governor|mayor|minister|president|prime minister|senator|"
    r"mp\b|member of parliament|lawmaker|legislator|ambassador|consul|"
    r"judge|prosecutor|police chief|army chief|general|colonel|commander|"
    r"chief of staff|spokesman|spokesperson|cleric|imam|priest|bishop|"
    r"journalist|activist|blogger|candidate|councilman|deputy|"
    r"district head|village head|traditional ruler|emir|sheikh)\b")
_ASSASSINATION_ACT = re.compile(
    r"(?i)\b(?:assassinat\w*|shot dead|shot and killed|gunned down|"
    r"ambushed and killed|targeted killing|killed by gunmen|"
    r"stabbed to death|murdered)\b|暗杀|刺杀|遇刺")

# 武装袭击：只认**动作**，不认身份词。
# 「gunmen」几乎出现在每条袭击报道里，用它判「武装袭击」会吞掉
# 绑架、劫持、爆炸 —— 实测「Gunmen stormed a hotel and held 200 people
# hostage」被判成武装袭击，而它的主要目的是劫持。
_ARMED = re.compile(
    r"(?i)\b(?:firearms?|rifles?|pistols?|revolvers?|AK-?47|machine guns?|"
    r"shotguns?|shoot(?:ing|out|s)?|shot|opened fire|fired (?:at|on|upon)|"
    r"sprayed (?:bullets|gunfire)|bullets?|"
    r"machetes?|knife|knives|stab(?:bing|bed|s)?|slash(?:ed|ing)?|"
    r"hack(?:ed|ing)?|axe|arson|set (?:fire|ablaze)|burn(?:ed|t|ing)|"
    r"torch(?:ed)?)\b|枪击|开枪|射击|砍杀|刀砍|纵火|焚烧")

_BOMBING = re.compile(
    r"(?i)\b(?:bombs?|bombed|bombing|blasts?|blasted|explos(?:ion|ive|ives|ed)|"
    r"IEDs?|improvised explosives?|grenades?|landmines?|land mines?|"
    r"suicide attacks?|suicide bombings?|car bombs?|truck bombs?|vbieds?|"
    r"rockets?|mortars?|artillery shells?|missiles?|detonat\w*)\b|"
    r"爆炸|炸弹|引爆|自杀式")

_KIDNAP = re.compile(
    r"(?i)\b(?:kidnap(?:ped|ping|s)?|abduct(?:ed|ion|ions|ing)?|"
    r"taken hostage|seized|snatched|captured \d+|held captive|ransom)\b|"
    r"绑架|劫持人质|掳走")

# 劫持：公开控制人质、发生在目标地点、时间较短。
# 与绑架的区别是「地点」和「时长」，不是「有没有人质」。
_HIJACK = re.compile(
    r"(?i)\b(?:hijack(?:ed|ing|s)?|hostage (?:crisis|situation|standoff|drama)|"
    r"stormed (?:the )?(?:building|hotel|cafe|restaurant|school|"
    r"hospital|mall|airport)|siege|barricaded|took over|"
    r"held \d+ (?:people )?hostage)\b|劫持|围攻|占领")

_INFRA = re.compile(
    r"(?i)\b(?:pipelines?|power (?:grid|plant|station|line)|"
    r"electricity (?:grid|pylon)|railways?|rail(?:road)? (?:line|track)|"
    r"train stations?|bridges?|dams?\b|"
    r"telecom(?:munications)? (?:tower|mast)|cell towers?|"
    r"water (?:plant|supply)|oil (?:field|refinery|facility)|"
    r"gas (?:pipeline|facility)|government building|municipal building|"
    r"school building|transmission towers?|substations?)\b|"
    r"基础设施|输油管道|电网|铁路|通信塔")

_UNARMED = re.compile(
    r"(?i)\b(?:beat(?:en|ing)?|bludgeon\w*|fists?|punched|kicked to death|"
    r"stoned to death|clubbed|chemical (?:weapons?|agents?|attacks?)|"
    r"poison(?:ed|ing)?|radiological|biological weapons?|"
    r"nuclear (?:weapons?|materials?))\b|徒手|殴打|石刑|化学武器")

# 火器/燃烧物 —— 专用于细则「爆炸物 + 火器 → 武装袭击」
_FIREARM_OR_BURN = re.compile(
    r"(?i)\b(?:firearms?|rifles?|pistols?|revolvers?|AK-?47|"
    r"shoot(?:ing|out|s)?|shot|opened fire|bullets?|"
    r"arson|set (?:fire|ablaze)|burn(?:ed|t|ing)|torch(?:ed)?|"
    r"molotov|incendiary)\b|枪击|开枪|纵火|焚烧")

_CASUALTY = re.compile(
    r"(?i)\b(?:kill(?:ed|s|ing)?|dead|death|died|fatalit\w*|"
    r"injur(?:ed|es|y)|wound(?:ed|s)?|casualt\w*|bodies|corpse)\b|"
    r"死亡|受伤|伤亡|丧生|遇难")

# 「no casualties」「without casualties」是否定式，不能当成有伤亡。
# 实测：「Militants bombed the pipeline, no casualties reported」
# 因为 casualties 命中 _CASUALTY，被判成爆炸袭击，
# 而这句的主要目的明明是毁设施。
_NO_CASUALTY = re.compile(
    r"(?i)(?:no|without|zero)\s+(?:\w+\s+){0,2}"
    r"(?:casualt\w*|injur\w*|deaths?|victims?|fatalit\w*|loss of life)"
    r"|(?:did not|didn't|no one was|nobody was)\s+(?:\w+\s+){0,2}"
    r"(?:hurt|injured|killed|wounded)"
    r"|未造成.{0,6}(?:伤亡|死亡|受伤)"
    r"|无人员伤亡")


def pick_act(text):
    """8 类袭击方式，按规范优先级取。

    返回 (类别, 命中的全部类别)。
    后者写进备注，方便人工复核时看「机器其实还看到了什么」。
    """
    t = text or ""
    hit = {}
    if _ASSASSINATION_TARGET.search(t) and _ASSASSINATION_ACT.search(t):
        hit["暗杀"] = True
    if _ARMED.search(t):
        hit["武装袭击"] = True
    if _BOMBING.search(t):
        hit["爆炸袭击"] = True
    if _KIDNAP.search(t):
        hit["绑架"] = True
    if _HIJACK.search(t):
        hit["劫持"] = True
    if _INFRA.search(t):
        hit["攻击基础设施"] = True
    if _UNARMED.search(t):
        hit["非武装袭击"] = True

    # 细则一：爆炸物 + 火器/燃烧物 → 武装袭击。
    # 把爆炸从候选里去掉，让它落到武装袭击那一档。
    if hit.get("爆炸袭击") and hit.get("武装袭击") \
            and _FIREARM_OR_BURN.search(t):
        del hit["爆炸袭击"]

    # 细则二：设施是主要目标、没有人员伤亡 → 攻击基础设施。
    # 这不是优先级规则，是「手段 vs 目的」的区分。规范对
    # 「攻击基础设施」的定义是「主要目的是损害设施，人员伤亡是附带结果」，
    # 所以「炸毁电网、没人死」的性质是攻击基础设施，不是爆炸袭击。
    if hit.get("攻击基础设施"):
        if not _CASUALTY.search(_NO_CASUALTY.sub(" ", t)):
            return "攻击基础设施", sorted(hit)

    for k in ACT_ORDER[:-1]:
        if hit.get(k):
            return k, sorted(hit)
    return "不详", sorted(hit)


# ============================================================
# 袭击武器
# ============================================================
# 名词全部写成 s? 复数兼容。实测漏例：
# 「AK-47 rifles and grenades」只抽出枪支 → grenade 的 \b 碰不到 grenades；
# 「beat him with sticks and stones」抽出「不详」→ 同样原因。
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

WEAPON_ORDER = ["生化放核武器", "枪支武器", "爆炸物武器", "燃烧武器",
                "近身攻击武器", "交通工具袭击", "新兴袭击武器",
                "其它类型武器", "不详"]


def pick_weapons(text):
    """9 类武器，**多武器全记**，不取优先级。

    规范：「若袭击者明确采用多种武器发动袭击，则需记录所有武器类型。」
    这与袭击方式相反。

    无人机按规范最终统一口径处理：「只要涉及到无人机，
    均记录为新兴袭击武器」。命中无人机时武器里至少有这一项。
    """
    t = text or ""
    got = [name for name, rx in WEAPON_PATTERNS if rx.search(t)]
    return got or ["不详"]


def weapons_str(text):
    """给 19 列「袭击武器」列的字符串：多武器用顿号分隔。"""
    return "、".join(pick_weapons(text))


# ============================================================
# 序号 / 事件 ID
# ============================================================
def day_key(row):
    """从 19 列的一行取出 yyyymmdd。日期不全时返回空串。"""
    y = str(row.get("年份") or "").strip()
    m = str(row.get("月份") or "").strip()
    d = str(row.get("日期") or "").strip()
    if not (y and m and d):
        return ""
    try:
        yi, mi, di = int(y), int(m), int(d)
    except Exception:  # noqa: BLE001
        return ""
    if not (1900 <= yi <= 2100 and 1 <= mi <= 12 and 1 <= di <= 31):
        return ""
    return "%04d%02d%02d" % (yi, mi, di)


def assign_ids(rows):
    """生成序号（10 位）和事件 ID（12 位）。

    同一天第 1、2、3 起事件依次为 ...01、...02、...03。
    排序键用（摘要, URL）而不是输入顺序 —— 否则换一次输入顺序，
    ID 全变了，没法在论文里引用。「事件 ID 稳定」比「顺序好看」重要。

    日期不全的行不给 ID：规范要求前 8 位是日期，
    编不出来就不编，留空等人工补。
    """
    from collections import defaultdict

    buckets = defaultdict(list)
    for r in rows:
        buckets[day_key(r)].append(r)

    for day, items in buckets.items():
        items.sort(key=lambda x: (str(x.get("事件摘要") or ""),
                                  str(x.get("信息来源URL") or "")))
        for i, r in enumerate(items, 1):
            if not day:
                r["序号"] = ""
                r["事件ID"] = ""
            else:
                r["序号"] = "%s%02d" % (day, i)
                r["事件ID"] = "%s%04d" % (day, i)
    return rows


# ============================================================
# 自测
# ============================================================
_ACT_CASES = [
    ("Gunmen assassinated the governor of Kandahar, shooting him dead", "暗杀"),
    ("Four gunmen opened fire on a village market, killing 12", "武装袭击"),
    ("A suicide bombing at a mosque killed 45 worshippers", "爆炸袭击"),
    ("Militants kidnapped 30 schoolchildren and demanded ransom", "绑架"),
    ("Gunmen stormed a hotel and held 200 people hostage", "劫持"),
    ("Explosives destroyed the main power grid and railway line", "攻击基础设施"),
    ("The mob beat him to death with stones and fists", "非武装袭击"),
    ("Bomb planted to assassinate the minister, who was shot dead", "暗杀"),
    ("Attackers used explosives and firearms during the raid", "武装袭击"),
    ("A bombing killed 20 people", "爆炸袭击"),
    ("Police officer was shot dead by gunmen at a checkpoint", "武装袭击"),
    ("Militants bombed the pipeline, no casualties reported", "攻击基础设施"),
    ("Militants bombed a market, killing 12", "爆炸袭击"),
    ("Blast at the railway station wounded 8 passengers", "爆炸袭击"),
]

_WEAPON_CASES = [
    ("Militants used AK-47 rifles and grenades", {"枪支武器", "爆炸物武器"}),
    ("A drone dropped explosives on the base", {"新兴袭击武器", "爆炸物武器"}),
    ("Attacker drove a truck into the crowd", {"交通工具袭击"}),
    ("Suicide bomber with a vest", {"爆炸物武器"}),
    ("They beat him with sticks and stones", {"近身攻击武器"}),
    ("A car bomb exploded near the market", {"爆炸物武器"}),
    ("Gunmen with machetes attacked villagers", {"近身攻击武器"}),
    ("They used a Molotov cocktail", {"燃烧武器"}),
    ("Unknown attackers", {"不详"}),
]


def _selftest():
    bad = 0
    for t, exp in _ACT_CASES:
        got, _ = pick_act(t)
        if got != exp:
            bad += 1
            print("  [act] 错 got=%s want=%s  %s" % (got, exp, t[:50]))
    for t, exp in _WEAPON_CASES:
        got = set(pick_weapons(t))
        if got != exp:
            bad += 1
            print("  [weapon] 错 got=%s want=%s  %s" % (got, exp, t[:50]))
    print("  袭击方式 %d 例 / 武器 %d 例，错 %d"
          % (len(_ACT_CASES), len(_WEAPON_CASES), bad))
    return bad


if __name__ == "__main__":
    raise SystemExit(0 if _selftest() == 0 else 2)
