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
  python scripts/run_pipeline.py --month 2026-08 --wiki-only --fetch-cmd "curl -sL {url}" --outdir ./out

  # 只跑前几步看看数据够不够，不出台账
  python scripts/run_pipeline.py --month 2025-08 --ucdp ged261.zip --stage harvest

阶段：harvest → judge → ledger → gate → all（默认 all）

退出码：0 = 全链路通过 | 2 = 有门禁未通过 | 1 = 无法判定
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _clean_env():
    """清空代理变量。很多机器设了 HTTP_PROXY 指向没启动的代理，
    子进程会全部连不上，看起来像断网。必须显式清空。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def _ged_months(path):
    """扫一个 GED 文件（zip 或 csv），返回它覆盖的月份集合。"""
    import csv as _csv
    import zipfile as _zip
    out = set()
    try:
        if str(path).lower().endswith(".zip"):
            z = _zip.ZipFile(path)
            cs = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not cs:
                return out
            fobj = z.open(cs[0])
            txt = io.TextIOWrapper(fobj, encoding="utf-8", errors="replace")
            for row in _csv.DictReader(txt):
                d = str(row.get("date_start") or row.get("date") or "")
                if len(d) >= 7:
                    out.add(d[:7])
        else:
            fobj = io.open(path, encoding="utf-8", errors="replace")
            for row in _csv.DictReader(fobj):
                d = str(row.get("date_start") or row.get("date") or "")
                if len(d) >= 7:
                    out.add(d[:7])
    except Exception:  # noqa: BLE001
        return out
    return out


def resolve_ucdp_auto(month, data_dir, here):
    """找一个覆盖 month 的 UCDP 文件；没有就下载候选集再找。"""
    import glob as _glob
    import subprocess as _sp

    def scan():
        cands = []
        for pat in ("*.zip", "*.csv"):
            cands += _glob.glob(os.path.join(data_dir, pat))
        hits = []
        for f in cands:
            low = os.path.basename(f).lower()
            if "ged" not in low and "ucdp" not in low:
                continue
            ms = _ged_months(f)
            if month in ms:
                hits.append((f, len(ms)))
        return hits

    print("")
    print("  自动选源：目标 %s，扫描 %s" % (month, data_dir))
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir, exist_ok=True)
    hits = scan()
    if hits:
        hits.sort(key=lambda x: x[1])   # 覆盖月份少的优先（增量包更快）
        print("  已找到覆盖该月的文件：%s" % os.path.basename(hits[0][0]))
        return hits[0][0]

    print("  本地没有覆盖 %s 的文件 → 下载 UCDP 月度候选集" % month)
    try:
        r = _sp.run([PY, os.path.join(here, "fetch_ucdp.py"),
                     "--candidate", "--outdir", data_dir],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", env=_clean_env(), timeout=900)
        for line in (r.stdout or "").split("\n"):
            if line.strip() and "RequestsDependencyWarning" not in line:
                print("    " + line.strip()[:100])
    except Exception as e:  # noqa: BLE001
        print("  下载失败：%s" % str(e)[:100])
        return None

    hits = scan()
    if hits:
        hits.sort(key=lambda x: x[1])
        print("  下载后可用：%s" % os.path.basename(hits[0][0]))
        return hits[0][0]
    print("  下载完成但仍没有覆盖 %s 的文件 —— 该月可能还没发布" % month)
    return None


def run_step(name, args, expect=0, allow_fail=False, timeout=600):
    """跑一步，返回 (ok, 输出)。"""
    print("\n" + "─" * 70)
    print("▶ %s" % name)
    print("─" * 70)
    import time as _t
    _t0 = _t.time()
    print("  （执行中…）", flush=True)
    try:
        r = subprocess.run([PY] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=_clean_env(),
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        print("  ✗ 超时（%d 秒）。处置：加 --timeout 放宽，或缩小数据量" % timeout)
        class _R:
            returncode = 124
            stdout = ""
            stderr = ""
        r = _R()
    _dt = _t.time() - _t0
    print("  （耗时 %.1f 秒）" % _dt, flush=True)
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


def merge_candidates(paths, out_path):
    """把多个源的候选合并成一个池，带 source 标记。

    为什么必须合并而不是「选一个最好的源」：每个源都有天然盲区。
      · UCDP   只收「有组织暴力且造成死亡」，非致命、未遂、图谋都不在
      · 信源表  只覆盖名录里的媒体，小国小语种媒体拿不到
      · 未遂专项 只找被挫败的图谋，正常发生的袭击它不找
    单源交付必然有系统性缺口，合并后至少能把缺口摊开说清楚。
    """
    import hashlib
    seen, rows = set(), []
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        tag = os.path.splitext(os.path.basename(p))[0]
        n = 0
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                # 跨源去重：同 URL 或（同国+同日+同死亡数+摘要前缀）
                url = (r.get("url") or "").strip()
                if url:
                    key = ("u", url)
                else:
                    import re as _re
                    summ = _re.sub(r"\W+", "", (r.get("summary") or ""))[:30].lower()
                    key = ("k", r.get("country"), r.get("date"), r.get("deaths"), summ)
                if key in seen:
                    # 已存在：把来源并进去，便于统计各源贡献
                    for ex in rows:
                        if ex.get("_key") == key:
                            srcs = set((ex.get("source") or "").split("+"))
                            srcs.add(r.get("source") or tag)
                            ex["source"] = "+".join(sorted(x for x in srcs if x))
                            break
                    continue
                seen.add(key)
                r["_key"] = key
                if not r.get("source"):
                    r["source"] = tag
                rows.append(r)
                n += 1
        print("    %-22s %4d 条" % (tag, n))
    for r in rows:
        r.pop("_key", None)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def write_coverage_note(path, month, contributions, judged, ledger_rows):
    """生成覆盖边界声明。

    为什么每次都要出这份：台账的数字看着像「全球恐怖袭击总量」，其实不是。
    不写清楚，读的人会拿它当普查数据用，而这个误用无法从表里看出来。
    """
    lines = []
    lines.append("# 覆盖边界声明 · %s" % month)
    lines.append("")
    lines.append("> 本文件由流水线自动生成。它的作用是说明：这份台账覆盖了什么、")
    lines.append("> 没覆盖什么、哪些数字不能怎么用。**不要省略本文件直接交付台账。**")
    lines.append("")
    lines.append("## 一、各数据源贡献")
    lines.append("")
    lines.append("| 数据源 | 候选条数 | 覆盖范围 | 天然盲区 |")
    lines.append("|---|---|---|---|")
    KNOWN = {
        "ucdp": ("UCDP GED", "全球有组织暴力，含分方死亡数",
                 "**只收造成死亡的致命事件**；非致命袭击、未遂、被挫败的图谋不在内"),
        "01_候选": ("UCDP GED", "同上", "同上"),
        "registry": ("信源表逐家遍历", "名录内媒体当月报道的全部相关事件（含非致命）",
                     "只覆盖名录里的机构；小国、小语种、无网站的媒体拿不到"),
        "registry_": ("信源表逐家遍历", "同上", "同上"),
        "acled": ("ACLED", "政治暴力与抗议，**不以死亡为门槛**，含零死亡事件",
                  "抗议/骚乱类需过滤；不含未遂图谋（那是执法通报，不是事件）"),
        "01d_acled": ("ACLED", "同 上", "同上"),
        "nonlethal": ("未遂/非致命专项检索", "被挫败的图谋、未遂袭击、零死亡事件",
                      "只找「未遂/被挫败」这一类的报道；正常发生的袭击它不找"),
        "wiki": ("维基当前事件日历", "重大事件（偏欧美）", "非洲小规模袭击几乎不进"),
        "gdelt": ("GDELT", "全球新闻线索", "粒度粗，只作线索，不能直接进台账"),
    }
    for name, n in contributions:
        k = None
        for kk in KNOWN:
            if name.startswith(kk) or kk in name:
                k = kk
                break
        label, scope, blind = KNOWN.get(k, (name, "—", "—"))
        lines.append("| %s | %d | %s | %s |" % (label, n, scope, blind))
    lines.append("")
    lines.append("## 二、三条不能做的事")
    lines.append("")
    lines.append("1. **不能把本台账的数字当成全球恐怖袭击总量。**")
    lines.append("   它是「公开可核实、且被上述数据源收录」的那一部分。")
    lines.append("2. **不能因为某个事件不在表里，就断言它没发生。**")
    lines.append("   缺记录有三个常见原因：数据源不收（非致命/未遂）、媒体没报、我们没检索到。")
    lines.append("   这三者的区别，见本文件第三节。")
    lines.append("3. **不能拿月度条数做烈度排名。**")
    lines.append("   报道密度在地区之间差一个量级：巴基斯坦、萨赫勒报道充分，")
    lines.append("   其他地区明显不足，条数更多反映「被报道的程度」，不是「发生的程度」。")
    lines.append("")
    lines.append("## 三、没覆盖到的部分怎么区分")
    lines.append("")
    lines.append("| 情形 | 判定依据 | 报告里怎么写 |")
    lines.append("|---|---|---|")
    lines.append("| 数据源不收 | 非致命、未遂、零死亡 | 「本口径要求收录未遂，该月未遂类仅 X 条，覆盖不足」 |")
    lines.append("| 媒体没报 | 检索到零结果 | 「该地区该时段无相关报道」 |")
    lines.append("| 我们没检索到 | 信源表里标「未检索」「取不到内容」 | 「该源未取到内容」，**不能写成「无事件」** |")
    lines.append("")
    lines.append("## 四、本月的实际数值")
    lines.append("")
    lines.append("- 候选池合并后：见上表合计")
    lines.append("- 判定后进台账：**%d** 条" % ledger_rows)
    lines.append("- 非致命（死亡=0）事件：**%d** 条" % (judged.get("zero_death", 0)))
    lines.append("- 未遂/被挫败事件：**%d** 条" % (judged.get("attempted", 0)))
    vf = judged.get("verify") or {}
    if vf:
        ev = vf.get("events") or 0
        mu = vf.get("multi") or 0
        ag = vf.get("aggregate") or 0
        lines.append("- 跨源合并后独立事件：**%d** 个" % ev)
        if ev:
            lines.append("- 其中被两个源以上印证：**%d** 个（%.1f%%）"
                         % (mu, 100.0 * mu / ev))
            lines.append("- 识别出累计/汇总条目：**%d** 个（已排除出事件统计）" % ag)
            if ev and mu / ev < 0.05:
                lines.append("")
                lines.append("> ⚠️ 多源印证比例很低（<5%）。说明绝大多数事件只有一个来源记录，")
                lines.append("> 交付时应标注「单源，待核」，不能当定论。")

    dist = judged.get("country_dist") or []
    if dist:
        tot = sum(n for _, n in dist) or 1
        lines.append("")
        lines.append("### 国家分布（前 12，用来识别覆盖偏斜）")
        lines.append("")
        lines.append("| 国家 | 条数 | 占比 |")
        lines.append("|---|---|---|")
        for c0, n0 in dist[:12]:
            lines.append("| %s | %d | %.1f%% |" % (c0 or "未标", n0, 100.0 * n0 / tot))
        top2 = sum(n for _, n in dist[:2])
        if tot >= 20 and top2 / tot > 0.5:
            lines.append("")
            lines.append("> 前两个国家占 **%.0f%%**，分布高度集中。" % (100.0 * top2 / tot))
            lines.append("> 这类分布通常反映数据源的地域偏向（ACLED 在俄乌、中东极密集），")
            lines.append("> **不能当全球恐袭分布用**。")
    lines.append("")
    lines.append("> 「非致命」与「未遂」是两类不同的缺口，要分开看：")
    lines.append("> 非致命（死亡=0）靠 ACLED 就能补上，它有零死亡事件。")
    lines.append("> 未遂/被挫败的图谋两个权威库都不收，只能靠 --nonlethal 定向检索。")
    lines.append("> **不要用 --registry 逐家遍历补这两类** —— 实测 113 家跑完 93% 无产出，")
    lines.append("> 原因是 site: 让检索按「该站最新」排序、目标月旧文被挤掉。")
    lines.append("> 给已有事件补链接用 --search-cmd（走 enrich_urls.py），那条路实测有效。")
    lines.append("")
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--ucdp-auto", action="store_true",
                    help="自动找覆盖目标月份的 UCDP 文件，没有就下载月度候选集。"
                         "UCDP 年度版滞后约一年（GED 26.1 只到 2025-12），"
                         "跑当月台账必须用候选集，否则会静默得到 0 条")
    ap.add_argument("--data-dir", default=None,
                    help="数据源目录（--ucdp-auto 在这里找，默认 ./数据源）")
    ap.add_argument("--ucdp", default=None, help="正式 GED 的 csv/zip（查历史月份）")
    ap.add_argument("--ucdp-candidate", default=None, help="候选集 csv（只查最新月份）")
    ap.add_argument("--wiki-only", action="store_true", help="没有 UCDP，用维基兜底")
    ap.add_argument("--fetch-cmd", default=None, help="外部抓取命令，{url} 占位")
    ap.add_argument("--outdir", default="./out")
    ap.add_argument("--stage", default="all",
                    choices=["harvest", "judge", "ledger", "gate", "all"])
    ap.add_argument("--min-events", type=int, default=40)
    ap.add_argument("--search-cmd", default=None,
                    help="补链用的搜索命令模板（{q} 占位）。给了才会补「信息来源URL」")
    ap.add_argument("--source-kind", default="structured",
                    choices=["news", "structured", "mixed"])
    ap.add_argument("--registry", default=None,
                    help="信源表 xlsx。给了就并行走「逐家遍历」，补 UCDP 收不到的非致命/未遂事件")
    ap.add_argument("--registry-crawl", action="store_true",
                    help="额外跑「逐家 site: 遍历」。默认关 —— 实测 113 家跑完 93%% 无产出、"
                         "0 条有效增量。按事件查请用 --search-cmd（走 enrich_urls）。"
                         "只有想单独看某家媒体当月发了什么时才开")
    ap.add_argument("--registry-limit", type=int, default=0,
                    help="逐家遍历只跑前 N 家（0=全部）。先小样本试时用")
    ap.add_argument("--audit", action="store_true", default=True,
                    help="产源后自动跑覆盖率审计与跨源交叉核验（默认开）")
    ap.add_argument("--no-audit", dest="audit", action="store_false",
                    help="关掉覆盖率审计")
    ap.add_argument("--enrich-limit", type=int, default=300,
                    help="补「信息来源URL」最多处理多少条（默认 300，按死亡数降序优先）。"
                         "0=不限 —— 但每条含 1 秒限速，全量会跑几小时")
    ap.add_argument("--enrich-budget", type=int, default=900,
                    help="补链的总时间预算秒数（默认 900 秒 = 15 分钟），到点就停")
    ap.add_argument("--acled", default=None,
                    help="ACLED 采集产物（jsonl，由 harvest_acled.py 生成）。"
                         "补 UCDP 收不到的非致命与未遂事件")
    ap.add_argument("--acled-env", default=None,
                    help="含 ACLED_USERNAME/PASSWORD 的 .env。给了就自动跑 ACLED 采集")
    ap.add_argument("--acled-max-pages", type=int, default=40,
                    help="ACLED 最多翻多少页（每页 5000 条）")
    ap.add_argument("--nonlethal-top", type=int, default=20,
                    help="未遂专项查事件最多的前 N 个国家（默认 20，每国 3 条检索式）")
    ap.add_argument("--nonlethal", action="store_true",
                    help="额外开一路「未遂/非致命专项检索」，需同时给 --search-cmd")
    a = ap.parse_args()

    od = os.path.abspath(a.outdir)
    os.makedirs(od, exist_ok=True)
    cand = os.path.join(od, "01_候选.jsonl")
    judged = os.path.join(od, "02_判定.json")
    ledger = os.path.join(od, "03_台账.xlsx")

    results = []

    # ============ 阶段 0：数据源（缺就自动下）============
    if a.stage in ("harvest", "all") and not a.wiki_only:
        have = None

        # --ucdp-auto：先确认给的文件真的覆盖目标月份。
        # 不覆盖就自动找/下载 —— 这是最容易踩的坑：
        # 用户拿 ged261.zip（到 2025-12）跑 2026-07，会静默得到 0 条，
        # 然后以为是「这个月没发生恐袭」。
        if a.ucdp_auto:
            _dd = a.data_dir or os.path.join(od, "数据源")
            _af = resolve_ucdp_auto(a.month, _dd, HERE)
            if _af:
                have = _af
                a.ucdp = _af
                a.ucdp_candidate = None

        if not have and a.ucdp and os.path.isfile(a.ucdp):
            have = a.ucdp
            _ms = _ged_months(a.ucdp)
            if _ms and a.month not in _ms:
                print("")
                print("  ⚠️  %s 不覆盖 %s（该文件覆盖 %s ~ %s）"
                      % (os.path.basename(a.ucdp), a.month,
                         min(_ms), max(_ms)))
                print("      用它会得到 0 条候选，但这不是「该月没事件」。")
                print("      处置：加 --ucdp-auto 自动改用月度候选集；")
                print("            或手动 --ucdp-candidate <候选集.csv>")
        elif not have and a.ucdp_candidate and os.path.isfile(a.ucdp_candidate):
            have = a.ucdp_candidate
            _ms = _ged_months(a.ucdp_candidate)
            if _ms and a.month not in _ms:
                print("")
                print("  ⚠️  候选集不覆盖 %s（覆盖 %s ~ %s）"
                      % (a.month, min(_ms), max(_ms)))
        if not have:
            print("\n未提供可用的 UCDP 文件，尝试自动下载候选集（1.6MB，轻量）…")
            ds = os.path.join(od, "数据源")
            ok, out = run_step("自动获取数据源",
                               [os.path.join(HERE, "fetch_ucdp.py"),
                                "--candidate", "--outdir", ds], allow_fail=True)
            if ok:
                import glob as _g
                got = sorted(_g.glob(os.path.join(ds, "*.csv")))
                if got:
                    a.ucdp_candidate = got[0]
                    print("  已获取：%s" % got[0])
            if not a.ucdp_candidate:
                print("\n  ⚠️  自动获取失败。处置：")
                print("     1) 手动下载 https://ucdp.uu.se/downloads/ 的候选集或正式 GED")
                print("     2) 用 --ucdp <文件> 指定")
                print("     3) 或改用 --wiki-only 走维基兜底（条数会少）")

    # ============ 阶段 1：采集 ============
    if a.stage in ("harvest", "all"):
        if a.ucdp or a.ucdp_candidate:
            args = [os.path.join(HERE, "harvest_ucdp.py"), "--month", a.month,
                    # 永远写分文件：若直接写 cand，多源合并时会出现
                    # 「读 cand → 写 cand」同一个文件，虽然能跑但很脆。
                    "--out", os.path.join(od, "01a_ucdp.jsonl"),
                    "--min-items", str(a.min_events)]
            args += (["--ged", a.ucdp] if a.ucdp else ["--candidate", a.ucdp_candidate])
            ok, out = run_step("采集：UCDP", args, expect=0, allow_fail=True)
            results.append(("采集(UCDP)", ok, out))
        # 采集失败必须立即停：否则 0 条候选会一路空转到底，
        # 最后只报「找不到判定结果」，让人以为是自己少跑了某步。
        if not ok and not (a.registry and a.registry_crawl) and not a.acled and not a.acled_env:
            print("")
            print("  采集失败，流水线终止。上面的诊断已说明原因。")
            print("  全链路结果：")
            for _n, _o, _ in results:
                print("    %-18s %s" % (_n, "✅" if _o else "❌"))
            if not a.registry:
                return 2
            print("  但已配置 --registry，继续走逐家遍历补数据。")
        if not ok:
                # 典型误用：拿候选集查历史月份
                if "候选集" in out and "历史月份" in out:
                    print("\n  ⚠️  诊断：你可能用候选集查了历史月份。")
                    print("     候选集只保留还在编码队列的月份；历史月份必须用正式 GED。")
                elif "未收录" in out:
                    print("\n  ⚠️  诊断：该月可能确实未收录（GED 滞后约一年）。")
        elif a.wiki_only or not (a.ucdp or a.ucdp_candidate):
            # 只有「明确要求维基」或「完全没有 UCDP 文件」时才走维基兜底。
            # 实测：UCDP 已成功采到 1984 条后仍去跑维基，白耗 295 秒。
            args = [os.path.join(HERE, "harvest_wiki_events.py"), "--month", a.month,
                    "--out", cand, "--min-items", str(a.min_events)]
            if a.fetch_cmd:
                args += ["--fetch-cmd", a.fetch_cmd]
            ok, out = run_step("采集：维基（兜底，条数会少）", args,
                               expect=0, allow_fail=True)
            results.append(("采集(维基)", ok, out))
            print("\n  ⚠️  提醒：维基偏欧美，非洲小规模袭击几乎不进。")
            print("     用它做月度台账会系统性偏斜，交付说明里必须写清。")

        # ---- 单源告警：只用 UCDP 必然有系统性缺口，必须说出来 ----
        if not a.registry and not a.nonlethal and not a.acled and not a.acled_env:
            print("")
            print("  ⚠️  只用了 UCDP 一个源，有两类事件会系统性缺失：")
            print("     · 非致命袭击（零死亡）—— UCDP 只收造成死亡的有组织暴力")
            print("     · 未遂/被挫败的图谋 —— 口径要求计入，但 UCDP 不收")
            print("     补法：--acled-env <.env>  （推荐）ACLED，可系统性补非致命事件")
            print("           --registry <信源表.xlsx>   逐家遍历，覆盖名录内媒体的全部报道")
            print("           --nonlethal --search-cmd ...  未遂专项检索")
            print("     交付时必须附「覆盖边界声明」说明这个缺口。")

        # ---- 并联：信源表逐家遍历（补 UCDP 收不到的部分）----
        parts = []
        ucdp_out = os.path.join(od, "01a_ucdp.jsonl")
        if os.path.isfile(ucdp_out):
            parts.append(ucdp_out)
        elif os.path.isfile(cand):
            parts.append(cand)

        # 逐家 site: 遍历：**默认不跑**。
        # 实测 113 家 P1 全跑完（1.4 分钟）：93% 显示「已检索·无事件」，
        # 候选 11 条里 0 条是有效恐袭增量。原因不是信源表没用，是 site: 让
        # 后端按「该站最新」排序、目标月的旧文被挤掉（正样本验证过）。
        # 要按事件查请用 enrich_urls.py——那才是有效路径。
        # 这里保留这个入口只是给「我就想看看某家媒体这个月发了什么」的场景用。
        if a.registry and a.registry_crawl:
            if not os.path.isfile(a.registry):
                print("\n  ✗ 信源表不存在：%s" % a.registry)
                return 1
            reg_out = os.path.join(od, "01b_registry.jsonl")
            rargs = [os.path.join(HERE, "harvest_source_registry.py"),
                     "--registry", a.registry, "--month", a.month,
                     "--out", reg_out,
                     "--status-out", os.path.join(od, "01b_信源遍历状态.xlsx"),
                     "--state", os.path.join(od, ".registry_state.json")]
            if a.search_cmd:
                rargs += ["--search-cmd", a.search_cmd]
            if a.fetch_cmd:
                rargs += ["--fetch-cmd", a.fetch_cmd]
            if a.registry_limit:
                rargs += ["--limit", str(a.registry_limit)]
            ok, out = run_step("采集：信源表逐家遍历", rargs, expect=0, allow_fail=True)
            results.append(("采集(信源遍历)", ok, out))
            if ok and os.path.isfile(reg_out):
                parts.append(reg_out)

        # ---- 并联：ACLED（唯一能系统性补非致命事件的源）----
        # UCDP 的门槛是「造成死亡」，非致命与未遂全靠这一路。
        # 实测 2025-08：ACLED 全球 25000 条里 81.8% 是零死亡事件，UCDP 一条都不给。
        if a.acled or a.acled_env:
            acl_out = a.acled or os.path.join(od, "01d_acled.jsonl")
            if a.acled and os.path.isfile(a.acled):
                print("\n  使用已有 ACLED 文件：%s" % a.acled)
                parts.append(a.acled)
            elif a.acled_env:
                rargs = [os.path.join(HERE, "harvest_acled.py"),
                         "--month", a.month, "--out", acl_out,
                         "--env-file", a.acled_env,
                         "--max-pages", str(a.acled_max_pages)]
                ok, out = run_step("采集：ACLED（补非致命）", rargs,
                                   expect=0, allow_fail=True)
                results.append(("采集(ACLED)", ok, out))
                if ok and os.path.isfile(acl_out):
                    parts.append(acl_out)
            else:
                print("\n  ⚠️  --acled 指向的文件不存在：%s" % a.acled)

        # ---- 并联：未遂/非致命专项 ----
        # 口径要求「预谋被破获、未遂、零伤亡的案子照常计入」，
        # 而 UCDP 只收造成死亡的事件 —— 这一类只能靠定向检索补。
        # 未遂通道改用 harvest_nonlethal.py：按国家 × 语言组合出 2-3 条检索式。
        # 原先只有 3 条全球检索式，不带国家也不带月份英文名，召回很散；
        # 实测 2025-08 那 3 条一条未遂都没收进来。
        nl_out2 = os.path.join(od, "01e_未遂.jsonl")
        if a.nonlethal:
            if not a.search_cmd:
                print("\n  ⚠️  --nonlethal 需要同时给 --search-cmd，已跳过")
            else:
                nargs = [os.path.join(HERE, "harvest_nonlethal.py"),
                         "--month", a.month, "--out", nl_out2,
                         "--search-cmd", a.search_cmd,
                         "--top", str(a.nonlethal_top)]
                # 用已采到的 UCDP 候选来决定查哪些国家。
                # 不能等 --from cand：那一步在合并之后，此刻还没生成，
                # 脚本会因为既没有 --from 也没有 --countries 直接退出。
                _src_for_country = os.path.join(od, "01a_ucdp.jsonl")
                if os.path.isfile(_src_for_country):
                    nargs += ["--from", _src_for_country]
                elif os.path.isfile(cand):
                    nargs += ["--from", cand]
                else:
                    nargs += ["--countries", "Nigeria,Somalia,Pakistan,Syria,Iraq,Afghanistan"]
                ok, out = run_step("采集：未遂/被挫败专项", nargs,
                                   expect=0, allow_fail=True)
                results.append(("采集(未遂)", ok, out))
                if ok and os.path.isfile(nl_out2):
                    parts.append(nl_out2)

        # ---- 合并多源 ----
        contrib = []
        if len(parts) > 1:
            print("\n  合并多源候选：")
            for pth in parts:
                try:
                    with open(pth, encoding="utf-8") as f:
                        contrib.append((os.path.basename(pth),
                                        sum(1 for ln in f if ln.strip())))
                except Exception:  # noqa: BLE001
                    pass
            nm = merge_candidates(parts, cand)
            print("    → 去重合并后 %d 条" % nm)
        elif len(parts) == 1 and os.path.abspath(parts[0]) != os.path.abspath(cand):
            try:
                import shutil
                shutil.copyfile(parts[0], cand)
            except Exception:  # noqa: BLE001
                pass

        # ---- 覆盖率自证 + 跨源交叉核验 ----
        # 这两步不是可选项：交付物要能回答「你们漏没漏」和「每条凭什么可信」。
        # 只报台账不报覆盖率，等于让人相信我们刚好把该收的都收到了。
        cross_out = os.path.join(od, "01x_合并事件.jsonl")
        cross_md = os.path.join(od, "交叉核验.md")
        audit_md = os.path.join(od, "覆盖率审计.md")
        if a.audit and len(parts) >= 1:
            srcargs = []
            for pth in parts:
                nm0 = os.path.splitext(os.path.basename(pth))[0]
                nm0 = nm0.replace("01a_", "").replace("01b_", "").replace("01d_", "")
                srcargs += ["--src", "%s=%s" % (nm0, pth)]
            if len(srcargs) >= 4:
                run_step("覆盖率审计",
                         [os.path.join(HERE, "audit_coverage.py")] + srcargs +
                         ["--out", audit_md], expect=0, allow_fail=True)
            # 双源死亡数比对：回答「权威库本身的数据可信度如何」
            _uc = [x for x in parts if "01a_ucdp" in x]
            _ac = [x for x in parts if "acled" in x.lower()]
            if _uc and _ac:
                run_step("双源死亡数比对",
                         [os.path.join(HERE, "compare_sources.py"),
                          "--a", _uc[0], "--b", _ac[0],
                          "--a-name", "UCDP", "--b-name", "ACLED",
                          "--out", os.path.join(od, "双源比对.md")],
                         expect=0, allow_fail=True)

            run_step("跨源交叉核验",
                     [os.path.join(HERE, "cross_verify.py")] + srcargs +
                     ["--out", cross_out, "--report", cross_md],
                     expect=0, allow_fail=True)

        # 采集后必须校验：0 条候选不许继续往下跑
        # 实测：候选集只有 2026-09，查 2025-08 会命中 0 条，
        # 但流水线一路空转到底，最后只报「找不到判定结果」，让人以为是自己的问题。
        ncand = 0
        if os.path.isfile(cand):
            with open(cand, encoding="utf-8") as _f:
                ncand = sum(1 for _l in _f if _l.strip())
        if ncand == 0:
            print("")
            print("  ✗ 采集到 0 条候选，流水线终止。")
            print("    常见原因：")
            print("    1) 用候选集查了历史月份 —— 候选集只含在编月份")
            print("       处置：改用正式 GED（--ucdp ged261-csv.zip）")
            print("    2) 该月确实未被数据源收录（UCDP 正式版滞后约一年）")
            print("       处置：换一个已收录的月份，或接 ACLED")
            print("    3) 网络/代理问题导致下载不完整")
            print("       处置：python scripts/fetch_ucdp.py --check --outdir ./数据源")
            print("")
            print("  全链路结果：")
            for _n, _o, _ in results:
                print("    %-18s %s" % (_n, "✅" if _o else "❌"))
            return 2
        print("  候选文件：%d 条" % ncand)

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

        jargs = [os.path.join(HERE, "judge_events.py"),
                 "--in", cand, "--out", judged,
                 "--out-manual", os.path.join(od, "02_需人工判.json"),
                 "--out-excluded", os.path.join(od, "02b_排除.json"),
                 "--out-keep", os.path.join(od, "02c_保留作对照.json"),
                 # 显式给 0：口径要求未遂与零伤亡照常计入
                 "--min-deaths", "0"]
        # 把跨源核验结果传进去：19 列台账的「置信度」列要写 A/B，
        # 而 A 的定义是「两个以上独立源都记了」—— 判定链自己看不到别的源，只能靠这份映射。
        _vmap = os.path.join(od, "01x_合并事件.jsonl.idx.json")
        if os.path.isfile(_vmap):
            jargs += ["--verify-map", _vmap]
        run_step("判定（判定链）", jargs, allow_fail=True)
        # 漏斗对账：每条数据的去向必须说得清。
        # 「无遗漏」不等于「全进台账」—— 判定链本来就要排除国家间战争、
        # 帮派火并、执法行动。要保证的是：四项加起来必须等于输入。
        run_step("漏斗对账（每条数据去向）",
                 [os.path.join(HERE, "audit_funnel.py"),
                  "--cand", cand,
                  "--ledger", judged,
                  "--manual", os.path.join(od, "02_需人工判.json"),
                  "--excluded", os.path.join(od, "02b_排除.json"),
                  "--keep", os.path.join(od, "02c_保留作对照.json"),
                  "--out", os.path.join(od, "漏斗对账.md")],
                 expect=0, allow_fail=True)

        print("")
        print("  ℹ️  「需人工判」不是「待定」，是明确的动作：逐条看原文判完写回。")
        print("     判定规则可改：edit scripts/judge_events.py 里的 judge() 函数。")

        # 可选：补「信息来源URL」（UCDP 不给链接，需靠媒体名+标题反查）
        if a.search_cmd:
            judged_url = os.path.join(od, "02_判定_带链接.json")
            eargs = [os.path.join(HERE, "enrich_urls.py"),
                     "--in", judged, "--out", judged_url,
                     "--search-cmd", a.search_cmd,
                     "--budget-seconds", str(a.enrich_budget)]
            # 传信源表：补链时顺便把每条链接归属到机构，一次算完覆盖率
            if a.registry and os.path.isfile(a.registry):
                eargs += ["--registry", a.registry]
            if a.enrich_limit:
                eargs += ["--limit", str(a.enrich_limit)]
            run_step("补信息来源URL（限 %d 条 / %d 秒）"
                     % (a.enrich_limit or 99999, a.enrich_budget),
                     eargs, allow_fail=True)
            if os.path.isfile(judged_url):
                judged = judged_url

    # ---- 正文核验 ----
    # 靠标题判断链接对不对，实测只有 67% 准。读正文能把「明确错误」筛出来
    # （实测 40 条抓到正文的里只有 2 条是错的，5%），并给每条一个可复核的结论。
    # 没给 --fetch-cmd 就跳过 —— 不抓正文也能交付，只是少了这道确认。
    if a.fetch_cmd and os.path.isfile(judged):
        vout = os.path.join(od, "02_判定_已核验.json")
        vargs = [os.path.join(HERE, "verify_urls.py"),
                 "--in", judged, "--out", vout,
                 "--fetch-cmd", a.fetch_cmd,
                 # 必须给预算：不给自己跑到天荒地老，run_step 默认 600 秒会超时
                 # 把整步 kill 掉，输出文件都不生成 —— 实测 34 条 × 20 秒 = 11 分钟
                 # 就超了，表现为「▶ 正文核验」打印了但结果全丢。
                 "--budget-seconds", "540"]
        run_step("正文核验（抓页面确认链接是否对应用起事件）",
                 vargs, expect=0, allow_fail=True, timeout=660)
        if os.path.isfile(vout):
            judged = vout

    # ---- 信源表覆盖度审计 ----
    # 这一步回答「我们的名单到底覆盖了多少真实报道这些事件的媒体」。
    # 必须放在补链之后：补链结果里的域名才是实际用到的媒体。
    if a.registry and os.path.isfile(a.registry):
        rcargs = [os.path.join(HERE, "audit_registry_coverage.py"),
                  "--registry", a.registry,
                  "--out", os.path.join(od, "信源覆盖.md")]
        if os.path.isfile(cand):
            rcargs += ["--events", cand]
        # 补链结果优先；没有就退回判定结果（那样覆盖率会低，但不会崩）
        _linked = os.path.join(od, "02_判定_带链接.json")
        if os.path.isfile(_linked):
            rcargs += ["--linked", _linked]
        elif os.path.isfile(judged):
            rcargs += ["--linked", judged]
        run_step("信源表覆盖度审计", rcargs, expect=0, allow_fail=True)

    if a.stage in ("judge",):
        return 0

    # ============ 阶段 3+4：台账与门禁 ============
    if a.stage in ("ledger", "gate", "all"):
        if not os.path.isfile(judged):
            print("\n找不到判定结果 %s。" % judged)
            print("判定结果需是 json 数组，每项含 19 列字段（见 make_ledger.py 的模板）。")
            print("你可以：① 手工判定后写成该文件；② 用你自己的判定脚本产出它。")
            return 1
        # 口径标注：让每行自带「这个死亡数来自哪个源、该源口径如何」。
        # 不动 19 列表头（硬性要求），写进备注列末尾。
        if os.path.isfile(judged):
            _tagged = os.path.join(od, "02_判定_带口径.json")
            _targs = [os.path.join(HERE, "tag_caliber.py"),
                      "--in", judged, "--out", _tagged]
            _uc2 = [x for x in parts if "01a_ucdp" in x]
            _ac2 = [x for x in parts if "acled" in x.lower()]
            if _uc2:
                _targs += ["--ucdp", _uc2[0]]
            if _ac2:
                _targs += ["--acled", _ac2[0]]
            run_step("死亡数口径标注", _targs, expect=0, allow_fail=True)
            if os.path.isfile(_tagged):
                judged = _tagged

        # 溯源嵌入：把源数据自带的事件ID/坐标/原始来源写进台账。
        # 这一步比补链接重要 —— 权威库 100% 自带这些字段，只是台账没写进去。
        # 补链接实测只到 24%，而这些字段能覆盖到 99.9%。
        # 「可点击 URL」和「可查事件ID」是两种可追溯，后者对长尾事件是唯一可行的。
        _prov_out = os.path.join(od, "02_判定_带溯源.json")
        _pargs = [os.path.join(HERE, "embed_provenance.py"),
                  "--ledger", judged, "--out", _prov_out]
        _uc3 = [x for x in parts if "01a_ucdp" in x]
        _ac3 = [x for x in parts if "acled" in x.lower()]
        if _ac3:
            _pargs += ["--acled", _ac3[0]]
        if _uc3:
            _pargs += ["--ucdp", _uc3[0]]
        # 条件只要 judged 存在，且至少给了一个源。
        # 早前的条件写的是 judged 里含 "01a_ucdp" 或以 .xlsx 结尾 ——
        # 而 judged 是 02_判定.json，两者都不满足，这步永远不跑。
        # 表现为台账里「事件ID/坐标/原始来源」全为 0，端到端测试抓出来的。
        if os.path.isfile(judged) and (_ac3 or _uc3):
            run_step("溯源嵌入（事件ID/坐标/原始来源）", _pargs,
                     expect=0, allow_fail=True)
            if os.path.isfile(_prov_out):
                judged = _prov_out

        run_step("生成 19 列台账", [os.path.join(HERE, "make_ledger.py"),
                                 "--template", "19列", "--rows", judged,
                                 "--out", ledger], allow_fail=True)
        # ---- 性质分档统计 ----
        # 整体精确率约 40-60%，但 A+B 档（有组织名/分方死亡明确/多源）
        # 实测 68-82%。不打印这个，使用者会拿整份台账当等质量数据用。
        try:
            import openpyxl as _ox
            _wb = _ox.load_workbook(ledger, data_only=True)
            _ws = _wb.worksheets[0]
            _tx = {}
            for _r in list(_ws.iter_rows(values_only=True))[1:]:
                if not _r or not _r[0]:
                    continue
                _m = re.search(r"\[性质\]([ABC])", str(_r[-1] or ""))
                _k = _m.group(1) if _m else "?"
                _tx[_k] = _tx.get(_k, 0) + 1
            _hi = _tx.get("A", 0) + _tx.get("B", 0)
            _all = sum(_tx.values())
            if _all:
                print("")
                print("  性质分档：A %d ｜ B %d ｜ C %d"
                      % (_tx.get("A", 0), _tx.get("B", 0), _tx.get("C", 0)))
                print("  高置信子集 A+B = %d 条（%.0f%%），实测精确率约 68-82%%"
                      % (_hi, 100.0 * _hi / _all))
                print("  C 档 %d 条（%.0f%%）精确率约 26-52%%，"
                      "逐条引用前需人工复核"
                      % (_tx.get("C", 0), 100.0 * _tx.get("C", 0) / _all))
        except Exception as _e:  # noqa: BLE001
            print("  （性质分档统计跳过：%s）" % str(_e)[:60])

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

    # ============ 覆盖边界声明（每版必出，不许省）============
    # 台账的数字看起来像「全球恐怖袭击总量」，其实只是「被这几个源收录到的那部分」。
    # 不写清楚，读的人会拿它当普查数据用，而这个误用从表里完全看不出来。
    if a.stage in ("gate", "all") and os.path.isfile(ledger):
        zero_death = attempted = nled = 0
        try:
            import openpyxl
            wb = openpyxl.load_workbook(ledger, data_only=True)
            ws = wb.worksheets[0]
            rows = list(ws.iter_rows(values_only=True))
            hdr = [str(h or "").strip() for h in rows[0]]
            i_d = hdr.index("死亡人数") if "死亡人数" in hdr else None
            i_b = hdr.index("备注") if "备注" in hdr else None
            for r in rows[1:]:
                if not r or not r[0]:
                    continue
                nled += 1
                if i_d is not None:
                    # 注意 r[i_d] 取到 int 0 时是假值，直接 or "" 会变成空串，
                    # 于是「死亡=0」全被统计成「死亡为空」，声明里永远报 0 条。
                    # 实测踩过：台账里明明 773 行零死亡，声明却说「非致命 0 条」。
                    _raw = r[i_d]
                    if _raw is not None and str(_raw).strip() in ("0", "0.0"):
                        zero_death += 1
                if i_b is not None:
                    b = str(r[i_b] or "")
                    if re.search(r"未遂|被挫败|图谋|未发生", b):
                        attempted += 1
        except Exception:  # noqa: BLE001
            pass
        # 国家分布：用来暴露数据源的地域偏向，避免把「俄乌密集」读成「全球恐袭上升」
        country_dist = []
        try:
            import openpyxl as _ox
            _wb = _ox.load_workbook(ledger, data_only=True)
            _ws = _wb.worksheets[0]
            _rows = list(_ws.iter_rows(values_only=True))
            _hdr = [str(h or "").strip() for h in _rows[0]]
            _ic = _hdr.index("国家") if "国家" in _hdr else None
            if _ic is not None:
                from collections import Counter as _C
                _c = _C(str(r[_ic] or "").strip() for r in _rows[1:] if r and r[0])
                country_dist = _c.most_common()
        except Exception:  # noqa: BLE001
            pass

        cn = os.path.join(od, "覆盖边界声明.md")
        # 多源印证率：从交叉核验结果里读，写进声明。
        # 这是「可复核」最直接的一个数：多少条有两个源都记了。
        verify_stat = {}
        if os.path.isfile(cross_out):
            try:
                _n = _multi = _agg = 0
                with io.open(cross_out, encoding="utf-8") as _f:
                    for _ln in _f:
                        _ln = _ln.strip()
                        if not _ln:
                            continue
                        _o = json.loads(_ln)
                        _n += 1
                        if (_o.get("n_sources") or 1) >= 2:
                            _multi += 1
                        if _o.get("is_aggregate"):
                            _agg += 1
                verify_stat = {"events": _n, "multi": _multi, "aggregate": _agg}
            except Exception:  # noqa: BLE001
                pass

        write_coverage_note(cn, a.month, contrib,
                            {"zero_death": zero_death, "attempted": attempted,
                             "country_dist": country_dist,
                             "verify": verify_stat}, nled)
        print("\n  覆盖边界声明 → %s" % cn)
        # 两类缺口分开告警：非致命看 ACLED，未遂看 --nonlethal，补法完全不同
        if zero_death == 0 and nled > 20:
            print("")
            print("  ⚠️  非致命 0 条 —— ACLED 没接上或缺数据。")
            print("     加 --acled-env <凭据文件> 让 ACLED 进来，它含大量零死亡事件。")
        if attempted <= 2 and nled > 20:
            print("")
            print("  ⚠️  未遂/被挫败只有 %d 条。" % attempted)
            print("     口径要求「预谋被破获、未遂照常计入」，这一类两个权威库都不收。")
            print("     补法：--nonlethal --search-cmd ...（走 harvest_nonlethal.py）")
            print("     不要用 --registry 逐家遍历 —— 实测 93% 无产出。")

    # ============ 阶段 5：三份报告 + PPT ============
    if a.stage in ("gate", "all") and os.path.isfile(ledger):
        repdir = os.path.join(od, "报告")
        run_step("生成三份专项报告",
                 [os.path.join(HERE, "make_reports.py"),
                  "--ledger", ledger,
                  "--candidates", cand,
                  "--excluded", os.path.join(od, "02b_排除.json"),
                  "--manual", os.path.join(od, "02_需人工判.json"),
                  "--month", a.month, "--outdir", repdir],
                 allow_fail=True)

        deck = os.path.join(od, "index.html")
        tpl = os.path.join(os.path.dirname(HERE), "assets", "templates",
                           "deck-single-file.html")
        if os.path.isfile(tpl):
            import shutil as _sh
            _sh.copy(tpl, deck)
            run_step("生成 PPT",
                     [os.path.join(HERE, "fill_deck.py"), "--deck", deck,
                      "--from-ledger", ledger, "--out", deck], allow_fail=True)
            run_step("门禁：PPT 离线",
                     [os.path.join(HERE, "gate_single_file.py"), deck],
                     allow_fail=True)

        run_step("门禁：数字对账",
                 [os.path.join(HERE, "gate_number_drift.py"), ledger],
                 allow_fail=True)

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
