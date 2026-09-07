from __future__ import annotations

import json
from pathlib import Path

import pytest

from main_logic.asr_client import VoiceIdentityActivationResult
from main_logic.voice_identity_service.state import (
    VoiceIdentityEffectiveReason,
    VoiceIdentityState,
)


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
def test_pending_retains_desired_configuration_without_claiming_ready() -> None:
    state = VoiceIdentityState(
        requested_enabled=True,
        effective_enabled=False,
        effective_reason=VoiceIdentityEffectiveReason.ACTIVATION_PENDING,
        has_profile=True,
    )
    assert state.as_dict() == {
        "requested_enabled": True,
        "effective_enabled": False,
        "effective_reason": "activation_pending",
        "has_profile": True,
    }
    assert VoiceIdentityActivationResult.ACTIVATION_PENDING.value == "activation_pending"
    with pytest.raises(ValueError, match="must be ready"):
        VoiceIdentityState(
            requested_enabled=True,
            effective_enabled=True,
            effective_reason=VoiceIdentityEffectiveReason.ACTIVATION_PENDING,
            has_profile=True,
        )


@pytest.mark.unit
@pytest.mark.parametrize("locale", ["en", "ja", "ko", "zh-CN", "zh-TW", "ru", "pt", "es"])
def test_pending_reason_is_available_in_every_shared_ui_locale(locale: str) -> None:
    catalog = json.loads((ROOT / "static/locales" / f"{locale}.json").read_text(encoding="utf-8"))
    messages = catalog["voiceIdentity"]
    pending = messages["reasonActivationPending"]
    assert pending and pending != messages["profileReady"]
    assert pending != messages["reasonRuntimeDegraded"]


@pytest.mark.unit
def test_web_and_desktop_shared_state_script_has_pending_message() -> None:
    script = (ROOT / "static/js/voice_identity.js").read_text(encoding="utf-8")
    template = (ROOT / "templates/voice_identity.html").read_text(encoding="utf-8")
    assert "activation_pending: 'voiceIdentity.reasonActivationPending'" in script
    assert "translate(EFFECTIVE_REASON_KEYS.activation_pending, '设置已保存，等待语音链路就绪')" in script
    assert "/static/js/voice_identity.js" in template
