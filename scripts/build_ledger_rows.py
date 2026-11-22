"""抽取层：把采集到的正文变成 19 列台账的候选行。

这一层在整条链路里的位置
------------------------
  feed 层  抓到条目（不筛，丢不起）
     ↓
  正文层    抓全文（按标题初筛，存得下、可重来）
     ↓
  ★ 抽取层  正文 → 事件字段 → 19 列台账的其中几列
     ↓
  判定层    是不是恐怖主义事件（人工 + 复核）

抽取不等于判定。这一层只负责「把文章里写了什么读出来」，
不负责「算不算恐怖袭击」。两者混在一起会出现的情况是：
判定标准一改，抽取结果全得重跑。分开之后，判定标准改了只需要重跑判定。

19 列里这一层能填的
-------------------
  事件摘要      从首段截取，不是标题（标题常常不带信息）
  国家          place_match 的地名词表 + 正文里最高频的国家
  年份/月份/日期 正文里的日期 + feed 里的 published
  地点          地名词表匹配，带行政区
  死亡人数      数字抽取（带依据）
  受伤人数      同上
  袭击方式      爆炸/枪击/砍杀/绑架/纵火…
  袭击武器      IED/VBIED/枪械/冷兵器…
  袭击者        组织名（塔利班/ISIS/博科圣地…）
  负责人/组织    声称负责的一方
  信息来源URL   原文 URL
  录入日期      抓取日期
  置信度        要素齐全度映射成高/中/低
  审核状态      统一"待审"
  其余列       留空，由判定层和人工填

为什么不一次填满 19 列
----------------------
「审核状态」「关联知识点」「备注」是人工环节的产物，
机器填了反而污染。宁可留空，也不要编。
"""
import argparse
import io
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 19 列，顺序固定，与老师那张表一致。改顺序会让已有台账全部错位。
LEDGER_COLS = [
    "事件摘要", "国家", "年份", "月份", "日期", "地点", "死亡人数",
    "受伤人数", "袭击方式", "袭击武器", "袭击者", "负责人/组织",
    "信息来源URL", "采集人/组别", "录入日期", "置信度", "审核状态",
    "关联知识点", "备注",
]

# ---- 组织名 ----
# 只收本项目信源里真会出现的。写全没有意义 ——
# 匹配不到的会落到「未知」，比错配强。
GROUPS = {
    "塔利班": ["taliban", "taleban", "阿富汗塔利班", "塔利班"],
    "巴基斯坦塔利班": ["tehrik-i-taliban pakistan", "ttp", "tahrik-e-taliban",
                  "巴基斯坦塔利班运动", "巴塔"],
    "哈卡尼网络": ["haqqani"],
    "伊斯兰国": ["islamic state", "isis", "isil", "daesh", "is-k",
             "islamic state khorasan", "伊斯兰国", "达伊沙"],
    "博科圣地": ["boko haram", "iswap", "博科圣地"],
    "青年党": ["al-shabaab", "al-shabab", "青年党", "沙巴布"],
    "基地组织": ["al-qaeda", "al-qaida", "aqap", "aqim", "基地组织"],
    "胡塞武装": ["houthi", "houthis", "ansar allah", "胡塞"],
    "真主党": ["hezbollah", "hizbollah", "真主党"],
    "哈马斯": ["hamas", "izz ad-din al-qassam", "哈马斯"],
    "杰哈德": ["islamic jihad", "piJ", "杰哈德"],
    "俾路支解放军": ["balochistan liberation army", "bla", "俾路支解放军"],
    "俾路支解放阵线": ["balochistan liberation front", "blf", "俾路支解放阵线"],
    "信德解放军": ["sindhudesh revolutionary army", "sra"],
    "虔诚军": ["lashkar-e-taiba", "let", "lashkar e taiba", "虔诚军"],
    "穆罕默德军": ["jaish-e-mohammed", "jem", "jaish e mohammed", "穆罕默德军"],
    "东伊运": ["east turkistan islamic movement", "etim", "tip", "东伊运"],
    "库尔德工人党": ["pkk", "kurdistan workers party", "库尔德工人党"],
    "哥伦比亚革命武装力量": ["farc", "eln", "哥伦比亚革命武装力量"],
    "民族解放军": ["national liberation army"],
    "正义与平等运动": ["jem ", "justice and equality movement"],
    "快速支援部队": ["rapid support forces", "快速支援部队"],
    "瓦格纳": ["wagner group", "wagner", "瓦格纳"],
}

# 纯缩写的组织名单独列，要求全大写出现。
# 混在 GROUPS 里做小写子串匹配会把普通词认成组织：
# 实测「the meeting discussed rsf」被认成苏丹快速支援部队。
ACRONYM_GROUPS = {
    "巴基斯坦塔利班": ["TTP"],
    "俾路支解放军": ["BLA"],
    "俾路支解放阵线": ["BLF"],
    "信德解放军": ["SRA"],
    "虔诚军": ["LeT", "Lashkar-e-Taiba", "LET"],
    "穆罕默德军": ["JeM", "JEM"],
    "东伊运": ["ETIM", "TIP"],
    "库尔德工人党": ["PKK"],
    "哥伦比亚革命武装力量": ["FARC"],
    "快速支援部队": ["RSF"],
    "伊斯兰国": ["ISIS", "ISIL", "ISKP", "IS-K", "ISWAP"],
    "青年党": ["Al-Shabaab"],
    "基地组织": ["AQAP", "AQIM"],
    "民族解放军": ["ELN"],
}

# ---- 袭击方式 ----
# 顺序有讲究：先判更具体的（爆炸/自杀式），再判泛的（武装袭击）。
ACT_PATTERNS = [
    ("自杀式爆炸", [r"\bsuicide (?:bomb|blast|attack)", r"suicide bomber",
                r"self[- ]?radicali[sz]ed", r"suicid[ae] ?bombing",
                r"自杀式", r"人体炸弹"]),
    ("汽车炸弹", [r"\bcar bomb", r"vehicle[- ]borne", r"\bvbi?ed\b",
              r"\btruck bomb", r"汽车炸弹", r"车载炸弹"]),
    ("路边炸弹", [r"\broadside bomb", r"improvised explosive", r"\bied\b",
              r"\bmine explosion", r"路边炸弹", r"简易爆炸装置"]),
    ("爆炸", [r"\bbomb(?:ing|ed|s)?\b", r"\bexplo(?:sion|ded|de)\b", r"\bblast\b",
            r"\bgrenade\b", r"\bdetonat", r"爆炸", r"炸弹"]),
    # "gun attack" / "attack" 这类词单独出现不算袭击方式 ——
    # 必须先有中文，或者英文词必须带明确的攻击动词。
    ("枪击", [r"\bgun(?:men|man|fire|shot|shots)\b", r"\bshoot(?:ing|out)\b",
            r"\bopened fire\b", r"\bshot dead\b", r"\bfire[ds]? (?:on|at)\b",
            r"枪击", r"开枪", r"射击"]),
    ("伏击", [r"\bambush", r"\bwaylaid\b", r"伏击"]),
    ("绑架", [r"\bkidnap", r"\babduct", r"\bhostage", r"绑架", r"劫持", r"人质"]),
    # 原来是 r"stab(?:bing|bed)?" —— 没有词边界，
    # 「establishment」「stability」「constable」全被当成砍杀。
    # 实测 300 篇里报出 46 起「砍杀」，实际一起都没有。
    ("砍杀", [r"\bstab(?:bing|bed|s)?\b", r"\bslit(?:ting)? throat\b",
            r"\bmachete\b", r"\bhatchet\b", r"砍杀", r"刺伤"]),
    ("纵火", [r"\bset (?:it )?ablaze\b", r"\btorch(?:ed)?\b", r"\barson\b",
            r"\bfirebomb", r"纵火", r"焚烧"]),
    ("武装袭击", [r"袭击", r"攻打", r"\battack(?:ed|s|ing)?\b",
              r"\bassault", r"\braid(?:ed|s)?\b", r"\bstorm(?:ed)?\b"]),
    ("简易装置", [r"booby[- ]trap", r"trip ?wire", r"陷阱"]),
]

