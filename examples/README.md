# 示例：2026 年 8 月台账（端到端产物）

这个目录是**用本 skill 真实跑出来的完整产物**，不是手写的示意。
月份故意换成 8 月（不是开发时用的 7 月），用来证明「换个月份也能跑通」。

## 怎么复现

```bash
# 1) 起台账（19 列表头）
python scripts/make_ledger.py --template 19列 \
       --rows examples/rows_2026_08.json --out examples/台账_2026-08.xlsx
python scripts/make_ledger.py --check examples/台账_2026-08.xlsx

# 2) 数字对账
python scripts/gate_number_drift.py examples/台账_2026-08.xlsx --rows 2-4 --expect 76:0

# 3) 起 PPT 并填数（数字从台账算，不手抄）
cp assets/templates/deck-single-file.html examples/index.html
python scripts/fill_deck.py --deck examples/index.html \
       --from-ledger examples/台账_2026-08.xlsx --out examples/index.html
python scripts/gate_single_file.py examples/index.html --pages 5
```

## 产物清单

| 文件 | 是什么 |
|---|---|
| `台账_2026-08.xlsx` | 主台账，19 列，3 条计入事件 |
| `台账_2026-08_飞书对齐版.xlsx` | 同一份内容的发布版（列序对齐飞书多维表格） |
| `排除清单_2026-08.xlsx` | 被排除的事件（同样 19 列），附排除理由与落在哪一步 |
| `index.html` | 单文件网页 PPT，双击即开，完全离线 |
| `报告-1-遍历性.md` | 该看的信源查了没有 |
| `报告-2-真实性.md` | 事件是真的吗、有没有虚构 |
| `报告-3-匹配性.md` | 符合口径吗、命中哪条判据 |
| `rows_2026_08.json` | 台账数据源（含原句证据），便于复现 |

## 这个示例的边界（必须知道）

- **规模是小规模演示模式**：3 条事件、5 家定向信源，**没有遍历全部信源名录**。
  所以它**不能**当作「2026 年 8 月全球恐怖袭击完整统计」。遍历性报告里已如实声明。
- 三条事件都核过正文原句，链接 100% 可打开；但只做了「链接可用 + 原句可引 + 日期可验」，
  不是「交叉验证到无可置疑」。
- 证据原句保留在 `台账` 的备注列（前缀 `[证据]`）与 `rows_2026_08.json` 里，可逐条复核。
