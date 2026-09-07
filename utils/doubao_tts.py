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

from __future__ import annotations

import base64
import binascii
import io
import json
import uuid
from collections.abc import Iterable
from typing import Any

# httpx 不在模块顶层 import。
#
# 这个模块只有一个函数用 httpx，却因为 DOUBAO_VOICE_STORAGE_KEY 这个字符串
# 常量被 utils/config_manager 引用，而坐在 launcher 的启动导入链上。httpx
# 会 eager import 自己的 CLI（httpx._main -> rich + pygments + click），实测
# 整包 108 ms、其中 CLI 占 74-85 ms——而进程启动到端口 bind 之间一次 HTTP
# 都不发。改成用到时再 import，与 utils/llm_client.py 对那几个 LLM SDK 的
# 处理同构（见 scripts/check_startup_import_lazy.py 的说明）。

DOUBAO_TTS_DEFAULT_BASE_URL = "https://openspeech.bytedance.com"
DOUBAO_TTS_DEFAULT_RESOURCE_ID = "seed-icl-2.0"
DOUBAO_VOICE_CLONE_RESOURCE_ID = DOUBAO_TTS_DEFAULT_RESOURCE_ID
DOUBAO_TTS_DEFAULT_SAMPLE_RATE = 24000
DOUBAO_TTS_DEFAULT_FORMAT = "wav"
DOUBAO_VOICE_STORAGE_KEY = "__DOUBAO_TTS__"
DOUBAO_TTS_DEFAULT_CONTEXT_TEXTS = (
    "用自然、轻快、口语化的中文表达，情绪随文本变化。"
    "句尾的喵只当作很轻的语气词带过，不要重读，也不要和啊、吧、呢连读成突兀的双语气词。"
    "整体语速比默认略快一点，但保持清晰。"
)


class DoubaoTtsError(Exception):
    pass


def doubao_normalize_base_url(base_url: str | None) -> str:
    return (base_url or DOUBAO_TTS_DEFAULT_BASE_URL).strip().rstrip("/")


def doubao_tts_url(base_url: str | None) -> str:
    return f"{doubao_normalize_base_url(base_url)}/api/v3/tts/unidirectional"


def doubao_voice_clone_url(base_url: str | None) -> str:
    return f"{doubao_normalize_base_url(base_url)}/api/v3/tts/voice_clone"


def doubao_api_headers(api_key: str, resource_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }


def doubao_voice_clone_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }


def build_doubao_tts_payload(
    text: str,
    speaker: str,
    *,
    audio_format: str = DOUBAO_TTS_DEFAULT_FORMAT,
    sample_rate: int = DOUBAO_TTS_DEFAULT_SAMPLE_RATE,
    context_texts: Iterable[str] | None = None,
    speed_ratio: float = 1.08,
) -> dict[str, Any]:
    additions = {
        "context_texts": [
            line for line in (context_texts or (DOUBAO_TTS_DEFAULT_CONTEXT_TEXTS,))
            if str(line).strip()
        ]
    }
    return {
        "user": {"uid": "project-neko"},
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {
                "format": audio_format,
                "sample_rate": sample_rate,
                "speed_ratio": speed_ratio,
            },
            "additions": json.dumps(additions, ensure_ascii=False),
        },
    }


def _decode_audio_b64(value: object) -> bytes:
    if not isinstance(value, str) or not value.strip():
        return b""
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        return b""


def _extract_from_json_obj(obj: object) -> bytes:
    if not isinstance(obj, dict):
        return b""
    code = obj.get("code")
    message = obj.get("message") or obj.get("msg") or ""
    message_ok = str(message).strip().lower() in {"ok", "success", "succeed"}
    if code not in (None, 0, "0") and not message_ok:
        message = message or obj
        raise DoubaoTtsError(f"豆包 TTS 返回错误: {message}")
    chunks: list[bytes] = []
    for key in ("data", "audio", "audio_data"):
        value = obj.get(key)
        chunk = _extract_from_json_obj(value) if isinstance(value, dict) else _decode_audio_b64(value)
        if chunk:
            chunks.append(chunk)
    nested = obj.get("result") or obj.get("response")
    if isinstance(nested, dict):
        nested_audio = _extract_from_json_obj(nested)
        if nested_audio:
            chunks.append(nested_audio)
    return b"".join(chunks)


