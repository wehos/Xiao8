# 使用 Hosted TSX 构建插件界面

> 本指南同时维护[英文版](/plugins/hosted-ui)与简体中文版，暂无日文镜像；日文读者请以英文页为当前事实来源。

如果你的插件需要在插件管理器里显示一个界面，优先从这里开始。

Hosted UI 是新插件面板和教程页的推荐方式。后端仍然写在 Python 里，前端可以选择：

- **Hosted TSX**：用于交互式面板。
- **Markdown**：用于简单只读文档。

你不需要单独构建前端 bundle。TSX 文件放在插件目录里，由插件管理器运行时加载并编译。

## 当前建议

当插件需要这些能力时，建议使用 Hosted UI：

- 配置或管理面板
- 调用插件 entry 的按钮
- 表格、表单、过滤器、状态卡片
- quickstart 或 guide 页面
- 插件本地 i18n

只有在你需要完全自定义旧式页面，或已经有一套独立 HTML/CSS/JS 时，再考虑 Static UI。

## 选择合适的 surface

| 需求 | 推荐模式 |
|------|----------|
| 交互式配置面板 | Hosted TSX |
| 工具/服务器管理界面 | Hosted TSX |
| 只读教程或说明文档 | Markdown |
| 完全自定义旧版页面 | Static UI |

新插件的交互式 UI 推荐使用 Hosted TSX。Static UI 仍作为兼容路径保留。

## 最小示例结构

```text
plugin/plugins/my_plugin/
  plugin.toml
  __init__.py
  ui/panel.tsx
  docs/quickstart.md
  i18n/en.json
  i18n/zh-CN.json
```

## 1. 在 `plugin.toml` 声明界面

```toml
# 默认插件元数据。每个插件都需要，不是 Hosted UI 专属写法。
[plugin]
id = "my_plugin"
name = "My Plugin"
description = "A plugin with a hosted UI"
version = "0.1.0"
entry = "plugin.plugins.my_plugin:MyPlugin"

# 推荐：需要多语言时配置。建议以 "en" 作为基准语言。
[plugin.i18n]
default_locale = "en"
locales_dir = "i18n"

# Hosted UI 开关。只有插件要暴露界面时才需要。
[plugin.ui]
enabled = true

# 交互式面板。需要按钮、表单、表格时使用。
[[plugin.ui.panel]]
id = "main"
title = "My Plugin"
# 必需：.tsx 后缀会选择 Hosted TSX 模式。
entry = "ui/panel.tsx"
# 面板需要读取 Python 状态时必需。必须匹配 @ui.context(id=...)。
context = "dashboard"
# 需要调用 action 时必需。只读面板可以去掉 action:call。
# 面板需要读取 props.config 时再加 config:read。
permissions = ["state:read", "action:call"]

# 可选教程页。只是展示说明文档时，用 Markdown 最轻。
[[plugin.ui.guide]]
id = "quickstart"
title = "Quickstart"
# 必需：.md 后缀会选择 Markdown 模式。
entry = "docs/quickstart.md"
permissions = ["state:read"]
```

### 字段含义

| 字段 | 含义 |
|------|------|
| `panel` / `guide` / `docs` | 界面在插件管理器中的位置 |
| `id` | surface 标识，同一类型内唯一 |
| `title` | 显示标题 |
| `entry` | 相对插件目录的文件路径 |
| `context` | Python 侧 `@ui.context(id=...)` 的上下文 ID |
| `permissions` | surface 能力，例如 `state:read`、`config:read`、`action:call` |

模式会根据 `entry` 后缀自动推断：

| 后缀 | 模式 |
|------|------|
| `.tsx`, `.jsx` | `hosted-tsx` |
| `.md`, `.mdx` | `markdown` |
| `.html`, `.htm` | `static` |

## 2. Python 侧提供状态和动作

