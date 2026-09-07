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

"""Voice Clone endpoints: upload trimming/silence analysis and direct-link clone.

Split out of the former monolithic ``main_routers/characters_router.py``.
"""

from ._shared import MAX_UPLOAD_SIZE, _UploadTooLargeError, _read_limited_stream, logger, router
from .direct_link import (
    DirectLinkSecurityError,
    _download_direct_link_audio,
    _request_direct_link_follow_redirects,
    _validate_direct_link_target,
)
from .voice_providers import (
    ElevenLabsUpstreamError,
    _elevenlabs_clone_voice,
    _is_local_voice_clone_tts_config,
    _local_voice_clone_tts_base_url,
)

import io
import re
import asyncio
import base64
import hashlib
from datetime import datetime
from fastapi import Request, File, UploadFile, Form
from fastapi.responses import JSONResponse
import httpx
from ..shared_state import (
    get_config_manager,
)
from utils.config_manager import _as_bool
from utils.audio import normalize_voice_clone_api_audio, validate_audio_file
from utils.doubao_tts import (
    DOUBAO_TTS_DEFAULT_BASE_URL,
    DOUBAO_TTS_DEFAULT_RESOURCE_ID,
    DOUBAO_VOICE_STORAGE_KEY,
    DoubaoTtsError,
    DoubaoVoiceCloneClient,
)
from utils.voice_clone import (
    MinimaxVoiceCloneClient,
    MinimaxVoiceCloneError,
    minimax_normalize_language,
    MimoVoiceCloneClient,
    MimoVoiceCloneError,
    QwenVoiceCloneClient,
    QwenVoiceCloneError,
    qwen_language_hints,
)
from utils.tts.providers.minimax import (
    build_minimax_request_voice_id,
    get_minimax_base_url,
    get_minimax_storage_prefix,
)
from utils.tts.providers.mimo import MIMO_VOICE_STORAGE_KEY
from config import (
    TFLINK_UPLOAD_URL,
)


# ==================== 智能静音移除 ====================
# 用于存储裁剪任务状态的全局字典
_trim_tasks: dict[str, dict] = {}


@router.post('/audio/analyze_silence')
async def analyze_silence(file: UploadFile = File(...)):
    """
    Analyze silence segments in the uploaded audio.

    Returns:
        - original_duration / original_duration_ms: total duration of the original audio
        - silence_duration / silence_duration_ms: total detected silence (total_silence_ms)
        - removable_silence / removable_silence_ms: silence that can actually be removed
        - estimated_duration / estimated_duration_ms: estimated remaining duration after processing
        - saving_percentage: savings percentage (based on the actually removable amount)
        - silence_segments: list of silence segments [{start_ms, end_ms, duration_ms}]
        - has_silence: whether removable silence was detected
    """
    from utils.audio_silence_remover import (
        detect_silence, convert_to_wav_if_needed, format_duration_mmss
    )

    try:
        file_buffer = await _read_limited_stream(file, MAX_UPLOAD_SIZE)
    except _UploadTooLargeError as e:
        return JSONResponse({'error': str(e)}, status_code=413)
    except Exception as e:
        logger.error(f"读取音频文件失败: {e}")
        return JSONResponse({'error': f'读取文件失败: {e}'}, status_code=500)

    try:
        # 转换为 WAV（如果需要）— 阻塞操作，放到线程中执行
        wav_buffer, _ = await asyncio.to_thread(convert_to_wav_if_needed, file_buffer, file.filename)

        # 执行静音检测
        analysis = await asyncio.to_thread(detect_silence, wav_buffer)

        return JSONResponse({
            'success': True,
            'original_duration': format_duration_mmss(analysis.original_duration_ms),
            'original_duration_ms': round(analysis.original_duration_ms, 1),
            'silence_duration': format_duration_mmss(analysis.total_silence_ms),
            'silence_duration_ms': round(analysis.total_silence_ms, 1),
            'removable_silence': format_duration_mmss(analysis.removable_silence_ms),
            'removable_silence_ms': round(analysis.removable_silence_ms, 1),
            'estimated_duration': format_duration_mmss(analysis.estimated_duration_ms),
            'estimated_duration_ms': round(analysis.estimated_duration_ms, 1),
            'saving_percentage': analysis.saving_percentage,
            'silence_segments': [
                {
                    'start_ms': round(s.start_ms, 1),
                    'end_ms': round(s.end_ms, 1),
                    'duration_ms': round(s.duration_ms, 1),
                }
                for s in analysis.silence_segments
            ],
            'has_silence': len(analysis.silence_segments) > 0,
            'sample_rate': analysis.sample_rate,
            'sample_width': analysis.sample_width,
            'channels': analysis.channels,
        })
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"静音分析失败: {e}")
        return JSONResponse({'error': f'静音分析失败: {str(e)}'}, status_code=500)


