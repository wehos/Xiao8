# Best Practices

## Use Result types consistently

Always return `Ok`/`Err` instead of raising exceptions in entry points:

```python
from plugin.sdk.plugin import Ok, Err, SdkError

@plugin_entry(id="process")
async def process(self, data: str, **_):
    if not data:
        return Err(SdkError("data is required"))

    try:
        result = self._do_work(data)
        return Ok({"result": result})
    except ValueError as e:
        return Err(SdkError(f"Validation error: {e}"))
    except Exception as e:
        self.logger.exception(f"Unexpected error: {e}")
        return Err(SdkError(f"Internal error"))
```

## Code organization

Separate initialization, helpers, and public entry points:

```python
@neko_plugin
class WellOrganizedPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._initialize()

    # --- Lifecycle ---
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        return Ok({"status": "ready"})

    # --- Private helpers ---
    def _initialize(self):
        """Setup resources."""
        pass

    def _validate(self, data):
        """Internal validation."""
        pass

    # --- Public entry points ---
    @plugin_entry(id="process")
    async def process(self, data: str, **_):
        self._validate(data)
        return Ok({"result": self._do_work(data)})
```

## Logging

Use appropriate log levels:

| Level | When to use |
|-------|------------|
| `debug` | Detailed diagnostic information |
| `info` | Normal operation milestones |
| `warning` | Unexpected but handled situations |
| `error` | Errors that need attention |
| `exception` | Errors with full stack trace |

Keep raw conversations, user-entered secrets, and other privacy-sensitive payloads out of logs and process output. Prefer redacted lengths, IDs, and error types. If a diagnostic truly requires sensitive content, reproduce it with synthetic data instead of printing or logging the original payload.

```python
self.logger.debug(f"Processing item {item_id}")
self.logger.info(f"Plugin started successfully")
self.logger.warning(f"Retry attempt {attempt}/3")
self.logger.error(f"Failed to connect: {err}")
self.logger.exception(f"Unexpected error in process()")
```

## Status updates

Report progress during long-running operations:

```python
@plugin_entry(id="batch_job")
async def batch_job(self, items: list, **_):
    total = len(items)
    for i, item in enumerate(items):
        self._process(item)
        self.report_status({
            "status": "processing",
            "progress": (i + 1) / total * 100,
            "message": f"Processing {i+1}/{total}"
        })

    self.report_status({"status": "completed", "progress": 100})
    return Ok({"processed": total})
```

## Input validation

Use `input_schema` for automatic JSON Schema validation, or `params` for Pydantic models:

```python
# Option A: JSON Schema
@plugin_entry(
    id="validated",
    input_schema={
        "type": "object",
        "properties": {
            "email": {"type": "string", "format": "email"},
            "age": {"type": "integer", "minimum": 0, "maximum": 150}
        },
        "required": ["email", "age"]
    }
)
async def validated(self, email: str, age: int, **_):
    return Ok({"email": email, "age": age})

# Option B: Pydantic model (auto-generates schema)
from pydantic import BaseModel, Field

class UserInput(BaseModel):
    email: str = Field(..., description="User email")
    age: int = Field(..., ge=0, le=150)

@plugin_entry(id="validated_v2", params=UserInput)
async def validated_v2(self, email: str, age: int, **_):
    return Ok({"email": email, "age": age})
```

## Working directory

Use `self.plugin_dir` only for packaged code and read-only resources. Runtime
configuration is managed through `self.config`; persistent data and cache belong
under the host-assigned state root:

```python
# Executable directory (where plugin.toml and packaged assets live)
manifest_path = self.plugin_dir / "plugin.toml"

# State directories, separate from executable code
db_path = self.data_path("cache.db")       # → <plugin-state-root>/data/cache.db
preview_path = self.cache_path("preview.png")  # → <plugin-state-root>/cache/preview.png
```

## Cross-plugin call error handling

Always handle `Err` when calling other plugins:

```python
@plugin_entry(id="orchestrate")
async def orchestrate(self, **_):
    # Check dependency first
    dep = await self.plugins.require_enabled("dependency_plugin")
    if isinstance(dep, Err):
        return Err(SdkError("Required plugin 'dependency_plugin' is not available"))

    # Make the call
    result = await self.plugins.call_entry("dependency_plugin:do_work", {"key": "val"})
    if isinstance(result, Err):
        self.logger.error(f"Cross-plugin call failed: {result.error}")
        return Err(SdkError("Dependency call failed"))

    return Ok({"combined": result.value})
```

## Graceful shutdown

Clean up resources in the shutdown lifecycle:

```python
@lifecycle(id="shutdown")
async def on_shutdown(self, **_):
    # Close network connections
    if self.session:
        await self.session.close()

    # No need to flush self.store: each set() commits synchronously.
    # If you opened the store yourself, close it here (optional):
    # await self.store.close()

    # Cancel timers (handled automatically, but log it)
    self.logger.info("Plugin shutting down gracefully")
    return Ok({"status": "stopped"})
```

## Plugin checklist

Before shipping your plugin:

- [ ] All entry points return `Ok`/`Err` (not raw dicts or exceptions)
- [ ] Lifecycle hooks are added only for resources that actually need setup or cleanup
- [ ] Entry parameters have an inferred schema, explicit `input_schema`, or a Pydantic `params` model as appropriate
- [ ] Handler signatures declare what they consume; `**_` is used only when extra fields are intentional
- [ ] Normal metadata uses the logger; raw privacy-sensitive content never does
- [ ] Shared state is protected with locks if timers are used
- [ ] Cross-plugin calls handle `Err` results
- [ ] `plugin.toml` has a correct host-loading `[plugin].entry` path and SDK version constraints
