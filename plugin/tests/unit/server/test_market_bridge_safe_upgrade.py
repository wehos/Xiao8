from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from plugin.core.plugin_layout import resolve_plugin_layout
from plugin.server.application.install_source import InstallSourceManager
from plugin.server.application.install_source.scanner import PluginDirectoryScanner
from plugin.server.application.plugins.installation_transactions import (
    replace as replacement_transaction,
)
from plugin.server.routes import market_bridge
from plugin.server.application.plugins import operation_lock
from plugin.server.application.plugins.operation_lock import serialized_plugin_operation


pytestmark = pytest.mark.plugin_unit
DEMO_MANIFEST_V2 = '[plugin]\nid = "demo"\nversion = "2.0.0"\n'


def _payload(plugin_id: str = "demo") -> SimpleNamespace:
    return SimpleNamespace(
        plugin_id=plugin_id,
        version="2.0.0",
        expected_plugin_toml_id=plugin_id,
        package_url="https://example.invalid/demo.neko-plugin",
        package_sha256="a" * 64,
        payload_hash="",
        channel="stable",
        published_at="",
    )


def _entry(
    plugin_id: str = "demo",
    package_id: str = "",
    *,
    profile_dir: str = "",
    updated_at: str = "",
    version: str = "",
    channel: str = "market",
) -> SimpleNamespace:
    return SimpleNamespace(
        root_id="user",
        channel=channel,
        removed=False,
        plugin_id=plugin_id,
        directory_name=plugin_id,
        source_detail=SimpleNamespace(version=version, package_sha256="") if version else None,
        package_id=package_id,
        profile_dir=profile_dir,
        updated_at=updated_at,
    )


def test_market_install_request_normalizes_legacy_rename_conflict_policy() -> None:
    request = market_bridge.MarketInstallRequest(
        plugin_id="demo",
        version="1.0.0",
        package_url="https://example.com/demo.neko-plugin",
        package_sha256="a" * 64,
        on_conflict="rename",
    )

    assert request.on_conflict == "fail"


def test_market_override_records_canonical_package_url() -> None:
    canonical_url = "https://github.com/example/demo/releases/download/v1.0.0/demo.neko-plugin"
    request = market_bridge.MarketInstallRequest(
        plugin_id="demo",
        version="1.0.0",
        package_url=f"https://cdn.gh-proxy.org/{canonical_url}",
        canonical_package_url=canonical_url,
        package_sha256="a" * 64,
    )

    override = market_bridge._build_market_override(request, mode="install")

    assert override["market_detail"]["package_url"] == canonical_url


def test_market_override_threads_only_server_verified_manual_takeover_evidence() -> None:
    request = market_bridge.MarketInstallRequest(
        plugin_id="demo",
        expected_plugin_toml_id="demo",
        version="2.0.0",
        package_url="https://example.invalid/demo.neko-plugin",
        package_sha256="a" * 64,
        mode="upgrade",
        verified_manual_snapshot_sha256="b" * 64,
    )

    override = market_bridge._build_market_override(request, mode="upgrade")

    assert override["manual_takeover_snapshot_sha256"] == "b" * 64


@pytest.mark.asyncio
async def test_market_builtin_override_requires_current_preflight_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    manifest = builtin_root / "demo" / "plugin.toml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '[plugin]\nid = "demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    user_root.mkdir()

    async def authoritative_release(_payload: object) -> dict[str, object]:
        return {
            "plugin_market_id": "market-demo",
            "version": "2.0.0",
            "channel": "stable",
            "package_url": "https://example.invalid/demo.neko-plugin",
            "package_sha256": "a" * 64,
            "payload_hash": None,
            "published_at": None,
        }

    monkeypatch.setattr(
        market_bridge,
        "_fetch_authoritative_market_override_release",
        authoritative_release,
    )
    monkeypatch.setattr(
        market_bridge.PluginCliPathPolicy,
        "from_settings",
        classmethod(
            lambda cls: SimpleNamespace(
                builtin_plugins_root=builtin_root,
                user_plugins_root=user_root,
            )
        ),
    )
    payload = market_bridge.MarketInstallRequest(
        plugin_id="market-demo",
        expected_plugin_toml_id="demo",
        version="2.0.0",
        package_url="https://example.invalid/demo.neko-plugin",
        package_sha256="a" * 64,
        mode="override_builtin",
    )
    bridge_token = market_bridge.get_bridge_token()

    with pytest.raises(HTTPException) as missing_info:
        await market_bridge.market_install(payload, token=bridge_token)
    assert missing_info.value.status_code == 409
    assert missing_info.value.detail["code"] == "override_confirmation_required"

    confirmation = await market_bridge.market_override_confirmation(
        payload,
        token=bridge_token,
    )
    assert confirmation.current_version == "1.0.0"
    assert confirmation.target_version == "2.0.0"
    assert len(confirmation.confirmation_token) == 64

    manifest.write_text(
        '[plugin]\nid = "demo"\nversion = "1.0.1"\n',
        encoding="utf-8",
    )
    stale_payload = payload.model_copy(
        update={"confirmation_token": confirmation.confirmation_token},
    )
    with pytest.raises(HTTPException) as stale_info:
        await market_bridge.market_install(stale_payload, token=bridge_token)
    assert stale_info.value.status_code == 409
    assert stale_info.value.detail["code"] == "override_confirmation_changed"

    fresh_confirmation = await market_bridge.market_override_confirmation(
        payload,
        token=bridge_token,
    )

    dispatched_payloads: list[object] = []

    async def finish_task(_task_id: str, dispatched_payload: object) -> None:
        dispatched_payloads.append(dispatched_payload)
        return None

    monkeypatch.setattr(market_bridge, "_execute_install", finish_task)
    accepted = await market_bridge.market_install(
        payload.model_copy(
            update={"confirmation_token": fresh_confirmation.confirmation_token},
        ),
        token=bridge_token,
    )
    await market_bridge._task_workers[accepted.task_id]
    market_bridge._task_workers.pop(accepted.task_id, None)
    market_bridge._tasks.pop(accepted.task_id, None)

    assert accepted.status == "pending"
    assert len(dispatched_payloads) == 1
    assert (
        getattr(dispatched_payloads[0], "verified_builtin_manifest_sha256", None)
        == fresh_confirmation.builtin_manifest_sha256
    )


