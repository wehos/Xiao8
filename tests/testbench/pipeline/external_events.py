"""External-event simulation handlers (P25 §2.1/§3 Day 1 + Day 2 polish r5).

Reproduce the three main-program "runtime prompt injection + write memory"
external event classes so a tester can study their impact on LLM reply,
recent-history compression, facts extraction, and reflection:

* **avatar**: avatar tool interactions (PR #769) producing a user-role
  ``[主人摸了摸你的头]``-style memory note + an LLM reaction reply.
* **agent_callback**: background agent task completion producing an
  ``AGENT_CALLBACK_NOTIFICATION`` instruction prefix driving a target reply.
* **proactive**: time/queue-idle proactive chat producing an LLM-emitted
  opener or ``[PASS]`` skip signal.

Semantic reproduction scope (P25_BLUEPRINT §1.2 / §1.3)
-------------------------------------------------------
What we DO reproduce (LESSONS_LEARNED §1.6 semantic contract):

* Avatar prompt assembly via the nine ``config.prompts.prompts_avatar_interaction``
  pure helpers + seven constant tables (no re-implementation).
* Agent-callback instruction prefix via ``AGENT_CALLBACK_NOTIFICATION``
  five-language dict.
* Proactive dispatch via ``get_proactive_chat_prompt(kind, lang)`` across
  the seven kinds and five languages.
* Avatar dedupe semantics via the copy-protected region of
  :mod:`tests.testbench.pipeline.avatar_dedupe` (8000 ms window + rank
  upgrade short-circuit).
* ``append_message`` choke-point for every write into
  ``session.messages`` (A17 / messages_writer invariant).

What we do NOT reproduce (runtime mechanism, intentionally out-of-scope):

* WebSocket / multi-process queue transport.
* Real-time jitter cooling, N-second trigger-window cooling, and
  ``merge_unsynced_tail_assistants`` tail-merging heuristics.
* Actual avatar canvas click flow — a manual click is always a single
  request; dedupe repeats are exercised via tester re-posts.

Message sourcing convention
---------------------------
We re-use two existing sources from :mod:`tests.testbench.chat_messages`
rather than introducing a P25-only enum value, so the messages roundtrip
through persistence / export without schema migrations:

* user-role memory notes → ``SOURCE_INJECT`` (a system-origin user-facing
  line, exactly matching ``chat/inject_system`` intent).
* assistant replies → ``SOURCE_LLM`` (the reply genuinely came from the
  target LLM stream).

The caller-facing discriminator "this was a simulated external event" is
carried by the :class:`SimulationResult` + the diagnostics op record, not
by the persisted message.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

from tests.testbench.chat_messages import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
    SOURCE_EXTERNAL_EVENT_BANNER,
    SOURCE_INJECT,
    SOURCE_LLM,
    make_message,
)
from tests.testbench.logger import python_logger
from tests.testbench.pipeline import diagnostics_store
from tests.testbench.pipeline.atomic_io import atomic_write_json
from tests.testbench.pipeline.avatar_dedupe import _AvatarDedupeCache
from tests.testbench.pipeline.chat_runner import ChatConfigError, resolve_group_config
from tests.testbench.pipeline.diagnostics_ops import DiagnosticsOp
from tests.testbench.pipeline.messages_writer import append_message
from tests.testbench.pipeline.prompt_builder import PreviewNotReady, build_prompt_bundle
from tests.testbench.pipeline.wire_tracker import (
    record_last_llm_wire,
    update_last_llm_wire_reply,
)
from tests.testbench.session_store import Session
from utils.llm_client import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    messages_to_dict,
)

# Map ``session.messages``'s role field → the corresponding LangChain
# message class so :func:`_apply_mirror_to_recent` can write
# ``recent.json`` in the main-program canonical on-disk shape
# ``{"type": "human"|"ai"|"system", "data": {"content": <str>}}``.
# Using the LangChain classes + ``messages_to_dict`` instead of hand-
# rolling the dict guarantees byte-exact round-trip with
# ``messages_from_dict`` (see ``utils.llm_client.py`` L71-L95).
_LANGCHAIN_ROLE_CLS: dict[str, type] = {
    "user": HumanMessage,
    "assistant": AIMessage,
    "system": SystemMessage,
}

# ─────────────────────────────────────────────────────────────
# Public value objects
# ─────────────────────────────────────────────────────────────


class SimulationKind(str, Enum):
    """The three event classes exposed by POST /api/session/external-event."""

    AVATAR = "avatar"
    AGENT_CALLBACK = "agent_callback"
    PROACTIVE = "proactive"


#: SimulationResult.reason — enumerated rejection / short-circuit reasons.
#: Only reproduces the semantic-contract layer of the main program; runtime
#: reasons like "websocket closed" / "queue full" are OOS per §1.3.
ReasonCode = Literal[
    "dedupe_window_hit",   # avatar: same dedupe_key inside 8000 ms window, same or lower rank
    "invalid_payload",     # avatar: authoritative payload normalizer returned None
    "empty_callbacks",     # agent_callback: no callback items in payload
    "pass_signaled",       # proactive: LLM returned [PASS] — recorded, not an error
    "llm_failed",          # wire assembled but target LLM call raised
    "persona_not_ready",   # build_prompt_bundle raised PreviewNotReady
    "chat_not_configured", # resolve_group_config raised ChatConfigError
]


@dataclass
class CoerceInfo:
    """Payload-level coercion surfacing (LESSONS_LEARNED §7.14).

    Supported non-avatar normalizations, such as prompt-language fallback,
    return the before/after pair here rather than hiding the applied value.
    Avatar payload contract violations are rejected as ``invalid_payload``.
    """

    field: str
    requested: Any
    applied: Any
    note: str = ""


@dataclass
class MirrorToRecentInfo:
    """Feature flag surfacing (LESSONS_LEARNED L17).

    ``requested`` tracks what the tester asked for; ``applied`` tracks
    what actually happened. A ``requested=True, applied=False`` pair
    MUST carry a ``fallback_reason`` string so the UI never silently
    drops a persistence intent.
    """

    requested: bool
    applied: bool
    fallback_reason: Optional[str] = None


@dataclass
class SimulationResult:
    """One-shot response for any of the three simulate_* handlers.

    Fields:

    * ``accepted`` — did we actually move any state (append a message /
      write memory / drive the LLM)? ``False`` on dedupe / pass / error.
    * ``reason`` — populated iff ``accepted=False`` (see :data:`ReasonCode`).
    * ``instruction`` — the wire-only prompt string that was sent to the
      LLM for this event (avatar event fact, agent_callback prefix,
      or proactive prompt). Returned for UI preview / audit. NEVER enters
      ``session.messages`` (P25_BLUEPRINT §A.8 #2).
    * ``memory_pair`` — the ``{user, assistant}`` message pair persisted
      when ``accepted=True`` (avatar always, agent_callback never on user
      side, proactive assistant-only). Entries are the full message dicts
      as returned by :func:`make_message` post ``append_message``.
    * ``persisted`` — quick bool: "did we write anything to session.messages".
    * ``dedupe_info`` — per-request dedupe snapshot: ``{hit, remaining_ms,
      cache_size}`` when avatar, ``None`` otherwise.
    * ``assistant_reply`` — the raw LLM reply text (before it becomes a
      message); useful to render immediately in the UI even when we also
      persisted it to ``session.messages``.
    * ``coerce_info`` — list of ``CoerceInfo`` for every silent payload
      correction this request made. Empty list = nothing coerced.
    * ``mirror_to_recent_info`` — :class:`MirrorToRecentInfo` triplet
      always returned (even when requested=False) so the UI can render a
      three-state "off / on-applied / on-fallback" badge uniformly.
    * ``elapsed_ms`` — wall clock from handler entry to handler return.
    """

    accepted: bool
    reason: Optional[ReasonCode] = None
    instruction: str = ""
    memory_pair: list[dict[str, Any]] = field(default_factory=list)
    persisted: bool = False
    dedupe_info: Optional[dict[str, Any]] = None
    assistant_reply: str = ""
    coerce_info: list[CoerceInfo] = field(default_factory=list)
    mirror_to_recent_info: MirrorToRecentInfo = field(
        default_factory=lambda: MirrorToRecentInfo(requested=False, applied=False),
    )
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe serialization for the router response."""
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "instruction": self.instruction,
            "memory_pair": list(self.memory_pair),
            "persisted": self.persisted,
            "dedupe_info": dict(self.dedupe_info) if self.dedupe_info else None,
            "assistant_reply": self.assistant_reply,
            "coerce_info": [asdict(c) for c in self.coerce_info],
            "mirror_to_recent_info": asdict(self.mirror_to_recent_info),
            "elapsed_ms": self.elapsed_ms,
        }


# ─────────────────────────────────────────────────────────────
# Per-session dedupe cache registry
# ─────────────────────────────────────────────────────────────

