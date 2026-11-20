"""把旧格式状态文件的历史迁到新格式 —— 保住已学到的容量和更新速度。

背景
----
状态文件名从「只按域名」改成「域名 + 入口URL 哈希」，是为了支持
一个域名挂多个入口（gov.uk 的 5 个定向栏目）。

但改完之后所有源的状态都换了文件名，等于**从零重学**：
容量 baseline、日更新量 rate、已见 ID 缓冲全部丢失。

后果不是数据丢失（raw 层按 URL 去重，重抓一遍不会重复入库），
而是：间隔被打回 1 小时探测档 → 1090 个源在几小时内被反复抓；
容量和速度要重新累积几轮才能恢复自适应。这是可避免的白跑。

迁移规则
--------
对每个孤儿（旧格式）文件：
  1. 找主表里主域名相同的行
  2. 恰好 1 行 → 直接迁到该行的新 slug
  3. 多行（如 gov.uk 5 栏目）→ 不迁。旧文件是「整站入口」的历史，
     混给任何一个栏目都不对，留着当 _stale 备查
  4. 0 行（源已从主表移除）→ 挪到 _stale

合并策略（新旧都可能有数据）
--------------------------
  n_fetches    取 max —— 抓取次数是累计事实
  capacity     取新值优先，新值为 0 才用旧值
  rate         取新值优先（EWMA 最近值更准）
  known ids    取并集 —— 已见 ID 只增不减，并集才对
  last_ok_at   取较新的
"""
import argparse
import glob
import io
import json
import os
import shutil
import sys
from collections import defaultdict

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
    by_dom = defaultdict(list)
    for r in rows:
        by_dom[r["domain"]].append(r)

    want = {}
    for r in rows:
        want[S.slug(r["domain"], r.get("feed_url") or "") + ".json"] = r

    have = set(os.path.basename(p) for p in
               glob.glob(os.path.join(a.state_dir, "*.json")))
    orphan = sorted(have - set(want))

    print("")
    print("  主表 %d 行 ｜ 应有状态 %d ｜ 现有 %d ｜ 孤儿 %d"
          % (len(rows), len(want), len(have), len(orphan)))
    print("")

    to_migrate, to_keep, to_stale = [], [], []
    for name in orphan:
        dom = name[:-5]                      # 去掉 .json
        cand = by_dom.get(dom, [])
        if len(cand) == 1:
            to_migrate.append((name, cand[0]))
        elif len(cand) > 1:
            to_keep.append((name, len(cand)))
        else:
            to_stale.append(name)

    print("  可迁移（该域名在主表唯一） %d 个" % len(to_migrate))
    print("  保留待查（该域名在主表多行） %d 个" % len(to_keep))
    print("  转 _stale（主表已无此域名）  %d 个" % len(to_stale))
    print("")

    if not a.apply:
        print("  只诊断未改动。加 --apply 执行。")
        return 0

    n_m = 0
    for name, r in to_migrate:
        old_p = os.path.join(a.state_dir, name)
        new_name = S.slug(r["domain"], r.get("feed_url") or "") + ".json"
        new_p = os.path.join(a.state_dir, new_name)
        try:
            old = json.load(io.open(old_p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        new = {}
        if os.path.isfile(new_p):
            try:
                new = json.load(io.open(new_p, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                new = {}

        merged = dict(old)
        merged.update({k: v for k, v in new.items()
                       if v not in (None, "", [], {})})
        # 累计量取大
        merged["n_fetches"] = max(int(old.get("n_fetches") or 0),
                                  int(new.get("n_fetches") or 0))
        merged["n_new_total"] = max(int(old.get("n_new_total") or 0),
                                    int(new.get("n_new_total") or 0))
        # 新值优先，缺失才回退旧值
        for k in ("capacity", "rate"):
            if not new.get(k):
                merged[k] = old.get(k)
        # 已见 ID 取并集（只增不减）
        known = new.get("known") or []
        if isinstance(known, dict):
            known = list(known)
        oldk = old.get("known") or []
        if isinstance(oldk, dict):
            oldk = list(oldk)
        merged["known"] = list(dict.fromkeys(list(oldk) + list(known)))
        # 时间取较新
        for k in ("last_ok_at", "last_try_at"):
            if (old.get(k) or "") > (new.get(k) or ""):
                merged[k] = old.get(k)
        # 保住新的入口 URL
        merged["feed_url"] = r.get("feed_url") or old.get("feed_url") or ""
        merged["domain"] = r["domain"]

        io.open(new_p, "w", encoding="utf-8").write(
            json.dumps(merged, ensure_ascii=False, indent=1))
        shutil.move(old_p, os.path.join(
            _ensure(os.path.join(a.state_dir, "_migrated")), name))
        n_m += 1

    print("  已迁移 %d 个" % n_m)

    dst = _ensure(os.path.join(a.state_dir, "_stale"))
    n_s = 0
    for name in to_stale:
        src = os.path.join(a.state_dir, name)
        if os.path.isfile(src):
            shutil.move(src, os.path.join(dst, name))
            n_s += 1
    print("  已转 _stale %d 个" % n_s)
    print("  活跃状态文件 %d 个" % len(glob.glob(
        os.path.join(a.state_dir, "*.json"))))
    return 0


def _ensure(p):
    os.makedirs(p, exist_ok=True)
    return p


if __name__ == "__main__":
    sys.exit(main())