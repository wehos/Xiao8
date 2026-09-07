# Agent System

The Agent system turns completed conversation turns into optional background tasks. It is split across the Main Server, which owns chat sessions and user delivery, and the Agent Server, which assesses requests, dispatches tools, and tracks task state.

## Runtime components

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

The live executor is `DirectTaskExecutor` in `brain/task_executor.py`. The older Planner / Processor / Analyzer pipeline no longer exists. A `TaskDeduper` remains, but it prevents duplicate dispatches; it is not a post-execution analyzer.

The Agent Server is served by `app/agent_server/__main__.py`. Its implementation is split across `api_runtime.py`, `api_routes.py`, `channels/`, `registry.py`, `tracker.py`, and `results.py`.

## From conversation to task

1. `main_logic/cross_server.py` builds a bounded recent-message view at `turn end` or `session end`. User images for the current turn can be attached to that view.
2. `publish_analyze_request_reliably()` sends an `analyze_request` with an `event_id`. The Main Server waits briefly for `analyze_ack` and retries once on timeout.
3. The Agent Server rejects the request if the master switch is off. It also redacts previously cancelled user turns and applies recent-task deduplication.
4. `DirectTaskExecutor.analyze_and_execute()` evaluates enabled channels. User plugins use their own discovery and two-stage entry selection path; the other channels share a unified assessment.
5. The selected channel creates a registry task, emits `task_update` events, performs the work, and emits a structured `task_result`.
6. The Main Server forwards live task updates to the browser and queues task results on the matching `LLMSessionManager`. Text sessions can deliver callbacks immediately; voice sessions may defer them until an injection or hot-swap boundary.

Agent callbacks are deliberately not analyzed again, preventing result delivery from recursively spawning another task.

## Transport map

The process bridge uses synchronous ZeroMQ sockets on background receive threads so it also works with the Windows Proactor event loop.

| Default address | Pattern | Direction | Purpose |
|---|---|---|---|
| `tcp://127.0.0.1:48961` | PUB / SUB | Main → Agent | Session and lifecycle events |
| `tcp://127.0.0.1:48963` | PUSH / PULL | Main → Agent | Reliable `analyze_request` queue |
| `tcp://127.0.0.1:48962` | PUSH / PULL | Agent → Main | ACKs, status, task updates, and results |

The ports can be overridden with `NEKO_ZMQ_SESSION_PUB_PORT`, `NEKO_ZMQ_ANALYZE_PUSH_PORT`, and `NEKO_ZMQ_AGENT_PUSH_PORT`. Agent HTTP control defaults to `127.0.0.1:48915`; the embedded user-plugin service defaults to `127.0.0.1:48916`.

There is no HTTP fallback for Agent → Main result delivery. If the ZeroMQ bridge is unavailable, the event is not delivered.

## Capability state

The authoritative state is exposed through Main Server proxies under `/api/agent/*`. The frontend changes state through `/api/agent/command` and refreshes it through `/api/agent/state`; it should not treat local checkbox state as authoritative.

| State | Meaning |
|---|---|
| `analyzer_enabled` / UI `agent_enabled` | Master assessment switch |
| `computer_use_enabled` | Vision-guided desktop interaction |
| `browser_use_enabled` | Browser automation |
| `user_plugin_enabled` | Installed user-plugin execution |
| `openclaw_enabled` | OpenClaw standalone-agent channel |

Managers and the Agent Server initialize these switches off. Persisted runtime intent can be restored after the first real `greeting_check`, so “off at construction” does not mean every new page load resets the user's choice.

Enabling a switch is not enough on its own: API readiness and channel capability probes can still block dispatch. OpenClaw also exposes a separate readiness state while its enable probe is pending.

## Routing rules

For non-plugin channels, the first executable result in this order wins:

```python
_CHANNEL_PRIORITY = ["qwenpaw", "browser_use", "computer_use"]
```

`qwenpaw` maps to `OpenClawAdapter`, a compatibility-facing adapter for an external QwenPaw service. N.E.K.O does not ship that service process or guarantee its model and tool permissions. The channel becomes a candidate only after the configured health check reports ready. User plugins are assessed separately and are not an item in `_CHANNEL_PRIORITY`.

QwenPaw commands `/clear`, `/new`, `/stop`, and `/daemon approve` use a dedicated magic-command classification path. Ordinary requests still go through availability checks and the shared non-plugin assessment.

User-plugin routing first performs deterministic filtering (`brain/plugin_filter.py`), then asks the LLM to select a plugin entry with validated `plugin_id`, `entry_id`, and arguments. Plugin execution timeouts come from entry metadata when present, with the project default as fallback.

## Concurrency, cancellation, and retention

- Analyze-and-dispatch work is serialized to prevent near-simultaneous turn-end events from creating duplicate tasks.
- Computer Use has an explicit queue and runs one desktop-control task at a time. Browser and remote-agent adapters maintain their own active-task guards.
- Cancelling a task first marks the registry entry `cancelled` and cancels its wrapper task, then starts provider-specific teardown in the background. Late provider results must not overwrite that terminal state.
- QwenPaw cancellation forwards a stop request to the external service and is cooperative and best effort. A local `cancelled` state does not prove that the service had not already performed an irreversible action.
- Completed, failed, and cancelled registry entries are retained for five minutes and cleaned at most once per minute.
- A user plugin may return `deferred: true`. The task remains `running` until `/api/agent/tasks/{task_id}/complete` is called or the one-hour deferred timeout marks it failed.

## Correction feedback

`POST /api/agent/tasks/{task_id}/correction` accepts feedback only after a Browser Use or Computer Use task reaches a terminal state, and only to correct one of those tools to the other. The executor writes redacted and truncated events to `correction_memory.json` in the active configuration directory, retaining at most 300 entries. This file is separate from character memory and plugin `bus.memory`.

Before later unified channel assessments, lightweight keyword matching selects at most three relevant events. There is currently no equivalent correction path for QwenPaw, user plugins, or a specific plugin entry ID.

## Implementation map

| Concern | Current implementation |
|---|---|
| Turn-end trigger and recent context | `main_logic/cross_server.py` |
| ZeroMQ bridge and ACK/retry | `main_logic/agent_event_bus.py` |
| Assessment and routing | `brain/task_executor.py` |
| Plugin candidate filtering | `brain/plugin_filter.py` |
| Agent lifecycle and HTTP API | `app/agent_server/api_runtime.py`, `api_routes.py` |
| Channel dispatch | `app/agent_server/channels/` |
| Task registry and retention | `app/agent_server/registry.py` |
| Result delivery to chat | `main_logic/core/proactive.py` |

See [Agent REST API](/api/rest/agent) for the public control endpoints and [Task HUD System](/architecture/task-hud-system) for task visualization.