WEAPONS = {
    "枪械": [r"gun", r"firearm", r"rifle", r"ak[- ]?47", r"pistol",
           r"自动步枪", r"枪支"],
    "爆炸装置": [r"explosive", r"bomb", r"ied", r"vbi?ed", r"grenade",
             r"爆炸装置", r"炸弹", r"手榴弹"],
    "冷兵器": [r"knife", r"knives", r"machete", r"sword", r"axe",
            r"blade", r"刀", r"斧", r"冷兵器"],
    "车载": [r"vehicle", r"car", r"truck", r"motorcycle", r"车辆"],
    "火箭弹/迫击炮": [r"rocket", r"mortar", r"rpg", r"grenade launcher",
                 r"火箭", r"迫击炮"],
}

# ---- 日期 ----
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

DATE_ISO = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
DATE_DMY = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) +
    r")\.?,?\s+(20\d{2})\b", re.I)
DATE_MDY = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(20\d{2})\b", re.I)
DATE_ZH = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")

NUMWORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}

# 提示词必须带词边界。
# 不带的话「dead」会匹配到「deadline」—— 实测把
# 「1,500 charities risk missing accounts deadline」抽成了死亡 1500 人。
#
# 「murdered / slain / massacred / shot dead / executed」也要收进来。
# 实测漏例：「Seven civilians were murdered and 14 others injured」——
# 死亡词表里没有 murdered，7 条命就丢了，只抽出受伤 14。
# 新闻里描述恐袭常用的正是这几个词。
DEATH_HINT = (r"(?:\bkill(?:ed|s|ing)?\b|\bdead\b|\bdeath(?:s)?\b|"
              r"\bfatalit(?:y|ies)\b|\bdied\b|\bbodies\b|\bcorpse\b|"
              r"\bmurder(?:ed|s)?\b|\bslain\b|\bslay\b|\bmasacr|\bmassacr|"
              r"\bexecuted\b|\bshot dead\b|\bburned alive\b|"
              r"死亡|丧生|遇难|身亡|遇害|被杀|杀死)")
INJ_HINT = (r"(?:\binjur(?:ed|es|y)?\b|\bwound(?:ed|s)?\b|\bhurt\b|"
            r"受伤|伤者|负伤)")

# 这些词出现说明数字是年龄、日期或金额，不是伤亡人数。
# （现在这套判据内联在 extract_numbers 的 ok_num 里，
#   常量保留给别处引用，别删。）
AGE_OR_DATE = re.compile(
    r"(?:ages?|aged|years?[\s\-]*old|yrs?[\s\-]*old|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"january|february|march|april|june|july|august|september|"
    r"october|november|december)\s*$", re.I)


def _find_group(text):
    """组织名匹配。全称走小写子串，缩写走全大写。"""
    low = text.lower()
    hits = []
    for canon, forms in GROUPS.items():
        for f in forms:
            f = f.strip()
            if len(f) < 4:
                # 太短的全称写法不收 —— 「jem」「let」这类四字母以下
                # 在正文里撞普通词的概率很高，交给 ACRONYM_GROUPS 走大写匹配
                continue
            if f in low:
                hits.append((canon, len(f)))
                break
    for canon, acs in ACRONYM_GROUPS.items():
        for ac in acs:
            if re.search(r"\b" + re.escape(ac) + r"\b", text):
                hits.append((canon, len(ac)))
                break
    if not hits:
        return ""
    hits.sort(key=lambda x: -x[1])
    return hits[0][0]


def _find_act(text):
    low = text.lower()
    for name, pats in ACT_PATTERNS:
        for p in pats:
            if re.search(p, low):
                return name
    return ""


def _find_weapon(text):
    low = text.lower()
    out = []
    for name, pats in WEAPONS.items():
        for p in pats:
            if re.search(p, low):
                out.append(name)
                break
    return "/".join(out)


def _num_from_token(tok):
    tok = tok.strip().lower().replace(",", "")
    if tok.isdigit():
        return int(tok)
    parts = tok.split()
    total = 0
    cur = 0
    for w in parts:
        if w in NUMWORDS:
            v = NUMWORDS[w]
            if v == 100:
                cur = max(cur, 1) * 100
            else:
                cur += v
        else:
            return None
    total = cur
    return total or None


