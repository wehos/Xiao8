# Plugin Config (plugin.toml)

Every plugin has a `plugin.toml` in its root folder. It tells N.E.K.O what the package is, which Python class the host must load, and which optional capabilities it exposes.

::: warning Two different kinds of entry
`[plugin].entry = "module.path:ClassName"` is a **host-loading entry point**. It imports one `NekoPluginBase` class when the plugin process starts. Runtime entry IDs such as `greet` come from `@plugin_entry(id="greet")` or `register_dynamic_entry(...)`; the Agent selects those IDs only after the plugin has loaded.
:::

Below is a complete config for a fictional "Smart Notes" plugin. This plugin can search and create notes, has its own UI panel, supports multiple languages, and can be called by the AI agent.

## Full example

```toml
[plugin]
id = "smart_notes"
name = "Smart Notes"
type = "plugin"
description = "Manage your notes: search, create, organize, with AI-powered classification."
short_description = "Note management with AI-powered organization."
keywords = ["note", "笔记", "memo", "record", "メモ"]
version = "1.2.0"
entry = "plugin.plugins.smart_notes:SmartNotesPlugin"

[plugin.author]
name = "Alice"

[plugin.sdk]
recommended = ">=0.1.0,<0.2.0"
supported = ">=0.1.0,<0.3.0"

[plugin.i18n]
default_locale = "zh-CN"
locales_dir = "i18n"

[plugin.store]
enabled = true

[plugin.ui]
enabled = true

[[plugin.ui.panel]]
id = "main"
title = "Smart Notes"
entry = "ui/panel.tsx"
context = "dashboard"
permissions = ["state:read", "action:call"]

[[plugin.ui.guide]]
id = "quickstart"
title = "User Guide"
entry = "docs/guide.md"
permissions = ["state:read"]

[plugin_runtime]
enabled = true
auto_start = true

[notes]
max_per_page = 20
auto_classify = true
```

## Section by section

### `[plugin]` — Who is this plugin

```toml
[plugin]
id = "smart_notes"
name = "Smart Notes"
version = "1.2.0"
entry = "plugin.plugins.smart_notes:SmartNotesPlugin"
```

These four fields are **required** by the supported check and release workflow. `id` must match `^[A-Za-z0-9_-]+$` and must be unique. Legacy source discovery can still load some incomplete manifests or folder/ID mismatches, but package build and production installation require the declared ID, archive directory, executable destination, and entry package to stay aligned; no suffixed executable copy is created. `entry` must use `module.path:ClassName` and resolve to a `NekoPluginBase` subclass; a `PluginRouter` cannot be launched directly.

For a normal plugin, `type = "plugin"` is optional because it is the default. Use `type = "adapter"` only for an Adapter package. The removed `extension` type and `[plugin.host]` table are rejected.

Keep `id` stable across releases. Upgrades, reinstalls, and downgrades replace executable code while preserving runtime `config`, `data`, and `cache`; changing `id` creates a different plugin identity. Optional `previous_ids` only prevents an old and new identity from being installed together. It is not a runtime alias and does not migrate or delete old data. Any replacement requires explicit user confirmation.

```toml
description = "Manage your notes: search, create, organize, with AI-powered classification."
short_description = "Note management with AI-powered organization."
keywords = ["note", "笔记", "memo", "record", "メモ"]
```

These fields affect Agent routing after the host has loaded the plugin:

- `description` — Full description used in plugin metadata and fine-grained Agent assessment.
- `short_description` — Compact description used by coarse screening. If absent, the Agent may generate and cache one from `description`.
- `keywords` — Regular-expression patterns. Matches are unioned into the Stage 1 candidate set; they do not bypass Stage 2 or guarantee execution.

Set `passive = true` for a listener/integration that must never participate in Agent dispatch. A non-passive plugin also needs at least one Agent-visible runtime entry before it can become a candidate.

The Agent's final Stage 2 decision returns a `plugin_id` and runtime `entry_id`. Both are checked against the candidates shown to that assessment; one corrective retry is allowed, then an invalid decision is rejected.

```toml
version = "1.2.0"
```

Required by the check and release workflow. Used for version management and marketplace publishing.

---

### `[plugin.author]` — Who wrote it

