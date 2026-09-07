#!/usr/bin/env python3
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

"""Static check: keep heavy SDKs OFF the startup import chain (lazy-import them).

Why this exists — the regression pattern
----------------------------------------
Production (Steam / Nuitka frozen) runs merged single-process mode:
``launcher.run_merged_servers`` imports the three app modules
(memory/agent/main) **serially, before any port binds**, so every module
pulled in at module scope is paid on the every-launch critical path
between double-click and the Pet window becoming interactive.

This class of regression is silent: nothing breaks, startup just gets
slower. Reference incident: PR #1496 cut ``import app.main_server`` to
~0.6s; six weeks later it had crept back to ~2.1s — mostly the openai
2.x SDK's pydantic ``types`` tree growing heavier plus anthropic, both
riding in via a module-level ``from openai import ...`` in
``utils/llm_client.py`` that every server imports transitively. Nobody
noticed, because nothing guarded it.

The repo-wide pattern for these SDKs is **lazy import + background
warmup** (``utils/module_warmup.py``): import inside the function that
first needs it, and list the module in the warmup table so a daemon
thread pre-imports it right after the server is ready. First real use
then never waits. This script pins that pattern down for the modules
already converted, so they cannot silently move back to module scope.

What it flags
-------------
A ``import X`` / ``from X import ...`` of a banned heavy module at
**module scope** (including inside module-level ``if`` / ``try`` /
``with`` blocks and class bodies — those all execute at import time) in
the startup-chain source trees. Imports inside functions/methods are
the sanctioned lazy form and are never flagged. ``if TYPE_CHECKING:``
blocks are skipped — they don't execute at runtime and are the standard
home for annotation-only imports.

Banned modules (all already lazy today; each one's first use is covered
by ``utils/module_warmup.py``):

    openai, anthropic     — LLM SDKs, ~0.7s combined, pydantic class
                            building (CPU-bound, survives freezing);
                            lazy accessors live in utils/llm_client.py
    bs4, bilibili_api     — scraping stack, ~0.3s, lazy in utils/web_scraper.py
    google.genai          — ~0.6s + drags mcp, lazy since PR #1496
    translatepy, googletrans, dashscope, pyncm_async
                          — feature-router deps, lazy per PR #1496
    onnxruntime           — embedding runtime, lazy in memory/embeddings.py

Suppression
-----------
Per-line: append ``# noqa: STARTUP_LAZY_IMPORT`` with a justification
comment when a module-scope import is genuinely required (rare — e.g. a
module that is itself only ever imported lazily AND needs the symbol at
class-definition time). Prefer restructuring to the lazy pattern first.
Directory-level: ``EXCLUDE_DIRS`` lists trees that are not on the
startup import chain (plugins load on demand; brain/cua is only
imported from on-demand agent paths).

Output
------
Every violation prints as ``path:line:col  STARTUP_LAZY_IMPORT  message``.
Exit status is 1 when any violation is found, 0 otherwise.

Usage:
    python scripts/check_startup_import_lazy.py [paths...]
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent

# Startup-chain source trees: everything launcher.run_merged_servers reaches
# via `from app import memory_server / agent_server / main_server`, plus the
# launcher itself and the config package (imported by all of them).
DEFAULT_PATHS: list[str] = [
    "app",
    "brain",
    "config",
    "main_logic",
    "main_routers",
    "memory",
    "utils",
    "launcher.py",
]

EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "dist",
    "build",
    "node_modules",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    # Not on the startup import chain: cua is only imported from on-demand
    # agent execution paths (no module-scope route from the three app
    # modules reaches it). If that ever changes, lazify its openai/anthropic
    # imports first, then remove this exclusion.
    "brain/cua",
}

CODE = "STARTUP_LAZY_IMPORT"

# module head -> where its sanctioned lazy home is (for the error message)
BANNED_MODULES: dict[str, str] = {
    "openai": "utils/llm_client.py (in-function imports + retry-type accessors)",
    "anthropic": "utils/llm_client.py (in-function imports + retry-type accessors)",
    "bs4": "utils/web_scraper.py (in-function imports)",
    "bilibili_api": "utils/web_scraper.py (find_spec probe; import only in handlers)",
    "google.genai": "main_logic/omni_offline_client.py (_ensure_genai)",
    "translatepy": "feature handlers (lazy since PR #1496)",
    "googletrans": "feature handlers (lazy since PR #1496)",
    "dashscope": "feature handlers (lazy since PR #1496)",
    "pyncm_async": "feature handlers (lazy since PR #1496)",
    "onnxruntime": "memory/embeddings.py (_load_session_blocking)",
}


# (module, symbol) -> where its sanctioned lazy home is.
#
# BANNED_MODULES 只认模块名，看不见符号。但同一类回归也可以由一个**符号**造成：
# 一个模块本身很轻，却导出一个求值时才昂贵的惰性名字，任何人在模块作用域取它就
# 把代价搬回了启动链——而 import 那个模块仍然是合法且必要的（同一个文件里还有别的
# 轻量名字要用），所以整模块封禁会把 main 打红，只能按符号封。
BANNED_SYMBOLS: dict[tuple[str, str], str] = {
    # 21 条封禁话题模板的 compile，实测 294-298 ms。模块 __getattr__ 里惰性求值，
    # 预热在 utils/module_warmup.py 的表里。模块作用域取这个名字 = 回到 bind 之前。
    ("config.prompts.prompts_directives", "DIRECTIVE_PATTERNS"): (
        "evaluated on first access via the module __getattr__; warmed in "
        "utils/module_warmup.py"
    ),
}


def _banned_key(module_name: str | None) -> str | None:
    if not module_name:
        return None
    for banned in BANNED_MODULES:
        if module_name == banned or module_name.startswith(banned + "."):
            return banned
    return None


def _is_type_checking_if(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def _iter_module_scope_stmts(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements that execute at import time (never descends into functions)."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(stmt, ast.If):
            if _is_type_checking_if(stmt):
                # TYPE_CHECKING body never runs; the else branch still does.
                yield from _iter_module_scope_stmts(stmt.orelse)
                continue
            yield from _iter_module_scope_stmts(stmt.body)
            yield from _iter_module_scope_stmts(stmt.orelse)
        elif isinstance(stmt, ast.Try):
            yield from _iter_module_scope_stmts(stmt.body)
            for handler in stmt.handlers:
                yield from _iter_module_scope_stmts(handler.body)
            yield from _iter_module_scope_stmts(stmt.orelse)
            yield from _iter_module_scope_stmts(stmt.finalbody)
        elif isinstance(stmt, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
            yield from _iter_module_scope_stmts(stmt.body)
            yield from _iter_module_scope_stmts(getattr(stmt, "orelse", []))
        elif isinstance(stmt, ast.ClassDef):
            # Class bodies execute at import time too.
            yield from _iter_module_scope_stmts(stmt.body)
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                yield from _iter_module_scope_stmts(case.body)
        elif isinstance(stmt, ast.TryStar):
            yield from _iter_module_scope_stmts(stmt.body)
            for handler in stmt.handlers:
                yield from _iter_module_scope_stmts(handler.body)
            yield from _iter_module_scope_stmts(stmt.orelse)
            yield from _iter_module_scope_stmts(stmt.finalbody)


def _noqa_lines(source: str) -> set[int]:
    lines: set[int] = set()
    for idx, line in enumerate(source.splitlines(), start=1):
        if "noqa" in line and CODE in line:
            lines.add(idx)
    return lines


def _iter_eager_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Walk one module-scope statement without entering deferred bodies.

    ``ast.walk`` descends into nested function bodies, and a read in there is
    precisely the sanctioned lazy form this rule exists to steer people toward —
    flagging it would make the guard fight its own advice. Decorators, default
    arguments and annotations do run at import time, so those stay in the walk.
    """
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(current.decorator_list)
            stack.extend(ast.iter_child_nodes(current.args))
            if current.returns is not None:
                stack.append(current.returns)
            continue
        if isinstance(current, ast.Lambda):
            stack.extend(ast.iter_child_nodes(current.args))
            continue
        if isinstance(current, ast.If) and _is_type_checking_if(current):
            # 和 _iter_module_scope_stmts 保持一致：TYPE_CHECKING 的 body 不在运行时
            # 执行，else 分支执行。外层迭代器把这个 If **节点本身**照常 yield 出来
            # （它先 yield 再决定要不要下降），所以这里不跳的话，走查会一头扎进那段
            # 不执行的代码，把零代价的写法报成启动期访问——共享闸门上的误报
            # （codex）。
            #
            # test 不用走：_is_type_checking_if 只认裸 `TYPE_CHECKING` 或
            # `typing.TYPE_CHECKING`，两者都不可能含被禁读取，所以走它是构造上
            # 不可达的死代码。普通 if 的 test 会执行、也确实可能藏读取，那条路
            # 不走这个分支，照常整棵走。
            stack.extend(current.orelse)
            continue
        stack.extend(ast.iter_child_nodes(current))


