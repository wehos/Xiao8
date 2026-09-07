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

"""vLLM-Omni TTS worker."""

import numpy as np
import soxr
import json
import websockets
import asyncio

from functools import partial
from urllib.parse import urlparse, urlunparse
from utils.config_manager import _as_bool
from utils.gptsovits_config import redact_url_for_log

from .._infra import (
    AudioDoneEmitter,
    TTS_SHUTDOWN_SENTINEL,
    _enqueue_error,
    _resample_audio,
    configured_tts_unavailable_worker,
    enqueue_configured_tts_failure,
    log_configured_tts_failure,
)
from .._telemetry import _record_tts_telemetry
from .dummy import dummy_tts_worker
from utils.logger_config import get_module_logger

logger = get_module_logger(__name__, "Main")

VLLM_OMNI_DEFAULT_BASE_URL = "ws://localhost:8091/v1"
VLLM_OMNI_DEFAULT_MODEL = "Qwen3-TTS"


def _vllm_omni_normalize_ws_endpoint(base_url: str) -> str:
    """Normalize a vLLM-Omni base URL into the full WebSocket speech endpoint.

    Extracted from the worker body so the clone-preview branch in
    ``characters_router`` can share the same normalization (http→ws,
    path completion, idempotent endpoint assembly) without copy-paste.

    - ``https://`` → ``wss://``, ``http://`` → ``ws://``; ``ws://``/``wss://``
      pass through unchanged.
    - Bare ``host:port`` defaults to ``ws://``.
    - Empty path gets ``/v1`` prepended.
    - Idempotent: if the path already ends with ``/audio/speech/stream``,
      it is not appended again.

    Returns the full ws endpoint, or ``''`` when ``base_url`` is blank (caller
    decides how to surface the missing-config error).
    """
    raw_base_url = (base_url or '').strip().rstrip('/')
    if not raw_base_url:
        return ''

    if raw_base_url.startswith("https://"):
        ws_url = "wss://" + raw_base_url[len("https://"):]
    elif raw_base_url.startswith("http://"):
        ws_url = "ws://" + raw_base_url[len("http://"):]
    elif raw_base_url.startswith(("ws://", "wss://")):
        ws_url = raw_base_url
    else:
        # 裸 host:port 形式，默认 ws
        ws_url = "ws://" + raw_base_url

    parsed = urlparse(ws_url)
    base_path = (parsed.path or "").rstrip("/")
    if base_path in ("", "/"):
        base_path = "/v1"
    # 幂等：若 path 已是完整 endpoint 则不重复拼接
    if not base_path.endswith("/audio/speech/stream"):
        base_path = f"{base_path}/audio/speech/stream"
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            base_path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def vllm_omni_tts_worker(request_queue, response_queue, audio_api_key, voice_id,
                          base_url='', model='', voice='', ref_audio='', ref_text=''):
    """vLLM-Omni TTS worker — full-duplex WebSocket streaming synthesis.

    Protocol: ``ws://{base_url}/v1/audio/speech/stream``

    Client → Server:
      1. ``{"type": "session.config", "model": "...", "voice": "...", ...}``
      2. ``{"type": "input.text", "text": "..."}``  (may be sent multiple times)
      3. ``{"type": "input.done"}``

    Server → Client:
      1. ``{"type": "audio.start", "sentence_index": N, ...}``
      2. <binary frame: PCM 24kHz/16bit/mono>
      3. ``{"type": "audio.done", "sentence_index": N}``
      4. ``{"type": "session.done", "total_sentences": N}``

    Args:
        base_url:  vLLM-Omni service root URL (e.g. ``http://localhost:8091``);
                   automatically rewritten to ws:// scheme.
        model:     Model name (defaults to ``Qwen3-TTS``).
        voice:     Voice id exposed by vllm-omni.
        ref_audio: Voice-clone reference audio as a ``data:audio/...;base64,...``
                   URI. When set, the worker switches to inline-clone mode and
                   forwards it as ``session.config.ref_audio`` (dual to MiMo's
                   inline ``clone_voice``). ⚠ vllm-omni only accepts the field
                   name ``ref_audio`` (NOT ``prompt_audio``).
        ref_text:  The transcript of the reference audio, forwarded as
                   ``session.config.ref_text`` when set. ⚠ field name ``ref_text``
                   (NOT ``prompt_text``).
    """
    raw_base_url = (base_url or '').strip().rstrip('/')
    if not raw_base_url:
        enqueue_configured_tts_failure(
            response_queue, "vllm_omni", "configuration"
        )
        response_queue.put(("__ready__", False))
        return

    # URL 规整 + 补 /v1 + 协议转换（http→ws / 补 endpoint / 幂等），与克隆预览分支
    # 共用 _vllm_omni_normalize_ws_endpoint（见函数 docstring）。
    ws_endpoint = _vllm_omni_normalize_ws_endpoint(raw_base_url)

    effective_model = (model or '').strip() or 'Qwen3-TTS'
    # 克隆模式（内联参考音频，对偶 MiMo 的 clone_voice）：ref_audio 为 data URI、ref_text
    # 为参考音频原文，二者非空时透传进 session.config。⚠ 字段名严格 ref_audio/ref_text，
    # vllm-omni 用错（prompt_audio/prompt_text）会 500。
    effective_ref_audio = (ref_audio or '').strip()
    effective_ref_text = (ref_text or '').strip()
    is_clone = bool(effective_ref_audio)

    # voice 解析：
    # 1. 克隆模式（ref_audio 非空）：忽略 voice_id（N.E.K.O. 内部存储标识如
    #    vllm-omni-clone-ch-xxx，不是 vLLM-Omni 服务端认识的预制音色名），
    #    只使用 voice 参数（clone resolve 传入 'default'）。
    # 2. voice_id 看起来像克隆 ID 但 ref_audio 为空：这是异常状态（resolve 应该
    #    走 clone 分支带 ref_audio，但可能因 voice_meta 缓存/时序问题漏传）。
    #    强制回退到 default voice + 发 warning，避免把克隆 ID 发给服务端导致
    #    Invalid Voice / 服务端崩溃（vLLM-Omni 会把该 ID 解析为 speaker name 并
    #    期望 ref_audio，缺失则 ValueError 崩溃）。
    # 3. 正常 preset：voice_id 优先（对偶其他 worker 的 voice_id→voice 回落）。
    _voice_id_is_clone_id = bool(voice_id and str(voice_id).startswith('vllm-omni-clone-'))
    if is_clone:
        effective_voice = (voice or '').strip() or 'default'
    elif _voice_id_is_clone_id:
        logger.warning(
            "[vLLM-Omni TTS] voice_id='%s' 是克隆音色 ID 但 ref_audio 为空，"
            "回退到 default voice（clone resolve 可能漏传 ref_audio）",
            voice_id,
        )
        effective_voice = (voice or '').strip() or 'default'
    else:
        effective_voice = (voice_id or '').strip() or (voice or '').strip() or 'default'

    logger.info(
        "[vLLM-Omni TTS] ws=%s model=%s voice=%s clone=%s",
        redact_url_for_log(ws_endpoint), effective_model, effective_voice, is_clone,
    )

    async def async_worker():
        ws = None
        receive_task = None
        # 流式重采样器（24kHz→48kHz）
        resampler = soxr.ResampleStream(24000, 48000, 1, dtype='float32')
        # 修复 PR #1764 review #3（CodeRabbit）：会话生命周期状态
        # session.done 后置为 False，下次 input 前重建连接 + 重发 session.config
        session_state = {
            "active": False,
            "awaiting_done": False,
            "speech_id": None,
        }
        pending_text: list[str] = []
        pending_text_sid: str | None = None
        # session.done = 整个 utterance 的音频流关闭（audio.done 是逐句事件，
        # 带 sentence_index，不能当轮次收尾用）。
        audio_done = AudioDoneEmitter(response_queue)

        async def _connect_and_config() -> bool:
            """Open the WS connection and send session.config; return success.

            PR #1764 review #2 (CodeRabbit) fix: forward audio_api_key both via
            the WS handshake Authorization header and the session.config.api_key
            field so deployments behind reverse proxies / auth layers are covered.
            """
            nonlocal ws
            ws_kwargs = {"max_size": None}
            key_for_auth = (audio_api_key or "").strip() if audio_api_key else ""
            if key_for_auth:
                # websockets >= 12: additional_headers；< 12: extra_headers
                ws_kwargs["additional_headers"] = [
                    ("Authorization", f"Bearer {key_for_auth}"),
                ]
            try:
                ws = await websockets.connect(ws_endpoint, **ws_kwargs)
            except TypeError:
                # 兼容旧版本 websockets：参数名退化为 extra_headers
                if "additional_headers" in ws_kwargs:
                    ws_kwargs["extra_headers"] = ws_kwargs.pop("additional_headers")
                try:
                    ws = await websockets.connect(ws_endpoint, **ws_kwargs)
                except Exception:
                    log_configured_tts_failure("vllm_omni", "connect_legacy")
                    return False
            except Exception:
                log_configured_tts_failure("vllm_omni", "connect")
                return False

            try:
                config = {
                    "type": "session.config",
                    "model": effective_model,
                    "voice": effective_voice,
                    "response_format": "pcm",
                    "speed": 1.0,
                    "stream_audio": True,
                    "split_granularity": "sentence",
                }
                # 克隆模式：内联参考音频 + 参考文本（对偶 MiMo 内联 clone_voice）。
                # ⚠ 字段名严格 ref_audio/ref_text，vllm-omni 服务端只认这两个。
                # ⚠ ref_text 必须与 ref_audio 配套发送：单独发 ref_text 而无 ref_audio
                # 会导致 vLLM-Omni 服务端 ValueError 崩溃（服务端把 voice 解析为
                # speaker name 后期望 ref_audio，缺失则报错）。
                if effective_ref_audio:
                    config["ref_audio"] = effective_ref_audio
                    if effective_ref_text:
                        config["ref_text"] = effective_ref_text
                # session 层鉴权（部分自建服务端从 config 读 api_key）
                if key_for_auth:
                    config["api_key"] = key_for_auth
                await ws.send(json.dumps(config))
                return True
            except Exception:
                log_configured_tts_failure("vllm_omni", "session_config")
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None
                return False

        async def _receive_loop():
            """Receive WS messages: JSON events plus binary PCM frames."""
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        # 二进制 PCM 帧：24kHz/16bit/mono → 重采样 48kHz
                        if len(message) < 2:
                            continue
                        audio_array = np.frombuffer(message, dtype=np.int16)
                        response_queue.put(
                            _resample_audio(audio_array, 24000, 48000, resampler)
                        )
                    else:
                        try:
                            event = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        event_type = event.get("type", "")
                        if event_type == "session.done":
                            logger.debug(
                                "[vLLM-Omni TTS] session.done: total_sentences=%s",
                                event.get("total_sentences", "?"),
                            )
                            # 修复 PR #1764 review #3：标记会话结束 + 清重采样器
                            # 主循环在下次 input.text 前会重建连接并重发 session.config
                            # PCM 帧是同一条接收循环里同步 put 的，走到这里说明本轮
                            # 音频已全部投递；先取 sid 再清状态。
                            audio_done.emit(session_state.get("speech_id"))
                            session_state["active"] = False
                            session_state["awaiting_done"] = False
                            session_state["speech_id"] = None
                            try:
                                resampler.clear()
                            except Exception:
                                pass
                        elif event_type == "audio.start":
                            logger.debug(
                                "[vLLM-Omni TTS] audio.start: idx=%s text=%s",
                                event.get("sentence_index"),
                                event.get("sentence_text", "")[:40],
                            )
                        elif event_type == "audio.done":
                            pass  # 逐句完成事件，轮次收尾看 session.done
                        elif event_type == "error":
                            # Provider events may echo signed URLs or input text.
                            # 服务端 error 事件只转换为稳定错误码，不记录原始 payload。
                            enqueue_configured_tts_failure(
                                response_queue, "vllm_omni", "server_event"
                            )
                            # 修复 PR #1764 review 第六轮：服务端 error 事件后会话已不可用，
                            # 标记 session 失效，主循环下次 input 前会主动重建（与 session.done 处理对齐）
                            session_state["active"] = False
                            session_state["awaiting_done"] = False
                            session_state["speech_id"] = None
                            response_queue.put(("__ready__", False))
            except websockets.exceptions.ConnectionClosed:
                was_awaiting_done = bool(session_state.get("awaiting_done"))
                # 修复 PR #1764 review 第六轮：WS 关闭后必须同步本地状态，
                # 否则主循环会试图往已死连接发送，依赖 send 异常才触发重建（噪声+延迟）
                session_state["active"] = False
                session_state["awaiting_done"] = False
                session_state["speech_id"] = None
                if was_awaiting_done:
                    _enqueue_error(response_queue, {
                        "code": "TTS_CONNECTION_FAILED",
                        "provider": "vllm_omni",
                        "message": "vLLM-Omni TTS 连接在 session.done 前关闭",
                    })
                    response_queue.put(("__ready__", False))
            except Exception:
                was_awaiting_done = bool(session_state.get("awaiting_done"))
                log_configured_tts_failure("vllm_omni", "receive")
                session_state["active"] = False
                session_state["awaiting_done"] = False
                session_state["speech_id"] = None
                if was_awaiting_done:
                    _enqueue_error(response_queue, {
                        "code": "TTS_CONNECTION_FAILED",
                        "provider": "vllm_omni",
                        "message": "vLLM-Omni TTS 接收异常，session.done 未完成",
                    })
                    response_queue.put(("__ready__", False))

        # 首次连接 + 就绪信号
        if not await _connect_and_config():
            _enqueue_error(response_queue, {
                "code": "TTS_CONNECTION_FAILED",
                "provider": "vllm_omni",
                "message": "vLLM-Omni TTS 初始连接失败",
            })
            response_queue.put(("__ready__", False))
            return

        session_state["active"] = True  # 修复 PR #1764 review #3
        receive_task = asyncio.create_task(_receive_loop())
        response_queue.put(("__ready__", True))
        logger.info("[vLLM-Omni TTS] 已就绪")

        async def _rebuild_session() -> bool:
            """PR #1764 review #3 helper: tear down the old session and rebuild a new one.

            Called after session.done / on ws.send failure / on __interrupt__.
            Returns True on success, False on failure (outer loop should stop).
            """
            nonlocal ws, receive_task
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                try:
                    await receive_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            receive_task = None
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None
            try:
                resampler.clear()
            except Exception:
                pass
            if not await _connect_and_config():
                session_state["active"] = False
                session_state["awaiting_done"] = False
                session_state["speech_id"] = None
                return False
            session_state["active"] = True
            session_state["awaiting_done"] = False
            session_state["speech_id"] = None
            audio_done.reset()  # 新会话重置 audio_done 去重标记
            receive_task = asyncio.create_task(_receive_loop())
            return True

        async def _replay_pending_text() -> bool:
            if not pending_text:
                return True
            try:
                replay_text = "".join(pending_text)
                await ws.send(json.dumps({
                    "type": "input.text",
                    "text": replay_text,
                }))
                session_state["speech_id"] = pending_text_sid
                session_state["awaiting_done"] = False
                return True
            except Exception:
                log_configured_tts_failure("vllm_omni", "replay_pending")
                session_state["active"] = False
                return False

        def _fail_pending_flush(message: str):
            nonlocal pending_text_sid
            pending_text.clear()
            pending_text_sid = None
            session_state["active"] = False
            session_state["awaiting_done"] = False
            session_state["speech_id"] = None
            _enqueue_error(response_queue, {
                "code": "TTS_CONNECTION_FAILED",
                "provider": "vllm_omni",
                "message": message,
            })
            response_queue.put(("__ready__", False))

        loop = asyncio.get_running_loop()

        while True:
            try:
                sid, tts_text = await loop.run_in_executor(None, request_queue.get)
            except Exception:
                break

            if sid == TTS_SHUTDOWN_SENTINEL:
                break

            if sid == "__interrupt__":
                # 修复 PR #1764 review 第二轮 #3：打断时只销毁当前连接、把 session 标记失效，
                # 不立刻重连——避免上游短暂不可用时一次失败就把整个 worker 退出。
                # 实际重连延迟到下一条输入到来时由活跃性检查（while 循环下方）处理。
                audio_done.begin_interrupt()  # 打断轮不发 audio_done（走独立 cancel 通道）
                # try/finally 与 step/qwen/grok/elevenlabs/gptsovits 对偶：拆卸段里
                # 任何意外抛出都不能把 emitter 永久停在 interrupted 上，否则本会话
                # 之后所有轮都静默漏发 audio_done。
                try:
                    if receive_task is not None and not receive_task.done():
                        receive_task.cancel()
                        try:
                            await receive_task
                        except asyncio.CancelledError:
                            # 这个 cancel 是上一行自己发的，正常路径
                            pass
                        except Exception:
                            # 接收协程临终抛的异常与本次打断无关，吞掉继续拆卸
                            pass
                    receive_task = None
                    if ws is not None:
                        try:
                            await ws.close()
                        except Exception:
                            # 连接马上要丢弃，close 失败不影响拆卸
                            pass
                        ws = None
                    try:
                        resampler.clear()
                    except Exception:
                        # 重采样器状态下一轮会重建，清不掉也不该拦住打断
                        pass
                    session_state["active"] = False
                    session_state["awaiting_done"] = False
                    # 与其他失效路径（_receive_loop / _rebuild_session / _fail_pending_flush /
                    # sid 切换）对齐，同步清理 speech_id，避免中断后残留旧 utterance id。
                    session_state["speech_id"] = None
                    pending_text.clear()
                    pending_text_sid = None
                    audio_done.reset()
                finally:
                    audio_done.end_interrupt()
                continue

            if sid is None:
                if not pending_text:
                    continue
                if not session_state["active"] or ws is None:
                    logger.info("[vLLM-Omni TTS] 会话已结束/失效，重建连接以发送 flush")
                    if not await _rebuild_session():
                        logger.error("[vLLM-Omni TTS] 重建会话失败，标记 worker 未就绪")
                        _fail_pending_flush("vLLM-Omni TTS flush 重连失败")
                        break
                    if not await _replay_pending_text():
                        _fail_pending_flush("vLLM-Omni TTS flush 重放失败")
                        break
                if ws is not None:
                    try:
                        await ws.send(json.dumps({"type": "input.done"}))
                        pending_text.clear()
                        pending_text_sid = None
                        session_state["awaiting_done"] = True
                    except Exception:
                        log_configured_tts_failure("vllm_omni", "input_done")
                        session_state["active"] = False
                        session_state["awaiting_done"] = False
                        if not await _rebuild_session():
                            _fail_pending_flush("vLLM-Omni TTS flush 重连失败")
                            break
                        try:
                            if not await _replay_pending_text():
                                _fail_pending_flush("vLLM-Omni TTS flush 重放失败")
                                break
                            await ws.send(json.dumps({"type": "input.done"}))
                            pending_text.clear()
                            pending_text_sid = None
                            session_state["awaiting_done"] = True
                            logger.info("[vLLM-Omni TTS] 重放 pending_text 并重发 input.done 成功")
                        except Exception:
                            log_configured_tts_failure("vllm_omni", "input_done_retry")
                            _fail_pending_flush("vLLM-Omni TTS flush 重发失败")
                            break
                else:
                    _fail_pending_flush("vLLM-Omni TTS flush 连接不可用")
                    break
                continue

            # 修复 PR #1764 review #3：发送前检查会话是否仍然可复用
            # active session 只能承载同一个 utterance；sid 切换时先重建，避免串音
            if (
                session_state["active"]
                and session_state.get("speech_id") not in (None, sid)
            ):
                logger.info(
                    "[vLLM-Omni TTS] 收到新 sid=%s，重建会话避免跨 utterance 复用",
                    sid,
                )
                pending_text.clear()
                pending_text_sid = None
                session_state["active"] = False
            if not session_state["active"] or ws is None:
                logger.info("[vLLM-Omni TTS] 会话已结束/失效，重建连接以发送新输入")
                if not await _rebuild_session():
                    logger.error("[vLLM-Omni TTS] 重建会话失败，标记 worker 未就绪")
                    _enqueue_error(response_queue, {
                        "code": "TTS_CONNECTION_FAILED",
                        "provider": "vllm_omni",
                        "message": "vLLM-Omni TTS 重连失败",
                    })
                    response_queue.put(("__ready__", False))
                    break
                if pending_text and pending_text_sid == sid:
                    if not await _replay_pending_text():
                        _fail_pending_flush("vLLM-Omni TTS input.text 重连后重放失败")
                        break

            if tts_text and tts_text.strip() and ws is not None:
                if pending_text and pending_text_sid not in (None, sid):
                    logger.debug(
                        "[vLLM-Omni TTS] 丢弃跨 utterance 的 pending_text (sid=%s, current=%s, len=%d)",
                        pending_text_sid,
                        sid,
                        sum(len(part) for part in pending_text),
                    )
                    pending_text.clear()
                    pending_text_sid = None
                payload = json.dumps({
                    "type": "input.text",
                    "text": tts_text,
                })
                try:
                    await ws.send(payload)
                    _record_tts_telemetry(effective_model, len(tts_text))
                    pending_text.append(tts_text)
                    pending_text_sid = sid
                    session_state["speech_id"] = sid
                    session_state["awaiting_done"] = False
                except Exception:
                    log_configured_tts_failure("vllm_omni", "input_text")
                    session_state["active"] = False
                    session_state["awaiting_done"] = False
                    if await _rebuild_session():
                        try:
                            if not await _replay_pending_text():
                                _fail_pending_flush("vLLM-Omni TTS input.text 重放失败")
                                break
                            await ws.send(payload)
                            _record_tts_telemetry(effective_model, len(tts_text))
                            pending_text.append(tts_text)
                            pending_text_sid = sid
                            session_state["speech_id"] = sid
                            session_state["awaiting_done"] = False
                            logger.info("[vLLM-Omni TTS] 重发 input.text 成功")
                        except Exception:
                            log_configured_tts_failure("vllm_omni", "input_text_retry")
                            session_state["active"] = False
                            _enqueue_error(response_queue, {
                                "code": "TTS_CONNECTION_FAILED",
                                "provider": "vllm_omni",
                                "message": "vLLM-Omni TTS 发送失败",
                            })
                            response_queue.put(("__ready__", False))
                            break
                    else:
                        _enqueue_error(response_queue, {
                            "code": "TTS_CONNECTION_FAILED",
                            "provider": "vllm_omni",
                            "message": "vLLM-Omni TTS 重连失败",
                        })
                        response_queue.put(("__ready__", False))
                        break

        # 清理
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

    try:
        asyncio.run(async_worker())
    except Exception:
        log_configured_tts_failure("vllm_omni", "worker_start")
        response_queue.put(("__ready__", False))

