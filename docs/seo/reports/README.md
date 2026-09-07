# N.E.K.O SEO / GEO 日报文件夹

本目录保存日报契约与可审计样本；生产日报不会直接提交到 Git。免费日报在维护者本机运行；需要刷新付费排名基线时，由维护者审查请求计划与预算后手动触发 `SEO GEO Daily Report`，生成 Markdown + JSON artifact，并在 Actions Summary 中展示 Markdown。

## 文件

- [`TEMPLATE.md`](./TEMPLATE.md)：完整生产日报结构与每个字段的可信度规则。
- [`SKILL-INTEGRATION.md`](./SKILL-INTEGRATION.md)：把 `gingiris-seo-geo-agent` 的四阶段、BOFU 优先、GEO 三件套和日/周/月节奏映射到 N.E.K.O 双站点的执行手册。
- [`2026-07-29-pr-validation.md`](./2026-07-29-pr-validation.md)：两个拟提交 PR 的完整测试证据、零费用请求计划，以及搜索频率/AIO 引用频率如何进入最终日报的验收说明。
- [`2026-07-28-integrated-skill-report.md`](./2026-07-28-integrated-skill-report.md)：按 SEO/GEO skill 四阶段模型生成的当前验收样本；它保留真实 DataForSEO 与技术探针结果，并把未在本机提供的 Google/IndexNow 凭证明确标成 `UNKNOWN` 或 `NOT_RUN`。
- [`2026-07-28-preflight.md`](./2026-07-28-preflight.md)：用真实 DataForSEO artifact 和线上技术探针生成的历史预检样本；文件头会注明当时缺少哪些凭证或数据源。
- `docs/seo/monitoring.config.json`：双站点、30 个排名查询、GSC、GA4、IndexNow、CTA 与负责人配置。

GA4 Data API 使用两个独立数字 Property ID：`.online` 为 `546216550`，`.cn` 为 `546978126`。`G-N4QZK4PHE3` 与 `G-2D1RSKSR72` 是前端 Measurement ID，不能代替数字 Property ID。

## 生产日报包含什么

1. `.cn` 8 个中文主页/功能词、`.online` 19 个英文词、`.online zh-CN` 3 个具体文档落地页词；全部使用 Google Organic depth 100，并请求 AIO。
2. `.cn` 与 `.online` 各自的 GSC 最新完整日、连续两个 7 日窗口、低 CTR 页面、新查询，以及 sitemap 的 submitted、indexed 与覆盖率。
3. 两个独立 GA4 Property 的 Organic / AI referral 会话、页面浏览与 `steam_cta_click`；Steam CTA 同时报告全站总数、Organic 子集和 AI 子集。`.online` 从具体文档页返回各语言主页的 `docs_home_click` 同样报告总数、Organic 子集和 AI 子集；同时报告昨日完整日、连续 7 日环比与 AI/全站会话占比。
4. 两站 IndexNow 最近一次真实提交 artifact，以及首页、robots、sitemap、AI crawler 访问、Bing 验证、IndexNow key、canonical、hreflang、Schema 与 GA4 Measurement ID 探针。
5. 每个关键词的指定落地页、真实命中 URL、AIO 状态和上一份同口径日报排名对比；`Δ上次 = 上次排名 - 本次排名`，正数表示提升。
6. DataForSEO 月搜索需求与两站 GSC 近 7 日日均曝光频率分开报告，并展示 GSC 日均频率对前 7 日变化；中国段采集 Volume，但 KD 明确为 `UNSUPPORTED`。
7. AIO 触发率、目标域全查询引用率、触发后引用率及同口径历史变化；GA4 AI referral 和人工平台抽查保持独立。
8. Top 10 同口径净变化、今日新进/跌出，以及两站 GSC sitemap 覆盖率在头条直接展示。
9. 最多 1–2 个由排名 11–20、低 CTR、落地页不一致或 AIO 引用缺口触发的动作，附 owner、证据与验收指标；P0/P1 阻塞存在时不混入 P2，数据完整后按站点与目标页去重；四类主规则均无候选时，才从真实 `#21–100` / `>100` 排名积压或 `#4–10` 冲 Top 3 中兜底，避免数据完整却空转。
10. CTA 追踪契约固定为 `steam_cta_click` 与 `.online` 的 `docs_home_click`；人工 AI 引用抽查没有逐条证据时固定为 `NOT_RUN`。
11. 每个数据源的 `COMPLETE / PARTIAL / FAILED / NOT_RUN / UNKNOWN / UNSUPPORTED` 状态和证据链接；未知数据绝不写成 0。
12. 生产门禁逐字段验证 8 + 19 + 3 个上海当日 depth-100 排名、Volume、AIO、频率汇总、GSC API 动态解析出的最新 finalized 日期与连续窗口、GA4 时间窗口、sitemap 覆盖、两个不同 GA4 Property、IndexNow 响应与 1–2 个真实动作；不是只看顶层 `status`。

详细部署、权限和故障处理见 [`docs/contributing/seo-geo-daily-monitoring.md`](/contributing/seo-geo-daily-monitoring)。
