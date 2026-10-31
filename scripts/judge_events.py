#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通用判定器：把候选事件按判定链判成「计入 / 不计入」，输出 19 列台账数据。

为什么必须有这一步（开箱即用的最大断点）
------------------------------------
实测：陌生人拿到候选池后，最卡的地方不是采集、不是出表，而是**判定**：
  · 判定链写在文档里，但文档不会自动执行
  · 让模型逐条判 2000 条，token 烧光且不稳定
  · 每条都要写「命中哪条判据、排除了哪几条反证」，手写不现实

本脚本把**可规则化的部分**做成代码，剩下的明确标「需人工判」，不假装全能。

判定链（与 reference/00-口径先立.md 一致）
----------------------------------------
  步0 M0 硬门槛：实施主体是国家/政府力量 → 不计入
  步1 反证：N1私人 / N2索财 / N3事故 / N4已交战 / N5反恐执法 / N6汇总 → 不计入
  步2 正面判据：P1 / P1b / P3 / P4 / P7 命中任一 → 计入
  步3 兜底 → 不计入

输出三种结论（不是两种！）
------------------------
  计入 / 不计入 / **需人工判**（要素不足，规则判不了）
  —— 「需人工判」不是「待定」，它是「交给人」的明确动作，不是第三种判定结果。

用法
----
  python scripts/judge_events.py --in 候选.jsonl --out 台账数据.json
  python scripts/judge_events.py --in 候选.jsonl --out 台账数据.json --min-deaths 1
  python scripts/judge_events.py --in 候选.jsonl --json      # 只看统计

