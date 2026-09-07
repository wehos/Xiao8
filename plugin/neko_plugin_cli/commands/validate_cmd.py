"""Internal plugin source repository validation helpers."""

from __future__ import annotations

import ast
import json
import math
import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath

from plugin.core.python_dependencies import (
    collect_project_python_requirements,
    find_missing_python_requirements,
    split_host_provided_requirements,
)
from plugin._types.plugin_types import (
    SUPPORTED_PLUGIN_TYPES,
    format_plugin_type_choice_error,
    format_removed_plugin_host,
)
from plugin.sdk.shared.core.push_message_schema import (
    LEGACY_PUSH_MESSAGE_FIELD_MIGRATIONS,
    LEGACY_PUSH_MESSAGE_POSITIONAL_FIELDS,
    LEGACY_PUSH_MESSAGE_TRUTHY_ONLY_FIELDS,
    format_push_message_v1_static_diagnostic,
)

from ..core.plugin_source import load_plugin_source
from ..core.toml_utils import load_toml

_PLUGIN_RUNTIME_TIMEOUT_MAX = 300.0


def validate_plugin_dir(
    plugin_dir: Path,
    *,
    strict: bool = False,
    require_matching_directory_name: bool = False,
) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    config_example_path = plugin_dir / "config.example.toml"
    config_example: dict[str, object] | None = None
    if config_example_path.is_file():
        try:
            config_example = load_toml(config_example_path)
        except Exception as exc:
            issues.append(
                (
                    "error",
                    "config.example.toml could not be read / "
                    "无法读取 config.example.toml / "
                    f"config.example.toml を読み取れません: {exc}",
                )
            )
            return issues
    try:
        source = load_plugin_source(plugin_dir)
    except Exception:
        plugin_toml_path = plugin_dir / "plugin.toml"
        try:
            config = load_toml(plugin_toml_path)
        except Exception as exc:
            issues.append(("error", f"plugin.toml could not be read: {exc}"))
            return issues
        plugin_table = config.get("plugin") if isinstance(config, dict) else {}
        plugin_id = plugin_table.get("id") if isinstance(plugin_table, dict) else plugin_dir.name
        plugin_id = plugin_id if isinstance(plugin_id, str) and plugin_id.strip() else plugin_dir.name
        _check_plugin_toml_schema(plugin_dir, config, plugin_id.strip(), issues)
        return issues
    plugin_table = source.plugin_table
    _check_plugin_toml_schema(plugin_dir, source.plugin_toml, source.plugin_id, issues)
    if require_matching_directory_name and source.plugin_id != plugin_dir.name:
        issues.append(
            (
                "warning",
                f"plugin.id '{source.plugin_id}' does not match directory name '{plugin_dir.name}' / "
                f"plugin.id '{source.plugin_id}' 与目录名 '{plugin_dir.name}' 不一致 / "
                f"plugin.id '{source.plugin_id}' がディレクトリ名 '{plugin_dir.name}' と一致しません",
            )
        )
    if config_example is not None:
        _check_config_example_schema(config_example, issues)
    elif "plugin_runtime" in source.plugin_toml or source.plugin_id in source.plugin_toml:
        issues.append(
            (
                "warning",
                "legacy runtime configuration in plugin.toml is supported; "
                "move mutable defaults to config.example.toml / "
                "仍兼容 plugin.toml 中的旧运行配置；请将可变默认值移到 config.example.toml / "
                "plugin.toml の従来の実行設定は互換対応です。可変の既定値を config.example.toml に移してください",
            )
        )

    entry = source.entry_point
    if not entry:
        issues.append(("error", "plugin.entry is missing"))
    else:
        expected_module = f"plugin.plugins.{source.plugin_id}"
        entry_module = entry.split(":", 1)[0].strip()
        if entry_module != expected_module and not entry_module.startswith(expected_module + "."):
            issues.append((
                "warning",
                f"plugin.entry should usually target '{expected_module}', got '{entry}'",
            ))
        _check_entry_target(plugin_dir, source.plugin_id, entry, source.package_type, issues)

    if not plugin_table.get("sdk"):
        issues.append(("warning", "[plugin.sdk] is missing"))

    _check_optional_file(plugin_dir / "README.md", "README.md", issues, strict=strict)
    _check_optional_file(plugin_dir / "tests" / "test_smoke.py", "tests/test_smoke.py", issues, strict=strict)
    _check_optional_file(plugin_dir / "pyproject.toml", "pyproject.toml", issues, strict=False)
    _check_requirements_file(plugin_dir / "requirements.txt", issues)
    _check_pyproject_dependency_layout(plugin_dir, source.pyproject_toml, source.package_type, issues)

    _check_json_file(plugin_dir / ".vscode" / "settings.json", ".vscode/settings.json", issues, strict=strict)
    _check_json_file(plugin_dir / ".vscode" / "tasks.json", ".vscode/tasks.json", issues, strict=strict)
    _check_optional_file(plugin_dir / ".github" / "workflows" / "verify.yml", ".github/workflows/verify.yml", issues, strict=strict)

    _check_gitignore(plugin_dir / ".gitignore", issues, strict=strict)
    _check_python_decorators(plugin_dir, issues)

    return issues


