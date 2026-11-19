"""原始层采集：只存，不判。

为什么单独一层
--------------
用户要求「原始内容与结构化结果分层保存」：

  原始层  链接、标题、发布时间、抓取时间、正文快照
          —— 不做任何加工，保住证据本身
  处理层  事件识别、恐袭判别、去重、字段抽取
          —— 每个字段带原文证据句

分开的好处：规则升级后能拿原始层重新跑一遍，历史数据不用重采。
混在一起的话，改一次规则就得重爬全网。

这一层只做三件事
----------------
  1. 按「国家 × 事件类型」组合查询，把结果全存下来
  2. 每条存：url、标题、站点、发布时间、抓取时间、正文
  3. 按 url 哈希去重（同一篇被多次搜到只存一次）

不做的事：不判是不是恐袭、不抽字段、不去重跨源。那些在处理层。

正文来源
--------
元宝检索的返回里直接带 content 字段（实测），不用再抓网页。
抓不到 content 的才回退去抓 url。
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 查询模板：国家名占位 {c}，月份占位 {m}
QUERIES = [
    "{c} 恐怖袭击 {m}",
    "{c} 爆炸 袭击 死亡",
    "{c} 武装分子 袭击 军警",
    "{c} 伏击 车队",
    "{c} 路边炸弹 简易爆炸装置",
    "{c} 自杀式袭击",
    "{c} 绑架 劫持 平民",
    "{c} 军事基地 遭袭",
    "{c} 击毙 武装分子 交火",
    "{c} 村庄 遭袭 屠杀",
]


def clean_env():
    """调本地检索工具要清代理。"""
    e = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        e.pop(k, None)
    e["NO_PROXY"] = "*"
    e["no_proxy"] = "*"
    return e


def search_json(cmd_tpl, q, timeout=60):
    """跑检索，解析 JSON 取 results。"""
    cmd = cmd_tpl.replace("{q}", q.replace('"', " "))
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=clean_env(), timeout=timeout)
        out = r.stdout or ""
    except Exception:  # noqa: BLE001
        return []
    i = out.find("{")
    if i < 0:
        return []
    try:
        d = json.loads(out[i:])
        return d.get("results") or []
    except Exception:  # noqa: BLE001
        return []


NOISE_DOM = re.compile(
    r"icon\.|cdn-|static\.|/favicon|baike\.|wikipedia|wikiwand|kiddle|"
    r"books\.google|academic\.|facebook|twitter|x\.com|youtube|instagram|"
    r"tiktok|t\.me|google\.|bing\.|baidu\.com|so\.com|sogou|yandex", re.I)


def dom_of(u):
    try:
        return str(u).split("/")[2].lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def doc_id(url):
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:16]


def _in_month(published, month):
    """发布时间是否落在目标月份。格式形如 2026/07/30 17:53:35。

    不丢不在此月的文档 —— 有些报道会回溯上月事件。只打标记，
    由处理层决定要不要用。
    """
    m = re.match(r"(\d{4})[-/](\d{1,2})", str(published or ""))
    if not m:
        return None
    return ("%s-%02d" % (m.group(1), int(m.group(2)))) == month


def guess_country(text, hint):
    """从正文里判断涉及哪个国家。原始层不改写，只做提示字段。"""
    return hint


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", required=True, help="逗号分隔，如 巴基斯坦,尼日利亚")
    ap.add_argument("--month", required=True, help="2026-07")
    ap.add_argument("--search-cmd", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--queries", type=int, default=10)
    ap.add_argument("--cnt", default="10")
    ap.add_argument("--sleep", type=float, default=0.3)
    ap.add_argument("--fetch-full", action="store_true",
                    help="按 url 再抓一次全文。元宝返回的 content 只是摘要片段"
                         "（实测中位 376 字，抓网页能到 2806 字）")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--month-filter", action="store_true", default=True,
                    help="标注发布时间不在目标月份的文档（不丢，只标记）")
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    raw_path = os.path.join(a.outdir, "raw_docs.jsonl")
    idx_path = os.path.join(a.outdir, "raw_index.json")

    seen = {}
    if os.path.isfile(idx_path):
        try:
            seen = json.load(io.open(idx_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            seen = {}

    y, m = a.month.split("-")
    mlabel = "%s年%d月" % (y, int(m))
    cmds = []
    for c in [x.strip() for x in a.countries.split(",") if x.strip()]:
        for tpl in QUERIES[:a.queries]:
            cmds.append((c, tpl.format(c=c, m=mlabel)))

    print("")
    print("  原始层采集：%d 个国家 × %d 条查询 = %d 次检索"
          % (len(a.countries.split(",")), a.queries, len(cmds)))

    n_new = 0
    t0 = time.time()
    with io.open(raw_path, "a", encoding="utf-8") as f:
        for i, (ctry, q) in enumerate(cmds, 1):
            res = search_json(
                a.search_cmd.replace("{q}", "{q}") + "", q)
            got = 0
            for r in res:
                url = str(r.get("url") or "")
                d = dom_of(url)
                if not url.startswith("http") or NOISE_DOM.search(d):
                    continue
                did = doc_id(url)
                if did in seen:
                    continue
                text = str(r.get("content") or r.get("passage") or "")
                rec = {
                    "doc_id": did,
                    "url": url,
                    "domain": d,
                    "site_name": r.get("site"),
                    "title": r.get("title"),
                    "published": r.get("date"),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "country_hint": ctry,
                    "query": q,
                    "month": a.month,
                    "text": text,
                    "text_len": len(text),
                    "text_kind": "snippet",
                    "in_month": _in_month(r.get("date"), a.month),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                seen[did] = {"url": url, "country": ctry, "title": r.get("title")}
                n_new += 1
                got += 1
            print("  [%2d/%d] %-12s 新增 %2d ｜ 累计 %d ｜ %s"
                  % (i, len(cmds), ctry, got, n_new, q[:26]))
            if a.sleep:
                time.sleep(a.sleep)

    json.dump(seen, io.open(idx_path, "w", encoding="utf-8"),
              ensure_ascii=False)

    # ---- 可选：按 url 再抓一次全文 ----
    if a.fetch_full:
        print("")
        print("  抓全文（%d 线程）..." % a.workers)
        import extract_article_fields as E
        from concurrent.futures import ThreadPoolExecutor

        docs = []
        for line in io.open(raw_path, encoding="utf-8"):
            line = line.strip()
            if line:
                docs.append(json.loads(line))

        def _full(rec):
            if rec.get("text_kind") == "full" and rec.get("text_len", 0) > 800:
                return rec
            t, err = E.fetch_article(rec["url"], timeout=20)
            if t and len(t) > rec.get("text_len", 0):
                rec["text"] = t
                rec["text_len"] = len(t)
                rec["text_kind"] = "full"
            elif not t:
                rec["fetch_error"] = err
            return rec

        nfull = 0
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            out = list(ex.map(_full, docs))
        for rec in out:
            if rec.get("text_kind") == "full":
                nfull += 1
        with io.open(raw_path, "w", encoding="utf-8") as f:
            for rec in out:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tl = sorted(r["text_len"] for r in out)
        print("    全文 %d / %d 篇，长度中位 %d 字"
              % (nfull, len(out), tl[len(tl) // 2] if tl else 0))

    print("")
    print("  原始层新增 %d 篇，累计 %d 篇" % (n_new, len(seen)))
    print("  耗时 %.0f 秒" % (time.time() - t0))
    print("  已写出 → %s" % raw_path)


if __name__ == "__main__":
    main()
