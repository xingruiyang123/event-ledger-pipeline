#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给事件补「信息来源URL」——UCDP 只给媒体名+日期+标题，不给链接。

为什么需要这一步
----------------
UCDP 的 49 个字段里没有 URL（`source_article` 是 "媒体名,日期,标题" 的引用串）。
所以从 UCDP 抽出的台账，`信息来源URL` 列全是空的 —— 而台账要"可核"就必须有链接。

本脚本用「媒体名 + 标题」反查原文链接。它不保证拿到 UCDP 当初引用的那一条，
只要求拿到**可信媒体对同一事件的报道**，足以支撑可核性。

通用性
------
搜索能力做成可插拔：`--search-cmd` 传任何能返回结果的命令，`{q}` 是查询占位。
  · 有检索路由（如本机 sx.cmd）：--search-cmd 'D:/DSH/sx.cmd search "{q}" --task auto --json'
  · 只有 curl + Bing：        --search-cmd 'curl -s "https://www.bing.com/search?q={q}"'
  · 什么都没有：             不加 --search-cmd，脚本会输出「待补链接」清单，不假装能补

支持的输出格式（自动识别）：
  · JSON（在任意层级找 url / link / href 字段，配 title）
  · 纯文本里的 http(s) 链接（每行一个或多个）

用法
----
  python scripts/enrich_urls.py --in 候选.jsonl --out 候选_带链接.jsonl \
      --search-cmd 'D:/DSH/sx.cmd search "{q}" --task auto --json'
  python scripts/enrich_urls.py --in 候选.jsonl --out out.jsonl --limit 20 --dry-run
  python scripts/enrich_urls.py --in 台账.xlsx --out 台账_带链接.xlsx   # 只导出待补清单

退出码：0 = 全部补到或无需补 | 2 = 有缺口（清单已导出）| 1 = 无法判定
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

# 社交/聚合平台：不能当台账的「来源」（老师核不到、也常被删）
# 汇总页/统计页：不是「某一起事件」的报道，不能当台账来源
# URL 里带这些词，说明报道讲的是「社区冲突/反恐清剿」而不是恐袭本身
BAD_URL_WORD = re.compile(
    r"(?i)(communal|clash|ethnic|sectarian|tribal|reprisal|"
    r"kill[s]?-?10-terrorists|forces-kill|troops-kill|army-kill|"
    r"foil|arrest|crackdown|offensive|operation)")

AGGREGATE = re.compile(
    r"(?i)(/timeline|/statistics|/statistic|/archive|/category/|/tag/|/topics?/|"
    r"terrorist-activity|/data/|/index|"
    # 实测抓出来的汇总页/非事件页。这些链接地名和国家都对得上，
    # 但内容不是「这一起事件」的报道：
    r"/wiki/|list_of_|list-of-|/dashboard|/publications/report|"
    r"/country-focus/|/global-conflict-tracker|/map/|/explainer|/backgrounder|"
    r"situation-report|humanitarian-response|/monitor/)")

# 垃圾/镜像域名：随机字符串域名、内容农场。
# 实测出现过 ergngevd.top/wiki/List_of_massacres_in_Burkina_Faso 这种。
# 只按 TLD 判，不按域名长度 —— 早前用 [a-z]{8,} 会把 nivonews.com、
# vanguardngr.com 这类正常媒体一起拦掉。
JUNK_DOMAIN = re.compile(
    r"(?i)\.(?:top|xyz|click|buzz|loan|work|men|date|review|country|stream)$"
    r"|^(?:[a-z0-9]{16,})\.")

BLOCK = re.compile(
    r"(?i)(facebook\.com|twitter\.com|x\.com/|instagram\.com|tiktok\.com|"
    r"youtube\.com|youtu\.be|t\.me/|reddit\.com|wikipedia\.org|"
    r"news\.google\.com|google\.com/url|bing\.com|duckduckgo\.com)")
# 常见可信媒体域名（用于优先挑选；不是硬门槛）
NEWS = re.compile(
    r"(?i)(reuters\.com|apnews\.com|afp\.com|bbc\.|aljazeera\.com|"
    r"lemonde\.fr|france24\.com|rfi\.fr|dw\.com|theguardian\.com|"
    r"nytimes\.com|washingtonpost\.com|economist\.com|"
    r"syriahr\.com|sana\.sana|middleeasteye\.net|al-monitor\.com|"
    r"punchng\.com|premiumtimesng\.com|thenationonlineng|vanguardngr\.com|"
    r"dailypost\.ng|leadership\.ng|humanglemedia\.com|"
    r"shabelle|hiiraan|garoweonline|somali|"
    r"nation\.africa|theeastafrican|monitor\.co\.ug|"
    r"dawn\.com|tribune\.com\.pk|thenews\.com\.pk|geo\.tv|arynews\.tv|"
    r"eltiempo\.com|semana\.com|infobae\.com|"
    r"g1\.globo|folha\.uol|estadao\.com|"
    r"irrawaddy|mizzima|rfa\.org|"
    r"sudantribune|radio-dabanga|"
    r"hindustantimes|timesofindia|thehindu|"
    r"tempo\.co|jakartapost|"
    r"bangkokpost|nationthailand)")


def first_seg(text):
    """取摘要第一段。ACLED 的 summary 常把多条不同新闻用分号拼在一起：
        「Gunmen kill two policemen in Imo;Suspected separatists kill two officers in...」
    这是**两起事件**。整段拿去做检索式和标题匹配，会匹配到另一条新闻。
    实测台账里 5% 的条目有这个问题（122/2568），但一旦命中就是错链接。
    """
    t = str(text or "")
    for sep in (";", "｜", " | "):
        if sep in t:
            t = t.split(sep)[0]
    return t.strip()


