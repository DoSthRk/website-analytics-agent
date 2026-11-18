# GeneMedi 网站分析：Windows / WorkBuddy 只读试点包

版本：2026-11-04，针对目前已部署的 `batch-service/v1` 六项案例查询。
**这是已保存聚合证据查询；每日／按需自动刷新尚未启用。Windows 真机验收尚未完成。**
首位试点为老板的 Windows＋WorkBuddy，包尚未向其发送，服务器尚无同事通道授权。
以后启用刷新时需更新本版本说明；不能仅凭查询服务live_sources=false判断后台调度状态。

## 包内有什么

`Query-Analytics.ps1` 为首选，不要求 Python、Google SDK 或完整项目。
`analytics_query.py` 是已有 Python3 的设备可选的标准库客户端。
`WorkBuddy-INSTRUCTIONS.md` 可作为 agent 的只读工具说明；不会创建新权限。
`ssh_config.example` 只是未配置模板。`ADMIN-APPROVAL.md` 由管理员处理，老板不执行服务器命令。
所有入口只调用127.0.0.1:8765，禁用代理和重定向，不接受任意URL、SQL、文件路径、命令、账号或令牌。

## 接通前要完成的事（当前未执行）

1. 管理员确认试点人员身份、设备、只读范围；按管理员清单批准一个独立受限隧道身份。
2. 老板本人或公司IT在该Windows设备上生成并保护专用SSH密钥。私钥始终留在本机；仅将`.pub`公钥和指纹经批准渠道交管理员核对。不要发送私钥、root密钥、密码或Google凭据。
3. 管理员确认实际SSH入口和服务器公钥指纹，登记经逐人批准的公钥。包中不猜测公网地址、不内置凭据。
4. 经确认后合并SSH模板到本人配置（不覆盖已有配置）；私钥路径应为本人专用密钥。
5. 先核对设备已有OpenSSH与执行脚本的公司策略。未安装或被策略禁止时交IT处理；不要关闭安全检查、改变全机策略或使用执行策略绕过参数。

## 授权完成后的操作

在一个PowerShell窗口执行并保持运行：

```powershell
ssh -N -T genemedi-analytics-read
```

第一次连接须核对管理员提供的服务器指纹；未知/变更指纹停止并核实，不能自动信任。
本机和服务器都用8765。若本机端口被占用，停止并请管理员调整方案；不能只改本机到18765，因为当前服务校验Host端口。

在另一个位于本包目录的PowerShell窗口，按公司允许的方式执行：

```powershell
.\Query-Analytics.ps1 -Operation health
.\Query-Analytics.ps1 -Operation list_cases
.\Query-Analytics.ps1 -Operation case_versions -CaseId purprox-seo-four-pages
.\Query-Analytics.ps1 -Operation case_observations -CaseId purprox-seo-four-pages
```

从`case_versions`或`case_observations`响应选择**实际返回的**evidence_id，再查询：

```powershell
.\Query-Analytics.ps1 -Operation case_trend -CaseId purprox-seo-four-pages -EvidenceId '<实际64位ID>'
```

`case_detail`同时返回页面选项。逐页查询再使用实际返回的page_key，不能拼造或将URL小写化。
所有返回都包在`server_result`中；`notice`说明此客户端的边界。
`partial`不等于调用失败，也不代表完整；注意分源日期、时区、抓取时间和缺失证据。

## WorkBuddy 使用

将本包放在本人可读的固定本机目录，让WorkBuddy读取`WorkBuddy-INSTRUCTIONS.md`，并只调用包内固定脚本。无需复制采集仓库或任何服务端配置。
不要让agent替代管理员开通访问、添加公钥或改变SSH设置。本包不安装后台自启；关闭SSH窗口会断开通道。

## 当前数据含义与问题处理

当前案例是9月初起持续修改、结束时间未确认的四页SEO观察；不能解释成单次干预的前后因果。
原始大写URL与小写变体独立。区间用户数由GA4集合查询去重，不相加页面UV。
询盘仅入库聚合数量，不等于有效线索；DATE日界线仍未知。GA4/GSC最终性也可能未知。

- 本地连接失败：检查经批准的通道是否仍在，不自动抓取新源数据。
- HTTP403：优先检查本地端口、Host和Origin；不放松服务安全设置。
- `partial`／旧日期：这是数据状态；报告来源和截至时间，不假称刚同步或补零。
- 任何异常只回报固定错误码和操作名，不发送完整终端、密钥或凭据。

真正Windows＋WorkBuddy验收应覆盖：首连、六项查询、断线重连、端口冲突、来源限制显示、停用公钥后无法再连。
本包只通过本地协议测试和静态检查；这些真机项目仍待授权后执行。
