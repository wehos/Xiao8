from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pytest

from main_logic.asr_client.admission.contracts import (
    CaptureClosed,
    SpeakerLow,
    SpeakerUnavailable,
)
from main_logic.asr_client.speaker_shadow.asset_manifest import (
    CAMPPLUS_MODEL_ID,
    CAMPPLUS_MODEL_REVISION,
)
from main_logic.asr_client.speaker_shadow.campplus import CAMPPLUS_EMBEDDING_DIM
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCandidateKey,
    SpeakerShadowCompletion,
    SpeakerShadowObservation,
)
from main_logic.voice_identity.contracts import SpeakerModelIdentity
from main_logic.voice_identity.profile import SpeakerProfile
from main_logic.voice_identity.reference import SpeakerReference
from main_logic.voice_identity_service.asr_composition import (
    OwnerVoiceAsrCompositionFactory,
)


@dataclass
class _EvidenceSink:
    events: list[SpeakerLow | SpeakerUnavailable | CaptureClosed] = field(
        default_factory=list
    )
    degraded_generations: list[str] = field(default_factory=list)
    healthy_generations: list[str] = field(default_factory=list)

    def _accept_speaker_evidence_fact(
        self,
        fact: SpeakerLow | SpeakerUnavailable,
        *,
        activation_generation: str,
        enforce: bool,
    ) -> bool:
        assert activation_generation == "activation-1"
        assert enforce is True
        self.events.append(fact)
        return True

    def _close_speaker_evidence(
        self,
        closed: CaptureClosed,
        *,
        activation_generation: str,
        enforce: bool,
        evidence_complete: bool,
    ) -> bool:
        assert activation_generation == "activation-1"
        assert enforce is True
        self.events.append(closed)
        return True

    def _mark_speaker_evidence_backend_degraded(
        self,
        *,
        activation_generation: str,
    ) -> None:
        self.degraded_generations.append(activation_generation)

    def _mark_speaker_evidence_backend_healthy(
        self,
        *,
        activation_generation: str,
    ) -> None:
        self.healthy_generations.append(activation_generation)


def _profile(identity: SpeakerModelIdentity | None = None) -> SpeakerProfile:
    identity = identity or SpeakerModelIdentity(
        CAMPPLUS_MODEL_ID, CAMPPLUS_MODEL_REVISION, CAMPPLUS_EMBEDDING_DIM
    )
    embedding = np.ones(identity.embedding_dimension, dtype=np.float32)
    reference = SpeakerReference(identity, embedding)
    embedding.fill(0.0)
    try:
        return SpeakerProfile("profile-generation", reference)
    finally:
        reference.close()


@pytest.mark.parametrize("iteration", range(50))
def test_shadow_constructor_failure_closes_unadopted_backend_factory(monkeypatch, iteration):
    import main_logic.voice_identity_service.asr_composition as module

    created = []
    original = module.CampPlusBackendFactory

    def backend_factory(embedding):
        backend = original(embedding)
        created.append(backend)
        return backend

    def broken_shadow(**kwargs):
        raise RuntimeError("injected observer construction failure")

    monkeypatch.setattr(module, "CampPlusBackendFactory", backend_factory)
    monkeypatch.setattr(module, "SpeakerShadowRuntime", broken_shadow)
    profile = _profile()
    factory = module.OwnerVoiceAsrCompositionFactory(
        _EvidenceSink(), profile, activation_generation="activation-1", enforce=True
    )
    try:
        with pytest.raises(RuntimeError, match="injected observer"):
            factory()
        assert len(created) == 1 and created[0]._closed
    finally:
        factory.close()
        profile.close()


