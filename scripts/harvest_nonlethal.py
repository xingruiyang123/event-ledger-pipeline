#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""未遂 / 被挫败 / 零伤亡事件的定向采集。

为什么必须单独做这一路
----------------------
口径写明「预谋被破获、未遂、零伤亡的案子照常计入」。但：

  · UCDP GED 的收录门槛是「造成死亡」—— 零死亡一条不收
  · ACLED 收政治暴力但不收「未发生的事件」—— 图谋被破获不是事件，是执法通报

实测 2025-08：台账 2565 行里「未遂」只有 **1** 条，而这一整类是口径明确要求计入的。

所以只能靠检索新闻补。但通用检索有两个坑：

  1. **默认返回最新新闻**。找 2025-08 的事，返回的全是今天的。
     检索式里必须带「August 2025」这样的月份英文名 + 年份。
  2. **不带国家的检索式召回很散**。`"2025-08 foiled attack"` 这种全球查，
     十条里八条是别的话题。必须按国家 × 关键词组合，一国一查。

检索式怎么造
------------
每国 2–3 条，按语言分开：

  英文   "<国名>" foiled thwarted plot attack "August 2025"
  中文   "<国名>" 未遂 挫败 图谋 恐袭 "August 2025"
  本地语 法语 / 西班牙语 / 阿拉伯语 的对应说法（只在相关国家用）

结果要过三道过滤才进候选池：

  ① 日期：必须落在目标月份（±3 天容忍跨月报道）
  ② 未遂特征：标题/摘要里要有 foiled / thwarted / defused / arrested 这类词
  ③ 排除：不能是「已发生并造成死亡」的事件（那归正常通道）

用法
----
  # 自动从权威库里取事件最多的 30 个国家来查
  python scripts/harvest_nonlethal.py --month 2025-08 \\
      --from acled_2508.jsonl --out 候选_未遂.jsonl \\
      --search-cmd 'D:/DSH/sx.cmd search "{q}" --task auto --json'

  # 只查指定国家
  python scripts/harvest_nonlethal.py --month 2025-08 --countries "Nigeria,Somalia" ...

  # 只看要发多少条检索，不实际跑
  python scripts/harvest_nonlethal.py --month 2025-08 --from a.jsonl --dry-run
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

MONTH_EN = {1: "January", 2: "February", 3: "March", 4: "April", 5: "May",
            6: "June", 7: "July", 8: "August", 9: "September", 10: "October",
            11: "November", 12: "December"}

# 未遂/被挫败的特征词。命中任一才算这一类。
FOILED_RE = re.compile(
    r"(?i)(foil|thwart|bust|prevent|defus|disarm|intercept|"
    r"plot (?:was )?foiled|attempted (?:attack|bombing|assassination)|"
    r"failed (?:attack|bombing)|arrested .{0,30}(?:bomb|explosive|terror)|"
    r"未遂|挫败|破获|图谋|预谋|缴获.{0,10}爆炸|拆除.{0,10}炸弹|抓获.{0,10}嫌犯|"
    r"déjou|tentative d|frustrad|intento de|atentado frustrado|"
    r"أحبط|إحباط|محاولة فاشلة)")

# 已发生且造成死亡的，不归这一路（避免把正常袭击当未遂收进来）
HAPPENED_RE = re.compile(
    r"(?i)(\d{1,4})\s*(?:people |civilians |soldiers )?"
    r"(?:killed|dead|died|martyrs)|"
    r"killed (\d{1,4})|死亡\s*\d+|致\s*\d+\s*人?死亡")

# 本地语言关键词：只在对应国家用，避免全球撒网
LANG_KEYWORDS = {
    "zh": "未遂 挫败 图谋 恐袭",
    "fr": "attentat déjoué tentative",
    "es": "atentado frustrado intento",
    "ar": "محاولة فاشلة إحباط",
}