# ── vLLM-Omni（本地 self-hosted 服务）────────────────────────────────────────
# 两种选中机制（设计文档 §3.1）合并在一个 provider 条目里，照搬 MiMo 的做法：
#   1. 配置选中（preset）——用户在下拉显式选 ttsModelProvider=='vllm_omni'，走预制/用户填的
#      voice id，不带克隆参考音频。
#   2. 音色元数据选中（clone）——用户挑了某个 vLLM-Omni 克隆音色（voice_meta.provider==
#      'vllm_omni'），对偶 MiMo 的克隆路由。vLLM-Omni 克隆没有远端 voice_id：参考音频存在本地
#      voice_meta（clone_sample_b64），dispatch 时读出来内联进 session.config 的 ref_audio。

def _vllm_omni_voice_meta_is_clone(vm) -> bool:
    return bool(vm and vm.get('provider') == 'vllm_omni')

def _build_vllm_omni_clone_data_uri(voice_meta) -> str | None:
    """Build the ``data:`` reference-audio URI for a vLLM-Omni clone from its
    voice_meta (the clone identity lives entirely in voice_storage.json — the
    sample base64 is stored inline, dual to MiMo's ``clone_sample_b64``), or None
    when absent.

    The stored value is already base64, so this only frames it as a data URI —
    no decode/re-encode.
    """
    b64 = str((voice_meta or {}).get('clone_sample_b64') or '').strip()
    if not b64:
        return None
    mime = str((voice_meta or {}).get('clone_sample_mime') or '').strip() or 'audio/wav'
    return f"data:{mime};base64,{b64}"

