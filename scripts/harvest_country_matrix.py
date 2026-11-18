#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按「国家 × 事件类型 × 月份」做组合遍历，捞权威库没收录的事件。

为什么不是「逐家媒体搜」
------------------------
2026-10 实测过：113 家 P1 信源逐家遍历，11 条候选、0 条有效增量。
不是因为遍历没用，是因为**搜法不对** —— 逐家搜等于问「你这家本月报了啥」，
媒体不会这么组织内容。

正确的做法是反过来：从**事件**出发，用「国家 × 事件类型 × 月份」组合查询。
事件类型是有限通用词（attack / ambush / IED / kidnapping / raid...），
可以**先验枚举**，不需要知道答案。

实测（2026-07，10 国，20 起媒体独家事件当靶子）：
  每国 3 条通用查询         → 可发现 65%
  每国 9 条（国家×类型）     → 可发现 90%
  96 条查询 / 115 秒

多语言是必需的：法语对萨赫勒三国、索马里语对索马里、阿拉伯语对叙利亚，
都实测有效（英文查不到的事件，本地语言能查到）。

用法
----
  python scripts/harvest_country_matrix.py --month 2026-07 \\\\
      --countries Somalia,Nigeria,Mali --search-cmd 'yb.cmd search "{q}" --cnt 20' \\\\
      --out candidates.jsonl

  # 国家列表也可以从文件读
  python scripts/harvest_country_matrix.py --month 2026-07 \\\\
      --countries-file countries.txt --search-cmd '...'

产出
----
每行一个候选，字段与 01_候选.jsonl 对齐，可直接喂给 judge_events.py：
  source="matrix", date, country, place, summary, deaths, url, raw_text

抽取规则是**保守**的：只保留同时出现「死亡数」和「暴力动词」的句子。
不追求高召回，追求「捞出来的每一条都值得看一眼」。
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 英文模板：8 种通用事件类型
BLOCK_MODE = False   # True=按新闻块抽（候选少但噪声高），False=按句抽
LOOSE_MODE = False   # True=死亡数可选（命中更高但噪声大），False=必须有死亡数

TPL_EN = [
    "{c} {m} {y} attack killed soldiers",
    "{c} {m} {y} ambush military convoy",
    "{c} {m} {y} IED explosion killed",
    "{c} {m} {y} kidnapping abduction civilians",
    "{c} {m} {y} suicide bombing",
    "{c} {m} {y} raid army base militants",
    "{c} {m} {y} gunmen killed police",
    "{c} {m} {y} terrorists attacked village",
]

TPL_FR = [
    "{c} {m} {y} attaque soldats tués",
    "{c} {m} {y} embuscade convoi militaire",
    "{c} {m} {y} attentat engin explosif",
    "{c} {m} {y} enlèvement civils",
]

TPL_SO = [
    "{c} {m} {y} weerar saldhig ciidan",
    "{c} {m} {y} weerar qarax",
]

TPL_AR = [
    "{c} {m} {y} هجوم مسلح قتلى",
    "{c} {m} {y} انفجار عبوة ناسفة",
]

# 中文模板 —— 中文检索通道返回的是中文报道，用中文查+中文匹配才有效
# 实测：英文查询经元宝返回的也是中文报道（新华社等），英文地名匹配不上，
# 改用中文查询后马里 4/6 中文地名命中（加奥/阿内菲斯/阿盖洛克/巴马科）
TPL_ZH = [
    "{c} {m} 袭击 死亡",
    "{c} {m} 军方 遭袭 士兵死亡",
    "{c} {m} 武装分子 袭击 村庄",
    "{c} {m} 爆炸 死亡",
    "{c} {m} 绑架 平民",
]

