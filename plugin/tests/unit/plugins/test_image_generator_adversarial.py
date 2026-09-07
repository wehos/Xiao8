"""Adversarial security and runtime-contract tests for image_generator."""

from __future__ import annotations

import asyncio
import base64
import copy
import http.server
import io
import json
import re
import struct
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from PIL import Image

import plugin.plugins.image_generator as image_generator_module
from plugin.core.host import PluginProcessHost
from plugin.plugins.image_generator import DEFAULT_SETTINGS, ImageGeneratorPlugin
from plugin.server.routes import plugin_ui as plugin_ui_route_module
from plugin.sdk.plugin import Err, Ok, SdkError
from plugin.sdk.plugin.llm_tool import entry_id_for_tool
from plugin.sdk.shared.constants import EVENT_META_ATTR
from plugin.sdk.shared.core.push_message_schema import translate_push_message

pytestmark = pytest.mark.plugin_unit

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins" / "image_generator"
PLUGIN_TOML = PLUGIN_DIR / "plugin.toml"

OLD_SECRET = "opaqueOLDkey_812345"
NEW_SECRET = "opaqueNEWkey_923456"
LOG_SECRET = "plaintext-host-log-regression-734921"


class CaptureLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple[Any, ...]]] = []

    def _capture(self, level: str, *args: Any, **_kwargs: Any) -> None:
        self.records.append((level, args))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._capture("debug", *args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._capture("info", *args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._capture("warning", *args, **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._capture("error", *args, **kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._capture("exception", *args, **kwargs)

    def rendered(self) -> str:
        return "\n".join(
            f"{level} " + " ".join(str(item) for item in args)
            for level, args in self.records
        )


class MemoryStore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.enabled = True
        self.data = copy.deepcopy(data or {})
        self.fail_get = False

    async def get(self, key: str, default: Any = None):
        if self.fail_get:
            return Err(SdkError("adversarial private Store read failure"))
        return Ok(copy.deepcopy(self.data.get(key, default)))

    async def set(self, key: str, value: Any):
        self.data[key] = copy.deepcopy(value)
        return Ok(None)

    async def delete(self, key: str):
        existed = key in self.data
        self.data.pop(key, None)
        return Ok(existed)


class BarrierStore(MemoryStore):
    """Pauses exactly one credential read and records the first settings write."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.block_next_key_read = False
        self.key_read_started = asyncio.Event()
        self.allow_key_read = asyncio.Event()
        self.settings_write_started = asyncio.Event()
        self.block_next_settings_write = False
        self.allow_settings_write = asyncio.Event()
        self.block_next_settings_delete = False
        self.settings_delete_started = asyncio.Event()
        self.allow_settings_delete = asyncio.Event()

    async def get(self, key: str, default: Any = None):
        if key == "api_key" and self.block_next_key_read:
            self.block_next_key_read = False
            self.key_read_started.set()
            await self.allow_key_read.wait()
        return await super().get(key, default)

    async def set(self, key: str, value: Any):
        if key == "settings":
            self.settings_write_started.set()
            if self.block_next_settings_write:
                self.block_next_settings_write = False
                await self.allow_settings_write.wait()
        return await super().set(key, value)

    async def delete(self, key: str):
        if key == "settings" and self.block_next_settings_delete:
            self.block_next_settings_delete = False
            self.settings_delete_started.set()
            await self.allow_settings_delete.wait()
        return await super().delete(key)


def _effective_config() -> dict[str, Any]:
    return {
        "plugin": {"store": {"enabled": True}, "ui": {"enabled": True}},
        "image_generator": copy.deepcopy(DEFAULT_SETTINGS),
    }


class AdversarialContext:
    plugin_id = "image_generator"
    metadata: dict[str, Any] = {}
    bus = None
    message_queue = None

    def __init__(self) -> None:
        self.logger = CaptureLogger()
        self.config_path = PLUGIN_TOML
        self.config = _effective_config()
        self._effective_config = self.config
        self.pushed: list[dict[str, Any]] = []
        self.status_updates: list[dict[str, Any]] = []

    async def get_own_config(self, timeout: float = 5.0) -> dict[str, Any]:
        del timeout
        return {"config": copy.deepcopy(self.config)}

    def push_message(self, **kwargs: Any) -> dict[str, bool]:
        self.pushed.append(copy.deepcopy(kwargs))
        return {"ok": True}

    def update_status(self, status: dict[str, Any]) -> None:
        self.status_updates.append(copy.deepcopy(status))


@pytest.fixture(autouse=True)
def isolate_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NEKO_STORAGE_SELECTED_ROOT", str(tmp_path / "runtime"))
    for name in (
        "NEKO_STORAGE_ANCHOR_ROOT",
        "NEKO_PLUGIN_SERVER_ORIGIN",
        "NEKO_USER_PLUGIN_SERVER_ORIGIN",
        "NEKO_SERVER_ORIGIN",
        "NEKO_USER_PLUGIN_SERVER_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolate_install_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep generated assets out of the real source checkout.

    Generated images are written under the installed plugin tree's
    static/generated (the only directory the frozen Steam host serves), so
    every startup() would otherwise create plugin/plugins/image_generator/
    static/generated in the repo. Redirect config_dir to a per-test tmp
    install tree pre-seeded with the bundled index.html."""
    install_dir = tmp_path / "install"
    static_dir = install_dir / "static"
    static_dir.mkdir(parents=True)
    (install_dir / "plugin.toml").write_text(
        PLUGIN_TOML.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (static_dir / "index.html").write_bytes(
        (PLUGIN_DIR / "static" / "index.html").read_bytes()
    )
    from plugin.sdk.plugin.base import NekoPluginBase

    monkeypatch.setattr(
        NekoPluginBase, "config_dir", property(lambda self: install_dir)
    )


def make_plugin(
    store: MemoryStore | None = None,
) -> tuple[ImageGeneratorPlugin, AdversarialContext, MemoryStore]:
    context = AdversarialContext()
    plugin = ImageGeneratorPlugin(context)
    resolved_store = store or MemoryStore()
    plugin.store = resolved_store
    return plugin, context, resolved_store


def encrypted_document(**overrides: Any) -> dict[str, Any]:
    payload = copy.deepcopy(DEFAULT_SETTINGS)
    payload.update(
        {
            "api_key": "",
            "clear_api_key": False,
        }
    )
    payload.update(overrides)
    return payload


def real_png_bytes(
    *,
    size: tuple[int, int] = (7, 5),
    color: tuple[int, int, int, int] = (26, 115, 232, 255),
) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


def png_pixel_bomb(width: int = 20_000, height: int = 20_000) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def stored_history(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"history-{index}",
            "timestamp": f"2026-07-{26 - index:02d}T00:00:00+08:00",
            "model": "history-model",
            "prompt_excerpt": f"history prompt {index}",
            "result_url": "",
            "status": "succeeded",
        }
        for index in range(count)
    ]


def encrypt_for_panel(document: dict[str, Any], envelope: dict[str, Any]) -> str:
    """Mirror the panel's RSA-wrapped AES-GCM WebCrypto envelope."""

    assert envelope["algorithm"] == "RSA-OAEP-256+A256GCM"
    key_id = envelope["key_id"]
    binding = f"image_generator:{key_id}".encode()
    public_key = serialization.load_der_public_key(
        base64.b64decode(envelope["public_key_spki_b64"], validate=True)
    )
    aes_key = AESGCM.generate_key(bit_length=256)
    iv = b"\x9f\x84\x0e\x0a\xec\x97\x11\xeb\x05\xbe\xab\x8c"
    wrapped_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=binding,
        ),
    )
    plaintext = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    ciphertext = AESGCM(aes_key).encrypt(iv, plaintext, binding)
    body = {
        "v": 1,
        "wrapped_key": base64.b64encode(wrapped_key).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    return base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


async def encrypted_save_args(
    plugin: ImageGeneratorPlugin,
    *,
    secret: str,
    **settings_overrides: Any,
) -> dict[str, Any]:
    state = await plugin.get_panel_state()
    assert state.is_ok()
    envelope = state.value["secret_envelope"]
    document = encrypted_document(api_key=secret, **settings_overrides)
    return {
        "encrypted_payload": encrypt_for_panel(document, envelope),
        "key_id": envelope["key_id"],
    }


def serialized(*values: Any) -> str:
    return json.dumps(values, ensure_ascii=False, default=str)


def assert_not_outward(
    secret: str,
    *,
    result: Any,
    context: AdversarialContext,
    store: MemoryStore,
) -> None:
    history = store.data.get("recent_generations", [])
    outward = serialized(
        result,
        context.logger.rendered(),
        context.pushed,
        context.status_updates,
        history,
    )
    assert secret not in outward


@contextmanager
def running_server(
    handler_factory: Any,
) -> Iterator[http.server.ThreadingHTTPServer]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_secret_envelope_schema_rejects_plaintext_and_is_one_time() -> None:
    plugin, context, store = make_plugin()
    await plugin.startup()
    try:
        meta = getattr(ImageGeneratorPlugin.save_settings, EVENT_META_ATTR)
        properties = meta.input_schema["properties"]
        assert "api_key" not in properties
        assert set(properties) == {"encrypted_payload", "key_id"}

        state = await plugin.get_panel_state()
        envelope = state.value["secret_envelope"]
        assert set(envelope) >= {
            "key_id",
            "public_key_spki_b64",
            "algorithm",
            "expires_at",
        }
        assert envelope["algorithm"] == "RSA-OAEP-256+A256GCM"
        assert "private" not in serialized(envelope).lower()
        assert LOG_SECRET not in serialized(state.value)

        plain = await plugin.save_settings(
            api_key=LOG_SECRET,
            model="plaintext-must-be-rejected",
        )
        assert plain.is_err()
        assert "api_key" not in store.data

        args = await encrypted_save_args(plugin, secret=LOG_SECRET)
        saved = await plugin.save_settings(**args)
        replayed = await plugin.save_settings(**args)

        assert saved.is_ok()
        assert store.data["api_key"] == LOG_SECRET
        assert replayed.is_err()
        assert LOG_SECRET not in serialized(saved.value, replayed.error)
        assert_not_outward(
            LOG_SECRET,
            result=(plain, saved, replayed),
            context=context,
            store=store,
        )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_secret_envelopes_are_bounded_expiring_and_tamper_evident() -> None:
    plugin, context, store = make_plugin()
    await plugin.startup()
    try:
        max_pending = image_generator_module._SECRET_ENVELOPE_MAX_PENDING
        evicted_args = await encrypted_save_args(plugin, secret=OLD_SECRET)
        for _ in range(max_pending + 1):
            state = await plugin.get_panel_state()
            assert state.is_ok()
        assert len(plugin._secret_envelopes) <= max_pending

        evicted = await plugin.save_settings(**evicted_args)
        assert evicted.is_err()
        assert "api_key" not in store.data

        tampered_args = await encrypted_save_args(plugin, secret=NEW_SECRET)
        raw = bytearray(base64.b64decode(tampered_args["encrypted_payload"]))
        raw[len(raw) // 2] ^= 1
        tampered_args["encrypted_payload"] = base64.b64encode(raw).decode()
        tampered = await plugin.save_settings(**tampered_args)
        assert tampered.is_err()
        assert tampered_args["key_id"] not in plugin._secret_envelopes
        assert "api_key" not in store.data

        expired_args = await encrypted_save_args(plugin, secret=NEW_SECRET)
        expired_key_id = expired_args["key_id"]
        with plugin._envelope_lock:
            private_key, _expires_at = plugin._secret_envelopes[expired_key_id]
            plugin._secret_envelopes[expired_key_id] = (private_key, -1.0)
        expired = await plugin.save_settings(**expired_args)
        assert expired.is_err()
        assert expired_key_id not in plugin._secret_envelopes
        assert "api_key" not in store.data

        for secret in (OLD_SECRET, NEW_SECRET):
            assert_not_outward(
                secret,
                result=(evicted, tampered, expired),
                context=context,
                store=store,
            )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_panel_state_degrades_safely_when_rsa_key_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore({"api_key": OLD_SECRET})
    plugin, context, _store = make_plugin(store)
    await plugin.startup()

    def fail_key_generation(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"cryptography backend failed near {OLD_SECRET}")

    # pycryptodomex is imported lazily as _CD_RSA; patch the generate symbol.
    monkeypatch.setattr(
        image_generator_module._CD_RSA,
        "generate",
        fail_key_generation,
    )
    try:
        state = await plugin.get_panel_state()

        assert state.is_ok()
        assert state.value["secret_envelope"] is None
        assert state.value["api_key_configured"] is True
        assert "加密" in str(state.value["configuration_warning"])
        assert OLD_SECRET not in serialized(
            state.value,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_host_trigger_debug_log_never_receives_plaintext_api_key() -> None:
    plugin, context, store = make_plugin()
    await plugin.startup()
    host_logger = CaptureLogger()

    class DirectCommunication:
        async def trigger(
            self,
            entry_id: str,
            args: dict[str, Any],
            timeout: float | None,
        ):
            assert entry_id == "save_settings"
            del timeout
            return await plugin.save_settings(**args)

    host = SimpleNamespace(
        plugin_id="image_generator",
        logger=host_logger,
        comm_manager=DirectCommunication(),
    )
    try:
        args = await encrypted_save_args(plugin, secret=LOG_SECRET)
        assert LOG_SECRET not in serialized(args)

        result = await PluginProcessHost.trigger(
            host,
            "save_settings",
            args,
            timeout=5.0,
        )

        assert result.is_ok()
        assert store.data["api_key"] == LOG_SECRET
        assert LOG_SECRET not in host_logger.rendered()
        assert LOG_SECRET not in serialized(result.value)

        smuggled_args = await encrypted_save_args(
            plugin,
            secret=LOG_SECRET,
            model=f"portable-{LOG_SECRET}",
        )
        assert LOG_SECRET not in serialized(smuggled_args)
        rejected = await PluginProcessHost.trigger(
            host,
            "save_settings",
            smuggled_args,
            timeout=5.0,
        )
        assert rejected.is_err()
        assert LOG_SECRET not in host_logger.rendered()
        assert LOG_SECRET not in serialized(rejected.error)
        assert_not_outward(
            LOG_SECRET,
            result=(result, rejected),
            context=context,
            store=store,
        )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_generation_snapshot_cannot_mix_old_settings_with_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"
    old_settings["model"] = "old-model"
    store = BarrierStore({"settings": old_settings, "api_key": OLD_SECRET})
    plugin, _context, _store = make_plugin(store)
    await plugin.startup()
    try:
        save_args = await encrypted_save_args(
            plugin,
            secret=NEW_SECRET,
            provider="custom",
            api_base_url="https://new-provider.example/v1",
            model="new-model",
        )
        store.block_next_key_read = True

        observed_requests: list[tuple[str, str, str]] = []

        async def capture_request(**kwargs: Any):
            observed_requests.append(
                (
                    kwargs["settings"]["api_base_url"],
                    kwargs["settings"]["model"],
                    kwargs["api_key"],
                )
            )
            return real_png_bytes(), "image/png", "png", ""

        monkeypatch.setattr(plugin, "_request_generation", capture_request)
        generation_task = asyncio.create_task(
            plugin.generate_image(prompt="atomic pair")
        )
        await asyncio.wait_for(store.key_read_started.wait(), timeout=5)
        save_task = asyncio.create_task(plugin.save_settings(**save_args))

        # The save task has been scheduled, but the settings write cannot begin
        # while the snapshot owns the shared mutation lock.
        for _ in range(10):
            await asyncio.sleep(0)
        write_started_before_release = store.settings_write_started.is_set()

        store.allow_key_read.set()
        generated = await asyncio.wait_for(generation_task, timeout=5)
        saved = await asyncio.wait_for(save_task, timeout=5)

        assert not write_started_before_release
        assert generated.is_ok()
        assert saved.is_ok()
        assert observed_requests == [
            (
                old_settings["api_base_url"],
                "old-model",
                OLD_SECRET,
            )
        ]

        current_settings, current_key = await plugin._generation_config_snapshot()
        assert current_settings["api_base_url"] == ("https://new-provider.example/v1")
        assert current_settings["model"] == "new-model"
        assert current_key == NEW_SECRET
    finally:
        store.allow_key_read.set()
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_generation_waits_for_atomic_settings_and_key_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"
    old_settings["model"] = "old-model"
    store = BarrierStore({"settings": old_settings, "api_key": OLD_SECRET})
    plugin, _context, _store = make_plugin(store)
    await plugin.startup()
    try:
        save_args = await encrypted_save_args(
            plugin,
            secret=NEW_SECRET,
            provider="custom",
            api_base_url="https://new-provider.example/v1",
            model="new-model",
        )
        observed_requests: list[tuple[str, str, str]] = []
        request_started = asyncio.Event()

        async def capture_request(**kwargs: Any):
            observed_requests.append(
                (
                    kwargs["settings"]["api_base_url"],
                    kwargs["settings"]["model"],
                    kwargs["api_key"],
                )
            )
            request_started.set()
            return real_png_bytes(), "image/png", "png", ""

        monkeypatch.setattr(plugin, "_request_generation", capture_request)
        store.settings_write_started.clear()
        store.block_next_settings_write = True

        save_task = asyncio.create_task(plugin.save_settings(**save_args))
        await asyncio.wait_for(store.settings_write_started.wait(), timeout=5)
        generation_task = asyncio.create_task(
            plugin.generate_image(prompt="save owns atomic pair")
        )
        for _ in range(10):
            await asyncio.sleep(0)
        request_started_before_release = request_started.is_set()

        store.allow_settings_write.set()
        saved = await asyncio.wait_for(save_task, timeout=5)
        generated = await asyncio.wait_for(generation_task, timeout=5)

        assert not request_started_before_release
        assert saved.is_ok()
        assert generated.is_ok()
        assert observed_requests == [
            (
                "https://new-provider.example/v1",
                "new-model",
                NEW_SECRET,
            )
        ]
    finally:
        store.allow_settings_write.set()
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_replace_rejects_both_old_and_new_secret_smuggling() -> None:
    plugin, context, store = make_plugin()
    await plugin.startup()
    try:
        established = await plugin.save_settings(
            **await encrypted_save_args(plugin, secret=OLD_SECRET)
        )
        assert established.is_ok()
        baseline_settings = copy.deepcopy(store.data["settings"])

        old_smuggle_args = await encrypted_save_args(
            plugin,
            secret=NEW_SECRET,
            model=f"portable-{OLD_SECRET}",
        )
        assert OLD_SECRET not in serialized(old_smuggle_args)
        assert NEW_SECRET not in serialized(old_smuggle_args)
        old_smuggle = await plugin.save_settings(**old_smuggle_args)
        assert old_smuggle.is_err()
        assert store.data["api_key"] == OLD_SECRET
        assert store.data["settings"] == baseline_settings

        new_smuggle_args = await encrypted_save_args(
            plugin,
            secret=NEW_SECRET,
            model=f"portable-{NEW_SECRET}",
        )
        assert NEW_SECRET not in serialized(new_smuggle_args)
        new_smuggle = await plugin.save_settings(**new_smuggle_args)
        assert new_smuggle.is_err()
        assert store.data["api_key"] == OLD_SECRET
        assert store.data["settings"] == baseline_settings

        panel = await plugin.get_panel_state()
        assert panel.is_ok()
        assert OLD_SECRET not in serialized(panel.value)
        assert NEW_SECRET not in serialized(panel.value)
        for secret in (OLD_SECRET, NEW_SECRET):
            assert_not_outward(
                secret,
                result=(old_smuggle, new_smuggle, panel),
                context=context,
                store=store,
            )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_clear_rejects_current_secret_smuggling_before_mutation() -> None:
    plugin, context, store = make_plugin()
    await plugin.startup()
    try:
        established = await plugin.save_settings(
            **await encrypted_save_args(plugin, secret=OLD_SECRET)
        )
        assert established.is_ok()
        baseline_settings = copy.deepcopy(store.data["settings"])

        clear_args = await encrypted_save_args(
            plugin,
            secret="",
            clear_api_key=True,
            model=f"portable-{OLD_SECRET}",
        )
        assert OLD_SECRET not in serialized(clear_args)
        result = await plugin.save_settings(**clear_args)

        assert result.is_err()
        assert store.data["api_key"] == OLD_SECRET
        assert store.data["settings"] == baseline_settings
        panel = await plugin.get_panel_state()
        assert OLD_SECRET not in serialized(result.error, panel.value)
        assert_not_outward(
            OLD_SECRET,
            result=(result, panel),
            context=context,
            store=store,
        )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_direct_clear_rejects_poisoned_settings_then_scrubs_history() -> None:
    poisoned_settings = copy.deepcopy(DEFAULT_SETTINGS)
    poisoned_settings["model"] = f"portable-{OLD_SECRET}"
    poisoned_history = [
        {
            "id": "poisoned-clear-record",
            "timestamp": "2026-07-26T00:00:00+08:00",
            "model": "old-model",
            "prompt_excerpt": f"draw {OLD_SECRET}",
            "result_url": "",
            "status": "failed",
        }
    ]
    store = MemoryStore(
        {
            "api_key": OLD_SECRET,
            "settings": poisoned_settings,
            "recent_generations": poisoned_history,
        }
    )
    plugin, context, _store = make_plugin(store)
    await plugin.startup()
    try:
        rejected = await plugin.clear_api_key()
        assert rejected.is_err()
        assert store.data["api_key"] == OLD_SECRET
        assert OLD_SECRET not in serialized(
            rejected.error,
            context.logger.rendered(),
            context.status_updates,
        )

        # Once non-secret settings are repaired, direct clear must redact any
        # historical copies before deleting the only authoritative secret.
        store.data["settings"] = copy.deepcopy(DEFAULT_SETTINGS)
        cleared = await plugin.clear_api_key()
        assert cleared.is_ok()
        assert "api_key" not in store.data
        assert OLD_SECRET not in serialized(
            cleared.value,
            store.data.get("recent_generations", []),
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_save_fails_closed_when_current_store_snapshot_cannot_be_read() -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"
    old_settings["model"] = "old-model"
    old_history = [
        {
            "id": "stable-record",
            "timestamp": "2026-07-26T00:00:00+08:00",
            "model": "old-model",
            "prompt_excerpt": "stable history",
            "result_url": "",
            "status": "failed",
        }
    ]
    store = MemoryStore(
        {
            "api_key": OLD_SECRET,
            "settings": old_settings,
            "recent_generations": old_history,
        }
    )
    plugin, context, _store = make_plugin(store)
    await plugin.startup()
    try:
        replacement_args = await encrypted_save_args(
            plugin,
            secret=NEW_SECRET,
            provider="custom",
            api_base_url="https://new-provider.example/v1",
            model="new-model",
        )
        persisted_before = copy.deepcopy(store.data)
        runtime_before = plugin._settings_snapshot()
        store.fail_get = True

        result = await plugin.save_settings(**replacement_args)

        assert result.is_err()
        assert store.data == persisted_before
        assert plugin._settings_snapshot() == runtime_before
        assert OLD_SECRET not in serialized(
            result.error,
            context.logger.rendered(),
            context.status_updates,
        )
        assert NEW_SECRET not in serialized(
            result.error,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        store.fail_get = False
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_save_crash_cannot_pair_new_endpoint_with_old_api_key() -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"

    class CrashAfterSettingsCommitStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__(
                {
                    "settings": old_settings,
                    "api_key": OLD_SECRET,
                }
            )
            self.crashed = False

        async def set(self, key: str, value: Any):
            result = await super().set(key, value)
            if key == "settings" and not self.crashed:
                self.crashed = True
                raise SystemExit("simulated process loss after settings commit")
            return result

    store = CrashAfterSettingsCommitStore()
    plugin, _context, _store = make_plugin(store)
    started = await plugin.startup()
    assert started.is_ok()

    with pytest.raises(SystemExit, match="simulated process loss"):
        await plugin.save_settings(
            **await encrypted_save_args(
                plugin,
                secret="",
                provider="custom",
                api_base_url="https://new-provider.example/v1",
            )
        )

    restarted, _restarted_context, _restarted_store = make_plugin(store)
    restarted_result = await restarted.startup()
    assert restarted_result.is_ok()
    settings, api_key = await restarted._generation_config_snapshot()

    assert settings["api_base_url"] == "https://new-provider.example/v1"
    assert api_key == ""
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_save_cancellation_after_key_commit_cannot_mix_runtime_config() -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"

    class CancelAfterKeyCommitStore(MemoryStore):
        async def set(self, key: str, value: Any):
            result = await super().set(key, value)
            if key == "api_key" and value == NEW_SECRET:
                raise asyncio.CancelledError("cancelled after durable key commit")
            return result

    store = CancelAfterKeyCommitStore(
        {
            "settings": old_settings,
            "api_key": OLD_SECRET,
        }
    )
    plugin, _context, _store = make_plugin(store)
    started = await plugin.startup()
    assert started.is_ok()
    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="cancelled after durable key commit",
        ):
            await plugin.save_settings(
                **await encrypted_save_args(
                    plugin,
                    secret=NEW_SECRET,
                    provider="custom",
                    api_base_url="https://new-provider.example/v1",
                )
            )

        settings, api_key = await plugin._generation_config_snapshot()
        assert store.data["settings"]["api_base_url"] == (
            "https://new-provider.example/v1"
        )
        assert settings["api_base_url"] == "https://new-provider.example/v1"
        assert api_key == NEW_SECRET
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_save_cancellation_during_key_rollback_cannot_mix_runtime_config() -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"

    class CancelAfterRollbackKeyCommitStore(MemoryStore):
        async def set(self, key: str, value: Any):
            if key == "api_key" and value == NEW_SECRET:
                return Err(SdkError("force forward key failure"))
            result = await super().set(key, value)
            if key == "api_key" and value == OLD_SECRET:
                raise asyncio.CancelledError(
                    "cancelled after durable rollback key commit"
                )
            return result

    store = CancelAfterRollbackKeyCommitStore(
        {
            "settings": old_settings,
            "api_key": OLD_SECRET,
        }
    )
    plugin, _context, _store = make_plugin(store)
    started = await plugin.startup()
    assert started.is_ok()
    try:
        with pytest.raises(
            asyncio.CancelledError,
            match="cancelled after durable rollback key commit",
        ):
            await plugin.save_settings(
                **await encrypted_save_args(
                    plugin,
                    secret=NEW_SECRET,
                    provider="custom",
                    api_base_url="https://new-provider.example/v1",
                )
            )

        settings, api_key = await plugin._generation_config_snapshot()
        assert store.data["settings"]["api_base_url"] == (
            "https://old-provider.example/v1"
        )
        assert settings["api_base_url"] == "https://old-provider.example/v1"
        assert api_key == OLD_SECRET
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_ambiguous_key_commit_and_failed_delete_keep_matching_runtime() -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"

    class AmbiguousKeyCommitStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__(
                {
                    "settings": old_settings,
                    "api_key": OLD_SECRET,
                }
            )
            self.key_delete_calls = 0

        async def set(self, key: str, value: Any):
            result = await super().set(key, value)
            if key == "api_key" and value == NEW_SECRET:
                return Err(SdkError("result lost after durable key commit"))
            return result

        async def delete(self, key: str):
            if key == "api_key":
                self.key_delete_calls += 1
                if self.key_delete_calls == 2:
                    return Err(SdkError("rollback delete failed"))
            return await super().delete(key)

    store = AmbiguousKeyCommitStore()
    plugin, _context, _store = make_plugin(store)
    started = await plugin.startup()
    assert started.is_ok()
    try:
        result = await plugin.save_settings(
            **await encrypted_save_args(
                plugin,
                secret=NEW_SECRET,
                provider="custom",
                api_base_url="https://new-provider.example/v1",
            )
        )

        assert result.is_err()
        settings, api_key = await plugin._generation_config_snapshot()
        assert store.data["settings"]["api_base_url"] == (
            "https://new-provider.example/v1"
        )
        assert settings["api_base_url"] == "https://new-provider.example/v1"
        assert api_key == NEW_SECRET
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_lowering_history_limit_immediately_trims_persisted_history() -> None:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["history_limit"] = 5
    original_history = stored_history(5)
    store = MemoryStore(
        {
            "api_key": OLD_SECRET,
            "settings": settings,
            "recent_generations": original_history,
        }
    )
    plugin, context, _store = make_plugin(store)
    await plugin.startup()
    try:
        save_args = await encrypted_save_args(
            plugin,
            secret="",
            history_limit=2,
        )
        saved = await plugin.save_settings(**save_args)

        assert saved.is_ok()
        assert store.data["settings"]["history_limit"] == 2
        assert store.data["recent_generations"] == original_history[:2]
        panel = await plugin.get_panel_state()
        assert panel.is_ok()
        assert len(panel.value["history"]) == 2
        assert OLD_SECRET not in serialized(
            saved.value,
            panel.value,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_inflight_generation_cannot_restore_old_history_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["history_limit"] = 5
    original_history = stored_history(4)
    store = MemoryStore(
        {
            "api_key": OLD_SECRET,
            "settings": settings,
            "recent_generations": original_history,
        }
    )
    plugin, context, _store = make_plugin(store)
    await plugin.startup()
    request_started = asyncio.Event()
    allow_request_to_finish = asyncio.Event()
    generation_task: asyncio.Task[Any] | None = None
    try:

        async def delayed_request(**_kwargs: Any):
            request_started.set()
            await allow_request_to_finish.wait()
            return real_png_bytes(), "image/png", "png", ""

        monkeypatch.setattr(plugin, "_request_generation", delayed_request)
        generation_task = asyncio.create_task(
            plugin.generate_image(prompt="late generation after limit change")
        )
        await asyncio.wait_for(request_started.wait(), timeout=5)

        save_args = await encrypted_save_args(
            plugin,
            secret="",
            history_limit=1,
        )
        saved = await plugin.save_settings(**save_args)
        allow_request_to_finish.set()
        generated = await asyncio.wait_for(generation_task, timeout=5)

        assert saved.is_ok()
        assert generated.is_ok()
        assert store.data["settings"]["history_limit"] == 1
        assert len(store.data["recent_generations"]) == 1
        assert store.data["recent_generations"][0]["prompt_excerpt"] == (
            "late generation after limit change"
        )
        panel = await plugin.get_panel_state()
        assert panel.is_ok()
        assert len(panel.value["history"]) == 1
        assert OLD_SECRET not in serialized(
            saved.value,
            generated.value,
            panel.value,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        allow_request_to_finish.set()
        if generation_task is not None and not generation_task.done():
            generation_task.cancel()
            await asyncio.gather(generation_task, return_exceptions=True)
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_direct_clear_fails_closed_when_store_read_fails() -> None:
    poisoned_settings = copy.deepcopy(DEFAULT_SETTINGS)
    poisoned_settings["model"] = f"portable-{OLD_SECRET}"
    poisoned_history = [
        {
            "id": "clear-read-failure-record",
            "timestamp": "2026-07-26T00:00:00+08:00",
            "model": "old-model",
            "prompt_excerpt": f"draw {OLD_SECRET}",
            "result_url": "",
            "status": "failed",
        }
    ]
    store = MemoryStore(
        {
            "api_key": OLD_SECRET,
            "settings": poisoned_settings,
            "recent_generations": poisoned_history,
        }
    )
    plugin, context, _store = make_plugin(store)
    await plugin.startup()
    try:
        persisted_before = copy.deepcopy(store.data)
        runtime_before = plugin._settings_snapshot()
        store.fail_get = True

        result = await plugin.clear_api_key()

        assert result.is_err()
        assert store.data == persisted_before
        assert plugin._settings_snapshot() == runtime_before
        assert OLD_SECRET not in serialized(
            result.error,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        store.fail_get = False
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_reset_fails_closed_when_key_read_fails() -> None:
    custom_settings = copy.deepcopy(DEFAULT_SETTINGS)
    custom_settings["api_base_url"] = "https://custom-provider.example/v1"
    custom_settings["model"] = "custom-model"
    store = MemoryStore(
        {
            "api_key": OLD_SECRET,
            "settings": custom_settings,
        }
    )
    plugin, context, _store = make_plugin(store)
    await plugin.startup()
    try:
        persisted_before = copy.deepcopy(store.data)
        runtime_before = plugin._settings_snapshot()
        store.fail_get = True

        result = await plugin.reset_settings()

        assert result.is_err()
        assert store.data == persisted_before
        assert plugin._settings_snapshot() == runtime_before
        assert OLD_SECRET not in serialized(
            result.error,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        store.fail_get = False
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_generation_waits_for_atomic_reset_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_settings = copy.deepcopy(DEFAULT_SETTINGS)
    old_settings["provider"] = "custom"
    old_settings["api_base_url"] = "https://old-provider.example/v1"
    old_settings["model"] = "old-model"
    store = BarrierStore({"settings": old_settings, "api_key": OLD_SECRET})
    plugin, context, _store = make_plugin(store)
    await plugin.startup()
    try:
        observed_requests: list[tuple[str, str, str]] = []
        request_started = asyncio.Event()

        async def capture_request(**kwargs: Any):
            observed_requests.append(
                (
                    kwargs["settings"]["api_base_url"],
                    kwargs["settings"]["model"],
                    kwargs["api_key"],
                )
            )
            request_started.set()
            return real_png_bytes(), "image/png", "png", ""

        monkeypatch.setattr(plugin, "_request_generation", capture_request)
        store.block_next_settings_delete = True

        reset_task = asyncio.create_task(plugin.reset_settings())
        await asyncio.wait_for(store.settings_delete_started.wait(), timeout=5)
        generation_task = asyncio.create_task(
            plugin.generate_image(prompt="reset owns atomic pair")
        )
        for _ in range(10):
            await asyncio.sleep(0)
        request_started_before_release = request_started.is_set()

        store.allow_settings_delete.set()
        reset = await asyncio.wait_for(reset_task, timeout=5)
        generated = await asyncio.wait_for(generation_task, timeout=5)

        assert not request_started_before_release
        assert reset.is_ok()
        assert generated.is_ok()
        assert observed_requests == [
            (
                DEFAULT_SETTINGS["api_base_url"],
                DEFAULT_SETTINGS["model"],
                OLD_SECRET,
            )
        ]
        assert "settings" not in store.data
        assert store.data["api_key"] == OLD_SECRET
        assert OLD_SECRET not in serialized(
            reset.value,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        store.allow_settings_delete.set()
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_tampered_store_secret_is_redacted_from_every_read_surface() -> None:
    poisoned_settings = copy.deepcopy(DEFAULT_SETTINGS)
    poisoned_settings["model"] = f"portable-{OLD_SECRET}"
    store = MemoryStore(
        {
            "api_key": OLD_SECRET,
            "settings": poisoned_settings,
            "recent_generations": [
                {
                    "id": "poisoned-record",
                    "timestamp": "2026-07-26T00:00:00+08:00",
                    "model": f"portable-{OLD_SECRET}",
                    "prompt_excerpt": f"draw {OLD_SECRET}",
                    "result_url": "",
                    "status": "failed",
                }
            ],
        }
    )
    plugin, context, _store = make_plugin(store)
    started = await plugin.startup()
    try:
        panel = await plugin.get_panel_state()
        recent = await plugin.get_recent_history()

        assert started.is_ok()
        assert panel.is_ok()
        assert recent.is_ok()
        assert panel.value["settings"]["model"] == DEFAULT_SETTINGS["model"]
        assert OLD_SECRET not in serialized(
            started.value,
            panel.value,
            recent.value,
            context.logger.rendered(),
            context.status_updates,
        )
    finally:
        await plugin.shutdown()


def test_image_decoder_verifies_real_structure_and_rejects_adversarial_pngs() -> None:
    real_png = real_png_bytes(size=(11, 9))
    decoded, mime, extension = image_generator_module._decode_b64_image(
        base64.b64encode(real_png).decode(),
        max_bytes=1_000_000,
    )
    assert mime == "image/png"
    assert extension == "png"
    with Image.open(io.BytesIO(decoded)) as image:
        image.verify()
    with Image.open(io.BytesIO(decoded)) as image:
        assert image.size == (11, 9)
        assert image.format == "PNG"

    # The frozen host cannot rely on Pillow, so decoding uses a pure-Python
    # container sniffer that only inspects the header (PNG signature + IHDR).
    # A payload truncated *after* a valid header therefore passes the sniffer;
    # integrity is the provider's transport concern, not something we can
    # re-verify without a full decoder. Assert the sniffer's real contract:
    # valid header → accepted; truncated-below-header → rejected.
    header_truncated = real_png[:20]  # signature + partial IHDR
    with pytest.raises(image_generator_module._GenerationFailure) as truncated_error:
        image_generator_module._decode_b64_image(
            base64.b64encode(header_truncated).decode(),
            max_bytes=1_000_000,
        )
    assert truncated_error.value.failure_class == "InvalidImageData"

    with pytest.raises(image_generator_module._GenerationFailure) as bomb_error:
        image_generator_module._decode_b64_image(
            base64.b64encode(png_pixel_bomb()).decode(),
            max_bytes=1_000_000,
        )
    assert bomb_error.value.failure_class in {
        "InvalidImageData",
        "ImagePixelLimit",
    }
    assert "尺寸" in bomb_error.value.message or "像素" in bomb_error.value.message


@pytest.mark.asyncio
async def test_real_http_provider_cached_static_asset_and_markdown_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = real_png_bytes(size=(13, 8))
    provider_payloads = [
        {
            "data": [
                {
                    "b64_json": base64.b64encode(png).decode(),
                    "revised_prompt": "real local provider",
                }
            ]
        }
    ]
    provider_requests: list[dict[str, Any]] = []
    provider_get_count = 0

    class ProviderHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            provider_requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("authorization"),
                    "body": body,
                }
            )
            payload = provider_payloads.pop(0)
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            nonlocal provider_get_count
            provider_get_count += 1
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    with running_server(ProviderHandler) as provider:
        port = provider.server_address[1]
        provider_payloads.append(
            {"data": [{"url": (f"http://127.0.0.1:{port}/should-not-fetch.png")}]}
        )
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings["provider"] = "local_compatible"
        settings["api_base_url"] = f"http://127.0.0.1:{port}/v1"
        settings["model"] = "portable-image-model"
        store = MemoryStore({"settings": settings, "api_key": OLD_SECRET})
        plugin, context, _store = make_plugin(store)
        started = await plugin.startup()
        try:
            assert started.is_ok()
            generated = await plugin.generate_image(prompt="真实 HTTP 合同测试")
            assert generated.is_ok()

            assert len(provider_requests) == 1
            request = provider_requests[0]
            assert request["path"] == "/v1/images/generations"
            assert request["authorization"] == f"Bearer {OLD_SECRET}"
            assert request["body"]["response_format"] == "b64_json"

            # On successful push the result no longer echoes image_url /
            # display_markdown back to the model (that caused a duplicate
            # render). The canonical image URL now lives on the pushed image
            # part; pull it from there.
            assert len(context.pushed) == 1
            canonical = translate_push_message(**context.pushed[0])
            assert canonical["visibility"] == ["chat"]
            assert canonical["ai_behavior"] == "blind"
            assert len(canonical["parts"]) == 2
            text_part = canonical["parts"][0]
            assert text_part["type"] == "text"
            assert "![AI 生成图片]" in text_part["text"]
            image_part = canonical["parts"][1]
            assert image_part["type"] == "image"
            image_url = image_part["url"]
            parsed_image_url = urlparse(image_url)
            assert parsed_image_url.path.startswith(
                "/plugin/image_generator/ui/generated/"
            )
            assert re.fullmatch(
                r"[0-9a-f]{32}\.(?:png|jpg|webp)",
                parsed_image_url.path.rsplit("/", 1)[-1],
            )
            # The chat renderer needs real dimensions to size the frame via
            # aspect-ratio; the part must carry the sniffed geometry.
            assert image_part.get("width") == 13
            assert image_part.get("height") == 8
            # The model is told NOT to re-emit the image; no markdown leaks.
            assert "display_markdown" not in generated.value
            assert "image_url" not in generated.value
            static_config = plugin.get_static_ui_config()
            assert static_config is not None
            static_root = Path(static_config["directory"])

            async def registered_static_dir(plugin_id: str):
                assert plugin_id == "image_generator"
                return static_root

            async def registered_static_config(plugin_id: str):
                assert plugin_id == "image_generator"
                return static_config

            monkeypatch.setattr(
                plugin_ui_route_module,
                "_get_plugin_static_dir",
                registered_static_dir,
            )
            monkeypatch.setattr(
                plugin_ui_route_module,
                "_get_static_ui_config",
                registered_static_config,
            )
            app = FastAPI()
            app.include_router(plugin_ui_route_module.router)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                served = await client.get(parsed_image_url.path)
            assert served.status_code == 200
            assert served.headers["content-type"].startswith("image/png")
            assert served.headers["cache-control"] == "no-cache"
            with Image.open(io.BytesIO(served.content)) as image:
                image.verify()
            with Image.open(io.BytesIO(served.content)) as image:
                assert image.size == (13, 8)

            url_only = await plugin.generate_image(prompt="拒绝远程 URL 响应")
            assert url_only.is_err()
            assert provider_get_count == 0
            assert len(context.pushed) == 1
            assert OLD_SECRET not in context.logger.rendered()
        finally:
            await plugin.shutdown()


@pytest.mark.asyncio
async def test_llm_callback_registration_uses_dual_entry_metadata() -> None:
    plugin, _context, _store = make_plugin()
    tools = plugin.list_llm_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "generate_image"
    assert tools[0]["parameters"]["required"] == ["prompt"]
    assert "画一张" in tools[0]["description"]

    callback_id = entry_id_for_tool("generate_image")
    entries = plugin.collect_entries()
    assert callback_id in entries
    callback = entries[callback_id]
    assert callback.meta.input_schema == tools[0]["parameters"]
    assert callback.meta.timeout == 300.0

    async def fake_generate(**kwargs: Any):
        assert kwargs["prompt"] == "LLM callback contract"
        return Ok(
            {
                "message": "generated",
                "image_url": "http://127.0.0.1:48916/plugin/"
                "image_generator/ui/generated/"
                f"{'a' * 32}.png",
                "display_markdown": "![AI 生成图片](safe)",
                "display_instruction": "display_markdown",
                "revised_prompt": "",
            }
        )

    plugin._generate = fake_generate  # type: ignore[method-assign]
    result = await callback.handler(prompt="LLM callback contract")
    assert result.is_ok()
    assert result.value["display_instruction"] == "display_markdown"


def test_test_generation_is_hidden_from_agent_routing_but_generate_image_is_not() -> None:
    """The panel's test entry must not be auto-routable by the Agent.

    Regression: the host TaskExecutor LLM was picking ``test_generation`` for
    "draw X" requests, which runs with ``auto_show_override=False`` — the image
    was written to history but never pushed into chat, surfacing as "猫娘画了
    但没发出来". The fix hides this entry via ``metadata={"agent_hidden": True}``;
    the host's ``_is_plugin_entry_agent_hidden`` reads exactly that key.
    """
    import sys

    task_executor = sys.modules.get("brain.task_executor")
    plugin, _context, _store = make_plugin()
    entries = plugin.collect_entries()

    test_entry = entries.get("test_generation")
    assert test_entry is not None, "test_generation entry must still exist for the panel"
    # The metadata must survive collect_entries so the host filter can read it.
    assert test_entry.meta.metadata.get("agent_hidden") is True

    # The LLM-facing generate_image plugin_entry must NOT be hidden — it is
    # the TaskExecutor-reachable path now that test_generation is hidden.
    gen_entry = entries.get("generate_image")
    assert gen_entry is not None
    assert gen_entry.meta.metadata.get("agent_hidden") is not True

    # If the host module is importable, prove the real filter agrees.
    if task_executor is not None and hasattr(task_executor, "DirectTaskExecutor"):
        hidden = task_executor.DirectTaskExecutor._is_plugin_entry_agent_hidden
        assert hidden({"id": "test_generation", "metadata": {"agent_hidden": True}}) is True
        assert hidden({"id": "generate_image", "metadata": {}}) is False


def test_public_cleartext_api_base_is_rejected_but_loopback_http_is_allowed() -> None:
    with pytest.raises(SdkError):
        image_generator_module._normalize_api_base_url(
            "http://public-provider.example/v1"
        )
    assert (
        image_generator_module._normalize_api_base_url("http://127.0.0.1:8080/v1/")
        == "http://127.0.0.1:8080/v1"
    )
    assert (
        image_generator_module._normalize_api_base_url("http://[::1]:8080/v1")
        == "http://[::1]:8080/v1"
    )