# 国家 → 该用哪种本地语言
COUNTRY_LANG = {
    "Mali": "fr", "Burkina Faso": "fr", "Niger": "fr", "Chad": "fr",
    "Cameroon": "fr", "Senegal": "fr", "Ivory Coast": "fr", "Guinea": "fr",
    "Democratic Republic of the Congo": "fr", "Congo": "fr",
    "Central African Republic": "fr", "Benin": "fr", "Togo": "fr",
    "Burundi": "fr", "Rwanda": "fr", "Madagascar": "fr",
    "Colombia": "es", "Mexico": "es", "Venezuela": "es", "Peru": "es",
    "Ecuador": "es", "Bolivia": "es", "Paraguay": "es", "Chile": "es",
    "Argentina": "es", "Honduras": "es", "Guatemala": "es",
    "El Salvador": "es", "Nicaragua": "es", "Costa Rica": "es",
    "Panama": "es", "Dominican Republic": "es", "Haiti": "fr", "Cuba": "es",
    "Iraq": "ar", "Syria": "ar", "Yemen": "ar", "Saudi Arabia": "ar",
    "Egypt": "ar", "Libya": "ar", "Algeria": "ar", "Morocco": "ar",
    "Tunisia": "ar", "Jordan": "ar", "Lebanon": "ar", "Kuwait": "ar",
    "China": "zh", "Taiwan": "zh",
}


def clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def load_jsonl(path):
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


def pick_countries(rows, top):
    """挑出该查未遂的国家。

    不能只看「事件最多」。实测：ACLED 2025-08 事件最多的是乌克兰、俄罗斯、
    巴勒斯坦 —— 那里主要是国与国的战争，不是恐怖袭击，「未遂恐袭」极少。
    按这个名单查，30 条检索只收到 4 条，其中 2 条还能用。

    优先看 ACLED 的 tags 里带 Terrorist 的记录：那些国家才是恐袭高发地。
    没有 tags 字段时退回「事件最多」。
    """
    terror = Counter()
    allc = Counter()
    for r in rows:
        c = str(r.get("country") or "").strip()
        if not c:
            continue
        allc[c] += 1
        tags = str((r.get("extra") or {}).get("acled_tags") or "")
        if "terror" in tags.lower():
            terror[c] += 1
    if terror:
        # 有恐袭标签的国家优先，按标签数排；不足 top 的再用总量补
        out = [k for k, _ in terror.most_common(top)]
        if len(out) < top:
            for k, _ in allc.most_common():
                if k not in out:
                    out.append(k)
                if len(out) >= top:
                    break
        return out
    return [k for k, _ in allc.most_common(top) if k]


def build_queries(country, month):
    """给一个国家生成 2–3 条检索式。"""
    y, m = month.split("-")
    men = MONTH_EN[int(m)]
    dash = "%s %s" % (men, y)          # "August 2025"
    qs = [
        '"%s" foiled thwarted attack plot %s' % (country, dash),
        '"%s" bomb defused arrested suspects %s' % (country, dash),
    ]
    lang = COUNTRY_LANG.get(country)
    if lang:
        qs.append('"%s" %s %s' % (country, LANG_KEYWORDS[lang], dash))
    else:
        # 非英语母语但没配本地语言的，补一条「attempted」变体
        qs.append('"%s" attempted attack failed %s' % (country, dash))
    return qs


def run_search(cmd_tpl, q, timeout):
    cmd = cmd_tpl.replace("{q}", q)
    try:
        pr = subprocess.run(cmd, shell=True, capture_output=True,
                            timeout=timeout, env=clean_env())
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:60]
    raw = (pr.stdout or b"").decode("utf-8", "replace")
    if not raw.strip():
        return None, "空输出"
    try:
        return json.loads(raw), ""
    except Exception as e:  # noqa: BLE001
        return None, "非 JSON：%s" % str(e)[:40]


