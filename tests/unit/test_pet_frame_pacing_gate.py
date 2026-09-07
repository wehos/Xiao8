"""Electron Pet window frame pacing gate: Live2D / VRM / MMD switch to timer-driven
ticks when the configured frame rate is below the display refresh rate.

The behavioural suite lives in ``tests/frontend/pet_frame_pacing_gate.test.cjs``
(node:test; it loads the real ``static/frame-pacing.js`` plus the three renderer
backends). This file is only the pytest entry point.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_pet_frame_pacing_gate_node_suite() -> None:
    from tests.node_harness import run_node_script

    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("node not found")

    test_path = ROOT / "tests" / "frontend" / "pet_frame_pacing_gate.test.cjs"
    result = run_node_script(
        node_path,
        test_path.read_text(encoding="utf-8"),
        cwd=ROOT,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
