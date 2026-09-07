# Shell 执行器 (shell_runner)

让猫娘直接在用户电脑上执行 **PowerShell / cmd** 命令并返回输出。

## 功能

- **`run_shell`**（LLM 工具）：执行单条命令，支持选择解释器（powershell / cmd）、指定工作目录、超时控制，返回 stdout + 退出码
- **`shell_test`**（插件面板）：自检（`Write-Output 'shell-ok'`），确认执行器可用

## 设计要点

- **PowerShell 中文乱码**：命令前自动注入 `$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8`，保证 UTF-8 输出
- **cmd 编码**：输出先按 UTF-8 解码，失败回退 GBK（Windows 默认代码页）
- **安全**：工具描述中明确要求——破坏性操作（删除、格式化、改注册表、shutdown 等）必须先向用户确认；查询类命令可直接执行
- **超时**：默认 60s，可调 5-600s，超时自动终止进程

## 使用示例

```text
猫娘，查一下现在有什么进程在跑
→ run_shell(shell="powershell", command="Get-Process | Select-Object -First 10 Name, CPU")

猫娘，我的 IP 是多少
→ run_shell(command="ipconfig")
```

## 与同类能力的区别

| 能力 | 适用场景 |
|------|----------|
| `run_shell`（本插件） | 猫娘自己直接执行单条命令，快、直接 |
| computer_use | 操作 GUI（截图 + 键鼠模拟），慢 |
| 编码 agent 插件 | 复杂多步开发任务，交给独立 agent 自主完成 |
