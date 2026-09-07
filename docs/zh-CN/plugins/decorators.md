# 装饰器

所有装饰器均从 `plugin.sdk.plugin` 导入。

```python
from plugin.sdk.plugin import (
    neko_plugin, plugin_entry, lifecycle, timer_interval, message,
    on_event, custom_event,
    hook, before_entry, after_entry, around_entry, replace_entry,
    plugin, quick_action,  # 命名空间风格和命令面板提示
)
```

## @neko_plugin

将类标记为 N.E.K.O. 插件。所有插件类都**必须**使用此装饰器。

```python
@neko_plugin
class MyPlugin(NekoPluginBase):
    pass
```

## @plugin_entry

定义一个可外部调用的入口点。

```python
@plugin_entry(
    id="process",                # 入口点 ID（如果省略则从方法名自动生成）
    name="Process Data",         # 显示名称
    description="Process data",  # 描述
    input_schema={...},          # 用于验证的 JSON Schema
    params=MyParamsModel,        # 替代方式：用于输入的 Pydantic 模型（自动生成 schema）
    kind="action",               # "action" | "service" | "hook" | "custom"
    auto_start=False,            # 元数据标志；普通入口不会在加载时自动执行
    persist=False,               # 覆盖调用后的状态快照策略
    model_validate=True,         # 启用 Pydantic 验证
    timeout=30.0,                # 执行超时时间（秒）
    llm_result_fields=["text"],  # 为 LLM 消费提取的字段
    llm_result_model=MyResult,   # 用于结果 schema 的 Pydantic 模型
    metadata={"category": "data"}  # 附加元数据
)
async def process(self, data: str, **_):
    return Ok({"result": data})
```

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `id` | `str` | 方法名 | 唯一入口点标识符 |
| `name` | `str` | `None` | 显示名称 |
| `description` | `str` | `""` | 描述 |
| `input_schema` | `dict` | `None` | 用于输入验证的 JSON Schema |
| `params` | `type` | `None` | Pydantic 模型（自动生成 `input_schema`） |
| `kind` | `str` | `"action"` | 入口类型 |
| `auto_start` | `bool` | `False` | 元数据标志；普通 `plugin_entry` 不会在加载时自动执行 |
| `persist` | `bool` | `None` | 覆盖该入口执行后是否保存已配置的可冻结状态 |
| `model_validate` | `bool` | `True` | 启用 Pydantic 验证 |
| `timeout` | `float` | `None` | 执行超时时间（秒） |
| `llm_result_fields` | `list[str]` | `None` | 用于 LLM 结果提取的字段 |
| `llm_result_model` | `type` | `None` | 用于结果 schema 的 Pydantic 模型 |
| `fields` | `type` | `None` | `params` 的别名 |
| `metadata` | `dict` | `None` | 附加元数据 |

::: tip
只有在处理器有意接受宿主额外字段时才使用 `**_`。显式签名的处理器会由运行时过滤不支持的关键字参数，因此它不是硬性要求。
:::

运行时入口必须使用 `async def`；宿主会拒绝同步入口处理器。

## @lifecycle

定义可选的启动、关闭、外部配置变更和进程挂起处理器。初始化应使用
`startup`；普通的 `@plugin_entry(auto_start=True)` 不会在插件进程启动时执行。

```python
@lifecycle(id="startup")
async def on_startup(self, **_):
    cfg = await self.config.dump()
    self.timeout = cfg.get("my_settings", {}).get("timeout", 30)
    return Ok({"status": "ready"})

@lifecycle(id="shutdown")
async def on_shutdown(self, **_):
    session = getattr(self, "session", None)
    if session:
        await session.close()
    return Ok({"status": "stopped"})

@lifecycle(id="config_change")
async def on_config_change(self, old_config, new_config, mode):
    self.timeout = new_config.get("my_settings", {}).get("timeout", 30)
    return Ok({"status": "config_updated"})
```

| 生命周期 ID 或操作 | 发生时机 | 常见用途 |
| --- | --- | --- |
| `startup` | 插件进程启动 | 读取配置、建立连接、准备资源 |
| `shutdown` | 插件进程停止 | 关闭连接、保存状态、释放资源 |
| 插件管理器“重载” | 用户点击重载 | 先执行 `shutdown`，再启动进程并执行 `startup` |
| `config_change` | 配置由外部修改 | 不重启地应用新设置 |
| `freeze` / `unfreeze` | 插件被挂起或恢复 | 暂停或恢复工作 |

