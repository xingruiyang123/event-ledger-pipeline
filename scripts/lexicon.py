# -*- coding: utf-8 -*-
"""多语种事件词表。

为什么单独做一份
----------------
抽取层和分类层的正则原来是 **英文 + 中文** 两套词。
补了印尼、海地、墨西哥、莫桑比克、乍得、尼日尔的本地媒体之后，
实测 8 条当地语言的袭击报道只认出 1 条 —— 那条是西语的
「Explosión」，碰巧长得像英文 explosive。

采到了读不懂，等于没采。信源池补得再多也没用。

所以把「事件动作」和「伤亡后果」的识别词按语种集中在这里，
两边共用一份。新增语种只改这里。

语种覆盖（按已入池信源的语种定）
--------------------------------
  fr  法语        布基纳法索、马里、尼日尔、乍得、喀麦隆、海地、科特迪瓦
  es  西班牙语     墨西哥、哥伦比亚
  pt  葡萄牙语     莫桑比克、巴西
  id  印尼语       印尼
  ar  阿拉伯语     中东、北非
  zh  中文
  en  英语
"""
import re

# ============================================================
# 一、事件动作：发生了暴力袭击
# ============================================================
ATTACK = {
    "fr": [
        r"attaque[sr]?", r"attentat", r"assailli", r"assaut",
        r"tu[ée]s?", r"tuent", r"tuer", r"abattu", r"abattus",
        r"bombard", r"explos", r"attentat[- ]suicide",
        r"enlev[ée]", r"enl[èe]vement", r"kidnapp", r"otage",
        r"poignard", r"[ée]gorgen?[ée]?s?", r"mitraill", r"fusillade",
        r"incendi[ée]", r"massacr", r"embuscade", r"raid",
        r"prises? d'otages?", r"assassin",
    ],
    "es": [
        r"ataque?s?", r"atentado", r"asalto", r"agredid",
        r"mat(?:ó|aron|a)", r"muert", r"asesinat",
        r"bombard", r"explosi[óo]n", r"coche ?bomba", r"bomba",
        r"secuestr", r"plagi", r"reh[ée]n", r"rehenes",
        r"apu[ñn]al", r"degoll", r"disparad", r"tiroteo", r"balacera",
        r"incendi", r"masacr", r"emboscada",
    ],
    "pt": [
        r"ataques?", r"atentado", r"assalt", r"agredid",
        r"mat(?:ou|aram|a)", r"mort", r"assassin",
        r"bombard", r"explos[ãa]o", r"carro ?bomba", r"bomba",
        r"sequestr", r"rapt", r"ref[ée]m", r"refens",
        r"esfaque", r"decapit", r"dispar", r"tiroteio",
        r"incendi", r"massacr", r"emboscada",
    ],
    "id": [
        r"serang", r"menyerang", r"penyerangan", r"serangan",
        r"bom", r"ledakan", r"meledak", r"pemboman",
        r"tembak", r"menembak", r"penembakan", r"ditembak",
        r"tewas", r"tewaskan", r"terbunuh", r"membunuh", r"pembunuhan",
        r"culik", r"penculikan", r"diculik", r"sandera", r"penyanderaan",
        r"bajingan bersenjata", r"kelompok bersenjata", r"orang bersenjata",
        r"teroris", r"terorisme", r"penikaman", r"dibacok", r"dibakar",
        r"pembantaian", r"penyerbuan", r"amuk",
    ],
    "ar": [
        r"هجوم", r"اعتداء", r"هجمات", r"عملية\s*إرهابية",
        r"تفجير", r"انفجار", r"قصف", r"إطلاق\s*نار",
        r"اختطاف", r"رهائن", r"مقتل", r"قتل", r"جرح",
        r"طعن", r"ذبح", r"مذبحة", r"اغتيال",
    ],
    "en": [
        r"attack(?:ed|s|ing)?", r"assail\w*", r"assault(?:ed|s)?",
        r"bomb(?:ed|ing|s)?", r"blast(?:ed|s)?", r"explos\w+",
        r"shoot(?:ing|s)?", r"shot", r"opened fire",
        r"stab(?:bed|bing|s)?", r"slash(?:ed|ing)?",
        r"kidnap(?:ped|ping|s)?", r"abduct(?:ed|ion|ions|ing)?",
        r"ambush(?:ed|es|ing)?", r"raid(?:ed|s|ing)?",
        r"storm(?:ed|ing|s)?", r"torch(?:ed|ing)?",
        r"massacre[ds]?", r"hijack(?:ed|ing)?", r"siege",
        r"assassinat(?:ed|ion)", r"clash(?:es|ed|ing)?",
        r"fired (?:at|on|upon)", r"militants? (?:attack|struck)",
        r"insurgen", r"gunmen", r"gunman", r"suicide attack",
        r"car bomb", r"grenade", r"rocket", r"mortar",
    ],
    "zh": [
        r"袭击", r"爆炸", r"枪击", r"砍杀", r"纵火",
        r"绑架", r"劫持", r"伏击", r"交火", r"暗杀", r"刺杀",
        r"屠杀", r"扫射", r"自杀式",
    ],
}