@pytest.mark.asyncio
async def test_market_manual_takeover_requires_current_bound_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_root = tmp_path / "user"
    plugin_dir = user_root / "demo"
    plugin_dir.mkdir(parents=True)
    manifest = plugin_dir / "plugin.toml"
    manifest.write_text(
        '[plugin]\nid = "demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    code = plugin_dir / "main.py"
    code.write_text("VALUE = 1\n", encoding="utf-8")
    entry = _entry(
        "demo",
        updated_at="2026-08-29T00:00:00Z",
        channel="manual",
    )
    manager = SimpleNamespace(
        is_degraded=False,
        find_active_user_entry=lambda _plugin_id: entry,
    )

    async def authoritative_release(_payload: object) -> dict[str, object]:
        return {
            "plugin_market_id": "market-demo",
            "version": "2.0.0",
            "channel": "stable",
            "package_url": "https://example.invalid/demo.neko-plugin",
            "package_sha256": "a" * 64,
            "payload_hash": None,
            "published_at": None,
        }

    monkeypatch.setattr(market_bridge, "get_install_source_manager", lambda: manager)
    monkeypatch.setattr(
        market_bridge,
        "_fetch_authoritative_market_override_release",
        authoritative_release,
    )
    monkeypatch.setattr(
        market_bridge.PluginCliPathPolicy,
        "from_settings",
        classmethod(
            lambda cls: SimpleNamespace(
                builtin_plugins_root=tmp_path / "builtin",
                user_plugins_root=user_root,
            )
        ),
    )
    payload = market_bridge.MarketInstallRequest(
        plugin_id="market-demo",
        expected_plugin_toml_id="demo",
        version="2.0.0",
        package_url="https://example.invalid/demo.neko-plugin",
        package_sha256="a" * 64,
        mode="upgrade",
    )
    bridge_token = market_bridge.get_bridge_token()

    with pytest.raises(HTTPException) as missing_info:
        await market_bridge.market_install(payload, token=bridge_token)
    assert missing_info.value.detail["code"] == "manual_takeover_confirmation_required"

    manifest.write_text(
        '[plugin]\nid = "other-plugin"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(HTTPException) as identity_info:
        await market_bridge.market_manual_takeover_confirmation(
            payload,
            token=bridge_token,
        )
    assert identity_info.value.detail["code"] == "manual_takeover_source_changed"
    manifest.write_text(
        '[plugin]\nid = "demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    confirmation = await market_bridge.market_manual_takeover_confirmation(
        payload,
        token=bridge_token,
    )
    assert confirmation.current_version == "1.0.0"
    assert len(confirmation.confirmation_token) == 64

    code.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(HTTPException) as stale_info:
        await market_bridge.market_install(
            payload.model_copy(
                update={"confirmation_token": confirmation.confirmation_token},
            ),
            token=bridge_token,
        )
    assert stale_info.value.detail["code"] == "manual_takeover_plan_changed"

    fresh = await market_bridge.market_manual_takeover_confirmation(
        payload,
        token=bridge_token,
    )
    dispatched_payloads: list[object] = []

    async def finish_task(_task_id: str, dispatched_payload: object) -> None:
        dispatched_payloads.append(dispatched_payload)

    monkeypatch.setattr(market_bridge, "_execute_install", finish_task)
    accepted = await market_bridge.market_install(
        payload.model_copy(update={"confirmation_token": fresh.confirmation_token}),
        token=bridge_token,
    )
    await market_bridge._task_workers[accepted.task_id]
    market_bridge._task_workers.pop(accepted.task_id, None)
    market_bridge._tasks.pop(accepted.task_id, None)

    assert len(dispatched_payloads) == 1
    assert (
        getattr(dispatched_payloads[0], "verified_manual_snapshot_sha256", None)
        == fresh.manual_snapshot_sha256
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["upgrade", "reinstall"])
async def test_legacy_market_upgrade_matches_market_record_id_without_expected_plugin_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user"
    builtin_root.mkdir()
    user_root.mkdir()
    manager = InstallSourceManager(
        lock_path=tmp_path / "plugins.lock.json",
        builtin_root=builtin_root,
        user_root=user_root,
        scanner=PluginDirectoryScanner(builtin_root, user_root),
    )
    manager.record_market_install(
        root_id="user",
        directory_name="runtime-demo",
        plugin_id="runtime-demo",
        market_detail={
            "plugin_market_id": "market-record-42",
            "version": "1.0.0",
            "package_url": "https://example.invalid/demo-v1.neko-plugin",
            "channel": "stable",
            "package_sha256": "a" * 64,
            "payload_hash": None,
            "published_at": "2026-08-29T00:00:00.000000Z",
        },
    )
    monkeypatch.setattr(market_bridge, "get_install_source_manager", lambda: manager)
    dispatched_payloads: list[market_bridge.MarketInstallRequest] = []

    async def finish_task(
        _task_id: str,
        dispatched_payload: market_bridge.MarketInstallRequest,
    ) -> None:
        dispatched_payloads.append(dispatched_payload)

    monkeypatch.setattr(market_bridge, "_execute_install", finish_task)
    payload = market_bridge.MarketInstallRequest(
        plugin_id="market-record-42",
        version="2.0.0",
        package_url="https://example.invalid/demo-v2.neko-plugin",
        package_sha256="b" * 64,
        mode=mode,
    )

    accepted = await market_bridge.market_install(
        payload,
        token=market_bridge.get_bridge_token(),
    )
    await market_bridge._task_workers[accepted.task_id]
    market_bridge._task_workers.pop(accepted.task_id, None)
    market_bridge._tasks.pop(accepted.task_id, None)

    assert len(dispatched_payloads) == 1
    assert dispatched_payloads[0].expected_plugin_toml_id is None


@pytest.mark.asyncio
async def test_market_install_discards_caller_verified_fields_before_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MutableManager:
        def __init__(self) -> None:
            self.current = _entry("demo", channel="market")
            self.is_degraded = False

        def find_active_user_entry(self, _plugin_ref: str) -> SimpleNamespace:
            return self.current

    manager = MutableManager()
    monkeypatch.setattr(market_bridge, "get_install_source_manager", lambda: manager)
    dispatched_payloads: list[market_bridge.MarketInstallRequest] = []

    async def finish_task(
        _task_id: str,
        dispatched_payload: market_bridge.MarketInstallRequest,
    ) -> None:
        dispatched_payloads.append(dispatched_payload)

    monkeypatch.setattr(market_bridge, "_execute_install", finish_task)
    payload = market_bridge.MarketInstallRequest(
        plugin_id="demo",
        expected_plugin_toml_id="demo",
        version="2.0.0",
        package_url="https://example.invalid/demo.neko-plugin",
        package_sha256="a" * 64,
        mode="upgrade",
        verified_builtin_manifest_sha256="b" * 64,
        verified_manual_snapshot_sha256="c" * 64,
    )
    assert payload.verified_manual_snapshot_sha256 == "c" * 64

    accepted = await market_bridge.market_install(
        payload,
        token=market_bridge.get_bridge_token(),
    )
    await market_bridge._task_workers[accepted.task_id]
    market_bridge._task_workers.pop(accepted.task_id, None)
    market_bridge._tasks.pop(accepted.task_id, None)

    queued_payload = dispatched_payloads[0]
    assert queued_payload.verified_builtin_manifest_sha256 is None
    assert queued_payload.verified_manual_snapshot_sha256 is None

    manager.current = _entry("demo", channel="manual")
    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, queued_payload, {})
    assert exc_info.value.code == "manual_takeover_confirmation_required"


@pytest.mark.asyncio
async def test_market_builtin_override_rejects_caller_hash_not_in_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = market_bridge.MarketInstallRequest(
        plugin_id="42",
        expected_plugin_toml_id="demo",
        version="2.0.0",
        package_url="https://attacker.invalid/demo.neko-plugin",
        canonical_package_url="https://attacker.invalid/demo.neko-plugin",
        package_sha256="a" * 64,
        channel="stable",
        mode="override_builtin",
    )

    class FakeAsyncClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, **_kwargs: object) -> object:
            request = market_bridge.httpx.Request("GET", url)
            return market_bridge.httpx.Response(
                200,
                request=request,
                json=[
                    {
                        "version": "2.0.0",
                        "channel": "stable",
                        "package_url": "https://market.invalid/demo.neko-plugin",
                        "package_sha256": "b" * 64,
                        "payload_hash": None,
                        "created_at": "2026-08-26T00:00:00Z",
                    }
                ],
            )

    monkeypatch.setattr(market_bridge.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(market_bridge, "MARKET_API_URL", "https://market.invalid")

    with pytest.raises(HTTPException) as exc_info:
        await market_bridge._fetch_authoritative_market_override_release(payload)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "market_release_mismatch"


@pytest.mark.asyncio
async def test_market_builtin_override_routes_verified_package_to_source_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_path = tmp_path / "study_companion.neko-plugin"
    package_path.write_bytes(b"verified package")
    payload = market_bridge.MarketInstallRequest(
        plugin_id="study_companion",
        expected_plugin_toml_id="study_companion",
        version="0.1.6",
        package_url="https://example.invalid/study_companion.neko-plugin",
        package_sha256="a" * 64,
        mode="override_builtin",
        verified_builtin_manifest_sha256="f" * 64,
    )
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        market_bridge,
        "_download_package",
        lambda _url, _task: _async_value(package_path),
    )
    monkeypatch.setattr(
        market_bridge,
        "_verify_downloaded_package_with_fallback",
        lambda *_args, **_kwargs: _async_value((package_path, "passed")),
    )
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)

    async def upload_and_install(**kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "upload": {},
            "unpack": {"operation": "override_builtin", "restarted": True},
            "install": {"channel": "market"},
        }

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=upload_and_install),
    )

    task: dict[str, Any] = {}
    await market_bridge._do_install(task, payload, {})

    assert calls[0]["install_source_override"]["mode"] == "override_builtin"
    assert calls[0]["install_source_override"]["override_confirmation"] == {
        "builtin_manifest_sha256": "f" * 64,
    }
    assert task["result"]["operation"] == "override_builtin"
    assert task["result"]["restarted"] is True
    assert task["result"]["install"]["operation"] == "override_builtin"


