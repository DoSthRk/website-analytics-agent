# Website Analytics Agent

## Codex Skill 源包

面向本地 Agent 的源 Skill 位于 `skill/website-analytics/`，其中包含受限命令流程和指标口径。仓库只跟踪该源包，不会自动安装到用户的 Codex skills 目录；审阅通过后再由用户按本机的 Skill 安装流程安装。可用以下命令检查源包结构：

```powershell
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$validator = Join-Path $codexHome 'skills\.system\skill-creator\scripts\quick_validate.py'
if (-not (Test-Path -LiteralPath $validator)) {
    throw "找不到 Skill validator；请使用包含 skill-creator 的 Codex 安装环境。"
}
& .\.venv\Scripts\python.exe -X utf8 $validator skill\website-analytics
```

一个面向本地 Agent 的官网数据命令行工具：将已登记官网的 GA4 与 Google Search Console（GSC）数据拉取、对比、缓存审计并导出为 Excel。第一阶段只覆盖 **GA4 + GSC**，所有数据访问均为只读。

工具只接受配置文件中登记的网站，不提供任意 URL、任意 API 请求体、维度、SQL 或凭据参数。命令结果只会输出机器可读 JSON；输入和配置错误输出到标准错误。

## 安全边界

- GA4 使用 Analytics Data API 的只读范围；GSC 使用 Search Console 的只读范围。
- 不在命令行、配置文件或缓存中保存、显示或传递凭据。`config/sites.yaml`、缓存、审计和导出目录默认均被 Git 忽略。
- 实际访问数据时使用 Google Application Default Credentials（ADC），或由 Google 客户端库使用 `GOOGLE_APPLICATION_CREDENTIALS` 所指向的本机凭据文件。不要把凭据内容复制进本项目，也不要提交该文件。
- `validate-config` 完全离线，不会创建 Google 客户端；带 `--fixture-dir` 的数据命令也完全离线。
- GSC Pages 与 GSC Queries 明细最多读取 50,000 行；如果触及该上限，结果会以结构化 `truncated: true` 标记为部分结果、命令返回码为 `3`，不能视为完整查询词或页面导出。

## 安装与站点配置

需要 Python 3.11+；Excel 导出还需要 Node.js 和 Codex 工作区提供的 Artifact Tool 运行环境。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item config\sites.example.yaml config\sites.yaml
```

仅在本机编辑 `config/sites.yaml`，为每个官网登记 `ga4_property_id`、`gsc_property_url`、时区、域名和可选的关键事件名称；不要把真实配置提交到 Git。

先离线检查配置：

```powershell
.\.venv\Scripts\python.exe -m website_analytics validate-config --site demo --config config\sites.yaml
```

成功时标准输出为 JSON，退出码为 `0`。参数或配置不合法时退出码为 `2`；数据源失败或只有部分来源成功时为 `3`。

对 `report` 与 `export-excel`，顶层 `sources` 表示当前期来源状态，`comparison.sources` 表示比较期来源状态；两期任一来源被截断时，顶层与 `comparison.complete` 都会是 `false`，并返回 `status: "partial"` 与退出码 `3`。

## 日常使用

以下命令都只接受已登记的 `--site` 与 ISO 日期（`YYYY-MM-DD`）。`report` 和 `export-excel` 默认同比紧邻的等长上一周期；可选的另一种受限比较为 `--compare previous-4-weeks`。

```powershell
# 拉取本期数据，写入本地脱敏缓存和审计清单
.\.venv\Scripts\python.exe -m website_analytics fetch --site demo --start 2026-08-03 --end 2026-08-09 --config config\sites.yaml

# 生成本期与上一周期的机器可读汇总
.\.venv\Scripts\python.exe -m website_analytics report --site demo --start 2026-08-03 --end 2026-08-09 --config config\sites.yaml

