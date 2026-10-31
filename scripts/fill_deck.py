#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把台账 / 汇总数据填进单文件网页 PPT：只改数据，不改结构。

设计意图：agent 最容易犯的错是「为了填几个数字去重写整个 HTML」，一改就破版。
这个脚本只做一件事：把 values 注入到 deck 的 `window.DECK_VALUES = ...`，
页面侧用 data-v / data-bars / data-table 占位符自动渲染。

退出码：0 = 注入成功且自校验通过 | 2 = 校验失败 | 1 = 无法判定

用法：
  # 1) 直接从台账 xlsx 算出来并注入（最省事：数字不可能抄错）
  python scripts/fill_deck.py --deck deck/index.html --from-ledger 台账.xlsx --out deck/index.html

  # 2) 用现成的 values.json（适合人工微调过的口径）
  python scripts/fill_deck.py --deck deck/index.html --values report_values.json --out deck/index.html

  # 3) 只看会注入什么，不写盘
  python scripts/fill_deck.py --deck deck/index.html --from-ledger 台账.xlsx --dry-run

values.json 支持的键（缺的会跳过，不会报错）：
  events / deaths / injuries / countries / uncovered / inferred   —— 标量
  countries_top  —— [{label, value}]   条形
  key_events     —— [{date, country, title, deaths, injuries}]   表格
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

DEFAULT_KEYS = ["events", "deaths", "injuries", "countries"]
MARK_BEGIN = "<!-- DECK_VALUES:BEGIN -->"
MARK_END = "<!-- DECK_VALUES:END -->"


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def _fmt_date(ws, r, c_year, c_month, c_date):
    """年份/月份/日期 三列拼成 YYYY-MM-DD。

    官方 19 列模板把年月日拆成三列（日期列只存「日」），
    直接取「日期」会得到 "21" 这种值，展示上毫无意义。
    缺失部分退化为能凑出的最短形式。
    """
    def gv(c):
        if not c:
            return ""
        v = ws.cell(r, c).value
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v).strip()

    y, m, d = gv(c_year), gv(c_month), gv(c_date)
    # 日期列可能是 "7/2"、"7-2"、"2026-07-02" 等，先剥出纯数字
    dn = ""
    if d:
        nums = re.findall(r"\d+", d)
        if len(nums) >= 3:            # 已经是完整日期
            return "%04d-%02d-%02d" % (int(nums[0]), int(nums[1]), int(nums[2]))
        if nums:
            dn = nums[0]
    if y and m and dn:
        try:
            return "%04d-%02d-%02d" % (int(y), int(m), int(dn))
        except Exception:  # noqa: BLE001
            pass
    if y and m:
        return "%s-%s" % (y, m)
    return d or (y + "-" + m if (y or m) else "")


