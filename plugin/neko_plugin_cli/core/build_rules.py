from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Built-in excludes are hard safety defaults. User rules extend them, but do
# not replace them, so common cache/build artifacts never leak into packages.
#
# On macOS an editor directory is not merely noise. The desktop build stages
# this payload under projectneko_server.app/Contents/MacOS/, and codesign's
# default rules treat every directory there whose name contains a dot as a
# nested bundle. It then fails to read one — there is no Contents/ inside — and
# aborts the whole seal with "bundle format unrecognized, invalid, or
# unsuitable", naming only the subcomponent. One stray .vscode/ in one plugin
# therefore breaks the entire mac backend build.
_DEFAULT_EXCLUDE_DIR_NAMES = {
    "__pycache__",
    ".github",
    ".vscode",
    ".idea",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    ".git",
}
_DEFAULT_ROOT_EXCLUDE_DIR_NAMES = {
    "dist",
    "build",
}
_DEFAULT_EXCLUDE_FILE_NAMES = {
    ".DS_Store",
}
_DEFAULT_EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
}


class BuildRuleSet(BaseModel):
    """User-defined file selection rules loaded from `[tool.neko.build]`."""

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
            raise TypeError("build rule value must be a list of strings")

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise TypeError("build rule entries must be strings")
            pattern = item.strip()
            if not pattern or pattern in seen:
                continue
            seen.add(pattern)
            normalized.append(pattern)
        return normalized


def load_build_rules(pyproject_toml: dict[str, object] | None) -> BuildRuleSet:
    # Missing config is treated as "no extra rules" so basic packaging keeps a
    # zero-config path.
    if not isinstance(pyproject_toml, dict):
        return BuildRuleSet()

    tool_table = pyproject_toml.get("tool")
    if not isinstance(tool_table, dict):
        return BuildRuleSet()

    neko_table = tool_table.get("neko")
    if not isinstance(neko_table, dict):
        return BuildRuleSet()

    build_table = neko_table.get("build")
    if not isinstance(build_table, dict):
        return BuildRuleSet()

    return BuildRuleSet.model_validate(build_table)


def should_skip_path(relative_path: Path, *, is_dir: bool, rules: BuildRuleSet) -> bool:
    # Matching always works on normalized archive-style relative paths so the
    # same rule semantics apply across platforms.
    path_str = relative_path.as_posix()

    # Check directory components only (exclude the filename for files).
    dir_parts = relative_path.parts if is_dir else relative_path.parts[:-1]
    if dir_parts and dir_parts[0] in _DEFAULT_ROOT_EXCLUDE_DIR_NAMES:
        return True
    if any(part in _DEFAULT_EXCLUDE_DIR_NAMES for part in dir_parts):
        return True

    if not is_dir:
        if relative_path.name in _DEFAULT_EXCLUDE_FILE_NAMES:
            return True
        if relative_path.suffix in _DEFAULT_EXCLUDE_SUFFIXES:
            return True

    if _matches_any(path_str, rules.exclude):
        return True

    if _matches_excluded_dir(relative_path, is_dir=is_dir, patterns=rules.exclude_dirs):
        return True

    if is_dir:
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


def _matches_excluded_dir(relative_path: Path, *, is_dir: bool, patterns: list[str]) -> bool:
    if not patterns:
        return False
    parts = relative_path.parts if is_dir else relative_path.parts[:-1]
    for index in range(len(parts)):
        candidate = "/".join(parts[:index + 1])
        if _matches_any(candidate, patterns):
            return True
    return False


def _match_pattern(path_str: str, pattern: str) -> bool:
    if fnmatchcase(path_str, pattern):
        return True
    if "/" not in pattern and fnmatchcase(Path(path_str).name, pattern):
        return True
    return False
