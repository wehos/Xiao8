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

"""User preferences and conversation settings endpoints.

Split out of the former monolithic ``main_routers/config_router.py``.
"""

from ._shared import logger, router

import asyncio
import json
import re

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from ..shared_state import get_session_manager
from utils.preferences import (
    GLOBAL_CONVERSATION_KEY,
    aload_global_conversation_settings_snapshot,
    aload_user_preferences,
    is_valid_asr_decision,
    move_model_to_top,
    save_global_conversation_settings_versioned,
    update_model_preferences,
    validate_model_preferences,
)
from utils.cloudsave_runtime import MaintenanceModeError


_CONVERSATION_SETTINGS_ASR_DECISION_HEADER = "x-conversation-settings-asr-decision"
_CONVERSATION_SETTINGS_FULL_SNAPSHOT_HEADER = "x-conversation-settings-full-snapshot"
_CONVERSATION_SETTINGS_ETAG_RE = re.compile(r'^(?:W/)?"conversation-settings-(\d+)"$')
_NOISE_REDUCTION_APPLY_LOCK = asyncio.Lock()


def _conversation_settings_etag(revision: int) -> str:
    return f'"conversation-settings-{revision}"'


def _parse_conversation_settings_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    match = _CONVERSATION_SETTINGS_ETAG_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("If-Match 格式无效")
    return int(match.group(1))


def _conversation_settings_response_payload(snapshot) -> dict:
    decisions = {}
    if snapshot.asr_decision is not None:
        decisions["independentAsrEnabled"] = snapshot.asr_decision
    return {
        "settings": snapshot.settings,
        "revision": snapshot.revision,
        "decisions": decisions,
        "reset": snapshot.reset,
    }


async def _apply_noise_reduction_to_active_sessions(enabled: bool):
    """Apply noise reduction toggle to all active voice sessions immediately."""
    from main_logic.omni_realtime_client import OmniRealtimeClient
    applied = True
    try:
        session_manager = get_session_manager()
        for _name, mgr in session_manager.items():
            if not mgr.is_active or mgr.session is None:
                continue
            # The Core-owned microphone pipeline comes FIRST in the frame path
            # and is not an Omni concern: it downsamples PC audio to 16 kHz, so
            # the Omni processor below skips RNNoise on what it receives, and
            # independent-ASR routes never reach the Omni processor at all.
            # Updating only the Omni side left this toggle a no-op for the rest
            # of the session on every route (Codex P2). Guarded per manager so
            # one failure cannot abandon the remaining ones.
            apply_core_pipeline = getattr(
                mgr, "apply_voice_input_noise_reduction", None
            )
            if callable(apply_core_pipeline):
                try:
                    await apply_core_pipeline(enabled)
                except Exception as core_exc:  # noqa: BLE001
                    applied = False
                    logger.warning(
                        f"Failed to apply noise reduction to the core "
                        f"microphone pipeline for {_name}: {core_exc}"
                    )
            if not isinstance(mgr.session, OmniRealtimeClient):
                continue
            # Isolated per manager for the same reason as the Core pipeline
            # above: this await reaches a live realtime transport, and with
            # only the shared try below, one character's failure abandoned the
            # toggle for every character after it in iteration order -- the
            # user sees the setting saved while some sessions never got it
            # (Codex P2).
            try:
                await mgr.session.set_audio_noise_reduction_enabled(enabled)
            except Exception as omni_exc:  # noqa: BLE001
                applied = False
                logger.warning(
                    f"Failed to apply noise reduction to the Omni processor "
                    f"for {_name}: {omni_exc}"
                )
    except Exception as e:
        applied = False
        logger.warning(f"Failed to apply noise reduction to active sessions: {e}")
    return applied


async def _apply_noise_reduction_if_current(enabled: bool):
    """Serialize runtime updates and discard superseded noise values."""
    async with _NOISE_REDUCTION_APPLY_LOCK:
        current = await aload_global_conversation_settings_snapshot()
        if current.settings.get("noiseReductionEnabled") is not enabled:
            return
        service = None
        try:
            from main_logic.voice_identity_service.registry import (
                VoiceIdentityServiceRegistryError,
                get_voice_identity_service_for_router,
            )

            try:
                service = get_voice_identity_service_for_router()
            except VoiceIdentityServiceRegistryError:
                service = None
            if service is not None:
                if not await service.prepare_runtime_audio_contract_change():
                    return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"Failed to suspend Owner voice evidence before noise "
                f"reduction transition: {exc}"
            )
            return
        applied = await _apply_noise_reduction_to_active_sessions(enabled)
        if service is not None and applied:
            try:
                await service.update_runtime_noise_reduction_enabled(enabled)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"Failed to reconcile Owner voice audio contract: {exc}"
                )


@router.get("/preferences")
async def get_preferences():
    """Get user preferences."""
    preferences = await aload_user_preferences()
    return preferences


