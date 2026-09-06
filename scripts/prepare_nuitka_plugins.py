"""Prepare built-in plugins for Nuitka using each plugin's build rules.

Nuitka deliberately does not copy Python source files as data.  Built-in
plugins are also imported dynamically, so the desktop build has two separate
requirements:

* compile only Python modules allowed by ``[tool.neko.build]``;
* copy the complete filtered runtime payload (including source files used by
  subprocess/``spec_from_file_location`` entrypoints) into the standalone
  distribution.

``prepare`` creates that filtered payload and a generated launcher containing
Nuitka project directives for Python modules excluded by the same rules.
``install`` atomically replaces ``dist/.../plugin/plugins`` with the staged
payload after Nuitka finishes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Direct execution (``python scripts/prepare_nuitka_plugins.py``) puts only
# ``scripts/`` on sys.path.  Add the repository root before importing the
# local plugin package; both desktop workflows intentionally use this form.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from plugin.core.python_dependencies import load_pyproject_toml
from plugin.neko_plugin_cli.core.build_rules import BuildRuleSet, load_build_rules, should_skip_path


DEFAULT_STAGE_DIR = Path("build/nuitka-plugins")
DEFAULT_GENERATED_LAUNCHER = Path("build_nuitka_launcher.py")
_PRIVATE_SUFFIXES = {".db", ".log"}


@dataclass(frozen=True, slots=True)
class PrepareResult:
    stage_dir: Path
    generated_launcher: Path
    plugin_dirs: tuple[str, ...]
    staged_files: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    excluded_modules: tuple[str, ...]
    skipped_entries: tuple[str, ...]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _safe_clean_directory(path: Path, *, project_root: Path) -> None:
    resolved = path.resolve()
    root = project_root.resolve()
    if resolved == root or not _is_relative_to(resolved, root):
        raise ValueError(f"refusing to clean path outside project root: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def _module_name(plugin_dir_name: str, relative_path: Path, *, is_dir: bool) -> str | None:
    parts = ["plugin", "plugins", plugin_dir_name, *relative_path.parts]
    if not is_dir:
        if relative_path.suffix != ".py":
            return None
        parts[-1] = relative_path.stem
        if parts[-1] == "__init__":
            parts.pop()
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _git_tracked_top_level_names(plugins_root: Path) -> set[str] | None:
    """Names Git tracks directly under ``plugins_root``.

    Returns ``None`` when Git is unavailable or the tree is not a checkout, in
    which case the caller falls back to the ``plugin.toml`` requirement alone.
    """

    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(plugins_root),
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None

    names: set[str] = set()
    for entry in completed.stdout.split(bytes([0])):
        if not entry:
            continue
        relative = entry.decode("utf-8", errors="replace")
        head = relative.split("/", 1)[0]
        if head:
            names.add(head)
    # An empty index for a directory that is supposed to hold the built-in
    # plugins means the query answered about some unrelated checkout (a temp
    # tree nested in a repository, for example).  Fall back rather than treat
    # every plugin as untracked.
    return names or None


def _skip_reason(entry: Path, *, tracked_names: set[str] | None) -> str | None:
    """Why ``entry`` must stay out of the desktop payload, or ``None`` to keep it.

    Local-only plugin directories (private experiments, plugins already moved to
    the marketplace and deleted from the index) stay on a developer machine long
    after Git stops tracking them.  Copying whatever ``plugin/plugins`` happens
    to contain shipped those directories in desktop builds, so the stage now
    mirrors the index instead of the working tree.
    """

    if tracked_names is not None and entry.name not in tracked_names:
        return "untracked by git"
    if not entry.is_dir():
        return None
    # ``_shared`` and friends are runtime helper packages, not plugins.
    if entry.name.startswith("_"):
        return None
    if not (entry / "plugin.toml").is_file():
        return "missing plugin.toml"
    return None


def _copy_plugin_tree(
    source_dir: Path,
    destination_dir: Path,
    *,
    rules: BuildRuleSet,
) -> tuple[list[str], list[str], set[str]]:
    staged_files: list[str] = []
    excluded_paths: list[str] = []
    excluded_modules: set[str] = set()
    plugin_dir_name = source_dir.name

    for current, dir_names, file_names in os.walk(source_dir, topdown=True):
        current_path = Path(current)
        relative_current = current_path.relative_to(source_dir)

        kept_dirs: list[str] = []
        for dir_name in sorted(dir_names):
            relative = relative_current / dir_name
            if should_skip_path(relative, is_dir=True, rules=rules):
                excluded_paths.append(relative.as_posix() + "/")
                module = _module_name(plugin_dir_name, relative, is_dir=True)
                if module is not None:
                    excluded_modules.add(module)
                continue
            kept_dirs.append(dir_name)
            (destination_dir / relative).mkdir(parents=True, exist_ok=True)
        dir_names[:] = kept_dirs

        for file_name in sorted(file_names):
            relative = relative_current / file_name
            source_path = source_dir / relative
            if should_skip_path(relative, is_dir=False, rules=rules):
                excluded_paths.append(relative.as_posix())
                module = _module_name(plugin_dir_name, relative, is_dir=False)
                if module is not None:
                    excluded_modules.add(module)
                continue
            destination_path = destination_dir / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
            staged_files.append(relative.as_posix())

    return staged_files, excluded_paths, excluded_modules


def _remove_private_runtime_artifacts(stage_dir: Path) -> list[str]:
    """Preserve the existing desktop-build privacy cleanup in the stage itself."""

    removed: list[str] = []
    for path in sorted(stage_dir.rglob("*"), reverse=True):
        if path.is_dir() and path.name == "__pycache__":
            removed.append(path.relative_to(stage_dir).as_posix() + "/")
            shutil.rmtree(path)
        elif path.is_file() and path.suffix.lower() in _PRIVATE_SUFFIXES:
            removed.append(path.relative_to(stage_dir).as_posix())
            path.unlink()

    return removed


def _write_generated_launcher(
    source_launcher: Path,
    generated_launcher: Path,
    *,
    excluded_modules: set[str],
) -> None:
    source_text = source_launcher.read_text(encoding="utf-8-sig")
    directives = [
        "# Generated by scripts/prepare_nuitka_plugins.py; do not edit.",
        *(
            f"# nuitka-project: --nofollow-import-to={module}"
            for module in sorted(excluded_modules)
        ),
        "",
    ]
    generated_launcher.parent.mkdir(parents=True, exist_ok=True)
    generated_launcher.write_text("\n".join(directives) + source_text, encoding="utf-8", newline="\n")


def prepare_plugins(
    *,
    project_root: Path,
    plugins_root: Path,
    stage_dir: Path,
    source_launcher: Path,
    generated_launcher: Path,
) -> PrepareResult:
    project_root = project_root.resolve()
    plugins_root = (project_root / plugins_root).resolve() if not plugins_root.is_absolute() else plugins_root.resolve()
    stage_dir = (project_root / stage_dir).resolve() if not stage_dir.is_absolute() else stage_dir.resolve()
    source_launcher = (
        (project_root / source_launcher).resolve()
        if not source_launcher.is_absolute()
        else source_launcher.resolve()
    )
    generated_launcher = (
        (project_root / generated_launcher).resolve()
        if not generated_launcher.is_absolute()
        else generated_launcher.resolve()
    )

    if not plugins_root.is_dir():
        raise FileNotFoundError(f"built-in plugins directory not found: {plugins_root}")
    if not source_launcher.is_file():
        raise FileNotFoundError(f"launcher not found: {source_launcher}")
    if _is_relative_to(stage_dir, plugins_root) or _is_relative_to(plugins_root, stage_dir):
        raise ValueError("stage directory and plugin source directory must not contain each other")

    _safe_clean_directory(stage_dir, project_root=project_root)

    plugin_dirs: list[str] = []
    staged_files: list[str] = []
    excluded_paths: list[str] = []
    excluded_modules: set[str] = set()
    skipped_entries: list[str] = []
    tracked_names = _git_tracked_top_level_names(plugins_root)

    for source_file in sorted(path for path in plugins_root.iterdir() if path.is_file()):
        reason = _skip_reason(source_file, tracked_names=tracked_names)
        if reason is not None:
            skipped_entries.append(f"{source_file.name} ({reason})")
            continue
        shutil.copy2(source_file, stage_dir / source_file.name)

    for source_dir in sorted(path for path in plugins_root.iterdir() if path.is_dir()):
        reason = _skip_reason(source_dir, tracked_names=tracked_names)
        if reason is not None:
            skipped_entries.append(f"{source_dir.name}/ ({reason})")
            continue
        plugin_dirs.append(source_dir.name)
        destination_dir = stage_dir / source_dir.name
        destination_dir.mkdir(parents=True, exist_ok=True)
        rules = load_build_rules(load_pyproject_toml(source_dir))
        copied, excluded, modules = _copy_plugin_tree(source_dir, destination_dir, rules=rules)
        staged_files.extend(f"{source_dir.name}/{item}" for item in copied)
        excluded_paths.extend(f"{source_dir.name}/{item}" for item in excluded)
        excluded_modules.update(modules)

    excluded_paths.extend(_remove_private_runtime_artifacts(stage_dir))
    staged_files = sorted(
        path.relative_to(stage_dir).as_posix()
        for path in stage_dir.rglob("*")
        if path.is_file()
    )
    _write_generated_launcher(
        source_launcher,
        generated_launcher,
        excluded_modules=excluded_modules,
    )

    manifest = {
        "schema_version": 1,
        "plugins_root": plugins_root.relative_to(project_root).as_posix(),
        "stage_dir": stage_dir.relative_to(project_root).as_posix(),
        "plugin_dirs": plugin_dirs,
        "staged_files": staged_files,
        "excluded_paths": sorted(excluded_paths),
        "excluded_modules": sorted(excluded_modules),
        "skipped_entries": sorted(skipped_entries),
    }
    (stage_dir.parent / "nuitka-plugin-stage.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    return PrepareResult(
        stage_dir=stage_dir,
        generated_launcher=generated_launcher,
        plugin_dirs=tuple(plugin_dirs),
        staged_files=tuple(staged_files),
        excluded_paths=tuple(sorted(excluded_paths)),
        excluded_modules=tuple(sorted(excluded_modules)),
        skipped_entries=tuple(sorted(skipped_entries)),
    )


def install_plugins(*, stage_dir: Path, destination_dir: Path) -> None:
    stage_dir = stage_dir.resolve()
    destination_dir = destination_dir.resolve()
    if not stage_dir.is_dir():
        raise FileNotFoundError(f"Nuitka plugin stage not found: {stage_dir}")
    if (
        stage_dir == destination_dir
        or _is_relative_to(stage_dir, destination_dir)
        or _is_relative_to(destination_dir, stage_dir)
    ):
        raise ValueError("plugin stage and destination must not contain each other")

    temporary_dir = destination_dir.with_name(destination_dir.name + ".staging")
    backup_dir = destination_dir.with_name(destination_dir.name + ".old")
    if backup_dir.exists():
        if destination_dir.exists():
            shutil.rmtree(backup_dir)
        else:
            backup_dir.replace(destination_dir)
    if temporary_dir.exists():
        shutil.rmtree(temporary_dir)
    temporary_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(stage_dir, temporary_dir)
    if destination_dir.exists():
        destination_dir.replace(backup_dir)
    try:
        temporary_dir.replace(destination_dir)
    except BaseException:
        if backup_dir.exists() and not destination_dir.exists():
            backup_dir.replace(destination_dir)
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare", help="stage plugins and generate launcher")
    prepare_parser.add_argument("--project-root", type=Path, default=Path("."))
    prepare_parser.add_argument("--plugins-root", type=Path, default=Path("plugin/plugins"))
    prepare_parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE_DIR)
    prepare_parser.add_argument("--source-launcher", type=Path, default=Path("launcher.py"))
    prepare_parser.add_argument("--output-launcher", type=Path, default=DEFAULT_GENERATED_LAUNCHER)

    install_parser = subparsers.add_parser("install", help="install staged plugins into a Nuitka dist")
    install_parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE_DIR)
    install_parser.add_argument("--destination-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "prepare":
        result = prepare_plugins(
            project_root=args.project_root,
            plugins_root=args.plugins_root,
            stage_dir=args.stage_dir,
            source_launcher=args.source_launcher,
            generated_launcher=args.output_launcher,
        )
        print(
            f"Prepared {len(result.plugin_dirs)} built-in plugin directories with "
            f"{len(result.staged_files)} files; excluded {len(result.excluded_paths)} paths."
        )
        for entry in result.skipped_entries:
            # ``entry`` already carries the reason it was left out.
            print(f"[WARNING] Not bundled: {entry}")
        print(f"Plugin stage: {result.stage_dir}")
        print(f"Generated launcher: {result.generated_launcher}")
        return 0

    install_plugins(stage_dir=args.stage_dir, destination_dir=args.destination_dir)
    print(f"Installed staged built-in plugins into {args.destination_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
