from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import threading
from types import MappingProxyType

from plugin.config.schema import PluginInstallSchema
from plugin.core.state import state
from plugin.logging_config import get_logger


logger = get_logger("server.install_registry")


@dataclass(frozen=True)
class InstallKindRegistration:
    entry_id: str
    label: str
    queued_message: str
    entry_timeout: float = 600.0


@dataclass(frozen=True)
class InstallPluginRegistration:
    plugin_id: str
    install_kinds: Mapping[str, InstallKindRegistration]
    ui_i18n_dir: Path | None = None
    tutorial_enabled: bool = False
    config_path: Path | None = None
    effective_source: str | None = None


@dataclass(frozen=True)
class _SelectedPluginState:
    config_path: str
    entry_ids: frozenset[str]
    runtime_load_state: str | None
    effective_source: str | None


_install_plugin_registry: dict[str, InstallPluginRegistration] = {}
_tutorial_migration_hooks: dict[str, list[Callable[[Path], None]]] | list[Callable[[Path], None]] = {}
_registry_lock = threading.RLock()


def normalize_registered_plugin_id(plugin_id: str) -> str:
    normalized = str(plugin_id or "").strip()
    if not normalized or ".." in normalized or "/" in normalized or "\\" in normalized:
        raise ValueError(f"invalid plugin id: {plugin_id!r}")
    return normalized


def register_install_plugin(
    plugin_id: str,
    *,
    install_kinds: Mapping[str, InstallKindRegistration],
    ui_i18n_dir: Path | str | None = None,
    tutorial_enabled: bool = False,
) -> None:
    """Register the legacy in-process install API used by third-party plugins."""

    normalized_plugin_id = normalize_registered_plugin_id(plugin_id)
    normalized_kinds: dict[str, InstallKindRegistration] = {}
    for raw_kind, registration in install_kinds.items():
        normalized_kind = str(raw_kind or "").strip().lower()
        if not normalized_kind:
            raise ValueError("install kind must not be empty")
        if not isinstance(registration, InstallKindRegistration):
            raise TypeError("install kind registrations must use InstallKindRegistration")
        if not str(registration.entry_id or "").strip():
            raise ValueError(f"install entry_id for kind {normalized_kind!r} must not be empty")
        normalized_kinds[normalized_kind] = registration

    registration = InstallPluginRegistration(
        plugin_id=normalized_plugin_id,
        install_kinds=MappingProxyType(normalized_kinds),
        ui_i18n_dir=Path(ui_i18n_dir).resolve() if ui_i18n_dir is not None else None,
        tutorial_enabled=bool(tutorial_enabled),
    )
    with _registry_lock:
        _install_plugin_registry[normalized_plugin_id] = registration


def _selected_plugin_state(plugin_id: str) -> _SelectedPluginState | None:
    with state.acquire_plugins_read_lock():
        raw_meta = state.plugins.get(plugin_id)
        if not isinstance(raw_meta, dict):
            return None
        config_path = raw_meta.get("config_path")
        if not isinstance(config_path, str) or not config_path:
            return None
        entries_preview = raw_meta.get("entries_preview")
        entries = entries_preview if isinstance(entries_preview, list) else []
        entry_ids = frozenset(
            entry_id
            for item in entries
            if isinstance(item, dict)
            for entry_id in (item.get("id"),)
            if isinstance(entry_id, str) and entry_id
        )
        runtime_load_state = raw_meta.get("runtime_load_state")
        effective_source = raw_meta.get("effective_source")
        return _SelectedPluginState(
            config_path=config_path,
            entry_ids=entry_ids,
            runtime_load_state=(
                runtime_load_state if isinstance(runtime_load_state, str) else None
            ),
            effective_source=(
                effective_source if isinstance(effective_source, str) else None
            ),
        )


def _load_selected_manifest(config_path: Path) -> tuple[dict[str, object], bool]:
    resolved = config_path.resolve(strict=True)
    if resolved.name != "plugin.toml" or not resolved.is_file():
        raise ValueError("selected plugin config_path must point to plugin.toml")
    with resolved.open("rb") as file_obj:
        manifest = tomllib.load(file_obj)
    plugin_table = manifest.get("plugin")
    if not isinstance(plugin_table, dict):
        raise ValueError("selected plugin manifest has no [plugin] table")
    return plugin_table, "install" in plugin_table


