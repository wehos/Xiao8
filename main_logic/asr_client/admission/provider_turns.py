"""Pure Provider-key correlation, separate from audio ownership execution."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal, TypeAlias

from main_logic.voice_turn.contracts import VoiceIngressToken, VoiceTurnToken

from .._provider_events import ProviderAudioRange, ProviderUtteranceKey
from .contracts import AdmissionResolutionTicket, BoundaryProof, PendingProviderFinal


BoundaryResultQuality: TypeAlias = Literal["exact", "unknown"]


@dataclass(frozen=True, slots=True)
class ProviderBoundaryResult:
    quality: BoundaryResultQuality
    audio_range: ProviderAudioRange | None
    proof: BoundaryProof | None

    def __post_init__(self) -> None:
        if self.quality == "exact":
            if self.audio_range is None or self.proof is None:
                raise ValueError("exact boundary requires range and proof")
        elif self.quality == "unknown":
            if self.audio_range is not None or self.proof is not None:
                raise ValueError("unknown boundary cannot carry authority")
        else:
            raise ValueError("unsupported boundary quality")

    @classmethod
    def unknown(cls) -> "ProviderBoundaryResult":
        return cls(quality="unknown", audio_range=None, proof=None)


@dataclass(slots=True)
class ProviderAliasRecord:
    provider_key: ProviderUtteranceKey
    boundary_result: ProviderBoundaryResult | None = None
    ordered_seen: bool = False
    bound_turn_token: VoiceTurnToken | None = None
    pending_final: PendingProviderFinal | None = None


class ProviderAliasConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderAliasCompletionResult:
    completed: bool
    retired_proofs: tuple[BoundaryProof, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderBoundaryRecordResult:
    """Normalized boundary authority plus every proof the caller must retire."""

    boundary_result: ProviderBoundaryResult
    retired_proofs: tuple[BoundaryProof, ...] = ()

    @property
    def quality(self) -> BoundaryResultQuality:
        return self.boundary_result.quality

    @property
    def audio_range(self) -> ProviderAudioRange | None:
        return self.boundary_result.audio_range

    @property
    def proof(self) -> BoundaryProof | None:
        return self.boundary_result.proof


@dataclass(frozen=True, slots=True)
class ProviderAliasRetirementResult:
    retired: bool
    provider_keys: tuple[ProviderUtteranceKey, ...] = ()
    bound_turn_tokens: tuple[VoiceTurnToken, ...] = ()
    retired_proofs: tuple[BoundaryProof, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderNamespaceRetirementResult:
    namespace: tuple[int, int]
    retired: bool
    provider_keys: tuple[ProviderUtteranceKey, ...] = ()
    bound_turn_tokens: tuple[VoiceTurnToken, ...] = ()
    retired_proofs: tuple[BoundaryProof, ...] = ()


class ProviderTurnCorrelator:
    """Bind aliases only in the ordered lane; optional exact proof is bounded."""

    def __init__(
        self,
        *,
        namespace: tuple[int, int],
        proof_capacity: int = 8,
        completed_capacity: int = 256,
    ) -> None:
        if type(proof_capacity) is not int or proof_capacity <= 0:
            raise ValueError("proof_capacity must be a positive integer")
        if type(completed_capacity) is not int or completed_capacity <= 0:
            raise ValueError("completed_capacity must be a positive integer")
        if (
            type(namespace) is not tuple
            or len(namespace) != 2
            or any(type(value) is not int or value < 0 for value in namespace)
        ):
            raise ValueError("namespace must be a non-negative generation/epoch pair")
        self._proof_capacity = proof_capacity
        self._completed_capacity = completed_capacity
        self._records: dict[ProviderUtteranceKey, ProviderAliasRecord] = {}
        self._token_bindings: dict[VoiceTurnToken, ProviderUtteranceKey] = {}
        self._exact_proofs: OrderedDict[
            ProviderUtteranceKey,
            ProviderBoundaryResult,
        ] = OrderedDict()
        self._completed: OrderedDict[ProviderUtteranceKey, None] = OrderedDict()
        self._namespace = namespace
        self._namespace_retired = False
        self._completed_high_water = 0
        self._retired_turn_high_water: dict[VoiceIngressToken, int] = {}

    @property
    def completed_tombstone_count(self) -> int:
        return len(self._completed)

    def is_completed(self, key: ProviderUtteranceKey) -> bool:
        if type(key) is not ProviderUtteranceKey:
            raise TypeError("key must be ProviderUtteranceKey")
        return bool(
            (self._namespace_retired and self._accept_namespace(key))
            or key in self._completed
            or (
                self._namespace == (key.generation, key.buffer_epoch)
                and key.utterance_id <= self._completed_high_water
            )
        )

    def _accept_namespace(self, key: ProviderUtteranceKey) -> bool:
        namespace = (key.generation, key.buffer_epoch)
        return namespace == self._namespace

    def record_for(self, key: ProviderUtteranceKey) -> ProviderAliasRecord | None:
        if type(key) is not ProviderUtteranceKey:
            raise TypeError("key must be ProviderUtteranceKey")
        return self._records.get(key)

    def _record(self, key: ProviderUtteranceKey) -> ProviderAliasRecord:
        record = self._records.get(key)
        if record is None:
            record = ProviderAliasRecord(provider_key=key)
            self._records[key] = record
        return record

    def record_boundary_result(
        self,
        key: ProviderUtteranceKey,
        result: ProviderBoundaryResult,
    ) -> ProviderBoundaryRecordResult:
        """Record ownership only; this API deliberately accepts no turn token."""

        if type(key) is not ProviderUtteranceKey:
            raise TypeError("key must be ProviderUtteranceKey")
        if type(result) is not ProviderBoundaryResult:
            raise TypeError("result must be ProviderBoundaryResult")
        if not self._accept_namespace(key) or self.is_completed(key):
            retired = (result.proof,) if result.proof is not None else ()
            return ProviderBoundaryRecordResult(
                ProviderBoundaryResult.unknown(),
                retired,
            )
        record = self._records.get(key)
        if (
            result.quality == "exact"
            and result.proof is not None
            and result.proof.provider_key != key
        ):
            retired = (result.proof,)
            result = ProviderBoundaryResult.unknown()
        else:
            retired = ()
        if record is None and result.quality == "unknown":
            return ProviderBoundaryRecordResult(result, retired)
        if (
            record is None
            and result.quality == "exact"
            and len(self._exact_proofs) >= self._proof_capacity
        ):
            assert result.proof is not None
            return ProviderBoundaryRecordResult(
                ProviderBoundaryResult.unknown(),
                _unique_proofs(retired, (result.proof,)),
            )
        if record is None:
            record = self._record(key)
        existing = record.boundary_result
        if existing is not None:
            if existing == result:
                return ProviderBoundaryRecordResult(existing, retired)
            # Authority never recovers after a duplicate/conflicting boundary.
            self._exact_proofs.pop(key, None)
            record.boundary_result = ProviderBoundaryResult.unknown()
            retired = _unique_proofs(
                retired,
                (existing.proof,) if existing.proof is not None else (),
                (result.proof,) if result.proof is not None else (),
            )
            return ProviderBoundaryRecordResult(record.boundary_result, retired)
        if result.quality == "exact" and len(self._exact_proofs) >= self._proof_capacity:
            record.boundary_result = ProviderBoundaryResult.unknown()
            assert result.proof is not None
            return ProviderBoundaryRecordResult(
                record.boundary_result,
                _unique_proofs(retired, (result.proof,)),
            )
        record.boundary_result = result
        if result.quality == "exact":
            self._exact_proofs[key] = result
        return ProviderBoundaryRecordResult(record.boundary_result, retired)

    def mark_ordered(self, key: ProviderUtteranceKey) -> ProviderAliasRecord:
        if type(key) is not ProviderUtteranceKey:
            raise TypeError("key must be ProviderUtteranceKey")
        if not self._accept_namespace(key):
            raise ProviderAliasConflictError("PROVIDER_NAMESPACE_MISMATCH")
        if self.is_completed(key):
            raise ProviderAliasConflictError("PROVIDER_KEY_ALREADY_COMPLETED")
        record = self._record(key)
        record.ordered_seen = True
        return record

    def bind_ordered(
        self,
        key: ProviderUtteranceKey,
        turn_token: VoiceTurnToken,
    ) -> ProviderAliasRecord:
        if type(key) is not ProviderUtteranceKey:
            raise TypeError("key must be ProviderUtteranceKey")
        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        if not self._accept_namespace(key):
            raise ProviderAliasConflictError("PROVIDER_NAMESPACE_MISMATCH")
        if self.is_completed(key):
            raise ProviderAliasConflictError("PROVIDER_KEY_ALREADY_COMPLETED")
        record = self._records.get(key)
        if record is None or not record.ordered_seen:
            raise ProviderAliasConflictError("PROVIDER_ALIAS_BIND_REQUIRES_ORDERED")
        if record.bound_turn_token not in {None, turn_token}:
            raise ProviderAliasConflictError("PROVIDER_KEY_ALREADY_BOUND")
        existing_key = self._token_bindings.get(turn_token)
        if turn_token.turn_id <= self._retired_turn_high_water.get(
            turn_token.ingress,
            0,
        ):
            raise ProviderAliasConflictError("VOICE_TURN_ALREADY_BOUND")
        if existing_key not in {None, key}:
            raise ProviderAliasConflictError("VOICE_TURN_ALREADY_BOUND")
        record.bound_turn_token = turn_token
        self._token_bindings[turn_token] = key
        return record

    def record_final(
        self,
        key: ProviderUtteranceKey,
        final: PendingProviderFinal,
    ) -> ProviderAliasRecord:
        if type(key) is not ProviderUtteranceKey:
            raise TypeError("key must be ProviderUtteranceKey")
        if type(final) is not PendingProviderFinal:
            raise TypeError("final must be PendingProviderFinal")
        if not self._accept_namespace(key):
            raise ProviderAliasConflictError("PROVIDER_NAMESPACE_MISMATCH")
        if self.is_completed(key):
            raise ProviderAliasConflictError("PROVIDER_KEY_ALREADY_COMPLETED")
        record = self._records.get(key)
        if record is None or not record.ordered_seen:
            raise ProviderAliasConflictError("PROVIDER_FINAL_REQUIRES_ORDERED")
        if final.provider_key != key:
            raise ProviderAliasConflictError("PROVIDER_FINAL_KEY_MISMATCH")
        if record.pending_final is None:
            record.pending_final = final
        elif record.pending_final != final:
            raise ProviderAliasConflictError("PROVIDER_FINAL_CONFLICT")
        return record

    def complete(
        self,
        key: ProviderUtteranceKey,
        resolution: AdmissionResolutionTicket,
    ) -> ProviderAliasCompletionResult:
        if type(key) is not ProviderUtteranceKey:
            raise TypeError("key must be ProviderUtteranceKey")
        if type(resolution) is not AdmissionResolutionTicket:
            raise TypeError("resolution must be AdmissionResolutionTicket")
        record = self._records.get(key)
        if (
            record is None
            or record.pending_final is None
            or record.bound_turn_token is None
            or resolution.turn_token != record.bound_turn_token
            or any(
                other.provider_key.utterance_id < key.utterance_id
                for other in self._records.values()
                if other is not record and other.ordered_seen
            )
        ):
            return ProviderAliasCompletionResult(False)
        retired_keys = tuple(
            candidate_key
            for candidate_key in self._records
            if candidate_key.utterance_id <= key.utterance_id
        )
        retired = self._retire_keys(retired_keys)
        return ProviderAliasCompletionResult(True, retired.retired_proofs)

    def abandon_turn(
        self,
        turn_token: VoiceTurnToken,
    ) -> ProviderAliasRetirementResult:
        """Retire an ACTIVE-DROP alias even when no Provider final can arrive."""

        if type(turn_token) is not VoiceTurnToken:
            raise TypeError("turn_token must be VoiceTurnToken")
        key = self._token_bindings.get(turn_token)
        if key is None:
            return ProviderAliasRetirementResult(False)
        if any(
            other.provider_key.utterance_id < key.utterance_id
            for other in self._records.values()
            if other.provider_key != key and other.ordered_seen
        ):
            return ProviderAliasRetirementResult(False)
        retired_keys = tuple(
            candidate_key
            for candidate_key in self._records
            if candidate_key.utterance_id <= key.utterance_id
        )
        return self._retire_keys(retired_keys)

    def retire_namespace(
        self,
        namespace: tuple[int, int],
    ) -> ProviderNamespaceRetirementResult:
        """Fence a complete Provider timeline and return all cleanup ownership."""

        if (
            type(namespace) is not tuple
            or len(namespace) != 2
            or any(type(value) is not int or value < 0 for value in namespace)
        ):
            raise ValueError("namespace must be a non-negative generation/epoch pair")
        if namespace != self._namespace:
            raise ProviderAliasConflictError("PROVIDER_NAMESPACE_MISMATCH")
        if self._namespace_retired:
            return ProviderNamespaceRetirementResult(namespace, False)
        retired = self._retire_keys(tuple(self._records))
        self._namespace_retired = True
        self._completed.clear()
        self._retired_turn_high_water.clear()
        return ProviderNamespaceRetirementResult(
            namespace=namespace,
            retired=True,
            provider_keys=retired.provider_keys,
            bound_turn_tokens=retired.bound_turn_tokens,
            retired_proofs=retired.retired_proofs,
        )

    def _retire_keys(
        self,
        keys: tuple[ProviderUtteranceKey, ...],
    ) -> ProviderAliasRetirementResult:
        retired_keys: list[ProviderUtteranceKey] = []
        retired_tokens: list[VoiceTurnToken] = []
        retired_proofs: list[BoundaryProof] = []
        for key in sorted(keys, key=lambda candidate: candidate.utterance_id):
            record = self._records.pop(key, None)
            if record is None:
                continue
            retired_keys.append(key)
            proof_result = self._exact_proofs.pop(key, None)
            if proof_result is not None and proof_result.proof is not None:
                retired_proofs.append(proof_result.proof)
            token = record.bound_turn_token
            if token is not None:
                retired_tokens.append(token)
                self._retired_turn_high_water[token.ingress] = max(
                    self._retired_turn_high_water.get(token.ingress, 0),
                    token.turn_id,
                )
                if self._token_bindings.get(token) == key:
                    self._token_bindings.pop(token, None)
            self._remember_retired_key(key)
        return ProviderAliasRetirementResult(
            retired=bool(retired_keys),
            provider_keys=tuple(retired_keys),
            bound_turn_tokens=tuple(retired_tokens),
            retired_proofs=_unique_proofs(tuple(retired_proofs)),
        )

    def _remember_retired_key(self, key: ProviderUtteranceKey) -> None:
        self._completed[key] = None
        self._completed_high_water = max(
            self._completed_high_water,
            key.utterance_id,
        )
        while len(self._completed) > self._completed_capacity:
            self._completed.popitem(last=False)


def _unique_proofs(
    *groups: tuple[BoundaryProof, ...],
) -> tuple[BoundaryProof, ...]:
    seen: set[BoundaryProof] = set()
    ordered: list[BoundaryProof] = []
    for group in groups:
        for proof in group:
            if proof not in seen:
                seen.add(proof)
                ordered.append(proof)
    return tuple(ordered)


__all__ = [
    "ProviderAliasConflictError",
    "ProviderAliasCompletionResult",
    "ProviderAliasRecord",
    "ProviderAliasRetirementResult",
    "ProviderBoundaryRecordResult",
    "ProviderBoundaryResult",
    "ProviderNamespaceRetirementResult",
    "ProviderTurnCorrelator",
]
