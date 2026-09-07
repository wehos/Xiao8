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
"""Every node-driving test must go through tests/node_harness.

Hand-rolled ``subprocess.run`` calls to node have broken this suite twice, both
times in a way that hides what actually went wrong:

* ``node -e <script>`` blows past Windows' 32767-character command line and
  raises ``WinError 206`` before node starts, so no assertion in the test runs.
* ``text=True`` without ``encoding`` encodes stdin with the host locale, so a
  harness carrying CJK passes on a UTF-8-configured machine and dies with
  ``UnicodeEncodeError`` on a stock English Windows — i.e. on every CI runner.

Both are invisible locally to whoever writes the harness.  The shared launcher
pins the temp-file form and UTF-8, so this test keeps new harnesses on it
rather than re-deriving the raw call.

Discovered by walking the AST, not from a hand-maintained file list: a list is
exactly what a new harness file would slip past.
"""

import ast
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests import node_harness
from tests.node_harness import (
    NodeHarnessSpawnTimeout,
    run_node_script,
    run_node_stdin,
)
from tests.repo_ast_cache import parse_source_file

TESTS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS_ROOT.parent
# The launcher itself is the one place allowed to call subprocess.run on node.
EXEMPT = {TESTS_ROOT / "node_harness.py"}


def _mentions_node(call: ast.Call) -> bool:
    """True when this subprocess.run call is driving node."""
    for node in ast.walk(call):
        if isinstance(node, ast.Name) and "node" in node.id.lower():
            return True
        if isinstance(node, ast.Attribute) and "node" in node.attr.lower():
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.lower() in {"node", "node.exe"} or node.value.lower().endswith("/node"):
                return True
    return False


# 全部 subprocess 入口，不只是 run：漏一个（比如 check_call）就等于给新
# harness 留了一条绕过这条契约、退回 node -e 的合法路径（Codex P2）。
_ENTRY_POINTS = frozenset({"run", "Popen", "check_output", "check_call", "call"})


def _subprocess_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Which names in this file resolve to subprocess.

    The module is not always spelled ``subprocess``: ``import subprocess as sp``
    and ``from subprocess import run`` are both ordinary, and matching only the
    literal ``subprocess.`` prefix leaves either one as a way around this
    contract.
    """
    module_aliases = {"subprocess"}
    direct_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _ENTRY_POINTS:
                    direct_names.add(alias.asname or alias.name)
    return module_aliases, direct_names


def _subprocess_run_calls(tree: ast.AST):
    module_aliases, direct_names = _subprocess_bindings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if (
                func.attr in _ENTRY_POINTS
                and getattr(func.value, "id", None) in module_aliases
            ):
                yield node
        elif isinstance(func, ast.Name) and func.id in direct_names:
            yield node


def test_node_harnesses_go_through_the_shared_launcher():
    offenders = []
    scanned = 0
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if path in EXEMPT:
            continue
        try:
            tree = parse_source_file(path)
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        scanned += 1
        for call in _subprocess_run_calls(tree):
            if _mentions_node(call):
                offenders.append(f"{path.relative_to(TESTS_ROOT).as_posix()}:{call.lineno}")

    assert scanned > 50, f"扫描面太小，断言已失效（只扫到 {scanned} 个文件）"
    assert not offenders, (
        "这些地方直接用 subprocess 跑 node，绕开了 tests/node_harness 的"
        f"命令行长度与 UTF-8 兜底：{offenders}"
    )


def test_unit_tests_workflow_pins_locked_pyclipper():
    """The workflow's standalone pyclipper install must track uv.lock.

    It is installed outside ``uv sync`` because the group carrying it also
    carries opencv, so nothing else keeps the two in step: an index update
    could otherwise hand an unchanged commit a different release and turn the
    workflow red. Pinning without this check just moves the drift somewhere
    nobody looks.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "unit-tests.yml").read_text(
        encoding="utf-8"
    )
    pinned = re.search(r"uv pip install pyclipper==([\w.]+)", workflow)
    assert pinned, "unit-tests.yml 里的 pyclipper 安装必须钉版本"

    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = [p["version"] for p in lock["package"] if p["name"] == "pyclipper"]
    assert locked, "uv.lock 里找不到 pyclipper，断言已失效"
    assert pinned.group(1) == locked[0], (
        f"workflow 钉的是 {pinned.group(1)}，uv.lock 解析的是 {locked[0]}"
    )


@pytest.mark.parametrize("runner", ["run_node_script", "run_node_stdin"])
def test_shared_launcher_pins_utf8(runner):
    """Both runners must pin the encoding rather than inherit the locale."""
    source = (TESTS_ROOT / "node_harness.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == runner
    )
    body = ast.get_source_segment(source, func) or ""
    assert "_utf8(kwargs)" in body, f"{runner} 必须把 kwargs 过一遍 _utf8()"

    helper = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_utf8"
    )
    helper_body = ast.get_source_segment(source, helper) or ""
    assert '"encoding"] = "utf-8"' in helper_body, "_utf8 必须强制 encoding，而不是 setdefault"


def _as_text(blob) -> str:
    """Decode what a stalled attempt emitted, whichever platform produced it.

    ``subprocess.run`` re-runs ``communicate()`` after killing the child only on
    Windows, so only there do ``TimeoutExpired.stdout``/``.stderr`` come back
    decoded. On POSIX they are whatever ``_check_timeout`` joined together --
    raw bytes -- and a test comparing against ``str`` is green on the CI runner
    and red on any developer's machine.
    """
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace")
    return blob


