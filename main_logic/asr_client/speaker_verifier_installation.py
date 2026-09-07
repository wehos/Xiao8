"""Instance-scoped verifier installation; no Provider or application policy."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from .admission.contracts import (
    SpeakerAuthorityUnavailable,
    SpeakerAuthorityUnarmed,
    SpeakerLeaseUnavailable,
)
from .admission.ingress import (
    AdmissionIngressClosedError,
    AdmissionIngressCapacityError,
)

from .speaker_verifier_contracts import (
    SpeakerVerifierAuthority,
    SpeakerVerifierAuthorityState,
    SpeakerVerifierHealthEvent,
    SpeakerVerifierInstallIdentity,
    SpeakerVerifierInstallOutcome as Outcome,
    SpeakerVerifierInstallReceipt,
    SpeakerVerifierOwnershipState as Ownership,
    SpeakerVerifierReplacementOperation,
    SpeakerVerifierSpec,
)


class SpeakerVerifierInstallation:
    """Explicit Runtime-owned component; no inheritance or attribute proxy.

    Mutable session state stays on the owning Runtime so existing identity
    fences, callbacks and cleanup owners all observe the same snapshot.
    """

    def __init__(self, runtime) -> None:
        self._runtime = runtime

    def _ensure_speaker_installation_state(self) -> None:
        if hasattr(self._runtime, "_speaker_installation_serial"):
            return
        self._runtime._speaker_installation_serial = 0
        self._runtime._speaker_installation_identity = None
        self._runtime._speaker_installation_shadow = None
        self._runtime._speaker_installation_pending = None
        self._runtime._speaker_installation_health = {}
        self._runtime._speaker_installation_spec = None
        self._runtime._speaker_verifier_install_receipt = None
        self._runtime._speaker_retired_cleanup = set()
        self._runtime._speaker_installation_diagnostics = {}

    def _count_speaker_installation(self, reason: str) -> None:
        counts = self._runtime._speaker_installation_diagnostics
        counts[reason] = counts.get(reason, 0) + 1

    def create_speaker_verifier_install_identity(
        self, manager_identity: int, route_generation: int, activation_revision: str
    ) -> SpeakerVerifierInstallIdentity:
        self._runtime._ensure_asr_runtime_state()
        self._ensure_speaker_installation_state()
        detector = self._runtime._asr_detector
        return SpeakerVerifierInstallIdentity(
            manager_identity=manager_identity,
            runtime_identity=id(self._runtime),
            session_generation=self._runtime._asr_session_epoch,
            route_generation=route_generation,
            detector_identity=id(detector) if detector is not None else 0,
            detector_epoch=getattr(detector, "_detector_epoch", 0),
            activation_revision=activation_revision,
            installation_id=uuid4().hex,
        )

    def _speaker_install_identity_current(self, identity) -> bool:
        detector = self._runtime._asr_detector
        committed = identity == getattr(self._runtime, "_speaker_installation_identity", None)
        # Candidate reset advances Detector's audio epoch without replacing its
        # installed observer. During prepare that change invalidates the receipt;
        # after commit the captured mount owner, not a per-turn epoch, is proof.
        detector_proof = (
            not getattr(detector, "_closed", False)
            and getattr(detector, "_speaker_shadow", None) is not None
            and getattr(detector, "_speaker_shadow", None)
            is self._runtime._speaker_installation_shadow
            and getattr(detector, "_speaker_owner_generation", None)
            == identity.installation_id
            if committed
            else identity.detector_epoch == getattr(detector, "_detector_epoch", 0)
        )
        return (
            not self._runtime._asr_terminal_close_requested
            and identity.runtime_identity == id(self._runtime)
            and identity.session_generation == self._runtime._asr_session_epoch
            and identity.detector_identity
            == (id(detector) if detector is not None else 0)
            and detector_proof
        )

    def speaker_verifier_installation_permits_evidence(self, identity) -> bool:
        self._ensure_speaker_installation_state()
        spec = self._runtime._speaker_installation_spec
        return (
            identity == self._runtime._speaker_installation_identity
            and self._speaker_install_identity_current(identity)
            and spec is not None
            and spec.revocable_authority.permits_evidence
        )

    def retire_speaker_verifier_authority(self) -> None:
        """No await: stale producers lose permission before route mutation."""
        self._runtime._ensure_asr_runtime_state()
        self._ensure_speaker_installation_state()
        self._runtime._speaker_installation_serial += 1
        self._runtime._speaker_installation_identity = None
        self._runtime._speaker_installation_shadow = None
        self._runtime._speaker_installation_pending = None
        self._runtime._speaker_installation_health.clear()
        self._runtime._speaker_verifier_activation_generation = None
        self._runtime._speaker_verifier_enforces_admission = False
        receipt = self._runtime._speaker_verifier_install_receipt
        if receipt is not None:
            self._runtime._speaker_verifier_install_receipt = replace(
                receipt, outcome=Outcome.REVOKED
            )
        # Existing reducer terminals remain sticky. Its ordered unavailable path
        # settles nonterminal sentences when the async reconciliation follows.
        for owner in self._runtime._asr_admission_capabilities.values():
            owner.revoked = True
        for ledger in tuple(self._runtime._asr_provider_speaker_ledgers.values()):
            self._runtime._poison_provider_speaker_ledger(ledger, "installation_retired")
        for transaction in tuple(self._runtime._asr_provider_exact_intervals.values()):
            if transaction.resolved_disposition is None:
                transaction.queue_poisoned = True
                self._runtime._schedule_exact_interval_event(
                    transaction,
                    SpeakerLeaseUnavailable(transaction.target_candidate, 1),
                )
        if self._runtime._asr_admission_ingress_started:
            exact_turns = {
                transaction.turn_token
                for transaction in self._runtime._asr_provider_exact_intervals.values()
            }
            events = [
                (turn, SpeakerAuthorityUnavailable(candidate))
                for candidate, turn in tuple(
                    self._runtime._asr_admission_candidate_turns.items()
                )
                if turn not in exact_turns
            ] + [
                (turn, SpeakerAuthorityUnarmed(generation))
                for turn, generation in tuple(
                    self._runtime._asr_speaker_authority_pending_turns.items()
                )
                if turn not in exact_turns
            ]
            for turn, event in events:
                try:
                    future = self._runtime._asr_admission_ingress.post_nowait(turn, event)
                except (AdmissionIngressClosedError, AdmissionIngressCapacityError):
                    continue
                self._runtime._consume_admission_future(turn, future)

    def _accept_speaker_verifier_health(
        self, event: SpeakerVerifierHealthEvent
    ) -> None:
        self._ensure_speaker_installation_state()
        if event.identity not in (
            self._runtime._speaker_installation_identity,
            self._runtime._speaker_installation_pending,
        ) or not self._speaker_install_identity_current(event.identity):
            self._count_speaker_installation("stale_health_event")
            return
        previous = self._runtime._speaker_installation_health.get(event.identity)
        if previous is not None and event.health_revision <= previous.health_revision:
            if (
                event.health_revision == previous.health_revision
                and event.causes != previous.causes
            ):
                self._count_speaker_installation("health_revision_conflict")
                self._runtime._speaker_installation_health[event.identity] = replace(
                    previous, causes=previous.causes | {"health_revision_conflict"}
                )
                self._runtime._speaker_verifier_degraded = True
            return
        self._runtime._speaker_installation_health[event.identity] = event
        if event.identity == self._runtime._speaker_installation_identity:
            self._runtime._speaker_verifier_degraded = bool(event.causes)
            receipt = self._runtime._speaker_verifier_install_receipt
            if receipt is not None:
                self._runtime._speaker_verifier_install_receipt = replace(
                    receipt, health_revision=event.health_revision
                )

    def _speaker_exact_installation_is_current(self, transaction) -> bool:
        if transaction.queue_poisoned:
            return False
        spec = getattr(self._runtime, "_speaker_installation_spec", None)
        if spec is None:
            return True  # Legacy explicit observer, no typed installation.
        identity = self._runtime._speaker_installation_identity
        return (
            identity is not None
            and self.speaker_verifier_installation_permits_evidence(identity)
        )

    def _own_speaker_cleanup(self, task) -> None:
        self._runtime._speaker_retired_cleanup.add(task)

        def settled(done):
            if done.cancelled():
                return  # Cancellation is not physical-close proof.
            if done.exception() is None:
                self._runtime._speaker_retired_cleanup.discard(done)
            else:
                self._count_speaker_installation("retired_cleanup_failed")

        task.add_done_callback(settled)

    async def _close_unadopted_speaker(self, shadow) -> None:
        self._count_speaker_installation("unadopted_shadow_cleanup")
        task = asyncio.create_task(shadow.close(), name="speaker-unadopted-cleanup")
        self._own_speaker_cleanup(task)
        done, _ = await asyncio.wait({task}, timeout=1.0)
        if not done:
            self._count_speaker_installation("retired_cleanup_timeout")

    async def install_speaker_verifier(
        self, spec: SpeakerVerifierSpec | None, identity: SpeakerVerifierInstallIdentity
    ) -> SpeakerVerifierInstallReceipt:
        self._runtime._ensure_asr_runtime_state()
        self._ensure_speaker_installation_state()
        if spec is None:
            spec = SpeakerVerifierSpec(
                None,
                identity.activation_revision,
                False,
                False,
                SpeakerVerifierAuthority(),
                None,
            )
        async with self._runtime._speaker_verifier_lock:
            receipt = await self._install_speaker_verifier_locked(spec, identity)
            # The inner finally may have retained a timed-out unadopted close.
            # Report that after cleanup settlement, not before entering finally.
            receipt = replace(
                receipt,
                cleanup_pending=receipt.cleanup_pending or bool(self._runtime._speaker_retired_cleanup),
            )
            actual = self._runtime._speaker_verifier_install_receipt
            if actual is not None and actual.identity == receipt.identity:
                self._runtime._speaker_verifier_install_receipt = receipt
            return receipt

    async def _install_speaker_verifier_locked(self, spec, identity):
        self._count_speaker_installation("install_requested")
        if (
            identity.activation_revision != spec.activation_revision
            or not self._speaker_install_identity_current(identity)
        ):
            self._count_speaker_installation("install_stale")
            return SpeakerVerifierInstallReceipt(identity, Outcome.STALE)
        detector = self._runtime._asr_detector
        enabled = spec.requested_enabled and spec.factory_builder is not None
        if (
            enabled
            and spec.revocable_authority.state is SpeakerVerifierAuthorityState.REVOKED
        ):
            self._count_speaker_installation("install_revoked")
            return SpeakerVerifierInstallReceipt(identity, Outcome.REVOKED)
        if enabled and detector is None:
            self.retire_speaker_verifier_authority()
            self._count_speaker_installation("install_deferred")
            return SpeakerVerifierInstallReceipt(identity, Outcome.DEFERRED_ROUTE)
        if enabled and spec.enforce and not self._runtime._speaker_verifier_route_supported():
            self.retire_speaker_verifier_authority()
            self._count_speaker_installation("install_unsupported")
            return SpeakerVerifierInstallReceipt(identity, Outcome.UNSUPPORTED_ROUTE)
        if enabled and len(self._runtime._speaker_retired_cleanup) >= 2:
            self.retire_speaker_verifier_authority()
            self._count_speaker_installation("retired_cleanup_capacity")
            self._runtime._speaker_verifier_degraded = True
            return SpeakerVerifierInstallReceipt(
                identity, Outcome.FAILED, cleanup_pending=True
            )

        self.retire_speaker_verifier_authority()
        serial = self._runtime._speaker_installation_serial
        self._runtime._speaker_installation_pending = identity
        self._runtime._speaker_installation_health[identity] = SpeakerVerifierHealthEvent(
            identity, 0
        )
        old_factory = self._runtime._speaker_verifier_factory
        self._runtime._speaker_verifier_factory = None
        factory = None
        shadow = None
        operation = SpeakerVerifierReplacementOperation(identity)
        committed = False

        def current():
            return (
                serial == self._runtime._speaker_installation_serial
                and self._runtime._speaker_installation_pending == identity
                and self._speaker_install_identity_current(identity)
                and (
                    not enabled
                    or spec.revocable_authority.state
                    is not SpeakerVerifierAuthorityState.REVOKED
                )
            )

        try:
            if enabled:
                factory = spec.factory_builder(self._runtime, identity)
                bind = getattr(factory, "bind_installation", None)
                if callable(bind):
                    bind(identity, spec.revocable_authority)
                shadow = factory()
                if shadow is None:
                    raise RuntimeError("speaker factory returned no observer")
            await self._runtime._revoke_runtime_speaker_authority_for_verifier_change()
            if not current():
                return SpeakerVerifierInstallReceipt(identity, Outcome.STALE)
            if detector is not None:
                await detector.replace_speaker_verifier(
                    shadow,
                    owner_generation=identity.installation_id,
                    operation=operation,
                )
            elif not enabled:
                operation.outcome = Outcome.REVOKED
                operation.ownership_state = Ownership.CLOSED
            if not current() or (
                enabled
                and (
                    operation.outcome is not Outcome.INSTALLED
                    or operation.ownership_state is not Ownership.DETECTOR
                    or getattr(detector, "_speaker_shadow", None) is not shadow
                )
            ):
                return SpeakerVerifierInstallReceipt(
                    identity,
                    Outcome.STALE,
                    operation.ownership_state,
                    cleanup_pending=operation.cleanup_pending,
                )
            health = self._runtime._speaker_installation_health[identity]
            receipt = SpeakerVerifierInstallReceipt(
                identity,
                Outcome.INSTALLED if enabled else Outcome.REVOKED,
                operation.ownership_state,
                health.health_revision,
                operation.cleanup_pending,
            )
            # Linearization point: no await between ownership proof and snapshot.
            self._runtime._speaker_installation_identity = identity if enabled else None
            self._runtime._speaker_installation_shadow = shadow if enabled else None
            self._runtime._speaker_installation_spec = spec
            self._runtime._speaker_verifier_activation_generation = (
                identity.installation_id if enabled else None
            )
            self._runtime._speaker_verifier_factory = factory
            self._runtime._speaker_verifier_enforces_admission = enabled and spec.enforce
            self._runtime._speaker_verifier_degraded = bool(health.causes)
            self._runtime._speaker_verifier_install_receipt = receipt
            committed = True
            self._count_speaker_installation("installed" if enabled else "revoked")
            return receipt
        except asyncio.CancelledError:
            self._count_speaker_installation("install_cancelled")
            raise
        except Exception:
            self._count_speaker_installation("install_failed")
            if serial == self._runtime._speaker_installation_serial:
                self._runtime._speaker_verifier_degraded = True
            return SpeakerVerifierInstallReceipt(
                identity,
                Outcome.FAILED,
                operation.ownership_state,
                cleanup_pending=operation.cleanup_pending,
            )
        finally:
            for task in operation.cleanup_tasks:
                self._own_speaker_cleanup(task)
            if not committed:
                if serial == self._runtime._speaker_installation_serial:
                    self.retire_speaker_verifier_authority()
                if (
                    shadow is not None
                    and operation.ownership_state is Ownership.OPERATION
                ):
                    # The install owner, not a later mutable Runtime field, closes it.
                    cleanup = asyncio.create_task(self._close_unadopted_speaker(shadow))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        self._own_speaker_cleanup(cleanup)
                if factory is not None:
                    self._runtime._close_speaker_verifier_factory(factory)
            if old_factory is not None and old_factory is not factory:
                self._runtime._close_speaker_verifier_factory(old_factory)
            if self._runtime._speaker_installation_pending == identity:
                self._runtime._speaker_installation_pending = None