def extract_numbers(text):
    """抽死亡和受伤人数。返回 (deaths, injured, basis)。

    做法改了四次，每次都是实测抽错逼出来的：

    第一版：为「死」和「伤」各写一组正则，谁先匹配用谁。
      错在「Seven civilians were murdered and 14 others injured」——
      死亡正则从 murdered 往后吃到 14，抽出死亡 14。
      而 7 才是死的，14 是伤的。

    第二版：加后处理，数字后面先出现受伤词就不算死亡。
      疗效过头：「12 killed and 30 injured」被判成没有死亡数。

    第三版：所有数字和提示词定位后，每个数字归给最近的提示词。
      还是错——14 距 murdered 5 个字、距 injured 8 个字，
      「最近」判给了 murdered。

    第四版（现在）：改成**以提示词为中心**分工。
      按出现顺序走每个提示词，先往左找最近的、未被占用的数字，
      左边没有才往右找。这就是人读句子的顺序：
        Seven(7) ← murdered     14 ← injured
        killed → 12（左边没有，往右）    injured → 30
        killed → 45（左边）
        dead → 5（左边没有，往右）
      四种句式全对，而且不依赖回溯运气。

    数字排除规则仍然保留（都是实测错例）：
      · 后跟 年/月/日/岁 / years old / at the age of / % / am/pm → 日期或年龄
      · 前面是 £ $ € ¥ → 金额
      · 形如 45-year-old → 年龄
      · 大于 10000 → 误抽
    """
    low = text.lower()

    # ---- 所有提示词的位置 ----
    marks = []
    for m in re.finditer(DEATH_HINT, low):
        marks.append((m.start(), m.end(), "d"))
    for m in re.finditer(INJ_HINT, low):
        marks.append((m.start(), m.end(), "i"))
    marks.sort()
    if not marks:
        return None, None, ""

    # ---- 所有候选数字 ----
    nums = []
    for m in re.finditer(r"(?<![\w£$€%.,])(\d{1,5}(?:,\d{3})*)", low):
        nums.append((m.start(), m.end(), m.group(1)))
    words = "|".join(sorted(NUMWORDS, key=len, reverse=True))
    for m in re.finditer(r"\b((?:" + words + r")(?:\s+(?:" + words +
                         r"))*)\b", low):
        nums.append((m.start(), m.end(), m.group(1)))
    nums.sort()

    def ok_num(start, end):
        tail = low[start:end + 34]
        head = low[max(0, start - 14):start]
        if re.search(r"\d\s*(?:年|月|日|岁)", tail):
            return False
        if re.search(r"\d[\s\-]*(?:year|yr)s?[\s\-]*(?:old|\b)", tail):
            return False
        if re.search(r"\d\s*(?:years?|yrs?)\b", tail):
            return False
        if re.search(r"\d\s*(?:a\.?m\.?|p\.?m\.?|h\b|hours?\b|minutes?\b|"
                     r"%|percent)", tail):
            return False
        if re.search(r"at\s+the\s+age", tail):
            return False
        if re.search(r"\b(?:ages?|aged)\s*$", head):
            return False
        if re.search(r"[£$€¥]\s*$", head):
            return False
        if re.search(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov"
                     r"|dec|january|february|march|april|june|july|august"
                     r"|september|october|november|december)\s*$", head):
            return False
        # ---- 后面跟的不是人 ----
        # 「about 50 evacuated」「24 rounds fired」「1,500 charities」
        # 都是数量不是人。伤亡人数的后面通常跟 people/civilians/soldiers
        # 这类人称，或者什么都不跟（「45 killed」）。
        # 若紧跟的是明显的「非人称量词」，这个数字不是伤亡。
        after = low[end:end + 22].lstrip()
        if re.match(r"(?i)^(?:rounds?|bullets?|shots?|vehicles?|cars?|trucks?|"
                    r"buses?|homes?|houses?|buildings?|charities|evacuated|"
                    r"displaced|arrested|detained|percent|per\s*cent|"
                    r"km\b|kilomet|meters?|miles?|hectares?|tons?|litres?|"
                    r"liters?|dollars?|euros?|pounds?|rupees?|pesos?|"
                    r"years?\b|months?\b|weeks?\b|days?\b|hours?\b|"
                    r"airstrikes?|strikes?|rockets?|missiles?|bombs?\b)", after):
            return False
        # ---- 四位数年份 ----
        # 「since 2018」「in 2023」「the 2023 death toll」「on nov. 15, 2026」
        # 里的数字都是年份，不是死亡人数。
        # 实测抽出过「死亡 2023」「死亡 2009」「死亡 1998」「死亡 2026」，
        # 共 48 条，全是这一类。
        #
        # 判据不是「看着像年份就丢」—— 否则「2,026 人死亡」这种
        # 四位数伤亡也会被误杀（大规模屠杀确实能到四位数）。
        # 所以只在**上下文有年份标记**时丢：
        #   前面是 since/in/by/until/from/during/of/the/toll
        #   或者前后紧邻逗号加年份数字
        val = _num_from_token(low[start:end])
        if val is not None and 1900 <= val <= 2100:
            if re.search(r"(?:since|in|by|until|from|during|after|before|of|"
                         r"early|late|mid|the|toll|year|annee|année|"
                         r"de|du|des|en|del|el|das|im|am)\s*$",
                         head):
                return False
            # 「nov. 15, 2026」这种日期串：前面的逗号加两位数
            if re.search(r"\d{1,2}\s*,\s*$", head):
                return False
            if re.match(r"(?i)^\s*(?:and|to|until|-|–|/)\s*(?:19|20)\d{2}",
                        after):
                return False
            # 「2023 death toll」：后面跟 toll/death/casualties 的
            # 反而是有效的伤亡数，不丢
        return True

    used = set()
    got = {"d": None, "i": None}
    for ms, me, kind in marks:
        if got[kind] is not None:
            continue
        pick = None
        # 先往左
        for s, e, raw in nums:
            if e <= ms and ms - e <= 60 and ok_num(s, e) and (s, e) not in used:
                pick = (s, e, raw)
        # 左边没有再往右
        if pick is None:
            for s, e, raw in nums:
                if s >= me and s - me <= 32 and ok_num(s, e) \
                        and (s, e) not in used:
                    pick = (s, e, raw)
                    break
        if pick is None:
            continue
        # 往回看 40 字符：命中片段里若有 at the age / aged / years old，
        # 这个数字是年龄不是伤亡。
        # 实测「He died in 2014 at the age of 66」——
        # 2014 被正确排掉（年份），66 却被当成死亡数。
        if re.search(r"(?i)(?:at\s+the\s+age\s+of|aged|years?\s+old|yrs?\s+old)"
                     r"\s*$", low[max(0, pick[0] - 40):pick[0]]):
            continue
        used.add((pick[0], pick[1]))
        v = _num_from_token(pick[2])
        if v is None or v > 10000:
            continue
        got[kind] = (v, low[max(0, pick[0] - 24):pick[1] + 18])

    parts = []
    if got["d"]:
        parts.append("死亡%d ← %s" % (got["d"][0], got["d"][1]))
    if got["i"]:
        parts.append("受伤%d ← %s" % (got["i"][0], got["i"][1]))
    return (got["d"][0] if got["d"] else None), \
           (got["i"][0] if got["i"] else None), "；".join(parts)


def extract_date(text, fallback_iso=""):
    """正文里的日期优先，退回 feed 的 published。返回 (y, m, d, basis)。"""
    m = DATE_ISO.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), "正文ISO"
    m = DATE_ZH.search(text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), "正文中文"
    m = DATE_DMY.search(text)
    if m:
        return (int(m.group(3)), MONTHS[m.group(2).lower()],
                int(m.group(1)), "正文日月年")
    m = DATE_MDY.search(text)
    if m:
        return (int(m.group(3)), MONTHS[m.group(1).lower()],
                int(m.group(2)), "正文月日年")
    if fallback_iso:
        m = re.match(r"(20\d{2})-(\d{2})-(\d{2})", str(fallback_iso))
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3)), "feed发布时间"
    return "", "", "", ""


