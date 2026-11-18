#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把源数据的可追溯线索写进台账：事件ID、坐标、原始来源。

为什么这一步比补链接更重要
--------------------------
用户要求「每一条数据都可审计可回溯可复核」。前面几轮一直在补 URL，
覆盖率只到 24.4%，因为村级别小事件根本没有可搜到的报道。

但查源数据发现：**ACLED 和 UCDP 每一条都自带可追溯线索，只是台账没写进去。**

| 源 | 自带字段 | 覆盖 |
|---|---|---|
| ACLED | `acled_event_id` / `lat` / `lon` / `acled_source` | 100% |
| UCDP | `raw_id` / `source_citation` | 100% |

这些字段的价值：

  event_id     可以拿回 ACLED 官网/API 反查原始记录，这是**真正的可审计**
  lat/lon      能在地图上定位，也能和别的数据源做空间匹配
  source       原始报道媒体名（"Dawn (Pakistan)"），可据此找原文
  citation     UCDP 给的「媒体,日期,标题」三元组，可直接当检索式

**有这些线索的行，即使没有 URL，也是可追溯的。** 区别在于：
URL 是「一步点到」，event_id 是「两步查到」——但后者对 100% 的行都成立。

做法
----
不动 19 列表头，把线索压进「备注」列，格式紧凑：

  [ID]ACLED:MEX100025 ｜ [坐标]19.4326,-99.1332 ｜ [源报]Dawn (Pakistan)

备注已经很长了，所以只在没有 URL 的行加 —— 有 URL 的行优先用 URL。

用法
----
  python scripts/embed_provenance.py --ledger 台账.xlsx --out 台账_带溯源.xlsx \\\\
      --acled acled.jsonl --ucdp ucdp.jsonl
