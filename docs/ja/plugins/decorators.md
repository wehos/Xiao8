# デコレーター

すべてのデコレーターは `plugin.sdk.plugin` からインポートします。

```python
from plugin.sdk.plugin import (
    neko_plugin, plugin_entry, lifecycle, timer_interval, message,
    on_event, custom_event,
    hook, before_entry, after_entry, around_entry, replace_entry,
    plugin, quick_action,  # namespace style と command-palette hint
)
```

## @neko_plugin

クラスを N.E.K.O. プラグインとしてマークします。すべてのプラグインクラスに**必須**です。

```python
@neko_plugin
class MyPlugin(NekoPluginBase):
    pass
```

## @plugin_entry

外部から呼び出し可能なエントリーポイントを定義します。

```python
@plugin_entry(
    id="process",                # エントリーポイント ID（省略時はメソッド名から自動生成）
    name="Process Data",         # 表示名
    description="Process data",  # 説明
    input_schema={...},          # バリデーション用 JSON Schema
    params=MyParamsModel,        # 代替：入力用 Pydantic モデル（スキーマを自動生成）
    kind="action",               # "action" | "service" | "hook" | "custom"
    auto_start=False,            # metadata flag。通常 entry は load 時に自動実行されない
    persist=False,               # call 後の state snapshot policy を override
    model_validate=True,         # Pydantic バリデーションを有効化
    timeout=30.0,                # 実行タイムアウト（秒）
    llm_result_fields=["text"],  # LLM 消費用に抽出するフィールド
    llm_result_model=MyResult,   # 結果スキーマ用 Pydantic モデル
    metadata={"category": "data"}  # 追加メタデータ
)
async def process(self, data: str, **_):
    return Ok({"result": data})
```

### パラメーター

| パラメーター | 型 | デフォルト | 説明 |
|------------|------|----------|------|
| `id` | `str` | メソッド名 | 一意のエントリーポイント識別子 |
| `name` | `str` | `None` | 表示名 |
| `description` | `str` | `""` | 説明 |
| `input_schema` | `dict` | `None` | 入力バリデーション用 JSON Schema |
| `params` | `type` | `None` | Pydantic モデル（`input_schema` を自動生成） |
| `kind` | `str` | `"action"` | エントリータイプ |
| `auto_start` | `bool` | `False` | metadata flag。通常の `plugin_entry` handler は load 時に自動実行されない |
| `persist` | `bool` | `None` | entry 実行後に configured freezable state を保存するか override |
| `model_validate` | `bool` | `True` | Pydantic バリデーションを有効化 |
| `timeout` | `float` | `None` | 実行タイムアウト（秒） |
| `llm_result_fields` | `list[str]` | `None` | LLM 結果抽出用フィールド |
| `llm_result_model` | `type` | `None` | 結果スキーマ用 Pydantic モデル |
| `fields` | `type` | `None` | `params` のエイリアス |
| `metadata` | `dict` | `None` | 追加メタデータ |

::: tip
handler が host からの追加 field を意図的に受け取る場合だけ `**_` を使います。明示的な signature では runtime が未対応 keyword を filter するため、必須ではありません。
:::

実行時エントリーは `async def` で定義してください。ホストは同期エントリーを受け付けません。

## @lifecycle

起動、終了、外部からの設定変更、プロセスの一時停止を処理する任意のハンドラーです。初期化には `startup` を使います。通常の `@plugin_entry(auto_start=True)` は、プラグインプロセスの起動時には実行されません。

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

| ライフサイクル ID または操作 | 実行される時点 | 主な用途 |
| --- | --- | --- |
| `startup` | プラグインプロセスの起動時 | 設定の読み込み、接続、リソースの準備 |
| `shutdown` | プラグインプロセスの終了時 | 接続の終了、状態の保存、リソースの解放 |
| Plugin Manager の再読み込み | ユーザーが再読み込みを実行 | `shutdown` の後にプロセスを起動し、`startup` を実行 |
| `config_change` | 外部から設定が変更されたとき | 再起動せずに新しい設定を反映 |
| `freeze` / `unfreeze` | プラグインの一時停止または再開時 | 処理の停止または再開 |

