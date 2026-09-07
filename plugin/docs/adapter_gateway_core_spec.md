# Adapter Gateway Core 规范（简版）

> **状态：历史设计快照。** 本文记录 Adapter Gateway Core 落地前的分层与迁移设想，
> 不再作为当前实现计划或公共 SDK 合同。当前契约以
> `plugin/sdk/adapter/`、`plugin/sdk/hosted-ui/index.d.ts` 和公共插件文档为准。

## 1. 目标

Adapter 作为 NEKO 插件系统的网关层，负责：

1. 连接外部应用协议（首期 MCP）
2. 把外部请求规范化后路由到 NEKO plugin entry
3. 支持动态注册/卸载 entry（与现有 PluginRouter 兼容）
4. 为 Adapter 提供可扩展的 UI 承载（iframe 区域由后续阶段接入）

本规范强调：

- 不破坏现有插件功能
- 渐进迁移，允许双栈
- 统一日志与错误模型
- 禁止 `except ...: pass`
- 新代码禁止 `Any` 类型注解

---

## 2. 分层设计

### 2.1 Transport Layer

职责：

- 建立/维护外部连接
- 收取外部请求消息（envelope）
- 回写响应消息

约束：

- 不直接调用 plugin runtime
- 不持有路由规则

### 2.2 Gateway Core

职责：

- normalize（外部消息 -> 统一请求）
- authorize（策略校验）
- route（路由决策）
- invoke（调用插件入口）
- serialize（统一响应）

约束：

- 不关心具体协议实现细节
- 所有错误必须结构化

### 2.3 Entry Registrar

职责：

- 将外部能力映射为动态 entry
- 幂等同步（同源重复同步可覆盖）
- 支持按 source 卸载

### 2.4 UI Surface（后续阶段）

职责：

- 注册 Adapter 的 iframe 专属区域
- 约束前后端消息桥协议（trace_id/request_id/source）

---

## 3. 统一数据模型（Core 内）

- `ExternalEnvelope`：协议输入原始包
- `GatewayRequest`：统一请求模型（`target_plugin_id`、`target_entry_id`、`params`）
- `RouteDecision`：`self | plugin | broadcast | drop`
- `GatewayError`：结构化错误（`code/message/details/retryable`）
- `GatewayResponse`：统一响应模型

命名规范：

- 目标插件：`target_plugin_id`
- 调用参数：`params`
- 全链路追踪：`trace_id`

---

## 4. 与现有功能兼容策略

### 4.1 运行时加载

Adapter 继续走统一加载主链路：依赖检查 -> 冲突处理 -> adapter-specific 启动。

### 4.2 动态 Entry

优先复用现有 `PluginRouter` 动态入口机制，不替换现有 `mcp_adapter` 行为。

### 4.3 双栈迁移

- 旧实现：`mcp_adapter` 当前逻辑
- 新实现：`Gateway Core` 抽象实现
- 通过配置开关逐步切换

---

## 5. 日志与错误规范

1. 日志：优先使用 NEKO plugin 上下文 logger（`ctx.logger`）
2. 错误：统一结构化，不返回裸字符串
3. 异常处理：
   - 禁止 `except Exception: pass`
   - 可以捕获并记录，然后转为结构化错误

---

## 6. 渐进实施里程碑

### M1（当前）

- 落地 `gateway_models/contracts/core` 骨架
- 不改变现有业务行为

### M2

- 把 MCP 请求处理路径接入 Gateway Core
- 保留原逻辑兜底

### M3

- 实现 EntryRegistrar 幂等同步
- 增加 source 级卸载

### M4

- 引入 iframe 专属区域与消息桥协议
- 加入权限与来源校验

---

## 7. 代码规范（本模块）

- 类型注解禁止 `Any`
- 不使用 `except ...: pass`
- 复杂异常路径必须打日志
- 新增接口尽量基于 `Protocol`
- 对现有代码仅做最小侵入改造
