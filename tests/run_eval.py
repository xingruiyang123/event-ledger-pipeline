#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在人工标注的真值集上跑判定链，算误报率与漏报率。

为什么必须有这个
----------------
判定链改了十几版，每版都有人问「这版比上版好吗」。没有测试集，答案只能是
「我觉得好点了」——那不是工程，是玄学。有了真值集，每次改动都能回答三个数：
误报率降了多少、漏报率涨了多少、哪些类型还在错。

指标定义
--------
按「是否计入伤亡统计」这一个维度评（这是委托方唯一关心的口径）：

    真值 include  ←→  判定「计入」   = TP
    真值 include  ←→  判定「不计入」 = FN（漏报）
    真值 exclude  ←→  判定「计入」   = FP（误报）
    真值 exclude  ←→  判定「不计入」 = TN

    误报率 = FP / (FP + TN)     判进来的里有多少是冤的
    漏报率 = FN / (TP + FN)     该进来的里有多少被挡了
    准确率 = (TP + TN) / 全部

「保留作对照」在「是否进统计」上等价于「不计入」——所以对真值 include 算漏报，
对真值 exclude 算正确。这个映射是口径决定的，不是随便定的。

「需人工判」也按「不计入」算进混淆矩阵，同时单独统计它的数量：
一个把 40% 的条目推给人工的判定链，准确率再高也不实用。

用法
----
  python tests/run_eval.py                       # 用默认 fixture
  python tests/run_eval.py --golden my.jsonl
  python tests/run_eval.py --show-errors 12      # 看前 12 条错判明细
  python tests/run_eval.py --baseline 0.82       # 准确率低于此则 exit 2
