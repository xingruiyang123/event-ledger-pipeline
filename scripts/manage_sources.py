"""信源三态管理与入口质量诊断。

用户定的原则（2026-11-20）
------------------------
「排除某个采集入口，不等于永久删除对应的信息来源。」

三种状态：
  ACTIVE        正常采集
  RECONFIGURE   需要重新配置栏目、RSS 或频率（技术问题，源本身有价值）
  EXCLUDED      当前采集策略下不参与自动抓取

这个区分很要紧：EXCLUDED 容易被误读成"这个源没用"，
而实际上多数情况是"这个入口配置得不对"。把两者混为一谈，
会因为技术问题丢掉有价值的信源。

本脚本做两件事
--------------
一、按域名诊断入口质量，给出建议状态
    从原始层的抓取记录反推：某域名下有多少条候选、成功多少、失败什么码
    失败率高且失败码集中在 403/429 → RECONFIGURE（反爬，要换入口或加延时）
    失败码集中在 404 → RECONFIGURE（入口是旧 sitemap）
    域名本身是占位/异常 → EXCLUDED
    内容与本项目无关（讣告、体育、财经）→ 加进候选排除规则，而非排除源

二、把状态写回信源主表
"""
import argparse
import glob
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

# 明显与本项目无关的内容路径。这类要排除的是**内容**，不是源 ——
# 同一家媒体的讣告版和要闻版价值完全不同。
JUNK_PATH = re.compile(
    r"/(obituar|obituary|obituaries|death-notice|funeral|"
    r"sports?|sport/|football|cricket|basketball|soccer|"
    r"entertainment|celebrity|movie|music|tv-shows?|"
    r"lifestyle|fashion|food|recipe|travel|horoscope|"
    r"business/markets?|stock|forex|crypto|"
    r"classifieds?|jobs?/|careers?/|real-?estate|"
    r"weather|lottery|crossword|puzzle)(/|$|\?)", re.I)

# 占位/异常域名
BAD_HOST = re.compile(
    r"^(default|localhost|example|test|demo|invalid|placeholder)"
    r"|\.(local|invalid|test|example)$", re.I)