# 动作本身就说明发生了暴力事件，不要求文本再写后果。
# 法语 attentat、印尼语 ledakan 这类，词本身只用于袭击语境。
INHERENT = {
    "fr": [r"attentat", r"bombard", r"mitraill", r"fusillade",
           r"poignard", r"[ée]gorgen?[ée]?s?", r"massacr",
           r"embuscade", r"prise[s]? d'otages?", r"assassin",
           r"raid", r"otage", r"enl[èe]vement", r"attaque[sr]?"],
    "es": [r"atentado", r"bombard", r"explosi[óo]n", r"coche ?bomba",
           r"masacr", r"emboscada", r"asesinat", r"secuestr",
           r"reh[ée]n", r"rehenes", r"tiroteo", r"balacera"],
    "pt": [r"atentado", r"bombard", r"explos[ãa]o", r"carro ?bomba",
           r"massacr", r"emboscada", r"assassin", r"sequestr",
           r"ref[ée]m", r"refens", r"tiroteio"],
    "id": [r"bom", r"ledakan", r"meledak", r"pemboman", r"penembakan",
           r"penculikan", r"penyanderaan", r"teroris", r"terorisme",
           r"pembantaian", r"penyerangan", r"serangan", r"penikaman"],
    "ar": [r"تفجير", r"انفجار", r"هجوم", r"اختطاف", r"رهائن",
           r"مذبحة", r"اغتيال", r"عملية\s*إرهابية"],
    "en": [r"bomb(?:ed|ing|s)?", r"blasts?", r"explos\w+", r"shoot(?:ing|s)?",
           r"shot", r"opened fire", r"stab(?:bed|bing)?",
           r"kidnap(?:ped|ping)?", r"abduct(?:ed|ion)?",
           r"ambush(?:ed)?", r"raid(?:ed)?", r"storm(?:ed)?",
           r"assassinat\w*", r"hijack(?:ed|ing)?", r"siege",
           r"suicide bomb\w*", r"massacr\w*"],
    "zh": [r"爆炸", r"枪击", r"砍杀", r"纵火", r"绑架", r"劫持",
           r"伏击", r"暗杀", r"袭击", r"屠杀", r"扫射"],
}