"""
import argparse
import io
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def load(p):
    if not p or not os.path.isfile(p):
        return []
    with io.open(p, encoding="utf-8") as f:
        txt = f.read().strip()
    if not txt:
        return []
    if txt.startswith("["):
        return json.loads(txt)
    return [json.loads(l) for l in txt.split("\n") if l.strip()]


def norm(s, n=60):
    """归一化文本用于匹配：小写、只留字母数字、截断。"""
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(s or "").lower())
    return t[:n]


def build_index(rows, src_name):
    """建三重索引：摘要前缀 / 日期+死亡数 / 日期+地名。"""
    by_sum, by_dd, by_dp = {}, {}, {}
    for r in rows:
        ex = r.get("extra") or {}
        if isinstance(ex, str):
            try:
                ex = json.loads(ex.replace("'", '"'))
            except Exception:  # noqa: BLE001
                ex = {}
        eid = (ex.get("acled_event_id") or r.get("raw_id") or "")
        lat = ex.get("lat") or r.get("latitude") or ""
        lon = ex.get("lon") or r.get("longitude") or ""
        src = (ex.get("acled_source") or r.get("source_citation") or "")
        ds = str(r.get("date") or "")
        de = r.get("deaths")
        try:
            de = int(float(str(de)))
        except Exception:  # noqa: BLE001
            de = 0
        val = (src_name, str(eid), str(lat), str(lon), str(src)[:120])
        s = norm(r.get("summary") or r.get("事件摘要"), 60)
        if s:
            by_sum.setdefault(s, val)
        by_dd.setdefault((ds[:10], de), val)
        pl = norm(r.get("place") or r.get("地点"), 20)
        if pl:
            by_dp.setdefault((ds[:10], pl), val)
    return by_sum, by_dd, by_dp


def main():
    ap = argparse.ArgumentParser(description="把事件ID/坐标/原始来源写进台账")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--acled", default=None)
    ap.add_argument("--ucdp", default=None)
    ap.add_argument("--only-missing-url", dest="only_missing", action="store_true",
                    default=True,
                    help="只给没有 URL 的行加（默认开）。备注已经很长了")
    ap.add_argument("--all-rows", dest="only_missing", action="store_false",
                    help="所有行都加（有 URL 的也加，备注会更长但溯源更完整）")
    a = ap.parse_args()

    try:
        import openpyxl
    except ImportError:
        print("需要 openpyxl", file=sys.stderr)
        return 1

    idx_sum, idx_dd, idx_dp = {}, {}, {}
    for p, nm in ((a.acled, "ACLED"), (a.ucdp, "UCDP")):
        rows = load(p)
        if not rows:
            continue
        s, d, dp = build_index(rows, nm)
        for k, v in s.items():
            idx_sum.setdefault(k, v)
        for k, v in d.items():
            idx_dd.setdefault(k, v)
        for k, v in dp.items():
            idx_dp.setdefault(k, v)
        print("%s 索引：摘要 %d ｜ 日期+死亡 %d ｜ 日期+地名 %d"
              % (nm, len(s), len(d), len(dp)))

    # 支持 json/jsonl：流水线在「判定后、出表前」调用，那时还没有 xlsx
    if a.ledger.lower().endswith((".json", ".jsonl")):
        recs = load(a.ledger)
        stat = {"有URL跳过": 0, "摘要命中": 0, "日期死亡命中": 0,
                "日期地名命中": 0, "未命中": 0}
        for r in recs:
            if a.only_missing and str(r.get("信息来源URL") or "").startswith("http"):
                stat["有URL跳过"] += 1
                continue
            note = str(r.get("备注") or "")
            if "[ID]" in note:
                continue
            try:
                ds = "%04d-%02d-%02d" % (int(r.get("年份") or 0),
                                         int(r.get("月份") or 0),
                                         int(r.get("日期") or 0))
                de = int(float(str(r.get("死亡人数") or 0)))
            except Exception:  # noqa: BLE001
                continue
            val = None
            s0 = norm(r.get("事件摘要"), 60)
            if s0 and s0 in idx_sum:
                val = idx_sum[s0]
                stat["摘要命中"] += 1
            elif (ds, de) in idx_dd:
                val = idx_dd[(ds, de)]
                stat["日期死亡命中"] += 1
            else:
                pl = norm(r.get("地点"), 20)
                if pl and (ds, pl) in idx_dp:
                    val = idx_dp[(ds, pl)]
                    stat["日期地名命中"] += 1
            if not val:
                stat["未命中"] += 1
                continue
            src_name, eid, lat, lon, src = val
            parts = []
            if eid:
                parts.append("[ID]%s:%s" % (src_name, eid))
            try:
                if lat and lon and abs(float(lat)) <= 90:
                    parts.append("[坐标]%.4f,%.4f" % (float(lat), float(lon)))
            except Exception:  # noqa: BLE001
                pass
            if src:
                parts.append("[源报]%s" % src[:60])
            if parts:
                r["备注"] = (note + " ｜ " + " ｜ ".join(parts)).strip()
        print()
        print("标注结果：")
        for k, v in stat.items():
            print("  %-14s %5d" % (k, v))
        with io.open(a.out, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False)
        print()
        print("已写出 → %s" % a.out)
        return 0

    wb = openpyxl.load_workbook(a.ledger)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}
    i_url, i_note = ix["信息来源URL"], ix["备注"]

    stat = {"有URL跳过": 0, "摘要命中": 0, "日期死亡命中": 0,
            "日期地名命中": 0, "未命中": 0}
    for ridx, r in enumerate(rows[1:], start=2):
        if not r or not r[0]:
            continue
        if a.only_missing and str(r[i_url] or "").startswith("http"):
            stat["有URL跳过"] += 1
            continue
        note = str(r[i_note] or "")
        if "[ID]" in note:
            continue

        try:
            mo = int(r[ix["月份"]] or 0)
            dy = int(r[ix["日期"]] or 0)
            yr = int(r[ix["年份"]] or 0)
            de = int(float(str(r[ix["死亡人数"]] or 0)))
        except Exception:  # noqa: BLE001
            continue
        ds = "%04d-%02d-%02d" % (yr, mo, dy)

        val = None
        s0 = norm(r[ix["事件摘要"]], 60)
        if s0 and s0 in idx_sum:
            val = idx_sum[s0]
            stat["摘要命中"] += 1
        elif (ds, de) in idx_dd:
            val = idx_dd[(ds, de)]
            stat["日期死亡命中"] += 1
        else:
            pl = norm(r[ix["地点"]], 20)
            if pl and (ds, pl) in idx_dp:
                val = idx_dp[(ds, pl)]
                stat["日期地名命中"] += 1

        if not val:
            stat["未命中"] += 1
            continue

        src_name, eid, lat, lon, src = val
        parts = []
        if eid:
            parts.append("[ID]%s:%s" % (src_name, eid))
        try:
            if lat and lon and abs(float(lat)) <= 90:
                parts.append("[坐标]%.4f,%.4f" % (float(lat), float(lon)))
        except Exception:  # noqa: BLE001
            pass
        if src:
            parts.append("[源报]%s" % src[:60])
        if parts:
            ws.cell(ridx, i_note + 1, (note + " ｜ " + " ｜ ".join(parts)).strip())

    print()
    print("标注结果：")
    for k, v in stat.items():
        print("  %-14s %5d" % (k, v))

    wb.save(a.out)
    print()
    print("已写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