@router.post("/preferences")
async def save_preferences(request: Request):
    """Save user preferences."""
    try:
        data = await request.json()
        if not data:
            return {"success": False, "error": "无效的数据"}
        
        # 验证偏好数据
        if not validate_model_preferences(data):
            return {"success": False, "error": "偏好数据格式无效"}
        
        # 防止使用保留的全局对话设置键作为模型路径
        if data.get('model_path') == GLOBAL_CONVERSATION_KEY:
            return {"success": False, "error": "model_path 不能使用保留键"}
        
        # 获取参数（可选）
        parameters = data.get('parameters')
        # 获取显示器信息（可选，用于多屏幕位置恢复）
        display = data.get('display')
        # 获取旋转信息（可选，用于VRM模型朝向）
        rotation = data.get('rotation')
        # 获取视口信息（可选，用于跨分辨率位置和缩放归一化）
        viewport = data.get('viewport')
        # 获取相机位置信息（可选，用于恢复VRM滚轮缩放状态）
        camera_position = data.get('camera_position')

        # 验证和清理 viewport 数据
        if viewport is not None:
            if not isinstance(viewport, dict):
                viewport = None
            else:
                # 验证必需的数值字段
                width = viewport.get('width')
                height = viewport.get('height')
                if not (isinstance(width, (int, float)) and isinstance(height, (int, float)) and
                        width > 0 and height > 0):
                    viewport = None

        # 更新偏好（底层 atomic_write_json 会阻塞事件循环，offload 到线程池）
        ok = await asyncio.to_thread(
            update_model_preferences,
            data['model_path'], data['position'], data['scale'], parameters, display, rotation, viewport, camera_position,
        )
        if ok:
            return {"success": True, "message": "偏好设置已保存"}
        else:
            return {"success": False, "error": "保存失败"}
            
    except MaintenanceModeError:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/preferences/set-preferred")
async def set_preferred_model(request: Request):
    """Set the preferred model."""
    try:
        data = await request.json()
        if not data or 'model_path' not in data:
            return {"success": False, "error": "无效的数据"}
        
        # move_model_to_top performs a cross-process locked read-modify-write.
        # Keep lock waits off the application event loop.
        if await asyncio.to_thread(move_model_to_top, data['model_path']):
            return {"success": True, "message": "首选模型已更新"}
        else:
            return {"success": False, "error": "模型不存在或更新失败"}
            
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/conversation-settings")
async def get_conversation_settings(response: Response):
    """Get global conversation settings (read from the user_preferences.json synced backup).

    Also returns the telemetry A/B test branch, so the frontend can pick default
    behavior by branch at first launch, consistent with the branch reported by the
    token tracker — the same device always lands in the same group, preventing
    control/experiment mismatches between client and server.
    """
    try:
        # 先解析 telemetry branch、再 load settings：get_telemetry_branch 可能在 slow
        # path 触发退役实验（proactive_interval_20s）的一次性偏好回滚（20s→15s）。若按
        # 旧顺序先 load，会拿到回滚前的 20s 返回前端；而存量用户没有首启 pending marker、
        # 会直接应用并经 periodic sync 把 20s POST 回来，撤销本次迁移（见 token_tracker
        # ._rollback_retired_proactive_interval）。
        try:
            from utils.token_tracker import get_telemetry_branch
            telemetry_branch = await asyncio.to_thread(get_telemetry_branch)
        except Exception:
            # 故意返回 None：前端只在 telemetryBranch 是字符串时清掉首启 pending
            # marker；如果这里 fallback 到 "main"，瞬时报错会被当成「控制组分流
            # 已决议」永久锁住，下次也不会重试。返 None 让前端保留 pending、
            # 下次 fetch 成功再决议
            logger.exception("解析 telemetry branch 失败，返回 null 让前端保留 pending marker")
            telemetry_branch = None
        snapshot = await aload_global_conversation_settings_snapshot()
        response.headers["ETag"] = _conversation_settings_etag(snapshot.revision)
        response.headers["Cache-Control"] = "no-store"
        return {
            "success": True,
            **_conversation_settings_response_payload(snapshot),
            "telemetryBranch": telemetry_branch,
        }
    except Exception as e:
        logger.exception(f"获取对话设置失败: {e}")
        return {"success": False, "error": "Internal server error", "settings": {}}


@router.post("/conversation-settings")
async def save_conversation_settings(request: Request):
    """CAS-save global conversation settings with legacy-client compatibility."""
    try:
        data = await request.json()
        if not isinstance(data, dict):
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": "请求体必须为对象"},
            )

        asr_decision = None
        raw_asr_decision = request.headers.get(
            _CONVERSATION_SETTINGS_ASR_DECISION_HEADER
        )
        if raw_asr_decision:
            try:
                parsed_asr_decision = json.loads(raw_asr_decision)
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "ASR decision header 格式无效"},
                )
            if not is_valid_asr_decision(parsed_asr_decision):
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "ASR decision header 格式无效"},
                )
            asr_decision = parsed_asr_decision
        try:
            expected_revision = _parse_conversation_settings_if_match(
                request.headers.get("if-match")
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"success": False, "error": str(exc)},
            )

        result = await asyncio.to_thread(
            save_global_conversation_settings_versioned,
            data,
            expected_revision=expected_revision,
            asr_decision=asr_decision,
            full_snapshot=(
                request.headers.get(_CONVERSATION_SETTINGS_FULL_SNAPSHOT_HEADER) == "1"
            ),
        )
        response_payload = _conversation_settings_response_payload(result.snapshot)
        response_headers = {
            "ETag": _conversation_settings_etag(result.snapshot.revision),
            "Cache-Control": "no-store",
        }
        if result.conflict:
            return JSONResponse(
                status_code=412,
                headers=response_headers,
                content={
                    "success": False,
                    "error": "conversation settings version conflict",
                    **response_payload,
                },
            )
        if not result.success:
            return JSONResponse(
                status_code=500,
                headers=response_headers,
                content={
                    "success": False,
                    "error": "保存失败",
                    **response_payload,
                },
            )

        if (
            isinstance(data.get("noiseReductionEnabled"), bool)
            and result.snapshot.settings.get("noiseReductionEnabled")
            == data["noiseReductionEnabled"]
        ):
            await _apply_noise_reduction_if_current(
                data["noiseReductionEnabled"],
            )

        return JSONResponse(
            headers=response_headers,
            content={
                "success": True,
                "message": "对话设置已保存",
                **response_payload,
            },
        )
    except MaintenanceModeError:
        raise
    except Exception as e:
        logger.exception(f"保存对话设置失败: {e}")
        return {"success": False, "error": "Internal server error"}