```python
from plugin.sdk.plugin import (
    NekoPluginBase,  # 默认插件基类。
    neko_plugin,     # 默认插件发现装饰器。
    plugin_entry,    # 默认后端 entry，也是 LLM 可见工具。
    ui,              # Hosted UI 装饰器：context 和 action。
    tr,              # 推荐：声明插件本地 i18n 引用。
    Ok,              # 推荐：成功结果辅助函数。
)


# 普通 Python 插件必需。
@neko_plugin
class MyPlugin(NekoPluginBase):
    # Hosted UI：surface 需要 props.state 时必需。
    # id 必须匹配 plugin.toml 里的 context = "dashboard"。
    @ui.context(id="dashboard")
    async def dashboard(self):
        # 这个对象会进入 TSX 面板的 props.state。
        return {
            "items": [
                {"id": "demo", "status": "ready"},
            ],
        }

    # Hosted UI：把这个插件 entry 暴露给当前 surface。
    # 推荐：用 tr(...)，这样 label 可以被 i18n/*.json 翻译。
    @ui.action(
        label=tr("actions.refresh.label", default="Refresh"),
        tone="primary",
        # 推荐：会修改状态的 action 成功后自动刷新 props.state。
        refresh_context=True,
    )
    # 可调用后端 entry 必需。Hosted UI 最终调用的是它。
    @plugin_entry(
        id="refresh_item",
        name=tr("entries.refresh.name", default="Refresh Item"),
        description=tr("entries.refresh.description", default="Refresh an item."),
        # 推荐：schema 会用于表单、参数提示和 LLM 工具元数据。
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": tr("fields.itemId", default="Item ID"),
                },
            },
            "required": ["item_id"],
        },
        # 可选：告诉 LLM 侧重点关注哪些返回字段。
        llm_result_fields=["message"],
    )
    async def refresh_item(self, item_id: str, **_):
        return Ok({"message": f"Refreshed {item_id}"})
```

这段代码给 UI 提供两类东西：

- `@ui.context(id="dashboard")` 的返回值会进入 `props.state`。
- `@ui.action(...)` 会把某个后端 entry 暴露为 UI 动作。
- `@plugin_entry(...)` 仍然是后端可调用入口，也是 LLM 可见工具元数据。
- `tr(...)` 声明插件本地 i18n key，并提供英文默认值。
- `refresh_context=True` 表示动作成功后自动刷新上下文。

## 3. 编写 TSX 面板

```tsx
// Hosted UI 专属：组件、hooks、类型都从 @neko/plugin-ui 导入。
// 插件 TSX 文件不要导入 npm 包。
import {
  Page,
  Card,
  Stack,
  Text,
  DataTable,
  ActionButton,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

// 推荐：给 Python context 返回值加类型，TSX 里更不容易写错。
type Item = {
  id: string
  status: string
}

type State = {
  items?: Item[]
}

// 必需：Hosted TSX 必须 default export 一个函数组件。
export default function Panel(props: PluginSurfaceProps<State>) {
  // Hosted UI 提供：
  // - t：插件本地翻译函数
  // - state：@ui.context(...) 的返回值
  // - actions：@ui.action(...) 暴露的动作
  const { t, state, actions } = props

  // 推荐：通过 action id 查找动作，不在 TSX 里硬编码展示文本。
  const refresh = actions.find((action) => action.id === "refresh_item") as HostedAction | undefined

  return (
    <Page title={props.plugin.name} subtitle={t("panel.subtitle")}>
      <Card title={t("panel.items")}>
        <Stack>
          {/* 推荐：简单表格优先使用 UI Kit 组件。 */}
          <DataTable
            data={state.items || []}
            rowKey="id"
            columns={[
              { key: "id", label: t("fields.itemId") },
              { key: "status", label: t("fields.status") },
            ]}
          />

          {/* 推荐快捷写法：它会调用 entry，并在 refresh_context=true 时刷新 context。 */}
          {refresh ? (
            <ActionButton action={refresh} values={{ item_id: "demo" }}>
              {t("actions.refresh.label")}
            </ActionButton>
          ) : (
            <Text>{t("panel.noActions")}</Text>
          )}
        </Stack>
      </Card>
    </Page>
  )
}
```

Hosted TSX 会在线编译。导入规则尽量保持简单：

- 从 `@neko/plugin-ui` 导入组件、hooks 和类型。
- 不要从插件 TSX 里导入 npm 包。
- 业务逻辑放在 Python，TSX 负责 UI 状态和交互。

## 4. 添加插件 i18n 文件

`i18n/en.json`：

```json
{
  "panel.subtitle": "Manage plugin items.",
  "panel.items": "Items",
  "panel.noActions": "No actions exposed.",
  "actions.refresh.label": "Refresh",
  "entries.refresh.name": "Refresh Item",
  "entries.refresh.description": "Refresh an item.",
  "fields.itemId": "Item ID",
  "fields.status": "Status"
}
```

