# SEO/GEO 日报与自动监控

`SEO GEO Daily Report` GitHub 工作流不再定时启动；它保留 PR 零费用验证和维护者手动付费基线。本地免费日报可按 `gingiris-seo-geo-agent` 的四阶段顺序运行：技术健康 → GSC/GA4 → 排名与 AI 引用 → P0/P1/P2 动作。模板见 [`seo/reports/TEMPLATE.md`](/seo/reports/TEMPLATE)，N.E.K.O 双站适配和完成定义见 [`seo/reports/SKILL-INTEGRATION.md`](/seo/reports/SKILL-INTEGRATION)。

## 手动付费基线实际执行什么

维护者明确选择 `paid` 后，工作流执行一次真实付费基线：

- `.online en / United States`：19 个英文文档与品类词，Google Organic depth 100，AIO 开启，同时读取搜索量与可用的 KD；
- `.cn zh-CN / China`：8 个产品主页/功能词，Google Ads Volume + Google Organic depth 100，AIO 开启，跳过不受支持的 Labs KD；
- `.online zh-CN / China`：3 个中文教程/功能词，Google Ads Volume + Google Organic depth 100，AIO 开启，跳过不受支持的 Labs KD；
- `.cn` 与 `.online` 各自读取 GSC、GA4 和线上技术 SEO；GA4 同时读取同域名全站会话，以计算可信的 AI 引流占比；
- GSC sitemap 读取 `contents[].submitted/indexed` 并计算覆盖率；DataForSEO 每个关键词保留真实命中 URL，日报在首页直接列出所有 Top 10 词名；
- `.cn` 当天已有完整源仓库 artifact 时直接复用；artifact 缺失、过期、不是当天或排名状态非 `COMPLETE` 时，统一工作流自动执行同配置的付费 fallback，不产生空白日报；
- 输出原始 DataForSEO 响应、每段执行状态、统一 JSON、统一 Markdown；
- 诊断 artifact `seo-geo-daily-report` 保留 30 天，并把 Markdown 写进 GitHub Actions Summary；只有 `main` 上通过完整付费门禁的运行才额外发布保留 90 天的 `seo-geo-daily-paid-baseline`，下一次排名/AIO 差值只读取该可信基线。

中国区不在 DataForSEO Labs 的 KD 地区列表中，因此两个 China 段的 KD 必须显示 `UNSUPPORTED`；Google Ads Volume 仍按 China `2156` 采集，这也不妨碍 SERP 排名、AIO、落地页匹配或 CTA 分析。

`本地 AI 助手`、`Live2D AI 助手`、`长期记忆 AI 助手` 会执行两次不同目标域名的真实查询：`.cn` 段检查产品主页能否获得排名，`.online zh-CN` 段检查具体教程页能否获得排名。两者不能互相替代，也不能把同一 SERP 结果重复计入同一站点分母；因此日报固定显示 `.cn 8`，另显示 `.online zh-CN 3`。

## 可信度契约

日报严格区分：

| 状态 | 含义 |
| --- | --- |
| `COMPLETE` | 请求已执行且证据完整；结果为 0 也是真实的 0 |
| `PARTIAL` | 已执行，但部分关键词或子数据源失败 |
| `FAILED` | 请求执行失败，有明确错误证据 |
| `NOT_RUN` | 本次没有执行，绝不能写成 0 |
| `UNKNOWN` | 没有凭证、artifact 或可验证证据 |
| `UNSUPPORTED` | 供应商不支持该口径，例如 China KD |

手动付费基线的生产门禁要求 **DataForSEO 排名/Volume + 两站 GSC + 两站 GA4 + 两站 IndexNow 执行证据 + 两站技术 SEO** 都不是缺失、失败或部分状态；同时逐字段校验固定 `8 + 19 + 3` 个 observed depth-100 排名、每段/每词采集时间确属上海时区当日日报、Volume 状态、AIO 布尔结果、搜索频率/引用频率汇总、费用、GSC 动态 finalized 最新日/两个 7 日窗口/sitemap 覆盖、两个不同的 GA4 数字 Property/昨日与两个 7 日窗口、IndexNow 的时间/URL 数/响应/artifact，以及首页、robots sitemap 声明、sitemap URL 数、Bing/IndexNow 文件、`lang`、canonical、hreflang、GA4 Measurement ID 和 AI crawler 策略。否则 workflow 会先保留完整诊断 artifact，再明确失败，而不是产生“顶层 complete、正文为空”的绿色日报。

## Repository 配置

创建以下非敏感 Actions Variables：

