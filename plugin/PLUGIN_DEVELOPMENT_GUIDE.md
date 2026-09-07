# N.E.K.O 插件系统开发指南

> SDK v2 完整开发教程。平台支持 Plugin / Adapter 两种插件包；Router 用于普通 Plugin 内部组合。
>
> 本轮接口收敛的完整替代关系见 [`docs/zh-CN/plugins/migration-v0.9.md`](../docs/zh-CN/plugins/migration-v0.9.md)。

## 目录

- [第一章：概述](#第一章概述)
- [第二章：快速开始](#第二章快速开始)
- [第三章：SDK 核心功能](#第三章sdk-核心功能)
- [第四章：装饰器详解](#第四章装饰器详解)
- [第五章：上下文与运行时](#第五章上下文与运行时)
- [第六章：完整示例](#第六章完整示例)
- [第七章：Router 组合](#第七章router-组合)
- [第八章：Adapter 适配器开发](#第八章adapter-适配器开发)
- [第九章：高级主题](#第九章高级主题)
- [第十章：最佳实践](#第十章最佳实践)
- [第十一章：常见问题](#第十一章常见问题)
- [第十二章：API 参考](#第十二章api-参考)

---

## 第一章：概述

### 1.1 什么是 N.E.K.O 插件系统？

N.E.K.O 插件系统是一个基于 Python 的插件框架，允许开发者创建可组合的功能模块。Plugin 与 Adapter 运行在独立进程中，通过 ZMQ IPC 与主系统交互。

### 1.2 包类型

| 范式 | 导入路径 | 用途 | 运行方式 |
|------|---------|------|---------|
| **Plugin** | `plugin.sdk.plugin` | 独立功能（搜索、提醒等） | 独立进程 |
| **Adapter** | `plugin.sdk.adapter` | 对接外部协议（MCP、NoneBot 等） | 独立进程 + 网关管线 |

**如何选择？**

- **「我想添加一个新的独立功能」** → 用 **Plugin**（99% 的开发者只需要这个）
- **「我想在现有功能周围增加命令」** → 使用普通 **Plugin**；若你维护原宿主且代码很大，可使用 `PluginRouter`
- **「我想把 MCP/NoneBot 等外部协议请求转发给插件」** → 用 **Adapter**

### 1.3 核心特性

- **进程隔离**：Plugin 与 Adapter 独立运行
- **异步运行时入口**：运行时入口使用 `async def`；同步辅助函数保持为内部实现
- **Result 类型**：`Ok`/`Err` 类型安全的错误处理（替代异常流）
- **Hook 系统**：`@before_entry`, `@after_entry`, `@around_entry`, `@replace_entry` 面向切面编程
- **跨插件调用**：`self.plugins.call_entry("other_plugin:entry_id")` 插件间通信
- **系统信息**：`self.system_info` 查询宿主元数据
- **持久化存储**：`PluginStore` 键值对持久化
- **Bus 系统**：`self.bus` 读取宿主状态并监听 `add` / `del` / `change`；不提供发布接口
- **动态入口**：运行时注册/注销入口点
- **静态 UI**：从插件目录提供 Web UI
- **生命周期**：`startup`, `shutdown`, `reload`, `freeze`, `unfreeze`, `config_change`
- **定时任务**：`@timer_interval` 周期执行
- **消息处理**：`@message` 响应主系统消息
- **音乐播放**：`push_message` + `parts=[{type:'ui_action', action:'media_*'}]` 跨进程音频控制

### 1.4 系统架构

```text
┌────────────────────────────────────────────────────┐
│              主进程 (Host)                          │
│  ┌──────────────────────────────────────────────┐  │
│  │   Plugin Host (core/)                        │  │
│  │   - 插件生命周期管理                          │  │
│  │   - Bus 系统 (memory, events, messages)      │  │
│  │   - ZMQ IPC 传输                             │  │
│  └──────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────┐  │
│  │   Plugin Server (server/)                    │  │
│  │   - HTTP API 端点 (FastAPI)                  │  │
│  │   - 插件注册表                                │  │
│  │   - 消息队列                                  │  │
│  └──────────────────────────────────────────────┘  │
└────────────────────┬───────────────────────────────┘
                     │ ZMQ IPC
      ┌──────────────┼──────────────┐
      ▼              ▼              ▼
  Plugin A       Plugin B       Adapter D
  (独立进程)     (独立进程)      (独立进程)
```

### 1.5 SDK 包结构

```text
plugin/sdk/
├── plugin/         ← 标准插件开发入口（99% 的开发者只需要这个）
└── adapter/        ← 适配器开发入口（对接外部协议）
```

> `plugin/sdk/shared/` 是内部实现细节，不应被开发者直接导入。

### 1.6 代码目录与状态目录

```text
plugin/plugins/
└── my_plugin/
    ├── __init__.py      # 插件代码（入口点）
    ├── plugin.toml      # 插件清单与默认值
    ├── config.example.toml # 可选：运行时配置模板
    └── static/          # 可选：Web UI 文件

<用户数据根目录>/plugins/my_plugin/
├── config/plugin.toml   # 当前用户的运行时配置
├── data/                # 持久数据
└── cache/               # 可重新生成的缓存
```

通过安装包安装时，可执行代码与状态目录分开存放。

---

## 第二章：快速开始

### 2.1 创建插件目录

```bash
mkdir -p plugin/plugins/hello_world
```

### 2.2 创建 `plugin.toml`

```toml
[plugin]
id = "hello_world"
name = "Hello World Plugin"
description = "一个简单的示例插件"
version = "1.0.0"
entry = "plugin.plugins.hello_world:HelloWorldPlugin"

[plugin.sdk]
recommended = ">=0.1.0,<0.2.0"
supported = ">=0.1.0,<0.3.0"
```

#### 配置字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `id` | 是 | 插件唯一标识符 |
| `name` | 否 | 显示名称 |
| `type` | 否 | `plugin`（默认）或 `adapter`；其他历史类型已移除 |
| `description` | 否 | 插件描述 |
| `version` | 否 | 插件版本 |
| `entry` | 是 | 入口点：`模块路径:类名` |

#### SDK 版本字段

| 字段 | 说明 |
|------|------|
| `recommended` | 推荐的 SDK 版本范围 |
| `supported` | 最低支持范围（不满足时拒绝加载） |
| `untested` | 允许但加载时会警告 |
| `conflicts` | 拒绝的版本范围 |

### 2.3 创建 `__init__.py`

```python
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    Ok, Err, SdkError,
)
from typing import Any

@neko_plugin
class HelloWorldPlugin(NekoPluginBase):
    """Hello World 插件示例"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.counter = 0

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self.logger.info("HelloWorldPlugin 已启动！")
        return Ok({"status": "ready"})

    @lifecycle(id="shutdown")
    def on_shutdown(self, **_):
        self.logger.info("HelloWorldPlugin 已停止！")
        return Ok({"status": "stopped"})

    @plugin_entry(
        id="greet",
        name="问候",
        description="返回一条问候消息",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要问候的名字",
                    "default": "World"
                }
            }
        }
    )
    async def greet(self, name: str = "World", **_):
        self.counter += 1
        message = f"Hello, {name}! (第 {self.counter} 次调用)"
        self.logger.info(f"问候: {message}")
        return Ok({"message": message, "count": self.counter})
```

### 2.4 关键要点

- **`@neko_plugin`** — 必须的类装饰器，将类注册为插件
- **`NekoPluginBase`** — 所有插件必须继承的基类
- **`@plugin_entry`** — 定义外部可调用的入口点
- **`@lifecycle`** — 处理生命周期事件（`startup`, `shutdown`, `reload`）
- **`Ok(...)` / `Err(...)`** — 返回 Result 类型，类型安全的错误处理
- **`**_`** — 仅在入口确实要接收额外宿主字段时使用；显式签名会过滤未声明字段

### 2.5 测试

启动插件服务器后，通过 HTTP 调用插件：

```bash
curl -X POST http://localhost:48916/plugin/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "plugin_id": "hello_world",
    "entry_id": "greet",
    "args": {"name": "N.E.K.O"}
  }'
```

---

## 第三章：SDK 核心功能

### 3.1 导入方式

所有插件开发 API 从 `plugin.sdk.plugin` 导入：

```python
from plugin.sdk.plugin import (
    # 基类
    NekoPluginBase, PluginMeta,
    # 装饰器
    neko_plugin, plugin_entry, lifecycle, timer_interval, message, on_event,
    custom_event, hook, before_entry, after_entry, around_entry, replace_entry,
    # Result 类型
    Ok, Err, Result, unwrap, unwrap_or,
    # 运行时工具
    Plugins, PluginRouter, PluginConfig, PluginStore,
    SystemInfo,
    # 错误
    SdkError, TransportError,
    # 日志
    get_plugin_logger,
)
```

### 3.2 NekoPluginBase 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `self.ctx` | `PluginContext` | 运行时上下文（宿主注入） |
| `self.plugin_id` | `str` | 插件唯一标识符 |
| `self.plugin_dir` | `Path` | 插件安装目录（代码、Manifest 和静态资源） |
| `self.config_dir` | `Path` | `self.plugin_dir` 的兼容别名 |
| `self.storage_dir` | `Path` | 分配给插件的用户存储根目录 |
| `self.runtime_config_path` | `Path` | 外部运行配置文件路径 |
| `self.metadata` | `dict` | 来自 `plugin.toml` 的元数据 |
| `self.bus` | `SdkBusContext` | 宿主状态的 read/watch 门面；没有 publish/emit API |
| `self.plugins` | `Plugins` | 跨插件调用工具 |
| `self.system_info` | `SystemInfo` | 宿主系统元数据 |

### 3.3 NekoPluginBase 方法

#### `report_status(status: dict) -> None`

向宿主报告插件状态：

```python
self.report_status({
    "status": "processing",
    "progress": 50,
    "message": "处理中..."
})
```

<a id="push-message-v2"></a>
#### `push_message(**kwargs) -> PushMessageResult`

`push_message` 是插件 → 主系统的**唯一**消息推送入口。两条独立的轴
决定下游行为，配合 `parts` 列表承载 OpenAI 风格的多模态内容。完整
schema 见
[plugin/sdk/shared/core/push_message_schema.py](sdk/shared/core/push_message_schema.py)。

```python
ctx.push_message(
    visibility=[],                  # ["chat"] / ["hud"] / ["chat","hud"] / []
    ai_behavior="respond",          # "respond" / "read" / "blind"
    parts=[                         # 有序的内容 parts（chat 保序；模型侧见下方限制）
        {"type": "text",  "text": "看这个"},
        {"type": "image", "data": img_bytes, "mime": "image/png"},
        # 以下三种当前只在 schema 中占位，AI 注入链路 warn-drop，
        # 详见下文「当前实现限制」：
        # {"type": "image", "url": "https://example.com/cat.png"},
        # {"type": "audio", "data": audio_bytes, "mime": "audio/mpeg"},
        # {"type": "video", "url":  "https://example.com/clip.mp4"},
        {"type": "ui_action",
         "action": "media_play_url",
         "url": "https://example.com/song.mp3",
         "media_type": "audio"},
    ],
    source="my_feature",
    target_lanlan="灵",             # 可选，路由到指定 session
    metadata={...},
    priority=0,                     # 数字越大优先级越高
)
```

返回值是本地提交结果：

```python
result = ctx.push_message(
    visibility=[],
    ai_behavior="respond",
    parts=[{"type": "text", "text": "请回应这条事件"}],
)
if not result["submitted"]:
    # 可以保留本地任务，稍后由插件自己的策略决定是否重试。
    # 不要把消息正文写入日志。
    logger.warning("message submission failed: %s", result.get("reason"))
```

`submitted=True` 只表示 SDK 已把 payload 交给权威本地提交路径，并接管后续提交
责任；它不表示宿主已经消费、AI 已生成回复或音频已经播放。
`submitted=False` 会携带稳定的 `reason`：`backpressure`、`transport_error`、
`transport_unavailable` 或 `payload_too_large`。前三个描述的是传输当时的状况
（拥塞 / 发送失败 / 没有可用通道），换个时机原样重发是有意义的。
`payload_too_large` 是另一类：它由 SDK 在**发送之前**本地量出来——整条 payload
打包后超过了 `MESSAGE_PLANE_PAYLOAD_MAX_BYTES`（判据见下面的「大小限制」），
所以原样重试必然还是同一个结果，唯一的出路是把这条 push 变小。inline 图片改用
`ctx.images.upload()` 换成 URL part；`audio` / `video` 目前没有对应的上传接口，
只能自己压小（更短的片段、更低的码率或分辨率），或者自己托管后用 `url=` 代替
`data=`。结果不会暴露内部 transport 名称，也不会回显消息正文或异常内容。拒绝结果还会携带兼容旧调用方的 `ok=False`；新代码应以
`submitted` 为正式判据。调用方可以保留本地状态，但重试和去重仍由具体插件决定。

##### 两条轴的语义

* **`visibility`** — plugin 的 parts 直接渲染给用户的目标列表（**与 AI 无关**）：

  | 值 | 含义 |
  |---|---|
  | `"chat"` | parts 在 chat 框里**原文**渲染（plugin 的话直接当 chat 气泡） |
  | `"hud"`  | parts 在 agent UI / HUD 通知面板显示 |
  | `[]`     | 用户**不直接**看到 parts；如果 `ai_behavior="respond"`，用户看到的是 AI 的回复 |

  可同时多选（如 `["chat", "hud"]`）。

* **`ai_behavior`** — LLM 怎么处理 parts：

  | 值 | LLM 上下文 | 触发回复 turn | 何时被提及 |
  |---|---|---|---|
  | `"respond"`（默认） | ✅ | ✅ 立即起 turn | AI 立刻接茬 |
  | `"read"`  | ✅ | ❌ 不打断 | 下次用户开口时 AI 自然提及 |
  | `"blind"` | ❌ | ❌ | AI 看不到 |

##### `parts` part 类型

每个 part 是个 dict，必须有 `type` discriminator：

| `type` | 字段 | 用途 |
|---|---|---|
| `text` | `text: str` | 纯文本 |
| `image` | `data: bytes` + `mime` 或 `url` + `mime` | 图（inline 或远端） |
| `audio` | 同 image | 音频（schema 占位，**当前 AI 注入链路尚未消费**，会 warn-drop） |
| `video` | 同 image | 视频（schema 占位，**当前 AI 注入链路尚未消费**，会 warn-drop） |
| `ui_action` | `action: str` + 各 action 的字段 | 前端 UI 副作用 |

inline `data: bytes` 由 SDK 自动 base64 编码后随 payload 传出。

> **当前实现限制**（v0.9 移除前会逐步补齐）：
> - `ai_behavior in ("respond","read")` 时，inline `image` parts 和
>   `ctx.images.upload()` 返回的本地临时 URL 都能进入 LLM 上下文（最终走
>   `session.stream_image(base64)`）。任意外部 URL 仍会被拒绝，避免把远端抓取
>   引入 agent event 投递路径。单条消息最多向模型注入 8 张、合计 8 MiB 图片；
>   超出的 image parts 仍可按 `visibility` 显示，但不会进入模型上下文。
> - 上面的 8 张 / 8 MiB 是**单次 push** 的上限。文字模式下 `ai_behavior="read"`
>   的图不是立刻发给模型，而是先暂存、等用户下次开口时一起送出，所以**一个回合**
>   里可能攒着好几次 push 的图。暂存按来源分开计额，互不侵蚀：
>
>   | 来源 | 张数 | 字节 |
>   | --- | --- | --- |
>   | 用户自己的截图 / 摄像头帧 | 5 | 16 MiB |
>   | 插件 `read` 图片 | 3 | 8 MiB |
>   | 主动搭话遗留的屏幕截图 | 1（独立单槽，带 TTL） | — |
>
>   超额时裁掉的**永远是同一来源里最旧的那张**——插件推得再猛也拿不走用户的帧，
>   反之亦然。所以一个回合最多可能带 9 张图，比单次 push 的 8 张略多，这是有意的：
>   共用一个总额度就必须在两个来源之间挑一个牺牲，而那没有正确答案。
> - `parts` 的顺序在 **chat 渲染**里是保留的（文字和图按你给的次序出现）。但
>   **进模型的那条路不保序**：图片会被拆出来先注入，文字合成一段随后给出。所以
>   别依赖「说明 A、图 A、说明 B、图 B」这种交错来让模型把说明和图对应起来——
>   要对应就把说明写进同一段文字里（例如「第一张是…，第二张是…」）。
> - `visibility=["chat"]` 可显示 image parts；HUD 通知目前只渲染 text part，
>   不显示 image part。
> - `audio` / `video` 当前没有对应的 realtime 注入通道（`stream_audio` 是 PCM 实时
>   麦克风专用，video 完全没有 API），都会 warn-drop。这两种 type 现阶段只
>   推荐配合 `ai_behavior="blind"` + `ui_action` 走纯前端展示。
>
> **大小限制**：inline part 通过 message_plane 走 ZMQ，整条 payload 上限是
> `MESSAGE_PLANE_PAYLOAD_MAX_BYTES` = 524288（512 **KiB** = 512*1024，不是
> 十进制的 512 KB）。这个数字量的是**打包后**的信封，不是原图字节数——inline
> 图片以 base64 放在 `parts[].binary_base64` 里，是原始字节的 4/3（+33%）。
> 所以一张 inline 图的原始字节上限是 512 KiB × 3/4 = **约 384 KiB**，再减掉
> 几百字节的信封开销和同一条消息里的 text part。实测：一张 256 KiB 的图加一句
> 短文字打包出来是 341.8 KiB，离上限还有约 170 KiB 余量；恰好卡满的原始图片大小
> 是 383.6 KiB。
>
> 这个「实际上限」曾经低得多，值得说清楚为什么变了。wire envelope 以前为了照顾
> 还没迁到 v2 的下游消费者，把同一张 inline 图带**两遍**：一份 base64 在
> `parts[].binary_base64`，一份原始 bytes 在 legacy 的 `binary_data` 字段，加起来
> 约是原图的 2.34 倍，于是当时 256 KiB 的上限实际只兜得住**约 110 KiB** 的图。
> 现在 `_build_wire_payload`（`plugin/core/context.py`）只在「调用方同时传了
> `parts=` 和 `binary_data=`」这一种形状下才填 legacy 字段——那时那些 bytes 不在
> 任何 part 里，是唯一的载体；普通的 inline 图片只走 base64 那一遍。双份没了，
> 上限又从 256 KiB 提到 512 KiB，两件事合起来才让「文档承诺 256 KiB 的图能过」
> 这句话第一次成立。
>
> 超限现在**在本地就被拦下**：`push_message()` 在把 payload 交给 message_plane 的
> ZMQ 通道之前先打包量一次，超了直接返回 `submitted=False` +
> `reason="payload_too_large"`，那条消息一个字节都不会上线，日志里还会写明是哪个
> part（`image` / `audio` / `video`）吃掉了预算。这一侧和 host 侧
> （`plugin/message_plane/ingest_server.py`）读的是同一个常量，所以两边不会漂移；
> host 侧的检查也还在，作为最后一道兜底。
>
> 但这没有改变 `submitted=True` 的含义：它仍然只表示「已交给传输」，不表示 host
> 收下了——宿主背压、进程重启之类仍然可能让消息静默消失。变的只是**超限**这一类
> 丢弃，它从「插件侧完全察觉不到」变成了一个同步的返回值。
>
> 这道闸挂在**每一条**提交出口上，不是只挂在 ZMQ 主路：批量快路径、同步路径，以及
> ZMQ 不可用时退下去的 legacy 控制面队列，三条都会先量一遍整条 payload 再决定。
> 校验的对象也是整条 payload 而不只是内联图片——一条超大的纯文本或 metadata 同样
> 会被拒，因为 host 那边量的就是整条 msgpack。唯一例外是
> `NEKO_MESSAGE_PLANE_VALIDATE_PAYLOAD_BYTES` 被显式关掉的部署，那种配置下 host
> 自己也不量，超限消息会一路走到底。
>
> 超限影响的是**整条 push**，不是「图掉了、文字还在」：同一条消息里的 text part 和
> `ui_action` 一起被拒。1080p 截图别指望 inline 走：先压成 JPEG q70 或
> 256x256 PNG，再大就用下面的上传接口。
>
> 较大图片使用独立的临时图片上传 interface；它会在线程池中规范化为最长边不超过
> 2048 的 JPEG，并通过独立 media transport 上传，不占用 `push_message` 的
> 512 KiB payload：
>
> ```python
> image_part = await ctx.images.upload(image_bytes, mime="image/png")
> ctx.push_message(
>     visibility=["chat"],
>     ai_behavior="respond",
>     parts=[{"type": "text", "text": "看看这张图"}, image_part],
> )
> ```
>
> `images.upload()` 只准备当前运行期可用的临时资源；它不会显示图片、不会写入
> 模型上下文，也不会触发回复。投递语义仍完全由 `push_message()` 控制。
> 请在 plugin entry、timer、message 或 custom event handler 中调用。lifecycle
> handler（`startup` / `freeze` / `unfreeze` / `shutdown` / `config_change`）
> 执行时不处理这类 request/response 上传，调用会立即抛出 `RuntimeError`，
> 而不是等待 timeout。
>
> **`upload()` 的硬失败**——下面每一条都是**抛异常**，不是静默降级成一张小图，
> 所以喂用户提供的图片时必须自己 `try`：
>
> | 关卡 | 常量 / 判据 | 越界结果 |
> |---|---|---|
> | 源图字节 | `MAX_SOURCE_IMAGE_BYTES` = 33554432（32 MiB） | `ValueError` |
> | 源图像素 | `MAX_SOURCE_IMAGE_PIXELS` = 16777216（16 MP，宽 x 高） | `ValueError` |
> | 归一化后字节 | `MAX_UPLOADED_IMAGE_BYTES` = 8388608（8 MiB） | `ValueError` |
> | 解码槽等待 | 全进程只有 2 个解码槽，排队时间计入你给的 `timeout`（默认 3s，上限 30s） | `TimeoutError` |
> | 输出最长边 | `MAX_IMAGE_EDGE` = 2048 | 不报错，等比缩小 |
>
> 归一化后字节那条容易被漏掉：源图过了 32 MiB / 16 MP 两关，重编码出来的 JPEG
> 仍可能超过 8 MiB（噪点多的大图压不动），这时抛的异常在**解码之后**，你已经
> 付过 CPU 了。此外 downlink 尚未就绪、transport 不回包同样是 `TimeoutError`；
> 空 bytes 是 `ValueError`，非 bytes 是 `TypeError`。
>
> **动图会被拍平**：`normalize_image_to_jpeg` 只取第 0 帧，输出恒为单帧 JPEG，
> 既不报错也不打日志（实测 4 帧 GIF 进、1 帧 JPEG 出）。`mime=` 参数只是给调用
> 方自己标注用的，真实格式由 Pillow 探测、输出永远是 JPEG。要动效请配合
> `ui_action` 让前端自己播。
>
> **传得更清晰买的是 chat 显示效果，不是模型精度**：进模型的那条路另有一套判据，
> 而且它是**无条件**的，不是超了预算才触发。
>
> 每一张要进模型的图都会先被重编码到模型档位：最长宽 `MODEL_IMAGE_MAX_WIDTH`
> （1280）、最高 `COMPRESS_TARGET_HEIGHT`（720）、JPEG q80，见
> `utils/screenshot_utils.py` 的 `normalize_image_for_model`。已经在档位内的图原样
> 通过、不会被反复重编码（它是幂等的，否则图片在会话历史里多存活几轮就会代际劣化）。
> 所以按 2048 长边上传的图，到模型眼前必定已经是 720p 档——传得更大只增加这一次
> 重编码的开销，不会让模型看得更准。
>
> 归一化之后，如果一个回合的图片**总字节**仍超过 `TURN_ATTACHED_IMAGE_MAX_TOTAL_BYTES`
> （8 MiB），才会再启动降级阶梯：先抽样（只留头/中/尾三张），再对活下来的重压一次，
> 两步都不够才从最旧的开始丢，并且只有真丢了整张图才会提示用户。
>
> chat 显示的那一份不走上面任何一步，保留你上传的分辨率。

##### 常见组合

```python
# 1. 让 AI 转述（最常用）：plugin 文本 → LLM 上下文 → AI 立即回复
ctx.push_message(parts=[{"type": "text", "text": "用户给你打赏了 100 块"}])

# 2. 安静通知 AI：下次用户说话时 AI 自然提及
ctx.push_message(
    visibility=[],
    ai_behavior="read",
    parts=[{"type": "text", "text": "B站直播间又有新弹幕"}],
)

# 3. 只在 HUD 通知用户，不打扰 AI
ctx.push_message(
    visibility=["hud"],
    ai_behavior="blind",
    parts=[{"type": "text", "text": "插件 X 已启动"}],
)

# 4. plugin 直接在 chat 渲染卡片，AI 不知情（取代旧 music_play_url）
ctx.push_message(
    visibility=["chat"],
    ai_behavior="blind",
    parts=[{
        "type": "ui_action",
        "action": "media_play_url",
        "url": "https://music.example.com/song.mp3",
        "media_type": "audio",
        "name": "Test Song",
        "artist": "Bot",
    }],
)

# 5. 文字 + 图同时给 AI
ctx.push_message(
    parts=[
        {"type": "text",  "text": "看这张图"},
        {"type": "image", "data": img_bytes, "mime": "image/png"},
    ],
)

# 6. 注册音乐域名白名单（取代旧 register_music_domains）
ctx.push_message(
    ai_behavior="blind",
    parts=[{
        "type": "ui_action",
        "action": "media_allowlist_add",
        "domains": ["music.my-cdn.com"],
    }],
)
```

##### 已废弃字段（v0.9 移除）

| 旧字段 | 新写法 |
|---|---|
| `message_type="proactive_notification"` | 默认行为，去掉即可 |
| `message_type="music_play_url"` | `parts=[{"type":"ui_action","action":"media_play_url",...}]` + `visibility=["chat"], ai_behavior="blind"` |
| `message_type="music_allowlist_add"` | `parts=[{"type":"ui_action","action":"media_allowlist_add","domains":[...]}]` + `ai_behavior="blind"` |
| `content="X"` | `parts=[{"type":"text","text":"X"}]` |
| `binary_data=bytes, mime` | 按 MIME 选择 `image` / `audio` / `video`，使用 `parts=[{"type":...,"data":bytes,"mime":...}]` |
| `binary_url=URL, mime` | 按 MIME 选择 `image` / `audio` / `video`，使用 `parts=[{"type":...,"url":URL,"mime":...}]` |
| `delivery="proactive"` / `reply=True` | 默认即是 `visibility=[], ai_behavior="respond"` |
| `delivery="passive"` | `visibility=[], ai_behavior="read"` |
| `delivery="silent"` / `reply=False` | `visibility=["hud"], ai_behavior="blind"` |
| `description="X"` | `metadata={"description": "X"}` |
| `unsafe=True` | drop |
| `fast_mode=True` | drop；v2 使用标准宿主投递路径，旧批处理/背压优化不会保留，高频生产者需重新压测 |

旧字段的有效使用仍可兼容，但会触发 `DeprecationWarning` 提示在 v0.9 移除；
`unsafe=False`、`fast_mode=False` 与值为 `None` 的旧字段不会触发 warning。
完整 changelog：[`docs/changelog/`](../docs/changelog/)。

> **`register_music_domains()` SDK helper 已删除**。请直接 push 一条带
> `ui_action: media_allowlist_add` 的消息（见上面例 6）。

#### `data_path(*parts) -> Path`

获取插件 `data/` 目录下的路径：

```python
db_path = self.data_path("records.db")
# → <storage-root>/plugins/<plugin_id>/data/records.db
```

#### `cache_path(*parts) -> Path`

获取插件可清理缓存目录下的路径：

```python
preview_path = self.cache_path("preview.png")
# → <storage-root>/plugins/<plugin_id>/cache/preview.png
```

#### `register_dynamic_entry(entry_id, handler, ...) -> bool`

运行时动态注册入口点（不通过装饰器）：

```python
self.register_dynamic_entry(
    entry_id="dynamic_greet",
    handler=lambda name="World", **_: Ok({"msg": f"Hi {name}"}),
    name="动态问候",
    description="动态注册的问候入口",
)
```

#### `unregister_dynamic_entry(entry_id) -> bool`

移除动态注册的入口点。

#### `list_entries(include_disabled=False) -> list[dict]`

列出所有入口点（静态 + 动态）。

#### `enable_entry(entry_id) / disable_entry(entry_id) -> bool`

启用或禁用动态入口点。

#### `register_static_ui(directory, *, index_file, cache_control) -> bool`

注册插件的静态 Web UI 目录：

```python
self.register_static_ui("static")  # 提供 <plugin_dir>/static/index.html
```

#### `include_router(router, *, prefix) -> None`

挂载 `PluginRouter`，用于组织大型或按功能拆分的普通 Plugin。

#### `run_update(**kwargs) -> object` (async)

在长时间运行操作期间发送更新。

#### `export_push(**kwargs) -> object` (async)

向宿主推送导出数据。

#### `finish(**kwargs) -> Any` (async)

通知宿主任务完成。

#### 回复控制（`finish()` 的 `delivery`）

`finish()` 仍接受 `delivery` 参数控制任务结果如何到达主 AI（三档枚举，默认 `"proactive"`）：

| `delivery` | 是否进 LLM 上下文 | 是否立即起 turn 播报 | 前端 HUD/通知 |
|---|---|---|---|
| `"proactive"`（默认） | ✅ | ✅ 立即起 turn | ✅ |
| `"passive"` | ✅ 写入上下文 | ❌ 不打断；下次用户发言时由 AI 自然提及 | ✅ |
| `"silent"` | ❌ AI 不知情 | ❌ | ✅（只剩 task_update） |

```python
# 正常：角色立即播报结果
return await self.finish(data={"summary": "天气晴朗"})

# 安静通知：进上下文不打断；下次用户开口时 AI 顺嘴提一下
return await self.finish(data={"summary": "番茄钟到点了"}, delivery="passive")

# 完全静默：AI 不知情；前端只通过 task_update 看到任务终态
return await self.finish(data={"summary": "..."}, delivery="silent")
```

> **`push_message()` 不再用 `delivery`**——改成 `visibility` + `ai_behavior`
> 两条独立轴（见上面 `push_message(**kwargs) -> PushMessageResult` 节）。旧 `delivery=`
> / `reply=` 仍能用但会 emit DeprecationWarning，v0.9 移除。

#### "任务汇报"vs"事件回应"：声明结果语义

主 AI 在收到通知时，会被套上一层外层 prompt。**外层 prompt 的措辞分两类**：

| 你调用 | 宿主分类 | AI 收到的外层 prompt 大意 |
|---|---|---|
| `await self.finish(...)`（默认） | `task_result` | "来自{你的插件}的任务已完成，请向主人**汇报**..." |
| 查询/即时回执 entry 声明 `result_kind="event"` | `event` | "来自{你的插件}的**新消息**，请按内容**回应**主人..." |
| `self.push_message(...)` | `event` | "来自{你的插件}的**新消息**，请按内容**回应**主人..." |

设计原因——"任务汇报"和"事件流"是两种完全不同的语义：
- 任务汇报：插件被调用后跑完，AI 应该告诉主人"我做了什么、结果怎样"
- 事件流：插件持续监听外部事件（弹幕、IM 消息、定时器），AI 应该**回应这个事件本身**，
  而不是叙述成"我刚刚处理了一下…"

旧版本曾经把所有 `ai_behavior="respond"` 的 push 也套上"任务已完成"模板，导致
弹幕插件让兰兰用"我刚才处理了一下弹幕"这种汇报型口吻回观众——这是 bug，已修复。

查询、状态读取或“已开始”一类即时回执应显式降级为事件语义。可静态声明：

```python
@plugin_entry(
    id="service_status",
    metadata={"result_kind": "event", "expires_in_s": 30},
)
```

也可由某次运行结果覆盖静态声明：

```python
return await self.finish(
    data={"status": "running"},
    meta={"agent": {"result_kind": "event", "expires_in_s": 10}},
)
```

解析优先级为“运行时 `meta.agent` > entry 静态 `metadata` > 默认
`task_result`”。`expires_in_s` 只对 `event` 结果生效；过期回执不会再进入主 AI。
宿主只允许成功的 `user_plugin task_result` 降级为 `event`，不能把
`proactive_message` 反向伪装成任务完成。

> `push_message()` 中，`visibility` 控制 parts 向哪些前端目标展示，`ai_behavior`
> 控制模型是否处理以及何时触发 turn；`delivery` 只是兼容旧调用方的已弃用参数。
> `result_kind` 与这些投递轴正交，只决定外层 prompt 的措辞。

#### 写"角色感知文本"：`{MASTER_NAME}` / `{LANLAN_NAME}` 占位符

插件通过 `finish()` 的 `data.summary` / `data.detail`、`push_message()` 的
`parts[*].text` 等渠道把字符串塞进对话 LLM 上下文（或在 `direct_reply` 时直接进
TTS / 聊天气泡）。这些字符串里**不要硬编码** `"用户"` / `"user"` / `"master"`
/ `"主人"` —— 会导致两个问题：

- **口吻别扭**：主 AI 看到"向用户汇报…"会原样照念"向用户继续叙述"，而不是用
  实际的 `master_name`，听感生硬、出戏。
- **多角色失真**：每个 `LLMSessionManager` 有自己的 `lanlan_name`；一个插件
  广播给多个角色时，硬编码会让所有角色用同一份措辞。

##### 占位符契约

在 `summary` / `detail` / `parts[*].text` 里直接写：

| 占位符 | 替换成 |
|---|---|
| `{MASTER_NAME}` | 当前会话的 `master_name`（用户起的名字） |
| `{LANLAN_NAME}` | 当前会话的角色名 |

宿主在 LLM 注入点（`main_logic.core.apply_role_placeholders`）按目标 session 展
开。**不能在插件侧自己替换**——`push_message` 的 visibility 过滤是宿主端的，
插件不知道这条消息最终落到哪个 session，也就拿不到正确的 name。

##### 例子

```python
# ✅ 推荐：让宿主按 session 展开
await self.finish(data={
    "summary": "立即基于最新画面向 {MASTER_NAME} 叙述刚才发生的事",
})

self.push_message(parts=[{
    "type": "text",
    "text": "{MASTER_NAME} 刚刚发了一条弹幕：『...』",
}], ai_behavior="respond")

# ❌ 不推荐：硬编码，口吻泛化 + 多角色失真
await self.finish(data={"summary": "立即向用户叙述..."})
self.push_message(parts=[{"type": "text", "text": "主人发了一条弹幕..."}], ...)
```

##### 实现细节

- **替换语义是 `str.replace`，不是 `str.format`**：`detail` 里嵌 JSON 片段 /
  代码 / 含 `{` 的用户原文都不会触发 `KeyError`。
- **空 name 时占位符保持字面量**：宿主拿不到 name（极少见的初始化阶段）时，
  `{MASTER_NAME}` 留在原文，不会替换成空串造成"向 ... 汇报"这种破句。
- **拼写固定**：用 **`{MASTER_NAME}` / `{LANLAN_NAME}`**（大写、下划线、单层
  花括号）。`prompts_chara.py` 里用的也是这套；不要写 `{master_name}` /
  `{master}` / `{MASTER}`——那些是宿主内部模板的 `.format(...)` 占位符，不跨
  插件边界。

##### 适用范围

| 渠道 | 是否展开 |
|---|---|
| `finish(data={"summary": ..., "detail": ...})` | ✅ |
| `push_message(parts=[{"type": "text", ...}])` 进 LLM 上下文 | ✅ |
| `task_result` + `direct_reply=True`（绕过 LLM 直接 TTS） | ✅ |
| `push_message(visibility=["chat"], ai_behavior="blind")` 直进聊天气泡 | ✅ |
| `push_message(visibility=["hud"])` HUD toast 文本 | ✅ |
| 静态描述字段（`plugin.toml` 的 `description`、入口的 `name` 等） | ❌ 不展开（这些是给开发者 / UI 看的，不进对话渠道） |

#### LLM 结果字段过滤

通过 `llm_result_fields` 控制主 LLM 能看到结果中的哪些字段：

```python
# 静态入口：在装饰器中声明
@plugin_entry(llm_result_fields=["summary"])
async def search(self, query: str):
    return await self.finish(data={"summary": "3条结果", "raw_results": [...]})

# 动态入口：在注册时声明
self.register_dynamic_entry(
    entry_id="my-tool",
    handler=handler,
    llm_result_fields=["summary"],
)
```

#### 旧 `message_type` 的迁移

> ⚠️ **`message_type` 已废弃，v0.9 移除**。新代码请用 `parts` 列表 +
> `visibility` / `ai_behavior` 描述消息——对照表见上面 push_message
> 节的「已废弃字段」。如果你需要扩展 push_message 的能力，请直接在
> `parts` 加新的 `type`，**不要**新增 `message_type` 值。完整迁移清单见
> [`docs/zh-CN/plugins/migration-v0.9.md`](../docs/zh-CN/plugins/migration-v0.9.md)。

### 3.4 Result 类型：Ok / Err

SDK 使用 Rust 风格的 Result 类型进行错误处理，替代传统异常：

```python
from plugin.sdk.plugin import Ok, Err, unwrap, unwrap_or, SdkError

# 返回成功
return Ok({"data": result})

# 返回错误
return Err(SdkError("出错了"))

# 消费结果
result = await self.plugins.call_entry("other:do_stuff")
if isinstance(result, Ok):
    data = result.value
else:
    error = result.error
    self.logger.error(f"调用失败: {error}")

# 辅助函数
value = unwrap(result)           # Err 时抛出异常
value = unwrap_or(result, None)  # Err 时返回默认值
```

### 3.5 跨插件调用 (Plugins)

通过 `self.plugins` 访问：

```python
# 列出所有插件
result = await self.plugins.list()

# 只列出已启用的插件
result = await self.plugins.list(enabled=True)

# 获取插件 ID 列表
result = await self.plugins.list_ids()

# 检查插件是否存在
result = await self.plugins.exists("other_plugin")

# 调用另一个插件的入口点
result = await self.plugins.call_entry("other_plugin:do_work", {"key": "value"})

# 调用并确保返回 JSON 对象
result = await self.plugins.call_entry_json("other_plugin:get_data")

# 要求插件必须存在且已启用
result = await self.plugins.require_enabled("dependency_plugin")
```

所有方法返回 `Result` 类型 — 使用前用 `isinstance(result, Ok)` 检查。

### 3.6 持久化存储 (PluginStore)

```python
from plugin.sdk.plugin import Err, unwrap_or

saved = await self.store.set("key", {"count": 42})
if isinstance(saved, Err):
    return saved

value = unwrap_or(await self.store.get("key"), None)  # → {"count": 42}
```

### 3.7 消息类型

`push_message` 没有 `message_type` 字段了——内容形态由 `parts` 各元素的
`type` 描述（`text` / `image` / `audio` / `video` / `ui_action`），下游
路由由 `visibility` + `ai_behavior` 决定。详见上面 [3.3 节
`push_message`](#push-message-v2)。

`message_type=...` 仍可作为旧 API 兼容形参传入，但每次会触发
`DeprecationWarning`，v0.9 移除。

### 3.8 优先级

| 范围 | 级别 | 用途 |
|------|------|------|
| 0-2 | 低 | 信息性消息 |
| 3-5 | 中 | 一般通知 |
| 6-8 | 高 | 重要通知 |
| 9-10 | 紧急 | 需要立即处理 |


### 3.9 音乐播放安全接口 (Music UI)

N.E.K.O 默认只允许来自 `music.163.com` 等已知安全域名的音频播放。如果你的插件引入了新的音频来源，必须在播放前通过以下接口将其域名加入白名单。

#### 3.9.1 前端扩展 API (JavaScript)

如果你的插件包含 Web UI（通过 `register_static_ui` 提供），你可以通过浏览器全局对象 `window.MusicPluginAPI` 注册域名。

**场景**：插件页面动态加载音频。

| 方法 | 参数 | 说明 |
|------|------|------|
| `getAllowlist()` | 无 | 获取当前所有合法的域名/IP 列表 |
| `addAllowlist(input)` | `string \| Array<string>` | 添加新域名。支持 URL 自动提取，自动去重 |

**推荐接入模式（异步安全）**：
由于插件页面可能通过 `<iframe>` 嵌入且加载时机不确定，建议监听 `music-ui-ready` 事件：

```javascript
window.addEventListener('music-ui-ready', () => {
    const api = window.MusicPluginAPI || (window.parent && window.parent.MusicPluginAPI);
    if (api) {
        api.addAllowlist(['my-music-cdn.com', 'https://safe-storage.org/stream/']);
    }
}, { once: true });
```

#### 3.9.2 后端插件 API (Python SDK)

后端插件（例如 AI 自动搜索并播放音乐）通过 `push_message` 推一条
`ui_action: media_allowlist_add` 给前端：

```python
@plugin_entry(id="play_external")
async def play_external(self, url: str, **_):
    # 先加白域名，确保播放不会被 Music UI 拦截
    self.push_message(
        ai_behavior="blind",
        parts=[{
            "type": "ui_action",
            "action": "media_allowlist_add",
            "domains": [url],
        }],
    )

    # 然后让前端直接开播
    self.push_message(
        visibility=["chat"],
        ai_behavior="blind",
        parts=[{
            "type": "ui_action",
            "action": "media_play_url",
            "url": url,
            "media_type": "audio",
        }],
    )
    return Ok({"status": "playing", "url": url})
```

> 旧 SDK helper `self.register_music_domains(...)` 已**删除**。

---

## 第四章：装饰器详解

### 4.1 @neko_plugin

标记类为 N.E.K.O 插件，**所有插件类必须使用**：

```python
@neko_plugin
class MyPlugin(NekoPluginBase):
    pass
```

### 4.2 @plugin_entry

定义外部可调用的入口点：

```python
@plugin_entry(
    id="process",                  # 入口点 ID（省略时自动使用方法名）
    name="处理数据",                # 显示名称
    description="处理输入数据",      # 描述
    input_schema={...},            # JSON Schema 验证
    params=MyParamsModel,          # 或 Pydantic 模型（自动生成 schema）
    kind="action",                 # "action" | "service" | "hook" | "custom"
    auto_start=False,              # 元数据标记；普通入口不会在加载时执行
    persist=False,                 # 覆盖调用后的状态快照策略
    model_validate=True,           # 启用 Pydantic 验证
    timeout=30.0,                  # 执行超时（秒）
    llm_result_fields=["text"],    # LLM 消费的字段
    llm_result_model=MyResult,     # 结果的 Pydantic 模型
    metadata={"category": "data"}  # 额外元数据
)
async def process(self, data: str, **_):
    return Ok({"result": data})
```

#### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `id` | `str` | 方法名 | 入口点唯一标识符 |
| `name` | `str` | `None` | 显示名称 |
| `description` | `str` | `""` | 描述 |
| `input_schema` | `dict` | `None` | 输入的 JSON Schema |
| `params` | `type` | `None` | Pydantic 模型（自动生成 `input_schema`） |
| `kind` | `str` | `"action"` | 入口类型 |
| `auto_start` | `bool` | `False` | 元数据标记；普通入口不会在加载时自动执行 |
| `persist` | `bool` | `None` | 覆盖本次入口调用后的状态快照策略 |
| `model_validate` | `bool` | `True` | 启用 Pydantic 验证 |
| `timeout` | `float` | `None` | 执行超时（秒） |
| `llm_result_fields` | `list[str]` | `None` | LLM 结果提取字段 |
| `llm_result_model` | `type` | `None` | 结果的 Pydantic 模型 |
| `metadata` | `dict` | `None` | 额外元数据 |

> 提示：只有在入口确实要接收额外宿主字段时才使用 `**_`。宿主会为显式签名过滤未声明字段。

### 4.3 @lifecycle

定义生命周期事件处理器：

```python
@lifecycle(id="startup")
async def on_startup(self, **_):
    return Ok({"status": "ready"})

@lifecycle(id="shutdown")
def on_shutdown(self, **_):
    return Ok({"status": "stopped"})

@lifecycle(id="reload")
def on_reload(self, **_):
    return Ok({"status": "reloaded"})
```

有效的生命周期 ID：`startup`, `shutdown`, `reload`, `freeze`, `unfreeze`, `config_change`

### 4.4 @timer_interval

定义周期执行的定时任务：

```python
@timer_interval(
    id="cleanup",
    seconds=3600,           # 每小时执行一次
    name="清理任务",
    auto_start=True          # 自动启动（默认 True）
)
async def cleanup(self, **_):
    # 在独立线程中运行
    return Ok({"cleaned": True})
```

> 注意：定时任务在独立线程中运行。异常会被记录但不会停止定时器。

### 4.5 @message

定义来自主系统的消息处理器：

```python
@message(
    id="handle_chat",
    source="chat",           # 按消息来源过滤
    auto_start=True
)
async def handle_chat(self, text: str, sender: str, **_):
    return Ok({"handled": True})
```

### 4.6 @on_event

通用事件处理器：

```python
@on_event(
    event_type="custom_event",
    id="my_handler",
    kind="hook"
)
async def custom_handler(self, event_data: str, **_):
    return Ok({"processed": True})
```

### 4.7 @custom_event

带触发方式控制的事件处理器：

```python
@custom_event(
    event_type="data_refresh",
    id="refresh_handler",
    trigger_method="message",
    auto_start=False
)
async def on_refresh(self, source: str, **_):
    return Ok({"refreshed": True})
```

### 4.8 Hook 装饰器（AOP 面向切面）

Hook 装饰器提供面向切面编程能力，可以拦截入口点的执行。

#### @before_entry — 前置钩子

```python
@before_entry(target="process", priority=0)
def validate_input(self, *, args, entry_id, **_):
    if not args.get("data"):
        return Err(SdkError("data 是必填的"))
    # 返回 None 继续执行，返回 Err 中止
```

#### @after_entry — 后置钩子

```python
@after_entry(target="process", priority=0)
def log_result(self, *, result, entry_id, **_):
    self.logger.info(f"入口 {entry_id} 返回: {result}")
    # 返回 None 保留原结果，返回新值替换
```

#### @around_entry — 环绕钩子

```python
@around_entry(target="process", priority=0)
async def timing_wrapper(self, *, proceed, args, **_):
    import time
    start = time.time()
    result = await proceed(**args)
    elapsed = time.time() - start
    self.logger.info(f"耗时 {elapsed:.2f}s")
    return result
```

#### @replace_entry — 替换钩子

```python
@replace_entry(target="old_entry", priority=0)
async def new_implementation(self, **kwargs):
    return Ok({"replaced": True})
```

#### Hook 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target` | `str` | `"*"` | 目标入口 ID（`"*"` = 所有入口） |
| `priority` | `int` | `0` | 执行顺序（越小越先） |
| `condition` | `str` | `None` | 可选条件表达式 |

### 4.9 命名空间风格：`plugin.*`

更简洁的替代语法：

```python
from plugin.sdk.plugin import plugin

@plugin.entry(id="greet", description="打招呼")
async def greet(self, name: str = "World", **_):
    return Ok({"message": f"Hello, {name}!"})

@plugin.lifecycle(id="startup")
async def on_startup(self, **_):
    return Ok({"status": "ready"})

@plugin.hook(target="greet", timing="before")
def validate(self, *, args, **_):
    pass

@plugin.timer(id="heartbeat", seconds=60)
async def heartbeat(self, **_):
    return Ok({"alive": True})
```

---

## 第五章：上下文与运行时

### 5.1 PluginContext (ctx)

`ctx` 对象在构造时由宿主注入：

| 属性 | 类型 | 说明 |
|------|------|------|
| `ctx.plugin_id` | `str` | 插件标识符 |
| `ctx.config_path` | `Path` | `plugin.toml` 的路径 |
| `ctx.logger` | `Logger` | 日志实例 |
| `ctx.bus` | `SdkBusContext` | 宿主状态的 read/watch 门面 |
| `ctx.metadata` | `dict` | 插件元数据 |

### 5.2 Bus 读取与监听

`self.bus` 不是发布/订阅总线，没有 `emit()` 或 `on()`。五个命名空间都可读取；
只有 `messages`、`events`、`lifecycle` 支持 `watch()`，`conversations` 与
`memory` 是只读快照。异步入口中先 `await get()`，再组合结构化过滤、排序与限量：

```python
events = await self.bus.events.get(plugin_id=self.plugin_id, max_count=50)
recent = events.filter(priority_min=1).sort(by="timestamp", reverse=True).limit(20)

watcher = recent.watch(self.ctx)

@watcher.subscribe(on="add")  # 仅支持 add / del / change
def on_added(delta):
    for event in delta.added:
        self.logger.info(f"new event: {event.type}")

watcher.start()
```

可调用形式 `filter(predicate)`、`where(predicate)` 与 `sort(key=callable)`
只处理已经物化的本地快照，不能被 `watch()` 重放。监听链必须使用上例中
可重放的结构化 `filter(field=value, ...)` 与 `sort(by=...)`。

最近记忆记录使用 `await self.bus.memory.get(bucket_id="default", limit=20)`。
`self.ctx.query_memory(...)` 只是已弃用的兼容占位调用，不提供语义召回；旧的
高层 `self.memory` / SDK `MemoryClient` 已删除。

### 5.3 PluginConfig

结构化配置，支持多环境 Profile：

```python
from plugin.sdk.plugin import PluginConfig

config = PluginConfig(self.ctx)
timeout = await config.get("timeout", default=30)
```

---

## 第六章：完整示例

### 6.1 带 Result 类型的基础插件

```python
from typing import Any
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    Ok, Err, SdkError,
)

@neko_plugin
class GreeterPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.greet_count = 0

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        self.logger.info("GreeterPlugin 就绪")
        return Ok({"status": "ready"})

    @plugin_entry(
        id="greet",
        name="问候",
        description="根据名字打招呼",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "default": "World"}
            }
        }
    )
    async def greet(self, name: str = "World", **_):
        if not name.strip():
            return Err(SdkError("名字不能为空"))

        self.greet_count += 1
        return Ok({
            "message": f"Hello, {name}!",
            "total_greets": self.greet_count,
        })
```

### 6.2 异步 API 客户端 + 跨插件调用

```python
import aiohttp
from typing import Any, Optional
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    Ok, Err, SdkError, unwrap_or,
)

@neko_plugin
class APIClientPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.session: Optional[aiohttp.ClientSession] = None

    @lifecycle(id="startup")
    async def startup(self, **_):
        self.session = aiohttp.ClientSession()
        return Ok({"status": "ready"})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        if self.session:
            await self.session.close()
        return Ok({"status": "stopped"})

    @plugin_entry(id="fetch")
    async def fetch(self, endpoint: str, method: str = "GET", **_):
        try:
            async with self.session.request(method, endpoint) as response:
                data = await response.json()
                return Ok({"status": response.status, "data": data})
        except Exception as e:
            return Err(SdkError(f"请求失败: {e}"))

    @plugin_entry(id="fetch_with_cache")
    async def fetch_with_cache(self, endpoint: str, **_):
        # 跨插件调用：先查缓存插件
        cached = await self.plugins.call_entry("cache_plugin:get", {"key": endpoint})
        cached_value = unwrap_or(cached, None)
        if cached_value and cached_value.get("hit"):
            return Ok(cached_value["data"])

        result = await self.fetch(endpoint=endpoint)
        if isinstance(result, Ok):
            await self.plugins.call_entry("cache_plugin:set", {"key": endpoint, "value": result.value})
        return result
```

### 6.3 带 Hook 和定时器的插件

```python
import time
from typing import Any
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry, lifecycle,
    timer_interval, before_entry, after_entry,
    Ok, Err, SdkError,
)

@neko_plugin
class MonitoredPlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self.call_stats: dict[str, int] = {}

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        return Ok({"status": "ready"})

    @before_entry(target="*")
    def count_calls(self, *, entry_id, **_):
        """统计每个入口点的调用次数"""
        self.call_stats[entry_id] = self.call_stats.get(entry_id, 0) + 1

    @after_entry(target="*")
    def log_results(self, *, entry_id, result, **_):
        """记录每个入口点的返回结果"""
        self.logger.info(f"[{entry_id}] result={result}")

    @plugin_entry(id="process", description="处理数据")
    async def process(self, data: str, **_):
        return Ok({"processed": data.upper()})

    @plugin_entry(id="stats", description="获取调用统计")
    async def stats(self, **_):
        return Ok({"stats": dict(self.call_stats)})

    @timer_interval(id="health_check", seconds=300, auto_start=True)
    async def health_check(self, **_):
        self.report_status({
            "status": "healthy",
            "total_calls": sum(self.call_stats.values()),
        })
        return Ok({"healthy": True})
```

### 6.4 带持久化存储的插件

```python
from typing import Any
from plugin.sdk.plugin import (
    NekoPluginBase, neko_plugin, plugin_entry,
    Ok, Err, SdkError,
)

@neko_plugin
class NotesPlugin(NekoPluginBase):
    @plugin_entry(id="save_note")
    async def save_note(self, title: str, content: str, **_):
        saved = await self.store.set(
            f"note:{title}", {"title": title, "content": content}
        )
        if isinstance(saved, Err):
            return saved
        return Ok({"saved": title})

    @plugin_entry(id="get_note")
    async def get_note(self, title: str, **_):
        stored = await self.store.get(f"note:{title}")
        if isinstance(stored, Err):
            return stored
        note = stored.value
        if note is None:
            return Err(SdkError(f"笔记未找到: {title}"))
        return Ok(note)
```

---

## 第七章：Router 组合

`PluginRouter` 用于拆分普通 Plugin 内部的入口，不是一种独立插件类型。把 Router 放在所属 Plugin 的源码树中，并在 `NekoPluginBase` 实例上显式挂载：

```python
from plugin.sdk.plugin import NekoPluginBase, PluginRouter, plugin_entry, Ok


class ExtraRouter(PluginRouter):
    @plugin_entry(id="extra_command", description="额外命令")
    async def extra_command(self, param: str = "", **_):
        return Ok({"param": param})


class MyPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.include_router(ExtraRouter(name="extra"))
```

原 Extension 包必须将 Router 合并进所属 Plugin，或改造成独立的普通 Plugin。`type = "extension"`、`[plugin.host]` 和 `plugin.sdk.extension` 均已移除，不提供兼容层。

---

## 第八章：Adapter 适配器开发

### 8.1 什么是 Adapter？

Adapter 将外部协议（MCP、NoneBot 等）的请求翻译成内部插件调用。它实现了**网关管线 (Gateway Pipeline)** 模式。

### 8.2 何时使用 Adapter？

- 想通过 MCP（Model Context Protocol）暴露 N.E.K.O 插件
- 想接收 NoneBot 消息并路由到插件
- 想桥接任何外部协议到插件系统

### 8.3 网关管线架构

```
外部请求 → Normalizer → PolicyEngine → RouteEngine → PluginInvoker → ResponseSerializer → 外部响应
```

| 阶段 | 职责 |
|------|------|
| **Normalizer** | 将外部协议格式转换为 `GatewayRequest` |
| **PolicyEngine** | 访问控制、速率限制、验证 |
| **RouteEngine** | 决定调用哪个插件/入口 |
| **PluginInvoker** | 执行实际的插件调用 |
| **ResponseSerializer** | 将结果转换回外部协议格式 |

### 8.4 创建 Adapter

```python
from plugin.sdk.plugin import neko_plugin, plugin_entry, lifecycle, Ok, Err, SdkError
from plugin.sdk.adapter import (
    AdapterGatewayCore, DefaultPolicyEngine, NekoAdapterPlugin,
)
from plugin.sdk.adapter.gateway_models import ExternalRequest

@neko_plugin
class MyProtocolAdapter(NekoAdapterPlugin):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.gateway = None

    @lifecycle(id="startup")
    async def startup(self, **_):
        self.gateway = AdapterGatewayCore(
            normalizer=MyNormalizer(),
            policy_engine=DefaultPolicyEngine(),
            route_engine=MyRouteEngine(),
            invoker=MyInvoker(self.ctx),
            serializer=MySerializer(),
            logger=self.logger,
        )
        return Ok({"status": "ready"})

    @plugin_entry(id="handle_request")
    async def handle_request(self, raw_data: dict, **_):
        external = ExternalRequest(protocol="my_protocol", raw=raw_data)
        response = await self.gateway.process(external)
        return Ok(response.to_dict())
```

### 8.5 Adapter 模式

| 模式 | 说明 |
|------|------|
| `GATEWAY` | 完整管线处理 |
| `ROUTER` | 仅路由（跳过策略） |
| `BRIDGE` | 直接透传 |
| `HYBRID` | 按请求选择模式 |

### 8.6 内置参考：MCP Adapter

参见 `plugin/plugins/mcp_adapter/` 获取完整的 Adapter 实现，演示了：
- 自定义 Normalizer (`MCPRequestNormalizer`)
- 自定义路由引擎 (`MCPRouteEngine`)
- 自定义调用器 (`MCPPluginInvoker`)
- 自定义序列化器 (`MCPResponseSerializer`)
- 自定义传输层 (`MCPTransportAdapter`)

### 8.7 Adapter SDK 导出

从 `plugin.sdk.adapter` 导入：

- `AdapterBase`, `AdapterConfig`, `AdapterContext`, `AdapterMode` — 基础类
- `NekoAdapterPlugin` — 适配器插件基类
- `AdapterGatewayCore` — 网关核心
- `DefaultPolicyEngine`, `DefaultRouteEngine` 等 — 默认管线组件
- `ExternalRequest`, `GatewayRequest`, `GatewayResponse` 等 — 数据模型
- 装饰器：`on_adapter_startup`, `on_adapter_shutdown`, `on_mcp_tool`, `on_mcp_resource`, `on_nonebot_message`

---

## 第九章：高级主题

### 9.1 异步编程

运行时入口必须使用 `async def`。同步辅助函数仍可使用，但应通过异步入口暴露：

```python
@plugin_entry(id="async_task")
async def async_task(self, url: str, **_):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return Ok({"data": await response.json()})
```

### 9.2 线程安全

定时任务在独立线程中运行，保护共享状态：

```python
import threading

@neko_plugin
class ThreadSafePlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._lock = threading.Lock()
        self._counter = 0

    @plugin_entry(id="increment")
    async def increment(self, **_):
        with self._lock:
            self._counter += 1
            return Ok({"count": self._counter})

    @timer_interval(id="report", seconds=60, auto_start=True)
    async def report(self, **_):
        with self._lock:
            count = self._counter
        self.report_status({"count": count})
```

### 9.3 自定义配置

运行时配置由宿主提供的 `self.config` 管理。配置 API 是异步的，应在
异步生命周期钩子或入口中加载，不要覆盖 `self.config`：

```python
@lifecycle(id="startup")
async def load_config(self, **_):
    self.timeout = await self.config.get("timeout", default=30)
    return Ok({"timeout": self.timeout})
```

### 9.4 SQLite 数据持久化

```python
import sqlite3

class PersistentPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.db_path = self.data_path("records.db")
        self.data_path().mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE,
                value TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
```

---

## 第十章：最佳实践

### 10.1 始终使用 Result 类型

```python
@plugin_entry(id="process")
async def process(self, data: str, **_):
    if not data:
        return Err(SdkError("data 是必填的"))
    try:
        result = self._do_work(data)
        return Ok({"result": result})
    except Exception as e:
        self.logger.exception(f"意外错误: {e}")
        return Err(SdkError("内部错误"))
```

### 10.2 合理使用日志级别

| 级别 | 用途 |
|------|------|
| `debug` | 详细诊断信息 |
| `info` | 正常运行里程碑 |
| `warning` | 意外但已处理的情况 |
| `error` | 需要关注的错误 |
| `exception` | 带完整堆栈的错误 |

### 10.3 跨插件调用错误处理

```python
@plugin_entry(id="orchestrate")
async def orchestrate(self, **_):
    dep = await self.plugins.require_enabled("dependency_plugin")
    if isinstance(dep, Err):
        return Err(SdkError("依赖插件不可用"))

    result = await self.plugins.call_entry("dependency_plugin:do_work", {"key": "val"})
    if isinstance(result, Err):
        self.logger.error(f"跨插件调用失败: {result.error}")
        return Err(SdkError("依赖调用失败"))

    return Ok({"combined": result.value})
```

### 10.4 优雅关闭

```python
@lifecycle(id="shutdown")
async def on_shutdown(self, **_):
    if self.session:
        await self.session.close()
    self.logger.info("插件优雅关闭")
    return Ok({"status": "stopped"})
```

### 10.5 使用路径工具

```python
# 插件安装目录（代码、Manifest 和静态资源）
template_path = self.plugin_dir / "static" / "template.json"

# 运行时配置通过 await self.config.get()/update() 读取或更新

# 数据目录
db_path = self.data_path("records.db")     # → <storage-dir>/data/records.db
logs_dir = self.data_path("logs")          # → <storage-dir>/data/logs/

# 缓存目录
preview_path = self.cache_path("preview.png")  # → <storage-dir>/cache/preview.png
```

### 10.6 插件发布检查清单

- [ ] 所有入口点返回 `Ok`/`Err`（不是裸 dict 或异常）
- [ ] 只在确实需要初始化或清理资源时实现对应生命周期钩子
- [ ] 入口参数具备可推断 schema、显式 `input_schema` 或 Pydantic `params` 模型
- [ ] 入口签名明确声明消费的参数，仅在确实接收额外字段时使用 `**_`
- [ ] 正常诊断使用 Logger，且日志和进程输出均不包含原始对话、密钥或私有 payload
- [ ] 如果使用定时器，共享状态受锁保护
- [ ] 跨插件调用处理了 `Err` 结果
- [ ] `plugin.toml` 的 `entry` 路径和 SDK 版本约束正确

---

## 第十一章：常见问题

### Q: 插件崩溃会影响主系统吗？

Plugin 与 Adapter 的崩溃通常不会影响主系统或其他插件，因为它们独立运行。

### Q: 如何在插件间传递数据？

使用 `self.plugins.call_entry("target_plugin:entry_id", {"key": "value"})` 进行跨插件调用。所有返回值都是 `Result` 类型。

### Q: 同步还是异步？

运行时入口只支持 `async def`。同步计算可以保留为私有辅助函数；如果它会阻塞事件循环，应在入口中显式卸载到线程。

### Q: 如何调试插件？

1. 使用 `self.logger` 输出日志
2. 使用 `self.report_status()` 报告状态
3. 检查插件进程的标准输出/错误输出

### Q: Plugin vs Adapter 怎么选？

- **Plugin**：默认选择，承载普通功能和大型插件 Router
- **Adapter**：桥接外部协议（MCP、NoneBot 等）

### Q: `shared` 包是什么？我需要用它吗？

`shared` 是 SDK 的内部实现细节。**你不应该直接导入它。** 新代码始终从 `plugin.sdk.plugin` 或 `plugin.sdk.adapter` 导入。

---

## 第十二章：API 参考

### Plugin SDK (`plugin.sdk.plugin`)

| 类别 | 导出 |
|------|------|
| **基类** | `NekoPluginBase` |
| **元数据与类型** | `PluginMeta`, `EntryKind`, `LlmToolMeta`, `NEKO_PLUGIN_META_ATTR`, `NEKO_PLUGIN_TAG` |
| **装饰器** | `neko_plugin`, `plugin_entry`, `quick_action`, `lifecycle`, `timer_interval`, `message`, `on_event`, `custom_event`, `hook`, `before_entry`, `after_entry`, `around_entry`, `replace_entry`, `plugin`, `ui`, `llm_tool` |
| **Result** | `Ok`, `Err`, `Result`, `PushMessageResult`, `unwrap`, `unwrap_or` |
| **运行时** | `Plugins`, `PluginRouter`, `PluginConfig`, `PluginStore`, `SystemInfo` |
| **错误** | `SdkError`, `TransportError` |
| **日志** | `get_plugin_logger` |
| **设置** | `PluginSettings`, `SettingsField` |
| **活动与 i18n** | `OsActivitySnapshot`, `get_os_activity_snapshot`, `PluginI18n`, `tr` |

### Adapter SDK (`plugin.sdk.adapter`)

| 类别 | 导出 |
|------|------|
| **基类** | `AdapterBase`, `AdapterConfig`, `AdapterContext`, `AdapterMode` |
| **插件基类** | `NekoAdapterPlugin` |
| **网关** | `AdapterGatewayCore`, `DefaultPolicyEngine`, `DefaultRouteEngine`, `DefaultRequestNormalizer`, `DefaultResponseSerializer`, `CallablePluginInvoker` |
| **数据模型** | `ExternalRequest`, `GatewayRequest`, `GatewayResponse`, `GatewayAction`, `GatewayError`, `RouteDecision`, `RouteMode` |
| **协议** | `TransportAdapter`, `RequestNormalizer`, `PolicyEngine`, `RouteEngine`, `PluginInvoker`, `ResponseSerializer` |
| **装饰器** | `on_adapter_startup`, `on_adapter_shutdown`, `on_mcp_tool`, `on_mcp_resource`, `on_nonebot_message` |
| **类型** | `Protocol`, `RouteTarget`, `AdapterMessage`, `AdapterResponse`, `RouteRule` |
