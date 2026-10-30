#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单文件离线交付门禁：外链=0 / 本地资源存在 / 页数与页码连续 / 体积预算 / 关键内容在位。

解决的真实事故：展示 PPT 带 4 个外链（断网打不开）；页码插页后错乱；
「三问」关键词在 18 页里一次都没出现，肉眼逐页看完全看不出。

退出码：0 = 通过 | 2 = 有失败项 | 1 = 无法判定（文件不存在/读不了）

用法：
    python scripts/gate_single_file.py 交付/index.html
    python scripts/gate_single_file.py 交付/index.html --pages 18 --max-mb 5
    python scripts/gate_single_file.py 交付/index.html --require "核心问题" --require "真实性"
    python scripts/gate_single_file.py 交付/index.html --json
"""
import argparse
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


REMOTE = re.compile(r'(?i)(?:src|href|data-src|poster|srcset)\s*=\s*["\'](https?:)?//', )
REMOTE_ANY = re.compile(r'(?i)url\(\s*["\']?(https?:)?//')
IMPORT_REMOTE = re.compile(r'(?i)@import\s+(?:url\()?["\'](https?:)?//')
CDN_HINTS = ["unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "fonts.googleapis",
             "fonts.gstatic", "cdn.tailwindcss.com", "esm.sh", "skypack"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("--pages", type=int, default=None, help="期望页数（用于页码连续性）")
    ap.add_argument("--max-mb", type=float, default=8.0, help="体积上限（MB，含内联资源）")
    ap.add_argument("--require", action="append", default=[], help="必须出现的文本，可多次")
    ap.add_argument("--forbid", action="append", default=[], help="禁止出现的文本，可多次")
    ap.add_argument("--allow-remote", action="append", default=[],
                    help="允许的外链域名（默认一个都不允许）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.html):
        print("文件不存在：%s" % a.html)
        return 1
    try:
        h = open(a.html, encoding="utf-8", errors="replace").read()
    except Exception as e:  # noqa: BLE001
        print("无法读取：%s" % e)
        return 1

    fails, warns = [], []
    size_mb = os.path.getsize(a.html) / 1048576.0

    # ---- 1. 外链 ----
    remote = []
    for m in REMOTE.finditer(h):
        s = h[m.start():m.start() + 160]
        u = re.search(r'["\']((?:https?:)?//[^"\']+)', s)
        remote.append(u.group(1) if u else s.strip()[:80])
    remote += [m.group(0) for m in REMOTE_ANY.finditer(h)]
    remote += [m.group(0) for m in IMPORT_REMOTE.finditer(h)]
    remote = [r for r in remote
              if not any(d in r for d in a.allow_remote)]
    if remote:
        fails.append("外链 %d 处（必须内联或本地化，否则断网演示不了）：%s"
                     % (len(remote), remote[:8]))

    # CDN 分级：静态 src/href 引用 = 硬依赖（断网必坏）-> fail；
    # 只出现在 try/catch 的动态 import 里 = 软 fallback（本地优先，断网不影响）-> warn
    for c in CDN_HINTS:
        if c not in h:
            continue
        if any(c in r for r in remote):
            continue                      # 已在「外链」里报过，不重复
        # 取每个 src/href 之后的 200 字符窗口，判断是否直接硬引用了该 CDN
        win = re.findall('(?i)(?:src|href)\\s*=\\s*.{0,200}', h)
        hard = any(re.escape(c) in w for w in win)
        if hard:
            fails.append("仍引用 CDN（静态 src/href 硬依赖，断网必坏）：%s" % c)
        else:
            warns.append("含 CDN 引用但属动态 import fallback（%s）：离线时会走本地或降级，"
                         "不影响断网演示；建议确认本地资源存在" % c)

    # ---- 2. 本地资源存在 ----
    base = os.path.dirname(os.path.abspath(a.html))
    local = re.findall(r'(?i)(?:src|href)\s*=\s*["\'](?!https?:|//|data:|#|mailto:)'
                       r'([^"\']+)["\']', h)
    missing = []
    for p in set(local):
        if p.startswith("file://"):
            continue
        fp = os.path.normpath(os.path.join(base, p.split("?")[0].split("#")[0]))
        if not os.path.isfile(fp):
            missing.append(p)
    if missing:
        fails.append("引用了不存在的本地文件 %d 个：%s" % (len(missing), missing[:8]))

    # ---- 3. 页数与页码 ----
    n_sec = len(re.findall(r"<section\b", h, re.I))
    if a.pages is not None:
        if n_sec != a.pages:
            fails.append("页数不符：--pages %d，实际 <section> %d" % (a.pages, n_sec))
        nums = re.findall(r"\b(\d{1,3})\s*/\s*%d\b" % a.pages, h)
        if nums:
            want = ["%02d" % i for i in range(1, a.pages + 1)]
            if nums != want:
                # 允许前导零不一致
                try:
                    if [int(x) for x in nums] != list(range(1, a.pages + 1)):
                        fails.append("页码不连续：%s" % " ".join(nums[:24]))
                except Exception:  # noqa: BLE001
                    fails.append("页码不可解析：%s" % " ".join(nums[:12]))
        else:
            if re.search(r"(?i)(id\s*=\s*[\"'](cur|pagecur|pagenow)|class\s*=\s*[\"'][^\"']*pager)", h):
                warns.append("页码由 JS 动态渲染（#cur / .pager）：静态检查跳过。"
                             "请在浏览器里翻到末页确认显示为 %d/%d" % (a.pages, a.pages))
            else:
                fails.append("既没有「NN / %d」页码，也没有 JS 页码容器："
                             "观众无法知道在第几页" % a.pages)

    # ---- 4. 体积 ----
    if size_mb > a.max_mb:
        fails.append("体积 %.2f MB 超过上限 %.2f MB" % (size_mb, a.max_mb))

    # ---- 5. 内容在位 / 禁止词 ----
    for t in a.require:
        if t not in h:
            fails.append("缺少必需内容：%r（实测 0 次出现）" % t)
    for t in a.forbid:
        if t in h:
            fails.append("出现禁止内容：%r（%d 次）" % (t, h.count(t)))

    # ---- 6. 离线自足性提醒 ----
    if re.search(r"(?i)\bfetch\s*\(", h) and re.search(r"(?i)\.json\b", h):
        warns.append("检测到 fetch(...) 与 .json 引用：file:// 下会被 CORS 拦截，"
                     "数据应内联为 JS 变量")
    if re.search(r'(?i)<script[^>]+type\s*=\s*["\']module["\']', h):
        warns.append("检测到 <script type=module>：file:// 下 import 会被拦，"
                     "依赖需用 UMD/classic 构建内联")

    exit_code = 0 if not fails else 2

    if a.json:
        print(json.dumps({
            "file": a.html, "size_mb": round(size_mb, 3), "sections": n_sec,
            "remote_refs": remote[:20], "missing_local": missing,
            "fails": fails, "warns": warns, "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 66)
    print(" 单文件离线交付门禁")
    print("=" * 66)
    print("文件      : %s" % a.html)
    print("体积      : %.2f MB  (上限 %.2f)" % (size_mb, a.max_mb))
    print("<section>: %d 页%s" % (n_sec, "" if a.pages is None else "（期望 %d）" % a.pages))
    print("外链      : %d 处" % len(remote))
    print("本地缺失  : %d 个" % len(missing))
    print()

    if warns:
        print("-- 提醒 ---------------------------------------------------------")
        for w in warns:
            print("  ! %s" % w)
        print()

    if fails:
        print("-- 失败项 -------------------------------------------------------")
        for f in fails:
            print("  ✗ %s" % f)
        print()
        print("结论：不通过。修完再跑本门禁；断网后必须能完整演示一遍。")
        print("=" * 66)
        return 2

    print("结论：通过。断网后双击该文件应能完整演示（建议真断网跑一遍再交付）。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
