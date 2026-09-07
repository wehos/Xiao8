# Hosted Plugin UI with TSX

> This guide is maintained in English and [Simplified Chinese](/zh-CN/plugins/hosted-ui). There is no Japanese mirror yet; Japanese readers should use this page as the current source.

If your plugin needs a visible UI in the Plugin Manager, start here.

Hosted UI is the recommended path for new plugin panels and guide pages. You keep the backend in Python, then describe the frontend as either:

- **Hosted TSX** for interactive panels.
- **Markdown** for simple read-only docs.

You do not need to build a separate frontend bundle. TSX is loaded from the plugin directory and compiled by the Plugin Manager at runtime.

## Current recommendation

Use Hosted UI when your plugin needs:

- a settings or management panel
- buttons that call plugin entries
- tables, forms, filters, and status cards
- a quickstart or guide page
- plugin-local i18n

Keep static UI only when you need a fully custom legacy page or you already have a standalone HTML/CSS/JS UI.

## Choose the right surface

| Need | Recommended mode |
|------|------------------|
| Interactive settings or management panel | Hosted TSX |
| Tool/server dashboard | Hosted TSX |
| Read-only guide or documentation | Markdown |
| Fully custom legacy page | Static UI |

Hosted TSX is the preferred mode for new interactive plugin UI. Static UI remains available for compatibility.

## Minimal example layout

```text
plugin/plugins/my_plugin/
  plugin.toml
  __init__.py
  ui/panel.tsx
  docs/quickstart.md
  i18n/en.json
  i18n/zh-CN.json
```

## 1. Declare surfaces in `plugin.toml`

```toml
# Default plugin metadata. Required for every plugin, not specific to Hosted UI.
[plugin]
id = "my_plugin"
name = "My Plugin"
description = "A plugin with a hosted UI"
version = "0.1.0"
entry = "plugin.plugins.my_plugin:MyPlugin"

# Recommended when UI text should be translated. Keep "en" as the baseline.
[plugin.i18n]
default_locale = "en"
locales_dir = "i18n"

# Hosted UI switch. Required only when this plugin exposes surfaces.
[plugin.ui]
enabled = true

# Interactive panel. Required when the plugin needs buttons, forms, or tables.
[[plugin.ui.panel]]
id = "main"
title = "My Plugin"
# Required: .tsx selects Hosted TSX mode.
entry = "ui/panel.tsx"
# Required when the panel reads Python state. Must match @ui.context(id=...).
context = "dashboard"
# Required for action buttons. Remove action:call for read-only panels.
# Add config:read if the panel needs props.config.
permissions = ["state:read", "action:call"]

# Optional guide page. Use Markdown when the page is just documentation.
[[plugin.ui.guide]]
id = "quickstart"
title = "Quickstart"
# Required: .md selects Markdown mode.
entry = "docs/quickstart.md"
permissions = ["state:read"]
```

### What these fields mean

| Field | Meaning |
|-------|---------|
| `panel` / `guide` / `docs` | Where the surface appears in the Plugin Manager |
| `id` | Surface identifier, unique within its kind |
| `title` | Display title |
| `entry` | File path relative to the plugin directory |
| `context` | Python `@ui.context(id=...)` provider used by this surface |
| `permissions` | Surface capabilities, such as `state:read`, `config:read`, and `action:call` |

Mode is inferred from the entry extension:

| Extension | Mode |
|-----------|------|
| `.tsx`, `.jsx` | `hosted-tsx` |
| `.md`, `.mdx` | `markdown` |
| `.html`, `.htm` | `static` |

## 2. Provide context and actions in Python

```python
from plugin.sdk.plugin import (
    NekoPluginBase,  # Default plugin base class.
    neko_plugin,     # Default decorator for plugin discovery.
    plugin_entry,    # Default backend entry and LLM-visible tool.
    ui,              # Hosted UI decorators: context and action.
    tr,              # Recommended: plugin-local i18n reference.
    Ok,              # Recommended result helper for successful entries.
)


# Required for a normal Python plugin.
@neko_plugin
class MyPlugin(NekoPluginBase):
    # Hosted UI: required when a surface needs props.state.
    # The id must match plugin.toml: context = "dashboard".
    @ui.context(id="dashboard")
    async def dashboard(self):
        # This object becomes props.state in the TSX panel.
        return {
            "items": [
                {"id": "demo", "status": "ready"},
            ],
        }

    # Hosted UI: expose this plugin entry to the current surface.
    # Recommended: use tr(...) so the same label can be translated in i18n/*.json.
    @ui.action(
        label=tr("actions.refresh.label", default="Refresh"),
        tone="primary",
        # Recommended for state-changing actions: refresh props.state after success.
        refresh_context=True,
    )
    # Required for a callable backend entry. Hosted UI calls this entry.
    @plugin_entry(
        id="refresh_item",
        name=tr("entries.refresh.name", default="Refresh Item"),
        description=tr("entries.refresh.description", default="Refresh an item."),
        # Recommended: schema drives forms, validation hints, and LLM tool metadata.
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
        # Optional: tells the LLM-facing layer which result fields matter.
        llm_result_fields=["message"],
    )
    async def refresh_item(self, item_id: str, **_):
        return Ok({"message": f"Refreshed {item_id}"})
```

