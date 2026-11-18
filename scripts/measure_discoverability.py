#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""信源遍历有效性度量：给定一批已知事件，测检索能发现多少。

为什么需要这个
--------------
「信源表遍历有没有用」这个问题，不能用「我觉得能搜到」回答，
也不能用「搜了 113 家没搜到增量」回答 —— 后者只是说明那 113 家当时搜法不对。

唯一可靠的测法：**拿一批已知存在的事件当靶子，只看国家+月份去搜，
看能不能捞到。** 捞到多少，就是遍历的真实上限。

实测（2026-07，20 起媒体独家事件）：
  只给国家+月份          → 65%
  给精确地名             → 100%
差距说明：事件都被报道过，瓶颈是线索质量，不是媒体覆盖。

用法
----
  # 1) 准备靶子文件（json，每行一起事件）
  #    [{"no":"1","country":"Syria","date":"2026-07-02",
  #      "keywords":["al-Hijaz","Damascus"]}, ...]
  #    keywords 是「这条新闻里应该出现的地名」，用于判命中
  #
  # 2) 跑
  python scripts/measure_discoverability.py --targets targets.json \\\\
      --search-cmd 'yb.cmd search "{q}" --cnt 20' --out report.md

产出
----
可发现率、命中/未命中的清单、以及按国家/按事件类型的分布。

重要限制
--------
量的是「关键词出现在检索结果里」，不等于「能自动抽成结构化事件」。
真实流水线还要过判定链、去重、死亡数校验，落地率只会更低。
**把这个数当上限，不是当预期。**
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

QUERIES = [
    "{country} {month_name} {year} terrorist attack killed",
    "{country} militants attack soldiers {month_name} {year}",
    "{country} attack killed police {month_name} {year}",
]

MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def build_cmd(tpl, q):
    tpl = tpl.replace("{q}", q)
    return tpl


def run(tpl, q, timeout=90):
    cmd = build_cmd(tpl, q)
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


def norm(t):
    return re.sub(r"[^a-z0-9]+", "", str(t).lower())


def main():
    ap = argparse.ArgumentParser(description="信源遍历有效性度量")
    ap.add_argument("--targets", required=True,
                    help="靶子 json，每项含 no/country/date/keywords")
    ap.add_argument("--search-cmd", required=True,
                    help='检索命令模板，{q} 为查询占位')
    ap.add_argument("--out", default=None, help="markdown 报告路径")
    ap.add_argument("--queries", type=int, default=3,
                    help="每国发几条查询（默认 3）")
    ap.add_argument("--sleep", type=float, default=0.05)
    a = ap.parse_args()

    with io.open(a.targets, encoding="utf-8") as f:
        targets = json.load(f)
    if not targets:
        print("靶子为空", file=sys.stderr)
        return 2

    # 补月份信息
    for t in targets:
        d = str(t.get("date") or "")
        m = re.match(r"(\d{4})-(\d{2})", d)
        if m:
            t["_year"] = m.group(1)
            t["_month"] = MONTHS[int(m.group(2))]
        else:
            t["_year"] = ""
            t["_month"] = ""

    by_country = {}
    for t in targets:
        by_country.setdefault(str(t.get("country") or ""), []).append(t)

    print("靶子 %d 起，%d 国，每国 %d 条查询"
          % (len(targets), len(by_country), a.queries))
    print()

    found, missed = [], []
    for cty, items in sorted(by_country.items()):
        y = items[0].get("_year") or ""
        mo = items[0].get("_month") or ""
        blob = []
        for qt in QUERIES[:a.queries]:
            q = qt.format(country=cty, month_name=mo, year=y).strip()
            blob.append(run(a.search_cmd, q))
            time.sleep(a.sleep)
        big = norm("\n".join(blob))
        for it in items:
            kws = [k for k in (it.get("keywords") or []) if k]
            hit = [k for k in kws if norm(k) and norm(k) in big]
            (found if hit else missed).append((it, hit, kws))
        print("  %-18s 返回 %6d 字符" % (cty, len(big)))
        time.sleep(a.sleep)

    n = len(targets)
    print()
    print("=" * 62)
    print("可发现性结果")
    print("=" * 62)
    print("  命中   %3d / %d = %.0f%%" % (len(found), n, 100.0 * len(found) / n))
    print("  未命中 %3d / %d = %.0f%%" % (len(missed), n,
                                        100.0 * len(missed) / n))
    print()
    print("  提示：这是**上限**。量的是关键词出现，不是能抽成事件。")
    print()

    if missed:
        print("未命中的（通用查询捞不到）：")
        for it, _, kws in missed:
            print("  #%-4s %-16s 该有的词: %s"
                  % (it.get("no"), it.get("country"),
                     ", ".join(kws[:3]) if kws else "(无)"))
        print()

    if a.out:
        L = ["# 信源遍历有效性度量", ""]
        L.append("| 项 | 数值 |")
        L.append("|---|---|")
        L.append("| 靶子事件 | %d |" % n)
        L.append("| 涉及国家 | %d |" % len(by_country))
        L.append("| 每国查询数 | %d |" % a.queries)
        L.append("| **可发现** | **%d（%.0f%%）** |"
                 % (len(found), 100.0 * len(found) / n))
        L.append("| 未发现 | %d（%.0f%%） |"
                 % (len(missed), 100.0 * len(missed) / n))
        L.append("")
        L.append("## 命中的")
        L.append("")
        L.append("| 编号 | 国家 | 命中关键词 |")
        L.append("|---|---|---|")
        for it, hit, _ in found:
            L.append("| %s | %s | %s |"
                     % (it.get("no"), it.get("country"), ", ".join(hit[:3])))
        L.append("")
        if missed:
            L.append("## 未发现的")
            L.append("")
            L.append("| 编号 | 国家 | 该有的关键词 |")
            L.append("|---|---|---|")
            for it, _, kws in missed:
                L.append("| %s | %s | %s |"
                         % (it.get("no"), it.get("country"),
                            ", ".join(kws[:3]) if kws else "(无)"))
            L.append("")
        L.append("## 怎么读这个数")
        L.append("")
        L.append("可发现率是**上限**，不是预期。它量的是「靶子的关键地名有没有"
                 "出现在检索结果里」，")
        L.append("不等于「能自动抽成一条结构化事件」。真实流水线还要过判定链、"
                 "去重、死亡数校验。")
        L.append("")
        L.append("实测参考：2026-07 的 20 起媒体独家事件，只给国家+月份是 65%；"
                 "给精确地名是 100%。")
        L.append("差距说明这些事件都被报道过，瓶颈是**线索质量**，不是媒体覆盖。")
        d = os.path.dirname(os.path.abspath(a.out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write("\n".join(L))
        print("已写出 → %s" % a.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
