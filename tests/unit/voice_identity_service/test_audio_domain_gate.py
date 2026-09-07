from __future__ import annotations

import builtins
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import wave

import numpy as np
import pytest


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_HARNESS_PATH = (
    _REPOSITORY_ROOT / "scripts" / "evaluate_voice_enrollment_audio_domain.py"
)
_AGGREGATE_REPORT_KEYS = {
    "schema_version",
    "run_id",
    "verdict",
    "speaker_count",
    "case_count",
    "device_class_count",
    "scenario_count",
    "decision_count",
    "path_decision_disagreement_count",
    "path_decision_disagreements",
    "evaluations",
    "near_threshold_margin",
    "runtime_noise_reduction",
    "error_code",
}
_DISAGREEMENT_KEYS = {
    "baseline_vs_production",
    "production_vs_control",
}
_EVALUATION_KEYS = {
    "browser_old_profile_to_runtime_holdout",
    "server_normalized_profile_to_runtime_holdout",
    "server_normalized_profile_to_same_path_holdout",
}
_PRIVATE_REPORT_KEYS = {
    "speaker_id",
    "device_class",
    "scenario",
    "references",
    "holdout",
    "path",
    "paths",
    "score",
    "scores",
    "embedding",
    "embeddings",
    "cases",
}


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "voice_enrollment_audio_domain_gate_under_test",
        _HARNESS_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load harness from {_HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _write_wav(
    path: Path,
    *,
    sample_rate_hz: int = 48_000,
    channels: int = 1,
    sample_width_bytes: int = 2,
    duration_seconds: float = 3.1,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = int(sample_rate_hz * duration_seconds)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width_bytes)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(
            b"\x00" * frame_count * channels * sample_width_bytes
        )


def _write_valid_corpus(root: Path) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    scenarios = ("quiet", "steady-noise", "natural-pause")
    device_classes = ("usb-headset", "laptop-array", "usb-headset")
    for speaker_index in range(3):
        case_root = Path("clips") / f"case-{speaker_index}"
        references = [
            (case_root / f"reference-{reference_index}.wav").as_posix()
            for reference_index in range(3)
        ]
        holdout = (case_root / "holdout.wav").as_posix()
        for relative_path in (*references, holdout):
            _write_wav(root / relative_path)
        cases.append(
            {
                "speaker_id": f"opaque-speaker-{speaker_index}",
                "device_class": device_classes[speaker_index],
                "scenario": scenarios[speaker_index],
                "references": references,
                "holdout": holdout,
            }
        )
    manifest: dict[str, object] = {
        "schema_version": 1,
        "source_sample_rate_hz": 48_000,
        "cases": cases,
    }
    _write_manifest(root, manifest)
    return manifest


def _write_manifest(root: Path, manifest: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )


def _run_harness(*arguments: str) -> tuple[int, dict[str, object]]:
    module = _load_harness()
    exit_code, report, _output = module._run(list(arguments))
    assert type(exit_code) is int
    assert type(report) is dict
    return exit_code, report


def _assert_aggregate_only(
    report: dict[str, object],
    *,
    secrets: tuple[str, ...] = (),
) -> None:
    assert set(report) <= _AGGREGATE_REPORT_KEYS
    run_id = report.get("run_id")
    assert type(run_id) is str and run_id
    disagreements = report.get("path_decision_disagreements")
    if disagreements is not None:
        assert type(disagreements) is dict
        assert set(disagreements) == _DISAGREEMENT_KEYS
    evaluations = report.get("evaluations")
    if evaluations is not None:
        assert type(evaluations) is dict
        assert set(evaluations) <= _EVALUATION_KEYS

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                assert key not in _PRIVATE_REPORT_KEYS
                inspect(nested)
            return
        assert not isinstance(value, (list, tuple, set))

    inspect(report)
    serialized = json.dumps(report, sort_keys=True)
    for secret in secrets:
        assert secret not in serialized


@pytest.mark.unit
def test_missing_corpus_has_stable_exit_code_and_private_error_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "gate-report.json"
    module = _load_harness()

    exit_code = module.main(
        [
            "--corpus-dir",
            str(tmp_path / "missing-corpus-private-canary"),
            "--output",
            str(output),
        ]
    )
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert report["verdict"] == "CORPUS_UNAVAILABLE_OR_INVALID"
    assert json.loads(output.read_text(encoding="utf-8")) == report
    _assert_aggregate_only(report, secrets=("missing-corpus-private-canary",))


@pytest.mark.unit
@pytest.mark.parametrize(
    "manifest_update",
    [
        {"schema_version": 2},
        {"source_sample_rate_hz": 16_000},
        {"cases": []},
    ],
    ids=("schema-version", "source-sample-rate", "empty-cases"),
)
def test_invalid_manifest_is_rejected_before_model_loading(
    tmp_path: Path,
    manifest_update: dict[str, object],
) -> None:
    corpus = tmp_path / "corpus"
    manifest = _write_valid_corpus(corpus)
    manifest.update(manifest_update)
    _write_manifest(corpus, manifest)

    exit_code, report = _run_harness("--corpus-dir", str(corpus))

    assert exit_code == 3
    assert report["verdict"] == "CORPUS_UNAVAILABLE_OR_INVALID"
    _assert_aggregate_only(report)


@pytest.mark.unit
@pytest.mark.parametrize("missing_field", ["device_class", "scenario"])
def test_device_class_and_scenario_are_required_manifest_fields(
    tmp_path: Path,
    missing_field: str,
) -> None:
    corpus = tmp_path / "corpus"
    manifest = _write_valid_corpus(corpus)
    cases = manifest["cases"]
    assert isinstance(cases, list)
    first_case = cases[0]
    assert isinstance(first_case, dict)
    del first_case[missing_field]
    _write_manifest(corpus, manifest)

    exit_code, report = _run_harness("--corpus-dir", str(corpus))

    assert exit_code == 3
    assert report["verdict"] == "CORPUS_UNAVAILABLE_OR_INVALID"
    _assert_aggregate_only(report)


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing_scenario",
    ["quiet", "steady-noise", "natural-pause"],
)
def test_manifest_must_cover_each_required_scenario(
    tmp_path: Path,
    missing_scenario: str,
) -> None:
    corpus = tmp_path / "corpus"
    manifest = _write_valid_corpus(corpus)
    cases = manifest["cases"]
    assert isinstance(cases, list)
    replacement = "quiet" if missing_scenario != "quiet" else "steady-noise"
    for case in cases:
        assert isinstance(case, dict)
        if case["scenario"] == missing_scenario:
            case["scenario"] = replacement
    _write_manifest(corpus, manifest)

    exit_code, report = _run_harness("--corpus-dir", str(corpus))

    assert exit_code == 3
    assert report["verdict"] == "CORPUS_UNAVAILABLE_OR_INVALID"
    _assert_aggregate_only(report)


