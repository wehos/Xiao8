"""Exercise metadata through registration/IPC/listing, not a hand-built Agent list."""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from plugin._types.events import EventMeta as LegacyEventMeta
from plugin.core import registry
from plugin.core.state import state
from plugin.sdk.plugin import plugin_entry
from plugin.server.application.plugins import metadata_scanner, query_service
from plugin.server.routes.plugins import router


class ContractPlugin:
    @plugin_entry(
        id="probe",
        timeout=5,
        llm_result_fields=["summary"],
        metadata={"agent_auto": False},
    )
    async def probe(self):
        pass

    @plugin_entry(id="consult", timeout=100, llm_result_fields=["summary"])
    async def consult(self):
        pass


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(state, "event_handlers", {})
    monkeypatch.setattr(
        state,
        "_snapshot_cache",
        {key: {"data": None, "timestamp": 0.0} for key in state._snapshot_cache},
    )
    monkeypatch.setattr(registry, "plugin_entry_method_map", {})


def manifest_entries():
    return [
        {
            "id": "probe",
            "timeout": 5,
            "llm_result_fields": ["summary"],
            "metadata": {"agent_auto": False},
        },
        {
            "id": "consult",
            "timeout": 100,
            "llm_result_fields": ["summary"],
            "metadata": {},
        },
    ]


@pytest.mark.parametrize("legacy", [False, True])
def test_wire_payload_preserves_slotted_and_legacy_entry_contract(legacy):
    source = ContractPlugin.probe.__neko_event_meta__
    if legacy:
        source = LegacyEventMeta(
            "plugin_entry", "probe", "Probe", metadata={"agent_auto": False}
        )
        source.timeout = 5
        source.llm_result_fields = ["summary"]
        source.llm_result_schema = {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
        }
        source.model_validate = False
    payload = json.loads(json.dumps(metadata_scanner._event_meta_payload(source)))
    for name in (
        "timeout",
        "llm_result_fields",
        "llm_result_schema",
        "model_validate",
        "metadata",
    ):
        assert payload[name] == getattr(source, name)
    payload["metadata"]["agent_auto"] = True
    assert source.metadata["agent_auto"] is False


@pytest.mark.parametrize("declared", [False, True])
async def test_registered_entry_survives_ipc_http_listing_and_agent_projection(
    isolated_registry, monkeypatch, declared
):
    from brain.task_executor import DirectTaskExecutor
    from utils.result_parser import parse_plugin_result

    config = {"entries": manifest_entries()} if declared else {}
    registry.scan_static_metadata("contract", ContractPlugin, config, {})
    preview = registry._extract_entries_preview("contract", ContractPlugin, config, {})
    wire = json.loads(
        json.dumps(
            {
                key: metadata_scanner._event_meta_payload(h.meta)
                for key, h in state.event_handlers.items()
            }
        )
    )
    metadata_scanner.install_isolated_plugin_metadata(
        "contract",
        metadata_scanner.IsolatedPluginMetadata(
            preview, wire, {"probe": "probe", "consult": "consult"}
        ),
    )
    monkeypatch.setattr(
        state,
        "get_plugins_snapshot_cached",
        lambda **_: {"contract": {"id": "contract", "entries_preview": preview}},
    )
    monkeypatch.setattr(
        state,
        "get_plugin_hosts_snapshot_cached",
        lambda **_: {"contract": SimpleNamespace(is_alive=lambda: True)},
    )
    monkeypatch.setattr(query_service, "_install_source_index", lambda: ({}, {}))
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/plugins?locale=en")
    assert response.status_code == 200
    plugin = response.json()["plugins"][0]
    entries = {entry["id"]: entry for entry in plugin["entries"]}
    assert entries["probe"]["metadata"]["agent_auto"] is False
    assert entries["consult"]["timeout"] == 100
    assert entries["consult"]["llm_result_fields"] == ["summary"]
    executor = object.__new__(DirectTaskExecutor)
    assert [e["id"] for e in executor._agent_visible_plugin_entries(plugin)] == [
        "consult"
    ]
    assert (
        parse_plugin_result(
            {"summary": "New evidence", "response": {"private": True}},
            llm_result_fields=entries["consult"]["llm_result_fields"],
            plugin_message="done",
        )
        == "New evidence"
    )


def test_config_controls_are_preserved_without_reinterpreting_empty_values(
    isolated_registry,
):
    entry = {
        "id": "consult",
        "name": "Configured",
        "description": "Configured description",
        "input_schema": {},
        "metadata": {},
        "timeout": 0,
        "llm_result_fields": [],
        "llm_result_schema": {},
        "kind": "service",
        "auto_start": False,
        "enabled": False,
        "dynamic": False,
        "model_validate": False,
        "persist": False,
        "return_message": "",
        "extra": {},
    }
    original = copy.deepcopy(entry)
    registry.scan_static_metadata(
        "contract",
        ContractPlugin,
        {"entries": [entry]},
        {"entries": [{"id": "consult", "timeout": 999}]},
    )
    meta = state.event_handlers["contract.consult"].meta
    for name, value in original.items():
        assert getattr(meta, name) == value, name
    meta.metadata["changed"] = True
    meta.llm_result_fields.append("changed")
    assert entry == original


