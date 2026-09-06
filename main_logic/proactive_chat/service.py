# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Framework-independent proactive-chat orchestration service."""

import asyncio
import random
from typing import Any
from uuid import uuid4

from config import (
    MEMORY_SERVER_PORT,
    MINI_GAME_INVITE_ENABLED,
    MINI_GAME_INVITE_FORCE_GAME_TYPE,
    PROACTIVE_EXTERNAL_PER_ITEM_MAX_TOKENS,
    PROACTIVE_EXTERNAL_TOTAL_MAX_TOKENS,
    PROACTIVE_PHASE1_FETCH_PER_SOURCE,
    PROACTIVE_PHASE1_TOTAL_TOPICS,
)
from config.prompts.prompts_proactive import (
    EXTERNAL_TOPIC_FOOTER,
    EXTERNAL_TOPIC_HEADER,
    MEME_SECTION_FOOTER,
    MEME_SECTION_HEADER,
    MUSIC_SECTION_FOOTER,
    MUSIC_SECTION_HEADER,
    PROACTIVE_SOURCE_LABELS,
    SCREEN_WINDOW_TITLE,
    get_meme_topic_line,
    get_proactive_format_sections,
    get_screen_img_hint,
    get_screen_section_footer,
    get_screen_section_header,
    normalize_proactive_prompt_locale,
)
from config.prompts.prompts_sys import _loc
from main_logic.omni_realtime_client import OmniRealtimeClient
from main_logic.proactive_chat.contracts import (
    PROACTIVE_REASON_CHAT_DELIVERED,
    PROACTIVE_REASON_DELIVERY_PREEMPTED,
    PROACTIVE_REASON_ERROR_INTERNAL,
    PROACTIVE_REASON_ERROR_SOURCE_FETCH_FAILED,
    PROACTIVE_REASON_ERROR_TIMEOUT,
    PROACTIVE_REASON_PASS_ACTIVITY_BUSY,
    PROACTIVE_REASON_PASS_BUSY,
    PROACTIVE_REASON_PASS_DISABLED,
    PROACTIVE_REASON_PASS_DUPLICATE,
    ProactiveChatCommand,
    ProactiveChatResult,
    _proactive_chat_body,
    _proactive_error_body,
    _proactive_pass_body,
)
from main_logic.proactive_chat.decisions import (
    _decide_activity_schedule,
    _decide_busy_entry_guard,
    _decide_closed_activity_gate,
    _decide_empty_source_gate,
    _decide_game_route_entry_guard,
    _decide_manager_entry_guard,
    _decide_probabilistic_activity_gate,
    _select_source_modes,
    _select_weighted_sources,
    _should_fetch_activity_snapshot,
    _should_skip_source,
    _should_use_voice_fast_path,
)
from main_logic.proactive_chat.delivery import (
    CommittedDelivery,
    DeliveryCommit,
    ProactiveLifecycle,
    _commit_proactive_delivery,
    _record_committed_delivery,
)
from main_logic.proactive_chat.candidate_selection import (
    _format_phase1_link_candidate,
    _number_phase1_links_by_source,
    _phase1_linkless_modes,
    _round_robin_phase1_links,
)
from main_logic.proactive_chat.generation import (
    Phase2PromptContext,
    ProactiveModelConfig,
    _append_directives_section,
    _decide_phase1_channels,
    _fetch_phase1_followups,
    _lookup_link_by_phase1_selection,
    _proactive_llm_retry_error_types,
    _run_phase2_generation,
    _run_unified_phase1,
)
from main_logic.proactive_chat.mini_game_invite import (
    _advance_mini_game_invite_entry,
    _build_mini_game_invite_options_payload,
    _last_user_message_at_from_activity,
    _mini_game_invite_count_post_response_chat,
    _mini_game_invite_get_state,
    _mini_game_invite_record_delivered,
    _pick_mini_game_type,
)
from main_logic.proactive_chat.music_recommendation import (
    _build_music_dynamic_context,
    _build_music_playing_hint,
    _select_music_recommendation,
)
from main_logic.proactive_chat.state import (
    _enter_proactive_phase2,
    _ensure_source_history_loaded,
    _format_recent_proactive_chats,
    _increment_proactive_chat_total,
    _record_invite_delivery_persistent,
    _record_source_used,
    _source_hash,
)
from main_logic.proactive_chat.sources import collect_proactive_sources
from utils.web_scraper.proactive_candidate import (
    SelectedWebCandidatePreempted,
    prepare_selected_web_candidate,
)
from utils.language_utils import (
    get_global_language,
    get_global_language_full,
    is_supported_language_code,
    normalize_language_code,
)
from utils.logger_config import get_module_logger
from utils.meme_moderation import moderate_meme_image_url
from utils.music_crawlers import mark_music_as_played
from .break_reminders import (
    _compose_break_system_prompt,
    _deliver_break_reminder_via_llm,
    _render_anti_slack_prompt,
    _render_work_break_game_invite_prompt,
    _render_work_break_prompt,
)

__all__ = [
    "CommittedDelivery",
    "DeliveryCommit",
    "ProactiveLifecycle",
    "_commit_proactive_delivery",
    "_record_committed_delivery",
    "handle_proactive_chat",
]


logger = get_module_logger(__name__, "Main")


def _consume_repeat_suppressed_break_reminder(
    mgr,
    *,
    lanlan_name: str,
    channel: str,
) -> None:
    """Consume a must-fire source that the repeat guard deliberately dropped."""
    marker_name = (
        "mark_anti_slack_used"
        if channel == "anti_slack"
        else "mark_work_break_used"
    )
    try:
        getattr(mgr._activity_tracker, marker_name)()
    except Exception as mark_err:
        logger.warning(
            "[%s] %s after repeat suppression failed: %s",
            lanlan_name,
            marker_name,
            mark_err,
        )


_MEME_PROXY_CANDIDATE_CHECK_LIMIT = 3


_PHASE1_FETCH_PER_SOURCE = (
    PROACTIVE_PHASE1_FETCH_PER_SOURCE  # Phase 1 每个信息源固定抓取条数
)


_PHASE1_TOTAL_TOPIC_TARGET = (
    PROACTIVE_PHASE1_TOTAL_TOPICS  # Phase 1 输入给筛选模型的总候选目标条数
)


def _open_threads_for_activity_state(
    activity_snapshot, fresh_open_threads
) -> list[str]:
    """Return semantic open_threads that should render in activity state.

    ``unfinished_thread`` is a stronger, rule-based continuation signal (the
    previous AI question is still hanging and may bypass normal propensity).
    When it exists, suppress softer LLM-enriched open_threads so Phase 2 sees a
    single follow-up surface. Also suppress open_threads during
    ``restricted_screen_only`` states: those rounds allow screen-derived chatter
    only, with unfinished_thread as the explicit text-only continuation
    exception. Otherwise keep open_threads in activity state, where they sit
    next to live state/tone rather than old reminiscence.
    """
    if activity_snapshot is None:
        return list(fresh_open_threads or [])
    if getattr(activity_snapshot, "unfinished_thread", None) is not None:
        return []
    if getattr(activity_snapshot, "propensity", None) == "restricted_screen_only":
        return []
    return list(fresh_open_threads or [])


def _render_followup_topic_hooks(
    proactive_lang: str,
    followup_topics: list[dict[str, Any]],
) -> tuple[str, list[Any]]:
    """Render follow-up topic hooks and return the surfaced reflection ids.

    Only reflections whose text actually survives build_topic_hook_prompt's
    blank/duplicate filter are reported as surfaced. Otherwise a blank or
    duplicate followup inside the first three would still be recorded via
    /record_surfaced and pushed into cooldown even though the model never saw
    it. Semantic open_threads intentionally do not flow through this helper:
    they render inside the activity-state section, where the live state/tone
    and decision rules can arbitrate them separately from old reminiscence.
    """
    if not followup_topics:
        return "", []

    from main_logic.topic.common import clean_text
    from main_logic.topic.hooks import build_topic_hook_prompt

    rendered_followup_topics = followup_topics[:3]
    prompt = build_topic_hook_prompt(
        proactive_lang,
        followup_topics=rendered_followup_topics,
    )
    if not prompt:
        return "", []

    # Mirror _iter_followup_texts: drop blanks/duplicates so the surfaced ids
    # match exactly what the prompt rendered.
    surfaced_reflection_ids: list[Any] = []
    seen_texts: set[str] = set()
    for topic in rendered_followup_topics:
        text = clean_text(topic.get("text"))
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        if topic.get("id"):
            surfaced_reflection_ids.append(topic["id"])
    return prompt, surfaced_reflection_ids


def _command_language_candidates(
    command_or_data: ProactiveChatCommand | dict,
) -> tuple[Any, Any, Any]:
    """Read locale aliases from the new command or the compatibility dict."""
    if isinstance(command_or_data, ProactiveChatCommand):
        return command_or_data.language_candidates
    return (
        command_or_data.get("language"),
        command_or_data.get("lang"),
        command_or_data.get("i18n_language"),
    )


def _command_render_language(command_or_data: ProactiveChatCommand | dict) -> Any:
    """Read a request-only template locale that must never become durable state."""
    if isinstance(command_or_data, ProactiveChatCommand):
        return command_or_data.render_language
    return command_or_data.get("render_language")


def _resolve_proactive_locale(
    data: ProactiveChatCommand | dict,
    mgr,
    *,
    fmt: str = "short",
) -> str:
    """Resolve the active user locale for proactive chat flows.

    An explicit request preference wins first, followed by an explicit manager
    preference. A request-only render locale may override only a non-explicit
    manager fallback; process-level global language remains the final fallback.
    This aligns render-only responses with the live UI without shadowing a
    durable per-character preference.

    Three output shapes, one precedence chain — which is the point of the parameter
    rather than a second resolver (issue #2500):

    - ``fmt="short"`` — a language family (``zh``). No room for a script, so
      ``zh-TW`` and ``zh-CN`` both come out ``zh``.
    - ``fmt="full"`` — a BCP-47 locale (``zh-CN`` / ``zh-TW``). This is the key
      scheme of ``static/locales/*.json``.
    - ``fmt="prompt"`` — a prompt-dict key (``zh`` / ``zh-TW``). This is the key
      scheme of every table in ``config/prompts``, and it is NOT the same as
      ``full``: those tables have no ``zh-CN`` row, so a full locale misses and
      degrades Simplified users to English on the plain ``dict.get`` lookups
      (``MUSIC_SEARCH_RESULT_TEXTS``, ``RECENT_PROACTIVE_TIME_LABELS``,
      ``WORK_BREAK_GENERIC_WORK_LABEL``, ...). Use this wherever the result ends
      up indexing a prompt dict; it keeps ``zh-TW`` reachable without stranding
      ``zh-CN``.
    """
    def _normalize(value: str) -> str:
        if fmt == "prompt":
            return normalize_proactive_prompt_locale(value)
        return normalize_language_code(value, format=fmt)

    request_lang = next(
        (value for value in _command_language_candidates(data) if value),
        None,
    )
    # 与 ``main_routers/game_router._absorb_request_language`` 同形：第三方客户端 /
    # corrupted localStorage 可能传 ``'undefined'`` / ``'estonian'`` 等 garbage，
    # ``normalize_language_code`` 对未识别值默认回退 ``'en'``——必须先用公共白名单
    # 挡掉，否则 proactive 邀请文案会被静默短路成英文，错过本应命中的 session 真值。
    if request_lang and is_supported_language_code(request_lang):
        normalized = _normalize(request_lang)
        if normalized:
            return normalized

    session_lang = getattr(mgr, "user_language", None)
    session_language_is_explicit = bool(
        getattr(mgr, "_user_language_explicit", False)
    )
    normalized_session_lang = None
    if session_lang and is_supported_language_code(session_lang):
        normalized_session_lang = _normalize(session_lang) or None

    if session_language_is_explicit and normalized_session_lang:
        return normalized_session_lang

    render_lang = _command_render_language(data)
    if render_lang and is_supported_language_code(render_lang):
        normalized = _normalize(render_lang)
        if normalized:
            return normalized

    if normalized_session_lang:
        return normalized_session_lang
    if fmt == "prompt":
        return normalize_proactive_prompt_locale(get_global_language_full()) or "en"
    if fmt == "full":
        return get_global_language_full() or "en"
    return get_global_language() or "en"


