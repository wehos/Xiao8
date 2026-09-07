# エージェントシステム

エージェントシステムは、完了した会話ターンを任意のバックグラウンドタスクへ変換します。チャットセッションとユーザーへの配信を担う Main Server と、要求判定・ツール実行・タスク状態管理を担う Agent Server に分かれています。

## ランタイム構成

```text
Main Server                                  Agent Server (:48915)
┌──────────────────────────┐                 ┌──────────────────────────────┐
│ LLMSessionManager        │                 │ DirectTaskExecutor           │
│ cross_server.py          │  analyze queue  │  ├─ unified channel assess  │
│ MainServerAgentBridge    │ ──────────────> │  ├─ user-plugin assess      │
│                          │ <────────────── │  └─ dedupe / task tracking  │
│ pending_agent_callbacks  │  result/events  │                              │
└──────────────────────────┘                 │ channel adapters             │
                                             └──────────────────────────────┘
```

現在の実行器は `brain/task_executor.py` の `DirectTaskExecutor` です。旧 Planner / Processor / Analyzer パイプラインは存在しません。`TaskDeduper` は残っていますが、重複ディスパッチの防止用であり、実行後の Analyzer ではありません。

Agent Server は `app/agent_server/__main__.py` から起動されます。実装は `api_runtime.py`、`api_routes.py`、`channels/`、`registry.py`、`tracker.py`、`results.py` に分割されています。

## 会話からタスクまで

1. `main_logic/cross_server.py` が `turn end` または `session end` でサイズ制限付きの直近メッセージを構築し、当該ターンのユーザー画像を添付できます。
2. `publish_analyze_request_reliably()` が `event_id` 付き `analyze_request` を送信します。Main Server は短時間 `analyze_ack` を待ち、タイムアウト時に一度再試行します。
3. マスタースイッチがオフなら Agent Server は判定を行いません。以前キャンセルされたユーザーターンの除去と直近タスクの重複判定も行います。
4. `DirectTaskExecutor.analyze_and_execute()` が有効なチャネルを判定します。ユーザープラグインは独立した検出・2段階エントリ選択を使い、その他は統一判定を使います。
5. 選択されたチャネルは registry タスクを作成し、`task_update` を送信し、実行後に構造化 `task_result` を送信します。
6. Main Server はライブ更新をブラウザへ転送し、結果を該当する `LLMSessionManager` にキューします。テキストセッションでは即時配信でき、音声セッションでは注入またはホットスワップ境界まで遅延する場合があります。

エージェント結果のコールバックは再解析されないため、結果配信が再帰的に別タスクを生成することはありません。

## トランスポート

プロセス間ブリッジは同期 ZeroMQ socket とバックグラウンド受信スレッドを使用し、Windows Proactor イベントループにも対応します。

| 既定アドレス | パターン | 方向 | 用途 |
|---|---|---|---|
| `tcp://127.0.0.1:48961` | PUB / SUB | Main → Agent | セッション・ライフサイクルイベント |
| `tcp://127.0.0.1:48963` | PUSH / PULL | Main → Agent | 信頼性付き `analyze_request` キュー |
| `tcp://127.0.0.1:48962` | PUSH / PULL | Agent → Main | ACK、状態、タスク更新、結果 |

各ポートは `NEKO_ZMQ_SESSION_PUB_PORT`、`NEKO_ZMQ_ANALYZE_PUSH_PORT`、`NEKO_ZMQ_AGENT_PUSH_PORT` で上書きできます。Agent HTTP 制御は既定で `127.0.0.1:48915`、組み込みユーザープラグインサービスは `127.0.0.1:48916` です。

Agent → Main の結果配信には HTTP フォールバックがありません。ZeroMQ ブリッジが利用できない場合、イベントは配信されません。

## 機能状態

権威ある状態は Main Server の `/api/agent/*` プロキシから取得します。フロントエンドは `/api/agent/command` で変更し、`/api/agent/state` で更新します。ローカルのチェックボックス状態を正として扱ってはいけません。

| 状態 | 意味 |
|---|---|
| `analyzer_enabled` / UI `agent_enabled` | 判定のマスタースイッチ |
| `computer_use_enabled` | 視覚ベースのデスクトップ操作 |
| `browser_use_enabled` | ブラウザ自動操作 |
| `user_plugin_enabled` | インストール済みユーザープラグイン実行 |
| `openclaw_enabled` | OpenClaw スタンドアロンチャネル |

