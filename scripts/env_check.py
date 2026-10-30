#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""环境自检：确认本机跑得动 event-ledger-pipeline。

退出码：0 = 齐备可开工 | 2 = 缺依赖（已给出逐条安装命令）| 1 = 无法判定

用法：
    python scripts/env_check.py
    python scripts/env_check.py --json     # 机读，便于其它脚本消费
    python scripts/env_check.py --fix      # 打印并执行 pip 安装（默认只打印）
"""
import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


# ---------- 依赖清单：导入名 / pip 名 / 用途 / 是否必需 ----------
PY_DEPS = [
    ("openpyxl", "openpyxl", "读写 xlsx 台账", True),
    ("requests", "requests", "抓正文与日期核验", True),
    ("PIL", "Pillow", "截图/拼图/配色测量", False),
    ("xlsxwriter", "XlsxWriter", "带格式导出（可选）", False),
    ("bs4", "beautifulsoup4", "解析 HTML 取 meta", False),
]

# ---------- 外部工具：可执行文件候选 / 用途 / 是否必需 ----------
EXE_DEPS = [
    ("chrome", [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        "chrome", "google-chrome", "chromium",
    ], "无头截图 / PPT 溢出与离线门禁", False),
    ("soffice", [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "soffice",
    ], "docx/pptx/pdf 互转", False),
    ("node", ["node"], "网页 PPT 构建工具链（若用）", False),
]

MIN_PY = (3, 9)


def find_exe(cands):
    for c in cands:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            p = shutil.which(c)
            if p:
                return p
    return None


def check_py():
    out = []
    for mod, pip_name, why, required in PY_DEPS:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "")
            out.append({"name": mod, "ok": True, "version": str(ver), "why": why,
                        "required": required, "pip": pip_name})
        except Exception as e:  # noqa: BLE001
            out.append({"name": mod, "ok": False, "error": type(e).__name__, "why": why,
                        "required": required, "pip": pip_name})
    return out


def check_exe():
    out = []
    for name, cands, why, required in EXE_DEPS:
        p = find_exe(cands)
        out.append({"name": name, "ok": bool(p), "path": p or "", "why": why,
                    "required": required})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix", action="store_true", help="执行 pip 安装缺失的 Python 依赖")
    ap.add_argument("--ppt", action="store_true",
                    help="交付含网页 PPT 时用：把 headless Chrome 列为必需项，"
                         "否则 exit 0 会让人误以为 PPT 验证链可用")
    a = ap.parse_args()

    py_ok = sys.version_info[:2] >= MIN_PY
    pys = check_py()
    exes = check_exe()

    # 交付含 PPT 时，Chrome 是必需项：溢出测量、截图验证全靠它。
    # 不加这个开关时它只是可选，于是 exit 0 会让人以为「环境齐备」而其实做不了 PPT 验证。
    if a.ppt:
        for e in exes:
            if e["name"] == "chrome":
                e["required"] = True

    missing_req = [d for d in pys if not d["ok"] and d["required"]]
    missing_opt = [d for d in pys if not d["ok"] and not d["required"]]
    missing_exe = [e for e in exes if not e["ok"]]

    exit_code = 0 if (py_ok and not missing_req) else 2
    if not py_ok:
        exit_code = 1

    if a.json:
        print(json.dumps({
            "python": {"version": sys.version.split()[0], "ok": py_ok,
                       "min": "%d.%d" % MIN_PY, "exe": sys.executable},
            "python_deps": pys, "external": exes, "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 66)
    print(" event-ledger-pipeline · 环境自检")
    print("=" * 66)
    print("Python   : %s  (%s)" % (sys.version.split()[0], sys.executable))
    print("要求     : >= %d.%d   %s" % (MIN_PY[0], MIN_PY[1], "OK" if py_ok else "不满足"))
    print()

    print("-- Python 依赖 --------------------------------------------------")
    for d in pys:
        tag = "OK  " if d["ok"] else ("缺失" if d["required"] else "缺(可选)")
        ver = d.get("version") or d.get("error") or ""
        print("  [%s] %-14s %-12s %s" % (tag, d["name"], ver, d["why"]))
    print()

    print("-- 外部工具 -----------------------------------------------------")
    for e in exes:
        tag = "OK  " if e["ok"] else ("缺失" if e["required"] else "缺(可选)")
        print("  [%s] %-10s %-52s %s" % (tag, e["name"], e["path"] or "-", e["why"]))
    print()

    if exit_code == 0 and not missing_opt and not missing_exe:
        print("结论：环境齐备，可直接开工。")
        if not a.ppt:
            print("      注意：本次未加 --ppt，headless Chrome 未作必需项。")
            print("      若交付含网页 PPT，改跑 --ppt 确认截图/溢出验证可用。")
        print("=" * 66)
        return 0

    print("-- 安装命令（在本机逐条执行）-------------------------------------")
    if not py_ok:
        print("  Python 版本过低，请先装 3.9+（本机若用 Anaconda，直接用其 python.exe 路径调用本脚本）")
    pip_cmd = "%s -m pip install " % sys.executable
    if missing_req:
        print("  " + pip_cmd + " ".join(d["pip"] for d in missing_req))
    if missing_opt:
        print("  " + pip_cmd + " " + " ".join(d["pip"] for d in missing_opt) + "   # 可选")
    names = {e["name"] for e in missing_exe}
    if "chrome" in names:
        print("  Chrome：装 Chrome 或设置 CHROME_PATH 环境变量；无头截图门禁需要它")
    if "soffice" in names:
        print("  LibreOffice：https://www.libreoffice.org/download/ （只需格式互转时安装）")
    if "node" in names:
        print("  Node：https://nodejs.org/ （只在用构建工具链时需要）")
    print()
    if missing_req:
        print("结论：缺必需依赖 -> 装完重跑本脚本，确认 exit 0 再开工。")
    else:
        print("结论：必需依赖齐备；上面是可选件缺项，视交付形态决定是否安装。")
        if "--ppt" in sys.argv:
            print("      （--ppt 模式下 Chrome 属必需，缺它 PPT 静态检查之外的验证做不了）")
    print("=" * 66)

    if a.fix and missing_req:
        pkgs = [d["pip"] for d in missing_req]
        print("执行：%s -m pip install %s" % (sys.executable, " ".join(pkgs)))
        rc = subprocess.call([sys.executable, "-m", "pip", "install"] + pkgs)
        print("pip 退出码 = %d，请重跑本脚本确认。" % rc)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