def _configure_paths(
    monkeypatch: pytest.MonkeyPatch,
    *,
    plugins_root: Path,
    profiles_root: Path,
    entry: SimpleNamespace | None = None,
) -> None:
    policy = SimpleNamespace(
        user_plugins_root=plugins_root,
        builtin_plugins_root=plugins_root.parent / "builtin",
        package_profiles_root=profiles_root,
        package_artifacts_root=plugins_root.parent / "packages",
    )
    monkeypatch.setattr(
        market_bridge.PluginCliPathPolicy,
        "from_settings",
        classmethod(lambda cls: policy),
    )
    monkeypatch.setattr(
        market_bridge,
        "get_install_source_manager",
        lambda: SimpleNamespace(
            is_degraded=False,
            find_active_market_entry=lambda plugin_id: entry or _entry(plugin_id),
            find_active_user_entry=lambda plugin_id: entry or _entry(plugin_id),
        ),
    )
    monkeypatch.setattr(
        market_bridge,
        "inspect_package",
        lambda path: SimpleNamespace(package_id="demo"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["install", "upgrade"])
async def test_market_mutation_rejects_degraded_lock_before_download(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    manager = SimpleNamespace(is_degraded=True, degrade_reason="legacy_migration_failed")
    monkeypatch.setattr(market_bridge, "get_install_source_manager", lambda: manager)

    async def unexpected_download(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("download must not start while the lock is degraded")

    monkeypatch.setattr(market_bridge, "_download_package", unexpected_download)
    payload = market_bridge.MarketInstallRequest(
        plugin_id="demo",
        expected_plugin_toml_id="demo",
        version="2.0.0",
        package_url="https://example.invalid/demo.neko-plugin",
        package_sha256="a" * 64,
        mode="install" if operation == "install" else "upgrade",
    )

    with pytest.raises(market_bridge._TaskError) as exc_info:
        if operation == "install":
            await market_bridge._do_install({}, payload, {})
        else:
            await market_bridge._do_upgrade({}, payload, {})

    assert exc_info.value.code == "install_source_read_only"
    assert exc_info.value.http_status == 503


@pytest.mark.asyncio
async def test_market_upgrade_delegates_file_replacement_to_shared_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)

    calls: list[dict[str, Any]] = []

    async def shared_replace(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            install_result={"operation": "upgrade"},
            restarted=False,
            rollback_status="not_needed",
            backup_dir=tmp_path / "backup",
        )

    monkeypatch.setattr(market_bridge, "replace_plugin", shared_replace, raising=False)

    task: dict[str, Any] = {}
    await market_bridge._do_upgrade(task, _payload(), {})

    assert len(calls) == 1
    assert calls[0]["layout"].installed_dir == plugin_dir.resolve()
    assert calls[0]["additional_targets"] == (profiles_root / "demo",)
    assert calls[0]["preserve_targets"] == (profiles_root / "demo",)
    assert task["result"] == {
        "operation": "upgrade",
        "restarted": False,
        "rollback_status": "not_needed",
    }


@pytest.mark.asyncio
async def test_market_builtin_manual_slot_uses_confirmed_takeover_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.server.application.plugin_cli.service import PluginCliService
    import plugin.server.application.plugin_cli.service as service_module

    manual_entry = market_bridge.LockEntry(
        root_id="user",
        directory_name="demo",
        plugin_id="demo",
        channel="manual",
        reason="user_requested",
        installed_at="2026-08-29T00:00:00.000000Z",
        updated_at="2026-08-29T00:00:00.000000Z",
        last_seen_at="2026-08-29T00:00:00.000000Z",
    )
    manager = SimpleNamespace(
        is_degraded=False,
        find_active_market_entry=lambda _plugin_id: None,
        find_active_user_entry=lambda _plugin_id: manual_entry,
    )
    monkeypatch.setattr(service_module, "get_install_source_manager", lambda: manager)

    service = PluginCliService()

    async def override_plan(**_kwargs: object) -> dict[str, object]:
        return {
            "action": "override_builtin",
            "package_id": "market-package",
            "plugin_id": "demo",
            "directory_name": "demo",
            "target_version": "2.0.0",
        }

    install_calls: list[dict[str, object]] = []

    def install_sync(**kwargs: object) -> dict[str, object]:
        install_calls.append(kwargs)
        return {"operation": "install", "plugin_id": "demo"}

    monkeypatch.setattr(service, "plan_install", override_plan)
    monkeypatch.setattr(service, "_install_sync", install_sync)

    with pytest.raises(ValueError, match="active install-source lock"):
        await service._install_market_builtin_replacement(
            package=str(tmp_path / "demo.neko-plugin"),
            profiles_root=str(tmp_path / "profiles"),
            _allow_external_profiles_root=True,
            forced_directory_name="demo",
            market_detail={
                "expected_plugin_toml_id": "demo",
                "version": "2.0.0",
                "package_sha256": "a" * 64,
            },
            actual_sha256="a" * 64,
        )

    result = await service._install_market_builtin_replacement(
        package=str(tmp_path / "demo.neko-plugin"),
        profiles_root=str(tmp_path / "profiles"),
        _allow_external_profiles_root=True,
        forced_directory_name="demo",
        market_detail={
            "expected_plugin_toml_id": "demo",
            "version": "2.0.0",
            "package_sha256": "a" * 64,
        },
        actual_sha256="a" * 64,
        manual_takeover_snapshot_sha256="b" * 64,
    )

    assert result["plugin_id"] == "demo"
    assert len(install_calls) == 1
    assert install_calls[0]["forced_directory_name"] == "demo"


@pytest.mark.asyncio
async def test_market_upgrade_holds_operation_lock_for_entire_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)

    entered = asyncio.Event()
    release = asyncio.Event()
    second_waiting_for_lock = asyncio.Event()
    calls: list[dict[str, Any]] = []
    acquire_attempts = 0
    original_acquire = operation_lock._PROCESS_LOCK.acquire

    async def observed_acquire(deadline: float | None = None) -> None:
        nonlocal acquire_attempts
        acquire_attempts += 1
        if acquire_attempts == 2:
            second_waiting_for_lock.set()
        # 截止期照原样转发。吞掉它的替身会让被测的等锁预算在这条路径上静默失效。
        await original_acquire(deadline)

    monkeypatch.setattr(operation_lock._PROCESS_LOCK, "acquire", observed_acquire)

    async def blocked_replace(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return SimpleNamespace(
            install_result={"operation": "upgrade"},
            restarted=False,
            rollback_status="not_needed",
            backup_dir=tmp_path / "backup",
        )

    monkeypatch.setattr(market_bridge, "replace_plugin", blocked_replace)
    first = asyncio.create_task(market_bridge._do_upgrade({}, _payload(), {}))
    await entered.wait()
    second = asyncio.create_task(market_bridge._do_upgrade({}, _payload(), {}))
    await asyncio.wait_for(second_waiting_for_lock.wait(), timeout=1)
    assert len(calls) == 1

    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_market_upgrade_does_not_hold_operation_lock_while_downloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    download_started = asyncio.Event()
    release_download = asyncio.Event()

    async def slow_download(_url: str, _task: dict[str, Any]) -> Path:
        download_started.set()
        await release_download.wait()
        return package_path

    monkeypatch.setattr(market_bridge, "_download_package", slow_download)
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(
        market_bridge,
        "replace_plugin",
        lambda **_kwargs: _async_value(
            SimpleNamespace(
                install_result={"operation": "upgrade"},
                restarted=False,
                rollback_status="not_needed",
                backup_dir=tmp_path / "backup",
            )
        ),
    )

    observed: list[str] = []

    @serialized_plugin_operation
    async def unrelated_operation() -> None:
        observed.append("ran")

    upgrade_task = asyncio.create_task(market_bridge._do_upgrade({}, _payload(), {}))
    await download_started.wait()
    await unrelated_operation()
    assert observed == ["ran"]

    release_download.set()
    await upgrade_task


@pytest.mark.asyncio
async def test_market_upgrade_preserves_profile_at_recorded_custom_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    custom_profile_dir = tmp_path / "custom_profiles" / "demo"
    plugin_dir.mkdir(parents=True)
    custom_profile_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")
    entry = _entry("demo", "demo", profile_dir=str(custom_profile_dir))

    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
        entry=entry,
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    calls: list[dict[str, Any]] = []
    upload_calls: list[dict[str, Any]] = []

    async def fake_replace(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        await kwargs["install_new"]()
        return SimpleNamespace(
            install_result={"operation": "upgrade"},
            restarted=False,
            rollback_status="not_needed",
            backup_dir=tmp_path / "backup",
        )

    monkeypatch.setattr(market_bridge, "replace_plugin", fake_replace)
    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(
            upload_and_install=lambda **kwargs: (
                upload_calls.append(kwargs) or _async_value({"operation": "upgrade"})
            )
        ),
    )
    await market_bridge._do_upgrade({}, _payload(), {})

    assert calls[0]["additional_targets"] == (custom_profile_dir.resolve(),)
    assert calls[0]["preserve_targets"] == (custom_profile_dir.resolve(),)
    assert upload_calls[0]["profiles_root"] == str(custom_profile_dir.parent)
    assert upload_calls[0]["_allow_external_profiles_root"] is True


@pytest.mark.asyncio
async def test_market_upgrade_rejects_symlinked_recorded_profile_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    symlinked_ancestor = tmp_path / "recorded_profiles"
    profile_dir = symlinked_ancestor / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")
    entry = _entry("demo", "demo", profile_dir=str(profile_dir))

    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
        entry=entry,
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    original_is_symlink = Path.is_symlink

    def _is_symlink(path: Path) -> bool:
        return path == symlinked_ancestor or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", _is_symlink)

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, _payload(), {})

    assert exc_info.value.code == "unsafe_profile_path"


@pytest.mark.asyncio
async def test_market_upgrade_rejects_stale_lock_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_entry = _entry("demo", "demo", updated_at="2026-01-01T00:00:00Z", version="1.0.0")
    updated_entry = _entry("demo", "demo", updated_at="2026-01-02T00:00:00Z", version="2.0.0")

    class ReloadingManager:
        def __init__(self) -> None:
            self.current = first_entry
            self.load_calls = 0

        def load(self) -> None:
            self.load_calls += 1
            self.current = updated_entry

        def find_active_user_entry(self, _plugin_id: str) -> SimpleNamespace:
            return self.current

    manager = ReloadingManager()

    async def install_new() -> dict[str, object]:
        return {}

    plugin_dir = tmp_path / "plugins" / "demo"

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._replace_market_plugin_transaction(
            manager=manager,
            expected_plugin_id="demo",
            original_entry=first_entry,
            original_entry_fingerprint=market_bridge._market_entry_fingerprint(first_entry),
            installed_package_id="demo",
            plugin_dir=plugin_dir,
            layout=resolve_plugin_layout("demo", plugin_dir),
            install_new=install_new,
        )

    assert exc_info.value.code == "plugin_upgrade_plan_changed"
    assert manager.load_calls == 1


@pytest.mark.asyncio
async def test_market_manual_takeover_revalidates_backup_after_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = tmp_path / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        "[plugin]\nid='demo'\nversion='1.0.0'\n",
        encoding="utf-8",
    )
    sentinel = plugin_dir / "manual.py"
    sentinel.write_text("confirmed = true\n", encoding="utf-8")
    entry = _entry("demo", "demo", channel="manual", updated_at="2026-01-01T00:00:00Z")
    manager = SimpleNamespace(
        find_active_user_entry=lambda _plugin_id: entry,
        find_active_market_entry=lambda _plugin_id: None,
    )
    expected_snapshot = market_bridge.manual_takeover_snapshot_sha256(
        entry=entry,
        target_dir=plugin_dir,
    )
    install_called = False

    async def install_new() -> dict[str, object]:
        nonlocal install_called
        install_called = True
        return {}

    async def is_running(_plugin_id: str) -> bool:
        return True

    async def stop(_plugin_id: str) -> None:
        sentinel.write_text("edited_during_stop = true\n", encoding="utf-8")

    async def start(_plugin_id: str) -> None:
        return None

    monkeypatch.setattr(replacement_transaction, "_plugin_is_running", is_running)
    monkeypatch.setattr(replacement_transaction, "_stop_plugin", stop)
    monkeypatch.setattr(replacement_transaction, "_start_plugin", start)

    layout = resolve_plugin_layout(
        "demo",
        plugin_dir,
        storage_root=tmp_path / "runtime_data",
    )
    with pytest.raises(market_bridge.ReplacePluginError) as exc_info:
        await market_bridge._replace_market_plugin_transaction(
            manager=manager,
            expected_plugin_id="demo",
            original_entry=entry,
            original_entry_fingerprint=market_bridge._market_entry_fingerprint(entry),
            installed_package_id="demo",
            plugin_dir=plugin_dir,
            layout=layout,
            install_new=install_new,
            manual_snapshot_sha256=expected_snapshot,
        )

    assert isinstance(exc_info.value.cause, market_bridge.ServerDomainError)
    assert exc_info.value.cause.code == "MANUAL_TAKEOVER_PLAN_CHANGED"
    assert install_called is False
    assert sentinel.read_text(encoding="utf-8") == "edited_during_stop = true\n"


