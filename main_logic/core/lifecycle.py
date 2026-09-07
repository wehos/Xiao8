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
"""Session lifecycle for ``LLMSessionManager``: start/end/cleanup,
pending-session preparation, the hot-swap finalization sequence, the
idle reset loop, and error/silence recovery.

Method-only mixin: every instance attribute is assigned in
``LLMSessionManager.__init__`` (``main_logic.core.manager``).
"""

import asyncio
import inspect
import json
import time
from datetime import datetime
from websockets import exceptions as web_exceptions
from fastapi import WebSocket, WebSocketDisconnect
from main_logic.omni_realtime_client import OmniRealtimeClient
from main_logic.omni_offline_client import OmniOfflineClient
from main_logic.provider_failure_signals import (
    CODES_REQUIRING_MSG_DETAIL,
    classify_provider_failure_text,
)
from main_logic.proactive_delivery import (
    DELIVERY_RETRACTED_KEY,
    PASSIVE_MEDIA_BUDGET_DEFERRED_KEY,
    PASSIVE_MEDIA_MAX_RETRIES,
    PASSIVE_MEDIA_RETRY_KEY,
    PASSIVE_MEDIA_TRANSIENT_KEY,
    SWAP_PRIME_DELIVERY_CLAIM_KEY,
    resolve_callback_delivery_ack,
)
from utils.gptsovits_config import is_gsv_disabled_voice_id
from config.prompts.prompts_sys import get_context_summary_ready
from utils.config_manager import _as_bool, ensure_default_yui_voice_for_free_api
from utils.language_utils import normalize_language_code, get_global_language_full
from utils.game_route_state import get_active_game_route_generation_identity
from queue import Empty
from uuid import uuid4
import httpx
from ._shared import (
    logger,
    IDLE_SESSION_RESET_THRESHOLD_SECONDS,
    IDLE_SESSION_RESET_CHECK_INTERVAL_SECONDS,
    FRONTEND_START_SESSION_TIMEOUT_SECONDS,
    _HANDSHAKE_OVERRIDE_UNSET,
    _START_LLM_CONCURRENT_ABORTED,
    _ORPHAN_SESSION_REAPER_TASKS,
    _PASSIVE_MEDIA_SESSION_UPDATE_ACK_TIMEOUT_S,
)
from .callback_render import (
    _build_callback_instruction,
    _render_pending_extra_replies_by_origin,
    _select_callbacks_within_token_budget,
)

# Late-binding read point for symbols that tests rebind on the facade via
# ``monkeypatch.setattr("main_logic.core.<attr>", ...)``. Do NOT from-import
# those names here: a from-import snapshots the value at import time and the
# facade patch would no longer reach this module's methods.
from main_logic import core as _core_facade

