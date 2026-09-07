# プラグインのサンプル

## Result 型を使った基本プラグイン

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

## プラグイン間呼び出しを伴う非同期 API クライアント

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
        # プラグイン間呼び出し：まずキャッシュプラグインを確認
        cached = await self.plugins.call_entry(
            "cache_plugin:get", {"key": endpoint}
        )
        cached_value = unwrap_or(cached, None)
        if cached_value and cached_value.get("hit"):
            return Ok(cached_value["data"])

        # 新しいデータを取得
        result = await self.fetch(endpoint=endpoint)
        if isinstance(result, Ok):
            # キャッシュプラグインに保存
            await self.plugins.call_entry(
                "cache_plugin:set",
                {"key": endpoint, "value": result.value}
            )
        return result
```

## フックとタイマーを使ったプラグイン

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

    # --- フック ---

    @before_entry(target="*")
    def count_calls(self, *, entry_id, **_):
        """すべてのエントリーポイント呼び出しをカウントする。"""
        self.call_stats[entry_id] = self.call_stats.get(entry_id, 0) + 1

    @after_entry(target="*")
    def log_results(self, *, entry_id, result, **_):
        """すべてのエントリーポイントの結果をログに記録する。"""
        # user content を含み得る raw result を persistent log に書かない。
        self.logger.info(f"[{entry_id}] completed result_type={type(result).__name__}")

    # --- エントリーポイント ---

    @plugin_entry(id="process", description="Process some data")
    async def process(self, data: str, **_):
        return Ok({"processed": data.upper()})

    @plugin_entry(id="stats", description="Get call statistics")
    async def stats(self, **_):
        return Ok({"stats": dict(self.call_stats)})

    # --- タイマー ---

    @timer_interval(id="health_check", seconds=300, auto_start=True)
    async def health_check(self, **_):
        self.report_status({
            "status": "healthy",
            "uptime": time.time(),
            "total_calls": sum(self.call_stats.values()),
        })
        return Ok({"healthy": True})
```

## 永続ストレージを使ったプラグイン

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
        # self.store は NekoPluginBase が既に構築済みなので、そのまま利用できます。

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

## 動的エントリーを使ったプラグイン

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
        # 設定に基づいて実行時にエントリーを登録
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
