from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_LOCALES = ("en", "ja", "ko", "zh-CN", "zh-TW", "ru", "es", "pt")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_REQUIRED_LOCALE_DIRS = (
    "static/locales",
)
_REQUIRED_LOCALE_DIR_SET = frozenset(_REQUIRED_LOCALE_DIRS)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_locale_dirs(root: Path) -> list[Path]:
    dirs = [root / relative for relative in _REQUIRED_LOCALE_DIRS]
    seen = set(dirs)
    plugins_root = root / "plugin" / "plugins"
    if plugins_root.exists():
        for plugin_dir in sorted(path for path in plugins_root.iterdir() if path.is_dir()):
            for name in ("locales", "i18n"):
                locale_dir = plugin_dir / name
                if locale_dir.exists() and locale_dir not in seen:
                    dirs.append(locale_dir)
                    seen.add(locale_dir)
    return dirs


def _format_keys(keys: list[str], *, limit: int = 20) -> str:
    shown = keys[:limit]
    message = ", ".join(shown)
    if len(keys) > limit:
        message += f", ... and {len(keys) - limit} more"
    return message


def _merge_flattened_keys(
    flattened: dict[str, str],
    child_values: dict[str, str],
) -> None:
    collisions = sorted(set(flattened).intersection(child_values))
    if collisions:
        raise RuntimeError(
            "flattened key collision(s): " + _format_keys(collisions)
        )
    flattened.update(child_values)


def _flatten_json(value: Any, *, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        flattened: dict[str, str] = {}
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            _merge_flattened_keys(
                flattened,
                _flatten_json(item, prefix=child_prefix),
            )
        return flattened
    if isinstance(value, list):
        flattened = {}
        for index, item in enumerate(value):
            child_prefix = f"{prefix}.{index}" if prefix else str(index)
            _merge_flattened_keys(
                flattened,
                _flatten_json(item, prefix=child_prefix),
            )
        return flattened
    if not isinstance(value, str):
        raise RuntimeError(
            f"{prefix}: locale value must be a string, got {type(value).__name__}"
        )
    return {prefix: value}


def _load_locale_file(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{path}: failed to read JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path}: locale file must contain a JSON object")
    try:
        return _flatten_json(payload)
    except RuntimeError as exc:
        raise RuntimeError(f"{path}: {exc}") from exc


def _placeholder_counter(text: str) -> Counter[str]:
    return Counter(PLACEHOLDER_RE.findall(text))


def _compare_locale_dir(locale_dir: Path) -> list[str]:
    errors: list[str] = []
    locale_maps = {
        locale: _load_locale_file(locale_dir / f"{locale}.json")
        for locale in EXPECTED_LOCALES
    }
    all_keys = set().union(*(set(values) for values in locale_maps.values()))
    for locale, values in locale_maps.items():
        missing = sorted(all_keys - set(values))
        if missing:
            errors.append(
                f"{locale_dir}: {locale} missing {len(missing)} key(s): "
                + _format_keys(missing)
            )

    for key in sorted(all_keys):
        placeholder_by_locale: dict[str, Counter[str]] = {}
        for locale, values in locale_maps.items():
            if key in values:
                placeholder_by_locale[locale] = _placeholder_counter(values[key])
        if "en" in placeholder_by_locale:
            expected = placeholder_by_locale["en"]
        else:
            expected = next(iter(placeholder_by_locale.values()))
        mismatched = {
            locale: placeholders
            for locale, placeholders in placeholder_by_locale.items()
            if placeholders != expected
        }
        if mismatched:
            detail = "; ".join(
                f"{locale}={dict(placeholders)}"
                for locale, placeholders in sorted(mismatched.items())
            )
            errors.append(
                f"{locale_dir}: placeholder mismatch for {key}: "
                f"expected={dict(expected)}; {detail}"
            )
    return errors


def _missing_locale_files(locale_dir: Path) -> list[str]:
    return [
        f"{locale}.json"
        for locale in EXPECTED_LOCALES
        if not (locale_dir / f"{locale}.json").is_file()
    ]


def _is_default_required_dir(root: Path, locale_dir: Path) -> bool:
    try:
        relative = locale_dir.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        # User-supplied directories outside the repo should fail strictly instead of
        # being downgraded to warnings by the default plugin allowlist.
        return True
    return relative in _REQUIRED_LOCALE_DIR_SET


def _report_results(
    *,
    checked: list[Path],
    skipped: list[Path],
    warnings: list[str],
    errors: list[str],
) -> int:
    for locale_dir in skipped:
        print(f"SKIP {locale_dir}")
    for warning in warnings:
        print(f"WARNING {warning}")
    for error in errors:
        print(f"ERROR {error}")
    print(f"Checked {len(checked)} locale directories; skipped {len(skipped)}.")
    if errors:
        print(f"Locale comparison failed with {len(errors)} error(s).")
        return 1
    if warnings:
        print(
            f"Locale comparison passed with {len(warnings)} warning(s). "
            "Use --strict-all to fail on every scanned plugin locale directory."
        )
        return 0
    print("Locale comparison passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare key and placeholder consistency across locale JSON files.",
    )
    parser.add_argument(
        "locale_dirs",
        nargs="*",
        help="Optional locale directories. Defaults to static/locales and plugin locale/i18n dirs.",
    )
    parser.add_argument(
        "--strict-all",
        action="store_true",
        help="Fail for every scanned locale directory, including non-Galgame plugins.",
    )
    args = parser.parse_args()

    root = _repo_root()
    custom_locale_dirs = bool(args.locale_dirs)
    locale_dirs = [Path(path).resolve() for path in args.locale_dirs]
    if not locale_dirs:
        locale_dirs = _default_locale_dirs(root)

    errors: list[str] = []
    warnings: list[str] = []
    checked: list[Path] = []
    skipped: list[Path] = []
    for locale_dir in locale_dirs:
        if not locale_dir.exists():
            if custom_locale_dirs or _is_default_required_dir(root, locale_dir):
                errors.append(f"{locale_dir}: directory does not exist")
            else:
                skipped.append(locale_dir)
            continue
        missing_files = _missing_locale_files(locale_dir)
        if missing_files:
            issue = (
                f"{locale_dir}: missing expected locale file(s): "
                + _format_keys(missing_files)
            )
            if args.strict_all or custom_locale_dirs or _is_default_required_dir(root, locale_dir):
                errors.append(issue)
            else:
                warnings.append(issue)
            continue
        checked.append(locale_dir)
        try:
            issues = _compare_locale_dir(locale_dir)
        except RuntimeError as exc:
            issues = [f"{locale_dir}: {exc}"]
        if args.strict_all or custom_locale_dirs or _is_default_required_dir(root, locale_dir):
            errors.extend(issues)
        else:
            warnings.extend(issues)

    return _report_results(
        checked=checked,
        skipped=skipped,
        warnings=warnings,
        errors=errors,
    )


if __name__ == "__main__":
    sys.exit(main())
