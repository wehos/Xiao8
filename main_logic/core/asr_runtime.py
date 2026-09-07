"""Core-side microphone ingress and independent-ASR bridge.

This module owns Core session, MicLease, queue, hot-swap, and transcript
delivery concerns. Provider sessions and endpointing remain encapsulated by
``main_logic.asr_client.runtime.IndependentAsrRuntime``.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import struct
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, ClassVar, Literal
from uuid import uuid4

from websockets import exceptions as web_exceptions

from main_logic.asr_client import (
    VoiceIdentityActivationResult,
    get_asr_core_capabilities,
)
from main_logic.asr_client.runtime import (
    ASR_CONNECT_TOTAL_BUDGET_SECONDS,
    ASR_HANDOFF_BUFFER_RESERVE_US,
    AsrRuntimeCallbacks,
    AsrStartStatus,
    IndependentAsrRuntime,
    SpeakerShadowFactory,
)
from main_logic.asr_client.lifecycle import VoiceLifecycleState
from main_logic.asr_client.speaker_verifier_contracts import (
    SpeakerVerifierAuthority,
    SpeakerVerifierAuthorityState,
    SpeakerVerifierInstallOutcome,
    SpeakerVerifierInstallReceipt,
    SpeakerVerifierSpec,
)
from main_logic.voice_input import (
    BuiltinVoiceInputConsumer,
    VoiceInputConsumerCapabilities,
    VoiceInputDispatchResult,
    VoiceInputRegistry,
)
from main_logic.voice_input.consumers import (
    CoreChatTurnContext,
    CoreChatVoiceInputConsumer,
    GameVoiceInputConsumer,
)
from main_logic.voice_turn.contracts import (
    AsrFailureEvent,
    AsrLifecycleNotification,
    AsrStatusEvent,
    AsrSubmitStatus,
    VoicePartialEvent,
    VoiceIngressToken,
    VoiceTranscriptEvent,
    VoiceTurnToken,
)
from main_logic.omni_realtime_client._response_arbiter import (
    ResponseAdmissionRejected,
)
from main_logic.voice_turn.activity_evidence import RnnoiseEvidence
from main_logic.voice_turn.audio_input import (
    ProcessedVoiceFrame,
    VoiceInputAudioPipeline,
)
from utils.game_route_state import get_active_game_route_generation_identity
from main_logic import core as _core_facade

from ._shared import logger
from .multimodal_turn import (
    _MAX_PRERECORD_VISUAL_VALIDATIONS,
    _MAX_LIVE_TURN_RECORDS,
    _ONSET_TRUST_WINDOW_S,
    MultimodalTurn,
    _CoreMultimodalTurnRecord,
    _IndependentVisualFrame,
)


@dataclass(frozen=True, slots=True)
class _QueuedMicFrame:
    # Longest microphone PCM frame accepted at ingress. Bounded by DURATION,
    # not by a fixed sample count: the binary wire decoder derives its limit
    # per sample rate (_VOICE_BINARY_MAX_DURATION_MS in
    # main_routers/websocket_router.py), so a fixed count would reject at
    # 120 ms for 48 kHz but only at 360 ms for 16 kHz and the two ingress paths
    # would disagree (CodeRabbit). ClassVar, not a module constant: the
    # layering gate keeps this mixin module to imports and classes only.
    MAX_DURATION_MS: ClassVar[int] = 120

    message: dict
    duration_us: int
    source_rate_hz: int
    token: VoiceIngressToken
    received_at: float
    captured_at: float
    audio_stream_epoch: int = 0
    ingress_sequence: int = 0

    @classmethod
    def from_message(
        cls,
        message: dict,
        *,
        token: VoiceIngressToken,
        received_at: float | None = None,
        captured_at: float | None = None,
        audio_stream_epoch: int = 0,
        ingress_sequence: int = 0,
    ) -> "_QueuedMicFrame":
        samples = message.get("data")
        if not isinstance(samples, list):
            raise ValueError("MIC_PCM_SAMPLES_REQUIRED")
        declared_rate_hz = message.get("sample_rate_hz")
        if declared_rate_hz is None:
            source_rate_hz = 48_000 if len(samples) == 480 else 16_000
        elif declared_rate_hz in {16_000, 48_000}:
            source_rate_hz = int(declared_rate_hz)
        else:
            raise ValueError("MIC_SAMPLE_RATE_UNSUPPORTED")
        # Per-frame bound, matching the binary wire cap in websocket_router
        # (_VOICE_BINARY_MAX_DURATION_MS). The queue's own 2 s / 256-frame
        # limits are post-decode, so without this a single JSON stream_data
        # frame could carry an arbitrarily long sample list -- the frontend
        # never sends JSON audio, so any oversized one is malformed by
        # construction.
        if len(samples) * 1_000 > source_rate_hz * cls.MAX_DURATION_MS:
            raise ValueError("MIC_PCM_FRAME_TOO_LONG")
        duration_us = (len(samples) * 1_000_000 + source_rate_hz - 1) // source_rate_hz
        return cls(
            message=message,
            duration_us=duration_us,
            source_rate_hz=source_rate_hz,
            token=token,
            received_at=time.monotonic() if received_at is None else received_at,
            captured_at=time.time() if captured_at is None else captured_at,
            audio_stream_epoch=audio_stream_epoch,
            ingress_sequence=ingress_sequence,
        )


class _AudioDurationQueue:
    """Bound Core microphone ingress by duration and frame count."""

    def __init__(self, *, capacity_us: int, max_frames: int) -> None:
        if capacity_us <= 0 or max_frames <= 0:
            raise ValueError("audio queue limits must be positive")
        self.capacity_us = capacity_us
        self.maxsize = max_frames
        self._normal_capacity_us = capacity_us
        self._normal_max_frames = max_frames
        self._handoff_capacity_us = capacity_us + ASR_HANDOFF_BUFFER_RESERVE_US
        self._handoff_max_frames = (
            max_frames * self._handoff_capacity_us + capacity_us - 1
        ) // capacity_us
        self._handoff_owner: tuple[VoiceIngressToken, int] | None = None
        self._duration_us = 0
        self._queue: asyncio.Queue[_QueuedMicFrame] = asyncio.Queue(
            maxsize=self._handoff_max_frames,
        )

    @property
    def duration_us(self) -> int:
        return self._duration_us

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def put_nowait(self, frame: _QueuedMicFrame, *, handoff_reserve: bool = False) -> None:
        owner = (frame.token, frame.audio_stream_epoch)
        if handoff_reserve and self._handoff_owner in (None, owner):
            self._handoff_owner = owner
            self.capacity_us = self._handoff_capacity_us
            self.maxsize = self._handoff_max_frames
        # A different ingress cannot borrow an old owner's drain allowance.
        same_owner = self._handoff_owner == owner
        capacity_us = self.capacity_us if same_owner else self._normal_capacity_us
        max_frames = self.maxsize if same_owner else self._normal_max_frames
        if (
            self._queue.qsize() >= max_frames
            or self._duration_us + frame.duration_us > capacity_us
        ):
            raise asyncio.QueueFull
        self._queue.put_nowait(frame)
        self._duration_us += frame.duration_us

    async def get(self) -> _QueuedMicFrame:
        frame = await self._queue.get()
        self._duration_us -= frame.duration_us
        self._trim_handoff_reserve()
        return frame

    def get_nowait(self) -> _QueuedMicFrame:
        frame = self._queue.get_nowait()
        self._duration_us -= frame.duration_us
        self._trim_handoff_reserve()
        return frame

    def _trim_handoff_reserve(self) -> None:
        # Retain headroom while the backlog drains after the handoff Future
        # resolves. Shrinking immediately would reject the next live frame
        # solely because this already-accepted backlog still exceeds 2 s.
        if (
            self._duration_us <= self._normal_capacity_us
            and self._queue.qsize() <= self._normal_max_frames
        ):
            self._handoff_owner = None
            self.capacity_us = self._normal_capacity_us
            self.maxsize = self._normal_max_frames

    def task_done(self) -> None:
        self._queue.task_done()


@dataclass(frozen=True, slots=True)
class _VoiceInputPipelineFailure:
    """One committed audio-preprocessing failure, as its notify phase sees it.

    Everything the notify/revoke phase fences on, captured at commit time so
    that phase can run without the pipeline transition lock. ``failure_token``
    is the identity: it is replaced only by a NEWER commit, unlike
    ``_voice_input_pipeline_failed``, which any pipeline replacement clears.
    """

    failure_token: object
    independent_route: bool
    provider: str
    session_epoch: int
    connection_id: str
    lease_generation: int
    transition_generation: int
    route_operation_generation: int
    session_ref: object
    audio_epoch: int


@dataclass(frozen=True, slots=True)
class _HotSwapAudioFrame:
    pcm16: bytes
    token: VoiceIngressToken
    captured_at: float = 0.0
    speech_probability: float | None = None
    rnnoise_available: bool = False
    rnnoise_evidence: RnnoiseEvidence | None = None
    audio_stream_epoch: int = 0
    ingress_sequence: int = 0


class _HotSwapAudioBuffer:
    """Bound hot-swap PCM without silently dropping the middle of a turn."""

    def __init__(self, *, capacity_ms: int = 8_000) -> None:
        if capacity_ms <= 0:
            raise ValueError("capacity_ms must be positive")
        self._capacity_bytes = 16_000 * 2 * capacity_ms // 1_000
        self._size_bytes = 0
        self._frames: list[_HotSwapAudioFrame] = []

    @property
    def duration_ms(self) -> int:
        return self._size_bytes * 1_000 // (16_000 * 2)

    def append(self, frame: _HotSwapAudioFrame) -> bool:
        if self._size_bytes + len(frame.pcm16) > self._capacity_bytes:
            self.clear()
            return False
        self._frames.append(frame)
        self._size_bytes += len(frame.pcm16)
        return True

    def drain(self) -> tuple[_HotSwapAudioFrame, ...]:
        frames = tuple(self._frames)
        self.clear()
        return frames

    def clear(self) -> None:
        self._frames.clear()
        self._size_bytes = 0

    def __bool__(self) -> bool:
        return bool(self._frames)

    def __len__(self) -> int:
        return len(self._frames)


class AsrRuntimeMixin:
    """Core manager facade for microphone input and independent ASR."""

    def _init_asr_runtime_state(self) -> None:
        self._voice_lease_generation = -1
        self._voice_lease_connection_id = ""
        # Socket holding the voice lease; see _set_voice_input_websocket.
        self._voice_input_websocket = None
        self._voice_lease_resync_suppressed = False
        self._voice_lease_synchronized = False
        self._voice_lease_control_seen = False
        self._voice_input_transition_generation = 0
        self._voice_lease_owner = "none"
        self._voice_lease_hard_muted = False
        self._voice_lease_focus_suppressed = False
        self._voice_lease_requires_abort = False
        self._voice_input_suppressed = True
        self._voice_input_suppression_reasons: set[str] = {"owner_none"}
        self._voice_input_external_suppressions: set[str] = set()
        self._voice_lease_resync_signal_state: (
            tuple[str, int, bool, str, int] | None
        ) = None
        self._voice_lease_resync_delivery_state: (
            tuple[object, set[str]] | None
        ) = None
        self._audio_stream_queue = _AudioDurationQueue(
            capacity_us=2_000_000,
            max_frames=256,
        )
        self._audio_stream_worker_task: asyncio.Task | None = None
        self._audio_stream_dropped_total = 0
        self._audio_stream_epoch = 0
        self._last_audio_stream_backlog_log_time = 0.0
        self._last_hot_swap_rebind_drop_log_time = 0.0
        self.hot_swap_audio_cache = _HotSwapAudioBuffer(capacity_ms=8_000)
        self.hot_swap_cache_lock = asyncio.Lock()
        self.is_flushing_hot_swap_cache = False
        self._hot_swap_ingress_sequence = 0
        self._hot_swap_pending_sequences: set[int] = set()
        self._hot_swap_sequence_progress = asyncio.Event()
        self._hot_swap_sequence_progress.set()
        self._omni_mic_audio_bytes = 0
        self._asr_route_mode = "blocked"
        self._visual_route_mode: Literal["native", "independent"] = "native"
        self._microphone_route_generation = 0
        self._asr_route_operation_generation = 0
        self._asr_notification_lock = asyncio.Lock()
        self._core_asr_notification_revision = 0
        # Shared with the hot-swap lifecycle: a prepared final either finishes
        # against the still-open old session, or waits until close+promotion
        # has atomically exposed the replacement.
        self._core_voice_session_swap_lock = asyncio.Lock()
        self._core_voice_session_swap_barrier_timeout_s = 5.0
        # 取消旧 listener 的等待上限。见 lifecycle.py：这里必须是"只等到期、不等
        # 取消完成"的语义，否则一个吞掉 CancelledError 的 listener 会连带卡死
        # swap lock。
        self._core_voice_listener_cancel_timeout_s = 2.0
        self._independent_visual_generation = 0
        self._independent_visual_frame_ttl_s = 5.0
        self._latest_independent_visual_frame: _IndependentVisualFrame | None = None
        self._core_multimodal_turns: dict[str, _CoreMultimodalTurnRecord] = {}
        self._prerecord_visual_validations: dict[asyncio.Task, float] = {}
        # record 建立之前**已经校验完**的帧。单槽 _latest_independent_visual_frame
        # 只留最新一张，所以光靠它救不回这段窗口里的开头/中间帧；任务栈也不行，
        # 任务一完成就从栈里摘掉了。
        self._prerecord_visual_frames: list[_IndependentVisualFrame] = []
        self._voice_input_pipeline_transition_lock = asyncio.Lock()
        self._independent_asr_provider: str | None = None
        self._independent_asr_route_key: str | None = None
        self._independent_asr_handshake_override: bool | None = None
        self._speaker_shadow_factory: SpeakerShadowFactory | None = None
        self._voice_input_resource_optimization_handshake_override: bool | None = None
        self._voice_input_resource_optimization_session_value: bool | None = None
        self._voice_input_noise_reduction_enabled = True
        self._voice_input_audio_pipeline = VoiceInputAudioPipeline(
            nr_enabled=self._voice_input_noise_reduction_enabled,
        )
        self._core_asr_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._voice_input_pipeline_failed = False
        # Identity of the committed pipeline failure that owns the blocked
        # route, if any. See _VoiceInputPipelineFailure.
        self._voice_input_pipeline_failure_token: object | None = None
        self._blocked_text_mode_microphone_signal_state: tuple[
            int,
            str,
            int,
            int,
            object,
        ] | None = None
        self._blocked_text_mode_microphone_delivery_state: (
            tuple[object, set[str]] | None
        ) = None
        # Identity of the independent-ASR turn that owns the frontend's
        # singleton preview bubble, plus its last rendered text. Both are
        # stamped/refreshed from the ordered partial stream so a late final
        # can tell "my own bubble" from "the next turn already took it over".
        self._core_asr_preview_turn_id = ""
        self._core_asr_preview_text = ""
        self._core_asr_preview_turn_token: VoiceTurnToken | None = None
        self._init_voice_input_registry()
        callbacks = AsrRuntimeCallbacks(
            display_name=lambda: str(getattr(self, "lanlan_name", "core")),
            on_prepare_turn=self._prepare_voice_input_turn,
            on_partial=self._dispatch_voice_input_partial,
            on_final=self._dispatch_voice_input_final,
            on_turn_abandoned=self._handle_core_asr_turn_abandoned,
            on_failure=self._handle_core_asr_failure,
            on_status=self._send_core_asr_status,
            on_lifecycle=self._send_core_asr_lifecycle,
        )
        self._asr_runtime = IndependentAsrRuntime(callbacks)

    def _init_voice_input_registry(self) -> None:
        """Install the manager-lifetime built-ins exactly once."""

        if hasattr(self, "_voice_input_registry"):
            return
        registry = VoiceInputRegistry()
        core_chat = CoreChatVoiceInputConsumer(
            session_ref=lambda: getattr(self, "session", None),
            game_route_identity=lambda: get_active_game_route_generation_identity(
                str(getattr(self, "lanlan_name", "core"))
            ),
            on_prepare=lambda token, context: self._prepare_core_voice_turn(
                token,
                session_ref=context.session_ref,
                abandon_on_failure=False,
            ),
            on_partial_event=self._send_core_asr_preview,
            on_final_event=lambda event, context: self._dispatch_core_asr_transcript(
                event,
                session_ref=context.session_ref,
                source_game_route_identity=context.source_game_route_identity,
            ),
            on_cancelled_event=self._cancel_core_chat_voice_turn,
        )
        game = GameVoiceInputConsumer(
            lanlan_name=lambda: str(getattr(self, "lanlan_name", "core")),
        )
        core_registration = registry.register_builtin(
            BuiltinVoiceInputConsumer.CORE_CHAT,
            core_chat,
            capabilities=VoiceInputConsumerCapabilities(
                accepts_partial=True,
                accepts_final=True,
            ),
        )
        game_registration = registry.register_builtin(
            BuiltinVoiceInputConsumer.GAME,
            game,
            capabilities=VoiceInputConsumerCapabilities(
                accepts_partial=False,
                accepts_final=True,
            ),
        )
        registry.activate(core_registration.handle)
        self._voice_input_registry = registry
        self._core_chat_voice_input_registration = core_registration
        self._game_voice_input_registration = game_registration

    def _ensure_asr_runtime_state(self) -> None:
        if not hasattr(self, "_asr_runtime"):
            self._init_asr_runtime_state()
        self._init_voice_input_registry()
        if not hasattr(self, "_asr_route_operation_generation"):
            self._asr_route_operation_generation = 0
        if not hasattr(self, "_visual_route_mode"):
            self._visual_route_mode = (
                "independent"
                if getattr(self, "_asr_route_mode", "blocked") == "independent"
                else "native"
            )
        if not hasattr(self, "_asr_notification_lock"):
            self._asr_notification_lock = asyncio.Lock()
        if not hasattr(self, "_core_asr_notification_revision"):
            self._core_asr_notification_revision = 0
        if not hasattr(self, "_core_voice_session_swap_lock"):
            self._core_voice_session_swap_lock = asyncio.Lock()
        if not hasattr(self, "_core_voice_session_swap_barrier_timeout_s"):
            self._core_voice_session_swap_barrier_timeout_s = 5.0
        if not hasattr(self, "_core_voice_listener_cancel_timeout_s"):
            self._core_voice_listener_cancel_timeout_s = 2.0
        if not hasattr(self, "_independent_visual_generation"):
            self._independent_visual_generation = 0
        if not hasattr(self, "_independent_visual_frame_ttl_s"):
            self._independent_visual_frame_ttl_s = 5.0
        if not hasattr(self, "_latest_independent_visual_frame"):
            self._latest_independent_visual_frame = None
        if not hasattr(self, "_core_multimodal_turns"):
            self._core_multimodal_turns = {}
        if not hasattr(self, "_prerecord_visual_validations"):
            self._prerecord_visual_validations = {}
        if not hasattr(self, "_prerecord_visual_frames"):
            self._prerecord_visual_frames = []
        if not hasattr(self, "_voice_input_pipeline_transition_lock"):
            self._voice_input_pipeline_transition_lock = asyncio.Lock()
        if not hasattr(self, "_voice_input_pipeline_failure_token"):
            self._voice_input_pipeline_failure_token = None
        if not hasattr(self, "_voice_input_transition_generation"):
            self._voice_input_transition_generation = 0
        if not hasattr(self, "_voice_lease_resync_signal_state"):
            self._voice_lease_resync_signal_state = None
        if not hasattr(self, "_voice_lease_resync_delivery_state"):
            self._voice_lease_resync_delivery_state = None
        if not hasattr(self, "_voice_input_noise_reduction_enabled"):
            self._voice_input_noise_reduction_enabled = True
        if not hasattr(self, "_core_asr_cleanup_tasks"):
            self._core_asr_cleanup_tasks = set()
        if not hasattr(self, "_last_hot_swap_rebind_drop_log_time"):
            self._last_hot_swap_rebind_drop_log_time = 0.0
        if not hasattr(self, "_independent_asr_handshake_override"):
            self._independent_asr_handshake_override = None
        if not hasattr(self, "_speaker_shadow_factory"):
            self._speaker_shadow_factory = None
        if not hasattr(self, "_voice_input_external_suppressions"):
            self._voice_input_external_suppressions = set()
        if not hasattr(
            self,
            "_voice_input_resource_optimization_handshake_override",
        ):
            self._voice_input_resource_optimization_handshake_override = None
        if not hasattr(
            self,
            "_voice_input_resource_optimization_session_value",
        ):
            self._voice_input_resource_optimization_session_value = None
        if not hasattr(self, "_core_asr_preview_turn_id"):
            self._core_asr_preview_turn_id = ""
        if not hasattr(self, "_core_asr_preview_text"):
            self._core_asr_preview_text = ""
        if not hasattr(self, "_core_asr_preview_turn_token"):
            self._core_asr_preview_turn_token = None
        if not hasattr(self, "_blocked_text_mode_microphone_signal_state"):
            self._blocked_text_mode_microphone_signal_state = None
        if not hasattr(self, "_blocked_text_mode_microphone_delivery_state"):
            self._blocked_text_mode_microphone_delivery_state = None
        if not hasattr(self, "_voice_input_websocket"):
            self._voice_input_websocket = None
        if not hasattr(self, "_voice_lease_resync_suppressed"):
            self._voice_lease_resync_suppressed = False

    def _stage_independent_visual_frame(
        self,
        image_b64: str,
        *,
        source: str,
        request_id: str | None,
        captured_at: float,
    ) -> bool:
        """Stage one validated live frame without sending it to any model."""

        self._ensure_asr_runtime_state()
        if (
            self._asr_route_mode != "independent"
            or not image_b64
            or not isinstance(captured_at, (int, float))
        ):
            return False

        ingress = self._capture_ingress_token()
        previous = self._latest_independent_visual_frame
        stable_captured_at = float(captured_at)
        # screen/camera 各自跑独立的 fire-and-forget 校验任务，先捕获的那帧完全
        # 可能后 staging。这两件事必须分开判：
        #   - 「最新帧缓存」必须真的是最新的，乱序到达的旧帧不能顶掉它；
        #   - 但那张旧帧仍然是本回合发声区间里的合法成员，抽样要收下它，
        #     否则开头那张（正是用户开口时指的东西）会被后到的邻居顶掉。
        supersedes_latest_cache = not (
            previous is not None
            and previous.session_epoch == ingress.session_epoch
            and previous.route_generation == self._voice_input_transition_generation
            and stable_captured_at <= previous.captured_at
        )

        self._independent_visual_generation += 1
        frame = _IndependentVisualFrame(
            image_b64=image_b64,
            session_epoch=ingress.session_epoch,
            route_generation=self._voice_input_transition_generation,
            generation=self._independent_visual_generation,
            captured_at=stable_captured_at,
            source=str(source or "unknown"),
            request_id=(str(request_id) if request_id is not None else None),
        )
        if supersedes_latest_cache:
            self._latest_independent_visual_frame = frame
        # 语义端点之后（lifecycle 进 DRAINING）到 provider final 派发之间，周期性
        # 的截图任务还会继续落地。那些画面已经不是这段发声了。判据必须是回合自己
        # 的端点截止时间而不是"当下的 lifecycle 状态"——校验任务并发跑，说话期间
        # 拍到、端点之后才校验完的帧按状态判会被误杀。
        self._mark_independent_asr_endpoint_if_sealed()
        observed = False
        for record in tuple(self._core_multimodal_turns.values()):
            # 跳过已作废的记录：它们留着只是为了别把正在派发的那条 final 的**话**
            # 弄丢，视觉所有权早已交给后继回合。喂给它们等于让新帧被一条不会再用图
            # 的记录"吃掉"，当前这一轮反而收不到（而且 prerecord 暂存也不会武装）。
            if record.invalidated.is_set():
                continue
            if record.accepts(frame):
                record.observe(frame)
                observed = True
        # 判据是"当前这一轮还没建起来"，不是"一条记录都没有"：为了保住正在派发的
        # 上一条 final，旧记录会被保留下来，dict 因此不再会空。
        if not observed and self._active_multimodal_turn_record() is None:
            # 语音确认到 _begin_core_multimodal_turn 之间还没有 record。这一帧已经
            # 校验完了，任务栈救不了它（任务一完成就被摘掉），单槽缓存也只留最新
            # 一张 —— 不留下来的话这段窗口里的开头/中间帧就永久丢了。
            self._retain_prerecord_visual_frame(frame)
        return supersedes_latest_cache or observed

    def _current_turn_onset_for_prerecord(self) -> float | None:
        """This turn's speech onset, when it is recent enough to be trusted.

        Same trust window the record uses, so the buffer and the record agree
        on where the turn began. Returns None when no usable onset exists --
        then every retained frame stays a candidate, which is the old behaviour.
        """
        runtime = getattr(self, "_asr_runtime", None)
        # 重叠期间优先读 _asr_pending_turn_onset_at：后继发声在前一轮还 DRAINING
        # 时开口，它的边界先记在 pending 槽里，要等前一轮的 provider final 把那一
        # 轮激活了才拷进 _asr_turn_onset_at。只读后者的话，整个重叠窗口里拿到的都
        # 是**前一轮**的 onset —— 封口之后、后继开口之前的帧照样算「本轮的」，占满
        # 有界缓冲，把后继真正的开头/中间帧挤掉。
        onset = getattr(runtime, "_asr_pending_turn_onset_at", None)
        if not isinstance(onset, (int, float)):
            onset = getattr(runtime, "_asr_turn_onset_at", None)
        if not isinstance(onset, (int, float)):
            return None
        age = time.monotonic() - float(onset)
        if not 0.0 <= age <= _ONSET_TRUST_WINDOW_S:
            return None
        return float(onset)

    def _retain_prerecord_visual_frame(
        self,
        frame: _IndependentVisualFrame,
    ) -> None:
        """Keep one already-validated frame until the onset-owned record exists."""

        frames = self._prerecord_visual_frames
        # 先按**本轮 onset** 裁掉开口之前的帧，再做有界抽稀。
        #
        # 这个缓冲会在屏幕/摄像头共享着、用户还没开口时就被闲置帧填满（8 张）。
        # 抽稀刻意保留跨度大的两端，于是那些闲置帧很占位；等用户真开口、确认到
        # 注册之间拍的几张挤进来时，它们和整段闲置历史一起被抽稀，随后建记录时
        # 的 onset 过滤又把开口之前的全丢掉 —— 最后只剩最新那一张，这一轮的开头
        # 和中间视图就没了。开口之前的帧本来就不属于这一轮，不该占它的名额。
        onset = self._current_turn_onset_for_prerecord()
        if onset is not None:
            stale = [item for item in frames if item.captured_at < onset]
            if stale:
                frames[:] = [item for item in frames if item.captured_at >= onset]
        # 按拍摄时间维护，不按落地顺序 —— 并发校验下两者不是一回事（这条判据在
        # middle_candidates 上已经栽过一次，这里是同一个坑）。否则下面按"相邻跨度"
        # 抽稀时，列表两端根本不是时间上的首尾，还可能删掉真正最早/最晚的那帧。
        bisect.insort(
            frames,
            frame,
            key=lambda item: (item.captured_at, item.generation),
        )
        # 有界。超限时**不能丢队头** —— 队头正是这段发声的开头（用户开口时指的东
        # 西），丢掉它就等于把 record 建立前那段窗口的起点抹掉。改成丢掉"最冗余"
        # 的内点（左右邻居跨度最小），两端保留；与 middle_candidates 的抽稀同一
        # 判据，留下的仍然大致铺满整段。
        while len(frames) > _MAX_PRERECORD_VISUAL_VALIDATIONS:
            victim = min(
                range(1, len(frames) - 1),
                key=lambda index: (
                    frames[index + 1].captured_at - frames[index - 1].captured_at,
                    index,
                ),
            )
            del frames[victim]

    def _mark_independent_asr_endpoint_if_sealed(self) -> None:
        """Stamp the endpoint cutoff on the active turn once ASR seals it.

        Prefers the runtime's recorded seal instant (``_asr_turn_endpointed_at``)
        over the moment Core happens to look. Sampling the lifecycle state can
        only ever place the cutoff at an observation point, which drifts later
        than the real endpoint and lets a frame captured in that gap pass as if
        it belonged to the utterance. The recorded instant has no such drift.

        Falls back to the observation time for a runtime that exposes the
        lifecycle but not the timestamp, and leaves the cutoff unset when
        neither is available (previous admit-everything behaviour).
        """

        runtime = getattr(self, "_asr_runtime", None)
        endpointed_at = getattr(runtime, "_asr_turn_endpointed_at", None)
        # live 字段在 PROVIDER_FINAL 时被清，所以它只可能描述**在飞的那一轮**，
        # 用 started_at 当下界就够。保留副本则是跨轮存活的，必须更严 —— 见下。
        live_endpoint = isinstance(endpointed_at, (int, float))
        # 三类来源要分开，不能只分 live / 非 live：
        #   live     —— 在飞那一轮的字段，PROVIDER_FINAL 会清；
        #   retained —— 跨轮存活的保留副本，可能是**上一轮**的；
        #   observed —— 两者都没有时按 DRAINING 当场取的 monotonic，它必然属于
        #               本轮（就是此刻），不该套跨轮那条更严的判据。
        retained_endpoint = False
        if not live_endpoint:
            # PROVIDER_FINAL 会把上面那个清掉，而 Core 要到 transcript 派发之后
            # 才冻结这一轮 —— 冻结时读到的必然是 None。所以再读一个不随 final
            # 清除的副本；上一轮的残值由下面 started_at 的比较排掉。
            endpointed_at = getattr(
                runtime,
                "_asr_last_turn_endpointed_at",
                None,
            )
            retained_endpoint = isinstance(endpointed_at, (int, float))
            retained_key = getattr(
                runtime,
                "_asr_last_turn_endpointed_key",
                None,
            )
        if not isinstance(endpointed_at, (int, float)):
            lifecycle = getattr(runtime, "_asr_lifecycle", None)
            state = getattr(getattr(lifecycle, "snapshot", None), "state", None)
            if state is not VoiceLifecycleState.DRAINING:
                return
            endpointed_at = time.monotonic()
        sealed_at = float(endpointed_at)
        for record in self._core_multimodal_turns.values():
            # 已作废的记录留着只是为了保住正在派发的那条 final 的话，它不会再交出
            # 图；把后继回合的封口盖到它头上没有意义，也让状态更难读。
            if record.invalidated.is_set() or record.endpoint_at is not None:
                continue
            # ⚠️ 判据不能用 started_at：overlap 的后继发声其起点**早于**上一轮封口
            # （onset 是在上一轮还 ACTIVE 时记下的），拿 started_at 比会把上一轮的
            # 封口绑成本轮的截止点，本轮之后拍的每一帧都被拒 —— 整轮退化成纯文本。
            #
            # 保留副本跨轮存活，所以要求它晚于 record **注册**的时刻：上一轮的封口
            # 必然发生在本轮 record 建立之前。live 字段只描述在飞那一轮，仍用
            # started_at，免得极短发声里 seal 抢在注册之前时反而绑不上。
            if retained_endpoint and isinstance(retained_key, str):
                # 保留副本跨轮存活，所以先问它**属于哪一轮**。
                #
                # 为什么不靠时间戳：monotonic 在 Windows 上是 ~15ms 粒度（本文件
                # _begin_core_multimodal_turn 里已经为同一个原因退回过生成号判
                # 据）。"上一轮的封口"和"本轮自己的封口"都可能与本轮 record 的注
                # 册时刻相等，往任一个方向猜都会错：
                #   猜"相等归上一轮"（严格 >）—— 本轮若在注册的同一 tick 里封口，
                #     它自己的截止点被丢掉，endpoint_at 一直是 None，停口之后拍的
                #     帧会被折进这条 transcript；
                #   猜"相等归本轮"（>=）—— 上一轮的封口被盖到后继回合头上，本轮
                #     之后拍的每一帧都过不了 accepts()，稍长的发声等开头那张过期
                #     就整轮退化成纯文本。
                # 带上身份就不用猜：runtime 封口时把回合键一并记下（同构于
                # record.turn_id），这里直接判是不是同一轮。
                floor_ok = retained_key == record.turn_id
            elif retained_endpoint:
                # 旧 runtime / 测试替身没有那个身份字段时的兼容回落：退回时间戳，
                # 并把相等判给上一轮（丢掉本轮自己的截止点只是少一道过滤，把上一
                # 轮的封口盖到后继头上则会让整轮退化成纯文本，后者更贵）。
                floor_ok = sealed_at > record.registered_at
            elif live_endpoint:
                # live 字段只描述在飞的那一轮，用 started_at 当下界；取 >= 是为了
                # 极短发声里 seal 抢在注册之前时仍绑得上。
                floor_ok = sealed_at >= record.started_at
            else:
                # 当场观测：这个值就是此刻，必然属于本轮，沿用原判据即可。
                floor_ok = sealed_at >= record.registered_at
            if floor_ok:
                record.endpoint_at = sealed_at

    def _track_independent_visual_validation_task(
        self,
        task: asyncio.Task,
        *,
        captured_at: object,
    ) -> bool:
        """Bind one router-owned validation task to the active ASR turn."""

        self._ensure_asr_runtime_state()
        if (
            self._asr_route_mode != "independent"
            or not isinstance(task, asyncio.Task)
            or not isinstance(captured_at, (int, float))
        ):
            return False
        stable_captured_at = float(captured_at)
        record = self._active_multimodal_turn_record()
        if record is None:
            # 语音确认到 _begin_core_multimodal_turn 之间还没有 record。直接丢掉
            # 这个任务的话，短发声在 final 时看不到它、冻结成纯文本回合，而那一帧
            # 校验完时 record 已经作废了。先暂存，等 record 建出来再挂上去。
            self._stash_prerecord_visual_validation_task(task, stable_captured_at)
            return False
        if record.invalidated.is_set():
            return False
        ingress = self._capture_ingress_token()
        if (
            record.session_epoch != ingress.session_epoch
            or record.route_generation != self._voice_input_transition_generation
            or stable_captured_at < record.started_at
        ):
            return False
        self._bind_visual_validation_task(record, task, stable_captured_at)
        return True

    @staticmethod
    def _bind_visual_validation_task(
        record: _CoreMultimodalTurnRecord,
        task: asyncio.Task,
        captured_at: float,
    ) -> None:
        record.pending_visual_validations[task] = captured_at

        def discard(completed: asyncio.Task) -> None:
            record.pending_visual_validations.pop(completed, None)

        task.add_done_callback(discard)

    def _active_multimodal_turn_record(self):
        """Return the newest record that has not been superseded.

        Once older records are retained (so an in-flight final keeps its
        transcript), "whichever record comes first" no longer means "the
        current turn" — the retained ones are still there, just invalidated.
        """

        active = [
            record
            for record in self._core_multimodal_turns.values()
            if not record.invalidated.is_set()
            # 已封口（endpoint_at 已打）的同样不算 active：它的 accepts() 会按
            # 那个时刻挡掉之后拍的帧，而作废要等 provider final 回来、后继才能
            # prepare。这段窗口里如果把它算作 active，后继发声的帧既进不了它、
            # 又因为「有 active record」而不进 prerecord 缓冲，直接丢掉 ——
            # 后继回合开头和中间的帧就永久没了，只剩单槽缓存那一张。
            and record.endpoint_at is None
        ]
        if not active:
            return None
        return max(active, key=lambda record: record.registered_at)

    def _stash_prerecord_visual_validation_task(
        self,
        task: asyncio.Task,
        captured_at: float,
    ) -> None:
        """Hold one validation task until the onset-owned record exists."""

        pending = self._prerecord_visual_validations
        pending[task] = captured_at
        # 有界：只可能积压「语音确认到 record 建立」这一小段窗口里的任务；仍然设
        # 一个上限，免得 record 一直没建出来时无限增长。
        #
        # ⚠️ 淘汰方向与 _prerecord_visual_frames 一致：**不能丢最早的那个**。
        # router 按拍摄顺序注册任务，最早那个就是这段发声的开头；丢了它之后，短发声
        # 在 final 时等不到它，record 在那一帧落地前就作废了，抽样里正好缺掉"用户开
        # 口时看到的东西"。改成丢「最冗余」的内点（左右邻居跨度最小），两端保留。
        while len(pending) > _MAX_PRERECORD_VISUAL_VALIDATIONS:
            ordered = sorted(pending.items(), key=lambda item: item[1])
            victim = min(
                range(1, len(ordered) - 1),
                key=lambda index: (
                    ordered[index + 1][1] - ordered[index - 1][1],
                    index,
                ),
            )
            pending.pop(ordered[victim][0], None)

        def discard(completed: asyncio.Task) -> None:
            self._prerecord_visual_validations.pop(completed, None)

        task.add_done_callback(discard)

    def _attach_prerecord_visual_validation_tasks(
        self,
        record: _CoreMultimodalTurnRecord,
    ) -> None:
        """Move eligible pre-record validation tasks onto the new record."""

        stashed = self._prerecord_visual_validations
        self._prerecord_visual_validations = {}
        for task, captured_at in stashed.items():
            if task.done() or captured_at < record.started_at:
                continue
            self._bind_visual_validation_task(record, task, captured_at)

    async def _await_independent_visual_validation_tasks(
        self,
        turn_id: str,
    ) -> None:
        """Bound final-freeze on validations captured during this ASR turn."""

        self._ensure_asr_runtime_state()
        record = self._core_multimodal_turns.get(turn_id)
        if record is None or record.invalidated.is_set():
            return
        freeze_at = time.monotonic()
        if (
            record.route_generation != self._voice_input_transition_generation
            or record.session_epoch != self._capture_ingress_token().session_epoch
        ):
            return
        eligible = [
            task
            for task, captured_at in record.pending_visual_validations.items()
            if (
                not task.done()
                and record.started_at <= captured_at <= freeze_at
                and freeze_at - captured_at
                <= self._independent_visual_frame_ttl_s
            )
        ]
        if not eligible:
            return
        remaining_ttl = max(
            self._independent_visual_frame_ttl_s
            - (freeze_at - record.pending_visual_validations[task])
            for task in eligible
        )
        # Image validation is local/threaded work. Give an already-captured
        # frame a short chance to join its transcript without holding the
        # serial ASR final dispatcher behind a slow or wedged worker.
        timeout = min(0.5, remaining_ttl)
        if timeout <= 0:
            return

        all_validations = asyncio.gather(
            *(asyncio.shield(task) for task in eligible),
            return_exceptions=True,
        )
        invalidated = asyncio.create_task(record.invalidated.wait())
        try:
            await asyncio.wait(
                {all_validations, invalidated},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not all_validations.done():
                all_validations.cancel()
            if not invalidated.done():
                invalidated.cancel()
            await asyncio.gather(
                all_validations,
                invalidated,
                return_exceptions=True,
            )

    def _begin_core_multimodal_turn(
        self,
        turn_id: str,
        token: VoiceTurnToken,
    ) -> None:
        """Register raw-image ownership at the independent-ASR turn boundary."""

        self._ensure_asr_runtime_state()
        # The independent-ASR registry admits one Core chat utterance at a
        # time. A newer prepare invalidates any older frame/text ownership
        # synchronously, before candidate creation or provider awaits.
        for previous in self._core_multimodal_turns.values():
            previous.invalidated.set()
        # ⚠️ 不能 clear()：上一条**已被接受**的 final 可能还在 dispatcher 里跑（比如
        # 正卡在有界的视觉校验 join 上）。记录一没，它回来做身份自检时会认为世界已经
        # 变了而直接 return —— 那句话既不落库也不提交，重叠发声等于抹掉用户完整的上
        # 一轮。invalidated 已经置上，足以让它放弃**图**；话必须留住。
        # 每条记录都在自己 dispatch 的 finally 里被 _abandon_core_voice_turn 按
        # turn_id 移除。到不到上限，淘汰的判据都是**这条 final 派发完了没有**，不是
        # 注册得早不早：挤掉一条还挂在 handle_input_transcript 上的 final，等于把用
        # 户整句话丢掉。上限只对还没开始派发的记录（准备了却没等到 final 的回合）生
        # 效，是内存兜底。
        while len(self._core_multimodal_turns) >= _MAX_LIVE_TURN_RECORDS:
            idle = [
                key
                for key, record in self._core_multimodal_turns.items()
                if not record.dispatch_started
            ]
            if not idle:
                # 全都在派发中。宁可让 dict 暂时超限也不丢数据 —— 真无限增长意味着
                # 有 dispatch 卡死不返回，那是另一个 bug，不该在这里用丢用户发言来
                # 掩盖它。
                logger.warning(
                    "[%s] %d independent ASR turn records are all mid-dispatch; "
                    "keeping them past the cap rather than dropping a final",
                    self.lanlan_name,
                    len(self._core_multimodal_turns),
                )
                break
            del self._core_multimodal_turns[
                min(
                    idle,
                    key=lambda key: self._core_multimodal_turns[key].registered_at,
                )
            ]
        # 所有权的起点是**语音确认那一刻**，不是这行代码执行的时刻：两者之间隔着
        # _send_asr_lifecycle_state() 的投递 await，期间落地的帧是这段发声真正的
        # 开头（用户开口时指的东西）。runtime 已经在每个 SPEECH_CONFIRMED 点记了
        # _asr_turn_audio_started_at，直接用它；只有在它足够新（TTL 之内）时才采
        # 信，免得残值把窗口往前拉到上一轮。
        registered_at = time.monotonic()
        started_at = registered_at
        runtime = getattr(self, "_asr_runtime", None)
        # 读 _asr_turn_onset_at 而不是 _asr_turn_audio_started_at：后者在两条
        # SPEECH_CONFIRMED 路径上是 _send_asr_lifecycle_state() 投递完成之后才打
        # 的，用它当起点会把投递窗口里拍的帧误判成不属于这段发声。
        onset = getattr(runtime, "_asr_turn_onset_at", None)
        if not isinstance(onset, (int, float)):
            onset = getattr(runtime, "_asr_turn_audio_started_at", None)
        # 这里判的是「这个 onset 是不是本回合的」，不是「帧新不新鲜」——帧的新鲜度
        # 由 _snapshot_core_multimodal_turn 在冻结时另行 fail-closed。原来借用帧的
        # 5 秒 TTL，会在**合法但迟到**的注册上误判：重叠发声排在一个 provider final
        # 后面（该超时最长可到 40 秒），或 ASR 重连，都会让 onset 早于注册超过 5 秒。
        # 一旦误判，started_at 退回注册时刻、prerecord 缓冲被清、真实开口以来的帧
        # 全部不采纳 —— 明明屏幕/摄像头一直有输入，这一轮却退化成纯文本。
        #
        # 改用「一个回合等到建记录最长能等多久」为界：provider final 的最长超时
        # （registry 里最大 40s）再留一倍余量。仍然拒绝未来时刻的 onset，也仍然
        # 有生成号基线兜底「绝不复用上一轮的帧」。
        onset_known = (
            isinstance(onset, (int, float))
            and 0.0 <= registered_at - float(onset)
            <= _ONSET_TRUST_WINDOW_S
        )
        if onset_known:
            started_at = float(onset)
        record = _CoreMultimodalTurnRecord(
            turn_id=turn_id,
            session_epoch=token.ingress.session_epoch,
            route_generation=self._voice_input_transition_generation,
            start_image_generation=self._independent_visual_generation,
            started_at=started_at,
            registered_at=registered_at,
        )
        self._core_multimodal_turns[turn_id] = record
        self._attach_prerecord_visual_validation_tasks(record)
        # 缓存里那张如果就是在这个窗口里拍的，直接并进来。生成号基线同样要让开
        # 它，否则 accepts() 会以 generation 为由把它挡在外面；而单槽缓存只留最新
        # 一张，更早的本来就找不回来了。
        #
        # 只有拿到可信的 onset 时才这么做。拿不到时时间戳区分不了「上一轮的残帧」
        # 和「本轮开头的帧」——monotonic 在 Windows 上是 ~15ms 粒度，注册时刻和刚
        # 才那帧的拍摄时刻完全可能相等——此时必须退回生成号基线这条硬判据，
        # 保住"绝不复用上一轮的帧"。
        retained = self._prerecord_visual_frames
        self._prerecord_visual_frames = []
        latest = self._latest_independent_visual_frame
        candidates = list(retained)
        if latest is not None and all(
            item.generation != latest.generation for item in candidates
        ):
            candidates.append(latest)
        if onset_known:
            eligible = [
                item
                for item in candidates
                if item.session_epoch == record.session_epoch
                and item.route_generation == record.route_generation
                and item.captured_at >= record.started_at
            ]
            eligible.sort(key=lambda item: (item.captured_at, item.generation))
            for item in eligible:
                record.start_image_generation = min(
                    record.start_image_generation,
                    item.generation - 1,
                )
                record.observe(item)

    def _snapshot_core_multimodal_turn(
        self,
        turn_id: str,
        transcript: str,
    ) -> MultimodalTurn | None:
        """Freeze the frame owned by ``turn_id``; stale frames are fail-closed."""

        self._ensure_asr_runtime_state()
        record = self._core_multimodal_turns.get(turn_id)
        if record is None:
            return None
        if record.invalidated.is_set():
            # 记录留着只是为了别把这条 final 的**话**弄丢；后继 prepare 已经接管了
            # 视觉所有权，这些帧属于新那一轮。返回 None = 走纯文本提交（这是正常的
            # 无图路径，不是失败路径）。
            return None
        # 冻结前再采一次端点：staging 只在有帧落地时跑，最后一帧之后到 final 之间
        # 的封口要在这里补上，否则截止值会漏。
        self._mark_independent_asr_endpoint_if_sealed()
        snapshot_at = time.monotonic()
        if record.last_frame is None:
            latest = self._latest_independent_visual_frame
            if (
                latest is not None
                # 走 record.accepts 而不是自己再抄一遍条件：兜底路径同样要受端点
                # 截止约束，否则端点之后拍的那张会从缓存溜进本回合。
                and record.accepts(latest)
                and snapshot_at - latest.captured_at
                <= self._independent_visual_frame_ttl_s
            ):
                record.adopt_single_frame(latest)
        newest = record.last_frame
        if (
            newest is None
            or record.route_generation != self._voice_input_transition_generation
            or newest.session_epoch != record.session_epoch
            or newest.route_generation != record.route_generation
            or newest.captured_at < record.started_at
            # 只有最新那张要过 TTL：它回答的是"画面流还活着吗"。开头/中间那两张
            # 本来就比 TTL 老（一次发声可以说很久），它们的有效性由"落在本回合
            # 窗口内"保证，不该按新鲜度否掉。
            or snapshot_at - newest.captured_at
            > self._independent_visual_frame_ttl_s
        ):
            return None
        frames = tuple(
            frame for frame in record.sampled_frames() if record.accepts(frame)
        )
        if not frames:
            return None
        return MultimodalTurn(
            turn_id=turn_id,
            session_epoch=record.session_epoch,
            route_generation=record.route_generation,
            start_image_generation=record.start_image_generation,
            image_generation=newest.generation,
            captured_at=newest.captured_at,
            images=tuple(frame.image_b64 for frame in frames),
            transcript=transcript,
            source=newest.source,
            request_id=newest.request_id,
        )
    def _schedule_core_asr_cleanup(
        self,
        awaitable: Awaitable[Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        tasks = getattr(self, "_core_asr_cleanup_tasks", None)
        if tasks is None:
            tasks = set()
            self._core_asr_cleanup_tasks = tasks
        task = asyncio.create_task(awaitable, name=name)
        tasks.add(task)
        task.add_done_callback(
            lambda completed: AsrRuntimeMixin._core_asr_cleanup_done(
                self,
                completed,
            )
        )
        return task

    def _core_asr_cleanup_done(self, task: asyncio.Task[Any]) -> None:
        self._core_asr_cleanup_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "[%s] Core ASR cleanup task %s failed error_type=%s",
                self.lanlan_name,
                task.get_name(),
                type(error).__name__,
            )

    def _replace_voice_input_audio_pipeline(
        self,
        *,
        nr_enabled: bool,
    ) -> asyncio.Task[Any]:
        stale_pipeline = self._voice_input_audio_pipeline
        self._voice_input_audio_pipeline = VoiceInputAudioPipeline(
            nr_enabled=nr_enabled,
        )
        self._voice_input_pipeline_failed = False

        async def close_stale_pipeline() -> None:
            try:
                await stale_pipeline.close()
            except Exception:
                logger.warning(
                    "[%s] voice input audio pipeline close failed",
                    self.lanlan_name,
                )

        return AsrRuntimeMixin._schedule_core_asr_cleanup(
            self,
            close_stale_pipeline(),
            name="core-voice-input-pipeline-close",
        )

    def _begin_asr_route_operation(self) -> int:
        self._retire_core_speaker_installation()
        self._asr_route_operation_generation += 1
        return self._asr_route_operation_generation

    def _asr_route_operation_matches(self, operation_generation: int) -> bool:
        return operation_generation == self._asr_route_operation_generation

    def _set_microphone_route(
        self,
        mode: Literal["native", "independent", "blocked"],
    ) -> None:
        if mode not in {"native", "independent", "blocked"}:
            raise ValueError("MICROPHONE_ROUTE_INVALID")
        leaving_blocked = self._asr_route_mode == "blocked" and mode != "blocked"
        if mode != self._asr_route_mode:
            self._retire_core_speaker_installation()
            self._microphone_route_generation += 1
        if mode != "blocked":
            # A live route means no committed pipeline failure owns it any
            # more, so release the ingress fail-closed latch here -- the one
            # place that decides the route is usable again. Leaving it to the
            # pipeline replacement instead is what let an unrelated toggle
            # reopen ingress mid-failure.
            self._voice_input_pipeline_failure_token = None
            # Re-arm the one-shot text-mode notice for the next episode, and
            # the lease-resync signal now that a live route exists again.
            self._blocked_text_mode_microphone_signal_state = None
            self._blocked_text_mode_microphone_delivery_state = None
            if leaving_blocked:
                self._voice_lease_resync_signal_state = None
            self._voice_lease_resync_delivery_state = None
            self._voice_lease_resync_suppressed = False
        self._asr_route_mode = mode

        if mode != "blocked":
            self._visual_route_mode = mode
        visual_route_mode = self._visual_route_mode
        self._sync_realtime_visual_delivery_mode(visual_route_mode)
        if mode == "blocked" and visual_route_mode != "independent":
            # A blocked route is unresolved, not permission to fall back to raw
            # provider vision. Native mode clears the session fence, so re-arm
            # it after restoring the remembered policy on a replacement session.
            self._block_realtime_raw_visual_delivery()

    def _block_realtime_raw_visual_delivery(self) -> None:
        session = getattr(self, "session", None)
        block_raw_visual_delivery = getattr(
            session,
            "block_raw_visual_delivery",
            None,
        )
        if not callable(block_raw_visual_delivery):
            return
        try:
            block_raw_visual_delivery()
        except Exception as exc:
            logger.warning(
                "[%s] raw visual delivery fence failed: %s",
                self.lanlan_name,
                exc,
            )

    def _sync_realtime_visual_delivery_mode(
        self,
        route_mode: Literal["native", "independent", "blocked"],
    ) -> None:
        """Apply the ASR strategy to the session's provider-neutral visual policy."""

        # Keep microphone ownership and visual delivery as separate contracts.
        # Independent ASR never authorizes raw frames to ride the Realtime
        # provider connection. Native audio keeps the session's established
        # capability routing (raw native vision or its legacy fallback).
        session = getattr(self, "session", None)
        set_visual_delivery_mode = getattr(
            session,
            "set_visual_delivery_mode",
            None,
        )
        if not callable(set_visual_delivery_mode):
            return
        if route_mode == "independent":
            # Independent ASR owns raw frames in Core until transcript final.
            # Arming the provider fence is sufficient; selecting the legacy
            # external-description mode would reintroduce image annotation in
            # an ordinary user conversation.
            self._block_realtime_raw_visual_delivery()
            return
        if route_mode != "native":
            return
        try:
            set_visual_delivery_mode("native")
            allow_raw_visual_delivery = getattr(
                session,
                "allow_raw_visual_delivery",
                None,
            )
            if callable(allow_raw_visual_delivery):
                allow_raw_visual_delivery()
        except Exception as exc:
            # This setter only updates local session policy. A broken or stale
            # session must not take the independent-ASR microphone route down.
            self._block_realtime_raw_visual_delivery()
            logger.warning(
                "[%s] visual delivery mode sync failed: %s",
                self.lanlan_name,
                exc,
            )

    def _capture_ingress_token(self, _lifecycle=None) -> VoiceIngressToken:
        return self._asr_runtime.capture_ingress_token(
            connection_id=self._voice_lease_connection_id,
            lease_generation=self._voice_lease_generation,
            route_generation=self._microphone_route_generation,
        )

    def _capture_native_ingress_token(self) -> VoiceIngressToken:
        return self._capture_ingress_token()

    def _capture_core_asr_operation_identity(self) -> tuple[object, ...]:
        return (
            self._asr_route_operation_generation,
            self._voice_input_transition_generation,
            self._voice_lease_connection_id,
            self._voice_lease_generation,
            self._voice_lease_owner,
            self._voice_lease_hard_muted,
            self._voice_lease_focus_suppressed,
            getattr(self, "session", None),
            self._capture_ingress_token(),
            self._asr_route_mode,
            str(getattr(self, "core_api_type", "") or "").strip().lower(),
            self._independent_asr_route_key,
            self._independent_asr_provider,
        )

    @staticmethod
    def _core_asr_identity_ingress_token(
        identity: tuple[object, ...],
    ) -> VoiceIngressToken:
        # The operation identity is a positional tuple; keep this a real
        # runtime check rather than an assert (asserts vanish under
        # ``python -O``).
        token = identity[8]
        if not isinstance(token, VoiceIngressToken):
            raise TypeError("CORE_ASR_IDENTITY_INGRESS_TOKEN_INVALID")
        return token

    def _core_asr_operation_identity_matches(
        self,
        identity: tuple[object, ...],
        *,
        include_runtime_identity: bool = True,
    ) -> bool:
        (
            route_operation_generation,
            voice_transition_generation,
            connection_id,
            lease_generation,
            owner,
            hard_muted,
            focus_suppressed,
            session_ref,
            ingress_token,
            route_mode,
            core_type,
            route_key,
            provider,
        ) = identity
        if (
            route_operation_generation != self._asr_route_operation_generation
            or voice_transition_generation != self._voice_input_transition_generation
            or connection_id != self._voice_lease_connection_id
            or lease_generation != self._voice_lease_generation
            or owner != self._voice_lease_owner
            or hard_muted != self._voice_lease_hard_muted
            or focus_suppressed != self._voice_lease_focus_suppressed
            or session_ref is not getattr(self, "session", None)
            or route_mode != self._asr_route_mode
            or core_type
            != str(getattr(self, "core_api_type", "") or "").strip().lower()
            or route_key != self._independent_asr_route_key
            or provider != self._independent_asr_provider
        ):
            return False
        return bool(
            not include_runtime_identity
            or ingress_token == self._capture_ingress_token()
        )

    def _ingress_token_matches(self, token: VoiceIngressToken) -> bool:
        return bool(
            token.connection_id == self._voice_lease_connection_id
            and token.lease_generation == self._voice_lease_generation
            and token.route_generation == self._microphone_route_generation
        )

    def _voice_input_accepts_pcm(self) -> bool:
        owner = self._voice_lease_owner
        active_identity = self._voice_input_registry.active_identity
        owner_has_target = bool(
            owner in {"core", "game"}
            and active_identity is not None
            and active_identity.namespace == "builtin"
            and active_identity.name
            == (
                BuiltinVoiceInputConsumer.CORE_CHAT.value
                if owner == "core"
                else BuiltinVoiceInputConsumer.GAME.value
            )
            and self._voice_input_registry.active_accepts_input
        )
        return bool(
            self._voice_lease_synchronized
            and owner_has_target
            and not self._voice_lease_hard_muted
            and not self._voice_lease_focus_suppressed
            and not self._voice_input_suppressed
            and not getattr(self, "_voice_input_external_suppressions", set())
        )

    async def set_voice_input_suppressed(
        self,
        reason: str,
        *,
        suppressed: bool,
    ) -> None:
        """Apply an application-level PCM gate without becoming a consumer."""

        normalized = str(reason or "").strip()
        if not normalized:
            raise ValueError("suppression reason must be non-empty")
        if type(suppressed) is not bool:
            raise TypeError("suppressed must be bool")
        reasons = getattr(self, "_voice_input_external_suppressions", None)
        if reasons is None:
            reasons = set()
            self._voice_input_external_suppressions = reasons
        changed = False
        if suppressed:
            if normalized not in reasons:
                reasons.add(normalized)
                changed = True
        elif normalized in reasons:
            reasons.discard(normalized)
            changed = True
        if not changed:
            return
        self._invalidate_voice_pcm_sync(normalized)
        if suppressed:
            if getattr(self, "_asr_route_mode", "blocked") == "native":
                await self._reset_native_audio_turn(normalized)
            else:
                await self._abort_independent_asr(normalized)

    def _retire_core_speaker_installation(self) -> None:
        """Fence evidence before route teardown can suspend; retain the goal."""
        runtime = getattr(self, "_asr_runtime", None)
        retire = getattr(runtime, "retire_speaker_verifier_authority", None)
        if callable(retire):
            retire()
        self._speaker_verifier_install_receipt = None
        self._speaker_verifier_install_fence = None
        self._speaker_shadow_factory = None

    @property
    def speaker_verifier_participating(self) -> bool:
        # Text sessions are not failed voice installations. During start the
        # route may be ready before Core publishes is_active.
        return bool(
            getattr(self, "input_mode", "audio") == "audio"
            and (
                getattr(self, "_asr_route_mode", "blocked") != "blocked"
                or getattr(self, "is_active", False)
            )
        )

    def _speaker_installation_fence(self) -> tuple:
        return (
            id(self._asr_runtime),
            self._asr_route_operation_generation,
            self._microphone_route_generation,
            id(getattr(self, "session", None)),
            self._capture_ingress_token().session_epoch,
            id(getattr(self._asr_runtime, "_asr_detector", None)),
        )

    def speaker_verifier_installation_status(
        self, activation_revision: str,
    ) -> SpeakerVerifierInstallReceipt:
        spec = getattr(self, "_speaker_verifier_spec", None)
        if spec is None or spec.activation_revision != activation_revision:
            return SpeakerVerifierInstallReceipt(None, SpeakerVerifierInstallOutcome.STALE)
        if not spec.requested_enabled or spec.revocable_authority.state is SpeakerVerifierAuthorityState.REVOKED:
            return SpeakerVerifierInstallReceipt(None, SpeakerVerifierInstallOutcome.REVOKED)
        if not self.speaker_verifier_participating:
            return SpeakerVerifierInstallReceipt(None, SpeakerVerifierInstallOutcome.DEFERRED_ROUTE)
        if (
            self._asr_route_mode != "independent"
            or not self._asr_runtime._speaker_verifier_route_supported()
        ):
            return SpeakerVerifierInstallReceipt(None, SpeakerVerifierInstallOutcome.UNSUPPORTED_ROUTE)
        receipt = getattr(self, "_speaker_verifier_install_receipt", None)
        if (
            receipt is None
            or getattr(self, "_speaker_verifier_install_fence", None) != self._speaker_installation_fence()
            or receipt.identity is None
            or receipt.identity.activation_revision != activation_revision
        ):
            return SpeakerVerifierInstallReceipt(None, SpeakerVerifierInstallOutcome.DEFERRED_ROUTE)
        actual = getattr(self._asr_runtime, "_speaker_verifier_install_receipt", None)
        if receipt.outcome is SpeakerVerifierInstallOutcome.INSTALLED and (
            actual is None
            or actual.identity != receipt.identity
            or not self._asr_runtime._speaker_install_identity_current(receipt.identity)
        ):
            return SpeakerVerifierInstallReceipt(receipt.identity, SpeakerVerifierInstallOutcome.STALE)
        if receipt.outcome is SpeakerVerifierInstallOutcome.INSTALLED:
            receipt = actual
        if receipt.outcome is SpeakerVerifierInstallOutcome.INSTALLED and getattr(self._asr_runtime, "_speaker_verifier_degraded", False):
            return replace(receipt, outcome=SpeakerVerifierInstallOutcome.FAILED)
        return receipt

    async def set_speaker_verifier_spec(
        self, spec: SpeakerVerifierSpec | None,
    ) -> SpeakerVerifierInstallReceipt:
        self._ensure_asr_runtime_state()
        if getattr(self, "_speaker_verifier_spec", None) is not spec:
            self._retire_core_speaker_installation()
            self._speaker_verifier_spec = spec
        return await self.reconcile_speaker_verifier()

    async def reconcile_speaker_verifier(self) -> SpeakerVerifierInstallReceipt:
        """One owner per manager, with queued callers coalescing to latest spec."""
        self._ensure_asr_runtime_state()
        lock = getattr(self, "_speaker_verifier_reconcile_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._speaker_verifier_reconcile_lock = lock
        requested = getattr(self, "_speaker_verifier_spec", None)
        async with lock:
            spec = getattr(self, "_speaker_verifier_spec", None)
            if requested is not spec:
                return SpeakerVerifierInstallReceipt(None, SpeakerVerifierInstallOutcome.STALE)
            if spec is not None and spec.requested_enabled and spec.revocable_authority.state is not SpeakerVerifierAuthorityState.REVOKED:
                status = self.speaker_verifier_installation_status(spec.activation_revision)
                if status.outcome in {
                    SpeakerVerifierInstallOutcome.INSTALLED,
                    SpeakerVerifierInstallOutcome.DEFERRED_ROUTE,
                    SpeakerVerifierInstallOutcome.UNSUPPORTED_ROUTE,
                } and (status.outcome is not SpeakerVerifierInstallOutcome.DEFERRED_ROUTE or not self.speaker_verifier_participating):
                    return status
            fence = self._speaker_installation_fence()
            identity = self._asr_runtime.create_speaker_verifier_install_identity(
                manager_identity=id(self),
                route_generation=self._asr_route_operation_generation,
                activation_revision=spec.activation_revision if spec is not None else "disabled",
            )
            receipt = await self._asr_runtime.install_speaker_verifier(spec, identity)
            if (
                getattr(self, "_speaker_verifier_spec", None) is not spec
                or fence != self._speaker_installation_fence()
            ):
                self._asr_runtime.retire_speaker_verifier_authority()
                return replace(receipt, outcome=SpeakerVerifierInstallOutcome.STALE)
            self._speaker_verifier_install_receipt = receipt
            self._speaker_verifier_install_fence = fence
            return receipt

    async def set_speaker_verifier_factory(
        self,
        factory: SpeakerShadowFactory | None,
        *,
        activation_generation: str,
    ) -> bool | VoiceIdentityActivationResult:
        """Compatibility adapter; a supplied instance cannot be rebuilt.

        Persistent configuration must use a spec's lazy factory_builder. This
        one-shot adapter never carries a closed factory across route changes.
        """
        authority = SpeakerVerifierAuthority()
        authority.commit()
        consumed = False

        def build(_runtime, _identity):
            nonlocal consumed
            if consumed:
                raise RuntimeError("legacy speaker factory requires a fresh configuration")
            consumed = True
            return factory

        spec = SpeakerVerifierSpec(
            profile_generation=activation_generation if factory is not None else None,
            activation_revision=uuid4().hex,
            requested_enabled=factory is not None,
            enforce=bool(getattr(factory, "enforces_admission", True)),
            revocable_authority=authority,
            factory_builder=build if factory is not None else None,
        )
        try:
            receipt = await self.set_speaker_verifier_spec(spec)
        finally:
            if not consumed and factory is not None:
                consumed = True
                self._asr_runtime._close_speaker_verifier_factory(factory)
        if receipt.outcome is SpeakerVerifierInstallOutcome.INSTALLED:
            return VoiceIdentityActivationResult.READY
        if receipt.outcome is SpeakerVerifierInstallOutcome.REVOKED and factory is None:
            return VoiceIdentityActivationResult.READY
        if receipt.outcome is SpeakerVerifierInstallOutcome.UNSUPPORTED_ROUTE:
            self._asr_runtime._record_unsupported_speaker_route()
            return VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
        if receipt.outcome is SpeakerVerifierInstallOutcome.DEFERRED_ROUTE:
            return VoiceIdentityActivationResult.ACTIVATION_PENDING
        return VoiceIdentityActivationResult.RUNTIME_DEGRADED

    def set_independent_asr_handshake(self, value: object) -> None:
        # Record the frontend's authoritative independent-ASR toggle carried by
        # the start_session message (websocket_router). Strictly typed: only a
        # real bool is accepted; anything else (missing field from an older
        # frontend, malformed payloads) clears the override so the route
        # decision falls back to the persisted setting. The handshake is
        # deliberately NOT persisted server-side: persistence stays the
        # settings POST's job, this value only pins the route decision for the
        # session the user just started.
        self._ensure_asr_runtime_state()
        self._independent_asr_handshake_override = (
            value if isinstance(value, bool) else None
        )

    def set_voice_input_resource_optimization_handshake(
        self,
        value: object,
    ) -> None:
        """Pin one session's authoritative resource-optimization preference."""
        self._ensure_asr_runtime_state()
        self._voice_input_resource_optimization_handshake_override = (
            value if isinstance(value, bool) else None
        )

    async def _start_independent_asr_if_enabled(
        self,
        input_mode: str,
        *,
        preserve_hot_swap_audio: bool = False,
        handshake_override=...,
        resource_optimization_override=...,
        connect_budget_seconds: float | None = None,
    ) -> None:
        """Resolve the microphone route for one session start.

        ``handshake_override`` carries the start_session handshake belonging to
        THIS start operation, snapshotted by ``start_session`` before its first
        await. Ellipsis means "not supplied" — the internal re-entry paths
        (hot-swap, device change) have no request of their own and reuse the
        accepted live session's optimization choice.

        ``connect_budget_seconds`` bounds only the PROVIDER CONNECT. A caller
        working against a deadline (the dedupe reroute) passes what is left of
        it; when that cannot cover a whole connect-and-retry phase this returns
        with the route on its blocked placeholder rather than produce a verdict
        nobody is still listening for. Checked here rather than at the call site
        on purpose: the cheap outcomes -- independent ASR disabled by handshake
        or by the persisted setting, or a Core that owns recognition natively --
        settle on ``native`` without connecting to anything, and gating those on
        a connect budget would strand a microphone that had nothing to wait for
        (Codex P2).
        """
        self._ensure_asr_runtime_state()
        operation_generation = self._begin_asr_route_operation()
        await self._close_independent_asr(
            next_route_mode="blocked",
            preserve_hot_swap_audio=preserve_hot_swap_audio,
            operation_generation=operation_generation,
        )
        if not self._asr_route_operation_matches(operation_generation):
            return
        self._omni_mic_audio_bytes = 0
        core_type = str(getattr(self, "core_api_type", "") or "").strip().lower()
        core_asr_capabilities = get_asr_core_capabilities(core_type)
        supports_independent_asr = bool(
            core_asr_capabilities is None
            or core_asr_capabilities.supports_independent_asr
        )
        self._independent_asr_route_key = core_type
        session_epoch = self._capture_ingress_token().session_epoch
        start_connection_id = self._voice_lease_connection_id
        start_session_ref = getattr(self, "session", None)

        def route_operation_unclaimed() -> bool:
            # No competing route operation has run or completed: the route is
            # still the blocked placeholder this start installed.
            return bool(
                self._asr_route_operation_matches(operation_generation)
                and self._asr_route_mode == "blocked"
                and self._independent_asr_route_key == core_type
                and self._independent_asr_provider is None
                and str(getattr(self, "core_api_type", "") or "").strip().lower()
                == core_type
                and getattr(self, "session", None) is start_session_ref
            )

        def core_start_is_current() -> bool:
            # Route setup is fenced on competing route operations and on
            # websocket replacement. Lease state (owner/mute/focus) gates PCM
            # at ingress, not routing: the frontend flips owner to "core"
            # only after session_started, so a lease-state gate here would
            # permanently block every cold start.
            return bool(
                route_operation_unclaimed()
                and self._voice_lease_connection_id == start_connection_id
            )

        if input_mode != "audio":
            self._set_microphone_route("blocked")
            # A text session blocks the route for its whole life, so revoke the
            # lease rather than let a client that missed session_started keep
            # uploading into it -- but deliver the mic-stop ack to the lease
            # holder first, and re-fence in between. The chokepoint owns that
            # order; the notice is the only thing specific to this exit.
            await self._fail_closed_voice_route(
                "text_session_active",
                operation_generation=operation_generation,
                voice_owner_notice=(
                    {"type": "session_started", "input_mode": "text"}
                    if input_mode == "text"
                    else None
                ),
            )
            return
        try:
            # strict=True or this except branch is unreachable for the failure
            # it exists to catch: the plain read swallows every IO/JSON error
            # and returns the SAME empty dict as a file that simply has no
            # settings yet, so an unreadable or malformed user_preferences.json
            # fell through to `enabled = False` below and quietly selected the
            # native Omni route -- overriding a persisted choice that required
            # independent ASR, which is the opposite of fail-closed. An absent
            # file still returns {} under strict, so a genuine first run keeps
            # defaulting normally.
            settings = await _core_facade.aload_global_conversation_settings(strict=True)
        except Exception:
            # Fail-closed is only the safe answer for a user who WANTS
            # independent ASR: for them the persisted read is the authority and
            # falling back to native would override their choice. A user whose
            # frontend handshake says the feature is OFF has no such choice to
            # protect, and this path runs for EVERY audio session -- so
            # revoking here would kill the microphone of someone who never
            # enabled the feature, over a settings file they may not even know
            # exists. Before this PR that case simply used the native route;
            # keep it that way. The handshake is read early because the
            # unreadable settings cannot answer the question at all.
            unreadable_handshake = (
                self._independent_asr_handshake_override
                if handshake_override is ...
                else handshake_override
            )
            if not supports_independent_asr or unreadable_handshake is False:
                if not core_start_is_current():
                    return
                # Same landing as the ordinary `not enabled` path below.
                self._set_microphone_route("native")
                await self._send_core_asr_status(
                    AsrStatusEvent(
                        code="ASR_INDEPENDENT_DISABLED",
                        provider=core_type or "unknown",
                        session_epoch=session_epoch,
                    )
                )
                return
            # The persisted choice cannot be read, but the handshake still
            # requires independent ASR. Fail closed for raw visual delivery
            # before any later failure handling or callback can run.
            if not core_start_is_current():
                return
            self._visual_route_mode = "independent"
            self._sync_realtime_visual_delivery_mode("independent")
            await self._fail_closed_voice_route(
                "asr_settings_unreadable",
                operation_generation=operation_generation,
                still_current=core_start_is_current,
                status=AsrStatusEvent(
                    code="ASR_INDEPENDENT_FAILED",
                    provider=core_type or "unknown",
                    session_epoch=session_epoch,
                ),
            )
            return
        if not core_start_is_current():
            return
        nr_enabled = settings.get("noiseReductionEnabled", True) is not False
        pipeline_cleanup = None
        async with self._voice_input_pipeline_transition_lock:
            if not core_start_is_current():
                return
            self._voice_input_noise_reduction_enabled = nr_enabled
            if self._voice_input_audio_pipeline.nr_enabled != nr_enabled:
                pipeline_cleanup = AsrRuntimeMixin._replace_voice_input_audio_pipeline(
                    self,
                    nr_enabled=nr_enabled,
                )
        if pipeline_cleanup is not None:
            await asyncio.shield(pipeline_cleanup)
            if not core_start_is_current():
                return
        # Prefer this operation's own snapshot; a concurrent start_session can
        # have replaced or cleared the shared field during the awaits above.
        handshake_enabled = (
            self._independent_asr_handshake_override
            if handshake_override is ...
            else handshake_override
        )
        if not supports_independent_asr:
            # Some Core routes own speech recognition natively. Their route
            # capability takes precedence over both the persisted preference
            # and the per-session handshake, so a stale enabled toggle cannot
            # turn a healthy native voice session into a blocked one.
            enabled = False
        elif handshake_enabled is not None:
            # The start_session handshake carries the frontend's authoritative
            # toggle; it overrides the persisted read, which is stale when the
            # settings POST failed or was still in flight at session start.
            enabled = handshake_enabled
        else:
            enabled = bool(settings.get("independentAsrEnabled", False))
        optimization_handshake = resource_optimization_override
        if resource_optimization_override is ...:
            optimization_handshake = getattr(
                self,
                "_voice_input_resource_optimization_session_value",
                None,
            )
            if optimization_handshake is None:
                optimization_handshake = getattr(
                    self,
                    "_voice_input_resource_optimization_handshake_override",
                    None,
                )
        optimization_value = (
            optimization_handshake
            if optimization_handshake is not None
            else settings.get("voiceInputResourceOptimizationEnabled", True)
        )
        resolved_optimization_value = optimization_value is not False
        if resource_optimization_override is not ...:
            # Only an accepted start_session call supplies this argument.
            # Losing/deduplicated requests may still overwrite the manager-level
            # handshake field, so internal provider restarts must use this
            # session-owned snapshot instead.
            self._voice_input_resource_optimization_session_value = (
                resolved_optimization_value
            )
        if not enabled:
            self._set_microphone_route("native")
            await self._send_core_asr_status(
                AsrStatusEvent(
                    code="ASR_INDEPENDENT_DISABLED",
                    provider=core_type or "unknown",
                    session_epoch=session_epoch,
                )
            )
            return
        # Close the raw-image path as soon as the independent-ASR choice is
        # authoritative. Provider connection may take seconds or fail; neither
        # window may let screen/camera or callback images reach Realtime raw.
        self._visual_route_mode = "independent"
        self._sync_realtime_visual_delivery_mode("independent")
        if (
            connect_budget_seconds is not None
            and connect_budget_seconds < ASR_CONNECT_TOTAL_BUDGET_SECONDS
        ):
            # Out of budget: leave the route on the blocked placeholder installed
            # above, which is exactly the state the caller would have re-acked
            # without re-deciding at all.
            logger.info(
                "[%s] independent ASR connect skipped: %.2fs of budget left,"
                " under the %.1fs connect ceiling",
                self.lanlan_name,
                connect_budget_seconds,
                ASR_CONNECT_TOTAL_BUDGET_SECONDS,
            )
            return
        start_kwargs: dict[str, object] = {
            "route_key": core_type,
            "resource_optimization_enabled": resolved_optimization_value,
            # Session language follows the Core-tracked user language; the
            # asr_client factory maps it per provider and falls back to
            # automatic detection when it is unset or unsupported.
            "user_language": getattr(self, "user_language", None),
        }
        result = await self._asr_runtime.start(**start_kwargs)
        current_epoch = self._capture_ingress_token().session_epoch
        if not core_start_is_current():
            route_fields_still_ours = bool(
                self._asr_route_operation_matches(operation_generation)
                and self._asr_route_mode == "blocked"
                and self._independent_asr_route_key == core_type
                and self._independent_asr_provider is None
            )
            if route_fields_still_ours:
                # Websocket replacement or session swap without a competing
                # route operation: nobody else owns the runtime, so close the
                # candidate and clear the route key so a later reconcile
                # retries.
                await self._abort_independent_asr("stale_core_start")
                # Re-check after the abort await: a competing start may have
                # installed its own blocked placeholder meanwhile, and
                # clearing that key would silently kill it before it even
                # reaches the native fallback.
                if (
                    self._asr_route_operation_matches(operation_generation)
                    and self._independent_asr_route_key == core_type
                ):
                    self._independent_asr_route_key = None
            return
        if result.failure_code == "ASR_START_STALE":
            # Runtime-level invalidation (e.g. a new websocket connection)
            # without a competing route operation: clear the route key so a
            # later reconcile retries instead of treating the route as done.
            self._independent_asr_route_key = None
            return
        if result.session_epoch != current_epoch:
            self._independent_asr_route_key = None
            return
        self._independent_asr_provider = result.provider
        if result.status is AsrStartStatus.READY:
            self._set_microphone_route("independent")
            if getattr(self, "_speaker_verifier_spec", None) is not None:
                await self.reconcile_speaker_verifier()
        else:
            # Independent ASR was ENABLED and failed to start (provider connect,
            # credentials, config). Unlike a runtime failure this emits no
            # BLOCKED lifecycle event -- IndependentAsrRuntime.start can never
            # reach _handle_independent_asr_error, the only emitter -- so the
            # client sees a toast and nothing else. Revoke after the route is
            # pinned: _set_microphone_route re-arms the resync suppression for
            # every non-blocked mode.
            self._set_microphone_route("blocked")
            # Generation-only fence on purpose: core_start_is_current() cannot
            # be reused here because _independent_asr_provider was just
            # assigned from the result above, so route_operation_unclaimed()'s
            # "provider is None" clause is deliberately false by now. Nothing
            # awaits between the check above and this call, so the generation
            # is exactly as strong as the original inline revoke.
            await self._fail_closed_voice_route(
                "asr_start_failed",
                operation_generation=operation_generation,
            )

    async def _rerun_route_for_deduped_start(
        self,
        input_mode: str,
        *,
        lease_connection_id: str,
        remaining_deadline_seconds: float,
        handshake_override=...,
        resource_optimization_override=...,
    ) -> None:
        """Re-decide the microphone route for a deduplicated same-mode start.

        A same-mode start that collides with an in-flight one starts nothing of
        its own and only re-acks, so it inherits the in-flight start's verdict.
        That verdict can already be void for THIS requester: a second window
        claims the voice lease (websocket_router does it synchronously before
        firing start_session), which invalidates the in-flight ASR start. That
        start then returns ASR_START_STALE and returns early, leaving the route
        on the "blocked" placeholder its own teardown installed -- and emitting
        no status at all, since the stale exit is upstream of every emitter.
        The ack carries that placeholder, so both windows latch fail-closed and
        the microphone never opens for the session that did start, with nothing
        on screen to explain it. Nothing re-decides in-session either: the only
        other entry is the core-change reconcile.

        Runs while no start is in flight (the caller waits for the count to
        reach 0), so the fence inside ``_start_independent_asr_if_enabled`` sees
        a settled, self-consistent state -- and the handshake passed down is
        this request's own snapshot rather than whatever the shared field holds
        by now.

        Blocked routes only. A settled native/independent verdict is valid for
        whoever ends up holding the microphone, and re-running would tear down
        a healthy provider mid-session for nothing.

        ``lease_connection_id`` is the caller's pre-wait snapshot: the wait is
        seconds long, and a THIRD audio start claiming the microphone during it
        would otherwise get its route configured from this superseded window's
        handshake (Codex P2). The new holder runs this same path with a snapshot
        that does match, so skipping here loses nothing.

        ``remaining_deadline_seconds`` is what is left of the frontend's start
        deadline, handed down as the connect budget. A connect-and-retry phase
        can run to ASR_CONNECT_TOTAL_BUDGET_SECONDS, so starting one without room
        for it would push the re-ack past the point where the client gives up --
        and its timeout fires end_session, tearing down the session that did
        start (Codex P2). Out of budget the bare re-ack is the better trade: it
        is the pre-existing behaviour, and it still reaches the requester in
        time. The budget stops the CONNECT only, never the cheap native
        outcomes -- see _start_independent_asr_if_enabled.
        """
        if input_mode != "audio":
            return
        self._ensure_asr_runtime_state()
        if self._voice_lease_connection_id != lease_connection_id:
            logger.info(
                "[%s] dedupe reroute skipped: voice lease moved on during the wait",
                self.lanlan_name,
            )
            return
        # The in-flight start's OWN mode is the authority, not this request's:
        # the dedupe branch treats a missing ``_starting_input_mode`` as a match,
        # so an audio request can land here against an in-flight text start. Its
        # route is legitimately blocked for the text session's whole life, and
        # re-deciding would hand a live microphone to a session that has no
        # audio path at all.
        if str(getattr(self, "input_mode", "") or "") != "audio":
            return
        if self._asr_route_mode != "blocked":
            return
        await self._start_independent_asr_if_enabled(
            input_mode,
            handshake_override=handshake_override,
            resource_optimization_override=resource_optimization_override,
            connect_budget_seconds=remaining_deadline_seconds,
        )

    def _abandon_core_voice_turn(
        self,
        turn_id: str | None = None,
        *,
        session_ref: object | None = None,
    ) -> None:
        turns = getattr(self, "_core_multimodal_turns", None)
        if turns is not None:
            if turn_id is None:
                for record in turns.values():
                    record.invalidated.set()
                turns.clear()
            else:
                record = turns.pop(turn_id, None)
                if record is not None:
                    record.invalidated.set()
        target_session = (
            session_ref if session_ref is not None else getattr(self, "session", None)
        )
        abandon = getattr(target_session, "abandon_external_voice_turn", None)
        if not callable(abandon):
            return
        try:
            abandon(turn_id)
        except Exception:
            logger.warning(
                "[%s] external ASR dispatch pause release failed",
                self.lanlan_name,
            )

    async def _abort_independent_asr(
        self,
        reason: str,
        *,
        still_current: Callable[[], bool] | None = None,
    ) -> None:
        """Abort the independent run and drop the audio that belonged to it.

        ``still_current`` is for callers that no longer hold the pipeline
        transition lock across this: ``self._asr_runtime.abort`` is an await,
        and a session restart or route replacement landing inside it installs
        a NEW route. Invalidating afterwards would then clear the successor's
        queued and hot-swap audio and drop its microphone input. Re-checked
        after the await for exactly that reason -- checking only on entry
        would leave the window this argument exists to close.
        """

        if still_current is not None and not still_current():
            return
        await self._asr_runtime.abort(reason)
        if still_current is not None and not still_current():
            return
        self._invalidate_voice_pcm_sync(reason)
        await self._voice_input_registry.wait_idle()

    async def _reset_native_audio_turn(
        self,
        reason: str,
        *,
        route_mode: str | None = None,
    ) -> None:
        """Discard the native provider's pending input audio after a PCM hole.

        A native turn is segmented by the provider's server VAD over one
        continuously appended input buffer, so microphone PCM dropped
        mid-utterance is invisible to it: speech from both sides of the hole
        gets concatenated into a single incorrect transcript. Clearing the
        server-side input buffer drops the pre-hole partial utterance
        instead -- the native-route equivalent of invalidating the whole
        candidate turn on the independent route. No-op for sessions without
        an input-buffer clear (text sessions, Gemini).
        """

        if (route_mode or self._asr_route_mode) != "native":
            return
        session_ref = getattr(self, "session", None)
        if getattr(session_ref, "_fatal_error_occurred", False):
            return
        clear_buffer = getattr(session_ref, "clear_audio_buffer", None)
        if not callable(clear_buffer):
            return
        try:
            await clear_buffer()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[%s] native audio turn reset failed reason=%s",
                self.lanlan_name,
                reason,
            )

    async def _invalidate_interrupted_voice_turn(
        self,
        reason: str,
        *,
        abort: "asyncio.Future[None] | None" = None,
    ) -> None:
        """Invalidate whichever route owns a turn broken by dropped PCM.

        The independent abort runs first on purpose: its synchronous prefix
        bumps the audio generation, so a pre-hole frame still in flight in
        the worker fails its ingress-token check instead of being appended
        after the buffer clear. The clear must not simply await the abort to
        completion, though: the abort's first real suspension is a frontend
        status send, under exactly the congestion that caused the overflow.
        So the abort is scheduled, given one loop tick to run its prefix, and
        only re-joined at the end.

        ``abort`` lets a caller that dispatches this method with
        ``_fire_task`` create the abort task itself. That is load-bearing, not
        cosmetic: creating it inside this coroutine puts it one task-
        scheduling layer deeper than the caller, so the caller resumes before
        the abort's first step ever runs and the ordering above is silently
        lost (CodeRabbit). Callers that ``await`` this method directly can
        leave it None.
        """

        owns_abort = abort is None
        if abort is None:
            abort = asyncio.ensure_future(self._abort_independent_asr(reason))
        try:
            if owns_abort:
                await asyncio.sleep(0)
            await self._reset_native_audio_turn(reason)
        finally:
            await abort

    async def _suspend_independent_asr(self, reason: str) -> None:
        await self._asr_runtime.suspend(reason)
        self._invalidate_voice_pcm_sync(reason)
        await self._voice_input_registry.wait_idle()

    async def _close_independent_asr(
        self,
        *,
        next_route_mode: Literal["blocked"],
        preserve_hot_swap_audio: bool = False,
        operation_generation: int | None = None,
    ) -> None:
        self._ensure_asr_runtime_state()
        if operation_generation is None:
            operation_generation = self._begin_asr_route_operation()
        elif not self._asr_route_operation_matches(operation_generation):
            return
        del next_route_mode
        provider = self._independent_asr_provider
        omni_audio_bytes = self._omni_mic_audio_bytes
        self._set_microphone_route("blocked")
        if not preserve_hot_swap_audio:
            self._invalidate_voice_pcm_sync("independent_asr_close")
        else:
            self._voice_input_registry.invalidate_utterance(
                reason="independent_asr_close",
            )
        async def finish_close() -> None:
            async with self._voice_input_pipeline_transition_lock:
                if not self._asr_route_operation_matches(operation_generation):
                    return
                pipeline_cleanup = (
                    AsrRuntimeMixin._replace_voice_input_audio_pipeline(
                        self,
                        nr_enabled=self._voice_input_noise_reduction_enabled,
                    )
                )
            self._independent_asr_provider = None
            self._independent_asr_route_key = None
            await self._voice_input_registry.wait_idle()
            # A successor start advances the route generation and installs its
            # own cancellation-safe close before its first suspension. Once
            # that happens, this retired operation must not re-read the shared
            # runtime and close the successor it no longer owns.
            if self._asr_route_operation_matches(operation_generation):
                await self._asr_runtime.stop_session()
            await asyncio.shield(pipeline_cleanup)
            if omni_audio_bytes:
                logger.info(
                    "[%s] microphone route metrics provider=%s omni_mic_audio_bytes=%d",
                    self.lanlan_name,
                    provider or "blocked",
                    omni_audio_bytes,
                )

        close_cleanup = self._schedule_core_asr_cleanup(
            finish_close(),
            name="core-independent-asr-close",
        )
        # 这段发声窗口的原图只对这一条路由有意义。它们此前唯一的清理点是**下一个**
        # 回合开始时（_begin_core_multimodal_turn），所以屏幕共享着、还没开口就结束
        # 这一集的话，最多 8 张满尺寸 base64 原图会一直挂在常驻的角色管理器上；下
        # 一集 ASR 起步时缓冲区里还掺着上一集的帧，把这一集自己开头的帧挤出上限。
        # 路由身份边界就是它们的生命周期终点。
        #
        # 这里是同步清（此函数从顶部的 operation_generation 检查到这一行没有任何
        # await，create_task 也还没让出控制权），所以不需要再自证身份；provider /
        # route_key 的置空、_asr_runtime.stop_session()、管线关闭与指标日志则一律交给 main
        # 的 finish_close()——它带 transition lock 和 operation_generation 围栏，能
        # 区分同一条连接上的后继回合，比在这里再抄一遍精确。
        self._prerecord_visual_frames = []
        self._latest_independent_visual_frame = None
        await asyncio.shield(close_cleanup)

    async def apply_voice_input_noise_reduction(self, enabled: bool) -> bool:
        """Make a mid-session noise-reduction toggle reach the live microphone.

        The settings endpoint used to update only
        ``OmniRealtimeClient._audio_processor``, but every microphone frame now
        passes through this Core-owned :class:`VoiceInputAudioPipeline` FIRST,
        and that pipeline is built once per session start. It also downsamples
        PC audio to 16 kHz, so the Omni processor downstream sees 16 kHz and
        skips RNNoise entirely -- and independent-ASR routes never reach the
        Omni processor at all. The toggle therefore did nothing at all until
        some later session rebuilt this pipeline, while the endpoint reported
        success (Codex P2).

        Replacing rather than mutating is deliberate and already the house
        pattern here: session start does exactly this when the persisted value
        differs, and the PCM ingress paths guard on
        ``self._voice_input_audio_pipeline is not pipeline_ref``, so frames
        in flight against the old processor are discarded instead of being
        mixed with the new one. A DSP stage being switched on or off mid-stream
        is precisely when dropping the in-flight frame is what you want.

        Returns True when the pipeline was actually rebuilt.
        """

        self._ensure_asr_runtime_state()
        nr_enabled = bool(enabled)
        transition_lock = getattr(
            self,
            "_voice_input_pipeline_transition_lock",
            None,
        )
        if transition_lock is None:
            transition_lock = asyncio.Lock()
            self._voice_input_pipeline_transition_lock = transition_lock

        async def transition() -> bool:
            async with transition_lock:
                self._voice_input_noise_reduction_enabled = nr_enabled
                if self._voice_input_audio_pipeline.nr_enabled == nr_enabled:
                    return False
                pipeline_cleanup = (
                    AsrRuntimeMixin._replace_voice_input_audio_pipeline(
                        self,
                        nr_enabled=nr_enabled,
                    )
                )
            await asyncio.shield(pipeline_cleanup)
            return True

        transition_task = AsrRuntimeMixin._schedule_core_asr_cleanup(
            self,
            transition(),
            name="core-voice-input-pipeline-transition",
        )
        return await asyncio.shield(transition_task)

    async def _reconcile_independent_asr_after_core_change(self) -> None:
        self._ensure_asr_runtime_state()
        core_type = str(getattr(self, "core_api_type", "") or "").strip().lower()
        if core_type == self._independent_asr_route_key:
            # A same-provider hot swap can promote a fresh Realtime session
            # while the microphone route key remains unchanged. Reapply the
            # current abstract route so the replacement inherits the visual
            # delivery policy without restarting ASR or touching PCM routing.
            self._set_microphone_route(self._asr_route_mode)
            if getattr(self, "_speaker_verifier_spec", None) is not None:
                await self.reconcile_speaker_verifier()
            return
        await self._start_independent_asr_if_enabled(
            str(getattr(self, "input_mode", "audio") or "audio"),
            preserve_hot_swap_audio=True,
        )

    def _ensure_audio_stream_worker(self) -> None:
        task = self._audio_stream_worker_task
        if task is not None and not task.done():
            return
        self._audio_stream_worker_task = self._fire_task(
            self._audio_stream_worker_loop()
        )

    def _clear_audio_stream_queue(self, reason: str) -> None:
        dropped = 0
        while True:
            try:
                frame = self._audio_stream_queue.get_nowait()
                self._audio_stream_queue.task_done()
                self._complete_hot_swap_ingress_sequence(frame.ingress_sequence)
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            self._audio_stream_dropped_total += dropped
            logger.info(
                "[%s] audio stream queue cleared reason=%s dropped=%d total_dropped=%d",
                self.lanlan_name,
                reason,
                dropped,
                self._audio_stream_dropped_total,
            )

    def _cancel_audio_stream_worker(self, reason: str) -> None:
        task = self._audio_stream_worker_task
        if task is None:
            return
        if task.done():
            self._audio_stream_worker_task = None
            return
        if task is asyncio.current_task():
            return
        task.cancel()
        self._audio_stream_worker_task = None
        logger.debug(
            "[%s] audio stream worker cancelled reason=%s",
            self.lanlan_name,
            reason,
        )

    async def _send_voice_control_status(
        self,
        message: str,
        *,
        progress: tuple[object, set[str]] | None = None,
        still_current: Callable[[], bool] | None = None,
    ) -> tuple[bool, bool]:
        """Send a mic control-plane status to the current AND voice sockets.

        ``self.websocket`` is the newest socket, which is not necessarily the
        one holding the hardware microphone: this PR deliberately supports a
        recorder superseded by a newer chat window. Lifecycle transitions,
        lease resync requests and blocked-route notices act on the microphone,
        so they must also reach its owner or the teardown never runs there.

        The extra delivery is getattr-guarded rather than folded into
        ``send_status``: that signature is doubled by a large number of focused
        tests, and narrow manager doubles do not carry the notify mixin at all.
        The returned pair records each plane independently so episode retries
        can skip a plane that already accepted this exact status.

        ``still_current`` is re-evaluated between the two planes. The display
        send is an await, and a microphone takeover completing inside it makes
        the voice-owner lookup below resolve the SUCCESSOR's socket -- which
        would hand the new active recorder a notice about the episode it just
        replaced. The post-send episode check at the call sites cannot help:
        it only withholds the ledger commit, and a delivered status cannot be
        retracted.
        """

        if progress is None:
            progress = (None, set())
        delivered_planes = progress[1]
        if "display_delivered" not in delivered_planes:
            if await self.send_status(message):
                delivered_planes.add("display_delivered")
        if "voice_owner_settled" in delivered_planes:
            return "display_delivered" in delivered_planes, True
        if still_current is not None and not still_current():
            # Do not settle the voice plane either: this episode is over, and
            # the caller drops its ledger rather than committing it.
            return "display_delivered" in delivered_planes, False
        voice_owner_resolver = getattr(self, "_voice_owner_socket", None)
        voice_owner_socket = (
            voice_owner_resolver() if callable(voice_owner_resolver) else None
        )
        send_to_voice_owner = getattr(self, "_send_to_voice_owner", None)
        if voice_owner_socket is None or not callable(send_to_voice_owner):
            delivered_planes.add("voice_owner_settled")
            return "display_delivered" in delivered_planes, True
        delivered_socket = await send_to_voice_owner(
            {"type": "status", "message": message}
        )
        if delivered_socket is voice_owner_socket:
            delivered_planes.add("voice_owner_settled")
        return (
            "display_delivered" in delivered_planes,
            "voice_owner_settled" in delivered_planes,
        )

    async def _maybe_signal_voice_lease_resync(self) -> None:
        """Nudge a client whose PCM is dropped only because no lease is set.

        Deliberate suppression (hard mute, focus suppression, game owner)
        must stay silent; only an unsynchronized lease or an installed
        ``none`` owner means the sender lost a lease it still believes it
        holds. One signal per connection and lease state keeps the channel
        quiet while every later lease change re-arms it.
        """

        async with self._asr_notification_lock:
            signal_state = self._voice_lease_resync_episode()
            if signal_state is None:
                self._voice_lease_resync_delivery_state = None
                return
            if signal_state == self._voice_lease_resync_signal_state:
                return
            progress = self._voice_lease_resync_delivery_state
            if progress is None or progress[0] != signal_state:
                progress = (signal_state, set())
                self._voice_lease_resync_delivery_state = progress
            await self._send_voice_control_status(
                still_current=(
                    lambda: self._voice_lease_resync_episode() == signal_state
                ),
                message=json.dumps(
                    {
                        "code": "VOICE_INPUT_LEASE_RESYNC_REQUIRED",
                        "details": {
                            "reason": (
                                "lease_unsynchronized"
                                if not signal_state[2]
                                else "owner_none"
                            ),
                        }
                    }
                ),
                progress=progress,
            )
            if self._voice_lease_resync_episode() != signal_state:
                return
            if {
                "display_delivered",
                "voice_owner_settled",
            }.issubset(progress[1]):
                self._voice_lease_resync_signal_state = signal_state
                self._voice_lease_resync_delivery_state = None

    def _voice_lease_resync_episode(
        self,
    ) -> tuple[str, int, bool, str, int] | None:
        if self._voice_lease_resync_suppressed:
            # The backend revoked this lease on purpose (fail-closed route, or
            # a text session took over). Asking the client to resync would make
            # a still-recording window re-send its snapshot and re-establish
            # exactly the lease we just dropped -- a revoke/resync ping-pong.
            # Deliberately not keyed on route == "blocked": blocked is also the
            # legitimate cold-start placeholder, where the signal IS wanted.
            return None
        if (
            self._voice_lease_hard_muted
            or self._voice_lease_focus_suppressed
            or self._voice_lease_owner == "game"
        ):
            return None
        if self._voice_lease_synchronized and self._voice_lease_owner != "none":
            return None
        return (
            self._voice_lease_connection_id,
            self._voice_lease_generation,
            self._voice_lease_synchronized,
            self._voice_lease_owner,
            self._microphone_route_generation,
        )

    async def _maybe_signal_blocked_text_mode_microphone(self) -> None:
        """Tell a client that is still recording into a text-mode session.

        The microphone lease belongs to the frontend and no session-lifecycle
        path resets it, while a text-mode session pins the route to
        ``blocked`` (``_start_independent_asr_if_enabled`` returns early for a
        non-audio ``input_mode``). A client that keeps uploading therefore has
        every frame accepted at ingress and dropped here. The current frontend
        stops the microphone itself on ``session_started(input_mode='text')``;
        this covers older and third-party clients, which would otherwise get
        no signal at all. One status per text-mode episode: the flag is
        cleared whenever the route leaves ``blocked``.
        """

        async with self._asr_notification_lock:
            signal_state = self._blocked_text_mode_microphone_episode()
            if signal_state is None:
                self._blocked_text_mode_microphone_delivery_state = None
                return
            if signal_state == self._blocked_text_mode_microphone_signal_state:
                return
            progress = self._blocked_text_mode_microphone_delivery_state
            if progress is None or progress[0] != signal_state:
                progress = (signal_state, set())
                self._blocked_text_mode_microphone_delivery_state = progress
            await self._send_voice_control_status(
                message=json.dumps(
                    {
                        "code": "VOICE_INPUT_BLOCKED_TEXT_SESSION",
                        "details": {"reason": "text_session_active"},
                    }
                ),
                progress=progress,
                still_current=(
                    lambda: self._blocked_text_mode_microphone_episode()
                    == signal_state
                ),
            )
            if self._blocked_text_mode_microphone_episode() != signal_state:
                return
            if {
                "display_delivered",
                "voice_owner_settled",
            }.issubset(progress[1]):
                self._blocked_text_mode_microphone_signal_state = signal_state
                self._blocked_text_mode_microphone_delivery_state = None

    def _blocked_text_mode_microphone_episode(
        self,
    ) -> tuple[int, str, int, int, object] | None:
        if (
            str(getattr(self, "input_mode", "audio") or "audio") != "text"
            or self._asr_route_mode != "blocked"
        ):
            return None
        return (
            self._asr_route_operation_generation,
            self._voice_lease_connection_id,
            self._voice_lease_generation,
            self._microphone_route_generation,
            getattr(self, "session", None),
        )

    async def _enqueue_audio_stream_data(self, message: dict) -> None:
        self._ensure_asr_runtime_state()
        if (
            self._voice_input_pipeline_failed
            # The same latch the ingress worker checks, one step earlier.
            # Nothing below this line closes during the failure notice on a
            # NATIVE route: that route never reaches _abort_independent_asr,
            # so the voice PCM sync is never invalidated, and
            # _voice_input_accepts_pcm is lease-only -- it reads neither the
            # route mode nor this latch. So while a backpressured status send
            # holds the failure open, the client's PCM kept filling the
            # bounded queue, and overflowing it takes the QueueFull path,
            # which aborts the run all over again.
            or getattr(self, "_voice_input_pipeline_failure_token", None)
            is not None
        ):
            return
        if not self._voice_input_accepts_pcm():
            await self._maybe_signal_voice_lease_resync()
            return
        token = self._capture_ingress_token()
        ingress_sequence = self._reserve_hot_swap_ingress_sequence()
        sequence_owned = True
        try:
            frame = _QueuedMicFrame.from_message(
                message,
                token=token,
                audio_stream_epoch=self._audio_stream_epoch,
                ingress_sequence=ingress_sequence,
            )
        except ValueError:
            self._complete_hot_swap_ingress_sequence(ingress_sequence)
            logger.warning("[%s] invalid microphone ingress frame", self.lanlan_name)
            return
        self._ensure_audio_stream_worker()
        try:
            self._audio_stream_queue.put_nowait(
                frame,
                handoff_reserve=(
                    self._asr_route_mode == "independent"
                    and self._asr_runtime.has_pending_turn_handoff(frame.token)
                ),
            )
            sequence_owned = False
        except asyncio.QueueFull:
            try:
                await asyncio.sleep(0)
                if not self._ingress_token_matches(frame.token):
                    rebound = self._rebind_hot_swap_ingress_token(
                        frame.token,
                        audio_stream_epoch=frame.audio_stream_epoch,
                    )
                    if rebound is None:
                        return
                    frame = replace(frame, token=rebound)
                try:
                    self._audio_stream_queue.put_nowait(
                        frame,
                        handoff_reserve=(
                            self._asr_route_mode == "independent"
                            and self._asr_runtime.has_pending_turn_handoff(frame.token)
                        ),
                    )
                    sequence_owned = False
                except asyncio.QueueFull:
                    self._clear_audio_stream_queue("ingress_backpressure")
                    self._audio_stream_dropped_total += 1
                    # Keep the slow provider teardown off the websocket
                    # receive path: run the abort as a tracked task and yield
                    # once so its synchronous prefix (the generation bumps and
                    # lifecycle invalidation ``IndependentAsrRuntime.abort``
                    # performs before its first await) still executes before
                    # this coroutine resumes and any later frame is accepted.
                    # The abort task is created HERE, not inside the
                    # wrapper: one extra scheduling layer would let this
                    # coroutine resume before the abort's synchronous prefix
                    # runs, breaking the ordering the comment above promises.
                    abort = self._fire_task(
                        self._abort_independent_asr("ingress_backpressure")
                    )
                    self._fire_task(
                        self._invalidate_interrupted_voice_turn(
                            "ingress_backpressure",
                            abort=abort,
                        )
                    )
                    await asyncio.sleep(0)
                    return
            finally:
                if sequence_owned:
                    self._complete_hot_swap_ingress_sequence(ingress_sequence)
        now = time.time()
        queued_duration_us = self._audio_stream_queue.duration_us
        if (
            queued_duration_us >= 1_500_000
            and now - self._last_audio_stream_backlog_log_time >= 2.0
        ):
            self._last_audio_stream_backlog_log_time = now
            logger.warning(
                "[%s] audio stream queue backlog qsize=%d duration_ms=%d "
                "max_duration_ms=%d total_dropped=%d",
                self.lanlan_name,
                self._audio_stream_queue.qsize(),
                queued_duration_us // 1_000,
                self._audio_stream_queue.capacity_us // 1_000,
                self._audio_stream_dropped_total,
            )

    async def _audio_stream_worker_loop(self) -> None:
        while True:
            frame = await self._audio_stream_queue.get()
            try:
                token = frame.token
                if not self._ingress_token_matches(token):
                    rebound = self._rebind_hot_swap_ingress_token(
                        token,
                        audio_stream_epoch=frame.audio_stream_epoch,
                    )
                    if rebound is None:
                        self._audio_stream_dropped_total += 1
                        continue
                    token = rebound
                await self._process_microphone_stream_data(
                    frame.message,
                    ingress_token=token,
                    audio_stream_epoch=frame.audio_stream_epoch,
                    ingress_sequence=frame.ingress_sequence,
                    captured_at=frame.captured_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "[%s] audio stream worker error: %s",
                    self.lanlan_name,
                    error,
                )
            finally:
                self._audio_stream_queue.task_done()
                self._complete_hot_swap_ingress_sequence(frame.ingress_sequence)

    def _reserve_hot_swap_ingress_sequence(self) -> int:
        self._hot_swap_ingress_sequence += 1
        sequence = self._hot_swap_ingress_sequence
        self._hot_swap_pending_sequences.add(sequence)
        self._hot_swap_sequence_progress.clear()
        return sequence

    def _complete_hot_swap_ingress_sequence(self, sequence: int) -> None:
        if sequence <= 0:
            return
        self._hot_swap_pending_sequences.discard(sequence)
        self._hot_swap_sequence_progress.set()

    def _hot_swap_cutoff_complete(self, cutoff: int) -> bool:
        return not any(
            sequence <= cutoff for sequence in self._hot_swap_pending_sequences
        )

    def _rebind_hot_swap_ingress_token(
        self,
        token: VoiceIngressToken,
        *,
        audio_stream_epoch: int,
    ) -> VoiceIngressToken | None:
        if not (self.is_hot_swap_imminent or self.is_flushing_hot_swap_cache):
            return None
        current = self._capture_ingress_token()
        if (
            token.session_epoch != current.session_epoch
            or token.audio_generation != current.audio_generation
            or audio_stream_epoch != self._audio_stream_epoch
            or token.connection_id != current.connection_id
            or token.lease_generation != current.lease_generation
            or token.route_generation == current.route_generation
            or self._voice_lease_owner != "core"
            or not self._voice_input_accepts_pcm()
        ):
            return None
        return current

    async def _fail_voice_input_pipeline(
        self,
        *,
        ingress_token: VoiceIngressToken,
        session_ref: object,
        audio_epoch: int,
        pipeline_ref: VoiceInputAudioPipeline,
    ) -> None:
        """Commit the failure under the lock, notify and revoke outside it.

        The lock is shared with session start, independent-ASR close and the
        noise-reduction toggle, and the notify/revoke phase below ends in
        ``_fail_closed_voice_route`` -- which writes to the frontend socket.
        A backpressured client would otherwise hold every one of those
        recovery operations behind an unrelated write.

        What the lock still has to cover is the COMMIT: validating that this
        failure is the live state and stamping it, so two failures cannot both
        decide they own the route. Everything after that is fenced by the
        stamp instead, which is why it can leave the lock -- see
        ``_voice_input_pipeline_failure_token``.
        """

        async with self._voice_input_pipeline_transition_lock:
            committed = self._commit_voice_input_pipeline_failure(
                ingress_token=ingress_token,
                session_ref=session_ref,
                audio_epoch=audio_epoch,
                pipeline_ref=pipeline_ref,
            )
        if committed is None:
            return
        await self._finish_voice_input_pipeline_failure(committed)

    async def _fail_voice_input_pipeline_locked(
        self,
        *,
        ingress_token: VoiceIngressToken,
        session_ref: object,
        audio_epoch: int,
        pipeline_ref: VoiceInputAudioPipeline,
    ) -> None:
        """Caller already holds the transition lock; commit and finish here.

        Kept as the locked-caller entry point. The finish phase runs with the
        lock still held, so a caller that needs the whole failure serialized
        against its own pipeline work still gets that.
        """

        committed = self._commit_voice_input_pipeline_failure(
            ingress_token=ingress_token,
            session_ref=session_ref,
            audio_epoch=audio_epoch,
            pipeline_ref=pipeline_ref,
        )
        if committed is None:
            return
        await self._finish_voice_input_pipeline_failure(committed)

    def _commit_voice_input_pipeline_failure(
        self,
        *,
        ingress_token: VoiceIngressToken,
        session_ref: object,
        audio_epoch: int,
        pipeline_ref: VoiceInputAudioPipeline,
    ) -> _VoiceInputPipelineFailure | None:
        """Validate and stamp one pipeline failure. Synchronous on purpose.

        No await between the validation above and the writes below, so this
        whole step is atomic against the event loop and the lock only has to
        span the call itself.
        """

        if (
            self._voice_input_pipeline_failed
            or ingress_token != self._capture_ingress_token()
            or self.session is not session_ref
            or self._audio_stream_epoch != audio_epoch
            or self._voice_input_audio_pipeline is not pipeline_ref
            or not self.is_active
        ):
            return None
        source_session_epoch = ingress_token.session_epoch
        source_connection_id = ingress_token.connection_id
        source_lease_generation = ingress_token.lease_generation
        voice_transition_generation = self._voice_input_transition_generation
        route_operation_generation = self._asr_route_operation_generation
        source_session_ref = session_ref
        source_audio_epoch = audio_epoch
        source_route_mode = self._asr_route_mode
        self._voice_input_pipeline_failed = True
        # The commit's own identity. `_voice_input_pipeline_failed` says
        # "frames are being dropped right now" and ANY pipeline replacement
        # clears it -- including a noise-reduction toggle, which does not end
        # the blocked route or revoke anything. Testing that flag in the
        # predicate below is what made the notify/revoke phase unable to leave
        # the lock: a toggle landing in the middle cleared it, the predicate
        # went False, the revoke was skipped, and the route stayed blocked
        # with the lease never revoked (commit 94c26715). The token is cleared
        # by nothing except a NEWER commit, so the predicate can ask about
        # this failure instead of about the world.
        failure_token = object()
        self._voice_input_pipeline_failure_token = failure_token
        independent_route = source_route_mode == "independent"
        source_provider = (
            self._independent_asr_provider
            or self._independent_asr_route_key
            or "unknown"
        )
        self._set_microphone_route("blocked")
        self._clear_audio_stream_queue("audio_preprocessing_failed")
        self.hot_swap_audio_cache.clear()
        return _VoiceInputPipelineFailure(
            failure_token=failure_token,
            independent_route=independent_route,
            provider=source_provider,
            session_epoch=source_session_epoch,
            connection_id=source_connection_id,
            lease_generation=source_lease_generation,
            transition_generation=voice_transition_generation,
            route_operation_generation=route_operation_generation,
            session_ref=source_session_ref,
            audio_epoch=source_audio_epoch,
        )

    async def _finish_voice_input_pipeline_failure(
        self,
        failure: "_VoiceInputPipelineFailure",
    ) -> None:
        """Abort the ASR run, tell the client, revoke the lease.

        Runs outside the pipeline transition lock. Everything here is fenced
        on ``failure.failure_token`` instead, so a concurrent session restart
        or noise-reduction toggle is free to take the lock while a
        backpressured frontend socket is still absorbing the status send.
        """

        if failure.independent_route:
            await self._abort_independent_asr(
                "audio_preprocessing_failed",
                # This phase runs without the pipeline transition lock, so a
                # restart can install a newer route while the abort is in
                # flight; its audio is not this failure's to clear.
                still_current=lambda: self._asr_route_operation_matches(
                    failure.route_operation_generation
                ),
            )

        def preprocessing_failure_is_current() -> bool:
            # The route-operation generation is deliberately NOT repeated here:
            # _fail_closed_voice_route fences on it already, with the identical
            # comparison, and re-checks it after the status send too.
            #
            # The pipeline REF is not repeated either, for the same reason the
            # failure flag is not: a noise-reduction toggle replaces the
            # pipeline without ending the blocked route, and treating that as
            # "someone else owns this now" is precisely how the revoke went
            # missing. A start or a close does replace the pipeline and DOES
            # invalidate this failure -- both advance the route operation
            # generation, which _fail_closed_voice_route already checks.
            return not (
                self._voice_input_pipeline_failure_token
                is not failure.failure_token
                or self._voice_lease_connection_id != failure.connection_id
                or self._voice_lease_generation != failure.lease_generation
                or (
                    self._voice_input_transition_generation
                    != failure.transition_generation
                )
                or self._capture_ingress_token().session_epoch
                != failure.session_epoch
                or self.session is not failure.session_ref
                or self._audio_stream_epoch != failure.audio_epoch
                or not self.is_active
                or self._asr_route_mode != "blocked"
                or (
                    self._independent_asr_provider
                    or self._independent_asr_route_key
                    or "unknown"
                )
                != failure.provider
            )

        # Ingress backstop. _voice_input_pipeline_failed already drops frames,
        # but only after _enqueue_audio_stream_data has parsed and queued them:
        # _voice_input_accepts_pcm is lease-only and never consults the route.
        # The revoke lands strictly AFTER the status send -- revoking first
        # would clear the voice socket and leave the notice with no target.
        await self._fail_closed_voice_route(
            "audio_preprocessing_failed",
            operation_generation=failure.route_operation_generation,
            still_current=preprocessing_failure_is_current,
            status=AsrStatusEvent(
                code="ASR_AUDIO_PREPROCESSING_FAILED",
                provider=failure.provider,
                session_epoch=failure.session_epoch,
            ),
        )

    async def _process_microphone_stream_data(
        self,
        message: dict,
        *,
        ingress_token: VoiceIngressToken,
        audio_stream_epoch: int | None = None,
        ingress_sequence: int | None = None,
        captured_at: float | None = None,
    ) -> None:
        sequence_owned = ingress_sequence is None
        if ingress_sequence is None:
            ingress_sequence = self._reserve_hot_swap_ingress_sequence()
        if audio_stream_epoch is None:
            audio_stream_epoch = self._audio_stream_epoch
        if (
            self._voice_input_pipeline_failed
            # The flag alone stopped being the whole answer once the notify
            # phase left the transition lock. ANY pipeline replacement clears
            # it -- a noise-reduction toggle included -- and a toggle can now
            # land while a backpressured status send is still in flight, i.e.
            # while this failure still owns a blocked route whose lease has
            # not been revoked yet. Frames would then be parsed, queued and
            # run through the replacement DSP until _route_microphone_audio
            # discards them, refilling the bounded queue and tripping ingress
            # backpressure during what must stay a fail-closed interval.
            # The token is cleared only when the route is live again.
            or getattr(self, "_voice_input_pipeline_failure_token", None)
            is not None
        ):
            if sequence_owned:
                self._complete_hot_swap_ingress_sequence(ingress_sequence)
            return
        if not self._ingress_token_matches(ingress_token):
            rebound = self._rebind_hot_swap_ingress_token(
                ingress_token,
                audio_stream_epoch=audio_stream_epoch,
            )
            if rebound is None:
                if sequence_owned:
                    self._complete_hot_swap_ingress_sequence(ingress_sequence)
                return
            ingress_token = rebound
        data = message.get("data")
        session_ref = self.session
        audio_epoch = audio_stream_epoch
        pipeline_ref = self._voice_input_audio_pipeline
        voice_owner = self._voice_lease_owner
        try:
            if not isinstance(data, list):
                logger.error("Microphone input rejected: expected a PCM sample list")
                return
            audio_bytes = struct.pack(f"<{len(data)}h", *data)
            declared_rate_hz = message.get("sample_rate_hz")
            if declared_rate_hz is None:
                source_rate_hz = 48_000 if len(data) == 480 else 16_000
            elif declared_rate_hz in {16_000, 48_000}:
                source_rate_hz = int(declared_rate_hz)
            else:
                logger.error(
                    "Microphone input rejected: unsupported sample rate %r",
                    declared_rate_hz,
                )
                return
            audio_captured_at = (
                float(captured_at)
                if isinstance(captured_at, (int, float)) and captured_at > 0
                else time.time()
            )
            try:
                processed_frame = await pipeline_ref.process(
                    audio_bytes,
                    sample_rate_hz=source_rate_hz,
                    ingress_sequence=ingress_sequence,
                    captured_at=audio_captured_at,
                )
                # Capture identity belongs to Core ingress. Even a legacy or
                # injected pipeline implementation must not be able to replace
                # it with a Runtime-local surrogate.
                processed_frame = replace(
                    processed_frame,
                    ingress_sequence=ingress_sequence,
                    captured_at=audio_captured_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._fail_voice_input_pipeline(
                    ingress_token=ingress_token,
                    session_ref=session_ref,
                    audio_epoch=audio_epoch,
                    pipeline_ref=pipeline_ref,
                )
                return
            if not processed_frame.pcm16:
                return
            if (
                not self.is_active
                or self._audio_stream_epoch != audio_epoch
                or self._voice_lease_owner != voice_owner
                or not self._voice_input_accepts_pcm()
            ):
                return
            if not self._ingress_token_matches(ingress_token):
                rebound = self._rebind_hot_swap_ingress_token(
                    ingress_token,
                    audio_stream_epoch=audio_epoch,
                )
                if rebound is None:
                    return
                ingress_token = rebound
            refs_changed = (
                self.session is not session_ref
                or self._voice_input_audio_pipeline is not pipeline_ref
            )
            cache_for_hot_swap = False
            async with self.hot_swap_cache_lock:
                hot_swap_barrier = (
                    self.is_hot_swap_imminent or self.is_flushing_hot_swap_cache
                )
                if refs_changed and not hot_swap_barrier:
                    return
                if hot_swap_barrier:
                    cache_for_hot_swap = True
                    accepted = self.hot_swap_audio_cache.append(
                        _HotSwapAudioFrame(
                            pcm16=processed_frame.pcm16,
                            token=ingress_token,
                            speech_probability=processed_frame.speech_probability,
                            rnnoise_available=processed_frame.rnnoise_available,
                            rnnoise_evidence=processed_frame.rnnoise_evidence,
                            audio_stream_epoch=audio_epoch,
                            ingress_sequence=processed_frame.ingress_sequence,
                            captured_at=processed_frame.captured_at,
                        )
                    )
            if cache_for_hot_swap:
                if not accepted:
                    # Weakest of the three sites: the hot-swap barrier is up, so
                    # self.session may still be the pre-swap session being torn
                    # down (clearing that buffer is a no-op) and the hole's real
                    # damage lands in the post-swap session, which the flush
                    # path below covers. Load-bearing only when the hot swap is
                    # aborted and the same session resumes.
                    await self._invalidate_interrupted_voice_turn(
                        "ingress_backpressure"
                    )
                return
            if not self._ingress_token_matches(ingress_token):
                return
            await self._route_microphone_audio(
                processed_frame.pcm16,
                sample_rate_hz=processed_frame.sample_rate_hz,
                speech_probability=processed_frame.speech_probability,
                rnnoise_available=processed_frame.rnnoise_available,
                rnnoise_evidence=processed_frame.rnnoise_evidence,
                ingress_token=ingress_token,
                ingress_sequence=processed_frame.ingress_sequence,
                captured_at=processed_frame.captured_at,
            )
        except struct.error:
            logger.error("Microphone input rejected: invalid PCM samples")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Microphone preprocessing or ASR routing failed")
        finally:
            if sequence_owned:
                self._complete_hot_swap_ingress_sequence(ingress_sequence)

    async def _route_microphone_audio(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        speech_probability: float | None = None,
        rnnoise_available: bool | None = None,
        rnnoise_evidence: RnnoiseEvidence | None = None,
        ingress_token: VoiceIngressToken | None = None,
        ingress_sequence: int = 0,
        captured_at: float | None = None,
    ) -> bool:
        route_mode = self._asr_route_mode
        if not self._voice_input_accepts_pcm():
            return True
        if route_mode == "native":
            if getattr(self, "session_closed_by_server", False):
                return True
            token = ingress_token or self._capture_native_ingress_token()
            session_ref = self.session

            def native_send_is_current() -> bool:
                return bool(
                    self.session is session_ref
                    and self._asr_route_mode == "native"
                    and token == self._capture_native_ingress_token()
                    and self._voice_lease_owner == "core"
                    and self._voice_input_accepts_pcm()
                )

            if not native_send_is_current():
                return True
            stream_audio = getattr(session_ref, "stream_audio", None)
            if not callable(stream_audio):
                return True
            if getattr(session_ref, "_fatal_error_occurred", False):
                # After an Omni fatal error (1011 / response timeout) the
                # session is doomed; stop feeding it microphone PCM, with
                # rate-limited logging (parity with the legacy streaming.py
                # audio-branch guard).
                now = time.monotonic()
                if now - getattr(
                    self, "last_audio_send_error_time", 0.0
                ) > getattr(self, "audio_error_log_interval", 2.0):
                    logger.warning(
                        "[%s] Omni session fatal error, skipping microphone audio",
                        self.lanlan_name,
                    )
                    self.last_audio_send_error_time = now
                return True
            try:
                if isinstance(session_ref, _core_facade.OmniRealtimeClient):
                    await stream_audio(pcm16, captured_at=captured_at)
                else:
                    await stream_audio(pcm16)
                if not native_send_is_current():
                    return True
                self._record_omni_microphone_audio(len(pcm16))
            except asyncio.CancelledError:
                raise
            except web_exceptions.ConnectionClosedOK:
                if not native_send_is_current():
                    return True
                self.session_closed_by_server = True
            except (web_exceptions.ConnectionClosed, AttributeError) as exc:
                if not native_send_is_current():
                    return True
                self.session_closed_by_server = True
                now = time.monotonic()
                if now - getattr(self, "last_audio_send_error_time", 0.0) > getattr(
                    self, "audio_error_log_interval", 2.0
                ):
                    logger.warning(
                        "[%s] Omni native microphone connection closed: %s",
                        self.lanlan_name,
                        exc,
                    )
                    self.last_audio_send_error_time = now
            except Exception as exc:
                if not native_send_is_current():
                    return True
                message = str(exc).lower()
                if "no close frame" in message or "connection closed" in message:
                    self.session_closed_by_server = True
                now = time.monotonic()
                if now - getattr(self, "last_audio_send_error_time", 0.0) > getattr(
                    self, "audio_error_log_interval", 2.0
                ):
                    logger.error(
                        "[%s] Omni native microphone routing failed: %s",
                        self.lanlan_name,
                        exc,
                    )
                    self.last_audio_send_error_time = now
            return True
        if route_mode != "independent":
            self._set_microphone_route("blocked")
            await self._maybe_signal_blocked_text_mode_microphone()
            return True
        token = ingress_token or self._capture_ingress_token()
        if not self._ingress_token_matches(token):
            return True
        route_mode = self._asr_route_mode
        voice_transition_generation = self._voice_input_transition_generation
        route_operation_generation = self._asr_route_operation_generation
        provider = self._independent_asr_provider
        owner = self._voice_lease_owner
        result = await self._asr_runtime.submit(
            ProcessedVoiceFrame(
                pcm16=pcm16,
                sample_rate_hz=sample_rate_hz,
                speech_probability=speech_probability,
                rnnoise_available=bool(rnnoise_available),
                rnnoise_evidence=rnnoise_evidence,
                ingress_sequence=ingress_sequence,
                captured_at=(
                    float(captured_at)
                    if isinstance(captured_at, (int, float)) and captured_at > 0
                    else 0.0
                ),
            ),
            ingress_token=token,
        )
        submit_is_current = bool(
            token == self._capture_ingress_token()
            and route_mode == "independent"
            and self._asr_route_mode == "independent"
            and (route_operation_generation == self._asr_route_operation_generation)
            and (voice_transition_generation == self._voice_input_transition_generation)
            and owner == self._voice_lease_owner
            and self._voice_input_accepts_pcm()
            and self._independent_asr_provider == provider
        )
        if not submit_is_current:
            return True
        if result.status is AsrSubmitStatus.UNAVAILABLE:
            self._set_microphone_route("blocked")
            self._clear_audio_stream_queue("independent_asr_unavailable")
            self.hot_swap_audio_cache.clear()
        return True

    def _record_omni_microphone_audio(self, byte_count: int) -> None:
        byte_count = int(byte_count)
        if byte_count <= 0:
            return
        if self._asr_route_mode != "native":
            raise RuntimeError("OMNI_MICROPHONE_ROUTE_FORBIDDEN")
        self._omni_mic_audio_bytes += byte_count

    async def _flush_hot_swap_audio_cache(self) -> None:
        damaged_frames: list[_HotSwapAudioFrame] = []
        # Cached pre-swap frames carry a stale route generation, so replay
        # REBINDS them onto the new session -- but only the local send token is
        # rebound; the frame objects in ``damaged_frames`` keep their original
        # token. The damage check at the end therefore missed them, skipped
        # _invalidate_interrupted_voice_turn, and left a prefix that had already
        # reached the new provider in place: later speech got concatenated
        # across the missing tail instead of the damaged turn being cleared
        # (Codex P2). Track current-route damage explicitly rather than trying
        # to reconstruct it from tokens that were never updated.
        rebound_to_current_route = False
        flush_complete = False
        async with self.hot_swap_cache_lock:
            self.is_flushing_hot_swap_cache = True
            cutoff = self._hot_swap_ingress_sequence
        try:
            while True:
                if self._hot_swap_cutoff_complete(cutoff):
                    break
                self._hot_swap_sequence_progress.clear()
                if self._hot_swap_cutoff_complete(cutoff):
                    continue
                await self._hot_swap_sequence_progress.wait()
            if not self.session or not self.is_active:
                async with self.hot_swap_cache_lock:
                    damaged_frames.extend(self.hot_swap_audio_cache.drain())
                return
            # Native replay throttle: coalesce up to five 10 ms frames per
            # send and sleep 25 ms between sends (~2x real time), matching
            # the pre-independent-route flush pacing (legacy streaming.py:
            # 320-byte chunks x5 at 0.025 s). The independent route keeps
            # per-frame submits so detector metadata stays frame-accurate.
            native_batch_frames = 5
            send_interval_s = 0.025

            async def replay_frames(
                audio_frames: tuple[_HotSwapAudioFrame, ...],
                *,
                paced: bool,
            ) -> bool:
                """Replay drained frames; ``False`` means a send failed."""
                nonlocal rebound_to_current_route
                index = 0
                while index < len(audio_frames):
                    frame = audio_frames[index]
                    token = frame.token
                    if not self._ingress_token_matches(token):
                        rebound = self._rebind_hot_swap_ingress_token(
                            token,
                            audio_stream_epoch=frame.audio_stream_epoch,
                        )
                        if rebound is None:
                            self._audio_stream_dropped_total += 1
                            now = time.time()
                            if (
                                now - self._last_hot_swap_rebind_drop_log_time
                                >= 2.0
                            ):
                                self._last_hot_swap_rebind_drop_log_time = now
                                logger.warning(
                                    "[%s] hot swap replay dropped stale "
                                    "frame total_dropped=%d",
                                    self.lanlan_name,
                                    self._audio_stream_dropped_total,
                                )
                            index += 1
                            continue
                        token = rebound
                        # From here on this frame's audio is being sent on the
                        # LIVE route, so any later damage in this flush belongs
                        # to the current turn even though the recorded frames
                        # still carry their pre-swap tokens.
                        rebound_to_current_route = True
                    batch_end = index + 1
                    if self._asr_route_mode == "native":
                        while (
                            batch_end < len(audio_frames)
                            and batch_end - index < native_batch_frames
                            and audio_frames[batch_end].token == frame.token
                            and audio_frames[batch_end].audio_stream_epoch
                            == frame.audio_stream_epoch
                        ):
                            batch_end += 1
                    try:
                        await self._route_microphone_audio(
                            b"".join(
                                item.pcm16
                                for item in audio_frames[index:batch_end]
                            ),
                            sample_rate_hz=16_000,
                            speech_probability=frame.speech_probability,
                            rnnoise_available=frame.rnnoise_available,
                            rnnoise_evidence=frame.rnnoise_evidence,
                            ingress_token=token,
                            ingress_sequence=audio_frames[
                                batch_end - 1
                            ].ingress_sequence,
                            captured_at=audio_frames[batch_end - 1].captured_at,
                        )
                    except asyncio.CancelledError:
                        damaged_frames.extend(audio_frames[index:])
                        raise
                    except Exception:
                        damaged_frames.extend(audio_frames[index:])
                        return False
                    index = batch_end
                    if paced and self._asr_route_mode == "native":
                        try:
                            await asyncio.sleep(send_interval_s)
                        except asyncio.CancelledError:
                            damaged_frames.extend(audio_frames[index:])
                            raise
                return True

            # Termination contract: live frames keep landing in the cache
            # while the flush runs, so a paced native pass can never drain
            # to empty on its own -- at ~2x real time each pass roughly
            # halves the backlog until per-batch pacing overhead dominates
            # (one <=5-frame batch costs 25 ms while ~2.5 frames arrive)
            # and the drain settles at a few-frame steady state. That
            # healthy tail is replayed unpaced while holding the cache
            # lock, so the flush barrier drops atomically and the next
            # live frame routes directly instead of being damaged. The
            # wall-clock deadline (2x the initial-backlog replay estimate
            # plus fixed slack) therefore only trips when replay cannot
            # outpace ingress -- genuine backpressure -- and the residue
            # then invalidates the candidate turn below.
            tail_handoff_frames = 25  # <=250 ms residue: burst and hand off
            flush_deadline = (
                time.monotonic()
                + 2.0 * self.hot_swap_audio_cache.duration_ms / 1_000.0
                + 3.0
            )
            while True:
                async with self.hot_swap_cache_lock:
                    audio_frames = self.hot_swap_audio_cache.drain()
                    if len(audio_frames) <= tail_handoff_frames:
                        if audio_frames and not await replay_frames(
                            audio_frames,
                            paced=False,
                        ):
                            return
                        self.is_flushing_hot_swap_cache = False
                        self.is_hot_swap_imminent = False
                        flush_complete = True
                        return
                if time.monotonic() >= flush_deadline:
                    damaged_frames.extend(audio_frames)
                    return
                if not await replay_frames(audio_frames, paced=True):
                    return
        finally:
            async with self.hot_swap_cache_lock:
                if not flush_complete:
                    damaged_frames.extend(self.hot_swap_audio_cache.drain())
                    self.is_flushing_hot_swap_cache = False
                    self.is_hot_swap_imminent = False
            # One abort invalidates the whole candidate turn, however many
            # damaged tokens remain current. rebound_to_current_route covers the
            # frames whose send token was rebound onto the live session while
            # the recorded frame kept its pre-swap token -- without it a replay
            # that failed AFTER delivering a rebound prefix looked like damage
            # to a turn nobody was listening to.
            if damaged_frames and (
                rebound_to_current_route
                or any(
                    self._ingress_token_matches(frame.token)
                    for frame in damaged_frames
                )
            ):
                await self._invalidate_interrupted_voice_turn(
                    "ingress_backpressure"
                )

    def _invalidate_voice_pcm_sync(self, reason: str) -> None:
        self._voice_input_registry.invalidate_utterance(reason=reason)
        self._clear_audio_stream_queue(reason)
        self.hot_swap_audio_cache.clear()

    async def _apply_voice_lease_state(
        self,
        *,
        owner: str,
        hard_muted: bool,
        focus_suppressed: bool,
        reason: str,
        force_abort: bool,
    ) -> None:
        self._ensure_asr_runtime_state()
        self._voice_input_transition_generation += 1
        previous = (
            self._voice_lease_owner,
            self._voice_lease_hard_muted,
            self._voice_lease_focus_suppressed,
        )
        self._voice_lease_owner = owner
        self._voice_lease_hard_muted = hard_muted
        self._voice_lease_focus_suppressed = focus_suppressed
        previous_owner = previous[0]
        if owner != previous_owner:
            if owner == "game":
                self._voice_input_registry.activate(
                    self._game_voice_input_registration.handle,
                )
            elif owner == "core":
                self._voice_input_registry.activate(
                    self._core_chat_voice_input_registration.handle,
                )
        reasons: set[str] = set()
        if owner == "none":
            reasons.add("owner_none")
        elif (
            owner == "game"
            and not self._voice_input_registry.active_accepts_input
        ):
            reasons.add("game")
        if hard_muted:
            reasons.add("hard_mute")
        if focus_suppressed:
            reasons.add("focus")
        self._voice_input_suppression_reasons = reasons
        self._voice_input_suppressed = bool(reasons)
        self._invalidate_voice_pcm_sync(reason)
        current = (owner, hard_muted, focus_suppressed)
        should_abort = (
            force_abort or self._voice_lease_requires_abort or previous != current
        )
        self._voice_lease_requires_abort = False
        if owner == "game" and not self._voice_input_registry.active_accepts_input:
            await self._asr_runtime.suspend(reason)
            await self._voice_input_registry.wait_idle()
        elif reason == "game_release":
            if should_abort:
                route_operation_snapshot = self._asr_route_operation_generation
                await self._asr_runtime.abort(reason)
                await self._voice_input_registry.wait_idle()
                if (
                    self._asr_route_operation_generation != route_operation_snapshot
                    or self._voice_lease_owner != "core"
                ):
                    return
            if self._voice_lease_owner != "core":
                return
            # Resume the lifecycle even while hard-muted or focus-suppressed:
            # those states gate PCM at ingress, and no later unmute path calls
            # resume, so skipping here would leave the runtime SUSPENDED for
            # the rest of the session.
            await self._asr_runtime.resume(reason)
        elif should_abort:
            await self._asr_runtime.abort(reason)
            await self._voice_input_registry.wait_idle()

    async def _suspend_independent_voice_input_for_game(self) -> None:
        await self._apply_voice_lease_state(
            owner="game",
            hard_muted=self._voice_lease_hard_muted,
            focus_suppressed=self._voice_lease_focus_suppressed,
            reason="game_takeover",
            force_abort=True,
        )

    async def _resume_independent_voice_input_after_game(self) -> None:
        await self._apply_voice_lease_state(
            owner="core",
            hard_muted=self._voice_lease_hard_muted,
            focus_suppressed=self._voice_lease_focus_suppressed,
            reason="game_release",
            force_abort=False,
        )

    def _set_voice_input_websocket(self, connection_id: str, websocket) -> bool:
        """Remember which socket holds the voice lease, for mic control-plane pushes.

        ``self.websocket`` is the NEWEST socket (it is reassigned at accept
        time), which is not necessarily the one recording: this PR deliberately
        supports a recorder that has been superseded by a newer chat window.
        Microphone teardown notices must follow the lease, not the display
        plane, or the window holding the hardware never hears about them.
        Only the current lease holder may install a socket, so a stale claim
        cannot redirect the control plane.
        """

        normalized = str(connection_id or "").strip()
        if not normalized or normalized != self._voice_lease_connection_id:
            return False
        self._voice_input_websocket = websocket
        return True

    def _clear_voice_input_websocket(self) -> None:
        self._voice_input_websocket = None

    def _begin_voice_input_connection(self, connection_id: str) -> bool:
        normalized = str(connection_id or "").strip()
        if not normalized or normalized == self._voice_lease_connection_id:
            return False
        invalidate_start = getattr(self._asr_runtime, "_invalidate_asr_start", None)
        if callable(invalidate_start):
            invalidate_start()
        # A new claim must not inherit the previous holder's socket, and a
        # fresh connection re-arms the resync signal.
        self._clear_voice_input_websocket()
        self._voice_lease_resync_suppressed = False
        self._voice_lease_connection_id = normalized
        self._voice_lease_generation = -1
        self._voice_lease_synchronized = False
        self._voice_lease_control_seen = False
        self._voice_lease_owner = "none"
        self._voice_lease_hard_muted = False
        self._voice_lease_focus_suppressed = False
        self._voice_input_suppression_reasons = {"owner_none"}
        self._voice_input_suppressed = True
        self._voice_lease_requires_abort = True
        self._invalidate_voice_pcm_sync("websocket_reconnect")
        return True

    async def _fail_closed_voice_route(
        self,
        reason: str,
        *,
        operation_generation: int,
        still_current: Callable[[], bool] | None = None,
        status: AsrStatusEvent | None = None,
        voice_owner_notice: dict | None = None,
    ) -> bool:
        """The single exit by which an enabled ASR route ends fail-closed.

        Five call sites reach this state (text session, unreadable settings,
        ASR start failure, audio-preprocessing failure, runtime failure) and
        each review round found another. What every one of them has to get
        right is not a shared fence -- their staleness conditions genuinely
        differ -- but a shared ORDER:

        1. fence, so a competing NEWER route operation is not clobbered;
        2. notify the LEASE holder while the lease still has a target: the
           revoke clears ``_voice_input_websocket`` AND the lease id, and
           ``_voice_owner_socket()`` returns None on either;
        3. re-fence, because step 2 awaited and a newer start may have
           installed its own blocked placeholder during it;
        4. revoke.

        Step 3 is why a naive "revoke whenever the route ends blocked" was
        rejected: ``_revoke_voice_input_connection`` calls
        ``_invalidate_asr_start()`` first, so revoking on a stale exit cancels
        the competing newer start -- reddening
        ``test_stale_start_abort_does_not_clobber_newer_start_placeholder``.
        Holding the order here rather than at each call site is what makes a
        NEW exit correct by construction instead of by review.

        ``operation_generation`` is a required keyword so a new exit cannot
        quietly omit the one fence every exit needs; ``still_current`` carries
        the caller's own, usually stricter, predicate (route key, provider,
        session ref, ingress epoch...) and is re-evaluated in step 3 too.
        Returns True only when the lease was actually revoked.
        """

        self._ensure_asr_runtime_state()

        def operation_is_current() -> bool:
            if not self._asr_route_operation_matches(operation_generation):
                return False
            return still_current is None or bool(still_current())

        if not operation_is_current():
            return False
        notified = False
        if voice_owner_notice is not None and self._voice_lease_owner != "game":
            send_to_voice_owner = getattr(self, "_send_to_voice_owner", None)
            if callable(send_to_voice_owner):
                await send_to_voice_owner(dict(voice_owner_notice))
                notified = True
        if status is not None:
            await self._send_core_asr_status(status)
            notified = True
        if notified and not operation_is_current():
            return False
        return await self._revoke_lease_for_blocked_route(reason)

    async def _revoke_lease_for_blocked_route(self, reason: str) -> bool:
        """Fail-closed fail-safe for an enabled-but-blocked microphone route.

        Reached ONLY through :meth:`_fail_closed_voice_route`, which owns the
        notify-then-fence-then-revoke order this must not be called without;
        ``scripts/check_core_contracts.py`` enforces that.

        Clients that never receive or never honour the teardown notice (older
        builds, third-party clients, throttled background tabs) keep uploading
        PCM into a route that discards it. Stop accepting it at ingress too.
        The game owner is exempt: its built-in Registry consumer holds the
        active transcript route and must not be collaterally revoked.

        NEVER hoist this above the stale/competing-start exits.
        ``_revoke_voice_input_connection`` calls ``_invalidate_asr_start()``
        first, so revoking on a stale exit cancels a concurrent NEWER start --
        verified: an over-broad "revoke whenever the route ends blocked"
        reddens test_stale_start_abort_does_not_clobber_newer_start_placeholder.

        This is a backstop, not the primary defence. A frontend that later runs
        startMicCapture re-claims the lease from scratch via refreshMicLease,
        and ``_handle_voice_input_control`` enforces only generation
        monotonicity -- the revoke resets the generation to -1, so the next
        client snapshot wins unconditionally. The latch that gates capture
        start on the client is what actually closes the hole.
        """

        del reason
        if self._asr_route_mode != "blocked" or self._voice_lease_owner == "game":
            return False
        self._voice_lease_resync_suppressed = True
        return await self._revoke_voice_input_connection(
            self._voice_lease_connection_id
        )

    async def _revoke_voice_input_connection(self, connection_id: str) -> bool:
        """Release the voice lease held by a websocket that just disconnected.

        Only the current holder may revoke: a socket that already lost the
        identity to a newer claim must never clear the winner's lease. The
        shared session is deliberately left alone -- this aborts the voice
        turn (releasing the realtime dispatch pause and the provider
        transport) and parks the lease at owner ``none`` until the next
        socket engages voice input, exactly like a fresh claim does.
        """

        self._ensure_asr_runtime_state()
        normalized = str(connection_id or "").strip()
        if not normalized or normalized != self._voice_lease_connection_id:
            return False
        invalidate_start = getattr(self._asr_runtime, "_invalidate_asr_start", None)
        if callable(invalidate_start):
            invalidate_start()
        self._clear_voice_input_websocket()
        self._voice_lease_connection_id = ""
        self._voice_lease_generation = -1
        self._voice_lease_synchronized = False
        self._voice_lease_control_seen = False
        await self._apply_voice_lease_state(
            owner="none",
            hard_muted=False,
            focus_suppressed=False,
            reason="voice_connection_closed",
            force_abort=True,
        )
        return True

    async def _ensure_voice_input_session_authorized(
        self,
        connection_id: str,
    ) -> bool:
        """Authorize one legacy ordinary-audio session without weakening MicLease."""

        self._ensure_asr_runtime_state()
        normalized = str(connection_id or "").strip()
        if not normalized or normalized != self._voice_lease_connection_id:
            return False
        if self._voice_lease_synchronized:
            return True
        if self._voice_lease_control_seen:
            return False

        self._voice_lease_generation = 0
        self._voice_lease_synchronized = True
        await self._apply_voice_lease_state(
            owner="core",
            hard_muted=False,
            focus_suppressed=False,
            reason="legacy_session_start",
            force_abort=False,
        )
        return bool(
            self._voice_lease_connection_id == normalized
            and self._voice_lease_generation == 0
            and self._voice_lease_synchronized
            and not self._voice_lease_control_seen
            and self._voice_lease_owner == "core"
            and not self._voice_lease_hard_muted
            and not self._voice_lease_focus_suppressed
        )

    async def _handle_voice_input_control(
        self,
        event: str,
        lease_generation: int,
        *,
        owner: str | None = None,
        hard_muted: bool | None = None,
        focus_suppressed: bool | None = None,
    ) -> bool:
        self._ensure_asr_runtime_state()
        self._voice_lease_control_seen = True
        try:
            generation = int(lease_generation)
        except (TypeError, ValueError):
            return False
        if generation <= self._voice_lease_generation:
            return False
        normalized_event = str(event or "").strip().lower()
        if normalized_event not in {
            "lease_sync",
            "hard_mute",
            "hard_unmute",
            "focus_suppress",
            "focus_resume",
            "game_takeover",
            "game_release",
        }:
            return False
        if normalized_event == "lease_sync":
            normalized_owner = str(owner or "").strip().lower()
            if normalized_owner not in {"none", "core", "game"}:
                return False
            if not isinstance(hard_muted, bool) or not isinstance(
                focus_suppressed,
                bool,
            ):
                return False
            next_owner = normalized_owner
            next_hard_muted = hard_muted
            next_focus_suppressed = focus_suppressed
        else:
            next_owner = self._voice_lease_owner
            next_hard_muted = self._voice_lease_hard_muted
            next_focus_suppressed = self._voice_lease_focus_suppressed
            if normalized_event == "hard_mute":
                next_hard_muted = True
            elif normalized_event == "hard_unmute":
                next_hard_muted = False
            elif normalized_event == "focus_suppress":
                next_focus_suppressed = True
            elif normalized_event == "focus_resume":
                next_focus_suppressed = False
            elif normalized_event == "game_takeover":
                next_owner = "game"
            elif normalized_event == "game_release":
                next_owner = "core"
        self._voice_lease_generation = generation
        self._voice_lease_synchronized = True
        await self._apply_voice_lease_state(
            owner=next_owner,
            hard_muted=next_hard_muted,
            focus_suppressed=next_focus_suppressed,
            reason=normalized_event,
            force_abort=True,
        )
        return True

    async def _handle_core_asr_turn_abandoned(self, token: VoiceTurnToken) -> None:
        self._voice_input_registry.invalidate_utterance(
            token,
            reason="asr_turn_abandoned",
        )
        await self._voice_input_registry.wait_idle()

    def _independent_asr_user_turn_active(self) -> bool:
        """Expose a provider-neutral user-turn gate to Core collaborators."""
        if getattr(self, "_asr_route_mode", "blocked") != "independent":
            return False
        runtime = getattr(self, "_asr_runtime", None)
        lifecycle = getattr(runtime, "_asr_lifecycle", None)
        state = getattr(getattr(lifecycle, "snapshot", None), "state", None)
        pending_delivery = getattr(
            runtime,
            "has_pending_transcript_delivery",
            None,
        )
        return (
            state
            in {
                VoiceLifecycleState.PREWARMING,
                VoiceLifecycleState.ACTIVE,
                VoiceLifecycleState.DRAINING,
            }
            or callable(pending_delivery)
            and pending_delivery()
        )

    async def _prepare_voice_input_turn(self, token: VoiceTurnToken) -> bool:
        self._ensure_asr_runtime_state()
        # A lease transition activates its next consumer before waiting for
        # keyed cancellation callbacks and aborting the ASR transport. Reject
        # a prepare arriving in that window before it can pin an old ingress
        # token to the newly active consumer.
        if (
            not isinstance(token, VoiceTurnToken)
            or token.ingress != self._capture_ingress_token()
            or not self._voice_input_accepts_pcm()
        ):
            return False
        if not self._voice_input_registry.begin_utterance(token):
            return False
        return await self._voice_input_registry.prepare_utterance(token)

    async def _dispatch_voice_input_partial(
        self,
        event: VoicePartialEvent,
    ) -> None:
        await self._voice_input_registry.dispatch_partial(event)

    async def _dispatch_voice_input_final(
        self,
        event: VoiceTranscriptEvent,
    ) -> None:
        result = await self._voice_input_registry.dispatch_final(event)
        self._observe_core_asr_delivery(event, stage="voice_registry_final", outcome=result.value, phase="dispatch")
        if result is VoiceInputDispatchResult.CALLBACK_FAILED:
            await self._send_core_asr_status(
                AsrStatusEvent(
                    code="ASR_INDEPENDENT_INJECTION_FAILED",
                    provider=event.provider,
                    session_epoch=event.turn_token.ingress.session_epoch,
                )
            )
        elif result is VoiceInputDispatchResult.REJECTED:
            logger.debug(
                "[%s] voice input final rejected turn=%s-%s",
                self.lanlan_name,
                event.turn_token.ingress.session_epoch,
                event.turn_token.turn_id,
            )

    async def _cancel_core_chat_voice_turn(
        self,
        context: CoreChatTurnContext,
        reason: str,
    ) -> None:
        del reason
        try:
            # Registry owns this cancellation even if its waiter goes away.
            # Optional display I/O must not retain the keyed turn/pause forever.
            async with asyncio.timeout(1.0):
                await self._send_core_asr_preview_clear(context.external_turn_id)
        except TimeoutError:
            logger.debug("[%s] cancelled ASR turn preview clear timed out", self.lanlan_name)
        finally:
            # Registry cancellation has already consumed the keyed route. Even
            # if websocket preview cleanup is itself cancelled, the response
            # arbiter pause must be released exactly once here.
            self._abandon_core_voice_turn(
                context.external_turn_id,
                session_ref=context.session_ref,
            )

    async def _prepare_core_voice_turn(
        self,
        token: VoiceTurnToken,
        *,
        session_ref: object | None = None,
        abandon_on_failure: bool = True,
    ) -> bool:
        if not self._ingress_token_matches(token.ingress):
            return False
        if self._voice_lease_owner != "core":
            return False
        if session_ref is None:
            session_ref = getattr(self, "session", None)
        if session_ref is None:
            return False
        transition_generation = self._voice_input_transition_generation
        external_turn_id = f"asr-{token.ingress.session_epoch}-{token.turn_id}"
        self._begin_core_multimodal_turn(external_turn_id, token)
        previous_preview_turn_id = self._core_asr_preview_turn_id
        previous_preview_turn_token = self._core_asr_preview_turn_token
        previous_preview_text = self._core_asr_preview_text
        # Turn preparation is the ordered boundary between two turns' partial
        # streams, so every preview from here on belongs to this turn. Stamping
        # the owner here is what lets a previous turn's delayed clear be
        # recognized as stale by the frontend instead of erasing this bubble.
        self._core_asr_preview_turn_id = external_turn_id
        self._core_asr_preview_turn_token = token
        self._core_asr_preview_text = ""

        def operation_is_current() -> bool:
            return bool(
                transition_generation == self._voice_input_transition_generation
                and self._voice_lease_owner == "core"
                and session_ref is self.session
                and self._ingress_token_matches(token.ingress)
            )

        prepare = getattr(session_ref, "prepare_external_voice_turn", None)
        preparation_succeeded = False
        try:
            if callable(prepare):
                reconnected = await prepare(turn_id=external_turn_id)
                if reconnected is True and not await (
                    self._restart_message_handler_after_session_reconnect(
                        session_ref
                    )
                ):
                    if abandon_on_failure:
                        self._abandon_core_voice_turn(
                            external_turn_id,
                            session_ref=session_ref,
                        )
                    return False
            else:
                interrupt = getattr(session_ref, "handle_interruption", None)
                if callable(interrupt):
                    await interrupt()
            if not operation_is_current():
                if abandon_on_failure:
                    self._abandon_core_voice_turn(
                        external_turn_id,
                        session_ref=session_ref,
                    )
                return False
            await self.handle_new_message()
            if operation_is_current():
                preparation_succeeded = True
                return True
            if abandon_on_failure:
                self._abandon_core_voice_turn(
                    external_turn_id,
                    session_ref=session_ref,
                )
            return False
        except asyncio.CancelledError:
            if abandon_on_failure:
                self._abandon_core_voice_turn(
                    external_turn_id,
                    session_ref=session_ref,
                )
            raise
        except Exception:
            if abandon_on_failure:
                self._abandon_core_voice_turn(
                    external_turn_id,
                    session_ref=session_ref,
                )
            if not operation_is_current():
                return False
            logger.warning(
                "[%s] independent ASR turn preparation failed",
                self.lanlan_name,
            )
            return False
        finally:
            if (
                not preparation_succeeded
                and self._core_asr_preview_turn_id == external_turn_id
                and self._core_asr_preview_turn_token == token
            ):
                self._core_asr_preview_turn_id = previous_preview_turn_id
                self._core_asr_preview_turn_token = previous_preview_turn_token
                self._core_asr_preview_text = previous_preview_text
            if not preparation_succeeded:
                record = self._core_multimodal_turns.pop(external_turn_id, None)
                if record is not None:
                    record.invalidated.set()

    async def _submit_core_voice_turn(
        self,
        text: str,
        *,
        turn_id: str,
        session_ref: object | None = None,
    ) -> None:
        """Submit a completed ASR turn to the session that produced it.

        ``session_ref`` is the session the caller already validated. Re-reading
        ``self.session`` here instead would discard that validation: the caller
        awaits a preview restore between its check and this call, and a hot swap
        or a concurrent start landing in that window would make this inject one
        conversation's transcript into another. Defaults to the current session
        only for callers that have no captured reference of their own.
        """

        if session_ref is None:
            session_ref = self.session
        if getattr(self, "response_backend", "realtime") == "offline_vlm":
            # A failed promotion-time TTS initialization leaves the Offline
            # conversation/listener usable. Retry the external TTS lifecycle on
            # every later independent-ASR turn before asking it to generate;
            # never push this concern into the general Offline stream_text path.
            await self.ensure_tts_pipeline_alive()
        submit = getattr(session_ref, "submit_external_voice_turn", None)
        if callable(submit):
            await submit(text, turn_id=turn_id)
        else:
            await session_ref.create_response(text)

    def _observe_core_asr_delivery(self, event, *, stage="core_voice_delivery", outcome, phase) -> None:
        """Keep ASR/Core correlation without copying transcript or session data."""
        try:
            self._asr_runtime._schedule_pipeline_event(
                stage, event.turn_token.ingress, turn_id=event.turn_token.turn_id,
                outcome=outcome, phase=phase,
            )
        except Exception:
            pass

    async def _dispatch_core_asr_transcript(
        self,
        event: VoiceTranscriptEvent,
        *,
        session_ref: object | None = None,
        source_game_route_identity: tuple[str, str, str] | None = None,
    ) -> None:
        token = event.turn_token.ingress
        external_turn_id = f"asr-{token.session_epoch}-{event.turn_token.turn_id}"
        if session_ref is None:
            session_ref = getattr(self, "session", None)
        prepared_session_ref = session_ref
        transition_generation = self._voice_input_transition_generation
        diagnostic_phase = "initial_route_check"
        diagnostic_outcome = "abandoned"
        self._observe_core_asr_delivery(event, outcome="started", phase=diagnostic_phase)
        try:
            if (
                not self._ingress_token_matches(token)
                or self._voice_lease_owner != "core"
                or session_ref is None
            ):
                return
            if not event.text.strip():
                diagnostic_phase = "empty_final"
                # An empty final still completed the turn provider-side (e.g.
                # the OpenAI/Step stalled-item timeouts): Core deliberately
                # injects no user_transcript for empty text, yet the frontend
                # removes the streaming preview bubble only on
                # user_transcript / fatal teardown / session stop. Tell it
                # explicitly, or the bubble lingers and gets reused by the
                # next turn.
                await self._send_core_asr_preview_clear(external_turn_id)
                return
            # A normal pending hot-swap may already be caching the conversation.
            # Snapshot it before handle_input_transcript appends this final: the
            # handoff candidate receives the prior context here, while the raw
            # transcript itself is submitted exactly once with its image below.
            cached_turns_before_final = [
                dict(item)
                for item in (
                    getattr(self, "message_cache_for_new_session", None) or []
                )
                if isinstance(item, dict)
            ]
            turn_record = self._core_multimodal_turns.get(external_turn_id)
            if turn_record is not None:
                # 从这里到本函数的 finally（_abandon_core_voice_turn 按 turn_id 摘
                # 掉它）之间，这条记录是**在派发中**的，后继的 prepare 不能挤掉它。
                turn_record.dispatch_started = True
                diagnostic_phase = "visual_validation"
                await self._await_independent_visual_validation_tasks(
                    external_turn_id
                )
                if (
                    self._core_multimodal_turns.get(external_turn_id)
                    is not turn_record
                    or transition_generation
                    != self._voice_input_transition_generation
                    or self._voice_lease_owner != "core"
                    or not self._ingress_token_matches(token)
                ):
                    return
            multimodal_turn = self._snapshot_core_multimodal_turn(
                external_turn_id,
                event.text,
            )
            transcript_kwargs = {
                "is_voice_source": True,
                "source": "independent_asr",
                "metadata": {"provider": event.provider},
            }
            transcript_kwargs["source_game_route_identity"] = (
                source_game_route_identity
            )
            diagnostic_phase = "transcript_acceptance"
            accepted = await self.handle_input_transcript(
                event.text,
                **transcript_kwargs,
            )
            self._observe_core_asr_delivery(
                event, outcome="accepted" if accepted else "rejected", phase=diagnostic_phase,
            )
            def route_still_core() -> bool:
                """The route-identity half, re-checkable across an await.

                Deliberately excludes ``self.session is session_ref``: a hot
                swap promoting a new session is NOT a reason to drop this
                transcript, it is a reason to submit it to the session it was
                validated against -- which ``_submit_core_voice_turn`` does via
                ``session_ref``. Folding the session check in here would make a
                hot swap silently discard the user's finished utterance.
                """
                return (
                    transition_generation
                    == self._voice_input_transition_generation
                    and self._voice_lease_owner == "core"
                    and self._ingress_token_matches(token)
                    and (
                        multimodal_turn is None
                        or turn_record is None
                        or self._core_multimodal_turns.get(external_turn_id)
                        is turn_record
                    )
                )

            operation_still_current = route_still_core()
            if not accepted or not operation_still_current:
                if (
                    not accepted
                    and operation_still_current
                    and self.session is session_ref
                ):
                    # Rejected text (echo suppression, takeover routing) also
                    # never produces a user_transcript; drop the preview so it
                    # cannot linger. Guarded on an unchanged runtime identity:
                    # when the world moved on, a newer turn may already own
                    # the preview bubble and the clear must not remove it.
                    await self._send_core_asr_preview_clear(external_turn_id)
                return
            def visual_ownership_lost() -> bool:
                """Whether a successor has taken this turn's frames since the freeze.

                Deliberately kept out of ``route_still_core()``: losing the
                frames must downgrade this turn to text, never drop the user's
                sentence. Re-checkable, because every await between the freeze
                and the provider call is another chance for a successor to
                prepare -- the transcript send, preview restoration, the swap
                barrier, replacement-session preparation.
                """
                return turn_record is not None and turn_record.invalidated.is_set()

            def note_visual_ownership_lost() -> None:
                logger.info(
                    "[%s] independent ASR turn %s superseded after freeze; "
                    "submitting transcript without its frames",
                    self.lanlan_name,
                    external_turn_id,
                )

            if multimodal_turn is not None and visual_ownership_lost():
                note_visual_ownership_lost()
                multimodal_turn = None
            if getattr(self, "response_backend", "realtime") == "offline_vlm":
                # handle_input_transcript historically keys voice display off
                # the session class. After a main-session VLM promotion the
                # input is still voice/independent-ASR even though the answering
                # client is Offline, so preserve the same frontend event here.
                websocket_ref = getattr(self, "websocket", None)
                send_json = getattr(websocket_ref, "send_json", None)
                if callable(send_json):
                    try:
                        await send_json({
                            "type": "user_transcript",
                            "text": event.text.strip(),
                        })
                    except Exception:
                        logger.warning(
                            "[%s] Offline VLM voice transcript delivery failed",
                            self.lanlan_name,
                        )
            diagnostic_phase = "preview_restore"
            await self._restore_core_asr_preview_after_final(
                external_turn_id,
                session_epoch=token.session_epoch,
            )
            # Re-fence: the restore above awaited a websocket send, and pinning
            # session_ref only protects the SESSION half of what was checked
            # before it. A game or text takeover landing inside that await moves
            # _voice_lease_owner / _voice_input_transition_generation without
            # necessarily replacing self.session, and this would still inject
            # the transcript and start an ordinary Core response after the route
            # had left Core (Codex P2). Same shape as the fence-notify-re-fence
            # order _fail_closed_voice_route owns.
            if not route_still_core():
                return
            # Synchronize with close+promotion itself, rather than sampling
            # final_swap_task once. A swap may begin during any awaited submit;
            # sharing this barrier means it cannot close the prepared session
            # until this final finishes, while a final arriving second observes
            # the promoted replacement. Bound the wait so the serial transcript
            # dispatcher cannot be held forever by a stuck swap.
            session_swap_lock = self._core_voice_session_swap_lock
            handoff_target = None
            diagnostic_phase = "session_swap_barrier"
            try:
                await asyncio.wait_for(
                    session_swap_lock.acquire(),
                    timeout=self._core_voice_session_swap_barrier_timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] Timed out waiting for Core voice hot-swap barrier; "
                    "dropping final fail-closed",
                    self.lanlan_name,
                )
                return
            try:
                if not route_still_core():
                    return

                # Re-read only while close+promotion is excluded. An unchanged
                # transition/ingress means a replacement session is the endpoint
                # for this same conversation, not an unrelated start.
                target_session = getattr(self, "session", None)
                if target_session is None:
                    return
                if target_session is not session_ref:
                    diagnostic_phase = "replacement_session_prepare"
                    session_ref = target_session
                    prepare = getattr(
                        session_ref,
                        "prepare_external_voice_turn",
                        None,
                    )
                    if callable(prepare):
                        reconnected = await prepare(turn_id=external_turn_id)
                        if reconnected is True and not await (
                            self._restart_message_handler_after_session_reconnect(
                                session_ref
                            )
                        ):
                            return
                    if not route_still_core() or self.session is not session_ref:
                        return
                diagnostic_phase = "response_submission"
                if multimodal_turn is None:
                    await self._submit_core_voice_turn(
                        event.text,
                        turn_id=external_turn_id,
                        session_ref=session_ref,
                    )
                else:
                    get_delivery = getattr(
                        session_ref,
                        "get_multimodal_turn_delivery",
                        None,
                    )
                    delivery = (
                        get_delivery() if callable(get_delivery) else None
                    )
                    delivery_value = str(
                        getattr(delivery, "value", delivery) or "handoff_required"
                    )
                    if delivery_value == "direct_atomic":
                        submit_multimodal = getattr(
                            session_ref,
                            "submit_multimodal_turn",
                            None,
                        )
                        if not callable(submit_multimodal):
                            raise RuntimeError(
                                "DIRECT_MULTIMODAL_SUBMIT_UNAVAILABLE"
                            )
                        if visual_ownership_lost():
                            # 上面那次检查之后还隔着传 transcript、恢复预览、抢
                            # swap 闸几段 await。这是**真正调用 provider 之前**的
                            # 最后一个同步点。
                            note_visual_ownership_lost()
                            multimodal_turn = None
                            await self._submit_core_voice_turn(
                                event.text,
                                turn_id=external_turn_id,
                                session_ref=session_ref,
                            )
                        else:
                            try:
                                await submit_multimodal(
                                    multimodal_turn.transcript,
                                    multimodal_turn.images,
                                    turn_id=multimodal_turn.turn_id,
                                    # The channel frozen with these frames,
                                    # not the one the session is on now: a
                                    # session-side read drifts to the next
                                    # utterance's channel across the trim /
                                    # queue / send awaits.
                                    source=multimodal_turn.source,
                                    # Gemini 那条路在真正送出之前还有一段
                                    # 压缩 await，后继发声可以在其中拿走帧。
                                    # 与 Offline 交接同源的判据。
                                    visual_still_owned=(
                                        lambda: not visual_ownership_lost()
                                    ),
                                )
                            except ResponseAdmissionRejected:
                                # provider 侧的取代判据（后继回合已经 prepare，
                                # arbiter 的 admission 闸把这张 ticket 拒了）比
                                # Core 的 invalidated 更晚也更权威。被拒时已提交
                                # 的 item 已经删干净，provider 侧不留痕迹 ——
                                # 但这一轮的**话**不能跟着帧一起消失。与
                                # visual_ownership_lost 那三处同一判据：丢帧降级
                                # 成纯文本，绝不丢用户的句子。
                                logger.info(
                                    "[%s] independent ASR turn %s lost its "
                                    "provider admission window; submitting "
                                    "transcript without its frames",
                                    self.lanlan_name,
                                    external_turn_id,
                                )
                                multimodal_turn = None
                                await self._submit_core_voice_turn(
                                    event.text,
                                    turn_id=external_turn_id,
                                    session_ref=session_ref,
                                )
                    else:
                        # Candidate connect must happen outside the swap lock so
                        # the healthy Realtime session remains usable until the
                        # replacement is ready.
                        handoff_target = session_ref
            finally:
                session_swap_lock.release()
            if handoff_target is not None and visual_ownership_lost():
                # 交接本身还要连一个候选会话，比直接提交更长。释放 swap 闸之后再
                # 确认一次所有权，否则整段交接都在替后继那一轮搬帧。
                note_visual_ownership_lost()
                handoff_target = None
                multimodal_turn = None
                await self._submit_core_voice_turn(
                    event.text,
                    turn_id=external_turn_id,
                    session_ref=session_ref,
                )
            if handoff_target is not None:
                diagnostic_phase = "offline_handoff"
                delivered = await self._handoff_to_offline_vlm_and_submit(
                    multimodal_turn,
                    expected_session=handoff_target,
                    prepared_session=prepared_session_ref,
                    operation_is_current=route_still_core,
                    cached_turns_before_final=cached_turns_before_final,
                    # 交接内部还有连候选会话、promote、起 TTS、同步工具一长串
                    # await。所有权判据必须跟着进去，而且**不能**并进
                    # operation_is_current —— 那个谓词为假会整轮放弃，丢帧只该
                    # 降级成纯文本。
                    visual_still_owned=lambda: not visual_ownership_lost(),
                )
                if not delivered and route_still_core():
                    await self.send_status(json.dumps({
                        "code": "ASR_MULTIMODAL_TURN_FAILED",
                        "details": {"stage": "offline_vlm_handoff"},
                    }))
                diagnostic_outcome = "submitted" if delivered else "handoff_not_delivered"
            else:
                diagnostic_outcome = "submitted"
        except asyncio.CancelledError:
            diagnostic_outcome = "cancelled"
            raise
        except Exception:
            diagnostic_outcome = "failed"
            raise
        finally:
            self._observe_core_asr_delivery(event, outcome=diagnostic_outcome, phase=diagnostic_phase)
            self._abandon_core_voice_turn(
                external_turn_id,
                session_ref=session_ref,
            )

    async def _send_core_asr_preview(
        self,
        event: VoicePartialEvent,
        *,
        remember: bool = True,
    ) -> None:
        if (
            event.session_epoch != self._capture_ingress_token().session_epoch
            or self._voice_lease_owner != "core"
            or self._asr_route_mode != "independent"
            or not self._voice_input_accepts_pcm()
        ):
            return
        websocket_ref = getattr(self, "websocket", None)
        send_json = getattr(websocket_ref, "send_json", None)
        if not callable(send_json):
            return
        turn_id = str(
            getattr(self, "current_speech_id", None)
            or f"asr-preview-{event.session_epoch}"
        )
        payload = {
            "type": "user_transcript_preview",
            "text": event.text,
            "turn_id": turn_id,
        }
        # ``turn_id`` stays the Core speech id (unchanged legacy field). The
        # ASR turn identity travels in its own key so the frontend compares
        # like with like; it is omitted while no turn is prepared, which keeps
        # an unkeyed bubble on the pre-existing unconditional-removal path.
        preview_turn_id = self._core_asr_preview_turn_id
        if preview_turn_id:
            payload["asr_turn_id"] = preview_turn_id
        await send_json(payload)
        # ``remember=False`` is used by the repair re-send, which only mirrors
        # what the cache already holds: it must never write its own (possibly
        # older, since ``send_json`` above is awaited) text back over a partial
        # that landed in the meantime.
        if (
            remember
            and preview_turn_id
            and self._core_asr_preview_turn_id == preview_turn_id
        ):
            self._core_asr_preview_text = event.text

    async def _restore_core_asr_preview_after_final(
        self,
        finalized_turn_id: str,
        *,
        session_epoch: int,
    ) -> None:
        """Re-render a newer turn's preview erased by a late user_transcript.

        ``user_transcript`` is emitted by the injection path and carries no
        turn identity, so the frontend cannot tell whether it belongs to the
        bubble on screen; it removes the singleton preview unconditionally.
        When the accepted final arrives through the transcript dispatcher
        worker after the next turn already started streaming partials, that
        removal erases the newer turn's bubble. Re-send the newer turn's last
        preview text right behind the transcript on the ordered websocket so
        the bubble comes back immediately instead of waiting for whichever
        partial happens to arrive next.

        Owner and text are read here, after the injection await, never
        snapshotted before it: the injection yields, so the newer turn can
        stream further partials (or hand the bubble to a turn newer still)
        while it is in flight. Re-sending a pre-await snapshot would push the
        visible preview backwards -- until the next partial, or forever if the
        turn has stopped producing them.
        """
        preview_owner_turn_id = self._core_asr_preview_turn_id
        preview_owner_turn_token = getattr(
            self,
            "_core_asr_preview_turn_token",
            None,
        )
        preview_owner_text = self._core_asr_preview_text
        if (
            not preview_owner_turn_id
            or preview_owner_turn_token is None
            or not preview_owner_text
            or preview_owner_turn_id == finalized_turn_id
        ):
            return
        try:
            await self._send_core_asr_preview(
                VoicePartialEvent(
                    turn_token=preview_owner_turn_token,
                    text=preview_owner_text,
                ),
                remember=False,
            )
        except Exception:
            logger.debug(
                "[%s] independent ASR preview restore delivery failed",
                self.lanlan_name,
            )

    async def _send_core_asr_preview_clear(self, turn_id: str) -> None:
        """Ask the frontend to drop the streaming ASR preview bubble.

        Reuses the existing ``user_transcript_preview`` message with empty
        text as the clear signal: genuine partials are never empty (the ASR
        runtime strips and drops blank partials before ``on_partial``), so
        the frontend can treat an empty preview as removal without a new
        protocol message type. Identity note: finals reach Core through the
        transcript dispatcher's own worker task, so this clear can trail the
        NEXT turn's partials on the ordered websocket. It therefore carries
        ``asr_turn_id`` and the frontend drops the bubble only while that id
        still matches the displayed preview, which makes a stale clear a
        no-op instead of erasing the newer turn's bubble.
        """
        websocket_ref = getattr(self, "websocket", None)
        send_json = getattr(websocket_ref, "send_json", None)
        if not callable(send_json):
            return
        if self._core_asr_preview_turn_id == turn_id:
            self._core_asr_preview_text = ""
            self._core_asr_preview_turn_token = None
        try:
            await send_json(
                {
                    "type": "user_transcript_preview",
                    "text": "",
                    "turn_id": turn_id,
                    "asr_turn_id": turn_id,
                }
            )
        except Exception:
            logger.debug(
                "[%s] independent ASR preview clear delivery failed",
                self.lanlan_name,
            )

    async def _handle_core_asr_failure(self, event: AsrFailureEvent) -> None:
        source_identity = self._capture_core_asr_operation_identity()
        route_operation_generation = self._asr_route_operation_generation

        def failure_is_current() -> bool:
            return bool(
                self._core_asr_operation_identity_matches(source_identity)
                and event.session_epoch
                == self._core_asr_identity_ingress_token(
                    source_identity
                ).session_epoch
            )

        # Runtime re-fences its failure identity after display delivery and
        # enters this callback directly. Commit the current route/lease fence
        # before our first suspension: display serialization must not defer
        # mandatory retirement beyond the caller's notification deadline.
        # Consumer cancellation can still publish through the notification
        # lock independently once the physical input has been fenced.
        if not failure_is_current():
            return
        self._set_microphone_route("blocked")
        self._invalidate_voice_pcm_sync("independent_asr_failure")
        post_transition_identity = self._capture_core_asr_operation_identity()
        # Fail-safe for clients that never receive or never honour the
        # teardown notice (an older build, a third-party client, a
        # throttled background tab). The route is fail-closed for the rest
        # of the session, so stop accepting the PCM at ingress too. The
        # game owner is exempt: the galgame route holds the lease through
        # its built-in Registry consumer and must not be collaterally
        # revoked.
        # No notice of its own -- Runtime already attempted BLOCKED delivery.
        # Revoke before joining Registry: its consumer can await display I/O,
        # and the caller's notification deadline can cancel this callback.
        # The revoke fences the lease/PCM synchronously before its first await;
        # Core's keyed Registry cancellation owns its bounded preview cleanup.
        #
        # Re-captured AFTER this handler's own mutations, and that is the
        # whole point: ``source_identity`` was taken while the route was
        # still "independent", and the identity tuple carries
        # ``_asr_route_mode`` (plus ``_microphone_route_generation``, inside
        # the ingress token). Handing that pre-transition tuple to
        # ``still_current`` made the predicate false on ENTRY -- against a
        # transition this handler had itself performed two lines up -- so
        # _fail_closed_voice_route returned before the revoke and left a
        # live hardware microphone uploading into a dead route. The fence
        # exists to reject a COMPETING newer operation, never our own step.
        #
        # The sibling predicates dodge this by testing the route mode
        # ABSOLUTELY (``_asr_route_mode == "blocked"``) rather than against
        # a captured value; a full-tuple comparison cannot, so it has to be
        # re-based here instead.
        await self._fail_closed_voice_route(
            "independent_asr_failure",
            operation_generation=route_operation_generation,
            still_current=lambda: self._core_asr_operation_identity_matches(
                post_transition_identity
            ),
        )
        await self._voice_input_registry.wait_idle()

    async def _send_core_asr_status(self, event: AsrStatusEvent) -> None:
        source_identity = self._capture_core_asr_operation_identity()
        async with self._asr_notification_lock:
            if (
                not self._core_asr_operation_identity_matches(source_identity)
                or event.session_epoch
                != self._core_asr_identity_ingress_token(source_identity).session_epoch
            ):
                return
            self._core_asr_notification_revision = max(
                self._core_asr_notification_revision + 1,
                event.lifecycle_revision,
            )
            transport_generation = max(0, event.transport_generation)
            await self._send_voice_control_status(
                json.dumps(
                    {
                        "code": event.code,
                        "details": {
                            "provider": event.provider,
                            "session_epoch": event.session_epoch,
                            "transport_generation": transport_generation,
                            "lifecycle_revision": self._core_asr_notification_revision,
                            "reason_code": event.reason_code,
                            "incident_id": event.incident_id,
                        },
                    }
                ),
            )

    async def _send_core_asr_lifecycle(
        self,
        event: AsrLifecycleNotification,
    ) -> None:
        source_identity = self._capture_core_asr_operation_identity()
        async with self._asr_notification_lock:
            if (
                not self._core_asr_operation_identity_matches(source_identity)
                or event.session_epoch
                != self._core_asr_identity_ingress_token(source_identity).session_epoch
            ):
                return
            self._core_asr_notification_revision = max(
                self._core_asr_notification_revision + 1,
                event.lifecycle_revision,
            )
            transport_generation = max(0, event.transport_generation)
            await self._send_voice_control_status(
                json.dumps(
                    {
                        "code": "ASR_LIFECYCLE_STATE",
                        "details": {
                            "provider": event.provider,
                            "state": event.state,
                            "route_mode": self._asr_route_mode,
                            "session_epoch": event.session_epoch,
                            "transport_generation": transport_generation,
                            "lifecycle_revision": self._core_asr_notification_revision,
                            "reason_code": event.reason_code,
                            "incident_id": event.incident_id,
                        },
                    }
                ),
            )

    async def _wait_asr_transcript_dispatch_idle(self) -> None:
        """Test seam: await Core-side transcript dispatch quiescence.

        Production code never needs this; tests use it to order assertions
        after the serial dispatch worker drains. Delegates to the ASR
        component's allow-listed public API.
        """

        await self._asr_runtime.wait_transcript_idle()
        await self._voice_input_registry.wait_idle()
