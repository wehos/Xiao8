"""Application wiring for the single Owner profile across character runtimes."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
import logging
import math
import os
from pathlib import Path
import uuid
import weakref

from main_logic.asr_client import VoiceIdentityActivationResult
from main_logic.asr_client.speaker_verifier_contracts import (
    SpeakerVerifierAuthority,
    SpeakerVerifierInstallOutcome,
    SpeakerVerifierInstallReceipt,
    SpeakerVerifierSpec,
)
from main_logic.asr_client.speaker_shadow.campplus import CampPlusEmbeddingModel
from main_logic.voice_identity.profile import SpeakerProfile
from main_logic.voice_identity_service.asr_composition import (
    OwnerVoiceAsrCompositionFactory,
)
from main_logic.voice_identity_service.diagnostics import (
    VOICE_IDENTITY_DIAGNOSTIC_COUNTERS as _VOICE_IDENTITY_DIAGNOSTIC_COUNTERS,
)
from main_logic.voice_identity_service.enrollment import (
    SileroEnrollmentSpeechValidator,
)
from main_logic.voice_identity_service.preference_store import (
    VoiceIdentityPreferenceStore,
)
from main_logic.voice_identity_service.profile_store import (
    SecureStorageUnavailableError,
    VoiceIdentityProfileStore,
)
from main_logic.voice_identity_service.registry import (
    install_voice_identity_service_for_app,
)
from main_logic.voice_identity_service.service import VoiceIdentityService
from main_logic.voice_input.suppression import VoiceInputSuppressionController
from main_routers.debug_router import set_voice_identity_diagnostics_provider
from utils.preferences import load_global_conversation_settings


logger = logging.getLogger(__name__)

_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS = 1.0
@dataclass(slots=True)
class _OwnerActivation:
    profile: SpeakerProfile
    generation: str
    enforce: bool
    revision: str = field(default_factory=lambda: str(uuid.uuid4()))
    authority: SpeakerVerifierAuthority = field(default_factory=SpeakerVerifierAuthority)
    _spec: SpeakerVerifierSpec | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_borrowed(
        cls,
        profile: SpeakerProfile,
        generation: str,
        *,
        enforce: bool,
    ) -> "_OwnerActivation":
        return cls(copy.copy(profile), generation, enforce)

    def factory_for(self, manager) -> OwnerVoiceAsrCompositionFactory:
        return OwnerVoiceAsrCompositionFactory(
            manager._asr_runtime,
            self.profile,
            activation_generation=self.generation,
            enforce=self.enforce,
        )

    def spec(self) -> SpeakerVerifierSpec:
        if self._spec is not None:
            return self._spec
        def build(runtime, identity):
            return OwnerVoiceAsrCompositionFactory(
                runtime,
                self.profile,
                activation_generation=self.generation,
                enforce=self.enforce,
                authority=self.authority,
                installation_identity=identity,
            )

        self._spec = SpeakerVerifierSpec(
            self.profile.generation, self.revision, True, self.enforce, self.authority, build,
        )
        return self._spec

    def close(self) -> None:
        self.authority.revoke()
        self.profile.close()


@dataclass(slots=True)
class ActivationPreparation:
    """Registry-owned resource preparation, not a durable configuration commit."""

    candidate: _OwnerActivation | None
    previous: _OwnerActivation | None
    result: VoiceIdentityActivationResult
    managers: list[object] = field(default_factory=list)
    settled: bool = False


class OwnerVoiceRuntimeRegistry:
    """Serialize activation, manager replacement, and enrollment suppression."""

    def __init__(
        self,
        *,
        enforce: bool,
        restore_retry_interval_seconds: float = 0.1,
        restore_retry_timeout_seconds: float = 10.0,
    ) -> None:
        if type(enforce) is not bool:
            raise TypeError("enforce must be bool")
        if (
            not math.isfinite(restore_retry_interval_seconds)
            or not math.isfinite(restore_retry_timeout_seconds)
            or restore_retry_interval_seconds <= 0
            or restore_retry_timeout_seconds < restore_retry_interval_seconds
        ):
            raise ValueError("restore retry bounds are invalid")
        self._enforce = enforce
        self._restore_retry_interval_seconds = float(restore_retry_interval_seconds)
        self._restore_retry_timeout_seconds = float(restore_retry_timeout_seconds)
        self._lock = asyncio.Lock()
        self._managers: weakref.WeakSet = weakref.WeakSet()
        self._restore_pending: weakref.WeakSet = weakref.WeakSet()
        self._restore_retry_task: asyncio.Task[None] | None = None
        self._attach_pending: weakref.WeakSet = weakref.WeakSet()
        self._attach_retry_task: asyncio.Task[None] | None = None
        self._detach_pending: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._detach_retry_task: asyncio.Task[None] | None = None
        self._activation: _OwnerActivation | None = None
        self._prepared_activation: ActivationPreparation | None = None
        self._installation_diagnostics: dict[str, int] = {}
        self._suppressed = False
        self._closed = False

    async def register_manager(
        self,
        manager,
    ) -> VoiceIdentityActivationResult:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Owner voice runtime registry is closed")
            prepared = self._prepared_activation
            target_activation = (
                prepared.candidate if prepared is not None else self._activation
            )
            if prepared is not None and manager not in prepared.managers:
                prepared.managers.append(manager)
            if manager in self._managers:
                activation = target_activation
                needs_attach = manager in self._attach_pending or (
                    activation is not None and manager in self._detach_pending
                )
                if not needs_attach:
                    return (
                        VoiceIdentityActivationResult.READY
                        if activation is None
                        else self._manager_activation_result(manager)
                    )
                if activation is None:
                    self._attach_pending.discard(manager)
                    return VoiceIdentityActivationResult.READY
                self._detach_pending.pop(manager, None)
                result = await self._attach_manager_bounded(manager, activation)
                if result is not VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                    self._record_attach_result(manager, result)
                    return result
                self._attach_pending.add(manager)
                self._ensure_attach_watchdog()
                return VoiceIdentityActivationResult.RUNTIME_DEGRADED
            self._managers.add(manager)
            try:
                if self._suppressed:
                    try:
                        await asyncio.wait_for(
                            manager.set_voice_input_suppressed(
                                "voice_identity_enrollment",
                                suppressed=True,
                            ),
                            timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        self._restore_pending.add(manager)
                        if self._activation is not None:
                            self._attach_pending.add(manager)
                            self._ensure_attach_watchdog()
                        return VoiceIdentityActivationResult.RUNTIME_DEGRADED
                    self._restore_pending.discard(manager)
                elif manager in self._restore_pending:
                    if await self._restore_manager_bounded(
                        manager,
                        "voice_identity_enrollment",
                    ):
                        self._restore_pending.discard(manager)
                    else:
                        self._ensure_restore_watchdog(
                            "voice_identity_enrollment"
                        )
                        return VoiceIdentityActivationResult.RUNTIME_DEGRADED
                activation = target_activation
                if activation is not None:
                    self._detach_pending.pop(manager, None)
                    result = await self._attach_manager_bounded(manager, activation)
                    if result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                        self._attach_pending.add(manager)
                        self._ensure_attach_watchdog()
                        return VoiceIdentityActivationResult.RUNTIME_DEGRADED
                    self._record_attach_result(manager, result)
                    self._detach_pending.pop(manager, None)
                    return result
                return VoiceIdentityActivationResult.READY
            except asyncio.CancelledError:
                activation = self._activation
                if self._suppressed:
                    # The manager belongs to the active enrollment gate. Keep it
                    # gated until restore() ends the lease; opening it here would
                    # admit normal PCM while every existing manager is suppressed.
                    self._restore_pending.add(manager)
                    if activation is not None:
                        self._attach_pending.add(manager)
                        self._ensure_attach_watchdog()
                elif manager in self._restore_pending:
                    self._ensure_restore_watchdog("voice_identity_enrollment")
                    if activation is not None:
                        self._attach_pending.add(manager)
                        self._ensure_attach_watchdog()
                elif activation is not None and manager in self._managers:
                    self._attach_pending.add(manager)
                    self._ensure_attach_watchdog()
                raise
            except BaseException:
                if self._suppressed:
                    self._restore_pending.add(manager)
                    if self._activation is not None:
                        self._attach_pending.add(manager)
                        self._ensure_attach_watchdog()
                elif manager in self._restore_pending:
                    self._ensure_restore_watchdog("voice_identity_enrollment")
                raise

    async def unregister_manager(self, manager) -> None:
        async with self._lock:
            self._managers.discard(manager)
            self._attach_pending.discard(manager)
            detach_generation = str(uuid.uuid4())
            cancellation: asyncio.CancelledError | None = None
            try:
                detached = await asyncio.wait_for(
                    self._detach_manager(manager, detach_generation),
                    timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                detached = False
            except asyncio.CancelledError as exc:
                cancellation = exc
                detached = False
            except Exception:
                detached = False
            if detached is VoiceIdentityActivationResult.READY:
                self._detach_pending.pop(manager, None)
            else:
                self._detach_pending[manager] = detach_generation
                self._ensure_detach_watchdog()
            if self._suppressed or manager in self._restore_pending:
                try:
                    restored = await self._restore_manager_bounded(
                        manager,
                        "voice_identity_enrollment",
                    )
                except asyncio.CancelledError as exc:
                    self._restore_pending.add(manager)
                    if not self._suppressed:
                        self._ensure_restore_watchdog("voice_identity_enrollment")
                    if cancellation is None:
                        cancellation = exc
                else:
                    if restored:
                        self._restore_pending.discard(manager)
                    else:
                        self._restore_pending.add(manager)
                        if not self._suppressed:
                            self._ensure_restore_watchdog(
                                "voice_identity_enrollment"
                            )
            if cancellation is not None:
                raise cancellation

    async def activate(
        self,
        profile: SpeakerProfile | None,
        generation: str,
    ) -> VoiceIdentityActivationResult:
        """Convenience operation for initialization/toggles (not enrollment)."""
        prepared = await self.prepare_activation(profile, generation)
        if prepared.result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
            return prepared.result
        return self.commit_activation(prepared)

    async def prepare_activation(
        self, profile: SpeakerProfile | None, generation: str,
    ) -> ActivationPreparation:
        if type(generation) is not str or not generation.strip():
            return ActivationPreparation(
                None, None, VoiceIdentityActivationResult.RUNTIME_DEGRADED, settled=True,
            )
        async with self._lock:
            if self._closed:
                return ActivationPreparation(
                    None, None, VoiceIdentityActivationResult.RUNTIME_DEGRADED, settled=True,
                )
            candidate = (
                None if profile is None else
                _OwnerActivation.from_borrowed(profile, generation, enforce=self._enforce)
            )
            self._count_installation("install_requested")
            previous_preparation = self._prepared_activation
            if previous_preparation is not None:
                self.revoke_prepared_activation(previous_preparation)
                previous_preparation.settled = True
                if previous_preparation.candidate is not None:
                    previous_preparation.candidate.close()
            prepared = ActivationPreparation(
                candidate, self._activation, VoiceIdentityActivationResult.READY,
            )
            self._prepared_activation = prepared
            # Disable must revoke before asynchronous teardown. A staged replacement
            # has no authority until commit, while the old activation remains valid
            # only in managers where it has not yet been replaced.
            if candidate is None and self._activation is not None:
                self._activation.authority.revoke()
                self._retire_activation_installations(self._activation, tuple(self._managers))
            if candidate is None:
                previous = self._activation
                self._activation = None
                self._attach_pending.clear()
                prepared.previous = None
                if previous is not None:
                    previous.close()
                managers = tuple(self._managers)
                for index, manager in enumerate(managers):
                    try:
                        result = await self._detach_manager(manager, generation)
                    except asyncio.CancelledError:
                        for pending in managers[index:]:
                            self._detach_pending[pending] = generation
                        self._ensure_detach_watchdog()
                        prepared.result = VoiceIdentityActivationResult.RUNTIME_DEGRADED
                        prepared.settled = True
                        self._prepared_activation = None
                        if self._current_task_is_cancelling():
                            raise
                        return prepared
                    if result is VoiceIdentityActivationResult.READY:
                        self._detach_pending.pop(manager, None)
                    else:
                        self._detach_pending[manager] = generation
                        prepared.result = VoiceIdentityActivationResult.RUNTIME_DEGRADED
                if self._detach_pending:
                    self._ensure_detach_watchdog()
                if prepared.result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                    prepared.settled = True
                    self._prepared_activation = None
                return prepared
            try:
                for manager in tuple(self._managers):
                    prepared.managers.append(manager)
                    if candidate is None:
                        result = await self._detach_manager(manager, generation)
                    else:
                        result = await self._attach_manager_bounded(manager, candidate)
                    if result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                        self._count_installation("install_failed")
                        raise RuntimeError("speaker verifier preparation failed")
                    if (
                        result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
                        and self._manager_participating(manager)
                    ):
                        prepared.result = result
                    elif (
                        result is VoiceIdentityActivationResult.ACTIVATION_PENDING
                        and prepared.result is VoiceIdentityActivationResult.READY
                    ):
                        prepared.result = result
                return prepared
            except BaseException as exc:
                self.revoke_prepared_activation(prepared)
                self._abort_preparation_locked(prepared)
                if isinstance(exc, asyncio.CancelledError) and self._current_task_is_cancelling():
                    raise
                prepared.result = VoiceIdentityActivationResult.RUNTIME_DEGRADED
                return prepared

    def commit_activation(self, prepared: ActivationPreparation) -> VoiceIdentityActivationResult:
        """No await: the only point granting staged configuration authority."""
        if (
            self._closed or prepared.settled
            or self._prepared_activation is not prepared
        ):
            self._count_installation("install_stale")
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        candidate = prepared.candidate
        if candidate is not None and candidate.authority.commit() is not True:
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        old = self._activation
        self._activation = candidate
        self._prepared_activation = None
        prepared.settled = True
        self._attach_pending.clear()
        if old is not None and old is not candidate:
            old.close()
            self._retire_activation_installations(old, tuple(self._managers))
        if candidate is None:
            return prepared.result
        for manager in self._managers:
            snapshot = getattr(manager, "speaker_verifier_installation_status", None)
            if callable(snapshot):
                self._record_attach_result(manager, self._receipt_result(snapshot(candidate.revision)))
        # Re-query live installation snapshots; routes may have changed while
        # preference/profile persistence was awaited by Service.
        snapshot_result = self.activation_status()
        if (
            snapshot_result is VoiceIdentityActivationResult.READY
            and prepared.result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
            and any(
                self._manager_participating(manager)
                and not callable(getattr(manager, "speaker_verifier_installation_status", None))
                for manager in prepared.managers
            )
        ):
            return prepared.result
        return snapshot_result

    def revoke_prepared_activation(self, prepared: ActivationPreparation) -> None:
        if prepared.candidate is not None:
            prepared.candidate.authority.revoke()
            self._retire_activation_installations(prepared.candidate, prepared.managers)

    def _retire_activation_installations(self, activation, managers) -> None:
        """Synchronously poison queued facts, without touching a successor install."""
        for manager in managers:
            runtime = getattr(manager, "_asr_runtime", None)
            pending = getattr(runtime, "_speaker_installation_pending", None)
            identity = pending or getattr(runtime, "_speaker_installation_identity", None)
            if identity is None or identity.activation_revision != activation.revision:
                continue
            retire = getattr(runtime, "retire_speaker_verifier_authority", None)
            if callable(retire):
                try:
                    retire()
                except Exception:
                    # Shared token is already revoked; failure to eagerly drain
                    # does not re-grant authority. Keep recovery diagnostics.
                    self._count_installation("authority_retirement_failed")

    def _count_installation(self, reason: str) -> None:
        self._installation_diagnostics[reason] = self._installation_diagnostics.get(reason, 0) + 1

    def installation_diagnostics_snapshot(self) -> dict[str, int]:
        """Internal control-plane counters; no profile/session/install identity."""
        return dict(self._installation_diagnostics)

    def _abort_preparation_locked(self, prepared: ActivationPreparation) -> None:
        if prepared.settled or self._prepared_activation is not prepared:
            return
        self.revoke_prepared_activation(prepared)
        previous = prepared.previous
        restored = (
            None if previous is None else
            _OwnerActivation.from_borrowed(
                previous.profile, previous.generation, enforce=previous.enforce,
            )
        )
        if restored is not None:
            restored.authority.commit()
        self._activation = restored
        self._prepared_activation = None
        prepared.settled = True
        # Publish recovery target before retrying any manager. Unsupported/deferred
        # are obligations, never completed compensation.
        self._rollback_activation(list(self._managers), restored)
        self._count_installation("rollback_pending")
        if prepared.candidate is not None:
            prepared.candidate.close()
        if previous is not None:
            previous.close()

    async def abort_activation(self, prepared: ActivationPreparation) -> VoiceIdentityActivationResult:
        self.revoke_prepared_activation(prepared)
        async with self._lock:
            self._abort_preparation_locked(prepared)
            return self.activation_status()

    @staticmethod
    async def _detach_manager(manager, generation: str) -> VoiceIdentityActivationResult:
        setter = getattr(manager, "set_speaker_verifier_spec", None)
        try:
            if callable(setter):
                receipt = await asyncio.wait_for(
                    setter(None), timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                )
                return (
                    VoiceIdentityActivationResult.READY
                    if receipt.outcome is SpeakerVerifierInstallOutcome.REVOKED
                    else VoiceIdentityActivationResult.RUNTIME_DEGRADED
                )
            result = await asyncio.wait_for(
                manager.set_speaker_verifier_factory(
                    None, activation_generation=generation,
                ), timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
            )
            return OwnerVoiceRuntimeRegistry._legacy_activation_result(result)
        except Exception:
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED

    @staticmethod
    def _legacy_activation_result(result) -> VoiceIdentityActivationResult:
        """The sole compatibility boundary for pre-contract bool callbacks."""
        if isinstance(result, VoiceIdentityActivationResult):
            return result
        return (
            VoiceIdentityActivationResult.READY if result is True else
            VoiceIdentityActivationResult.RUNTIME_DEGRADED
        )

    def _record_attach_result(self, manager, result: VoiceIdentityActivationResult) -> None:
        if result is VoiceIdentityActivationResult.READY:
            if manager in self._attach_pending:
                self._count_installation("rollback_settled")
            self._attach_pending.discard(manager)
            self._count_installation("install_installed")
        else:
            # Route-triggered Core reconcile owns retry; this weak obligation is
            # retained without an idle polling task until an installed snapshot.
            self._attach_pending.add(manager)
            self._count_installation(
                "install_unsupported" if result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
                else "install_deferred" if result is VoiceIdentityActivationResult.ACTIVATION_PENDING
                else "install_failed"
            )

    @staticmethod
    def _receipt_result(receipt: SpeakerVerifierInstallReceipt) -> VoiceIdentityActivationResult:
        if receipt.outcome is SpeakerVerifierInstallOutcome.INSTALLED:
            return VoiceIdentityActivationResult.READY
        if receipt.outcome in (
            SpeakerVerifierInstallOutcome.DEFERRED_ROUTE,
            SpeakerVerifierInstallOutcome.STALE,
        ):
            return VoiceIdentityActivationResult.ACTIVATION_PENDING
        if receipt.outcome is SpeakerVerifierInstallOutcome.UNSUPPORTED_ROUTE:
            return VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
        return VoiceIdentityActivationResult.RUNTIME_DEGRADED

    def activation_status(self) -> VoiceIdentityActivationResult:
        activation = self._activation
        if self._closed or activation is None:
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        if self._prepared_activation is not None:
            return VoiceIdentityActivationResult.ACTIVATION_PENDING
        managers = tuple(m for m in self._managers if self._manager_participating(m))
        if not managers:
            return VoiceIdentityActivationResult.ACTIVATION_PENDING
        result = VoiceIdentityActivationResult.READY
        for manager in managers:
            manager_result = self._manager_activation_result(manager)
            if manager_result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
                return manager_result
            if manager_result is VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE:
                result = manager_result
            elif (
                manager_result is VoiceIdentityActivationResult.ACTIVATION_PENDING
                and result is VoiceIdentityActivationResult.READY
            ):
                result = manager_result
            elif manager in self._restore_pending or manager in self._detach_pending:
                return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        return result

    @staticmethod
    def _manager_participating(manager) -> bool:
        value = getattr(manager, "speaker_verifier_participating", None)
        if type(value) is bool:
            return value
        # Legacy adapters without route metadata are assumed to represent a live
        # route, but real Core must report its actual input mode.
        if getattr(manager, "is_active", None) is False:
            return False
        return getattr(manager, "input_mode", "audio") == "audio"

    def diagnostics_snapshot(self) -> dict[str, int]:
        """Aggregate verifier counters without exposing identity or scores."""

        totals = {name: 0 for name in _VOICE_IDENTITY_DIAGNOSTIC_COUNTERS}
        managers = tuple(self._managers)
        seen_runtimes: set[int] = set()
        for manager in managers:
            runtime = getattr(manager, "_asr_runtime", None)
            runtime_id = id(runtime)
            if runtime is None or runtime_id in seen_runtimes:
                continue
            seen_runtimes.add(runtime_id)
            snapshot = getattr(runtime, "_speaker_verifier_diagnostics", None)
            if not callable(snapshot):
                continue
            try:
                runtime_metrics = snapshot()
            except Exception:
                continue
            if not isinstance(runtime_metrics, dict):
                continue
            for name in _VOICE_IDENTITY_DIAGNOSTIC_COUNTERS:
                value = runtime_metrics.get(name)
                if type(value) is int and value >= 0:
                    totals[name] += value
            missing_identity = runtime_metrics.get(
                "provider_candidate_bind_missing_identity_count"
            )
            seal_unbound = runtime_metrics.get(
                "rejection_seal_snapshot_unbound_count"
            )
            has_missing_identity = bool(
                type(missing_identity) is int and missing_identity > 0
            )
            has_seal_unbound = bool(type(seal_unbound) is int and seal_unbound > 0)
            if has_missing_identity:
                totals["diagnostic_runtime_missing_identity_count"] += 1
            if has_seal_unbound:
                totals["diagnostic_runtime_seal_unbound_count"] += 1
            if has_missing_identity and has_seal_unbound:
                totals[
                    "diagnostic_runtime_missing_identity_and_seal_unbound_count"
                ] += 1
        totals["registered_manager_count"] = len(managers)
        totals["diagnostic_runtime_count"] = len(seen_runtimes)
        return totals

    def _manager_activation_result(self, manager) -> VoiceIdentityActivationResult:
        activation = self._activation
        snapshot = getattr(manager, "speaker_verifier_installation_status", None)
        if callable(snapshot) and activation is not None:
            receipt = snapshot(activation.revision)
            result = self._receipt_result(receipt)
            if result is not VoiceIdentityActivationResult.READY:
                return result
            if not activation.authority.permits_evidence:
                return VoiceIdentityActivationResult.ACTIVATION_PENDING
            return result
        if OwnerVoiceRuntimeRegistry._manager_is_inactive_blocked(manager):
            return VoiceIdentityActivationResult.ACTIVATION_PENDING
        if manager in self._attach_pending:
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        runtime = getattr(manager, "_asr_runtime", None)
        if bool(getattr(runtime, "_speaker_verifier_degraded", False)):
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        return OwnerVoiceRuntimeRegistry._manager_route_result(manager)

    @staticmethod
    def _manager_route_result(manager) -> VoiceIdentityActivationResult:
        route_mode = getattr(manager, "_asr_route_mode", None)
        if OwnerVoiceRuntimeRegistry._manager_is_inactive_blocked(manager):
            return VoiceIdentityActivationResult.ACTIVATION_PENDING
        if route_mode is not None and route_mode != "independent":
            return VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE
        return VoiceIdentityActivationResult.READY

    @staticmethod
    def _manager_is_inactive_blocked(manager) -> bool:
        return (
            getattr(manager, "_asr_route_mode", None) == "blocked"
            and getattr(manager, "is_active", None) is False
        )

    @staticmethod
    def _current_task_is_cancelling() -> bool:
        current = asyncio.current_task()
        return current is not None and current.cancelling() > 0

    @staticmethod
    async def _attach_manager(
        manager,
        activation: _OwnerActivation,
    ) -> VoiceIdentityActivationResult:
        factory: OwnerVoiceAsrCompositionFactory | None = None
        try:
            setter = getattr(manager, "set_speaker_verifier_spec", None)
            if callable(setter):
                receipt = await setter(activation.spec())
                return OwnerVoiceRuntimeRegistry._receipt_result(receipt)
            factory = activation.factory_for(manager)
            updated = await asyncio.wait_for(
                manager.set_speaker_verifier_factory(
                    factory,
                    activation_generation=activation.generation,
                ),
                timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            if factory is not None:
                factory.close()
            raise
        except BaseException:
            if factory is not None:
                factory.close()
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        result = OwnerVoiceRuntimeRegistry._legacy_activation_result(updated)
        if OwnerVoiceRuntimeRegistry._manager_is_inactive_blocked(manager):
            return VoiceIdentityActivationResult.ACTIVATION_PENDING
        if result is VoiceIdentityActivationResult.RUNTIME_DEGRADED:
            factory.close()
        return result

    @staticmethod
    async def _attach_manager_bounded(
        manager,
        activation: _OwnerActivation,
    ) -> VoiceIdentityActivationResult:
        try:
            return await asyncio.wait_for(
                OwnerVoiceRuntimeRegistry._attach_manager(manager, activation),
                timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED
        except asyncio.CancelledError:
            if OwnerVoiceRuntimeRegistry._current_task_is_cancelling():
                raise
            return VoiceIdentityActivationResult.RUNTIME_DEGRADED

    def _ensure_attach_watchdog(self) -> None:
        task = self._attach_retry_task
        if task is not None and not task.done():
            return
        self._attach_retry_task = asyncio.create_task(
            self._run_attach_watchdog(),
            name="voice-identity-attach-watchdog",
        )

    async def _run_attach_watchdog(self) -> None:
        current = asyncio.current_task()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._restore_retry_timeout_seconds
        try:
            while loop.time() < deadline:
                await asyncio.sleep(self._restore_retry_interval_seconds)
                async with self._lock:
                    if self._closed:
                        return
                    if self._prepared_activation is not None:
                        return
                    activation = self._activation
                    if activation is None:
                        self._attach_pending.clear()
                        return
                    targets = tuple(self._attach_pending)
                    if not targets:
                        return
                    for manager in targets:
                        if manager not in self._managers:
                            self._attach_pending.discard(manager)
                            continue
                        call_timeout = min(
                            _WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                            deadline - loop.time(),
                        )
                        if call_timeout <= 0:
                            break
                        try:
                            attached = await asyncio.wait_for(
                                self._attach_manager(manager, activation),
                                timeout=call_timeout,
                            )
                        except asyncio.TimeoutError:
                            continue
                        except asyncio.CancelledError:
                            if current is not None and current.cancelling():
                                raise
                            continue
                        if attached is VoiceIdentityActivationResult.READY:
                            self._attach_pending.discard(manager)
                            self._detach_pending.pop(manager, None)
                        elif attached in (
                            VoiceIdentityActivationResult.ACTIVATION_PENDING,
                            VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE,
                        ) and callable(getattr(manager, "set_speaker_verifier_spec", None)):
                            # Core now owns the deferred obligation and resumes on
                            # route startup. Do not poll an idle microphone.
                            continue
                    if all(
                        callable(getattr(manager, "speaker_verifier_installation_status", None))
                        and self._receipt_result(
                            manager.speaker_verifier_installation_status(activation.revision)
                        ) in (
                            VoiceIdentityActivationResult.ACTIVATION_PENDING,
                            VoiceIdentityActivationResult.UNSUPPORTED_ASR_ROUTE,
                        )
                        for manager in self._attach_pending
                    ):
                        return
            async with self._lock:
                pending_count = 0 if self._closed else len(self._attach_pending)
            if pending_count:
                logger.warning(
                    "Owner voice verifier attach watchdog exhausted with %d "
                    "manager(s) still pending",
                    pending_count,
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._attach_retry_task is current:
                self._attach_retry_task = None

    def _rollback_activation(
        self,
        managers: list[object],
        activation: _OwnerActivation | None,
    ) -> None:
        for manager in managers:
            if activation is None:
                self._attach_pending.discard(manager)
                self._detach_pending[manager] = str(uuid.uuid4())
            else:
                self._attach_pending.add(manager)
                self._detach_pending.pop(manager, None)
        if self._attach_pending:
            self._ensure_attach_watchdog()
        if self._detach_pending:
            self._ensure_detach_watchdog()

    async def suppress(self, reason: str) -> None:
        await self._set_suppressed(reason, True)

    async def restore(self, reason: str) -> None:
        await self._set_suppressed(reason, False)

    async def _set_suppressed(self, reason: str, suppressed: bool) -> None:
        if reason != "voice_identity_enrollment":
            raise ValueError("unsupported voice input suppression reason")
        async with self._lock:
            if self._closed:
                if suppressed:
                    raise RuntimeError("Owner voice runtime registry is closed")
                return
            if self._suppressed is suppressed and (
                suppressed or not self._restore_pending
            ):
                return
            if not suppressed:
                targets = tuple(set(self._managers).union(self._restore_pending))
                for manager in targets:
                    self._restore_pending.add(manager)
                try:
                    for manager in targets:
                        try:
                            await self._restore_manager(manager, reason)
                        except Exception:
                            self._restore_pending.add(manager)
                        else:
                            self._restore_pending.discard(manager)
                finally:
                    # Even cancellation or an unexpected BaseException cannot
                    # make future/replacement managers inherit a stale gate.
                    # Per-manager transient failures are retried immediately
                    # and then by the bounded watchdog below.
                    self._suppressed = False
                    if self._restore_pending:
                        self._ensure_restore_watchdog(reason)
                return
            changed: list[object] = []
            try:
                for manager in tuple(self._managers):
                    # Include the in-flight manager before awaiting: its Core
                    # gate is published synchronously before ASR abort/cleanup,
                    # so cancellation may leave work incomplete but must still
                    # trigger a restore attempt.
                    changed.append(manager)
                    await asyncio.wait_for(
                        manager.set_voice_input_suppressed(
                            reason,
                            suppressed=suppressed,
                        ),
                        timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                    )
            except BaseException:
                for manager in changed:
                    self._restore_pending.add(manager)
                try:
                    for manager in reversed(changed):
                        try:
                            await self._restore_manager(manager, reason)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            continue
                        else:
                            self._restore_pending.discard(manager)
                finally:
                    if self._restore_pending:
                        self._ensure_restore_watchdog(reason)
                raise
            for manager in changed:
                self._restore_pending.discard(manager)
            self._suppressed = suppressed

    def _ensure_restore_watchdog(self, reason: str) -> None:
        task = self._restore_retry_task
        if task is not None and not task.done():
            return
        self._restore_retry_task = asyncio.create_task(
            self._run_restore_watchdog(reason),
            name="voice-identity-restore-watchdog",
        )

    async def _run_restore_watchdog(self, reason: str) -> None:
        current = asyncio.current_task()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._restore_retry_timeout_seconds
        try:
            while loop.time() < deadline:
                await asyncio.sleep(self._restore_retry_interval_seconds)
                async with self._lock:
                    if self._closed or self._suppressed:
                        return
                    targets = tuple(self._restore_pending)
                    if not targets:
                        return
                    for manager in targets:
                        call_timeout = min(
                            _WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                            deadline - loop.time(),
                        )
                        if call_timeout <= 0:
                            break
                        try:
                            await asyncio.wait_for(
                                self._restore_manager(manager, reason),
                                timeout=call_timeout,
                            )
                        except asyncio.CancelledError:
                            if current is not None and current.cancelling():
                                raise
                            continue
                        except Exception:
                            continue
                        self._restore_pending.discard(manager)
            async with self._lock:
                pending_count = (
                    0
                    if self._closed or self._suppressed
                    else len(self._restore_pending)
                )
            if pending_count:
                logger.warning(
                    "Owner voice input restore watchdog exhausted with %d "
                    "manager(s) still pending",
                    pending_count,
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._restore_retry_task is current:
                self._restore_retry_task = None

    def _ensure_detach_watchdog(self) -> None:
        task = self._detach_retry_task
        if task is not None and not task.done():
            return
        self._detach_retry_task = asyncio.create_task(
            self._run_detach_watchdog(),
            name="voice-identity-detach-watchdog",
        )

    async def _run_detach_watchdog(self) -> None:
        current = asyncio.current_task()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._restore_retry_timeout_seconds
        try:
            while loop.time() < deadline:
                await asyncio.sleep(self._restore_retry_interval_seconds)
                async with self._lock:
                    if self._closed:
                        return
                    targets = tuple(self._detach_pending.items())
                    if not targets:
                        return
                    for manager, generation in targets:
                        call_timeout = min(
                            _WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                            deadline - loop.time(),
                        )
                        if call_timeout <= 0:
                            break
                        try:
                            detached = await asyncio.wait_for(
                                self._detach_manager(manager, generation),
                                timeout=call_timeout,
                            )
                        except asyncio.CancelledError:
                            if current is not None and current.cancelling():
                                raise
                            continue
                        except Exception:
                            continue
                        if detached is VoiceIdentityActivationResult.READY:
                            self._detach_pending.pop(manager, None)
            async with self._lock:
                pending_count = 0 if self._closed else len(self._detach_pending)
            if pending_count:
                logger.warning(
                    "Owner voice verifier detach watchdog exhausted with %d "
                    "manager(s) still pending",
                    pending_count,
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._detach_retry_task is current:
                self._detach_retry_task = None

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._activation is not None:
                self._activation.authority.revoke()
                self._retire_activation_installations(self._activation, tuple(self._managers))
            prepared = self._prepared_activation
            if prepared is not None:
                self.revoke_prepared_activation(prepared)
                prepared.settled = True
                self._prepared_activation = None
                if prepared.candidate is not None:
                    prepared.candidate.close()
            retry_task = self._restore_retry_task
            attach_task = self._attach_retry_task
            detach_task = self._detach_retry_task
        tasks = tuple(
            task
            for task in (retry_task, attach_task, detach_task)
            if task is not None
        )
        for task in tasks:
            task.cancel()
        cleanup_cancellations: list[asyncio.CancelledError] = []
        cleanup_task = asyncio.create_task(
            self._finish_close_cleanup(retry_task, attach_task, detach_task),
            name="voice-identity-registry-close-cleanup",
        )
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                if not cleanup_cancellations:
                    cleanup_cancellations.append(exc)
        await cleanup_task
        if cleanup_cancellations:
            raise cleanup_cancellations[0]

    async def _finish_close_cleanup(
        self,
        retry_task: asyncio.Task[None] | None,
        attach_task: asyncio.Task[None] | None,
        detach_task: asyncio.Task[None] | None,
    ) -> None:
        tasks = tuple(
            task
            for task in (retry_task, attach_task, detach_task)
            if task is not None
        )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._restore_retry_task is retry_task:
            self._restore_retry_task = None
        if self._attach_retry_task is attach_task:
            self._attach_retry_task = None
        if self._detach_retry_task is detach_task:
            self._detach_retry_task = None
        async with self._lock:
            managers = tuple(
                set(self._managers)
                .union(self._restore_pending)
                .union(self._detach_pending)
            )
            self._suppressed = False
            self._restore_pending.clear()
            self._attach_pending.clear()
            self._detach_pending.clear()
            try:
                for manager in managers:
                    try:
                        await asyncio.wait_for(
                            self._restore_manager(
                                manager,
                                "voice_identity_enrollment",
                            ),
                            timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                        )
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            raise
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(
                            self._detach_manager(manager, str(uuid.uuid4())),
                            timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
                        )
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            raise
                    except Exception:
                        pass
            finally:
                self._managers.clear()
                activation = self._activation
                self._activation = None
                if activation is not None:
                    activation.close()

    @staticmethod
    async def _restore_manager(manager, reason: str) -> None:
        last_error: BaseException | None = None
        attempts = 2
        attempt_timeout = _WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS / attempts
        for _attempt in range(attempts):
            try:
                await asyncio.wait_for(
                    manager.set_voice_input_suppressed(
                        reason,
                        suppressed=False,
                    ),
                    timeout=attempt_timeout,
                )
                return
            except asyncio.CancelledError as exc:
                if OwnerVoiceRuntimeRegistry._current_task_is_cancelling():
                    raise
                last_error = exc
                await asyncio.sleep(0)
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(0)
        assert last_error is not None
        raise last_error

    @staticmethod
    async def _restore_manager_bounded(manager, reason: str) -> bool:
        try:
            await asyncio.wait_for(
                OwnerVoiceRuntimeRegistry._restore_manager(manager, reason),
                timeout=_WATCHDOG_MANAGER_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            if OwnerVoiceRuntimeRegistry._current_task_is_cancelling():
                raise
            return False
        except Exception:
            return False
        return True


_runtime_registry: OwnerVoiceRuntimeRegistry | None = None
_service: VoiceIdentityService | None = None


class _UnavailableProfileStore(VoiceIdentityProfileStore):
    """Concrete fail-closed store used when DPAPI cannot be constructed."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self):
        raise SecureStorageUnavailableError("secure_storage_unavailable")

    def stage(self, profile: SpeakerProfile, *, audio_contract):
        del profile, audio_contract
        raise SecureStorageUnavailableError("secure_storage_unavailable")

    def delete(self) -> bool:
        raise SecureStorageUnavailableError("secure_storage_unavailable")