def _extract_from_text_fragment(fragment: str) -> bytes:
    decoder = json.JSONDecoder()
    chunks: list[bytes] = []
    idx = 0
    parsed_json = False
    while idx < len(fragment):
        while idx < len(fragment) and fragment[idx].isspace():
            idx += 1
        if idx >= len(fragment):
            break
        try:
            obj, end = decoder.raw_decode(fragment, idx)
        except json.JSONDecodeError:
            if parsed_json:
                break
            return _decode_audio_b64(fragment[idx:].strip())
        parsed_json = True
        chunk = _extract_from_json_obj(obj)
        if chunk:
            chunks.append(chunk)
        idx = end
    return b"".join(chunks)


def extract_doubao_audio_bytes(raw: bytes | str | dict[str, Any]) -> bytes:
    if isinstance(raw, dict):
        return _extract_from_json_obj(raw)
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw or "")
    chunks: list[bytes] = []
    for raw_line in text.splitlines() or [text]:
        line = raw_line.strip()
        if not line or line == "[DONE]":
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        chunk = _extract_from_text_fragment(line)
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


class DoubaoVoiceCloneClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        resource_id: str = DOUBAO_VOICE_CLONE_RESOURCE_ID,
    ):
        self.api_key = api_key
        self.base_url = doubao_normalize_base_url(base_url)
        # Kept for call-site compatibility; the voice-clone endpoint does not
        # accept X-Api-Resource-Id. That header belongs to TTS synthesis.
        self.resource_id = resource_id

    async def clone_voice(
        self,
        audio_buffer: io.BytesIO,
        *,
        speaker_id: str,
        display_name: str | None = None,
        audio_format: str = "wav",
    ) -> str:
        audio_buffer.seek(0)
        audio_b64 = base64.b64encode(audio_buffer.read()).decode("ascii")
        payload = {
            "speaker_id": speaker_id,
            "audio": {
                "data": audio_b64,
                "format": audio_format,
            },
            "display_name": display_name or speaker_id,
        }
        headers = doubao_voice_clone_headers(self.api_key)
        # 放在 try 之前：下面的 except 子句也要认得 httpx.TimeoutException。
        import httpx

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    doubao_voice_clone_url(self.base_url),
                    headers=headers,
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise DoubaoTtsError("豆包声音复刻请求超时，请稍后重试") from exc
        except Exception as exc:
            raise DoubaoTtsError(f"豆包声音复刻请求失败: {exc}") from exc
        if resp.status_code not in (200, 201):
            raise DoubaoTtsError(
                f"豆包声音复刻失败: HTTP {resp.status_code}, {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise DoubaoTtsError("豆包声音复刻返回了无法解析的响应") from exc
        if not isinstance(data, dict):
            raise DoubaoTtsError("豆包声音复刻返回了未知响应")
        code = data.get("code") if isinstance(data, dict) else None
        if code not in (None, 0, "0"):
            message = data.get("message") or data.get("msg") or data
            raise DoubaoTtsError(f"豆包声音复刻失败: {message}")
        result = data.get("data")
        voice_id = ""
        if isinstance(result, dict):
            voice_id = str(result.get("speaker_id") or result.get("voice_id") or "").strip()
        if not voice_id:
            voice_id = str(data.get("speaker_id") or data.get("voice_id") or "").strip()
        if not voice_id:
            raise DoubaoTtsError("豆包声音复刻成功但未返回音色 ID")
        return voice_id
