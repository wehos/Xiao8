# 插件系统概览

N.E.K.O. 插件系统是一个基于 Python 的插件框架，建立在**进程隔离**和**异步 IPC** 之上。平台只有两种包类型：产品功能使用 **Plugin（插件）**，外部协议桥接使用 **Adapter（适配器）**。原 **Extension（扩展）** 包类型已经移除；`PluginRouter` 仍可在普通 Plugin 内部使用。

## 架构

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

## 包类型

| 范式 | 导入来源 | 使用场景 | 运行方式 |
|------|----------|----------|----------|
| **Plugin** | `plugin.sdk.plugin` | 独立功能（搜索、提醒等） | 独立进程 |
| **Adapter** | `plugin.sdk.adapter` | 将外部协议（MCP、NoneBot）桥接到内部插件调用 | 独立进程，带网关管线 |

### 何时使用哪种范式？

- **"我想添加一个新的独立功能"** → 使用 **Plugin**
- **“我想在现有功能周围增加命令”** → 使用普通 **Plugin**；若你维护原宿主且代码很大，可在宿主内使用 `PluginRouter`
- **"我想接受 MCP/NoneBot/外部协议调用并将其路由到插件"** → 使用 **Adapter**

> 从 **Plugin** 开始。迁移原 Extension 时，把 Router 合并进所属 Plugin，或改造成独立 Plugin。

## 开始开发

先阅读[插件开发入门文档](./plugin-development)，了解插件的目录构成和完整制作流程；然后跟随[快速开始](./quick-start)动手，并在实现功能时查阅[插件能力与 SDK 参考](./sdk-reference)。

## 快速链接

- [插件开发入门文档](./plugin-development) — 先了解插件由什么组成，以及怎样完成第一个插件
- [快速开始](./quick-start) — 按步骤创建你的第一个插件
- [发布插件](/zh-CN/plugins/cli) — 上传 GitHub、提交审核并持续发布新版本
- [v0.9 迁移](./migration-v0.9) — 已删除接口与准确替代方案
- [SDK 参考](./sdk-reference) — 基类、上下文 API、Result 类型
- [装饰器](./decorators) — 所有可用的装饰器
- [Hosted UI](./hosted-ui) — 构建 TSX 面板和 Markdown 教程页
- [示例](./examples) — 完整的可运行示例
- [Adapter 与并发编程](./advanced) — Adapter 网关、异步入口与线程安全
- [LLM 工具调用](./tool-calling) — 注册插件功能给 LLM 在对话中调用
- [最佳实践](./best-practices) — 错误处理、测试、代码组织
