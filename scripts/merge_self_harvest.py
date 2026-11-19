"""把自采候选（矩阵遍历）并入 UCDP 候选，不重复计数。

为什么需要单独一步
------------------
UCDP 收录有上限，媒体独家事件不在其中。矩阵遍历能搜到这些，
但直接并进候选会让同一事件出现两条：一条来自 UCDP、一条来自新闻，
台账总数虚增。

所以合并要做三件事：
  1. 去噪：自采候选里混着国家错配和重复拼贴
  2. 匹配：与 UCDP 已记录的同一事件，不新增，只补它的新闻来源
  3. 新增：确实不在 UCDP 里的，才作为新事件进入判定

实测噪声（2026-07 马里+索马里，166 条候选）
--------------------------------------------
  搜 Mali 返回「尼日利亚多地安全形势严峻」「阿富汗努里斯坦省山洪」——
  检索通道不保证结果与查询国家一致，不校验会让台账里出现
  "马里：阿富汗山洪致 20 人死亡"这种条目。

  同一条新闻的标题与正文被拼在一起：
  「【微特稿】马里政府军车队遭伏击 逾50人死亡 网易。【微特稿】马里政府军..."
  → 摘要前 40 字相同即视为同一条。
"""
import argparse
import io
import json
import os
import re

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET_MONTH = ""    # 由 --month 设定，用于剔除往年事件
WITH_MATCH = False   # True = 把匹配到的新闻写回 UCDP 事件（默认关闭，见下）

# 国家名映射：中文 -> 英文（够覆盖 UCDP 常见国家即可，缺的按原文处理）
CN_EN = {
    "阿富汗": ["afghanistan", "afghan"],
    "巴基斯坦": ["pakistan", "pakistani"],
    "尼日利亚": ["nigeria", "nigerian"],
    "叙利亚": ["syria", "syrian"],
    "刚果（金）": ["congo", "drc", "congolese"],
    "刚果民主共和国": ["congo", "drc"],
    "乌克兰": ["ukraine", "ukrainian"],
    "索马里": ["somalia", "somali"],
    "马里": ["mali", "malian"],
    "哥伦比亚": ["colombia", "colombian"],
    "以色列": ["israel", "israeli"],
    "喀麦隆": ["cameroon", "cameroonian"],
    "伊朗": ["iran", "iranian"],
    "也门": ["yemen", "yemeni"],
    "布基纳法索": ["burkina"],
    "墨西哥": ["mexico", "mexican"],
    "尼日尔": ["niger", "nigerien"],
    "伊拉克": ["iraq", "iraqi"],
    "印度": ["india", "indian"],
    "泰国": ["thailand", "thai"],
    "菲律宾": ["philippines", "philippine"],
    "莫桑比克": ["mozambique", "mozambican"],
    "苏丹": ["sudan", "sudanese"],
    "南苏丹": ["south sudan"],
    "中非": ["central african", "centrafrique"],
    "埃塞俄比亚": ["ethiopia", "ethiopian"],
    "肯尼亚": ["kenya", "kenyan"],
    "埃及": ["egypt", "egyptian"],
    "土耳其": ["turkey", "turkish"],
    "俄罗斯": ["russia", "russian"],
    "美国": ["united states", "american", "u.s."],
    "英国": ["britain", "british", "uk"],
    "法国": ["france", "french"],
    "德国": ["germany", "german"],
    "巴西": ["brazil", "brazilian"],
    "秘鲁": ["peru", "peruvian"],
    "厄瓜多尔": ["ecuador"],
    "海地": ["haiti", "haitian"],
    "南非": ["south africa"],
    "阿尔及利亚": ["algeria", "algerian"],
    "利比亚": ["libya", "libyan"],
    "突尼斯": ["tunisia"],
    "摩洛哥": ["morocco"],
    "沙特阿拉伯": ["saudi"],
    "约旦": ["jordan"],
    "黎巴嫩": ["lebanon", "lebanese"],
    "孟加拉国": ["bangladesh"],
    "缅甸": ["myanmar", "burma"],
    "印度尼西亚": ["indonesia"],
    "马来西亚": ["malaysia"],
    "斯里兰卡": ["sri lanka"],
    "尼泊尔": ["nepal"],
    "塔吉克斯坦": ["tajikistan"],
    "乌兹别克斯坦": ["uzbekistan"],
    "哈萨克斯坦": ["kazakhstan"],
    "阿塞拜疆": ["azerbaijan"],
    "亚美尼亚": ["armenia"],
    "格鲁吉亚": ["georgia"],
    "委内瑞拉": ["venezuela"],
    "玻利维亚": ["bolivia"],
    "阿根廷": ["argentina"],
    "智利": ["chile"],
    "危地马拉": ["guatemala"],
    "洪都拉斯": ["honduras"],
    "萨尔瓦多": ["salvador"],
    "尼加拉瓜": ["nicaragua"],
    "布隆迪": ["burundi"],
    "卢旺达": ["rwanda"],
    "乌干达": ["uganda"],
    "坦桑尼亚": ["tanzania"],
    "津巴布韦": ["zimbabwe"],
    "赞比亚": ["zambia"],
    "安哥拉": ["angola"],
    "塞内加尔": ["senegal"],
    "几内亚": ["guinea"],
    "塞拉利昂": ["sierra leone"],
    "利比里亚": ["liberia"],
    "科特迪瓦": ["ivory coast", "côte d'ivoire"],
    "加纳": ["ghana"],
    "多哥": ["togo"],
    "贝宁": ["benin"],
    "乍得": ["chad"],
    "毛里塔尼亚": ["mauritania"],
    "阿尔巴尼亚": ["albania"],
}

