"""地名匹配 —— 从文本里认出地点，并给出可用于消歧的候选。

这一层要解决的具体问题
----------------------
抽取层要把「地点」那一列填出来，而同一个地方在报道里有十几种写法：

  「白沙瓦一处检查站遇袭」             中文
  「پشاور میں ایک چیک پوسٹ پر حملہ」   乌尔都文
  「Peshāwar」                        带变音符号
  「Peshawar」                        标准拼法
  「在开伯尔-普什图省的马尔丹」         中文省名 + 中文市名

不做词表匹配，只能靠人工一条条改。

匹配到名字还不够，必须能消歧
----------------------------
「Bagh」在巴控克什米尔有两个、在旁遮普有一个。报道说「Bagh 发生袭击」
时不看上下文判不出是哪个。所以这层给的不是「一个地点」，而是
**候选列表 + 消歧依据**（国家、一级行政区、坐标、人口），由下游结合上下文选。

三个必须挡住的误配
------------------
用 40 篇真实正文跑出来的，不是想象出来的：

  media      → 阿尔及利亚 Medea，38 次
  massacre   → 圣卢西亚 Massacre 村
  mali       → 土库曼斯坦 Mali 村（同时也是国家马里的小写形式）
  content    → 牙买加 Content 村
  a la       → 印度比哈尔邦 A la 村

这些都是**真实存在的地名**，光看词表发现不了。挡法有三层：
  ① 拉丁写法必须在原文里首字母大写
  ② 普通词黑名单
  ③ 城市人口 ≥ 2 万、二级行政区默认不参与

性能
----
全量词表 12 万种写法。最初逐写法跑 re.finditer，一篇正文 452 毫秒；
改成分桶预编译后 217 毫秒 —— 还是慢，因为 Python 的 re 引擎面对
几万个分支的 alternation 会退化。
最终改成**按词查表**：把正文切成词，逐个查字典（含 2-4 词的组合），
一篇正文降到 2 毫秒级，且完全避开了正则退化。
"""
import io
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "place_gazetteer.jsonl.gz")

# 太短的拉丁写法极容易撞普通词。四字母以下的基本都是缩写或常见词。
MIN_LATIN = 4

# 城市要够大才参与匹配。小村庄名字和普通词撞的概率太高：
# Content（牙买加村落）、Massacre（圣卢西亚村落）、Mali（土库曼村落）
# 都是这么进来的。2 万人口是个经验线 —— 低于这个数的居民点，
# 新闻报道里出现时几乎一定带上级地名，靠上级地名匹配就够了。
MIN_CITY_POP = 20000

# 行政二级（县/区）名字噪声大，默认不参与匹配，需要时显式打开。
INCLUDE_ADMIN2 = False

# 这些名字在正文里几乎一定是别的意思，不是地名。
STOP_NAMES = {
    "poor", "safe", "best", "hope", "union", "liberty", "progress",
    "victory", "independence", "industry", "commerce", "confidence",
    "concord", "harmony", "paradise", "providence", "rock", "hill",
    "park", "garden", "green", "river", "lake", "north", "south",
    "east", "west", "eastern", "western", "northern", "southern",
    "center", "central", "new", "old", "upper", "lower", "big",
    "little", "long", "white", "black", "red", "main", "market",
    "model", "modern", "national", "international", "general",
    "public", "civil", "royal", "grand", "great", "high", "home",
    "house", "city", "town", "village", "area", "zone", "sector",
    "block", "colony", "extension", "scheme", "society", "camp",
    "base", "port", "airport", "station", "hospital", "university",
    "college", "school", "academy", "institute", "mobile", "a la",
    "media", "content", "massacre", "mali", "iran", "iraq", "oman",
    "chad", "jordan", "guinea", "niger", "congo", "sudan", "japan",
    "china", "india", "israel", "egypt", "libya", "syria", "yemen",
    "police", "army", "court", "judge", "doctor", "leader",
    "minister", "president", "attack", "bomb", "blast", "shooting",
    "killed", "death", "victim", "suspect", "gunmen", "militant",
    "terror", "government", "official", "security", "military",
    "forces", "border", "region", "province", "district", "county",
    "state", "afghan", "pakistani", "indian", "american", "british",
    "french", "russian", "chinese", "turkish", "saudi", "iranian",
    # 大洲/大区名。GeoNames 把这些当真别名挂到了具体条目上：
    #   Mahdia（突尼斯城市）的别名里有 "Africa"
    #   Sahel（布基纳法索一级行政区）的别名就是 "Sahel"
    #   Central / East / West / North / South 是一堆国家的行政区名
    # 实测后果：semafor.com 的正文页脚有导航词 "Africa"，
    # 855 篇文章里 731 篇被判成「突尼斯·马赫迪耶」——
    # 而它们讲的是加沙、乌克兰、拜登竞选。
    "africa", "asia", "europe", "america", "americas", "oceania",
    "antarctica", "eurasia", "sahel", "balkans", "caucasus",
    "maghreb", "levant", "caribbean", "world", "global",
    "international", "online", "today", "times", "news",
}

