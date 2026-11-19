"""把 RSS 探测结果填进信源主表，产出调度器直接能用的表。

为什么要这一步
--------------
合并表的「入口URL」列只有 12 条有值，其余为空。
而调度器的全部逻辑都依赖 feed_url —— 没有入口的源只能算"无入口"跳过。

之前 probe_feeds.py 探过 687 个可用入口，但那是针对旧的 1178 条表。
现在合并表是 1487 条，要重新对一遍：
  - 探测结果里有的 → 填进去
  - 探测结果里没有的 → 留空，标记"待探测"

这样调度器能立刻跑起来，未探的部分边跑边补。
"""
import argparse
import io
import json
import os

import openpyxl


def norm(x):
    return str(x or "").strip().lower().replace("www.", "").rstrip("/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True, help="合并后的信源主表")
    ap.add_argument("--probe", required=True, help="probe_feeds 的输出 json")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    # 探测结果：{domain: {kind, url, items, ...}}
    probe = json.load(io.open(a.probe, encoding="utf-8"))
    pmap = {norm(k): v for k, v in probe.items()}

    wb = openpyxl.load_workbook(a.registry, data_only=True)
    ws = wb[max(wb.sheetnames, key=lambda s: wb[s].max_row)]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}

    ci_dom = ix.get("主域名")
    ci_url = ix.get("入口URL")
    ci_kind = ix.get("入口类型")
    ci_st = ix.get("状态")

    n_fill = n_has = n_none = 0
    out_rows = []
    for r in rows[1:]:
        if not r or not r[ci_dom]:
            continue
        rec = list(r)
        d = norm(rec[ci_dom])
        url = str(rec[ci_url] or "").strip() if ci_url is not None else ""
        p = pmap.get(d)
        if not url and p and p.get("url") and p.get("kind"):
            rec[ci_url] = p["url"]
            if ci_kind is not None:
                rec[ci_kind] = p["kind"]
            if ci_st is not None and not rec[ci_st]:
                rec[ci_st] = "启用"
            url = p["url"]
            n_fill += 1
        if url:
            n_has += 1
        else:
            n_none += 1
            if ci_st is not None and not rec[ci_st]:
                rec[ci_st] = "待探测"
        out_rows.append(rec)

    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "信源主表"
    ws2.append(hdr)
    for r in out_rows:
        ws2.append(r)
    ws2.freeze_panes = "A2"
    wb2.save(a.out)

    print("")
    print("  信源主表      %d 条" % len(out_rows))
    print("  本次填入入口   %d 条" % n_fill)
    print("  有入口合计     %d 条" % n_has)
    print("  无入口（待探测）%d 条" % n_none)
    print("")
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
