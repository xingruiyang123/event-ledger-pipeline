#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""正文核验：抓取链接页面正文，比对死亡数与地名，判定链接是否真的对应用这起事件。

为什么需要这一步
----------------
靠「标题 + URL 里的死亡数」判断链接对不对，实测准确率只有 67%。
两类场景必然出错：

  · 村级别小事件 —— 布基纳法索某村死 2 人，搜到的报道都长得差不多
  · 同一地区连续多起袭击 —— 地名、死亡数区间、月份全一样，标题几乎相同

唯一的解法是读正文：真正的报道正文里会写清「哪天、哪里、死了几人、谁干的」。
标题受长度限制经常省略这些，正文不会。

判据
----
抓到的正文里检查三件事：

  死亡数   ±50% 容差，且数字附近 8 个词内要有死亡词（"14 civilians were killed"）
  地名     事件的省份/城市/村庄名，至少命中 1 个
  日期     事件日期（月 + 日），允许出现在正文任何位置

判定：

  通过     死亡数 + 地名 都对上
  部分     只对上一个
  不通过   都对不上
  抓不到   网络失败 / 反爬 / 页面无正文

「通过」不等于 100% 正确 —— 正文里可能顺带提到别的袭击。但它比标题可靠得多。

用法
----
  python scripts/verify_urls.py --in 带链接.jsonl --out 已核验.jsonl \\\\
      --fetch-cmd 'D:/DSH/sx.cmd fetch "{url}"'

  # 只要「通过」的
  python scripts/verify_urls.py --in x.json --out y.json --only pass
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

# 死亡词：正文里表达「死了」的各种写法
DYING = re.compile(
    r"(?i)(killed|killing|kill|dead|died|death|deaths|fatalit|martyrs?|"
    r"casualt|slain|slay|assassinat|massacr|bodies|corpses?|"
    r"lost (?:his|her|their) li(?:fe|ves)|shot dead|found dead|"
    r"死亡|遇难|丧生|罹难|身亡|被杀|击毙|打死|遗体)")

NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "dozens": 12, "scores": 20,
}

MONTHS = {1: "january", 2: "february", 3: "march", 4: "april", 5: "may",
          6: "june", 7: "july", 8: "august", 9: "september", 10: "october",
          11: "november", 12: "december"}

NOISE = {
    "village", "villages", "town", "towns", "city", "area", "areas", "near",
    "around", "the", "of", "in", "on", "at", "to", "from", "outskirts",
    "locality", "commune", "department", "district", "province", "region",
    "state", "county", "zone", "sector", "camp", "displacement", "settlement",
    "quarter", "neighbourhood", "neighborhood", "roundabout", "market",
    "road", "street", "checkpoint", "bridge", "and", "a", "an", "north",
    "south", "east", "west", "central", "upper", "lower", "greater",
    "sub", "prefecture", "municipality", "ward", "block", "section",
    # 叙述性/泛指词：ACLED 的 place 和新闻正文里都会出现，但零定位价值。
    # 实测不排掉会出现「地名命中3(located,fasher,area)」这种假命中。
    "located", "local", "locals", "government", "governments", "forces",
    "security", "according", "statement", "statements", "group", "groups",
    "officials", "official", "sources", "source", "reported", "reports",
    "people", "persons", "civilians", "residents", "militants", "fighters",
    "soldiers", "troops", "police", "army", "military", "terrorists",
    "attack", "attacks", "attacked", "killed", "killing", "dead", "deaths",
    "which", "where", "when", "while", "after", "before", "during",
    "also", "been", "were", "was", "has", "have", "had", "that", "this",
    "their", "there", "these", "those", "with", "without", "from", "into",
    "over", "under", "about", "more", "most", "some", "several", "many",
}


def clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


ENV = clean_env()


