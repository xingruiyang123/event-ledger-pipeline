# 抽取层：正文 → 19 列台账候选行

## 它在整条链路的哪一环

```
feed 层   抓到条目（不筛，丢不起）
   ↓
正文层    抓全文（按标题初筛）
   ↓
抽取层    正文 → 事件字段 → 台账的其中几列        ← 本文档
   ↓
判定层    算不算恐怖主义事件（人工 + 复核）
```

**抽取不等于判定。** 这一层只负责「把文章里写了什么读出来」，
不负责「算不算恐怖袭击」。两者混在一起会出现的情况是：
判定标准一改，抽取结果全得重跑。分开之后只需要重跑判定。

## 能填 14 列，其余留空

| 填 | 留空 |
|---|---|
| 事件摘要、国家、年份、月份、日期、地点 | 负责人/组织（要读「claimed responsibility」句式，下一版） |
| 死亡人数、受伤人数、袭击方式、袭击武器、袭击者 | 关联知识点（人工环节产物） |
| 信息来源URL、采集人/组别、录入日期、置信度、审核状态 | 备注（只放抽取依据，不放判断） |

「审核状态」统一填「待审」。机器填别的会污染人工判断。

## 用法

```bash
# 基础
python scripts/build_ledger_rows.py --out 台账.csv

# 带站点模板地名剔除（推荐，先生成黑名单）
python scripts/detect_template_places.py \
    --ledger 台账.csv --registry 信源主表.xlsx --out 模板地名.json
python scripts/build_ledger_rows.py \
    --template-places 模板地名.json --out 台账.csv

# 不做标题初筛（调试用，会把所有文章都列出来）
python scripts/build_ledger_rows.py --no-prefilter --out 全量.csv
```

## 实测产出（2026-11-21，15,154 篇已抓正文）

```
初筛剔除 1,843 篇 ｜ 进入抽取 13,326 篇 ｜ 有正文 12,931 篇
置信度  高 299 / 中 4,763 / 低 7,335 / 极低 534
有地点 11,910（92%）｜ 有组织 2,088（16%）｜ 有死亡数字 645（5%）
```

**这个覆盖率要说清楚。** 死亡数字只有 5%，不是新闻里不写，
而是这一层只从**首段**抽 —— 全文抽会把「22 Indians had been killed」
这种历史背景当成这次事件的死亡数（实测抽出了「死亡 2014 人」）。
宁缺勿错。

同样的道理，「有组织 16%」也不代表八成文章没写袭击者 ——
组织名识别只收了本项目信源里真会出现的三十来个，
写全没有意义，匹配不到的落到「未知」比错配强。

## 踩过的坑（每一条都是实测出来的）

### 一、词边界：`stab(?:bing|bed)?` 把 establishment 判成砍杀

没有 `\b`，`establishment`、`stability`、`constable` 全被当成砍杀。
300 篇里报出 46 起「砍杀」，实际一起都没有。加 `\b` 后降到 9 起。

同类：`dead` 匹配到 `deadline`，把
「1,500 charities risk missing accounts deadline」抽成死亡 1500 人。

### 二、缩写必须全大写

「RSF」是苏丹快速支援部队，但小写 `rsf` 在英文正文里可能是任何东西。
「the meeting discussed rsf」也被认成快速支援部队。
改成缩写必须全大写出现。

### 三、大洲名被 GeoNames 挂成了具体城市的别名

`Mahdia`（突尼斯城市）的别名里有 **"Africa"**。
于是正文页脚导航词「Africa」被当成城市名，
semafor.com 的 855 篇里 731 篇被判成「突尼斯·马赫迪耶」——
而它们讲的是加沙、乌克兰、拜登竞选。

同类：`Sahel`（布基纳法索行政区）、`Central`/`East`/`West`/`North`/`South`
（一堆国家的行政区）、`media`（阿尔及利亚城市 Medea 的别名）。

已加进 `place_match.STOP_NAMES`。

### 四、人名/品牌名里的地名

`Lockheed Martin to manufacture ExLS naval missile launchers`
→ 被判成斯洛伐克城市「马丁」，372 篇美军装备报道全算到斯洛伐克头上。

