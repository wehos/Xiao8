# Adapter と並行処理

## Adapter

Adapter は外部プロトコル（MCP、NoneBot など）を内部プラグイン呼び出しにブリッジします。**ゲートウェイパイプライン**パターンを実装します。

### Adapter を使うべき場合

- N.E.K.O. プラグインを MCP（Model Context Protocol）経由で公開したい
- NoneBot メッセージを受け付けてプラグインにルーティングしたい
- 外部プロトコルをプラグインシステムにブリッジしたい

### Adapter ゲートウェイパイプライン

```
External Request → Normalizer → PolicyEngine → RouteEngine → PluginInvoker → ResponseSerializer → External Response
```

| ステージ | 責務 |
|---------|------|
| **Normalizer** | 外部プロトコル形式を `GatewayRequest` に変換 |
| **PolicyEngine** | アクセス制御、レート制限、バリデーション |
| **RouteEngine** | 呼び出すプラグイン/エントリーを決定 |
| **PluginInvoker** | 実際のプラグイン呼び出しを実行 |
| **ResponseSerializer** | 結果を外部プロトコル形式に変換 |

### Adapter の作成

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

### Adapter モード

| モード | 説明 |
|--------|------|
| `GATEWAY` | 完全なパイプライン処理 |
| `ROUTER` | ルーティングのみ（ポリシーをスキップ） |
| `BRIDGE` | 直接パススルー |
| `HYBRID` | リクエストごとにモードを選択 |

### 組み込みリファレンス: MCP Adapter

`plugin/plugins/mcp_adapter/` に、MCP プロトコルを N.E.K.O. プラグインにブリッジする完全な Adapter 実装があります。以下を実演しています：
- カスタム Normalizer（`MCPRequestNormalizer`）
- カスタム RouteEngine（`MCPRouteEngine`）
- カスタム Invoker（`MCPPluginInvoker`）
- カスタム Serializer（`MCPResponseSerializer`）
- カスタム Transport（`MCPTransportAdapter`）

## 非同期プログラミング

実行時エントリーは `async def` で定義する必要があります。同期ヘルパーも利用できますが、外部には非同期エントリーを通して公開してください：

```python
@plugin_entry(id="async_task")
async def async_task(self, url: str, **_):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return Ok({"data": await response.json()})
```

---

## スレッドセーフティ

タイマータスクは別スレッドで実行されます。共有状態を保護してください：

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