"""
import argparse
import io
import json
import os
import sys
from collections import Counter, defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

try:
    import judge_events as J
except ImportError as e:
    print("无法 import judge_events：%s" % e, file=sys.stderr)
    print("应在 skill 根目录下运行：python tests/run_eval.py", file=sys.stderr)
    sys.exit(1)


def load(path):
    rows = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_one(rec, min_deaths=1):
    """跑判定链，返回 (verdict, chain, note)。"""
    try:
        return J.judge(rec, min_deaths)
    except Exception as e:  # noqa: BLE001
        return "需人工判", "-", "判定链异常：%s" % str(e)[:60]


def as_binary(verdict):
    """按「是否计入统计」二值化。保留作对照 = 不计入。"""
    return 1 if verdict == "计入" else 0


def main():
    ap = argparse.ArgumentParser(description="判定链真值评测")
    ap.add_argument("--golden", default=os.path.join(HERE, "fixtures", "golden_2026-07.jsonl"))
    ap.add_argument("--min-deaths", type=int, default=1)
    ap.add_argument("--show-errors", type=int, default=0, help="打印前 N 条错判")
    ap.add_argument("--baseline", type=float, default=0.0,
                    help="准确率低于此值则 exit 2（用于回归门禁）")
    ap.add_argument("--json-out", default=None, help="把指标写成 json，便于版本间对比")
    a = ap.parse_args()

    if not os.path.isfile(a.golden):
        print("真值集不存在：%s" % a.golden, file=sys.stderr)
        return 1

    gold = load(a.golden)
    print("真值集：%s" % os.path.basename(a.golden))
    print("样本数：%d" % len(gold))
    print("标签分布：%s" % dict(Counter(g["label"] for g in gold)))
    print()

    # ---------- 逐条跑 ----------
    tp = fp = fn = tn = 0
    manual_n = 0
    keep_n = 0
    errs = []
    by_type = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
    chain_hit = Counter()

    for g in gold:
        if g["label"] == "manual":
            # 真值为「存疑」的样本不进混淆矩阵 —— 它本身就没有定论
            verdict, chain, note = run_one(g, a.min_deaths)
            manual_n += 1
            continue

        verdict, chain, note = run_one(g, a.min_deaths)
        chain_hit[chain] += 1
        if verdict == "保留":
            keep_n += 1
        pred = as_binary(verdict)
        truth = 1 if g["label"] == "include" else 0

        # 错误归类：用真值 + 判定链落点做交叉，便于定位改哪里
        if truth == 1 and pred == 0:
            k = "漏报·%s" % chain
        elif truth == 0 and pred == 1:
            k = "误报·%s" % chain
        else:
            k = "正确"

        if truth == 1 and pred == 1:
            tp += 1
            by_type[k]["tp"] += 1 if False else 0
        elif truth == 0 and pred == 1:
            fp += 1
        elif truth == 1 and pred == 0:
            fn += 1
        else:
            tn += 1

        if truth != pred:
            errs.append({
                "id": g["id"], "kind": k, "truth": g["label"],
                "pred": verdict, "chain": chain, "note": note,
                "country": g.get("country", ""), "date": g.get("date", ""),
                "summary": (g.get("summary") or "")[:90],
            })

    total = tp + fp + fn + tn
    print("=" * 62)
    print(" 混淆矩阵（n=%d，已排除 %d 条存疑样本）" % (total, manual_n))
    print("=" * 62)
    print("                 判定计入    判定不计入")
    print("  真值 计入  %10d %12d      ← 右下角是漏报" % (tp, fn))
    print("  真值 不计入 %10d %12d      ← 左下角是误报" % (fp, tn))
    print()
    acc = (tp + tn) / total if total else 0
    fp_rate = fp / (fp + tn) if (fp + tn) else 0
    fn_rate = fn / (tp + fn) if (tp + fn) else 0
    prec = tp / (tp + fp) if (tp + fp) else 0
    rec = tp / (tp + fn) if (tp + fn) else 0
    print("  准确率  %.1f%%   (%d/%d)" % (100 * acc, tp + tn, total))
    print("  误报率  %.1f%%   (%d/%d)  判进来的里有多少是冤的" % (100 * fp_rate, fp, fp + tn))
    print("  漏报率  %.1f%%   (%d/%d)  该进来的里有多少被挡了" % (100 * fn_rate, fn, tp + fn))
    print("  精确率  %.1f%%   召回率 %.1f%%" % (100 * prec, 100 * rec))
    print()
    print("  其中「保留作对照」%d 条（按口径不计入统计，对真值 exclude 算正确）" % keep_n)
    print("  其中「需人工判」被计入混淆矩阵的：见下方落点分布")
    print()

    print("-- 判定链落点分布 --")
    for k, v in chain_hit.most_common():
        print("  %-12s %4d" % (k or "(空)", v))
    print()

    if errs:
        print("-- 错判分类（按落点归因，指哪打哪）--")
        cnt = Counter(e["kind"] for e in errs)
        for k, v in cnt.most_common():
            print("  %-18s %3d 条" % (k, v))
        print()

        if a.show_errors:
            print("-- 错判明细（前 %d 条）--" % a.show_errors)
            for e in errs[:a.show_errors]:
                print("  [%s] %s %s %s" % (e["kind"], e["id"], e["country"], e["date"]))
                print("        真值=%s 判定=%s(%s)" % (e["truth"], e["pred"], e["chain"]))
                print("        %s" % e["summary"])
                print("        理由：%s" % (e["note"] or "")[:80])
            print()
    else:
        print("-- 无错判 --")
        print()

    metrics = {
        "n": total, "manual_skipped": manual_n,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "accuracy": round(acc, 4), "false_positive_rate": round(fp_rate, 4),
        "false_negative_rate": round(fn_rate, 4),
        "precision": round(prec, 4), "recall": round(rec, 4),
        "kept_as_control": keep_n,
        "min_deaths": a.min_deaths,
    }
    if a.json_out:
        d = os.path.dirname(os.path.abspath(a.json_out))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        print("指标已写出 → %s" % a.json_out)

    if a.baseline and acc < a.baseline:
        print()
        print("✗ 准确率 %.1f%% 低于基线 %.1f%%" % (100 * acc, 100 * a.baseline), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
