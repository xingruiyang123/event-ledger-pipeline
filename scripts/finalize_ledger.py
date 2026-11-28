"""把判定结果出成《判定与编码规范》要求的完整台账。

规范要求的字段，一个都不能少。这个脚本负责补上判定链没覆盖的三列：

  序号    10 位，yyyymmdd + 2 位当天顺序
  事件ID  12 位，yyyymmdd + 4 位当天顺序
  事件类型 六类（1 确定属于 / 2 与安全部队交火 / 3 施害者不明 /
          4 武装组织交战 / 5 确定不属于 / 6 其它）

以及把人数列修回规范允许的值域：
  数字 / 0 / 数人 / 不详
「0」是「明确未造成伤亡」，「空」是「不知道」，两者不能混。

输入：judge_events.py 的 --out（计入）/ --out-manual（需人工判）
输出：规范台账 csv
"""
import argparse
import csv
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 规范 4.1 节：序号 10 位、事件ID 12 位，都是 yyyymmdd + 当天顺序
# 规范 2.10 节：事件类型六类
EVENT_TYPES = {
    "1": "确定属于恐怖袭击事件",
    "2": "恐怖分子与安全部队交火",
    "3": "因袭击者身份不明而无法确定",
    "4": "武装组织之间的交战",
    "5": "确定不属于恐怖袭击",
    "6": "其它",
}

# 规范 4.5 节允许的非数字值
UNKNOWN = "不详"
SEVERAL = "数人"


def _day(rec):
    d = str(rec.get("年份") or "")
    m = rec.get("月份")
    dd = rec.get("日期")
    if not (d and m and dd):
        return ""
    try:
        return "%04d%02d%02d" % (int(d), int(m), int(dd))
    except Exception:  # noqa: BLE001
        return ""


def _casualty(rec, key):
    """人数列：数字 / 0 / 数人 / 不详。

    规范 4.5 节：
      · 明确未造成伤亡 → 0
      · 有证据但人数不明，或只写「数人/多人」 → 数人
      · 绑架未报且无后续 → 不详
      · 「至少 X」「Y 人左右」 → X / Y
      · 区间 → 取最低值

    判定链把「数人」「不详」转成了 None 并写进 note，
    这里从 note 里把它们读回来 —— 不能因为要数字就把信息抹掉。
    """
    v = rec.get(key)
    note = str(rec.get("备注") or "")
    if isinstance(v, int):
        return v
    # note 里带「死亡人数：数人」这种
    label = "死亡人数" if "死亡" in key else "受伤人数"
    m = re.search(label + r"[：:]\s*([^\s；;]+)", note)
    if m:
        raw = m.group(1).strip()
        if raw in (UNKNOWN, SEVERAL, "多人", "若干"):
            return raw
    if v is None:
        # 有伤亡描述但抽不出数 → 数人；完全没有 → 留空（不知道）
        return ""
    s = str(v).strip()
    m = re.search(r"(\d{1,6})", s.replace(",", ""))
    return int(m.group(1)) if m else ""


def _events_of(rec, default="1"):
    """事件类型：按它是从哪个文件读出来的定。

    judge_events.py 的 to_row 把「审核状态」写死成「待审核」，
    不能靠它反推。但 judge 已经按结论分了文件：
      --out         计入        → 事件类型 1（确定属于恐怖袭击事件）
      --out-manual  需人工判    → 事件类型 3（因袭击者身份不明无法确定）
      --out-keep    保留作对照  → 事件类型 2/4（交战类，不进统计）
    所以调用方给 default 就行，这里不再自己判一遍。

    判定标准只有一处（judge_events.py），改也只改那里。
    """
    return default


def build(rows, etype_map=None):
    """按规范排定序号与事件 ID，并补齐三列。

    etype_map: {id(rec): 事件类型}，由调用方按来源文件定。
    """
    etype_map = etype_map or {}
    buckets = defaultdict(list)
    for r in rows:
        buckets[_day(r)].append(r)

    for day, items in buckets.items():
        # 同一天内按摘要排序，保证同一批数据换输入顺序 ID 不变
        items.sort(key=lambda x: (str(x.get("事件摘要") or ""),
                                  str(x.get("信息来源URL") or "")))
        for i, r in enumerate(items, 1):
            if day:
                r["序号"] = "%s%02d" % (day, i)
                r["事件ID"] = "%s%04d" % (day, i)
            else:
                # 日期不全编不出 ID。规范要求前 8 位是日期，
                # 编不出来就不编，留空等人工补，不要瞎填一个。
                r["序号"] = ""
                r["事件ID"] = ""
            r["事件类型"] = etype_map.get(id(r), "1")
    return rows


# 输出列顺序：先规范字段，再原有 19 列里其余部分
SPEC_COLS = ["序号", "事件ID", "国家", "年份", "月份", "日期", "地点",
             "死亡人数", "受伤人数", "袭击方式", "袭击武器", "袭击者",
             "事件摘要", "事件类型", "备注", "信息来源URL"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True,
                    help="judge_events.py --out（计入）")
    ap.add_argument("--manual", default="",
                    help="judge_events.py --out-manual（需人工判），可选")
    ap.add_argument("--out", required=True)
    ap.add_argument("--include-manual", action="store_true",
                    help="把「需人工判」也并进台账，事件类型填 3")
    a = ap.parse_args()

    rows = json.load(io.open(a.inp, encoding="utf-8"))
    print("")
    print("  计入 %d 条" % len(rows))
    etype = {}
    for r in rows:
        etype[id(r)] = "1"

    if a.include_manual and a.manual and os.path.isfile(a.manual):
        man = json.load(io.open(a.manual, encoding="utf-8"))
        print("  并入需人工判 %d 条" % len(man))
        for r in man:
            etype[id(r)] = "3"
        rows = rows + man

    for r in rows:
        r["死亡人数"] = _casualty(r, "死亡人数")
        r["受伤人数"] = _casualty(r, "受伤人数")

    rows = build(rows, etype)

    cols = SPEC_COLS + [c for c in (rows[0].keys() if rows else [])
                        if c not in SPEC_COLS]
    with io.open(a.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print("")
    print("  → %s（%d 行 × %d 列）" % (a.out, len(rows), len(cols)))
    print("")
    print("  有事件ID %d ｜ 有国家 %d ｜ 有事件类型 %d"
          % (sum(1 for r in rows if r["事件ID"]),
             sum(1 for r in rows if r["国家"]),
             sum(1 for r in rows if r["事件类型"])))
    print("  事件类型分布：%s"
          % dict(Counter(r["事件类型"] for r in rows).most_common()))
    print("  死亡人数值域：%s"
          % dict(Counter(str(r["死亡人数"]) for r in rows).most_common(8)))


if __name__ == "__main__":
    main()