ZH_NAME = {
    "Mali": "马里", "Niger": "尼日尔", "Burkina Faso": "布基纳法索",
    "Chad": "乍得", "Senegal": "塞内加尔", "Cameroon": "喀麦隆",
    "Guinea": "几内亚", "Benin": "贝宁", "Togo": "多哥",
    "Somalia": "索马里", "Nigeria": "尼日利亚", "Syria": "叙利亚",
    "Iraq": "伊拉克", "Yemen": "也门", "Egypt": "埃及",
    "Libya": "利比亚", "Saudi Arabia": "沙特阿拉伯",
    "Pakistan": "巴基斯坦", "Afghanistan": "阿富汗",
    "India": "印度", "Bangladesh": "孟加拉国",
    "DR Congo": "刚果（金）", "Mozambique": "莫桑比克",
    "Sudan": "苏丹", "Kenya": "肯尼亚", "Colombia": "哥伦比亚",
    "Philippines": "菲律宾", "Indonesia": "印度尼西亚",
    "Thailand": "泰国", "Turkey": "土耳其", "Iran": "伊朗",
    "Central African Republic": "中非", "Uganda": "乌干达",
    "Ethiopia": "埃塞俄比亚", "Tanzania": "坦桑尼亚",
    "Algeria": "阿尔及利亚", "Tunisia": "突尼斯", "Morocco": "摩洛哥",
    "Lebanon": "黎巴嫩", "Israel": "以色列", "Palestine": "巴勒斯坦",
    "Russia": "俄罗斯", "Ukraine": "乌克兰", "Myanmar": "缅甸",
    "Mexico": "墨西哥", "Brazil": "巴西", "Ecuador": "厄瓜多尔",
    "Haiti": "海地", "Sri Lanka": "斯里兰卡", "Nepal": "尼泊尔",
}

FR_COUNTRIES = {"Mali", "Niger", "Burkina Faso", "Chad", "Senegal",
                "Cameroon", "Guinea", "Benin", "Togo", "Cote d'Ivoire"}
SO_COUNTRIES = {"Somalia"}
AR_COUNTRIES = {"Syria", "Iraq", "Yemen", "Egypt", "Libya", "Saudi Arabia"}

MONTH_NAME = {1: "January", 2: "February", 3: "March", 4: "April",
              5: "May", 6: "June", 7: "July", 8: "August",
              9: "September", 10: "October", 11: "November",
              12: "December"}
MONTH_FR = {1: "janvier", 2: "février", 3: "mars", 4: "avril", 5: "mai",
            6: "juin", 7: "juillet", 8: "août", 9: "septembre",
            10: "octobre", 11: "novembre", 12: "décembre"}

# 死亡数线索（多语言）
DEATH_PAT = re.compile(
    r"(\d{1,4})\s*(?:people\s+|civilians\s+|soldiers\s+|persons\s+)?"
    r"(?:were\s+)?(?:killed|dead|died|mort|tués|tues|décédé)", re.I)
DEATH_PAT2 = re.compile(r"(?:killed|death\s+toll\s+of)\s+(\d{1,4})", re.I)
DEATH_PAT_ZH = re.compile(r"(\d{1,4})\s*(?:人)?\s*(?:死亡|遇难|丧生|被杀)")
VIOLENT = re.compile(
    r"attack|ambush|assault|raid|bomb|explos|gunmen|militant|terror|"
    r"insurgen|killed|attaque|embuscade|attentat|explosif|"
    r"weerar|qarax|هجوم|انفجار|袭击|爆炸|伏击|恐袭", re.I)


def clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_search(cmd_tpl, q, timeout=90):
    cmd = cmd_tpl.replace("{q}", q)
    try:
        if os.name == "nt":
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               env=clean_env(), timeout=timeout, shell=True)
        else:
            import shlex
            r = subprocess.run(shlex.split(cmd), capture_output=True,
                               text=True, encoding="utf-8",
                               errors="replace", env=clean_env(),
                               timeout=timeout)
        return (r.stdout or "") + "\n" + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return "ERR:" + str(e)[:80]


def strip_json_noise(txt):
    """去掉检索结果里的 JSON 结构噪声。

    检索通道常返回 JSON，同一条新闻会以 passage / title / content 三个字段
    各出现一次 —— 不过滤的话一条新闻会变成三条候选，去重也去不掉
    （因为外层还裹着 "passage": ��类字段名，文本不完全相同）。
    实测：马里一条「车队遭伏击 逾50人死亡」被拆成 5 条候选。
    """
    # 去掉字段名与引号： "passage": "  /  "title":"  /  "content": "
    t = re.sub(r'"{1}[A-Za-z_]{2,20}"{1}\s*:\s*"{1}', ' ', txt)
    # 去掉残留的转义与括号
    t = t.replace('\\n', ' ').replace('\\"', ' ').replace('"', ' ')
    t = re.sub(r'[{}]', ' ', t)
    return t


