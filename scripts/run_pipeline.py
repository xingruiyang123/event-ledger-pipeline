#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端一键跑：从数据源到台账，中间每一步都有门禁。

为什么需要这个脚本
-----------------
2026-08 全量实测暴露的问题不是「某个脚本不好用」，而是**没人负责串起来**：
用户得自己记住 10 个脚本的调用顺序、参数、以及「哪一步失败意味着什么」。
结果就是跳过某一步、或用错数据集而不自知（实测踩过：用 UCDP 候选集查历史月份，
得出「8 月只有 27 起」的错误结论）。

本脚本把顺序、参数、门禁、失败诊断全部固化。

用法
----
  # 最常用：给一个月份 + UCDP 数据文件，一路跑到台账
  python scripts/run_pipeline.py --month 2025-08 --ucdp ged261.zip --outdir ./out

  # 只有候选集（查最新月份）
  python scripts/run_pipeline.py --month 2026-09 --ucdp-candidate GEDEvent_v26.csv --outdir ./out

  # 没有 UCDP，用维基兜底（条数会少，脚本会明确警告）
  python scripts/run_pipeline.py --month 2026-08 --wiki-only --fetch-cmd "tf.py fetch {url}" --outdir ./out

  # 只跑前几步看看数据够不够，不出台账
  python scripts/run_pipeline.py --month 2025-08 --ucdp ged261.zip --stage harvest

阶段：harvest → judge → ledger → gate → all（默认 all）