This gives the UI two things:

- `@ui.context(id="dashboard")` returns the `props.state` payload.
- `@ui.action(...)` exposes a backend entry as a UI action.
- `@plugin_entry(...)` is still the callable backend entry and the LLM-visible tool metadata.
- `tr(...)` declares a plugin-local i18n key with an English default.
- `refresh_context=True` asks the hosted UI to refresh context after the action succeeds.

## 3. Build a TSX panel

```tsx
// Hosted UI only: import components, hooks, and types from @neko/plugin-ui.
// Do not import npm packages from a plugin TSX file.
import {
  Page,
  Card,
  Stack,
  Text,
  DataTable,
  ActionButton,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

// Recommended: type the Python context payload for safer TSX.
type Item = {
  id: string
  status: string
}

type State = {
  items?: Item[]
}

// Required: Hosted TSX must export a default function component.
export default function Panel(props: PluginSurfaceProps<State>) {
  // Provided by Hosted UI:
  // - t: plugin-local translator
  // - state: result from @ui.context(...)
  // - actions: entries exposed by @ui.action(...)
  const { t, state, actions } = props

  // Recommended: locate actions by id instead of hardcoding labels in TSX.
  const refresh = actions.find((action) => action.id === "refresh_item") as HostedAction | undefined

  return (
    <Page title={props.plugin.name} subtitle={t("panel.subtitle")}>
      <Card title={t("panel.items")}>
        <Stack>
          {/* Recommended UI Kit component for simple tabular state. */}
          <DataTable
            data={state.items || []}
            rowKey="id"
            columns={[
              { key: "id", label: t("fields.itemId") },
              { key: "status", label: t("fields.status") },
            ]}
          />

          {/* Recommended shortcut. It calls the entry and refreshes context when
              the action has refresh_context=true. */}
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

Hosted TSX is compiled online. Keep imports simple:

- Use `@neko/plugin-ui` for components, hooks, and types.
- Do not import npm packages from plugin TSX.
- Keep business logic in Python; use TSX for UI state and interaction.

## 4. Add plugin i18n files

`i18n/en.json`:

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

`i18n/zh-CN.json`:

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

Use the same keys from Python and TSX:

```python
# Python declarations: use tr(...) in decorators and schemas.
tr("actions.refresh.label", default="Refresh")