SDK は互換性のため `reload` ID を受け付けますが、Plugin Manager の再読み込みボタンはプロセスを再起動するため、このイベントを通知しません。`await self.ctx.update_own_config(...)` または `await self.config.update(...)` で設定を更新した場合も、同じプロセスには `config_change` が通知されません。呼び出し後に派生状態を更新してください。

## @timer_interval

固定間隔で実行されるスケジュールタスクを定義します。

```python
@timer_interval(
    id="cleanup",
    seconds=3600,           # 1時間ごとに実行
    name="Cleanup Task",
    auto_start=True          # 自動的に開始（デフォルト: True）
)
async def cleanup(self, **_):
    # 別スレッドで実行
    return Ok({"cleaned": True})
```

::: info
timer task は `async def` が必須です。各 task は独自 event loop を持つ timer thread で実行され、exception は log されますが timer は停止しません。
:::

## @message

ホストシステムからのメッセージハンドラーを定義します。

```python
@message(
    id="handle_chat",
    source="chat",           # メッセージソースでフィルタリング
)
async def handle_chat(self, text: str, sender: str, **_):
    return Ok({"handled": True})
```

## @on_event

カスタムイベントタイプの汎用イベントハンドラーです。

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

トリガーメソッド制御を備えた特殊化されたイベントハンドラーです。

```python
@custom_event(
    event_type="data_refresh",
    id="refresh_handler",
    trigger_method="message",  # このイベントがトリガーされる方法
    auto_start=False
)
async def on_refresh(self, source: str, **_):
    return Ok({"refreshed": True})
```

## @quick_action

plugin entry を command palette で優先表示します。Python が先に適用するよう `@plugin_entry` の下に置きます。

```python
@plugin_entry(id="get_weather", name="Get Weather")
@quick_action(icon="🌤️", priority=10)
async def get_weather(self, city: str = ""):
    return Ok({"city": city})
```

`priority` が大きいほど先に表示されます。display metadata だけを変更し、Agent routing や自動実行には影響しません。

---

## フックデコレーター（AOP）

フックデコレーターはアスペクト指向プログラミング機能を提供します。エントリーポイントの実行をインターセプトします。

### @before_entry

ターゲットのエントリーポイントの前に実行されます。引数を変更したり、実行を中止したりできます。

```python
@before_entry(target="process", priority=0)
def validate_input(self, *, args, entry_id, **_):
    if not args.get("data"):
        return Err(SdkError("data is required"))
    # 続行するには None を返し、中止するには Err を返す
```

### @after_entry

ターゲットのエントリーポイントの後に実行されます。結果を変更または置換できます。

```python
@after_entry(target="process", priority=0)
def log_result(self, *, result, entry_id, **_):
    self.logger.info(f"Entry {entry_id} returned: {result}")
    # 元の結果を維持するには None を返し、置換するには新しい値を返す
```

### @around_entry

ターゲットのエントリーポイントをラップします。実行を完全に制御できます。

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

ターゲットのエントリーポイントを完全に置換します。

```python
@replace_entry(target="old_entry", priority=0)
async def new_implementation(self, **kwargs):
    return Ok({"replaced": True})
```

### フックパラメーター

| パラメーター | 型 | デフォルト | 説明 |
|------------|------|----------|------|
| `target` | `str` | `"*"` | フック対象のエントリー ID（`"*"` = 全エントリー） |
| `priority` | `int` | `0` | 実行順序（小さいほど先に実行） |
| `condition` | `str` | `None` | オプションの条件式 |

---

## 名前空間スタイルの代替: `plugin.*`

よりクリーンな構文のために、`plugin` 名前空間オブジェクトを使用できます：

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
