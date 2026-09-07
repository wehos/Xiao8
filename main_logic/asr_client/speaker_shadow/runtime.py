"""Bounded, fail-open runtime for observation-only speaker scoring."""

from __future__ import annotations

import asyncio
import inspect
import math
import multiprocessing
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Literal

from .diagnostics import SpeakerShadowDiagnostic

from .contracts import (
    CompletionCallback,
    EvidenceCallback,
    MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES,
    MAX_SPEAKER_SHADOW_FRAME_PCM_BYTES,
    MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES,
    ObservationCallback,
    SpeakerShadowBackend,
    SpeakerShadowBackendFactory,
    SpeakerShadowBatchReconcileReceipt,
    SpeakerShadowBatchReconcileRequest,
    SpeakerShadowCandidateKey,
    SpeakerShadowCaptureDecisionState,
    SpeakerShadowCaptureDisposition,
    SpeakerShadowCaptureResult,
    SpeakerShadowConfig,
    SpeakerShadowCompletion,
    SpeakerShadowDeferredAnchorReceipt,
    SpeakerShadowDeferredAnchorRequest,
    SpeakerShadowDeferredAnchorStatus,
    SpeakerShadowMetrics,
    SpeakerShadowObservation,
    SpeakerShadowReconcileSource,
    SpeakerShadowReconciliationReceipt,
    SpeakerShadowReconciliationStatus,
    SpeakerShadowScope,
    SPEAKER_SHADOW_SAMPLE_RATE_HZ,
    SpeakerShadowTerminalCoverageReceipt,
    SpeakerShadowTerminalCoverageRequest,
    SpeakerShadowTerminalReason,
)

_HOST_POLL_INTERVAL_SECONDS = 0.005
_HostOperation = Literal["load", "score", "close"]
_DegradedCause = Literal[
    "backend_unavailable",
    "terminal_overflow",
    "completion_overflow",
    "completion_stalled",
    "worker_start_failure",
    "dispatcher_start_failure",
    "resetting",
]


class _FinishState(StrEnum):
    OPEN = "open"
    QUEUED = "queued"
    PROCESSED = "processed"
    ABANDONED = "abandoned"


class _CompletionState(StrEnum):
    NONE = "none"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    ATTEMPTED = "attempted"
    ABANDONED = "abandoned"


class _BackendHostError(RuntimeError):
    pass


class _BackendHostTimeout(_BackendHostError):
    pass


def _backend_host_error_name(exc: BaseException) -> str:
    """Return a non-sensitive error identity safe to cross the process pipe."""

    return type(exc).__name__


def _backend_host_main(
    factory: SpeakerShadowBackendFactory,
    connection: Connection,
    pcm_buffer: Any,
) -> None:
    """Own one blocking backend session inside a killable spawn process."""

    backend: SpeakerShadowBackend | None = None
    factory_closed = False

    def close_owned_resources() -> str | None:
        nonlocal backend, factory_closed
        error_name: str | None = None
        if backend is not None:
            owned_backend, backend = backend, None
            try:
                owned_backend.close()
            except BaseException as exc:  # process boundary must contain backend faults
                error_name = _backend_host_error_name(exc)
        close_factory = getattr(factory, "close", None)
        if not factory_closed and callable(close_factory):
            factory_closed = True
            try:
                close_factory()
            except BaseException as exc:  # process boundary must contain factory faults
                error_name = error_name or _backend_host_error_name(exc)
        return error_name

    try:
        while True:
            try:
                message = connection.recv()
            except EOFError:
                return
            operation = message[0]
            try:
                if operation == "load":
                    if backend is None:
                        backend = factory()
                    connection.send((True, bool(backend.load())))
                    continue
                if operation == "score":
                    if backend is None:
                        raise RuntimeError("backend is not loaded")
                    pcm_length = int(message[1])
                    sample_rate_hz = int(message[2])
                    pcm16 = bytearray(memoryview(pcm_buffer).cast("B")[:pcm_length])
                    try:
                        similarity = float(backend.score(bytes(pcm16), sample_rate_hz))
                    finally:
                        pcm16[:] = b"\x00" * len(pcm16)
                        del pcm16
                    connection.send((True, similarity))
                    continue
                if operation == "close":
                    error_name = close_owned_resources()
                    connection.send((error_name is None, error_name))
                    return
                raise RuntimeError("unsupported backend-host operation")
            except BaseException as exc:  # backend faults stay inside this process
                try:
                    connection.send((False, _backend_host_error_name(exc)))
                except (BrokenPipeError, EOFError, OSError):
                    return
    finally:
        close_owned_resources()
        connection.close()


class _BackendProcessHost:
    """One serial spawn-process host for one backend session."""

    def __init__(
        self,
        *,
        factory: SpeakerShadowBackendFactory,
        terminate_timeout_seconds: float,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        pcm_buffer = context.RawArray("B", MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES)
        process = context.Process(
            target=_backend_host_main,
            args=(factory, child_connection, pcm_buffer),
            name="speaker-shadow-backend",
            daemon=True,
        )
        self._connection: Connection | None = parent_connection
        self._child_connection: Connection | None = child_connection
        # Abandoned host reads keep a strong reference here until the thread
        # they are blocked in unwinds, so the event loop cannot drop them.
        self._pending_responses: set[asyncio.Future[Any]] = set()
        self._pcm_buffer = pcm_buffer
        self._process: BaseProcess | None = process
        self._terminate_timeout_seconds = terminate_timeout_seconds
        self.loaded = False
        self.was_terminated = False
        self.timed_out = False
        self.pcm_bytes_in_use = 0

    @classmethod
    def create_started(
        cls,
        *,
        factory: SpeakerShadowBackendFactory,
        terminate_timeout_seconds: float,
    ) -> _BackendProcessHost:
        """Construct IPC resources and spawn outside the asyncio event loop."""

        host = cls(
            factory=factory,
            terminate_timeout_seconds=terminate_timeout_seconds,
        )
        host.start()
        return host

    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.is_alive()

    @property
    def process_count(self) -> int:
        return int(self.alive)

    def start(self) -> None:
        process = self._process
        child_connection = self._child_connection
        if process is None or child_connection is None:
            raise _BackendHostError("backend host is already closed")
        try:
            process.start()
        except BaseException:
            self._dispose_handles()
            raise
        finally:
            child_connection.close()
            self._child_connection = None

    async def load(self, *, timeout_seconds: float) -> bool:
        available = bool(await self._request("load", timeout_seconds=timeout_seconds))
        self.loaded = available
        return available

    async def score(
        self,
        pcm16: bytes | bytearray,
        *,
        timeout_seconds: float,
    ) -> float:
        if len(pcm16) > MAX_SPEAKER_SHADOW_CANDIDATE_PCM_BYTES:
            raise _BackendHostError("candidate PCM exceeds host buffer")
        if self._pcm_buffer is None:
            raise _BackendHostError("backend host PCM buffer is closed")
        pcm_view = memoryview(self._pcm_buffer).cast("B")
        pcm_view[: len(pcm16)] = pcm16
        self.pcm_bytes_in_use = len(pcm16)
        try:
            return float(
                await self._request(
                    "score",
                    len(pcm16),
                    SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                    timeout_seconds=timeout_seconds,
                )
            )
        finally:
            pcm_view[: len(pcm16)] = b"\x00" * len(pcm16)
            self.pcm_bytes_in_use = 0

    async def close(self, *, timeout_seconds: float) -> bool:
        success = True
        if self.alive:
            try:
                await self._request("close", timeout_seconds=timeout_seconds)
            except _BackendHostError:
                success = False
        self.loaded = False
        if self.alive and not await self._wait_for_exit(timeout_seconds):
            success = False
            await self.terminate()
        await asyncio.to_thread(self._dispose_handles)
        return success

    async def terminate(self) -> None:
        process = self._process
        self.loaded = False
        if process is None:
            await asyncio.to_thread(self._dispose_handles)
            return
        if process.is_alive():
            self.was_terminated = True
            process.terminate()
            if not await self._wait_for_exit(self._terminate_timeout_seconds):
                process.kill()
                if not await self._wait_for_exit(self._terminate_timeout_seconds):
                    raise _BackendHostError("backend host could not be terminated")
        await asyncio.to_thread(self._dispose_handles)

    async def _request(
        self,
        operation: _HostOperation,
        *payload: object,
        timeout_seconds: float,
    ) -> object:
        connection = self._connection
        process = self._process
        if connection is None or process is None or not process.is_alive():
            await asyncio.to_thread(self._dispose_handles)
            raise _BackendHostError("backend host is not alive")
        try:
            connection.send((operation, *payload))
        except (BrokenPipeError, EOFError, OSError) as exc:
            await self.terminate()
            raise _BackendHostError("backend host command failed") from exc

        # One blocking read off the event loop, not a ``poll(0)`` spin. Each
        # zero-timeout poll starts an overlapped pipe read and cancels it in
        # the same breath, and on Windows that cancellation races the very
        # response it asked about: the child answers and stays alive, the
        # answer is swallowed, and the parent spins to a false timeout that no
        # later poll can recover. A plain ``recv`` issues one read and never
        # cancels it. Nothing else waits on this connection, so the read is
        # released either by the response or by the host dying — including the
        # ``terminate`` below, which closes the pipe on timeout and on
        # cancellation.
        response = asyncio.ensure_future(asyncio.to_thread(connection.recv))
        self._pending_responses.add(response)
        response.add_done_callback(self._consume_response_result)
        try:
            done, _ = await asyncio.wait({response}, timeout=timeout_seconds)
        except asyncio.CancelledError:
            await self.terminate()
            raise
        if not done:
            self.timed_out = True
            # Terminating kills the child first, which breaks the pipe and
            # releases the blocked read before the handles are disposed.
            await self.terminate()
            await asyncio.wait({response}, timeout=self._terminate_timeout_seconds)
            raise _BackendHostTimeout(f"backend {operation} timed out")
        try:
            succeeded, value = response.result()
        except (BrokenPipeError, EOFError, OSError) as exc:
            if not process.is_alive():
                await asyncio.to_thread(self._dispose_handles)
                raise _BackendHostError(
                    "backend host exited without a response"
                ) from exc
            await self.terminate()
            raise _BackendHostError("backend host response failed") from exc
        if succeeded:
            return value
        raise _BackendHostError(f"backend operation failed: {value}")

    def _consume_response_result(self, response: asyncio.Future[Any]) -> None:
        """Retire an abandoned host read once its blocked thread unwinds."""

        self._pending_responses.discard(response)
        if response.cancelled():
            return
        response.exception()

    async def _wait_for_exit(self, timeout_seconds: float) -> bool:
        process = self._process
        if process is None:
            return True
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while process.is_alive():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_HOST_POLL_INTERVAL_SECONDS, remaining))
        await asyncio.to_thread(process.join, 0)
        return True

    def _dispose_handles(self) -> None:
        for connection_name in ("_connection", "_child_connection"):
            connection = getattr(self, connection_name)
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
                setattr(self, connection_name, None)
        process = self._process
        if process is not None and not process.is_alive():
            # ``Process.join()`` raises when ``start()`` failed before a PID was
            # assigned, which would otherwise mask the original spawn error.
            if process.pid is not None:
                process.join(timeout=0)
            try:
                process.close()
            except ValueError:
                pass
            self._process = None
        pcm_buffer = self._pcm_buffer
        if pcm_buffer is not None:
            pcm_view = memoryview(pcm_buffer).cast("B")
            pcm_view[:] = b"\x00" * len(pcm_view)
            self._pcm_buffer = None
            self.pcm_bytes_in_use = 0


@dataclass(frozen=True, slots=True)
class _AudioFrame:
    generation: int
    candidate: SpeakerShadowCandidateKey
    token: _CandidateToken
    pcm16: bytearray
    sample_rate_hz: int
    sample_count: int
    rolling_deferred: bool = False


@dataclass(frozen=True, slots=True)
class _CandidateFinished:
    generation: int
    candidate: SpeakerShadowCandidateKey
    token: _CandidateToken


@dataclass(frozen=True, slots=True)
class _CandidateDeferred:
    generation: int
    candidate: SpeakerShadowCandidateKey
    token: _CandidateToken


@dataclass(frozen=True, slots=True)
class _CandidateActivated:
    generation: int
    candidate: SpeakerShadowCandidateKey
    token: _CandidateToken


@dataclass(frozen=True, slots=True)
class _CandidateAnchored:
    generation: int
    candidate: SpeakerShadowCandidateKey
    token: _CandidateToken
    expected_observed_sample_count: int
    discard_prefix_sample_count: int
    receipt: SpeakerShadowDeferredAnchorReceipt


@dataclass(frozen=True, slots=True)
class _CandidatePrefixReconciliation:
    generation: int
    source: SpeakerShadowCandidateKey
    source_token: _CandidateToken
    target: SpeakerShadowCandidateKey
    target_token: _CandidateToken
    source_sample_count: int
    target_sample_count: int
    prefix_sample_count: int
    transferred_sample_count: int
    suffix: SpeakerShadowCandidateKey | None
    suffix_token: _CandidateToken | None


@dataclass(frozen=True, slots=True)
class _ReservedReconcileSource:
    source: SpeakerShadowReconcileSource
    token: _CandidateToken


@dataclass(frozen=True, slots=True)
class _CandidateBatchReconciliation:
    generation: int
    batch_id: int
    sources: tuple[_ReservedReconcileSource, ...]
    target: SpeakerShadowCandidateKey
    target_token: _CandidateToken
    target_was_existing: bool
    target_sample_count: int
    suffix: SpeakerShadowCandidateKey | None
    suffix_token: _CandidateToken | None
    suffix_sample_count: int
    finish_target: bool
    receipt: SpeakerShadowBatchReconcileReceipt


@dataclass(frozen=True, slots=True)
class _CandidateTerminalCoverage:
    generation: int
    batch_id: int
    sources: tuple[SpeakerShadowReconcileSource, ...]
    reserved_sources: tuple[_ReservedReconcileSource, ...]
    target: SpeakerShadowCandidateKey
    target_token: _CandidateToken
    suffix: SpeakerShadowCandidateKey | None
    suffix_token: _CandidateToken | None
    suffix_sample_count: int
    receipt: SpeakerShadowTerminalCoverageReceipt


@dataclass(slots=True)
class _CandidateToken:
    candidate: SpeakerShadowCandidateKey
    sample_rate_hz: int
    accepted_sample_count: int = 0
    terminal_reason: SpeakerShadowTerminalReason | None = None
    finish_state: _FinishState = _FinishState.OPEN
    last_checkpoint_ms: int | None = None
    scored_sample_count: int = 0
    last_delivered_checkpoint_ms: int | None = None
    completion_state: _CompletionState = _CompletionState.NONE
    deferred_requested: bool = False
    defer_processed: bool = False
    scoring_deferred: bool = False
    activation_queued: bool = False
    pcm_frozen: bool = False
    reconciliation_batch_id: int | None = None
    evidence_sequence_no: int = 0
    evidence_complete: bool = True
    evidence_closed: bool = False
    deferred_retained_start_sample_count: int = 0
    anchor_revision: int = 0
    anchor_discard_prefix_sample_count: int | None = None
    anchor_queued: bool = False
    anchor_applied: bool = False
    rolling_buffer_deferred: bool = True
    # Diagnostics only: never consulted by scoring or admission decisions.
    score_attempt_count: int = 0
    score_input_sample_count: int = 0
    score_outcome: str = "not_started"
    finish_sample_count: int | None = None


@dataclass(slots=True)
class _ReconciliationRecord:
    marker: _CandidateBatchReconciliation
    state: Literal["pending", "applied", "failed"] = "pending"
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    completed: bool = False


@dataclass(slots=True)
class _DeferredAnchorRecord:
    marker: _CandidateAnchored
    state: Literal["pending", "applied", "failed"] = "pending"
    settled: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _PreparedExactIntervalRecord:
    """Revocable reservation that has not been published to the worker."""

    marker: _CandidateBatchReconciliation
    source_accepted_sample_count: int
    source_finish_state: _FinishState
    source_pcm_frozen: bool
    source_reconciliation_batch_id: int | None
    reserved_data_slots: int
    suffix_scratch_pcm16: bytearray = field(default_factory=bytearray)
    source_states: tuple[tuple[_FinishState, bool, int | None], ...] = ()


@dataclass(slots=True)
class _TerminalCoverageRecord:
    marker: _CandidateTerminalCoverage
    state: Literal["pending", "applied", "failed"] = "pending"
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    completed: bool = False


@dataclass(slots=True)
class _PreparedTerminalCoverageRecord:
    marker: _CandidateTerminalCoverage
    reserved_data_slots: int
    suffix_scratch_pcm16: bytearray = field(default_factory=bytearray)


@dataclass(slots=True)
class _CandidateBuffer:
    token: _CandidateToken
    sample_rate_hz: int
    pcm16: bytearray
    sample_count: int = 0
    next_checkpoint_index: int = 0
    completion_confirmation_checkpoint_ms: int | None = None
    backend_prewarm_attempted: bool = False
    observed_sample_count: int = 0
    retained_start_sample_count: int = 0
    exact_boundary_deadline: float | None = None

    @property
    def audio_ms(self) -> int:
        return self.sample_count * 1_000 // self.sample_rate_hz


@dataclass(frozen=True, slots=True)
class _FinalizedCandidate:
    finish_state: _FinishState
    terminal_reason: SpeakerShadowTerminalReason
    token: _CandidateToken | None = None

    @property
    def finish_seen(self) -> bool:
        return self.finish_state is _FinishState.PROCESSED


@dataclass(frozen=True, slots=True)
class _CompletionEnvelope:
    generation: int
    candidate: SpeakerShadowCandidateKey
    token: _CandidateToken
    completion: SpeakerShadowCompletion


@dataclass(frozen=True, slots=True)
class _BackendReady:
    generation: int
    available: bool


@dataclass(slots=True)
class _PendingBackendCandidate:
    generation: int
    candidate: SpeakerShadowCandidateKey
    token: _CandidateToken
    buffer: _CandidateBuffer
    allow_frozen: bool = False
    finish: _CandidateFinished | None = None


_STOP = object()
_COMPLETION_STOP = object()
_QueueItem = (
    _AudioFrame
    | _CandidateDeferred
    | _CandidateActivated
    | _CandidateAnchored
    | _CandidatePrefixReconciliation
    | _CandidateBatchReconciliation
    | _CandidateTerminalCoverage
    | _CandidateFinished
    | _BackendReady
    | object
)


