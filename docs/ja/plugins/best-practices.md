# ベストプラクティス

## Result 型を一貫して使用する

エントリーポイントでは例外を発生させる代わりに、常に `Ok`/`Err` を返してください：

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

## コード構成

初期化、ヘルパー、パブリックエントリーポイントを分離してください：

```python
@neko_plugin
class WellOrganizedPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._initialize()

    # --- ライフサイクル ---
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        return Ok({"status": "ready"})

    # --- プライベートヘルパー ---
    def _initialize(self):
        """リソースのセットアップ。"""
        pass

    def _validate(self, data):
        """内部バリデーション。"""
        pass

    # --- パブリックエントリーポイント ---
    @plugin_entry(id="process")
    async def process(self, data: str, **_):
        self._validate(data)
        return Ok({"result": self._do_work(data)})
```

## ロギング

適切なログレベルを使用してください：

| レベル | 使用場面 |
|--------|---------|
| `debug` | 詳細な診断情報 |
| `info` | 通常動作のマイルストーン |
| `warning` | 予期しないが処理された状況 |
| `error` | 注意が必要なエラー |
| `exception` | スタックトレース付きエラー |

会話の原文、ユーザーが入力した秘密情報、その他の機密データをログやプロセス出力に書かないでください。通常の診断には、伏せ字にした長さ、ID、エラー種別を使います。機密内容を含む再現が必要な場合も、元データを出力せず、合成したテストデータを使ってください。

```python
self.logger.debug(f"Processing item {item_id}")
self.logger.info(f"Plugin started successfully")
self.logger.warning(f"Retry attempt {attempt}/3")
self.logger.error(f"Failed to connect: {err}")
self.logger.exception(f"Unexpected error in process()")
```

## ステータス更新

長時間実行される操作中は進捗を報告してください：

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

## 入力バリデーション

自動 JSON Schema バリデーションのために `input_schema` を使用するか、Pydantic モデルのために `params` を使用してください：

```python
# オプション A: JSON Schema
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

# オプション B: Pydantic モデル（スキーマを自動生成）
from pydantic import BaseModel, Field

class UserInput(BaseModel):
    email: str = Field(..., description="User email")
    age: int = Field(..., ge=0, le=150)

@plugin_entry(id="validated_v2", params=UserInput)
async def validated_v2(self, email: str, age: int, **_):
    return Ok({"email": email, "age": age})
```

## 作業ディレクトリ

`self.plugin_dir` は、パッケージ内のコードと読み取り専用リソースにだけ使います。実行時設定は `self.config` で管理し、永続データとキャッシュはホストが割り当てた状態ディレクトリに保存します：

```python
# 実行コードのディレクトリ（plugin.toml とパッケージ内の素材がある場所）
manifest_path = self.plugin_dir / "plugin.toml"

# 実行コードとは分離された状態ディレクトリ
db_path = self.data_path("cache.db")       # → <plugin-state-root>/data/cache.db
preview_path = self.cache_path("preview.png")  # → <plugin-state-root>/cache/preview.png
```

## プラグイン間呼び出しのエラーハンドリング

他のプラグインを呼び出す際は常に `Err` を処理してください：

```python
@plugin_entry(id="orchestrate")
async def orchestrate(self, **_):
    # まず依存関係を確認
    dep = await self.plugins.require_enabled("dependency_plugin")
    if isinstance(dep, Err):
        return Err(SdkError("Required plugin 'dependency_plugin' is not available"))

    # 呼び出しを実行
    result = await self.plugins.call_entry("dependency_plugin:do_work", {"key": "val"})
    if isinstance(result, Err):
        self.logger.error(f"Cross-plugin call failed: {result.error}")
        return Err(SdkError("Dependency call failed"))

    return Ok({"combined": result.value})
```

## グレースフルシャットダウン

shutdown ライフサイクルでリソースをクリーンアップしてください：

```python
@lifecycle(id="shutdown")
async def on_shutdown(self, **_):
    # ネットワーク接続を閉じる
    if self.session:
        await self.session.close()

    # self.store のフラッシュは不要です：各 set() は同期的にコミットされます。
    # 自分で store を開いた場合は、ここで閉じてください（任意）：
    # await self.store.close()

    # タイマーをキャンセル（自動的に処理されますが、ログに記録）
    self.logger.info("Plugin shutting down gracefully")
    return Ok({"status": "stopped"})
```

## プラグインチェックリスト

プラグインをリリースする前に確認してください：

- [ ] すべてのエントリーポイントが `Ok`/`Err` を返している（生の dict や例外ではなく）
- [ ] setup/cleanup が本当に必要な resource にだけ lifecycle hook を追加している
- [ ] entry parameter に inferred schema、explicit `input_schema`、または Pydantic `params` model を適切に使っている
- [ ] handler signature は消費する field だけを宣言し、追加 field を意図する場合だけ `**_` を使う
- [ ] 通常 metadata は logger、raw privacy-sensitive content は logger に書かない
- [ ] タイマーを使用する場合、共有状態がロックで保護されている
- [ ] プラグイン間呼び出しが `Err` 結果を処理している
- [ ] `plugin.toml` の host-loading `[plugin].entry` path と SDK version constraint が正しい