# 导出固定工作表结构的 Excel，同时生成每个工作表的渲染图用于检查
.\.venv\Scripts\python.exe -m website_analytics export-excel --site demo --start 2026-08-03 --end 2026-08-09 --config config\sites.yaml --output exports\demo-weekly.xlsx
```

如果系统找不到 `node`，设置 `WEBSITE_ANALYTICS_NODE` 为本机 Node.js 可执行文件路径后再运行导出。该环境变量仅用于定位 Node.js，不应存放任何密钥。

### 首次克隆后的 Excel 运行环境

项目不会提交 `node_modules`，也不会安装或下载 npm 包。首次克隆时，请先在 Codex 中运行 **load workspace dependencies**，取得其返回的 Node.js 可执行文件路径和 Node modules 目录路径；两项路径都只来自本机 Codex 运行环境。

在项目根目录以占位符替换为上一步返回的路径（不要把真实路径、凭据或任何密钥写进仓库）：

```powershell
$env:WEBSITE_ANALYTICS_NODE = '<Node executable path returned by the Codex dependency loader>'
.\scripts\setup-artifact-tool-runtime.ps1 -NodeModulesPath '<Node modules directory returned by the Codex dependency loader>'
```

脚本会验证目录中存在 `@oai/artifact-tool`，然后仅在本项目下创建被 Git 忽略的 `node_modules` junction。它不会猜测路径、安装依赖或覆盖已有目录；如本地 runtime 缺失，`export-excel` 会给出相同的修复提示。

## 无 Google 账号的离线演示

测试夹具会绕过所有 Google 客户端创建与网络调用：

```powershell
.\.venv\Scripts\python.exe -m website_analytics export-excel --site demo --start 2026-08-03 --end 2026-08-09 --config config\sites.example.yaml --fixture-dir tests\fixtures --output exports\fixture-demo.xlsx
```

这会生成固定顺序的 README、Executive Summary、GA4 Daily、GA4 Pages、GSC Daily、GSC Pages、GSC Queries 和 Audit 工作表。输出 JSON 会记录来源状态、日期范围、数据新鲜度和导出验证结果；不会输出原始 Google API JSON。

## 日期边界与时区

配置中的 `selection_timezone`（站点配置字段仍为 `timezone`）只是把“上周”“本月”等相对日期转换为 ISO 日期时使用的本地选择约定；显式输入的 ISO 日期会原样提交，不会被此字段重新解释。

- GA4 报表使用 GA4 property reporting timezone 作为报表日界线；上线前应在 GA4 属性中核对该时区是否与 `selection_timezone` 一致。[官方 GA4 Property timeZone 文档](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties#Property.FIELDS.time_zone)
- Search Console 的 `startDate` / `endDate` 按 Pacific Time (PT, UTC-7/UTC-8) 解释，所以与 GA4 或本地选择约定的每日边界可能不同。[官方 Search Analytics query 文档](https://developers.google.com/webmaster-tools/v1/searchanalytics/query)

## 页面与产品映射报告

当站点存在页面分类、产品映射和信息页映射配置时，`report` 会返回页面类型及业务维度汇总，`export-excel` 会在原始 GA4/GSC 工作表之外自动加入：

- `Product Weekly Summary`：按已审核的三级产品分类汇总本期、上一期和变化；GA4 Sessions 与 GSC Clicks / Impressions / CTR 保持独立，绝不合并为同一种“流量”。
- `Page Type Summary`：分别汇总产品页、信息页、技术页面、未映射页面、异常页面和 PDF 资源，并报告 GA4、GSC、询盘各自的分类覆盖率。
- `Page Classification`：列出本期实际出现的页面、数据库页面 ID、模板、分类证据和异常状态。
- `Product Page Mapping`：列出本期实际匹配到的页面、命中的规则和是否纳入周报；若本期没有匹配页面，会明确显示该状态而不会导致导出失败。
- `Information Theme Summary`：按 Target、基因治疗、诊断、Biologics 和综合信息等主题汇总信息页。
- `Information Content Summary`：按 Target 资料、科研知识、在线工具、方案应用、案例、FAQ、新闻和公司支持等内容类型汇总。
- `Information Page Mapping`：列出每个信息页命中的模板或 slug 规则，以及是否使用兜底分类。

所有 GA4 请求都使用注册站点的 `domains` 对 `hostName` 做精确过滤，因此共享 GA4 属性中的其他站点不会进入总量、每日或页面明细。页面类型使用固定只读字段 `urltable.url/pageid/dbname` 与 `pages.pageid/template`：`dbname=pages` 时继续严格依据模板是否包含 `sideba`；已审核的动态 `dbname` 按官网运行时渲染器分类；`/g/`、文章及搜索/表单技术路径使用版本化的固定路径规则。未审核的数据源、冲突路由和孤立记录保持 `invalid_broken`，未找到任何证据的路径保持 `unknown_unmapped`。

只有确认为 `product_page` 的页面才能进入产品分类。`config/product_mappings/genemedi-net.yaml` 采用“具体规则优先、通用 GMP 其次、其他产品最后”的顺序，并保留 AAV 纯化、AAV 滴度和 Payload 补充分组；TARGET AB 明确排除 `-conjugate`。`config/information_mappings/genemedi-net.yaml` 只作用于 `information_page`：模板优先判定业务主题，slug 判定内容类型并补充主题，无法细分时保留在“综合信息 / 一般信息”，不会重新猜测页面是产品页还是信息页。

在已获数据库只读网络授权的主机上，可运行 `python scripts/audit_page_mappings.py` 检查全站覆盖率。该命令固定使用注册的页面维表适配器，只输出分类数量与模板汇总，不输出页面 URL、凭据或询盘内容。

### Excel test runtime contract

Renderer integration tests discover Node.js from `WEBSITE_ANALYTICS_NODE` first, then `node on PATH`. They skip only when neither executable is available or the local `@oai/artifact-tool` runtime has not been linked. On a fresh clone, run **load workspace dependencies**, set `WEBSITE_ANALYTICS_NODE` when Node is not on PATH, then run `scripts/setup-artifact-tool-runtime.ps1` with the Node modules path returned by the dependency loader. The project never guesses a runtime path, installs packages, or relies on a global Artifact Tool installation.

## Configured inquiry database source

`genemedi-net` can optionally collect actual form records from the approved legacy
`contacts` table. This is a third, separate source: it is not a GA4 key event
and it is not a GSC metric. The CLI queries only fixed, aggregate statements;
it never accepts SQL, a table name, or database credentials from the command
line.

Add this local-only block to the registered site in `config/sites.yaml`:

```yaml
inquiry_source:
  kind: legacy_contacts_mysql
  credential_env: WEBSITE_ANALYTICS_GENEMEDI_NET_INQUIRY_DSN
  credential_target: WebsiteAnalytics/genemedi-net/inquiry-dsn