def _check_config_example_schema(
    config: dict[str, object],
    issues: list[tuple[str, str]],
) -> None:
    if "plugin" in config:
        issues.append(
            (
                "error",
                "config.example.toml must not contain [plugin] / "
                "config.example.toml 不能包含 [plugin] / "
                "config.example.toml に [plugin] を含めることはできません",
            )
        )
    _check_runtime_table(config.get("plugin_runtime"), issues)


def _check_plugin_toml_schema(
    plugin_dir: Path,
    config: dict[str, object],
    plugin_id: str,
    issues: list[tuple[str, str]],
) -> None:
    plugin_table = config.get("plugin")
    if not isinstance(plugin_table, dict):
        issues.append(("error", "[plugin] must be a table"))
        return

    allowed_plugin_keys = {
        "id",
        "name",
        "type",
        "description",
        "short_description",
        "keywords",
        "passive",
        "version",
        "entry",
        "author",
        "i18n",
        "sdk",
        "ui",
        "store",
        "host",
        "install",
        "safety",
        "config_profiles",
        "dependency",
        "dependencies",
        "previous_ids",
    }
    _warn_unknown_keys(plugin_table, allowed_plugin_keys, "[plugin]", issues)

    _require_string(plugin_table, "id", "[plugin].id", issues, pattern=r"^[A-Za-z0-9_-]+$")
    _require_string(plugin_table, "name", "[plugin].name", issues)
    _require_string(plugin_table, "version", "[plugin].version", issues, pattern=r"^\d+\.\d+\.\d+.*$")
    _require_string(plugin_table, "entry", "[plugin].entry", issues, pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")

    plugin_type = plugin_table.get("type", "plugin")
    if not isinstance(plugin_type, str) or plugin_type not in SUPPORTED_PLUGIN_TYPES:
        issues.append(("error", format_plugin_type_choice_error("[plugin].type")))
    if "host" in plugin_table:
        issues.append(("error", format_removed_plugin_host()))

    _check_optional_string(plugin_table, "description", "[plugin].description", issues)
    _check_optional_string(plugin_table, "short_description", "[plugin].short_description", issues)
    _check_optional_bool(plugin_table, "passive", "[plugin].passive", issues)
    _check_string_list(plugin_table.get("keywords"), "[plugin].keywords", issues, required=False)
    _check_plugin_dependency_id_list(plugin_table.get("dependencies"), "[plugin].dependencies", plugin_id, issues)
    _check_previous_plugin_ids(plugin_table.get("previous_ids"), plugin_id, issues)

    _check_author_table(plugin_table.get("author"), issues)
    _check_sdk_table(plugin_table.get("sdk"), issues)
    _check_store_table(plugin_table.get("store"), issues)
    _check_i18n_table(plugin_dir, plugin_table.get("i18n"), issues)
    _check_install_table(plugin_dir, plugin_table.get("install"), issues)
    _check_safety_table(plugin_table.get("safety"), issues)
    _check_config_profiles_table(plugin_table.get("config_profiles"), issues)
    _check_dependency_tables(plugin_table.get("dependency"), issues)
    _check_ui_table(plugin_dir, plugin_table.get("ui"), issues)
    _check_runtime_table(config.get("plugin_runtime"), issues)
    _check_plugin_state_table(config.get("plugin_state"), issues)

    adapter_table = config.get("adapter")
    if adapter_table is not None:
        if not isinstance(adapter_table, dict):
            issues.append(("error", "[adapter] must be a table"))
        else:
            _check_enum(adapter_table.get("mode", "gateway"), "[adapter].mode", {"gateway"}, issues)
            _check_optional_number(adapter_table, "priority", "[adapter].priority", issues, integer=True)


def _warn_unknown_keys(table: dict[str, object], allowed: set[str], label: str, issues: list[tuple[str, str]]) -> None:
    for key in sorted(set(table) - allowed):
        issues.append(("warning", f"{label}.{key} is not a recognized plugin.toml field"))


def _error_unknown_keys(table: dict[str, object], allowed: set[str], label: str, issues: list[tuple[str, str]]) -> None:
    for key in sorted(set(table) - allowed):
        issues.append(("error", f"{label}.{key} is not a recognized plugin.toml field"))


def _require_string(
    table: dict[str, object],
    key: str,
    label: str,
    issues: list[tuple[str, str]],
    *,
    pattern: str | None = None,
) -> None:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        issues.append(("error", f"{label} must be a non-empty string"))
        return
    if value.strip() != value:
        issues.append(("warning", f"{label} contains leading/trailing whitespace"))
    if pattern and not re.fullmatch(pattern, value.strip()):
        issues.append(("error", f"{label} has invalid format: {value}"))


def _check_optional_string(table: dict[str, object], key: str, label: str, issues: list[tuple[str, str]]) -> None:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        issues.append(("error", f"{label} must be a string"))


def _check_optional_i18n_string(table: dict[str, object], key: str, label: str, issues: list[tuple[str, str]]) -> None:
    value = table.get(key)
    if value is None or isinstance(value, str):
        return
    if not isinstance(value, dict):
        issues.append(("error", f"{label} must be a string or $i18n table"))
        return
    _warn_unknown_keys(value, {"$i18n", "default"}, label, issues)
    ref = value.get("$i18n")
    if not isinstance(ref, str) or not ref.strip():
        issues.append(("error", f"{label}.$i18n must be a non-empty string"))
    default = value.get("default")
    if default is not None and not isinstance(default, str):
        issues.append(("error", f"{label}.default must be a string"))


def _check_optional_bool(table: dict[str, object], key: str, label: str, issues: list[tuple[str, str]]) -> None:
    value = table.get(key)
    if value is None:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        issues.append(("warning", f"{label} uses string boolean '{value}'; prefer true/false"))
        return
    issues.append(("error", f"{label} must be a boolean"))


def _check_enum(value: object, label: str, allowed: set[str], issues: list[tuple[str, str]]) -> None:
    if not isinstance(value, str) or value not in allowed:
        issues.append(("error", f"{label} must be one of: {', '.join(sorted(allowed))}"))


def _check_string_list(value: object, label: str, issues: list[tuple[str, str]], *, required: bool) -> None:
    if value is None:
        if required:
            issues.append(("error", f"{label} is required"))
        return
    if not isinstance(value, list):
        issues.append(("error", f"{label} must be a list of strings"))
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            issues.append(("error", f"{label}[{index}] must be a non-empty string"))


def _check_plugin_dependency_id_list(
    value: object,
    label: str,
    plugin_id: str,
    issues: list[tuple[str, str]],
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        issues.append(("error", f"{label} must be a list of plugin id strings"))
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            issues.append(("error", f"{label}[{index}] must be a non-empty plugin id"))
            continue
        dependency_id = item.strip()
        if not re.fullmatch(r"^[A-Za-z0-9_-]+$", dependency_id):
            issues.append((
                "error",
                f"{label}[{index}] must be a plugin id, got '{dependency_id}'. "
                "Python packages belong in pyproject.toml [project].dependencies.",
            ))
            continue
        if dependency_id == plugin_id:
            issues.append(("error", f"{label}[{index}] must not reference the plugin itself"))


def _check_previous_plugin_ids(
    value: object,
    plugin_id: str,
    issues: list[tuple[str, str]],
) -> None:
    label = "[plugin].previous_ids"
    if value is None:
        return
    if not isinstance(value, list):
        issues.append(("error", f"{label} must be a list of plugin id strings"))
        return

    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            issues.append(("error", f"{label}[{index}] must be a non-empty plugin id"))
            continue
        previous_id = item.strip()
        if not re.fullmatch(r"^[A-Za-z0-9_-]+$", previous_id):
            issues.append(("error", f"{label}[{index}] must be a plugin id, got '{previous_id}'"))
        elif previous_id == plugin_id:
            issues.append(("error", f"{label}[{index}] must not equal [plugin].id"))
        elif previous_id in seen:
            issues.append(("error", f"{label}[{index}] duplicates '{previous_id}'"))
        seen.add(previous_id)


def _check_optional_number(
    table: dict[str, object],
    key: str,
    label: str,
    issues: list[tuple[str, str]],
    *,
    integer: bool = False,
    minimum: float | None = None,
) -> None:
    value = table.get(key)
    if value is None:
        return
    valid_type = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not valid_type:
        issues.append(("error", f"{label} must be an {'integer' if integer else 'number'}"))
        return
    if minimum is not None and value < minimum:
        issues.append(("error", f"{label} must be >= {minimum:g}"))


def _check_author_table(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.author] must be a table"))
        return
    _warn_unknown_keys(value, {"name", "email", "url"}, "[plugin.author]", issues)
    for key in ("name", "email", "url"):
        _check_optional_string(value, key, f"[plugin.author].{key}", issues)


def _check_sdk_table(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.sdk] must be a table"))
        return
    _warn_unknown_keys(value, {"recommended", "supported", "untested", "conflicts"}, "[plugin.sdk]", issues)
    for key in ("recommended", "supported", "untested"):
        _check_optional_string(value, key, f"[plugin.sdk].{key}", issues)
    conflicts = value.get("conflicts")
    if conflicts is not None:
        _check_string_list(conflicts, "[plugin.sdk].conflicts", issues, required=False)


def _check_store_table(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.store] must be a table"))
        return
    _warn_unknown_keys(value, {"enabled", "backend"}, "[plugin.store]", issues)
    _check_optional_bool(value, "enabled", "[plugin.store].enabled", issues)
    backend = value.get("backend")
    if backend is not None:
        _check_enum(backend, "[plugin.store].backend", {"file", "sqlite", "memory"}, issues)


def _check_i18n_table(plugin_dir: Path, value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.i18n] must be a table"))
        return
    _warn_unknown_keys(value, {"default_locale", "locales_dir"}, "[plugin.i18n]", issues)
    _check_optional_string(value, "default_locale", "[plugin.i18n].default_locale", issues)
    locales_dir = value.get("locales_dir")
    if locales_dir is not None:
        if not isinstance(locales_dir, str) or not locales_dir.strip():
            issues.append(("error", "[plugin.i18n].locales_dir must be a non-empty string"))
        elif not (plugin_dir / locales_dir).is_dir():
            issues.append(("warning", f"[plugin.i18n].locales_dir does not exist: {locales_dir}"))


def _check_install_table(
    plugin_dir: Path,
    value: object,
    issues: list[tuple[str, str]],
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.install] must be a table"))
        return

    _error_unknown_keys(
        value,
        {"enabled", "ui_i18n_dir", "tutorial_enabled", "kinds"},
        "[plugin.install]",
        issues,
    )
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        issues.append(("error", "[plugin.install].enabled must be a boolean"))

    tutorial_enabled = value.get("tutorial_enabled", False)
    if not isinstance(tutorial_enabled, bool):
        issues.append(("error", "[plugin.install].tutorial_enabled must be a boolean"))

    kinds = value.get("kinds", {})
    if not isinstance(kinds, dict):
        issues.append(("error", "[plugin.install].kinds must be a table"))
        kinds = {}
    else:
        for kind, declaration in kinds.items():
            label = f"[plugin.install.kinds.{kind}]"
            if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
                issues.append(
                    (
                        "error",
                        f"[plugin.install].kinds key {kind!r} must match ^[a-z][a-z0-9_]*$",
                    )
                )
            if not isinstance(declaration, dict):
                issues.append(("error", f"{label} must be a table"))
                continue
            _error_unknown_keys(
                declaration,
                {"entry_id", "label", "queued_message", "entry_timeout"},
                label,
                issues,
            )
            for field in ("entry_id", "label", "queued_message"):
                field_value = declaration.get(field)
                field_label = f"{label}.{field}"
                if not isinstance(field_value, str) or not field_value:
                    issues.append(("error", f"{field_label} must be a non-empty string"))
                elif field_value.strip() != field_value:
                    issues.append(
                        ("error", f"{field_label} must not contain leading/trailing whitespace")
                    )
            timeout = declaration.get("entry_timeout")
            timeout_is_valid = not isinstance(timeout, bool) and isinstance(
                timeout,
                (int, float),
            )
            if timeout_is_valid:
                try:
                    timeout_number = float(timeout)
                except (OverflowError, ValueError):
                    timeout_is_valid = False
                else:
                    timeout_is_valid = math.isfinite(timeout_number) and timeout_number > 0
            if not timeout_is_valid:
                issues.append(
                    (
                        "error",
                        f"{label}.entry_timeout must be a finite number greater than zero",
                    )
                )

    ui_i18n_dir = value.get("ui_i18n_dir")
    if ui_i18n_dir is not None:
        _check_install_i18n_dir(plugin_dir, ui_i18n_dir, issues)

    if enabled is False:
        if kinds:
            issues.append(("error", "disabled [plugin.install] must not define install kinds"))
        if tutorial_enabled is True:
            issues.append(("error", "disabled [plugin.install] must not enable tutorials"))
        if ui_i18n_dir is not None:
            issues.append(("error", "disabled [plugin.install] must not define ui_i18n_dir"))


def _check_install_i18n_dir(
    plugin_dir: Path,
    value: object,
    issues: list[tuple[str, str]],
) -> None:
    label = "[plugin.install].ui_i18n_dir"
    if not isinstance(value, str) or not value or value.strip() != value:
        issues.append(("error", f"{label} must be a non-empty relative path"))
        return

    relative = Path(value)
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or ".." in posix_path.parts
        or ".." in windows_path.parts
    ):
        issues.append(("error", f"{label} must stay within the plugin directory"))
        return

    try:
        plugin_root = plugin_dir.resolve(strict=True)
        candidate = (plugin_root / relative).resolve(strict=True)
        candidate.relative_to(plugin_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        issues.append(
            (
                "error",
                f"{label} must resolve to an existing directory inside the plugin directory",
            )
        )
        return

    current = plugin_root
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except OSError:
            break
        file_attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if current.is_symlink() or bool(file_attributes & reparse_attribute):
            try:
                current.resolve(strict=True).relative_to(plugin_root)
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                issues.append(
                    ("error", f"{label} must not escape through a link or reparse point")
                )
                return

    if not candidate.is_dir():
        issues.append(("error", f"{label} must point to an existing directory"))


def _check_safety_table(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.safety] must be a table"))
        return
    _warn_unknown_keys(value, {"sync_call_in_handler"}, "[plugin.safety]", issues)
    setting = value.get("sync_call_in_handler")
    if setting is not None:
        _check_enum(setting, "[plugin.safety].sync_call_in_handler", {"warn", "reject"}, issues)


def _check_config_profiles_table(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.config_profiles] must be a table"))
        return
    _warn_unknown_keys(value, {"active", "files"}, "[plugin.config_profiles]", issues)
    _check_optional_string(value, "active", "[plugin.config_profiles].active", issues)
    files = value.get("files")
    if files is not None:
        if not isinstance(files, dict):
            issues.append(("error", "[plugin.config_profiles].files must be a table"))
        else:
            for key, item in files.items():
                if not isinstance(key, str) or not isinstance(item, str) or not item.strip():
                    issues.append(("error", "[plugin.config_profiles].files must map profile names to file paths"))


def _check_dependency_tables(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    deps = value if isinstance(value, list) else [value] if isinstance(value, dict) else None
    if deps is None:
        issues.append(("error", "[[plugin.dependency]] must be an array of tables"))
        return
    for index, dep in enumerate(deps):
        label = f"[[plugin.dependency]][{index}]"
        if not isinstance(dep, dict):
            issues.append(("error", f"{label} must be a table"))
            continue
        _warn_unknown_keys(dep, {"id", "entry", "custom_event", "providers", "recommended", "supported", "untested", "conflicts"}, label, issues)
        selectors = [name for name in ("id", "entry", "custom_event", "providers") if dep.get(name)]
        if not selectors:
            issues.append(("error", f"{label} must declare at least one of id, entry, custom_event, providers"))
        if dep.get("entry") and dep.get("custom_event"):
            issues.append(("error", f"{label} cannot declare both entry and custom_event"))
        if dep.get("conflicts") is True and not dep.get("id"):
            issues.append(("error", f"{label} with conflicts=true requires id"))
        if dep.get("conflicts") is not True and "untested" not in dep:
            issues.append(("error", f"{label} requires untested unless conflicts=true"))
        for key in ("id", "entry", "custom_event", "recommended", "supported", "untested"):
            _check_optional_string(dep, key, f"{label}.{key}", issues)
        providers = dep.get("providers")
        if providers is not None:
            _check_string_list(providers, f"{label}.providers", issues, required=False)
        conflicts = dep.get("conflicts")
        if conflicts is not None and conflicts is not True:
            if isinstance(conflicts, str):
                continue
            _check_string_list(conflicts, f"{label}.conflicts", issues, required=False)


def _check_ui_table(plugin_dir: Path, value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin.ui] must be a table"))
        return
    _warn_unknown_keys(
        value,
        {"enabled", "expose_legacy_static_panel", "panel", "guide", "docs", "warnings"},
        "[plugin.ui]",
        issues,
    )
    _check_optional_bool(value, "enabled", "[plugin.ui].enabled", issues)
    _check_optional_bool(
        value,
        "expose_legacy_static_panel",
        "[plugin.ui].expose_legacy_static_panel",
        issues,
    )
    for kind in ("panel", "guide", "docs"):
        raw = value.get(kind)
        if raw is None:
            continue
        items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else None
        if items is None:
            issues.append(("error", f"[plugin.ui].{kind} must be a table or array of tables"))
            continue
        for index, surface in enumerate(items):
            _check_ui_surface(plugin_dir, surface, f"[plugin.ui].{kind}[{index}]", issues)


def _check_ui_surface(plugin_dir: Path, value: object, label: str, issues: list[tuple[str, str]]) -> None:
    if not isinstance(value, dict):
        issues.append(("error", f"{label} must be a table"))
        return
    _warn_unknown_keys(value, {"id", "title", "entry", "mode", "url", "ui_path", "open_in", "context", "permissions", "available"}, label, issues)
    _check_optional_string(value, "id", f"{label}.id", issues)
    _check_optional_i18n_string(value, "title", f"{label}.title", issues)
    entry = value.get("entry")
    url = value.get("url")
    if entry and url:
        issues.append(("error", f"{label} cannot declare both entry and url"))
    if entry is not None:
        if not isinstance(entry, str) or not entry.strip():
            issues.append(("error", f"{label}.entry must be a non-empty string"))
        elif not (plugin_dir / entry).exists():
            issues.append(("warning", f"{label}.entry path does not exist: {entry}"))
    _check_optional_string(value, "url", f"{label}.url", issues)
    mode = value.get("mode")
    if mode is not None:
        _check_enum(mode, f"{label}.mode", {"static", "hosted-tsx", "markdown", "auto"}, issues)
    open_in = value.get("open_in")
    if open_in is not None:
        _check_enum(open_in, f"{label}.open_in", {"iframe", "new_tab", "same_tab"}, issues)
    context = value.get("context")
    if context is not None and (not isinstance(context, str) or not context.strip()):
        # context is the plugin-defined @ui.context provider id resolved via
        # host.get_ui_context(), not a placement enum
        issues.append(("error", f"{label}.context must be a non-empty string"))
    permissions = value.get("permissions")
    if permissions is not None:
        _check_string_list(permissions, f"{label}.permissions", issues, required=False)
        if isinstance(permissions, list):
            allowed = {"state:read", "config:read", "config:write", "action:call", "logs:read", "runs:read"}
            for index, permission in enumerate(permissions):
                if isinstance(permission, str) and permission not in allowed:
                    issues.append(("warning", f"{label}.permissions[{index}] is not a recognized permission: {permission}"))
    _check_optional_bool(value, "available", f"{label}.available", issues)


def _check_runtime_table(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin_runtime] must be a table"))
        return
    _warn_unknown_keys(value, {"enabled", "auto_start", "priority", "timeout", "startup_failure"}, "[plugin_runtime]", issues)
    _check_optional_bool(value, "enabled", "[plugin_runtime].enabled", issues)
    _check_optional_bool(value, "auto_start", "[plugin_runtime].auto_start", issues)
    _check_optional_number(value, "priority", "[plugin_runtime].priority", issues, integer=True)
    _check_optional_number(value, "timeout", "[plugin_runtime].timeout", issues)
    timeout = value.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        if not math.isfinite(float(timeout)):
            issues.append(("error", "[plugin_runtime].timeout must be finite"))
        elif timeout <= 0:
            issues.append(("error", "[plugin_runtime].timeout must be > 0"))
        elif timeout > _PLUGIN_RUNTIME_TIMEOUT_MAX:
            issues.append(("error", "[plugin_runtime].timeout must be <= 300"))
    startup_failure = value.get("startup_failure")
    if startup_failure is not None:
        _check_enum(startup_failure, "[plugin_runtime].startup_failure", {"warn", "fail", "ignore"}, issues)


def _check_plugin_state_table(value: object, issues: list[tuple[str, str]]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", "[plugin_state] must be a table"))
        return
    _warn_unknown_keys(value, {"backend", "persist_mode"}, "[plugin_state]", issues)
    backend = value.get("backend")
    if backend is not None:
        _check_enum(backend, "[plugin_state].backend", {"file", "sqlite", "memory"}, issues)
    persist_mode = value.get("persist_mode")
    if persist_mode is not None:
        _check_enum(persist_mode, "[plugin_state].persist_mode", {"auto", "always", "manual", "disabled"}, issues)


def _check_entry_target(
    plugin_dir: Path,
    plugin_id: str,
    entry: str,
    package_type: str,
    issues: list[tuple[str, str]],
) -> None:
    if ":" not in entry:
        issues.append(("error", "plugin.entry must use 'module:ClassName' format"))
        return

    module_name, class_name = (part.strip() for part in entry.split(":", 1))
    if not module_name or not class_name:
        issues.append(("error", "plugin.entry must include both module and class name"))
        return
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", module_name):
        issues.append(("error", f"plugin.entry module path is invalid: {module_name}"))
        return
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", class_name):
        issues.append(("error", f"plugin.entry class name is invalid: {class_name}"))
        return

    module_path = _resolve_entry_module_path(plugin_dir, plugin_id, module_name)
    if module_path is None:
        issues.append(("warning", f"plugin.entry module '{module_name}' is outside the plugin directory; static entry checks skipped"))
        return
    if not module_path.is_file():
        issues.append(("error", f"plugin.entry module file is missing: {module_path.relative_to(plugin_dir)}"))
        return

    tree = _parse_python_file(module_path, issues, label=str(module_path.relative_to(plugin_dir)))
    if tree is None:
        return

    class_node = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name), None)
    if class_node is None:
        issues.append(("error", f"plugin.entry class '{class_name}' was not found in {module_path.relative_to(plugin_dir)}"))
        return

    if not _has_decorator(class_node.decorator_list, "neko_plugin"):
        issues.append(("error", f"plugin.entry class '{class_name}' must be decorated with @neko_plugin"))

    expected_bases = {
        "plugin": {"NekoPluginBase"},
        "adapter": {"NekoAdapterPlugin"},
    }.get(package_type, {"NekoPluginBase"})
    actual_bases = {_name_of(base) for base in class_node.bases}
    if expected_bases and actual_bases.isdisjoint(expected_bases):
        issues.append(("warning", f"plugin.entry class '{class_name}' should inherit one of: {', '.join(sorted(expected_bases))}"))

    lifecycle_ids = _decorator_ids_in_class(class_node, "lifecycle")
    if "startup" not in lifecycle_ids:
        issues.append(("warning", "plugin.entry class should define @lifecycle(id=\"startup\")"))
    if "shutdown" not in lifecycle_ids:
        issues.append(("warning", "plugin.entry class should define @lifecycle(id=\"shutdown\")"))


def _resolve_entry_module_path(plugin_dir: Path, plugin_id: str, module_name: str) -> Path | None:
    prefix = f"plugin.plugins.{plugin_id}"
    if module_name == prefix:
        return plugin_dir / "__init__.py"
    if module_name.startswith(prefix + "."):
        parts = module_name.removeprefix(prefix + ".").split(".")
        base = plugin_dir.joinpath(*parts)
        file_path = base.with_suffix(".py")
        return file_path if file_path.exists() else base / "__init__.py"
    if "." not in module_name:
        base = plugin_dir / module_name
        file_path = base.with_suffix(".py")
        return file_path if file_path.exists() else base / "__init__.py"
    return None


def _check_python_decorators(plugin_dir: Path, issues: list[tuple[str, str]]) -> None:
    seen_ids: dict[str, str] = {}
    for path in sorted(plugin_dir.rglob("*.py")):
        relative = path.relative_to(plugin_dir)
        if any(part in {"__pycache__", ".venv", "venv", "vendor"} for part in relative.parts):
            continue
        tree = _parse_python_file(path, issues, label=str(relative))
        if tree is None:
            continue
        _check_deprecated_push_message_calls(tree, relative, issues)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                name = _decorator_name(decorator)
                if name == "plugin_entry":
                    entry_id = _decorator_keyword_string(decorator, "id") or node.name
                    _check_identifier(entry_id, f"@plugin_entry id in {relative}:{node.lineno}", issues)
                    _check_schema_keyword(decorator, "input_schema", f"{relative}:{node.lineno}", issues)
                    _check_mutually_exclusive(decorator, {"input_schema", "params"}, f"@plugin_entry in {relative}:{node.lineno}", issues)
                    _check_mutually_exclusive(decorator, {"llm_result_fields", "llm_result_model", "fields"}, f"@plugin_entry in {relative}:{node.lineno}", issues)
                    previous = seen_ids.get(entry_id)
                    location = f"{relative}:{node.lineno}"
                    if previous:
                        issues.append(("warning", f"duplicate @plugin_entry id '{entry_id}' in {location}; first seen at {previous}"))
                    else:
                        seen_ids[entry_id] = location
                elif name == "lifecycle":
                    lifecycle_id = _decorator_keyword_string(decorator, "id")
                    allowed = {"startup", "shutdown", "reload", "freeze", "unfreeze", "config_change"}
                    if not lifecycle_id:
                        issues.append(("error", f"@lifecycle in {relative}:{node.lineno} must declare a non-empty id"))
                    elif lifecycle_id not in allowed:
                        issues.append(("error", f"@lifecycle id '{lifecycle_id}' in {relative}:{node.lineno} must be one of: {', '.join(sorted(allowed))}"))
                elif name == "timer_interval":
                    timer_id = _decorator_keyword_string(decorator, "id")
                    if not timer_id:
                        issues.append(("error", f"@timer_interval in {relative}:{node.lineno} must declare a non-empty id"))
                    seconds = _decorator_keyword_literal(decorator, "seconds")
                    if not isinstance(seconds, int) or seconds <= 0:
                        issues.append(("error", f"@timer_interval in {relative}:{node.lineno} must declare seconds > 0"))
                elif name == "message":
                    message_id = _decorator_keyword_string(decorator, "id")
                    if not message_id:
                        issues.append(("error", f"@message in {relative}:{node.lineno} must declare a non-empty id"))
                    _check_schema_keyword(decorator, "input_schema", f"{relative}:{node.lineno}", issues)


def _check_deprecated_push_message_calls(
    tree: ast.Module,
    relative: Path,
    issues: list[tuple[str, str]],
) -> None:
    """Report explicitly-written v1 fields without executing plugin code."""
    for node in ast.walk(tree):
        # The SDK exposes push_message as a method. Requiring an attribute call
        # avoids flagging an unrelated local helper named ``push_message``.
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Attribute)
            or node.func.attr != "push_message"
        ):
            continue
        reported_fields: set[str] = set()

        for index, argument in enumerate(node.args):
            if index >= len(LEGACY_PUSH_MESSAGE_POSITIONAL_FIELDS):
                break
            field = LEGACY_PUSH_MESSAGE_POSITIONAL_FIELDS[index]
            if field is None or isinstance(argument, ast.Starred):
                continue
            if _legacy_push_value_is_inactive(field, argument):
                continue
            _append_push_v1_warning(
                field=field,
                value_node=argument,
                call_node=node,
                relative=relative,
                issues=issues,
            )
            reported_fields.add(field)

        for keyword in node.keywords:
            field = keyword.arg
            if (
                field not in LEGACY_PUSH_MESSAGE_FIELD_MIGRATIONS
                or field in reported_fields
                or _legacy_push_value_is_inactive(field, keyword.value)
            ):
                continue
            _append_push_v1_warning(
                field=field,
                value_node=keyword,
                call_node=node,
                relative=relative,
                issues=issues,
            )