@pytest.mark.asyncio
async def test_market_manual_takeover_rejects_unowned_existing_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    profile_dir = profiles_root / "demo"
    plugin_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text(
        "[plugin]\nid='demo'\nversion='1.0.0'\n",
        encoding="utf-8",
    )
    sentinel = profile_dir / "custom.toml"
    sentinel.write_text("belongs_to_user = true\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")
    entry = _entry("demo", "demo", channel="manual")
    manager = SimpleNamespace(
        is_degraded=False,
        find_active_user_entry=lambda _plugin_id: entry,
        find_active_market_entry=lambda _plugin_id: None,
    )

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(market_bridge, "get_install_source_manager", lambda: manager)
    monkeypatch.setattr(
        market_bridge,
        "_download_package",
        lambda _url, _task: _async_value(package_path),
    )
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(
        market_bridge,
        "inspect_package",
        lambda _path: SimpleNamespace(
            package_id="demo",
            profile_names=["payload/profiles/default.toml"],
        ),
    )
    payload = _payload()
    payload.verified_manual_snapshot_sha256 = (
        market_bridge.manual_takeover_snapshot_sha256(
            entry=entry,
            target_dir=plugin_dir,
        )
    )

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, payload, {})

    assert exc_info.value.code == "manual_takeover_profile_target_exists"
    assert sentinel.read_text(encoding="utf-8") == "belongs_to_user = true\n"
    assert plugin_dir.is_dir()


