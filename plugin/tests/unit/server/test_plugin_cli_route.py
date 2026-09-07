from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from plugin.neko_plugin_cli.public import pack_plugin
from plugin.server.application.plugin_cli import service as plugin_cli_service
from plugin.server.application.plugin_cli.service import PluginCliService
from plugin.server.application.install_source import (
    InstallSourceError,
    InstallSourceManager,
    PluginDirectoryScanner,
    set_global_manager,
)
from plugin.server.domain.errors import ServerDomainError
from plugin.server.infrastructure.exceptions import register_exception_handlers
from plugin.server.routes.plugin_cli import router
from plugin.server.routes import plugin_cli as plugin_cli_routes

pytestmark = pytest.mark.plugin_unit
FIXTURE_PLUGINS_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "neko_plugin_cli" / "plugins"


def _make_plugin_dir(
    tmp_path: Path,
    plugin_id: str = "route_demo",
    *,
    version: str = "0.0.1",
) -> Path:
    plugin_dir = tmp_path / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f'id = "{plugin_id}"',
                'name = "Route Demo"',
                f'version = "{version}"',
                'type = "plugin"',
                "",
                f"[{plugin_id}]",
                'value = "demo"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    return plugin_dir


def _copy_fixture_plugin(tmp_path: Path, fixture_name: str) -> Path:
    source = FIXTURE_PLUGINS_ROOT / fixture_name
    target = tmp_path / fixture_name
    shutil.copytree(source, target)
    if fixture_name == "bundle_alpha":
        _write_vendor_dist(target, "shared-lib", "2.0.0")
        _write_vendor_dist(target, "alpha-only", "0.1.0")
    elif fixture_name == "bundle_beta":
        _write_vendor_dist(target, "shared-lib", "2.0.0")
        _write_vendor_dist(target, "beta-only", "0.5.0")
    return target


def _write_vendor_dist(plugin_dir: Path, name: str, version: str) -> None:
    dist_dir = plugin_dir / "vendor" / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


def _make_archive_limit_package(
    tmp_path: Path,
    *,
    attack: str,
) -> tuple[Path, str]:
    plugin_id = f"archive_{attack}"
    package_path = tmp_path / "packages" / f"{plugin_id}.neko-plugin"
    package_path.parent.mkdir(parents=True)
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id), package_path)
    member_name = f"payload/plugins/{plugin_id}/bomb.bin"
    if attack == "compression_ratio":
        content = b"\0" * (256 * 1024)
        compression = zipfile.ZIP_DEFLATED
    else:
        content = b"x" * 2048
        compression = zipfile.ZIP_STORED
    with zipfile.ZipFile(package_path, "a") as archive:
        archive.writestr(member_name, content, compress_type=compression)
    return package_path, member_name


def _patch_plugin_cli_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    builtin_root: Path,
    user_root: Path | None = None,
    packages_root: Path | None = None,
    profiles_root: Path | None = None,
) -> None:
    import plugin.settings as plugin_settings

    monkeypatch.setattr(plugin_settings, "BUILTIN_PLUGIN_CONFIG_ROOT", builtin_root)
    monkeypatch.setattr(plugin_settings, "USER_PLUGIN_CONFIG_ROOT", user_root or builtin_root)
    monkeypatch.setattr(plugin_settings, "USER_PLUGIN_PACKAGES_ROOT", packages_root or builtin_root)
    monkeypatch.setattr(plugin_settings, "USER_PACKAGE_PROFILES_ROOT", profiles_root or (builtin_root / "profiles"))


def _set_imported_owner(
    *,
    tmp_path: Path,
    builtin_root: Path,
    user_root: Path,
    directory_path: Path,
    package_id: str,
    profile_dir: Path | None = None,
) -> InstallSourceManager:
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    manager.record_import(
        directory_path=directory_path,
        package_filename=f"{package_id}.neko-plugin",
        package_sha256="a" * 64,
        package_id=package_id,
        profile_dir=str(profile_dir) if profile_dir is not None else "",
    )
    set_global_manager(manager)
    return manager


class _MemoryUploadFile:
    def __init__(self) -> None:
        self.filename = "demo.neko-plugin"

    async def read(self) -> bytes:
        return b"demo"


def _market_install_override(plugin_id: str) -> dict[str, object]:
    return {
        "channel": "market",
        "mode": "install",
        "market_detail": {
            "plugin_market_id": plugin_id,
            "version": "1.0.0",
            "package_url": f"https://example.invalid/{plugin_id}.neko-plugin",
            "expected_plugin_toml_id": plugin_id,
            "published_at": "2026-09-02T00:00:00Z",
        },
    }


@pytest.mark.asyncio
async def test_save_uploaded_file_streams_and_accepts_uppercase_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=packages_root)

    result = await PluginCliService().save_uploaded_file(
        filename="DEMO.NEKO-PLUGIN",
        source_file=BytesIO(b"package-bytes"),
    )

    saved_path = Path(str(result["path"]))
    assert saved_path.name == "DEMO.NEKO-PLUGIN"
    assert saved_path.read_bytes() == b"package-bytes"
    assert PluginCliService()._resolve_package_path(str(saved_path)) == saved_path


@pytest.mark.asyncio
async def test_save_uploaded_file_removes_partial_file_when_size_limit_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=packages_root)
    monkeypatch.setattr(plugin_cli_service, "_UPLOAD_MAX_BYTES", 5)

    with pytest.raises(ServerDomainError, match="File too large"):
        await PluginCliService().save_uploaded_file(
            filename="demo.neko-plugin",
            source_file=BytesIO(b"123456"),
        )

    assert not list(packages_root.glob("*"))


@pytest.mark.asyncio
async def test_discard_uploaded_package_only_removes_the_selected_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=packages_root)
    service = PluginCliService()
    selected = await service.save_uploaded_file(
        filename="demo.neko-plugin",
        source_file=BytesIO(b"selected"),
    )
    preserved = await service.save_uploaded_file(
        filename="demo.neko-plugin",
        source_file=BytesIO(b"preserved"),
    )

    result = await service.discard_uploaded_package(package=str(selected["path"]))

    assert result == {"success": True, "removed": True, "name": selected["name"]}
    assert not Path(str(selected["path"])).exists()
    assert Path(str(preserved["path"])).read_bytes() == b"preserved"