SDK 仍兼容 `reload` 生命周期 ID，但插件管理器的重载按钮会重启进程，不会分派该事件。通过 `await self.ctx.update_own_config(...)` 或 `await self.config.update(...)` 更新配置时，也不会向同一进程回派 `config_change`；调用后应主动刷新派生状态。

## @timer_interval

定义按固定间隔执行的定时任务。

```python
@timer_interval(
    id="cleanup",
    seconds=3600,           # 每小时执行一次
    name="Cleanup Task",
    auto_start=True          # 自动启动（默认值：True）
)
async def cleanup(self, **_):
    # 在独立线程中运行
    return Ok({"cleaned": True})
```

::: info
定时任务必须使用 `async def`。每个任务在拥有独立事件循环的定时器线程中运行；异常会被记录，但不会停止计时器。
:::

## @message

定义处理来自宿主系统消息的处理器。

```python
@message(
    id="handle_chat",
    source="chat",           # 按消息来源过滤
)
async def handle_chat(self, text: str, sender: str, **_):
    return Ok({"handled": True})
```

## @on_event

通用事件处理器，用于自定义事件类型。

```python
@on_event(
    event_type="custom_event",
    id="my_handler",
    kind="hook"
)
async def custom_handler(self, event_data: str, **_):
    return Ok({"processed": True})
```

## @custom_event

带触发方法控制的专用事件处理器。

```python
@custom_event(
    event_type="data_refresh",
    id="refresh_handler",
    trigger_method="message",  # 此事件的触发方式
    auto_start=False
)
async def on_refresh(self, source: str, **_):
    return Ok({"refreshed": True})
```

## @quick_action

把插件入口标记为命令面板中的优先快捷操作。它必须写在 `@plugin_entry` 下方，让 Python 先应用它：

```python
@plugin_entry(id="get_weather", name="获取天气")
@quick_action(icon="🌤️", priority=10)
async def get_weather(self, city: str = ""):
    return Ok({"city": city})
```

`priority` 越大，展示越靠前。这个装饰器只修改展示元数据，不会改变 Agent 路由，也不会自动执行入口。

---

## 钩子装饰器（AOP）

钩子装饰器提供面向切面编程（AOP）能力，用于拦截入口点的执行。

### @before_entry

在目标入口点之前运行。可以修改参数或中止执行。

```python
@before_entry(target="process", priority=0)
def validate_input(self, *, args, entry_id, **_):
    if not args.get("data"):
        return Err(SdkError("data is required"))
    # 返回 None 继续执行，或返回 Err 中止执行
```

### @after_entry

在目标入口点之后运行。可以修改或替换结果。

```python
@after_entry(target="process", priority=0)
def log_result(self, *, result, entry_id, **_):
    self.logger.info(f"Entry {entry_id} returned: {result}")
    # 返回 None 保留原始结果，或返回新值替换结果
```

### @around_entry

包装目标入口点。完全控制执行流程。

```python
@around_entry(target="process", priority=0)
async def timing_wrapper(self, *, proceed, args, **_):
    import time
    start = time.time()
    result = await proceed(**args)
    elapsed = time.time() - start
    self.logger.info(f"Took {elapsed:.2f}s")
    return result
```

### @replace_entry

完全替换目标入口点。

```python
@replace_entry(target="old_entry", priority=0)
async def new_implementation(self, **kwargs):
    return Ok({"replaced": True})
```

### 钩子参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `target` | `str` | `"*"` | 要钩住的入口 ID（`"*"` = 所有入口） |
| `priority` | `int` | `0` | 执行顺序（值越小越先执行） |
| `condition` | `str` | `None` | 可选的条件表达式 |

---

## 命名空间风格替代方式：`plugin.*`

为了更简洁的语法，可以使用 `plugin` 命名空间对象：

```python
from plugin.sdk.plugin import plugin

@plugin.entry(id="greet", description="Say hello")
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

@plugin.message(id="on_chat", source="chat")
async def on_chat(self, text: str, **_):
    return Ok({"handled": True})
```
