# Plugin System Overview

The N.E.K.O. plugin system is a Python-based plugin framework built on **process isolation** and **async IPC**. It has two package types: **Plugin** for product features and **Adapter** for external protocol bridges. The former **Extension** package type has been removed; `PluginRouter` remains available inside a normal Plugin.

## Architecture

```
┌────────────────────────────────────────────────────┐
│              Main Process (Host)                   │
│  ┌──────────────────────────────────────────────┐  │
│  │   Plugin Host (core/)                        │  │
│  │   - Plugin lifecycle management              │  │
│  │   - Bus system (memory, events, messages)    │  │
│  │   - ZMQ IPC transport                        │  │
│  └──────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────┐  │
│  │   Plugin Server (server/)                    │  │
│  │   - HTTP API endpoints (FastAPI)             │  │
│  │   - Plugin registry                          │  │
│  │   - Message queue                            │  │
│  └──────────────────────────────────────────────┘  │
└────────────────────┬───────────────────────────────┘
                     │ ZMQ IPC
      ┌──────────────┼──────────────┐
      ▼              ▼              ▼
  Plugin A       Plugin B       Adapter D
  (process)      (process)      (process)
```

## Package types

| Paradigm | Import from | Use case | How it runs |
|----------|------------|----------|-------------|
| **Plugin** | `plugin.sdk.plugin` | Independent features (search, reminders, etc.) | Separate process |
| **Adapter** | `plugin.sdk.adapter` | Bridge external protocols (MCP, NoneBot) to internal plugin calls | Separate process with gateway pipeline |

### When to use which?

- **"I want to add a new standalone feature"** → use **Plugin**
- **"I want to add commands around an existing feature"** → use a normal **Plugin**, or add a `PluginRouter` inside the existing host when you own it
- **"I want to accept MCP/NoneBot/external protocol calls and route them to plugins"** → use **Adapter**

> Start with **Plugin**. Migrate a former Extension by merging its Router into the owning Plugin or converting it into a standalone Plugin.

## Start developing

Read [Getting Started with Plugin Development](./plugin-development) for the package structure and complete workflow. Then follow the [Quick Start](./quick-start) and use the [Plugin Capabilities and SDK Reference](./sdk-reference) while implementing features.

## Quick Links

- [Getting Started with Plugin Development](./plugin-development) — Understand what a plugin contains and how to build one
- [Quick Start](./quick-start) — Create your first plugin step by step
- [Publish a Plugin](/plugins/cli) — Upload to GitHub, submit for review, and publish new versions
- [v0.9 Migration](./migration-v0.9) — Removed surfaces and exact replacements
- [SDK Reference](./sdk-reference) — Base classes, context API, Result types
- [Decorators](./decorators) — All available decorators
- [Hosted UI](./hosted-ui) — Build TSX panels and Markdown guides
- [Examples](./examples) — Complete working examples
- [Adapters & Concurrency](./advanced) — Adapter gateways, async entries, and thread safety
- [LLM Tool Calling](./tool-calling) — Register plugin functions for the LLM to invoke during conversations
- [Best Practices](./best-practices) — Error handling, testing, code organization
