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

"""Plugins that were installed but never started by the user.

A plugin's manifest declares ``plugin_runtime.auto_start`` and it defaults to
true, so a freshly installed plugin would run its own code at the next greeting
without the user ever having started it. Installing and running are different
acts, and only the second one is the user's.

The record is deliberately a list of plugins **awaiting** approval rather than a
list of approved ones. An approved-list needs a baseline — some moment where
every already-installed plugin is grandfathered in — and getting that baseline
wrong in the quiet direction (seeding it while the registry happens to be empty,
or before a refresh that failed) permanently silences a user's whole autostart
set. With a pending-list, the absence of a record means "not our business", so
the failure mode of every bug in this file is a plugin autostarting the way it
always did.

Entries are added when a plugin is newly installed and removed the first time
the user starts or enables it — and only once that start has been durably
recorded. Clearing before the runtime preference lands would grant autostart on
the strength of an intent that never persisted.

The in-memory set never claims more than what is on disk: a failed write rolls
its mutation back. Letting the two diverge is what turns a full disk into either
a plugin autostarting without approval (a lost ``mark``) or one that has to be
started by hand after every restart (a lost ``clear``), in both cases with
nothing to retry it.
"""

from __future__ import annotations

import threading

from plugin.logging_config import get_logger

logger = get_logger("server.infrastructure.autostart_approvals")

PENDING_FILENAME = "plugin_autostart_pending.json"

_lock = threading.Lock()
_cache: set[str] | None = None
# 上一次读盘是不是失败了。读侧照常 fail-open（读不出来=照常自启），但写侧不行：
# 拿一个"空集"当基线保存下去，会把盘上其它插件的待批准记录一起抹掉，它们从此
# 无声地获得自启资格（codex）。
_load_failed = False


def _load_locked() -> set[str]:
    global _cache, _load_failed
    if _cache is not None and not _load_failed:
        return _cache
    # 上一次读失败留下的空集只能用来回答"能不能自启"，不能当成后续操作的基线，
    # 所以每次都再试一次读，直到读成功为止（codex）。
    try:
        from utils.config_manager import get_config_manager

        raw = get_config_manager().load_json_config(PENDING_FILENAME)
    except FileNotFoundError:
        _load_failed = False
        _cache = set()
        return _cache
    except Exception as exc:
        # 读不出来就当作没有待批准记录。这个方向的错误是"新装插件照常自启"，
        # 也就是这个功能出现之前的行为；反方向是"用户的插件集体不启动"。
        # ⚠️ 但要留痕：这个空集只能用来回答"能不能自启"，不能当成写盘的基线。
        logger.error("failed to load {}: {}", PENDING_FILENAME, exc)
        _load_failed = True
        _cache = set()
        return _cache

    _load_failed = False
    pending: set[str] = set()
    if isinstance(raw, dict):
        items = raw.get("pending")
        if isinstance(items, list):
            pending = {item for item in items if isinstance(item, str) and item}
    _cache = pending
    return _cache


def _save_locked(pending: set[str]) -> bool:
    """Persist the pending set. Returns whether it actually reached disk."""
    if _load_failed:
        # 读还是失败的，那手上这个集合就不是盘面的全集。保存是整文件替换，
        # 用它落盘会把盘上其它插件的待批准记录一起抹掉，它们从此无声地获得
        # 自启资格（codex）。读侧继续 fail-open，写侧到此为止。
        logger.error(
            "refusing to persist {}: the store could not be read, and saving now "
            "would drop the records already on disk",
            PENDING_FILENAME,
        )
        return False
    try:
        from utils.config_manager import get_config_manager

        get_config_manager().save_json_config(
            PENDING_FILENAME, {"pending": sorted(pending)}
        )
    except Exception as exc:
        logger.error("failed to persist {}: {}", PENDING_FILENAME, exc)
        return False
    return True


def mark_autostart_pending(plugin_id: str) -> bool:
    """Record that ``plugin_id`` was installed but never started by the user.

    Returns whether the record is durable. A caller that promotes or starts new
    code must treat ``False`` as a reason not to proceed: without the record the
    code is autostart-eligible at the next boot despite never having been
    started.
    """
    normalized = str(plugin_id or "").strip()
    if not normalized:
        return True
    with _lock:
        pending = _load_locked()
        if normalized in pending:
            return True
        pending.add(normalized)
        if not _save_locked(pending):
            # 写盘失败就把内存改回去。留着的话，本进程以为这个插件被拦住了，而盘上
            # 根本没有这条记录——重启之后它未经批准就自启，而且没有任何东西会重试
            # （greptile 指出的是相反那半，这半更要紧）。回滚之后内存和盘面一致：
            # 这个插件这一轮就是没被拦住，日志里留着原因。
            pending.discard(normalized)
            logger.error(
                "plugin {} could not be recorded as awaiting approval; it will "
                "autostart as before",
                normalized,
            )
            return False
        logger.info(
            "plugin {} installed; it will not autostart until the user starts it",
            normalized,
        )
        return True


def clear_autostart_pending(plugin_id: str) -> bool:
    """Record that the user started or enabled ``plugin_id`` themselves.

    Returns whether the approval is now durable. A caller must not report the
    start as fully persisted on ``False``: the plugin stays pending, so it gets
    held back from autostart again after a restart, and swallowing that leaves
    the user with no explanation for why.
    """
    normalized = str(plugin_id or "").strip()
    if not normalized:
        return True
    with _lock:
        pending = _load_locked()
        if normalized not in pending:
            return True
        pending.discard(normalized)
        if not _save_locked(pending):
            # 同上，反方向：内存说已批准而盘上还留着待批准记录的话，调用方会把这次
            # 批准当成已完成，重启后旧文件又把它拦下来，而没有人知道为什么
            # （greptile）。回滚，让下一次启动重试这次写入。
            pending.add(normalized)
            logger.error(
                "plugin {} was started by the user but the approval could not be "
                "persisted; it stays pending until the next successful start",
                normalized,
            )
            return False
        return True


def is_autostart_approved(plugin_id: str) -> bool:
    """Whether ``plugin_id`` may start itself at server startup."""
    with _lock:
        return str(plugin_id or "").strip() not in _load_locked()


def _reset_cache_for_testing() -> None:
    global _cache, _load_failed
    with _lock:
        _cache = None
        _load_failed = False
