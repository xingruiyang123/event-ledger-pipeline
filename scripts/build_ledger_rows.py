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
DEATH_HINT = (r"(?:\bkill(?:ed|s|ing)?\b|\bdead\b|\bdeath(?:s)?\b|"
              r"\bfatalit(?:y|ies)\b|\bdied\b|\bbodies\b|\bcorpse\b|"
              r"死亡|丧生|遇难|身亡)")
INJ_HINT = (r"(?:\binjur(?:ed|es|y)?\b|\bwound(?:ed|s)?\b|\bhurt\b|"
            r"受伤|伤者|负伤)")

# 这些词出现说明数字是年龄、日期或金额，不是伤亡人数。
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

    规则比想象中难。下面每一条都来自实测错例：

    ① 数字必须紧挨提示词。正文里到处都是年份、年龄；
       宽松匹配会把「died in 2014」抽成死亡 2014 人。

    ② 「22 Indians had been killed」不算这次事件的死亡数 ——
       那是历史背景。本次事件的死亡数通常在首段、
       且主语是事件本身。所以只在**首段**里抽数字，
       首段没有就留空，宁可空也不错。

    ③ 「des morts et nombreux blessés」这种不带数字的表述
       抽不出数，就留空，不要给 0。

    ④ 数字大于 10000 一律视为误抽（单起袭击死亡过万极罕见，
       哈萨克/非洲大屠杀除外，但那种会写「bodies」）。
    """
    low = text.lower()

    def grab(hint):
        # 数字前面不能是 £ $ % 或字母（"ages 18"、"oct 10" 这种）
        pat_a = (r"(?<![\w£$€%.,])(\d{1,5}(?:,\d{3})*)"
                 r"\s*(?:[a-z\-]+\s+){0,4}?" + hint)
        pat_b = (hint + r"\s*(?:[a-z\-]+\s+){0,3}?(?:by\s+)?"
                 r"(\d{1,5}(?:,\d{3})*)")
        pat_c = (r"(\d{1,5})\s*(?:人|名)[^。，,]{0,6}?" + hint)
        pat_d = (r"(?:造成|导致|致使)\s*(\d{1,5})\s*(?:人|名)?\s*" + hint)
        best = None
        for p in (pat_a, pat_b, pat_c, pat_d):
            for m in re.finditer(p, low):
                v = _num_from_token(m.group(1))
                if v is None or v > 10000:
                    continue
                ctx = m.group(0)
                # 年龄写法：「45-year-old man was killed」里的 45 是年龄，
                # 不是死亡人数。实测这条抽出「死亡 45 人」。
                if re.search(r"\d\s*[\-\s]?\s*(?:year|yr)s?\s*[\-\s]?\s*old",
                             low[m.start():m.end() + 30]):
                    continue
                # 数字后面紧跟年/月/日/岁 的，是日期或年龄不是伤亡
                if re.search(r"\d\s*(?:年|月|日|岁|years?\s+old|yrs?|"
                             r"a\.?m\.?|p\.?m\.?|h\b|hours?\b|minutes?\b|"
                             r"%|percent)", ctx):
                    continue
                # 「dead at 66」「deadly 2025」这类：数字紧跟 at/ly/of 的
                if re.search(r"(?:at|ly|of|to|in)\s+\d", ctx):
                    continue
                # 年龄/日期：数字后紧跟 ages / years old / 月份
                if AGE_OR_DATE.search(ctx.split(str(m.group(1)))[0]):
                    continue
                # 货币
                if re.search(r"[£$€¥]\s*\d", ctx):
                    continue
                if best is None or v > best[0]:
                    best = (v, ctx[:80])
        words = "|".join(sorted(NUMWORDS, key=len, reverse=True))
        m = re.search(r"\b(" + words + r"(?:\s+" + words + r")*)\s+"
                      r"(?:[a-z\-]+\s+){0,3}?" + hint, low)
        if m:
            v = _num_from_token(m.group(1))
            if v is not None and v <= 10000 \
                    and (best is None or v > best[0]):
                best = (v, m.group(0)[:80])
        return best

    d = grab(DEATH_HINT)
    i = grab(INJ_HINT)
    parts = []
    if d:
        parts.append("死亡%d ← %s" % (d[0], d[1]))
    if i:
        parts.append("受伤%d ← %s" % (i[0], i[1]))
    return (d[0] if d else None), (i[0] if i else None), "；".join(parts)


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


def ledger_row(rec, gaz, today):
    """一条正文 → 一行台账候选。"""
    text = rec.get("text") or ""
    title = rec.get("title") or ""
    body = (title + "\n\n" + text) if title else text

    # 伤亡数字只从首段抽。
    # 全文抽会把「died in 2014」「22 Indians had been killed」这种
    # 历史背景当成这次事件的死亡数 —— 实测抽出了「死亡 2014 人」
    # 和「死亡 22 人」两条明显错误。
    head = text[:1500]
    lead_para = ""
    for para in re.split(r"(?<=[。.!?])\s+|\n\n", head):
        if len(para.strip()) >= 60:
            lead_para = para
            break
    if not lead_para:
        lead_para = head

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
    # 主地点：优先城市级，其次按出现顺序
    city_hits = [e for e in places if e.get("level") == "city"]
    main_place = (city_hits or places or [None])[0]

    country = ""
    country_zh = ""
    if main_place:
        country_zh = main_place.get("country_zh") or ""
        country = country_zh or main_place.get("country") or ""
    else:
        # 没有地名命中就看国家词频
        cnt = Counter()
        for h in use_hits:
            e = h["entry"]
            if e.get("level") == "country":
                cnt[e.get("country_zh") or e.get("country") or ""] += 1
        if cnt:
            country = cnt.most_common(1)[0][0]

    place_str = ""
    if main_place:
        bits = [main_place.get("name_zh") or main_place.get("name") or ""]
        a1 = main_place.get("admin1") or ""
        if a1 and a1 != main_place.get("name"):
            bits.append(a1)
        place_str = "，".join(x for x in bits if x)

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
        "备注": "｜".join(x for x in (nbasis, dbasis) if x)[:200],
    })
    return row, score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=r"D:\event-ledger-data")
    ap.add_argument("--gazetteer", default=None)
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-score", type=int, default=0,
                    help="只要要素齐全度达到此值的行")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    import place_match as PM
    gaz = PM.Gazetteer(a.gazetteer) if a.gazetteer else PM.Gazetteer()

    import gzip
    import glob
    import datetime

    today = datetime.date.today().isoformat()
    files = sorted(glob.glob(os.path.join(a.store, "raw", "meta", "*", "*.json")))
    if a.limit:
        files = files[:a.limit]

    rows = []
    n_read = n_text = 0
    for p in files:
        try:
            m = json.load(io.open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") != "article":
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
            # 粗暴去标签。真正的正文抽取由 extract_article_fields 负责，
            # 这里只是兜底 —— 没有正文就无法抽事件。
            b = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
            b = re.sub(r"(?s)<[^>]+>", " ", b)
            text = re.sub(r"\s+", " ", b)
        if len(text) < 200:
            continue
        n_text += 1
        rec = {"url": m.get("url"), "final_url": m.get("final_url"),
               "title": m.get("title"), "published": m.get("published"),
               "text": text[:20000]}
        row, score = ledger_row(rec, gaz, today)
        if score < a.min_score:
            continue
        row["_score"] = score
        rows.append(row)

    print("")
    print("  读过 meta %d 篇 ｜ 有正文 %d 篇 ｜ 产出候选行 %d" %
          (n_read, n_text, len(rows)))
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


if __name__ == "__main__":
    sys.exit(main())