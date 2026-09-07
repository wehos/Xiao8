from __future__ import annotations

import asyncio
import gc
import weakref
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main_logic.asr_client as asr_client
import main_logic.asr_client._infra as asr_infra
import main_logic.asr_client.runtime as asr_runtime_module
from main_logic.asr_client import AsrSessionConfig, create_asr_session
from main_logic.asr_client.admission.contracts import AdmissionDisposition
from main_logic.asr_client._infra import (
    _AsrRequestQueue,
    _AsrWorkerEvent,
    _AsrWorkerRequest,
    _RealtimeAsrSessionImpl,
)
from main_logic.asr_client._registry_meta import (
    ASR_PROVIDER_REGISTRY,
    CORE_ASR_ROUTES,
)
from main_logic.asr_client.workers.dummy import dummy_asr_worker
from main_logic.asr_client.lifecycle import (
    VoiceInputLifecycleController,
    VoiceLifecycleEvent,
    VoiceRouteMode,
)
from main_logic.asr_client.provider_policy import resolve_provider_policy
from main_logic.asr_client.runtime import (
    AsrRuntimeCallbacks,
    AsrStartStatus,
    IndependentAsrRuntime,
)
from main_logic.asr_client.transcript import TranscriptDispatcher
from main_logic.voice_turn.contracts import (
    SpeechActivityEvent,
    VoiceIngressToken,
    VoiceTurnToken,
)


async def _scripted_worker(request_queue, response_queue, api_key, config):
    del config
    await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
    while True:
        request = await request_queue.get()
        if request.kind == "commit":
            if api_key == "events":
                common = {
                    "generation": request.generation,
                    "buffer_epoch": request.buffer_epoch,
                    "utterance_id": request.utterance_id,
                }
                await response_queue.put(
                    _AsrWorkerEvent(kind="partial", text="draft", **common)
                )
                await response_queue.put(
                    _AsrWorkerEvent(kind="final", text=" first ", **common)
                )
                await response_queue.put(
                    _AsrWorkerEvent(kind="final", text="conflict", **common)
                )
            elif api_key == "error":
                await response_queue.put(
                    _AsrWorkerEvent(
                        kind="error",
                        generation=request.generation,
                        buffer_epoch=request.buffer_epoch,
                        utterance_id=request.utterance_id,
                        error_code="ASR_WORKER_FAILED",
                        error_message="provider rejected Authorization: Bearer sk-secret",
                    )
                )
            elif api_key == "provider":
                await response_queue.put(
                    _AsrWorkerEvent(
                        kind="final",
                        generation=request.generation,
                        buffer_epoch=request.buffer_epoch,
                        utterance_id=request.utterance_id,
                        text="unexpected commit",
                    )
                )
        elif request.kind == "shutdown":
            await response_queue.put(
                _AsrWorkerEvent(kind="closed", generation=request.generation)
            )
            return


async def _delayed_error_worker(request_queue, response_queue, api_key, config):
    del api_key, config
    pending = set()
    await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
    try:
        while True:
            request = await request_queue.get()
            if request.kind == "commit":

                async def emit_error(committed_request=request):
                    await asyncio.sleep(0.02)
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="error",
                            generation=committed_request.generation,
                            buffer_epoch=committed_request.buffer_epoch,
                            utterance_id=committed_request.utterance_id,
                            error_code="ASR_WORKER_FAILED",
                            error_message="stale utterance failure",
                        )
                    )

                task = asyncio.create_task(emit_error())
                pending.add(task)
                task.add_done_callback(pending.discard)
            elif request.kind == "shutdown":
                await response_queue.put(
                    _AsrWorkerEvent(kind="closed", generation=request.generation)
                )
                return
    finally:
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _non_consuming_worker(request_queue, response_queue, api_key, config):
    del request_queue, api_key, config
    await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
    await asyncio.Event().wait()


async def _wrong_generation_ready_worker(
    request_queue, response_queue, api_key, config
):
    del request_queue, api_key, config
    await response_queue.put(_AsrWorkerEvent(kind="ready", generation=99))
    await asyncio.Event().wait()


def test_public_exports_are_frozen():
    assert asr_client.__all__ == [
        "AsrCoreCapabilities",
        "AsrSessionConfig",
        "RealtimeAsrSession",
        "VoiceIdentityActivationResult",
        "create_asr_session",
        "get_asr_core_capabilities",
    ]
    assert not hasattr(asr_client, "get_asr_worker")
    assert not hasattr(asr_client, "AsrWorkerFn")
    assert not hasattr(asr_client, "ASR_PROVIDER_REGISTRY")
    assert not hasattr(asr_client, "CORE_ASR_ROUTES")
    assert not hasattr(asr_client, "dummy_asr_worker")


def test_routes_fail_synchronously_without_dummy(monkeypatch):
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    monkeypatch.setattr(asr_client, "_load_core_config", lambda: {})
    callback = AsyncMock()

    with pytest.raises(RuntimeError, match="ASR_UNKNOWN_CORE"):
        create_asr_session(
            "unknown",
            on_input_transcript=callback,
            on_connection_error=callback,
        )
    with pytest.raises(RuntimeError, match="ASR_CREDENTIALS_MISSING"):
        create_asr_session(
            "qwen",
            on_input_transcript=callback,
            on_connection_error=callback,
        )
    with pytest.raises(RuntimeError, match="ASR_BACKEND_BLOCKED"):
        create_asr_session(
            "free",
            on_input_transcript=callback,
            on_connection_error=callback,
        )


def test_dummy_requires_explicit_override_and_manual_mode(monkeypatch):
    callback = AsyncMock()
    monkeypatch.setenv("ASR_PROVIDER", "soniox")
    with pytest.raises(RuntimeError, match="ASR_INVALID_CONFIG"):
        create_asr_session(
            "qwen",
            on_input_transcript=callback,
            on_connection_error=callback,
        )
    with pytest.raises(TypeError, match="ASR_INVALID_CONFIG"):
        create_asr_session(
            "qwen",
            config={"endpointing_mode": "manual"},
            on_input_transcript=callback,
            on_connection_error=callback,
        )

    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    session = create_asr_session(
        "qwen",
        on_input_transcript=callback,
        on_connection_error=callback,
    )
    assert session is not None
    with pytest.raises(RuntimeError, match="ASR_ENDPOINTING_NOT_SUPPORTED"):
        create_asr_session(
            "qwen",
            config=AsrSessionConfig(endpointing_mode="provider"),
            on_input_transcript=callback,
            on_connection_error=callback,
        )


def test_phase2_registry_routes_and_capabilities():
    assert set(asr_client._IMPLEMENTED_WORKERS) == {
        "dummy",
        "qwen",
        "openai",
        "step",
        "grok",
        "glm",
        "gemini",
        "soniox",
    }
    assert CORE_ASR_ROUTES["qwen"].provider_key == "qwen"
    assert CORE_ASR_ROUTES["qwen"].credential_field == "ASSIST_API_KEY_QWEN"
    assert CORE_ASR_ROUTES["qwen"].region == "cn"
    assert CORE_ASR_ROUTES["qwen"].default_endpointing_mode == "provider"
    assert CORE_ASR_ROUTES["qwen_intl"].credential_field == ("ASSIST_API_KEY_QWEN_INTL")
    assert CORE_ASR_ROUTES["qwen_intl"].region == "intl"
    assert CORE_ASR_ROUTES["qwen_intl"].default_endpointing_mode == "provider"
    assert CORE_ASR_ROUTES["openai"].credential_field == "ASSIST_API_KEY_OPENAI"
    assert CORE_ASR_ROUTES["openai"].default_endpointing_mode == "provider"
    assert CORE_ASR_ROUTES["step"].credential_field == "ASSIST_API_KEY_STEP"
    assert CORE_ASR_ROUTES["step"].default_endpointing_mode == "provider"
    assert CORE_ASR_ROUTES["grok"].credential_field == "ASSIST_API_KEY_GROK"
    assert CORE_ASR_ROUTES["grok"].default_endpointing_mode == "provider"
    assert {
        core_key: route.capabilities.supports_independent_asr
        for core_key, route in CORE_ASR_ROUTES.items()
    } == {
        "qwen": True,
        "qwen_intl": True,
        "openai": True,
        "step": True,
        "grok": True,
        "glm": True,
        "gemini": True,
        "free": False,
    }
    assert asr_client.get_asr_core_capabilities(" FREE ") == (
        asr_client.AsrCoreCapabilities(supports_independent_asr=False)
    )
    assert asr_client.get_asr_core_capabilities("unknown") is None

    assert ASR_PROVIDER_REGISTRY["qwen"].supported_endpointing_modes == {
        "manual",
        "provider",
    }
    assert ASR_PROVIDER_REGISTRY["openai"].wire_sample_rate_hz == 24_000
    assert ASR_PROVIDER_REGISTRY["openai"].supported_endpointing_modes == {"provider"}
    assert ASR_PROVIDER_REGISTRY["grok"].supported_endpointing_modes == {"provider"}
    assert ASR_PROVIDER_REGISTRY["step"].supported_endpointing_modes == {"provider"}
    assert ASR_PROVIDER_REGISTRY["step"].implementation_status == "implemented"
    assert ASR_PROVIDER_REGISTRY["grok"].implementation_status == "implemented"
    assert ASR_PROVIDER_REGISTRY["openai"].implementation_status == "implemented"
    assert ASR_PROVIDER_REGISTRY["qwen"].implementation_status == "implemented"
    assert ASR_PROVIDER_REGISTRY["openai"].requires_smart_turn is False
    assert ASR_PROVIDER_REGISTRY["step"].requires_smart_turn is False
    assert ASR_PROVIDER_REGISTRY["qwen"].requires_smart_turn is False
    for provider_key in ("glm", "gemini"):
        meta = ASR_PROVIDER_REGISTRY[provider_key]
        assert meta.implementation_status == "implemented"
        assert meta.requires_smart_turn is True
    soniox_meta = ASR_PROVIDER_REGISTRY["soniox"]
    assert soniox_meta.implementation_status == "implemented"
    assert soniox_meta.supported_endpointing_modes == {"manual", "provider"}
    assert soniox_meta.requires_smart_turn is False


def test_phase3_selection_prefers_soniox_only_for_explicit_intl_region(
    monkeypatch,
):
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("ASR_USER_REGION", raising=False)
    monkeypatch.delenv("SONIOX_REGION", raising=False)
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: {"SONIOX_API_KEY": "configured", "SONIOX_REGION": "eu"},
        raising=False,
    )

    intl = asr_client._resolve_asr_selection("gemini", user_region="intl")
    unknown = asr_client._resolve_asr_selection("gemini", user_region="unknown")
    mainland = asr_client._resolve_asr_selection("gemini", user_region="cn")

    assert (intl.provider_key, intl.endpointing_mode, intl.soniox_region) == (
        "soniox",
        "provider",
        "eu",
    )
    assert unknown.provider_key == "gemini"
    assert mainland.provider_key == "gemini"


def test_phase3_selection_does_not_treat_key_as_region(monkeypatch):
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("ASR_USER_REGION", raising=False)
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: {"SONIOX_API_KEY": "configured"},
        raising=False,
    )

    selection = asr_client._resolve_asr_selection("qwen")

    assert selection.provider_key == "qwen"
    assert selection.endpointing_mode == "provider"


def test_openai_core_resolves_to_provider_endpointing_without_smart_turn(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("ASR_USER_REGION", raising=False)
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: {"ASSIST_API_KEY_OPENAI": "openai-key"},
        raising=False,
    )

    selection = asr_client._resolve_asr_selection("openai", user_region="cn")
    session = asr_client._create_asr_session_from_selection(
        "openai",
        selection=selection,
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )

    assert selection.provider_key == "openai"
    assert selection.endpointing_mode == "provider"
    assert session._voice_turn_factory is None


def test_step_core_resolves_to_provider_endpointing_without_smart_turn(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("ASR_USER_REGION", raising=False)
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: {"ASSIST_API_KEY_STEP": "step-key"},
        raising=False,
    )

    selection = asr_client._resolve_asr_selection("step", user_region="cn")
    session = asr_client._create_asr_session_from_selection(
        "step",
        selection=selection,
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )

    assert selection.provider_key == "step"
    assert selection.endpointing_mode == "provider"
    assert session._voice_turn_factory is None


