# プラグイン設定 (plugin.toml)

すべてのプラグインのルートには `plugin.toml` があります。package の種類、host が import する Python class、公開する optional capability を N.E.K.O に伝えます。

::: warning 2 種類の entry
`[plugin].entry = "module.path:ClassName"` は **host-loading entry point** です。plugin process 起動時に 1 つの `NekoPluginBase` class を import します。`greet` のような runtime entry ID は `@plugin_entry(id="greet")` または `register_dynamic_entry(...)` から作られ、plugin のロード後に Agent が選択します。
:::

以下は架空の "Smart Notes" プラグインの完全な設定例です。このプラグインはノートの検索と作成、自分専用の UI、多言語対応、AI エージェントからの呼び出しに対応しています。

## 完全な例

```toml
[plugin]
id = "smart_notes"
name = "Smart Notes"
type = "plugin"
description = "Manage your notes: search, create, organize, with AI-powered classification."
short_description = "Note management with AI-powered organization."
keywords = ["note", "筆記", "memo", "record", "メモ"]
version = "1.2.0"
entry = "plugin.plugins.smart_notes:SmartNotesPlugin"

[plugin.author]
name = "Alice"

[plugin.sdk]
recommended = ">=0.1.0,<0.2.0"
supported = ">=0.1.0,<0.3.0"

[plugin.i18n]
default_locale = "zh-CN"
locales_dir = "i18n"

[plugin.store]
enabled = true

[plugin.ui]
enabled = true

[[plugin.ui.panel]]
id = "main"
title = "Smart Notes"
entry = "ui/panel.tsx"
context = "dashboard"
permissions = ["state:read", "action:call"]

[[plugin.ui.guide]]
id = "quickstart"
title = "User Guide"
entry = "docs/guide.md"
permissions = ["state:read"]

[plugin_runtime]
enabled = true
auto_start = true

[notes]
max_per_page = 20
auto_classify = true
```

## セクションごとの説明

### `[plugin]` — このプラグインについて

```toml
[plugin]
id = "smart_notes"
name = "Smart Notes"
version = "1.2.0"
entry = "plugin.plugins.smart_notes:SmartNotesPlugin"
```

サポート対象の check / release workflow では、この 4 フィールドが **必須** です。従来のソース探索では、不完全な manifest やディレクトリ名と ID が異なるプラグインを読み込める場合がありますが、有効なリリース package であることを意味しません。`id` は `^[A-Za-z0-9_-]+$` に一致し、一意でなければなりません。パッケージのビルドと本番環境へのインストールでは、宣言 ID、アーカイブ内のディレクトリ、インストール先、エントリーパッケージを一致させる必要があります。競合時に接尾辞付きのコピーは作成されません。`entry` は `module.path:ClassName` 形式で `NekoPluginBase` のサブクラスを指す必要があり、`PluginRouter` は直接起動できません。

通常の plugin では `type = "plugin"` は default なので省略できます。Adapter package のみ `type = "adapter"` を使います。削除済みの `extension` type と `[plugin.host]` table は拒否されます。

リリース間で `id` を変更しないでください。upgrade、reinstall、downgrade は実行コードだけを置き換え、実行時の `config`、`data`、`cache` は保持します。`id` を変更すると別のプラグインとして扱われます。任意の `previous_ids` は新旧 ID の同時インストールを防ぐだけで、runtime alias ではなく、旧データの移行や削除も行いません。置換操作にはユーザーの明示的な確認が必要です。

```toml
description = "Manage your notes: search, create, organize, with AI-powered classification."
short_description = "Note management with AI-powered organization."
keywords = ["note", "筆記", "memo", "record", "メモ"]
```

これらの field は host が plugin をロードした後の Agent routing に使われます。

- `description` — plugin metadata と Agent fine assessment に使う完全な説明です。
- `short_description` — coarse screening 用の短い説明です。省略時は `description` から生成して cache される場合があります。
- `keywords` — 正規表現 pattern です。hit は Stage 1 candidate に union されますが、Stage 2 を省略したり実行を保証したりしません。

listener/integration を Agent dispatch から完全に外すには `passive = true` を設定します。non-passive plugin も Agent-visible runtime entry が 1 つ以上なければ candidate になりません。

Stage 2 の最終出力は `plugin_id` と runtime `entry_id` です。どちらも今回表示した candidate set と照合され、最初の不正値だけ correction retry を 1 回行い、それでも不正なら拒否されます。

```toml
version = "1.2.0"
```

check / release workflow では必須です。バージョン管理やマーケットプレイス公開で使います。

---

