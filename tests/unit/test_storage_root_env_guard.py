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
"""Behaviour of tests/storage_root_env_guard.py.

The guard's hook glue is three lines around these helpers; the helpers carry
the decisions (what counts as drift, what gets restored, what the message
names), so they are what gets pinned here.
"""

import os

import pytest

from tests import storage_root_env_guard as guard
from utils.storage.layout import (
    NEKO_STORAGE_ANCHOR_ROOT_ENV,
    NEKO_STORAGE_CLOUDSAVE_ROOT_ENV,
    NEKO_STORAGE_SELECTED_ROOT_ENV,
)

pytestmark = pytest.mark.unit


def test_guarded_keys_are_exactly_the_layout_exporter_keys():
    # export_storage_layout_to_env writes these three; a fourth key added there
    # without extending the guard would leak silently.
    assert set(guard.STORAGE_ROOT_ENV_KEYS) == {
        NEKO_STORAGE_SELECTED_ROOT_ENV,
        NEKO_STORAGE_ANCHOR_ROOT_ENV,
        NEKO_STORAGE_CLOUDSAVE_ROOT_ENV,
    }


def test_storage_root_env_is_absent_when_a_test_starts():
    # The invariant the session-start clear and the per-test restore together
    # promise: nothing inherited, nothing left over from an earlier test.
    for key in guard.STORAGE_ROOT_ENV_KEYS:
        assert key not in os.environ, key


def test_restore_puts_back_removed_and_overwritten_keys_and_reports_them():
    snapshot = {
        NEKO_STORAGE_SELECTED_ROOT_ENV: r"D:\real",
        NEKO_STORAGE_ANCHOR_ROOT_ENV: None,
        NEKO_STORAGE_CLOUDSAVE_ROOT_ENV: None,
    }
    env = {
        NEKO_STORAGE_SELECTED_ROOT_ENV: r"C:\tmp\leak",   # overwritten
        NEKO_STORAGE_ANCHOR_ROOT_ENV: r"C:\tmp\anchor",   # newly set
        "UNRELATED": "kept",
    }

    drifted = guard.restore_storage_root_env(snapshot, env)

    assert drifted == {
        NEKO_STORAGE_SELECTED_ROOT_ENV: (r"D:\real", r"C:\tmp\leak"),
        NEKO_STORAGE_ANCHOR_ROOT_ENV: (None, r"C:\tmp\anchor"),
    }
    assert env == {NEKO_STORAGE_SELECTED_ROOT_ENV: r"D:\real", "UNRELATED": "kept"}


def test_restore_reports_nothing_when_env_matches_snapshot():
    env = {NEKO_STORAGE_SELECTED_ROOT_ENV: r"C:\tmp\root"}
    snapshot = guard.snapshot_storage_root_env(env)

    assert guard.restore_storage_root_env(snapshot, env) == {}
    assert env == {NEKO_STORAGE_SELECTED_ROOT_ENV: r"C:\tmp\root"}


def test_restore_also_removes_a_key_the_test_deleted_then_set_back_differently():
    snapshot = {key: None for key in guard.STORAGE_ROOT_ENV_KEYS}
    env = {NEKO_STORAGE_CLOUDSAVE_ROOT_ENV: r"C:\tmp\cloud"}

    drifted = guard.restore_storage_root_env(snapshot, env)

    assert list(drifted) == [NEKO_STORAGE_CLOUDSAVE_ROOT_ENV]
    assert env == {}


def test_clear_inherited_removes_only_guarded_keys_and_returns_them():
    env = {
        NEKO_STORAGE_SELECTED_ROOT_ENV: r"D:\real",
        NEKO_STORAGE_CLOUDSAVE_ROOT_ENV: r"D:\real\cloudsave",
        "PATH": "x",
    }

    cleared = guard.clear_inherited_storage_root_env(env)

    assert cleared == {
        NEKO_STORAGE_SELECTED_ROOT_ENV: r"D:\real",
        NEKO_STORAGE_CLOUDSAVE_ROOT_ENV: r"D:\real\cloudsave",
    }
    assert env == {"PATH": "x"}
    assert guard.clear_inherited_storage_root_env(env) == {}


def test_leak_message_names_the_test_and_every_drifted_key():
    message = guard.describe_storage_root_env_leak(
        "tests/unit/test_x.py::test_y",
        {NEKO_STORAGE_SELECTED_ROOT_ENV: (None, r"D:\real")},
    )

    assert "tests/unit/test_x.py::test_y" in message
    assert NEKO_STORAGE_SELECTED_ROOT_ENV in message
    # repr, not the bare string: it is what separates an unset key from one set
    # to "" -- both of which the guard has to be able to report distinctly.
    assert repr(r"D:\real") in message
    assert "None" in message
    assert "monkeypatch" in message


def test_monkeypatched_env_is_not_reported_as_a_leak(monkeypatch):
    # monkeypatch restores in its own teardown, which runs before the guard's
    # post-teardown comparison; this test passing under the live guard is the
    # end-to-end proof that legitimate per-test overrides stay green.
    monkeypatch.setenv(NEKO_STORAGE_SELECTED_ROOT_ENV, r"C:\tmp\per-test-root")
    assert os.environ[NEKO_STORAGE_SELECTED_ROOT_ENV] == r"C:\tmp\per-test-root"
