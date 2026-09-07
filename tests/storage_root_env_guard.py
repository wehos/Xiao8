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
"""Keep the storage-root env vars from steering a test into real user data.

``ConfigManager`` honours ``NEKO_STORAGE_SELECTED_ROOT`` (and the anchor /
cloudsave siblings) above every other root candidate: when the variable is
set, the patched ``_get_documents_directory`` and
``_get_standard_data_directory_candidates`` that test helpers install are
never consulted (see ``utils/config_manager/storage_roots.py``). The launcher
and the plugin host export exactly these keys into ``os.environ`` for their
child processes, computed from the machine's REAL storage policy. So a test
that reaches either exporter unpatched, or sets one of the keys by hand and
never restores it, leaves the developer's real runtime root in the
environment for every later test of the session -- and each of those then
constructs a "temporary" ``ConfigManager`` that writes into the real root.

Measured on 2026-09-01 on a developer machine: about fifty fixture
directories appeared under ``memory/``, ``config/characters.json`` was
replaced by ``{"current":"A"}`` (six real characters gone from the roster),
``workshop_config.json`` pointed at a pytest tmp dir, and ``root_state.json``
recorded a pytest path as its last migration source.

Two layers, both runtime checks so they are indifferent to how the value was
set:

- session start drops any inherited value with a visible warning, so a shell
  started from the app cannot hand its layout to the test process;
- every test is wrapped: the keys are snapshotted before setup and restored
  after teardown, and a test that changed them without cleaning up errors
  with the diff. ``monkeypatch`` restores during teardown, so its users are
  never flagged; the check runs after all finalizers.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

import pytest

from utils.storage.layout import (
    NEKO_STORAGE_ANCHOR_ROOT_ENV,
    NEKO_STORAGE_CLOUDSAVE_ROOT_ENV,
    NEKO_STORAGE_SELECTED_ROOT_ENV,
)

STORAGE_ROOT_ENV_KEYS: tuple[str, ...] = (
    NEKO_STORAGE_SELECTED_ROOT_ENV,
    NEKO_STORAGE_ANCHOR_ROOT_ENV,
    NEKO_STORAGE_CLOUDSAVE_ROOT_ENV,
)

_SNAPSHOT_KEY: pytest.StashKey[dict[str, str | None]] = pytest.StashKey()


def snapshot_storage_root_env(environ: MutableMapping[str, str] | None = None) -> dict[str, str | None]:
    env = os.environ if environ is None else environ
    return {key: env.get(key) for key in STORAGE_ROOT_ENV_KEYS}


def restore_storage_root_env(
    snapshot: dict[str, str | None],
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, tuple[str | None, str | None]]:
    """Put every guarded key back to ``snapshot``; return ``{key: (expected, found)}`` for the ones that had drifted."""
    env = os.environ if environ is None else environ
    drifted: dict[str, tuple[str | None, str | None]] = {}
    for key in STORAGE_ROOT_ENV_KEYS:
        expected = snapshot.get(key)
        found = env.get(key)
        if found == expected:
            continue
        drifted[key] = (expected, found)
        if expected is None:
            env.pop(key, None)
        else:
            env[key] = expected
    return drifted


def clear_inherited_storage_root_env(environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Drop the guarded keys; return what was removed."""
    env = os.environ if environ is None else environ
    cleared: dict[str, str] = {}
    for key in STORAGE_ROOT_ENV_KEYS:
        value = env.pop(key, None)
        if value is not None:
            cleared[key] = value
    return cleared


def describe_storage_root_env_leak(nodeid: str, drifted: dict[str, tuple[str | None, str | None]]) -> str:
    lines = [
        f"{nodeid} 改了存储根环境变量却没有恢复（已替它还原）：",
    ]
    for key, (expected, found) in drifted.items():
        lines.append(f"  {key}: {expected!r} -> {found!r}")
    lines.append(
        "这些变量在 ConfigManager 里压过所有 patch 过的目录候选，泄漏到后面的用例就会把"
        "临时 ConfigManager 指向开发机的真实运行根并写脏用户数据。"
        "改用 monkeypatch.setenv/delenv，或者把 launcher/plugin host 里的 "
        "export_storage_layout_to_env 调用 patch 掉。"
    )
    return "\n".join(lines)


def pytest_sessionstart(session: pytest.Session) -> None:
    cleared = clear_inherited_storage_root_env()
    if cleared:
        session.config.issue_config_time_warning(
            pytest.PytestConfigWarning(
                "已从测试进程环境里清掉继承来的存储根变量，否则每个临时 ConfigManager 都会指向"
                f"真实运行根：{cleared}"
            ),
            stacklevel=2,
        )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item):
    # tryfirst + wrapper: the snapshot is taken before any fixture runs.
    item.stash[_SNAPSHOT_KEY] = snapshot_storage_root_env()
    yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    outcome = yield
    # After yield every fixture finalizer (monkeypatch included) has run, so
    # whatever is still different from the snapshot was left behind on purpose
    # or by accident -- either way it must not reach the next test.
    snapshot = item.stash.get(_SNAPSHOT_KEY, None)
    if snapshot is None:
        return
    drifted = restore_storage_root_env(snapshot)
    if drifted and outcome.excinfo is None:  # do not mask a teardown that already failed
        pytest.fail(describe_storage_root_env_leak(item.nodeid, drifted), pytrace=False)
