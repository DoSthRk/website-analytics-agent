# Website Analytics Agent

一个面向本地 Agent 的官网数据命令行工具：将已登记官网的 GA4 与 Google Search Console（GSC）数据拉取、对比、缓存审计并导出为 Excel。第一阶段只覆盖 **GA4 + GSC**，所有数据访问均为只读。

工具只接受配置文件中登记的网站，不提供任意 URL、任意 API 请求体、维度、SQL 或凭据参数。命令结果只会输出机器可读 JSON；输入和配置错误输出到标准错误。

## 安全边界

- GA4 使用 Analytics Data API 的只读范围；GSC 使用 Search Console 的只读范围。
- 不在命令行、配置文件或缓存中保存、显示或传递凭据。`config/sites.yaml`、缓存、审计和导出目录默认均被 Git 忽略。
- 实际访问数据时使用 Google Application Default Credentials（ADC），或由 Google 客户端库使用 `GOOGLE_APPLICATION_CREDENTIALS` 所指向的本机凭据文件。不要把凭据内容复制进本项目，也不要提交该文件。
- `validate-config` 完全离线，不会创建 Google 客户端；带 `--fixture-dir` 的数据命令也完全离线。
- GSC 明细查询受 API 行数上限约束，不能视为所有查询词的完整导出。

## 安装与站点配置

需要 Python 3.11+；Excel 导出还需要 Node.js 和项目附带的 Artifact Tool 运行环境。

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

## 无 Google 账号的离线演示

测试夹具会绕过所有 Google 客户端创建与网络调用：

```powershell
.\.venv\Scripts\python.exe -m website_analytics export-excel --site demo --start 2026-08-03 --end 2026-08-09 --config config\sites.example.yaml --fixture-dir tests\fixtures --output exports\fixture-demo.xlsx
```

这会生成固定顺序的 README、Executive Summary、GA4 Daily、GA4 Pages、GSC Daily、GSC Pages、GSC Queries 和 Audit 工作表。输出 JSON 会记录来源状态、日期范围、数据新鲜度和导出验证结果；不会输出原始 Google API JSON。
