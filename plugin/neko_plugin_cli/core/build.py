from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .models import BuildResult, PayloadBuildResult, PluginSource
from .normalize import normalize_relative_posix
from .build_rules import BuildRuleSet, load_build_rules, should_skip_path
from .dependencies import (
    validate_payload_dependency_layout,
    validate_source_dependency_layout,
    write_dependency_manifest,
)
from .metadata_probe import PACKAGED_METADATA_FILENAME, write_packaged_metadata
from .plugin_source import load_plugin_source
from .profile import write_bundle_profile, write_default_profile
from .toml_utils import escape_string

_SCHEMA_VERSION = "1.0"
_SAFE_PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _record_staged_file(staged_files: list[Path], path: Path | None) -> None:
    """Append ``path`` unless that file is already staged.

    ``PayloadBuildResult`` only sorts its file list, it does not de-duplicate,
    so a path staged twice is counted twice.
    """
    if path is None:
        return
    resolved = path.resolve()
    if any(existing.resolve() == resolved for existing in staged_files):
        return
    staged_files.append(path)


def _settle_staged_metadata(
    staged_files: list[Path], plugin_payload_dir: Path, written: Path | None
) -> None:
    """Keep ``staged_files`` describing what is actually in the staging tree.

    A plugin tree can already carry ``plugin.meta.json``, so
    ``copy_plugin_runtime_files`` lists it before the probe runs. When the probe
    fails, that stale copy is deleted — and a path that no longer exists makes
    ``BuildResult._validate_layout`` raise ``FileNotFoundError`` with
    ``keep_staging=True``, after the archive has already been written (codex).
    """
    if written is not None:
        _record_staged_file(staged_files, written)
        return
    removed = (plugin_payload_dir / PACKAGED_METADATA_FILENAME).resolve()
    staged_files[:] = [
        path for path in staged_files if path.resolve() != removed
    ]


def _validate_package_id(package_id: str, *, label: str = "package_id") -> str:
    value = package_id.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    if not _SAFE_PACKAGE_ID_RE.fullmatch(value):
        raise ValueError(
            f"{label} must contain only ASCII letters, numbers, dots, underscores, or hyphens"
        )
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be a safe single path segment")
    return value


@dataclass(slots=True)
class BuildPaths:
    """Resolved staging layout for a build operation."""

    staging_root: Path
    payload_dir: Path
    plugins_dir: Path
    profiles_dir: Path
    manifest_path: Path
    metadata_path: Path

    @classmethod
    def create(cls, *, package_id: str) -> BuildPaths:
        staging_root = Path(tempfile.mkdtemp(prefix=f"neko_build_{package_id}_")).resolve()
        try:
            payload_dir = staging_root / "payload"
            plugins_dir = payload_dir / "plugins"
            profiles_dir = payload_dir / "profiles"
            manifest_path = staging_root / "manifest.toml"
            metadata_path = staging_root / "metadata.toml"

            plugins_dir.mkdir(parents=True, exist_ok=True)
            profiles_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise

        return cls(
            staging_root=staging_root,
            payload_dir=payload_dir,
            plugins_dir=plugins_dir,
            profiles_dir=profiles_dir,
            manifest_path=manifest_path,
            metadata_path=metadata_path,
        )