@pytest.mark.asyncio
async def test_discard_uploaded_package_rejects_paths_outside_artifact_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    outside = tmp_path / "outside.neko-plugin"
    outside.write_bytes(b"outside")
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=packages_root)

    with pytest.raises(ServerDomainError):
        await PluginCliService().discard_uploaded_package(package=str(outside))

    assert outside.read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_discard_uploaded_package_route_removes_only_requested_upload(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = tmp_path / "packages"
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=packages_root)
    selected = await plugin_cli_routes.service.save_uploaded_file(
        filename="selected.neko-plugin",
        source_file=BytesIO(b"selected"),
    )
    preserved = await plugin_cli_routes.service.save_uploaded_file(
        filename="preserved.neko-plugin",
        source_file=BytesIO(b"preserved"),
    )

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.delete(
            "/plugin-cli/upload",
            params={"package": str(selected["path"])},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "removed": True,
        "name": "selected.neko-plugin",
    }
    assert not Path(str(selected["path"])).exists()
    assert Path(str(preserved["path"])).read_bytes() == b"preserved"


@pytest.fixture
def plugin_cli_test_app() -> FastAPI:
    app = FastAPI(title="plugin-cli-test-app")
    register_exception_handlers(app)
    app.include_router(router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "/plugin-cli/inspect",
        "/plugin-cli/verify",
        "/plugin-cli/install-plan",
        "/plugin-cli/install",
    ],
)
@pytest.mark.parametrize(
    ("attack", "expected_detail"),
    [
        ("compression_ratio", "compression ratio"),
        ("oversized_member", "single-member limit"),
    ],
)
async def test_package_entrypoints_reject_archive_bombs_before_reading_payload(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    attack: str,
    expected_detail: str,
) -> None:
    from plugin.neko_plugin_cli.core import archive_utils

    package_path, bomb_member = _make_archive_limit_package(tmp_path, attack=attack)
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=tmp_path / "plugins",
        packages_root=package_path.parent,
        profiles_root=tmp_path / "profiles",
    )
    if attack == "oversized_member":
        monkeypatch.setattr(archive_utils, "MAX_ARCHIVE_MEMBER_BYTES", 1024)
        monkeypatch.setattr(
            archive_utils,
            "MAX_ARCHIVE_COMPRESSION_RATIO",
            1_000_000,
        )
    original_open = zipfile.ZipFile.open

    def guarded_open(archive, member, *args, **kwargs):  # type: ignore[no-untyped-def]
        member_name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if member_name == bomb_member:
            raise AssertionError("archive bomb payload must not be opened")
        return original_open(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", guarded_open)
    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(endpoint, json={"package": str(package_path)})

    assert response.status_code == 400
    assert response.headers["x-error-code"] == "PLUGIN_PACKAGE_INVALID_ARCHIVE"
    assert expected_detail in response.text


def test_upload_and_unpack_legacy_returns_unpack_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_upload_and_install(*_args, **_kwargs) -> dict[str, object]:
        return {
            "upload": {"filename": "demo.neko-plugin"},
            "install": {
                "installed_plugins": ["demo"],
                "installed_plugin_count": 1,
            },
        }

    monkeypatch.setattr(
        plugin_cli_routes,
        "plugin_cli_upload_and_install",
        fake_upload_and_install,
    )

    import asyncio

    body = asyncio.run(
        plugin_cli_routes.plugin_cli_upload_and_unpack_legacy(
            _MemoryUploadFile(),  # type: ignore[arg-type]
            on_conflict="fail",
            _="",
        )
    )

    assert "install" not in body
    assert body["upload"] == {"filename": "demo.neko-plugin"}
    assert body["unpack"] == {
        "unpacked_plugins": ["demo"],
        "unpacked_plugin_count": 1,
    }


@pytest.mark.asyncio
async def test_plugin_cli_inspect_and_verify_routes(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    package_path = tmp_path / "route_demo.neko-plugin"
    pack_plugin(plugin_dir, package_path)
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=tmp_path)

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        inspect_response = await client.post(
            "/plugin-cli/inspect",
            json={"package": str(package_path)},
        )
        assert inspect_response.status_code == 200
        inspect_body = inspect_response.json()
        assert inspect_body["package_id"] == "route_demo"
        assert inspect_body["payload_hash_verified"] is True

        verify_response = await client.post(
            "/plugin-cli/verify",
            json={"package": str(package_path)},
        )
        assert verify_response.status_code == 200
        verify_body = verify_response.json()
        assert verify_body["ok"] is True


@pytest.mark.asyncio
async def test_plugin_cli_list_plugins_route_returns_shape(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_plugin_dir(tmp_path, plugin_id="route_list_demo")
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=tmp_path)

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/plugin-cli/plugins")

        assert response.status_code == 200
        body = response.json()
        assert "plugins" in body
        assert "count" in body
        assert isinstance(body["plugins"], list)
        assert body["plugins"] == ["route_list_demo"]
        assert body["plugin_refs"] == [
            {
                "root_id": "builtin",
                "directory_name": "route_list_demo",
                "plugin_id": "route_list_demo",
                "label": "builtin/route_list_demo",
            }
        ]


@pytest.mark.asyncio
async def test_plugin_cli_build_single_legacy_string_resolves_user_root_when_builtin_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steam_builtin_root = tmp_path / "steam" / "steamapps" / "common" / "NEKO" / "resources" / "plugin" / "plugins"
    user_root = tmp_path / "documents" / "Neko" / "plugins"
    packages_root = tmp_path / "documents" / "Neko" / "packages"
    steam_builtin_root.mkdir(parents=True)
    user_root.mkdir(parents=True)
    packages_root.mkdir(parents=True)
    _make_plugin_dir(user_root, plugin_id="neko_minecraft")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=steam_builtin_root,
        user_root=user_root,
        packages_root=packages_root,
    )

    body = await PluginCliService().build(mode="single", plugin="neko_minecraft")

    assert body["ok"] is True
    assert body["built_count"] == 1
    built = body["built"][0]
    assert built["plugin_id"] == "neko_minecraft"
    assert Path(built["package_path"]).is_relative_to(packages_root.resolve())


