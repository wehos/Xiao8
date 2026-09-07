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
"""Point every unpatched ConfigManager at a throwaway root instead of the user's.

``get_config_manager()`` defaults to ``migrate=True``, and migration is
triggered from nowhere else -- so the FIRST module-level
``_config_manager = get_config_manager()`` to be imported runs the whole
chain (config dir, card faces, memory files, the legacy Documents sweep and
the ``.mig-staging`` reclaim) against whatever root the machine resolves.
There are seven such module-level singletons and about three hundred bare
call sites; on a developer machine that root is the real one. Measured: the
suite created character directories in the user's ``memory/``, truncated
``config/core_config.json`` to ``{"coreApi": "free"}``, and left
``.mig-staging`` workspaces behind.

The lever is ConfigManager's own directory resolution, not ``LOCALAPPDATA``:
that variable is shared with unrelated tooling -- redirecting it sends uv's
wheel cache somewhere uv cannot write, and
``test_hatch_artifacts_explicitly_exclude_local_endpointing_weights`` fails
building an sdist for reasons that have nothing to do with N.E.K.O.

Three hooks, because the root is reachable three ways: the standard app-data
directory is where a fresh install lands, and the two legacy lists are what
the import sweep walks looking for an older install to adopt -- leaving
those pointed at the real Documents tree means the sweep still reads (and
stages inside) the user's data even when the destination is a temp dir.

Tests that patch these themselves are unaffected: ``patch.object`` installs
over this and restores back to it.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import tempfile
from pathlib import Path

# Escape hatch for inspecting what a run wrote into the stand-in root.
KEEP_ROOT_ENV = "NEKO_TEST_KEEP_APPDATA_ROOT"

_PATCHED_METHODS = (
    "_get_standard_data_directory_candidates",
    "_get_legacy_storage_candidates",
    "_get_legacy_document_candidates",
    "get_legacy_app_root_candidates",
)

_ISOLATED_ROOT: Path | None = None
_ORIGINALS: dict[str, object] = {}


def _discard_root(root: Path) -> None:
    """Delete the stand-in root at process exit.

    One per PROCESS, so under ``-n auto`` a full run mints one per worker plus
    one for the controller, and each holds a whole N.E.K.O tree -- config,
    memory, card faces, migration workspaces. Measured on a dev machine: three
    full runs left 42 directories and 45 MB behind. atexit rather than
    ``pytest_sessionfinish`` because the root is minted at conftest IMPORT
    time, before any hook or fixture exists, and a collection-time crash never
    reaches sessionfinish.

    ``ignore_errors`` because on Windows a still-open SQLite handle makes the
    unlink fail, and leaving a temp directory behind must never be what turns
    a green run red.
    """
    if os.environ.get(KEEP_ROOT_ENV, "").strip():
        return
    shutil.rmtree(root, ignore_errors=True)


def isolated_app_data_root() -> Path:
    """The throwaway parent directory that stands in for the app-data root."""
    global _ISOLATED_ROOT
    if _ISOLATED_ROOT is None:
        _ISOLATED_ROOT = Path(tempfile.mkdtemp(prefix="neko_test_appdata_"))
        atexit.register(_discard_root, _ISOLATED_ROOT)
    return _ISOLATED_ROOT


def install(config_manager_class) -> None:
    """Redirect the class's root resolution. Idempotent."""
    if getattr(config_manager_class, "_neko_test_root_isolated", False):
        return

    # getattr, not __dict__: these live on a storage-roots mixin, not on
    # ConfigManager itself, so a __dict__ lookup raises KeyError. Restoring
    # via setattr then shadows the mixin with the very same function.
    for name in _PATCHED_METHODS:
        _ORIGINALS[name] = getattr(config_manager_class, name)

    root = isolated_app_data_root()
    config_manager_class._get_standard_data_directory_candidates = lambda self: [root]
    config_manager_class._get_legacy_storage_candidates = lambda self: []
    config_manager_class._get_legacy_document_candidates = lambda self: []
    config_manager_class.get_legacy_app_root_candidates = lambda self: []
    config_manager_class._neko_test_root_isolated = True


@contextlib.contextmanager
def real_resolution(config_manager_class):
    """Put the genuine resolution methods back for one test.

    For the handful of tests whose subject IS this resolution -- platform
    branches of the app-data candidates, the legacy-candidate ordering, the
    CFA read-only-Documents fallback. They drive it inside their own tmp
    sandbox with ``sys.platform`` and ``Path.home`` patched, so the real
    methods there never reach the developer's directories.
    """
    if not _ORIGINALS:
        yield
        return
    stubs = {name: getattr(config_manager_class, name) for name in _PATCHED_METHODS}
    for name, original in _ORIGINALS.items():
        setattr(config_manager_class, name, original)
    try:
        yield
    finally:
        for name, stub in stubs.items():
            setattr(config_manager_class, name, stub)
