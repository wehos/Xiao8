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
"""Shared launcher for the generated node simulation harnesses.

Several static-contract suites drive real frontend modules through a node
script built at test time.  Two ways of handing that script to node have both
bitten this repo, and both failures look like anything but what they are:

Command-line length
    ``node -e <script>`` puts the whole script on the command line.  Past 32767
    characters Windows' ``CreateProcess`` refuses it and ``subprocess`` raises
    ``WinError 206`` before node starts, so not one assertion runs.  A suite
    crossed that line at 34067 characters and stayed red unnoticed.

Locale encoding
    ``subprocess.run(..., text=True)`` without an explicit ``encoding`` encodes
    stdin and decodes stdout with ``locale.getpreferredencoding()``.  On a
    machine with the Windows UTF-8 option enabled that is cp65001 and CJK in a
    harness script sails through; on a stock English Windows (every GitHub
    runner) it is cp1252 and the same script dies with ``UnicodeEncodeError``.
    Five tests passed locally and failed in CI on exactly this.

Both runners here take the script off the command line and pin UTF-8 in both
directions.  Node lookup and the node-missing policy (skip vs. hard failure)
stay with each caller, since the suites deliberately differ there.

A third failure mode showed up once the suite ran under ``pytest -n auto`` on
the Windows runner: ``subprocess.TimeoutExpired`` with nothing else to go on.
A bare ceiling around ``subprocess.run`` cannot say which of two very different
things happened, and the two want opposite responses:

The script never settled
    A ``setInterval`` the harness forgot to clear, or an ``await`` on a promise
    nothing resolves, keeps node's event loop alive forever.  That is a real
    defect in the harness and must stay red however often it is retried.

The node process never got going
    Process creation, the read of the script off disk, or V8 bootstrap stalled
    on the runner.  Nothing in the script is reached, so no ceiling the caller
    picks can help and no assertion is being tested; retrying is the only
    sensible answer.

They are told apart by giving the script its own deadline inside node, carried
by a module preloaded with ``node --require``.  It arms two things.  A timer,
``unref()``-ed so it is never itself the reason node stays up, fires only while
something else still holds the event loop open, prints what that something is,
and exits 87 -- so a harness that leaked a handle fails deterministically, with
the leak named, and is never retried.  An ``exit`` hook re-checks the budget on
the way out, covering the script that merely ran long and then ended and the one
that calls ``process.exit()`` itself; every way out of node passes through it,
and an ``exitCode`` assigned there overrides even ``process.exit(0)``.  The
subprocess ceiling sits a few seconds above the deadline, so a surviving
``TimeoutExpired`` means node never got that far.  The script's budget starts
when node compiles the harness script -- not at process start, and not when the
preload runs, since a ``--require`` module is loaded before node reads the main
script at all.  Bootstrap, reading the temp file and draining ``node -`` stdin
are therefore outside it: they are the stall the ceiling is there to catch and
retry, and charging them to the script would report a slow *spawn* as a script
timeout, instead of retrying it.

The guard is a preload rather than text spliced into the script, because a
harness script is not a safe place to stand.  Injected code resolves its names
in the caller's module scope, and these harnesses routinely install fake clocks
and browser shims: ``const setTimeout = cb => cb()`` shadows the bare name for
the whole module (temporal dead zone included, so merely reading it throws),
``global.window = global`` turns ``window.setTimeout = fake`` into an overwrite
of the real one, and the same goes for ``globalThis``, ``Function``, ``process``
and every other identifier a guard might reach for.  Splicing also has to dodge
the script's own text: a hashbang counts only at the very start of the source, a
``'use strict'`` directive stops being a directive the moment any statement
precedes it, and anything taking a line of its own renumbers every stack frame
below.  A preloaded module has none of these problems -- it runs before the
script, in a scope the script cannot reach, and leaves the script byte-for-byte
as the caller wrote it.

One boundary is worth naming: the deadline is armed from a CommonJS compile
hook, so a harness evaluated as an ES module would never arm it and would be
left with only the outer ceiling.  Every harness here is CommonJS, and the only
way to change that from outside -- an inherited
NODE_OPTIONS=--input-type=module -- makes all 26 of them fail immediately on
their first require, so the guard cannot go quietly blind while the suites
still pass.  Adding an ES-module harness would need this revisited.

One in-script hang escapes the watchdog: a synchronous block.  ``while (true)
{}`` never yields, and a timer cannot interrupt the thread it is queued on --
no amount of retrying will make that script finish.  What separates it from a
genuine spawn stall is evidence: measured on Windows, whatever the script wrote
before it blocked still reaches the parent, while a node that never reached the
script emits nothing at all.  Which of the two happened is never inferred from output.  Output
answers a different question in both directions: a caller that does not capture
can never show any, and an inherited ``NODE_OPTIONS`` preload that prints before
stalling shows some without the harness being reached at all.  The preload drops
a marker file the moment node compiles the script, and that file is the whole
signal: an attempt that never got that far is repeated, and one that did is
reported as it stands, because a second run would stall in the same place.
"""