@pytest.mark.asyncio
async def test_plugin_cli_build_all_includes_builtin_and_user_in_stable_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    packages_root = tmp_path / "packages"
    _make_plugin_dir(builtin_root, plugin_id="builtin_z")
    _make_plugin_dir(builtin_root, plugin_id="builtin_a")
    _make_plugin_dir(user_root, plugin_id="user_a")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=packages_root,
    )

    body = await PluginCliService().build(mode="all")

    assert body["ok"] is True
    assert [item["plugin_id"] for item in body["built"]] == [
        "builtin_a",
        "builtin_z",
        "user_a",
    ]


@pytest.mark.asyncio
async def test_plugin_cli_build_single_plugin_ref_routes_to_exact_user_plugin(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    packages_root = tmp_path / "packages"
    _make_plugin_dir(builtin_root, plugin_id="shared")
    shared_user = user_root / "shared"
    shared_user.mkdir(parents=True, exist_ok=True)
    (shared_user / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                'id = "shared_user"',
                'name = "Shared User"',
                'version = "0.0.1"',
                'type = "plugin"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=packages_root,
    )

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/plugin-cli/build",
            json={
                "mode": "single",
                "plugin_ref": {"root_id": "user", "directory_name": "shared"},
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["built"][0]["plugin_id"] == "shared_user"


@pytest.mark.asyncio
async def test_plugin_cli_build_rejects_target_dir_outside_package_artifacts_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_root = tmp_path / "builtin"
    packages_root = tmp_path / "packages"
    outside_root = tmp_path / "outside"
    _make_plugin_dir(builtin_root, plugin_id="route_outside_demo")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        packages_root=packages_root,
    )

    with pytest.raises(ServerDomainError) as info:
        await PluginCliService().build(
            mode="single",
            plugin="route_outside_demo",
            target_dir=str(outside_root),
        )

    assert info.value.status_code == 400
    assert not list(outside_root.glob("*.neko-plugin"))
    assert not list(packages_root.glob("*.neko-plugin"))


@pytest.mark.asyncio
async def test_plugin_cli_list_packages_route_returns_target_packages(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path, plugin_id="route_pkg_demo")
    package_path = tmp_path / "route_pkg_demo.neko-plugin"
    pack_plugin(plugin_dir, package_path)
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=tmp_path)

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/plugin-cli/packages")

        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["target_dir"] == str(tmp_path)
        assert body["packages"][0]["name"] == "route_pkg_demo.neko-plugin"


@pytest.mark.asyncio
async def test_plugin_cli_lists_legacy_packages_beside_explicit_config_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.settings as plugin_settings

    custom_exec_root = tmp_path / "custom" / "plugins"
    legacy_packages_root = custom_exec_root.parent / ".neko-plugin-packages"
    legacy_packages_root.mkdir(parents=True)
    package_path = legacy_packages_root / "legacy.neko-plugin"
    package_path.write_bytes(b"legacy package")
    monkeypatch.setenv("PLUGIN_CONFIG_ROOT", str(custom_exec_root))
    monkeypatch.delenv("PLUGIN_PACKAGES_ROOT", raising=False)
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=custom_exec_root,
        packages_root=plugin_settings.get_user_plugin_packages_root(),
    )

    result = await PluginCliService().list_local_packages()

    assert result["target_dir"] == str(legacy_packages_root.resolve())
    assert [item["name"] for item in result["packages"]] == [package_path.name]


