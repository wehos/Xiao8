# Adapter 与并发编程

## 适配器（Adapter）

适配器将外部协议（MCP、NoneBot 等）桥接到内部插件调用。它们实现了一个**网关管线**模式。

### 何时使用适配器

- 你想通过 MCP（模型上下文协议）暴露 N.E.K.O 插件
- 你想接受 NoneBot 消息并将其路由到插件
- 你想将任何外部协议桥接到插件系统

### 适配器网关管线

```
External Request → Normalizer → PolicyEngine → RouteEngine → PluginInvoker → ResponseSerializer → External Response
```

| 阶段 | 职责 |
|------|------|
| **Normalizer** | 将外部协议格式转换为 `GatewayRequest` |
| **PolicyEngine** | 访问控制、速率限制、验证 |
| **RouteEngine** | 决定调用哪个插件/入口 |
| **PluginInvoker** | 执行实际的插件调用 |
| **ResponseSerializer** | 将结果转换回外部协议格式 |

### 创建适配器

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

### 适配器模式

| 模式 | 说明 |
|------|------|
| `GATEWAY` | 完整管线处理 |
| `ROUTER` | 仅路由（跳过策略） |
| `BRIDGE` | 直接透传 |
| `HYBRID` | 按请求选择模式 |

### 内置参考：MCP 适配器

参见 `plugin/plugins/mcp_adapter/` 获取完整的适配器实现，它将 MCP 协议桥接到 N.E.K.O 插件。其中演示了：
- 自定义规范化器（`MCPRequestNormalizer`）
- 自定义路由引擎（`MCPRouteEngine`）
- 自定义调用器（`MCPPluginInvoker`）
- 自定义序列化器（`MCPResponseSerializer`）
- 自定义传输层（`MCPTransportAdapter`）

## 异步编程

运行时入口必须使用 `async def`。同步辅助函数仍可使用，但应通过异步入口暴露：

```python
@plugin_entry(id="async_task")
async def async_task(self, url: str, **_):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return Ok({"data": await response.json()})
```

---

## 线程安全

定时任务在独立线程中运行。请保护共享状态：

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
