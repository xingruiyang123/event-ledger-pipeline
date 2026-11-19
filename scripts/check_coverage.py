"""覆盖率验证：原始层文档能否覆盖人工流程发现的基准事件。

基准是什么
----------
用户的人工流程在 2026-07 产出了 111 起事件，其中 47 起 UCDP 里没有
（这批才能验覆盖率，因为有 ged_id 的本来就是从 UCDP 来的）。

取其中 4 国（巴基斯坦 12、叙利亚 5、马里 4、尼日利亚 4）= 25 条基准。

怎么判断「覆盖到」
------------------
从基准摘要里抽可识别的实体：
  地名（Gwadar / Jiwani / Aguelhoc / Ngala / Ngala…）
  组织名（BLA / ISWAP / JNIM / TTP / IS…）
  数字（死亡数）
在原始层文档里搜这些词，命中 >= 阈值就认为「可能覆盖」，再人工确认。

为什么要人工确认
----------------
地点名重叠很常见（比如多个国家都有「Muna」）。自动判据只能缩小范围，
不能定论。这个脚本输出候选对，人工看一遍给结论。
"""
import argparse
import io
import json
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# 地名/组织名：从基准摘要里抓专名
ORG_PAT = re.compile(
    r"\b(BLA|BLF|TTP|TTP[-\s]?[A-Z]*|ISWAP|Boko\s*Haram|JNIM|FLA|ISIS|ISIL|IS|"
    r"al-?Shabaab|Taliban|Houthi|Hezbollah|Hamas|PKK|FARC|ELN|RSF|ADF|"
    r"Ansar\s*\w+|AQIM|al-?Qaeda|Maute|Abu\s*Sayyaf)\b", re.I)
EN_PLACE = re.compile(r"\b([A-Z][a-z]{3,}(?:\s+[A-Z][a-z]{3,}){0,2})\b")
ZH_PLACE = re.compile(r"[\u4e00-\u9fff]{2,8}(?:州|省|市|区|县|镇|村|地区|走廊)")
NUM = re.compile(r"\b(\d{1,3})\b")

# 这些词太泛，不能当匹配依据
STOP = set("""The This That With From After During When Where Which While Their
There These Those About Against Between Because Before Being Below Above Under
Over Such Same Other More Most Some Any All Each Every Both Few Many Much
Killed Dead Died Attack Attacks Attacked Bomb Bombing Explosion Shooting Shot
Police Officer Officers Soldier Soldiers Military Army Forces Security
Local Officials Reported According Said Says Also Just Only Even Still Yet
January February March April May June July August September October November December
Monday Tuesday Wednesday Thursday Friday Saturday Sunday""".split())


def entities(summary):
    """从基准摘要里抽可匹配的实体。"""
    s = str(summary or "")
    ents = {"orgs": set(), "places": set(), "nums": set()}
    for m in ORG_PAT.finditer(s):
        ents["orgs"].add(m.group(1).lower().replace("-", "").replace(" ", ""))
    for m in EN_PLACE.finditer(s):
        w = m.group(1).strip()
        if w.split()[0] in STOP:
            continue
        ents["places"].add(w.lower())
    for m in ZH_PLACE.finditer(s):
        ents["places"].add(m.group(0))
    for m in NUM.finditer(s):
        v = int(m.group(1))
        if 1 <= v <= 500:
            ents["nums"].add(v)
    return ents


def score(ents, text):
    """算一篇文档对一条基准的匹配分。"""
    t = str(text or "")
    tl = t.lower()
    hit = {"orgs": 0, "places": 0, "nums": 0}
    for o in ents["orgs"]:
        if o in tl.replace("-", "").replace(" ", ""):
            hit["orgs"] += 1
    for p in ents["places"]:
        if p in tl:
            hit["places"] += 1
    for n in ents["nums"]:
        if re.search(r"\b%d\b" % n, t) or ("%d人" % n) in t or ("%d 人" % n) in t:
            hit["nums"] += 1
    # 加权：组织名和地名比数字更有区分度
    sc = hit["orgs"] * 3 + hit["places"] * 2 + min(hit["nums"], 2)
    return sc, hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--countries", default="巴基斯坦,叙利亚,马里,尼日利亚")
    ap.add_argument("--threshold", type=int, default=4)
    a = ap.parse_args()

    docs = [json.loads(l) for l in io.open(a.raw, encoding="utf-8") if l.strip()]
    g = [json.loads(l) for l in io.open(a.golden, encoding="utf-8") if l.strip()]
    cs = set(x.strip() for x in a.countries.split(","))
    base = [r for r in g if not r.get("ged_id") and r.get("country") in cs]

    print("")
    print("  基准 %d 条 ｜ 原始层 %d 篇" % (len(base), len(docs)))

    res = []
    for r in base:
        ents = entities(r.get("summary"))
        best = []
        for d in docs:
            sc, hit = score(ents, (d.get("title") or "") + " " + (d.get("text") or ""))
            if sc >= a.threshold:
                best.append((sc, d.get("title"), d.get("url"), hit))
        best.sort(key=lambda x: -x[0])
        res.append({
            "country": r.get("country"),
            "summary": r.get("summary"),
            "label": r.get("label"),
            "n_candidates": len(best),
            "top": [{"score": s, "title": t, "url": u, "hit": h}
                    for s, t, u, h in best[:3]],
            "ents": {k: sorted(v) for k, v in ents.items()},
        })

    json.dump(res, io.open(a.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    covered = sum(1 for x in res if x["n_candidates"] > 0)
    print("")
    print("  有候选匹配的: %d / %d = %.0f%%"
          % (covered, len(res), 100.0 * covered / max(1, len(res))))
    print("")
    for x in res:
        mark = "✓" if x["n_candidates"] else "✗"
        print("  %s [%s] 候选 %2d ｜ %s"
              % (mark, x["country"], x["n_candidates"], str(x["summary"])[:50]))
        if x["top"]:
            print("        → %s" % str(x["top"][0]["title"])[:66])
    print("")
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