def test_config_only_registration_invalidates_handler_snapshot(isolated_registry):
    class PlainPlugin:
        async def invoke(self):
            pass

    assert state.get_event_handlers_snapshot_cached() == {}
    registry.scan_static_metadata(
        "plain",
        PlainPlugin,
        {"entries": [{"id": "invoke", "metadata": {"agent_auto": False}}]},
        {},
    )
    assert state.get_event_handlers_snapshot_cached()["plain.invoke"].meta.metadata == {
        "agent_auto": False
    }


def test_explicit_empty_entries_do_not_fall_back_to_manifest(isolated_registry):
    class PlainPlugin:
        async def invoke(self):
            pass

    registry.scan_static_metadata(
        "plain", PlainPlugin, {"entries": []}, {"entries": [{"id": "invoke"}]}
    )
    assert state.event_handlers == {}


def test_preview_does_not_restore_controls_explicitly_cleared_by_config(
    isolated_registry,
):
    configured = {
        "entries": [
            {
                "id": "probe",
                "metadata": {},
                "timeout": None,
                "llm_result_fields": [],
                "llm_result_schema": {},
            }
        ]
    }
    registry.scan_static_metadata("contract", ContractPlugin, configured, {})
    entries, seen = query_service._build_entries_from_handlers(
        plugin_id="contract", handlers_snapshot=dict(state.event_handlers)
    )
    preview = registry._extract_entries_preview(
        "contract", ContractPlugin, configured, {}
    )
    query_service._append_entries_from_preview(
        plugin_id="contract",
        plugin_meta={"entries_preview": preview},
        entries=entries,
        seen=seen,
    )
    probe = next(entry for entry in entries if entry["id"] == "probe")
    assert probe["metadata"] == {}
    assert probe["timeout"] is None
    assert probe["llm_result_fields"] == []


def test_quick_action_config_survives_as_structured_metadata():
    from plugin.sdk.shared.core.events import QuickActionConfig

    source = copy.deepcopy(ContractPlugin.consult.__neko_event_meta__)
    source.quick_action = True
    source.quick_action_config = QuickActionConfig(icon="search", priority=7)
    wire = metadata_scanner._event_meta_payload(source)
    assert wire["quick_action"] is True
    assert wire["quick_action_config"] == {"icon": "search", "priority": 7}


def test_unrelated_dataclass_metadata_keeps_legacy_json_conversion():
    from dataclasses import dataclass

    @dataclass
    class CustomDiagnostic:
        value: int

    diagnostic = CustomDiagnostic(7)
    source = LegacyEventMeta(
        "plugin_entry", "probe", "Probe", metadata={"custom": diagnostic}
    )
    wire = metadata_scanner._event_meta_payload(source)
    assert wire["metadata"]["custom"] == str(diagnostic)


def test_wire_payload_preserves_readonly_mapping_values_without_pickling():
    original = {"agent_auto": False, "custom": {"values": [1, 2]}}
    source = LegacyEventMeta(
        "plugin_entry", "probe", "Probe", metadata=MappingProxyType(original)
    )
    source.llm_result_schema = MappingProxyType({"type": "object"})
    wire = metadata_scanner._event_meta_payload(source)
    assert wire["metadata"] == original
    assert wire["llm_result_schema"] == {"type": "object"}
    wire["metadata"]["custom"]["values"].append(3)
    assert original["custom"]["values"] == [1, 2]


def test_metadata_controls_remain_explicit_in_isolated_reconstruction(
    isolated_registry,
):
    raw = {
        "event_type": "plugin_entry",
        "id": "consult",
        "name": "Configured",
        "metadata": {},
        "timeout": None,
        "llm_result_fields": [],
        "llm_result_schema": {},
        "auto_start": False,
        "enabled": False,
        "dynamic": False,
        "model_validate": False,
    }
    metadata_scanner.install_isolated_plugin_metadata(
        "contract",
        metadata_scanner.IsolatedPluginMetadata(
            [], {"contract.consult": raw}, {"consult": "consult"}
        ),
    )
    installed = state.event_handlers["contract.consult"].meta
    for name, value in raw.items():
        assert getattr(installed, name) == value