```toml
[plugin.author]
name = "Alice"
```

Optional. Shown in Plugin Manager.

---

### `[plugin.sdk]` — Which SDK version it's compatible with

```toml
[plugin.sdk]
recommended = ">=0.1.0,<0.2.0"
supported = ">=0.1.0,<0.3.0"
```

Tells the host which plugin SDK versions this package supports. Values use Python packaging specifier syntax.

- `supported` — Versions accepted without an untested exception
- `recommended` — Best-tested range; being outside it produces a warning
- `untested` — Additional allowed range that produces a warning
- `conflicts` — Explicitly rejected version ranges, even if another range matches

If `supported` is present, a host outside both `supported` and `untested` is not loaded. Invalid specifiers are also rejected.

---

### `[plugin_runtime]` — How it runs

```toml
[plugin_runtime]
enabled = true
auto_start = true
priority = 0
timeout = 10
startup_failure = "warn"
```

- `timeout` - Number of seconds to wait for startup readiness; must satisfy `0 < timeout <= 300`. Omit it to use the system default.
- `startup_failure` - What to do if `lifecycle.startup` raises after the process is alive. Omit it to default to `warn`: `warn` keeps the plugin running and marks startup as degraded, `fail` aborts startup, and `ignore` only logs the error.
- `enabled` — Set to `false` to temporarily disable without deleting files
- `auto_start` — When `true`, starts automatically with N.E.K.O; otherwise start manually from the panel
- `priority` — Optional integer runtime ordering hint

---

### `[plugin.i18n]` — Multi-language support

```toml
[plugin.i18n]
default_locale = "zh-CN"
locales_dir = "i18n"
```

If your plugin needs multiple languages, create an `i18n/` folder in your plugin directory with locale files:

```text
i18n/
├── en.json
└── zh-CN.json
```

Don't need i18n? Just don't include this section.

---

### `[plugin.store]` — Persistent storage

```toml
[plugin.store]
enabled = true
```

When enabled, you can use `self.store` in code to save and retrieve data (key-value pairs) that persists across restarts.

Don't need storage? Just don't include this section (disabled by default).

---

### `[plugin.ui]` — Custom UI

```toml
[plugin.ui]
enabled = true

[[plugin.ui.panel]]
id = "main"
title = "Smart Notes"
entry = "ui/panel.tsx"
context = "dashboard"
permissions = ["state:read", "action:call"]

[[plugin.ui.guide]]
id = "quickstart"
title = "User Guide"
entry = "docs/guide.md"
permissions = ["state:read"]
```

If your plugin needs a custom interface in Plugin Manager:

- `panel` — Interactive panel (written in TSX, can have buttons, tables, forms)
- `guide` — Read-only documentation (written in Markdown)

The file extension determines rendering: `.tsx` = interactive panel, `.md` = documentation.

Don't need UI? Just don't include this section. See [Hosted UI](./hosted-ui) for details.

---

### Custom sections — Your business config

```toml
[notes]
max_per_page = 20
auto_classify = true
```

Additional top-level sections are preserved as business config. Read them in code:

```python
cfg = await self.config.dump()
notes_cfg = cfg.get("notes", {})
max_per_page = notes_cfg.get("max_per_page", 20)
```

You can define as many custom sections as you want, named however you like.

---

## Directory structure for this plugin

```text
plugin/plugins/smart_notes/
├── plugin.toml              ← the file above
├── __init__.py              ← plugin code
├── i18n/                    ← locale files (because [plugin.i18n] is configured)
│   ├── en.json
│   └── zh-CN.json
├── ui/                      ← interactive panel (because [[plugin.ui.panel]] is configured)
│   └── panel.tsx
├── docs/                    ← user guide (because [[plugin.ui.guide]] is configured)
│   └── guide.md
```

Writable state is separate from the source or installed executable directory:

```text
<user data root>/plugins/smart_notes/
├── config/plugin.toml      ← effective runtime configuration
├── data/                   ← self.data_path()
└── cache/                  ← self.cache_path()
```

Only `plugin.toml` and the importable Python module named by `[plugin].entry` are required. The module does not have to be `__init__.py`, although that is the common layout. Installed package code remains separate from this writable state.
