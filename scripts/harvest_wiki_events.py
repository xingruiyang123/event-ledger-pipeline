#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从维基百科「当前事件」日历批量提取「武装冲突与袭击」条目 —— 免费、无需密钥、每天更新。

为什么这个脚本是整套流程的基线
--------------------------------
大多数使用者手上只有一个 `web_search`。英文国际大媒体只报「大事件」，
一个尼日利亚村庄死 12 人不会被 Reuters 报，但它确实发生了。
所以靠「搜大媒体」永远拿不到完整台账 —— 无论你搜多少轮。

维基百科 `Portal:Current events/YYYY_Month_D` 每天的「Armed conflicts and attacks」
段落，是**唯一同时满足**下面四条的公开数据源：

  1. 免费、无需 API key、无需注册（任何能上网的人都能取）
  2. 每天一页、粒度到日（不会像月度汇总那样吞掉单起事件）
  3. 每条都标注原始出处（Reuters / AFP / 当地媒体），可直接回溯核验
  4. 覆盖全球、包含非洲萨赫勒等大媒体不报的地区

本脚本把这 31 页结构化提取成候选清单，再交给人工/模型按口径判定。
**它是「找全」的基线，不是终点** —— 提取后仍要：
  · 补 web_search 交叉（维基也会漏）
  · 逐条抓原文核验日期与伤亡
  · 按判定链逐条判「计入 / 不计入」

退出码：0 = 提取成功且有产出 | 2 = 提取到的条目少于期望（疑似抓取失败）| 1 = 无法判定

用法：
  python scripts/harvest_wiki_events.py --month 2026-08 --out 候选.jsonl
  python scripts/harvest_wiki_events.py --month 2026-08 --out 候选.jsonl --min-items 150
  python scripts/harvest_wiki_events.py --month 2026-08 --json      # 只看统计不许盘

注意：
  · 自动剔除明显不是事件的条目（地震、车祸、台风、火灾、塌方、体育意外、私人刑案）
  · 但「剔除」只做粗筛，最终判「计入/不计入」必须由口径文档决定，不由本脚本决定
