# 插件示例

## 使用 Result 类型的基础插件

```python
from typing import Any
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    Ok, Err, SdkError,
)

@neko_plugin
class GreeterPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.greet_count = 0

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self.logger.info("GreeterPlugin ready")
        return Ok({"status": "ready"})

    @plugin_entry(
        id="greet",
        name="Greet",
        description="Greet someone by name",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "World"}
            }
        }
    )
    async def greet(self, name: str = "World", **_):
        if not name.strip():
            return Err(SdkError("Name cannot be empty"))

        self.greet_count += 1
        return Ok({
            "message": f"Hello, {name}!",
            "total_greets": self.greet_count,
        })
```

## 带跨插件调用的异步 API 客户端

```python
import aiohttp
from typing import Any, Optional
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    Ok, Err, SdkError, unwrap_or,
)

@neko_plugin
class APIClientPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.session: Optional[aiohttp.ClientSession] = None
        self.base_url = "https://api.example.com"

    @lifecycle(id="startup")
    async def startup(self, **_):
        self.session = aiohttp.ClientSession()
        return Ok({"status": "ready"})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        if self.session:
            await self.session.close()
        return Ok({"status": "stopped"})

    @plugin_entry(
        id="fetch",
        name="Fetch Data",
        input_schema={
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"}
            },
            "required": ["endpoint"]
        }
    )
    async def fetch(self, endpoint: str, method: str = "GET", **_):
        try:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            async with self.session.request(method, url) as response:
                data = await response.json()
                return Ok({"status": response.status, "data": data})
        except Exception as e:
            return Err(SdkError(f"Request failed: {e}"))

    @plugin_entry(id="fetch_with_cache")
    async def fetch_with_cache(self, endpoint: str, **_):
        # 跨插件调用：先检查缓存插件
        cached = await self.plugins.call_entry(
            "cache_plugin:get", {"key": endpoint}
        )
        cached_value = unwrap_or(cached, None)
        if cached_value and cached_value.get("hit"):
            return Ok(cached_value["data"])

        # 获取新数据
        result = await self.fetch(endpoint=endpoint)
        if isinstance(result, Ok):
            # 存入缓存插件
            await self.plugins.call_entry(
                "cache_plugin:set",
                {"key": endpoint, "value": result.value}
            )
        return result
```

## 带钩子和定时器的插件

```python
import time
from typing import Any
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    timer_interval, before_entry, after_entry,
    Ok, Err, SdkError,
)

@neko_plugin
class MonitoredPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.call_stats: dict[str, int] = {}

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        return Ok({"status": "ready"})

    # --- 钩子 ---

    @before_entry(target="*")
    def count_calls(self, *, entry_id, **_):
        """统计每个入口点的调用次数。"""
        self.call_stats[entry_id] = self.call_stats.get(entry_id, 0) + 1

    @after_entry(target="*")
    def log_results(self, *, entry_id, result, **_):
        """记录每个入口点的返回结果。"""
        # 不要把可能含用户内容的原始结果写进持久日志。
        self.logger.info(f"[{entry_id}] completed result_type={type(result).__name__}")

    # --- 入口点 ---

    @plugin_entry(id="process", description="Process some data")
    async def process(self, data: str, **_):
        return Ok({"processed": data.upper()})

    @plugin_entry(id="stats", description="Get call statistics")
    async def stats(self, **_):
        return Ok({"stats": dict(self.call_stats)})

    # --- 定时器 ---

    @timer_interval(id="health_check", seconds=300, auto_start=True)
    async def health_check(self, **_):
        self.report_status({
            "status": "healthy",
            "uptime": time.time(),
            "total_calls": sum(self.call_stats.values()),
        })
        return Ok({"healthy": True})
```

## 带持久化存储的插件

```python
from typing import Any
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    Ok, Err, SdkError,
)

@neko_plugin
class NotesPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        # self.store 已由 NekoPluginBase 构造好，直接使用即可。

    @plugin_entry(
        id="save_note",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["title", "content"]
        }
    )
    async def save_note(self, title: str, content: str, **_):
        saved = await self.store.set(f"note:{title}", {
            "title": title,
            "content": content,
        })
        if isinstance(saved, Err):
            return saved
        return Ok({"saved": title})

    @plugin_entry(id="get_note")
    async def get_note(self, title: str, **_):
        stored = await self.store.get(f"note:{title}")
        if isinstance(stored, Err):
            return stored
        note = stored.value
        if note is None:
            return Err(SdkError(f"Note not found: {title}"))
        return Ok(note)
```

## 带动态入口的插件

```python
from typing import Any
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    Ok, Err, SdkError,
)

@neko_plugin
class DynamicPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        # 根据配置在运行时注册入口点
        commands = self.metadata.get("commands", {})
        for cmd_id, cmd_config in commands.items():
            self.register_dynamic_entry(
                entry_id=cmd_id,
                handler=self._make_handler(cmd_config),
                name=cmd_config.get("name", cmd_id),
                description=cmd_config.get("description", ""),
            )
        return Ok({"registered": list(commands.keys())})

    @plugin_entry(id="list_commands")
    async def list_commands(self, **_):
        entries = self.list_entries()
        return Ok({"commands": [e["id"] for e in entries]})

    def _make_handler(self, config):
        template = config.get("template", "Executed: {cmd}")
        async def handler(cmd: str = "", **_):
            return Ok({"output": template.format(cmd=cmd)})
        return handler
```