def fetch(cmd_tpl, url, timeout):
    """跑外部抓取命令，返回正文（或空串）。"""
    if not cmd_tpl:
        return ""
    cmd = cmd_tpl.replace("{url}", url)
    try:
        pr = subprocess.run(cmd, shell=True, capture_output=True,
                            timeout=timeout, env=ENV)
        raw = (pr.stdout or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    # 抓取器常在首行打「=== url -> url chars=N」，去掉它
    lines = raw.split("\n")
    if lines and lines[0].startswith("==="):
        raw = "\n".join(lines[1:])
    return raw.strip()


def deaths_in(text, deaths):
    """正文里是否有「接近 deaths」的死亡数字。返回 (ok, seen)。"""
    if not deaths:
        return None, []
    try:
        d = int(deaths)
    except Exception:  # noqa: BLE001
        return None, []
    if d <= 0:
        return None, []
    lo, hi = max(1, int(d * 0.5)), int(d * 1.5) + 1
    nums = [int(m.group(1)) for m in re.finditer(r"\b(\d{1,5})\b", text)]
    nums = [v for v in nums if not (1900 <= v <= 2100) and v < 100000]
    seen = sorted({v for v in nums if lo <= v <= hi})
    if not seen:
        return False, []
    # 数字附近要有死亡词 —— 只按词距离，不按字符（字符窗口两头都不准）
    low = text.lower()
    for m in re.finditer(r"\b(\d{1,5})\b", low):
        v = int(m.group(1))
        if not (lo <= v <= hi) or 1900 <= v <= 2100:
            continue
        tail = low[m.end():m.end() + 100]
        head = low[max(0, m.start() - 60):m.start()]
        w_after = re.findall(r"[a-z\u4e00-\u9fff]+", tail)[:8]
        w_before = re.findall(r"[a-z\u4e00-\u9fff]+", head)[-5:]
        if DYING.search(" ".join(w_after) + " " + " ".join(w_before)):
            return True, seen
    # 数词
    for w, val in NUM_WORDS.items():
        if not (lo <= val <= hi):
            continue
        for m in re.finditer(r"\b%s\b" % w, low):
            tail = low[m.end():m.end() + 100]
            head = low[max(0, m.start() - 60):m.start()]
            w_after = re.findall(r"[a-z]+", tail)[:8]
            w_before = re.findall(r"[a-z]+", head)[-5:]
            if DYING.search(" ".join(w_after) + " " + " ".join(w_before)):
                return True, seen
    return False, seen


def place_hits(text, place, country):
    """正文里命中的地名。返回命中列表。"""
    low = str(text or "").lower()
    words = [w.strip(".,;:()[]").lower()
             for w in re.findall(r"[^\s,/]+", str(place or ""))]
    words = [w for w in words if len(w) > 3 and w not in NOISE]
    hits = [w for w in words if w in low]
    return hits


def date_in(text, month, day):
    """正文里有没有「月份 + 日」。day 可空。"""
    low = str(text or "").lower()
    mn = MONTHS.get(int(month)) if month else None
    if not mn:
        return None
    if day:
        pats = [r"%s\s+%d\b" % (mn, int(day)),
                r"\b%d\s+%s" % (int(day), mn),
                r"%s\s+0?%d\b" % (mn, int(day))]
        for p in pats:
            if re.search(p, low):
                return True
        return False
    return True if mn in low else False


def main():
    ap = argparse.ArgumentParser(description="正文核验：抓页面确认链接对不对")
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--fetch-cmd", default="",
                    help='抓取命令模板，{url} 为占位。例：'
                         '\'D:/DSH/sx.cmd fetch "{url}"\'')
    ap.add_argument("--timeout", type=int, default=20,
                    help="单次抓取超时秒。默认 20 —— 实测抓不到的站点 60 秒也抓不到，"
                         "只是白等。43% 的 URL 抓不到，用 60 秒会让平均耗时变成 40 秒/条"
                         "（589 条要 6.5 小时）；降到 20 后约 11 秒/条")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--limit", type=int, default=0, help="只核前 N 条（0=全部）")
    ap.add_argument("--only", default="",
                    help="只输出这些判定（逗号分隔 pass,partial,fail,fetchfail）")
    ap.add_argument("--budget-seconds", type=int, default=0)
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：跳过 <out>.progress.json 里已核过的 URL。"
                         "实测抓正文约 40 秒/条（含反爬重试），589 条要 6 小时，"
                         "不分批跑永远跑不完")
    a = ap.parse_args()

    if not a.fetch_cmd:
        print("必须给 --fetch-cmd，否则没法抓正文。")
        print('例：--fetch-cmd \'D:/DSH/sx.cmd fetch "{url}"\'')
        return 2

    rows = []
    with io.open(a.inp, encoding="utf-8") as f:
        txt = f.read().strip()
    if txt.startswith("["):
        rows = json.loads(txt)
    else:
        rows = [json.loads(l) for l in txt.split("\n") if l.strip()]

    todo = [r for r in rows
            if str(r.get("信息来源URL") or r.get("url") or "").startswith("http")]
    if a.limit:
        todo = todo[:a.limit]
    print("待核验 %d 条（有 http 链接的）" % len(todo))
    print()

    _prog = a.out + ".progress.json"
    _done = set()
    if a.resume and os.path.isfile(_prog):
        try:
            _raw = json.load(io.open(_prog, encoding="utf-8"))
            if isinstance(_raw, dict):
                _done = set(_raw.keys())
                _saved = _raw
            else:
                _done = set(_raw)
                _saved = {}
            print("  断点续跑：已核 %d 条（其中 %d 条带结论）"
                  % (len(_done), len(_saved)))
            # 把已保存的结论直接写回记录，不必重跑
            if _saved:
                _n_fill = 0
                for _r in rows:
                    _u = str(_r.get("信息来源URL") or _r.get("url") or "")
                    if _u in _saved and not _r.get("verify_result"):
                        _r["verify_result"] = _saved[_u][0]
                        _r["verify_note"] = _saved[_u][1]
                        _n_fill += 1
                if _n_fill:
                    print("  从进度恢复结论 %d 条" % _n_fill)
        except Exception:  # noqa: BLE001
            _done = set()
            _saved = {}
    if _done:
        _b = len(todo)
        todo = [r for r in todo
                if str(r.get("信息来源URL") or r.get("url") or "") not in _done]
        print("  跳过已核 %d 条，剩 %d 条" % (_b - len(todo), len(todo)))
        if not todo:
            print("  全部核完，无需再跑。")
            return 0

    def _save_prog():
        """每 10 条落一次盘，同时保住「结论」。

        早前版本只记 URL，中断后重跑时那些 URL 被跳过、结论却丢了
        —— 实测 kill 时 110 条里只有 60 条的结论进了 out 文件。
        现在存 {url: [result, note]}，中断不掉结论。
        """
        try:
            _acc = {}
            if os.path.isfile(_prog):
                try:
                    _acc = json.load(io.open(_prog, encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    _acc = {}
            for _r in todo:
                _u = str(_r.get("信息来源URL") or _r.get("url") or "")
                if _u and _r.get("verify_result"):
                    _acc[_u] = [_r["verify_result"], _r.get("verify_note") or ""]
            with io.open(_prog, "w", encoding="utf-8") as _f:
                json.dump(_acc, _f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    _new = []
    t0 = time.time()
    stat = {"pass": 0, "partial": 0, "fail": 0, "fetchfail": 0}
    for n, r in enumerate(todo, 1):
        if a.budget_seconds and (time.time() - t0) > a.budget_seconds:
            print("  ⏱ 预算到点，停在第 %d 条" % n)
            break
        url = str(r.get("信息来源URL") or r.get("url") or "")
        body = fetch(a.fetch_cmd, url, a.timeout)
        _new.append(url)
        if len(_new) % 10 == 0:
            _save_prog()

        if len(body) < 200:
            r["verify_result"] = "抓不到"
            r["verify_note"] = "正文 %d 字符，太少" % len(body)
            stat["fetchfail"] += 1
            print("  [%d/%d] ✗抓不到 %s" % (n, len(todo), url[:60]), flush=True)
            time.sleep(a.sleep)
            continue

        dnum = r.get("死亡人数") if r.get("死亡人数") not in (None, "") else r.get("deaths")
        ok_d, seen = deaths_in(body, dnum)
        hits = place_hits(body, r.get("地点") or r.get("place") or "",
                          r.get("国家") or r.get("country") or "")
        ok_date = date_in(body, r.get("月份"), r.get("日期"))

        if ok_d and hits:
            res = "通过"
            stat["pass"] += 1
        elif ok_d or hits:
            res = "部分"
            stat["partial"] += 1
        else:
            res = "不通过"
            stat["fail"] += 1

        r["verify_result"] = res
        r["verify_note"] = "正文%d字 死亡数%s 地名命中%d(%s) 日期%s" % (
            len(body),
            {True: "对上", False: "未对上", None: "无死亡数"}[ok_d],
            len(hits), ",".join(hits[:3]),
            {True: "对上", False: "未对上", None: "无日期"}[ok_date])
        mark = {"通过": "✓", "部分": "~", "不通过": "✗"}[res]
        print("  [%d/%d] %s %s | %s" % (n, len(todo), mark, res, r["verify_note"][:70]))
        time.sleep(a.sleep)

    _save_prog()

    print()
    print("=== 核验结果 ===")
    tot = sum(stat.values())
    for k, label in (("pass", "通过"), ("partial", "部分"),
                     ("fail", "不通过"), ("fetchfail", "抓不到")):
        if tot:
            print("  %-4s %4d 条（%.0f%%）" % (label, stat[k], 100.0 * stat[k] / tot))

    # 按 --only 过滤
    out_rows = rows
    if a.only:
        want = {x.strip() for x in a.only.split(",") if x.strip()}
        MAP = {"pass": "通过", "partial": "部分", "fail": "不通过", "fetchfail": "抓不到"}
        keep = {MAP.get(w, w) for w in want}
        out_rows = [r for r in rows if r.get("verify_result") in keep]
        print()
        print("按 --only 过滤后 %d 条" % len(out_rows))

    # 分批跑时，本批只核了一部分；要把之前批次的核验结果合并回来，
    # 否则每批的输出都会覆盖掉上一批，最后只剩最后一批。
    if a.resume and os.path.isfile(a.out):
        try:
            _prev = json.load(io.open(a.out, encoding="utf-8"))
            _pm = {}
            for x in _prev:
                _u = str(x.get("信息来源URL") or x.get("url") or "")
                if _u and x.get("verify_result"):
                    _pm[_u] = (x["verify_result"], x.get("verify_note") or "")
            n_merge = 0
            for r in out_rows:
                _u = str(r.get("信息来源URL") or r.get("url") or "")
                if _u in _pm and not r.get("verify_result"):
                    r["verify_result"], r["verify_note"] = _pm[_u]
                    n_merge += 1
            if n_merge:
                print("  合并前几批结果 %d 条" % n_merge)
        except Exception:  # noqa: BLE001
            pass

    d = os.path.dirname(os.path.abspath(a.out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        json.dump(out_rows, f, ensure_ascii=False, indent=1)
    print()
    print("已写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
