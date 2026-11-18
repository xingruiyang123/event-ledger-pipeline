#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨源交叉核验：把多个源的事件合并成「独立事件表」，并标出每条被几个源印证。

为什么需要这一步
----------------
UCDP 和 ACLED 都没有单条事件的公开 URL，只有「媒体名 + 日期 + 摘要」。
单看任何一个源，读者无法判断这条是不是真的。但两个源都记了同一件事，
可信度就完全不同 —— 这是唯一能自证「这条不是我们编的」的办法。

实测 2025-08：UCDP 2361 条里 50.6% 能在 ACLED 找到，ACLED 16999 条里 21.8%
能在 UCDP 找到。也就是说约一半的 UCDP 事件有双源印证，另一半只有单源。

三种印证状态，报告里必须分开写
------------------------------
  双源印证   两个或以上源都记了同一事件 → 可信度最高
  单源记录   只有一个源记了 → 需要人工核验或补链接
  三源印证   两个库 + 新闻都记了 → 最可靠

匹配方法
--------
坐标优先（两源 100% 有经纬度）：10 公里内 + 同日±1 视为同一事件。
实测中位距离 0.8km，抽查 6 对全是同一事件。
没有坐标时才退回文字相似度，阈值 0.30。

为什么不用文字相似度做主力：实测 1678 对候选里只有 3.3% 的文字相似度超过 0.25。
UCDP 写「Army: an officer and a soldier were killed」，ACLED 写
「On 28 August 2025, an Israeli drone exploded during inspection」——
同一件事，两种叙述风格，字面几乎不重合。

用法
----
  python scripts/cross_verify.py \\
      --src ucdp=ucdp.jsonl --src acled=acled.jsonl \\
      --out 合并事件.jsonl --report 交叉核验.md

  # 也可以只给一个源（那就是全部标成单源）
  python scripts/cross_verify.py --src ucdp=u.jsonl --out merged.jsonl
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from audit_coverage import (norm_country, norm_date, day_num, to_int,
                                haversine, sim)
except Exception as e:  # noqa: BLE001
    print("需要同目录的 audit_coverage.py：%s" % e, file=sys.stderr)
    sys.exit(1)


# 累计/汇总条目：不是单起事件，不能计入条数与死亡统计。
# 实测 2025-08 UCDP 里有 3 条这类记录：
#   「Russia's losses in the war 01.08.2025 - 20.08.2025」deaths=3682
#   「Russia's losses in the war 21.08.2025 - 20.10.2025」deaths=7161
#   「Frontline report: ...」deaths=7986
# 不识别就会被当成「一起造成 7000 人死亡的袭击」写进台账。
# 只留高置信的累计表述。早前版本把 as of / timeline / during the month 也算进来，
# 误报 98 条 —— 「On 2 August 2025, five children were killed」这种具体事件
# 也被判成累计条目。宁可漏判，也不能把真实事件当汇总删掉。
AGGREGATE_RE = re.compile(
    r"(?i)(losses in the war|total (?:combat )?losses|战斗损失合计|"
    r"cumulative (?:toll|total|deaths)|"
    r"monthly (?:report|total|summary|roundup)|月度统计|月度汇总|"
    r"timeline \(terrorist|terrorist activities\)|恐怖活动时间线|"
    r"全年统计|年度统计|累计伤亡|累计击毙)")

# 摘要缺失或只有日期（UCDP 偶尔这样），无法判断内容，标出来
THIN_RE = re.compile(r"^[\d\s年月日.,/\-]*$")


def is_aggregate(rec):
    """只看 summary，不看 notes。

    notes 是 UCDP 的旁注，常引用别的数据源标签，
    实测「1 cop, 3 terrorists killed as militants ambush police checkpost」
    因为 notes 里带了一句 "Timeline (Terrorist Activities)" 被误判成汇总条目。
    判断一条记录本身是不是汇总，只能看它自己的摘要。
    """
    t = str(rec.get("summary") or "")
    # UCDP 常把多条新闻用分号拼在一个 summary 里：
    #   「1 cop, 3 terrorists killed as militants ambush police checkpost;
    #     Pakistan: Timeline (Terrorist Activities)-2025」
    # 前半是具体事件，后半才是汇总标签。只看第一段，避免把真实事件判成汇总。
    first = t.split(";")[0].split("｜")[0]
    return bool(AGGREGATE_RE.search(first))


