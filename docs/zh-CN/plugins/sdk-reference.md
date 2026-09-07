# 插件能力与 SDK 参考

所有插件开发 API 均从 `plugin.sdk.plugin` 导入。

```python
from plugin.sdk.plugin import (
    # 基类
    NekoPluginBase, PluginMeta,
    # 装饰器
    EntryKind, neko_plugin, plugin_entry, lifecycle, timer_interval, message,
    on_event, custom_event, hook, before_entry, after_entry, around_entry,
    replace_entry, quick_action, plugin, ui,
    # LLM 工具与系统活动
    llm_tool, LlmToolMeta, OsActivitySnapshot, get_os_activity_snapshot,
    # 插件本地 i18n 与设置
    PluginI18n, tr, PluginSettings, SettingsField,
    # Result 类型
    Ok, Err, Result, unwrap, unwrap_or,
    # 运行时辅助工具
    Plugins, PluginRouter, PluginConfig, PluginStore,
    SystemInfo,
    # 错误
    SdkError, TransportError,
    # 日志
    get_plugin_logger,
)
```

`plugin.sdk.plugin` 是受支持的开发者导入面。根包 `plugin.sdk` 有意只暴露保守的共享子集；不要假定插件专用 helper 都会从根包再次导出。

## NekoPluginBase

所有插件必须继承 `NekoPluginBase`。

```python
@neko_plugin
class MyPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
```

### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `self.ctx` | `PluginContext` | 运行时上下文（由宿主注入） |
| `self.plugin_id` | `str` | 本插件的唯一标识符 |
| `self.plugin_dir` | `Path` | 包含代码、Manifest 和静态资源的插件安装目录 |
| `self.config_dir` | `Path` | `self.plugin_dir` 的兼容别名 |
| `self.storage_dir` | `Path` | 分配给插件的用户存储根目录 |
| `self.runtime_config_path` | `Path` | 外部运行配置文件路径 |
| `self.metadata` | `dict` | 来自 `plugin.toml` 的插件元数据 |
| `self.bus` | `SdkBusContext` | 宿主状态的 read/watch 门面；没有 publish/emit API |
| `self.plugins` | `Plugins` | 跨插件调用辅助工具 |
| `self.system_info` | `SystemInfo` | 宿主系统元数据 |

### 常用能力

`NekoPluginBase` 无需额外设置即可提供日志和配置：

```python
self.logger.info("Processing request: {}", request_id)
timeout = await self.config.get_int("my_settings.timeout", default=30)
```

日志会显示在插件管理器中，并写入宿主管理的日志目录；日志文件位置和轮换策略由宿主负责。运行时配置通过 `await self.ctx.update_own_config(...)` 或 `await self.config.update(...)` 更新。调用后，应在同一进程中主动刷新依赖配置的派生状态。

根据数据形式选择存储方式：

| 需求 | API |
| --- | --- |
| 少量键值状态 | 启用 `[plugin.store]` 后使用 `self.store` |
| 结构化 SQLite 数据 | 启用 `[plugin.database]` 后使用 `self.db` |
| 任意持久文件 | `self.data_path(...)` |
| 可重新生成的文件 | `self.cache_path(...)` |

```python
from plugin.sdk.plugin import unwrap

unwrap(await self.store.set("last_query", "weather"))

async with unwrap(await self.db.session()) as session:
    await session.execute(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT)"
    )
    await session.commit()
```

需要翻译插件文本时，在 `plugin.toml` 中配置 `[plugin.i18n]`、添加各语言 JSON 文件，然后使用 `self.i18n.t(...)`：

```python
message = self.i18n.t("greeting", name="Alice")
```

### 方法

#### `report_status(status: dict) -> None`

向宿主进程报告插件状态。

```python
self.report_status({
    "status": "processing",
    "progress": 50,
    "message": "Halfway done..."
})
```

#### `push_message(**kwargs) -> PushMessageResult`

使用 v2 schema 向宿主系统推送消息。

```python
result = self.push_message(
    source="my_feature",
    visibility=["chat"],       # []、["chat"]、["hud"] 或二者
    ai_behavior="blind",       # "respond"、"read"、"blind"
    parts=[{"type": "text", "text": "任务已完成"}],
    priority=5,
)

if not result["submitted"]:
    # 保留本地状态；重试和去重仍由插件自行决定。
    self.logger.warning("消息提交失败：%s", result["reason"])
```