@pytest.mark.asyncio
async def test_plugin_cli_pack_bundle_route_uses_mode_payload(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_plugin_dir(tmp_path, plugin_id="route_bundle_one")
    _make_plugin_dir(tmp_path, plugin_id="route_bundle_two")
    target_dir = tmp_path / "target"
    _patch_plugin_cli_settings(monkeypatch, builtin_root=tmp_path, packages_root=tmp_path)

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/plugin-cli/pack",
            json={
                "mode": "bundle",
                "plugins": ["route_bundle_one", "route_bundle_two"],
                "bundle_id": "route_bundle_demo",
                "target_dir": str(target_dir),
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["packed_count"] == 1
        assert body["packed"][0]["package_type"] == "bundle"
        assert body["packed"][0]["plugin_ids"] == ["route_bundle_one", "route_bundle_two"]


@pytest.mark.asyncio
async def test_plugin_cli_route_workflow_pack_analyze_inspect_verify_and_unpack(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "runtime"
    alpha_dir = _copy_fixture_plugin(source_root, "bundle_alpha")
    beta_dir = _copy_fixture_plugin(source_root, "bundle_beta")
    target_dir = tmp_path / "target"
    plugins_root = source_root / "installed"
    profiles_root = tmp_path / "runtime_profiles"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin_plugins",
        user_root=source_root,
        packages_root=tmp_path,
        profiles_root=profiles_root,
    )

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        analyze_response = await client.post(
            "/plugin-cli/analyze",
            json={
                "plugins": [alpha_dir.name, beta_dir.name],
                "current_sdk_version": "2.3.0",
            },
        )
        assert analyze_response.status_code == 200
        analyze_body = analyze_response.json()
        assert analyze_body["plugin_ids"] == ["bundle_alpha", "bundle_beta"]
        assert analyze_body["sdk_supported_analysis"]["current_sdk_supported_by_all"] is True
        assert analyze_body["common_dependencies"][0]["name"] == "shared-lib"

        pack_response = await client.post(
            "/plugin-cli/pack",
            json={
                "mode": "bundle",
                "plugins": [alpha_dir.name, beta_dir.name],
                "bundle_id": "route_workflow_bundle",
                "package_name": "Route Workflow Bundle",
                "package_description": "Route workflow integration bundle.",
                "version": "1.0.0",
                "target_dir": str(target_dir),
            },
        )
        assert pack_response.status_code == 200
        pack_body = pack_response.json()
        assert pack_body["ok"] is True
        assert pack_body["packed_count"] == 1

        package_path = target_dir / "route_workflow_bundle.neko-bundle"
        assert package_path.is_file()

        inspect_response = await client.post(
            "/plugin-cli/inspect",
            json={"package": str(package_path)},
        )
        assert inspect_response.status_code == 200
        inspect_body = inspect_response.json()
        assert inspect_body["package_type"] == "bundle"
        assert inspect_body["package_name"] == "Route Workflow Bundle"
        assert inspect_body["plugin_count"] == 2
        assert inspect_body["payload_hash_verified"] is True

        verify_response = await client.post(
            "/plugin-cli/verify",
            json={"package": str(package_path)},
        )
        assert verify_response.status_code == 200
        verify_body = verify_response.json()
        assert verify_body["ok"] is True
        assert verify_body["payload_hash_verified"] is True

        unpack_response = await client.post(
            "/plugin-cli/unpack",
            json={
                "package": str(package_path),
                "plugins_root": str(plugins_root),
                "profiles_root": str(profiles_root),
                "on_conflict": "fail",
            },
        )
        assert unpack_response.status_code == 200
        unpack_body = unpack_response.json()
        assert unpack_body["package_type"] == "bundle"
        assert unpack_body["unpacked_plugin_count"] == 2
        assert unpack_body["payload_hash_verified"] is True
        assert (plugins_root / "bundle_alpha" / "plugin.toml").is_file()
        assert (plugins_root / "bundle_beta" / "plugin.toml").is_file()
        assert (profiles_root / "route_workflow_bundle" / "default.toml").is_file()


@pytest.mark.asyncio
async def test_plugin_cli_unpack_route_uses_default_roots_when_fields_omitted(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """省略 plugins_root/profiles_root 时，默认落盘到 _INSTALL_*_ROOT 下。"""
    plugin_dir = _copy_fixture_plugin(tmp_path, "simple_plugin")
    package_path = tmp_path / "simple_plugin.neko-plugin"
    pack_plugin(plugin_dir, package_path)

    default_plugins_root = tmp_path / "default_user_plugins"
    default_profiles_root = tmp_path / "default_user_profiles"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=default_plugins_root,
        packages_root=tmp_path,
        profiles_root=default_profiles_root,
    )

    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/plugin-cli/unpack",
            json={"package": str(package_path)},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["plugins_root"] == str(default_plugins_root.resolve())
        assert (default_plugins_root / "simple_plugin" / "plugin.toml").is_file()


@pytest.mark.asyncio
async def test_plugin_cli_install_plan_reports_matching_plugin_upgrade(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _copy_fixture_plugin(tmp_path, "simple_plugin")
    package_path = tmp_path / "simple_plugin.neko-plugin"
    pack_plugin(source, package_path)
    plugins_root = tmp_path / "plugins"
    installed = plugins_root / "simple_plugin"
    shutil.copytree(source, installed)
    manifest = (installed / "plugin.toml").read_text(encoding="utf-8")
    (installed / "plugin.toml").write_text(
        manifest.replace('version = "0.1.0"', 'version = "0.0.9"'),
        encoding="utf-8",
    )
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=plugins_root,
        packages_root=tmp_path,
        profiles_root=tmp_path / "profiles",
    )
    _set_imported_owner(
        tmp_path=tmp_path,
        builtin_root=tmp_path / "builtin",
        user_root=plugins_root,
        directory_path=installed,
        package_id="simple_plugin",
    )

    try:
        transport = ASGITransport(app=plugin_cli_test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/plugin-cli/install-plan",
                json={"package": str(package_path)},
            )
    finally:
        set_global_manager(None)

    assert response.status_code == 200
    assert response.json()["action"] == "upgrade"
    assert response.json()["plugin_id"] == "simple_plugin"


@pytest.mark.asyncio
async def test_plugin_cli_installs_over_manifestless_state_without_losing_state_or_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "state_only_demo"
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    package_path = packages_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    user_root = tmp_path / "user-plugins"
    state_target = user_root / plugin_id
    state_file = state_target / "data" / "user.db"
    state_file.parent.mkdir(parents=True)
    state_file.write_bytes(b"existing-user-state")
    profiles_root = tmp_path / "profiles"
    profile_file = profiles_root / plugin_id / "custom.toml"
    profile_file.parent.mkdir(parents=True)
    profile_file.write_bytes(b"existing-profile")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )
    service = PluginCliService()

    plan = await service.plan_install(package=str(package_path))

    assert plan["action"] == "reinstall"
    assert plan["reason"] == "manifestless_state"
    result = await service.install(
        package=str(package_path),
        confirm_upgrade=True,
        confirmation_token=str(plan["confirmation_token"]),
    )

    assert result["operation"] == "reinstall"
    assert (state_target / "plugin.toml").is_file()
    assert state_file.read_bytes() == b"existing-user-state"
    assert profile_file.read_bytes() == b"existing-profile"


@pytest.mark.asyncio
async def test_plugin_cli_still_blocks_manifestless_target_with_unknown_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "unknown_target_demo"
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    package_path = packages_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    user_root = tmp_path / "user-plugins"
    target = user_root / plugin_id
    target.mkdir(parents=True)
    unknown_file = target / "hand_edited.py"
    unknown_file.write_bytes(b"developer-copy")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=tmp_path / "profiles",
    )

    plan = await PluginCliService().plan_install(package=str(package_path))

    assert plan["action"] == "blocked"
    assert plan["reason"] == "directory_identity_conflict"
    assert unknown_file.read_bytes() == b"developer-copy"


