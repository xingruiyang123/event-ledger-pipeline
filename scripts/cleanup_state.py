"""清理孤儿状态文件 —— 主表里已不存在的源留下的 state/*.json。

为什么会产生孤儿
----------------
状态文件名从「只按域名」改成了「域名 + 入口URL 哈希」（为了支持一个域名
挂多个入口，如 gov.uk 的 5 个定向栏目）。改完之后，旧格式的文件全部留下：

  旧格式  gov.uk.json            （整站入口，已停用）
  新格式  gov.uk_eca80f8c.json   （Home Office 涉恐）

这些孤儿文件不参与调度（主表里没有对应行），但会被监控脚本扫进去，
导致数字虚高：实测状态库报 2172 家，而主表只有 1489 行。

更麻烦的是它们的 ID 环形缓冲还在 —— 会永久屏蔽那批 URL 的重新收录。
所以不能只从监控里过滤掉，要真的挪走。

做法
----
算出主表现在应该有的状态文件名集合，不在集合里的挪到 state/_stale/。
不删 —— 里面的 capacity/id 缓冲还有参考价值，只是不该混在活跃状态里。
"""
import argparse
import glob
import io
import json
import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--state-dir", default=r"D:\event-ledger-data\state")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    import scheduler as S

    rows = S.load_registry(a.registry, None, None)
    want = set()
    for r in rows:
        want.add(S.slug(r["domain"], r.get("feed_url") or "") + ".json")
    print("")
    print("  主表 %d 行 → 应有状态文件 %d 个" % (len(rows), len(want)))

    havel = glob.glob(os.path.join(a.state_dir, "*.json"))
    have = set(os.path.basename(p) for p in havel)
    print("  现有状态文件 %d 个" % len(have))

    orphan = sorted(have - want)
    missing = sorted(want - have)
    print("  孤儿（主表已无对应行） %d 个" % len(orphan))
    print("  缺状态（还没抓过）     %d 个" % len(missing))
    print("")

    # 孤儿里有多少还有实质内容（抓过成功的）
    live = []
    for name in orphan:
        p = os.path.join(a.state_dir, name)
        try:
            j = json.load(io.open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if j.get("last_ok_at") or j.get("n_fetches"):
            live.append((name, j.get("domain"), j.get("n_fetches"),
                         j.get("capacity")))
    if live:
        print("  其中真的抓过数据的有 %d 个（这些的 ID 缓冲会影响收录）：" % len(live))
        for name, dom, nf, cap in live[:10]:
            print("    %-40s %s 抓 %s 次 ｜ 容量 %s"
                  % (name[:38], str(dom)[:20], nf, cap))
        print("")

    if not a.apply:
        print("  只诊断未改动。加 --apply 才真正挪走。")
        return 0

    dst = os.path.join(a.state_dir, "_stale")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for name in orphan:
        src = os.path.join(a.state_dir, name)
        if os.path.isfile(src):
            shutil.move(src, os.path.join(dst, name))
            n += 1
    print("  已挪走 %d 个孤儿 → %s" % (n, dst))
    print("  剩余活跃状态文件 %d 个" % len(glob.glob(
        os.path.join(a.state_dir, "*.json"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())