@pytest.mark.asyncio
async def test_market_upgrade_rolls_back_plugin_profile_with_plugin_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    profile_dir = profiles_root / "demo"
    plugin_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    (profile_dir / "default.toml").write_text("version = 1\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda plugin_id: _async_false(),
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda url, task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda path: None)

    async def install_then_fail(**kwargs: Any) -> dict[str, object]:
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)
        if profile_dir.exists():
            shutil.rmtree(profile_dir)
        plugin_dir.mkdir(parents=True)
        profile_dir.mkdir(parents=True)
        (plugin_dir / "plugin.toml").write_text(DEMO_MANIFEST_V2, encoding="utf-8")
        (profile_dir / "default.toml").write_text("version = 2\n", encoding="utf-8")
        raise RuntimeError("install failed after promotion")

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=install_then_fail),
    )

    with pytest.raises(market_bridge._TaskError, match="install failed after promotion"):
        await market_bridge._do_upgrade({}, _payload(), {})

    assert (plugin_dir / "plugin.toml").read_text(encoding="utf-8") == "version = '1.0.0'\n"
    assert (profile_dir / "default.toml").read_text(encoding="utf-8") == "version = 1\n"


@pytest.mark.asyncio
async def test_market_upgrade_exposes_rollback_while_files_are_being_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda _plugin_id: _async_false(),
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(
            upload_and_install=lambda **_kwargs: _async_raise(RuntimeError("install failed")),
        ),
    )

    rollback_started = asyncio.Event()
    allow_rollback = asyncio.Event()
    remove_directory = replacement_transaction.remove_directory

    async def pause_during_rollback(path: Path) -> None:
        rollback_started.set()
        await allow_rollback.wait()
        await remove_directory(path)

    monkeypatch.setattr(
        replacement_transaction,
        "remove_directory",
        pause_during_rollback,
    )

    task: dict[str, Any] = {}
    operation = asyncio.create_task(market_bridge._do_upgrade(task, _payload(), {}))
    await asyncio.wait_for(rollback_started.wait(), timeout=1)

    assert task["stage"] == "rollback"
    assert task["rollback"]["running"] is True
    assert task["rollback"]["restored"] is False

    allow_rollback.set()
    with pytest.raises(market_bridge._TaskError) as exc_info:
        await operation

    assert exc_info.value.code == "upgrade_rollback_completed"
    assert task["rollback"]["running"] is False
    assert task["rollback"]["restored"] is True


