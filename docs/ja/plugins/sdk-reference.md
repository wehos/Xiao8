# プラグイン機能と SDK リファレンス

すべてのプラグイン開発 API は `plugin.sdk.plugin` からインポートします。

```python
from plugin.sdk.plugin import (
    # ベース
    NekoPluginBase, PluginMeta,
    # デコレーター
    EntryKind, neko_plugin, plugin_entry, lifecycle, timer_interval, message,
    on_event, custom_event, hook, before_entry, after_entry, around_entry,
    replace_entry, quick_action, plugin, ui,
    # LLM tool と OS activity
    llm_tool, LlmToolMeta, OsActivitySnapshot, get_os_activity_snapshot,
    # plugin-local i18n と settings
    PluginI18n, tr, PluginSettings, SettingsField,
    # Result 型
    Ok, Err, Result, PushMessageResult, unwrap, unwrap_or,
    # ランタイムヘルパー
    Plugins, PluginRouter, PluginConfig, PluginStore,
    SystemInfo,
    # エラー
    SdkError, TransportError,
    # ロギング
    get_plugin_logger,
)
```

`plugin.sdk.plugin` が supported developer import surface です。root `plugin.sdk` package は意図的に conservative shared subset だけを公開するため、plugin-only helper がすべて再 export されるとは仮定しないでください。

## NekoPluginBase

すべてのプラグインは `NekoPluginBase` を継承する必要があります。

```python
@neko_plugin
class MyPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
```

### プロパティ

| プロパティ | 型 | 説明 |
|----------|------|------|
| `self.ctx` | `PluginContext` | ランタイムコンテキスト（ホストにより注入） |
| `self.plugin_id` | `str` | このプラグインの一意の識別子 |
| `self.plugin_dir` | `Path` | コード、Manifest、静的リソースを含むインストール先ディレクトリ |
| `self.config_dir` | `Path` | `self.plugin_dir` の互換エイリアス |
| `self.storage_dir` | `Path` | このプラグインに割り当てられたユーザーストレージルート |
| `self.runtime_config_path` | `Path` | 外部ランタイム設定ファイルのパス |
| `self.metadata` | `dict` | `plugin.toml` からのプラグインメタデータ |
| `self.bus` | `SdkBusContext` | host state の read/watch facade。publish/emit API はありません |
| `self.plugins` | `Plugins` | プラグイン間呼び出しヘルパー |
| `self.system_info` | `SystemInfo` | ホストシステムのメタデータ |

### よく使う機能

`NekoPluginBase` は、追加設定なしでログと実行時設定を提供します：

```python
self.logger.info("Processing request: {}", request_id)
timeout = await self.config.get_int("my_settings.timeout", default=30)
```

ログは Plugin Manager に表示され、ホストが管理するログディレクトリにも保存されます。ファイルの保存先とローテーション方針はホストが管理します。実行時設定の更新には `await self.ctx.update_own_config(...)` または `await self.config.update(...)` を使い、呼び出し後に設定へ依存する派生状態を更新してください。

データの種類に応じて保存方法を選びます：

| 用途 | API |
| --- | --- |
| 小さなキー・バリュー状態 | `[plugin.store]` を有効にして `self.store` を使用 |
| 構造化された SQLite データ | `[plugin.database]` を有効にして `self.db` を使用 |
| 任意の永続ファイル | `self.data_path(...)` |
| 再生成可能なファイル | `self.cache_path(...)` |

```python
from plugin.sdk.plugin import unwrap

unwrap(await self.store.set("last_query", "weather"))

async with unwrap(await self.db.session()) as session:
    await session.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT)"
    )
    await session.commit()
```

プラグインの表示文を翻訳する場合は、`plugin.toml` で `[plugin.i18n]` を設定し、各言語の JSON ファイルを追加してから `self.i18n.t(...)` を使います：

```python
message = self.i18n.t("greeting", name="Alice")
```

### メソッド

#### `report_status(status: dict) -> None`

プラグインのステータスをホストプロセスに報告します。

```python
self.report_status({
    "status": "processing",
    "progress": 50,
    "message": "Halfway done..."
})
```

#### `push_message(**kwargs) -> PushMessageResult`

v2 schema でホストシステムにメッセージをプッシュします。

```python
result = self.push_message(
    source="my_feature",
    visibility=["chat"],       # []、["chat"]、["hud"]、または両方
    ai_behavior="blind",       # "respond"、"read"、"blind"
    parts=[{"type": "text", "text": "タスクが完了しました"}],
    priority=5,
)

if not result["submitted"]:
    # ローカル状態を保持します。再試行と重複排除はプラグイン側の方針です。
    self.logger.warning("message submission failed: %s", result["reason"])
```

