#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键获取 UCDP 数据（自动下载 + 断点续传 + 版本探测）。

为什么单独做一个
----------------
开箱即用的最大障碍不是脚本逻辑，是**用户不知道该下哪个文件**：
  · 正式 GED 有多个版本（ged261 / ged251 …），版本号随发布更新
  · 候选集文件名带版本（GEDEvent_v26_0_9.csv）
  · 39MB 在国内网络可能要十几分钟，中途断线要能续

本脚本做三件事：
  1. **自动探测最新版本**（抓下载页，找最新的 gedXXX-csv.zip）
  2. **断点续传下载**（curl -C -，中断后重跑会接着下）
  3. **完整性校验**（zip 必须能打开、必须含 csv）

用法
----
  python scripts/fetch_ucdp.py --outdir ./数据源              # 下最新正式 GED
  python scripts/fetch_ucdp.py --outdir ./数据源 --candidate  # 下候选集（轻量，1.6MB）
  python scripts/fetch_ucdp.py --outdir ./数据源 --check      # 只检查已有文件
  python scripts/fetch_ucdp.py --list                         # 列出可用版本

退出码：0 = 文件已就绪且校验通过 | 2 = 下载失败/校验失败 | 1 = 无法判定
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PAGE = "https://ucdp.uu.se/downloads/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0 Safari/537.36"


def _http_get(url, timeout=30):
    """取文本；优先 requests（trust_env=False 避开未启动的代理），失败再 curl。"""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA})
        r = s.get(url, timeout=timeout, verify=False)
        if r.status_code == 200:
            return r.text
    except Exception:  # noqa: BLE001
        pass
    if shutil.which("curl"):
        try:
            r = subprocess.run(["curl", "-sL", "-m", str(timeout), url],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=_clean_env())
            if r.stdout:
                return r.stdout
        except Exception:  # noqa: BLE001
            pass
    return None


def list_versions():
    """从下载页找所有 gedXXX-csv.zip，返回版本号列表（降序）。"""
    html = _http_get(PAGE)
    if not html:
        return []
    found = set(re.findall(r"ged(\d{3})-csv\.zip", html))
    return sorted(found, reverse=True)


def candidate_files():
    """找月度候选集文件名。

    坑：下载页里同时有 candidateged/ 和 vpp/ 两类文件，vpp 的是「验证期专项包」，
    路径不同（放 candidateged/ 会 404）。必须按所在目录精确匹配。
    """
    html = _http_get(PAGE)
    if not html:
        return []
    # 只取 candidateged/ 目录下的，且排除 vpp 专项包（文件名含下划线段落过多）
    cand = set(re.findall(r"downloads/candidateged/(GEDEvent_[A-Za-z0-9_]+\.csv)", html))
    return sorted(cand)


def _clean_env():
    """返回一个清空了代理变量的环境。

    这是通用性的关键：很多机器设了 HTTP_PROXY 指向一个没启动的代理（Clash/V2Ray 关了），
    此时 curl / wget 会**所有请求都失败**，看起来像「断网」，实际是代理变量污染。
    必须显式清空，否则任何人的机器上都可能跑不动。
    """
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def download(url, dest, timeout=180):
    """优先 curl（支持断点续传），没有 curl 用 requests 流式下载。"""
    env = _clean_env()
    if shutil.which("curl"):
        for attempt in range(1, 4):
            r = subprocess.run(
                ["curl", "-C", "-", "-L", "--no-progress-meter",
                 "-m", str(timeout), "-o", dest, url],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=env)
            size = os.path.getsize(dest) if os.path.isfile(dest) else 0
            print("  尝试 %d/%d：已下 %s" % (attempt, 3, _human(size)), flush=True)
            if r.returncode == 0 and size > 1024:
                return True, size
        size = os.path.getsize(dest) if os.path.isfile(dest) else 0
        return False, size

    # 无 curl：用 requests 流式
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA})
        done = os.path.getsize(dest) if os.path.isfile(dest) else 0
        headers = {"Range": "bytes=%d-" % done} if done else {}
        with s.get(url, stream=True, timeout=timeout, verify=False,
                   headers=headers) as r:
            mode = "ab" if (done and r.status_code == 206) else "wb"
            with open(dest, mode) as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
        size = os.path.getsize(dest)
        return size > 1024, size
    except Exception as e:  # noqa: BLE001
        print("  下载失败：%s" % e)
        return False, 0


def _human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f TB" % n


