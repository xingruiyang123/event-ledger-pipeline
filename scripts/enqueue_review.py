#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给每个事件生成待核验队列：先查哪些、用什么检索式、查到什么算通过。

为什么需要排队
--------------
合并后 14972 个单起事件里，96% 只有一个源记录。要给每条都配上可点击的新闻链接，
按每条 2 秒算要 8 小时 —— 不现实。

但优先级差别极大：

    P0 多源+重伤亡     45 条   两个库都记了，只差一个链接
    P1 多源+低伤亡    516 条   同上，但事件小
    P2 单源+重伤亡     45 条   只有一源，但死人多，值得核
    P3 单源+中伤亡    256 条
    P4 单源+低伤亡   5058 条   ← 这里开始性价比骤降
    P5 单源+零死亡   9052 条   ← 占 60%，大多是抗议/小冲突

先做 P0+P1（561 条，约 20 分钟）就能覆盖全部「多源印证」事件的可回溯性。
这是投入产出比最高的一刀。

检索式怎么给
------------
每条事件预生成 2–3 条检索式，供 `enrich_urls.py` 直接使用：

  1. 媒体名 + 标题（最准，但依赖采集端给了 [src]）
  2. "国家 地点 日期 事件类型"（媒体名缺失时用）
  3. site: 限定到已知报过该地区的媒体（提高命中）

检索式要短。实测超过 12 个词的查询在多数检索后端上召回明显下降。

用法
----
  # 生成全部队列
  python scripts/enqueue_review.py --in 合并事件.jsonl --out 待核验队列.jsonl

  # 只要 P0+P1（推荐先跑这批）
  python scripts/enqueue_review.py --in 合并事件.jsonl --out 待核验队列.jsonl \\
      --max-priority P1

  # 只看排了多少，不写文件
  python scripts/enqueue_review.py --in 合并事件.jsonl --dry-run