def _legacy_push_value_is_inactive(field: str, value_node: ast.AST) -> bool:
    if not isinstance(value_node, ast.Constant):
        return False
    if value_node.value is None:
        return True
    return (
        field in LEGACY_PUSH_MESSAGE_TRUTHY_ONLY_FIELDS
        and value_node.value is False
    )


def _append_push_v1_warning(
    *,
    field: str,
    value_node: ast.AST,
    call_node: ast.Call,
    relative: Path,
    issues: list[tuple[str, str]],
) -> None:
    location = f"{relative}:{getattr(value_node, 'lineno', call_node.lineno)}"
    issues.append(
        (
            "warning",
            f"{location}: {format_push_message_v1_static_diagnostic(field)}",
        )
    )


def _parse_python_file(path: Path, issues: list[tuple[str, str]], *, label: str) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        issues.append(("error", f"Python syntax error in {label}: {exc.msg} at line {exc.lineno}"))
    except UnicodeDecodeError as exc:
        issues.append(("error", f"Python file is not valid UTF-8: {label}: {exc}"))
    return None


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    return _name_of(target)


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _name_of(node.value)
    return ""


def _has_decorator(decorators: list[ast.expr], name: str) -> bool:
    return any(_decorator_name(item) == name for item in decorators)


def _decorator_ids_in_class(class_node: ast.ClassDef, decorator_name: str) -> set[str]:
    ids: set[str] = set()
    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in item.decorator_list:
            if _decorator_name(decorator) == decorator_name:
                value = _decorator_keyword_string(decorator, "id")
                if value:
                    ids.add(value)
    return ids


