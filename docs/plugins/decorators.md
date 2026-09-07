# Decorators

All decorators are imported from `plugin.sdk.plugin`.

```python
from plugin.sdk.plugin import (
    neko_plugin, plugin_entry, lifecycle, timer_interval, message,
    on_event, custom_event,
    hook, before_entry, after_entry, around_entry, replace_entry,
    plugin, quick_action,  # namespace-style alternative and command-palette hint
)
```

## @neko_plugin

Marks a class as a N.E.K.O. plugin. **Required** on all plugin classes.

```python
@neko_plugin
class MyPlugin(NekoPluginBase):
    pass
```

## @plugin_entry

Defines an externally callable entry point.

```python
@plugin_entry(
    id="process",                # Entry point ID (auto-generated from method name if omitted)
    name="Process Data",         # Display name
    description="Process data",  # Description
    input_schema={...},          # JSON Schema for validation
    params=MyParamsModel,        # Alternative: Pydantic model for input (auto-generates schema)
    kind="action",               # "action" | "service" | "hook" | "custom"
    auto_start=False,            # Metadata flag; ordinary entries are not invoked at load
    persist=False,               # Override post-call state snapshot policy
    model_validate=True,         # Enable Pydantic validation
    timeout=30.0,                # Execution timeout in seconds
    llm_result_fields=["text"],  # Fields to extract for LLM consumption
    llm_result_model=MyResult,   # Pydantic model for result schema
    metadata={"category": "data"}  # Additional metadata
)
async def process(self, data: str, **_):
    return Ok({"result": data})
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `str` | method name | Unique entry point identifier |
| `name` | `str` | `None` | Display name |
| `description` | `str` | `""` | Description |
| `input_schema` | `dict` | `None` | JSON Schema for input validation |
| `params` | `type` | `None` | Pydantic model (auto-generates `input_schema`) |
| `kind` | `str` | `"action"` | Entry type |
| `auto_start` | `bool` | `False` | Metadata flag; ordinary `plugin_entry` handlers are not invoked automatically at load |
| `persist` | `bool` | `None` | Override whether configured freezable state is saved after this entry runs |
| `model_validate` | `bool` | `True` | Enable Pydantic validation |
| `timeout` | `float` | `None` | Execution timeout (seconds) |
| `llm_result_fields` | `list[str]` | `None` | Fields for LLM result extraction |
| `llm_result_model` | `type` | `None` | Pydantic model for result schema |
| `fields` | `type` | `None` | Alias for `params` |
| `metadata` | `dict` | `None` | Additional metadata |

::: tip
Use `**_` only when the handler intentionally accepts extra host-supplied fields. The runtime filters unsupported keyword arguments for handlers with explicit signatures, so it is not mandatory.
:::

Runtime entries must use `async def`; the host rejects synchronous entry handlers.

## @lifecycle

Defines optional handlers for startup, shutdown, external configuration changes,
and process suspension. Use `startup` for initialization; a normal
`@plugin_entry(auto_start=True)` is not executed when the plugin process starts.

```python
@lifecycle(id="startup")
async def on_startup(self, **_):
    cfg = await self.config.dump()
    self.timeout = cfg.get("my_settings", {}).get("timeout", 30)
    return Ok({"status": "ready"})

@lifecycle(id="shutdown")
async def on_shutdown(self, **_):
    session = getattr(self, "session", None)
    if session:
        await session.close()
    return Ok({"status": "stopped"})

@lifecycle(id="config_change")
async def on_config_change(self, old_config, new_config, mode):
    self.timeout = new_config.get("my_settings", {}).get("timeout", 30)
    return Ok({"status": "config_updated"})
