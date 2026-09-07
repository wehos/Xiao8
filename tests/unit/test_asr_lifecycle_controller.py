from __future__ import annotations

import pytest

from main_logic.asr_client.lifecycle import (
    FinalKey,
    VoiceIngressToken,
    VoiceLifecycleEvent,
    VoiceLifecycleState,
    VoiceRouteMode,
    VoiceTurnToken,
    next_lifecycle_state,
)
from main_logic.asr_client.lifecycle import (
    AudioDisposition,
    VoiceInputLifecycleController,
)
from main_logic.asr_client.provider_policy import resolve_provider_policy


def _pcm(milliseconds: int) -> bytes:
    return b"\x01\x00" * (16_000 * milliseconds // 1_000)


def _ingress(audio_generation: int) -> VoiceIngressToken:
    return VoiceIngressToken(1, "socket", 2, 3, audio_generation)


def test_shadow_mode_observes_suppression_without_dropping_independent_asr_audio() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=True,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)

    decision = controller.accept_audio(_pcm(100), sample_rate_hz=16_000)

    assert decision.disposition is AudioDisposition.FORWARD
    assert decision.shadow_disposition is AudioDisposition.BUFFER
    assert controller.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    assert controller.metrics.local_audio_ms == 100
    assert controller.metrics.cloud_audio_ms == 0
    assert controller.metrics.shadow_suppressed_audio_ms == 100


def test_enforced_local_listen_buffers_until_speech_is_confirmed() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)

    first = controller.accept_audio(_pcm(500), sample_rate_hz=16_000)
    controller.transition(VoiceLifecycleEvent.SOFT_WAKE)
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    second = controller.accept_audio(_pcm(100), sample_rate_hz=16_000)

    assert first.disposition is AudioDisposition.BUFFER
    assert second.disposition is AudioDisposition.FORWARD_WITH_PRE_ROLL
    assert second.pre_roll == _pcm(600)
    assert controller.snapshot.state is VoiceLifecycleState.ACTIVE


def test_prewarming_uses_eight_second_pending_connect_buffer() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.accept_audio(_pcm(500), sample_rate_hz=16_000)
    controller.transition(VoiceLifecycleEvent.SOFT_WAKE)
    controller.accept_audio(_pcm(7_800), sample_rate_hz=16_000)

    assert controller.pending_connect_bytes == len(_pcm(8_000))
    assert controller.metrics.buffer_overflow_count == 1

    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    start_audio = controller.drain_active_start_audio()

    assert start_audio == _pcm(200) + _pcm(7_800)
    assert controller.pending_connect_bytes == 0