"""
import argparse
import json
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

MONTHS = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}

# 「冲突与袭击」之后的段落标题（到达即停止）
STOP_HEADS = [
    "Disasters and accidents", "Politics and elections", "Health and environment",
    "International relations", "Law and crime", "Science and technology",
    "Sports", "Business and economy", "Arts and culture", "Death",
]

# 明显不是「武装冲突/袭击事件」的噪声（粗筛，宁放过不误杀）
NOISE = re.compile(
    r"(?i)"
    r"earthquake|quake|tsunami|flood|landslide|cyclone|typhoon|hurricane|"
    r"volcano|wildfire|drought|famine|"
    r"bus (?:crash|crashed|overturn)|car (?:crash|crashed)|plane crash|"
    r"train (?:crash|derail)|collision|road accident|traffic accident|"
    r"landfill collaps|building collaps|bridge collaps|"
    r"fire at a|fire broke out|blaze at|"
    r"hospitalis|hospitaliz|"
    r"stage at the|race is neutralised|"
    r"football|cricket|basketball|Olympic|FIFA|"
    r"stock market|inflation|currency"
)

# 有杀伤/袭击语义
VIOLENT = re.compile(
    r"(?i)\b(?:kill(?:ed|s|ing)?|dead|death|died|fatalit|wound(?:ed|s)?|"
    r"attack(?:ed|s)?|bomb(?:ed|ing)?|blast|ambush(?:ed)?|gunmen|gunman|"
    r"militant|insurgent|terror|IED|suicide|massacre|abduct(?:ed)?|"
    r"kidnap(?:ped)?|raid|shoot(?:ing)?|shelling|strike|offensive|siege)\w*"
)

# 国家力量/国家行为体特征（本人所在库的硬门槛是「非国家主体」，命中这些多半要排除）
STATE_HINT = re.compile(
    r"(?i)armed forces|air ?force|airstrike|drone strike|military (?:strike|operation)|"
    r"army (?:says|announces|kills)|police (?:kills|killed|raid)|"
    r"security forces (?:kills|kill|raid)|soldiers (?:kill|killed)"
)


def month_pages(month):
    """按 --month 的年份生成页面名，并按大小月决定天数。

    原实现把年份写死成 2026，查 2025-08 也在抓 2026 年的页面；
    且用 range(1,32) 不判大小月，2 月会多抓 2/29-2/31 三个不存在的页面。
    """
    import calendar
    y, m = month.split("-")
    try:
        ndays = calendar.monthrange(int(y), int(m))[1]
    except Exception:  # noqa: BLE001
        ndays = 31
    return [("%s_%s_%d" % (y, MONTHS[m], d),
             "https://en.wikipedia.org/wiki/Portal:Current_events/%s_%s_%d"
             % (y, MONTHS[m], d)) for d in range(1, ndays + 1)]


def _clean_urls(name):
    """同一个维基页面有三种可用形态，按可靠性排序。"""
    return [
        "https://en.wikipedia.org/w/index.php?title=Portal:Current_events/%s&action=raw" % name,
        "https://en.wikipedia.org/wiki/Portal:Current_events/%s" % name,
    ]


def _direct(url, timeout, retries):
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
    except Exception:  # noqa: BLE001
        return None
    s = requests.Session()
    s.trust_env = False           # 忽略 HTTP_PROXY：代理没开时会把一切请求拖死
    s.headers.update({"User-Agent": UA})
    for i in range(retries):
        try:
            r = s.get(url, timeout=timeout, verify=False)
            if r.status_code == 200:
                if _is_empty_day_page(r.text):
                    return r.text
                if len(r.text) > 500 and _looks_like_wikitext(r.text, url):
                    return r.text
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6 * (i + 1))
    return None


def _is_empty_day_page(text):
    """维基某天的「当前事件」页可能本身就只有一个空框架（没有当日条目）。

    真实样本（2026-08-04）：
        {{Current events|year=2026|month=08|day=4|top=yes}}
        \*
        {{Current events|year=2026|month=08|day=4|bottom=yes}}
    这不是抓取失败，是当天确实没编辑条目。必须与失败分开，否则会误报。
    """
    if not text:
        return False
    t = text.strip()
    if "Jump to content" in t[:2000]:
        return False
    # 外部抓取工具（curl / wget / 任意能取网页的命令）会在前面加几行包装头，所以不能只认精确子串
    has_frame = ("Current events" in t and "year=" in t) or "top=yes" in t
    has_news_marker = "All news items below this line" in t
    # 页面里有没有真正的条目？没有 * 开头且长于 25 字的行，就算空页
    real_items = 0
    for line in t.split(chr(10)):
        ls = line.strip().lstrip(chr(92)).strip()
        if ls.startswith("*") and len(ls) > 25:
            real_items += 1
    if (has_frame or has_news_marker) and real_items == 0:
        return True
    return False


def _looks_like_wikitext(text, url):
    """判断抓回来的到底是不是事件正文。

    真实教训：某些页面对 raw 接口会返回网页导航壳（"Jump to content / Main menu /
    Navigation ..."），长度上千字符、状态码 200，看起来像抓成功了，其实一个字的事件都没有。
    必须显式识别，否则会被当成「当天无事件」。
    """
    if not text:
        return False
    if _is_empty_day_page(text):
        return True          # 空页也是一种合法结果，交给上层记为「当日空页」
    head = text[:2000]
    # 典型噪音特征：网页壳
    if "Jump to content" in head and "move to sidebar" in head:
        return False
    if text.count("Main menu") >= 2:
        return False
    # 必须是事件正文的特征：出现段标题或大量 * 列表
    if re.search(r"Armed conflicts and attacks", text):
        return True
    if text.count(chr(10)+"*") >= 3:
        return True
    return False


def _by_cmd(cmd, url, timeout):
    """调用外部抓取命令，{url} 占位。本机有什么抓取工具都能接上。"""
    import shlex
    import subprocess
    argv = [a.replace("{url}", url) for a in shlex.split(cmd)]
    env = dict(os.environ)
    env["NO_PROXY"] = "*"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        out = r.stdout or ""
        # 空页也是合法结果：只要它是维基的当日页框架，就必须让上层看见，
        # 由上层记成「当日空页」，而不是被这里挡掉、误报成抓取失败。
        if _is_empty_day_page(out):
            return out
        if len(out) > 500 and _looks_like_wikitext(out, url):
            return out
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_page(name, cmd=None, timeout=25, retries=2):
    """抓一天的维基当前事件页。

    后端顺序：
      1) --fetch-cmd 指定的外部命令（推荐：任何能取网页的 CLI 都行）
      2) 直连 requests
    两种都失败就返回 None —— 让调用方明确记成「抓取失败」，不许当成「当天无事件」。
    """
    for url in _clean_urls(name):
        if cmd:
            t = _by_cmd(cmd, url, timeout * 2)
            if t:
                return t
        t = _direct(url, timeout, retries)
        if t:
            return t
    return None


def extract_section(text):
    """截出「Armed conflicts and attacks」段（到下一个顶层段标题为止）。

    维基 raw wikitext 里段标题有多种写法，必须全兼容：
        '''Armed conflicts and attacks'''      ← 实际最常见的
        == Armed conflicts and attacks ==
        === Armed conflicts and attacks ===
        Armed conflicts and attacks                （裸标题行）
    只认 == == 会匹配不到，页面抓成功却被当成「当天无事件」。
    """
    pats = [
        r"'{2,3}\s*Armed conflicts and attacks\s*'{2,3}",
        r"={2,4}\s*Armed conflicts and attacks\s*={2,4}",
        r"^\s*Armed conflicts and attacks\s*$",
    ]
    m = None
    for p in pats:
        m = re.search(p, text, re.M)
        if m:
            break
    if not m:
        return ""

    start = m.end()
    tail = text[start:]
    stop = len(tail)
    for h in STOP_HEADS:
        for pat in (r"'{2,3}\s*" + re.escape(h) + r"\s*'{2,3}",
                    r"={2,4}\s*" + re.escape(h) + r"\s*={2,4}"):
            mm = re.search(pat, tail)
            if mm:
                stop = min(stop, mm.start())
    return tail[:stop]


def extract_bullets(seg):
    """从段落里抽「以 * 开头的行」，去掉 wikitext 标记，返回纯文本条目。"""
    out = []
    for line in seg.split("\n"):
        s = line.strip()
        if not s.startswith("*"):
            continue
        # 去掉多级项目符号
        s = re.sub(r"^[\*#:]+", "", s).strip()
        if len(s) < 25:
            continue
        # 去 wiki 链接 [[a|b]] -> b，[[a]] -> a
        s = re.sub(r"\[\[([^\]\|]+)\|([^\]]+)\]\]", r"\2", s)
        s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
        # 去外链 [url 标题] -> （标题）
        s = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"（\1）", s)
        s = re.sub(r"\[https?://[^\]]+\]", "", s)
        s = re.sub(r"''+", "", s)
        s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) >= 25:
            out.append(s)
    return out


def guess_country(s):
    """粗猜国家/地区（仅用于分组与统计，不作为最终字段）。"""
    KNOWN = [
        "Nigeria", "Somalia", "Mali", "Burkina Faso", "Niger", "Chad", "Cameroon",
        "Democratic Republic of the Congo", "DR Congo", "Congo", "Mozambique",
        "Sudan", "South Sudan", "Ethiopia", "Kenya", "Uganda", "Burundi",
        "Pakistan", "Afghanistan", "India", "Bangladesh", "Sri Lanka", "Nepal",
        "Syria", "Iraq", "Yemen", "Lebanon", "Israel", "Palestine", "Gaza",
        "Turkey", "Iran", "Saudi Arabia", "Jordan", "Egypt", "Libya", "Algeria",
        "Morocco", "Tunisia", "Colombia", "Venezuela", "Mexico", "Haiti",
        "Ecuador", "Peru", "Brazil", "Honduras", "Guatemala", "El Salvador",
        "Philippines", "Indonesia", "Thailand", "Malaysia", "Myanmar",
        "Russia", "Ukraine", "France", "Germany", "United Kingdom", "Spain",
        "United States", "Canada", "Australia",
    ]
    low = s.lower()
    for k in KNOWN:
        if k.lower() in low:
            return k
    return ""


def guess_deaths(s):
    """粗抽死亡数字（仅用于排序与预警，最终数值必须回原文核）。"""
    m = re.search(r"(?i)(?:at least\s+)?(\d{1,4})\s+(?:people\s+)?(?:were\s+)?(?:killed|dead|die)", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)kill(?:ed|s)?\s+(?:at least\s+)?(\d{1,4})", s)
    if m:
        return int(m.group(1))
    if re.search(r"(?i)\b(?:killed|dead)\b", s):
        return None      # 提到死亡但没给数字
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM，如 2026-08")
    ap.add_argument("--out", default=None, help="输出 jsonl")
    ap.add_argument("--min-items", type=int, default=80,
                    help="低于此条数视为抓取失败（默认 80：一个月全球恐袭/武装袭击不该少于这个量级）")
    ap.add_argument("--keep-noise", action="store_true", help="保留疑似非事件条目（调试用）")
    ap.add_argument("--fetch-cmd", default=None,
                    help="外部抓取命令，用 {url} 占位，如 'curl -sL {url}'。"
                         "国内网络常连不上维基，强烈建议指定一个本机可用的抓取工具")
    ap.add_argument("--timeout", type=int, default=25, help="单页抓取超时秒数")
    ap.add_argument("--workers", type=int, default=4, help="并发抓取天数（默认 4，别调太猛）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", a.month):
        print("--month 格式应为 YYYY-MM")
        return 1

    try:
        import requests  # noqa: F401
    except Exception:  # noqa: BLE001
        print("需要 requests：python -m pip install requests")
        return 1

    rows, failed_pages, parse_failed, empty_pages = [], [], [], []
    pages = month_pages(a.month)
    parsed = {}

    # 并发抓取（默认 4 路；抓外站别调太猛，容易被限流）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _get(pair):
        name, url = pair
        return int(name.rsplit("_", 1)[1]), fetch_page(
            name, cmd=a.fetch_cmd, timeout=a.timeout)

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = [ex.submit(_get, p) for p in pages]
        for f in as_completed(futs):
            d, t = f.result()
            parsed[d] = t

    for name, url in pages:
        day = int(name.rsplit("_", 1)[1])
        text = parsed.get(day)
        if text is None:
            failed_pages.append(day)      # 真的没抓到（网络/超时）
            continue
        seg = extract_section(text)
        if not seg:
            # 重试一次：并发/抖动可能导致内容被截断
            text2 = fetch_page(name, cmd=a.fetch_cmd, timeout=a.timeout)
            if text2:
                seg = extract_section(text2)
                text = text2
        if not seg:
            # 区分「该页确实没有当日条目」与「真的解析失败」
            if _is_empty_day_page(text or ""):
                empty_pages.append(day)   # 维基该页本身是空的，不是错误
            else:
                parse_failed.append(day)
            continue
        for item in extract_bullets(seg):
            noise = bool(NOISE.search(item))
            violent = bool(VIOLENT.search(item))
            if noise and not a.keep_noise:
                continue
            if not violent and not a.keep_noise:
                continue
            rows.append({
                "month": a.month,
                "day": day,
                "date": "%s-%02d" % (a.month, day),
                "source": "wikipedia-current-events",
                "wiki_url": url,
                "text": item,
                "country_guess": guess_country(item),
                "deaths_guess": guess_deaths(item),
                "state_actor_hint": bool(STATE_HINT.search(item)),
                "wiki_noise_hint": noise,
            })

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_country = {}
    for r in rows:
        by_country[r["country_guess"] or "(未识别)"] = \
            by_country.get(r["country_guess"] or "(未识别)", 0) + 1
    top = sorted(by_country.items(), key=lambda kv: -kv[1])[:15]

    ok = len(rows) >= a.min_items
    exit_code = 0 if ok and not failed_pages and not parse_failed else 2

    if a.json:
        print(json.dumps({
            "month": a.month, "candidates": len(rows),
            "min_items": a.min_items, "failed_days": failed_pages,
            "parse_failed_days": parse_failed, "empty_days": empty_pages,
            "top_countries": top, "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 70)
    print(" 维基「当前事件」批量提取 · %s" % a.month)
    print("=" * 70)
    print("扫描页面   : %d 天" % len(pages))
    print("抓取失败   : %d 天%s" % (len(failed_pages),
                                  ("（%s）" % failed_pages) if failed_pages else ""))
    print("解析失败   : %d 天%s" % (len(parse_failed),
                                  ("（%s）" % parse_failed) if parse_failed else ""))
    print("当日空页   : %d 天%s" % (len(empty_pages),
                                  ("（%s）" % empty_pages) if empty_pages else ""))
    print("提取候选   : %d 条（已粗筛掉灾害/事故/体育/私人刑案）" % len(rows))
    print("期望下限   : %d 条" % a.min_items)
    print()
    if top:
        print("-- 国别分布（前 15，粗猜值仅供参考）------------------------------")
        for k, v in top:
            print("  %-42s %3d" % (k, v))
        print()

    if failed_pages:
        print("  ✗ 有 %d 天没抓到（网络/超时/被墙）。先修抓取再谈判定：" % len(failed_pages))
        print("    国内网络常连不上 en.wikipedia.org，用 --fetch-cmd 指定一个能取网页的命令。")
    if parse_failed:
        print("  ✗ 有 %d 天抓到了但解析不出「Armed conflicts and attacks」段：" % len(parse_failed))
        print("    这通常是维基改了标题写法。把该天的原文存下来，检查段落标题形态。")
    if not ok:
        print("  ✗ 只提取到 %d 条，低于下限 %d。" % (len(rows), a.min_items))
        print("    「一个月全球只有这么几起」几乎总是错的 —— 是提取不完整。")
        print("    先查：页面是否抓全 / 段标题是否变了 / 是否被 NOISE 误杀（用 --keep-noise 对照）")

    if exit_code == 0:
        print("结论：提取到 %d 条候选，达到下限。" % len(rows))
        print("下一步（本脚本只负责「找全」，不负责「判对」）：")
        print("  1. 逐条抓原文核验日期与伤亡（维基只是索引，不是证据）")
        print("  2. 按口径判定链逐条判「计入 / 不计入」，写明落在哪一步")
        print("  3. 用 web_search 补维基的漏项（尤其本地媒体）")
    print("=" * 70)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