import atexit
import os
import subprocess
import tempfile
import threading


# Script deadline for callers that pass no ``timeout`` of their own.  Their
# ceiling today is the 25-minute job cap, so anything finite is an improvement;
# the slowest harness in the suite measures 5.1s.
_DEFAULT_WATCHDOG_SECONDS = 120.0
# Head-room between the script's own deadline and the subprocess ceiling.  The
# gap is what the watchdog needs to fire, write its diagnosis and exit, and it
# is the only thing that makes a surviving ``TimeoutExpired`` mean "node never
# ran the script".  Callers keep exactly the script budget they asked for.
_SPAWN_SLACK_SECONDS = 5.0
# Distinctive exit code so a watchdog kill is never mistaken for an assertion
# failure or an uncaught exception, both of which leave node with 1.
_WATCHDOG_EXIT_CODE = 87



def _excerpt(blob, limit: int = 400) -> str:
    """Readable, bounded view of whatever a stalled attempt had emitted."""
    if blob is None:
        return "<none>"
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "replace")
    if len(blob) > limit:
        blob = blob[:limit] + "..."
    return repr(blob)


class NodeHarnessSpawnTimeout(subprocess.TimeoutExpired):
    """The run hit the ceiling without node exiting.

    Subclasses ``TimeoutExpired`` so existing ``except`` clauses keep working,
    and carries what each attempt managed to emit.  One attempt means the stall
    came with output and was not worth repeating; two means both were silent.
    """

    def __init__(self, cmd, timeout, attempts, started=False):
        # Carry the last attempt's output on the exception the way
        # ``subprocess.run`` would have: a caller that catches
        # ``TimeoutExpired`` and reads ``.stdout``/``.stderr`` (or ``.output``)
        # must not get None just because the launcher wrapped the error.
        last = attempts[-1] if attempts else None
        super().__init__(
            cmd,
            timeout,
            output=getattr(last, "stdout", None),
            stderr=getattr(last, "stderr", None),
        )
        self.attempts = list(attempts)
        self.started = started

    def __str__(self) -> str:
        if self.started:
            diagnosis = (
                "node reached the harness script and then stalled without "
                "the watchdog firing. Either the script's top level blocked "
                "the event loop or compiling it did -- a timer cannot "
                "interrupt either, and from out here they look the same. Not "
                "retried: a second run stalls the same way, and repeating a "
                "script that may already have run is the worse mistake."
            )
        else:
            diagnosis = (
                "node never reached the harness script -- process creation, "
                "reading it off disk, or V8 bootstrap stalled. Nothing was "
                "under test, so the attempt was repeated."
            )
        lines = [super().__str__(), "", diagnosis]
        for index, attempt in enumerate(self.attempts, 1):
            lines.append(
                f"  attempt {index}: stdout={_excerpt(attempt.stdout)} "
                f"stderr={_excerpt(attempt.stderr)}"
            )
        return "\n".join(lines)


def _utf8(kwargs: dict) -> dict:
    """Force UTF-8 for stdin/stdout so the host locale cannot decide."""
    merged = dict(kwargs)
    merged.setdefault("text", True)
    merged["encoding"] = "utf-8"
    return merged


def _budgeted(kwargs: dict) -> tuple[dict, float]:
    """Split the caller's ceiling into a script deadline and a spawn ceiling.

    The caller's ``timeout`` stays the budget the *script* gets; the ceiling
    handed to ``subprocess.run`` is raised by the slack, so a script that
    overruns is killed from inside node with a diagnosis rather than from
    outside with none.  A caller that passes no timeout gets the default
    deadline and a ceiling to match: it used to get neither, which left a
    synchronously blocked script running until the job cap.
    """
    merged = dict(kwargs)
    timeout = merged.get("timeout")
    watchdog = _DEFAULT_WATCHDOG_SECONDS if timeout is None else float(timeout)
    if watchdog > 0:
        merged["timeout"] = watchdog + _SPAWN_SLACK_SECONDS
    # A zero or negative timeout is a caller asking to fail immediately.  Adding
    # slack to it would hand a slow script five seconds of grace it never had.
    return merged, watchdog


