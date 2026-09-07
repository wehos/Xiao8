# 最佳实践

## 一致使用 Result 类型

始终在入口点中返回 `Ok`/`Err`，而不是抛出异常：

```python
from plugin.sdk.plugin import Ok, Err, SdkError

@plugin_entry(id="process")
async def process(self, data: str, **_):
    if not data:
        return Err(SdkError("data is required"))

    try:
        result = self._do_work(data)
        return Ok({"result": result})
    except ValueError as e:
        return Err(SdkError(f"Validation error: {e}"))
    except Exception as e:
        self.logger.exception(f"Unexpected error: {e}")
        return Err(SdkError(f"Internal error"))
```

## 代码组织

将初始化、辅助方法和公共入口点分离：

```python
@neko_plugin
class WellOrganizedPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._initialize()

    # --- 生命周期 ---
    @lifecycle(id="startup")
    async def on_startup(self, **_):
        return Ok({"status": "ready"})

    # --- 私有辅助方法 ---
    def _initialize(self):
        """设置资源。"""
        pass

    def _validate(self, data):
        """内部验证。"""
        pass

    # --- 公共入口点 ---
    @plugin_entry(id="process")
    async def process(self, data: str, **_):
        self._validate(data)
        return Ok({"result": self._do_work(data)})
```

## 日志记录

使用适当的日志级别：

| 级别 | 使用场景 |
|------|----------|
| `debug` | 详细的诊断信息 |
| `info` | 正常运行的里程碑 |
| `warning` | 意外但已处理的情况 |
| `error` | 需要关注的错误 |
| `exception` | 带完整堆栈跟踪的错误 |

不要把原始对话、用户输入的密钥或其他隐私敏感 payload 写进日志或进程输出。正常诊断优先记录脱敏后的长度、ID 和错误类型；确实需要敏感内容才能复现时，应改用合成数据，而不是打印或记录原始 payload。

```python
self.logger.debug(f"Processing item {item_id}")
self.logger.info(f"Plugin started successfully")
self.logger.warning(f"Retry attempt {attempt}/3")
self.logger.error(f"Failed to connect: {err}")
self.logger.exception(f"Unexpected error in process()")
```

## 状态更新

在长时间运行的操作中报告进度：

```python
@plugin_entry(id="batch_job")
async def batch_job(self, items: list, **_):
    total = len(items)
    for i, item in enumerate(items):
        self._process(item)
        self.report_status({
            "status": "processing",
            "progress": (i + 1) / total * 100,
            "message": f"Processing {i+1}/{total}"
        })

    self.report_status({"status": "completed", "progress": 100})
    return Ok({"processed": total})
```

## 输入验证

使用 `input_schema` 进行自动 JSON Schema 验证，或使用 `params` 配合 Pydantic 模型：

```python
# 方式 A：JSON Schema
@plugin_entry(
    id="validated",
    input_schema={
        "type": "object",
        "properties": {
            "email": {"type": "string", "format": "email"},
            "age": {"type": "integer", "minimum": 0, "maximum": 150}
        },
        "required": ["email", "age"]
    }
)
async def validated(self, email: str, age: int, **_):
    return Ok({"email": email, "age": age})

# 方式 B：Pydantic 模型（自动生成 schema）
from pydantic import BaseModel, Field

class UserInput(BaseModel):
    email: str = Field(..., description="User email")
    age: int = Field(..., ge=0, le=150)

@plugin_entry(id="validated_v2", params=UserInput)
async def validated_v2(self, email: str, age: int, **_):
    return Ok({"email": email, "age": age})
```

## 工作目录

`self.plugin_dir` 只用于随包代码和只读资源。运行时配置由 `self.config` 管理；
持久数据和缓存应写入宿主分配的状态根目录：

```python
# 可执行目录（plugin.toml 和随包资源所在位置）
manifest_path = self.plugin_dir / "plugin.toml"

# 与可执行代码分离的状态目录
db_path = self.data_path("cache.db")       # → <plugin-state-root>/data/cache.db
preview_path = self.cache_path("preview.png")  # → <plugin-state-root>/cache/preview.png
```

## 跨插件调用的错误处理

调用其他插件时始终处理 `Err`：

```python
@plugin_entry(id="orchestrate")
async def orchestrate(self, **_):
    # 先检查依赖
    dep = await self.plugins.require_enabled("dependency_plugin")
    if isinstance(dep, Err):
        return Err(SdkError("Required plugin 'dependency_plugin' is not available"))

    # 发起调用
    result = await self.plugins.call_entry("dependency_plugin:do_work", {"key": "val"})
    if isinstance(result, Err):
        self.logger.error(f"Cross-plugin call failed: {result.error}")
        return Err(SdkError("Dependency call failed"))

    return Ok({"combined": result.value})
```

## 优雅关闭

在 shutdown 生命周期中清理资源：

```python
@lifecycle(id="shutdown")
async def on_shutdown(self, **_):
    # 关闭网络连接
    if self.session:
        await self.session.close()

    # 无需刷新 self.store：每次 set() 都会同步提交。
    # 如果是你自己打开的 store，可在此关闭（可选）：
    # await self.store.close()

    # 取消定时器（自动处理，但记录日志）
    self.logger.info("Plugin shutting down gracefully")
    return Ok({"status": "stopped"})
```

## 插件检查清单

发布插件前请检查：

- [ ] 所有入口点返回 `Ok`/`Err`（而非原始字典或异常）
- [ ] 只在确实需要初始化或清理资源时添加生命周期钩子
- [ ] 入口参数按需使用自动推导 schema、显式 `input_schema` 或 Pydantic `params` 模型
- [ ] 处理器签名只声明实际消费的字段；只有有意接收额外字段时才使用 `**_`
- [ ] 普通元数据使用 Logger；原始隐私敏感内容绝不写 Logger
- [ ] 如果使用了定时器，共享状态已用锁保护
- [ ] 跨插件调用已处理 `Err` 结果
- [ ] `plugin.toml` 中宿主加载用的 `[plugin].entry` 路径和 SDK 版本约束正确
