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
    hl_words = [w for w in re.findall(r"[a-z]{4,}", hl)]
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
        if not ud:
            continue
        gap = _days_diff(ud, evd) if evd else 0
        if evd and gap > 7:      # 从 31 天收到 7 天：实测 31 天会放进「6月20日」的旧报道
            continue
        why.append("日期差%d天" % gap)
        s = 5 if gap <= 7 else 3

        # ② 标题校验（严格）：至少 3 个「长实词」命中
        t = (title or "").lower()
        long_words = [w for w in hl_words if len(w) >= 5]
        if len(long_words) >= 3:
            hit = sum(1 for w in long_words if w in t)
            if hit < 3:
                continue
        else:
            hit = sum(1 for w in hl_words if w in t)
            if hit < 2:
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
        return data, "jsonl"          # 写出时同样逐行，两种格式通用
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
        r = rows[i]
        q = build_query(r)
        raw = run_search(a.search_cmd, q, a.timeout)
        res = extract_results(raw)
        url, note = pick(res, r.get("source_office"),
                         r.get("source_headline") or r.get("事件摘要"),
                         event_date(r))
        if url:
            USED_URLS.add(url)
        if url:
            if kind == "jsonl":
                r["url"] = url
            else:
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
        if kind == "jsonl":
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
