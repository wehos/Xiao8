# -- coding: utf-8 --
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

from ._shared import (
    GEMINI_CANCELLED_TERMINAL_TTL_SECONDS,
    Any,
    Callable,
    Dict,
    IMAGE_IDLE_RATE_MULTIPLIER,
    ImageStageResult,
    List,
    NATIVE_IMAGE_MIN_INTERVAL,
    OMNI_WS_FRAME_LIMIT_BYTES,
    Optional,
    ToolCall,
    TurnDetectionMode,
    VisualDeliveryMode,
    VISION_ANALYSIS_MAX_TOKENS,
    _IMAGE_ANALYSIS_PENDING_DESCRIPTION,
    asyncio,
    base64,
    calculate_text_similarity,
    get_stepfun_tts_default_voice,
    json,
    logger,
    np,
    parse_arguments_json,
    time,
    uuid,
    websockets,
)
from main_logic.provider_failure_signals import (
    CODES_REQUIRING_MSG_DETAIL,
    classify_provider_failure_text,
)
from ._protocol_capabilities import (
    ID_BEARING_RESPONSE_CONTENT_EVENT_TYPES,
    _response_id_text,
)


_ATTACHED_TRANSPORT = object()


class _RealtimeEventOwnerRetired(ConnectionError):
    """The explicit send guard rejected this event before transport I/O."""

# Ceiling on each host step inside a fail-open release that may be cut short.
# The arbiter bounds the WHOLE notification with one shared budget
# (_STUCK_RELEASE_NOTIFY_TIMEOUT, 2.0s); without a per-step ceiling the first
# await consumes it and everything after it is cancelled where it stands.
# Three bounded steps x 0.5s leaves the rest of the arbiter's budget for the
# speech-id rotation, which gets no ceiling of its own because it is last —
# nothing behind it can be starved. That is a necessary condition, not a
# guarantee: asyncio.wait_for bounds when the cancellation is DELIVERED, not
# when the coroutine returns, and the outer budget still reaches the rotation.
_STUCK_RELEASE_STEP_TIMEOUT = 0.5


# How many finished response ids to remember for usage deduplication. A repeat
# arrives right behind its original, so this only has to outlive the events
# interleaved between them; it is a leak guard, not a history.
_USAGE_RECORDED_ID_LIMIT = 32
_INPUT_ROUTE_IDENTITY_ITEM_LIMIT = 8
# ``None`` is a valid route owner (no active game route), so the "nothing to
# freeze" answer needs its own sentinel.
_NO_ROUTE_IDENTITY_COMMIT = object()

# How many utterance ids a ``speech_started`` may have scoped and still be
# recognised when that utterance's transcript arrives. Input transcription is
# its own job on these providers, so a transcript can land several turns after
# the speech it describes; anything older than this is indistinguishable from a
# new user turn, which is also the safe reading -- it retires stale tool work.
_RAW_SCOPED_UTTERANCE_MEMORY = 8

# Oldest capture age still trusted when translating a frame's monotonic
# ingress stamp into wall clock for the plugin bus. Frames reach the bus within
# a couple of seconds of capture; anything past this is a caller whose
# "captured_at" is not on the monotonic clock at all, and guessing a wall time
# from it would put the record decades away.
_FRAME_BUS_MAX_CAPTURE_AGE_SECONDS = 300.0

# `error` 事件的致命性判定是一串子串匹配（'429' / '1008' / '503' / 'quota' ...）。它
# 过去匹配在 `str(event['error'])` 上，也就是整个 dict 的 repr —— 里面回显着我们自己
# 生成的客户端相关性 id（`event_user_item_<uuid4().hex>` 之类）。hex 的字符集是
# 0-9a-f，'429' 这三个字符全在里面：32 位 hex 串里随机出现 '429' 的概率约 0.7%，
# '1008' 约 0.04%，'503' 约 0.7%。撞上一次，一次普通的「这条事件被拒」就被误判成配额 /
# 策略致命错误，直接 close() 掉整条 realtime 连接 —— 用户话说到一半，连接没了，而且
# 无法复现。id 只是相关性标识，不携带任何分类信息，所以分类前先把它们剔干净。
#
# 只剔 id 字段，不动 message / code / type / param：`code: 1008`、`"HTTP 429"` 这些
# 真信号一个不少。剔掉的字段照样进日志和 on_connection_error，诊断信息没有损失。
def _is_correlation_key(key: str) -> bool:
    lowered = key.lower()
    return lowered == "id" or lowered.endswith("_id")


def _error_classification_text(error: Any) -> str:
    """Build the keyword-matching text for an ``error`` event, minus correlation ids."""
    if isinstance(error, dict):
        return " ".join(
            str(value)
            for key, value in error.items()
            if value is not None and not _is_correlation_key(str(key))
        )
    return str(error or "")


def _peer_close_descriptor(received_code) -> str:
    """Name a peer close by its code alone, never by its reason text."""

    if received_code is None:
        return "WebSocket closed without a close code"
    return f"WebSocket close code {received_code}"


def _classify_peer_close(received_code, received_reason) -> tuple[str, dict[str, str] | None]:
    """Map a peer close to existing stable UI codes without exposing its reason."""

    code = classify_provider_failure_text(received_reason)
    if code is None and received_code == 1008:
        # The reason said nothing, but the close code alone is a policy
        # signal on every provider that sends it.
        code = "API_1008_FALLBACK"
    if code is None:
        return "CHARACTER_DISCONNECTED", None
    if code in CODES_REQUIRING_MSG_DETAIL:
        # The reason string is peer-controlled and deliberately withheld, but
        # these codes' i18n strings interpolate {{msg}} — emitting them with
        # no msg renders the raw placeholder to the user. Substitute the close
        # code, which is ours to disclose and is what a bug report needs.
        return code, {"msg": _peer_close_descriptor(received_code)}
    return code, None


class RealtimeImagePayloadTooLargeError(RuntimeError):
    """A callback image cannot fit the provider's WebSocket frame limit."""