退出码：0 = 全链路通过 | 2 = 有门禁未通过 | 1 = 无法判定
"""
import argparse
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run_step(name, args, expect=0, allow_fail=False):
    """跑一步，返回 (ok, 输出)。"""
    print("\n" + "─" * 70)
    print("▶ %s" % name)
    print("─" * 70)
    r = subprocess.run([PY] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    # 只打印关键行，避免刷屏
    for line in out.split("\n"):
        s = line.strip()
        if any(k in s for k in ("命中", "计入", "不计入", "结论", "✗", "!", "→",
                                "事件数", "数据行", "表头", "覆盖率", "总", "扫描")):
            print("  " + s)
    ok = (r.returncode == expect)
    if not ok and not allow_fail:
        print("  ✗ 期望退出码 %d，实际 %d" % (expect, r.returncode))
    return ok, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--ucdp", default=None, help="正式 GED 的 csv/zip（查历史月份）")
    ap.add_argument("--ucdp-candidate", default=None, help="候选集 csv（只查最新月份）")
    ap.add_argument("--wiki-only", action="store_true", help="没有 UCDP，用维基兜底")
    ap.add_argument("--fetch-cmd", default=None, help="外部抓取命令，{url} 占位")
    ap.add_argument("--outdir", default="./out")
    ap.add_argument("--stage", default="all",
                    choices=["harvest", "judge", "ledger", "gate", "all"])
    ap.add_argument("--min-events", type=int, default=40)
    ap.add_argument("--source-kind", default="structured",
                    choices=["news", "structured", "mixed"])
    a = ap.parse_args()

    od = os.path.abspath(a.outdir)
    os.makedirs(od, exist_ok=True)
    cand = os.path.join(od, "01_候选.jsonl")
    judged = os.path.join(od, "02_判定.json")
    ledger = os.path.join(od, "03_台账.xlsx")

    results = []

    # ============ 阶段 1：采集 ============
    if a.stage in ("harvest", "all"):
        if a.ucdp or a.ucdp_candidate:
            args = [os.path.join(HERE, "harvest_ucdp.py"), "--month", a.month,
                    "--out", cand, "--min-items", str(a.min_events)]
            args += (["--ged", a.ucdp] if a.ucdp else ["--candidate", a.ucdp_candidate])
            ok, out = run_step("采集：UCDP", args, expect=0, allow_fail=True)
            results.append(("采集(UCDP)", ok, out))
            if not ok:
                # 典型误用：拿候选集查历史月份
                if "候选集" in out and "历史月份" in out:
                    print("\n  ⚠️  诊断：你可能用候选集查了历史月份。")
                    print("     候选集只保留还在编码队列的月份；历史月份必须用正式 GED。")
                elif "未收录" in out:
                    print("\n  ⚠️  诊断：该月可能确实未收录（GED 滞后约一年）。")
        else:
            args = [os.path.join(HERE, "harvest_wiki_events.py"), "--month", a.month,
                    "--out", cand, "--min-items", str(a.min_events)]
            if a.fetch_cmd:
                args += ["--fetch-cmd", a.fetch_cmd]
            ok, out = run_step("采集：维基（兜底，条数会少）", args,
                               expect=0, allow_fail=True)
            results.append(("采集(维基)", ok, out))
            print("\n  ⚠️  提醒：维基偏欧美，非洲小规模袭击几乎不进。")
            print("     用它做月度台账会系统性偏斜，交付说明里必须写清。")

        if a.stage == "harvest":
            return 0

    # ============ 阶段 2：判定 ============
    if a.stage in ("judge", "all"):
        if not os.path.isfile(cand):
            print("\n找不到候选文件 %s，先跑 harvest 阶段。" % cand)
            return 1
        # 用结构化抽取 + 战争语境分流 + 判定
        ev = os.path.join(od, "01b_结构化.jsonl")
        sp = os.path.join(od, "01c_分流.jsonl")
        run_step("结构化抽取", [os.path.join(HERE, "extract_events.py"),
                              "--in", cand, "--out", ev], allow_fail=True)
        run_step("战争语境分流", [os.path.join(HERE, "filter_war_context.py"),
                               "--in", ev, "--out", sp], allow_fail=True)
        # 判定由 build 脚本做（本脚本不含业务判定逻辑，避免把口径写死在工具里）
        print("\n  ℹ️  判定需按 reference/00-口径先立.md 逐条做。")
        print("     可用你自己的判定脚本；本脚本不内置口径（口径随委托方变）。")

    if a.stage in ("judge",):
        return 0

    # ============ 阶段 3+4：台账与门禁 ============
    if a.stage in ("ledger", "gate", "all"):
        if not os.path.isfile(judged):
            print("\n找不到判定结果 %s。" % judged)
            print("判定结果需是 json 数组，每项含 19 列字段（见 make_ledger.py 的模板）。")
            print("你可以：① 手工判定后写成该文件；② 用你自己的判定脚本产出它。")
            return 1
        run_step("生成 19 列台账", [os.path.join(HERE, "make_ledger.py"),
                                 "--template", "19列", "--rows", judged,
                                 "--out", ledger], allow_fail=True)
        run_step("门禁：表头", [os.path.join(HERE, "make_ledger.py"),
                             "--check", ledger], allow_fail=True)

        if a.stage in ("gate", "all"):
            ok, out = run_step("门禁：合理性（量级）",
                               [os.path.join(HERE, "gate_plausibility.py"), ledger,
                                "--min-events", str(a.min_events),
                                "--source-kind", a.source_kind], allow_fail=True)
            results.append(("合理性门禁", ok, out))
            if not ok:
                print("\n  ⚠️  量级门禁未通过。不要直接交付 ——")
                print("     先补数据源（换正式 GED / 接 ACLED / 逐国点名本地媒体）。")

    print("\n" + "=" * 70)
    print(" 全链路结果")
    print("=" * 70)
    for name, ok, _ in results:
        print("  %-18s %s" % (name, "✅" if ok else "❌"))
    allok = all(ok for _, ok, _ in results)
    print()
    print("产物目录：%s" % od)
    print("结论：%s" % ("全链路通过" if allok else "有环节未通过，见上面的诊断"))
    print("=" * 70)
    return 0 if allok else 2


if __name__ == "__main__":
    sys.exit(main())