def _resolve_topic_hook_locale(
    data: ProactiveChatCommand | dict,
    mgr,
    *,
    fallback: str,
) -> str:
    """Resolve the locale for topic-hook prompts without collapsing zh-TW."""
    declared = _resolve_declared_topic_hook_locale(data, mgr)
    if declared:
        return declared
    render_lang = _command_render_language(data)
    if render_lang and is_supported_language_code(render_lang):
        normalized = normalize_language_code(render_lang, format="full")
        if normalized:
            return normalized
    global_lang = normalize_language_code(get_global_language_full(), format="full")
    if global_lang:
        return global_lang
    return fallback


def _resolve_declared_topic_hook_locale(
    data: ProactiveChatCommand | dict,
    mgr,
) -> str | None:
    """Resolve only a locale explicitly declared by the request or session."""
    for raw_lang in _command_language_candidates(data):
        if raw_lang and is_supported_language_code(raw_lang):
            normalized = normalize_language_code(raw_lang, format="full")
            if normalized:
                return normalized
    if not getattr(mgr, "_user_language_explicit", False):
        return None
    raw_lang = getattr(mgr, "user_language", None)
    if raw_lang and is_supported_language_code(raw_lang):
        return normalize_language_code(raw_lang, format="full") or None
    return None


def _new_dialog_locale_params(
    data: ProactiveChatCommand | dict,
    mgr,
) -> dict[str, str] | None:
    """Pass durable and render-only locale evidence through separate fields."""
    params: dict[str, str] = {}
    declared = _resolve_declared_topic_hook_locale(data, mgr)
    if declared:
        params["language"] = declared
    render_language = _command_render_language(data)
    if render_language and is_supported_language_code(render_language):
        normalized_render_language = normalize_language_code(
            render_language,
            format="full",
        )
        if normalized_render_language:
            params["render_language"] = normalized_render_language
    return params or None


def _resolve_phase2_avatar_position(fresh, request_avatar_position):
    """Pick the avatar position to annotate a freshly fetched Phase 2 screenshot with.

    Who is allowed to place the annotation depends on how the image was produced:

    - ``'websocket'``: the frontend already judged THIS image. A position means
      "annotate here"; ``None`` means it deliberately decided not to annotate
      (window capture, camera, avatar collapsed, multi-monitor). Never substitute
      the request's older position for that ``None`` -- it is a verdict, not a
      missing value. Same rule as ``main_logic.core.streaming``, which refuses to
      fall back to the cached position for exactly this reason.
    - ``'backend_fallback'``: the frontend never saw this image, so it cannot have
      objected. The position carried by the original request is the only avatar
      information that exists for this path.

    Neither branch of ``request_fresh_screenshot`` draws the annotation itself;
    the caller does it with whatever this returns.
    """
    source = getattr(fresh, "source", "")
    if source == "backend_fallback":
        return request_avatar_position
    return getattr(fresh, "avatar_position", None)