@pytest.mark.asyncio
async def test_plugin_cli_route_upgrades_in_place_after_confirmation(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "route_upgrade_demo"
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    v1_source = _make_plugin_dir(
        tmp_path / "v1-source",
        plugin_id=plugin_id,
        version="1.0.0",
    )
    v2_source = _make_plugin_dir(
        tmp_path / "v2-source",
        plugin_id=plugin_id,
        version="2.0.0",
    )
    v1_package = packages_root / f"{plugin_id}-1.0.0.neko-plugin"
    v2_package = packages_root / f"{plugin_id}-2.0.0.neko-plugin"
    pack_plugin(v1_source, v1_package)
    pack_plugin(v2_source, v2_package)
    user_root = tmp_path / "user-plugins"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=tmp_path / "profiles",
    )
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        scanner=PluginDirectoryScanner(tmp_path / "builtin", user_root),
    )
    set_global_manager(manager)

    try:
        transport = ASGITransport(app=plugin_cli_test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            install_response = await client.post(
                "/plugin-cli/install",
                json={"package": str(v1_package)},
            )
            assert install_response.status_code == 200, install_response.text
            assert install_response.json()["operation"] == "install"
            manager.record_import(
                directory_path=user_root / plugin_id,
                package_filename=v1_package.name,
                package_sha256="a" * 64,
                package_id=plugin_id,
                profile_dir=str(tmp_path / "profiles" / plugin_id),
            )
            preserved_state = {
                "config/user.toml": b"user-config",
                "data/user.db": b"user-data",
                "cache/user.cache": b"user-cache",
            }
            for relative_path, payload in preserved_state.items():
                state_path = tmp_path / "runtime_data" / "plugins" / plugin_id / relative_path
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_bytes(payload)

            plan_response = await client.post(
                "/plugin-cli/install-plan",
                json={"package": str(v2_package)},
            )
            assert plan_response.status_code == 200, plan_response.text
            plan = plan_response.json()
            assert plan["action"] == "upgrade"

            upgrade_response = await client.post(
                "/plugin-cli/install",
                json={
                    "package": str(v2_package),
                    "confirm_upgrade": True,
                    "confirmation_token": plan["confirmation_token"],
                },
            )
    finally:
        set_global_manager(None)

    assert upgrade_response.status_code == 200, upgrade_response.text
    assert upgrade_response.json()["operation"] == "upgrade"
    installed_manifest = (user_root / plugin_id / "plugin.toml").read_text(encoding="utf-8")
    assert 'version = "2.0.0"' in installed_manifest
    for relative_path, payload in preserved_state.items():
        assert (
            tmp_path / "runtime_data" / "plugins" / plugin_id / relative_path
        ).read_bytes() == payload
    assert not (user_root / f"{plugin_id}_1").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installed_version", "package_version", "expected_operation"),
    [
        ("1.0.0", "1.0.0", "reinstall"),
        ("2.0.0", "0.9.0", "downgrade"),
    ],
)
async def test_plugin_cli_route_preserves_replacement_operation_after_confirmation(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_version: str,
    package_version: str,
    expected_operation: str,
) -> None:
    plugin_id = "route_replace_demo"
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    installed_source = _make_plugin_dir(
        tmp_path / "installed-source",
        plugin_id=plugin_id,
        version=installed_version,
    )
    package_source = _make_plugin_dir(
        tmp_path / "package-source",
        plugin_id=plugin_id,
        version=package_version,
    )
    installed_package = packages_root / f"{plugin_id}-installed.neko-plugin"
    replacement_package = packages_root / f"{plugin_id}-replacement.neko-plugin"
    pack_plugin(installed_source, installed_package)
    pack_plugin(package_source, replacement_package)
    user_root = tmp_path / "user-plugins"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=tmp_path / "profiles",
    )
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        scanner=PluginDirectoryScanner(tmp_path / "builtin", user_root),
    )
    set_global_manager(manager)

    try:
        transport = ASGITransport(app=plugin_cli_test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            install_response = await client.post(
                "/plugin-cli/install",
                json={"package": str(installed_package)},
            )
            assert install_response.status_code == 200, install_response.text
            manager.record_import(
                directory_path=user_root / plugin_id,
                package_filename=installed_package.name,
                package_sha256="a" * 64,
                package_id=plugin_id,
                profile_dir=str(tmp_path / "profiles" / plugin_id),
            )

            plan_response = await client.post(
                "/plugin-cli/install-plan",
                json={"package": str(replacement_package)},
            )
            assert plan_response.status_code == 200, plan_response.text
            plan = plan_response.json()
            assert plan["action"] == expected_operation

            replacement_response = await client.post(
                "/plugin-cli/install",
                json={
                    "package": str(replacement_package),
                    "confirm_upgrade": True,
                    "confirmation_token": plan["confirmation_token"],
                },
            )
    finally:
        set_global_manager(None)

    assert replacement_response.status_code == 200, replacement_response.text
    assert replacement_response.json()["operation"] == expected_operation
    installed_manifest = (user_root / plugin_id / "plugin.toml").read_text(encoding="utf-8")
    assert f'version = "{package_version}"' in installed_manifest


@pytest.mark.asyncio
async def test_plugin_cli_install_returns_structured_rollback_details(
    plugin_cli_test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_install(**_kwargs: object) -> dict[str, object]:
        raise ServerDomainError(
            code="PLUGIN_UPGRADE_ROLLED_BACK",
            message="Plugin replacement failed and rollback completed",
            status_code=500,
            details={"stage": "install", "rollback_status": "completed"},
        )

    monkeypatch.setattr(plugin_cli_routes.service, "install", fail_install)
    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/plugin-cli/install",
            json={"package": "/packages/demo.neko-plugin"},
        )

    assert response.status_code == 500
    assert response.headers["x-error-code"] == "PLUGIN_UPGRADE_ROLLED_BACK"
    assert response.json() == {
        "detail": {
            "code": "PLUGIN_UPGRADE_ROLLED_BACK",
            "message": "Plugin replacement failed and rollback completed",
            "details": {"stage": "install", "rollback_status": "completed"},
        }
    }


@pytest.mark.asyncio
async def test_plugin_cli_install_records_uploaded_package_as_imported(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "route_import_demo"
    source = _make_plugin_dir(tmp_path / "source", plugin_id=plugin_id)
    package_source_root = tmp_path / "package-source"
    package_source_root.mkdir()
    package_path = package_source_root / f"{plugin_id}.neko-plugin"
    pack_plugin(source, package_path)
    package_bytes = package_path.read_bytes()
    packages_root = tmp_path / "packages"
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user-plugins"
    profiles_root = tmp_path / "profiles"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    set_global_manager(manager)
    try:
        transport = ASGITransport(app=plugin_cli_test_app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            upload_response = await client.post(
                "/plugin-cli/upload",
                files={"file": (package_path.name, package_bytes, "application/octet-stream")},
            )
            assert upload_response.status_code == 200, upload_response.text
            response = await client.post(
                "/plugin-cli/install",
                json={
                    "package": upload_response.json()["path"],
                    "install_source": "imported",
                },
            )
    finally:
        set_global_manager(None)

    assert response.status_code == 200, response.text
    installed_dir = user_root / plugin_id
    source_view = manager.to_api_view(plugin_id, directory_path=installed_dir)
    assert source_view["source"] == "imported"
    assert source_view["source_detail"] == {
        "package_filename": package_path.name,
        "package_sha256": hashlib.sha256(package_bytes).hexdigest(),
    }


@pytest.mark.asyncio
async def test_plugin_cli_install_remains_successful_when_import_hashing_fails(
    plugin_cli_test_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "route_import_hash_failure"
    source = _make_plugin_dir(tmp_path / "source", plugin_id=plugin_id)
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    package_path = packages_root / f"{plugin_id}.neko-plugin"
    pack_plugin(source, package_path)
    user_root = tmp_path / "user-plugins"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=tmp_path / "profiles",
    )
    def _hash_failure(_path: Path) -> str:
        raise OSError("package archive disappeared")

    monkeypatch.setattr(plugin_cli_routes.service, "_sha256_file", _hash_failure)
    transport = ASGITransport(app=plugin_cli_test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/plugin-cli/install",
            json={
                "package": str(package_path),
                "install_source": "imported",
            },
        )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["operation"] == "install"
    assert result["installed_plugin_count"] == 1
    assert result["install_source_warning"] == (
        "install_source_prepare_failed: package archive disappeared"
    )
    assert (user_root / plugin_id / "plugin.toml").is_file()


@pytest.mark.asyncio
async def test_plugin_cli_upload_and_install_failure_cleans_staging_and_saved_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    package_source_root = tmp_path / "package_source"
    user_root = tmp_path / "user_plugins"
    profiles_root = tmp_path / "profiles"
    packages_root = tmp_path / "packages"
    plugin_dir = _make_plugin_dir(source_root, plugin_id="simple_plugin")
    package_source_root.mkdir(parents=True, exist_ok=True)
    package_path = package_source_root / "simple_plugin.neko-plugin"
    pack_plugin(plugin_dir, package_path)
    existing_target = user_root / "simple_plugin"
    existing_target.mkdir(parents=True, exist_ok=True)
    (existing_target / "plugin.toml").write_text(
        '[plugin]\nid = "simple_plugin"\n',
        encoding="utf-8",
    )
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )

    with pytest.raises(ServerDomainError):
        await PluginCliService().upload_and_install(
            filename="simple_plugin.neko-plugin",
            package_path=str(package_path),
            on_conflict="fail",
        )

    assert (existing_target / "plugin.toml").is_file()
    assert not list(user_root.glob(".neko_staging_*"))
    assert not list(profiles_root.glob(".neko_staging_*"))
    assert not list(packages_root.glob("*.neko-plugin"))


@pytest.mark.asyncio
async def test_fresh_install_reuses_verified_retained_profile_without_changing_its_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "legacy_profile_demo"
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    package_path = packages_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    user_root = tmp_path / "user-plugins"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_id
    profile_dir.mkdir(parents=True)
    (profile_dir / "default.toml").write_bytes(b"legacy-profile-bytes\r\n")
    (profile_dir / "custom.toml").write_bytes(b"custom-profile-bytes\n")
    before = {path.name: path.read_bytes() for path in profile_dir.iterdir()}
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )
    previous_target = _make_plugin_dir(user_root, plugin_id=plugin_id)
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        scanner=PluginDirectoryScanner(tmp_path / "builtin", user_root),
    )
    manager.record_import(
        directory_path=previous_target,
        package_filename="legacy.neko-plugin",
        package_sha256="a" * 64,
        package_id=plugin_id,
        profile_dir=str(profile_dir),
    )
    manager.mark_removed(directory_path=previous_target)
    shutil.rmtree(previous_target)
    set_global_manager(manager)
    try:
        result = await PluginCliService().install(package=str(package_path))
    finally:
        set_global_manager(None)

    assert result["installed_plugin_count"] == 1
    assert result["profile_reused"] is True
    assert (user_root / plugin_id / "plugin.toml").is_file()
    assert {path.name: path.read_bytes() for path in profile_dir.iterdir()} == before


