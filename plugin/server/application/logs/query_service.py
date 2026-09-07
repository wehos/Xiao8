from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException

from plugin.logging_config import get_logger
from plugin.server.domain import IO_RUNTIME_ERRORS
from plugin.server.domain.errors import ServerDomainError
from plugin.utils.time_utils import now_iso
from plugin.server.logs import get_plugin_log_files, get_plugin_logs, get_plugin_log_directory_path

logger = get_logger("server.application.logs.query")


def _normalize_mapping(raw: Mapping[object, object], *, context: str) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ServerDomainError(
                code="INVALID_DATA_SHAPE",
                message=f"{context} contains non-string key",
                status_code=500,
                details={"key_type": type(key).__name__},
            )
        normalized[key] = value
    return normalized


def _normalize_mapping_list(raw_items: list[object], *, context: str) -> list[dict[str, object]]:
    normalized_items: list[dict[str, object]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise ServerDomainError(
                code="INVALID_DATA_SHAPE",
                message=f"{context} item is not an object",
                status_code=500,
                details={"index": index, "item_type": type(item).__name__},
            )
        normalized_items.append(_normalize_mapping(item, context=f"{context}[{index}]"))
    return normalized_items


def _detail_to_message(detail: object, *, default_message: str) -> str:
    if isinstance(detail, str) and detail:
        return detail
    return default_message


class LogQueryService:
    def get_plugin_logs(
        self,
        *,
        plugin_id: str,
        lines: int,
        level: str | None,
        start_time: str | None,
        end_time: str | None,
        search: str | None,
    ) -> dict[str, object]:
        try:
            raw_result = get_plugin_logs(
                plugin_id=plugin_id,
                lines=lines,
                level=level,
                start_time=start_time,
                end_time=end_time,
                search=search,
            )
            if not isinstance(raw_result, Mapping):
                raise ServerDomainError(
                    code="INVALID_DATA_SHAPE",
                    message="plugin logs result is not an object",
                    status_code=500,
                    details={
                        "plugin_id": plugin_id,
                        "result_type": type(raw_result).__name__,
                    },
                )

            result = _normalize_mapping(raw_result, context=f"plugin_logs[{plugin_id}]")
            logs_obj = result.get("logs")
            if isinstance(logs_obj, list):
                result["logs"] = _normalize_mapping_list(logs_obj, context=f"plugin_logs[{plugin_id}].logs")
            return result
        except ServerDomainError:
            raise
        except HTTPException as exc:
            logger.warning(
                "get_plugin_logs failed with HTTPException: plugin_id={}, status_code={}, detail={}",
                plugin_id,
                exc.status_code,
                str(exc.detail),
            )
            raise ServerDomainError(
                code="PLUGIN_LOG_QUERY_FAILED",
                message=_detail_to_message(exc.detail, default_message="Failed to get plugin logs"),
                status_code=exc.status_code,
                details={"plugin_id": plugin_id, "error_type": "HTTPException"},
            ) from exc
        except IO_RUNTIME_ERRORS as exc:
            logger.error(
                "get_plugin_logs failed: plugin_id={}, err_type={}, err={}",
                plugin_id,
                type(exc).__name__,
                str(exc),
            )
            raise ServerDomainError(
                code="PLUGIN_LOG_QUERY_FAILED",
                message="Failed to get plugin logs",
                status_code=500,
                details={
                    "plugin_id": plugin_id,
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def get_plugin_log_files(self, plugin_id: str) -> dict[str, object]:
        try:
            raw_files = get_plugin_log_files(plugin_id)
            if not isinstance(raw_files, list):
                raise ServerDomainError(
                    code="INVALID_DATA_SHAPE",
                    message="plugin log files result is not an array",
                    status_code=500,
                    details={
                        "plugin_id": plugin_id,
                        "result_type": type(raw_files).__name__,
                    },
                )

            files = _normalize_mapping_list(raw_files, context=f"plugin_log_files[{plugin_id}]")
            return {
                "plugin_id": plugin_id,
                "log_files": files,
                "count": len(files),
                "time": now_iso(),
            }
        except ServerDomainError:
            raise
        except HTTPException as exc:
            logger.warning(
                "get_plugin_log_files failed with HTTPException: plugin_id={}, status_code={}, detail={}",
                plugin_id,
                exc.status_code,
                str(exc.detail),
            )
            raise ServerDomainError(
                code="PLUGIN_LOG_FILES_QUERY_FAILED",
                message=_detail_to_message(exc.detail, default_message="Failed to get plugin log files"),
                status_code=exc.status_code,
                details={"plugin_id": plugin_id, "error_type": "HTTPException"},
            ) from exc
        except IO_RUNTIME_ERRORS as exc:
            logger.error(
                "get_plugin_log_files failed: plugin_id={}, err_type={}, err={}",
                plugin_id,
                type(exc).__name__,
                str(exc),
            )
            raise ServerDomainError(
                code="PLUGIN_LOG_FILES_QUERY_FAILED",
                message="Failed to get plugin log files",
                status_code=500,
                details={
                    "plugin_id": plugin_id,
                    "error_type": type(exc).__name__,
                },
            ) from exc

    def get_plugin_log_directory(self, plugin_id: str) -> dict[str, object]:
        try:
            directory_path = get_plugin_log_directory_path(plugin_id)
            return {
                "plugin_id": plugin_id,
                "directory": directory_path,
                "time": now_iso(),
            }
        except ServerDomainError:
            raise
        except HTTPException as exc:
            logger.warning(
                "get_plugin_log_directory failed with HTTPException: plugin_id={}, status_code={}, detail={}",
                plugin_id,
                exc.status_code,
                str(exc.detail),
            )
            raise ServerDomainError(
                code="PLUGIN_LOG_DIRECTORY_QUERY_FAILED",
                message=_detail_to_message(exc.detail, default_message="Failed to get plugin log directory"),
                status_code=exc.status_code,
                details={"plugin_id": plugin_id, "error_type": "HTTPException"},
            ) from exc
        except IO_RUNTIME_ERRORS as exc:
            logger.error(
                "get_plugin_log_directory failed: plugin_id={}, err_type={}, err={}",
                plugin_id,
                type(exc).__name__,
                str(exc),
            )
            raise ServerDomainError(
                code="PLUGIN_LOG_DIRECTORY_QUERY_FAILED",
                message="Failed to get plugin log directory",
                status_code=500,
                details={
                    "plugin_id": plugin_id,
                    "error_type": type(exc).__name__,
                },
            ) from exc