# How the preload receives this call's deadline.  An environment variable rather
# than a substitution into the source keeps the preload file identical for every
# call, so it can be written once per process instead of once per invocation.
_DEADLINE_ENV = "NEKO_NODE_HARNESS_DEADLINE_MS"
# Where the preload records that node reached the harness script.  Retrying is
# only ever right for an attempt that never got that far, and output is a poor
# proxy for it: a caller that does not capture cannot show any, so "silent" and
# "never started" look identical from out here.  The file makes it a fact.
_MARKER_ENV = "NEKO_NODE_HARNESS_STARTED_MARKER"

_PRELOAD_SOURCE = r"""
// Preloaded with `node --require`, so this runs before the harness script, in
// its own module scope, out of reach of anything the harness declares.
const timers = require('node:timers');
const fs = require('node:fs');
const Module = require('node:module');

// Snapshot every moving part before the script gets a turn.  A preload is out
// of reach of the caller's *lexical* scope, but not of shared objects: these
// harnesses stub `process.exit` (a normal thing to do when the code under test
// exits on error), hang browser shims off `global`, and `require('node:fs')`
// hands the script the very same module object this one is holding.  A binding
// taken here cannot be reached afterwards, which closes that class rather than
// moving it one name further along.
const nativeProcess = process;
const armTimer = timers.setTimeout.bind(timers);
const writeSync = fs.writeSync.bind(fs);
const writeFileSync = fs.writeFileSync.bind(fs);
const exitNow = nativeProcess.exit.bind(nativeProcess);
const stringify = JSON.stringify.bind(JSON);
// Monotonic, and snapshotted like everything else.  Three harnesses in this
// repo pin Date.now to a fake clock (test_app_websocket_static,
// test_avatar_annotation_frontend x2), which would freeze the deadline check
// for exactly the scripts most likely to need it; hrtime is also immune to the
// wall clock being adjusted underneath a long run.
const hrtime = nativeProcess.hrtime && typeof nativeProcess.hrtime.bigint === 'function'
  ? nativeProcess.hrtime.bigint.bind(nativeProcess.hrtime)
  : null;
const wallClock = Date.now.bind(Date);
const millisNow = hrtime ? function () { return Number(hrtime() / 1000000n); } : wallClock;
const activeResources = typeof nativeProcess.getActiveResourcesInfo === 'function'
  ? nativeProcess.getActiveResourcesInfo.bind(nativeProcess)
  : null;

const deadlineMs = Number(nativeProcess.env.NEKO_NODE_HARNESS_DEADLINE_MS || 0);
const EXIT_CODE = __EXIT_CODE__;

// The budget starts when node compiles the harness script, not when this
// preload runs and not at process start.  A `--require` module is loaded before
// node reads and compiles the main script, so everything up to that point --
// bootstrap, reading the temp file, draining `node -` stdin -- is pre-script
// work.  That is precisely the stall the outer ceiling exists to catch and
// retry; charging it to the script would report a slow *spawn* as a script
// timeout, and would do it *instead of* retrying, which is the one outcome this
// launcher was written to avoid.
//
// `Module.prototype._compile` is the marker: it fires for a file main module
// and for `node -` alike (`[stdin]-wrapper`), before the script's first
// statement runs.  The patch removes itself on the first call, so only the main
// module is measured and anything the script requires later is untouched.
let startedAt = null;

function overdue() {
  // Never started == never the script's fault.  Staying quiet here leaves the
  // outer ceiling to time out, which is what gets the attempt retried.
  return startedAt !== null && millisNow() - startedAt > deadlineMs;
}

function diagnose(prefix) {
  let held;
  try {
    held = activeResources
      ? stringify(activeResources())
      : '<getActiveResourcesInfo unavailable on this node>';
  } catch (err) {
    held = '<unavailable: ' + err + '>';
  }
  try {
    // writeSync, because process.exit() does not flush a pending async pipe
    // write and stderr to a pipe is async on Windows -- the diagnosis is the
    // whole point, so it must not be the part that gets dropped.
    writeSync(
      2,
      '\n[node_harness] ' + prefix + ' ' + (deadlineMs / 1000) + 's after the '
      + 'script started.\n'
      + '[node_harness] event loop is held by: ' + held + '\n'
      + '[node_harness] a harness that never settles has usually left a timer '
      + 'armed (clearInterval/clearTimeout) or is awaiting a promise that '
      + 'nothing resolves.\n'
    );
  } catch (err) {
    // Nothing left to report with; the exit code still carries the verdict.
  }
}

if (deadlineMs > 0) {
  // Covers every way out of node: the loop draining, an explicit process.exit()
  // in the script, an uncaught throw.  An exitCode assigned in an 'exit'
  // listener overrides even process.exit(0).
  nativeProcess.on('exit', function () {
    if (overdue()) {
      diagnose('the script was still running');
      nativeProcess.exitCode = EXIT_CODE;
    }
  });

  // Which compile is the harness script?  Not simply the first one: an
  // inherited NODE_OPTIONS can add its own preloads, and an ESM `--import`
  // module is evaluated after CommonJS `--require` ones, so a CommonJS
  // dependency it pulls in would compile after this hook is installed and
  // before the harness script.  Arming there would put pre-main startup back
  // inside the script's budget -- the very thing keying off compile was meant
  // to take out of it.
  //
  // The main module identifies itself: `process.mainModule` is set once node
  // starts loading it, so a preload's dependency is never `=== mainModule`.
  // `node -` is the exception -- it has no main module and compiles as
  // `[stdin]-wrapper` -- so that shape is matched explicitly, and only while
  // there is no main module to compare against.
  function isHarnessScript(module, filename) {
    if (nativeProcess.mainModule) return module === nativeProcess.mainModule;
    return String(filename).slice(0, 7) === '[stdin]';
  }

  const originalCompile = Module.prototype._compile;
  Module.prototype._compile = function (content, filename) {
    if (startedAt === null && isHarnessScript(this, filename)) {
      startedAt = millisNow();
      Module.prototype._compile = originalCompile;

      // Tell the launcher node reached the script.  This is written before
      // `originalCompile` rather than after, and the difference matters:
      // `_compile` both compiles the module *and* runs it, so "after" would
      // mean "after the whole top level", and a script that blocks the event
      // loop would never mark at all -- it would look like a spawn stall and
      // be retried, which is the one thing that must not happen to a script
      // that may already have had effects.  The cost is that a stall in
      // compilation itself also counts as reached; the error message says so
      // rather than claiming to know which it was.
      //
      // Best effort: if this cannot be written the launcher falls back to
      // judging by output, which is where it was before.
      try {
        const marker = nativeProcess.env.NEKO_NODE_HARNESS_STARTED_MARKER;
        if (marker) writeFileSync(marker, '1');
      } catch (err) {
        // Nothing to do; the deadline itself is unaffected.
      }

      // ...and this covers the script that never exits at all.  unref() so the
      // watchdog is never itself the reason node stays up: with nothing else
      // holding the loop, node exits first and the hook above does the
      // checking.
      const deadline = armTimer(function () {
        diagnose('the script still had pending work');
        exitNow(EXIT_CODE);
      }, deadlineMs);
      if (deadline && typeof deadline.unref === 'function') deadline.unref();
    }
    return originalCompile.call(this, content, filename);
  };
}
"""

