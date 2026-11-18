#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ACLED 采集：补 UCDP 收不到的非致命事件与未遂图谋。

为什么需要这一路
----------------
UCDP GED 的收录条件是「有组织的、造成死亡的暴力事件」。于是：
  · 非致命袭击（0 死）   基本不收
  · 未遂、被挫败的图谋    不收
  · 图谋被破获、零伤亡    不收
而本项目口径明确要求「预谋被破获、未遂、零伤亡的案子照常计入」。
实测 2025-08：只跑 UCDP，台账 247 行里死亡=0 的只有 0 行。

ACLED 比 UCDP 多出来的三样东西
------------------------------
1. **零死亡事件**（fatalities=0）。ACLED 收「政治暴力与抗议」，不以死亡为门槛。
2. **actor1 / actor2 语义明确**：actor1 是发起方、actor2 是承受方。
   UCDP 的 side_a/side_b 只表示「冲突双方」，看不出谁先动手 —— 这是判定链
   最容易判反的地方（2026-07 真值集里 60 多条 actor 写成 Government of X）。
3. **civilian_targeting 与 tags**：直接标明是否针对平民、是否属恐怖活动。

API 注意事项（2026-11 实测）
---------------------------
  · 端点 https://acleddata.com/api/acled/read ，OAuth 走 /oauth/token
  · **`page` 参数已废弃**，传了直接 400。分页要用响应里的 `next_cursor`
    （整数），下一请求传 `cursor=<值>`，直到 next_cursor 为空。
    网上多数教程和你手上旧脚本用的还是 page，现在全部跑不通。
  · 单页上限 5000 条
  · `total_count` 经常返回 null，不能靠它估算总量，只能翻到 cursor 为空

凭据
----
优先级：命令行 --username/--password > 环境变量 > --env-file 指定的 .env
注册（免费）：https://developer.acleddata.com/
不要把凭据写进脚本或提交进仓库。

用法
----
  # 按月查全球
  python scripts/harvest_acled.py --month 2025-08 --out 候选_acled.jsonl

  # 从 .env 读凭据
  python scripts/harvest_acled.py --month 2025-08 --out a.jsonl --env-file .env

  # 只查某国（调试用）
  python scripts/harvest_acled.py --month 2025-08 --out a.jsonl --country "Nigeria"

  # 只看能取到多少，不写文件
  python scripts/harvest_acled.py --month 2025-08 --count-only

退出码：0 = 成功且有数据 | 2 = 成功但 0 条 | 1 = 凭据/网络/参数错误
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

TOKEN_URL = "https://acleddata.com/oauth/token"
READ_URL = "https://acleddata.com/api/acled/read"
PAGE_LIMIT = 5000

# ACLED event_type / sub_event_type → 中文袭击方式
ACT_MAP = {
    "Explosions/Remote violence": "爆炸/远程打击",
    "Battles": "武装冲突",
    "Violence against civilians": "针对平民暴力",
    "Riots": "骚乱",
    "Protests": "抗议",
    "Strategic developments": "战略动态",
}
SUB_ACT_MAP = {
    "Attack": "袭击",
    "Armed clash": "武装交火",
    "Shelling/artillery/missile attack": "炮击/导弹",
    "Air/drone strike": "空袭",
    "Remote explosive/landmine/IED": "爆炸装置/地雷",
    "Suicide bomb": "自杀式爆炸",
    "Grenade": "手榴弹",
    "Government regains territory": "政府收复",
    "Non-state actor overtakes territory": "非国家方夺取",
    "Violence against civilians": "针对平民暴力",
    "Abduction/forced disappearance": "绑架/强迫失踪",
    "Sexual violence": "性暴力",
}


def clean_env():
    """清空代理变量 —— 很多机器设了指向未启动代理的 HTTP_PROXY，会让所有请求失败。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


def read_env_file(path):
    """读 .env（KEY=VALUE，# 注释）。不依赖 python-dotenv。"""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    for line in io.open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_creds(a):
    u = a.username or os.environ.get("ACLED_USERNAME")
    p = a.password or os.environ.get("ACLED_PASSWORD")
    if not (u and p):
        envs = read_env_file(a.env_file)
        u = u or envs.get("ACLED_USERNAME")
        p = p or envs.get("ACLED_PASSWORD")
    return u, p


