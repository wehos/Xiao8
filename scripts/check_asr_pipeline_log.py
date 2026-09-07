"""Summarize ASR diagnostic evidence; missing observations are never success."""

from __future__ import annotations

import argparse
import ast
from collections import OrderedDict
import json
from pathlib import Path
import re

_REF = re.compile(r"[a-f0-9]{24}\Z")
_LABEL = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,79}\Z")
_KEYS = frozenset({
    "stage", "phase", "reason", "reason_code", "failed_check", "outcome", "state",
    "disposition", "dispatcher_applied", "endpoint_authority", "speaker_enabled",
    "speaker_classification", "score_outcome", "terminal_reason", "candidate_role",
    "turn_id", "session_epoch", "provider_generation", "provider_buffer_epoch",
    "provider_utterance_id", "detector_epoch", "semantic_turn_id", "semantic_generation",
    "sequence_no", "speaker_sequence_no", "diagnostic_records_dropped", "has_text",
    "frame_count", "audio_samples", "pipeline_schema", "observed_at_ns",
    "source_session_epoch", "component", "cleanup_outcome", "residual_count",
    "audio_generation", "route_generation", "lease_generation", "residual_components",
})


def parse_record(line: str) -> dict | None:
    marker = next((m for m in ("ASR resolution ", "ASR incident ", "ASR cleanup ") if m + "{" in line), None)
    if len(line) > 65_536 or marker is None:
        return None
    try:
        record = ast.literal_eval(line.split(marker, 1)[1].strip())
        if type(record) is not dict or not _REF.fullmatch(str(record.get("diagnostic_session_ref", ""))):
            return None
        result = {k: v for k, v in record.items() if k in _KEYS and (
            v is None or type(v) in (int, bool) or type(v) is str and _LABEL.fullmatch(v)
        )}
        result["diagnostic_session_ref"] = record["diagnostic_session_ref"]
        if marker == "ASR incident ":
            result["stage"] = "incident"
            result["session_epoch"] = record.get("source_session_epoch", record.get("session_epoch"))
        return result
    except (ValueError, SyntaxError, TypeError, RecursionError):
        return None


def summarize(lines, *, max_sessions=16, max_records=512) -> dict:
    sessions = OrderedDict()
    truncated = False
    for line in lines:
        record = parse_record(line)
        if record is None:
            continue
        ref = record["diagnostic_session_ref"]
        if ref not in sessions:
            if len(sessions) >= max_sessions:
                sessions.popitem(last=False)
                truncated = True
            sessions[ref] = {"records": [], "truncated": False, "drops": False}
        session = sessions[ref]
        sessions.move_to_end(ref)
        session["drops"] |= bool(record.get("diagnostic_records_dropped"))
        if len(session["records"]) >= max_records:
            session["records"].pop(0)
            session["truncated"] = True
        session["records"].append(record)
    reports = []
    for ref, session in sessions.items():
        records = session["records"]
        stages = {r.get("stage") for r in records}
        phases = {r.get("phase") for r in records if r.get("stage") == "endpoint_diagnostic"}
        policies = {r.get("endpoint_authority") for r in records if r.get("endpoint_authority")}
        provider_only = policies == {"provider"}
        coverage = {
            "audio_input": "observed" if "audio_received" in stages else "not_observed",
            "audio_write": "observed" if "provider_audio_written" in stages else "not_observed",
            "vad": "observed" if phases & {"vad_activity", "vad_load", "vad_feed"} or "vad_activity" in stages else "not_observed",
            "smart_turn": "observed" if "evaluation_result" in phases else "not_applicable" if provider_only else "not_observed",
            "asr_final": "observed" if stages & {"asr_final_received", "provider_final_received"} else "not_observed",
            "speaker": "observed" if stages & {"speaker_fact_observed", "speaker_capture_closed"} else "not_observed",
            "admission": "observed" if "admission_decision" in stages else "not_observed",
            "core_delivery": "observed" if "core_voice_delivery" in stages else "not_observed",
        }
        # Provider alias mapping is learned only from records explicitly carrying
        # both identities. Never assign unkeyed session events to the latest turn.
        identities = {}
        for r in records:
            if type(r.get("turn_id")) is int and all(type(r.get(k)) is int for k in ("audio_generation", "route_generation", "lease_generation")):
                identities.setdefault(r["turn_id"], set()).add(_turn_key(r))
        ambiguous = {turn for turn, keys in identities.items() if len(keys) > 1}
        alias = {}
        for r in records:
            if type(r.get("turn_id")) is int and r["turn_id"] not in ambiguous and type(r.get("provider_utterance_id")) is int:
                alias[_provider_key(r)] = r["turn_id"]
        turns = OrderedDict()
        for r in records:
            turn_id = r.get("turn_id")
            if turn_id is None:
                turn_id = alias.get(_provider_key(r))
            if turn_id is not None:
                known = identities.get(turn_id, set())
                if len(known) == 1:
                    key = next(iter(known))
                elif len(known) > 1:
                    key = _turn_key(r)
                    if key not in known:
                        continue
                else:
                    key = (turn_id, None, None, None)
                turns.setdefault(key, []).append(r)
        turn_reports = []
        for turn_key, events in turns.items():
            decisions = [r for r in events if r.get("stage") == "admission_decision"]
            core = [r for r in events if r.get("stage") == "core_voice_delivery" and r.get("outcome") not in {"started", "accepted"}]
            findings = []
            for r in events:
                if r.get("failed_check") or r.get("stage", "").endswith("ignored"):
                    findings.append({k: r[k] for k in ("stage", "failed_check", "reason") if k in r})
            turn_reports.append({
                "turn_id": turn_key[0], "audio_generation": turn_key[1],
                "route_generation": turn_key[2], "lease_generation": turn_key[3],
                "admission": decisions[-1].get("disposition") if decisions else "not_observed",
                "reason": decisions[-1].get("reason_code") if decisions else None,
                "core_outcome": core[-1].get("outcome") if core else "not_observed",
                "core_phase": core[-1].get("phase") if core else None,
                "findings": findings,
            })
        reports.append({
            "session_ref": ref, "session_epoch": records[-1].get("session_epoch"),
            "coverage": coverage, "log_gaps": session["drops"] or session["truncated"],
            "ambiguous_partial_turn_ids": sorted(ambiguous),
            "turns": turn_reports,
            "session_findings": [r for r in records if r.get("stage", "").endswith("ignored")
                                 or r.get("stage") in {"endpoint_diagnostic", "incident"}
                                 or r.get("stage", "").startswith("cleanup_")],
        })
    return {"schema": 1, "sessions_truncated": truncated, "sessions": reports,
            "interpretation": "not_observed means insufficient evidence; submitted means request submitted, not reply played"}


def _provider_key(record):
    return tuple(record.get(k) for k in ("provider_generation", "provider_buffer_epoch", "provider_utterance_id"))


def _turn_key(record):
    return tuple(record.get(k) for k in ("turn_id", "audio_generation", "route_generation", "lease_generation"))


def main() -> None:
    parser = argparse.ArgumentParser(description="检查 ASR/VAD/SmartTurn/声纹日志证据，缺失记录不视为通过")
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with args.log.open(encoding="utf-8", errors="replace") as stream:
        report = summarize(stream)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