def _vllm_omni_is_selected(ctx) -> bool:
    # 配置选中（preset）优先判定，且**必须先于任何 ctx.voice_meta 访问**——voice_meta 是惰性的，
    # 显式选中 vllm_omni（下拉默认）时不能触发 voice_meta 加载，否则违反
    # test_get_tts_worker_routes_explicit_vllm_before_cloned_voice 的短路契约（对偶 _mimo_is_selected
    # 的顺序：config-selected 先判，clone-meta 后判）。
    core_config = ctx.core_config
    if _as_bool(core_config.get('ENABLE_CUSTOM_API'), False):
        # Selection uses the existing sanitized snapshot; sensitive credentials
        # remain a resolver-only disk read.
        # 选中判断只读现有快照，API Key 仍只在 resolve 阶段读取原始配置。
        if 'ttsModelProvider' in core_config:
            configured_provider = str(core_config.get('ttsModelProvider') or '').strip()
        else:
            try:
                raw = ctx.cm.load_json_config('core_config.json', {}) or {}
                configured_provider = str(raw.get('ttsModelProvider') or '').strip()
            except Exception:
                # Selection errors use the same redacted diagnostic contract.
                # 选中判定失败也不能把配置异常原文写入后端日志。
                log_configured_tts_failure("vllm_omni", "selection")
        if configured_provider == 'vllm_omni':
            return True
    # 克隆音色选中：按所选音色的 voice_meta.provider 路由（惰性，命中前面 config-selected
    # provider / 本 provider 的 config 分支时不会触发 voice_meta 加载）。
    return _vllm_omni_voice_meta_is_clone(ctx.voice_meta)

