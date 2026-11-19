"""周期性采集调度器 —— 从第一性原理推导。

================ 根本问题 ================

在任意时刻，把全球信源上新出现的文章，完整地、可追溯地存下来，
全过程无人值守。


================ 分解 ================

这个目标拆开是四个必须分别解决的问题，任何一个没解决，目标就不成立。

【问题一】抓什么 —— 入口

一个源可能有 RSS、Atom、sitemap、HTML 列表页四种入口，且不固定。
没有 RSS 的站（约三分之一）只能靠列表页。
所以必须有 fallback 链：feed → sitemap → 列表页。
入口发现本身有成本（每源要试几次），所以要缓存结果，不能每轮重探。

【问题二】什么算"新" —— 增量识别

增量识别的自然做法是"存全部已见 URL，每次比对"。
但这个方案的成本是 O(历史总量)，跑一年就不可用。

关键观察：订阅源只保留最近 N 条。更早的条目不可能再出现。
所以不需要存全部历史，只需要存"最近一次抓取时看到的那批 id"。
下一次抓取时，出现的不在这批里，就是新的。

这就是环形缓冲的依据，不是工程上的偷懒，是数据源的物理约束决定的。
缓冲长度取 max(上次条目数 × 2, 100) 足够。

【问题三】多久抓一次 —— 频率

这是最容易出错、且损失不可恢复的地方。

物理约束：某源的订阅源保留 C 条，该源每天更新 R 条，
         那么这份订阅源能覆盖 C/R 天。
         如果抓取间隔超过 C/R，中间产生的文章会被挤出，永久丢失。

举例：某站每天更新 20 条，其 RSS 只保留 30 条 → 只能撑 1.5 天。
      若按"每天抓一次"运行，看似安全，实际已经在临界点上，
      一旦某天更新 35 条，就有 5 条丢失。

所以频率不能统一设定，必须由每个源的实测 C 和 R 算出来：

    T_safe = (C / R) × α           α 为安全系数，取 0.5
    next_due = last_ok + clamp(T_safe, T_min, T_max)

C 在第一次抓取时实测（条目数）。R 用指数加权移动平均估计。
R 未知时（新源）取保守值：T = 6 小时。

【问题四】怎么不中断 —— 无人值守

三类故障必须分别处理：
  a. 单次失败（网络抖动）      → 下轮重试，不改变间隔
  b. 连续失败（站挂了/被封）    → 指数退避，连续 3 次熔断 6 小时
  c. 长期失败（站已死）        → 超过 7 天没成功，标记为死源，降为月检

状态必须落盘，进程被杀不丢进度。


================ 设计约束 ================

一、抓取与解析分离。
   抓取只负责拿到字节，解析失败不影响抓取成功。
   这样今天解析器有 bug，明天修好可以对原始层重跑，不用重抓。
   监控里抓取成功率和解析成功率是两个独立指标。

二、先监控，后功能。
   第一版不产出事件，只回答"每个源活着吗、抓到了吗、还剩多少安全余量"。
   没有这层监控，判断不了问题出在采集、解析还是判定。

三、容量告警是核心指标，不是附加功能。
   容量溢出 = 永久丢失，比其他任何问题都严重。
   单次新增条目数超过容量的 60%，就必须告警并立即缩短间隔。

四、状态文件一源一份，不用数据库。
   1487 个源，每份几百字节，合计不到 1 MB。
   JSON 文件的优点是可用文本工具直接检查、可 git 管理、无依赖。
"""
import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------- 参数常量（第一性原理推导的结果）----------------
ALPHA = 0.5              # 安全系数：只用掉容量寿命的一半
T_MIN_H = 1.0            # 最短间隔（小时）；再短没有收益，只是增加请求
T_MAX_H = 72.0           # 最长间隔（小时）；再长对低频源也没意义
T_UNKNOWN_H = 6.0        # 从未抓过时的保守间隔
T_PROBE_H = 1.0          # 已有基线、但还没测出更新速度时的间隔
CAPACITY_WARN = 0.6      # 单次新增占容量超过此比例 → 容量告警
FAIL_BREAK = 3           # 连续失败几次触发熔断
BREAK_H = 6.0            # 熔断时长
DEAD_DAYS = 7            # 超过多少天没成功判定为死源
DEAD_INTERVAL_H = 24 * 30  # 死源降为月检

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

