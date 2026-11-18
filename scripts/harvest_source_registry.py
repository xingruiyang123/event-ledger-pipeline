#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""信源表逐家遍历采集：把「1446 家名录」真正走一遍，输出可进判定链的候选。

为什么需要这个脚本
------------------
别的采集器都是「从数据源拉」，这个脚本是「按名录逐家查」。
两者的价值不同：
  · UCDP / ACLED 给你「已收录的事件」，但收录标准是别人定的
  · 逐家遍历给你「这些媒体这个月报了什么」，能回答老师问的**遍历性**

实测背景（2026-08 全量实测）：
  · 只靠 UCDP → 量级够，但覆盖不到 UCDP 不收的小规模事件
  · 只靠信源表逐家检索 → 量级小，但每一家都有「查过/没查过」的记录
  · 两者叠加，才既能量级完整、又能证明没漏

设计原则
--------
1. **遍历性优先于产量**：每家的结果都要落盘，哪怕 0 条。
   「查了没找到」和「没查」在报告里是两件事，必须能分开。
2. **通道可插拔**：不假设你装了某个检索服务。
   `--fetch-cmd` 抓单个 URL，`--search-cmd` 做检索，两个都有 `{url}` / `{q}` 占位。
3. **断点续跑**：1446 家跑一半断网是常态。状态文件记录已完成的家，重跑跳过。
4. **限速不并发默认拉满**：默认 4 线程 + 每家 1 秒间隔，避免把人家站点打挂。

五态判定（互斥，逐家落一个）
--------------------------
  ① 有线索      抓到至少 1 条像事件报道的链接
  ② 已检索·零结果  检索通道正常，但没有任何相关结果
  ③ 已检索·无事件  站点能打开、能搜，但该月确实没有相关报道
  ④ 取不到内容   403/超时/反爬/付费墙 —— **这不是「没报道」**
  ⑤ 未检索      本轮没跑到（限速跳过、--limit 截断、通道未配置）

用法
----
  # 最小用法：只有 curl，逐家抓站点搜索页
  python scripts/harvest_source_registry.py \
      --registry 信源表.xlsx --month 2026-08 --out 候选.jsonl \
      --fetch-cmd "curl -sL --max-time 20 {url}"

  # 有检索路由时（推荐，覆盖率高得多）
  python scripts/harvest_source_registry.py \
      --registry 信源表.xlsx --month 2026-08 --out 候选.jsonl \
      --search-cmd 'D:/DSH/sx.cmd search "{q}" --task auto --json'

  # 先小样本试
  python scripts/harvest_source_registry.py --registry 信源表.xlsx \
      --month 2026-08 --out 候选.jsonl --limit 20 --fetch-cmd "curl -sL {url}"

  # 看名单里有什么、会生成哪些检索式（不联网）
  python scripts/harvest_source_registry.py --registry 信源表.xlsx --month 2026-08 --dry-run

