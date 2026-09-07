#!/usr/bin/env python3
"""Compare enrollment and runtime audio domains without retaining biometrics."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from enum import IntEnum
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import wave
from typing import Any, Sequence

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


SOURCE_SAMPLE_RATE_HZ = 48_000
TARGET_SAMPLE_RATE_HZ = 16_000
TARGET_AUDIO_SAMPLES = TARGET_SAMPLE_RATE_HZ * 3
HOLDOUT_FIRST_SAMPLES = TARGET_SAMPLE_RATE_HZ * 3 // 2
THRESHOLD = 0.40
RUNTIME_CHUNK_SAMPLES = 480
MINIMUM_SPEAKER_COUNT = 3
MAXIMUM_CASE_COUNT = 64
MINIMUM_SOURCE_SAMPLES = SOURCE_SAMPLE_RATE_HZ * 31 // 10
MAXIMUM_SOURCE_SECONDS = 4
MAXIMUM_MANIFEST_BYTES = 1_000_000
REPORT_SCHEMA_VERSION = 2
NEAR_THRESHOLD_MARGIN = 0.05
WORKLET_RUNNER = PROJECT_ROOT / "scripts" / "_voice_enrollment_worklet_runner.cjs"
WORKLET_SOURCE = PROJECT_ROOT / "static" / "audio-processor.js"
REQUIRED_SCENARIOS = frozenset({"quiet", "steady-noise", "natural-pause"})
PROTECTED_PROJECT_ROOTS = tuple(
    PROJECT_ROOT / name
    for name in (
        "app",
        "main_logic",
        "main_routers",
        "scripts",
        "static",
        "templates",
        "utils",
    )
)
ALLOWED_PROJECT_REPORT_ROOTS = frozenset({"reports", "artifacts", ".artifacts"})
BASELINE_PATH = "browser_old_profile_to_runtime_holdout"
PRODUCTION_PATH = "server_normalized_profile_to_runtime_holdout"
CONTROL_PATH = "server_normalized_profile_to_same_path_holdout"


class ExitCode(IntEnum):
    PASS = 0
    BLOCK = 2
    CORPUS_UNAVAILABLE = 3
    CAMPPLUS_UNAVAILABLE = 4
    RUNTIME_PREPROCESSOR_UNAVAILABLE = 5
    BROWSER_RESAMPLER_UNAVAILABLE = 6
    INTERNAL_ERROR = 70


class _HarnessFailure(RuntimeError):
    def __init__(self, exit_code: ExitCode, verdict: str) -> None:
        self.exit_code = exit_code
        self.verdict = verdict
        super().__init__(verdict)


@dataclass(frozen=True, slots=True)
class _CorpusCase:
    speaker_id: str
    reference_paths: tuple[Path, Path, Path]
    holdout_path: Path


@dataclass(slots=True)
class _ProfileVectors:
    centroid: np.ndarray
    reference_scores: tuple[float, float, float]

    def wipe(self) -> None:
        _wipe_array(self.centroid)


@dataclass(slots=True)
class _CandidateVectors:
    first: np.ndarray
    full: np.ndarray

    def wipe(self) -> None:
        _wipe_array(self.first)
        _wipe_array(self.full)


def _empty_report(verdict: str, *, run_id: str) -> dict[str, object]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "verdict": verdict,
        "speaker_count": 0,
        "case_count": 0,
        "device_class_count": 0,
        "scenario_count": 0,
        "decision_count": 0,
        "path_decision_disagreement_count": 0,
        "path_decision_disagreements": {
            "baseline_vs_production": 0,
            "production_vs_control": 0,
        },
        "evaluations": {},
        "near_threshold_margin": NEAR_THRESHOLD_MARGIN,
        "runtime_noise_reduction": "enabled",
    }


def _corpus_failure() -> _HarnessFailure:
    return _HarnessFailure(
        ExitCode.CORPUS_UNAVAILABLE,
        "CORPUS_UNAVAILABLE_OR_INVALID",
    )


def _safe_atom(value: object) -> str:
    if type(value) is not str:
        raise _corpus_failure()
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or "/" in normalized
        or "\\" in normalized
    ):
        raise _corpus_failure()
    return normalized


def _safe_audio_path(corpus_dir: Path, value: object) -> Path:
    if type(value) is not str or not value.strip():
        raise _corpus_failure()
    declared = Path(value)
    if declared.is_absolute():
        raise _corpus_failure()
    resolved = (corpus_dir / declared).resolve()
    try:
        resolved.relative_to(corpus_dir)
    except ValueError:
        raise _corpus_failure() from None
    if not resolved.is_file():
        raise _corpus_failure()
    return resolved


def _validate_source_wav(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != SOURCE_SAMPLE_RATE_HZ
                or source.getcomptype() != "NONE"
            ):
                raise _corpus_failure()
            frame_count = source.getnframes()
            if not (
                MINIMUM_SOURCE_SAMPLES
                <= frame_count
                <= SOURCE_SAMPLE_RATE_HZ * MAXIMUM_SOURCE_SECONDS
            ):
                raise _corpus_failure()
    except _HarnessFailure:
        raise
    except (OSError, EOFError, wave.Error):
        raise _corpus_failure() from None


def _load_manifest(
    corpus_dir: Path,
) -> tuple[list[_CorpusCase], int, int, int]:
    try:
        root = corpus_dir.resolve(strict=True)
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file() or manifest_path.stat().st_size > MAXIMUM_MANIFEST_BYTES:
            raise _corpus_failure()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except _HarnessFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _corpus_failure() from None

    if (
        type(payload) is not dict
        or payload.get("schema_version") != 1
        or payload.get("source_sample_rate_hz") != SOURCE_SAMPLE_RATE_HZ
    ):
        raise _corpus_failure()
    raw_cases = payload.get("cases")
    if type(raw_cases) is not list or not 1 <= len(raw_cases) <= MAXIMUM_CASE_COUNT:
        raise _corpus_failure()

    cases: list[_CorpusCase] = []
    speaker_ids: set[str] = set()
    device_classes: set[str] = set()
    scenarios: set[str] = set()
    all_paths: set[Path] = set()
    for raw_case in raw_cases:
        if type(raw_case) is not dict:
            raise _corpus_failure()
        speaker_id = _safe_atom(raw_case.get("speaker_id"))
        device_class = _safe_atom(raw_case.get("device_class"))
        scenario = _safe_atom(raw_case.get("scenario"))
        raw_references = raw_case.get("references")
        if type(raw_references) is not list or len(raw_references) != 3:
            raise _corpus_failure()
        references = tuple(
            _safe_audio_path(root, value) for value in raw_references
        )
        holdout = _safe_audio_path(root, raw_case.get("holdout"))
        owned_paths = (*references, holdout)
        if len(set(owned_paths)) != 4 or any(path in all_paths for path in owned_paths):
            raise _corpus_failure()
        for path in owned_paths:
            _validate_source_wav(path)
        all_paths.update(owned_paths)
        cases.append(
            _CorpusCase(
                speaker_id=speaker_id,
                reference_paths=(references[0], references[1], references[2]),
                holdout_path=holdout,
            )
        )
        speaker_ids.add(speaker_id)
        device_classes.add(device_class)
        scenarios.add(scenario)
    if len(speaker_ids) < MINIMUM_SPEAKER_COUNT:
        raise _corpus_failure()
    if not REQUIRED_SCENARIOS.issubset(scenarios):
        raise _corpus_failure()
    return cases, len(speaker_ids), len(device_classes), len(scenarios)


def _read_source_pcm16(path: Path) -> bytearray:
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != SOURCE_SAMPLE_RATE_HZ
                or source.getcomptype() != "NONE"
            ):
                raise _corpus_failure()
            frame_count = source.getnframes()
            if not (
                MINIMUM_SOURCE_SAMPLES
                <= frame_count
                <= SOURCE_SAMPLE_RATE_HZ * MAXIMUM_SOURCE_SECONDS
            ):
                raise _corpus_failure()
            pcm16 = bytearray(source.readframes(frame_count))
    except _HarnessFailure:
        raise
    except (OSError, EOFError, wave.Error):
        raise _corpus_failure() from None
    if len(pcm16) != frame_count * 2:
        _wipe_bytes(pcm16)
        raise _corpus_failure()
    return pcm16


def _wipe_bytes(value: bytearray | None) -> None:
    if value is not None:
        value[:] = b"\x00" * len(value)


def _wipe_array(value: np.ndarray | None) -> None:
    if value is None:
        return
    try:
        if not value.flags.writeable:
            value.setflags(write=True)
        value.fill(0)
    except Exception:
        pass


def _resolve_node(node: str) -> str:
    resolved = shutil.which(node)
    if resolved is None:
        raise _HarnessFailure(
            ExitCode.BROWSER_RESAMPLER_UNAVAILABLE,
            "BROWSER_RESAMPLER_UNAVAILABLE",
        )
    return resolved


def _run_browser_path(
    source_pcm16: bytearray,
    *,
    node: str,
) -> bytearray:
    try:
        completed = subprocess.run(
            [
                node,
                str(WORKLET_RUNNER),
                str(WORKLET_SOURCE),
                str(SOURCE_SAMPLE_RATE_HZ),
                str(TARGET_SAMPLE_RATE_HZ),
                str(TARGET_AUDIO_SAMPLES),
            ],
            input=bytes(source_pcm16),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise _HarnessFailure(
            ExitCode.BROWSER_RESAMPLER_UNAVAILABLE,
            "BROWSER_RESAMPLER_UNAVAILABLE",
        ) from None
    if completed.returncode == int(ExitCode.CORPUS_UNAVAILABLE):
        raise _corpus_failure()
    if completed.returncode != 0:
        raise _HarnessFailure(
            ExitCode.BROWSER_RESAMPLER_UNAVAILABLE,
            "BROWSER_RESAMPLER_UNAVAILABLE",
        )
    result = bytearray(completed.stdout)
    if len(result) != TARGET_AUDIO_SAMPLES * 2:
        _wipe_bytes(result)
        raise _HarnessFailure(
            ExitCode.BROWSER_RESAMPLER_UNAVAILABLE,
            "BROWSER_RESAMPLER_UNAVAILABLE",
        )
    return result


async def _run_runtime_path(source_pcm16: bytearray) -> bytearray:
    try:
        from main_logic.voice_turn.audio_input import VoiceInputAudioPipeline
        from utils import audio_processor as audio_processor_module
    except Exception:
        raise _HarnessFailure(
            ExitCode.RUNTIME_PREPROCESSOR_UNAVAILABLE,
            "RUNTIME_PREPROCESSOR_UNAVAILABLE",
        ) from None

    # The CLI emits exactly one aggregate JSON report. Mute production audio
    # diagnostics only while this isolated evaluator owns the pipeline, then
    # restore the process-global logger state for import-based unit tests.
    audio_logger = audio_processor_module.logger
    logger_was_disabled = audio_logger.disabled
    audio_logger.disabled = True
    pipeline: Any | None = None
    output = bytearray()
    try:
        pipeline = VoiceInputAudioPipeline(nr_enabled=True)
        chunk_bytes = RUNTIME_CHUNK_SAMPLES * 2
        for offset in range(0, len(source_pcm16) - chunk_bytes + 1, chunk_bytes):
            chunk = bytes(memoryview(source_pcm16)[offset : offset + chunk_bytes])
            frame = await pipeline.process(
                chunk,
                sample_rate_hz=SOURCE_SAMPLE_RATE_HZ,
            )
            if not frame.rnnoise_available:
                raise _HarnessFailure(
                    ExitCode.RUNTIME_PREPROCESSOR_UNAVAILABLE,
                    "RUNTIME_PREPROCESSOR_UNAVAILABLE",
                )
            output.extend(frame.pcm16)
            if len(output) >= TARGET_AUDIO_SAMPLES * 2:
                del output[TARGET_AUDIO_SAMPLES * 2 :]
                return output
        raise _corpus_failure()
    except _HarnessFailure:
        _wipe_bytes(output)
        raise
    except Exception:
        _wipe_bytes(output)
        raise _HarnessFailure(
            ExitCode.RUNTIME_PREPROCESSOR_UNAVAILABLE,
            "RUNTIME_PREPROCESSOR_UNAVAILABLE",
        ) from None
    finally:
        unwinding = sys.exc_info()[0] is not None
        close_failure: BaseException | None = None
        try:
            if pipeline is not None:
                await pipeline.close()
        except BaseException as exc:
            close_failure = exc
            _wipe_bytes(output)
        finally:
            audio_logger.disabled = logger_was_disabled
        if close_failure is not None and not unwinding:
            if isinstance(close_failure, asyncio.CancelledError):
                raise close_failure
            raise _HarnessFailure(
                ExitCode.RUNTIME_PREPROCESSOR_UNAVAILABLE,
                "RUNTIME_PREPROCESSOR_UNAVAILABLE",
            ) from None


async def _run_server_normalized_path(source_pcm16: bytearray) -> bytearray:
    try:
        from main_logic.voice_identity_service.enrollment_audio import (
            EnrollmentAudioNormalizer,
        )
    except Exception:
        raise _HarnessFailure(
            ExitCode.RUNTIME_PREPROCESSOR_UNAVAILABLE,
            "RUNTIME_PREPROCESSOR_UNAVAILABLE",
        ) from None

    result: bytearray | None = None
    try:
        normalized = await EnrollmentAudioNormalizer(
            nr_enabled=True,
        ).normalize(
            bytes(source_pcm16),
            sample_rate_hz=SOURCE_SAMPLE_RATE_HZ,
            target_samples=TARGET_AUDIO_SAMPLES,
        )
        result = bytearray(normalized)
        if len(result) != TARGET_AUDIO_SAMPLES * 2:
            raise _corpus_failure()
        return result
    except _HarnessFailure:
        _wipe_bytes(result)
        raise
    except Exception:
        _wipe_bytes(result)
        raise _HarnessFailure(
            ExitCode.RUNTIME_PREPROCESSOR_UNAVAILABLE,
            "RUNTIME_PREPROCESSOR_UNAVAILABLE",
        ) from None


def _create_model(asset_dir: Path | None) -> Any:
    model: Any | None = None
    try:
        from main_logic.asr_client.speaker_shadow.campplus import (
            CampPlusEmbeddingModel,
        )
        model = CampPlusEmbeddingModel(asset_dir=asset_dir)
        if model.load():
            return model
    except Exception:
        pass
    if model is not None:
        try:
            model.close()
        except Exception:
            pass
    raise _HarnessFailure(
        ExitCode.CAMPPLUS_UNAVAILABLE,
        "CAMPPLUS_MODEL_UNAVAILABLE",
    )


def _normalized_sum(embeddings: Sequence[np.ndarray]) -> np.ndarray:
    if not embeddings:
        raise ValueError("embedding set must not be empty")
    result = np.zeros(embeddings[0].shape, dtype=np.float32)
    try:
        for embedding in embeddings:
            if embedding.shape != result.shape or not np.isfinite(embedding).all():
                raise ValueError("embedding contract mismatch")
            np.add(result, embedding, out=result)
        norm = float(np.linalg.norm(result))
        if not math.isfinite(norm) or norm <= 1e-12:
            raise ValueError("embedding norm is invalid")
        np.divide(result, np.float32(norm), out=result)
        return result
    except BaseException:
        _wipe_array(result)
        raise


def _embedding(
    model: Any,
    pcm16: bytearray,
    *,
    sample_count: int,
) -> np.ndarray:
    bounded = bytes(memoryview(pcm16)[: sample_count * 2])
    try:
        return model.embedding_from_pcm16(
            bounded,
            sample_rate_hz=TARGET_SAMPLE_RATE_HZ,
        )
    except Exception:
        raise _HarnessFailure(
            ExitCode.CAMPPLUS_UNAVAILABLE,
            "CAMPPLUS_MODEL_UNAVAILABLE",
        ) from None


def _profile_vectors(
    model: Any,
    references_pcm16: Sequence[bytearray],
) -> _ProfileVectors:
    embeddings: list[np.ndarray] = []
    leave_one_out: list[np.ndarray] = []
    centroid: np.ndarray | None = None
    try:
        embeddings = [
            _embedding(model, pcm16, sample_count=TARGET_AUDIO_SAMPLES)
            for pcm16 in references_pcm16
        ]
        leave_one_out = [
            _normalized_sum((embeddings[1], embeddings[2])),
            _normalized_sum((embeddings[0], embeddings[2])),
            _normalized_sum((embeddings[0], embeddings[1])),
        ]
        reference_scores = tuple(
            float(np.dot(embedding, other_reference))
            for embedding, other_reference in zip(
                embeddings,
                leave_one_out,
                strict=True,
            )
        )
        centroid = _normalized_sum(embeddings)
        result = _ProfileVectors(
            centroid=centroid,
            reference_scores=(
                reference_scores[0],
                reference_scores[1],
                reference_scores[2],
            ),
        )
        centroid = None
        return result
    finally:
        for value in embeddings:
            _wipe_array(value)
        for value in leave_one_out:
            _wipe_array(value)
        _wipe_array(centroid)


def _candidate_vectors(
    model: Any,
    holdout_pcm16: bytearray,
) -> _CandidateVectors:
    first: np.ndarray | None = None
    full: np.ndarray | None = None
    try:
        first = _embedding(
            model,
            holdout_pcm16,
            sample_count=HOLDOUT_FIRST_SAMPLES,
        )
        full = _embedding(
            model,
            holdout_pcm16,
            sample_count=TARGET_AUDIO_SAMPLES,
        )
        result = _CandidateVectors(first=first, full=full)
        first = None
        full = None
        return result
    finally:
        _wipe_array(first)
        _wipe_array(full)


def _is_near_threshold(score: float) -> bool:
    return abs(score - THRESHOLD) <= NEAR_THRESHOLD_MARGIN


def _path_metrics(
    profiles: Sequence[_ProfileVectors],
    candidates: Sequence[_CandidateVectors],
    speaker_ids: Sequence[str],
) -> tuple[dict[str, object], tuple[bool, ...]]:
    if len(profiles) != len(candidates) or len(profiles) != len(speaker_ids):
        raise ValueError("profile, candidate, and speaker counts must match")

    reference_false_low = 0
    holdout_first_false_low = 0
    holdout_full_false_low = 0
    impostor_first_false_high = 0
    impostor_full_false_high = 0
    near_threshold_count = 0
    decisions: list[bool] = []

    for case_index, (profile, candidate) in enumerate(
        zip(profiles, candidates, strict=True)
    ):
        for score in profile.reference_scores:
            accepted = score >= THRESHOLD
            decisions.append(accepted)
            reference_false_low += int(not accepted)
            near_threshold_count += int(_is_near_threshold(score))

        owner_first_score = float(np.dot(profile.centroid, candidate.first))
        owner_full_score = float(np.dot(profile.centroid, candidate.full))
        owner_first_accepted = owner_first_score >= THRESHOLD
        owner_full_accepted = owner_full_score >= THRESHOLD
        decisions.extend((owner_first_accepted, owner_full_accepted))
        holdout_first_false_low += int(not owner_first_accepted)
        holdout_full_false_low += int(not owner_full_accepted)
        near_threshold_count += int(_is_near_threshold(owner_first_score))
        near_threshold_count += int(_is_near_threshold(owner_full_score))

        for other_index, impostor in enumerate(candidates):
            if speaker_ids[other_index] == speaker_ids[case_index]:
                continue
            impostor_first_score = float(
                np.dot(profile.centroid, impostor.first)
            )
            impostor_full_score = float(np.dot(profile.centroid, impostor.full))
            impostor_first_accepted = impostor_first_score >= THRESHOLD
            impostor_full_accepted = impostor_full_score >= THRESHOLD
            decisions.extend((impostor_first_accepted, impostor_full_accepted))
            impostor_first_false_high += int(impostor_first_accepted)
            impostor_full_false_high += int(impostor_full_accepted)
            near_threshold_count += int(_is_near_threshold(impostor_first_score))
            near_threshold_count += int(_is_near_threshold(impostor_full_score))

    owner_total = (
        reference_false_low
        + holdout_first_false_low
        + holdout_full_false_low
    )
    impostor_total = impostor_first_false_high + impostor_full_false_high
    return (
        {
            "decision_count": len(decisions),
            "owner_false_low": {
                "reference_leave_one_out": reference_false_low,
                "holdout_1_5": holdout_first_false_low,
                "holdout_3_0": holdout_full_false_low,
                "total": owner_total,
            },
            "impostor_false_high": {
                "holdout_1_5": impostor_first_false_high,
                "holdout_3_0": impostor_full_false_high,
                "total": impostor_total,
            },
            "near_threshold_count": near_threshold_count,
        },
        tuple(decisions),
    )


async def _prepare_case_audio(
    case: _CorpusCase,
    *,
    node: str,
) -> tuple[list[bytearray], list[bytearray], bytearray, bytearray]:
    browser_references: list[bytearray] = []
    server_references: list[bytearray] = []
    server_holdout: bytearray | None = None
    runtime_holdout: bytearray | None = None
    try:
        for path in case.reference_paths:
            source: bytearray | None = None
            browser_pcm: bytearray | None = None
            server_pcm: bytearray | None = None
            try:
                source = _read_source_pcm16(path)
                browser_pcm = _run_browser_path(source, node=node)
                server_pcm = await _run_server_normalized_path(source)
                browser_references.append(browser_pcm)
                server_references.append(server_pcm)
                browser_pcm = None
                server_pcm = None
            finally:
                _wipe_bytes(source)
                _wipe_bytes(browser_pcm)
                _wipe_bytes(server_pcm)

        source = None
        try:
            source = _read_source_pcm16(case.holdout_path)
            server_holdout = await _run_server_normalized_path(source)
            runtime_holdout = await _run_runtime_path(source)
        finally:
            _wipe_bytes(source)
        return (
            browser_references,
            server_references,
            server_holdout,
            runtime_holdout,
        )
    except BaseException:
        for value in browser_references:
            _wipe_bytes(value)
        for value in server_references:
            _wipe_bytes(value)
        _wipe_bytes(server_holdout)
        _wipe_bytes(runtime_holdout)
        raise


async def _evaluate(
    cases: Sequence[_CorpusCase],
    *,
    speaker_count: int,
    device_class_count: int = 0,
    scenario_count: int = 0,
    asset_dir: Path | None,
    node: str,
    run_id: str | None = None,
) -> tuple[ExitCode, dict[str, object]]:
    effective_run_id = run_id or uuid.uuid4().hex
    model = _create_model(asset_dir)
    browser_profiles: list[_ProfileVectors] = []
    server_profiles: list[_ProfileVectors] = []
    server_candidates: list[_CandidateVectors] = []
    runtime_candidates: list[_CandidateVectors] = []
    try:
        for case in cases:
            browser_refs: list[bytearray] = []
            server_refs: list[bytearray] = []
            server_holdout: bytearray | None = None
            runtime_holdout: bytearray | None = None
            try:
                (
                    browser_refs,
                    server_refs,
                    server_holdout,
                    runtime_holdout,
                ) = await _prepare_case_audio(case, node=node)
                browser_profiles.append(_profile_vectors(model, browser_refs))
                server_profiles.append(_profile_vectors(model, server_refs))
                server_candidates.append(
                    _candidate_vectors(model, server_holdout)
                )
                runtime_candidates.append(
                    _candidate_vectors(model, runtime_holdout)
                )
            finally:
                for value in browser_refs:
                    _wipe_bytes(value)
                for value in server_refs:
                    _wipe_bytes(value)
                _wipe_bytes(server_holdout)
                _wipe_bytes(runtime_holdout)

        speaker_ids = tuple(case.speaker_id for case in cases)
        baseline_metrics, baseline_decisions = _path_metrics(
            browser_profiles,
            runtime_candidates,
            speaker_ids,
        )
        production_metrics, production_decisions = _path_metrics(
            server_profiles,
            runtime_candidates,
            speaker_ids,
        )
        control_metrics, control_decisions = _path_metrics(
            server_profiles,
            server_candidates,
            speaker_ids,
        )
        baseline_vs_production = sum(
            left != right
            for left, right in zip(
                baseline_decisions,
                production_decisions,
                strict=True,
            )
        )
        production_vs_control = sum(
            left != right
            for left, right in zip(
                production_decisions,
                control_decisions,
                strict=True,
            )
        )
        total_disagreements = baseline_vs_production + production_vs_control

        production_owner = production_metrics["owner_false_low"]
        production_impostor = production_metrics["impostor_false_high"]
        baseline_impostor = baseline_metrics["impostor_false_high"]
        assert isinstance(production_owner, dict)
        assert isinstance(production_impostor, dict)
        assert isinstance(baseline_impostor, dict)
        gate_passed = (
            production_owner["total"] == 0
            and production_impostor["total"] <= baseline_impostor["total"]
            and production_vs_control == 0
        )
        verdict = (
            "PASS_AUDIO_NORMALIZATION_GATE"
            if gate_passed
            else "BLOCK_AUDIO_NORMALIZATION_GATE"
        )
        evaluations = {
            BASELINE_PATH: baseline_metrics,
            PRODUCTION_PATH: production_metrics,
            CONTROL_PATH: control_metrics,
        }
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run_id": effective_run_id,
            "verdict": verdict,
            "speaker_count": speaker_count,
            "case_count": len(cases),
            "device_class_count": device_class_count,
            "scenario_count": scenario_count,
            "decision_count": sum(
                int(metrics["decision_count"])
                for metrics in evaluations.values()
            ),
            "path_decision_disagreement_count": total_disagreements,
            "path_decision_disagreements": {
                "baseline_vs_production": baseline_vs_production,
                "production_vs_control": production_vs_control,
            },
            "evaluations": evaluations,
            "near_threshold_margin": NEAR_THRESHOLD_MARGIN,
            "runtime_noise_reduction": "enabled",
        }
        return (
            ExitCode.PASS if gate_passed else ExitCode.BLOCK,
            report,
        )
    finally:
        for profile in (*browser_profiles, *server_profiles):
            profile.wipe()
        for candidate in (*server_candidates, *runtime_candidates):
            candidate.wipe()
        model.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--campplus-asset-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--node", default="node")
    return parser


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_has_symlink_component(path: Path) -> bool:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            return True
    return False


def _prepare_output_path(
    output: Path | None,
    *,
    corpus_dir: Path,
    asset_dir: Path | None,
) -> Path | None:
    if output is None:
        return None
    absolute = output.expanduser().absolute()
    if absolute.suffix.lower() != ".json" or _path_has_symlink_component(absolute):
        raise _HarnessFailure(
            ExitCode.INTERNAL_ERROR,
            "HARNESS_INTERNAL_ERROR",
        )
    resolved = absolute.resolve(strict=False)
    protected_roots = [
        root.resolve(strict=False) for root in PROTECTED_PROJECT_ROOTS
    ]
    protected_roots.append(corpus_dir.resolve(strict=False))
    if asset_dir is not None:
        protected_roots.append(asset_dir.resolve(strict=False))
    if any(_path_is_within(resolved, root) for root in protected_roots):
        raise _HarnessFailure(
            ExitCode.INTERNAL_ERROR,
            "HARNESS_INTERNAL_ERROR",
        )
    try:
        project_relative = resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        project_relative = None
    if project_relative is not None:
        first_part = project_relative.parts[0] if project_relative.parts else ""
        if (
            first_part not in ALLOWED_PROJECT_REPORT_ROOTS
            and not first_part.startswith(".pytest-")
        ):
            raise _HarnessFailure(
                ExitCode.INTERNAL_ERROR,
                "HARNESS_INTERNAL_ERROR",
            )
    if absolute.exists():
        raise _HarnessFailure(
            ExitCode.INTERNAL_ERROR,
            "HARNESS_INTERNAL_ERROR",
        )
    return absolute


def _write_report_atomic(output: Path, rendered: str) -> None:
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if _path_has_symlink_component(output) or output.exists():
            raise OSError("unsafe output target")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = None
            stream.write(rendered)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _run(argv: Sequence[str] | None = None) -> tuple[int, dict[str, object], Path | None]:
    args = _build_parser().parse_args(argv)
    run_id = uuid.uuid4().hex
    output: Path | None = None
    try:
        output = _prepare_output_path(
            args.output,
            corpus_dir=args.corpus_dir,
            asset_dir=args.campplus_asset_dir,
        )
        cases, speaker_count, device_class_count, scenario_count = _load_manifest(
            args.corpus_dir
        )
        node = _resolve_node(args.node)
        asset_dir = (
            args.campplus_asset_dir.resolve()
            if args.campplus_asset_dir is not None
            else None
        )
        exit_code, report = asyncio.run(
            _evaluate(
                cases,
                speaker_count=speaker_count,
                device_class_count=device_class_count,
                scenario_count=scenario_count,
                asset_dir=asset_dir,
                node=node,
                run_id=run_id,
            )
        )
        return int(exit_code), report, output
    except _HarnessFailure as exc:
        return (
            int(exc.exit_code),
            _empty_report(exc.verdict, run_id=run_id),
            output,
        )
    except Exception:
        return (
            int(ExitCode.INTERNAL_ERROR),
            _empty_report("HARNESS_INTERNAL_ERROR", run_id=run_id),
            output,
        )


def main(argv: Sequence[str] | None = None) -> int:
    exit_code, report, output = _run(argv)
    rendered = json.dumps(
        report,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if output is not None:
        try:
            _write_report_atomic(output, rendered)
        except OSError:
            exit_code = int(ExitCode.INTERNAL_ERROR)
            report = _empty_report(
                "HARNESS_INTERNAL_ERROR",
                run_id=str(report["run_id"]),
            )
            rendered = json.dumps(
                report,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
    print(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