# Python runtime: useful for messages produced by plugin code.
self.i18n.t("messages.done", default="Done")
```

```tsx
// TSX runtime: use props.t(...) for visible UI text.
props.t("panel.subtitle")
props.t("item.count", { count: 3 })
```

Fallback order:

1. current locale
2. base locale, such as `zh` from `zh-CN`
3. plugin `default_locale`
4. the `default` argument or key name

Only Chinese locales fall back to `zh-CN`; non-Chinese locales do not leak Chinese text by default.

## 5. Add a Markdown guide if needed

For a read-only guide, use a Markdown file:

```toml
[[plugin.ui.guide]]
id = "quickstart"
title = "Quickstart"
# .md selects the simple Markdown renderer. No Python context is required
# unless this guide needs state from @ui.context(...).
entry = "docs/quickstart.md"
permissions = ["state:read"]
```

Supported Markdown features:

- headings
- paragraphs
- unordered lists
- blockquotes
- fenced code blocks
- inline code
- `http` / `https` links

Not supported:

- inline HTML
- scripts
- MDX components

## API quick reference: `PluginSurfaceProps`

| Prop | Type | Description |
|------|------|-------------|
| `plugin` | `Record<string, any>` | Plugin metadata |
| `host` | `{ origin?: string }` | Optional host-origin metadata |
| `surface` | `Record<string, any>` | Current surface metadata |
| `state` | generic `State` | Context state from Python |
| `stateSchema` | `JsonSchema \| null` | Optional schema for `state` |
| `actions` | `HostedAction[]` | Actions exposed by `@ui.action` |
| `entries` | `Record<string, any>[]` | Plugin entries |
| `config` | `{ schema, value, readonly }` | Read-only plugin config snapshot when `config:read` is allowed |
| `warnings` | `Array<{ path, code, message }>` | Surface warnings |
| `locale` | `string` | Current UI locale |
| `t` | `(key, params?) => string` | Plugin-local translator |
| `i18n` | `HostedI18n` | Locale, default locale, and plugin-local message catalogs |
| `api` | `HostedApi` | Action and refresh bridge |
| `useLocalState` | hook | iframe-local state persisted across context refresh |

## API quick reference: `HostedApi`

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

- `api.call()` calls a plugin entry exposed by `@ui.action`.
- `api.parseDocument()` extracts text and metadata from a PDF or DOCX selected by the user. The current surface must declare the exact `document:parse` permission in `plugin.toml`:

  ```toml
  permissions = ["state:read", "document:parse"]
  ```

  Call it directly from a real user event handler, such as a file input's `onChange`, a click, or a drop. The Hosted UI runtime marks synchronous calls from those handlers as user initiated; a call made after unrelated asynchronous work is rejected.
- `api.refresh()` fetches the latest context and re-renders the surface.
- If an action has `refresh_context=false`, it will not refresh automatically.

## UI Kit quick reference

### Layout

| Component | Purpose |
|-----------|---------|
| `Page` | page shell |
| `Card` | section card |
| `Section` | generic section |
| `Heading` | heading text |
| `Stack` | vertical layout |
| `Grid` | grid layout |
| `Text` | paragraph text |
| `Divider` | separator |

### Data display

| Component | Purpose |
|-----------|---------|
| `StatusBadge` | status label |
| `StatCard` | metric card |
| `KeyValue` | key-value rows |
| `DataTable` | table |
| `List` | list |
| `JsonView` | JSON preview |
| `CodeBlock` | code block |

### Forms and actions

| Component | Purpose |
|-----------|---------|
| `Field` | label/help/error wrapper |
| `Input` | text input |
| `Textarea` | multiline input |
| `Select` | select input |
| `Switch` | checkbox switch |
| `Form` | form wrapper |
| `ActionForm` | schema-driven action form |
| `ActionButton` | button that calls an exposed action |
| `RefreshButton` | button that calls `api.refresh()` |

### Feedback and dialogs

| Component | Purpose |
|-----------|---------|
| `Alert` | inline message |
| `InlineError` | error block |
| `EmptyState` | empty placeholder |
| `Modal` | modal dialog |
| `ConfirmDialog` | confirm dialog |
| `AsyncBlock` | async loading/error/data block |
| `Tip` | informational tip |
| `Warning` | warning tip |

The kit also includes layout, input, feedback, media, artifact, form, and
navigation components. Use `plugin/sdk/hosted-ui/index.d.ts` for the complete
export list and exact signatures.

## Hooks quick reference

| Hook | Use |
|------|-----|
| `useLocalState` | surface-local state that survives context refresh |
| `useAsync` | async data with loading/error/reload |
| `useForm` | form value helpers |
| `useToast` | toast notifications |
| `useConfirm` | promise-based confirm dialog |
| `useDebounce` | debounced derived value |
| `useDebouncedState` | state plus debounced state |
| `useI18n` | translator and current locale |
| `useElementSize`, `useScrollIntoView`, `useScrollToBottom`, `useClipboard` | element, scrolling, and clipboard helpers |
| `useState`, `useEffect`, `useLayoutEffect`, `useMemo`, `useCallback`, `useRef`, `useReducer` | basic hosted runtime hooks |

Example:

```tsx
// Recommended for extra data that is loaded after initial render.
const tools = useAsync(() => props.api.call("list_tools"), [])

if (tools.loading) return <Text>Loading...</Text>
if (tools.error) return <InlineError error={tools.error} />

return <DataTable data={tools.data?.tools || []} />
```

## Runtime limits

Hosted TSX is not full React. It intentionally supports a smaller runtime:

Supported:

- function components
- Fragment
- keyed children
- controlled inputs
- hooks listed above
- plugin-local i18n
- action bridge

Not supported:

- class components
- React Context
- portals
- Suspense or concurrent rendering
- server components
- npm package imports from plugin TSX
- `dangerouslySetInnerHTML`

`useLayoutEffect` currently behaves like `useEffect`; do not rely on pre-paint layout timing.

## Testing

Run the full hosted UI check:

```bash
# From the repository root: runs type checks, TSX checks, hosted tests,
# browser E2E, Python compile checks, and relevant pytest cases.
scripts/check-hosted-ui.sh
```

Useful subcommands:

```bash
# Frontend-only checks.
cd frontend/plugin-manager
npm run check-hosted-tsx -- plugin/plugins/my_plugin
npm run test:hosted
npm run test:hosted:e2e
```

`check-hosted-tsx` verifies TSX syntax and types. The hosted tests cover the runtime, iframe execution, i18n coverage, and MCP Adapter panel fixture.

## Complete example

See the MCP Adapter:

```text
plugin/plugins/mcp_adapter/
  __init__.py
  plugin.toml
  ui/panel.tsx
  docs/quickstart.tsx
  i18n/en.json
  i18n/zh-CN.json
```

It demonstrates:

- context state from Python
- exposed actions
- table and form UI
- batch JSON import
- toast and confirm dialog
- plugin-local i18n
- hosted TSX tests