退出码：0 = 判定完成 | 2 = 判定后计入条数为 0（数据或口径有问题）| 1 = 无法判定
"""
import argparse
import json
import re
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------- 行为体性质
STATE = re.compile(
    r"(?i)\b(?:government of|armed forces|military forces?|national army|"
    r"\barmy\b|\bair ?force\b|\bnavy\b|police forces?|\bpolice\b|"
    r"security forces?|national guard|border guard|gendarmerie|"
    r"ministry of (?:defence|defense|interior)|coalition|nato|"
    r"united nations|peacekeepers?|unmiss|unifil|monusco|paramilitary)\b"
)
CRIMINAL = re.compile(
    r"(?i)\b(?:cartel|carteles|narcos?|mafia|gang|gangs|maras?|"
    r"drug|trafficking|smuggling|los mayos|sinaloa|jalisco|zetas|"
    r"bandits?|banditry|cattle rustlers?|armed robbers?)\b"
)
TERROR = re.compile(
    r"(?i)\b(?:al-?shabaab|al-?shabab|boko haram|iswap|isis|isil|daesh|"
    r"islamic state|jihadist|jnim|ansar(?:u|ou)?l|aqap|aqim|nusrat|"
    r"taliban|ttp|tehrik-i-taliban|bla\b|baloch liberation|"
    r"jamaat-ul-ahrar|lashkar|hizbul|adf\b|allied democratic forces|"
    r"farc|eln\b|national liberation army|dissidents?|"
    r"rsf|rapid support|janjaweed|"
    r"militants?|insurgents?|terrorists?|extremists?|"
    r"separatists?|rebels?|self-defen[cs]e|vigilantes?)\b"
)
INTERSTATE = re.compile(
    r"(?i)\b(?:ukraine|russia|israel|palestine|gaza|iran|lebanon|hezbollah|"
    r"houthi|saudi|yemen|coalition forces|nato|"
    r"ukrainian armed forces|russian armed forces|israel defense forces|idf)\b"
)

# 政府军行动的迹象词：出现这些，说明大概率是国家力量在打，不是非国家方袭击
GOV_ACTION = re.compile(
    r"(?i)(junta|regime forces|army (?:strike|airstrike|shelling|operation)|"
    r"airstrike|air strike|drone strike|drone attack|shelling|artillery|"
    r"military (?:operation|offensive|campaign)|troops (?:kill|raid|shell)|"
    r"security forces (?:kill|raid)|state forces|government forces)")
# 社区/民族/家族冲突（N1：私人或群体纠纷，不是恐怖主义）
# 实测踩到：UCDP 把「farmers-herders clash」归入 type=2 单方面暴力，
# 被当成恐袭计入（尼日利亚 7-01 死 48 那条）。农牧民冲突是社区暴力，不是恐袭。
COMMUNAL = re.compile(
    r"(?i)\b(?:farmers?[- ]herders?|herders?|herdsmen|communal (?:clash|violence|conflict)|"
    r"inter-?communal|ethnic (?:clash|violence|conflict)|sectarian (?:clash|violence)|"
    r"cattle (?:raid|rustl)|land dispute|family (?:feud|dispute|infighting)|"
    r"clan (?:feud|clash|conflict)|tribal (?:clash|conflict|fighting|violence)|Fulani (?:settlement|village|herders?)|reprisal attack|vigilante (?:clash|group)|border clash(?:es)?|Oromia|Sidama|Amhara|Benishangul|Afar)\b")

# 拉美：UCDP 的 type=2「单方面暴力」在该区域大量用于记录帮派/卡特尔杀人，
# 不是恐怖主义。实测 2026-07：巴西 Comando Vermelho 9 条、墨西哥 Jalisco Cartel 106 条、
# 厄瓜多尔 Los Choneros、哥伦比亚 La Nueva Generación —— 全是街头火并和凶杀，
# 摘要形如「Man shot dead in front of his wife」「拒绝交手机被枪杀」。
# 只有明确的政治性武装组织（ELN/FARC 等）才算。
LATAM = {"巴西", "哥伦比亚", "墨西哥", "厄瓜多尔", "危地马拉", "洪都拉斯",
         "萨尔瓦多", "委内瑞拉", "秘鲁", "玻利维亚", "海地", "多米尼加",
         "巴拿马", "哥斯达黎加", "尼加拉瓜", "阿根廷", "巴拉圭", "智利"}
POLITICAL_ARMED = re.compile(
    r"(?i)(\bELN\b|\bFARC\b|\bEPL\b|dissiden|guerrilla|民族解放军|"
    r"Autodefensas Unidas|\bAUC\b|Sendero Luminoso|光辉道路)")

# ---- 摘要兜底信号 ----
# 行为体被脱敏（XXX100）或分方死亡缺失时，用摘要关键词做二次判定。
# 实测：2026-07 有 140 条非洲条目摘要里有完整事件描述（如
# 「IS claims killing 17 soldiers」「Nine civilians killed in an attack」），
# 但因为没有 actor 名称全被扔进「需人工判」。这些其实能判。
SRC_ATTACK = re.compile(
    r"(?i)("
    r"(?:militants?|insurgents?|terrorists?|jihadists?|gunmen|assailants|"
    r"extremists?|bandits?|IS|ISWAP|ISIS|Boko Haram|JNIM|al-?Shabaab|"
    r"ADF|M23|AQAP|ISKP|TTP|BLA)\s+"
    r"(?:claims?|claimed|kills?|killed|attacks?|attacked|opened fire|raided|stormed)"
    r"|(?:killed|kills|killing|murdered|abducted|massacred)\s+(?:by|in an? attack)"
    r"|(?:suicide|car bomb|roadside bomb|IED|grenade|rocket)\s+(?:attack|blast|explosion)"
    r"|(?:attack|raid|ambush|massacre)\s+(?:on|kills?|leaves?)"
    r"|killing of .{0,40}? by (?:gunmen|militants?|attackers?|assailants?|terrorists?|insurgents?|unknown)"
    r")")
SRC_GOV_OP = re.compile(
    r"(?i)(neutraliz|(?:forces?|troops?|army|police|security|military)\s+(?:kill|killed|kills|raid|raided|foil|foiled|arrest|arrested|capture|captured|repel|repelled|rescue|rescued|launch|launches|manhunt)|rescue|repel|manhunt|foil|arrest|operation\s+(?:against|targeting)|airstrike|air strike|drone strike|offensive|combat)")

# 社区/农牧冲突信号（尼日利亚 Fulani、Plateau 等地高发）
SRC_COMMUNAL = re.compile(
    r"(?i)(Fulani|herders?|herdsmen|farmers?[- ]herders?|Plateau|community (?:attack|clash)|communal|cattle|livestock|land (?:conflict|dispute))")

# 汇总条目（不是单起事件）
SRC_AGGREGATE = re.compile(r"(?i)(Timeline\s*.{0,8}Terrorist|monthly (?:report|newsletter|roundup|summary))")

# 其他排除信号（土地冲突/普通刑案/交通事故）
SRC_EXCLUDE = re.compile(
    r"(?i)(land conflict|land dispute|clan conflict|tribal|drug|narcotics|smuggled|domestic (?:dispute|violence)|road accident|traffic (?:accident|crash)|robbery)")

# 事故/灾害（N3）
ACCIDENT = re.compile(
    r"(?i)\b(?:accident|collision|crash|explosion at a (?:factory|mine)|"
    r"gas leak|electrical|structural failure|stampede|fire at a (?:factory|market))\b"
)


ANON = re.compile(r"^XXX\d*$", re.I)


def is_anon(s):
    """UCDP 候选集会把未确认的行为体脱敏成 XXX100 这类占位符。

    实测：1984 条候选里 552 条是脱敏的。这些条目**无法用行为体判定**，
    必须降级为「需人工判」，不能靠国家/摘要硬猜 —— 否则哥伦比亚 190 条
    贩毒集团火并会被当成恐袭计入。
    """
    return bool(ANON.match((s or "").strip()))


def side_kind(s):
    if not s:
        return "unknown"
    if TERROR.search(s):
        return "terror"
    if STATE.search(s):
        return "state"
    if CRIMINAL.search(s):
        return "criminal"
    return "unknown"


def _num_from_text(txt):
    """从摘要里提取「N killed/killed N/N dead」的数字。"""
    m = re.search(r"(?i)(\d{1,4})\s*(?:people\s*)?(?:were\s*)?(?:killed|dead|died|die)", txt)
    if m:
        return int(m.group(1))
    m = re.search(r"(?i)(?:killed|kills|leaves?|left)\s+(?:at least\s+)?(\d{1,4})", txt)
    if m:
        return int(m.group(1))
    return None


def _from_text(txt, civ, deaths, cty, t):
    """行为体判不了时，用摘要做兜底判定。返回 (verdict, chain, note) 或 None。

    顺序很重要：排除信号 > 政府行动 > 袭击信号。
    因为「军队击毙武装分子」同时含 attack 和 gov_op 词，必须判成反恐行动。
    """
    if SRC_AGGREGATE.search(txt):
        return ("不计入", "兜底", "汇总结算条目，不是单起事件")

    if SRC_EXCLUDE.search(txt) or SRC_COMMUNAL.search(txt):
        return ("不计入", "N1",
                "摘要显示为社区/农牧/土地冲突或普通刑案，非恐怖主义暴力")

    has_atk = bool(SRC_ATTACK.search(txt))
    has_gov = bool(SRC_GOV_OP.search(txt))

    # 两者都有 -> 大概率是政府行动（袭击者被击毙之类）
    if has_gov and not (has_atk and re.search(r"(?i)(claims?|claimed|responsibility)", txt)):
        return ("保留", "X1",
                "摘要显示为政府/军警主导的打击行动（反恐行动），作对照、不计入统计")

    if has_atk:
        n = _num_from_text(txt)
        dd = max(civ or 0, deaths or 0, n or 0)
        if civ > 0:
            return ("计入", "P3+P4", "摘要显示武装袭击致平民死亡 %d" % civ)
        if dd > 0:
            return ("计入", "P5", "摘要显示武装袭击，死亡 %d" % dd)
        return ("计入", "P5", "摘要显示武装袭击（未给死亡数）")

    if has_gov:
        return ("保留", "X1", "摘要显示为政府/军警行动，作对照、不计入统计")

    return None


def judge(rec, min_deaths=1):
    """返回 (verdict, chain, note)。verdict ∈ {计入, 不计入, 需人工判}。"""
    a = rec.get("actor_a", "") or ""
    b = rec.get("actor_b", "") or ""
    txt = " ".join([rec.get("summary", "") or "", rec.get("place", "") or "",
                    a, b, str(rec.get("extra", {}).get("conflict", "") or "")])
    deaths = rec.get("deaths")
    try:
        deaths = int(deaths)
    except Exception:  # noqa: BLE001
        deaths = 0
    civ = rec.get("extra", {}).get("deaths_civilians")
    try:
        civ = int(civ)
    except Exception:  # noqa: BLE001
        civ = 0
    t = str(rec.get("extra", {}).get("type_of_violence") or "")
    cty = rec.get("country", "") or ""

    # 脱敏行为体 / 分方死亡缺失：先用摘要兜底判定，判不了才交人
    if is_anon(a) or is_anon(b):
        v = _from_text(txt, civ, deaths, cty, t)
        if v:
            return v
        if deaths == 0 and civ == 0:
            return "不计入", "兜底", "行为体脱敏且无死亡记录"
        return "需人工判", "-", ("行为体被 UCDP 脱敏（%s/%s），摘要无足够信号；"
                                "需看原文确认" % (a or "?", b or "?"))

    ka, kb = side_kind(a), side_kind(b)
    kinds = {ka, kb}

    # ===== 步0 M0：双方都是国家力量 =====
    if ka == "state" and kb == "state":
        return "不计入", "M0", "双方均为国家/政府力量（%s vs %s）" % (a, b)

    # ===== 步1 反证 =====
    # N3 事故
    if ACCIDENT.search(txt):
        return "不计入", "N3", "疑似事故/非故意事件"
    # N1 社区/民族/家族冲突
    if COMMUNAL.search(txt):
        return "不计入", "N1", "社区/民族/家族冲突（非恐怖主义暴力）"

    # N2 拉美有组织犯罪：该区域 type=2 默认是帮派/卡特尔暴力，不是恐怖主义
    if cty in LATAM and t == "2" and not POLITICAL_ARMED.search(a + " " + b + " " + txt):
        return "不计入", "N2", (
            "拉美区域的有组织犯罪暴力（%s）：UCDP 在该区域把帮派/卡特尔杀人记为"
            "单方面暴力，不属恐怖主义" % (a or "?"))

    # N2 犯罪组织
    if "criminal" in kinds:
        return "不计入", "N2", "涉犯罪/贩毒集团（%s vs %s），属以经济目的的帮派暴力" % (a, b)
    # N4 双方都是武装/恐怖组织互相打（如 JAS vs ISWAP）
    if ka == "terror" and kb == "terror":
        return "不计入", "N4", "两个武装/恐怖组织之间交战（%s vs %s）" % (a, b)

    # N4 组织间交战
    if t == "3":
        return "不计入", "N4", "非国家武装团体之间交战（%s vs %s）" % (a, b)
    # N4 国家间战争
    if INTERSTATE.search(txt):
        return "不计入", "N4", "国家间战争语境（%s）" % cty
    # type=1 = 至少一方是国家/政府的冲突。按「委托方口径」分类，不再一律排除：
    #
    #   委托方台账的实际做法（从 2026-07 已交付台账提炼）：
    #     · 尼日尔「ISSP 伏击军事巡逻，16 士兵死」        -> 性质:恐怖袭击（计入）
    #     · 巴基斯坦「海岸警卫队哨所遭 VBIED+突击」        -> 性质:恐怖袭击（计入）
    #     · 马里「军方车队遭 JNIM-FLA 伏击」               -> 性质:恐怖袭击（计入）
    #     · 叙当局「逮捕 IS 关联小组」                     -> 性质:反恐行动（对照，不计入伤亡统计）
    #     · 摩洛哥「挫败七城市连环图谋」                   -> 性质:反恐行动（对照，不计入）
    #
    # 也就是：**非国家方打政府方 = 恐怖袭击；政府方打非国家方 = 反恐行动**。
    # 这个区分用 UCDP 的 deaths_a / deaths_b 可以自动判，不需要人工。
    if t == "1":
        d_a = rec.get("extra", {}).get("deaths_a")
        d_b = rec.get("extra", {}).get("deaths_b")
        try:
            d_a = int(d_a) if d_a is not None else None
            d_b = int(d_b) if d_b is not None else None
        except Exception:  # noqa: BLE001
            d_a = d_b = None

        # side_a 通常记为政府方，据此换算「政府方伤亡 / 非国家方伤亡」
        if ka == "state":
            gov_loss, ins_loss = d_a, d_b
        elif kb == "state":
            gov_loss, ins_loss = d_b, d_a
        else:
            gov_loss = ins_loss = None

        # A. 政府方 0 死、非国家方有死 -> 政府主导打击 = 反恐行动，保留但单独标性质
        if gov_loss == 0 and (ins_loss or 0) > 0:
            return "保留", "X1", (
                "政府主导打击（政府方0死/对方死%s），性质=反恐行动，保留作对照、不计入恐袭统计"
                % ins_loss)

        # B. 非国家方单方面袭击政府方（政府方有死、对方无死）= 恐怖袭击
        if (gov_loss or 0) > 0 and (ins_loss or 0) == 0:
            if civ > 0:
                return "计入", "P5+P4", (
                    "非国家方袭击政府/军警目标致政府方死%s，并伤及平民%d（%s vs %s）"
                    % (gov_loss, civ, a, b))
            return "计入", "P5", (
                "非国家方袭击政府/军警目标（检查站/哨所/巡逻队/基地），政府方死%s（%s vs %s）"
                % (gov_loss, a, b))

        # C. 双方都有死 = 交火/交战，保留但标性质
        if (gov_loss or 0) > 0 and (ins_loss or 0) > 0:
            return "保留", "X2", (
                "双方交火（政府方%s/对方%s），性质=交战，保留作对照、不计入统计"
                % (gov_loss, ins_loss))

        # D. 分方死亡都是 0、只有平民死 -> 判不了是谁干的，不能算恐袭。
        #
        # 实测踩坑：缅甸「Junta Drone Attack Kills Two Civilians」、
        # 苏丹「Sudanese army drone strike kills civilians」这类条目，
        # deaths_a=0 且 deaths_b=0（双方战斗人员都没死、只有平民死），
        # side_a 却是「Government of Myanmar/Sudan」—— 这是政府军杀平民（国家暴力），
        # 早前版本落到下面「有平民死亡就计入 P4」，把政府军空袭算成了恐怖袭击。
        if civ > 0:
            if GOV_ACTION.search(txt):
                return "保留", "X1", (
                    "疑似国家力量行动致平民死亡 %d（%s）：性质=国家暴力，保留作对照、不计入"
                    % (civ, a))
            return "需人工判", "-", (
                "平民死亡 %d 但分方死亡均为 0、实施方不明（%s vs %s），需看原文"
                % (civ, a, b))
        return "需人工判", "-", "国家参与的冲突但分方死亡缺失（%s vs %s）" % (a, b)


    # ===== 步2 正面判据 =====
    # P1：主体属恐怖组织且有实施证据
    if "terror" in kinds and deaths >= min_deaths:
        if civ > 0:
            return "计入", "P1+P4", "恐怖组织袭击致平民死亡 %d" % civ
        return "计入", "P1", "恐怖组织袭击，死亡 %d" % deaths

    # P3/P4：单方面暴力针对平民
    if t == "2" and civ > 0:
        return "计入", "P3+P4", "单方面暴力针对平民，平民死亡 %d" % civ
    if t == "2" and deaths >= min_deaths:
        return "计入", "P3", "单方面暴力，死亡 %d（受害者多为军警/非战斗人员）" % deaths

    # P3：武装冲突中平民死亡
    if civ > 0:
        return "计入", "P4", "武装冲突中平民死亡 %d" % civ

    # ===== 步3 兜底 =====
    if deaths == 0:
        return "不计入", "兜底", "无死亡记录"
    # 有死亡但性质判不出来 → 交人
    return "需人工判", "-", "有死亡（%d）但行为体性质与动作类型均判不出来，需人工看原文" % deaths


def _src_hint(rec):
    """从 UCDP 记录里拼出「媒体名 + 标题」，给补链接脚本用。"""
    ex = rec.get("extra", {}) or {}
    office = (ex.get("source_office") or "").split(";")[0].strip()
    hl = (ex.get("source_headline") or "").strip()
    if ";" in hl:
        hl = hl.split(";")[0].strip()
    if "|" in hl:
        hl = re.split(r"[|｜]", hl)[-1].strip()
    if not office or not hl:
        art = (rec.get("source_citation") or ex.get("source_citation") or "").strip().strip('"')
        parts = [p.strip().strip('"') for p in art.split(",")]
        if len(parts) >= 3:
            office = office or parts[0]
            if not hl:
                hl = parts[-1]
                if ";" in hl:
                    hl = hl.split(";")[0].strip()
                if "|" in hl:
                    hl = re.split(r"[|｜]", hl)[-1].strip()
    hint = ("%s %s" % (office, hl)).strip()[:70]
    return hint or "无来源线索"


def to_row(rec, verdict, chain, note):
    d = rec.get("date") or ""
    try:
        y, mo, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
    except Exception:  # noqa: BLE001
        y = mo = dd = ""
    ns = rec.get("extra", {}).get("n_sources") or 0
    return {
        "事件摘要": (rec.get("summary") or "")[:200],
        "国家": rec.get("country") or "",
        "年份": y, "月份": mo, "日期": dd,
        "地点": rec.get("place") or "",
        "死亡人数": rec.get("deaths") if isinstance(rec.get("deaths"), int) else 0,
        "受伤人数": rec.get("injured") if isinstance(rec.get("injured"), int) else 0,
        "袭击方式": rec.get("act_type") or "",
        "袭击武器": "",
        "袭击者": rec.get("actor_a") or "",
        "负责人/组织": rec.get("actor_a") or "",
        "信息来源URL": rec.get("url") or "",
        "采集人/组别": "",
        "录入日期": "",
        "置信度": "A" if ns >= 2 else "B",
        "审核状态": "已确认" if verdict == "计入" else ("待审核" if verdict == "需人工判" else "已退回"),
        "关联知识点": chain,
        # 备注里带上「媒体名 + 标题」：UCDP 不给 URL，后续 enrich_urls 靠这个反查
        "备注": "[判定]%s ｜ [src]%s" % (note, _src_hint(rec)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", default=None, help="计入的台账数据（json 数组）")
    ap.add_argument("--out-excluded", default=None, help="不计入的（json 数组）")
    ap.add_argument("--out-manual", default=None, help="需人工判的（json 数组）")
    ap.add_argument("--out-keep", default=None, help="保留作对照的（反恐行动/交战，不计入统计）")
    ap.add_argument("--min-deaths", type=int, default=1,
                    help="计入的最低死亡数（默认 1；口径要求死亡≥1 或受伤≥3）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    rows = []
    with open(a.inp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    # 先去重：同国 + 同日 + 同死亡数 -> 同一事件被多次记录
    seen = set()
    deduped = []
    for r in rows:
        # 保守去重：国家+日期+死亡数+地点 都相同才算同一事件。
        # 只按 国+日+死 会把「同一天同国两起都死1人」误合并（实测去掉 872 条，过激）。
        key = (r.get("country"), r.get("date"), r.get("deaths"),
               (r.get("place") or "").strip().lower()[:40])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    if len(deduped) != len(rows):
        print("去重：%d -> %d 条（同国+同日+同死亡数视为同一事件）" % (len(rows), len(deduped)))
    rows = deduped

    led, exc, man, keep = [], [], [], []   # keep=保留作对照（反恐行动/交战），不计入统计
    reasons = Counter()
    for r in rows:
        v, chain, note = judge(r, a.min_deaths)
        row = to_row(r, v, chain, note)
        if v == "计入":
            led.append(row)
        elif v == "保留":
            row["备注"] = "[对照]%s ｜ %s" % (note, row.get("备注", ""))
            keep.append(row)
        elif v == "需人工判":
            man.append(row)
        else:
            exc.append(row)
        reasons["%s %s" % (v, chain)] += 1

    def dump(path, data):
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)

    dump(a.out, led)
    dump(a.out_excluded, exc)
    dump(a.out_manual, man)
    dump(a.out_keep, keep)

    exit_code = 0 if led else 2

    if a.json:
        print(json.dumps({
            "in": a.inp, "total": len(rows),
            "included": len(led), "excluded": len(exc),
            "manual": len(man), "keep_as_control": len(keep),
            "reasons": dict(reasons), "exit": exit_code,
        }, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 70)
    print(" 判定结果")
    print("=" * 70)
    print("输入      : %d 条" % len(rows))
    print("计入      : %d 条" % len(led))
    print("不计入    : %d 条" % len(exc))
    print("需人工判  : %d 条" % len(man))
    print("保留作对照: %d 条（反恐行动/交战，不计入恐袭统计）" % len(keep))
    print()
    print("-- 判定链落点分布 ------------------------------------------------")
    for k, v in reasons.most_common():
        print("  %-22s %5d" % (k, v))
    print()
    if led:
        ctys = Counter(r["国家"] for r in led)
        print("-- 计入的国家分布（前 15）-----------------------------------------")
        for k, v in ctys.most_common(15):
            print("  %-18s %4d" % (k, v))
        print()
        print("-- 死亡合计：%d（仅统计「计入」类，不含保留作对照的 %d 条）"
              % (sum(r["死亡人数"] for r in led), len(keep)))
        print()

    if not led:
        print("  ✗ 计入条数为 0。可能原因：")
        print("    · 候选池里没有非国家主体袭击（源选错了）")
        print("    · --min-deaths 设得太高")
        print("    · 行为体字段全为空（UCDP 的 side_a/side_b 没读进来）")

    if man:
        print("  ℹ️  %d 条需人工判 —— 这不是「待定」，是明确的动作：" % len(man))
        print("     逐条看原文，判完写回台账。可用 --out-manual 导出这份清单。")

    print()
    print("→", a.out or "(未指定 --out)")
    print("=" * 70)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
