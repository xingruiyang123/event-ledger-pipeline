"""高价值信源事件漏采率 —— 评估 RECONFIGURE 策略有没有损失信息。

用户 2026-11-20 的判断
----------------------
「现在系统有三个数字容易被混淆：入口覆盖 1080/1479 = 73%、正文获取率
 12432/12565 = 98.9%、采集成功率 97.1%。这三个指标都不能直接代表
 全球恐怖主义事件发现率。」

「我建议在 30 天评估中增加一个独立指标：高价值信源事件漏采率。
 例如，人工基准中确认某个事件被 Times of Israel 或 MindaNews 报道，
 而自动系统没有发现，就记录为漏采；如果发现了标题但没有正文，
 则记录为发现成功、内容获取不完整。这样才能真正评估 RECONFIGURE
 策略有没有损失信息。」

本脚本只做这一件事，不给总分。

三种判定（沿用前述口径）
----------------------
  MISSED          基准里有这个事件、系统完全没发现        ← 真漏采
  PARTIAL         发现了、但只有摘要没有全文              ← 发现成功、内容不完整
  FOUND           发现了且有全文                        ← 成功

为什么要分三档而不是二值
------------------------
把 PARTIAL 算成漏采会高估系统缺陷（实际上标题和摘要已经拿到了，
后续人工核实够用）；算成成功会掩盖"正文获取不了"这个真实问题
（没法做字段抽取）。两者必须分开计数，否则 RECONFIGURE 策略的
得失算不清。

输入
----
--baseline  人工基准，xlsx 或 jsonl。需要至少这几列（列名允许别名）：
              事件摘要 / 日期 / 国家 / 来源URL 或 信源
            来源URL 是用来判定"这条报道我们有没有"的锚点。
--store     原始层根目录

输出
----
逐事件判定 + 按信源的漏采率表。
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 高价值信源（用户 2026-11-20 定的 P1 补充档）
HIGH_VALUE = {
    "timesofisrael.com", "mindanews.com",
    # 其余 RECONFIGURE 但价值较高的
    "nytimes.com", "albawaba.com", "thewirenews.in", "ppcms.co.uk",
}


def host_of(u):
    try:
        return str(u).split("/")[2].lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def norm_title(t):
    """标题归一化，用于宽松比对。"""
    t = str(t or "").lower()
    t = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", t)
    return " ".join(t.split())


def load_baseline(path):
    """读人工基准。支持 xlsx / jsonl / csv。"""
    rows = []
    low = path.lower()
    if low.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb[max(wb.sheetnames, key=lambda s: wb[s].max_row)]
        data = [list(r) for r in ws.iter_rows(values_only=True)]
        hdr = [str(x or "").strip() for x in data[0]]

        def col(*names):
            for n in names:
                if n in hdr:
                    return hdr.index(n)
            return None

        ci = {
            "summary": col("事件摘要", "事件", "摘要", "title", "summary"),
            "date": col("日期", "date"),
            "country": col("国家", "country"),
            "url": col("信息来源URL", "来源URL", "url", "link"),
            "source": col("信源", "信息来源", "source", "媒体"),
        }
        for r in data[1:]:
            if not r or all(x is None or str(x).strip() == "" for x in r):
                continue
            rec = {}
            for k, i in ci.items():
                rec[k] = str(r[i] or "").strip() if i is not None else ""
            rows.append(rec)
    else:
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            rows.append({
                "summary": d.get("事件摘要") or d.get("summary") or "",
                "date": str(d.get("日期") or d.get("date") or ""),
                "country": d.get("国家") or d.get("country") or "",
                "url": d.get("信息来源URL") or d.get("url") or "",
                "source": d.get("信源") or d.get("source") or "",
            })
    return rows


def build_index(store_root):
    """把原始层里所有文章建成可检索的索引。

    两类证据都要：
      全文（raw/meta 里 kind=article 且非 failed）
      摘要（extracted/search_snippets.jsonl）
    """
    by_url = {}          # url -> "full" / "snippet"
    by_title = []        # (norm_title, url, level)

    for mp in glob.glob(os.path.join(store_root, "raw/meta", "*", "*.json")):
        try:
            m = json.load(io.open(mp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if m.get("kind") != "article":
            continue
        url = m.get("url") or ""
        if not url:
            continue
        if m.get("failed"):
            continue
        nb = int(m.get("n_bytes") or 0)
        if not nb:
            for v in (m.get("versions") or []):
                nb = max(nb, int(v.get("bytes") or 0))
        if nb < 500:
            continue
        by_url[url] = "full"
        t = norm_title(m.get("title"))
        if t:
            by_title.append((t, url, "full"))

    snip = os.path.join(store_root, "extracted", "search_snippets.jsonl")
    if os.path.isfile(snip):
        for line in io.open(snip, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            u = d.get("url") or ""
            if not u:
                continue
            # 全文优先，摘要不覆盖
            by_url.setdefault(u, "snippet")
            t = norm_title(d.get("title"))
            if t:
                by_title.append((t, u, "snippet"))
    return by_url, by_title


def judge(rec, by_url, by_title):
    """判一个基准事件：漏采 / 部分 / 完整。"""
    u = str(rec.get("url") or "").strip()
    if u:
        # 精确 URL 命中
        if u in by_url:
            return by_url[u], "url_exact"
        # 去协议/尾斜杠再试
        u2 = re.sub(r"^https?://", "", u).rstrip("/")
        for k, v in by_url.items():
            if re.sub(r"^https?://", "", k).rstrip("/") == u2:
                return v, "url_norm"
        # 同域名同尾段
        tail = u.rstrip("/").split("/")[-1].lower()
        if len(tail) > 12:
            h = host_of(u)
            for k, v in by_url.items():
                if host_of(k) == h and k.rstrip("/").split("/")[-1].lower() == tail:
                    return v, "url_tail"

    # 标题宽松匹配
    nt = norm_title(rec.get("summary"))
    if nt:
        qw = [w for w in nt.split() if len(w) > 3]
        if qw:
            for t, _u, lvl in by_title:
                hit = sum(1 for w in qw if w in t)
                if hit >= max(2, int(len(qw) * 0.6)):
                    return lvl, "title"
    return "missed", "none"


def main():
    ap = argparse.ArgumentParser(
        description="高价值信源事件漏采率（只报事实，不给总分）")
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--store", default=r"D:\event-ledger-data")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    base = load_baseline(a.baseline)
    by_url, by_title = build_index(a.store)

    print("")
    print("  人工基准 %d 个事件" % len(base))
    print("  系统索引 全文 %d 篇 ｜ 含摘要共 %d 条"
          % (sum(1 for v in by_url.values() if v == "full"), len(by_url)))
    print("")

    verdicts = []
    for rec in base:
        lvl, how = judge(rec, by_url, by_title)
        src = host_of(rec.get("url")) or str(rec.get("source") or "")
        verdicts.append({
            "summary": rec.get("summary") or "",
            "date": rec.get("date") or "",
            "country": rec.get("country") or "",
            "source": src,
            "high_value": src in HIGH_VALUE,
            "verdict": {"full": "FOUND", "snippet": "PARTIAL",
                        "missed": "MISSED"}[lvl],
            "matched_by": how,
            "url": rec.get("url") or "",
        })

    cnt = Counter(v["verdict"] for v in verdicts)
    n = len(verdicts)
    print("  ── 总体 ──")
    for k, label in (("FOUND", "发现且有全文"),
                     ("PARTIAL", "发现但只有摘要"),
                     ("MISSED", "完全没发现")):
        c = cnt.get(k, 0)
        print("    %-16s %3d ｜ %5.1f%%" % (label, c, 100.0 * c / max(1, n)))
    print("")
    print("    真正的漏采率（MISSED / 全部）  %5.1f%%"
          % (100.0 * cnt.get("MISSED", 0) / max(1, n)))
    print("    内容完整率（FOUND / 全部）     %5.1f%%"
          % (100.0 * cnt.get("FOUND", 0) / max(1, n)))
    print("")

    # 按信源分组
    grp = defaultdict(lambda: Counter())
    for v in verdicts:
        grp[v["source"]][v["verdict"]] += 1
    if grp:
        print("  ── 按信源 ──")
        rows = []
        for s, c in grp.items():
            tot = sum(c.values())
            rows.append((c.get("MISSED", 0) / max(1, tot), s, tot, c))
        rows.sort(reverse=True)
        for rate, s, tot, c in rows[:15]:
            flag = "★" if s in HIGH_VALUE else " "
            print("   %s %-28s 计 %2d ｜ 全文 %2d ｜ 摘要 %2d ｜ 漏 %2d (%.0f%%)"
                  % (flag, s[:26], tot, c.get("FOUND", 0),
                     c.get("PARTIAL", 0), c.get("MISSED", 0), rate * 100))
        print("")

    hv = [v for v in verdicts if v["high_value"]]
    if hv:
        hc = Counter(v["verdict"] for v in hv)
        print("  ── 高价值信源（RECONFIGURE 档）事件 %d 个 ──" % len(hv))
        for k, label in (("FOUND", "发现且有全文"), ("PARTIAL", "只有摘要"),
                         ("MISSED", "完全没发现")):
            print("    %-16s %3d" % (label, hc.get(k, 0)))
        print("")
        print("    这就是 RECONFIGURE 策略的信息损失量：")
        print("      PARTIAL = 拿得到线索但拿不到全文（人工可补）")
        print("      MISSED  = 完全没线索（真损失）")
        print("")

    miss = [v for v in verdicts if v["verdict"] == "MISSED"]
    if miss:
        print("  ── 漏采清单（前 15，这些要人工看）──")
        for v in miss[:15]:
            print("    %-12s %-10s %s"
                  % (v["date"][:10], v["country"][:8], v["summary"][:46]))
        print("")

    if a.out:
        payload = {"total": n, "counts": dict(cnt),
                   "verdicts": verdicts}
        io.open(a.out, "w", encoding="utf-8").write(
            json.dumps(payload, ensure_ascii=False, indent=1))
        print("  已写出 → %s" % a.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