Manager と Agent Server は構築時にすべてオフです。ただし最初の実ユーザー `greeting_check` 後に永続化された runtime intent を復元できるため、ページ更新のたびに設定がリセットされるわけではありません。

スイッチだけでなく API readiness とチャネル capability probe も実行可否に影響します。OpenClaw は有効化 probe 中に独立した readiness 状態を持ちます。

## ルーティング規則

非プラグインチャネルでは、実行可能な最初の結果が次の順序で選ばれます。

```python
_CHANNEL_PRIORITY = ["qwenpaw", "browser_use", "computer_use"]
```

`qwenpaw` は `OpenClawAdapter` に対応します。これは外部 QwenPaw サービスへ接続する互換アダプターです。N.E.K.O はそのサービスプロセスを同梱せず、必要なモデルやツールの権限も保証しません。設定されたヘルスチェックが ready を返した場合にだけ、このチャネルが候補になります。ユーザープラグインは別経路で判定され、`_CHANNEL_PRIORITY` には含まれません。

QwenPaw の `/clear`、`/new`、`/stop`、`/daemon approve` は専用のマジックコマンド判定経路を使います。通常のリクエストは、引き続き可用性チェックと非プラグインチャネルの統一判定を通ります。

ユーザープラグインは最初に `brain/plugin_filter.py` の決定的フィルターを通り、その後 LLM がエントリを選択します。`plugin_id`、`entry_id`、引数は厳密に検証されます。実行タイムアウトはエントリメタデータがあればそれを使い、なければプロジェクト既定値です。

## 並行性・キャンセル・保持

- 判定とディスパッチは直列化され、同時刻の turn-end が重複タスクを作るのを防ぎます。
- Computer Use は明示的なキューを持ち、デスクトップ操作は一度に1件です。Browser とリモート Agent も独自の active-task guard を持ちます。
- キャンセルは registry を先に `cancelled` にし、wrapper task を止めてから provider 固有の teardown をバックグラウンドで開始します。遅延結果は終端状態を上書きできません。
- QwenPaw のキャンセルは外部サービスへ停止要求を転送する協調的なベストエフォート処理です。ローカル状態が `cancelled` でも、外部サービスが不可逆な操作をまだ行っていないことは保証されません。
- 完了・失敗・キャンセル済み registry は5分保持され、クリーンアップは最大でも1分に1回です。
- ユーザープラグインは `deferred: true` を返せます。`/api/agent/tasks/{task_id}/complete` が呼ばれるか、1時間の timeout で失敗するまで `running` のままです。

## 修正フィードバック

`POST /api/agent/tasks/{task_id}/correction` は、終端状態になった Browser Use または Computer Use のタスクだけを対象とし、一方のツールからもう一方への修正だけを受け付けます。実行器は切り詰め・秘匿化したイベントを現在の設定ディレクトリにある `correction_memory.json` へ書き込み、最大 300 件を保持します。このファイルはキャラクターメモリでもプラグインの `bus.memory` でもありません。

以後の統一チャネル判定前に、軽量なキーワード照合で最大 3 件の関連イベントを選びます。QwenPaw、ユーザープラグイン、特定のプラグインエントリ ID には同等の修正経路がありません。

## 実装マップ

| 関心事 | 現在の実装 |
|---|---|
| turn-end と直近コンテキスト | `main_logic/cross_server.py` |
| ZeroMQ ブリッジと ACK/再試行 | `main_logic/agent_event_bus.py` |
| 判定とルーティング | `brain/task_executor.py` |
| プラグイン候補フィルター | `brain/plugin_filter.py` |
| Agent lifecycle / HTTP API | `app/agent_server/api_runtime.py`、`api_routes.py` |
| チャネル実行 | `app/agent_server/channels/` |
| タスク registry と保持 | `app/agent_server/registry.py` |
| チャットへの結果配信 | `main_logic/core/proactive.py` |

公開制御 API は [Agent REST API](/api/rest/agent)、表示側は [Task HUD System](/architecture/task-hud-system) を参照してください。
