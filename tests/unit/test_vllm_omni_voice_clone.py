# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""vLLM-Omni voiceclone enrollment + dispatch (dual to mimo).

vLLM-Omni has no remote cloned voice id: the reference sample is persisted
locally (clone_sample_b64) and inlined per synthesis request via
``session.config.ref_audio`` against the WebSocket stream endpoint. Unlike MiMo
it is a self-hosted local service with no API key, so the clone bucket is the
fixed ``__VLLM_OMNI__`` partition and the api_key override is an empty string.
"""

import base64
import io
import json
import queue
import threading
import time
from functools import partial
from types import SimpleNamespace

import numpy as np
import pytest

from main_logic import tts_client
from utils.config_manager import get_config_manager


class ControlledQueue:
    def __init__(self):
        self._queue = queue.Queue()
        self._stop = object()

    def put(self, item):
        self._queue.put(item)

    def get(self, timeout=None):
        item = self._queue.get(timeout=timeout)
        if item is self._stop:
            raise EOFError("queue closed")
        return item

    def close(self):
        self._queue.put(self._stop)


def _wait_for_queue_item(q, predicate, timeout=5.0):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        remaining = max(0.01, deadline - time.time())
        try:
            item = q.get(timeout=remaining)
        except queue.Empty:
            continue
        seen.append(item)
        if predicate(item):
            return item, seen
    raise AssertionError(f"Timed out waiting for queue item, seen={seen!r}")


# ── fake WebSocket: records sent JSON frames, emits one PCM frame then done ───

class _FakeWS:
    """Minimal stand-in for a websockets connection used by the worker.

    Records every JSON frame the worker sends (so the test can assert the
    session.config carries ref_audio/ref_text), and yields one binary PCM frame
    followed by a ``session.done`` event after ``input.done`` is received.
    """

    def __init__(self):
        self.sent = []
        self._incoming = queue.Queue()
        self.closed = False

    async def send(self, message):
        self.sent.append(message)
        try:
            data = json.loads(message)
        except Exception:
            data = {}
        if data.get("type") == "input.done":
            self._incoming.put((np.arange(1600, dtype=np.int16)).tobytes())
            self._incoming.put(json.dumps({"type": "session.done", "total_sentences": 1}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio
        loop = asyncio.get_running_loop()
        try:
            item = await loop.run_in_executor(None, self._get_blocking)
        except queue.Empty:
            raise StopAsyncIteration
        return item

    def _get_blocking(self):
        return self._incoming.get(timeout=5.0)

    async def close(self):
        self.closed = True


def _start_worker(monkeypatch, **kwargs):
    fake_ws = _FakeWS()

    async def fake_connect(*args, **kw):
        return fake_ws

    import main_logic.tts_client.workers.vllm_omni as vllm_mod
    monkeypatch.setattr(vllm_mod.websockets, "connect", fake_connect)

    request_queue = ControlledQueue()
    response_queue = queue.Queue()
    base_kwargs = {
        "request_queue": request_queue,
        "response_queue": response_queue,
        "audio_api_key": "",
        "voice_id": "default",
        "base_url": "ws://127.0.0.1:8091/v1",
        "model": "Qwen3-TTS",
        "voice": "default",
    }
    base_kwargs.update(kwargs)
    thread = threading.Thread(
        target=tts_client.vllm_omni_tts_worker, kwargs=base_kwargs, daemon=True,
    )
    thread.start()
    return fake_ws, request_queue, response_queue, thread


def _config_frame(fake_ws):
    for m in fake_ws.sent:
        try:
            data = json.loads(m)
        except Exception:
            continue
        if data.get("type") == "session.config":
            return data
    raise AssertionError(f"no session.config frame sent, got={fake_ws.sent!r}")


# ── worker: clone inlines ref_audio/ref_text into session.config ──────────────

@pytest.mark.unit
def test_vllm_omni_worker_clone_inlines_ref_audio_and_ref_text(monkeypatch):
    clone_uri = "data:audio/wav;base64,QUJDRA=="
    ref_text = "参考音频对应的原文"
    fake_ws, request_queue, response_queue, thread = _start_worker(
        monkeypatch, voice_id="default", ref_audio=clone_uri, ref_text=ref_text,
    )

    _wait_for_queue_item(response_queue, lambda item: item == ("__ready__", True))
    request_queue.put(("speech-1", "你好。"))
    request_queue.put((None, None))
    _wait_for_queue_item(response_queue, lambda item: isinstance(item, bytes))

    cfg = _config_frame(fake_ws)
    # ⚠ 字段名严格 ref_audio/ref_text，vllm-omni 用旧名 prompt_audio/prompt_text 会 500
    assert cfg["ref_audio"] == clone_uri
    assert cfg["ref_text"] == ref_text
    assert "prompt_audio" not in cfg
    assert "prompt_text" not in cfg

    request_queue.close()
    thread.join(timeout=6.0)
    assert not thread.is_alive(), "vllm_omni worker thread did not exit in time"


@pytest.mark.unit
def test_vllm_omni_worker_preset_omits_ref_fields(monkeypatch):
    """Preset path (no ref_audio) must NOT inject ref_audio/ref_text — backward compat."""
    fake_ws, request_queue, response_queue, thread = _start_worker(
        monkeypatch, voice_id="cyrene", voice="cyrene",
    )
    _wait_for_queue_item(response_queue, lambda item: item == ("__ready__", True))
    request_queue.put(("speech-1", "你好。"))
    request_queue.put((None, None))
    _wait_for_queue_item(response_queue, lambda item: isinstance(item, bytes))

    cfg = _config_frame(fake_ws)
    assert "ref_audio" not in cfg
    assert "ref_text" not in cfg

    request_queue.close()
    thread.join(timeout=6.0)
    assert not thread.is_alive(), "vllm_omni worker thread did not exit in time"


@pytest.mark.unit
def test_vllm_omni_worker_clone_id_without_ref_audio_falls_back(monkeypatch):
    """When voice_id looks like a clone ID but ref_audio is empty, voice must
    fall back to 'default' instead of leaking the clone ID to the server.

    Bug-fix guard: if resolve fails to pass ref_audio (e.g. stale voice_meta
    cache), the worker must not send the clone ID (e.g. vllm-omni-clone-ch-xxx)
    as the voice parameter — the server would reject it as Invalid Voice or
    crash with ValueError.
    """
    fake_ws, request_queue, response_queue, thread = _start_worker(
        monkeypatch, voice_id="vllm-omni-clone-ch-91a8a87dc029", voice="default",
        ref_audio="", ref_text="你好",
    )
    _wait_for_queue_item(response_queue, lambda item: item == ("__ready__", True))
    request_queue.put(("speech-1", "你好。"))
    request_queue.put((None, None))
    _wait_for_queue_item(response_queue, lambda item: isinstance(item, bytes))

    cfg = _config_frame(fake_ws)
    # voice 绝不能是克隆 ID，必须回退到 default
    assert cfg["voice"] == "default"
    # ref_audio 为空时 ref_text 不应单独发给服务端（服务端会 ValueError 崩溃）
    assert "ref_audio" not in cfg
    assert "ref_text" not in cfg

    request_queue.close()
    thread.join(timeout=6.0)
    assert not thread.is_alive(), "vllm_omni worker thread did not exit in time"


# ── dispatch: a vLLM-Omni clone voice routes to the vllm_omni worker ──────────

def _vllm_clone_meta(sample: bytes, **extra):
    """A vLLM-Omni clone voice_meta (B-storage model: reference sample base64 in
    voice_storage, dual to MiMo)."""
    meta = {
        "provider": "vllm_omni",
        "source": "clone",
        "clone_sample_b64": base64.b64encode(sample).decode("ascii"),
        "clone_sample_mime": "audio/wav",
        "clone_ref_text": "参考音频对应的原文",
    }
    meta.update(extra)
    return meta


class _CMBase:
    """A config_manager stand-in for dispatch tests."""

    def __init__(self, voices, raw_json=None, core_config=None):
        self._voices = voices
        self._raw = raw_json or {}
        self._core_config = {
            "assistApi": "qwen", "TTS_PROVIDER": "", "GPTSOVITS_ENABLED": False,
        }
        self._core_config.update(core_config or {})

    def get_core_config(self):
        return dict(self._core_config)

    def get_model_api_config(self, model_type):
        return {"is_custom": False}

    def get_tts_api_key(self, provider):
        return ""

    def get_voices_for_current_api(self, for_listing=False):
        return self._voices

    def load_json_config(self, name, default=None):
        return dict(self._raw)


@pytest.mark.unit
def test_get_tts_worker_routes_vllm_omni_clone_voice(monkeypatch):
    sample = (np.arange(256, dtype=np.int16)).tobytes()
    cm = _CMBase({"vllm-omni-clone-ch-abc": _vllm_clone_meta(sample)})
    monkeypatch.setattr(tts_client, "get_config_manager", lambda: cm)

    worker, api_key, provider_key = tts_client.get_tts_worker(
        core_api_type="qwen", has_custom_voice=True, voice_id="vllm-omni-clone-ch-abc",
    )

    assert isinstance(worker, partial)
    assert worker.func is tts_client.vllm_omni_tts_worker
    assert provider_key == "vllm_omni"
    # 本地服务无 key：api_key override 为空串（禁止 fallback 别家 key）
    assert api_key == ""
    expected_uri = "data:audio/wav;base64," + base64.b64encode(sample).decode("ascii")
    assert worker.keywords["ref_audio"] == expected_uri
    assert worker.keywords["ref_text"] == "参考音频对应的原文"


@pytest.mark.unit
def test_get_tts_worker_vllm_omni_clone_missing_sample_falls_back_to_dummy(monkeypatch):
    cm = _CMBase({"vllm-omni-clone-x": {"provider": "vllm_omni", "source": "clone"}})  # no sample
    monkeypatch.setattr(tts_client, "get_config_manager", lambda: cm)

    worker, api_key, provider_key = tts_client.get_tts_worker(
        core_api_type="qwen", has_custom_voice=True, voice_id="vllm-omni-clone-x",
    )
    assert worker is tts_client.dummy_tts_worker
    assert provider_key is None


@pytest.mark.unit
def test_get_tts_worker_vllm_omni_clone_uses_stored_base_url(monkeypatch):
    sample = (np.arange(64, dtype=np.int16)).tobytes()
    cm = _CMBase({
        "vllm-omni-clone-s": _vllm_clone_meta(sample, vllm_omni_base_url="ws://10.0.1.92:8091/v1"),
    })
    monkeypatch.setattr(tts_client, "get_config_manager", lambda: cm)
    worker, _, provider_key = tts_client.get_tts_worker(
        core_api_type="qwen", has_custom_voice=True, voice_id="vllm-omni-clone-s",
    )
    assert provider_key == "vllm_omni"
    assert worker.keywords["base_url"] == "ws://10.0.1.92:8091/v1"


@pytest.mark.unit
def test_get_tts_worker_vllm_omni_clone_uses_current_vllm_url_over_legacy_snapshot(monkeypatch):
    """Switching to vLLM-Omni must repair clones saved while TTS followed another API."""
    sample = (np.arange(64, dtype=np.int16)).tobytes()
    cm = _CMBase(
        {
            "vllm-omni-clone-s": _vllm_clone_meta(
                sample, vllm_omni_base_url="https://www.lanlan.tech/text/v1"
            ),
        },
        raw_json={
            "enableCustomApi": True,
            "ttsModelProvider": "vllm_omni",
            "ttsModelUrl": "ws://127.0.0.1:8091/v1",
        },
        core_config={"ENABLE_CUSTOM_API": True, "ttsModelProvider": "vllm_omni"},
    )
    monkeypatch.setattr(tts_client, "get_config_manager", lambda: cm)

    worker, _, provider_key = tts_client.get_tts_worker(
        core_api_type="qwen", has_custom_voice=True, voice_id="vllm-omni-clone-s",
    )

    assert provider_key == "vllm_omni"
    assert worker.keywords["base_url"] == "ws://127.0.0.1:8091/v1"


@pytest.mark.unit
def test_get_tts_worker_vllm_omni_clone_uses_default_over_legacy_snapshot_when_selected(monkeypatch):
    """An empty selected vLLM URL means its supported localhost default."""
    sample = (np.arange(64, dtype=np.int16)).tobytes()
    cm = _CMBase(
        {
            "vllm-omni-clone-s": _vllm_clone_meta(
                sample, vllm_omni_base_url="https://www.lanlan.tech/text/v1"
            ),
        },
        raw_json={
            "enableCustomApi": True,
            "ttsModelProvider": "vllm_omni",
            "ttsModelUrl": "",
        },
        core_config={"ENABLE_CUSTOM_API": True, "ttsModelProvider": "vllm_omni"},
    )
    monkeypatch.setattr(tts_client, "get_config_manager", lambda: cm)

    worker, _, provider_key = tts_client.get_tts_worker(
        core_api_type="qwen", has_custom_voice=True, voice_id="vllm-omni-clone-s",
    )

    assert provider_key == "vllm_omni"
    assert worker.keywords["base_url"] == "ws://localhost:8091/v1"


@pytest.mark.unit
def test_get_tts_worker_vllm_omni_clone_uses_selected_tts_model_url_alias(monkeypatch):
    """The legacy TTS_MODEL_URL alias remains an active vLLM endpoint."""
    sample = (np.arange(64, dtype=np.int16)).tobytes()
    cm = _CMBase(
        {
            "vllm-omni-clone-s": _vllm_clone_meta(
                sample, vllm_omni_base_url="https://www.lanlan.tech/text/v1"
            ),
        },
        raw_json={
            "enableCustomApi": True,
            "ttsModelProvider": "vllm_omni",
            "ttsModelUrl": "",
            "TTS_MODEL_URL": "ws://127.0.0.1:8099/v1",
        },
        core_config={"ENABLE_CUSTOM_API": True, "ttsModelProvider": "vllm_omni"},
    )
    monkeypatch.setattr(tts_client, "get_config_manager", lambda: cm)

    worker, _, provider_key = tts_client.get_tts_worker(
        core_api_type="qwen", has_custom_voice=True, voice_id="vllm-omni-clone-s",
    )

    assert provider_key == "vllm_omni"
    assert worker.keywords["base_url"] == "ws://127.0.0.1:8099/v1"


@pytest.mark.unit
async def test_vllm_omni_clone_bypasses_active_local_tts_registration(monkeypatch):
    """A selected vLLM inline clone must never call another local provider's register API."""
    from main_routers.characters_router import voice_cloning as voice_cloning_router

    saved = {}

    class _ConfigManager:
        async def aget_core_config(self):
            return {"ENABLE_CUSTOM_API": True, "ttsModelProvider": "follow_assist"}

        async def aget_model_api_config(self, _model_type, core_config=None):
            return {}

        def get_tts_api_key(self, _provider):
            return ""

        def find_voice_by_audio_md5(self, *_args):
            return None

        def save_voice_for_api_key(self, storage_key, voice_id, voice_data):
            saved.update(storage_key=storage_key, voice_id=voice_id, voice_data=voice_data)

    async def fake_read_limited_stream(_file, _limit):
        return io.BytesIO(b"reference audio")

    def fake_normalize(_buffer, _filename):
        return io.BytesIO(b"normalized audio"), "reference.wav", {
            "original": {"sample_rate": 16000, "channels": 1},
            "normalized": {"sample_rate": 16000},
        }

    def reject_local_registration(*_args, **_kwargs):
        raise AssertionError("vLLM-Omni clones must not call /v1/speakers/register")

    monkeypatch.setattr(voice_cloning_router, "get_config_manager", lambda: _ConfigManager())
    monkeypatch.setattr(voice_cloning_router, "_read_limited_stream", fake_read_limited_stream)
    monkeypatch.setattr(voice_cloning_router, "normalize_voice_clone_api_audio", fake_normalize)
    monkeypatch.setattr(voice_cloning_router, "_is_local_voice_clone_tts_config", lambda *_args: True)
    monkeypatch.setattr(voice_cloning_router.httpx, "AsyncClient", reject_local_registration)

    response = await voice_cloning_router.voice_clone(
        file=SimpleNamespace(filename="reference.wav"),
        prefix="sample",
        ref_language="ch",
        provider="vllm_omni",
        ref_text="参考音频原文",
    )

    assert response.status_code == 200
    assert saved["storage_key"] == "__VLLM_OMNI__"
    assert saved["voice_data"]["clone_ref_text"] == "参考音频原文"