def test_soniox_selection_captures_environment_credential_once(monkeypatch):
    reads: dict[str, int] = {}

    def fake_getenv(name, default=""):
        reads[name] = reads.get(name, 0) + 1
        if name == "SONIOX_API_KEY":
            return "soniox-env-key"
        if name == "SONIOX_REGION":
            return "us"
        return default

    monkeypatch.setattr(asr_client, "_load_core_config", lambda: {})
    monkeypatch.setattr(asr_client.os, "getenv", fake_getenv)

    selection = asr_client._resolve_asr_selection("gemini", user_region="intl")

    assert selection.provider_key == "soniox"
    assert selection.endpointing_mode == "provider"
    assert reads["SONIOX_API_KEY"] == 1
    assert "soniox-env-key" not in repr(selection)


def test_public_factory_resolves_once_and_builds_from_exact_selection(monkeypatch):
    callback = AsyncMock()
    selection = asr_client._AsrSelection(
        provider_key="dummy",
        endpointing_mode="manual",
    )
    resolver = MagicMock(return_value=selection)
    built_session = object()
    builder = MagicMock(return_value=built_session)

    monkeypatch.setattr(asr_client, "_resolve_asr_selection", resolver)
    monkeypatch.setattr(
        asr_client,
        "_create_asr_session_from_selection",
        builder,
        raising=False,
    )
    # Keep the legacy path constructible so the failure is specifically that
    # it bypasses the new builder, rather than an unrelated credential error.
    monkeypatch.setattr(
        asr_client,
        "_get_asr_worker",
        lambda *_args, **_kwargs: (dummy_asr_worker, "", "dummy"),
    )

    session = create_asr_session(
        "qwen",
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert session is built_session
    resolver.assert_called_once_with("qwen", user_region=None)
    assert builder.call_args.kwargs["selection"] is selection


def test_builder_uses_resolved_snapshot_without_rereading_routing_config(
    monkeypatch,
):
    callback = AsyncMock()
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("ASR_USER_REGION", raising=False)
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: {"ASSIST_API_KEY_QWEN": "snapshot-key"},
    )
    selection = asr_client._resolve_asr_selection("qwen")

    assert "snapshot-key" not in repr(selection)
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: (_ for _ in ()).throw(AssertionError("config reread")),
    )
    monkeypatch.setattr(
        asr_client.os,
        "getenv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("environment reread")
        ),
    )

    session = asr_client._create_asr_session_from_selection(
        "qwen",
        selection=selection,
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert session._api_key == "snapshot-key"
    assert session._config.endpointing_mode == "provider"
    assert session._voice_turn_factory is None


@pytest.mark.parametrize(
    ("provider_key", "user_language", "expected"),
    [
        ("qwen", "zh-CN", "zh-CN"),
        ("qwen", "ja", "ja"),
        ("qwen", "AUTO", "auto"),
        ("qwen", None, "auto"),
        ("qwen", "   ", "auto"),
        ("qwen", "not a language!!", "auto"),
        # Valid BCP-47 shape but outside the Qwen matrix must not fail the
        # session; it degrades to provider-side detection.
        ("qwen", "tlh", "auto"),
        ("openai", "zh-CN", "zh-CN"),
        ("openai", "en-US", "en-US"),
        ("openai", "ja", "auto"),
        ("step", "en", "en"),
        ("step", "ja", "auto"),
        ("grok", "zh", "zh"),
        ("grok", "ko", "auto"),
        ("soniox", "ja", "ja"),
        ("gemini", "ko", "ko"),
        ("dummy", "ja", "ja"),
    ],
)
def test_session_language_resolves_against_provider_matrix(
    provider_key,
    user_language,
    expected,
):
    resolved = asr_client._resolve_session_language(provider_key, user_language)

    assert resolved == expected


def test_builder_maps_user_language_onto_session_config(monkeypatch):
    callback = AsyncMock()
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.delenv("ASR_USER_REGION", raising=False)
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: {"ASSIST_API_KEY_OPENAI": "openai-key"},
    )
    selection = asr_client._resolve_asr_selection("openai")

    supported = asr_client._create_asr_session_from_selection(
        "openai",
        selection=selection,
        on_input_transcript=callback,
        on_connection_error=callback,
        user_language="zh-CN",
    )
    unsupported = asr_client._create_asr_session_from_selection(
        "openai",
        selection=selection,
        on_input_transcript=callback,
        on_connection_error=callback,
        user_language="ja",
    )
    unset = asr_client._create_asr_session_from_selection(
        "openai",
        selection=selection,
        on_input_transcript=callback,
        on_connection_error=callback,
    )
    explicit = asr_client._create_asr_session_from_selection(
        "openai",
        selection=selection,
        config=AsrSessionConfig(language="en", endpointing_mode="provider"),
        on_input_transcript=callback,
        on_connection_error=callback,
        user_language="ja",
    )

    assert supported._config.language == "zh-CN"
    assert unsupported._config.language == "auto"
    assert unset._config.language == "auto"
    # An explicit config always wins over the user-language hint.
    assert explicit._config.language == "en"


def test_public_factory_forwards_user_language(monkeypatch):
    callback = AsyncMock()
    monkeypatch.setenv("ASR_PROVIDER", "dummy")

    session = create_asr_session(
        "qwen",
        on_input_transcript=callback,
        on_connection_error=callback,
        user_language="ja",
    )

    assert session._config.language == "ja"


