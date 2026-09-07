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
"""Behaviour of tests/real_root_isolation.py."""

from pathlib import Path

import pytest

from tests import real_root_isolation as isolation
from utils.config_manager import ConfigManager

pytestmark = pytest.mark.unit


def test_no_unpatched_config_manager_can_reach_a_real_documents_tree():
    # The invariant the whole module exists for, asserted on the resolution
    # inputs rather than on the resolved app_docs_dir: that one legitimately
    # follows whatever storage_policy.json is on disk, and sibling tests write
    # policies pointing at their own tmp_path. Asserting the resolved root
    # therefore passes alone and fails in a full run -- measured.
    #
    # The legacy lists matter as much as the standard candidate: pointing only
    # the destination at a temp dir still lets the import sweep read the user's
    # Documents tree and mint migration workspaces inside it.
    config_manager = ConfigManager("N.E.K.O")
    stand_in = isolation.isolated_app_data_root()

    assert config_manager._get_standard_data_directory_candidates() == [stand_in]
    assert config_manager._get_legacy_storage_candidates() == []
    assert config_manager._get_legacy_document_candidates() == []
    assert config_manager.get_legacy_app_root_candidates() == []


def test_discard_root_deletes_the_tree(tmp_path, monkeypatch):
    monkeypatch.delenv(isolation.KEEP_ROOT_ENV, raising=False)
    root = tmp_path / "stand-in"
    (root / "config").mkdir(parents=True)
    (root / "config" / "characters.json").write_text("{}", encoding="utf-8")

    isolation._discard_root(root)

    assert not root.exists()


def test_discard_root_keeps_the_tree_when_the_escape_hatch_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv(isolation.KEEP_ROOT_ENV, "1")
    root = tmp_path / "stand-in"
    root.mkdir()

    isolation._discard_root(root)

    assert root.exists()


def test_discard_root_swallows_a_failed_removal(monkeypatch, tmp_path):
    # A still-open SQLite handle on Windows makes the unlink fail; a leftover
    # temp directory must never be what turns a green run red.
    monkeypatch.delenv(isolation.KEEP_ROOT_ENV, raising=False)
    monkeypatch.setattr(
        isolation.shutil,
        "rmtree",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("locked")) if not kw.get("ignore_errors") else None,
    )

    isolation._discard_root(tmp_path / "whatever")  # must not raise


def test_real_resolution_restores_the_genuine_methods_and_puts_the_stubs_back():
    stub = ConfigManager._get_standard_data_directory_candidates

    with isolation.real_resolution(ConfigManager):
        inside = ConfigManager._get_standard_data_directory_candidates
        assert inside is not stub

    assert ConfigManager._get_standard_data_directory_candidates is stub


def test_real_resolution_puts_the_stubs_back_even_when_the_body_raises():
    # try/except rather than pytest.raises: this asserts two things about one
    # event -- the exception still escapes, AND the stubs are back afterwards.
    # Spelling the control flow out keeps both visible (and keeps static
    # analysers from reading everything after the block as unreachable).
    stub = ConfigManager._get_standard_data_directory_candidates
    escaped = False

    try:
        with isolation.real_resolution(ConfigManager):
            raise RuntimeError("boom")
    except RuntimeError:
        escaped = True

    assert escaped, "real_resolution swallowed the body's exception"
    assert ConfigManager._get_standard_data_directory_candidates is stub


def test_install_is_idempotent_and_does_not_capture_its_own_stub():
    # A second install() must not overwrite the saved originals with the stubs
    # it already put there -- that would make real_resolution restore a stub.
    before = dict(isolation._ORIGINALS)

    isolation.install(ConfigManager)

    assert isolation._ORIGINALS == before
    with isolation.real_resolution(ConfigManager):
        assert ConfigManager._get_standard_data_directory_candidates.__name__ == (
            "_get_standard_data_directory_candidates"
        )


def test_the_stand_in_root_is_stable_within_a_process():
    assert isolation.isolated_app_data_root() is isolation.isolated_app_data_root()
    assert isinstance(isolation.isolated_app_data_root(), Path)