def get_token(session, user, pwd):
    r = session.post(
        TOKEN_URL,
        data={"username": user, "password": pwd, "grant_type": "password",
              "client_id": "acled", "scope": "authenticated"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError("取 token 失败 HTTP %s：%s" % (r.status_code, r.text[:200]))
    tok = (r.json() or {}).get("access_token")
    if not tok:
        raise RuntimeError("响应里没有 access_token：%s" % r.text[:200])
    return tok


def fetch_month(session, headers, month, country=None, max_pages=200, verbose=True):
    """按月份拉全部事件，cursor 分页。返回 list[dict]。"""
    y, m = month.split("-")
    import calendar
    last = calendar.monthrange(int(y), int(m))[1]
    params = {
        "_format": "json",
        "event_date": "%s-%s-01|%s-%s-%02d" % (y, m, y, m, last),
        "event_date_where": "BETWEEN",
        "limit": PAGE_LIMIT,
    }
    if country:
        params["country"] = country

    rows, cursor, page = [], None, 0
    while page < max_pages:
        page += 1
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        try:
            r = session.get(READ_URL, params=p, headers=headers, timeout=180)
        except Exception as e:  # noqa: BLE001
            print("  第 %d 页请求失败：%s" % (page, str(e)[:80]))
            break
        if r.status_code != 200:
            print("  第 %d 页 HTTP %s：%s" % (page, r.status_code, r.text[:160]))
            break
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            print("  第 %d 页返回不是 JSON" % page)
            break
        data = j.get("data") or []
        rows.extend(data)
        if verbose:
            print("  第 %d 页 +%d 条（累计 %d）" % (page, len(data), len(rows)))
        nxt = j.get("next_cursor")
        if not data or not nxt:
            break
        if nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.3)   # 轻限速，别把人家的 API 打急
    return rows


def to_unified(r, month):
    """ACLED 记录 → 本项目的统一字段。"""
    ev = r.get("event_type") or ""
    sub = r.get("sub_event_type") or ""
    act = SUB_ACT_MAP.get(sub) or ACT_MAP.get(ev) or ev
    fat = r.get("fatalities")
    try:
        fat = int(fat)
    except Exception:  # noqa: BLE001
        fat = None
    lat = r.get("latitude")
    lon = r.get("longitude")

    def _f(v):
        try:
            return float(v)
        except Exception:  # noqa: BLE001
            return None

    extra = {
        "acled_event_id": r.get("event_id_cnty"),
        "acled_event_type": ev,
        "acled_sub_event_type": sub,
        "acled_source": r.get("source"),
        "acled_tags": r.get("tags"),
        "civilian_targeting": r.get("civilian_targeting"),
        "inter1": r.get("inter1"),
        "inter2": r.get("inter2"),
        "interaction": r.get("interaction"),
        "region": r.get("region"),
        "iso": r.get("iso"),
        "lat": _f(lat),
        "lon": _f(lon),
        "time_precision": r.get("time_precision"),
    }
    return {
        "source": "acled",
        "date": (r.get("event_date") or "")[:10],
        "country": r.get("country") or "",
        "place": " ".join(x for x in [r.get("admin1"), r.get("admin2"),
                                      r.get("location")] if x),
        "summary": (r.get("notes") or "").replace("\n", " ")[:600],
        "deaths": fat,
        "injured": None,
        "actor_a": r.get("actor1") or "",
        "actor_b": r.get("actor2") or "",
        "act_type": act,
        "url": "",          # ACLED 不给单条 URL，媒体名在 source 字段
        "raw_id": r.get("event_id_cnty") or "",
        "extra": extra,
    }


def main():
    ap = argparse.ArgumentParser(description="ACLED 采集（补非致命与未遂事件）")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out", default="候选_acled.jsonl")
    ap.add_argument("--country", default=None, help="只查某国（默认全球）")
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--env-file", default=None, help="含 ACLED_USERNAME/PASSWORD 的 .env")
    ap.add_argument("--max-pages", type=int, default=200, help="最多翻多少页（默认 200）")
    ap.add_argument("--count-only", action="store_true", help="只统计不写文件")
    ap.add_argument("--min-items", type=int, default=1, help="少于这个数视为失败，exit 2")
    ap.add_argument("--event-types",
                    default="Explosions/Remote violence,Battles,Violence against civilians",
                    help="只要这些 event_type（逗号分隔）。"
                         "默认排除 Protests/Riots/Strategic developments —— "
                         "它们是政治表达不是暴力袭击，混进来会把台账撑成抗议记录表。"
                         "传 all 表示不过滤")
    a = ap.parse_args()

    if not a.month or len(a.month) != 7:
        print("--month 要写成 YYYY-MM", file=sys.stderr)
        return 1

    u, p = get_creds(a)
    if not (u and p):
        print("✗ 没找到 ACLED 凭据。三种给法：", file=sys.stderr)
        print("   1) 环境变量 ACLED_USERNAME / ACLED_PASSWORD", file=sys.stderr)
        print("   2) --env-file .env （内含 ACLED_USERNAME=... / ACLED_PASSWORD=...）", file=sys.stderr)
        print("   3) --username / --password", file=sys.stderr)
        print("   没账号先注册（免费）：https://developer.acleddata.com/", file=sys.stderr)
        return 1

    try:
        import requests
    except ImportError:
        print("需要 requests：pip install requests", file=sys.stderr)
        return 1

    s = requests.Session()
    s.trust_env = False      # 不用系统代理
    s.proxies = {}

    print("ACLED 采集：%s%s" % (a.month, (" / " + a.country) if a.country else " / 全球"))
    try:
        tok = get_token(s, u, p)
    except Exception as e:  # noqa: BLE001
        print("✗ %s" % str(e)[:200], file=sys.stderr)
        return 1
    print("  凭据有效")

    headers = {"Authorization": "Bearer " + tok, "Accept": "application/json"}
    raw = fetch_month(s, headers, a.month, a.country, a.max_pages)
    print("  共取到 %d 条" % len(raw))

    if not raw:
        print("✗ 该月 0 条。可能原因：月份超出 ACLED 覆盖、或该月确实无记录", file=sys.stderr)
        return 2

    # 事件类型过滤：ACLED 含大量抗议/骚乱/战略动态，不是暴力袭击。
    # 实测 2025-08 全球：25000 条里 12769 条是这三类，占一半以上。
    keep_types = None
    if a.event_types and a.event_types.strip().lower() != "all":
        keep_types = {t.strip() for t in a.event_types.split(",") if t.strip()}
        before = len(raw)
        raw = [r for r in raw if (r.get("event_type") or "") in keep_types]
        print("  类型过滤：%d → %d 条（去掉抗议/骚乱/战略动态）" % (before, len(raw)))
        if not raw:
            print("✗ 过滤后 0 条。检查 --event-types", file=sys.stderr)
            return 2

    rows = [to_unified(r, a.month) for r in raw]
    # 只保留目标月（防止 API 边界返回邻月数据）
    rows = [r for r in rows if r["date"].startswith(a.month)]

    from collections import Counter
    nz = sum(1 for r in rows if (r["deaths"] or 0) == 0)
    print()
    print("  目标月记录：%d 条" % len(rows))
    print("  零死亡（UCDP 收不到的）：%d 条（%.1f%%）"
          % (nz, 100.0 * nz / len(rows) if rows else 0))
    print("  事件类型：%s" % dict(Counter(r["extra"]["acled_event_type"] for r in rows).most_common(6)))
    print("  含「Terrorist」标签：%d 条"
          % sum(1 for r in rows if "terror" in str(r["extra"].get("acled_tags") or "").lower()))
    print("  针对平民：%d 条" % sum(1 for r in rows if r["extra"].get("civilian_targeting")))

    if a.count_only:
        return 0

    if len(rows) < a.min_items:
        print("✗ 少于 --min-items=%d" % a.min_items, file=sys.stderr)
        return 2

    d = os.path.dirname(os.path.abspath(a.out))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(a.out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print()
    print("  已写出 → %s" % a.out)
    print()
    print("  下一步：合并进候选池")
    print("    python scripts/run_pipeline.py --month %s --ucdp ged.zip \\" % a.month)
    print("        --acled %s --outdir ./out" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