@pytest.mark.parametrize(
    ("provider_key", "endpointing_mode", "requires_smart_turn"),
    [
        ("dummy", "manual", True),
        ("qwen", "manual", True),
        ("qwen", "provider", False),
        ("openai", "provider", False),
        ("step", "provider", False),
        ("grok", "provider", False),
        ("glm", "manual", True),
        ("gemini", "manual", True),
        ("soniox", "provider", False),
        ("soniox", "manual", True),
    ],
)
def test_builder_selects_endpoint_runtime_from_provider_mode(
    provider_key,
    endpointing_mode,
    requires_smart_turn,
):
    callback = AsyncMock()
    selection = asr_client._AsrSelection(
        provider_key=provider_key,
        endpointing_mode=endpointing_mode,
        _worker_fn=dummy_asr_worker,
        _api_key="" if provider_key == "dummy" else "test-key",
    )

    session = asr_client._create_asr_session_from_selection(
        "qwen",
        selection=selection,
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert (session._voice_turn_factory is not None) is requires_smart_turn


@pytest.mark.parametrize("provider_key", ["dummy", "qwen", "glm", "gemini"])
def test_builder_marks_policy_required_smart_turn_as_strict(provider_key):
    callback = AsyncMock()
    selection = asr_client._AsrSelection(
        provider_key=provider_key,
        endpointing_mode="manual",
        _worker_fn=dummy_asr_worker,
        _api_key="" if provider_key == "dummy" else "test-key",
    )

    session = asr_client._create_asr_session_from_selection(
        "qwen",
        selection=selection,
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert session._voice_turn_factory is not None
    assert session._voice_turn_factory.keywords["smart_turn_required"] is True


def test_core_follow_selection_ignores_soniox_and_dev_routing(monkeypatch):
    callback = AsyncMock()
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    monkeypatch.setenv("ASR_USER_REGION", "intl")
    monkeypatch.setenv("SONIOX_REGION", "eu")
    monkeypatch.setattr(
        asr_client,
        "_load_core_config",
        lambda: {
            "SONIOX_API_KEY": "soniox-key",
            "ASSIST_API_KEY_GEMINI": "gemini-key",
        },
    )

    selection = asr_client._resolve_core_follow_selection("gemini")
    session = asr_client._create_asr_session_from_selection(
        "gemini",
        selection=selection,
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert selection.provider_key == "gemini"
    assert selection.endpointing_mode == "manual"
    assert session._voice_turn_factory is not None


def test_dummy_selection_reads_dev_override_once_and_uses_smart_turn(monkeypatch):
    callback = AsyncMock()
    reads = 0

    def fake_getenv(name, default=""):
        nonlocal reads
        if name == "ASR_PROVIDER":
            reads += 1
            return "dummy"
        return default

    monkeypatch.setattr(asr_client.os, "getenv", fake_getenv)
    session = create_asr_session(
        "qwen",
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert reads == 1
    assert session._config.endpointing_mode == "manual"
    assert session._voice_turn_factory is not None


def test_provider_endpoint_does_not_install_smart_turn_factory(monkeypatch):
    callback = AsyncMock()
    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.setattr(
        asr_client,
        "_resolve_asr_selection",
        lambda *_args, **_kwargs: asr_client._AsrSelection(
            provider_key="qwen",
            endpointing_mode="provider",
            _worker_fn=dummy_asr_worker,
            _api_key="test-key",
        ),
        raising=False,
    )
    monkeypatch.setitem(
        ASR_PROVIDER_REGISTRY,
        "qwen",
        replace(
            ASR_PROVIDER_REGISTRY["qwen"],
            implementation_status="implemented",
            requires_smart_turn=True,
        ),
    )

    session = create_asr_session(
        "qwen",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert session._voice_turn_factory is None


def test_endpointing_contract_is_provider_neutral_and_route_defaulted(monkeypatch):
    callback = AsyncMock()
    observed_modes: list[tuple[str, str]] = []

    def fake_get_asr_worker(core_type, endpointing_mode="manual", **kwargs):
        observed_modes.append((core_type, endpointing_mode))
        provider_key = (
            kwargs.get("provider_key_override")
            or CORE_ASR_ROUTES[core_type].provider_key
        )
        return dummy_asr_worker, "test-key", provider_key

    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.setattr(asr_client, "_get_asr_worker", fake_get_asr_worker)

    grok_session = create_asr_session(
        "grok",
        on_input_transcript=callback,
        on_connection_error=callback,
    )
    qwen_session = create_asr_session(
        "qwen",
        on_input_transcript=callback,
        on_connection_error=callback,
    )

    assert grok_session._config.endpointing_mode == "provider"
    assert qwen_session._config.endpointing_mode == "provider"
    assert grok_session._voice_turn_factory is None
    assert qwen_session._voice_turn_factory is None
    assert observed_modes == [("grok", "provider"), ("qwen", "provider")]
    with pytest.raises(ValueError, match="manual.*provider"):
        AsrSessionConfig(endpointing_mode="server_vad")


def test_phase2_factory_resolves_credentials_and_qwen_region(monkeypatch):
    import utils.config_manager as config_manager

    class FakeConfigManager:
        def get_core_config(self):
            return {
                "ASSIST_API_KEY_QWEN": "qwen-cn-key",
                "ASSIST_API_KEY_QWEN_INTL": "qwen-intl-key",
                "ASSIST_API_KEY_OPENAI": "openai-key",
                "ASSIST_API_KEY_STEP": "step-key",
                "ASSIST_API_KEY_GROK": "grok-key",
                "AUDIO_API_KEY": "must-not-be-used",
            }

    monkeypatch.delenv("ASR_PROVIDER", raising=False)
    monkeypatch.setattr(
        config_manager,
        "get_config_manager",
        lambda: FakeConfigManager(),
    )
    monkeypatch.setitem(
        ASR_PROVIDER_REGISTRY,
        "qwen",
        replace(
            ASR_PROVIDER_REGISTRY["qwen"],
            implementation_status="implemented",
        ),
    )

    cn_worker, cn_key, cn_provider = asr_client._get_asr_worker("qwen")
    intl_worker, intl_key, intl_provider = asr_client._get_asr_worker("qwen_intl")
    assert (cn_key, cn_provider) == ("qwen-cn-key", "qwen")
    assert (intl_key, intl_provider) == ("qwen-intl-key", "qwen")
    assert cn_worker.keywords == {"region": "cn"}
    assert intl_worker.keywords == {"region": "intl"}

    openai_worker, openai_key, openai_provider = asr_client._get_asr_worker(
        "openai", "provider"
    )
    assert (openai_key, openai_provider) == ("openai-key", "openai")
    assert openai_worker is asr_client._IMPLEMENTED_WORKERS["openai"]

    step_worker, step_key, step_provider = asr_client._get_asr_worker(
        "step", "provider"
    )
    assert (step_key, step_provider) == ("step-key", "step")
    assert step_worker is asr_client._IMPLEMENTED_WORKERS["step"]

    grok_worker, grok_key, grok_provider = asr_client._get_asr_worker(
        "grok", "provider"
    )
    assert (grok_key, grok_provider) == ("grok-key", "grok")
    assert grok_worker is asr_client._IMPLEMENTED_WORKERS["grok"]

    with pytest.raises(RuntimeError, match="ASR_ENDPOINTING_NOT_SUPPORTED"):
        asr_client._get_asr_worker("openai", "manual")

    class MissingOpenAIConfigManager:
        def get_core_config(self):
            return {"ASSIST_API_KEY_QWEN": "another-provider-key"}

    monkeypatch.setattr(
        config_manager,
        "get_config_manager",
        lambda: MissingOpenAIConfigManager(),
    )
    with pytest.raises(RuntimeError, match="ASR_CREDENTIALS_MISSING: openai"):
        asr_client._get_asr_worker("openai", "provider")
    with pytest.raises(RuntimeError, match="ASR_CREDENTIALS_MISSING: step"):
        asr_client._get_asr_worker("step", "provider")
    with pytest.raises(RuntimeError, match="ASR_CREDENTIALS_MISSING: grok"):
        asr_client._get_asr_worker("grok", "provider")

    class AudioOnlyConfigManager:
        def get_core_config(self):
            return {
                "AUDIO_API_KEY": "audio-key",
                "TTS_API_KEY": "tts-key",
                "ASSIST_API_KEY_OPENAI": "another-provider-key",
            }

    monkeypatch.setattr(
        config_manager,
        "get_config_manager",
        lambda: AudioOnlyConfigManager(),
    )
    with pytest.raises(RuntimeError, match="ASR_CREDENTIALS_MISSING"):
        asr_client._get_asr_worker("qwen")


async def test_connect_ready_status_and_idempotent_close(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    statuses: asyncio.Queue[str] = asyncio.Queue()
    session = create_asr_session(
        "qwen",
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        on_status_message=statuses.put,
        external_endpointing_runtime=True,
    )

    await session.connect()
    await session.connect()
    assert session.is_ready is True
    assert await asyncio.wait_for(statuses.get(), 1) == "ASR_CONNECTING"
    assert await asyncio.wait_for(statuses.get(), 1) == "ASR_READY"

    await session.close()
    await session.close()
    assert session.is_ready is False
    assert await asyncio.wait_for(statuses.get(), 1) == "ASR_CLOSED"
    assert statuses.empty()


async def test_dummy_handles_multiple_utterances(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    monkeypatch.setenv("ASR_DUMMY_TRANSCRIPT", "测试识别文本")
    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = create_asr_session(
        "qwen",
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
        external_endpointing_runtime=True,
    )
    await session.connect()
    await session.signal_user_activity_end()
    await asyncio.sleep(0)
    assert transcripts.empty()

    for _ in range(2):
        await session.stream_audio(b"\x00\x00" * 160, sample_rate_hz=16_000)
        await session.signal_user_activity_end()

    assert await asyncio.wait_for(transcripts.get(), 1) == "测试识别文本"
    assert await asyncio.wait_for(transcripts.get(), 1) == "测试识别文本"
    assert session.is_ready is True
    await session.close()


async def test_pcm_16k_and_48k_are_accepted_and_rate_is_locked(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    for sample_rate, sample_count in ((16_000, 320), (48_000, 960)):
        transcripts: asyncio.Queue[str] = asyncio.Queue()
        session = create_asr_session(
            "qwen",
            config=AsrSessionConfig(input_sample_rate_hz=sample_rate),
            on_input_transcript=transcripts.put,
            on_connection_error=AsyncMock(),
            external_endpointing_runtime=True,
        )
        await session.connect()
        await session.stream_audio(b"\x00\x00" * sample_count)
        with pytest.raises((RuntimeError, ValueError), match="ASR_SAMPLE_RATE_CHANGED"):
            await session.stream_audio(
                b"\x00\x00" * 16,
                sample_rate_hz=48_000 if sample_rate == 16_000 else 16_000,
            )
        await session.signal_user_activity_end()
        assert await asyncio.wait_for(transcripts.get(), 1)
        await session.close()


async def test_48k_pcm_is_resampled_to_16k_before_worker():
    normalized_sizes: asyncio.Queue[int] = asyncio.Queue()

    async def capture_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        chunks = []
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            if request.kind == "audio":
                chunks.append(request.audio)
            elif request.kind == "commit":
                await normalized_sizes.put(sum(map(len, chunks)))
            elif request.kind == "shutdown":
                await response_queue.put(
                    _AsrWorkerEvent(kind="closed", generation=request.generation)
                )
                return

    session = _RealtimeAsrSessionImpl(
        worker_fn=capture_worker,
        api_key="",
        config=AsrSessionConfig(input_sample_rate_hz=48_000),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 48_000)
    await session.signal_user_activity_end()
    assert await asyncio.wait_for(normalized_sizes.get(), 1) == 16_000 * 2
    await session.close()


async def test_pcm_validation_and_empty_chunk(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    session = create_asr_session(
        "qwen",
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        external_endpointing_runtime=True,
    )
    await session.connect()
    await session.stream_audio(b"")
    with pytest.raises((RuntimeError, ValueError), match="ASR_INVALID_PCM"):
        await session.stream_audio(b"\x00")
    with pytest.raises((RuntimeError, ValueError), match="ASR_AUDIO_CHUNK_TOO_LARGE"):
        await session.stream_audio(b"\x00\x00" * 16_001, sample_rate_hz=16_000)
    await session.close()


async def test_duplicate_final_is_delivered_once(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    monkeypatch.setenv("ASR_DUMMY_MODE", "duplicate")
    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = create_asr_session(
        "qwen",
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
        external_endpointing_runtime=True,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    assert await asyncio.wait_for(transcripts.get(), 1)
    await asyncio.sleep(0.05)
    assert transcripts.empty()
    await session.close()


async def test_delayed_final_is_dropped_after_clear_and_close(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    monkeypatch.setenv("ASR_DUMMY_MODE", "delayed")
    monkeypatch.setenv("ASR_DUMMY_DELAY_MS", "50")
    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = create_asr_session(
        "qwen",
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
        external_endpointing_runtime=True,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    await session.clear_audio_buffer()
    await asyncio.sleep(0.1)
    assert transcripts.empty()

    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    await session.close()
    await asyncio.sleep(0.1)
    assert transcripts.empty()


async def test_callback_failure_does_not_break_session(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    callback_started: asyncio.Queue[str] = asyncio.Queue()

    async def failing_callback(text):
        await callback_started.put(text)
        raise RuntimeError("downstream failure")

    session = create_asr_session(
        "qwen",
        on_input_transcript=failing_callback,
        on_connection_error=AsyncMock(),
        external_endpointing_runtime=True,
    )
    await session.connect()
    for _ in range(2):
        await session.stream_audio(b"\x00\x00" * 160)
        await session.signal_user_activity_end()
        assert await asyncio.wait_for(callback_started.get(), 1)
    assert session.is_ready is True
    await session.close()


async def test_worker_error_is_terminal_reported_once_and_sanitized():
    errors: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=_scripted_worker,
        api_key="error",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=errors.put,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    message = await asyncio.wait_for(errors.get(), 1)
    assert message.startswith("ASR_WORKER_FAILED:")
    assert "sk-secret" not in message
    assert session.is_ready is False
    with pytest.raises(RuntimeError, match="ASR_SESSION_NOT_READY"):
        await session.stream_audio(b"\x00\x00")
    await asyncio.sleep(0.05)
    assert errors.empty()
    assert session._worker_task is not None and session._worker_task.done()
    assert session._response_task is not None and session._response_task.done()
    assert session._callback_task is not None and session._callback_task.done()
    await session.close()


async def test_update_session_is_locked_after_connect(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    session = create_asr_session(
        "qwen",
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
        external_endpointing_runtime=True,
    )
    await session.update_session({"language": "en-US", "instructions": "ignored"})
    with pytest.raises(ValueError, match="unknown session field"):
        await session.update_session({"endpointing_mode": "provider"})
    with pytest.raises((RuntimeError, ValueError), match="ASR_INVALID_CONFIG"):
        await session.update_session({"unknown_asr_field": True})
    await session.connect()
    await session.update_session({"instructions": "ignored", "tools": []})
    with pytest.raises(RuntimeError, match="ASR_SESSION_CONFIG_LOCKED"):
        await session.update_session({"language": "ja"})
    await session.close()


async def test_provider_mode_does_not_commit():
    observed_requests: list[tuple[str, int]] = []

    async def capture_provider_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                observed_requests.append((request.kind, len(request.audio)))
                if request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    transcripts: asyncio.Queue[str] = asyncio.Queue()
    errors = AsyncMock()
    session = _RealtimeAsrSessionImpl(
        worker_fn=capture_provider_worker,
        api_key="",
        config=AsrSessionConfig(
            input_sample_rate_hz=48_000,
            endpointing_mode="provider",
        ),
        on_input_transcript=transcripts.put,
        on_connection_error=errors,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 480)
    await session.signal_user_activity_end()

    assert session._request_queue is not None
    await asyncio.wait_for(session._request_queue.join(), 1)
    audio_requests = [size for kind, size in observed_requests if kind == "audio"]
    assert sum(audio_requests) == 160 * 2
    assert all(kind != "commit" for kind, _ in observed_requests)
    assert transcripts.empty()
    errors.assert_not_awaited()
    await session.close()


async def test_provider_started_is_idempotent_and_finals_are_delivered_in_order():
    observed_kinds: list[str] = []

    async def server_vad_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        audio_requests = []
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                observed_kinds.append(request.kind)
                if request.kind == "audio":
                    audio_requests.append(request)
                    if len(audio_requests) == 2:
                        common = {
                            "generation": request.generation,
                            "buffer_epoch": request.buffer_epoch,
                        }
                        await response_queue.put(
                            _AsrWorkerEvent(
                                kind="utterance_started",
                                utterance_id=10,
                                **common,
                            )
                        )
                        # A repeated provider speech-start notification is
                        # idempotent and must not create another turn.
                        await response_queue.put(
                            _AsrWorkerEvent(
                                kind="utterance_started",
                                utterance_id=10,
                                **common,
                            )
                        )
                        await response_queue.put(
                            _AsrWorkerEvent(
                                kind="utterance_started",
                                utterance_id=11,
                                **common,
                            )
                        )
                        # Turn 2 may complete before turn 1.
                        await response_queue.put(
                            _AsrWorkerEvent(
                                kind="final",
                                utterance_id=11,
                                text="second",
                                **common,
                            )
                        )
                        await response_queue.put(
                            _AsrWorkerEvent(
                                kind="final",
                                utterance_id=10,
                                text="first",
                                **common,
                            )
                        )
                        await response_queue.put(
                            _AsrWorkerEvent(
                                kind="final",
                                utterance_id=10,
                                text="duplicate",
                                **common,
                            )
                        )
                elif request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(
                            kind="closed",
                            generation=request.generation,
                        )
                    )
                    return
            finally:
                request_queue.task_done()

    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=server_vad_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()

    assert await asyncio.wait_for(transcripts.get(), 1) == "first"
    assert await asyncio.wait_for(transcripts.get(), 1) == "second"
    await asyncio.sleep(0.05)
    assert transcripts.empty()
    assert "commit" not in observed_kinds
    assert session._utterance_id == 1
    await session.close()


async def test_manual_finals_are_delivered_in_commit_order():
    async def out_of_order_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        commits = []
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            try:
                if request.kind == "commit":
                    commits.append(request)
                    if len(commits) == 2:
                        for item, text in (
                            (commits[1], "second"),
                            (commits[0], "first"),
                        ):
                            await response_queue.put(
                                _AsrWorkerEvent(
                                    kind="final",
                                    generation=item.generation,
                                    buffer_epoch=item.buffer_epoch,
                                    utterance_id=item.utterance_id,
                                    text=text,
                                )
                            )
                elif request.kind == "shutdown":
                    await response_queue.put(
                        _AsrWorkerEvent(kind="closed", generation=request.generation)
                    )
                    return
            finally:
                request_queue.task_done()

    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=out_of_order_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="manual"),
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    for _ in range(2):
        await session.stream_audio(b"\x00\x00" * 160)
        await session.signal_user_activity_end()

    assert await asyncio.wait_for(transcripts.get(), 1) == "first"
    assert await asyncio.wait_for(transcripts.get(), 1) == "second"
    await session.close()


async def test_partial_empty_duplicate_and_conflicting_finals_are_filtered():
    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=_scripted_worker,
        api_key="events",
        config=AsrSessionConfig(),
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    assert await asyncio.wait_for(transcripts.get(), 1) == "first"
    await asyncio.sleep(0.05)
    assert transcripts.empty()
    await session.close()


async def test_optional_partial_callback_receives_preview_without_history_write():
    previews: asyncio.Queue[str] = asyncio.Queue()
    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=_scripted_worker,
        api_key="events",
        config=AsrSessionConfig(),
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
    )
    asr_client._attach_partial_callback(session, previews.put)

    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()

    assert await asyncio.wait_for(previews.get(), 1) == "draft"
    assert await asyncio.wait_for(transcripts.get(), 1) == "first"
    assert previews.empty()
    assert transcripts.empty()
    await session.close()


async def test_empty_final_is_delivered_as_turn_completion() -> None:
    async def empty_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            if request.kind == "commit":
                await response_queue.put(
                    _AsrWorkerEvent(
                        kind="final",
                        generation=request.generation,
                        buffer_epoch=request.buffer_epoch,
                        utterance_id=request.utterance_id,
                        text="",
                    )
                )
            elif request.kind == "shutdown":
                await response_queue.put(
                    _AsrWorkerEvent(kind="closed", generation=request.generation)
                )
                return

    transcripts: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=empty_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=transcripts.put,
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()

    assert await asyncio.wait_for(transcripts.get(), 1) == ""
    await session.close()


async def test_stale_utterance_error_is_dropped_after_clear():
    errors: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=_delayed_error_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=errors.put,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    await session.clear_audio_buffer()
    await asyncio.sleep(0.05)
    assert errors.empty()
    assert session.is_ready is True
    await session.close()


async def test_close_unblocks_request_backpressure(monkeypatch):
    monkeypatch.setattr(asr_infra, "_WORKER_CLOSE_TIMEOUT_SECONDS", 0.02)
    session = _RealtimeAsrSessionImpl(
        worker_fn=_non_consuming_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    one_second = b"\x00\x00" * 16_000
    for _ in range(asr_infra._ACTIVE_QUEUE_MAX_AUDIO_MS // 1_000):
        await session.stream_audio(one_second)

    blocked_producer = asyncio.create_task(session.stream_audio(one_second))
    await asyncio.sleep(0)
    assert blocked_producer.done() is False
    await asyncio.wait_for(session.close(), 1)
    with pytest.raises(RuntimeError, match="ASR_SESSION_NOT_READY"):
        await blocked_producer


async def test_request_queue_hold_atomically_keeps_dequeued_audio_in_budget():
    queue = _AsrRequestQueue()
    one_second = b"\x00\x00" * 16_000
    request = _AsrWorkerRequest(kind="audio", generation=0, audio=one_second)
    queue.put_nowait(request)

    assert queue.waiting_audio_bytes == len(one_second)
    assert queue.waiting_audio_items == 1
    dequeued, hold = await queue.get_with_audio_hold()

    assert dequeued is request
    assert hold is not None
    assert queue.waiting_audio_bytes == len(one_second)
    assert queue.waiting_audio_items == 1
    assert queue.held_audio_bytes == len(one_second)
    assert queue.held_audio_items == 1

    hold.release()
    hold.release()
    queue.task_done()
    assert queue.waiting_audio_bytes == 0
    assert queue.waiting_audio_items == 0
    assert queue.held_audio_bytes == 0
    assert queue.held_audio_items == 0

    for kind in ("commit", "clear", "shutdown"):
        queue.put_nowait(_AsrWorkerRequest(kind=kind, generation=0))
    assert queue.waiting_audio_bytes == 0
    assert queue.waiting_audio_items == 0
    for _ in range(3):
        queue.get_nowait()
        queue.task_done()


async def test_request_backpressure_limits_tiny_audio_item_count(monkeypatch):
    monkeypatch.setattr(asr_infra, "_REQUEST_BACKPRESSURE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(asr_infra, "_WORKER_CLOSE_TIMEOUT_SECONDS", 0.02)
    session = _RealtimeAsrSessionImpl(
        worker_fn=_non_consuming_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    for _ in range(asr_infra._ACTIVE_QUEUE_MAX_AUDIO_ITEMS):
        await session.stream_audio(b"\x00\x00")

    with pytest.raises(RuntimeError, match="ASR_STREAM_BACKPRESSURE"):
        await session.stream_audio(b"\x00\x00")

    await session.close()


async def test_sustained_request_backpressure_blocks_the_turn(monkeypatch):
    monkeypatch.setattr(asr_infra, "_REQUEST_BACKPRESSURE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(asr_infra, "_WORKER_CLOSE_TIMEOUT_SECONDS", 0.02)
    session = _RealtimeAsrSessionImpl(
        worker_fn=_non_consuming_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    one_second = b"\x00\x00" * 16_000
    for _ in range(asr_infra._ACTIVE_QUEUE_MAX_AUDIO_MS // 1_000):
        await session.stream_audio(one_second)

    with pytest.raises(RuntimeError, match="ASR_STREAM_BACKPRESSURE"):
        await session.stream_audio(one_second)

    await session.close()


async def test_invalid_ready_generation_times_out(monkeypatch):
    monkeypatch.setattr(asr_infra, "_READY_TIMEOUT_SECONDS", 0.02)
    errors: asyncio.Queue[str] = asyncio.Queue()
    session = _RealtimeAsrSessionImpl(
        worker_fn=_wrong_generation_ready_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=errors.put,
    )
    with pytest.raises(RuntimeError, match="ASR_CONNECT_TIMEOUT"):
        await session.connect()
    assert (await asyncio.wait_for(errors.get(), 1)).startswith("ASR_CONNECT_TIMEOUT:")
    await session.close()


async def test_worker_normal_exit_is_terminal():
    release_worker = asyncio.Event()
    errors: asyncio.Queue[str] = asyncio.Queue()

    async def exiting_worker(request_queue, response_queue, api_key, config):
        del request_queue, api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        await release_worker.wait()

    session = _RealtimeAsrSessionImpl(
        worker_fn=exiting_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=errors.put,
    )
    await session.connect()
    release_worker.set()
    message = await asyncio.wait_for(errors.get(), 1)
    assert message == "ASR_WORKER_FAILED: worker closed unexpectedly"
    assert session._response_task is not None
    await asyncio.wait_for(asyncio.shield(session._response_task), 1)
    assert session.is_ready is False
    assert session._worker_task is not None and session._worker_task.done()
    assert session._response_task.done()
    assert session._callback_task is not None and session._callback_task.done()
    await session.close()

    async def immediately_exiting_worker(
        request_queue, response_queue, api_key, config
    ):
        del request_queue, api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))

    immediate_errors: asyncio.Queue[str] = asyncio.Queue()
    immediate_session = _RealtimeAsrSessionImpl(
        worker_fn=immediately_exiting_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=immediate_errors.put,
    )
    with pytest.raises(RuntimeError, match="ASR_WORKER_FAILED"):
        await immediate_session.connect()
    assert (await asyncio.wait_for(immediate_errors.get(), 1)).startswith(
        "ASR_WORKER_FAILED:"
    )
    assert immediate_session.is_ready is False
    await immediate_session.close()


async def test_cancelled_close_still_finishes_cleanup():
    session = _RealtimeAsrSessionImpl(
        worker_fn=_scripted_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    await session._operation_lock.acquire()
    try:
        close_waiter = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        assert session.is_ready is False
        close_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_waiter
    finally:
        session._operation_lock.release()

    await asyncio.wait_for(session.close(), 1)
    assert session.is_ready is False
    assert session._worker_task is not None and session._worker_task.done()
    assert session._response_task is not None and session._response_task.done()
    assert session._callback_task is not None and session._callback_task.done()


async def test_dummy_does_not_retain_pcm_requests(monkeypatch):
    class Payload:
        pass

    monkeypatch.setenv("ASR_DUMMY_MODE", "normal")
    request_queue: asyncio.Queue[_AsrWorkerRequest] = asyncio.Queue()
    events: asyncio.Queue[_AsrWorkerEvent] = asyncio.Queue()
    worker_task = asyncio.create_task(
        dummy_asr_worker(
            request_queue,
            events,
            "",
            AsrSessionConfig(),
        )
    )
    ready = await asyncio.wait_for(events.get(), 1)
    assert ready.kind == "ready"

    first_payload = Payload()
    first_payload_ref = weakref.ref(first_payload)
    first_request = _AsrWorkerRequest(
        kind="audio",
        generation=0,
        buffer_epoch=0,
        utterance_id=1,
        audio=first_payload,  # type: ignore[arg-type]
    )
    second_request = _AsrWorkerRequest(
        kind="audio",
        generation=0,
        buffer_epoch=0,
        utterance_id=1,
        audio=Payload(),  # type: ignore[arg-type]
    )

    try:
        await request_queue.put(first_request)
        await request_queue.put(second_request)
        for _ in range(20):
            if request_queue.empty():
                break
            await asyncio.sleep(0)
        assert request_queue.empty()
        await asyncio.sleep(0)
        del first_request, first_payload
        gc.collect()
        assert first_payload_ref() is None
    finally:
        await request_queue.put(_AsrWorkerRequest(kind="shutdown", generation=1))
        await asyncio.wait_for(worker_task, 1)


async def test_dummy_close_cancels_long_delayed_final(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    monkeypatch.setenv("ASR_DUMMY_MODE", "delayed")
    monkeypatch.setenv("ASR_DUMMY_DELAY_MS", "10000")
    transcripts: asyncio.Queue[str] = asyncio.Queue()
    errors = AsyncMock()
    session = create_asr_session(
        "qwen",
        on_input_transcript=transcripts.put,
        on_connection_error=errors,
        external_endpointing_runtime=True,
    )

    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()
    await asyncio.wait_for(session.close(), 1)

    assert transcripts.empty()
    errors.assert_not_awaited()


async def test_transcript_callback_can_close_session(monkeypatch):
    monkeypatch.setenv("ASR_PROVIDER", "dummy")
    monkeypatch.setenv("ASR_DUMMY_MODE", "normal")
    callback_returned = asyncio.Event()
    errors = AsyncMock()
    session = None

    async def close_from_callback(text):
        assert text
        assert session is not None
        await session.close()
        callback_returned.set()

    session = create_asr_session(
        "qwen",
        on_input_transcript=close_from_callback,
        on_connection_error=errors,
        external_endpointing_runtime=True,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await session.signal_user_activity_end()

    await asyncio.wait_for(callback_returned.wait(), 1)
    assert session.is_ready is False
    assert session._callback_task is not None
    await asyncio.wait_for(asyncio.shield(session._callback_task), 1)
    await session.close()
    errors.assert_not_awaited()


async def test_manual_mode_accepts_only_committed_utterance_finals():
    uncommitted_finals_emitted = asyncio.Event()

    async def eager_final_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            common = {
                "generation": request.generation,
                "buffer_epoch": request.buffer_epoch,
                "utterance_id": request.utterance_id,
            }
            if request.kind == "audio":
                await response_queue.put(
                    _AsrWorkerEvent(kind="final", text="uncommitted", **common)
                )
                await response_queue.put(
                    _AsrWorkerEvent(
                        kind="final",
                        text="arbitrary",
                        generation=request.generation,
                        buffer_epoch=request.buffer_epoch,
                        utterance_id=(request.utterance_id or 0) + 100,
                    )
                )
                uncommitted_finals_emitted.set()
            elif request.kind == "commit":
                await response_queue.put(
                    _AsrWorkerEvent(kind="final", text="committed", **common)
                )
            elif request.kind == "shutdown":
                await response_queue.put(
                    _AsrWorkerEvent(kind="closed", generation=request.generation)
                )
                return

    transcripts: asyncio.Queue[str] = asyncio.Queue()
    errors = AsyncMock()
    session = _RealtimeAsrSessionImpl(
        worker_fn=eager_final_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=transcripts.put,
        on_connection_error=errors,
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00" * 160)
    await asyncio.wait_for(uncommitted_finals_emitted.wait(), 1)
    assert session._response_queue is not None
    await asyncio.wait_for(session._response_queue.join(), 1)
    assert transcripts.empty()
    errors.assert_not_awaited()

    await session.signal_user_activity_end()
    assert await asyncio.wait_for(transcripts.get(), 1) == "committed"
    await session.close()


async def test_provider_final_waits_for_in_flight_audio_enqueue():
    first_audio_received = asyncio.Event()
    release_final = asyncio.Event()

    async def racing_provider_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        first_request = None
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            if request.kind == "audio" and first_request is None:
                first_request = request
                first_audio_received.set()
                await release_final.wait()
                await response_queue.put(
                    _AsrWorkerEvent(
                        kind="utterance_started",
                        generation=request.generation,
                        buffer_epoch=request.buffer_epoch,
                        utterance_id=request.utterance_id,
                    )
                )
                await response_queue.put(
                    _AsrWorkerEvent(
                        kind="final",
                        generation=request.generation,
                        buffer_epoch=request.buffer_epoch,
                        utterance_id=request.utterance_id,
                        text="provider final",
                    )
                )
                await asyncio.sleep(0)
            elif request.kind == "shutdown":
                await response_queue.put(
                    _AsrWorkerEvent(kind="closed", generation=request.generation)
                )
                return

    callback_states: asyncio.Queue[bool] = asyncio.Queue()
    blocked_producer: asyncio.Task[None] | None = None

    async def capture_enqueue_state(text):
        assert text == "provider final"
        assert blocked_producer is not None
        await callback_states.put(blocked_producer.done())

    session = _RealtimeAsrSessionImpl(
        worker_fn=racing_provider_worker,
        api_key="",
        config=AsrSessionConfig(endpointing_mode="provider"),
        on_input_transcript=capture_enqueue_state,
        on_connection_error=AsyncMock(),
    )
    await session.connect()
    await session.stream_audio(b"\x00\x00")
    await asyncio.wait_for(first_audio_received.wait(), 1)
    for _ in range(asr_infra._REQUEST_QUEUE_SIZE):
        await session.stream_audio(b"\x00\x00")

    blocked_producer = asyncio.create_task(session.stream_audio(b"\x00\x00"))
    await asyncio.sleep(0)
    assert blocked_producer.done() is False
    release_final.set()

    assert await asyncio.wait_for(callback_states.get(), 1) is True
    await asyncio.wait_for(blocked_producer, 1)
    await session.close()


async def test_delivered_request_wins_over_simultaneous_worker_exit():
    release_worker = asyncio.Event()

    async def exiting_worker(request_queue, response_queue, api_key, config):
        del request_queue, api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        await release_worker.wait()

    errors: asyncio.Queue[str] = asyncio.Queue()
    delivered: list[_AsrWorkerRequest] = []
    session = _RealtimeAsrSessionImpl(
        worker_fn=exiting_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=errors.put,
    )
    await session.connect()

    class DeliveringQueue:
        async def put(self, request):
            delivered.append(request)
            release_worker.set()

    session._request_queue = DeliveringQueue()  # type: ignore[assignment]
    await session.stream_audio(b"\x00\x00")

    assert [request.kind for request in delivered] == ["audio"]
    assert (await asyncio.wait_for(errors.get(), 1)).startswith("ASR_WORKER_FAILED:")
    await session.close()


async def test_worker_exception_during_close_is_not_reported():
    async def shutdown_error_worker(request_queue, response_queue, api_key, config):
        del api_key, config
        await response_queue.put(_AsrWorkerEvent(kind="ready", generation=0))
        while True:
            request = await request_queue.get()
            if request.kind == "shutdown":
                raise RuntimeError("shutdown failed")

    errors = AsyncMock()
    session = _RealtimeAsrSessionImpl(
        worker_fn=shutdown_error_worker,
        api_key="",
        config=AsrSessionConfig(),
        on_input_transcript=AsyncMock(),
        on_connection_error=errors,
    )
    await session.connect()
    await asyncio.wait_for(session.close(), 1)

    assert session.is_ready is False
    errors.assert_not_awaited()


class _RuntimeStartCandidate:
    def __init__(
        self,
        *,
        connect_gate: asyncio.Event | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.connect_started = asyncio.Event()
        self._connect_gate = connect_gate
        self._connect_error = connect_error
        self.is_ready = True
        self.close = AsyncMock()

    async def connect(self) -> None:
        self.connect_started.set()
        if self._connect_gate is not None:
            await self._connect_gate.wait()
        if self._connect_error is not None:
            raise self._connect_error


def _runtime_selection():
    return type(
        "Selection",
        (),
        {
            "provider_key": "qwen",
            "endpointing_mode": "provider",
        },
    )()


def _runtime_callbacks(
    *,
    on_prepare_turn=None,
    on_lifecycle=None,
    failures: list | None = None,
    statuses: list | None = None,
) -> AsrRuntimeCallbacks:
    captured_failures = failures if failures is not None else []
    captured_statuses = statuses if statuses is not None else []

    async def capture_failure(event) -> None:
        captured_failures.append(event)

    async def capture_status(event) -> None:
        captured_statuses.append(event)

    return AsrRuntimeCallbacks(
        display_name=lambda: "runtime-test",
        on_prepare_turn=on_prepare_turn or AsyncMock(return_value=True),
        on_partial=AsyncMock(),
        on_final=AsyncMock(),
        on_turn_abandoned=AsyncMock(),
        on_failure=capture_failure,
        on_status=capture_status,
        on_lifecycle=on_lifecycle or AsyncMock(),
    )


def _patch_runtime_start(
    monkeypatch,
    candidates: list[_RuntimeStartCandidate],
) -> None:
    selection = _runtime_selection()
    monkeypatch.setattr(
        asr_runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=selection),
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_create_asr_session_from_selection",
        MagicMock(side_effect=candidates),
    )


async def test_runtime_stop_session_preserves_ingress_and_allows_restart(
    monkeypatch,
) -> None:
    first = _RuntimeStartCandidate()
    second = _RuntimeStartCandidate()
    _patch_runtime_start(monkeypatch, [first, second])
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    ingress = runtime._asr_admission_ingress

    first_result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
    )
    ingress_worker = ingress._worker
    await runtime.stop_session()

    assert first_result.status is AsrStartStatus.READY
    assert runtime._asr_admission_ingress is ingress
    assert runtime._asr_admission_ingress_started is True
    assert ingress_worker is not None and ingress_worker.done() is False

    second_result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
    )

    assert second_result.status is AsrStartStatus.READY
    assert runtime._asr_admission_ingress is ingress
    assert ingress._worker is ingress_worker
    first.close.assert_awaited_once_with()
    second.close.assert_not_awaited()

    await runtime.close()
    assert ingress_worker.done() is True


async def test_runtime_retires_twelve_admission_turns_across_session_cycles(
    monkeypatch,
) -> None:
    candidates = [_RuntimeStartCandidate() for _ in range(12)]
    _patch_runtime_start(monkeypatch, candidates)
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    ingress = runtime._asr_admission_ingress

    for cycle in range(12):
        result = await runtime.start(
            route_key="qwen",
            resource_optimization_enabled=True,
        )
        assert result.status is AsrStartStatus.READY
        token = VoiceTurnToken(
            VoiceIngressToken(
                runtime._asr_session_epoch,
                "capacity-test",
                cycle + 1,
                cycle + 1,
                runtime._asr_audio_generation,
            ),
            1,
        )
        await ingress.open_turn(token)
        await runtime.stop_session()
        assert await runtime._asr_admission.live_turn_tokens() == ()

    assert runtime._asr_admission_ingress is ingress
    assert ingress._worker is not None and ingress._worker.done() is False
    assert all(candidate.close.await_count == 1 for candidate in candidates)
    await runtime.close()


async def test_runtime_terminal_close_is_idempotent_and_cannot_restart() -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())

    await runtime.close()
    terminal_close = runtime._asr_terminal_close_task

    assert terminal_close is not None and terminal_close.done() is True
    assert terminal_close not in runtime._asr_owned_cleanup_tasks
    assert runtime._asr_terminal_close_requested is True
    assert runtime._asr_admission_ingress_started is False

    await runtime.close()
    result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
    )

    assert runtime._asr_terminal_close_task is terminal_close
    assert result.status is AsrStartStatus.FAILED
    assert result.failure_code == "ASR_START_STALE"


async def test_runtime_terminal_close_waits_for_admission_settlement() -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    await runtime._asr_admission_ingress.start()
    runtime._asr_admission_ingress_started = True
    settlement_entered = asyncio.Event()
    release_settlement = asyncio.Event()

    async def settle() -> None:
        settlement_entered.set()
        await release_settlement.wait()

    settlement = asyncio.create_task(settle())
    runtime._track_admission_effect_task(settlement, None)
    settlement.add_done_callback(runtime._admission_effect_done)

    closing = asyncio.create_task(runtime.close())
    await asyncio.wait_for(settlement_entered.wait(), 1)
    await asyncio.sleep(0)

    assert closing.done() is False
    assert runtime._asr_admission_ingress._closing is False

    release_settlement.set()
    await asyncio.wait_for(closing, 1)

    assert settlement.done() is True
    assert runtime._asr_admission_ingress._closed is True
    assert settlement not in runtime._asr_admission_effect_tasks


async def test_terminal_timeout_keeps_unsettled_effect_task_owned(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        asr_runtime_module,
        "_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS",
        0.08,
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_ASR_TERMINAL_HARD_CLOSE_RESERVE_SECONDS",
        0.04,
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_ASR_TERMINAL_CLOSE_JOIN_SLICE_SECONDS",
        0.01,
    )
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    release_settlement = asyncio.Event()

    async def resist_one_cancellation() -> None:
        try:
            await release_settlement.wait()
        except asyncio.CancelledError:
            await release_settlement.wait()

    settlement = asyncio.create_task(resist_one_cancellation())
    runtime._track_admission_effect_task(settlement, None)
    settlement.add_done_callback(runtime._admission_effect_done)

    await asyncio.wait_for(runtime.close(), 0.5)

    assert settlement.done() is False
    assert settlement in runtime._asr_admission_effect_tasks
    assert settlement in runtime._asr_admission_effect_task_turns

    release_settlement.set()
    await asyncio.wait_for(settlement, 1)
    assert settlement not in runtime._asr_admission_effect_tasks
    assert settlement not in runtime._asr_admission_effect_task_turns


async def test_terminal_timeout_keeps_blocked_ingress_close_owner_tracked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        asr_runtime_module,
        "_ASR_TERMINAL_CLOSE_TIMEOUT_SECONDS",
        0.08,
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_ASR_TERMINAL_HARD_CLOSE_RESERVE_SECONDS",
        0.04,
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_ASR_TERMINAL_CLOSE_JOIN_SLICE_SECONDS",
        0.01,
    )
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    ingress = runtime._asr_admission_ingress
    await ingress.start()
    runtime._asr_admission_ingress_started = True
    token = VoiceTurnToken(
        VoiceIngressToken(0, "blocked-ingress", 1, 1, 0),
        1,
    )

    await runtime._asr_admission._lock.acquire()
    opened = ingress.open_turn_nowait(token)
    while not ingress._items:
        await asyncio.sleep(0)

    await asyncio.wait_for(runtime.close(), 0.5)

    owners = {
        task
        for task in runtime._asr_close_tasks
        if task.get_name() == "independent-asr-terminal-admission-ingress-close"
    }
    assert len(owners) == 1
    ingress_owner = owners.pop()
    assert ingress_owner.done() is False
    assert ingress._worker is not None and ingress._worker.done() is False
    assert ingress._closing is True

    runtime._asr_admission._lock.release()
    await asyncio.wait_for(opened, 1)
    await asyncio.wait_for(ingress_owner, 1)
    await asyncio.sleep(0)

    assert ingress._closed is True
    assert ingress_owner not in runtime._asr_close_tasks


async def test_runtime_start_closed_during_lifecycle_returns_stale_without_ready(
    monkeypatch,
) -> None:
    lifecycle_entered = asyncio.Event()
    release_lifecycle = asyncio.Event()
    statuses = []

    async def block_lifecycle(_event) -> None:
        lifecycle_entered.set()
        await release_lifecycle.wait()

    candidate = _RuntimeStartCandidate()
    _patch_runtime_start(monkeypatch, [candidate])
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(
            on_lifecycle=block_lifecycle,
            statuses=statuses,
        )
    )

    start_task = asyncio.create_task(
        runtime.start(
            route_key="qwen",
            resource_optimization_enabled=True,
        )
    )
    await asyncio.wait_for(lifecycle_entered.wait(), 1)
    await asyncio.wait_for(runtime.close(), 1)
    release_lifecycle.set()
    result = await asyncio.wait_for(start_task, 1)

    assert result.status is AsrStartStatus.FAILED
    assert result.failure_code == "ASR_START_STALE"
    assert runtime._asr_session is None
    assert [event.code for event in statuses] == []
    candidate.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["close", "abort", "warm"])
async def test_cancelled_runtime_teardown_keeps_provider_close_owned(operation: str):
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocking_close() -> None:
        close_started.set()
        await release_close.wait()

    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    _install_active_runtime_state(runtime, detector)
    session = runtime._asr_session
    session.close = AsyncMock(side_effect=blocking_close)

    if operation == "close":
        teardown = runtime.close()
    elif operation == "abort":
        teardown = runtime.abort("test_cancel")
    else:
        teardown = runtime._close_transport_only()
    teardown_task = asyncio.create_task(teardown)
    await close_started.wait()

    teardown_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await teardown_task

    assert runtime._asr_session is None
    assert session.close.await_count == 1

    owned_cleanup = tuple(runtime._asr_owned_cleanup_tasks)
    assert owned_cleanup
    release_close.set()
    await asyncio.gather(*owned_cleanup)
    assert not runtime._asr_owned_cleanup_tasks
    assert session.close.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_runtime_close_retry_waits_for_same_cleanup() -> None:
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocking_close() -> None:
        close_started.set()
        await release_close.wait()

    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    _install_active_runtime_state(runtime, detector)
    session = runtime._asr_session
    session.close = AsyncMock(side_effect=blocking_close)

    first_close = asyncio.create_task(runtime.close())
    await close_started.wait()
    owned_close = runtime._asr_terminal_close_task
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close

    retry_started = asyncio.Event()

    async def retry_runtime_close() -> None:
        retry_started.set()
        await runtime.close()

    retry_close = asyncio.create_task(retry_runtime_close())
    await retry_started.wait()
    assert runtime._asr_terminal_close_task is owned_close
    assert retry_close.done() is False

    release_close.set()
    await asyncio.wait_for(retry_close, 1)
    assert session.close.await_count == 1


@pytest.mark.asyncio
async def test_runtime_close_detaches_before_owned_task_can_be_invalidated() -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    _install_active_runtime_state(runtime, detector)
    session = runtime._asr_session
    original_schedule = runtime._schedule_owned_cleanup
    detached_at_schedule: list[bool] = []

    def invalidate_in_schedule_gap(awaitable, *, name):
        detached_at_schedule.append(
            runtime._asr_session is None
            and runtime._asr_lifecycle is None
            and runtime._asr_detector is None
        )
        task = original_schedule(awaitable, name=name)
        runtime._invalidate_asr_start()
        return task

    runtime._schedule_owned_cleanup = invalidate_in_schedule_gap

    await runtime.close()

    # Every cleanup this close owns must be scheduled against fully detached
    # state -- asserted as a property rather than a count, so the claim does
    # not depend on how many tasks close() happens to split its teardown into.
    assert detached_at_schedule
    assert all(detached_at_schedule), detached_at_schedule
    assert runtime._asr_session is None
    assert runtime._asr_lifecycle is None
    assert runtime._asr_detector is None
    detector.close.assert_awaited_once_with()
    session.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cancelled_start_keeps_predecessor_cleanup_owned() -> None:
    detector_close_started = asyncio.Event()
    release_detector_close = asyncio.Event()

    async def blocking_detector_close() -> None:
        detector_close_started.set()
        await release_detector_close.wait()

    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    detector.close.side_effect = blocking_detector_close
    _install_active_runtime_state(runtime, detector)
    session = runtime._asr_session

    starting = asyncio.create_task(
        runtime.start(
            route_key="independent",
            resource_optimization_enabled=True,
        )
    )
    await asyncio.wait_for(detector_close_started.wait(), timeout=1)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    owned_cleanup = tuple(runtime._asr_owned_cleanup_tasks)
    assert runtime._asr_session is None
    assert runtime._asr_detector is None
    assert runtime._asr_lifecycle is None
    assert len(owned_cleanup) == 1
    assert owned_cleanup[0].cancelled() is False
    assert session.close.await_count == 0

    release_detector_close.set()
    await asyncio.wait_for(asyncio.gather(*owned_cleanup), timeout=1)

    detector.close.assert_awaited_once_with()
    session.close.assert_awaited_once_with()
    assert not runtime._asr_owned_cleanup_tasks


@pytest.mark.asyncio
async def test_runtime_close_invalidates_start_waiting_for_predecessor_cleanup(
    monkeypatch,
) -> None:
    detector_close_started = asyncio.Event()
    release_detector_close = asyncio.Event()
    explicit_close_scheduled = asyncio.Event()

    async def blocking_detector_close() -> None:
        detector_close_started.set()
        await release_detector_close.wait()

    candidate = _RuntimeStartCandidate()
    _patch_runtime_start(monkeypatch, [candidate])
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    detector.close.side_effect = blocking_detector_close
    _install_active_runtime_state(runtime, detector)
    old_session = runtime._asr_session
    original_schedule = runtime._schedule_owned_cleanup

    def observe_explicit_close(awaitable, *, name):
        task = original_schedule(awaitable, name=name)
        if name == "independent-asr-stop-session":
            explicit_close_scheduled.set()
        return task

    runtime._schedule_owned_cleanup = observe_explicit_close

    starting = asyncio.create_task(
        runtime.start(
            route_key="qwen",
            resource_optimization_enabled=True,
        )
    )
    await asyncio.wait_for(detector_close_started.wait(), timeout=1)
    predecessor_cleanup = next(
        task
        for task in runtime._asr_owned_cleanup_tasks
        if task.get_name() == "independent-asr-start-predecessor-close"
    )

    closing = asyncio.create_task(runtime.close())
    await asyncio.wait_for(explicit_close_scheduled.wait(), timeout=1)

    assert runtime._asr_terminal_close_task is not predecessor_cleanup
    assert closing.done() is False

    release_detector_close.set()
    await asyncio.wait_for(closing, timeout=1)
    result = await asyncio.wait_for(starting, timeout=1)

    assert result.status is AsrStartStatus.FAILED
    assert result.failure_code == "ASR_START_STALE"
    assert runtime._asr_session is None
    assert candidate.connect_started.is_set() is False
    asr_runtime_module._create_asr_session_from_selection.assert_not_called()
    detector.close.assert_awaited_once_with()
    old_session.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_close_continues_after_detector_close_failure() -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    detector.close.side_effect = RuntimeError("detector close failed")
    _install_active_runtime_state(runtime, detector)
    session = runtime._asr_session

    await runtime.close()

    detector.close.assert_awaited_once_with()
    session.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_abort_closes_session_when_lease_release_fails() -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    _install_active_runtime_state(runtime, detector)
    session = runtime._asr_session
    lease = SimpleNamespace(
        release=AsyncMock(side_effect=RuntimeError("lease release failed")),
    )
    runtime._asr_smart_turn_lease = lease

    await runtime.abort("test_release_failure")

    lease.release.assert_awaited_once_with()
    session.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_owned_cleanup_observes_background_failure(caplog) -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())

    async def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    cleanup_observed = asyncio.Event()
    original_done = runtime._owned_cleanup_done

    def observed_done(task) -> None:
        original_done(task)
        cleanup_observed.set()

    runtime._owned_cleanup_done = observed_done
    caplog.set_level("ERROR")
    cleanup = runtime._schedule_owned_cleanup(
        fail_cleanup(),
        name="test-owned-cleanup-failure",
    )
    await asyncio.wait_for(cleanup_observed.wait(), timeout=1)

    assert cleanup.done() is True
    assert not runtime._asr_owned_cleanup_tasks
    assert (
        "independent ASR background task test-owned-cleanup-failure failed"
        in caplog.text
    )


async def test_new_runtime_start_survives_old_connect_success(monkeypatch) -> None:
    first_release = asyncio.Event()
    first = _RuntimeStartCandidate(connect_gate=first_release)
    second = _RuntimeStartCandidate()
    statuses = []
    _patch_runtime_start(monkeypatch, [first, second])
    runtime = IndependentAsrRuntime(_runtime_callbacks(statuses=statuses))

    old_start = asyncio.create_task(
        runtime.start(
            route_key="qwen",
            resource_optimization_enabled=True,
        )
    )
    await asyncio.wait_for(first.connect_started.wait(), 1)
    current_result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
    )
    first_release.set()
    old_result = await asyncio.wait_for(old_start, 1)

    assert current_result.status is AsrStartStatus.READY
    assert old_result.failure_code == "ASR_START_STALE"
    assert runtime._asr_session is second
    assert runtime._asr_provider == "qwen"
    assert runtime._asr_lifecycle is not None
    assert runtime._asr_session_factory is not None
    assert runtime._asr_transport_selection is not None
    assert [event.code for event in statuses] == ["ASR_INDEPENDENT_READY"]
    first.close.assert_awaited_once_with()
    second.close.assert_not_awaited()
    await runtime.close()


async def test_old_connect_failure_after_new_start_has_no_failure_status(
    monkeypatch,
) -> None:
    first_release = asyncio.Event()
    first = _RuntimeStartCandidate(
        connect_gate=first_release,
        connect_error=RuntimeError("old connect failed"),
    )
    second = _RuntimeStartCandidate()
    statuses = []
    _patch_runtime_start(monkeypatch, [first, second])
    runtime = IndependentAsrRuntime(_runtime_callbacks(statuses=statuses))

    old_start = asyncio.create_task(
        runtime.start(
            route_key="qwen",
            resource_optimization_enabled=True,
        )
    )
    await asyncio.wait_for(first.connect_started.wait(), 1)
    current_result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
    )
    first_release.set()
    old_result = await asyncio.wait_for(old_start, 1)

    assert current_result.status is AsrStartStatus.READY
    assert old_result.failure_code == "ASR_START_STALE"
    assert runtime._asr_session is second
    assert [event.code for event in statuses] == ["ASR_INDEPENDENT_READY"]
    first.close.assert_awaited_once_with()
    second.close.assert_not_awaited()
    await runtime.close()


async def test_runtime_start_threads_user_language_into_session_factory(
    monkeypatch,
) -> None:
    candidates = [_RuntimeStartCandidate(), _RuntimeStartCandidate()]
    selection = _runtime_selection()
    builder_calls: list[dict] = []

    def builder(_core_type, **kwargs):
        builder_calls.append(kwargs)
        return candidates[len(builder_calls) - 1]

    monkeypatch.setattr(
        asr_runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=selection),
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_create_asr_session_from_selection",
        builder,
    )
    runtime = IndependentAsrRuntime(_runtime_callbacks())

    result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
        user_language="ja",
    )

    assert result.status is AsrStartStatus.READY
    assert builder_calls[0]["user_language"] == "ja"
    # Recovery candidates built from the stored factory must keep the
    # start-time language.
    factory = runtime._asr_session_factory
    assert factory is not None
    factory(selection)
    assert builder_calls[1]["user_language"] == "ja"
    await runtime.close()


async def test_runtime_start_defaults_user_language_to_unset(
    monkeypatch,
) -> None:
    candidate = _RuntimeStartCandidate()
    selection = _runtime_selection()
    builder_calls: list[dict] = []

    def builder(_core_type, **kwargs):
        builder_calls.append(kwargs)
        return candidate

    monkeypatch.setattr(
        asr_runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=selection),
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_create_asr_session_from_selection",
        builder,
    )
    runtime = IndependentAsrRuntime(_runtime_callbacks())

    result = await runtime.start(
        route_key="qwen",
        resource_optimization_enabled=True,
    )

    assert result.status is AsrStartStatus.READY
    assert builder_calls[0]["user_language"] is None
    await runtime.close()


def _install_runtime_prepare_state(
    runtime: IndependentAsrRuntime,
) -> None:
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    runtime._asr_session = type("Session", (), {"is_ready": True})()
    runtime._asr_provider = "qwen"
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = object()
    runtime._asr_current_ingress_token = runtime.capture_ingress_token(
        connection_id="connection",
        lease_generation=1,
        route_generation=1,
    )


async def test_stale_open_turn_cannot_reserve_on_successor_dispatcher() -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    _install_runtime_prepare_state(runtime)
    runtime._asr_session.close = AsyncMock()
    old_dispatcher = runtime._asr_transcript_dispatcher
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    turn_token = runtime._capture_turn_token(lifecycle)
    final_key = asr_runtime_module.FinalKey.from_turn(turn_token)

    # Hold the reducer lock after open_turn has entered the FIFO. stop_session()
    # can then synchronously detach/swap dispatchers and queue RouteReplaced
    # behind that open, reproducing the exact cross-session resume ordering.
    await runtime._asr_admission._lock.acquire()
    prepare = asyncio.create_task(
        runtime._prepare_independent_asr_turn(runtime._asr_session_epoch)
    )
    while not runtime._asr_admission_ingress._items:
        await asyncio.sleep(0)

    stopping = asyncio.create_task(runtime.stop_session())
    while runtime._asr_transcript_dispatcher is old_dispatcher:
        await asyncio.sleep(0)
    successor_dispatcher = runtime._asr_transcript_dispatcher
    runtime._asr_admission._lock.release()

    await asyncio.wait_for(asyncio.gather(prepare, stopping), 1)

    assert final_key not in old_dispatcher._reservations
    assert final_key not in successor_dispatcher._reservations
    assert final_key not in runtime._asr_admission_reservation_dispatchers
    assert runtime._asr_turn_prepared is False
    assert await runtime._asr_admission.get_record(turn_token) is None
    await runtime.close()


@pytest.mark.parametrize("raises", [False, True])
async def test_stale_prepare_unwind_only_releases_old_reservation(raises) -> None:
    prepare_entered = asyncio.Event()
    release_prepare = asyncio.Event()

    async def delayed_prepare(_token) -> bool:
        prepare_entered.set()
        await release_prepare.wait()
        if raises:
            raise RuntimeError("old prepare failed")
        return False

    runtime = IndependentAsrRuntime(_runtime_callbacks(on_prepare_turn=delayed_prepare))
    _install_runtime_prepare_state(runtime)
    old_dispatcher = runtime._asr_transcript_dispatcher
    prepare_task = asyncio.create_task(
        runtime._prepare_independent_asr_turn(runtime._asr_session_epoch)
    )
    await asyncio.wait_for(prepare_entered.wait(), 1)
    old_turn_token = runtime._capture_turn_token(runtime._asr_lifecycle)
    old_final_key = asr_runtime_module.FinalKey.from_turn(old_turn_token)
    assert runtime._asr_admission_reservation_dispatchers[old_final_key] is (
        old_dispatcher
    )
    old_dispatcher.resolve_reserved = MagicMock(
        wraps=old_dispatcher.resolve_reserved
    )

    new_dispatcher = TranscriptDispatcher(runtime._dispatch_asr_transcript_envelope)
    runtime._asr_transcript_dispatcher = new_dispatcher
    new_final_key = replace(
        old_final_key,
        turn_token=replace(
            old_final_key.turn_token,
            turn_id=old_final_key.turn_token.turn_id + 1,
        ),
    )
    assert new_dispatcher.try_reserve(new_final_key)
    runtime._asr_admission_reservation_dispatchers[new_final_key] = new_dispatcher
    runtime._asr_turn_prepared = True
    release_prepare.set()
    await asyncio.wait_for(prepare_task, 1)

    async def wait_for_old_resolution() -> None:
        while old_final_key not in old_dispatcher._resolved:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_old_resolution(), 1)
    await old_dispatcher.wait_idle()

    assert runtime._asr_transcript_dispatcher is new_dispatcher
    assert runtime._asr_admission_reservation_dispatchers[new_final_key] is (
        new_dispatcher
    )
    assert runtime._asr_turn_prepared is True
    assert old_final_key not in old_dispatcher._reservations
    assert old_final_key not in runtime._asr_admission_reservation_dispatchers
    assert new_final_key in new_dispatcher._reservations
    resolution = old_dispatcher.resolve_reserved.call_args
    assert resolution.args == (old_final_key, AdmissionDisposition.ABANDON)
    assert resolution.kwargs == {"envelope": None}


@pytest.mark.parametrize("raises", [False, True])
async def test_current_prepare_failure_releases_current_reservation(raises) -> None:
    async def reject_prepare(_token) -> bool:
        if raises:
            raise RuntimeError("current prepare failed")
        return False

    runtime = IndependentAsrRuntime(_runtime_callbacks(on_prepare_turn=reject_prepare))
    _install_runtime_prepare_state(runtime)
    dispatcher = runtime._asr_transcript_dispatcher
    turn_token = runtime._capture_turn_token(runtime._asr_lifecycle)
    final_key = asr_runtime_module.FinalKey.from_turn(turn_token)
    dispatcher.resolve_reserved = MagicMock(wraps=dispatcher.resolve_reserved)

    await runtime._prepare_independent_asr_turn(runtime._asr_session_epoch)

    async def wait_for_resolution() -> None:
        while final_key not in dispatcher._resolved:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_resolution(), 1)
    await dispatcher.wait_idle()

    assert runtime._asr_turn_prepared is False
    assert final_key not in dispatcher._reservations
    assert final_key not in runtime._asr_admission_reservation_dispatchers
    resolution = dispatcher.resolve_reserved.call_args
    assert resolution.args == (final_key, AdmissionDisposition.ABANDON)
    assert resolution.kwargs == {"envelope": None}


class _RuntimeDetectorStub:
    def __init__(
        self,
        *,
        on_endpointing_failure=None,
        bind_candidate=None,
        release_deferred_turn=None,
    ) -> None:
        self.on_endpointing_failure = on_endpointing_failure
        self.close = AsyncMock()
        self.reset = AsyncMock()
        self.bind_candidate = bind_candidate or AsyncMock(return_value=object())
        self.release_deferred_turn = release_deferred_turn or AsyncMock()
        self._turn_token = None

    async def prepare_endpointing(self, turn_token):
        self._turn_token = turn_token
        return SimpleNamespace(token=turn_token, release=AsyncMock())

    def endpointing_ready(self, turn_token) -> bool:
        return self._turn_token == turn_token


def _patch_runtime_detector_start(
    monkeypatch,
    candidates: list[_RuntimeStartCandidate],
) -> list[_RuntimeDetectorStub]:
    detectors: list[_RuntimeDetectorStub] = []
    selection = SimpleNamespace(provider_key="glm", endpointing_mode="manual")
    monkeypatch.setattr(
        asr_runtime_module,
        "_resolve_asr_selection",
        MagicMock(return_value=selection),
    )
    monkeypatch.setattr(
        asr_runtime_module,
        "_create_asr_session_from_selection",
        MagicMock(side_effect=candidates),
    )

    def create_detector(**kwargs):
        detector = _RuntimeDetectorStub(
            on_endpointing_failure=kwargs.get("on_endpointing_failure"),
        )
        detectors.append(detector)
        return detector

    monkeypatch.setattr(asr_runtime_module, "DetectorRuntime", create_detector)
    return detectors


def _install_pending_runtime_state(
    runtime: IndependentAsrRuntime,
    detector: _RuntimeDetectorStub,
    *,
    endpointing_mode: str = "manual",
) -> None:
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", endpointing_mode),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    lifecycle.mark_pending_turn_speech()
    lifecycle.accept_audio(b"\x01\x00" * 160, sample_rate_hz=16_000)
    lifecycle.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    runtime._asr_session = SimpleNamespace(
        is_ready=True,
        close=AsyncMock(),
        stream_audio=AsyncMock(),
    )
    runtime._asr_provider = "qwen"
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    runtime._asr_current_ingress_token = runtime.capture_ingress_token(
        connection_id="connection",
        lease_generation=1,
        route_generation=1,
    )
    runtime._asr_pending_detector_candidate = object()


def _install_active_runtime_state(
    runtime: IndependentAsrRuntime,
    detector: _RuntimeDetectorStub,
) -> None:
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("glm", "manual"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    runtime._asr_session = SimpleNamespace(is_ready=True, close=AsyncMock())
    runtime._asr_provider = "glm"
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    runtime._asr_current_ingress_token = runtime.capture_ingress_token(
        connection_id="connection",
        lease_generation=1,
        route_generation=1,
    )


def _replace_runtime_identity_same_epoch(
    runtime: IndependentAsrRuntime,
) -> tuple[object, VoiceInputLifecycleController, _RuntimeDetectorStub]:
    runtime._asr_audio_generation += 1
    session = SimpleNamespace(is_ready=True, close=AsyncMock())
    lifecycle = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    lifecycle.open(route_mode=VoiceRouteMode.INDEPENDENT)
    detector = _RuntimeDetectorStub()
    runtime._asr_session = session
    runtime._asr_provider = "qwen"
    runtime._asr_lifecycle = lifecycle
    runtime._asr_detector = detector
    runtime._asr_current_ingress_token = runtime.capture_ingress_token(
        connection_id="connection",
        lease_generation=1,
        route_generation=2,
    )
    return session, lifecycle, detector


async def test_stale_detector_failure_callback_cannot_close_new_runtime(
    monkeypatch,
) -> None:
    failures = []
    statuses = []
    first = _RuntimeStartCandidate()
    second = _RuntimeStartCandidate()
    detectors = _patch_runtime_detector_start(monkeypatch, [first, second])
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(failures=failures, statuses=statuses)
    )
    await runtime.start(route_key="glm", resource_optimization_enabled=True)
    old_detector = detectors[0]
    callback = old_detector.on_endpointing_failure
    assert callback is not None
    await runtime.start(route_key="glm", resource_optimization_enabled=True)
    new_session = runtime._asr_session
    new_lifecycle = runtime._asr_lifecycle
    new_detector = runtime._asr_detector
    current_epoch = runtime._asr_session_epoch

    await callback()

    assert runtime._asr_session_epoch == current_epoch
    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert failures == []
    assert [event.code for event in statuses].count("ASR_ENDPOINTING_FAILED") == 0
    old_detector.close.assert_awaited_once_with()
    await runtime.close()


async def test_current_detector_failure_callback_fails_closed_once(monkeypatch) -> None:
    failures = []
    statuses = []
    detectors = _patch_runtime_detector_start(
        monkeypatch,
        [_RuntimeStartCandidate()],
    )
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(failures=failures, statuses=statuses)
    )
    await runtime.start(route_key="glm", resource_optimization_enabled=True)
    callback = detectors[0].on_endpointing_failure
    assert callback is not None

    await callback()
    await asyncio.sleep(0)

    assert runtime._asr_session is None
    assert runtime._asr_lifecycle is None
    assert runtime._asr_detector is None
    assert [event.code for event in failures] == ["ASR_ENDPOINTING_FAILED"]
    assert [event.code for event in statuses].count("ASR_ENDPOINTING_FAILED") == 1


async def test_stale_pending_candidate_bind_none_cannot_fail_new_runtime() -> None:
    bind_entered = asyncio.Event()
    release_bind = asyncio.Event()

    async def bind_none(_candidate, _turn_token):
        bind_entered.set()
        await release_bind.wait()
        return None

    failures = []
    statuses = []
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(failures=failures, statuses=statuses)
    )
    detector = _RuntimeDetectorStub(
        bind_candidate=AsyncMock(side_effect=bind_none),
    )
    _install_pending_runtime_state(runtime, detector)
    activate = asyncio.create_task(
        runtime._activate_pending_independent_turn(runtime._asr_session_epoch)
    )
    await asyncio.wait_for(bind_entered.wait(), 1)
    new_session, new_lifecycle, new_detector = _replace_runtime_identity_same_epoch(
        runtime
    )

    release_bind.set()
    await asyncio.wait_for(activate, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert failures == []
    assert statuses == []


@pytest.mark.parametrize("raises", [False, True])
async def test_current_pending_candidate_bind_failure_fails_closed_once(
    raises: bool,
) -> None:
    failures = []
    statuses = []
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(failures=failures, statuses=statuses)
    )
    bind_candidate = (
        AsyncMock(side_effect=RuntimeError("bind failed"))
        if raises
        else AsyncMock(return_value=None)
    )
    detector = _RuntimeDetectorStub(bind_candidate=bind_candidate)
    _install_pending_runtime_state(runtime, detector)

    await runtime._activate_pending_independent_turn(runtime._asr_session_epoch)

    assert runtime._asr_session is None
    assert runtime._asr_lifecycle is None
    assert runtime._asr_detector is None
    assert [event.code for event in failures] == ["ASR_ENDPOINTING_FAILED"]
    assert [event.code for event in statuses] == ["ASR_ENDPOINTING_FAILED"]


@pytest.mark.parametrize("raises", [False, True])
async def test_current_provider_pending_candidate_bind_failure_fails_open(
    raises: bool,
) -> None:
    failures = []
    statuses = []
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(failures=failures, statuses=statuses)
    )
    bind_candidate = (
        AsyncMock(side_effect=RuntimeError("bind failed"))
        if raises
        else AsyncMock(return_value=None)
    )
    detector = _RuntimeDetectorStub(bind_candidate=bind_candidate)
    _install_pending_runtime_state(runtime, detector, endpointing_mode="provider")
    session = runtime._asr_session
    lifecycle = runtime._asr_lifecycle

    await runtime._activate_pending_independent_turn(runtime._asr_session_epoch)
    await runtime._asr_audio_dispatcher.wait_idle()

    detector.bind_candidate.assert_awaited_once()
    assert runtime._asr_session is session
    assert runtime._asr_lifecycle is lifecycle
    assert lifecycle.snapshot.state.value == "active"
    assert failures == []
    assert statuses == []
    await runtime.close()


async def test_provider_candidate_waits_for_confirmed_pending_turn() -> None:
    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    detector.detector_epoch = 7
    _install_pending_runtime_state(runtime, detector, endpointing_mode="provider")
    lifecycle = runtime._asr_lifecycle
    assert lifecycle is not None
    lifecycle.discard_pending_turn()
    runtime._asr_pending_detector_candidate = None
    lifecycle.transition(VoiceLifecycleEvent.SOFT_WAKE)
    lifecycle.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    lifecycle.transition(VoiceLifecycleEvent.TURN_SEALED)
    ingress = runtime._asr_current_ingress_token
    assert ingress is not None
    detector_identity = SimpleNamespace(
        ingress_token=ingress,
        detector_epoch=7,
    )
    candidate = SimpleNamespace(detector_epoch=7)
    identity = runtime._capture_runtime_identity(ingress_token=ingress)

    assert await runtime._bind_provider_detector_candidate(
        lifecycle,
        detector,
        detector_identity=detector_identity,
        candidate=candidate,
        expected_identity=identity,
    )
    assert runtime._asr_pending_detector_candidate is None
    detector.bind_candidate.assert_not_awaited()

    lifecycle.mark_pending_turn_speech()
    assert await runtime._bind_provider_detector_candidate(
        lifecycle,
        detector,
        detector_identity=detector_identity,
        candidate=candidate,
        expected_identity=identity,
        pending_speech_confirmed=True,
    )
    assert runtime._asr_pending_detector_candidate is candidate
    detector.bind_candidate.assert_not_awaited()
    await runtime.close()


async def test_stale_deferred_turn_release_error_cannot_fail_new_runtime() -> None:
    release_entered = asyncio.Event()
    release_error = asyncio.Event()

    async def fail_release() -> None:
        release_entered.set()
        await release_error.wait()
        raise RuntimeError("old detector release failed")

    failures = []
    statuses = []
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(failures=failures, statuses=statuses)
    )
    detector = _RuntimeDetectorStub(
        release_deferred_turn=AsyncMock(side_effect=fail_release),
    )
    _install_active_runtime_state(runtime, detector)
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    final = asyncio.create_task(
        runtime._handle_independent_asr_final("final", epoch, "glm")
    )
    await asyncio.wait_for(release_entered.wait(), 1)
    new_session, new_lifecycle, new_detector = _replace_runtime_identity_same_epoch(
        runtime
    )

    release_error.set()
    await asyncio.wait_for(final, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert failures == []
    assert statuses == []


async def test_current_deferred_turn_release_error_fails_closed_once() -> None:
    failures = []
    statuses = []
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(failures=failures, statuses=statuses)
    )
    detector = _RuntimeDetectorStub(
        release_deferred_turn=AsyncMock(
            side_effect=RuntimeError("current detector release failed")
        ),
    )
    _install_active_runtime_state(runtime, detector)
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)

    await runtime._handle_independent_asr_final("final", epoch, "glm")

    assert runtime._asr_session is None
    assert runtime._asr_lifecycle is None
    assert runtime._asr_detector is None
    assert [event.code for event in failures] == ["ASR_ENDPOINTING_FAILED"]
    assert [event.code for event in statuses] == ["ASR_ENDPOINTING_FAILED"]


@pytest.mark.parametrize("teardown", ["abort", "suspend"])
async def test_runtime_start_transport_teardown_returns_stale_without_ready(
    monkeypatch,
    teardown: str,
) -> None:
    release_connect = asyncio.Event()
    candidate = _RuntimeStartCandidate(connect_gate=release_connect)
    statuses = []
    _patch_runtime_start(monkeypatch, [candidate])
    runtime = IndependentAsrRuntime(_runtime_callbacks(statuses=statuses))

    start_task = asyncio.create_task(
        runtime.start(
            route_key="qwen",
            resource_optimization_enabled=True,
        )
    )
    await asyncio.wait_for(candidate.connect_started.wait(), 1)
    if teardown == "suspend":
        await runtime.suspend("game_takeover")
    else:
        await runtime.abort("hard_mute")
    release_connect.set()
    result = await asyncio.wait_for(start_task, 1)

    assert result.status is AsrStartStatus.FAILED
    assert result.failure_code == "ASR_START_STALE"
    assert runtime._asr_session is None
    assert runtime._asr_lifecycle is None
    assert runtime._asr_detector is None
    assert [event.code for event in statuses] == []
    candidate.close.assert_awaited_once_with()


async def test_stale_pending_lifecycle_callback_cannot_fail_reconnected_runtime() -> (
    None
):
    lifecycle_entered = asyncio.Event()
    release_lifecycle = asyncio.Event()
    failures = []
    statuses = []

    async def block_active_lifecycle(_event) -> None:
        lifecycle_entered.set()
        await release_lifecycle.wait()

    runtime = IndependentAsrRuntime(
        _runtime_callbacks(
            on_lifecycle=block_active_lifecycle,
            failures=failures,
            statuses=statuses,
        )
    )
    detector = _RuntimeDetectorStub()
    _install_pending_runtime_state(runtime, detector)
    runtime._asr_lifecycle.provider_policy = resolve_provider_policy(
        "qwen",
        "provider",
    )
    runtime._ensure_smart_turn_ready = AsyncMock(return_value=True)
    activate = asyncio.create_task(
        runtime._activate_pending_independent_turn(runtime._asr_session_epoch)
    )
    await asyncio.wait_for(lifecycle_entered.wait(), 1)
    new_session, new_lifecycle, new_detector = _replace_runtime_identity_same_epoch(
        runtime
    )

    release_lifecycle.set()
    await asyncio.wait_for(activate, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert runtime._asr_audio_bytes == 0
    assert failures == []
    assert statuses == []


async def test_stale_pending_prepare_callback_cannot_fail_reconnected_runtime() -> None:
    prepare_entered = asyncio.Event()
    release_prepare = asyncio.Event()
    failures = []
    statuses = []

    async def block_prepare(_token) -> bool:
        prepare_entered.set()
        await release_prepare.wait()
        return True

    runtime = IndependentAsrRuntime(
        _runtime_callbacks(
            on_prepare_turn=block_prepare,
            failures=failures,
            statuses=statuses,
        )
    )
    detector = _RuntimeDetectorStub()
    _install_pending_runtime_state(runtime, detector)
    runtime._asr_lifecycle.provider_policy = resolve_provider_policy(
        "qwen",
        "provider",
    )
    activate = asyncio.create_task(
        runtime._activate_pending_independent_turn(runtime._asr_session_epoch)
    )
    await asyncio.wait_for(prepare_entered.wait(), 1)
    new_session, new_lifecycle, new_detector = _replace_runtime_identity_same_epoch(
        runtime
    )

    release_prepare.set()
    await asyncio.wait_for(activate, 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert runtime._asr_audio_bytes == 0
    assert failures == []
    assert statuses == []


async def test_stale_final_lease_unwind_uses_old_dispatcher_only() -> None:
    lifecycle_callback = AsyncMock()
    failures = []
    statuses = []
    runtime = IndependentAsrRuntime(
        _runtime_callbacks(
            on_lifecycle=lifecycle_callback,
            failures=failures,
            statuses=statuses,
        )
    )
    detector = _RuntimeDetectorStub()
    _install_active_runtime_state(runtime, detector)
    epoch = runtime._asr_session_epoch
    await runtime._handle_independent_asr_activity(
        SpeechActivityEvent.SPEECH_STARTED,
        epoch,
    )
    await runtime._handle_independent_asr_endpoint(epoch)
    old_dispatcher = runtime._asr_transcript_dispatcher
    sealed_token = runtime._asr_sealed_turn_token
    assert sealed_token is not None
    old_final_key = asr_runtime_module.FinalKey.from_turn(sealed_token.turn)
    old_lease = runtime._asr_smart_turn_lease
    assert old_lease is not None
    assert runtime._asr_admission_reservation_dispatchers[old_final_key] is (
        old_dispatcher
    )
    old_dispatcher.resolve_reserved = MagicMock(
        wraps=old_dispatcher.resolve_reserved
    )
    release_started = asyncio.Event()
    release_lease = asyncio.Event()

    async def block_release() -> None:
        release_started.set()
        await release_lease.wait()

    old_lease.release = AsyncMock(side_effect=block_release)
    final = asyncio.create_task(
        runtime._handle_independent_asr_final("final", epoch, "glm")
    )
    await asyncio.wait_for(release_started.wait(), 1)
    detached_cleanup = runtime._detach_independent_asr()
    assert detached_cleanup is not None
    new_dispatcher = runtime._asr_transcript_dispatcher
    new_dispatcher.resolve_reserved = MagicMock(
        wraps=new_dispatcher.resolve_reserved
    )
    new_session, new_lifecycle, new_detector = _replace_runtime_identity_same_epoch(
        runtime
    )
    lifecycle_callback.reset_mock()

    release_lease.set()
    await asyncio.wait_for(asyncio.gather(final, detached_cleanup), 1)

    assert runtime._asr_session is new_session
    assert runtime._asr_lifecycle is new_lifecycle
    assert runtime._asr_detector is new_detector
    assert old_final_key not in old_dispatcher._reservations
    assert old_final_key in old_dispatcher._resolved
    assert old_final_key not in runtime._asr_admission_reservation_dispatchers
    old_resolution = old_dispatcher.resolve_reserved.call_args
    assert old_resolution.args[0] == old_final_key
    assert old_resolution.args[1] is AdmissionDisposition.FORWARD
    new_dispatcher.resolve_reserved.assert_not_called()
    lifecycle_callback.assert_not_awaited()
    assert failures == []
    assert statuses == []


def test_asr_connect_retry_budget_cannot_outlive_the_frontend_start_deadline():
    # Codex P1. Each connect attempt can burn _READY_TIMEOUT_SECONDS before
    # ASR_CONNECT_TIMEOUT, and _start_session_activate awaits the WHOLE retry
    # loop before sending session_started -- while the frontend cancels the
    # start and fires end_session at its deadline. With Soniox's three attempts
    # a sustained outage always had the frontend tear the session down mid-retry,
    # so the user saw a generic start timeout instead of the fail-closed ASR
    # verdict the backend was busy producing.
    #
    # Constants-and-structure, deliberately: driving the start loop to a real
    # timeout would need a multi-second test. What this pins is that the budget
    # stays tight enough to matter, and that the check gates the retry.
    import inspect

    from main_logic.asr_client import runtime as runtime_module
    from main_logic.asr_client._infra import _READY_TIMEOUT_SECONDS

    assert (
        runtime_module._CONNECT_TOTAL_BUDGET_SECONDS
        < runtime_module._FRONTEND_START_DEADLINE_SECONDS
    ), "the ASR connect phase must finish before the client stops listening"

    # One timed-out attempt already consumes _READY_TIMEOUT_SECONDS, so a second
    # full-timeout attempt must not fit -- otherwise the budget is decorative.
    worst_case_second_attempt = _READY_TIMEOUT_SECONDS + _READY_TIMEOUT_SECONDS
    assert worst_case_second_attempt > runtime_module._CONNECT_TOTAL_BUDGET_SECONDS, (
        "the budget must refuse a second attempt that could not finish in time"
    )

    # The guard has to sit before the backoff sleep, not after it.
    source = inspect.getsource(runtime_module)
    start_loop = source.split("connect_started_at = time.monotonic()", 1)[1].split(
        "if asr_session is None:", 1
    )[0]
    assert "_CONNECT_TOTAL_BUDGET_SECONDS" in start_loop, (
        "the start connect loop must consult the aggregate budget"
    )
    assert start_loop.index("_CONNECT_TOTAL_BUDGET_SECONDS") < start_loop.index(
        "await asyncio.sleep(backoff)"
    ), "the budget check must refuse the retry before sleeping for it"


@pytest.mark.asyncio
async def test_close_does_not_queue_its_own_cleanup_behind_a_stuck_predecessor() -> None:
    """A retired teardown that never returns must not keep this generation open."""

    runtime = IndependentAsrRuntime(_runtime_callbacks())
    detector = _RuntimeDetectorStub()
    _install_active_runtime_state(runtime, detector)
    session = runtime._asr_session

    stuck_forever = asyncio.Event()

    async def never_returns() -> None:
        await stuck_forever.wait()

    predecessor = runtime._schedule_owned_cleanup(
        never_returns(),
        name="retired-provider-close",
    )

    close = asyncio.create_task(runtime.close())
    # The predecessor is still blocked, so close() cannot have returned -- but
    # the resources IT detached are independent of that teardown and must
    # already be released.
    for _ in range(40):
        if session.close.await_count and detector.close.await_count:
            break
        await asyncio.sleep(0.01)

    detector.close.assert_awaited_once_with()
    session.close.assert_awaited_once_with()
    assert not close.done(), "close() joins the predecessor before returning"

    stuck_forever.set()
    await asyncio.wait_for(close, timeout=1)
    await asyncio.wait_for(predecessor, timeout=1)
