"""External-event simulation router (P25 §3 Day 1).

Endpoints
---------
* ``POST /api/session/external-event`` — simulate one event of the three
  kinds (avatar / agent_callback / proactive). Body:
  ``{kind, payload, mirror_to_recent?}``. Long-running (LLM round trip);
  holds the per-session BUSY lock for the duration so a concurrent
  ``/chat/send`` or ``auto_dialog/start`` cannot corrupt interleaved
  messages. Returns :class:`SimulationResult.to_dict`.
* ``GET /api/session/external-event/dedupe-info`` — read-only snapshot
  of the avatar dedupe cache. Cheap; does not acquire the session lock.
* ``POST /api/session/external-event/dedupe-reset`` — clear the avatar
  dedupe cache and rearm the overflow notice. Mutating; takes the BUSY
  lock briefly.

Design notes
------------
* **BUSY lock, never AbortController (L19)**: the simulation appends
  several ``session.messages`` records and may write to ``recent.json``.
  Allowing the browser to abort mid-call would leave some of those
  writes applied and others not — the exact footgun L19 flags. We hold
  the lock for the full LLM round trip (up to a minute) and rely on
  upstream ``timeout`` to bound it rather than a client-driven abort.
* **Single entry point for three kinds**: keeps API surface small and
  makes the UI tab switcher trivial (one fetch helper shared across
  three forms). See P25_BLUEPRINT §2.6 "三类一个抽象".
* **Frontend mutation rule (L19) enforcement is the UI's job** —
  see :mod:`static/ui/chat/external_events_panel.js` Day 2 for the
  no-AbortController convention on the three Simulate buttons.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from tests.testbench.pipeline.external_events import (
    SimulationKind,
    build_external_event_preview,
    peek_dedupe_info,
    reset_dedupe,
    simulate_agent_callback,
    simulate_avatar_interaction,
    simulate_proactive,
)
from tests.testbench.session_store import (
    SessionConflictError,
    SessionState,
    get_session_store,
)

router = APIRouter(prefix="/api/session", tags=["external-event"])


# ── request / response schemas ───────────────────────────────────────


class _ExternalEventRequest(BaseModel):
    """POST /api/session/external-event body."""

    kind: str = Field(
        ...,
        description=(
            "事件类型; 必须是 avatar / agent_callback / proactive 之一."
        ),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "事件 payload, 结构随 kind 变化 (详见 SimulationResult 文档 + "
            "P25_BLUEPRINT §2.1). avatar 需 {interaction_id, tool_id, "
            "action_id, target='avatar', intensity?, ...}; agent_callback "
            "需 {callbacks: [str | {text:str}, ...]}; proactive 需 "
            "{kind: home|screenshot|window|news|community|video|personal|music}."
        ),
    )
    mirror_to_recent: bool = Field(
        default=False,
        description=(
            "P25_BLUEPRINT §2.4 opt-in. 勾上则把本次事件产出的 memory "
            "pair 额外写入 memory/recent.json (默认只写 session.messages)."
        ),
    )


class _DedupeResetResponse(BaseModel):
    cleared: int = Field(..., description="清除前 cache 中的条目数.")


class _ExternalEventPreviewRequest(BaseModel):
    """POST /api/session/external-event/preview body — dry-run only."""

    kind: str = Field(
        ...,
        description=(
            "事件类型; 同 /external-event, avatar / agent_callback / "
            "proactive 之一."
        ),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "与 /external-event 完全同构的 payload; 本 endpoint 不写 "
            "session.messages / last_llm_wire / dedupe 缓存, 也不调 "
            "LLM — 仅把 \"这次 /external-event 会构造什么样的 wire\" "
            "同步返回, 供 UI 发送前预览."
        ),
    )


# ── helpers ──────────────────────────────────────────────────────────


def _session_conflict_to_http(exc: SessionConflictError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error_type": "SessionConflict",
            "message": str(exc),
            "state": exc.state.value,
            "busy_op": exc.busy_op,
        },
    )


def _lookup_error_to_http(exc: LookupError) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error_type": "NoActiveSession", "message": str(exc)},
    )


def _parse_kind(raw: str) -> SimulationKind:
    try:
        return SimulationKind(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "InvalidKind",
                "message": (
                    f"kind={raw!r} 不受支持; 合法值: "
                    f"{sorted(k.value for k in SimulationKind)}."
                ),
            },
        ) from exc


# ── endpoints ────────────────────────────────────────────────────────


@router.post("/external-event")
async def post_external_event(body: _ExternalEventRequest) -> dict[str, Any]:
    """Run one simulation handler end-to-end and return a ``SimulationResult``.

    Holds the per-session BUSY lock for the full duration. Client callers
    MUST NOT attach an ``AbortController`` (P25 L19): aborting midway
    would leave ``session.messages`` / ``recent.json`` partially written.
    """
    kind = _parse_kind(body.kind)

    store = get_session_store()
    try:
        async with store.session_operation(
            f"external_event.{kind.value}",
            state=SessionState.BUSY,
        ) as session:
            if kind == SimulationKind.AVATAR:
                result = await simulate_avatar_interaction(
                    session,
                    body.payload,
                    mirror_to_recent=body.mirror_to_recent,
                )
            elif kind == SimulationKind.AGENT_CALLBACK:
                result = await simulate_agent_callback(
                    session,
                    body.payload,
                    mirror_to_recent=body.mirror_to_recent,
                )
            elif kind == SimulationKind.PROACTIVE:
                result = await simulate_proactive(
                    session,
                    body.payload,
                    mirror_to_recent=body.mirror_to_recent,
                )
            else:  # pragma: no cover — _parse_kind guards above
                raise HTTPException(status_code=400, detail={
                    "error_type": "InvalidKind",
                    "message": f"unexpected kind={kind!r}",
                })
            # Wire shape: flat SimulationResult fields + a sibling "kind"
            # discriminator at the top level. P25_BLUEPRINT §2.6 "一个响应
            # 结构 — SimulationResult dataclass 三类共用, UI 前端按字段渲染
            # 不按 kind 分支": return SimulationResult keys directly; do NOT
            # wrap them inside a "result" envelope (that was the L36 shape
            # drift bug caught during 2026-04-23 manual P25 Day 2 UI
            # testing — the UI's ``state.lastResult = resp.data`` then
            # accessed ``r.accepted`` / ``r.persisted`` which were
            # ``undefined`` under the envelope, silently surfacing
            # "未处理 / 未写入 session.messages / 本次未产出 assistant
            # reply" for every successful simulation).
            #
            # ``kind`` is kept as a top-level sibling for audit/log clarity
            # (identical to what the UI already tracks locally); it's OK
            # for SimulationResult to never carry ``kind`` internally —
            # that keeps the dataclass clean (three handlers share one
            # shape) and leaves wire composition to the router.
            return {"kind": kind.value, **result.to_dict()}
    except SessionConflictError as exc:
        raise _session_conflict_to_http(exc) from exc
    except LookupError as exc:
        raise _lookup_error_to_http(exc) from exc


@router.post("/external-event/preview")
async def post_external_event_preview(
    body: _ExternalEventPreviewRequest,
) -> dict[str, Any]:
    """Return a dry-run snapshot of the wire ``simulate_<kind>(session, payload)``
    would emit — **without** touching ``session.messages``, ``last_llm_wire``,
    or the dedupe cache, and **without** calling the LLM.

    Contract (L36 §7.25 第 5 层 "预览=真实"):

      The returned ``wire_preview[-1]["content"]`` MUST equal the ``content``
      field of the wire entry that a subsequent ``POST /external-event`` call
      with the same ``(kind, payload)`` would record via
      :func:`tests.testbench.pipeline.wire_tracker.record_last_llm_wire`
      before invoking the LLM. Both the preview and the real-send paths
      route through the same ``_build_<kind>_instruction_bundle`` helper in
      :mod:`tests.testbench.pipeline.external_events` to guarantee this
      byte-identical correspondence.

    Does **not** acquire ``session_operation`` — a preview is a pure read,
    and we don't want a tester clicking \"preview\" to compete with an
    in-flight ``/chat/send`` for the BUSY lock.
    """
    kind = _parse_kind(body.kind)

    store = get_session_store()
    session = store.get()
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_type": "NoActiveSession",
                "message": "No active session; create one via POST /api/session first.",
            },
        )

    preview = build_external_event_preview(session, kind, body.payload or {})

    # to_dict shape mirrors SimulationResult.to_dict — flat fields, no
    # envelope. Fields a UI panel cares about: wire_preview (list of
    # {role, content}), instruction_final (str), instruction_template_raw
    # (str, for "模板 vs 填完后的差异" toggle if ever added), coerce_info
    # (list of rule-application notes to render in a hint strip). When
    # the preview fails pre-LLM (invalid_payload / empty_callbacks /
    # persona_not_ready), ``reason`` / ``error_code`` / ``error_message``
    # surface the cause so the UI can render "why I can't preview".
    return {
        "kind": preview.kind,
        "instruction_template_raw": preview.instruction_template_raw,
        "instruction_final": preview.instruction_final,
        "wire_preview": preview.wire_preview,
        "coerce_info": [
            {
                "field": info.field,
                "requested": info.requested,
                "applied": info.applied,
                "note": info.note,
            }
            for info in preview.coerce_info
        ],
        "reason": preview.reason,
        "error_code": preview.error_code,
        "error_message": preview.error_message,
    }


@router.get("/external-event/dedupe-info")
async def get_dedupe_info() -> dict[str, Any]:
    """Read-only cache snapshot; does NOT acquire the session lock.

    Cheap enough to poll from the UI while a simulation is in flight,
    which is the whole point — tester wants to see "the cache has 3
    entries, the one I just posted is at ts=...".
    """
    store = get_session_store()
    session = store.get()
    if session is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_type": "NoActiveSession",
                "message": "No active session; create one via POST /api/session first.",
            },
        )
    return {"kind": "avatar", "info": peek_dedupe_info(session)}


@router.post("/external-event/dedupe-reset", response_model=_DedupeResetResponse)
async def post_dedupe_reset() -> _DedupeResetResponse:
    """Clear the avatar dedupe cache for the active session.

    Takes the BUSY lock briefly; tester usually hits this only while no
    simulation is running, but the lock is cheap and avoids a subtle race
    where a clear interleaves with a should_persist call from a concurrent
    simulation and resurrects a just-evicted key.
    """
    store = get_session_store()
    try:
        async with store.session_operation(
            "external_event.dedupe_reset",
            state=SessionState.BUSY,
        ) as session:
            summary = reset_dedupe(session)
            cleared = int(summary.get("cleared", 0))
            # P25 Day 2 hotfix: audit trail. session_operation() 只是把
            # "external_event.dedupe_reset" 作为 busy_op 标签占锁, 不写
            # JSONL. 但 tester 需要在 Logs 页看到 "何时重置过缓存" 才能
            # 复现 "为什么上一次 avatar 事件没被去重" 这类调试问题. 本
            # 行把动作写入 session JSONL, 和其它 external_event.* 家族
            # 保持对称 (avatar/agent_callback/proactive 都走 log_sync).
            session.logger.log_sync(
                "external_event.dedupe_reset",
                payload={"cleared": cleared},
            )
            return _DedupeResetResponse(cleared=cleared)
    except SessionConflictError as exc:
        raise _session_conflict_to_http(exc) from exc
    except LookupError as exc:
        raise _lookup_error_to_http(exc) from exc


__all__ = ["router"]