`i18n/zh-CN.json`：

```json
{
  "panel.subtitle": "管理插件项目。",
  "panel.items": "项目",
  "panel.noActions": "没有暴露可用动作。",
  "actions.refresh.label": "刷新",
  "entries.refresh.name": "刷新项目",
  "entries.refresh.description": "刷新一个项目。",
  "fields.itemId": "项目 ID",
  "fields.status": "状态"
}
```

Python 和 TSX 共用同一套 key：

```python
# Python 声明侧：在装饰器和 schema 里用 tr(...)。
tr("actions.refresh.label", default="Refresh")

# Python 运行时：适合插件代码自己生成消息时使用。
self.i18n.t("messages.done", default="Done")
```

```tsx
// TSX 运行时：所有可见 UI 文本优先用 props.t(...)。
props.t("panel.subtitle")
props.t("item.count", { count: 3 })
```

fallback 顺序：

1. 当前 locale
2. 基础 locale，例如 `zh-CN` 的 `zh`
3. 插件 `default_locale`
4. `default` 参数或 key 名

只有中文 locale 会回退到 `zh-CN`。非中文 locale 不会默认漏出中文文本。

## 5. 如果需要，再添加 Markdown 教程页

只读文档可以使用 Markdown：

```toml
[[plugin.ui.guide]]
id = "quickstart"
title = "Quickstart"
# .md 会选择简单 Markdown 渲染器。除非教程页需要 Python 状态，
# 否则不需要额外声明 @ui.context(...)。
entry = "docs/quickstart.md"
permissions = ["state:read"]
```

支持：

- 标题
- 段落
- 无序列表
- 引用
- fenced code block
- inline code
- `http` / `https` 链接

不支持：

- inline HTML
- 脚本
- MDX 组件

## API 快速参考：`PluginSurfaceProps`

| Prop | 类型 | 说明 |
|------|------|------|
| `plugin` | `Record<string, any>` | 插件元数据 |
| `host` | `{ origin?: string }` | 可选的宿主 origin 元数据 |
| `surface` | `Record<string, any>` | 当前 surface 元数据 |
| `state` | 泛型 `State` | Python context 返回的状态 |
| `stateSchema` | `JsonSchema \| null` | 可选状态 schema |
| `actions` | `HostedAction[]` | `@ui.action` 暴露的动作 |
| `entries` | `Record<string, any>[]` | 插件入口列表 |
| `config` | `{ schema, value, readonly }` | 允许 `config:read` 时提供只读插件配置快照 |
| `warnings` | `Array<{ path, code, message }>` | UI 声明告警 |
| `locale` | `string` | 当前 UI locale |
| `t` | `(key, params?) => string` | 插件本地翻译函数 |
| `i18n` | `HostedI18n` | locale、默认 locale 与插件本地消息目录 |
| `api` | `HostedApi` | action/refresh bridge |
| `useLocalState` | hook | iframe 内本地状态，刷新 context 后仍保留 |

## API 快速参考：`HostedApi`

```ts
type HostedApi = {
  call(
    actionId: string,
    args?: Record<string, any>,
    options?: { timeoutMs?: number; signal?: AbortSignal; userInitiated?: boolean },
  ): Promise<any>
  parseDocument(
    file: File,
    options?: { timeoutMs?: number; signal?: AbortSignal },
  ): Promise<ParsedHostedDocument>
  refresh(): Promise<any>
}
```

- `api.call()` 调用当前 surface 暴露的插件 entry。
- `api.parseDocument()` 从用户选择的 PDF 或 DOCX 中提取文本和元数据。当前 surface 必须在 `plugin.toml` 中声明名称完全一致的 `document:parse` 权限：

  ```toml
  permissions = ["state:read", "document:parse"]
  ```

  请在真实用户操作的事件处理器中直接调用，例如文件输入框的 `onChange`、点击或拖放。Hosted UI 运行时会把这些处理器内的同步调用标记为用户发起；先执行无关异步工作、再调用会被拒绝。
- `api.refresh()` 重新拉取 context 并重新渲染。
- 如果 action 设置了 `refresh_context=false`，则不会自动刷新。

## UI Kit 快速参考

### 布局

| 组件 | 用途 |
|------|------|
| `Page` | 页面外壳 |
| `Card` | 卡片区块 |
| `Section` | 通用区块 |
| `Heading` | 标题 |
| `Stack` | 垂直布局 |
| `Grid` | 网格布局 |
| `Text` | 段落文本 |
| `Divider` | 分隔线 |

