"""每日运行监控：判断问题出在采集、解析还是别处。

为什么单独做监控
----------------
用户要求（2026-11-19）：
「先产出运行监控，不急于产出正式事件。每日记录抓取成功率、新增文章数、
  RSS 容量告警、候选数量、人工确认率和各源失败情况。这样才能判断问题
  究竟出在采集、检索还是判别。」

这句话的重点是最后一句：监控不是为了好看，是为了**定位问题在哪一层**。

所以报告按层组织，每层给一个明确的判读方法：

  采集层  抓到了吗？        → 成功率低于 80% 说明网络或代理有问题
  解析层  抓到的是 feed 吗？ → 解析率低说明入口 URL 是假阳性
  增量层  有新的吗？        → 新增长期为 0 说明源已停更或入口是缓存页
  容量层  还剩多少余量？    → 告警说明抓取间隔不够密，正在丢文章

报告同时给「失败清单」，按错误类型分组，因为不同错误对应不同修法：
  HTTP 0      → 网络/代理
  HTTP 403    → 反爬，需要更真实的请求头或换入口
  解析无条目   → 入口 URL 错，重新探测
"""
import argparse
import glob
import io
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def load_states(state_dir):
    out = []
    for f in glob.glob(os.path.join(state_dir, "*.json")):
        try:
            out.append(json.load(io.open(f, encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return out


def classify_error(e):
    e = str(e or "")
    if e.startswith("HTTP 0") or "Timeout" in e or "Connection" in e:
        return "网络/代理"
    if "403" in e:
        return "反爬403"
    if "404" in e:
        return "入口失效404"
    if "解析无条目" in e:
        return "入口不是feed"
    if "无入口" in e:
        return "无入口URL"
    return "其他"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=r"D:\event-ledger-data\state")
    ap.add_argument("--store", default=r"D:\event-ledger-data", help="原始层根目录")
    ap.add_argument("--out", default=None, help="报告输出（.md 或 .json）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    st = load_states(a.state_dir)
    if not st:
        print("状态库为空：%s" % a.state_dir)
        return 2

    now = datetime.now(timezone.utc)
    ok = [j for j in st if not j.get("fail_streak") and j.get("last_ok_at")]
    bad = [j for j in st if j.get("fail_streak")]
    dead = [j for j in st if j.get("dead")]
    # 容量告警只对「至少抓过两轮」的源算 —— 首次抓取所有条目都是新的，
    # n_new == capacity 是基线建立，不是溢出风险。
    warn = [j for j in ok
            if int(j.get("n_fetches") or 0) > 1
            and (j.get("last_n_new") or 0) >= (j.get("capacity") or 1) * 0.6]

    # 增量必须区分「首轮基线」和「真实新增」。
    # 首轮抓取时 n_new == capacity（全部条目都是新的），那不是增量，是基线。
    # 混在一起算会得出「本轮新增 20 万篇」这种荒唐数字 —— 实测正是如此：
    # 200 家跑第二轮只新增 837 篇，报告却写 205,978。
    first = [j for j in ok if int(j.get("n_fetches") or 0) <= 1]
    warm = [j for j in ok if int(j.get("n_fetches") or 0) > 1]
    n_baseline = sum(j.get("last_n_new") or 0 for j in first)
    n_new = sum(j.get("last_n_new") or 0 for j in warm)
    n_total_new = sum(j.get("n_new_total") or 0 for j in st)
    caps = sorted(j.get("capacity") or 0 for j in ok if j.get("capacity"))
    rates = sorted(j.get("rate") for j in ok if j.get("rate"))

    # 未来 24 小时内到期的：按每源的 safe_interval 估算
    def interval_of(j):
        cap = j.get("capacity") or 0
        r = j.get("rate") or 0
        if cap <= 0 or r <= 0:
            return 6.0
        return max(1.0, min(72.0, (cap / (r / 24.0)) * 0.5))

    due24 = 0
    for j in st:
        if j.get("dead"):
            continue
        la = j.get("last_ok_at") or j.get("last_try_at")
        if not la:
            due24 += 1
            continue
        try:
            t = datetime.fromisoformat(str(la).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            due24 += 1
            continue
        if (now - t).total_seconds() / 3600.0 + 24 >= interval_of(j):
            due24 += 1

    errs = Counter(classify_error(j.get("last_error")) for j in bad)
    rate_ok = 100.0 * len(ok) / max(1, len(st))

    rep = {
        "generated_at": now.isoformat(),
        "sources": {
            "total": len(st), "ok": len(ok), "failed": len(bad),
            "dead": len(dead), "success_rate": round(rate_ok, 1),
        },
        "volume": {
            "new_last_run": n_new, "baseline": n_baseline,
            "new_cumulative": n_total_new,
            "median_capacity": caps[len(caps) // 2] if caps else 0,
            "median_daily_rate": round(rates[len(rates) // 2], 1) if rates else 0,
        },
        "health": {
            "capacity_warnings": len(warn), "due_next_24h": due24,
        },
        "failures_by_cause": dict(errs),
        "top_failures": [
            {"domain": j.get("domain"), "error": j.get("last_error"),
             "cause": classify_error(j.get("last_error")),
             "feed_url": j.get("feed_url")}
            for j in sorted(bad, key=lambda x: str(x.get("domain")))[:40]
        ],
        "top_yield": [
            {"domain": j.get("domain"), "new": j.get("last_n_new"),
             "capacity": j.get("capacity"), "rate": j.get("rate")}
            for j in sorted(ok, key=lambda x: -(x.get("last_n_new") or 0))[:20]
        ],
    }

    if a.json:
        s = json.dumps(rep, ensure_ascii=False, indent=1)
        if a.out:
            io.open(a.out, "w", encoding="utf-8").write(s)
            print("已写出 → %s" % a.out)
        else:
            print(s)
        return 0

    # 文本报告
    L = []
    L.append("")
    L.append("  采集运行监控    %s" % now.strftime("%Y-%m-%d %H:%M UTC"))
    L.append("  " + "=" * 62)
    L.append("")
    L.append("  ── 一、采集层：抓到了吗 ──")
    L.append("    信源状态库      %d 家" % len(st))
    L.append("    最近一次成功     %d 家 ｜ 失败 %d 家 ｜ 标记死源 %d 家"
             % (len(ok), len(bad), len(dead)))
    L.append("    成功率          %.1f%%%s" % (
        rate_ok, "" if rate_ok >= 80 else "   ← 低于 80%，先查网络与代理"))
    L.append("")
    L.append("  ── 二、解析层：抓到的是 feed 吗 ──")
    n_parse = errs.get("入口不是feed", 0)
    L.append("    入口不是 feed    %d 家%s" % (
        n_parse, "" if n_parse == 0 else "   ← 这些入口 URL 是探测假阳性，需重新探测"))
    L.append("")
    L.append("  ── 三、增量层：有新的吗 ──")
    # 标签必须写准。「本轮新增」是错的说法 ——
    # 每个源的「最近一轮」发生在不同时刻（自适应间隔决定的），
    # 把它们加起来不等于「我们本轮抓到 10 万篇」。
    # 实测踩过：状态迁移后报告显示「本轮新增 100,213 篇」，
    # 实际是几十个源各自最近一次的增量拼在一起。
    L.append("    各源最近一次新增合计 %d 篇（%d 家已跑过两轮以上）"
             % (n_new, len(warm)))
    L.append("    首轮基线        %d 条（%d 家只跑过一次，不算增量）"
             % (n_baseline, len(first)))
    L.append("    累计入库        %d 条" % n_total_new)
    L.append("    订阅源容量中位   %d 条" % rep["volume"]["median_capacity"])
    L.append("    日更新量中位     %.1f 条" % rep["volume"]["median_daily_rate"])
    L.append("")
    L.append("  ── 四、容量层：还剩多少余量 ──")
    L.append("    容量告警        %d 家%s" % (
        len(warn), "" if len(warn) == 0 else "   ← 抓取间隔不够密，正在丢文章"))
    L.append("    未来 24h 到期   %d 家" % due24)
    L.append("")
    L.append("  ── 五、失败归因 ──")
    for k, n in errs.most_common():
        L.append("    %-14s %d 家" % (k, n))
    if rep["top_failures"]:
        L.append("")
        L.append("    失败清单（前 10）：")
        for x in rep["top_failures"][:10]:
            L.append("      %-24s %-12s %s"
                     % (str(x["domain"])[:22], x["cause"],
                        str(x["error"])[:36]))
    L.append("")
    L.append("  ── 六、产出最高的源（前 10）──")
    for x in rep["top_yield"][:10]:
        L.append("    %-26s 新增 %4s ｜ 容量 %4s ｜ 日更 %s"
                 % (str(x["domain"])[:24], x["new"], x["capacity"], x["rate"]))
    L.append("")

    # ── 七、正文层 ──
    # 用户 2026-11-20 指出：入口覆盖、正文获取率、采集成功率这三个数
    # "都不能直接代表全球恐怖主义事件发现率"。所以这一节只报事实，
    # 不给汇总成一个"总成功率"—— 那会把三种不同的失败混成一个数。
    L.append("  ── 七、正文层：候选抓到全文了吗 ──")
    try:
        import glob as _g
        import os as _os
        n_full = n_snip = n_fail = 0
        for mp in _g.glob(_os.path.join(a.store, "raw/meta", "*", "*.json")):
            try:
                m = json.load(io.open(mp, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if m.get("kind") != "article":
                continue
            if m.get("evidence_kind") == "search_snippet":
                n_snip += 1
                continue
            # 判"有全文"要看 failed 标记和实际字节数，两个都要。
            # 只看 n_bytes 会把「抓到了但 meta 没记字节数」的误判成失败 ——
            # 实测正文快照 13,197 篇、全文证据却报 0 篇，就是这个原因。
            if m.get("failed"):
                n_fail += 1
                continue
            nb = int(m.get("n_bytes") or 0)
            if not nb:
                for v in (m.get("versions") or []):
                    nb = max(nb, int(v.get("bytes") or 0))
            if nb >= 500:
                n_full += 1
            else:
                n_fail += 1
        tot = n_full + n_snip + n_fail
        if tot:
            L.append("    全文证据        %d 篇（%.1f%%）"
                     % (n_full, 100.0 * n_full / tot))
            L.append("    仅摘要证据      %d 篇（%.1f%%）"
                     % (n_snip, 100.0 * n_snip / tot))
            L.append("    两次都没拿到    %d 篇（%.1f%%）"
                     % (n_fail, 100.0 * n_fail / tot))
        # 弃抓数：失败达上限、不再排队的
        dead = 0
        for mp in _g.glob(_os.path.join(a.store, "raw/meta", "*", "*.json")):
            try:
                m = json.load(io.open(mp, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if int(m.get("fail_count") or 0) >= int(
                    _os.environ.get("ARTICLE_MAX_FAIL", "3")):
                dead += 1
        if dead:
            L.append("    已弃抓          %d 篇（失败达上限，转搜索或人工）" % dead)
    except Exception:  # noqa: BLE001
        L.append("    （正文层统计不可用）")
    L.append("")

    txt = "\n".join(L)
    print(txt)
    if a.out:
        io.open(a.out, "w", encoding="utf-8").write(txt)
        print("  已写出 → %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