@pytest.mark.asyncio
async def test_market_upgrade_preserves_install_source_error_after_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda _plugin_id: _async_false(),
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(
            upload_and_install=lambda **_kwargs: _async_raise(
                market_bridge.InstallSourceError("lock_write_failed", "lock is read-only")
            ),
        ),
    )

    task: dict[str, Any] = {}
    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade(task, _payload(), {})

    assert exc_info.value.code == "lock_write_failed"
    assert task["rollback"]["running"] is False
    assert task["rollback"]["restored"] is True
    assert (plugin_dir / "plugin.toml").read_text(encoding="utf-8") == "version = '1.0.0'\n"


@pytest.mark.asyncio
async def test_market_upgrade_preserves_existing_profile_files_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    profile_dir = profiles_root / "demo"
    plugin_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    (profile_dir / "default.toml").write_text("user_value = true\n", encoding="utf-8")
    (profile_dir / "custom.toml").write_text("custom = true\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda plugin_id: _async_false(),
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda url, task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda path: None)

    async def install_new(**kwargs: Any) -> dict[str, object]:
        plugin_dir.mkdir(parents=True)
        profile_dir.mkdir(parents=True)
        (plugin_dir / "plugin.toml").write_text(DEMO_MANIFEST_V2, encoding="utf-8")
        (profile_dir / "default.toml").write_text("package_value = true\n", encoding="utf-8")
        (profile_dir / "new.toml").write_text("new = true\n", encoding="utf-8")
        return {"operation": "upgrade"}

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=install_new),
    )

    await market_bridge._do_upgrade({}, _payload(), {})

    assert (plugin_dir / "plugin.toml").read_text(encoding="utf-8") == DEMO_MANIFEST_V2
    assert (profile_dir / "default.toml").read_text(encoding="utf-8") == "user_value = true\n"
    assert (profile_dir / "custom.toml").read_text(encoding="utf-8") == "custom = true\n"
    assert (profile_dir / "new.toml").read_text(encoding="utf-8") == "new = true\n"


