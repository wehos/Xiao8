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

"""Cloudsave manifest load/save/ensure and the phase-0 local environment
bootstrap flow.

Split out of the former monolithic ``utils/cloudsave_runtime.py``.
"""

from __future__ import annotations

from typing import Any

from utils.file_utils import atomic_write_json
from utils.root_state_lock import root_state_transaction

from ._shared import (
    ROOT_MODE_DEFERRED_INIT,
    ROOT_MODE_NORMAL,
    _ensure_local_state_directory_or_raise,
    cloudsave_disabled_reason,
    is_cloudsave_disabled,
)
from .fence import _recover_stale_write_blocking_mode
from .legacy_migration import import_legacy_runtime_root_if_needed


def build_default_cloudsave_manifest(*, client_id: str = "") -> dict[str, Any]:
    """Build the minimal local manifest skeleton for phase 0."""
    return {
        "schema_version": 1,
        "min_reader_schema_version": 1,
        "min_app_version": "",
        "client_id": str(client_id or ""),
        "device_id": "",
        "sequence_number": 0,
        "exported_at_utc": "",
        "snapshot_kind": "",
        "files": {},
        "fingerprint": "",
    }


def load_cloudsave_manifest(config_manager, default_value: dict[str, Any] | None = None) -> dict[str, Any]:
    if default_value is None:
        cloud_state = config_manager.load_cloudsave_local_state()
        default_value = build_default_cloudsave_manifest(client_id=cloud_state.get("client_id", ""))
    return config_manager._load_json_file(config_manager.cloudsave_manifest_path, default_value)


def save_cloudsave_manifest(config_manager, data: dict[str, Any]) -> None:
    config_manager.ensure_cloudsave_structure()
    atomic_write_json(config_manager.cloudsave_manifest_path, data, ensure_ascii=False, indent=2)


def ensure_cloudsave_manifest(config_manager, *, preserve_existing_client_id: bool = False) -> dict[str, Any]:
    config_manager.ensure_cloudsave_structure()
    cloud_state = config_manager.load_cloudsave_local_state()
    manifest = load_cloudsave_manifest(
        config_manager,
        default_value=build_default_cloudsave_manifest(client_id=cloud_state.get("client_id", "")),
    )
    changed = False
    current_client_id = str(manifest.get("client_id") or "")
    expected_client_id = str(cloud_state.get("client_id", "") or "")
    if not current_client_id:
        manifest["client_id"] = expected_client_id
        changed = True
    elif not preserve_existing_client_id and current_client_id != expected_client_id:
        manifest["client_id"] = cloud_state.get("client_id", "")
        changed = True
    if "schema_version" not in manifest:
        manifest["schema_version"] = 1
        changed = True
    if "min_reader_schema_version" not in manifest:
        manifest["min_reader_schema_version"] = 1
        changed = True
    if "min_app_version" not in manifest:
        manifest["min_app_version"] = ""
        changed = True
    if "device_id" not in manifest:
        manifest["device_id"] = ""
        changed = True
    if "sequence_number" not in manifest:
        manifest["sequence_number"] = 0
        changed = True
    if "exported_at_utc" not in manifest:
        manifest["exported_at_utc"] = ""
        changed = True
    if "snapshot_kind" not in manifest:
        manifest["snapshot_kind"] = ""
        changed = True
    if "files" not in manifest or not isinstance(manifest.get("files"), dict):
        manifest["files"] = {}
        changed = True
    if "fingerprint" not in manifest:
        manifest["fingerprint"] = ""
        changed = True
    if changed or not config_manager.cloudsave_manifest_path.exists():
        save_cloudsave_manifest(config_manager, manifest)
    return manifest