# ── registry: vllm_omni advertises both preset + clone capabilities ───────────

@pytest.mark.unit
def test_registry_declares_clone_and_preset_for_vllm_omni():
    import utils.tts_provider_registry as reg
    vllm = reg.get("vllm_omni")
    assert vllm is not None
    assert "clone" in vllm.capabilities and "preset" in vllm.capabilities
    meta = {m["key"]: m for m in reg.ui_metadata()}
    assert "clone" in meta["vllm_omni"]["capabilities"]
    assert "preset" in meta["vllm_omni"]["capabilities"]


# ── config_manager: __VLLM_OMNI__ bucket merges into the current-API voice list ─

@pytest.mark.unit
def test_get_voices_merges_vllm_omni_bucket(monkeypatch):
    cm = get_config_manager()
    monkeypatch.setattr(cm, "load_voice_storage", lambda: {
        "__VLLM_OMNI__": {"vllm-omni-clone-x": {"source": "clone"}}  # provider stamped by merge
    })
    monkeypatch.setattr(cm, "get_model_api_config", lambda t: {})
    monkeypatch.setattr(cm, "get_core_config", lambda: {})
    monkeypatch.setattr(cm, "_is_local_tts_storage_active", lambda *a, **k: False)
    monkeypatch.setattr(cm, "is_free_voice", lambda: False)
    monkeypatch.setattr(cm, "_get_cosyvoice_storage_keys", lambda *_a, **_k: [])
    monkeypatch.setattr(cm, "_get_minimax_storage_keys", lambda: [])
    monkeypatch.setattr(cm, "_get_elevenlabs_storage_keys", lambda: [])
    monkeypatch.setattr(cm, "_get_mimo_storage_keys", lambda: [])

    voices = cm.get_voices_for_current_api()
    assert "vllm-omni-clone-x" in voices
    assert voices["vllm-omni-clone-x"]["provider"] == "vllm_omni"


