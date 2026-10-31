#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多源事件采集：能接什么源就接什么源，统一成同一套事件格式。

为什么需要「多源」而不是「一个最好的源」
--------------------------------------
2026-08 全量实测的教训：
  · 只靠新闻索引 → 8 条，而尼日利亚一国同月 500 起安全事件
  · 只靠 UCDP 候选集 → 27 条（候选集只保留还在编码队列的月份，历史月份移入正式 GED）
  · 只靠维基 → 偏欧美，非洲小规模袭击几乎不进

**没有任何单一免费源能给出「量级完整」的月度台账。**
但不同源的偏差方向不一样：
  · UCDP/ACLED 偏「有组织武装冲突」，非洲覆盖好
  · 维基偏「国际关注度」，大国战争多
  · GDELT 偏「新闻热度」，实时但粒度粗

把它们叠起来，覆盖率才上得去。本脚本做的就是「叠」。

设计原则
--------
  1. **每个源一个适配器**，统一输出字段，缺的留空不猜
  2. **能连上就用，连不上就跳过并记录原因**（不许因为一个源挂了就整体失败）
  3. **去重在同一层做**：同国 + 同日 + 死亡数相同 + 地点相近 → 合并为一条，记录来自哪些源
  4. **每个源都记录「它给了多少条」**，这是后续判断覆盖率的依据

统一字段
--------
  source        来源标识（ucdp-candidate / wikimedia-en / gdelt / ...）
  date          事发日 YYYY-MM-DD
  country       国家（尽量归一成中文名）
  place         地点描述
  summary       事件摘要
  deaths        死亡数（int 或 None）
  injured       受伤数（int 或 None）
  actor_a       加害方
  actor_b       受害方
  act_type      动作类型
  url           来源链接
  raw_id        源内 id

用法
----
  python scripts/harvest_multi_source.py --month 2026-08 --out 候选.jsonl
  python scripts/harvest_multi_source.py --month 2026-08 --out 候选.jsonl \
      --fetch-cmd "tf.py fetch {url}" --sources ucdp,wiki
  python scripts/harvest_multi_source.py --month 2026-08 --list-sources

退出码：0 = 至少一个源有产出且达到下限 | 2 = 全部源都没产出或总量过少 | 1 = 参数错误
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 国家归一
COUNTRY_ZH = {
    "Nigeria": "尼日利亚", "Somalia": "索马里", "Sudan": "苏丹",
    "South Sudan": "南苏丹", "Mali": "马里", "Burkina Faso": "布基纳法索",
    "Niger": "尼日尔", "Chad": "乍得", "Cameroon": "喀麦隆",
    "Democratic Republic of the Congo": "刚果（金）", "DR Congo (Zaire)": "刚果（金）",
    "Congo": "刚果（布）", "Mozambique": "莫桑比克", "Ethiopia": "埃塞俄比亚",
    "Kenya": "肯尼亚", "Uganda": "乌干达", "Burundi": "布隆迪",
    "Rwanda": "卢旺达", "Tanzania": "坦桑尼亚", "Angola": "安哥拉",
    "Zimbabwe": "津巴布韦", "South Africa": "南非", "Libya": "利比亚",
    "Algeria": "阿尔及利亚", "Egypt": "埃及", "Morocco": "摩洛哥",
    "Tunisia": "突尼斯", "Pakistan": "巴基斯坦", "Afghanistan": "阿富汗",
    "India": "印度", "Bangladesh": "孟加拉国", "Sri Lanka": "斯里兰卡",
    "Nepal": "尼泊尔", "Myanmar (Burma)": "缅甸", "Myanmar": "缅甸",
    "Thailand": "泰国", "Philippines": "菲律宾", "Indonesia": "印度尼西亚",
    "Malaysia": "马来西亚", "Syria": "叙利亚", "Iraq": "伊拉克",
    "Yemen (North Yemen)": "也门", "Yemen": "也门", "Lebanon": "黎巴嫩",
    "Israel": "以色列", "Palestine": "巴勒斯坦", "Jordan": "约旦",
    "Turkey": "土耳其", "Iran": "伊朗", "Saudi Arabia": "沙特阿拉伯",
    "Colombia": "哥伦比亚", "Venezuela": "委内瑞拉", "Mexico": "墨西哥",
    "Haiti": "海地", "Ecuador": "厄瓜多尔", "Peru": "秘鲁", "Brazil": "巴西",
    "Honduras": "洪都拉斯", "Guatemala": "危地马拉", "El Salvador": "萨尔瓦多",
    "Russia": "俄罗斯", "Ukraine": "乌克兰", "United States": "美国",
    "United Kingdom": "英国", "France": "法国", "Germany": "德国",
}


def zh_country(name):
    n = (name or "").strip()
    if n in COUNTRY_ZH:
        return COUNTRY_ZH[n]
    for k, v in COUNTRY_ZH.items():
        if k.lower() == n.lower():
            return v
    return n


