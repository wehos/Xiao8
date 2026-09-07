from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Built-in excludes are hard safety defaults. User rules extend them, but do
# not replace them, so common cache/build artifacts never leak into packages.
_DEFAULT_EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "dist",
    "build",
    ".git",
}
_DEFAULT_EXCLUDE_FILE_NAMES = {
    ".DS_Store",
}
_DEFAULT_EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
}


class PackRuleSet(BaseModel):
    """User-defined file selection rules loaded from `[tool.neko.pack]`."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    exclude_dirs: list[str] = Field(default_factory=list)
    exclude_files: list[str] = Field(default_factory=list)

    @field_validator("include", "exclude", "exclude_dirs", "exclude_files", mode="before")
    @classmethod
    def _normalize_pattern_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("pack rule value must be a list of strings")

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise TypeError("pack rule entries must be strings")
            pattern = item.strip()
            if not pattern or pattern in seen:
                continue
            seen.add(pattern)
            normalized.append(pattern)
        return normalized


def load_pack_rules(pyproject_toml: dict[str, object] | None) -> PackRuleSet:
    # Missing config is treated as "no extra rules" so basic packaging keeps a
    # zero-config path.
    if not isinstance(pyproject_toml, dict):
        return PackRuleSet()

    tool_table = pyproject_toml.get("tool")
    if not isinstance(tool_table, dict):
        return PackRuleSet()

    neko_table = tool_table.get("neko")
    if not isinstance(neko_table, dict):
        return PackRuleSet()

    pack_table = neko_table.get("pack")
    if not isinstance(pack_table, dict):
        return PackRuleSet()

    return PackRuleSet.model_validate(pack_table)


def should_skip_path(relative_path: Path, *, is_dir: bool, rules: PackRuleSet) -> bool:
    # Matching always works on normalized archive-style relative paths so the
    # same rule semantics apply across platforms.
    path_str = relative_path.as_posix()

    if any(part in _DEFAULT_EXCLUDE_DIR_NAMES for part in relative_path.parts):
        return True

    if not is_dir:
        if relative_path.name in _DEFAULT_EXCLUDE_FILE_NAMES:
            return True
        if relative_path.suffix in _DEFAULT_EXCLUDE_SUFFIXES:
            return True

    if _matches_any(path_str, rules.exclude):
        return True

    if any(part in rules.exclude_dirs for part in relative_path.parts):
        return True

    if is_dir:
        if relative_path.name in rules.exclude_dirs:
            return True
        if _matches_any(path_str, rules.exclude_dirs):
            return True
        return False

    if relative_path.name in rules.exclude_files:
        return True
    if _matches_any(path_str, rules.exclude_files):
        return True

    if not rules.include:
        return False

    # When include rules are present, they become an allow-list after all
    # exclude checks have run.
    return not _matches_include(path_str, rules.include)


def _matches_include(path_str: str, patterns: list[str]) -> bool:
    return _matches_any(path_str, patterns)


def _matches_any(path_str: str, patterns: list[str]) -> bool:
    return any(_match_pattern(path_str, pattern) for pattern in patterns)


def _match_pattern(path_str: str, pattern: str) -> bool:
    if fnmatchcase(path_str, pattern):
        return True
    if "/" not in pattern and fnmatchcase(Path(path_str).name, pattern):
        return True
    return False
