"""
[Shell 执行器] — 让猫娘直接执行 PowerShell / cmd 命令。

定位与 pi_agent 不同：
- run_shell：猫娘自己直接执行单条命令（快、直接）
- pi_run：把复杂编码任务交给独立 PiAgent 自主完成（慢、强）
"""

import asyncio
import os
import shutil
from typing import Any

from plugin.sdk.plugin import (
    NekoPluginBase,
    neko_plugin,
    llm_tool,
    lifecycle,
    plugin_entry,
    Ok,
)

_MAX_OUTPUT_CHARS = 6000
_DEFAULT_TIMEOUT = 60.0


def _decode_bytes(data: bytes) -> str:
    """先试 UTF-8（PowerShell 强制 UTF-8 输出），失败回退 GBK（cmd 默认）。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _find_powershell() -> str:
    """定位 PowerShell（优先 pwsh，回退 powershell）。"""
    for name in ("pwsh", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    return "powershell"


@neko_plugin
class ShellRunnerPlugin(NekoPluginBase):
    """Shell 执行器——执行 PowerShell/cmd 命令并返回输出。"""

    _ps = _find_powershell()

    # ── 生命周期 ───────────────────────────────────────────────────

    @lifecycle(id="startup")
    async def startup(self, **_):
        self.logger.info(f"Shell 执行器启动，PowerShell 位于: {self._ps}")
        return Ok({"status": "ok"})

    # ── 内部执行 ───────────────────────────────────────────────────

    async def _exec(
        self,
        shell: str,
        command: str,
        cwd: str | None,
        timeout: float,
    ) -> dict:
        workdir = cwd or os.path.expanduser("~")
        if not os.path.isdir(workdir):
            workdir = os.path.expanduser("~")

        if shell == "cmd":
            cmdline = ["cmd", "/c", command]
        else:
            # PowerShell：强制 UTF-8 输出，避免中文乱码
            ps_prefix = "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8;"
            cmdline = [self._ps, "-NoProfile", "-NonInteractive", "-Command", ps_prefix + command]

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmdline,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return {"ok": False, "error": f"命令执行超时（>{timeout:.0f}s），已终止"}
        except Exception as exc:
            return {"ok": False, "error": f"命令启动失败: {exc}"}

        out_text = _decode_bytes(stdout or b"").strip()
        err_text = _decode_bytes(stderr or b"").strip()
        exit_code = proc.returncode if proc is not None else -1

        combined = out_text or err_text
        if len(combined) > _MAX_OUTPUT_CHARS:
            combined = combined[:_MAX_OUTPUT_CHARS] + (
                f"\n...[输出过长已截断，完整长度 {len(combined)} 字符]"
            )

        return {
            "ok": exit_code == 0,
            "output": combined,
            "exit_code": exit_code,
            "error": None if exit_code == 0 else (err_text or f"退出码 {exit_code}"),
        }

    # ── LLM 工具 ───────────────────────────────────────────────────

    @llm_tool(
        name="run_shell",
        description=(
            "【Shell·命令执行】在用户电脑上直接执行 PowerShell 或 cmd 命令。\n\n"
            "适合：运行终端命令、查询系统信息、管理文件、执行脚本、安装软件、查看进程/服务、网络诊断。\n"
            "shell 参数：powershell（默认，功能强，支持管道/对象/模块）或 cmd（兼容旧命令）。\n"
            "command 写完整命令文本；需要指定运行目录时用 cwd（绝对路径）。\n"
            "timeout_seconds 默认 60，长任务可调大；超时会终止命令。\n"
            "⚠️ 安全：命令会直接在本机执行。破坏性操作（删除文件、格式化、改注册表、下载并运行不明脚本、shutdown）必须先向用户确认，用户明确同意后才能执行。\n"
            "普通查询类命令（dir、ipconfig、systeminfo、Get-Process 等）可直接执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "shell": {
                    "type": "string",
                    "enum": ["powershell", "cmd"],
                    "description": "解释器：powershell（默认）或 cmd",
                },
                "command": {
                    "type": "string",
                    "description": "要执行的完整命令文本",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录（绝对路径），默认用户主目录",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "超时秒数，默认 60，范围 5-600",
                },
            },
            "required": ["command"],
        },
        timeout=130.0,
    )
    async def run_shell(
        self,
        *,
        command: str,
        shell: str = "powershell",
        cwd: str = "",
        timeout_seconds: int = 60,
        **_,
    ):
        if shell not in ("powershell", "cmd"):
            shell = "powershell"
        timeout = float(max(5, min(int(timeout_seconds or 60), 600)))
        result = await self._exec(shell, command, cwd or None, timeout)
        if result.get("error"):
            return {
                "output": (
                    f"命令失败（退出码 {result['exit_code']}）：{result['error']}\n\n"
                    f"输出：{result['output']}"
                ),
                "is_error": True,
            }
        return {"output": result["output"], "is_error": False}

    # ── 面板操作 ───────────────────────────────────────────────────

    @plugin_entry(
        id="shell_test",
        name="Shell 执行器自检",
        description="执行 echo 验证 Shell 执行器可用。",
    )
    async def shell_test(self, **_):
        result = await self._exec("powershell", "Write-Output 'shell-ok'", None, 15.0)
        if result.get("ok"):
            return Ok({"status": "ok", "message": f"Shell 执行器可用: {result['output']}"})
        return Ok({"status": "error", "message": f"Shell 执行器异常: {result.get('error')}"})
