# N.E.K.O Plugin CLI で最初のプラグインを作成する

このページでは、N.E.K.O に含まれる Plugin CLI が動作することを確認し、すぐに実行して開発を続けられる Hello World プラグインを作成します。

開発中のプラグインは `N.E.K.O/plugin/plugins/` に直接置きます。完了すると、サンプルコード、設定、テスト、コードチェック、GitHub Actions のリリース設定を含む `hello_world` プロジェクトができます。

## 1. Git と uv を確認する

ターミナルで次を実行します：

```bash
git --version
uv --version
```

| コマンド | 確認すること |
| --- | --- |
| `git --version` | Git を使用できること。N.E.K.O のソースを複製し、プラグインの変更履歴を記録して GitHub へ送信するために使います。 |
| `uv --version` | uv を使用できること。`uv.lock` に固定された Python 依存関係のインストールと Plugin CLI の実行に使います。 |

両方ともバージョンを表示する必要があります。どちらかで「コマンドが見つかりません」と表示された場合は、先にそのツールをインストールします：

- [Git 公式ダウンロード](https://git-scm.com/downloads)から Git をインストールします。
- [uv 公式インストールガイド](https://docs.astral.sh/uv/getting-started/installation/)に従って uv をインストールします。

## 2. N.E.K.O のソースを取得する

Plugin CLI は N.E.K.O のソースに含まれており、単独ではインストールできません。**N.E.K.O プラグイン**の開発を始める推奨方法は、ソースを直接取得することです：

```bash
git clone --filter=blob:none https://github.com/Project-N-E-K-O/N.E.K.O.git
cd N.E.K.O
```

すでにソースがある場合は、再度 clone せず、その checkout に移動します：

```bash
cd /path/to/N.E.K.O
```

::: warning 既存ディレクトリへ clone しないでください
`N.E.K.O` というディレクトリがすでにあると `git clone` は停止します。既存のソースかどうかを確認してください。設定や未コミットの変更が含まれる可能性があるため、ガイドを続ける目的だけで削除しないでください。
:::

## 3. 環境を準備して CLI を確認する

N.E.K.O リポジトリのルートで実行します：

```bash
uv sync
uv run neko-plugin --help
```

`neko-plugin` のヘルプには少なくとも次のコマンドが表示されます：

```text
init
check
sync
build
publish
```

これらが表示されれば CLI を使用できます。以後も `uv run neko-plugin` を使用します。

## 4. ソースツリーにプラグインを作成する

N.E.K.O リポジトリのルートから実行します：

```bash
uv run neko-plugin init hello_world \
  --type plugin \
  --name "Hello World"
```

このコマンドは **Hello World** を次の場所に直接作成します：

```text
plugin/plugins/hello_world/
```

N.E.K.O は `plugin/plugins/` をスキャンするため、開発時にはこのソースがそのまま実行されます。ユーザープラグインディレクトリへのコピーやシンボリックリンクは不要です。

これはコードの読み込み元を示すものです。実行時の設定、永続データ、キャッシュはユーザーデータディレクトリに保存されます。

### ソースとユーザーデータの役割

ソース開発中の同じプラグインは、次の二つの場所を使用します：

```text
N.E.K.O/plugin/plugins/hello_world/       <- 開発者が編集するソース
├── plugin.toml                           <- プラグイン identity とコード entry
├── config.example.toml                   <- 初期 runtime config のテンプレート
├── __init__.py                           <- プラグインコード
└── tests/

<ユーザーデータルート>/plugins/hello_world/ <- N.E.K.O が実行時に書き込む場所
├── config/plugin.toml                    <- このユーザーの runtime config
├── data/                                 <- 永続化するプラグインデータ
└── cache/                                <- 再生成可能なキャッシュ
```

保存場所や関連する環境変数を変更していない場合、既定のユーザーデータルートは次のとおりです：

| OS | 既定ディレクトリ |
| --- | --- |
| Linux | `~/.local/share/N.E.K.O` |
| macOS | `~/Library/Application Support/N.E.K.O` |
| Windows | `%LOCALAPPDATA%\N.E.K.O` |

ソースルートの `plugin.toml` はプラグイン manifest です。ユーザーデータの `config/plugin.toml` は、そのユーザーが実際に使用する runtime config です。初回に N.E.K.O が `config.example.toml` を runtime config へコピーし、既存の設定は上書きしません。

インストール済みコードと、書き込み可能な実行時データは別々に保存されます。このガイドはソース開発用なので、編集するのは `N.E.K.O/plugin/plugins/hello_world/` だけです。インストール先とユーザーデータの保存先は N.E.K.O が管理します。

対象ディレクトリがすでにある場合、CLI は上書きせず停止します。別の新しいディレクトリを選ぶか、既存ディレクトリの用途を確認してください。

## 5. 最初のチェックを実行する

```bash
uv run neko-plugin check hello_world
```

新しいプロジェクトでは次のように表示されます：

```text
[OK] hello_world: check found 0 error(s), 4 warning(s)
```

4 件の warning は次のとおりです：

- `startup` lifecycle hook がない
- `shutdown` lifecycle hook がない
- Git remote が設定されていない
- Git worktree に未 commit のファイルがある

この Hello World plugin には startup と shutdown hook は不要です。Git remote と commit の warning は公開時にだけ影響します。どの warning も plugin の実行を妨げません。`[FAIL]` または error が表示された場合は停止し、コマンドの修正案に従ってください。

## CLI が作成したもの

```text
plugin/plugins/hello_world/
├── .git/
├── .gitignore
├── .vscode/
├── plugin.toml
├── config.example.toml
├── __init__.py
├── pyproject.toml
├── README.md
├── tests/test_smoke.py
├── ruff.toml
└── .github/workflows/
    ├── verify.yml
    └── release.yml
```

ディレクトリ構成や GitHub Actions を手作業で用意する必要はありません。生成されたファイルを使って最初の機能を作ります。

## 6. プラグイン設定を理解する

`plugin/plugins/hello_world/plugin.toml` を開きます。CLI は identity と entry point をすでに記述しています：

```toml
[plugin]
id = "hello_world"
name = "Hello World"
version = "0.1.0"
type = "plugin"
entry = "plugin.plugins.hello_world:HelloWorldPlugin"

[plugin.sdk]
recommended = ">=0.1.0,<0.2.0"
supported = ">=0.1.0,<0.3.0"
```

- `id` は安定したプラグイン identity であり、ソースディレクトリ名と一致させます。
- `name` は Plugin Manager に表示される名前です。
- `version` は次の build と release に使われます。
- `entry` は `module.path:ClassName` 形式で Python class を指定します。
- `[plugin.sdk]` は対応する SDK version を宣言します。

scaffold は新しいユーザー向けの初期 runtime config として `config.example.toml` も作成します：

```toml
[plugin_runtime]
enabled = true
auto_start = false
```

`enabled = true` は既定で読み込み可能であることを示します。`auto_start = false` の場合は Plugin Manager から手動で start します。実際の設定を作成した後は、ユーザーデータ内の `config/plugin.toml` が使われ、`config.example.toml` の変更で既存設定が上書きされることはありません。

## 7. 最初のプラグイン機能を書く

`plugin/plugins/hello_world/__init__.py` には、名前を受け取って greeting を返す entry がすでにあります：

```python
from plugin.sdk.plugin import NekoPluginBase, Ok, neko_plugin, plugin_entry


@neko_plugin
class HelloWorldPlugin(NekoPluginBase):
    @plugin_entry(id="hello", name="Hello", description="Say hello")
    async def hello(self, name: str = "World", **_):
        return Ok({"message": f"Hello, {name}!"})
```

| Code | Meaning |
| --- | --- |
| `@neko_plugin` | class を N.E.K.O plugin として宣言 |
| `NekoPluginBase` | logging、config、storage などを提供 |
| `@plugin_entry(...)` | Plugin Manager に呼び出し可能な機能を公開 |
| `async def hello(...)` | entry の処理を定義。プラグイン entry は `async def` を使用します。 |
| `name: str = "World"` | optional string parameter。省略時は `World` を使用します。 |
| `**_` | N.E.K.O runtime から渡される追加情報を受け取ります。この entry では使いません。 |
| `Ok({...})` | successful result を返す |

`plugin_id` は `hello_world`、この機能の `entry_id` は `hello` です。別の identity なので混同しないでください。

::: tip プラグイン entry と LLM tool は別のものです
`@plugin_entry` は runtime のプラグイン entry を宣言します。`@llm_tool` は会話中に使う tool を登録します。最初の実行では `@plugin_entry` だけでよく、LLM tool の設定は不要です。
:::

最後の行を変更します：

```python
return Ok({"message": f"こんにちは、{name}さん！"})
```

保存後、もう一度チェックします：

```bash
uv run neko-plugin check hello_world
```

## 8. N.E.K.O で実行する

ソース版 N.E.K.O がまだ起動できない場合は[開発環境の準備](/ja/guide/dev-setup)を完了し、リポジトリルートで実行します：

```bash
uv run python launcher.py
```

ターミナルに表示されたアドレスを開き、**Plugins** ページで次を行います：

1. **Refresh** をクリックし、`plugin/plugins/` を再スキャンします。
2. **Hello World** を見つけて start します。
3. 詳細画面の **Entries** を開きます。
4. **Hello** を trigger し、名前を入力します。

成功結果が表示されれば、プラグインはソースディレクトリから直接実行されています。

## 9. 変更して reload する

引き続き `plugin/plugins/hello_world/` を編集します。変更するたびに：

1. 保存して `uv run neko-plugin check hello_world` を実行します。
2. プラグイン詳細に戻り、**Reload** をクリックします。
3. **Hello** を再度 trigger して変更を確認します。

Reload は現在のプラグインを停止し、保存済みソースから再起動します。日常開発ではパッケージの build や import を繰り返す必要はありません。プラグインの追加・削除や `plugin.toml` の変更後は、先に一覧を refresh してください。

## 10. 配布するときだけ build する

ほかのユーザーへインストール可能な形で渡すときに `.neko-plugin` を build します：

```bash
uv run neko-plugin build hello_world --out hello_world.neko-plugin
```

このパッケージはインストールと公開のためのもので、日常開発の必須手順ではありません。GitHub、審査、公開の完全な流れは[Market にプラグインを公開する](/ja/plugins/cli)を参照してください。

## 次のステップ

| 目的 | ドキュメント |
| --- | --- |
| GitHub へ upload し、審査提出と version 公開を行う | [Market にプラグインを公開する](/ja/plugins/cli) |
| `plugin.toml` を理解する | [プラグイン設定](/ja/plugins/plugin-toml) |
| 呼び出し可能な機能を追加する | [エントリーとパラメーター](/ja/plugins/entries) |
| startup と shutdown の処理を追加する | [デコレーター](/ja/plugins/decorators) |
| conversation-time LLM tool を登録する | [LLM Tool Calling](/ja/plugins/tool-calling) |
| 実際の plugin 例を見る | [サンプル](/ja/plugins/examples) |
| error handling を学ぶ | [ベストプラクティス](/ja/plugins/best-practices) |
| SDK 全体を確認する | [SDK リファレンス](/ja/plugins/sdk-reference) |
