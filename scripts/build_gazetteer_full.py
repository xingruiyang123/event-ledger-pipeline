"""建全量地名词表 —— 覆盖所有国家，供抽取层匹配地点。

为什么要一次做全量而不是按需补
------------------------------
按国家现补的做法在巴基斯坦试过了：能做，但每次都要重跑下载 + 解析，
而 GeoNames 的原始数据本来就是全量的。分批做只是把同一件事做几十遍。

规模控制靠三道闸，不是靠分国家：
  component  只取 P（居民点）和 A（行政区），排除山峰/水系/设施
  population 城市只收 1.5 万人以上的（cities15000 已经是这个门槛）
  语种      只留本项目信源会遇到的 17 种写法

产物大小估计：全球 1.5 万人以上城市约 2.6 万个，
加各级行政区约 5 万个，每行带别名，压缩前约 200-300 MB。
这太大了，不适合放进 skill 仓库。

所以分两份：
  assets/place_gazetteer.jsonl        精简版（只留中英文名 + 主要别名）
  D:\\event-ledger-data\\gazetteer_full.jsonl  全量（含所有语种别名）

仓库里那份要能在 GitHub 上放得下、clone 得快。
"""
import argparse
import io
import json
import os
import sys
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RAW = r"D:\event-ledger-data\geoname_raw"

LANG_WANT = {
    "zh", "zh-CN", "zh-TW", "zh-Hans", "zh-Hant",
    "ur", "ar", "fa", "ps", "sd", "bal",
    "fr", "es", "pt", "ru", "ha", "tr", "id",
    "en",
}

# 精简版只保留这些语种的别名 —— 仓库里那份是给人读和给下游兜底用的，
# 不需要 40 种语言的转写。
LANG_SLIM = {"zh", "zh-CN", "zh-TW", "zh-Hans", "zh-Hant",
             "ur", "ar", "fa", "ps", "sd", "en"}


def pick_member(names, kw):
    cand = [n for n in names if n.lower().endswith(".txt") and kw.lower() in n.lower()]
    return cand[0] if cand else [n for n in names if n.endswith(".txt")][0]


def read_country_info():
    out = {}
    p = os.path.join(RAW, "countryInfo.txt")
    if not os.path.isfile(p):
        return out
    for ln in io.open(p, encoding="utf-8"):
        if ln.startswith("#"):
            continue
        f = ln.rstrip("\n").split("\t")
        if len(f) < 9:
            continue
        out[f[0]] = {"code": f[0], "code3": f[1], "name": f[4],
                     "population": int(f[7]) if f[7].isdigit() else 0}
    return out


def read_admin(name):
    out = {}
    p = os.path.join(RAW, name)
    if not os.path.isfile(p):
        return out
    for ln in io.open(p, encoding="utf-8"):
        f = ln.rstrip("\n").split("\t")
        if len(f) >= 2:
            out[f[0]] = f[1]
    return out