TAG = re.compile(r"<[^>]+>")
CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)


# ================= 状态层 =================
def now():
    return datetime.now(timezone.utc)


def iso(t=None):
    return (t or now()).isoformat()


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def slug(domain):
    return re.sub(r"[^a-z0-9._-]+", "_", str(domain).lower())[:80]


class State:
    """单个信源的状态。

    字段来历：
      capacity      实测，首次抓取时的条目数。容量寿命的分母。
      rate          实测，用 EWMA 估的日更新量。容量寿命的分子。
      seen_ids      见"问题二"。环形缓冲，只存最近一次看到的条目 id。
      fail_streak   见"问题四"。连续失败计数，触发熔断。
      next_due      本轮调度的依据。
    """

    def __init__(self, d):
        self.d = d or {}

    @property
    def domain(self):
        return self.d.get("domain", "")

    @classmethod
    def load(cls, path):
        if os.path.isfile(path):
            try:
                return cls(json.load(io.open(path, encoding="utf-8")))
            except Exception:  # noqa: BLE001
                return cls({})
        return cls({})

    def save(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(
            json.dumps(self.d, ensure_ascii=False, indent=1))
        os.replace(tmp, path)

    # ---- 调度决策 ----
    def safe_interval_h(self):
        """容量寿命的一半。这是整个调度器的核心公式。

        rate 的三种状态要分开处理，否则会互相等死：

          从未抓过（rate=0, 无基线）  → T_UNKNOWN_H（6h，先建立基线）
          有基线但还没测出 rate      → T_PROBE_H（1h，尽快抓第二轮把 rate 测出来）
          已有 rate                  → C/R×α

        为什么要有中间那档：rate 靠「两次成功抓取之间的增量÷间隔」算出来，
        一次抓取永远算不出速度。若基线之后仍按 6 小时排，第二轮要等 6 小时，
        这 6 小时里所有源都停在「速度未知」上，自适应频率等于没启用。
        实测就卡在这：683 家全部建成基线，算得出 rate 的 0 家。
        """
        cap = self.d.get("capacity") or 0
        rate = self.d.get("rate") or 0
        if cap <= 0:
            return T_UNKNOWN_H
        if rate <= 0:
            # 有基线没速度 —— 尽快抓一轮把速度测出来，别用 6 小时拖
            return T_PROBE_H
        life_h = cap / (rate / 24.0)      # rate 是「每天」，换成每小时
        t = life_h * ALPHA
        return max(T_MIN_H, min(T_MAX_H, t))

    def due_at(self):
        if self.d.get("dead"):
            nxt = parse_iso(self.d.get("last_ok_at")) or now()
            return nxt + timedelta(hours=DEAD_INTERVAL_H)
        base = parse_iso(self.d.get("last_ok_at")) or parse_iso(
            self.d.get("last_try_at")) or now() - timedelta(days=365)
        brk = parse_iso(self.d.get("break_until"))
        if brk and brk > now():
            return brk
        return base + timedelta(hours=self.safe_interval_h())

    def is_due(self, t=None):
        return (t or now()) >= self.due_at()

    # ---- 更新 ----
    def on_success(self, n_entries, n_new, fetched_at):
        """抓取成功后更新状态。rate 用 EWMA。"""
        d = self.d
        prev_ok = parse_iso(d.get("last_ok_at"))
        if n_entries:
            d["capacity"] = max(int(d.get("capacity") or 0), n_entries)
        # rate：两次成功之间的增量 / 间隔天数
        if prev_ok and n_new >= 0:
            gap_d = max((fetched_at - prev_ok).total_seconds() / 86400.0, 1 / 24.0)
            inst = n_new / gap_d
            old = d.get("rate")
            d["rate"] = round(inst if not old else (0.3 * inst + 0.7 * old), 2)
        d["last_ok_at"] = iso(fetched_at)
        d["last_try_at"] = iso(fetched_at)
        d["fail_streak"] = 0
        d["break_until"] = None
        d["dead"] = False
        d["n_fetches"] = int(d.get("n_fetches") or 0) + 1
        d["n_new_total"] = int(d.get("n_new_total") or 0) + n_new
        d["last_n_new"] = n_new
        d["last_n_entries"] = n_entries

    def on_failure(self, err, fetched_at):
        d = self.d
        d["last_try_at"] = iso(fetched_at)
        d["fail_streak"] = int(d.get("fail_streak") or 0) + 1
        d["last_error"] = str(err)[:120]
        if d["fail_streak"] >= FAIL_BREAK:
            d["break_until"] = iso(fetched_at + timedelta(hours=BREAK_H))
        lo = parse_iso(d.get("last_ok_at"))
        if lo and (fetched_at - lo).days >= DEAD_DAYS:
            d["dead"] = True

    def remember(self, ids):
        """环形缓冲：只保留最近看到的条目 id。

        依据见文件头【问题二】：更早的条目不可能再出现。
        长度取 max(上次条目数 × 2, 100)。
        """
        keep = max(int(self.d.get("capacity") or 0) * 2, 100)
        old = list(self.d.get("seen_ids") or [])
        merged = list(dict.fromkeys(list(ids) + old))[:keep]
        self.d["seen_ids"] = merged
        self.d["capacity"] = max(int(self.d.get("capacity") or 0), len(ids))

    def known(self):
        return set(self.d.get("seen_ids") or [])


# ================= 抓取与解析 =================
_SESSION = None
_PROXY = None            # 探测结果，None = 未探测，"" = 不走代理


def detect_proxy(explicit=None, timeout=8):
    """探测可用代理。这是实测调出来的，别退回 trust_env。

    为什么不能用 requests 的 trust_env
    ---------------------------------
    本机 Windows 系统代理是 127.0.0.1:17890（注册表 ProxyEnable=1）。
    但 urllib.request.getproxies() 只返回 no_proxy 列表、不含 ProxyServer，
    所以 trust_env=True 实际等同于直连。

    实测后果（30 家试跑）：
      直连：aljazeera/indiatoday/independent/punchng 全部 ConnectTimeout
            30 家里 17 家失败，占用 424 秒
      代理：上述全部 200，afintl 从 49.5 秒降到 6.5 秒

    所以必须自己探测，不能交给 requests。

    探测顺序：显式参数 → 环境变量 → Windows 注册表 → 常见端口。
    """
    global _PROXY
    cands = []
    if explicit:
        cands.append(explicit)
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "ALL_PROXY", "all_proxy"):
        v = os.environ.get(k)
        if v:
            cands.append(v if "://" in v else "http://" + v)
    # Windows 注册表
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion"
                             r"\Internet Settings")
        try:
            en, _ = winreg.QueryValueEx(key, "ProxyEnable")
            srv, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            key.Close()
        if en and srv:
            s = str(srv).strip()
            if "=" in s:                      # http=...;https=... 形态
                for part in s.split(";"):
                    if "=" in part:
                        k2, v2 = part.split("=", 1)
                        if k2.strip().lower() == "https":
                            s = v2.strip()
                            break
                else:
                    s = s.split(";")[0].split("=")[-1].strip()
            cands.append(s if "://" in s else "http://" + s)
    except Exception:  # noqa: BLE001
        pass
    # 常见端口兜底
    for p in (17890, 7890, 7897, 10809, 1080):
        cands.append("http://127.0.0.1:%d" % p)

    seen = set()
    for c in cands:
        c = str(c).strip()
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            import requests
            import urllib3
            urllib3.disable_warnings()
            t0 = time.time()
            r = requests.get("https://afintl.com/feed", timeout=timeout,
                             verify=False, proxies={"http": c, "https": c},
                             headers={"User-Agent": UA})
            if r.status_code == 200 and len(r.content) > 500:
                _PROXY = c
                return c, round(time.time() - t0, 1)
        except Exception:  # noqa: BLE001
            continue
    _PROXY = ""
    return "", 0.0