# 反向：任何国家名 -> 中文标准名
ALIAS = {}
for cn, ens in CN_EN.items():
    ALIAS[cn.lower()] = cn
    for en in ens:
        ALIAS[en.lower()] = cn


def norm_country(x):
    """英文国名 -> 中文标准名；已是中文就归一。"""
    if not x:
        return ""
    k = str(x).strip().lower()
    if k in ALIAS:
        return ALIAS[k]
    # 中文里带括号的（刚果（金））
    for cn in CN_EN:
        if k == cn.lower():
            return cn
    return str(x).strip()


def other_countries_in(text, own):
    """摘要里出现了哪些「别的国家」。

    检索通道不保证结果与查询国家一致。实测搜 Mali 返回了
    「尼日利亚多地安全形势严峻」「阿富汗努里斯坦省山洪」。
    """
    t = str(text or "").lower()
    own_cn = norm_country(own)
    hit = set()
    for alias, cn in ALIAS.items():
        if cn == own_cn:
            continue
        if len(alias) <= 3 and alias.isascii():
            # 太短的别名（如 "is"，"us"）容易误命中，要求词边界
            if not re.search(r"\b" + re.escape(alias) + r"\b", t):
                continue
        elif alias not in t:
            continue
        hit.add(cn)
    return hit


# 百科词条：搜索会返回往年事件的百科页面，标题里带日期，极易被当成当月新闻。
# 实测 2026-07 马里自采里 8/13 条新增是这种。
# 月度汇总/多日合计/历史回顾：搜索结果里大量这类文章，
# 死亡数是整月或数日之和，当成单起事件会让台账总数严重虚高。
AGG_HINT = re.compile(
    r"(?i)("
    r"deadliest month|deadliest (?:of|in)|month of \w+|in july|in august|"
    r"martyred in|recorded (?:its|the)|over \d+ (?:killed|dead) in|"
    r"\bmonthly\b|\bthis month\b|\bsince july\b|\bso far this\b|"
    r"(?:attacks?|killed).{0,24}in (?:four|three|two|five) days|"
    r"主要事件|今年以来|本月以来|共造成|累计造成|月度|统计显示|数据显示"
    r")")

# 事故：爆炸厂、油罐车、火灾、交通事故。搜索"袭击 爆炸"会大量召回这些。
ACCIDENT_HINT = re.compile(
    r"(?i)("
    r"tank truck|fuel truck|oil tanker|fireworks|factory fire|"
    r"gas explosion|gas cylinder|mine collapse|building collapse|"
    r"\baccident\b|\bblaze\b|"
    r"油罐车|烟花|爆竹|火灾|爆炸事故|交通事故|翻车|坍塌|意外|事故"
    r")")

# 汇总片段：区间合计、括号统计。判定链的 SRC_AGGREGATE 认不出这种。
FRAGMENT_HINT = re.compile(
    r"(?i)("
    r"\(\s*\d+\s+killed\s*;"          # "(105 killed;"
    r"|between\s+\w+\s+\d+\s+and\s+\w+\s+\d+"   # "Between July 15 and 24"
    r"|on\s+\w+\s+\d+\s+and\s+\d+"                # "on July 27 and 28"
    r"|\bin \w+ \d{4} and \w+ \d{4}\b"              # "Between July 2025 and June 2026"
    r")")

