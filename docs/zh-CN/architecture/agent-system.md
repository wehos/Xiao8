# 智能体系统

智能体系统把已完成的对话轮次转换为可选的后台任务。系统分布在两类进程中：主服务器负责聊天会话和面向用户的投递，智能体服务器负责判断请求、分派工具和跟踪任务状态。

## 运行时组件

```text
主服务器                                      智能体服务器 (:48915)
┌──────────────────────────┐                 ┌──────────────────────────────┐
│ LLMSessionManager        │                 │ DirectTaskExecutor           │
│ cross_server.py          │  分析队列        │  ├─ 统一通道判断              │
│ MainServerAgentBridge    │ ──────────────> │  ├─ 用户插件判断              │
│                          │ <────────────── │  └─ 去重 / 任务跟踪           │
│ pending_agent_callbacks  │  结果 / 事件     │                              │
└──────────────────────────┘                 │ 通道适配器                    │
                                             └──────────────────────────────┘
```

当前执行器是 `brain/task_executor.py` 中的 `DirectTaskExecutor`。旧的 Planner / Processor / Analyzer 分层已经不存在。`TaskDeduper` 仍然保留，但它用于阻止重复分派，并不是执行后的结果分析器。

智能体服务器由 `app/agent_server/__main__.py` 启动。实现拆分在 `api_runtime.py`、`api_routes.py`、`channels/`、`registry.py`、`tracker.py` 和 `results.py` 中。

## 从对话到任务

1. `main_logic/cross_server.py` 在 `turn end` 或 `session end` 时构造有界的近期消息视图，并可附带当前轮次的用户图片。
2. `publish_analyze_request_reliably()` 发送带 `event_id` 的 `analyze_request`。主服务器短暂等待 `analyze_ack`，超时后重试一次。
3. 主开关关闭时，智能体服务器会拒绝分析。它还会移除此前已取消的用户轮次，并应用近期任务去重。
4. `DirectTaskExecutor.analyze_and_execute()` 判断已启用通道。用户插件走独立的发现与两阶段入口选择；其他通道共享统一判断。
5. 被选中的通道创建 registry 任务、发送 `task_update`、执行工作并发送结构化 `task_result`。
6. 主服务器把实时任务更新转发给浏览器，并把任务结果放入对应 `LLMSessionManager` 的队列。文本会话可以立即投递回调；语音会话可能延迟到注入或热切换边界。

智能体回调不会再次触发分析，避免结果投递递归产生新任务。

## 传输映射

进程桥接使用同步 ZeroMQ socket，并在后台线程接收，因此也兼容 Windows Proactor 事件循环。

| 默认地址 | 模式 | 方向 | 用途 |
|---|---|---|---|
| `tcp://127.0.0.1:48961` | PUB / SUB | 主 → Agent | 会话和生命周期事件 |
| `tcp://127.0.0.1:48963` | PUSH / PULL | 主 → Agent | 可靠的 `analyze_request` 队列 |
| `tcp://127.0.0.1:48962` | PUSH / PULL | Agent → 主 | ACK、状态、任务更新和结果 |

端口可分别通过 `NEKO_ZMQ_SESSION_PUB_PORT`、`NEKO_ZMQ_ANALYZE_PUSH_PORT` 和 `NEKO_ZMQ_AGENT_PUSH_PORT` 覆盖。智能体 HTTP 控制服务默认监听 `127.0.0.1:48915`，嵌入式用户插件服务默认监听 `127.0.0.1:48916`。

Agent → 主服务器的结果投递没有 HTTP 降级路径。ZeroMQ 桥不可用时，事件不会送达。

## 能力状态

权威状态通过主服务器的 `/api/agent/*` 代理暴露。前端通过 `/api/agent/command` 修改状态，通过 `/api/agent/state` 刷新状态，不应把本地复选框当作权威来源。

