"""验证：抓新闻正文，能否抽出 UCDP 缺失的字段。

背景
----
UCDP 有 49 个字段，但缺少最关键的几项：
  袭击方式（爆炸/枪击/绑架…）  → 台账「袭击方式」列现在是 state_based 这种代码
  袭击武器（IED/AK-47/车载炸弹）→ 台账这列 100% 空
  受伤人数                      → 台账这列 100% 空
  施害组织的宣称                → 只有 side_a/side_b 这种粗分类

这些都在新闻报道的正文里。本脚本验证能不能抽出来。

抽出来之后，同一事件的多条报道还能互相补：A 家写死亡数、B 家写组织名、
C 家写武器，拼起来字段就全了。
"""
import argparse
import io
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# ---- 正文抽取：去掉脚本/样式/导航，取最长的正文块 ----
TAG = re.compile(r"<[^>]+>")
SCRIPT = re.compile(r"<(script|style|nav|footer|header|aside)[^>]*>.*?</\1>",
                    re.S | re.I)


def html_to_text(html):
    """粗暴但够用的正文抽取。

    不用 readability 之类的库：这些站点结构各异，通用正文抽取器在
    小语种本地媒体上经常抽空。用「去标签 + 找最长段落块」更稳。
    """
    t = SCRIPT.sub(" ", html or "")
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = TAG.sub("\n", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&quot;", '"').replace("&#39;", "'")
         .replace("&lt;", "<").replace("&gt;", ">"))
    lines = [x.strip() for x in t.split("\n") if len(x.strip()) > 40]
    return "\n".join(lines)


# ---- 字段抽取 ----
# 死亡：多语种
DEATH_PATS = [
    r"(\d{1,5})\s*(?:people\s+)?(?:were\s+)?(?:killed|dead|died|slain)",
    r"killed\s+(?:at\s+least\s+)?(\d{1,5})",
    r"death\s+toll\s+(?:rose\s+to|of|to)\s+(\d{1,5})",
    r"(?:at\s+least\s+)?(\d{1,5})\s+(?:people\s+)?(?:killed|dead)",
    r"(\d{1,5})\s*(?:人|名)\s*(?:死亡|丧生|遇难|被杀)",
    r"(?:死亡|丧生|遇难)\s*(?:人数)?\s*(?:升至|达到)?\s*(\d{1,5})\s*人",
    r"(\d{1,5})\s*(?:muertos|muertas|fallecidos)",
    r"(\d{1,5})\s*(?:morts|tués)",
    r"(\d{1,5})\s*(?:mortos|pessoas)",
]
INJ_PATS = [
    r"(\d{1,5})\s*(?:people\s+)?(?:were\s+)?(?:injured|wounded|hurt)",
    r"injured\s+(?:at\s+least\s+)?(\d{1,5})",
    r"(\d{1,5})\s*(?:人|名)\s*(?:受伤|负伤)",
    r"(\d{1,5})\s*(?:heridos|feridos|blessés)",
]

# 袭击方式
ATTACK_TYPES = [
    ("自杀式袭击", r"suicide\s+(?:bomb|attack|blast)|自殺|自杀式|self-?immolation"),
    ("爆炸", r"bomb|explos|blast|IED|detonat|爆炸|炸弹|地雷|mine\b"),
    ("枪击", r"gun|shoot|fire[ds]?\b|small arms|枪击|开枪|射击|扫射"),
    ("武装袭击", r"armed\s+attack|assault|raid|stormed|ambush|袭击|伏击|突袭"),
    ("绑架", r"kidnap|abduct|hostage|绑架|劫持|绑走"),
    ("纵火", r"arson|set\s+fire|burned|纵火|焚烧"),
    ("车辆冲撞", r"ramming|vehicle-?borne|car\s+ram|冲撞|驾车"),
    ("斩首", r"behead|decapitat|斩首"),
]

# 武器
WEAPONS = [
    ("简易爆炸装置", r"\bIED\b|improvised explosive|简易爆炸装置|路边炸弹"),
    ("车载炸弹", r"car\s+bomb|vehicle-?borne|VBIED|汽车炸弹|车载炸弹"),
    ("自杀式背心", r"suicide\s+(?:vest|belt)|人肉炸弹|自杀背心"),
    ("步枪", r"AK-?47|assault\s+rifle|Kalashnikov|步枪|自动步枪"),
    ("火箭弹", r"\brocket\b|RPG|火箭弹|火箭筒"),
    ("迫击炮", r"mortar|迫击炮"),
    ("无人机", r"\bdrone\b|UAV|无人机"),
    ("爆炸物", r"explosive|TNT|grenade|手榴弹|炸药"),
    ("冷兵器", r"\bknife\b|machete|blade|刀|砍刀"),
]