# 反恐战果：政府方主动击杀
GOV_KILL_SELF = re.compile(
    r"(?i)((?:army|forces?|troops?|commandos?|security forces?|police)\s+"
    r"(?:kill|killed|kills|neutrali[sz]ed|eliminated)\s+"
    r"(?:at\s+least\s+)?\d+\s+"
    r"(?:militants?|terrorists?|insurgents?|gunmen))")

# 历史回顾：摘要含 20xx 年但不是目标年份 -> 是旧事件回顾
OLD_YEAR = re.compile(r"\b(19|20)\d{2}\s*年")

WIKI_HINT = re.compile(
    r"(百度百科|维基百科|搜狗百科|词条|wikiwand|wikipedia|"
    r"百科\s*[·:]|中文名\s|发生时间\s|伤亡情况\s)")


STOPW = {"killed", "kill", "kills", "attack", "attacks", "after", "with",
         "from", "that", "this", "were", "have", "been", "over", "into",
         "their", "they", "more", "than", "said", "report", "reports",
         "news", "people", "dead", "death", "deaths", "says", "about"}


def _words(t):
    """抽实词：英文取 4 字母以上，中文取 2 字以上的连续片段。"""
    t = str(t or "").lower()
    out = set(re.findall(r"[a-z]{4,}", t))
    out |= set(re.findall(r"[\u4e00-\u9fff]{2,}", t))
    return {w for w in out if w not in STOPW}


def _overlap(a, b):
    """两段摘要共有的实词数。用于确认是同一事件，而不只是同国同日。"""
    return len(_words(a) & _words(b))