# 拉丁词：允许词内带撇号、连字符、变音符号。
WORD = re.compile(r"[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F'’\-]*")

# 非拉丁文字按「连续同类字符」切段：中日韩一段，阿拉伯字母一段。
CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
AR_RUN = re.compile(r"[\u0600-\u06FF\u0750-\u077F]+")


def _open_any(path):
    """同时支持 .jsonl 和 .jsonl.gz。

    词表 19 MB（8.6 万条、含 17 种语种别名），明文放仓库太大，
    压缩后 2 MB。加载时透明解压，调用方不用关心是哪种。
    """
    if str(path).endswith(".gz"):
        import gzip
        return io.TextIOWrapper(gzip.open(path, "rb"),
                                encoding="utf-8", errors="replace")
    return io.open(path, encoding="utf-8")


# 允许出现在地名前面的「大写词」—— 这些是功能词，不是人名的一部分。
# 判据只有一条：这个词在正文里经常紧挨着地名出现（in / from / near / at…），
# 而人名前面的词是名字本身。
CAPITALIZED_STOP = {
    "in", "at", "from", "near", "of", "to", "the", "a", "an", "and", "or",
    "on", "by", "for", "with", "into", "over", "under", "across", "outside",
    "inside", "around", "via", "off", "as", "after", "before", "during",
    "since", "until", "against", "between", "within", "through", "toward",
    "towards", "along", "beyond", "behind", "beside", "among", "amid",
    "north", "south", "east", "west", "northern", "southern", "eastern",
    "western", "central", "upper", "lower", "new", "old", "greater",
    "said", "says", "told", "reported", "including", "especially",
    "particularly", "meanwhile", "however", "but", "also", "then",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
}


def has_cjk(s):
    return bool(re.search(r"[\u3400-\u9fff]", s))


def has_arabic(s):
    return bool(re.search(r"[\u0600-\u06FF\u0750-\u077F]", s))


def is_latin(s):
    return bool(re.fullmatch(r"[A-Za-z\u00C0-\u024F][A-Za-z\u00C0-\u024F' .\-]*", s))