# 恐怖组织（够覆盖主要活跃组织）
ORGS = [
    "al-Shabaab", "Al-Shabaab", "الشباب", "青年党", "索马里青年党",
    "Boko Haram", "ISWAP", "博科圣地", "伊斯兰国西非省",
    "Islamic State", "ISIS", "ISIL", "IS-K", "ISKP", "伊斯兰国", "达伊什",
    "JNIM", "Jama'at Nusrat", "支持伊斯兰与穆斯林",
    "AQIM", "Al-Qaeda", "基地组织",
    "Taliban", "塔利班", "TTP", "Tehreek-e-Taliban", "巴基斯坦塔利班",
    "BLA", "Baloch Liberation", "俾路支解放军", "BLF", "BLA",
    "Houthi", "胡塞", "Ansar Allah",
    "Hezbollah", "真主党", "Hamas", "哈马斯",
    "PKK", "库尔德工人党", "YPG",
    "FARC", "ELN", "Clan del Golfo", "海湾家族",
    "al-Mourabitoun", "Ansar Dine", "Ansarul Islam",
    "Maute", "Abu Sayyaf", "阿布沙耶夫",
    "RSF", "快速支援部队", "Janjaweed", "金戈威德",
    "NLA", "New People's Army", "新人民军",
    "ADF", "Allied Democratic Forces", "民主同盟军",
    "Dan Na Ambassagou", "Dozo", "多佐猎人",
]


def first_num(txt, pats, lo=1, hi=100000):
    for p in pats:
        for m in re.finditer(p, txt, re.I):
            try:
                v = int(m.group(1))
            except Exception:  # noqa: BLE001
                continue
            if lo <= v <= hi:
                return v
    return None


def extract_fields(text):
    """从正文抽结构化字段。"""
    t = text or ""
    out = {
        "deaths": first_num(t, DEATH_PATS, 1, 5000),
        "injured": first_num(t, INJ_PATS, 1, 20000),
        "attack_types": [],
        "weapons": [],
        "orgs": [],
        "chars": len(t),
    }
    low = t.lower()
    for name, pat in ATTACK_TYPES:
        if re.search(pat, low if name.isascii() else t, re.I):
            out["attack_types"].append(name)
    for name, pat in WEAPONS:
        if re.search(pat, low if name.isascii() else t, re.I):
            out["weapons"].append(name)
    for o in ORGS:
        if o.lower() in low:
            if o not in out["orgs"]:
                out["orgs"].append(o)
    return out


def fetch_article(url, timeout=20):
    """抓一篇文章的正文。"""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings()
        s = requests.Session()
        s.trust_env = True          # 境外站点需要代理
        r = s.get(url, timeout=timeout, verify=False,
                  headers={"User-Agent": UA,
                           "Accept-Language": "en,zh;q=0.9,*;q=0.8"})
        if r.status_code != 200:
            return None, "HTTP %s" % r.status_code
        enc = r.encoding
        if not enc or enc.lower() in ("iso-8859-1", "ascii"):
            r.encoding = r.apparent_encoding or "utf-8"
        return html_to_text(r.text), ""
    except Exception as e:  # noqa: BLE001
        return None, type(e).__name__


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judged", required=True, help="02_判定_带链接.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    rows = json.load(io.open(a.judged, encoding="utf-8"))
    todo = [r for r in rows if str(r.get("信息来源URL") or "").startswith("http")]
    if a.limit:
        todo = todo[:a.limit]

    done = {}
    if a.resume and os.path.isfile(a.out):
        try:
            done = json.load(io.open(a.out, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            done = {}

    print("")
    print("  抓 %d 篇正文（%d 线程）" % (len(todo), a.workers))

    def work(r):
        url = r["信息来源URL"]
        text, err = fetch_article(url)
        f = extract_fields(text) if text else {}
        return {
            "url": url,
            "ok": bool(text),
            "err": err,
            "summary": r.get("事件摘要"),
            "country": r.get("国家"),
            "date": r.get("日期"),
            "ucdp_deaths": r.get("死亡人数"),
            "text_len": len(text) if text else 0,
            "fields": f,
        }

    n = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, r): r for r in todo if r["信息来源URL"] not in done}
        for fu in as_completed(futs):
            n += 1
            try:
                rec = fu.result()
            except Exception:  # noqa: BLE001
                continue
            done[rec["url"]] = rec
            if n % 10 == 0:
                json.dump(done, io.open(a.out, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
                print("    %d/%d" % (n, len(futs)))

    json.dump(done, io.open(a.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 统计
    ok = [v for v in done.values() if v.get("ok")]
    print("")
    print("  抓到 %d / %d = %.0f%%" % (len(ok), len(done),
                                       100.0 * len(ok) / max(1, len(done))))
    if ok:
        def rate(k):
            n2 = sum(1 for v in ok if v["fields"].get(k) not in (None, [], 0))
            return n2, 100.0 * n2 / len(ok)
        for k, label in (("deaths", "死亡数"), ("injured", "受伤数"),
                         ("attack_types", "袭击方式"), ("weapons", "袭击武器"),
                         ("orgs", "组织名")):
            n2, p = rate(k)
            print("    %-8s %3d / %3d = %3.0f%%" % (label, n2, len(ok), p))
        tl = [v["text_len"] for v in ok]
        tl.sort()
        print("    正文长度中位 %d 字" % tl[len(tl) // 2])
    print("  已写出 → %s" % a.out)


if __name__ == "__main__":
    main()
