# 持续优化与周趋势（本地探索性预览）

新增 `batch-preview/v2` 连续干预模型，保留 `batch-preview/v1` 近似单次事件及此前
`batch-analysis/v1` 精确确认批次/不可变快照的读取行为。没有把旧确认记录或报告改写为新事实。

## 日期与解释

- start 使用 `approximate_month_part`：年/月、early/middle/late/unknown、未知时区。
  “9 月初”没有被偷偷换算为 9 月 1 日、某个时间或用户未给出的上下界。
- end 目前 unknown。此前约 10 月 13 日作为 `prior_completion_estimate` 保留，明确不是
  已确认完成日期，也不再充当唯一 SEO 干预日。
- 仅记录用户报告的 title → body → faq 顺序，阶段起止日期均 null，不能分别计算贡献。
- 草稿仍是 `draft_unconfirmed`。不存在虚构确认摘要、上线时刻或真实优化效果。
- 8 月完整周只是候选优化前基线，仍需确认当时页面存在且未修改。跨 8/9 月的周单列过渡；
  9 月及之后为持续修改观察期，不标记成“稳定优化后”。
- 旧 10 月 6–12 日与 14–20 日报告保留。新的解释记录引用旧报告摘要，明确两段都位于
  持续修改期间，不能作为未优化 vs 已优化比较。旧统计值和历史文件字节不变。

## 读取与聚合边界

`scripts/read_seo_preview.py` 继续复用既有连接/服务身份和适配器，以内存进程读取，
不部署持久远程服务或写服务器代码。它是负责人只读开发工具，不是同事共享的 root SSH 接口。

```sh
python scripts/read_seo_preview.py --draft <continuous-draft.json> --legacy-query-project <existing-readonly-query-project> --weekly-start 2026-08-03 --weekly-end 2026-10-25 --variant-path /i/purprox-aavfull-enrichment-kit --output <new-local-output.json>
```

最多 16 个连续周一至周日，保持提交路径原始大小写，变体单列。GA4 每周集合 UV 独立从源端
查询，不相加页面/周用户；每页周 UV 也是源端 landingPage 维度区间值。GA4/GSC 页面过滤在
源端执行，不拉取全站页面清单。询盘用原有固定日期聚合适配器，只输出指定路径的计数和
数据库级无法归页数，不读取客户姓名/邮箱/正文，也不把未知归给某批次或定义为有效销售线索。

周数据源请求状态并非全日期最终化证明：

- GA4 保留属性时区和客户端可见质量元数据；最终化未独立验证。
- GSC 周报告为 `final`，并另查这些路径的 `all` 新鲜度元数据。官方 discovery JSON 字段
  是 `metadata.firstIncompleteDate`，不是文档描述中的 snake_case；显式兼容两种形式，
  冲突或无效日期会拒绝。缺失该字段不能解释为全部完整。
- 可使用 `--availability-only` 对现有连续草稿做一次小范围元数据复核，不重复抓历史。
  原始记录保留，报告用单独摘要绑定复核证据。跨入已观察不完整日的周会标明未满足截止条件。
- GSC 页面日无行保留未知，不补零；API 明确返回的零计数与无行区分。截断保留状态。
- 询盘使用旧服务器日历，时区/专库隔离未独立核实时明确标记。没有客户质量判断。

报告为 `continuous-trend-report/v1`，保留每个原始 URL、周范围、来源状态、模型草稿与
证据摘要、候选基线/过渡/修改期标签及未知信息。搜索、普通访问、询盘分开展示，不把
多来源拼接成严格转化旅程，不推断标题/正文/FAQ 单独作用或 SEO 因果提升。

## 大小写迁移

开发方的菜单/301 建议只是第三方方案，不是执行授权或上线证据。本地分析没有修改 CMS、
菜单、redirect、canonical。HTTP 只读核验记录 `observed_at`，没有观察到变更时继续保留待迁移；
即使后续观察到 301，也不能把观察时间冒充实际切换时间。映射依据、有效日期和集合去重未
明确前，不合并两 URL 的用户数。旧分类的 casefold lookup key 不等于真实 URL 身份。

本阶段真实数据文件只在任务 validation 目录，不进入测试样例或用户原 checkout。
测试均为人工合成/假客户端；尚未完成真实变更日志、历史页面家族、阶段日期、跨机访问或生产发布。