@pytest.mark.parametrize("partial", [False, True])
def test_packaging_uses_fixed_worker_and_restores_v3_contract_artifacts(
    tmp_path: Path, isolated_registry, monkeypatch, partial
):
    from plugin.neko_plugin_cli.core.metadata_probe import derive_plugin_metadata
    from plugin.server.application.plugins.lifecycle_service import (
        _read_packaged_isolated_metadata,
    )
    from plugin.server.infrastructure import packaged_metadata

    plugin_dir = tmp_path / "contract"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        '[plugin]\nid="contract"\nname="Contract"\nversion="1.0.0"\ntype="plugin"\n'
        'entry="plugin.plugins.contract:ContractPlugin"\n'
        '[[entries]]\nid="consult"\nname="Configured"\n'
        + ('' if partial else 'timeout=100\nllm_result_fields=["summary"]\nmetadata={agent_auto=false}\n'),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from plugin.sdk.plugin import NekoPluginBase, neko_plugin, plugin_entry\n"
        "@neko_plugin\nclass ContractPlugin(NekoPluginBase):\n"
        '    @plugin_entry(id="consult", timeout=100, llm_result_fields=["summary"], metadata={"agent_auto":False})\n'
        '    async def consult(self): return {"summary":"New evidence"}\n',
        encoding="utf-8",
    )
    payload = derive_plugin_metadata(plugin_dir)
    preview = next(entry for entry in payload["entries"] if entry["id"] == "consult")
    assert preview["timeout"] == 100
    assert preview["llm_result_fields"] == ["summary"]
    assert preview["metadata"] == {"agent_auto": False}
    handler = payload["handlers"]["contract.consult"]
    assert handler["timeout"] == 100
    assert handler["llm_result_fields"] == ["summary"]
    assert handler["metadata"] == {"agent_auto": False}
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    config = tomllib.loads((plugin_dir / "plugin.toml").read_text(encoding="utf-8"))
    loaded = _read_packaged_isolated_metadata(
        plugin_dir / "plugin.toml", "contract", conf=config, pdata=config["plugin"]
    )
    assert loaded is not None
    metadata_scanner.install_isolated_plugin_metadata("contract", loaded)
    assert state.event_handlers["contract.consult"].meta.timeout == 100

    # Old files may match all source fingerprints and still have truncated handlers.
    payload["schema_version"] = 3
    for old_handler in payload["handlers"].values():
        old_handler.pop("timeout", None)
        old_handler.pop("llm_result_fields", None)
        old_handler["metadata"] = None
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    restored = _read_packaged_isolated_metadata(
        plugin_dir / "plugin.toml", "contract", conf=config, pdata=config["plugin"]
    )
    assert restored is not None
    for old_handler in restored.handlers.values():
        assert old_handler["timeout"] == 100
        assert old_handler["llm_result_fields"] == ["summary"]
        assert old_handler["metadata"] == {"agent_auto": False}
    metadata_scanner.install_isolated_plugin_metadata("contract", restored)
    assert state.event_handlers["contract.consult"].meta.timeout == 100

    # Reading the old format must not import the plugin or rewrite the artifact.
    def cannot_import(*args, **kwargs):
        raise AssertionError("metadata reads must not import plugins")

    monkeypatch.setattr(
        metadata_scanner, "scan_plugin_metadata_isolated", cannot_import
    )
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None
    assert json.loads(meta_path.read_text()) == payload
    payload["schema_version"] = 2
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None


@pytest.mark.parametrize("controls", [{}, {
    "timeout": 20, "llm_result_fields": ["answer"], "metadata": {"agent_auto": True},
}, {"timeout": None, "llm_result_fields": [], "metadata": {}}])
@pytest.mark.parametrize("alias", [False, True])
def test_partial_config_merges_decorator_and_matches_static_listing(
    isolated_registry, controls, alias,
):
    class Plugin:
        @plugin_entry(id="probe", name="Decorated", description="Description",
                      timeout=100, llm_result_fields=["summary"],
                      metadata={"agent_auto": False})
        async def invoke(self):
            pass

    if not alias:
        Plugin.probe = Plugin.invoke
        del Plugin.invoke
    configured = {"id": "probe", "name": "Configured", **controls}
    conf = {"entries": [configured]}
    original_conf = copy.deepcopy(conf)
    source = getattr(Plugin, "invoke" if alias else "probe").__neko_event_meta__
    original = copy.deepcopy(source)
    preview = registry._extract_entries_preview("contract", Plugin, conf, {})
    registry.scan_static_metadata("contract", Plugin, conf, {})
    meta = state.event_handlers["contract.probe"].meta
    expected = {"timeout": 100, "llm_result_fields": ["summary"],
                "metadata": {"agent_auto": False}, **controls}
    for key, value in expected.items():
        assert getattr(meta, key) == value
        assert preview[0][key] == value
    assert meta.name == "Configured"
    before = []
    query_service._append_entries_from_preview(
        plugin_id="contract", plugin_meta={"entries_preview": preview}, entries=before, seen=set(),
    )
    after, _ = query_service._build_entries_from_handlers(
        plugin_id="contract", handlers_snapshot=dict(state.event_handlers),
    )
    for key in expected:
        assert before[0][key] == after[0][key]
    meta.metadata["mutation"] = True
    assert source.metadata == original.metadata
    assert conf == original_conf
    assert "mutation" not in preview[0]["metadata"]