@pytest.mark.asyncio
async def test_fresh_install_rejects_profile_owned_by_another_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming_id = "profile_collision"
    package_path = tmp_path / "packages" / f"{incoming_id}.neko-plugin"
    package_path.parent.mkdir()
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=incoming_id), package_path)
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user-plugins"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / incoming_id
    profile_dir.mkdir(parents=True)
    sentinel = profile_dir / "default.toml"
    sentinel.write_bytes(b"belongs-to-another-plugin\n")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=package_path.parent,
        profiles_root=profiles_root,
    )

    previous_target = _make_plugin_dir(user_root, plugin_id="another_plugin")
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    manager.record_import(
        directory_path=previous_target,
        package_filename="another.neko-plugin",
        package_sha256="b" * 64,
        package_id=incoming_id,
        profile_dir=str(profile_dir),
    )
    manager.mark_removed(directory_path=previous_target)
    shutil.rmtree(previous_target)
    set_global_manager(manager)
    try:
        with pytest.raises(
            ServerDomainError,
            match="profile ownership does not match",
        ) as caught:
            await PluginCliService().install(package=str(package_path))
    finally:
        set_global_manager(None)

    assert caught.value.code == "PLUGIN_PACKAGE_PROFILE_OWNERSHIP_CONFLICT"
    assert not (user_root / incoming_id).exists()
    assert sentinel.read_bytes() == b"belongs-to-another-plugin\n"