def split_sents(txt):
    """按句切分（默认）。

    四档实测对比（2026-07，20 起已知靶子）：
      粒度/门槛            计入条数  靶子命中  零死亡占比
      按句 + 要求死亡数      412     50%       0%     ← 默认，性价比最好
      按句 + 暴力动词        2330     75%      84%
      按句 + 暴力动词+数字   1598     55%      77%
      按块 + 暴力动词        1009     55%      72%

    结论：不是「调阈值」能解决的。要求死亡数虽然命中率只有 50%，
    但 0% 零死亡噪声，交付时质量可控 —— 另三档换来的是几倍噪声。
    """
    txt = strip_json_noise(txt)
    parts = re.split(r"(?<=[.!?。！？;؛])\s+|\n+", txt)
    return [p.strip() for p in parts if len(p.strip()) >= 25]


def split_blocks(txt, size=700):
    """按新闻块切分，而不是按句。

    为什么不能按句：一条新闻里地名常在标题、死亡数在正文，
    按句切会把它们拆进两个候选 —— 一个「有地名没死亡数」、
    一个「有死亡数没地名」，两边都过不了抽取门槛。
    实测按句切时，20 起已知靶子只命中 50%。

    按块切保证同一篇报道的地名与死亡数待在一起。
    """
    txt = strip_json_noise(txt)
    # 先按常见的结果分隔特征切
    chunks = re.split(r"\n\s*\n+|(?=标题[:：])|(?=【)|(?=\d+\.\s)", txt)
    out = []
    for c in chunks:
        c = c.strip()
        if len(c) < 60:
            continue
        # 太长的再按 size 硬切，避免一条候选塞进十篇报道
        while len(c) > size * 3:
            out.append(c[:size * 3])
            c = c[size * 3:]
        out.append(c)
    return [x for x in out if len(x) >= 60]