def _decorator_keyword_string(decorator: ast.expr, keyword: str) -> str:
    value = _decorator_keyword_literal(decorator, keyword)
    return value.strip() if isinstance(value, str) else ""


def _decorator_keyword_literal(decorator: ast.expr, keyword: str) -> object:
    if not isinstance(decorator, ast.Call):
        return None
    for item in decorator.keywords:
        if item.arg == keyword:
            try:
                return ast.literal_eval(item.value)
            except (ValueError, TypeError):
                return None
    return None


def _decorator_keywords(decorator: ast.expr) -> set[str]:
    if not isinstance(decorator, ast.Call):
        return set()
    return {item.arg for item in decorator.keywords if item.arg}


def _check_identifier(value: str, label: str, issues: list[tuple[str, str]]) -> None:
    if not value:
        issues.append(("error", f"{label} must be non-empty"))
    elif not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        issues.append(("warning", f"{label} should contain only A-Z, a-z, 0-9, _, -"))


def _check_schema_keyword(decorator: ast.expr, keyword: str, label: str, issues: list[tuple[str, str]]) -> None:
    value = _decorator_keyword_literal(decorator, keyword)
    if value is None:
        return
    if not isinstance(value, dict):
        issues.append(("error", f"{keyword} in {label} must be a dict literal when statically declared"))
        return
    if value.get("type") != "object":
        issues.append(("warning", f"{keyword} in {label} should declare type='object'"))
    properties = value.get("properties")
    if properties is not None and not isinstance(properties, dict):
        issues.append(("error", f"{keyword}.properties in {label} must be an object"))
    required = value.get("required")
    if required is not None and not isinstance(required, list):
        issues.append(("error", f"{keyword}.required in {label} must be a list"))