def _module_alias_map(body: list[ast.stmt]) -> dict[str, str]:
    """Names bound at module scope -> the module path each one stands for.

    Needed because the expensive read can be spelled through an alias:
    ``import config.prompts.prompts_directives as directives`` followed by
    ``directives.DIRECTIVE_PATTERNS``. Only module-scope bindings count; a name
    rebound inside a function cannot put anything on the import chain.
    """
    aliases: dict[str, str] = {}
    for stmt in _iter_module_scope_stmts(body):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    # ``import a.b.c`` binds only ``a``; the rest is spelled out
                    # again at the use site, so the head maps to itself.
                    head = alias.name.split(".")[0]
                    aliases.setdefault(head, head)
        elif isinstance(stmt, ast.ImportFrom) and stmt.module and stmt.level == 0:
            for alias in stmt.names:
                aliases[alias.asname or alias.name] = f"{stmt.module}.{alias.name}"
    return aliases


def _dotted_name(node: ast.AST) -> str | None:
    """``a.b.c`` as a string, or None when the root is not a bare name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _banned_symbol_read(dotted: str, aliases: dict[str, str]) -> tuple[str, str] | None:
    """Resolve ``directives.DIRECTIVE_PATTERNS`` to a BANNED_SYMBOLS hit."""
    parts = dotted.split(".")
    if len(parts) < 2:
        return None
    head, middle, symbol = parts[0], parts[1:-1], parts[-1]
    target = aliases.get(head)
    if target is None:
        # 头名字不是本模块 import 进来的，解析不出模块路径——不猜。
        return None
    module = ".".join([target, *middle])
    home = BANNED_SYMBOLS.get((module, symbol))
    if home is None:
        return None
    return module + "." + symbol, home


def check_source(path: Path, source: str, tree: ast.Module) -> list[tuple[int, int, str]]:
    violations: list[tuple[int, int, str]] = []
    suppressed = _noqa_lines(source)
    aliases = _module_alias_map(tree.body)
    # 全文件去重：同一个读取点会被外层容器和内层语句各遍历一次。
    reported_symbols: set[tuple[int, int, str]] = set()
    for stmt in _iter_module_scope_stmts(tree.body):
        found: list[tuple[str, str]] = []  # (banned key, import text)
        # (qualified name, home, text, lineno, col, whole_stmt) —— 位置取**读取点
        # 自身**，不是
        # 外层语句：一个嵌在模块作用域 if/try/class 里的读取，会被外层容器和内层
        # 语句各走一遍，按语句定位就会报两条。
        symbols: list[tuple[str, str, str, int, int, bool]] = []
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                key = _banned_key(alias.name)
                if key is not None:
                    found.append((key, f"import {alias.name}"))
        elif isinstance(stmt, ast.ImportFrom):
            key = _banned_key(stmt.module)
            if key is not None:
                names = ", ".join(a.name for a in stmt.names) or "*"
                found.append((key, f"from {stmt.module} import {names}"))
            elif stmt.module and stmt.level == 0:
                # `from google import genai` resolves to google.genai — match
                # each alias against the banned list too, or namespace-package
                # spellings slip through.
                for alias in stmt.names:
                    alias_key = _banned_key(f"{stmt.module}.{alias.name}")
                    if alias_key is not None:
                        found.append((alias_key, f"from {stmt.module} import {alias.name}"))
            if stmt.module and stmt.level == 0:
                # 精确 (模块, 符号) 对，不做前缀匹配：同一个模块里的其它名字都是
                # 合法导入，宽一点就会把 main 打红。
                #
                # level == 0 一起卡住：相对导入的 stmt.module 是**相对**路径，
                # 拿它去比绝对的 (模块, 符号) 对会误判——`from ...a.b import X`
                # 解析出来的根本是另一个模块（CodeRabbit）。
                for alias in stmt.names:
                    if alias.name == "*":
                        # 通配导入会把 __all__ 里每个名字都 getattr 一遍，惰性访问器
                        # 照常触发。而 __all__ 恰恰是为了让惰性名字重新出现在通配面里
                        # 才加的，等于亲手给这条规则开了个口子——按字面名 "*" 去查表
                        # 永远查不中（codex）。当成"把这个模块的每个被禁符号都导了"。
                        for (module, symbol), star_home in BANNED_SYMBOLS.items():
                            if module != stmt.module:
                                continue
                            symbols.append(
                                (
                                    f"{module}.{symbol}",
                                    star_home,
                                    f"from {stmt.module} import *",
                                    stmt.lineno,
                                    stmt.col_offset,
                                    True,
                                )
                            )
                        continue
                    home = BANNED_SYMBOLS.get((stmt.module, alias.name))
                    if home is not None:
                        symbols.append(
                            (
                                f"{stmt.module}.{alias.name}",
                                home,
                                f"from {stmt.module} import {alias.name}",
                                stmt.lineno,
                                stmt.col_offset,
                                True,
                            )
                        )
        # 属性读法：`import ... as d` + 模块作用域的 `d.DIRECTIVE_PATTERNS`。
        # 只认 from-import 的话，这条等价写法可以完全绕过闸门，而它触发的是同一个
        # __getattr__、付的是同一份代价（CodeRabbit）。只看 Load，不看赋值目标。
        for node in _iter_eager_nodes(stmt):
            if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
                continue
            dotted = _dotted_name(node)
            if dotted is None:
                continue
            hit = _banned_symbol_read(dotted, aliases)
            if hit is not None:
                symbols.append(
                    (hit[0], hit[1], dotted, node.lineno, node.col_offset, False)
                )
        # noqa on any line of a multiline import suppresses the statement.
        end_lineno = getattr(stmt, "end_lineno", None) or stmt.lineno
        stmt_suppressed = any(
            ln in suppressed for ln in range(stmt.lineno, end_lineno + 1)
        )
        for qualified, home, text, lineno, col, whole_stmt in symbols:
            # 整条语句的 noqa 范围只给 import 语句用——多行 import 上 noqa 写在哪一行
            # 都该算数。属性读取不能用它：一个 FunctionDef 的范围**覆盖整个函数体**，
            # 于是函数体里随便一句 noqa 就能压掉装饰器/默认参数里的 import 期读取；
            # if 语句同理，体里的 noqa 会压掉判断表达式里的读取（CodeRabbit）。
            # 那些只认读取点自己那一行。
            if lineno in suppressed or (whole_stmt and stmt_suppressed):
                continue
            if (lineno, col, qualified) in reported_symbols:
                continue
            reported_symbols.add((lineno, col, qualified))
            violations.append(
                (
                    lineno,
                    col + 1,
                    f"`{text}` at module scope forces `{qualified}` to be "
                    f"evaluated at import time, which puts its cost back on the "
                    f"startup chain before any port binds. Import the module and "
                    f"read the name inside the function that needs it, or call "
                    f"the accessor that wraps it. Sanctioned home: {home}.",
                )
            )
        for key, text in found:
            if stmt_suppressed:
                continue
            violations.append(
                (
                    stmt.lineno,
                    stmt.col_offset + 1,
                    f"`{text}` at module scope puts `{key}` back on the "
                    f"startup import chain — merged production mode imports "
                    f"this tree serially before any port binds, so every "
                    f"launch pays the import again (the #1496→openai-2.x "
                    f"silent-regression pattern). Move the import inside the "
                    f"function that first needs it; background warmup in "
                    f"utils/module_warmup.py keeps first use fast. Sanctioned "
                    f"lazy home: {BANNED_MODULES[key]}.",
                )
            )
    return violations


def _is_excluded(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return True
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()
    for ex in EXCLUDE_DIRS:
        if "/" in ex and (rel == ex or rel.startswith(ex + "/")):
            return True
    return False


def _iter_python_files(paths: Iterable[Path]) -> Iterator[Path]:
    for p in paths:
        if p.is_file():
            if p.suffix == ".py" and not _is_excluded(p):
                yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*.py")):
                if not _is_excluded(f):
                    yield f


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Forbid module-scope imports of heavy lazy-by-contract SDKs on the startup chain."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files/directories to scan (default: startup-chain trees).",
    )
    args = parser.parse_args(argv)

    raw_paths = args.paths or DEFAULT_PATHS
    targets = [Path(p) if Path(p).is_absolute() else REPO_ROOT / p for p in raw_paths]

    total = 0
    for file in _iter_python_files(targets):
        try:
            source = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            print(f"{file}: skipped — {e}", file=sys.stderr)
            continue
        try:
            tree = ast.parse(source, filename=str(file))
        except SyntaxError as e:
            print(f"{file}:{e.lineno}: syntax error — {e.msg}", file=sys.stderr)
            continue
        for lineno, col, msg in check_source(file, source, tree):
            rel = file.relative_to(REPO_ROOT) if file.is_relative_to(REPO_ROOT) else file
            print(f"{rel}:{lineno}:{col}  {CODE}  {msg}")
            total += 1

    if total:
        print(
            f"\n{total} startup-chain lazy-import violation(s) found.\n"
            "These SDKs are lazy-by-contract: production merged mode imports the "
            "app tree serially before any port binds, so a module-scope import "
            "here slows EVERY launch, silently (see #1496 → openai 2.x creep). "
            "Import inside the function that first needs the module and make "
            "sure it is listed in utils/module_warmup.py so the background "
            "warmup thread pre-imports it after the server is ready. If module "
            "scope is genuinely unavoidable, add `# noqa: STARTUP_LAZY_IMPORT` "
            "with a justification.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