def _rendered_preload() -> str:
    """The preload with the exit code bound to the Python constant.

    The substitution is the only one, and it does not vary per call, so the file
    written from this is still identical for every invocation.  It exists so
    that 87 has one definition: the JS carried its own literal for a while,
    which is a drift waiting to happen between the guard and every test that
    asserts on the code it exits with.
    """
    rendered = _PRELOAD_SOURCE.replace("__EXIT_CODE__", str(_WATCHDOG_EXIT_CODE))
    assert "__EXIT_CODE__" not in rendered
    return rendered


_preload_guard = threading.Lock()
_preload_file: str | None = None


def _drop_preload() -> None:
    """Remove the preload file at interpreter exit; losing it is not an error."""
    global _preload_file
    if _preload_file:
        try:
            os.unlink(_preload_file)
        except OSError:
            # Deliberate: this runs at interpreter exit, where a leaked temp
            # file is not worth raising over and there is nobody left to tell.
            pass
        _preload_file = None


def _preload_path() -> str:
    """Path to this process's preload module, written on first use.

    The contents never vary -- the deadline arrives by environment variable --
    so one file serves every call in the process rather than one per invocation.
    Under ``pytest -n auto`` each worker is its own process and gets its own.

    ``.cjs``, not ``.js``: the guard is CommonJS, and a ``.js`` file inherits
    its module type from the nearest ``package.json``.  Should the system temp
    directory ever sit inside a package declaring ``"type": "module"``, every
    call would die on the guard's first ``require``.  The suffix pins the type
    regardless of where the file lands.
    """
    global _preload_file
    with _preload_guard:
        if _preload_file is None or not os.path.exists(_preload_file):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".neko-harness-guard.cjs", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(_rendered_preload())
            _preload_file = handle.name
            atexit.register(_drop_preload)
        return _preload_file