"""
import argparse
import io
import json
import os
import re
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 事件类型 → 英文检索词（检索后端对英文召回更好）
ACT_WORD = {
    "自杀式袭击": "suicide bombing",
    "爆炸": "bomb blast",
    "枪击": "shooting",
    "持刀": "stabbing",
    "绑架": "abduction",
    "纵火": "arson",
    "车辆冲撞": "ramming attack",
    "伏击": "ambush",
    "空袭": "airstrike",
    "袭击": "attack",
    "武装冲突": "clashes",
    "针对平民暴力": "attack civilians",
}


def priority_of(r):
    """返回 (档位字符串, 排序分)。分数越大越该先查。"""
    d = r.get("deaths") or 0
    multi = (r.get("n_sources") or 1) >= 2
    if multi and d >= 10:
        return "P0", 100000 + d
    if multi:
        return "P1", 50000 + d
    if d >= 25:
        return "P2", 10000 + d
    if d >= 10:
        return "P3", 5000 + d
    if d >= 1:
        return "P4", 1000 + d
    return "P5", d


def clean(s, n=70):
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    # UCDP 的摘要常以「On 3 August 2025, 」开头，检索时会浪费词
    t = re.sub(r"(?i)^on\s+\d{1,2}\s+\w+\s+\d{4}\s*,?\s*", "", t)
    t = re.sub(r"(?i)^around\s+\d{1,2}\s+\w+\s+\d{4}\s*,?\s*", "", t)
    t = re.sub(r"\[src\][^\u4e00-\u9fff]{0,80}$", "", t)
    return t[:n].strip()


def build_queries(r):
    """给一条事件生成 2–3 条检索式。"""
    qs = []
    cty = str(r.get("country") or "").strip()
    place = str(r.get("place") or "").strip()
    date = str(r.get("date") or "")[:10]
    act = ACT_WORD.get(str(r.get("act_type") or ""), "")
    summ = clean(r.get("summary"))

    # 采集端给的媒体名/标题。注意 ACLED 的 source 是分号拼的多个媒体名：
    #   「Xinhua News Agency;BBC Monitoring Report;AllAfrica;Goobjoog News」
    # 直接拿去检索会得到一条没有信息量的「媒体名串烧」。
    # 拆开后取第一个像媒体名的，和结构化特征拼起来用。
    hints = r.get("source_hints") or []
    for h in hints[:1]:
        body = str(h).split(":", 1)[-1]
        parts = [x.strip() for x in re.split(r"[;；]", body) if x.strip()]
        media = ""
        for x in parts:
            # 排除社媒账号、纯标签、纯地名
            if x.startswith("@") or x.startswith("#") or len(x) < 5:
                continue
            if re.match(r"^[\W\d]+$", x):
                continue
            media = x
            break
        if media:
            tail = " ".join([x for x in [act, cty] if x])
            q = (media[:60] + " " + tail).strip()
            if len(q) > 14:
                qs.append(q)

    # 结构化特征
    bits = [x for x in [cty, place, date, act] if x]
    if bits:
        qs.append(" ".join(bits)[:110])

    # 摘要前 70 字（摘要本身通常就是标题）
    if summ and len(summ) > 15:
        qs.append(summ)

    # 去重 + 去太短的
    out, seen = [], set()
    for q in qs:
        k = re.sub(r"\W+", "", q).lower()[:40]
        if not k or k in seen or len(q) < 10:
            continue
        seen.add(k)
        out.append(q)
    return out[:3]


def main():
    ap = argparse.ArgumentParser(description="生成待核验队列（分级 + 预生成检索式）")
    ap.add_argument("--in", dest="inp", required=True, help="cross_verify.py 输出的合并事件表")
    ap.add_argument("--out", default="待核验队列.jsonl")
    ap.add_argument("--max-priority", default="P5",
                    choices=["P0", "P1", "P2", "P3", "P4", "P5"],
                    help="只输出到这一档（含）。先跑 P1 = 全部多源印证事件")
    ap.add_argument("--limit", type=int, default=0, help="最多输出多少条（0=不限）")
    ap.add_argument("--skip-aggregate", action="store_true", default=True,
                    help="跳过累计/汇总条目（默认开）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.inp):
        print("找不到：%s" % a.inp, file=sys.stderr)
        return 1

    rows = []
    with io.open(a.inp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    print("读入 %d 条" % len(rows))

    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5}
    cut = order[a.max_priority]

    queue = []
    dist = Counter()
    for r in rows:
        if a.skip_aggregate and r.get("is_aggregate"):
            continue
        p, score = priority_of(r)
        dist[p] += 1
        if order[p] > cut:
            continue
        r2 = dict(r)
        r2["priority"] = p
        r2["_score"] = score
        r2["queries"] = build_queries(r)
        r2["review_status"] = "pending"
        queue.append(r2)

    queue.sort(key=lambda x: -x["_score"])
    if a.limit:
        queue = queue[:a.limit]

    print()
    print("全量分档：")
    for k in sorted(dist):
        mark = "  ← 本次输出到此" if order[k] == cut else ""
        print("  %s %6d 条%s" % (k, dist[k], mark))
    print()
    print("本次输出：%d 条" % len(queue))
    with_q = sum(1 for x in queue if x["queries"])
    print("其中生成了检索式的：%d 条（%.0f%%）" % (with_q, 100.0 * with_q / len(queue) if queue else 0))

    if a.dry_run:
        print()
        for x in queue[:8]:
            print("  [%s] %s %s 死%s ｜ %s"
                  % (x["priority"], x.get("country"), x.get("date"),
                     x.get("deaths"), str(x.get("summary"))[:52]))
            for q in x["queries"]:
                print("        Q: %s" % q[:96])
        return 0

    d = os.path.dirname(os.path.abspath(a.out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        for x in queue:
            x.pop("_score", None)
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    print()
    print("已写出 → %s" % a.out)
    print()
    print("下一步：用预生成检索式批量补链接")
    print("  python scripts/enrich_urls.py --in %s --out 带链接.json \\" % a.out)
    print("      --search-cmd 'D:/DSH/sx.cmd search \"{q}\" --task auto --json' \\")
    print("      --budget-seconds 1800")
    print()
    print("注意：enrich_urls 用的是它自己从字段里拼的检索式。")
    print("      本文件里的 queries 是给人工/其他工具参考的分级清单。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