| Variable | 示例/要求 | 用途 |
| --- | --- | --- |
| `GA4_PROPERTY_ID` | `546216550` | `.online` 的数字 GA4 Property ID，不是 `G-` Measurement ID |
| `GA4_CN_PROPERTY_ID` | `546978126` | `.cn` 独立 GA4 Property 的数字 ID（已从 GA4 属性列表核验）；不得复用 `.online`，也不得填写 `G-2D1RSKSR72` |
| `GSC_SITE_URL` | `https://project-neko.online/` | `.online` 已验证的 URL-prefix property |
| `GSC_CN_SITE_URL` | `sc-domain:project-neko.cn` | `.cn` 已验证的 Domain property |

保存以下 Actions Secrets：

| Secret | 用途 |
| --- | --- |
| `DATAFORSEO_LOGIN` | DataForSEO API Basic Auth 登录名 |
| `DATAFORSEO_PASSWORD` | DataForSEO API 密码 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 同时拥有两站 GSC 只读权限、两个 GA4 Viewer 权限的完整服务账号 JSON |
| `SEO_REPORTS_TOKEN` | 只授予私有仓库 `Project-N-E-K-O/N.E.K.O.OfficialWebsite` **Actions: Read** 的 fine-grained token，用于读取 `.cn` 排名与 IndexNow artifact |

禁止把上述凭证提交到 Git、Markdown、`docs/public` 或任何 `VITE_*` 变量。Measurement ID 可以公开，API 密码和服务账号私钥不可以。

## Google 一次性授权

1. 在 Google Cloud 项目启用 **Google Search Console API** 与 **Google Analytics Data API**。
2. 使用专用服务账号；把完整 JSON Key 保存为 `GOOGLE_SERVICE_ACCOUNT_JSON`。
3. 在 GSC 的 `https://project-neko.online/` 与 `sc-domain:project-neko.cn` 中，把服务账号添加为 Full user。
4. 在 `.online` 和 `.cn` 两个独立 GA4 Property 的 Property access management 中，把服务账号添加为 Viewer。
5. 分别把两个数字 Property ID 写入 `GA4_PROPERTY_ID` 与 `GA4_CN_PROPERTY_ID`。

CLI 会拒绝两个站点使用同一个数字 GA4 Property ID，以避免把 `.online` 流量误报成 `.cn`。

## 时间窗口

- GSC 最新完整日：先按 Google Search Console 的 `America/Los_Angeles` 日期查询 `dataState=all + dimensions=date`，读取 API `metadata.first_incomplete_date`，再把其前一天作为真实最新完整日；如果所查范围没有未完整日期，则使用探测范围末日。随后比较以该日结尾的连续两个 7 日窗口。sitemap 覆盖率为 GSC API 返回的 `indexed / submitted`，不是公网 sitemap 的 URL 数；API 未返回内容时保持 `N/A`。生产门禁同时检查这份完整性元数据，并要求完整日距上海日报日为 1–4 天（Google 通常延迟 2–3 天，另留 1 天覆盖上海/PT 日期边界）；更旧的数据视为陈旧证据，不能报绿。
- GA4 最新完整日：昨日；另比较连续两个 7 日窗口。Steam CTA 同时报全站总数、Organic Search 子集和 AI referral 子集，不能把任一子集标成总数。`.online` 在用户同意 Analytics 后，以 `docs_home_click` 记录从具体内容页点击 `/`、`/zh-CN/` 或 `/ja/` 主页，并同样拆成总数、Organic 与 AI；`.cn` 没有“文档→主页”这一步，因此该格为 `N/A`，不能写成 0。AI 来源会话除以同域名全部会话得到 `AI/全站会话`，不能把 AI referral 除以 Organic 后称为“自然流量占比”。
- 排名：本次 depth-100 artifact；工作流只下载 `main` 上最新、未过期且已通过完整付费门禁的 `seo-geo-daily-paid-baseline`。只有 segment、地区、语言、设备、depth 与关键词相同时才计算 `Δ上次 = 上次排名 - 本次排名`，正数代表提升；dry-run、失败运行、分支运行或没有同口径证据时保持 `N/A`。
- 技术 SEO：运行时直接检查首页、robots、sitemap、Bing 验证文件、IndexNow key、canonical、hreflang、Schema 与 GA4 Measurement ID；robots 另验证 GPTBot、OAI-SearchBot、ChatGPT-User、ClaudeBot、PerplexityBot 没有被根路径规则阻断。

## 本地验证