def load(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return rows


def prep(rows, src_name):
    out = []
    for r in rows:
        ex = r.get("extra") or {}
        lat, lon = ex.get("lat"), ex.get("lon")
        try:
            lat = float(lat) if lat is not None else None
            lon = float(lon) if lon is not None else None
        except Exception:  # noqa: BLE001
            lat = lon = None
        d = norm_date(r.get("date"))
        dn = day_num(d)
        if dn is None:
            continue
        out.append({
            "aggregate": is_aggregate(r),
            "src": src_name,
            "cty": norm_country(r.get("country")),
            "date": d, "day": dn,
            "deaths": to_int(r.get("deaths")) or 0,
            "lat": lat, "lon": lon,
            "rec": r,
            # 每条记录自己带的来源线索，合并时留给主记录
            "hint": (ex.get("acled_source") or r.get("source_office")
                     or (ex.get("source_office")) or ""),
        })
    return out


def same_event(a, b, max_km=10.0, min_sim=0.30):
    if a["cty"] != b["cty"]:
        return False
    if abs(a["day"] - b["day"]) > 1:
        return False
    dkm = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    if dkm is not None:
        return dkm <= max_km
    return sim(a["rec"].get("summary"), b["rec"].get("summary")) >= min_sim


def cluster(items):
    """把跨源记录聚成独立事件。用并查集，避免链式合并把一整片战区并成一条。"""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    # 按国家 + 日期分桶，只比同桶的，避免 O(n²)
    from collections import defaultdict
    bucket = defaultdict(list)
    for i, it in enumerate(items):
        bucket[it["cty"]].append(i)

    for cty, idxs in bucket.items():
        idxs.sort(key=lambda i: items[i]["day"])
        for p in range(len(idxs)):
            i = idxs[p]
            for q in range(p + 1, len(idxs)):
                j = idxs[q]
                if items[j]["day"] - items[i]["day"] > 1:
                    break
                # 只在「跨源」时合并。同源内部不合并 ——
                # ACLED 在乌克兰一天有几十条记录，10km 内会把整片战区并成一条
                # （实测不加这条限制：19360 条记录被并成 7113 个事件，过度合并）。
                # 同源的多条就是多个事件，这是源本身的粒度，不该由我们改。
                if items[i]["src"] == items[j]["src"]:
                    continue
                if same_event(items[i], items[j]):
                    union(i, j)

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def merge_group(items, idxs):
    """把一个簇合成一条事件记录。主记录选信息来源最全的那条。"""
    recs = [items[i] for i in idxs]
    srcs = sorted({r["src"] for r in recs})

    # 主记录：优先选有媒体名、摘要最长的
    def score(r):
        return (1 if r["hint"] else 0, len(str(r["rec"].get("summary") or "")))
    main = max(recs, key=score)
    out = dict(main["rec"])

    out["source"] = "+".join(srcs)
    out["verify"] = ("多源印证" if len(srcs) >= 2 else "单源记录")
    out["n_sources"] = len(srcs)

    # 各源的原始 id 都留着，便于回到库里查
    ids = []
    hints = []
    for r in recs:
        rid = (r["rec"].get("raw_id") or
               ((r["rec"].get("extra") or {}).get("acled_event_id")) or
               ((r["rec"].get("extra") or {}).get("ged_id")) or "")
        if rid:
            ids.append("%s:%s" % (r["src"], rid))
        if r["hint"]:
            hints.append("%s:%s" % (r["src"], str(r["hint"])[:80]))
    out["source_ids"] = ids
    out["source_hints"] = hints
    out["cluster_size"] = len(recs)

    # 死亡数用主记录的值，**不能取 max**。
    # 取 max 的后果：一条坏数据（比如把累计伤亡 7161 记成单起事件）
    # 会把整簇的死亡数放大到几千（实测乌克兰 7986 就是这么来的）。
    # 各源原值留在 deaths_by_source 里，谁跟谁差多少一目了然。
    out["deaths"] = main["deaths"]
    out["deaths_by_source"] = {r["src"]: r["deaths"] for r in recs}
    out["deaths_spread"] = (max(r["deaths"] for r in recs) -
                            min(r["deaths"] for r in recs)) if recs else 0

    # 累计/汇总条目单独标记，不进事件统计
    out["is_aggregate"] = any(r.get("aggregate") for r in recs)

    # 日期取最早的（事发日优先于报道日）
    dates = sorted(r["date"] for r in recs)
    out["date"] = dates[0] if dates else out.get("date", "")
    return out


def main():
    ap = argparse.ArgumentParser(description="跨源交叉核验与事件合并")
    ap.add_argument("--src", action="append", default=[],
                    help="name=path，可给多个")
    ap.add_argument("--out", default="合并事件.jsonl", help="合并后的事件表")
    ap.add_argument("--report", default=None, help="markdown 核验报告")
    ap.add_argument("--max-km", type=float, default=10.0, help="坐标匹配阈值（km）")
    ap.add_argument("--min-sim", type=float, default=0.30, help="无坐标时的文字相似度阈值")
    a = ap.parse_args()

    if not a.src:
        print("至少给一个 --src name=path", file=sys.stderr)
        return 1

    items = []
    sizes = {}
    for spec in a.src:
        if "=" not in spec:
            print("--src 要写成 name=path：%s" % spec, file=sys.stderr)
            return 1
        name, path = spec.split("=", 1)
        rows = load(path)
        if not rows:
            print("⚠️  %s 为空或不存在：%s" % (name, path), file=sys.stderr)
            continue
        sizes[name] = len(rows)
        items.extend(prep(rows, name))
        print("%-10s %6d 条 → 可参与比对 %d" % (name, len(rows), len(prep(rows, name))))

    if not items:
        print("没有可用数据", file=sys.stderr)
        return 1

    print("\n聚类中（同国 + 同日±1 + 坐标 %.0fkm 内）…" % a.max_km)
    groups = cluster(items)
    print("得到 %d 个独立事件（输入 %d 条记录）" % (len(groups), len(items)))

    merged = [merge_group(items, g) for g in groups]
    merged.sort(key=lambda r: (-(r["n_sources"]), -(r["deaths"] or 0)))

    agg = [r for r in merged if r.get("is_aggregate")]
    if agg:
        print()
        print("  ⚠️  识别出 %d 条累计/汇总条目（不是单起事件，已单独标记）：" % len(agg))
        for r in agg[:5]:
            print("     %s %s 死%s ｜ %s" % (r.get("country"), r.get("date"),
                                          r.get("deaths"),
                                          str(r.get("summary") or "")[:56]))
    st = Counter(r["verify"] for r in merged)
    print()
    for k, v in st.most_common():
        print("  %-8s %6d 条（%.1f%%）" % (k, v, 100.0 * v / len(merged)))

    d = os.path.dirname(os.path.abspath(a.out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print()
    print("已写出 → %s" % a.out)

    if a.report:
        lines = ["# 交叉核验报告", ""]
        lines.append("## 一、输入")
        lines.append("")
        lines.append("| 数据源 | 记录数 |")
        lines.append("|---|---|")
        for k, v in sizes.items():
            lines.append("| %s | %d |" % (k, v))
        lines.append("")
        lines.append("## 二、合并结果")
        lines.append("")
        lines.append("输入 %d 条记录 → **%d 个独立事件**" % (len(items), len(merged)))
        lines.append("")
        lines.append("| 印证状态 | 条数 | 占比 |")
        lines.append("|---|---|---|")
        for k, v in st.most_common():
            lines.append("| %s | %d | %.1f%% |" % (k, v, 100.0 * v / len(merged)))
        lines.append("")
        lines.append("## 三、多源印证事件（可信度最高）")
        lines.append("")
        multi = [r for r in merged if r["n_sources"] >= 2]
        for r in multi[:25]:
            lines.append("- **%s %s** 死%s ｜ %s ｜ 源：%s"
                         % (r.get("country"), r.get("date"), r.get("deaths"),
                            str(r.get("summary") or "")[:64], r["source"]))
        if len(multi) > 25:
            lines.append("- … 另有 %d 条" % (len(multi) - 25))
        lines.append("")
        lines.append("## 四、单源事件（需人工核验或补链接）")
        lines.append("")
        solo = [r for r in merged if r["n_sources"] < 2]
        for r in solo[:25]:
            lines.append("- %s %s 死%s ｜ %s ｜ 源：%s"
                         % (r.get("country"), r.get("date"), r.get("deaths"),
                            str(r.get("summary") or "")[:64], r["source"]))
        if len(solo) > 25:
            lines.append("- … 另有 %d 条" % (len(solo) - 25))
        lines.append("")
        lines.append("## 五、怎么用这两份清单")
        lines.append("")
        lines.append("- 多源印证：可以直接进台账，并在「备注」写 `[印证]源A+源B`")
        lines.append("- 单源记录：不能直接当定论。要么补新闻链接（`enrich_urls.py`），"
                     "要么在交付说明里标「单源，待核」")
        lines.append("- 无论哪一种，`source_ids` 里都留着各源的原始事件 ID，"
                     "可以回到 UCDP / ACLED 官网逐条查")
        text = "\n".join(lines)
        dd = os.path.dirname(os.path.abspath(a.report))
        if dd and not os.path.isdir(dd):
            os.makedirs(dd, exist_ok=True)
        with io.open(a.report, "w", encoding="utf-8") as f:
            f.write(text)
        print("核验报告 → %s" % a.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