@pytest.mark.asyncio
async def test_market_upgrade_uses_package_id_for_profile_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_id = "demo"
    package_id = "demo-package"
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / plugin_id
    profile_dir = profiles_root / package_id
    plugin_dir.mkdir(parents=True)
    profile_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    (profile_dir / "default.toml").write_text("user_value = true\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
    )
    monkeypatch.setattr(
        market_bridge,
        "get_install_source_manager",
        lambda: SimpleNamespace(
            find_active_market_entry=lambda _plugin_id: _entry(plugin_id, package_id)
        ),
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda plugin_id: _async_false(),
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda url, task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda path: None)
    monkeypatch.setattr(
        market_bridge,
        "inspect_package",
        lambda path: SimpleNamespace(package_id=package_id),
        raising=False,
    )

    async def install_new(**kwargs: Any) -> dict[str, object]:
        if profile_dir.exists():
            raise FileExistsError(profile_dir)
        plugin_dir.mkdir(parents=True)
        profile_dir.mkdir(parents=True)
        (plugin_dir / "plugin.toml").write_text(DEMO_MANIFEST_V2, encoding="utf-8")
        (profile_dir / "new.toml").write_text("new = true\n", encoding="utf-8")
        return {"operation": "upgrade"}

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=install_new),
    )

    await market_bridge._do_upgrade({}, _payload(plugin_id), {})

    assert (profile_dir / "default.toml").read_text(encoding="utf-8") == "user_value = true\n"
    assert (profile_dir / "new.toml").read_text(encoding="utf-8") == "new = true\n"


@pytest.mark.asyncio
async def test_market_upgrade_rejects_legacy_rename_despite_stale_incoming_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    old_profile = profiles_root / "old-package"
    stale_profile = profiles_root / "new-package"
    plugin_dir.mkdir(parents=True)
    old_profile.mkdir(parents=True)
    stale_profile.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    (old_profile / "default.toml").write_text("user_value = true\n", encoding="utf-8")
    (stale_profile / "default.toml").write_text("stale = true\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(
        market_bridge,
        "get_install_source_manager",
        lambda: SimpleNamespace(find_active_market_entry=lambda _plugin_id: _entry("demo", "")),
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda _plugin_id: _async_false(),
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(
        market_bridge,
        "inspect_package",
        lambda _path: SimpleNamespace(package_id="new-package"),
    )

    install_called = False

    async def unexpected_install(**_kwargs: Any) -> dict[str, object]:
        nonlocal install_called
        install_called = True
        return {}

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=unexpected_install),
    )

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, _payload(), {})

    assert exc_info.value.code == "package_id_change"
    assert "package id changes are not supported" in str(exc_info.value)
    assert install_called is False
    assert (plugin_dir / "plugin.toml").read_text(encoding="utf-8") == "version = '1.0.0'\n"
    assert (old_profile / "default.toml").read_text(encoding="utf-8") == "user_value = true\n"
    assert (stale_profile / "default.toml").read_text(encoding="utf-8") == "stale = true\n"


@pytest.mark.asyncio
async def test_market_upgrade_blocks_package_id_change_and_preserves_old_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    old_profile = profiles_root / "old-package"
    plugin_dir.mkdir(parents=True)
    old_profile.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    (old_profile / "default.toml").write_text("user_value = true\n", encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(
        market_bridge,
        "get_install_source_manager",
        lambda: SimpleNamespace(
            find_active_market_entry=lambda _plugin_id: _entry("demo", "old-package")
        ),
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda _plugin_id: _async_false(),
    )
    monkeypatch.setattr(market_bridge, "_download_package", lambda _url, _task: _async_value(package_path))
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(
        market_bridge,
        "inspect_package",
        lambda _path: SimpleNamespace(package_id="new-package"),
    )

    install_called = False

    async def unexpected_install(**_kwargs: Any) -> dict[str, object]:
        nonlocal install_called
        install_called = True
        return {}

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=unexpected_install),
    )

    with pytest.raises(market_bridge._TaskError, match="package id changes are not supported"):
        await market_bridge._do_upgrade({}, _payload(), {})

    assert install_called is False
    assert (plugin_dir / "plugin.toml").read_text(encoding="utf-8") == "version = '1.0.0'\n"
    assert (old_profile / "default.toml").read_text(encoding="utf-8") == "user_value = true\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("original_channel", ["market", "manual"])
async def test_market_restart_failure_restores_previous_install_source_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original_channel: str,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    profile_dir = profiles_root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    if original_channel == "market":
        profile_dir.mkdir(parents=True)
        (profile_dir / "default.toml").write_text(
            "user_value = true\n",
            encoding="utf-8",
        )
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")

    old_entry = _entry("demo", "demo", channel=original_channel)

    class FakeManager:
        def __init__(self) -> None:
            self.current = old_entry
            self.restore_calls: list[SimpleNamespace] = []

        def find_active_market_entry(self, _plugin_id: str) -> SimpleNamespace:
            return self.current

        def find_active_user_entry(self, _plugin_id: str) -> SimpleNamespace:
            return self.current

        def restore_entry_for_rollback(self, entry: SimpleNamespace) -> None:
            self.restore_calls.append(entry)
            self.current = entry

    manager = FakeManager()
    _configure_paths(monkeypatch, plugins_root=plugins_root, profiles_root=profiles_root)
    monkeypatch.setattr(market_bridge, "get_install_source_manager", lambda: manager)
    monkeypatch.setattr(
        market_bridge,
        "inspect_package",
        lambda _path: SimpleNamespace(
            package_id="demo",
            profile_names=["payload/profiles/default.toml"],
        ),
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda _plugin_id: _async_true(),
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_stop_plugin",
        lambda _plugin_id: _async_none(),
    )
    monkeypatch.setattr(
        market_bridge,
        "_download_package",
        lambda _url, _task: _async_value(package_path),
    )
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)

    async def install_new(**_kwargs: Any) -> dict[str, object]:
        plugin_dir.mkdir(parents=True)
        profile_dir.mkdir(parents=True)
        (plugin_dir / "plugin.toml").write_text(DEMO_MANIFEST_V2, encoding="utf-8")
        (profile_dir / "generated.toml").write_text(
            "replacement = true\n",
            encoding="utf-8",
        )
        manager.current = _entry("demo", "demo", channel="market")
        manager.current.source_detail = SimpleNamespace(version="2.0.0")
        return {"operation": "upgrade"}

    start_calls = 0

    async def fail_new_start(_plugin_id: str) -> None:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            raise RuntimeError("replacement start failed")
        return None

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=install_new),
    )
    monkeypatch.setattr(replacement_transaction, "_start_plugin", fail_new_start)

    payload = _payload()
    if original_channel == "manual":
        payload.verified_manual_snapshot_sha256 = (
            market_bridge.manual_takeover_snapshot_sha256(
                entry=old_entry,
                target_dir=plugin_dir,
            )
        )

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, payload, {})

    assert exc_info.value.code == "upgrade_rollback_completed"
    assert manager.restore_calls == [old_entry]
    assert manager.current is old_entry
    assert (plugin_dir / "plugin.toml").read_text(encoding="utf-8") == "version = '1.0.0'\n"
    if original_channel == "market":
        assert (profile_dir / "default.toml").read_text(encoding="utf-8") == "user_value = true\n"
        assert not (profile_dir / "generated.toml").exists()
    else:
        assert not profile_dir.exists()