class SpeakerShadowRuntime:
    """Score accepted candidate PCM without controlling the ASR path.

    ``submit`` and ``finish_candidate`` are non-blocking. Queue pressure and all
    backend/callback failures terminate shadow work locally and never escape to
    the ASR task graph. Observation callbacks are cancellation-cooperative;
    shutdown uses bounded repeated cancellation so a callback can finish cleanup
    after consuming its first cancellation request.
    """

    def __init__(
        self,
        *,
        backend_factory: SpeakerShadowBackendFactory | None,
        config: SpeakerShadowConfig | None = None,
        on_observation: ObservationCallback | None = None,
        on_completion: CompletionCallback | None = None,
        on_evidence: EvidenceCallback | None = None,
        on_backend_degraded: Callable[[], None] | None = None,
        on_backend_recovered: Callable[[], None] | None = None,
        on_health_changed: Callable[[int, frozenset[str]], None] | None = None,
        on_diagnostic: Callable[[SpeakerShadowDiagnostic], None] | None = None,
    ) -> None:
        self._config = config or SpeakerShadowConfig()
        self._backend_factory = backend_factory
        self._on_backend_degraded = on_backend_degraded
        self._on_backend_recovered = on_backend_recovered
        self._on_health_changed = on_health_changed
        if on_diagnostic is not None and (
            not callable(on_diagnostic)
            or inspect.iscoroutinefunction(on_diagnostic)
            or inspect.iscoroutinefunction(getattr(on_diagnostic, "__call__", None))
        ):
            raise TypeError("SpeakerShadowRuntime diagnostic callback must be synchronous")
        self._on_diagnostic = on_diagnostic
        self._health_revision = 0
        if on_observation is not None and not (
            inspect.iscoroutinefunction(on_observation)
            or inspect.iscoroutinefunction(getattr(on_observation, "__call__", None))
        ):
            raise TypeError("SpeakerShadowRuntime observation callback must be async")
        self._on_observation = on_observation
        if on_completion is not None and not (
            inspect.iscoroutinefunction(on_completion)
            or inspect.iscoroutinefunction(getattr(on_completion, "__call__", None))
        ):
            raise TypeError("SpeakerShadowRuntime completion callback must be async")
        self._on_completion = on_completion
        if on_evidence is not None and (
            not callable(on_evidence)
            or inspect.iscoroutinefunction(on_evidence)
            or inspect.iscoroutinefunction(getattr(on_evidence, "__call__", None))
        ):
            raise TypeError(
                "SpeakerShadowRuntime evidence callback must be synchronous"
            )
        self._on_evidence = on_evidence
        self._metrics = SpeakerShadowMetrics()
        self._would_block_counts = {
            threshold: 0 for threshold in self._config.similarity_thresholds
        }
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(
            maxsize=(
                self._config.queue_capacity + self._config.terminal_queue_capacity + 1
            )
        )
        self._queued_data_item_count = 0
        self._queued_terminal_count = 0
        self._completion_queue: asyncio.Queue[_CompletionEnvelope | object] = (
            asyncio.Queue(maxsize=self._config.completion_queue_capacity + 1)
        )
        self._queued_pcm_bytes = 0
        self._active_pcm_bytes = 0
        self._buffers: OrderedDict[SpeakerShadowCandidateKey, _CandidateBuffer] = (
            OrderedDict()
        )
        self._terminal_pcm_expiry_handle: asyncio.TimerHandle | None = None
        self._finalized: OrderedDict[SpeakerShadowCandidateKey, _FinalizedCandidate] = (
            OrderedDict()
        )
        self._finalized_through: dict[SpeakerShadowScope, tuple[int, int]] = {}
        self._candidate_tokens: OrderedDict[
            SpeakerShadowCandidateKey, _CandidateToken
        ] = OrderedDict()
        self._deferred_anchor_owner = object()
        self._next_deferred_anchor_operation_id = 1
        self._deferred_anchors: OrderedDict[int, _DeferredAnchorRecord] = (
            OrderedDict()
        )
        self._reconciliation_owner = object()
        self._terminal_coverage_owner = object()
        self._next_reconciliation_batch_id = 1
        self._reconciliations: OrderedDict[int, _ReconciliationRecord] = OrderedDict()
        self._prepared_exact_intervals: dict[
            int, _PreparedExactIntervalRecord
        ] = {}
        self._terminal_coverages: OrderedDict[int, _TerminalCoverageRecord] = (
            OrderedDict()
        )
        self._prepared_terminal_coverages: dict[
            int, _PreparedTerminalCoverageRecord
        ] = {}
        self._worker_task: asyncio.Task[None] | None = None
        self._completion_dispatcher_task: asyncio.Task[None] | None = None
        self._completion_dispatch_in_progress = False
        self._observation_task: asyncio.Task[None] | None = None
        self._completion_callback_task: asyncio.Task[None] | None = None
        self._completion_callback_token: _CandidateToken | None = None
        self._detached_callback_tasks: set[asyncio.Task[None]] = set()
        self._detached_completion_tokens: dict[asyncio.Task[None], _CandidateToken] = {}
        self._reset_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._host_start_task: asyncio.Task[_BackendProcessHost] | None = None
        self._backend_load_task: asyncio.Task[None] | None = None
        self._pending_backend_candidates: OrderedDict[
            SpeakerShadowCandidateKey, _PendingBackendCandidate
        ] = OrderedDict()
        self._active_evaluation: tuple[int, SpeakerShadowCandidateKey] | None = None
        self._active_evaluation_terminal = False
        self._active_terminal_token: _CandidateToken | None = None
        self._backend_host: _BackendProcessHost | None = None
        self._load_failure_streak = 0
        self._next_load_attempt_at = 0.0
        self._generation = 0
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._degraded_causes: set[_DegradedCause] = set()
        self._resetting = False
        self._closed = False
        self._factory_closed = False

    @property
    def enabled(self) -> bool:
        """Whether submissions can do work.

        A missing factory is treated exactly like disabled configuration: no
        PCM is queued and no task or model-loading attempt is created.
        """

        return (
            self._config.enabled
            and self._backend_factory is not None
            and not self._closed
        )

    @property
    def generation(self) -> int:
        return self._generation

    def supports_deferred_candidate(
        self,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        """Whether this exact candidate scope may use buffer-only admission."""

        return bool(
            self.enabled
            and isinstance(candidate, SpeakerShadowCandidateKey)
            and candidate.scope in self._config.pending_observation_gate_scopes
        )

    def defer_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        """Predeclare one candidate as buffer-only before accepting its first PCM."""

        return self._defer_candidate(candidate, rolling_buffer=True)

    def defer_coverage_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        """Predeclare a fixed-size, buffer-only exact-coverage continuation."""

        return self._defer_candidate(candidate, rolling_buffer=False)

    def _defer_candidate(
        self,
        candidate: SpeakerShadowCandidateKey,
        *,
        rolling_buffer: bool,
    ) -> bool:

        if (
            self._resetting
            or not self.supports_deferred_candidate(candidate)
            or candidate in self._finalized
            or self._candidate_was_evicted(candidate)
        ):
            return False
        token = self._candidate_tokens.get(candidate)
        if token is not None:
            return bool(
                token.deferred_requested
                and token.terminal_reason is None
                and token.finish_state is _FinishState.OPEN
                and not token.pcm_frozen
            )
        if self._buffer_slots_in_use() >= self._config.buffered_candidate_capacity:
            return False
        token = _CandidateToken(
            candidate,
            0,
            deferred_requested=True,
            scoring_deferred=True,
            rolling_buffer_deferred=rolling_buffer,
        )
        marker = _CandidateDeferred(self._generation, candidate, token)
        if not self._admit_data_item(marker):
            return False
        self._candidate_tokens[candidate] = token
        self._candidate_tokens.move_to_end(candidate)
        return True

    def activate_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        """Order scoring activation behind all PCM already accepted for a defer."""

        if (
            self._resetting
            or not self.enabled
            or not isinstance(candidate, SpeakerShadowCandidateKey)
        ):
            return False
        token = self._candidate_tokens.get(candidate)
        if (
            token is None
            or not token.deferred_requested
            or token.terminal_reason is not None
            or token.finish_state is not _FinishState.OPEN
            or token.pcm_frozen
            or candidate in self._finalized
            or self._candidate_was_evicted(candidate, token=token)
        ):
            return False
        if not token.scoring_deferred or token.activation_queued:
            return True
        marker = _CandidateActivated(self._generation, candidate, token)
        if not self._admit_data_item(marker):
            self._drop_candidate(candidate, token=token)
            return False
        token.activation_queued = True
        return True

    def anchor_deferred_candidate(
        self,
        request: SpeakerShadowDeferredAnchorRequest,
    ) -> SpeakerShadowDeferredAnchorReceipt | None:
        """Reserve one exact deferred-buffer rebase before enabling scoring."""

        if (
            self._resetting
            or not self.enabled
            or type(request) is not SpeakerShadowDeferredAnchorRequest
        ):
            return None
        candidate = request.candidate
        token = self._candidate_tokens.get(candidate)
        if (
            token is None
            or not token.deferred_requested
            or token.terminal_reason is not None
            or token.finish_state is not _FinishState.OPEN
            or token.pcm_frozen
            or candidate in self._finalized
            or self._candidate_was_evicted(candidate, token=token)
        ):
            return None

        if token.anchor_revision:
            if (
                token.anchor_revision != request.anchor_revision
                or token.anchor_discard_prefix_sample_count
                != request.discard_prefix_sample_count
            ):
                return None
            return next(
                (
                    record.marker.receipt
                    for record in self._deferred_anchors.values()
                    if record.marker.token is token
                ),
                None,
            )
        if (
            request.expected_observed_sample_count != token.accepted_sample_count
            or request.discard_prefix_sample_count
            < token.deferred_retained_start_sample_count
        ):
            return None
        while len(self._deferred_anchors) >= self._config.buffered_candidate_capacity:
            settled = next(
                (
                    operation_id
                    for operation_id, record in self._deferred_anchors.items()
                    if record.state != "pending"
                ),
                None,
            )
            if settled is None:
                return None
            self._deferred_anchors.pop(settled, None)

        operation_id = self._next_deferred_anchor_operation_id
        retained_sample_count = (
            request.expected_observed_sample_count
            - request.discard_prefix_sample_count
        )
        receipt = SpeakerShadowDeferredAnchorReceipt(
            runtime_generation=self._generation,
            operation_id=operation_id,
            candidate=candidate,
            anchor_revision=request.anchor_revision,
            observed_sample_count=request.expected_observed_sample_count,
            discarded_sample_count=request.discard_prefix_sample_count,
            retained_sample_count=retained_sample_count,
            _owner=self._deferred_anchor_owner,
        )
        marker = _CandidateAnchored(
            generation=self._generation,
            candidate=candidate,
            token=token,
            expected_observed_sample_count=request.expected_observed_sample_count,
            discard_prefix_sample_count=request.discard_prefix_sample_count,
            receipt=receipt,
        )
        if not self._admit_data_item(marker):
            return None

        # Queue admission cannot yield. Frames counted in ``expected`` are
        # ordered before this marker; later frames are ordered after it and use
        # the rebased count without ever seeing scoring enabled early.
        self._next_deferred_anchor_operation_id += 1
        token.accepted_sample_count = retained_sample_count
        token.deferred_retained_start_sample_count = 0
        token.anchor_revision = request.anchor_revision
        token.anchor_discard_prefix_sample_count = request.discard_prefix_sample_count
        token.anchor_queued = True
        self._deferred_anchors[operation_id] = _DeferredAnchorRecord(marker)
        return receipt

    def deferred_anchor_status(
        self,
        receipt: SpeakerShadowDeferredAnchorReceipt,
    ) -> SpeakerShadowDeferredAnchorStatus:
        if (
            type(receipt) is not SpeakerShadowDeferredAnchorReceipt
            or receipt._owner is not self._deferred_anchor_owner
            or receipt.runtime_generation != self._generation
        ):
            return "stale"
        record = self._deferred_anchors.get(receipt.operation_id)
        if record is None or record.marker.receipt is not receipt:
            return "stale"
        return record.state

    async def wait_deferred_anchor_settled(
        self,
        receipt: SpeakerShadowDeferredAnchorReceipt,
        *,
        deadline: float,
    ) -> SpeakerShadowDeferredAnchorStatus:
        while True:
            status = self.deferred_anchor_status(receipt)
            if status != "pending":
                return status
            record = self._deferred_anchors.get(receipt.operation_id)
            if record is None:
                return "stale"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "pending"
            try:
                await asyncio.wait_for(record.settled.wait(), timeout=remaining)
            except TimeoutError:
                return self.deferred_anchor_status(receipt)

    def reconcile_candidate_prefix(
        self,
        *,
        source: SpeakerShadowCandidateKey,
        target: SpeakerShadowCandidateKey,
        prefix_sample_count: int,
        suffix: SpeakerShadowCandidateKey | None = None,
    ) -> bool:
        """Atomically reserve one same-queue candidate prefix reconciliation.

        A distinct source is a deferred physical tail whose covered prefix is
        appended to target.  An equal source and target seals an active head at
        the prefix.  Any remainder is reserved synchronously for a fresh
        deferred suffix so submissions arriving before marker execution cannot
        overrun either candidate's bounded sample budget.
        """

        if self._resetting or not self.enabled:
            return False
        if (
            not isinstance(source, SpeakerShadowCandidateKey)
            or not isinstance(target, SpeakerShadowCandidateKey)
            or type(prefix_sample_count) is not int
            or prefix_sample_count < 0
            or (
                suffix is not None and not isinstance(suffix, SpeakerShadowCandidateKey)
            )
            or source.scope not in self._config.pending_observation_gate_scopes
            or target.scope != source.scope
            or target.detector_epoch != source.detector_epoch
        ):
            return False

        source_token = self._candidate_tokens.get(source)
        if (
            source_token is None
            or source_token.terminal_reason is not None
            or source_token.finish_state is not _FinishState.OPEN
            or source_token.pcm_frozen
            or source in self._finalized
            or self._candidate_was_evicted(source, token=source_token)
            or source_token.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
        ):
            return False
        source_sample_count = source_token.accepted_sample_count
        if prefix_sample_count > source_sample_count:
            return False
        remainder_sample_count = source_sample_count - prefix_sample_count
        if (remainder_sample_count > 0) != (suffix is not None):
            return False

        if suffix is not None:
            if (
                suffix in (source, target)
                or suffix.scope != source.scope
                or suffix.detector_epoch != source.detector_epoch
                or suffix in self._candidate_tokens
                or suffix in self._buffers
                or suffix in self._finalized
                or self._candidate_was_evicted(suffix)
                or self._buffer_slots_in_use()
                >= self._config.buffered_candidate_capacity
            ):
                return False

        same_candidate = source == target
        target_finalized = self._finalized.get(target)
        target_token = (
            source_token if same_candidate else self._candidate_tokens.get(target)
        )
        if same_candidate:
            evaluated_through_samples = self._evaluated_through_samples(
                source,
                source_token,
            )
            if prefix_sample_count < evaluated_through_samples:
                return False
        elif target_token is None:
            if (
                target_finalized is None
                or target_finalized.terminal_reason != "scored"
                or target_finalized.token is None
            ):
                return False
            target_token = target_finalized.token
        elif (
            target_token.terminal_reason is not None
            or target_token.finish_state is not _FinishState.OPEN
            or target_token.pcm_frozen
            or target in self._finalized
            or self._candidate_was_evicted(target, token=target_token)
            or target_token.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
        ):
            return False

        target_sample_count = target_token.accepted_sample_count
        target_is_terminal = target_token.terminal_reason is not None
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        transferred_sample_count = (
            0
            if same_candidate or target_is_terminal
            else min(
                prefix_sample_count,
                max(0, maximum_samples - target_sample_count),
            )
        )
        suffix_token = (
            _CandidateToken(
                suffix,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                accepted_sample_count=remainder_sample_count,
                deferred_requested=True,
                scoring_deferred=True,
            )
            if suffix is not None
            else None
        )
        marker = _CandidatePrefixReconciliation(
            generation=self._generation,
            source=source,
            source_token=source_token,
            target=target,
            target_token=target_token,
            source_sample_count=source_sample_count,
            target_sample_count=target_sample_count,
            prefix_sample_count=prefix_sample_count,
            transferred_sample_count=transferred_sample_count,
            suffix=suffix,
            suffix_token=suffix_token,
        )
        if not self._admit_data_item(marker):
            return False

        # ``put_nowait`` cannot yield to the worker.  Commit all reservations
        # only after admission so False never exposes partial candidate state.
        source_token.pcm_frozen = True
        if same_candidate:
            source_token.accepted_sample_count = prefix_sample_count
        elif not target_is_terminal:
            target_token.accepted_sample_count = (
                target_sample_count + transferred_sample_count
            )
        if suffix is not None and suffix_token is not None:
            self._candidate_tokens[suffix] = suffix_token
            self._candidate_tokens.move_to_end(suffix)
        return True

    def reconcile_candidate_batch(
        self,
        request: SpeakerShadowBatchReconcileRequest,
    ) -> SpeakerShadowBatchReconcileReceipt | None:
        """Reserve one all-or-nothing split/merge/finish marker.

        Admission is synchronous.  A ``None`` result never freezes a candidate
        or changes sample ownership; an accepted receipt owns both one data
        queue slot and one terminal slot until its single marker is retired.
        """

        if (
            self._resetting
            or not self.enabled
            or type(request) is not SpeakerShadowBatchReconcileRequest
            or not request.finish_target
        ):
            return None
        sources = request.sources
        target = request.target
        suffix = request.suffix
        if (
            target.scope not in self._config.pending_observation_gate_scopes
            or any(
                source.candidate.scope != target.scope
                or source.candidate.detector_epoch != target.detector_epoch
                for source in sources
            )
            or (
                suffix is not None
                and (
                    suffix.scope != target.scope
                    or suffix.detector_epoch != target.detector_epoch
                )
            )
        ):
            return None
        source_candidates = tuple(source.candidate for source in sources)
        if len(set(source_candidates)) != len(source_candidates):
            return None

        kept_indexes = tuple(
            index
            for index, source in enumerate(sources)
            if source.keep_end_sample > source.keep_start_sample
        )
        if not kept_indexes or kept_indexes[-1] != len(sources) - 1:
            return None
        first_kept = kept_indexes[0]
        if kept_indexes != tuple(range(first_kept, len(sources))):
            return None
        for index, source in enumerate(sources):
            if index < first_kept:
                if source.keep_start_sample != source.keep_end_sample:
                    return None
                continue
            if index > first_kept and source.keep_start_sample != 0:
                return None
            if index < len(sources) - 1 and (
                source.keep_end_sample != source.expected_sample_count
            ):
                return None

        last_source = sources[-1]
        suffix_sample_count = (
            last_source.expected_sample_count - last_source.keep_end_sample
        )
        if (suffix_sample_count > 0) != (suffix is not None):
            return None
        if suffix is not None and suffix in {*source_candidates, target}:
            return None

        target_is_source = target in source_candidates
        if target_is_source and (
            target != sources[first_kept].candidate
            or sources[first_kept].keep_start_sample != 0
        ):
            return None
        if not target_is_source and (
            target in self._candidate_tokens
            or target in self._buffers
            or target in self._finalized
            or self._candidate_was_evicted(target)
        ):
            return None
        if suffix is not None and (
            suffix in self._candidate_tokens
            or suffix in self._buffers
            or suffix in self._finalized
            or self._candidate_was_evicted(suffix)
        ):
            return None

        reserved_sources: list[_ReservedReconcileSource] = []
        for source in sources:
            token = self._candidate_tokens.get(source.candidate)
            if (
                token is None
                or token.terminal_reason is not None
                or token.finish_state is not _FinishState.OPEN
                or token.pcm_frozen
                or token.reconciliation_batch_id is not None
                or source.candidate in self._finalized
                or self._candidate_was_evicted(source.candidate, token=token)
                or token.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
                or token.accepted_sample_count != source.expected_sample_count
            ):
                return None
            reserved_sources.append(_ReservedReconcileSource(source, token))

        target_sample_count = sum(
            source.keep_end_sample - source.keep_start_sample for source in sources
        )
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        if target_sample_count <= 0 or target_sample_count > maximum_samples:
            return None

        if target_is_source:
            target_index = source_candidates.index(target)
            target_token = reserved_sources[target_index].token
            if self._evaluated_through_samples(target, target_token) > (
                sources[target_index].keep_end_sample
            ):
                return None
        else:
            target_token = _CandidateToken(
                target,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                accepted_sample_count=target_sample_count,
            )
        suffix_token = (
            _CandidateToken(
                suffix,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                accepted_sample_count=suffix_sample_count,
                deferred_requested=True,
                scoring_deferred=True,
            )
            if suffix is not None
            else None
        )

        new_candidate_count = int(not target_is_source) + int(suffix is not None)
        if (
            self._buffer_slots_in_use() + new_candidate_count
            > self._config.buffered_candidate_capacity
        ):
            return None
        while len(self._reconciliations) >= self._config.buffered_candidate_capacity:
            oldest_id, oldest = next(iter(self._reconciliations.items()))
            if oldest.state == "pending":
                return None
            self._reconciliations.pop(oldest_id, None)

        batch_id = self._next_reconciliation_batch_id
        receipt = SpeakerShadowBatchReconcileReceipt(
            runtime_generation=self._generation,
            batch_id=batch_id,
            target=target,
            suffix=suffix,
            target_sample_count=target_sample_count,
            suffix_sample_count=suffix_sample_count,
            _owner=self._reconciliation_owner,
        )
        marker = _CandidateBatchReconciliation(
            generation=self._generation,
            batch_id=batch_id,
            sources=tuple(reserved_sources),
            target=target,
            target_token=target_token,
            target_was_existing=target_is_source,
            target_sample_count=target_sample_count,
            suffix=suffix,
            suffix_token=suffix_token,
            suffix_sample_count=suffix_sample_count,
            finish_target=True,
            receipt=receipt,
        )
        if not self._admit_batch_item(marker):
            return None

        # ``put_nowait`` cannot yield.  Publish reservations only after the
        # single marker owns both queue budgets, so a failed admission is pure.
        self._next_reconciliation_batch_id += 1
        for reserved in reserved_sources:
            reserved.token.pcm_frozen = True
            reserved.token.reconciliation_batch_id = batch_id
        target_token.accepted_sample_count = target_sample_count
        target_token.pcm_frozen = True
        target_token.reconciliation_batch_id = batch_id
        target_token.finish_state = _FinishState.QUEUED
        self._candidate_tokens[target] = target_token
        self._candidate_tokens.move_to_end(target)
        if suffix is not None and suffix_token is not None:
            suffix_token.reconciliation_batch_id = batch_id
            self._candidate_tokens[suffix] = suffix_token
            self._candidate_tokens.move_to_end(suffix)
        self._reconciliations[batch_id] = _ReconciliationRecord(marker)
        self._metrics.terminal_queued_count += 1
        self._metrics.reconciliation_batch_admitted_count += 1
        return receipt

    def reconciliation_status(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> SpeakerShadowReconciliationStatus:
        if (
            type(receipt) is not SpeakerShadowBatchReconcileReceipt
            or receipt._owner is not self._reconciliation_owner
            or receipt.runtime_generation != self._generation
        ):
            return "stale"
        record = self._reconciliations.get(receipt.batch_id)
        if record is None or record.marker.receipt is not receipt:
            return "stale"
        return record.state

    def _retains_terminal_boundary_pcm(self, candidate: SpeakerShadowCandidateKey) -> bool:
        return bool(
            candidate.scope == "provider_candidate"
            and candidate.scope in self._config.pending_observation_gate_scopes
            and self._config.exact_boundary_pcm_retention_seconds > 0
        )

    def _buffer_slots_in_use(self) -> int:
        return len(set(self._candidate_tokens) | set(self._buffers))

    def _candidate_pcm_is_reserved(self, candidate: SpeakerShadowCandidateKey) -> bool:
        buffer = self._buffers.get(candidate)
        if buffer is None:
            return False
        token = buffer.token
        batch_id = token.reconciliation_batch_id
        exact = self._prepared_exact_intervals.get(batch_id)
        if exact is not None and any(item.token is token for item in exact.marker.sources):
            return True
        applied = self._reconciliations.get(batch_id)
        if applied is not None and applied.state == "pending" and any(item.token is token for item in applied.marker.sources):
            return True
        markers = [item.marker for item in self._prepared_terminal_coverages.values()]
        markers.extend(item.marker for item in self._terminal_coverages.values() if item.state == "pending")
        return any(marker.target_token is token or any(
            item.token is token for item in marker.reserved_sources
        ) for marker in markers)

    def _retained_terminal_source_is_current(
        self, candidate: SpeakerShadowCandidateKey, token: _CandidateToken,
    ) -> bool:
        finalized = self._finalized.get(candidate)
        buffer = self._buffers.get(candidate)
        return bool(
            not self._closed and not self._resetting
            and finalized is not None and finalized.token is token
            and finalized.terminal_reason == token.terminal_reason == "scored"
            and buffer is not None and buffer.token is token
            and buffer.exact_boundary_deadline is not None
            and time.monotonic() < buffer.exact_boundary_deadline
            and not self._candidate_was_evicted(candidate, token=token)
        )

    def _exact_source_token(self, candidate: SpeakerShadowCandidateKey) -> _CandidateToken | None:
        token = self._candidate_tokens.get(candidate)
        if token is not None and token.terminal_reason is None:
            return token
        finalized = self._finalized.get(candidate)
        token = finalized.token if finalized is not None else None
        return token if token is not None and self._retained_terminal_source_is_current(candidate, token) else None

    def _schedule_terminal_pcm_expiry(self) -> None:
        handle = self._terminal_pcm_expiry_handle
        if handle is not None:
            handle.cancel()
        self._terminal_pcm_expiry_handle = None
        deadlines = [buffer.exact_boundary_deadline for buffer in self._buffers.values()
                     if buffer.exact_boundary_deadline is not None]
        if deadlines and not self._closed and not self._resetting:
            self._terminal_pcm_expiry_handle = asyncio.get_running_loop().call_later(
                max(.001, min(deadlines) - time.monotonic()), self._expire_terminal_pcm,
            )

    def _expire_terminal_pcm(self) -> None:
        self._terminal_pcm_expiry_handle = None
        now = time.monotonic()
        for candidate, buffer in tuple(self._buffers.items()):
            if buffer.exact_boundary_deadline is None or now < buffer.exact_boundary_deadline:
                continue
            batch_id = buffer.token.reconciliation_batch_id
            prepared = self._prepared_exact_intervals.get(batch_id)
            if prepared is not None and any(item.token is buffer.token for item in prepared.marker.sources):
                self._abort_prepared_exact_interval_record(prepared)
            committed = self._reconciliations.get(batch_id)
            if (committed is not None and committed.state == "pending"
                    and any(item.token is buffer.token for item in committed.marker.sources)):
                self._fail_candidate_batch_reconciliation(committed.marker)
            if self._buffers.get(candidate) is buffer:
                self._buffers.pop(candidate, None)
                self._wipe_bytearray(buffer.pcm16)
        self._schedule_terminal_pcm_expiry()

    def exact_interval_requires_fresh_target(
        self,
        source: SpeakerShadowReconcileSource,
    ) -> bool:
        """Prove that owned PCM can be scored under a fresh exact identity.

        This grants no score authority or reservation. The ordinary prepare
        must still validate and freeze the actual source and queue budgets.
        A scored source must retain its original buffer, identity and unexpired
        boundary deadline. This neither revives its token nor reconstructs PCM.
        """

        if self._resetting or not self.enabled or type(source) is not SpeakerShadowReconcileSource:
            return False
        token = self._exact_source_token(source.candidate)
        buffer = self._buffers.get(source.candidate)
        return bool(
            token is not None
            and token.finish_state is _FinishState.OPEN
            and not token.pcm_frozen
            and token.reconciliation_batch_id is None
            and not self._candidate_was_evicted(source.candidate, token=token)
            and token.sample_rate_hz == SPEAKER_SHADOW_SAMPLE_RATE_HZ
            and token.accepted_sample_count == source.expected_sample_count
            and source.keep_start_sample == 0
            and buffer is not None
            and buffer.token is token
            and buffer.sample_rate_hz == SPEAKER_SHADOW_SAMPLE_RATE_HZ
            and source.keep_end_sample <= buffer.sample_count <= source.expected_sample_count
            and len(buffer.pcm16) == buffer.sample_count * 2
            and (
                self._evaluated_through_samples(source.candidate, token) > source.keep_end_sample
                or (
                    self._active_evaluation == (self._generation, source.candidate)
                    and self._active_evaluation_terminal
                    and token.anchor_applied
                    and self._retains_terminal_boundary_pcm(source.candidate)
                )
            )
        )

    def prepare_exact_interval(
        self,
        request: SpeakerShadowBatchReconcileRequest,
    ) -> SpeakerShadowBatchReconcileReceipt | None:
        """Reserve an ordered prefix/suffix split of bounded, owned buffers."""

        if (
            self._resetting
            or not self.enabled
            or type(request) is not SpeakerShadowBatchReconcileRequest
            or not request.finish_target
            or not 0 < len(request.sources) <= self._config.buffered_candidate_capacity
        ):
            return None
        source = request.sources[0]
        source_candidates = {item.candidate for item in request.sources}
        target = request.target
        suffix = request.suffix
        if (
            source.candidate.scope
            not in self._config.pending_observation_gate_scopes
            or target.scope != source.candidate.scope
            or target.detector_epoch != source.candidate.detector_epoch
            or (
                suffix is not None
                and (
                    suffix.scope != target.scope
                    or suffix.detector_epoch != target.detector_epoch
                    or suffix in {source.candidate, target}
                )
            )
            or len(source_candidates) != len(request.sources)
            or (len(request.sources) > 1 and any(item.keep_start_sample != 0 for item in request.sources))
            or any(item.candidate.scope != source.candidate.scope
                   or item.candidate.detector_epoch != source.candidate.detector_epoch
                   for item in request.sources)
            or target in source_candidates - {source.candidate}
            or suffix in source_candidates
        ):
            return None
        sources: list[_ReservedReconcileSource] = []
        suffix_started = False
        for item in request.sources:
            token = self._exact_source_token(item.candidate)
            if (
                token is None or token.finish_state is not _FinishState.OPEN
                or token.pcm_frozen or token.reconciliation_batch_id is not None
                or self._candidate_was_evicted(item.candidate, token=token)
                or token.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
                or token.accepted_sample_count != item.expected_sample_count
                or (suffix_started and item.keep_end_sample != 0)
                or (token.terminal_reason is not None and target == item.candidate)
            ):
                return None
            suffix_started |= item.keep_end_sample < item.expected_sample_count
            sources.append(_ReservedReconcileSource(item, token))
        source_token = sources[0].token
        protected_terminals = {
            candidate for candidate in self._finalized
            if candidate in source_candidates or self._candidate_pcm_is_reserved(candidate)
        }
        if self._active_evaluation_terminal and self._active_evaluation is not None:
            active_generation, active_candidate = self._active_evaluation
            if active_generation == self._generation and (
                active_candidate in source_candidates
                or self._candidate_pcm_is_reserved(active_candidate)
            ):
                protected_terminals.add(active_candidate)
        if len(protected_terminals) > self._config.finalized_candidate_capacity:
            return None

        target_is_source = target == source.candidate
        if target_is_source:
            if source.keep_start_sample != 0:
                return None
            if self._evaluated_through_samples(target, source_token) > (
                source.keep_end_sample
            ):
                return None
            target_token = source_token
        else:
            if (
                target in self._candidate_tokens
                or target in self._buffers
                or target in self._finalized
                or self._candidate_was_evicted(target)
            ):
                return None
            target_token = _CandidateToken(target, SPEAKER_SHADOW_SAMPLE_RATE_HZ)

        suffix_sample_count = sum(item.expected_sample_count - item.keep_end_sample for item in request.sources)
        if suffix_sample_count > 0 and suffix is None:
            return None
        if suffix is not None and (
            suffix in self._candidate_tokens
            or suffix in self._buffers
            or suffix in self._finalized
            or self._candidate_was_evicted(suffix)
        ):
            return None

        target_sample_count = sum(item.keep_end_sample - item.keep_start_sample for item in request.sources)
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        if target_sample_count <= 0 or target_sample_count > maximum_samples or suffix_sample_count > maximum_samples:
            return None
        new_candidate_count = int(not target_is_source) + int(suffix is not None)
        reserved_data_slots = 1 + int(suffix is not None)
        if (
            self._buffer_slots_in_use() + new_candidate_count
            > self._config.buffered_candidate_capacity
            or self._queued_data_item_count + reserved_data_slots
            > self._config.queue_capacity
            or self._queued_terminal_count >= self._config.terminal_queue_capacity
        ):
            return None
        while (
            len(self._reconciliations) + len(self._prepared_exact_intervals)
            >= self._config.buffered_candidate_capacity
        ):
            settled = next(
                (
                    batch_id
                    for batch_id, record in self._reconciliations.items()
                    if record.state != "pending"
                ),
                None,
            )
            if settled is None:
                return None
            self._reconciliations.pop(settled, None)
        if not self._ensure_worker():
            self._metrics.worker_start_failure_count += 1
            self._set_degraded_cause("worker_start_failure")
            return None

        batch_id = self._next_reconciliation_batch_id
        suffix_token = (
            _CandidateToken(
                suffix,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                accepted_sample_count=suffix_sample_count,
                pcm_frozen=True,
                deferred_requested=True,
                scoring_deferred=True,
            )
            if suffix is not None
            else None
        )
        receipt = SpeakerShadowBatchReconcileReceipt(
            runtime_generation=self._generation,
            batch_id=batch_id,
            target=target,
            suffix=suffix,
            target_sample_count=target_sample_count,
            suffix_sample_count=suffix_sample_count,
            _owner=self._reconciliation_owner,
        )
        marker = _CandidateBatchReconciliation(
            generation=self._generation,
            batch_id=batch_id,
            sources=tuple(sources),
            target=target,
            target_token=target_token,
            target_was_existing=target_is_source,
            target_sample_count=target_sample_count,
            suffix=suffix,
            suffix_token=suffix_token,
            suffix_sample_count=suffix_sample_count,
            finish_target=True,
            receipt=receipt,
        )
        record = _PreparedExactIntervalRecord(
            marker=marker,
            source_accepted_sample_count=sum(item.expected_sample_count for item in request.sources),
            source_finish_state=source_token.finish_state,
            source_pcm_frozen=source_token.pcm_frozen,
            source_reconciliation_batch_id=source_token.reconciliation_batch_id,
            reserved_data_slots=reserved_data_slots,
            source_states=tuple((item.token.finish_state, item.token.pcm_frozen,
                                 item.token.reconciliation_batch_id) for item in sources),
        )

        # No await occurs after capacity validation.  Reserve both logical
        # budgets without queue publication, then freeze every touched token.
        self._next_reconciliation_batch_id += 1
        self._queued_data_item_count += reserved_data_slots
        self._queued_terminal_count += 1
        for item in sources:
            item.token.pcm_frozen = True
            item.token.reconciliation_batch_id = batch_id
        target_token.accepted_sample_count = target_sample_count
        target_token.pcm_frozen = True
        target_token.reconciliation_batch_id = batch_id
        target_token.finish_state = _FinishState.QUEUED
        self._candidate_tokens[target] = target_token
        self._candidate_tokens.move_to_end(target)
        if suffix is not None and suffix_token is not None:
            suffix_token.reconciliation_batch_id = batch_id
            self._candidate_tokens[suffix] = suffix_token
            self._candidate_tokens.move_to_end(suffix)
        self._prepared_exact_intervals[batch_id] = record
        return receipt

    def commit_exact_interval(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> bool:
        """Publish one prepared marker at a synchronous linearization point."""

        if (
            type(receipt) is not SpeakerShadowBatchReconcileReceipt
            or receipt._owner is not self._reconciliation_owner
            or receipt.runtime_generation != self._generation
            or self._resetting
            or self._closed
        ):
            return False
        record = self._prepared_exact_intervals.get(receipt.batch_id)
        if record is None or record.marker.receipt is not receipt:
            return False
        marker = record.marker
        source = marker.sources[0]
        source_token = source.token
        suffix_token = marker.suffix_token
        if (
            any(self._exact_source_token(item.source.candidate) is not item.token
                or not item.token.pcm_frozen
                or item.token.reconciliation_batch_id != marker.batch_id
                for item in marker.sources)
            or self._candidate_tokens.get(marker.target) is not marker.target_token
            or marker.target_token.finish_state is not _FinishState.QUEUED
            or not marker.target_token.pcm_frozen
            or marker.target_token.reconciliation_batch_id != marker.batch_id
            or (
                marker.suffix is not None
                and (
                    suffix_token is None
                    or self._candidate_tokens.get(marker.suffix) is not suffix_token
                    or not suffix_token.pcm_frozen
                    or suffix_token.reconciliation_batch_id != marker.batch_id
                )
            )
            or not self._ensure_worker()
        ):
            return False
        if suffix_token is not None:
            suffix_token.pcm_frozen = False
        staged_frame = None
        if marker.suffix is not None and suffix_token is not None and (
            record.suffix_scratch_pcm16
        ):
            staged_frame = _AudioFrame(
                generation=self._generation,
                candidate=marker.suffix,
                token=suffix_token,
                pcm16=record.suffix_scratch_pcm16,
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                sample_count=len(record.suffix_scratch_pcm16) // 2,
            )
        physical_items = 1 + int(staged_frame is not None)
        if self._queue.qsize() + physical_items > self._queue.maxsize:
            if suffix_token is not None:
                suffix_token.pcm_frozen = True
            return False
        try:
            self._queue.put_nowait(marker)
            if staged_frame is not None:
                self._queue.put_nowait(staged_frame)
        except asyncio.QueueFull:
            if suffix_token is not None:
                suffix_token.pcm_frozen = True
            self._metrics.terminal_overflow_count += 1
            self._set_degraded_cause("terminal_overflow")
            return False
        unused_data_slots = record.reserved_data_slots - physical_items
        if unused_data_slots > 0:
            self._queued_data_item_count = max(
                0,
                self._queued_data_item_count - unused_data_slots,
            )
        if staged_frame is not None:
            self._queued_pcm_bytes += len(staged_frame.pcm16)
            record.suffix_scratch_pcm16 = bytearray()
        self._prepared_exact_intervals.pop(receipt.batch_id, None)
        self._reconciliations[receipt.batch_id] = _ReconciliationRecord(marker)
        self._metrics.terminal_queued_count += 1
        self._metrics.reconciliation_batch_admitted_count += 1
        return True

    def abort_exact_interval(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> bool:
        """Roll back an unpublished reservation without retiring its source."""

        if (
            type(receipt) is not SpeakerShadowBatchReconcileReceipt
            or receipt._owner is not self._reconciliation_owner
        ):
            return False
        record = self._prepared_exact_intervals.get(receipt.batch_id)
        if record is None or record.marker.receipt is not receipt:
            return False
        return self._abort_prepared_exact_interval_record(record)

    def _abort_prepared_exact_interval_record(
        self,
        record: _PreparedExactIntervalRecord,
        *,
        restore_staged_audio: bool = True,
    ) -> bool:
        marker = record.marker
        current = self._prepared_exact_intervals.get(marker.batch_id)
        if current is not record:
            return False
        self._prepared_exact_intervals.pop(marker.batch_id, None)
        staged_frame = None
        source = marker.sources[-1]
        source_token = source.token
        staged_audio_restored = not bool(
            restore_staged_audio and record.suffix_scratch_pcm16
        )
        if (
            restore_staged_audio
            and not self._resetting
            and not self._closed
            and record.suffix_scratch_pcm16
            and self._exact_source_token(source.source.candidate) is source_token
            and self._ensure_worker()
        ):
            if source_token.terminal_reason == "scored":
                buffer = self._buffers.get(source.source.candidate)
                extra_samples = len(record.suffix_scratch_pcm16) // 2
                if (
                    buffer is not None and buffer.token is source_token
                    and buffer.sample_count + extra_samples
                    <= SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1000
                    and self._retained_pcm_bytes() + len(record.suffix_scratch_pcm16)
                    <= MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES
                ):
                    buffer.pcm16.extend(record.suffix_scratch_pcm16)
                    buffer.sample_count += extra_samples
                    buffer.observed_sample_count += extra_samples
                    source_token.accepted_sample_count = buffer.sample_count
                    self._wipe_bytearray(record.suffix_scratch_pcm16)
                    record.suffix_scratch_pcm16 = bytearray()
                    staged_audio_restored = True
            else:
                staged_frame = _AudioFrame(
                    generation=self._generation, candidate=source.source.candidate,
                    token=source_token, pcm16=record.suffix_scratch_pcm16,
                    sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                    sample_count=len(record.suffix_scratch_pcm16) // 2,
                )
                try:
                    self._queue.put_nowait(staged_frame)
                    staged_audio_restored = True
                except asyncio.QueueFull:
                    staged_frame = None
        retired_data_slots = record.reserved_data_slots - int(staged_frame is not None)
        self._queued_data_item_count = max(
            0,
            self._queued_data_item_count - retired_data_slots,
        )
        self._queued_terminal_count = max(0, self._queued_terminal_count - 1)
        if self._queued_terminal_count == 0:
            self._clear_degraded_cause("terminal_overflow")

        for index, item in enumerate(marker.sources):
            token = item.token
            finalized = self._finalized.get(item.source.candidate)
            if (
                self._candidate_tokens.get(item.source.candidate) is not token
                and (finalized is None or finalized.token is not token)
            ):
                continue
            if token.reconciliation_batch_id != marker.batch_id:
                continue
            if token.terminal_reason is None:
                token.accepted_sample_count = item.source.expected_sample_count + (
                    staged_frame.sample_count if item is source and staged_frame is not None else 0
                )
            finish_state, frozen, batch_id = record.source_states[index] if record.source_states else (
                record.source_finish_state, record.source_pcm_frozen, record.source_reconciliation_batch_id,
            )
            token.finish_state = finish_state
            token.pcm_frozen = frozen
            token.reconciliation_batch_id = batch_id
        for candidate, token in (
            (marker.target, marker.target_token),
            (marker.suffix, marker.suffix_token),
        ):
            if candidate is None or token is None or any(token is item.token for item in marker.sources):
                continue
            if self._candidate_tokens.get(candidate) is token:
                self._candidate_tokens.pop(candidate, None)
            buffer = self._buffers.get(candidate)
            if buffer is not None and buffer.token is token:
                self._buffers.pop(candidate, None)
                self._wipe_bytearray(buffer.pcm16)
        if staged_frame is not None:
            self._queued_pcm_bytes += len(staged_frame.pcm16)
            record.suffix_scratch_pcm16 = bytearray()
        else:
            self._wipe_bytearray(record.suffix_scratch_pcm16)
        return staged_audio_restored

    def _abort_all_prepared_exact_intervals(self) -> None:
        for record in tuple(self._prepared_exact_intervals.values()):
            self._abort_prepared_exact_interval_record(
                record,
                restore_staged_audio=False,
            )

    def prepare_finalized_candidate_coverage(
        self,
        request: SpeakerShadowTerminalCoverageRequest,
    ) -> SpeakerShadowTerminalCoverageReceipt | None:
        """Freeze finalized coverage without publishing destructive work."""

        if (
            self._resetting
            or not self.enabled
            or type(request) is not SpeakerShadowTerminalCoverageRequest
        ):
            return None
        sources = request.sources
        target = request.target
        suffix = request.suffix
        if (
            target.scope not in self._config.pending_observation_gate_scopes
            or sources[0].candidate != target
            or request.provider_exact_start_sample != 0
            or request.scored_window_start_sample != 0
            or request.provider_exact_end_sample <= request.provider_exact_start_sample
            or request.scored_window_end_sample <= request.scored_window_start_sample
            or request.provider_exact_end_sample < request.scored_window_end_sample
            or any(
                source.candidate.scope != target.scope
                or source.candidate.detector_epoch != target.detector_epoch
                for source in sources
            )
            or (
                suffix is not None
                and (
                    suffix.scope != target.scope
                    or suffix.detector_epoch != target.detector_epoch
                )
            )
        ):
            return None
        source_candidates = tuple(source.candidate for source in sources)
        if len(set(source_candidates)) != len(source_candidates):
            return None

        # The exact Provider interval is expressed from the finalized target's
        # evidence origin. Every later source is contiguous and fully covered,
        # except that the last source may leave a suffix after the boundary.
        if sources[0].keep_start_sample != 0:
            return None
        for index, source in enumerate(sources):
            if index > 0 and source.keep_start_sample != 0:
                return None
            if index < len(sources) - 1 and (
                source.keep_end_sample != source.expected_sample_count
            ):
                return None
        covered_sample_count = sum(
            source.keep_end_sample - source.keep_start_sample for source in sources
        )
        if (
            request.provider_exact_end_sample - request.provider_exact_start_sample
            != covered_sample_count
        ):
            return None

        finalized = self._finalized.get(target)
        target_token = finalized.token if finalized is not None else None
        if (
            finalized is None
            or finalized.terminal_reason != "scored"
            or target_token is None
            or target_token.terminal_reason != "scored"
            or target_token.scored_sample_count <= 0
            or request.scored_window_end_sample != target_token.scored_sample_count
            or sources[0].keep_end_sample < target_token.scored_sample_count
            or self._candidate_was_evicted(target, token=target_token)
        ):
            return None

        last_source = sources[-1]
        suffix_sample_count = (
            last_source.expected_sample_count - last_source.keep_end_sample
        )
        # A zero-length suffix may still be reserved so PCM arriving between
        # prepare and commit has an explicit successor owner.
        if suffix_sample_count > 0 and suffix is None:
            return None
        if suffix is not None and suffix in {*source_candidates, target}:
            return None
        if suffix is not None and (
            suffix in self._candidate_tokens
            or suffix in self._buffers
            or suffix in self._finalized
            or self._candidate_was_evicted(suffix)
        ):
            return None

        reserved_sources: list[_ReservedReconcileSource] = []
        for source in sources[1:]:
            token = self._candidate_tokens.get(source.candidate)
            if (
                token is None
                or token.terminal_reason is not None
                or token.finish_state is not _FinishState.OPEN
                or token.pcm_frozen
                or token.reconciliation_batch_id is not None
                or source.candidate in self._finalized
                or self._candidate_was_evicted(source.candidate, token=token)
                or token.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
                or token.accepted_sample_count != source.expected_sample_count
            ):
                return None
            reserved_sources.append(_ReservedReconcileSource(source, token))

        suffix_from_live_source = bool(
            suffix is not None and last_source.candidate != target
        )
        suffix_token = (
            _CandidateToken(
                suffix,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                accepted_sample_count=(
                    suffix_sample_count if suffix_from_live_source else 0
                ),
                deferred_requested=True,
                scoring_deferred=True,
            )
            if suffix is not None
            else None
        )
        new_candidate_count = int(suffix is not None)
        if (
            self._buffer_slots_in_use() + new_candidate_count
            > self._config.buffered_candidate_capacity
        ):
            return None
        while (
            len(self._terminal_coverages) + len(self._prepared_terminal_coverages)
            >= self._config.buffered_candidate_capacity
        ):
            if not self._terminal_coverages:
                return None
            oldest_id, oldest = next(iter(self._terminal_coverages.items()))
            if oldest.state == "pending":
                return None
            self._terminal_coverages.pop(oldest_id, None)

        reserved_data_slots = 1 + int(suffix is not None)
        if (
            self._queued_data_item_count + reserved_data_slots
            > self._config.queue_capacity
            or self._queued_terminal_count >= self._config.terminal_queue_capacity
            or not self._ensure_worker()
        ):
            return None

        batch_id = self._next_reconciliation_batch_id
        receipt = SpeakerShadowTerminalCoverageReceipt(
            runtime_generation=self._generation,
            batch_id=batch_id,
            target=target,
            suffix=suffix,
            retained_sample_count=target_token.scored_sample_count,
            covered_sample_count=covered_sample_count,
            terminal_preserved=True,
            _owner=self._terminal_coverage_owner,
        )
        marker = _CandidateTerminalCoverage(
            generation=self._generation,
            batch_id=batch_id,
            sources=sources,
            reserved_sources=tuple(reserved_sources),
            target=target,
            target_token=target_token,
            suffix=suffix,
            suffix_token=suffix_token,
            suffix_sample_count=suffix_sample_count,
            receipt=receipt,
        )
        self._next_reconciliation_batch_id += 1
        self._queued_data_item_count += reserved_data_slots
        self._queued_terminal_count += 1
        for reserved in reserved_sources:
            reserved.token.pcm_frozen = True
            reserved.token.reconciliation_batch_id = batch_id
        if suffix is not None and suffix_token is not None:
            suffix_token.pcm_frozen = True
            suffix_token.reconciliation_batch_id = batch_id
            self._candidate_tokens[suffix] = suffix_token
            self._candidate_tokens.move_to_end(suffix)
        self._prepared_terminal_coverages[batch_id] = (
            _PreparedTerminalCoverageRecord(
                marker=marker,
                reserved_data_slots=reserved_data_slots,
            )
        )
        return receipt

    def commit_finalized_candidate_coverage(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> bool:
        """Publish prepared terminal coverage at an await-free linearization."""

        if (
            type(receipt) is not SpeakerShadowTerminalCoverageReceipt
            or receipt._owner is not self._terminal_coverage_owner
            or receipt.runtime_generation != self._generation
            or self._resetting
            or self._closed
        ):
            return False
        prepared = self._prepared_terminal_coverages.get(receipt.batch_id)
        if prepared is None or prepared.marker.receipt is not receipt:
            return False
        marker = prepared.marker
        suffix_token = marker.suffix_token
        for reserved in marker.reserved_sources:
            if (
                self._candidate_tokens.get(reserved.source.candidate)
                is not reserved.token
                or not reserved.token.pcm_frozen
                or reserved.token.reconciliation_batch_id != marker.batch_id
            ):
                return False
        if marker.suffix is not None and (
            suffix_token is None
            or self._candidate_tokens.get(marker.suffix) is not suffix_token
            or not suffix_token.pcm_frozen
            or suffix_token.reconciliation_batch_id != marker.batch_id
        ):
            return False

        staged_frame = None
        if (
            marker.suffix is not None
            and suffix_token is not None
            and prepared.suffix_scratch_pcm16
        ):
            staged_frame = _AudioFrame(
                generation=self._generation,
                candidate=marker.suffix,
                token=suffix_token,
                pcm16=prepared.suffix_scratch_pcm16,
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                sample_count=len(prepared.suffix_scratch_pcm16) // 2,
            )
        physical_items = 1 + int(staged_frame is not None)
        if self._queue.qsize() + physical_items > self._queue.maxsize:
            return False
        if suffix_token is not None:
            suffix_token.pcm_frozen = False
        try:
            self._queue.put_nowait(marker)
            if staged_frame is not None:
                self._queue.put_nowait(staged_frame)
        except asyncio.QueueFull:
            if suffix_token is not None:
                suffix_token.pcm_frozen = True
            return False

        unused_slots = prepared.reserved_data_slots - physical_items
        if unused_slots:
            self._queued_data_item_count = max(
                0,
                self._queued_data_item_count - unused_slots,
            )
        if staged_frame is not None:
            self._queued_pcm_bytes += len(staged_frame.pcm16)
            prepared.suffix_scratch_pcm16 = bytearray()
        self._prepared_terminal_coverages.pop(receipt.batch_id, None)
        self._terminal_coverages[receipt.batch_id] = _TerminalCoverageRecord(marker)
        self._metrics.terminal_queued_count += 1
        self._metrics.reconciliation_batch_admitted_count += 1
        return True

    def abort_finalized_candidate_coverage(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> bool:
        """Abort an unpublished terminal reservation and restore staged PCM."""

        if (
            type(receipt) is not SpeakerShadowTerminalCoverageReceipt
            or receipt._owner is not self._terminal_coverage_owner
        ):
            return False
        prepared = self._prepared_terminal_coverages.get(receipt.batch_id)
        if prepared is None or prepared.marker.receipt is not receipt:
            return False
        return self._abort_prepared_terminal_coverage(prepared)

    def _abort_prepared_terminal_coverage(
        self,
        prepared: _PreparedTerminalCoverageRecord,
        *,
        restore_staged_audio: bool = True,
    ) -> bool:
        marker = prepared.marker
        if self._prepared_terminal_coverages.get(marker.batch_id) is not prepared:
            return False
        self._prepared_terminal_coverages.pop(marker.batch_id, None)
        staged_frame = None
        last_reserved = marker.reserved_sources[-1] if marker.reserved_sources else None
        staged_audio_restored = not bool(
            restore_staged_audio and prepared.suffix_scratch_pcm16
        )
        if (
            restore_staged_audio
            and last_reserved is not None
            and prepared.suffix_scratch_pcm16
            and self._candidate_tokens.get(last_reserved.source.candidate)
            is last_reserved.token
            and last_reserved.token.accepted_sample_count
            + len(prepared.suffix_scratch_pcm16) // 2
            <= SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
            and self._ensure_worker()
        ):
            staged_frame = _AudioFrame(
                generation=self._generation,
                candidate=last_reserved.source.candidate,
                token=last_reserved.token,
                pcm16=prepared.suffix_scratch_pcm16,
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                sample_count=len(prepared.suffix_scratch_pcm16) // 2,
            )
            try:
                self._queue.put_nowait(staged_frame)
                last_reserved.token.accepted_sample_count += staged_frame.sample_count
                staged_audio_restored = True
            except asyncio.QueueFull:
                staged_frame = None

        retired_slots = prepared.reserved_data_slots - int(staged_frame is not None)
        self._queued_data_item_count = max(
            0,
            self._queued_data_item_count - retired_slots,
        )
        self._queued_terminal_count = max(0, self._queued_terminal_count - 1)
        for reserved in marker.reserved_sources:
            if self._candidate_tokens.get(reserved.source.candidate) is reserved.token:
                reserved.token.pcm_frozen = False
                reserved.token.reconciliation_batch_id = None
        if marker.suffix is not None and marker.suffix_token is not None:
            if self._candidate_tokens.get(marker.suffix) is marker.suffix_token:
                self._candidate_tokens.pop(marker.suffix, None)
        if staged_frame is not None:
            self._queued_pcm_bytes += len(staged_frame.pcm16)
            prepared.suffix_scratch_pcm16 = bytearray()
        else:
            if restore_staged_audio and prepared.suffix_scratch_pcm16:
                self._metrics.dropped_frame_count += 1
                self._metrics.dropped_audio_ms += self._audio_ms(
                    len(prepared.suffix_scratch_pcm16) // 2,
                    SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                )
            self._wipe_bytearray(prepared.suffix_scratch_pcm16)
        return staged_audio_restored

    def _abort_all_prepared_terminal_coverages(self) -> None:
        for prepared in tuple(self._prepared_terminal_coverages.values()):
            self._abort_prepared_terminal_coverage(
                prepared,
                restore_staged_audio=False,
            )

    def reconcile_finalized_candidate_coverage(
        self,
        request: SpeakerShadowTerminalCoverageRequest,
    ) -> SpeakerShadowTerminalCoverageReceipt | None:
        """Compatibility wrapper that prepares then immediately commits."""

        receipt = self.prepare_finalized_candidate_coverage(request)
        if receipt is None:
            return None
        if self.commit_finalized_candidate_coverage(receipt):
            return receipt
        self.abort_finalized_candidate_coverage(receipt)
        return None

    def terminal_coverage_status(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> SpeakerShadowReconciliationStatus:
        if (
            type(receipt) is not SpeakerShadowTerminalCoverageReceipt
            or receipt._owner is not self._terminal_coverage_owner
            or receipt.runtime_generation != self._generation
        ):
            return "stale"
        record = self._terminal_coverages.get(receipt.batch_id)
        if record is None:
            prepared = self._prepared_terminal_coverages.get(receipt.batch_id)
            if prepared is not None and prepared.marker.receipt is receipt:
                return "pending"
        if record is None or record.marker.receipt is not receipt:
            return "stale"
        return record.state

    async def wait_reconciliation_settled(
        self,
        receipt: SpeakerShadowReconciliationReceipt,
        *,
        deadline: float,
    ) -> SpeakerShadowReconciliationStatus:
        """Wait on one owned receipt event until its monotonic deadline."""

        if type(deadline) not in {int, float} or not math.isfinite(deadline):
            return "stale"
        record: _ReconciliationRecord | _TerminalCoverageRecord | None
        if type(receipt) is SpeakerShadowBatchReconcileReceipt:
            if (
                receipt._owner is not self._reconciliation_owner
                or receipt.runtime_generation != self._generation
            ):
                return "stale"
            record = self._reconciliations.get(receipt.batch_id)
        elif type(receipt) is SpeakerShadowTerminalCoverageReceipt:
            if (
                receipt._owner is not self._terminal_coverage_owner
                or receipt.runtime_generation != self._generation
            ):
                return "stale"
            record = self._terminal_coverages.get(receipt.batch_id)
        else:
            return "stale"
        if record is None or record.marker.receipt is not receipt:
            return "stale"
        if record.state == "pending":
            remaining = float(deadline) - time.monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(record.settled.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
        if receipt.runtime_generation != self._generation:
            return "stale"
        if type(receipt) is SpeakerShadowBatchReconcileReceipt:
            current = self._reconciliations.get(receipt.batch_id)
        else:
            current = self._terminal_coverages.get(receipt.batch_id)
        if current is not record or current.marker.receipt is not receipt:
            return "stale"
        return record.state

    def revoke_terminal_coverage(
        self,
        receipt: SpeakerShadowTerminalCoverageReceipt,
    ) -> None:
        if self.terminal_coverage_status(receipt) == "stale":
            return
        record = self._terminal_coverages.get(receipt.batch_id)
        if record is not None:
            self._revoke_terminal_coverage_record(record.marker)

    def revoke_reconciliation(
        self,
        receipt: SpeakerShadowBatchReconcileReceipt,
    ) -> None:
        if self.reconciliation_status(receipt) == "stale":
            return
        record = self._reconciliations.get(receipt.batch_id)
        if record is None:
            return
        self._revoke_candidate_batch_reconciliation(record.marker)

    def complete_reconciliation(
        self,
        receipt: SpeakerShadowReconciliationReceipt,
        *,
        successor: SpeakerShadowCandidateKey | None,
    ) -> Literal["completed", "already_completed", "pending", "stale", "invalid"]:
        """Retire an applied proof's authority after its successor was handed off.

        The exact receipt is the capability; its immutable suffix records the
        original transfer even after that suffix enters another batch. Normal
        completion never rewrites a score or resurrects a terminal candidate.
        The bounded receipt registry retains the idempotence tombstone.
        """
        if type(receipt) is SpeakerShadowBatchReconcileReceipt:
            status = self.reconciliation_status(receipt)
            record = self._reconciliations.get(receipt.batch_id)
        elif type(receipt) is SpeakerShadowTerminalCoverageReceipt:
            status = self.terminal_coverage_status(receipt)
            record = self._terminal_coverages.get(receipt.batch_id)
        else:
            return "stale"
        if status == "stale" or self._closed or self._resetting:
            return "stale"
        if record is None or record.marker.receipt is not receipt:
            return "stale"
        if successor != record.marker.suffix:
            return "invalid"
        if record.completed:
            return "already_completed"
        if status == "pending":
            return "pending"
        if status != "applied":
            return "invalid"
        record.completed = True
        return "completed"

    def _revoke_candidate_batch_reconciliation(
        self,
        marker: _CandidateBatchReconciliation,
    ) -> None:
        """Revoke one owned batch exactly once, including reset/close drains."""

        record = self._reconciliations.get(marker.batch_id)
        if (
            record is None
            or record.marker is not marker
            or record.state == "failed"
            or record.completed
        ):
            return
        self._metrics.reconciliation_batch_revoked_count += 1
        self._fail_candidate_batch_reconciliation(marker)

    def _revoke_all_candidate_batch_reconciliations(self) -> None:
        """Invalidate every still-addressable receipt before generation rotation."""

        for record in tuple(self._reconciliations.values()):
            self._revoke_candidate_batch_reconciliation(record.marker)

    def _revoke_terminal_coverage_record(
        self,
        marker: _CandidateTerminalCoverage,
    ) -> None:
        record = self._terminal_coverages.get(marker.batch_id)
        if (
            record is None
            or record.marker is not marker
            or record.state == "failed"
            or record.completed
        ):
            return
        self._metrics.reconciliation_batch_revoked_count += 1
        self._fail_terminal_coverage(marker)

    def _revoke_all_terminal_coverages(self) -> None:
        for record in tuple(self._terminal_coverages.values()):
            self._revoke_terminal_coverage_record(record.marker)

    def _evaluated_through_samples(
        self,
        candidate: SpeakerShadowCandidateKey,
        token: _CandidateToken,
    ) -> int:
        """Return the prefix already exposed to scoring for an in-place split."""

        checkpoint_ms = token.last_checkpoint_ms
        if self._active_evaluation == (self._generation, candidate):
            checkpoints = self._config.observation_checkpoints_ms or (
                self._config.minimum_audio_ms,
            )
            buffer = self._buffers.get(candidate)
            if self._active_evaluation_terminal:
                active_checkpoint_ms = checkpoints[-1]
            elif buffer is not None and buffer.next_checkpoint_index > 0:
                active_checkpoint_ms = checkpoints[buffer.next_checkpoint_index - 1]
            else:
                active_checkpoint_ms = checkpoints[0]
            checkpoint_ms = max(checkpoint_ms or 0, active_checkpoint_ms)
        if checkpoint_ms is None:
            return 0
        return math.ceil(SPEAKER_SHADOW_SAMPLE_RATE_HZ * checkpoint_ms / 1_000)

    def requires_provisional_decision(
        self,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        """Whether accepted PCM has reached a still-undelivered checkpoint."""

        if (
            self._resetting
            or not self.enabled
            or not isinstance(candidate, SpeakerShadowCandidateKey)
            or candidate.scope not in self._config.pending_observation_gate_scopes
            or candidate in self._finalized
        ):
            return False
        token = self._candidate_tokens.get(candidate)
        if (
            token is None
            or token.candidate != candidate
            or token.terminal_reason is not None
            or self._candidate_was_evicted(candidate, token=token)
        ):
            return False
        explicit_checkpoints = self._config.observation_checkpoints_ms
        first_checkpoint_ms = (
            explicit_checkpoints[0]
            if explicit_checkpoints is not None
            else self._config.minimum_audio_ms
        )
        first_checkpoint_samples = math.ceil(
            token.sample_rate_hz * first_checkpoint_ms / 1_000
        )
        delivered_checkpoint_ms = token.last_delivered_checkpoint_ms
        return (
            token.sample_rate_hz == SPEAKER_SHADOW_SAMPLE_RATE_HZ
            and (not token.scoring_deferred or token.activation_queued)
            and token.accepted_sample_count >= first_checkpoint_samples
            and (
                delivered_checkpoint_ms is None
                or delivered_checkpoint_ms < first_checkpoint_ms
            )
        )

    def snapshot(self) -> dict[str, int]:
        prepared_audio_bytes = sum(
            len(record.suffix_scratch_pcm16)
            for record in self._prepared_exact_intervals.values()
        ) + sum(
            len(record.suffix_scratch_pcm16)
            for record in self._prepared_terminal_coverages.values()
        )
        buffered_audio_bytes = prepared_audio_bytes + sum(
            len(buffer.pcm16) for buffer in self._buffers.values()
        )
        host_pcm_bytes = (
            self._backend_host.pcm_bytes_in_use if self._backend_host is not None else 0
        )
        snapshot = self._metrics.snapshot()
        snapshot.update(
            buffered_candidate_count=len(self._buffers),
            buffered_audio_bytes=buffered_audio_bytes,
            queued_audio_bytes=self._queued_pcm_bytes,
            active_audio_bytes=self._active_pcm_bytes,
            retained_pcm_bytes=(
                buffered_audio_bytes
                + self._queued_pcm_bytes
                + self._active_pcm_bytes
                + host_pcm_bytes
            ),
            finalized_tombstone_count=len(self._finalized),
            queued_item_count=self._queue.qsize(),
            pending_terminal_count=self._queued_terminal_count,
            pending_completion_count=self._completion_queue.qsize(),
            detached_callback_task_count=sum(
                not task.done() for task in self._detached_callback_tasks
            ),
            delivery_degraded_cause_count=len(self._degraded_causes),
            in_flight_candidate_count=int(self._active_evaluation is not None),
            worker_task_count=int(
                self._worker_task is not None and not self._worker_task.done()
            ),
            callback_task_count=int(
                self._observation_task is not None and not self._observation_task.done()
            )
            + int(
                self._completion_callback_task is not None
                and not self._completion_callback_task.done()
            )
            + sum(not task.done() for task in self._detached_callback_tasks),
            completion_dispatcher_task_count=int(
                self._completion_dispatcher_task is not None
                and not self._completion_dispatcher_task.done()
            ),
            cleanup_task_count=int(
                self._cleanup_task is not None and not self._cleanup_task.done()
            ),
            host_start_task_count=int(
                self._host_start_task is not None and not self._host_start_task.done()
            ),
            backend_loaded_count=int(
                self._backend_host is not None
                and self._backend_host.alive
                and self._backend_host.loaded
            ),
            backend_process_count=(
                self._backend_host.process_count
                if self._backend_host is not None
                else 0
            ),
            backend_close_failed_count=0,
        )
        snapshot.update(
            {
                self._threshold_metric_key(threshold): count
                for threshold, count in self._would_block_counts.items()
            }
        )
        return snapshot

    def submit(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> bool:
        """Queue PCM while preserving the original boolean observer contract."""

        return (
            self.submit_capture(
                pcm16,
                sample_rate_hz=sample_rate_hz,
                candidate=candidate,
            ).accepted_sample_count
            > 0
        )

    def submit_capture(
        self,
        pcm16: bytes,
        *,
        sample_rate_hz: int,
        candidate: SpeakerShadowCandidateKey,
    ) -> SpeakerShadowCaptureResult:
        """Submit once and report normal completion separately from data loss."""

        if self._resetting or not self.enabled:
            return self._capture_result(candidate, unavailable=True)
        if not isinstance(candidate, SpeakerShadowCandidateKey):
            return self._capture_result(candidate, unavailable=True)
        if not isinstance(pcm16, bytes) or not pcm16 or len(pcm16) % 2:
            return self._capture_result(candidate, unavailable=True)
        if sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ:
            self._metrics.dropped_frame_count += 1
            return self._capture_result(candidate, unavailable=True)
        if len(pcm16) > MAX_SPEAKER_SHADOW_FRAME_PCM_BYTES:
            self._metrics.dropped_frame_count += 1
            self._metrics.dropped_audio_ms += self._audio_ms(
                len(pcm16) // 2,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            )
            self._drop_candidate(candidate)
            return self._capture_result(candidate, unavailable=True)

        prepared = next(
            (
                record
                for record in self._prepared_exact_intervals.values()
                if record.marker.generation == self._generation
                and record.marker.sources[-1].source.candidate == candidate
            ),
            None,
        )
        if prepared is not None:
            return self._stage_prepared_exact_interval_capture(prepared, pcm16)
        prepared_terminal = next(
            (
                record
                for record in self._prepared_terminal_coverages.values()
                if record.marker.generation == self._generation
                and record.marker.sources[-1].candidate == candidate
            ),
            None,
        )
        if prepared_terminal is not None:
            return self._stage_prepared_terminal_coverage_capture(
                prepared_terminal,
                pcm16,
            )

        identity = (self._generation, candidate)
        finalized = self._finalized.get(candidate)
        if finalized is not None:
            return self._capture_result(
                candidate,
                unavailable=finalized.terminal_reason != "scored",
            )
        if self._candidate_was_evicted(candidate):
            return self._capture_result(candidate, unavailable=True)
        if identity == self._active_evaluation and self._active_evaluation_terminal:
            return self._capture_result(candidate)

        token = self._candidate_tokens.get(candidate)
        if token is not None and (
            token.sample_rate_hz != sample_rate_hz
            and not (token.sample_rate_hz == 0 and token.deferred_requested)
        ):
            self._metrics.dropped_frame_count += 1
            self._metrics.dropped_audio_ms += self._audio_ms(
                len(pcm16) // 2,
                sample_rate_hz,
            )
            self._drop_candidate(candidate, token=token)
            return self._capture_result(candidate, unavailable=True, token=token)
        if token is not None and token.pcm_frozen:
            return self._capture_result(
                candidate,
                unavailable=not self._capture_is_complete(token),
                token=token,
            )
        if token is None:
            if (
                self._buffer_slots_in_use() >= self._config.buffered_candidate_capacity
                and (self._prepared_exact_intervals
                     or any(item.state == "pending" for item in self._reconciliations.values())
                     or self._prepared_terminal_coverages
                     or any(item.state == "pending" for item in self._terminal_coverages.values())
                     or any(buffer.exact_boundary_deadline is not None for buffer in self._buffers.values()))
            ):
                return self._capture_result(candidate, unavailable=True)
            token = _CandidateToken(candidate, sample_rate_hz)
        elif token.sample_rate_hz == 0 and token.deferred_requested:
            token.sample_rate_hz = sample_rate_hz
        accepted_sample_count = token.accepted_sample_count
        maximum_samples = sample_rate_hz * self._config.maximum_audio_ms // 1_000
        terminal_window_samples = self._terminal_scoring_window_samples()
        candidate_capacity = (
            min(maximum_samples, terminal_window_samples)
            if (token.anchor_queued or token.anchor_applied)
            and terminal_window_samples > 0
            else maximum_samples
        )
        rolling_deferred = bool(
            token.deferred_requested
            and token.scoring_deferred
            and not token.anchor_queued
            and not token.anchor_applied
            and token.rolling_buffer_deferred
        )
        remaining_samples = (
            len(pcm16) // 2
            if rolling_deferred
            else candidate_capacity - accepted_sample_count
        )
        if remaining_samples <= 0:
            self._metrics.dropped_frame_count += 1
            self._metrics.dropped_audio_ms += self._audio_ms(
                len(pcm16) // 2,
                sample_rate_hz,
            )
            return self._capture_result(candidate, token=token)
        input_sample_count = len(pcm16) // 2
        sample_count = min(input_sample_count, remaining_samples)
        if sample_count <= 0:
            return self._capture_result(candidate, token=token)
        if sample_count < input_sample_count:
            self._metrics.dropped_audio_ms += self._audio_ms(
                input_sample_count - sample_count,
                sample_rate_hz,
            )
        bounded_pcm16 = bytearray(memoryview(pcm16)[: sample_count * 2])
        frame = _AudioFrame(
            generation=self._generation,
            candidate=candidate,
            token=token,
            pcm16=bounded_pcm16,
            sample_rate_hz=sample_rate_hz,
            sample_count=sample_count,
            rolling_deferred=rolling_deferred,
        )
        if self._retained_pcm_bytes() + len(bounded_pcm16) > (
            MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES
        ):
            self._metrics.dropped_frame_count += 1
            self._metrics.dropped_audio_ms += self._audio_ms(
                sample_count,
                sample_rate_hz,
            )
            self._wipe_bytearray(bounded_pcm16)
            self._drop_candidate(candidate, token=token)
            return self._capture_result(candidate, unavailable=True, token=token)
        if not self._admit_data_item(frame):
            self._metrics.dropped_frame_count += 1
            self._metrics.dropped_audio_ms += self._audio_ms(
                sample_count, sample_rate_hz
            )
            self._wipe_bytearray(bounded_pcm16)
            self._drop_candidate(candidate, token=token)
            return self._capture_result(candidate, unavailable=True, token=token)
        self._queued_pcm_bytes += len(bounded_pcm16)
        token.accepted_sample_count = accepted_sample_count + sample_count
        if rolling_deferred:
            token.deferred_retained_start_sample_count = max(
                token.deferred_retained_start_sample_count,
                token.accepted_sample_count - maximum_samples,
            )
        self._candidate_tokens[candidate] = token
        self._candidate_tokens.move_to_end(candidate)
        self._metrics.submitted_frame_count += 1
        self._metrics.submitted_audio_ms += self._audio_ms(sample_count, sample_rate_hz)
        return self._capture_result(
            candidate,
            accepted_sample_count=sample_count,
            token=token,
        )

    def _stage_prepared_terminal_coverage_capture(
        self,
        record: _PreparedTerminalCoverageRecord,
        pcm16: bytes,
    ) -> SpeakerShadowCaptureResult:
        """Retain post-boundary PCM without publishing terminal ownership."""

        marker = record.marker
        suffix_token = marker.suffix_token
        source = marker.sources[-1]
        source_is_finalized_target = source.candidate == marker.target
        source_token = (
            marker.target_token
            if source_is_finalized_target
            else next(
                (
                    reserved.token
                    for reserved in marker.reserved_sources
                    if reserved.source.candidate == source.candidate
                ),
                None,
            )
        )
        finalized = self._finalized.get(marker.target)
        source_is_current = bool(
            (
                source_is_finalized_target
                and finalized is not None
                and finalized.terminal_reason == "scored"
                and finalized.token is source_token
            )
            or (
                not source_is_finalized_target
                and source_token is not None
                and self._candidate_tokens.get(source.candidate) is source_token
                and source_token.reconciliation_batch_id == marker.batch_id
            )
        )
        if (
            marker.suffix is None
            or suffix_token is None
            or source_token is None
            or not source_is_current
            or self._candidate_tokens.get(marker.suffix) is not suffix_token
            or suffix_token.reconciliation_batch_id != marker.batch_id
        ):
            return self._capture_result(
                source.candidate,
                unavailable=True,
                token=source_token,
            )
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        input_samples = len(pcm16) // 2
        remaining_samples = maximum_samples - suffix_token.accepted_sample_count
        sample_count = min(input_samples, max(0, remaining_samples))
        if sample_count <= 0 or self._retained_pcm_bytes() + sample_count * 2 > (
            MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES
        ):
            return self._capture_result(
                source.candidate,
                unavailable=True,
                token=source_token,
            )
        record.suffix_scratch_pcm16.extend(memoryview(pcm16)[: sample_count * 2])
        suffix_token.accepted_sample_count += sample_count
        self._metrics.submitted_frame_count += 1
        self._metrics.submitted_audio_ms += self._audio_ms(
            sample_count,
            SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        )
        if sample_count < input_samples:
            self._metrics.dropped_audio_ms += self._audio_ms(
                input_samples - sample_count,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            )
        return SpeakerShadowCaptureResult(
            disposition=SpeakerShadowCaptureDisposition.ACCEPTED,
            accepted_sample_count=sample_count,
            cumulative_sample_count=(
                source.expected_sample_count
                + len(record.suffix_scratch_pcm16) // 2
            ),
            completed_window_sample_count=marker.target_token.scored_sample_count,
            decision_state=SpeakerShadowCaptureDecisionState.PENDING,
        )

    def _stage_prepared_exact_interval_capture(
        self,
        record: _PreparedExactIntervalRecord,
        pcm16: bytes,
    ) -> SpeakerShadowCaptureResult:
        """Retain post-boundary PCM behind an unpublished suffix reservation."""

        marker = record.marker
        source = marker.sources[-1]
        suffix_token = marker.suffix_token
        if (
            marker.suffix is None
            or suffix_token is None
            or self._exact_source_token(source.source.candidate) is not source.token
            or self._candidate_tokens.get(marker.suffix) is not suffix_token
            or source.token.reconciliation_batch_id != marker.batch_id
            or suffix_token.reconciliation_batch_id != marker.batch_id
        ):
            return self._capture_result(
                source.source.candidate,
                unavailable=True,
                token=source.token,
            )
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        input_samples = len(pcm16) // 2
        remaining_samples = maximum_samples - suffix_token.accepted_sample_count
        sample_count = min(input_samples, max(0, remaining_samples))
        if sample_count <= 0 or self._retained_pcm_bytes() + sample_count * 2 > (
            MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES
        ):
            return self._capture_result(
                source.source.candidate,
                unavailable=True,
                token=source.token,
            )
        record.suffix_scratch_pcm16.extend(memoryview(pcm16)[: sample_count * 2])
        suffix_token.accepted_sample_count += sample_count
        self._metrics.submitted_frame_count += 1
        self._metrics.submitted_audio_ms += self._audio_ms(
            sample_count,
            SPEAKER_SHADOW_SAMPLE_RATE_HZ,
        )
        if sample_count < input_samples:
            self._metrics.dropped_audio_ms += self._audio_ms(
                input_samples - sample_count,
                SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            )
        return SpeakerShadowCaptureResult(
            disposition=SpeakerShadowCaptureDisposition.ACCEPTED,
            accepted_sample_count=sample_count,
            cumulative_sample_count=(
                record.source_accepted_sample_count
                + len(record.suffix_scratch_pcm16) // 2
            ),
            completed_window_sample_count=source.token.scored_sample_count,
            decision_state=SpeakerShadowCaptureDecisionState.PENDING,
        )

    def _capture_result(
        self,
        candidate: object,
        *,
        accepted_sample_count: int = 0,
        unavailable: bool = False,
        token: _CandidateToken | None = None,
    ) -> SpeakerShadowCaptureResult:
        if not isinstance(candidate, SpeakerShadowCandidateKey):
            token = None
        elif token is None:
            token = self._candidate_tokens.get(candidate)
            finalized = self._finalized.get(candidate)
            if token is None and finalized is not None:
                token = finalized.token
        cumulative_sample_count = (
            token.accepted_sample_count if token is not None else 0
        )
        completed_window_sample_count = (
            self._completed_scoring_window_samples(token) if token is not None else 0
        )
        terminal_reason = token.terminal_reason if token is not None else None
        if isinstance(candidate, SpeakerShadowCandidateKey):
            finalized = self._finalized.get(candidate)
            if finalized is not None:
                terminal_reason = finalized.terminal_reason
        if unavailable or terminal_reason in {"failed", "dropped", "insufficient"}:
            disposition = SpeakerShadowCaptureDisposition.UNAVAILABLE
            decision_state = SpeakerShadowCaptureDecisionState.UNAVAILABLE
        elif terminal_reason == "scored":
            disposition = SpeakerShadowCaptureDisposition.COMPLETE
            decision_state = SpeakerShadowCaptureDecisionState.SCORED
        elif token is not None and self._capture_is_complete(token):
            disposition = SpeakerShadowCaptureDisposition.COMPLETE
            decision_state = SpeakerShadowCaptureDecisionState.PENDING
        else:
            disposition = SpeakerShadowCaptureDisposition.ACCEPTED
            decision_state = SpeakerShadowCaptureDecisionState.PENDING
        return SpeakerShadowCaptureResult(
            disposition=disposition,
            accepted_sample_count=accepted_sample_count,
            cumulative_sample_count=cumulative_sample_count,
            completed_window_sample_count=completed_window_sample_count,
            decision_state=decision_state,
        )

    def _capture_is_complete(self, token: _CandidateToken) -> bool:
        if token.scoring_deferred and not token.anchor_applied:
            maximum_samples = (
                SPEAKER_SHADOW_SAMPLE_RATE_HZ
                * self._config.maximum_audio_ms
                // 1_000
            )
            if token.anchor_queued:
                terminal_window_samples = self._terminal_scoring_window_samples()
                return bool(
                    terminal_window_samples > 0
                    and token.accepted_sample_count
                    >= min(maximum_samples, terminal_window_samples)
                )
            return bool(
                not token.rolling_buffer_deferred
                and token.accepted_sample_count >= maximum_samples
            )
        if token.terminal_reason == "scored":
            return True
        terminal_window_samples = self._terminal_scoring_window_samples()
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        return bool(
            (
                terminal_window_samples > 0
                and token.accepted_sample_count >= terminal_window_samples
            )
            or token.accepted_sample_count >= maximum_samples
            or (
                self._active_evaluation == (self._generation, token.candidate)
                and self._active_evaluation_terminal
            )
        )

    def _terminal_scoring_window_samples(self) -> int:
        checkpoints = self._config.observation_checkpoints_ms
        if checkpoints is None:
            return 0
        return math.ceil(SPEAKER_SHADOW_SAMPLE_RATE_HZ * checkpoints[-1] / 1_000)

    def _completed_scoring_window_samples(self, token: _CandidateToken) -> int:
        if token.scored_sample_count > 0:
            return token.scored_sample_count
        checkpoints = self._config.observation_checkpoints_ms
        if checkpoints is None:
            return 0
        completed_ms = max(
            (
                checkpoint
                for checkpoint in checkpoints
                if token.accepted_sample_count
                >= math.ceil(SPEAKER_SHADOW_SAMPLE_RATE_HZ * checkpoint / 1_000)
            ),
            default=0,
        )
        return math.ceil(SPEAKER_SHADOW_SAMPLE_RATE_HZ * completed_ms / 1_000)

    def abandon_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        """Fence and wipe one candidate without publishing a terminal fact.

        The external owner is authoritative for lifecycle abandonment.  This
        candidate-local control only makes queued/in-flight work stale and
        releases retained PCM; it deliberately does not turn abandonment into
        speaker-unavailable evidence.
        """

        if self._resetting or self._closed:
            return False
        if not isinstance(candidate, SpeakerShadowCandidateKey):
            return False

        finalized = self._finalized.get(candidate)
        token = self._candidate_tokens.get(candidate)
        buffer = self._buffers.get(candidate)
        if token is None and buffer is not None:
            token = buffer.token
        if token is None and finalized is not None:
            token = finalized.token

        if finalized is None:
            self._drop_candidate(candidate, token=token)
        else:
            retained = self._buffers.pop(candidate, None)
            if retained is not None:
                self._metrics.dropped_audio_ms += retained.audio_ms
                self._wipe_bytearray(retained.pcm16)
            self._candidate_tokens.pop(candidate, None)

        if token is not None:
            token.finish_state = _FinishState.ABANDONED
            token.evidence_complete = False
            token.evidence_closed = True
            self._abandon_completion(token)
        return True

    def finish_candidate(self, candidate: SpeakerShadowCandidateKey) -> bool:
        """Order the terminal boundary behind all previously accepted PCM."""

        if self._resetting or not self.enabled:
            return False
        if not isinstance(candidate, SpeakerShadowCandidateKey):
            return False
        finalized = self._finalized.get(candidate)
        if finalized is not None and finalized.finish_seen:
            return True
        if self._candidate_was_evicted(candidate):
            return True
        token = self._candidate_tokens.get(candidate)
        if token is None and finalized is not None:
            token = finalized.token
        if token is None:
            token = _CandidateToken(candidate, 0)
            if finalized is not None:
                token.terminal_reason = finalized.terminal_reason
        if token.finish_state in {
            _FinishState.QUEUED,
            _FinishState.PROCESSED,
        }:
            return True
        if token.finish_state is _FinishState.ABANDONED:
            return False
        marker = _CandidateFinished(self._generation, candidate, token)
        if not self._admit_terminal_item(marker):
            self._abandon_terminal(candidate, token=token)
            return False
        self._candidate_tokens[candidate] = token
        self._candidate_tokens.move_to_end(candidate)
        token.finish_state = _FinishState.QUEUED
        self._metrics.terminal_queued_count += 1
        return True

    def _admit_data_item(self, item: _QueueItem) -> bool:
        if self._resetting or self._closed:
            return False
        if self._queued_data_item_count >= self._config.queue_capacity:
            return False
        if not self._ensure_worker():
            self._metrics.worker_start_failure_count += 1
            self._set_degraded_cause("worker_start_failure")
            return False
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            return False
        self._queued_data_item_count += 1
        return True

    def _admit_batch_item(
        self,
        marker: _CandidateBatchReconciliation | _CandidateTerminalCoverage,
    ) -> bool:
        """Reserve one physical marker against data and terminal budgets."""

        if self._resetting or self._closed:
            return False
        if self._queued_data_item_count >= self._config.queue_capacity:
            return False
        if self._queued_terminal_count >= self._config.terminal_queue_capacity:
            self._metrics.terminal_overflow_count += 1
            self._set_degraded_cause("terminal_overflow")
            return False
        if not self._ensure_worker():
            self._metrics.worker_start_failure_count += 1
            self._set_degraded_cause("worker_start_failure")
            return False
        try:
            self._queue.put_nowait(marker)
        except asyncio.QueueFull:
            self._metrics.terminal_overflow_count += 1
            self._set_degraded_cause("terminal_overflow")
            return False
        self._queued_data_item_count += 1
        self._queued_terminal_count += 1
        return True

    def _admit_terminal_item(self, marker: _CandidateFinished) -> bool:
        if self._resetting or self._closed:
            return False
        if self._queued_terminal_count >= self._config.terminal_queue_capacity:
            self._metrics.terminal_overflow_count += 1
            self._set_degraded_cause("terminal_overflow")
            return False
        if not self._ensure_worker():
            self._metrics.worker_start_failure_count += 1
            self._set_degraded_cause("worker_start_failure")
            return False
        try:
            self._queue.put_nowait(marker)
        except asyncio.QueueFull:
            self._metrics.terminal_overflow_count += 1
            self._set_degraded_cause("terminal_overflow")
            return False
        self._queued_terminal_count += 1
        return True

    def _abandon_terminal(
        self,
        candidate: SpeakerShadowCandidateKey,
        *,
        token: _CandidateToken,
    ) -> None:
        self._publish_terminal_admission_unavailable(candidate, token=token)
        self._abandon_completion(token)
        if token.finish_state in {
            _FinishState.PROCESSED,
            _FinishState.ABANDONED,
        }:
            return
        token.finish_state = _FinishState.ABANDONED
        self._metrics.terminal_abandoned_count += 1
        self._drop_candidate(candidate, token=token)

    def _publish_terminal_admission_unavailable(
        self,
        candidate: SpeakerShadowCandidateKey,
        *,
        token: _CandidateToken,
    ) -> None:
        """Close speaker authority when its ordered terminal cannot be queued."""

        current = self._candidate_tokens.get(candidate)
        if (
            self._closed
            or self._resetting
            or token.evidence_closed
            or candidate in self._finalized
            or (current is not None and current is not token)
        ):
            return
        sequence_no = token.evidence_sequence_no + 1
        token.evidence_sequence_no = sequence_no
        unavailable = SpeakerShadowObservation(
            candidate=candidate,
            similarity=0.0,
            would_block=(),
            audio_ms=self._audio_ms(
                token.accepted_sample_count,
                token.sample_rate_hz or SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            ),
            sequence_no=sequence_no,
            evidence_available=False,
        )
        self._publish_evidence(unavailable, token=token)
        completion = SpeakerShadowCompletion(
            candidate=candidate,
            terminal_reason="dropped",
            last_checkpoint_ms=token.last_checkpoint_ms,
            through_sequence_no=sequence_no,
            evidence_complete=token.evidence_complete,
        )
        token.evidence_closed = True
        self._publish_evidence(completion, token=token)

    async def wait_idle(self) -> None:
        """Wait for accepted work, excluding the warm-backend idle timer."""

        reset_task = self._reset_task
        if (
            reset_task is not None
            and reset_task is not asyncio.current_task()
            and not reset_task.done()
        ):
            await asyncio.shield(reset_task)
        while not self._queue.empty():
            worker = self._worker_task
            if worker is None or worker.done():
                if self._closed or self._resetting:
                    self._drain_queue()
                    break
                if not self._ensure_worker():
                    self._metrics.worker_start_failure_count += 1
                    self._set_degraded_cause("worker_start_failure")
                    self._drain_queue()
                    break
            await asyncio.sleep(0)
        await self._queue.join()
        load = self._backend_load_task
        while load is not None and not load.done():
            await asyncio.shield(load)
            # Loading publishes a same-queue marker; it is part of accepted work.
            await self._queue.join()
            load = self._backend_load_task
        await asyncio.sleep(0)
        while True:
            callback = self._completion_callback_task
            completion_idle = (
                self._completion_queue.empty()
                and not self._completion_dispatch_in_progress
                and (callback is None or callback.done())
            )
            if completion_idle:
                return
            if "dispatcher_start_failure" in self._degraded_causes:
                return
            if "completion_stalled" in self._degraded_causes and (
                (callback is not None and not callback.done())
                or bool(self._detached_completion_tokens)
            ):
                return
            dispatcher = self._completion_dispatcher_task
            if (
                not self._completion_queue.empty()
                and (dispatcher is None or dispatcher.done())
                and not self._ensure_completion_dispatcher()
            ):
                self._set_degraded_cause("dispatcher_start_failure")
                return
            await asyncio.sleep(0)

    async def reset(self) -> None:
        """Invalidate queued/in-flight results while retaining a warm backend."""

        if self._closed:
            return
        reset_task = self._reset_task
        if reset_task is None or reset_task.done():
            self._resetting = True
            self._abort_all_prepared_exact_intervals()
            self._abort_all_prepared_terminal_coverages()
            self._revoke_all_candidate_batch_reconciliations()
            self._revoke_all_terminal_coverages()
            self._generation += 1
            self._set_degraded_cause("resetting")
            reset_task = asyncio.create_task(
                self._reset_impl(),
                name="speaker-shadow-reset",
            )
            self._reset_task = reset_task
            reset_task.add_done_callback(self._consume_reset_result)
        await asyncio.shield(reset_task)

    async def _reset_impl(self) -> None:
        owned_tokens = list(self._owned_candidate_tokens())
        observation = self._observation_task
        try:
            await self._cancel_backend_load()
            if observation is not None and not observation.done():
                cancelled = await self._cancel_callback_bounded(observation)
                if not cancelled:
                    self._detach_callback(observation)
            if self._observation_task is observation:
                self._observation_task = None
            await self._cancel_completion_dispatcher_bounded()
        finally:
            try:
                self._drain_completion_queue()
                self._drain_queue(revoke_pending_batches=True)
                owned_tokens.extend(self._owned_candidate_tokens())
                self._sweep_reset_tokens(owned_tokens)
                self._clear_buffers()
                self._retire_finalized_candidates()
                self._candidate_tokens.clear()
                self._deferred_anchors.clear()
                self._prepared_exact_intervals.clear()
                self._prepared_terminal_coverages.clear()
                self._reconciliations.clear()
                self._terminal_coverages.clear()
                self._load_failure_streak = 0
                self._next_load_attempt_at = 0.0
            finally:
                self._resetting = False
                self._clear_degraded_cause("resetting")

    async def close(self) -> None:
        """Stop accepting work and release every tracked resource exactly once.

        Blocking backend calls live only in the dedicated spawn process. If the
        serial worker misses its grace period, cleanup terminates that process
        before joining the worker, so close has a hard resource boundary.
        """

        cleanup = self._cleanup_task
        if not self._closed:
            self._closed = True
            self._abort_all_prepared_exact_intervals()
            self._abort_all_prepared_terminal_coverages()
            self._revoke_all_candidate_batch_reconciliations()
            self._revoke_all_terminal_coverages()
            self._generation += 1
            reset_task = self._reset_task
            cleanup = asyncio.create_task(
                self._close_after_reset(reset_task),
                name="speaker-shadow-cleanup",
            )
            self._cleanup_task = cleanup
            cleanup.add_done_callback(self._consume_cleanup_result)
        if cleanup is None:
            self._close_parent_factory()
            return
        await asyncio.shield(cleanup)

    async def _close_after_reset(
        self,
        reset_task: asyncio.Task[None] | None,
    ) -> None:
        if reset_task is not None and not reset_task.done():
            try:
                await asyncio.shield(reset_task)
            except asyncio.CancelledError:
                if not reset_task.done():
                    raise
            except Exception:
                # Reset already ran its mandatory local cleanup in ``finally``.
                pass
        await self._cancel_backend_load()
        self._cancel_observation_callback()
        self._drain_queue(revoke_pending_batches=True)
        self._drain_completion_queue()
        self._clear_buffers()
        self._finalized.clear()
        self._candidate_tokens.clear()
        self._deferred_anchors.clear()
        self._prepared_exact_intervals.clear()
        self._prepared_terminal_coverages.clear()
        self._reconciliations.clear()
        self._terminal_coverages.clear()
        worker = self._worker_task
        if worker is not None and not worker.done():
            self._queue.put_nowait(_STOP)
        dispatcher = self._completion_dispatcher_task
        if dispatcher is not None and not dispatcher.done():
            self._completion_queue.put_nowait(_COMPLETION_STOP)
        needs_cleanup = (
            worker is not None
            or dispatcher is not None
            or self._backend_host is not None
            or self._host_start_task is not None
            or self._observation_task is not None
            or self._completion_callback_task is not None
            or bool(self._detached_callback_tasks)
        )
        if needs_cleanup:
            await self._cleanup_after_worker(worker)
        else:
            self._close_parent_factory()

    def _ensure_worker(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._owner_loop is None:
            self._owner_loop = loop
        elif self._owner_loop is not loop:
            return False
        if self._worker_task is not None and not self._worker_task.done():
            return True
        worker = loop.create_task(self._run(), name="speaker-shadow-runtime")
        worker.add_done_callback(self._consume_worker_result)
        self._worker_task = worker
        self._clear_degraded_cause("worker_start_failure")
        return True

    async def _run(self) -> None:
        while True:
            try:
                work_items = self._queue
                item = await asyncio.wait_for(
                    work_items.get(),
                    timeout=self._config.idle_unload_seconds,
                )
            except asyncio.TimeoutError:
                await self._unload_backend()
                if self._queue.empty():
                    return
                continue
            try:
                if item is _STOP:
                    return
                if isinstance(item, _BackendReady):
                    await self._process_backend_ready(item)
                elif isinstance(item, _CandidateFinished):
                    self._active_terminal_token = item.token
                    await self._process_finish(item)
                elif isinstance(item, _CandidateDeferred):
                    self._process_defer(item)
                elif isinstance(item, _CandidateAnchored):
                    await self._process_anchor(item)
                elif isinstance(item, _CandidateBatchReconciliation):
                    self._active_terminal_token = item.target_token
                    await self._process_candidate_batch_reconciliation(item)
                elif isinstance(item, _CandidateTerminalCoverage):
                    await self._process_terminal_coverage(item)
                elif isinstance(item, _CandidatePrefixReconciliation):
                    await self._process_prefix_reconciliation(item)
                elif isinstance(item, _CandidateActivated):
                    await self._process_activate(item)
                else:
                    assert isinstance(item, _AudioFrame)
                    await self._process_frame(item)
            except asyncio.CancelledError:
                if not self._closed and not self._resetting:
                    if isinstance(item, _CandidateFinished):
                        if item.token.finish_state is _FinishState.QUEUED:
                            self._abandon_terminal(item.candidate, token=item.token)
                        self._abandon_completion(item.token)
                    elif isinstance(item, _CandidateBatchReconciliation):
                        self._fail_candidate_batch_reconciliation(item)
                    elif isinstance(item, _CandidateTerminalCoverage):
                        self._fail_terminal_coverage(item)
                    elif isinstance(
                        item,
                        (
                            _AudioFrame,
                            _CandidateDeferred,
                            _CandidateAnchored,
                            _CandidateActivated,
                            _CandidatePrefixReconciliation,
                        ),
                    ):
                        if isinstance(item, _CandidatePrefixReconciliation):
                            self._fail_candidate_reconciliation(item)
                        elif isinstance(item, _CandidateAnchored):
                            self._fail_deferred_anchor(item)
                        else:
                            self._drop_candidate(item.candidate, token=item.token)
                raise
            except Exception:
                # A defensive final fence: shadow errors never reach ASR.
                self._metrics.inference_failure_count += 1
                if isinstance(item, _CandidateFinished):
                    self._recover_failed_finish(item)
                elif isinstance(item, _CandidateBatchReconciliation):
                    self._fail_candidate_batch_reconciliation(item)
                elif isinstance(item, _CandidateTerminalCoverage):
                    self._fail_terminal_coverage(item)
                elif isinstance(
                    item,
                    (
                        _AudioFrame,
                        _CandidateDeferred,
                        _CandidateAnchored,
                        _CandidateActivated,
                        _CandidatePrefixReconciliation,
                    ),
                ):
                    if isinstance(item, _CandidatePrefixReconciliation):
                        self._fail_candidate_reconciliation(item)
                    elif isinstance(item, _CandidateAnchored):
                        self._fail_deferred_anchor(item)
                    else:
                        self._finalize_candidate(
                            item.candidate,
                            "failed",
                            token=item.token,
                        )
            finally:
                if isinstance(item, _AudioFrame):
                    self._queued_pcm_bytes = max(
                        0,
                        self._queued_pcm_bytes - len(item.pcm16),
                    )
                    self._wipe_bytearray(item.pcm16)
                self._retire_queued_item(item)
                self._queue.task_done()
                if isinstance(
                    item, (_CandidateFinished, _CandidateBatchReconciliation)
                ) and self._active_terminal_token is (
                    item.token
                    if isinstance(item, _CandidateFinished)
                    else item.target_token
                ):
                    self._active_terminal_token = None
                item = None
            if (
                self._queue.empty()
                and self._backend_host is None
                and (self._backend_load_task is None or self._backend_load_task.done())
            ):
                return

    def _retire_queued_item(self, item: _QueueItem) -> None:
        if isinstance(
            item,
            (_CandidateBatchReconciliation, _CandidateTerminalCoverage),
        ):
            self._queued_data_item_count = max(0, self._queued_data_item_count - 1)
            self._queued_terminal_count = max(0, self._queued_terminal_count - 1)
            if self._queued_terminal_count == 0:
                self._clear_degraded_cause("terminal_overflow")
            return
        if isinstance(item, _CandidateFinished):
            self._queued_terminal_count = max(0, self._queued_terminal_count - 1)
            if self._queued_terminal_count == 0:
                self._clear_degraded_cause("terminal_overflow")
            return
        if isinstance(
            item,
            (
                _AudioFrame,
                _CandidateDeferred,
                _CandidateActivated,
                _CandidateAnchored,
                _CandidatePrefixReconciliation,
            ),
        ):
            self._queued_data_item_count = max(0, self._queued_data_item_count - 1)

    def _process_defer(self, marker: _CandidateDeferred) -> None:
        if not self._identity_is_current(
            marker.generation,
            marker.candidate,
            marker.token,
        ):
            return
        marker.token.defer_processed = True

    async def _process_anchor(self, marker: _CandidateAnchored) -> None:
        record = self._deferred_anchors.get(marker.receipt.operation_id)
        token = marker.token
        buffer = self._buffers.get(marker.candidate)
        if (
            buffer is None
            and marker.expected_observed_sample_count == 0
            and len(self._buffers) < self._config.buffered_candidate_capacity
        ):
            buffer = _CandidateBuffer(
                token=token,
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                pcm16=bytearray(),
            )
            self._buffers[marker.candidate] = buffer
            self._metrics.started_candidate_count += 1
        if (
            record is None
            or record.marker is not marker
            or record.state != "pending"
            or not self._identity_is_current(
                marker.generation,
                marker.candidate,
                token,
            )
            or not token.deferred_requested
            or not token.anchor_queued
            or token.anchor_revision != marker.receipt.anchor_revision
            or token.anchor_discard_prefix_sample_count
            != marker.discard_prefix_sample_count
            or buffer is None
            or buffer.token is not token
            or buffer.observed_sample_count
            != marker.expected_observed_sample_count
            or buffer.retained_start_sample_count
            > marker.discard_prefix_sample_count
        ):
            self._fail_deferred_anchor(marker)
            return

        trim_samples = (
            marker.discard_prefix_sample_count
            - buffer.retained_start_sample_count
        )
        if trim_samples > buffer.sample_count:
            self._fail_deferred_anchor(marker)
            return
        if trim_samples:
            prefix = buffer.pcm16[: trim_samples * 2]
            buffer.pcm16[: trim_samples * 2] = b"\x00" * len(prefix)
            del buffer.pcm16[: trim_samples * 2]
        buffer.sample_count -= trim_samples
        buffer.observed_sample_count = buffer.sample_count
        buffer.retained_start_sample_count = 0
        buffer.next_checkpoint_index = 0
        buffer.completion_confirmation_checkpoint_ms = None
        token.anchor_queued = False
        token.anchor_applied = True
        token.scoring_deferred = False
        record.state = "applied"
        record.settled.set()

        if not await self._prewarm_candidate_backend(
            generation=marker.generation,
            candidate=marker.candidate,
            token=token,
            buffer=buffer,
        ):
            self._fail_deferred_anchor(marker)
            return
        if token.pcm_frozen:
            return
        await self._process_buffer_checkpoints(
            generation=marker.generation,
            candidate=marker.candidate,
            token=token,
            buffer=buffer,
        )

    def _fail_deferred_anchor(self, marker: _CandidateAnchored) -> None:
        record = self._deferred_anchors.get(marker.receipt.operation_id)
        if record is not None and record.marker is marker:
            record.state = "failed"
            record.settled.set()
        marker.token.anchor_queued = False
        self._drop_candidate(marker.candidate, token=marker.token)

    async def _process_activate(self, marker: _CandidateActivated) -> None:
        token = marker.token
        if not self._identity_is_current(
            marker.generation,
            marker.candidate,
            token,
        ):
            return
        token.activation_queued = False
        if not token.deferred_requested or not token.defer_processed:
            self._drop_candidate(marker.candidate, token=token)
            return
        token.scoring_deferred = False
        # Reconciliation may synchronously freeze this candidate after its
        # activation marker was admitted.  Preserve activation state, but let
        # the later same-queue reconciliation split PCM before any checkpoint.
        if token.pcm_frozen:
            return
        buffer = self._buffers.get(marker.candidate)
        if buffer is None:
            return
        if buffer.token is not token:
            self._drop_candidate(marker.candidate, token=token)
            return
        if not await self._prewarm_candidate_backend(
            generation=marker.generation,
            candidate=marker.candidate,
            token=token,
            buffer=buffer,
        ):
            return
        if token.pcm_frozen:
            return
        await self._process_buffer_checkpoints(
            generation=marker.generation,
            candidate=marker.candidate,
            token=token,
            buffer=buffer,
        )

    async def _process_terminal_coverage(
        self,
        marker: _CandidateTerminalCoverage,
    ) -> None:
        """Apply exact ownership retirement while preserving one scored tombstone."""

        record = self._terminal_coverages.get(marker.batch_id)
        finalized = self._finalized.get(marker.target)
        if (
            marker.generation != self._generation
            or self._closed
            or self._resetting
            or record is None
            or record.marker is not marker
            or record.state != "pending"
            or finalized is None
            or finalized.terminal_reason != "scored"
            or finalized.token is not marker.target_token
            or marker.target_token.terminal_reason != "scored"
            or marker.target_token.scored_sample_count
            != marker.receipt.retained_sample_count
        ):
            self._fail_terminal_coverage(marker)
            return

        source_buffers: dict[SpeakerShadowCandidateKey, _CandidateBuffer] = {}
        for reserved in marker.reserved_sources:
            source = reserved.source
            token = reserved.token
            buffer = self._buffers.get(source.candidate)
            if (
                self._candidate_tokens.get(source.candidate) is not token
                or token.terminal_reason is not None
                or token.finish_state is not _FinishState.OPEN
                or not token.pcm_frozen
                or token.reconciliation_batch_id != marker.batch_id
                or token.accepted_sample_count != source.expected_sample_count
                or buffer is None
                or buffer.token is not token
                or buffer.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
                or buffer.sample_count != source.expected_sample_count
                or len(buffer.pcm16) != source.expected_sample_count * 2
            ):
                self._fail_terminal_coverage(marker)
                return
            source_buffers[source.candidate] = buffer

        suffix_token = marker.suffix_token
        suffix_retained_sample_count = 0
        if marker.suffix is None:
            if suffix_token is not None or marker.suffix_sample_count != 0:
                self._fail_terminal_coverage(marker)
                return
        else:
            suffix_from_live_source = marker.sources[-1].candidate != marker.target
            suffix_retained_sample_count = (
                marker.suffix_sample_count if suffix_from_live_source else 0
            )
            maximum_samples = (
                SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
            )
            if (
                suffix_token is None
                or self._candidate_tokens.get(marker.suffix) is not suffix_token
                or suffix_token.terminal_reason is not None
                or suffix_token.finish_state is not _FinishState.OPEN
                or suffix_token.pcm_frozen
                or suffix_token.reconciliation_batch_id != marker.batch_id
                or not (
                    suffix_retained_sample_count
                    <= suffix_token.accepted_sample_count
                    <= maximum_samples
                )
                or marker.suffix in self._buffers
                or marker.suffix in self._finalized
            ):
                self._fail_terminal_coverage(marker)
                return

        suffix_pcm = bytearray()
        try:
            if marker.suffix is not None and suffix_retained_sample_count > 0:
                last_source = marker.sources[-1]
                last_buffer = source_buffers.get(last_source.candidate)
                if last_buffer is None:
                    raise ValueError("terminal coverage suffix source is unavailable")
                suffix_pcm.extend(last_buffer.pcm16[last_source.keep_end_sample * 2 :])
                if len(suffix_pcm) != suffix_retained_sample_count * 2:
                    raise ValueError("terminal coverage suffix PCM length mismatch")
                if self._retained_pcm_bytes() + len(suffix_pcm) > (
                    MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES
                ):
                    raise ValueError("terminal coverage suffix exceeds PCM budget")
        except asyncio.CancelledError:
            self._wipe_bytearray(suffix_pcm)
            self._fail_terminal_coverage(marker)
            raise
        except Exception:
            self._wipe_bytearray(suffix_pcm)
            self._fail_terminal_coverage(marker)
            return

        # This validated marker already owns a terminal queue reservation.
        # Retain its finish authority while retiring sources below: inserting
        # their tombstones may evict the scored target. The marker owns that
        # token until the await-free scored finish, without pinning the table
        # or reviving an abandoned/already-processed finish.
        if marker.target_token.finish_state is _FinishState.OPEN:
            marker.target_token.finish_state = _FinishState.QUEUED

        for reserved in marker.reserved_sources:
            removed = self._buffers.pop(reserved.source.candidate, None)
            if removed is not None:
                self._wipe_bytearray(removed.pcm16)
            self._finalize_candidate(
                reserved.source.candidate,
                "dropped",
                token=reserved.token,
            )
        if marker.suffix is not None and suffix_token is not None:
            suffix_token.defer_processed = True
            suffix_token.reconciliation_batch_id = None
            if suffix_retained_sample_count > 0:
                self._buffers[marker.suffix] = _CandidateBuffer(
                    token=suffix_token,
                    sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                    pcm16=suffix_pcm,
                    sample_count=suffix_retained_sample_count,
                    observed_sample_count=suffix_retained_sample_count,
                )
                self._buffers.move_to_end(marker.suffix)
                self._metrics.started_candidate_count += 1
            else:
                self._wipe_bytearray(suffix_pcm)
        else:
            self._wipe_bytearray(suffix_pcm)
        record.state = "applied"
        record.settled.set()
        self._metrics.reconciliation_batch_applied_count += 1

        # Coverage of an already-scored window still closes its evidence
        # stream. Reuse ordered finish for exactly-once publication without
        # rescoring or touching resources transferred to the suffix.
        await self._process_finish(
            _CandidateFinished(marker.generation, marker.target, marker.target_token)
        )

    def _fail_terminal_coverage(
        self,
        marker: _CandidateTerminalCoverage,
    ) -> None:
        """Invalidate coverage without deleting the finalized scored verdict."""

        record = self._terminal_coverages.get(marker.batch_id)
        if record is not None and record.marker is marker:
            if record.state != "failed":
                self._metrics.reconciliation_batch_failed_count += 1
            record.state = "failed"
            record.settled.set()
        for reserved in marker.reserved_sources:
            self._drop_candidate(
                reserved.source.candidate,
                token=reserved.token,
            )
        if (
            marker.suffix is not None and marker.suffix_token is not None
            and not (record is not None and record.marker is marker and record.completed)
        ):
            self._drop_candidate(marker.suffix, token=marker.suffix_token)

    async def _process_candidate_batch_reconciliation(
        self,
        marker: _CandidateBatchReconciliation,
    ) -> None:
        """Apply one reserved ownership transaction before any scoring await."""

        record = self._reconciliations.get(marker.batch_id)
        if (
            marker.generation != self._generation
            or self._closed
            or self._resetting
            or record is None
            or record.marker is not marker
            or record.state != "pending"
        ):
            self._fail_candidate_batch_reconciliation(marker)
            return

        source_buffers: list[_CandidateBuffer | None] = []
        for reserved in marker.sources:
            source = reserved.source
            token = reserved.token
            expected_token_samples = (
                marker.target_sample_count
                if source.candidate == marker.target
                else source.expected_sample_count
            )
            expected_finish_state = (
                _FinishState.QUEUED
                if source.candidate == marker.target
                else _FinishState.OPEN
            )
            if (
                self._exact_source_token(source.candidate) is not token
                or token.finish_state is not expected_finish_state
                or not token.pcm_frozen
                or token.reconciliation_batch_id != marker.batch_id
                or token.accepted_sample_count != expected_token_samples
            ):
                self._fail_candidate_batch_reconciliation(marker)
                return
            buffer = self._buffers.get(source.candidate)
            if source.expected_sample_count == 0:
                if buffer is not None and buffer.sample_count != 0:
                    self._fail_candidate_batch_reconciliation(marker)
                    return
            elif (
                buffer is None
                or buffer.token is not token
                or buffer.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
                or buffer.sample_count != source.expected_sample_count
                or len(buffer.pcm16) != source.expected_sample_count * 2
            ):
                self._fail_candidate_batch_reconciliation(marker)
                return
            source_buffers.append(buffer)

        target_token = marker.target_token
        if (
            self._candidate_tokens.get(marker.target) is not target_token
            or target_token.terminal_reason is not None
            or target_token.finish_state is not _FinishState.QUEUED
            or not target_token.pcm_frozen
            or target_token.reconciliation_batch_id != marker.batch_id
            or target_token.accepted_sample_count != marker.target_sample_count
        ):
            self._fail_candidate_batch_reconciliation(marker)
            return
        suffix_token = marker.suffix_token
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        if marker.suffix is None:
            if suffix_token is not None or marker.suffix_sample_count != 0:
                self._fail_candidate_batch_reconciliation(marker)
                return
        elif (
            suffix_token is None
            or self._candidate_tokens.get(marker.suffix) is not suffix_token
            or suffix_token.terminal_reason is not None
            or suffix_token.finish_state is not _FinishState.OPEN
            or suffix_token.pcm_frozen
            or suffix_token.reconciliation_batch_id != marker.batch_id
            or not (
                marker.suffix_sample_count
                <= suffix_token.accepted_sample_count
                <= maximum_samples
            )
            or marker.suffix in self._buffers
            or marker.suffix in self._finalized
        ):
            self._fail_candidate_batch_reconciliation(marker)
            return

        scratch_bytes = (marker.target_sample_count + marker.suffix_sample_count) * 2
        if self._retained_pcm_bytes() + scratch_bytes > (
            MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES
        ):
            self._fail_candidate_batch_reconciliation(marker)
            return

        target_pcm = bytearray()
        suffix_pcm = bytearray()
        try:
            for reserved, buffer in zip(marker.sources, source_buffers, strict=True):
                source = reserved.source
                if source.keep_end_sample > source.keep_start_sample:
                    assert buffer is not None
                    target_pcm.extend(
                        buffer.pcm16[
                            source.keep_start_sample * 2 : source.keep_end_sample * 2
                        ]
                    )
            if marker.suffix is not None:
                for reserved, buffer in zip(marker.sources, source_buffers, strict=True):
                    source = reserved.source
                    if source.keep_end_sample < source.expected_sample_count:
                        assert buffer is not None
                        suffix_pcm.extend(buffer.pcm16[source.keep_end_sample * 2 :])
            if (
                len(target_pcm) != marker.target_sample_count * 2
                or len(suffix_pcm) != marker.suffix_sample_count * 2
            ):
                raise ValueError("speaker-shadow batch PCM length mismatch")
        except BaseException:
            self._wipe_bytearray(target_pcm)
            self._wipe_bytearray(suffix_pcm)
            self._fail_candidate_batch_reconciliation(marker)
            raise

        previous_target_buffer = (
            self._buffers.get(marker.target) if marker.target_was_existing else None
        )
        target_buffer = _CandidateBuffer(
            token=target_token,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            pcm16=target_pcm,
            sample_count=marker.target_sample_count,
            observed_sample_count=marker.target_sample_count,
            next_checkpoint_index=(
                previous_target_buffer.next_checkpoint_index
                if previous_target_buffer is not None
                else 0
            ),
            completion_confirmation_checkpoint_ms=(
                previous_target_buffer.completion_confirmation_checkpoint_ms
                if previous_target_buffer is not None
                else None
            ),
            backend_prewarm_attempted=(
                previous_target_buffer.backend_prewarm_attempted
                if previous_target_buffer is not None
                else False
            ),
        )
        suffix_buffer = (
            _CandidateBuffer(
                token=suffix_token,
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                pcm16=suffix_pcm,
                sample_count=marker.suffix_sample_count,
                observed_sample_count=marker.suffix_sample_count,
            )
            if marker.suffix is not None and suffix_token is not None
            else None
        )

        # Ownership commit is deliberately await-free.  From this point a
        # failure is fail-open cleanup, never a partial rollback to old owners.
        for reserved in marker.sources:
            removed = self._buffers.pop(reserved.source.candidate, None)
            if removed is not None:
                self._wipe_bytearray(removed.pcm16)
        self._buffers[marker.target] = target_buffer
        self._buffers.move_to_end(marker.target)
        if not marker.target_was_existing:
            self._metrics.started_candidate_count += 1
        target_token.scoring_deferred = False
        target_token.defer_processed = True
        for reserved in marker.sources:
            if reserved.source.candidate != marker.target:
                self._finalize_candidate(
                    reserved.source.candidate,
                    "dropped",
                    token=reserved.token,
                )
        if marker.suffix is not None and suffix_token is not None:
            assert suffix_buffer is not None
            suffix_token.defer_processed = True
            # The successor is now an ordinary live deferred candidate.  Its
            # batch-1 reservation must not prevent a later exact endpoint from
            # consuming it as a batch-2 source.
            suffix_token.reconciliation_batch_id = None
            self._buffers[marker.suffix] = suffix_buffer
            self._buffers.move_to_end(marker.suffix)
            self._metrics.started_candidate_count += 1
        record.state = "applied"
        record.settled.set()
        self._metrics.reconciliation_batch_applied_count += 1

        self._schedule_terminal_pcm_expiry()
        ready = await self._prewarm_candidate_backend(
            generation=marker.generation,
            candidate=marker.target,
            token=target_token,
            buffer=target_buffer,
        )
        if ready and record.state == "applied":
            await self._process_buffer_checkpoints(
                generation=marker.generation,
                candidate=marker.target,
                token=target_token,
                buffer=target_buffer,
                allow_frozen=True,
            )
        if record.state == "applied":
            await self._process_finish(
                _CandidateFinished(marker.generation, marker.target, target_token)
            )

    def _fail_candidate_batch_reconciliation(
        self,
        marker: _CandidateBatchReconciliation,
    ) -> None:
        """Revoke authority and terminalize every candidate touched by a batch."""

        record = self._reconciliations.get(marker.batch_id)
        if record is not None and record.marker is marker:
            if record.state != "failed":
                self._metrics.reconciliation_batch_failed_count += 1
            record.state = "failed"
            record.settled.set()
        seen: set[SpeakerShadowCandidateKey] = set()
        for reserved in marker.sources:
            candidate = reserved.source.candidate
            if candidate == marker.target or candidate in seen:
                continue
            seen.add(candidate)
            self._drop_candidate(candidate, token=reserved.token)
        target_token = marker.target_token
        if target_token.finish_state is _FinishState.QUEUED:
            self._abandon_terminal(marker.target, token=target_token)
        elif target_token.finish_state is _FinishState.OPEN:
            self._drop_candidate(marker.target, token=target_token)
        if (
            marker.suffix is not None and marker.suffix_token is not None
            and not (record is not None and record.marker is marker and record.completed)
        ):
            self._drop_candidate(marker.suffix, token=marker.suffix_token)

    async def _process_prefix_reconciliation(
        self,
        marker: _CandidatePrefixReconciliation,
    ) -> None:
        """Apply one reserved prefix transfer without exposing partial PCM."""

        if marker.generation != self._generation or self._closed or self._resetting:
            return
        source_token = marker.source_token
        if (
            self._candidate_tokens.get(marker.source) is not source_token
            or not source_token.pcm_frozen
            or source_token.terminal_reason is not None
        ):
            self._fail_candidate_reconciliation(marker)
            return
        source_buffer = self._buffers.get(marker.source)
        if marker.source_sample_count == 0:
            if source_buffer is not None and source_buffer.sample_count != 0:
                self._fail_candidate_reconciliation(marker)
                return
        elif (
            source_buffer is None
            or source_buffer.token is not source_token
            or source_buffer.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
            or source_buffer.sample_count != marker.source_sample_count
            or len(source_buffer.pcm16) != marker.source_sample_count * 2
        ):
            self._fail_candidate_reconciliation(marker)
            return

        suffix_pcm = bytearray()
        if source_buffer is not None and marker.suffix is not None:
            suffix_pcm.extend(source_buffer.pcm16[marker.prefix_sample_count * 2 :])

        if marker.source == marker.target:
            assert source_buffer is not None or marker.source_sample_count == 0
            if source_buffer is not None:
                prefix_bytes = marker.prefix_sample_count * 2
                removed_bytes = len(source_buffer.pcm16) - prefix_bytes
                if removed_bytes > 0:
                    source_buffer.pcm16[prefix_bytes:] = b"\x00" * removed_bytes
                    del source_buffer.pcm16[prefix_bytes:]
                source_buffer.sample_count = marker.prefix_sample_count
                self._buffers.move_to_end(marker.source)
            target_buffer = source_buffer
        else:
            target_buffer = self._reconciliation_target_buffer(marker)
            if target_buffer is False:
                self._wipe_bytearray(suffix_pcm)
                self._fail_candidate_reconciliation(marker)
                return
            if (
                isinstance(target_buffer, _CandidateBuffer)
                and source_buffer is not None
            ):
                transfer_bytes = marker.transferred_sample_count * 2
                if transfer_bytes > 0:
                    target_buffer.pcm16.extend(source_buffer.pcm16[:transfer_bytes])
                    target_buffer.sample_count += marker.transferred_sample_count
                self._buffers.move_to_end(marker.target)
            removed_source = self._buffers.pop(marker.source, None)
            if removed_source is not None:
                self._wipe_bytearray(removed_source.pcm16)
            self._finalize_candidate(
                marker.source,
                "dropped",
                token=source_token,
            )

        if not self._install_reconciliation_suffix(marker, suffix_pcm):
            self._wipe_bytearray(suffix_pcm)
            self._fail_candidate_reconciliation(marker)
            return
        self._wipe_bytearray(suffix_pcm)

        if not isinstance(target_buffer, _CandidateBuffer):
            return
        target_token = marker.target_token
        if not self._identity_is_current(
            marker.generation,
            marker.target,
            target_token,
        ):
            return
        if not await self._prewarm_candidate_backend(
            generation=marker.generation,
            candidate=marker.target,
            token=target_token,
            buffer=target_buffer,
        ):
            return
        if target_token.scoring_deferred:
            return
        await self._process_buffer_checkpoints(
            generation=marker.generation,
            candidate=marker.target,
            token=target_token,
            buffer=target_buffer,
            allow_frozen=(marker.source == marker.target),
        )

    def _reconciliation_target_buffer(
        self,
        marker: _CandidatePrefixReconciliation,
    ) -> _CandidateBuffer | Literal[False] | None:
        """Return an active target buffer, None for a terminal target."""

        target_token = marker.target_token
        finalized = self._finalized.get(marker.target)
        if finalized is not None and finalized.token is target_token:
            return None
        if (
            self._candidate_tokens.get(marker.target) is not target_token
            or target_token.terminal_reason is not None
        ):
            return False
        expected_reserved_samples = (
            marker.target_sample_count + marker.transferred_sample_count
        )
        maximum_samples = (
            SPEAKER_SHADOW_SAMPLE_RATE_HZ * self._config.maximum_audio_ms // 1_000
        )
        if not (
            expected_reserved_samples
            <= target_token.accepted_sample_count
            <= maximum_samples
        ):
            return False
        target_buffer = self._buffers.get(marker.target)
        if target_buffer is None:
            if marker.target_sample_count != 0:
                return False
            target_buffer = _CandidateBuffer(
                token=target_token,
                sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
                pcm16=bytearray(),
            )
            self._buffers[marker.target] = target_buffer
            self._metrics.started_candidate_count += 1
        elif (
            target_buffer.token is not target_token
            or target_buffer.sample_rate_hz != SPEAKER_SHADOW_SAMPLE_RATE_HZ
            or target_buffer.sample_count != marker.target_sample_count
            or len(target_buffer.pcm16) != marker.target_sample_count * 2
        ):
            return False
        return target_buffer

    def _install_reconciliation_suffix(
        self,
        marker: _CandidatePrefixReconciliation,
        pcm16: bytearray,
    ) -> bool:
        suffix = marker.suffix
        suffix_token = marker.suffix_token
        remainder_sample_count = marker.source_sample_count - marker.prefix_sample_count
        if suffix is None or suffix_token is None:
            return remainder_sample_count == 0 and not pcm16
        if (
            remainder_sample_count <= 0
            or len(pcm16) != remainder_sample_count * 2
            or self._candidate_tokens.get(suffix) is not suffix_token
            or suffix_token.terminal_reason is not None
            or not (
                remainder_sample_count
                <= suffix_token.accepted_sample_count
                <= (
                    SPEAKER_SHADOW_SAMPLE_RATE_HZ
                    * self._config.maximum_audio_ms
                    // 1_000
                )
            )
            or suffix in self._buffers
            or suffix in self._finalized
        ):
            return False
        suffix_token.defer_processed = True
        self._buffers[suffix] = _CandidateBuffer(
            token=suffix_token,
            sample_rate_hz=SPEAKER_SHADOW_SAMPLE_RATE_HZ,
            pcm16=bytearray(pcm16),
            sample_count=remainder_sample_count,
            observed_sample_count=remainder_sample_count,
        )
        self._metrics.started_candidate_count += 1
        return True

    def _fail_candidate_reconciliation(
        self,
        marker: _CandidatePrefixReconciliation,
    ) -> None:
        """Fail open and wipe every non-terminal candidate touched by a marker."""

        self._drop_candidate(marker.source, token=marker.source_token)
        if (
            marker.source != marker.target
            and marker.target_token.terminal_reason is None
        ):
            self._drop_candidate(marker.target, token=marker.target_token)
        if marker.suffix is not None and marker.suffix_token is not None:
            self._drop_candidate(marker.suffix, token=marker.suffix_token)

    async def _process_frame(self, frame: _AudioFrame) -> None:
        if frame.generation != self._generation:
            return
        if (
            frame.token.terminal_reason is not None
            or frame.candidate in self._finalized
            or self._candidate_was_evicted(
                frame.candidate,
                token=frame.token,
            )
        ):
            return
        buffer = self._buffers.get(frame.candidate)
        if buffer is None:
            if len(self._buffers) >= self._config.buffered_candidate_capacity:
                dropped_candidate = next(
                    (key for key in self._buffers if not self._candidate_pcm_is_reserved(key)), None,
                )
                if dropped_candidate is None:
                    self._drop_candidate(frame.candidate, token=frame.token)
                    return
                dropped_buffer = self._buffers.pop(dropped_candidate)
                self._metrics.dropped_audio_ms += dropped_buffer.audio_ms
                self._wipe_bytearray(dropped_buffer.pcm16)
                self._finalize_candidate(
                    dropped_candidate,
                    "dropped",
                    token=dropped_buffer.token,
                )
            buffer = _CandidateBuffer(
                token=frame.token,
                sample_rate_hz=frame.sample_rate_hz,
                pcm16=bytearray(),
            )
            self._buffers[frame.candidate] = buffer
            self._metrics.started_candidate_count += 1
        elif buffer.token is not frame.token:
            return
        elif buffer.sample_rate_hz != frame.sample_rate_hz:
            self._buffers.pop(frame.candidate, None)
            self._wipe_bytearray(buffer.pcm16)
            self._finalize_candidate(
                frame.candidate,
                "failed",
                token=frame.token,
            )
            return
        else:
            self._buffers.move_to_end(frame.candidate)

        maximum_samples = buffer.sample_rate_hz * self._config.maximum_audio_ms // 1_000
        if frame.rolling_deferred:
            buffer.pcm16.extend(frame.pcm16[: frame.sample_count * 2])
            buffer.sample_count += frame.sample_count
            buffer.observed_sample_count += frame.sample_count
            overflow_samples = max(0, buffer.sample_count - maximum_samples)
            if overflow_samples:
                prefix = buffer.pcm16[: overflow_samples * 2]
                buffer.pcm16[: overflow_samples * 2] = b"\x00" * len(prefix)
                del buffer.pcm16[: overflow_samples * 2]
                buffer.sample_count -= overflow_samples
                buffer.retained_start_sample_count += overflow_samples
        else:
            allowed_samples = min(
                frame.sample_count,
                maximum_samples - buffer.sample_count,
            )
            if allowed_samples > 0:
                buffer.pcm16.extend(frame.pcm16[: allowed_samples * 2])
                buffer.sample_count += allowed_samples
                buffer.observed_sample_count += allowed_samples
        if frame.token.pcm_frozen:
            return
        if not await self._prewarm_candidate_backend(
            generation=frame.generation,
            candidate=frame.candidate,
            token=frame.token,
            buffer=buffer,
        ):
            return
        if frame.token.pcm_frozen:
            return
        if frame.token.scoring_deferred:
            return
        await self._process_buffer_checkpoints(
            generation=frame.generation,
            candidate=frame.candidate,
            token=frame.token,
            buffer=buffer,
        )

    async def _prewarm_candidate_backend(
        self,
        *,
        generation: int,
        candidate: SpeakerShadowCandidateKey,
        token: _CandidateToken,
        buffer: _CandidateBuffer,
    ) -> bool:
        if (
            buffer.sample_count > 0
            and not buffer.backend_prewarm_attempted
            and candidate.scope in self._config.backend_prewarm_scopes
        ):
            buffer.backend_prewarm_attempted = True
            self._defer_until_backend_ready(
                generation=generation,
                candidate=candidate,
                token=token,
                buffer=buffer,
            )
        return True

    def _defer_until_backend_ready(
        self,
        *,
        generation: int,
        candidate: SpeakerShadowCandidateKey,
        token: _CandidateToken,
        buffer: _CandidateBuffer,
        allow_frozen: bool = False,
    ) -> bool:
        """Keep bounded references, never duplicate PCM or wait on the worker."""
        host = self._backend_host
        if host is not None and host.alive and host.loaded:
            return False
        pending = self._pending_backend_candidates
        for key, previous in tuple(pending.items()):
            if self._buffers.get(key) is not previous.buffer:
                pending.pop(key, None)
                if previous.finish is not None:
                    self._recover_failed_finish(previous.finish)
        previous = pending.get(candidate)
        if (
            previous is None
            or previous.token is not token
            or previous.buffer is not buffer
        ):
            if len(pending) >= self._config.buffered_candidate_capacity:
                self._drop_candidate(candidate, token=token)
                return True
            previous = _PendingBackendCandidate(generation, candidate, token, buffer)
            pending[candidate] = previous
        previous.allow_frozen |= allow_frozen
        task = self._backend_load_task
        if task is None or task.done():
            self._backend_load_task = asyncio.create_task(
                self._load_backend_and_notify(generation),
                name="speaker-shadow-backend-load",
            )
        return True

    async def _load_backend_and_notify(self, generation: int) -> None:
        host = await self._ensure_backend()
        if generation != self._generation or self._closed or self._resetting:
            return
        # One marker per load, on the existing ordered queue. Its put may wait
        # for capacity, but it never holds the PCM/control worker.
        await self._queue.put(_BackendReady(generation, host is not None))
        self._ensure_worker()

    async def _process_backend_ready(self, marker: _BackendReady) -> None:
        if marker.generation != self._generation or self._closed or self._resetting:
            return
        pending = tuple(self._pending_backend_candidates.values())
        self._pending_backend_candidates.clear()
        for index, item in enumerate(pending):
            if (
                not self._identity_is_current(item.generation, item.candidate, item.token)
                or self._buffers.get(item.candidate) is not item.buffer
            ):
                if item.finish is not None:
                    self._recover_failed_finish(item.finish)
                continue
            try:
                if not marker.available:
                    self._finalize_candidate(item.candidate, "failed", token=item.token)
                    self._buffers.pop(item.candidate, None)
                    self._wipe_bytearray(item.buffer.pcm16)
                elif not item.token.scoring_deferred:
                    await self._process_buffer_checkpoints(
                        generation=item.generation,
                        candidate=item.candidate,
                        token=item.token,
                        buffer=item.buffer,
                        allow_frozen=item.allow_frozen,
                    )
                if item.finish is not None:
                    await self._process_finish(item.finish)
            except asyncio.CancelledError:
                if not self._closed and not self._resetting:
                    for remaining in pending[index:]:
                        self._recover_pending_backend_work(remaining)
                raise
            except Exception:
                self._metrics.inference_failure_count += 1
                self._recover_pending_backend_work(item)

    def _recover_pending_backend_work(self, item: _PendingBackendCandidate) -> None:
        if item.generation != self._generation or self._closed or self._resetting:
            return
        if self._buffers.get(item.candidate) is item.buffer:
            self._buffers.pop(item.candidate, None)
            self._wipe_bytearray(item.buffer.pcm16)
        if item.finish is not None:
            self._recover_failed_finish(item.finish)
        elif self._candidate_tokens.get(item.candidate) is item.token:
            self._finalize_candidate(item.candidate, "failed", token=item.token)

    async def _cancel_backend_load(self) -> None:
        task = self._backend_load_task
        self._pending_backend_candidates.clear()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._backend_load_task is task:
            self._backend_load_task = None

    async def _process_buffer_checkpoints(
        self,
        *,
        generation: int,
        candidate: SpeakerShadowCandidateKey,
        token: _CandidateToken,
        buffer: _CandidateBuffer,
        allow_frozen: bool = False,
    ) -> None:
        explicit_checkpoints = self._config.observation_checkpoints_ms
        checkpoints = explicit_checkpoints or (self._config.minimum_audio_ms,)
        if (
            buffer.next_checkpoint_index < len(checkpoints)
            and buffer.audio_ms >= checkpoints[buffer.next_checkpoint_index]
            and self._defer_until_backend_ready(
                generation=generation,
                candidate=candidate,
                token=token,
                buffer=buffer,
                allow_frozen=allow_frozen,
            )
        ):
            return
        while buffer.next_checkpoint_index < len(checkpoints):
            if token.pcm_frozen and not allow_frozen:
                return
            checkpoint_index = buffer.next_checkpoint_index
            checkpoint_ms = checkpoints[checkpoint_index]
            checkpoint_samples = math.ceil(
                buffer.sample_rate_hz * checkpoint_ms / 1_000
            )
            if buffer.sample_count < checkpoint_samples:
                return

            terminal = checkpoint_index == len(checkpoints) - 1
            buffer.next_checkpoint_index += 1
            score_sample_count = (
                buffer.sample_count
                if explicit_checkpoints is None
                else checkpoint_samples
            )
            retain_boundary = bool(
                terminal and self._retains_terminal_boundary_pcm(candidate)
                and token.anchor_applied
                and token.finish_state is _FinishState.OPEN
            )
            if (
                retain_boundary
                and self._retained_pcm_bytes() + score_sample_count * 2
                > MAX_SPEAKER_SHADOW_RETAINED_PCM_BYTES
            ):
                # The original terminal PCM remains owned until its boundary.
                # Account for the scoring copy before allocating it; the
                # existing global budget separately leaves one host-copy slot.
                self._drop_candidate(candidate, token=token)
                return
            candidate_pcm = bytearray(buffer.pcm16[: score_sample_count * 2])
            if terminal and not retain_boundary:
                self._buffers.pop(candidate, None)
                self._wipe_bytearray(buffer.pcm16)
            try:
                would_block = await self._evaluate_candidate(
                    generation=generation,
                    candidate=candidate,
                    token=token,
                    pcm16=candidate_pcm,
                    sample_rate_hz=buffer.sample_rate_hz,
                    audio_ms=(
                        buffer.audio_ms
                        if explicit_checkpoints is None
                        else checkpoint_ms
                    ),
                    checkpoint_ms=(
                        checkpoint_ms if explicit_checkpoints is not None else None
                    ),
                    terminal=terminal,
                )
            except BaseException:
                retained_buffer = self._buffers.get(candidate)
                if retained_buffer is buffer:
                    self._buffers.pop(candidate, None)
                    self._wipe_bytearray(buffer.pcm16)
                raise
            finally:
                self._wipe_bytearray(candidate_pcm)
            if not self._identity_is_current(
                generation,
                candidate,
                token,
            ):
                if generation == self._generation and self._retained_terminal_source_is_current(candidate, token):
                    return
                retained_buffer = self._buffers.get(candidate)
                if retained_buffer is buffer:
                    self._buffers.pop(candidate, None)
                    self._wipe_bytearray(buffer.pcm16)
                return
            if (
                not terminal
                and candidate.scope in self._config.completion_confirmation_scopes
                and buffer.next_checkpoint_index < len(checkpoints)
            ):
                buffer.completion_confirmation_checkpoint_ms = (
                    checkpoint_ms if would_block else None
                )

    async def _evaluate_candidate(
        self,
        *,
        generation: int,
        candidate: SpeakerShadowCandidateKey,
        token: _CandidateToken,
        pcm16: bytearray,
        sample_rate_hz: int,
        audio_ms: int,
        checkpoint_ms: int | None,
        terminal: bool,
        observation_kind: Literal[
            "checkpoint", "completion_confirmation"
        ] = "checkpoint",
    ) -> bool | None:
        self._active_evaluation = (generation, candidate)
        self._active_evaluation_terminal = terminal
        self._active_pcm_bytes = len(pcm16)
        token.score_input_sample_count = len(pcm16) // 2
        token.score_outcome = "waiting_backend"
        try:
            backend_host = await self._ensure_backend()
            if not self._identity_is_current(generation, candidate, token):
                token.score_outcome = "stale_before_score"
                self._metrics.stale_result_count += 1
                return
            if backend_host is None:
                token.score_outcome = "backend_unavailable"
                self._mark_backend_degraded()
                self._publish_unavailable_observation(
                    generation=generation,
                    candidate=candidate,
                    token=token,
                    audio_ms=audio_ms,
                    checkpoint_ms=checkpoint_ms,
                    observation_kind=observation_kind,
                )
                self._finalize_candidate(candidate, "failed", token=token)
                return
            started = time.perf_counter()
            token.score_attempt_count += 1
            token.score_outcome = "in_progress"
            self._emit_diagnostic(token, "speaker_score_started", generation=generation)
            try:
                similarity = float(
                    await backend_host.score(
                        pcm16,
                        timeout_seconds=self._config.backend_score_timeout_seconds,
                    )
                )
                if not math.isfinite(similarity) or not -1.0 <= similarity <= 1.0:
                    raise ValueError("speaker cosine similarity must be within [-1, 1]")
                self._mark_backend_recovered()
            except asyncio.CancelledError:
                token.score_outcome = "cancelled"
                raise
            except _BackendHostTimeout:
                token.score_outcome = "timeout"
                self._metrics.backend_timeout_count += 1
                self._discard_backend_host(backend_host)
                self._metrics.inference_failure_count += 1
                self._mark_backend_degraded()
                if self._identity_is_current(generation, candidate, token):
                    self._publish_unavailable_observation(
                        generation=generation,
                        candidate=candidate,
                        token=token,
                        audio_ms=audio_ms,
                        checkpoint_ms=checkpoint_ms,
                        observation_kind=observation_kind,
                    )
                    self._finalize_candidate(candidate, "failed", token=token)
                return
            except Exception:
                token.score_outcome = "failed"
                if not backend_host.alive:
                    self._discard_backend_host(backend_host)
                self._metrics.inference_failure_count += 1
                self._mark_backend_degraded()
                if self._identity_is_current(generation, candidate, token):
                    self._publish_unavailable_observation(
                        generation=generation,
                        candidate=candidate,
                        token=token,
                        audio_ms=audio_ms,
                        checkpoint_ms=checkpoint_ms,
                        observation_kind=observation_kind,
                    )
                    self._finalize_candidate(candidate, "failed", token=token)
                return
            finally:
                self._metrics.inference_ms += int(
                    (time.perf_counter() - started) * 1_000
                )
            if not self._identity_is_current(generation, candidate, token):
                token.score_outcome = "stale_after_score"
                self._metrics.stale_result_count += 1
                return

            would_block = tuple(
                (threshold, similarity < threshold)
                for threshold in self._config.similarity_thresholds
            )
            blocked_at_any_threshold = any(blocked for _, blocked in would_block)
            token.score_outcome = "completed"
            token.last_checkpoint_ms = (
                checkpoint_ms
                if checkpoint_ms is not None
                else self._config.minimum_audio_ms
            )
            token.scored_sample_count = len(pcm16) // 2
            if terminal:
                self._finalize_candidate(candidate, "scored", token=token)
                self._metrics.evaluated_candidate_count += 1
            if blocked_at_any_threshold:
                self._metrics.would_block_count += 1
            for threshold, blocked in would_block:
                if blocked:
                    self._would_block_counts[threshold] += 1
            if not token.evidence_complete:
                return blocked_at_any_threshold
            sequence_no = token.evidence_sequence_no + 1
            token.evidence_sequence_no = sequence_no
            observation = SpeakerShadowObservation(
                candidate=candidate,
                similarity=similarity,
                would_block=would_block,
                audio_ms=audio_ms,
                checkpoint_ms=checkpoint_ms,
                observation_kind=observation_kind,
                sequence_no=sequence_no,
            )
            if not self._publish_evidence(observation, token=token):
                return blocked_at_any_threshold

            # The synchronous evidence sink above is the sole production
            # authority.  This async callback remains a bounded compatibility
            # seam; its timeout or cancellation cannot retract or reorder the
            # already-published fact.
            callback = self._on_observation
            if callback is None:
                return blocked_at_any_threshold
            existing_callback_task = self._observation_task
            if existing_callback_task is not None:
                if not existing_callback_task.done():
                    self._metrics.callback_failure_count += 1
                    return blocked_at_any_threshold
                self._consume_callback_result(existing_callback_task)
            callback_task = asyncio.create_task(
                callback(replace(observation, sequence_no=0)),
                name="speaker-shadow-observation",
            )
            self._observation_task = callback_task
            callback_task.add_done_callback(self._consume_callback_result)
            try:
                done, _ = await asyncio.wait(
                    {callback_task},
                    timeout=self._config.callback_timeout_seconds,
                )
            except asyncio.CancelledError:
                cancelled = await self._cancel_callback_bounded(callback_task)
                if not cancelled:
                    self._detach_callback(callback_task)
                if self._observation_task is callback_task:
                    self._observation_task = None
                raise
            if not done:
                self._metrics.callback_failure_count += 1
                cancelled = await self._cancel_callback_bounded(callback_task)
                if not cancelled:
                    self._detach_callback(callback_task)
                if self._observation_task is callback_task:
                    self._observation_task = None
                return blocked_at_any_threshold
            try:
                callback_task.result()
            except asyncio.CancelledError:
                self._metrics.stale_result_count += 1
            except Exception:
                self._metrics.callback_failure_count += 1
            else:
                if (
                    observation_kind == "checkpoint"
                    and checkpoint_ms is not None
                    and self._identity_is_current(
                        generation,
                        candidate,
                        token,
                    )
                ):
                    token.last_delivered_checkpoint_ms = checkpoint_ms
            return blocked_at_any_threshold
        except asyncio.CancelledError:
            if token.score_outcome == "waiting_backend":
                token.score_outcome = "cancelled_before_score"
            raise
        finally:
            self._emit_diagnostic(token, "speaker_score_finished", generation=generation)
            if self._active_evaluation == (generation, candidate):
                self._active_evaluation = None
                self._active_evaluation_terminal = False
                self._active_pcm_bytes = 0

    def _publish_evidence(
        self,
        event: SpeakerShadowObservation | SpeakerShadowCompletion,
        *,
        token: _CandidateToken,
    ) -> bool:
        """Publish one authoritative fact in the serial worker context."""

        callback = self._on_evidence
        if callback is None:
            return True
        try:
            callback(event)
        except Exception:
            self._metrics.callback_failure_count += 1
            token.evidence_complete = False
            return False
        return True

    def _publish_unavailable_observation(
        self,
        *,
        generation: int,
        candidate: SpeakerShadowCandidateKey,
        token: _CandidateToken,
        audio_ms: int,
        checkpoint_ms: int | None,
        observation_kind: Literal["checkpoint", "completion_confirmation"],
    ) -> None:
        """Publish one explicit ordered fail-open fact after score failure."""

        if (
            generation != self._generation
            or self._closed
            or not token.evidence_complete
        ):
            return
        sequence_no = token.evidence_sequence_no + 1
        token.evidence_sequence_no = sequence_no
        unavailable = SpeakerShadowObservation(
            candidate=candidate,
            similarity=0.0,
            would_block=(),
            audio_ms=audio_ms,
            checkpoint_ms=checkpoint_ms,
            observation_kind=observation_kind,
            sequence_no=sequence_no,
            evidence_available=False,
        )
        self._publish_evidence(unavailable, token=token)

    async def _ensure_backend(self) -> _BackendProcessHost | None:
        existing_host = self._backend_host
        if existing_host is not None and existing_host.alive and existing_host.loaded:
            return existing_host
        if existing_host is not None:
            self._discard_backend_host(existing_host)
        if time.monotonic() < self._next_load_attempt_at:
            self._metrics.load_retry_suppressed_count += 1
            return None
        factory = self._backend_factory
        if factory is None:
            return None

        started = time.perf_counter()
        start_task = asyncio.create_task(
            asyncio.to_thread(
                _BackendProcessHost.create_started,
                factory=factory,
                terminate_timeout_seconds=(
                    self._config.process_terminate_timeout_seconds
                ),
            ),
            name="speaker-shadow-host-start",
        )
        self._host_start_task = start_task
        host: _BackendProcessHost | None = None
        try:
            try:
                host = await asyncio.shield(start_task)
            except asyncio.CancelledError:
                # ``to_thread`` cannot stop an in-progress ``Process.start``.
                # Keep ownership across repeated worker cancellations so a host
                # that finishes starting after shutdown is always retrieved and
                # terminated by the outer cancellation handler.
                while not start_task.done():
                    try:
                        await asyncio.shield(start_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if start_task.done() and not start_task.cancelled():
                    try:
                        host = start_task.result()
                    except Exception:
                        host = None
                raise
            available = await host.load(
                timeout_seconds=self._config.backend_load_timeout_seconds
            )
            if not available:
                await self._close_host(host)
                self._record_load_failure()
                return None
        except asyncio.CancelledError:
            if host is not None:
                await self._terminate_host(host)
            raise
        except _BackendHostTimeout:
            self._metrics.backend_timeout_count += 1
            if host is not None:
                self._record_host_termination(host)
            self._record_load_failure()
            return None
        except Exception:
            if host is not None:
                await self._close_host(host)
            self._record_load_failure()
            return None
        finally:
            if self._host_start_task is start_task:
                self._host_start_task = None
            self._metrics.load_ms += int((time.perf_counter() - started) * 1_000)
        assert host is not None
        self._backend_host = host
        self._load_failure_streak = 0
        self._next_load_attempt_at = 0.0
        self._metrics.load_count += 1
        self._mark_backend_recovered()
        return host

    def _record_load_failure(self) -> None:
        self._load_failure_streak += 1
        retry_seconds = self._config.load_retry_initial_seconds
        for _ in range(self._load_failure_streak - 1):
            if retry_seconds >= self._config.load_retry_max_seconds:
                break
            retry_seconds = min(
                self._config.load_retry_max_seconds,
                retry_seconds * 2,
            )
        self._next_load_attempt_at = time.monotonic() + retry_seconds
        self._metrics.load_failure_count += 1
        self._mark_backend_degraded()

    def _mark_backend_degraded(self) -> None:
        self._set_degraded_cause("backend_unavailable")

    def _mark_backend_recovered(self) -> None:
        self._clear_degraded_cause("backend_unavailable")

    def _set_degraded_cause(self, cause: _DegradedCause) -> None:
        if self._closed or cause in self._degraded_causes:
            return
        notify = not self._degraded_causes
        self._degraded_causes.add(cause)
        self._publish_health_snapshot()
        if not notify:
            return
        self._metrics.delivery_degraded_count += 1
        callback = self._on_backend_degraded
        if callback is None:
            return
        try:
            callback()
        except Exception:
            self._metrics.callback_failure_count += 1

    def _clear_degraded_cause(self, cause: _DegradedCause) -> None:
        if cause not in self._degraded_causes:
            return
        self._degraded_causes.discard(cause)
        self._publish_health_snapshot()
        if self._degraded_causes:
            return
        if self._closed:
            return
        callback = self._on_backend_recovered
        if callback is None:
            return
        try:
            callback()
        except Exception:
            self._metrics.callback_failure_count += 1

    def _publish_health_snapshot(self) -> None:
        if self._closed:
            return
        self._health_revision += 1
        callback = self._on_health_changed
        if callback is not None:
            try:
                callback(self._health_revision, frozenset(self._degraded_causes))
            except Exception:
                self._metrics.callback_failure_count += 1

    async def _unload_backend(self) -> bool:
        host = self._backend_host
        if host is None:
            return True
        closed = await self._close_host(host)
        if self._backend_host is host:
            self._backend_host = None
        if closed:
            self._metrics.unload_count += 1
        else:
            self._metrics.unload_failure_count += 1
        self._load_failure_streak = 0
        self._next_load_attempt_at = 0.0
        return closed

    async def _close_host(self, host: _BackendProcessHost) -> bool:
        try:
            closed = await host.close(
                timeout_seconds=self._config.backend_close_timeout_seconds
            )
        except Exception:
            await self._terminate_host(host)
            return False
        if host.timed_out:
            self._metrics.backend_timeout_count += 1
        self._record_host_termination(host)
        return closed

    async def _terminate_host(self, host: _BackendProcessHost) -> None:
        try:
            await host.terminate()
        except Exception:
            self._metrics.unload_failure_count += 1
        self._record_host_termination(host)

    def _discard_backend_host(self, host: _BackendProcessHost) -> None:
        if self._backend_host is host:
            self._backend_host = None
        self._record_host_termination(host)

    def _record_host_termination(self, host: _BackendProcessHost) -> None:
        if host.was_terminated:
            self._metrics.backend_process_termination_count += 1
            host.was_terminated = False

    async def _cleanup_after_worker(
        self,
        worker: asyncio.Task[None] | None,
    ) -> None:
        try:
            if worker is not None and not worker.done():
                done, _ = await asyncio.wait(
                    {worker},
                    timeout=self._config.shutdown_grace_seconds,
                )
                if not done:
                    self._metrics.shutdown_timeout_count += 1
                    cancellation_timeout = (
                        self._config.process_terminate_timeout_seconds * 2
                        + _HOST_POLL_INTERVAL_SECONDS
                    )
                    for attempt in range(2):
                        worker.cancel()
                        done, _ = await asyncio.wait(
                            {worker},
                            timeout=cancellation_timeout,
                        )
                        if done:
                            break
                        if attempt == 0:
                            host, self._backend_host = self._backend_host, None
                            if host is not None:
                                await self._terminate_host(host)
                    if not worker.done():
                        # A thread already inside ``Process.start`` cannot be
                        # cancelled. Keep cleanup attached until the worker
                        # retrieves and terminates any host it eventually
                        # returns; close must not leave that ownership orphaned.
                        await asyncio.wait({worker})
            if worker is not None and worker.done():
                self._consume_worker_result(worker)
            observation = self._observation_task
            if observation is not None:
                cancelled = await self._cancel_callback_bounded(observation)
                if not cancelled:
                    self._detach_callback(observation)
                if self._observation_task is observation:
                    self._observation_task = None
            await self._cancel_completion_dispatcher_bounded()
            await self._cancel_detached_callbacks_bounded()
        finally:
            try:
                await self._unload_backend()
            finally:
                self._close_parent_factory()

    def _close_parent_factory(self) -> None:
        """Release the parent-owned profile exactly once without exposing it."""

        if self._factory_closed:
            return
        self._factory_closed = True
        close_factory = getattr(self._backend_factory, "close", None)
        if not callable(close_factory):
            return
        try:
            # Factory.close is a parent-memory wipe contract. It must be
            # idempotent and non-blocking; running a copy elsewhere would not
            # clear the parent-owned profile or embedding.
            close_factory()
        except Exception:
            self._metrics.unload_failure_count += 1

    async def _process_finish(self, marker: _CandidateFinished) -> None:
        if marker.generation != self._generation:
            self._abandon_terminal(marker.candidate, token=marker.token)
            return
        if marker.token.finish_state is _FinishState.ABANDONED:
            self._abandon_completion(marker.token)
            return
        if marker.token.finish_state is _FinishState.PROCESSED:
            return
        if (
            marker.token.finish_state is not _FinishState.QUEUED
            and self._candidate_was_evicted(
                marker.candidate,
                token=marker.token,
            )
        ):
            return
        pending = self._pending_backend_candidates.get(marker.candidate)
        if pending is not None and pending.token is marker.token:
            pending.finish = marker
            return
        # Only an accepted QUEUED marker outranks the tombstone watermark.
        finishing_buffer = self._buffers.get(marker.candidate)
        if finishing_buffer is not None and finishing_buffer.token is marker.token:
            marker.token.finish_sample_count = finishing_buffer.sample_count
        self._mark_finish_processed(marker.token)
        if marker.token.terminal_reason is not None:
            buffer = self._buffers.get(marker.candidate)
            if buffer is not None and buffer.token is marker.token:
                self._buffers.pop(marker.candidate, None)
                self._wipe_bytearray(buffer.pcm16)
                self._schedule_terminal_pcm_expiry()
            self._record_token_finish(marker.token)
            self._enqueue_completion(
                marker,
                terminal_reason=marker.token.terminal_reason,
            )
            return
        finalized = self._finalized.get(marker.candidate)
        if finalized is not None:
            self._record_finish(marker.candidate, finalized)
            completion_token = finalized.token or marker.token
            self._enqueue_completion(
                _CandidateFinished(
                    marker.generation,
                    marker.candidate,
                    completion_token,
                ),
                terminal_reason=finalized.terminal_reason,
            )
            return
        buffer = self._buffers.get(marker.candidate)
        explicit_checkpoints = self._config.observation_checkpoints_ms
        confirmation_checkpoint_ms = (
            buffer.completion_confirmation_checkpoint_ms if buffer is not None else None
        )
        should_confirm = (
            buffer is not None
            and buffer.token is marker.token
            and self._identity_is_current(
                marker.generation,
                marker.candidate,
                marker.token,
            )
            and marker.candidate.scope in self._config.completion_confirmation_scopes
            and explicit_checkpoints is not None
            and confirmation_checkpoint_ms is not None
            and 0 < buffer.next_checkpoint_index < len(explicit_checkpoints)
            and explicit_checkpoints[buffer.next_checkpoint_index - 1]
            == confirmation_checkpoint_ms
            and buffer.audio_ms > confirmation_checkpoint_ms
            and buffer.audio_ms < explicit_checkpoints[buffer.next_checkpoint_index]
        )
        if should_confirm:
            assert buffer is not None
            assert confirmation_checkpoint_ms is not None
            candidate_pcm = bytearray(buffer.pcm16)
            audio_ms = buffer.audio_ms
            sample_rate_hz = buffer.sample_rate_hz
            try:
                try:
                    await self._evaluate_candidate(
                        generation=marker.generation,
                        candidate=marker.candidate,
                        token=marker.token,
                        pcm16=candidate_pcm,
                        sample_rate_hz=sample_rate_hz,
                        audio_ms=audio_ms,
                        checkpoint_ms=confirmation_checkpoint_ms,
                        terminal=True,
                        observation_kind="completion_confirmation",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._identity_is_current(
                        marker.generation,
                        marker.candidate,
                        marker.token,
                    ):
                        self._finalize_candidate(
                            marker.candidate,
                            "failed",
                            token=marker.token,
                        )
            finally:
                self._wipe_bytearray(candidate_pcm)
                retained_buffer = self._buffers.get(marker.candidate)
                if retained_buffer is buffer:
                    self._buffers.pop(marker.candidate, None)
                self._wipe_bytearray(buffer.pcm16)

            if marker.generation != self._generation or self._closed:
                self._abandon_completion(marker.token)
                return
            terminal_reason = marker.token.terminal_reason
            if terminal_reason is None:
                self._finalize_candidate(
                    marker.candidate,
                    "failed",
                    token=marker.token,
                )
                terminal_reason = marker.token.terminal_reason or "failed"
            self._record_token_finish(marker.token)
            self._enqueue_completion(
                marker,
                terminal_reason=terminal_reason,
            )
            return

        buffer = self._buffers.pop(marker.candidate, None)
        terminal_reason: SpeakerShadowTerminalReason = "insufficient"
        if buffer is not None:
            if buffer.next_checkpoint_index > 0:
                terminal_reason = "scored"
                self._metrics.evaluated_candidate_count += 1
            self._wipe_bytearray(buffer.pcm16)
        self._finalize_candidate(
            marker.candidate,
            terminal_reason,
            token=marker.token,
        )
        self._record_token_finish(marker.token)
        self._enqueue_completion(
            marker,
            terminal_reason=terminal_reason,
        )

    def _recover_failed_finish(self, marker: _CandidateFinished) -> None:
        """Convert a consumed finish fault into one explicit failed completion."""

        if marker.generation != self._generation or self._closed:
            self._abandon_completion(marker.token)
            return
        self._mark_finish_processed(marker.token)
        if marker.token.terminal_reason is None:
            self._finalize_candidate(
                marker.candidate,
                "failed",
                token=marker.token,
            )
        terminal_reason = marker.token.terminal_reason or "failed"
        self._record_token_finish(marker.token)
        self._enqueue_completion(marker, terminal_reason=terminal_reason)

    def _enqueue_completion(
        self,
        marker: _CandidateFinished,
        *,
        terminal_reason: SpeakerShadowTerminalReason,
    ) -> bool:
        """Accept one terminal notice into the bounded ordered outbox."""

        token = marker.token
        if marker.generation != self._generation or self._closed:
            self._abandon_completion(token)
            return False
        if token.completion_state in {
            _CompletionState.QUEUED,
            _CompletionState.DISPATCHED,
            _CompletionState.ATTEMPTED,
        }:
            return True
        if token.completion_state is _CompletionState.ABANDONED:
            return False
        completion = SpeakerShadowCompletion(
            candidate=marker.candidate,
            terminal_reason=terminal_reason,
            last_checkpoint_ms=token.last_checkpoint_ms,
            through_sequence_no=token.evidence_sequence_no,
            evidence_complete=token.evidence_complete,
        )
        if not token.evidence_closed:
            token.evidence_closed = True
            self._emit_diagnostic(token, "speaker_capture_closed")
            self._publish_evidence(completion, token=token)
        legacy_completion = SpeakerShadowCompletion(
            candidate=marker.candidate,
            terminal_reason=terminal_reason,
            last_checkpoint_ms=token.last_checkpoint_ms,
        )
        if self._completion_queue.qsize() >= self._config.completion_queue_capacity:
            self._metrics.completion_overflow_count += 1
            self._set_degraded_cause("completion_overflow")
            self._abandon_completion(token)
            return False
        if not self._ensure_completion_dispatcher():
            self._set_degraded_cause("dispatcher_start_failure")
            self._abandon_completion(token)
            return False
        envelope = _CompletionEnvelope(
            generation=marker.generation,
            candidate=marker.candidate,
            token=token,
            completion=legacy_completion,
        )
        try:
            self._completion_queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self._metrics.completion_overflow_count += 1
            self._set_degraded_cause("completion_overflow")
            self._abandon_completion(token)
            return False
        token.completion_state = _CompletionState.QUEUED
        self._metrics.completion_count += 1
        if token.last_checkpoint_ms is None:
            self._metrics.completion_before_first_checkpoint_count += 1
        else:
            self._metrics.completion_after_first_checkpoint_count += 1
        return True

    def _ensure_completion_dispatcher(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._owner_loop is None:
            self._owner_loop = loop
        elif self._owner_loop is not loop:
            return False
        dispatcher = self._completion_dispatcher_task
        if dispatcher is not None and not dispatcher.done():
            return True
        dispatcher = loop.create_task(
            self._run_completion_dispatcher(),
            name="speaker-shadow-completion-dispatcher",
        )
        dispatcher.add_done_callback(self._consume_completion_dispatcher_result)
        self._completion_dispatcher_task = dispatcher
        self._clear_degraded_cause("dispatcher_start_failure")
        return True

    async def _run_completion_dispatcher(self) -> None:
        while True:
            item = await self._completion_queue.get()
            try:
                if item is _COMPLETION_STOP:
                    return
                assert isinstance(item, _CompletionEnvelope)
                self._completion_dispatch_in_progress = True
                try:
                    await self._dispatch_completion(item)
                except asyncio.CancelledError:
                    if item.token.completion_state is _CompletionState.QUEUED:
                        self._abandon_completion(item.token)
                    raise
                except Exception:
                    self._set_degraded_cause("dispatcher_start_failure")
                    self._abandon_completion(item.token)
            finally:
                self._completion_dispatch_in_progress = False
                self._completion_queue.task_done()
            if self._completion_queue.empty():
                self._clear_degraded_cause("completion_overflow")
                if (
                    self._completion_callback_task is None
                    or self._completion_callback_task.done()
                ):
                    self._clear_degraded_cause("completion_stalled")

    async def _dispatch_completion(self, envelope: _CompletionEnvelope) -> None:
        token = envelope.token
        if (
            envelope.generation != self._generation
            or self._closed
            or token.completion_state is not _CompletionState.QUEUED
        ):
            self._abandon_completion(token)
            return

        await self._wait_for_detached_completion_callbacks()
        if (
            envelope.generation != self._generation
            or self._closed
            or token.completion_state is not _CompletionState.QUEUED
        ):
            self._abandon_completion(token)
            return

        callback = self._on_completion
        if callback is None:
            token.completion_state = _CompletionState.ATTEMPTED
            self._metrics.completion_attempted_count += 1
            return
        try:
            callback_task = asyncio.create_task(
                callback(envelope.completion),
                name="speaker-shadow-completion",
            )
        except Exception:
            self._set_degraded_cause("dispatcher_start_failure")
            raise
        self._completion_callback_task = callback_task
        self._completion_callback_token = token
        token.completion_state = _CompletionState.DISPATCHED
        self._metrics.completion_dispatched_count += 1
        callback_task.add_done_callback(self._consume_callback_result)
        try:
            done, _ = await asyncio.wait(
                {callback_task},
                timeout=self._config.callback_timeout_seconds,
            )
            if not done:
                self._metrics.callback_failure_count += 1
                self._metrics.completion_callback_failure_count += 1
                self._metrics.completion_stall_count += 1
                self._set_degraded_cause("completion_stalled")
                cancelled = await self._cancel_callback_bounded(callback_task)
                if not cancelled:
                    try:
                        await asyncio.shield(callback_task)
                    except asyncio.CancelledError:
                        self._detach_callback(
                            callback_task,
                            completion_token=token,
                        )
                        raise
            self._consume_completion_callback_result(callback_task)
        except asyncio.CancelledError:
            cancelled = await self._cancel_callback_bounded(callback_task)
            if not cancelled:
                self._detach_callback(
                    callback_task,
                    completion_token=token,
                )
            else:
                self._consume_completion_callback_result(callback_task)
            raise
        finally:
            if (
                token.completion_state is _CompletionState.DISPATCHED
                and callback_task.done()
            ):
                token.completion_state = _CompletionState.ATTEMPTED
                self._metrics.completion_attempted_count += 1
            if self._completion_callback_task is callback_task and callback_task.done():
                self._completion_callback_task = None
                self._completion_callback_token = None

    def _consume_completion_callback_result(
        self,
        callback_task: asyncio.Task[None],
    ) -> None:
        try:
            callback_task.result()
        except asyncio.CancelledError:
            self._metrics.stale_result_count += 1
        except Exception:
            self._metrics.callback_failure_count += 1
            self._metrics.completion_callback_failure_count += 1

    def _abandon_completion(self, token: _CandidateToken) -> None:
        if token.completion_state in {
            _CompletionState.DISPATCHED,
            _CompletionState.ATTEMPTED,
            _CompletionState.ABANDONED,
        }:
            return
        token.completion_state = _CompletionState.ABANDONED
        self._metrics.completion_abandoned_count += 1

    def _detach_callback(
        self,
        task: asyncio.Task[None],
        *,
        completion_token: _CandidateToken | None = None,
    ) -> None:
        if completion_token is not None:
            if "completion_stalled" not in self._degraded_causes:
                self._metrics.completion_stall_count += 1
                self._set_degraded_cause("completion_stalled")
            self._detached_completion_tokens[task] = completion_token
            if self._completion_callback_task is task:
                self._completion_callback_task = None
                self._completion_callback_token = None
        if self._observation_task is task:
            self._observation_task = None
        if task in self._detached_callback_tasks:
            return
        self._detached_callback_tasks.add(task)
        task.add_done_callback(self._consume_detached_callback_result)

    async def _wait_for_detached_completion_callbacks(self) -> None:
        while self._detached_completion_tokens:
            tasks = tuple(self._detached_completion_tokens)
            done, _ = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                self._consume_detached_callback_result(task)

    def _drop_candidate(
        self,
        candidate: SpeakerShadowCandidateKey,
        *,
        token: _CandidateToken | None = None,
    ) -> None:
        buffer = self._buffers.pop(candidate, None)
        if buffer is not None:
            self._metrics.dropped_audio_ms += buffer.audio_ms
            self._wipe_bytearray(buffer.pcm16)
            if token is None:
                token = buffer.token
            if buffer.exact_boundary_deadline is not None:
                self._schedule_terminal_pcm_expiry()
        self._finalize_candidate(
            candidate,
            "dropped",
            token=token,
        )

    def _finalize_candidate(
        self,
        candidate: SpeakerShadowCandidateKey,
        terminal_reason: SpeakerShadowTerminalReason,
        *,
        token: _CandidateToken | None = None,
    ) -> None:
        if token is None:
            token = self._candidate_tokens.get(candidate)
        if token is not None and token.terminal_reason is not None:
            return
        if token is not None:
            token.terminal_reason = terminal_reason
            self._emit_diagnostic(token, "speaker_candidate_terminal")
            if self._candidate_tokens.get(candidate) is token:
                self._candidate_tokens.pop(candidate, None)
        previous = self._finalized.pop(candidate, None)
        if previous is not None:
            self._finalized[candidate] = _FinalizedCandidate(
                finish_state=(
                    _FinishState.PROCESSED
                    if previous.finish_seen
                    else token.finish_state
                    if token is not None
                    else previous.finish_state
                ),
                terminal_reason=previous.terminal_reason,
                token=previous.token or token,
            )
            return
        self._finalized[candidate] = _FinalizedCandidate(
            finish_state=(
                token.finish_state if token is not None else _FinishState.OPEN
            ),
            terminal_reason=terminal_reason,
            token=token,
        )
        buffer = self._buffers.get(candidate)
        if (
            terminal_reason == "scored" and token is not None
            and token.finish_state is _FinishState.OPEN
            and token.anchor_applied
            and self._retains_terminal_boundary_pcm(candidate)
            and buffer is not None and buffer.token is token
            and buffer.exact_boundary_deadline is None
        ):
            buffer.exact_boundary_deadline = time.monotonic() + self._config.exact_boundary_pcm_retention_seconds
            self._schedule_terminal_pcm_expiry()
        counter_name = f"{terminal_reason}_candidate_count"
        setattr(
            self._metrics,
            counter_name,
            getattr(self._metrics, counter_name) + 1,
        )
        while len(self._finalized) > self._config.finalized_candidate_capacity:
            evicted_candidate = next(
                (key for key in self._finalized if not self._candidate_pcm_is_reserved(key)), None,
            )
            if evicted_candidate is None:
                # Prepare reserves room for in-flight terminal results as well
                # as existing tombstones. Never destroy a source if metadata
                # outside that contract nevertheless exhausts all victims.
                break
            self._finalized.pop(evicted_candidate, None)
            evicted_buffer = self._buffers.pop(evicted_candidate, None)
            if evicted_buffer is not None:
                self._wipe_bytearray(evicted_buffer.pcm16)
            self._record_evicted_candidate(evicted_candidate)

    def _emit_diagnostic(
        self, token: _CandidateToken, stage: str, *, generation: int | None = None,
    ) -> None:
        callback = self._on_diagnostic
        if callback is None:
            return
        try:
            buffer = self._buffers.get(token.candidate)
            callback(SpeakerShadowDiagnostic(
                candidate=token.candidate,
                stage=stage,
                worker_generation=self._generation if generation is None else generation,
                sample_rate_hz=token.sample_rate_hz,
                accepted_sample_count=token.accepted_sample_count,
                buffered_sample_count=(
                    buffer.sample_count if buffer is not None and buffer.token is token else None
                ),
                finish_sample_count=token.finish_sample_count,
                minimum_sample_count=(
                    (self._config.observation_checkpoints_ms or (self._config.minimum_audio_ms,))[0]
                    * token.sample_rate_hz // 1000
                    if token.sample_rate_hz > 0 else None
                ),
                score_attempt_count=token.score_attempt_count,
                score_input_sample_count=token.score_input_sample_count,
                score_outcome=token.score_outcome,
                scored_sample_count=token.scored_sample_count,
                last_checkpoint_ms=token.last_checkpoint_ms,
                terminal_reason=token.terminal_reason,
                evidence_sequence_no=token.evidence_sequence_no,
                anchor_applied=token.anchor_applied,
                anchor_discard_prefix_sample_count=token.anchor_discard_prefix_sample_count,
                scoring_deferred=token.scoring_deferred,
            ))
        except Exception:
            # Telemetry has no effect on evidence integrity or lifecycle.
            pass

    def _candidate_was_evicted(
        self,
        candidate: SpeakerShadowCandidateKey,
        *,
        token: _CandidateToken | None = None,
    ) -> bool:
        current_token = self._candidate_tokens.get(candidate)
        buffer = self._buffers.get(candidate)
        finalized = self._finalized.get(candidate)
        if token is None and (
            current_token is not None
            or buffer is not None
            or self._active_evaluation == (self._generation, candidate)
        ):
            return False
        if token is not None and (
            current_token is token
            or (buffer is not None and buffer.token is token)
            or (finalized is not None and finalized.token is token)
        ):
            return False
        finalized_through = self._finalized_through.get(candidate.scope)
        if finalized_through is None:
            return False
        return (
            candidate.detector_epoch,
            candidate.shadow_generation,
        ) <= finalized_through

    def _record_evicted_candidate(
        self,
        candidate: SpeakerShadowCandidateKey,
    ) -> None:
        position = (candidate.detector_epoch, candidate.shadow_generation)
        previous = self._finalized_through.get(candidate.scope)
        if previous is None or position > previous:
            self._finalized_through[candidate.scope] = position

    def _retire_finalized_candidates(self) -> None:
        for candidate in self._finalized:
            self._record_evicted_candidate(candidate)
        self._finalized.clear()

    @staticmethod
    def _threshold_metric_key(threshold: float) -> str:
        # ``repr`` is the shortest round-trippable float representation, so
        # distinct configured thresholds cannot collapse into one metric key.
        suffix = repr(threshold).replace("-", "m").replace("+", "p").replace(".", "_")
        return f"would_block_at_{suffix}_count"

    def _record_finish(
        self,
        candidate: SpeakerShadowCandidateKey,
        finalized: _FinalizedCandidate,
    ) -> None:
        if finalized.finish_seen:
            return
        if finalized.token is not None:
            finalized.token.finish_state = _FinishState.PROCESSED
        self._finalized.pop(candidate, None)
        self._finalized[candidate] = _FinalizedCandidate(
            finish_state=_FinishState.PROCESSED,
            terminal_reason=finalized.terminal_reason,
            token=finalized.token,
        )

    def _mark_finish_processed(self, token: _CandidateToken) -> None:
        if token.finish_state is _FinishState.PROCESSED:
            return
        if token.finish_state is _FinishState.ABANDONED:
            return
        token.finish_state = _FinishState.PROCESSED
        self._metrics.finished_candidate_count += 1

    def _record_token_finish(self, token: _CandidateToken) -> None:
        self._mark_finish_processed(token)
        if self._candidate_tokens.get(token.candidate) is token:
            self._candidate_tokens.pop(token.candidate, None)
        finalized = self._finalized.get(token.candidate)
        if finalized is not None:
            self._finalized.pop(token.candidate, None)
            self._finalized[token.candidate] = _FinalizedCandidate(
                finish_state=_FinishState.PROCESSED,
                terminal_reason=finalized.terminal_reason,
                token=finalized.token or token,
            )

    def _identity_is_current(
        self,
        generation: int,
        candidate: SpeakerShadowCandidateKey,
        token: _CandidateToken,
    ) -> bool:
        return (
            generation == self._generation
            and not self._closed
            and not self._resetting
            and candidate not in self._finalized
            and token.terminal_reason is None
            and self._candidate_tokens.get(candidate) is token
        )

    def _owned_candidate_tokens(self) -> tuple[_CandidateToken, ...]:
        tokens: list[_CandidateToken] = []
        seen: set[int] = set()

        def append(token: _CandidateToken | None) -> None:
            if token is None or id(token) in seen:
                return
            seen.add(id(token))
            tokens.append(token)

        for token in self._candidate_tokens.values():
            append(token)
        for buffer in self._buffers.values():
            append(buffer.token)
        for finalized in self._finalized.values():
            append(finalized.token)
        append(self._active_terminal_token)
        append(self._completion_callback_token)
        for token in self._detached_completion_tokens.values():
            append(token)
        return tuple(tokens)

    def _sweep_reset_tokens(self, tokens: list[_CandidateToken]) -> None:
        seen: set[int] = set()
        for token in tokens:
            if id(token) in seen:
                continue
            seen.add(id(token))
            if token.finish_state is _FinishState.QUEUED:
                self._abandon_terminal(token.candidate, token=token)
            elif token.finish_state in {
                _FinishState.PROCESSED,
                _FinishState.ABANDONED,
            }:
                self._abandon_completion(token)

    def _drain_queue(
        self,
        *,
        abandon_data_candidates: bool = False,
        revoke_pending_batches: bool = False,
    ) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                if isinstance(item, _AudioFrame):
                    self._queued_pcm_bytes = max(
                        0,
                        self._queued_pcm_bytes - len(item.pcm16),
                    )
                    self._wipe_bytearray(item.pcm16)
                    if abandon_data_candidates:
                        self._drop_candidate(item.candidate, token=item.token)
                elif isinstance(
                    item,
                    (_CandidateDeferred, _CandidateActivated, _CandidateAnchored),
                ):
                    if abandon_data_candidates:
                        if isinstance(item, _CandidateAnchored):
                            self._fail_deferred_anchor(item)
                        else:
                            self._drop_candidate(item.candidate, token=item.token)
                elif isinstance(item, _CandidatePrefixReconciliation):
                    if abandon_data_candidates:
                        self._fail_candidate_reconciliation(item)
                elif isinstance(item, _CandidateBatchReconciliation):
                    if revoke_pending_batches:
                        self._revoke_candidate_batch_reconciliation(item)
                    elif abandon_data_candidates:
                        self._fail_candidate_batch_reconciliation(item)
                elif isinstance(item, _CandidateTerminalCoverage):
                    if revoke_pending_batches:
                        self._revoke_terminal_coverage_record(item)
                    elif abandon_data_candidates:
                        self._fail_terminal_coverage(item)
                elif isinstance(item, _CandidateFinished):
                    self._abandon_terminal(item.candidate, token=item.token)
                self._retire_queued_item(item)
                self._queue.task_done()

    def _drain_completion_queue(self) -> None:
        while True:
            try:
                item = self._completion_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, _CompletionEnvelope):
                self._abandon_completion(item.token)
            self._completion_queue.task_done()
        self._clear_degraded_cause("completion_overflow")

    def _retained_pcm_bytes(self) -> int:
        host_pcm_bytes = (
            self._backend_host.pcm_bytes_in_use if self._backend_host is not None else 0
        )
        return (
            self._queued_pcm_bytes
            + sum(len(buffer.pcm16) for buffer in self._buffers.values())
            + sum(
                len(record.suffix_scratch_pcm16)
                for record in self._prepared_exact_intervals.values()
            )
            + sum(
                len(record.suffix_scratch_pcm16)
                for record in self._prepared_terminal_coverages.values()
            )
            + self._active_pcm_bytes
            + host_pcm_bytes
        )

    def _clear_buffers(self) -> None:
        if self._terminal_pcm_expiry_handle is not None:
            self._terminal_pcm_expiry_handle.cancel()
            self._terminal_pcm_expiry_handle = None
        for buffer in self._buffers.values():
            self._wipe_bytearray(buffer.pcm16)
        self._buffers.clear()

    @staticmethod
    def _wipe_bytearray(value: bytearray) -> None:
        value[:] = b"\x00" * len(value)

    def _cancel_observation_callback(self) -> None:
        callback_task = self._observation_task
        if callback_task is not None and not callback_task.done():
            callback_task.cancel()

    async def _cancel_callback_bounded(
        self,
        task: asyncio.Task[None],
    ) -> bool:
        callback_task = task
        for _ in range(2):
            if callback_task.done():
                self._consume_callback_result(callback_task)
                return True
            callback_task.cancel()
            done, _ = await asyncio.wait(
                {callback_task},
                timeout=self._config.callback_timeout_seconds,
            )
            if done:
                self._consume_callback_result(callback_task)
                return True
        return False

    async def _cancel_completion_dispatcher_bounded(self) -> bool:
        dispatcher = self._completion_dispatcher_task
        if dispatcher is None:
            callback = self._completion_callback_task
            if callback is None:
                return True
            token = self._completion_callback_token
            cancelled = await self._cancel_callback_bounded(callback)
            if not cancelled:
                self._detach_callback(
                    callback,
                    completion_token=token,
                )
            elif token is not None:
                self._consume_completion_callback_result(callback)
                if token.completion_state is _CompletionState.DISPATCHED:
                    token.completion_state = _CompletionState.ATTEMPTED
                    self._metrics.completion_attempted_count += 1
            if self._completion_callback_task is callback:
                self._completion_callback_task = None
                self._completion_callback_token = None
            return cancelled
        if not dispatcher.done():
            dispatcher.cancel()
            timeout = max(
                self._config.callback_timeout_seconds * 3,
                _HOST_POLL_INTERVAL_SECONDS,
            )
            done, _ = await asyncio.wait({dispatcher}, timeout=timeout)
            if not done:
                dispatcher.cancel()
                done, _ = await asyncio.wait({dispatcher}, timeout=timeout)
            if not done:
                return False
        self._consume_completion_dispatcher_result(dispatcher)
        return True

    async def _cancel_detached_callbacks_bounded(self) -> bool:
        detached_tasks = tuple(self._detached_callback_tasks)
        if not detached_tasks:
            return True
        results = await asyncio.gather(
            *(self._cancel_callback_bounded(task) for task in detached_tasks)
        )
        for task in detached_tasks:
            if task.done():
                self._consume_detached_callback_result(task)
        return all(results)

    def _consume_callback_result(self, task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        finally:
            if self._observation_task is task and task.done():
                self._observation_task = None

    def _consume_detached_callback_result(self, task: asyncio.Task[None]) -> None:
        self._detached_callback_tasks.discard(task)
        token = self._detached_completion_tokens.pop(task, None)
        if token is None:
            return
        self._consume_completion_callback_result(task)
        if token.completion_state is _CompletionState.DISPATCHED:
            token.completion_state = _CompletionState.ATTEMPTED
            self._metrics.completion_attempted_count += 1
        if (
            self._completion_callback_task is None
            and not self._detached_completion_tokens
            and self._completion_queue.empty()
        ):
            self._clear_degraded_cause("completion_stalled")

    def _consume_completion_dispatcher_result(
        self,
        task: asyncio.Task[None],
    ) -> None:
        abnormal = False
        try:
            abnormal = task.exception() is not None
        except asyncio.CancelledError:
            pass
        if self._completion_dispatcher_task is task and task.done():
            self._completion_dispatcher_task = None
        if abnormal and not self._closed:
            self._set_degraded_cause("dispatcher_start_failure")
            self._drain_completion_queue()

    def _consume_worker_result(self, task: asyncio.Task[None]) -> None:
        abnormal = False
        try:
            abnormal = task.exception() is not None
        except asyncio.CancelledError:
            abnormal = True
        if self._worker_task is task and task.done():
            self._worker_task = None
        if self._closed or self._resetting:
            return
        if abnormal:
            self._metrics.worker_start_failure_count += 1
            self._set_degraded_cause("worker_start_failure")
            self._drain_queue(abandon_data_candidates=True)
            return
        if not self._queue.empty() and not self._ensure_worker():
            self._metrics.worker_start_failure_count += 1
            self._set_degraded_cause("worker_start_failure")
            self._drain_queue(abandon_data_candidates=True)

    @staticmethod
    def _audio_ms(sample_count: int, sample_rate_hz: int) -> int:
        return max(1, sample_count * 1_000 // sample_rate_hz)

    @staticmethod
    def _consume_cleanup_result(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            return

    @staticmethod
    def _consume_reset_result(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            return