def read_ledger(path):
    """从 19 列台账算出展示用数值。列名按官方模板；找不到就按位置兜底。"""
    try:
        import openpyxl
    except Exception as e:  # noqa: BLE001
        die("需要 openpyxl：python -m pip install openpyxl\n%s" % e)

    if not os.path.isfile(path):
        die("找不到台账：%s" % path)
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    header = [("" if c.value is None else str(c.value).strip()) for c in ws[1]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n) + 1
        return None

    c_sum = col("事件摘要", "摘要", "事件")
    c_cty = col("国家", "国家/地区")
    c_d = col("死亡人数", "死亡")
    c_i = col("受伤人数", "受伤")
    c_date = col("日期", "日期(日)")
    c_year = col("年份", "年")
    c_month = col("月份", "月")
    c_place = col("地点")
    c_org = col("负责人/组织", "组织")

    if not c_cty or not c_d:
        die("台账缺关键列（国家 / 死亡人数）。表头是：%s" % " | ".join(header))

    def num(v):
        if v is None or str(v).strip() == "":
            return 0
        try:
            return int(float(str(v).replace(",", "").strip()))
        except Exception:  # noqa: BLE001
            m = re.search(r"-?\d+", str(v))
            return int(m.group()) if m else 0

    rows = []
    for r in range(2, (ws.max_row or 1) + 1):
        if all(ws.cell(r, c).value is None for c in range(1, (ws.max_column or 1) + 1)):
            continue
        first = str(ws.cell(r, 1).value or "")
        if re.match(r"^(合计|总计|小计|total)", first, re.I):
            continue
        cty = str(ws.cell(r, c_cty).value or "").strip()
        if not cty:
            continue
        rows.append({
            "country": cty,
            "deaths": num(ws.cell(r, c_d).value),
            "injuries": num(ws.cell(r, c_i).value) if c_i else 0,
            "title": str(ws.cell(r, c_sum).value or "").strip() if c_sum else "",
            "date": _fmt_date(ws, r, c_year, c_month, c_date),
            "place": str(ws.cell(r, c_place).value or "").strip() if c_place else "",
        })

    cnt = Counter(x["country"] for x in rows)
    by_d = Counter()
    for x in rows:
        by_d[x["country"]] += x["deaths"]

    top = sorted(cnt.items(), key=lambda kv: (-kv[1], -by_d[kv[0]]))[:12]
    top_dead = sorted(by_d.items(), key=lambda kv: -kv[1])[:12]

    key = sorted(rows, key=lambda x: (-x["deaths"], -x["injuries"]))[:8]

    return {
        "events": len(rows),
        "deaths": sum(x["deaths"] for x in rows),
        "injuries": sum(x["injuries"] for x in rows),
        "countries": len(cnt),
        "countries_top": [{"label": k, "value": v} for k, v in top],
        "deaths_top": [{"label": k, "value": v} for k, v in top_dead],
        "key_events": [{
            "date": x["date"], "country": x["country"],
            "title": x["title"][:60], "deaths": x["deaths"], "injuries": x["injuries"],
        } for x in key],
    }


def load_values(path):
    if not os.path.isfile(path):
        die("找不到 values：%s" % path)
    d = json.load(open(path, encoding="utf-8"))
    if not isinstance(d, dict):
        die("values 必须是 JSON 对象")
    # 兼容 report_values.json 那种带 published_totals 的结构
    if "published_totals" in d and isinstance(d["published_totals"], dict):
        pt = d["published_totals"]
        flat = dict(d)
        flat.setdefault("events", pt.get("included_events"))
        flat.setdefault("deaths", pt.get("deaths"))
        flat.setdefault("injuries", pt.get("injuries_min") or pt.get("injuries"))
        return flat
    return d


def _sanitize(obj):
    """递归清理字符串里的控制字符（换行/制表/不可见字符）。

    真实事故：事件摘要里带换行，json.dumps 会保留成裸控制字符，
    注入后 JSON.parse 报 "Invalid control character"，页面直接空白。
    """
    CTRL = "".join(chr(c) for c in list(range(0, 32)) + [127])
    if isinstance(obj, str):
        return obj.translate({ord(c): " " for c in CTRL}).strip()
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    return obj