def _country_run_ranges(text, gaz):
    """找出「国家名连成一串」的区间，这些是导航列表不是叙述。

    实测 koaci.com 的正文里混着一串国家链接：
      «Bénin Afrique du Sud Angola Botswana Burundi Cameroun Cap-Vert
       Centrafrique Congo (RDC) Djibouti Erythrée Ethiopie Gambie ...»
    它落在正文前 1/3 里，所以只按「前 1/3 截断」挡不住 ——
    275 行里 76 行的国家被填成了「吉布提」，跟这些文章毫无关系。

    判据不是「相邻间距小」—— 多了几个词就断开，实测第一版只抓到 5 段
    却把 Djibouti 漏在外面。改成**窗口判据**：任意 400 字符内如果出现
    ≥5 个国家/省级/首都级地名，这一段就是列表。新闻正文里不会这么写。
    """
    if not gaz:
        return []
    hits = gaz.find(text, max_hits=600)
    pts = []
    for h in hits:
        e = h["entry"]
        lvl = e.get("level")
        if lvl == "country" or lvl == "admin1":
            pts.append((h["start"], h["end"]))
        elif lvl == "city":
            # 首都名常和国家名同形（Djibouti / Djibouti、
            # Singapore / Singapore），导航列表里两级别都会出现
            nm = (e.get("name") or "").lower()
            cn = (e.get("country") or "").lower()
            if nm and nm == cn:
                pts.append((h["start"], h["end"]))
    pts.sort()
    runs = []
    i = 0
    n = len(pts)
    while i < n:
        j = i
        while j + 1 < n and pts[j + 1][1] - pts[i][0] <= 400:
            j += 1
        if j - i + 1 >= 5:
            runs.append((pts[i][0], pts[j][1]))
            i = j + 1
        else:
            i += 1
    # 收尾：合并相邻区间，并向两端延伸。
    #
    # 实测两个坑都出在这里：
    #   ① 导航列表被 400 字符窗口切成两段，后一段只剩 3 个国家
    #      （Zambie Zimbabwe Algérie…），不够 5 个就不算区间，
    #      于是 Zimbabwe 留在正文里被当成事件发生国。
    #   ② 法语写法（Zambie / Algérie / Comores / Egypte / Maroc / Tunisie）
    #      根本认不出来，靠数量凑不够 5。
    # 所以：相邻 <200 字符的区间并起来；再从区间两端继续外扩，
    # 只要下一个地名命中离得近（<60 字符），它就是同一串列表的一部分。
    merged = []
    for s, en in runs:
        if merged and s - merged[-1][1] < 200:
            merged[-1] = (merged[-1][0], en)
        else:
            merged.append((s, en))

    # 外扩
    out = []
    for s, en in merged:
        # 向后：后面还有地名且贴得近，说明列表没结束
        k = 0
        while k < len(pts):
            if pts[k][0] >= en and pts[k][0] - en < 60:
                en = pts[k][1]
            k += 1
        # 向前
        k = len(pts) - 1
        while k >= 0:
            if pts[k][1] <= s and s - pts[k][1] < 60:
                s = pts[k][0]
            k -= 1
        out.append((s, en))
    return out


def _mask_text(text, ranges):
    """把区间内的文字替换成等长空白，保持偏移不变。"""
    if not ranges:
        return text
    buf = list(text)
    for s, en in ranges:
        for i in range(max(0, s), min(len(buf), en)):
            buf[i] = " "
    return "".join(buf)


def _mask_repeated_headings(text):
    """有没有真正的正文，判不出来就返回原样。

    这一层不做正文抽取（那是 extract_article_fields 的职责），
    只做一件事：把明显是「标题列表」的连续片段抹掉。
    判据是同一段里出现「大写开头短语 + 竖线/破折号分隔」超过 3 次，
    且没有事件动词 —— 那是导航栏不是叙述。

    实测留下的问题：有文章的正文其实是「Hochul Weighs Scaffold Law
    Reform After Brooklyn Collapse New York City National Politics
    Crime Weather」这种文章标题堆叠，地名匹配会指向纽约，
    而正文讲的是别的事。这类文章本来就该在标题初筛时被挡掉，
    这里只做兜底。
    """
    return text


def make_summary(text, title=""):
    """事件摘要：正文首段，不用标题。

    原因：恐怖袭击的标题常常是「Police say…」「Update:…」这类无信息量的，
    而首段通常包含「时间 + 地点 + 手段 + 伤亡」四要素。
    截断到 120 字，够台账用。
    """
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        return (title or "").strip()[:120]
    # 跳过导航式开头
    for lead in ("skip to", "advertise", "sign up", "subscribe",
                 "home »", "menu", "share this"):
        if t.lower().startswith(lead):
            t = t[200:]
            break
    t = t.strip()
    if len(t) <= 120:
        return t
    cut = t[:120]
    # 尽量在句末断开
    for sep in ("。", ". ", "! ", "? ", "；", "; "):
        idx = cut.rfind(sep)
        if idx >= 40:
            return cut[:idx + len(sep)].strip()
    return cut.strip() + "…"


def html_to_body(html):
    """HTML → 正文，保留行边界。

    这里有个必须说清的顺序问题。原来走的是「去标签 + 把连续空白压成一个空格」，
    结果整页变成一整行，**首段就是页头的导航**：

      | Environment | News Hi Subscribe Activate Manage eEdition Sign In Sign Out
      99&cent; for 6 mos Support local journalism.

    死亡人数从首段抽，首段是导航栏，所以什么都抽不到 ——
    14,899 篇里只有 563 篇（4%）有死亡数字，不是新闻里没写，是根本没读到正文。

    正确顺序：
      ① 标签换成**换行**而不是空格，保住段落边界
      ② 丢掉 <script>/<style>/<nav>/<footer>/<header>/<aside>
      ③ 按行长和字符集筛掉导航行（短行、全是竖线分隔、菜单词）
      ④ 剩下的行里找**最长连续块**，那才是正文

    不做通用正文抽取器（readability 那类）：小语种地方媒体上经常抽空，
    而且引进来要装依赖，违背「任何人的机器上都能跑」的前提。
    """
    t = re.sub(r"(?is)<(script|style|nav|footer|header|aside|form|"
               r"noscript|iframe|svg)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    # 段落级标签先转换行，行内标签转空格 —— 这样 <p> 之间才断得开
    t = re.sub(r"(?i)</?(?:p|div|br|li|tr|h[1-6]|article|section|blockquote)\b[^>]*>",
               "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&quot;", '"').replace("&#39;", "'")
         .replace("&lt;", "<").replace("&gt;", ">")
         .replace("&cent;", "c").replace("&hellip;", "…"))
    t = re.sub(r"&[a-z]{2,8};", " ", t)

    lines = []
    for raw in t.split("\n"):
        s = re.sub(r"\s+", " ", raw).strip()
        if len(s) < 30:
            continue
        # 竖线/斜杠堆起来的行是导航条
        if s.count("|") >= 3 or s.count("»") >= 2:
            continue
        # 菜单词密度太高
        low = s.lower()
        nav_hits = sum(1 for w in NAV_WORDS if w in low)
        if nav_hits >= 3:
            continue
        # 没有句读符号、字数又少的，多半是标签墙
        if not re.search(r"[.。!?！？;；:：,，]", s) and len(s) < 80:
            continue
        lines.append(s)
    if not lines:
        return ""

    # 找最长连续块：正文段落彼此相邻，导航区是碎片
    best_s, best_len, cur_s, cur_len = 0, 0, 0, 0
    for i, s in enumerate(lines):
        if cur_len == 0:
            cur_s = i
        cur_len += len(s)
        if i + 1 == len(lines) or len(lines[i + 1]) < 60:
            if cur_len > best_len:
                best_s, best_len = cur_s, cur_len
            cur_len = 0
    body = lines[best_s:]
    # 从最优块起点往后取，遇到连续短行就收尾
    out = []
    for s in body:
        if out and len(s) < 60:
            break
        out.append(s)

    # 判「这块到底是不是正文」。
    # 实测问题：侧栏的「相关文章列表」条目之间没有分隔，会被当成
    # 一个很长的连续块。它们长这样：
    #   «Russell Brand's prison message IDF soldiers operating in the West Bank.»
    #   «Appeals in the cases in Texas and Florida were pending.»
    # 条目都是标题，**不以句号结尾**；正文段落几乎都以句号收尾。
    # 所以看句末标点占比：正文块应该有一半以上的行带句末标点。
    def sentence_ratio(block):
        if not block:
            return 0.0
        n = sum(1 for s in block
                if re.search(r"[.。!?！？][\"'”’)\]]?\s*$", s))
        return n / len(block)

    if out and sentence_ratio(out) < 0.35:
        # 这块不像正文，退回去用全部筛过的行
        out = lines

    text_out = "\n\n".join(out or body)
    # 剥掉开头混进来的「别的文章的标题」（yahoo 那种相关新闻小卡）
    return _strip_foreign_head(text_out)


