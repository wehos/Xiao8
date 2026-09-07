# Adapters and Concurrency

## Adapters

Adapters bridge external protocols (MCP, NoneBot, etc.) to internal plugin calls. They implement a **gateway pipeline** pattern.

### When to use Adapters

- You want to expose N.E.K.O plugins via MCP (Model Context Protocol)
- You want to accept NoneBot messages and route them to plugins
- You want to bridge any external protocol to the plugin system

### Adapter Gateway Pipeline

```
External Request → Normalizer → PolicyEngine → RouteEngine → PluginInvoker → ResponseSerializer → External Response
```

| Stage | Responsibility |
|-------|---------------|
| **Normalizer** | Convert external protocol format to `GatewayRequest` |
| **PolicyEngine** | Access control, rate limiting, validation |
| **RouteEngine** | Decide which plugin/entry to call |
| **PluginInvoker** | Execute the actual plugin call |
| **ResponseSerializer** | Convert result back to external protocol format |

### Creating an Adapter

```python
from plugin.sdk.plugin import neko_plugin, plugin_entry, lifecycle, Ok, Err, SdkError
from plugin.sdk.adapter import (
    AdapterGatewayCore, DefaultPolicyEngine, NekoAdapterPlugin,
)
from plugin.sdk.adapter.gateway_models import ExternalRequest

@neko_plugin
class MyProtocolAdapter(NekoAdapterPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.gateway = None

    @lifecycle(id="startup")
    async def startup(self, **_):
        self.gateway = AdapterGatewayCore(
            normalizer=MyNormalizer(),
            policy_engine=DefaultPolicyEngine(),
            route_engine=MyRouteEngine(),
            invoker=MyInvoker(self.ctx),
            serializer=MySerializer(),
            logger=self.logger,
        )
        return Ok({"status": "ready"})

    @plugin_entry(id="handle_request")
    async def handle_request(self, raw_data: dict, **_):
        external = ExternalRequest(protocol="my_protocol", raw=raw_data)
        response = await self.gateway.process(external)
        return Ok(response.to_dict())
```

### Adapter Modes

| Mode | Description |
|------|-------------|
| `GATEWAY` | Full pipeline processing |
| `ROUTER` | Route-only (skip policy) |
| `BRIDGE` | Direct pass-through |
| `HYBRID` | Mode selected per-request |

### Built-in Reference: MCP Adapter

See `plugin/plugins/mcp_adapter/` for a complete adapter implementation that bridges MCP protocol to N.E.K.O plugins. It demonstrates:
- Custom normalizer (`MCPRequestNormalizer`)
- Custom route engine (`MCPRouteEngine`)
- Custom invoker (`MCPPluginInvoker`)
- Custom serializer (`MCPResponseSerializer`)
- Custom transport (`MCPTransportAdapter`)

## Async Programming

Runtime entry points must use `async def`. Synchronous helpers remain supported,
but expose them through an async entry:

```python
@plugin_entry(id="async_task")
async def async_task(self, url: str, **_):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return Ok({"data": await response.json()})
```

---

## Thread Safety

Timer tasks run in separate threads. Protect shared state:

```python
import threading

@neko_plugin
class ThreadSafePlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._lock = threading.Lock()
        self._counter = 0

    @plugin_entry(id="increment")
    async def increment(self, **_):
        with self._lock:
            self._counter += 1
            return Ok({"count": self._counter})

    @timer_interval(id="report", seconds=60, auto_start=True)
    async def report(self, **_):
        with self._lock:
            count = self._counter
        self.report_status({"count": count})
```