@router.post('/audio/trim_silence')
async def trim_silence_endpoint(file: UploadFile = File(...), task_id: str | None = Form(default=None)):
    """
    Perform silence trimming and return the processed audio.

    Analyzes silence segments first, then shrinks over-long silences down to 200ms (cut from the middle).
    Returns the processed WAV file (base64-encoded) plus an MD5 checksum.
    """
    import uuid
    import base64 as b64
    from utils.audio_silence_remover import (
        detect_silence, trim_silence, convert_to_wav_if_needed,
        format_duration_mmss, CancelledError
    )

    if task_id:
        try:
            uuid.UUID(task_id)
        except ValueError:
            return JSONResponse({'error': '无效的 task_id 格式'}, status_code=400)
        if task_id in _trim_tasks:
            return JSONResponse({'error': '该 task_id 已存在'}, status_code=409)
    else:
        task_id = str(uuid.uuid4())

    # 立即占位，防止 TOCTOU 竞态
    _trim_tasks[task_id] = {'progress': 0, 'cancelled': False, 'phase': 'queued'}

    try:
        file_buffer = await _read_limited_stream(file, MAX_UPLOAD_SIZE)
    except _UploadTooLargeError as e:
        _trim_tasks.pop(task_id, None)
        return JSONResponse({'error': str(e)}, status_code=413)
    except Exception as e:
        _trim_tasks.pop(task_id, None)
        logger.error(f"读取音频文件失败: {e}")
        return JSONResponse({'error': f'读取文件失败: {e}'}, status_code=500)

    try:
        # 文件读取完成，切换到分析阶段
        _trim_tasks[task_id]['phase'] = 'analyzing'

        def progress_cb(pct: int):
            task = _trim_tasks.get(task_id)
            if task is None:
                return
            if task.get('phase', 'analyzing') == 'analyzing':
                # 分析阶段占 0-40%
                task['progress'] = int(pct * 0.4)
            else:
                # 裁剪阶段占 40-100%
                task['progress'] = 40 + int(pct * 0.6)

        def cancel_check() -> bool:
            return _trim_tasks.get(task_id, {}).get('cancelled', False)

        # 转换为 WAV — 阻塞操作，放到线程中执行
        wav_buffer, _ = await asyncio.to_thread(convert_to_wav_if_needed, file_buffer, file.filename)

        # 分析静音
        analysis = await asyncio.to_thread(
            detect_silence, wav_buffer,
            progress_callback=progress_cb, cancel_check=cancel_check,
        )

        if not analysis.silence_segments:
            # 没有可移除的静音
            _trim_tasks.pop(task_id, None)
            return JSONResponse({
                'success': True,
                'has_changes': False,
                'message': '未检测到可移除的静音段',
                'task_id': task_id,
            })

        # 切换到裁剪阶段
        if task_id in _trim_tasks:
            _trim_tasks[task_id]['phase'] = 'trimming'

        # 执行裁剪
        result = await asyncio.to_thread(
            trim_silence, wav_buffer, analysis,
            progress_callback=progress_cb, cancel_check=cancel_check,
        )

        # 编码为 base64
        audio_b64 = b64.b64encode(result.audio_data).decode('ascii')

        # 清理任务
        _trim_tasks.pop(task_id, None)

        return JSONResponse({
            'success': True,
            'has_changes': True,
            'task_id': task_id,
            'audio_base64': audio_b64,
            'md5': result.md5,
            'original_duration': format_duration_mmss(result.original_duration_ms),
            'original_duration_ms': round(result.original_duration_ms, 1),
            'trimmed_duration': format_duration_mmss(result.trimmed_duration_ms),
            'trimmed_duration_ms': round(result.trimmed_duration_ms, 1),
            'removed_silence_ms': round(result.removed_silence_ms, 1),
            'sample_rate': result.sample_rate,
            'sample_width': result.sample_width,
            'channels': result.channels,
            'filename': f"trimmed_{file.filename}",
        })

    except CancelledError:
        _trim_tasks.pop(task_id, None)
        return JSONResponse({
            'success': False,
            'cancelled': True,
            'message': '任务已被用户取消',
            'task_id': task_id,
        })
    except ValueError as e:
        _trim_tasks.pop(task_id, None)
        return JSONResponse({'error': str(e)}, status_code=400)
    except Exception as e:
        _trim_tasks.pop(task_id, None)
        logger.error(f"静音裁剪失败: {e}")
        return JSONResponse({'error': f'静音裁剪失败: {str(e)}'}, status_code=500)


@router.get('/audio/trim_progress/{task_id}')
async def get_trim_progress(task_id: str):
    """Get trim task progress."""
    task = _trim_tasks.get(task_id)
    if not task:
        return JSONResponse({'exists': False, 'progress': 100, 'phase': 'done'})
    return JSONResponse({
        'exists': True,
        'progress': task.get('progress', 0),
        'phase': task.get('phase', 'unknown'),
        'cancelled': task.get('cancelled', False),
    })


@router.post('/audio/trim_cancel/{task_id}')
async def cancel_trim_task(task_id: str):
    """Cancel a trim task."""
    task = _trim_tasks.get(task_id)
    if task:
        task['cancelled'] = True
        return JSONResponse({'success': True, 'message': '取消请求已发送'})
    return JSONResponse({'success': False, 'message': '任务不存在或已完成'})


def _normalize_doubao_voice_clone_speaker_id(value: str) -> str:
    speaker_id = str(value or '').strip()
    if not re.fullmatch(r"S_[A-Za-z0-9]+", speaker_id):
        raise ValueError("豆包声音复刻需要填写 S_ 开头的 Speaker ID")
    return speaker_id


