"""Bounded metadata observation, sharing the caller's active-key budget.

The registry owns neither audio nor final delivery. Callers register only keys
already held in Admission and retire them with that record; the local bound
does not reserve additional Admission slots. Each registry belongs to one
session/transport/timeline. Callers supply already accounted range references.
No callback, task or executable capability is
accepted, so observational failures cannot trigger rejection or I/O.
"""

from collections import Counter, OrderedDict

from .._provider_events import ProviderUtteranceKey
from .contracts import (
    EvidenceMode, EvidenceProof, ProviderEvidenceBinding, ScoreEvidence, ContinuityEvidence,
)
from .coverage import evaluate_coverage


class EvidenceObservationRegistry:
    def __init__(self, *, active_key_capacity: int = 8, retired_capacity: int = 32) -> None:
        if type(active_key_capacity) is not int or not 1 <= active_key_capacity <= 8:
            raise ValueError("observational keys must fit the existing admission capacity")
        if type(retired_capacity) is not int or not 1 <= retired_capacity <= 1024:
            raise ValueError("invalid bounded retirement window")
        self._capacity = active_key_capacity
        self._retired_capacity = retired_capacity
        self._active: dict[ProviderUtteranceKey, EvidenceProof] = {}
        self._retired: OrderedDict[ProviderUtteranceKey, None] = OrderedDict()
        self._retired_high_water: tuple[int, int, int] | None = None
        self._counts: Counter[str] = Counter()
        self._timeline: tuple[int, int, int] | None = None

    def observe(
        self, binding: ProviderEvidenceBinding,
        scores: tuple[ScoreEvidence, ...] = (),
        continuity: tuple[ContinuityEvidence, ...] = (),
    ) -> EvidenceProof | None:
        key = binding.provider_key
        if self._timeline is None:
            self._timeline = binding.target_range.timeline
        elif self._timeline != binding.target_range.timeline:
            self._counts["stale_scope"] += 1
            return None
        order = (key.generation, key.buffer_epoch, key.utterance_id)
        if key in self._retired or (
            key not in self._active and self._retired_high_water is not None
            and order <= self._retired_high_water
        ):
            self._counts["stale"] += 1
            return None
        existing = self._active.get(key)
        if existing is not None and existing.binding != binding:
            self._counts["identity_conflict"] += 1
            return None
        if existing is None and len(self._active) >= self._capacity:
            self._counts["capacity_skipped"] += 1
            return None
        proof = evaluate_coverage(binding, scores, continuity, mode=EvidenceMode.OBSERVE)
        windows = {
            (score.window.window_id, score.window.revision)
            for item_key, item in self._active.items() if item_key != key
            for score in item.scores
        }
        windows.update(
            (item.binding.window.window_id, item.binding.window.revision)
            for item_key, item in self._active.items() if item_key != key
        )
        windows.add((binding.window.window_id, binding.window.revision))
        windows.update((score.window.window_id, score.window.revision) for score in scores)
        if len(windows) > 32:
            self._counts["window_capacity_skipped"] += 1
            return None
        self._active[key] = proof
        if proof != existing:
            self._counts[proof.reason] += 1
        return proof

    def retire(self, binding: ProviderEvidenceBinding) -> str:
        key = binding.provider_key
        existing = self._active.get(key)
        if existing is None:
            return "already_completed" if key in self._retired else "stale"
        if existing.binding != binding:
            return "stale"
        self._active.pop(key)
        self._retired[key] = None
        order = (key.generation, key.buffer_epoch, key.utterance_id)
        self._retired_high_water = max(self._retired_high_water or order, order)
        while len(self._retired) > self._retired_capacity:
            self._retired.popitem(last=False)
        return "completed"

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts, active=len(self._active), retired=len(self._retired))
