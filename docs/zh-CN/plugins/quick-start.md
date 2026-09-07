# 用 Plugin CLI 创建并运行第一个插件

这篇教程会确认 N.E.K.O 自带的 Plugin CLI 可以正常工作，并用它创建一个可以立即运行和继续开发的 Hello World 插件。

开发中的插件直接放在 N.E.K.O 源码的 `plugin/plugins/` 目录。完成后，你会得到一个包含插件示例、配置、测试、代码检查和 GitHub 发布配置的 `hello_world` 项目。

## 1. 确认 Git 和 uv 已安装

打开终端并运行：

```bash
git --version
uv --version
```

| 命令 | 确认什么 |
| --- | --- |
| `git --version` | Git 可以使用。后面需要用它克隆 N.E.K.O 源码，并为插件提交和推送版本。 |
| `uv --version` | uv 可以使用。后面需要用它安装锁定的 Python 依赖并启动 Plugin CLI。 |

两条命令都必须显示版本号。如果任何一条提示“找不到命令”或“不是内部或外部命令”，请先安装：

- 安装 Git：[Git 官方下载](https://git-scm.com/downloads)
- 安装 uv：[uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)

## 2. 获取 N.E.K.O 源码

Plugin CLI 目前随 N.E.K.O 源码提供。开始开发 N.E.K.O 插件时，先获取源码：

```bash
git clone --filter=blob:none https://github.com/Project-N-E-K-O/N.E.K.O.git
cd N.E.K.O
```

如果已经有 N.E.K.O 源码，不要再次克隆，直接进入原来的仓库：

```bash
cd /path/to/N.E.K.O
```

::: warning 不要在有同名目录时继续克隆
`git clone` 遇到已经存在的 `N.E.K.O` 目录会停止。先确认那个目录是不是已有的源码；不要为了继续教程直接删除它，其中可能有你的配置或尚未提交的修改。
:::

## 3. 准备环境并检查 CLI

在 N.E.K.O 仓库根目录运行：

```bash
uv sync
uv run neko-plugin --help
```

`neko-plugin` 帮助信息中应至少能看到：

```text
init
check
sync
build
publish
```

看到这些命令，说明 CLI 已经可以使用。后续命令都在 N.E.K.O 仓库根目录运行，并使用 `uv run neko-plugin`。

## 4. 在源码目录中创建插件

运行：

```bash
uv run neko-plugin init hello_world \
  --type plugin \
  --name "Hello World"
```

这会把 **Hello World** 插件直接创建在：

```text
plugin/plugins/hello_world/
```

N.E.K.O 会扫描 `plugin/plugins/`，所以这里的代码就是开发时实际运行的代码，不需要复制到用户插件目录，也不需要使用符号链接。

这句话只说明“代码从哪里加载”。插件运行时产生的配置、数据和缓存仍然写入用户数据目录，不会写回源码目录。

### 源码目录和用户数据目录分别放什么

开发 `hello_world` 时，同一个插件会涉及两个目录：

```text
N.E.K.O/plugin/plugins/hello_world/       ← 开发者修改这里
├── plugin.toml                           ← 插件身份和代码入口
├── config.example.toml                   ← 首次运行配置的模板
├── __init__.py                           ← 插件代码
├── tests/
└── ...

<用户数据根目录>/plugins/hello_world/     ← N.E.K.O 运行时写入这里
├── config/
│   └── plugin.toml                       ← 这个用户实际使用的配置
├── data/                                 ← 需要保留的插件数据
└── cache/                                ← 插件缓存
```

在没有更改存储位置或相关环境变量时，默认的用户数据根目录是：

| 系统 | 默认目录 |
| --- | --- |
| Linux | `~/.local/share/N.E.K.O` |
| macOS | `~/Library/Application Support/N.E.K.O` |
| Windows | `%LOCALAPPDATA%\N.E.K.O` |

如果用户在 N.E.K.O 中选择了其他存储位置，或者设置了系统数据目录相关的环境变量，则以上目录跟随实际的用户数据根目录变化。

这里最容易混淆的是两个同名文件：

- 源码目录根部的 `plugin.toml` 是插件清单，N.E.K.O 用它识别插件并找到代码入口。
- 用户数据目录中的 `config/plugin.toml` 是这个用户正在使用的运行配置。

`config.example.toml` 只是运行配置的初始模板。N.E.K.O 第一次需要运行配置时，会把它复制到用户数据目录中的 `config/plugin.toml`；如果实际配置已经存在，就不会用模板覆盖它。

安装包代码与可写的运行时状态分开存放。这篇教程讲的是源码开发，所以后面只修改 `N.E.K.O/plugin/plugins/hello_world/`；安装目录和用户数据目录都由 N.E.K.O 管理。

命令中的三个值分别表示：

| 值 | 含义 |
| --- | --- |
| `hello_world` | 插件 ID，也是目录名。只能使用小写字母、数字和下划线，并且必须以字母开头。 |
| `--type plugin` | 创建一个普通插件，通常来说使用plugin即可。 |
| `--name "Hello World"` | 名称，建议和插件 ID保持相同。 |

如果目标目录已经存在，CLI 会停止，并且不会覆盖其中的文件。请先确认已有目录的用途，再决定继续使用它，还是换一个新的插件 ID。

## 5. 运行第一次检查

```bash
uv run neko-plugin check hello_world
```

新项目会显示：

```text
[OK] hello_world: check found 0 error(s), 4 warning(s)
```

四条警告分别是：

- 还没有 `startup` 生命周期钩子
- 还没有 `shutdown` 生命周期钩子
- 还没有配置 Git remote
- Git 工作区中有尚未提交的文件

这个 Hello World 插件不需要启动和关闭钩子；Git remote 和提交也只影响后续发布。四条警告都不会阻止它运行。以后出现 `[FAIL]` 或 error 时，才需要先停下来，按照命令给出的提示修复。

## CLI 已经为你准备了什么

```text
plugin/plugins/hello_world/
├── .git/
├── .gitignore
├── .vscode/
├── plugin.toml
├── config.example.toml
├── __init__.py
├── pyproject.toml
├── README.md
├── tests/
│   └── test_smoke.py
├── ruff.toml
└── .github/
    └── workflows/
        ├── verify.yml
        └── release.yml
```

你不需要手写插件目录结构，也不需要自己拼 GitHub Actions。下面继续使用生成的文件完成第一个功能。

## 6. 看懂插件配置

打开 `plugin/plugins/hello_world/plugin.toml`。CLI 已经写好插件身份和入口：

```toml
[plugin]
id = "hello_world"
name = "Hello World"
version = "0.1.0"
type = "plugin"
entry = "plugin.plugins.hello_world:HelloWorldPlugin"

[plugin.sdk]
recommended = ">=0.1.0,<0.2.0"
supported = ">=0.1.0,<0.3.0"
```

关键点：

- `id` 是插件的稳定身份，应与目录名一致。
- `name` 是插件管理器中显示的名称。
- `version` 是构建和发布时使用的版本。
- `entry` 告诉 N.E.K.O 从哪个 Python 模块加载哪个类，格式是 `模块路径:类名`。
- `[plugin.sdk]` 声明这个插件推荐和支持的 SDK 版本范围。

脚手架还会在源码目录中生成 `config.example.toml`，为新用户提供初始运行设置：

```toml
[plugin_runtime]
enabled = true
auto_start = false
```

`enabled = true` 表示插件默认可以加载。`auto_start = false` 表示它默认需要用户在插件管理器中手动启动。

第一次创建实际配置后，用户使用的是用户数据目录中的 `config/plugin.toml`。以后修改源码中的 `config.example.toml` 不会覆盖已经存在的用户配置。插件管理器中保存的启用和自动启动选择也会作为用户设置，在下次扫描插件时覆盖清单中的默认值。

## 7. 写第一个插件功能

打开 `plugin/plugins/hello_world/__init__.py`。它已经包含一个可以按名字问候用户的入口：

```python
from plugin.sdk.plugin import NekoPluginBase, Ok, neko_plugin, plugin_entry


@neko_plugin
class HelloWorldPlugin(NekoPluginBase):
    @plugin_entry(id="hello", name="Hello", description="Say hello")
    async def hello(self, name: str = "World", **_):
        return Ok({"message": f"Hello, {name}!"})
```

| 代码 | 作用 |
| --- | --- |
| `@neko_plugin` | 把这个类声明为 N.E.K.O 插件。 |
| `NekoPluginBase` | 提供日志、配置、存储等插件能力。 |
| `@plugin_entry(...)` | 在插件管理器中公开一个可以触发的功能。 |
| `async def hello(...)` | 定义这个功能执行时要做的事情。插件入口必须使用 `async def`。 |
| `name: str = "World"` | 声明一个可选的字符串参数；不填写时使用 `World`。 |
| `**_` | 接收 N.E.K.O 运行时附带的信息；这个功能不需要读取它。 |
| `Ok({...})` | 向 N.E.K.O 返回一次成功结果。 |

`plugin_id` 是 `hello_world`，这个功能自己的 `entry_id` 是 `hello`。前者用来找到插件，后者用来找到插件中的具体功能，两者不要互换。

::: tip 插件入口和 LLM 工具不是一回事
`@plugin_entry` 声明的是插件运行时入口。`@llm_tool` 注册的是对话期间使用的工具。第一次运行插件时只需要使用 `@plugin_entry`，不需要先配置 LLM 工具。
:::

现在把最后一行改成：

```python
return Ok({"message": f"你好，{name}！"})
```

保存后再次检查：

```bash
uv run neko-plugin check hello_world
```

## 8. 在 N.E.K.O 中运行

如果还没有启动源码版 N.E.K.O，请先按照[开发环境搭建](/zh-CN/guide/dev-setup)完成前端构建，然后在 N.E.K.O 根目录运行：

```bash
uv run python launcher.py
```

终端会显示本次启动的访问地址，请打开它显示的地址。进入“插件”页面后：

1. 点击右上角的“刷新”，让 N.E.K.O 重新扫描 `plugin/plugins/`。
2. 找到 **Hello World**，点击“启动”。
3. 打开插件详情，切换到“入口点”。
4. 找到 **Hello**，点击“触发”。
5. 输入一个名字，再次点击“触发”。

页面提示“触发成功”，说明插件已经正常运行。

## 9. 修改并重新载入

继续修改 `plugin/plugins/hello_world/` 中的代码。每次修改后的开发流程是：

1. 保存代码。
2. 运行检查：

   ```bash
   uv run neko-plugin check hello_world
   ```

3. 回到 Hello World 的插件详情页，点击“重载”。
4. 再次触发 **Hello**，验证修改后的行为。

“重载”会先停止当前插件，再从 `plugin/plugins/hello_world/` 读取刚保存的代码并重新启动。日常开发不需要构建安装包，也不需要在插件管理器中反复导入。

如果新建、删除或修改了 `plugin.toml`，请先点击插件列表右上角的“刷新”，让 N.E.K.O 重新读取插件配置；插件正在运行时，再点击“重载”。

## 10. 准备交付时再构建

当插件开发完成，准备交给其他用户安装时，才需要构建 `.neko-plugin` 安装包：

```bash
uv run neko-plugin build hello_world --out hello_world.neko-plugin
```

这个安装包用于用户安装或发布，不是日常开发时运行源码的必经步骤。把源码上传到 GitHub、提交审核和发布版本的完整流程请看[把插件发布到市场](/zh-CN/plugins/cli)。

## 接下来做什么

| 我想要…… | 看这里 |
| --- | --- |
| 把插件上传到 GitHub、提交审核并发布版本 | [把插件发布到市场](/zh-CN/plugins/cli) |
| 理解 `plugin.toml` | [插件配置](/zh-CN/plugins/plugin-toml) |
| 添加可调用的插件功能 | [入口与参数](/zh-CN/plugins/entries) |
| 在启动或关闭时执行代码 | [装饰器](/zh-CN/plugins/decorators) |
| 注册对话期 LLM 工具 | [LLM Tool Calling](/zh-CN/plugins/tool-calling) |
| 给插件制作 UI 面板 | [Hosted UI](/zh-CN/plugins/hosted-ui) |
| 查看真实插件示例 | [示例](/zh-CN/plugins/examples) |
| 正确处理错误 | [最佳实践](/zh-CN/plugins/best-practices) |
| 查询完整 SDK | [SDK 参考](/zh-CN/plugins/sdk-reference) |
