#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""覆盖率审计：权威源之间互相覆盖了多少、还漏多少、该不该跑信源表。

要回答的问题
------------
用户提出一个关键判断：「UCDP 和 ACLED 若已覆盖大多数事件，那就是最优情况，
因为权威库比新闻报道可信；若覆盖不足，才必须做信源表逐家遍历。」

这个判断要成立，前提是**先量出来**。本脚本就是那把尺子：

  1. **两源重叠率**：UCDP 的事件有多少能在 ACLED 里找到？
     重叠高 → 两个源互相印证，可信度高，且说明单源不会大漏。
     重叠低 → 两个源各看各的，谁都不完整，必须补第三个维度。

  2. **并集规模**：两个源合起来有多少独立事件。
     这是「不跑信源表时的下限」，也是判断是否需要补漏的基准。

  3. **疑似孤例**：只在一个源里出现的事件。
     这些既可能是另一源漏了，也可能是本源的误报，需要交叉核验。

  4. **媒体引用偏向**：ACLED 的 `source` 字段里有媒体名。
     统计它的媒体分布，与信源表名录对比，就能看出「权威库其实只依赖少数几家媒体」，
     从而判断信源表遍历该重点覆盖哪些媒体。

匹配方法
--------
不做精确 ID 匹配（两个源没有共同 ID），用「事件指纹 + 容差」：

    同国家（名称标准化后）
    且 日期相差 ≤ 1 天
    且 死亡数相差 ≤ max(1, 较大值的 20%)

为什么容差要放开：两个源对同一起事件常记不同日期（事发 vs 报道）、
不同死亡数（不同消息源）。实测 2025-08 大马士革同一起爆炸，
一个源记 10 死、另一个记 6 死。

用法
----
  python scripts/audit_coverage.py --src ucdp=ucdp.jsonl --src acled=acled.jsonl
  python scripts/audit_coverage.py --src ucdp=a.jsonl --src acled=b.jsonl \\
      --registry 信源表.xlsx --out 覆盖率审计.md