class PluginBuilder:
    """Service object that owns plugin and bundle packaging pipelines."""

    def build_plugin(
        self,
        plugin_dir: str | Path,
        out_file: str | Path | None = None,
        *,
        keep_staging: bool = False,
    ) -> BuildResult:
        source = load_plugin_source(plugin_dir)
        paths = BuildPaths.create(package_id=source.plugin_id)
        try:
            payload = self.build_single_payload(source, paths)
            self.write_manifest(
                package_type="plugin",
                package_id=source.plugin_id,
                package_name=source.name,
                version=source.version,
                package_description=source.description,
                paths=paths,
            )
            self.write_metadata(
                payload_hash=payload.payload_hash,
                source_kind="local",
                source_paths=[Path(source.plugin_id)],
                paths=paths,
            )

            package_path = (
                Path(out_file).expanduser().resolve()
                if out_file is not None
                else source.plugin_dir.parent / source.default_package_name
            )
            self.export_package(paths.staging_root, package_path)
            return self.build_result(
                package_id=source.plugin_id,
                package_type="plugin",
                plugin_ids=[source.plugin_id],
                package_name=source.name,
                version=source.version,
                payload=payload,
                package_path=package_path,
                paths=paths,
                keep_staging=keep_staging,
            )
        finally:
            if not keep_staging:
                shutil.rmtree(paths.staging_root, ignore_errors=True)

    def build_bundle(
        self,
        plugin_dirs: list[str | Path],
        out_file: str | Path | None = None,
        *,
        bundle_id: str | None = None,
        package_name: str | None = None,
        package_description: str | None = None,
        version: str = "0.1.0",
        keep_staging: bool = False,
    ) -> BuildResult:
        sources = [load_plugin_source(item) for item in plugin_dirs]
        if len(sources) < 2:
            raise ValueError(
                f"bundle build requires at least two plugins, but only "
                f"{len(sources)} plugin(s) were provided. "
                f"Use build_plugin() for single-plugin builds."
            )

        plugin_ids = [source.plugin_id for source in sources]
        if len(set(plugin_ids)) != len(plugin_ids):
            seen: dict[str, int] = {}
            for pid in plugin_ids:
                seen[pid] = seen.get(pid, 0) + 1
            duplicates = [f"'{pid}' (x{count})" for pid, count in seen.items() if count > 1]
            raise ValueError(
                f"bundle build does not support duplicate plugin_ids. "
                f"Duplicates found: {', '.join(duplicates)}. "
                f"Each plugin in a bundle must have a unique [plugin].id in its plugin.toml."
            )

        resolved_bundle_id = _validate_package_id(
            bundle_id or self.build_bundle_id(plugin_ids),
            label="bundle_id",
        )
        resolved_package_name = (package_name or f"{resolved_bundle_id} bundle").strip()
        resolved_description = (package_description or f"Bundle package for {', '.join(plugin_ids)}").strip()
        resolved_version = version.strip() or "0.1.0"

        paths = BuildPaths.create(package_id=resolved_bundle_id)
        try:
            payload = self.build_bundle_payload(sources, paths)
            self.write_manifest(
                package_type="bundle",
                package_id=resolved_bundle_id,
                package_name=resolved_package_name,
                version=resolved_version,
                package_description=resolved_description,
                paths=paths,
            )
            self.write_metadata(
                payload_hash=payload.payload_hash,
                source_kind="local_bundle",
                source_paths=[Path(source.plugin_id) for source in sources],
                paths=paths,
            )

            package_path = (
                Path(out_file).expanduser().resolve()
                if out_file is not None
                else sources[0].plugin_dir.parent / f"{resolved_bundle_id}-{resolved_version}.neko-bundle"
            )
            self.export_package(paths.staging_root, package_path)
            return self.build_result(
                package_id=resolved_bundle_id,
                package_type="bundle",
                plugin_ids=plugin_ids,
                package_name=resolved_package_name,
                version=resolved_version,
                payload=payload,
                package_path=package_path,
                paths=paths,
                keep_staging=keep_staging,
            )
        finally:
            if not keep_staging:
                shutil.rmtree(paths.staging_root, ignore_errors=True)

    def build_single_payload(
        self,
        source: PluginSource,
        paths: BuildPaths,
    ) -> PayloadBuildResult:
        validate_source_dependency_layout(source)
        build_rules = load_build_rules(source.pyproject_toml)
        plugin_payload_dir = paths.plugins_dir / source.plugin_id
        plugin_payload_dir.mkdir(parents=True, exist_ok=True)
        staged_files = self.copy_plugin_runtime_files(
            source.plugin_dir,
            plugin_payload_dir,
            rules=build_rules,
        )
        # 在算 payload_hash 之前写：plugin.meta.json 是包内容的一部分，不能游离在
        # 完整性校验之外。导不进来的插件拿到 None，包照常出，只是不带元数据。
        staged_metadata = write_packaged_metadata(
            source_dir=source.plugin_dir,
            target_dir=plugin_payload_dir,
        )
        # 仓库里的插件目录已经带着 plugin.meta.json，copy_plugin_runtime_files 会
        # 把它复制过去并记进 staged_files；这里覆盖的是同一个路径，再 append 一次就
        # 会让 staged_file_count 多算、--keep-staging 重复列出同一个文件（coderabbit）。
        # 探测失败时那份旧的会被删掉，清单也要跟着少一条（codex）。
        _settle_staged_metadata(staged_files, plugin_payload_dir, staged_metadata)
        profile_files = write_default_profile(source, paths.profiles_dir)
        write_dependency_manifest([source], paths.payload_dir)
        validate_payload_dependency_layout(paths.payload_dir, [source.plugin_id])
        payload_hash = self.compute_payload_hash(paths.payload_dir)

        return PayloadBuildResult(
            staging_dir=paths.staging_root,
            payload_dir=paths.payload_dir,
            plugin_payload_dir=plugin_payload_dir,
            profiles_dir=paths.profiles_dir,
            staged_files=staged_files,
            profile_files=profile_files,
            payload_hash=payload_hash,
        )

    def build_bundle_payload(
        self,
        sources: list[PluginSource],
        paths: BuildPaths,
    ) -> PayloadBuildResult:
        for source in sources:
            validate_source_dependency_layout(source)
        staged_files: list[Path] = []
        for source in sources:
            plugin_payload_dir = paths.plugins_dir / source.plugin_id
            plugin_payload_dir.mkdir(parents=True, exist_ok=True)
            build_rules = load_build_rules(source.pyproject_toml)
            staged_files.extend(
                self.copy_plugin_runtime_files(
                    source.plugin_dir,
                    plugin_payload_dir,
                    rules=build_rules,
                )
            )
            staged_metadata = write_packaged_metadata(
                source_dir=source.plugin_dir,
                target_dir=plugin_payload_dir,
            )
            _settle_staged_metadata(staged_files, plugin_payload_dir, staged_metadata)

        profile_files = write_bundle_profile(sources, paths.profiles_dir)
        write_dependency_manifest(sources, paths.payload_dir)
        validate_payload_dependency_layout(
            paths.payload_dir,
            [source.plugin_id for source in sources],
        )
        payload_hash = self.compute_payload_hash(paths.payload_dir)

        return PayloadBuildResult(
            staging_dir=paths.staging_root,
            payload_dir=paths.payload_dir,
            plugin_payload_dir=paths.plugins_dir / sources[0].plugin_id,
            profiles_dir=paths.profiles_dir,
            staged_files=staged_files,
            profile_files=profile_files,
            payload_hash=payload_hash,
        )

    def copy_plugin_runtime_files(
        self,
        source_dir: Path,
        destination_dir: Path,
        *,
        rules: BuildRuleSet,
    ) -> list[Path]:
        copied: list[Path] = []
        for path in sorted(source_dir.rglob("*")):
            relative = path.relative_to(source_dir)
            if self.should_skip(relative, is_dir=path.is_dir(), rules=rules):
                continue
            destination_path = destination_dir / relative
            if path.is_dir():
                destination_path.mkdir(parents=True, exist_ok=True)
                continue
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination_path)
            copied.append(destination_path.resolve())
        return copied

    def should_skip(self, relative_path: Path, *, is_dir: bool, rules: BuildRuleSet) -> bool:
        return should_skip_path(relative_path, is_dir=is_dir, rules=rules)

    def write_manifest(
        self,
        *,
        package_type: str,
        package_id: str,
        package_name: str,
        version: str,
        package_description: str,
        paths: BuildPaths,
    ) -> None:
        lines = [
            f'schema_version = "{_SCHEMA_VERSION}"',
            f'package_type = "{escape_string(package_type)}"',
            "",
            f'id = "{escape_string(package_id)}"',
            f'package_name = "{escape_string(package_name)}"',
            f'version = "{escape_string(version)}"',
        ]

        if package_description:
            lines.append(f'package_description = "{escape_string(package_description)}"')

        lines.append("")
        # Force LF line endings regardless of platform.
        paths.manifest_path.write_text(
            "\n".join(lines), encoding="utf-8", newline="\n",
        )

    def write_metadata(
        self,
        *,
        payload_hash: str,
        source_kind: str,
        source_paths: list[Path],
        paths: BuildPaths,
    ) -> None:
        safe_sources = []
        for item in source_paths:
            item_path = Path(item)
            if item_path.is_absolute():
                safe_sources.append(item_path.name)
            else:
                safe_sources.append(item_path.as_posix())
        source_values = ", ".join(f'"{escape_string(item)}"' for item in safe_sources)
        content = "\n".join(
            [
                "[payload]",
                'hash_algorithm = "sha256"',
                f'hash = "{payload_hash}"',
                "",
                "[source]",
                f'kind = "{escape_string(source_kind)}"',
                f"paths = [{source_values}]",
                "",
            ]
        )
        # Force LF line endings regardless of platform.
        paths.metadata_path.write_text(content, encoding="utf-8", newline="\n")

    def export_package(self, staging_root: Path, package_path: Path) -> None:
        """Write the staging tree into a ZIP archive.

        Files are sorted by their NFC-normalized posix-relative path so that
        the archive entry order is identical on every platform.
        """
        package_path.parent.mkdir(parents=True, exist_ok=True)
        file_entries = [
            (normalize_relative_posix(path, staging_root), path)
            for path in staging_root.rglob("*")
            if not path.is_dir()
        ]
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for arcname, path in sorted(file_entries, key=lambda item: item[0]):
                archive.write(path, arcname=arcname)

    def compute_payload_hash(self, payload_dir: Path) -> str:
        """Compute SHA-256 over all files under *payload_dir*.

        Uses the same algorithm as :func:`archive_utils.compute_archive_payload_hash`:
        entries are sorted by NFC-normalized posix-relative path (case-sensitive),
        and each entry contributes ``path + NUL + content + NUL`` to the digest.
        """
        digest = hashlib.sha256()
        file_entries = [
            (normalize_relative_posix(path, payload_dir), path)
            for path in payload_dir.rglob("*")
            if not path.is_dir()
        ]
        for relative, path in sorted(file_entries, key=lambda item: item[0]):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def build_result(
        self,
        *,
        package_id: str,
        package_type: str,
        plugin_ids: list[str],
        package_name: str,
        version: str,
        payload: PayloadBuildResult,
        package_path: Path,
        paths: BuildPaths,
        keep_staging: bool,
    ) -> BuildResult:
        return BuildResult(
            plugin_id=package_id,
            package_type=package_type,
            plugin_ids=plugin_ids,
            package_name=package_name,
            version=version,
            package_path=package_path,
            staging_dir=paths.staging_root if keep_staging else None,
            profile_files=payload.profile_files if keep_staging else [],
            staged_files=payload.staged_files if keep_staging else [],
            payload_hash=payload.payload_hash,
        )

    def build_bundle_id(self, plugin_ids: list[str]) -> str:
        return "__".join(sorted(plugin_ids))


def build_plugin(
    plugin_dir: str | Path,
    out_file: str | Path | None = None,
    *,
    keep_staging: bool = False,
) -> BuildResult:
    """Public convenience wrapper for one-shot single-plugin builds."""

    return PluginBuilder().build_plugin(
        plugin_dir=plugin_dir,
        out_file=out_file,
        keep_staging=keep_staging,
    )


def build_bundle(
    plugin_dirs: list[str | Path],
    out_file: str | Path | None = None,
    *,
    bundle_id: str | None = None,
    package_name: str | None = None,
    package_description: str | None = None,
    version: str = "0.1.0",
    keep_staging: bool = False,
) -> BuildResult:
    """Public convenience wrapper for one-shot multi-plugin bundle builds."""

    return PluginBuilder().build_bundle(
        plugin_dirs=plugin_dirs,
        out_file=out_file,
        bundle_id=bundle_id,
        package_name=package_name,
        package_description=package_description,
        version=version,
        keep_staging=keep_staging,
    )
