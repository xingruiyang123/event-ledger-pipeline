#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据源覆盖边界检查：跑某个月之前，先确认有没有源覆盖它。

为什么必须有这一步
------------------
UCDP 正式版 GED 滞后约一年，ACLED 滞后 1-2 周。所以「跑 2026-07」这件事，
在 2026-11 的时间点上，两个权威库都可能是空的。

实测（2026-11）：
  UCDP GED 26.1        覆盖到 2025-12
  ACLED API            覆盖到 2025-11
  → 2026-07 两个源都是 0 条

不知道这件事的后果很严重：用户会以为「这个月没发生恐袭」，
而实际是「数据源还没收录」。这两件事完全不同。

这个工具做什么
--------------
1. 扫本地 UCDP 文件（zip / csv），报告年月覆盖范围
2. 探 ACLED API，二分找出最近有数据的月份
3. 对指定月份给出「有哪个源可用 / 都不可用该怎么办」

用法
----
  # 只查本地 UCDP 覆盖
  python scripts/check_coverage_window.py --ucdp ged261.zip

  # 查指定月份有没有源可用
  python scripts/check_coverage_window.py --month 2026-07 \\\\
      --ucdp ged261.zip --acled-env /path/.env

  # 探 ACLED 边界（会发 3-5 次请求，约 10 秒）
  python scripts/check_coverage_window.py --acled-env /path/.env --probe-acled
"""
import argparse
import csv
import io
import os
import re
import sys
import zipfile
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def clean_env():
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def scan_ucdp(path):
    """扫 UCDP 文件，返回 (月份计数, 年份计数, 总行数, 说明)。"""
    months, years, total = Counter(), Counter(), 0
    src = ""
    if path.lower().endswith(".zip"):
        try:
            z = zipfile.ZipFile(path)
        except Exception as e:  # noqa: BLE001
            return months, years, 0, "打不开：%s" % str(e)[:60]
        csvs = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not csvs:
            return months, years, 0, "压缩包里没有 csv"
        src = csvs[0]
        with z.open(src) as f:
            txt = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
            rd = csv.DictReader(txt)
            for row in rd:
                total += 1
                d = str(row.get("date_start") or row.get("date") or "")
                if len(d) >= 7:
                    months[d[:7]] += 1
                    years[d[:4]] += 1
    else:
        try:
            f = io.open(path, encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return months, years, 0, "打不开：%s" % str(e)[:60]
        with f:
            rd = csv.DictReader(f)
            for row in rd:
                total += 1
                d = str(row.get("date_start") or row.get("date") or "")
                if len(d) >= 7:
                    months[d[:7]] += 1
                    years[d[:4]] += 1
    return months, years, total, src


def probe_acled(env_file, months):
    """探 ACLED 哪些月份有数据。返回 {month: count}。"""
    import subprocess
    HERE = os.path.dirname(os.path.abspath(__file__))
    out = {}
    for m in months:
        cmd = [sys.executable, os.path.join(HERE, "harvest_acled.py"),
               "--month", m, "--count-only"]
        if env_file:
            cmd += ["--env-file", env_file]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               env=clean_env(), timeout=120)
            txt = (r.stdout or "") + (r.stderr or "")
            mm = re.search(r"共取到\s*(\d+)\s*条", txt)
            out[m] = int(mm.group(1)) if mm else 0
        except Exception:  # noqa: BLE001
            out[m] = -1
    return out


def main():
    ap = argparse.ArgumentParser(description="数据源覆盖边界检查")
    ap.add_argument("--ucdp", action="append", default=[],
                    help="UCDP 文件（zip 或 csv），可给多个")
    ap.add_argument("--acled-env", default=None, help="ACLED 凭据文件")
    ap.add_argument("--month", default=None, help="要检查的目标月份 YYYY-MM")
    ap.add_argument("--probe-acled", action="store_true",
                    help="探 ACLED 边界（会发若干请求）")
    ap.add_argument("--probe-months", default="",
                    help="探哪些月份，逗号分隔。不给则按目标月份往前推 4 个月")
    a = ap.parse_args()

    print("=" * 64)
    print("数据源覆盖边界检查")
    print("=" * 64)
    print()

    all_months = Counter()
    for p in a.ucdp:
        if not os.path.isfile(p):
            print("  UCDP 文件不存在：%s" % p)
            continue
        months, years, total, note = scan_ucdp(p)
        print("UCDP  %s" % os.path.basename(p))
        print("      行数 %d ｜ %s" % (total, note))
        if months:
            ks = sorted(months)
            print("      月份范围 %s ~ %s（%d 个不同月份）"
                  % (ks[0], ks[-1], len(ks)))
            print("      最后 6 个月：" + ", ".join(
                "%s:%d" % (k, months[k]) for k in ks[-6:]))
        print()
        for k, v in months.items():
            all_months[k] += v

    # ACLED 探测
    ac = {}
    if a.probe_acled and a.acled_env:
        if a.probe_months:
            ms = [x.strip() for x in a.probe_months.split(",") if x.strip()]
        elif a.month:
            y, mo = int(a.month[:4]), int(a.month[5:7])
            ms = []
            for i in range(4):
                mo2 = mo - i
                y2 = y
                while mo2 <= 0:
                    mo2 += 12
                    y2 -= 1
                ms.append("%04d-%02d" % (y2, mo2))
        else:
            ms = []
        print("ACLED 探测 %d 个月（每月一次请求）…" % len(ms))
        ac = probe_acled(a.acled_env, ms)
        for m in ms:
            v = ac.get(m, -1)
            print("      %s -> %s" % (m, "请求失败" if v < 0 else "%d 条" % v))
        print()

    # 判定目标月份
    if a.month:
        print("=" * 64)
        print("目标月份 %s" % a.month)
        print("=" * 64)
        u_ok = all_months.get(a.month, 0)
        print("  UCDP  : %d 条%s" % (u_ok, "" if u_ok else "  ← 无覆盖"))
        if ac:
            av = ac.get(a.month, 0)
            print("  ACLED : %s" % ("%d 条" % av if av > 0 else "无覆盖"))
        elif a.acled_env:
            print("  ACLED : 未探测（加 --probe-acled）")
        print()

        if u_ok or (ac.get(a.month, 0) > 0):
            print("  ✓ 该月至少有一个权威库覆盖，可以正常跑。")
            print("     python scripts/run_pipeline.py --month %s ..." % a.month)
        else:
            print("  ✗ 该月两个权威库都没有数据。")
            print()
            print("    这不是「该月没发生恐袭」，是「数据源还没收录」。")
            print("    两个权威库都滞后：UCDP 正式版约一年，ACLED 约 1-2 周。")
            print()
            print("    三条出路：")
            print("    a) 等数据源更新后再跑（最省事，但要等）")
            print("    b) 用 UCDP 的月度候选数据集（比年度版快，但需另下）")
            print("    c) 走媒体检索路径补：")
            print("         python scripts/harvest_nonlethal.py --month %s \\" % a.month)
            print("             --search-cmd '...' --top 40")
            print("       —— 但这条路只能补零散事件，做不到权威库那种覆盖度。")
            print()
            print("    重要：不要因为跑出 0 条就写「本月无恐袭」。")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