def host_of(u):
    try:
        return str(u).split("/")[2].lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def diagnose(store_root):
    """从原始层反推每个域名的入口质量。"""
    import scheduler as S

    # 候选池：从 feed 快照重算，与 fetch_articles 同逻辑
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fetch_articles as F
    from raw_store import RawStore

    st = RawStore(store_root)
    cands, n_feed, n_entry = F.gather_candidates(st)

    # 已抓到的正文 URL
    have = set()
    for mp in glob.glob(os.path.join(st.root, "raw/meta", "*", "*.json")):
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") == "article":
            have.add(m.get("url"))

    by_host = defaultdict(lambda: {"cand": 0, "have": 0, "junk": 0,
                                   "sample": []})
    for c in cands:
        h = host_of(c["url"])
        if not h:
            continue
        d = by_host[h]
        d["cand"] += 1
        if c["url"] in have:
            d["have"] += 1
        if JUNK_PATH.search(c["url"]):
            d["junk"] += 1
        if len(d["sample"]) < 3:
            d["sample"].append(c["url"])
    return by_host, len(cands), n_feed, n_entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=r"D:\event-ledger-data")
    ap.add_argument("--registry", default=None, help="信源主表，给了就写回状态")
    ap.add_argument("--out", default=None, help="诊断结果 json")
    ap.add_argument("--apply", action="store_true",
                    help="真的写回主表（默认只诊断不写）")
    a = ap.parse_args()

    by_host, n_cand, n_feed, n_entry = diagnose(a.store)
    print("")
    print("  候选 %d 条（来自 %d 个 feed 快照、%d 条目录条目）"
          % (n_cand, n_feed, n_entry))
    print("  涉及域名 %d 个" % len(by_host))
    print("")

    rows = []
    for h, d in by_host.items():
        junk_ratio = d["junk"] / max(1, d["cand"])
        grab_ratio = d["have"] / max(1, d["cand"])
        status, why = "ACTIVE", ""
        if BAD_HOST.search(h):
            status, why = "EXCLUDED", "占位/异常域名"
        elif junk_ratio >= 0.5:
            status, why = "ACTIVE", "内容无关（靠路径过滤，不排除源）"
        elif d["cand"] >= 5 and grab_ratio == 0:
            status, why = "RECONFIGURE", "候选 %d 条一条没抓到，需换入口" % d["cand"]
        elif d["cand"] >= 5 and grab_ratio < 0.3:
            status, why = "RECONFIGURE", "抓到率仅 %.0f%%，需换入口或加延时" % (grab_ratio * 100)
        rows.append({"domain": h, "candidates": d["cand"],
                     "fetched": d["have"], "junk": d["junk"],
                     "grab_rate": round(grab_ratio, 3),
                     "suggest": status, "reason": why,
                     "sample": d["sample"]})

    rows.sort(key=lambda x: (-x["candidates"], x["domain"]))
    cnt = Counter(r["suggest"] for r in rows)
    print("  建议状态分布:")
    for k in ("ACTIVE", "RECONFIGURE", "EXCLUDED"):
        print("    %-12s %d 个域名" % (k, cnt.get(k, 0)))
    print("")

    print("  ── 需要重新配置的（源有价值，入口不对）──")
    for r in [x for x in rows if x["suggest"] == "RECONFIGURE"][:15]:
        print("    %-32s 候选 %3d ｜ 抓到 %3d (%.0f%%) ｜ %s"
              % (r["domain"][:30], r["candidates"], r["fetched"],
                 r["grab_rate"] * 100, r["reason"][:28]))
        if r["sample"]:
            print("        %s" % r["sample"][0][:76])
    print("")

    print("  ── 内容无关的（靠路径过滤，源保留）──")
    for r in [x for x in rows if x["junk"] > 0][:10]:
        print("    %-32s 候选 %3d ｜ 其中无关 %3d (%.0f%%)"
              % (r["domain"][:30], r["candidates"], r["junk"],
                 100.0 * r["junk"] / max(1, r["candidates"])))
    print("")

    if a.out:
        io.open(a.out, "w", encoding="utf-8").write(
            json.dumps(rows, ensure_ascii=False, indent=1))
        print("  已写出 → %s" % a.out)

    if a.registry and a.apply:
        import openpyxl
        wb = openpyxl.load_workbook(a.registry, data_only=True)
        ws = wb[max(wb.sheetnames, key=lambda s: wb[s].max_row)]
        data = [list(r) for r in ws.iter_rows(values_only=True)]
        hdr = [str(x or "").strip() for x in data[0]]
        ix = {x: i for i, x in enumerate(hdr)}
        if "状态" not in ix:
            print("  ! 主表没有「状态」列，无法写回")
            return 1
        sug = {r["domain"]: r["suggest"] for r in rows}
        n = 0
        for r in data[1:]:
            if not r or not r[ix.get("主域名", 0)]:
                continue
            d = str(r[ix["主域名"]]).strip().lower().replace("www.", "")
            if d in sug and sug[d] == "RECONFIGURE":
                r[ix["状态"]] = "RECONFIGURE"
                r[ix.get("问题标注", -1) if "问题标注" in ix else -1] = \
                    "入口需重配（抓到率低）"
                n += 1
        wb2 = openpyxl.Workbook()
        ws2 = wb2.active
        ws2.title = ws.title
        ws2.append(hdr)
        for r in data[1:]:
            ws2.append(r)
        ws2.freeze_panes = "A2"
        wb2.save(a.registry)
        print("  已写回主表：%d 家标为 RECONFIGURE" % n)

    return 0


if __name__ == "__main__":
    sys.exit(main())