def date_in_month(d, month, tol=3):
    """日期是否落在目标月份（±tol 天，容忍跨月报道）。"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    if not m:
        return False
    y, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ty, tm = int(month[:4]), int(month[5:7])
    if y == ty and mo == tm:
        return True
    # 上月末 / 下月初的几天也算（报道常晚于事发）
    import datetime
    try:
        want = datetime.date(ty, tm, 15)
        got = datetime.date(y, mo, dd)
        return abs((got - want).days) <= 20
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser(description="未遂/被挫败/零伤亡事件定向采集")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out", default="候选_未遂.jsonl")
    ap.add_argument("--from", dest="src", default=None,
                    help="权威库候选 jsonl，用来决定查哪些国家（不给自己指定 --countries）")
    ap.add_argument("--countries", default=None, help="逗号分隔的国家名")
    ap.add_argument("--top", type=int, default=25,
                    help="从数据里取事件最多的前 N 个国家（默认 25）")
    ap.add_argument("--search-cmd", default=None, help="检索命令模板，{q} 为占位")
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--sleep", type=float, default=0.5)
    ap.add_argument("--max-queries", type=int, default=200, help="最多发多少条检索")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if len(a.month) != 7:
        print("--month 要写成 YYYY-MM", file=sys.stderr)
        return 1

    # 决定查哪些国家
    if a.countries:
        countries = [x.strip() for x in a.countries.split(",") if x.strip()]
    elif a.src:
        rows = load_jsonl(a.src)
        if not rows:
            print("--from 读不到数据：%s" % a.src, file=sys.stderr)
            return 1
        countries = pick_countries(rows, a.top)
        print("从 %s 取事件最多的 %d 个国家：" % (os.path.basename(a.src), len(countries)))
        print("  " + "、".join(countries))
    else:
        print("要么给 --from（自动选国家），要么给 --countries", file=sys.stderr)
        return 1

    queries = []
    for c0 in countries:
        for q in build_queries(c0, a.month):
            queries.append((c0, q))
    if len(queries) > a.max_queries:
        queries = queries[:a.max_queries]

    print()
    print("检索式 %d 条（%d 国 × 每国 3 条）" % (len(queries), len(countries)))
    if a.dry_run:
        for c0, q in queries[:10]:
            print("  [%s] %s" % (c0, q))
        if len(queries) > 10:
            print("  … 另有 %d 条" % (len(queries) - 10))
        return 0
    if not a.search_cmd:
        print("没给 --search-cmd，只导出清单。", file=sys.stderr)
        return 1

    print()
    cands, seen = [], set()
    n_ok = n_res = 0
    t0 = time.time()
    for i, (c0, q) in enumerate(queries, 1):
        d, err = run_search(a.search_cmd, q, a.timeout)
        if not d:
            if err:
                print("  [%d/%d] %s 检索失败：%s" % (i, len(queries), c0, err), flush=True)
            continue
        n_ok += 1
        for r in (d.get("results") or []):
            u = str(r.get("url") or "")
            t = str(r.get("title") or "")
            sn = str(r.get("snippet") or "")
            dt = str(r.get("date") or "")[:10]
            if not u or u in seen:
                continue
            seen.add(u)
            n_res += 1
            blob = t + " " + sn
            # ① 未遂特征
            if not FOILED_RE.search(blob):
                continue
            # ② 日期落在目标月
            if dt and not date_in_month(dt, a.month):
                continue
            if not dt:
                # 无日期的不直接拒（很多站不给日期），但要月份词对得上
                men = MONTH_EN[int(a.month[5:7])]
                if men.lower() not in blob.lower() and a.month[:4] not in blob:
                    continue
            # ③ 已造成死亡的不归这一路
            hm = HAPPENED_RE.search(blob)
            if hm:
                try:
                    v = int(re.sub(r"\D", "", hm.group(1) or "0") or 0)
                    if v >= 1:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            cands.append({
                "source": "nonlethal",
                "date": dt or "",
                "country": c0,
                "place": "",
                "summary": t[:300],
                "deaths": 0,
                "injured": None,
                "actor_a": "",
                "actor_b": "",
                "act_type": "",
                "url": u,
                "raw_id": "",
                "extra": {"query": q, "snippet": sn[:300], "engine_date": dt},
            })
        if i % 10 == 0:
            print("  [%d/%d] 已收 %d 条候选 ｜ %.0fs"
                  % (i, len(queries), len(cands), time.time() - t0), flush=True)
        time.sleep(a.sleep)

    print()
    print("发出 %d 条检索，成功 %d，返回 %d 条结果" % (len(queries), n_ok, n_res))
    print("命中的未遂候选：%d 条" % len(cands))

    if not cands:
        print()
        print("⚠️  一条都没收到。常见原因：")
        print("   · 检索后端不支持把「August 2025」当检索词（换 --search-cmd 试别家）")
        print("   · 目标月份太远，新闻站已归档/下线")
        print("   · 该国当月确实没有公开报道的未遂图谋")
        print("   这三种情况不能混为一谈，交付时要分开写。")

    d = os.path.dirname(os.path.abspath(a.out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        for r in cands:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print()
    print("已写出 → %s" % a.out)
    return 0 if cands else 2


if __name__ == "__main__":
    sys.exit(main())
