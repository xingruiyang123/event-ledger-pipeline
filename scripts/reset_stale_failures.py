"""校正陈旧的失败状态 —— 把「其实可达」的源放回正常调度。

问题
----
状态里的 fail_streak 和 last_error 是**历史记录**，不是当前事实。
312 家判为「连续失败≥3」的源，当场复测 282 家能正常解析（90%）。
它们被锁在 6-48 小时的熔断里，只是因为 11 月 20 日晚那段代理抖动。

不校正的后果：这些源要等熔断到期才重试，期间完全不在采。
叠加递进熔断（3→6h、4→12h、5→24h、6→48h），
一次夜间抖动能让上百个源停整整两天。

做法
----
逐个复测，按结果分三种处置：

  能解析出条目   → 重置 fail_streak=0、清 last_error、清 break_until
                   但不改 n_fetches 和 seen_ids（那是真实历史）
  能通但 0 条    → 保留失败态，但把 last_error 改成准确的描述
                   这类是「入口不是 feed」，跟网络无关，得人来配
  仍然不通       → 原样保留，让它按熔断继续

为什么不直接清空所有 fail_streak
--------------------------------
因为真失败的源需要被记住。30 家确实打不开的（403/404/超时），
清了会每轮重试、浪费时间还压慢整体。区别对待才对。
"""
import argparse
import glob
import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=r"D:\event-ledger-data\state")
    ap.add_argument("--min-streak", type=int, default=3)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument("--out", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    import scheduler as S
    S.detect_proxy(None)

    targets = []
    for f in glob.glob(os.path.join(a.state_dir, "*.json")):
        try:
            j = json.load(io.open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if int(j.get("fail_streak") or 0) < a.min_streak:
            continue
        if not j.get("feed_url"):
            continue
        targets.append((f, j))

    print("")
    print("  连续失败 ≥%d 的源 %d 家，逐个复测..." % (a.min_streak, len(targets)))

    def probe(item):
        f, j = item
        code, body, enc, final = S.http_get(j["feed_url"],
                                            timeout=a.timeout, retries=1)
        n = len(S.parse_feed(body)) if (code == 200 and body) else 0
        return (f, j, code, n)

    res = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(probe, targets):
            res.append(r)

    recovered = [(f, j, c, n) for f, j, c, n in res if c == 200 and n > 0]
    empty = [(f, j, c, n) for f, j, c, n in res if c == 200 and n == 0]
    broken = [(f, j, c, n) for f, j, c, n in res if c != 200]

    print("")
    print("  能解析出条目（可放回调度）  %d 家" % len(recovered))
    print("  能通但 0 条（入口不是 feed） %d 家" % len(empty))
    print("  仍然不通（保留熔断）        %d 家" % len(broken))
    print("")

    if empty:
        print("  入口不是 feed 的：")
        for f, j, c, n in empty[:15]:
            print("    %-26s %s" % (j.get("domain", "")[:24],
                                    str(j.get("feed_url"))[:60]))
        print("")

    if broken:
        print("  仍然不通的：")
        for f, j, c, n in broken[:15]:
            print("    %-26s HTTP %s" % (j.get("domain", "")[:24], c))
        print("")

    if a.out:
        io.open(a.out, "w", encoding="utf-8").write(json.dumps(
            {"recovered": [{"domain": j.get("domain"), "code": c, "n": n}
                           for _f, j, c, n in recovered],
             "empty": [{"domain": j.get("domain"), "url": j.get("feed_url")}
                       for _f, j, _c, _n in empty],
             "broken": [{"domain": j.get("domain"), "code": c}
                        for _f, j, _c, _n in broken]},
            ensure_ascii=False, indent=1))
        print("  明细 → %s" % a.out)

    if not a.apply:
        print("  只诊断未改动。加 --apply 执行校正。")
        return 0

    n1 = n2 = 0
    for f, j, c, n in recovered:
        j["fail_streak"] = 0
        j["break_until"] = None
        j["dead"] = False
        j.pop("last_error", None)
        # last_ok_at 不伪造成「刚才成功过」——
        # 这一轮是探测，没有真的入库。留空让下一轮正常抓取时写。
        j["last_probe_recovered_at"] = S.iso()
        io.open(f, "w", encoding="utf-8").write(
            json.dumps(j, ensure_ascii=False, indent=1))
        n1 += 1

    for f, j, c, n in empty:
        # 状态码是 200，错误描述要改成准确的说法。
        # 留着「HTTP 0」会误导成网络问题。
        j["last_error"] = "解析无条目"
        io.open(f, "w", encoding="utf-8").write(
            json.dumps(j, ensure_ascii=False, indent=1))
        n2 += 1

    print("  已放回调度 %d 家 ｜ 已更正错误描述 %d 家" % (n1, n2))
    return 0


if __name__ == "__main__":
    sys.exit(main())