def verify(path):
    """校验文件：zip 必须能打开且含 csv；csv 必须像 UCDP（含 date_start 列）。"""
    if not os.path.isfile(path):
        return False, "文件不存在"
    if path.lower().endswith(".zip"):
        try:
            import zipfile
            z = zipfile.ZipFile(path)
            bad = z.testzip()
            if bad:
                return False, "zip 内文件损坏：%s" % bad
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                return False, "zip 里没有 csv"
            # 抽第一行验证字段
            with z.open(names[0]) as f:
                head = f.readline().decode("utf-8", "replace")
            if "date_start" not in head:
                return False, "csv 缺少 date_start 列（可能不是 UCDP 文件）"
            return True, "zip OK（含 %s）" % names[0]
        except Exception as e:  # noqa: BLE001
            return False, "zip 无法打开（可能下载未完成）：%s" % e

    # 裸 csv：读完整第一行（表头可能有 40+ 列，400 字节不够）
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            head = f.readline()
        if "date_start" not in head:
            return False, "csv 缺少 date_start 列（可能不是 UCDP 文件）"
        return True, "csv OK"
    except Exception as e:  # noqa: BLE001
        return False, "无法读取：%s" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="./数据源")
    ap.add_argument("--candidate", action="store_true", help="下候选集（轻量 1.6MB）而非正式 GED")
    ap.add_argument("--version", default=None, help="指定 GED 版本号，如 261")
    ap.add_argument("--check", action="store_true", help="只检查已有文件，不下载")
    ap.add_argument("--list", action="store_true", help="列出可用版本")
    ap.add_argument("--timeout", type=int, default=180,
                    help="单次下载超时秒数（默认 180，重试 3 次）")
    a = ap.parse_args()

    if a.list:
        vs = list_versions()
        print("UCDP 正式 GED 可用版本（新→旧）：%s" % (", ".join(vs) if vs else "探测失败"))
        cf = candidate_files()
        print("候选集文件：%s" % (", ".join(cf) if cf else "探测失败"))
        return 0 if vs else 2

    os.makedirs(a.outdir, exist_ok=True)

    # ---- 只检查 ----
    if a.check:
        found = []
        for fn in os.listdir(a.outdir):
            if fn.lower().endswith((".zip", ".csv")):
                p = os.path.join(a.outdir, fn)
                ok, msg = verify(p)
                print("  %-42s %-4s %s" % (fn, "OK" if ok else "BAD", msg))
                if ok:
                    found.append(p)
        if not found:
            print("\n目录里没有可用的 UCDP 文件。")
            print("下载：python scripts/fetch_ucdp.py --outdir %s" % a.outdir)
            return 2
        print("\n可用文件 %d 个。" % len(found))
        return 0

    # ---- 候选集 ----
    if a.candidate:
        cf = candidate_files()
        if not cf:
            print("无法探测候选集文件名。请手动下载：")
            print("  https://ucdp.uu.se/downloads/  → Candidate datasets")
            return 2
        # 优先选「月度候选集」：命名最短的那个（专项包带长日期段，如 _1101_2512）
        fn = sorted(cf, key=len)[0]
        print("候选集可选：%s" % ", ".join(cf))
        print("选用：%s" % fn)
        dest = os.path.join(a.outdir, fn)
        if os.path.isfile(dest) and verify(dest)[0]:
            print("已存在且校验通过：%s" % dest)
            return 0
        print("下载候选集 %s …" % fn)
        ok, size = download("https://ucdp.uu.se/downloads/candidateged/" + fn, dest, a.timeout)
        good, msg = verify(dest)
        print("  %s（%s）" % (msg, _human(size)))
        if not good:
            print("下载不完整，重跑本命令会断点续传。")
            return 2
        print("完成：%s" % dest)
        return 0

    # ---- 正式 GED ----
    ver = a.version
    if not ver:
        vs = list_versions()
        if not vs:
            print("无法探测最新版本（下载页不可达）。可手动指定 --version，或手动下载：")
            print("  https://ucdp.uu.se/downloads/  → UCDP GED")
            return 2
        ver = vs[0]
        print("探测到最新版本：ged%s" % ver)
    fn = "ged%s-csv.zip" % ver
    dest = os.path.join(a.outdir, fn)
    url = "https://ucdp.uu.se/downloads/ged/" + fn

    if os.path.isfile(dest) and verify(dest)[0]:
        print("已存在且校验通过：%s（%s）" % (dest, _human(os.path.getsize(dest))))
        print("提示：这只是文件就绪，要抽某月请跑 harvest_ucdp.py")
        return 0

    print("下载 %s（约 39MB，国内网络可能需要十几分钟）" % fn)
    print("  支持断点续传：中断后重跑本命令会接着下")
    ok, size = download(url, dest, a.timeout)
    good, msg = verify(dest)
    print("  校验：%s（%s）" % (msg, _human(size)))
    if not good:
        print("\n未完成。处置：")
        print("  1) 重跑本命令（会断点续传）")
        print("  2) 或手动下载：%s" % url)
        print("  3) 或改用轻量候选集：python scripts/fetch_ucdp.py --candidate --outdir %s" % a.outdir)
        return 2

    print("\n完成：%s" % dest)
    print("下一步：python scripts/harvest_ucdp.py --ged %s --month 2025-08 --out 候选.jsonl" % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