def _vllm_omni_clone_is_selected(ctx) -> bool:
    """Clone-only selection predicate (for symmetry with _mimo dispatch / tests)."""
    return _vllm_omni_voice_meta_is_clone(ctx.voice_meta)

def _vllm_omni_clone_resolve(ctx):
    """Resolve a vLLM-Omni *clone* voice to its worker (inline reference audio).

    Dual to ``_mimo_resolve``'s clone branch: reads the reference-audio sample
    from ``voice_meta`` and builds a data URI to inline into
    ``session.config.ref_audio``; reads ``clone_ref_text`` as ``ref_text``.
    vLLM-Omni is a local service with no API key — the api_key override returns
    an empty string (consistent with ``_vllm_omni_resolve``'s credential-leak
    prevention: never fall back to another provider's key).
    """
    cm = ctx.cm
    vm = ctx.voice_meta or {}
    clone_uri = _build_vllm_omni_clone_data_uri(vm)
    if not clone_uri:
        logger.warning(
            "vLLM-Omni 克隆音色 %s 缺少参考音频样本，改用 dummy TTS worker", ctx.voice_id)
        return dummy_tts_worker, None, None
    try:
        raw = cm.load_json_config('core_config.json', {})
    except Exception:
        raw = {}
    # 已启用 vLLM-Omni 时，当前配置就是唯一的运行端点：这样旧克隆里意外留存的
    # follow_assist URL 不会覆盖用户后来切换到的 vLLM 服务。未启用时才保留历史快照
    # 作为兼容回退；这不会阻止本地保存，只会让未配置的克隆在运行时按原有方式失败。
    raw_vllm_omni_selected = (
        _as_bool(raw.get('enableCustomApi'), False)
        and str(raw.get('ttsModelProvider') or '').strip() == 'vllm_omni'
    )
    current_vllm_url = (
        str(raw.get('ttsModelUrl') or raw.get('TTS_MODEL_URL') or '').strip()
        if raw_vllm_omni_selected
        else ''
    )
    # An explicitly selected vLLM-Omni provider owns the endpoint choice. Its
    # blank URL is the supported localhost default, not a signal to reuse a
    # stale clone snapshot captured while TTS followed another provider.
    if raw_vllm_omni_selected:
        vllm_url = current_vllm_url or VLLM_OMNI_DEFAULT_BASE_URL
    else:
        vllm_url = (
            str(vm.get('vllm_omni_base_url') or '').strip()
            or (raw.get('ttsModelUrl') or '').strip()
            or VLLM_OMNI_DEFAULT_BASE_URL
        )
    vllm_model = (raw.get('ttsModelId') or '').strip() or VLLM_OMNI_DEFAULT_MODEL
    clone_ref_text = str(vm.get('clone_ref_text') or '').strip()
    if not clone_ref_text:
        # ref_text 与参考音频严格对应是 vLLM-Omni 克隆音质的前提；前端注册入口已强制必填
        # （voice_clone.js），但旧数据 / 直接编辑 voice_storage 可能缺失。空 ref_text 仍可
        # 合成（不阻塞），仅音质下降——记 warning 提供可观测性。
        logger.warning(
            "vLLM-Omni 克隆音色 %s 缺少 ref_text，克隆音质可能下降", ctx.voice_id)
    worker = partial(
        vllm_omni_tts_worker,
        base_url=vllm_url, model=vllm_model, voice='default',
        ref_audio=clone_uri, ref_text=clone_ref_text,
    )
    # 凭证防泄漏：与 preset 路径一致，读 ttsModelApiKey；无 key 返回空串，禁止 fallback
    # 到别家 provider 的 key（见 _vllm_omni_resolve L713 同源逻辑）。
    vllm_key = (raw.get('ttsModelApiKey') or '').strip()
    return worker, vllm_key, 'vllm_omni'

