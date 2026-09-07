from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME = _REPO_ROOT / "main_logic" / "asr_client" / "runtime.py"
_DETECTOR_RUNTIME = (
    _REPO_ROOT / "main_logic" / "asr_client" / "endpointing" / "detector_runtime.py"
)
_COMPOSITION = (
    _REPO_ROOT / "main_logic" / "voice_identity_service" / "asr_composition.py"
)
_ADMISSION_CONTRACTS = (
    _REPO_ROOT / "main_logic" / "asr_client" / "admission" / "contracts.py"
)
_ASR_PUBLIC = _REPO_ROOT / "main_logic" / "asr_client" / "__init__.py"
_OPENAI_WORKER = (
    _REPO_ROOT / "main_logic" / "asr_client" / "workers" / "openai.py"
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _named_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(matches) == 1, f"expected exactly one {name}, got {len(matches)}"
    return matches[0]


def _call_attribute_names(node: ast.AST) -> set[str]:
    return {
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }


def test_runtime_has_no_legacy_admission_decision_state() -> None:
    source = _RUNTIME.read_text(encoding="utf-8")
    forbidden = {
        "_CandidateRejectionSuppression",
        "_SpeakerCandidateDecisionGate",
        "_SpeakerCandidateArmOperation",
        "_SpeakerCandidateDecisionPreparation",
        "_SpeakerEvidenceBridgeRecord",
        "_ProviderBoundarySnapshotRecord",
        "_asr_candidate_rejection",
        "_asr_suppressed_final_key",
        "_asr_reserved_final_key",
        "_asr_accepted_final_keys",
        "_asr_provider_boundary_snapshots",
        "_asr_provider_boundary_overflow_keys",
        "_asr_ordered_provider_keys",
        "_asr_completed_provider_keys",
        "_asr_provider_seal_fail_open_key",
        "_request_speaker_candidate_decision_arm",
        "request_speaker_candidate_rejection",
        "_resolve_speaker_candidate_decision",
        "_wait_for_speaker_candidate_decision",
    }

    assert not (forbidden & set(source.split()))
    for symbol in forbidden:
        assert symbol not in source
    for direct_mutation in (
        "_asr_admission.open_turn",
        "_asr_admission.post",
        "_asr_admission.invalidate_all",
        "_asr_admission.retire",
    ):
        assert direct_mutation not in source


def test_final_handler_only_posts_provider_final_to_admission() -> None:
    handler = _named_function(_tree(_RUNTIME), "_handle_independent_asr_final")
    calls = _call_attribute_names(handler)
    constructed = {
        call.func.id
        for call in ast.walk(handler)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }

    assert "ProviderFinalReceived" in constructed
    assert "_post_admission_event" in calls
    assert not ({"try_reserve", "release", "submit", "resolve_reserved"} & calls)


def test_runtime_resolves_dispatcher_only_from_typed_settlement_owners() -> None:
    tree = _tree(_RUNTIME)
    owners: list[str] = []
    for function in (
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if "resolve_reserved" in _call_attribute_names(function):
            owners.append(function.name)

    assert owners == [
        "_resolve_admission_reservation",
        "_finish_speaker_deny_cleanup",
        "_settle_published_provider_turn_ownership",
    ]


def test_speaker_completion_has_no_forward_authority_or_runtime_import_cycle() -> None:
    tree = _tree(_COMPOSITION)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    source = _COMPOSITION.read_text(encoding="utf-8")

    assert "main_logic.asr_client.runtime" not in imported_modules
    assert "AdmissionDisposition" not in source
    assert "ResolveReserved" not in source
    assert "FORWARD" not in source


def test_runtime_and_detector_have_no_provider_name_branch() -> None:
    for path in (_RUNTIME, _DETECTOR_RUNTIME):
        for comparison in (
            node for node in ast.walk(_tree(path)) if isinstance(node, ast.Compare)
        ):
            constants = {
                child.value
                for child in ast.walk(comparison)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
            assert "qwen" not in {value.casefold() for value in constants}


def test_provider_neutral_contracts_contain_no_provider_item_identifier() -> None:
    source = _ADMISSION_CONTRACTS.read_text(encoding="utf-8").casefold()

    assert "item_id" not in source
    assert "response_id" not in source
    assert "provider_item" not in source


def test_public_endpoint_callback_remains_no_argument_and_openai_exact_is_disabled() -> (
    None
):
    public_source = _ASR_PUBLIC.read_text(encoding="utf-8")
    openai_source = _OPENAI_WORKER.read_text(encoding="utf-8")

    assert public_source.count(
        "on_turn_endpointed: Callable[[], Awaitable[None]] | None = None"
    ) >= 2
    assert 'kind="provider_endpoint"' not in openai_source
    assert "boundary_quality=\"exact\"" not in openai_source