def _resolve_i18n_dir(plugin_root: Path, relative_value: str | None) -> Path | None:
    if relative_value is None:
        return None
    if not relative_value or relative_value.strip() != relative_value:
        raise ValueError("ui_i18n_dir must be non-empty and unpadded")
    relative = Path(relative_value)
    posix_path = PurePosixPath(relative_value)
    windows_path = PureWindowsPath(relative_value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        raise ValueError("ui_i18n_dir must stay within the selected plugin root")
    resolved_root = plugin_root.resolve(strict=True)
    resolved = (resolved_root / relative).resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("ui_i18n_dir escapes the selected plugin root") from exc
    if not resolved.is_dir():
        raise ValueError("ui_i18n_dir must point to an existing directory")
    return resolved


def _validate_entry_ids(
    install_kinds: Mapping[str, InstallKindRegistration],
    selected: _SelectedPluginState,
) -> None:
    missing = sorted(
        registration.entry_id
        for registration in install_kinds.values()
        if registration.entry_id not in selected.entry_ids
    )
    if missing:
        raise ValueError(
            "install declaration references entries absent from selected metadata: "
            + ", ".join(missing)
        )


def _explicit_registration(
    plugin_id: str,
    *,
    config_path: Path,
    selected: _SelectedPluginState,
    install_table: object,
) -> InstallPluginRegistration | None:
    declaration = PluginInstallSchema.model_validate(install_table)
    if not declaration.enabled:
        return None
    kinds = MappingProxyType(
        {
            kind: InstallKindRegistration(
                entry_id=spec.entry_id,
                label=spec.label,
                queued_message=spec.queued_message,
                entry_timeout=spec.entry_timeout,
            )
            for kind, spec in declaration.kinds.items()
        }
    )
    _validate_entry_ids(kinds, selected)
    return InstallPluginRegistration(
        plugin_id=plugin_id,
        install_kinds=kinds,
        ui_i18n_dir=_resolve_i18n_dir(
            config_path.parent,
            declaration.ui_i18n_dir,
        ),
        tutorial_enabled=declaration.tutorial_enabled,
        config_path=config_path,
        effective_source=selected.effective_source,
    )


def _dynamic_registration(
    plugin_id: str,
    *,
    config_path: Path,
    selected: _SelectedPluginState,
) -> InstallPluginRegistration | None:
    with _registry_lock:
        registration = _install_plugin_registry.get(plugin_id)
    if registration is None:
        return None
    return InstallPluginRegistration(
        plugin_id=registration.plugin_id,
        install_kinds=registration.install_kinds,
        ui_i18n_dir=registration.ui_i18n_dir,
        tutorial_enabled=registration.tutorial_enabled,
        config_path=config_path,
        effective_source=selected.effective_source,
    )


def _study_legacy_registration(
    plugin_id: str,
    *,
    config_path: Path,
    selected: _SelectedPluginState,
) -> InstallPluginRegistration | None:
    if plugin_id != "study_companion":
        return None
    kinds = MappingProxyType(
        {
            "rapidocr_models": InstallKindRegistration(
                entry_id="study_download_rapidocr_models",
                label="RapidOCR Models",
                queued_message="RapidOCR model download queued",
            )
        }
    )
    _validate_entry_ids(kinds, selected)
    return InstallPluginRegistration(
        plugin_id=plugin_id,
        install_kinds=kinds,
        ui_i18n_dir=_resolve_i18n_dir(config_path.parent, "i18n"),
        tutorial_enabled=True,
        config_path=config_path,
        effective_source=selected.effective_source,
    )


def _galgame_legacy_registration(
    plugin_id: str,
    *,
    config_path: Path,
    selected: _SelectedPluginState,
) -> InstallPluginRegistration | None:
    if plugin_id != "galgame_plugin":
        return None
    kinds = MappingProxyType(
        {
            "textractor": InstallKindRegistration(
                entry_id="galgame_install_textractor",
                label="Textractor",
                queued_message="Textractor install queued",
                entry_timeout=600.0,
            ),
            "rapidocr_models": InstallKindRegistration(
                entry_id="galgame_download_rapidocr_models",
                label="RapidOCR Models",
                queued_message="RapidOCR model download queued",
                entry_timeout=600.0,
            ),
        }
    )
    _validate_entry_ids(kinds, selected)
    return InstallPluginRegistration(
        plugin_id=plugin_id,
        install_kinds=kinds,
        ui_i18n_dir=_resolve_i18n_dir(config_path.parent, "i18n/ui"),
        tutorial_enabled=True,
        config_path=config_path,
        effective_source=selected.effective_source,
    )


def _registration_for_selected_source(
    plugin_id: str,
    selected: _SelectedPluginState,
) -> InstallPluginRegistration | None:
    if selected.runtime_load_state == "failed":
        return None
    config_path = Path(selected.config_path).resolve(strict=True)
    plugin_table, has_install_declaration = _load_selected_manifest(config_path)
    if has_install_declaration:
        return _explicit_registration(
            plugin_id,
            config_path=config_path,
            selected=selected,
            install_table=plugin_table.get("install"),
        )
    for legacy_resolver in (
        _study_legacy_registration,
        _galgame_legacy_registration,
    ):
        legacy_registration = legacy_resolver(
            plugin_id,
            config_path=config_path,
            selected=selected,
        )
        if legacy_registration is not None:
            return legacy_registration
    return _dynamic_registration(
        plugin_id,
        config_path=config_path,
        selected=selected,
    )

def get_install_plugin_registration(plugin_id: str) -> InstallPluginRegistration | None:
    """Resolve install metadata from the source selected by the main registry.

    Manifest parsing intentionally is not cached. A source switch can therefore
    never keep serving install metadata from an older plugin directory. If the
    selected source changes while the manifest is being read, the complete read
    is retried once and then fails closed.
    """

    normalized_plugin_id = normalize_registered_plugin_id(plugin_id)
    for attempt in range(2):
        selected = _selected_plugin_state(normalized_plugin_id)
        if selected is None or selected.runtime_load_state == "failed":
            return None
        try:
            registration = _registration_for_selected_source(
                normalized_plugin_id,
                selected,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if attempt == 0 and _selected_plugin_state(normalized_plugin_id) != selected:
                logger.info(
                    "selected source changed while rejecting install declaration for {}; retrying",
                    normalized_plugin_id,
                )
                continue
            logger.warning(
                "install declaration rejected for selected plugin {}: {}",
                normalized_plugin_id,
                exc,
            )
            raise ValueError(
                f"install declaration rejected for {normalized_plugin_id}: {exc}"
            ) from exc

        if _selected_plugin_state(normalized_plugin_id) == selected:
            return registration
        if attempt == 0:
            logger.info(
                "selected source changed while reading install declaration for {}; retrying",
                normalized_plugin_id,
            )
    logger.warning(
        "selected source kept changing while reading install declaration for {}; failing closed",
        normalized_plugin_id,
    )
    return None



def register_tutorial_migration_hook(
    hook: Callable[[Path], None],
    *,
    plugin_id: str = "",
) -> None:
    normalized_plugin_id = normalize_registered_plugin_id(plugin_id) if plugin_id else ""
    with _registry_lock:
        # Some third-party tests and older plugins still patch the pre-plugin
        # list shape. Preserve that compatibility while production uses a map.
        if isinstance(_tutorial_migration_hooks, list):
            if hook not in _tutorial_migration_hooks:
                _tutorial_migration_hooks.append(hook)
            return
        hooks = _tutorial_migration_hooks.setdefault(normalized_plugin_id, [])
        if hook not in hooks:
            hooks.append(hook)


def tutorial_migration_hooks_for(plugin_id: str) -> list[Callable[[Path], None]]:
    normalized_plugin_id = normalize_registered_plugin_id(plugin_id) if plugin_id else ""
    with _registry_lock:
        if isinstance(_tutorial_migration_hooks, list):
            return list(_tutorial_migration_hooks)
        return [
            *_tutorial_migration_hooks.get("", []),
            *_tutorial_migration_hooks.get(normalized_plugin_id, []),
        ]
