"""把抽取层的中文 19 列候选，转成判定器认的 jsonl。

为什么要这一步
--------------
本 skill 有两条采集链：

  链 A（原版）harvest_*.py → 候选.jsonl → judge_events.py → 19 列台账
  链 B（自建）scheduler.py → fetch_articles.py → build_ledger_rows.py → 中文 19 列 CSV

两条链的候选格式不一样：A 用英文键的 jsonl，B 用中文列名的 CSV。
判定器、出表、门禁都只认 A 的格式 —— 所以 B 采到的东西进不了下游。

这个脚本做适配，把 B 接到 A 上。它只做字段改名和类型清洗，
**不做任何判断** —— 判断是 judge_events.py 的事。

字段对应
--------
  country   ← 国家
  place     ← 地点
  summary   ← 事件摘要
  date      ← 年份-月份-日期（补零）
  deaths    ← 死亡人数（数字；「数人」「不详」转 None）
  injured   ← 受伤人数（同上）
  act_type  ← 袭击方式
  actor_a   ← 袭击者
  url       ← 信息来源URL
  note      ← 备注
  extra     ← 正文里的武器线索（给 act_classify 判武器用）

关于 deaths / injured 的两个非数字值
------------------------------------
规范第 2.5 节允许填「不详」「数人」。
但判定器和统计要的是数字，不能拿字符串去做加法。
所以：
  · 数字          → 保留
  · 「数人」      → None，并在 note 里留「死亡人数：数人」
  · 「不详」      → None，并在 note 里留「死亡人数：不详」
  · 0             → 保留 0（规范：明确未造成伤亡记 0）
「None」和「0」不是一回事，别在转换时抹平。
"""
import argparse
import csv
import io
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 这些值不是数字，但也不是「没有数据」—— 要原样带进备注
NON_NUMERIC = {"不详", "数人", "多人", "若干", "几十", "数百", "数千",
               "unknown", "several", "many", "dozens"}


def _int_or_none(v):
    """把人数列转成 int / None，并把非数字值原样返回给调用方写备注。"""
    s = "" if v is None else str(v).strip()
    if not s:
        return None, ""
    if s in NON_NUMERIC or s.lower() in NON_NUMERIC:
        return None, s
    # 「45人」「45 人」「at least 45」
    m = re.search(r"(\d{1,6})", s.replace(",", ""))
    if not m:
        return None, s
    return int(m.group(1)), ""


def _date_of(row):
    y = str(row.get("年份") or "").strip()
    mo = str(row.get("月份") or "").strip()
    d = str(row.get("日期") or "").strip()
    if not (y and mo and d):
        return ""
    try:
        return "%04d-%02d-%02d" % (int(y), int(mo), int(d))
    except Exception:  # noqa: BLE001
        return ""


def convert(row):
    """一行中文 19 列 → 判定器认的一条 rec。"""
    deaths, d_raw = _int_or_none(row.get("死亡人数"))
    injured, i_raw = _int_or_none(row.get("受伤人数"))

    notes = []
    base_note = str(row.get("备注") or "").strip()
    if base_note:
        notes.append(base_note)
    if d_raw:
        notes.append("死亡人数：%s" % d_raw)
    if i_raw:
        notes.append("受伤人数：%s" % i_raw)
    # 抽取层标过的模板地名剔除，要在判定阶段还能看出来
    if "站点模板地名已剔除" in base_note:
        notes.append("地点疑为站点模板，已剔除")

    return {
        "country": str(row.get("国家") or "").strip(),
        "place": str(row.get("地点") or "").strip(),
        "summary": str(row.get("事件摘要") or "").strip(),
        "date": _date_of(row),
        "deaths": deaths,
        "injured": injured,
        "act_type": str(row.get("袭击方式") or "").strip(),
        "actor_a": str(row.get("袭击者") or "").strip(),
        "url": str(row.get("信息来源URL") or "").strip(),
        "note": "；".join(notes),
        # extra 必须是 dict —— 判定器里写的是
        # rec.get("extra", {}).get("conflict", "")，传字符串会 AttributeError。
        # 实测踩过：转成 str 后判定器在第 526 行崩掉。
        #   weapon_text：给 act_classify 判武器用。摘要通常一句话，
        #                写不下「AK-47 和手榴弹」，所以把原备注也塞进来。
        #   conflict：战争语境提示，留给判定器排除国家间冲突
        "extra": {
            "casualty_source_values": {"deaths": d_raw, "injured": i_raw},
            "weapon_text": (str(row.get("事件摘要") or "") + " "
                             + str(row.get("备注") or ""))[:500],
            "conflict": "",
            "source_table": str(row.get("来源表") or "") if "来源表" in row else "",
        },
        "source_kind": "news",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True,
                    help="build_ledger_rows.py 输出的 CSV")
    ap.add_argument("--out", required=True, help="判定器输入 jsonl")
    ap.add_argument("--drop-empty-summary", action="store_true", default=True,
                    help="丢弃摘要为空的行（判定器会全判成需人工，没意义）")
    a = ap.parse_args()

    rows = list(csv.DictReader(io.open(a.inp, encoding="utf-8-sig")))
    out = []
    skipped = 0
    for r in rows:
        rec = convert(r)
        if a.drop_empty_summary and len(rec["summary"]) < 8:
            skipped += 1
            continue
        out.append(rec)

    with io.open(a.out, "w", encoding="utf-8") as f:
        for rec in out:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print("")
    print("  读入 %d 行 ｜ 转出 %d 条 ｜ 摘要过短丢弃 %d"
          % (len(rows), len(out), skipped))
    if out:
        n_d = sum(1 for x in out if isinstance(x["deaths"], int)
                  and x["deaths"] > 0)
        n_i = sum(1 for x in out if isinstance(x["injured"], int)
                  and x["injured"] > 0)
        n_c = sum(1 for x in out if x["country"])
        n_u = sum(1 for x in out if x["url"])
        print("  有国家 %d ｜ 有死亡数(>0) %d ｜ 有受伤数(>0) %d ｜ 有URL %d"
              % (n_c, n_d, n_i, n_u))
    print("  → %s" % a.out)


if __name__ == "__main__":
    main()
