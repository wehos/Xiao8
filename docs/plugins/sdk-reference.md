# Plugin Capabilities and SDK Reference

All plugin development APIs are imported from `plugin.sdk.plugin`.

```python
from plugin.sdk.plugin import (
    # Base
    NekoPluginBase, PluginMeta,
    # Decorators
    EntryKind, neko_plugin, plugin_entry, lifecycle, timer_interval, message,
    on_event, custom_event, hook, before_entry, after_entry, around_entry,
    replace_entry, quick_action, plugin, ui,
    # LLM tools and activity
    llm_tool, LlmToolMeta, OsActivitySnapshot, get_os_activity_snapshot,
    # Plugin-local i18n and settings
    PluginI18n, tr, PluginSettings, SettingsField,
    # Result types
    Ok, Err, Result, unwrap, unwrap_or,
    # Runtime helpers
    Plugins, PluginRouter, PluginConfig, PluginStore,
    SystemInfo,
    # Errors
    SdkError, TransportError,
    # Logging
    get_plugin_logger,
)
```

`plugin.sdk.plugin` is the supported developer import surface. The root `plugin.sdk` package intentionally exposes only a conservative shared subset; do not assume every plugin-only helper is re-exported there.

## NekoPluginBase

All plugins must inherit from `NekoPluginBase`.

```python
@neko_plugin
class MyPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
```

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `self.ctx` | `PluginContext` | The runtime context (injected by host) |
| `self.plugin_id` | `str` | This plugin's unique identifier |
| `self.plugin_dir` | `Path` | Installed plugin directory containing code, manifest, and static assets |
| `self.config_dir` | `Path` | Compatibility alias for `self.plugin_dir` |
| `self.storage_dir` | `Path` | User storage root assigned to this plugin |
| `self.runtime_config_path` | `Path` | External runtime configuration file |
| `self.metadata` | `dict` | Plugin metadata from `plugin.toml` |
| `self.bus` | `SdkBusContext` | Read/watch facade over host state; it has no publish/emit API |
| `self.plugins` | `Plugins` | Cross-plugin call helper |
| `self.system_info` | `SystemInfo` | Host system metadata |

### Everyday capabilities

`NekoPluginBase` provides logging and configuration without extra setup:

```python
self.logger.info("Processing request: {}", request_id)
timeout = await self.config.get_int("my_settings.timeout", default=30)
```

Logs appear in the Plugin Manager and are written to the host-managed log
directory. The host owns log-file location and rotation. Runtime configuration
updates use `await self.ctx.update_own_config(...)` or
`await self.config.update(...)`; after updating, refresh any derived state in
the same process.

Choose storage by data shape:

| Need | API |
| --- | --- |
| Small key-value state | `self.store` after enabling `[plugin.store]` |
| Structured SQLite data | `self.db` after enabling `[plugin.database]` |
| Arbitrary persistent files | `self.data_path(...)` |
| Rebuildable files | `self.cache_path(...)` |

```python
from plugin.sdk.plugin import unwrap

unwrap(await self.store.set("last_query", "weather"))

async with unwrap(await self.db.session()) as session:
    await session.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT)"
    )
    await session.commit()
```

For translated plugin text, configure `[plugin.i18n]`, add locale JSON files,
and use `self.i18n.t(...)`:

```python
message = self.i18n.t("greeting", name="Alice")
```

### Methods

#### `report_status(status: dict) -> None`

Report plugin status to the host process.

```python
self.report_status({
    "status": "processing",
    "progress": 50,
    "message": "Halfway done..."
})
```

#### `push_message(**kwargs) -> PushMessageResult`

Push a message to the host system with the v2 schema.

```python
result = self.push_message(
    source="my_feature",
    visibility=["chat"],       # [], ["chat"], ["hud"], or both
    ai_behavior="blind",       # "respond", "read", or "blind"
    parts=[{"type": "text", "text": "Task complete"}],
    priority=5,
)

if not result["submitted"]:
    # Keep local state; retry and deduplication remain plugin policy.
    self.logger.warning("message submission failed: %s", result["reason"])
```