class _TransportMixin:
    _WS_FRAME_LIMIT = OMNI_WS_FRAME_LIMIT_BYTES  # safe threshold below 256KB server cap

    def _clear_input_route_identities(self) -> None:
        self._input_route_identity_captured = False
        self._input_route_identity = None
        self._input_route_identity_by_item.clear()
        self._reset_input_route_identity_stream()

    def _reset_input_route_identity_stream(self) -> None:
        """Forget the per-buffer route observation after it was consumed."""
        self._input_route_identity_stream_armed = False
        self._input_route_identity_stream_owner = None

    def _note_input_route_identity_frame(self, identity) -> None:
        """Track the route owning the most recent frame before an onset.

        The local onset gate (RMS / RNNoise) and the provider's server VAD are
        independent detectors with independent thresholds, so "server VAD fired
        but the local gate never armed a snapshot" is an ordinary outcome rather
        than an anomaly. This observation covers that case: the frames
        themselves still prove which route was active while they were captured.

        Deliberately last-write-wins. Read the alternatives before changing it;
        this line has already oscillated across four revisions, because every
        variant that tries to be stricter here fails in the OTHER direction:

        * Arming once and keeping the first owner (or freezing on one raw-RMS
          frame) strands the mark on a pre-switch route, so the first utterance
          after entering or replacing a route is rejected.
        * Accumulating a per-buffer verdict and binding ``None`` when the buffer
          looks like it spans two routes has to decide when its window ends, and
          every window it fails to close leaks a stale owner into the next
          utterance. Tried; it drops the player's first line whenever the mic was
          already open across a route switch. See
          ``test_idle_frames_before_a_route_switch_do_not_strand_the_next_utterance``
          and ``test_a_finished_utterance_does_not_poison_the_next_one``.

        Every one of those failures is a SILENT drop: the mismatch is rejected in
        ``handle_input_transcript`` above the takeover dispatcher, so the game
        receives nothing and nothing is logged. Overwriting is self-correcting
        instead -- a stale owner survives at most until the next frame.

        The accepted residual is the reverse error: if the route switches inside
        the provider's onset delay and a post-switch frame is streamed before the
        delayed ``speech_started``, that utterance binds the new route. The
        exposure window is that onset delay (hundreds of ms), an order of
        magnitude below the seconds-scale STT latency this ownership guards
        against, and it additionally needs speech quiet enough that the local
        gate never fired. Before this mechanism existed the misattribution window
        was the entire STT latency, unconditionally -- so this is a much smaller
        instance of a pre-existing error, not a new one.

        Eliminating that residual needs the input buffer isolated when the route
        changes, not another ownership heuristic. That belongs to the realtime
        audio subsystem and must also cover Gemini, where ``clear_audio_buffer``
        is a no-op and transcripts arrive with no ``item_id`` at all.

        No "an utterance is already open" guard: once ``speech_started`` has
        bound an item, that binding is fixed, and every frame between two onsets
        is equally "before the next onset", so suppressing the ones after an
        onset cannot change any outcome.
        """
        self._input_route_identity_stream_armed = True
        self._input_route_identity_stream_owner = identity

    def _pending_input_route_identity_commit(self):
        """Read the owner a MANUAL commit would freeze, without freezing it.

        MANUAL mode disables server VAD, so no ``speech_started`` ever arrives
        and nothing binds an owner for the buffer being committed. The commit
        itself IS that boundary, exactly as ``speech_started`` is in server-VAD
        mode. The value is read here, at the boundary, so that frames streamed
        while the commit is in flight cannot move it.

        Returns ``_NO_ROUTE_IDENTITY_COMMIT`` when there is nothing to freeze --
        a distinct sentinel because ``None`` is itself a valid owner (no route).
        Never overrides a local onset snapshot: that is stronger evidence than
        the frame mark.
        """
        if self._input_route_identity_captured:
            return _NO_ROUTE_IDENTITY_COMMIT
        if not self._input_route_identity_stream_armed:
            return _NO_ROUTE_IDENTITY_COMMIT
        return self._input_route_identity_stream_owner

    def _apply_input_route_identity_commit(self, pending) -> None:
        """Pin ownership once the MANUAL boundary actually reached the provider.

        Only called on the success paths. A commit that never went out (no
        session, missing SDK types, a send that raised on a still-usable
        connection) leaves ownership unfrozen on purpose: that buffer will never
        produce a transcript, so a freeze left behind would answer for the NEXT
        utterance instead, and after a route change every one of those would be
        rejected as a mismatch and silently dropped.
        """
        if pending is _NO_ROUTE_IDENTITY_COMMIT:
            return
        if self._input_route_identity_captured:
            return
        self._input_route_identity = pending
        self._input_route_identity_captured = True

    def _resolve_input_route_identity_owner(self):
        """Return the route that owned the audio currently buffered, if provable.

        Ownership comes only from observed frames or a local onset snapshot,
        never from the route that happens to be active when a provider event
        lands. With no evidence at all the answer is ``None``, not a guess.

        That last case is reachable and must stay fail-closed: ``stream_audio``
        calls ``clear_audio_buffer()`` itself on detected silence, which drops
        the frame observation, so a ``speech_started`` the server had already
        emitted for the pre-clear audio can arrive afterwards -- possibly after
        the route moved on. Reading the live route there would tag the old audio
        with the new route. Nothing is dropped by refusing: every frame arms the
        observation (for Gemini too, which reaches this via ``stream_audio``
        before its provider branch), so a genuine utterance always has evidence
        by the time its onset is reported, and the buffer is only cleared here
        because there was silence rather than speech.
        """
        if self._input_route_identity_captured:
            return self._input_route_identity
        if self._input_route_identity_stream_armed:
            return self._input_route_identity_stream_owner
        return None

    def _read_input_route_identity(self):
        identity = None
        reader = getattr(self, "get_input_route_identity", None)
        if callable(reader):
            try:
                candidate = reader()
                if (
                    isinstance(candidate, tuple)
                    and len(candidate) == 3
                ):
                    identity = tuple(str(part or "") for part in candidate)
            except Exception:
                identity = None
        return identity

    def _capture_input_route_identity(self) -> None:
        """Compatibility helper that snapshots the current route immediately."""
        self._capture_input_route_identity_snapshot(
            self._read_input_route_identity()
        )

    def _capture_input_route_identity_snapshot(self, identity) -> None:
        """Commit the ingress snapshot owning the first confirmed speech frame."""
        if self._input_route_identity_captured or bool(
            getattr(self, "_audio_in_buffer", False)
        ):
            return
        self._input_route_identity = identity
        self._input_route_identity_captured = True

    def _remember_input_route_identity(self, item_id: object = None) -> None:
        """Compatibility helper for tests and non-stream ingress paths."""
        identity = self._read_input_route_identity()
        item_key = str(item_id or "").strip()
        if item_key:
            identities = self._input_route_identity_by_item
            identities.pop(item_key, None)
            identities[item_key] = identity
            while len(identities) > _INPUT_ROUTE_IDENTITY_ITEM_LIMIT:
                identities.pop(next(iter(identities)))
            return
        self._input_route_identity = identity
        self._input_route_identity_captured = True

    def _bind_input_route_identity_to_item(self, item_id: object = None) -> None:
        """Bind a server-VAD item to the captured speech owner when available."""
        item_key = str(item_id or "").strip()
        if not item_key:
            return
        # Bind the route that actually owned this audio, in decreasing order of
        # proof strength:
        #   1. the local onset snapshot, when the client gate armed one;
        #   2. otherwise the route observed on the streamed frames themselves —
        #      the server event can arrive after the active route changes, but
        #      the frames it is reporting on were still captured under a known
        #      route, and one stable value across the whole buffer proves it;
        #   3. None only when the route genuinely changed mid-buffer, so no
        #      single owner exists.
        # Pinning None whenever the local gate stayed quiet (its threshold is
        # independent of the server's) would make ordinary soft speech
        # unroutable and drop it before the takeover dispatcher ever sees it.
        # Rejecting audio that predates a route switch stays the caller's job:
        # ``handle_input_transcript`` compares this owner against the live route.
        identity = self._resolve_input_route_identity_owner()
        identities = self._input_route_identity_by_item
        identities.pop(item_key, None)
        identities[item_key] = identity
        while len(identities) > _INPUT_ROUTE_IDENTITY_ITEM_LIMIT:
            identities.pop(next(iter(identities)))
        self._input_route_identity = None
        self._input_route_identity_captured = False
        self._reset_input_route_identity_stream()

    def _take_input_route_identity(self, item_id: object = None):
        item_key = str(item_id or "").strip()
        if item_key:
            if item_key in self._input_route_identity_by_item:
                return self._input_route_identity_by_item.pop(item_key)
            if self._has_server_vad:
                # A server-VAD item has an exact owner or no provable owner. If
                # its bounded mapping was evicted, falling through would consume
                # the next utterance's global snapshot and misattribute the old
                # final. MANUAL/client-VAD providers may still attach item IDs
                # without ever emitting the event that creates this map.
                return None
        identity = self._resolve_input_route_identity_owner()
        self._input_route_identity = None
        self._input_route_identity_captured = False
        self._reset_input_route_identity_stream()
        return identity

    async def _deliver_input_transcript(self, transcript: str, *, item_id: object = None) -> None:
        identity = self._take_input_route_identity(item_id)
        routed_callback = getattr(self, "on_input_transcript_with_route", None)
        if callable(routed_callback):
            await routed_callback(
                transcript,
                source_game_route_identity=identity,
            )
            return
        if self.on_input_transcript:
            await self.on_input_transcript(transcript)

    async def connect(self, instructions: str, native_audio=True) -> None:
        """Establish WebSocket connection with the Realtime API."""
        self._native_audio = native_audio
        # Validate turn_detection_mode BEFORE any side effect (websockets.connect,
        # silence-check task, or Gemini SDK init). Applies uniformly to all providers.
        if self.turn_detection_mode not in (TurnDetectionMode.MANUAL, TurnDetectionMode.SERVER_VAD):
            raise ValueError(f"Invalid turn detection mode: {self.turn_detection_mode}")

        # 同一个实例会被跨会话复用，所以 close() 立起来的帧抄送闭锁必须在这里
        # 落下——否则重连之后 frames 总线上这个角色就再也不出现了，而且是静默
        # 的（抄送本来就是 best-effort，没人会因此报错）。
        self._frame_copies_closed = False

        # [ISSUE4c] Reset the tool-call flood window on every (re)connect. The
        # same OmniRealtimeClient instance is reused across sessions, so stale
        # timestamps from a previous connection must not carry over and make the
        # new session's first tool calls look like a burst. Cleared before the
        # provider branch so it covers both Gemini and the WS providers.
        self._recent_tool_call_times = []
        self._clear_input_route_identities()

        # Same reason, same lifetime: response ids are scoped to a connection,
        # so a provider that restarts its numbering (or simply reuses an id)
        # after a reconnect would otherwise have the new session's first turns
        # suppressed as already-billed duplicates.
        self._usage_recorded_ids = []
        # Same lifetime as the id bookkeeping above, and for the same
        # reason: a reconnect may reach a different upstream.
        self._announces_responses = False
        # Same lifetime, same reason: the quarantine is lowered only by a
        # response.created on THIS socket, so a replacement connection to a
        # never-announcing upstream would never clear it. Unreachable today
        # (connect() swaps self.ws before any of this can matter, so the old
        # response's events cannot arrive on the new socket) — reset anyway,
        # because 'connection-scoped' should be true by construction.
        self._idless_quarantine = False

        # ``close()`` releases RNNoise/soxr state. The client object is reused
        # across sessions, so recreate that session-owned processor on demand.
        if self._audio_processor is None:
            self._audio_processor = self._create_audio_processor()

        # Gemini uses google-genai SDK, not raw WebSocket
        if self._is_gemini:
            await self._connect_gemini(instructions, native_audio)
            self._response_arbiter.reset_connection_state()
            return

        # 确保开始新连接时状态完全重置
        self._silence_reset_pending = False
        self._last_silence_clear_speech_time = 0.0
        self._last_local_loud_time = 0.0
        self._client_vad_active = False
        self._client_vad_last_speech_time = 0.0
        self._speech_detect_start = 0.0
        self._rnnoise_vad_active = False
        self._user_recent_activity_time = 0.0
        self._ai_recent_activity_time = 0.0
        if self._audio_processor is not None:
            self._audio_processor.reset()
        # Flush uplink resampler FIR history so a previous session's tail
        # samples don't bleed into the new connection's first frames.
        self._clear_uplink_resampler()

        # WebSocket-based APIs (GLM, Qwen, GPT, Step, Free)
        url = f"{self.base_url}?model={self.model}" if self._model_lower != "free-model" else self.base_url
        headers = {
            "Authorization": f"Bearer {self.api_key}"
        }
        # Give proxies and cross-region free routes enough time to complete the
        # close handshake without restoring websockets' 10s default. A peer
        # that never answers is still force-aborted after this bounded wait.
        self.ws = await websockets.connect(url, additional_headers=headers, close_timeout=2.0)
        self._on_connection_attached()
        # Do not reopen the arbiter until the replacement transport exists.
        # A failed reconnect must leave the prior shutdown state intact.
        self._response_arbiter.reset_connection_state()
        # Clear fatal flag so send_event/update_session work on this new
        # connection (flag may be leftover from a previous failed session
        # when the same OmniRealtimeClient instance is reused).
        self._fatal_error_occurred = False
        capabilities = self._realtime_protocol_capabilities
        logger.info(
            "Realtime protocol profile resolved "
            "(route=%s response_start_evidence=%s)",
            capabilities.route_key,
            capabilities.response_start_evidence.value,
        )

        # 启动静默检测任务（只在启用时）
        self._last_speech_time = time.time()
        self._silence_timeout_triggered = False
        if self._silence_check_task:
            self._silence_check_task.cancel()
        # 只在启用静默超时时启动检测任务
        if self._enable_silence_timeout:
            self._silence_check_task = asyncio.create_task(self._check_silence_timeout())
        else:
            reason = "livestream模式" if self._livestream_mode else f"API类型: {self._api_type}"
            logger.info(f"静默超时检测已禁用（{reason}），不会自动关闭会话")

        # Set up default session configuration
        is_manual = self.turn_detection_mode == TurnDetectionMode.MANUAL
        # MANUAL mode: every per-provider session.update below sends
        # ``turn_detection: null``, so the provider will NOT emit
        # speech_started / speech_stopped events. _has_server_vad was
        # initialised in __init__ from provider/model heuristics
        # (defaults to True for Qwen/GLM/GPT/Step/lanlan.tech-free), but
        # those events won't arrive in MANUAL — so downstream branches in
        # stream_audio() and _check_silence_timeout() must take the
        # client-VAD path, same as Gemini / lanlan.app-free. Override the
        # flag here uniformly across all providers; the Gemini connect
        # path is unaffected because __init__ already set this to False
        # for ``_is_gemini`` clients.
        if is_manual:
            self._has_server_vad = False
        self._modalities = ["text", "audio"] if native_audio else ["text"]

        if 'glm' in self._model_lower:
            # GLM: server_vad payload in SERVER_VAD; turn_detection=null in MANUAL.
            # Best-effort — provider may reject; if so we degrade to local-suppression-only.
            glm_session = {
                "instructions": instructions,
                "modalities": self._modalities ,
                "voice": self.voice if self.voice else "tongtong",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm",
                "turn_detection": None if is_manual else {
                    "type": "server_vad",
                },
                "input_audio_noise_reduction": {
                    "type": "far_field",
                },
                "beta_fields":{
                    "chat_mode": "video_passive",
                    "auto_search": True,
                },
                "temperature": 1.0
            }
            # GLM Realtime: tools only honoured in audio mode per docs.
            # Use the flat (OpenAI-Realtime-style) schema GLM expects.
            if self.has_tools() and 'audio' in self._modalities:
                glm_session["tools"] = self._tools_for_openai_realtime()
            await self.update_session(glm_session)
        elif "qwen" in self._model_lower:
            qwen_session: Dict[str, Any] = {
                "instructions": instructions,
                "modalities": self._modalities ,
                "voice": self.voice if self.voice else "Momo",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "gummy-realtime-v1"
                },
                "turn_detection": None if is_manual else {
                    # TODO: 未来需要cover更多型号
                    "type": "semantic_vad" if "3.5" in self._model_lower else "server_vad",
                    "threshold": 0.55,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 650
                },
                "repetition_penalty": 1.2,
                "temperature": 0.7,
                # "enable_search": True,
                # "search_options": {'enable_source': True}
            }
            # Qwen-Omni-Realtime 自 2026 起支持 tools（嵌套 function 形，
            # 同 StepFun）。重要约束：tools 与 enable_search 互斥——
            # 我们注册了自定义工具时强制 enable_search=False，避免
            # session.update 被服务端拒绝。文档参见 Aliyun client-events
            # 章节 "工具调用（tools）和联网搜索（enable_search）不兼容"。
            if self.has_tools():
                qwen_session["tools"] = self._tools_for_qwen()
                qwen_session["enable_search"] = False
            await self.update_session(qwen_session)
        elif "gpt" in self._model_lower:
            gpt_session = {
                "type": "realtime",
                "model": self.model,
                "instructions": instructions,
                "output_modalities": ['audio'] if 'audio' in self._modalities else ['text'],
                "audio": {
                    "input": {
                        # OpenAI Realtime PCM 输入只支持 24kHz；显式声明以匹配
                        # 我们 _resample_uplink 上采后的实际采样率。复用
                        # _uplink_sample_rate（此分支恒为 24000）作单一数据源，
                        # 避免声明与实际两处来源漂移。
                        "format": {"type": "audio/pcm", "rate": self._uplink_sample_rate},
                        "transcription": {"model": "gpt-4o-mini-transcribe"},
                        "turn_detection": None if is_manual else {
                            "type": "semantic_vad",
                            "eagerness": "auto",
                            "create_response": True,
                            "interrupt_response": True
                        },
                    },
                    "output": {
                        "voice": self.voice if self.voice else "marin",
                        "speed": 1.0
                    }
                }
            }
            if self.has_tools():
                gpt_session["tools"] = self._tools_for_openai_realtime()
                gpt_session["tool_choice"] = "auto"
            await self.update_session(gpt_session)
        elif "step" in self._model_lower:
            default_voice = get_stepfun_tts_default_voice('step')
            step_session = {
                "instructions": instructions,
                "modalities": ['text', 'audio'], # Step API只支持这一个模式
                "voice": self.voice if self.voice else default_voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": None if is_manual else {
                    "type": "server_vad"
                },
            }
            step_tools: List[Dict[str, Any]] = []
            if self.has_tools():
                step_tools.extend(self._tools_for_step())
            step_session["tools"] = step_tools
            await self.update_session(step_session)
        elif "free" in self._model_lower:
            # NOTE: lanlan.tech (China free) backs onto StepFun and
            # supports the StepFun custom-function protocol — the
            # server-side tool stripping the user mentioned will be
            # lifted, after which our tools propagate naturally.
            # lanlan.app (international free) backs onto Vertex AI
            # Live; that path is currently TODO (no client→server
            # tools propagation confirmed). Tools below match the
            # StepFun shape and become a no-op on lanlan.app until
            # the proxy supports them.
            #
            # MANUAL mode: both proxies receive ``turn_detection: null``
            # via the StepFun-shape websocket session config. lanlan.tech
            # (StepFun proxy) honours it natively; lanlan.app (Vertex
            # Gemini proxy) translates the disabled-VAD intent on the
            # server side, since the proxy already maps StepFun-shape
            # client events to Vertex Live (see _has_server_vad gate
            # at __init__ — lanlan.app+free is already treated as
            # client-side VAD only).
            default_voice = get_stepfun_tts_default_voice('free')
            free_session = {
                "instructions": instructions,
                "modalities": ['text', 'audio'],
                "voice": self.voice if self.voice else default_voice,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": None if is_manual else {
                    "type": "server_vad"
                },
            }
            # 海外免费（lanlan.app，Gemini 代理）建 session 时一次性指定
            # language_code，与 TTS server 路对偶；lanlan.tech（StepFun）不发，
            # 沿用其自动识别 / voice_label 语义。
            if 'lanlan.app' in (self.base_url or ''):
                from utils.language_utils import get_tts_language_code
                free_session["language_code"] = get_tts_language_code()
            free_tools: List[Dict[str, Any]] = []
            if self.has_tools():
                free_tools.extend(self._tools_for_step())
            free_session["tools"] = free_tools
            await self.update_session(free_session)
        elif "grok" in self._model_lower:
            # xAI Grok Voice：OpenAI Realtime 1.0 风格的扁平 schema。
            # 内置 voice 见 GET /v1/tts/voices（eve/ara/leo/rex/sal），默认 eve。
            # tools 走 OpenAI 兼容的 function 协议（response.function_call_arguments.done）。
            grok_session = {
                "instructions": instructions,
                "modalities": self._modalities,
                "voice": self.voice if self.voice else "eve",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": None if is_manual else {
                    "type": "server_vad"
                },
            }
            if self.has_tools():
                grok_session["tools"] = self._tools_for_openai_realtime()
                grok_session["tool_choice"] = "auto"
            await self.update_session(grok_session)
        else:
            raise ValueError(f"Invalid model: {self.model}")
        self.instructions = instructions

    @staticmethod
    def _try_shrink_image_payload(event: dict, payload: str) -> Optional[str]:
        """Re-compress an oversized image payload at lower JPEG quality.

        Looks for a base64 image blob in the event (``image``,
        ``video_frame``, or ``image_url`` fields), decodes it, re-encodes
        at progressively lower quality, and returns a new JSON payload that
        fits under ``_WS_FRAME_LIMIT``.  Returns *None* if the frame
        cannot be shrunk (non-image event, or still too big at minimum
        quality).
        """
        from io import BytesIO
        from PIL import Image as PILImage

        limit = _TransportMixin._WS_FRAME_LIMIT

        # Locate the base64 blob and a setter to write it back
        b64_data: Optional[str] = None
        prefix = ""

        etype = event.get("type", "")
        if "image" in etype and "image" in event:
            # input_image_buffer.append  →  event["image"]
            b64_data = event.get("image")
        elif "video_frame" in etype and "video_frame" in event:
            # input_audio_buffer.append_video_frame  →  event["video_frame"]
            b64_data = event.get("video_frame")
        elif etype == "conversation.item.create":
            # GPT path: content[i].image_url = "data:image/jpeg;base64,<b64>".
            # A multimodal ASR turn carries the sampled first/middle/last frames
            # in ONE item, so every image part has to be recompressed against
            # the same aggregate budget — shrinking only content[0] leaves the
            # item over the limit and the whole turn gets dropped.
            image_parts = []
            try:
                for part in event["item"]["content"]:
                    url = part.get("image_url") if isinstance(part, dict) else None
                    if isinstance(url, str) and url.startswith("data:image/"):
                        image_parts.append(part)
            except (KeyError, TypeError):
                image_parts = []
            if image_parts:
                return _TransportMixin._shrink_multi_image_item(
                    event,
                    payload,
                    image_parts,
                )

        if not b64_data:
            logger.warning(
                "⚠️ 丢弃超大帧 type=%s size=%d bytes (非图片，无法压缩)",
                etype, len(payload),
            )
            return None

        try:
            raw = base64.b64decode(b64_data)
            img = PILImage.open(BytesIO(raw))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")

            for quality in (50, 35, 20):
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                new_b64 = base64.b64encode(buf.getvalue()).decode()

                # Write back into the event dict (mutates in place)
                if "image" in etype and "image" in event:
                    event["image"] = new_b64
                elif "video_frame" in etype and "video_frame" in event:
                    event["video_frame"] = new_b64
                elif prefix:
                    event["item"]["content"][0]["image_url"] = prefix + new_b64

                new_payload = json.dumps(event)
                if len(new_payload) <= limit:
                    logger.info(
                        "🗜️ 图片帧重压缩成功 q=%d: %d → %d bytes",
                        quality, len(payload), len(new_payload),
                    )
                    return new_payload

            logger.warning(
                "⚠️ 丢弃超大图片帧 type=%s (q=20 仍 %d bytes > %d 上限)",
                etype, len(new_payload), limit,
            )
            return None
        except Exception as e:
            logger.warning("⚠️ 图片重压缩失败 type=%s: %s — 丢弃帧", etype, e)
            return None

    @staticmethod
    def _shrink_multi_image_item(
        event: dict,
        payload: str,
        image_parts: list,
    ) -> Optional[str]:
        """Recompress every image part of one item against the shared budget."""

        from io import BytesIO
        from PIL import Image as PILImage

        limit = _TransportMixin._WS_FRAME_LIMIT
        decoded = []
        for part in image_parts:
            prefix, b64_data = part["image_url"].split(",", 1)
            try:
                img = PILImage.open(BytesIO(base64.b64decode(b64_data)))
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGB")
            except Exception as exc:
                logger.warning("⚠️ 多图 item 解码失败，丢弃帧: %s", exc)
                return None
            decoded.append((part, prefix + ",", img))

        new_payload = payload
        for quality in (50, 35, 20):
            for part, prefix, img in decoded:
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True)
                part["image_url"] = prefix + base64.b64encode(
                    buf.getvalue()
                ).decode()
            new_payload = json.dumps(event)
            if len(new_payload) <= limit:
                logger.info(
                    "🗜️ 多图 item 重压缩成功 q=%d images=%d: %d → %d bytes",
                    quality,
                    len(decoded),
                    len(payload),
                    len(new_payload),
                )
                return new_payload

        # 压到底仍然超限：与其整条 item 被丢掉（那会让本轮既没有图也没有
        # transcript 进 provider 历史），不如按"多余丢弃"逐张摘掉最旧的那些。
        # 最新那张必须留 —— 回合身份和 TTL 都以它为准。
        content = event["item"]["content"]
        while len(decoded) > 1:
            dropped_part, _prefix, _img = decoded.pop(0)
            try:
                content.remove(dropped_part)
            except ValueError:
                break
            new_payload = json.dumps(event)
            logger.warning(
                "⚠️ 多图 item 仍超限，丢弃最旧的一帧（剩 %d 张）",
                len(decoded),
            )
            if len(new_payload) <= limit:
                return new_payload
        logger.warning(
            "⚠️ 丢弃超大 item：单张图 q=20 仍 %d bytes > %d 上限",
            len(new_payload),
            limit,
        )
        return None

    async def send_event(
        self,
        event,
        *,
        raise_on_oversize: bool = False,
        expected_visual_mode: VisualDeliveryMode | str | None = None,
        callback_owned_visual: bool = False,
        send_guard: Callable[[], bool] | None = None,
        pre_send: Callable[[dict], None] | None = None,
    ) -> bool:
        if send_guard is not None and not send_guard():
            raise ConnectionError("realtime event owner is no longer current")
        # 检查是否已发生致命错误，直接跳过发送
        if self._fatal_error_occurred:
            return False

        # Gemini 不使用 WebSocket 风格的事件发送
        # 而是使用 session.send_client_content() 或 session.send_realtime_input()
        if self._is_gemini:
            # Gemini 的事件通过专用方法处理，这里直接返回
            # 对于 session.update / conversation.item.create 等事件，Gemini 不支持
            logger.debug(f"Gemini mode: skipping WebSocket event {event.get('type', 'unknown')}")
            return False

        # Backpressure: 检查是否处于节流状态
        if self._is_throttled:
            if time.time() < self._throttle_until:
                # 仍在节流期，丢弃音频帧以减轻服务器压力
                if event.get("type") == "input_audio_buffer.append":
                    return False  # 丢弃音频帧
            else:
                # 节流期结束，恢复正常发送
                self._is_throttled = False
                logger.info("🔄 Backpressure throttle ended, resuming sends")

        # 检查websocket是否有效
        if not self.ws:
            return False

        # Use setdefault so callers that explicitly stamp an event_id
        # (e.g. proactive inject paths matching server-side
        # ``error.event_id`` echoes for rejection callbacks) keep theirs.
        # Otherwise fall back to the legacy timestamp-based id.
        event.setdefault('event_id', "event_" + str(int(time.time() * 1000)))
        async with self._send_semaphore:  # 限制并发发送数量
            try:
                if send_guard is not None and not send_guard():
                    raise _RealtimeEventOwnerRetired(
                        "realtime event owner is no longer current"
                    )
                if not self.ws:
                    return False
                if pre_send is not None:
                    # 临界区之内、序列化**之前**的最后一次就地改写机会。等信号量
                    # 是真实让出点，调用方在此之前做的任何判据都可能在这段等待里
                    # 翻转；而 payload 是下一行才生成的，所以在这里改 event 就能
                    # 被带上，不需要重新序列化。
                    pre_send(event)
                payload = json.dumps(event)
                # Guard: Qwen/GLM/Step servers enforce 256KB max frame; for
                # oversized image payloads, try to re-compress the JPEG at
                # lower quality before dropping. PIL decode + JPEG re-encode
                # is CPU-heavy (50-150ms on a 4K screenshot), so off-load to
                # a thread to keep the event loop responsive.
                if len(payload) > OMNI_WS_FRAME_LIMIT_BYTES:
                    payload = await asyncio.to_thread(
                        self._try_shrink_image_payload, event, payload
                    )
                    if payload is None:
                        if raise_on_oversize:
                            raise RealtimeImagePayloadTooLargeError(
                                "image payload exceeds realtime WebSocket frame limit"
                            )
                        return False
                # 发送前的两道复查，判据不同、都要跑：send_guard 管「这条连接还
                # 是不是当前所有者」，raw-vision fence 管「这一帧还符不符合当前
                # 路由模式」。等信号量、重压缩超大图都是让出点，两者都可能在这
                # 期间翻转。
                #
                # 注意 expected_visual_mode 是**送出时的那个模式**，不是写死的
                # NATIVE：fence 要拦的是"帧在准入之后跨过了模式边界"，不是"只准
                # 在 native 模式下发原始帧"。描述模式下主动搭话的那张一次性 cue
                # 图就是照原样送的（见 stream_image 里 cache_latest=False 的分支），
                # 写死 NATIVE 会把它一并拦掉。
                if send_guard is not None and not send_guard():
                    raise _RealtimeEventOwnerRetired(
                        "realtime event owner is no longer current"
                    )
                if expected_visual_mode is not None:
                    expected_mode = VisualDeliveryMode(expected_visual_mode)
                    current_mode = getattr(
                        self,
                        "_visual_delivery_mode",
                        VisualDeliveryMode.NATIVE,
                    )
                    if current_mode != expected_mode or (
                        expected_mode == VisualDeliveryMode.NATIVE
                        and getattr(self, "_raw_visual_delivery_blocked", False)
                        and not callback_owned_visual
                    ):
                        return False
                transport = self.ws
                if not transport:
                    return False
                await transport.send(payload)
                return True
            except _RealtimeEventOwnerRetired:
                raise
            except Exception as e:
                if send_guard is not None and not send_guard():
                    logger.info(
                        "Ignoring send failure from a retired realtime connection"
                    )
                    return False
                error_msg = str(e)
                # ── Fatal WebSocket errors ────────────────────────────
                # 1009 (message too big) / 1006 (abnormal close) /
                # 1011 (internal error) / Response timeout
                # → mark fatal, fire error callback, schedule close,
                #   and *re-raise* so callers (connect, update_session)
                #   see the failure instead of assuming success.
                is_frame_error = '1009' in error_msg or '1006' in error_msg
                is_server_error = 'Response timeout' in error_msg or '1011' in error_msg
                if is_frame_error or is_server_error:
                    if not self._fatal_error_occurred:
                        self._fatal_error_occurred = True
                        self.ws = None
                        code = "WS_FRAME_ERROR" if is_frame_error else "RESPONSE_TIMEOUT"
                        logger.error("💥 WebSocket 致命错误 (%s)，停止发送: %s", code, error_msg)
                        if self.on_connection_error:
                            self._fire_task(self.on_connection_error(json.dumps({"code": code})))
                        self._fire_task(self.close())
                    raise
                if '1000' not in error_msg:
                    logger.warning(f"⚠️ 发送 {event.get('type', '未知')} 事件失败: {error_msg}")

                raise

    async def update_session(self, config: Dict[str, Any]) -> None:
        """Update session configuration."""
        # Mirror the chat-completion chokepoint: catch any unrendered
        # {placeholder} before the system instruction (nested at provider-
        # specific paths inside `config`) is shipped over the wire. See
        # utils/llm_prompt_leak_check.py for rationale.
        try:
            from utils import llm_prompt_leak_check
            llm_prompt_leak_check.check_dict_strings_for_leaks(
                config, context="OmniRealtimeClient.update_session"
            )
        except AssertionError:
            raise
        except Exception:
            pass
        event = {
            "type": "session.update",
            "session": config
        }
        await self.send_event(event)

    def expect_session_update_ack(self, instructions: str) -> asyncio.Future:
        """Arm an exact waiter for a future ``session.updated`` snapshot."""

        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._session_update_ack_waiters.append((str(instructions), waiter))
        return waiter

    def discard_session_update_ack(self, waiter: asyncio.Future) -> None:
        """Remove a delivery-barrier waiter without affecting other updates."""

        self._session_update_ack_waiters = [
            (expected, pending)
            for expected, pending in self._session_update_ack_waiters
            if pending is not waiter
        ]
        if not waiter.done():
            waiter.cancel()

    def _notify_session_updated(self, event: Dict[str, Any]) -> None:
        """Settle waiters whose exact instructions snapshot was accepted."""

        session = event.get("session")
        if not isinstance(session, dict):
            return
        accepted_instructions = session.get("instructions")
        if accepted_instructions is None:
            return
        accepted_text = str(accepted_instructions)
        remaining = []
        for expected, waiter in self._session_update_ack_waiters:
            if waiter.done():
                continue
            if expected == accepted_text:
                waiter.set_result(None)
            else:
                remaining.append((expected, waiter))
        self._session_update_ack_waiters = remaining

    def _cancel_session_update_ack_waiters(self) -> None:
        waiters, self._session_update_ack_waiters = (
            self._session_update_ack_waiters,
            [],
        )
        for _expected, waiter in waiters:
            if not waiter.done():
                waiter.cancel()

    async def stream_audio(
        self,
        audio_chunk: bytes,
        *,
        captured_at: float | None = None,
    ) -> None:
        """Stream raw audio data to the API.

        Supports two input modes:
        - 48kHz from PC: Apply RNNoise then downsample to 16kHz
        - 16kHz from mobile: Pass through directly (no RNNoise)
        """
        # 检查是否已发生致命错误，如果是则直接返回
        if self._fatal_error_occurred:
            return

        audio_timeline_at = (
            float(captured_at)
            if isinstance(captured_at, (int, float)) and captured_at > 0
            else time.time()
        )

        # 本地音量判定：用原始输入做 RMS，避免 VAD 延迟时误清 buffer
        ingress_route_identity = self._read_input_route_identity()
        # Observe ownership on every frame, not only on frames the local onset
        # gate accepts: server VAD may commit an utterance the gate never heard.
        self._note_input_route_identity_frame(ingress_route_identity)
        raw_samples = np.frombuffer(audio_chunk, dtype=np.int16)
        raw_loud = False
        if len(raw_samples) > 0:
            local_rms = np.sqrt(np.mean(raw_samples.astype(np.float32) ** 2))
            raw_loud = local_rms > self._client_vad_threshold
        if raw_loud:
            # Publish user engagement before waiting on the shared admission
            # boundary. A proactive callback holding that lock must see speech
            # that arrived during its media transaction and defer its response;
            # all VAD state transitions and provider writes remain serialized
            # below.
            self._last_local_loud_time = audio_timeline_at
            self._user_recent_activity_time = time.time()

        # Detect input sample rate based on chunk size
        # 48kHz: 480 samples (10ms) = 960 bytes
        # 16kHz: 512 samples (~32ms) = 1024 bytes
        num_samples = len(audio_chunk) // 2  # 16-bit = 2 bytes per sample
        is_48khz = (num_samples == 480)  # RNNoise frame size


        use_rnnoise_path = is_48khz and self._audio_processor is not None
        # Apply RNNoise noise reduction only for 48kHz input (PC)
        if use_rnnoise_path:
            # Use async wrapper to avoid blocking main loop
            audio_chunk = await self.process_audio_chunk_async(audio_chunk)

            # Skip if RNNoise is buffering (returns empty)
            if len(audio_chunk) == 0:
                return

        audio_processor = self._audio_processor
        use_rnnoise_path = use_rnnoise_path and audio_processor is not None
        _rnnoise_vad_live = (
            use_rnnoise_path
            and audio_processor.noise_reduce_enabled
            and audio_processor._denoiser is not None
        )

        # Serialize at the actual user-audio admission boundary, not inside the
        # provider receive loop. A callback that already owns the boundary can
        # finish its native image+text transaction before new PCM is admitted;
        # receive-side audio/done/error events remain continuously drainable.
        async with self._ensure_turn_admission_lock():
            if self._fatal_error_occurred:
                return
            admitted_at = time.time()

            # Unified VAD update (priority: server VAD > RNNoise > RMS).
            if (
                self._client_vad_active
                and audio_timeline_at - self._client_vad_last_speech_time
                > self._client_vad_grace_period
            ):
                self._client_vad_active = False
            self._rnnoise_vad_active = _rnnoise_vad_live
            # Local onset evidence for route ownership, deliberately OUTSIDE the
            # `not self._has_server_vad` guard below: server VAD can commit an
            # utterance this local gate never accepted, and the snapshot is the
            # stronger evidence either way. `ingress_route_identity` was read
            # before the first await, so what moves here is only when it is
            # stored, not which route it names.
            if _rnnoise_vad_live:
                if audio_processor.speech_probability > 0.4:
                    self._capture_input_route_identity_snapshot(
                        ingress_route_identity
                    )
            elif raw_loud:
                # RMS is the only local onset signal for 16 kHz/mobile input or
                # when RNNoise is unavailable. Commit the pre-await ingress
                # owner, never the route that happens to be active afterwards.
                self._capture_input_route_identity_snapshot(ingress_route_identity)
            if not self._has_server_vad:
                if _rnnoise_vad_live:
                    if audio_processor.speech_probability > 0.4:
                        self._user_recent_activity_time = admitted_at
                        if self._speech_detect_start == 0.0:
                            self._speech_detect_start = audio_timeline_at
                        elif (
                            audio_timeline_at - self._speech_detect_start
                            >= self._speech_sustain_threshold
                        ):
                            self._client_vad_last_speech_time = audio_timeline_at
                            self._client_vad_active = True
                    else:
                        self._speech_detect_start = 0.0
                elif raw_loud:
                    self._client_vad_last_speech_time = audio_timeline_at
                    self._client_vad_active = True

            # 静音清 buffer：有 RNNoise 以 RNNoise 为准，否则 VAD + 连续本地静音。
            if self._should_clear_audio_buffer_on_silence(
                audio_timeline_at,
                use_rnnoise_path,
            ):
                self._silence_reset_pending = False
                await self.clear_audio_buffer()

            # Gemini uses different API (16kHz, no uplink resample needed)
            if self._is_gemini:
                await self._stream_audio_gemini(audio_chunk)
                return

            # By this point audio_chunk is always 16kHz (RNNoise-downsampled,
            # mobile-native, or hot-swap-cache replay). Upsample to the provider
            # uplink rate as the very last step (24kHz for OpenAI; no-op others).
            audio_chunk = self._resample_uplink(audio_chunk)
            if not audio_chunk:
                return  # resampler still buffering — nothing to send this frame

            audio_b64 = base64.b64encode(audio_chunk).decode()

            append_event = {
                "type": "input_audio_buffer.append",
                "audio": audio_b64
            }
            await self.send_event(append_event)

    async def _analyze_image_with_vision_model(
        self,
        image_b64: str,
        *,
        update_turn_state: bool = True,
    ) -> str:
        """Use VISION_MODEL to analyze an image and return its description.

        Callback-owned images pass ``update_turn_state=False`` because their
        description is delivered in the callback's exact arbiter ticket. They
        must not overwrite or consume the ambient screen/camera snapshot state.
        """
        try:
            # 使用统一的视觉分析函数
            from utils.screenshot_utils import analyze_image_with_vision_model

            description = await analyze_image_with_vision_model(
                image_b64=image_b64,
                max_completion_tokens=VISION_ANALYSIS_MAX_TOKENS
            )

            if description:
                if update_turn_state:
                    self._image_description = (
                        f"[实时屏幕截图或相机画面]: {description}"
                    )
                    self._image_recognized_this_turn = True
                logger.info("✅ Image analysis complete.")
                return description
            else:
                logger.warning("VISION_MODEL not configured or analysis failed")
                if update_turn_state:
                    self._image_description = _IMAGE_ANALYSIS_PENDING_DESCRIPTION
                    self._image_recognized_this_turn = False
                    self._latest_image_b64 = None
                    self._proactive_image_consumed = True
                return ""

        except Exception as e:
            logger.error(f"Error analyzing image with vision model: {e}")
            if update_turn_state:
                self._image_recognized_this_turn = False
                self._image_description = _IMAGE_ANALYSIS_PENDING_DESCRIPTION
                self._latest_image_b64 = None
                self._proactive_image_consumed = True
            # 检测内容审查错误并发送中文提示到前端（不关闭session）
            error_str = str(e)
            if 'censorship' in error_str:
                if self.on_status_message:
                    await self.on_status_message(json.dumps({"code": "IMAGE_BLOCKED"}))
                return ""
            if not update_turn_state:
                # Callback-owned one-shot images are retriable work. Preserve
                # transient provider/time-out failures as exceptions so the
                # callback remains queued with its image; only censorship or an
                # explicit empty model result is terminal ``analysis_empty``.
                raise
            return ""
        finally:
            if update_turn_state:
                self._image_being_analyzed = False

    def block_raw_visual_delivery(self) -> None:
        """Fail closed for raw frames without changing the microphone route."""

        self._raw_visual_delivery_blocked = True

    def allow_raw_visual_delivery(self) -> None:
        """Release a temporary raw-frame fence after native routing settles."""

        self._raw_visual_delivery_blocked = False

    def set_visual_delivery_mode(
        self,
        mode: VisualDeliveryMode | str,
    ) -> None:
        """Switch image delivery without changing provider capability flags."""

        selected = VisualDeliveryMode(mode)
        if selected == VisualDeliveryMode.EXTERNAL_DESCRIPTION:
            # Arm the raw-frame fence before turn/cache cleanup. Independent
            # ASR can keep its microphone route even if that cleanup fails.
            self.block_raw_visual_delivery()
        previous = getattr(
            self,
            "_visual_delivery_mode",
            VisualDeliveryMode.NATIVE,
        )
        if selected == previous:
            return
        self._visual_delivery_mode = selected
        self._visual_delivery_epoch = getattr(self, "_visual_delivery_epoch", 0) + 1
        # A cached frame from the old routing contract must never cross the
        # mode boundary and later appear as context for a different ASR turn.
        self._latest_image_b64 = None
        self._latest_image_captured_at = 0.0
        self._latest_image_source = "unknown"
        self._latest_image_request_id = None
        self._proactive_image_consumed = True
        if (
            previous == VisualDeliveryMode.EXTERNAL_DESCRIPTION
            and selected == VisualDeliveryMode.NATIVE
        ):
            self._raw_visual_delivery_blocked = False

    def stage_multimodal_frame(
        self,
        image_b64: str,
        *,
        source: str = "unknown",
        request_id: str | None = None,
        captured_at: float | None = None,
    ) -> ImageStageResult:
        """Cache a raw frame without sending it or invoking image analysis."""

        from utils.screenshot_utils import MAX_BASE64_SIZE

        if (
            not isinstance(image_b64, str)
            or not image_b64
            or len(image_b64) > MAX_BASE64_SIZE
        ):
            return ImageStageResult(
                accepted=False,
                mode="staged",
                generation=getattr(self, "_latest_image_generation", 0),
                rejection_reason=(
                    "payload_too_large"
                    if isinstance(image_b64, str)
                    and len(image_b64) > MAX_BASE64_SIZE
                    else "invalid_payload"
                ),
            )

        stable_source = str(source or "unknown").strip() or "unknown"
        stable_request_id = (
            str(request_id).strip() if request_id is not None else None
        )
        has_ingress_order = isinstance(captured_at, (int, float))
        frame_captured_at = (
            float(captured_at) if has_ingress_order else time.monotonic()
        )
        if has_ingress_order and frame_captured_at <= getattr(
            self,
            "_latest_image_captured_at",
            0.0,
        ):
            return ImageStageResult(
                accepted=False,
                mode="staged",
                generation=getattr(self, "_latest_image_generation", 0),
                rejection_reason="stale_frame",
            )

        self._latest_image_generation = (
            getattr(self, "_latest_image_generation", 0) + 1
        )
        generation = self._latest_image_generation
        self._latest_image_b64 = image_b64
        self._latest_image_captured_at = frame_captured_at
        self._latest_image_source = stable_source
        self._latest_image_request_id = stable_request_id
        self._proactive_image_consumed = False
        return ImageStageResult(
            accepted=True,
            mode="staged",
            generation=generation,
        )

    @staticmethod
    def _frame_bus_wall_clock(captured_at: Optional[float]) -> float:
        """Translate a monotonic capture instant into a wall-clock timestamp.

        Live frames are stamped with ``time.monotonic()`` on the way in
        (``_visual_input_ingress_time``), which is process-local and means
        nothing to a plugin reading the record in another process -- and the
        frames store indexes and sorts that field alongside records stamped
        with ``time.time()``. Convert here rather than forward a number from a
        clock the reader cannot interpret.

        An implausible age falls back to now, because a caller passing epoch
        seconds would otherwise land the record decades in the future, which
        sorts far worse than being a few milliseconds late.
        """

        now = time.time()
        if not isinstance(captured_at, (int, float)):
            return now
        age = time.monotonic() - float(captured_at)
        if not 0.0 <= age <= _FRAME_BUS_MAX_CAPTURE_AGE_SECONDS:
            return now
        return now - age

    @staticmethod
    def _delivered_frame_from_event(
        event: Dict[str, Any],
    ) -> Optional[tuple[str, str]]:
        """Read the image bytes an outgoing append event actually carries.

        Deliberately reads the event and not the caller's ``image_b64``:
        ``send_event`` shrinks an oversized frame by rewriting the very fields
        below IN PLACE, so after a successful send the parameter still holds
        the larger, discarded picture while the event holds the one the
        provider received. Field selection mirrors
        ``_try_shrink_image_payload`` -- that is the function doing the
        rewriting, so the two have to agree on where the bytes live.

        Returns ``(base64, mime)``, or None for an event carrying no image.
        """

        if not isinstance(event, dict):
            return None
        etype = str(event.get("type", ""))
        if "image" in etype and isinstance(event.get("image"), str):
            return event["image"], "image/jpeg"
        if "video_frame" in etype and isinstance(event.get("video_frame"), str):
            return event["video_frame"], "image/jpeg"
        try:
            parts = event["item"]["content"]
        except (KeyError, TypeError):
            return None
        if not isinstance(parts, list):
            return None
        # Last image part, not the first: a multi-image item that could not be
        # shrunk far enough drops its OLDEST parts and keeps the newest, which
        # is the frame this delivery is about.
        for part in reversed(parts):
            url = part.get("image_url") if isinstance(part, dict) else None
            if isinstance(url, str) and url.startswith("data:image/"):
                header, _, data = url.partition(",")
                mime = header[len("data:"):].split(";", 1)[0] or "image/jpeg"
                return data, mime
        return None

    def _publish_provider_frame_from_event(
        self,
        event: Dict[str, Any],
        *,
        source: str,
        captured_at: Optional[float],
    ) -> None:
        """Publish the frame one outgoing append event actually carried."""

        delivered = self._delivered_frame_from_event(event)
        if delivered is None:
            return
        image_b64, mime = delivered
        self._publish_provider_frame(
            image_b64,
            source=source,
            captured_at=captured_at,
            mime=mime,
        )

    def _publish_provider_frame(
        self,
        image_b64: str,
        *,
        source: str,
        captured_at: Optional[float],
        mime: str = "image/jpeg",
    ) -> None:
        """Copy a frame the provider just accepted onto the plugin bus.

        Only ever called where the frame was genuinely delivered. A frame the
        NATIVE_IMAGE_MIN_INTERVAL throttle or the delivery-mode fence dropped
        was never sent, so it must never reach the bus -- that is what keeps
        plugins observers of what the model saw rather than a second camera.

        Fire-and-forget by construction, and the scheduling is guarded too:
        copying a frame is never a reason to slow down or fail a send that
        already succeeded. The publish is not guaranteed to stay on this loop
        either (``publish_session_event_threadsafe`` hands off to the bridge's
        owner loop when that is a different one), and a stalled bridge must
        not be able to stall the session.
        """

        if not image_b64:
            return
        try:
            # Sample the turn identity HERE, not inside the task: the copy runs
            # on a later loop iteration, and the receive loop can rotate the
            # speech id in that gap. Then the frame would be filed under the
            # turn that followed the one it was actually sent in.
            self._fire_frame_copy(
                self._publish_provider_frame_task(
                    image_b64,
                    source=str(source or "unknown"),
                    captured_at=self._frame_bus_wall_clock(captured_at),
                    turn_id=self._read_host_turn_id(),
                    # Ambient frames are ordered by this counter, but a
                    # one-shot cue image (cache_latest=False) never advances
                    # it, so two records can legitimately share a generation.
                    # Plugin-side dedup is documented on the record id, which
                    # is unique per publish; this only orders the ambient
                    # stream.
                    generation=getattr(self, "_latest_image_generation", 0),
                    mime=mime,
                )
            )
        except Exception as exc:
            logger.debug("frame bus publish not scheduled: %s", exc)

    async def _publish_provider_frame_task(
        self,
        image_b64: str,
        *,
        source: str,
        captured_at: float,
        turn_id: Optional[str],
        generation: int,
        mime: str,
    ) -> None:
        """Hand one delivered frame to the session event bus. Never raises."""

        try:
            from main_logic.agent_event_bus import (
                publish_provider_frame_observed_best_effort,
            )

            await publish_provider_frame_observed_best_effort(
                # Read rather than hardcode None: the realtime client is
                # constructed without a character name today (see
                # core/lifecycle), so the record simply omits the field.
                getattr(self, "lanlan_name", None),
                image_base64=image_b64,
                source=source,
                captured_at=captured_at,
                turn_id=turn_id,
                generation=generation,
                mime=mime,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("frame bus publish failed: %s", exc)

    async def stream_image(
        self,
        image_b64: str,
        *,
        source: str = "unknown",
        request_id: str | None = None,
        captured_at: float | None = None,
        bypass_rate_limit: bool = False,
        cache_latest: bool = True,
        event_id: str | None = None,
        on_rejected: Optional[Callable[[str], None]] = None,
    ) -> ImageStageResult:
        """Stream raw image data to the API.

        ``bypass_rate_limit=True`` skips the native-vision frame-rate throttle
        for a deliberate single cue image (e.g. a proactive callback's
        screenshot) so it isn't silently dropped just because a high-frequency
        screen/camera frame was streamed within NATIVE_IMAGE_MIN_INTERVAL
        (Codex P2). It's one intentional image, not a stream, so it won't flood.

        WebSocket-native callback images may pass ``on_rejected`` to correlate
        a later provider ``error.event_id`` with the callback delivery that
        owns the image. The handler is registered before send so an immediate
        asynchronous rejection cannot outrun it.

        ``source`` is accepted for signature parity with the text client,
        which charges staged frames to a per-source quota. Realtime sends
        immediately and stages nothing, so there is no quota to charge here --
        but the host injects images through a duck-typed ``stream_image`` and
        cannot know which client it holds.

        ``cache_latest=False`` sends an already-cached proactive snapshot
        without treating that resend as a newly captured frame generation.
        For a non-native callback image it returns a structured result carrying
        the callback-owned VISION_MODEL description, without changing ambient
        frame state.
        """
        rejection_event_id: str | None = None
        try:
            callback_owned_raw_image = bool(
                not cache_latest and str(source or "").strip() == "callback"
            )
            # 主动搭话那一张 cue 图。与上面同款谓词并列，别只判 not cache_latest
            # —— 那覆盖的是所有一次性图（callback media、插件 read 图都算）。
            proactive_cue_image = bool(
                not cache_latest and str(source or "").strip() == "proactive"
            )
            delivery_mode = getattr(
                self,
                "_visual_delivery_mode",
                VisualDeliveryMode.NATIVE,
            )
            if (
                getattr(self, "_raw_visual_delivery_blocked", False)
                and delivery_mode != VisualDeliveryMode.EXTERNAL_DESCRIPTION
                and not callback_owned_raw_image
            ):
                return ImageStageResult(
                    accepted=False,
                    mode=delivery_mode.value,
                    generation=getattr(self, "_latest_image_generation", 0),
                    rejection_reason="raw_visual_delivery_blocked",
                )
            if delivery_mode == VisualDeliveryMode.EXTERNAL_DESCRIPTION:
                if proactive_cue_image:
                    # 一次性 cue 图（主动搭话的那张截图），不是环境帧。
                    #
                    # 这里**不**返回 handoff_required：多模态 handoff 是给独立 ASR
                    # 回合准备的，主动搭话没有那样一个回合可以交接，调用方
                    # (prompt_ephemeral) 拿不到东西就会把快照判成终局失败、
                    # _mark_snapshot_consumed_if_current() 永久退休它——于是描述
                    # 模式下每一次主动搭话都静默丢掉自己的视觉上下文。
                    #
                    # 直接落到下面的原生发送路径把图原样送出去，调用方补一句简单
                    # 引导即可，不再为它单独跑一次 VISION_MODEL 注释。
                    # 原始帧闸门在上面已经明确豁免了描述模式，所以这里不越权。
                    # 收不了原始图的 provider（标准 StepFun，_supports_native_image
                    # 为假）会继续走下面那条 VISION_MODEL 分支——对它们那是唯一通道。
                    #
                    # ⚠️ 判据必须带 source == "proactive"，只放行主动搭话那一张 cue
                    # 图。一开始我只判 not cache_latest，那覆盖的是**所有**一次性
                    # 图：callback media（source="callback"）和插件 read 图
                    # （source="plugin"）也跟着上了线。后果不止是越权——仓库里另外
                    # 三个 cache_latest=False 的调用方各自维护着一套"这次有没有走
                    # 原始 WS 投递"的判据，全都写死 _visual_delivery_mode ==
                    # "native"：
                    #   proactive.py 的 attempted_websocket_native_delivery 和
                    #   websocket_native_delivery —— 它们控制着写到一半抛异常时
                    #   要不要退掉 session（字节可能已经过界），在描述模式下会
                    #   静默失效；
                    #   passive callback 那条"必须以原始媒体到达最终 VLM"的约定，
                    #   原本正是靠这里回 handoff_required 落实的。
                    # 收窄到 proactive 之后这些路径回到 handoff_required，那些判据
                    # 也就不会被绕过。
                    pass
                elif not cache_latest:
                    # 其余一次性图（callback media / 插件 read）维持原状：多模态
                    # handoff 是它们的通道，也是 passive callback"必须以原始媒体
                    # 到达最终 VLM"那条约定的落实方式。
                    return ImageStageResult(
                        accepted=False,
                        mode="handoff_required",
                        generation=getattr(self, "_latest_image_generation", 0),
                        rejection_reason="multimodal_handoff_required",
                    )
                else:
                    return self.stage_multimodal_frame(
                        image_b64,
                        source=source,
                        request_id=request_id,
                        captured_at=captured_at,
                    )

            if not self._supports_native_image and not cache_latest:
                description = await self._analyze_image_with_vision_model(
                    image_b64,
                    update_turn_state=False,
                )
                clean_description = str(description or "").strip()
                return ImageStageResult(
                    accepted=bool(clean_description),
                    mode=VisualDeliveryMode.EXTERNAL_DESCRIPTION.value,
                    generation=getattr(self, "_latest_image_generation", 0),
                    description=clean_description or None,
                    rejection_reason=(
                        None if clean_description else "analysis_empty"
                    ),
                )

            # Standard StepFun is the only realtime provider without native
            # vision; its first frame triggers VISION_MODEL analysis.
            if '实时屏幕截图或相机画面正在分析中' in self._image_description and not self._supports_native_image:
                # 非原生视觉后端只需要本轮第一帧做分析；后续高频帧直接丢弃，避免并发刷爆 VISION_MODEL。
                async with self._image_lock:
                    if self._image_recognized_this_turn or self._image_being_analyzed:
                        return ImageStageResult(
                            accepted=False,
                            mode=VisualDeliveryMode.EXTERNAL_DESCRIPTION.value,
                            generation=getattr(self, "_latest_image_generation", 0),
                        )
                    self._image_being_analyzed = True
                if cache_latest:
                    # Bind the cached generation to the frame that actually
                    # owns this analysis. Concurrent frames rejected by the
                    # gate above must not replace it and later receive the
                    # first frame's description.
                    self._latest_image_generation = (
                        getattr(self, "_latest_image_generation", 0) + 1
                    )
                    self._latest_image_b64 = image_b64
                    self._latest_image_captured_at = time.monotonic()
                    self._latest_image_source = str(source or "unknown")
                    self._latest_image_request_id = request_id
                    self._proactive_image_consumed = False
                description = await self._analyze_image_with_vision_model(image_b64)
                clean_description = str(description or "").strip()
                return ImageStageResult(
                    accepted=bool(clean_description),
                    mode=VisualDeliveryMode.EXTERNAL_DESCRIPTION.value,
                    generation=getattr(self, "_latest_image_generation", 0),
                    description=clean_description or None,
                )

            preserve_cached_step_frame = (
                cache_latest
                and not self._supports_native_image
                and self._image_recognized_this_turn
                and self._latest_image_b64 is not None
                and not self._proactive_image_consumed
            )
            # A completed Step annotation remains bound to its still-pending
            # cached frame. Do not replace that generation with a newer frame
            # carrying no matching analysis. Still continue so an active user
            # turn can receive the completed description through the normal
            # _image_sent_this_turn path.

            if cache_latest and not preserve_cached_step_frame:
                # A monotonic generation distinguishes separately captured frames
                # even when their JPEG payloads are byte-for-byte identical.
                self._latest_image_generation = (
                    getattr(self, "_latest_image_generation", 0) + 1
                )
                self._latest_image_b64 = image_b64
                self._latest_image_captured_at = time.monotonic()
                self._latest_image_source = str(source or "unknown")
                self._latest_image_request_id = request_id
                self._proactive_image_consumed = False

            # Rate limiting for native image input (with VAD-based throttling).
            # A deliberate cue image (bypass_rate_limit) skips the interval check
            # so it's never silently dropped. The timestamp is updated only
            # after the provider confirms that the frame was sent.
            if self._supports_native_image:
                current_time = time.time()
                if not bypass_rate_limit:
                    elapsed = current_time - self._last_native_image_time
                    min_interval = NATIVE_IMAGE_MIN_INTERVAL
                    if not self._client_vad_active:
                        min_interval *= IMAGE_IDLE_RATE_MULTIPLIER
                    if elapsed < min_interval:
                        # Skip this image frame due to rate limiting
                        return ImageStageResult(
                            accepted=False,
                            mode=VisualDeliveryMode.NATIVE.value,
                            generation=getattr(self, "_latest_image_generation", 0),
                        )
            # Gemini uses SDK, not WebSocket events (_audio_in_buffer is not set for Gemini)
            if self._is_gemini:
                if self._gemini_session:
                    try:
                        image_bytes = base64.b64decode(image_b64)
                        if (
                            getattr(
                                self,
                                "_visual_delivery_mode",
                                VisualDeliveryMode.NATIVE,
                            )
                            != delivery_mode
                            or (
                                # 与本函数入口那道闸门同口径：描述模式是被明确
                                # 豁免的（set_visual_delivery_mode 进这个模式时
                                # 会顺手把 _raw_visual_delivery_blocked 置上，
                                # 所以只看这个标志会把描述模式下的一次性 cue 图
                                # 一起拦掉——Gemini 的主动搭话原始图投递整条断掉）。
                                delivery_mode
                                != VisualDeliveryMode.EXTERNAL_DESCRIPTION
                                and getattr(
                                    self,
                                    "_raw_visual_delivery_blocked",
                                    False,
                                )
                                and not callback_owned_raw_image
                            )
                        ):
                            return ImageStageResult(
                                accepted=False,
                                mode=VisualDeliveryMode.NATIVE.value,
                                generation=getattr(
                                    self, "_latest_image_generation", 0
                                ),
                                rejection_reason="raw_visual_delivery_blocked",
                            )
                        await self._gemini_session.send_realtime_input(
                            media={"data": image_bytes, "mime_type": "image/jpeg"}
                        )
                    except Exception as e:
                        logger.error(f"Error sending image to Gemini: {e}")
                        if "closed" in str(e).lower():
                            self._fatal_error_occurred = True
                        raise
                    if self._supports_native_image:
                        self._last_native_image_time = current_time
                    # 送到了才复制。Gemini 不走 send_event，没有那条就地重压缩
                    # 的路径，所以 image_b64 就是 provider 收到的那份字节。
                    self._publish_provider_frame(
                        image_b64,
                        source=source,
                        captured_at=captured_at,
                    )
                    return ImageStageResult(
                        accepted=True,
                        mode=VisualDeliveryMode.NATIVE.value,
                        generation=getattr(self, "_latest_image_generation", 0),
                    )
                return ImageStageResult(
                    accepted=False,
                    mode=VisualDeliveryMode.NATIVE.value,
                    generation=getattr(self, "_latest_image_generation", 0),
                )

            if on_rejected is not None and self._supports_native_image:
                event_id = event_id or f"event_callback_image_{uuid.uuid4().hex}"
                rejection_event_id = event_id
                self._inject_rejection_handlers[event_id] = on_rejected
                self._fire_task(
                    self._expire_inject_rejection_handler(event_id, 60.0)
                )

            if self._is_free_provider:
                append_event = {
                    "type": "input_image_buffer.append" ,
                    "image": image_b64
                }
                if event_id is not None:
                    append_event["event_id"] = event_id
                sent = await self.send_event(
                    append_event,
                    raise_on_oversize=bypass_rate_limit,
                    expected_visual_mode=delivery_mode,
                    callback_owned_visual=callback_owned_raw_image,
                )
                if not sent and rejection_event_id is not None:
                    self._inject_rejection_handlers.pop(rejection_event_id, None)
                    rejection_event_id = None
                if sent and self._supports_native_image:
                    self._last_native_image_time = current_time
                if sent:
                    # 只判 sent，别跟着上面那半条件走：_supports_native_image 管
                    # 的是节流时间戳该不该更新，不是"这一帧有没有送出去"——free
                    # 路该标志为假时照样把帧发了出去，带上它就会静默漏掉真实投递。
                    # 字节从 append_event 里读回来——send_event 对超限帧的重压缩
                    # 是就地改写 event 的，参数里那份已不是 provider 收到的图。
                    self._publish_provider_frame_from_event(
                        append_event,
                        source=source,
                        captured_at=captured_at,
                    )
                return ImageStageResult(
                    accepted=sent,
                    mode=VisualDeliveryMode.NATIVE.value,
                    generation=getattr(self, "_latest_image_generation", 0),
                    rejection_reason=(
                        "raw_visual_delivery_blocked"
                        if not sent
                        and getattr(self, "_raw_visual_delivery_blocked", False)
                        and not callback_owned_raw_image
                        else None
                    ),
                )

            if self._audio_in_buffer or bypass_rate_limit:
                if "qwen" in self._model_lower:
                    append_event = {
                        "type": "input_image_buffer.append" ,
                        "image": image_b64
                    }
                elif "glm" in self._model_lower:
                    append_event = {
                        "type": "input_audio_buffer.append_video_frame",
                        "video_frame": image_b64
                    }
                elif "gpt" in self._model_lower:
                    append_event = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/jpeg;base64," + image_b64
                                }
                            ]
                        }
                    }
                else:
                    # Model does not support video streaming, use VISION_MODEL to analyze
                    # Only recognize one image per conversation turn
                    description_sent = False
                    async with self._image_lock:
                        if not self._image_recognized_this_turn:
                            if not self._image_being_analyzed:
                                self._image_being_analyzed = True
                                text_event = {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": self._image_description
                                            }
                                        ]
                                    }
                                }
                                logger.info("Sending image description before recognition.")
                                try:
                                    description_sent = await self.send_event(text_event)
                                    if description_sent:
                                        await self._analyze_image_with_vision_model(image_b64)
                                finally:
                                    if not description_sent:
                                        self._image_being_analyzed = False
                        elif not self._image_sent_this_turn:
                            self._image_sent_this_turn = True
                            text_event = {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "message",
                                        "role": "user",
                                        "content": [
                                            {
                                                "type": "input_text",
                                                "text": self._image_description
                                            }
                                        ]
                                    }
                            }
                            logger.info("Sending image description after recognition.")
                            description_sent = await self.send_event(text_event)
                    return ImageStageResult(
                        accepted=description_sent,
                        mode=VisualDeliveryMode.EXTERNAL_DESCRIPTION.value,
                        generation=getattr(self, "_latest_image_generation", 0),
                        description=(
                            str(getattr(self, "_image_description", "") or "").strip()
                            or None
                        ),
                    )

                if event_id is not None:
                    append_event["event_id"] = event_id
                sent = await self.send_event(
                    append_event,
                    raise_on_oversize=bypass_rate_limit,
                    expected_visual_mode=delivery_mode,
                    callback_owned_visual=callback_owned_raw_image,
                )
                if not sent and rejection_event_id is not None:
                    self._inject_rejection_handlers.pop(rejection_event_id, None)
                    rejection_event_id = None
                if sent and self._supports_native_image:
                    self._last_native_image_time = current_time
                if sent:
                    # 只判 sent，别跟着上面那半条件走：_supports_native_image 管
                    # 的是节流时间戳该不该更新，不是"这一帧有没有送出去"——free
                    # 路该标志为假时照样把帧发了出去，带上它就会静默漏掉真实投递。
                    # 字节从 append_event 里读回来——send_event 对超限帧的重压缩
                    # 是就地改写 event 的，参数里那份已不是 provider 收到的图。
                    self._publish_provider_frame_from_event(
                        append_event,
                        source=source,
                        captured_at=captured_at,
                    )
                return ImageStageResult(
                    accepted=sent,
                    mode=VisualDeliveryMode.NATIVE.value,
                    generation=getattr(self, "_latest_image_generation", 0),
                    rejection_reason=(
                        "raw_visual_delivery_blocked"
                        if not sent
                        and getattr(self, "_raw_visual_delivery_blocked", False)
                        and not callback_owned_raw_image
                        else None
                    ),
                    rejection_event_id=rejection_event_id,
                )
            return ImageStageResult(
                accepted=False,
                mode=VisualDeliveryMode.NATIVE.value,
                generation=getattr(self, "_latest_image_generation", 0),
            )
        except asyncio.CancelledError:
            if rejection_event_id is not None:
                self._inject_rejection_handlers.pop(rejection_event_id, None)
            raise
        except Exception as e:
            if rejection_event_id is not None:
                self._inject_rejection_handlers.pop(rejection_event_id, None)
            logger.error(f"Error streaming image: {e}")
            raise e

    async def _check_repetition(
        self, response: str, should_recover: Callable[[], bool] | None = None
    ) -> bool:
        """
        Check whether the reply is highly repetitive of recent replies.
        Returns True and triggers the callback if 3 consecutive turns are highly repetitive.
        """

        # 与最近的回复比较相似度
        high_similarity_count = 0
        for recent in self._recent_responses:
            similarity = calculate_text_similarity(response, recent)
            if similarity >= self._repetition_threshold:
                high_similarity_count += 1

        # 添加到最近回复列表
        self._recent_responses.append(response)
        if len(self._recent_responses) > self._max_recent_responses:
            self._recent_responses.pop(0)

        # 如果与最近2轮都高度重复（即第3轮重复），触发检测
        if high_similarity_count >= 2:
            logger.warning(f"OmniRealtimeClient: 检测到连续{high_similarity_count + 1}轮高重复度对话")

            # 清空重复检测缓存
            self._recent_responses.clear()

            # 触发回调
            if should_recover is not None and not should_recover():
                # Recording history is about the text this turn produced and
                # lands nowhere else. The RECOVERY is not: the host clears the
                # focus state, resets the emotion scorer and warns the user, so
                # firing it once a new turn has started applies a dead turn's
                # remedy to a live one. Checked here rather than at the caller
                # because ``wait_for`` yields before this body runs.
                logger.info(
                    "repetition detected on a turn that is no longer current; "
                    "recording it but skipping the recovery"
                )
                return True
            if self.on_repetition_detected:
                await self.on_repetition_detected()

            return True

        return False

    def _reset_per_turn_output_state(self) -> None:
        """Clear the transport state scoped to one response.

        Extracted from the ``response.done`` handler so any future path that
        ends a turn without its terminal event has one place to call rather
        than a list to re-derive. Every field here leaks into the NEXT turn if
        it is missed: a stale ``_image_sent_this_turn`` makes ``stream_image``
        withhold that turn's visual context for its whole duration, a stale
        transcript buffer is flushed against the wrong turn, and
        ``_audio_delta_count`` drives the "did this turn actually speak"
        checks.

        Behaviour is unchanged — this is the same block, in the same order,
        with the same conditions.
        """

        self._audio_delta_count = 0
        # 确保 buffer 被清空
        self._output_transcript_buffer = ""
        self._print_input_transcript = False
        if self._supports_native_image:
            self._image_recognized_this_turn = False
        elif (
            self._latest_image_b64 is None
            or self._proactive_image_consumed
        ):
            # Standard StepFun analyzes only while this sentinel is
            # present. Rearm after a consumed/absent frame, but keep
            # a completed annotation generation-bound to an
            # unconsumed cached frame across unrelated responses.
            self._image_recognized_this_turn = False
            self._image_description = _IMAGE_ANALYSIS_PENDING_DESCRIPTION
        self._image_sent_this_turn = False

    async def _flush_pending_output_transcript(self) -> None:
        """Forward transcript text this turn produced but never flushed.

        Some providers (the lanlan.app Gemini proxy among them) emit
        ``response.audio_transcript.delta`` and no transcript-done event, so
        the buffer is normally drained by the streaming branch. In a turn that
        used tools, the tool round's terminal clears
        ``_print_input_transcript``, and the real reply's transcript then
        accumulates in the buffer with nothing left to flush it — resetting
        per-turn state would drop it and the frontend shows audio with no text.

        Fires only when this turn actually spoke, so a normal turn is a no-op
        and nothing is sent twice. Must run BEFORE the per-turn reset, which
        is what clears the buffer.
        """

        await self._emit_pending_output_transcript(
            self._take_pending_output_transcript()
        )

    def _record_response_usage(self, resp_data: Any) -> None:
        """Book the provider's token counts for one finished response, once.

        Shared by the terminal path and the stale-terminal path, because a
        response's cost does not depend on whose turn the host thinks is
        current when its ``response.done`` finally arrives.

        Which is exactly why it has to deduplicate. The transport already
        tolerates a repeated ``response.done`` without finalizing the turn
        twice — and a repeat necessarily takes the stale branch, because the
        first one cleared ``_current_response_id`` — so counting on both paths
        without a guard would overstate usage for a case the transport
        supports on purpose. Keyed by response id.

        The last sentence of this docstring used to read "a terminal with no
        id never reaches the stale branch, so it can only be counted once
        anyway." That is backwards. An id-less terminal never reaches the
        stale branch precisely BECAUSE the filter needs an id — so a repeat of
        it takes the ordinary terminal path both times, and the guard below,
        keyed on an id it does not have, does not fire for either. Two copies
        book twice; measured.

        Left unfixed on purpose. A latch would have to be reset per turn, and
        the provider class that omits a terminal id is the same one that omits
        ``response.created`` — on such a connection there is no reset point at
        all, so the latch would swallow every turn after the first. That trades
        an accounting error no measured provider can produce for a real missed
        bill. If a provider ever does repeat an id-less terminal, the fix
        belongs at the terminal dispatch as a "this turn is already finalized"
        latch, not here.
        """

        if not isinstance(resp_data, dict):
            return
        try:
            usage = resp_data.get("usage")
            if not usage:
                return
            response_id = resp_data.get("id")
            if response_id is not None:
                if response_id in self._usage_recorded_ids:
                    return
                self._usage_recorded_ids.append(response_id)
                if len(self._usage_recorded_ids) > _USAGE_RECORDED_ID_LIMIT:
                    self._usage_recorded_ids.pop(0)
            from utils.token_tracker import TokenTracker

            TokenTracker.get_instance().record(
                model=resp_data.get("model", self.model or "realtime"),
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                call_type="conversation_realtime",
                source="main_logic/omni_realtime_client",
            )
        except Exception as exc:
            # Accounting is bookkeeping, and it runs on the receive loop. A
            # tracker that is unavailable, or a provider whose usage payload
            # has an unexpected shape, must not take the voice session down
            # with it — the turn itself already happened either way.
            logger.debug("realtime usage accounting skipped: %s", exc)

    def _take_response_transcript(self) -> str:
        """Close the books on what this turn actually said.

        Split from the repetition check that consumes it for the same reason
        as the output-transcript pair below: the release path has to commit
        every synchronous write before its first await, or a cancellation
        strands this turn's state for the next one to inherit.

        Reads ``_audio_delta_count`` for its log line, so it must run BEFORE
        the per-turn reset zeroes it.
        """

        transcript = self._current_response_transcript
        if transcript:
            self._last_response_transcript = transcript
            print(
                f"OmniRealtimeClient: response.done - 当前转录: "
                f"'{transcript[:50]}...' | audio_deltas={self._audio_delta_count}"
            )
            self._current_response_transcript = ""
        else:
            self._last_response_transcript = ""
            print(
                "OmniRealtimeClient: response.done - 没有转录文本 | "
                f"audio_deltas={self._audio_delta_count}"
            )
        return transcript

    async def _record_response_repetition(
        self, transcript: str, should_recover: Callable[[], bool] | None = None
    ) -> None:
        """Add what this turn said to the repetition history.

        Ending a turn has to do this on EVERY path, not just the terminal one.
        A provider that repeatedly loses its ``response.done`` — the case the
        fail-open hatch exists for — would otherwise never contribute an
        audible reply to ``_recent_responses``, so three identical turns in a
        row could not trigger ``on_repetition_detected`` at all.

        The history is recorded before ``_check_repetition``'s only await, so
        a bounded caller that cuts the host callback short still keeps it.
        """

        if transcript:
            await self._check_repetition(transcript, should_recover)

    def _take_pending_output_transcript(self) -> tuple[str, bool] | None:
        """Decide what the fallback flush owes the host, and settle the state.

        Split from the sending half so a caller that must not be interrupted
        mid-cleanup can commit every synchronous write first, then await. The
        turn's remaining state is consistent the moment this returns, whether
        or not the emit that follows ever completes.
        """

        if not (
            self._output_transcript_buffer
            and self.on_output_transcript
            and self._audio_delta_count > 0
        ):
            return None
        # 「有声无字」是反复出现的问题（见 ISSUE4b），留一条 debug 日志方便下次
        # 诊断时确认是这条兜底生效、还是 streaming/transcript.done 路径生效。
        # audio_delta_count 此处尚未清零，记录的是本轮真实值。
        logger.debug(
            "turn-end 兜底 flush 输出转录: buffer_len=%d audio_deltas=%d is_first=%s",
            len(self._output_transcript_buffer),
            self._audio_delta_count,
            self._is_first_transcript_chunk,
        )
        pending = (self._output_transcript_buffer, self._is_first_transcript_chunk)
        self._is_first_transcript_chunk = False
        return pending

    async def _emit_pending_output_transcript(
        self, pending: tuple[str, bool] | None
    ) -> None:
        """Send what ``_take_pending_output_transcript`` decided was owed."""

        if pending is None or not self.on_output_transcript:
            return
        text, is_first = pending
        await self.on_output_transcript(text, is_first)

    def _clear_turn_response_state(self) -> None:
        """Drop the flags that say "a response is in progress".

        Extracted from the ``response.done`` handler alongside
        ``_notify_turn_finished`` so that ending a turn is one implementation
        rather than a sequence any second caller has to reproduce. Behaviour
        is unchanged — same assignments, same order.
        """

        self._is_responding = False
        self._current_response_id = None
        self._current_item_id = None
        self._skip_until_next_response = False
        # 确保中断标志在响应结束时清除，防止阻塞下一轮 text.delta
        self._interrupted = False

    def _begin_response_lifecycle(self, response_id: Any) -> None:
        """Apply the host-side state shared by all accepted start evidence."""

        self._current_response_id = response_id
        self._is_responding = True
        self._turn_epoch += 1
        self._current_turn_epoch = self._turn_epoch
        self._current_turn_host_id = self._read_host_turn_id()
        self._interrupted = False
        # A stable successor id also closes the id-less quarantine opened by
        # a fail-open release; ordered socket delivery puts this evidence
        # before any later id-less event from the successor.
        self._idless_quarantine = False
        self._is_first_text_chunk = self._is_first_transcript_chunk = True
        self._output_transcript_buffer = ""
        self._current_response_transcript = ""

    def _read_host_turn_id(self) -> str | None:
        """Sample the host's live speech id, or None for "no answer".

        No answer covers both an unwired client and a host that raised, and
        ``_host_turn_is_still_ours`` treats it as "still ours" either way —
        which restores the pre-#2612 behaviour rather than inverting it.
        """

        if self.get_host_turn_id is None:
            return None
        try:
            return self.get_host_turn_id()
        except Exception as exc:
            logger.warning("host turn id unreadable (%s); turn guard is off", exc)
            return None

    def _host_turn_is_still_ours(self) -> bool:
        """Has the host started a turn of its own since this one began?

        Both "no answer" cases resolve to yes, and for the same reason in each
        direction: withholding the end of a turn is the worse failure, so a
        host that cannot be read disables the guard rather than the hooks.
        Unreadable is NOT "a different turn" — reading it as one would make an
        unwired or mid-teardown host silently stop ending turns at all.
        """

        if self._current_turn_host_id is None:
            return True
        live = self._read_host_turn_id()
        if live is None:
            return True
        return live == self._current_turn_host_id

    async def _notify_turn_finished(
        self,
        *,
        step_timeout: float | None = None,
        still_ours: Callable[[], bool] | None = None,
        connection_still_ours: Callable[[], bool] | None = None,
        carry_host_turn_forward: bool = False,
    ) -> None:
        """Tell the host this turn is over.

        The two hooks the terminal path fires, in the order it fires them.

        ``step_timeout`` and ``still_ours`` belong to the fail-open release
        path, and both default to the terminal path's behaviour so this stays
        one implementation rather than two.

        ``still_ours`` gates the PAIR, once, rather than each hook. They are
        not independent: ``on_response_done`` queues this turn's TTS-done
        sentinel, which closes its speech id, and ``on_sid_rotate`` is what
        hands out the next one. Re-checking between them lets a turn that
        starts mid-notification split the pair — the old sid closed, no new
        one issued — and on a provider without server VAD the successor then
        speaks under a closed sid and has its text silently dropped, which is
        the failure this hook exists to prevent. So either the release still
        owns the turn and finishes ending it, or it never started.

        ``connection_still_ours`` is deliberately separate. A replacement
        connection owns different host state even when the host-turn id is
        unavailable or unchanged, so it is re-checked after the first hook's
        await before the old terminal is allowed to rotate the replacement's
        speech id. This does not change the pair-once semantics of
        ``still_ours`` within one connection.

        ``on_sid_rotate`` gets no step bound of its own, because it is the
        last step — there is nothing behind it for a slow hook to starve. That
        is NOT the same as being uncancellable, and an earlier version of this
        comment claimed it was: the arbiter bounds the whole notification, so
        the rotation can be cancelled. What it cannot do is land half-applied.
        Its only await is taking the session lock, and no holder of that lock
        suspends while holding it, so the lock is never observed held and that
        acquire always takes the uncontended fast path without yielding — the
        cancellation therefore arrives before the rotation is entered or after
        it has returned. A second version of this comment claimed the opposite
        (TTS flags saying a fresh turn while the speech id still said the old
        one); measured, that state is not reachable while the lock invariant
        holds, and the invariant is now enforced by CORE_LOCK_NO_AWAIT in
        ``scripts/check_core_contracts.py`` rather than left to convention
        (#2619).

        This still is not the path to shield the rotation from. The rotation
        has two other callers cancelled just as ordinarily and with no escape
        hatch involved
        ([_responses.py](main_logic/omni_realtime_client/_responses.py) and
        [proactive.py](main_logic/core/proactive.py), both inside
        fire-and-forget tasks), and shielding here measurably reopens the hole
        ``_turn_epoch`` closed: a detached rotation takes the lock after the
        epoch has already moved and overwrites the new session's speech id,
        which ``lifecycle.py``'s lock-free write cannot be FIFO-ordered
        against.

        ``on_sid_rotate`` is conditional because providers WITH server VAD
        rotate the speech id from ``speech_stopped`` instead; firing here too
        would be a second, unpaired rotation on a live turn. Providers without
        it never emit ``speech_stopped`` (the Gemini proxy: lanlan.app+free,
        and livestream), so this is their only rotation point — and without it
        TTS upstream silently drops every later turn's text once the first
        ``tts.response.done`` closes the initial sid. The lightweight
        rotate-only path is deliberate: a full ``handle_new_message`` would
        clip trailing TTS audio and mis-fire USER_INPUT, since no user input
        actually happened.

        Each hook is awaited independently so a host that raises while closing
        the turn cannot skip the rotation that follows it.

        The host-side turn check (#2612) is a SEPARATE condition from
        ``still_ours``, and unlike it, is re-read before each hook. Two reasons
        the pair-once rule does not apply to it:

        - It is the only condition that sees a turn the host started on its
          own. ``still_ours`` compares turn epochs, and the epoch only counts
          turn starts this transport observes; a text input or an independent
          ASR utterance goes straight to ``handle_new_message``, which takes a
          fresh speech id without this side ever hearing about it. On a
          provider without server VAD that is the whole failure: the host hangs
          in ``on_response_done``, the user starts a turn during the hang, and
          ``on_sid_rotate`` then throws away the speech id that turn is
          speaking under — after which TTS upstream drops every later turn's
          text for the life of the connection.
        - Splitting the pair is what the pair-once rule protects against —
          "old sid closed, no new one issued". This condition cannot produce
          that state: it is true precisely BECAUSE the host issued a new speech
          id, and every writer of it also resets the per-turn TTS flags. So
          standing down here leaves the successor whole, while proceeding
          closes the successor's own sid (``on_response_done`` requests the
          TTS-done sentinel against whatever sid is live) and then rotates it
          out from under itself.
        """

        if connection_still_ours is not None and not connection_still_ours():
            logger.info(
                "the connection was replaced before its turn could be ended; "
                "leaving both end-of-turn hooks to the replacement"
            )
            return
        if still_ours is not None and not still_ours():
            logger.info(
                "a new turn started before this one could be ended; leaving "
                "both end-of-turn hooks to it"
            )
            return
        if not self._host_turn_is_still_ours():
            logger.info(
                "the host is already on a new turn (%s); leaving both "
                "end-of-turn hooks to it",
                self._current_turn_host_id,
            )
            return
        if self.on_response_done:
            try:
                if step_timeout is None:
                    await self.on_response_done()
                else:
                    await asyncio.wait_for(self.on_response_done(), step_timeout)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # Kept ahead of the bare Exception arm even though TimeoutError
                # is one: "took too long" and "raised" are different diagnoses.
                #
                # ``%s``, not ``%.1f``: the terminal path calls in with
                # ``step_timeout=None`` and awaits the hook directly, so this
                # arm is also how a TimeoutError raised BY the host surfaces
                # there. Formatting None with %.1f raises inside logging and
                # destroys the record — the one diagnosis this arm exists to
                # give.
                logger.warning(
                    "turn-finished notification exceeded its %ss step bound; "
                    "rotating anyway",
                    step_timeout,
                )
            except Exception as exc:
                logger.warning("turn-finished notification failed: %s", exc)
        if connection_still_ours is not None and not connection_still_ours():
            logger.info(
                "the connection was replaced while its turn was being closed; "
                "leaving the replacement's speech id alone"
            )
            return
        if not self._host_turn_is_still_ours():
            # Re-read, because the hook above is exactly where the host hangs.
            logger.info(
                "the host started a new turn while this one was being closed "
                "(%s); leaving its speech id alone",
                self._current_turn_host_id,
            )
            return
        if not self._has_server_vad and self.on_sid_rotate:
            try:
                await self.on_sid_rotate()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("turn-finished speech-id rotation failed: %s", exc)
            else:
                if carry_host_turn_forward and (
                    connection_still_ours is None or connection_still_ours()
                ):
                    # Some no-VAD proxies omit the next response.created and
                    # response id, so its terminal has no other turn owner.
                    self._current_turn_host_id = self._read_host_turn_id()
                    self._is_first_text_chunk = True
                    self._is_first_transcript_chunk = True

    async def _on_arbiter_stuck_release(
        self, reason: str, response_id: str | None = None
    ) -> None:
        """End a turn the arbiter gave up on, exactly as its terminal would.

        The same three steps ``response.done`` runs, in the same order. That
        is the entire point: a second way to end a turn is a second thing to
        keep correct, and the withdrawn #2592 spent seven review rounds
        discovering, one at a time, which parts its own version had left out.

        Clearing the identity here is what quarantines the abandoned
        response's later events — the stale-event filter then routes its
        terminal to the arbiter alone, so the lane still releases but nothing
        finalizes a second time. Note this is the opposite of what
        ``handle_interruption`` wants, which keeps the identity precisely so
        the cancelled response's own terminal still ends the turn.

        ``response_id`` names the response the arbiter abandoned, and this
        finalizes only that one. The turn being tracked here is not always it:
        an owned response can overlap a server-initiated one, and it is the
        server response's ``response.created`` that last wrote
        ``_current_response_id``. Ending "the current turn" would then close a
        response that is still streaming, and its own terminal would find
        nothing left to close. A ``None`` id means the arbiter had nothing to
        name — it never learned one — and the tracked turn is finalized as
        before.

        A tracked id of ``None`` is not a wildcard either. ``response_id``
        comes from the owner's own ``response.created`` — the event that wrote
        ``_current_response_id`` three lines later in the same handler — so a
        named release implies the host once tracked that exact id. Seeing
        ``None`` now means a later, id-less ``response.created`` overwrote it:
        an overlapping response that is still streaming, and not this
        release's to end.

        The synchronous state is settled before the first await on purpose.
        Both remaining awaits reach host code that can block past the
        arbiter's notification bound, and being cancelled there must not leave
        this turn's flags half-cleared for the next turn to inherit.

        Identity has to survive those awaits, not merely precede them. The
        lane can reopen mid-notification — the abandoned response's own
        terminal can land and release it — and the next turn can be live
        before the transcript flush returns. The arbiter cannot prevent that
        from its side (the user's own turn starts through
        ``handle_new_message``, which never consults the lane), so the check
        lives here: the release captures ``_turn_epoch`` and abandons the rest
        of its work the moment a new turn has started. The rotation it skips
        is deferred rather than lost — the turn that took over ends through
        its own terminal, which rotates.
        """

        tracked_id = self._current_response_id
        # Compared as text on both sides. The arbiter normalises ids through
        # `_event_response_id` (`str(...)`), while this side stores whatever
        # the JSON carried — so a provider using a numeric id made every
        # comparison here false ("123" != 123) and the release silently
        # finalized and quarantined nothing, on every turn.
        if response_id is not None and (
            tracked_id is None or str(tracked_id) != str(response_id)
        ):
            if tracked_id is None:
                # Nothing is tracked, so nothing id-less arriving before the
                # next response.created can belong to a live turn — the
                # released response is the only candidate, and its tool calls
                # are what the quarantine exists to stop.
                #
                # Deliberately NOT raised when tracked_id names a different,
                # LIVE response: that one has already announced, so the
                # window's "closes at the next response.created" bound would
                # fall after its own id-less tool calls and suppress them
                # instead. Containing an abandoned turn must not mute a live
                # one.
                self._idless_quarantine = True
            logger.info(
                "Arbiter released %s but this turn is tracking %s; leaving it "
                "alone",
                response_id,
                tracked_id,
            )
            return
        if not self._is_responding and self._current_response_id is None:
            return
        # The epoch this response began in, not the one the callback happens to
        # find. Between them a barge-in can have advanced _turn_epoch at
        # speech_stopped — which does not clear _current_response_id, so the id
        # guard above still passes — and reading the live value here would make
        # the check compare the successor's epoch with itself.
        released_epoch = self._current_turn_epoch
        # Connection ownership is a SEPARATE question from turn ownership, and
        # this path needs both. A replacement bumps _connection_generation
        # without touching _turn_epoch, and this release runs in its caller's
        # task rather than the arbiter worker -- so reset_connection_state does
        # not cancel it. Without this, a release that outlived its connection
        # still passes _still_ours() and rotates the REPLACEMENT's speech id
        # (and queues the abandoned turn's trailing text as its TTS).
        released_generation = self._connection_generation

        def _still_ours() -> bool:
            return self._turn_epoch == released_epoch

        def _connection_still_ours() -> bool:
            return self._connection_generation == released_generation

        if not _still_ours():
            # A turn already started before this release even ran, so NOTHING
            # here belongs to it — not the awaited hooks, and not the
            # synchronous cleanup ahead of them either. Both have side effects
            # on the live turn: `_clear_turn_response_state` resets
            # `_interrupted`, which on a provider whose late deltas carry no id
            # is the only thing keeping the abandoned response's audio out of
            # the new turn; and `_check_repetition` can fire
            # `on_repetition_detected`, whose host resets the shared focus
            # scorer and emotion state rather than merely recording history.
            #
            # Leaving this turn's per-turn flags for the successor's own
            # terminal to clear is the lesser harm, and the successor's
            # `response.created` overwrites the identity fields regardless.
            # One thing does still have to happen: give up the identity. The
            # stale-event filter keys on `_current_response_id`, so leaving it
            # naming the abandoned response makes that response's LATER
            # id-bearing events match and pass — a delayed
            # `function_call_arguments.done` would execute its tool, and its
            # `response.done` would run a full finalization against the user's
            # new turn. Clearing it is what quarantines them, and it is the one
            # piece of `_clear_turn_response_state` that belongs to the dead
            # turn rather than the live one: `_is_responding`, `_interrupted`
            # and the per-turn flags are the successor's now.
            logger.info(
                "a turn already started before this release ran (%s); "
                "quarantining %s and leaving the rest of the host alone",
                reason,
                self._current_response_id,
            )
            self._current_response_id = None
            self._current_item_id = None
            # The per-response output accounting belongs to the dead turn as
            # well, and nothing else will clear it: `response.created` resets
            # the transcript buffers but not `_image_sent_this_turn` or
            # `_audio_delta_count`, so a successor would spend its whole
            # duration withholding its own visual context and counting the
            # previous turn's audio. Safe here because reaching this line
            # means the tracked id still named the abandoned response — the
            # successor has not announced itself yet, so it has produced no
            # output of its own to erase.
            self._reset_per_turn_output_state()
            # `_skip_until_next_response` is deliberately NOT touched here,
            # and neither leaving it nor clearing it is right — which is the
            # actual finding.
            #
            # Leaving it mutes the successor: `_interrupted` may be left for
            # the next turn because `response.created` resets it, and this flag
            # has no such reset, so the successor's every delta stays
            # suppressed until its own terminal. But clearing it is not the
            # answer either, because the flag may already belong to the
            # successor: `create_response(skipped=True)` raises it BEFORE it
            # enqueues (`_responses.py`), so a request queued behind the
            # abandoned one owns it while it waits for the lane. Clearing would
            # then un-skip a turn the caller explicitly asked to suppress.
            #
            # A flag with no owner cannot be correctly cleared or correctly
            # left; picking a side is arbitrary. The fix is to give output
            # suppression a per-turn identity, which is issue #2594. Until
            # then this stays as it shipped rather than trading one wrong
            # behaviour for another — the whole state is unreachable today
            # (nothing on the WebSocket path passes `skipped=True`), so there
            # is nothing to buy by guessing.
            # Both release paths raise it: the abandoned response may still be
            # streaming, and from here until the next response.created nothing
            # id-less can be attributed. Clearing _current_response_id above
            # quarantines its ID-BEARING events; this covers the rest.
            self._idless_quarantine = True
            return
        logger.info("Ending abandoned turn after arbiter release: %s", reason)
        # Both release paths raise it: the abandoned response may still be
        # streaming, and from here until the next response.created nothing
        # id-less can be attributed. Clearing _current_response_id above
        # quarantines its ID-BEARING events; this covers the rest.
        self._idless_quarantine = True

        # Captured before the reset, which is what clears the buffer: a stalled
        # lifecycle is exactly the case where the terminal that would normally
        # flush it never arrives.
        pending_transcript = self._take_pending_output_transcript()
        pending_response = self._take_response_transcript()
        self._clear_turn_response_state()
        self._reset_per_turn_output_state()
        # Same order the terminal path uses: repetition history first, then
        # the fallback transcript flush.
        try:
            await asyncio.wait_for(
                self._record_response_repetition(pending_response, _still_ours),
                _STUCK_RELEASE_STEP_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "stuck-release repetition check exceeded %.1fs; ending the "
                "turn anyway",
                _STUCK_RELEASE_STEP_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("stuck-release repetition check failed: %s", exc)
        # Epoch-guarded, unlike the repetition check above it. That one is
        # bookkeeping about the released turn's own text and lands nowhere
        # else; this one goes out through ``handle_output_transcript``, which
        # publishes and queues TTS under whatever speech id is CURRENT — so
        # once a successor has started, flushing here speaks the abandoned
        # turn's half-sentence as part of the successor's. The released turn's
        # trailing text is worth losing to prevent that; it is the same
        # "lands on that turn or not at all" rule the end-of-turn hooks follow.
        #
        # The repetition check ahead of it can yield (on_repetition_detected),
        # which is what makes this reachable — an earlier version of this
        # comment said the flush was the first await and therefore safe, and
        # inserting that step in front of it quietly made that false.
        #
        # Best-effort besides: a host that blocks or raises while taking the
        # last half-sentence must not take the rotation behind it down too.
        if _still_ours() and _connection_still_ours():
            try:
                await asyncio.wait_for(
                    self._emit_pending_output_transcript(pending_transcript),
                    _STUCK_RELEASE_STEP_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "stuck-release transcript flush exceeded %.1fs; ending the "
                    "turn anyway",
                    _STUCK_RELEASE_STEP_TIMEOUT,
                )
            except Exception as exc:
                logger.warning("stuck-release transcript flush failed: %s", exc)
        elif pending_transcript is not None:
            logger.info(
                "a new turn started before the abandoned turn's trailing "
                "transcript could be sent; dropping it rather than speaking "
                "it as the new turn's"
            )
        await self._notify_turn_finished(
            step_timeout=_STUCK_RELEASE_STEP_TIMEOUT,
            still_ours=_still_ours,
            connection_still_ours=_connection_still_ours,
        )

    async def handle_interruption(
        self,
        *,
        connection_still_ours: Callable[[], bool] | None = None,
    ) -> None:
        """Handle user interruption of the current response."""
        if connection_still_ours is not None and not connection_still_ours():
            return
        if not self._is_responding:
            return

        logger.info("Handling interruption")

        # Mark as interrupted to suppress any remaining output until next response
        self._interrupted = True
        # 这一轮被取消了，但 provider 仍然欠它一个终结事件（Gemini 是
        # interrupted / turn_complete）。记一笔「欠账」：那条终结属于**这一轮**，
        # 不能让它去结算之后才铸造的 external turn token —— 逐事件捕获的实现会
        # 让它捕获到新 token 并清掉，会话随即显得空闲而外部回合还活着。
        # 一次性的：由下一个到达的终结消费掉，或在真正的新回合开始时作废
        # （_gemini_support.py 的 turn-start 块），或到期。
        self._gemini_cancelled_terminal_pending = True
        # 期限是独立于内容的第二道界。turn-start 那处作废只在「合法终结之前先来了
        # AI 内容」时才跑得到；裸 turn_complete 绕过它，此后再无内容的情形也绕过
        # 它。没有期限时，陈旧欠账会一直挂着直到吃掉某一条不属于它的终结。
        self._gemini_cancelled_terminal_deadline = (
            time.monotonic() + GEMINI_CANCELLED_TERMINAL_TTL_SECONDS
        )
        # 期限的起点要等中断真正送达 provider 才算数。Gemini 没有 response.cancel，
        # 中断是靠后继内容送出去才生效的，所以 _gemini_send_user_turn 会把它重打
        # 一次；这里先打是为了让下面任何一条 return 路径都带着期限离开，不至于落进
        # deadline is None 的 fail-open。只允许重打一次，免得后续每次发送都续命。
        self._gemini_cancelled_terminal_awaiting_delivery = True
        # 身份：让 _gemini_send_user_turn 能认出「这笔欠账是不是我这次发送送达的」。
        # 那边是 await 之后才读全局状态的，没有身份的话，一次**更早**发起、此刻才
        # 返回的发送会把刚武装的这笔当成自己送的，提前起算 TTL。
        self._gemini_cancelled_terminal_id = object()

        # 1. Cancel the current response
        # Presence, not truthiness — the third site in this file where a
        # numeric id of 0 would have read as "no response". Here the cost is
        # the worst of the three: the barge-in would mark the turn interrupted
        # and never send response.cancel, so generation keeps running and the
        # arbiter lane stays held until the provider finishes on its own.
        if self._current_response_id is not None:
            try:
                await self.cancel_response(send_guard=connection_still_ours)
            except ConnectionError:
                if (
                    connection_still_ours is not None
                    and not connection_still_ours()
                ):
                    return
                raise

        if connection_still_ours is not None and not connection_still_ours():
            return

        self._is_responding = False
        # Keep the cancelled response identity until its terminal event arrives.
        # Clearing it here makes the stale-event filter classify that
        # response.done as stale. The filter still forwards stale terminals to
        # the arbiter (the lane would reopen either way), but the rest of the
        # done handling is skipped for the turn: the done counters and usage
        # recording, the _interrupted reset, the transcript flush and the
        # on_response_done callback all silently miss one turn.
        self._current_item_id = None
        # 清空转录buffer和重置标志，防止打断后的错位
        self._output_transcript_buffer = ""
        self._is_first_transcript_chunk = True

    async def handle_messages(self) -> None:
        # Gemini uses different message handling
        if self._is_gemini:
            await self._handle_messages_gemini()
            return

        try:
            message_ws = self.ws
            message_generation = self._connection_generation
            if not message_ws:
                logger.error("WebSocket connection is not established")
                return

            def receive_owner_is_current() -> bool:
                return bool(
                    message_generation == self._connection_generation
                    and message_ws is self.ws
                )

            async def retire_if_replaced() -> bool:
                if receive_owner_is_current():
                    return False
                if self._transport_detached_for_teardown(
                    message_generation,
                ):
                    logger.info(
                        "Raw receive event retired by the connection teardown"
                    )
                    return True
                # Delegate instead of aborting inline. Returning True exits
                # handle_messages, which SKIPS the `else:` tail -- and that
                # tail is the only consumer of _local_failure_recovery. An
                # arbiter fail-close landing while this loop sat in a host
                # callback would otherwise leave the latch armed: socket dead,
                # _fatal_error_occurred dropping every later send, and the
                # manager never told, so no toast and no rebuild. The fail-
                # close reasons ARE slow-host reasons, so that pairing is the
                # expected one, not a corner.
                #
                # The recovery already splits the two cases this needs: latch
                # present -> finish the teardown and report
                # CHARACTER_DISCONNECTED; latch absent -> exactly the silent
                # abort that used to be inlined here. It also logs each case,
                # where the line this replaces claimed a replacement had
                # attached even when none had.
                await self._recover_receive_loop_disconnect(
                    message_ws,
                    message_generation,
                    "retired raw receive event",
                    status_code="CHARACTER_DISCONNECTED",
                )
                return True

            async for message in message_ws:
                if await retire_if_replaced():
                    return
                event = json.loads(message)
                event_type = event.get("type")

                # if event_type not in ["response.audio.delta", "response.audio_transcript.delta",  "response.output_audio.delta", "response.output_audio_transcript.delta"]:
                #     # print(f"Received event: {event}")
                #     print(f"Received event: {event_type}")
                # else:
                #     print(f"Event type: {event_type}")
                if event_type == "error":
                    error_msg = str(event.get('error', ''))
                    logger.error(f"API Error: {error_msg}")

                    # Route server rejections of a proactive inject's
                    # ``response.create`` / ``conversation.item.create`` back to
                    # the caller so it can re-enqueue the optimistically-pruned
                    # cb (see _route_inject_rejection). ``error`` events
                    # normally echo the offending client event_id at
                    # ``error.event_id``; some providers put it top-level or
                    # omit it entirely — the helper handles all three.
                    err_obj = event.get('error') if isinstance(event.get('error'), dict) else {}
                    err_event_id = err_obj.get('event_id') or event.get('event_id')
                    self._route_inject_rejection(err_event_id, error_msg)
                    self._response_arbiter.notify_error(err_event_id, error_msg)

                    # 致命性判定只看语义字段，绝不看回显的 event_id（见
                    # _error_classification_text 的注释）。日志、路由和
                    # on_connection_error 继续用完整的 error_msg。
                    classify_text = _error_classification_text(event.get('error'))
                    classify_lower = classify_text.lower()

                    # 检测503过载错误，触发backpressure节流
                    if '503' in classify_text or 'overloaded' in classify_lower:
                        self._is_throttled = True
                        self._throttle_until = time.time() + self._throttle_duration
                        self._server_busy_count += 1
                        logger.warning(f"⚡ 503 detected (count={self._server_busy_count}), throttling for {self._throttle_duration}s")
                        # 前2次静默节流，第3次起通知前端
                        if self._server_busy_count >= 3 and self.on_status_message:
                            await self.on_status_message(json.dumps({"code": "SERVER_BUSY_THROTTLE"}))
                            if await retire_if_replaced():
                                return
                        continue

                    # Idle timeout — Qwen 约 25s 无操作断连
                    if 'too long without operation' in classify_lower or 'idle' in classify_lower:
                        logger.warning("⏰ Idle timeout from API: %s", error_msg)
                        if self.on_connection_error:
                            await self.on_connection_error(json.dumps({"code": "API_IDLE_TIMEOUT", "details": {"msg": error_msg}}))
                            if await retire_if_replaced():
                                return
                        await self.close()
                        return

                    if ('欠费' in classify_text or 'standing' in classify_lower or 'time limit' in classify_lower or
                        'policy violation' in classify_lower or '1008' in classify_lower or
                        '429' in classify_lower or 'quota' in classify_lower or 'too many' in classify_lower):
                        if self.on_connection_error:
                            await self.on_connection_error(error_msg)
                            if await retire_if_replaced():
                                return
                        await self.close()
                        return
                    continue

                if event_type == "session.updated":
                    self._notify_session_updated(event)
                    handler = self.extra_event_handlers.get(event_type)
                    if handler is not None:
                        await handler(event)
                    continue

                if event_type in ID_BEARING_RESPONSE_CONTENT_EVENT_TYPES:
                    content_started = self._response_arbiter.notify_response_content(
                        event
                    )
                    if content_started:
                        self._begin_response_lifecycle(
                            _response_id_text(event.get("response_id"))
                        )

                # A cancelled response can still emit buffered events after a
                # replacement response has become current.  Providers that
                # include response identity let us reject those late events
                # without changing the legacy behaviour of id-less proxies.
                if event_type != "response.created":
                    # Presence, not truthiness, on both reads — the same
                    # correction the arbiter's `_event_response_id` gets in this
                    # PR, and useless without it. A provider numbering from zero
                    # would have response `0`'s late deltas, tool events and
                    # terminal slip past this filter once a successor is
                    # current, and a late terminal would then run the ordinary
                    # host finalization against that successor.
                    event_response_id = _response_id_text(event.get("response_id"))
                    if event_response_id is None and event_type == "response.done":
                        response = event.get("response")
                        if isinstance(response, dict):
                            event_response_id = _response_id_text(response.get("id"))
                    tracked = self._current_response_id
                    tracked_text = None if tracked is None else str(tracked)
                    if (
                        event_response_id is not None
                        and event_response_id != tracked_text
                        # ...unless this connection has never announced a
                        # response at all. A provider that omits
                        # response.created never writes _current_response_id,
                        # so its id-bearing terminal looks stale against a
                        # permanently-None tracked id and the whole turn
                        # finalization below is skipped: no transcript flush,
                        # no on_response_done, and — on exactly those routes,
                        # which have no server VAD — no speech-id rotation,
                        # which is what silences every turn after the first.
                        #
                        # Same reasoning as the arbiter's: a terminal for an id
                        # this connection has never seen announced cannot be
                        # another response's, because there is no other
                        # response to have announced it. The latch is per
                        # connection and set only by response.created, so on
                        # any announcing provider this condition is false from
                        # its first turn onward and the stale filter behaves
                        # exactly as before.
                        and self._announces_responses
                    ):
                        if event_type == "response.done":
                            # A terminal event must reach the arbiter even when
                            # a newer response has become current (crossed
                            # response.created events): the arbiter tracks every
                            # live server response id, and an undelivered
                            # terminal would hold the lane closed until its
                            # staleness timer. The arbiter attributes terminals
                            # by response id, so a mismatched id releases only
                            # that response and never completes the current
                            # owner. Content of the stale response stays
                            # filtered below.
                            self._response_arbiter.notify_response_terminal(event)
                            # The tokens were spent whoever the turn belonged
                            # to, and this is the ONLY path a fail-open
                            # released turn's terminal can take: the release
                            # clears _current_response_id on purpose, so its
                            # real terminal always lands here. Quarantining
                            # the host finalization must not also quarantine
                            # the accounting, or every recovered turn vanishes
                            # from usage stats even though the provider sent
                            # exact counts. Counted here and only here — the
                            # branch continues, so nothing double-counts.
                            self._response_done_total += 1
                            self._record_response_usage(event.get("response"))
                        logger.info(
                            "Dropping stale response event type=%s response_id=%s current_response_id=%s",
                            event_type,
                            event_response_id,
                            self._current_response_id,
                        )
                        continue
                # ── Tool calling events ────────────────────────────
                # Three providers, three flavours of the same idea:
                #   - OpenAI Realtime (gpt): the canonical event is the
                #     output_item.done with item.type=="function_call";
                #     response.done also carries it inside output[].
                #     Arguments are streamed as
                #     response.function_call_arguments.delta and finalized
                #     in response.function_call_arguments.done.
                #   - StepFun (step / lanlan.tech free): same pattern,
                #     function_call_arguments.delta + .done with call_id.
                #   - GLM (glm): only function_call_arguments.done is
                #     emitted (no delta), and there is no call_id field —
                #     we synthesize one from response_id+output_index.
                # All three return results via conversation.item.create
                # of type function_call_output + response.create, handled
                # by ``_send_tool_result_openai_realtime``.
                if event_type == "response.function_call_arguments.delta":
                    call_id = event.get("call_id") or ""
                    if call_id:
                        slot = self._inflight_tool_args.setdefault(call_id, {
                            "name": event.get("name") or "",
                            "arguments": "",
                        })
                        if event.get("name"):
                            slot["name"] = event["name"]
                        delta = event.get("delta") or ""
                        if delta:
                            slot["arguments"] += delta
                elif event_type == "response.function_call_arguments.done":
                    if self._idless_quarantine and not event.get("response_id"):
                        # A fail-open release abandoned a turn, and this event
                        # names no response — so it cannot be told apart from
                        # the successor's. Content that leaks is a wrong
                        # sentence; a tool call that leaks is a side effect
                        # executed on behalf of a turn nobody is having.
                        #
                        # Bounded, not blanket: the window closes at the next
                        # response.created (see below), which on the only
                        # providers that can reach here is guaranteed to carry
                        # an id — a release requires the abandoned response to
                        # have had one, and ids are written only from
                        # response.created. The successor's announcement
                        # therefore always precedes its own id-less events on
                        # this single ordered socket, so nothing of the
                        # successor's is ever suppressed.
                        logger.warning(
                            "quarantined an id-less tool call arriving after a "
                            "stuck-turn release (call_id=%s name=%s)",
                            event.get("call_id") or "?",
                            event.get("name") or "?",
                        )
                        self._inflight_tool_args.pop(event.get("call_id") or "", None)
                        continue
                    name = event.get("name") or ""
                    raw_args = event.get("arguments") or ""
                    call_id = event.get("call_id") or ""
                    if not call_id:
                        # GLM path: synthesize a stable call_id so we have
                        # something to thread through the registry.
                        rid = event.get("response_id") or ""
                        idx = event.get("output_index", 0)
                        call_id = f"glm_{rid}_{idx}" if rid else f"glm_call_{int(time.time()*1000)}"
                    # Prefer accumulated delta args if delta path was used.
                    accumulated = self._inflight_tool_args.pop(call_id, None)
                    if accumulated and accumulated.get("arguments"):
                        raw_args = accumulated["arguments"]
                        if not name:
                            name = accumulated.get("name") or name
                    if not name:
                        logger.warning(
                            "function_call_arguments.done with no name (call_id=%s) — skipping",
                            call_id,
                        )
                    else:
                        if self.on_tool_call is None:
                            logger.warning(
                                "function_call '%s' but no on_tool_call handler bound — replying with error",
                                name,
                            )
                        # Execute and reply asynchronously — don't block the
                        # message loop. handle_messages stays responsive to
                        # other events while the tool runs.
                        owner = self._capture_tool_task_owner(
                            message_ws,
                            connection_generation=message_generation,
                        )
                        self._start_raw_tool_call(
                            ToolCall(
                                name=name,
                                arguments=(
                                    {}
                                    if self.on_tool_call is None
                                    else parse_arguments_json(raw_args)
                                ),
                                call_id=call_id,
                                raw_arguments=raw_args,
                            ),
                            owner,
                            # Groups this call with the parallel siblings the
                            # SAME provider response issued, so they are
                            # answered as one batch with a single
                            # response.create. Absent on a provider that omits
                            # response identity -- there is then nothing that
                            # can prove two calls are siblings, and each is
                            # answered on its own as before.
                            response_id=_response_id_text(
                                event.get("response_id")
                            ),
                        )
                elif event_type == "conversation.item.created":
                    self._response_arbiter.notify_item_created(event)
                elif event_type == "response.done":
                    # No further function call can name this response, so its
                    # tool batch may answer as soon as its own calls settle.
                    # Ahead of the finalize branches below on purpose: several
                    # of them `continue`, and a stale or non-finalizing
                    # terminal still proves the response is over.
                    self.close_raw_tool_batch(
                        _response_id_text(event.get("response_id"))
                        or _response_id_text(
                            (event.get("response") or {}).get("id")
                            if isinstance(event.get("response"), dict)
                            else None
                        )
                    )
                    response = event.get("response")
                    response_status = (
                        str(response.get("status") or "").strip().lower()
                        if isinstance(response, dict)
                        else ""
                    )
                    finalize_response = (
                        self._response_arbiter.notify_response_terminal(event)
                    )
                    self._response_done_total += 1
                    self._last_response_done_time = time.time()
                    # 解析实时 API 返回的 token 用量
                    self._record_response_usage(event.get("response"))
                    if finalize_response is False:
                        continue
                    self._clear_turn_response_state()
                    # 响应完成，检测重复度
                    await self._record_response_repetition(
                        self._take_response_transcript()
                    )
                    if await retire_if_replaced():
                        return
                    # [有声无字兜底] 部分 provider（如 lanlan.app Gemini 语音代理）只发
                    # response.audio_transcript.delta、从不发 response.audio_transcript.done，
                    # 输出转录全靠下面 streaming 分支（_print_input_transcript=True）实时送出。
                    # 但带工具调用的一轮里，工具调用那一轮的 response.done 会把
                    # _print_input_transcript 置 False（见下方），紧随其后的真回复转录便走
                    # buffer 分支累积进 _output_transcript_buffer，没有 transcript.done 来 flush，
                    # 就在这里被直接清空 → 前端有声无字。这里在清空前补一次 flush：只要本轮真
                    # 出过声（audio_delta_count>0）且 buffer 仍有残留就补发。streaming 分支每次都
                    # 会清空 buffer，故正常轮此处为 no-op，不会重复发送。
                    try:
                        await self._flush_pending_output_transcript()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "response.done transcript flush failed (%s); continuing",
                            type(exc).__name__,
                        )
                    if await retire_if_replaced():
                        return
                    self._reset_per_turn_output_state()
                    await self._notify_turn_finished(
                        connection_still_ours=receive_owner_is_current,
                        carry_host_turn_forward=response_status
                        in {"", "completed", "success", "succeeded"},
                    )
                    if await retire_if_replaced():
                        return
                elif event_type == "response.created":
                    confirms_started_owner = (
                        self._response_arbiter.response_created_confirms_started_owner(
                            event
                        )
                    )
                    expose_response = self._response_arbiter.notify_response_created(event)
                    self._response_created_total += 1
                    self._last_response_created_time = time.time()
                    if not expose_response:
                        # A delayed announcement for a content-started owner is
                        # consumed without beginning host lifecycle twice, but
                        # still proves that this connection announces.
                        if confirms_started_owner:
                            self._announces_responses = True
                        continue
                    self._announces_responses = True
                    self._begin_response_lifecycle(
                        _response_id_text(
                            (event.get("response") or {}).get("id")
                            if isinstance(event.get("response"), dict)
                            else None
                        )
                    )
                elif event_type == "response.output_item.added":
                    self._current_item_id = event.get("item", {}).get("id")
                elif event_type == "input_audio_buffer.committed":
                    self._input_audio_committed_total += 1
                    self._last_input_audio_committed_time = time.time()
                    logger.info("input_audio_buffer.committed observed (total=%d)", self._input_audio_committed_total)
                # Handle interruptions
                elif event_type == "input_audio_buffer.speech_started":
                    self.note_user_turn_started()
                    self._note_raw_speech_started_scope(event.get("item_id"))
                    self._speech_started_total += 1
                    logger.info("Speech detected")
                    self._response_arbiter.notify_server_vad_started()
                    self._bind_input_route_identity_to_item(event.get("item_id"))
                    self._audio_in_buffer = True
                    # 重置静默计时器
                    self._last_speech_time = time.time()
                    # Priority 1: server VAD → sync to unified _client_vad_active
                    self._client_vad_active = True
                    self._client_vad_last_speech_time = self._last_speech_time
                    # B: server-VAD 也喂给 _user_recent_activity，保持各 VAD 源对称。
                    self._user_recent_activity_time = self._last_speech_time
                    if self._is_responding:
                        logger.info("Handling interruption")
                        await self.handle_interruption(
                            connection_still_ours=receive_owner_is_current,
                        )
                        if await retire_if_replaced():
                            return
                elif event_type == "input_audio_buffer.speech_stopped":
                    self._speech_stopped_total += 1
                    logger.info("Speech ended")
                    # Only an ended utterance can causally create the automatic
                    # server-VAD response.  Marking this at speech_started can
                    # steal an explicit response.created whose create was
                    # already accepted but whose echo is still in flight.
                    self._response_arbiter.notify_server_vad_response_pending(
                        arm_timeout=False
                    )
                    # The user's turn starts HERE on a server-VAD provider, not
                    # at response.created: on_new_message assigns the new
                    # speech id and fires USER_INPUT, and the provider's
                    # response.created only follows some time later. A release
                    # suspended in a host callback would otherwise resume in
                    # that gap, still believe the turn is its own, and finalize
                    # against the speech id this user turn just took.
                    self._turn_epoch += 1
                    try:
                        if self.on_new_message:
                            await self.on_new_message()
                    finally:
                        # response.created cannot be observed while this receive
                        # loop is blocked in on_new_message. Start the missing-
                        # created backstop only after the loop can read again,
                        # so a slow callback cannot release a real VAD response.
                        if receive_owner_is_current():
                            self._response_arbiter.arm_server_vad_response_pending_timeout()
                    if await retire_if_replaced():
                        return
                    self._audio_in_buffer = False
                    # Update timestamp so grace period starts from speech end
                    _now = time.time()
                    self._client_vad_last_speech_time = _now
                    self._user_recent_activity_time = _now
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    already_scoped = self._raw_transcript_was_already_scoped(
                        event.get("item_id")
                    )
                    if not self._has_server_vad and not already_scoped:
                        # Compatibility proxies may omit both server-VAD
                        # boundary events. The completed transcript is then the
                        # first authoritative signal that a new user turn has
                        # begun, so stale tool work must retire here.
                        self.note_user_turn_started()
                    self._print_input_transcript = True
                    transcript = event.get("transcript", "")
                    if self.on_input_transcript or self.on_input_transcript_with_route:
                        await self._deliver_input_transcript(
                            transcript,
                            item_id=event.get("item_id"),
                        )
                        if await retire_if_replaced():
                            return
                elif event_type in ["response.audio_transcript.done", "response.output_audio_transcript.done"]:
                    self._print_input_transcript = False
                    # [ISSUE4b] Voice-without-text fix. Audio deltas and transcript
                    # deltas are gated by _skip_until_next_response/_interrupted at
                    # delta time. But this transcript.done re-checks those flags at
                    # *done* time — if a flag flipped True between audio playing and
                    # done (session-transition / proactive-inject race), the audio
                    # was already spoken yet the transcript got dropped → 前端有声无字.
                    # If audio already went out this response (_audio_delta_count>0),
                    # always forward the matching transcript regardless of a late
                    # flag flip; only suppress when nothing was spoken (interrupted
                    # before any audio).
                    _audio_already_spoken = self._audio_delta_count > 0
                    if (
                        self._output_transcript_buffer and self.on_output_transcript
                        and (
                            (not self._skip_until_next_response and not self._interrupted)
                            or _audio_already_spoken
                        )
                    ):
                        await self.on_output_transcript(self._output_transcript_buffer, self._is_first_transcript_chunk)
                        if await retire_if_replaced():
                            return
                        self._is_first_transcript_chunk = False
                    self._output_transcript_buffer = ""

                if not self._skip_until_next_response and not self._interrupted:
                    if event_type in ["response.text.delta", "response.output_text.delta"]:
                        if self.on_text_delta:
                            if "glm" not in self._model_lower:
                                self._ai_recent_activity_time = time.time()
                                await self.on_text_delta(event["delta"], self._is_first_text_chunk)
                                if await retire_if_replaced():
                                    return
                                self._is_first_text_chunk = False
                    elif event_type in ["response.audio.delta", "response.output_audio.delta"]:
                        self._audio_delta_count += 1
                        self._audio_delta_total += 1
                        self._last_audio_delta_time = time.time()
                        if self._audio_delta_count == 1:
                            logger.info(f"🔊 首个 audio.delta 已收到 (type={event_type}, bytes={len(event.get('delta',''))})")
                        if self.on_audio_delta:
                            audio_bytes = base64.b64decode(event["delta"])
                            self._ai_recent_activity_time = time.time()
                            await self.on_audio_delta(audio_bytes)
                            if await retire_if_replaced():
                                return
                    elif event_type in ["response.audio.done", "response.output_audio.done"]:
                        # 权威的「这一轮音频流已关闭」信号（issue #1566）。前端原本
                        # 靠「四个音频队列当下是否为空」猜本轮放完没，落在音频阵之间
                        # 的空档就会提前收尾（口型停一下又重启、尾音孤儿）。
                        #
                        # ⚠️ 时序：必须在这里 await 触发，绝不能 _fire_task /
                        # create_task。本接收循环是顺序的，走到这条事件时该轮所有
                        # audio.delta 的 ``await self.on_audio_delta(...)`` 都已经
                        # 返回，因此完结信号天然排在最后一块音频之后。改成
                        # fire-and-forget 会让它插到音频前面，前端提前收尾 —— 那正是
                        # 这个 issue 本身。
                        #
                        # 放在 _skip_until_next_response / _interrupted 守卫内，与
                        # audio.delta 同门：被打断的一轮不发（打断有独立的 cancel
                        # 通道）。漏发是可接受的降级，前端有 give-up 计时器兜底。
                        if self.on_audio_done:
                            await self.on_audio_done()
                            if await retire_if_replaced():
                                return
                    elif event_type in ["response.audio_transcript.done", "response.output_audio_transcript.done"]:
                        if self.on_output_transcript and self._is_first_transcript_chunk:
                            transcript = event.get("transcript", "")
                            if transcript:
                                await self.on_output_transcript(transcript, True)
                                if await retire_if_replaced():
                                    return
                                self._is_first_transcript_chunk = False
                    elif event_type in ["response.audio_transcript.delta", "response.output_audio_transcript.delta"]:
                        if self.on_output_transcript:
                            delta = event.get("delta", "")
                            # 累积当前回复的转录文本用于重复度检测
                            self._current_response_transcript += delta
                            if not self._print_input_transcript:
                                self._output_transcript_buffer += delta
                            else:
                                if self._output_transcript_buffer:
                                    # logger.info(f"{self._output_transcript_buffer} is_first_chunk: True")
                                    await self.on_output_transcript(self._output_transcript_buffer, self._is_first_transcript_chunk)
                                    if await retire_if_replaced():
                                        return
                                    self._is_first_transcript_chunk = False
                                    self._output_transcript_buffer = ""
                                await self.on_output_transcript(delta, self._is_first_transcript_chunk)
                                if await retire_if_replaced():
                                    return
                                self._is_first_transcript_chunk = False

                    elif event_type in self.extra_event_handlers:
                        await self.extra_event_handlers[event_type](event)
                        if await retire_if_replaced():
                            return
                else:
                    # 调试日志：text.delta 被 _interrupted/_skip 标志拦截（每个 response 仅记录一次）
                    if event_type in ["response.text.delta", "response.output_text.delta"]:
                        if self._suppressed_delta_logged_resp_id != self._current_response_id:
                            self._suppressed_delta_logged_resp_id = self._current_response_id
                            logger.warning(
                                "⚠️ text.delta suppressed: _skip=%s, _interrupted=%s, resp_id=%s",
                                self._skip_until_next_response, self._interrupted, self._current_response_id
                            )

        except websockets.exceptions.ConnectionClosedOK:
            recovered = await self._recover_receive_loop_disconnect(
                message_ws,
                message_generation,
                "realtime connection closed",
                status_code="CHARACTER_DISCONNECTED",
            )
            if recovered:
                logger.info("Realtime connection was closed by the peer")
        except websockets.exceptions.ConnectionClosedError as e:
            received_code = getattr(getattr(e, "rcvd", None), "code", None)
            received_reason = getattr(getattr(e, "rcvd", None), "reason", None)
            sent_code = getattr(getattr(e, "sent", None), "code", None)
            status_code, status_details = _classify_peer_close(
                received_code,
                received_reason,
            )
            recovered = await self._recover_receive_loop_disconnect(
                message_ws,
                message_generation,
                "realtime connection closed unexpectedly",
                status_code=status_code,
                status_details=status_details,
            )
            if recovered:
                logger.warning(
                    "Realtime connection closed unexpectedly "
                    "(received_code=%s sent_code=%s)",
                    received_code,
                    sent_code,
                )
        except asyncio.TimeoutError:
            await self._recover_receive_loop_disconnect(
                message_ws,
                message_generation,
                "realtime connection timeout",
                status_code="CONNECTION_TIMEOUT",
            )
        except Exception as e:
            if (
                not self._still_owns_connection(message_generation)
                or self.ws is not message_ws
            ):
                logger.info(
                    "Retired realtime receive loop failed after replacement; "
                    "ignoring its handler exception"
                )
                return
            await self._close_failed_transport(
                f"realtime message handling failed: {type(e).__name__}"
            )
            logger.error(f"Error in message handling: {str(e)}")
            raise
        else:
            await self._recover_receive_loop_disconnect(
                message_ws,
                message_generation,
                "realtime message stream ended",
                status_code="CHARACTER_DISCONNECTED",
            )

    def _transport_detached_for_teardown(
        self,
        connection_generation: int,
    ) -> bool:
        """Whether the current generation's close path already seized a socket."""

        return bool(
            connection_generation == self._connection_generation
            and self.ws is None
            and (
                self._close_task is not None
                or self._failed_transport_close_task is not None
            )
        )

    def _note_raw_speech_started_scope(self, item_id: Any) -> None:
        """Record that a ``speech_started`` already scoped this utterance."""

        utterance = str(item_id) if item_id else ""
        if not utterance:
            # ONLY an id-less speech_started arms the fallback marker. An
            # identified one is answered from the id list below, and an
            # identified transcript deliberately does not consume the marker
            # -- so arming it here would leave it set for the rest of the
            # connection, and the next id-less transcript would read as
            # already scoped. That is the original stale-marker bug, rebuilt
            # one turn further along.
            self._raw_speech_started_scope_pending_transcript = True
            return
        scoped = self._raw_speech_started_scoped_item_ids
        if utterance in scoped:
            return
        scoped.append(utterance)
        del scoped[:-_RAW_SCOPED_UTTERANCE_MEMORY]

    def _raw_transcript_was_already_scoped(self, item_id: Any) -> bool:
        """Whether this transcript's OWN utterance already began a turn here.

        The question the no-server-VAD fallback has to answer is per
        utterance, and the flag alone answers a different one -- "did any
        ``speech_started`` happen and not get a transcript yet". Those come
        apart on a proxy that emits ``speech_started`` for one turn, drops
        that turn's transcript, then drops ``speech_started`` for the NEXT
        turn: nothing clears the flag between them (neither
        ``speech_stopped`` nor ``response.done`` touches it), so the next
        turn's transcript reads as already scoped, ``note_user_turn_started``
        is skipped, and a cancellation-resistant tool from the previous turn
        can still inject its result into the new one.

        None of the cheap lifecycle expiry points fix that. The tool scope
        does not advance between those two turns -- that IS the failure --
        and ``_turn_epoch`` moves at ``speech_stopped``, before the transcript
        normally arrives, so keying on either would fire a spurious extra
        ``note_user_turn_started`` every healthy turn. Clearing at
        ``speech_stopped`` breaks the same normal order. Clearing at
        ``response.done`` would only hold if a transcript could never land
        after it, which is not something these proxies promise: input
        transcription runs as its own job alongside the response.

        So the marker is scoped by utterance instead of by lifecycle. The
        remembered ids are bounded and matching one is a no-op, which also
        makes a transcript that arrives late -- after its successor's turn has
        already begun -- stop re-advancing the scope.
        """

        utterance = str(item_id) if item_id else ""
        if utterance:
            # Answered from the id list ALONE, and deliberately without
            # consuming the id-less fallback marker. Transcription is
            # asynchronous, so on a proxy that omitted `item_id` for the
            # NEWER utterance an older identified transcript can arrive
            # first; consuming the marker there would make that newer
            # utterance's own id-less transcript read as an unscoped turn and
            # retire tool work that legitimately belongs to it.
            return utterance in self._raw_speech_started_scoped_item_ids
        # Neither side carries identity: this is the pre-identity shape, and
        # the flag is the only answer available. It is one-shot, so consume it.
        already_pending = self._raw_speech_started_scope_pending_transcript
        self._raw_speech_started_scope_pending_transcript = False
        return already_pending

    def _on_connection_attached(self) -> None:
        """Mark a replacement connection as live and hand it the teardown latches.

        A close task closes the socket it detached, so it is finished with the
        previous connection's socket the moment a replacement is installed —
        and a latched finished task would make the new connection's close a
        no-op. This has to happen where the socket is assigned, not at the top
        of connect(): a close landing in the connect await window would
        otherwise run to completion against no socket at all, and the
        replacement would attach behind an already-finished latch that every
        later close() just re-awaits. No await between the assignment and this
        call, so no third party can observe the pair half-applied.

        The generation bump is the other half. An unfinished predecessor is not
        cancelled — it owns the retired socket and must finish closing it — but
        everything else it would touch (the silence scalars connect() just
        primed, the shared audio processor, the Gemini session) is client-wide
        state that now belongs to the replacement. Teardowns compare the
        generation after each await and keep their hands off what is no longer
        theirs.
        """

        self._connection_generation += 1
        self._clear_input_route_identities()
        self._advance_tool_scope()
        # A pending proactive outcome belongs to the connection that created
        # it. Left in place it makes the REPLACEMENT reject its own proactive
        # work as "another Gemini proactive inject is pending" until the 60s
        # expiry or the quarantine settles the predecessor's -- and a stalled
        # retired receive loop or context close can push that past anything
        # useful. Retire it here; the caller re-queues the callback and the
        # replacement gets to run it. An outcome the replacement creates
        # afterwards carries the new generation and is untouched.
        retired_outcome_owner = getattr(
            self, "_gemini_proactive_outcome_owner", None
        )
        if (
            retired_outcome_owner is not None
            and retired_outcome_owner[0] != self._connection_generation
        ):
            self._settle_gemini_proactive_inject(
                error_msg=(
                    "Gemini proactive inject retired by a replacement connection"
                ),
                expected_connection_generation=retired_outcome_owner[0],
                expected_provider_session=retired_outcome_owner[1],
                expected_outcome_token=retired_outcome_owner[2],
            )
        # 取消欠账同样属于退役的那条连接：替换连接上的第一条终结是**它自己**的，
        # 被上一条连接的欠账吃掉就等于新回合的 token 没人结算。与上面退役 proactive
        # outcome 同一判据。隔离重连那条路上 connect() 跑在 handle_interruption()
        # 之前（_responses.py 的 prepare_external_voice_turn），不会抹掉刚武装的那笔。
        self._gemini_cancelled_terminal_pending = False
        self._gemini_cancelled_terminal_deadline = None
        self._gemini_cancelled_terminal_awaiting_delivery = False
        self._gemini_cancelled_terminal_id = None
        self._raw_speech_started_scope_pending_transcript = False
        self._raw_speech_started_scoped_item_ids = []
        # Provider response identity and interruption state belong to the
        # retired connection. Some raw proxies never announce responses, so a
        # stale interrupt would mute the replacement for its entire lifetime,
        # while a stale response id would make its first barge-in cancel a
        # response that existed only on the old socket. Reuse the turn reset so
        # transcript/audio state is fresh too; its image branch deliberately
        # preserves a completed annotation for an unconsumed cached frame.
        self._clear_turn_response_state()
        # Keep this separate from the ordinary response.done reset: no-VAD
        # tool results still need their provider-turn snapshot after done, but
        # a replacement connection must never inherit the retired snapshot.
        self._current_turn_host_id = None
        self._reset_per_turn_output_state()
        self._current_response_transcript = ""
        self._is_first_text_chunk = True
        self._is_first_transcript_chunk = True
        self._close_task = None
        self._failed_transport_close_task = None
        self._gemini_close_task = None
        # A predecessor's unclaimed abort is not the replacement's to report.
        # The generation stamp already refuses it; dropping it here keeps the
        # latch from outliving the connection it describes.
        self._local_failure_recovery = None

    def _still_owns_connection(self, generation) -> bool:
        """Whether the connection a teardown seized is still the client's.

        The rule this expresses has to hold at EVERY await boundary inside a
        teardown, not just the first: the teardown outlives its caller by
        design, so a replacement can attach during any one of them, and from
        that moment the client's shared state (the arbiter, the fatal flag, the
        silence scalars, the audio processor, the Gemini session) is the
        replacement's. What the teardown seized up front stays its own to
        release; everything else it must leave alone. Any await added below is
        a new place to ask this.
        """

        return self._connection_generation == generation

    async def _recover_receive_loop_disconnect(
        self,
        message_ws,
        generation,
        reason: str,
        *,
        status_code: str,
        status_details: dict[str, Any] | None = None,
    ) -> bool:
        """Retire a peer-failed connection and request the existing recovery.

        ``close()`` detaches its socket synchronously before awaiting the close
        handshake. Therefore an ending receive loop still owns ``self.ws`` only
        when the peer (or the network) ended the live connection first. An old
        loop ending after a manager close or replacement attach must be silent:
        reporting it as a fresh provider failure would tear down the successor.
        """

        if not self._still_owns_connection(generation) or self.ws is not message_ws:
            local_failure = self._consume_local_failure_recovery(generation)
            if local_failure is None:
                if not self._transport_detached_for_teardown(
                    generation,
                ):
                    # A replacement can attach before the retired receive
                    # iterator reaches EOF. Release the socket captured by
                    # this loop without touching successor-wide fatal state.
                    await self._abort_failed_transport(
                        reason,
                        message_ws,
                        generation,
                    )
                logger.info(
                    "Realtime receive loop ended after its transport was already "
                    "closed or replaced; no connection error will be reported"
                )
                return False
            # A local component (the arbiter's fail-close, a fatal send) tore
            # this transport down and nobody told the manager. Finish the host
            # cleanup the aborting caller could not do from its own stack, and
            # route it through the ordinary disconnect recovery. Deliberately
            # NOT reclassified from the local close result: the primary cause
            # is the reason the aborting caller already logged, and the CLOSE
            # 1000 handshake outcome must not be shown as a provider API error.
            logger.warning(
                "Realtime transport was aborted locally (%s); requesting "
                "session recovery",
                local_failure,
            )
            await self._close_failed_transport(local_failure)
            if not self._still_owns_connection(generation):
                return False
            self._schedule_connection_error(
                "CHARACTER_DISCONNECTED",
                generation,
            )
            return True

        await self._close_failed_transport(reason)
        if not self._still_owns_connection(generation):
            return False
        self._schedule_connection_error(
            status_code,
            generation,
            status_details=status_details,
        )
        return True

    def _consume_local_failure_recovery(self, generation) -> str | None:
        """Take the pending local-abort reason, if it belongs to ``generation``.

        One-shot on purpose: the receive loop that owned the aborted socket is
        the only party that may act on it, and a retired loop or a replacement
        connection must not inherit a predecessor's failure.
        """

        pending = getattr(self, "_local_failure_recovery", None)
        if pending is None:
            return None
        pending_generation, reason = pending
        if pending_generation != generation:
            return None
        self._local_failure_recovery = None
        return reason

    def _schedule_connection_error(
        self,
        status_code: str,
        generation,
        *,
        status_details: dict[str, Any] | None = None,
    ) -> None:
        """Run manager recovery outside the receive loop that it must cancel."""

        callback = self.on_connection_error
        if callback is None:
            return

        async def _notify() -> None:
            if not self._still_owns_connection(generation):
                return
            try:
                details = dict(status_details or {})
                details["connection_generation"] = generation
                await callback(
                    json.dumps(
                        {
                            "code": status_code,
                            "details": details,
                        }
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Realtime connection recovery callback failed: %s",
                    type(exc).__name__,
                )

        self._fire_task(_notify())

    async def _own_teardown(self, slot: str, detach):
        """Await a teardown that this client owns, not the caller.

        Both close paths detach ``self.ws`` first and only then await the
        arbiter shutdown — deliberately, so no ticket can outlive the socket.
        That ordering also means a cancel landing in the middle takes the only
        reference to a still-open socket with it: ``self.ws`` is already None,
        so a retry closes nothing and reports success. Every real canceller is
        internal (a hot-swap final task cancelled by a concurrent
        start/end_session), so this is reachable without anyone injecting one.

        Running the teardown as a task the client holds, and awaiting it
        through ``shield``, separates the two: the caller's cancel stops the
        waiting, the closing continues, and a later caller awaits the same
        task rather than a fresh one against an emptied field.

        ``detach`` is a plain function — called HERE, synchronously, before the
        task exists. A coroutine's body does not run at ``create_task`` time,
        so a detach written inside the teardown would be scheduled, not
        performed: a connect() parked one await away can attach its
        replacement and clear the latch first, and the teardown then wakes up
        and closes the brand-new socket it finds in ``self.ws``. Detaching in
        the caller's own step keeps the seizure exactly where it used to be,
        back when close() was an ordinary coroutine. ``detach`` returns the
        coroutine to run, with everything it seized already bound.
        """

        task = getattr(self, slot, None)
        if task is None:
            task = asyncio.create_task(detach())
            setattr(self, slot, task)
        await asyncio.shield(task)

    async def _close_failed_transport(self, reason: str) -> None:
        """Fail response tickets and atomically detach the failed socket."""

        # Latched before the task starts: callers check this flag to stop
        # sending on a socket that is on its way out, and a scheduling gap
        # before the task's first line must not be a window where they still
        # think the transport is healthy.
        self._fatal_error_occurred = True
        await self._own_teardown(
            "_failed_transport_close_task",
            lambda: self._detach_for_failed_transport(reason),
        )

    def _detach_for_failed_transport(self, reason: str):
        generation = self._connection_generation
        ws, self.ws = self.ws, None
        tool_tasks = self._advance_tool_scope()
        return self._close_failed_transport_impl(reason, generation, ws, tool_tasks)

    async def _close_failed_transport_impl(
        self,
        reason: str,
        generation,
        ws,
        tool_tasks=(),
    ) -> None:
        await self._await_retired_tool_tasks(tool_tasks)
        # The fatal flag is the retired connection's, and the wrapper has
        # already set it. Re-asserting it here would re-condemn a replacement
        # that attached in between — connect() clears the flag on purpose, and
        # a live connection marked fatal rejects every later send.
        if self._still_owns_connection(generation):
            response_arbiter = getattr(self, "_response_arbiter", None)
            if response_arbiter is not None:
                # Shared across connections, and connect() has already reopened
                # it for the replacement. Shutting it down now would fail the
                # new connection's tickets over a socket that is fine.
                await response_arbiter.shutdown(reason)
        await self._abort_failed_transport(reason, ws, generation)

    async def _abort_failed_transport(
        self,
        reason: str,
        ws=_ATTACHED_TRANSPORT,
        generation=None,
    ) -> None:
        """Detach, when needed, and physically close a failed raw WebSocket.

        The sentinel ``ws`` marks the arbiter's own entry point: it seizes the
        attached socket itself, where ``_close_failed_transport_impl`` hands
        over a socket it already seized. Only the former leaves nobody holding
        the failure — the receive loop is about to wake up on a socket it no
        longer owns and, without the latch armed here, would exit silently and
        strand the manager on a live session over a dead transport.
        """

        attached_transport = ws is _ATTACHED_TRANSPORT
        if attached_transport:
            generation = getattr(self, "_connection_generation", None)
            ws, self.ws = self.ws, None
            self._fatal_error_occurred = True
            # Arm recovery before the first await. The receive loop can wake as
            # soon as the socket is detached and must still be able to report
            # the local abort to the manager while retired tool tasks unwind.
            self._local_failure_recovery = (
                0 if generation is None else generation,
                reason,
            )
            tool_tasks = self._advance_tool_scope()
            await self._await_retired_tool_tasks(tool_tasks)
        elif generation is None or self._still_owns_connection(generation):
            self._fatal_error_occurred = True
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:
                logger.debug(
                    "failed transport close also failed (%s): %s",
                    reason,
                    type(exc).__name__,
                )

    async def close(self) -> None:
        """Close the WebSocket connection."""
        # Before the teardown, and deliberately not inside ``_detach_for_close``
        # (which is synchronous by contract). These copies belong to THIS
        # instance's own set, so a replacement session attaching mid-teardown
        # gets a fresh one and nothing races. Left alive, a copy parked in the
        # cross-loop handoff keeps its base64 and publishes a frame from a
        # retired session if the bridge recovers -- the offline client is
        # drained the same way, in ``_cancel_bus_copies``.
        await self._cancel_frame_copies()
        await self._own_teardown("_close_task", self._detach_for_close)

    def _detach_for_close(self):
        """Seize this connection's resources, then hand them to the teardown.

        Synchronous on purpose (see ``_own_teardown``), and it takes everything
        the teardown will release in one uninterrupted step: the teardown
        outlives its caller by design, so connect() is free to attach a
        replacement while it is parked in the arbiter shutdown, and anything
        re-read off the client after that point can already be the
        replacement's. The Gemini context comes along for the same reason —
        ``_connect_gemini()`` overwrites the field, and the retired SDK
        connection would have no one left to exit it.
        """

        self._cancel_session_update_ack_waiters()
        generation = self._connection_generation
        ws, self.ws = self.ws, None
        # The manager is the one closing, so it already knows this session is
        # over: an abort latched just before this must not also fire recovery.
        self._local_failure_recovery = None
        silence_check_task, self._silence_check_task = self._silence_check_task, None
        gemini_context = self._gemini_context_manager
        gemini_close_task = self._gemini_close_task
        gemini_proactive_submit_task = getattr(
            self,
            "_gemini_proactive_submit_task",
            None,
        )
        gemini_external_submit_task = getattr(
            self,
            "_gemini_external_submit_task",
            None,
        )
        tool_tasks = self._advance_tool_scope()
        if (
            self._is_gemini
            and gemini_context is not None
            and gemini_close_task is None
        ):
            gemini_close_task = asyncio.create_task(
                self._close_gemini_context(
                    gemini_context,
                    self._gemini_session,
                    tool_tasks,
                ),
                name="realtime-retired-gemini-context-close",
            )
        return self._close_impl(
            generation,
            ws,
            silence_check_task,
            gemini_context,
            gemini_close_task,
            gemini_proactive_submit_task,
            gemini_external_submit_task,
            tool_tasks,
        )

    async def _close_impl(
        self,
        generation,
        ws,
        silence_check_task,
        gemini_context,
        gemini_close_task,
        gemini_proactive_submit_task,
        gemini_external_submit_task,
        tool_tasks=(),
    ) -> None:
        # 先取消在飞的 Gemini 提交，再等退休的工具调用收尾：前者是可能一直挂着的
        # SDK 写，把它留到后面会让整段拆除跟着它一起等。取消逻辑只有
        # _gemini_support._cancel_gemini_submit_tasks 一份，别在这里再抄一遍。
        await self._cancel_gemini_submit_tasks(
            gemini_proactive_submit_task,
            gemini_external_submit_task,
        )
        await self._await_retired_tool_tasks(tool_tasks)
        response_arbiter = getattr(self, "_response_arbiter", None)
        if response_arbiter is not None and self._still_owns_connection(generation):
            # The arbiter is shared across connections, not owned by one. If a
            # replacement attached between the caller's seizure and this task's
            # first line, connect() has already reopened it — shutting it down
            # here would fail the live connection's tickets while its socket
            # stays perfectly healthy.
            await response_arbiter.shutdown("realtime client closed")

        # 取消静默检测任务
        if silence_check_task:
            silence_check_task.cancel()
            try:
                await silence_check_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error cancelling silence check task: {e}")

        if not self._still_owns_connection(generation):
            # A replacement attached while this teardown ran. Everything below
            # is client-wide — the silence scalars connect() has just primed,
            # the audio processor the new connection is already feeding, the
            # Gemini session it installed — and none of it is ours to release.
            # What we seized still is.
            logger.info(
                "Realtime close: a replacement connection attached; releasing only the retired connection"
            )
            await self._release_retired_connection(ws, gemini_context, gemini_close_task)
            return

        # 重置静默超时相关状态
        self._silence_timeout_triggered = False
        self._last_speech_time = None
        self._silence_reset_pending = False
        self._last_silence_clear_speech_time = 0.0
        self._last_local_loud_time = 0.0
        self._client_vad_active = False
        self._client_vad_last_speech_time = 0.0
        self._speech_detect_start = 0.0
        self._rnnoise_vad_active = False
        self._user_recent_activity_time = 0.0
        self._ai_recent_activity_time = 0.0

        # Wait for any executor-owned chunk to finish before releasing the
        # session's RNNoise native state and soxr streaming buffers.
        await self._close_audio_processor(generation)

        if not self._still_owns_connection(generation):
            # Waiting for the audio lock is an await like any other, and this
            # is the last one before the release below reads the client again:
            # ``_close_gemini()`` would exit the replacement's context — the
            # session a successful reconnect just installed.
            logger.info(
                "Realtime close: a replacement connection attached; releasing only the retired connection"
            )
            await self._release_retired_connection(ws, gemini_context, gemini_close_task)
            return

        # Gemini uses different cleanup
        if self._is_gemini:
            if gemini_close_task is not None:
                await asyncio.shield(gemini_close_task)
            else:
                await self._close_gemini()
            return

        await self._release_retired_connection(ws, gemini_context, gemini_close_task)

    async def _release_retired_connection(
        self,
        ws,
        gemini_context=None,
        gemini_close_task=None,
    ) -> None:
        """Physically release the connection a teardown seized."""

        if self._is_gemini:
            # A Gemini session is released through the context manager that
            # opened it, not by closing a socket. On the replacement path that
            # context is no longer reachable from the client — connect()
            # overwrote the field — so the reference we seized is the only one
            # left, and dropping it would leave the SDK connection open with
            # nobody to exit it.
            if gemini_close_task is not None:
                # Already being exited by an in-flight teardown of its own
                # (the proactive quarantine close); awaiting it is how we avoid
                # a second __aexit__ on the same one-shot context.
                await asyncio.shield(gemini_close_task)
            elif gemini_context is not None:
                await self._close_gemini_context(gemini_context, ws)
            return
        if ws:
            try:
                # 连接时已设 close_timeout=2s：远端超时未回 CLOSE 帧时，
                # websockets 内部会自行 abort transport 强制关闭，
                # 在兼容慢代理的同时保持清理等待有界。
                await ws.close()
            except Exception as e:
                logger.error(f"Error closing websocket: {e}")
            finally:
                logger.info("WebSocket connection closed")
        else:
            logger.warning("WebSocket connection is already closed or None")