```bash
cd docs
npm test

# 三段零费用计划验证
npm run seo:dataforseo -- --config seo/dataforseo.config.json --mode all --depth 100 --include-ai-overview --dry-run
npm run seo:dataforseo -- --config seo/dataforseo.cn.config.json --mode all --skip-keyword-difficulty --depth 100 --include-ai-overview --dry-run
npm run seo:dataforseo -- --config seo/dataforseo.online-zh.config.json --mode all --skip-keyword-difficulty --depth 100 --include-ai-overview --dry-run
```

真实日报由 workflow 生成。需要离线复算时，使用重复的 `--dataforseo SEGMENT=PATH` 与 `--dataforseo-status SEGMENT=PATH` 参数，再提供 Google 凭证环境变量。生成后用：

```bash
node scripts/seo-monitoring/assert-report.mjs --input .seo-reports/seo-monitoring-RUN_ID.json --level daily
```

## 如何读“今日动作”

自动动作只来自真实证据，并且每天最多优先执行 1–2 项：

1. P0 技术阻断：修抓取、canonical、discovery 文件或 AI crawler 阻断；
2. P1 数据不完整：修 DataForSEO、GSC、GA4、IndexNow 权限、配置或 artifact，不制造伪增长任务；
3. P2 排名 11–20：补足目标页查询覆盖，并从相关高权重页面增加 2–3 条描述性内链；只在缺失直接答案时补 FAQ；
4. P2 高曝光低 CTR：按数字、年份、括号、社证、50–60 字符五项审计，只改 title/description/snippet，不凭感觉重写正文；
5. P2 AIO 触发但未引用：增加一句话直接答案、Key Stats、5–8 条 FAQ、最后更新日期和可验证来源；
6. P2 排名 URL 与指定落地页不一致：修正内链与 canonical/页面定位。
7. P2 排名兜底：仅当 3–6 的四类主规则全部没有候选时，才用真实 `#21–100` / `>100` 排名生成“先到 Top 20 / Top 100”，或用 `#4–10` 生成“冲 Top 3”动作；未执行/失败排名绝不能兜底。

选择顺序为 P0/P1 优先于 P2；只要存在数据或技术阻塞，本次队列就不混入 P2。数据完整后，同优先级按 BOFU → MOFU → TOFU，再按机会量排序，并按 `站点 + 目标页` 去重，避免把同一页面的排名、CTR 与 AIO 缺口拆成两项重复劳动。报告生成的动作是 `TODO`，不是“已经完成”；只有后续能关联 commit、PR、部署或页面证据时才可标记 `DONE`。

`steam_cta_click` 与 `docs_home_click` 必须由网站在用户同意 Analytics 后真实发送；日报只读取真实事件，不会从 page_view 推断转化。两种事件先查询同域名全量，再按 Organic Search 与 AI referral source 分开查询：总量回答“发生了多少”，子集回答“SEO/AI 带来了多少”，二者不能混写。

AI source regex 同时覆盖 ChatGPT/OpenAI、Perplexity、Claude/Anthropic、Copilot、Gemini/Bard、DeepSeek、Qwen、豆包、Poe 与 Bing AI referral。它只作为 GA4 `Session source` 的过滤条件，不写死为某个渠道组，也不会把搜索爬虫请求当成用户会话。

## 日 / 周 / 月执行节奏

- **Daily**：刷新三段排名、两站 GSC/GA4/IndexNow 与技术探针，生成报告，实施最多 1–2 个可验收 TODO。
- **Weekly**：盘点 GSC 新词、高曝光低 CTR、11–20 名页面、内链结构，并复盘 Organic / AI → docs→home → Steam CTA。
- **Monthly**：所有段重拉 Volume，只对支持地区重拉 KD；同时扩大 tracked 集、更新衰退页面、复盘 AIO 引用与高转化页面类型。China KD 始终保留 `UNSUPPORTED`，不可用其他地区数字冒充。

## 仍需单独保留的证据

IndexNow 没有可替代提交日志的公开统计 API。`.online` 部署工作流把每次提交时间、URL 数、HTTP 状态与失败原因保存成固定名 `indexnow-online-submission` artifact，并保留 90 天；日报按 artifact 名读取最新的未过期证据，包括失败状态，而不是只查“成功 workflow”。

`.cn` 私有仓库使用独立工作流，在服务器完成真实部署后由部署端触发 `repository_dispatch` 的 `production_deployed` 事件；该工作流提交已配置的生产 URL，并始终上传固定名 `indexnow-cn-submission` artifact，保留 90 天。统一日报通过 `SEO_REPORTS_TOKEN` 只读下载该 artifact。这个 token 不需要代码写入或仓库写权限，也绝不能提交到 Git。没有 artifact 或 token 时，日报必须显示 `NOT_RUN`，并在日报上传后触发生产门禁失败，不能写“提交 0 个”。
