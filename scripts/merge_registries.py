"""合并两份信源表，产出主表。

发现
----
手上有两份互不包含的信源表：

  A 信源总表_优化版_v1.xlsx   1178 条 × 20 列
    侧重本地媒体，字段丰富（回溯能力、入口类型、入口URL、状态、圈层）
    独有 42 个，都是恐袭高发国的本地源：
      aawsat.com / abamako.com（马里）/ actuniger.com（尼日尔）
      caboligado.com（莫桑比克）/ actucameroun.com（喀麦隆）

  C default_registry.xlsx     1446 条 × 8 列
    侧重国际媒体与通讯社，字段少但有优先级
    独有 310 个，其中 P1 级 37 个：
      afp.com / dpa.com / efe.com / kyodonews.net / yna.co.kr
      cnn.com / nytimes.com / bloomberg.com / ft.com / haaretz.com

重叠 1136 个。并集约 1488 个机构。

为什么要合并
------------
国际通讯社是大量本地报道的原始出处。缺了 AFP、DPA、EFE，
很多本地媒体的稿子就找不到源头。缺了 42 个本地源，
恐袭高发国（马里、尼日尔、喀麦隆、莫桑比克）就没有一手报道。

所以不能二选一，必须并集。

输出
----
一张合并主表，保留 A 的全部 20 列（字段丰富），
补上 C 独有的行，并把 C 的「机构类型」「优先级」带过来。
"""
import argparse
import io
import os

import openpyxl

A_COLS = ["source_id", "信源名", "主域名", "注册域", "国家/区域", "语种",
          "语种置信度", "圈层", "采集频率", "重要性等级", "2025数据量",
          "回溯能力", "入口类型", "入口URL", "状态", "上线批次",
          "责任人", "数据来源", "问题标注", "备注"]
OUT_COLS = A_COLS + ["机构类型", "优先级", "来源表"]


def norm(x):
    return str(x or "").strip().lower().replace("www.", "")


def read_a(p):
    wb = openpyxl.load_workbook(p, data_only=True)
    ws = wb["信源总表"] if "信源总表" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        rec = {c: (r[ix[c]] if c in ix else None) for c in A_COLS}
        rec["来源表"] = "A_优化版"
        out.append(rec)
    return out


def read_c(p):
    wb = openpyxl.load_workbook(p, data_only=True)
    ws = wb["信源表"] if "信源表" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    ix = {h: i for i, h in enumerate(hdr)}
    out = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        rec = {
            "source_id": r[ix.get("机构ID", 0)],
            "信源名": r[ix.get("机构名称", 1)],
            "主域名": r[ix.get("主域名", 2)],
            "注册域": r[ix.get("主域名", 2)],
            "国家/区域": r[ix.get("所在国", 4)],
            "语种": r[ix.get("语种", 5)],
            "圈层": None,
            "采集频率": None,
            "重要性等级": r[ix.get("优先级", 6)],
            "回溯能力": "待探测",
            "入口类型": "待探测",
            "入口URL": None,
            "状态": "观察",
            "机构类型": r[ix.get("机构类型", 3)],
            "优先级": r[ix.get("优先级", 6)],
            "来源表": "C_内置",
        }
        out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--c", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    A = read_a(a.a)
    C = read_c(a.c)
    seen = {norm(r["主域名"]) for r in A if r["主域名"]}

    added = 0
    for r in C:
        d = norm(r["主域名"])
        if not d or d in seen:
            continue
        seen.add(d)
        A.append(r)
        added += 1

    # 写
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "信源主表"
    ws.append(OUT_COLS)
    for r in A:
        ws.append([r.get(c) for c in OUT_COLS])
    ws.freeze_panes = "A2"
    wb.save(a.out)

    print("")
    print("  A 优化版    %d 条" % len(read_a(a.a)))
    print("  C 内置版    %d 条" % len(C))
    print("  重叠         %d 条" % (len(C) - added))
    print("  C 新增       %d 条" % added)
    print("  ---------------")
    print("  合并后       %d 条" % len(A))
    print("")
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