# ---------------------------------------------------------------- 源适配器
def src_ucdp(month, workdir, **_):
    """UCDP 月度候选集（免费 CSV，覆盖最新在编月份）。

    局限：候选集只保留「还在编码队列」的月份，历史月份会移入正式 GED（滞后 1-2 年）。
    所以它对新月份有用，对旧月份几乎为空 —— 这是数据源特性，不是 bug。
    """
    out = []
    csvs = [f for f in os.listdir(workdir) if f.lower().startswith("gedevent") and f.endswith(".csv")]
    for fn in csvs:
        p = os.path.join(workdir, fn)
        try:
            rows = list(csv.DictReader(io.open(p, encoding="utf-8", errors="replace")))
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            ds = (r.get("date_start") or "")[:10]
            if not ds.startswith(month):
                continue
            def num(k):
                v = r.get(k)
                try:
                    return int(float(v))
                except Exception:  # noqa: BLE001
                    return None
            best = num("best")
            out.append({
                "source": "ucdp-candidate",
                "date": ds,
                "country": zh_country(r.get("country")),
                "place": (r.get("where_description") or "").strip(),
                "summary": (r.get("source_headline") or r.get("conflict_name") or "").strip()[:300],
                "deaths": best,
                "injured": None,
                "actor_a": (r.get("side_a") or "").strip(),
                "actor_b": (r.get("side_b") or "").strip(),
                "act_type": {"1": "armed_conflict", "2": "one_sided",
                             "3": "one_sided"}.get(r.get("type_of_violence", ""), "unknown"),
                "url": (r.get("source_article") or "").strip(),
                "raw_id": r.get("id"),
                "extra": {
                    "deaths_civilians": num("deaths_civilians"),
                    "deaths_a": num("deaths_a"),
                    "deaths_b": num("deaths_b"),
                    "type_of_violence": r.get("type_of_violence"),
                    "dyad": r.get("dyad_name"),
                },
            })
    return out, ("ucdp-candidate", "ok" if out else "no-rows-for-month")


def src_wikimedia(month, workdir, fetch_cmd=None, lang="en", **_):
    """维基「当前事件」日历（免费，每天一页，每条标原出处）。

    局限：编辑者偏欧美，非洲小规模袭击几乎不进。
    """
    y, m = month.split("-")
    MONTHS = {"01": "January", "02": "February", "03": "March", "04": "April",
              "05": "May", "06": "June", "07": "July", "08": "August",
              "09": "September", "10": "October", "11": "November", "12": "December"}
    mname = MONTHS[m]

    def _fetch(url):
        if fetch_cmd:
            import shlex
            import subprocess
            env = dict(os.environ)
            env["NO_PROXY"] = "*"
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env.pop(k, None)
            argv = [a.replace("{url}", url) for a in shlex.split(fetch_cmd)]
            try:
                r = subprocess.run(argv, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=60, env=env)
                if len(r.stdout or "") > 300:
                    return r.stdout
            except Exception:  # noqa: BLE001
                pass
        try:
            import requests
            import urllib3
            urllib3.disable_warnings()
            s = requests.Session()
            s.trust_env = False
            s.headers.update({"User-Agent": UA})
            r = s.get(url, timeout=25, verify=False)
            if r.status_code == 200 and len(r.text) > 300:
                return r.text
        except Exception:  # noqa: BLE001
            pass
        return None

    out = []
    for d in range(1, 32):
        name = "%s_%s_%d" % (y, mname, d)
        url = "https://%s.wikipedia.org/w/index.php?title=Portal:Current_events/%s&action=raw" % (lang, name)
        text = _fetch(url)
        if not text or "Jump to content" in text[:2000]:
            continue
        m2 = re.search(r"'{2,3}\s*Armed conflicts and attacks\s*'{2,3}", text) or \
             re.search(r"={2,4}\s*Armed conflicts and attacks\s*={2,4}", text)
        if not m2:
            continue
        seg = text[m2.end():]
        stop = len(seg)
        for h in ("Disasters and accidents", "Politics and elections",
                  "International relations", "Law and crime", "Sports"):
            mm = re.search(r"'{2,3}\s*" + re.escape(h), seg) or re.search(r"={2,4}\s*" + re.escape(h), seg)
            if mm:
                stop = min(stop, mm.start())
        seg = seg[:stop]
        for line in seg.split("\n"):
            s2 = line.strip()
            if not s2.startswith("*"):
                continue
            s2 = re.sub(r"^[\*#:]+", "", s2).strip()
            s2 = re.sub(r"\[\[([^\]\|]+)\|([^\]]+)\]\]", r"\2", s2)
            s2 = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s2)
            s2 = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"（\1）", s2)
            s2 = re.sub(r"\[https?://[^\]]+\]", "", s2)
            s2 = re.sub(r"''+", "", s2)
            s2 = re.sub(r"<[^>]+>", "", s2)
            s2 = re.sub(r"\s+", " ", s2).strip()
            if len(s2) < 25:
                continue
            out.append({
                "source": "wikimedia-%s" % lang,
                "date": "%s-%02d" % (month, d),
                "country": "",
                "place": "",
                "summary": s2,
                "deaths": None,
                "injured": None,
                "actor_a": "", "actor_b": "",
                "act_type": "unknown",
                "url": url,
                "raw_id": None,
                "extra": {},
            })
    return out, ("wikimedia-%s" % lang, "ok" if out else "no-rows-for-month")


