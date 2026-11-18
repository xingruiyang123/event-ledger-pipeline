#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""信源表覆盖度审计：衡量「我们的名单到底覆盖了多少真实报道这些事件的媒体」。

先说清楚一个被算错过的指标
--------------------------
早前版本算的是「1445 家里有多少家在单月内被检索到」，得到 1.x%，
然后据此判断「信源表遍历没用」。**这个指标是错的**，分母选错了：
信源表里绝大多数是全球性媒体（BBC、NYT、Le Monde），它们不报索马里某个村的冲突，
拿它们当分母，覆盖率必然接近 0，但这不代表名单有问题。

正确的问法是：**在真实报道了这些事件的媒体里，有多少家在我们的名单上？**

两个可算的指标
--------------
1. **媒体池覆盖度**：权威库（ACLED/UCDP）一个月引用了 N 家媒体，
   其中多少家在我们的信源表里。
   实测 2025-08：ACLED 引用 926 家，对上 98 家 = **10.6%**。

2. **补链命中率**：我们给事件补链接时，实际用到的域名里多少家来自信源表。
   实测：17 个域名里 8 个 = **47.1%**。

指标 1 回答「名单还缺谁」；指标 2 回答「补链接时我们的名单够不够用」。

匹配方式
--------
权威库给的是**媒体名**（"Dawn (Pakistan)"、"South Asia Terrorism Portal"），
信源表给的是**机构名 + 域名**。所以要做两次匹配：

  · 媒体名 ↔ 机构名：模糊包含匹配，去短名避免误配
  · URL 域名 ↔ 主域名：精确匹配，最可靠

只用名字匹配会漏（同一家媒体中英名不同），只用域名匹配也会漏
（权威库不给域名）。两个都算，分别报告。

用法
----
  python scripts/audit_registry_coverage.py \\\\
      --registry 信源表.xlsx \\\\
      --events acled.jsonl \\\\
      --linked 带链接.json \\\\
      --out 信源覆盖.md
