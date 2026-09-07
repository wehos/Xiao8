"""
日志路由
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, WebSocket
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from plugin.logging_config import get_logger
from plugin.server.application.logs import LogQueryService
from plugin.server.logs import list_plugin_log_files_for_export
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.auth import require_admin
from plugin.server.logs import log_stream_endpoint
from plugin.server.infrastructure.error_mapping import raise_http_from_domain

router = APIRouter()
logger = get_logger("server.routes.logs")
log_query_service = LogQueryService()


def file_iterator(file_path: Path, chunk_size: int = 65536):
    """流式读取文件，避免一次性加载到内存"""
    with open(file_path, 'rb') as f:
        while chunk := f.read(chunk_size):
            yield chunk


def cleanup_temp_path(path: str):
    """清理临时文件或目录"""
    try:
        p = Path(path)
        if p.is_file():
            p.unlink(missing_ok=True)
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Failed to cleanup temp path {path}: {e}")



@router.get("/plugin/{plugin_id}/logs")
async def get_plugin_logs_endpoint(
    plugin_id: str,
    lines: int = Query(default=100, ge=1, le=10000),
    level: Optional[str] = Query(default=None, description="日志级别: DEBUG, INFO, WARNING, ERROR"),
    start_time: Optional[str] = Query(default=None),
    end_time: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None, description="关键词搜索"),
    _: str = require_admin,
) -> dict[str, object]:
    try:
        return log_query_service.get_plugin_logs(
            plugin_id=plugin_id,
            lines=lines,
            level=level,
            start_time=start_time,
            end_time=end_time,
            search=search,
        )
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.get("/plugin/{plugin_id}/logs/files")
async def get_plugin_log_files_endpoint(plugin_id: str, _: str = require_admin) -> dict[str, object]:
    try:
        return log_query_service.get_plugin_log_files(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.get("/plugin/{plugin_id}/logs/directory")
async def get_plugin_log_directory_endpoint(plugin_id: str, _: str = require_admin) -> dict[str, object]:
    try:
        return log_query_service.get_plugin_log_directory(plugin_id)
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.get("/plugin/{plugin_id}/logs/export")
async def export_plugin_log_endpoint(plugin_id: str, _: str = require_admin) -> StreamingResponse:
    try:
        # 拒绝包含换行符的 plugin_id，防止 HTTP 响应头注入
        if '\n' in plugin_id or '\r' in plugin_id:
            raise ServerDomainError(
                code="INVALID_PLUGIN_ID",
                message="Plugin ID cannot contain newline characters",
                status_code=400,
                details={"plugin_id": plugin_id}
            )

        # 获取日志目录
        result = log_query_service.get_plugin_log_directory(plugin_id)
        log_dir = Path(result["directory"])

        # 创建临时目录用于打包
        temp_dir = Path(tempfile.mkdtemp())
        temp_log_dir = temp_dir / "logs"
        temp_log_dir.mkdir()

        # 确保异常路径也会清理临时目录
        temp_needs_cleanup = True

        try:
            # 只收集当前插件自己的日志文件：logs/plugin/ 是所有插件共享的，
            # 直接打包整个目录会把别的插件日志一起泄漏出去。
            log_files = list_plugin_log_files_for_export(log_dir, plugin_id)

            if not log_files:
                raise ServerDomainError(
                    code="NO_LOG_FILES",
                    message="No log files found for this plugin",
                    status_code=404,
                    details={"plugin_id": plugin_id}
                )

            # 在线程池中执行文件复制和打包，避免阻塞事件循环
            import asyncio
            loop = asyncio.get_event_loop()

            def _copy_and_archive():
                """在线程池中执行同步文件操作"""
                copied_count = 0
                for log_file in log_files:
                    shutil.copy2(log_file, temp_log_dir / log_file.name)
                    copied_count += 1

                # 打包临时日志目录
                # 使用固定文件名避免超长 plugin_id 导致超过文件系统 NAME_MAX 限制
                zip_path = temp_dir / "export"
                archive_path = shutil.make_archive(
                    str(zip_path),
                    'zip',
                    temp_log_dir
                )
                return archive_path

            archive_path = await loop.run_in_executor(None, _copy_and_archive)

            # 成功创建响应，清理责任移交给 BackgroundTask
            temp_needs_cleanup = False

            # 使用流式响应 + 后台清理任务（复用主程序模式）
            return StreamingResponse(
                file_iterator(Path(archive_path)),
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{plugin_id}_logs.zip"'},
                background=BackgroundTask(cleanup_temp_path, str(temp_dir))
            )
        finally:
            # 如果在复制、打包或构造响应时失败，立即清理临时目录
            if temp_needs_cleanup:
                cleanup_temp_path(str(temp_dir))
    except ServerDomainError as error:
        raise_http_from_domain(error, logger=logger)


@router.websocket("/ws/logs/{plugin_id}")
async def websocket_log_stream(websocket: WebSocket, plugin_id: str) -> None:
    await log_stream_endpoint(websocket, plugin_id)
