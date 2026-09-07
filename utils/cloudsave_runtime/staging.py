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

"""Staging workspace, hashing, atomic file copy, SQLite shadow copy and
tombstone-state primitives for cloudsave operations.

Split out of the former monolithic ``utils/cloudsave_runtime.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from utils.file_utils import atomic_write_json

# Late-bound package reference: tests monkeypatch
# ``utils.cloudsave_runtime._atomic_copy_file`` on the package facade, and
# ``_apply_runtime_file`` must see that patch, so the helper is resolved
# through the facade at call time instead of via this module's globals.
from utils import cloudsave_runtime as _facade

from ._shared import MANAGED_CLOUDSAVE_PREFIXES, logger


SQLITE_FILE_HEADER = b"SQLite format 3\x00"


def _json_canonical_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_staging_workspace(config_manager, prefix: str) -> Path:
    config_manager.ensure_cloudsave_structure()
    return Path(
        tempfile.mkdtemp(
            prefix=f"{prefix}-",
            dir=str(config_manager.cloudsave_staging_dir),
        )
    )


def _atomic_copy_file(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as temp_file, open(source_path, "rb") as source_file:
            shutil.copyfileobj(source_file, temp_file)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target_path)
    except Exception:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise


def _stage_json_file(stage_root: Path, relative_path: str, payload: Any) -> Path:
    target_path = stage_root / relative_path
    atomic_write_json(target_path, payload, ensure_ascii=False, indent=2)
    return target_path


def _stage_file_copy(stage_root: Path, relative_path: str, source_path: Path) -> Path:
    staged_path = stage_root / relative_path
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, staged_path)
    return staged_path


def _looks_like_sqlite_database(source_path: Path) -> bool:
    try:
        if source_path.stat().st_size < len(SQLITE_FILE_HEADER):
            return False
        with open(source_path, "rb") as file_obj:
            return file_obj.read(len(SQLITE_FILE_HEADER)) == SQLITE_FILE_HEADER
    except OSError:
        return False


def _run_sqlite_shadow_copy(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    journal_mode = ""

    with sqlite3.connect(str(source_path), timeout=5.0, isolation_level=None) as source_conn:
        source_conn.execute("PRAGMA busy_timeout = 5000")
        try:
            row = source_conn.execute("PRAGMA journal_mode").fetchone()
            journal_mode = str(row[0]).lower() if row and row[0] is not None else ""
        except sqlite3.DatabaseError:
            journal_mode = ""

        if journal_mode == "wal":
            try:
                checkpoint_row = source_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() or ()
                if checkpoint_row and int(checkpoint_row[0] or 0) != 0:
                    logger.warning(
                        "SQLite wal_checkpoint(TRUNCATE) reported busy=%s for %s; continuing with backup API",
                        checkpoint_row[0],
                        source_path,
                    )
            except sqlite3.DatabaseError as exc:
                logger.warning(
                    "SQLite wal_checkpoint(TRUNCATE) failed for %s; continuing with backup API: %s",
                    source_path,
                    exc,
                )

        with sqlite3.connect(str(target_path), timeout=5.0, isolation_level=None) as target_conn:
            target_conn.execute("PRAGMA busy_timeout = 5000")
            source_conn.backup(target_conn)
            quick_check = target_conn.execute("PRAGMA quick_check").fetchone()
            quick_check_result = str(quick_check[0]) if quick_check and quick_check[0] is not None else ""
            if quick_check_result.lower() != "ok":
                raise sqlite3.DatabaseError(
                    f"shadow copy integrity check failed for {source_path}: {quick_check_result or 'unknown'}"
                )


def _stage_memory_file(stage_root: Path, relative_path: str, source_path: Path) -> Path:
    if source_path.name != "time_indexed.db" or not _looks_like_sqlite_database(source_path):
        return _stage_file_copy(stage_root, relative_path, source_path)

    staged_path = stage_root / relative_path
    try:
        _run_sqlite_shadow_copy(source_path, staged_path)
        return staged_path
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"failed to create SQLite shadow copy for {source_path}: {exc}") from exc


def _apply_runtime_file(source_path: Path, target_path: Path) -> None:
    if source_path.name == "time_indexed.db" and _looks_like_sqlite_database(source_path):
        target_looks_like_sqlite = not target_path.exists() or _looks_like_sqlite_database(target_path)
        if target_looks_like_sqlite:
            try:
                _run_sqlite_shadow_copy(source_path, target_path)
                return
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(f"failed to apply SQLite backup copy for {target_path}: {exc}") from exc

    _facade._atomic_copy_file(source_path, target_path)


def _list_existing_cloudsave_files(config_manager) -> set[str]:
    existing_files: set[str] = set()
    for prefix in MANAGED_CLOUDSAVE_PREFIXES:
        prefix_path = config_manager.cloudsave_dir / prefix.rstrip("/")
        if not prefix_path.exists():
            continue
        for file_path in prefix_path.rglob("*"):
            if file_path.is_file():
                existing_files.add(str(file_path.relative_to(config_manager.cloudsave_dir)).replace("\\", "/"))
    return existing_files


def _cleanup_empty_parent_dirs(path: Path, stop_at: Path) -> None:
    current = path.parent
    while current != stop_at and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _build_manifest_fingerprint(
    *,
    client_id: str,
    sequence_number: int,
    files: dict[str, Any],
    snapshot_kind: str = "",
) -> str:
    payload = {
        "client_id": client_id,
        "sequence_number": int(sequence_number),
        "files": files,
    }
    normalized_snapshot_kind = str(snapshot_kind or "").strip()
    if normalized_snapshot_kind:
        payload["snapshot_kind"] = normalized_snapshot_kind
    return _sha256_bytes(_json_canonical_dumps(payload).encode("utf-8"))


def _normalize_tombstone_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    character_name = str(entry.get("character_name") or entry.get("name") or "").strip()
    if not character_name:
        return None
    try:
        sequence_number = int(entry.get("sequence_number") or 0)
    except (TypeError, ValueError):
        sequence_number = 0
    return {
        "character_name": character_name,
        "deleted_at": str(entry.get("deleted_at") or ""),
        "sequence_number": sequence_number,
    }


def _normalize_tombstones_state(payload: Any) -> dict[str, Any]:
    raw_entries = []
    if isinstance(payload, dict):
        raw_entries = payload.get("tombstones") or []
    elif isinstance(payload, list):
        raw_entries = payload

    normalized_entries: dict[str, dict[str, Any]] = {}
    for raw_entry in raw_entries:
        normalized_entry = _normalize_tombstone_entry(raw_entry)
        if normalized_entry is None:
            continue
        key = normalized_entry["character_name"]
        existing_entry = normalized_entries.get(key)
        if existing_entry is None or normalized_entry["sequence_number"] >= existing_entry["sequence_number"]:
            normalized_entries[key] = normalized_entry

    return {
        "version": 1,
        "tombstones": [
            normalized_entries[name]
            for name in sorted(normalized_entries)
        ],
    }


def _load_local_tombstones_state(config_manager) -> dict[str, Any]:
    return _normalize_tombstones_state(config_manager.load_character_tombstones_state())


def _save_local_tombstones_state(config_manager, payload: Any) -> dict[str, Any]:
    normalized_state = _normalize_tombstones_state(payload)
    config_manager.save_character_tombstones_state(normalized_state)
    return normalized_state


def _load_tombstone_names_from_state_path(state_path: Path) -> set[str]:
    payload = _load_json_if_exists(state_path)
    normalized_state = _normalize_tombstones_state(payload)
    return {
        entry["character_name"]
        for entry in normalized_state.get("tombstones") or []
        if isinstance(entry, dict) and entry.get("character_name")
    }


def _make_tombstones_catalog_payload(*, tombstones: list[dict[str, Any]], sequence_number: int, exported_at: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sequence_number": sequence_number,
        "exported_at_utc": exported_at,
        "tombstones": deepcopy(tombstones),
    }


def _load_json_if_exists(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except Exception:
        return None


# Public aliases for helpers consumed outside the package
# (``utils/steam_cloud_bundle.py``).
create_staging_workspace = _create_staging_workspace
atomic_copy_file = _atomic_copy_file
load_json_if_exists = _load_json_if_exists