"""
import argparse
import io
import json
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def load_registry(path, sheet=None):
    """读信源表，返回 {域名: 机构名} 与 {机构名小写: 域名}。"""
    try:
        import openpyxl
    except ImportError:
        print("需要 openpyxl：pip install openpyxl", file=sys.stderr)
        sys.exit(1)
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else None
    if ws is None:
        for w in wb.worksheets:
            if w.max_row > 100:
                ws = w
                break
        if ws is None:
            ws = max(wb.worksheets, key=lambda w: w.max_row)
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]

    def find(*keys):
        for k in keys:
            for i, h in enumerate(hdr):
                if h == k:
                    return i
        for k in keys:
            for i, h in enumerate(hdr):
                if k and k in h:
                    return i
        return None

    i_dom = find("主域名", "域名", "domain")
    i_name = find("机构名称", "名称", "name")
    if i_dom is None:
        print("信源表里找不到域名列", file=sys.stderr)
        sys.exit(1)
    by_dom, by_name = {}, {}
    for r in rows[1:]:
        if not r:
            continue
        d = str(r[i_dom] or "").strip().lower().replace("www.", "")
        if not d:
            continue
        nm = str(r[i_name] or "") if i_name is not None else d
        by_dom[d] = nm
        if nm:
            by_name[nm.strip().lower()] = d
    return by_dom, by_name


def load_jsonl(path):
    out = []
    if not path or not os.path.isfile(path):
        return out
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    return out


def load_any(path):
    """jsonl 或 json 数组都吃。"""
    if not path or not os.path.isfile(path):
        return []
    if path.lower().endswith(".json"):
        try:
            d = json.load(io.open(path, encoding="utf-8"))
            return d if isinstance(d, list) else [d]
        except Exception:  # noqa: BLE001
            return []
    return load_jsonl(path)


def main():
    ap = argparse.ArgumentParser(description="信源表覆盖度审计")
    ap.add_argument("--registry", required=True)
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--events", default=None,
                    help="权威库候选 jsonl（用来抽它引用了哪些媒体）")
    ap.add_argument("--linked", default=None,
                    help="补链结果（json 或 jsonl），用 URL 域名算命中率")
    ap.add_argument("--out", default=None, help="markdown 报告")
    a = ap.parse_args()

    by_dom, by_name = load_registry(a.registry, a.sheet)
    print("信源表 %d 家" % len(by_dom))

    lines = ["# 信源表覆盖度审计", ""]
    lines.append("## 一、名单规模")
    lines.append("")
    lines.append("| 项 | 数量 |")
    lines.append("|---|---|")
    lines.append("| 信源表机构 | %d |" % len(by_dom))
    lines.append("")

    # ---- 指标 1：媒体池覆盖度 ----
    if a.events:
        rows = load_jsonl(a.events)
        media = Counter()
        for r in rows:
            s = (r.get("extra") or {}).get("acled_source") or ""
            if not s:
                s = r.get("source_office") or ""
            for m in re.split(r"[;,]", str(s)):
                m = m.strip()
                if m and len(m) > 2:
                    media[m] += 1
        print("权威库引用媒体 %d 家" % len(media))

        hit, hit_list = 0, []
        for m in media:
            ml = m.lower().strip()
            for rn, dom in by_name.items():
                if not rn or len(rn) < 4 or len(ml) < 4:
                    continue
                if rn in ml or ml in rn:
                    hit += 1
                    hit_list.append((m, by_dom.get(dom, dom)))
                    break
        rate = 100.0 * hit / len(media) if media else 0
        print("对上信源表 %d 家 = %.1f%%" % (hit, rate))

        lines.append("## 二、媒体池覆盖度")
        lines.append("")
        lines.append("权威库一个月引用的媒体里，有多少家在我们的名单上。")
        lines.append("")
        lines.append("| 项 | 数量 |")
        lines.append("|---|---|")
        lines.append("| 权威库引用媒体 | %d |" % len(media))
        lines.append("| 在信源表里 | %d（**%.1f%%**） |" % (hit, rate))
        lines.append("")
        if rate < 20:
            lines.append("> 这个比例偏低，说明权威库实际依赖的媒体与我们的名单重叠有限。")
            lines.append("> 若要补名单，优先加下面这些「权威库在用、我们还没有」的媒体。")
        lines.append("")
        miss = [m for m, c in media.most_common(40)
                if not any(m.lower() == h[0].lower() for h in hit_list)]
        if miss:
            lines.append("### 权威库在用、但不在名单上的媒体（前 20）")
            lines.append("")
            lines.append("| 媒体 | 被引用次数 |")
            lines.append("|---|---|")
            for m in miss[:20]:
                lines.append("| %s | %d |" % (str(m)[:48], media[m]))
            lines.append("")
        if hit_list:
            lines.append("### 已在名单上的（前 20）")
            lines.append("")
            for m, nm in hit_list[:20]:
                lines.append("- %s → %s" % (str(m)[:40], nm))
            lines.append("")

    # ---- 指标 2：补链命中率 ----
    if a.linked:
        linked = load_any(a.linked)
        hosts = Counter()
        for r in linked:
            u = str(r.get("信息来源URL") or r.get("url") or "")
            if u.startswith("http"):
                h = urlparse(u).netloc.replace("www.", "").lower()
                if h:
                    hosts[h] += 1
        hit_dom = [h for h in hosts if h in by_dom]
        rate2 = 100.0 * len(hit_dom) / len(hosts) if hosts else 0
        print("补链涉及 %d 个域名，命中信源表 %d 个 = %.1f%%"
              % (len(hosts), len(hit_dom), rate2))

        lines.append("## 三、补链命中率")
        lines.append("")
        lines.append("给事件补链接时，实际用到的域名里有多少家来自信源表。")
        lines.append("")
        lines.append("| 项 | 数量 |")
        lines.append("|---|---|")
        lines.append("| 用到的域名 | %d |" % len(hosts))
        lines.append("| 其中在信源表 | %d（**%.1f%%**） |" % (len(hit_dom), rate2))
        lines.append("")
        if hit_dom:
            lines.append("命中的机构：")
            lines.append("")
            for h in hit_dom[:20]:
                lines.append("- %s（%s）" % (h, by_dom[h][:30]))
            lines.append("")

    # ---- 结论 ----
    lines.append("## 四、这两个数字该怎么读")
    lines.append("")
    lines.append("**不要用「1445 家里被检索到几家」当覆盖率。** 那个分母是错的：")
    lines.append("名单里大部分是全球性媒体，它们本来就不报索马里某个村的冲突，")
    lines.append("拿它们当分母算出来 1.x%，只能说明「分母选错」，不能说明名单没用。")
    lines.append("")
    lines.append("正确的两条：")
    lines.append("")
    lines.append("1. **媒体池覆盖度** 回答「名单还缺谁」——低就去补那些权威库在用的媒体。")
    lines.append("2. **补链命中率** 回答「补链接时名单够不够用」——这个是实操口径。")
    lines.append("")

    text = "\n".join(lines)
    print()
    print(text[:1500])
    if a.out:
        d = os.path.dirname(os.path.abspath(a.out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print()
        print("已写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