`submitted=True` means only that the SDK's authoritative local submission path
accepted responsibility for the payload. It does not acknowledge host
consumption, model generation, or playback. Rejections use the stable reasons
`backpressure`, `transport_error`, or `transport_unavailable`; the result never
contains the message body or raw exception text. Rejected results also carry
`ok=False` for compatibility with legacy callers; new code should use
`submitted` as the authoritative discriminator.

The v1 fields (`message_type`, `content`, `delivery`, `reply`, and the other legacy aliases) are deprecated but still translated in current source. Migrate now; this documentation does not guarantee an exact removal release. See the [migration guide](./migration-v0.9#push-message-v2).

#### Media parts

- Text and image parts are supported. A small image can be sent inline:

  ```python
  parts=[{"type": "image", "data": image_bytes, "mime": "image/png"}]
  ```

- For a non-trivial image, upload it through the host and send the returned part instead:

  ```python
  image_part = await self.ctx.images.upload(image_bytes, mime="image/png")
  result = self.push_message(
      source="my_feature",
      visibility=["chat"],
      ai_behavior="read",
      parts=[image_part],
  )
  ```

  Arbitrary external image URLs are not accepted for model delivery; use this temporary host upload. Inline images share the message-plane payload budget, while the uploaded part keeps the image bytes out of that envelope. `ctx.images.upload()` is unavailable in lifecycle handlers because the plugin command loop cannot service the upload response there; call it from a plugin entry, timer, message, or custom event handler.
- `visibility` controls rendering in the user's chat or HUD; `ai_behavior` independently controls whether the model reads the message or responds.
- Audio and video message parts are not currently delivered by the host. Do not report them as successfully played or shown.
- A Hosted UI panel can render user-facing audio or video itself, but that does not deliver media through the native chat or model channel.

#### `data_path(*parts) -> Path`

Get a path under the plugin's `data/` directory.

```python
db_path = self.data_path("records.db")
# → <storage-root>/plugins/<plugin_id>/data/records.db
```

#### `cache_path(*parts) -> Path`

Get a path under the plugin's disposable cache directory.

```python
preview_path = self.cache_path("preview.png")
# → <storage-root>/plugins/<plugin_id>/cache/preview.png
```

#### `register_dynamic_entry(entry_id, handler, ...) -> bool`

Register an entry point at runtime (not via decorator).

```python
self.register_dynamic_entry(
    entry_id="dynamic_greet",
    handler=lambda name="World", **_: Ok({"msg": f"Hi {name}"}),
    name="Dynamic Greet",
    description="A dynamically registered greeting",
)
```

#### `unregister_dynamic_entry(entry_id) -> bool`

Remove a dynamically registered entry.

#### `list_entries(include_disabled=False) -> list[dict]`

List all entry points (static + dynamic).

#### `enable_entry(entry_id) / disable_entry(entry_id) -> bool`

Enable or disable a dynamic entry at runtime.

#### `register_static_ui(directory, *, index_file, cache_control) -> bool`

Register a static web UI directory for this plugin.

```python
self.register_static_ui("static")  # serves <plugin_dir>/static/index.html
```

#### `include_router(router, *, prefix) -> None`

Mount a `PluginRouter` to organize a large or feature-split normal plugin.

Related methods are `exclude_router(router_or_name) -> bool`, `get_router(name)`, and `list_routers()`. A Router cannot be the manifest `[plugin].entry`, and this mount path does not automatically invoke `on_mount` / `on_unmount`.

#### Hosted/static UI and list actions

Hosted TSX uses the exported `ui` namespace plus manifest surfaces; see [Hosted UI](./hosted-ui). Legacy static UI uses `register_static_ui(...)`. List-row actions are managed with `set_list_actions(...)`, `register_list_action(...)`, `clear_list_actions()`, and `get_list_actions()`.

#### LLM tool methods

`register_llm_tool(...)`, `unregister_llm_tool(name)`, and `list_llm_tools()` are the imperative counterparts to `@llm_tool`. They register conversation-time tools, not user-plugin Agent entries. See [LLM Tool Calling](./tool-calling).

#### `run_update(**kwargs) -> object` (async)

Send an update to the host during long-running operations.

#### `export_push(**kwargs) -> object` (async)

Push export data to the host.

#### `finish(**kwargs) -> Any` (async)

Signal task completion to the host.

### Reply Control

The `finish()` method accepts a `reply` parameter (default `True`) that controls whether the plugin result triggers the main character to speak.

```python
# Normal: character will announce the result
return await self.finish(data={"summary": "Done"}, reply=True)

# Silent: result is recorded but character stays quiet
return await self.finish(data={"summary": "Done"}, reply=False)
```

### LLM Result Field Filtering

Use `llm_result_fields` on `@plugin_entry` (static entries) or `register_dynamic_entry()` (dynamic entries) to control which fields of the result the main LLM can see. Fields not listed are excluded from the LLM prompt but still stored in the task registry.

```python
# Static entry
@plugin_entry(llm_result_fields=["summary"])
async def search(self, query: str):
    return await self.finish(data={"summary": "3 results", "raw_results": [...]})

# Dynamic entry
self.register_dynamic_entry(
    entry_id="my-tool",
    handler=handler,
    llm_result_fields=["summary"],
)
```

---

## Result Types: Ok / Err

The SDK uses Rust-inspired Result types for error handling instead of exceptions.

```python
from plugin.sdk.plugin import Ok, Err, unwrap, unwrap_or

# Returning success
return Ok({"data": result})

# Returning error
return Err(SdkError("something went wrong"))

# Consuming results
result = await self.plugins.call_entry("other:do_stuff")
if isinstance(result, Ok):
    data = result.value
else:
    error = result.error
    self.logger.error(f"Call failed: {error}")

# Helper functions
value = unwrap(result)           # raises if Err
value = unwrap_or(result, None)  # returns default if Err
```

---

## Plugins (Cross-Plugin Calls)

Access via `self.plugins`.

```python
# List all plugins
result = await self.plugins.list()

# List only enabled plugins
result = await self.plugins.list(enabled=True)

# Get plugin IDs
result = await self.plugins.list_ids()

# Check if a plugin exists
result = await self.plugins.exists("other_plugin")

# Call another plugin's entry point
result = await self.plugins.call_entry("other_plugin:do_work", {"key": "value"})

# Call and ensure JSON object response
result = await self.plugins.call_entry_json("other_plugin:get_data")

# Require a plugin to be present and enabled
result = await self.plugins.require_enabled("dependency_plugin")
```

All methods return `Result` types — check with `isinstance(result, Ok)` before using `.value`.

---

## PluginStore (Persistent Storage)

Access via `self.store` (the host pre-builds and injects it at plugin construction time — you do not instantiate `PluginStore` yourself).

All `PluginStore` methods return a `Result`; unwrap with `unwrap_or(...)`.

```python
unwrap_or(await self.store.set("key", {"count": 42}), None)
value = unwrap_or(await self.store.get("key"), None)  # → {"count": 42}
```

---

## SystemInfo

Access via `self.system_info`. These methods all return a `Result`; unwrap with `unwrap_or(...)`.

```python
config = unwrap_or(await self.system_info.get_system_config(), {})
settings = unwrap_or(await self.system_info.get_server_settings(), {})
python_env = unwrap_or(await self.system_info.get_python_env(), {})
```

---

## PluginContext (ctx)

The `ctx` object is injected by the host at construction time.

| Property | Type | Description |
|----------|------|-------------|
| `ctx.plugin_id` | `str` | Plugin identifier |
| `ctx.config_path` | `Path` | Path to `plugin.toml` |
| `ctx.logger` | `Logger` | Logger instance |
| `ctx.bus` | `SdkBusContext` | Read/watch facade over host state |
| `ctx.metadata` | `dict` | Plugin metadata |

### Bus and memory

Inside async entries, await `get()` before applying the local list operations:

```python
events = await self.bus.events.get(plugin_id=self.plugin_id, max_count=50)
recent = events.filter(priority_min=1).sort(by="timestamp", reverse=True).limit(20)

records = await self.bus.memory.get(bucket_id="default", limit=20)
```

The list surface is `filter` / `where`, `sort`, `limit`, and `watch`. Callable `filter(predicate)`, `where(predicate)`, and `sort(key=...)` are local-only; replayable watcher chains must use structured `filter(field=value, ...)` and `sort(by=...)`. Only `messages`, `events`, and `lifecycle` support `watch()`; `conversations`, `memory`, and `frames` are read-only snapshots. Watcher subscriptions accept only `add`, `del`, or `change`.

These stores are shared, and reading them is not gated per plugin: any enabled plugin can read `conversations` and `frames`, including turns and pictures that came from the user or from another plugin. Nothing here widens what the host already sent to the model — a frame the session never sent never appears — but it does widen who can see it, from the model provider to every plugin the user has enabled. Treat installing a plugin as granting it that visibility, and say so in your own plugin's description if you read these.

`bus.memory` contains a bounded, in-memory window of recent user-utterance events (one-hour TTL); it is separate from the character's persistent facts, reflections, and persona. `ctx.query_memory(...)` is retained only as a deprecated compatibility call to a placeholder endpoint and does not perform semantic recall.

### Frames

`bus.frames` holds the last few frames the host already pushed to the model provider — exactly the bytes the provider received.

```python
frames = await self.bus.frames.get(max_count=4)
latest = frames.sort(by="timestamp", reverse=True).limit(1)
```

A plugin cannot ask for a capture. A frame the session's own throttle or delivery-mode fence dropped was never sent to the provider, so it never appears here.

**This is not a log and not a queue.** Frames are dropped by design at four points: the message-plane PUB socket is lossy for slow joiners and at its high-water mark, the host publish is non-blocking, the send queue is bounded and refuses frames as soon as it falls behind, and the store keeps only a handful (`NEKO_MESSAGE_PLANE_FRAMES_STORE_MAXLEN`, clamped to 2-8, default 4). Polling for "every frame", or expecting a frame you read once to still be there, will be wrong.

Each record carries `image_base64` (one copy — there is no raw-bytes twin to read), `mime`, `source`, `captured_at`, `turn_id`, `generation`, `frame_id`, and `metadata`. `source` is the channel the host attributed the frame to: `screen`, `camera`, `plugin`, `callback`, `proactive`, `user`, or `unknown` once a turn was reshaped and the host could no longer say. `screen` and `camera` separate the two live channels only on the voice path; in text mode every frame the user shares — a shared screen, a camera still, a dragged-in photo — arrives through one queue that does not keep them apart, so it is reported as `user`. Filter for `user` if you want "something the user showed the character" in both modes. Dedupe on `frame_id`: `generation` orders ambient frames but does not advance for one-shot cue images, so two records can share one.

Pictures a tool handed back are on this bus too, once the follow-up request that carried them was answered. They arrive with `source="plugin"` and a `metadata` of `{"tool_name": "..."}`. A tool may return up to 2 MiB of base64; anything over the delivery budget (500 KiB) is re-encoded by the host through the same profile every model-bound image goes through — JPEG, at most 1280x720 — so it fits under the message plane's record bound and reaches both the model and this bus. The record then says `mime: image/jpeg` even if the tool handed back a PNG, and the tool result carries a model-visible note that the picture was re-encoded. An image that still will not fit after that is dropped with its own warning rather than sent. Read `source` before you read the pixels: a `plugin` frame is media some plugin gave the model — possibly not yours — and is not a picture the user shared with the character.

### Priority levels

| Range | Level | Use case |
|-------|-------|----------|
| 0-2 | Low | Informational messages |
| 3-5 | Medium | General notifications |
| 6-8 | High | Important notifications |
| 9-10 | Emergency | Needs immediate handling |