```

The adapter first checks the referenced local environment variable, then the
optional Windows Credential Manager `credential_target`. The stored secret must
be a local MySQL DSN such as
`mysql+pymysql://<url-encoded-user>:<url-encoded-password>@<host>:<port>/<database>`.
Never place its value in YAML, a command history, an export, or Git. The target
name is metadata, not a secret, and must start with `WebsiteAnalytics/`. Use a
dedicated database account with only the minimum `SELECT` permission needed for
`contacts.submission_date`, `contacts.email_sent_to`, and `contacts.PageURL`;
the existing website application account is suitable only for a temporary
connectivity check, not ongoing analytics use.

The database output contains only daily and page-level aggregates:

- `storedSubmissions`: all records stored by the legacy form for the interval.
- `quarantinedSubmissions`: records with `email_sent_to = 'SPAM_QUARANTINE'`.
- `nonQuarantinedSubmissions`: stored records not matched by that legacy rule.

`nonQuarantinedSubmissions` is not a manual qualification or sales acceptance
metric. It must remain separate from GA4 key events and from GSC traffic. The
database day boundary uses the legacy website server calendar, so it can differ
from both GA4 and GSC. The report removes query strings and fragments from
legacy `PageURL` values before caching or exporting them. Page details are
capped at 50,000 rows; an extra row marks that detail as `partial` instead of
claiming full coverage.

### Google credential renewal

Use a local OAuth client created in your Google Cloud project, or an approved
service-account impersonation path. Do not rely on the default `gcloud` client
for Analytics scope renewal, and do not commit the OAuth client JSON. With a
local, restricted OAuth client JSON, the interactive renewal command is:

```powershell
gcloud auth application-default login --client-id-file '<local OAuth client JSON path>' --scopes 'https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/analytics.readonly,https://www.googleapis.com/auth/webmasters.readonly'
```

After the browser confirmation, run `validate-config` and a normal read-only
`fetch` command. The signed-in Google identity must have read access to the
registered GA4 property and GSC property.

## 可扩展时间模型与飞书同步

飞书看板不再把数据模型限制为固定周报。`website_analytics.periods`支持日、周、月、季度、年度、滚动窗口和自定义日期范围；每一个范围都必须使用对应的API区间汇总，并生成包含统计类型的稳定周期键。

默认物化范围配置在`config/sync_profiles/genemedi-net.json`。以下命令只生成同步计划，不调用GA4、GSC、询盘数据库或飞书：

```powershell
.\.venv\Scripts\python.exe scripts\build_analytics_sync_plan.py `
  --profile config\sync_profiles\genemedi-net.json `
  --anchor 2026-08-19 `
  --output audits\sync-plan\genemedi-net-2026-08-19.json
