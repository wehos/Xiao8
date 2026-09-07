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

"""Every desktop matrix leg must build the architecture its runner is native to.

Issue #2898: the ``mac-x64`` leg ran on ``macos-15``, which is an Apple Silicon
image. ``actions/setup-python`` with ``architecture: x64`` puts Python under
Rosetta but Nuitka still emits arm64, so an unusable "Intel" build shipped for
months with a fully green pipeline.

The runner-label table below is the part that carries the weight: it is the fact
nobody checked. Adding a leg on an unlisted label fails here rather than silently
producing the wrong binary.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CROSS_PLATFORM_WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop.yml"
LINUX_WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop-linux.yml"
ARCH_GATE_SCRIPT = ROOT / "scripts" / "check_dist_arch.py"
GATE_STEP_NAME = "Verify dist CPU architecture"

# GitHub-hosted runner label -> the CPU architecture that image natively builds.
# Source: https://github.com/actions/runner-images README label table.
_RUNNER_NATIVE_ARCH = {
    "windows-latest": "x64",
    "macos-15-intel": "x64",
    "macos-15-large": "x64",
    "macos-15": "arm64",
    "macos-latest": "arm64",
    "ubuntu-latest": "x64",
    "ubuntu-24.04": "x64",
    "ubuntu-24.04-arm": "arm64",
    "ubuntu-22.04-arm": "arm64",
}


def _load(path: Path) -> dict:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _matrix_entries(workflow: dict, job: str) -> list[dict]:
    include = workflow["jobs"][job]["strategy"]["matrix"]["include"]
    if isinstance(include, list):
        return include
    # `include: ${{ fromJSON(inputs.windows_only && '[...]' || '[...]') }}`
    branches = re.findall(r"'(\[\{.*?\}\])'", include)
    assert len(branches) == 2, f"unexpected matrix expression for {job}: {include[:120]}"
    entries: list[dict] = []
    for branch in branches:
        entries.extend(json.loads(branch))
    return entries


def _steps(workflow: dict, job: str) -> dict[str, dict]:
    return {s["name"]: s for s in workflow["jobs"][job]["steps"] if "name" in s}


def test_arch_gate_script_exists() -> None:
    assert ARCH_GATE_SCRIPT.is_file()


@pytest.mark.parametrize("job", ["build-python", "build-electron"])
def test_every_leg_runs_on_a_known_runner_label(job: str) -> None:
    workflow = _load(CROSS_PLATFORM_WORKFLOW)
    unknown = sorted(
        {e["os"] for e in _matrix_entries(workflow, job)} - set(_RUNNER_NATIVE_ARCH)
    )
    assert not unknown, (
        f"{job} uses runner label(s) with no recorded native architecture: {unknown}. "
        "Add them to _RUNNER_NATIVE_ARCH after checking actions/runner-images, "
        "otherwise nothing stops a repeat of #2898."
    )


def test_build_python_legs_declare_the_arch_their_runner_actually_builds() -> None:
    """The #2898 assertion: mac-x64 on an Apple Silicon image must not pass."""
    workflow = _load(CROSS_PLATFORM_WORKFLOW)
    wrong = [
        (e["platform"], e["os"], e.get("expect_arch"), _RUNNER_NATIVE_ARCH[e["os"]])
        for e in _matrix_entries(workflow, "build-python")
        if e.get("expect_arch") != _RUNNER_NATIVE_ARCH[e["os"]]
    ]
    assert not wrong, f"leg(s) claim an arch their runner does not build natively: {wrong}"


def test_build_electron_mac_and_linux_legs_match_their_backend_runner() -> None:
    """The Electron shell and the Python backend for one platform must agree.

    Downloading an x64 backend into an arm64 shell (or the reverse) reproduces
    #2898 from the other side.
    """
    workflow = _load(CROSS_PLATFORM_WORKFLOW)
    backend_arch = {
        e["artifact_name"]: _RUNNER_NATIVE_ARCH[e["os"]]
        for e in _matrix_entries(workflow, "build-python")
    }
    mismatched = [
        (e["artifact_name"], e["os"], _RUNNER_NATIVE_ARCH[e["os"]], backend_arch[e["python_artifact"]])
        for e in _matrix_entries(workflow, "build-electron")
        if _RUNNER_NATIVE_ARCH[e["os"]] != backend_arch[e["python_artifact"]]
    ]
    assert not mismatched, f"Electron shell / backend architecture mismatch: {mismatched}"


def test_every_build_python_leg_carries_the_gate_inputs() -> None:
    workflow = _load(CROSS_PLATFORM_WORKFLOW)
    missing = [
        e["artifact_name"]
        for e in _matrix_entries(workflow, "build-python")
        if not e.get("expect_arch") or not e.get("expect_platform")
    ]
    assert not missing, f"matrix leg(s) without expect_arch/expect_platform: {missing}"


def test_cross_platform_workflow_runs_the_gate_before_upload() -> None:
    workflow = _load(CROSS_PLATFORM_WORKFLOW)
    steps = workflow["jobs"]["build-python"]["steps"]
    names = [s.get("name") for s in steps]
    assert GATE_STEP_NAME in names
    gate = _steps(workflow, "build-python")[GATE_STEP_NAME]
    assert "scripts/check_dist_arch.py" in gate["run"]
    assert "${{ matrix.expect_arch }}" in gate["run"]
    assert "${{ matrix.expect_platform }}" in gate["run"]
    # 闸门必须在产物上传之前跑，否则坏包照样发出去。
    assert names.index(GATE_STEP_NAME) < names.index("Upload Python backend artifact")


def test_linux_only_workflow_runs_the_same_gate_before_upload() -> None:
    workflow = _load(LINUX_WORKFLOW)
    steps = workflow["jobs"]["build-python"]["steps"]
    names = [s.get("name") for s in steps]
    assert GATE_STEP_NAME in names
    gate = _steps(workflow, "build-python")[GATE_STEP_NAME]
    assert "scripts/check_dist_arch.py" in gate["run"]
    assert "--expect-arch x64" in gate["run"]
    assert names.index(GATE_STEP_NAME) < names.index("Upload Python backend artifact")
