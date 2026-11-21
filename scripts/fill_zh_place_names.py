"""补中文地名 —— GeoNames 的中文覆盖不齐，得从别处补。

问题
----
巴基斯坦 375 个城市里，只有 53 个有中文名。缺的包括费萨拉巴德
（Faisalabad，380 万人）、本努（Bannu）、穆扎法拉巴德（Muzaffarabad）
这些经常上报的城市。而中文报道恰恰主要用中文地名。

GeoNames 缺的原因很直接：那批地名没有 `zh` 语种的 alias，
只有 `ur`（乌尔都文）和拉丁转写。这不是抓取问题，是数据源本身没有。

补法
----
两条路，按可靠性排序：

1. **维基数据（Wikidata）** —— langlinks 里有 zh / zh-hans / zh-cn，
   覆盖比 GeoNames 全，而且是人工维护的对应关系。
   查法：按 GeoNames 的英文名 + 国家去维基数据找实体，取中文 label。
   风险：同名实体多，必须带国家约束，否则 Faisalabad 可能连到别处。

2. **不补，标出来。** 找不到就留空，不要编。宁可中文名空着
   让下游用英文名匹配，也不能填错 —— 填错的地名会直接写进台账。

所以这个脚本的产出分两部分：补上的，以及**补不上的清单**。
后者要交给人看，不是静默跳过。
"""
import argparse
import io
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _api(sess, params, timeout=25, tries=4):
    """调维基数据 API，遇到 429 退避重试。

    429 是本通道的常态，不是异常：实测连着发十几次就会被限流，
    返回的是纯文本 "You are making too many requests..."，
    json() 直接抛 JSONDecodeError —— 看起来像「找不到中文名」，
    实际是**请求被拒了**。这两件事必须分开，否则会把限流
    错记成「该地名没有中文名」。
    """
    import time
    for i in range(tries):
        try:
            r = sess.get("https://www.wikidata.org/w/api.php", timeout=timeout,
                         params=params)
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code == 429:
            wait = 3.0 * (2 ** i)
            time.sleep(wait)
            continue
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return None


def wd_search(name, country_en, sess, timeout=25):
    """在维基数据里按名字找一个实体，用国家和描述做消歧。

    不能把国家名拼进检索词 —— wbsearchentities 是按条目标签匹配的，
    「Faisalabad Pakistan」一条都搜不到（实测结果为 0），
    而「Faisalabad」能搜到 5 条、第一条正是巴基斯坦旁遮普省的城市。
    国家约束放在**过滤描述**这一步，不放在检索词里。
    """
    js = _api(sess, {"action": "wbsearchentities", "search": name,
                     "language": "en", "format": "json",
                     "limit": 10, "type": "item"}, timeout)
    if not js:
        return None
    best = None
    for item in (js.get("search") or []):
        desc = (item.get("description") or "").lower()
        lab = (item.get("label") or "").lower()
        if lab != name.lower() and not lab.startswith(name.lower()):
            continue
        if any(k in lab for k in (" district", " province", " university",
                                  " division", " tehsil", " regiment",
                                  " airport", " stadium", " club",
                                  " railway", " cricket")):
            continue
        if country_en and country_en.lower() in desc:
            return item.get("id")
        if best is None and any(k in desc for k in
                                ("city", "town", "village", "capital")):
            best = item.get("id")
    return best


def wd_zh_label(qid, sess, timeout=25):
    """取实体的中文标签。先 zh，再 zh-hans / zh-cn，最后中文维基标题。"""
    js = _api(sess, {"action": "wbgetentities", "ids": qid,
                     "props": "labels|sitelinks", "format": "json",
                     "languages": "zh|zh-hans|zh-cn|zh-tw|en"}, timeout)
    if not js:
        return ""
    ent = (js.get("entities") or {}).get(qid) or {}
    labels = ent.get("labels") or {}
    for k in ("zh", "zh-hans", "zh-cn", "zh-tw"):
        v = (labels.get(k) or {}).get("value")
        if v and any("\u3400" <= ch <= "\u9fff" for ch in v):
            return v
    sl = (ent.get("sitelinks") or {}).get("zhwiki") or {}
    t = sl.get("title") or ""
    if t and any("\u3400" <= ch <= "\u9fff" for ch in t):
        return t
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gazetteer", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0, help="最多补几条，0=全部")
    ap.add_argument("--min-pop", type=int, default=100000,
                    help="只补人口达到此数的城市，小地名不值得一个个查")
    ap.add_argument("--sleep", type=float, default=0.12)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    import requests
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    sess.trust_env = True          # 维基数据要走代理

    rows = [json.loads(l) for l in io.open(a.gazetteer, encoding="utf-8")
            if l.strip()]
    todo = [r for r in rows
            if not r.get("name_zh")
            and r.get("level") in ("city", "admin1")
            and int(r.get("population") or 0) >= a.min_pop]
    todo.sort(key=lambda r: -int(r.get("population") or 0))
    if a.limit:
        todo = todo[:a.limit]

    print("")
    print("  词表 %d 条 ｜ 缺中文名且人口≥%d 的 %d 条"
          % (len(rows), a.min_pop, len(todo)))
    print("")

    filled, failed = {}, []
    for i, r in enumerate(todo, 1):
        cc = r.get("country_code") or ""
        qid = wd_search(r["name"], r.get("country") or "", sess)
        zh = ""
        if qid:
            zh = wd_zh_label(qid, sess)
        if zh:
            filled[r["name"]] = zh
            print("  %3d/%d  %-26s → %s（%s）"
                  % (i, len(todo), r["name"][:24], zh, qid))
        else:
            failed.append((r["name"], r.get("country"), r.get("population")))
            print("  %3d/%d  %-26s → 找不到，留空" % (i, len(todo), r["name"][:24]))
        time.sleep(a.sleep)

    print("")
    print("  补上 %d 条 ｜ 补不上 %d 条" % (len(filled), len(failed)))

    if a.out:
        io.open(a.out, "w", encoding="utf-8").write(
            json.dumps({"filled": filled,
                        "failed": [{"name": n, "country": c, "pop": p}
                                   for n, c, p in failed]},
                       ensure_ascii=False, indent=1))
        print("  明细 → %s" % a.out)

    if a.apply and filled:
        n = 0
        for r in rows:
            z = filled.get(r["name"])
            if z and not r.get("name_zh"):
                r["name_zh"] = z
                # 中文名同时作为可匹配写法
                if z not in r["aliases"]:
                    r["aliases"] = sorted(set(r["aliases"] + [z]))
                n += 1
        with io.open(a.gazetteer, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print("  已写回 %d 条 → %s" % (n, a.gazetteer))
    return 0


if __name__ == "__main__":
    sys.exit(main())