@pytest.mark.asyncio
async def test_market_backup_failure_reports_incomplete_when_old_plugin_cannot_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("version = '1.0.0'\n", encoding="utf-8")
    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda plugin_id: _async_true(),
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_stop_plugin",
        lambda plugin_id: _async_none(),
    )
    monkeypatch.setattr(
        replacement_transaction,
        "_start_plugin",
        lambda plugin_id: _async_raise(RuntimeError("old plugin restart failed")),
    )
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"package")
    monkeypatch.setattr(
        market_bridge,
        "_download_package",
        lambda _url, _task: _async_value(package_path),
    )
    monkeypatch.setattr(market_bridge, "_verify_sha256_file", lambda *args, **kwargs: "passed")
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(market_bridge.os, "rename", lambda source, target: _raise_permission_error())

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, _payload(), {})

    assert exc_info.value.code == "upgrade_rollback_incomplete"


@pytest.mark.asyncio
async def test_market_builtin_override_upgrade_rejects_non_catalog_release_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    builtin_manifest = tmp_path / "builtin" / "demo" / "plugin.toml"
    plugin_dir.mkdir(parents=True)
    builtin_manifest.parent.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text('[plugin]\nid = "demo"\n', encoding="utf-8")
    builtin_manifest.write_text('[plugin]\nid = "demo"\n', encoding="utf-8")
    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
    )

    async def reject_release(_payload: object) -> dict[str, object]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "market_release_mismatch",
                "message": "request does not match catalog",
            },
        )

    async def unexpected_download(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("non-catalog packages must not be downloaded")

    monkeypatch.setattr(
        market_bridge,
        "_fetch_authoritative_market_override_release",
        reject_release,
    )
    monkeypatch.setattr(market_bridge, "_download_package", unexpected_download)

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, _payload("demo"), {})

    assert exc_info.value.code == "market_release_mismatch"
    assert exc_info.value.http_status == 409


@pytest.mark.asyncio
async def test_stopped_builtin_override_upgrade_validates_runtime_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugin.server.application.plugins import lifecycle_service

    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"
    plugin_dir = plugins_root / "demo"
    builtin_manifest = tmp_path / "builtin" / "demo" / "plugin.toml"
    plugin_dir.mkdir(parents=True)
    builtin_manifest.parent.mkdir(parents=True)
    original_manifest = '[plugin]\nid = "demo"\nversion = "1.0.0"\n'
    (plugin_dir / "plugin.toml").write_text(original_manifest, encoding="utf-8")
    builtin_manifest.write_text(original_manifest, encoding="utf-8")
    package_path = tmp_path / "demo.neko-plugin"
    package_path.write_bytes(b"catalog package")
    _configure_paths(
        monkeypatch,
        plugins_root=plugins_root,
        profiles_root=profiles_root,
    )
    authoritative_release = {
        "plugin_market_id": "42",
        "version": "2.0.0",
        "channel": "stable",
        "package_url": "https://market.invalid/demo.neko-plugin",
        "package_sha256": "a" * 64,
        "payload_hash": "catalog-payload",
        "published_at": "2026-08-26T00:00:00Z",
    }
    monkeypatch.setattr(
        market_bridge,
        "_fetch_authoritative_market_override_release",
        lambda _payload: _async_value(authoritative_release),
    )
    monkeypatch.setattr(
        market_bridge,
        "_download_package",
        lambda _url, _task: _async_value(package_path),
    )
    monkeypatch.setattr(
        market_bridge,
        "_verify_downloaded_package_with_fallback",
        lambda *_args, **_kwargs: _async_value((package_path, "passed")),
    )
    monkeypatch.setattr(market_bridge, "_cleanup_download_file", lambda _path: None)
    monkeypatch.setattr(
        replacement_transaction,
        "_plugin_is_running",
        lambda _plugin_id: _async_false(),
    )

    captured_override: dict[str, Any] = {}

    async def install_invalid_runtime(**kwargs: Any) -> dict[str, object]:
        captured_override.update(kwargs["install_source_override"])
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.toml").write_text(
            '[plugin]\nid = "demo"\nentry = "plugin.plugins.demo:Plugin"\n',
            encoding="utf-8",
        )
        return {"operation": "upgrade"}

    validation_calls: list[tuple[str, Path]] = []

    async def reject_invalid_runtime(*, plugin_id: str, config_path: Path) -> None:
        validation_calls.append((plugin_id, config_path))
        raise RuntimeError("entry class is missing")

    monkeypatch.setattr(
        market_bridge,
        "_cli_service",
        SimpleNamespace(upload_and_install=install_invalid_runtime),
    )
    monkeypatch.setattr(
        lifecycle_service.plugin_registry_service,
        "validate_plugin_runtime_source",
        reject_invalid_runtime,
    )

    with pytest.raises(market_bridge._TaskError) as exc_info:
        await market_bridge._do_upgrade({}, _payload("demo"), {})

    assert exc_info.value.code == "upgrade_rollback_completed"
    assert validation_calls == [("demo", plugin_dir / "plugin.toml")]
    assert (plugin_dir / "plugin.toml").read_text(encoding="utf-8") == original_manifest
    assert captured_override["market_detail"] == {
        **authoritative_release,
        "expected_plugin_toml_id": "demo",
    }


async def _async_none() -> None:
    return None


async def _async_true() -> bool:
    return True


async def _async_false() -> bool:
    return False


async def _async_value(value: Any) -> Any:
    return value


async def _async_raise(error: Exception) -> None:
    raise error


def _raise_permission_error() -> None:
    raise PermissionError("backup denied")