@pytest.mark.unit
def test_get_voices_strips_vllm_omni_sample_b64_for_listing(monkeypatch):
    """dispatch (for_listing=False) needs the sample base64; the UI list
    (for_listing=True) must not ship the MB-sized blob to the frontend."""
    cm = get_config_manager()
    monkeypatch.setattr(cm, "load_voice_storage", lambda: {
        "__VLLM_OMNI__": {"vllm-omni-clone-x": {
            "source": "clone", "clone_sample_b64": "QUJDRA==",
            "clone_sample_mime": "audio/wav", "clone_ref_text": "原文",
        }}
    })
    monkeypatch.setattr(cm, "get_model_api_config", lambda t: {})
    monkeypatch.setattr(cm, "get_core_config", lambda: {})
    monkeypatch.setattr(cm, "_is_local_tts_storage_active", lambda *a, **k: False)
    monkeypatch.setattr(cm, "is_free_voice", lambda: False)
    monkeypatch.setattr(cm, "_get_cosyvoice_storage_keys", lambda *_a, **_k: [])
    monkeypatch.setattr(cm, "_get_minimax_storage_keys", lambda: [])
    monkeypatch.setattr(cm, "_get_elevenlabs_storage_keys", lambda: [])
    monkeypatch.setattr(cm, "_get_mimo_storage_keys", lambda: [])

    full = cm.get_voices_for_current_api(for_listing=False)
    assert full["vllm-omni-clone-x"]["clone_sample_b64"] == "QUJDRA=="

    listing = cm.get_voices_for_current_api(for_listing=True)
    assert "clone_sample_b64" not in listing["vllm-omni-clone-x"]
    assert listing["vllm-omni-clone-x"]["provider"] == "vllm_omni"
    assert listing["vllm-omni-clone-x"]["clone_ref_text"] == "原文"