def dedupe_key(summary):
    """摘要前 40 字（去标点）。同一条新闻的标题与正文常被拼在一起。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(summary or ""))
    return s[:40]


def merge(ucdp_rows, matrix_rows, date_tol=3, log=True):
    """合并。返回 (合并后候选, 统计)。"""
    st = {"matrix_in": len(matrix_rows), "drop_country": 0,
          "drop_dup": 0, "matched": 0, "added": 0}

    # UCDP 索引：国家 -> [(date, deaths, row)]
    idx = {}
    for r in ucdp_rows:
        c = norm_country(r.get("country"))
        idx.setdefault(c, []).append(r)

    seen = set()
    for r in ucdp_rows:
        seen.add(dedupe_key(r.get("summary")))

    new_rows = []
    for m in matrix_rows:
        c = norm_country(m.get("country"))
        s = m.get("summary") or ""

        # 0d) 事故
        if ACCIDENT_HINT.search(s):
            st["drop_accident"] = st.get("drop_accident", 0) + 1
            if log and st["drop_accident"] <= 3:
                print("    丢弃（事故）：%s" % s[:46])
            continue

        # 0e) 汇总片段
        if FRAGMENT_HINT.search(s):
            st["drop_frag"] = st.get("drop_frag", 0) + 1
            if log and st["drop_frag"] <= 3:
                print("    丢弃（汇总片段）：%s" % s[:46])
            continue

        # 0f) 反恐战果
        if GOV_KILL_SELF.search(s):
            st["drop_govkill"] = st.get("drop_govkill", 0) + 1
            if log and st["drop_govkill"] <= 3:
                print("    丢弃（反恐战果）：%s" % s[:46])
            continue

        # 0b) 月度汇总 / 多日合计 / 历史回顾
        if AGG_HINT.search(s):
            st["drop_agg"] = st.get("drop_agg", 0) + 1
            if log and st["drop_agg"] <= 3:
                print("    丢弃（汇总统计）：%s" % s[:46])
            continue
        _oy = OLD_YEAR.search(s)
        if _oy and TARGET_MONTH:
            _yy = str(int(_oy.group(0).replace("年", "").strip()))
            if not TARGET_MONTH.startswith(_yy):
                st["drop_oldyear"] = st.get("drop_oldyear", 0) + 1
                if log and st["drop_oldyear"] <= 3:
                    print("    丢弃（历史回顾 %s 年）：%s" % (_yy, s[:42]))
                continue

        # 0c) 死亡数过大：单起恐袭很少过百，几百的基本是汇总
        try:
            _big = int(m.get("deaths") or 0)
        except Exception:  # noqa: BLE001
            _big = 0
        if _big >= 150:
            st["drop_toobig"] = st.get("drop_toobig", 0) + 1
            if log and st["drop_toobig"] <= 3:
                print("    丢弃（死亡 %d 人，疑汇总）：%s" % (_big, s[:40]))
            continue

        # 0a) 死亡数异常：武装组织单方面宣称的夸大数字
        #     实测「Al-Shabaab said that it killed 321 soldiers」
        try:
            _dd = int(m.get("deaths") or 0)
        except Exception:  # noqa: BLE001
            _dd = 0
        if _dd > 200 and re.search(
                r"(said|claimed|claims|宣称|声称|扬言)", s, re.I):
            st["drop_boast"] = st.get("drop_boast", 0) + 1
            if log and st["drop_boast"] <= 2:
                print("    丢弃（组织宣称夸大 %d 人）：%s" % (_dd, s[:40]))
            continue

        # 0) 百科词条：直接丢
        if WIKI_HINT.search(s):
            st["drop_wiki"] = st.get("drop_wiki", 0) + 1
            if log and st["drop_wiki"] <= 3:
                print("    丢弃（百科词条）：%s" % s[:46])
            continue

        # 1) 国家校验
        others = other_countries_in(s, c)
        if others:
            st["drop_country"] += 1
            if log and st["drop_country"] <= 3:
                print("    丢弃（国家错配 %s ≠ %s）：%s"
                      % ("/".join(sorted(others)), c, s[:42]))
            continue

        # 2) 事件级去重：国家 + 死亡数接近 + 日期接近
        #    摘要前缀去重不够 —— 同一事件的标题版和正文版开头不同。
        _date = m.get("date") or ""
        _dv = m.get("deaths")
        is_dup = False
        for cand in new_rows:
            if norm_country(cand.get("country")) != c:
                continue
            try:
                if abs(int(cand.get("deaths") or 0) - int(_dv or 0)) > 2:
                    continue
            except Exception:  # noqa: BLE001
                pass
            cd = cand.get("date") or ""
            if _date and cd:
                try:
                    from datetime import date as _d
                    g = abs((_d(*[int(x) for x in _date.split("-")])
                             - _d(*[int(x) for x in cd.split("-")])).days)
                except Exception:  # noqa: BLE001
                    g = 99
                if g > 2:
                    continue
            is_dup = True
            # 保留摘要更长的那条
            if len(s) > len(str(cand.get("summary") or "")):
                cand["summary"] = s
            break
        if is_dup:
            st["drop_dup"] += 1
            continue

        # 2b) 日期闸门：抽到的日期若不在目标月份，说明是往年事件
        _md = m.get("date") or ""
        if _md and TARGET_MONTH and not str(_md).startswith(TARGET_MONTH):
            st["drop_outmonth"] = st.get("drop_outmonth", 0) + 1
            if log and st["drop_outmonth"] <= 3:
                print("    丢弃（%s 不是 %s）：%s"
                      % (_md, TARGET_MONTH, s[:44]))
            continue

        # 3) 与 UCDP 匹配：同国 + 日期接近 + 死亡数接近
        matched = None
        md = m.get("date") or ""
        mv = m.get("deaths")
        for u in idx.get(c, []):
            ud = u.get("date") or ""
            if md and ud:
                try:
                    from datetime import date as _d
                    d1 = _d(*[int(x) for x in md.split("-")])
                    d2 = _d(*[int(x) for x in ud.split("-")])
                    gap = abs((d1 - d2).days)
                except Exception:  # noqa: BLE001
                    gap = 99
                if gap > date_tol:
                    continue
            uv = u.get("deaths") or 0
            try:
                if abs(int(mv or 0) - int(uv)) > 2:
                    continue
            except Exception:  # noqa: BLE001
                pass
            # 还要求内容有交集，否则同国同日的两起不同事件会被误配。
            # 实测误配：UCDP「政治领袖身中七枪」被配上新闻「无人机袭击」。
            if _overlap(s, u.get("summary") or "") < 2:
                continue
            matched = u
            break

        if matched is not None:
            # 不新增事件，只补它的新闻出处
            # 实测：8 条匹配里只有 1-2 条真是同一事件。根因是 UCDP 摘要
            # 常是「Nine killed in attack」这种极简描述，与新闻标题
            # 几乎没有共同实词，靠摘要匹配判不准。
            # 所以默认不写回（避免把不相干新闻挂到 UCDP 事件上），
            # 只统计数量。需要时用 --with-match 打开。
            st["matched"] += 1
            if not WITH_MATCH:
                continue
            cite = str(matched.get("source_citation") or "")
            hint = "新闻印证:" + str(s)[:50].replace("|", " ")
            if hint not in cite:
                matched["source_citation"] = (cite + " ｜ " + hint).strip(" ｜")
            if not matched.get("url") and m.get("url"):
                matched["url"] = m["url"]
            continue

        # 4) 确实是 UCDP 没有的，作为新事件
        st["added"] += 1
        new_rows.append({
            "source": "matrix",
            "date": md,
            "country": c,
            "place": m.get("place") or "",
            "summary": s,
            "deaths": mv if isinstance(mv, int) else 0,
            "injured": m.get("injured") or 0,
            "actor_a": "",
            "actor_b": "",
            "act_type": "",
            "url": m.get("url") or "",
            "source_citation": "自采:" + str(s)[:60],
            "raw_id": "",
            "extra": {"from": "self-harvest", "low_conf": m.get("low_conf", True)},
        })

    return ucdp_rows + new_rows, st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ucdp", required=True, help="UCDP 候选 jsonl")
    ap.add_argument("--matrix", required=True, help="自采候选 jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--date-tol", type=int, default=3, help="日期容差天数")
    ap.add_argument("--with-match", action="store_true",
                    help="把匹配到的新闻写回 UCDP 事件的 source_citation。"
                         "默认关闭 —— 实测 8 条匹配里只有 1-2 条真是同一事件")
    ap.add_argument("--clue-only", action="store_true", default=True,
                    help="自采候选只出线索清单，不并入主台账（默认）")
    ap.add_argument("--merge", dest="clue_only", action="store_false",
                    help="把自采候选并入主台账。慎用 —— 实测信噪比约 1:3")
    ap.add_argument("--clue-out", default=None,
                    help="线索清单输出路径（默认与 --out 同目录的 自采线索.jsonl）")
    ap.add_argument("--month", default="",
                    help="目标月份 YYYY-MM。给了就剔除抽到别的月份的自采候选"
                         "（搜索会返回往年百科词条）")
    a = ap.parse_args()
    global TARGET_MONTH, WITH_MATCH
    TARGET_MONTH = a.month or ""
    WITH_MATCH = bool(a.with_match)

    def load(p):
        out = []
        for line in io.open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    u = load(a.ucdp)
    m = load(a.matrix)
    print("")
    print("  合并自采候选：UCDP %d 条 ＋ 自采 %d 条" % (len(u), len(m)))

    rows, st = merge(u, m, a.date_tol)

    if a.clue_only:
        # 线索模式：主台账只留 UCDP，自采候选单独出清单
        out_rows = [r for r in rows if r.get("source") != "matrix"]
        clue_path = a.clue_out or os.path.join(
            os.path.dirname(os.path.abspath(a.out)), "自采线索.jsonl")
        with io.open(clue_path, "w", encoding="utf-8") as f:
            for r in rows:
                if r.get("source") == "matrix":
                    r["clue"] = "疑似 UCDP 未收录，需人工核实"
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("")
        print("  自采线索另存 → %s（%d 条，需人工核）"
              % (clue_path, st["added"]))
        print("  主台账不含自采候选 —— 实测其信噪比约 1:3，")
        print("  并入会拉低整体精确率，所以默认只作线索。")
    else:
        out_rows = rows

    with io.open(a.out, "w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if st.get("drop_accident"):
        print("    丢弃事故     : %d" % st["drop_accident"])
    if st.get("drop_frag"):
        print("    丢弃汇总片段 : %d" % st["drop_frag"])
    if st.get("drop_govkill"):
        print("    丢弃反恐战果 : %d" % st["drop_govkill"])
    if st.get("drop_agg"):
        print("    丢弃汇总统计 : %d" % st["drop_agg"])
    if st.get("drop_oldyear"):
        print("    丢弃历史回顾 : %d" % st["drop_oldyear"])
    if st.get("drop_toobig"):
        print("    丢弃死亡过大 : %d" % st["drop_toobig"])
    if st.get("drop_boast"):
        print("    丢弃夸大宣称 : %d" % st["drop_boast"])
    if st.get("drop_wiki"):
        print("    丢弃百科词条 : %d" % st["drop_wiki"])
    print("    丢弃国家错配 : %d" % st["drop_country"])
    if st.get("drop_outmonth"):
        print("    丢弃往年事件 : %d" % st["drop_outmonth"])
    print("    丢弃重复摘要 : %d" % st["drop_dup"])
    print("    与 UCDP 匹配 : %d（不新增，只补新闻出处）" % st["matched"])
    print("    确认新增事件 : %d" % st["added"])
    print("  合计 → %d 条" % len(out_rows))
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
