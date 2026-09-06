"""Atomic storage of plugin model settings in the selected runtime root."""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import ValidationError

from plugin.server.domain.errors import ServerDomainError
from plugin.server.domain.model_config import PluginModelsConfig
from utils.file_utils import read_json_tolerating_replace

CONFIG_FILENAME = "plugin_models.json"
_write_lock = threading.RLock()
T = TypeVar("T")


class ModelConfigStore:
    def __init__(self, config_manager: Any = None):
        self._config_manager = config_manager

    def _manager(self):
        if self._config_manager is not None:
            return self._config_manager
        from utils.config_manager import get_config_manager

        return get_config_manager()

    def _read(self, cm) -> PluginModelsConfig:
        path = cm.get_runtime_config_path(CONFIG_FILENAME)
        try:
            raw = read_json_tolerating_replace(path)
        except FileNotFoundError:
            return PluginModelsConfig()
        except (OSError, ValueError, UnicodeError) as exc:
            raise ServerDomainError(
                "MODEL_CONFIG_READ_FAILED", "Plugin model configuration could not be read", 500
            ) from exc
        try:
            return PluginModelsConfig.model_validate(raw)
        except ValidationError as exc:
            # Never include validation inputs: this document contains credentials.
            raise ServerDomainError(
                "MODEL_CONFIG_INVALID", "Plugin model configuration is invalid; existing file was preserved", 500
            ) from exc

    def read(self) -> PluginModelsConfig:
        return self._read(self._manager())

    def update(self, change: Callable[[PluginModelsConfig], T]) -> T:
        from utils.cloudsave_runtime import cloudsave_writable_transaction

        cm = self._manager()
        failure = None
        # The transaction coordinates with cross-process storage maintenance.
        # The local lock also covers concurrent writes when cloudsave is disabled.
        with _write_lock, cloudsave_writable_transaction(cm, operation="save", target=CONFIG_FILENAME):
            try:
                config = self._read(cm)
                result = change(config)
                try:
                    validated = PluginModelsConfig.model_validate(config.model_dump())
                except ValidationError as exc:
                    raise ServerDomainError(
                        "MODEL_CONFIG_INVALID", "Invalid model slot or binding references", 400
                    ) from exc
                try:
                    cm.save_json_config(CONFIG_FILENAME, validated.model_dump(mode="json"))
                except OSError as exc:
                    raise ServerDomainError(
                        "MODEL_CONFIG_WRITE_FAILED", "Plugin model configuration could not be saved", 500
                    ) from exc
            except ServerDomainError as exc:
                # The shared domain exception is a frozen dataclass. contextlib
                # cannot assign its traceback while unwinding a generator-based
                # transaction, so propagate it only after the transaction exits.
                failure = exc
        if failure is not None:
            raise failure
        return result