@pytest.mark.unit
def test_infer_provider_from_vllm_omni_storage_key():
    cm = get_config_manager()
    assert cm._infer_provider_from_storage_key("__VLLM_OMNI__") == "vllm_omni"


# ── preview: vLLM-Omni clone preview via WebSocket ────────────────────────────

class _FakePreviewWS:
    """Fake WebSocket for preview endpoint tests.

    Simulates a vLLM-Omni server that receives session.config + input.done,
    then returns PCM frames and session.done.
    """

    def __init__(self, frames=None, events=None):
        self.frames = frames if frames is not None else [(np.arange(1600, dtype=np.int16)).tobytes()]
        self.events = events if events is not None else [{"type": "session.done", "total_sentences": 1}]
        self._iter = iter(self.frames + [json.dumps(e) for e in self.events])
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        self.closed = True


class _FakeWSAsyncCtx:
    """Async context manager that yields a fake WebSocket, mimicking
    ``async with websockets.connect(...) as ws:``."""

    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        pass


def _fake_ws_connect(ws):
    """Return a callable that mimics websockets.connect, yielding the given fake WS."""
    def _connect(*args, **kwargs):
        return _FakeWSAsyncCtx(ws)
    return _connect


def _make_config_manager_for_preview(voice_id, voice_data, core_config=None):
    """Build a mock ConfigManager for preview tests."""
    class _CM:
        def get_voices_for_current_api(self, for_listing=False):
            return {voice_id: voice_data}

        def get_model_api_config(self, model_type):
            return {"api_key": "", "is_custom": False}

        def get_core_config(self):
            return core_config or {"ttsModelUrl": "ws://localhost:8091/v1"}

        async def aget_core_config(self):
            return self.get_core_config()

        def get_tts_api_key(self, provider):
            return ""

    return _CM()


