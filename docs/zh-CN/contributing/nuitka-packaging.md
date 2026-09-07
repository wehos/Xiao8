
# Nuitka 打包

当前可追踪的打包契约是桌面 workflow 及其准备、校验脚本。仓库中没有受跟踪的 `build_nuitka.bat`，因此不要把它写成第二个权威入口。

## Python 包命名

含可导入 `.py` 的目录使用下划线名称并包含 `__init__.py`。连字符包名不符合普通 Python 命名，也会干扰数据包含。`tests/unit/test_no_hyphen_python_packages.py` 会检查此规则。

不要用 `--include-data-dir` 分发可导入 Python。Nuitka 会从数据目录过滤类代码后缀。应正常编译 package；只有存在明确运行时契约的解释型/沙箱源码 payload 才作为原始数据包含。

## 内置插件 staging

当前桌面 workflow 依次执行：

1. `scripts/prepare_nuitka_plugins.py prepare`
2. 编译生成的 `build_nuitka_launcher.py`
3. 用 `scripts/prepare_nuitka_plugins.py install` 安装到构建分发目录
4. 执行 `scripts/check_nuitka_dist.py <dist> --plugin-stage build/nuitka-plugins`

Staging 脚本按各插件的 `[tool.neko.build]` 规则处理并生成选择性排除。不要恢复全量 `--include-data-dir=plugin/plugins=plugin/plugins` 或全量 `--nofollow-import-to=plugin.plugins`；两者都会以不同方式绕过 staging 契约。

## 资源与动态导入

新增运行时资源或动态导入可能需要协调修改：

- 跨平台和 Linux-only workflow 的 Nuitka include 选项；
- 插件 payload 的 `scripts/prepare_nuitka_plugins.py`；
- `scripts/check_nuitka_dist.py` 的必要资源验证；
- 定向 import/package 测试。

Embedding 与 tiktoken 资源由 workflow 分别准备和验证。

## 安全诊断

打包后的 launcher 会启动多个服务。只终止父进程可能留下子进程占用 `dist/Xiao8`。启动前优先做静态检查：

```bash
uv run python scripts/check_nuitka_dist.py dist/Xiao8 --plugin-stage build/nuitka-plugins
uv run pytest tests/unit/test_no_hyphen_python_packages.py -q
```

确需运行打包产物时，记录精确 artifact/revision，并在重建前关闭所有子服务。不要用未经审查的递归删除处理被旧进程锁定的分发目录。