class LifecycleMixin:
    """Session lifecycle methods (see module docstring)."""

    def is_goodbye_silent(self) -> bool:
        """Whether cat-mode silence after being asked to leave is in effect."""
        return bool(getattr(self, "goodbye_silent", False))

    def set_goodbye_silent(self, active: bool, reason: str = "") -> None:
        """Sync the frontend cat-mode silence state, and park queued proactive callbacks in the persistent queue."""
        active = bool(active)
        reason = str(reason or "")[:64]
        was_active = self.is_goodbye_silent()
        if active and not was_active:
            self.goodbye_silent_started_monotonic = time.monotonic()
            self.goodbye_silent_completed_duration = None
        elif not active and was_active:
            started_at = float(getattr(self, "goodbye_silent_started_monotonic", 0.0) or 0.0)
            self.goodbye_silent_completed_duration = (
                max(0.0, time.monotonic() - started_at)
                if started_at > 0.0
                else None
            )
            self.goodbye_silent_started_monotonic = 0.0
        self.goodbye_silent = active
        self.goodbye_silent_reason = reason
        self.goodbye_silent_updated_at = time.time()
        if active:
            self._park_proactive_for_goodbye()
        if was_active != active:
            logger.info("[%s] goodbye_silent=%s reason=%s", self.lanlan_name, active, reason or "-")

    def consume_goodbye_cycle_duration(self) -> float | None:
        """Return one completed server-observed goodbye duration at most once."""
        if self.is_goodbye_silent():
            return None
        duration = getattr(self, "goodbye_silent_completed_duration", None)
        self.goodbye_silent_completed_duration = None
        return duration


    async def handle_silence_timeout(self, *, expected_session=None):
        """Handle voice-input silence timeout: automatically close the session while keeping the Live2D display"""
        try:
            if expected_session is not None:
                if expected_session is self.pending_session:
                    logger.info("⏭️ handle_silence_timeout: expected_session is pending_session, delegating to pending teardown")
                    await self._teardown_pending_session_from_lifecycle_callback(expected_session)
                    return
                if expected_session is not self.session:
                    logger.info("⏭️ handle_silence_timeout: expected_session stale, skipping")
                    return
            logger.warning(f"[{self.lanlan_name}] 检测到长时间无语音输入，自动关闭session")
            
            # 静默关闭是权威抑制边界；不能保留一段残缺候选音频。
            async with self.hot_swap_cache_lock:
                # Re-check: a hot-swap could have completed while we waited for the lock.
                if expected_session is not None and expected_session is not self.session and expected_session is not self.pending_session:
                    logger.info("⏭️ handle_silence_timeout: expected_session stale after acquiring cache lock, skipping")
                    return
                if self.hot_swap_audio_cache:
                    cached_duration_ms = self.hot_swap_audio_cache.duration_ms
                    self.hot_swap_audio_cache.clear()
                    logger.info(
                        "🗑️ 静默超时：已清空 %s ms 热切换音频缓存",
                        cached_duration_ms,
                    )
            
            # Re-check before websocket side-effects
            if expected_session is not None and expected_session is not self.session and expected_session is not self.pending_session:
                logger.info("⏭️ handle_silence_timeout: expected_session stale before WS send, skipping")
                return
            
            # Deriving the payload needs no socket, so it is hoisted out of the
            # display-socket guard: the lease holder must hear the microphone
            # teardown even when the display socket is already gone. Closing a
            # chat window leaves mgr.websocket pointing at the dead socket until
            # that disconnect's voice handover repoints it, and the silence
            # timeout can fire inside exactly that window.
            session_for_reason = expected_session or self.session or self.pending_session
            timeout_api_type = str(
                getattr(session_for_reason, "_api_type", "") or getattr(self, "core_api_type", "") or ""
            ).lower()
            timeout_model = str(
                getattr(session_for_reason, "_model_lower", "")
                or getattr(session_for_reason, "model", "")
                or ""
            ).lower()
            is_free_timeout = timeout_api_type == "free" or "free" in timeout_model
            timeout_reason_code = (
                "free_api_silence_timeout" if is_free_timeout else "silence_timeout"
            )
            # Snapshot the lease this timeout is speaking to, BEFORE the sends
            # below await. Compared again just before the fan-out.
            #
            # Connection id only, deliberately NOT the lease generation: the
            # generation is bumped by the SAME holder on every hard_mute /
            # hard_unmute / focus_suppress / focus_resume / lease_sync
            # (asr_runtime.py _handle_voice_input_control). Including it makes a
            # user muting during the display send look like a lease handover, so
            # the teardown is skipped while end_session below still runs -- the
            # backend closes the session and the recorder never hears about it,
            # which is the exact zombie microphone this fan-out exists to
            # prevent. Identity is what decides whether we are still talking to
            # the same window; its mute state is not.
            voice_lease_identity = getattr(self, "_voice_lease_connection_id", "")
            if is_free_timeout:
                # Pure display toast; send_status guards its own socket.
                await self.send_status(json.dumps({"code": "FREE_API_AUTO_CLOSE_VOICE"}))
            auto_close_payload = {
                "type": "auto_close_mic",
                "reason_code": timeout_reason_code,
                "api_type": timeout_api_type,
                "message": f"{self.lanlan_name}检测到长时间无语音输入，已自动关闭麦克风"
            }
            if self.websocket and hasattr(self.websocket, 'client_state') and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                try:
                    await self.websocket.send_json(auto_close_payload)
                except WebSocketDisconnect:
                    # Isolated: a display socket that dies between the CONNECTED
                    # check and the send must not skip the fan-out below, nor
                    # the end_session that follows it.
                    pass
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[%s] auto_close_mic display push failed: %s",
                        self.lanlan_name,
                        e,
                    )
            # Re-validate before targeting the LEASE holder. Codex P2: the sends
            # above await and nothing here holds ``self.lock``, so a replacement
            # session can be installed during them -- and _send_to_voice_owner
            # resolves the owner socket at CALL time, so this old timeout's
            # auto_close_mic would land on the NEW recorder and stop a
            # microphone the user just opened. Reproduced: with the lease moved
            # mid-send, the fresh recorder received the stale teardown.
            #
            # end_session below is guarded by expected_session and refuses on
            # its own, which is precisely why the damage was frontend-only and
            # invisible from the backend. It still runs: a stale timeout has
            # nothing to close, and the guard is what decides that.
            #
            # The lease holder is the window with the live hardware; the
            # current socket may be a newer chat window with no microphone.
            # Game owner exempt, mirroring _revoke_lease_for_blocked_route.
            # (The FREE_API_AUTO_CLOSE_VOICE send_status above stays
            # current-socket-only on purpose: it is a pure toast.)
            session_still_current = expected_session is None or (
                expected_session is self.session
                or expected_session is self.pending_session
            )
            lease_still_current = voice_lease_identity == getattr(
                self, "_voice_lease_connection_id", ""
            )
            if not session_still_current or not lease_still_current:
                logger.info(
                    "⏭️ handle_silence_timeout: session/lease moved during the display send, "
                    "skipping the recorder teardown (session_current=%s lease_current=%s)",
                    session_still_current,
                    lease_still_current,
                )
            elif getattr(self, "_voice_lease_owner", "none") != "game":
                send_to_voice_owner = getattr(self, "_send_to_voice_owner", None)
                if callable(send_to_voice_owner):
                    await send_to_voice_owner(auto_close_payload)
            
            await self.end_session(by_server=True, expected_session=expected_session)
            
        except Exception as e:
            logger.error(f"处理静默超时时出错: {e}")
    
    async def handle_connection_error(self, message=None, *, expected_session=None):
        message_text = str(message) if message is not None else ""
        try:
            _parsed = json.loads(message_text) if message_text.startswith('{') else None
        except (json.JSONDecodeError, TypeError):
            _parsed = None

        async with self.lock:
            is_pending = False
            if expected_session is not None:
                if expected_session is self.pending_session:
                    is_pending = True
                elif expected_session is not self.session:
                    logger.info("⏭️ handle_connection_error: expected_session stale (not current session), skipping")
                    return
                details = _parsed.get('details') if isinstance(_parsed, dict) else None
                failure_generation = (
                    details.get('connection_generation')
                    if isinstance(details, dict)
                    else None
                )
                current_generation = getattr(
                    expected_session, '_connection_generation', None
                )
                if (
                    isinstance(failure_generation, int)
                    and isinstance(current_generation, int)
                    and failure_generation != current_generation
                ):
                    logger.info(
                        "⏭️ handle_connection_error: connection generation stale "
                        "(failure=%s current=%s), skipping",
                        failure_generation,
                        current_generation,
                    )
                    return
            # Only flag the manager-level flag for main session errors (or unguarded calls).
            # A pending_session failure must not misclassify the main session as closed.
            if not is_pending:
                self.session_closed_by_server = True
        
        if is_pending:
            logger.info("⏭️ handle_connection_error: expected_session is pending_session, delegating to pending teardown")
            await self._teardown_pending_session_from_lifecycle_callback(expected_session, message)
            return
        
        if message:
            # Pre-classified structured errors from omni_realtime_client (JSON with "code")
            # Forward them directly so the frontend sees the original code.
            if _parsed and isinstance(_parsed, dict) and _parsed.get('code'):
                # Peer disconnects use the existing recovery below, which
                # supplies CHARACTER_DISCONNECTED with the configured name.
                # Forwarding this marker here would show the same toast twice.
                if _parsed.get('code') != 'CHARACTER_DISCONNECTED':
                    await self.send_status(message_text)
            else:
                # Same criteria, and the same ordering, the realtime close
                # path reads — from one place, so a keyword added for one
                # provider never goes missing on the other side. The details
                # payload stays this side's own: here we are holding a real
                # upstream diagnostic and echo it, where a peer-controlled
                # close reason is deliberately withheld.
                status_code = (
                    classify_provider_failure_text(message_text)
                    or "API_UNKNOWN_ERROR"
                )
                status_payload = {"code": status_code}
                if status_code in CODES_REQUIRING_MSG_DETAIL:
                    status_payload["details"] = {"msg": message_text}
                await self.send_status(json.dumps(status_payload))
        logger.info("💥 Realtime connection recovery requested.")
        await self.disconnected_by_server(expected_session=expected_session)
    
    async def handle_repetition_detected(self):
        """Handle the repetition-detection callback: reset Focus state, notify the frontend"""
        try:
            logger.warning(f"[{self.lanlan_name}] 检测到高重复度对话")

            # Repetition recovery wiped _conversation_history — the Focus
            # accumulator charge / mode and the cadence baseline are evidence
            # from the now-erased conversation, so clear them too (对偶
            # _init_renew_status 的会话级清场). clear_focus emits no FOCUS_EXIT:
            # a degenerate looping episode is not a coherent episode to
            # synthesize. Best-effort — never block the frontend notice.
            try:
                await self.state.clear_focus()
                self._focus_scorer.reset()
                self._master_emotion.reset()
            except Exception as _focus_err:
                logger.debug(f"[{self.lanlan_name}] focus reset on repetition failed: {_focus_err}")

            # 向前端发送重复警告消息（使用 i18n key）
            if self.websocket and hasattr(self.websocket, 'client_state') and self.websocket.client_state == self.websocket.client_state.CONNECTED:
                await self.websocket.send_json({
                    "type": "repetition_warning",
                    "name": self.lanlan_name  # 前端会用这个名字填充 i18n 模板
                })
            
        except Exception as e:
            logger.error(f"处理重复度检测时出错: {e}")

    def _bind_session_lifecycle_callbacks(self, session):
        """Bind lifecycle callbacks with closure-captured session reference.
        
        Ensures that even if self.session is replaced later, the callbacks
        still carry a reference to the session they were bound to,
        enabling the expected_session guard to detect stale callbacks.
        """
        async def on_connection_error(message=None, session_ref=session):
            await self.handle_connection_error(message, expected_session=session_ref)
        
        # OmniRealtimeClient stores as .on_connection_error
        if isinstance(session, OmniRealtimeClient):
            session.on_connection_error = on_connection_error
        # OmniOfflineClient stores as .handle_connection_error
        elif isinstance(session, OmniOfflineClient):
            session.handle_connection_error = on_connection_error
        
        if hasattr(session, 'on_silence_timeout'):
            async def on_silence_timeout(session_ref=session):
                await self.handle_silence_timeout(expected_session=session_ref)
            session.on_silence_timeout = on_silence_timeout

    async def _restart_message_handler_after_session_reconnect(
        self,
        session_ref,
    ) -> bool:
        """Replace the receive task after an in-place Provider reconnect.

        Gemini quarantine reconnects the same ``OmniRealtimeClient`` object,
        while its existing handler remains bound to the retired SDK session.
        Keep task ownership in Core and use the manager/session identity as the
        CAS fence so a concurrent end-session or hot swap cannot resurrect a
        listener for a session it already replaced.
        """

        if session_ref is not self.session or not self.is_active:
            return False
        previous_task = self.message_handler_task
        if previous_task is not None and previous_task is not asyncio.current_task():
            if not previous_task.done():
                previous_task.cancel()
            # 有界地等：绑在退休 SDK 会话上的 receive task 可能延迟或吞掉
            # CancelledError，而这个 helper 的多个调用方是**持着**
            # _core_voice_session_swap_lock 进来的（例如 asr_runtime.py 的
            # final-submit 路径），无界 gather 会把后续所有语音 final 和热切换一起
            # 卡死。与 handoff 那条 listener 超时同一判据：只等到期，不等取消完成。
            done, _pending = await asyncio.wait(
                {previous_task},
                timeout=getattr(
                    self,
                    "_core_voice_listener_cancel_timeout_s",
                    2.0,
                ),
            )
            if not done:
                # 停不下来的 listener 仍绑在退休会话上。不能在它之上装替换
                # listener（两个 receive 循环同时跑同一个 client）。
                #
                # 但**光返回 False 不够**：调用方只会放弃本次投递，
                # self.session / is_active / message_handler_task 仍然指着一个看
                # 起来还活着、实际没有 receive 循环的 client —— 之后每一轮都撞上
                # 同一个卡死的 task、再超时一次，语音从此永远收不到回复。所以这里
                # 必须把这条会话退休掉，与 handoff 那条超时路径同一判据。
                logger.error(
                    '[%s] session reconnect: previous listener cancellation timed out; retiring the unusable session',
                    self.lanlan_name,
                )
                orphan_session = session_ref
                stuck_listener = previous_task
                async with self.lock:
                    # 双身份 CAS：并发的赢家（新 session 或新 listener）绝不能被
                    # 这条失败路径清掉。
                    if (
                        self.session is session_ref
                        and self.message_handler_task is previous_task
                    ):
                        self.session = None
                        self.message_handler_task = None
                        self.is_active = False
                        self.session_ready = False
                    else:
                        orphan_session = None

                if orphan_session is not None:
                    async def _reap_reconnect_session_after_listener_exit():
                        # 先关（close() 会同步摘掉 socket），再有界 join —— 反过来
                        # 就是在等一个已经证明停不下来的 task。
                        try:
                            await orphan_session.close()
                        except Exception as reap_err:
                            logger.debug(
                                '[%s] session reconnect: orphan close failed: %s',
                                self.lanlan_name,
                                reap_err,
                            )
                        try:
                            await asyncio.wait(
                                {stuck_listener},
                                timeout=getattr(
                                    self,
                                    "_core_voice_listener_cancel_timeout_s",
                                    2.0,
                                ),
                            )
                        except (asyncio.CancelledError, Exception):
                            pass

                    reaper = asyncio.create_task(
                        _reap_reconnect_session_after_listener_exit()
                    )
                    _ORPHAN_SESSION_REAPER_TASKS.add(reaper)
                    reaper.add_done_callback(_ORPHAN_SESSION_REAPER_TASKS.discard)
                    # 会话没了，麦克风不能还开着收 PCM —— 独立 ASR 会继续往一个
                    # 已经不存在的回答会话里投 transcript。与 handoff 那条
                    # listener_timeout_fail_closed 路径同款收口（只在 CAS 赢了、
                    # 确实是我们清掉这条会话时才做）。
                    await self._close_independent_asr(next_route_mode="blocked")
                    await self.send_session_ended_by_server()
                return False
            # 取一次结果，免得旧 listener 的异常变成 "never retrieved" 警告。
            if not previous_task.cancelled():
                previous_task.exception()

        async with self.lock:
            if session_ref is not self.session or not self.is_active:
                return False
            current_task = self.message_handler_task
            if (
                current_task is not None
                and current_task is not previous_task
                and not current_task.done()
            ):
                return True
            self.message_handler_task = asyncio.create_task(
                session_ref.handle_messages()
            )
        return True

    async def _teardown_pending_session_from_lifecycle_callback(self, expected_session, message=None):
        """Handle lifecycle callback (connection_error / silence_timeout) fired
        by a pending_session that has NOT yet been promoted to self.session.
        
        This avoids routing through the main session cleanup flow which would
        incorrectly kill the active main session.
        """
        if message:
            message_text = str(message)
            logger.warning(f"💥 Pending session lifecycle error: {message_text}")
        else:
            logger.warning("💥 Pending session lifecycle event (silence/disconnect)")
        
        if expected_session is self.pending_session:
            await self._cleanup_pending_session_resources()
            await self._reset_preparation_state(clear_main_cache=True)
        else:
            # pending_session already swapped or cleaned by someone else
            logger.info("⏭️ _teardown_pending: expected_session no longer matches pending_session, skipping")

    async def _reset_preparation_state(self, clear_main_cache=False, from_final_swap=False):
        """[Hot-swap related] Helper to reset flags and pending components related to new session prep.
        
        async because we await cancelled tasks to guarantee they have exited
        before clearing references — prevents >2 concurrent OmniRealtimeClient.
        """
        self.is_preparing_new_session = False
        self._require_context_append_current_delivery = False
        self.summary_triggered_time = None
        self.initial_cache_snapshot_len = 0
        
        # Snapshot task refs, cancel, await completion, THEN clear.
        # This ensures CancelledError handlers (e.g. _cleanup_pending_session_resources)
        # finish before we drop references, preventing races with newly created tasks.
        bg_task_ref = self.background_preparation_task
        swap_task_ref = self.final_swap_task if not from_final_swap else None
        # 自引用守卫：本清理常被 final_swap_task 自己调回（swap 的中止 handler、
        # 入口守卫、prime 失败出口都没传 from_final_swap）。cancel 当前任务会在
        # 下面 gather 悬挂点把 CancelledError 打回调用方 except 块，截断其后的
        # 全部清理——生产拓扑实测 swap 各 fail-close / 恢复尾巴因此成死代码，
        # 且 try 之前的入口守卫死在 reset 后会把 is_hot_swap_imminent 卡成 True。
        # 当前任务一律不 cancel、不等待（等自己必死锁）。
        _current_task = asyncio.current_task()
        if bg_task_ref is _current_task:
            bg_task_ref = None
        if swap_task_ref is _current_task:
            swap_task_ref = None

        tasks_to_await = []
        if bg_task_ref and not bg_task_ref.done():
            bg_task_ref.cancel()
            tasks_to_await.append(bg_task_ref)
        if swap_task_ref and not swap_task_ref.done():
            swap_task_ref.cancel()
            tasks_to_await.append(swap_task_ref)
        # 并行 wait：bg 和 swap task 已 cancel，串行最坏 4s 墙钟，gather 后 2s 封顶
        if tasks_to_await:
            async def _wait_one(t):
                try:
                    await asyncio.wait_for(t, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    # 清理路径：cancel 后的任务必然抛这两者之一，吞掉即可
                    pass
                except Exception as e:
                    # 非预期异常不应阻塞准备状态重置，记 debug 方便排障
                    logger.debug(f"_wait_one: ignored unexpected exception: {e}")
            await asyncio.gather(*(_wait_one(t) for t in tasks_to_await), return_exceptions=True)
        
        if self.background_preparation_task is bg_task_ref:
            self.background_preparation_task = None
        if not from_final_swap and self.final_swap_task is swap_task_ref:
            self.final_swap_task = None
        self.pending_session_warmed_up_event = None
        self.pending_session_final_prime_complete_event = None
        self.pending_use_tts = None

        if clear_main_cache:
            self.message_cache_for_new_session = []
            self.initial_next_session_context_snapshot_len = 0

    async def _cleanup_pending_session_resources(self):
        """[Hot-swap related] Safely cleans up ONLY PENDING connector and session if they exist AND are not the current main session.

        The close runs as a task this manager owns and every caller awaits it
        through ``shield``. Its usual caller is the background prep task's
        CancelledError handler, and ``_reset_preparation_state`` caps its wait
        at 2s by cancelling that same task a second time — which, when the
        close was awaited directly, interrupted it after the reference had
        already been dropped: nobody left to finish closing the socket, and
        ``_wait_one`` swallows the timeout so the reset reports success. The
        cap now bounds only how long the caller waits.
        """
        # Stop any listener specifically for the pending session (if different from main listener structure)
        # The _listen_for_pending_session_response tasks are short-lived and managed by their callers.
        session, self.pending_session = self.pending_session, None
        if session:
            task = asyncio.create_task(self._close_detached_pending_session(session))
            self._pending_session_close_tasks.add(task)
            task.add_done_callback(self._pending_session_close_tasks.discard)
            await asyncio.shield(task)

    async def _close_detached_pending_session(self, session):
        """Close a pending session that no longer has a slot to be cleared from."""
        try:
            logger.info("🧹 清理pending_session资源...")
            await session.close()
            logger.info("✅ Pending session已关闭")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"💥 清理pending_session时出错: {e}")

    async def _init_renew_status(self):
        await self._reset_preparation_state(True)
        self.session_start_time = None
        await self._cleanup_pending_session_resources()  # 关闭由 manager 持有，取消也不会丢
        self.is_hot_swap_imminent = False
        # 状态机是 per-manager 的，跨 start_session/end_session 复用同一实例。
        # 若上一轮 proactive 在 PHASE1/PHASE2 中途 WS 断开、PROACTIVE_DONE 来不及
        # fire，phase/_preempted 会泄漏到新会话，堵死 can_start_proactive。
        # teardown 必须用 force=True：默认 reset() 会在活动 phase 上 no-op（保护
        # auto-start 不被误清），但 end_session 语义就是整轮收尾，必须强制清场。
        await self.state.reset(force=True)
        # 对偶 SM.reset 清 focus 态：scorer 的 cadence 基线也按会话隔离，新会话
        # 不继承上一会话的消息长度基线。master 情绪画像同样按会话隔离。
        self._focus_scorer.reset()
        self._master_emotion.reset()

    def _realtime_base_url(self) -> str:
        """Read the realtime route's base_url, for the native voice routing host remap
        (overseas free free→free_intl). Returns an empty string when unreadable, treated as non-lanlan.app."""
        try:
            return str((self._config_manager.get_model_api_config('realtime') or {}).get('base_url') or '')
        except Exception:
            return ''

    async def _handle_session_start_exception(self, e: BaseException, input_mode: str, diag_start: float) -> None:
        """Unified handling of session start failure: log, send the status code, send_session_failed, cleanup.

        Used by start_session's outer except, covering both the prelude
        (_cleanup_pending_session_resources / end_session etc.) and the gather
        block, so the frontend doesn't get stuck on preparing.
        """
        self.session_start_failure_count += 1
        self.session_start_last_failure_time = datetime.now()
        logger.error(f"[语音会话诊断] start_session 失败 (总耗时: {time.time() - diag_start:.2f}秒): {e}")
        # Telemetry：语音会话启动失败 —— 语音优先桌宠，voice 在用户开口前就坏掉
        # = 静默 D1 流失（现在完全看不到）。reason 用异常类名（低基数 enum）。
        # **仅 audio 模式计**：本收口对 text/audio 两种 start_session 都用，text
        # 启动失败不该误标成 voice_setup_failed 污染该信号。best-effort 不阻塞收口。
        if input_mode == 'audio':
            try:
                from utils.instrument import counter as _instr_counter
                _instr_counter("voice_setup_failed", reason=type(e).__name__[:32])
            except Exception:
                pass  # 埋点 best-effort：instrument 不可用也不能挡失败收口流程
        error_str = str(e)

        is_memory_server_error = isinstance(e, ConnectionError) and any(
            kw in error_str.lower() for kw in ["memory server", "记忆服务"]
        )

        if is_memory_server_error:
            logger.error(f"🧠 {error_str}")
            await self.send_status(json.dumps({"code": "MEMORY_SERVER_NOT_RUNNING"}))
            # Memory Server 错误不计入失败次数（这是配置问题而非网络问题）
            self.session_start_failure_count -= 1
            self._memory_error_retry_after = time.time() + self._memory_error_cooldown_seconds
        else:
            error_message = f"Error starting session: {e}"
            logger.exception(f"💥 {error_message} (失败次数: {self.session_start_failure_count})")

            if self.session_start_failure_count >= self.session_start_max_failures:
                # 仅在熔断"刚跳闸"时打 CRITICAL + 推 status；之后的失败由
                # start_session 早退拦截（理论上不会再走到这里），CRITICAL 只发一次。
                if not self._session_start_circuit_open:
                    self._session_start_circuit_open = True
                    critical_message = f"⛔ Session启动连续失败{self.session_start_failure_count}次，已停止自动重试。请检查网络连接和API配置，然后刷新页面重试。"
                    logger.critical(critical_message)
                    await self.send_status(json.dumps({"code": "SESSION_START_CRITICAL", "details": {"count": self.session_start_failure_count}}))
            else:
                await self.send_status(json.dumps({"code": "SESSION_START_FAILED", "details": {"error": str(e), "count": self.session_start_failure_count}}))

            if 'WinError 10061' in error_str or 'WinError 10054' in error_str:
                if str(self.memory_server_port) in error_str or '48912' in error_str:
                    await self.send_status(json.dumps({"code": "MEMORY_SERVER_CRASHED", "details": {"port": self.memory_server_port}}))
                else:
                    await self.send_status(json.dumps({"code": "CONNECTION_REFUSED"}))
            elif ('401' in error_str or 'unauthorized' in error_str.lower()
                    or 'authentication' in error_str.lower()
                    or 'incorrect api key' in error_str.lower()
                    or 'invalid_api_key' in error_str.lower()
                    or ('invalid' in error_str.lower() and 'key' in error_str.lower())):
                await self.send_status(json.dumps({"code": "API_KEY_REJECTED"}))
            elif '429' in error_str:
                await self.send_status(json.dumps({"code": "API_RATE_LIMIT_SESSION"}))
            elif 'HTTP 503' in error_str:
                await self.send_status(json.dumps({"code": "UPSTREAM_SERVER_BUSY"}))
            elif 'All connection attempts failed' in error_str:
                await self.send_status(json.dumps({"code": "LLM_CONNECTION_FAILED"}))
            else:
                await self.send_status(json.dumps({"code": "CONNECTION_CLOSED_ABNORMAL", "details": {"error": error_str}}))

        # 必须在 cleanup 之前发送，因为 cleanup 会清空 websocket 引用
        await self.send_session_failed(input_mode)
        # reset_starting_count=False：本函数从失败的 start_session 的 except 里调用，
        # 那次 start_session 的 finally 才是 _starting_session_count guard 的唯一所有者
        # 并会在最后递减它。若让这里的 cleanup 提前把 count 清 0，会开出一个"失败任务
        # 尚未完全收尾、但 count 已 0"的窗口，等待中的跨模式重启会据此重入，随后被
        # 失败任务残余的 cleanup（清 websocket）和 finally（减 guard）clobber（Codex P2）。
        await self.cleanup(reset_starting_count=False)
        # 但 reset_starting_count=False 会让 end_session 的 inactive-early 路径跳过
        # pending_input_data.clear()（那块与 guard 释放耦合），导致本次失败启动期间缓存的
        # 输入残留、被下次成功启动的 _flush_pending_input_data() 误注入（Codex P2）。
        # 这里显式补清本次失败 start 自己的输入：此刻 count 仍被本次 finally 持有(>0)，
        # 没有并发 start 穿过、缓存里只可能是本次失败 start 的输入，清理安全。
        # 不走 end_session 的 gating 改动，rebuild 路径(同样 reset_starting_count=False 但
        # 需要保留输入回放)语义不受影响。
        async with self.input_cache_lock:
            self.session_ready = False
            self.pending_input_data.clear()
            self._clear_pending_context_appends()

    @property
    def is_starting(self) -> bool:
        """The window where the start_session coroutine is running but is_active isn't True yet.
        Externals (e.g. the catgirl-switch path) use this to decide whether to keep
        the current manager instance, avoiding replacing a manager mid-initialization
        and leaking an orphan session.
        """
        return self._starting_session_count > 0

    @property
    def starting_input_mode(self):
        """Return the target mode being started, avoiding reads of an input_mode that hasn't finished switching."""
        if self._starting_session_count <= 0:
            return None
        return self._starting_input_mode

    def reset_session_start_circuit(self) -> None:
        """Clear the circuit breaker + failure counter + memory cooldown. Only for
        websocket_router upon receiving an explicit user start_session action — that is
        equivalent to "the user saw CRITICAL, chose to retry, and declares the config
        fixed". So _memory_error_retry_after is cleared along the way; otherwise the
        user would still wait an extra 10 seconds after starting the memory server.
        Internal recovery paths must never call this, or the circuit breaker becomes
        meaningless."""
        if (self._session_start_circuit_open
                or self.session_start_failure_count
                or self._memory_error_retry_after):
            logger.info(f"🔄 重置 session 启动熔断 (之前失败 {self.session_start_failure_count} 次)")
        self._session_start_circuit_open = False
        self.session_start_failure_count = 0
        self.session_start_last_failure_time = None
        self._memory_error_retry_after = 0

    def shutdown(self) -> None:
        """Manager-level shutdown — cancels the idle reset background task. Caller:
        main_server's ``_init_character_resources``, before replacing the old
        manager with a new one.

        Why needed: ``_idle_session_reset_task`` is a bound-method coroutine
        holding a strong reference to ``self`` — after a config hot-reload creates
        a new LLMSessionManager to replace the old one, the old manager should be
        GC'd, but the leftover task wakes every 60s (even though it only takes the
        ``is_active==False`` early-exit branch), extending the old manager's
        lifetime indefinitely; N copies accumulate after repeated reloads.
        """
        task = self._idle_session_reset_task
        if task is not None and not task.done():
            task.cancel()
        self._idle_session_reset_task = None

    async def aclose(self) -> None:
        """Permanently release manager-owned background runtimes.

        Ordinary microphone/session teardown must use the reusable ASR
        ``stop_session()`` boundary.  This hook is reserved for the manager's
        ownership boundary (replacement, character deletion, or process
        shutdown), where the admission ingress itself must also be retired.

        Keep the existing synchronous ``shutdown()`` as the first step so no
        idle-reset task can wake and start new manager work while the terminal
        ASR close is in progress.  ``IndependentAsrRuntime.close()`` owns its
        cancellation-safe, idempotent terminal task, so repeated manager close
        requests converge on the same cleanup instead of creating a second
        ingress teardown.
        """

        self.shutdown()
        runtime = getattr(self, "_asr_runtime", None)
        if runtime is not None:
            await runtime.close()

    def _ensure_idle_session_reset_loop(self) -> None:
        """Lazily start the idle reset background task. Idempotent, safe to call repeatedly."""
        if self._idle_session_reset_task is not None and not self._idle_session_reset_task.done():
            return
        try:
            self._idle_session_reset_task = asyncio.create_task(self._idle_session_reset_loop())
        except RuntimeError:
            # 极端情况：没有 running event loop（不该发生于 start_session 路径）
            logger.debug("[%s] _ensure_idle_session_reset_loop: no running loop, skip", self.lanlan_name)

    async def _idle_session_reset_loop(self) -> None:
        """Periodically check the user's silence duration; past the threshold, proactively
        end_session so the next message triggers fresh /new_dialog context injection.
        Guards: responding / takeover / session starting / no activity timestamp →
        skip this round, re-evaluate next round.
        """
        while True:
            try:
                await asyncio.sleep(IDLE_SESSION_RESET_CHECK_INTERVAL_SECONDS)
                if not self.is_active or self.session is None:
                    continue
                if self._starting_session_count > 0:
                    continue
                if self._takeover_active:
                    continue
                if getattr(self.session, '_is_responding', False):
                    continue
                last_activity = self.last_user_activity_time
                if last_activity is None:
                    continue
                idle_seconds = time.time() - last_activity
                if idle_seconds < IDLE_SESSION_RESET_THRESHOLD_SECONDS:
                    continue
                # 快照当前 session：传给 end_session 的 expected_session 守卫，
                # 在 end_session 内部多个 await 期间若用户触发新一轮 start_session
                # 把 self.session 换掉了，end_session 会早退而不会误清新 session
                # 或 _starting_session_count guard（参见 end_session 6011-6013 注释）。
                session_snapshot = self.session
                logger.info(
                    "[%s] idle_session_reset: 用户静默 %.0fs ≥ %ds，主动关闭 session 让下一条消息刷新上下文",
                    self.lanlan_name, idle_seconds, IDLE_SESSION_RESET_THRESHOLD_SECONDS,
                )
                try:
                    # by_server=True：抑制末尾的 CHARACTER_LEFT 状态推送，把本路径
                    # 与用户主动离开的语义区分开。reset_starting_count=False：
                    # expected_session 早退已经把 race 兜住了，再叠一层保险防止
                    # await 期间挤进来的新 start_session guard 被清零。
                    await self.end_session(
                        by_server=True,
                        expected_session=session_snapshot,
                        reset_starting_count=False,
                    )
                except Exception as e:
                    logger.warning("[%s] idle_session_reset: end_session 失败: %s", self.lanlan_name, e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[%s] idle_session_reset 单轮异常: %s", self.lanlan_name, e)

    async def _maybe_kick_activity_loop_for_context_prompt(self) -> None:
        """Start the activity tracker's background heartbeat.

        Context-prompt detection (entering gaming/entertainment / entering focused
        work) hangs off the tracker's 20s heartbeat, and the heartbeat lazy-starts
        only on the first get_snapshot; get_snapshot in turn is only called by
        paths where proactive chat is on. Proactive chat defaults to off at first
        start, so without an explicit kick a user who hasn't enabled proactive chat
        would never detect entering a game and the prompt would never show. Here we
        kick once when the session comes up.

        The context prompt used to be gated to the vision_chat_default_off A/B group;
        it's now merged into main and open to everyone. OS signal collection is still
        gated on the user having *explicitly* allowed autonomous vision (privacy mode
        off), but the topic candidate heartbeat is privacy-independent and should
        run even when vision is disabled.
        """
        try:
            self._activity_tracker.ensure_activity_guess_loop_started()
            # 只有当 proactiveVisionEnabled 已被显式落盘为 True 才 kick：get_snapshot 会起
            # SystemSignalCollector 采集窗口/进程信号，且绕过隐私模式（loop 只跳过 LLM、
            # collector 仍在采）。不能用 is_privacy_mode_active()——它在 proactiveVisionEnabled
            # 缺失时 fail-open 成「隐私关」，于是首启 settings 尚未同步的窗口里，会把 UI 默认
            # 隐私开（海外首启 proactiveVisionEnabled 默认 false）的用户误判成可采集，启动一次
            # session 就采了窗口/进程（Codex P1）。这里改读原始落盘值，缺失/False 一律不 kick，
            # 等下一次 session（settings 已同步、用户确为 vision 开）再拉起；隐私开的用户本就是
            # no-op（屏幕分享来源开不了），不 kick 无损。
            from utils.preferences import aload_global_conversation_settings
            settings = await aload_global_conversation_settings()
            if settings.get('proactiveVisionEnabled') is not True:
                return
            # 清情境弹窗基线：tracker 跨 session 长存，若用户上个 session 结束时就在
            # 游戏/工作、这个 session 仍在同一状态，不清就检测不到「进入」、本会话漏弹。
            self._activity_tracker.reset_context_prompt_baseline()
            await self._activity_tracker.get_snapshot()
        except Exception as e:
            logger.debug("[%s] 活动心跳 kick 失败: %s", self.lanlan_name, e)

    async def start_session(
        self,
        websocket: WebSocket,
        new=False,
        input_mode='audio',
        *,
        user_initiated=False,
        _allow_cross_mode_restart=True,
        handshake_override=_HANDSHAKE_OVERRIDE_UNSET,
        resource_optimization_override=_HANDSHAKE_OVERRIDE_UNSET,
        request_id=None,
    ):
        # user_initiated：True 仅由 websocket_router 的 start_session action 传入，
        # 标记"用户显式点击启动"。跨模式撞车时只有用户显式请求才会等 in-flight
        # 落定后改起目标模式；后台 proactive / greeting 的 auto-start 跨模式撞车
        # 仍走静默 return（保持原行为，避免后台 text 启动反过来顶掉用户的语音会话）。
        # _allow_cross_mode_restart：跨模式重启重入时置 False，把递归深度封到 1，
        # 二次并发撞车回落静默 return 而非无界递归。
        # Codex P2. Snapshot the start_session handshake for THIS dispatched
        # operation, before the first await. websocket_router writes the
        # frontend's authoritative independent-ASR toggle into one manager-level
        # field and then fires start_session as a background task; the route
        # decision only reads that field much later, inside
        # _start_independent_asr_if_enabled, after many awaits. A second
        # start_session arriving in between -- including a text one, or an older
        # frontend whose field is absent and therefore CLEARS the override --
        # replaced the first request's value, so that audio session selected the
        # persisted or opposite route. Read once here, then carry it down.
        session_handshake_override = (
            getattr(self, "_independent_asr_handshake_override", None)
            if handshake_override is _HANDSHAKE_OVERRIDE_UNSET
            else handshake_override
        )
        session_resource_optimization_handshake_override = (
            getattr(
                self,
                "_voice_input_resource_optimization_handshake_override",
                None,
            )
            if resource_optimization_override is _HANDSHAKE_OVERRIDE_UNSET
            else resource_optimization_override
        )
        self._start_session_seed_turn_language()
        # 重置防刷屏标志
        self.session_closed_by_server = False
        self.last_audio_send_error_time = 0.0
        # 熔断早退：达到失败上限后，所有内部 recovery 路径在此返回，
        # 避免 stream_data / _process_stream_data_internal 每个音频包都触发
        # 一次连接尝试导致日志被刷屏。用户显式 retry（websocket_router 的
        # start_session action）会在那边先调 reset_session_start_circuit() 清掉。
        if self._session_start_circuit_open:
            logger.debug("Session启动熔断已跳闸，忽略本次启动请求（等用户刷新/重试）")
            return
        # 检查是否正在启动中（in-flight 去重 / 跨模式改起，详见 helper）
        if await self._start_session_handle_inflight(
            websocket, new, input_mode,
            user_initiated=user_initiated,
            _allow_cross_mode_restart=_allow_cross_mode_restart,
            request_id=request_id,
            handshake_override=session_handshake_override,
            resource_optimization_override=(
                session_resource_optimization_handshake_override
            ),
        ):
            return

        # 标记正在启动（使用计数器，避免并发 start_session 的 finally 互相覆盖）
        self._starting_session_count += 1
        self._starting_input_mode = input_mode
        # 干净的播放门控：清掉上一会话可能残留的 playback flag / manager 队列
        # （前端中途断线/刷新导致 voice_play_end 丢失时尤为重要）。放在熔断早退
        # 与 "正在启动中" 去重早退 *之后*——那些早退不会真正起新 session，提前
        # reset 会误清掉仍在播放的旧会话门控（Codex P1）。这里已确定要起新会话。
        self._reset_proactive_gate()
        # 首次 start_session 起算，让 idle reset loop 永久存活
        self._ensure_idle_session_reset_loop()
        # rebase idle 计时基准：last_user_activity_time 是 manager 状态、跨 session 持久。
        # idle-reset 触发 end_session 后用户再开新 session 时，如不重置就会继承超过
        # 阈值的旧时间戳，下一轮 sweep 立刻把新 session 当成 30 min idle 再关一次。
        # 同步刷新 proactive 路径 10s 抑制窗口（prepare_proactive_delivery），避免
        # session 刚起来就被立刻触发主动搭话。
        self.last_user_activity_time = time.time()
        # CAS 落败早退标志：True 时禁止 finally 递减 guard，
        # 防止赢家初始化期间第三个协程穿过 guard 浪费 LLM 连接。
        _llm_concurrent_aborted = False
        _diag_start = time.time()
        # 预创建的 /new_dialog 任务：若 _start_session_start_llm 之前就抛异常，
        # finally 会负责 cancel + await，避免孤儿 task 残留连接。
        _new_dialog_task = None

        try:
            realtime_config, core_config_snapshot = await self._start_session_prepare_runtime(
                websocket, input_mode, new, _diag_start
            )
        
            await self._start_session_reset_stream_state(
                input_mode, realtime_config, core_config_snapshot
            )
        
            await self._start_session_retire_previous()

            # —— 提前发起 /new_dialog，避免被 TTS worker 线程的 dashscope
            # import 抢 GIL 拖慢。在 gather 之前就 create_task，让 httpx 先
            # 和 server 建好连接、收到响应；gather 时 _start_session_start_llm 只
            # await 现成的结果即可。
            logger.info(f"[语音会话诊断] 开始获取记忆上下文 (端口 {self.memory_server_port})")
            _mem_start = time.time()
            _new_dialog_task = asyncio.create_task(
                self._start_session_fetch_new_dialog(self.lanlan_name, self.memory_server_port)
            )

            # 重置状态
            if new:
                await self._start_session_reset_state_for_new()

            # 并行启动 TTS 和 LLM Session
            logger.info("🚀 并行启动 TTS 和 LLM Session...")
            start_parallel_time = time.time()
            
            tts_result, llm_result = await asyncio.gather(
                self._start_session_start_tts_if_needed(),
                self._start_session_start_llm(
                    input_mode, core_config_snapshot, realtime_config,
                    _new_dialog_task, _mem_start
                ),
                return_exceptions=True
            )
            
            logger.info(f"⚡ 并行启动完成 (总用时: {time.time() - start_parallel_time:.2f}秒)")
            tts_status = '异常' if isinstance(tts_result, Exception) else ('跳过(原生语音)' if not self.use_tts else 'OK')
            logger.info(f"[语音会话诊断] 并行启动结果: TTS={tts_status}, LLM={'异常' if isinstance(llm_result, Exception) else 'OK'}")
            # 检查是否有错误
            if isinstance(tts_result, Exception):
                logger.error(f"TTS 启动失败: {tts_result}")
            # 并发落败分支：赢家已持有 self.session / message_handler_task，
            # 我们不能继续走 "if self.session" 分支（会覆盖 handler task、重复
            # send_session_started），也不能 raise（会误触发 cleanup 杀掉赢家）。
            # 同时设置 _llm_concurrent_aborted=True 让 finally 跳过 guard 递减：
            # 赢家尚未完成初始化，必须保持 guard 以阻止第三个协程穿过。
            if llm_result is _START_LLM_CONCURRENT_ABORTED:
                logger.info("[语音会话诊断] start_session 因并发 CAS 落败早退，保持 guard 关闭")
                _llm_concurrent_aborted = True
                return
            if isinstance(llm_result, Exception):
                raise llm_result  # LLM Session 失败是致命的
            
            # 标记 session 激活
            if self.session:
                await self._start_session_activate(
                    input_mode,
                    llm_result,
                    _diag_start,
                    request_id=request_id,
                    handshake_override=session_handshake_override,
                    resource_optimization_override=(
                        session_resource_optimization_handshake_override
                    ),
                )
            else:
                raise Exception("Session not initialized")
        
        except Exception as e:
            # prelude（_cleanup_pending_session_resources / end_session / asyncio.sleep 等）
            # 与 gather 块的错误统一走这里收口：send_session_failed + cleanup，避免前端卡在 preparing。
            # 注意：except Exception 不会捕获 CancelledError，shutdown 路径保持原语义。
            await self._handle_session_start_exception(e, input_mode, _diag_start)
        finally:
            # 例外：CAS 落败早退时不递减——赢家还在初始化，若此时放开 guard，
            # 第三个协程会穿过并再次把入口快照当作"赢家"进而覆盖掉真正的赢家。
            # 赢家完成（成功或异常）后会通过自己的 finally 或 cleanup 清理 guard。
            if not _llm_concurrent_aborted:
                self._starting_session_count = max(0, self._starting_session_count - 1)
                if self._starting_session_count == 0:
                    self._starting_input_mode = None
            # 保险：若 /new_dialog 预取任务早期异常后仍在跑（gather 没来得及
            # await 它就异常退出），这里统一 cancel + await，避免 "Task exception
            # was never retrieved" warning 和连接池泄漏。
            if _new_dialog_task is not None and not _new_dialog_task.done():
                _new_dialog_task.cancel()
                try:
                    await _new_dialog_task
                except (asyncio.CancelledError, Exception):
                    # Cancellation echo or the prefetch's own error — moot once this start attempt ends.
                    pass

    async def _start_session_handle_inflight(
        self,
        websocket,
        new,
        input_mode,
        *,
        user_initiated,
        _allow_cross_mode_restart,
        request_id,
        handshake_override,
        resource_optimization_override,
    ):
        """Handle a start request that collides with an in-flight start_session.

        Returns True when the collision was fully handled here (same-mode dedup
        ack, cross-mode restart, or drop) and the caller must return without
        starting anything; False when no start is in flight.

        NOTE: the no-collision fast path must stay await-free so the caller's
        guard check -> increment sequence remains atomic on the event loop.
        """
        if self._starting_session_count <= 0:
            return False
        # 另一路 start_session（典型是 greeting 的 auto-start）已在飞。早期实现
        # 直接静默 return，但前端的 start_session 在 await 一个 session_started
        # ack——若它撞在这里被去重，ack 永远不来，前端 15s 后超时并卡死（用户
        # 在 greeting 出现前抢发消息触发的竞态：greeting 先把 in-flight 占住，
        # 而它完成时发的 ack 又早于前端开始 await，前端两头落空）。
        #
        # 仅对**同模式**的去重请求补发 ack：in-flight 启的是它自己的模式，
        # 跨模式（如 greeting 拉 text、另一路同刻请求 audio）若复用 in-flight 的
        # session_started(text)，前端会按 text 切 UI、收口 promise，而用户要的
        # audio 会话根本没起（CodeRabbit）。
        if (self._starting_input_mode or input_mode) == input_mode:
            logger.warning("⚠️ Session正在启动中，等 in-flight 启动落定后给本请求补发 session_started")
            # 等 in-flight 那次启动**自己落定**（_starting_session_count 归 0）。
            # 不拿 session_ready 当谓词：它可能还残留上一个 session 的 True
            # （in-flight start 要过几个 await 才把它重置），那样循环会被直接
            # 跳过、在 in-flight 还没真正起好时就误发 started 假阳性（Codex P1）。
            # 等待上限绑前端的 start_session 超时：超过它再补发 ack 已无意义
            # （前端早已 reject + end_session），故以它为窗口上界兼防挂安全阀。
            #
            # 快照本请求进入时的 voice lease 身份：等待可能长达十几秒，期间第三个
            # audio start 抢走麦克风是可能的，那时替它重跑路由会用**本请求**（已经
            # 被顶掉的那个窗口）的 handshake 去配新持有者的路由。新持有者自己也会
            # 走这条路径、且它的快照对得上，所以这里跳过不丢东西（Codex P2）。
            _lease_at_request = getattr(self, "_voice_lease_connection_id", "")
            # 墙钟基准，供下面算「前端 deadline 还剩多少」。不能拿 _waited
            # 当依据：它按标称 50ms 累加，事件循环一卡（或 sleep 超发）真实
            # 时间会甩开它好几秒，于是预算被高估、放行一次最长 12s 的 ASR
            # connect，补发的 ack 仍然赶在前端超时之后（Codex P2）。
            _wait_started = time.monotonic()
            _waited = 0.0
            while self._starting_session_count > 0 and _waited < FRONTEND_START_SESSION_TIMEOUT_SECONDS:
                await asyncio.sleep(0.05)
                _waited += 0.05
            # 仅当 in-flight 真正落定（count 归 0、即循环是「落定退出」而非
            # 「超时退出」）且会话确实活跃时才补发 session_started（与
            # in-flight 自身发的那条幂等，前端 resolver 一次性）。若是超时退出
            # （count 仍 >0、in-flight 没结束），self.session/is_active 在 restart
            # 流程里可能是上一个 session 残留的 True，补发会是假阳性（Codex P1），
            # 故一律不发。也**不**发 session_failed——in-flight 可能仍在跑/或其
            # 失败路径已通知前端，过早发 failed 会被前端当终态打断本会成功的启动。
            if self._starting_session_count == 0 and self.session and self.is_active:
                # 补发的 ack 带的是 in-flight 那次 start 的路由裁决（见
                # send_session_started），而这条路径本身从不重跑决策。裁决对本
                # 请求方可能已经作废：本请求抢 voice lease 会 invalidate in-flight
                # 的 ASR start，那次 start 于是 ASR_START_STALE 早退、把路由留在
                # blocked 占位上且不发任何 status，结果两个窗口都 fail-closed latch
                # 住、麦克风在本会话内再也打不开。先重跑一次决策再补 ack，让 ack
                # 带的是本请求方真正成立的路由（详见 _rerun_route_for_deduped_start）。
                await self._rerun_route_for_deduped_start(
                    input_mode,
                    lease_connection_id=_lease_at_request,
                    remaining_deadline_seconds=(
                        FRONTEND_START_SESSION_TIMEOUT_SECONDS
                        - (time.monotonic() - _wait_started)
                    ),
                    handshake_override=handshake_override,
                    resource_optimization_override=resource_optimization_override,
                )
                # ``also_notify``：重跑若 fail-closed 会 revoke lease，把
                # _voice_lease_connection_id 和 voice socket 一起清掉，本请求方
                # 就不在任何一条投递面上了（self.websocket 可能是更新的窗口）。
                # 那样它会一直等到 15s 超时，而超时发的 end_session 会把刚起来的
                # 会话撕掉。本请求那把 ws 是已知的，直接定向送一份（Codex P2）。
                #
                # 麦克风是不是还归本请求方，要在重跑**之后**判：重跑内部会 await
                # 整个 provider connect（最长 12s），第三个窗口在那期间抢走麦
                # 并把路由 settle 成健康值是可能的，重跑前的快照看不到（Codex P2）。
                # lease 为空不算易主——那是本次 fail-closed 自己 revoke 的，路由
                # 此刻本就是 blocked，照报即可。
                _lease_now = getattr(self, "_voice_lease_connection_id", "")
                _lease_moved = bool(_lease_now) and _lease_now != _lease_at_request
                #
                # lease 已易主时，ack 里的路由只能报 blocked。此刻 _asr_route_mode
                # 是**新持有者**的裁决，可能已经 settle 成 native/independent；
                # 照报会让本请求方（已经被顶掉的那个窗口）看到一条健康路由、
                # 开麦，而服务端的 voice identity 归新持有者，它之后的每一帧
                # PCM 都会被当 stale 丢掉——又一个"开着麦说给空气听"（Codex P2）。
                # 报 blocked 让它 fail-closed 收口：UI 干净、可重试。
                await self.send_session_started(
                    input_mode,
                    request_id=request_id,
                    also_notify=websocket,
                    microphone_route_override="blocked" if _lease_moved else None,
                )
        elif user_initiated and _allow_cross_mode_restart:
            # 跨模式撞车，且这是用户显式启动：典型是 proactive（主动搭话 /
            # greeting）自起的 text 会话还在飞，而用户此刻点了"开始语音对话"
            # （audio）。早期实现静默 return，但用户的 audio 请求是显式意图：
            # 静默丢弃会让前端干等 15s ack 超时，且超时时发的 end_session 还会把
            # 正在建立的 proactive text 会话一并撕掉（proactive 语音也播不出）。
            # 改为：等 in-flight 那次启动落定（_starting_session_count 归 0）后，
            # 递归重入起一个本模式的新会话——它会按 ``_start_session_retire_previous`` 的旧 session 清理逻辑
            # 替换掉刚建好的旧模式会话。不复用 in-flight 的 ack（跨模式复用会按
            # 错模式切 UI，见上）。
            logger.warning("⚠️ Session正在启动中（跨模式），等 in-flight 落定后改起 %s 会话", input_mode)
            # 快照"用户放弃"计数：仅在前端/用户主动 end_session 时递增（见
            # end_session 顶部）。in-flight 真正落定时 count 由其自身 finally 归 0、
            # abandon epoch 不变；而前端 15s 超时发的 end_session 会把 count 清 0
            # 且 abandon epoch +1。只在「count 归 0 且 abandon epoch 未变」时重启——
            # 区分"真落定"与"用户已放弃 + 被 end_session 清零"，避免在 UI 已 reject
            # 后凭空起孤儿会话（Codex P2）。关键：不能用 _audio_stream_epoch——它在
            # in-flight 启动失败的 by_server cleanup 里也会涨，会把用户仍在等待的
            # audio 误判成放弃、回到 15s 干等（CodeRabbit）。
            _abandon_epoch = self._user_session_abandon_epoch
            _waited = 0.0
            while self._starting_session_count > 0 and _waited < _core_facade.CROSS_MODE_RESTART_WAIT_SECONDS:
                await asyncio.sleep(0.05)
                _waited += 0.05
            # 重启前的连接校验，两个条件都要满足：
            #   1) 本请求那把 ws（param）仍连接。in-flight 失败时
            #      _handle_session_start_exception→cleanup() 会把 self.websocket 清成
            #      None，但浏览器连接其实还开着——这种必须能重启，否则用户 audio 干等
            #      15s（Codex P2）。判据用 param ws 的连接态，不看 self.websocket：浏览器
            #      真刷新/断连时这把 param ws 会变 DISCONNECTED。
            #   2) self.websocket 仍是这把或已被清空（None）。若已被换成另一条新连接，
            #      说明发生了重连，别用旧 ws 重启去和新连接打架（Codex P2 stale ws）。
            try:
                _param_ws_connected = (
                    websocket is not None
                    and hasattr(websocket, 'client_state')
                    and websocket.client_state == websocket.client_state.CONNECTED
                )
            except Exception:  # noqa: BLE001
                _param_ws_connected = False
            _self_ws_ok = self.websocket is websocket or self.websocket is None
            if self._starting_session_count != 0:
                logger.warning("⚠️ 跨模式等待 in-flight 启动超时（%.1fs，留余量给重启），放弃改起 %s", _waited, input_mode)
            elif self._user_session_abandon_epoch != _abandon_epoch:
                logger.warning("⚠️ 跨模式等待期间用户主动结束了启动（已放弃），不再改起 %s", input_mode)
            elif not (_param_ws_connected and _self_ws_ok):
                logger.warning("⚠️ 跨模式等待期间 websocket 已断连/被新连接替换，不再改起 %s", input_mode)
            else:
                # in-flight 干净落定且连接仍在：递归重入起目标模式。
                # 先清熔断：用户显式请求按 websocket_router 语义本应清，但当时
                # _starting_session_count>0 让它没清；若 in-flight 失败把熔断跳了闸，
                # 递归会在 start_session 顶部的熔断检查处静默 return、不发 session_failed → 前端干等
                # 15s。这里替它清，让重启真正起来或走正常失败上报（Codex P2）。
                # 重入禁用跨模式重启（_allow_cross_mode_restart=False）把递归深度封到 1，
                # 二次并发撞车回落静默 return 而非无界递归（greptile P2）。guard 检查
                # （_starting_session_count 判定）前无 await，count==0 的判定到重入是原子的。
                self.reset_session_start_circuit()
                await self.start_session(
                    websocket,
                    new,
                    input_mode,
                    user_initiated=True,
                    _allow_cross_mode_restart=False,
                    request_id=request_id,
                    handshake_override=handshake_override,
                    resource_optimization_override=resource_optimization_override,
                )
        else:
            logger.warning("⚠️ Session正在启动中（跨模式重复请求），忽略")
        return True

    def _start_session_seed_turn_language(self):
        """Seed ``user_language`` and the conversation-turn language once.

        Only seeds when ``user_language`` was never set; an existing session
        truth is preserved (late global-cache updates go through the
        ``refresh_global_language`` path instead).
        """
        # 之前每次 start_session 都无脑用 get_global_language() 覆盖 user_language，
        # 想"语言变更即时生效"，但实际效果是把 ws greeting_check 已经推上来的
        # 前端 i18n 真值（例如 Steam=zh / 系统=en 时正确的 'zh-CN'）一律打回错的
        # 全局缓存值（race 失败时的 'en'），让游戏 / proactive / memory 的 prompt
        # 全部回退英文。改为：仅在 user_language 还没被设过时才 seed 一次，已经
        # 有 session 真值就保留——全局缓存晚到的更新由 refresh_global_language
        # 路径独立处理（见 main_routers/config_router.py:steam_language 端点）。
        topic_language_seed = None
        if not getattr(self, 'user_language', None):
            topic_language_seed = normalize_language_code(get_global_language_full(), format='full')
            # Seed the FULL code (e.g. 'zh-TW'), consistent with set_user_language's
            # format='full'; every consumer short-normalizes at its use site. Keeping
            # the Hant variant here is what lets resolve_dialog_slop_lang route a
            # Traditional-Chinese session to the 'zh-TW' slop rules instead of the
            # Simplified ones (a short 'zh' would hide the distinction entirely).
            self.user_language = topic_language_seed
            self._conversation_turn_language = topic_language_seed
        self._set_conversation_turn_language(
            self._conversation_turn_language
            or topic_language_seed
            or self.user_language
        )

    async def _start_session_prepare_runtime(self, websocket, input_mode, new, diag_start):
        """Bind the websocket, reload config and voice routing, and notify the
        frontend that preparation started (silent window begins).

        Returns ``(realtime_config, core_config_snapshot)`` so later phases
        reuse the single config read.
        """
        # 回收残留的热切换资源，防止 main + pending + new-main 叠到 >2 个 session
        await self._cleanup_pending_session_resources()
        await self._reset_preparation_state(clear_main_cache=False)

        logger.info(f"[语音会话诊断] 开始 start_session: input_mode={input_mode}, new={new}")
        logger.info(f"启动新session: input_mode={input_mode}, new={new}")
        self.websocket = websocket
        self.input_mode = input_mode
        self._reset_voice_echo_suppression_cache()

        # 拉起活动 tracker 心跳，让进游戏/娱乐/工作的情境弹窗检测得到（详见
        # _maybe_kick_activity_loop_for_context_prompt）。fire-and-forget，不阻塞会话
        # 启动；仅在用户已显式开启 vision（隐私关）时才 kick，否则直接早退、零成本。
        self._fire_task(self._maybe_kick_activity_loop_for_context_prompt())

        # 立即通知前端系统正在准备（静默期开始）
        await self.send_session_preparing(input_mode)

        # 会话的线路在下面这几行定死、整场不再复议，所以先给仍在飞的区域探测一个
        # 收尾窗口（启动预热的 join 可能在 DNS 解析上过期）。已落定时立即返回，
        # 正常路径零开销；等待也是 offload 的，不占事件循环。
        await self._config_manager.aensure_region_resolved()

        # 重新读取配置以支持热重载
        # core_api_type 从 realtime 配置获取，支持自定义 realtime API 时自动设为 'local'
        # 合并两次同步 IO：core_config.json 只 read 一次，realtime 解析复用同一份快照
        core_config_snapshot = await self._config_manager.aget_core_config()
        realtime_config = await self._config_manager.aget_model_api_config(
            'realtime', core_config=core_config_snapshot
        )
        self.core_api_type = realtime_config.get('api_type', '') or core_config_snapshot.get('CORE_API_TYPE', '')
        self.audio_api_key = core_config_snapshot['AUDIO_API_KEY']

        # 每次启动会话前都清理一次无效 voice_id，避免角色配置残留旧音色导致启动异常
        try:
            cleaned_count, legacy_names = await asyncio.to_thread(self._config_manager.cleanup_invalid_voice_ids)
            if cleaned_count > 0:
                logger.info(f"🧹 start_session 前已清理 {cleaned_count} 个无效 voice_id")
            self._enqueue_voice_migration_notice(legacy_names)
        except Exception as e:
            logger.warning(f"⚠️ start_session 清理无效 voice_id 失败，继续启动会话: {e}")

        # 默认 YUI 卡的免费音色绑定在区域未落定时会主动推迟（绑错了纠正不回来），
        # 而它原本只有两个调用点——保存核心配置与 clear_voice_ids。用户在设置里切到
        # 免费 API 时，保存路径紧接着就调它，此时探测刚起、必然还是未落定，于是推迟；
        # 之后再没有任何路径调它，"等下一轮"永远等不到。这里就是那一轮：紧跟上面的
        # aensure_region_resolved，区域已尽力落定；每场会话都跑一次，幂等（只在默认
        # YUI 卡且 voice_id 为空时写入）；放在下面读 voice_id 之前，绑上本场即生效。
        try:
            await ensure_default_yui_voice_for_free_api(self._config_manager, core_config_snapshot)
        except Exception as e:
            logger.warning(f"⚠️ start_session 绑定默认 YUI 音色失败，继续启动会话: {e}")

        # 重新读取角色配置以获取最新的voice_id（支持角色切换后的音色热更新）
        _, _, _, self.lanlan_basic_config, _, _, _, _, _ = await self._config_manager.aget_character_data()
        old_voice_id = self.voice_id
        self._apply_voice_id_for_route()

        # 如果角色没有设置 voice_id，尝试使用自定义API配置的 TTS_VOICE_ID 作为回退
        if not self.voice_id:
            # core_config 在单次 start_session 内不会变（改它走 save_core_api → end_session），复用顶部 snapshot
            tts_voice_id = core_config_snapshot.get('TTS_VOICE_ID', '')
            # 过滤掉 GPT-SoVITS 禁用时的占位符（格式: __gptsovits_disabled__|...）
            if (
                tts_voice_id
                and not is_gsv_disabled_voice_id(tts_voice_id)
                and (
                    _as_bool(core_config_snapshot.get('ENABLE_CUSTOM_API'), False)
                    or core_config_snapshot.get('GPTSOVITS_ENABLED')
                )
            ):
                self.voice_id = tts_voice_id
                logger.info(f"🔄 使用自定义TTS回退音色: '{self.voice_id}'")
                self._is_free_preset_voice = False

        if old_voice_id != self.voice_id:
            logger.info(f"🔄 voice_id已更新: '{old_voice_id}' -> '{self.voice_id}'")
        if self._is_free_preset_voice:
            logger.info(f"🆓 当前使用免费预设音色: '{self.voice_id}'")

        # 日志输出模型配置（直接从配置读取，避免创建不必要的实例变量）
        _realtime_model = realtime_config.get('model', '')
        _conversation_model = (await self._config_manager.aget_model_api_config(
            'conversation', core_config=core_config_snapshot
        )).get('model', '')
        _vision_model = (await self._config_manager.aget_model_api_config(
            'vision', core_config=core_config_snapshot
        )).get('model', '')
        logger.info(f"📌 已重新加载配置: core_api={self.core_api_type}, realtime_model={_realtime_model}, text_model={_conversation_model}, vision_model={_vision_model}, voice_id={self.voice_id}")
        logger.info(f"[语音会话诊断] 配置加载完成 (耗时: {time.time() - diag_start:.2f}秒)")
        return realtime_config, core_config_snapshot

    async def _start_session_reset_stream_state(self, input_mode, realtime_config,
                                                core_config_snapshot):
        """Reset the TTS/input caches for the new session and resolve
        ``use_tts``."""
        # 重置 TTS 缓存状态。若 TTS worker 已经存活且此前确认 ready，
        # 这里只清空待播文本，不要把 ready 状态抹掉；存活 worker 不会
        # 因为新 text session 再发一次 __ready__，否则赛后一次性文本会
        # 永远停在 pending chunks 里。
        preserve_tts_ready = self._can_preserve_tts_ready_for_session_start()
        async with self.tts_cache_lock:
            self.tts_ready = preserve_tts_ready
            self.tts_pending_chunks.clear()
            # Session replacement invalidates the previous utterance replay ledger.
            # 新会话不得继承上一轮的 TTS 回放文本或结束信号。
            self._reset_tts_replay_state()

        # 重置输入缓存状态
        async with self.input_cache_lock:
            self.session_ready = False
            # 注意：不清空 pending_input_data，因为可能已有数据在缓存中

        self.use_tts = self._resolve_session_use_tts(
            input_mode,
            realtime_config,
            core_config_snapshot,
        )

    async def _start_session_retire_previous(self):
        """Tear down a still-active old session and stop a TTS thread the new
        session will not use."""
        async with self.lock:
            if self.is_active:
                logger.warning("检测到活跃的旧session，正在清理...")
                # 释放锁后清理，避免死锁

        # 如果检测到旧 session，先清理
        if self.is_active:
            # reset_starting_count=False：保留自己递增的 guard，防止 end_session 里
            # 的 _starting_session_count=0 让并发第二次 start_session 穿过，产生孤儿 session。
            await self.end_session(by_server=True, reset_starting_count=False)
            # 等待一小段时间确保资源完全释放
            await asyncio.sleep(0.5)
            logger.info("旧session清理完成")

        # 如果当前不需要TTS，worker 与其绑定的响应 handler 必须成对释放。
        # handler 在启动时会捕获当时的 response queue；只关 worker 会让它
        # 永久阻塞在旧队列，之后小游戏懒启动 TTS 时也无法消费新队列。
        if not self.use_tts:
            if self.tts_thread and self.tts_thread.is_alive():
                logger.info("当前模式不需要TTS，关闭TTS线程")
                try:
                    self.tts_request_queue.put(("__shutdown__", None))  # 通知线程退出
                    await asyncio.to_thread(self.tts_thread.join, 1.0)  # 等待线程结束
                except Exception as e:
                    logger.error(f"关闭TTS线程时出错: {e}")
                finally:
                    self.tts_thread = None
            await self._stop_tts_response_handler()

    async def _start_session_start_tts_if_needed(self):
        """Asynchronously start the TTS process and wait for readiness"""
        if not self.use_tts:
            return True

        # 启动TTS线程
        tts_ready = False
        if self.tts_thread is None or not self.tts_thread.is_alive():
            self._start_tts_thread()

            # 等待TTS进程发送就绪信号（最多等待12秒）
            has_custom_tts = self._has_custom_tts()
            tts_type = "free-preset-TTS" if self._is_free_preset_voice else ("custom-TTS" if has_custom_tts else f"{self.core_api_type}-default-TTS")
            logger.info(f"🎤 TTS进程已启动，等待就绪... (使用: {tts_type})")
            logger.info("[语音会话诊断] 开始等待 TTS 就绪信号 (超时: 12秒)")
            start_time = time.time()
            timeout = 12.0  # 最多等待12秒
            _last_tts_log = 0.0
            while time.time() - start_time < timeout:
                # worker 线程已死亡则无需继续等待
                if not self.tts_thread.is_alive():
                    # 抽干此刻队列：__ready__ 用于决定本次等待结果，
                    # 其他消息（如承载 NO_RETRY 错误码的 __error__）放回队列，
                    # 让稍后启动的 tts_response_handler 处理，避免错误码丢失。
                    _requeue: list = []
                    while True:
                        try:
                            msg = self.tts_response_queue.get_nowait()
                        except Empty:
                            break
                        if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "__ready__":
                            tts_ready = msg[1]
                        else:
                            _requeue.append(msg)
                    for _m in _requeue:
                        self.tts_response_queue.put(_m)
                    if not tts_ready:
                        logger.error("❌ TTS Worker 线程已退出，无法继续等待")
                    break
                remaining = timeout - (time.time() - start_time)
                # 单次阻塞窗口封顶 2 秒，保证 worker 死亡探测与诊断日志能及时触发
                poll_window = min(remaining, 2.0)
                if poll_window <= 0:
                    break
                try:
                    msg = await asyncio.to_thread(
                        self.tts_response_queue.get, True, poll_window
                    )
                except Empty:
                    # 每约2秒输出一次诊断日志，便于定位卡在哪一阶段
                    _elapsed = time.time() - start_time
                    if _elapsed - _last_tts_log >= 2.0:
                        _last_tts_log = _elapsed
                        logger.info(f"[语音会话诊断] TTS 就绪等待中... 已等待 {_elapsed:.1f}秒 / {timeout}秒")
                    continue
                if isinstance(msg, tuple) and len(msg) == 2 and msg[0] == "__ready__":
                    tts_ready = msg[1]
                    if tts_ready:
                        logger.info(f"✅ TTS进程已就绪 (用时: {time.time() - start_time:.2f}秒)")
                    else:
                        logger.error("❌ TTS进程初始化失败")
                    break
                else:
                    # 不是就绪信号，放回队列后退出（与旧行为一致）
                    self.tts_response_queue.put(msg)
                    break

            if not tts_ready:
                if time.time() - start_time >= timeout:
                    logger.warning(f"⚠️ TTS进程就绪信号超时 ({timeout}秒)，继续执行...")
                    logger.warning(f"[语音会话诊断] TTS 在 {timeout} 秒内未就绪，可能为 TTS 服务慢或网络问题")
                else:
                    logger.error("❌ TTS进程初始化失败，但继续执行...")

                # The replacement worker writes readiness to a fresh queue;
                # the handler created below will consume it and flush pending text.
                # 自定义端点启动失败时先切保底，下面的新 handler 会接管新队列。
                if self._activate_configured_tts_fallback("会话启动"):
                    logger.info("🔄 自定义 TTS API 启动失败，已启动既有保底 worker")
        else:
            # TTS线程已存活，复用现有线程；保留上次的就绪状态（避免失败的 worker 被误标为就绪）
            tts_ready = self.tts_ready
            logger.info(f"🎤 TTS线程已在运行，复用现有线程 (ready={tts_ready})")

        # 确保旧的 TTS handler task 已经停止，同时按其绑定 queue 校验缓存所有权。
        if self.tts_handler_task and not self.tts_handler_task.done():
            logger.info("🎧 Cancelling old tts_handler_task...")
        await self._stop_tts_response_handler()

        # 启动新的 TTS handler task
        logger.info(f"🎧 Creating tts_handler_task (response_queue id={id(self.tts_response_queue):#x})")
        self._start_tts_response_handler()

        # 仅在确认为就绪时才标记可发送，避免“假就绪”导致静默
        async with self.tts_cache_lock:
            self.tts_ready = bool(tts_ready)

        # 处理在TTS启动期间可能已经缓存的文本chunk
        if tts_ready:
            await self._flush_tts_pending_chunks()
        else:
            logger.warning("⚠️ TTS未就绪，当前回复将继续缓存，等待后续就绪信号")
        return True

    def _new_dialog_request_kwargs(self) -> dict:
        """Share explicit-locale provenance across initial and hot-swap bootstrap."""
        request_kwargs = {"timeout": 5.0}
        if getattr(self, "_user_language_explicit", False):
            request_kwargs["params"] = {"language": self.user_language}
        elif getattr(self, "_conversation_render_language", None):
            request_kwargs["params"] = {
                "render_language": self._conversation_render_language,
            }
        return request_kwargs

    async def _start_session_fetch_new_dialog(self, lanlan_name, port):
        """Independent task: fetch the /new_dialog response. Kicked off before the gather,
        deliberately avoiding the GIL contention window during TTS worker startup."""
        from utils.internal_http_client import get_internal_http_client
        _mem_client = get_internal_http_client()
        try:
            resp = await _mem_client.get(
                f"http://127.0.0.1:{port}/new_dialog/{lanlan_name}",
                **self._new_dialog_request_kwargs(),
            )
        except httpx.ConnectError:
            raise ConnectionError(f"❌ 记忆服务未启动！请先启动记忆服务 (端口 {port})")
        except httpx.TimeoutException:
            raise ConnectionError(f"❌ 记忆服务响应超时！请检查记忆服务是否正常运行 (端口 {port})")
        except Exception as e:
            raise ConnectionError(f"❌ 记忆服务连接失败: {e} (端口 {port})")
        if not resp.is_success:
            raise ConnectionError(f"❌ 记忆服务返回非2xx状态 {resp.status_code}: {resp.text[:200]}")
        return resp.text

    def _create_offline_vlm_client(
        self,
        *,
        conversation_config: dict,
        vision_config: dict,
        tool_definitions: list,
        max_response_length: int,
        external_tts_enabled: bool,
    ) -> OmniOfflineClient:
        """Build the shared Offline/VLM client used by starts and promotions."""

        session = OmniOfflineClient(
            base_url=conversation_config['base_url'],
            api_key=conversation_config['api_key'],
            model=conversation_config['model'],
            vision_model=vision_config['model'],
            vision_base_url=vision_config['base_url'],
            vision_api_key=vision_config['api_key'],
            provider_type=conversation_config.get('provider_type'),
            vision_provider_type=vision_config.get('provider_type'),
            on_text_delta=self.handle_text_data,
            on_input_transcript=self.handle_text_input_transcript,
            on_output_transcript=self.handle_output_transcript,
            on_connection_error=self.handle_connection_error,
            on_response_done=self.handle_response_complete,
            on_repetition_detected=self.handle_repetition_detected,
            on_response_discarded=self.handle_response_discarded,
            on_status_message=self.send_status,
            max_response_length=max_response_length,
            lanlan_name=self.lanlan_name,
            master_name=self.master_name,
            user_language_provider=lambda: self.user_language,
            on_tool_call=None,
            tool_definitions=tool_definitions,
            enable_long_response_summary=external_tts_enabled,
        )
        session.on_proactive_done = self.handle_proactive_complete
        session.on_thinking_active = self._make_thinking_active_callback(session)
        # 和两个 realtime 构造点同一处理：句柄绑到这个 client 自己，而不是
        # 构造时刻的 self.session。handoff candidate 在被提升前既不是
        # self.session 也不是 pending_session，_sync_tools_to_active_session
        # 扫不到它，它得从出生就认得自己。
        session.on_tool_call = self._make_tool_call_handler(session)
        return session

    async def _create_offline_vlm_handoff_candidate(
        self,
        *,
        cached_turns: list[dict],
        previous_core_url: str,
    ):
        """Connect an Offline VLM without mutating active-session ownership."""

        await self._config_manager.aensure_region_resolved()
        core_config = await self._config_manager.aget_core_config()
        current_core_url = str(core_config.get('CORE_URL') or '')
        if str(previous_core_url or '') != current_core_url:
            logger.warning(
                "[GeoIP] Offline VLM handoff: 区域结论在 Realtime 会话与候选创建之间发生变化"
                "（%s → %s），本场音色可能落到服务端默认",
                previous_core_url,
                current_core_url,
            )
            self._drop_free_voice_on_route_flip(
                previous_core_url,
                current_core_url,
            )
        conversation_config, vision_config = await asyncio.gather(
            self._config_manager.aget_model_api_config(
                'conversation', core_config=core_config,
            ),
            self._config_manager.aget_model_api_config(
                'vision', core_config=core_config,
            ),
        )
        self._register_builtin_tools()
        candidate = self._create_offline_vlm_client(
            conversation_config=conversation_config,
            vision_config=vision_config,
            tool_definitions=self.tool_registry.all(),
            max_response_length=self._get_text_guard_max_length(),
            external_tts_enabled=not core_config.get('DISABLE_TTS', False),
        )
        next_context = self._snapshot_next_session_context_messages()
        try:
            initial_prompt = await self._build_initial_prompt()
            initial_prompt += await self._start_session_fetch_new_dialog(
                self.lanlan_name,
                self.memory_server_port,
            )
            initial_prompt += self._convert_cache_to_str(next_context)
            initial_prompt += self._convert_cache_to_str(cached_turns)
            self._bind_session_lifecycle_callbacks(candidate)
            await candidate.connect(initial_prompt, native_audio=False)
        except BaseException:
            try:
                await candidate.close()
            except Exception:
                pass
            raise
        return candidate, len(next_context)

    async def _handoff_to_offline_vlm_and_submit(
        self,
        turn,
        *,
        expected_session,
        prepared_session,
        operation_is_current,
        cached_turns_before_final: list[dict],
        visual_still_owned=None,
    ) -> bool:
        """Two-phase Realtime -> Offline VLM promotion for one raw-image turn.

        Candidate construction is side-effect free for the active session. The
        old Realtime session is retired only after the Offline VLM connected and
        the independent-ASR route still owns the same turn.
        """

        lock = getattr(self, '_multimodal_handoff_lock', None)
        if lock is None:
            lock = asyncio.Lock()
            self._multimodal_handoff_lock = lock
        async with lock:
            # A normal hot-swap can promote an Offline session after the ASR
            # dispatcher releases its barrier but before this handoff lock is
            # entered. Re-read behind the same swap barrier and prepare that
            # exact session before using the fast path; a naked type check here
            # would race a second promotion and submit into a retired client.
            session_swap_lock = self._core_voice_session_swap_lock
            try:
                await asyncio.wait_for(
                    session_swap_lock.acquire(),
                    timeout=self._core_voice_session_swap_barrier_timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    '[%s] Offline VLM handoff timed out waiting for entry barrier',
                    self.lanlan_name,
                )
                return False
            try:
                current = getattr(self, 'session', None)
                if isinstance(current, OmniOfflineClient):
                    if not operation_is_current():
                        return False
                    # The session captured at ASR prepare already owns the turn:
                    # repeating interruption/handle_new_message would rotate its
                    # speech id twice. Only a hot-swap replacement needs the
                    # preparation that could not run at the original boundary.
                    if current is not prepared_session:
                        prepare = getattr(
                            current,
                            'prepare_external_voice_turn',
                            None,
                        )
                        if callable(prepare):
                            reconnected = await prepare(turn_id=turn.turn_id)
                            if reconnected is True and not await (
                                self._restart_message_handler_after_session_reconnect(
                                    current
                                )
                            ):
                                return False
                        else:
                            interrupt = getattr(
                                current,
                                'handle_interruption',
                                None,
                            )
                            if callable(interrupt):
                                await interrupt()
                        if (
                            not operation_is_current()
                            or self.session is not current
                        ):
                            return False
                        await self.handle_new_message()
                        if (
                            not operation_is_current()
                            or self.session is not current
                        ):
                            return False
                    submit = getattr(current, 'submit_multimodal_turn', None)
                    if not callable(submit):
                        return False
                    self.response_backend = 'offline_vlm'
                    try:
                        await self.ensure_tts_pipeline_alive()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            '[%s] Offline VLM submit TTS retry failed: %s',
                            self.lanlan_name,
                            exc,
                        )
                        return False
                    if (
                        not operation_is_current()
                        or self.session is not current
                    ):
                        return False
                    if visual_still_owned is not None and not visual_still_owned():
                        # 与下面那条慢路径同一判据：这条快速路径同样隔着
                        # prepare_external_voice_turn / handle_interruption /
                        # handle_new_message / ensure_tts_pipeline_alive 几段
                        # await，后继发声可以在其中任意一段拿走帧。丢帧只降级成
                        # 纯文本，话照送。
                        submit_text = getattr(
                            current,
                            'submit_external_voice_turn',
                            None,
                        )
                        if not callable(submit_text):
                            # 这条分支的既有风格是拿不到提交方法就 return False
                            # （不像慢路径那样抛），保持一致。
                            return False
                        logger.info(
                            '[%s] Offline submit lost frame ownership mid-flight; '
                            'submitting turn %s without its frames',
                            self.lanlan_name,
                            turn.turn_id,
                        )
                        delivered = await submit_text(
                            turn.transcript,
                            turn_id=turn.turn_id,
                        )
                        return delivered is not False
                    delivered = await submit(
                        turn.transcript,
                        turn.images,
                        turn_id=turn.turn_id,
                        # 与这批帧一起冻结的采集通道。不传的话离线侧会把屏幕
                        # 和摄像头帧一律标成 "user"。
                        source=turn.source,
                    )
                    return delivered is not False
                if current is None or not operation_is_current():
                    return False
                if current is not expected_session:
                    # A same-conversation normal hot-swap may have promoted a
                    # newer Realtime session after the dispatcher selected its
                    # handoff source. The multimodal user turn has priority:
                    # prepare the replacement behind the barrier and continue
                    # candidate construction from that exact live identity.
                    prepare = getattr(
                        current,
                        'prepare_external_voice_turn',
                        None,
                    )
                    if callable(prepare):
                        reconnected = await prepare(turn_id=turn.turn_id)
                        if reconnected is True and not await (
                            self._restart_message_handler_after_session_reconnect(
                                current
                            )
                        ):
                            return False
                    else:
                        interrupt = getattr(current, 'handle_interruption', None)
                        if callable(interrupt):
                            await interrupt()
                    if (
                        not operation_is_current()
                        or self.session is not current
                    ):
                        return False
                    expected_session = current
            finally:
                session_swap_lock.release()

            # A user-owned multimodal final supersedes speculative archival
            # preparation. Cancel and close that candidate before building a
            # dedicated local replacement; never borrow pending_session's slot.
            await self._reset_preparation_state(clear_main_cache=False)
            await self._cleanup_pending_session_resources()
            if (
                not operation_is_current()
                or self.session is not expected_session
            ):
                return False

            try:
                candidate, next_context_count = (
                    await self._create_offline_vlm_handoff_candidate(
                        cached_turns=cached_turns_before_final,
                        previous_core_url=str(
                            getattr(expected_session, 'base_url', '') or ''
                        ),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    '[%s] Offline VLM handoff preparation failed: %s',
                    self.lanlan_name,
                    exc,
                )
                return False

            promoted = False
            old_listener = self.message_handler_task
            listener_cancel_timed_out = False
            listener_timeout_fail_closed = False
            listener_cancelled_for_handoff = False
            ownership_lost_after_close = False
            try:
                if (
                    not operation_is_current()
                    or self.session is not expected_session
                ):
                    return False
                try:
                    await asyncio.wait_for(
                        session_swap_lock.acquire(),
                        timeout=self._core_voice_session_swap_barrier_timeout_s,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        '[%s] Offline VLM handoff timed out waiting for promotion barrier',
                        self.lanlan_name,
                    )
                    return False
                try:
                    if (
                        not operation_is_current()
                        or self.session is not expected_session
                    ):
                        return False
                    if old_listener and not old_listener.done():
                        old_listener.cancel()
                        listener_cancelled_for_handoff = True
                        try:
                            # 刻意不用 wait_for：它在超时后会取消目标并**继续等**
                            # 取消完成，而 handle_messages 若卡在 recv() 里延迟或
                            # 吞掉 CancelledError，这里就永远不会抛 TimeoutError。
                            # 此刻还握着 _core_voice_session_swap_lock，一旦挂住
                            # 后续所有语音回合和热切换都会跟着卡死；fail-closed
                            # 分支也永远轮不到。asyncio.wait 只等到期，不等取消完成。
                            done, _pending = await asyncio.wait(
                                {old_listener},
                                timeout=getattr(
                                    self,
                                    "_core_voice_listener_cancel_timeout_s",
                                    2.0,
                                ),
                            )
                            if not done:
                                raise asyncio.TimeoutError
                            # 目标已经结束：把它的异常取出来，保持与 wait_for 相同
                            # 的传播行为（取消回声仍按下面那条分支处理）。
                            old_listener.result()
                        except asyncio.CancelledError:
                            current_task = asyncio.current_task()
                            if current_task is not None and current_task.cancelling():
                                raise
                        except asyncio.TimeoutError:
                            logger.error(
                                '[%s] Offline VLM handoff: old listener cancellation timed out',
                                self.lanlan_name,
                            )
                            listener_cancel_timed_out = True
                            # Once cancellation has timed out, the old listener
                            # may still own recv() and cannot be closed in place.
                            # Fail-close only if both active ownership refs still
                            # identify the pair observed before promotion; a
                            # concurrent winner must never be cleared here.
                            async with self.lock:
                                listener_timeout_fail_closed = bool(
                                    self.session is expected_session
                                    and self.message_handler_task is old_listener
                                )
                                if listener_timeout_fail_closed:
                                    self.session = None
                                    self.message_handler_task = None
                                    self.is_active = False
                                    self.session_ready = False

                            stuck_listener = old_listener
                            orphan_session = expected_session

                            async def _reap_handoff_session_after_listener_exit():
                                # 先关会话，再（有界地）等 listener。能走到这条路
                                # 的前提就是 listener 吞掉了取消、停不下来；先等它
                                # 就等于让 WebSocket 永远开着，而那个脱缰的 listener
                                # 会在 Core 已经宣告会话结束之后继续回调。
                                # OmniRealtimeClient.close() 会先同步摘掉 socket，
                                # 所以立刻发起才是止血的那一步。
                                try:
                                    await orphan_session.close()
                                except Exception as reap_err:
                                    logger.debug(
                                        '[%s] Offline VLM handoff: orphan close failed: %s',
                                        self.lanlan_name,
                                        reap_err,
                                    )
                                try:
                                    await asyncio.wait(
                                        {stuck_listener},
                                        timeout=getattr(
                                            self,
                                            "_core_voice_listener_cancel_timeout_s",
                                            2.0,
                                        ),
                                    )
                                except (asyncio.CancelledError, Exception):
                                    pass

                            reaper = asyncio.create_task(
                                _reap_handoff_session_after_listener_exit()
                            )
                            _ORPHAN_SESSION_REAPER_TASKS.add(reaper)
                            reaper.add_done_callback(
                                _ORPHAN_SESSION_REAPER_TASKS.discard
                            )
                        except Exception as exc:
                            logger.debug(
                                '[%s] Offline VLM handoff: old listener exited with error: %s',
                                self.lanlan_name,
                                exc,
                            )
                    if not listener_cancel_timed_out:
                        if (
                            not operation_is_current()
                            or self.session is not expected_session
                        ):
                            # Cancellation retired the receive task, but the
                            # Realtime session itself is still healthy. If this
                            # handoff merely lost turn ownership, restore Core's
                            # listener before abandoning the candidate.
                            if (
                                listener_cancelled_for_handoff
                                and self.session is expected_session
                            ):
                                await self._restart_message_handler_after_session_reconnect(
                                    expected_session
                                )
                            return False
                        try:
                            await expected_session.close()
                        except Exception as exc:
                            logger.warning(
                                '[%s] Offline VLM handoff: old session close failed: %s',
                                self.lanlan_name,
                                exc,
                            )
                        async with self.lock:
                            if self.session is not expected_session:
                                return False
                            if not operation_is_current():
                                # The old session has already crossed its
                                # destructive close boundary, so neither it nor
                                # the stale candidate may remain active.
                                self.session = None
                                self.message_handler_task = None
                                self.is_active = False
                                self.session_ready = False
                                ownership_lost_after_close = True
                            else:
                                self.session = candidate
                                promoted = True
                finally:
                    session_swap_lock.release()

                if listener_cancel_timed_out:
                    if listener_timeout_fail_closed:
                        await self._close_independent_asr(
                            next_route_mode="blocked",
                        )
                        await self.send_session_ended_by_server()
                    return False
                if ownership_lost_after_close:
                    await self._close_independent_asr(
                        next_route_mode="blocked",
                    )
                    await self.send_session_ended_by_server()
                    return False

                self.response_backend = 'offline_vlm'
                # Offline emits text even though microphone ownership remains
                # audio/independent-ASR, so its response always needs the
                # external TTS pipeline.
                self.use_tts = True
                self.message_handler_task = asyncio.create_task(
                    candidate.handle_messages()
                )
                # Promotion is already committed. Settle the context ownership
                # before any later initialization await so a fail-closed turn
                # cannot leave the promoted session paired with stale cache.
                self._consume_next_session_context_messages(next_context_count)
                self.message_cache_for_new_session = []
                self.is_preparing_new_session = False
                self.summary_triggered_time = None
                try:
                    await self.ensure_tts_pipeline_alive()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Keep the promoted Offline session coherent and listened
                    # to, but do not submit a reply that cannot own its TTS
                    # lifecycle. A later turn may retry the TTS pipeline.
                    logger.warning(
                        '[%s] Offline VLM handoff TTS initialization failed: %s',
                        self.lanlan_name,
                        exc,
                    )
                    return False
                try:
                    await self._sync_tools_to_active_session()
                except Exception as exc:
                    logger.warning(
                        '[%s] Offline VLM handoff tool sync failed: %s',
                        self.lanlan_name,
                        exc,
                    )
                # New speech can invalidate the frozen image owner while TTS
                # or tool synchronization is awaiting. The candidate remains
                # the active answer backend, but the superseded turn must never
                # reach it.
                if (
                    not operation_is_current()
                    or self.session is not candidate
                ):
                    return False
                if visual_still_owned is not None and not visual_still_owned():
                    # 所有权是在候选会话连接 / promotion / TTS 起管线 / 工具同步这
                    # 一长串 await 里丢的 —— 这是整条链上最长的窗口，
                    # operation_is_current 只覆盖路由身份，不覆盖它。
                    #
                    # 但丢的是**图**，不是话：会话照常 promote（路由本来就需要
                    # offline），这一轮降级成纯文本送出去。返回 False 会让调用方
                    # 报 ASR_MULTIMODAL_TURN_FAILED 并且用户整句话消失。
                    logger.info(
                        '[%s] Offline VLM handoff lost frame ownership mid-flight; '
                        'submitting turn %s without its frames',
                        self.lanlan_name,
                        turn.turn_id,
                    )
                    submit_text = getattr(
                        candidate,
                        'submit_external_voice_turn',
                        None,
                    )
                    if not callable(submit_text):
                        raise RuntimeError('OFFLINE_EXTERNAL_SUBMIT_UNAVAILABLE')
                    delivered = await submit_text(
                        turn.transcript,
                        turn_id=turn.turn_id,
                    )
                    return delivered is not False
                submit = getattr(candidate, 'submit_multimodal_turn', None)
                if not callable(submit):
                    raise RuntimeError('OFFLINE_MULTIMODAL_SUBMIT_UNAVAILABLE')
                delivered = await submit(
                    turn.transcript,
                    turn.images,
                    turn_id=turn.turn_id,
                    # 同上：帧总线的通道标签跟着帧走。
                    source=turn.source,
                )
                return delivered is not False
            finally:
                if not promoted:
                    try:
                        await candidate.close()
                    except Exception:
                        pass

    async def _start_session_start_llm(self, input_mode, core_config_snapshot,
                                       prepared_realtime_config,
                                       new_dialog_task, mem_start):
        """Asynchronously create and connect the LLM Session.

        Uses connect-then-assign: a local new_session is created and connected
        first.  Only after connect() succeeds is it promoted to self.session.
        On failure the half-initialised session is closed and an exception raised.

        Returns the number of next-session context messages folded into the
        start prompt (consumed after activation), or
        ``_START_LLM_CONCURRENT_ABORTED`` when the CAS loses to a concurrent
        start_session.
        """
        next_context_count = 0
        # 强 CAS 语义：只允许在 self.session 为 None（start_session 已清场）
        # 或已经是自己的 new_session 时赋值。任何其他状态都视为并发落败，
        # 必须关闭本次 new_session，避免覆盖赢家造成孤儿。
        #
        # 反例：若仅对比"入口快照"，当赢家已把 self.session 置为 B、
        # 落败者 A 早退后 guard 被 finally 放开，第三者 C 会把入口快照
        # 记作 B，随后 CAS 通过 B==B 的自反检查覆盖 B，产生新的孤儿。
        guard_max_length = self._get_text_guard_max_length()
        _lang = normalize_language_code(self.user_language, format='short')
        initial_prompt = await self._build_initial_prompt()
        next_session_context_messages = self._snapshot_next_session_context_messages()
        start_prompt_context_owner = object()
        self._mark_pending_context_appends_delivered_in_start_prompt(
            next_session_context_messages,
            owner=start_prompt_context_owner,
        )

        # 等待上面预先发出的 /new_dialog 完成
        try:
            _nd_text = await new_dialog_task
            initial_prompt += (
                _nd_text
                + self._convert_cache_to_str(next_session_context_messages)
                # 按本场的实际形态选措辞：text 模式此前也在念"即将开始用
                # 语音继续对话"——input_mode 的分支要到下面创建 client 时
                # 才出现，这段却是无条件拼的。
                + get_context_summary_ready(_lang, input_mode=input_mode).format(
                    name=self.lanlan_name, master=self.master_name,
                )
            )
            logger.info(f"[语音会话诊断] 记忆上下文获取完成 (耗时: {time.time() - mem_start:.2f}秒)")
        except ConnectionError:
            raise
        except Exception as e:
            raise ConnectionError(f"❌ 记忆服务连接失败: {e} (端口 {self.memory_server_port})")

        logger.info(f"🤖 开始创建 LLM Session (input_mode={input_mode})")
        logger.info("[语音会话诊断] 开始创建 LLM 连接 (realtime/text)...")
        _llm_create_start = time.time()

        # Create into a LOCAL variable — not self.session yet
        new_session = None
        # 在抓快照前先把内置工具的 description 对齐到当前
        # user_language —— __init__ 时 user_language 可能还是 None
        # 走的英文占位，这里 user_language 已经定型了，重新注册
        # 一份覆盖 registry 里的旧描述，再被下面的 snapshot 读走。
        self._register_builtin_tools()
        # Snapshot the registry once per session create so the
        # tools list seen by the wire matches what the registry
        # held at connect time. ``set_tools`` keeps it live for
        # later mutations.
        _initial_tool_defs = self.tool_registry.all()

        # 下面两个分支都会在此刻重读配置并把 base_url 冻进 client（text 分支的
        # OmniOfflineClient / realtime 分支的连接配置）。prepare_runtime 虽已落定
        # 过一次，但上面 await 记忆拉取可达数秒——若那次落定是 Steam 兜底或超时
        # fail-open，权威 IP 结论可能恰在这几秒内落地，这里再给一个收尾窗口，
        # 让冻结用的快照与最终结论一致。已落定时零开销。
        await self._config_manager.aensure_region_resolved()

        if input_mode == 'text':
            # 不复用 prepare_runtime 的快照：上面 await 过 /new_dialog 的记忆拉取
            # （可达数秒），期间用户可能刚在 /core_api 存了新凭证。构造 client 前重新
            # 读一份**新鲜快照**，conversation 与 vision 都从它解析——两次独立的读会
            # 让保存恰好落在中间时拿到撕裂的一对（旧 conversation + 新 vision）。
            _fresh_core_config = await self._config_manager.aget_core_config()
            # 区域可能恰在记忆拉取那几秒里翻转（prepare 的落定是 Steam 兜底/超时、
            # 权威 IP 结论随后到达）：此时 prepare 阶段解析的 voice_id 属于旧区域
            # 目录，而本快照的线路是新区域。realtime 侧有按快照的配对闸门兜底
            # （错配不下发、落服务端默认）；text 的 TTS 直接拿 self.voice_id 合成，
            # 由 _drop_free_voice_on_route_flip 施加同一条 fail-safe（清空免费音色
            # 落服务端默认，本场生效、下场按新区域正常解析）。
            if (str(core_config_snapshot.get('CORE_URL') or '')
                    != str(_fresh_core_config.get('CORE_URL') or '')):
                logger.warning(
                    "[GeoIP] 区域结论在会话准备与连接创建之间发生变化"
                    "（%s → %s），本场音色可能落到服务端默认",
                    core_config_snapshot.get('CORE_URL'), _fresh_core_config.get('CORE_URL'),
                )
                self._drop_free_voice_on_route_flip(
                    core_config_snapshot.get('CORE_URL'), _fresh_core_config.get('CORE_URL'),
                )
            conversation_config = await self._config_manager.aget_model_api_config(
                'conversation', core_config=_fresh_core_config
            )
            vision_config = await self._config_manager.aget_model_api_config(
                'vision', core_config=_fresh_core_config
            )
            new_session = self._create_offline_vlm_client(
                conversation_config=conversation_config,
                vision_config=vision_config,
                tool_definitions=_initial_tool_defs,
                max_response_length=guard_max_length,
                external_tts_enabled=(
                    self.use_tts
                    and not core_config_snapshot.get('DISABLE_TTS', False)
                ),
            )
        else:
            # 同上：await 记忆拉取之后必须重读，不复用 prepare_runtime 的快照
            _prev_realtime_base = str((prepared_realtime_config or {}).get('base_url') or '')
            realtime_config = await self._config_manager.aget_model_api_config('realtime')
            # 区域翻转诊断，与 text 分支对偶；realtime 的音色下发按本快照的
            # base_url 走配对闸门，错配不下发、落服务端默认（fail-safe）。
            if _prev_realtime_base != str(realtime_config.get('base_url') or ''):
                logger.warning(
                    "[GeoIP] 区域结论在会话准备与连接创建之间发生变化"
                    "（%s → %s），本场音色可能落到服务端默认",
                    _prev_realtime_base, realtime_config.get('base_url'),
                )
            nr_enabled = (await _core_facade.aload_global_conversation_settings()).get('noiseReductionEnabled', True)
            new_session = OmniRealtimeClient(
                base_url=realtime_config.get('base_url', ''),
                api_key=realtime_config['api_key'],
                model=realtime_config['model'],
                voice=self._resolve_realtime_voice(realtime_config),
                # 同上：帧抄送的角色归属。
                lanlan_name=self.lanlan_name,
                on_text_delta=self.handle_text_data,
                on_audio_delta=self.handle_audio_data,
                on_audio_done=self.handle_audio_done,
                on_new_message=self.handle_new_message,
                on_sid_rotate=self.rotate_speech_id_for_response_done,
                get_host_turn_id=self.read_current_speech_id,
                on_input_transcript=self.handle_input_transcript,
                on_input_transcript_with_route=self.handle_input_transcript,
                get_input_route_identity=lambda: get_active_game_route_generation_identity(
                    self.lanlan_name
                ),
                on_output_transcript=self.handle_output_transcript,
                on_connection_error=self.handle_connection_error,
                on_response_done=self.handle_response_complete,
                on_silence_timeout=self.handle_silence_timeout,
                on_status_message=self.send_status,
                on_repetition_detected=self.handle_repetition_detected,
                api_type=self.core_api_type,
                on_tool_call=None,
                tool_definitions=_initial_tool_defs,
                livestream_mode=self._is_livestream_active(),
                noise_reduction_enabled=nr_enabled,
                turn_admission_lock=self._voice_proactive_inject_lock,
            )
            # Apply user's noise reduction preference to the AudioProcessor
            if hasattr(new_session, '_audio_processor') and new_session._audio_processor:
                await new_session.set_audio_noise_reduction_enabled(nr_enabled)

        new_session.on_tool_call = self._make_tool_call_handler(new_session)

        # Bind guarded callbacks BEFORE connect — connect() can invoke
        # on_connection_error during the handshake, and without the guard
        # it would run the raw unbound handler and potentially kill the
        # current active session.
        self._bind_session_lifecycle_callbacks(new_session)

        try:
            await new_session.connect(initial_prompt, native_audio=not self.use_tts)
        except Exception:
            try:
                await new_session.close()
            except Exception:
                # Best-effort close of the half-connected session; the connect error re-raises below.
                pass
            raise

        # 强 CAS 提升：仅在 self.session 为 None（已被 end_session 清场）
        # 或已经是自己时才赋值，确保不会覆盖任何已就位的赢家 session。
        concurrent_winner = False
        async with self.lock:
            if self.session is None or self.session is new_session:
                self.session = new_session
                if not self.current_speech_id:
                    self.current_speech_id = str(uuid4())
                next_context_count = len(next_session_context_messages)
            else:
                concurrent_winner = True

        if concurrent_winner:
            self._clear_pending_context_start_prompt_marks(owner=start_prompt_context_owner)
            logger.warning("⚠️ start_llm_session: 检测到并发 start_session 已抢先建立 session，关闭本次 new_session 避免孤儿泄漏")
            try:
                await new_session.close()
            except Exception as _close_err:
                logger.error(f"💥 关闭并发落败的 new_session 失败: {_close_err}")
            # 返回哨兵（而非 raise）以绕开 start_session 的通用 except：后者会调
            # cleanup()（无 expected_session 守卫），反过来拆掉赢家的 session/ws，
            # 还会 +1 session_start_failure_count 并向前端发 SESSION_START_FAILED。
            return _START_LLM_CONCURRENT_ABORTED

        # 关 race 的最后一道闸：构造时拍了一次 registry 快照塞进 client，
        # 但 connect() 期间若有 register_tool / unregister_tool 发生，前面
        # 那次异步 _sync_tools_to_active_session 可能找不到 self.session
        # （它当时还是 None / 旧 session）。这里 self.session 已就位，
        # 重新 sync 一次，让 wire 上的 tools 与 registry 保持最终一致。
        try:
            await self._sync_tools_to_active_session()
        except Exception as _sync_err:
            logger.warning("⚠️ start_llm_session: post-connect tool sync failed: %s", _sync_err)

        logger.info("✅ LLM Session 已连接")
        logger.info(f"[语音会话诊断] LLM 连接并 connect 完成 (耗时: {time.time() - _llm_create_start:.2f}秒)")
        # ``initial_prompt`` contains memory and raw conversation context.
        # Never emit it to stdout or a persistent logger.
        return next_context_count

    async def _start_session_reset_state_for_new(self):
        """Reset per-conversation caches when the caller asked for a brand-new
        dialog (``new=True``)."""
        self.message_cache_for_new_session = []
        self.next_session_context_messages = []
        self.last_time = None
        self.is_preparing_new_session = False
        self.summary_triggered_time = None
        self.initial_cache_snapshot_len = 0
        self.initial_next_session_context_snapshot_len = 0
        # 清空输入缓存（新对话时不需要保留旧的输入）
        async with self.input_cache_lock:
            self.pending_input_data.clear()
            self._clear_pending_context_appends(release_durable_cached=True)

    async def _start_session_activate(
        self,
        input_mode,
        next_context_count,
        diag_start,
        *,
        request_id=None,
        handshake_override=...,
        resource_optimization_override=...,
    ):
        """Post-connect activation: flip the active flags, start the message
        handler, reset the failure circuit, ack the frontend, and open the
        input gate after queued context is drained."""
        async with self.lock:
            self.is_active = True
        self.response_backend = (
            'offline_vlm'
            if isinstance(self.session, OmniOfflineClient)
            else 'realtime'
        )

        # Activity tracker：voice_engaged state 的硬前置就是 voice mode flag。
        # 文本模式置 False 让 voice_engaged 永不触发；语音模式打开后由
        # handle_input_transcript 的 on_voice_rms() 维持 8s 活跃窗口。
        self._activity_tracker.on_voice_mode(input_mode == 'audio')

        self.session_start_time = datetime.now()
        self._session_turn_count = 0

        # 启动消息处理任务
        self.message_handler_task = asyncio.create_task(self.session.handle_messages())

        # Resolve the microphone route before session_started opens the frontend
        # input path. When independent ASR is required, failure is non-fatal to
        # the Core session but keeps microphone input blocked instead of using Omni.
        await self._start_independent_asr_if_enabled(
            input_mode,
            handshake_override=handshake_override,
            resource_optimization_override=resource_optimization_override,
        )

        # 启动成功，重置失败计数器和熔断
        self.session_start_failure_count = 0
        self.session_start_last_failure_time = None
        self._memory_error_retry_after = 0
        self._session_start_circuit_open = False
        if self.is_goodbye_silent():
            self.set_goodbye_silent(False)

        logger.info(f"[语音会话诊断] 即将通知前端 session_started (start_session 总耗时: {time.time() - diag_start:.2f}秒)")
        # 通知前端 session 已成功启动。带上本次 start 的 request_id：别的窗口
        # 若也有 start 在等，它据此认出这条不是回应自己的，从而不会用一条属于
        # 别人的 ack 收口自己的 promise（详见 send_session_started）。
        await self.send_session_started(input_mode, request_id=request_id)

        # 在 queued context 写入 session 前保持输入闸门关闭；否则第一条
        # 缓存/并发用户输入可能抢在上下文前面进入模型。
        async with self.input_cache_lock:
            await self._drain_pending_context_appends_before_ready()
            self.session_ready = True

        # 处理在session启动期间可能已经缓存的输入数据
        await self._flush_pending_input_data()
        self._consume_next_session_context_messages(next_context_count)

        # WebSocket 重连后，投递因断线积压的 agent 任务回调
        if self.pending_agent_callbacks:
            self._fire_task(self.trigger_agent_callbacks())


    async def _background_prepare_pending_session(self):
        """[Hot-swap related] Prewarm the pending session in the background"""

        # 确保旧的 pending session 已释放，防止泄漏到第 3 个实例
        if self.pending_session:
            logger.info("🧹 BG Prep: 清理残留的 pending session 后再创建新的")
            await self._cleanup_pending_session_resources()

        # 2. Create PENDING session components (as before, store in self.pending_connector, self.pending_session)
        try:
            # 与 _start_session_prepare_runtime 对偶：热切换准备出来的会话同样会把
            # 线路定死一整场，所以这里也要给仍在飞的区域探测一个收尾窗口。
            await self._config_manager.aensure_region_resolved()

            # 重新读取配置以支持热重载
            # core_api_type 从 realtime 配置获取，支持自定义 realtime API 时自动设为 'local'
            # 合并两次同步 IO：core_config.json 只 read 一次，realtime 解析复用同一份快照
            core_config_snapshot = await self._config_manager.aget_core_config()
            realtime_config = await self._config_manager.aget_model_api_config(
                'realtime', core_config=core_config_snapshot
            )
            self.core_api_type = realtime_config.get('api_type', '') or core_config_snapshot.get('CORE_API_TYPE', '')
            self.audio_api_key = core_config_snapshot['AUDIO_API_KEY']

            # 热切换准备时同样清理无效 voice_id，防止旧版本 voice 残留进入热切换流程
            try:
                cleaned_count, legacy_names = await asyncio.to_thread(self._config_manager.cleanup_invalid_voice_ids)
                if cleaned_count > 0:
                    logger.info(f"🧹 热切换准备: 已清理 {cleaned_count} 个无效 voice_id")
                self._enqueue_voice_migration_notice(legacy_names)
            except Exception as e:
                logger.warning(f"⚠️ 热切换准备: 清理无效 voice_id 失败，继续准备会话: {e}")

            # 与 _start_session_prepare_runtime 对偶：补上被区域未落定推迟的默认 YUI
            # 音色绑定（那次推迟没有其它路径会回来补）。
            try:
                await ensure_default_yui_voice_for_free_api(self._config_manager, core_config_snapshot)
            except Exception as e:
                logger.warning(f"⚠️ 热切换准备: 绑定默认 YUI 音色失败，继续准备会话: {e}")

            # 重新读取角色配置以获取最新的voice_id（支持角色切换后的音色热更新）
            _, _, _, self.lanlan_basic_config, _, _, _, _, _ = await self._config_manager.aget_character_data()
            old_voice_id = self.voice_id
            self._apply_voice_id_for_route()

            # 如果角色没有设置 voice_id，尝试使用自定义API配置的 TTS_VOICE_ID 作为回退
            if not self.voice_id:
                # 复用本次热切换准备顶部的 snapshot（save_core_api 会 end_session 才能改 core_config）
                tts_voice_id = core_config_snapshot.get('TTS_VOICE_ID', '')
                # 过滤掉 GPT-SoVITS 禁用时的占位符（格式: __gptsovits_disabled__|...）
                if (
                    tts_voice_id
                    and not is_gsv_disabled_voice_id(tts_voice_id)
                    and (
                        _as_bool(core_config_snapshot.get('ENABLE_CUSTOM_API'), False)
                        or core_config_snapshot.get('GPTSOVITS_ENABLED')
                    )
                ):
                    self.voice_id = tts_voice_id
                    logger.info(f"🔄 热切换准备: 使用自定义TTS回退音色: '{self.voice_id}'")
                    self._is_free_preset_voice = False
            
            if old_voice_id != self.voice_id:
                logger.info(f"🔄 热切换准备: voice_id已更新: '{old_voice_id}' -> '{self.voice_id}'")

            pending_offline_vlm = (
                self.input_mode == 'text'
                or getattr(self, 'response_backend', 'realtime') == 'offline_vlm'
            )
            self.pending_use_tts = self._resolve_session_use_tts(
                'text' if pending_offline_vlm else self.input_mode,
                realtime_config,
                core_config_snapshot,
                log_prefix="热切换准备: ",
            )
            
            # 根据input_mode创建对应类型的pending session
            # 复用 main session 的 ToolRegistry 状态（registry 是 manager 级，
            # 跨 session 持久），保证热切换前后工具集合保持一致。
            # 热切换可能跨语言（用户切了 user_language 后再热切换猫娘），
            # 抓快照前 refresh 一下内置工具的 description。
            self._register_builtin_tools()
            _pending_tool_defs = self.tool_registry.all()
            if pending_offline_vlm:
                # 文本模式：使用 OmniOfflineClient
                # 与主会话构造点对偶：顶部快照与此处之间隔着角色数据读取等 await，
                # 故重新读一份新鲜快照，并让 conversation / vision 共用它，避免撕裂
                _fresh_core_config = await self._config_manager.aget_core_config()
                # 与主会话构造点对偶：区域可能恰在上面的 cleanup / 角色读取等
                # await 期间翻转（顶部落定是 Steam 兜底时不落 _region_cache），
                # self.voice_id 解析自旧区域目录——同一条 fail-safe：清空免费
                # 音色落服务端默认，promotion 后的 TTS 不拿旧区域 ID 去新端点。
                if (str(core_config_snapshot.get('CORE_URL') or '')
                        != str(_fresh_core_config.get('CORE_URL') or '')):
                    logger.warning(
                        "[GeoIP] 热切换准备: 区域结论在准备与连接创建之间发生变化"
                        "（%s → %s），本场音色可能落到服务端默认",
                        core_config_snapshot.get('CORE_URL'), _fresh_core_config.get('CORE_URL'),
                    )
                    self._drop_free_voice_on_route_flip(
                        core_config_snapshot.get('CORE_URL'), _fresh_core_config.get('CORE_URL'),
                    )
                conversation_config = await self._config_manager.aget_model_api_config(
                    'conversation', core_config=_fresh_core_config
                )
                vision_config = await self._config_manager.aget_model_api_config(
                    'vision', core_config=_fresh_core_config
                )
                guard_max_length = self._get_text_guard_max_length()
                self.pending_session = self._create_offline_vlm_client(
                    conversation_config=conversation_config,
                    vision_config=vision_config,
                    tool_definitions=_pending_tool_defs,
                    max_response_length=guard_max_length,
                    external_tts_enabled=(
                        self.pending_use_tts
                        and not core_config_snapshot.get('DISABLE_TTS', False)
                    ),
                )
                logger.info("🔄 热切换准备: 创建文本模式 OmniOfflineClient")
            else:
                # 语音模式：使用 OmniRealtimeClient
                # 同上：不复用顶部快照
                realtime_config = await self._config_manager.aget_model_api_config('realtime')
                nr_enabled = (await _core_facade.aload_global_conversation_settings()).get('noiseReductionEnabled', True)
                self.pending_session = OmniRealtimeClient(
                    base_url=realtime_config.get('base_url', ''),
                    api_key=realtime_config['api_key'],
                    model=realtime_config['model'],
                    voice=self._resolve_realtime_voice(realtime_config),
                    # 帧抄送要能归到角色：多角色同时开实时会话时，frames/all
                    # 是共享的，没有这个名字插件分不出哪一帧属于谁（离线侧一直
                    # 是带名字的，realtime 这侧此前恒为 None）。
                    lanlan_name=self.lanlan_name,
                    on_text_delta=self.handle_text_data,
                    on_audio_delta=self.handle_audio_data,
                    on_audio_done=self.handle_audio_done,
                    on_new_message=self.handle_new_message,
                    on_sid_rotate=self.rotate_speech_id_for_response_done,
                    get_host_turn_id=self.read_current_speech_id,
                    on_input_transcript=self.handle_input_transcript,
                    on_input_transcript_with_route=self.handle_input_transcript,
                    get_input_route_identity=lambda: get_active_game_route_generation_identity(
                        self.lanlan_name
                    ),
                    on_output_transcript=self.handle_output_transcript,
                    on_connection_error=self.handle_connection_error,
                    on_response_done=self.handle_response_complete,
                    on_silence_timeout=self.handle_silence_timeout,
                    on_status_message=self.send_status,
                    on_repetition_detected=self.handle_repetition_detected,
                    api_type=self.core_api_type,
                    on_tool_call=None,
                    tool_definitions=_pending_tool_defs,
                    livestream_mode=self._is_livestream_active(),
                    noise_reduction_enabled=nr_enabled,
                    turn_admission_lock=self._voice_proactive_inject_lock,
                )
                # Apply user's noise reduction preference to the AudioProcessor
                if hasattr(self.pending_session, '_audio_processor') and self.pending_session._audio_processor:
                    await self.pending_session.set_audio_noise_reduction_enabled(nr_enabled)
                logger.info("🔄 热切换准备: 创建语音模式 OmniRealtimeClient")
            
            self.pending_session.on_tool_call = self._make_tool_call_handler(
                self.pending_session
            )

            initial_prompt = await self._build_initial_prompt()
            next_session_context_messages = list(getattr(self, "next_session_context_messages", []) or [])
            self.initial_next_session_context_snapshot_len = len(next_session_context_messages)
            self.initial_cache_snapshot_len = len(self.message_cache_for_new_session)
            from utils.internal_http_client import get_internal_http_client
            _hs_client = get_internal_http_client()
            try:
                resp = await _hs_client.get(
                    f"http://127.0.0.1:{self.memory_server_port}/new_dialog/{self.lanlan_name}",
                    **self._new_dialog_request_kwargs(),
                )
            except httpx.ConnectError:
                raise ConnectionError(f"❌ 记忆服务未启动！请先启动记忆服务 (端口 {self.memory_server_port})")
            except httpx.TimeoutException:
                raise ConnectionError(f"❌ 记忆服务响应超时！请检查记忆服务是否正常运行 (端口 {self.memory_server_port})")
            if not resp.is_success:
                raise ConnectionError(f"❌ 记忆服务热切换时返回非2xx状态 {resp.status_code}: {resp.text[:200]}")
            initial_prompt += (
                resp.text
                + self._convert_cache_to_str(next_session_context_messages)
                + self._convert_cache_to_str(self.message_cache_for_new_session)
            )
            self._bind_session_lifecycle_callbacks(self.pending_session)
            await self.pending_session.connect(initial_prompt, native_audio=not self.pending_use_tts)

            # 同主 session 路径：热切换的 pending_session 也要在 connect 后
            # 补一次 sync，覆盖 connect 期间发生的 register/unregister race。
            try:
                await self._sync_tools_to_active_session()
            except Exception as _sync_err:
                logger.warning("⚠️ pending_session post-connect tool sync failed: %s", _sync_err)

            if self.pending_session_warmed_up_event:
                self.pending_session_warmed_up_event.set()

        except asyncio.CancelledError:
            logger.error("💥 BG Prep Stage 1: Task cancelled.")
            await self._cleanup_pending_session_resources()
            # Do not set warmed_up_event here if cancelled.
        except Exception as e:
            # 记录HTTP详细错误信息（如503等）
            error_detail = str(e)
            if hasattr(e, 'status_code'):
                error_detail = f"HTTP {e.status_code}: {e}"
            if hasattr(e, 'body'):
                error_detail += f" | Body: {e.body}"
            logger.error(f"💥 BG Prep Stage 1: Error: {error_detail}", exc_info=True)
            await self._cleanup_pending_session_resources()
            # Do not set warmed_up_event on error.
        finally:
            # Ensure this task variable is cleared so it's known to be done
            if self.background_preparation_task and self.background_preparation_task.done():
                self.background_preparation_task = None

    async def _trigger_immediate_preparation_for_extra(self):
        """When extra prompts need injecting and preparation hasn't started yet, start preparing immediately and schedule the renew logic."""
        try:
            if not self.is_preparing_new_session:
                logger.info("Extra Reply: Triggering preparation due to pending extra reply.")
                self.is_preparing_new_session = True
                self.summary_triggered_time = datetime.now()
                self.message_cache_for_new_session = []
                self.initial_cache_snapshot_len = 0
                self.initial_next_session_context_snapshot_len = 0
                # 立即启动后台预热，不等待10秒
                self.pending_session_warmed_up_event = asyncio.Event()
                if not self.background_preparation_task or self.background_preparation_task.done():
                    self.background_preparation_task = asyncio.create_task(self._background_prepare_pending_session())
        except Exception as e:
            logger.error(f"💥 Extra Reply: preparation trigger error: {e}")

    @staticmethod
    def _swap_session_is_dead(session) -> bool:
        """[Hot-swap related] Closed/unusable session detection for the swap
        abort handlers: a dead session must be fail-closed instead of getting
        a listener restarted on it. Realtime clients clear ``ws`` on close();
        offline (text) clients clear ``llm`` on close().
        """
        if not session:
            return False
        if isinstance(session, OmniRealtimeClient):
            return not session.ws
        if isinstance(session, OmniOfflineClient):
            return session.llm is None
        return False

    def _restore_undelivered_swap_extras(self, injected_extras: list, cb_backed_ids: set = None) -> None:
        """[Hot-swap related] Return removed-but-undelivered extras to the queue head.

        ``_perform_final_swap_sequence`` keeps ``pending_extra_replies``
        untouched through the prime window and removes the budget-selected
        entries only at promote success — so every pre-promote abort keeps the
        queue intact and needs no restore. The exits where removal has already
        happened but the promoted session dies before speaking (post-promote
        ws-invalid fail-close, post-promote external cancellation) put the
        removed entries back at the queue head so the next hot-swap delivers
        them, mirroring the ``_deferred`` entries that stay queued across
        aborts.

        ``cb_backed_ids``: delivery ids whose paired callback was still in
        ``pending_agent_callbacks`` at removal time. An id in this set whose
        callback is GONE by restore time was consumed inside the window (a
        successful voice delivery prunes both queues; the extras half no-ops
        on checked-out entries) — restoring it would announce the callback a
        second time on the next hot-swap, so it is dropped.
        """
        if not injected_extras:
            return
        try:
            from config import AGENT_CALLBACK_QUEUE_MAX_ITEMS
            # 窗口期内配对 callback 可能已被 retract：retraction 清扫只作用于
            # 当时还在队列里的镜像条目，被摘走的 _selected 逃过了那一轮——
            # 塞回前按 pending_agent_callbacks 里仍带 retracted 标记的 id 补删。
            retracted_ids = {
                cb.get("_callback_delivery_id")
                for cb in (getattr(self, "pending_agent_callbacks", None) or [])
                if isinstance(cb, dict)
                and cb.get(DELIVERY_RETRACTED_KEY)
                and cb.get("_callback_delivery_id")
            }
            queued_ids = {
                extra.get("_callback_delivery_id")
                for extra in self.pending_extra_replies
                if isinstance(extra, dict) and extra.get("_callback_delivery_id")
            }
            # topic hook extras 一律不塞回：主线 retract 流（语音封锁清扫/ack 超时
            # 撤回）是"打标记后同步 purge"，窗口期内发生时 marker 已消失、上面的
            # retracted_ids 看不见，塞回会绕过 _drop_pending_topic_hooks_for_voice
            # 的清扫在语音里复活被禁止的 hook；且 TopicHookPool 有自己的
            # ack/retry 簿记，丢掉 extra 不会丢内容。
            # ⚠️前提：retraction 目前是 topic-hook 专属机制——DELIVERY_RETRACTED_KEY
            # 的全部设置点都 gate 在 channel=="topic_hook" 或只从 topic 投递流可达
            # （proactive.py 三处 + proactive_delivery.retract 唯一调用链
            # topic/delivery._remove_callback_from_manager），所以排除 topic 即
            # 杜绝"窗口期内被撤回的条目经此复活"。若未来引入非 topic 的
            # retraction，这里必须改为可查询的撤回 ledger 而非 marker 复查。
            # 窗口期消费检测：移除时有配对 cb、现在 cb 没了 ⇒ 被语音投递等
            # 消费掉了，不塞回。extras-only 条目（移除时就无 cb）唯一投递者是
            # hot-swap 本身，窗口内不可能被投递，放行。
            current_cb_ids = {
                cb.get("_callback_delivery_id")
                for cb in (getattr(self, "pending_agent_callbacks", None) or [])
                if isinstance(cb, dict) and cb.get("_callback_delivery_id")
            }
            consumed_ids = (cb_backed_ids or set()) - current_cb_ids
            restored = [
                extra for extra in injected_extras
                if not isinstance(extra, dict)
                or (extra.get("source_kind") != "topic"
                    and extra.get("_callback_delivery_id") not in retracted_ids
                    and extra.get("_callback_delivery_id") not in queued_ids
                    and extra.get("_callback_delivery_id") not in consumed_ids)
            ]
            if not restored:
                return
            # 塞回队首保持原始相对顺序（_selected 本来就排在 _deferred 之前）。
            self.pending_extra_replies = restored + self.pending_extra_replies
            # flood guard 与 enqueue_agent_callback 对齐：drop-oldest。
            if len(self.pending_extra_replies) > AGENT_CALLBACK_QUEUE_MAX_ITEMS:
                self.pending_extra_replies = self.pending_extra_replies[-AGENT_CALLBACK_QUEUE_MAX_ITEMS:]
            logger.info(
                "Final Swap Sequence: %d undelivered extra replies restored to queue head after aborted swap",
                len(restored),
            )
        except Exception as e:
            # 塞回是尽力而为：绝不能让队列簿记反过来打断中止清理流程。
            logger.warning(f"Final Swap Sequence: failed to restore undelivered extras: {e}")

    def _select_passive_callbacks_for_swap_prime(
        self,
        extras_selected: list = None,
        *,
        require_media_ready: bool = True,
        render: bool = True,
    ) -> tuple:
        """[Hot-swap related] Pick queued passive callbacks to ride the swap prime.

        Passive (``delivery_mode="passive"`` / ai_behavior="read") callbacks
        never mirror into ``pending_extra_replies`` (PR #2469), so a pure
        voice session has no user-turn drain to carry them. Their delivery
        point is HERE: the next NATURALLY-occurring hot swap hands them to
        the new session as background context. Non-Gemini providers get a
        DEDICATED ``prime_context(skipped=True)`` call (instructions channel,
        no turn) AFTER the announce prime — physically separate, so read
        content can never become part of the user turn that triggers a
        spoken response. Gemini has no instructions channel and a global,
        response-unscoped skip guard, so one swap may carry at most ONE
        prime turn: on a no-extras swap the passive block merges into the
        single ``skipped=True`` prime; a swap that also announces extras
        skips the ride-along and leaves passive queued for the next swap.

        Deliberate trade-off (owner decision): an ACTIVE voice session gets
        no mid-session context push for passive cues — they wait for the
        next natural swap, even if that is several user turns away. Pushing
        into a live session would reopen the interruption/session-churn
        surface this design exists to close.

        Returns ``(selected, rendered_text)``. Selection shares the
        ``AGENT_CALLBACK_TOTAL_MAX_TOKENS`` budget with the already-selected
        proactive extras: ``extras_selected`` is prepended to the candidate
        list before the budget walk, so passive only takes what the extras
        left over. Over-budget passive callbacks stay queued for the next
        swap (same semantics as the extras ``_deferred``).

        The queue is NOT drained here — removal is deferred to promote
        success via :meth:`_remove_swap_delivered_passive_cbs`. Selected
        entries are atomically marked provider-owned before this method
        returns, so enqueue coalescing, text drain, staleness sweeps, and the
        flood guard cannot retract them during the prime await. Every
        pre-promote exit releases that claim in the swap sequence's ``finally``
        block. Topic-hook snapshots are excluded: they have their own ack/retry
        lifecycle and delivery gates that this path must not bypass.
        """
        try:
            candidates = [
                cb
                for cb in (
                    getattr(self, "pending_agent_callbacks", []) or []
                )
                if isinstance(cb, dict)
                and cb.get("delivery_mode") == "passive"
                and not cb.get(DELIVERY_RETRACTED_KEY)
                and not cb.get(SWAP_PRIME_DELIVERY_CLAIM_KEY)
                and cb.get("channel") != "topic_hook"
                and (
                    not require_media_ready
                    or self._callback_media_ready_for_session(
                        cb,
                        getattr(self, "pending_session", None),
                    )
                )
            ]
            if not candidates:
                return [], ""
            # Same staleness hygiene as the text-mode drain: a same-key
            # superseded cue must not deliver, and gets purged from the live
            # queue (ack False) rather than lingering until the next drain.
            self._retract_stale_coalesced(candidates)
            self._purge_undeliverable_callbacks()
            candidates = [
                cb for cb in candidates if not cb.get(DELIVERY_RETRACTED_KEY)
            ]
            if not candidates:
                return [], ""
            from config import AGENT_CALLBACK_TOTAL_MAX_TOKENS
            _extras = list(extras_selected or [])
            selected_all, _ = _select_callbacks_within_token_budget(
                _extras + candidates, AGENT_CALLBACK_TOTAL_MAX_TOKENS
            )
            selected = selected_all[len(_extras):]
            if not selected:
                return [], ""
            rendered = ""
            if render:
                # 与 proactive 三条投递路径同口径：字形留到渲染函数再归一化。
                _lang = normalize_language_code(self.user_language, format='full')
                rendered = _build_callback_instruction(
                    selected,
                    lang=_lang,
                    lanlan_name=getattr(self, "lanlan_name", "") or "",
                    master_name=getattr(self, "master_name", "") or "",
                    passive=True,
                )
            # No await exists between selection and this ownership claim. From
            # here until promote/abort, every queue mutation sees the same
            # provider-owned boundary as the swap sequence.
            for cb in selected:
                cb[SWAP_PRIME_DELIVERY_CLAIM_KEY] = True
            return selected, rendered
        except Exception as e:
            # 选取/渲染失败绝不能打断 swap：这批 passive 留在队列等下一轮。
            logger.warning(f"Final Swap Sequence: passive callback selection failed: {e}")
            return [], ""

    def _render_claimed_passive_callbacks_for_swap_prime(
        self,
        selected: list,
    ) -> tuple:
        """Render the media-ready subset of one pre-staging swap snapshot."""
        ready = []
        for callback in selected:
            if callback.get(PASSIVE_MEDIA_BUDGET_DEFERRED_KEY):
                # 本轮图片预算没轮到它。STOP 而不是 skip，也不是只挡带图的那些：
                # split_callbacks_by_image_budget 是**严格 FIFO**，预算耗尽之后
                # 整条后缀（包括其中的纯文本 callback）都被标上这个键。而
                # _callback_media_ready_for_session 对纯文本 callback 恒为真，
                # 只按它过滤的话，排在被延后的带图 cue **后面**的那条纯文本会被
                # 这次 swap 抢先投出去并摘出队列，模型听到的顺序就反了。
                # 与 drain_agent_callbacks_for_llm 同一判据（那边是第一个消费点，
                # 这里是第二个）。
                break
            if callback.get(DELIVERY_RETRACTED_KEY):
                continue
            if not self._callback_media_ready_for_session(
                callback,
                getattr(self, "pending_session", None),
            ):
                # 媒体没就绪就 STOP，**不分**瞬时还是终局。跳过它去渲染更晚那条，
                # promote 之后更晚那条被摘出队列、它自己还留着，模型听到的顺序就
                # 反了——与预算延后那条同一个 FIFO 论证。
                #
                # 终局失败也 STOP 不会把它永久堵死：drain 那个消费点对终局失败走
                # best-effort（文字照投、图这一轮带不上），下一个用户回合就把它连
                # 同后面的一起放行了。所以这里只是"这次 swap 不抢跑"，不是"永远
                # 不投"。
                #
                # 也不在这里按瞬时/终局分类：staging 的异常分支刻意不打
                # PASSIVE_MEDIA_TRANSIENT_KEY（drain 对它的既定处置是文字照投），
                # 拿那个标记当判据会把异常误判成终局。既然两种都要 STOP，就不需要
                # 分类。
                break
            ready.append(callback)
        ready_obj_ids = {id(callback) for callback in ready}
        self._release_swap_prime_passive_claims(
            [callback for callback in selected if id(callback) not in ready_obj_ids]
        )
        if not ready:
            return [], ""
        _lang = normalize_language_code(self.user_language, format='full')
        return ready, _build_callback_instruction(
            ready,
            lang=_lang,
            lanlan_name=getattr(self, "lanlan_name", "") or "",
            master_name=getattr(self, "master_name", "") or "",
            passive=True,
        )

    @staticmethod
    def _release_swap_prime_passive_claims(selected: list) -> None:
        """Release provider ownership after promote or any abort exit."""
        for cb in selected or []:
            if isinstance(cb, dict):
                cb.pop(SWAP_PRIME_DELIVERY_CLAIM_KEY, None)

    def _remove_swap_delivered_passive_cbs(self, selected: list) -> list:
        """[Hot-swap related] Dequeue prime-injected passive callbacks at
        promote success; returns the actually-removed subset (ack'd True).

        Identity-based removal, same as the extras counterpart: entries a
        concurrent path consumed inside the prime→promote window (text-turn
        drain, retraction purge, flood cap) no-op here instead of deleting a
        re-queued same-id newcomer.
        """
        if not selected:
            return []
        self._release_swap_prime_passive_claims(selected)
        try:
            selected_obj_ids = {id(cb) for cb in selected}
            removed = [
                cb for cb in self.pending_agent_callbacks
                if id(cb) in selected_obj_ids
            ]
            if not removed:
                return []
            self.pending_agent_callbacks = [
                cb for cb in self.pending_agent_callbacks
                if id(cb) not in selected_obj_ids
            ]
            for cb in removed:
                resolve_callback_delivery_ack(cb, True)
            return removed
        except Exception as e:
            logger.warning(f"Final Swap Sequence: passive callback dequeue failed: {e}")
            return []

    def _restore_undelivered_swap_passive_cbs(self, removed_cbs: list) -> None:
        """[Hot-swap related] Mirror of :meth:`_restore_undelivered_swap_extras`
        for prime-injected passive callbacks.

        Only the post-promote death exits call this (promoted session dies
        before its next reply, so the primed context is lost): put the
        removed entries back at the queue head for the next swap. Staleness
        is deliberately NOT re-checked here — every delivery point
        (text-turn drain / next swap's selection) re-runs
        ``_retract_stale_coalesced`` anyway. Topic-hook snapshots and entries
        whose delivery id is already back in the queue (a newer re-enqueue
        won) are skipped, matching the extras restore guards.
        """
        if not removed_cbs:
            return
        try:
            from config import AGENT_CALLBACK_QUEUE_MAX_ITEMS
            queued_ids = {
                cb.get("_callback_delivery_id")
                for cb in (self.pending_agent_callbacks or [])
                if isinstance(cb, dict) and cb.get("_callback_delivery_id")
            }
            restored = [
                cb for cb in removed_cbs
                if isinstance(cb, dict)
                and not cb.get(DELIVERY_RETRACTED_KEY)
                and cb.get("channel") != "topic_hook"
                and cb.get("_callback_delivery_id") not in queued_ids
            ]
            if not restored:
                return
            self.pending_agent_callbacks = restored + self.pending_agent_callbacks
            self._enforce_agent_callback_queue_limit(
                AGENT_CALLBACK_QUEUE_MAX_ITEMS
            )
            logger.info(
                "Final Swap Sequence: %d undelivered passive callback(s) restored to queue head after aborted swap",
                len(restored),
            )
        except Exception as e:
            # 塞回是尽力而为：绝不能让队列簿记反过来打断中止清理流程。
            logger.warning(f"Final Swap Sequence: failed to restore undelivered passive callbacks: {e}")

    async def _perform_final_swap_sequence(self):
        """[Hot-swap related] Perform the final swap sequence"""
        logger.info("Final Swap Sequence: Starting...")
        if not self.pending_session:
            logger.error("💥 Final Swap Sequence: Pending session not found. Aborting swap.")
            await self._reset_preparation_state(clear_main_cache=True)  # Reset all flags and cache for clean restart
            self.is_hot_swap_imminent = False
            return
        
        # 检查pending_session的websocket是否有效
        if isinstance(self.pending_session, OmniRealtimeClient):
            if not hasattr(self.pending_session, 'ws') or not self.pending_session.ws:
                logger.error("💥 Final Swap Sequence: Pending session的WebSocket已关闭，放弃swap操作")
                await self._cleanup_pending_session_resources()
                await self._reset_preparation_state(clear_main_cache=True)
                self.is_hot_swap_imminent = False
                return
            
            # 检查是否发生致命错误
            if hasattr(self.pending_session, '_fatal_error_occurred') and self.pending_session._fatal_error_occurred:
                logger.error("💥 Final Swap Sequence: Pending session已发生致命错误，放弃swap操作")
                await self._cleanup_pending_session_resources()
                await self._reset_preparation_state(clear_main_cache=True)
                self.is_hot_swap_imminent = False
                return

        try:
            new_session = None  # 提前初始化，确保 except 块安全访问（实际赋值在 PERFORM ACTUAL HOT SWAP 段）
            old_listener_cancel_timed_out = False  # 旧 listener 取消超时标志，供 except 块做 fail-close 决策
            # 已注入 pending_session 的 _selected 条目引用（队列原地保留，promote
            # 成功时才移除）；_removed_extras 是 promote 时真正移除掉的子集，仅
            # 供"移除已发生且被注入会话已死"的出口（ws 失效 fail-close、promote
            # 后外部取消）塞回。其余中止出口队列本来就没动，无需恢复。
            # _removed_cb_backed_ids：移除时配对 callback 仍在 pending_agent_callbacks
            # 的 delivery_id——restore 用它识别"窗口期内被语音投递消费"的条目。
            _prime_selected_extras: list = []
            _removed_extras: list = []
            _removed_cb_backed_ids: set = set()
            # Passive callbacks riding this swap (voice pending session only).
            # Same deferred-removal bookkeeping as _prime_selected_extras:
            # queue untouched until promote succeeds. Injection goes through
            # its OWN prime_context(skipped=True) call below — never merged
            # into the announce prime text.
            _prime_selected_passive_cbs: list = []
            _removed_passive_cbs: list = []
            _passive_sel: list = []
            _passive_swap_text = ""
            _extras_for_budget: list = []
            _passive_media_outcome: dict[str, bool] | None = None

            def _abort_if_passive_claim_retracted(stage: str) -> None:
                if any(cb.get(DELIVERY_RETRACTED_KEY)
                       for cb in _prime_selected_passive_cbs):
                    logger.info(
                        "Final Swap Sequence: passive callback retracted %s; abandoning pending session",
                        stage,
                    )
                    raise asyncio.CancelledError()

            async def _abort_if_native_prefix_lost_its_text(
                outcome: dict | None,
                rendered_text: str,
                stage: str,
            ) -> bool:
                if rendered_text or not (
                    outcome and outcome.get("native_prefix_committed")
                ):
                    return False
                logger.warning(
                    "Final Swap Sequence: passive native media lost its "
                    "callback text %s; abandoning pending session",
                    stage,
                )
                await self._cleanup_pending_session_resources()
                await self._reset_preparation_state(clear_main_cache=True)
                self.is_hot_swap_imminent = False
                return True

            next_session_context_messages = getattr(self, "next_session_context_messages", []) or []
            incremental_next_session_context = next_session_context_messages[
                self.initial_next_session_context_snapshot_len:
            ]
            incremental_cache = (
                list(incremental_next_session_context)
                + self.message_cache_for_new_session[self.initial_cache_snapshot_len:]
            )
            # 1. Send incremental cache (or a heartbeat) to PENDING session for its *second* ignored response
            if incremental_cache:
                final_prime_text = self._convert_cache_to_str(incremental_cache)
            else:  # Ensure session cycles a turn even if no incremental cache
                final_prime_text = ""  # Initialize to empty string to prevent NameError
                logger.debug(f"🔄 No incremental cache found. 缓存长度: {len(self.message_cache_for_new_session)}, 快照长度: {self.initial_cache_snapshot_len}")

            # Re-check queue-backed and orphan voice mirrors at the final
            # render boundary. They may have expired while this pending session
            # was warming up, after leaving the proactive delivery manager.
            self.pending_extra_replies = self.filter_deliverable_callbacks(
                list(self.pending_extra_replies)
            )
            # 若存在需要植入的额外提示，则指示模型忽略上一条消息，并在下一次响应中统一向用户补充这些提示
            if self.pending_extra_replies and len(self.pending_extra_replies) > 0:
                _lang = normalize_language_code(self.user_language, format='short')
                from config import AGENT_CALLBACK_TOTAL_MAX_TOKENS
                # Budget-aware selection (mirror of the text-mode drain): render
                # only what fits, keep the rest for the next hot-swap rather than
                # dropping it after clearing the queue.
                _selected, _deferred = _select_callbacks_within_token_budget(
                    list(self.pending_extra_replies), AGENT_CALLBACK_TOTAL_MAX_TOKENS
                )
                # Pull-model staleness guard (same contract as the voice/text
                # delivery points): drop extras whose coalesce_key has a newer
                # submission — the push-side eviction removes queue entries but
                # cannot reach text already baked into final_prime_text, so the
                # check must run here, synchronously between select and render
                # (no await in between; a newer cue landing during the prime
                # await below is an accepted residual window). Stale extras are
                # NOT added to _deferred: their newer same-key mirror is (or will
                # be) queued in their place.
                _selected = [
                    e for e in _selected
                    if not self._coalesce_entry_is_stale(e)
                ]
                final_prime_text += _render_pending_extra_replies_by_origin(
                    _selected,
                    lang=_lang,
                    lanlan_name=self.lanlan_name,
                    master_name=self.master_name,
                )
                # Passive（read）回调搭车：本分支只记预算参照（extras 先占
                # 份额），选取推迟到主 prime 之后的统一注入点——收窄"同 key
                # 更新 cue 在主 prime await 期间入队"的陈旧窗口（Codex P2）。
                # 注入绝不混进本分支 skipped=False 的播报 prime，read 内容
                # 不能成为触发主动发言那一轮 user turn 的一部分。Gemini 在
                # 有 extras 的 swap 上不搭车（统一注入点会跳过）：全局
                # _skip_until_next_response 标志不分响应，第二个 turn 会
                # 丢播报、放出 read 响应（Codex P1），留队等下一次无 extras
                # 的 swap。
                _extras_for_budget = _selected
                try:
                    await self.pending_session.prime_context(final_prime_text, skipped=False)
                except (
                    web_exceptions.ConnectionClosed,
                    AttributeError,
                    ConnectionError,
                    RuntimeError,
                    asyncio.TimeoutError,
                ) as e:
                    # pending_session 连接已关闭或websocket为None，放弃整个 swap 操作。
                    # skipped=False 的 prime 走 create_response → response arbiter，
                    # ticket 失败以 ConnectionError / RuntimeError / TimeoutError 浮出
                    # （派发被打断、连接不可用、终态超时等）——这些同样意味着"本次
                    # prime 没有送达、放弃本次 swap、老会话继续用"，必须走本分支的
                    # 定向收尾，而不是落到外层兜底把良性放弃当 INTERNAL_UPDATE_FAILED
                    # 报给前端。
                    logger.error(f"💥 Final Swap Sequence: pending_session不可用，放弃swap操作: {e}")
                    await self._cleanup_pending_session_resources()
                    await self._reset_preparation_state(clear_main_cache=True)
                    self.is_hot_swap_imminent = False
                    return
                # 注入成功后队列**原地不动**，只记住已选条目的对象引用；真正的
                # 移除延迟到 promote 成功那一刻（"被注入的 session 成为活跃会话"）。
                # 这样 prime→promote 窗口期内的并发移除方——语音主动投递成功清除
                # （trigger_agent_callbacks 按 delivery_id 清双队列）、retraction
                # purge、topic 语音封锁清扫、flood cap——都能正常命中队列里的条目，
                # 不存在"被摘走的条目逃过清除、中止后又被塞回复活"的 TOCTOU 盲区；
                # 而任何 promote 前的中止出口天然保留整个队列（与 _deferred 对齐），
                # 无需恢复代码。over-budget 的 _deferred 留到下一轮。
                _prime_selected_extras = _selected
            else:
                _lang = normalize_language_code(self.user_language, format='short')
                # 与主会话构造点同一口径：热切换同样跑在 text 模式下
                # （见下方 self.input_mode == 'text' 的 pending 分支）。
                final_prime_text += get_context_summary_ready(
                    _lang, input_mode=self.input_mode,
                ).format(name=self.lanlan_name, master=self.master_name)
                # Passive（read）回调搭车：本分支没有 proactive extras，
                # passive 吃满整个预算。非 Gemini 走 if/else 之后的统一
                # skipped=True 注入；Gemini 特例在这里合并进本分支唯一的
                # skipped=True prime——Gemini 没有 instructions 通道，任何
                # prime 都是一个 turn，同一次 swap 发两个 turn 会被全局
                # skip 标志错序丢弃（Codex P1），所以必须单 turn：响应被
                # skip-guard 丢弃、内容留在上下文，read 语义不变。
                if (isinstance(self.pending_session, OmniRealtimeClient)
                        and getattr(self.pending_session, "_is_gemini", False)):
                    _passive_sel, _ = (
                        self._select_passive_callbacks_for_swap_prime(
                            require_media_ready=False,
                            render=False,
                        )
                    )
                    _passive_media_outcome = await self._stage_passive_callback_media(
                        _passive_sel,
                        self.pending_session,
                    )
                    if not _passive_media_outcome["safe_to_continue"]:
                        logger.warning(
                            "Final Swap Sequence: passive native media staging became partial/rejected; abandoning pending session"
                        )
                        await self._cleanup_pending_session_resources()
                        await self._reset_preparation_state(clear_main_cache=True)
                        self.is_hot_swap_imminent = False
                        return
                    _passive_sel, _passive_swap_text = (
                        self._render_claimed_passive_callbacks_for_swap_prime(
                            _passive_sel
                        )
                    )
                    if await _abort_if_native_prefix_lost_its_text(
                        _passive_media_outcome,
                        _passive_swap_text,
                        "after Gemini media staging",
                    ):
                        return
                    if _passive_swap_text:
                        final_prime_text += "\n" + _passive_swap_text
                try:
                    await self.pending_session.prime_context(final_prime_text, skipped=True)
                except (
                    web_exceptions.ConnectionClosed,
                    AttributeError,
                    ConnectionError,
                    RuntimeError,
                    asyncio.TimeoutError,
                ) as e:
                    # 与上方 skipped=False 分支同款收窄→放宽（对偶）：Gemini 的
                    # skipped=True prime 走 SDK send，同样以 RuntimeError /
                    # ConnectionError 浮出；良性"放弃本次 swap"不该落外层兜底。
                    logger.error(f"💥 Final Swap Sequence: pending_session不可用，放弃swap操作: {e}")
                    await self._cleanup_pending_session_resources()
                    await self._reset_preparation_state(clear_main_cache=True)
                    self.is_hot_swap_imminent = False
                    return
                # Gemini 合并搭车成功：prime 已带上 passive 块，此刻记账，
                # 统一注入点会因 _is_gemini 跳过，不会双投。
                if _passive_swap_text:
                    _prime_selected_passive_cbs = _passive_sel

            # Passive（read）搭车条目的统一注入点（非 Gemini；Gemini 已在
            # 无 extras 分支合并单 turn，有 extras 的 Gemini swap 不搭车）。
            # 选取放在主 prime await 之后：同 key 更新 cue 在主 prime 期间
            # 入队会 retract 掉队列里的旧条目，此刻再选就不会把已过期的
            # 快照注进新会话（Codex P2）；剩余窗口只有本次注入自身的
            # await，与 extras 的 accepted residual window 对齐。skipped=True
            # 在非 Gemini 上走 session instructions（Qwen 同路），不产生
            # turn，与播报 prime 物理隔离。provider 调用一旦开始后抛错，无法
            # 证明远端是否已写入；因此必须放弃并关闭本次 pending session，
            # 不能继续 promote 后又把 cue 留队造成未来重试/双投。
            if (isinstance(self.pending_session, OmniRealtimeClient)
                    and not getattr(self.pending_session, "_is_gemini", False)):
                _passive_sel, _ = (
                    self._select_passive_callbacks_for_swap_prime(
                        extras_selected=_extras_for_budget,
                        require_media_ready=False,
                        render=False,
                    )
                )
                _passive_media_outcome = await self._stage_passive_callback_media(
                    _passive_sel,
                    self.pending_session,
                )
                if not _passive_media_outcome["safe_to_continue"]:
                    logger.warning(
                        "Final Swap Sequence: passive native media staging became partial/rejected; abandoning pending session"
                    )
                    await self._cleanup_pending_session_resources()
                    await self._reset_preparation_state(clear_main_cache=True)
                    self.is_hot_swap_imminent = False
                    return
                _passive_sel, _passive_swap_text = (
                    self._render_claimed_passive_callbacks_for_swap_prime(
                        _passive_sel
                    )
                )
                if await _abort_if_native_prefix_lost_its_text(
                    _passive_media_outcome,
                    _passive_swap_text,
                    "after media staging",
                ):
                    return
                if _passive_swap_text:
                    try:
                        await self.pending_session.prime_context(_passive_swap_text, skipped=True)
                        _prime_selected_passive_cbs = _passive_sel
                    except Exception as e:
                        logger.warning(
                            f"Final Swap Sequence: passive ride-along prime failed; abandoning pending session: {e}"
                        )
                        await self._cleanup_pending_session_resources()
                        await self._reset_preparation_state(clear_main_cache=True)
                        self.is_hot_swap_imminent = False
                        return

            _abort_if_passive_claim_retracted("during prime")

            # 2. Start temporary listener for PENDING session's *second* ignored response
            if self.pending_session_final_prime_complete_event:
                self.pending_session_final_prime_complete_event.set()

            # --- PERFORM ACTUAL HOT SWAP ---
            logger.info("Final Swap Sequence: Starting actual session swap...")
            old_main_session = self.session
            old_main_message_handler_task = self.message_handler_task
            # 立即用局部变量持有新 session，并清空 self.pending_session。
            # 必须在任何 await 之前完成：后续 cancel/close 的 await 若触发
            # CancelledError，异常处理器会调 _cleanup_pending_session_resources()，
            # 它检查 self.pending_session；若不提前清零，会把新 session 的 ws 关掉。
            new_session = self.pending_session
            self.pending_session = None

            # ── 步骤 1：先停旧 listener ────────────────────────────────────────────
            # 必须在 old_main_session.close() 之前完成：ws.close() 内部执行关闭握手
            # （等待服务端 CLOSE 帧），本质上是一次 recv()。若旧 task 仍在
            # async for 的 recv() 中，就会产生
            # "cannot call recv while another coroutine is already running recv" 并发冲突。
            if old_main_message_handler_task and not old_main_message_handler_task.done():
                old_main_message_handler_task.cancel()
                try:
                    await asyncio.wait_for(old_main_message_handler_task, timeout=2.0)
                    logger.info("Final Swap Sequence: Old message handler task stopped")
                except asyncio.TimeoutError:
                    # 旧 task 仍占着 recv()，继续往下 close() 会重演并发 recv 冲突。
                    # 关闭 new_session 防止 ws 泄漏，标记超时后中止 swap。
                    old_listener_cancel_timed_out = True
                    logger.error("Final Swap Sequence: 旧 listener 取消超时，中止热切换")
                    try:
                        await new_session.close()
                    except Exception as _e:
                        logger.debug(f"Final Swap Sequence: 超时中止时关闭 new_session 失败（可忽略）: {_e}")
                    raise RuntimeError("旧 listener 取消超时，热切换中止")
                except asyncio.CancelledError:
                    # 这里只允许吞"刚被 cancel 的旧 listener 抛回的取消回波"。
                    # 外层对 final_swap_task 自身的取消（_reset_preparation_state /
                    # end_session）在此 await 点抛的同样是 CancelledError：若一并吞掉，
                    # swap 会活过取消（_wait_one 2s 超时后 final_swap_task 失去追踪），
                    # 稍后无锁 promote self.session，毒化/覆盖新 start_session 的赢家。
                    # cancelling() > 0 表示本任务有未确认的取消请求 —— re-raise 交给
                    # 外层 CancelledError 处理器关闭 new_session 并重置状态。
                    _swap_task = asyncio.current_task()
                    if _swap_task is not None and _swap_task.cancelling() > 0:
                        raise
                except Exception as e:
                    logger.warning(f"Final Swap Sequence: Old task exited with error: {e}")

            _abort_if_passive_claim_retracted("before old session close")

            # Exclude Core voice-final delivery across the entire close+promote
            # window. The ASR dispatcher shares this lock, so a final either
            # completes before the old arbiter closes or sees the replacement
            # after promotion; it can no longer land between the two.
            core_voice_session_lock = getattr(
                self,
                "_core_voice_session_swap_lock",
                None,
            )
            if core_voice_session_lock is None:
                core_voice_session_lock = asyncio.Lock()
                self._core_voice_session_swap_lock = core_voice_session_lock
            async with core_voice_session_lock:
                _abort_if_passive_claim_retracted(
                    "while waiting for core voice swap lock"
                )
                # ── 步骤 2：旧 task 已停，安全关闭旧 session ─────────────────────
                if old_main_session:
                    try:
                        await old_main_session.close()
                    except Exception as e:
                        logger.error(f"💥 Final Swap Sequence: Error closing old session: {e}")

                _abort_if_passive_claim_retracted("before promote")

                # ── promote 前的协作取消检查点 ───────────────────────────────────
                # Python 3.11 的 asyncio.wait_for（步骤 1）以及部分 session.close()
                # （步骤 2）在外层取消恰好落在其内层 await 已完成之后时，会把该取消
                # “正常返回”式吞掉。只要本任务有未确认的取消请求，就交给下面的
                # CancelledError 处理器关闭 new_session、重置状态。
                _swap_task = asyncio.current_task()
                if _swap_task is not None and _swap_task.cancelling() > 0:
                    raise asyncio.CancelledError()

                # ── 步骤 3：promote 新 session ────────────────────────────────────
                # 镜像启动侧的强 CAS：任何偏离都意味着并发 start/end_session
                # 已接管会话，此时覆盖 self.session 会孤儿化赢家。
                async with self.lock:
                    _abort_if_passive_claim_retracted("while waiting for promote lock")
                    _promote_allowed = self.session is old_main_session
                    if _promote_allowed:
                        self.session = new_session
            if not _promote_allowed:
                logger.warning("⚠️ Final Swap Sequence: promote 时 self.session 已被并发接管，中止 swap 并关闭 new_session")
                try:
                    await new_session.close()
                except Exception as _e:
                    logger.debug(f"Final Swap Sequence: 中止 promote 时关闭 new_session 失败（可忽略）: {_e}")
                # 队列没动过：已注入 new_session 的 _selected 仍在队列里，随
                # 接管方纪元的下一次 hot-swap 照常投递（与 _deferred 一致）。
                return

            # WebSocket-native image writes are only provisionally accepted.
            # The passive text prime sends a session.update after those writes;
            # its matching session.updated snapshot is the ordered Provider
            # boundary proving that the entire media+text prefix was processed.
            # Do not replace this with a timing grace period: an image error can
            # legitimately arrive later than a local sleep.
            if (
                _passive_media_outcome is not None
                and _passive_media_outcome["native_rejection_pending"]
            ):
                session_update_ack = new_session.expect_session_update_ack(
                    new_session.instructions
                )
                replacement_listener = asyncio.create_task(
                    new_session.handle_messages()
                )
                self.message_handler_task = replacement_listener
                rejection_wait = asyncio.create_task(
                    _passive_media_outcome["rejection_observed"].wait()
                )
                try:
                    done, _pending = await asyncio.wait(
                        {session_update_ack, rejection_wait},
                        timeout=_PASSIVE_MEDIA_SESSION_UPDATE_ACK_TIMEOUT_S,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    _passive_media_outcome["settled"] = True
                    # 屏障已经落地，晚到的图片拒绝回调再没有意义了。它们的闭包
                    # 扣着整条 callback（可能数张 ~13MB base64），不摘的话要等
                    # 60 秒过期，连续几次成功的图片 callback 就能压着几百 MB。
                    for _image_event_id in _passive_media_outcome.get(
                        "rejection_event_ids", ()
                    ):
                        getattr(
                            new_session, "_inject_rejection_handlers", {}
                        ).pop(_image_event_id, None)
                    new_session.discard_session_update_ack(session_update_ack)
                    if not rejection_wait.done():
                        rejection_wait.cancel()
                    await asyncio.gather(rejection_wait, return_exceptions=True)
                media_prefix_committed = (
                    session_update_ack in done
                    and not session_update_ack.cancelled()
                    and session_update_ack.exception() is None
                    and not _passive_media_outcome["rejected"]
                )
                if not media_prefix_committed:
                    logger.warning(
                        "Final Swap Sequence: passive native media was rejected or its session-update barrier timed out; retiring promoted replacement before callback ACK"
                    )
                    # Retire only the listener/session this swap created.  A
                    # concurrent start_session may have replaced both manager
                    # slots while the Provider barrier above was pending; reading
                    # self.message_handler_task here would capture and cancel that
                    # winner instead of this replacement's listener.
                    async with self.lock:
                        owned_before_retire = bool(
                            self.session is new_session
                            and self.message_handler_task is replacement_listener
                        )
                    if not replacement_listener.done():
                        replacement_listener.cancel()
                        await asyncio.gather(
                            replacement_listener,
                            return_exceptions=True,
                        )
                    try:
                        await new_session.close()
                    except Exception as close_err:
                        logger.debug(
                            "Final Swap Sequence: rejected passive media replacement close failed: %s",
                            close_err,
                        )
                    # Cancellation and close both yield.  Revalidate the complete
                    # ownership pair before touching shared lifecycle state: if a
                    # winner arrived during either await, local retirement above
                    # is the only cleanup this stale swap is allowed to perform.
                    async with self.lock:
                        still_owns_replacement = bool(
                            owned_before_retire
                            and self.session is new_session
                            and self.message_handler_task is replacement_listener
                        )
                        if still_owns_replacement:
                            self.session = None
                            self.message_handler_task = None
                            self.is_active = False
                    if not still_owns_replacement:
                        logger.info(
                            "Final Swap Sequence: rejected passive media replacement lost ownership during retirement; preserving concurrent session"
                        )
                        return
                    await self._close_independent_asr(
                        next_route_mode="blocked",
                    )
                    await self.send_status(json.dumps({
                        "code": "INTERNAL_UPDATE_FAILED",
                        "details": {
                            "error": (
                                "passive media session-update barrier failed"
                            ),
                        },
                    }))
                    await self.send_session_ended_by_server()
                    await self._reset_preparation_state(
                        clear_main_cache=True,
                        from_final_swap=True,
                    )
                    return
            # promote 成功：被注入的 session 已成为活跃会话，注入内容必随其下一
            # 轮回复送达——此刻才把 _selected 从队列移除。按对象身份移除：窗口期
            # 内被并发路径（语音投递清除/retraction/清扫/cap）先行移除的条目在此
            # 自然 no-op，不会误删同 id 重入队的新条目。_removed_extras 记录真正
            # 移除的子集，供紧随其后的 ws 失效 fail-close 出口塞回。
            if _prime_selected_extras:
                _selected_obj_ids = {id(extra) for extra in _prime_selected_extras}
                _removed_extras = [
                    extra for extra in self.pending_extra_replies
                    if id(extra) in _selected_obj_ids
                ]
                if _removed_extras:
                    self.pending_extra_replies = [
                        extra for extra in self.pending_extra_replies
                        if id(extra) not in _selected_obj_ids
                    ]
                    # 快照"此刻仍有配对 cb"的 delivery_id：restore 时该 id 的 cb
                    # 若已消失，说明窗口期内被消费（语音投递成功会 prune 双队列，
                    # extras 半边对已摘走条目 no-op），塞回会重复播报。
                    _removed_ids = {
                        extra.get("_callback_delivery_id")
                        for extra in _removed_extras
                        if isinstance(extra, dict) and extra.get("_callback_delivery_id")
                    }
                    _removed_cb_backed_ids = {
                        cb.get("_callback_delivery_id")
                        for cb in (getattr(self, "pending_agent_callbacks", None) or [])
                        if isinstance(cb, dict)
                        and cb.get("_callback_delivery_id") in _removed_ids
                    }
            # Passive 搭车条目的对偶出队：同样按对象身份、同样只在 promote
            # 成功后。窗口期被 drain/清扫抢先消费的条目在此 no-op。
            _removed_passive_cbs = self._remove_swap_delivered_passive_cbs(
                _prime_selected_passive_cbs
            )
            self.response_backend = (
                'offline_vlm'
                if isinstance(self.session, OmniOfflineClient)
                else 'realtime'
            )
            self._require_context_append_current_delivery = True
            next_context_count_at_promote = len(self._snapshot_next_session_context_messages())
            await self._apply_pending_tts_route_after_swap()
            # The pending Omni session is now the active session. If its Core
            # provider changed, replace the independent ASR before replaying
            # cached microphone audio so one frame can never cross providers.
            await self._reconcile_independent_asr_after_core_change()
            self.current_speech_id = str(uuid4())
            self._tts_done_queued_for_turn = False
            self._tts_done_pending_until_ready = False
            self.session_start_time = datetime.now()
            self._session_turn_count = 0

            # promote 之后立刻把 registry 最新状态推过去 —— swap 序列里
            # ``self.pending_session → 局部 new_session → self.session``
            # 跨了几个 await，期间 register_tool 触发的 _sync 可能既赶不上
            # pending_session（已被挪走置 None）也赶不上 self.session
            # （还没赋值），导致 promote 后新 session 缺了那次注册的工具。
            try:
                await self._sync_tools_to_active_session()
            except Exception as _sync_err:
                logger.warning("⚠️ final swap post-promote tool sync failed: %s", _sync_err)

            # 验证新session的WebSocket是否仍然有效（可能在swap过程中被服务器断开）
            if isinstance(self.session, OmniRealtimeClient) and not self.session.ws:
                # 旧session已关闭无法回滚，抛出异常让 except 块走重建流程
                raise RuntimeError("新session的WebSocket在swap后已失效，热切换失败")

            transferred_next_context_count = (
                self.initial_next_session_context_snapshot_len
                + len(incremental_next_session_context)
            )
            consumed_next_context_count = await self._prime_late_next_session_context_after_swap(
                transferred_next_context_count,
                next_context_count_at_promote,
            )

            # ── 步骤 4：启动新 listener ───────────────────────────────────────────
            if (
                self.session
                and hasattr(self.session, 'handle_messages')
                and (
                    not self.message_handler_task
                    or self.message_handler_task.done()
                )
            ):
                self.message_handler_task = asyncio.create_task(self.session.handle_messages())

            # ── 步骤 5：flush 热切换音频缓存到新 session ─────────────────────────
            # 必须在 promote 之后调用：_flush_hot_swap_audio_cache 使用 self.session
            # 发送音频，此时 self.session 已是新 session，音频会正确发往新会话。
            await self._flush_hot_swap_audio_cache()
            self._consume_next_session_context_messages(consumed_next_context_count)

            # Reset all preparation states and clear the *main* cache now that it's fully transferred
            # pending_session已在swap后立即清除，这里只需要重置其他状态
            await self._reset_preparation_state(
                clear_main_cache=True, from_final_swap=True)  # This will clear pending_*, is_preparing_new_session, etc. and self.message_cache_for_new_session
            logger.info("✅ 热切换完成")
            

        except asyncio.CancelledError:
            logger.info("Final Swap Sequence: Task cancelled.")
            self.is_hot_swap_imminent = False
            # new_session 在 self.pending_session = None 后由局部变量持有。
            # 若 swap 在 promote 之前被取消，_cleanup_pending_session_resources 不再持有它，
            # 必须在此手动关闭，防止 ws 泄漏。
            if new_session is not None and new_session is not self.session:
                try:
                    await new_session.close()
                except Exception as _e:
                    logger.debug(f"Final Swap Sequence: CancelledError 路径关闭 new_session 失败（可忽略）: {_e}")
            await self._cleanup_pending_session_resources()
            await self._reset_preparation_state(clear_main_cache=True)
            # 镜像 except Exception 的死会话 fail-close：取消若落在旧会话已
            # close() 之后（realtime 的 ws / 文本 offline 的 llm 已被 close()
            # 清空）或 promote 后连接失效，下面的重启只会给死会话建 listener
            # ——直接 fail-close 让前端重连。（pending 生命周期错误触发 reset
            # 的场景里旧会话仍活着，重启行为保留。）
            if self._swap_session_is_dead(self.session):
                self.session = None
                self.is_active = False
            # promote 前取消→队列没动过、_removed_extras 为空，此调用 no-op。
            # promote 后取消→只可能来自外部 reset/end_session/新 start_session
            # prelude，它们随后都会关闭 promoted 会话，注入内容不会再投递——把
            # promote 时移除的条目塞回队首等下一次 hot-swap。
            self._restore_undelivered_swap_extras(_removed_extras, _removed_cb_backed_ids)
            self._restore_undelivered_swap_passive_cbs(_removed_passive_cbs)
            # post-promote 取消（_removed_extras / _removed_passive_cbs 非空）不
            # 重启 listener：promoted 会话即将被 canceller 关闭，重启会让它在
            # 关闭前把已 prime 的内容播出来，与刚塞回的队列形成双投；没有
            # listener，服务器响应不会被消费播出。重启只服务 promote 前取消后
            # "老会话失去 listener"的恢复。
            if self.is_active and self.session and hasattr(self.session, 'handle_messages') and not _removed_extras and not _removed_passive_cbs and (not self.message_handler_task or self.message_handler_task.done()):
                self.message_handler_task = asyncio.create_task(self.session.handle_messages())

        except Exception as e:
            logger.error(f"💥 Final Swap Sequence: Error: {e}")
            self.is_hot_swap_imminent = False
            await self.send_status(json.dumps({"code": "INTERNAL_UPDATE_FAILED", "details": {"error": str(e)}}))
            # 同上：new_session 若未完成 promote，需手动关闭防 ws 泄漏。
            if new_session is not None and new_session is not self.session:
                try:
                    await new_session.close()
                except Exception as _e:
                    logger.debug(f"Final Swap Sequence: 异常路径关闭 new_session 失败（可忽略）: {_e}")
            await self._cleanup_pending_session_resources()
            await self._reset_preparation_state(clear_main_cache=True)
            if old_listener_cancel_timed_out:
                # 旧 listener 取消超时：旧 task 可能在本函数返回后才真正退出，
                # 此时无法安全判断 task.done() 并补建 listener，会留下"活跃但无监听"状态。
                # 直接 fail-close：清除会话状态让前端重连，优于让后续输入陷入僵局。
                # （promote 前出口，队列没动过，_selected 仍在队列无需恢复。）
                # 旧 session 不能就地 close()（会与卡死的 recv() 并发冲突，见步骤1
                # 注释），但裸清引用会泄漏 ws：挂分离收尸 task，等旧 listener 真正
                # 退出后再 best-effort 关闭。listener 永不退出时与裸清引用等价
                # （无回归），退出时 ws 得到回收。
                _stuck_listener_task = old_main_message_handler_task
                _orphan_old_session = old_main_session

                async def _reap_old_session_after_listener_exit():
                    if _stuck_listener_task is not None:
                        try:
                            await _stuck_listener_task
                        except (asyncio.CancelledError, Exception):
                            pass  # 收尸只关心"已退出"，退出方式无所谓
                    if _orphan_old_session is not None:
                        try:
                            await _orphan_old_session.close()
                        except Exception as _reap_err:
                            logger.debug(f"Final Swap Sequence: 收尸关闭旧 session 失败（可忽略）: {_reap_err}")

                _reaper = asyncio.create_task(_reap_old_session_after_listener_exit())
                _ORPHAN_SESSION_REAPER_TASKS.add(_reaper)
                _reaper.add_done_callback(_ORPHAN_SESSION_REAPER_TASKS.discard)
                self.session = None
                self.message_handler_task = None
                self.is_active = False
                return
            # 若 self.session 已死（promote 后 realtime ws 失效 / 文本 offline
            # llm 被清、或取消落在旧会话 close 之后），清除会话状态，防止
            # is_active=True + 死连接让后续输入进入坏会话。
            # 这也是"移除已发生（promote 时）且被注入的会话已死"的出口：把
            # promote 时真正移除的 _removed_extras 塞回队首等下一次 hot-swap。
            # promote 后会话存活的其他失败不塞回——注入内容仍在其上下文里会随
            # 下一轮回复送达，塞回会造成双投。
            if self._swap_session_is_dead(self.session):
                self.session = None
                self.is_active = False
                self._restore_undelivered_swap_extras(_removed_extras, _removed_cb_backed_ids)
                self._restore_undelivered_swap_passive_cbs(_removed_passive_cbs)
            if self.is_active and self.session and hasattr(self.session, 'handle_messages') and (not self.message_handler_task or self.message_handler_task.done()):
                self.message_handler_task = asyncio.create_task(self.session.handle_messages())
        finally:
            self._release_swap_prime_passive_claims(_passive_sel)
            self._purge_undeliverable_callbacks()
            self.is_hot_swap_imminent = False  # Always reset this flag
            if self.final_swap_task and self.final_swap_task.done():
                self.final_swap_task = None

    async def disconnected_by_server(self, *, expected_session=None):
        if expected_session is not None and expected_session is not self.session:
            logger.info("⏭️ disconnected_by_server: expected_session stale, skipping")
            return
        await self.send_status(json.dumps({"code": "CHARACTER_DISCONNECTED", "details": {"name": self.lanlan_name}}))
        await self.send_session_ended_by_server()
        self.sync_message_queue.put({'type': 'system', 'data': 'API server disconnected'})
        await self.cleanup(expected_session=expected_session)

    def _queue_session_end_memory_barrier(self, callback):
        """Queue a terminal memory settlement followed by one local callback."""
        completion = asyncio.get_running_loop().create_future()
        self.sync_message_queue.put({
            'type': 'system',
            'data': 'session end',
            '_after_memory_settlement': callback,
            '_memory_settlement_done': completion,
        })
        return completion

    async def _wait_for_session_end_memory_barrier(
        self,
        completion,
        callback,
        *,
        timeout_seconds: float,
    ) -> None:
        """Wait for connector settlement, with an idempotent immediate fallback.

        The queue item retains the callback after a timeout.  Therefore a slow or
        temporarily stopped connector will run it again *after* its eventual
        memory write, closing the late-write window that motivated the barrier.
        """
        try:
            await asyncio.wait_for(
                asyncio.shield(completion),
                timeout=max(0.1, float(timeout_seconds)),
            )
            return
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] memory settlement barrier timed out; clearing recent "
                "context now and leaving a queued post-settlement cleanup",
                self.lanlan_name,
            )

            # The caller no longer awaits this future after the fallback. Consume
            # a possible late callback exception to avoid an unhandled-future log;
            # the connector logs the failure at its source as well.
            def _consume_late_completion(future):
                if future.cancelled():
                    return
                try:
                    future.exception()
                except Exception:
                    pass

            completion.add_done_callback(_consume_late_completion)
            result = callback()
            if inspect.isawaitable(result):
                await result

    async def settle_session_memory_if_idle(
        self,
        callback,
        *,
        timeout_seconds: float = 15.0,
    ) -> bool:
        """Queue a memory barrier only while this manager is still idle."""
        async with self.lock:
            if self.is_active or self.is_starting:
                return False
            completion = self._queue_session_end_memory_barrier(callback)
        await self._wait_for_session_end_memory_barrier(
            completion,
            callback,
            timeout_seconds=timeout_seconds,
        )
        return True

    async def end_session(
        self,
        by_server=False,
        *,
        expected_session=None,
        reset_starting_count=True,
        after_memory_settlement=None,
        memory_settlement_timeout=15.0,
        preserve_pending_input=False,
    ):  # 与Core API断开连接
        # 「用户/前端主动结束启动」信号：只有前端发来的 end_session / pause_session
        # （by_server=False 且 reset_starting_count=True，见 websocket_router）才计。
        # 内部 recovery（reset_starting_count=False）与各类 by_server=True cleanup
        # 不算，避免把 in-flight 启动失败误判成"用户放弃"而误杀跨模式重启
        # （见 start_session 跨模式分支的 _user_session_abandon_epoch 守卫）。放在所有
        # 早退之前，确保 in-flight（尚未 active）期间前端 end_session 也能计上。
        if not by_server and reset_starting_count:
            self._user_session_abandon_epoch += 1
        memory_barrier_completion = None
        # Pre-check: no-side-effect guard before _init_renew_status which mutates
        # pending/prewarm state.  A stale callback must not nuke preparation state.
        _inactive_early = False
        async with self.lock:
            if not self.is_active:
                # Stale-session guard: 即使未激活，也要确认不是过期回调，
                # 否则会误清理新 session 正在创建的 TTS 资源
                if expected_session is not None and expected_session is not self.session:
                    logger.info("⏭️ end_session: expected_session stale (inactive-early), skipping")
                    return
                # 即使会话未完全激活（如 start_session 失败），也要清理
                # 可能残留的 TTS 重试状态，防止污染下一次会话
                self._reset_tts_retry_state()
                self._audio_stream_epoch += 1
                self._clear_audio_stream_queue("end_session_inactive")
                self._cancel_audio_stream_worker("end_session_inactive")
                self._reset_voice_echo_suppression_cache()
                _inactive_early = True
                # start_tts_if_needed 可能已启动 TTS 线程/handler，
                # 但 is_active 尚未置 True 就失败了——快照引用以便释放锁后清理
                _orphan_tts_handler = self.tts_handler_task
                _orphan_tts_thread = self.tts_thread
                _orphan_tts_rq = self.tts_request_queue
                _orphan_tts_rsq = self.tts_response_queue
            elif expected_session is not None and expected_session is not self.session:
                logger.info("⏭️ end_session: expected_session stale (pre-check), skipping")
                return
            else:
                # 尽早取消 TTS 延迟重试任务并清理错误码（持锁状态下），
                # 防止 _init_renew_status 期间 respawn task 触发无效重试
                self._reset_tts_retry_state()

        # Clear the playback gate + manager queue on genuine teardown. Placed
        # AFTER the stale-session guards above (which `return` early) so a stale/
        # duplicate end_session callback can't reset the CURRENT live session's
        # gate or drop its queued cues (Codex P1).
        self._reset_proactive_gate()
        self.clear_speech_playback_gains()

        # Stale expected_session callbacks have already returned above. Invalidate
        # ASR callbacks before any remaining teardown awaits can yield.
        await self._close_independent_asr(next_route_mode="blocked")

        if _inactive_early:
            if reset_starting_count:
                # 前端启动超时会在 session 尚未 active 时发送 end_session。
                # 旧输入缓存必须在释放 start_session guard 之前清掉；释放后
                # 新一轮启动可能已经开始缓存用户消息，旧收尾不能再碰它们。
                async with self.input_cache_lock:
                    self.session_ready = False
                    self.pending_input_data.clear()
                    self._clear_pending_context_appends()
                async with self.lock:
                    if expected_session is None or expected_session is self.session:
                        self._starting_session_count = 0
                        self._starting_input_mode = None
            # start_tts_if_needed 可能已启动 TTS 但 is_active 未置 True（如 LLM 启动失败），
            # 必须清理这些孤儿资源，否则线程/task 会泄漏
            await self._teardown_tts_runtime(
                _orphan_tts_handler, _orphan_tts_thread,
                _orphan_tts_rq, _orphan_tts_rsq)
            if callable(after_memory_settlement):
                memory_barrier_completion = self._queue_session_end_memory_barrier(
                    after_memory_settlement,
                )
                await self._wait_for_session_end_memory_barrier(
                    memory_barrier_completion,
                    after_memory_settlement,
                    timeout_seconds=memory_settlement_timeout,
                )
            return

        await self._init_renew_status()

        _post_init_inactive = False
        async with self.lock:
            # Re-check after await: another task may have deactivated or swapped session.
            if expected_session is not None and expected_session is not self.session:
                logger.info("⏭️ end_session: expected_session stale (post-init), skipping")
                return
            if not self.is_active:
                self._audio_stream_epoch += 1
                self._clear_audio_stream_queue("end_session_post_init_inactive")
                self._cancel_audio_stream_worker("end_session_post_init_inactive")
                self._reset_voice_echo_suppression_cache()
                _post_init_inactive = True
            else:
                self.is_active = False
                # 重置 _starting_session_count：如果 start_session 正在执行中（比如卡在预热），
                # 前端超时后发来 end_session，必须解除这个 guard，否则用户手动重试会被
                # 静默丢弃（_starting_session_count>0 → return），导致"必须重启应用才能恢复"。
                # 但 start_session 内部自己调 end_session 清理旧 session 时必须传
                # reset_starting_count=False，否则 guard 被清零后并发的第二次 start_session
                # 会穿过，产生孤儿 OmniRealtimeClient（silence_check_task/ws 泄漏）。
                if reset_starting_count:
                    self._starting_session_count = 0
                    self._starting_input_mode = None
                self._audio_stream_epoch += 1
                self._clear_audio_stream_queue("end_session")
                self._cancel_audio_stream_worker("end_session")
                self._reset_voice_echo_suppression_cache()

                # Activity tracker：session 关闭，voice_engaged 不再可能触发。
                self._activity_tracker.on_voice_mode(False)

                # Snapshot all mutable resource refs while holding the lock,
                # then operate only on locals to prevent killing newly created resources.
                main_session_ref = self.session
                message_handler_task_ref = self.message_handler_task
                tts_handler_task_ref = self.tts_handler_task
                tts_thread_ref = self.tts_thread
                tts_request_queue_ref = self.tts_request_queue
                tts_response_queue_ref = self.tts_response_queue

        if _post_init_inactive:
            if callable(after_memory_settlement):
                memory_barrier_completion = self._queue_session_end_memory_barrier(
                    after_memory_settlement,
                )
                await self._wait_for_session_end_memory_barrier(
                    memory_barrier_completion,
                    after_memory_settlement,
                    timeout_seconds=memory_settlement_timeout,
                )
            return

        logger.info("End Session: Starting cleanup...")
        if not callable(after_memory_settlement):
            self.sync_message_queue.put({'type': 'system', 'data': 'session end'})

        if message_handler_task_ref:
            message_handler_task_ref.cancel()
            try:
                await asyncio.wait_for(message_handler_task_ref, timeout=3.0)
            except asyncio.CancelledError:
                # Normal cancellation echo; the timeout case is handled separately below.
                pass
            except asyncio.TimeoutError:
                logger.warning("End Session: Warning: Listener task cancellation timeout.")
            except Exception as e:
                # 任务可能已因并发 recv() 冲突等原因提前退出，此处只是发现既成事实
                logger.warning(f"End Session: Listener task had prior error: {e}")
            if self.message_handler_task is message_handler_task_ref:
                self.message_handler_task = None

        if main_session_ref:
            try:
                logger.info("End Session: Closing connection...")
                await main_session_ref.close()
                logger.info("End Session: Qwen connection closed.")
            except Exception as e:
                logger.error(f"💥 End Session: Error during cleanup: {e}")
            finally:
                if self.session is main_session_ref:
                    self.session = None

        await self._teardown_tts_runtime(
            tts_handler_task_ref, tts_thread_ref,
            tts_request_queue_ref, tts_response_queue_ref)
        # handler 可能在锁释放到 task 取消之间重新引入了过期错误码——
        # 在活跃会话拆除路径（is_active 已置 False）补充一次清理。
        # 但仅当 TTS 资源尚未被新会话替换时才重置，避免擦除新会话的状态。
        tts_replaced_by_new_session = (
            (self.tts_handler_task is not None and self.tts_handler_task is not tts_handler_task_ref) or
            (self.tts_thread is not None and self.tts_thread is not tts_thread_ref)
        )
        if not tts_replaced_by_new_session:
            self._reset_tts_retry_state()
        
        # 重置输入缓存状态
        async with self.input_cache_lock:
            self.session_ready = False
            if not preserve_pending_input:
                self.pending_input_data.clear()
            self._clear_pending_context_appends()

        self.last_time = None
        if callable(after_memory_settlement):
            # The isolation barrier is intentionally queued only after every
            # session producer has been stopped.  Otherwise an output callback
            # racing with teardown could enqueue old text *behind* the barrier
            # and survive its post-settlement clear.
            memory_barrier_completion = self._queue_session_end_memory_barrier(
                after_memory_settlement,
            )
            await self._wait_for_session_end_memory_barrier(
                memory_barrier_completion,
                after_memory_settlement,
                timeout_seconds=memory_settlement_timeout,
            )
        if not by_server:
            await self.send_status(json.dumps({"code": "CHARACTER_LEFT", "details": {"name": self.lanlan_name}}))
            logger.info("End Session: Resources cleaned up.")

    async def cleanup(self, expected_websocket=None, *, expected_session=None, reset_starting_count=True):
        """
        Clean up session resources.

        Args:
            expected_websocket: optional, the expected websocket instance.
                               If provided and it doesn't match the current websocket, skip cleanup.
                               Prevents an old connection from wrongly cleaning up a new connection's resources (race protection).
            expected_session: optional, the expected session instance.
                             Session-level guard from lifecycle callbacks, passed through to end_session.
            reset_starting_count: forwarded to end_session. Pass False when the
                             caller is itself a start_session that owns the
                             _starting_session_count guard and will decrement it
                             in its own finally — letting cleanup reset it to 0
                             early opens a premature-0 window where a concurrent
                             start (e.g. the cross-mode restart wait) sees the
                             guard freed before the failing start has fully
                             unwound, then gets its websocket/guard clobbered by
                             the still-running teardown (Codex P2). Same rationale
                             as the in-start old-session cleanup at line ~5610.
        """
        if expected_websocket is not None and self.websocket is not None:
            if self.websocket != expected_websocket:
                logger.info("⏭️ cleanup 跳过：当前 websocket 已被新连接替换")
                return

        await self.end_session(by_server=True, expected_session=expected_session,
                               reset_starting_count=reset_starting_count)
        # 清理websocket引用，防止保留失效的连接
        # 使用共享锁保护websocket操作，防止与initialize_character_data()中的restore竞争
        if self.websocket_lock:
            async with self.websocket_lock:
                # 再次检查：只有当 websocket 仍是我们期望的那个时才清理
                if expected_websocket is None or self.websocket == expected_websocket:
                    self.websocket = None
        else:
            # 如果没有设置websocket_lock（旧代码路径），直接清理
            if expected_websocket is None or self.websocket == expected_websocket:
                self.websocket = None