退出码：0 = 至少跑通一家且有产出 | 2 = 全部失败或 0 条候选 | 1 = 参数/文件错误
"""
import argparse
import concurrent.futures as futures
import hashlib
import html as htmlmod
import io
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote, urljoin, urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 五态
ST_HIT = "① 有线索"
ST_ZERO = "② 已检索·零结果"
ST_NOEV = "③ 已检索·无事件"
ST_BLOCK = "④ 取不到内容"
ST_SKIP = "⑤ 未检索"
ALL_ST = [ST_HIT, ST_ZERO, ST_NOEV, ST_BLOCK, ST_SKIP]

# ---------------------------------------------------------------- 事件词
# 判定「这条链接像不像事件报道」。宁可宽一点 —— 后面还有判定链兜。
EVENT_WORDS = [
    # 英文
    r"attack", r"killed", r"kill\b", r"dead\b", r"death", r"bomb", r"blast",
    r"explos", r"gunmen", r"shoot", r"ambush", r"raid", r"massacre", r"abduct",
    r"kidnap", r"terror", r"militant", r"insurgen", r"suicide", r"ied\b",
    r"wounded", r"casualt", r"slain", r"executed", r"behead",
    # 中文
    r"袭击", r"爆炸", r"枪击", r"死亡", r"遇袭", r"恐袭", r"恐怖", r"武装",
    r"屠杀", r"绑架", r"伏击", r"自杀式", r"伤亡", r"击毙", r"斩首",
    # 法/西/葡/阿（非洲拉美高发区常用语）
    r"attentat", r"tué", r"morts?", r"embuscade", r"enlev",
    r"ataque", r"muert", r"asesina", r"secuestr",
    r"ataque", r"morto", r"morte", r"sequestr",
    r"هجوم", r"قتل", r"انفجار", r"تفجير",
]
EVENT_RE = re.compile("|".join(EVENT_WORDS), re.I)

# 明显不是事件报道的路径
BAD_PATH = re.compile(
    r"/(tag|tags|category|categories|author|authors|about|contact|privacy|terms|"
    r"subscribe|login|signin|advertis|career|jobs|rss|feed|sitemap|search)(/|$|\?)", re.I)

# 站点搜索页模板（按常见 CMS 顺序试）
SEARCH_PATHS = [
    "/?s={q}",                  # WordPress
    "/search?q={q}",            # 通用
    "/search/?q={q}",
    "/search/node?keys={q}",    # Drupal
    "/en/search?q={q}",
    "/search/{q}",
]

# ---------------------------------------------------------------- 月份→检索词
MONTH_EN = ["", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"]


def month_terms(month):
    """把 2026-08 变成一组检索用词。"""
    y, m = month.split("-")
    mi = int(m)
    return {
        "ym": "%s-%s" % (y, m),
        "y": y,
        "en": MONTH_EN[mi],
        "en_abbr": MONTH_EN[mi][:3],
        "zh": "%d月" % mi,
        "m": m,
    }


# ---------------------------------------------------------------- 信源表
def load_registry(path, sheet=None, priority=None):
    """读信源表，返回 [{id,name,domain,type,country,lang,level}]。

    容忍列名差异：找含「域名」的列当主域名，找含「名称/机构」的列当名称。
    这是通用性要求 —— 别人的信源表列名不会和我们一样。
    """
    try:
        import openpyxl
    except ImportError:
        print("需要 openpyxl：pip install openpyxl", file=sys.stderr)
        sys.exit(1)

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else None
    if ws is None:
        # 默认选「第一个超过 100 行的 sheet」，不是「行数最多的」。
        # 踩过的坑：域名对照表往往比机构主表行数还多（一机构多域名），
        # 取 max_row 会选错表，家数从 1446 变成 1448 且 id 全乱。
        ws = None
        for w in wb.worksheets:
            if w.max_row > 100:
                ws = w
                break
        if ws is None:
            ws = max(wb.worksheets, key=lambda w: w.max_row)
        print("  （未指定 --sheet，使用：%s）" % ws.title)

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    hdr = [str(h or "").strip() for h in rows[0]]

    def find(*keys):
        # 先精确匹配、再包含匹配。
        # 踩过的坑：只用包含匹配时，「机构」会先命中「机构ID」列，
        # 结果机构名全变成 S0001 这种 id。
        for k in keys:
            for i, h in enumerate(hdr):
                if h == k:
                    return i
        for k in keys:
            for i, h in enumerate(hdr):
                if k and k in h:
                    return i
        return None

    i_dom = find("主域名", "域名", "domain", "url")
    i_name = find("机构名称", "名称", "机构", "name")
    i_type = find("机构类型", "类型", "type")
    i_cty = find("所在国", "国家", "country")
    i_lang = find("语种", "语言", "lang")
    i_lvl = find("建议优先级", "优先级", "原等级", "等级", "level")
    # 优先级与等级是两回事：信源表里「原等级」是 S/A/B（媒体影响力），
    # 「建议优先级」是 P1/P2/P3（这一轮该先跑谁）。
    # 取错列的话按 P1 筛会得到 0 家 —— 实测踩过一次。
    i_id = find("机构ID", "ID", "id")

    if i_dom is None:
        print("✗ 信源表里找不到「域名」列。表头是：%s" % " / ".join(hdr[:12]), file=sys.stderr)
        sys.exit(1)

    out = []
    for n, r in enumerate(rows[1:], 1):
        if not r:
            continue
        dom = str(r[i_dom] or "").strip().lower()
        if not dom or dom in ("none", "-"):
            continue
        # 去协议、去 www、去路径
        dom = re.sub(r"^https?://", "", dom).split("/")[0].replace("www.", "")
        if not re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", dom):
            continue
        out.append({
            "id": str(r[i_id] or "S%04d" % n) if i_id is not None else "S%04d" % n,
            "name": str(r[i_name] or dom) if i_name is not None else dom,
            "domain": dom,
            "type": str(r[i_type] or "") if i_type is not None else "",
            "country": str(r[i_cty] or "") if i_cty is not None else "",
            "lang": str(r[i_lang] or "") if i_lang is not None else "",
            "level": str(r[i_lvl] or "") if i_lvl is not None else "",
        })
    # 同域去重（保留第一条）
    seen, uniq = set(), []
    for x in out:
        if x["domain"] in seen:
            continue
        seen.add(x["domain"])
        uniq.append(x)

    # 按优先级筛选。名单 1446 家里 P1 只有 114 家、P3 有 1158 家，
    # 全量跑一轮要两小时以上。先跑 P1 就能拿到有统计意义的覆盖率数字，
    # 不必等全量跑完才知道这一路值不值得做。
    if priority:
        want = {p.strip().upper() for p in str(priority).split(",") if p.strip()}
        before = len(uniq)
        uniq = [x for x in uniq if (x.get("level") or "").strip().upper() in want]
        print("  按优先级筛选 %s：%d -> %d 家" % (sorted(want), before, len(uniq)))
    return uniq


# ---------------------------------------------------------------- 执行外部命令
def clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def run_cmd(tpl, **kw):
    """执行模板命令，返回 (ok, 输出文本, 备注)。模板里用 {url} / {q} 占位。"""
    cmd = tpl
    for k, v in kw.items():
        if k.startswith("_"):
            continue  # _timeout 这类是参数，不是模板占位
        cmd = cmd.replace("{%s}" % k, str(v))
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=kw.get("_timeout") or 40,
            env=clean_env(),
        )
    except subprocess.TimeoutExpired:
        return False, "", "超时"
    except Exception as e:  # noqa: BLE001
        return False, "", "命令失败：%s" % str(e)[:60]
    txt = (p.stdout or b"").decode("utf-8", "replace")
    if p.returncode != 0 and not txt.strip():
        return False, "", "退出码 %d" % p.returncode
    return True, txt, ""


# ---------------------------------------------------------------- 抓取 & 抽链
LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
DATE_RE = re.compile(
    r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})|"
    r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(20\d{2})",
    re.I)


# 标题里常见的噪声：视频时长、播放按钮、栏目前缀
NOISE_RE = re.compile(
    r"(\bPlay\b|\bWATCH\b|\bVIDEO\b|\b\d{1,2}:\d{2}\b|\b\d+\s*sec\b|"
    r"^\s*[|·\-–—]+\s*|\s*[|·\-–—]+\s*$)", re.I)


def strip_tags(s):
    t = htmlmod.unescape(TAG_RE.sub(" ", s or ""))
    t = NOISE_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract_links(html, base_url, month_terms_):
    """从页面抽「像事件报道」的链接。返回 [{url,title,date}]。"""
    out, seen = [], set()
    for m in LINK_RE.finditer(html or ""):
        href, inner = m.group(1), m.group(2)
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith("http"):
            continue
        p = urlparse(url)
        if p.netloc.replace("www.", "") != urlparse(base_url).netloc.replace("www.", ""):
            continue  # 只要本站链接
        if BAD_PATH.search(p.path):
            continue
        title = strip_tags(inner)
        if len(title) < 12:
            continue
        if not EVENT_RE.search(title) and not EVENT_RE.search(p.path):
            continue
        if NON_EVENT_RE.search(title):
            continue   # 预警/谴责/评估/防御成功，不是事件本身
        if url in seen:
            continue
        seen.add(url)
        # 尝试从 URL 或标题里读日期
        dm = DATE_RE.search(url) or DATE_RE.search(title)
        date = ""
        if dm:
            if dm.group(1):
                date = "%s-%02d-%02d" % (dm.group(1), int(dm.group(2)), int(dm.group(3)))
            else:
                mi = MONTH_EN.index(dm.group(5).title()) if dm.group(5).title() in MONTH_EN else 0
                if mi:
                    date = "%s-%02d-%02d" % (dm.group(6), mi, int(dm.group(4)))
        out.append({"url": url, "title": title, "date": date})
    return out


# ---------------------------------------------------------------- 标题结构化
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
             "dozens of": 12, "dozen": 12, "scores of": 20}

DEAD_PAT = re.compile(
    r"(\d[\d,]*)\s*(?:people\s+)?(?:were\s+)?(?:dead|killed|die[ds]?|deaths?|dead\b)"
    r"|kills?\s+(?:at\s+least\s+)?(\d[\d,]*)"
    r"|(\d[\d,]*)\s*(?:人)?\s*(?:死亡|遇难|被杀|丧生)"
    r"|death\s+toll\s+(?:of\s+)?(\d[\d,]*)", re.I)
DEAD_WORD_PAT = re.compile(
    r"\b(" + "|".join(NUM_WORDS.keys()) + r")\s+(?:\w+\s+){0,3}?(?:dead|killed)\b", re.I)
INJ_PAT = re.compile(
    r"(\d[\d,]*)\s*(?:people\s+)?(?:were\s+)?(?:injured|hurt|wounded)"
    r"|(\d[\d,]*)\s*(?:人)?\s*(?:受伤|伤)"
    r"|injures?\s+(?:at\s+least\s+)?(\d[\d,]*)", re.I)

# 顺序 = 优先级。先把专指性强的放前面：
# 「gunmen abduct 30 villagers」同时含 gunmen 和 abduct，
# 枪击若排在前面会把它判成枪击，实测踩过。
ACT_RULES = [
    ("自杀式袭击", r"suicide|\bSVBIED\b|self-?bomb|自杀式|人体炸弹"),
    ("绑架", r"abduct|kidnap|hostage|绑架|劫持"),
    ("爆炸", r"bomb|blast|explos|\bIED\b|grenade|mine\b|爆炸|炸弹|地雷"),
    ("持刀", r"knife|stab|machete|持刀|砍"),
    ("枪击", r"shoot|gunmen|gunfire|opened fire|shot\b|枪击|开枪|射击"),
    ("武装冲突", r"clash|fighting|battle|combat|混战|冲突|交火|战斗"),
    ("纵火", r"arson|set fire|burn|纵火|烧"),
    ("车辆冲撞", r"ramm|vehicle attack|car attack|冲撞"),
    ("伏击", r"ambush|伏击"),
    ("空袭", r"air\s?strike|drone strike|airstrike|空袭|无人机"),
    ("袭击", r"attack|assault|raid|storm|offensive|袭击|攻击|突袭"),
]
ACT_RES = [(name, re.compile(pat, re.I)) for name, pat in ACT_RULES]

# 非事件报道：预警、谴责、评估、防御成功、调查通报 —— 都不是「这个月发生的一起袭击」
NON_EVENT_RE = re.compile(
    r"(?i)("
    r"\bbraces? for\b|\bon alert\b|\bwarns? of\b|\bcould launch\b|\bpossible attack\b|"
    r"\bexpect(?:ed|s)? (?:attack|strike)|\bfears? of\b|\bprepares? for\b|"
    r"\bcondemns?\b|\bdenounces?\b|\burges?\b|\bcalls? for\b|\bdemands?\b|"
    r"\bintel(?:ligence)? assesses\b|\bassess(?:es|ment)?\b|\binvestigat(?:e|es|ion)\b|"
    r"\breleases? (?:report|finding)|\breport says\b|\banalysis\b|\bexplainer\b|"
    r"\bfends? off\b|\bintercepts?\b|\bthwarts?\b|\bfoils?\b|\bdrill\b|\bexercise\b|"
    r"\bwould\b|\bmay\b|\bcould\b|\bmight\b|"
    # 网络攻击不是恐怖袭击（实测「Ransomware attack hits … computer systems」
    # 「Iranian cyber attack on power plant」都被算进了恐袭台账）
    r"\bcyber\b|ransomware|malware|hack(?:ed|ing|s)?\b|data breach|phishing|"
    r"网络攻击|黑客|勒索软件|数据泄露"
    r")")

# 行为体线索：加害方
ACTOR_HINT = [
    ("武装分子", r"militants?|insurgents?|gunmen|assailants|extremists?|jihadists?|武装分子|武装人员"),
    ("恐怖组织", r"ISIS|ISIL|\bIS\b|ISWAP|Boko Haram|JNIM|al-?Shabaab|Taliban|TTP|BLA|AQAP|恐怖"),
    ("不明身份者", r"unknown gunmen|unidentified|suspects?|不明身份"),
]


def _num(s):
    if not s:
        return None
    s = s.replace(",", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def extract_from_headline(title):
    """从新闻标题抽 deaths / injured / act_type / actor_a。

    为什么必须做这一步：判定链的兜底正则是按 UCDP 长摘要写的
    （「militants attacked a village, killing 17 civilians」那种），
    对新闻标题（「Suspected suicide bombing kills at least 14 in Pakistan」）覆盖不到。
    实测 7 条典型标题里有 4 条返回 None、直接落「不计入」。
    所以采集这一层就要把标题里的数字和动作类型抽出来。
    """
    t = title or ""
    deaths = None
    for m in DEAD_PAT.finditer(t):
        for g in m.groups():
            v = _num(g)
            if v is not None:
                deaths = v if deaths is None else max(deaths, v)
                break
    if deaths is None:
        mw = DEAD_WORD_PAT.search(t)
        if mw:
            deaths = NUM_WORDS.get(mw.group(1).lower())

    injured = None
    for m in INJ_PAT.finditer(t):
        for g in m.groups():
            v = _num(g)
            if v is not None:
                injured = v if injured is None else max(injured, v)
                break

    act = ""
    for name, rx in ACT_RES:
        if rx.search(t):
            act = name
            break

    actor = ""
    for name, pat in ACTOR_HINT:
        if re.search(pat, t, re.I):
            actor = name
            break

    return {"deaths": deaths, "injured": injured, "act_type": act, "actor_a": actor}


# 从标题里认国家。信源表候选的 country 字段原本留空（不能填媒体所在国：
# Dawn 是巴基斯坦媒体，但它报的西岸定居者袭击会被错标成巴基斯坦）。
# 但留空会让判定链和跨源核验都没法按国家匹配，等于这一路的数据进不了主流程。
# 折中：从标题正文里认，认不出就留空，绝不拿媒体所在国顶替。
COUNTRY_WORDS = {
    "Afghanistan": ["afghanistan", "阿富汗", "kabul", "喀布尔"],
    "Nigeria": ["nigeria", "尼日利亚", "abuja", "borno", "kaduna", "zamfara"],
    "Pakistan": ["pakistan", "巴基斯坦", "peshawar", "karachi", "waziristan",
                 "balochistan", "kp province", "islamabad"],
    "Somalia": ["somalia", "索马里", "mogadishu", "摩加迪沙", "al-shabaab", "al shabaab"],
    "Syria": ["syria", "叙利亚", "damascus", "大马士革", "idlib", "aleppo"],
    "Iraq": ["iraq", "伊拉克", "baghdad", "巴格达", "mosul", "kirkuk"],
    "Yemen": ["yemen", "也门", "sanaa", "aden", "houthi", "胡塞"],
    "Sudan": ["sudan", "苏丹", "khartoum", "喀土穆", "darfur", "达尔富尔", "rsf"],
    "South Sudan": ["south sudan", "南苏丹", "juba"],
    "Mali": ["mali", "马里", "bamako", "timbuktu", "jnim"],
    "Burkina Faso": ["burkina", "布基纳法索", "ouagadougou"],
    "Niger": ["niger", "尼日尔", "niamey"],
    "Chad": ["chad", "乍得", "ndjamena"],
    "Cameroon": ["cameroon", "喀麦隆", "boko haram", "yaounde"],
    "Democratic Republic of the Congo": ["congo", "刚果", "kinshasa", "goma",
                                         "north kivu", "south kivu", "m23"],
    "Mozambique": ["mozambique", "莫桑比克", "cabo delgado"],
    "Ethiopia": ["ethiopia", "埃塞俄比亚", "addis ababa", "tigray", "amhara"],
    "Kenya": ["kenya", "肯尼亚", "nairobi", "al-shabaab"],
    "Libya": ["libya", "利比亚", "tripoli", "benghazi"],
    "Egypt": ["egypt", "埃及", "cairo", "开罗", "sinai", "西奈"],
    "Algeria": ["algeria", "阿尔及利亚", "algiers"],
    "Morocco": ["morocco", "摩洛哥", "rabat"],
    "Tunisia": ["tunisia", "突尼斯"],
    "Israel": ["israel", "以色列", "jerusalem", "tel aviv", "idf"],
    "Palestine": ["palestine", "palestinian", "巴勒斯坦", "gaza", "加沙",
                  "west bank", "约旦河西岸", "hamas", "rafah", "khan younis"],
    "Lebanon": ["lebanon", "黎巴嫩", "beirut", "贝鲁特", "hezbollah"],
    "Jordan": ["jordan", "约旦", "amman"],
    "Saudi Arabia": ["saudi", "沙特"],
    "Turkey": ["turkey", "türkiye", "土耳其", "istanbul", "ankara"],
    "Iran": ["iran", "伊朗", "tehran", "德黑兰"],
    "Russia": ["russia", "俄罗斯", "moscow", "莫斯科", "dagestan"],
    "Ukraine": ["ukraine", "乌克兰", "kyiv", "kharkiv", "odesa", "donetsk"],
    "India": ["india", "印度", "kashmir", "克什米尔", "delhi", "mumbai",
              "chhattisgarh", "manipur", "naxal"],
    "Bangladesh": ["bangladesh", "孟加拉"],
    "Sri Lanka": ["sri lanka", "斯里兰卡"],
    "Nepal": ["nepal", "尼泊尔"],
    "Myanmar": ["myanmar", "burma", "缅甸", "yangon", "仰光", "rakhine"],
    "Thailand": ["thailand", "泰国", "bangkok"],
    "Philippines": ["philippines", "菲律宾", "mindanao", "manila"],
    "Indonesia": ["indonesia", "印度尼西亚", "jakarta", "papua"],
    "Malaysia": ["malaysia", "马来西亚"],
    "China": ["china", "中国", "xinjiang", "新疆"],
    "Colombia": ["colombia", "哥伦比亚", "bogota", "eln", "farc"],
    "Mexico": ["mexico", "墨西哥", "juarez", "sinaloa", "tijuana", "guerrero"],
    "Brazil": ["brazil", "巴西", "rio de janeiro", "sao paulo"],
    "Venezuela": ["venezuela", "委内瑞拉", "caracas"],
    "Peru": ["peru", "秘鲁", "lima"],
    "Ecuador": ["ecuador", "厄瓜多尔", "guayaquil", "quito"],
    "Haiti": ["haiti", "海地", "port-au-prince", "太子港"],
    "Honduras": ["honduras", "洪都拉斯"],
    "Guatemala": ["guatemala", "危地马拉"],
    "El Salvador": ["el salvador", "萨尔瓦多"],
    "Chile": ["chile", "智利"],
    "Argentina": ["argentina", "阿根廷"],
    "United States": ["united states", "u.s.", "美国", "texas", "chicago"],
    "United Kingdom": ["united kingdom", "britain", "英国", "london", "manchester"],
    "France": ["france", "法国", "paris", "巴黎", "marseille"],
    "Germany": ["germany", "德国", "berlin", "柏林"],
    "Spain": ["spain", "西班牙", "madrid"],
    "Italy": ["italy", "意大利", "rome"],
    "Belgium": ["belgium", "比利时", "brussels"],
    "Sweden": ["sweden", "瑞典", "stockholm"],
    "Norway": ["norway", "挪威", "oslo"],
    "Netherlands": ["netherlands", "荷兰", "amsterdam"],
    "Australia": ["australia", "澳大利亚", "sydney", "melbourne"],
    "Canada": ["canada", "加拿大", "toronto", "ottawa"],
    "Kazakhstan": ["kazakhstan", "哈萨克斯坦"],
    "Tajikistan": ["tajikistan", "塔吉克斯坦"],
    "Uzbekistan": ["uzbekistan", "乌兹别克斯坦"],
    "Azerbaijan": ["azerbaijan", "阿塞拜疆"],
    "Georgia": ["georgia", "格鲁吉亚", "tbilisi"],
}


def guess_country(text):
    """从标题/摘要里认国家。认不出返回空串 —— 不猜、不拿媒体所在国顶替。"""
    t = " " + str(text or "").lower() + " "
    best, best_n = "", 0
    for cty, words in COUNTRY_WORDS.items():
        n = 0
        for w in words:
            if len(w) <= 4:
                if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(w), t):
                    n += 1
            elif w in t:
                n += 1
        if n > best_n:
            best, best_n = cty, n
    return best


def in_month(date, ym):
    """日期是否落在目标月。空日期放行（留给后续核验）。"""
    if not date:
        return True
    return date.startswith(ym)


# ---------------------------------------------------------------- 单家处理
def process_one(src, args, mt):
    """处理一家。

    路线优先级（这是实测调过来的，别倒回去）：
      A. --search-cmd 外部检索  —— 主路线
         实测：site: 语法一次返回 10 条，URL 里带 /2026/08/07/，标题就是事件句。
      B. --fetch-cmd 站点搜索页 —— 兜底
         实测：对大媒体基本无效，/?s= 返回的是首页新闻流、全无日期；
         只对 WordPress 小站有用。

    日期硬过滤：无日期的候选**不算命中**。宁可漏，不可把去年的新闻算进这个月。
    """
    dom = src["domain"]
    tried, items, note = [], [], ""

    # ---- A. 外部检索（主）----
    if args.search_cmd:
        # 两条检索式：先带月份，再带年月日形态
        queries = [
            "site:%s %s %s killed OR attack OR bombing OR 袭击 OR 爆炸"
            % (dom, mt["en"], mt["y"]),
            "site:%s %s/%s attack" % (dom, mt["y"], mt["m"]),
        ]
        for q in queries:
            tried.append("search:" + q[:70])
            ok, raw, err = run_cmd(args.search_cmd, q=q, _timeout=args.timeout)
            if ok and raw.strip():
                cand = parse_search_output(raw, dom)
                # 只留目标月的（必须能判定日期）
                keep = [c for c in cand if c.get("date") and in_month(c["date"], mt["ym"])]
                if keep:
                    items.extend(keep)
                    break
                elif cand:
                    note = note or "检索有返回但无目标月条目(%d条)" % len(cand)
                else:
                    note = note or "检索无本站链接"
            else:
                note = note or (err or "检索无返回")

    # ---- B. 站点搜索页（兜底，只在前一条路没结果时试）----
    if not items and args.fetch_cmd:
        for tpl in SEARCH_PATHS:
            q = quote("%s %s" % (mt["en"], mt["y"]))
            url = "https://%s%s" % (dom, tpl.replace("{q}", q))
            tried.append(url)
            ok, html, err = run_cmd(args.fetch_cmd, url=url, _timeout=args.timeout)
            if not ok:
                note = note or err
                if any(k in (err or "") for k in ("超时", "退出码 28", "退出码 6",
                                                  "退出码 7", "退出码 35", "命令失败")):
                    break
                continue
            if len(html) < 800:
                continue
            low = html.lower()
            if ("just a moment" in low or "checking your browser" in low
                    or "enable javascript and cookies" in low):
                note = note or "反爬验证页"
                break
            hits = extract_links(html, url, mt)
            # 无日期的不算 —— 首页新闻流全都没有日期
            hits = [h for h in hits if h["date"] and in_month(h["date"], mt["ym"])]
            if hits:
                items.extend(hits)
                break
            note = note or "页面可取但无目标月事件链接"
            break

    # ---- 落状态 ----
    if items:
        st = ST_HIT
        note = ""   # 已经抓到了，前面某条检索式的失败原因不该留在状态里
    elif note and any(k in note for k in ("超时", "反爬", "退出码", "命令失败")):
        st = ST_BLOCK
    elif tried:
        st = ST_NOEV
    else:
        st = ST_SKIP

    return {"id": src["id"], "name": src["name"], "domain": dom,
            "type": src["type"], "country": src["country"], "level": src["level"],
            "status": st, "n_items": len(items), "note": note[:160],
            "tried": tried[-2:], "items": items}


def parse_search_output(raw, dom, drop_non_event=True):
    """解析检索命令的输出：支持 JSON（找 url/link 字段）或纯文本链接。"""
    out = []
    # JSON 优先
    try:
        d = json.loads(raw)
        stack = [d]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                u = cur.get("url") or cur.get("link") or cur.get("href")
                if isinstance(u, str) and u.startswith("http") and dom in u:
                    _t = (cur.get("title") or cur.get("name") or ""
                          or cur.get("snippet") or cur.get("content") or "")
                    _d = str(cur.get("date") or cur.get("published") or "")[:10]
                    # date 可能是 2026-08-18T... 也可能空；从 URL 兜底
                    if not _d:
                        dm2 = re.search(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/", u)
                        if dm2:
                            _d = "%s-%02d-%02d" % (dm2.group(1), int(dm2.group(2)),
                                                   int(dm2.group(3)))
                    _tt = strip_tags(str(_t))
                    if drop_non_event and NON_EVENT_RE.search(_tt):
                        continue   # 预警/谴责/评估类，不是事件
                    out.append({"url": u,
                                "title": _tt[:200],
                                "date": _d})
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
        if out:
            return out
    except Exception:  # noqa: BLE001
        pass
    # 纯文本链接
    seen = set()
    for m in re.finditer(r'https?://[^\s<>"\')]+', raw):
        u = m.group(0)
        if dom not in u or u in seen:
            continue
        seen.add(u)
        dm = DATE_RE.search(u)
        date = ""
        if dm and dm.group(1):
            date = "%s-%02d-%02d" % (dm.group(1), int(dm.group(2)), int(dm.group(3)))
        out.append({"url": u, "title": "", "date": date})
    return out


# ---------------------------------------------------------------- 状态表
def write_status(path, results, total, args):
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        # 退化成 csv
        path = os.path.splitext(path)[0] + ".csv"
        with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("机构ID,机构名称,主域名,类型,国家,等级,遍历状态,产出条数,备注\n")
            for r in results:
                f.write("%s,%s,%s,%s,%s,%s,%s,%d,%s\n" % (
                    r["id"], r["name"], r["domain"], r["type"], r["country"],
                    r["level"], r["status"], r["n_items"],
                    (r["note"] or "").replace(",", "；")))
        return path

    from collections import Counter
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "遍历状态"
    hdr = ["机构ID", "机构名称", "主域名", "类型", "国家", "等级",
           "遍历状态", "产出条数", "备注"]
    ws.append(hdr)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    order = {s: i for i, s in enumerate(ALL_ST)}
    for r in sorted(results, key=lambda x: (order.get(x["status"], 9), -x["n_items"])):
        ws.append([r["id"], r["name"], r["domain"], r["type"], r["country"],
                   r["level"], r["status"], r["n_items"], r["note"]])
    ws.freeze_panes = "A2"
    for col, w in zip("ABCDEFGHI", [10, 30, 26, 14, 12, 8, 16, 10, 44]):
        ws.column_dimensions[col].width = w

    # 汇总页
    ws2 = wb.create_sheet("汇总")
    cnt = Counter(r["status"] for r in results)
    ws2.append(["指标", "数值"])
    for c in ws2[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="FFF2CC")
    ws2.append(["名单总家数", total])
    ws2.append(["本轮实跑", len(results)])
    ws2.append(["遍历率", "%.1f%%" % (100.0 * len(results) / total if total else 0)])
    ws2.append([])
    for s in ALL_ST:
        ws2.append([s, cnt.get(s, 0)])
    ws2.append([])
    hit = sum(1 for r in results if r["n_items"])
    ws2.append(["抓到线索的家数", hit])
    ws2.append(["产出候选总条数", sum(r["n_items"] for r in results)])
    ws2.append([])
    ws2.append(["月份", args.month])
    ws2.append(["抓取通道", "有" if args.fetch_cmd else "无"])
    ws2.append(["检索通道", "有" if args.search_cmd else "无"])
    ws2.column_dimensions["A"].width = 22
    ws2.column_dimensions["B"].width = 26
    wb.save(path)
    return path


# ---------------------------------------------------------------- 主流程
def ensure_parent(path):
    """写文件前建父目录 —— 用户给的子目录不一定存在。"""
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def main():
    ap = argparse.ArgumentParser(
        description="信源表逐家遍历采集（输出可进判定链的候选 + 每家的遍历状态）")
    ap.add_argument("--registry", required=True, help="信源表 xlsx")
    ap.add_argument("--sheet", default=None, help="指定 sheet 名（默认取行数最多的）")
    ap.add_argument("--month", required=True, help="目标月份 YYYY-MM")
    ap.add_argument("--out", default="候选_信源遍历.jsonl", help="候选输出 jsonl")
    ap.add_argument("--status-out", default="信源遍历状态.xlsx",
                    help="每家遍历状态表")
    ap.add_argument("--state", default=".harvest_state.json",
                    help="断点续跑状态文件")
    ap.add_argument("--fetch-cmd", default=None,
                    help="抓单个 URL 的命令模板，{url} 为占位。例：curl -sL --max-time 20 {url}")
    ap.add_argument("--search-cmd", default=None,
                    help='检索命令模板，{q} 为占位。例：xxx search "{q}" --json')
    ap.add_argument("--priority", default=None,
                    help="只跑这些优先级的机构（逗号分隔，如 P1 或 P1,P2）。"
                         "名单 1446 家里 P1 只有 114 家，先跑 P1 就能拿到覆盖率数字")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 家（0=全部）")
    ap.add_argument("--workers", type=int, default=6, help="并发数，默认 6")
    ap.add_argument("--delay", type=float, default=1.0, help="每家间隔秒，默认 1.0")
    ap.add_argument("--timeout", type=int, default=14, help="单次命令超时秒，默认 14")
    ap.add_argument("--min-yield", type=int, default=1,
                    help="整批产出低于此数则 exit 2，默认 1")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列名单与检索式，不联网")
    a = ap.parse_args()

    if not os.path.isfile(a.registry):
        print("✗ 信源表不存在：%s" % a.registry, file=sys.stderr)
        return 1
    if not (a.fetch_cmd or a.search_cmd) and not a.dry_run:
        print("✗ 至少要给 --fetch-cmd 或 --search-cmd 之一。", file=sys.stderr)
        print("  只给 --search-cmd 也能跑（走外部检索），覆盖率通常更高。", file=sys.stderr)
        return 1

    mt = month_terms(a.month)
    srcs = load_registry(a.registry, a.sheet, a.priority)
    print("信源表：%s" % a.registry)
    print("机构家数：%d（同域已合并）" % len(srcs))
    print("目标月份：%s" % a.month)
    print("通道：抓取=%s ｜ 检索=%s" % ("有" if a.fetch_cmd else "无",
                                  "有" if a.search_cmd else "无"))
    print()

    if a.dry_run:
        print("=== 前 20 家与检索式（dry-run，不联网）===")
        for s in srcs[:20]:
            print("  %-8s %-28s %-26s" % (s["id"], s["name"][:26], s["domain"]))
            print("           抓: https://%s/?s=%s" % (s["domain"], quote(mt["en"])))
            print("           搜: site:%s %s %s attack" % (s["domain"], mt["en"], mt["y"]))
        if len(srcs) > 20:
            print("  … 另有 %d 家" % (len(srcs) - 20))
        print()
        print("按等级分布：")
        from collections import Counter
        print("  ", dict(Counter(s["level"] or "未标" for s in srcs).most_common()))
        return 0

    # 断点续跑
    done = {}
    if os.path.isfile(a.state):
        try:
            done = json.load(io.open(a.state, encoding="utf-8")).get("done", {})
            print("断点续跑：已完成 %d 家，跳过" % len(done))
        except Exception:  # noqa: BLE001
            done = {}

    todo = [s for s in srcs if s["domain"] not in done]
    if a.limit:
        todo = todo[:a.limit]
    print("本轮待跑：%d 家" % len(todo))
    print()

    ensure_parent(a.state)
    ensure_parent(a.out)
    ensure_parent(a.status_out)

    results, t0 = [], time.time()

    def work(s):
        time.sleep(a.delay)
        try:
            return process_one(s, a, mt)
        except Exception as e:  # noqa: BLE001
            return {"id": s["id"], "name": s["name"], "domain": s["domain"],
                    "type": s["type"], "country": s["country"], "level": s["level"],
                    "status": ST_BLOCK, "n_items": 0,
                    "note": "异常：%s" % str(e)[:100], "tried": [], "items": []}

    with futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(work, s): s for s in todo}
        for n, f in enumerate(futures.as_completed(futs), 1):
            r = f.result()
            results.append(r)
            done[r["domain"]] = r["status"]
            flag = "✓" if r["n_items"] else "·"
            print("  [%4d/%4d] %s %-30s %-14s %2d 条  %s"
                  % (n, len(todo), flag, r["name"][:28], r["status"], r["n_items"],
                     (r["note"] or "")[:30]))
            if n % 25 == 0:
                json.dump({"done": done, "month": a.month},
                          io.open(a.state, "w", encoding="utf-8"), ensure_ascii=False)

    ensure_parent(a.state)
    json.dump({"done": done, "month": a.month},
              io.open(a.state, "w", encoding="utf-8"), ensure_ascii=False)

    # ---- 写候选 ----
    cand = []
    for r in results:
        for it in r["items"]:
            _ex = extract_from_headline(it.get("title", "") or "")
            cand.append({
                "source": "registry:%s" % r["domain"],
                "source_org": r["name"],
                "source_org_id": r["id"],
                "date": it.get("date", ""),
                # 不能填信源机构所在国：Dawn 是巴基斯坦媒体，但它报的
                # 西岸定居者袭击会被错标成「巴基斯坦」。国家归属要看正文，
                # 这里留空、交给后续核验补。
                # 用标题认国家；认不出留空，绝不填媒体所在国
                "country": guess_country(it.get("title", "")),
                "place": "",
                "summary": it.get("title", "")[:300],
                "deaths": _ex["deaths"],
                "injured": _ex["injured"],
                "actor_a": _ex["actor_a"],
                "actor_b": "",
                "act_type": _ex["act_type"],
                "url": it["url"],
                "raw_id": hashlib.md5(it["url"].encode()).hexdigest()[:12],
            })
    # 同 URL 去重
    # 去重：同 URL，或同标题前 40 字（同一事件常有多条路径：/amp/、/index.html、不同栏目）
    seen_url, seen_title, uniq = set(), set(), []
    for c in cand:
        tk = re.sub(r"\W+", "", (c.get("summary") or ""))[:40].lower()
        if c["url"] in seen_url or (tk and tk in seen_title):
            continue
        seen_url.add(c["url"])
        if tk:
            seen_title.add(tk)
        uniq.append(c)

    ensure_parent(a.out)
    with io.open(a.out, "w", encoding="utf-8") as f:
        for c in uniq:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    ensure_parent(a.status_out)
    st_path = write_status(a.status_out, results, len(srcs), a)

    # ---- 汇总 ----
    el = time.time() - t0
    from collections import Counter
    cnt = Counter(r["status"] for r in results)
    print()
    print("=" * 56)
    print(" 遍历完成：%d 家 / 耗时 %.1f 秒 / 平均 %.2f 秒每家"
          % (len(results), el, el / max(1, len(results))))
    print("=" * 56)
    for s in ALL_ST:
        print("  %-16s %4d" % (s, cnt.get(s, 0)))
    print()
    print("  候选条数：%d（同名 URL 已去重）" % len(uniq))
    print("  名单总数：%d ｜ 累计已跑：%d ｜ 遍历率：%.1f%%"
          % (len(srcs), len(done), 100.0 * len(done) / max(1, len(srcs))))
    print()
    print("  候选 → %s" % a.out)
    print("  状态 → %s" % st_path)
    print()

    if not uniq:
        print("✗ 本轮 0 条候选。排查方向：", file=sys.stderr)
        print("  1) 通道是否真的能取到内容（先手工跑一次 --fetch-cmd 的命令）", file=sys.stderr)
        print("  2) 站点搜索路径是否匹配（不同 CMS 路径不同，可改 SEARCH_PATHS）", file=sys.stderr)
        print("  3) 目标月份是否真的没有相关报道（看状态表里「③ 已检索·无事件」的比例）",
              file=sys.stderr)
        return 2

    if len(uniq) < a.min_yield:
        print("✗ 产出低于 --min-yield=%d" % a.min_yield, file=sys.stderr)
        return 2

    print("下一步：把候选过判定链")
    print("  python scripts/judge_events.py --in %s --out 02_判定.json \\" % a.out)
    print("         --out-manual 02_需人工判.json --out-excluded 02b_排除.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