@pytest.mark.unit
def test_malformed_manifest_json_is_rejected(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "manifest.json").write_text("{not-json", encoding="utf-8")

    exit_code, report = _run_harness("--corpus-dir", str(corpus))

    assert exit_code == 3
    assert report["verdict"] == "CORPUS_UNAVAILABLE_OR_INVALID"
    _assert_aggregate_only(report)


@pytest.mark.unit
def test_manifest_audio_path_cannot_escape_corpus_root(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    manifest = _write_valid_corpus(corpus)
    outside_name = "outside-private-audio-canary.wav"
    _write_wav(tmp_path / outside_name)
    cases = manifest["cases"]
    assert isinstance(cases, list)
    first_case = cases[0]
    assert isinstance(first_case, dict)
    first_case["references"] = [
        f"../{outside_name}",
        *first_case["references"][1:],
    ]
    _write_manifest(corpus, manifest)

    exit_code, report = _run_harness("--corpus-dir", str(corpus))

    assert exit_code == 3
    assert report["verdict"] == "CORPUS_UNAVAILABLE_OR_INVALID"
    _assert_aggregate_only(report, secrets=(outside_name,))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("wav_options", "case_id"),
    [
        ({"sample_rate_hz": 16_000}, "sample-rate"),
        ({"channels": 2}, "channels"),
        ({"sample_width_bytes": 1}, "sample-width"),
        ({"duration_seconds": 1.0}, "duration"),
        ({"duration_seconds": 3.0}, "exact-three-seconds"),
    ],
)
def test_invalid_wav_contract_is_rejected_before_inference(
    tmp_path: Path,
    wav_options: dict[str, int | float],
    case_id: str,
) -> None:
    del case_id
    corpus = tmp_path / "corpus"
    manifest = _write_valid_corpus(corpus)
    cases = manifest["cases"]
    assert isinstance(cases, list)
    first_case = cases[0]
    assert isinstance(first_case, dict)
    first_reference = first_case["references"][0]
    assert isinstance(first_reference, str)
    _write_wav(corpus / first_reference, **wav_options)

    exit_code, report = _run_harness("--corpus-dir", str(corpus))

    assert exit_code == 3
    assert report["verdict"] == "CORPUS_UNAVAILABLE_OR_INVALID"
    _assert_aggregate_only(report)