```

| Lifecycle ID or action | When it happens | Typical use |
| --- | --- | --- |
| `startup` | Plugin process starts | Load config, open connections, prepare resources |
| `shutdown` | Plugin process stops | Close connections, save state, release resources |
| Plugin Manager Reload | User clicks Reload | Runs `shutdown`, then starts the process and runs `startup` |
| `config_change` | Config is changed externally | Apply new settings without a restart |
| `freeze` / `unfreeze` | Plugin is suspended or resumed | Pause or resume work |

The `reload` lifecycle ID remains accepted for compatibility, but the Plugin
Manager Reload button restarts the process instead of dispatching it. Updating
configuration through `await self.ctx.update_own_config(...)` or
`await self.config.update(...)` also does not dispatch `config_change` back to
the same process; refresh derived state after the call.

## @timer_interval

Defines a scheduled task that executes at fixed intervals.

```python
@timer_interval(
    id="cleanup",
    seconds=3600,           # Execute every hour
    name="Cleanup Task",
    auto_start=True          # Start automatically (default: True)
)
async def cleanup(self, **_):
    # Runs in a dedicated timer thread with its own event loop
    return Ok({"cleaned": True})
```

::: info
Timer tasks must use `async def`. Each timer runs in a separate thread with its own event loop; exceptions are logged but don't stop the timer.
:::

## @message

Defines a handler for messages from the host system.

```python
@message(
    id="handle_chat",
    source="chat",           # Filter by message source
)
async def handle_chat(self, text: str, sender: str, **_):
    return Ok({"handled": True})
```

## @on_event

Generic event handler for custom event types.

```python
@on_event(
    event_type="custom_event",
    id="my_handler",
    kind="hook"
)
async def custom_handler(self, event_data: str, **_):
    return Ok({"processed": True})
```

## @custom_event

Specialized event handler with trigger method control.

```python
@custom_event(
    event_type="data_refresh",
    id="refresh_handler",
    trigger_method="message",  # How this event is triggered
    auto_start=False
)
async def on_refresh(self, source: str, **_):
    return Ok({"refreshed": True})
```

## @quick_action

Marks a plugin entry for prominent display in the command palette. Put it below `@plugin_entry` so Python applies it first:

```python
@plugin_entry(id="get_weather", name="Get Weather")
@quick_action(icon="🌤️", priority=10)
async def get_weather(self, city: str = ""):
    return Ok({"city": city})
```

Larger `priority` values appear earlier. This decorator changes display metadata only; it does not alter Agent routing or execute the entry automatically.

---

## Hook Decorators (AOP)

Hook decorators provide Aspect-Oriented Programming capabilities. They intercept entry point execution.

### @before_entry

Runs before the target entry point. Can modify arguments or abort execution.

```python
@before_entry(target="process", priority=0)
def validate_input(self, *, args, entry_id, **_):
    if not args.get("data"):
        return Err(SdkError("data is required"))
    # Return None to continue, or Err to abort
```

### @after_entry

Runs after the target entry point. Can modify or replace the result.

```python
@after_entry(target="process", priority=0)
def log_result(self, *, result, entry_id, **_):
    self.logger.info(f"Entry {entry_id} returned: {result}")
    # Return None to keep original result, or a new value to replace it
```

### @around_entry

Wraps the target entry point. Full control over execution.

```python
@around_entry(target="process", priority=0)
async def timing_wrapper(self, *, proceed, args, **_):
    import time
    start = time.time()
    result = await proceed(**args)
    elapsed = time.time() - start
    self.logger.info(f"Took {elapsed:.2f}s")
    return result
```

### @replace_entry

Completely replaces the target entry point.

```python
@replace_entry(target="old_entry", priority=0)
async def new_implementation(self, **kwargs):
    return Ok({"replaced": True})
```

### Hook parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target` | `str` | `"*"` | Entry ID to hook (`"*"` = all entries) |
| `priority` | `int` | `0` | Execution order (lower = earlier) |
| `condition` | `str` | `None` | Optional condition expression |

---

## Namespace-Style Alternative: `plugin.*`

For cleaner syntax, use the `plugin` namespace object:

```python
from plugin.sdk.plugin import plugin

@plugin.entry(id="greet", description="Say hello")
async def greet(self, name: str = "World", **_):
    return Ok({"message": f"Hello, {name}!"})

@plugin.lifecycle(id="startup")
async def on_startup(self, **_):
    return Ok({"status": "ready"})

@plugin.hook(target="greet", timing="before")
def validate(self, *, args, **_):
    pass

@plugin.timer(id="heartbeat", seconds=60)
async def heartbeat(self, **_):
    return Ok({"alive": True})

@plugin.message(id="on_chat", source="chat")
async def on_chat(self, text: str, **_):
    return Ok({"handled": True})
```