### `[plugin.author]` — 作者情報

```toml
[plugin.author]
name = "Alice"
```

任意です。Plugin Manager に表示されます。

---

### `[plugin.sdk]` — 対応 SDK バージョン

```toml
[plugin.sdk]
recommended = ">=0.1.0,<0.2.0"
supported = ">=0.1.0,<0.3.0"
```

package が対応する plugin SDK version を host に伝えます。値は Python packaging の specifier syntax です。

- `supported` — 通常サポートする範囲
- `recommended` — 最もよく検証した範囲。範囲外では warning
- `untested` — 追加で許可する範囲。該当時は warning
- `conflicts` — 他の範囲に一致していても明示的に拒否する範囲

`supported` がある場合、host は `supported` または `untested` に入らなければロードされません。不正な specifier も拒否されます。

---

### `[plugin_runtime]` — 実行方法

```toml
[plugin_runtime]
enabled = true
auto_start = true
priority = 0
timeout = 10
startup_failure = "warn"
```

- `enabled` — `false` にすると、ファイルを削除せず一時的に無効化できます
- `auto_start` — `true` なら N.E.K.O 起動時に自動開始、そうでなければパネルから手動開始します
- `priority` — optional integer runtime ordering hint
- `timeout` — startup readiness を待つ秒数。`0 < timeout <= 300` が必要で、省略時は system default
- `startup_failure` — `startup` hook failure の扱い。`warn`（default、process を残して degraded）、`fail`（startup abort）、`ignore`（log only）

---

### `[plugin.i18n]` — 多言語対応

```toml
[plugin.i18n]
default_locale = "zh-CN"
locales_dir = "i18n"
```

多言語対応が必要な場合、プラグインディレクトリに `i18n/` フォルダーを作り、ロケールファイルを置きます。

```text
i18n/
├── en.json
└── zh-CN.json
```

i18n が不要なら、このセクションは書かなくてかまいません。

---

### `[plugin.store]` — 永続ストレージ

```toml
[plugin.store]
enabled = true
```

有効にすると、コード内で `self.store` を使って、再起動後も残るデータを保存・取得できます。

ストレージが不要なら、このセクションは書かなくてかまいません。デフォルトでは無効です。

---

### `[plugin.ui]` — カスタム UI

```toml
[plugin.ui]
enabled = true

[[plugin.ui.panel]]
id = "main"
title = "Smart Notes"
entry = "ui/panel.tsx"
context = "dashboard"
permissions = ["state:read", "action:call"]

[[plugin.ui.guide]]
id = "quickstart"
title = "User Guide"
entry = "docs/guide.md"
permissions = ["state:read"]
```

Plugin Manager に独自の画面を出したい場合に使います。

- `panel` — ボタン、テーブル、フォームを持てるインタラクティブなパネルです。TSX で書きます。
- `guide` — 読み取り専用のドキュメントです。Markdown で書きます。

拡張子で表示方式が決まります。`.tsx` はインタラクティブパネル、`.md` はドキュメントとして扱われます。

UI が不要なら、このセクションは書かなくてかまいません。

---

### カスタムセクション — プラグイン固有の設定

```toml
[notes]
max_per_page = 20
auto_classify = true
```

追加の top-level section は business config として保持され、コードから読み取れます。

```python
cfg = await self.config.dump()
notes_cfg = cfg.get("notes", {})
max_per_page = notes_cfg.get("max_per_page", 20)
```

必要なだけ自由にカスタムセクションを定義できます。

---

## このプラグインのディレクトリ構造

```text
plugin/plugins/smart_notes/
├── plugin.toml              ← 上記の設定ファイル
├── __init__.py              ← プラグインコード
├── i18n/                    ← ロケールファイル（[plugin.i18n] を設定したため）
│   ├── en.json
│   └── zh-CN.json
├── ui/                      ← インタラクティブパネル（[[plugin.ui.panel]] を設定したため）
│   └── panel.tsx
├── docs/                    ← ユーザーガイド（[[plugin.ui.guide]] を設定したため）
│   └── guide.md
```

書き込み可能な状態データは、ソースやインストール済みコードのディレクトリとは分けて保存されます。

```text
<ユーザーデータルート>/plugins/smart_notes/
├── config/plugin.toml      ← 実際に使用される設定
├── data/                   ← self.data_path()
└── cache/                  ← self.cache_path()
```

必須なのは `plugin.toml` と `[plugin].entry` が指す、インポート可能な Python モジュールです。一般的には `__init__.py` を使いますが、それに限定されません。インストール済みコードは、これらの書き込み可能な状態データとは別に保存されます。