@pytest.mark.unit
def test_three_point_one_second_wav_is_valid_corpus_input(tmp_path: Path) -> None:
    module = _load_harness()
    corpus = tmp_path / "corpus"
    _write_valid_corpus(corpus)

    cases, speaker_count, device_class_count, scenario_count = (
        module._load_manifest(corpus)
    )

    assert len(cases) == 3
    assert speaker_count == 3
    assert device_class_count == 2
    assert scenario_count == 3


@pytest.mark.unit
def test_missing_campplus_assets_have_distinct_exit_code(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _write_valid_corpus(corpus)
    missing_assets = tmp_path / "missing-campplus-assets"

    exit_code, report = _run_harness(
        "--corpus-dir",
        str(corpus),
        "--campplus-asset-dir",
        str(missing_assets),
        "--node",
        sys.executable,
    )

    assert exit_code == 4
    assert report["verdict"] == "CAMPPLUS_MODEL_UNAVAILABLE"
    _assert_aggregate_only(report, secrets=(missing_assets.name,))


@pytest.mark.unit
def test_missing_browser_resampler_has_distinct_exit_code(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _write_valid_corpus(corpus)

    exit_code, report = _run_harness(
        "--corpus-dir",
        str(corpus),
        "--node",
        "definitely-missing-node-binary",
    )

    assert exit_code == 6
    assert report["verdict"] == "BROWSER_RESAMPLER_UNAVAILABLE"
    _assert_aggregate_only(report)


class _FakeModel:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _corpus_cases_for_pure_evaluation(module) -> list[object]:
    return [
        module._CorpusCase(
            speaker_id=f"private-speaker-canary-{case_index}",
            reference_paths=(
                Path(f"private-reference-{case_index}-0.wav"),
                Path(f"private-reference-{case_index}-1.wav"),
                Path(f"private-reference-{case_index}-2.wav"),
            ),
            holdout_path=Path(f"private-holdout-{case_index}.wav"),
        )
        for case_index in range(3)
    ]


def _install_pure_evaluation_fakes(
    monkeypatch: pytest.MonkeyPatch,
    module,
    *,
    browser_centroids: list[np.ndarray] | None = None,
    server_centroids: list[np.ndarray] | None = None,
    server_candidates: list[tuple[np.ndarray, np.ndarray]] | None = None,
    runtime_candidates: list[tuple[np.ndarray, np.ndarray]] | None = None,
    browser_reference_scores: list[tuple[float, float, float]] | None = None,
    server_reference_scores: list[tuple[float, float, float]] | None = None,
) -> _FakeModel:
    model = _FakeModel()
    monkeypatch.setattr(module, "_create_model", lambda _asset_dir: model)
    axes = [row.copy() for row in np.eye(3, dtype=np.float32)]
    browser_centroids = browser_centroids or axes
    server_centroids = server_centroids or axes
    server_candidates = server_candidates or [
        (axis.copy(), axis.copy()) for axis in axes
    ]
    runtime_candidates = runtime_candidates or [
        (axis.copy(), axis.copy()) for axis in axes
    ]
    browser_reference_scores = browser_reference_scores or [
        (0.9, 0.9, 0.9) for _ in axes
    ]
    server_reference_scores = server_reference_scores or [
        (0.9, 0.9, 0.9) for _ in axes
    ]

    async def prepare_case_audio(case, *, node: str):
        assert node == "fake-node"
        case_index = int(case.speaker_id.rsplit("-", 1)[1])
        return (
            [bytearray(f"browser:{case_index}".encode()) for _ in range(3)],
            [bytearray(f"server:{case_index}".encode()) for _ in range(3)],
            bytearray(f"server:{case_index}".encode()),
            bytearray(f"runtime:{case_index}".encode()),
        )

    def profile_vectors(_model, references):
        path, raw_index = bytes(references[0]).decode().split(":")
        case_index = int(raw_index)
        centroids = (
            browser_centroids if path == "browser" else server_centroids
        )
        reference_scores = (
            browser_reference_scores
            if path == "browser"
            else server_reference_scores
        )
        return module._ProfileVectors(
            centroid=np.asarray(centroids[case_index], dtype=np.float32).copy(),
            reference_scores=reference_scores[case_index],
        )

    def candidate_vectors(_model, holdout):
        path, raw_index = bytes(holdout).decode().split(":")
        case_index = int(raw_index)
        candidates = (
            server_candidates if path == "server" else runtime_candidates
        )
        first, full = candidates[case_index]
        return module._CandidateVectors(
            first=np.asarray(first, dtype=np.float32).copy(),
            full=np.asarray(full, dtype=np.float32).copy(),
        )

    monkeypatch.setattr(module, "_prepare_case_audio", prepare_case_audio)
    monkeypatch.setattr(module, "_profile_vectors", profile_vectors)
    monkeypatch.setattr(module, "_candidate_vectors", candidate_vectors)
    return model


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_production_combinations_replace_same_domain_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    cases = _corpus_cases_for_pure_evaluation(module)
    axes = [row.copy() for row in np.eye(3, dtype=np.float32)]
    model = _install_pure_evaluation_fakes(
        monkeypatch,
        module,
        browser_centroids=[axes[1], axes[1], axes[2]],
    )

    exit_code, report = await module._evaluate(
        cases,
        speaker_count=3,
        device_class_count=2,
        scenario_count=3,
        asset_dir=None,
        node="fake-node",
        run_id="production-matrix-run-id",
    )

    assert int(exit_code) == 0
    assert report["verdict"] == "PASS_AUDIO_NORMALIZATION_GATE"
    assert report["decision_count"] == 81
    disagreements = report["path_decision_disagreements"]
    assert disagreements["baseline_vs_production"] > 0
    assert disagreements["production_vs_control"] == 0
    evaluations = report["evaluations"]
    assert (
        evaluations[module.BASELINE_PATH]["owner_false_low"]["total"] > 0
    )
    assert (
        evaluations[module.PRODUCTION_PATH]["owner_false_low"]["total"] == 0
    )
    assert (
        evaluations[module.CONTROL_PATH]["owner_false_low"]["total"] == 0
    )
    assert model.closed
    _assert_aggregate_only(
        report,
        secrets=(
            "private-speaker-canary",
            "private-reference",
            "private-holdout",
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_production_owner_false_low_blocks_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    cases = _corpus_cases_for_pure_evaluation(module)
    axes = [row.copy() for row in np.eye(3, dtype=np.float32)]
    model = _install_pure_evaluation_fakes(
        monkeypatch,
        module,
        server_centroids=[axes[1], axes[1], axes[2]],
    )

    exit_code, report = await module._evaluate(
        cases,
        speaker_count=3,
        device_class_count=2,
        scenario_count=3,
        asset_dir=None,
        node="fake-node",
        run_id="owner-false-low-run-id",
    )

    assert int(exit_code) == 2
    assert report["verdict"] == "BLOCK_AUDIO_NORMALIZATION_GATE"
    production = report["evaluations"][module.PRODUCTION_PATH]
    assert production["owner_false_low"]["holdout_1_5"] > 0
    assert production["owner_false_low"]["holdout_3_0"] > 0
    assert model.closed
    _assert_aggregate_only(report)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_impostor_false_high_regression_blocks_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    cases = _corpus_cases_for_pure_evaluation(module)
    axes = [row.copy() for row in np.eye(3, dtype=np.float32)]
    risky = np.asarray((0.8, 0.6, 0.0), dtype=np.float32)
    model = _install_pure_evaluation_fakes(
        monkeypatch,
        module,
        server_centroids=[risky, axes[1], axes[2]],
    )

    exit_code, report = await module._evaluate(
        cases,
        speaker_count=3,
        device_class_count=2,
        scenario_count=3,
        asset_dir=None,
        node="fake-node",
        run_id="impostor-regression-run-id",
    )

    baseline = report["evaluations"][module.BASELINE_PATH]
    production = report["evaluations"][module.PRODUCTION_PATH]
    assert int(exit_code) == 2
    assert production["owner_false_low"]["total"] == 0
    assert (
        production["impostor_false_high"]["total"]
        > baseline["impostor_false_high"]["total"]
    )
    assert model.closed
    _assert_aggregate_only(report)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_same_path_control_disagreement_blocks_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    cases = _corpus_cases_for_pure_evaluation(module)
    axes = [row.copy() for row in np.eye(3, dtype=np.float32)]
    model = _install_pure_evaluation_fakes(
        monkeypatch,
        module,
        server_candidates=[
            (-axes[0], -axes[0]),
            (axes[1], axes[1]),
            (axes[2], axes[2]),
        ],
    )

    exit_code, report = await module._evaluate(
        cases,
        speaker_count=3,
        device_class_count=2,
        scenario_count=3,
        asset_dir=None,
        node="fake-node",
        run_id="control-disagreement-run-id",
    )

    assert int(exit_code) == 2
    assert report["path_decision_disagreements"]["production_vs_control"] > 0
    assert model.closed
    _assert_aggregate_only(report)


@pytest.mark.unit
def test_path_metrics_cover_loo_holdouts_impostors_and_near_threshold() -> None:
    module = _load_harness()
    axes = [row.copy() for row in np.eye(3, dtype=np.float32)]
    profiles = [
        module._ProfileVectors(axis.copy(), (0.9, 0.9, 0.9))
        for axis in axes
    ]
    candidates = [
        module._CandidateVectors(axis.copy(), axis.copy())
        for axis in axes
    ]
    candidates[0].first = np.asarray((0.4, 0.0, 0.0), dtype=np.float32)
    try:
        metrics, decisions = module._path_metrics(
            profiles,
            candidates,
            ("speaker-a", "speaker-b", "speaker-c"),
        )
    finally:
        for profile in profiles:
            profile.wipe()
        for candidate in candidates:
            candidate.wipe()

    assert len(decisions) == 27
    assert metrics["owner_false_low"] == {
        "reference_leave_one_out": 0,
        "holdout_1_5": 0,
        "holdout_3_0": 0,
        "total": 0,
    }
    assert metrics["impostor_false_high"]["total"] == 0
    assert metrics["near_threshold_count"] == 1


@pytest.mark.unit
def test_impostor_metrics_exclude_other_cases_from_the_same_speaker() -> None:
    module = _load_harness()
    axes = [row.copy() for row in np.eye(3, dtype=np.float32)]
    profiles = [
        module._ProfileVectors(axis.copy(), (0.9, 0.9, 0.9))
        for axis in axes
    ]
    candidates = [
        module._CandidateVectors(axis.copy(), axis.copy())
        for axis in axes
    ]
    try:
        metrics, decisions = module._path_metrics(
            profiles,
            candidates,
            ("speaker-a", "speaker-a", "speaker-b"),
        )
    finally:
        for profile in profiles:
            profile.wipe()
        for candidate in candidates:
            candidate.wipe()

    assert len(decisions) == 23
    assert metrics["impostor_false_high"]["total"] == 0


@pytest.mark.unit
def test_output_inside_protected_roots_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    module = _load_harness()
    corpus = tmp_path / "corpus"
    assets = tmp_path / "assets"
    _write_valid_corpus(corpus)
    assets.mkdir()
    protected_outputs = (
        corpus / "report.json",
        corpus / "nested" / ".." / "report.json",
        assets / "report.json",
        _REPOSITORY_ROOT / ".phase0-protected-report.json",
    )

    for output in protected_outputs:
        assert not output.exists()
        arguments = [
            "--corpus-dir",
            str(corpus),
            "--output",
            str(output),
        ]
        if output == assets / "report.json":
            arguments.extend(("--campplus-asset-dir", str(assets)))
        exit_code, report, prepared_output = module._run(arguments)
        assert exit_code == 70
        assert report["verdict"] == "HARNESS_INTERNAL_ERROR"
        assert prepared_output is None
        assert not output.exists()
        _assert_aggregate_only(report)


@pytest.mark.unit
def test_output_hardlink_alias_of_corpus_file_is_rejected(tmp_path: Path) -> None:
    module = _load_harness()
    corpus = tmp_path / "corpus"
    manifest = _write_valid_corpus(corpus)
    manifest_path = corpus / "manifest.json"
    alias = tmp_path / "manifest-hardlink-alias.json"
    try:
        os.link(manifest_path, alias)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    exit_code, report, prepared_output = module._run(
        [
            "--corpus-dir",
            str(corpus),
            "--output",
            str(alias),
        ]
    )

    assert exit_code == 70
    assert report["verdict"] == "HARNESS_INTERNAL_ERROR"
    assert prepared_output is None
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert alias.exists()
    _assert_aggregate_only(report)


@pytest.mark.unit
def test_symlink_output_is_rejected_without_touching_target(tmp_path: Path) -> None:
    module = _load_harness()
    corpus = tmp_path / "corpus"
    _write_valid_corpus(corpus)
    target = tmp_path / "safe-target.json"
    target.write_text("private-stale-target", encoding="utf-8")
    link = tmp_path / "output-symlink.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    exit_code, report, prepared_output = module._run(
        [
            "--corpus-dir",
            str(corpus),
            "--output",
            str(link),
        ]
    )

    assert exit_code == 70
    assert report["verdict"] == "HARNESS_INTERNAL_ERROR"
    assert prepared_output is None
    assert target.read_text(encoding="utf-8") == "private-stale-target"
    assert link.is_symlink()
    _assert_aggregate_only(report, secrets=("private-stale-target",))


@pytest.mark.unit
def test_any_symlink_component_is_rejected_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    output = tmp_path / "symlink-parent" / "report.json"
    monkeypatch.setattr(module, "_path_has_symlink_component", lambda _path: True)

    with pytest.raises(module._HarnessFailure) as raised:
        module._prepare_output_path(
            output,
            corpus_dir=tmp_path / "corpus",
            asset_dir=None,
        )

    assert int(raised.value.exit_code) == 70
    assert raised.value.verdict == "HARNESS_INTERNAL_ERROR"
    assert not output.exists()


@pytest.mark.unit
def test_existing_stale_pass_output_is_rejected_and_left_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_harness()
    corpus = tmp_path / "missing-corpus"
    output = tmp_path / "report.json"
    stale = json.dumps({"verdict": "PASS_KEEP_16K_CONTRACT"})
    output.write_text(stale, encoding="utf-8")

    exit_code = module.main(
        [
            "--corpus-dir",
            str(corpus),
            "--output",
            str(output),
        ]
    )
    stdout_report = json.loads(capsys.readouterr().out)

    assert exit_code == 70
    assert stdout_report["verdict"] == "HARNESS_INTERNAL_ERROR"
    assert output.read_text(encoding="utf-8") == stale
    _assert_aggregate_only(stdout_report)


@pytest.mark.unit
@pytest.mark.parametrize("output_name", ["report.txt", "report", "report.json.txt"])
def test_output_requires_json_extension(
    tmp_path: Path,
    output_name: str,
) -> None:
    module = _load_harness()
    output = tmp_path / output_name

    exit_code, report, prepared_output = module._run(
        [
            "--corpus-dir",
            str(tmp_path / "missing-corpus"),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 70
    assert report["verdict"] == "HARNESS_INTERNAL_ERROR"
    assert prepared_output is None
    assert not output.exists()
    _assert_aggregate_only(report)


@pytest.mark.unit
def test_project_output_is_only_allowed_under_report_artifact_roots() -> None:
    module = _load_harness()
    allowed = (
        _REPOSITORY_ROOT / "reports" / "phase0-contract-test.json",
        _REPOSITORY_ROOT / "artifacts" / "phase0-contract-test.json",
        _REPOSITORY_ROOT / ".artifacts" / "phase0-contract-test.json",
    )
    try:
        for output in allowed:
            assert not output.exists()
            prepared = module._prepare_output_path(
                output,
                corpus_dir=_REPOSITORY_ROOT / "private-corpus-outside-project",
                asset_dir=None,
            )
            assert prepared == output.absolute()
    finally:
        for output in allowed:
            assert not output.exists()


@pytest.mark.unit
def test_success_report_is_atomically_written_with_stdout_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_harness()
    corpus = tmp_path / "corpus"
    output = tmp_path / "reports" / "gate.json"
    fake_cases = _corpus_cases_for_pure_evaluation(module)
    monkeypatch.setattr(
        module,
        "_load_manifest",
        lambda _corpus_dir: (fake_cases, 3, 2, 3),
    )
    monkeypatch.setattr(module, "_resolve_node", lambda _node: "fake-node")

    async def evaluate(
        _cases,
        *,
        speaker_count: int,
        device_class_count: int,
        scenario_count: int,
        asset_dir,
        node: str,
        run_id: str,
    ):
        assert speaker_count == 3
        assert device_class_count == 2
        assert scenario_count == 3
        assert asset_dir is None
        assert node == "fake-node"
        report = module._empty_report("PASS_KEEP_16K_CONTRACT", run_id=run_id)
        report.update(
            {
                "speaker_count": 3,
                "case_count": 3,
                "device_class_count": 2,
                "scenario_count": 3,
                "decision_count": 15,
            }
        )
        return module.ExitCode.PASS, report

    monkeypatch.setattr(module, "_evaluate", evaluate)
    exit_code = module.main(
        [
            "--corpus-dir",
            str(corpus),
            "--output",
            str(output),
        ]
    )
    stdout_report = json.loads(capsys.readouterr().out)
    file_report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert stdout_report == file_report
    assert stdout_report["run_id"] == file_report["run_id"]
    assert len(stdout_report["run_id"]) == 32
    assert not list(output.parent.glob(f".{output.name}.*.tmp"))
    _assert_aggregate_only(file_report)


@pytest.mark.unit
def test_lazy_campplus_import_failure_maps_to_exit_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    real_import = builtins.__import__

    def fail_campplus_import(name, *args, **kwargs):
        if name == "main_logic.asr_client.speaker_shadow.campplus":
            raise ImportError("private-campplus-import-canary")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_campplus_import)
    with pytest.raises(module._HarnessFailure) as raised:
        module._create_model(None)

    assert int(raised.value.exit_code) == 4
    assert raised.value.verdict == "CAMPPLUS_MODEL_UNAVAILABLE"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lazy_runtime_preprocessor_import_failure_maps_to_exit_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    real_import = builtins.__import__

    def fail_runtime_import(name, *args, **kwargs):
        if name == "main_logic.voice_turn.audio_input":
            raise ImportError("private-runtime-import-canary")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_runtime_import)
    with pytest.raises(module._HarnessFailure) as raised:
        await module._run_runtime_path(bytearray(b"\x00\x00"))

    assert int(raised.value.exit_code) == 5
    assert raised.value.verdict == "RUNTIME_PREPROCESSOR_UNAVAILABLE"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_server_path_uses_enrollment_normalizer_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()
    from main_logic.voice_identity_service import enrollment_audio

    calls: dict[str, object] = {}

    class FakeNormalizer:
        def __init__(self, *, nr_enabled: bool) -> None:
            calls["nr_enabled"] = nr_enabled

        async def normalize(
            self,
            pcm16: bytes,
            *,
            sample_rate_hz: int,
            target_samples: int,
        ) -> bytes:
            calls.update(
                pcm16=pcm16,
                sample_rate_hz=sample_rate_hz,
                target_samples=target_samples,
            )
            return b"\x00\x00" * target_samples

    monkeypatch.setattr(
        enrollment_audio,
        "EnrollmentAudioNormalizer",
        FakeNormalizer,
    )
    source = bytearray(b"private-source-pcm-canary")
    result = await module._run_server_normalized_path(source)

    assert calls == {
        "nr_enabled": True,
        "pcm16": bytes(source),
        "sample_rate_hz": 48_000,
        "target_samples": 48_000,
    }
    assert len(result) == 96_000
    result[:] = b"\x00" * len(result)
    source[:] = b"\x00" * len(source)


@pytest.mark.unit
def test_real_node_runner_rejects_three_seconds_and_accepts_three_point_one() -> None:
    module = _load_harness()
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    exact_three_seconds = bytearray(b"\x00\x00" * (48_000 * 3))
    three_point_one_seconds = bytearray(b"\x00\x00" * (48_000 * 31 // 10))

    with pytest.raises(module._HarnessFailure) as raised:
        module._run_browser_path(exact_three_seconds, node=node)
    output = module._run_browser_path(three_point_one_seconds, node=node)

    assert int(raised.value.exit_code) == 3
    assert len(output) == 16_000 * 3 * 2
    assert type(output) is bytearray
    output[:] = b"\x00" * len(output)
    exact_three_seconds[:] = b"\x00" * len(exact_three_seconds)
    three_point_one_seconds[:] = b"\x00" * len(three_point_one_seconds)


@pytest.mark.unit
def test_unexpected_failure_has_internal_error_exit_code_and_private_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_harness()

    def fail_unexpectedly(_corpus_dir: Path):
        raise RuntimeError("private-internal-error-canary")

    monkeypatch.setattr(module, "_load_manifest", fail_unexpectedly)
    exit_code, report, _output = module._run(
        ["--corpus-dir", str(tmp_path / "private-corpus-canary")]
    )

    assert exit_code == 70
    assert report["verdict"] == "HARNESS_INTERNAL_ERROR"
    _assert_aggregate_only(
        report,
        secrets=("private-internal-error-canary", "private-corpus-canary"),
    )
