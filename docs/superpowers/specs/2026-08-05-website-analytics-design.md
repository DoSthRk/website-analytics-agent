# 官网数据 Skill：第一期设计

## 目标

构建一个可在本机运行的只读官网数据工具，并由 Codex Skill 调用。用户以自然语言提出“某站点、某时间范围、某项指标”的请求后，Skill 调用受控命令拉取 GA4 与 Google Search Console（GSC）数据，输出中文分析和可追溯的 Excel 工作簿。

第一期仅覆盖 GA4 与 GSC；不接入 Cloudflare、表单、CRM、数据库写回或线上站点变更。

## 使用场景

- “分析上周 example.com 的访问、自然搜索和核心页面变化。”
- “导出 example.com 上周的 GA4 和 GSC 原始数据及汇总到 Excel。”
- “比较本周与前四周，找出点击或会话下降的页面和关键词。”

## 架构

```text
Codex Skill
  -> Python CLI
       -> GA4 Data API adapter
       -> GSC Search Analytics API adapter
       -> normalizer / comparator / Excel exporter
  -> local cache + audit manifest
  -> Chinese narrative + XLSX
```

Skill 只处理请求解析、数据口径、调用顺序和结果解释。API 调用、分页、重试、聚合、日期计算和 Excel 生成必须位于可测试的 Python 代码中，不能由模型临时拼接。

## 站点与凭据配置

每个站点在 `config/sites.example.yaml` 中定义：

- `site_key`：供用户和命令使用的稳定标识。
- `display_name`、`domains`、`timezone`。
- `ga4_property_id` 与 `gsc_property_url`。
- 默认转换事件列表（可为空）。

真实配置文件为忽略提交的 `config/sites.yaml`。Google OAuth/service-account 凭据、令牌和密钥只能由环境变量或本机密钥管理服务注入，不写入 Skill、YAML、日志或导出文件。

## 指标口径

GA4 与 GSC 保持为独立事实源，不求逐行相加：

| 数据源 | 第一期开出指标 |
| --- | --- |
| GA4 | sessions、totalUsers、activeUsers、engagedSessions、engagementRate、screenPageViews、keyEvents |
| GSC | clicks、impressions、ctr、position，按日期、页面、查询词、国家或设备拆分 |

报告必须标出来源、数据截至日期、查询时间、时区和维度。GA4 的会话/用户不得与 GSC 点击相等同；GSC 的关键词分页结果不得宣称为全量搜索词总和。

## CLI 合约

统一入口为 `python -m website_analytics`，并支持：

- `fetch --site <key> --start YYYY-MM-DD --end YYYY-MM-DD [--dimensions ...]`：拉取并缓存标准化数据。
- `report --site <key> --start ... --end ... --compare <previous-period|previous-4-weeks>`：生成结构化 JSON 摘要。
- `export-excel --site <key> --start ... --end ... --output <path>`：生成 Excel。
- `validate-config --site <key>`：离线校验配置结构，不访问 API。

CLI 输入只接受已登记站点、ISO 日期和白名单维度；不提供任意 SQL 或任意 API URL/参数透传。

## 本地数据与输出

原始 API 响应和标准化数据保存在本地缓存目录，按 `site/source/date-range/request-hash` 隔离。每次报告写入一个不含凭据的审计清单，记录请求参数、数据源、行数、拉取时间、版本和输出文件哈希。

Excel 工作簿包含：

1. `README`：站点、时间范围、数据源、口径、刷新时间与限制。
2. `Executive Summary`：核心 GA4/GSC 指标及比较期变化。
3. `GA4 Daily`、`GA4 Pages`：GA4 原始/标准化明细。
4. `GSC Daily`、`GSC Pages`、`GSC Queries`：GSC 明细。
5. `Audit`：请求与生成记录。

## 错误处理

- 配置缺失、权限不足或授权过期：明确指出来源和修复项，不臆造结果。
- 单一来源失败：保留其他成功来源，报告标示“不完整”，退出码非零。
- API 限流或瞬时网络失败：有限次数指数退避；失败记录进审计清单。
- 空结果：区分“合法无数据”与“查询/授权失败”。

## Codex Skill 行为

Skill 在涉及“官网、GA4、GSC、流量、自然搜索、关键词、页面表现、周报、Excel 导出”的请求时触发。它必须：

1. 从用户话语中确认站点、时间范围、目标指标和是否要求导出。
2. 在信息不完整时只询问一个必要缺失项。
3. 调用上述 CLI，读取结构化结果，而非自行请求 Google API。
4. 先呈现数据来源和事实，再把归因说明为假设或待核查项。
5. 保持只读；任何写入 GA/GSC、修改站点或传输凭据均不在范围内。

## 测试与验收

- 适配器使用录制的 JSON fixture，不依赖真实 Google 帐号。
- 覆盖日期解析、站点白名单、GA4/GSC 标准化、比较计算、部分失败、审计清单与 Excel 表结构。
- 运行完整测试套件后，生成一个 fixture 驱动的 `.xlsx`，并检查工作簿的工作表、单元格内容和压缩包完整性。
- 使用 `validate-config` 验证示例站点配置。

## 非目标

- Cloudflare、表单、CRM、广告平台和服务器日志接入。
- BigQuery、PostgreSQL、后台 Web UI、定时调度与告警。
- 管理 GA/GSC 帐号、写入配置或更改网站埋点。

后续阶段可以在不改变 CLI 报告合约的前提下，添加数据源、定时缓存、PostgreSQL 和 MCP 包装层。
