from __future__ import annotations

import hashlib
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .models import PackResult, PayloadBuildResult, PluginSource
from .pack_rules import PackRuleSet, load_pack_rules, should_skip_path
from ..core.build import _settle_staged_metadata
from ..core.metadata_probe import write_packaged_metadata
from .plugin_source import load_plugin_source
from .profile import write_bundle_profile, write_default_profile
from .toml_utils import escape_string

_SCHEMA_VERSION = "1.0"


@dataclass(slots=True)
class PackPaths:
    """Resolved staging layout for a pack operation."""

    staging_root: Path
    payload_dir: Path
    plugins_dir: Path
    profiles_dir: Path
    manifest_path: Path
    metadata_path: Path

    @classmethod
    def create(cls, *, package_id: str) -> PackPaths:
        staging_root = Path(tempfile.mkdtemp(prefix=f"neko_pack_{package_id}_")).resolve()
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


class PluginPacker:
    """Service object that owns plugin and bundle packaging pipelines."""

    def pack_plugin(
        self,
        plugin_dir: str | Path,
        out_file: str | Path | None = None,
        *,
        keep_staging: bool = False,
    ) -> PackResult:
        source = load_plugin_source(plugin_dir)
        if source.package_type != "plugin":
            raise ValueError(
                f"single-plugin pack only supports package_type='plugin', got {source.package_type!r}"
            )
        paths = PackPaths.create(package_id=source.plugin_id)
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
                source_paths=[source.plugin_dir],
                paths=paths,
            )

            package_path = (
                Path(out_file).expanduser().resolve()
                if out_file is not None
                else source.plugin_dir.parent / source.default_package_name
            )
            self.export_package(paths.staging_root, package_path)
            return self.build_pack_result(
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

    def pack_bundle(
        self,
        plugin_dirs: list[str | Path],
        out_file: str | Path | None = None,
        *,
        bundle_id: str | None = None,
        package_name: str | None = None,
        package_description: str | None = None,
        version: str = "0.1.0",
        keep_staging: bool = False,
    ) -> PackResult:
        sources = [load_plugin_source(item) for item in plugin_dirs]
        if len(sources) < 2:
            raise ValueError("bundle pack requires at least two plugins")

        plugin_ids = [source.plugin_id for source in sources]
        if len(set(plugin_ids)) != len(plugin_ids):
            raise ValueError("bundle pack does not support duplicate plugin_ids")

        resolved_bundle_id = (bundle_id or self.build_bundle_id(plugin_ids)).strip()
        resolved_package_name = (package_name or f"{resolved_bundle_id} bundle").strip()
        resolved_description = (package_description or f"Bundle package for {', '.join(plugin_ids)}").strip()
        resolved_version = version.strip() or "0.1.0"

        paths = PackPaths.create(package_id=resolved_bundle_id)
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
                source_paths=[source.plugin_dir for source in sources],
                paths=paths,
            )

            package_path = (
                Path(out_file).expanduser().resolve()
                if out_file is not None
                else sources[0].plugin_dir.parent / f"{resolved_bundle_id}-{resolved_version}.neko-bundle"
            )
            self.export_package(paths.staging_root, package_path)
            return self.build_pack_result(
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
        paths: PackPaths,
    ) -> PayloadBuildResult:
        pack_rules = load_pack_rules(source.pyproject_toml)
        plugin_payload_dir = paths.plugins_dir / source.plugin_id
        plugin_payload_dir.mkdir(parents=True, exist_ok=True)
        staged_files = self.copy_plugin_runtime_files(
            source.plugin_dir,
            plugin_payload_dir,
            rules=pack_rules,
        )
        # 这条向后兼容的打包 API 有自己的 payload 构建，不经过 core.build
        # （codex）。不在这里写的话，走它打出来的包在用户机器上没有元数据，
        # 入口 schema 会一路退化成占位。
        _settle_staged_metadata(
            staged_files,
            plugin_payload_dir,
            write_packaged_metadata(
                source_dir=source.plugin_dir,
                target_dir=plugin_payload_dir,
            ),
        )
        profile_files = write_default_profile(source, paths.profiles_dir)
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
        paths: PackPaths,
    ) -> PayloadBuildResult:
        staged_files: list[Path] = []
        for source in sources:
            plugin_payload_dir = paths.plugins_dir / source.plugin_id
            plugin_payload_dir.mkdir(parents=True, exist_ok=True)
            pack_rules = load_pack_rules(source.pyproject_toml)
            staged_files.extend(
                self.copy_plugin_runtime_files(
                    source.plugin_dir,
                    plugin_payload_dir,
                    rules=pack_rules,
                )
            )
            _settle_staged_metadata(
                staged_files,
                plugin_payload_dir,
                write_packaged_metadata(
                    source_dir=source.plugin_dir,
                    target_dir=plugin_payload_dir,
                ),
            )

        profile_files = write_bundle_profile(sources, paths.profiles_dir)
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
        rules: PackRuleSet,
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

    def should_skip(self, relative_path: Path, *, is_dir: bool, rules: PackRuleSet) -> bool:
        return should_skip_path(relative_path, is_dir=is_dir, rules=rules)

    def write_manifest(
        self,
        *,
        package_type: str,
        package_id: str,
        package_name: str,
        version: str,
        package_description: str,
        paths: PackPaths,
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
        paths.manifest_path.write_text("\n".join(lines), encoding="utf-8")

    def write_metadata(
        self,
        *,
        payload_hash: str,
        source_kind: str,
        source_paths: list[Path],
        paths: PackPaths,
    ) -> None:
        source_values = ", ".join(f'"{escape_string(str(item))}"' for item in source_paths)
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
        paths.metadata_path.write_text(content, encoding="utf-8")

    def export_package(self, staging_root: Path, package_path: Path) -> None:
        package_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging_root.rglob("*")):
                if path.is_dir():
                    continue
                archive.write(path, arcname=path.relative_to(staging_root).as_posix())

    def compute_payload_hash(self, payload_dir: Path) -> str:
        # Sort by the posix relative path (matches `compute_archive_payload_hash`).
        # `sorted(rglob(...))` sorts WindowsPath objects case-insensitively, which
        # diverges from the archive-side hash that sorts case-sensitively on the
        # posix path string and would yield a payload-hash mismatch on Windows.
        entries: list[tuple[str, Path]] = []
        for path in payload_dir.rglob("*"):
            if path.is_dir():
                continue
            entries.append((path.relative_to(payload_dir).as_posix(), path))

        digest = hashlib.sha256()
        for relative, path in sorted(entries, key=lambda item: item[0]):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def build_pack_result(
        self,
        *,
        package_id: str,
        package_type: str,
        plugin_ids: list[str],
        package_name: str,
        version: str,
        payload: PayloadBuildResult,
        package_path: Path,
        paths: PackPaths,
        keep_staging: bool,
    ) -> PackResult:
        return PackResult(
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


def pack_plugin(
    plugin_dir: str | Path,
    out_file: str | Path | None = None,
    *,
    keep_staging: bool = False,
) -> PackResult:
    """Public convenience wrapper for one-shot single-plugin packaging."""

    return PluginPacker().pack_plugin(
        plugin_dir=plugin_dir,
        out_file=out_file,
        keep_staging=keep_staging,
    )


def pack_bundle(
    plugin_dirs: list[str | Path],
    out_file: str | Path | None = None,
    *,
    bundle_id: str | None = None,
    package_name: str | None = None,
    package_description: str | None = None,
    version: str = "0.1.0",
    keep_staging: bool = False,
) -> PackResult:
    """Public convenience wrapper for one-shot multi-plugin bundle packaging."""

    return PluginPacker().pack_bundle(
        plugin_dirs=plugin_dirs,
        out_file=out_file,
        bundle_id=bundle_id,
        package_name=package_name,
        package_description=package_description,
        version=version,
        keep_staging=keep_staging,
    )