def install_voice_identity_runtime(config_manager) -> VoiceIdentityService:
    """Construct and install the application singleton once."""

    global _runtime_registry, _service
    if _service is not None:
        return _service
    configured_mode = (
        os.environ.get(
            "NEKO_VOICE_IDENTITY_MODE",
            "enforce",
        )
        .strip()
        .lower()
    )
    runtime_mode = (
        configured_mode if configured_mode in {"off", "shadow", "enforce"} else "off"
    )
    if configured_mode not in {"off", "shadow", "enforce"}:
        logger.warning(
            "Unsupported NEKO_VOICE_IDENTITY_MODE value %r; Owner voice "
            "filtering is disabled",
            configured_mode,
        )
    registry = OwnerVoiceRuntimeRegistry(enforce=runtime_mode == "enforce")
    local_state_dir = Path(config_manager.local_state_dir)
    try:
        profile_store = VoiceIdentityProfileStore(
            local_state_dir / "voice_identity.profile"
        )
    except SecureStorageUnavailableError:
        profile_store = _UnavailableProfileStore(
            local_state_dir / "voice_identity.profile"
        )
    suppression = VoiceInputSuppressionController(
        registry.suppress,
        registry.restore,
        default_ttl_seconds=45.0,
        hard_ttl_seconds=60.0,
    )
    service = VoiceIdentityService(
        profile_store,
        VoiceIdentityPreferenceStore(local_state_dir / "voice_identity.settings.json"),
        suppression,
        CampPlusEmbeddingModel,
        registry.activate,
        runtime_mode=runtime_mode,
        enrollment_ttl_seconds=45.0,
        runtime_status_callback=registry.activation_status,
        activation_transaction=registry,
        speech_validator_factory=SileroEnrollmentSpeechValidator,
        enrollment_noise_reduction_enabled=(
            load_global_conversation_settings().get(
                "noiseReductionEnabled",
                True,
            )
            is not False
        ),
    )
    install_voice_identity_service_for_app(service)
    _runtime_registry = registry
    _service = service
    return service