# ============================================================
# 二、伤亡与破坏后果
# ============================================================
HARM = {
    "fr": [
        r"tu[ée]s?", r"tuent", r"morts?", r"morte?s?", r"d[ée]c[èe]s",
        r"bless[ée]s?", r"blessures?", r"victimes?",
        r"cadavres?", r"corps", r"massacr[ée]s?",
        r"d[ée]truit", r"endommag", r"incendi", r"ras[ée]",
    ],
    "es": [
        r"muert(?:o|a|os|as)", r"mat(?:ó|aron|a)", r"fallecid",
        r"herid(?:o|a|os|as)", r"lesionad", r"v[íi]ctimas?",
        r"cad[áa]veres?", r"cuerpos", r"masacr",
        r"destruid", r"da[ñn]ad", r"incendi", r"arrasad",
    ],
    "pt": [
        r"mort(?:o|a|os|as)", r"mat(?:ou|aram|a)", r"falecid",
        r"ferid(?:o|a|os|as)", r"v[íi]timas?", r"cad[áa]veres?",
        r"corpos", r"massacr", r"destru[íi]", r"danific",
        r"incendi", r"arrasad",
    ],
    "id": [
        r"tewas", r"tewaskan", r"meninggal", r"terbunuh", r"korban",
        r"mati", r"membunuh", r"pembunuhan",
        r"luka", r"luka-luka", r"terluka", r"korban jiwa",
        r"jenazah", r"mayat", r"tewas di tempat",
        r"hancur", r"rusak", r"dibakar", r"diratakan",
    ],
    "ar": [
        r"مقتل", r"قتل", r"قتلى", r"وفيات", r"جثث", r"جثة",
        r"جرح", r"جرحى", r"مصاب", r"إصابات", r"ضحايا",
        r"تدمير", r"دمر", r"احتراق",
    ],
    "en": [
        r"kill(?:ed|s|ing)?", r"dead", r"deaths?", r"died",
        r"fatalit\w*", r"injur(?:ed|es|y|ing)", r"wound(?:ed|s|ing)?",
        r"casualt\w*", r"bodies", r"corpse", r"slain",
        r"murder(?:ed|s)?", r"massacred",
        r"destroyed", r"damaged", r"razed", r"torched",
        r"blown up", r"shelled", r"levelled", r"leveled",
    ],
    "zh": [
        r"死亡", r"受伤", r"伤亡", r"丧生", r"遇难", r"身亡", r"遇害",
        r"被毁", r"被炸", r"尸体",
    ],
}

# 绑架/劫持：受害者在报道时还活着，可能没有伤亡描述。
CAPTIVE = {
    "fr": [r"enl[èe]vement", r"enlev[ée]", r"kidnapp", r"otages?",
           r"disparu", r"port[ée]s? disparus"],
    "es": [r"secuestr", r"plagi", r"reh[ée]n", r"rehenes",
           r"desaparecid"],
    "pt": [r"sequestr", r"rapt", r"ref[ée]m", r"refens",
           r"desaparecid"],
    "id": [r"culik", r"penculikan", r"diculik", r"sandera",
           r"penyanderaan", r"hilang"],
    "ar": [r"اختطاف", r"رهائن", r"مفقود"],
    "en": [r"kidnap(?:ped|ping|s)?", r"abduct(?:ed|ion|ions|ing)?",
           r"taken hostage", r"held hostage", r"hostage", r"seized",
           r"missing"],
    "zh": [r"绑架", r"劫持", r"被绑", r"失踪"],
}

# ============================================================
# 三、袭击方式分类用词
# ============================================================
# 武装袭击 = 用枪械/火器/燃烧物/利器造成伤亡。只认动作，不认身份词。
ARMED = {
    "fr": [r"mitraill", r"fusillade", r"tir(?:s|é)", r"abattu",
           r"poignard", r"[ée]gorgen?[ée]?s?", r"incendi",
           r"br[ûu]l[ée]", r"armes? [àa] feu"],
    "es": [r"disparad", r"tiroteo", r"balacera", r"disparos",
           r"apu[ñn]al", r"degoll", r"incendi", r"quemad",
           r"armas? de fuego"],
    "pt": [r"dispar", r"tiroteio", r"disparos", r"esfaque",
           r"decapit", r"incendi", r"queimad", r"armas? de fogo"],
    "id": [r"tembak", r"menembak", r"penembakan", r"ditembak",
           r"senjata api", r"penikaman", r"dibacok", r"dibakar",
           r"membakar"],
    "ar": [r"إطلاق\s*نار", r"طعن", r"ذبح", r"حرق"],
    "en": [r"firearms?", r"rifles?", r"pistols?", r"shoot(?:ing|s)?",
           r"shot", r"opened fire", r"machetes?", r"knife", r"knives",
           r"stab(?:bing|bed|s)?", r"arson", r"set (?:fire|ablaze)",
           r"burn(?:ed|t|ing)"],
    "zh": [r"枪击", r"开枪", r"射击", r"砍杀", r"刀砍", r"纵火", r"焚烧"],
}