def _new_marker() -> str:
    """A path the preload will create once node reaches the harness script."""
    handle, path = tempfile.mkstemp(suffix=".neko-harness-started")
    os.close(handle)
    os.unlink(path)  # its *existence* is the signal, so start from absent
    return path


def _script_started(marker: str) -> bool:
    """Did this attempt get as far as the harness script?

    The marker and nothing else.  Output used to serve as a fallback, and it was
    wrong in both directions: a caller that does not capture can never show any,
    and an inherited ``NODE_OPTIONS`` preload that prints before stalling would
    show some without the harness ever being reached.  The marker is written by
    the guard itself, at the one moment that actually answers the question.
    """
    return os.path.exists(marker)


def _guarded(merged: dict, deadline_seconds: float, marker: str) -> tuple[list[str], dict]:
    """The ``--require`` argument and the environment carrying the deadline."""
    base = merged.get("env")
    environment = dict(os.environ if base is None else base)
    environment[_MARKER_ENV] = marker
    # Floor at 1ms: a sub-millisecond budget must still arm the guard, and
    # rounding it to 0 is how the preload gets switched off by accident.
    environment[_DEADLINE_ENV] = str(max(1, int(deadline_seconds * 1000)))
    return ["--require", _preload_path()], dict(merged, env=environment)


def _run_retrying_spawn_stalls(next_attempt, cmd_for_error):
    """Run once, and once more if node stalled without ever reaching the script.

    ``next_attempt`` is called per attempt so a caller that stages a temp file
    gets a fresh one, rather than a second attempt inheriting whatever state
    the killed first attempt left behind.
    """
    attempts = []
    for attempt in (1, 2):
        argv, run_kwargs, marker = next_attempt()
        try:
            return subprocess.run(argv, **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            attempts.append(exc)
            started = _script_started(marker)
            if attempt == 2 or started:
                raise NodeHarnessSpawnTimeout(
                    cmd_for_error, exc.timeout, attempts, started=started
                ) from exc
        finally:
            try:
                os.unlink(marker)
            except OSError:
                # Absent is the normal case: the preload only creates it once
                # node reaches the script, and there is nothing to clean up
                # otherwise.
                pass
    raise AssertionError("unreachable")  # pragma: no cover


def run_node_script(node_path: str, script: str, **kwargs) -> subprocess.CompletedProcess[str]:
    """Run ``script`` from a temp file under ``node_path``.

    Use this when the script is large or grows with the behaviour it simulates.
    Extra keyword arguments go straight to ``subprocess.run``.
    """
    budget, deadline_seconds = _budgeted(_utf8(kwargs))
    staged: list[str] = []

    def _attempt():
        marker = _new_marker()
        preload, merged = _guarded(budget, deadline_seconds, marker)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(script)
            staged.append(handle.name)
        return [node_path, *preload, staged[-1]], merged, marker

    try:
        return _run_retrying_spawn_stalls(_attempt, [node_path, "<temp script>"])
    finally:
        for path in staged:
            try:
                os.unlink(path)
            except OSError:
                # Best effort on purpose: a killed node on Windows can still
                # hold the file for a moment, and a leaked temp file must not
                # replace the real error with a cleanup one.
                pass


def run_node_stdin(node_path: str, script: str, **kwargs) -> subprocess.CompletedProcess[str]:
    """Pipe ``script`` into ``node -`` over stdin.

    Equivalent to ``run_node_script`` for callers already written against the
    stdin form; stdin has no length ceiling, so only the encoding pin matters
    here. Extra keyword arguments go straight to ``subprocess.run``.
    """
    budget, deadline_seconds = _budgeted(_utf8(kwargs))

    def _attempt():
        marker = _new_marker()
        preload, merged = _guarded(budget, deadline_seconds, marker)
        return [node_path, *preload, "-"], dict(merged, input=script), marker

    return _run_retrying_spawn_stalls(_attempt, [node_path, "-"])