def bootstrap_local_cloudsave_environment(config_manager) -> dict[str, Any]:
    """Initialize phase-0 local cloudsave skeleton and state files."""
    if is_cloudsave_disabled():
        return {
            "disabled": True,
            "disabled_reason": cloudsave_disabled_reason(),
            "root_state": config_manager.build_default_root_state(),
            "cloudsave_local_state": config_manager.build_default_cloudsave_local_state(client_id=""),
            "manifest": build_default_cloudsave_manifest(client_id=""),
            "legacy_import": {
                "migrated": False,
                "source": "",
                "copied_paths": [],
                "backup_path": "",
                "repair_reason": "",
                "result": "cloudsave_disabled",
            },
        }

    _ensure_local_state_directory_or_raise(config_manager, "preparing local cloudsave state")

    if not config_manager.ensure_cloudsave_structure():
        raise OSError("failed to ensure cloudsave directory structure")

    config_manager.ensure_cloudsave_state_files()

    root_state = config_manager.load_root_state()
    if str(root_state.get("mode") or ROOT_MODE_NORMAL) == ROOT_MODE_DEFERRED_INIT:
        cloud_state = config_manager.load_cloudsave_local_state()
        cloud_changed = False
        if not cloud_state.get("client_id"):
            cloud_state["client_id"] = config_manager.build_default_cloudsave_local_state()["client_id"]
            cloud_changed = True
        next_seq = int(cloud_state.get("next_sequence_number") or 0)
        if next_seq < 1:
            cloud_state["next_sequence_number"] = 1
            cloud_changed = True
        if cloud_changed:
            config_manager.save_cloudsave_local_state(cloud_state)

        manifest = ensure_cloudsave_manifest(config_manager, preserve_existing_client_id=True)
        return {
            "root_state": root_state,
            "cloudsave_local_state": config_manager.load_cloudsave_local_state(),
            "manifest": manifest,
            "legacy_import": {
                "migrated": False,
                "source": "",
                "copied_paths": [],
                "backup_path": "",
                "repair_reason": "",
                "result": "recovery_required",
            },
        }

    legacy_import = import_legacy_runtime_root_if_needed(config_manager)
    root_state, recovered_stale_mode = _recover_stale_write_blocking_mode(config_manager, root_state)

    # 下面这串编辑 + 末尾的 save_root_state 是一次读—改—写，必须整段进锁。
    #
    # 锁从这里才开始，故意不往上包：import_legacy_runtime_root_if_needed 可能整目录
    # 拷贝，_recover_stale_write_blocking_mode 会去拿跨进程的 cloud apply 锁——把
    # root_state 锁包在它外面会让锁序变成 root_state → cloud_apply，跟
    # cloud_apply_fence 的方向相反。全仓统一 cloud_apply_lock → root_state_transaction。
    with root_state_transaction():
        # 锁内重读：上面 load_root_state 那次到这里之间，storage_location 的变更路由
        # （现在跑在工作线程上）可能已经提交过一轮，拿旧 pre-image 编辑再存回去会把
        # 那一轮整份盖掉。
        latest_state = config_manager.load_root_state()
        if isinstance(latest_state, dict):
            root_state = latest_state

        root_changed = False
        app_root = str(config_manager.app_docs_dir)
        if root_state.get("current_root") != app_root:
            root_state["current_root"] = app_root
            root_changed = True
        if not root_state.get("last_known_good_root"):
            root_state["last_known_good_root"] = app_root
            root_changed = True
        if not root_state.get("last_successful_boot_at"):
            root_state["last_successful_boot_at"] = ""
            root_changed = True
        if legacy_import.get("source"):
            root_state["last_migration_source"] = str(legacy_import["source"])
            root_state["last_migration_result"] = str(legacy_import.get("result") or "")
            root_changed = True
            if legacy_import.get("backup_path"):
                root_state["last_migration_backup"] = str(legacy_import["backup_path"])
                root_changed = True
        elif recovered_stale_mode:
            root_changed = True
        elif not root_state.get("last_migration_result"):
            root_state["last_migration_result"] = str(legacy_import.get("result") or "bootstrap_initialized")
            root_changed = True
        if root_changed:
            config_manager.save_root_state(root_state)

    cloud_state = config_manager.load_cloudsave_local_state()
    cloud_changed = False
    if not cloud_state.get("client_id"):
        cloud_state["client_id"] = config_manager.build_default_cloudsave_local_state()["client_id"]
        cloud_changed = True
    next_seq = int(cloud_state.get("next_sequence_number") or 0)
    if next_seq < 1:
        cloud_state["next_sequence_number"] = 1
        cloud_changed = True
    if cloud_changed:
        config_manager.save_cloudsave_local_state(cloud_state)

    manifest = ensure_cloudsave_manifest(config_manager, preserve_existing_client_id=True)
    return {
        "root_state": config_manager.load_root_state(),
        "cloudsave_local_state": config_manager.load_cloudsave_local_state(),
        "manifest": manifest,
        "legacy_import": legacy_import,
    }