def _vllm_omni_resolve(ctx):
    cm = ctx.cm
    try:
        raw = cm.load_json_config('core_config.json', {})
    except Exception:
        log_configured_tts_failure("vllm_omni", "configuration")
        # Preserve WSS/HTTPS provider symmetry: both report setup failure through
        # the same ready channel before core activates the existing fallback.
        # WSS 与 HTTPS 配置读取失败都先回报原 provider，再进入同一保底接缝。
        return configured_tts_unavailable_worker, "", "vllm_omni"

    def _config_value(key):
        # Selection and resolution must use one session snapshot. Disk is only
        # a compatibility fallback when an older snapshot omits the field.
        # 选中与解析共用会话快照；仅旧快照缺字段时回退磁盘配置。
        if key in ctx.core_config:
            return ctx.core_config.get(key)
        return raw.get(key)

    configured_voice = str(_config_value('ttsVoiceId') or '').strip() or 'default'
    configured_provider = str(_config_value('ttsModelProvider') or '').strip()
    config_selected = (
        _as_bool(ctx.core_config.get('ENABLE_CUSTOM_API'), False)
        and configured_provider == 'vllm_omni'
    )
    raw_provider = str(raw.get('ttsModelProvider') or '').strip()
    if config_selected and raw_provider and raw_provider != configured_provider:
        # The raw credential may already belong to a newly selected provider.
        # Never send it to the endpoint retained by this session snapshot.
        # 磁盘中的凭证可能已属于新 provider，禁止发往旧会话快照的端点。
        log_configured_tts_failure("vllm_omni", "configuration")
        return configured_tts_unavailable_worker, "", "vllm_omni"
    # The exact configured preset owns a same-ID collision; a different clone
    # still keeps the historical clone-first behavior.
    # 仅当角色音色与配置 Voice ID 精确相同时，显式配置才压过同名克隆；
    # 其他克隆仍沿用原有优先级。
    configured_preset_selected = (
        config_selected
        and str(ctx.voice_id or '').strip() == configured_voice
    )

    # 克隆音色通常优先于配置默认（preset）：用户选了不同的克隆音色 = 明确意图用克隆，
    # 即使配置了 vllm_omni 作为默认 provider 也应走 clone resolve。之前的
    # `not config_selected` 守卫导致"配置选了 vllm_omni + 用户选了克隆音色"时走
    # preset 路径，把克隆音色的内部 ID（如 vllm-omni-clone-ch-xxx）当作 voice
    # 发给 vLLM-Omni 服务端，服务端报 Invalid Voice（该 ID 是 N.E.K.O. 本地
    # 存储标识，不是 vLLM-Omni 的预制音色名）。config_selected 守卫的原始目的是
    # 避免触发 voice_meta 惰性加载（短路契约），但 _vllm_omni_is_selected 已在
    # config-selected 后惰性检查 voice_meta，resolve 到达时 is_selected 已返回
    # True，voice_meta 已加载完毕，不存在短路问题。
    if (
        _vllm_omni_voice_meta_is_clone(ctx.voice_meta)
        and not configured_preset_selected
    ):
        return _vllm_omni_clone_resolve(ctx)
    vllm_url = str(_config_value('ttsModelUrl') or '').strip() or VLLM_OMNI_DEFAULT_BASE_URL
    vllm_model = str(_config_value('ttsModelId') or '').strip() or VLLM_OMNI_DEFAULT_MODEL
    vllm_voice = configured_voice
    # Credentials remain a resolver-only raw read and never enter the session
    # snapshot; an empty key must still block fallback to another provider's key.
    # 凭证仍只在 resolve 阶段读原始配置，不进会话快照；空 key 也禁止借用其他 provider 凭证。
    # 无 key 时返回空字符串而非 None，配合 core.resolve_tts_api_key
    # 的 provider_key=='vllm_omni' 特判，禁止 fallback 到别家 provider 的 key
    # （见 get_tts_worker 原注释 / PR #1764 review 第三轮 #3）。
    vllm_key = (raw.get('ttsModelApiKey') or '').strip()
    worker = partial(
        vllm_omni_tts_worker,
        base_url=vllm_url, model=vllm_model, voice=vllm_voice,
    )
    return worker, vllm_key, 'vllm_omni'