`submitted=True` は、SDK の正規ローカル送信経路が payload の送信責任を
引き受けたことだけを示します。ホストでの消費、モデル生成、再生完了の
確認ではありません。拒否理由は `backpressure`、`transport_error`、
`transport_unavailable` のいずれかで、メッセージ本文や生の例外テキストは含みません。
拒否結果には従来の呼び出し元との互換性のため `ok=False` も含まれます。新しいコードでは
`submitted` を正式な判定基準として使用してください。

v1 field（`message_type`、`content`、`delivery`、`reply` および他の legacy alias）は deprecated ですが current source では変換されます。今すぐ移行し、この文書から正確な removal release を保証しないでください。[移行ガイド](./migration-v0.9#push-message-v2)を参照してください。

#### メディアパート

- テキストと画像のパートはホストで処理されます。小さな画像は inline で送れます：

  ```python
  parts=[{"type": "image", "data": image_bytes, "mime": "image/png"}]
  ```

- 小さくない画像は host に一時 upload し、返された part を送信してください：

  ```python
  image_part = await self.ctx.images.upload(image_bytes, mime="image/png")
  result = self.push_message(
      source="my_feature",
      visibility=["chat"],
      ai_behavior="read",
      parts=[image_part],
  )
  ```

  model への配信では任意の外部画像 URL は受け付けません。この host の一時 upload を使ってください。inline 画像は message plane payload の予算を message 全体と共有しますが、upload 後の part は画像 bytes をその envelope に含めません。lifecycle handler の実行中は plugin command loop が upload response を処理できないため、`ctx.images.upload()` は使用できません。plugin entry、timer、message handler、または custom event handler から呼び出してください。
- `visibility` はユーザーのチャットまたは HUD への表示を決め、`ai_behavior` はモデルがメッセージを読むか応答するかを独立して決めます。
- 音声と動画のメッセージパートは現在ホストから配信されません。送信成功を再生・表示の成功として扱わないでください。
- Hosted UI パネル内ではユーザー向けの音声や動画を再生できますが、ネイティブのチャットまたはモデル経路へのメディア配信ではありません。

#### `data_path(*parts) -> Path`

プラグインの `data/` ディレクトリ配下のパスを取得します。

```python
db_path = self.data_path("records.db")
# → <storage-root>/plugins/<plugin_id>/data/records.db
```

#### `cache_path(*parts) -> Path`

プラグインの削除可能なキャッシュディレクトリ配下のパスを取得します。

```python
preview_path = self.cache_path("preview.png")
# → <storage-root>/plugins/<plugin_id>/cache/preview.png
```

#### `register_dynamic_entry(entry_id, handler, ...) -> bool`

実行時にエントリーポイントを登録します（デコレーター経由ではなく）。

```python
self.register_dynamic_entry(
    entry_id="dynamic_greet",
    handler=lambda name="World", **_: Ok({"msg": f"Hi {name}"}),
    name="Dynamic Greet",
    description="動的に登録された挨拶",
)
```

#### `unregister_dynamic_entry(entry_id) -> bool`

動的に登録されたエントリーを削除します。

#### `list_entries(include_disabled=False) -> list[dict]`

すべてのエントリーポイント（静的 + 動的）を一覧表示します。

#### `enable_entry(entry_id) / disable_entry(entry_id) -> bool`

実行時に動的エントリーを有効化または無効化します。

#### `register_static_ui(directory, *, index_file, cache_control) -> bool`

このプラグインの静的 Web UI ディレクトリを登録します。

```python
self.register_static_ui("static")  # <plugin_dir>/static/index.html を配信
```

#### `include_router(router, *, prefix) -> None`

大規模または機能分割された通常 Plugin を整理するために `PluginRouter` を mount します。

関連 method は `exclude_router(router_or_name) -> bool`、`get_router(name)`、`list_routers()` です。Router は manifest `[plugin].entry` にはできず、この mount path は `on_mount` / `on_unmount` を自動実行しません。

#### Hosted/static UI と list action

Hosted TSX は exported `ui` namespace と manifest surface を使います。日本語 mirror は未整備のため [English Hosted UI](/plugins/hosted-ui) を参照してください。legacy static UI は `register_static_ui(...)`、list-row action は `set_list_actions(...)`、`register_list_action(...)`、`clear_list_actions()`、`get_list_actions()` で管理します。

#### LLM tool method

`register_llm_tool(...)`、`unregister_llm_tool(name)`、`list_llm_tools()` は `@llm_tool` の imperative API です。conversation-time tool を登録し、user-plugin Agent entry とは別です。[LLM Tool Calling](./tool-calling) を参照してください。

#### `run_update(**kwargs) -> object` (async)

長時間実行中の操作中にホストに更新を送信します。

#### `export_push(**kwargs) -> object` (async)

エクスポートデータをホストにプッシュします。

#### `finish(**kwargs) -> Any` (async)

タスク完了をホストに通知します。

### 返信制御

`finish()` メソッドは `reply` パラメータ（デフォルト `True`）を受け付け、プラグインの結果がメインキャラクターの発話をトリガーするかどうかを制御します。

```python
# 通常：キャラクターが結果を報告する
return await self.finish(data={"summary": "完了"}, reply=True)

# サイレント：結果は記録されるがキャラクターは話さない
return await self.finish(data={"summary": "完了"}, reply=False)
```

### LLM 結果フィールドフィルタリング

`@plugin_entry`（静的エントリ）または `register_dynamic_entry()`（動的エントリ）の `llm_result_fields` パラメータを使用して、メイン LLM が参照できる結果フィールドを制御します。リストにないフィールドは LLM プロンプトから除外されますが、タスクレジストリには保存されます。

```python
# 静的エントリ
@plugin_entry(llm_result_fields=["summary"])
async def search(self, query: str):
    return await self.finish(data={"summary": "3件の結果", "raw_results": [...]})

# 動的エントリ
self.register_dynamic_entry(
    entry_id="my-tool",
    handler=handler,
    llm_result_fields=["summary"],
)
```

---

## Result 型: Ok / Err

SDK は例外の代わりに、Rust にインスパイアされた Result 型をエラーハンドリングに使用します。

```python
from plugin.sdk.plugin import Ok, Err, unwrap, unwrap_or

# 成功を返す
return Ok({"data": result})

# エラーを返す
return Err(SdkError("something went wrong"))

# 結果を消費する
result = await self.plugins.call_entry("other:do_stuff")
if isinstance(result, Ok):
    data = result.value
else:
    error = result.error
    self.logger.error(f"Call failed: {error}")

# ヘルパー関数
value = unwrap(result)           # Err の場合は例外を発生
value = unwrap_or(result, None)  # Err の場合はデフォルト値を返す
```

---

## Plugins（プラグイン間呼び出し）

`self.plugins` 経由でアクセスします。

```python
# すべてのプラグインを一覧表示
result = await self.plugins.list()

# 有効なプラグインのみを一覧表示
result = await self.plugins.list(enabled=True)

# プラグイン ID を取得
result = await self.plugins.list_ids()

# プラグインが存在するか確認
result = await self.plugins.exists("other_plugin")

# 他のプラグインのエントリーポイントを呼び出す
result = await self.plugins.call_entry("other_plugin:do_work", {"key": "value"})

# JSON オブジェクトレスポンスを保証して呼び出す
result = await self.plugins.call_entry_json("other_plugin:get_data")

# プラグインが存在し有効であることを要求する
result = await self.plugins.require_enabled("dependency_plugin")
```

すべてのメソッドは `Result` 型を返します — `.value` を使用する前に `isinstance(result, Ok)` で確認してください。

---

## PluginStore（永続ストレージ）

`self.store` 経由でアクセスします（ホストがプラグイン構築時に事前生成して注入するため、自分でインスタンス化する必要はありません）。

`PluginStore` のすべてのメソッドは `Result` を返すため、`unwrap_or(...)` で展開してください。

```python
unwrap_or(await self.store.set("key", {"count": 42}), None)
value = unwrap_or(await self.store.get("key"), None)  # → {"count": 42}
```

---

## SystemInfo

`self.system_info` 経由でアクセスします。これらのメソッドはいずれも `Result` を返すため、`unwrap_or(...)` で展開してください。

```python
config = unwrap_or(await self.system_info.get_system_config(), {})
settings = unwrap_or(await self.system_info.get_server_settings(), {})
python_env = unwrap_or(await self.system_info.get_python_env(), {})
```

---

## PluginContext (ctx)

`ctx` オブジェクトは構築時にホストにより注入されます。

| プロパティ | 型 | 説明 |
|----------|------|------|
| `ctx.plugin_id` | `str` | プラグイン識別子 |
| `ctx.config_path` | `Path` | `plugin.toml` へのパス |
| `ctx.logger` | `Logger` | ロガーインスタンス |
| `ctx.bus` | `SdkBusContext` | host state の read/watch facade |
| `ctx.metadata` | `dict` | プラグインメタデータ |

### Bus と Memory

async entry 内では、local list 操作より先に `get()` を await します。

```python
events = await self.bus.events.get(plugin_id=self.plugin_id, max_count=50)
recent = events.filter(priority_min=1).sort(by="timestamp", reverse=True).limit(20)

records = await self.bus.memory.get(bucket_id="default", limit=20)
```

list surface は `filter` / `where`、`sort`、`limit`、`watch` です。callable の `filter(predicate)`、`where(predicate)`、`sort(key=...)` は local-only です。replayable な watcher chain では structured `filter(field=value, ...)` と `sort(by=...)` を使います。`watch()` を使えるのは `messages`、`events`、`lifecycle` だけで、`conversations`、`memory`、`frames` は read-only snapshot です。watcher subscription は `add`、`del`、`change` のみ受け付けます。

【アクセス範囲】これらの store は共有で、読み取りは plugin 単位で制限されません：有効化されたどの plugin も `conversations` と `frames` を読めます。user 由来のものも、他の plugin 由来の turn や画像も含みます。host が既に model へ送った内容が広がるわけではありません（session が送らなかった frame はここにも現れません）が、**誰が見られるか**は広がります——model provider から、user が有効にしたすべての plugin へ。plugin を install することはその可視性を与えることだと考えてください。これらを読む plugin は、その旨を自分の説明に書いてください。

`bus.memory` に入るのは、件数制限付きでメモリ上に保持される最近のユーザー発話イベント（TTL は 1 時間）です。キャラクターの永続的な facts、reflections、persona とは別物です。`ctx.query_memory(...)` は非推奨の placeholder endpoint に対する互換呼び出しとしてのみ残されており、semantic recall は行いません。

### Frames

`bus.frames` には、host が model provider にすでに送った直近数枚の frame が入ります。provider が実際に受け取ったバイト列そのものです。

```python
frames = await self.bus.frames.get(max_count=4)
latest = frames.sort(by="timestamp", reverse=True).limit(1)
```

プラグインから撮影を要求することはできません。session 側の throttle や delivery-mode fence で落ちた frame は provider に送られていないので、ここにも現れません。

**これは log でも queue でもありません。** frame は 4 箇所で意図的に捨てられます。message plane の PUB socket は slow joiner と high-water mark で落としますし、host 側の publish は non-blocking、送信 queue は上限付きで遅れ始めた時点で frame を拒否し、store 自体も数枚しか保持しません（`NEKO_MESSAGE_PLANE_FRAMES_STORE_MAXLEN`、2-8 に丸められ、既定は 4）。「すべての frame」を polling したり、一度読めた frame がまだ残っていると考えたりすると間違います。

各 record は `image_base64`（1 部のみ。読める raw bytes の複製はありません）、`mime`、`source`、`captured_at`、`turn_id`、`generation`、`frame_id`、`metadata` を持ちます。`source` は host がその frame に割り当てた channel です：`screen`、`camera`、`plugin`、`callback`、`proactive`、`user`、そして turn が組み換えられ host が判断できなくなった場合の `unknown` です。`screen` と `camera` がこの 2 つの live channel を区別するのは音声経路だけです。text mode では user が共有するすべての frame——共有画面、camera の静止画、drag & drop した写真——が区別を保たない 1 本の queue を通るため、`user` として報告されます。両方の mode で「user が character に見せたもの」が欲しい場合は `user` で絞り込んでください。重複排除は `frame_id` で行ってください。`generation` は常時取得される frame の順序を表すだけで、単発の cue 画像では進まないため、2 つの record が同じ値を持ち得ます。

tool が返した画像も、それを載せた後続 request に応答があった時点でこの bus に載ります。その frame は `source="plugin"`、`metadata` は `{"tool_name": "..."}` です。tool は base64 で最大 2 MiB まで返せます。配信予算（500 KiB）を超えたものは、model 向け画像と同じ profile（JPEG、最大 1280x720）で host が再エンコードするので、message plane の record 上限に収まり、model にも この bus にも届きます。その場合 record の `mime` は tool が PNG を返していても `image/jpeg` になり、tool result には再エンコードされた旨の model から見える注記が付きます。再エンコードしても収まらない画像は、専用の警告とともに落とされます。pixel より先に `source` を読んでください。`plugin` の frame は何らかの plugin がモデルに渡した media であり（自分のものとは限りません）、ユーザーがキャラクターに共有した画面ではありません。

### 優先度レベル

| 範囲 | レベル | 用途 |
|------|--------|------|
| 0-2 | 低 | 情報メッセージ |
| 3-5 | 中 | 一般的な通知 |
| 6-8 | 高 | 重要な通知 |
| 9-10 | 緊急 | 即座の対応が必要 |
