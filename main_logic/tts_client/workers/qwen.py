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

"""Qwen realtime TTS worker."""

import numpy as np
import soxr
import time
import json
import base64
import websockets
import asyncio

from urllib.parse import quote
from utils.config_manager import get_config_manager
from utils.dashscope_region import dashscope_ws_url_from_base

from .._infra import (
    AudioDoneEmitter,
    TTS_SHUTDOWN_SENTINEL,
    _resample_audio,
    make_audio_jitter_buffer,
    _enqueue_error,
)
from .._telemetry import _record_tts_telemetry
from utils.logger_config import get_module_logger

logger = get_module_logger(__name__, "Main")

_QWEN_REALTIME_TTS_MODEL = "qwen3-tts-flash-realtime"
_DASHSCOPE_DEFAULT_REALTIME_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"

def _resolve_qwen_realtime_tts_url() -> str:
    """Pick the realtime TTS WebSocket URL based on the current Qwen/Qwen Intl core config."""
    try:
        core_config = get_config_manager().get_core_config() or {}
    except Exception:
        core_config = {}
    base_ws_url = dashscope_ws_url_from_base(
        core_config.get("CORE_URL", ""),
        "realtime",
        _DASHSCOPE_DEFAULT_REALTIME_WS_URL,
    )
    configured_model = str(core_config.get("TTS_MODEL") or "").strip()
    model = configured_model if configured_model.startswith("qwen3-tts") else _QWEN_REALTIME_TTS_MODEL
    return f"{base_ws_url}?model={quote(model, safe='')}"

