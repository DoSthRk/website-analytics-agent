# Agent 查询已保存的真实四页案例

这是本机 loopback 开发入口，不是跨机或生产服务。它只读本地已导入证据；不连接 Google、
数据库、SSH 或飞书，不因提问抓最新数据。没有新增凭据、后台服务、UI 或复杂权限。

## 已导入的内容与状态

案例 `purprox-seo-four-pages` 有两个固定证据版本：

| kind | 含义 | 状态 |
|---|---|---|
| continuous_trend | 8 月候选基线至 10/25 的持续改动周趋势、逐页数据、来源质量 | provisional |
| approximate_window | 此前以约 10/13 为日历锚点的窗口详情；单次干预解释已废止 | provisional |

都不是已确认批次。8 月只是候选未改动基线；9 月初只有近似开始信息；标题→正文→FAQ
是用户回忆的顺序，没有阶段日期；结束时间仍未知。**10/6–12 已不是未优化基线，10/14–20
是否稳定完成未确认**，不能笼统断言两段都确定仍在编辑。每个版本的 `interpretation`
都返回这份修正及所替代报告/旧解释的摘要。旧文件没有改写。

四个提交 URL 的大小写保持独立。小写 AAVFull 变体单列 `separate_variant`，不算四页批次
的一部分。选择页面使用服务给出的 `page_key`，客户端不传文件路径或任意 URL。

## 启动现成真实案例

从开发 worktree 根目录，使用现有开发 Python 并设置 `PYTHONPATH=src`：

```sh
export PYTHONPATH="$PWD/src"
/Users/dosthrk/dev-project/company/website-analysis/.venv/bin/python -m website_analytics.analysis.service --cases /Users/dosthrk/Documents/Codex/2026-09-29/task/validation/unified-real-cases/cases.sqlite --port 8765
```

只绑定 `127.0.0.1`；Ctrl-C 停止，不安装常驻服务。同一个服务也可加 `--snapshots <existing.sqlite>`
兼容旧 `replay`。旧快照响应标为 synthetic 或 confirmed；本案例始终 provisional，不互相伪装。

## 同事/agent 的最小工作流

以下 `python` 指上述开发解释器；客户端只需同一只读契约，未来无需各自维护采集项目。

```sh
python -m website_analytics.analysis.client --operation list_cases
python -m website_analytics.analysis.client --operation case_versions --case-id purprox-seo-four-pages
python -m website_analytics.analysis.client --operation case_trend --case-id purprox-seo-four-pages --evidence-id <continuous_trend evidence_id>
python -m website_analytics.analysis.client --operation case_detail --case-id purprox-seo-four-pages --evidence-id <approximate_window evidence_id>
python -m website_analytics.analysis.client --operation page_contribution --case-id purprox-seo-four-pages --evidence-id <evidence_id> --page-key <page_key from pages>
```

案例数据请求必须明确固定 `evidence_id`，没有隐式“最新”或实时 fallback。所有客户端可改
`--port`，不能指定远端主机/URL。HTTP 仍为 `POST /v1/query`，严格 JSON：

```json
{"contract":"batch-service/v1","operation":"case_trend","case_id":"purprox-seo-four-pages","evidence_id":"<64 lowercase hex>"}
```

操作白名单：list_cases、case_versions、case_observations、case_detail、case_trend、page_contribution，以及旧 replay。
`case_trend` 只接受周趋势版本；窗口预览用 case_detail。逐页贡献是会话/搜索计数的描述性占比，
不是优化因果贡献；分母未对账、无数据或为零时不给占比。用户数不求页面/周合计，不给用户份额。
变体不使用四页批次分母。来源抓取时间、日界线、截止/缺日/截断、模型与报告摘要均可追溯。

可给现有 Codex/WorkBuddy agent 的最小指令：

> 使用本机 batch-service/v1 的只读客户端。先列案例和证据版本，选择
> purprox-seo-four-pages 的 continuous_trend 版本并固定 evidence_id；按搜索、普通访问、
> 询盘顺序解释已有周趋势。说明 provisional、来源截至日与完整性，不把 8 月候选基线
> 当已验证未改动期；10/6–12 已非未优化基线，10/14–20 稳定状态未知。需要下钻时先从
> pages 取得 page_key，再查描述性逐页贡献；不相加用户数，不合并大小写变体，不抓新源。

HTTP 200 / CLI 0 表示读请求完成，**不代表业务效果成立或所有来源完整**；必须检查
`evidence_state`、`interpretation`、`report/source_quality` 和每源状态。保留未知，不能把无行当零。

本地开发版可先用 `case_observations`（仅传 case_id）发现按窗口和来源登记分组的最新
provisional 指针，再固定 evidence_id 查询。`strict_complete` 是独立指针，缺证明时为空。
详情、趋势和逐页响应的 `scoped_completeness/v1` 将请求、覆盖、日界、成熟度、质量分开；
旧 `data_completeness` 保留兼容语义，不应被当作新的 provider 最终性证明。
参见 [完整性契约与边界](scoped-completeness.md)。未部署或接通同事客户端。

含 worker 聚合证据的本地开发版，在详情、趋势和逐页响应中统一增加
`source_assessment_contract: case-source-assessments/v1`、`source_window_assessments`
及 `data_completeness`。逐窗口评估保留来源日界、抓取时间、覆盖、complete-through、
截断和限制；`data_completeness.status: partial` 不会因 HTTP 200 或来源请求成功而升级。
`comparison_eligible` 仅判断捕获的来源窗口依据，不确认批次、稳定完成或因果效果。
查询层 `source_quality.ga4_property_timezones` 从捕获的 worker 日界投影，说明其 basis；
不会改写已固定的原报告或摘要。旧版无 worker 证据的案例保留响应形状，不补造完整性。
这些新增投影尚未部署到生产查询服务。

2026-11-04 隔离验收使用真实来源、原四页及独立小写变体、10/19–25 与 10/26–11/1：
三源读取、临时 provisional 案例组装、双客户端、重启、幂等和旧证据保留均已验证。
实际来源窗口仍为 partial，三源 complete-through 未验证，询盘日界未知；未发布生产证据，
未部署刷新服务或修改原同步。不能将本轮工程验收写成完整数据或业务效果验收。

## 离线导入与安全边界

导入只能由本地操作员显式执行，不是 HTTP 能力：

```sh
python scripts/import_saved_cases.py --preview-dir <saved seo-oct13-preview directory> --continuous-dir <saved seo-continuous-preview directory> --store <new or existing local cases.sqlite>
```

它只读取既有草稿、聚合响应、HTTP 元数据和解释记录，校验整个包后事务追加；不重抓来源。
每版最多 512 KB，固定来源/指标/维度/元数据字段，未知字段、客户信息、凭据式文本、外域/
越界日期/大小写范围变化会被拒绝。重放绑定引擎/模型及报告摘要；重复导入不覆盖，损坏不自动修复。
实际保存的是聚合证据，不是原始 HTTP 头、完整页面 HTML、客户记录、DSN 或 token。

服务请求不能导入、确认、抓取、写库或传 SQL/path/command/remoteURL。沿用 loopback、Host/
Origin、请求大小、禁止代理/重定向等限制；没有公开内网/公网。当前未验收同事跨机连接、
Windows 真机、生产部署和授权生命周期，不应把此入口直接改绑 0.0.0.0。