def test_composition_emits_stateless_ordered_low_facts_then_close() -> None:
    sink = _EvidenceSink()
    profile = _profile()
    factory = OwnerVoiceAsrCompositionFactory(
        sink,
        profile,
        activation_generation="activation-1",
        enforce=True,
    )
    assert factory.enforces_admission is True
    shadow = factory()
    candidate = SpeakerShadowCandidateKey(1, 2, "provider_candidate")
    callback = shadow._on_evidence
    assert callback is not None

    callback(
        SpeakerShadowObservation(
            candidate,
            0.2,
            ((0.4, True),),
            1_500,
            1_500,
            sequence_no=1,
        )
    )
    callback(
        SpeakerShadowObservation(
            candidate,
            0.2,
            ((0.4, True),),
            3_000,
            3_000,
            sequence_no=2,
        )
    )
    callback(SpeakerShadowCompletion(candidate, "scored", 3_000, 2, True))

    assert [type(event) for event in sink.events] == [
        SpeakerLow,
        SpeakerLow,
        CaptureClosed,
    ]
    assert [event.sequence_no for event in sink.events[:2]] == [1, 2]
    assert sink.events[-1] == CaptureClosed(candidate, 2)
    diagnostics = factory.diagnostics_snapshot()
    assert diagnostics["speaker_first_low_count"] == 1
    assert diagnostics["speaker_second_low_count"] == 1
    assert "reject_decision_count" not in diagnostics
    factory.close()


def test_incomplete_close_emits_unavailable_before_close() -> None:
    sink = _EvidenceSink()
    profile = _profile()
    factory = OwnerVoiceAsrCompositionFactory(
        sink,
        profile,
        activation_generation="activation-1",
        enforce=True,
    )
    shadow = factory()
    candidate = SpeakerShadowCandidateKey(1, 3, "provider_candidate")
    callback = shadow._on_evidence
    assert callback is not None

    callback(SpeakerShadowCompletion(candidate, "failed", None, 1, False))

    assert sink.events == [
        SpeakerUnavailable(candidate, 1),
        CaptureClosed(candidate, 1),
    ]
    factory.close()


async def test_factory_close_wipes_owned_profile_and_backend_material() -> None:
    sink = _EvidenceSink()
    profile = _profile()
    source_embedding = profile._reference._embedding
    factory = OwnerVoiceAsrCompositionFactory(
        sink,
        profile,
        activation_generation="activation-1",
        enforce=True,
    )
    factory_profile = factory._profile
    factory_profile_embedding = factory_profile._reference._embedding

    profile.close()
    assert profile.closed is True
    assert not np.any(source_embedding)
    assert np.any(factory_profile_embedding)

    shadow = factory()
    backend_factory = shadow._backend_factory
    assert backend_factory is not None
    backend_storage = backend_factory._reference._storage
    assert any(backend_storage)

    factory.close()
    factory.close()
    assert factory_profile.closed is True
    assert not np.any(factory_profile_embedding)
    with pytest.raises(RuntimeError, match="factory is closed"):
        factory()

    await shadow.close()
    assert not any(backend_storage)


async def test_backend_health_uses_generation_scoped_evidence_sink() -> None:
    sink = _EvidenceSink()
    profile = _profile()
    factory = OwnerVoiceAsrCompositionFactory(
        sink,
        profile,
        activation_generation="activation-1",
        enforce=True,
    )
    shadow = factory()

    shadow._mark_backend_degraded()
    shadow._mark_backend_recovered()

    assert sink.degraded_generations == ["activation-1"]
    assert sink.healthy_generations == ["activation-1"]
    await shadow.close()
    factory.close()
    profile.close()


def test_wrong_model_identity_wipes_temporary_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(
        SpeakerModelIdentity(
            "wrong-model",
            "wrong-revision",
            CAMPPLUS_EMBEDDING_DIM,
        )
    )
    sink = _EvidenceSink()
    captured_references: list[SpeakerReference] = []
    captured_embeddings: list[np.ndarray] = []
    original_clone: Callable[[SpeakerProfile], SpeakerReference] = (
        SpeakerProfile.clone_reference
    )

    def capture_clone(owner: SpeakerProfile) -> SpeakerReference:
        reference = original_clone(owner)
        captured_references.append(reference)
        captured_embeddings.append(reference._embedding)
        return reference

    monkeypatch.setattr(SpeakerProfile, "clone_reference", capture_clone)
    factory = OwnerVoiceAsrCompositionFactory(
        sink,
        profile,
        activation_generation="activation-1",
        enforce=True,
    )

    with pytest.raises(ValueError, match=r"model identity does not match CAM\+\+"):
        factory()

    assert len(captured_references) == 1
    assert captured_references[0].closed is True
    assert not np.any(captured_embeddings[0])
    factory.close()
    profile.close()