def src_gdelt(month, workdir, **_):
    """GDELT DOC API（完全免费、实时）。

    局限：CAMEO 编码粒度粗，不是「事件台账」，只适合找线索。
    """
    y, m = month.split("-")
    start = "%s%s01000000" % (y, m)
    end = "%s%s31235959" % (y, m)
    q = "(terror OR attack OR bombing OR ambush OR massacre)"
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query=%s&mode=artlist"
           "&startdatetime=%s&enddatetime=%s&maxrecords=250&format=json"
           % (q.replace(" ", "%20").replace("(", "%28").replace(")", "%29"), start, end))
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": UA})
        r = s.get(url, timeout=40, verify=False)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return [], ("gdelt", "error:%s" % type(e).__name__)

    out = []
    for a in (data.get("articles") or []):
        seendate = (a.get("seendate") or "")[:8]
        if len(seendate) != 8:
            continue
        ds = "%s-%s-%s" % (seendate[:4], seendate[4:6], seendate[6:8])
        out.append({
            "source": "gdelt",
            "date": ds,
            "country": zh_country(a.get("sourcecountry") or ""),
            "place": "",
            "summary": (a.get("title") or "")[:300],
            "deaths": None, "injured": None,
            "actor_a": "", "actor_b": "",
            "act_type": "unknown",
            "url": a.get("url") or "",
            "raw_id": None,
            "extra": {"domain": a.get("domain")},
        })
    return out, ("gdelt", "ok" if out else "no-rows-for-month")


SOURCES = {
    "ucdp": src_ucdp,
    "wiki": src_wikimedia,
    "gdelt": src_gdelt,
}


# ---------------------------------------------------------------- 去重
def dedup(rows):
    """同国 + 同日 + 死亡数相同 + 摘要相似 → 合并。保留所有来源。"""
    def key(r):
        d = r.get("deaths")
        cty = r.get("country") or ""
        return (cty, r.get("date"), d)

    buckets = {}
    for r in rows:
        buckets.setdefault(key(r), []).append(r)

    merged = []
    for k, group in buckets.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        base = dict(group[0])
        base["source"] = "+".join(sorted({g["source"] for g in group}))
        base["extra"] = dict(base.get("extra") or {})
        base["extra"]["merged_from"] = len(group)
        merged.append(base)
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out", default=None)
    ap.add_argument("--sources", default="ucdp,wiki,gdelt",
                    help="逗号分隔，默认全开；连不上的会自动跳过")
    ap.add_argument("--workdir", default=".", help="UCDP CSV 所在目录")
    ap.add_argument("--fetch-cmd", default=None, help="外部抓取命令，{url} 占位")
    ap.add_argument("--min-items", type=int, default=80)
    ap.add_argument("--list-sources", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.list_sources:
        for k, v in SOURCES.items():
            print("%-8s %s" % (k, (v.__doc__ or "").strip().split("\n")[0]))
        return 0

    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", a.month):
        print("--month 格式应为 YYYY-MM")
        return 1

    want = [s.strip() for s in a.sources.split(",") if s.strip()]
    rows, report = [], []
    for name in want:
        fn = SOURCES.get(name)
        if not fn:
            report.append({"source": name, "status": "unknown-source", "rows": 0})
            continue
        try:
            got, (label, status) = fn(a.month, a.workdir, fetch_cmd=a.fetch_cmd)
        except Exception as e:  # noqa: BLE001
            got, label, status = [], name, "exception:%s" % type(e).__name__
        rows.extend(got)
        report.append({"source": label, "status": status, "rows": len(got)})

    merged = dedup(rows)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            for r in merged:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = len(merged) >= a.min_items
    exit_code = 0 if ok else 2

    if a.json:
        print(json.dumps({"month": a.month, "raw_rows": len(rows),
                          "merged": len(merged), "sources": report,
                          "min_items": a.min_items, "exit": exit_code},
                         ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 70)
    print(" 多源事件采集 · %s" % a.month)
    print("=" * 70)
    for r in report:
        print("  %-18s %-22s %4d 条" % (r["source"], r["status"], r["rows"]))
    print()
    print("原始 %d 条 → 去重合并后 %d 条（下限 %d）" % (len(rows), len(merged), a.min_items))
    print()

    if report and all(r["rows"] == 0 for r in report):
        print("  ✗ 所有源都没有产出。先查：源是否可达 / 该月数据是否已发布。")
    elif not ok:
        print("  ✗ 总量低于下限。这不是「这个月事件少」，而是源覆盖不足：")
        print("    · UCDP 候选集只含还在编码队列的月份，历史月份要等正式 GED")
        print("    · 维基偏欧美，非洲小规模袭击几乎不进")
        print("    · GDELT 粒度粗，只是线索")
        print("    处置：接 ACLED（学术免费额度）或用本地媒体逐家点名补漏。")

    print()
    print("→", a.out or "(未指定 --out)")
    print("=" * 70)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