def zh_country_names():
    out = {}
    try:
        from babel import Locale
        t = Locale.parse("zh_Hans").territories
        for k, v in t.items():
            out.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", default=r"D:\event-ledger-data\gazetteer_full.jsonl")
    ap.add_argument("--slim",
                    default=r"D:\shared-skills\event-ledger-pipeline\assets\place_gazetteer.jsonl")
    a = ap.parse_args()

    cinfo = read_country_info()
    a1 = read_admin("admin1CodesASCII.txt")
    a2 = read_admin("admin2Codes.txt")
    zhc = zh_country_names()
    print("")
    print("  国家 %d ｜ 一级行政区 %d ｜ 二级行政区 %d" % (len(cinfo), len(a1), len(a2)))

    # 先把所有要查别名的 id 收集齐，再扫一遍 alternateNames（只扫一次）
    zp = os.path.join(RAW, "cities15000.zip")
    cities = []
    with zipfile.ZipFile(zp) as z:
        with z.open(pick_member(z.namelist(), "cities15000")) as fh:
            for raw in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
                f = raw.rstrip("\n").split("\t")
                if len(f) < 15:
                    continue
                gid, nm, asci, alt, lat, lon, fc, fcode, cc, cc2, \
                    x1, x2, x3, x4, pop = f[:15]
                cities.append({
                    "id": gid, "name": asci or nm, "native": nm,
                    "lat": lat, "lon": lon, "fclass": fc, "fcode": fcode,
                    "cc": cc, "a1": x1, "a2": x2,
                    "pop": int(pop) if pop.isdigit() else 0, "alt": alt,
                })
    print("  城市 %d 条" % len(cities))

    want = set(c["id"] for c in cities) | set(a1) | set(a2)
    alts = {}
    zp2 = os.path.join(RAW, "alternateNamesV2.zip")
    if os.path.isfile(zp2):
        with zipfile.ZipFile(zp2) as z:
            with z.open(pick_member(z.namelist(), "alternateNamesV2")) as fh:
                for raw in io.TextIOWrapper(fh, encoding="utf-8", errors="replace"):
                    f = raw.rstrip("\n").split("\t")
                    if len(f) < 4:
                        continue
                    # 列序：alternateNameId, geonameid, isolanguage, name
                    gid, lang, nm = f[1], f[2] or "", f[3].strip()
                    if gid not in want or lang not in LANG_WANT or not nm:
                        continue
                    alts.setdefault(gid, []).append((lang, nm))
    print("  有别名地名 %d" % len(alts))

    rows = []

    for code, ci in sorted(cinfo.items()):
        al = alts.get(code, [])
        rows.append({
            "id": code, "level": "country", "name": ci["name"],
            "name_zh": zhc.get(code, ""),
            "aliases": sorted(set([ci["name"], ci.get("code3") or ""] +
                                  [x for _l, x in al]) - {""}),
            "country": ci["name"], "country_zh": zhc.get(code, ""),
            "country_code": code, "admin1": "", "admin2": "",
            "lat": "", "lon": "", "feature": "A",
            "population": ci.get("population", 0),
        })

    for code, nm in sorted(a1.items()):
        cc = code.split(".")[0]
        al = alts.get(code, [])
        rows.append({
            "id": code, "level": "admin1", "name": nm, "name_zh": "",
            "aliases": sorted(set([nm] + [x for _l, x in al]) - {""}),
            "country": cinfo.get(cc, {}).get("name", ""),
            "country_zh": zhc.get(cc, ""), "country_code": cc,
            "admin1": nm, "admin2": "", "lat": "", "lon": "", "feature": "A",
            "population": 0,
        })

    for code, nm in sorted(a2.items()):
        cc = code.split(".")[0]
        al = alts.get(code, [])
        rows.append({
            "id": code, "level": "admin2", "name": nm, "name_zh": "",
            "aliases": sorted(set([nm] + [x for _l, x in al]) - {""}),
            "country": cinfo.get(cc, {}).get("name", ""),
            "country_zh": zhc.get(cc, ""), "country_code": cc,
            "admin1": a1.get(".".join(code.split(".")[:2]), ""),
            "admin2": nm, "lat": "", "lon": "", "feature": "A",
            "population": 0,
        })

    for c in cities:
        al = alts.get(c["id"], [])
        cc = c["cc"]
        aliases = set([c["name"], c["native"]])
        for _l, x in al:
            aliases.add(x)
        for x in (c.get("alt") or "").split(","):
            x = x.strip()
            if x:
                aliases.add(x)
        zh = [x for l, x in al if l.startswith("zh")]
        rows.append({
            "id": c["id"], "level": "city", "name": c["name"],
            "name_zh": zh[0] if zh else "",
            "aliases": sorted(x for x in aliases if x),
            "country": cinfo.get(cc, {}).get("name", ""),
            "country_zh": zhc.get(cc, ""), "country_code": cc,
            "admin1": a1.get("%s.%s" % (cc, c["a1"]), c["a1"]),
            "admin2": a2.get("%s.%s.%s" % (cc, c["a1"], c["a2"]), c["a2"]),
            "lat": c["lat"], "lon": c["lon"], "feature": c["fclass"],
            "population": c["pop"],
        })

    os.makedirs(os.path.dirname(os.path.abspath(a.full)), exist_ok=True)
    with io.open(a.full, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("")
    print("  全量 → %s（%.0f MB）" % (a.full, os.path.getsize(a.full) / 1048576.0))

    # 精简版：只留中/乌尔都/阿拉伯/波斯/普什图/信德语别名，
    # 且城市只留人口 5 万以上的 —— 仓库要能放得下。
    slim = []
    for r in rows:
        if r["level"] == "city" and int(r["population"] or 0) < 50000:
            continue
        keep = []
        for x in r["aliases"]:
            if any("\u4e00" <= ch <= "\u9fff" for ch in x):
                keep.append(x)
            elif any("\u0600" <= ch <= "\u06ff" for ch in x):
                keep.append(x)
            elif x.isascii():
                keep.append(x)
        r = dict(r)
        r["aliases"] = sorted(set(keep))[:40]
        slim.append(r)
    os.makedirs(os.path.dirname(os.path.abspath(a.slim)), exist_ok=True)
    with io.open(a.slim, "w", encoding="utf-8", newline="\n") as f:
        for r in slim:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("  精简 → %s（%d 条，%.1f MB）"
          % (a.slim, len(slim), os.path.getsize(a.slim) / 1048576.0))

    from collections import Counter
    lv = Counter(r["level"] for r in rows)
    print("")
    for k in ("country", "admin1", "admin2", "city"):
        print("    %-8s %7d" % (k, lv.get(k, 0)))
    print("    合计     %7d" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())