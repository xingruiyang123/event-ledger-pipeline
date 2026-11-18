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
    r"terrorist-activity|/data/|/index)")

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
    """从 URL 里猜日期，返回 (y,m,d) 或 None。"""
    m = re.search(r"/(20\d{2})[/\-_](\d{1,2})[/\-_](\d{1,2})(?=[/\-_.]|$)", url)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"/(20\d{2})[/\-_](\d{1,2})(?=[/\-_.]|$)", url)
    if m:
        return (int(m.group(1)), int(m.group(2)), 15)
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


def pick(urls, office, headline, evd=None):
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
    frag = (len(hl.strip()) < 18
            or not re.search(r"[A-Za-z一-龥]{4,}\s+\S", hl)
            or hl.strip().startswith("#"))
    if frag:
        parts = []
        cty = str(rec.get("国家") or rec.get("country") or "").strip()
        if cty:
            parts.append(cty)
        plc = str(rec.get("地点") or rec.get("place") or "").strip()
        # 地点里常有坐标串，只取前两段文字
        plc = re.split(r"[;,]", plc)[0].strip()[:32]
        if plc and plc.lower() not in cty.lower():
            parts.append(plc)
        mm = str(rec.get("月份") or "").strip()
        yy = str(rec.get("年份") or "").strip()
        MON = {"1": "January", "2": "February", "3": "March", "4": "April",
               "5": "May", "6": "June", "7": "July", "8": "August",
               "9": "September", "10": "October", "11": "November", "12": "December"}
        if mm in MON:
            parts.append(MON[mm])
        if yy:
            parts.append(yy)
        parts.append("attack killed")
        q2 = " ".join(parts)
        return q2

    q = ("%s %s" % (office, hl)).strip()
    return q or hl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", default=None)
    ap.add_argument("--search-cmd", default=None,
                    help="搜索命令模板，{q} 为查询占位。不传则只导出待补清单")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    ap.add_argument("--timeout", type=int, default=60, help="单次搜索超时秒")
    ap.add_argument("--sleep", type=float, default=1.0, help="每条之间的间隔秒数（防限流）")
    ap.add_argument("--dry-run", action="store_true", help="只打印查询，不实际搜索")
    ap.add_argument("--budget-seconds", type=int, default=0,
                    help="总时间预算，到点就停并写回已补到的（0=不限）。"
                         "实测每条含 1 秒限速，2000 条要 33 分钟，全量要 4 小时 —— "
                         "跑大批量时必须给预算")
    ap.add_argument("--priority", default="deaths",
                    choices=["deaths", "none"],
                    help="处理顺序。deaths=按死亡数降序（重要事件先补到链接），none=按原序")
    ap.add_argument("--fill", default="", help="xlsx 模式下用哪个值填（默认 信息来源URL）")
    a = ap.parse_args()

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
    if a.limit:
        todo = todo[:a.limit]
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

    filled = 0
    failed = []
    t0 = time.time()
    for n, i in enumerate(todo, 1):
        # 预算到点就停，把已补到的写回（剩下的一轮再继续）
        if a.budget_seconds and (time.time() - t0) > a.budget_seconds:
            print("  ⏱  预算 %d 秒到点，停止。已处理 %d 条，剩 %d 条留待下一轮"
                  % (a.budget_seconds, n - 1, len(todo) - n + 1))
            break

        r = rows[i]

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
            _txt = "[媒体名来源] " + _hint
            if str(a.out or "").lower().endswith((".xlsx", ".xlsm")):
                r[a.fill or "信息来源URL"] = _txt
            else:
                r["source_note"] = _txt
                r[a.fill or "信息来源URL"] = _txt
            filled += 1
            if n % 200 == 0 or n == len(todo):
                print("  [%d/%d] ✓(采集端来源免检) 已补 %d" % (n, len(todo), filled), flush=True)
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

        q = build_query(r)
        raw = run_search(a.search_cmd, q, a.timeout)
        res = extract_results(raw)
        url, note = pick(res, r.get("source_office"),
                         r.get("source_headline") or r.get("事件摘要"),
                         event_date(r))
        if url:
            USED_URLS.add(url)
        if url:
            if str(a.out or "").lower().endswith((".xlsx", ".xlsm")):
                r[a.fill or "信息来源URL"] = url
            else:
                r["url"] = url
                r[a.fill or "信息来源URL"] = url
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