DATE_PAT = re.compile(
    r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", re.I)


def grab_date(s, year, month):
    """从句子/上下文里抽日期，返回 YYYY-MM-DD。

    这个字段必须填 —— 判定阶段的去重键是「国家+日期+死亡数」，
    date 为空会让同国同死亡数的所有事件被并成一条。
    实测：1092 条候选因 date 全空，被去重成 338 条，靶子命中率从
    应有的 ~90% 掉到 1/20。
    """
    m = DATE_PAT.search(s)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return "%s-%02d-%02d" % (y, mo, d)
    return ""


def extract(text, country, year, month):
    """从检索文本里抽候选事件。保守：死亡数 + 暴力动词 都要有。"""
    out = []
    for s in (split_blocks(text) if BLOCK_MODE else split_sents(text)):
        if not VIOLENT.search(s):
            continue
        # 块内取最大的死亡数（一条新闻里可能提到多个数字，取最大通常是主案）
        d = None
        for pat in (DEATH_PAT, DEATH_PAT2, DEATH_PAT_ZH):
            for m in pat.finditer(s):
                try:
                    v = int(m.group(1))
                except Exception:  # noqa: BLE001
                    continue
                if 0 < v <= 5000 and (d is None or v > d):
                    d = v
        # 门槛取中间档。三档实测（20 起已知靶子）：
        #   要求死亡数        → 412 条计入，靶子命中 50%
        #   只要暴力动词      → 2330 条计入，靶子命中 75%，但 84% 是零死亡噪声
        #   暴力动词 + 含数字 → 本档，见下
        # 中间档要求句子里有数字（死亡数或日期都算），能滤掉
        # 「武装分子袭击了该地区」这类完全无信息的句子。
        # 默认：必须有死亡数。实测这是性价比最好的一档 ——
        # 命中率 50%，但零死亡噪声 0%，交付质量可控。
        # 放宽到「有数字就行」命中 55%、噪声 77%；放宽到「只要暴力动词」
        # 命中 75%、噪声 84%。多出的命中率不值得几倍噪声。
        if d is None:
            if not LOOSE_MODE:
                continue
            if not re.search(r"\d", s):
                continue
            d = 0
        if len(s) > 600:
            s = s[:600]
        out.append({
            "source": "matrix",
            "date": grab_date(s, year, month),
            "country": country,
            "place": "",
            "summary": s,
            "deaths": d,
            "low_conf": (d == 0),
            "injured": 0,
            "actor_a": "",
            "actor_b": "",
            "act_type": "",
            "url": "",
            "raw_month": "%s-%02d" % (year, month),
        })
    return out


def main():
    ap = argparse.ArgumentParser(
        description="按国家×事件类型×月份组合遍历，捞权威库没收录的事件")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--countries", default="",
                    help="逗号分隔的国家名（英文）")
    ap.add_argument("--countries-file", default=None,
                    help="每行一个国家的文件")
    ap.add_argument("--search-cmd", required=True,
                    help='检索命令模板，{q} 为查询占位')
    ap.add_argument("--out", default=None, help="输出 jsonl")
    ap.add_argument("--no-local-lang", action="store_true",
                    help="只用英文模板（默认会按国家自动加法语/索马里语/阿拉伯语）")
    ap.add_argument("--loose", action="store_true",
                    help="死亡数可选（候选更多、命中略高，但零死亡噪声从 0%% 升到 77%%）")
    ap.add_argument("--block-mode", action="store_true",
                    help="按新闻块抽而不是按句。候选更少，但零死亡噪声从 0%% 升到 72%%")
    ap.add_argument("--no-zh", action="store_true",
                    help="不加中文模板。默认会加 —— 中文检索通道返回中文报道，"
                         "中文查询+中文匹配实测比英文更准（马里 4/6 命中）")
    ap.add_argument("--sleep", type=float, default=0.03)
    a = ap.parse_args()

    global BLOCK_MODE, LOOSE_MODE
    BLOCK_MODE = a.block_mode
    LOOSE_MODE = a.loose
    m = re.match(r"^(\d{4})-(\d{2})$", a.month)
    if not m:
        print("--month 要 YYYY-MM", file=sys.stderr)
        return 1
    year, mon = m.group(1), int(m.group(2))

    cs = []
    if a.countries:
        cs += [x.strip() for x in a.countries.split(",") if x.strip()]
    if a.countries_file and os.path.isfile(a.countries_file):
        with io.open(a.countries_file, encoding="utf-8") as f:
            cs += [l.strip() for l in f if l.strip()]
    cs = list(dict.fromkeys(cs))
    if not cs:
        print("要给 --countries 或 --countries-file", file=sys.stderr)
        return 1

    print("组合遍历：%s，%d 国" % (a.month, len(cs)))
    print()

    all_cands = []
    n_q = 0
    for c in cs:
        qs = [t.format(c=c, m=MONTH_NAME[mon], y=year) for t in TPL_EN]
        if not a.no_local_lang:
            if c in FR_COUNTRIES:
                qs += [t.format(c=c, m=MONTH_FR[mon], y=year) for t in TPL_FR]
            if c in SO_COUNTRIES:
                qs += [t.format(c=c, m="Luulyo", y=year) for t in TPL_SO]
            if c in AR_COUNTRIES:
                qs += [t.format(c=c, m="يوليو", y=year) for t in TPL_AR]
        if not a.no_zh and c in ZH_NAME:
            zm = "%d月" % mon
            qs += [t.format(c=ZH_NAME[c], m=zm) for t in TPL_ZH]
        blob = []
        for q in qs:
            blob.append(run_search(a.search_cmd, q))
            n_q += 1
            time.sleep(a.sleep)
        cands = extract("\n".join(blob), c, year, mon)
        # 同一国家内按摘要去重
        seen, uniq = set(), []
        for x in cands:
            k = re.sub(r"\W+", "", x["summary"])[:60].lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(x)
        all_cands += uniq
        print("  %-18s %2d 条查询 → %3d 条候选" % (c, len(qs), len(uniq)))
        time.sleep(a.sleep)

    print()
    print("=" * 60)
    print("合计 %d 条查询，%d 条候选" % (n_q, len(all_cands)))
    print()
    print("注意：这是**未经判定**的原始候选，全部需要过 judge_events.py。")
    print("抽取规则保守（死亡数 + 暴力动词都要有），但仍然会有大量")
    print("重复报道和无关句子 —— 判定链就是干这个的。")
    print()
    print("下一步：")
    print("  python scripts/judge_events.py --in <本文件> --out 02_判定.json \\")
    print("      --out-manual 02_需人工判.json --min-deaths 0")

    if a.out:
        d = os.path.dirname(os.path.abspath(a.out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            for x in all_cands:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        print()
        print("已写出 → %s" % a.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