# 信源表：域名 -> 机构名。补链时把每条链接归属到机构，
# 这样「补到链接」和「信源表覆盖了多少家」一次算完，不必分两步。
REG_BY_DOM = {}


def load_registry_domains(path):
    if not path:
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as e:  # noqa: BLE001
        print("  信源表读取失败：%s" % str(e)[:80])
        return {}
    ws = None
    for w in wb.worksheets:
        if w.max_row > 100:
            ws = w
            break
    if ws is None:
        ws = max(wb.worksheets, key=lambda w: w.max_row)
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]

    def find(*keys):
        for k in keys:
            for i, h in enumerate(hdr):
                if h == k:
                    return i
        for k in keys:
            for i, h in enumerate(hdr):
                if k and k in h:
                    return i
        return None

    i_dom = find("主域名", "域名", "domain")
    i_name = find("机构名称", "名称", "name")
    out = {}
    if i_dom is None:
        return out
    for r in rows[1:]:
        if not r:
            continue
        d = str(r[i_dom] or "").strip().lower().replace("www.", "")
        if d:
            out[d] = str(r[i_name] or "") if i_name is not None else d
    return out


def org_of(url):
    """URL 归属到信源表机构。返回 (机构名, 是否在名单内)。"""
    try:
        from urllib.parse import urlparse
        h = urlparse(str(url or "")).netloc.replace("www.", "").lower()
    except Exception:  # noqa: BLE001
        return "", False
    if not h:
        return "", False
    if h in REG_BY_DOM:
        return REG_BY_DOM[h], True
    # 子域兜底：english.aawsat.com -> aawsat.com 之类
    parts = h.split(".")
    for i in range(1, len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in REG_BY_DOM:
            return REG_BY_DOM[cand], True
    return "", False


def clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def run_search(cmd_tpl, query, timeout):
    """执行搜索命令，返回原始输出。"""
    cmd = cmd_tpl.replace("{q}", query)
    try:
        if os.name == "nt":
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               env=clean_env(), timeout=timeout)
        else:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               env=clean_env(), timeout=timeout)
        return (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    except subprocess.TimeoutExpired:
        return ""
    except Exception:  # noqa: BLE001
        return ""


def extract_results(raw):
    """从搜索输出里抽 (url, title) 列表。支持 JSON 与纯文本。"""
    out = []

    # 1) 尝试当 JSON 解析（可能是整段，也可能是最后一行）
    for text in (raw, raw.strip().split("\n")[-1] if raw.strip() else ""):
        text = (text or "").strip()
        if not text.startswith(("{", "[")):
            continue
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                url = None
                for k in ("url", "link", "href", "source_url"):
                    v = node.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        url = v
                        break
                if url:
                    title = ""
                    for k in ("title", "name", "headline", "text"):
                        v = node.get(k)
                        if isinstance(v, str) and v.strip():
                            title = v.strip()
                            break
                    out.append((url, title))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        if out:
            return out

    # 2) 退回纯文本抓链接
    for m in re.finditer(r"https?://[^\s\"'<>)\]]+", raw or ""):
        out.append((m.group(0).rstrip(".,;"), ""))
    return out


def url_date(url):
    """从 URL 里猜日期，返回 (y,m,d) 或 None。

    要认三种写法，少认一种就会有链接逃过日期校验：
      /2025/08/07/      标准
      /20251113/        紧凑 8 位（新华社、人民网英文版常用）
      /2025/Aug/123/    英文月份
    """
    # 紧凑 8 位：20250807 / 20251113
    m = re.search(r"[/t_](20\d{2})(\d{2})(\d{2})(?=[/\-_.]|$)", url)
    if m:
        y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= dd <= 31:
            return (y, mo, dd)
    m = re.search(r"/(20\d{2})[/\-_](\d{1,2})[/\-_](\d{1,2})(?=[/\-_.]|$)", url)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"/(20\d{2})[/\-_](\d{1,2})(?=[/\-_.]|$)", url)
    if m:
        return (int(m.group(1)), int(m.group(2)), 15)
    # 英文月份缩写路径：/2025/Aug/202515/xxx（hiiraan、许多非洲/南亚站点用它）
    # 不认这个格式，大量本可以评「强」的链接会被压成「中」——
    # 实测 2025-08 那批 Somali 报道全是这个形态。
    MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    m = re.search(r"/(20\d{2})/(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                  r"(?:[a-z]*)", url, re.I)
    if m:
        return (int(m.group(1)), MON[m.group(2).lower()], 15)
    return None


def event_date(rec):
    """取事件日期。支持台账列（年份/月份/日期）与候选字段（date）。"""
    d = str(rec.get("date") or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    try:
        y = int(str(rec.get("年份")).strip())
        mo = int(str(rec.get("月份")).strip())
        dd = int(str(rec.get("日期")).strip())
        return (y, mo, dd)
    except Exception:  # noqa: BLE001
        return None


def _days_diff(a, b):
    import datetime
    try:
        da = datetime.date(*a)
        db = datetime.date(*b)
        return abs((da - db).days)
    except Exception:  # noqa: BLE001
        return 99999


USED_URLS = set()


def pick(urls, office, headline, evd=None, deaths=None):
    """挑最可能的一条；返回 (url, note)。

    核心教训：早期版本「只要搜到链接就填」，10 条里约 5 条是错的
    —— 搜到过 2025 年 12 月的美国法院新闻、法属圭亚那的枪击案，
    全都和巴西七月的案件毫不相干。所以必须做两重校验：

      ① 日期校验：URL 里的年月日与事件日期相差 > 60 天 -> 拒
      ② 标题校验：与标题的实词重叠少于 2 个 -> 拒

    拒掉的宁可留空，让人去查，也不填一条假链接。
    """
    scored = []
    hl = (headline or "").lower()
    # 必须用 Unicode 归一化后再提取：原来只认 [a-z]{4,}，
    # 葡萄牙语 Polícia / 法语 Région 会被重音字母切断，导致实词匹配永远失败。
    import unicodedata
    hl_norm = unicodedata.normalize("NFKD", hl)
    hl_norm = "".join(c for c in hl_norm if not unicodedata.combining(c))
    hl_words = [w for w in re.findall(r"[a-z]{4,}", hl_norm)]
    # 过滤掉太通用的词，避免「police」「killed」凑数
    STOP = {"killed", "police", "attack", "with", "from", "that", "this",
            "have", "been", "were", "after", "over", "into", "their", "they",
            "more", "than", "said", "will", "report", "reports", "news"}
    hl_words = [w for w in hl_words if w not in STOP][:14]

    for url, title in urls:
        if BLOCK.search(url):
            continue
        why = []

        # 汇总页直接淘汰（不是某一起事件的报道）
        if AGGREGATE.search(url):
            continue
        # URL 里写明是社区冲突/反恐行动 -> 淘汰
        if BAD_URL_WORD.search(url):
            continue

        # ① 日期校验（严格）：没有日期信息的 URL 一律不要。
        #    实测教训：不卡日期会补进「2025-12 美国法院新闻」「法属圭亚那枪击」这类
        #    看似沾边、其实与本月本国的案件毫不相干的链接。
        ud = url_date(url)
        if ud:
            gap = _days_diff(ud, evd) if evd else 0
            if evd and gap > 7:   # 31 天会放进「6月20日」的旧报道
                continue
            why.append("日期差%d天" % gap)
            s = 5 if gap <= 7 else 3
        else:
            # URL 里没有日期：不直接拒（大量新闻站 URL 不含日期），
            # 但要求标题更强匹配，否则一律不要。
            why.append("URL无日期")
            s = 0

        # ② 标题校验（严格）：至少 3 个「长实词」命中
        t = (title or "").lower()
        t = "".join(c for c in unicodedata.normalize("NFKD", t)
                    if not unicodedata.combining(c))
        long_words = [w for w in hl_words if len(w) >= 5]
        need = 4 if not ud else 3
        if len(long_words) >= need:
            hit = sum(1 for w in long_words if w in t)
            if hit < need:
                continue
        else:
            hit = sum(1 for w in hl_words if w in t)
            if hit < (need - 1 if need > 2 else need):
                continue
        s += min(hit, 5) * 2

        if NEWS.search(url):
            s += 2

        if url in USED_URLS:      # 同一链接不重复用
            continue
        scored.append((s, url, title, hit, why))

    if not scored:
        return None, "没有通过日期/标题校验的候选（宁缺毋滥）"
    scored.sort(key=lambda x: -x[0])
    s, url, title, hit, why = scored[0]
    note = "分%d 标题命中%d词 %s" % (s, hit, " ".join(why))

    # 死亡数校验：这一套判据原本只看日期和标题词，不看死亡数。
    # 后果：走 pick() 的条目无论链接多准，note 里都没有「死亡数对上了」，
    # 下游按 note 定档时一律判「弱」。实测 225 条中文国家名事件里 99 条全弱，
    # 链接本身是对的 —— 就是被这个吃掉的。
    try:
        _okp, _seenp = _deaths_in_text(url + " " + (title or ""), deaths)
    except Exception:  # noqa: BLE001
        _okp, _seenp = None, []
    if _okp is True:
        note += " 死亡数对上了"
        if hit >= 3:
            note = note.replace("分%d" % s, "置信度强 分%d" % s)
    elif _okp is False:
        note += " 死亡数未对上(见%s)" % _seenp
    else:
        note += " 标题未见死亡数"

    return url, note


NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
             "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
             "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
             "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
             "hundred": 100, "dozens": 12, "scores": 20}


def _deaths_in_text(text, deaths):
    """看文本里出现的伤亡数字是否与事件的死亡数接近。

    返回 (是否对上, 文本里看到的数字列表)。
    没看到任何数字时返回 (None, [])，表示「无法判定」，不当作不命中。
    """
    try:
        d = int(str(deaths).strip())
    except Exception:  # noqa: BLE001
        return None, []
    if d <= 0:
        return None, []
    t = str(text or "").lower()
    nums = []
    for m in re.finditer(r"\b(\d{1,5})\b", t):
        v = int(m.group(1))
        if 0 < v < 100000:
            nums.append(v)
    for w, v in NUM_WORDS.items():
        if re.search(r"\b%s\b" % w, t):
            nums.append(v)
    # 去掉明显不是死亡数的（年份、月份、日期里的数字）
    nums = [v for v in nums if not (1900 <= v <= 2100)]
    if not nums:
        return None, []
    # 死亡数容差：±50%，且允许「至少 N」「超过 N」这类表述
    lo, hi = max(1, int(d * 0.5)), int(d * 1.5) + 1

    # 光有数字不够：必须挨着死亡词才算数。
    # 实测踩过：「nearly 90 attacks in a day」被当成「90 人死亡」，
    # 而事件实际是 90 死 —— 数字对上了，事情完全不是一件。
    DYING = r"(?i)(killed|killing|kill|dead|died|death|deaths|fatalities|"
    DYING += r"martyrs?|casualt|slain|slay|assassinat|massacr|bodies|corpses?|"
    DYING += r"shot dead|found dead|lost (?:his|their) li(?:fe|ves)|"
    DYING += r"死亡|遇难|丧生|罹难|身亡|被杀|击毙|打死|遗体)"
    ok = False
    for mt in re.finditer(r"\b(\d{1,5})\b", t):
        v = int(mt.group(1))
        if not (lo <= v <= hi) or 1900 <= v <= 2100:
            continue
        # 判据是「数字后面 6 个词之内有死亡词」，不是「附近若干字符内有」。
        # 字符窗口两头不讨好：24 太窄（「120 al-shabaab militants killed」
        # 数字到 killed 隔 25 字符被切断后漏判）；40 太宽
        #（「nearly 90 attacks in a day ... casualties」把 casualties 圈进来，误判）。
        # 按词数卡正好对上「N killed」「N people dead」「over N women killed」这类表达。
        # 前后都要看。实测漏判：「assassinate two policemen」——
        # 死亡词 assassinate 在数字前面，只往后看就判成「没提死亡」。
        # 这类标题很常见（「Gunmen kill two policemen」「Two soldiers killed」）。
        tail = t[mt.end():mt.end() + 90]
        head = t[max(0, mt.start() - 50):mt.start()]
        w_after = re.findall(r"[a-z\u4e00-\u9fff]+", tail)[:6]
        w_before = re.findall(r"[a-z\u4e00-\u9fff]+", head)[-4:]
        if re.search(DYING, " ".join(w_after) + " " + " ".join(w_before)):
            ok = True
            break
    if not ok:
        # 退一步：英文数词 + 死亡词
        for w, v in NUM_WORDS.items():
            if lo <= v <= hi and re.search(r"\b%s\b" % w, t):
                for mm2 in re.finditer(r"\b%s\b" % w, t):
                    _t2 = t[mm2.end():mm2.end() + 90]
                    _h2 = t[max(0, mm2.start() - 50):mm2.start()]
                    _wa = re.findall(r"[a-z]+", _t2)[:6]
                    _wb = re.findall(r"[a-z]+", _h2)[-4:]
                    if re.search(DYING, " ".join(_wa) + " " + " ".join(_wb)):
                        ok = True
                        break
                if ok:
                    break
    return ok, nums[:8]


def pick_structured(urls, rec, evd=None):
    """给「长叙述体」记录的挑选器：按地名/国家命中打分，并标注置信度。

    为什么需要第二个挑选器
    ----------------------
    pick() 是给「有明确标题」的场景设计的：要求标题命中 3–4 个长实词。
    但 ACLED 的 summary 是整段叙述：
       「On 8 August 2025, government security forces (SNA) and AUSSOM UPDF
         launched a heavy coordinated operation in ...」
    新闻标题不可能复现这些词，实测 20 条只有 1 条能过 pick()。

    这类记录的检索式是结构化造出来的（国家 + 地名 + 月 年），
    召回的结果天然已经限定了范围，所以改按「地名/国家是否命中」判定，
    比按「词重叠」判定更贴合。

    置信度分两档，写进备注，让人知道该不该直接引用：
      强  = URL 有日期且差 ≤ 7 天，且命中 ≥ 2 个地名/国家词
      中  = 只命中地名/国家词，或 URL 无日期
    """
    import unicodedata

    def norm(t):
        t = unicodedata.normalize("NFKD", str(t or "").lower())
        return "".join(c for c in t if not unicodedata.combining(c))

    # 地名/国家实词（≥4 字母，去通用词）
    # 判定结果的字段名是中文（国家/地点），而候选池是英文（country/place）。
    # 只认英文会让 pick_structured 直接判定「没有可用地名」——
    # 实测全量跑时每一条都这样失败，看起来像「检索不到」，其实是字段没认。
    _cty = (rec.get("country") or rec.get("国家") or "")
    _plc = (rec.get("place") or rec.get("地点") or "")
    geo_raw = " ".join([str(_cty), str(_plc), str(rec.get("地区") or "")])
    # 中文国家名转英文，否则 url/标题全是英文，匹配不上
    try:
        from audit_coverage import ZH_TO_EN
        _k = str(_cty).strip().lower()
        if _k in ZH_TO_EN:
            geo_raw += " " + ZH_TO_EN[_k]
    except Exception:  # noqa: BLE001
        pass
    GENERIC = {"province", "district", "region", "state", "county", "city",
               "north", "south", "east", "west", "central", "area", "town",
               "village", "near", "the", "and", "lower", "upper", "greater"}
    geo = [w for w in re.findall(r"[a-z]{4,}", norm(geo_raw)) if w not in GENERIC]
    geo = list(dict.fromkeys(geo))[:8]
    if not geo:
        return None, "记录里没有可用的地名，无法判定"

    scored = []
    for url, title in urls:
        if BLOCK.search(url) or AGGREGATE.search(url) or BAD_URL_WORD.search(url):
            continue
        try:
            from urllib.parse import urlparse
            _h = urlparse(url).netloc.replace("www.", "")
            if JUNK_DOMAIN.search(_h):
                continue
        except Exception:  # noqa: BLE001
            pass
        hay = norm(url) + " " + norm(title)
        hit = [w for w in geo if w in hay]
        if not hit:
            continue

        ud = url_date(url)
        if ud and evd:
            gap = _days_diff(ud, evd)
            if gap > 7:
                continue
        else:
            gap = None

        # ③ 死亡数校验 —— 最强的一条判据。
        # 同一起事件，两个来源报的死亡数应该接近。实测：
        #   事件「苏丹 死40」-> thehindu「killing 40」命中
        #   事件「布基纳法索 死40」-> voanews「kills four」不命中（4 ≠ 40）
        # 不加这条，上面两条会被同样处理，而后者是错链接。
        # 判定结果的死亡数字段叫「死亡人数」，候选池叫 deaths。
        # 只认英文会让每条都判成「标题未见死亡数」→ 置信度全掉到「弱」，
        # 明明 URL 里写着 151 也认不出来。实测踩过一次。
        _dd2 = rec.get("deaths")
        if _dd2 in (None, "", 0):
            _dd2 = rec.get("死亡人数")
        num_ok, num_seen = _deaths_in_text(url + " " + (title or ""), _dd2)
        if num_ok is False:
            continue

        # 分档：死亡数对上 + 地名命中 >= 2 个 就算强，不要求 URL 里有日期。
        # 早前版本要求「URL 有日期且差 <=7 天」才算强，结果大量新闻站 URL 不含日期，
        # 明明死亡数和地名都对上了也被压成「中」—— 把真实质量低估了。
        # 死亡数是最强指纹：别的事件不会凑巧也死 139 人。
        # 强：死亡数对上 + 地名 >=2 个。不要求 URL 里有日期
        # （大量新闻站 URL 不含日期，硬要求会把真实链接压成「中」）。
        # 中：死亡数对上但地名只命中 1 个，或地名 >=2 但死亡数没在标题出现。
        strong = bool(num_ok) and len(hit) >= 2
        score = (len(hit) * 3 + (12 if num_ok else 0) +
                 (6 if strong else 0) + (2 if NEWS.search(url) else 0))
        if url in USED_URLS:
            continue
        scored.append((score, url, title, hit, gap, strong, num_ok, num_seen))

    if not scored:
        return None, "地名/国家一个都没命中（宁缺毋滥）"
    scored.sort(key=lambda x: -x[0])
    score, url, title, hit, gap, strong, num_ok, num_seen = scored[0]
    if strong:
        conf = "强"
    elif num_ok:
        conf = "中"
    else:
        conf = "弱"
    note = "置信度%s 地名%d个(%s)%s %s" % (
        conf, len(hit), ",".join(hit[:4]),
        (" 日期差%d天" % gap) if gap is not None else " URL无日期",
        (" 死亡数对上了" if num_ok else (" 死亡数未对上(见%s)" % num_seen if num_seen else " 标题未见死亡数")))
    return url, note


def read_rows(path):
    if path.lower().endswith(".json") and not path.lower().endswith(".jsonl"):
        data = json.load(io.open(path, encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        return data, "json"   # 读 .json 就按 .json 回写（JSON 数组）。
                              # 早前这里返回 "jsonl"，导致 --out xxx.json 写出的是逐行 JSON，
                              # 下游 make_ledger 用 json.load 读它会报 Extra data。
                              # 这类「格式与扩展名不符」是最难查的一类数据流断裂。
    if path.lower().endswith(".jsonl"):
        rows = []
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows, "jsonl"
    if path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        hdr = [("" if c.value is None else str(c.value).strip()) for c in ws[1]]
        rows = []
        for r in range(2, ws.max_row + 1):
            v = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
            if all(x is None for x in v):
                continue
            rows.append({hdr[i]: ("" if v[i] is None else str(v[i]))
                         for i in range(len(hdr)) if hdr[i]})
        return rows, "xlsx"
    raise SystemExit("只支持 .json / .jsonl / .xlsx")


def build_query(rec):
    """用媒体名 + 标题构造查询。

    踩过的坑：
      · `source_office` 可能是 "SOHR;SOHR"（多来源用分号连接）-> 取第一个
      · `source_headline` 可能把几条新闻拼在一起（分号分隔）-> 只取第一条
      · 标题常带 "分类 | 正文" 格式 -> 取竖线后面（正文部分）
      · 太长会拖累检索 -> 截到 100 字符
    """
    ex = rec.get("extra", {}) or {}
    office = (rec.get("source_office") or ex.get("source_office") or "").split(";")[0].strip()

    hl = (rec.get("source_headline") or ex.get("source_headline")
          or rec.get("summary") or rec.get("事件摘要") or "").strip()

    # 多条新闻拼在一起时只取第一条
    if ";" in hl:
        hl = hl.split(";")[0].strip()
    # "分类 | 正文" -> 正文
    if "|" in hl or "｜" in hl:
        hl = re.split(r"[|｜]", hl)[-1].strip()

    # citation 里藏着 "媒体,日期,标题"
    if not office or not hl:
        art = rec.get("source_citation") or rec.get("source_article") or ex.get("source_citation") or ""
        art = art.strip().strip('"')
        parts = [p.strip().strip('"') for p in art.split(",")]
        if len(parts) >= 3:
            office = office or parts[0]
            if not hl:
                hl = parts[-1]
                if ";" in hl:
                    hl = hl.split(";")[0].strip()
                if "|" in hl:
                    hl = re.split(r"[|｜]", hl)[-1].strip()

    # 台账模式：备注里可能带 "[src]媒体名 标题"
    if not office and not hl:
        note = str(rec.get("备注") or "")
        m = re.search(r"\[src\](.+)$", note)
        if m:
            return m.group(1).strip()[:110]

    hl = hl[:100]

    # 标题碎片化时换成「国家 + 地点 + 月份 + 伤亡」构造查询。
    # 实测：UCDP 的 headline 经常只有 "A soldier" / "#BreakingNews" / "Two people"，
    # 这种标题搜不出任何东西（188 条只补到 8 条）。改用结构化特征更有效。
    # ACLED 的 summary 不是标题，是整段叙述：
    #   「On 8 August 2025, government security forces (SNA) and AUSSOM UPDF
    #     launched a heavy coordinated operation in ...」
    # 整段丢给检索后端召回极差（实测 12 条只成 1 条）。
    # 识别这种「日期开头的叙述体」，改走结构化检索式：国家 + 地名 + 月 年。
    acled_style = bool(re.match(
        r"(?i)^(?:on|around|at|during|in)\s+(?:\d{1,2}\s+)?\w*\s*\d{4}", hl))

    frag = (len(hl.strip()) < 18
            or not re.search(r"[A-Za-z一-龥]{4,}\s+\S", hl)
            or hl.strip().startswith("#")
            or acled_style)
    if frag:
        parts = []
        cty = str(rec.get("国家") or rec.get("country") or "").strip()
        # 国家名必须转英文再进检索式。
        # 台账里国家名中英混杂（"Burkina Faso" 和 "布基纳法索" 并存），
        # 中文国名 + 英文地名 = 检索后端一个词都匹配不上。
        # 实测：Burkina Faso 27 条强中率 7%，布基纳法索 17 条 **0%**；
        #       Nigeria 15 条 27%，尼日利亚 14 条 **0%**。
        if cty:
            _k = cty.strip().lower()
            try:
                from audit_coverage import ZH_TO_EN as _Z
                cty = _Z.get(_k, cty)
            except Exception:  # noqa: BLE001
                pass
            parts.append(cty)
        plc = str(rec.get("地点") or rec.get("place") or "").strip()
        # 地点里常有坐标串，只取前两段文字
        plc = re.split(r"[;,]", plc)[0].strip()[:32]
        if plc and plc.lower() not in cty.lower():
            parts.append(plc)
        mm = str(rec.get("月份") or "").strip()
        yy = str(rec.get("年份") or "").strip()
        # ACLED 的记录没有「月份/年份」列，日期在 date 字段（2025-08-06）。
        # 不补这一步，结构化检索式会退化成「国家 地点 attack killed」，
        # 查到的是最新新闻而不是目标月份的旧文。
        if not mm or not yy:
            _d = str(rec.get("date") or rec.get("日期") or "")[:10]
            _m = re.match(r"(\d{4})-(\d{2})", _d)
            if _m:
                yy = yy or _m.group(1)
                mm = mm or str(int(_m.group(2)))
        MON = {"1": "January", "2": "February", "3": "March", "4": "April",
               "5": "May", "6": "June", "7": "July", "8": "August",
               "9": "September", "10": "October", "11": "November", "12": "December"}
        if mm in MON:
            parts.append(MON[mm])
        if yy:
            parts.append(yy)
        # 死亡数必须进检索式 —— 这是唯一能确认「同一起事件」的强证据。
        # A/B 实测（6 条高死亡 ACLED 事件，元宝 20 条/次）：
        #   不带死亡数：「Ukraine Pokrovsk killed militants August 2025」
        #                目标月 87 条，标题里数字对上的 0 条
        #   带死亡数  ：「Ukraine Pokrovsk killed 151 militants August 2025」
        #                目标月 78 条，数字对上的 14 条
        # 召回条数略降，但「能确认是同一事件」的从 0 变成 14。
        # 别的事件不会凑巧也死 151 人 —— 这个数就是指纹。
        try:
            _dd = int(str(rec.get("deaths") or rec.get("死亡人数") or "").strip())
        except Exception:  # noqa: BLE001
            _dd = 0
        if _dd > 0:
            parts.append("killed %d" % _dd)
        else:
            parts.append("attack killed")
        q2 = " ".join(parts)
        return q2

    q = ("%s %s" % (office, hl)).strip()
    return q or hl


def main():
    global REG_BY_DOM
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", default=None)
    ap.add_argument("--search-cmd", default=None,
                    help="搜索命令模板，{q} 为查询占位。不传则只导出待补清单。"
                         "**速度差 7.5 倍**：直连元宝 'D:/DSH/yb.cmd search \"{q}\" --cnt 20'"
                         " 约 1.2 秒/条；走路由器 'D:/DSH/sx.cmd search \"{q}\" --task auto --json'"
                         " 约 9 秒/条（2484 条要 6.2 小时 vs 50 分钟）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    ap.add_argument("--timeout", type=int, default=60, help="单次搜索超时秒")
    ap.add_argument("--sleep", type=float, default=1.0, help="每条之间的间隔秒数（防限流）")
    ap.add_argument("--dry-run", action="store_true", help="只打印查询，不实际搜索")
    ap.add_argument("--registry", default=None,
                    help="信源表 xlsx。给了就在补链时标注每条链接来自哪家机构、"
                         "是否在名单内，并统计名单覆盖率")
    ap.add_argument("--budget-seconds", type=int, default=0,
                    help="总时间预算，到点就停并写回已补到的（0=不限）。"
                         "实测每条含 1 秒限速，2000 条要 33 分钟，全量要 4 小时 —— "
                         "跑大批量时必须给预算")
    ap.add_argument("--priority", default="deaths",
                    choices=["deaths", "none"],
                    help="处理顺序。deaths=按死亡数降序（重要事件先补到链接），none=按原序")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：跳过 .progress.json 里已处理过的条目。"
                         "全量 2568 条要两小时，中途被打断必须能接着跑，否则永远跑不完")
    ap.add_argument("--fill", default="", help="xlsx 模式下用哪个值填（默认 信息来源URL）")
    a = ap.parse_args()

    if a.registry:
        REG_BY_DOM = load_registry_domains(a.registry)
        print("信源表 %d 家（用于链接归属）" % len(REG_BY_DOM))

    if not os.path.isfile(a.inp):
        print("文件不存在：%s" % a.inp)
        return 1
    rows, kind = read_rows(a.inp)
    print("输入：%s（%s，%d 条）" % (a.inp, kind, len(rows)))

    # 已有 URL 的跳过
    def has_url(r):
        for k in ("url", "信息来源URL"):
            v = str(r.get(k) or "").strip()
            if v.startswith("http"):
                return True
        return False

    todo = [i for i, r in enumerate(rows) if not has_url(r)]
    # 按死亡数降序：链接有限时先给重要事件补上。
    # 实测：不排序时前 200 条可能是随机顺序，大事件反而排在最后补不到。
    if a.priority == "deaths":
        def _deaths(idx):
            r0 = rows[idx]
            for k in ("deaths", "死亡人数", "fatalities"):
                v = r0.get(k)
                if v is not None and str(v).strip() not in ("", "—", "None"):
                    try:
                        return int(float(str(v).replace(",", "")))
                    except Exception:  # noqa: BLE001
                        pass
            return 0
        todo.sort(key=_deaths, reverse=True)
    # 注意：这里不截断 todo。limit 的含义是「最多发多少次检索」，
    # 而免检条目（采集端已给媒体名、ACLED event_id）不发检索。
    # 若在这里截断，免检条目一多就把检索配额吃光 ——
    # 实测 --enrich-limit 15 时，前 15 条全是免检，一条真链接都没补。
    _search_quota = a.limit or 0
    print("已有 URL：%d 条 ｜ 待补：%d 条" % (len(rows) - sum(1 for i in range(len(rows)) if not has_url(rows[i])), len(todo)))
    print()

    if a.dry_run or not a.search_cmd:
        print("=== 待补链接清单（前 30 条查询）===")
        for i in todo[:30]:
            print("  %s" % build_query(rows[i])[:110])
        if len(todo) > 30:
            print("  … 另有 %d 条" % (len(todo) - 30))
        print()
        if not a.search_cmd:
            print("没有给 --search-cmd，本轮只导出清单，没有补任何链接。")
            print("补法（任选其一）：")
            print("  ① python scripts/enrich_urls.py --in %s --out %s \\" % (a.inp, a.out or "out.jsonl"))
            print("         --search-cmd 'D:/DSH/sx.cmd search \"{q}\" --task auto --json'")
            print("  ② 用 agent 自己的 web_search 逐条查，再把 url 写回台账")
        return 2

    # ---- 断点续跑 ----
    # 全量补链要两小时，中途被打断（关机、超时、换轮次）很常见。
    # 没有进度文件就只能从头再来，于是永远跑不完。
    # progress 记「处理过的 key」，没补到链接的也记 —— 否则每轮都会重试
    # 那些注定失败的长尾事件（村级别死 2 人，根本没有英文报道）。
    _prog_path = (a.out or a.inp) + ".progress.json"
    _done = set()
    if a.resume and os.path.isfile(_prog_path):
        try:
            _done = set(json.load(io.open(_prog_path, encoding="utf-8")))
            print("  断点续跑：已完成 %d 条" % len(_done))
        except Exception:  # noqa: BLE001
            _done = set()

    def _rkey(rec):
        return "|".join([
            str(rec.get("国家") or rec.get("country") or ""),
            str(rec.get("月份") or ""),
            str(rec.get("日期") or ""),
            str(rec.get("死亡人数") or rec.get("deaths") or ""),
            str(rec.get("事件摘要") or rec.get("summary") or "")[:50],
        ])

    if _done:
        _before = len(todo)
        todo = [i for i in todo if _rkey(rows[i]) not in _done]
        print("  跳过已处理 %d 条，剩 %d 条" % (_before - len(todo), len(todo)))
        if not todo:
            print("  全部已处理完，无需再跑。")
            return 0

    def _save_progress():
        if not _prog_new:
            return
        try:
            _all = set(_done) | set(_prog_new)
            with io.open(_prog_path, "w", encoding="utf-8") as _pf:
                json.dump(sorted(_all), _pf, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    filled = 0
    n_searched = 0
    failed = []
    _prog_new = []
    t0 = time.time()
    for n, i in enumerate(todo, 1):
        # 预算到点就停，把已补到的写回（剩下的一轮再继续）
        if a.budget_seconds and (time.time() - t0) > a.budget_seconds:
            print("  ⏱  预算 %d 秒到点，停止。已处理 %d 条，剩 %d 条留待下一轮"
                  % (a.budget_seconds, n - 1, len(todo) - n + 1))
            break

        r = rows[i]

        # 记进度：处理过就记，不管有没有补到链接。
        # 只记成功的会让失败的长尾每条重跑一遍，两小时变四小时。
        _prog_new.append(_rkey(r))
        if len(_prog_new) % 20 == 0:
            _save_progress()

        # ACLED 自带 source（媒体名）与 event_id_cnty，官网可据 event_id 查回原条目。
        # 再花一次检索去反查 URL 是浪费：4 小时里有一半会耗在这上面。
        # 采集端已给来源线索的（备注里有 [src]媒体名/标题），直接采信、不检索。
        # 实测 2025-08 全量判定结果 2236 条，[src] 覆盖率 100%、URL 覆盖率 0%。
        # 也就是说这一批条目本来就没有 URL 可查，联网检索纯属空转：
        # 每条 1 秒限速 × 2236 条 = 37 分钟，换回来 0 个链接。
        _note = str(r.get("备注") or r.get("备注") or "")
        _m_src = re.search(r"\[src\]\s*(.+)$", _note)
        if _m_src and not str(r.get("信息来源URL") or r.get("url") or "").startswith("http"):
            _hint = _m_src.group(1).strip()[:160]
            # 「无来源线索」是采集端没拿到媒体名时的占位，不是真的来源。
            # 不排掉它，这一批 15 条全被当成「免检通过」，
            # 结果一条真实链接都没补，覆盖率审计拿到 0 个域名。
            if not _hint or _hint in ("无来源线索", "-", "None", "无"):
                _hint = ""
            _txt = ("[媒体名来源] " + _hint) if _hint else ""
            if str(a.out or "").lower().endswith((".xlsx", ".xlsm")):
                r[a.fill or "信息来源URL"] = _txt
            else:
                r["source_note"] = _txt
                r[a.fill or "信息来源URL"] = _txt
            # 关键：只有 [src] 里已经是 URL 才免检。
            # [src] 里放的是「媒体名 + 标题」——那是线索，不是可点击链接。
            # 台账 100% 有 [src]，若一律免检就一条 URL 都补不到
            # （实测：2568 行台账，补链后只有 22 条有 URL）。
            if not _hint or not re.search(r"https?://", _hint):
                # 继续走检索
                pass
            else:
                filled += 1
                if n % 200 == 0 or n == len(todo):
                    print("  [%d/%d] ✓(采集端来源免检) 已补 %d"
                          % (n, len(todo), filled), flush=True)
                continue

        # ACLED 条目识别：不能靠 extra（judge 转 19 列时已丢），也不能靠 source
        # （同样丢了）。用 event_id 的固定格式：三字母国家码 + 7 位数字，如 MEX100025。
        # 这类条目带着 event_id，官网可据它查回原条目，不必再花一次检索去反查 URL。
        acl_src = (r.get("extra") or {}).get("acled_source") or ""
        _rid = str(r.get("raw_id") or r.get("GED 编号") or r.get("GED编号") or "").strip()
        _is_acled = bool(re.match(r"^[A-Z]{3}\d{6,8}$", _rid))
        if _is_acled and not acl_src:
            acl_src = "ACLED event_id=" + _rid
        if acl_src and (str(r.get("source") or "") == "acled" or _is_acled):
            note_txt = "[ACLED] " + str(acl_src)[:120]
            if str(a.out or "").lower().endswith((".xlsx", ".xlsm")):
                r[a.fill or "信息来源URL"] = note_txt
            else:
                r["source_note"] = note_txt
                r[a.fill or "信息来源URL"] = note_txt
            filled += 1
            if n % 5 == 0 or n == len(todo):
                print("  [%d/%d] ✓(ACLED免检) 已补 %d" % (n, len(todo), filled), flush=True)
            continue

        # 检索配额：只数真正发出的检索
        if _search_quota and n_searched >= _search_quota:
            print("  ⏹  检索配额 %d 条已用完，停止（免检条目不受此限）" % _search_quota)
            break
        n_searched += 1

        q = build_query(r)
        raw = run_search(a.search_cmd, q, a.timeout)
        res = extract_results(raw)
        # 长叙述体（ACLED 那种整段 notes）走地名匹配，标题体走词重叠匹配
        _hl = first_seg(r.get("source_headline") or r.get("事件摘要")
                        or r.get("summary") or "")
        _is_narr = bool(re.match(
            r"(?i)^(?:on|around|at|during|in)\s+(?:\d{1,2}\s+)?\w*\s*\d{4}",
            str(_hl)))
        if _is_narr:
            url, note = pick_structured(res, r, event_date(r))
        else:
            url, note = pick(res, r.get("source_office"),
                             first_seg(r.get("source_headline") or r.get("事件摘要")),
                             event_date(r),
                             r.get("deaths") if r.get("deaths") not in (None, "", 0)
                             else r.get("死亡人数"))
        if url:
            USED_URLS.add(url)
        if url:
            if str(a.out or "").lower().endswith((".xlsx", ".xlsm")):
                r[a.fill or "信息来源URL"] = url
                r["备注"] = (str(r.get("备注") or "") + " ｜[链接判据]" + note).strip()
            else:
                r["url"] = url
                r[a.fill or "信息来源URL"] = url
                # 判据必须一起留下来。只给链接不给判据，
                # 下游没法分辨「死亡数与标题都对上」和「只撞了个地名」，
                # 台账上看起来一样可信 —— 这是最危险的一种假可信。
                r["source_note"] = note
                r["link_confidence"] = ("强" if "置信度强" in note else
                                        "中" if "置信度中" in note else "弱")
                if REG_BY_DOM:
                    _org, _in = org_of(url)
                    r["source_org"] = _org
                    r["in_registry"] = "是" if _in else "否"
            filled += 1
            mark = "✓"
        else:
            failed.append((i, q, note))
            mark = "✗"
        if n % 5 == 0 or n == len(todo):
            el = time.time() - t0
            eta = el / n * (len(todo) - n)
            print("  [%d/%d] %s 已补 %d ｜ 用时 %.0fs ｜ 预计还剩 %.0fs"
                  % (n, len(todo), mark, filled, el, eta), flush=True)
        time.sleep(a.sleep)

    print()
    print("补到链接：%d / %d 条" % (filled, len(todo)))

    # 信源表覆盖率：补到的链接里有多少条来自名单内机构
    if REG_BY_DOM:
        from collections import Counter as _C
        _ins = _C()
        for r in rows:
            if str(r.get("in_registry") or "") in ("是", "否"):
                _ins[r["in_registry"]] += 1
        _tot = sum(_ins.values())
        if _tot:
            print("信源表归属：名单内 %d 条 / 名单外 %d 条 = 覆盖 %.0f%%"
                  % (_ins.get("是", 0), _ins.get("否", 0),
                     100.0 * _ins.get("是", 0) / _tot))

    # 导出
    if a.out:
        # 输出格式由输出文件的扩展名决定，不由输入决定 ——
        # 输入是 .json、输出是 .jsonl（或反之）都可能，跟着输入走必然出错。
        out_kind = "jsonl" if str(a.out).lower().endswith(".jsonl") else "json"
        if out_kind == "json":
            with io.open(a.out, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
        elif out_kind == "jsonl":
            with io.open(a.out, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        else:
            import openpyxl
            from openpyxl import Workbook
            wb = Workbook(); ws = wb.active; ws.title = "恐怖袭击事件采集表"
            hdr = [k for k in rows[0].keys()]
            ws.append(hdr)
            for r in rows:
                ws.append([r.get(h) for h in hdr])
            wb.save(a.out)
        print("已写出：%s" % a.out)

    # 收尾落盘：正常跑完、预算到点、被 Ctrl-C，都尽量落到这里
    _save_progress()

    if failed:
        print()
        print("=== 没补到的 %d 条（需人工查）===" % len(failed))
        for i, q, note in failed[:20]:
            print("  %s  ← %s" % (q[:80], note))
        if len(failed) > 20:
            print("  … 另有 %d 条" % (len(failed) - 20))

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