BOMBING = {
    "fr": [r"bomb(?:e|es|ardement)", r"explos", r"attentat[- ]suicide",
           r"voiture ?pi[ée]g[ée]e", r"engin explosif", r"IED",
           r"grenade", r"mine", r"roquette", r"mortier", r"missile"],
    "es": [r"bomba", r"explosi[óo]n", r"explosivo", r"coche ?bomba",
           r"artefacto explosivo", r"granada", r"mina", r"cohete",
           r"mortero", r"misil"],
    "pt": [r"bomba", r"explos[ãa]o", r"explosivo", r"carro ?bomba",
           r"artefato explosivo", r"granada", r"mina", r"foguete",
           r"morteiro", r"m[íi]ssil"],
    "id": [r"bom", r"ledakan", r"meledak", r"pemboman", r"bom bunuh diri",
           r"bom mobil", r"peledak", r"granat", r"ranjau", r"roket",
           r"mortir", r"misil"],
    "ar": [r"تفجير", r"انفجار", r"عبوة", r"قنبلة", r"حزام ناسف",
           r"صاروخ", r"قذيفة"],
    "en": [r"bombs?", r"bombed", r"bombing", r"blasts?", r"explos\w+",
           r"IEDs?", r"grenades?", r"landmines?", r"suicide bomb\w*",
           r"car bombs?", r"truck bombs?", r"rockets?", r"mortars?",
           r"missiles?", r"detonat\w*"],
    "zh": [r"爆炸", r"炸弹", r"引爆", r"自杀式", r"路边炸弹"],
}

KIDNAP = {
    "fr": [r"enl[èe]vement", r"enlev[ée]", r"kidnapp", r"otages?",
           r"ran[çc]on"],
    "es": [r"secuestr", r"plagi", r"reh[ée]n", r"rescate"],
    "pt": [r"sequestr", r"rapt", r"ref[ée]m", r"resgate"],
    "id": [r"culik", r"penculikan", r"diculik", r"sandera",
           r"penyanderaan", r"tebusan"],
    "ar": [r"اختطاف", r"رهائن", r"فدية"],
    "en": [r"kidnap(?:ped|ping|s)?", r"abduct(?:ed|ion|ions|ing)?",
           r"taken hostage", r"held captive", r"ransom"],
    "zh": [r"绑架", r"劫持人质", r"掳走", r"勒索"],
}

# 劫持：公开控制人质、发生在目标地点、时间较短
HIJACK = {
    "fr": [r"prise[s]? d'otages?", r"si[èe]ge", r"retranch",
           r"assailli.{0,20}(?:h[ôo]tel|[ée]cole|march[ée]|banque)"],
    "es": [r"toma de rehenes", r"asedio", r"sitiad",
           r"asalto.{0,20}(?:hotel|escuela|mercado|banco)"],
    "pt": [r"tomada de ref[ée]ns", r"cerco", r"assalt",
           r"invas[ãa]o"],
    "id": [r"penyanderaan", r"disandera", r"menguasai",
           r"menyerbu.{0,20}(?:hotel|sekolah|pasar|bank)"],
    "ar": [r"احتجاز رهائن", r"حصار"],
    "en": [r"hijack(?:ed|ing|s)?", r"hostage (?:crisis|situation|standoff)",
           r"siege", r"barricaded",
           # 冠词必须可选含 a/an —— 原文写「stormed a hotel」，
           # 只写 (?:the )? 就一条都匹配不到。
           r"stormed (?:the |a |an )?(?:building|hotel|cafe|restaurant|school|"
           r"hospital|mall|airport)",
           r"held \d+ (?:people )?hostage", r"took over the",
           r"took \d+ hostage"],
    "zh": [r"劫持", r"围攻", r"占领"],
}


