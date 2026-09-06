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

"""Delivery, post-commit recording, and lifecycle stages for proactive chat."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import MEMORY_SERVER_PORT
from config.prompts.prompts_proactive import build_proactive_action_note
from utils.logger_config import get_module_logger

from .contracts import (
    PROACTIVE_REASON_CHAT_DELIVERED,
    PROACTIVE_REASON_DELIVERY_FAILED,
    PROACTIVE_REASON_DELIVERY_PREEMPTED,
    ProactiveChatResult,
    _ensure_proactive_reason_code,
    _proactive_chat_body,
    _proactive_pass_body,
)
from .decisions import build_proactive_response
from .mini_game_invite import _mini_game_invite_count_post_response_chat
from .music_recommendation import _append_music_recommendations
from .state import (
    _increment_proactive_chat_total,
    _proactive_feed_rejected_for_takeover,
    _proactive_material_key,
    _proactive_turn_still_owned,
    _record_proactive_chat,
    _record_proactive_material,
    _record_reminiscence_usage,
    _record_source_used,
)

logger = get_module_logger(__name__, "Main")


@dataclass(slots=True)
class ProactiveLifecycle:
    """Explicit owner of normal and exceptional proactive-turn finalization."""

    mgr: Any
    state_event: Any
    lanlan_name: str
    log: logging.Logger | None = None
    done_emitted: bool = False
    next_schedule_fixed_mode: bool = False
    focus_phase2_reached: bool = False
    focus_episode_token: Any = None
    focus_turn_token: Any = None

    @property
    def _logger(self) -> logging.Logger:
        return self.log or logger

    def set_fixed_schedule(self, enabled: bool) -> None:
        self.next_schedule_fixed_mode = enabled

    def mark_phase2(self, snapshot: dict[str, Any]) -> None:
        self.focus_phase2_reached = True
        self.focus_episode_token = snapshot.get("focus_episode_id")
        self.focus_turn_token = snapshot.get("focus_turn_count")

    async def safe_done(self) -> None:
        """Fire DONE at most once without applying normal-exit side effects."""
        if self.done_emitted:
            return
        self.done_emitted = True
        try:
            await self.mgr.state.fire(self.state_event)
        except Exception as exc:
            self._logger.warning(
                "[%s] PROACTIVE_DONE fire 异常: %s",
                self.lanlan_name,
                exc,
            )

    async def finalize(self, result: ProactiveChatResult) -> ProactiveChatResult:
        """Apply the legacy normal-exit DONE, Focus, and schedule semantics."""
        await self.safe_done()
        body = _ensure_proactive_reason_code(dict(result.body))
        replied = body.get("action") == "chat"
        if not replied:
            self._logger.info(
                "[%s] 主动搭话本轮未发起：%s",
                self.lanlan_name,
                body.get("message") or body.get("error") or "(无原因说明)",
            )
        if self.focus_phase2_reached:
            try:
                await self.mgr._focus_idle_cooldown(
                    replied=replied,
                    episode_token=self.focus_episode_token,
                    turn_token=self.focus_turn_token,
                )
            except Exception as exc:
                self._logger.debug(
                    "[%s] focus idle cooldown failed: %s",
                    self.lanlan_name,
                    exc,
                )
        body.setdefault(
            "next_schedule_fixed_mode",
            self.next_schedule_fixed_mode,
        )
        return ProactiveChatResult(body=body, status_code=result.status_code)


@dataclass(frozen=True, slots=True)
class CommittedDelivery:
    """Delivery facts that may be recorded only after a successful commit."""

    primary_channel: str
    source_links: list[dict[str, Any]]
    delivered_tag: str
    delivered_music_link: dict[str, Any] | None
    is_music_used: bool
    action_note: str
    vision_screenshot_b64: str | None


@dataclass(frozen=True, slots=True)
class DeliveryCommit:
    """Either a terminal pass result or successfully committed delivery facts."""

    result: ProactiveChatResult | None
    delivery: CommittedDelivery | None


def _get_internal_http_client():
    """Keep the memory-server client import lazy, as in the legacy handler."""
    from utils.internal_http_client import get_internal_http_client

    return get_internal_http_client()


def _is_link_selected(
    selected_link: dict[str, Any] | None,
    source_links: list[dict[str, Any]],
) -> bool:
    """Match a selected candidate against links that were actually delivered."""
    if not selected_link:
        return False
    target_url = (selected_link.get("url") or "").strip()
    if target_url:
        return any(
            (link.get("url") or "").strip() == target_url
            for link in source_links
            if link
        )
    target_signature = (
        (selected_link.get("title") or "").strip(),
        (selected_link.get("artist") or "").strip(),
        (selected_link.get("source") or "").strip(),
    )
    return any(
        (
            (link.get("title") or "").strip(),
            (link.get("artist") or "").strip(),
            (link.get("source") or "").strip(),
        )
        == target_signature
        for link in source_links
        if link
    )


async def _commit_proactive_delivery(
    *,
    mgr: Any,
    proactive_sid: Any,
    lanlan_name: str,
    response_text: str,
    source_tag: str,
    active_channels: list[str],
    selected_web_link: dict[str, Any] | None,
    selected_music_link: dict[str, Any] | None,
    selected_meme_link: dict[str, Any] | None,
    music_content: dict[str, Any] | None,
    is_music_used: bool,
    is_playing_music: bool,
    music_cooldown: bool,
    vision_content: dict[str, Any] | None,
    phase2_use_vision: bool,
    screenshot_b64: str | None,
    proactive_lang: str,
    master_name: str,
    log: logging.Logger | None = None,
) -> DeliveryCommit:
    """Build, feed, and atomically finish one proactive delivery."""
    active_logger = log or logger
    has_music_topic = "music" in active_channels
    primary_channel, source_links = build_proactive_response(
        source_tag,
        {
            "lanlan_name": lanlan_name,
            "is_music_used": is_music_used,
            "selected_web_link": selected_web_link,
            "selected_music_link": selected_music_link,
            "selected_meme_link": selected_meme_link,
            "vision_content": vision_content,
        },
    )

    should_try_music_fallback = (
        not is_playing_music
        and not music_cooldown
        and (
            primary_channel == "music"
            or (
                has_music_topic
                and not any(
                    channel in ("vision", "web", "meme") for channel in active_channels
                )
            )
        )
    )
    if should_try_music_fallback:
        if source_links is None:
            source_links = []
        if _append_music_recommendations(source_links, music_content) > 0:
            is_music_used = True

    if is_music_used:
        music_already_appended = any(
            link.get("source") == "音乐推荐" for link in source_links
        )
        if not music_already_appended:
            _append_music_recommendations(source_links, music_content)

    delivered_music_link = selected_music_link or next(
        (
            link
            for link in (source_links or [])
            if isinstance(link, dict) and link.get("source") == "音乐推荐"
        ),
        None,
    )
    if (is_music_used or primary_channel == "music") and delivered_music_link:
        delivered_tag = "MUSIC"
    elif primary_channel == "meme" and selected_meme_link is not None:
        delivered_tag = "MEME"
    else:
        delivered_tag = "CHAT"
        is_music_used = False

    action_note = build_proactive_action_note(
        primary_channel=primary_channel,
        source_links=source_links,
        language=proactive_lang,
        master_name=master_name,
    )
    staged_screenshot = screenshot_b64 if phase2_use_vision else None
    expected_user_engagement_time = getattr(
        mgr,
        "last_user_engagement_time",
        None,
    )
    try:
        tts_accepted = await mgr.feed_tts_chunk(
            response_text,
            expected_speech_id=proactive_sid,
            expected_user_engagement_time=expected_user_engagement_time,
        )
        if tts_accepted is False and _proactive_feed_rejected_for_takeover(
            mgr,
            proactive_sid,
            expected_user_engagement_time,
        ):
            active_logger.info(
                "[%s] buffered proactive TTS dropped after user interaction or takeover",
                lanlan_name,
            )
            if _proactive_turn_still_owned(mgr, proactive_sid):
                await mgr.handle_new_message()
            return DeliveryCommit(
                result=ProactiveChatResult(
                    body=_proactive_pass_body(
                        PROACTIVE_REASON_DELIVERY_PREEMPTED,
                        message="proactive TTS skipped: user became active",
                        lanlan_name=lanlan_name,
                        turn_id=mgr.current_speech_id,
                    )
                ),
                delivery=None,
            )
        if tts_accepted is False:
            active_logger.warning(
                "[%s] proactive TTS enqueue failed; committing text without audio",
                lanlan_name,
            )
        committed = await mgr.finish_proactive_delivery(
            response_text,
            expected_speech_id=proactive_sid,
            expected_user_engagement_time=expected_user_engagement_time,
            action_note=action_note,
            source_tag=delivered_tag,
            vision_screenshot_b64=staged_screenshot,
        )
    except Exception as exc:
        active_logger.warning(
            "[%s] buffered proactive delivery failed: %s",
            lanlan_name,
            exc,
        )
        if _proactive_turn_still_owned(mgr, proactive_sid):
            await mgr.handle_new_message()
        else:
            active_logger.info(
                "[%s] buffered delivery failed after user takeover; skip TTS cleanup",
                lanlan_name,
            )
        return DeliveryCommit(
            result=ProactiveChatResult(
                body=_proactive_pass_body(
                    PROACTIVE_REASON_DELIVERY_FAILED,
                    message="Phase 2 buffered delivery failed",
                )
            ),
            delivery=None,
        )

    if not committed:
        active_logger.info(
            "[%s] 主动搭话被用户接管，短路下游写入（topic/memory/response）",
            lanlan_name,
        )
        if _proactive_turn_still_owned(mgr, proactive_sid):
            # The TTS chunk was accepted before the final engagement check.
            # Retract it when UI-only engagement invalidates the commit.
            await mgr.handle_new_message()
        return DeliveryCommit(
            result=ProactiveChatResult(
                body=_proactive_pass_body(
                    PROACTIVE_REASON_DELIVERY_PREEMPTED,
                    message="proactive delivery skipped: user took over turn",
                    lanlan_name=lanlan_name,
                    turn_id=mgr.current_speech_id,
                )
            ),
            delivery=None,
        )

    return DeliveryCommit(
        result=None,
        delivery=CommittedDelivery(
            primary_channel=primary_channel,
            source_links=source_links,
            delivered_tag=delivered_tag,
            delivered_music_link=delivered_music_link,
            is_music_used=is_music_used,
            action_note=action_note,
            vision_screenshot_b64=staged_screenshot,
        ),
    )


async def _record_committed_delivery(
    *,
    mgr: Any,
    delivery: CommittedDelivery,
    lanlan_name: str,
    response_text: str,
    source_tag: str,
    active_channels: list[str],
    has_unfinished_thread: bool,
    surfaced_reflection_ids: list[Any],
    selected_web_link: dict[str, Any] | None,
    selected_web_topic_key: str | None,
    web_parsed: dict[str, Any] | None,
    selected_music_link: dict[str, Any] | None,
    selected_music_topic_key: str | None,
    selected_meme_link: dict[str, Any] | None,
    selected_meme_topic_key: str | None,
    meme_content: dict[str, Any] | None,
    memory_server_port: int = MEMORY_SERVER_PORT,
    memory_dir: str | Path | None = None,
    log: logging.Logger | None = None,
) -> ProactiveChatResult:
    """Record post-commit history and return the successful chat contract."""
    active_logger = log or logger
    primary_channel = delivery.primary_channel
    source_links = delivery.source_links
    effective_source_mode = primary_channel.lower()
    if (
        effective_source_mode not in {"music", "both"}
        and delivery.is_music_used
        and delivery.delivered_music_link
    ):
        effective_source_mode = "music"
    state_storage_kwargs = (
        {"memory_dir": memory_dir} if memory_dir is not None else {}
    )

    _record_proactive_chat(lanlan_name, response_text, primary_channel)
    _record_proactive_material(
        lanlan_name,
        delivery.delivered_tag,
        _proactive_material_key(
            delivery.delivered_tag,
            delivery.delivered_music_link,
            meme_content,
        ),
    )
    _mini_game_invite_count_post_response_chat(lanlan_name)
    await _increment_proactive_chat_total(lanlan_name, **state_storage_kwargs)
    if surfaced_reflection_ids:
        _record_reminiscence_usage(lanlan_name)

    if has_unfinished_thread and (source_tag == "CHAT" or primary_channel == "chat"):
        try:
            mgr._activity_tracker.mark_unfinished_thread_used()
            print(f"[{lanlan_name}] 跟进未收尾话题：mark_used")
        except Exception as exc:
            active_logger.warning(
                "[%s] mark_unfinished_thread_used failed: %s",
                lanlan_name,
                exc,
            )

    try:
        memory_base = f"http://127.0.0.1:{memory_server_port}"
        memory_client = _get_internal_http_client()
        if surfaced_reflection_ids:
            await memory_client.post(
                f"{memory_base}/record_surfaced/{lanlan_name}",
                json={"reflection_ids": surfaced_reflection_ids},
                timeout=5.0,
            )
            print(
                f"[{lanlan_name}] 记录 surfaced 反思: {len(surfaced_reflection_ids)} 条"
            )
    except Exception as exc:
        active_logger.debug(
            "[%s] 长期记忆后处理失败（不影响主流程）: %s",
            lanlan_name,
            exc,
        )

    if selected_web_topic_key and (
        selected_web_link is None or _is_link_selected(selected_web_link, source_links)
    ):
        web_link = selected_web_link or {}
        web_title = web_link.get("title", "") or (
            web_parsed.get("title", "") if web_parsed else ""
        )
        await _record_source_used(
            url=web_link.get("dedupe_key") or web_link.get("url", "") or "",
            kind="web",
            title=web_title,
            **state_storage_kwargs,
        )
        print(
            f"[{lanlan_name}] 已记录 Web source 衰减历史: {selected_web_topic_key[:16]}"
        )

    if selected_music_topic_key and (
        delivery.is_music_used or _is_link_selected(selected_music_link, source_links)
    ):
        music_link = selected_music_link or {}
        music_title = (
            f"{music_link.get('title', '')} - {music_link.get('artist', '')}"
        ).strip(" -")
        await _record_source_used(
            url=music_link.get("url", "") or "",
            kind="music",
            title=music_title,
            **state_storage_kwargs,
        )
        print(
            f"[{lanlan_name}] 已记录音乐 source 衰减历史: "
            f"{selected_music_topic_key[:16]}"
        )

    if selected_meme_topic_key and _is_link_selected(
        selected_meme_link,
        source_links,
    ):
        await _record_source_used(
            url=(selected_meme_link or {}).get("url", "") or "",
            kind="image",
            title=(selected_meme_link or {}).get("title", "") or "",
            **state_storage_kwargs,
        )
        print(
            f"[{lanlan_name}] 已记录表情包 source 衰减历史: "
            f"{selected_meme_topic_key[:16]}"
        )

    return ProactiveChatResult(
        body=_proactive_chat_body(
            PROACTIVE_REASON_CHAT_DELIVERED,
            message="主动搭话已发送",
            lanlan_name=lanlan_name,
            source_mode=effective_source_mode,
            source_tag=source_tag or "unknown",
            active_channels=active_channels,
            source_links=source_links,
            turn_id=mgr.current_speech_id,
        )
    )