def _node_or_skip() -> str:
    node_path = shutil.which("node")
    if not node_path:
        pytest.skip("node is required for the launcher's own liveness tests")
    return node_path


# A script that finishes its work but leaves a timer armed. Node's event loop
# stays alive forever on it, which is the shape every "harness hangs" report in
# this repo has taken.
_LEAKS_A_TIMER = "setInterval(function () {}, 1000);\nprocess.stdout.write('started');\n"


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_leaks_a_timer_fails_with_the_leak_named(runner):
    """A never-settling script must die from inside node, saying what held it.

    Before the watchdog this ran out the caller's ``subprocess.run`` ceiling and
    surfaced as a bare ``TimeoutExpired`` naming only ``node.EXE`` and a temp
    file - indistinguishable from a runner that never started node at all, and
    with nothing in it to act on.
    """
    node_path = _node_or_skip()

    # 3s, not the caller-typical 10-60: this test deliberately waits out the
    # deadline, so the budget is CI time spent on purpose.
    result = runner(
        node_path, _LEAKS_A_TIMER, capture_output=True, check=False, timeout=3
    )

    assert result.returncode == node_harness._WATCHDOG_EXIT_CODE, (
        "泄漏 handle 的脚本必须由 watchdog 结束，而不是被外层 ceiling 杀掉："
        f"rc={result.returncode} stderr={result.stderr!r}"
    )
    assert "[node_harness]" in result.stderr
    assert "Timeout" in result.stderr, (
        f"诊断必须点名还占着事件循环的 handle：{result.stderr!r}"
    )
    # The script's own output is still there: the watchdog reports, it does not
    # replace what the harness was saying.
    assert result.stdout == "started"


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_healthy_harness_is_untouched_by_the_watchdog(runner):
    """The dual: a script that settles must see no trace of the guard.

    Without this the watchdog could "pass" the test above by firing on
    everything, and every caller asserting ``result.stdout == "ok"`` or
    ``result.stderr == ""`` would go red.
    """
    node_path = _node_or_skip()

    result = runner(
        node_path,
        "process.stdout.write('ok');\n",
        capture_output=True,
        check=False,
        timeout=6,
    )

    assert result.returncode == 0
    assert result.stdout == "ok"
    assert result.stderr == ""


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
@pytest.mark.parametrize(
    "opening",
    [
        pytest.param("'use strict'; // 说明\n", id="directive-with-trailing-comment"),
        pytest.param("'use strict'; let first = 1;\n", id="directive-sharing-its-line"),
        pytest.param('"use strict"\n', id="double-quoted-no-semicolon"),
    ],
)
def test_use_strict_survives_in_its_less_tidy_forms(runner, opening):
    """A directive is a statement, not a line, so the splice point is a column.

    Anchoring the match to a whole line covers only the tidiest spelling; the
    other three put the prologue in front of the directive and take strict mode
    with it, silently and greenly.
    """
    node_path = _node_or_skip()

    script = opening + "undeclaredOnPurpose = 1;\nprocess.stdout.write('ok');\n"
    result = runner(node_path, script, capture_output=True, check=False, timeout=6)

    assert result.returncode != 0, (
        f"这种写法下 'use strict' 被挤掉了：rc={result.returncode} "
        f"stdout={result.stdout!r}"
    )
    assert "ReferenceError" in result.stderr, result.stderr[:400]


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_leading_hashbang_still_parses(runner):
    """Node honours ``#!`` only at the very start of the source."""
    node_path = _node_or_skip()

    script = "#!/usr/bin/env node\nprocess.stdout.write('ok');\n"
    result = runner(node_path, script, capture_output=True, check=False, timeout=6)

    assert result.returncode == 0, (
        f"prologue 挤到 hashbang 前面会直接语法错误：stderr={result.stderr[:300]!r}"
    )
    assert result.stdout == "ok"


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_shadows_globalThis_still_runs(runner):
    """``const globalThis`` puts that name in the TDZ for the whole module.

    The prologue runs before the declaration initialises, so reading the bare
    name would throw before the harness reached its first statement. The global
    is fetched through ``Function('return this')()`` instead, which does not
    participate in the caller's lexical scope.
    """
    node_path = _node_or_skip()

    script = (
        "process.stdout.write('ok');\n"
        "const globalThis = { fake: true };\n"
        "void globalThis;\n"
    )
    result = runner(node_path, script, capture_output=True, check=False, timeout=6)

    assert result.returncode == 0, (
        f"被影子化的 globalThis 不该把 prologue 打挂：stderr={result.stderr[:300]!r}"
    )
    assert result.stdout == "ok"


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_leading_use_strict_survives_the_prologue(runner):
    """The guard must go after a directive, never in front of it.

    ``tests/unit/test_assistant_speech_finalize_static.py`` opens its harness
    with ``'use strict';``. A statement placed before it demotes it from a
    directive to an ordinary string expression -- silently, with strict mode
    gone and the harness still green. Here the assignment to an undeclared name
    is the witness: it throws under strict mode and quietly succeeds without it.
    """
    node_path = _node_or_skip()

    script = "'use strict';\nundeclaredOnPurpose = 1;\nprocess.stdout.write('ok');\n"
    result = runner(node_path, script, capture_output=True, check=False, timeout=6)

    assert result.returncode != 0, (
        f"'use strict' 被挤掉了：赋值给未声明变量本该抛 ReferenceError，"
        f"却拿到 rc={result.returncode} stdout={result.stdout!r}"
    )
    assert "ReferenceError" in result.stderr, result.stderr[:400]


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_the_prologue_does_not_renumber_the_scripts_own_lines(runner):
    """A stack trace must still point at the line the caller wrote.

    The prologue rides on the front of the first statement's line rather than
    taking a line of its own, so a throw on the script's line 3 still reports
    line 3.
    """
    node_path = _node_or_skip()

    script = "const a = 1;\nconst b = 2;\nthrow new Error('boom');\n"
    result = runner(node_path, script, capture_output=True, check=False, timeout=6)

    assert result.returncode != 0
    # <file>:<line>:<column>, so a bare ":3" also matches a column of 3 on the
    # wrong line. Mutation confirmed it: "prologue given its own line" survived.
    assert re.search(r":3:\d+", result.stderr), (
        f"抛错行号应仍是脚本自己的第 3 行：{result.stderr[:400]!r}"
    )
    assert not re.search(r":4:\d+", result.stderr), (
        f"行号被 prologue 挤下去了一行：{result.stderr[:400]!r}"
    )


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_script_that_exits_itself_cannot_escape_its_deadline(runner):
    """``process.exit(0)`` in the top level never reaches appended code.

    The dual of the natural-completion case, and it defeats an arming-time check
    too: the script terminates before the watchdog is even installed. Only a
    hook on the exit itself sees it. Measured: an 800ms busy loop followed by
    ``process.exit(0)`` under a 0.2s timeout came back rc=0 before this.
    """
    node_path = _node_or_skip()

    script = (
        "var until = Date.now() + 800;\n"
        "while (Date.now() < until) {}\n"
        "process.stdout.write('done');\n"
        "process.exit(0);\n"
    )
    result = runner(node_path, script, capture_output=True, check=False, timeout=0.2)

    assert result.returncode == node_harness._WATCHDOG_EXIT_CODE, (
        "脚本自己 exit(0) 也不能绕过调用方的 timeout："
        f"rc={result.returncode} stdout={result.stdout!r}"
    )
    assert "still running" in result.stderr


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_script_inside_its_budget_keeps_its_own_exit_code(runner):
    """The dual: the hook must only fire when the budget is actually blown.

    Ten harnesses end on ``process.exit(1)`` to signal their own failure. A hook
    that rewrote every exit code would turn each of those into 87 and make the
    two indistinguishable.
    """
    node_path = _node_or_skip()

    result = runner(
        node_path,
        "process.stdout.write('bye');\nprocess.exit(3);\n",
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 3, f"预算之内的退出码必须原样保留：{result.returncode}"
    assert result.stdout == "bye"
    assert result.stderr == ""


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_script_that_merely_runs_long_still_fails_its_deadline(runner):
    """A script that overruns and then finishes must not pass.

    Moving the caller's timeout off ``subprocess.run`` and into the script put
    this at risk: the watchdog is unref'd, so on an otherwise empty loop node
    exits 0 before an overdue timer can fire, and a 1.5s script under a 0.5s
    timeout came back green (measured) where it used to be killed at 0.5s. The
    overdue case is therefore decided synchronously when the watchdog arms.
    """
    node_path = _node_or_skip()

    script = (
        "var until = Date.now() + 1500;\n"
        "while (Date.now() < until) {}\n"
        "process.stdout.write('done');\n"
    )
    result = runner(node_path, script, capture_output=True, check=False, timeout=0.5)

    assert result.returncode == node_harness._WATCHDOG_EXIT_CODE, (
        "跑超了预算再结束的脚本必须红，否则调用方的 timeout 什么也不保证："
        f"rc={result.returncode} stdout={result.stdout!r}"
    )
    assert "still running" in result.stderr, (
        f"诊断要说清是「跑太久」而不是「卡住不动」：{result.stderr!r}"
    )


def test_a_heavy_synchronous_top_level_does_not_push_the_watchdog_past_the_ceiling(
    monkeypatch,
):
    """The deadline is measured from node start, not from when the watchdog arms.

    The watchdog is appended, so it arms only after the script's synchronous top
    level finishes - but the outer ceiling has been running since the spawn. Arm
    for the full deadline and the two clocks drift apart by exactly that top
    level; any top level heavier than the slack and the ceiling fires first,
    taking the diagnosis with it.

    Driven with a shrunken slack so the case costs ~2s instead of ~9s.
    """
    node_path = _node_or_skip()
    monkeypatch.setattr(node_harness, "_SPAWN_SLACK_SECONDS", 1.0)

    # 2s of synchronous top level - twice the slack - and then a leaked timer.
    script = (
        "var until = Date.now() + 2000;\n"
        "while (Date.now() < until) {}\n"
        "setInterval(function () {}, 1000);\n"
        "process.stdout.write('started');\n"
    )
    result = run_node_script(
        node_path, script, capture_output=True, check=False, timeout=2
    )

    assert result.returncode == node_harness._WATCHDOG_EXIT_CODE, (
        "同步 top-level 吃掉的时间必须算进 deadline，否则 watchdog 会被外层 ceiling "
        f"抢先杀掉、诊断全丢：rc={result.returncode} stderr={result.stderr!r}"
    )
    assert "[node_harness]" in result.stderr


def test_the_spawn_ceiling_sits_above_the_script_deadline(monkeypatch):
    """The caller's timeout is the script's budget; the spawn gets slack on top.

    This ordering is the whole basis for telling the two failures apart. If the
    two deadlines were equal, the subprocess kill would race the watchdog and a
    hung script could still surface as an undiagnosed ``TimeoutExpired`` - and
    then get retried, which is exactly what must not happen to a real defect.
    """
    assert node_harness._SPAWN_SLACK_SECONDS > 0, "没有间隙就没有先后，分类失效"

    seen = {}

    def _fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        seen["argv"] = argv
        seen["env"] = kwargs.get("env") or {}
        seen["script"] = kwargs.get("input") or Path(argv[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(node_harness.subprocess, "run", _fake_run)
    run_node_script("node", "process.stdout.write('ok');", timeout=12)

    assert seen["timeout"] == 12 + node_harness._SPAWN_SLACK_SECONDS
    assert seen["env"][node_harness._DEADLINE_ENV] == "12000", (
        f"watchdog 必须按调用方的 timeout 武装，而不是别的值：{seen['env'].get(node_harness._DEADLINE_ENV)!r}"
    )
    assert "--require" in seen["argv"], seen["argv"]
    # The script itself must reach node exactly as the caller wrote it.
    assert seen["script"] == "process.stdout.write('ok');", seen["script"]


def test_a_caller_without_a_timeout_still_gets_a_finite_script_deadline(monkeypatch):
    """No ceiling at all used to mean "hang until the 25-minute job cap"."""
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(node_harness.subprocess, "run", _fake_run)
    run_node_script("node", "process.stdout.write('ok');")

    assert seen["timeout"] == (
        node_harness._DEFAULT_WATCHDOG_SECONDS + node_harness._SPAWN_SLACK_SECONDS
    ), "没给 timeout 的调用方以前连外层 ceiling 都没有，同步卡死能一路跑到 job cap"
    millis = int(node_harness._DEFAULT_WATCHDOG_SECONDS * 1000)
    assert seen["env"][node_harness._DEADLINE_ENV] == str(millis)


def test_a_spawn_that_stalls_once_is_retried():
    """A stall with the script never reached is the runner's fault, not ours.

    ``node.EXE`` has come back from a Windows runner having burned 30s without
    reaching the first line of a 55ms script. Nothing in the harness can make
    that attempt succeed, and no assertion was under test, so the run is
    repeated rather than reported as a contract violation.
    """
    calls = []
    ok = subprocess.CompletedProcess(["node"], 0, "ok", "")

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        return ok

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        result = run_node_script("node", "process.stdout.write('ok');", timeout=3)
    finally:
        node_harness.subprocess.run = original

    assert result is ok
    assert len(calls) == 2, "第一次 spawn 卡死后必须再试一次"
    assert calls[0][-1] != calls[1][-1], "重试要用新的临时脚本，别继承上一次被 kill 时的残留"


def test_a_spawn_that_keeps_stalling_reports_both_attempts():
    """The dual: retrying must not become a way to hide a reproducible stall.

    A stall only earns a retry when it was completely silent, so a two-attempt
    run is by construction two silent attempts - and the error has to say that
    twice over rather than collapsing it into one line.
    """
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"), output="", stderr="")

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            run_node_script("node", "process.stdout.write('ok');", timeout=3)
    finally:
        node_harness.subprocess.run = original

    assert len(calls) == 2, "重试次数必须有界"
    assert isinstance(excinfo.value, NodeHarnessSpawnTimeout)
    message = str(excinfo.value)
    assert "attempt 1:" in message and "attempt 2:" in message, (
        f"两次尝试都得各自列出来：{message}"
    )
    assert message.count("stdout=") == 2 and message.count("stderr=") == 2, (
        f"每次尝试各自吐了什么必须跟着错误一起报出来：{message}"
    )


# Three suites drive fake clocks by shadowing the timer globals at module scope.
# `const` makes that shadow cover the whole module, temporal dead zone included,
# so an appended guard reading the bare name gets the fake or throws outright.
_SHADOWS_THE_TIMERS = (
    "const setTimeout = (callback) => { callback(); return 0; };\n"
    "const clearTimeout = () => {};\n"
    "const setInterval = (callback) => { callback(); return 0; };\n"
    "setTimeout(function () {});\n"
    "process.stdout.write('ok');\n"
)


# The other door to the same problem: a harness that aliases window onto the
# global object (four files do) turns `window.setTimeout = fake` into an
# overwrite of the real global, which a `globalThis.setTimeout` watchdog would
# happily pick up.
_OVERWRITES_THE_GLOBAL_TIMER = (
    "globalThis.window = globalThis;\n"
    "window.setTimeout = (callback) => { callback(); return 0; };\n"
    "window.setInterval = (callback) => { callback(); return 0; };\n"
    "setTimeout(function () {});\n"
    "process.stdout.write('ok');\n"
)


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_overwrites_the_global_timer_still_runs_clean(runner):
    """`global.window = global` makes `window.setTimeout = fake` a real overwrite.

    Reading the timer off ``globalThis`` survives a module-scope ``const``
    shadow but not this, so the watchdog takes it from ``node:timers`` instead -
    behind both doors.
    """
    node_path = _node_or_skip()

    result = runner(
        node_path,
        _OVERWRITES_THE_GLOBAL_TIMER,
        capture_output=True,
        check=False,
        timeout=6,
    )

    assert result.returncode == 0, (
        f"被改写掉的全局 setTimeout 不能牵动 watchdog：stderr={result.stderr!r}"
    )
    assert result.stdout == "ok"
    assert result.stderr == ""


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_shadows_the_timers_still_runs_clean(runner):
    """The guard must not be steerable by the script it is guarding.

    Found the hard way: the first version called the bare ``setTimeout`` and
    ``tests/unit/test_avatar_annotation_frontend.py`` -- whose harness replaces
    it with ``(callback) => callback()`` -- ran the watchdog's own callback on
    the spot and died at exit 87 with nothing wrong with it.
    """
    node_path = _node_or_skip()

    result = runner(
        node_path, _SHADOWS_THE_TIMERS, capture_output=True, check=False, timeout=6
    )

    assert result.returncode == 0, (
        f"被 harness 影子化的 setTimeout 不能牵动 watchdog：stderr={result.stderr!r}"
    )
    assert result.stdout == "ok"
    assert result.stderr == ""


# A script the watchdog cannot save: a synchronous block never yields, so the
# timer it is queued behind can never run. What separates it from a node that
# never started is that anything written first still reaches the parent.
_BLOCKS_THE_EVENT_LOOP = (
    "process.stdout.write('started');\n"
    "while (true) {}\n"
)


def test_a_synchronously_blocked_script_is_reported_not_retried():
    """The one in-script hang the watchdog cannot catch must still not be retried.

    ``while (true) {}`` never yields, so the watchdog's own timer never runs and
    the stall reaches the outer ceiling looking exactly like a spawn stall. It
    is not one, and a second spawn cannot help. Measured on Windows: what the
    script wrote before blocking still arrives, so the output is the tell.
    """
    node_path = _node_or_skip()

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        # 1s, so the run costs one ceiling (1 + slack) rather than two.
        run_node_script(
            node_path, _BLOCKS_THE_EVENT_LOOP, capture_output=True, timeout=1
        )

    error = excinfo.value
    assert isinstance(error, NodeHarnessSpawnTimeout)
    assert len(error.attempts) == 1, (
        f"卡住但吐了东西的脚本证明它跑起来了，重试没有意义：{error.attempts}"
    )
    assert _as_text(error.attempts[0].stdout) == "started", (
        "同步卡死之前写出去的东西必须还能拿到——这是区分它和「node 没跑起来」的唯一证据"
    )
    assert "reached the harness script" in str(error), (
        f"报错不能一口咬定是 node 没跑起来：{error}"
    )
    assert "blocked the event loop or compiling it did" in str(error), (
        f"也不能反过来断言一定是同步阻塞——标记只证明到过脚本：{error}"
    )


def test_a_silent_stall_is_still_retried():
    """The dual: with no output there is nothing to conclude, so retry stands."""
    calls = []
    ok = subprocess.CompletedProcess(["node"], 0, "ok", "")

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(
                argv, kwargs.get("timeout"), output="", stderr=""
            )
        return ok

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        assert run_node_script("node", "process.stdout.write('ok');", timeout=3) is ok
    finally:
        node_harness.subprocess.run = original

    assert len(calls) == 2


def test_output_from_a_pre_main_preload_does_not_count_as_started():
    """Output proves nothing about the harness, in either direction.

    An inherited ``NODE_OPTIONS=--require`` module runs before the guard and
    before the script; if it prints and then stalls, the old output fallback
    read that as "the harness ran" and suppressed the retry -- for an attempt
    where the harness was never reached at all.
    """
    calls = []
    ok = subprocess.CompletedProcess(["node"], 0, "ok", "")

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            # Output, but no marker: exactly what a chatty pre-main preload
            # leaves behind.
            raise subprocess.TimeoutExpired(
                argv, kwargs.get("timeout"), output="from a preload", stderr="noise"
            )
        return ok

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        assert run_node_script("node", "process.stdout.write('ok');", timeout=3) is ok
    finally:
        node_harness.subprocess.run = original

    assert len(calls) == 2, (
        "预载在主脚本之前吐的东西不能算「脚本跑过了」——那次尝试该重试"
    )


def test_the_wrapped_error_reports_the_second_attempt_not_the_first():
    """With two attempts, ``.stdout`` must be the retry's, not the first try's.

    Reachable and distinguishing: attempt 1 stalls silently (so it earns the
    retry), attempt 2 stalls with output. Picking ``attempts[0]`` would hand the
    caller the empty one and hide the only evidence in the run.
    """
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"), output="", stderr="")
        raise subprocess.TimeoutExpired(
            argv, kwargs.get("timeout"), output="second-out", stderr="second-err"
        )

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            run_node_script(
                "node", "process.stdout.write('ok');", capture_output=True, timeout=1
            )
    finally:
        node_harness.subprocess.run = original

    assert len(calls) == 2
    assert excinfo.value.stdout == "second-out"
    assert excinfo.value.stderr == "second-err"
    # Both are still listed; only the exception's own fields follow the last one.
    # Count the per-attempt lines, not the word: the diagnosis prose says
    # "the attempt was repeated" and would inflate a bare substring count.
    listed = [l for l in str(excinfo.value).splitlines() if l.startswith("  attempt ")]
    assert len(listed) == 2, listed


def test_the_wrapped_error_keeps_a_stalled_attempt_output_where_callers_look():
    """``except TimeoutExpired`` reading ``.stdout`` must not regress to None.

    ``subprocess.run`` fills those in after killing the child; wrapping the
    error in a subclass and forgetting to pass them through would quietly take
    that away from every caller.
    """

    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, kwargs.get("timeout"), output="partial-out", stderr="partial-err"
        )

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            run_node_script("node", "process.stdout.write('ok');", timeout=1)
    finally:
        node_harness.subprocess.run = original

    assert excinfo.value.stdout == "partial-out"
    assert excinfo.value.output == "partial-out"
    assert excinfo.value.stderr == "partial-err"


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_pins_date_now_cannot_freeze_the_deadline(runner):
    """Three harnesses in this repo pin ``Date.now`` to a fake clock.

    ``test_app_websocket_static.py`` and ``test_avatar_annotation_frontend.py``
    both do it to drive TTL logic. Reading the clock through the global at check
    time froze ``overdue()`` for them -- the guard was inert for exactly the
    suites that simulate the passage of time. The preload takes a monotonic
    clock before the script gets a turn.
    """
    node_path = _node_or_skip()

    # Date.now pinned, then real time burned through a clock the fake cannot
    # reach. Under the old lookup this returned 0.
    script = (
        "Date.now = () => 1000000;\n"
        "const until = process.hrtime.bigint() + 500000000n;\n"
        "while (process.hrtime.bigint() < until) {}\n"
        "process.stdout.write('done');\n"
    )
    result = runner(node_path, script, capture_output=True, check=False, timeout=0.1)

    assert result.returncode == node_harness._WATCHDOG_EXIT_CODE, (
        f"被钉死的 Date.now 不该冻住 deadline：rc={result.returncode} "
        f"stdout={result.stdout!r}"
    )
    assert "still running" in result.stderr


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_stubs_process_exit_cannot_disarm_the_timer(runner):
    """``process.exit = () => {}`` is a normal stub, not sabotage.

    A harness testing code that exits on error will stub it. The preload
    resolved ``process.exit`` when the timer fired, so the stub swallowed the
    kill and the run hung to the outer ceiling; the binding is taken at preload
    time now, before the script can touch it.
    """
    node_path = _node_or_skip()

    script = (
        "process.exit = () => {};\n"
        "setInterval(function () {}, 1000);\n"
        "process.stdout.write('started');\n"
    )
    result = runner(node_path, script, capture_output=True, check=False, timeout=2)

    assert result.returncode == node_harness._WATCHDOG_EXIT_CODE, (
        f"被打桩的 process.exit 不该让 watchdog 失效：rc={result.returncode}"
    )


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_replaces_the_global_process_still_runs(runner):
    """A browser shim may do ``global.process = { env: {} }``.

    The preload is out of reach of the caller's lexical scope but not of the
    global object, so resolving ``process`` at fire time picked up the
    replacement and died in the exit hook with a TypeError -- turning a healthy
    script into a failure.
    """
    node_path = _node_or_skip()

    script = (
        "const native = process;\n"
        "global.process = { env: {} };\n"
        "native.stdout.write('ok');\n"
    )
    result = runner(node_path, script, capture_output=True, check=False, timeout=6)

    assert result.returncode == 0, (
        f"换掉全局 process 不该把健康脚本打红：stderr={result.stderr[:300]!r}"
    )
    assert result.stdout == "ok"


def test_a_zero_timeout_still_fails_fast_and_still_arms_the_guard(monkeypatch):
    """``timeout=0`` means fail now, not "five seconds and no guard".

    The slack is added only to a positive budget, and the deadline is floored at
    1ms so rounding cannot switch the preload off -- ``int(0 * 1000)`` did, and
    the preload's own ``deadlineMs > 0`` check then skipped everything.
    """
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(node_harness.subprocess, "run", _fake_run)
    run_node_script("node", "process.stdout.write('ok');", timeout=0)

    assert seen["timeout"] == 0, (
        f"timeout=0 的调用方要的是立刻失败，不是多给 5 秒：{seen['timeout']!r}"
    )
    assert seen["env"][node_harness._DEADLINE_ENV] == "1", (
        f"预算再小也必须武装护栏，不能被取整成 0 关掉：{seen['env'].get(node_harness._DEADLINE_ENV)!r}"
    )


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_another_preloads_dependency_does_not_start_the_budget(runner, tmp_path):
    """The clock must start at the harness script, not at the first compile.

    An inherited ``NODE_OPTIONS`` can add preloads of its own, and an ESM
    ``--import`` module is evaluated after CommonJS ``--require`` ones -- so a
    CommonJS dependency it pulls in compiles after the guard is installed and
    before the harness script. Keying on "first compile" armed the deadline
    there, putting pre-main startup back inside the script's budget.

    The witness is a preload slower than the whole budget: if its time is
    charged to the script, an otherwise instant script comes back 87.
    """
    node_path = _node_or_skip()

    # tmp_path, not the shared temp dir: fixed names there let one parametrised
    # instance delete the .mjs another is still loading.
    dependency = tmp_path / "neko-probe-dep.cjs"
    slow_import = tmp_path / "neko-probe-slow.mjs"
    dependency.write_text("module.exports = 1;\n", encoding="utf-8")
    slow_import.write_text(
        "import { createRequire } from 'node:module';\n"
        "const require = createRequire(import.meta.url);\n"
        f"require({str(dependency)!r}.split('\\\\').join('/'));\n"
        "const until = Date.now() + 800;\n"
        "while (Date.now() < until) {}\n",
        encoding="utf-8",
    )
    try:
        result = runner(
            node_path,
            "process.stdout.write('ok');\n",
            capture_output=True,
            check=False,
            timeout=0.5,
            env={**os.environ, "NODE_OPTIONS": f"--import {slow_import.as_uri()}"},
        )
    finally:
        dependency.unlink(missing_ok=True)
        slow_import.unlink(missing_ok=True)

    assert result.returncode == 0, (
        "别人的预载花掉的时间被算进了脚本预算："
        f"rc={result.returncode} stderr={result.stderr[:300]!r}"
    )
    assert result.stdout == "ok"


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_the_preload_marks_that_the_script_actually_started(runner):
    """The marker is the launcher's only direct evidence, so prove it appears.

    Everything downstream -- whether a stall gets retried, and which of the two
    stories the error tells -- hangs off this file existing.
    """
    node_path = _node_or_skip()
    seen = {}
    real_run = node_harness.subprocess.run

    def _watching_run(argv, **kwargs):
        result = real_run(argv, **kwargs)
        marker = (kwargs.get("env") or {})[node_harness._MARKER_ENV]
        seen["existed"] = os.path.exists(marker)
        return result

    node_harness.subprocess.run = _watching_run
    try:
        result = runner(
            node_path, "process.stdout.write('ok');\n",
            capture_output=True, check=False, timeout=6,
        )
    finally:
        node_harness.subprocess.run = real_run

    assert result.returncode == 0
    assert seen["existed"] is True, "脚本明明跑了，标记却没落下来"


def test_a_stall_before_the_script_started_is_retried():
    """No marker means nothing was under test, so the attempt is repeated.

    This is the whole point of the retry: a spawn that never reached the script
    has run no assertions, and no ceiling the caller picks can help it.
    """
    calls = []
    ok = subprocess.CompletedProcess(["node"], 0, "ok", "")

    def _fake_run(argv, **kwargs):
        calls.append(kwargs.get("env", {}).get(node_harness._MARKER_ENV))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"), output="", stderr="")
        return ok

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        assert run_node_script("node", "process.stdout.write('ok');", timeout=3) is ok
    finally:
        node_harness.subprocess.run = original

    assert len(calls) == 2
    assert calls[0] != calls[1], "每次尝试要有自己的标记，否则上一次的会误导下一次"


def test_a_stall_after_the_script_started_is_not_retried_even_with_no_output():
    """The fix for the case output could never have answered.

    A caller that does not capture leaves ``TimeoutExpired.stdout`` as None, so
    the old output test read "silent" and retried -- repeating whatever the
    script had already done. The marker answers it directly: the script ran, so
    a second run would block in the same place.
    """
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        # Stand in for the preload: node reached the script and marked it.
        Path(kwargs["env"][node_harness._MARKER_ENV]).write_text("1", encoding="utf-8")
        # No capture, so both streams come back as None -- indistinguishable
        # from silence without the marker.
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            run_node_script("node", "process.stdout.write('ok');", timeout=1)
    finally:
        node_harness.subprocess.run = original

    assert len(calls) == 1, (
        f"脚本已经跑过了就不该重试——重跑一遍只会在同一个地方卡住，"
        f"顺带把它已经做过的事再做一遍：{len(calls)} 次"
    )
    assert excinfo.value.started is True
    message = str(excinfo.value)
    # The marker is written before the module is compiled *and run*, so a stall
    # in compilation reaches here too. The message must not claim to know which.
    assert "blocked the event loop or compiling it did" in message, message
    assert "reached the harness script" in message


def test_a_started_stall_does_not_claim_to_know_which_kind_it_was():
    """The marker proves node reached the script, and nothing finer.

    It is written before ``_compile``, which compiles *and* runs the module --
    deliberately, because writing it after would leave a synchronously blocked
    script unmarked and get it retried. The price is that a stall in
    compilation lands here too, so the message offers both readings instead of
    asserting the one it cannot distinguish.
    """
    def _fake_run(argv, **kwargs):
        Path(kwargs["env"][node_harness._MARKER_ENV]).write_text("1", encoding="utf-8")
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"), output="", stderr="")

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            run_node_script(
                "node", "process.stdout.write('ok');", capture_output=True, timeout=1
            )
    finally:
        node_harness.subprocess.run = original

    message = str(excinfo.value)
    # The whole phrase: asserting only the tail would still pass on a message
    # that offered compilation alone, which is the opposite of what this pins.
    assert "blocked the event loop or compiling it did" in message, message
    assert "blocked the event loop synchronously." not in message, (
        f"这句话断言了我们分辨不出来的事：{message}"
    )


def test_the_error_says_which_of_the_two_stalls_it_was():
    """The dual: a never-started stall must not be described as a script hang."""
    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"), output="", stderr="")

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        with pytest.raises(subprocess.TimeoutExpired) as excinfo:
            run_node_script("node", "process.stdout.write('ok');", capture_output=True, timeout=1)
    finally:
        node_harness.subprocess.run = original

    assert excinfo.value.started is False
    message = str(excinfo.value)
    assert "never reached the harness script" in message
    assert "blocked the event loop synchronously" not in message


def test_the_marker_is_cleaned_up_after_a_run():
    """One marker per attempt, and none of them outlive the call."""
    seen = []

    def _fake_run(argv, **kwargs):
        marker = kwargs["env"][node_harness._MARKER_ENV]
        Path(marker).write_text("1", encoding="utf-8")
        seen.append(marker)
        return subprocess.CompletedProcess(argv, 0, "", "")

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        run_node_script("node", "process.stdout.write('ok');", timeout=3)
    finally:
        node_harness.subprocess.run = original

    assert len(seen) == 1
    assert not os.path.exists(seen[0]), f"标记文件留在盘上了：{seen[0]}"


def test_the_script_budget_is_counted_from_the_preload_not_process_start():
    """Bootstrap time belongs to the spawn stall, not to the script.

    Structural, because a slow V8 bootstrap cannot be induced on demand -- but
    the distinction matters more than most: ``process.uptime()`` includes the
    pre-script delay this launcher exists to tell apart, so a healthy script on
    a slow spawn would be reported as a harness timeout *instead of* retried.
    """
    preload = node_harness._PRELOAD_SOURCE

    assert "let startedAt = null;" in preload
    assert "startedAt = millisNow();" in preload
    assert "startedAt !== null && millisNow() - startedAt > deadlineMs" in preload
    assert "Module.prototype._compile" in preload, (
        "预算要从「node 编译脚本」那一刻起算，靠的就是这个钩子"
    )
    assert "uptime()" not in preload, (
        "uptime() 从进程启动算起，会把 node 自己的 bootstrap 记到脚本头上"
    )


def test_the_child_keeps_the_environment_it_would_have_inherited(monkeypatch):
    """The deadline is *added* to the environment, not substituted for it.

    Found by mutation: replacing the inherited environment with a bare dict
    survived every test. A harness that reads an env var -- or anything relying
    on PATH, NODE_OPTIONS, or the locale -- would start failing for reasons with
    no connection to what it tests.
    """
    monkeypatch.setenv("NEKO_HARNESS_ENV_WITNESS", "kept")
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(node_harness.subprocess, "run", _fake_run)
    run_node_script("node", "process.stdout.write('ok');", timeout=5)

    assert seen["env"].get("NEKO_HARNESS_ENV_WITNESS") == "kept", (
        "继承来的环境被整个换掉了，调用方依赖的 env 会莫名其妙消失"
    )
    assert seen["env"][node_harness._DEADLINE_ENV] == "5000"


@pytest.mark.parametrize("runner", [run_node_script, run_node_stdin])
def test_a_harness_that_stubs_fs_writesync_still_gets_the_diagnosis(runner):
    """``require('node:fs')`` hands the script the preload's own module object.

    Found by mutation: reading ``fs.writeSync`` at report time instead of
    binding it up front survived, because the surviving tests only checked the
    exit code -- which comes from a different snapshot. The diagnosis is the
    part that tells the next person what leaked, so it is the part worth
    pinning.
    """
    node_path = _node_or_skip()

    script = (
        "require('node:fs').writeSync = () => {};\n"
        "setInterval(function () {}, 1000);\n"
        "process.stdout.write('started');\n"
    )
    result = runner(node_path, script, capture_output=True, check=False, timeout=2)

    assert result.returncode == node_harness._WATCHDOG_EXIT_CODE
    assert "[node_harness]" in result.stderr, (
        f"被打桩的 fs.writeSync 把诊断吞掉了：stderr={result.stderr!r}"
    )
    assert "still had pending work" in result.stderr


def test_the_guard_never_edits_the_script_it_is_guarding():
    """The script reaches node byte-for-byte as the caller wrote it.

    Every splicing bug this launcher has had -- a demoted ``'use strict'``, a
    broken hashbang, renumbered stack frames, a guard reading an identifier the
    harness had shadowed -- came from injecting text into the caller's source.
    The guard is a preloaded module now, so the source is not touched at all.
    """
    seen = {}
    awkward = (
        "#!/usr/bin/env node\n"
        "'use strict'; // 说明\n"
        "const setTimeout = (cb) => cb();\n"
        "const globalThis = {};\n"
        "process.stdout.write('ok');\n"
    )

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["script"] = Path(argv[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    original = node_harness.subprocess.run
    node_harness.subprocess.run = _fake_run
    try:
        run_node_script("node", awkward, timeout=5)
    finally:
        node_harness.subprocess.run = original

    assert seen["script"] == awkward, "脚本被改写了，注入式护栏的所有坑就都回来了"
    assert seen["argv"][1] == "--require", seen["argv"]
    assert seen["argv"][2].endswith(".neko-harness-guard.cjs"), seen["argv"]


def test_one_preload_file_serves_the_whole_process():
    """The deadline rides in the environment, so the file never varies.

    Writing one per invocation would put a fresh, never-before-seen ``.js`` into
    the temp directory on every single harness call -- more of exactly the file
    churn this launcher is trying to stop paying for.
    """
    first = node_harness._preload_path()
    second = node_harness._preload_path()

    assert first == second
    assert Path(first).read_text(encoding="utf-8") == node_harness._rendered_preload()


def test_the_exit_code_has_exactly_one_definition():
    """The guard's exit code and the tests' expectation cannot drift apart.

    The preload carried its own literal 87 for a while, which left
    ``_WATCHDOG_EXIT_CODE`` unreferenced in the module and two places to change.
    """
    rendered = node_harness._rendered_preload()
    declaration = f"const EXIT_CODE = {node_harness._WATCHDOG_EXIT_CODE};"

    # count == 1, not `in`: _rendered_preload() replaces every placeholder, so a
    # duplicated one would emit two declarations and `in` would still pass.
    assert rendered.count(declaration) == 1, rendered.count(declaration)
    assert node_harness._PRELOAD_SOURCE.count('__EXIT_CODE__') == 1
    assert '__EXIT_CODE__' not in rendered
    assert "= 87;" not in node_harness._PRELOAD_SOURCE, (
        "预载不该再自带一个 87 的字面量，否则和 Python 常量会各改各的"
    )