# We intentionally keep the cache off ``Session`` — avoiding a dataclass
# field touches zero persistence / export / diagnostics code. The cache is
# pure in-memory scratch that is fine to forget on process restart.
_DEDUPE_CACHES: dict[str, _AvatarDedupeCache] = {}


def _get_dedupe_cache(session: Session) -> _AvatarDedupeCache:
    """Return (or lazily create) the ``_AvatarDedupeCache`` for this session."""
    cache = _DEDUPE_CACHES.get(session.id)
    if cache is None:
        cache = _AvatarDedupeCache(on_full=_make_cache_full_notifier(session))
        _DEDUPE_CACHES[session.id] = cache
    return cache


def _make_cache_full_notifier(session: Session):
    """Bind a ``(cap: int) -> None`` hook that logs AVATAR_DEDUPE_CACHE_FULL."""
    session_id = session.id

    def _notify(cap: int) -> None:
        try:
            diagnostics_store.record_internal(
                DiagnosticsOp.AVATAR_DEDUPE_CACHE_FULL,
                (
                    f"avatar 事件去重缓存达到软上限 {cap} 条, LRU 丢弃最旧条目. "
                    "调 POST /api/session/external-event/dedupe-reset 清空即可 rearm."
                ),
                level="warning",
                session_id=session_id,
                detail={"max_entries": cap},
            )
        except Exception:  # noqa: BLE001 — diagnostics must not break dedupe
            python_logger().exception(
                "external_events: record_internal(AVATAR_DEDUPE_CACHE_FULL) failed"
            )

    return _notify


def peek_dedupe_info(session: Session) -> dict[str, Any]:
    """Snapshot of the session's dedupe cache for the diagnostics GET."""
    cache = _get_dedupe_cache(session)
    return {
        "size": len(cache),
        "max_entries": _AvatarDedupeCache._MAX_ENTRIES,
        "entries": cache.snapshot(),
    }


def reset_dedupe(session: Session) -> dict[str, Any]:
    """Clear the session's dedupe cache and rearm the overflow notice."""
    cache = _get_dedupe_cache(session)
    size_before = len(cache)
    cache.clear()
    return {"cleared": size_before}


def discard_session_caches(session_id: str) -> None:
    """Drop any per-session caches. Callable from SessionStore.destroy."""
    _DEDUPE_CACHES.pop(session_id, None)


# ─────────────────────────────────────────────────────────────
# Shared LLM plumbing
# ─────────────────────────────────────────────────────────────


def _resolve_language(session: Session) -> tuple[str, str]:
    """Return ``(full_language, short_language)`` for prompt dispatch.

    ``full`` is the raw ``session.persona.language`` (default ``zh-CN``).
    ``short`` is normalised by the main program's
    :func:`config.prompts.prompts_proactive._normalize_prompt_language` — the
    single source of truth — so the testbench dispatches the exact same
    per-language templates as production. As of the 2026-06 upstream sync this
    set is ``zh/en/ja/ko/ru/es/pt``; es/pt became native (they previously fell
    back to English). Delegating keeps the testbench aligned automatically when
    upstream adds further languages, rather than hard-coding a divergent list.
    """
    from config.prompts.prompts_proactive import _normalize_prompt_language

    persona = session.persona or {}
    full = str(persona.get("language") or "zh-CN").strip()
    return full, _normalize_prompt_language(full)


def _resolve_names(session: Session) -> tuple[str, str]:
    """Return ``(character_name, master_name)``.

    ``master_name`` 缺省时返回空串，由调用方按场景自行兜底——避免在源头硬编码
    "主人"等物化称呼。memory_note 路径下游
    (:func:`config.prompts.prompts_avatar_interaction._build_avatar_interaction_memory_meta`)
    自带本地化中性词回退（zh="对方"、en="they" 等）。
    """
    persona = session.persona or {}
    lanlan = str(persona.get("character_name") or "").strip()
    master = str(persona.get("master_name") or "").strip()
    return lanlan, master


async def _invoke_llm_once(
    session: Session,
    wire_messages: list[dict[str, Any]],
) -> str:
    """Call the chat-group LLM once (non-streaming) and return the text reply.

    Mirrors :meth:`OfflineChatBackend.stream_send` config resolution but
    uses ``streaming=False`` so we get the full reply in one shot — the
    three simulate_* handlers are synchronous-style (request/response)
    rather than SSE streams, so streaming buys nothing.
    """
    cfg = resolve_group_config(session, "chat")
    from utils.llm_client import ChatOpenAI

    client = ChatOpenAI(
        model=cfg.model,
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        timeout=cfg.timeout or 60.0,
        max_retries=1,
        streaming=False,
    )
    try:
        # NOSTAMP(wire_tracker): shared helper — each of the 3 callers
        # (simulate_avatar_interaction / simulate_agent_callback /
        # simulate_proactive) stamps its own source + kind-specific note
        # immediately before calling this helper, because the note
        # content ("avatar:fist+poke@5" vs "agent_callback:3items@zh"
        # vs "proactive:time_passed@en") is caller-specific and would
        # force this helper to take ~4 extra kwargs. Coverage smoke
        # (p25_llm_call_site_stamp_coverage_smoke.py) recognizes this
        # sentinel and skips the call site.
        resp = await client.ainvoke(wire_messages)
        return (resp.content or "").strip()
    finally:
        try:
            await client.aclose()
        except Exception as close_exc:  # noqa: BLE001
            python_logger().debug(
                "external_events ChatOpenAI.aclose failed: %s", close_exc,
            )


def _build_base_wire(session: Session) -> list[dict[str, Any]]:
    """Build the base ``[system, *history]`` wire without any event-specific
    instruction attached. Handlers will tail-append their own instruction.
    """
    bundle = build_prompt_bundle(session)
    # We want a fresh copy so the handler can mutate (append the instruction)
    # without leaking back into the prompt builder cache.
    return [dict(m) for m in bundle.wire_messages]


# ─────────────────────────────────────────────────────────────
# Recent.json mirror (opt-in)
# ─────────────────────────────────────────────────────────────


