import shutil
from pathlib import Path

import pytest

from tests.node_harness import run_node_script


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOAST_TEMPLATE = PROJECT_ROOT / "templates" / "toast.html"
BEHAVIOR_TEST = (
    PROJECT_ROOT / "tests" / "frontend" / "toast_pointer_passthrough.test.cjs"
)


def test_status_toast_uses_precise_pointer_tracking_instead_of_fullscreen_capture():
    source = TOAST_TEMPLATE.read_text(encoding="utf-8")
    status_section = source.split("// ===== Status Toast =====", 1)[1].split(
        "// ===== Voice Preparing Toast =====", 1
    )[0]

    assert "getBoundingClientRect()" in status_section
    assert "api.getCursorPoint()" in status_section
    assert "setToastMouseThrough(!isCursorInsideStatusToast(point));" in status_section
    assert "if (api && api.setMouseThrough) api.setMouseThrough(false);" not in status_section
    assert "stopStatusPointerTracking(false);" in status_section


def test_prominent_notice_owns_mouse_state_across_async_status_results():
    source = TOAST_TEMPLATE.read_text(encoding="utf-8")
    prominent_section = source.split("// ===== Prominent Notice =====", 1)[1].split(
        "// ===== IPC 监听", 1
    )[0]

    assert "prominentNoticeInteractive = true;" in prominent_section
    assert "stopStatusPointerTracking(true);" in prominent_section
    assert "setToastMouseThrough(false);" in prominent_section
    assert "onDismiss();" in prominent_section
    assert "if (!prominentNoticeInteractive)" in prominent_section


def test_toast_pointer_passthrough_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for Toast pointer passthrough tests")

    run_node_script(
        node,
        "require(" + repr(str(BEHAVIOR_TEST)) + ");",
        check=True,
        cwd=PROJECT_ROOT,
        timeout=15,
    )
