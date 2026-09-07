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

"""Stable public entry point for realtime speech recognition sessions."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from main_logic.voice_turn.contracts import SpeechActivityEvent
from typing import Literal

from ._infra import (
    AsrSessionConfig,
    RealtimeAsrSession,
    AsrWorkerFn as _AsrWorkerFn,
    _RealtimeAsrSessionImpl,
)
from ._registry_meta import (
    ASR_PROVIDER_REGISTRY as _ASR_PROVIDER_REGISTRY,
    CORE_ASR_ROUTES as _CORE_ASR_ROUTES,
    AsrCoreCapabilities,
    AsrEndpointingMode as _AsrEndpointingMode,
    AsrProviderAvailability as _AsrProviderAvailability,
    AsrSpeakerExactIntervalCapability,
)
from ._provider_events import (
    ProviderEndpointNotification,
    ProviderFinalNotification,
    ProviderStartedSettlement,
    ProviderUtteranceKey,
    ProviderUtteranceStartedNotification,
)
from .endpointing.detector_runtime import _create_voice_turn_adapter
from .provider_policy import resolve_provider_policy
from .workers.dummy import dummy_asr_worker as _dummy_asr_worker
from .workers.gemini import gemini_asr_worker as _gemini_asr_worker
from .workers.glm import glm_asr_worker as _glm_asr_worker
from .workers.grok import (
    _normalize_grok_language,
    grok_asr_worker as _grok_asr_worker,
)
from .workers.openai import (
    _normalize_openai_language,
    openai_asr_worker as _openai_asr_worker,
)
from .workers.qwen import (
    _qwen_language_code,
    qwen_asr_worker as _qwen_asr_worker,
)
from .workers.soniox import soniox_asr_worker as _soniox_asr_worker
from .workers.step import (
    _step_language_code,
    step_asr_worker as _step_asr_worker,
)


__all__ = [
    "AsrCoreCapabilities",
    "AsrSessionConfig",
    "RealtimeAsrSession",
    "VoiceIdentityActivationResult",
    "create_asr_session",
    "get_asr_core_capabilities",
]


class VoiceIdentityActivationResult(Enum):
    """Outcome of applying a profile to the currently active ASR route."""

    READY = "ready"
    ACTIVATION_PENDING = "activation_pending"
    UNSUPPORTED_ASR_ROUTE = "unsupported_asr_route"
    RUNTIME_DEGRADED = "runtime_degraded"

    def __bool__(self) -> bool:
        """Treat a future-route install as applied even when not active yet."""

        return self is not VoiceIdentityActivationResult.RUNTIME_DEGRADED


def get_asr_core_capabilities(core_type: str) -> AsrCoreCapabilities | None:
    """Return route-owned ASR capabilities, or ``None`` for an unknown Core.

    Callers must preserve their fail-closed behavior for ``None`` instead of
    treating an unrecognized route as native-only.
    """

    core_key = str(core_type or "").strip().lower()
    route = _CORE_ASR_ROUTES.get(core_key)
    return None if route is None else route.capabilities


# Providers with a restricted language matrix reuse their worker's own
# validator, so the session hint can never desync from what the worker will
# accept at connect time. Providers absent here (soniox, glm, gemini, dummy)
# either accept any normalized hint or ignore the language entirely.
_PROVIDER_LANGUAGE_VALIDATORS: dict[str, Callable[[str], str | None]] = {
    "qwen": _qwen_language_code,
    "openai": _normalize_openai_language,
    "step": _step_language_code,
    "grok": _normalize_grok_language,
}


def _resolve_session_language(
    provider_key: str,
    user_language: str | None,
) -> str:
    """Map a caller-supplied language onto one provider's accepted hint.

    Empty, malformed, or provider-unsupported values fall back to ``"auto"``
    (provider-side detection) instead of failing the session at connect time.
    """

    candidate = str(user_language or "").strip()
    if not candidate:
        return "auto"
    try:
        normalized = AsrSessionConfig(language=candidate).language
    except ValueError:
        return "auto"
    if normalized == "auto":
        return "auto"
    validator = _PROVIDER_LANGUAGE_VALIDATORS.get(provider_key)
    if validator is None:
        return normalized
    try:
        validator(normalized)
    except ValueError:
        return "auto"
    return normalized


_IMPLEMENTED_WORKERS: dict[str, _AsrWorkerFn] = {
    "dummy": _dummy_asr_worker,
    "qwen": _qwen_asr_worker,
    "openai": _openai_asr_worker,
    "step": _step_asr_worker,
    "grok": _grok_asr_worker,
    "glm": _glm_asr_worker,
    "gemini": _gemini_asr_worker,
    "soniox": _soniox_asr_worker,
}


@dataclass(frozen=True, slots=True)
class _AsrSelection:
    provider_key: str
    endpointing_mode: _AsrEndpointingMode
    soniox_region: Literal["us", "eu", "jp"] | None = None
    availability: _AsrProviderAvailability = _AsrProviderAvailability.IMPLEMENTED
    _worker_fn: _AsrWorkerFn | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _api_key: str = field(default="", repr=False, compare=False)


def _load_core_config() -> dict:
    from utils.config_manager import get_config_manager

    try:
        return get_config_manager().get_core_config() or {}
    except Exception:
        return {}


def _is_explicit_intl_region(user_region: str) -> bool:
    normalized = user_region.strip().lower().replace("_", "-")
    return normalized in {
        "intl",
        "international",
        "us",
        "eu",
        "jp",
        "europe",
        "japan",
        "uk",
        "gb",
        "au",
        "nz",
        "kr",
        "sg",
    }


def _mapped_soniox_region(user_region: str) -> Literal["us", "eu", "jp"]:
    normalized = user_region.strip().lower().replace("_", "-")
    if normalized in {"eu", "europe", "uk", "gb"}:
        return "eu"
    if normalized in {"jp", "japan", "au", "nz", "kr", "sg"}:
        return "jp"
    return "us"


def _resolve_asr_selection(
    core_type: str,
    *,
    user_region: str | None = None,
    include_dev_override: bool = True,
) -> _AsrSelection:
    """Resolve one pre-audio provider choice without opening a session."""

    core_key = str(core_type or "").strip().lower()
    route = _CORE_ASR_ROUTES.get(core_key)
    if route is None:
        raise RuntimeError(f"ASR_UNKNOWN_CORE: {core_key or '<empty>'}")

    provider_override = (
        os.getenv("ASR_PROVIDER", "").strip().lower()
        if include_dev_override
        else ""
    )
    if provider_override:
        if provider_override != "dummy":
            raise RuntimeError(
                "ASR_INVALID_CONFIG: ASR_PROVIDER only supports the development "
                "value 'dummy'"
            )
        worker_fn, api_key, _provider_key = _get_asr_worker(
            core_key,
            "manual",
            provider_key_override="dummy",
            core_config={},
            require_credential=False,
        )
        return _AsrSelection(
            provider_key="dummy",
            endpointing_mode="manual",
            _worker_fn=worker_fn,
            _api_key=api_key or "",
        )

    core_config = _load_core_config()
    resolved_region = str(
        user_region
        or os.getenv("ASR_USER_REGION", "")
        or core_config.get("ASR_USER_REGION")
        or ""
    ).strip()
    soniox_key = str(
        core_config.get("SONIOX_API_KEY")
        or os.getenv("SONIOX_API_KEY", "")
        or ""
    ).strip()
    if _is_explicit_intl_region(resolved_region) and soniox_key:
        raw_soniox_region = str(
            os.getenv("SONIOX_REGION", "")
            or core_config.get("SONIOX_REGION")
            or _mapped_soniox_region(resolved_region)
        ).strip().lower()
        if raw_soniox_region not in {"us", "eu", "jp"}:
            raise RuntimeError(
                "ASR_INVALID_CONFIG: SONIOX_REGION must be us, eu, or jp"
            )
        soniox_config = {**core_config, "SONIOX_API_KEY": soniox_key}
        worker_fn, api_key, _provider_key = _get_asr_worker(
            core_key,
            "provider",
            provider_key_override="soniox",
            soniox_region=raw_soniox_region,
            core_config=soniox_config,
            require_credential=False,
        )
        return _AsrSelection(
            provider_key="soniox",
            endpointing_mode="provider",
            soniox_region=raw_soniox_region,  # type: ignore[arg-type]
            _worker_fn=worker_fn,
            _api_key=api_key or "",
        )

    provider_meta = _ASR_PROVIDER_REGISTRY.get(route.provider_key)
    if provider_meta is None:
        raise RuntimeError(f"ASR_BACKEND_NOT_IMPLEMENTED: {route.provider_key}")
    if provider_meta.availability is not _AsrProviderAvailability.IMPLEMENTED:
        return _AsrSelection(
            provider_key=route.provider_key,
            endpointing_mode=route.default_endpointing_mode,
            availability=provider_meta.availability,
        )

    worker_fn, api_key, provider_key = _get_asr_worker(
        core_key,
        route.default_endpointing_mode,
        provider_key_override=route.provider_key,
        core_config=core_config,
        require_credential=False,
    )
    availability = (
        _AsrProviderAvailability.IMPLEMENTED
        if provider_key == "dummy" or bool(api_key)
        else _AsrProviderAvailability.MISSING_CREDENTIALS
    )
    return _AsrSelection(
        provider_key=provider_key,
        endpointing_mode=route.default_endpointing_mode,
        availability=availability,
        _worker_fn=worker_fn,
        _api_key=api_key or "",
    )


def _get_default_endpointing_mode(core_type: str) -> _AsrEndpointingMode:
    return _resolve_asr_selection(core_type).endpointing_mode


def _get_asr_worker(
    core_type: str,
    endpointing_mode: _AsrEndpointingMode = "manual",
    *,
    provider_key_override: str | None = None,
    soniox_region: str | None = None,
    core_config: dict | None = None,
    require_credential: bool = True,
) -> tuple[_AsrWorkerFn, str | None, str]:
    """Resolve one worker without exposing provider internals to callers."""

    core_key = str(core_type or "").strip().lower()
    route = _CORE_ASR_ROUTES.get(core_key)
    if route is None:
        raise RuntimeError(f"ASR_UNKNOWN_CORE: {core_key or '<empty>'}")

    provider_key = provider_key_override or route.provider_key

    meta = _ASR_PROVIDER_REGISTRY.get(provider_key)
    if meta is None:
        raise RuntimeError(f"ASR_BACKEND_NOT_IMPLEMENTED: {provider_key}")
    if endpointing_mode not in meta.supported_endpointing_modes:
        raise RuntimeError(
            "ASR_ENDPOINTING_NOT_SUPPORTED: "
            f"{provider_key} does not support {endpointing_mode}"
        )
    if meta.implementation_status == "blocked_backend":
        raise RuntimeError(f"ASR_BACKEND_BLOCKED: {core_key}")
    if meta.implementation_status == "blocked_credentials":
        raise RuntimeError(f"ASR_CREDENTIALS_MISSING: {provider_key}")
    if meta.implementation_status != "implemented":
        raise RuntimeError(f"ASR_BACKEND_NOT_IMPLEMENTED: {core_key}")

    worker_fn = _IMPLEMENTED_WORKERS.get(provider_key)
    if worker_fn is None:
        raise RuntimeError(f"ASR_BACKEND_NOT_IMPLEMENTED: {core_key}")

    if provider_key == "dummy":
        return worker_fn, "", provider_key

    # ConfigManager owns the only permitted provider-specific credential
    # fallback: a matching Core may supply CORE_API_KEY through its matching
    # ASSIST_API_KEY_* slot. No audio/TTS/other-provider key is consulted here.
    resolved_core_config = _load_core_config() if core_config is None else core_config
    credential_field = (
        "SONIOX_API_KEY" if provider_key == "soniox" else route.credential_field
    )
    api_key = str(
        resolved_core_config.get(credential_field)
        or (os.getenv("SONIOX_API_KEY", "") if provider_key == "soniox" else "")
        or ""
    ).strip()
    if not api_key and require_credential:
        raise RuntimeError(f"ASR_CREDENTIALS_MISSING: {provider_key}")

    if provider_key == "qwen":
        worker_fn = partial(worker_fn, region=route.region or "cn")  # type: ignore[assignment]
    elif provider_key == "soniox":
        worker_fn = partial(
            worker_fn,
            region=soniox_region or "us",
            replay_policy=meta.replay_policy,
        )  # type: ignore[assignment]
    return worker_fn, api_key, provider_key


def create_asr_session(
    core_type: str,
    *,
    config: AsrSessionConfig | None = None,
    on_input_transcript: Callable[[str], Awaitable[None]],
    on_connection_error: Callable[[str], Awaitable[None]],
    on_status_message: Callable[[str], Awaitable[None]] | None = None,
    on_speech_activity: Callable[[SpeechActivityEvent], Awaitable[None]] | None = None,
    on_turn_endpointed: Callable[[], Awaitable[None]] | None = None,
    external_endpointing_runtime: bool = False,
    user_region: str | None = None,
    user_language: str | None = None,
) -> RealtimeAsrSession:
    """Create an isolated ASR session or fail fast for unsupported routes."""

    if config is not None and not isinstance(config, AsrSessionConfig):
        raise TypeError("ASR_INVALID_CONFIG: config must be AsrSessionConfig")
    selection = _resolve_asr_selection(core_type, user_region=user_region)
    return _create_asr_session_from_selection(
        core_type,
        selection=selection,
        config=config,
        user_language=user_language,
        on_input_transcript=on_input_transcript,
        on_connection_error=on_connection_error,
        on_status_message=on_status_message,
        on_speech_activity=on_speech_activity,
        on_turn_endpointed=on_turn_endpointed,
        external_endpointing_runtime=external_endpointing_runtime,
    )


def _create_asr_session_from_selection(
    core_type: str,
    *,
    selection: _AsrSelection,
    config: AsrSessionConfig | None = None,
    on_input_transcript: Callable[[str], Awaitable[None]],
    on_connection_error: Callable[[str], Awaitable[None]],
    on_status_message: Callable[[str], Awaitable[None]] | None = None,
    on_speech_activity: Callable[[SpeechActivityEvent], Awaitable[None]] | None = None,
    on_turn_endpointed: Callable[[], Awaitable[None]] | None = None,
    on_provider_utterance_started: Callable[
        [ProviderUtteranceStartedNotification],
        Awaitable[ProviderStartedSettlement | None],
    ]
    | None = None,
    on_provider_endpoint: Callable[[ProviderEndpointNotification], Awaitable[None]]
    | None = None,
    on_provider_final: Callable[[ProviderUtteranceKey, str], Awaitable[None]]
    | None = None,
    on_provider_final_ready: Callable[
        [ProviderFinalNotification], Awaitable[None]
    ]
    | None = None,
    external_endpointing_runtime: bool = False,
    user_language: str | None = None,
) -> RealtimeAsrSession:
    """Build one session from an already-resolved, immutable selection."""

    if not isinstance(selection, _AsrSelection):
        raise TypeError("ASR_INVALID_CONFIG: selection must be _AsrSelection")
    if config is not None and not isinstance(config, AsrSessionConfig):
        raise TypeError("ASR_INVALID_CONFIG: config must be AsrSessionConfig")
    session_config = config or AsrSessionConfig(
        language=_resolve_session_language(
            selection.provider_key,
            user_language,
        ),
        endpointing_mode=selection.endpointing_mode,
    )
    provider_key = selection.provider_key
    provider_meta = _ASR_PROVIDER_REGISTRY.get(provider_key)
    if provider_meta is None:
        raise RuntimeError(f"ASR_BACKEND_NOT_IMPLEMENTED: {provider_key}")
    if selection.availability is _AsrProviderAvailability.BLOCKED_BACKEND:
        raise RuntimeError(f"ASR_BACKEND_BLOCKED: {core_type}")
    if selection.availability is _AsrProviderAvailability.MISSING_CREDENTIALS:
        raise RuntimeError(f"ASR_CREDENTIALS_MISSING: {provider_key}")
    if session_config.endpointing_mode not in provider_meta.supported_endpointing_modes:
        raise RuntimeError(
            "ASR_ENDPOINTING_NOT_SUPPORTED: "
            f"{provider_key} does not support {session_config.endpointing_mode}"
        )
    worker_fn = selection._worker_fn
    if worker_fn is None:
        raise RuntimeError(f"ASR_BACKEND_NOT_IMPLEMENTED: {provider_key}")
    if provider_key != "dummy" and not selection._api_key:
        raise RuntimeError(f"ASR_CREDENTIALS_MISSING: {provider_key}")
    provider_policy = resolve_provider_policy(
        provider_key,
        session_config.endpointing_mode,
    )

    return _RealtimeAsrSessionImpl(
        worker_fn=worker_fn,
        api_key=selection._api_key,
        config=session_config,
        on_input_transcript=on_input_transcript,
        on_connection_error=on_connection_error,
        on_status_message=on_status_message,
        on_turn_endpointed=on_turn_endpointed,
        on_provider_utterance_started=on_provider_utterance_started,
        on_provider_endpoint=on_provider_endpoint,
        on_provider_final=on_provider_final,
        on_provider_final_ready=on_provider_final_ready,
        voice_turn_factory=(
            partial(
                _create_voice_turn_adapter,
                on_activity=on_speech_activity,
                smart_turn_required=provider_policy.smart_turn_required,
            )
            if provider_policy.smart_turn_required and not external_endpointing_runtime
            else None
        ),
        provider_policy=provider_policy,
    )


def _resolve_core_follow_selection(core_type: str) -> _AsrSelection:
    """Resolve the configured Core's ASR without regional provider preference."""

    core_key = str(core_type or "").strip().lower()
    route = _CORE_ASR_ROUTES.get(core_key)
    if route is None:
        raise RuntimeError(f"ASR_UNKNOWN_CORE: {core_key or '<empty>'}")
    endpointing_mode = route.default_endpointing_mode
    core_config = _load_core_config()
    worker_fn, api_key_override, provider_key = _get_asr_worker(
        core_key,
        endpointing_mode,
        provider_key_override=route.provider_key,
        core_config=core_config,
        require_credential=False,
    )
    return _AsrSelection(
        provider_key=provider_key,
        endpointing_mode=endpointing_mode,
        _worker_fn=worker_fn,
        _api_key=api_key_override or "",
    )


def _attach_partial_callback(
    session: RealtimeAsrSession,
    callback: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Attach display-only partial delivery to the built-in session."""

    if isinstance(session, _RealtimeAsrSessionImpl):
        session._on_partial_transcript = callback