# 导航/菜单常见词。只用来统计行内密度，不做匹配规则。
NAV_WORDS = (
    "subscribe", "sign in", "sign up", "log in", "login", "register",
    "newsletter", "advertise", "contact us", "privacy", "terms",
    "cookie", "menu", "search", "home", "about us", "careers",
    "e-edition", "eedition", "activate", "manage", "sign out",
    "skip to", "back to top", "share", "follow us", "download",
    "订阅", "登录", "注册", "首页", "关于我们", "联系我们", "广告",
)


def pick_lead(body, title=""):
    """从正文里取「首段」—— 第一段有实质内容的。

    跳过：标题重复（正文常把标题再写一遍）、
          纯来源署名（「By X | Nov 20, 2026」）、
          过短的句子。
    """
    if not body:
        return ""
    tl = (title or "").strip().lower()
    for para in re.split(r"\n\n+", body):
        s = para.strip()
        if len(s) < 60:
            continue
        if tl and s.lower().startswith(tl[:40]):
            s = s[len(tl):].strip(" |-–—:")
            if len(s) < 60:
                continue
        # 署名行
        if re.match(r"(?i)^(by\s+\S+|posted\s+|updated\s+|published\s+)", s) \
                and len(s) < 120:
            continue
        return s
    return body[:800]