def _check_mutually_exclusive(decorator: ast.expr, names: set[str], label: str, issues: list[tuple[str, str]]) -> None:
    present = names & _decorator_keywords(decorator)
    if len(present) > 1:
        issues.append(("error", f"{label} cannot combine mutually exclusive options: {', '.join(sorted(present))}"))


def _check_optional_file(path: Path, label: str, issues: list[tuple[str, str]], *, strict: bool) -> None:
    if path.is_file():
        return
    issues.append(("error" if strict else "warning", f"{label} is missing"))


def _check_requirements_file(path: Path, issues: list[tuple[str, str]]) -> None:
    if path.exists():
        issues.append((
            "error",
            "requirements.txt is not supported; use pyproject.toml [project].dependencies "
            "and vendor/ for Python runtime dependencies",
        ))


def _check_pyproject_dependency_layout(
    plugin_dir: Path,
    pyproject_toml: dict[str, object] | None,
    package_type: str,
    issues: list[tuple[str, str]],
) -> None:
    python_requirements = collect_project_python_requirements(pyproject_toml)
    external_requirements, _host_requirements = split_host_provided_requirements(python_requirements)
    if not external_requirements:
        return
    vendor_dir = plugin_dir / "vendor"
    if not vendor_dir.is_dir():
        issues.append((
            "error",
            "pyproject.toml [project].dependencies declares Python runtime dependencies "
            f"({', '.join(external_requirements)}), but vendor/ is missing",
        ))
    elif not any(path.is_file() for path in vendor_dir.rglob("*")):
        issues.append((
            "error",
            "pyproject.toml [project].dependencies declares Python runtime dependencies "
            f"({', '.join(external_requirements)}), but vendor/ does not contain any files",
        ))
    else:
        missing_requirements = find_missing_python_requirements(
            external_requirements,
            search_paths=[vendor_dir],
        )
        if missing_requirements:
            issues.append((
                "error",
                "vendor/ does not satisfy Python runtime dependencies: "
                f"{', '.join(missing_requirements)}",
            ))


def _check_json_file(path: Path, label: str, issues: list[tuple[str, str]], *, strict: bool) -> None:
    if not path.is_file():
        issues.append(("error" if strict else "warning", f"{label} is missing"))
        return
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        issues.append(("error" if strict else "warning", f"{label} is not valid UTF-8: {exc}"))
        return
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        issues.append(("error", f"{label} is invalid JSON: {exc}"))


def _check_gitignore(path: Path, issues: list[tuple[str, str]], *, strict: bool) -> None:
    if not path.is_file():
        issues.append(("error" if strict else "warning", ".gitignore is missing"))
        return

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        issues.append(("error" if strict else "warning", f".gitignore is not valid UTF-8: {exc}"))
        return
    required_patterns = ["__pycache__/", ".pytest_cache/", "store.db"]
    for pattern in required_patterns:
        if not re.search(rf"(^|\n){re.escape(pattern)}($|\n)", text):
            issues.append(("warning", f".gitignore should include {pattern}"))