| 状态 | 含义 |
|---|---|
| `analyzer_enabled` / UI `agent_enabled` | 分析主开关 |
| `computer_use_enabled` | 视觉驱动的桌面交互 |
| `browser_use_enabled` | 浏览器自动化 |
| `user_plugin_enabled` | 已安装用户插件执行 |
| `openclaw_enabled` | OpenClaw 独立智能体通道 |

Manager 和智能体服务器构造时这些开关均为关闭。首次真实 `greeting_check` 后可以恢复持久化的运行时意图，因此“构造时关闭”不表示每次刷新页面都会重置用户选择。

打开开关本身还不够：API 就绪检查和各通道能力探测仍可能阻止分派。OpenClaw 在启用探测期间还有独立的 readiness 状态。

## 路由规则

对于非插件通道，第一个可执行的判断结果按以下顺序胜出：

```python
_CHANNEL_PRIORITY = ["qwenpaw", "browser_use", "computer_use"]
```

`qwenpaw` 映射到 `OpenClawAdapter`。它是面向兼容接口的适配器，连接外部 QwenPaw 服务；N.E.K.O 不提供该服务进程，也不保证它具有所需的模型和工具权限。只有配置的健康检查返回 ready 后，该渠道才会成为候选。用户插件单独判断，不属于 `_CHANNEL_PRIORITY`。

QwenPaw 的 `/clear`、`/new`、`/stop` 和 `/daemon approve` 使用独立的魔法命令识别路径；普通请求仍要经过可用性检查和非插件渠道的统一评估。

用户插件路由先执行确定性的筛选（`brain/plugin_filter.py`），再由 LLM 选择插件入口，并严格校验 `plugin_id`、`entry_id` 和参数。插件入口元数据可覆盖执行超时，否则使用项目默认值。

## 并发、取消和保留

- 分析与分派串行执行，避免几乎同时发生的 turn-end 事件创建重复任务。
- Computer Use 有显式队列，同一时间只运行一个桌面控制任务。Browser 和远程 Agent 适配器各自维护活动任务保护。
- 取消时先把 registry 条目标记为 `cancelled` 并取消包装任务，再在后台启动 provider 特定的清理。迟到的 provider 结果不得覆盖该终态。
- 取消 QwenPaw 任务时只会向外部服务转发停止请求，属于尽力而为的协作式取消。本地状态为 `cancelled`，并不能证明外部服务尚未执行不可逆操作。
- 完成、失败和取消的 registry 条目保留五分钟；清理最多每分钟执行一次。
- 用户插件可以返回 `deferred: true`。任务保持 `running`，直到调用 `/api/agent/tasks/{task_id}/complete`，或一小时 deferred 超时将其标记失败。

## 纠正反馈

`POST /api/agent/tasks/{task_id}/correction` 只接受已经进入终态的 Browser Use 或 Computer Use 任务，并且只能把其中一个工具纠正为另一个。执行器会将经过裁剪和脱敏的事件写入当前配置目录的 `correction_memory.json`，最多保留 300 条。该文件既不是角色记忆，也不是插件的 `bus.memory`。

后续统一渠道评估前，系统通过轻量关键词匹配选取最多 3 条相关事件。QwenPaw、用户插件和具体插件入口 ID 目前没有同类纠正通道。

## 实现映射

| 关注点 | 当前实现 |
|---|---|
| turn-end 触发和近期上下文 | `main_logic/cross_server.py` |
| ZeroMQ 桥及 ACK/重试 | `main_logic/agent_event_bus.py` |
| 判断和路由 | `brain/task_executor.py` |
| 插件候选筛选 | `brain/plugin_filter.py` |
| Agent 生命周期和 HTTP API | `app/agent_server/api_runtime.py`、`api_routes.py` |
| 通道分派 | `app/agent_server/channels/` |
| 任务 registry 和保留 | `app/agent_server/registry.py` |
| 结果投递到聊天 | `main_logic/core/proactive.py` |

公共控制端点见[智能体 REST API](/api/rest/agent)，任务可视化见[任务 HUD 系统](/architecture/task-hud-system)。
