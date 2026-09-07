#!/usr/bin/env python3
"""Static check (PR-only): require change reports in the PR description.

Two project policies, both enforced against the pull request body. The
sections are located by their Chinese heading keywords, defined as the
``REGRESSION_KEYWORD`` / ``NO_SPLIT_KEYWORD`` constants below.

REGRESSION_REPORT
    If the PR touches app/, main_logic/, main_routers/, or memory/ (any ``*.py`` file),
    the body must carry a non-empty regression-report section documenting
    the change, its rationale / necessity, before-and-after behaviour, and
    the potential regressions. These three are the project's highest-risk
    modules (session orchestration, the memory pipeline, the server entry
    points), so every code change to them must come with a written report.

NO_SPLIT_RATIONALE
    If the PR changes more than 20 counted files, the body must carry a
    non-empty no-split-rationale section explaining why it is not split into
    smaller PRs. New files, i18n locale files, and test files (anywhere —
    under main or under plugin/) do not count toward this limit: a big PR
    that is mostly new surface area, locale fan-out, or test coverage is
    exactly the kind of PR we do NOT want to discourage from staying in one
    piece.

The check only verifies that a substantive section EXISTS — it cannot judge
whether the report is any good. Report quality is the reviewer's job; the
companion .github/CODEOWNERS routes these paths to a maintainer so a human
gate sits behind this machine gate.

Escape hatch
------------
Apply the ``report-exempt`` label to the PR to skip both rules (pure
renames, bulk reformatting, generated code, etc.). The label set is read
from the ``PR_LABELS`` env var (comma-separated).

Inputs (all from env, set by the workflow):
    PR_BODY    — the pull request description (markdown)
    PR_LABELS  — comma-separated label names
Changed files come from ``git diff --name-status <base>...HEAD``.

Exit 1 on any violation, 0 otherwise (2 on git failure).

Usage:
    python scripts/check_pr_report.py [--base origin/main]
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Modules whose every *.py change must ship a regression report.
WATCHED_PREFIXES = ("app/", "main_logic/", "main_routers/", "memory/")
# A PR touching more than this many counted files must justify not splitting.
FILE_COUNT_LIMIT = 20

# Path segments that mark a directory as holding tests (any depth, main or
# plugin/). A file under one of these never counts toward FILE_COUNT_LIMIT.
_TEST_DIR_PARTS = {"tests", "test", "__tests__"}
# Test-file basenames not living in a test dir: pytest's test_*.py / *_test.py
# and the JS/TS *.test.* / *.spec.* conventions used across the frontends.
_TEST_FILE_RE = re.compile(
    r"(^test_.+\.py$|.+_test\.py$|.+\.(test|spec)\.[cm]?[jt]sx?$)"
)
# Locale files move in lockstep by design; a single user-facing string can
# touch every supported locale, so they do not count toward FILE_COUNT_LIMIT.
_I18N_LOCALE_GROUPS = {
    "static/locales": {
        "en.json", "es.json", "ja.json", "ko.json",
        "pt.json", "ru.json", "zh-CN.json", "zh-TW.json",
    },
    "frontend/plugin-manager/src/i18n/locales": {
        "en-US.ts", "es.ts", "ja.ts", "ko.ts",
        "pt.ts", "ru.ts", "zh-CN.ts", "zh-TW.ts",
    },
}
# Maintainer escape hatch.
EXEMPT_LABEL = "report-exempt"

REGRESSION_REPORT = "REGRESSION_REPORT"
NO_SPLIT_RATIONALE = "NO_SPLIT_RATIONALE"

# Heading keywords the PR template uses for the two required sections; the
# section body is the text under the first heading containing the keyword.
REGRESSION_KEYWORD = "回归报告"
NO_SPLIT_KEYWORD = "不拆分理由"

# Section bodies equal to one of these (after stripping) count as "not filled":
# the author left a placeholder instead of writing the report.
_PLACEHOLDERS = {
    "", "不适用", "无", "暂无", "n/a", "na", "none", "-", "/", "tbd", "todo",
}

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADER_RE = re.compile(r"^(#{1,6})\s")
# Markdown emphasis / code chars stripped before the placeholder test, so a
# decorated placeholder is recognised too.
_EMPHASIS_RE = re.compile(r"[*_`]")
# Separator / punctuation chars stripped from each whitespace-split token, so
# a combined or punctuated placeholder (e.g. "不适用 / N/A", "不适用。", "N/A.")
# collapses to all-placeholder tokens while an internal-slash token like "n/a"
# stays intact (we split on whitespace, NOT on these).
_TOKEN_TRIM_CHARS = "/|,，、-—:：.。!！?？;；"


# ---------------------------------------------------------------------------
# git diff plumbing (mirrors scripts/check_docstring_no_cjk.py)
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(2)
    return result.stdout


def _changed_file_statuses(base: str) -> list[tuple[str, str]]:
    """All file changes in HEAD relative to the merge-base with `base`.
    Returns (status, posix-style path) pairs. Deletions are included, but
    additions can be excluded from the no-split count by the caller.

    ``--no-renames`` disables rename collapsing on purpose: a move OUT of a
    watched module (e.g. ``memory/foo.py`` -> ``tools/foo.py``) would otherwise
    report only the destination path and slip past the watched-prefix check.
    With renames split into delete(old) + add(new), the old watched path still
    shows up."""
    out = _git("diff", "--no-renames", "--name-status", "-z", f"{base}...HEAD")
    fields = out.split("\0")
    changes: list[tuple[str, str]] = []
    for i in range(0, len(fields) - 1, 2):
        status = fields[i]
        path = fields[i + 1]
        if status and path:
            changes.append((status[:1], path.replace("\\", "/")))
    return changes


def _is_test_file(path: str) -> bool:
    """True for any test file — by directory (a ``tests``/``test``/``__tests__``
    segment at any depth) or by basename convention (``test_*.py`` / ``*_test.py``
    / ``*.test.*`` / ``*.spec.*``). Covers both the main tree and ``plugin/``;
    these files are excluded from the no-split file count."""
    parts = path.split("/")
    if any(p in _TEST_DIR_PARTS for p in parts[:-1]):
        return True
    return _TEST_FILE_RE.match(parts[-1]) is not None


def _is_i18n_locale_file(path: str) -> bool:
    """True for locale files that are expected to move as an 8-file group."""
    for locale_dir, files in _I18N_LOCALE_GROUPS.items():
        prefix = f"{locale_dir}/"
        if path.startswith(prefix):
            name = path[len(prefix):]
            return "/" not in name and name in files
    return False


def _counts_for_no_split(status: str, path: str) -> bool:
    """Whether this changed file counts toward the no-split threshold."""
    return (
        status != "A"
        and not _is_test_file(path)
        and not _is_i18n_locale_file(path)
    )


# ---------------------------------------------------------------------------
# PR-body parsing
# ---------------------------------------------------------------------------


def _section_body(body: str, keyword: str) -> str | None:
    """Text under the first heading whose title contains `keyword`, with HTML
    comments stripped. Returns None if no such heading exists (instructions
    live in <!-- --> comments, so they never count as content).

    The section ends at the next heading of the SAME or higher level, so a
    report written with deeper sub-headings (a level-3 heading under the
    level-2 section heading) keeps its sub-sections as content instead of
    being cut off at the first one."""
    text = _COMMENT_RE.sub("", body or "")
    lines = text.splitlines()
    start = None
    level = 6
    for i, ln in enumerate(lines):
        m = _HEADER_RE.match(ln)
        if m and keyword in ln:
            level = len(m.group(1))
            start = i + 1
            break
    if start is None:
        return None
    collected: list[str] = []
    for ln in lines[start:]:
        m = _HEADER_RE.match(ln)
        if m and len(m.group(1)) <= level:
            break
        collected.append(ln)
    return "\n".join(collected).strip()


def _is_filled(section: str | None) -> bool:
    """True iff the section has real content — i.e. it is not empty and not made
    up entirely of placeholder tokens. Splits on whitespace and trims separator
    chars from each token, so a combined placeholder (e.g. a slash-joined
    "not-applicable" pair) collapses to all-placeholder tokens and is rejected,
    while a token whose slash is internal (e.g. "n/a") stays intact."""
    if section is None:
        return False
    cleaned = _EMPHASIS_RE.sub("", section).strip().lower()
    tokens = [
        trimmed
        for raw in cleaned.split()
        if (trimmed := raw.strip(_TOKEN_TRIM_CHARS))
    ]
    if not tokens:
        return False
    return not all(t in _PLACEHOLDERS for t in tokens)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Require regression / no-split reports in the PR body."
    )
    parser.add_argument(
        "--base",
        default=os.environ.get("PR_REPORT_BASE", "origin/main"),
        help="Merge-base ref to diff against (default: origin/main).",
    )
    args = parser.parse_args()

    labels = {
        lbl.strip().lower()
        for lbl in os.environ.get("PR_LABELS", "").split(",")
        if lbl.strip()
    }
    if EXEMPT_LABEL in labels:
        print(f"[pr-report] '{EXEMPT_LABEL}' label present — skipping report checks.")
        return 0

    body = os.environ.get("PR_BODY", "")
    changed_entries = _changed_file_statuses(args.base)
    changed = [path for _, path in changed_entries]
    watched = [
        f for f in changed
        if f.endswith(".py") and any(f.startswith(p) for p in WATCHED_PREFIXES)
    ]
    counted = [
        path for status, path in changed_entries
        if _counts_for_no_split(status, path)
    ]

    violations: list[tuple[str, str]] = []

    if watched and not _is_filled(_section_body(body, REGRESSION_KEYWORD)):
        sample = ", ".join(watched[:5]) + (" …" if len(watched) > 5 else "")
        watched_dirs = " | ".join(WATCHED_PREFIXES)
        violations.append((
            REGRESSION_REPORT,
            f"This PR changes {len(watched)} file(s) under {watched_dirs} "
            f"({sample}) but the PR body has no filled-in "
            f"'{REGRESSION_KEYWORD}' section. Document the change, its "
            f"rationale/necessity, before-and-after behaviour, and regressions.",
        ))

    if len(counted) > FILE_COUNT_LIMIT and not _is_filled(
        _section_body(body, NO_SPLIT_KEYWORD)
    ):
        excluded = len(changed) - len(counted)
        excluded_note = (
            f" ({excluded} new/test/i18n locale file(s) excluded)"
            if excluded else ""
        )
        violations.append((
            NO_SPLIT_RATIONALE,
            f"This PR changes {len(counted)} counted files "
            f"(> {FILE_COUNT_LIMIT}){excluded_note} but the PR body has no filled-in "
            f"'{NO_SPLIT_KEYWORD}' section. Explain why this is not split into "
            f"smaller PRs.",
        ))

    if not violations:
        print(
            f"[pr-report] OK — {len(changed)} file(s) changed "
            f"({len(counted)} counted), {len(watched)} under watched modules."
        )
        return 0

    for code, msg in violations:
        sys.stderr.write(f"{code}  {msg}\n\n")
    sys.stderr.write(
        f"Fill in the missing section(s) in the PR description (template: "
        f".github/pull_request_template.md), or apply the '{EXEMPT_LABEL}' "
        f"label if a maintainer agrees the PR is exempt.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