@pytest.mark.unit
def test_vllm_omni_preview_success(monkeypatch):
    """Preview a vLLM-Omni clone voice: WebSocket receives PCM → returns WAV audio.

    Uses nest_asyncio to allow nested event-loop execution under pytest-asyncio.
    """
    import asyncio
    import nest_asyncio
    nest_asyncio.apply()
    import types as _types
    from main_routers.characters_router import get_voice_preview
    import main_routers.characters_router.voice_preview as _cr

    voice_id = "vllm-omni-clone-ch-abc123def456"
    sample_pcm = (np.arange(1600, dtype=np.int16)).tobytes()
    voice_data = {
        "provider": "vllm_omni",
        "source": "clone",
        "clone_sample_b64": base64.b64encode(sample_pcm).decode("ascii"),
        "clone_sample_mime": "audio/wav",
        "clone_ref_text": "测试原文",
        "vllm_omni_base_url": "ws://localhost:8091/v1",
    }
    cm = _make_config_manager_for_preview(voice_id, voice_data)

    fake_ws = _FakePreviewWS()
    _orig_ws = _cr.websockets
    fake_ws_mod = _types.ModuleType("websockets")
    fake_ws_mod.connect = lambda *a, **kw: _FakeWSAsyncCtx(fake_ws)
    fake_ws_mod.exceptions = _orig_ws.exceptions
    monkeypatch.setattr(_cr, "get_config_manager", lambda: cm)
    monkeypatch.setattr(_cr, "websockets", fake_ws_mod)

    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/preview",
                       "query_string": b"", "headers": [], "server": ("testserver", 80)})
    result = asyncio.get_event_loop().run_until_complete(
        get_voice_preview(request, voice_id=voice_id)
    )
    assert result["success"] is True
    assert result.get("audio"), "preview response should contain audio base64"

    # 验证 session.config 被正确发送
    config_msg = None
    for m in fake_ws.sent:
        try:
            data = json.loads(m)
            if data.get("type") == "session.config":
                config_msg = data
                break
        except (json.JSONDecodeError, TypeError):
            continue
    assert config_msg is not None, "session.config should be sent"
    assert config_msg["response_format"] == "pcm"
    assert "ref_audio" in config_msg
    assert config_msg.get("ref_text") == "测试原文"