def inject(html, values):
    """把 values 注入为 window.DECK_VALUES。已有则替换，没有则插在 </body> 前。

    必须用唯一标记包裹：模板内部本来就有 `window.DECK_VALUES = window.DECK_VALUES || {}`
    这类普通赋值，裸正则会把它一起吞掉，页面直接崩。
    """
    payload = json.dumps(_sanitize(values), ensure_ascii=False, indent=2)
    block = (MARK_BEGIN + chr(10)
             + "<script>" + chr(10) + "window.DECK_VALUES = " + payload + ";" + chr(10)
             + "</scr" + "ipt>" + chr(10)
             + MARK_END + chr(10))

    pat = re.compile(re.escape(MARK_BEGIN) + r"[\s\S]*?" + re.escape(MARK_END) + r"\s*", re.I)
    if pat.search(html):
        return pat.sub(block, html, count=1), "replaced"

    if "</body>" in html:
        return html.replace("</body>", block + "</body>", 1), "inserted"
    return html + chr(10) + block, "appended"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True)
    ap.add_argument("--from-ledger", default=None)
    ap.add_argument("--values", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.isfile(a.deck):
        die("找不到 deck：%s" % a.deck)
    html = open(a.deck, encoding="utf-8", errors="replace").read()

    if not a.from_ledger and not a.values:
        die("需要 --from-ledger 或 --values 之一")

    values = {}
    src = None
    if a.from_ledger:
        values.update(read_ledger(a.from_ledger))
        src = "ledger:%s" % a.from_ledger
    if a.values:
        ext = load_values(a.values)
        # values 文件优先级更高（人工微调覆盖自动算的）
        values.update({k: v for k, v in ext.items() if not k.startswith("_")})
        src = (src + " + " if src else "") + "values:%s" % a.values

    new_html, how = inject(html, values)

    # 自校验：注入块必须能解析，且关键键在位
    # 只在 marker 块内找，并用「括号配平」提取完整 JSON 对象。
    # 不能用非贪婪正则：JSON 里有嵌套数组/对象，正则会提前截断，导致误报「缺键」。
    m = re.search(re.escape(MARK_BEGIN) + r"([\s\S]*?)" + re.escape(MARK_END), new_html)
    inner = m.group(1) if m else ""
    back, problems = {}, []
    ok = True

    def extract_json_obj(text):
        k = text.find("window.DECK_VALUES")
        if k < 0:
            return None
        k = text.find("{", k)
        if k < 0:
            return None
        depth, instr, esc = 0, False, False
        for idx in range(k, len(text)):
            ch = text[idx]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[k:idx + 1]
        return None

    raw = extract_json_obj(inner)
    if not raw:
        ok = False
        problems.append("注入后找不到完整的 DECK_VALUES JSON 对象")
    else:
        try:
            back = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            ok = False
            problems.append("注入块不是合法 JSON：%s" % e)
            back = {}

    for key in DEFAULT_KEYS:
        if back.get(key) in (None, ""):
            problems.append("缺关键键：%s（页面上会显示 —）" % key)
    if not back.get("countries_top") and not back.get("deaths_top"):
        problems.append("缺 countries_top / deaths_top（条形图会是空的）")
    if not back.get("key_events"):
        problems.append("缺 key_events（明细表会是空的）")

    # 顺带检查外链（避免填完就出离线问题）
    remote = re.findall(r'(?i)(?:src|href)\s*=\s*["\'](?:https?:)?//', new_html)

    # ---- 写盘 ----
    if not a.dry_run:
        out = a.out or a.deck
        try:
            open(out, "w", encoding="utf-8", newline="\n").write(new_html)
        except PermissionError:
            die("无法写入 %s（被占用？）" % out)
    else:
        out = "(dry-run，未写盘)"

    exit_code = 0 if ok and not remote else 2

    if a.json:
        print(json.dumps({"deck": a.deck, "out": out, "source": src,
                          "mode": how, "keys": sorted(values.keys()),
                          "problems": problems, "remote_refs": len(remote),
                          "exit": exit_code}, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 66)
    print(" 填数进单文件 PPT")
    print("=" * 66)
    print("deck  : %s" % a.deck)
    print("数据源: %s" % src)
    print("写出  : %s（%s）" % (out, how))
    print()
    print("注入的键：%s" % ", ".join(sorted(values.keys())))
    for k in ("events", "deaths", "injuries", "countries"):
        if k in values:
            print("  %-10s = %s" % (k, values[k]))
    if values.get("countries_top"):
        print("  countries_top = %d 行，首位 %s(%s)"
              % (len(values["countries_top"]),
                 values["countries_top"][0].get("label"),
                 values["countries_top"][0].get("value")))
    if values.get("key_events"):
        print("  key_events    = %d 行" % len(values["key_events"]))
    print()

    if problems:
        print("-- 提醒 ---------------------------------------------------------")
        for p in problems:
            print("  ! %s" % p)
        print()
    if remote:
        print("  ✗ 检测到 %d 处外链，断网会坏" % len(remote))
        print()

    if exit_code == 0:
        print("结论：注入成功，数据可解析、关键键齐全、无外链。")
        print("下一步：python scripts/gate_single_file.py %s --pages N" % out)
    else:
        print("结论：注入完成但有上面这些问题，修完再出门禁。")
    print("=" * 66)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