"""
import argparse
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 国家名标准化：两个源对同一国家常有不同写法
COUNTRY_ALIAS = {
    "democratic republic of congo": "congo, dr",
    "dr congo": "congo, dr",
    "congo, democratic republic": "congo, dr",
    "drc": "congo, dr",
    "congo": "congo",
    "republic of the congo": "congo",
    "cote d'ivoire": "ivory coast",
    "côte d'ivoire": "ivory coast",
    "myanmar": "myanmar",
    "burma": "myanmar",
    "russia": "russia",
    "russian federation": "russia",
    "united states": "united states",
    "usa": "united states",
    "united kingdom": "united kingdom",
    "uk": "united kingdom",
    "syrian arab republic": "syria",
    "iran (islamic republic of)": "iran",
    "turkey": "turkey",
    "türkiye": "turkey",
    "south korea": "south korea",
    "korea, republic of": "south korea",
    "north korea": "north korea",
    "vietnam": "vietnam",
    "viet nam": "vietnam",
    "laos": "laos",
    "philippines": "philippines",
    "macedonia": "north macedonia",
    "czech republic": "czechia",
}


# 中文 → 英文：UCDP 侧的国家名被 harvest_ucdp.py 转成了中文，
# 而 ACLED 侧是英文。不统一就会得到「两源重叠 0.1%」这种假结论 ——
# 实测第一次跑就是这个数，差点得出「两个权威库几乎不重叠」的错误判断。
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from harvest_multi_source import COUNTRY_ZH as _ZH
    ZH_TO_EN = {}
    for _en, _zh in _ZH.items():
        ZH_TO_EN.setdefault(_zh.lower(), _en.lower())
except Exception:  # noqa: BLE001
    ZH_TO_EN = {}

# 补充一些两个源常见但映射表没有的写法
ZH_TO_EN.update({
    "巴勒斯坦": "palestine", "以色列": "israel", "伊朗": "iran",
    "土耳其": "turkey", "也门": "yemen", "黎巴嫩": "lebanon",
    "约旦": "jordan", "伊拉克": "iraq", "阿富汗": "afghanistan",
    "印度": "india", "孟加拉国": "bangladesh", "斯里兰卡": "sri lanka",
    "尼泊尔": "nepal", "缅甸": "myanmar", "泰国": "thailand",
    "菲律宾": "philippines", "印度尼西亚": "indonesia", "马来西亚": "malaysia",
    "俄罗斯": "russia", "乌克兰": "ukraine", "美国": "united states",
    "英国": "united kingdom", "法国": "france", "德国": "germany",
    "西班牙": "spain", "意大利": "italy", "希腊": "greece",
    "哥伦比亚": "colombia", "墨西哥": "mexico", "巴西": "brazil",
    "委内瑞拉": "venezuela", "秘鲁": "peru", "厄瓜多尔": "ecuador",
    "海地": "haiti", "洪都拉斯": "honduras", "危地马拉": "guatemala",
    "萨尔瓦多": "el salvador", "智利": "chile", "阿根廷": "argentina",
    "玻利维亚": "bolivia", "巴拉圭": "paraguay", "古巴": "cuba",
    "牙买加": "jamaica", "加拿大": "canada", "澳大利亚": "australia",
    "中国": "china", "日本": "japan", "韩国": "south korea",
    "朝鲜": "north korea", "越南": "vietnam", "柬埔寨": "cambodia",
    "老挝": "laos", "蒙古": "mongolia", "哈萨克斯坦": "kazakhstan",
    "塔吉克斯坦": "tajikistan", "乌兹别克斯坦": "uzbekistan",
    "格鲁吉亚": "georgia", "亚美尼亚": "armenia", "阿塞拜疆": "azerbaijan",
    "白俄罗斯": "belarus", "摩尔多瓦": "moldova", "塞尔维亚": "serbia",
    "波黑": "bosnia and herzegovina", "科索沃": "kosovo",
    "北马其顿": "north macedonia", "阿尔巴尼亚": "albania",
    "沙特阿拉伯": "saudi arabia", "阿联酋": "united arab emirates",
    "卡塔尔": "qatar", "科威特": "kuwait", "阿曼": "oman",
    "巴林": "bahrain", "叙利亚": "syria", "塞浦路斯": "cyprus",
    "南苏丹": "south sudan", "苏丹": "sudan", "利比亚": "libya",
    "刚果（金）": "democratic republic of the congo",
    "刚果（布）": "congo", "中非": "central african republic",
    "布基纳法索": "burkina faso", "喀麦隆": "cameroon",
    "尼日尔": "niger", "尼日利亚": "nigeria", "乍得": "chad",
    "毛里塔尼亚": "mauritania", "塞内加尔": "senegal",
    "几内亚": "guinea", "塞拉利昂": "sierra leone",
    "利比里亚": "liberia", "加纳": "ghana", "多哥": "togo",
    "贝宁": "benin", "科特迪瓦": "cote d'ivoire",
    "布隆迪": "burundi", "卢旺达": "rwanda", "乌干达": "uganda",
    "肯尼亚": "kenya", "坦桑尼亚": "tanzania", "莫桑比克": "mozambique",
    "津巴布韦": "zimbabwe", "赞比亚": "zambia", "马拉维": "malawi",
    "安哥拉": "angola", "纳米比亚": "namibia", "博茨瓦纳": "botswana",
    "南非": "south africa", "埃塞俄比亚": "ethiopia", "厄立特里亚": "eritrea",
    "吉布提": "djibouti", "索马里": "somalia", "阿尔及利亚": "algeria",
    "摩洛哥": "morocco", "突尼斯": "tunisia", "埃及": "egypt",
})


def norm_country(s):
    t = re.sub(r"\s+", " ", str(s or "").strip().lower())
    t = t.replace("（", "(").replace("）", ")")
    # 中文名先转英文，保证两边用同一个键
    if t in ZH_TO_EN:
        return ZH_TO_EN[t]
    return COUNTRY_ALIAS.get(t, t)


def to_int(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("—", "-", "None", "nan"):
        return None
    m = re.search(r"-?\d+", s)
    return int(m.group(0)) if m else None


def norm_date(s):
    t = str(s or "").strip()[:10]
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
    return ""


def day_num(d):
    """把 YYYY-MM-DD 转成可比较的序号，失败返回 None。"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d or "")
    if not m:
        return None
    import datetime
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)),
                             int(m.group(3))).toordinal()
    except ValueError:
        return None


