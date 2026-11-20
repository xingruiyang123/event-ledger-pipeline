# GOV.UK 定向入口配置

用户 2026-11-20 的要求：

> 「gov.uk：我建议定向到具体栏目，而不是整个网站。……不要直接把整个 gov.uk
>  的海量更新当成事件候选。」
> 「尤其是政府机构新闻稿与事件报道并不等价，应该保留来源类型和内容用途的区分。」

## 为什么 GOV.UK 能定向而普通新闻站不能

GOV.UK 有一套统一的检索接口，可以直接生成 atom feed：

```
https://www.gov.uk/search/news-and-communications.atom?<参数>
```

参数两种：

| 参数 | 含义 | 例 |
|---|---|---|
| `organisations[]` | 按发布机构筛 | `home-office`、`cabinet-office` |
| `keywords` | 按关键词筛 | `terrorism`、`national+security` |

两个可以叠加。这就是"定向栏目"的技术实现 —— 不需要抓全站。

## 实测可用的入口（2026-11-20）

| 名称 | 入口 | 条数 |
|---|---|---|
| Home Office 全部公告 | `...atom?organisations%5B%5D=home-office` | 20 |
| Home Office 涉恐 | `...atom?organisations%5B%5D=home-office&keywords=terrorism` | 20 |
| Cabinet Office | `...atom?organisations%5B%5D=cabinet-office` | 20 |
| 全政府涉恐 | `...atom?keywords=terrorism` | 20 |
| 全政府 national security | `...atom?keywords=national+security` | 20 |

（`...` = `https://www.gov.uk/search/news-and-communications`）

**Counter Terrorism Policing 返回 0 条** —— 它没有独立发布权，
公告走 Home Office 渠道。所以不单列。

## 来源类型必须区分（用户明确要求）

GOV.UK 的条目是**政府公告**，不是**事件报道**。两者性质不同：

| 维度 | 新闻媒体报道 | 政府公告 |
|---|---|---|
| 谁写的 | 记者，可能转述 | 当事机构，第一手 |
| 时间 | 事件发生后数小时 | 可能滞后数天到数周 |
| 内容 | 发生了什么 | 官方认定、处置、政策 |
| 用途 | 发现事件 | **核实事件、补充官方定性** |

所以进库时要打标：`source_type = government_announcement`，
不能和媒体报道混在同一池子里当"候选事件"。

实际用法：

- **发现层**：不用它。政府公告滞后，且只报"已定性的"，漏掉大量事件。
- **核实层**：用它。某事件是否为恐怖袭击、官方如何定性、有无起诉，
  这些只有政府公告能给。

这与台账的 19 列对应：`置信度`（官方公告可提高）、`审核状态`
（有官方认定的可标"已核"）、`备注`（记官方文号）。

## 配置方式

在信源主表里，一个信源可以挂多条入口。当前主表的 `入口URL` 是单值列，
所以这 5 条按「同一信源多行」录入，`信源名` 后加栏目后缀：

```
gov.uk ｜ Home Office 涉恐      ｜ ...keywords=terrorism
gov.uk ｜ Home Office 全部公告  ｜ ...organisations[]=home-office
gov.uk ｜ Cabinet Office        ｜ ...organisations[]=cabinet-office
```

三行共享同一个 `主域名`，调度器按行独立调度，互不影响。

## 维护

`keywords=terrorism` 这类关键词过滤的召回率取决于 GOV.UK 的全文索引，
不是标题匹配。实测 20 条/次（feed 上限），说明有内容。

要扩到别的机构，把 `organisations[]` 换成对应的 slug 即可，
slug 在 GOV.UK 机构页 URL 里能直接看到（如 `/government/organisations/home-office`）。