async def handle_proactive_chat(
    command: ProactiveChatCommand,
    *,
    config_manager: Any,
    session_manager: Any,
    character_data: tuple[Any, ...],
    game_route_active_for: Any,
    break_config_manager_provider: Any,
    run_mini_game_invite_short_circuit: Any,
    push_mini_game_invite_options: Any,
    push_mini_game_invite_resolved: Any,
    meme_proxy_candidate_fetchable: Any | None = None,
) -> ProactiveChatResult:
    """
    Proactive chat: two-phase architecture — Phase 1 merged LLM (web screening + music/meme keywords, 1 call), Phase 2 persona-aware chat generation.
    """
    lifecycle: ProactiveLifecycle | None = None
    try:
        _config_manager = config_manager
        # Character data is intentionally fetched by the Router before JSON
        # parsing to preserve the endpoint's legacy exception priority.
        master_name_current, her_name_current, _, _, _, lanlan_prompt_map, _, _, _ = (
            character_data
        )
        lanlan_name = command.lanlan_name or her_name_current
        is_playing_music = command.is_playing_music
        is_music_occupied = command.is_music_occupied
        current_track = command.current_track
        music_cooldown = command.music_cooldown

        # 获取session manager
        mgr = session_manager.get(lanlan_name)
        manager_exists = bool(mgr)
        entry_result = _decide_manager_entry_guard(
            lanlan_name,
            manager_exists=manager_exists,
            goodbye_silent=(
                getattr(mgr, "is_goodbye_silent", lambda: False)()
                if manager_exists
                else False
            ),
        )
        if entry_result is not None:
            if entry_result.body.get("reason_code") == PROACTIVE_REASON_PASS_DISABLED:
                logger.info("[%s] 主动搭话本轮未发起：goodbye silent", lanlan_name)
            return ProactiveChatResult(
                body=entry_result.body,
                status_code=entry_result.status_code,
            )

        try:
            game_route_active = bool(game_route_active_for(lanlan_name))
        except Exception as game_route_err:
            logger.warning(
                "[%s] proactive game-route guard failed closed: %s",
                lanlan_name,
                game_route_err,
            )
            game_route_active = None
        entry_result = _decide_game_route_entry_guard(game_route_active)
        if entry_result is not None:
            if game_route_active:
                logger.info("[%s] 主动搭话本轮未发起：游戏路由 active", lanlan_name)
            return ProactiveChatResult(
                body=entry_result.body,
                status_code=entry_result.status_code,
            )

        state_memory_dir = getattr(_config_manager, "memory_dir", None)
        state_storage_kwargs = (
            {"memory_dir": state_memory_dir}
            if state_memory_dir is not None
            else {}
        )

        # 检查能否发起新一轮主动搭话：状态机统一把 "AI 正在响应"（_is_responding）、
        # "另一轮 proactive 在跑"（phase != IDLE）两个信号收拢到 O(1) 判定。
        # mgr.is_active 仅用于判断 session 是否已实例化，故仍需保留。
        probe_session = mgr.session if mgr.is_active else None

        # ========== Voice mode fast path ==========
        # 语音模式下不走 Phase1/Phase2，不占 SM 的 proactive phase；先用只读
        # can_start_proactive 做 409 判定即可。
        # Preserve the original short-circuit evaluation order: a text request
        # must not re-read manager/session properties solely to decide the voice
        # path, and an inactive manager must not read ``mgr.session`` here.
        if command.voice_mode:
            manager_active = mgr.is_active
            realtime_session = (
                isinstance(mgr.session, OmniRealtimeClient) if manager_active else False
            )
        else:
            manager_active = False
            realtime_session = False
        use_voice_fast_path = _should_use_voice_fast_path(
            voice_mode=command.voice_mode,
            manager_active=manager_active,
            realtime_session=realtime_session,
        )
        if use_voice_fast_path:
            # Mini-game invite 状态机推进：voice fast path 不走 activity tracker，
            # 直接用 session 自己跟踪的「用户最后一次真实消息时间」喂给
            # advance_response。否则纯 voice 用户收到 mini-game 邀请回应后，
            # pending 永远翻不掉，邀请会被永久抑制；CodeRabbit Major review 指出。
            #
            # ⚠️ 用 last_user_message_time（仅真实非空非 echo 用户输入）而非
            # last_user_activity_time（顶部无条件刷新，含 VAD 空噪声 + 麦克风录回
            # AI 自己 TTS 的回声）。后者会被 AI 念邀请台词的回声污染：邀请投递后
            # 回声立刻把 activity 刷到 > delivered_at，下一个 tick 的隐式 dismiss
            # 误判「用户已回应」→ 把 pending 邀请清成 'later'（5min）+ 撤掉按钮，
            # 用户随后点「现在不想玩」落到 expired、真正的 5h decline 起不来、邀请
            # 5min 后反复重来。改用真消息时间戳后，纯点按钮（不说话）的用户活动
            # 时间不会越过 delivered_at，pending 一直留到用户显式点按钮 / 说话。
            _voice_entry_advance = _advance_mini_game_invite_entry(
                lanlan_name,
                getattr(mgr, "last_user_message_time", None),
            )
            # advance 触发了隐式 dismiss → 推 WS 让前端清掉 prompt UI（cross-window
            # 一致性）。codex P2 指出非按钮路径漏推 WS 让 UI 挂着。
            if _voice_entry_advance is not None:
                await push_mini_game_invite_resolved(
                    mgr,
                    session_id=_voice_entry_advance.session_id,
                    action=_voice_entry_advance.action,
                )
            can_start_proactive = mgr.state.can_start_proactive(session=probe_session)
            entry_result = _decide_busy_entry_guard(
                can_start_proactive,
                state_snapshot=(None if can_start_proactive else mgr.state.snapshot()),
            )
            if entry_result is not None:
                logger.info(
                    "[%s] 主动搭话本轮未发起：语音模式 AI 正在响应中（409）",
                    lanlan_name,
                )
                return ProactiveChatResult(
                    body=entry_result.body,
                    status_code=entry_result.status_code,
                )
            voice_language = _resolve_proactive_locale(command, mgr, fmt="full")
            delivered = await mgr.trigger_voice_proactive_nudge(
                language=voice_language,
            )
            if delivered:
                # 1h+10 chats 冷却的 chat counter：voice nudge 也算一次主动搭话，
                # 与 text path 在 _record_proactive_chat 之后调 count 对称。
                _mini_game_invite_count_post_response_chat(lanlan_name)
                # 持久化"累计成功投递的主动搭话总数"，给 force-first 用。
                await _increment_proactive_chat_total(
                    lanlan_name, **state_storage_kwargs
                )
            else:
                logger.info(
                    "[%s] 主动搭话本轮未发起：语音 nudge 被 guard 跳过", lanlan_name
                )
            # No Focus cooldown here: a voice nudge is realtime and never runs a
            # Focus thinking-on reply, so it is not a Focus proactive turn — the
            # cooldown is applied only at the text Phase-2 idle path (which is
            # where _focus_idle_thinking actually gates thinking-on).
            if delivered:
                return ProactiveChatResult(
                    body=_proactive_chat_body(
                        PROACTIVE_REASON_CHAT_DELIVERED,
                        message="voice proactive triggered",
                    )
                )
            return ProactiveChatResult(
                body=_proactive_pass_body(
                    PROACTIVE_REASON_PASS_BUSY,
                    message="voice proactive skipped (guard)",
                )
            )

        # ========== Text-mode proactive：原子 "检查 + 占坑" ==========
        # try_start_proactive 在 _write_lock 内完成 can_start_proactive 判定 + 翻
        # IDLE→PHASE1 + 订阅派发，避免并发请求双双通过 can_start_proactive 后
        # 各自 fire(PROACTIVE_START) 导致两路 proactive 同时进入 PHASE1。
        from main_logic.session_state import SessionEvent as _SE

        lifecycle = ProactiveLifecycle(
            mgr=mgr,
            state_event=_SE.PROACTIVE_DONE,
            lanlan_name=lanlan_name,
            log=logger,
        )
        proactive_started = await mgr.state.try_start_proactive(session=probe_session)
        entry_result = _decide_busy_entry_guard(
            proactive_started,
            state_snapshot=(None if proactive_started else mgr.state.snapshot()),
        )
        if entry_result is not None:
            logger.info(
                "[%s] 主动搭话本轮未发起：AI 正在响应或已有一轮在跑（409）", lanlan_name
            )
            return ProactiveChatResult(
                body=entry_result.body,
                status_code=entry_result.status_code,
            )

        async def _end_proactive(
            result: ProactiveChatResult,
        ) -> ProactiveChatResult:
            """Finalize every normal/short-circuit service exit."""
            return await lifecycle.finalize(result)

        def _proactive_preempted_json(where: str) -> dict:
            # 细粒度的 state 快照留 debug；面向排查的"本轮未发起 + 原因"由统一
            # 出口 _end_proactive 按 message 打 info（这些 dict 全部经它返回），
            # 避免同一轮 skip 打出两条重复 info。
            logger.debug(
                "[%s] proactive %s preempted by user takeover (state=%s)",
                lanlan_name,
                where,
                mgr.state.snapshot(),
            )
            return {
                "success": True,
                "action": "pass",
                "reason_code": PROACTIVE_REASON_DELIVERY_PREEMPTED,
                "message": f"proactive {where} preempted by user takeover",
            }

        print(f"[{lanlan_name}] 开始主动搭话流程（两阶段架构）...")

        # ========== 拉用户活动快照 ==========
        # 在 enabled_modes 解析之前拉一次，因为 propensity 可能需要把
        # enabled_modes 收紧到只剩 vision（restricted_screen_only 状态）。
        # 详见 docs/design/user-activity-tracker.md。
        #
        # 隐私模式：用户开了"隐私模式"开关 → 临时禁用整个 user-activity-tracker，
        # 回退到 PR #1015 之前的无限制策略。snapshot 留 None，下游所有 gating
        # 都已在 PR #1015 设计时按 "snapshot is not None" 写过 fallback：
        #   - propensity 收紧（restricted_screen_only）→ 不触发
        #   - 反思/回忆 _allow_reminiscence → 默认放开
        #   - state_section 渲染 → 输出空串
        #   - mark_unfinished_thread_used → 不计数
        # 所以这里把 snapshot 直接设 None 就够，等价于"tracker 不存在"。
        from utils.preferences import ais_privacy_mode_enabled

        try:
            privacy_mode = await ais_privacy_mode_enabled()
        except Exception as _pm_err:
            # fail-closed：读不出来按隐私开启处理。正常"用户没开隐私"是
            # ais_privacy_mode_enabled 返回 False，不进这个 except。
            logger.warning(
                f"[{lanlan_name}] privacy mode check failed, defaulting to enabled: {_pm_err}",
            )
            privacy_mode = True
        if not _should_fetch_activity_snapshot(privacy_mode):
            print(
                f"[{lanlan_name}] 隐私模式开启，跳过 activity tracker，按无限制策略搭话"
            )
            activity_snapshot = None
        else:
            try:
                activity_snapshot = await mgr._activity_tracker.get_snapshot()
                print(
                    f"[{lanlan_name}] activity snapshot: state={activity_snapshot.state} "
                    f"propensity={activity_snapshot.propensity} reasons={activity_snapshot.propensity_reasons} "
                    f"skip_prob={activity_snapshot.skip_probability:.2f} tone={activity_snapshot.tone}"
                )
            except Exception as _act_err:
                logger.warning(
                    f"[{lanlan_name}] activity snapshot fetch failed: {_act_err}; falling back to open propensity"
                )
                activity_snapshot = None

        # 进 proactive_chat 后第一时间推进 mini-game invite 的"已回应"判定：
        # 即便本轮不发邀请，pending 的上一次邀请也得在用户已说话时翻成已回应，
        # 否则 cooldown 永远卡在 pending。Text path 从 activity_snapshot 反推
        # last_user_msg_at；voice fast path 在上面的 voice block 内独立调一次
        # （用 mgr.last_user_activity_time），两边对称。
        _text_last_user_msg_at = _last_user_message_at_from_activity(
            activity_snapshot,
        )
        _text_entry_advance = _advance_mini_game_invite_entry(
            lanlan_name,
            _text_last_user_msg_at,
        )
        # 隐式 dismiss 推 WS（同 voice fast path 对称，codex P2）
        if _text_entry_advance is not None:
            await push_mini_game_invite_resolved(
                mgr,
                session_id=_text_entry_advance.session_id,
                action=_text_entry_advance.action,
            )

        # 用户级 toggle：前端 CHAT_MODE_CONFIG 里的 ``proactiveMiniGameInviteEnabled``
        # 通过 request body 的 ``mini_game_invite_enabled`` 字段透传。缺省 True 兼容
        # 旧客户端。提到 _debug_force_invite 计算之前——把 user toggle 关同时
        # 服务端开了调试旗标的场景下，下游早退 gate（closed / skip_probability）
        # 也维持原有抑制语义；不能因为旗标开了就把 gate 一并 bypass 掉。
        # CodeRabbit Major review 指出原版只在 _maybe_deliver_mini_game_invite
        # 入口拦 user toggle，旗标已经把上游 gate 绕过 → 进 _maybe_deliver
        # 又被 toggle 拦 None → caller 走普通 source picking，封禁场景仍然漏过。
        _user_invite_toggle = command.mini_game_invite_enabled

        # 调试旗标 ``MINI_GAME_INVITE_FORCE_GAME_TYPE`` 非 None 时绕开本函数所有
        # 上游早退 gate（closed / skip_probability / restricted_screen_only），
        # 让 ``_maybe_deliver_mini_game_invite`` 能稳定接到本轮调用——契约是
        # "开启后主动搭话必定触发特定小游戏"。仅本地手测使用；生产
        # ``MINI_GAME_INVITE_ENABLED`` 总开关 + 旗标默认 None 双保险。
        # 用户 toggle 关时旗标无效（与 _maybe_deliver_mini_game_invite 入口
        # 的 toggle 检查同语义，单一事实源在前端 toggle）。
        # CodeRabbit Major 指出：这条不在 ``_maybe_deliver_mini_game_invite``
        # 内部加是因为那时已经过了上游 gate，旗标做不到"必定"。
        _debug_force_invite = (
            MINI_GAME_INVITE_FORCE_GAME_TYPE is not None and _user_invite_toggle
        )

        # ========== Hard short-circuit: propensity=closed ==========
        # ``private`` state pins propensity to ``closed`` (see
        # main_logic/activity/snapshot.py). Skip everything — no LLM,
        # no source fetch, no prompt assembly. The user is in a
        # password manager / banking app / etc and we promised not to
        # look. Bypassed for the unfinished_thread override is
        # deliberate: if the AI just asked a question, hanging on it
        # mid-private is rude. closed > thread.
        activity_result = _decide_closed_activity_gate(
            activity_snapshot,
            debug_force_invite=_debug_force_invite,
        )
        if activity_result is not None:
            print(
                f"[{lanlan_name}] propensity=closed (state={activity_snapshot.state}), 跳过本轮 proactive"
            )
            return await _end_proactive(
                ProactiveChatResult(
                    body=activity_result.body,
                    status_code=activity_result.status_code,
                )
            )

        # ========== Screen-only：固定间隔 + 后端抖动 ==========
        # 用户处于 gaming / focused_work（propensity=restricted_screen_only）
        # 时，常规的前端 3-tier 退避会让搭话间隔指数级增长，跟陪伴产品
        # 命题冲突（用户最长会话段反而最安静）。改用：
        #   1. 前端 reset backoffLevel=0 并按 baseInterval 等间隔触发
        #      （由响应里的 next_schedule_fixed_mode=True 通知前端切换）
        #   2. 后端在 LLM 调用前 sleep uniform(0, 0.5 * baseInterval)，把每轮
        #      实际间隔从 base 抹成 [base, 1.5*base] 的均匀分布
        # 总效果：屏幕态平均间隔 ≈ 1.25*base，且有自然的随机抖动。
        # skip_probability（仅 immersive_horror=0.3）作为正交机制保留。
        #
        # ⚠️ 标志位 vs sleep 拆开：anti_slack_pending / work_break_pending
        # 是 focused_work 下的 must-fire 提醒（紧跟在下一段 4425+），本身
        # 时间敏感，不能被这里的随机抖动延后。但前端 fixed_mode 标志位
        # 仍然要设——否则 must-fire 走 _end_proactive 时响应里会带回
        # next_schedule_fixed_mode=False，前端误切回 tier backoff，让用户
        # 离开 must-fire 状态后又被退避机制吞掉一段时间。
        # Codex P2 + CodeRabbit Major review。
        activity_schedule = _decide_activity_schedule(
            activity_snapshot,
            base_interval_seconds=command.base_interval_seconds,
        )
        lifecycle.set_fixed_schedule(activity_schedule.fixed_mode)
        if activity_schedule.fixed_mode:
            if activity_schedule.has_must_fire:
                print(
                    f"[{lanlan_name}] propensity=restricted_screen_only 但有 must-fire 提醒待发，跳过本轮抖动 sleep"
                )
            elif activity_schedule.jitter_max > 0:
                _jitter = random.uniform(0.0, activity_schedule.jitter_max)
                print(
                    f"[{lanlan_name}] propensity=restricted_screen_only, "
                    f"后端注入 {_jitter:.2f}s 间隔抖动"
                    f"（base={activity_schedule.base_interval:.1f}s）"
                )
                await asyncio.sleep(_jitter)

        # ========== Must-fire: break-reminder branches ==========
        # Anti-slack outranks water-break (transition trigger more
        # time-sensitive than the cumulative one). Both bypass Phase 1
        # entirely and run via _deliver_break_reminder_via_llm — see
        # the helper docstring above. ``private`` state already cleared
        # both pendings inside the tracker, so reaching here implies
        # not-private. Debug-force-invite still takes precedence so the
        # mini-game force flag keeps its "guaranteed mini-game" contract.
        if (
            not _debug_force_invite
            and activity_snapshot is not None
            and (
                activity_snapshot.anti_slack_pending is not None
                or activity_snapshot.work_break_pending is not None
            )
        ):
            try:
                # break reminder 的消费点全是 prompt dict（休息提醒模板、
                # WORK_BREAK_* 兜底 label、_loc 出来的分隔符），一律要 prompt
                # key 而不是短码，否则 zh-TW 行取不到（issue #2500）。
                _break_lang = _resolve_proactive_locale(command, mgr, fmt="prompt")
                # 邀请按钮 label 经 normalize_mini_game_invite_locale 二次归一，
                # full locale 在那边同样收敛到 zh-TW，故保持 full 不动。
                _break_invite_lang = _resolve_proactive_locale(
                    command, mgr, fmt="full"
                )
            except Exception:
                _break_lang = "zh"
                _break_invite_lang = "zh"

            # Resolve character_prompt up front and prepend it to every
            # break-reminder SystemMessage. Without this the model would
            # see only the env-notice template and lose its persona —
            # CodeRabbit Major review (PR #1226). Mirrors the
            # placeholder substitution the normal Phase 2 path does at
            # line ~5300 (LANLAN_NAME / MASTER_NAME).
            _break_character_prompt = lanlan_prompt_map.get(lanlan_name, "")
            if _break_character_prompt:
                _break_character_prompt = _break_character_prompt.replace(
                    "{LANLAN_NAME}", lanlan_name
                ).replace("{MASTER_NAME}", master_name_current)

            # Anti-slack first — single-behavior 'back to work' nudge.
            if activity_snapshot.anti_slack_pending is not None:
                anti_pending = activity_snapshot.anti_slack_pending
                anti_prompt = _render_anti_slack_prompt(
                    pending=anti_pending,
                    master_name=master_name_current,
                    lang=_break_lang,
                )
                reminder_delivery = await _deliver_break_reminder_via_llm(
                    lanlan_name=lanlan_name,
                    mgr=mgr,
                    config_manager=break_config_manager_provider(),
                    system_prompt=_compose_break_system_prompt(
                        _break_character_prompt,
                        anti_prompt,
                    ),
                    channel="anti_slack",
                    lang=_break_lang,
                )
                delivered_text = reminder_delivery.delivered_text
                if reminder_delivery.repeat_suppressed:
                    _consume_repeat_suppressed_break_reminder(
                        mgr,
                        lanlan_name=lanlan_name,
                        channel="anti_slack",
                    )
                    return await _end_proactive(
                        ProactiveChatResult(
                            body={
                                "success": True,
                                "action": "pass",
                                "reason_code": PROACTIVE_REASON_PASS_DUPLICATE,
                                "message": "anti-slack reminder suppressed as repeated",
                            }
                        )
                    )
                if delivered_text:
                    try:
                        mgr._activity_tracker.mark_anti_slack_used()
                    except Exception as _mark_err:
                        logger.warning(
                            "[%s] mark_anti_slack_used failed: %s",
                            lanlan_name,
                            _mark_err,
                        )
                    # Mini-game cooldown counter — same contract as the
                    # normal text proactive path at ~6253: any successful
                    # proactive emission counts as one of the "10 chats
                    # since user responded" gate. No-op when no prior
                    # invite is pending. Codex/CodeRabbit Minor: PR #1226.
                    _mini_game_invite_count_post_response_chat(lanlan_name)
                    await _increment_proactive_chat_total(
                        lanlan_name, **state_storage_kwargs
                    )
                    return await _end_proactive(
                        ProactiveChatResult(
                            body={
                                "success": True,
                                "action": "chat",
                                "reason_code": PROACTIVE_REASON_CHAT_DELIVERED,
                                "message": "anti-slack reminder delivered",
                                "channel": "anti_slack",
                            }
                        )
                    )
                # Delivery rejected (user took over / config issue).
                # Don't fall through to normal proactive — must-fire
                # semantics: leave pending armed for the next round.
                return await _end_proactive(
                    ProactiveChatResult(
                        body={
                            "success": True,
                            "action": "pass",
                            "reason_code": PROACTIVE_REASON_DELIVERY_PREEMPTED,
                            "message": "anti-slack reminder pending but delivery skipped",
                        }
                    )
                )

            # Water-break — 50% pivots to a rest+game-invite combo
            # (gated on mini-game cooldown / user toggle / global
            # kill switch / existence of a valid game_type). Any of
            # those gates failing falls through to the regular
            # drink/stretch nudge instead of breaking the must-fire.
            water_pending = activity_snapshot.work_break_pending
            prefs_for_break = mgr._activity_tracker._sm._prefs
            _gi_prob = prefs_for_break.work_break_game_invite_probability
            if _gi_prob is None:
                # Resolved at import time — see tracker.py defaults.
                from main_logic.activity.tracker import (
                    _WORK_BREAK_GAME_INVITE_PROBABILITY as _gi_prob_default,
                )

                _gi_prob = _gi_prob_default
            branch_game_invite = False
            chosen_game_type: str | None = None
            gi_prompt: str | None = None
            if MINI_GAME_INVITE_ENABLED and _user_invite_toggle and _gi_prob > 0:
                import random as _random

                if _random.random() < _gi_prob:
                    chosen_game_type = _pick_mini_game_type(lanlan_name)
                    if chosen_game_type is not None:
                        gi_prompt = _render_work_break_game_invite_prompt(
                            pending=water_pending,
                            game_type=chosen_game_type,
                            master_name=master_name_current,
                            lang=_break_lang,
                        )
                        if gi_prompt is not None:
                            branch_game_invite = True

            if (
                branch_game_invite
                and chosen_game_type is not None
                and gi_prompt is not None
            ):
                reminder_delivery = await _deliver_break_reminder_via_llm(
                    lanlan_name=lanlan_name,
                    mgr=mgr,
                    config_manager=break_config_manager_provider(),
                    system_prompt=_compose_break_system_prompt(
                        _break_character_prompt,
                        gi_prompt,
                    ),
                    channel="work_break_game_invite",
                    lang=_break_lang,
                )
                delivered_text = reminder_delivery.delivered_text
                if reminder_delivery.repeat_suppressed:
                    _consume_repeat_suppressed_break_reminder(
                        mgr,
                        lanlan_name=lanlan_name,
                        channel="work_break_game_invite",
                    )
                    return await _end_proactive(
                        ProactiveChatResult(
                            body={
                                "success": True,
                                "action": "pass",
                                "reason_code": PROACTIVE_REASON_PASS_DUPLICATE,
                                "message": "work-break game invite suppressed as repeated",
                            }
                        )
                    )
                if delivered_text:
                    invite_session_id = str(uuid4())
                    _mini_game_invite_record_delivered(lanlan_name, invite_session_id)
                    _mini_game_invite_get_state(lanlan_name)["last_game_type"] = (
                        chosen_game_type
                    )
                    # Persist counter+1 + ever_delivered atomically (mini-game cooldown
                    # contract). Track success so we can fall back to the plain
                    # _increment_proactive_chat_total if persistence fails — otherwise
                    # the chat-total counter would skip this round entirely.
                    # CodeRabbit Major: don't double-count — the persistent record
                    # already does the +1, so plain counter is only the fallback.
                    _persist_ok = False
                    try:
                        await _record_invite_delivery_persistent(
                            lanlan_name, **state_storage_kwargs
                        )
                        _persist_ok = True
                    except Exception as _persist_err:
                        logger.warning(
                            "[%s] record_invite_delivery_persistent failed: %s",
                            lanlan_name,
                            _persist_err,
                        )
                    try:
                        from utils.instrument import counter as _instr_counter

                        # 与 proactive 通道共用 mini_game_invited，channel 维度区分；
                        # 不计 persist 成败——邀请 UI 已投递给用户即算一次邀请。
                        _instr_counter(
                            "mini_game_invited",
                            game_type=str(chosen_game_type)[:24],
                            channel="work_break",
                            force_first=False,
                        )
                    except Exception:
                        # 埋点 best-effort，失败不影响邀请投递
                        pass
                    options_payload = _build_mini_game_invite_options_payload(
                        invite_lang=_break_invite_lang,
                        game_type=chosen_game_type,
                        session_id=invite_session_id,
                    )
                    try:
                        await push_mini_game_invite_options(mgr, options_payload)
                    except Exception as _ws_err:
                        logger.warning(
                            "[%s] work_break+game_invite options WS push failed: %s",
                            lanlan_name,
                            _ws_err,
                        )
                    try:
                        mgr._activity_tracker.mark_work_break_used()
                    except Exception as _mark_err:
                        logger.warning(
                            "[%s] mark_work_break_used failed: %s",
                            lanlan_name,
                            _mark_err,
                        )
                    if not _persist_ok:
                        # Persistence path failed → counter wasn't bumped.
                        # Fall back to the plain in-memory increment so the
                        # round still counts toward proactive_chat totals.
                        await _increment_proactive_chat_total(
                            lanlan_name, **state_storage_kwargs
                        )
                    return await _end_proactive(
                        ProactiveChatResult(
                            body={
                                "success": True,
                                "action": "chat",
                                "reason_code": PROACTIVE_REASON_CHAT_DELIVERED,
                                "message": "work-break + game-invite delivered",
                                "channel": "work_break_game_invite",
                                "game_type": chosen_game_type,
                                "invite_session_id": invite_session_id,
                            }
                        )
                    )
                # Combo branch delivery failed → don't fall through to
                # regular drink branch (would double-charge the user's
                # attention). Pending stays armed for next round.
                return await _end_proactive(
                    ProactiveChatResult(
                        body={
                            "success": True,
                            "action": "pass",
                            "reason_code": PROACTIVE_REASON_DELIVERY_PREEMPTED,
                            "message": "work-break + game-invite pending but delivery skipped",
                        }
                    )
                )

            # Regular drink/stretch nudge branch
            wb_prompt, wb_seed = _render_work_break_prompt(
                pending=water_pending,
                master_name=master_name_current,
                lang=_break_lang,
            )
            reminder_delivery = await _deliver_break_reminder_via_llm(
                lanlan_name=lanlan_name,
                mgr=mgr,
                config_manager=break_config_manager_provider(),
                system_prompt=_compose_break_system_prompt(
                    _break_character_prompt,
                    wb_prompt,
                ),
                channel="work_break",
                lang=_break_lang,
            )
            delivered_text = reminder_delivery.delivered_text
            if reminder_delivery.repeat_suppressed:
                _consume_repeat_suppressed_break_reminder(
                    mgr,
                    lanlan_name=lanlan_name,
                    channel="work_break",
                )
                return await _end_proactive(
                    ProactiveChatResult(
                        body={
                            "success": True,
                            "action": "pass",
                            "reason_code": PROACTIVE_REASON_PASS_DUPLICATE,
                            "message": "work-break reminder suppressed as repeated",
                        }
                    )
                )
            if delivered_text:
                try:
                    mgr._activity_tracker.mark_work_break_used()
                except Exception as _mark_err:
                    logger.warning(
                        "[%s] mark_work_break_used failed: %s",
                        lanlan_name,
                        _mark_err,
                    )
                # Same chats-since-response counter as anti_slack branch.
                _mini_game_invite_count_post_response_chat(lanlan_name)
                await _increment_proactive_chat_total(
                    lanlan_name, **state_storage_kwargs
                )
                return await _end_proactive(
                    ProactiveChatResult(
                        body={
                            "success": True,
                            "action": "chat",
                            "reason_code": PROACTIVE_REASON_CHAT_DELIVERED,
                            "message": "work-break reminder delivered",
                            "channel": "work_break",
                            "seed": wb_seed,
                        }
                    )
                )
            return await _end_proactive(
                ProactiveChatResult(
                    body={
                        "success": True,
                        "action": "pass",
                        "reason_code": PROACTIVE_REASON_DELIVERY_PREEMPTED,
                        "message": "work-break reminder pending but delivery skipped",
                    }
                )
            )

        # ========== Probabilistic skip (intensity-driven gate) ==========
        # ``skip_probability`` is rolled BEFORE we burn LLM cost.
        # Default 0 for non-gaming and varied gaming, so this only
        # kicks in for tagged competitive / immersive-horror gaming
        # — or whatever combos the user has dialed up via
        # preferences.json::skip_probability_overrides.
        #
        # The unfinished_thread guard means open threads still get
        # follow-ups even at skip=1.0: if the AI promised to come
        # back to something, we honour that promise regardless of
        # how silenced the user wanted us. The thread mechanism's
        # 2-followup hard cap already prevents harassment.
        activity_result = _decide_probabilistic_activity_gate(
            activity_snapshot,
            debug_force_invite=_debug_force_invite,
        )
        if activity_result is not None:
            print(
                f"[{lanlan_name}] skip_probability={activity_snapshot.skip_probability:.2f} "
                f"rolled (state={activity_snapshot.state} intensity={activity_snapshot.game_intensity} "
                f"genre={activity_snapshot.game_genre})，本轮跳过"
            )
            return await _end_proactive(
                ProactiveChatResult(
                    body=activity_result.body,
                    status_code=activity_result.status_code,
                )
            )

        # ========== 解析 enabled_modes ==========
        # 兼容旧版前端：``enabled_modes`` 字段缺席 → 根据其它字段推断；显式传 ``[]``
        # 表示新版客户端"用户把所有 source toggle 都关了"，不能再走 BC fallback
        # 退化到 home/trending（否则 mini-game 邀请 toggle 单独开启的场景下 dice
        # miss 会让 home 兜底打破 toggle 契约——codex P1）。
        source_mode_selection = _select_source_modes(
            command,
            activity_snapshot,
            debug_force_invite=_debug_force_invite,
        )
        enabled_modes = source_mode_selection.enabled_modes
        _has_unfinished_thread = source_mode_selection.has_unfinished_thread

        # restricted_screen_only：用户处于 gaming / focused_work，仅允许屏幕通道。
        # 把 enabled_modes 收紧到只剩 vision。如果前端这一轮根本没启用 vision，
        # 直接 pass —— 没东西可看，又不让聊外部，没必要继续。
        # 例外：有未收尾话题（5min 内 AI 提的问题用户还没回）→ 即使没 vision
        # 也允许跑下去，跟进上一个问题不需要外部素材。
        if source_mode_selection.restricted_to_vision:
            print(
                f"[{lanlan_name}] propensity=restricted_screen_only, 收紧 enabled_modes 到仅 vision"
            )
        elif source_mode_selection.text_only_followup:
            print(
                f"[{lanlan_name}] propensity=restricted_screen_only 但有未收尾话题，允许 text-only 跟进"
            )
        elif source_mode_selection.result is not None:
            return await _end_proactive(
                ProactiveChatResult(
                    body=source_mode_selection.result.body,
                    status_code=source_mode_selection.result.status_code,
                )
            )

        print(f"[{lanlan_name}] 启用的搭话模式: {enabled_modes}")

        # ========== Mini-game 邀请短路 ==========
        # 过完 propensity / skip_probability / restricted_screen_only 这几道门后，
        # 独立掷一次 10% 骰子；命中即用静态 i18n 模板直投递邀请，跳过 Phase 1/2
        # LLM 与 source fetching。一次邀请被回应后 24h+10 chats cooldown，期间
        # 不再掷骰。activity_snapshot is None（隐私模式 / tracker 不可用）保守
        # 不发——无法判断是否在工作状态。
        try:
            # fmt="full"：邀请文案与三个按钮 label 都走带 zh-TW 行的 prompt dict，
            # 短码会把 zh-TW 折成 zh，那些行就永远取不到（issue #2500）。
            invite_lang = _resolve_proactive_locale(command, mgr, fmt="full")
        except Exception:
            invite_lang = "zh"
        # _user_invite_toggle 已经在上面 _debug_force_invite 计算前算过——把
        # toggle 关时旗标也连带禁用，保证早退 gate 不被绕过。
        invite_short_circuit = await run_mini_game_invite_short_circuit(
            lanlan_name=lanlan_name,
            mgr=mgr,
            activity_snapshot=activity_snapshot,
            invite_lang=invite_lang,
            master_name=master_name_current,
            user_toggle_enabled=_user_invite_toggle,
            **state_storage_kwargs,
        )
        if invite_short_circuit is not None:
            if invite_short_circuit.options_payload is not None:
                try:
                    await push_mini_game_invite_options(
                        mgr,
                        invite_short_circuit.options_payload,
                    )
                except Exception as _ws_err:
                    logger.warning(
                        "[%s] mini-game invite options WS push failed: %s",
                        lanlan_name,
                        _ws_err,
                    )
            return await _end_proactive(
                ProactiveChatResult(
                    body=invite_short_circuit.result.body,
                    status_code=invite_short_circuit.result.status_code,
                )
            )

        # 用户把所有 source toggle 都关了（仅留 mini-game 邀请独立 toggle 触发本轮
        # 请求），mini-game 短路又没命中：没什么可聊。直接 pass 而不是落到下面源
        # picking 走空 list / 撞 "所有信息源获取失败" 500 分支。例外：仍然有未收尾
        # 话题 → 让 Phase 2 走 text-only 跟进路径（与 sources={} 但 thread 在的兜
        # 底语义对齐）。codex P1 指出：BC fallback 已经按 "字段缺席 vs 显式 []" 分
        # 流，这里对显式空清晰退出。
        source_result = _decide_empty_source_gate(
            enabled_modes,
            has_unfinished_thread=_has_unfinished_thread,
        )
        if source_result is not None:
            print(
                f"[{lanlan_name}] enabled_modes 空 + mini-game miss + 无 unfinished_thread → pass"
            )
            return await _end_proactive(
                ProactiveChatResult(
                    body=source_result.body,
                    status_code=source_result.status_code,
                )
            )

        # 全局 source 衰减历史：进入 picking 前确保已惰性加载到内存（首次为线程池
        # IO，后续是 O(1) flag 检查）。同步 picking loop 后续直接读 dict。
        await _ensure_source_history_loaded(**state_storage_kwargs)

        # ========== 0. 并行获取所有信息源内容（无 LLM） ==========
        sources = await collect_proactive_sources(
            command=command,
            enabled_modes=enabled_modes,
            lanlan_name=lanlan_name,
            log=logger,
        )
        avatar_position = command.avatar_position

        if not sources:
            # 例外：未收尾话题模式下 enabled_modes 可能本就被清空（restricted_screen_only
            # + 无 vision），sources 必定为空但不应当 pass —— 让 Phase 2 拿对话
            # 历史 + state_section 跑 text-only [CHAT] 跟进。
            if not _has_unfinished_thread:
                return await _end_proactive(
                    ProactiveChatResult(
                        body=_proactive_pass_body(
                            PROACTIVE_REASON_ERROR_SOURCE_FETCH_FAILED,
                            success=False,
                            error="所有信息源获取失败",
                        ),
                        status_code=500,
                    )
                )
            print(
                f"[{lanlan_name}] sources 为空但有未收尾话题，进入 text-only 跟进路径"
            )

        # Phase 1 preempt check：信息源并行 fetch 完，正式进入 LLM 前先瞄一眼
        if mgr.state.is_proactive_preempted():
            return await _end_proactive(
                ProactiveChatResult(body=_proactive_preempted_json("phase1_post_fetch"))
            )

        print(
            f"[{lanlan_name}] 成功获取 {len(sources)} 个信息源: {list(sources.keys())}"
        )

        # ========== 1. 获取记忆上下文 (New Dialog) ==========
        # new_dialog 返回格式：
        # ========以下是{name}的内心活动========
        # {内心活动/Settings}...
        # 现在时间...整理了近期发生的事情。
        # Name | Content
        # ...

        raw_memory_context = ""
        try:
            # fmt="prompt"：proactive_lang 一路喂到 Phase 1/2 模板、屏幕/外部/
            # 音乐/表情包分节、近期搭话记录、action note——全是 prompt dict。
            proactive_lang = _resolve_proactive_locale(command, mgr, fmt="prompt")
        except Exception:
            proactive_lang = "zh"
        topic_hook_lang = _resolve_topic_hook_locale(
            command,
            mgr,
            fallback=proactive_lang,
        )
        try:
            from utils.internal_http_client import get_internal_http_client

            _pt_client = get_internal_http_client()
            resp = await _pt_client.get(
                f"http://127.0.0.1:{MEMORY_SERVER_PORT}/new_dialog/{lanlan_name}",
                params=_new_dialog_locale_params(command, mgr),
                timeout=5.0,
            )
            resp.raise_for_status()  # Check for HTTP errors explicitly
            if resp.status_code == 200:
                raw_memory_context = resp.text
            else:
                logger.warning(
                    f"[{lanlan_name}] 记忆服务返回非200状态: {resp.status_code}，使用空上下文"
                )
        except Exception as e:
            logger.warning(f"[{lanlan_name}] 获取记忆上下文失败，使用空上下文: {e}")

        # 解析 new_dialog 响应：把"内心活动"与"对话历史"切开。
        # 切分逻辑（locale 无关）集中在 prompts_memory.split_inner_thoughts_and_history，
        # 以 INNER_THOUGHTS_DYNAMIC 的多语言模板为准；任一 locale 都匹配不到时返回
        # None，这里兜底为"全部当历史、内心活动留空"并打 warning（不再静默错位）。
        def _parse_new_dialog(text: str) -> tuple[str, str]:
            if not text:
                return "", ""
            from config.prompts.prompts_memory import split_inner_thoughts_and_history

            split = split_inner_thoughts_and_history(text)
            if split is None:
                logger.warning(
                    "[%s] new_dialog 未匹配到内心活动分隔句（任一 locale），"
                    "整段归入对话历史，当前内心留空",
                    lanlan_name,
                )
                return text, ""
            inner_thoughts_part, history_part = split
            return history_part, inner_thoughts_part

        memory_context, inner_thoughts = _parse_new_dialog(raw_memory_context)

        # Phase 1 preempt check：memory_server new_dialog 是 phase1 里首次大 await
        # （httpx timeout 5s）。用户在这期间打断只能等超时才有下一次 check，
        # 这里补一刀。
        if mgr.state.is_proactive_preempted():
            return await _end_proactive(
                ProactiveChatResult(
                    body=_proactive_preempted_json("phase1_post_memory")
                )
            )

        # ========== 2. 选择语言 ==========
        # 与 mini-game 邀请短路同源：request body → mgr.user_language → 全局缓存。
        # 见 _resolve_proactive_locale 的 docstring。
        # ========== 3. 注入近期搭话记录 ==========
        proactive_chat_history_prompt = _format_recent_proactive_chats(
            lanlan_name, proactive_lang
        )

        # 趁机把 open_threads 计算起来——和下面 Phase 1 unified LLM 调用并行。
        # 缓存按用户消息序号失效；没新用户发言就 no-op 直接返回。Phase 2 读
        # snapshot 时会拿到这次的结果（如果赶上了）；赶不上就用上一次的缓存。
        try:
            mgr._activity_tracker.kickoff_open_threads_compute(lang=topic_hook_lang)
        except Exception as _ot_err:
            logger.debug(
                f"[{lanlan_name}] kickoff_open_threads_compute failed: {_ot_err}"
            )

        # ========== 3.5 反思 + 回调话题（通过 memory_server API） ==========
        # 认知框架：Facts → Reflection(pending) → 主动搭话自然提及 → 用户反馈 → Persona
        #
        # 用户在 gaming / focused_work 状态下不应自然回忆——会很尬。直接跳过整段
        # （也省 reflect POST 的 15s timeout 风险）。stale_returning 反而欢迎回忆。
        followup_topics_prompt = ""
        _followup_topics = []
        _surfaced_reflection_ids = []  # 记录本次搭话提及了哪些 pending 反思
        _allow_reminiscence = (
            activity_snapshot is None
            or activity_snapshot.propensity != "restricted_screen_only"
        )
        if not _allow_reminiscence:
            print(
                f"[{lanlan_name}] propensity=restricted_screen_only, 跳过反思/回忆话题获取"
            )
        # 复用 internal_http_client 单例：proactive_chat 每次主动搭话都走此路径。
        # 仅 read：取 followup_topics 候选用于本轮 prompt 注入。
        # 历史上这一段还前置调过 POST /reflect/{name}（"自动状态迁移 + 反思合成"），
        # 已删除——合成迁到 ``_periodic_reflection_synthesis_loop`` 后端循环、
        # auto_promote 早就由 ``_periodic_auto_promote_loop`` 每 180s 跑。把
        # mutation 留在 proactive 关键路径上既拖延 ~15s response、又让整个
        # reflection 生命周期跟前端 setTimeout 强耦合（前端不开 → 永不合成）。
        if _allow_reminiscence:
            try:
                from utils.internal_http_client import get_internal_http_client

                _mem_base = f"http://127.0.0.1:{MEMORY_SERVER_PORT}"
                _mem_client = get_internal_http_client()
                _topics_resp = await _mem_client.get(
                    f"{_mem_base}/followup_topics/{lanlan_name}",
                    timeout=5.0,
                )
                if _topics_resp.status_code == 200:
                    _followup_topics = _topics_resp.json().get("topics", [])
                    if _followup_topics:
                        try:
                            (
                                followup_topics_prompt,
                                _surfaced_reflection_ids,
                            ) = _render_followup_topic_hooks(
                                topic_hook_lang,
                                _followup_topics,
                            )
                        except Exception as _followup_prompt_err:
                            logger.debug(
                                f"[{lanlan_name}] followup topic prompt build failed: {_followup_prompt_err}"
                            )
                        print(
                            f"[{lanlan_name}] 回调话题候选: {len(_followup_topics)} 条"
                        )
            except Exception as e:
                logger.debug(f"[{lanlan_name}] 回调话题获取失败（不影响主流程）: {e}")

        # Phase 1 preempt check：followup GET(5s) 是一段可能拖久的 await，
        # 整段裸跑会让用户打断后继续跑完 LLM 配置和后续步骤，再到 pre-LLM
        # check 才识破。这里补一刀。
        if mgr.state.is_proactive_preempted():
            return await _end_proactive(
                ProactiveChatResult(
                    body=_proactive_preempted_json("phase1_post_reflect")
                )
            )

        # ========== 4. 获取 LLM 配置 ==========
        # 主动搭话全链路（Phase1 筛选 / Phase2 生成 / regen）用 conversation tier
        # 而非 correction tier：correction（纠错）模型在不开思考时较难稳定遵循
        # "第一行写来源标签" 的格式，容易把人设约束块当正文吐出来；conversation
        # 是主对话主力模型，格式遵循更稳。仍保持 disable_thinking（vision+思考必超时）。
        try:
            # 一份新鲜快照供 conversation / vision 共用：两次独立的读会让 /core_api
            # 的保存恰好落在中间时拿到撕裂的一对（旧 conversation + 新 vision）。
            _fresh_core_config = await _config_manager.aget_core_config()
            conversation_config = await _config_manager.aget_model_api_config(
                "conversation", core_config=_fresh_core_config
            )
            conversation_model = conversation_config.get("model")
            conversation_api_key = conversation_config.get("api_key")

            if not conversation_model or not conversation_api_key:
                logger.error("对话模型配置缺失: model或api_key未设置")
                return await _end_proactive(
                    ProactiveChatResult(
                        body={
                            "success": False,
                            "reason_code": PROACTIVE_REASON_ERROR_INTERNAL,
                            "error": "对话模型配置缺失",
                            "detail": "请在设置中配置对话模型的model和api_key",
                        },
                        status_code=500,
                    )
                )

            vision_config = await _config_manager.aget_model_api_config(
                "vision", core_config=_fresh_core_config
            )
            model_config = ProactiveModelConfig(
                conversation_model=conversation_model,
                conversation_base_url=conversation_config.get("base_url"),
                conversation_api_key=conversation_api_key,
                conversation_provider_type=conversation_config.get("provider_type"),
                vision_model=vision_config.get("model", ""),
                vision_base_url=vision_config.get("base_url", ""),
                vision_api_key=vision_config.get("api_key", ""),
                vision_provider_type=vision_config.get("provider_type"),
            )
            if not model_config.has_vision_model:
                logger.info("Vision 模型未配置，Phase 2 将退回使用对话模型")
        except Exception as e:
            logger.error(f"获取模型配置失败: {e}")
            return await _end_proactive(
                ProactiveChatResult(
                    body={
                        "success": False,
                        "reason_code": PROACTIVE_REASON_ERROR_INTERNAL,
                        "error": "模型配置异常",
                        "detail": str(e),
                    },
                    status_code=500,
                )
            )

        # ================================================================
        # Phase 1: 合并 LLM 调用（web 筛选 + music 关键词 + meme 关键词）
        # ⚠️ 一阶段一定不要分析屏幕！截图会在二阶段由 vision_model 直接 feed in。
        # - 所有文本源合并 → 1 次 LLM 同时完成 web 筛选、music/meme 关键词生成
        # - 来源动态权重系统在 LLM 调用前剔除低权重通道
        # 总计最多 1 次 LLM 调用
        # ================================================================

        vision_content = sources.get("vision")  # 仅保留给 Phase 2 使用，Phase 1 不处理
        music_content = sources.get("music")
        meme_content = sources.get("meme")
        logger.debug(
            f"[{lanlan_name}] 主动搭话-音乐内容: type={type(music_content)}, success={music_content.get('success') if music_content else 'N/A'}"
        )
        logger.debug(
            f"[{lanlan_name}] 主动搭话-表情包内容: type={type(meme_content)}, success={meme_content.get('success') if meme_content else 'N/A'}"
        )

        all_web_links: list[dict] = []

        # 收集音乐链接（在 Phase 1 Web 筛选完成后）
        # meme 也不经过 Phase 1 LLM 筛选，直接添加话题
        web_modes = [m for m in sources if m not in ("vision", "music", "meme")]
        # Personal-feed context wins when the same URL also appears elsewhere.
        web_modes.sort(key=lambda mode: 0 if mode == "personal" else 1)

        merged_web_content = ""

        # Phase 1 结果收集
        phase1_topics: list[tuple[str, str]] = []  # [(channel, topic_summary), ...]
        source_links: list[dict] = []  # [{"title": ..., "url": ..., "source": ...}]
        selected_web_link = None
        selected_web_topic_key = None
        selected_music_link = None
        selected_music_topic_key = None
        selected_meme_link = None
        selected_meme_topic_key = None

        # 【加固】如果正在放歌或处于冷却期，强制清空 music 通道，彻底跳过搜歌逻辑
        if is_music_occupied or is_playing_music or music_cooldown:
            if music_content:
                reason = (
                    "音乐播放器已占用"
                    if is_music_occupied
                    else "音乐正在播放"
                    if is_playing_music
                    else "用户连续秒关，音乐冷却中"
                )
                logger.debug(f"[{lanlan_name}]-{reason}，强制屏蔽 Phase 1 搜歌逻辑")
            music_content = None
            sources.pop("music", None)

        # ============================================================
        # 来源动态权重过滤（vision / 已屏蔽的 music 不参与权重计算）
        #
        # ``reminiscence`` 作为虚拟 channel：当本轮已经从 memory_server 取到
        # pending followup topics 时，把它放进权重计算池。和 web/news/music
        # 一样按使用频率衰减——AI 连续多次"回忆"会让 reminiscence 进入
        # suppressed 集合，本轮就跳过 followup_topics_prompt（per-reflection
        # cooldown 在 reflection.py 那侧另算，这里是 channel 级别的兜底）。
        # ============================================================
        source_weight_selection = _select_weighted_sources(
            lanlan_name,
            enabled_modes,
            sources,
            has_reminiscence=bool(_surfaced_reflection_ids),
        )
        if source_weight_selection.weights:
            source_weights = source_weight_selection.weights
            suppressed = source_weight_selection.suppressed
            weight_str = " ".join(f"{ch}={w:.3f}" for ch, w in source_weights.items())
            logger.debug(
                f"[{lanlan_name}] 来源权重: {weight_str} | 剔除: {suppressed or '无'}"
            )

            for ch in suppressed:
                sources.pop(ch, None)
            if "music" in suppressed:
                music_content = None
            if "meme" in suppressed:
                meme_content = None
            if "reminiscence" in suppressed:
                # 回忆 channel 被 throttle：只清空旧 reflection。
                # 后台深话题池走独立 one-shot 触发，不在 proactive prompt 里消费。
                if followup_topics_prompt:
                    print(
                        f"[{lanlan_name}] reminiscence channel suppressed by weight, dropping followup section"
                    )
                _followup_topics = []
                _surfaced_reflection_ids = []
                followup_topics_prompt = ""


        # Build once after source suppression so candidates are decayed only once.
        web_modes = [mode for mode in web_modes if mode in sources]
        if web_modes:
            from utils.tokenize import truncate_to_tokens as _ttt

            parts = []
            fallback_modes = _phase1_linkless_modes(web_modes, sources)
            selected_by_mode = _round_robin_phase1_links(
                web_modes,
                sources,
                total=max(
                    0, _PHASE1_TOTAL_TOPIC_TARGET - len(fallback_modes)
                ),
            )
            remaining_total = _PHASE1_TOTAL_TOPIC_TARGET - sum(
                len(items) for items in selected_by_mode.values()
            )
            remaining_fallback_modes = len(fallback_modes)
            phase1_source_positions: dict[str, int] = {}
            for mode in web_modes:
                src = sources[mode]
                label_map = PROACTIVE_SOURCE_LABELS.get(
                    proactive_lang, PROACTIVE_SOURCE_LABELS["en"]
                )
                label = label_map.get(mode, mode)
                selected_links = selected_by_mode.get(mode, [])

                if selected_links:
                    all_web_links.extend(selected_links)
                    lines = [
                        _ttt(
                            _format_phase1_link_candidate(index, item),
                            PROACTIVE_EXTERNAL_PER_ITEM_MAX_TOKENS,
                        )
                        for index, item in _number_phase1_links_by_source(
                            selected_links, source_positions=phase1_source_positions
                        )
                    ]
                    if lines:
                        parts.append(f"--- {label} ---\n" + "\n".join(lines))
                        continue

                # Community cards only enter Phase 1 through selected links:
                # a formatted fallback would bypass cooldown and data escaping.
                if mode == "community":
                    continue
                content_text = src.get("formatted_content", "")
                if content_text and remaining_total > 0:
                    compact_lines = [
                        line.strip()
                        for line in content_text.splitlines()
                        if line.strip()
                    ]
                    if compact_lines:
                        is_reserved_fallback = mode in fallback_modes
                        reserve_for_later = max(
                            0,
                            remaining_fallback_modes
                            - (1 if is_reserved_fallback else 0),
                        )
                        fallback_limit = remaining_total - reserve_for_later
                        if fallback_limit <= 0:
                            continue
                        fallback_lines = [
                            _ttt(line, PROACTIVE_EXTERNAL_PER_ITEM_MAX_TOKENS)
                            for line in compact_lines[:fallback_limit]
                        ]
                        parts.append(
                            f"--- {label} ---\n" + "\n".join(fallback_lines)
                        )
                        remaining_total -= len(fallback_lines)
                        if is_reserved_fallback:
                            remaining_fallback_modes -= 1

            # 兜底总和截断：防止 20 source × 200 token = 4k 超过 2k 总预算
            merged_web_content = _ttt(
                "\n\n".join(parts), PROACTIVE_EXTERNAL_TOTAL_MAX_TOKENS
            )

        # ============================================================
        # 合并 Phase 1 LLM 调用：web 筛选 + music 关键词 + meme 关键词
        # 一次 LLM 调用完成所有任务，降低 RPM
        # ============================================================
        has_music_task = bool(music_content and music_content.get("placeholder"))
        has_meme_task = bool(meme_content and meme_content.get("placeholder"))
        has_web_task = bool(merged_web_content)

        enriched_memory_context = memory_context
        if followup_topics_prompt:
            enriched_memory_context = memory_context + "\n" + followup_topics_prompt

        if has_web_task or has_music_task or has_meme_task:
            # Phase 1 preempt check：拨号前最后一次检查。大头 LLM 调用即将开始，
            # 此后等待期间用户抢占只能靠流结束后的兜底识别。
            if mgr.state.is_proactive_preempted():
                return await _end_proactive(
                    ProactiveChatResult(
                        body=_proactive_preempted_json("phase1_pre_llm")
                    )
                )
        unified_parsed = await _run_unified_phase1(
            model_config=model_config,
            proactive_lang=proactive_lang,
            lanlan_name=lanlan_name,
            master_name=master_name_current,
            merged_web_content=merged_web_content,
            memory_context=enriched_memory_context,
            recent_chats_section=proactive_chat_history_prompt,
            has_music_task=has_music_task,
            has_meme_task=has_meme_task,
            log=logger,
        )

        # ============================================================
        # 解析 web 结果 → 链接匹配 → 去重
        # ============================================================
        web_parsed = unified_parsed.get("web")
        if web_parsed and web_parsed.get("title"):
            matched = _lookup_link_by_phase1_selection(web_parsed, all_web_links)
            topic_key = _source_hash(
                (matched.get("dedupe_key") or matched.get("url", ""))
                if matched
                else "",
                web_parsed.get("title", ""),
            )
            # matched 的链接已经在 picking 阶段过了一次 _should_skip_source，
            # 这里再 roll 等于让等效 p_skip = 1-(1-p)^2，违背单次半衰期模型。
            # 仅对未匹配（LLM 幻觉的 title-only 候选）兜底再判一次。
            needs_recheck = bool(topic_key) and matched is None
            if needs_recheck and _should_skip_source(topic_key):
                print(
                    f"[{lanlan_name}] Phase 1 title-only 话题命中衰减，跳过: {web_parsed.get('title', '')[:60]}"
                )
            else:
                if matched:
                    selected_web_link = dict(matched)
                    canonical_title = matched.get("title", "")
                    selected_title = (
                        canonical_title
                        if matched.get("mode") == "community"
                        else web_parsed.get("title", canonical_title)
                    )
                    selected_web_link.update(
                        {
                            "title": selected_title,
                            "url": matched["url"],
                            "source": web_parsed.get(
                                "source", matched.get("source", "")
                            ),
                            "mode": matched.get("mode", "web"),
                        }
                    )
                    print(
                        f"[{lanlan_name}] Phase 1 链接预匹配成功: {matched.get('title', '')[:60]}"
                    )
                else:
                    print(
                        f"[{lanlan_name}] Phase 1 未在 web_links 中匹配到标题: {web_parsed.get('title', '')[:60]}"
                    )
                # 不论 matched 与否，都把 topic_key 留下来供 Phase 2 后落盘 ——
                # 哪怕只有 title 也参与衰减历史，避免同样的标题被反复 surface
                selected_web_topic_key = topic_key
                # 用 web_parsed 的 summary 或原始文本作为 topic
                web_topic_text = web_parsed.get("summary", web_parsed.get("title", ""))
                phase1_topics.append(("web", web_topic_text.strip()))

        # ============================================================
        # 并行后置 fetch：music + meme（使用 LLM 生成的关键词）
        # ============================================================
        if has_music_task and unified_parsed.get("music_pass"):
            print(f"[{lanlan_name}] Phase 1 音乐通道明确 PASS，跳过后置 fetch")
        if has_meme_task and unified_parsed.get("meme_pass"):
            print(f"[{lanlan_name}] Phase 1 表情包通道明确 PASS，跳过后置 fetch")
        needs_phase1_followups = (
            has_music_task and not unified_parsed.get("music_pass")
        ) or (has_meme_task and not unified_parsed.get("meme_pass"))
        if needs_phase1_followups:
            # Phase 1 preempt check：unified LLM 刚回，music/meme 后置 fetch 前再瞄
            if mgr.state.is_proactive_preempted():
                return await _end_proactive(
                    ProactiveChatResult(
                        body=_proactive_preempted_json("phase1_post_llm")
                    )
                )
        music_content, meme_content = await _fetch_phase1_followups(
            parsed=unified_parsed,
            has_music_task=has_music_task,
            has_meme_task=has_meme_task,
            music_content=music_content,
            meme_content=meme_content,
            proactive_lang=proactive_lang,
            lanlan_name=lanlan_name,
            log=logger,
        )

        # ============================================================
        # 音乐话题组装（遍历候选 → 衰减 skip → 暂存链接）
        # 与 web/meme 对偶：超取 N 条后逐条概率 skip，遇命中瞬移到下一条。
        # 全部命中则清空 music_content 让通道整体降级。
        # ============================================================
        music_selection = _select_music_recommendation(
            music_content,
            lang=proactive_lang,
            source_hash=_source_hash,
            should_skip_source=_should_skip_source,
            lanlan_name=lanlan_name,
        )
        music_content = music_selection.content
        if music_selection.link:
            music_topic = music_selection.topic
            selected_music_link = music_selection.link
            selected_music_topic_key = music_selection.topic_key
            logger.debug(
                f"[{lanlan_name}]- Phase 1 音乐话题已添加 "
                f"(topic_len={len(music_topic)})"
            )
            print(f"[{lanlan_name}]- Phase 1 音乐话题: {music_topic[:100]}")
            phase1_topics.append(("music", music_topic))

        # ============================================================
        # 表情包话题组装（遍历候选 → 去重 → 限1张）
        # ============================================================
        if meme_content and meme_content.get("success") and meme_content.get("data"):
            meme_data = meme_content.get("data", [])
            if meme_data:
                proxy_checked_count = 0
                for candidate_meme in meme_data:
                    meme_title = candidate_meme.get("title", "")
                    meme_url = candidate_meme.get("url", "")
                    if not meme_url:
                        continue  # 跳过无 URL 的候选
                    meme_source = candidate_meme.get("source", "表情包")
                    meme_topic_key = _source_hash(meme_url, meme_title)
                    if meme_topic_key and _should_skip_source(meme_topic_key):
                        logger.debug(
                            f"[{lanlan_name}]- Phase 1 表情包候选去重命中，跳过: {meme_title[:30]}"
                        )
                        continue
                    if mgr.state.is_proactive_preempted():
                        return await _end_proactive(
                            ProactiveChatResult(
                                body=_proactive_preempted_json(
                                    "phase1_pre_meme_moderation"
                                )
                            )
                        )
                    moderation = await moderate_meme_image_url(
                        meme_url, fail_closed=False
                    )
                    if mgr.state.is_proactive_preempted():
                        return await _end_proactive(
                            ProactiveChatResult(
                                body=_proactive_preempted_json(
                                    "phase1_post_meme_moderation"
                                )
                            )
                        )
                    if not moderation.allowed:
                        logger.info(
                            "[%s]- Phase 1 meme candidate moderation blocked: reason=%s cached=%s url_hash=%s title=%s",
                            lanlan_name,
                            moderation.reason,
                            moderation.cached,
                            moderation.url_hash,
                            meme_title[:30],
                        )
                        await _record_source_used(
                            url=meme_url,
                            kind="image",
                            title=meme_title,
                            **state_storage_kwargs,
                        )
                        logger.info(
                            "[%s]- 已记录被 moderation 拦截的表情包 source 衰减历史: url_hash=%s",
                            lanlan_name,
                            meme_topic_key[:16],
                        )
                        continue
                    if meme_proxy_candidate_fetchable is not None:
                        if (
                            proxy_checked_count
                            >= _MEME_PROXY_CANDIDATE_CHECK_LIMIT
                        ):
                            logger.info(
                                "[%s]- Phase 1 表情包代理预检达到上限(%d)，"
                                "跳过本轮 meme 通道",
                                lanlan_name,
                                _MEME_PROXY_CANDIDATE_CHECK_LIMIT,
                            )
                            break
                        if mgr.state.is_proactive_preempted():
                            return await _end_proactive(
                                ProactiveChatResult(
                                    body=_proactive_preempted_json(
                                        "phase1_pre_meme_proxy_check"
                                    )
                                )
                            )
                        proxy_checked_count += 1
                        proxy_ok, proxy_reason = (
                            await meme_proxy_candidate_fetchable(meme_url)
                        )
                        if mgr.state.is_proactive_preempted():
                            return await _end_proactive(
                                ProactiveChatResult(
                                    body=_proactive_preempted_json(
                                        "phase1_post_meme_proxy_check"
                                    )
                                )
                            )
                        if not proxy_ok:
                            logger.info(
                                "[%s]- Phase 1 表情包代理不可取，跳过候选: "
                                "reason=%s title=%s url=%s",
                                lanlan_name,
                                proxy_reason,
                                meme_title[:30],
                                meme_url[:100],
                            )
                            continue
                    single_meme_topic = get_meme_topic_line(
                        proactive_lang,
                        keyword=meme_content.get("keyword", ""),
                        title=meme_title,
                        source=meme_source,
                    )
                    logger.debug(
                        f"[{lanlan_name}]- Phase 1 表情包话题已添加 (限额1张): {single_meme_topic}"
                    )
                    phase1_topics.append(("meme", single_meme_topic))
                    selected_meme_link = {
                        "title": meme_title,
                        "url": meme_url,
                        "source": meme_source,
                        "type": candidate_meme.get("type", "meme"),
                    }
                    selected_meme_topic_key = meme_topic_key
                    logger.debug(f"[{lanlan_name}] 预选表情包话题: {meme_title[:30]}")
                    break
                else:
                    logger.debug(
                        f"[{lanlan_name}]- Phase 1 未选出可用表情包候选，跳过表情包话题"
                    )
            else:
                logger.warning(
                    f"[{lanlan_name}] Phase 1 表情包数据为空，跳过表情包话题"
                )

        phase1_decision = _decide_phase1_channels(
            phase1_topics,
            vision_content,
            has_unfinished_thread=_has_unfinished_thread,
        )
        if phase1_decision.result is not None:
            print(f"[{lanlan_name}] Phase 1 所有通道均无可用话题")
            return await _end_proactive(
                ProactiveChatResult(body=phase1_decision.result.body)
            )
        if not phase1_topics and not vision_content:
            print(
                f"[{lanlan_name}] Phase 1 无话题但有未收尾话题，进入 text-only 跟进 Phase 2"
            )

        # Phase 1 preempt check：topic assembly 完，进入 Phase 2 前最后一次瞄
        if mgr.state.is_proactive_preempted():
            return await _end_proactive(
                ProactiveChatResult(body=_proactive_preempted_json("phase1_pre_phase2"))
            )

        # 收集各通道结果
        active_channels = phase1_decision.active_channels
        print(
            f"[{lanlan_name}] Phase 1 结果: phase1_topics={phase1_topics}, vision_content={'有' if vision_content else '无'}"
        )
        web_topic = phase1_decision.web_topic
        music_topic = phase1_decision.music_topic
        primary_channel = phase1_decision.primary_channel
        if selected_web_link:
            if mgr.state.is_proactive_preempted():
                return await _end_proactive(
                    ProactiveChatResult(
                        body=_proactive_preempted_json(
                            "phase1_pre_candidate_preparation"
                        )
                    )
                )
            try:
                selected_web_link, web_topic = await prepare_selected_web_candidate(
                    selected_web_link,
                    fallback_topic=web_topic,
                    language=proactive_lang,
                    is_preempted=mgr.state.is_proactive_preempted,
                )
            except SelectedWebCandidatePreempted:
                return await _end_proactive(
                    ProactiveChatResult(
                        body=_proactive_preempted_json(
                            "phase1_candidate_preparation"
                        )
                    )
                )
            if mgr.state.is_proactive_preempted():
                return await _end_proactive(
                    ProactiveChatResult(
                        body=_proactive_preempted_json(
                            "phase1_post_candidate_preparation"
                        )
                    )
                )
        print(
            f"[{lanlan_name}] Phase 1 可用通道: {active_channels}，主通道: {primary_channel}"
        )

        # ================================================================
        # Phase 2: 结合人设 + 双通道信息 → 流式生成搭话
        # ⚠️ 二阶段一定要用 vision_model，在调用前使用最新截图。
        #    只有这样才能减少 vision_model 读屏幕的延迟。
        # ⚠️ 二阶段一定不要打开思考 (disable_thinking 必须为 True)，
        #    否则 vision_model + thinking 一定会超时。
        # ⚠️ 不重试、不改写。流式拦截到异常直接 abort，失败即 pass 等下一次。
        # 流程：tokens → TTS 即时生成 → 全文完成后一次性投递文本 → abort 时中断两端
        # ================================================================

        # 获取角色完整人设，替换模板变量
        character_prompt = lanlan_prompt_map.get(lanlan_name, "")
        if not character_prompt:
            logger.warning(f"[{lanlan_name}] 未找到角色人设，使用空字符串")
        character_prompt = character_prompt.replace(
            "{LANLAN_NAME}", lanlan_name
        ).replace("{MASTER_NAME}", master_name_current)

        # --- 向前端请求最新截图，替换 Phase 1 时拿到的旧截图 ---
        screenshot_b64_for_phase2 = ""
        if vision_content and model_config.has_vision_model:
            fresh = await mgr.request_fresh_screenshot(timeout=3.0)
            if fresh.b64:
                fresh_b64 = fresh.b64
                # 判据见 _resolve_phase2_avatar_position：WS 路径不给坐标 = 前端明确
                # 说「别叠」，绝不回退请求里的旧坐标；pyautogui 兜底才用 Phase 1 坐标。
                # ⚠️ request_fresh_screenshot 两条路径都不叠加，overlay 只在这里做。
                # ⚠️ 已知上限：Phase1→Phase2 之间隔了一整轮 LLM（数秒），兜底路径复用
                #    的坐标可能已经陈旧。
                av_pos = _resolve_phase2_avatar_position(fresh, avatar_position)
                if av_pos and isinstance(av_pos, dict):
                    try:
                        from utils.screenshot_utils import overlay_avatar_annotation

                        fresh_b64 = await asyncio.to_thread(
                            overlay_avatar_annotation,
                            fresh_b64,
                            av_pos,
                            lanlan_name,
                            proactive_lang,
                        )
                    except Exception as ann_err:
                        logger.warning(
                            f"[{lanlan_name}] Phase 2 avatar annotation failed: {ann_err}"
                        )
                screenshot_b64_for_phase2 = fresh_b64
                print(
                    f"[{lanlan_name}] Phase 2 获取到最新截图 ({len(fresh_b64) // 1024}KB)"
                )
            else:
                screenshot_b64_for_phase2 = vision_content.get("screenshot_b64", "")
                if screenshot_b64_for_phase2:
                    print(
                        f"[{lanlan_name}] Phase 2 刷新截图失败，退回使用 Phase 1 旧截图"
                    )

        # 构建屏幕内容段（vision 通道）
        screen_section = ""
        if screenshot_b64_for_phase2:
            sl = get_screen_section_header(master_name_current, proactive_lang)
            sf = get_screen_section_footer(master_name_current, proactive_lang)
            vision_window = (
                vision_content.get("window_title", "") if vision_content else ""
            )
            window_line = (
                _loc(SCREEN_WINDOW_TITLE, proactive_lang).format(window=vision_window)
                if vision_window
                else ""
            )
            hint = get_screen_img_hint(master_name_current, proactive_lang)
            screen_section = f"{sl}\n{window_line}{hint}\n{sf}"
            print(f"[{lanlan_name}] Phase 2 将使用 vision 模型直接看截图")
        else:
            print(f"[{lanlan_name}] Phase 2 无截图或无 vision 模型，跳过屏幕分析")

        # 构建网络话题段（web 通道）
        external_section = ""
        if web_topic:
            el = _loc(EXTERNAL_TOPIC_HEADER, proactive_lang)
            ef = _loc(EXTERNAL_TOPIC_FOOTER, proactive_lang)
            external_section = f"{el}\n{web_topic}\n{ef}"

        music_section = ""
        # gate 钉在 selected_music_link（本轮真选中、可播的曲目）而非 music_topic：
        # 保证 Phase 2 prompt 一旦出现音乐素材 / output-format 列出 [MUSIC]，下游必有
        # 歌可投递，不会"发了 [MUSIC] 却转译不出"。selected_music_link 非空时
        # music_topic 必非空（同生于 Phase 1 选曲）。正在放歌 / 冷却期时
        # music_content / selected_music_link 已在上游清空，此分支自然不命中。
        if (
            selected_music_link
            and not is_music_occupied
            and not is_playing_music
            and not music_cooldown
        ):
            # 【优化】使用独立的标识符，防止模型将音乐素材误认为普通的外部 WEB 话题
            msh = _loc(MUSIC_SECTION_HEADER, proactive_lang)
            msf = _loc(MUSIC_SECTION_FOOTER, proactive_lang)
            music_section = f"{msh}\n{music_topic}\n{msf}"
        elif is_playing_music:
            print(
                f"[{lanlan_name}] 正在播放音乐，已屏蔽音乐推荐素材（仅保留 playing_hint）"
            )
            music_section = ""

        # 构建表情包段（meme 通道）
        meme_section = ""
        meme_topic = None
        for channel, topic in phase1_topics:
            if channel == "meme":
                meme_topic = topic
                break
        if meme_topic:
            meh = _loc(MEME_SECTION_HEADER, proactive_lang)
            mef = _loc(MEME_SECTION_FOOTER, proactive_lang)
            meme_section = f"{meh}\n{meme_topic}\n{mef}"

        source_instruction, output_format_section = get_proactive_format_sections(
            has_screen=bool(screen_section),
            has_web=bool(external_section),
            has_music=bool(music_section),
            has_meme=bool(meme_section),
            lang=proactive_lang,
        )
        # 本轮是否启用"来源标签系统"：有 web/music/meme 副作用通道时，
        # get_proactive_format_sections 用 _of_header（要求第一行写 [TAG]）；三者全无
        # 时用 _of_none（明确要求纯文本、无 tag，下游靠 source_tag='CHAT' 兜底投递）。
        # 无 tag gate 只在前者生效，否则会把 _of_none 模式的合法纯文本搭话误判为
        # 格式泄漏 drop（Codex P1）。
        _expects_source_tag = (
            bool(external_section) or bool(music_section) or bool(meme_section)
        )
        music_playing_hint = _build_music_playing_hint(
            is_playing_music=is_playing_music,
            current_track=current_track,
            master_name=master_name_current,
            lang=proactive_lang,
        )

        # 把活动快照渲染成 prompt 段。snapshot 缺失时退化为空串——decision frame
        # 里的 A) 看「用户当前状态」分支会自动走到"其它状态：所有切入点都可用"。
        #
        # 重要：渲染前重拉一次 tracker enrichment 缓存（activity_scores /
        # activity_guess / open_threads）。kickoff_open_threads_compute 是在
        # Phase 1 起点 fire-and-forget 跑的，结果会在 Phase 1 进行中陆续落到
        # 缓存里——早期捕获的 activity_snapshot 看不到这些更新。专门并行起来
        # 就是为了本轮就用。决策性字段（state / propensity / propensity_reasons /
        # unfinished_thread）仍取自早期 snapshot，避免 Phase 1 中途 state 变化
        # 导致 gating 决策（restricted_screen_only 收紧 enabled_modes 等）和最终
        # prompt 不一致。
        # Freshest enrichment for the proactive prompt — Phase 1 (source fetch +
        # memory + LLM) just elapsed, so activity scores / open threads moved on.
        # Falls back to the entry snapshot if the refresh fails / is unavailable.
        # (The idle Focus decision no longer consumes a snapshot — it is a pure
        # charge cooldown — so this block only feeds the prompt now.)
        if activity_snapshot is not None:
            from dataclasses import replace as _dc_replace

            from main_logic.activity import format_activity_state_section

            try:
                fresh_enrich = await mgr._activity_tracker.get_snapshot()
                # restricted_screen_only deliberately strips semantic open_threads
                # so gaming / focused-work prompts stay screen-only — render the
                # prompt with that filtered set.
                _filtered_open_threads = _open_threads_for_activity_state(
                    activity_snapshot,
                    fresh_enrich.open_threads,
                )
                display_snap = _dc_replace(
                    activity_snapshot,
                    activity_scores=fresh_enrich.activity_scores,
                    activity_guess=fresh_enrich.activity_guess,
                    open_threads=_filtered_open_threads,
                )
            except Exception as _enrich_err:
                logger.debug(
                    f"[{lanlan_name}] fresh enrichment fetch failed: {_enrich_err}"
                )
                display_snap = activity_snapshot
            state_section = format_activity_state_section(display_snap, proactive_lang)
        else:
            display_snap = None
            state_section = ""

        # 静动分离：generate_prompt 作为静态 SystemMessage（可被缓存），
        # 追加的音乐/表情包指令作为动态上下文注入 HumanMessage
        # 使用 enriched_memory_context（含回忆线索）而非原始 memory_context。
        # open_threads 保持在上方 activity state section，不混进 memory_context。
        phase2_memory_context = memory_context
        if followup_topics_prompt:
            phase2_memory_context = memory_context + "\n" + followup_topics_prompt

        # ── 用户显式 ban-topic 注入 ─────────────────────────────────
        # Phase 2 的 system prompt 自己拼，从不经过 _build_initial_prompt，
        # 所以在此之前用户说过的"别再提 X"对**主动搭话**完全不可见：常规回复
        # 受禁令约束，主动搭话照提不误——而主动搭话恰恰是"哪壶不开提哪壶"
        # 最伤人的那条路径（用户没问，是 AI 自己挑起的）。
        # 这里只做软约束（prompt 提醒）；出口还有一道 drop 硬闸，见
        # generation.py 的 _proactive_directive_hits。两级都不额外烧 LLM。
        phase2_memory_context = _append_directives_section(
            phase2_memory_context, lanlan_name, proactive_lang,
        )

        phase2_prompt_context = Phase2PromptContext(
            music_playing_hint=music_playing_hint,
            character_prompt=character_prompt,
            inner_thoughts=inner_thoughts,
            state_section=state_section,
            memory_context=phase2_memory_context,
            recent_chats_section=proactive_chat_history_prompt,
            screen_section=screen_section,
            external_section=external_section,
            music_section=music_section,
            meme_section=meme_section,
            source_instruction=source_instruction,
            output_format_section=output_format_section,
        )
        dynamic_context_for_phase2 = _build_music_dynamic_context(
            selected_music_link=selected_music_link,
            music_content=music_content,
            is_playing_music=is_playing_music,
            master_name=master_name_current,
            lang=proactive_lang,
        )
        phase2_system_prompt = phase2_prompt_context.render(
            proactive_lang=proactive_lang,
            master_name=master_name_current,
        )
        # music_cooldown 时不再注入 strict_constraint —— 此时 music 通道已被前端/后端
        # 完全剔除，不应向模型暴露任何音乐相关指令，以免干扰其他 source 的选择。
        print(
            f"[{lanlan_name}] Phase 2 prompt 长度: {len(phase2_system_prompt)}, "
            f"动态上下文: {len(dynamic_context_for_phase2)} 字符"
        )
        # Phase 1 preempt check (final)：request_fresh_screenshot 最多 await 3s，
        # 是 prepare_proactive_delivery 之前唯一剩下的可打断窗口。若此处用户已
        # 接管，继续走 prepare 会让其内部的 `current_speech_id = uuid4()` 覆盖
        # 用户轮次的 sid —— 即使 SM 的 PROACTIVE_CLAIM 在 _preempted=True 时不
        # 回写 proactive_sid，mgr.current_speech_id 已经被物理换掉，用户的
        # 回复 TTS 会被错贴上一个陌生 sid。
        if mgr.state.is_proactive_preempted():
            return await _end_proactive(
                ProactiveChatResult(
                    body=_proactive_preempted_json("phase1_pre_prepare")
                )
            )

        # --- 前置检查：用户是否空闲、WebSocket 是否在线、session 是否可用 ---
        if not await mgr.prepare_proactive_delivery(min_idle_secs=10.0):
            return await _end_proactive(
                ProactiveChatResult(
                    body={
                        "success": True,
                        "action": "pass",
                        "reason_code": PROACTIVE_REASON_PASS_ACTIVITY_BUSY,
                        "message": "主动搭话条件未满足（用户近期活跃或语音会话正在进行）",
                    }
                )
            )

        # 记录本轮主动搭话起始的 speech_id；abort 时若该 id 已变，说明用户已打断并接管，
        # 此时再调 handle_new_message() 会把用户正常回复的 TTS 也一起清掉。
        # prepare_proactive_delivery 已经 fire(PROACTIVE_CLAIM, sid=...)；这里把
        # 状态机翻到 PHASE2，后续 astream 循环的抢占检查基于此阶段。
        proactive_sid = mgr.current_speech_id
        if not await _enter_proactive_phase2(
            mgr,
            proactive_sid,
            log=logger,
        ):
            return await _end_proactive(
                ProactiveChatResult(
                    body=_proactive_pass_body(
                        PROACTIVE_REASON_DELIVERY_PREEMPTED,
                        message="用户在 Phase 2 状态切换期间恢复互动",
                    )
                )
            )

        # Path B (idle) Focus 凝神：this round is now committed to speaking
        # (PHASE2 fired). Read-only: does this proactive reply run thinking-on?
        # (= the session is already in Focus, inline-driven). A proactive turn
        # never raises the charge; the charge cooldown happens after the turn in
        # _end_proactive (it needs to know whether we actually spoke). Dominates
        # all three Phase-2 generate sites below (main stream / format-fix regen
        # / BM25 anti-repeat regen).
        _focus_phase2_thinking = mgr._focus_idle_thinking()
        # Mark that this turn reached the Phase-2 idle Focus decision and pin the
        # focus state it observed (episode id + turn count) — _end_proactive
        # applies the cooldown only for such turns, and only if still in this
        # exact episode/turn (race guard: a no-op if inline moved it since).
        lifecycle.mark_phase2(mgr.state.snapshot())

        guarded_output = await _run_phase2_generation(
            mgr=mgr,
            proactive_sid=proactive_sid,
            model_config=model_config,
            lanlan_name=lanlan_name,
            proactive_lang=proactive_lang,
            master_name=master_name_current,
            system_prompt=phase2_system_prompt,
            dynamic_context=dynamic_context_for_phase2,
            screenshot_b64=screenshot_b64_for_phase2,
            focus_thinking=_focus_phase2_thinking,
            expects_source_tag=_expects_source_tag,
            active_channels=active_channels,
            selected_music_link=selected_music_link,
            selected_meme_link=selected_meme_link,
            music_content=music_content,
            meme_content=meme_content,
            is_playing_music=is_playing_music,
            music_cooldown=music_cooldown,
            log=logger,
        )
        if guarded_output.result is not None:
            return await _end_proactive(
                ProactiveChatResult(body=guarded_output.result.body)
            )
        response_text = guarded_output.response_text
        source_tag = guarded_output.source_tag
        selected_music_link = guarded_output.selected_music_link
        music_content = guarded_output.music_content
        is_music_used = guarded_output.is_music_used
        phase2_use_vision = bool(
            screenshot_b64_for_phase2 and model_config.has_vision_model
        )
        delivery_commit = await _commit_proactive_delivery(
            mgr=mgr,
            proactive_sid=proactive_sid,
            lanlan_name=lanlan_name,
            response_text=response_text,
            source_tag=source_tag,
            active_channels=active_channels,
            selected_web_link=selected_web_link,
            selected_music_link=selected_music_link,
            selected_meme_link=selected_meme_link,
            music_content=music_content,
            is_music_used=is_music_used,
            is_playing_music=is_playing_music,
            music_cooldown=music_cooldown,
            vision_content=vision_content,
            phase2_use_vision=phase2_use_vision,
            screenshot_b64=screenshot_b64_for_phase2,
            proactive_lang=proactive_lang,
            master_name=master_name_current,
            log=logger,
        )
        if delivery_commit.result is not None:
            return await _end_proactive(
                ProactiveChatResult(body=delivery_commit.result.body)
            )
        committed_delivery = delivery_commit.delivery
        if committed_delivery is None:  # Defensive: the stage contract is exhaustive.
            raise RuntimeError("delivery commit returned neither result nor delivery")
        if (
            committed_delivery.is_music_used
            and committed_delivery.delivered_music_link
        ):
            delivered_music_link = committed_delivery.delivered_music_link
            mark_music_as_played(
                {
                    "name": delivered_music_link.get("title", ""),
                    "artist": delivered_music_link.get("artist", ""),
                    "url": delivered_music_link.get("url", ""),
                }
            )
        recorded_result = await _record_committed_delivery(
            mgr=mgr,
            delivery=committed_delivery,
            lanlan_name=lanlan_name,
            response_text=response_text,
            source_tag=source_tag,
            active_channels=active_channels,
            **state_storage_kwargs,
            has_unfinished_thread=_has_unfinished_thread,
            surfaced_reflection_ids=_surfaced_reflection_ids,
            selected_web_link=selected_web_link,
            selected_web_topic_key=selected_web_topic_key,
            web_parsed=web_parsed,
            selected_music_link=selected_music_link,
            selected_music_topic_key=selected_music_topic_key,
            selected_meme_link=selected_meme_link,
            selected_meme_topic_key=selected_meme_topic_key,
            meme_content=meme_content,
            memory_server_port=MEMORY_SERVER_PORT,
            log=logger,
        )
        return await _end_proactive(
            ProactiveChatResult(
                body=recorded_result.body,
                status_code=recorded_result.status_code,
            )
        )

    except asyncio.TimeoutError:
        logger.error("主动搭话超时")
        if lifecycle is not None:
            await lifecycle.safe_done()
        return ProactiveChatResult(
            body=_proactive_error_body(
                PROACTIVE_REASON_ERROR_TIMEOUT,
                error="AI处理超时",
            ),
            status_code=504,
        )
    except Exception as e:
        logger.error(f"主动搭话接口异常: {e}")
        if lifecycle is not None:
            await lifecycle.safe_done()
        return ProactiveChatResult(
            body=_proactive_error_body(
                PROACTIVE_REASON_ERROR_INTERNAL,
                error="服务器内部错误",
                detail=str(e),
            ),
            status_code=500,
        )