### 数据展示

| 组件 | 用途 |
|------|------|
| `StatusBadge` | 状态标签 |
| `StatCard` | 指标卡片 |
| `KeyValue` | 键值行 |
| `DataTable` | 表格 |
| `List` | 列表 |
| `JsonView` | JSON 预览 |
| `CodeBlock` | 代码块 |

### 表单和动作

| 组件 | 用途 |
|------|------|
| `Field` | label/help/error 包装 |
| `Input` | 单行输入 |
| `Textarea` | 多行输入 |
| `Select` | 下拉选择 |
| `Switch` | checkbox 开关 |
| `Form` | 表单包装 |
| `ActionForm` | 基于 schema 的 action 表单 |
| `ActionButton` | 调用 action 的按钮 |
| `RefreshButton` | 调用 `api.refresh()` 的按钮 |

### 反馈和弹层

| 组件 | 用途 |
|------|------|
| `Alert` | 行内消息 |
| `InlineError` | 错误块 |
| `EmptyState` | 空状态 |
| `Modal` | 弹窗 |
| `ConfirmDialog` | 确认弹窗 |
| `AsyncBlock` | 异步 loading/error/data 块 |
| `Tip` | 提示 |
| `Warning` | 警告 |

UI Kit 还包含布局、输入、反馈、媒体、产物、表单和导航组件。完整导出列表与精确签名以 `plugin/sdk/hosted-ui/index.d.ts` 为准。

## Hooks 快速参考

| Hook | 用途 |
|------|------|
| `useLocalState` | surface 本地状态，context refresh 后保留 |
| `useAsync` | 异步数据，带 loading/error/reload |
| `useForm` | 表单状态辅助 |
| `useToast` | toast 通知 |
| `useConfirm` | Promise 风格确认框 |
| `useDebounce` | 防抖派生值 |
| `useDebouncedState` | state + 防抖 state |
| `useI18n` | 翻译函数和当前 locale |
| `useElementSize`, `useScrollIntoView`, `useScrollToBottom`, `useClipboard` | 元素尺寸、滚动与剪贴板辅助 |
| `useState`, `useEffect`, `useLayoutEffect`, `useMemo`, `useCallback`, `useRef`, `useReducer` | 基础 runtime hooks |

示例：

```tsx
// 推荐：初次渲染后还要加载额外数据时使用。
const tools = useAsync(() => props.api.call("list_tools"), [])

if (tools.loading) return <Text>Loading...</Text>
if (tools.error) return <InlineError error={tools.error} />

return <DataTable data={tools.data?.tools || []} />
```

## Runtime 能力边界

Hosted TSX 不是完整 React。它有意提供一个较小的运行时。

支持：

- function component
- Fragment
- keyed children
- controlled input/select/textarea/checkbox
- 上面列出的 hooks
- 插件本地 i18n
- action bridge

不支持：

- class component
- React Context
- portal API
- Suspense / concurrent rendering
- server component
- 从插件 TSX 里导入 npm 包
- `dangerouslySetInnerHTML`

`useLayoutEffect` 当前等同于 `useEffect`，不要依赖 React 的 pre-paint layout timing 语义。

## 测试

运行完整 hosted UI 检查：

```bash
# 在仓库根目录运行：包含类型检查、TSX 检查、hosted 测试、
# 浏览器 E2E、Python 编译检查和相关 pytest。
scripts/check-hosted-ui.sh
```

常用单项：

```bash
# 只跑前端相关检查。
cd frontend/plugin-manager
npm run check-hosted-tsx -- plugin/plugins/my_plugin
npm run test:hosted
npm run test:hosted:e2e
```

`check-hosted-tsx` 检查 TSX 语法和类型。hosted 测试覆盖 runtime、iframe 执行、i18n 覆盖和 MCP Adapter 面板 fixture。

## 完整示例

参考 MCP Adapter：

```text
plugin/plugins/mcp_adapter/
  __init__.py
  plugin.toml
  ui/panel.tsx
  docs/quickstart.tsx
  i18n/en.json
  i18n/zh-CN.json
```

它展示了：

- Python context 状态
- 暴露 actions
- 表格和表单 UI
- 批量 JSON 导入
- toast 和 confirm dialog
- 插件本地 i18n
- hosted TSX 测试