def test_prewarm_expiry_returns_to_local_listen_and_clears_buffer() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("openai", "provider"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.open_turn(_ingress(4))
    controller.accept_audio(_pcm(10), sample_rate_hz=16_000)
    assert controller.pending_connect_bytes == len(_pcm(10))

    controller.transition(VoiceLifecycleEvent.PREWARM_EXPIRED)

    assert controller.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    assert controller.pending_connect_bytes == 0
    assert controller.current_turn_token is None


def test_blocked_route_consumes_audio_without_buffer_or_forward() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("gemini", "manual"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.BLOCKED)

    decision = controller.accept_audio(_pcm(100), sample_rate_hz=16_000)

    assert decision.disposition is AudioDisposition.BLOCK
    assert decision.pre_roll == b""
    assert controller.metrics.cloud_audio_ms == 0


def test_turn_identity_is_allocated_when_speech_starts_not_when_final_arrives() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("openai", "provider"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    idle_identity = controller.identity
    controller.transition(VoiceLifecycleEvent.SOFT_WAKE)
    candidate_identity = controller.identity
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    identity = controller.identity
    controller.transition(VoiceLifecycleEvent.TURN_SEALED)
    controller.transition(VoiceLifecycleEvent.PROVIDER_FINAL)

    assert controller.snapshot.state is VoiceLifecycleState.WARM_IDLE
    assert candidate_identity.turn_id == idle_identity.turn_id + 1
    assert identity.turn_id == candidate_identity.turn_id
    assert controller.identity.turn_id == identity.turn_id
    assert controller.matches(identity) is False


def test_final_key_wraps_complete_turn_token_including_audio_generation() -> None:
    first = VoiceTurnToken(_ingress(4), turn_id=1)
    second = VoiceTurnToken(_ingress(5), turn_id=1)

    assert FinalKey.from_turn(first).turn_token is first
    assert FinalKey.from_turn(first) != FinalKey.from_turn(second)


def test_draining_audio_is_isolated_for_the_next_turn_until_old_final() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.transition(VoiceLifecycleEvent.SOFT_WAKE)
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    old_turn = controller.identity.turn_id
    controller.transition(VoiceLifecycleEvent.TURN_SEALED)

    decision = controller.accept_audio(_pcm(400), sample_rate_hz=16_000)
    controller.mark_pending_turn_speech()

    assert decision.disposition is AudioDisposition.BUFFER
    assert controller.pending_turn_bytes == len(_pcm(400))
    assert controller.identity.turn_id == old_turn

    controller.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    pending = controller.begin_pending_turn()

    assert controller.snapshot.state is VoiceLifecycleState.ACTIVE
    assert controller.identity.turn_id == old_turn + 1
    assert pending == _pcm(400)
    assert controller.pending_turn_bytes == 0


def test_pending_turn_token_keeps_ingress_captured_at_mark_time() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    first = controller.open_turn(_ingress(4))
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    controller.transition(VoiceLifecycleEvent.TURN_SEALED)
    controller.accept_audio(_pcm(400), sample_rate_hz=16_000)

    pending = controller.mark_pending_turn_speech(_ingress(5))
    assert pending is not None
    controller.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    payload = controller.begin_pending_turn()

    assert payload == _pcm(400)
    assert controller.current_turn_token is pending
    assert controller.current_turn_token.ingress == _ingress(5)
    assert controller.current_turn_token.turn_id > first.turn_id


def test_pending_turn_overflow_discards_entire_candidate() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("openai", "provider"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.transition(VoiceLifecycleEvent.SOFT_WAKE)
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    controller.transition(VoiceLifecycleEvent.TURN_SEALED)

    controller.mark_pending_turn_speech()
    decision = controller.accept_audio(_pcm(9_000), sample_rate_hz=16_000)

    assert decision.disposition is AudioDisposition.BLOCK
    assert decision.backpressure is True
    assert controller.pending_turn_bytes == 0
    assert controller.has_pending_turn is False
    assert controller.metrics.buffer_overflow_count == 1


def test_stop_clears_audio_and_invalidates_all_async_identity() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.accept_audio(_pcm(100), sample_rate_hz=16_000)
    identity = controller.identity

    controller.stop()

    assert controller.snapshot.state is VoiceLifecycleState.OFF
    assert controller.matches(identity) is False
    assert controller.pre_roll_bytes == 0
    assert controller.pending_turn_bytes == 0


@pytest.mark.parametrize(
    "events",
    [
        (),
        (VoiceLifecycleEvent.SOFT_WAKE,),
        (VoiceLifecycleEvent.SOFT_WAKE, VoiceLifecycleEvent.SPEECH_CONFIRMED),
        (
            VoiceLifecycleEvent.SOFT_WAKE,
            VoiceLifecycleEvent.SPEECH_CONFIRMED,
            VoiceLifecycleEvent.TURN_SEALED,
        ),
        (
            VoiceLifecycleEvent.SOFT_WAKE,
            VoiceLifecycleEvent.SPEECH_CONFIRMED,
            VoiceLifecycleEvent.TURN_SEALED,
            VoiceLifecycleEvent.PROVIDER_FINAL,
        ),
    ],
)
def test_hard_block_records_incident_and_clears_all_audio(events) -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.accept_audio(_pcm(100), sample_rate_hz=16_000)
    for event in events:
        controller.transition(event)
        if event is VoiceLifecycleEvent.TURN_SEALED:
            controller.accept_audio(_pcm(100), sample_rate_hz=16_000)
            controller.mark_pending_turn_speech()
    before = controller.snapshot

    state = controller.block(
        reason_code="ASR_DENY_CLEANUP_FAILED",
        incident_id="incident-1",
    )
    after = controller.snapshot

    assert state is VoiceLifecycleState.BLOCKED
    assert after.route_mode is VoiceRouteMode.BLOCKED
    assert after.transport_generation == before.transport_generation + 1
    assert after.lifecycle_revision == before.lifecycle_revision + 1
    assert after.reason_code == "ASR_DENY_CLEANUP_FAILED"
    assert after.incident_id == "incident-1"
    assert controller.pre_roll_bytes == 0
    assert controller.pending_connect_bytes == 0
    assert controller.pending_turn_bytes == 0
    assert controller.current_turn_token is None
    assert controller.has_pending_turn is False


def test_hard_block_is_idempotent_only_for_the_same_incident() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.block(
        reason_code="ASR_DENY_CLEANUP_FAILED",
        incident_id="incident-1",
    )
    blocked = controller.snapshot

    assert controller.block(
        reason_code="ASR_DENY_CLEANUP_FAILED",
        incident_id="incident-1",
    ) is VoiceLifecycleState.BLOCKED
    assert controller.snapshot == blocked
    with pytest.raises(RuntimeError, match="VOICE_LIFECYCLE_BLOCKED_INCIDENT_CONFLICT"):
        controller.block(
            reason_code="ASR_DENY_CLEANUP_FAILED",
            incident_id="incident-2",
        )


def test_detector_failure_fails_open_only_to_continuous_independent_asr() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    buffered = controller.accept_audio(_pcm(100), sample_rate_hz=16_000)

    controller.enable_independent_asr_fail_open()
    local_listen = controller.accept_audio(_pcm(20), sample_rate_hz=16_000)
    controller.transition(VoiceLifecycleEvent.SOFT_WAKE)
    prewarming = controller.accept_audio(_pcm(20), sample_rate_hz=16_000)
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    first = controller.accept_audio(_pcm(20), sample_rate_hz=16_000)
    second = controller.accept_audio(_pcm(20), sample_rate_hz=16_000)

    assert buffered.disposition is AudioDisposition.BUFFER
    assert local_listen.disposition is AudioDisposition.BUFFER
    assert prewarming.disposition is AudioDisposition.BUFFER
    assert first.disposition is AudioDisposition.FORWARD_WITH_PRE_ROLL
    assert first.pre_roll == _pcm(160)
    assert second.disposition is AudioDisposition.FORWARD
    assert controller.snapshot.state is VoiceLifecycleState.ACTIVE
    assert controller.snapshot.route_mode is VoiceRouteMode.INDEPENDENT


def test_game_takeover_suspends_active_turn_and_clears_audio() -> None:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "manual"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.accept_audio(_pcm(100), sample_rate_hz=16_000)
    controller.open_turn(_ingress(4))
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    old_identity = controller.identity

    controller.transition(VoiceLifecycleEvent.GAME_TAKEOVER)
    blocked = controller.accept_audio(_pcm(20), sample_rate_hz=16_000)

    assert controller.snapshot.state is VoiceLifecycleState.SUSPENDED
    assert controller.pre_roll_bytes == 0
    assert blocked.disposition is AudioDisposition.BLOCK
    assert controller.matches(old_identity) is False
    assert controller.current_turn_token is None

    controller.transition(VoiceLifecycleEvent.GAME_RELEASED)
    assert controller.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN


def _controller_in_turn_denied_source_state(
    state: VoiceLifecycleState,
) -> VoiceInputLifecycleController:
    controller = VoiceInputLifecycleController(
        provider_policy=resolve_provider_policy("qwen", "provider"),
        shadow_mode=False,
    )
    controller.open(route_mode=VoiceRouteMode.INDEPENDENT)
    controller.accept_audio(_pcm(100), sample_rate_hz=16_000)
    controller.open_turn(_ingress(6))
    controller.accept_audio(_pcm(100), sample_rate_hz=16_000)
    if state is VoiceLifecycleState.PREWARMING:
        return controller

    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    if state is VoiceLifecycleState.ACTIVE:
        return controller

    controller.transition(VoiceLifecycleEvent.TURN_SEALED)
    controller.accept_audio(_pcm(100), sample_rate_hz=16_000)
    controller.mark_pending_turn_speech(_ingress(7))
    if state is VoiceLifecycleState.DRAINING:
        return controller

    controller.transition(VoiceLifecycleEvent.PROVIDER_FINAL)
    return controller


@pytest.mark.parametrize(
    "source_state",
    (
        VoiceLifecycleState.PREWARMING,
        VoiceLifecycleState.ACTIVE,
        VoiceLifecycleState.DRAINING,
        VoiceLifecycleState.WARM_IDLE,
    ),
)
def test_turn_denied_returns_to_clean_local_listen(
    source_state: VoiceLifecycleState,
) -> None:
    controller = _controller_in_turn_denied_source_state(source_state)
    old_identity = controller.identity
    old_transport_generation = controller.snapshot.transport_generation

    result = controller.transition(VoiceLifecycleEvent.TURN_DENIED)

    assert result is VoiceLifecycleState.LOCAL_LISTEN
    assert controller.snapshot.state is VoiceLifecycleState.LOCAL_LISTEN
    assert controller.snapshot.route_mode is VoiceRouteMode.INDEPENDENT
    assert controller.snapshot.transport_generation == old_transport_generation + 1
    assert controller.identity.turn_id > old_identity.turn_id
    assert controller.matches(old_identity) is False
    assert controller.current_turn_token is None
    assert controller.pending_turn_token is None
    assert controller.pre_roll_bytes == 0
    assert controller.pending_connect_bytes == 0
    assert controller.pending_turn_bytes == 0
    assert controller.has_pending_turn is False

    controller.transition(VoiceLifecycleEvent.SOFT_WAKE)
    controller.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
    next_audio = controller.accept_audio(_pcm(20), sample_rate_hz=16_000)
    assert next_audio.disposition is AudioDisposition.FORWARD_WITH_PRE_ROLL
    assert next_audio.pre_roll == _pcm(20)


@pytest.mark.parametrize(
    "source_state",
    (
        VoiceLifecycleState.OFF,
        VoiceLifecycleState.LOCAL_LISTEN,
        VoiceLifecycleState.DEEP_SLEEP,
        VoiceLifecycleState.BACKOFF,
        VoiceLifecycleState.BLOCKED,
        VoiceLifecycleState.SUSPENDED,
    ),
)
def test_turn_denied_rejects_states_without_provider_turn(
    source_state: VoiceLifecycleState,
) -> None:
    with pytest.raises(RuntimeError, match="VOICE_LIFECYCLE_INVALID_TRANSITION"):
        next_lifecycle_state(source_state, VoiceLifecycleEvent.TURN_DENIED)
