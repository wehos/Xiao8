"""Independent-ASR preprocessing, buffering, and provider dispatch."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

if TYPE_CHECKING:
    from .lifecycle import VoiceTurnToken


class AudioRingBuffer:
    """Retain the newest fixed-duration mono PCM16 audio without disk writes."""

    def __init__(self, *, capacity_ms: int, sample_rate_hz: int = 16_000) -> None:
        if capacity_ms <= 0:
            raise ValueError("capacity_ms must be positive")
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        self._sample_rate_hz = sample_rate_hz
        self._capacity_bytes = sample_rate_hz * 2 * capacity_ms // 1_000
        self._capacity_bytes -= self._capacity_bytes % 2
        if self._capacity_bytes <= 0:
            raise ValueError("capacity_ms is too small for the sample rate")
        self._audio = bytearray()

    @property
    def duration_ms(self) -> int:
        return len(self._audio) * 1_000 // (self._sample_rate_hz * 2)

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

    def append(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int | None = None,
    ) -> bytes:
        if not isinstance(pcm16, bytes):
            raise TypeError("PCM16 audio must be bytes")
        if len(pcm16) % 2:
            raise ValueError("PCM16 audio must contain complete samples")
        effective_rate = sample_rate_hz or self._sample_rate_hz
        if effective_rate != self._sample_rate_hz:
            raise ValueError("audio sample rate does not match the ring buffer")
        if not pcm16:
            return b""

        self._audio.extend(pcm16)
        overflow = len(self._audio) - self._capacity_bytes
        if overflow <= 0:
            return b""
        overflow += overflow % 2
        dropped = bytes(self._audio[:overflow])
        del self._audio[:overflow]
        return dropped

    def peek(self) -> bytes:
        return bytes(self._audio)

    def drain(self) -> bytes:
        payload = bytes(self._audio)
        self._audio.clear()
        return payload

    def clear(self) -> None:
        self._audio.clear()


@dataclass(frozen=True, slots=True)
class AsrActivateCommand:
    generation: int
    turn_token: VoiceTurnToken
    session_ref: Any
    buffered_pcm16: bytes
    sample_rate_hz: int


@dataclass(frozen=True, slots=True)
class AsrAudioCommand:
    generation: int
    turn_token: VoiceTurnToken
    session_ref: Any
    sequence_no: int
    pcm16: bytes
    sample_rate_hz: int


@dataclass(frozen=True, slots=True)
class AsrSealCommand:
    generation: int
    turn_token: VoiceTurnToken
    session_ref: Any
    after_sequence: int


_Command: TypeAlias = AsrActivateCommand | AsrAudioCommand | AsrSealCommand
_Validator: TypeAlias = Callable[["VoiceTurnToken", Any], bool]
_WireCallback: TypeAlias = Callable[["VoiceTurnToken", Any, int], Awaitable[None]]
_FailureCallback: TypeAlias = Callable[["VoiceTurnToken", BaseException], Awaitable[None]]
_CloseSessionCallback: TypeAlias = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class AudioRetirementReceipt:
    """Proof that one dispatcher transport was fenced and retired."""

    transport_generation: int
    discarded_commands: int
    active_writer_joined: bool
    session_closed: bool


class AsrAudioDispatcher:
    """Serialize all writes for one logical turn before its seal barrier."""

    def __init__(
        self,
        *,
        validator: _Validator,
        on_wire_audio: _WireCallback,
        on_failure: _FailureCallback,
        max_commands: int = 256,
    ) -> None:
        if max_commands <= 0:
            raise ValueError("ASR audio command capacity must be positive")
        self._validator = validator
        self._on_wire_audio = on_wire_audio
        self._on_failure = on_failure
        self._queue: asyncio.Queue[_Command] = asyncio.Queue(maxsize=max_commands)
        self._worker: asyncio.Task[None] | None = None
        self._failure_tasks: set[asyncio.Task[None]] = set()
        self._generation = 0
        self._turn_token: VoiceTurnToken | None = None
        self._session_ref: Any = None
        self._state: Literal["idle", "active", "sealed", "aborted"] = "idle"
        self._last_sequence = 0
        self._active_writer_generation: int | None = None
        self._active_writer_done = asyncio.Event()
        self._active_writer_done.set()
        # Keyed by id(command). Sound because no path leaves an entry alive
        # past its command: _put writes the key AFTER put_nowait with no await
        # between (Queue.put_nowait only schedules a wakeup, never runs the
        # getter), _run pops it as the first statement after get() binds the
        # command locally, abort() pops per drained command in an await-free
        # loop, and the QueueFull branch returns before writing a key at all.
        # Bounded by max_commands.
        self._enqueued_at: dict[int, float] = {}
        self.asr_audio_command_queue_ms = 0
        self.asr_abort_discarded_command_count = 0
        self.provider_wire_sequence = 0

    @property
    def active_turn(self) -> VoiceTurnToken | None:
        return self._turn_token if self._state in {"active", "sealed"} else None

    @property
    def transport_generation(self) -> int:
        return self._generation

    def activate(
        self,
        turn_token: VoiceTurnToken,
        session_ref: Any,
        buffered_pcm16: bytes,
        *,
        sample_rate_hz: int = 16_000,
    ) -> bool:
        if sample_rate_hz <= 0 or len(buffered_pcm16) % 2:
            raise ValueError("ASR_ACTIVATE_INVALID_PCM")
        self._generation += 1
        self._turn_token = turn_token
        self._session_ref = session_ref
        self._state = "active"
        self._last_sequence = 0
        return self._put(
            AsrActivateCommand(
                self._generation,
                turn_token,
                session_ref,
                buffered_pcm16,
                sample_rate_hz,
            )
        )

    def enqueue_audio(
        self,
        turn_token: VoiceTurnToken,
        session_ref: Any,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        sequence_no: int,
    ) -> bool:
        if not pcm16:
            return True
        if len(pcm16) % 2 or sample_rate_hz <= 0 or sequence_no <= 0:
            raise ValueError("ASR_AUDIO_COMMAND_INVALID")
        if (
            self._state != "active"
            or self._turn_token != turn_token
            or self._session_ref is not session_ref
            or sequence_no <= self._last_sequence
        ):
            return False
        self._last_sequence = sequence_no
        return self._put(
            AsrAudioCommand(
                self._generation,
                turn_token,
                session_ref,
                sequence_no,
                pcm16,
                sample_rate_hz,
            )
        )

    def seal(
        self,
        turn_token: VoiceTurnToken,
        session_ref: Any,
        *,
        after_sequence: int,
    ) -> bool:
        if (
            self._state != "active"
            or self._turn_token != turn_token
            or self._session_ref is not session_ref
            or after_sequence < self._last_sequence
        ):
            return False
        self._state = "sealed"
        return self._put(
            AsrSealCommand(
                self._generation,
                turn_token,
                session_ref,
                after_sequence,
            )
        )

    def abort(self, turn_token: VoiceTurnToken | None = None) -> None:
        if turn_token is not None and self._turn_token != turn_token:
            return
        discarded = 0
        while True:
            try:
                command = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._enqueued_at.pop(id(command), None)
            self._queue.task_done()
            discarded += 1
        self.asr_abort_discarded_command_count += discarded
        self._generation += 1
        self._turn_token = None
        self._session_ref = None
        self._state = "aborted"
        self._last_sequence = 0

    async def abort_and_join(
        self,
        turn_token: VoiceTurnToken | None = None,
        *,
        close_session: _CloseSessionCallback | None = None,
        transport_generation: int | None = None,
    ) -> AudioRetirementReceipt:
        """Fence one transport, discard its queue, and join its active writer.

        ``abort()`` is deliberately executed before the first await so the
        generation fence is visible immediately to enqueue and worker paths.
        Provider close runs alongside the writer join because closing the
        socket may be what releases an already-entered ``stream_audio()``.
        Callers own the timeout and quarantine policy.
        """

        retired_generation = self._generation
        retired_turn = self._turn_token
        if (
            transport_generation is not None
            and transport_generation != retired_generation
        ):
            raise RuntimeError("ASR_AUDIO_RETIREMENT_GENERATION_MISMATCH")
        if turn_token is not None and retired_turn != turn_token:
            raise RuntimeError("ASR_AUDIO_RETIREMENT_TURN_MISMATCH")

        discarded_before = self.asr_abort_discarded_command_count
        self.abort(turn_token)
        discarded_commands = (
            self.asr_abort_discarded_command_count - discarded_before
        )

        async def join_active_writer() -> bool:
            # abort() has synchronously task_done()'d every queued command, so
            # join now waits only for the command already owned by the worker.
            # Using Queue.join is the proof carried by active_writer_joined.
            await self._queue.join()
            return True

        async def close_provider_session() -> bool:
            if close_session is None:
                return False
            try:
                await close_session()
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            return True

        active_writer_joined, session_closed = await asyncio.gather(
            join_active_writer(),
            close_provider_session(),
        )
        return AudioRetirementReceipt(
            transport_generation=retired_generation,
            discarded_commands=discarded_commands,
            active_writer_joined=active_writer_joined,
            session_closed=session_closed,
        )

    async def wait_idle(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        self.abort()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def _put(self, command: _Command) -> bool:
        self._ensure_worker()
        try:
            self._queue.put_nowait(command)
        except asyncio.QueueFull:
            self.abort(command.turn_token)
            self._dispatch_failure(
                command.turn_token,
                RuntimeError("ASR_AUDIO_COMMAND_BACKPRESSURE"),
                name="asr-audio-command-backpressure",
            )
            return False
        self._enqueued_at[id(command)] = time.monotonic()
        return True

    def _dispatch_failure(
        self,
        turn_token: VoiceTurnToken,
        error: BaseException,
        *,
        name: str,
    ) -> None:
        """Run the failure callback outside the worker task it may tear down."""
        failure_task = asyncio.create_task(
            self._on_failure(turn_token, error),
            name=name,
        )
        self._failure_tasks.add(failure_task)
        failure_task.add_done_callback(self._failure_tasks.discard)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name="independent-asr-audio-dispatcher"
            )

    async def _run(self) -> None:
        while True:
            command = await self._queue.get()
            self._active_writer_generation = command.generation
            self._active_writer_done.clear()
            try:
                queued_at = self._enqueued_at.pop(id(command), None)
                if queued_at is not None:
                    self.asr_audio_command_queue_ms = int(
                        (time.monotonic() - queued_at) * 1_000
                    )
                if not self._command_is_current(command):
                    continue
                if isinstance(command, AsrSealCommand):
                    await command.session_ref.signal_user_activity_end()
                    if self._command_is_current(command):
                        self._state = "idle"
                        self._turn_token = None
                        self._session_ref = None
                    continue
                payload = (
                    command.buffered_pcm16
                    if isinstance(command, AsrActivateCommand)
                    else command.pcm16
                )
                max_bytes = command.sample_rate_hz * 2
                for offset in range(0, len(payload), max_bytes):
                    if not self._command_is_current(command):
                        break
                    chunk = payload[offset : offset + max_bytes]
                    await command.session_ref.stream_audio(
                        chunk,
                        sample_rate_hz=command.sample_rate_hz,
                    )
                    if not self._command_is_current(command):
                        break
                    self.provider_wire_sequence += 1
                    await self._on_wire_audio(
                        command.turn_token,
                        command.session_ref,
                        len(chunk),
                    )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                if not self._command_is_current(command):
                    continue
                self.abort(command.turn_token)
                self._dispatch_failure(
                    command.turn_token,
                    exc,
                    name="asr-audio-dispatch-failure",
                )
            finally:
                self._active_writer_generation = None
                self._active_writer_done.set()
                self._queue.task_done()

    def _command_is_current(self, command: _Command) -> bool:
        before_validator = bool(
            command.generation == self._generation
            and self._state in {"active", "sealed"}
            and self._turn_token == command.turn_token
            and self._session_ref is command.session_ref
        )
        if not before_validator or not self._validator(
            command.turn_token,
            command.session_ref,
        ):
            return False
        # The validator is user-supplied synchronous code and can itself trip
        # a fence. Recheck the dispatcher-owned identity after it returns so a
        # DENY between validation and provider invocation cannot start a write.
        return bool(
            command.generation == self._generation
            and self._state in {"active", "sealed"}
            and self._turn_token == command.turn_token
            and self._session_ref is command.session_ref
        )
