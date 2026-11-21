# 地名词表

`assets/place_gazetteer.jsonl.gz` —— 8.6 万条地名、17 种语种的 16 万种写法，
压缩后 2 MB。抽取层用它把正文里的「地点」那一列填出来。

## 为什么需要它

同一地方在报道里的写法差得很远：

| 写法 | 来源 |
|---|---|
| `Peshawar` | 英文标准拼法 |
| `Peshāwar` | 带变音符号 |
| `پشاور` | 乌尔都文 |
| `白沙瓦` | 中文 |
| `Peshawar District` | 行政区 |

不做词表匹配，只能人工一条条改。而中文报道主要用中文地名，
巴基斯坦本地媒体用乌尔都文 —— 只按英文匹配会漏掉一半。

## 数据来源

[GeoNames](https://download.geonames.org/export/dump/) 全量导出（CC BY 4.0）：

| 文件 | 用途 |
|---|---|
| `cities15000.zip` | 1.5 万人以上居民点，34,248 条 |
| `admin1CodesASCII.txt` | 一级行政区（省/州），3,865 条 |
| `admin2Codes.txt` | 二级行政区（县/区），47,749 条 |
| `countryInfo.txt` | 国家信息，252 条 |
| `alternateNamesV2.zip` | 多语种别名，取自其中 17 种语言 |

中文国家名来自 [babel](https://babel.pocoo.org/) 的 CLDR 数据（比 GeoNames 的
`zh` 别名更全）。缺中文名的城市用 `fill_zh_place_names.py` 从
[维基数据](https://www.wikidata.org/) 补。

## 重新生成

```bash
# 1. 下载原始数据（约 210 MB）
python scripts/build_gazetteer_full.py    # 会自动提示缺哪个文件

# 2. 生成词表
python scripts/build_gazetteer_full.py \
  --full D:/data/gazetteer_full.jsonl \
  --slim assets/place_gazetteer.jsonl

# 3. 压缩（仓库里只放压缩版）
gzip -9 assets/place_gazetteer.jsonl
```

原始数据不在仓库里 —— 210 MB 放不进 GitHub。`build_gazetteer_full.py` 的
`RAW` 常量指向本地缓存目录，换机器时改这一行。

## 用法

```python
import sys
sys.path.insert(0, "scripts")
from place_match import Gazetteer

g = Gazetteer()                      # 加载约 1 秒
hits = g.find("A bombing in Peshawar killed three. 白沙瓦也有袭击。")
for h in hits:
    e = h["entry"]
    print(h["form"], e["country_zh"], e["admin1"], e["population"])
# peshawar 巴基斯坦 Khyber Pakhtunkhwa 4758762
# 白沙瓦    巴基斯坦 Khyber Pakhtunkhwa 4758762

# 同名多义时取候选
g.disambiguate("Bagh")               # 巴控克什米尔 ×2、旁遮普 ×1
```

命令行：

```bash
python scripts/place_match.py --text "Attack in Kabul and Kandahar"
python scripts/place_match.py --file article.txt --country AF
```

## 三层防误配

地名匹配最大的风险不是漏，是**把普通词当地名**。用 40 篇真实正文跑出来的
问题，不是想象出来的：

| 误配 | 实际是什么 | 出现次数 |
|---|---|---|
| `media` → 阿尔及利亚 Medea | 英文「媒体」 | 38 |
| `massacre` → 圣卢西亚 Massacre 村 | 英文「屠杀」 | 多次 |
| `mali` → 土库曼斯坦 Mali 村 | 国家马里的小写 | 多次 |
| `content` → 牙买加 Content 村 | 英文「内容」 | 多次 |
| `soci` → 俄罗斯 Sochi 的别名 | 法语「社会」词根 | 31 |

这些全是**真实存在的地名**，光看词表发现不了。挡住它们靠三层：

1. **拉丁写法必须在原文里首字母大写。** 这一条最有效。
2. **普通词黑名单**（`STOP_NAMES`，约 200 个）。
3. **城市人口 ≥ 2 万、二级行政区默认不参与。** 小村庄的报道里
   几乎一定带上级地名，靠上级地名匹配就够了。

另有一条：4-5 个字母的**别名**单独收紧，只放行主名和国家/一级行政区。
`Soci`（Sochi 的别名）就是这么挡掉的。

## 已知残留问题

**菜单式罗列会误配。** 有些站的正文抽取把侧边栏「国家列表」也带进来了：

```
... Côte d'Ivoire Burkina Faso Gabon Congo Sénégal Bénin Angola ...
```

这段会被识别出加蓬、安哥拉、布基纳法索等一串国家，而文章讲的是加纳。
这是**抽取层**的问题（正文边界没切干净），不是词表的问题，
不在这一层解决。表现是单篇命中数异常高（25-40 处），
下游按「同一国家出现次数最高」聚合时不受影响。

**中文名覆盖不全。** 巴基斯坦 375 个城市里约 100 个有中文名，
维基数据也补不全小城市。缺的就留空，不编 ——
下游用英文名匹配，比填错强。

**坐标只有城市级有。** 国家、一级、二级行政区在 GeoNames 里是分开的表，
坐标需要额外关联。当前版本没做，需要时按 `id` 回查 `admin1Codes` 补。

## 更新频率

地名变化很慢，一年重建一次足够。行政边界调整（如巴基斯坦新设省）
可以单独跑 `build_gazetteer_full.py` 覆盖。