```

同步计划分别记录GA4、GSC和询盘的数据截至日期，并将周期标记为`complete`、`preliminary`或`partial`。延迟或失败的来源必须保持为空并等待回补，不能写成0。完整设计见`docs/analytics-time-model.md`。

## 飞书 V3 每日运营看板

V3 已启用“时间 × 产品”每日模型，并保留 V2 周数据作为回退：

- `全站每日数据`：28 条全站日记录。
- `产品每日数据`：616 条产品末级分类日记录。
- `信息页每日数据`：1,764 条信息主题与内容类型日记录。
- `负责人驾驶舱 V3`、`产品表现 V3`、`内容与 SEO V3`：共 22 个组件，其中 19 个数据图表。

三个看板顶部均有`数据日期`筛选，默认是`过去 30 天内`。点击`筛选`可以改成过去 7 天、本月、上月、单日；任意起止日期可组合日期条件。图表内置`数据状态 = 完整`，只显示三个来源均已最终化的日记录。

V3 的表创建、断点续传写入和回读验收命令分别是：

```powershell
.\.venv\Scripts\python.exe scripts\apply_feishu_dashboard_v3_tables.py --base-token '<base token>' --contract config\feishu_dashboard\v3\data_contract.json --backfill '<audited backfill JSON>' --target config\feishu_dashboard\v3\sync_target.json
.\.venv\Scripts\python.exe scripts\apply_feishu_dashboard_v3_records.py --contract config\feishu_dashboard\v3\data_contract.json --backfill '<audited backfill JSON>' --target config\feishu_dashboard\v3\sync_target.json
.\.venv\Scripts\python.exe scripts\verify_feishu_dashboard_v3.py --contract config\feishu_dashboard\v3\data_contract.json --backfill '<audited backfill JSON>' --target config\feishu_dashboard\v3\sync_target.json
```

以上命令默认都是只读或 dry-run；只有显式增加`--apply`才会创建表或补写缺失记录。记录同步以稳定键去重，不会覆盖 V2，也不会删除飞书记录。自定义日期指标卡是每日可加指标的汇总；活跃用户、日点击率和平均排名等不可加指标不进入默认看板，需要时应重新调用对应来源的完整区间 API。

生产服务器的 `website-analytics-sync@intraday.service` 同时维护 V2 回退表和
V3 每日事实表。V3 只接收同步计划中 `isFinal=true` 的单日数据：每次先检查三张
表的稳定键，再新增缺失日期，并且只更新指标或映射真正发生变化的记录；仅刷新
时间变化不会产生飞书写入。历史记录不会被删除，任一来源失败或明细截断时也不会
把缺失值写成 0。每批写入完成后会重新读取三张表，并逐键核对本批指标。服务参数
中的 V3 契约与目标必须成对提供：

```text
--v3-contract config/feishu_dashboard/v3/data_contract.json
--v3-target config/feishu_dashboard/v3/sync_target.json
```

## 网页 URL 主数据映射

审核通过的 `genemedi-net` 页面维表已按“规范 URL × 页面类型 × 产品层级”发布到飞书。产品字段只允许出现在 `product_page`，信息页和异常 URL 的产品字段保持为空。由于飞书单表最多容纳 20,000 条记录，33,918 条 URL 被无重叠、无遗漏地拆为：

- `产品URL映射-TARMART`：16,237 条产品页。
- `产品URL映射-其他产品`：6,724 条产品页。
- `信息与异常URL映射`：10,937 条信息页和 20 条异常 URL，共 10,957 条。

映射基于 2026-08-24 的不可变官网页面快照，页面分类版本和产品映射版本都随每行写入。旧的 19,954 条不完整表保留并改名为`网页-产品分类映射（旧版错误且不完整）`，不得作为统计或运营筛选的数据源。

映射产物的构建、幂等同步和全字段回读验收命令如下：

```powershell
.\.venv\Scripts\python.exe scripts\build_feishu_page_product_mapping.py --site genemedi-net --page-dimension-snapshot outputs\feishu-v3-backfill\source\page_dimension.json --output outputs\feishu-v3-page-product-mapping\genemedi-net_2026-08-24.json
.\.venv\Scripts\python.exe scripts\apply_feishu_page_product_mapping.py --contract config\feishu_dashboard\v3\page_product_mapping_contract.json --data outputs\feishu-v3-page-product-mapping\genemedi-net_2026-08-24.json --daily-target config\feishu_dashboard\v3\sync_target.json --target config\feishu_dashboard\v3\page_product_mapping_target.json
.\.venv\Scripts\python.exe scripts\verify_feishu_page_product_mapping.py --contract config\feishu_dashboard\v3\page_product_mapping_contract.json --data outputs\feishu-v3-page-product-mapping\genemedi-net_2026-08-24.json --target config\feishu_dashboard\v3\page_product_mapping_target.json
```

同步命令默认只生成差异计划；仅显式增加`--apply`才会创建表或补写/更新记录。全字段验收会读取三张表的全部 21 列，并逐单元格与审核产物比较。
