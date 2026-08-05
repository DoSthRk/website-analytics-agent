# GA4 与 GSC 指标口径

## GA4

| 输出字段 | 含义与使用边界 |
| --- | --- |
| `sessions` | 会话数；一次访问会话的计数，不是点击数。 |
| `totalUsers` | 报告区间内的总用户数；是区间聚合的去重用户数，不应把每日值相加。 |
| `activeUsers` | 报告区间内的活跃用户数；同样是区间聚合的去重用户数。 |
| `keyEvents`（key events） | 被 GA4 标记为关键事件的次数；应结合站点配置的事件名称解释，不等同于线索或收入。 |

## Google Search Console

| 指标 | 含义与使用边界 |
| --- | --- |
| `clicks` | Google 搜索结果带来的点击次数。 |
| `impressions` | Google 搜索结果中的展示次数。 |
| `CTR` | 点击率，计算为 `clicks / impressions`。 |
| `position` | 展示的平均排名位置；数值较小通常表示更靠前，应结合展示量和筛选维度解读。 |

## 不可直接等同

- **GA4 sessions 永不等于 GSC clicks**：两者的采集位置、归因、去重和口径不同，只能并列观察趋势。
- `totalUsers` 和 `activeUsers` 都是区间聚合的去重用户数，不能按日累计来构造区间总数。
- GSC 的 query/page 明细可能被截断：工具会为 Pages 和 Queries 施加行数上限。出现 `partial`、`truncated: true` 或退出码 `3` 时，只能描述已返回的明细，不能声称覆盖全部搜索词或页面。

## 日期边界与时区

- `selection_timezone` 只是将“上周”“本月”等相对日期转成 ISO 日期的本地选择约定；显式给出的 ISO 日期会原样传给数据源。
- GA4 数据按 property reporting timezone 划分报表日界线。使用前应核对 GA4 属性的时区是否与 `selection_timezone` 一致；该属性时区是报表日界线，详见 [GA4 Property timeZone](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties#Property.FIELDS.time_zone)。
- GSC 的 `startDate` 和 `endDate` 按 Pacific Time (PT, UTC-7/UTC-8) 解释，因此与 GA4 或 `selection_timezone` 的日界线可能不同，详见 [Search Analytics query](https://developers.google.com/webmaster-tools/v1/searchanalytics/query)。