判据：单字命中时，若前一个词也是大写开头且紧邻，多半是人名的一部分。
双词以上命中（Port Said、New Delhi、Sao Paulo）不受此限，
因为多词地名不是普通英文句子能碰巧凑出来的。

这条例外不能省：`Port Said was hit by a blast` 里 Port 在句首，
早期版本跳过所有句首大写词，整条地名就丢了。

### 五、国家全填成吉布提

koaci.com 的正文里混着一串国家链接：
`Bénin Afrique du Sud Angola Botswana Burundi Cameroun … Djibouti …`
它落在正文前 1/3，只按位置截断挡不住。

判据改成**窗口**：任意 400 字符内出现 ≥5 个国家/省级/首都名 → 判为导航列表，
整段抹掉并向两端外扩到相邻地名。修好后吉布提消失，
科特迪瓦/布基纳法索这些真相关的国家浮现。

### 六、死亡数被安到受伤数上

- 第一版：「Seven civilians were **murdered** and 14 others **injured**」
  抽成死亡 14（14 是受伤数，7 才是死亡数）
- 第二版：加后处理「数字后面先出现受伤词就不算死亡」→
  把「12 killed and 30 injured」判成没有死亡数
- 第三版：数字归给最近的提示词 → 14 距 murdered 5 字、距 injured 8 字，
  仍然判给 murdered
- 第四版（现在）：**以提示词为中心**，按出现顺序走每个提示词，
  先往左找最近的未占用数字，左边没有才往右。
  `Seven ← murdered`、`14 ← injured`、`killed → 12`、`injured → 30`。

### 七、四位数年份被当死亡数

抽出过「死亡 2023」「死亡 2009」「死亡 1998」「死亡 2026」共 48 条。

但不能简单地「看着像年份就丢」——「2,026 people were killed」
这种四位数伤亡是真事（大规模屠杀确实能到四位数）。
所以只在**上下文有年份标记**时丢：前面是
`since/in/by/until/from/during/of/the/toll/year` 或法语 `de/du/en`。

同时「the 2023 death toll as 206」这种句式要能取到 206 —— 修完后是对的。

### 八、正文抽取把整页压成一行，首段变成页头导航

原来走「去标签 + 压空白」，整页变成一行：

```
| Environment | News Hi Subscribe Activate Manage eEdition Sign In Sign Out
99&cent; for 6 mos Support local journalism.
```

死亡数从首段抽，首段是导航栏，所以什么都抽不到 ——
14,899 篇里只有 563 篇（4%）有死亡数字，不是新闻里没写，是没读到正文。

现在改成：标签换**换行**保住段落边界 → 丢掉
`script/style/nav/footer/header/aside` → 按行长和字符集筛掉导航行 →
在剩下的行里找最长连续块。

## 还没解决的

1. **标题初筛太宽。** 复用 `fetch_articles.KW`（为「发现」设计，宁多勿少），
   12,931 行里大多数跟恐怖袭击无关。这一层产出的是**候选**，
   要判定层再过一遍才算台账。想做严一点可以用 `--only-attack`。
2. **年份残留 27 条**（占 645 条死亡数字的 4%），都是「2023」「2018」
   与别的数字拼在一起的情况。已修大部分，剩下的靠判定层人工核。
3. **站点模板地名只识别出 24 个站。** 主表里没登记的站不判模板
   （保守起见），新补的源要先跑一遍检测。
4. **不做 JS 渲染。** 靠前端渲染的站取不到正文。
5. **不做同事件合并。** 同一袭击被 10 家媒体报道，这里出 10 行。
   去重是判定层的事。

## 相关脚本

| 脚本 | 作用 |
|---|---|
| `build_ledger_rows.py` | 本文档描述的主脚本 |
| `detect_template_places.py` | 识别站点模板地名（两条件：地点不属于该站所在国 + 站内集中度 ≥55%） |
| `place_match.py` | 地名词表匹配，8.6 万条 / 16 万写法 |
| `extract_events.py` | 更早的结构化抽取（actor/victim/act），判定层会用到 |
| `extract_article_fields.py` | 单篇字段抽取的试验版本 |