# 已知信源入口（探测探不到、但确实存在的）

**为什么单列一份**：`probe_fast.py` 只能试常见路径。大型媒体的 RSS 地址是
历史遗留的、不规则的、也不写在首页 `<link rel="alternate">` 里，
通用探测找不着。但这些地址是公开固定的，一旦知道了就不用再猜。

实测（2026-11-20）：P1 级 24 家缺入口的信源里，用这份清单直接补上了 9 家，
P1 覆盖率从 60% 升到 75%，每轮多拿 266 条。

## 通讯社与国际大报

| 信源 | 入口 | 条数 |
|---|---|---|
| The New York Times | `https://rss.nytimes.com/services/xml/rss/nyt/World.xml` | 57 |
| The Times of India | `https://timesofindia.indiatimes.com/rssfeedstopstories.cms` | 47 |
| The Jerusalem Post | `https://www.jpost.com/rss/rssfeedsfrontpage.aspx` | 26 |
| ABC News (US) | `https://abcnews.go.com/abcnews/internationalheadlines` | 25 |
| ABC Online (AU) | `https://www.abc.net.au/news/feed/51120/rss.xml` | 25 |
| France 24 | `https://www.france24.com/en/rss` | 24 |
| RFI | `https://www.rfi.fr/en/rss` | 22 |
| NDTV | `https://feeds.feedburner.com/ndtvnews-top-stories` | 20 |
| Bloomberg | `https://feeds.bloomberg.com/markets/news.rss` | 20 |

## 仍未找到的（试过常见路径均失败）

| 信源 | 试过的路径 | 备注 |
|---|---|---|
| AFP | 无公开 RSS | 商业授权，只能抓网页或走搜索 |
| Al Arabiya | `/tools/rss`、`/.mrss/en.xml` | 可能改了路径或封了境外 IP |
| ThePrint (IN) | `/feed/` | 403 |
| The New Arab | `/rss`、`/feed` | 403 |
| Outlook India | `/rss/news.xml`、`/rss` | 403 |
| Tasnim (IR) | `/en/rss/feed/0/8/0/all-news` | 疑似封境外 |
| Saba (YE) | `/rss` | 站点可能已改版 |
| Yonhap | — | 未试 |
| CGTN | — | 未试，但 cgtn.com 的 sitemap 已通 |
| 巴基斯坦联邦调查局 | `fia.gov.pk` 无 RSS | 官方站，需网页列表页适配 |

## 用法

补进信源主表的「入口URL」「入口类型」两列即可，`scheduler.py` 直接认。

## 维护约定

新增一条必须满足：**实测过 200 且解析出条目**，不写"应该有"的地址。
表里的条数是 2026-11-20 实测值，会随后续运行变化。