class Gazetteer:
    def __init__(self, path=DEFAULT_PATH, include_admin2=None):
        self.by_form = {}      # 归一化写法 → [entry, ...]
        self.entries = []
        self.max_words = 1
        self.include_admin2 = (INCLUDE_ADMIN2 if include_admin2 is None
                               else include_admin2)
        self._nonlatin = []
        if os.path.isfile(path):
            self.load(path)
            self._index()

    # ---- 装载 ----
    def load(self, path):
        for ln in _open_any(path):
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if not self._wanted(e):
                continue
            self.entries.append(e)
            for form in self._forms(e):
                self.by_form.setdefault(form, []).append(e)

    def _wanted(self, e):
        lvl = e.get("level")
        if lvl == "admin2" and not self.include_admin2:
            return False
        if lvl == "city" and int(e.get("population") or 0) < MIN_CITY_POP:
            return False
        return True

    def _forms(self, e):
        out = set()
        primary = (e.get("name") or "").strip().lower()
        for x in [e.get("name"), e.get("name_zh")] + list(e.get("aliases") or []):
            x = (x or "").strip()
            if not x:
                continue
            if is_latin(x):
                if len(x) < MIN_LATIN:
                    continue
                low = re.sub(r"\s+", " ", x.lower())
                if low in STOP_NAMES:
                    continue
                # 短别名单独收紧：4-5 个字母的**别名**极容易撞普通词，
                # 实测 Sochi 的别名「Soci」在法语文本里被当成地名 31 次、
                # Sidon 的别名「Syda」被当成地名、Gaza 这类真城市反而没问题。
                # 所以只放行「主名」和国家/一级行政区，别名要够长。
                if len(low) < 6 and low != primary \
                        and e.get("level") not in ("country", "admin1"):
                    continue
                out.add(low)
            elif has_cjk(x) or has_arabic(x):
                if len(x) < 2:
                    continue
                out.add(x)
        return out

    def _index(self):
        # 同一个写法对上多个实体时（Kabul 既是省也是市），城市的排前面 ——
        # 报道说「Kabul 发生爆炸」通常指城区。
        lvl = {"city": 0, "admin2": 1, "admin1": 2, "country": 3}
        for f in self.by_form:
            self.by_form[f].sort(
                key=lambda e: (lvl.get(e.get("level"), 9),
                               -int(e.get("population") or 0)))
        # 拉丁写法最长几个词：查表时要试 1..N 词的组合
        for f in self.by_form:
            if is_latin(f):
                self.max_words = max(self.max_words, f.count(" ") + 1)
        self.max_words = min(self.max_words, 6)
        self._nonlatin = [(f, ents) for f, ents in self.by_form.items()
                          if not is_latin(f)]
        # 非拉丁写法最长几个字：切段查表时按这个上限枚举子串
        self.max_nonlatin = max(
            [len(f) for f in self.by_form if not is_latin(f)] or [1])

    # ---- 匹配 ----
    def find(self, text, country=None, max_hits=40):
        """在文本里找地名，返回 [{form, start, end, entry}]。

        拉丁部分：切成词，按 1..N 词的组合查表。要求首词在原文里首字母大写。
        非拉丁部分：在文本里子串查找（中日韩、阿拉伯字母没有词边界）。
        """
        if not text:
            return []
        cands = []

        # ---- 拉丁：按词查表 ----
        toks = [(m.start(), m.group(0)) for m in WORD.finditer(text)]
        lows = [t[1].lower() for t in toks]
        n = len(toks)
        for i in range(n):
            t = toks[i][1]
            if not t[:1].isupper():
                continue
            # 句首的大写词要不要算证据，分两种情况：
            #   · 单字命中：跳过。英文每句第一个词都大写，
            #     「Sochi」「Soci」这种在句首就是普通的大写词，跳掉最稳。
            #   · 多词命中（Port Said、New Delhi、Sao Paulo）：保留。
            #     实测漏例：「Port Said was hit by a blast」——
            #     Port 在句首被跳过，整条地名就没了。
            #     多词地名不是普通英文句子会碰巧凑出来的。
            at_sentence_start = False
            if i == 0:
                at_sentence_start = True
            else:
                prev = text[:toks[i][0]].rstrip()
                if prev and prev[-1:] in ".!?:\"»'":
                    at_sentence_start = True
            for k in range(1, min(self.max_words, n - i) + 1):
                key = " ".join(lows[i:i + k])
                ents = self.by_form.get(key)
                if not ents:
                    continue
                if k == 1 and at_sentence_start:
                    continue
                # 单字命中时，若前一个词也是大写开头，多半是人名/品牌名的一部分。
                # 实测：「Lockheed Martin to manufacture ExLS naval missile
                # launchers」被判成斯洛伐克城市「马丁」（Martin）——
                # 372 篇美军装备报道全被算到斯洛伐克头上。
                # 同类的还有 Martin Luther King、George Bush 之类。
                # 双词以上的命中（New Delhi、Port Said）不受这条限制，
                # 因为它们是整体查表查出来的。
                if k == 1 and i > 0:
                    prev = toks[i - 1][1]
                    if (prev[:1].isupper()
                            and prev.lower() not in CAPITALIZED_STOP):
                        gap = text[toks[i - 1][0] + len(prev):toks[i][0]]
                        if not gap.strip():
                            continue
                s = toks[i][0]
                en = toks[i + k - 1][0] + len(toks[i + k - 1][1])
                for e in ents:
                    if country and e.get("country_code") != country:
                        continue
                    cands.append((s, en, len(key), key, e))

        # ---- 非拉丁：子串查找 ----
        # 不能逐个 `form in text` 扫 2.4 万种写法 —— 实测这一句吃掉 95 毫秒，
        # 是整篇耗时的主体。改成两端取交集：先按字符集判断这段文字里
        # 到底有没有中日韩/阿拉伯字母，没有就整段跳过；有才切段查表。
        if any(has_cjk(text) for _ in (0,)) or has_arabic(text):
            for rx in ((CJK_RUN, AR_RUN) if True else ()):
                for m in rx.finditer(text):
                    run = m.group(0)
                    # 一段里最长可能的地名长度，由词表决定
                    for L in range(min(len(run), self.max_nonlatin),
                                   1, -1):
                        for off in range(0, len(run) - L + 1):
                            sub = run[off:off + L]
                            ents = self.by_form.get(sub)
                            if not ents:
                                continue
                            for e in ents:
                                if country and e.get("country_code") != country:
                                    continue
                                cands.append((m.start() + off,
                                              m.start() + off + L,
                                              L, sub, e))

        # 长的优先，占住区间后短的不能再重叠
        cands.sort(key=lambda x: (-x[2], x[0]))
        taken = []
        hits = []
        for s, en, ln, form, e in cands:
            if any(not (en <= a_ or s >= b_) for a_, b_ in taken):
                continue
            taken.append((s, en))
            hits.append({"form": form, "start": s, "end": en, "entry": e})
            if len(hits) >= max_hits:
                break
        hits.sort(key=lambda x: x["start"])
        return hits

    def disambiguate(self, form, country=None, admin1=None, limit=8):
        """同名多义时给出候选，按可判定程度排序。

        排序依据（谁更有可能是报道里说的那个）：
          1. 城市优先于同名行政区（说「X 发生袭击」通常指城区）
          2. 人口多的优先（报道更可能提到大城市）
          3. 有上级行政区的优先（能被上下文约束）
        """
        key = form.lower() if is_latin(form) else form
        ents = list(self.by_form.get(key) or [])
        if country:
            ents = [e for e in ents if e.get("country_code") == country]
        if admin1:
            hit = [e for e in ents if str(e.get("admin1") or "").lower()
                   == str(admin1).lower()]
            if hit:
                ents = hit
        lvl = {"city": 0, "admin2": 1, "admin1": 2, "country": 3}
        ents.sort(key=lambda e: (lvl.get(e.get("level"), 9),
                                 -int(e.get("population") or 0)))
        return ents[:limit]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--gazetteer", default=DEFAULT_PATH)
    ap.add_argument("--text", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--country", default=None)
    a = ap.parse_args()

    g = Gazetteer(a.gz if hasattr(a, "gz") else a.gazetteer)
    print("")
    print("  词表 %s" % a.gazetteer)
    print("  条目 %d ｜ 可匹配写法 %d" % (len(g.entries), len(g.by_form)))
    print("")

    text = a.text
    if a.file:
        text = io.open(a.file, encoding="utf-8", errors="replace").read()
    if not text:
        print("  用 --text 或 --file 给一段文字")
        return 0

    hits = g.find(text, country=a.country)
    print("  命中 %d 处：" % len(hits))
    for h in hits:
        e = h["entry"]
        print("    %-24s [%s] %s / %s  人口 %s"
              % (h["form"][:22], e.get("level"),
                 e.get("country_zh") or e.get("country"),
                 e.get("admin1") or "—", e.get("population") or 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())