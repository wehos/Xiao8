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

import os
import sys
from pathlib import Path
from typing import Any

from utils.cloudsave_runtime import (
    CloudsaveDeadlineExceeded,
    bootstrap_local_cloudsave_environment,
    cloudsave_disabled_reason,
    export_local_cloudsave_snapshot,
    import_local_cloudsave_snapshot,
    is_cloudsave_disabled,
    load_cloudsave_manifest,
    runtime_root_has_user_content,
)
from utils.steam_cloud_bundle import (
    download_cloudsave_bundle_from_steam,
    is_source_launch,
    upload_cloudsave_bundle_to_steam,
)


STEAM_AUTO_CLOUD_SYNC_BACKEND = "steam_auto_cloud"


def _get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[1]


def _load_steam_app_id() -> str:
    try:
        return (_get_app_root() / "steam_appid.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _build_recommended_root_paths(config_manager) -> dict[str, Any]:
    app_name = str(getattr(config_manager, "app_name", "N.E.K.O") or "N.E.K.O")
    subdirectory = f"{app_name}/cloudsave"
    return {
        "cross_platform_strategy": "single_root_with_overrides",
        "primary_root": {
            "root": "WinAppDataLocal",
            "subdirectory": subdirectory,
            "pattern": "*",
            "os": "All OSes",
            "recursive": True,
        },
        "root_overrides": [
            {
                "os": "macOS",
                "new_root": "MacAppSupport",
                "add_replace_path": subdirectory,
                "replace_path": True,
            },
            {
                "os": "Linux",
                "new_root": "LinuxXdgDataHome",
                "add_replace_path": subdirectory,
                "replace_path": True,
            },
        ],
    }


def _build_current_platform_rule_preview(config_manager) -> dict[str, str]:
    app_name = str(getattr(config_manager, "app_name", "N.E.K.O") or "N.E.K.O")
    subdirectory = f"{app_name}/cloudsave"
    if sys.platform == "win32":
        return {
            "platform": "Windows",
            "root": "WinAppDataLocal",
            "subdirectory": subdirectory,
        }
    if sys.platform == "darwin":
        return {
            "platform": "macOS",
            "root": "MacAppSupport",
            "subdirectory": subdirectory,
        }
    return {
        "platform": "Linux",
        "root": "LinuxXdgDataHome",
        "subdirectory": subdirectory,
    }


def _build_missing_snapshot_hint(status: dict[str, Any]) -> str:
    cloudsave_root = str(status.get("cloudsave_root") or "")
    runtime_root = str(status.get("runtime_root") or "")
    launched_from_source = not getattr(sys, "frozen", False) and Path(sys.argv[0]).suffix.lower() == ".py"

    hint_parts = [
        f"No staged Steam Auto-Cloud snapshot was found under {cloudsave_root}.",
    ]
    if launched_from_source:
        hint_parts.append(
            "If you are validating cross-device Steam Cloud sync, launch through Steam or the desktop launcher once "
            "so Steam can download cloudsave/ before startup import."
        )
    else:
        hint_parts.append(
            "If you expected cloud data on this device, launch through Steam once and confirm Auto-Cloud finished "
            "downloading cloudsave/ before startup import."
        )
    if runtime_root and bool(status.get("runtime_has_user_content")):
        hint_parts.append(
            f"The current runtime root {runtime_root} already has local user content, so this session will continue "
            "with local data until a staged snapshot appears."
        )
    return " ".join(hint_parts)


def _build_manual_download_hint(status: dict[str, Any]) -> str:
    runtime_root = str(status.get("runtime_root") or "")
    cloudsave_root = str(status.get("cloudsave_root") or "")
    return (
        f"A newer staged Steam Auto-Cloud snapshot is available under {cloudsave_root}, "
        f"but the runtime root {runtime_root} already contains local user content. "
        "Startup will keep the current local data and wait for an explicit download/apply action from Cloud Save Manager."
    )


def _build_steam_connectivity_status(steamworks) -> dict[str, bool]:
    if steamworks is None:
        return {
            "available": False,
            "running": False,
            "logged_on": False,
        }

    try:
        running = bool(steamworks.IsSteamRunning())
    except Exception:
        running = False
    try:
        logged_on = bool(steamworks.Users.LoggedOn())
    except Exception:
        logged_on = False
    return {
        "available": running and logged_on,
        "running": running,
        "logged_on": logged_on,
    }


def _is_steam_launch_tracked(app_id: str) -> bool:
    normalized_app_id = str(app_id or "").strip()
    if not normalized_app_id:
        return False
    steam_app_id = str(os.environ.get("SteamAppId") or "").strip()
    steam_game_id = str(os.environ.get("SteamGameId") or "").strip()
    return normalized_app_id in {steam_app_id, steam_game_id}


def _build_cloudsave_disabled_action_result(
    config_manager,
    *,
    action: str,
    reason: str,
    requested_reason: str,
    steamworks=None,
) -> dict[str, Any]:
    return {
        "success": True,
        "action": action,
        "reason": reason,
        "requested_reason": str(requested_reason or ""),
        "status": get_cloudsave_manager(config_manager).build_status(steamworks=steamworks),
    }


class CloudSaveManager:
    def __init__(self, config_manager):
        self.config_manager = config_manager

    def _try_download_remote_bundle(self, *, steamworks=None, deadline_monotonic: float | None = None) -> dict[str, Any]:
        try:
            return download_cloudsave_bundle_from_steam(
                self.config_manager,
                steamworks=steamworks,
                deadline_monotonic=deadline_monotonic,
            )
        except CloudsaveDeadlineExceeded:
            raise
        except Exception as exc:
            return {
                "success": False,
                "action": "failed",
                "reason": "remote_bundle_download_failed",
                "message": str(exc),
            }

    def _try_upload_remote_bundle(self, *, steamworks=None, deadline_monotonic: float | None = None) -> dict[str, Any]:
        try:
            return upload_cloudsave_bundle_to_steam(
                self.config_manager,
                steamworks=steamworks,
                deadline_monotonic=deadline_monotonic,
            )
        except CloudsaveDeadlineExceeded:
            raise
        except Exception as exc:
            return {
                "success": False,
                "action": "failed",
                "reason": "remote_bundle_upload_failed",
                "message": str(exc),
            }

    def build_status(self, *, steamworks=None) -> dict[str, Any]:
        if is_cloudsave_disabled():
            steam_status = _build_steam_connectivity_status(steamworks)
            app_id = _load_steam_app_id()
            source_launch = bool(is_source_launch())
            steam_launch_tracked = _is_steam_launch_tracked(app_id)
            recommended_paths = _build_recommended_root_paths(self.config_manager)
            current_platform_rule = _build_current_platform_rule_preview(self.config_manager)
            return {
                "backend": STEAM_AUTO_CLOUD_SYNC_BACKEND,
                "app_id": app_id,
                "has_snapshot": False,
                "snapshot_sequence_number": 0,
                "snapshot_exported_at_utc": "",
                "manifest_fingerprint": "",
                "last_applied_manifest_fingerprint": "",
                "startup_import_required": False,
                "manual_download_required": False,
                "runtime_has_user_content": runtime_root_has_user_content(
                    self.config_manager.app_docs_dir,
                    config_manager=self.config_manager,
                ),
                "last_successful_export_at": "",
                "last_successful_import_at": "",
                "source_launch": source_launch,
                "steam_session_ready": False,
                "steam_launch_tracked": bool(steam_launch_tracked),
                "steam_available": bool(steam_status["available"]),
                "steam_running": bool(steam_status["running"]),
                "steam_logged_on": bool(steam_status["logged_on"]),
                "runtime_root": str(self.config_manager.app_docs_dir),
                "cloudsave_root": str(self.config_manager.cloudsave_dir),
                "manifest_path": str(self.config_manager.cloudsave_manifest_path),
                "manifest_exists": False,
                "recommended_paths": recommended_paths,
                "current_platform_rule": current_platform_rule,
                "disabled": True,
                "disabled_reason": cloudsave_disabled_reason(),
            }

        bootstrap_local_cloudsave_environment(self.config_manager)
        cloud_state = self.config_manager.load_cloudsave_local_state()
        manifest = load_cloudsave_manifest(self.config_manager)
        manifest_files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
        manifest_fingerprint = str(manifest.get("fingerprint") or "")
        snapshot_sequence_number = int(manifest.get("sequence_number") or 0)
        snapshot_exported_at_utc = str(manifest.get("exported_at_utc") or "")
        snapshot_kind = str(manifest.get("snapshot_kind") or "")
        last_applied_manifest_fingerprint = str(cloud_state.get("last_applied_manifest_fingerprint") or "")
        runtime_has_user_content = runtime_root_has_user_content(
            self.config_manager.app_docs_dir,
            config_manager=self.config_manager,
        )
        has_snapshot = bool(manifest_files)
        snapshot_differs_from_runtime = bool(
            has_snapshot
            and (
                not last_applied_manifest_fingerprint
                or not manifest_fingerprint
                or manifest_fingerprint != last_applied_manifest_fingerprint
            )
        )
        startup_import_required = bool(
            has_snapshot
            and not runtime_has_user_content
            and snapshot_differs_from_runtime
        )
        manual_download_required = bool(
            has_snapshot
            and runtime_has_user_content
            and snapshot_differs_from_runtime
        )

        steam_status = _build_steam_connectivity_status(steamworks)
        app_id = _load_steam_app_id()
        steam_launch_tracked = _is_steam_launch_tracked(app_id)
        source_launch = bool(is_source_launch())
        steam_session_ready = bool(
            steam_status["available"]
            and not source_launch
            and steam_launch_tracked
        )
        recommended_paths = _build_recommended_root_paths(self.config_manager)
        current_platform_rule = _build_current_platform_rule_preview(self.config_manager)
        return {
            "backend": STEAM_AUTO_CLOUD_SYNC_BACKEND,
            "app_id": app_id,
            "has_snapshot": has_snapshot,
            "snapshot_sequence_number": snapshot_sequence_number,
            "snapshot_exported_at_utc": snapshot_exported_at_utc,
            "snapshot_kind": snapshot_kind,
            "manifest_fingerprint": manifest_fingerprint,
            "last_applied_manifest_fingerprint": last_applied_manifest_fingerprint,
            "startup_import_required": startup_import_required,
            "manual_download_required": manual_download_required,
            "runtime_has_user_content": runtime_has_user_content,
            "last_successful_export_at": str(cloud_state.get("last_successful_export_at") or ""),
            "last_successful_import_at": str(cloud_state.get("last_successful_import_at") or ""),
            "source_launch": source_launch,
            "steam_session_ready": steam_session_ready,
            "steam_launch_tracked": bool(steam_launch_tracked),
            "steam_available": bool(steam_status["available"]),
            "steam_running": bool(steam_status["running"]),
            "steam_logged_on": bool(steam_status["logged_on"]),
            "runtime_root": str(self.config_manager.app_docs_dir),
            "cloudsave_root": str(self.config_manager.cloudsave_dir),
            "manifest_path": str(self.config_manager.cloudsave_manifest_path),
            "manifest_exists": bool(self.config_manager.cloudsave_manifest_path.is_file()),
            "recommended_paths": recommended_paths,
            "current_platform_rule": current_platform_rule,
        }

    def import_if_needed(
        self,
        *,
        reason: str = "",
        force: bool = False,
        steamworks=None,
        deadline_monotonic: float | None = None,
        fence_already_active: bool = False,
    ) -> dict[str, Any]:
        if is_cloudsave_disabled():
            return _build_cloudsave_disabled_action_result(
                self.config_manager,
                action="skipped",
                reason="cloudsave_disabled",
                requested_reason=reason,
                steamworks=steamworks,
            )

        remote_bundle_result = self._try_download_remote_bundle(
            steamworks=steamworks,
            deadline_monotonic=deadline_monotonic,
        )
        status = self.build_status(steamworks=steamworks)
        if not status["has_snapshot"]:
            return {
                "success": True,
                "action": "skipped",
                "reason": "no_snapshot",
                "requested_reason": str(reason or ""),
                "hint": _build_missing_snapshot_hint(status),
                "remote_bundle_result": remote_bundle_result,
                "status": status,
            }
        if not force and not status["startup_import_required"]:
            reason_code = "manual_download_required" if status.get("manual_download_required") else "already_applied"
            result_payload = {
                "success": True,
                "action": "skipped",
                "reason": reason_code,
                "requested_reason": str(reason or ""),
                "remote_bundle_result": remote_bundle_result,
                "status": status,
            }
            if reason_code == "manual_download_required":
                result_payload["hint"] = _build_manual_download_hint(status)
            return result_payload
        result = import_local_cloudsave_snapshot(
            self.config_manager,
            deadline_monotonic=deadline_monotonic,
            use_cloud_apply_fence=not fence_already_active,
        )
        return {
            "success": True,
            "action": "imported",
            "requested_reason": str(reason or ""),
            "result": result,
            "remote_bundle_result": remote_bundle_result,
            "status": self.build_status(steamworks=steamworks),
        }

    def export_snapshot(
        self,
        *,
        reason: str = "",
        steamworks=None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        if is_cloudsave_disabled():
            return _build_cloudsave_disabled_action_result(
                self.config_manager,
                action="skipped",
                reason="cloudsave_disabled",
                requested_reason=reason,
                steamworks=steamworks,
            )

        result = export_local_cloudsave_snapshot(
            self.config_manager,
            deadline_monotonic=deadline_monotonic,
        )
        remote_bundle_result = self._try_upload_remote_bundle(
            steamworks=steamworks,
            deadline_monotonic=deadline_monotonic,
        )
        payload = {
            "success": True,
            "action": "exported",
            "requested_reason": str(reason or ""),
            "result": result,
            "remote_bundle_result": remote_bundle_result,
            "status": self.build_status(steamworks=steamworks),
        }
        return payload

    def upload_existing_snapshot(
        self,
        *,
        reason: str = "",
        steamworks=None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        if is_cloudsave_disabled():
            return _build_cloudsave_disabled_action_result(
                self.config_manager,
                action="skipped",
                reason="cloudsave_disabled",
                requested_reason=reason,
                steamworks=steamworks,
            )

        status = self.build_status(steamworks=steamworks)
        if not status["has_snapshot"]:
            return {
                "success": True,
                "action": "skipped",
                "reason": "no_local_snapshot",
                "requested_reason": str(reason or ""),
                "status": status,
            }

        remote_bundle_result = self._try_upload_remote_bundle(
            steamworks=steamworks,
            deadline_monotonic=deadline_monotonic,
        )
        remote_success = bool(remote_bundle_result.get("success", False))
        action = str(remote_bundle_result.get("action") or "")
        if remote_success or action == "skipped":
            return {
                "success": True,
                "action": "uploaded" if action == "uploaded" else "skipped",
                "requested_reason": str(reason or ""),
                "remote_bundle_result": remote_bundle_result,
                "status": self.build_status(steamworks=steamworks),
            }
        return {
            "success": False,
            "action": "failed",
            "reason": str(remote_bundle_result.get("reason") or "remote_bundle_upload_failed"),
            "requested_reason": str(reason or ""),
            "remote_bundle_result": remote_bundle_result,
            "status": self.build_status(steamworks=steamworks),
        }


def get_cloudsave_manager(config_manager) -> CloudSaveManager:
    return CloudSaveManager(config_manager)


def build_steam_autocloud_status(config_manager, *, steamworks=None) -> dict[str, Any]:
    return get_cloudsave_manager(config_manager).build_status(steamworks=steamworks)