def load(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return rows


def prep(rows):
    """统一成可比较的元组。"""
    out = []
    for r in rows:
        cty = norm_country(r.get("country"))
        d = norm_date(r.get("date"))
        dd = to_int(r.get("deaths"))
        dn = day_num(d)
        if not cty or dn is None:
            continue
        ex = r.get("extra") or {}
        lat, lon = ex.get("lat"), ex.get("lon")
        try:
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except Exception:  # noqa: BLE001
            lat = lon = None
        out.append({"cty": cty, "date": d, "day": dn,
                    "deaths": dd if dd is not None else 0,
                    "deaths_known": dd is not None,
                    "lat": lat, "lon": lon,
                    "rec": r})
    return out


def deaths_close(a, b):
    """死亡数是否接近：差值 ≤ max(1, 较大值的 20%)。"""
    hi = max(a, b)
    tol = max(1, int(hi * 0.2))
    return abs(a - b) <= tol


STOP = set("""a an the on in at of to for and or by with from as is are was were be been
has have had this that these those it its their they he she his her not no but if then than
into over under after before during near outside inside across from about against while""".split())


def tokens(s):
    """摘要词袋。英文取 3 字以上词（去停用词），中文取 2-gram。"""
    t = str(s or "").lower()
    en = {w for w in re.findall(r"[a-z]{3,}", t) if w not in STOP}
    zh = set()
    for seg in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(seg) == 1:
            zh.add(seg)
        for i in range(len(seg) - 1):
            zh.add(seg[i:i + 2])
    return en | zh


def sim(a, b):
    """Jaccard 相似度。"""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def haversine(lat1, lon1, lat2, lon2):
    """两点球面距离（公里）。"""
    import math
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, a ** 0.5))


def count_overlap(A, B, min_sim=0.22, max_km=10.0):
    """A 里有多少条能在 B 里找到。

    只靠「同国 + 日期±1 + 死亡接近」是不够的：在乌克兰这种高密度冲突区，
    随便两条都能满足，会得出 77.6% 的假重叠（实测抽查 8 对里 6 对是错配）。
    必须再加摘要相似度，让「同一事件」在文字上也对得上。
    """
    idx = defaultdict(list)
    for x in B:
        idx[x["cty"]].append(x)

    hit, matched_pairs = 0, []
    for a in A:
        cands = idx.get(a["cty"], [])
        best, best_d = None, 1e9
        for b in cands:
            if abs(b["day"] - a["day"]) > 1:
                continue
            # 坐标是首选判据：同一起事件，两个库记的经纬度会很接近。
            # 文字相似度不可靠 —— 实测 1678 对候选里只有 3.3% 的文字相似度
            # 超过 0.25，因为 UCDP 写「Army: an officer was killed」，
            # ACLED 写「On 28 August 2025, an Israeli drone exploded...」，
            # 同一件事两种叙述风格，字面几乎不重合。
            dkm = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            if dkm is None:
                # 没有坐标才退回文字匹配
                s0 = sim(a["rec"].get("summary"), b["rec"].get("summary"))
                if s0 >= max(min_sim, 0.30):
                    best, best_d = b, 0
                continue
            if dkm <= max_km and dkm < best_d:
                best, best_d = b, dkm
        if best is not None:
            hit += 1
            matched_pairs.append((a, best))
    return hit, matched_pairs