`submitted=True` 只表示 SDK 的权威本地提交路径已接收 payload，并由 SDK 接管后续
提交责任；它不表示宿主已经消费、模型已经生成或音频已经播放。
拒绝结果使用稳定的 `backpressure`、`transport_error` 或
`transport_unavailable` reason，且不会包含消息正文或原始异常文本。拒绝结果还会携带
兼容旧调用方的 `ok=False`；新代码应以 `submitted` 为正式判据。

v1 字段（`message_type`、`content`、`delivery`、`reply` 及其他旧别名）已经弃用，但当前源码仍会转换。请立即迁移；本文档不保证确切移除版本。参见[迁移指南](./migration-v0.9#push-message-v2)。

#### 媒体消息片段

- 宿主支持文字和图片片段。小图片可以直接内联发送：

  ```python
  parts=[{"type": "image", "data": image_bytes, "mime": "image/png"}]
  ```

- 对于不是很小的图片，先交给宿主临时上传，再发送返回的 part：

  ```python
  image_part = await self.ctx.images.upload(image_bytes, mime="image/png")
  result = self.push_message(
      source="my_feature",
      visibility=["chat"],
      ai_behavior="read",
      parts=[image_part],
  )
  ```

  模型投递不接受任意外部图片 URL，请使用这条宿主临时上传路径。内联图片与整条消息共享 message plane 的 payload 预算；上传后的 part 不会把图片字节塞进该消息包。生命周期处理器运行时，插件命令循环无法接收上传响应，因此不能调用 `ctx.images.upload()`；请在插件 entry、定时器、消息处理器或自定义事件处理器中调用。
- `visibility` 决定是否在用户的聊天窗口或 HUD 中显示；`ai_behavior` 独立决定模型是否读取消息或作出回应。
- 宿主目前不会投递音频和视频消息片段，插件不能把提交成功当作已经播放或显示。
- Hosted UI 面板可以自行向用户播放音频或视频，但这不等于通过原生聊天或模型通道投递媒体。

#### `data_path(*parts) -> Path`

获取插件 `data/` 目录下的路径。

```python
db_path = self.data_path("records.db")
# → <storage-root>/plugins/<plugin_id>/data/records.db
```

#### `cache_path(*parts) -> Path`

获取插件可清理缓存目录下的路径。

```python
preview_path = self.cache_path("preview.png")
# → <storage-root>/plugins/<plugin_id>/cache/preview.png
```

#### `register_dynamic_entry(entry_id, handler, ...) -> bool`

在运行时注册入口点（非通过装饰器）。

```python
self.register_dynamic_entry(
    entry_id="dynamic_greet",
    handler=lambda name="World", **_: Ok({"msg": f"Hi {name}"}),
    name="Dynamic Greet",
    description="A dynamically registered greeting",
)
```

#### `unregister_dynamic_entry(entry_id) -> bool`

移除一个动态注册的入口点。

#### `list_entries(include_disabled=False) -> list[dict]`

列出所有入口点（静态 + 动态）。

#### `enable_entry(entry_id) / disable_entry(entry_id) -> bool`

在运行时启用或禁用动态入口点。

#### `register_static_ui(directory, *, index_file, cache_control) -> bool`

为本插件注册一个静态 Web UI 目录。

```python
self.register_static_ui("static")  # 提供 <plugin_dir>/static/index.html 服务
```

#### `include_router(router, *, prefix) -> None`

挂载一个 `PluginRouter`，用于组织大型或按功能拆分的普通 Plugin。

相关方法还有 `exclude_router(router_or_name) -> bool`、`get_router(name)` 和 `list_routers()`。Router 不能作为 manifest 的 `[plugin].entry`，而且这条挂载路径不会自动调用 `on_mount` / `on_unmount`。

#### Hosted/静态 UI 与列表操作

Hosted TSX 使用导出的 `ui` namespace 和 manifest surface，详见 [Hosted UI](./hosted-ui)。旧式静态 UI 使用 `register_static_ui(...)`。列表行操作使用 `set_list_actions(...)`、`register_list_action(...)`、`clear_list_actions()` 和 `get_list_actions()` 管理。

#### LLM 工具方法

`register_llm_tool(...)`、`unregister_llm_tool(name)` 和 `list_llm_tools()` 是 `@llm_tool` 的命令式对应接口。它们注册对话期工具，不是用户插件 Agent 入口。详见 [LLM Tool Calling](./tool-calling)。

#### `run_update(**kwargs) -> object`（异步）

在长时间运行的操作期间向宿主发送更新。

#### `export_push(**kwargs) -> object`（异步）

向宿主推送导出数据。

#### `finish(**kwargs) -> Any`（异步）

向宿主发送任务完成信号。

### 回复控制

`finish()` 方法接受 `reply` 参数（默认 `True`），用于控制插件结果是否触发角色说话。

```python
# 正常：角色会播报结果
return await self.finish(data={"summary": "完成"}, reply=True)

# 静默：结果会记录但角色不说话
return await self.finish(data={"summary": "完成"}, reply=False)
```

### LLM 结果字段过滤

通过 `@plugin_entry` 装饰器（静态入口）或 `register_dynamic_entry()`（动态入口）的 `llm_result_fields` 参数，控制主 LLM 能看到结果中的哪些字段。未列出的字段不会出现在 LLM 提示中，但仍保存在任务注册表中。

```python
# 静态入口
@plugin_entry(llm_result_fields=["summary"])
async def search(self, query: str):
    return await self.finish(data={"summary": "找到3条结果", "raw_results": [...]})

# 动态入口
self.register_dynamic_entry(
    entry_id="my-tool",
    handler=handler,
    llm_result_fields=["summary"],
)
```

---

## Result 类型：Ok / Err

SDK 使用受 Rust 启发的 Result 类型进行错误处理，而非异常。

```python
from plugin.sdk.plugin import Ok, Err, unwrap, unwrap_or

# 返回成功
return Ok({"data": result})

# 返回错误
return Err(SdkError("something went wrong"))

# 使用结果
result = await self.plugins.call_entry("other:do_stuff")
if isinstance(result, Ok):
    data = result.value
else:
    error = result.error
    self.logger.error(f"Call failed: {error}")

# 辅助函数
value = unwrap(result)           # 如果是 Err 则抛出异常
value = unwrap_or(result, None)  # 如果是 Err 则返回默认值
```

---

## Plugins（跨插件调用）

通过 `self.plugins` 访问。

```python
# 列出所有插件
result = await self.plugins.list()

# 仅列出已启用的插件
result = await self.plugins.list(enabled=True)

# 获取插件 ID 列表
result = await self.plugins.list_ids()

# 检查插件是否存在
result = await self.plugins.exists("other_plugin")

# 调用另一个插件的入口点
result = await self.plugins.call_entry("other_plugin:do_work", {"key": "value"})

# 调用并确保返回 JSON 对象
result = await self.plugins.call_entry_json("other_plugin:get_data")

# 要求某个插件存在且已启用
result = await self.plugins.require_enabled("dependency_plugin")
```

所有方法返回 `Result` 类型 — 在使用 `.value` 之前，请先用 `isinstance(result, Ok)` 检查。

---

## PluginStore（持久化存储）

通过 `self.store` 访问（由宿主在插件构造时预先创建并注入，无需自己实例化）。

`PluginStore` 的所有方法都返回 `Result`，需用 `unwrap_or(...)` 解包。

```python
unwrap_or(await self.store.set("key", {"count": 42}), None)
value = unwrap_or(await self.store.get("key"), None)  # → {"count": 42}
```

---

## SystemInfo

通过 `self.system_info` 访问。这些方法都返回 `Result`，需用 `unwrap_or(...)` 解包。

```python
config = unwrap_or(await self.system_info.get_system_config(), {})
settings = unwrap_or(await self.system_info.get_server_settings(), {})
python_env = unwrap_or(await self.system_info.get_python_env(), {})
```

---

## PluginContext (ctx)

`ctx` 对象在构造时由宿主注入。

| 属性 | 类型 | 说明 |
|------|------|------|
| `ctx.plugin_id` | `str` | 插件标识符 |
| `ctx.config_path` | `Path` | `plugin.toml` 的路径 |
| `ctx.logger` | `Logger` | 日志记录器实例 |
| `ctx.bus` | `SdkBusContext` | 宿主状态的 read/watch 门面 |
| `ctx.metadata` | `dict` | 插件元数据 |

### Bus 与 Memory

在异步入口中，先 `await get()`，再使用本地列表操作：

```python
events = await self.bus.events.get(plugin_id=self.plugin_id, max_count=50)
recent = events.filter(priority_min=1).sort(by="timestamp", reverse=True).limit(20)

records = await self.bus.memory.get(bucket_id="default", limit=20)
```

列表接口为 `filter` / `where`、`sort`、`limit`、`watch`。可调用形式 `filter(predicate)`、`where(predicate)` 和 `sort(key=...)` 仅处理本地快照；可重放的 watcher 链必须使用结构化 `filter(field=value, ...)` 与 `sort(by=...)`。只有 `messages`、`events`、`lifecycle` 支持 `watch()`；`conversations`、`memory` 与 `frames` 是只读快照。watcher 仅接受 `add`、`del`、`change`。

【访问范围】这几个 store 是共享的，读取不按插件做权限隔离：任何已启用的插件都能读 `conversations` 和 `frames`，包括来自用户、以及来自别的插件的轮次与画面。这不会扩大宿主**已经发给模型**的内容——会话没发过的帧不会出现在这里——但确实扩大了**谁能看到**：从模型服务方扩大到用户启用的每一个插件。安装一个插件就等于授予它这份可见性；你的插件如果读这两条总线，请在自己的说明里写明。

`bus.memory` 保存的是有容量上限、只驻留内存的近期用户话语事件（TTL 为一小时），与角色持久化的事实、反思和人格相互独立。`ctx.query_memory(...)` 只为兼容而保留，它调用已弃用的占位端点，不执行语义召回。

### Frames

`bus.frames` 保存宿主已经推送给模型提供方的最近几帧画面——就是提供方实际收到的那份字节。

```python
frames = await self.bus.frames.get(max_count=4)
latest = frames.sort(by="timestamp", reverse=True).limit(1)
```

插件无法要求截图。被会话自身的节流或投递模式栅栏丢掉的那一帧压根没有发给提供方，因此也不会出现在这里。

**它不是日志，也不是队列。** 帧在四个位置被有意丢弃：message plane 的 PUB 套接字对慢订阅方和到达高水位时都会丢包；宿主侧的发布是非阻塞的；发送队列有上限，一旦落后就直接拒收新帧；store 本身只保留个位数（`NEKO_MESSAGE_PLANE_FRAMES_STORE_MAXLEN`，取值被夹在 2-8，默认 4）。轮询"每一帧"，或者认为读到过一次的帧还在，都会出错。

每条记录带有 `image_base64`（只有一份，不存在可读的裸字节副本）、`mime`、`source`、`captured_at`、`turn_id`、`generation`、`frame_id` 和 `metadata`。`source` 是宿主给这一帧归的通道：`screen`、`camera`、`plugin`、`callback`、`proactive`、`user`，以及这一轮被重排后宿主已经说不准时的 `unknown`。`screen` 和 `camera` 只在语音路径上区分这两条实时通道；文字模式下用户分享的每一帧——共享的屏幕、相机截图、拖进来的照片——都走同一条不区分来源的队列，因此一律报作 `user`。想在两种模式下都拿到「用户给角色看的东西」，请按 `user` 过滤。请按 `frame_id` 去重：`generation` 只为常驻画面排序，一次性提示图不会让它前进，所以两条记录可能共用同一个值。

工具返回的图片也在这条总线上——前提是携带它们的那次后继请求已经得到应答。这类帧的 `source` 为 `"plugin"`，`metadata` 为 `{"tool_name": "..."}`。工具最多可以返回 2 MiB base64；超过投递预算（500 KiB）的部分由宿主按模型画面的同一套 profile 重新编码（JPEG，最大 1280×720），使其落在 message plane 的记录上限之内，从而同时到达模型和这条总线。此时记录里的 `mime` 会是 `image/jpeg`（即便工具交回的是 PNG），工具结果里也会带一条模型可见的说明，告诉它这张图被重新编码过。压缩之后仍然放不下的图会被丢弃，并附带自己的告警。读像素之前先读 `source`：标为 `plugin` 的帧是某个插件交给模型的媒体（未必是你自己的），不是用户共享给角色的画面。

### 优先级等级

| 范围 | 等级 | 使用场景 |
|------|------|----------|
| 0-2 | 低 | 信息性消息 |
| 3-5 | 中 | 一般通知 |
| 6-8 | 高 | 重要通知 |
| 9-10 | 紧急 | 需要立即处理 |