@router.post('/voice_clone')
async def voice_clone(
    file: UploadFile = File(...),
    prefix: str = Form(...),
    ref_language: str = Form(default="ch"),
    provider: str = Form(default="cosyvoice"),
    ref_text: str = Form(default=""),
):
    """
    Voice cloning endpoint.

    Parameters:
        file: audio file
        prefix: voice prefix name
        ref_language: language of the reference audio; one of: ch, en, fr, de, ja, ko, ru
                      Note: this is the language of the reference audio, not the target voice
        provider: service provider; one of: cosyvoice (Alibaba Bailian), cosyvoice_intl (Alibaba international), minimax (China), minimax_intl (international), elevenlabs, mimo, vllm_omni
        ref_text: transcript of the reference audio (vLLM-Omni inline clone only; must
                  correspond strictly to the audio content)
    """
    # 流式读取上传文件（带大小限制）并增量计算 MD5
    try:
        file_buffer = await _read_limited_stream(file, MAX_UPLOAD_SIZE)
    except _UploadTooLargeError as e:
        return JSONResponse({'error': str(e)}, status_code=413)
    except Exception as e:
        logger.error(f"读取文件到内存失败: {e}")
        return JSONResponse({'error': f'读取文件失败: {e}'}, status_code=500)

    audio_md5 = hashlib.md5(file_buffer.getvalue()).hexdigest()

    # 提前规范化 provider 和 ref_language
    provider = provider.lower().strip() if provider else 'cosyvoice'
    valid_languages = ['ch', 'en', 'fr', 'de', 'ja', 'ko', 'ru']
    ref_language = ref_language.lower().strip() if ref_language else 'ch'
    if ref_language not in valid_languages:
        ref_language = 'ch'

    # vLLM-Omni 克隆必填 ref_text：vLLM-Omni 服务端要求 ref_audio 与 ref_text 严格对应，
    # 缺失 ref_text 会导致合成失败（服务端 ValueError）。前端 voice_clone.js 也做了
    # 同步校验（L1484-1491），后端补上防止绕过前端直接调 API。
    vllm_ref_text = ref_text.strip() if ref_text else ''
    if provider == 'vllm_omni':
        if not vllm_ref_text:
            return JSONResponse(
                {'error': 'vLLM-Omni 克隆必须填写参考音频原文（ref_text）', 'provider': provider},
                status_code=400,
            )
        if len(vllm_ref_text) > 100:
            return JSONResponse(
                {'error': 'vLLM-Omni 参考音频原文过长，请控制在 100 字以内', 'provider': provider},
                status_code=400,
            )

    # 检测是否使用本地 TTS（ws/wss 协议）
    _config_manager = get_config_manager()
    # 先取 core_config 快照，再让 tts_custom 从同一份解析：两次独立的读之间若发生
    # /core_api 保存，_local_voice_clone_tts_base_url 会用旧 tts_config 的 URL，而
    # _is_local_voice_clone_tts_config 读的是新的 ttsModelProvider，本地 TTS 注册
    # 路径可能被跳过或打到旧端点。
    try:
        core_config = await _config_manager.aget_core_config() or {}
    except Exception:
        core_config = {}
    # 快照拿不到时退回独立读，保持原有容错（不能把 {} 当快照传下去）
    tts_config = await (
        _config_manager.aget_model_api_config('tts_custom', core_config=core_config)
        if core_config
        else _config_manager.aget_model_api_config('tts_custom')
    )
    base_url = _local_voice_clone_tts_base_url(tts_config, core_config)
    is_local_tts = _is_local_voice_clone_tts_config(tts_config, core_config)

    # vLLM-Omni uses an inline reference sample and has no /speakers/register
    # endpoint. Even when another active TTS provider is a local WS service,
    # its generic registration route must not consume a vLLM-Omni clone.
    if is_local_tts and provider != 'vllm_omni':
        # ==================== 本地 TTS 注册流程 ====================
        # MD5 + ref_language 去重：检查是否已有相同音频 + 相同语言注册过的音色
        existing = _config_manager.find_voice_by_audio_md5('__LOCAL_TTS__', audio_md5, ref_language)
        if existing:
            voice_id, voice_data = existing
            logger.info(f"本地 TTS 音频 MD5 命中，复用 voice_id: {voice_id}")
            return JSONResponse({
                'voice_id': voice_id,
                'message': '已复用现有音色，跳过上传',
                'reused': True,
                'is_local': True
            })

        # 将 ws(s):// 转换为 http(s):// 用于 REST API 调用
        if base_url.startswith('wss://'):
            http_base = 'https://' + base_url[6:]
        else:
            http_base = 'http://' + base_url[5:]

        # 移除可能的 /v1/audio/speech/stream 路径，只保留主机部分
        # 例如: ws://127.0.0.1:50000/v1/audio/speech/stream -> http://127.0.0.1:50000
        if '/v1/' in http_base:
            http_base = http_base.split('/v1/')[0]

        register_url = f"{http_base}/v1/speakers/register"
        logger.info(f"使用本地 TTS 注册: {register_url}")

        try:
            file_buffer.seek(0)

            # 根据用户 demo，API 格式：
            # POST /v1/speakers/register
            # multipart/form-data: speaker_id, prompt_text, prompt_audio
            files = {
                'prompt_audio': (file.filename, file_buffer, 'audio/wav')
            }
            data = {
                'speaker_id': prefix,
                'prompt_text': f"<|{ref_language}|>" if ref_language != 'ch' else "希望你以后能够做的比我还好呦。"
            }

            # per-call AsyncClient: 用户手动上传音色文件触发，冷路径
            async with httpx.AsyncClient(timeout=60, proxy=None, trust_env=False) as client:
                resp = await client.post(register_url, data=data, files=files)

                if resp.status_code == 200:
                    result = resp.json()
                    voice_id = prefix  # 本地 TTS 使用 speaker_id 作为 voice_id

                    # 保存到本地音色库（使用特殊的 key 标识本地 TTS）
                    voice_data = {
                        'voice_id': voice_id,
                        'prefix': prefix,
                        'provider': 'local',
                        'is_local': True,
                        'audio_md5': audio_md5,
                        'ref_language': ref_language,
                        'created_at': datetime.now().isoformat()
                    }
                    try:
                        local_tts_key = '__LOCAL_TTS__'
                        _config_manager.save_voice_for_api_key(local_tts_key, voice_id, voice_data)
                        logger.info(f"本地 TTS voice_id 已保存: {voice_id}")
                    except Exception as save_error:
                        logger.warning(f"保存 voice_id 到音色库失败（本地 TTS 仍可用）: {save_error}")

                    return JSONResponse({
                        'voice_id': voice_id,
                        'message': result.get('message', '本地音色注册成功'),
                        'is_local': True
                    })
                else:
                    error_text = resp.text
                    logger.error(f"本地 TTS 注册失败: {error_text}")
                    return JSONResponse({
                        'error': f'本地 TTS 注册失败: {error_text[:200]}'
                    }, status_code=resp.status_code)

        except httpx.ConnectError as e:
            logger.error(f"无法连接本地 TTS 服务器: {e}")
            return JSONResponse({
                'error': f'无法连接本地 TTS 服务器: {http_base}，请确保服务器已启动'
            }, status_code=503)
        except Exception as e:
            logger.error(f"本地 TTS 注册时发生错误: {e}")
            return JSONResponse({
                'error': f'本地 TTS 注册失败: {str(e)}'
            }, status_code=500)

    # ==================== 云端语音克隆：按 provider 对偶分支 ====================

    # 统一通过 config_manager 获取 API Key
    cosyvoice_runtime = None
    api_key = _config_manager.get_tts_api_key(provider)

    if provider in ('minimax', 'minimax_intl'):
        # ---------- MiniMax（国服 / 国际服）----------
        if not api_key:
            return JSONResponse({
                'error': 'MINIMAX_API_KEY_MISSING',
                'code': 'MINIMAX_API_KEY_MISSING',
                'message': '未配置 MiniMax API Key，请先在设置中填写'
            }, status_code=400)
        base_url = get_minimax_base_url(provider)
        storage_key = f'{get_minimax_storage_prefix(provider)}{api_key[-8:]}'
        provider_label = 'MiniMax国际服' if provider == 'minimax_intl' else 'MiniMax国服'

    elif provider in ('cosyvoice', 'cosyvoice_intl'):
        # ---------- 阿里 CosyVoice（国内 / 国际）----------
        cosyvoice_runtime = _config_manager.get_cosyvoice_clone_runtime(provider)
        api_key = (cosyvoice_runtime.get('api_key') or '').strip()
        if not api_key:
            return JSONResponse({
                'error': 'TTS_AUDIO_API_KEY_MISSING',
                'code': 'TTS_AUDIO_API_KEY_MISSING'
            }, status_code=400)
        base_url = cosyvoice_runtime.get('base_url', '')
        storage_key = cosyvoice_runtime.get('storage_key') or api_key
        provider_label = cosyvoice_runtime.get('provider_label') or '阿里百炼CosyVoice'

    elif provider == 'elevenlabs':
        if not api_key:
            return JSONResponse({
                'error': 'ELEVENLABS_API_KEY_MISSING',
                'code': 'ELEVENLABS_API_KEY_MISSING',
                'message': '未配置 ElevenLabs API Key，请先在设置中填写'
            }, status_code=400)
        base_url = await _get_elevenlabs_base_url(_config_manager)
        storage_key = f'__ELEVENLABS__{api_key[-8:]}'
        provider_label = 'ElevenLabs'

    elif provider == 'mimo':
        if not api_key:
            return JSONResponse({
                'error': 'MIMO_API_KEY_MISSING',
                'code': 'MIMO_API_KEY_MISSING',
                'message': '未配置 MiMo API Key，请先在设置中填写'
            }, status_code=400)
        # base_url 须与 api_key 同源：assistApi=mimo（含 Token Plan）时 get_core_config 已把
        # OPENROUTER_URL 解析成对应端点（普通 / token-plan-*），get_tts_api_key('mimo') 也据此
        # 返回配套 key；否则用默认 xiaomimimo 端点。和 _mimo_resolve 的 base_url 规则对偶。
        if str(core_config.get('assistApi') or '').strip().lower() == 'mimo':
            base_url = (core_config.get('OPENROUTER_URL') or '').strip()
        else:
            base_url = ''
        storage_key = f'{MIMO_VOICE_STORAGE_KEY}{api_key[-8:]}'
        provider_label = 'MiMo'

    elif provider == 'vllm_omni':
        # vLLM-Omni 是本地 self-hosted 服务，没有 API key、也没有远端音色注册接口。克隆走
        # 「内联参考音频」范式（对偶 MiMo）：参考音频 base64 + ref_text 整段落进 voice_storage
        # 的 voice_meta，每次合成时内联进 session.config 的 ref_audio/ref_text。桶名固定
        # __VLLM_OMNI__（无 key 后缀，因本地服务无 key 可分桶）。只有当前真正启用了
        # vLLM-Omni 时才保留端点快照；若 TTS 正在跟随辅助 API，保存那个地址会让将来的
        # vLLM clone 固化到一个非 WebSocket TTS 端点。此时参考样本照常保存，之后用户切到
        # vLLM-Omni 会读取新的当前端点。
        vllm_omni_active = (
            _as_bool(core_config.get('ENABLE_CUSTOM_API'), False)
            and str(core_config.get('ttsModelProvider') or '').strip() == 'vllm_omni'
        )
        base_url = (
            (core_config.get('ttsModelUrl') or core_config.get('TTS_MODEL_URL') or '').strip()
            if vllm_omni_active
            else ''
        )
        storage_key = '__VLLM_OMNI__'
        provider_label = 'vLLM-Omni'

    elif provider == 'doubao_tts':
        if not api_key:
            return JSONResponse({
                'error': 'DOUBAO_TTS_API_KEY_MISSING',
                'code': 'DOUBAO_TTS_API_KEY_MISSING',
                'message': '未配置豆包语音 API Key，请先在设置中填写'
            }, status_code=400)
        base_url = DOUBAO_TTS_DEFAULT_BASE_URL
        storage_key = f'{DOUBAO_VOICE_STORAGE_KEY}{api_key[-8:]}'
        provider_label = '豆包语音'

    else:
        return JSONResponse({'error': f'不支持的 provider: {provider}'}, status_code=400)

    # ---------- 公共流程：MD5 去重 ----------
    if provider in ('cosyvoice', 'cosyvoice_intl'):
        existing = _config_manager.find_cosyvoice_voice_by_audio_md5(provider, audio_md5, ref_language)
    else:
        existing = _config_manager.find_voice_by_audio_md5(storage_key, audio_md5, ref_language)
    if existing:
        voice_id_ex, voice_data_ex = existing
        # vLLM-Omni：同音频 + 同语言但不同 ref_text 视为不同音色（转录修正场景），
        # 不命中去重，允许用户用修正后的 ref_text 重新注册。
        if provider == 'vllm_omni':
            existing_ref_text = str((voice_data_ex or {}).get('clone_ref_text') or '').strip()
            if existing_ref_text != vllm_ref_text:
                # 清理旧条目：否则 find_voice_by_audio_md5 按插入顺序总是先返回
                # 最旧的匹配条目，旧 voice 永远占位，新注册无限重复创建；
                # 旧 voice 也仍出现在音色列表中，用户可能选到错误音色。
                try:
                    _config_manager.delete_voice_for_current_api(voice_id_ex)
                    logger.info(
                        f"vLLM-Omni 克隆音色 {voice_id_ex} ref_text 变更"
                        f"（旧: {existing_ref_text!r} → 新: {vllm_ref_text!r}），已删除旧条目")
                except Exception:
                    logger.warning(
                        "vLLM-Omni 旧条目 %s 删除失败，可能导致下次去重仍命中旧条目",
                        voice_id_ex, exc_info=True)
                existing = None
    if existing:
        voice_id, voice_data = existing
        logger.info(f"{provider_label} 音频 MD5 命中，复用 voice_id: {voice_id}")
        return JSONResponse({
            'voice_id': voice_id,
            'message': f'已复用现有{provider_label}音色，跳过上传',
            'reused': True,
            'provider': provider
        })

    # ---------- 公共流程：音频规范化 ----------
    try:
        if provider in ('cosyvoice', 'cosyvoice_intl'):
            mime_type, error_msg = validate_audio_file(file_buffer, file.filename)
            if not mime_type:
                return JSONResponse({'error': error_msg}, status_code=400)
        normalized_buffer, normalized_filename, audio_meta = await asyncio.to_thread(
            normalize_voice_clone_api_audio,
            file_buffer,
            file.filename or 'prompt_audio.wav',
        )
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    logger.info(
        "%s 语音克隆参考音频已规范化: %sHz/%sch -> %sHz/mono",
        provider_label,
        audio_meta['original']['sample_rate'],
        audio_meta['original']['channels'],
        audio_meta['normalized']['sample_rate'],
    )

    # ---------- 按 provider 调用对应克隆 API ----------
    try:
        if provider in ('minimax', 'minimax_intl'):
            original_prefix, minimax_prefix = build_minimax_request_voice_id(prefix, provider_label)

            minimax_lang = minimax_normalize_language(ref_language)
            client = MinimaxVoiceCloneClient(api_key=api_key, base_url=base_url)
            voice_id = await client.clone_voice(
                audio_buffer=normalized_buffer,
                filename=normalized_filename,
                prefix=minimax_prefix,
                language=minimax_lang,
            )
            voice_data = {
                'voice_id': voice_id,
                'prefix': original_prefix,  # 保存原始前缀用于显示
                'minimax_prefix': minimax_prefix,  # 保存实际提交给 MiniMax 的安全前缀
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'minimax_language': minimax_lang,
                'provider': provider,
                'minimax_base_url': base_url,
                'created_at': datetime.now().isoformat()
            }

        elif provider == 'elevenlabs':
            voice_id = await _elevenlabs_clone_voice(
                api_key=api_key,
                base_url=base_url,
                audio_buffer=normalized_buffer,
                filename=normalized_filename,
                name=prefix,
            )
            voice_data = {
                'voice_id': voice_id,
                'raw_voice_id': _raw_elevenlabs_voice_id(voice_id),
                'prefix': prefix,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'provider': 'elevenlabs',
                'source': 'clone',
                'elevenlabs_base_url': base_url,
                'created_at': datetime.now().isoformat()
            }

        elif provider == 'mimo':
            # MiMo 没有远端注册接口（已核实官方文档：voiceclone 只能每次内联参考音频，无
            # create-voice / 远端 voice_id）。所以严格对偶 MiniMax 的做法是：把克隆身份整段
            # 落进 voice_storage.json 的 voice_meta——MiniMax 那里存的是远端 voice_id，这里存
            # 参考音频本身（base64）。不另起本地文件存储，voice_meta 随 voice_storage.json 一起
            # 云同步（与 MiniMax 同构）。校验样本可用后再落库。
            client = MimoVoiceCloneClient(api_key=api_key, base_url=base_url or None)
            sample_bytes = normalized_buffer.getvalue()
            await client.validate_sample(sample_bytes, mime_type='audio/wav')
            # voice_id 维度必须与 MD5 去重键 (storage_key, audio_md5, ref_language) 一致：
            #  - 含 key 末 8 位：同一音频在不同 MiMo key 下落不同 voice_id，避免跨 __MIMO__ 桶
            #    同名被 delete_voice_for_current_api 按 id 扫桶误删（Codex review #1851）。
            #  - 含 ref_language：去重带 ref_language，若 id 不带则「同音频换语言」绕过去重却又
            #    生成同名 id，覆盖掉前一条 voice_data（CodeRabbit review #1851）。
            voice_id = f'mimo-clone-{api_key[-8:]}-{ref_language}-{audio_md5[:12]}'
            voice_data = {
                'voice_id': voice_id,
                'prefix': prefix,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'provider': 'mimo',
                'source': 'clone',
                # 克隆身份：参考音频 base64（对偶 MiniMax 的远端 voice_id），dispatch/preview
                # 读它内联进 voiceclone 请求。存进 voice_meta 即随 voice_storage.json 云同步。
                'clone_sample_b64': base64.b64encode(sample_bytes).decode('ascii'),
                'clone_sample_mime': 'audio/wav',
                # base_url 存进 voice_meta（对偶 minimax_base_url）；dispatch 在 assistApi=mimo
                # （Token Plan 的唯一场景）时仍按当前配置重解析，保证 key/端点配套。
                'mimo_base_url': base_url or '',
                'created_at': datetime.now().isoformat()
            }

        elif provider == 'vllm_omni':
            # vLLM-Omni 内联克隆（对偶 MiMo）：无远端注册接口，参考音频 base64 + ref_text 整段
            # 落进 voice_storage 的 voice_meta，dispatch 时读出来内联进 session.config 的
            # ref_audio/ref_text。vLLM-Omni 无远端校验接口（不像 MiMo 有 validate_sample），
            # 参考音频运行时才用，所以这里跳过校验直接落库。
            sample_bytes = normalized_buffer.getvalue()
            # voice_id 维度与去重键一致：含 ref_language 避免同音频换语言覆盖，含
            # ref_text hash 避免同音频不同转录命中旧 voice（转录修正场景）。
            # 无 key 后缀（本地服务无 key），桶 __VLLM_OMNI__ 是全局唯一分区。
            ref_text_hash = hashlib.md5(vllm_ref_text.encode('utf-8')).hexdigest()[:8]
            voice_id = f'vllm-omni-clone-{ref_language}-{audio_md5[:12]}-{ref_text_hash}'
            voice_data = {
                'voice_id': voice_id,
                'prefix': prefix,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'provider': 'vllm_omni',
                'source': 'clone',
                # 克隆身份：参考音频 base64（对偶 MiMo 的 clone_sample_b64），dispatch/preview
                # 读它内联进 session.config 的 ref_audio。存进 voice_meta 即随 voice_storage 云同步。
                'clone_sample_b64': base64.b64encode(sample_bytes).decode('ascii'),
                'clone_sample_mime': 'audio/wav',
                # 参考音频原文：vLLM-Omni 克隆要求 ref_text 与音频严格对应，作 session.config.ref_text。
                'clone_ref_text': vllm_ref_text,
                'created_at': datetime.now().isoformat()
            }
            # 仅存真正激活的 vLLM-Omni 端点快照；跟随辅助 API 时不污染克隆的运行地址。
            if base_url:
                voice_data['vllm_omni_base_url'] = base_url

        elif provider == 'doubao_tts':
            try:
                speaker_id = _normalize_doubao_voice_clone_speaker_id(prefix)
            except ValueError as exc:
                return JSONResponse({
                    'error': 'DOUBAO_SPEAKER_ID_REQUIRED',
                    'code': 'DOUBAO_SPEAKER_ID_REQUIRED',
                    'message': str(exc),
                }, status_code=400)
            resource_id = DOUBAO_TTS_DEFAULT_RESOURCE_ID
            client = DoubaoVoiceCloneClient(
                api_key=api_key,
                base_url=base_url,
                resource_id=resource_id,
            )
            voice_id = await client.clone_voice(
                normalized_buffer,
                speaker_id=speaker_id,
                display_name=speaker_id,
                audio_format='wav',
            )
            voice_data = {
                'voice_id': voice_id,
                'prefix': prefix,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'provider': 'doubao_tts',
                'source': 'clone',
                'doubao_base_url': base_url,
                'doubao_resource_id': resource_id,
                'clone_model': resource_id,
                'created_at': datetime.now().isoformat()
            }

        else:  # cosyvoice / cosyvoice_intl
            from utils.api_config_loader import get_cosyvoice_clone_model
            clone_model = get_cosyvoice_clone_model(provider)
            language_hints = qwen_language_hints(ref_language)
            dashscope_base_url = (cosyvoice_runtime or {}).get('base_url', '')
            client = QwenVoiceCloneClient(
                api_key=api_key,
                tflink_upload_url=TFLINK_UPLOAD_URL,
                dashscope_base_url=dashscope_base_url,
            )
            voice_id, tmp_url, _request_id = await client.clone_voice(
                audio_buffer=normalized_buffer,
                filename=normalized_filename,
                prefix=prefix,
                language_hints=language_hints,
                target_model=clone_model,
            )
            voice_data = {
                'voice_id': voice_id,
                'prefix': prefix,
                'file_url': tmp_url,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'provider': provider,
                'dashscope_base_url': dashscope_base_url,
                'clone_model': clone_model,
                'created_at': datetime.now().isoformat()
            }

        logger.info(f"{provider_label} 音色注册成功，voice_id: {voice_id}")

    except ElevenLabsUpstreamError as e:
        logger.error(f"ElevenLabs 音色注册上游服务错误 ({e.status_code}): {e}")
        return JSONResponse({
            'error': f'ElevenLabs上游服务错误: {str(e)}',
            'code': 'ELEVENLABS_UPSTREAM_ERROR',
            'provider': provider,
        }, status_code=502)
    except (MinimaxVoiceCloneError, QwenVoiceCloneError, MimoVoiceCloneError, DoubaoTtsError) as e:
        logger.error(f"{provider_label} 音色注册失败: {e}")
        error_detail = str(e)
        if '超时' in error_detail:
            return JSONResponse({'error': error_detail, 'provider': provider}, status_code=408)
        elif '下载' in error_detail:
            return JSONResponse({'error': error_detail, 'provider': provider}, status_code=415)
        return JSONResponse({'error': f'{provider_label}音色注册失败: {error_detail}', 'provider': provider}, status_code=500)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    except Exception as e:
        logger.error(f"{provider_label} 音色注册时发生错误: {e}")
        return JSONResponse({'error': f'{provider_label}音色注册失败: {str(e)}', 'provider': provider}, status_code=500)

    # ---------- 公共流程：保存到本地音色库 ----------
    try:
        _config_manager.save_voice_for_api_key(storage_key, voice_id, voice_data)
        logger.info(f"{provider_label} voice_id 已保存到音色库: {voice_id}")
    except Exception as save_error:
        logger.error(f"保存 {provider_label} voice_id 到音色库失败: {save_error}")
        # MiMo 与其它家不同：它没有远端音色资源（克隆身份 = voice_meta 里的样本 base64，
        # save 失败＝什么都没落库，voice_id 是本地生成、此刻指向空）。返回 200+local_save_failed
        # 会给用户一个根本不存在的 voice_id。而且 MiMo 不存在"重试会重复创建远端资源"的代价
        # （validate 不创建任何东西），重试是安全的——所以这里返回真失败，让客户端知道并可重试
        # （Codex review #1851；与 PR #528「远端已创建→200 partial」规则的前提相反）。
        if provider in ('mimo', 'vllm_omni'):
            return JSONResponse({
                'error': f'{provider_label}音色保存失败: {str(save_error)}',
                'code': 'TTS_VOICE_SAVE_FAILED',
                'provider': provider,
            }, status_code=500)
        # 其它 provider（cosyvoice/minimax/elevenlabs）远端音色已创建，本地保存失败仍返回
        # 200+local_save_failed，避免客户端重试重复创建远端资源、浪费配额（PR #528 既定规则）。
        return JSONResponse({
            'voice_id': voice_id,
            'message': f'{provider_label}音色注册成功，但本地保存失败',
            'local_save_failed': True,
            'error': str(save_error),
            'provider': provider,
        }, status_code=200)

    return JSONResponse({
        'voice_id': voice_id,
        'message': f'{provider_label}音色注册成功并已保存到音色库',
        'provider': provider,
    })


