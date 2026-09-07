"""Provider-neutral contracts and lifecycle ownership for independent ASR."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum

from main_logic.voice_turn.contracts import (
    VoiceIngressToken,
    VoiceTurnToken,
)

from .audio import AudioRingBuffer
from .provider_policy import AsrProviderPolicy


@dataclass(frozen=True, slots=True)
class VoiceTransportToken:
    """Bind Provider I/O and callbacks to one transport attempt."""

    turn: VoiceTurnToken
    transport_generation: int


@dataclass(frozen=True, slots=True)
class FinalKey:
    """Logical final identity; transport retries do not create a new turn."""

    turn_token: VoiceTurnToken

    @classmethod
    def from_turn(cls, token: VoiceTurnToken) -> "FinalKey":
        return cls(turn_token=token)


class VoiceRouteMode(Enum):
    """The microphone either belongs to independent ASR or is fail-closed."""

    INDEPENDENT = "independent"
    BLOCKED = "blocked"


class VoiceLifecycleState(Enum):
    OFF = "off"
    LOCAL_LISTEN = "local_listen"
    PREWARMING = "prewarming"
    ACTIVE = "active"
    DRAINING = "draining"
    WARM_IDLE = "warm_idle"
    DEEP_SLEEP = "deep_sleep"
    BACKOFF = "backoff"
    BLOCKED = "blocked"
    SUSPENDED = "suspended"


class VoiceLifecycleEvent(Enum):
    MIC_OPENED = "mic_opened"
    SOFT_WAKE = "soft_wake"
    SPEECH_CONFIRMED = "speech_confirmed"
    CONNECT_FAILED = "connect_failed"
    RETRY = "retry"
    RETRIES_EXHAUSTED = "retries_exhausted"
    TURN_SEALED = "turn_sealed"
    # 兼容旧调用方；新代码应使用 TURN_SEALED，明确区分语义端点和 provider final。
    TURN_ENDPOINTED = "turn_endpointed"
    PROVIDER_FINAL = "provider_final"
    TURN_DENIED = "turn_denied"
    PREWARM_EXPIRED = "prewarm_expired"
    WARM_EXPIRED = "warm_expired"
    RECOVERED = "recovered"
    GAME_TAKEOVER = "game_takeover"
    GAME_RELEASED = "game_released"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class VoiceLifecycleConfig:
    pre_roll_ms: int = 700
    confirm_speech_ms: int = 240
    candidate_pause_ms: int = 320
    trailing_audio_ms: int = 400
    pending_audio_ms: int = 8_000
    default_warm_transport_ms: int = 25_000
    smart_turn_warm_ms: int = 60_000

    def __post_init__(self) -> None:
        for field_name in (
            "pre_roll_ms",
            "confirm_speech_ms",
            "candidate_pause_ms",
            "trailing_audio_ms",
            "pending_audio_ms",
            "smart_turn_warm_ms",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.default_warm_transport_ms < 0:
            raise ValueError("default_warm_transport_ms must not be negative")
        if self.pre_roll_ms > self.pending_audio_ms:
            raise ValueError("pre_roll_ms cannot exceed pending_audio_ms")


@dataclass(frozen=True, slots=True)
class VoiceLifecycleSnapshot:
    state: VoiceLifecycleState
    route_mode: VoiceRouteMode
    route_generation: int
    transport_generation: int
    turn_id: int
    lifecycle_revision: int
    reason_code: str | None
    incident_id: str | None


_TRANSITIONS: dict[
    tuple[VoiceLifecycleState, VoiceLifecycleEvent], VoiceLifecycleState
] = {
    (VoiceLifecycleState.OFF, VoiceLifecycleEvent.MIC_OPENED): VoiceLifecycleState.LOCAL_LISTEN,
    (VoiceLifecycleState.LOCAL_LISTEN, VoiceLifecycleEvent.SOFT_WAKE): VoiceLifecycleState.PREWARMING,
    (VoiceLifecycleState.PREWARMING, VoiceLifecycleEvent.SPEECH_CONFIRMED): VoiceLifecycleState.ACTIVE,
    (VoiceLifecycleState.PREWARMING, VoiceLifecycleEvent.CONNECT_FAILED): VoiceLifecycleState.BACKOFF,
    (VoiceLifecycleState.ACTIVE, VoiceLifecycleEvent.TURN_SEALED): VoiceLifecycleState.DRAINING,
    (VoiceLifecycleState.ACTIVE, VoiceLifecycleEvent.TURN_ENDPOINTED): VoiceLifecycleState.DRAINING,
    (VoiceLifecycleState.DRAINING, VoiceLifecycleEvent.PROVIDER_FINAL): VoiceLifecycleState.WARM_IDLE,
    (VoiceLifecycleState.PREWARMING, VoiceLifecycleEvent.TURN_DENIED): VoiceLifecycleState.LOCAL_LISTEN,
    (VoiceLifecycleState.ACTIVE, VoiceLifecycleEvent.TURN_DENIED): VoiceLifecycleState.LOCAL_LISTEN,
    (VoiceLifecycleState.DRAINING, VoiceLifecycleEvent.TURN_DENIED): VoiceLifecycleState.LOCAL_LISTEN,
    (VoiceLifecycleState.WARM_IDLE, VoiceLifecycleEvent.TURN_DENIED): VoiceLifecycleState.LOCAL_LISTEN,
    (VoiceLifecycleState.WARM_IDLE, VoiceLifecycleEvent.SOFT_WAKE): VoiceLifecycleState.PREWARMING,
    (VoiceLifecycleState.WARM_IDLE, VoiceLifecycleEvent.SPEECH_CONFIRMED): VoiceLifecycleState.ACTIVE,
    (VoiceLifecycleState.PREWARMING, VoiceLifecycleEvent.PREWARM_EXPIRED): VoiceLifecycleState.LOCAL_LISTEN,
    (VoiceLifecycleState.WARM_IDLE, VoiceLifecycleEvent.WARM_EXPIRED): VoiceLifecycleState.DEEP_SLEEP,
    (VoiceLifecycleState.LOCAL_LISTEN, VoiceLifecycleEvent.WARM_EXPIRED): VoiceLifecycleState.DEEP_SLEEP,
    (VoiceLifecycleState.DEEP_SLEEP, VoiceLifecycleEvent.SOFT_WAKE): VoiceLifecycleState.PREWARMING,
    (VoiceLifecycleState.BACKOFF, VoiceLifecycleEvent.RETRY): VoiceLifecycleState.PREWARMING,
    (VoiceLifecycleState.BACKOFF, VoiceLifecycleEvent.RETRIES_EXHAUSTED): VoiceLifecycleState.BLOCKED,
    (VoiceLifecycleState.BLOCKED, VoiceLifecycleEvent.RECOVERED): VoiceLifecycleState.LOCAL_LISTEN,
    (VoiceLifecycleState.LOCAL_LISTEN, VoiceLifecycleEvent.GAME_TAKEOVER): VoiceLifecycleState.SUSPENDED,
    (VoiceLifecycleState.PREWARMING, VoiceLifecycleEvent.GAME_TAKEOVER): VoiceLifecycleState.SUSPENDED,
    (VoiceLifecycleState.ACTIVE, VoiceLifecycleEvent.GAME_TAKEOVER): VoiceLifecycleState.SUSPENDED,
    (VoiceLifecycleState.DRAINING, VoiceLifecycleEvent.GAME_TAKEOVER): VoiceLifecycleState.SUSPENDED,
    (VoiceLifecycleState.WARM_IDLE, VoiceLifecycleEvent.GAME_TAKEOVER): VoiceLifecycleState.SUSPENDED,
    (VoiceLifecycleState.DEEP_SLEEP, VoiceLifecycleEvent.GAME_TAKEOVER): VoiceLifecycleState.SUSPENDED,
    (VoiceLifecycleState.BACKOFF, VoiceLifecycleEvent.GAME_TAKEOVER): VoiceLifecycleState.SUSPENDED,
    (VoiceLifecycleState.SUSPENDED, VoiceLifecycleEvent.GAME_RELEASED): VoiceLifecycleState.LOCAL_LISTEN,
}


def next_lifecycle_state(
    state: VoiceLifecycleState,
    event: VoiceLifecycleEvent,
) -> VoiceLifecycleState:
    """Apply one explicit transition; user stop wins from every live state."""

    if event is VoiceLifecycleEvent.STOPPED and state is not VoiceLifecycleState.OFF:
        return VoiceLifecycleState.OFF
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError as exc:
        raise RuntimeError(
            "VOICE_LIFECYCLE_INVALID_TRANSITION: "
            f"{state.value} + {event.value}"
        ) from exc


@dataclass(slots=True)
class VoiceLifecycleMetrics:
    local_audio_ms: int = 0
    cloud_audio_ms: int = 0
    suppressed_silence_ms: int = 0
    shadow_suppressed_audio_ms: int = 0
    wake_candidate_count: int = 0
    wake_confirmed_count: int = 0
    false_wake_count: int = 0
    buffer_overflow_count: int = 0
    queue_backpressure_count: int = 0
    reconnect_count: int = 0
    stale_callback_count: int = 0
    omni_mic_audio_bytes: int = 0
    provider_wire_audio_ms: int = 0
    connect_latency_ms: int = 0
    first_partial_latency_ms: int = 0
    final_latency_ms: int = 0
    warm_hit_count: int = 0
    smart_turn_load_ms: int = 0
    smart_turn_inference_ms: int = 0
    detector_submit_latency_ms: int = 0
    detector_queue_audio_ms: int = 0
    detector_queue_high_water_ms: int = 0
    detector_oldest_frame_age_ms: int = 0
    detector_overflow_count: int = 0
    smart_turn_stale_result_count: int = 0
    smart_turn_coalesced_evaluation_count: int = 0
    detector_stale_event_count: int = 0
    asr_audio_command_queue_ms: int = 0
    asr_abort_discarded_command_count: int = 0
    provider_wire_sequence: int = 0

    @property
    def throttle_ratio(self) -> float:
        if self.local_audio_ms <= 0:
            return 0.0
        suppressed = max(0, self.local_audio_ms - self.cloud_audio_ms)
        return min(1.0, suppressed / self.local_audio_ms)

    def add_local_audio(self, duration_ms: int) -> None:
        self.local_audio_ms += max(0, int(duration_ms))

    def add_cloud_audio(self, duration_ms: int) -> None:
        self.cloud_audio_ms += max(0, int(duration_ms))

    def add_provider_wire_audio(self, duration_ms: int) -> None:
        value = max(0, int(duration_ms))
        self.provider_wire_audio_ms += value
        self.cloud_audio_ms += value

    def add_suppressed_audio(self, duration_ms: int, *, shadow: bool = False) -> None:
        value = max(0, int(duration_ms))
        if shadow:
            self.shadow_suppressed_audio_ms += value
        else:
            self.suppressed_silence_ms += value

    def add_omni_microphone_bytes(self, byte_count: int) -> None:
        value = max(0, int(byte_count))
        if value:
            raise RuntimeError(
                "OMNI_MICROPHONE_ROUTE_FORBIDDEN: microphone PCM belongs to independent ASR"
            )

    def snapshot(self) -> dict[str, int | float]:
        result: dict[str, int | float] = asdict(self)
        result["throttle_ratio"] = self.throttle_ratio
        return result


class AudioDisposition(Enum):
    FORWARD = "forward"
    FORWARD_WITH_PRE_ROLL = "forward_with_pre_roll"
    BUFFER = "buffer"
    SUPPRESS = "suppress"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class VoiceAsyncIdentity:
    route_generation: int
    transport_generation: int
    turn_id: int


@dataclass(frozen=True, slots=True)
class AudioDecision:
    disposition: AudioDisposition
    pre_roll: bytes = b""
    shadow_disposition: AudioDisposition | None = None
    backpressure: bool = False


class VoiceInputLifecycleController:
    """Keep routing, lifecycle, and audio gating as separate decisions."""

    def __init__(
        self,
        *,
        provider_policy: AsrProviderPolicy,
        config: VoiceLifecycleConfig | None = None,
        shadow_mode: bool = True,
        resource_optimization_enabled: bool = True,
    ) -> None:
        self.provider_policy = provider_policy
        self.config = config or VoiceLifecycleConfig()
        self.shadow_mode = bool(shadow_mode)
        self.metrics = VoiceLifecycleMetrics()
        self._state = VoiceLifecycleState.OFF
        self._route_mode = VoiceRouteMode.BLOCKED
        self._route_generation = 0
        self._transport_generation = 0
        self._lifecycle_revision = 0
        self._reason_code: str | None = None
        self._incident_id: str | None = None
        # Candidate identity is allocated at SOFT_WAKE so pre-roll and the
        # confirmation frame keep one turn id.
        self._turn_sequence = 0
        self._turn_id = 0
        self._completed_turn_id = -1
        self._current_turn_token: VoiceTurnToken | None = None
        self._pre_roll = AudioRingBuffer(
            capacity_ms=self.config.pre_roll_ms,
            sample_rate_hz=16_000,
        )
        self._pre_roll_sent_for_turn = False
        self._pending_turn = AudioRingBuffer(
            capacity_ms=self.config.pending_audio_ms,
            sample_rate_hz=16_000,
        )
        self._pending_turn_speech = False
        self._pending_turn_id: int | None = None
        self._pending_turn_token: VoiceTurnToken | None = None
        self._pending_connect = AudioRingBuffer(
            capacity_ms=self.config.pending_audio_ms,
            sample_rate_hz=16_000,
        )
        self._active_start_audio = b""
        self._independent_asr_fail_open = not bool(resource_optimization_enabled)

    @property
    def snapshot(self) -> VoiceLifecycleSnapshot:
        return VoiceLifecycleSnapshot(
            state=self._state,
            route_mode=self._route_mode,
            route_generation=self._route_generation,
            transport_generation=self._transport_generation,
            turn_id=self._turn_id,
            lifecycle_revision=self._lifecycle_revision,
            reason_code=self._reason_code,
            incident_id=self._incident_id,
        )

    @property
    def identity(self) -> VoiceAsyncIdentity:
        return VoiceAsyncIdentity(
            route_generation=self._route_generation,
            transport_generation=self._transport_generation,
            turn_id=self._turn_id,
        )

    @property
    def pre_roll_bytes(self) -> int:
        return len(self._pre_roll.peek())

    @property
    def pending_turn_bytes(self) -> int:
        return len(self._pending_turn.peek())

    @property
    def pending_connect_bytes(self) -> int:
        return len(self._pending_connect.peek())

    @property
    def has_pending_turn(self) -> bool:
        return self._pending_turn_speech and self.pending_turn_bytes > 0

    @property
    def has_pending_turn_identity(self) -> bool:
        """Return whether a successor turn is known even if PCM is already on wire."""

        return self._pending_turn_speech and self._pending_turn_token is not None

    @property
    def current_turn_token(self) -> VoiceTurnToken | None:
        return self._current_turn_token

    @property
    def pending_turn_token(self) -> VoiceTurnToken | None:
        return self._pending_turn_token

    def bind_current_turn_token(
        self,
        ingress_token: VoiceIngressToken,
    ) -> VoiceTurnToken:
        """Bind the current lifecycle turn once to its complete ingress identity."""

        if not isinstance(ingress_token, VoiceIngressToken):
            raise TypeError("VOICE_TURN_INGRESS_TOKEN_INVALID")
        current = self._current_turn_token
        if current is None:
            current = VoiceTurnToken(ingress=ingress_token, turn_id=self._turn_id)
            self._current_turn_token = current
        elif current.turn_id != self._turn_id or current.ingress != ingress_token:
            raise RuntimeError("VOICE_TURN_TOKEN_ALREADY_BOUND")
        return current

    def open_turn(self, ingress_token: VoiceIngressToken) -> VoiceTurnToken:
        """Allocate and bind one local candidate at speech onset."""

        self.transition(VoiceLifecycleEvent.SOFT_WAKE)
        return self.bind_current_turn_token(ingress_token)

    def open(self, *, route_mode: VoiceRouteMode) -> None:
        if self._state is not VoiceLifecycleState.OFF:
            raise RuntimeError("VOICE_LIFECYCLE_ALREADY_OPEN")
        self._route_generation += 1
        self._route_mode = route_mode
        self._reason_code = None
        self._incident_id = None
        self._state = next_lifecycle_state(
            self._state,
            VoiceLifecycleEvent.MIC_OPENED,
        )
        if route_mode is VoiceRouteMode.BLOCKED:
            self._state = VoiceLifecycleState.BLOCKED
        self._lifecycle_revision += 1

    def transition(self, event: VoiceLifecycleEvent) -> VoiceLifecycleState:
        self._state = next_lifecycle_state(self._state, event)
        if event is VoiceLifecycleEvent.SOFT_WAKE:
            self._turn_id = self._allocate_turn_id()
            self._current_turn_token = None
            self.metrics.wake_candidate_count += 1
            existing_pre_roll = self._pre_roll.drain()
            if existing_pre_roll:
                self._pending_connect.append(existing_pre_roll)
        elif event is VoiceLifecycleEvent.SPEECH_CONFIRMED:
            if self._turn_id <= self._completed_turn_id:
                self._turn_id = self._allocate_turn_id()
                self._current_turn_token = None
            self.metrics.wake_confirmed_count += 1
            if self._pre_roll.peek():
                self._pending_connect.append(self._pre_roll.drain())
            self._active_start_audio = self._pending_connect.drain()
        elif event is VoiceLifecycleEvent.CONNECT_FAILED:
            self._transport_generation += 1
        elif event is VoiceLifecycleEvent.PROVIDER_FINAL:
            self._completed_turn_id = self._turn_id
            self._pre_roll_sent_for_turn = False
            self._pre_roll.clear()
        elif event is VoiceLifecycleEvent.TURN_DENIED:
            self._transport_generation += 1
            self._turn_id = self._allocate_turn_id()
            self._current_turn_token = None
            self._completed_turn_id = self._turn_id
            self._pre_roll_sent_for_turn = False
            self._pre_roll.clear()
            self._pending_turn.clear()
            self._pending_turn_speech = False
            self._pending_turn_id = None
            self._pending_turn_token = None
            self._pending_connect.clear()
            self._active_start_audio = b""
        elif event is VoiceLifecycleEvent.PREWARM_EXPIRED:
            self._current_turn_token = None
            self._pre_roll_sent_for_turn = False
            self._pre_roll.clear()
            self._pending_connect.clear()
            self._active_start_audio = b""
        elif event is VoiceLifecycleEvent.GAME_TAKEOVER:
            self._turn_id = self._allocate_turn_id()
            self._current_turn_token = None
            self._completed_turn_id = self._turn_id
            self._pre_roll_sent_for_turn = False
            self._pre_roll.clear()
            self._pending_turn.clear()
            self._pending_turn_speech = False
            self._pending_turn_id = None
            self._pending_turn_token = None
            self._pending_connect.clear()
            self._active_start_audio = b""
        elif event is VoiceLifecycleEvent.RECOVERED:
            self._reason_code = None
            self._incident_id = None
        self._lifecycle_revision += 1
        return self._state

    def block(self, *, reason_code: str, incident_id: str) -> VoiceLifecycleState:
        """Quarantine one live route after a hard cleanup failure.

        The incident identity is immutable once BLOCKED. An exact retry is an
        idempotent read; a different failure cannot overwrite the first
        incident that owns the quarantined route.
        """

        normalized_reason = str(reason_code or "").strip()
        normalized_incident = str(incident_id or "").strip()
        if not normalized_reason:
            raise ValueError("VOICE_LIFECYCLE_BLOCK_REASON_REQUIRED")
        if not normalized_incident:
            raise ValueError("VOICE_LIFECYCLE_BLOCK_INCIDENT_REQUIRED")
        if self._state is VoiceLifecycleState.BLOCKED:
            if (
                self._reason_code == normalized_reason
                and self._incident_id == normalized_incident
            ):
                return self._state
            raise RuntimeError("VOICE_LIFECYCLE_BLOCKED_INCIDENT_CONFLICT")
        if self._state not in {
            VoiceLifecycleState.LOCAL_LISTEN,
            VoiceLifecycleState.PREWARMING,
            VoiceLifecycleState.ACTIVE,
            VoiceLifecycleState.DRAINING,
            VoiceLifecycleState.WARM_IDLE,
        }:
            raise RuntimeError(
                "VOICE_LIFECYCLE_BLOCK_REQUIRES_LIVE_ROUTE: "
                f"{self._state.value}"
            )
        self._state = VoiceLifecycleState.BLOCKED
        self._route_mode = VoiceRouteMode.BLOCKED
        self._route_generation += 1
        self._transport_generation += 1
        self._lifecycle_revision += 1
        self._reason_code = normalized_reason
        self._incident_id = normalized_incident
        self._turn_id = self._allocate_turn_id()
        self._current_turn_token = None
        self._completed_turn_id = self._turn_id
        self._pre_roll_sent_for_turn = False
        self._pre_roll.clear()
        self._pending_turn.clear()
        self._pending_turn_speech = False
        self._pending_turn_id = None
        self._pending_turn_token = None
        self._pending_connect.clear()
        self._active_start_audio = b""
        return self._state

    def accept_audio(self, pcm16: bytes, *, sample_rate_hz: int) -> AudioDecision:
        if not isinstance(pcm16, bytes):
            raise TypeError("PCM16 audio must be bytes")
        if len(pcm16) % 2:
            raise ValueError("PCM16 audio must contain complete samples")
        if sample_rate_hz != 16_000:
            raise ValueError("lifecycle controller requires normalized 16 kHz PCM16")
        duration_ms = len(pcm16) * 1_000 // (sample_rate_hz * 2)
        self.metrics.add_local_audio(duration_ms)

        if (
            self._route_mode is VoiceRouteMode.BLOCKED
            or self._state
            in {
                VoiceLifecycleState.OFF,
                VoiceLifecycleState.BLOCKED,
                VoiceLifecycleState.SUSPENDED,
            }
        ):
            return AudioDecision(AudioDisposition.BLOCK)

        # 已 seal 的旧轮次绝不能再接收麦克风音频。DRAINING 期间到达的
        # PCM 只进入下一轮的有界内存缓冲，等待旧 final 后再启动新 turn。
        if self._state is VoiceLifecycleState.DRAINING:
            dropped = self._pending_turn.append(
                pcm16,
                sample_rate_hz=sample_rate_hz,
            )
            if dropped:
                self.metrics.buffer_overflow_count += 1
                self._pending_turn.clear()
                self._pending_turn_speech = False
                self._pending_turn_id = None
                self._pending_turn_token = None
                self.metrics.add_suppressed_audio(duration_ms)
                return AudioDecision(
                    AudioDisposition.BLOCK,
                    backpressure=True,
                )
            self.metrics.add_suppressed_audio(duration_ms)
            return AudioDecision(AudioDisposition.BUFFER)

        target = self._target_disposition()
        if target is AudioDisposition.BUFFER:
            target_buffer = (
                self._pending_connect
                if self._state
                in {
                    VoiceLifecycleState.PREWARMING,
                    VoiceLifecycleState.BACKOFF,
                }
                else self._pre_roll
            )
            dropped = target_buffer.append(pcm16, sample_rate_hz=sample_rate_hz)
            if dropped:
                self.metrics.buffer_overflow_count += 1
            if self.shadow_mode:
                self.metrics.add_suppressed_audio(duration_ms, shadow=True)
                return AudioDecision(
                    AudioDisposition.FORWARD,
                    shadow_disposition=AudioDisposition.BUFFER,
                )
            self.metrics.add_suppressed_audio(duration_ms)
            return AudioDecision(AudioDisposition.BUFFER)

        if target is AudioDisposition.FORWARD and not self._pre_roll_sent_for_turn:
            self._pre_roll.append(pcm16, sample_rate_hz=sample_rate_hz)
            pre_roll = self._active_start_audio + self._pre_roll.drain()
            self._active_start_audio = b""
            self._pre_roll_sent_for_turn = True
            return AudioDecision(
                AudioDisposition.FORWARD_WITH_PRE_ROLL,
                pre_roll=pre_roll,
            )

        return AudioDecision(AudioDisposition.FORWARD)

    def drain_active_start_audio(self) -> bytes:
        """Drain pre-roll and pending-connect audio as transport becomes active."""

        if self._state is not VoiceLifecycleState.ACTIVE:
            return b""
        payload, self._active_start_audio = self._active_start_audio, b""
        if not payload:
            return b""
        self._pre_roll_sent_for_turn = True
        return payload

    def record_provider_wire_audio(self, duration_ms: int) -> None:
        """Record audio only after transport crosses the provider boundary."""

        self.metrics.add_provider_wire_audio(duration_ms)

    def matches(self, identity: VoiceAsyncIdentity) -> bool:
        matches = (
            identity == self.identity
            and identity.turn_id > self._completed_turn_id
        )
        if not matches:
            self.metrics.stale_callback_count += 1
        return matches

    def mark_pending_turn_speech(
        self,
        ingress_token: VoiceIngressToken | None = None,
    ) -> VoiceTurnToken | None:
        """Mark confirmed next-turn speech while the previous turn drains."""

        if self._state is not VoiceLifecycleState.DRAINING:
            raise RuntimeError("VOICE_PENDING_TURN_REQUIRES_DRAINING")
        if not self._pending_turn_speech:
            self._pending_turn_id = self._allocate_turn_id()
            if ingress_token is not None:
                self._pending_turn_token = VoiceTurnToken(
                    ingress=ingress_token,
                    turn_id=self._pending_turn_id,
                )
        elif (
            ingress_token is not None
            and self._pending_turn_token is not None
            and self._pending_turn_token.ingress != ingress_token
        ):
            raise RuntimeError("VOICE_PENDING_TURN_TOKEN_ALREADY_BOUND")
        elif ingress_token is not None and self._pending_turn_token is None:
            if self._pending_turn_id is None:
                raise RuntimeError("VOICE_PENDING_TURN_ID_MISSING")
            self._pending_turn_token = VoiceTurnToken(
                ingress=ingress_token,
                turn_id=self._pending_turn_id,
            )
        self._pending_turn_speech = True
        return self._pending_turn_token

    def begin_pending_turn(self, *, allow_empty: bool = False) -> bytes:
        """Activate and drain the pending turn after the prior final."""

        if self._state is not VoiceLifecycleState.WARM_IDLE:
            raise RuntimeError("VOICE_PENDING_TURN_REQUIRES_WARM_IDLE")
        if not self.has_pending_turn and not (
            allow_empty and self.has_pending_turn_identity
        ):
            return b""
        payload = self._pending_turn.drain()
        self._pending_turn_speech = False
        pending_turn_id, self._pending_turn_id = self._pending_turn_id, None
        pending_turn_token, self._pending_turn_token = (
            self._pending_turn_token,
            None,
        )
        if pending_turn_id is None:
            raise RuntimeError("VOICE_PENDING_TURN_ID_MISSING")
        self._turn_id = pending_turn_id
        self._current_turn_token = pending_turn_token
        self.transition(VoiceLifecycleEvent.SPEECH_CONFIRMED)
        self._pre_roll_sent_for_turn = True
        return payload

    def discard_pending_turn(self) -> None:
        """Discard the whole next-turn candidate while preserving a sealed turn."""

        self._pending_turn.clear()
        self._pending_turn_speech = False
        self._pending_turn_id = None
        self._pending_turn_token = None

    def discard_unconfirmed_pending_audio(self) -> None:
        """Discard post-seal audio that never became confirmed speech."""

        if self._pending_turn_speech:
            return
        self._pending_turn.clear()

    def preserve_unconfirmed_pending_audio(self) -> bool:
        """Move an unconfirmed successor into pre-roll after the old final."""

        if self._pending_turn_speech:
            return False
        payload = self._pending_turn.drain()
        if not payload:
            return False
        dropped = self._pre_roll.append(payload, sample_rate_hz=16_000)
        if dropped:
            self.metrics.buffer_overflow_count += 1
        return True

    def stop(self) -> None:
        if self._state is not VoiceLifecycleState.OFF:
            self._state = next_lifecycle_state(
                self._state,
                VoiceLifecycleEvent.STOPPED,
            )
        self._route_mode = VoiceRouteMode.BLOCKED
        self._route_generation += 1
        self._transport_generation += 1
        self._lifecycle_revision += 1
        self._reason_code = None
        self._incident_id = None
        self._turn_id = self._allocate_turn_id()
        self._current_turn_token = None
        self._pre_roll_sent_for_turn = False
        self._pre_roll.clear()
        self._pending_turn.clear()
        self._pending_turn_speech = False
        self._pending_turn_id = None
        self._pending_turn_token = None
        self._pending_connect.clear()
        self._active_start_audio = b""

    def enable_independent_asr_fail_open(self) -> None:
        """Disable throttling while preserving the hard independent route."""

        if self._route_mode is VoiceRouteMode.INDEPENDENT:
            self._independent_asr_fail_open = True

    def invalidate_transport(self) -> None:
        """Advance transport generation without stopping local listening."""

        self._transport_generation += 1
        self._lifecycle_revision += 1

    def invalidate_audio(self) -> None:
        """Invalidate buffered PCM and turn identity after input suppression."""

        self._turn_id = self._allocate_turn_id()
        self._current_turn_token = None
        self._completed_turn_id = self._turn_id
        self._pre_roll_sent_for_turn = False
        self._pre_roll.clear()
        self._pending_connect.clear()
        self._pending_turn.clear()
        self._pending_turn_speech = False
        self._pending_turn_id = None
        self._pending_turn_token = None
        self._active_start_audio = b""
        if self._state not in {
            VoiceLifecycleState.OFF,
            VoiceLifecycleState.BLOCKED,
            VoiceLifecycleState.SUSPENDED,
        }:
            self._state = VoiceLifecycleState.LOCAL_LISTEN
        self._lifecycle_revision += 1

    def _allocate_turn_id(self) -> int:
        self._turn_sequence += 1
        return self._turn_sequence

    def _target_disposition(self) -> AudioDisposition:
        if self._state in {
            VoiceLifecycleState.LOCAL_LISTEN,
            VoiceLifecycleState.PREWARMING,
            VoiceLifecycleState.WARM_IDLE,
            VoiceLifecycleState.DEEP_SLEEP,
            VoiceLifecycleState.BACKOFF,
        }:
            return AudioDisposition.BUFFER
        if self._state is VoiceLifecycleState.ACTIVE:
            return AudioDisposition.FORWARD
        return AudioDisposition.BLOCK