def qwen_realtime_tts_worker(request_queue, response_queue, audio_api_key, voice_id):
    """
    Qwen realtime TTS worker (for default voices)
    Uses Aliyun's realtime TTS API (qwen3-tts-flash-realtime)
    
    Args:
        request_queue: multiprocess request queue receiving (speech_id, text) tuples
        response_queue: multiprocess response queue sending audio data (also used for the ready signal)
        audio_api_key: API key
        voice_id: voice ID, defaults to "Momo"
    """
    if not voice_id:
        voice_id = "Momo"

    from utils.language_utils import detect_tts_language_hint, TTS_LANG_DETECT_MIN_CHARS

    async def async_worker():
        """Async TTS worker main loop"""
        tts_url = _resolve_qwen_realtime_tts_url()
        ws = None
        current_speech_id = None
        receive_task = None
        session_ready = asyncio.Event()
        response_done = asyncio.Event()  # 用于标记当前响应是否完成
        buffer_committed = False  # 防止同一轮次重复提交缓冲区
        session_configured = False  # 当前连接是否已发出 session.update（延迟到首批文本到达）
        pending_text_buffer = ""  # 延迟发送的文本缓冲，用于首 N 字语言检测
        # 流式重采样器（24kHz→48kHz）- 维护 chunk 边界状态
        resampler = soxr.ResampleStream(24000, 48000, 1, dtype='float32')
        # Qwen realtime can produce 1-2s inter-chunk gaps. A small jitter buffer
        # gives the client enough queued PCM to ride over short upstream stalls.
        # 共享 AudioJitterBuffer，旧的 NEKO_QWEN_TTS_* 覆盖仍兼容（见 make_audio_jitter_buffer）。
        qwen_audio_jitter = make_audio_jitter_buffer(response_queue, legacy_env_prefix="NEKO_QWEN_TTS")
        # 上游 done 事件 = 本轮音频流关闭。两个 receive task 共用同一个 emitter，
        # bound_speech_id 在建任务时钉死本轮 sid：sid 切换路径会先推进
        # current_speech_id 再 await 关旧连接，此刻旧 receive 任务若读到迟到的
        # done 事件，会把上一轮的收尾错标到新一轮。
        # 再压一道 buffer_committed 闸：只有本轮已经 commit 过缓冲区，done 事件
        # 才可能是整轮收尾；上游若按句发 done，没这道闸就是早发。
        audio_done = AudioDoneEmitter(response_queue)

        def _emit_audio_done(bound_speech_id) -> None:
            """Signal end-of-stream once the round's buffer commit was sent."""
            if buffer_committed:
                audio_done.emit(bound_speech_id)

        def build_config_message(lang_hint=None):
            """Build the session.update message; lang_hint='ja' specifies Japanese, anything else uses server-side Auto."""
            session = {
                "mode": "server_commit",
                "voice": voice_id,
                "response_format": "pcm",
                "sample_rate": 24000,
                "channels": 1,
                "bit_depth": 16,
            }
            if lang_hint == "ja":
                session["language_type"] = "Japanese"
            return {
                "type": "session.update",
                "event_id": f"event_{int(time.time() * 1000)}",
                "session": session,
            }

        async def _flush_deferred_config(force: bool = False) -> bool:
            """Send the deferred session.update on demand and append the buffered text out.

            - Below threshold and not force: returns False.
            - Already sent or after executing: returns True.
            """
            nonlocal session_configured, pending_text_buffer
            if session_configured:
                return True
            if not ws:
                return False
            if not force and len(pending_text_buffer) < TTS_LANG_DETECT_MIN_CHARS:
                return False
            lang_hint = detect_tts_language_hint(pending_text_buffer)
            try:
                await ws.send(json.dumps(build_config_message(lang_hint)))
            except Exception as e:
                logger.error(f"发送延迟 session.update 失败: {e}")
                return False
            session_configured = True
            try:
                await asyncio.wait_for(session_ready.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Qwen TTS: 延迟 session.update 等待超时")
            if pending_text_buffer.strip():
                try:
                    await ws.send(json.dumps({
                        "type": "input_text_buffer.append",
                        "event_id": f"event_{int(time.time() * 1000)}",
                        "text": pending_text_buffer,
                    }))
                    _record_tts_telemetry("qwen", len(pending_text_buffer))
                except Exception as e:
                    # append 发失败时连接多半已断，调用方不能继续发 commit；
                    # 返回 False 让 sid=None/文本路径走 continue 触发重连。
                    logger.error(f"发送缓冲文本失败: {e}")
                    return False
            pending_text_buffer = ""
            return True

        try:
            # 连接WebSocket
            headers = {"Authorization": f"Bearer {audio_api_key}"}

            ws = await websockets.connect(tts_url, additional_headers=headers)
            
            # 等待并处理初始消息
            async def wait_for_session_ready():
                """Wait for session-creation confirmation"""
                try:
                    async for message in ws:
                        event = json.loads(message)
                        event_type = event.get("type")
                        
                        # Qwen TTS API 返回 session.updated 而不是 session.created
                        if event_type in ["session.created", "session.updated"]:
                            session_ready.set()
                            break
                        elif event_type == "error":
                            _enqueue_error(response_queue, event)
                            break
                except Exception as e:
                    _enqueue_error(response_queue, e)
            
            # 发送预热配置（pre-warm），真正的 session.update 会在首批文本到达后
            # 通过 _flush_deferred_config 重新发送（携带语言提示）。
            await ws.send(json.dumps(build_config_message(None)))
            session_configured = True

            # 等待会话就绪（超时5秒）
            try:
                await asyncio.wait_for(wait_for_session_ready(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.error("❌ 等待会话就绪超时")
                response_queue.put(("__ready__", False))
                return

            if not session_ready.is_set():
                logger.error("❌ 会话未能正确初始化")
                response_queue.put(("__ready__", False))
                return

            # 发送就绪信号
            logger.info("Qwen TTS 已就绪，发送就绪信号")
            response_queue.put(("__ready__", True))

            # 初始接收任务（会在每次新 speech_id 时重新创建）
            async def receive_messages_initial(bound_speech_id):
                """Initial receive task"""
                nonlocal ws
                cancelled = False
                try:
                    async for message in ws:
                        event = json.loads(message)
                        event_type = event.get("type")

                        if event_type == "error":
                            # 空闲超时 / 会话过期：不报 error，标记连接丢失，按需重连
                            err_msg = event.get("error", {}).get("message", "")
                            if "request timeout" in err_msg or "session_expired" in err_msg:
                                logger.debug(f"Qwen TTS 空闲超时，标记连接已断开: {err_msg}")
                                break
                            _enqueue_error(response_queue, event)
                        elif event_type == "response.audio.delta":
                            try:
                                audio_bytes = base64.b64decode(event.get("delta", ""))
                                audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                                # 使用流式重采样器 24000Hz -> 48000Hz
                                qwen_audio_jitter.append(_resample_audio(audio_array, 24000, 48000, resampler))
                            except Exception as e:
                                logger.error(f"处理音频数据时出错: {e}")
                        elif event_type in ["response.done", "response.audio.done", "output.done"]:
                            # 服务器明确表示音频生成完成，设置完成标志
                            logger.debug(f"收到响应完成事件: {event_type}")
                            qwen_audio_jitter.flush()
                            # 预热连接绑的是 None（首个真实 sid 一定先走重连分支），
                            # emit(None) 静默跳过；带参保持两个 receive task 同形。
                            _emit_audio_done(bound_speech_id)
                            response_done.set()
                except websockets.exceptions.ConnectionClosed:
                    pass
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except Exception as e:
                    logger.error(f"消息接收出错: {e}")
                finally:
                    if not cancelled:
                        qwen_audio_jitter.flush()
                    # 接收循环退出（超时/断开），清理连接状态以便主循环按需重连
                    if ws:
                        try:
                            await ws.close()
                        except Exception:
                            pass
                        ws = None
                    session_ready.clear()

            receive_task = asyncio.create_task(receive_messages_initial(current_speech_id))

            # 主循环：处理请求队列
            loop = asyncio.get_running_loop()
            pending = None  # 断线重试时暂存当前片段，保证顺序（不回共享队列）
            while True:
                # 优先处理断线暂存的片段，再从队列取新请求
                if pending:
                    sid, tts_text = pending
                    pending = None
                else:
                    try:
                        sid, tts_text = await loop.run_in_executor(None, request_queue.get)
                    except Exception:
                        break

                if sid == TTS_SHUTDOWN_SENTINEL:
                    break

                if sid == "__interrupt__":
                    # 打断：立即关闭连接，不发 commit、不等服务器确认
                    qwen_audio_jitter.begin_interrupt()
                    audio_done.begin_interrupt()  # 打断轮不发 audio_done（走独立 cancel 通道）
                    try:
                        if receive_task and not receive_task.done():
                            receive_task.cancel()
                            try:
                                await receive_task
                            except asyncio.CancelledError:
                                # Expected during interrupt teardown.
                                pass
                            except Exception as e:
                                logger.debug(f"Qwen TTS interrupted receive task cleanup failed: {e}")
                            receive_task = None
                        if ws:
                            try:
                                await ws.close()
                            except Exception as e:
                                logger.debug(f"Qwen TTS interrupted websocket close failed: {e}")
                            ws = None
                    finally:
                        session_ready.clear()
                        current_speech_id = None
                        buffer_committed = False
                        session_configured = False
                        pending_text_buffer = ""
                        qwen_audio_jitter.reset()
                        qwen_audio_jitter.end_interrupt()
                        audio_done.reset()
                        audio_done.end_interrupt()
                    continue

                if sid is None:
                    # 正常结束（非阻塞）：提交缓冲区，但不等待服务器确认、不关闭连接
                    # 音频继续通过 receive_task 流入 response_queue，
                    # 连接由下次 speech_id 切换 / __interrupt__ 关闭
                    if ws and current_speech_id is not None:
                        # 若此轮文本不足 MIN_CHARS 还没发出 session.update，force 一次
                        if not session_configured:
                            if not await _flush_deferred_config(force=True):
                                # flush 失败（session.update 或 append 发失败），连接已死，
                                # 跳过 commit，等待下一个 speech_id 触发重连
                                continue
                        # 短句场景下 session.updated 可能比 _flush 内的 2s 等待更晚到达；
                        # 不再依赖 session_ready，直接发 commit（服务端会在 session.updated
                        # 就绪后按顺序处理 append + commit）。漏 commit 会导致短句静默丢失。
                        if not buffer_committed:
                            try:
                                await ws.send(json.dumps({
                                    "type": "input_text_buffer.commit",
                                    "event_id": f"event_{int(time.time() * 1000)}_commit"
                                }))
                                buffer_committed = True
                            except Exception as e:
                                logger.warning(f"提交缓冲区失败: {e}")
                    continue
                
                # 新的语音ID，重新建立连接（类似 speech_synthesis_worker 的逻辑）
                # 直接关闭旧连接，打断旧语音
                if current_speech_id != sid:
                    current_speech_id = sid
                    buffer_committed = False
                    session_configured = False
                    pending_text_buffer = ""
                    response_done.clear()
                    if ws:
                        try:
                            await ws.close()
                        except:  # noqa: E722
                            pass
                    if receive_task and not receive_task.done():
                        qwen_audio_jitter.flush()
                        receive_task.cancel()
                        try:
                            await receive_task
                        except asyncio.CancelledError:
                            pass
                    # 旧接收任务已完全停止后再重置流式状态：await ws.close() 会让出，
                    # 期间旧 receive_task 可能写入晚到的 audio.delta，若提前重置会被残留污染下一轮
                    resampler.clear()  # 重置重采样器状态（新轮次音频不应与上轮次连续）
                    qwen_audio_jitter.reset()
                    audio_done.reset()  # 新轮次重置 audio_done 去重标记

                    # 建立新连接（延迟 session.update 至首批文本到达后发送，携带语言提示）
                    try:
                        ws = await websockets.connect(tts_url, additional_headers=headers)
                        session_ready.clear()

                        # 启动新的接收任务（合并 session.updated 监听）
                        async def receive_messages(bound_speech_id):
                            nonlocal ws
                            cancelled = False
                            try:
                                async for message in ws:
                                    event = json.loads(message)
                                    event_type = event.get("type")

                                    if event_type in ["session.created", "session.updated"]:
                                        session_ready.set()
                                    elif event_type == "error":
                                        # 空闲超时 / 会话过期：不报 error，标记连接丢失，按需重连
                                        err_msg = event.get("error", {}).get("message", "")
                                        if "request timeout" in err_msg or "session_expired" in err_msg:
                                            logger.debug(f"Qwen TTS 空闲超时，标记连接已断开: {err_msg}")
                                            break
                                        _enqueue_error(response_queue, event)
                                    elif event_type == "response.audio.delta":
                                        try:
                                            audio_bytes = base64.b64decode(event.get("delta", ""))
                                            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
                                            # 使用流式重采样器 24000Hz -> 48000Hz
                                            qwen_audio_jitter.append(_resample_audio(audio_array, 24000, 48000, resampler))
                                        except Exception as e:
                                            logger.error(f"处理音频数据时出错: {e}")
                                    elif event_type in ["response.done", "response.audio.done", "output.done"]:
                                        # 服务器明确表示音频生成完成，设置完成标志
                                        logger.debug(f"收到响应完成事件: {event_type}")
                                        qwen_audio_jitter.flush()
                                        # flush 已经把尾音投进队列，此刻本轮音频流才真正关闭
                                        _emit_audio_done(bound_speech_id)
                                        response_done.set()
                            except websockets.exceptions.ConnectionClosed:
                                pass
                            except asyncio.CancelledError:
                                cancelled = True
                                raise
                            except Exception as e:
                                logger.error(f"消息接收出错: {e}")
                            finally:
                                if not cancelled:
                                    qwen_audio_jitter.flush()
                                # 接收循环退出（超时/断开），清理连接状态以便主循环按需重连
                                if ws:
                                    try:
                                        await ws.close()
                                    except Exception:
                                        pass
                                    ws = None
                                session_ready.clear()
                        
                        receive_task = asyncio.create_task(receive_messages(current_speech_id))

                    except Exception as e:
                        logger.error(f"重新建立连接失败: {e}")
                        if 'HTTP 503' in str(e):
                            _enqueue_error(response_queue, json.dumps({"code": "UPSTREAM_SERVER_BUSY"}))
                        response_queue.put(("__reconnecting__", "TTS_RECONNECTING"))
                        await asyncio.sleep(1.0)
                        continue

                # 检查文本有效性
                if not tts_text or not tts_text.strip():
                    continue

                if not ws:
                    # 连接已因空闲超时断开，暂存当前片段并重置 speech_id 以触发重连
                    # 断线前先冲刷抖动缓冲残留 PCM：重连会走 speech_id 切换分支并 reset()，
                    # 未达阈值的当前轮尾音否则会被清掉；此处仍是同一 speech_id，顺序连续
                    qwen_audio_jitter.flush()
                    current_speech_id = None
                    pending = (sid, tts_text)
                    continue

                # 尚未发送 session.update 时，先缓冲 MIN_CHARS 个字符用于语言检测
                if not session_configured:
                    pending_text_buffer += tts_text
                    ready = await _flush_deferred_config(force=False)
                    if not ready:
                        continue
                    # 已在 _flush_deferred_config 内把 pending_text_buffer 随 append 一起发出
                    continue

                if not session_ready.is_set():
                    # session.update 已发但会话还未就绪（超时/断开），触发重连
                    current_speech_id = None
                    pending = (sid, tts_text)
                    continue

                # 追加文本到缓冲区（不立即提交，等待响应完成时的终止信号再 commit）
                try:
                    await ws.send(json.dumps({
                        "type": "input_text_buffer.append",
                        "event_id": f"event_{int(time.time() * 1000)}",
                        "text": tts_text
                    }))
                    _record_tts_telemetry("qwen", len(tts_text))
                except Exception as e:
                    logger.error(f"发送TTS文本失败: {e}")
                    # 连接已关闭，标记为无效以便下次重连
                    ws = None
                    current_speech_id = None  # 清空ID以强制下次重连
                    session_ready.clear()
                    if receive_task and not receive_task.done():
                        receive_task.cancel()
        
        except Exception as e:
            logger.error(f"Qwen实时TTS Worker错误: {type(e).__name__}: {e!r}", exc_info=True)
            if 'HTTP 503' in str(e):
                _enqueue_error(response_queue, json.dumps({"code": "UPSTREAM_SERVER_BUSY"}))
            response_queue.put(("__ready__", False))
        finally:
            # 清理资源
            if receive_task and not receive_task.done():
                receive_task.cancel()
                try:
                    await receive_task
                except asyncio.CancelledError:
                    pass
            
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
    
    # 运行异步worker
    try:
        asyncio.run(async_worker())
    except Exception as e:
        logger.error(f"Qwen实时TTS Worker启动失败: {type(e).__name__}: {e!r}", exc_info=True)
        response_queue.put(("__ready__", False))