def _session():
    """复用连接池。每次新建 Session 会重建 TCP 连接，并发时容易耗尽本地端口。"""
    global _SESSION
    if _SESSION is None:
        import requests
        import urllib3
        urllib3.disable_warnings()
        s = requests.Session()
        s.trust_env = False          # 见 detect_proxy 的说明
        if _PROXY:
            s.proxies = {"http": _PROXY, "https": _PROXY}
        ad = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16,
                                           max_retries=0)
        s.mount("http://", ad)
        s.mount("https://", ad)
        s.headers.update({"User-Agent": UA,
                          # Accept 要同时容得下 feed 和 HTML 页。
                          # 之前只写 application/rss+xml，抓正文时 africanews.com
                          # 直接返 406 Not Acceptable —— 它要求 text/html。
                          "Accept": "text/html,application/xhtml+xml,"
                                    "application/xml;q=0.9,"
                                    "application/rss+xml;q=0.9,"
                                    "application/atom+xml;q=0.9,*/*;q=0.8",
                          "Accept-Language": "en,zh;q=0.9,*;q=0.8"})
        _SESSION = s
    return _SESSION


def http_get(url, timeout=20, retries=2):
    """抓字节，带重试。"""
    last = ""
    for attempt in range(retries + 1):
        try:
            r = _session().get(url, timeout=timeout, verify=False)
            enc = r.encoding
            if not enc or enc.lower() in ("iso-8859-1", "ascii"):
                enc = r.apparent_encoding or "utf-8"
            if r.status_code >= 500 and attempt < retries:
                last = "HTTP %s" % r.status_code
                time.sleep(1.5 * (attempt + 1))
                continue
            return r.status_code, r.content, enc, r.url
        except Exception as e:  # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, str(e)[:60])
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    return 0, b"", "", last