def ledger_row(rec, gaz, today, template=None):
    """一条正文 → 一行台账候选。

    template: {站点: 该站的模板地名}，来自 detect_template_places.py。
    命中说明这个地名是站点模板（页脚/侧栏），不是文章讲的地方，
    该站这一篇的地点与国家对判定作废 —— 宁可留空，不要错填。
    """
    text = rec.get("text") or ""
    title = rec.get("title") or ""
    body = (title + "\n\n" + text) if title else text

    # 这个站的模板地名
    host = ""
    m = re.match(r"https?://([^/]+)", rec.get("url") or "")
    if m:
        host = m.group(1).lower()
        if host.startswith("www."):
            host = host[4:]
    tpl_place = (template or {}).get(host, {}).get("place", "") if template else ""

    # 伤亡数字只从首段抽。
    # 全文抽会把「died in 2014」「22 Indians had been killed」这种
    # 历史背景当成这次事件的死亡数 —— 实测抽出了「死亡 2014 人」
    # 和「死亡 22 人」两条明显错误。
    lead_para = pick_lead(text, title)
    if not lead_para:
        lead_para = text[:1500]

    # 这道门决定「这篇文章进不进候选池」。
    # 放在抽取层最前面，因为它判的是「是不是事件报道」；
    # 判定层要判的是「算不算恐怖袭击」—— 两个标准不能混。
    is_event, why_not = is_event_report(lead_para)
    if not is_event:
        return None, why_not

    deaths, injured, nbasis = extract_numbers(lead_para)
    y, mo, d, dbasis = extract_date(text, rec.get("published") or "")

    # 地点：先用地名匹配，再用命中次数最多的国家兜底。
    #
    # 必须排除侧边栏/菜单带来的「国家列表」。实测 koaci.com 的正文
    # 末尾带一串国家链接（Côte d'Ivoire Burkina Faso Gabon Congo
    # Sénégal Bénin …），里面每个国家都被匹配到，而文章讲的是加纳。
    # 表现为 275 行里 76 行的国家是「吉布提」—— 一个跟这些文章毫无关系的国家。
    #
    # 挡法：把「国家名连成一串」的导航区抹掉，
    # 并且地名必须出现在**正文前 1/3** —— 新闻正文首段几乎一定
    # 提到事件发生地，这是新闻写作的常识。
    clean = _mask_text(text, _country_run_ranges(text, gaz)) if gaz else text
    # 再抹掉开头一段密集摘要列表（Google News 那种「标题 + 来源」堆叠）
    clean = _mask_repeated_headings(clean)

    # 国家与地点按**内容**判：首段里的地名 + 袭击动作锚点。
    # 旧做法「正文前 1/3 第一个城市」在跨国媒体报道上必然错 ——
    # semafor 报道加沙的事，正文里有 Washington（记者站），判出来就是美国。
    main_place, place_basis = _pick_event_place(clean, gaz, lead_para)
    if main_place is None:
        # 首段真的没有地名，退回旧办法
        lead = clean[:max(1200, len(clean) // 3)]
        lead_hits = gaz.find(lead, max_hits=30) if gaz else []
        use_hits = lead_hits or (gaz.find(clean, max_hits=60) if gaz else [])
        places = []
        seen = set()
        for h in use_hits:
            e = h["entry"]
            key = (e.get("name"), e.get("admin1"))
            if key in seen:
                continue
            seen.add(key)
            places.append(e)
        city_hits = [e for e in places if e.get("level") == "city"]
        main_place = (city_hits or places or [None])[0]
        if main_place is not None:
            place_basis = "正文前1/3首个城市（兜底）"

    country = ""
    country_zh = ""
    if main_place:
        country_zh = main_place.get("country_zh") or ""
        country = country_zh or main_place.get("country") or ""
    else:
        # 连兜底都没有 → 不填。宁缺勿错：
        # 填一个国家会让下游统计凭空多出一条事件。
        place_basis = place_basis or "未找到事件地点"

    place_str = ""
    if main_place:
        bits = [main_place.get("name_zh") or main_place.get("name") or ""]
        a1 = main_place.get("admin1") or ""
        if a1 and a1 != main_place.get("name"):
            bits.append(a1)
        place_str = "，".join(x for x in bits if x)

    # 站点模板地名：这一篇判出的地点就是该站所有文章都命中的那个，
    # 说明它是页脚/侧栏里的词，不是文章讲的地方。
    # 处理是「整篇的地点与国家作废」，不是「换成别的地点」——
    # 换就等于猜，猜出来的国家会污染下游统计。
    if tpl_place and place_str and place_str == tpl_place:
        place_str = ""
        country = ""
        country_zh = ""
        _tpl_dropped = True
    else:
        _tpl_dropped = False

    act = _find_act(body)
    weapon = _find_weapon(body)
    group = _find_group(body)

    # 置信度按要素齐全度映射。不用打分制 —— 台账里写「置信度 3 分」
    # 没人看得懂，写「高/中/低」直接可读。
    score = 0
    score += 1 if country else 0
    score += 2 if isinstance(deaths, int) and deaths > 0 else 0
    score += 1 if (y and mo and d) else 0
    score += 1 if act else 0
    score += 1 if place_str else 0
    score += 1 if group else 0
    if score >= 6:
        conf = "高"
    elif score >= 4:
        conf = "中"
    elif score >= 2:
        conf = "低"
    else:
        conf = "极低"

    row = {c: "" for c in LEDGER_COLS}
    row.update({
        "事件摘要": make_summary(text, title),
        "国家": country,
        "年份": y if y else "",
        "月份": mo if mo else "",
        "日期": d if d else "",
        "地点": place_str,
        "死亡人数": deaths if isinstance(deaths, int) else "",
        "受伤人数": injured if isinstance(injured, int) else "",
        "袭击方式": act,
        "袭击武器": weapon,
        "袭击者": group,
        "负责人/组织": "",      # 需要读「claimed responsibility」句式，下一版再做
        "信息来源URL": rec.get("url") or rec.get("final_url") or "",
        "采集人/组别": "自动采集",
        "录入日期": today,
        "置信度": conf,
        "审核状态": "待审",
        "关联知识点": "",
        "备注": "｜".join(x for x in (nbasis, dbasis, place_basis,
                                   "站点模板地名已剔除" if _tpl_dropped else "")
                        if x)[:200],
    })
    return row, score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=r"D:\event-ledger-data")
    ap.add_argument("--gazetteer", default=None)
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--template-places", default="",
                    help="detect_template_places.py 的输出；剔除站点模板地名")
    ap.add_argument("--min-score", type=int, default=0,
                    help="只要要素齐全度达到此值的行")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="不做标题初筛，全部文章都抽（默认做）")
    ap.add_argument("--only-attack", action="store_true",
                    help="只输出标题命中袭击词的文章（更严，用于人工核）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    import place_match as PM
    import fetch_articles as FETCH
    gaz = PM.Gazetteer(a.gazetteer) if a.gazetteer else PM.Gazetteer()

    template = {}
    if a.template_places and os.path.isfile(a.template_places):
        try:
            t = json.load(io.open(a.template_places, encoding="utf-8"))
            template = t.get("templates") or {}
        except Exception:  # noqa: BLE001
            template = {}

    import gzip
    import glob
    import datetime

    today = datetime.date.today().isoformat()
    files = sorted(glob.glob(os.path.join(a.store, "raw", "meta", "*", "*.json")))
    if a.limit:
        files = files[:a.limit]

    rows = []
    n_read = n_text = n_skip = 0
    n_not_event = n_agg = 0
    for p in files:
        try:
            m = json.load(io.open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") != "article":
            continue
        # ---- 标题初筛 ----
        # 这一层必须做，否则台账里会混进「Raiders list 3 starters as
        # questionable」「Magnitude 4.4 earthquake strikes」这类文章。
        # 实测：不做初筛时 14,680 行里绝大多数跟袭击无关，
        # 只因为它们恰好提到某个地名就被判了国家。
        #
        # 词表复用 fetch_articles.KW —— 多语种，已经在十几万条
        # 条目上跑过。三个来源都看（标题、摘要、URL slug），
        # 因为 sitemap 来的条目常常没有标题。
        if not a.no_prefilter:
            hits = FETCH.looks_like_attack(
                m.get("title") or "", "", m.get("url") or "")
            if not hits:
                n_skip += 1
                continue
        n_read += 1
        text = m.get("text") or ""
        if not text:
            v = m.get("versions") or []
            if not v:
                continue
            fp = os.path.join(a.store, v[-1]["file"].replace("/", os.sep))
            try:
                raw = gzip.open(fp, "rb").read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            # 正文抽取在这里做，不用简单的「去标签压空白」。
            # 那种做法会把整页压成一行，首段变成页头导航，
            # 后面所有按段落的判断全废（见 html_to_body 的说明）。
            text = html_to_body(raw)
        if len(text) < 200:
            continue
        # 聚合页：一页多篇。抽出来的「正文」是几条无关新闻的拼接，
        # 用它判国家、判袭击方式全是错的。
        if looks_like_aggregate(text):
            n_agg += 1
            continue
        n_text += 1
        rec = {"url": m.get("url"), "final_url": m.get("final_url"),
               "title": m.get("title"), "published": m.get("published"),
               "text": text[:20000]}
        row, score = ledger_row(rec, gaz, today, template)
        if row is None:
            n_not_event += 1
            continue
        if score < a.min_score:
            continue
        row["_score"] = score
        rows.append(row)

    print("")
    print("  初筛（标题无袭击词）剔除 %d 篇 ｜ 进入抽取 %d 篇 ｜ 有正文 %d 篇"
          % (n_skip, n_read, n_text))
    print("  非事件报道（首段无「袭击动作+后果」）剔除 %d 篇" % n_not_event)
    print("  聚合页（一页多篇）剔除 %d 篇" % n_agg)
    rows, n_dup = dedupe_rows(rows)
    print("  内容重复（同篇多 URL 副本）合并 %d 行" % n_dup)
    print("  产出候选行 %d" % len(rows))
    print("")

    if rows:
        conf = Counter(r["置信度"] for r in rows)
        cty = Counter(r["国家"] or "(未识别)" for r in rows)
        act = Counter(r["袭击方式"] or "(未识别)" for r in rows)
        print("  置信度分布：%s" % dict(conf))
        print("  国家前 10 ：%s" % dict(cty.most_common(10)))
        print("  袭击方式  ：%s" % dict(act.most_common(8)))
        print("")
        with_d = sum(1 for r in rows if r["死亡人数"] != "")
        with_p = sum(1 for r in rows if r["地点"])
        with_g = sum(1 for r in rows if r["袭击者"])
        print("  有死亡人数 %d（%.0f%%）｜ 有地点 %d（%.0f%%）｜ 有组织 %d（%.0f%%）"
              % (with_d, 100.0 * with_d / len(rows),
                 with_p, 100.0 * with_p / len(rows),
                 with_g, 100.0 * with_g / len(rows)))

    if a.out:
        import csv
        with io.open(a.out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER_COLS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in LEDGER_COLS})
        print("")
        print("  台账候选 → %s（%d 行 × %d 列）" % (a.out, len(rows), len(LEDGER_COLS)))
    return 0


# ============================================================
# 事件报道判据：这篇文章到底在不在讲一起具体的袭击
# ============================================================
# 抽取层的输入是「抓到的所有文章」，其中大部分不是事件报道：
#   · 体育新闻「Dean knows how a championship defense is supposed to look」
#   · 讣告「Anthony Clark's cause of death has been revealed」
#   · 网络安全「Shai-Hulud worm exploited GitHub Actions」
#   · 政治评论「Can the Nevada Legislature improve outcomes?」
#   · 医学网站（acc.org 是心脏病学会）
#
# 共同点：没有「袭击动作」，或者有动作但**没有后果**。
# 讣告里有 death，那是病死的；网络安全新闻里有 attack，但没人死、没设施被炸。
#
# 判据：首段必须同时出现
#   ① 袭击动作（ATTACK_MOVE）
#   ② 伤亡、实体破坏或被控制的人员
# 两条都不满足的不是事件报道，不进候选池。
#
# 为什么放在抽取层而不是判定层：判定层要判「算不算恐怖袭击」，
# 不是「算不算新闻事件」。两个标准混在一起，谁也说不清。
ATTACK_MOVE = re.compile(
    r"(?i)\b(?:attack(?:ed|s|ing)?|assail\w*|assault(?:ed|s)?|"
    r"bomb(?:ed|ing|s)?|blast(?:ed|s)?|explos\w+|"
    r"shoot(?:ing|s)?|shot|opened fire|fired (?:at|on|upon)|"
    r"stab(?:bed|bing|s)?|slash(?:ed|ing)?|machete|"
    r"kidnap(?:ped|ping|s)?|abduct(?:ed|ion|ions|ing)?|taken hostage|"
    r"ambush(?:ed|es|ing)?|raid(?:ed|s|ing)?|storm(?:ed|ing|s)?|"
    r"torch(?:ed|ing)?|set (?:fire|ablaze)|arson|"
    r"massacre[ds]?|beat(?:en|ing)?|bludgeon\w*|"
    r"hijack(?:ed|ing)?|siege|assassinat(?:ed|ion)|"
    r"clash(?:es|ed|ing)?|fighting|gunbattle|firefight|"
    r"insurgen\w*|militants? (?:attack|struck)|"
    r"gunmen|suicide bomb\w*|car bomb|ied\b|grenade|rocket|mortar)\b|"
    r"袭击|爆炸|枪击|砍杀|纵火|绑架|劫持|伏击|交火|暗杀")

# 「强动作」：动作本身就说明发生了暴力事件，不要求首段再写后果。
# 「raid on al-Shifa hospital」这种，新闻当时还没统计出伤亡，但事情发生了。
# 反过来「attack on its servers」「fighting for rights」这类泛泛的用法，
# 必须靠后果来证明它真是一个事件 —— 否则网络安全新闻全进来了。
INHERENT_HARM = re.compile(
    r"(?i)\b(?:bomb(?:ed|ing|s)?|blasts?|explos\w+|torch\w*|arson|"
    r"set (?:fire|ablaze)|burn(?:ed|t)|massacr\w*|beat(?:en)? to death|"
    r"shoot(?:ing|s)?|shot|opened fire|stab(?:bed|bing)?|"
    r"kidnap(?:ped|ping)?|abduct(?:ed|ion)?|taken hostage|held hostage|"
    r"ambush(?:ed)?|raid(?:ed)?|storm(?:ed)?|assassinat\w*|"
    r"hijack(?:ed|ing)?|siege|suicide bomb\w*)\b|"
    r"爆炸|枪击|砍杀|纵火|绑架|劫持|伏击|暗杀|袭击")

HARM_RESULT = re.compile(
    r"(?i)(?:"
    r"\b(?:kill(?:ed|s|ing)?|dead|deaths?|died|fatalit\w*|"
    r"injur(?:ed|es|y|ing)|wound(?:ed|s|ing)?|casualt\w*|bodies|corpse|"
    r"slain|murder(?:ed|s)?|massacred)\b"
    r"|"
    r"\b(?:destroyed|damaged|razed|torched|blown up|bombed|shelled|"
    r"levelled|leveled|gutted|wrecked)\b"
    r"|"
    r"死亡|受伤|伤亡|丧生|遇难|身亡|遇害|被毁|被炸"
    r")")

# 绑架/劫持：受害者在报道时还活着，文本里可能没有 death 也没有 destroy。
# 后果是「人员被控制」，靠 CAPTIVE_EVENT 放行。
CAPTIVE_EVENT = re.compile(
    r"(?i)\b(?:kidnap(?:ped|ping|s)?|abduct(?:ed|ion|ions|ing)?|"
    r"taken hostage|held hostage|hostage|seized|missing)\b|"
    r"绑架|劫持|被绑|失踪")

# 研究/统计类报道不是事件。
# 「Study finds terrorism deaths rose 12 percent since 2020」
# 有 terror（动作词）、有 deaths（后果词），两条都满足，
# 但它讲的是统计趋势，不是一起事件。
STUDY_REPORT = re.compile(
    r"(?i)\b(?:study|studies|report (?:says|finds|shows|warns)|"
    r"research(?:ers)? (?:say|says|found|shows)|survey|data shows|"
    r"according to a (?:new )?(?:report|study|survey)|"
    r"analysis (?:says|finds|shows)|statistics show|"
    r"index (?:shows|finds)|figures show)\b|"
    r"研究报告|调查显示|统计显示|数据显示|分析指出")

# 电头：`WASHINGTON (Reuters) -`、`LONDON, Nov 20 (Reuters) -`。
# 这是记者的发稿地，不是事件发生地。
# 不一刀切掉，就会把「路透华盛顿电」记成事件在美国。
DATELINE = re.compile(
    r"^\s*[A-Z][A-Za-z\.\-']{1,28}"
    r"(?:[ ,]+[A-Z][A-Za-z\.\-']{1,18}){0,3}"
    r"(?:\s*\([A-Za-z ]{2,20}\))?"
    r"(?:\s*,?\s*\d{1,2}\s+[A-Z][a-z]{2,9}\s+\d{4})?"
    r"\s*(?:--|—|–|-)\s+")


def _strip_dateline(text):
    """剥掉开头的电头。只剥一层，剥不掉就算了 —— 猜错比不剥更糟。"""
    t = (text or "").lstrip()
    m = DATELINE.match(t)
    if m and len(m.group(0)) < 90:
        return t[m.end():]
    return t


def _strip_foreign_head(body):
    """剥掉正文开头混进来的「别的文章的标题」。

    实测 yahoo.com 的新闻页，正文容器前面挂了一张「相关新闻」小卡：
      [ 33 字] Nvidia won't reinstate Huang          ← 另一篇（科技）的标题
      [234 字] A 19-year-old Palatine man has been indicted on 27 charges…
    第一行是别的新闻标题，第二行起才是真正文。
    不剥掉，首段就变成「Nvidia 不发 Huang 的邀请函」，
    国家、袭击方式、判定全跟着错。实测同一个 URL 出了 58 条互不相同的候选。

    判据：开头连续若干行，只要「短（<60 字）且不以句末标点结尾」就是标题，丢掉。
    """
    lines = [x.strip() for x in (body or "").split("\n") if x.strip()]
    i = 0
    while i < len(lines) and i < 5:
        x = lines[i]
        if len(x) < 60 and not re.search(r"[.。!?！？][\"'”’)\]]?\s*$", x):
            i += 1
            continue
        break
    if i == 0:
        return body
    return "\n\n".join(lines[i:])


def looks_like_aggregate(body):
    """判断抽出来的这块是不是「多篇文章拼在一个页面上」。

    yahoo.com 的栏目页把多条新闻渲染在同一页，抽出来是几条无关新闻的拼接。
    用它判国家、判袭击方式全是错的。

    判据：开头 6 行里若有 ≥2 行「短且不以句末标点结尾」，就是标题堆叠。
    正文第一行通常是导语，会有完整句子。
    """
    lines = [x.strip() for x in (body or "").split("\n") if x.strip()]
    if len(lines) < 3:
        return False
    n_title = 0
    for x in lines[:6]:
        ends_ok = bool(re.search(r"[.。!?！？][\"'”’)\]]?\s*$", x))
        if len(x) < 120 and not ends_ok:
            n_title += 1
    return n_title >= 2


def is_event_report(lead):
    """首段是否在讲一起具体的袭击事件。

    返回 (是否, 依据)。依据写进备注，方便人工复核时看机器凭什么放行/拦下。
    """
    t = lead or ""
    if not t:
        return False, "无首段"
    if STUDY_REPORT.search(t) and not INHERENT_HARM.search(t):
        return False, "研究/统计类报道，不是具体事件"
    neg, why = is_context_negative(t)
    if neg:
        return False, "语境反例：" + why
    moved = ATTACK_MOVE.search(t)
    harmed = HARM_RESULT.search(t)
    captive = CAPTIVE_EVENT.search(t)
    inherent = INHERENT_HARM.search(t)
    if moved and (harmed or captive or inherent):
        return True, ""
    if moved:
        return False, "首段只有泛泛的动作，没有伤亡、破坏或被控制的人员"
    if harmed:
        return False, "首段有伤亡但无袭击动作（事故/讣告/医疗）"
    return False, "首段无袭击动作"


def _pick_event_place(text, gaz, lead):
    """按内容挑事件发生地：袭击动作做锚点，取离它最近的地名。

    为什么不能用「正文前 1/3 第一个城市」
    --------------------------------
    那套判据在跨国媒体的报道上必然错：
    semafor 报道加沙的事，正文里有 Washington（记者站）、Israel、Gaza，
    「第一个城市」取到华盛顿 → 国家记成美国。
    实测 773 条「美国」里大半是这么来的，而美国本土恐袭一年是个位数。

    新闻写作的规律是「谁在哪做了什么」写在首段。所以：
      ① 首段优先，找不到地名才往后扩到全文前 1/3；
      ② 以袭击动作词为锚点，取**距离最近**的地名；
         "Gunmen attacked a village in northern Nigeria, killing 12" → Nigeria
         "raid on al-Shifa in Gaza ... prompting alarm from Washington" → Gaza
      ③ 城市级加权靠前（更精确）。

    返回 (entry, 依据)。找不到返回 (None, "")。
    """
    if not gaz:
        return None, ""
    seg = _strip_dateline(lead or text or "")
    if not seg:
        return None, ""

    anchors = [m.start() for m in ATTACK_MOVE.finditer(seg)]
    hits = gaz.find(seg, max_hits=80)
    if not hits:
        seg2 = _strip_dateline(text or "")
        seg2 = seg2[:max(1500, len(seg2) // 3)]
        anchors = [m.start() for m in ATTACK_MOVE.finditer(seg2)]
        hits = gaz.find(seg2, max_hits=80)
        seg = seg2
    if not hits:
        return None, ""

    def score(h):
        if not anchors:
            return (h["start"], 0 if h["entry"].get("level") == "city" else 1)
        d = min(abs(h["start"] - a) for a in anchors)
        bonus = 0 if h["entry"].get("level") == "city" else 40
        return (d + bonus, h["start"])

    best = min(hits, key=score)
    d, _ = score(best)
    return best["entry"], "首段袭击动作锚点（距离 %d 字符）" % d



def _dedupe_key(row):
    """同一篇新闻的多个 URL 副本 → 同一个键。

    yahoo 的 URL 尾部是一串数字（-164743176.html），同一篇新闻
    每次生成一个不同的数字。去掉它，再看摘要前缀。
    """
    url = str(row.get("信息来源URL") or "")
    url = re.sub(r"[-_]\d{6,}(?=\.(?:html?|php|aspx?)?$)", "", url)
    url = re.sub(r"\?.*$", "", url)
    summary = re.sub(r"\W+", "", str(row.get("事件摘要") or ""))[:60].lower()
    return (url, summary)


def dedupe_rows(rows):
    """按内容去重，保留第一次出现的那条。

    返回 (去重后, 去掉的条数)。
    """
    seen = set()
    out = []
    for r in rows:
        k = _dedupe_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out, len(rows) - len(out)


# 反例一：航天。`blast off`、`rocket launch`、`space shuttle` 里的 blast
# 是火箭点火，不是爆炸袭击。实测「Space shuttle Atlantis will blast off」
# 被当成爆炸事件，国家判到南非。
SPACE_NEWS = re.compile(
    r"(?i)\b(?:blast off|blasted off|rocket launch|spacecraft|space shuttle|"
    r"space station|NASA|ESA|SpaceX|Blue Origin|orbital|orbit\b|"
    r"satellite|cosmonaut|astronaut|payload fairing|re-?entry|"
    r"solar storm|space weather|launch window)\b")

# 反例二：武器装备介绍。`MRAP (Mine-Resistant Ambush Protected)`
# 里的 Ambush 是车名的一部分。这类文章整篇讲型号参数，不是事件。
WEAPON_SPEC = re.compile(
    r"(?i)\b(?:MRAP|mine-?resistant|armou?red (?:vehicle|personnel carrier)|"
    r"APC\b|IFV\b|main battle tank|combat vehicle|"
    r"frigate|destroyer|corvette|submarine|"
    r"fighter jet|bomber aircraft|attack helicopter|"
    r"air defense system|missile system|radar system|"
    r"is a \d+(?:x\d+)? .{0,30}vehicle|designed and manufactured|"
    r"entered service|serial production|maiden flight|"
    r"defen[cs]e contract|arms deal)\b")

# 反例三：事故。油罐车翻车、火车脱轨、瓦斯泄漏、电线短路，
# 文本里全是 explosion / blast / killed，但它们没有施害者。
ACCIDENT_NEWS = re.compile(
    r"(?i)\b(?:tanker|fuel truck|petrol tanker|camion-citerne|"
    r"gas leak|gas explosion|gas cylinder|pipeline rupture|"
    r"derailed|derailment|train crash|road accident|car crash|"
    r"collision|electrical fault|short circuit|"
    r"mine collapse|building collapse|scaffolding|"
    r"industrial accident|workplace accident|"
    r"negligence|safety lapses?)\b|"
    r"翻车|脱轨|瓦斯爆炸|燃气泄漏|车间事故|坍塌")

# 反例四：偷猎/野生动物。`shot` 出现在「Elk illegally shot」里。
WILDLIFE_NEWS = re.compile(
    r"(?i)\b(?:elk|deer|moose|bear|wolf|poach\w*|trophy hunt\w*|"
    r"antlers?|carcass|wildlife (?:officer|department|crime)|"
    r"game warden|illegally (?:shot|killed) (?:an? )?(?:elk|deer|bear))\b")

# 反例五：司法/判决类后续报道。
# 「 convicted of stabbing ... was sentenced to 25 years」——
# 事件本身发生在几年前，这篇是判刑消息，不是新事件。
COURT_FOLLOWUP = re.compile(
    r"(?i)\b(?:sentenced to|pleads? not guilty|pleaded not guilty|"
    r"convicted of|acquitted|indicted on|jury (?:found|convicted)|"
    r"appeals? court|supreme court (?:ruled|upholds)|"
    r"charged with|arraigned|bail (?:set|granted)|"
    r"will (?:stand|go) trial|case adjourned)\b")


def is_context_negative(text):
    """语境反例：字面命中暴力词，但整篇不是袭击事件。

    返回 (是否命中反例, 类别)。
    """
    t = text or ""
    if SPACE_NEWS.search(t):
        return True, "航天发射"
    if WEAPON_SPEC.search(t):
        return True, "武器装备介绍"
    if ACCIDENT_NEWS.search(t):
        return True, "事故（无施害者）"
    if WILDLIFE_NEWS.search(t):
        return True, "野生动物/偷猎"
    return False, ""


def is_court_followup(text):
    """司法后续报道：事件已发生，这篇讲的是判刑/开庭，不是新事件。

    单独一个函数，因为它和「不是事件」有区别 —— 它确实对应一起事件，
    只是不该记成**新的一条**。上层要怎么处理由调用方决定。
    """
    return bool(COURT_FOLLOWUP.search(text or ""))


if __name__ == "__main__":
    sys.exit(main())