@pytest.mark.unit
def test_vllm_omni_preview_server_error(monkeypatch):
    """Preview returns 502 when the vLLM-Omni server sends an error event."""
    import asyncio
    import nest_asyncio
    nest_asyncio.apply()
    import types as _types
    from main_routers.characters_router import get_voice_preview
    import main_routers.characters_router.voice_preview as _cr

    voice_id = "vllm-omni-clone-ch-err123"
    sample_pcm = (np.arange(1600, dtype=np.int16)).tobytes()
    voice_data = {
        "provider": "vllm_omni",
        "source": "clone",
        "clone_sample_b64": base64.b64encode(sample_pcm).decode("ascii"),
        "clone_sample_mime": "audio/wav",
        "clone_ref_text": "原文",
        "vllm_omni_base_url": "ws://localhost:8091/v1",
    }
    cm = _make_config_manager_for_preview(voice_id, voice_data)

    fake_ws = _FakePreviewWS(
        frames=[],
        events=[{"type": "error", "message": "synthesis failed"}],
    )
    _orig_ws = _cr.websockets
    fake_ws_mod = _types.ModuleType("websockets")
    fake_ws_mod.connect = lambda *a, **kw: _FakeWSAsyncCtx(fake_ws)
    fake_ws_mod.exceptions = _orig_ws.exceptions
    monkeypatch.setattr(_cr, "get_config_manager", lambda: cm)
    monkeypatch.setattr(_cr, "websockets", fake_ws_mod)

    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/preview",
                       "query_string": b"", "headers": [], "server": ("testserver", 80)})
    result = asyncio.get_event_loop().run_until_complete(
        get_voice_preview(request, voice_id=voice_id)
    )
    assert hasattr(result, 'status_code')
    assert result.status_code == 502
    body = json.loads(result.body)
    assert body["success"] is False
    assert "VLLM_OMNI_VOICE_PREVIEW_FAILED" in body.get("code", "")