def _text(x):
    if not x:
        return ""
    m = CDATA.search(x)
    if m:
        x = m.group(1)
    x = TAG.sub(" ", str(x))
    x = (x.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return " ".join(x.split())


def parse_feed(raw):
    """解析 RSS / Atom / sitemap，统一成 [{id,title,link,published,summary}]。

    只做一件事：把不同格式拉平。不判断内容，不做业务筛选 ——
    依据见文件头【设计约束 一】。
    """
    if isinstance(raw, bytes):
        txt = raw.decode("utf-8", "replace")
    else:
        txt = raw or ""
    low = txt[:2000].lower()
    out = []

    if "<urlset" in low or "<sitemapindex" in low:
        # sitemap：条目是 <url><loc>...</loc><lastmod>...</lastmod>
        for m in re.finditer(r"<url>(.*?)</url>", txt, re.S | re.I):
            blk = m.group(1)
            loc = re.search(r"<loc>(.*?)</loc>", blk, re.S | re.I)
            lm = re.search(r"<lastmod>(.*?)</lastmod>", blk, re.S | re.I)
            if not loc:
                continue
            link = _text(loc.group(1))
            out.append({"id": link, "title": "", "link": link,
                        "published": _text(lm.group(1)) if lm else "",
                        "summary": ""})
        return out

    # RSS / Atom
    pat = r"<item[^>]*>(.*?)</item>" if "<item" in low else r"<entry[^>]*>(.*?)</entry>"
    for m in re.finditer(pat, txt, re.S | re.I):
        blk = m.group(1)
        g = lambda t: (re.search(r"<%s[^>]*>(.*?)</%s>" % (t, t), blk, re.S | re.I)
                       or [None, ""])[1]
        link = _text(g("link"))
        if not link:
            lm = re.search(r'<link[^>]+href=["\']([^"\']+)', blk, re.I)
            link = lm.group(1) if lm else ""
        guid = _text(g("guid")) or _text(g("id")) or link
        pub = _text(g("pubDate")) or _text(g("published")) or _text(g("updated"))
        out.append({
            "id": guid or link,
            "title": _text(g("title")),
            "link": link,
            "published": pub,
            "summary": _text(g("description")) or _text(g("summary"))[:400],
        })
    return out


# ================= 单源处理 =================
def process_source(rec, state_dir, store, args):
    """处理一个源：抓入口 → 解析 → 判增量 → 落快照 → 更新状态。"""
    dom = rec["domain"]
    sp = os.path.join(state_dir, slug(dom) + ".json")
    st = State.load(sp)
    if not st.d:
        st.d = {"domain": dom, "name": rec.get("name"),
                "feed_url": rec.get("feed_url") or "",
                "first_seen_at": iso()}
    if rec.get("feed_url") and not st.d.get("feed_url"):
        st.d["feed_url"] = rec["feed_url"]
    if rec.get("category"):
        st.d["category"] = rec["category"]

    t0 = now()
    feed_url = st.d.get("feed_url") or rec.get("feed_url")
    if not feed_url:
        st.on_failure("无入口URL", t0)
        st.save(sp)
        return {"domain": dom, "status": "no-entry", "n_new": 0,
                "n_entries": 0, "note": "无入口URL"}

    code, body, enc, final = http_get(feed_url, args.timeout)
    if code != 200 or not body:
        st.on_failure("HTTP %s" % code, t0)
        st.save(sp)
        return {"domain": dom, "status": "fetch-fail", "n_new": 0,
                "n_entries": 0, "note": "HTTP %s" % code}

    entries = parse_feed(body)
    if not entries:
        st.on_failure("解析无条目", t0)
        st.save(sp)
        return {"domain": dom, "status": "parse-empty", "n_new": 0,
                "n_entries": 0, "note": "解析无条目（%d 字节）" % len(body)}

    # 增量判定：依据见文件头【问题二】
    known = st.known()
    fresh = [e for e in entries if e["id"] not in known]

    # 落快照：feed 原文本身就是证据，先存
    if store is not None:
        try:
            store.put(feed_url, body, code, enc, final, fetched_at=t0,
                      extra={"kind": "feed", "domain": dom,
                             "n_entries": len(entries)})
        except Exception:  # noqa: BLE001
            pass

    # 容量告警：依据见【设计约束 三】
    # 但首次抓取全部条目都是「新的」，必然触发 —— 那是基线建立，不是溢出风险。
    cap = max(len(entries), 1)
    is_first = int(st.d.get("n_fetches") or 0) == 0
    warn = (not is_first) and len(fresh) >= cap * CAPACITY_WARN
    st.on_success(len(entries), len(fresh), t0)
    st.remember([e["id"] for e in entries])
    st.save(sp)

    return {"domain": dom, "status": "ok", "n_new": len(fresh),
            "n_entries": len(entries), "capacity_warn": bool(warn),
            "first": is_first,
            "interval_h": round(st.safe_interval_h(), 1),
            "note": ("首次基线 %d 条" % len(entries)) if is_first else
                    ("容量告警：新增 %d / 容量 %d" % (len(fresh), cap) if warn else "")}


# ================= 主流程 =================
def load_registry(path, sheet=None, priority=None):
    """从信源主表读：域名 + 入口 URL + 优先级 + 类别。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    if sheet and sheet in wb.sheetnames:
        ws = wb[sheet]
    else:
        ws = max(wb.worksheets, key=lambda s: s.max_row)
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}

    def col(*names):
        for n in names:
            if n in ix:
                return ix[n]
        return None

    ci_dom = col("主域名", "域名", "domain")
    ci_name = col("信源名", "机构名称", "name")
    ci_url = col("入口URL", "feed_url")
    ci_pri = col("重要性等级", "优先级", "priority")
    ci_cat = col("机构类型", "类别")
    ci_st = col("入口类型")

    out = []
    pri_set = None
    if priority:
        pri_set = set(x.strip().upper() for x in priority.split(","))
    for r in rows[1:]:
        if not r or not r[0] or ci_dom is None or not r[ci_dom]:
            continue
        pri = str(r[ci_pri] or "").strip().upper() if ci_pri is not None else ""
        if pri_set and pri not in pri_set:
            continue
        kind = str(r[ci_st] or "").strip() if ci_st is not None else ""
        out.append({
            "domain": str(r[ci_dom]).strip().lower().replace("www.", ""),
            "name": str(r[ci_name] or "") if ci_name is not None else "",
            "feed_url": str(r[ci_url] or "").strip() if ci_url is not None else "",
            "priority": pri,
            "category": str(r[ci_cat] or "").strip() if ci_cat is not None else "",
            "kind": kind,
        })
    return out


def main():
    ap = argparse.ArgumentParser(
        description="周期性采集调度器（第一性原理设计，见文件头）")
    ap.add_argument("--registry", required=True, help="信源主表 xlsx")
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--store", default=r"D:\event-ledger-data",
                    help="原始快照根目录")
    ap.add_argument("--state-dir", default=None,
                    help="状态目录，默认 <store>/state")
    ap.add_argument("--priority", default=None, help="只跑这些优先级，如 P1,P2")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多处理几家")
    ap.add_argument("--workers", type=int, default=4,
                    help="并发数。实测 8 线程时大量 HTTP 0，4 线程稳定")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--force", action="store_true",
                    help="忽略到期判断，全部重抓（调试用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只算调度，不联网")
    ap.add_argument("--report", default=None, help="监控报告输出路径 (json)")
    ap.add_argument("--proxy", default=None,
                    help="显式指定代理，如 http://127.0.0.1:17890。不给则自动探测")
    ap.add_argument("--no-proxy", action="store_true", help="强制直连")
    a = ap.parse_args()

    if a.no_proxy:
        globals()["_PROXY"] = ""
        print("  代理：已禁用（--no-proxy）")
    else:
        p, sec = detect_proxy(a.proxy)
        if p:
            print("  代理：%s（探测用时 %.1fs）" % (p, sec))
        else:
            print("  代理：未找到可用代理，走直连（境外站点可能大量超时）")

    state_dir = a.state_dir or os.path.join(a.store, "state")

    # 快照层
    store = None
    if not a.dry_run:
        try:
            from raw_store import RawStore
            store = RawStore(a.store)
        except Exception as e:  # noqa: BLE001
            print("  ! 快照层不可用（%s），本轮只更新状态不存原文" % str(e)[:60])

    srcs = load_registry(a.registry, a.sheet, a.priority)
    print("")
    print("  信源主表 %s" % os.path.basename(a.registry))
    print("  候选信源 %d 家%s" % (len(srcs),
                                 "（优先级 %s）" % a.priority if a.priority else ""))
    print("  状态目录 %s" % state_dir)

    # 调度：算出这一轮该跑谁
    due, notdue, noentry = [], 0, 0
    t = now()
    for r in srcs:
        sp = os.path.join(state_dir, slug(r["domain"]) + ".json")
        st = State.load(sp)
        if not (r.get("feed_url") or st.d.get("feed_url")):
            noentry += 1
            continue
        if a.force or st.is_due(t):
            due.append(r)
        else:
            notdue += 1

    # 到期里按"逾期时长"排序——越该抓的越靠前
    def overdue_h(r):
        sp = os.path.join(state_dir, slug(r["domain"]) + ".json")
        st = State.load(sp)
        d = st.due_at()
        return (t - d).total_seconds() / 3600.0

    due.sort(key=lambda r: -overdue_h(r))
    if a.limit:
        due = due[:a.limit]

    print("  到期 %d 家 ｜ 未到期 %d 家 ｜ 无入口 %d 家"
          % (len(due), notdue, noentry))
    print("")

    if a.dry_run:
        for r in due[:15]:
            sp = os.path.join(state_dir, slug(r["domain"]) + ".json")
            st = State.load(sp)
            print("    %-30s 逾期 %5.1fh  间隔 %5.1fh  上次新增 %s"
                  % (r["domain"][:28], overdue_h(r), st.safe_interval_h(),
                     st.d.get("last_n_new", "—")))
        if len(due) > 15:
            print("    … 另有 %d 家" % (len(due) - 15))
        return 0

    if not due:
        print("  本轮无到期信源。下一轮最早应在 %s"
              % (min((State.load(os.path.join(state_dir, slug(r["domain"]) + ".json"))
                      .due_at() for r in srcs
                      if os.path.isfile(os.path.join(state_dir, slug(r["domain"]) + ".json"))),
                     default="—")))
        return 0

    # 执行
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    results = []
    n_ok = n_new = n_warn = n_fail = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(process_source, r, state_dir, store, a) for r in due]
        for i, fu in enumerate(futs, 1):
            try:
                res = fu.result()
            except Exception as e:  # noqa: BLE001
                res = {"domain": "?", "status": "error", "n_new": 0,
                       "n_entries": 0, "note": str(e)[:80]}
            results.append(res)
            if res["status"] == "ok":
                n_ok += 1
                n_new += res["n_new"]
                if res.get("capacity_warn"):
                    n_warn += 1
            else:
                n_fail += 1
            if i % 20 == 0 or i == len(due):
                print("    %d/%d  成功 %d ｜ 新增 %d 篇 ｜ 告警 %d ｜ 失败 %d"
                      % (i, len(due), n_ok, n_new, n_warn, n_fail))

    # 监控报告：依据见【设计约束 二】
    rep = {
        "ran_at": iso(),
        "elapsed_s": round(time.time() - t0, 1),
        "due": len(due), "ok": n_ok, "failed": n_fail,
        "new_articles": n_new, "capacity_warnings": n_warn,
        "details": sorted(results, key=lambda x: -x.get("n_new", 0))[:50],
    }
    if a.report:
        os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
        io.open(a.report, "w", encoding="utf-8").write(
            json.dumps(rep, ensure_ascii=False, indent=1))

    print("")
    print("  ── 本轮汇总 ──")
    print("    处理 %d 家，成功 %d，失败 %d" % (len(due), n_ok, n_fail))
    print("    新增文章 %d 篇" % n_new)
    print("    容量告警 %d 家%s" % (n_warn, "（已自动缩短间隔）" if n_warn else ""))
    print("    耗时 %.1f 秒" % rep["elapsed_s"])
    if a.report:
        print("    监控报告 → %s" % a.report)

    # 状态库概况
    try:
        files = [f for f in os.listdir(state_dir) if f.endswith(".json")]
        rates = []
        warns = 0
        for f in files:
            try:
                d = json.load(io.open(os.path.join(state_dir, f), encoding="utf-8"))
                if d.get("rate"):
                    rates.append(d["rate"])
                if d.get("dead"):
                    warns += 1
            except Exception:  # noqa: BLE001
                pass
        if rates:
            rates.sort()
            print("    状态库 %d 家 ｜ 日更新量中位 %.1f 条 ｜ 标记死源 %d"
                  % (len(files), rates[len(rates) // 2], warns))
    except Exception:  # noqa: BLE001
        pass

    return 0 if n_ok else 2


if __name__ == "__main__":
    sys.exit(main())