def main():
    ap = argparse.ArgumentParser(description="权威源覆盖率审计")
    ap.add_argument("--src", action="append", default=[],
                    help="name=path，可给多个。例：--src ucdp=a.jsonl --src acled=b.jsonl")
    ap.add_argument("--registry", default=None, help="信源表 xlsx（用于媒体偏向对比）")
    ap.add_argument("--out", default=None, help="输出 markdown 报告")
    ap.add_argument("--max-print", type=int, default=12, help="每组样例打印几条")
    a = ap.parse_args()

    if not a.src:
        print("至少给一个 --src name=path", file=sys.stderr)
        return 1

    srcs = {}
    for spec in a.src:
        if "=" not in spec:
            print("--src 要写成 name=path：%s" % spec, file=sys.stderr)
            return 1
        name, path = spec.split("=", 1)
        rows = load(path)
        if not rows:
            print("⚠️  %s 读取为空或文件不存在：%s" % (name, path), file=sys.stderr)
            continue
        srcs[name] = prep(rows)
        print("%-10s %6d 条（可比较 %d）" % (name, len(rows), len(srcs[name])))

    if not srcs:
        print("没有可用的数据源", file=sys.stderr)
        return 1

    names = list(srcs.keys())
    lines = []
    lines.append("# 覆盖率审计")
    lines.append("")
    lines.append("## 一、各源规模")
    lines.append("")
    lines.append("| 数据源 | 记录数 | 可比对（有国家+日期） |")
    lines.append("|---|---|---|")
    for n in names:
        lines.append("| %s | %d | %d |" % (n, len(srcs[n]), len(srcs[n])))
    lines.append("")

    # ---- 两两重叠 ----
    lines.append("## 二、两源重叠（同国 + 日期±1天 + 死亡数接近）")
    lines.append("")
    lines.append("| A | B | A 能在 B 找到 | 占 A | 判断 |")
    lines.append("|---|---|---|---|---|")
    pairs = []
    for i in range(len(names)):
        for j in range(len(names)):
            if i == j:
                continue
            A, B = srcs[names[i]], srcs[names[j]]
            hit, mp = count_overlap(A, B)
            rate = 100.0 * hit / len(A) if A else 0
            if rate >= 70:
                verdict = "高度重叠：两源互相印证，可信"
            elif rate >= 40:
                verdict = "部分重叠：各有独立记录，需补维度"
            else:
                verdict = "**低重叠：两源各看各的，单源必漏**"
            lines.append("| %s | %s | %d/%d | %.1f%% | %s |"
                         % (names[i], names[j], hit, len(A), rate, verdict))
            pairs.append((names[i], names[j], hit, len(A), rate, mp))
    lines.append("")

    # ---- 并集估算 ----
    # 用最大重叠对做保守去重：并集 ≈ |A| + |B| - overlap
    lines.append("## 三、并集规模（保守估算）")
    lines.append("")
    if len(names) >= 2:
        best = None
        for p in pairs:
            if best is None or p[4] > best[4]:
                best = p
        if best:
            nA, nB, hit = len(srcs[best[0]]), len(srcs[best[1]]), best[2]
            union = nA + nB - hit
            lines.append("- %s %d 条 + %s %d 条 − 重叠 %d 条 ≈ **%d 条独立事件**"
                         % (best[0], nA, best[1], nB, hit, union))
            lines.append("- 覆盖率只能证明「这两个源合起来至少有这么多」，"
                         "**不能证明「全球这么多个」**")
    else:
        lines.append("- 只给了一个源，无法估算并集")
    lines.append("")

    # ---- 孤例 ----
    lines.append("## 四、只在单一源出现的事件（需交叉核验）")
    lines.append("")
    if len(names) >= 2:
        for p in pairs:
            if p[2] == 0:
                continue
            A_name, B_name, hit, total, rate, mp = p
            matched_a = {id(x[0]["rec"]) for x in mp}
            lonely = [x for x in srcs[A_name] if id(x["rec"]) not in matched_a]
            lines.append("- **%s 独有 %d 条**（%s 里找不到对应）"
                         % (A_name, len(lonely), B_name))
            for x in lonely[:a.max_print]:
                r = x["rec"]
                lines.append("    - %s %s 死%s ｜ %s"
                             % (r.get("country"), x["date"], x["deaths"],
                                str(r.get("summary") or "")[:70]))
            if len(lonely) > a.max_print:
                lines.append("    - … 另有 %d 条" % (len(lonely) - a.max_print))
            lines.append("")
    else:
        lines.append("- 只给了一个源，跳过")
    lines.append("")

    # ---- 媒体引用偏向 ----
    lines.append("## 五、权威库依赖的媒体（判断信源表该重点覆盖谁）")
    lines.append("")
    media = Counter()
    for n in names:
        for x in srcs[n]:
            r = x["rec"]
            s = (r.get("extra") or {}).get("acled_source") or r.get("source_office") or ""
            if not s:
                continue
            for m in re.split(r"[;,]", str(s)):
                m = m.strip()
                if m and len(m) > 2:
                    media[m] += 1
    if media:
        lines.append("| 媒体 | 被引用次数 |")
        lines.append("|---|---|")
        for m, c in media.most_common(20):
            lines.append("| %s | %d |" % (m[:48], c))
        lines.append("")
        lines.append("媒体引用总去重家数：**%d**" % len(media))

        # 与信源表对比
        if a.registry and os.path.isfile(a.registry):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(a.registry, data_only=True)
                ws = None
                for w in wb.worksheets:
                    hdr0 = [str(c or "") for c in next(w.iter_rows(values_only=True), [])]
                    if any("域名" in h for h in hdr0):
                        ws = w
                        break
                reg_domains = set()
                if ws:
                    rows0 = list(ws.iter_rows(values_only=True))
                    hdr = [str(h or "").strip() for h in rows0[0]]
                    idom = next((i for i, h in enumerate(hdr) if "主域名" in h), None)
                    iname = next((i for i, h in enumerate(hdr) if "机构名称" in h), None)
                    for r0 in rows0[1:]:
                        if not r0:
                            continue
                        if idom is not None and r0[idom]:
                            reg_domains.add(str(r0[idom]).lower().replace("www.", ""))
                        if iname is not None and r0[iname]:
                            reg_domains.add(str(r0[iname]).lower())
                hit_media = 0
                for m in media:
                    ml = m.lower()
                    if any(d and (d in ml or ml in d) for d in reg_domains if len(d) > 3):
                        hit_media += 1
                lines.append("")
                lines.append("信源表名录：**%d** 家 ｜ 权威库引用的媒体里，"
                             "能在名录找到的：**%d / %d = %.0f%%**"
                             % (len(reg_domains), hit_media, len(media),
                                100.0 * hit_media / len(media) if media else 0))
                lines.append("")
                lines.append("> 这个比例说明：权威库实际依赖的媒体，与我们的信源表重叠多少。")
                lines.append("> 重叠低 → 信源表遍历能补到权威库没有的媒体，值得跑。")
                lines.append("> 重叠高 → 信源表遍历主要是给权威库的事件补链接，不是补事件。")
            except Exception as e:  # noqa: BLE001
                lines.append("- （信源表读取失败：%s）" % str(e)[:80])
    else:
        lines.append("- 没有媒体字段（UCDP 的 source_office 可能为空）")
    lines.append("")

    # ---- 结论 ----
    lines.append("## 六、结论与下一步")
    lines.append("")
    if pairs:
        best = max(pairs, key=lambda p: p[4])
        lines.append("- 最高重叠：**%s → %s %.1f%%**" % (best[0], best[1], best[4]))
        if best[4] >= 70:
            lines.append("- 判断：**两个权威源高度重叠**，可以作为主干；"
                         "此时信源表遍历的主要任务是**给权威库事件补可追溯链接**，"
                         "而不是补事件。")
        elif best[4] >= 40:
            lines.append("- 判断：**部分重叠**，两源各有独立记录。"
                         "信源表遍历既补事件也补链接。")
        else:
            lines.append("- 判断：**低重叠，两源各看各的**。"
                         "单靠权威库会系统性漏事件，**必须做信源表逐家遍历**。")
    lines.append("")

    text = "\n".join(lines)
    print()
    print(text)

    if a.out:
        d = os.path.dirname(os.path.abspath(a.out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print()
        print("已写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