async def initialize_voice_identity_runtime(config_manager) -> None:
    service = install_voice_identity_runtime(config_manager)
    await service.initialize()


async def close_voice_identity_runtime() -> None:
    service = _service
    registry = _runtime_registry
    try:
        if service is not None:
            await service.close()
    except BaseException:
        try:
            if registry is not None:
                await registry.close()
        except BaseException:
            logger.warning(
                "Owner voice runtime registry cleanup failed after service "
                "cleanup failure",
                exc_info=True,
            )
        raise
    if registry is not None:
        await registry.close()


async def register_voice_identity_manager(
    manager,
) -> VoiceIdentityActivationResult:
    registry = _runtime_registry
    if registry is None:
        return VoiceIdentityActivationResult.READY
    try:
        return await registry.register_manager(manager)
    except Exception:
        return VoiceIdentityActivationResult.RUNTIME_DEGRADED


async def unregister_voice_identity_manager(manager) -> None:
    registry = _runtime_registry
    if registry is not None:
        await registry.unregister_manager(manager)


def get_voice_identity_diagnostics() -> dict[str, int]:
    registry = _runtime_registry
    if registry is None:
        return {
            **{name: 0 for name in _VOICE_IDENTITY_DIAGNOSTIC_COUNTERS},
            "registered_manager_count": 0,
            "diagnostic_runtime_count": 0,
        }
    return registry.diagnostics_snapshot()


set_voice_identity_diagnostics_provider(get_voice_identity_diagnostics)


__all__ = [
    "OwnerVoiceRuntimeRegistry",
    "close_voice_identity_runtime",
    "get_voice_identity_diagnostics",
    "initialize_voice_identity_runtime",
    "install_voice_identity_runtime",
    "register_voice_identity_manager",
    "unregister_voice_identity_manager",
]