@pytest.mark.unit
def test_vllm_omni_preview_no_audio_frames(monkeypatch):
    """Preview returns 502 when the server sends session.done without any PCM frames."""
    import asyncio
    import nest_asyncio
    nest_asyncio.apply()
    import types as _types
    from main_routers.characters_router import get_voice_preview
    import main_routers.characters_router.voice_preview as _cr

    voice_id = "vllm-omni-clone-ch-empty"
    sample_pcm = (np.arange(1600, dtype=np.int16)).tobytes()
    voice_data = {
        "provider": "vllm_omni",
        "source": "clone",
        "clone_sample_b64": base64.b64encode(sample_pcm).decode("ascii"),
        "clone_sample_mime": "audio/wav",
        "clone_ref_text": "原文",
        "vllm_omni_base_url": "ws://localhost:8091/v1",
    }
    cm = _make_config_manager_for_preview(voice_id, voice_data)

    # 服务端只发 session.done，无 PCM 帧
    fake_ws = _FakePreviewWS(frames=[], events=[{"type": "session.done", "total_sentences": 0}])
    _orig_ws = _cr.websockets
    fake_ws_mod = _types.ModuleType("websockets")
    fake_ws_mod.connect = lambda *a, **kw: _FakeWSAsyncCtx(fake_ws)
    fake_ws_mod.exceptions = _orig_ws.exceptions
    monkeypatch.setattr(_cr, "get_config_manager", lambda: cm)
    monkeypatch.setattr(_cr, "websockets", fake_ws_mod)

    from starlette.requests import Request
    request = Request({"type": "http", "method": "GET", "path": "/preview",
                       "query_string": b"", "headers": [], "server": ("testserver", 80)})
    result = asyncio.get_event_loop().run_until_complete(
        get_voice_preview(request, voice_id=voice_id)
    )
    assert hasattr(result, 'status_code')
    assert result.status_code == 502
    body = json.loads(result.body)
    assert body["success"] is False
    assert body.get("code") == "VLLM_OMNI_VOICE_PREVIEW_NO_AUDIO"