@pytest.mark.asyncio
async def test_fresh_install_rejects_legacy_profile_with_unknown_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "legacy_unknown_profile"
    package_path = tmp_path / "packages" / f"{plugin_id}.neko-plugin"
    package_path.parent.mkdir()
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user-plugins"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_id
    profile_dir.mkdir(parents=True)
    sentinel = profile_dir / "default.toml"
    sentinel.write_bytes(b"legacy-owner-unknown\n")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=package_path.parent,
        profiles_root=profiles_root,
    )

    lock_path = tmp_path / "plugins.lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "updated_at": "2026-01-01T00:00:00.000000Z",
                "entries": [
                    {
                        "root_id": "user",
                        "directory_name": plugin_id,
                        "plugin_id": plugin_id,
                        "channel": "imported",
                        "reason": "user_requested",
                        "installed_at": "2026-01-01T00:00:00.000000Z",
                        "updated_at": "2026-01-01T00:00:00.000000Z",
                        "last_seen_at": "2026-01-01T00:00:00.000000Z",
                        "removed": True,
                        "source_detail": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = InstallSourceManager(
        lock_path=lock_path,
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    manager.load()
    set_global_manager(manager)
    try:
        with pytest.raises(ServerDomainError) as caught:
            await PluginCliService().install(package=str(package_path))
    finally:
        set_global_manager(None)

    assert caught.value.code == "PLUGIN_PACKAGE_PROFILE_OWNERSHIP_CONFLICT"
    assert sentinel.read_bytes() == b"legacy-owner-unknown\n"
    assert not (user_root / plugin_id).exists()


def test_existing_profile_without_source_manager_reports_not_ready(
    tmp_path: Path,
) -> None:
    profile_dir = tmp_path / "profiles" / "demo"
    profile_dir.mkdir(parents=True)
    sentinel = profile_dir / "default.toml"
    sentinel.write_bytes(b"unchanged\n")
    previous_manager = plugin_cli_service.get_install_source_manager()
    set_global_manager(None)

    try:
        with pytest.raises(ServerDomainError) as caught:
            plugin_cli_service._validate_existing_profile_ownership(
                profile_dir=profile_dir,
                profiles_root=profile_dir.parent,
                package_id="demo",
                plugin_ids={"demo"},
            )
    finally:
        set_global_manager(previous_manager)

    assert caught.value.code == "INSTALL_SOURCE_NOT_READY"
    assert caught.value.status_code == 503
    assert sentinel.read_bytes() == b"unchanged\n"


@pytest.mark.asyncio
async def test_reused_profile_survives_failure_after_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "profile_late_failure"
    package_path = tmp_path / "packages" / f"{plugin_id}.neko-plugin"
    package_path.parent.mkdir()
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user-plugins"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_id
    profile_dir.mkdir(parents=True)
    sentinel = profile_dir / "default.toml"
    sentinel.write_bytes(b"preserve-after-late-failure\n")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=package_path.parent,
        profiles_root=profiles_root,
    )

    previous_target = _make_plugin_dir(user_root, plugin_id=plugin_id)
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    manager.record_import(
        directory_path=previous_target,
        package_filename="previous.neko-plugin",
        package_sha256="d" * 64,
        package_id=plugin_id,
        profile_dir=str(profile_dir),
    )
    manager.mark_removed(directory_path=previous_target)
    shutil.rmtree(previous_target)
    set_global_manager(manager)
    monkeypatch.setattr(
        plugin_cli_service,
        "InstallResult",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("injected late failure")),
    )
    try:
        with pytest.raises(ServerDomainError, match="injected late failure"):
            await PluginCliService().install(package=str(package_path))
    finally:
        set_global_manager(None)

    assert sentinel.read_bytes() == b"preserve-after-late-failure\n"
    assert not (user_root / plugin_id).exists()


@pytest.mark.asyncio
async def test_fresh_install_rejects_linked_legacy_profile_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "linked_legacy_profile"
    packages_root = tmp_path / "packages"
    packages_root.mkdir()
    package_path = packages_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    user_root = tmp_path / "user-plugins"
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir()
    outside = tmp_path / "outside-profile"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside-must-survive")
    linked_profile = profiles_root / plugin_id
    if os.name == "nt":
        completed = await asyncio.to_thread(
            subprocess.run,
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(linked_profile), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.skip("Windows junctions are unavailable in this environment")
    else:
        try:
            linked_profile.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"directory symlinks are unavailable: {exc}")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )

    with pytest.raises(ServerDomainError, match="link or reparse point"):
        await PluginCliService().install(package=str(package_path))

    assert not (user_root / plugin_id).exists()
    assert sentinel.read_bytes() == b"outside-must-survive"


@pytest.mark.asyncio
async def test_market_record_failure_removes_new_code_but_preserves_reused_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "legacy_market_profile"
    package_source_root = tmp_path / "package-source"
    package_source_root.mkdir()
    package_path = package_source_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    packages_root = tmp_path / "packages"
    user_root = tmp_path / "user-plugins"
    profiles_root = tmp_path / "profiles"
    profile_dir = profiles_root / plugin_id
    profile_dir.mkdir(parents=True)
    preserved = profile_dir / "default.toml"
    preserved.write_bytes(b"do-not-delete\n")
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )
    previous_target = _make_plugin_dir(user_root, plugin_id=plugin_id)
    owner_manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=tmp_path / "builtin",
        user_root=user_root,
        scanner=PluginDirectoryScanner(tmp_path / "builtin", user_root),
    )
    owner_manager.record_import(
        directory_path=previous_target,
        package_filename="legacy.neko-plugin",
        package_sha256="c" * 64,
        package_id=plugin_id,
        profile_dir=str(profile_dir),
    )
    owner_manager.mark_removed(directory_path=previous_target)
    shutil.rmtree(previous_target)

    class _FailingManager:
        def record_market_install(self, **_kwargs: object) -> None:
            raise InstallSourceError("lock_write_failed", "injected source failure")

    service = PluginCliService()
    failing_manager = _FailingManager()
    failing_manager.builtin_root = tmp_path / "builtin"
    failing_manager.user_root = user_root
    monkeypatch.setattr(service, "_require_install_source_manager", lambda: failing_manager)

    set_global_manager(owner_manager)
    try:
        with pytest.raises(InstallSourceError, match="lock_write_failed"):
            await service.upload_and_install(
                filename=package_path.name,
                package_path=str(package_path),
                install_source_override={
                    "channel": "market",
                    "mode": "install",
                    "market_detail": {
                        "plugin_market_id": plugin_id,
                        "version": "0.0.1",
                        "package_url": "https://example.invalid/demo.neko-plugin",
                        "expected_plugin_toml_id": plugin_id,
                    },
                },
            )
    finally:
        set_global_manager(None)

    assert not (user_root / plugin_id).exists()
    assert preserved.read_bytes() == b"do-not-delete\n"
    assert not list(packages_root.glob("*.neko-plugin"))