# ============================================================
# 四、编译成正则
# ============================================================
def _alt(items):
    """把一个词表列表拼成一个 alternation。按长度倒序，长词优先匹配。

    拉丁语系的词干默认补 `\\w*`，伸到词尾。
    不补的话「secuestró」「sequestraram」这类屈折形式匹配不到 ——
    `\\b` 要求词尾是边界，而 ó、aram 都还是字母。
    实测西语 secuestró / 葡语 sequestraram / 印尼语 menembak mati
    三条全因此漏掉。
    已经写了量词或括号的词（attack(?:ed|s)?）保持原样，不重复加。
    """
    flat = []
    for it in items:
        if re.search(r"\(\?:|\\w|\\s|\\d|[?*+]", it):
            flat.append(it)
        else:
            flat.append(it + r"\w*")
    flat.sort(key=len, reverse=True)
    return "|".join(flat)


def build(*dicts, flags=re.I):
    """把若干 {语种: [词...]} 合并成一个正则。

    拉丁语系用 \\b 边界；中日韩、阿拉伯不用（没有词边界）。
    """
    latin, other = [], []
    for d in dicts:
        for lg, words in d.items():
            if lg in ("en", "fr", "es", "pt", "id"):
                latin.extend(words)
            else:
                other.extend(words)
    parts = []
    if latin:
        parts.append(r"\b(?:" + _alt(latin) + r")\b")
    if other:
        parts.append(r"(?:" + _alt(other) + r")")
    return re.compile("|".join(parts), flags)


def languages():
    return sorted(set(ATTACK) | set(HARM))


if __name__ == "__main__":
    # 自测：每个语种的典型袭击句都要能认出来
    CASES = [
        ("id", "Serangan bom di Poso tewaskan tiga warga, delapan luka-luka"),
        ("id", "Kelompok bersenjata menembak mati seorang polisi di Papua"),
        ("id", "Dua warga diculik dan disandera oleh kelompok teroris"),
        ("fr", "Une attaque terroriste a tué 12 personnes et blessé 20 autres"),
        ("fr", "Des hommes armés ont enlevé 30 écoliers dans le nord"),
        ("fr", "Un attentat-suicide a fait 45 morts dans une mosquée"),
        ("es", "Un ataque armado dejó cinco muertos y diez heridos en Sinaloa"),
        ("es", "Explosión de coche bomba mata a 8 personas en el mercado"),
        ("es", "Un grupo armado secuestró a 20 niños en la aldea"),
        ("pt", "Um ataque com bomba matou 6 pessoas em Cabo Delgado"),
        ("pt", "Homens armados sequestraram 20 crianças na aldeia"),
        ("ar", "هجوم مسلح أسفر عن مقتل 12 شخصا وجرح 20 آخرين"),
        ("en", "Gunmen attacked a village, killing 12"),
        ("zh", "武装分子袭击村庄，造成12人死亡"),
    ]
    atk = build(ATTACK)
    herm = build(HARM)
    inh = build(INHERENT)
    cap = build(CAPTIVE)
    bad = 0
    for lg, t in CASES:
        ok = bool(atk.search(t)) and (bool(herm.search(t))
                                      or bool(inh.search(t))
                                      or bool(cap.search(t)))
        if not ok:
            bad += 1
        print("  %s %-3s 动作=%-5s 后果=%-5s %s"
              % ("OK " if ok else "错", lg,
                 bool(atk.search(t)), bool(herm.search(t)), t[:44]))
    print()
    print("  语种：%s" % ",".join(languages()))
    print("  错 %d / %d" % (bad, len(CASES)))
    raise SystemExit(0 if bad == 0 else 2)
