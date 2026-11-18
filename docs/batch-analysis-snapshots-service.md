# 第二阶段：固定证据与本地查询服务

本地开发阶段，未部署、未连接真实源或同事环境。沿用 `batch-analysis/v1` / `batch-metrics/v1`，
新增 `batch-snapshot/v1`、`batch-engine/2` 和 `batch-service/v1`。
不修改生产、Google 授权、timer、飞书、网络配置或旧看板；没有新增持久凭据。

## 自审修正

- 确认和分析入口重新解析契约，防止直接构造 dataclass 绕过范围校验。
- 账本读取及追加前验证截至所选版本的整个正文/摘要链，检测旧版本损坏。
  确认用 `BEGIN IMMEDIATE` 和主键约束；同一版本并发确认只有一个成功，其他明确冲突。
  重复确认不修改确认者或时间。确认摘要绑定规范化草稿，仍不是身份认证或管理员防篡改机制。
- 未核实日界线不能允许效果比较；完整截至日不得晚于抓取日期。
- 无效分母/分子、询盘隔离分区计数不一致会失败，不能伪造有效率或未知数。
- 保留源端区间 UV、host AND 页面 AND 可选 GA4 分群、GSC 截断、未知分类和站点级未知询盘语义。

## 快照模型与一致性

`analysis/snapshots.py` 的 `capture` 只读取每个来源/窗口/组一次，保存：

- 规范化查询（固定批次版本、窗口、页面、分群、对照开关）；确认后的批次文档和摘要。
- 三来源的**批准聚合字段**、状态、scope 摘要、抓取时间、日界线、覆盖/缺日、
  完整截至日及依据、截断、站点级无法关联询盘计数和受限原因码。
- 分类/产品模型版本，以及本查询各页面的有效分类、产品线结果。只保存这些有效模型输入，
  不复制整个 CMS 路由库、模板/解释原文或产品规则文件。
- 固定评估时间、引擎/指标版本、预期结果摘要。没有 HTTP 原始响应、客户行、查询词、
  请求头、DSN、令牌或任意外部响应字段；未知客户字段会被拒绝。

SQLite 中的快照 ID 是规范 JSON 的 SHA-256。每条快照不更新/删除，重复保存相同内容返回
相同 ID，并验证原内容未变；并发保存同内容只有一条。不同抓取/评估时间可以产生不同快照。
每份最多 512,000 UTF-8 字节，限制 6/12 个来源记录和已批准页面范围。先完整验证、重放，
再事务追加；坏输入或源抛出异常不会覆盖旧证据。来源明确的 error/partial 是可保存的证据，
重放仍为 partial，差值保持未知，而不是零或自动改用别的来源。

重放只用快照内的有效模型和聚合证据，不读取今天的配置、账本、源或时间；服务重启后结果
包含生成时间在内完全一致。遇到不支持的引擎/模型契约或摘要不符会拒绝，不套用新口径。
未来修改模型必须保留相应旧引擎或显式迁移；本阶段没有自动升级旧快照。
这是逻辑只追加存储和完整性检查，不是 WORM、签名或对抗本机管理员的审计系统。

## 可运行演示

在 worktree 根目录，用已有开发 Python。以下只写本地合成数据：

```sh
/Users/dosthrk/dev-project/company/website-analysis/.venv/bin/python scripts/demo_batch_analysis.py --snapshot --output-dir /tmp/website-batch-stage2
```

输出 `snapshot_id`，并生成本地合成账本、`snapshots.sqlite`、`synthetic-report.json`。
脚本只自动确认固定 `sample=true` 样例，不是用户真实批次确认，不会启动监听。

用同一解释器并设置 `PYTHONPATH` 后，可分别运行：

```sh
export PYTHONPATH="$PWD/src"
python -m website_analytics.analysis.service --snapshots /tmp/website-batch-stage2/snapshots.sqlite --port 8765
```

该进程**只绑定 127.0.0.1**，没有 host/bind 参数；Ctrl-C 结束。不安装后台服务、定时任务或
持久连接。若沙箱不允许监听，需按既有授权申请仅用于本地测试的执行权限，不能改绑公网绕过。

两个独立终端/agent 可用相同固定版本查询：

```sh
python -m website_analytics.analysis.client --snapshot-id <snapshot_id> --port 8765
```

或任何普通 HTTP 客户端向 `http://127.0.0.1:8765/v1/query` 发 POST，
`Content-Type: application/json`，正文严格为：

```json
{"contract":"batch-service/v1","operation":"replay","snapshot_id":"<64 lowercase hex characters>"}
```

响应含契约、snapshot_id、sample、status 和同一份 report。`partial` 是 HTTP 200 的业务状态，
自带客户端退出码 3；错误请求/快照为 400/404，临时本地存储故障为 503。
CLI 完整成功为 0，输入/连接错误为 2。另有 `GET /healthz`，不披露文件路径和来源凭据。

离线创建/重放也可通过已有 CLI，无需网络：

```sh
python -m website_analytics.analysis snapshot --request examples/batch-analysis/query.json --store /tmp/website-batch-stage2/synthetic-batches.sqlite --sample-evidence examples/batch-analysis/evidence.json --snapshots /tmp/website-batch-stage2/snapshots.sqlite
python -m website_analytics.analysis replay --snapshot-id <snapshot_id> --snapshots /tmp/website-batch-stage2/snapshots.sqlite
```

## 服务边界

- 服务只提供固定快照重放。HTTP 请求不能确认批次、抓取来源、写快照、改窗口、传文件路径、
  SQL、命令、任意 URL 或修改站点。存储路径只由启动服务的本地操作员指定。
- 请求体最多 4096 字节，拒绝重复 JSON 键/Content-Length、Transfer-Encoding、非 JSON、
  未知字段/路由/操作、路径穿越式 ID。没有文件服务、远程执行、源码上传或 root SSH 通道。
- 限定 loopback 对端和 Host，拒绝 Origin/跨站上下文；无 CORS，浏览器 preflight 不开放。
  客户端固定 loopback、禁用环境代理和重定向，不提供任意服务 URL 参数。
- 不记录原始请求、错误响应内容或凭据；错误消息固定。不把 snapshot_id 当成授权令牌。
- 这是同一受信本机环境的开发入口，**不是多用户安全隔离**。未开放内网/公网，未声称同事
  WorkBuddy/Codex 已接通。将来跨机访问必须先确认主机与最小访问边界，不能直接改成 0.0.0.0。

## 验收与未完成项

- [x] 确认合成批次→抓取聚合证据一次→不可变快照→固定重放。
- [x] 两种 HTTP 客户端及独立进程使用同契约结果一致；重启后相同；读接口不改 SQLite 文件。
- [x] 并发重复、历史损坏、失败、截断、缺日、未知页面、错误请求、外域及越权路径回归。
- [x] 临时 listener 测试结束关闭；没有扩展网络设置或服务常驻。
- [ ] 用户确认第一批真实页面/时间和目标访客条件；现阶段没有业务默认。
- [ ] 真实只读来源连接、独立完整性/采样证据、GSC 最新可用状态、CMS/URL 历史对账。
- [ ] 真实运行下的快照留存/备份/容量策略、源失败分类和运行监控。
- [ ] 同事跨机连接的访问方案、Windows 真机、生产并行验证/部署/回退和稳定 Google 认证。

测试：普通离线测试无需网络；`tests/test_analysis_service.py` 需要临时 loopback socket。
Excel 渲染跳过项仍来自原基线缺少本地渲染依赖。本阶段不将未跑的真实源/生产/Windows 检查写成通过。