@pytest.mark.asyncio
async def test_market_fresh_install_refreshes_after_source_row_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "market_refresh_demo"
    package_source_root = tmp_path / "package-source"
    package_source_root.mkdir()
    package_path = package_source_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user-plugins"
    packages_root = tmp_path / "packages"
    profiles_root = tmp_path / "profiles"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    refresh_calls: list[tuple[str, bool]] = []

    async def refresh_plugin(requested_id: str, *, force: bool = False) -> dict[str, object]:
        installed_dir = user_root / plugin_id
        source_view = manager.to_api_view(plugin_id, directory_path=installed_dir)
        assert source_view["source"] == "market"
        assert installed_dir.joinpath("plugin.toml").is_file()
        refresh_calls.append((requested_id, force))
        return {"success": True, "plugin": {"id": requested_id}}

    monkeypatch.setattr(
        plugin_cli_service,
        "plugin_registry_service",
        SimpleNamespace(refresh_plugin=refresh_plugin),
        raising=False,
    )
    set_global_manager(manager)
    try:
        result = await PluginCliService().upload_and_install(
            filename=package_path.name,
            package_path=str(package_path),
            install_source_override=_market_install_override(plugin_id),
        )
    finally:
        set_global_manager(None)

    assert refresh_calls == [(plugin_id, True)]
    assert result.get("install_source_warning") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("install_mode", ["upgrade", "reinstall"])
async def test_market_upgrade_and_reinstall_do_not_use_fresh_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    install_mode: str,
) -> None:
    plugin_id = f"market_no_fresh_refresh_{install_mode}"
    package_path = tmp_path / f"{plugin_id}.neko-plugin"
    package_path.write_bytes(b"package")
    target_dir = tmp_path / "user-plugins" / plugin_id
    target_dir.mkdir(parents=True)
    (target_dir / "plugin.toml").write_text(
        f'[plugin]\nid = "{plugin_id}"\n',
        encoding="utf-8",
    )
    service = PluginCliService()

    monkeypatch.setattr(
        service,
        "_save_package_file_sync",
        lambda **_kwargs: {"path": str(package_path), "name": package_path.name},
    )
    monkeypatch.setattr(service, "_sha256_file", lambda _path: "a" * 64)

    async def plan_install(**_kwargs: object) -> dict[str, object]:
        return {"action": "upgrade"}

    async def install(**_kwargs: object) -> dict[str, object]:
        return {
            "unpacked_plugins": [
                {
                    "target_dir": str(target_dir),
                    "target_plugin_id": plugin_id,
                }
            ],
            "package_id": plugin_id,
            "profile_dir": "",
        }

    monkeypatch.setattr(service, "plan_install", plan_install)
    monkeypatch.setattr(service, "install", install)

    class _Manager:
        builtin_root = tmp_path / "builtin"
        user_root = target_dir.parent

        def record_market_upgrade(self, **_kwargs: object):
            return (
                SimpleNamespace(
                    channel="market",
                    directory_name=plugin_id,
                    plugin_id=plugin_id,
                    source_detail=SimpleNamespace(
                        version="1.0.0",
                        package_sha256="a" * 64,
                        payload_hash=None,
                        published_at="2026-09-02T00:00:00Z",
                        previous_version="0.9.0",
                    ),
                ),
                [],
            )

    monkeypatch.setattr(service, "_require_install_source_manager", lambda: _Manager())
    refresh_calls: list[str] = []

    async def unexpected_refresh(requested_id: str) -> None:
        refresh_calls.append(requested_id)

    monkeypatch.setattr(
        plugin_cli_service,
        "_refresh_committed_market_install",
        unexpected_refresh,
    )

    result = await service.upload_and_install(
        filename=package_path.name,
        package_path=str(package_path),
        install_source_override={
            **_market_install_override(plugin_id),
            "mode": install_mode,
        },
    )

    assert result["install"]["channel"] == "market"
    assert refresh_calls == []


@pytest.mark.asyncio
async def test_market_fresh_refresh_failure_keeps_committed_install_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "market_refresh_warning"
    package_source_root = tmp_path / "package-source"
    package_source_root.mkdir()
    package_path = package_source_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user-plugins"
    packages_root = tmp_path / "packages"
    profiles_root = tmp_path / "profiles"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=packages_root,
        profiles_root=profiles_root,
    )
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )

    async def fail_refresh(_plugin_id: str, *, force: bool = False) -> dict[str, object]:
        assert force is True
        raise RuntimeError("injected registry refresh failure")

    monkeypatch.setattr(
        plugin_cli_service,
        "plugin_registry_service",
        SimpleNamespace(refresh_plugin=fail_refresh),
        raising=False,
    )
    set_global_manager(manager)
    try:
        result = await PluginCliService().upload_and_install(
            filename=package_path.name,
            package_path=str(package_path),
            install_source_override=_market_install_override(plugin_id),
        )
    finally:
        set_global_manager(None)

    installed_dir = user_root / plugin_id
    assert installed_dir.joinpath("plugin.toml").is_file()
    assert manager.to_api_view(plugin_id, directory_path=installed_dir)["source"] == "market"
    assert "registry refresh" in str(result["install_source_warning"]).lower()
    assert "injected registry refresh failure" in str(result["install_source_warning"])


@pytest.mark.asyncio
async def test_market_fresh_cancellation_waits_for_post_commit_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "market_refresh_cancel"
    package_source_root = tmp_path / "package-source"
    package_source_root.mkdir()
    package_path = package_source_root / f"{plugin_id}.neko-plugin"
    pack_plugin(_make_plugin_dir(tmp_path / "source", plugin_id=plugin_id), package_path)
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user-plugins"
    _patch_plugin_cli_settings(
        monkeypatch,
        builtin_root=builtin_root,
        user_root=user_root,
        packages_root=tmp_path / "packages",
        profiles_root=tmp_path / "profiles",
    )
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh_plugin(_plugin_id: str, *, force: bool = False) -> dict[str, object]:
        assert force is True
        assert manager.to_api_view(
            plugin_id,
            directory_path=user_root / plugin_id,
        )["source"] == "market"
        refresh_started.set()
        await release_refresh.wait()
        return {"success": True}

    monkeypatch.setattr(
        plugin_cli_service,
        "plugin_registry_service",
        SimpleNamespace(refresh_plugin=refresh_plugin),
        raising=False,
    )
    set_global_manager(manager)
    try:
        task = asyncio.create_task(
            PluginCliService().upload_and_install(
                filename=package_path.name,
                package_path=str(package_path),
                install_source_override=_market_install_override(plugin_id),
            )
        )
        await asyncio.wait_for(refresh_started.wait(), timeout=5.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_refresh.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        set_global_manager(None)

    assert (user_root / plugin_id / "plugin.toml").is_file()