def _apply_mirror_to_recent(
    session: Session,
    messages_to_mirror: list[dict[str, Any]],
    requested: bool,
) -> MirrorToRecentInfo:
    """Optionally append ``messages_to_mirror`` to the character's recent.json.

    P25_BLUEPRINT §2.4: **opt-in** semantics — caller asks via
    ``requested=True``; we surface ``applied`` + ``fallback_reason`` so a
    write failure is visible (L17 feature-flag surfacing), never silent.
    """
    if not requested:
        return MirrorToRecentInfo(requested=False, applied=False)

    character = (session.persona or {}).get("character_name") or ""
    character = str(character).strip()
    if not character:
        return MirrorToRecentInfo(
            requested=True,
            applied=False,
            fallback_reason="session.persona.character_name 为空; recent.json 路径无法解析",
        )
    if not messages_to_mirror:
        return MirrorToRecentInfo(
            requested=True,
            applied=False,
            fallback_reason="本次事件没有产生任何可 mirror 的消息 (dedupe/PASS 等)",
        )

    try:
        from utils.config_manager import get_config_manager  # deferred import — avoids startup cycle
    except Exception as exc:  # noqa: BLE001
        return MirrorToRecentInfo(
            requested=True,
            applied=False,
            fallback_reason=f"ConfigManager 不可用: {type(exc).__name__}: {exc}",
        )

    try:
        cm = get_config_manager()
        recent_path = Path(str(cm.memory_dir)) / character / "recent.json"
    except Exception as exc:  # noqa: BLE001
        return MirrorToRecentInfo(
            requested=True,
            applied=False,
            fallback_reason=f"ConfigManager.memory_dir 解析失败: {type(exc).__name__}: {exc}",
        )

    try:
        existing: list[dict[str, Any]] = []
        if recent_path.exists():
            import json
            with recent_path.open("r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, list):
                existing = [m for m in loaded if isinstance(m, dict)]
            # Invalid file: treat as empty; we refuse to overwrite arbitrary
            # content, so surface a fallback reason instead.
            elif loaded is not None:
                return MirrorToRecentInfo(
                    requested=True,
                    applied=False,
                    fallback_reason=(
                        f"recent.json 顶层非 list (实为 {type(loaded).__name__}); "
                        "拒绝覆盖, 请 tester 手动修复或从 Paths 子页删除"
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        return MirrorToRecentInfo(
            requested=True,
            applied=False,
            fallback_reason=f"recent.json 读取失败: {type(exc).__name__}: {exc}",
        )

    # Mirror entries must be written in the main-program canonical
    # ``messages_to_dict`` shape — ``{"type": "human"|"ai"|"system",
    # "data": {"content": <str>}}`` — because downstream consumers
    # (``memory_runner._preview_recent_compress`` at line 456,
    # ``memory_runner._preview_facts_extract`` at line 621, plus the
    # main program's own ``memory/recent.py``) all round-trip via
    # ``messages_from_dict(_read_json_list(recent_path))``. Writing our
    # testbench-internal ``{role, content:[{type:text,text:...}]}``
    # shape would pass ``isinstance(loaded, list)`` but then silently
    # fall back to ``HumanMessage(content=str(d))`` inside
    # ``messages_from_dict`` (``utils.llm_client.py`` L113-114) — the
    # content would read as the stringified dict ``"{'role': ...}"``
    # instead of the actual user/assistant text, breaking compress /
    # facts-extract without raising.
    #
    # Build LangChain messages via ``_ROLE_CLS`` (which maps
    # user→HumanMessage, assistant→AIMessage, system→SystemMessage),
    # then serialize with ``messages_to_dict``. This is exactly the
    # write path the main program uses in ``memory/recent.py``
    # (``cm.memory_dir / character / "recent.json"``).
    lc_messages: list[Any] = []
    for msg in messages_to_mirror:
        role = str(msg.get("role") or "user").strip().lower()
        cls = _LANGCHAIN_ROLE_CLS.get(role, _LANGCHAIN_ROLE_CLS["user"])
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list) and content and isinstance(content[0], dict):
            # testbench richer content shape — flatten to plain text
            text = str(content[0].get("text") or "")
        else:
            text = str(content or "")
        lc_messages.append(cls(content=text))
    mirrored = messages_to_dict(lc_messages)

    try:
        atomic_write_json(recent_path, existing + mirrored)
    except Exception as exc:  # noqa: BLE001
        return MirrorToRecentInfo(
            requested=True,
            applied=False,
            fallback_reason=f"recent.json 原子写入失败: {type(exc).__name__}: {exc}",
        )

    return MirrorToRecentInfo(requested=True, applied=True)


# ─────────────────────────────────────────────────────────────
# Handler: avatar interaction
# ─────────────────────────────────────────────────────────────


async def simulate_avatar_interaction(
    session: Session,
    payload: dict[str, Any],
    *,
    mirror_to_recent: bool = False,
) -> SimulationResult:
    """Simulate one avatar interaction (PR #769) end-to-end.

    Pipeline:
      1. Normalise the raw UI payload via the authoritative avatar-interaction
         contract. Invalid → reject.
      2. Dedupe via ``_AvatarDedupeCache.should_persist`` (8000 ms window
         + rank upgrade). Dedupe hit → reject (no LLM call, no messages).
      3. Build instruction via ``_build_avatar_interaction_instruction``
         and memory meta via ``_build_avatar_interaction_memory_meta``
         (both main-program helpers, reused verbatim).
      4. Append instruction to a **throwaway** wire (not session.messages).
      5. Call LLM once, get assistant reply.
      6. Write the pair (user memory_note, assistant reply) to
         session.messages through ``append_message``.
      7. If LLM fails AFTER dedupe reserved the slot, roll back the
         cache entry so the tester can retry without an 8-second wait.
      8. Optionally mirror to recent.json (opt-in).
    """
    started = time.perf_counter()

    # Shared helper between real send + preview — keeps instruction
    # construction byte-identical across both paths (L33 "跨路径不对称"
    # / L36 §7.25 第 5 层防御). Any future change to how we assemble the
    # avatar instruction MUST go through this builder to prevent the
    # "preview says one thing, LLM sees another" drift bug.
    bundle = _build_avatar_instruction_bundle(session, payload)
    if bundle is None:
        return _record_and_return(
            session,
            SimulationKind.AVATAR,
            SimulationResult(
                accepted=False,
                reason="invalid_payload",
                elapsed_ms=_elapsed(started),
            ),
            detail={"payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else None},
        )

    coerce_info = bundle.coerce_info
    normalized = bundle.avatar_normalized or {}
    memory_note = bundle.avatar_memory_note
    dedupe_key = bundle.avatar_dedupe_key
    dedupe_rank = bundle.avatar_dedupe_rank

    cache = _get_dedupe_cache(session)
    cache_size_before = len(cache)
    allowed = cache.should_persist(memory_note, dedupe_key, dedupe_rank)
    if not allowed:
        return _record_and_return(
            session,
            SimulationKind.AVATAR,
            SimulationResult(
                accepted=False,
                reason="dedupe_window_hit",
                coerce_info=coerce_info,
                dedupe_info={
                    "hit": True,
                    "cache_size": len(cache),
                    "dedupe_key": dedupe_key,
                    "dedupe_rank": dedupe_rank,
                },
                elapsed_ms=_elapsed(started),
            ),
            detail={"interaction_id": normalized.get("interaction_id"), "tool_id": normalized["tool_id"]},
        )

    instruction = bundle.instruction_final

    # r5 T8: prompt-injection audit — scan the tester-editable ``text_
    # context`` field on BOTH the raw payload and the normalized copy.
    # Scanning both lets audit diffs surface if a future _normalize_* step
    # adds accidental rewriting that hides a jailbreak from detection.
    # Never raises (helper catches); never mutates.
    try:
        from tests.testbench.pipeline import injection_audit as _ia
        raw_tc = ""
        if isinstance(payload, dict):
            raw_tc = str(
                payload.get("text_context")
                or payload.get("textContext")
                or ""
            )
        norm_tc = str(normalized.get("text_context") or "")
        _ia.scan_and_record(
            raw_tc,
            source="avatar_event.text_context.raw",
            session_id=getattr(session, "id", None),
            extra={
                "interaction_id": normalized.get("interaction_id"),
                "tool_id": normalized.get("tool_id"),
                "action_id": normalized.get("action_id"),
            },
        )
        if norm_tc and norm_tc != raw_tc:
            _ia.scan_and_record(
                norm_tc,
                source="avatar_event.text_context.normalized",
                session_id=getattr(session, "id", None),
                extra={
                    "interaction_id": normalized.get("interaction_id"),
                    "tool_id": normalized.get("tool_id"),
                },
            )
    except Exception as exc:  # noqa: BLE001 — audit never blocks pipeline
        python_logger().warning(
            "avatar injection_audit skipped: %s: %s",
            type(exc).__name__, exc,
        )

    try:
        base_wire = _build_base_wire(session)
    except PreviewNotReady as exc:
        # Roll back the dedupe entry — we reserved a slot expecting to
        # persist, but persona/bundle failed before any LLM call. Failing
        # without rollback would force the tester to wait 8 s just because
        # they forgot to fill in character_name.
        _rollback_dedupe(cache, dedupe_key)
        return _record_and_return(
            session,
            SimulationKind.AVATAR,
            SimulationResult(
                accepted=False,
                reason="persona_not_ready",
                instruction=instruction,
                coerce_info=coerce_info,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_code": exc.code, "error_message": exc.message},
        )
    # Instruction lands ONLY on the wire, NOT session.messages — per
    # P25_BLUEPRINT §A.8 #2, the testbench UI must never surface the
    # instruction as a persisted chat bubble.
    #
    # CRITICAL — wire role must be "user", NOT "system" (L36 第三轮证据 /
    # LESSONS_LEARNED §1.6 Semantic Contract vs Runtime Mechanism):
    # 主程序 ``OmniOfflineClient.prompt_ephemeral`` (main_logic/
    # omni_offline_client.py L718) 的语义契约是
    #   messages_to_send = history + [HumanMessage(content=instruction)]
    # 即 **以 user 角色** 临时注入 instruction. 早期我们错误地写成
    # ``role=system`` (只看了字符串 helper 没看 role), 触发两个级联 bug:
    #   (a) 空 session + 空 history 时 wire 变成 ``[system_prompt, system_
    #       instruction]`` — 零 user 消息. Gemini 会直接 400
    #       "Model input cannot be empty" (INVALID_ARGUMENT).
    #   (b) 非空 session 时 Gemini **偶尔** 对 "两条 system 尾接" 的 shape
    #       返回空字符串 + 200, 空 reply 被 append 进 session.messages;
    #       下一轮 LLM 读到 "上一轮 user memory_note + 上一轮空 assistant
    #       + 新 instruction" 的 wire, 基于上一轮事件生成回复 — tester
    #       观察到 "再次触发才拿到上次的 reply".
    # 两个 bug 共一个根因: 违反了主程序"instruction 入 wire 以 user 角色"
    # 的语义契约. Smoke 漏掉这是因为 offline_chat_client.py 的 fake LLM
    # 不会因为 wire 全 system 就返空, 反而照给 reply.
    base_wire.append({"role": "user", "content": instruction})

    # Prompt Preview 契约 (L36 §7.25 第五次同族证据 / r4):
    # 下面这行 wire 是**真正发给 LLM 的**, 但 instruction 本身不会进
    # session.messages (只有 memory_note 会). 如果 tester 打开 Prompt
    # Preview 时我们从 session.messages 反推, 预览里就**看不到** instruction.
    # 必须在这里把真实 wire 快照一下, 让 preview 从 session.last_llm_wire
    # 读取, 否则就是"UI 承诺的预览 ≠ 实际发送的 wire"语义漂移.
    try:
        interaction_facts = {
            key: normalized[key]
            for key in (
                "action_id",
                "intensity",
                "user_gesture",
                "avatar_gesture",
                "round_result",
            )
            if key in normalized
        }
        if "action_id" in normalized:
            preview_note = (
                f"avatar:{normalized['tool_id']}+{normalized['action_id']}"
                f"@{normalized['intensity']}"
            )
        else:
            preview_note = (
                f"avatar:{normalized['tool_id']} "
                f"{normalized['user_gesture']}/{normalized['avatar_gesture']} "
                f"result={normalized['round_result']}"
            )
        record_last_llm_wire(
            session,
            base_wire,
            source="avatar_event",
            note=preview_note,
        )
    except Exception as exc:  # noqa: BLE001 — observability must not block LLM
        python_logger().debug(
            "external_events avatar: record_last_llm_wire failed: %s: %s",
            type(exc).__name__, exc,
        )

    try:
        reply_text = await _invoke_llm_once(session, base_wire)
    except ChatConfigError as exc:
        _rollback_dedupe(cache, dedupe_key)
        try:
            update_last_llm_wire_reply(session, reply_chars=-1)
        except Exception:  # noqa: BLE001
            pass
        return _record_and_return(
            session,
            SimulationKind.AVATAR,
            SimulationResult(
                accepted=False,
                reason="chat_not_configured",
                instruction=instruction,
                coerce_info=coerce_info,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_code": exc.code, "error_message": exc.message},
        )
    except Exception as exc:  # noqa: BLE001
        _rollback_dedupe(cache, dedupe_key)
        try:
            update_last_llm_wire_reply(session, reply_chars=-1)
        except Exception:  # noqa: BLE001
            pass
        return _record_and_return(
            session,
            SimulationKind.AVATAR,
            SimulationResult(
                accepted=False,
                reason="llm_failed",
                instruction=instruction,
                coerce_info=coerce_info,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_type": type(exc).__name__, "error_message": str(exc)},
        )

    try:
        update_last_llm_wire_reply(session, reply_chars=len(reply_text or ""))
    except Exception as exc:  # noqa: BLE001
        python_logger().debug(
            "external_events avatar: update_last_llm_wire_reply failed: %s: %s",
            type(exc).__name__, exc,
        )

    now = session.clock.now()
    user_msg = make_message(
        role=ROLE_USER,
        content=memory_note,
        timestamp=now,
        source=SOURCE_INJECT,
    )
    user_result = append_message(session, user_msg, on_violation="coerce")

    assistant_msg = make_message(
        role=ROLE_ASSISTANT,
        content=reply_text,
        timestamp=session.clock.now(),
        source=SOURCE_LLM,
    )
    asst_result = append_message(session, assistant_msg, on_violation="coerce")

    mirror_info = _apply_mirror_to_recent(
        session,
        [user_result.msg, asst_result.msg],
        requested=mirror_to_recent,
    )

    result = SimulationResult(
        accepted=True,
        instruction=instruction,
        memory_pair=[user_result.msg, asst_result.msg],
        persisted=True,
        dedupe_info={
            "hit": False,
            "cache_size": len(cache),
            "cache_size_before": cache_size_before,
            "dedupe_key": dedupe_key,
            "dedupe_rank": dedupe_rank,
        },
        assistant_reply=reply_text,
        coerce_info=coerce_info,
        mirror_to_recent_info=mirror_info,
        elapsed_ms=_elapsed(started),
    )
    return _record_and_return(
        session,
        SimulationKind.AVATAR,
        result,
        detail={
            "interaction_id": normalized.get("interaction_id"),
            "tool_id": normalized["tool_id"],
            **interaction_facts,
            "reward_drop": normalized.get("reward_drop"),
            "easter_egg": normalized.get("easter_egg"),
            "reply_chars": len(reply_text),
        },
    )


def _rollback_dedupe(cache: _AvatarDedupeCache, dedupe_key: str) -> None:
    """Best-effort remove the reservation we made before a downstream fail.

    The copy-protected helper unconditionally inserts on the accept branch,
    so a post-check failure leaves a "ghost" entry that would reject the
    tester's next retry for 8 s. Dropping the entry restores retry-ability
    without touching the protected region.
    """
    key = str(dedupe_key or "").strip()
    if not key:
        return
    cache._cache.pop(key, None)  # pylint: disable=protected-access


# ─────────────────────────────────────────────────────────────
# Handler: agent callback
# ─────────────────────────────────────────────────────────────


async def simulate_agent_callback(
    session: Session,
    payload: dict[str, Any],
    *,
    mirror_to_recent: bool = False,
) -> SimulationResult:
    """Simulate one agent callback (``AGENT_CALLBACK_NOTIFICATION`` prefix)."""
    started = time.perf_counter()

    full_lang, short_lang = _resolve_language(session)

    # Count raw items for diagnostics note (needed regardless of bundle
    # success — empty bundle still wants the raw_len surfacing).
    raw_items = payload.get("callbacks") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raw_items = []
    items: list[str] = []
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            items.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("text") or item.get("summary") or ""
            text = str(text or "").strip()
            if text:
                items.append(text)

    # Shared helper between real send + preview (L33/L36 §7.25 第 5 层 —
    # see simulate_avatar_interaction for the full rationale). Note: the
    # builder does its own identical ``items`` collapse so we never drift.
    bundle = _build_agent_callback_instruction_bundle(session, payload)
    if bundle is None:
        return _record_and_return(
            session,
            SimulationKind.AGENT_CALLBACK,
            SimulationResult(
                accepted=False,
                reason="empty_callbacks",
                elapsed_ms=_elapsed(started),
            ),
            detail={"raw_len": len(raw_items)},
        )

    instruction = bundle.instruction_final

    # r5 T8: prompt-injection audit — scan the concatenated callback
    # bodies. These are the only tester-editable free-form text fields
    # on the agent-callback path; target/language/kind are enum-bounded.
    try:
        from tests.testbench.pipeline import injection_audit as _ia
        _ia.scan_and_record(
            "\n".join(items),
            source="agent_callback.callbacks",
            session_id=getattr(session, "id", None),
            extra={
                "callback_count": len(items),
                "language": full_lang,
            },
        )
    except Exception as exc:  # noqa: BLE001
        python_logger().warning(
            "agent_callback injection_audit skipped: %s: %s",
            type(exc).__name__, exc,
        )

    try:
        base_wire = _build_base_wire(session)
    except PreviewNotReady as exc:
        return _record_and_return(
            session,
            SimulationKind.AGENT_CALLBACK,
            SimulationResult(
                accepted=False,
                reason="persona_not_ready",
                instruction=instruction,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_code": exc.code, "error_message": exc.message},
        )
    # instruction 以 user 角色入 wire (对齐主程序 prompt_ephemeral;
    # 详见 simulate_avatar_interaction 里的长 comment).
    base_wire.append({"role": "user", "content": instruction})

    # L36 §7.25 r4 — Prompt Preview "预览 = 真实" 契约 (详见
    # simulate_avatar_interaction 里的长 comment).
    try:
        record_last_llm_wire(
            session,
            base_wire,
            source="agent_callback",
            note=f"agent_callback:{len(items)}items@{short_lang}",
        )
    except Exception as exc:  # noqa: BLE001
        python_logger().debug(
            "external_events agent_callback: record_last_llm_wire failed: %s: %s",
            type(exc).__name__, exc,
        )

    try:
        reply_text = await _invoke_llm_once(session, base_wire)
    except ChatConfigError as exc:
        try:
            update_last_llm_wire_reply(session, reply_chars=-1)
        except Exception:  # noqa: BLE001
            pass
        return _record_and_return(
            session,
            SimulationKind.AGENT_CALLBACK,
            SimulationResult(
                accepted=False,
                reason="chat_not_configured",
                instruction=instruction,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_code": exc.code, "error_message": exc.message},
        )
    except Exception as exc:  # noqa: BLE001
        try:
            update_last_llm_wire_reply(session, reply_chars=-1)
        except Exception:  # noqa: BLE001
            pass
        return _record_and_return(
            session,
            SimulationKind.AGENT_CALLBACK,
            SimulationResult(
                accepted=False,
                reason="llm_failed",
                instruction=instruction,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_type": type(exc).__name__, "error_message": str(exc)},
        )

    try:
        update_last_llm_wire_reply(session, reply_chars=len(reply_text or ""))
    except Exception as exc:  # noqa: BLE001
        python_logger().debug(
            "external_events agent_callback: update_last_llm_wire_reply failed: "
            "%s: %s", type(exc).__name__, exc,
        )

    # r5 T7: banner 在 assistant_msg 之前落盘, 对话流里按时间戳会先显示
    # 一条 "[测试事件] 测试用户触发了一次 Agent 回调事件 · N 条回调",
    # 再是 AI 的回复, tester 能立刻看出因果.
    _append_external_event_banner(
        session, SimulationKind.AGENT_CALLBACK,
        extra_suffix=f"{len(items)} 条回调",
    )

    assistant_msg = make_message(
        role=ROLE_ASSISTANT,
        content=reply_text,
        timestamp=session.clock.now(),
        source=SOURCE_LLM,
    )
    asst_result = append_message(session, assistant_msg, on_violation="coerce")

    mirror_info = _apply_mirror_to_recent(
        session, [asst_result.msg], requested=mirror_to_recent,
    )

    result = SimulationResult(
        accepted=True,
        instruction=instruction,
        memory_pair=[asst_result.msg],
        persisted=True,
        assistant_reply=reply_text,
        mirror_to_recent_info=mirror_info,
        elapsed_ms=_elapsed(started),
    )
    return _record_and_return(
        session,
        SimulationKind.AGENT_CALLBACK,
        result,
        detail={
            "callback_count": len(items),
            "total_chars": sum(len(t) for t in items),
            "instruction_lang": short_lang,
            "full_language": full_lang,
            "reply_len": len(reply_text),
        },
    )


# ─────────────────────────────────────────────────────────────
# Handler: proactive chat
# ─────────────────────────────────────────────────────────────


_PROACTIVE_KINDS = frozenset({
    "home", "screenshot", "window", "news", "community", "video", "personal", "music",
})


# ─────────────────────────────────────────────────────────────
# Proactive prompt slot fill (Day 2 polish r5)
# ─────────────────────────────────────────────────────────────
#
# 主程序填 ``{memory_context}`` 走 memory_server ``/new_dialog/<character>``
# HTTP (system_router.proactive_chat L2847-L2861); testbench **不** 起 memory
# server, 所以直接从 ``session.messages`` 的尾部 K 条渲染成 role-prefixed 纯
# 文本 (与主程序 ``_format_recent_proactive_chats`` 风格一致, 非字节级 copy:
# 主程序用的是全局 ``_proactive_chat_history`` 时间窗口过滤, testbench 走
# session.messages 尾部 K 条).
#
# K 选取: 主程序没有一个统一常量明显是"proactive memory K" — plugin/qq_
# auto_reply 用 6, system_router 用 time-window 过滤 (无硬数量). 我们保
# 守地取 **12**, 够覆盖一轮 6-turn 对话而不膨胀 prompt; Day 3+ 若 tester
# 反馈想改就加 UI 可调.
_PROACTIVE_MEMORY_K: int = 12

# 完整占位符白名单 (逐 kind 扫描 config/prompts/prompts_proactive.py 五语种). 缺的
# 占位符用 ``"(无)"`` 兜底, 保证 ``replace`` 不漏 (见 L33 §1: 语义契约 vs
# 运行时机制 — 缺失 slot 必须显式 surface, 不能让 ``{window_context}``
# 字面量漏进 LLM 引发 silent parse error).
_PROACTIVE_SLOT_WHITELIST: frozenset[str] = frozenset({
    # 五语种所有 kind 共用:
    "lanlan_name",
    "master_name",
    "memory_context",
    # home / news / video 用 trending_content:
    "trending_content",
    # personal 用 personal_dynamic:
    "personal_dynamic",
    # music 用 current_chat:
    "current_chat",
    # screenshot 用 screenshot_content + window_title_section:
    "screenshot_content",
    "window_title_section",
    # window (window_search) 用 window_context:
    "window_context",
})

_PROACTIVE_MEMORY_EMPTY_FALLBACK: str = "(暂无对话历史)"
_PROACTIVE_TOPIC_EMPTY_FALLBACK: str = "(无内容)"
_PROACTIVE_CONTEXT_EMPTY_FALLBACK: str = "(无)"


def _render_session_messages_for_memory_context(
    session: Session,
    *,
    k: int = _PROACTIVE_MEMORY_K,
) -> str:
    """Render ``session.messages[-k:]`` into a role-prefixed string block.

    Shape per line: ``"<role_label>: <content>"`` where ``role_label`` is:

      * ``"用户"`` for ``role == "user"``
      * ``"AI"`` for ``role == "assistant"``
      * ``"系统"`` for ``role == "system"``

    Multiple physical lines inside a single message content are preserved
    (no line-flattening) so ``memory_note`` style multi-line messages stay
    readable; we just prepend the role label on the first line.

    Empty history → :data:`_PROACTIVE_MEMORY_EMPTY_FALLBACK` so the LLM
    sees an explicit "empty" marker rather than a bare blank line.
    """
    messages = getattr(session, "messages", None) or []
    if not messages:
        return _PROACTIVE_MEMORY_EMPTY_FALLBACK
    # Drop external-event banner pseudo-messages BEFORE windowing so a
    # tail full of banners doesn't push real conversation out of the
    # k-message window. The banner is a tester-visible UI marker only;
    # ``prompt_builder`` already filters it out of the *direct* wire
    # (R5 T7 read-side chokepoint), but this proactive memory_context
    # path is a *second* read site that has to apply the same filter,
    # otherwise the banner ("[测试事件] 测试用户触发了一次 Agent 回调
    # 事件") leaks into the proactive system prompt as a fake "系统"
    # turn and the LLM treats it as an instruction (GH AI-review issue
    # #5; same family as L33 single-writer / L36 §7.25 fifth-layer
    # defense — one chokepoint ``_append_external_event_banner`` writes,
    # *every* read site must filter symmetrically).
    filtered = [
        m for m in messages
        if (m or {}).get("source") != SOURCE_EXTERNAL_EVENT_BANNER
    ]
    if not filtered:
        return _PROACTIVE_MEMORY_EMPTY_FALLBACK
    recent = filtered[-max(1, k):]
    lines: list[str] = []
    for msg in recent:
        role = str((msg or {}).get("role") or "").lower()
        if role == "user":
            label = "用户"
        elif role == "assistant":
            label = "AI"
        elif role == "system":
            label = "系统"
        else:
            # defensive: skip unknown roles rather than poison the context
            # with a cryptic label; testbench's ALLOWED_ROLES is a hard
            # chokepoint at append time so this is essentially dead code,
            # but keep the guard so a future refactor can't bypass it.
            continue
        content = (msg or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list) and content and isinstance(content[0], dict):
            # testbench's richer content shape {type:"text", text:"..."}
            text = str(content[0].get("text") or "")
        else:
            text = str(content or "")
        lines.append(f"{label}: {text}")
    if not lines:
        return _PROACTIVE_MEMORY_EMPTY_FALLBACK
    return "\n".join(lines)


def _fill_proactive_instruction(
    template_raw: str,
    *,
    lanlan_name: str,
    master_name: str,
    memory_context: str,
    topic_text: str,
) -> str:
    """Substitute every slot in :data:`_PROACTIVE_SLOT_WHITELIST` into
    ``template_raw`` via :py:meth:`str.replace` (NOT :py:meth:`str.format`).

    Why ``replace`` and not ``format``: proactive templates contain literal
    ``{`` / ``}`` in bullet rules ("例如 '[PASS]'") and occasionally in Ruby
    / Russian / Japanese punctuation paths. ``format`` would raise on the
    first brace-literal and lose fidelity. ``replace`` is brittle only if a
    slot name is a **substring** of another slot name — our whitelist has
    no such collision (``memory_context`` is never a prefix of another
    slot name, ``master_name`` is not a prefix of ``master`` anywhere).

    Fallbacks:
      * ``{trending_content}`` / ``{personal_dynamic}`` / ``{current_chat}``
        → tester-provided ``topic_text`` (same semantic "main content" slot
        across kinds) or :data:`_PROACTIVE_TOPIC_EMPTY_FALLBACK`.
      * ``{screenshot_content}`` / ``{window_title_section}`` /
        ``{window_context}`` → :data:`_PROACTIVE_CONTEXT_EMPTY_FALLBACK`
        (testbench can't reproduce real vision / shell inspection out of
        scope).
    """
    topic_filled = topic_text.strip() or _PROACTIVE_TOPIC_EMPTY_FALLBACK
    out = template_raw
    out = out.replace("{lanlan_name}", lanlan_name)
    out = out.replace("{master_name}", master_name)
    out = out.replace("{memory_context}", memory_context)
    # The three "main content" slots (mutually exclusive per kind) all take
    # the tester's topic text; only one of them will appear in any given
    # template, the others are no-ops.
    out = out.replace("{trending_content}", topic_filled)
    out = out.replace("{personal_dynamic}", topic_filled)
    out = out.replace("{current_chat}", topic_filled)
    # Vision / shell context — OOS for testbench; substitute a visible
    # placeholder so the LLM can't silently parse an un-replaced
    # ``{screenshot_content}`` as the payload.
    out = out.replace("{screenshot_content}", _PROACTIVE_CONTEXT_EMPTY_FALLBACK)
    out = out.replace("{window_title_section}", _PROACTIVE_CONTEXT_EMPTY_FALLBACK)
    out = out.replace("{window_context}", _PROACTIVE_CONTEXT_EMPTY_FALLBACK)
    return out


# ─────────────────────────────────────────────────────────────
# Instruction-bundle builders (shared between simulate_* and
# build_external_event_preview — Day 2 polish r5)
# ─────────────────────────────────────────────────────────────
#
# L33 §"跨路径不对称" + L36 §7.25 第 5 层防御:
# "预览给测试人员看的" 和 "真实发给 LLM 的" 必须**字符级一致**. 若 preview
# 走一条 path, simulate_* 走另一条 path, 同一 bug 会以不同方式偷偷漂移;
# 解法 = 抽出单一 builder 让两个调用点共用. 每个 builder 接受 ``session``
# + ``payload``, 返回 ``(instruction_template_raw, instruction_final,
# coerce_info)`` 三元组, **不**触碰 session.messages / dedupe cache /
# last_llm_wire — 这些副作用保留在 simulate_* 外层, preview 跳过.


@dataclass
class _InstructionBundle:
    """Tuple returned by the three ``_build_*_instruction_bundle`` helpers."""

    template_raw: str
    instruction_final: str
    coerce_info: list[CoerceInfo]
    # Avatar-specific: the normalized payload + dedupe metadata. Other
    # kinds leave these empty; callers should only read them when ``kind``
    # is AVATAR. Not a separate type because three kinds share ~90% of
    # the builder contract and Python dataclasses don't do tagged unions
    # cheaply; a discriminator is added by the caller via ``kind``.
    avatar_normalized: Optional[dict[str, Any]] = None
    avatar_memory_note: str = ""
    avatar_dedupe_key: str = ""
    avatar_dedupe_rank: int = 0


def _build_avatar_instruction_bundle(
    session: Session,
    payload: dict[str, Any],
) -> Optional[_InstructionBundle]:
    """Build the avatar instruction bundle. Returns ``None`` if the payload
    is invalid (caller converts to ``reason="invalid_payload"``).

    Shared between :func:`simulate_avatar_interaction` (real path) and
    :func:`build_external_event_preview` (dry-run path). Both paths see
    the exact same ``instruction_final`` string — that's the contract.
    """
    from config.prompts.avatar_interaction_contract import (
        normalize_avatar_interaction_payload,
    )
    from config.prompts.prompts_avatar_interaction import (
        _build_avatar_interaction_instruction,
        _build_avatar_interaction_memory_meta,
        _sanitize_avatar_interaction_text_context,
    )

    full_lang, _short_lang = _resolve_language(session)
    lanlan_name, master_name = _resolve_names(session)

    normalized = normalize_avatar_interaction_payload(
        payload,
        sanitize_text_context=_sanitize_avatar_interaction_text_context,
    )
    if normalized is None:
        return None

    meta = _build_avatar_interaction_memory_meta(full_lang, normalized, master_name)
    memory_note = str(meta.get("memory_note") or "")
    dedupe_key = str(meta.get("memory_dedupe_key") or normalized["tool_id"])
    dedupe_rank = int(meta.get("memory_dedupe_rank") or 1)

    instruction = _build_avatar_interaction_instruction(
        full_lang, lanlan_name, master_name, normalized,
    )
    # Avatar uses one direct event-fact builder with no later UI polish, so
    # template_raw == instruction_final. The normalized payload summary stays
    # separate for diagnostics and does not become part of the model prompt.
    return _InstructionBundle(
        template_raw=instruction,
        instruction_final=instruction,
        coerce_info=[],
        avatar_normalized=normalized,
        avatar_memory_note=memory_note,
        avatar_dedupe_key=dedupe_key,
        avatar_dedupe_rank=dedupe_rank,
    )


def _build_agent_callback_instruction_bundle(
    session: Session,
    payload: dict[str, Any],
) -> Optional[_InstructionBundle]:
    """Build the agent_callback instruction bundle. Returns ``None`` if
    callbacks are empty (caller → ``reason="empty_callbacks"``).

    Mirrors the production drain path in ``main_logic.core``: synthesizes
    callback dicts (status=completed, source_kind=system) and renders via
    ``_build_callback_instruction`` so the testbench wire matches the
    grouped-by-source/status outer template the real LLM sees.
    """
    from config.prompts.prompts_sys import SYSTEM_NOTIFICATION_PASSIVE, _loc
    from main_logic.core import _build_callback_instruction

    _full_lang, short_lang = _resolve_language(session)

    raw_items = payload.get("callbacks") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raw_items = []
    callbacks: list[dict[str, Any]] = []
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            callbacks.append({
                "status": "completed",
                "source_kind": "system",
                "source_name": "",
                "summary": item.strip(),
                "detail": item.strip(),
            })
        elif isinstance(item, dict):
            text = item.get("text") or item.get("summary") or ""
            text = str(text or "").strip()
            if text:
                callbacks.append({
                    "status": item.get("status") or "completed",
                    "source_kind": item.get("source_kind") or "system",
                    "source_name": item.get("source_name") or "",
                    "summary": text,
                    "detail": str(item.get("detail") or text),
                })

    if not callbacks:
        return None

    # Use passive=True to mirror the next-user-turn drain semantics that
    # this simulator was originally built around.
    instruction = _build_callback_instruction(
        callbacks,
        lang=short_lang,
        lanlan_name=getattr(session, "lanlan_name", "") or "",
        master_name=getattr(session, "master_name", "") or "",
        passive=True,
    )
    template_raw = _loc(SYSTEM_NOTIFICATION_PASSIVE, short_lang)
    return _InstructionBundle(
        template_raw=template_raw,
        instruction_final=instruction,
        coerce_info=[],
    )


def _build_proactive_instruction_bundle(
    session: Session,
    payload: dict[str, Any],
) -> _InstructionBundle:
    """Build the proactive instruction bundle.

    Unlike avatar/agent_callback, proactive always returns a bundle —
    invalid ``kind`` coerces to ``home`` (surfaced via ``coerce_info``),
    unknown language falls back to en (also surfaced). Tester-provided
    ``topic`` fills the ``{trending_content}`` / ``{personal_dynamic}``
    / ``{current_chat}`` slot; session.messages[-K:] fills
    ``{memory_context}``.
    """
    from config.prompts.prompts_proactive import (
        _normalize_prompt_language,
        get_proactive_chat_prompt,
    )

    coerce_info: list[CoerceInfo] = []

    full_lang, _short_lang = _resolve_language(session)
    requested_kind = str((payload or {}).get("kind") or "home").strip().lower()
    if requested_kind in _PROACTIVE_KINDS:
        kind = requested_kind
    else:
        kind = "home"
        coerce_info.append(CoerceInfo(
            field="kind",
            requested=requested_kind,
            applied="home",
            note=(
                "proactive kind 不在合法集合 (home/screenshot/window/news/community/"
                "video/personal/music), 已回退到 home."
            ),
        ))

    normalized_lang = _normalize_prompt_language(full_lang)
    req_lower = full_lang.lower() if full_lang else ""
    # A language only "coerces" when the main program's normalizer falls it
    # back to English and the request wasn't English to begin with. Native
    # languages (zh/ja/ko/ru/es/pt + en) normalize to themselves, so deferring
    # to _normalize_prompt_language tracks the upstream supported-language set
    # automatically (es/pt became native in the 2026-06 sync — no longer coerced).
    if req_lower and normalized_lang == "en" and not req_lower.startswith("en"):
        coerce_info.append(CoerceInfo(
            field="language",
            requested=full_lang,
            applied=normalized_lang,
            note=(
                "proactive 未原生翻译该语言, _normalize_prompt_language 回退 "
                f"到 {normalized_lang!r} (主程序同策略)."
            ),
        ))

    template_raw = get_proactive_chat_prompt(kind, full_lang)
    lanlan_name, master_name = _resolve_names(session)

    # Fill memory_context from session.messages[-K:] (L36 §7.25 第 5 层:
    # 预览 = 真实, 这里是 **单一 source of truth** — 让 simulate_proactive
    # 和 build_external_event_preview 读到字符级一致的 instruction).
    memory_context = _render_session_messages_for_memory_context(session)
    topic_text = str((payload or {}).get("topic") or "")

    instruction = _fill_proactive_instruction(
        template_raw,
        lanlan_name=lanlan_name,
        master_name=master_name,
        memory_context=memory_context,
        topic_text=topic_text,
    )
    return _InstructionBundle(
        template_raw=template_raw,
        instruction_final=instruction,
        coerce_info=coerce_info,
    )


async def simulate_proactive(
    session: Session,
    payload: dict[str, Any],
    *,
    mirror_to_recent: bool = False,
) -> SimulationResult:
    """Simulate one proactive-chat opener via ``get_proactive_chat_prompt``.

    Day 2 polish r5: now fills ``{memory_context}`` from
    ``session.messages[-K:]`` (so the LLM sees real conversation history, not
    the literal placeholder) and ``{trending_content}`` / related content
    slots from the tester-provided ``payload['topic']``. See
    :func:`_build_proactive_instruction_bundle` for the shared builder that
    :func:`build_external_event_preview` also calls — L36 §7.25 第 5 层.
    """
    started = time.perf_counter()
    from config.prompts.prompts_proactive import _normalize_prompt_language

    full_lang, _short_lang = _resolve_language(session)

    # Shared helper between real send + preview (L33/L36 §7.25 第 5 层 —
    # see simulate_avatar_interaction for the full rationale).
    bundle = _build_proactive_instruction_bundle(session, payload)
    coerce_info = bundle.coerce_info
    instruction = bundle.instruction_final

    # Recover ``kind`` + ``normalized_lang`` from the same logic the builder
    # used — we need them for diagnostics note + detail payload. The builder
    # already applied coerce_info, so this branch is cheap bookkeeping.
    requested_kind = str((payload or {}).get("kind") or "home").strip().lower()
    kind = requested_kind if requested_kind in _PROACTIVE_KINDS else "home"
    normalized_lang = _normalize_prompt_language(full_lang)

    # r5 T8: prompt-injection audit — scan the tester-filled "主动对话
    # 话题" (topic) field. Only meaningful tester-editable free-form
    # field on the proactive path; kind/language are enum-bounded.
    try:
        from tests.testbench.pipeline import injection_audit as _ia
        topic_text = ""
        if isinstance(payload, dict):
            topic_text = str(payload.get("topic") or "")
        if topic_text:
            _ia.scan_and_record(
                topic_text,
                source="proactive.topic",
                session_id=getattr(session, "id", None),
                extra={"kind": kind, "language": normalized_lang},
            )
    except Exception as exc:  # noqa: BLE001
        python_logger().warning(
            "proactive injection_audit skipped: %s: %s",
            type(exc).__name__, exc,
        )

    try:
        base_wire = _build_base_wire(session)
    except PreviewNotReady as exc:
        return _record_and_return(
            session,
            SimulationKind.PROACTIVE,
            SimulationResult(
                accepted=False,
                reason="persona_not_ready",
                instruction=instruction,
                coerce_info=coerce_info,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_code": exc.code, "error_message": exc.message, "kind": kind},
        )
    # instruction 以 user 角色入 wire (对齐主程序 prompt_ephemeral;
    # 详见 simulate_avatar_interaction 里的长 comment).
    base_wire.append({"role": "user", "content": instruction})

    # L36 §7.25 r4 — Prompt Preview "预览 = 真实" 契约 (详见
    # simulate_avatar_interaction 里的长 comment).
    try:
        record_last_llm_wire(
            session,
            base_wire,
            source="proactive_chat",
            note=f"proactive:{kind}@{normalized_lang}",
        )
    except Exception as exc:  # noqa: BLE001
        python_logger().debug(
            "external_events proactive: record_last_llm_wire failed: %s: %s",
            type(exc).__name__, exc,
        )

    try:
        reply_text = await _invoke_llm_once(session, base_wire)
    except ChatConfigError as exc:
        try:
            update_last_llm_wire_reply(session, reply_chars=-1)
        except Exception:  # noqa: BLE001
            pass
        return _record_and_return(
            session,
            SimulationKind.PROACTIVE,
            SimulationResult(
                accepted=False,
                reason="chat_not_configured",
                instruction=instruction,
                coerce_info=coerce_info,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_code": exc.code, "error_message": exc.message, "kind": kind},
        )
    except Exception as exc:  # noqa: BLE001
        try:
            update_last_llm_wire_reply(session, reply_chars=-1)
        except Exception:  # noqa: BLE001
            pass
        return _record_and_return(
            session,
            SimulationKind.PROACTIVE,
            SimulationResult(
                accepted=False,
                reason="llm_failed",
                instruction=instruction,
                coerce_info=coerce_info,
                elapsed_ms=_elapsed(started),
            ),
            detail={"error_type": type(exc).__name__, "error_message": str(exc), "kind": kind},
        )

    try:
        update_last_llm_wire_reply(session, reply_chars=len(reply_text or ""))
    except Exception as exc:  # noqa: BLE001
        python_logger().debug(
            "external_events proactive: update_last_llm_wire_reply failed: "
            "%s: %s", type(exc).__name__, exc,
        )

    # Main program proactive semantics: reply == "[PASS]" (case-insensitive,
    # exact match after strip) means "skip this opportunity, don't say
    # anything". We surface via reason="pass_signaled" + accepted=False
    # and DO NOT append to session.messages (§2.1 contract). Non-[PASS]
    # replies always append.
    stripped_reply = reply_text.strip()
    if stripped_reply.upper() == "[PASS]":
        result = SimulationResult(
            accepted=False,
            reason="pass_signaled",
            instruction=instruction,
            assistant_reply=reply_text,
            coerce_info=coerce_info,
            mirror_to_recent_info=MirrorToRecentInfo(requested=mirror_to_recent, applied=False, fallback_reason=("proactive LLM 返回 [PASS], 无消息可 mirror" if mirror_to_recent else None)),
            elapsed_ms=_elapsed(started),
        )
        return _record_and_return(
            session,
            SimulationKind.PROACTIVE,
            result,
            detail={
                "kind": kind,
                "lang": normalized_lang,
                "pass_signaled": True,
                "reply_len": len(reply_text),
            },
        )

    # r5 T7: banner 仅在 non-[PASS] 分支 (LLM 真的开口说话了) 才落盘 —
    # 如果 LLM 返回 [PASS] 意味着 "本轮不说话", 这时 banner 会让 tester
    # 困惑 ("我触发了事件, 但 AI 没回, banner 却在"), 所以上面 [PASS]
    # early-return 分支没有 banner.
    _append_external_event_banner(
        session, SimulationKind.PROACTIVE,
        extra_suffix=f"触发类型: {kind}",
    )

    assistant_msg = make_message(
        role=ROLE_ASSISTANT,
        content=reply_text,
        timestamp=session.clock.now(),
        source=SOURCE_LLM,
    )
    asst_result = append_message(session, assistant_msg, on_violation="coerce")

    mirror_info = _apply_mirror_to_recent(
        session, [asst_result.msg], requested=mirror_to_recent,
    )

    result = SimulationResult(
        accepted=True,
        instruction=instruction,
        memory_pair=[asst_result.msg],
        persisted=True,
        assistant_reply=reply_text,
        coerce_info=coerce_info,
        mirror_to_recent_info=mirror_info,
        elapsed_ms=_elapsed(started),
    )
    return _record_and_return(
        session,
        SimulationKind.PROACTIVE,
        result,
        detail={
            "kind": kind,
            "lang": normalized_lang,
            "pass_signaled": False,
            "reply_len": len(reply_text),
        },
    )


# ─────────────────────────────────────────────────────────────
# Dry-run preview (Day 2 polish r5 — L36 §7.25 第 5 层)
# ─────────────────────────────────────────────────────────────


@dataclass
class ExternalEventPreview:
    """Snapshot returned by :func:`build_external_event_preview`.

    Contract: ``wire_preview[-1]["content"] == instruction_final``, and the
    ``instruction_final`` field here MUST equal the ``instruction`` that
    :func:`simulate_avatar_interaction` / :func:`simulate_agent_callback`
    / :func:`simulate_proactive` would pass to the LLM for the same
    ``(session, payload)`` pair. This is the "预览 = 真实" contract — any
    future change to any of the four functions must preserve it, or the
    two PE2-style smoke tests in ``p25_prompt_preview_truth_smoke.py``
    will fail.
    """

    kind: str
    instruction_template_raw: str
    instruction_final: str
    wire_preview: list[dict[str, Any]]
    coerce_info: list[CoerceInfo]
    # Why ``reason`` here: same shape as SimulationResult — if the payload
    # is invalid (avatar) or empty (agent_callback) we still want the UI to
    # render "why can't I preview this?" rather than silently fail.
    reason: Optional[str] = None
    # Base-wire assembly error surfacing (persona not ready etc). When set,
    # ``wire_preview`` will be empty and ``instruction_final`` still holds
    # whatever we managed to build.
    error_code: Optional[str] = None
    error_message: Optional[str] = None


def build_external_event_preview(
    session: Session,
    kind: SimulationKind,
    payload: dict[str, Any],
) -> ExternalEventPreview:
    """Build a dry-run preview of what ``simulate_<kind>(session, payload)``
    would send to the LLM — **without** touching ``session.messages``,
    ``last_llm_wire``, the dedupe cache, or calling the LLM.

    Contract (L36 §7.25 第 5 层 / L33 跨路径不对称):

      ``build_external_event_preview(session, kind, payload)
        .wire_preview[-1]["content"]``

    MUST equal the ``content`` field of the last element of the wire that
    ``simulate_<kind>(session, payload)`` would record via
    :func:`record_last_llm_wire` before calling ``_invoke_llm_once``. This
    is enforced at runtime by having both paths route through the same
    ``_build_<kind>_instruction_bundle`` helper + ``_build_base_wire``.
    """
    # kind dispatch table — keep the same order as SimulationKind for
    # readability. The builder returns None for user-error cases (invalid
    # avatar payload, empty callbacks); we surface those as ``reason``
    # instead of raising so the UI can render the specific error.
    if kind is SimulationKind.AVATAR:
        bundle = _build_avatar_instruction_bundle(session, payload)
        if bundle is None:
            return ExternalEventPreview(
                kind=kind.value,
                instruction_template_raw="",
                instruction_final="",
                wire_preview=[],
                coerce_info=[],
                reason="invalid_payload",
            )
    elif kind is SimulationKind.AGENT_CALLBACK:
        bundle = _build_agent_callback_instruction_bundle(session, payload)
        if bundle is None:
            return ExternalEventPreview(
                kind=kind.value,
                instruction_template_raw="",
                instruction_final="",
                wire_preview=[],
                coerce_info=[],
                reason="empty_callbacks",
            )
    elif kind is SimulationKind.PROACTIVE:
        bundle = _build_proactive_instruction_bundle(session, payload)
    else:  # pragma: no cover — SimulationKind is an Enum, exhaustive
        raise ValueError(f"build_external_event_preview: unknown kind {kind!r}")

    # Base wire may raise PreviewNotReady when persona/memory/recent are
    # not configured; surface as preview-level error_code/error_message
    # rather than 500 so the tester can diagnose.
    try:
        base_wire = _build_base_wire(session)
    except PreviewNotReady as exc:
        return ExternalEventPreview(
            kind=kind.value,
            instruction_template_raw=bundle.template_raw,
            instruction_final=bundle.instruction_final,
            wire_preview=[],
            coerce_info=bundle.coerce_info,
            reason="persona_not_ready",
            error_code=exc.code,
            error_message=exc.message,
        )

    # Identical append shape to the real-send path: role="user" (NOT
    # system — see long comment in simulate_avatar_interaction). Use a
    # fresh list copy so the caller can't mutate anything the real
    # simulate_* path is sharing (currently there's no sharing but the
    # copy is cheap and prevents future aliasing footguns).
    wire_preview = list(base_wire)
    wire_preview.append({"role": "user", "content": bundle.instruction_final})

    return ExternalEventPreview(
        kind=kind.value,
        instruction_template_raw=bundle.template_raw,
        instruction_final=bundle.instruction_final,
        wire_preview=wire_preview,
        coerce_info=bundle.coerce_info,
    )


# ─────────────────────────────────────────────────────────────
# Diagnostics recording
# ─────────────────────────────────────────────────────────────


_KIND_TO_OP: dict[SimulationKind, DiagnosticsOp] = {
    SimulationKind.AVATAR: DiagnosticsOp.AVATAR_INTERACTION_SIMULATED,
    SimulationKind.AGENT_CALLBACK: DiagnosticsOp.AGENT_CALLBACK_SIMULATED,
    SimulationKind.PROACTIVE: DiagnosticsOp.PROACTIVE_SIMULATED,
}


# ─────────────────────────────────────────────────────────────
# Day 2 polish r5 T7: external-event system banner
# ─────────────────────────────────────────────────────────────
#
# 对"不制造 user 对话消息但又真实存在"的外部事件 (agent_callback /
# proactive), 在对话流里插一条 role=system + source=external_event_banner
# 的短消息 ("测试用户触发了一次 agent_callback 事件"), 让 tester 能在一
# 长串对话里看出"这条 assistant 回复是回应一次 tester 模拟的事件". 用
# 户原话 (2026-04-23 r4 feedback): "XXX later 系统提示的扩展 — 对于
# 不制造对话消息但是又真实存在的操作, 加一行提示".
#
# 关键约束:
#   * banner 不能进 LLM wire — 否则会污染上下文 + 和 prompt_builder 的
#     system→user rewrite chokepoint 冲突. 靠 source = SOURCE_EXTERNAL_
#     EVENT_BANNER + prompt_builder.py 第 604 行的单点过滤实现 (L33 单
#     chokepoint pattern).
#   * banner 走 session.messages 持久化 — 这样 refresh / 重载会话后
#     banner 仍在, 不是 UI 层 ephemeral. 语义是"对话流的一部分, 只是
#     不进 LLM".
#   * 必须在 assistant_msg append 之前, base_wire 构造之后. 如果 banner
#     先写再构 base_wire, 不会出问题 (prompt_builder 过滤), 但语义上更
#     干净的是 banner 和 assistant_msg 背靠背出现.
#   * avatar 路径不加 banner — avatar 本身就 append memory_note (role=
#     user) 作为 "tester 触发" 的锚点, 再加 banner 重复.
_EXTERNAL_EVENT_BANNER_TEMPLATES: dict[SimulationKind, str] = {
    SimulationKind.AGENT_CALLBACK:
        "[测试事件] 测试用户触发了一次 Agent 回调事件",
    SimulationKind.PROACTIVE:
        "[测试事件] 测试用户触发了一次主动搭话事件",
}


def _append_external_event_banner(
    session: Session,
    kind: SimulationKind,
    *,
    extra_suffix: str = "",
) -> None:
    """Append a visual-only banner marking the external event in the
    conversation timeline. Does NOT enter the LLM wire (filtered at
    :mod:`tests.testbench.pipeline.prompt_builder` chokepoint).

    ``extra_suffix`` (optional) 追加到 banner 末尾, 用来给 tester 一个
    简短区分 ("触发类型: home" / "3 条回调: ..."). 留空也没问题.
    """
    template = _EXTERNAL_EVENT_BANNER_TEMPLATES.get(kind)
    if not template:
        # avatar 路径不配 banner; 任何未来新 kind 也默认不配, 直到明确
        # 写进 _EXTERNAL_EVENT_BANNER_TEMPLATES.
        return
    content = template
    if extra_suffix:
        content = f"{content} · {extra_suffix}"
    banner = make_message(
        role=ROLE_SYSTEM,
        content=content,
        timestamp=session.clock.now(),
        source=SOURCE_EXTERNAL_EVENT_BANNER,
    )
    try:
        append_message(session, banner, on_violation="coerce")
    except Exception as exc:  # noqa: BLE001
        # Don't let a banner write failure bubble and abort the whole
        # simulation — banner is convenience-only, the real assistant
        # reply is what tester actually cares about.
        python_logger().warning(
            "external_events: failed to append banner for %s: %s: %s",
            kind.value, type(exc).__name__, exc,
        )


def _record_and_return(
    session: Session,
    kind: SimulationKind,
    result: SimulationResult,
    *,
    detail: Optional[dict[str, Any]] = None,
) -> SimulationResult:
    """Single exit point — record a diagnostics op + return the result.

    Funnelling every handler return through here keeps the op naming
    convention and ``detail`` shape consistent; future handlers only need
    to hand us a ``detail`` dict.
    """
    op = _KIND_TO_OP[kind]
    mirror_info = result.mirror_to_recent_info
    merged_detail: dict[str, Any] = {
        "accepted": result.accepted,
        "reason": result.reason,
        "persisted": result.persisted,
        "elapsed_ms": result.elapsed_ms,
        "mirror_to_recent": {
            "requested": mirror_info.requested,
            "applied": mirror_info.applied,
            "fallback_reason": mirror_info.fallback_reason,
        },
        "coerce_count": len(result.coerce_info),
    }
    if result.dedupe_info is not None:
        merged_detail["dedupe_hit"] = bool(result.dedupe_info.get("hit"))
    if detail:
        merged_detail.update(detail)

    try:
        # avatar dedupe hit is explicitly logged as info — not an error; we
        # still want the op in the ring for event-density inspection.
        diagnostics_store.record_internal(
            op,
            f"外部事件仿真 kind={kind.value} accepted={result.accepted} reason={result.reason or '-'}",
            level="info",
            session_id=getattr(session, "id", None),
            detail=merged_detail,
        )
    except Exception:  # noqa: BLE001
        python_logger().exception(
            "external_events: record_internal(%s) failed (non-fatal)", op.value,
        )

    try:
        session.logger.log_sync(
            f"external_event.{kind.value}",
            payload={
                "accepted": result.accepted,
                "reason": result.reason,
                "persisted": result.persisted,
                "elapsed_ms": result.elapsed_ms,
                "mirror_to_recent_info": asdict(mirror_info),
                "coerce_count": len(result.coerce_info),
                "instruction_chars": len(result.instruction),
                "reply_chars": len(result.assistant_reply),
                **(detail or {}),
            },
        )
    except Exception:  # noqa: BLE001
        python_logger().exception(
            "external_events: session.logger.log_sync failed (non-fatal)",
        )

    return result


# ─────────────────────────────────────────────────────────────
# Misc
# ─────────────────────────────────────────────────────────────


def _elapsed(started_perf: float) -> int:
    return int((time.perf_counter() - started_perf) * 1000)


__all__ = [
    "CoerceInfo",
    "ExternalEventPreview",
    "MirrorToRecentInfo",
    "SimulationKind",
    "SimulationResult",
    "build_external_event_preview",
    "discard_session_caches",
    "peek_dedupe_info",
    "reset_dedupe",
    "simulate_agent_callback",
    "simulate_avatar_interaction",
    "simulate_proactive",
]
