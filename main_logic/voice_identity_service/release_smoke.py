"""Frozen Windows release smoke for the local Owner voice identity stack."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import tempfile

import numpy as np

from main_logic.asr_client.speaker_shadow.asset_manifest import (
    CAMPPLUS_MODEL_ID,
    CAMPPLUS_MODEL_REVISION,
    CAMPPLUS_SAMPLE_RATE_HZ,
    resolve_verified_campplus_asset,
)
from main_logic.asr_client.speaker_shadow.campplus import (
    CAMPPLUS_EMBEDDING_DIM,
    CampPlusBackendFactory,
    CampPlusEmbeddingModel,
)
from main_logic.asr_client.speaker_shadow.contracts import (
    SpeakerShadowCandidateKey,
    SpeakerShadowConfig,
)
from main_logic.asr_client.speaker_shadow.runtime import SpeakerShadowRuntime
from main_logic.voice_identity.contracts import SpeakerModelIdentity
from main_logic.voice_identity.profile import SpeakerProfile
from main_logic.voice_identity.reference import SpeakerReference

from .audio_contract import desktop_audio_contract_snapshot
from .profile_store import VoiceIdentityProfileStore


RELEASE_SMOKE_ENV = "NEKO_VOICE_IDENTITY_RELEASE_SMOKE"
RELEASE_SMOKE_SUCCESS_MARKER = "NEKO_VOICE_IDENTITY_RELEASE_SMOKE_OK"
_OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}


def _fixed_pcm16() -> bytes:
    """Create deterministic, non-private audio without reading a microphone."""

    sample_count = CAMPPLUS_SAMPLE_RATE_HZ * 3
    samples = np.arange(sample_count, dtype=np.float64)
    waveform = 0.18 * np.sin(
        2 * np.pi * 173 * samples / CAMPPLUS_SAMPLE_RATE_HZ
    ) + 0.07 * np.sin(2 * np.pi * 641 * samples / CAMPPLUS_SAMPLE_RATE_HZ)
    pcm = np.rint(waveform * 32_767).astype("<i2")
    try:
        return pcm.tobytes()
    finally:
        pcm.fill(0)
        waveform.fill(0)
        samples.fill(0)


def _assert_encrypted_envelope(path: Path) -> None:
    envelope = json.loads(path.read_text(encoding="ascii"))
    if set(envelope) != {
        "algorithm",
        "ciphertext",
        "key_wrapping",
        "nonce",
        "schema_version",
        "wrapped_key",
    }:
        raise RuntimeError("profile_envelope_invalid")
    encoded = path.read_bytes()
    if b"embedding" in encoded or CAMPPLUS_MODEL_ID.encode("utf-8") in encoded:
        raise RuntimeError("profile_contains_plaintext_biometric_material")


async def run_release_smoke() -> None:
    """Exercise the same frozen components used by enrollment and filtering."""

    if os.name != "nt":
        raise RuntimeError("release_smoke_requires_windows")
    for name, value in _OFFLINE_ENVIRONMENT.items():
        os.environ[name] = value

    model_path = resolve_verified_campplus_asset()
    pcm16 = _fixed_pcm16()
    embedding: np.ndarray | None = None
    loaded_embedding: np.ndarray | None = None
    model = CampPlusEmbeddingModel(asset_dir=model_path.parent)
    profile: SpeakerProfile | None = None
    loaded_profile: SpeakerProfile | None = None
    reference: SpeakerReference | None = None
    runtime: SpeakerShadowRuntime | None = None
    observations = []
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        if not model.load():
            raise RuntimeError("campplus_model_unavailable")
        embedding = model.embedding_from_pcm16(
            pcm16,
            sample_rate_hz=CAMPPLUS_SAMPLE_RATE_HZ,
        )
        if embedding.shape != (CAMPPLUS_EMBEDDING_DIM,):
            raise RuntimeError("campplus_embedding_dimension_invalid")
        if not np.isfinite(embedding).all() or not math.isclose(
            float(np.linalg.norm(embedding)),
            1.0,
            abs_tol=1e-5,
        ):
            raise RuntimeError("campplus_embedding_invalid")

        identity = SpeakerModelIdentity(
            CAMPPLUS_MODEL_ID,
            CAMPPLUS_MODEL_REVISION,
            CAMPPLUS_EMBEDDING_DIM,
        )
        reference = SpeakerReference(identity, embedding)
        profile = SpeakerProfile("release-smoke", reference)
        reference.close()
        reference = None

        with tempfile.TemporaryDirectory(prefix="neko-voice-identity-smoke-") as temp:
            profile_path = Path(temp) / "voice_identity.profile"
            store = VoiceIdentityProfileStore(profile_path)
            await store.asave(
                profile,
                audio_contract=desktop_audio_contract_snapshot(
                    noise_reduction_enabled=True,
                ),
            )
            _assert_encrypted_envelope(profile_path)
            loaded = await store.aload()
            if loaded is None:
                raise RuntimeError("profile_roundtrip_missing")
            loaded_profile = loaded.profile
            loaded_reference = loaded_profile.clone_reference()
            try:
                loaded_embedding = loaded_reference.copy_embedding()
            finally:
                loaded_reference.close()
            if loaded_profile.generation != "release-smoke" or not np.allclose(
                embedding,
                loaded_embedding,
                atol=1e-6,
            ):
                raise RuntimeError("profile_roundtrip_mismatch")

        factory = CampPlusBackendFactory(
            loaded_embedding,
            asset_dir=model_path.parent,
        )

        async def observe(observation) -> None:
            observations.append(observation)

        runtime = SpeakerShadowRuntime(
            backend_factory=factory,
            config=SpeakerShadowConfig(
                enabled=True,
                similarity_thresholds=(0.40,),
                minimum_audio_ms=1_500,
                maximum_audio_ms=3_000,
                idle_unload_seconds=10.0,
            ),
            on_observation=observe,
        )
        candidate = SpeakerShadowCandidateKey(
            detector_epoch=1,
            shadow_generation=1,
            scope="provider_candidate",
        )
        if not runtime.submit(
            pcm16,
            sample_rate_hz=CAMPPLUS_SAMPLE_RATE_HZ,
            candidate=candidate,
        ):
            raise RuntimeError("speaker_shadow_submit_failed")
        if not runtime.finish_candidate(candidate):
            raise RuntimeError("speaker_shadow_finish_failed")
        await runtime.wait_idle()
        if len(observations) != 1:
            raise RuntimeError("speaker_shadow_observation_missing")
        if any(
            item.candidate != candidate
            or not math.isfinite(item.similarity)
            or item.similarity < 0.99
            for item in observations
        ):
            raise RuntimeError("speaker_shadow_score_invalid")
    except BaseException as exc:
        primary_error = exc
    finally:
        try:
            model.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        if runtime is not None:
            try:
                await runtime.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        for owned in (loaded_profile, profile, reference):
            if owned is None:
                continue
            try:
                owned.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        for owned_embedding in (loaded_embedding, embedding):
            if owned_embedding is None:
                continue
            try:
                owned_embedding.fill(0)
            except BaseException as exc:
                cleanup_errors.append(exc)
    if primary_error is not None:
        raise primary_error.with_traceback(primary_error.__traceback__)
    if cleanup_errors:
        cleanup_error = cleanup_errors[0]
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)


def main() -> int:
    try:
        asyncio.run(run_release_smoke())
    except BaseException as exc:
        print(
            f"NEKO_VOICE_IDENTITY_RELEASE_SMOKE_FAILED:{type(exc).__name__}", flush=True
        )
        return 1
    print(RELEASE_SMOKE_SUCCESS_MARKER, flush=True)
    return 0


__all__ = [
    "RELEASE_SMOKE_ENV",
    "RELEASE_SMOKE_SUCCESS_MARKER",
    "main",
    "run_release_smoke",
]