@router.post('/voice_clone_direct')
async def voice_clone_direct(request: Request):
    """
    Direct-link voice cloning endpoint — skips the audio upload step and registers the voice directly from the provided direct URL.

    Supports the CosyVoice, MiniMax and ElevenLabs providers:
    - CosyVoice: registers the voice directly with the direct-link URL
    - MiniMax: downloads the audio file first, then uploads it to the MiniMax server to register the voice

    Request body:
        {
            "direct_link": "https://example.com/audio.wav",  // direct audio URL
            "prefix": "custom_prefix",                        // voice prefix name
            "ref_language": "ch",                             // reference audio language
            "provider": "cosyvoice"                           // provider: cosyvoice / cosyvoice_intl / minimax / minimax_intl / elevenlabs
        }
    """
    try:
        data = await request.json()
    except Exception as e:
        return JSONResponse({'error': f'请求体解析失败: {e}'}, status_code=400)

    direct_link = str(data.get('direct_link') or '').strip()
    prefix = str(data.get('prefix') or '').strip()
    ref_language = str(data.get('ref_language') or 'ch').lower().strip()
    provider = str(data.get('provider') or 'cosyvoice').lower().strip()

    # 参数验证
    if not direct_link:
        return JSONResponse({'error': '缺少 direct_link 参数'}, status_code=400)
    if not prefix:
        return JSONResponse({'error': '缺少 prefix 参数'}, status_code=400)
    try:
        await _validate_direct_link_target(direct_link)
    except DirectLinkSecurityError as e:
        return JSONResponse({
            'error': str(e),
            'code': e.code,
        }, status_code=400)
    except Exception as e:
        logger.warning(f"SSRF检查失败: {e}")
        return JSONResponse({'error': '直链安全检查失败'}, status_code=400)

    # 验证语言参数
    valid_languages = ['ch', 'en', 'fr', 'de', 'ja', 'ko', 'ru']
    if ref_language not in valid_languages:
        ref_language = 'ch'

    # 验证服务商参数
    valid_providers = ['minimax', 'minimax_intl', 'cosyvoice', 'cosyvoice_intl', 'elevenlabs']
    if provider not in valid_providers:
        return JSONResponse({
            'error': f'无效的服务商: {provider}',
            'code': 'TTS_PROVIDER_INVALID',
            'message': f'支持的服务商: {", ".join(valid_providers)}',
            'details': {'provider': provider, 'valid_providers': ', '.join(valid_providers)},
        }, status_code=400)

    # 获取 API Key
    _config_manager = get_config_manager()
    cosyvoice_runtime = None
    if provider in ('cosyvoice', 'cosyvoice_intl'):
        cosyvoice_runtime = _config_manager.get_cosyvoice_clone_runtime(provider)
        api_key = (cosyvoice_runtime.get('api_key') or '').strip()
    else:
        api_key = _config_manager.get_tts_api_key(provider)
    if not api_key:
        if provider in ('minimax', 'minimax_intl'):
            return JSONResponse({
                'error': 'MINIMAX_API_KEY_MISSING',
                'code': 'MINIMAX_API_KEY_MISSING',
                'message': '未配置 MiniMax API Key，请先在设置中填写'
            }, status_code=400)
        if provider == 'elevenlabs':
            return JSONResponse({
                'error': 'ELEVENLABS_API_KEY_MISSING',
                'code': 'ELEVENLABS_API_KEY_MISSING',
                'message': '未配置 ElevenLabs API Key，请先在设置中填写'
            }, status_code=400)
        else:
            return JSONResponse({
                'error': 'TTS_AUDIO_API_KEY_MISSING',
                'code': 'TTS_AUDIO_API_KEY_MISSING'
            }, status_code=400)

    # 导入所有可能用到的异常类（用于后面的异常捕获）
    from utils.voice_clone import MinimaxVoiceCloneError, QwenVoiceCloneError

    # 设置服务商相关参数
    if provider in ('minimax', 'minimax_intl'):
        from utils.voice_clone import (
            MinimaxVoiceCloneClient,
            minimax_normalize_language,
        )
        base_url = get_minimax_base_url(provider)
        storage_key = f'{get_minimax_storage_prefix(provider)}{api_key[-8:]}'
        provider_label = 'MiniMax国际服' if provider == 'minimax_intl' else 'MiniMax国服'
    elif provider == 'elevenlabs':
        base_url = await _get_elevenlabs_base_url(_config_manager)
        storage_key = f'__ELEVENLABS__{api_key[-8:]}'
        provider_label = 'ElevenLabs'
    else:  # cosyvoice / cosyvoice_intl
        from utils.voice_clone import QwenVoiceCloneClient, qwen_language_hints
        base_url = (cosyvoice_runtime or {}).get('base_url', '')
        storage_key = (cosyvoice_runtime or {}).get('storage_key') or api_key
        provider_label = (cosyvoice_runtime or {}).get('provider_label') or '阿里百炼CosyVoice'

    # 验证直链是否可访问（HEAD失败时回退到GET）
    # 每一跳都固定到已校验的解析结果，避免校验后请求阶段被 DNS rebinding 绕过。
    try:
        head_resp = await _request_direct_link_follow_redirects("HEAD", direct_link)
        try:
            if head_resp.status_code >= 400:
                # HEAD失败，尝试GET
                logger.warning(f"HEAD请求失败({head_resp.status_code})，尝试GET请求: {direct_link}")
                get_resp = await _request_direct_link_follow_redirects(
                    "GET",
                    direct_link,
                    stream=True,
                    headers={"Range": "bytes=0-0"},
                )
                try:
                    if get_resp.status_code >= 400:
                        return JSONResponse({
                            'error': f'直链无法访问，状态码: {get_resp.status_code}',
                            'code': 'DIRECT_LINK_INACCESSIBLE'
                        }, status_code=400)
                finally:
                    await get_resp.aclose()
        finally:
            await head_resp.aclose()
    except DirectLinkSecurityError as e:
        logger.warning(f"直链安全校验失败: {e}")
        return JSONResponse({
            'error': str(e),
            'code': e.code,
        }, status_code=400)
    except Exception as e:
        logger.warning(f"直链验证失败: {e}")
        # 不阻断流程，只是警告

    # 根据服务商类型执行不同的克隆逻辑
    try:
        if provider in ('minimax', 'minimax_intl'):
            # ========== MiniMax 直链克隆流程 ==========
            # 1. 下载音频文件（使用流式读取避免内存问题）
            logger.info(f"开始下载直链音频: {direct_link}")
            MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB限制

            filename, audio_bytes = await _download_direct_link_audio(
                direct_link,
                max_file_size=MAX_FILE_SIZE,
            )

            logger.info(f"音频下载完成: {filename}, 大小: {len(audio_bytes)} bytes")

            # 2. 计算音频内容的 MD5 用于去重（与文件上传路径保持一致）
            import hashlib
            audio_md5 = hashlib.md5(audio_bytes).hexdigest()

            # 3. MD5 去重检查
            existing = _config_manager.find_voice_by_audio_md5(storage_key, audio_md5, ref_language)
            if existing:
                voice_id, voice_data = existing
                logger.info(f"{provider_label} 直链 MD5 命中，复用 voice_id: {voice_id}")
                return JSONResponse({
                    'voice_id': voice_id,
                    'message': f'已复用现有{provider_label}音色，跳过注册',
                    'reused': True,
                    'provider': provider
                })

            # 2. 音频归一化处理（与文件上传路径保持一致）
            from utils.audio import normalize_voice_clone_api_audio
            original_buffer = io.BytesIO(audio_bytes)
            normalized_buffer, normalized_filename, _ = await asyncio.to_thread(
                normalize_voice_clone_api_audio,
                original_buffer, filename
            )

            original_prefix, minimax_prefix = build_minimax_request_voice_id(prefix, provider_label)

            # 4. 使用 MinimaxVoiceCloneClient 上传并注册音色
            minimax_lang = minimax_normalize_language(ref_language)
            client = MinimaxVoiceCloneClient(api_key=api_key, base_url=base_url)

            voice_id = await client.clone_voice(
                audio_buffer=normalized_buffer,
                filename=normalized_filename,
                prefix=minimax_prefix,
                language=minimax_lang,
            )

            voice_data = {
                'voice_id': voice_id,
                'prefix': original_prefix,  # 保存原始前缀用于显示
                'minimax_prefix': minimax_prefix,  # 保存实际提交给 MiniMax 的安全前缀
                'direct_link': direct_link,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'minimax_language': minimax_lang,
                'provider': provider,
                'minimax_base_url': base_url,
                'created_at': datetime.now().isoformat(),
                'is_direct_link': True
            }

            logger.info(f"{provider_label} 直链音色注册成功，voice_id: {voice_id}")

        elif provider == 'elevenlabs':
            MAX_FILE_SIZE = 100 * 1024 * 1024

            filename, audio_bytes = await _download_direct_link_audio(
                direct_link,
                max_file_size=MAX_FILE_SIZE,
            )

            audio_md5 = hashlib.md5(audio_bytes).hexdigest()

            existing = _config_manager.find_voice_by_audio_md5(storage_key, audio_md5, ref_language)
            if existing:
                voice_id, voice_data = existing
                logger.info(f"{provider_label} 直链 MD5 命中，复用 voice_id: {voice_id}")
                return JSONResponse({
                    'voice_id': voice_id,
                    'message': f'已复用现有{provider_label}音色，跳过注册',
                    'reused': True,
                    'provider': provider
                })

            normalized_buffer, normalized_filename, _ = await asyncio.to_thread(
                normalize_voice_clone_api_audio,
                io.BytesIO(audio_bytes),
                filename,
            )
            voice_id = await _elevenlabs_clone_voice(
                api_key=api_key,
                base_url=base_url,
                audio_buffer=normalized_buffer,
                filename=normalized_filename,
                name=prefix,
            )
            voice_data = {
                'voice_id': voice_id,
                'raw_voice_id': _raw_elevenlabs_voice_id(voice_id),
                'prefix': prefix,
                'direct_link': direct_link,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'provider': 'elevenlabs',
                'elevenlabs_base_url': base_url,
                'created_at': datetime.now().isoformat(),
                'is_direct_link': True
            }

            logger.info(f"{provider_label} 直链音色注册成功，voice_id: {voice_id}")

        else:  # cosyvoice / cosyvoice_intl
            # ========== CosyVoice 直链克隆流程 ==========
            # 1. 下载音频文件以计算内容MD5（使用流式读取避免内存问题）
            logger.info(f"开始下载直链音频用于CosyVoice: {direct_link}")
            MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB限制

            _, audio_bytes = await _download_direct_link_audio(
                direct_link,
                max_file_size=MAX_FILE_SIZE,
            )

            logger.info(f"音频下载完成，大小: {len(audio_bytes)} bytes")

            # 2. 计算音频内容的 MD5 用于去重
            import hashlib
            audio_md5 = hashlib.md5(audio_bytes).hexdigest()

            # 3. MD5 去重检查
            existing = _config_manager.find_voice_by_audio_md5(storage_key, audio_md5, ref_language)
            if existing:
                voice_id, voice_data = existing
                logger.info(f"{provider_label} 直链 MD5 命中，复用 voice_id: {voice_id}")
                return JSONResponse({
                    'voice_id': voice_id,
                    'message': f'已复用现有{provider_label}音色，跳过注册',
                    'reused': True,
                    'provider': provider
                })

            # 4. 使用直链注册音色
            language_hints = qwen_language_hints(ref_language)
            client = QwenVoiceCloneClient(
                api_key=api_key,
                tflink_upload_url=TFLINK_UPLOAD_URL,
                dashscope_base_url=base_url,
            )

            from utils.api_config_loader import get_cosyvoice_clone_model
            clone_model = get_cosyvoice_clone_model(provider)
            voice_id, _ = await asyncio.to_thread(
                client.create_voice,
                prefix=prefix,
                url=direct_link,
                language_hints=language_hints,
                target_model=clone_model,
            )

            voice_data = {
                'voice_id': voice_id,
                'prefix': prefix,
                'file_url': direct_link,
                'audio_md5': audio_md5,
                'ref_language': ref_language,
                'provider': provider,
                'dashscope_base_url': base_url,
                'clone_model': clone_model,
                'created_at': datetime.now().isoformat(),
                'is_direct_link': True
            }

            logger.info(f"{provider_label} 直链音色注册成功，voice_id: {voice_id}")

    except DirectLinkSecurityError as e:
        logger.warning(f"{provider_label} 直链安全校验失败: {e}")
        return JSONResponse({
            'error': str(e),
            'code': e.code,
            'provider': provider,
        }, status_code=400)
    except ElevenLabsUpstreamError as e:
        logger.error(f"ElevenLabs 直链音色注册上游服务错误 ({e.status_code}): {e}")
        return JSONResponse({
            'error': f'ElevenLabs上游服务错误: {str(e)}',
            'code': 'ELEVENLABS_UPSTREAM_ERROR',
            'provider': provider,
        }, status_code=502)
    except (MinimaxVoiceCloneError, QwenVoiceCloneError) as e:
        logger.error(f"{provider_label} 直链音色注册失败: {e}")
        error_detail = str(e)
        if '超时' in error_detail:
            return JSONResponse({'error': error_detail, 'provider': provider}, status_code=408)
        elif '下载' in error_detail:
            return JSONResponse({'error': error_detail, 'provider': provider}, status_code=415)
        return JSONResponse({
            'error': f'{provider_label}音色注册失败: {error_detail}',
            'provider': provider
        }, status_code=500)
    except Exception as e:
        logger.error(f"{provider_label} 直链音色注册时发生错误: {e}")
        return JSONResponse({
            'error': f'{provider_label}音色注册失败: {str(e)}',
            'provider': provider
        }, status_code=500)

    # 保存到本地音色库
    try:
        _config_manager.save_voice_for_api_key(storage_key, voice_id, voice_data)
        logger.info(f"{provider_label} 直链 voice_id 已保存到音色库: {voice_id}")
    except Exception as save_error:
        logger.error(f"保存 {provider_label} 直链 voice_id 到音色库失败: {save_error}")
        return JSONResponse({
            'voice_id': voice_id,
            'message': f'{provider_label}直链音色注册成功，但本地保存失败',
            'local_save_failed': True,
            'error': str(save_error),
            'provider': provider,
        }, status_code=200)

    return JSONResponse({
        'voice_id': voice_id,
        'message': f'{provider_label}直链音色注册成功并已保存到音色库',
        'provider': provider,
        'is_direct_link': True
    })
