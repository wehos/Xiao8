# Image Generator · 图片生成器

`image_generator` 让 N.E.K.O 的猫娘从普通聊天中响应“画一张……”等请求，也可从
插件管理面板手动测试。插件只调用用户自行配置的图像生成服务，不附带额度或 API
密钥。当前支持两类 provider：**OpenAI-compatible Images API** 与
**阿里云百炼（DashScope）原生异步文生图**，通过面板“API 风味”选择
（`openai_compatible` 或 `dashscope_native`）。

## API 契约 — OpenAI-compatible provider

插件发送：

```http
POST <api_base_url>/images/generations
Authorization: Bearer <stored key>
Content-Type: application/json
```

请求体包含 `model`、`prompt`、`n = 1`，以及配置后适用的 `size`、`quality`、
`style`。面板中的“图片格式”对应 OpenAI 的 `output_format`
（`png` / `jpeg` / `webp`）。出于 SSRF 与隐私安全考虑，插件将响应接收策略
固定为 Base64：只接受 `data[0].b64_json`，不会读取或下载提供商返回的远程
`data[0].url`。对于 DALL·E 与一般兼容模型，请求会发送
`response_format = b64_json`；GPT Image 模型本身固定返回 Base64 且不支持该旧字段，
因此插件会省略字段但仍只接受 Base64。响应可包含可选的 `revised_prompt`。

不同提供商对模型、尺寸、质量、风格、`output_format` 和
`response_format = b64_json` 的支持不同。请把面板允许列表设置为提供商真实支持的值；
提供商忽略或拒绝字段时，以其文档为准。Base URL 应包含版本路径（例如
`https://api.example.com/v1`），但不要包含 `/images/generations`。
公网与内网服务必须使用 HTTPS；只有 `localhost`、`127.0.0.0/8` 或 `::1`
回环开发地址允许使用普通 HTTP。

兼容范围有意保持清晰：OpenAI-compatible 模式只支持 Bearer 密钥、固定追加
`/images/generations`、不带查询参数的 Base URL，以及 `auto` 或 `宽x高`
形式的尺寸。需要 `api-key` 请求头、`api-version` 查询参数（常见于部分 Azure
部署）或符号尺寸的服务不在本模式支持范围内。

## API 契约 — 阿里云百炼（DashScope 原生异步）

`dashscope_native` 模式使用百炼原生异步任务 API，**不**走上面的
`/images/generations` 同步契约（万相系列模型在 OpenAI 兼容模式下会返回
`model_not_supported`）：

```http
POST <api_base_url>/api/v1/services/aigc/text2image/image-synthesis
Authorization: Bearer <API key>
X-DashScope-Async: enable
Content-Type: application/json
```

- 请求体：`{"model": ..., "input": {"prompt": ...}, "parameters": {"size": ...}}`。
- 提交返回 `output.task_id`；插件随后轮询
  `GET <api_base_url>/api/v1/tasks/{task_id}`（同样 Bearer 认证）直到任务
  `SUCCEEDED` / `FAILED` / `CANCELED` 或超出面板总超时。
- 成功响应在 `output.results[*].url` 提供限时 OSS 下载链接；插件会对该 URL 做
  与 Base64 路径相同的大小限制与容器嗅探后下载保存。这是唯一允许的服务端二次
  请求，目标仅限任务响应内返回的结果 URL。
- Base URL 示例：`https://dashscope.aliyuncs.com`（北京）或
  `https://dashscope-intl.aliyuncs.com`（新加坡），不要包含
  `/api/v1/services/...` 之后的部分。尺寸用 `宽*高` 形式（如 `1024*1024`），
  具体可用值以所用万相模型文档为准。

## 安全与存储

- API 密钥只写入 `PluginStore` 的独立记录，不出现在 `plugin.toml`、日志、面板响应、
  生成历史或工具结果中。面板只显示是否已配置，不返回任何密钥片段。
- 面板从 `get_panel_state` 获取一个短时、一次性的 RSA 公钥信封；浏览器使用
  WebCrypto 生成临时 AES-256-GCM 密钥加密完整保存载荷（所有设置与可选 API
  密钥），再以 RSA-OAEP SHA-256 包装该 AES 密钥。`save_settings` 的 `/runs`
  参数只包含密文和信封编号，因此即使宿主记录任务参数，也没有明文设置或凭据。
  信封加解密使用 pycryptodomex（冻结运行时内置；cryptography 在 Steam 打包中
  只有扩展桩、缺纯 Python 源码而不可导入）。RSA 私钥只存在于插件进程内，
  对称密钥不会返回给面板。过期、重复使用或未知信封会被拒绝。
- 空密钥保存会保留旧值；只有面板中的“清除密钥”动作才会删除。
- 提供商错误正文不会回显或写入日志。提示词、修订提示和历史摘要有长度上限并会对
  密钥形态做脱敏。
- 提供商 URL 图片会直接拒绝，服务器不会为响应中的 URL 发起二次网络请求，从而
  消除 URL 下载路径的 DNS 重绑定竞态。
- Base64 图片先执行严格大小限制，再按 PNG / JPEG / WebP 容器格式做结构嗅探：
  魔数、容器分块、尺寸与像素上限、动画帧拒绝。Pillow 在 Steam 打包中同样缺
  纯 Python 源码不可导入，因此不再做整图重编码，原始字节在通过校验后原样保存。
  文件名由随机 ID 生成，不使用提示词或远端路径。
- 最近历史仅保存时间、模型、截断提示摘要、本地结果 URL 和状态。历史数量与本地
  文件缓存的数量/总字节数都受面板设置约束；缓存淘汰后，对外读取历史时会隐藏已
  失效的本地链接。

生成文件位于插件数据目录的可写静态 UI 副本中，通过
`/plugin/image_generator/ui/generated/...` 提供。管理面板只会预览同源且匹配该
路径的缓存图片，绝不会加载历史或结果中的任意远程 URL。源插件目录不会被运行时写入。

## 聊天显示

当前 N.E.K.O 的 blind chat passthrough 会把文本交给 Markdown 渲染器，但不会在该
路径渲染 URL image part。因此插件推送一条很小的 Markdown 图片与链接消息：

```markdown
### 图片已生成

![AI 生成图片](http://127.0.0.1:48916/plugin/image_generator/ui/generated/...)

[打开原图](http://127.0.0.1:48916/plugin/image_generator/ui/generated/...)
```

图片字节和 Base64 不会进入 `push_message` 或 LLM 上下文，远低于 256 KiB ZMQ
inline 上限。关闭“自动显示”时，工具结果仍返回 `display_markdown` 和明确的角色
指令作为回退。由于 SDK 的 `push_message` 不提供最终送达确认，工具也会要求猫娘在
回复中附上同一 Markdown；极少数情况下可能重复显示，但不会因消息队列丢弃而只剩
“成功”文字。默认绝对地址使用本机插件服务端口；远程部署应设置
`NEKO_PLUGIN_SERVER_ORIGIN`（或相应的 N.E.K.O 服务地址环境变量）为客户端可访问的
来源。

## 示例

- “画一张雨夜霓虹街头的猫娘插画，电影感构图。”
- “生成一张 1024x1024 的极简咖啡店 logo。”
- 在管理面板填写测试提示词并点击“立即测试生成”（此操作可能产生提供商费用）。

建议先使用面板保存配置，再执行测试生成。不要在提示词、截图或问题报告中粘贴真实
密钥。
