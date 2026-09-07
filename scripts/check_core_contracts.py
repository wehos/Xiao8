#!/usr/bin/env python3
"""Static gate: structural contracts of the ``main_logic/core`` package.

The ``LLMSessionManager`` mixin split (#2272) rests on a set of contracts
that comments alone cannot enforce. This check makes every one of them a
CI failure:

CORE_PATCH_ROUTING
    Every facade symbol that any test rebinds via
    ``monkeypatch.setattr("main_logic.core.<attr>", ...)`` (string form) or
    ``monkeypatch.setattr(core_module, "<attr>", ...)`` /
    ``patch.object(core_module, "<attr>", ...)`` (object form) must be read
    by manager/mixin code ONLY through the ``_core_facade`` late-binding
    module object. A module-level from-import of the symbol's REAL binding —
    ``from <owner_module> import <original_name>`` (the module the facade
    re-exports it from) or ``from main_logic.core import <attr>`` (the facade
    itself) — snapshots the value at import time: assertion-style stubs then
    fail loudly, but isolation-style stubs (blocking disk/network IO) go
    silently green while calling the real function. Matching is by SOURCE
    MODULE + original name, so an unrelated ``from vendor import
    get_tts_worker as vw`` (a different object that merely shares the name) is
    NOT flagged. The patched-symbol set is harvested from ``tests/`` by AST
    on every run, so the contract tightens automatically as tests grow. Reads
    through the OWNER module — attribute chains (``import
    main_logic.agent_event_bus`` / ``from main_logic import agent_event_bus
    as bus`` + ``bus.<attr>(...)``) and string getattr (``getattr(bus,
    "<attr>")``) — are rejected the same way. So are facade reads inside
    method defaults/decorators/annotations and class-level constant values:
    those evaluate once at import and freeze the value. Function-local imports
    from the OWNER module (e.g. ``from utils.preferences import ...`` inside a
    method) are a different, deliberate late-binding pattern and stay allowed:
    the facade patch never targeted them, before or after the split.

    The patch-target harvest recognizes both positional and keyword call
    spellings, string (``"main_logic.core.X"``) and object
    (``setattr(core_module, "X", ...)``) forms, and object targets reached
    through a package alias (``import main_logic as ml`` -> ``ml.core``).

CORE_PATCH_TARGET_EXISTS
    Every patched facade attribute must actually exist at the facade top
    level (typo guard), unless the call passes ``raising=False`` or
    ``create=True`` (intentional absent-name guards).

CORE_MIXIN_SHAPE
    A mixin module's top level holds only a docstring, imports, exactly one
    ``*Mixin`` class, and any explicitly registered private support classes;
    the mixin class body holds only a docstring and
    methods, and the class has an empty base list (a base would pull
    inherited behavior into the MRO uncounted). Instance state has a single
    home (``LLMSessionManager.__init__``), and module-level state in a
    mixin would sit outside the facade's rebind semantics entirely. Any
    core module that is neither a mixin, ``manager.py`` nor a registered
    owner submodule is rejected — adding a new owner module is a conscious
    edit to this check.

CORE_MANAGER_SHAPE
    ``manager.py`` holds exactly one class, ``LLMSessionManager``, and its
    top level holds nothing but docstring/imports/that class; the class
    body holds nothing but a docstring, class-level constants and
    ``__init__``. Any other method, nested class or executable statement is
    behavior/state that belongs in a domain mixin.

CORE_MIXIN_DISJOINT
    No method name is defined by two mixin classes (or by both a mixin and
    the manager class). Python would resolve the clash silently by MRO
    order; this makes it a build failure instead.

CORE_MIXIN_BASES
    The base list of ``LLMSessionManager`` is exactly the set of ``*Mixin``
    classes defined in the package — no orphan mixin module, no missing
    base, every base a plain name (dotted/computed bases would make the
    exact-set comparison silently incomplete), and every base bound via a
    package-relative import (``from .focus import FocusMixin``) so a
    same-named class from outside the package cannot take the MRO slot.

CORE_FACADE_LAYOUT
    ``__init__.py`` defines no class at top level, and its last statement
    is ``from .manager import LLMSessionManager`` — the facade namespace
    must be fully populated before the class modules bind it as
    ``_core_facade``.

ASR_LAYERING
    The Core ASR bridge owns microphone ingress and Core callbacks, while the
    independent runtime owns provider state. TTS cannot import ASR, ASR cannot
    import Core, and provider literals cannot leak into the bridge. Voice-turn
    contracts cannot import ASR; Core cannot bypass the ASR runtime to import
    endpointing; endpointing cannot import Core, workers, or scripts; workers
    cannot import endpointing implementations; lifecycle/provider policy
    cannot depend back on endpointing; ONNX Runtime remains lazy. Streaming
    can only enqueue audio into the bridge. Speaker Shadow remains a
    provider-neutral, observation-only leaf: endpointing can see only its
    contracts, Core can obtain only the opaque factory exported by
    ``asr_client.runtime``, and the package cannot depend back on Core,
    endpointing, workers, lifecycle, policy, voice-input, routers, or scripts.
    Its package initializer stays inert and model runtimes remain lazy.

VOICE_INPUT_LAYERING
    The controlled transcript Registry and its consumers may depend only on
    their own package, provider-neutral voice-turn contracts, and the narrow
    game-route facade. They cannot import Core, ASR/provider code, PCM
    processing, routers, or arbitrary utility modules. ASR runtime code emits
    neutral callbacks and cannot import the Core-owned Registry in reverse.

CORE_LOCK_NO_AWAIT
    No ``async with self.lock`` block holds a suspension point. Twelve of
    those blocks write ``current_speech_id``, eight of them writing the two
    TTS done flags in the same block. Because no holder suspends, the lock is
    never observed held, every acquire takes the uncontended fast path, and
    no acquire is a cancellation point — which is what makes those paired
    writes atomic (#2619). The first ``await`` inside any of these blocks
    makes the lock contendable and reopens a torn "flags say the new turn,
    speech id still says the old one" state at every one of them. The lock
    must also be taken ONLY as a context manager — manual
    ``acquire()``/``release()`` would hold it across awaits while presenting
    no block for the shape check to see.

VOICE_IDENTITY_LAYERING
    The in-memory speaker identity domain owns only model identity, normalized
    references, and profiles. Its package initializer is inert; its domain
    modules cannot depend on ASR, Core, Voice Turn, routers, app runtime, model
    runtimes, or persistence/cryptography libraries. Trusted outer layers may
    consume these provider-neutral contracts without reversing that direction.

Every violation prints as ``path:line:col  CODE  message``. Exit 1 on any
violation, 0 otherwise, 2 when the expected layout itself is missing (this
gate hard-fails rather than silently skipping when paths move — see the
agent_server split postmortem).

Usage:
    python scripts/check_core_contracts.py [--root PATH]
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path

FACADE_MODULE_ALIAS = "_core_facade"
OWNER_SUBMODULES = {
    "_shared",
    "callback_render",
    "game_speech_audio_cache",
    "multimodal_turn",
    "notices",
}
MIXIN_SUPPORT_CLASSES = {
    "asr_runtime": {
        "_QueuedMicFrame",
        "_AudioDurationQueue",
        "_HotSwapAudioFrame",
        "_HotSwapAudioBuffer",
        "_VoiceInputPipelineFailure",
    },
    "tts_runtime": {
        # Private control-flow signal for the game-speech preload batch. It has
        # to be a distinct type from asyncio.CancelledError so that absorbing a
        # supersede/teardown does not also swallow a real task cancellation, and
        # it lives next to its only raiser and catcher.
        "_GameSpeechPreloadCancelled",
    },
}
PATCH_CALL_NAMES = {"setattr", "patch", "delattr"}


class Violation:
    def __init__(self, path, line, col, code, message):
        self.path, self.line, self.col, self.code, self.message = path, line, col, code, message

    def render(self, root: Path) -> str:
        try:
            rel = self.path.resolve().relative_to(root.resolve())
        except ValueError:
            rel = self.path
        return f"{rel}:{self.line}:{self.col}  {self.code}  {self.message}"


def parse(path: Path) -> ast.Module:
    # Read BYTES, not text: ast.parse honors a PEP 263 coding cookie
    # (gbk/shift-jis/latin-1) and strips a UTF-8 BOM. read_text(encoding="utf-8")
    # would keep a BOM (→ SyntaxError) or raise UnicodeDecodeError on a non-UTF-8
    # cookie — either one crashes the whole gate on a valid, importable file
    # (BOM is realistic here: Windows + PowerShell `Out-File -Encoding utf8`).
    return ast.parse(path.read_bytes(), filename=str(path))


# --------------------------------------------------------------- tests scan
def collect_patch_targets(tests_dir: Path):
    """Return {attr: [(file, line, raising_false)]} for facade rebind sites."""
    targets: dict[str, list[tuple[Path, int, bool]]] = {}

    def add(name, path, node, exempt):
        targets.setdefault(name, []).append((path, node.lineno, exempt))

    for path in sorted(tests_dir.rglob("*.py")):
        try:
            tree = parse(path)
        except (SyntaxError, ValueError):
            # Malformed/undecodable test file: skip its harvest rather than
            # crash the gate (ValueError covers UnicodeDecodeError).
            continue
        aliases = set()        # names bound to the main_logic.core MODULE
        pkg_aliases = set()    # names bound to the main_logic PACKAGE (for ``ml.core``)
        patch_aliases = set()  # extra names meaning unittest.mock.patch
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "main_logic.core" and a.asname:
                        aliases.add(a.asname)           # import main_logic.core as core_module
                    elif a.name == "main_logic" and a.asname:
                        pkg_aliases.add(a.asname)        # import main_logic as ml
                    elif a.name == "main_logic" or a.name.startswith("main_logic."):
                        pkg_aliases.add("main_logic")    # import main_logic[.core] → binds main_logic
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module == "main_logic":
                    for a in node.names:
                        if a.name == "core":
                            aliases.add(a.asname or "core")
                elif node.module in ("unittest.mock", "mock"):
                    for a in node.names:
                        if a.name == "patch" and a.asname:
                            patch_aliases.add(a.asname)  # from unittest.mock import patch as mock_patch

        def is_core_ref(expr):
            if isinstance(expr, ast.Name) and expr.id in aliases:
                return True
            # ``main_logic.core`` or ``<pkg-alias>.core`` (import main_logic as ml)
            return (isinstance(expr, ast.Attribute) and expr.attr == "core"
                    and isinstance(expr.value, ast.Name) and expr.value.id in pkg_aliases)

        def is_patch_ref(expr):  # a Name/Attribute referring to unittest.mock.patch
            if isinstance(expr, ast.Name):
                return expr.id == "patch" or expr.id in patch_aliases
            return isinstance(expr, ast.Attribute) and expr.attr == "patch"

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
            fbase = fn.value if isinstance(fn, ast.Attribute) else None
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            # A patch that need not resolve to a real facade attribute:
            #   raising=False  (pytest monkeypatch negative guard)
            #   create=True    (unittest.mock.patch / patch.object absent-name guard)
            # These waive the EXISTENCE requirement (target may be absent); they
            # do NOT waive routing when the target does exist (see run()).
            exempt = ((isinstance(kw.get("raising"), ast.Constant) and kw["raising"].value is False)
                      or (isinstance(kw.get("create"), ast.Constant) and kw["create"].value is True))
            args = node.args
            # Positional and keyword spellings are equivalent for these APIs:
            # setattr(target=..., name=...), patch(target=...),
            # patch.object(target=..., attribute=...).
            first = args[0] if args else kw.get("target")
            second = args[1] if len(args) >= 2 else (kw.get("name") or kw.get("attribute"))
            # monkeypatch.setattr/delattr, patch("...") and aliased patch("...")
            is_str_patch = fname in PATCH_CALL_NAMES or (isinstance(fn, ast.Name) and fn.id in patch_aliases)
            if is_str_patch and first is not None:
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    s = first.value
                    if s.startswith("main_logic.core.") and s.count(".") == 2:
                        add(s.rsplit(".", 1)[1], path, node, exempt)
            # object form: setattr(core_module, "X"), patch.object(core_module, "X").
            # ``.object`` counts only on a real patch receiver — do NOT fold
            # "object" into the union or ``x.object(core_ref, "y")`` on any
            # receiver would be mis-harvested (the is_patch_ref check would be
            # dead code and could invent false patch targets).
            is_obj_patch = fname in PATCH_CALL_NAMES or (fname == "object" and is_patch_ref(fbase))
            if is_obj_patch and first is not None and second is not None:
                if is_core_ref(first) and isinstance(second, ast.Constant) and isinstance(second.value, str):
                    add(second.value, path, node, exempt)
            # patch.multiple("main_logic.core", X=..., Y=...) / (core_module, X=...)
            if fname == "multiple" and is_patch_ref(fbase) and first is not None:
                target_is_core = (
                    (isinstance(first, ast.Constant) and first.value == "main_logic.core")
                    or is_core_ref(first))
                if target_is_core:
                    reserved = {"spec", "spec_set", "create", "autospec", "new_callable", "target"}
                    for k in node.keywords:
                        if k.arg and k.arg not in reserved:
                            add(k.arg, path, node, exempt)
    return targets


# ------------------------------------------------------------ facade layout
def facade_top_level_names(init_tree: ast.Module) -> set[str]:
    names = set()
    for node in init_tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def facade_owner_modules(init_tree: ast.Module) -> dict[str, tuple[str, str]]:
    """{facade name: (absolute owner module, ORIGINAL symbol name)}.

    The facade lives at package ``main_logic.core``, so relative re-exports
    (``from ._shared import CROSS_MODE_RESTART_WAIT_SECONDS``) resolve against
    it. The original name matters for aliased re-exports
    (``from ...bus import dispatch_text_user_message as send_text`` → facade
    name ``send_text`` but the owner attribute is ``dispatch_text_user_message``)
    so ``owner_module_reads`` searches the owner for the RIGHT attribute.
    """
    out: dict[str, tuple[str, str]] = {}
    for node in init_tree.body:
        if isinstance(node, ast.ImportFrom):
            base = node.module if node.level == 0 else _resolve_relative("main_logic.core", node.level, node.module)
            if not base:
                continue
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = (base, a.name)
    return out


# ----------------------------------------------------------- module analysis
def facade_snapshot_imports(tree: ast.Module, pkg: str,
                            facade_owners: dict[str, tuple[str, str]]) -> dict[str, int]:
    """{facade attr: lineno} for module-level from-imports that SNAPSHOT the
    facade's real symbol — a binding the facade patch will not reach.

    A snapshot is ``from <owner_module> import <original_name> [as x]`` (the
    real symbol, under any alias) or ``from main_logic.core import <attr>`` (the
    facade's own copy, frozen at import). Crucially, matching is by SOURCE
    MODULE + original name, not by name alone: an unrelated
    ``from vendor import get_tts_worker as vw`` imports a DIFFERENT object and
    must NOT be flagged (that was a false positive of the old name-only check).
    """
    by_owner = {(mod, orig): attr for attr, (mod, orig) in facade_owners.items()}
    out: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        src = node.module if node.level == 0 else _resolve_relative(pkg, node.level, node.module)
        if not src:
            continue
        for a in node.names:
            if a.name == "*":
                continue
            attr = by_owner.get((src, a.name))
            if attr is not None:
                out.setdefault(attr, node.lineno)          # from owner import orig_name
            elif src == "main_logic.core" and a.name in facade_owners:
                out.setdefault(a.name, node.lineno)         # from the facade itself
    return out


def _resolve_relative(pkg: str, level: int, module) -> str | None:
    """Absolute dotted base for a relative import in package ``pkg``.

    ``from . import x`` (level 1) in ``main_logic.core`` anchors at
    ``main_logic.core``; ``from .. import x`` (level 2) at ``main_logic``.
    Returns None if the level escapes the top of the tree.
    """
    parts = pkg.split(".") if pkg else []
    keep = len(parts) - (level - 1)
    if keep < 0:
        return None
    anchor = parts[:keep]
    if module:
        anchor = anchor + module.split(".")
    return ".".join(anchor)


def _imported_paths(
    node: ast.AST,
    pkg: str,
    alias_paths: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Return absolute module paths imported or referenced by one AST node."""

    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.Attribute) and alias_paths is not None:
        # The innermost attribute is sufficient to expose a package alias:
        # ``ml.core.runtime`` visits ``ml.core`` and resolves it to
        # ``main_logic.core`` without reporting the same expression twice.
        if isinstance(node.value, ast.Attribute):
            return ()
        resolved = resolve_chain(dotted_node_path(node) or "", alias_paths)
        return (resolved,) if resolved else ()
    if not isinstance(node, ast.ImportFrom):
        return ()
    base = (
        node.module
        if node.level == 0
        else _resolve_relative(pkg, node.level, node.module)
    )
    if not base:
        return ()
    members = tuple(
        f"{base}.{alias.name}"
        for alias in node.names
        if alias.name != "*"
    )
    return (base, *members)


def _registry_provider_keys(path: Path) -> frozenset[str]:
    """Extract ASR provider keys from the registry without importing runtime code.

    Hard-fails (exit 2) when the registry shape is unrecognized: silently
    returning an empty set would disable the provider-literal rule while the
    gate keeps reporting green — the exact go-dark failure mode this gate
    exists to prevent (see module docstring).
    """

    for node in parse(path).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name)
            and target.id == "ASR_PROVIDER_REGISTRY"
            for target in targets
        ):
            continue
        value = node.value
        if not isinstance(value, ast.Dict):
            print(f"error: ASR_PROVIDER_REGISTRY in {path} is not a top-level dict literal — "
                  f"the provider-literal rule cannot harvest its keys; keep the registry a "
                  f"literal dict or update _registry_provider_keys in "
                  f"scripts/check_core_contracts.py instead of letting the rule go dark.",
                  file=sys.stderr)
            sys.exit(2)
        return frozenset(
            key.value
            for key in value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        )
    print(f"error: no top-level ASR_PROVIDER_REGISTRY assignment found in {path} — "
          f"the provider-literal rule cannot harvest its keys; restore the assignment or "
          f"update _registry_provider_keys in scripts/check_core_contracts.py instead of "
          f"letting the rule go dark.",
          file=sys.stderr)
    sys.exit(2)


def _dynamic_import_target(
    node: ast.AST,
    alias_paths: dict[str, str],
) -> tuple[tuple[str, ...] | None, bool]:
    """(modules, is_dynamic) for dynamic-import entry points.

    The static layering scans see only ``import``/``from`` forms, so
    ``importlib.import_module("main_logic.core")`` would sail through the gate.
    ``modules`` contains the absolute module paths the call can load when they
    are statically knowable, including relative ``import_module`` targets and
    literal ``__import__`` fromlist entries. It is None when any required part
    cannot be inferred, so guarded packages fail closed. ``is_dynamic`` is True
    whenever the call is one of the two entry points. Recognizes ``importlib``
    under an alias and ``from importlib import import_module [as x]`` through
    ``alias_paths``.
    """
    if not isinstance(node, ast.Call):
        return None, False
    chain = dotted_node_path(node.func)
    if chain is None:
        return None, False
    resolved = resolve_chain(chain, alias_paths) or chain
    if resolved not in {"__import__", "importlib.import_module"}:
        return None, False
    name_arg = node.args[0] if node.args else next(
        (kw.value for kw in node.keywords if kw.arg == "name"),
        None,
    )
    if not (
        isinstance(name_arg, ast.Constant)
        and isinstance(name_arg.value, str)
    ):
        return None, True
    name = name_arg.value

    if resolved == "importlib.import_module":
        if not name.startswith("."):
            return (name,), True
        package_arg = (
            node.args[1]
            if len(node.args) > 1
            else next(
                (kw.value for kw in node.keywords if kw.arg == "package"),
                None,
            )
        )
        if not (
            isinstance(package_arg, ast.Constant)
            and isinstance(package_arg.value, str)
        ):
            return None, True
        try:
            return (
                importlib.util.resolve_name(name, package_arg.value),
            ), True
        except (ImportError, ValueError):
            return None, True

    level_arg = (
        node.args[4]
        if len(node.args) > 4
        else next(
            (kw.value for kw in node.keywords if kw.arg == "level"),
            None,
        )
    )
    if level_arg is not None and not (
        isinstance(level_arg, ast.Constant)
        and level_arg.value == 0
    ):
        return None, True
    fromlist_arg = (
        node.args[3]
        if len(node.args) > 3
        else next(
            (kw.value for kw in node.keywords if kw.arg == "fromlist"),
            None,
        )
    )
    if fromlist_arg is None:
        return (name,), True
    if not isinstance(fromlist_arg, (ast.List, ast.Tuple, ast.Set)):
        return None, True
    entries: list[str] = []
    for entry in fromlist_arg.elts:
        if not (
            isinstance(entry, ast.Constant)
            and isinstance(entry.value, str)
            and entry.value
            and entry.value != "*"
        ):
            return None, True
        entries.append(entry.value)
    targets = [name]
    targets.extend(f"{name}.{entry}" for entry in entries)
    return tuple(dict.fromkeys(targets)), True


def _name_binding(node: ast.AST) -> tuple[str, ast.AST] | None:
    """(target name, value expr) for a simple single-name binding, else None.

    Covers plain ``x = v``, annotated ``x: T = v`` (a bare annotation without a
    value binds nothing), and walrus ``(x := v)``. ``ast.AugAssign`` is
    deliberately excluded: ``x += v`` requires ``x`` to be bound already and
    never creates a fresh alias to ``v``.
    """
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        target, value = node.target, node.value
    elif isinstance(node, ast.NamedExpr):
        target, value = node.target, node.value
    else:
        return None
    if not isinstance(target, ast.Name):
        return None
    return target.id, value


def _importlib_alias_paths(tree: ast.Module) -> dict[str, str]:
    """importlib-related bindings from ANY scope → absolute dotted path.

    ``module_alias_paths`` only reads ``tree.body``, so a function-local
    ``import importlib as il`` or ``from importlib import import_module as im``
    would evade the dynamic-import gate. This walks the whole tree and is
    deliberately scope-insensitive: a binding collected here applies to the
    entire module even where Python scoping would shadow it. For a gate that
    over-approximation is the right trade — flagging a shadowed name is better
    than a blind spot. Only importlib bindings are collected, so unrelated
    local names never resolve to a dynamic-import entry point.

    Assignment re-bindings are covered too: ``il = importlib``,
    ``il: ModuleType = importlib``, ``(il := importlib)``,
    ``im = importlib.import_module`` and ``f = __import__`` all resolve to the
    same entry points (iterated to a fixpoint so chained re-aliases like
    ``a = importlib; b = a`` cannot dodge the gate either).
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "importlib" or a.name.startswith("importlib."):
                    out[a.asname or a.name.split(".")[0]] = (
                        a.name if a.asname else "importlib")
        elif (isinstance(node, ast.ImportFrom) and node.level == 0
              and node.module
              and (node.module == "importlib"
                   or node.module.startswith("importlib."))):
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{node.module}.{a.name}"
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            binding = _name_binding(node)
            if binding is None:
                continue
            name, value = binding
            chain = dotted_node_path(value)
            if chain is None:
                continue
            resolved = resolve_chain(chain, out) or (
                "__import__" if chain == "__import__" else None)
            if (resolved
                    and (resolved == "__import__" or resolved == "importlib"
                         or resolved.startswith("importlib."))
                    and out.get(name) != resolved):
                out[name] = resolved
                changed = True
    return out


def _dynamic_import_violations(
    path: Path,
    tree: ast.Module,
    alias_paths: dict[str, str],
    forbidden_prefix: str | tuple[str, ...],
    where: str,
    report_generic: bool = True,
) -> list["Violation"]:
    """ASR_LAYERING violations for dynamic imports in a guarded module.

    ``forbidden_prefix`` accepts one or multiple forbidden prefixes. A literal
    target inside any of them is flagged the same way the static import ban
    does, and any non-literal target is rejected outright — the gate cannot
    prove a computed module name stays on the right side of the boundary.
    """
    forbidden_prefixes = (
        (forbidden_prefix,)
        if isinstance(forbidden_prefix, str)
        else forbidden_prefix
    )
    # Function-local importlib aliases win over same-named module-level
    # bindings so nested ``import importlib as il`` cannot dodge the gate;
    # module-level importlib aliases resolve identically through either dict.
    alias_paths = {**alias_paths, **_importlib_alias_paths(tree)}
    out: list[Violation] = []
    for node in ast.walk(tree):
        targets, dynamic = _dynamic_import_target(node, alias_paths)
        if not dynamic:
            continue
        if targets is None:
            if report_generic:
                out.append(Violation(
                    path, node.lineno, node.col_offset, "ASR_LAYERING",
                    f"dynamic import with a non-literal module name is not allowed in "
                    f"{where} — the layering gate cannot verify its target; use a "
                    f"static import or a string literal",
                ))
            continue
        matched_prefix = next(
            (
                prefix
                for target in targets
                for prefix in forbidden_prefixes
                if target == prefix
                or target.startswith(f"{prefix}.")
            ),
            None,
        )
        if matched_prefix is None:
            continue
        out.append(Violation(
            path, node.lineno, node.col_offset, "ASR_LAYERING",
            f"{where} must not import {matched_prefix} (dynamic import)",
        ))
    return out


def _module_scope_nodes(tree: ast.Module):
    """Yield nodes evaluated at module import time, excluding function bodies."""

    stack = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _asr_runtime_alias_reads(fn: ast.AST, forbidden: set[str]) -> list[tuple[int, int, str]]:
    """(line, col, attr) reads of forbidden runtime fields through a local alias.

    ``rt = self._asr_runtime; rt.lifecycle`` dodges the exact three-node
    ``self._asr_runtime.<attr>`` pattern the bridge scan matches. Track simple
    single-target Name bindings from ``self._asr_runtime`` — plain, annotated
    (``rt: T = self._asr_runtime``) and walrus assignments alike, via
    ``_name_binding`` — within one function scope (order-insensitive and
    without reassignment tracking — a deliberately conservative
    over-approximation for a gate).
    """
    aliases = set()
    for child in ast.walk(fn):
        binding = _name_binding(child)
        if binding is None:
            continue
        name, value = binding
        if (isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == "self"
                and value.attr == "_asr_runtime"):
            aliases.add(name)
    if not aliases:
        return []
    return [
        (child.lineno, child.col_offset, child.attr)
        for child in ast.walk(fn)
        if isinstance(child, ast.Attribute)
        and child.attr in forbidden
        and isinstance(child.value, ast.Name)
        and child.value.id in aliases
    ]


def module_alias_paths(tree: ast.Module, pkg: str) -> dict[str, str]:
    """Module/name bindings at top level → ABSOLUTE dotted path.

    ``pkg`` is the importing module's package (``main_logic.core``) so relative
    imports resolve to absolute paths and compare cleanly against
    ``main_logic.core``. Covers ``import a.b`` (binds ``a``; full chain kept
    too), ``import a.b as c``, package aliases (``import main_logic as ml`` →
    ``ml`` → ``main_logic``, so ``ml.core`` resolves via prefix), and both
    absolute and relative from-imports (``from main_logic import agent_event_bus
    as bus`` / ``from .. import agent_event_bus as bus`` → the same owner
    module; ``from main_logic import core as _core_facade`` → ``main_logic.core``).

    From-imports of plain symbols land here too, but that is harmless: the
    routing scan (``owner_module_reads``) only flags a ``<binding>.<attr>`` read
    when the binding resolves to the attr's ACTUAL owner module, so a plain
    imported object with a coincidentally same-named method never matches.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    root = a.name.split(".")[0]
                    out.setdefault(root, root)
                    out[a.name] = a.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module if node.level == 0 else _resolve_relative(pkg, node.level, node.module)
            if base is None:
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                out[a.asname or a.name] = f"{base}.{a.name}"
    return out


def resolve_chain(chain: str, alias_paths: dict[str, str]) -> str | None:
    """Resolve a dotted read chain to an absolute module path, or None.

    Exact binding first (``bus`` → ``main_logic.agent_event_bus``); else
    substitute a bound prefix (``ml.agent_event_bus`` where ``ml`` →
    ``main_logic`` yields ``main_logic.agent_event_bus``).
    """
    if chain in alias_paths:
        return alias_paths[chain]
    parts = chain.split(".")
    if parts[0] in alias_paths:
        rest = parts[1:]
        return ".".join([alias_paths[parts[0]], *rest]) if rest else alias_paths[parts[0]]
    return None


def dotted_node_path(node):
    """Dotted string of a Name/Attribute node itself, or None."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def attr_value_chain(node: ast.Attribute):
    """Dotted string of an attribute node's value side, or None."""
    return dotted_node_path(node.value)


def owner_module_reads(tree: ast.Module, alias_paths: dict[str, str], owner_info):
    """(line, col, module) for reads of the owner attribute that a facade patch
    would miss.

    ``owner_info`` is ``(owner_module, original_name)``: the facade re-exports
    the patched symbol from ``owner_module`` under ``original_name`` (which
    differs from the facade name only for aliased re-exports). A monkeypatch of
    ``main_logic.core.<facade_name>`` rebinds only the facade copy, so a mixin
    that reads ``owner_module.original_name`` (or ``getattr(...)``) sees the
    un-patched original. Matching the SPECIFIC owner module and its ORIGINAL
    attribute name avoids flagging a coincidental same-named attribute on an
    unrelated object. If ``owner_info`` is unknown (attr not from-imported by
    the facade) nothing is flagged here.
    """
    if not owner_info:
        return []
    owner, name = owner_info
    sites = []
    for node in ast.walk(tree):
        chain = None
        if isinstance(node, ast.Attribute) and node.attr == name:
            chain = attr_value_chain(node)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "getattr" and len(node.args) >= 2
              and isinstance(node.args[1], ast.Constant) and node.args[1].value == name):
            chain = dotted_node_path(node.args[0])
        if chain is None:
            continue
        if resolve_chain(chain, alias_paths) == owner:
            sites.append((node.lineno, node.col_offset, owner))
    return sites


def def_time_facade_reads(tree: ast.Module, alias_paths: dict[str, str], attr: str):
    """(line, col) of facade reads of ``attr`` evaluated at class-creation.

    Class decorators, method decorators, defaults, evaluated annotations and
    class-level constant values run ONCE at import time; a facade read there
    (attribute ``_core_facade.<attr>`` or ``getattr(_core_facade, "<attr>")``)
    freezes the value, so later facade patches no longer reach it — the read
    must live in the method body.
    """
    sites = []
    for klass in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        def_time_nodes = list(klass.decorator_list)  # the class's own decorators
        for stmt in klass.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = stmt.args
                def_time_nodes += stmt.decorator_list
                def_time_nodes += [d for d in a.defaults if d is not None]
                def_time_nodes += [d for d in a.kw_defaults if d is not None]
                for arg in (a.posonlyargs + a.args + a.kwonlyargs
                            + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else [])):
                    if arg.annotation is not None:
                        def_time_nodes.append(arg.annotation)
                if stmt.returns is not None:
                    def_time_nodes.append(stmt.returns)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                # Class-level constants (allowed in manager.py) evaluate at
                # class creation too — a facade read there freezes the value.
                if stmt.value is not None:
                    def_time_nodes.append(stmt.value)
        for sub in def_time_nodes:
            for node in ast.walk(sub):
                chain = None
                if isinstance(node, ast.Attribute) and node.attr == attr:
                    chain = attr_value_chain(node)
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                      and node.func.id == "getattr" and len(node.args) >= 2
                      and isinstance(node.args[1], ast.Constant) and node.args[1].value == attr):
                    chain = dotted_node_path(node.args[0])
                if chain and resolve_chain(chain, alias_paths) == "main_logic.core":
                    sites.append((node.lineno, node.col_offset))
    return sites


# ------------------------------------------------------------------- checks
def check_fail_closed_chokepoint(core_dir: Path) -> list[Violation]:
    """VOICE_FAIL_CLOSED_CHOKEPOINT — one way for a route to end fail-closed.

    Five call sites can leave the microphone route blocked while independent
    ASR is enabled, and every review round on #2345 found another. Each one
    has to notify the LEASE holder, then re-check that no competing newer
    route operation has taken over, and only then revoke — because
    ``_revoke_voice_input_connection`` calls ``_invalidate_asr_start()`` and
    would otherwise cancel that newer start.

    That order now lives in ``_fail_closed_voice_route``. Keeping it correct
    by construction means the revoke helpers must be unreachable from
    anywhere else in the core package: a new exit then cannot open the mic
    onto a dead route by simply forgetting a step, because there is no step
    left to forget.

    Outside ``main_logic/core`` the disconnect-cleanup caller in
    ``main_routers/websocket_router.py`` is untouched and deliberately so —
    it revokes because a socket departed, not because a route ended blocked,
    and it is reached through a getattr by name, not this call graph.
    """

    REVOKE_HELPERS = {"_revoke_lease_for_blocked_route", "_revoke_voice_input_connection"}
    CHOKEPOINT = "_fail_closed_voice_route"
    CHOKEPOINT_PATH = core_dir / "asr_runtime.py"

    def called_name(target: ast.expr) -> str | None:
        """Resolve the callee name, including a literal ``getattr`` lookup.

        CodeRabbit: matching only ``Name``/``Attribute`` let
        ``getattr(self, "_revoke_lease_for_blocked_route")(...)`` straight
        through — the callee is a ``Call`` node there, so the gate resolved no
        name at all and reported nothing. Measured: the direct form produces
        one violation, the getattr form produced zero. A gate a one-line
        rewrite defeats is not a gate.
        """

        if isinstance(target, ast.Attribute):
            return target.attr
        if isinstance(target, ast.Name):
            return target.id
        if (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Name)
            and target.func.id == "getattr"
            and len(target.args) >= 2
            and isinstance(target.args[1], ast.Constant)
            and isinstance(target.args[1].value, str)
        ):
            return target.args[1].value
        return None

    violations: list[Violation] = []
    chokepoint_seen = False
    for path in sorted(core_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = parse(path)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Pinned to the canonical module: a same-named function anywhere
            # else in the package would otherwise exempt ITSELF from the gate
            # and satisfy the not-vacuous check below at the same time.
            is_chokepoint = path == CHOKEPOINT_PATH and func.name == CHOKEPOINT
            if is_chokepoint:
                chokepoint_seen = True
            # A helper may call its own downstream revoke; what must not
            # happen is an ARBITRARY function reaching one directly.
            if func.name in REVOKE_HELPERS or is_chokepoint:
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                name = called_name(node.func)
                if name in REVOKE_HELPERS:
                    violations.append(Violation(
                        path, node.lineno, node.col_offset, "VOICE_FAIL_CLOSED_CHOKEPOINT",
                        f"{func.name}() calls {name}() directly — every fail-closed route exit "
                        f"must go through {CHOKEPOINT}(reason, operation_generation=..., ...), "
                        f"which notifies the lease holder BEFORE the revoke and re-fences the "
                        f"route operation in between; revoking on a stale exit cancels a "
                        f"competing newer start"))
    if not chokepoint_seen:
        violations.append(Violation(
            core_dir / "asr_runtime.py", 1, 0, "VOICE_FAIL_CLOSED_CHOKEPOINT",
            f"{CHOKEPOINT}() is gone from main_logic/core — it is the only sanctioned caller "
            f"of {sorted(REVOKE_HELPERS)}, so its removal makes this gate vacuous"))
    return violations


def _statements_in_critical_section(body: list[ast.stmt]):
    """Yield every node a critical section actually executes.

    A generator expression is deferred the same way, with one eager part:
    measured, ``(await work(x) async for x in src())`` evaluates NOTHING at
    creation, while ``(x for x in await get())`` does evaluate the outermost
    iterable there. So only ``generators[0].iter`` is walked for those. List,
    set and dict comprehensions are NOT deferred — measured, they run their
    element expression immediately — and stay fully walked.

    A nested ``def`` / ``async def`` / ``lambda`` splits into two halves that
    run at different times, and only the body is deferred:

    * the BODY runs when the closure is called, which is necessarily after
      the block has exited, so an ``await`` in there is not a suspension of
      this critical section and flagging it would be a false positive;
    * the DEFINITION-TIME parts — decorators, default values, annotations —
      are evaluated right here, while the lock is held. ``async def
      later(x=await self.flush())`` parses, and the default is evaluated at
      def time (measured), so skipping the whole node would let a real
      suspension through.

    So the body is skipped and everything else about the definition is still
    walked.
    """

    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.GeneratorExp):
            # Everything but the outermost iterable runs at consumption time,
            # which is necessarily after the block has exited.
            if node.generators:
                stack.append(node.generators[0].iter)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # ``Lambda.body`` is one expression; the others hold a statement
            # list. Compare by identity — AST nodes have no ``__eq__``, and
            # two structurally identical children must not collapse.
            deferred = (
                {id(node.body)}
                if isinstance(node, ast.Lambda)
                else {id(stmt) for stmt in node.body}
            )
            stack.extend(
                child
                for child in ast.iter_child_nodes(node)
                if id(child) not in deferred
            )
            continue
        stack.extend(ast.iter_child_nodes(node))


def _name_binding_sites(tree: ast.AST, name: str) -> list[ast.AST]:
    """Return every node that binds ``name``, in any scope, by any syntax.

    Written as an enumeration of BINDINGS rather than of ways-to-rebind: the
    latter is open-ended (import-as, assignment, parameter, loop target,
    ``with ... as``, ``except ... as``, comprehension, walrus, ``match``
    capture, a def or class of that name …) and a checker built by listing
    them stays one form behind whoever is looking. Scope is deliberately
    ignored — any binding of the name anywhere in the module is enough to
    make a spelling-based check on it unsound.

    Models ordinary binding syntax only, not reflective mutation through
    ``globals()`` / ``exec`` / attribute writes on the module object.
    """

    sites: list[ast.AST] = []
    for node in ast.walk(tree):
        # The ``ast.alias`` is returned, not the containing import: one
        # statement can carry several, and ``import asyncio, vendor as
        # asyncio`` must not be judged by whichever alias happens to look
        # right — Python leaves the name bound to the LAST one.
        if isinstance(node, ast.Import):
            sites.extend(
                alias for alias in node.names
                if (alias.asname or alias.name.split(".")[0]) == name
            )
        elif isinstance(node, ast.ImportFrom):
            sites.extend(
                alias for alias in node.names
                # ``from x import *`` can bind ANY name the module exports,
                # including this one, and nothing in the AST says which. It
                # counts as a binding of every name — unknown, so not the
                # sanctioned one.
                if alias.name == "*" or (alias.asname or alias.name) == name
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                sites.append(node)
        elif isinstance(node, ast.arg):
            if node.arg == name:
                sites.append(node)
        elif isinstance(node, ast.Assign):
            if any(
                name in _assignment_target_names(target) for target in node.targets
            ):
                sites.append(node)
        elif isinstance(node, ast.AnnAssign):
            if name not in _assignment_target_names(node.target):
                pass
            elif node.value is not None:
                sites.append(node)
            elif isinstance(node.target, ast.Name):
                # A valueless annotation binds no VALUE but does affect scope:
                # ``asyncio: object`` anywhere in a function makes the name
                # local for that whole function, so a later ``asyncio.Lock()``
                # raises UnboundLocalError rather than reaching the module.
                # Either way the name no longer means the import, so it counts.
                # (An ATTRIBUTE annotation — ``self.lock: asyncio.Lock`` — has
                # no such effect and is handled where lock bindings are
                # collected, not here.)
                sites.append(node)
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            if name in _assignment_target_names(node.target):
                sites.append(node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if name in _assignment_target_names(node.target):
                sites.append(node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            if any(
                item.optional_vars is not None
                and name in _assignment_target_names(item.optional_vars)
                for item in node.items
            ):
                sites.append(node)
        elif isinstance(node, ast.ExceptHandler):
            if node.name == name:
                sites.append(node)
        elif isinstance(node, ast.comprehension):
            if name in _assignment_target_names(node.target):
                sites.append(node)
        elif isinstance(node, ast.match_case):
            if name in _match_pattern_binding_names(node.pattern):
                sites.append(node)
    return sites


def check_session_lock_atomicity(core_dir: Path, manager_path: Path) -> list[Violation]:
    """CORE_LOCK_NO_AWAIT — the session lock is never held across a suspension.

    Every ``async with self.lock`` block in the package holds only
    synchronous statements. That is not a style preference, it is what makes
    the speech-id rotations atomic, and it holds by induction: if no holder
    ever suspends while holding the lock, no other task can ever observe it
    held, so ``await self.lock.acquire()`` always takes the uncontended fast
    path and never yields — which means cancellation cannot be delivered
    between taking the lock and the writes inside it.

    #2619 read the same code the other way round and proposed reordering the
    writes in ``rotate_speech_id_for_response_done`` to close a torn-state
    window. Measured, that window does not exist while this contract holds:
    the acquire never suspends, so the rotation either runs whole or does not
    start. The first ``await`` added inside any of these blocks is what would
    make the window real — for that rotation and for the eight other blocks
    that write ``current_speech_id`` alongside both TTS done flags. So the
    invariant is the fix, and this gate is what keeps it true.

    Two halves, because a shape check alone is defeatable. The reachable
    shape — no suspension inside any ``async with self.lock`` block — is only
    meaningful if every acquisition IS such a block, so the lock is also
    required to appear nowhere else: manual ``acquire()``/``release()`` holds
    it across arbitrary awaits while presenting no block to inspect.
    ``manager.py``'s binding assignment is the single exemption.

    The check is deliberately package-wide rather than pinned to the rotation
    sites: the property is a property of the lock, and one careless holder
    anywhere is enough to lose it everywhere.

    Known boundary: the scan covers ``main_logic/core`` only. A holder
    OUTSIDE the package — ``async with manager.lock: await ...`` somewhere
    that was handed a manager — would make the lock contendable just the
    same, and this gate would not see it. That gap is not closed here
    because the cheap closure is not sound: matching ``.lock`` repo-wide
    collides with unrelated locks that legitimately suspend under
    themselves (multiple plugins legitimately suspend under plugin-owned
    locks by design), and demanding an allowlist
    entry from unrelated code makes the gate about the wrong thing. What IS
    closed is the leak path: core cannot hand the lock out, because every
    ``.lock`` mention there must be an ``async with`` context expression, so
    an external holder can only arise from new code reaching into the
    attribute directly. Measured today: no production module outside the
    package references a manager's ``.lock`` at all.

    Second known boundary: the ``asyncio`` binding check is scope- and
    control-flow-blind. It requires every binding of the name to be a plain
    ``import asyncio``, but does not work out which one reaches the
    ``self.lock`` assignment — a module-level import plus a conditional
    ``import asyncio`` inside ``__init__`` satisfies it. That is deliberate:
    the local import makes the name local for the whole method, so the
    un-taken branch raises ``UnboundLocalError`` at ``asyncio.Lock()``
    (measured). The failure is a crash at construction, not a lock with
    different suspension semantics slipping through — unlike the shadowing
    forms above, which run fine and only the gate cannot see. Deciding it
    properly needs reaching-definition analysis at the assignment, which is a
    dataflow pass this syntax-level gate does not want to become.
    """

    def suspension_kind(node: ast.AST) -> str | None:
        # ``yield`` counts: it turns the holder into an async generator and
        # hands control back to whoever drives it, which can abandon or throw
        # into the generator while the lock is still held.
        if isinstance(node, ast.Await):
            return "await"
        if isinstance(node, ast.AsyncFor):
            return "async for"
        if isinstance(node, ast.AsyncWith):
            return "nested async with"
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return "yield"
        # ``[x async for x in y]`` carries no AsyncFor node of its own.
        if isinstance(node, ast.comprehension) and node.is_async:
            return "async comprehension"
        return None

    def is_lock_attr(node: ast.AST) -> bool:
        """Any reach for the ``lock`` attribute, however it is spelled.

        Deliberately NOT pinned to a literal ``self`` receiver: ``owner =
        self`` followed by ``async with owner.lock:`` acquires the same
        object, and resolving aliases properly is dataflow analysis. Matching
        the attribute name alone over-approximates instead, which is the safe
        direction — and measured, it costs nothing: the package contains no
        ``.lock`` acquisition with any other receiver, and manager.py's
        binding is the only non-``async with`` mention at all.

        The literal reflective spellings count too — ``getattr(x, "lock")``,
        ``setattr(x, "lock", …)`` and ``x.__dict__["lock"]`` reach the same
        attribute while carrying no ``Attribute`` node named ``lock`` for a
        syntax-only match to see. Reads and WRITES both: a reflective write
        replaces the primitive itself, which the exact-once binding check
        would never notice, and then a perfectly clean-looking ``async with
        self.lock:`` runs on a lock whose acquire may suspend. Same reasoning
        as ``check_fail_closed_chokepoint``'s ``called_name`` in this file: a
        gate a one-line rewrite defeats is not a gate. A computed name
        (``getattr(x, name)``) is out of reach of any AST check and is not
        claimed to be covered.
        """

        return is_lock_read(node) or is_lock_write(node)

    def is_lock_read(node: ast.AST) -> bool:
        """A reach for the attribute that yields the lock object."""

        if isinstance(node, ast.Attribute):
            return node.attr == "lock"
        if (
            isinstance(node, ast.Call)
            and dotted_node_path(node.func) in {"getattr", "object.__getattribute__"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "lock"
        ):
            return True
        return (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "__dict__"
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "lock"
        )

    def is_lock_write(node: ast.AST) -> bool:
        """A reflective call that REPLACES the attribute."""

        return (
            isinstance(node, ast.Call)
            and dotted_node_path(node.func) in {"setattr", "object.__setattr__"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "lock"
        )

    def is_session_lock(item: ast.withitem) -> bool:
        """Reads only — a write is never an acquisition.

        Both spellings have to be recognised by the form check, but only a
        READ can be the thing an ``async with`` acquires. Letting a write
        qualify here sanctioned the setter itself: ``async with setattr(self,
        "lock", OtherLock()): pass`` was recorded as a lock acquisition and
        reported nothing, even though the swap already happened by the time
        entering ``None`` raises TypeError — which the surrounding code is
        free to catch.
        """

        return is_lock_read(item.context_expr)

    def is_self_lock_attr(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "lock"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    violations: list[Violation] = []
    blocks_seen = 0
    manager_bindings: list[tuple[ast.AST, ast.expr | None]] = []
    annotation_only: list[ast.AST] = []
    # rglob, and no ``__init__.py`` exemption: a holder in the facade or in a
    # subpackage is inside the package whose invariant this enforces. The flat
    # layout is enforced separately by CORE_MIXIN_SHAPE, but this gate must not
    # depend on that one still being there to be complete.
    for path in sorted(p for p in core_dir.rglob("*.py") if "__pycache__" not in p.parts):
        tree = parse(path)
        # Every sanctioned mention of the lock is the context expression of an
        # ``async with``. Manual ``await self.lock.acquire()`` ... ``release()``
        # would hold it across arbitrary awaits while presenting no AsyncWith
        # node for the scan below to inspect — the one rewrite that defeats
        # this gate without tripping it. So the reachable-shape check is
        # paired with a form check: any other reference to ``self.lock`` is
        # rejected outright, which also covers handing the lock to a helper
        # that awaits under it.
        sanctioned: set[int] = {
            id(item.context_expr)
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncWith)
            for item in node.items
            if is_session_lock(item)
        }
        # The module can stay the real stdlib one while its ``Lock`` attribute
        # is replaced: ``asyncio.Lock = OtherLock`` leaves both the name check
        # and the ``asyncio.Lock()`` spelling intact while the manager builds
        # an arbitrary primitive. Same family as the reflective writes above —
        # the swap happens where the checker was only reading spelling.
        # Reflective writes count the same as direct ones, for the same reason
        # they do on the lock attribute itself: catching one spelling and not
        # the other is not a gate, it is a speed bump.
        def replaces_asyncio_lock(node: ast.AST) -> bool:
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if dotted_node_path(target) == "asyncio.Lock":
                    return True
                # ``asyncio.__dict__["Lock"] = X``
                if (
                    isinstance(target, ast.Subscript)
                    and dotted_node_path(target.value) == "asyncio.__dict__"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "Lock"
                ):
                    return True
            # ``setattr(asyncio, "Lock", X)`` / ``object.__setattr__(...)``
            return (
                isinstance(node, ast.Call)
                and dotted_node_path(node.func) in {"setattr", "object.__setattr__"}
                and len(node.args) >= 2
                and dotted_node_path(node.args[0]) == "asyncio"
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "Lock"
            )

        for node in ast.walk(tree):
            if replaces_asyncio_lock(node):
                violations.append(Violation(
                    path, node.lineno, node.col_offset, "CORE_LOCK_NO_AWAIT",
                    "asyncio.Lock is replaced — the primitive check validates the "
                    "spelling 'asyncio.Lock()' and that 'asyncio' means the standard "
                    "library, but neither survives the attribute itself being "
                    "replaced; the manager would then build an arbitrary lock whose "
                    "acquire may suspend (#2619)"))
        # manager.py binds the attribute; that assignment target is the only
        # non-``async with`` mention the package is allowed to carry. Every
        # binding is collected — not just the first — because the primitive
        # check below has to see a rebind that a later statement performs.
        if path == manager_path:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    if not any(is_self_lock_attr(t) for t in node.targets):
                        continue
                    # ``self.other_lock = self.lock = asyncio.Lock()`` binds
                    # ONE object to two names. The gate deliberately ignores
                    # non-session locks, so the second name would then be a
                    # sanctioned handle on the session lock that may be held
                    # across awaits. One target only.
                    if len(node.targets) != 1:
                        violations.append(Violation(
                            path, node.lineno, node.col_offset, "CORE_LOCK_NO_AWAIT",
                            "the self.lock binding must have exactly one target — a "
                            "chained assignment aliases the same lock object under "
                            "another name, and every other lock name is allowed to be "
                            "held across awaits, so the alias becomes a way to suspend "
                            "while holding the session lock (#2619)"))
                    manager_bindings.extend(
                        (target, node.value)
                        for target in node.targets
                        if is_self_lock_attr(target)
                    )
                elif isinstance(node, ast.AnnAssign) and is_self_lock_attr(node.target):
                    if node.value is None:
                        # ``self.lock: asyncio.Lock`` with no value is a type
                        # declaration; it neither assigns nor replaces the
                        # attribute, so it is exempt from the form check
                        # without counting as a binding.
                        annotation_only.append(node.target)
                    else:
                        manager_bindings.append((node.target, node.value))
        bind_targets = {id(target) for target, _ in manager_bindings}
        bind_targets.update(id(target) for target in annotation_only)
        for node in ast.walk(tree):
            if not is_lock_attr(node):
                continue
            if id(node) in sanctioned or id(node) in bind_targets:
                continue
            violations.append(Violation(
                path, node.lineno, node.col_offset, "CORE_LOCK_NO_AWAIT",
                "a '.lock' attribute is referenced outside an 'async with' block — the "
                "session lock may only be taken as a context manager, and the receiver "
                "is not required to be literally 'self' because an alias reaches the "
                "same object. Manual "
                "acquire()/release() (or passing the lock elsewhere) can hold it across "
                "an await without presenting a block for this gate to check, which makes "
                "the lock contendable and reopens the torn sid/TTS-flag state (#2619)"))
        for block in ast.walk(tree):
            if not isinstance(block, ast.AsyncWith):
                continue
            if not any(is_session_lock(item) for item in block.items):
                continue
            blocks_seen += 1
            # ``async with self.lock, other:`` enters ``other`` while the
            # session lock is already held, and on the way out awaits its
            # ``__aexit__`` before the lock is released — two suspensions the
            # body scan below never sees, because neither is in the body. So
            # the session lock has to be the LAST item. An item BEFORE it is
            # fine: the lock is not yet held on the way in, and is already
            # released on the way out.
            lock_index = next(
                i for i, item in enumerate(block.items) if is_session_lock(item)
            )
            for trailing in block.items[lock_index + 1:]:
                violations.append(Violation(
                    path, trailing.context_expr.lineno, trailing.context_expr.col_offset,
                    "CORE_LOCK_NO_AWAIT",
                    "context manager entered after self.lock in the same 'async with' — "
                    "its __aenter__ (and its __aexit__ on the way out) run while the "
                    "session lock is held, which is a suspension the lock must never "
                    "span (#2619). Put it in its own block before the lock, or make "
                    "self.lock the last item"))
            for node in _statements_in_critical_section(block.body):
                kind = suspension_kind(node)
                if kind is None:
                    continue
                violations.append(Violation(
                    path, getattr(node, "lineno", block.lineno),
                    getattr(node, "col_offset", block.col_offset),
                    "CORE_LOCK_NO_AWAIT",
                    f"'{kind}' inside the 'async with self.lock' block opened at line "
                    f"{block.lineno} — the session lock must never be held across a "
                    f"suspension. While it is not, the lock is never contended, so every "
                    f"acquire takes the fast path, no acquire is a cancellation point, and "
                    f"the sid+TTS-flag writes inside these blocks are atomic (#2619). One "
                    f"suspension here makes the lock contendable and reopens that torn "
                    f"state at every one of these blocks, not just this one. Do the awaited "
                    f"work before or after the block"))
                break  # one report per block; the first suspension is the defect
    if not blocks_seen:
        violations.append(Violation(
            # Anchored on the package initializer, not on whichever module
            # happens to hold a rotation today: this violation fires exactly
            # when the package changed shape, so naming a module that may
            # itself have been renamed or deleted would render as a bare
            # absolute path. ``run()`` hard-fails if this file is missing.
            core_dir / "__init__.py", 1, 0, "CORE_LOCK_NO_AWAIT",
            "no 'async with self.lock' block left in main_logic/core — either the session "
            "lock moved or the rotations stopped taking it; this gate is now vacuous, so "
            "update it instead of letting it go dark"))
    # The no-suspension argument assumes a plain asyncio.Lock: a reentrant or
    # threading primitive would make 'never observed held' false for reasons
    # this AST cannot see. Checking that SOME binding is asyncio.Lock() is not
    # enough — a later ``self.lock = OtherLock()`` would leave the first
    # binding intact for the check to find while the attribute the code
    # actually takes is the second. So exactly one binding is required, and
    # that one is the one validated.
    if len(manager_bindings) != 1:
        found = ", ".join(
            f"line {getattr(target, 'lineno', '?')}" for target, _ in manager_bindings
        ) or "none"
        violations.append(Violation(
            manager_path,
            getattr(manager_bindings[0][0], "lineno", 1) if manager_bindings else 1, 0,
            "CORE_LOCK_NO_AWAIT",
            f"self.lock must be bound exactly once in manager.py, found "
            f"{len(manager_bindings)} ({found}) — with more than one binding the "
            f"primitive check cannot tell which object the package actually takes, and "
            f"a rebind to a suspending lock would pass while an earlier asyncio.Lock() "
            f"line satisfies the check"))
    else:
        target, value = manager_bindings[0]
        line = getattr(target, "lineno", 1)
        if not (
            isinstance(value, ast.Call)
            and dotted_node_path(value.func) == "asyncio.Lock"
        ):
            violations.append(Violation(
                manager_path, line, 0, "CORE_LOCK_NO_AWAIT",
                "self.lock is no longer bound to asyncio.Lock() in manager.py — the "
                "atomicity this gate protects rests on the uncontended-acquire fast path "
                "of that exact primitive; re-derive the contract before swapping it"))
        else:
            # ``asyncio.Lock`` is matched by SPELLING, so the name has to mean
            # the standard library. Rather than enumerate the ways it could
            # mean something else (``import custom_locks as asyncio``, a plain
            # ``asyncio = custom_locks``, a parameter named ``asyncio``, a
            # loop target, a ``with ... as`` …), require the name to have
            # exactly one binding in manager.py and require that binding to be
            # a plain ``import asyncio``. That is closed under binding syntax
            # instead of chasing forms one at a time.
            manager_tree = parse(manager_path)
            bindings = _name_binding_sites(manager_tree, "asyncio")

            def binds_the_stdlib_package(node: ast.AST) -> bool:
                # ``import asyncio`` and ``import asyncio.subprocess`` both
                # bind the top-level name to the same stdlib package, so both
                # are the guarantee this check wants; anything with an
                # ``asname`` or any other syntax is not. Judged per ALIAS —
                # judging the containing statement let ``import asyncio,
                # vendor as asyncio`` pass on the strength of its first alias
                # while Python bound the name to the second.
                return (
                    isinstance(node, ast.alias)
                    and node.asname is None
                    and (node.name == "asyncio" or node.name.startswith("asyncio."))
                )

            plain_import = bool(bindings) and all(
                binds_the_stdlib_package(node) for node in bindings
            )
            if not plain_import:
                where = ", ".join(
                    f"line {getattr(b, 'lineno', '?')}" for b in bindings
                ) or "nowhere"
                violations.append(Violation(
                    manager_path,
                    getattr(bindings[0], "lineno", 1) if bindings else 1, 0,
                    "CORE_LOCK_NO_AWAIT",
                    f"the name 'asyncio' must be bound in manager.py only by a plain "
                    f"'import asyncio' — found {len(bindings)} binding(s) "
                    f"({where}). The primitive check matches the spelling "
                    f"'asyncio.Lock()', so any other binding of that name (alias import, "
                    f"wildcard import, assignment, parameter, loop target, with/except "
                    f"capture …) lets a lock with different suspension semantics pass as "
                    f"the standard-library one (#2619)"))
    return violations


def _assignment_target_names(target: ast.AST) -> set[str]:
    """Return names directly bound by one assignment target."""

    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _assignment_target_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return {
            name
            for element in target.elts
            for name in _assignment_target_names(element)
        }
    return set()


def _match_pattern_binding_names(pattern: ast.pattern) -> set[str]:
    """Return names captured by one structural pattern."""

    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            names.add(node.rest)
    return names


def _class_global_binding_sites(node: ast.ClassDef) -> list[tuple[str, ast.AST]]:
    """Return bindings redirected to module scope by class-body ``global``."""

    global_names: set[str] = set()
    nested_classes: list[ast.ClassDef] = []
    stack: list[ast.AST] = list(reversed(node.body))
    while stack:
        child = stack.pop()
        if isinstance(child, ast.Global):
            global_names.update(child.names)
            continue
        if isinstance(child, ast.ClassDef):
            nested_classes.append(child)
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(child))))

    synthetic = ast.Module(body=node.body, type_ignores=[])
    sites = [
        (name, binding_node)
        for name, binding_node in _module_scope_binding_sites(
            synthetic,
            include_class_globals=False,
        )
        if name in global_names
    ]
    for nested_class in nested_classes:
        sites.extend(_class_global_binding_sites(nested_class))
    return sites


def _module_scope_binding_sites(
    tree: ast.Module,
    *,
    include_class_globals: bool = True,
) -> list[tuple[str, ast.AST]]:
    """Return lexical module bindings without descending into local scopes.

    This intentionally models ordinary Python binding syntax, not reflective
    mutation through globals(), exec(), or arbitrary object state.
    """

    sites: list[tuple[str, ast.AST]] = []
    stack: list[ast.AST] = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        names: set[str] = set()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sites.append((node.name, node))
            evaluated_expressions: list[ast.AST] = [
                *node.decorator_list,
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default is not None),
            ]
            stack.extend(reversed(evaluated_expressions))
            continue
        if isinstance(node, ast.ClassDef):
            sites.append((node.name, node))
            if include_class_globals:
                sites.extend(_class_global_binding_sites(node))
            evaluated_expressions = [
                *node.decorator_list,
                *node.bases,
                *(keyword.value for keyword in node.keywords),
            ]
            stack.extend(reversed(evaluated_expressions))
            continue
        if isinstance(node, ast.Lambda):
            evaluated_expressions = [
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default is not None),
            ]
            stack.extend(reversed(evaluated_expressions))
            continue
        if isinstance(node, ast.Assign):
            names = {
                name
                for target in node.targets
                for name in _assignment_target_names(target)
            }
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names = _assignment_target_names(node.target)
        elif isinstance(node, ast.AugAssign):
            names = _assignment_target_names(node.target)
        elif isinstance(node, ast.NamedExpr):
            names = _assignment_target_names(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names = _assignment_target_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            names = {
                name
                for item in node.items
                if item.optional_vars is not None
                for name in _assignment_target_names(item.optional_vars)
            }
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            names = {node.name}
        elif isinstance(node, ast.match_case):
            names = _match_pattern_binding_names(node.pattern)
        sites.extend((name, node) for name in names)
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return sites


def _voice_identity_call_scope_metadata(
    tree: ast.Module,
    alias_paths: dict[str, str],
    module_import_names: set[str],
) -> tuple[set[int], set[int]]:
    """Return shadowed import calls and statically NumPy-backed dump calls."""

    def scope_bindings(body: list[ast.stmt]) -> tuple[set[str], set[str]]:
        synthetic = ast.Module(body=body, type_ignores=[])
        bound = {
            name
            for name, _ in _module_scope_binding_sites(
                synthetic,
                include_class_globals=False,
            )
        }
        global_names: set[str] = set()
        nonlocal_names: set[str] = set()
        stack: list[ast.AST] = list(reversed(body))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Global):
                global_names.update(node.names)
                continue
            if isinstance(node, ast.Nonlocal):
                nonlocal_names.update(node.names)
                continue
            if isinstance(node, ast.Import):
                bound.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
                continue
            if isinstance(node, ast.ImportFrom):
                bound.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name != "*"
                )
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.extend(reversed(list(ast.iter_child_nodes(node))))
        return bound - global_names - nonlocal_names, global_names

    def argument_names(arguments: ast.arguments) -> set[str]:
        names = {
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            )
        }
        if arguments.vararg is not None:
            names.add(arguments.vararg.arg)
        if arguments.kwarg is not None:
            names.add(arguments.kwarg.arg)
        return names

    def numpy_result_bindings(
        body: list[ast.stmt],
        shadowed_imports: set[str],
    ) -> set[str]:
        bindings: set[str] = set()
        stack: list[ast.AST] = list(reversed(body))
        while stack:
            node = stack.pop()
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            elif isinstance(node, ast.NamedExpr):
                targets = [node.target]
                value = node.value
            if isinstance(value, ast.Call):
                chain = dotted_node_path(value.func)
                if chain is not None and chain.split(".", 1)[0] not in shadowed_imports:
                    resolved = resolve_chain(chain, alias_paths) or chain
                    if resolved.startswith("numpy."):
                        bindings.update(
                            name
                            for target in targets
                            for name in _assignment_target_names(target)
                        )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.extend(reversed(list(ast.iter_child_nodes(node))))
        return bindings

    protected_call_names = module_import_names | {
        "__import__",
        "eval",
        "exec",
        "open",
    }

    def statement_binding_names(
        statement: ast.stmt,
        *,
        include_class_globals: bool,
    ) -> set[str]:
        synthetic = ast.Module(body=[statement], type_ignores=[])
        return {
            name
            for name, _ in _module_scope_binding_sites(
                synthetic,
                include_class_globals=include_class_globals,
            )
        }

    class ScopeVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            module_shadowed: set[str] = set()
            module_numpy: set[str] = set()
            self.shadowed_stack: list[set[str]] = [module_shadowed]
            self.numpy_stack: list[set[str]] = [module_numpy]
            self.lexical_shadowed_stack: list[set[str]] = [module_shadowed]
            self.lexical_numpy_stack: list[set[str]] = [module_numpy]
            self.shadowed_call_ids: set[int] = set()
            self.numpy_dump_call_ids: set[int] = set()

        def _visit_statement_sequence(
            self,
            body: list[ast.stmt],
            *,
            global_names: set[str],
            include_class_globals: bool,
        ) -> None:
            for statement in body:
                self.visit(statement)
                bound_names = statement_binding_names(
                    statement,
                    include_class_globals=include_class_globals,
                ) - global_names
                numpy_bindings = numpy_result_bindings(
                    [statement],
                    self.shadowed_stack[-1],
                ) - global_names
                self.numpy_stack[-1].difference_update(bound_names)
                self.numpy_stack[-1].update(numpy_bindings)
                self.shadowed_stack[-1].update(
                    bound_names & protected_call_names
                )

        def visit_Module(self, node: ast.Module) -> None:  # noqa: N802
            self._visit_statement_sequence(
                node.body,
                global_names=set(),
                include_class_globals=True,
            )

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
            chain = dotted_node_path(node.func)
            if (
                chain is not None
                and chain.split(".", 1)[0] in self.shadowed_stack[-1]
            ):
                self.shadowed_call_ids.add(id(node))
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "dump"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in self.numpy_stack[-1]
            ):
                self.numpy_dump_call_ids.add(id(node))
            self.generic_visit(node)

        def _visit_function(
            self,
            node: ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> None:
            outer_expressions: list[ast.AST] = [
                *node.decorator_list,
                *node.args.defaults,
                *(default for default in node.args.kw_defaults if default is not None),
            ]
            for expression in outer_expressions:
                self.visit(expression)
            local_names, global_names = scope_bindings(node.body)
            local_names.update(argument_names(node.args))
            shadowed = (
                self.lexical_shadowed_stack[-1] - global_names
            ) | (local_names & protected_call_names)
            numpy_names = self.lexical_numpy_stack[-1] - local_names
            self.shadowed_stack.append(shadowed)
            self.numpy_stack.append(numpy_names)
            self.lexical_shadowed_stack.append(shadowed)
            self.lexical_numpy_stack.append(numpy_names)
            self._visit_statement_sequence(
                node.body,
                global_names=global_names,
                include_class_globals=False,
            )
            self.lexical_numpy_stack.pop()
            self.lexical_shadowed_stack.pop()
            self.numpy_stack.pop()
            self.shadowed_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._visit_function(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            outer_expressions: list[ast.AST] = [
                *node.decorator_list,
                *node.bases,
                *(keyword.value for keyword in node.keywords),
            ]
            for expression in outer_expressions:
                self.visit(expression)
            _, global_names = scope_bindings(node.body)
            shadowed = self.shadowed_stack[-1] - global_names
            numpy_names = set(self.numpy_stack[-1])
            self.shadowed_stack.append(shadowed)
            self.numpy_stack.append(numpy_names)
            self.lexical_shadowed_stack.append(self.lexical_shadowed_stack[-1])
            self.lexical_numpy_stack.append(self.lexical_numpy_stack[-1])
            self._visit_statement_sequence(
                node.body,
                global_names=global_names,
                include_class_globals=False,
            )
            self.lexical_numpy_stack.pop()
            self.lexical_shadowed_stack.pop()
            self.numpy_stack.pop()
            self.shadowed_stack.pop()

        def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.visit(default)
            local_names = argument_names(node.args)
            shadowed = self.lexical_shadowed_stack[-1] | (
                local_names & protected_call_names
            )
            numpy_names = self.lexical_numpy_stack[-1] - local_names
            self.shadowed_stack.append(shadowed)
            self.numpy_stack.append(numpy_names)
            self.lexical_shadowed_stack.append(shadowed)
            self.lexical_numpy_stack.append(numpy_names)
            self.visit(node.body)
            self.lexical_numpy_stack.pop()
            self.lexical_shadowed_stack.pop()
            self.numpy_stack.pop()
            self.shadowed_stack.pop()

        def _visit_comprehension(
            self,
            generators: list[ast.comprehension],
            result_nodes: tuple[ast.AST, ...],
        ) -> None:
            if not generators:
                for result in result_nodes:
                    self.visit(result)
                return
            self.visit(generators[0].iter)
            shadowed = set(self.lexical_shadowed_stack[-1])
            numpy_names = set(self.lexical_numpy_stack[-1])
            self.shadowed_stack.append(shadowed)
            self.numpy_stack.append(numpy_names)
            self.lexical_shadowed_stack.append(shadowed)
            self.lexical_numpy_stack.append(numpy_names)
            for index, generator in enumerate(generators):
                if index:
                    self.visit(generator.iter)
                target_names = _assignment_target_names(generator.target)
                self.shadowed_stack[-1].update(
                    target_names & protected_call_names
                )
                self.numpy_stack[-1].difference_update(target_names)
                for condition in generator.ifs:
                    self.visit(condition)
            for result in result_nodes:
                self.visit(result)
            self.lexical_numpy_stack.pop()
            self.lexical_shadowed_stack.pop()
            self.numpy_stack.pop()
            self.shadowed_stack.pop()

        def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802
            self._visit_comprehension(node.generators, (node.elt,))

        def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802
            self._visit_comprehension(node.generators, (node.elt,))

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
            self._visit_comprehension(node.generators, (node.elt,))

        def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802
            self._visit_comprehension(node.generators, (node.key, node.value))

    visitor = ScopeVisitor()
    visitor.visit(tree)
    return visitor.shadowed_call_ids, visitor.numpy_dump_call_ids


def check_voice_identity_contracts(root: Path) -> list[Violation]:
    """Cooperatively lint the provider-neutral voice-identity boundary.

    This check covers module-scope imports and direct call sites. It is an
    architecture guard for trusted repository code, not a Python sandbox or a
    proof against reflection and intentionally obscured runtime behavior.
    """

    package_dir = root / "main_logic" / "voice_identity"
    violations: list[Violation] = []
    if not package_dir.exists():
        violations.append(Violation(
            package_dir,
            1,
            0,
            "VOICE_IDENTITY_LAYERING",
            "required voice_identity package is missing",
        ))

    init_path = package_dir / "__init__.py"
    domain_paths = tuple(
        package_dir / name
        for name in ("contracts.py", "reference.py", "profile.py")
    )
    for required in (init_path, *domain_paths):
        if not required.exists():
            violations.append(Violation(
                required,
                1,
                0,
                "VOICE_IDENTITY_LAYERING",
                "required voice_identity domain file is missing",
            ))

    for packaged_path in sorted(
        candidate
        for candidate in package_dir.rglob("*")
        if candidate.is_file()
    ):
        relative_path = packaged_path.relative_to(package_dir)
        if packaged_path.suffix == ".py" or "__pycache__" in relative_path.parts:
            continue
        violations.append(Violation(
            packaged_path,
            1,
            0,
            "VOICE_IDENTITY_LAYERING",
            "voice_identity domain must not contain packaged assets; "
            f"found {relative_path.as_posix()}",
        ))

    if init_path.exists():
        init_tree = parse(init_path)
        docstring_only = (
            len(init_tree.body) == 1
            and isinstance(init_tree.body[0], ast.Expr)
            and isinstance(init_tree.body[0].value, ast.Constant)
            and isinstance(init_tree.body[0].value.value, str)
        )
        if not docstring_only:
            offender = init_tree.body[1] if len(init_tree.body) > 1 else (
                init_tree.body[0] if init_tree.body else None
            )
            violations.append(Violation(
                init_path,
                getattr(offender, "lineno", 1),
                getattr(offender, "col_offset", 0),
                "VOICE_IDENTITY_LAYERING",
                "voice_identity/__init__.py may contain only a package docstring",
            ))

    forbidden_domain_prefixes = (
        "main_logic.asr_client",
        "main_logic.core",
        "main_logic.voice_turn",
        "main_routers",
        "app",
        "onnxruntime",
        "keyring",
        "cryptography",
    )
    allowed_import_prefixes = {
        package_dir / "contracts.py": (
            "__future__",
            "dataclasses",
        ),
        package_dir / "reference.py": (
            "__future__",
            "math",
            "threading",
            "numpy",
            "main_logic.voice_identity.contracts",
        ),
        package_dir / "profile.py": (
            "__future__",
            "threading",
            "main_logic.voice_identity.contracts",
            "main_logic.voice_identity.reference",
        ),
    }
    direct_dynamic_calls = {
        "eval",
        "exec",
        "__import__",
        "builtins.eval",
        "builtins.exec",
        "builtins.__import__",
        "importlib.import_module",
    }
    direct_file_io_calls = {
        "open",
        "builtins.open",
        "numpy.fromfile",
        "numpy.fromregex",
        "numpy.genfromtxt",
        "numpy.lib.format.open_memmap",
        "numpy.load",
        "numpy.loadtxt",
        "numpy.memmap",
        "numpy.ndarray.dump",
        "numpy.recfromtxt",
        "numpy.save",
        "numpy.savetxt",
        "numpy.savez",
        "numpy.savez_compressed",
    }
    direct_native_loading_calls = {
        "numpy.ctypeslib.load_library",
    }
    direct_native_compilation_calls = {
        "numpy.f2py.compile",
    }
    allowed_numpy_calls = {
        "numpy.all",
        "numpy.array",
        "numpy.asarray",
        "numpy.ascontiguousarray",
        "numpy.copy",
        "numpy.divide",
        "numpy.empty",
        "numpy.errstate",
        "numpy.iscomplexobj",
        "numpy.isfinite",
        "numpy.linalg.norm",
        "numpy.ones",
        "numpy.zeros",
    }
    allowed_threading_calls = {
        "threading.Lock",
    }
    direct_file_io_methods = {
        "tofile",
    }

    scanned_paths = sorted(
        path
        for path in package_dir.rglob("*.py")
        if path != init_path
    )
    for path in scanned_paths:
        tree = parse(path)
        pkg = ".".join(path.relative_to(root).parts[:-1])
        alias_paths = module_alias_paths(tree, pkg)
        module_scope_imports = {
            id(node)
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        }
        module_scope_import_bindings: set[str] = set()
        for import_node in tree.body:
            if isinstance(import_node, ast.Import):
                bound_names = (
                    alias.asname or alias.name.split(".", 1)[0]
                    for alias in import_node.names
                )
            elif (
                isinstance(import_node, ast.ImportFrom)
                and import_node.module != "__future__"
            ):
                bound_names = (
                    alias.asname or alias.name
                    for alias in import_node.names
                    if alias.name != "*"
                )
            else:
                continue
            for bound_name in bound_names:
                if bound_name in module_scope_import_bindings:
                    violations.append(Violation(
                        path,
                        import_node.lineno,
                        import_node.col_offset,
                        "VOICE_IDENTITY_LAYERING",
                        "voice_identity module-scope import bindings must not "
                        f"be rebound; found {bound_name}",
                    ))
                else:
                    module_scope_import_bindings.add(bound_name)
        reported_rebindings: set[tuple[str, int, int]] = set()
        for bound_name, binding_node in _module_scope_binding_sites(tree):
            if bound_name not in module_scope_import_bindings:
                continue
            location = (
                bound_name,
                getattr(binding_node, "lineno", 1),
                getattr(binding_node, "col_offset", 0),
            )
            if location in reported_rebindings:
                continue
            reported_rebindings.add(location)
            violations.append(Violation(
                path,
                location[1],
                location[2],
                "VOICE_IDENTITY_LAYERING",
                "voice_identity module-scope import bindings must not "
                f"be rebound; found {bound_name}",
            ))
        shadowed_import_call_ids, numpy_dump_call_ids = (
            _voice_identity_call_scope_metadata(
                tree,
                alias_paths,
                module_scope_import_bindings,
            )
        )
        allowed = allowed_import_prefixes.get(path)
        if allowed is None:
            violations.append(Violation(
                path,
                1,
                0,
                "VOICE_IDENTITY_LAYERING",
                "voice_identity module is missing an explicit dependency allowlist",
            ))
            allowed = ()
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Import, ast.ImportFrom))
                and id(node) not in module_scope_imports
            ):
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    "voice_identity imports must be declared at module scope",
                ))
            if (
                isinstance(node, ast.ImportFrom)
                and any(alias.name == "*" for alias in node.names)
            ):
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    "voice_identity domain must not use wildcard imports",
                ))
            imported_paths = _imported_paths(node, pkg)
            if imported_paths:
                allowed_paths = {
                    imported
                    for imported in imported_paths
                    if any(
                        imported == prefix
                        or imported.startswith(f"{prefix}.")
                        for prefix in allowed
                    )
                }
                if (
                    isinstance(node, ast.ImportFrom)
                    and any(
                        imported in allowed_paths
                        for imported in imported_paths[1:]
                    )
                ):
                    # ``from . import contracts`` imports the package anchor
                    # alongside the approved member. That structural base is
                    # allowed only for this exact ImportFrom node; a broad
                    # standalone ``import main_logic`` remains forbidden.
                    allowed_paths.add(imported_paths[0])
                disallowed = next(
                    (
                        imported
                        for imported in dict.fromkeys(imported_paths)
                        if imported not in allowed_paths
                    ),
                    None,
                )
                if disallowed is not None:
                    forbidden = next(
                        (
                            prefix
                            for prefix in forbidden_domain_prefixes
                            if disallowed == prefix
                            or disallowed.startswith(f"{prefix}.")
                        ),
                        None,
                    )
                    message = (
                        f"voice_identity domain must not import {forbidden}"
                        if forbidden is not None
                        else "voice_identity domain may only import approved "
                        f"in-memory dependencies; found {disallowed}"
                    )
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "VOICE_IDENTITY_LAYERING",
                        message,
                    ))

            if not isinstance(node, ast.Call):
                continue
            direct_file_io_method = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in direct_file_io_methods
            )
            direct_numpy_ndarray_dump = False
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "dump"
                and isinstance(node.func.value, ast.Call)
                and id(node.func.value) not in shadowed_import_call_ids
            ):
                receiver_chain = dotted_node_path(node.func.value.func)
                if receiver_chain is not None:
                    resolved_receiver = (
                        resolve_chain(receiver_chain, alias_paths) or receiver_chain
                    )
                    direct_numpy_ndarray_dump = resolved_receiver.startswith("numpy.")
            direct_bound_numpy_dump = id(node) in numpy_dump_call_ids
            if direct_numpy_ndarray_dump or direct_bound_numpy_dump:
                dump_path = (
                    "numpy.ndarray.dump"
                    if direct_numpy_ndarray_dump
                    else dotted_node_path(node.func) or ".dump"
                )
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    "voice_identity domain must not perform file I/O via "
                    f"{dump_path}",
                ))
                continue
            chain = dotted_node_path(node.func)
            if chain is None:
                if direct_file_io_method:
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "VOICE_IDENTITY_LAYERING",
                        "voice_identity domain must not perform file I/O via "
                        f".{node.func.attr}",
                    ))
                continue
            if (
                id(node) in shadowed_import_call_ids
                and isinstance(node.func, ast.Name)
            ):
                continue
            resolved = (
                chain
                if id(node) in shadowed_import_call_ids
                else resolve_chain(chain, alias_paths) or chain
            )
            if resolved in direct_dynamic_calls:
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    f"voice_identity domain must not call {resolved}",
                ))
            elif resolved in direct_file_io_calls or direct_file_io_method:
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    f"voice_identity domain must not perform file I/O via {resolved}",
                ))
            elif resolved in direct_native_loading_calls:
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    "voice_identity domain must not load a native library via "
                    f"{resolved}",
                ))
            elif resolved in direct_native_compilation_calls:
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    f"voice_identity domain must not compile native code via {resolved}",
                ))
            elif (
                resolved.startswith("numpy.")
                and resolved not in allowed_numpy_calls
            ):
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    "voice_identity domain may only call approved in-memory "
                    f"NumPy APIs; found {resolved}",
                ))
            elif (
                resolved.startswith("threading.")
                and resolved not in allowed_threading_calls
            ):
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "VOICE_IDENTITY_LAYERING",
                    "voice_identity domain may only call threading.Lock; "
                    f"found {resolved}",
                ))

    return violations


def run(root: Path) -> list[Violation]:
    core_dir = root / "main_logic" / "core"
    tests_dir = root / "tests"
    init_path = core_dir / "__init__.py"
    manager_path = core_dir / "manager.py"
    for required in (core_dir, tests_dir, init_path, manager_path):
        if not required.exists():
            print(f"error: expected path missing: {required} — the core package layout moved; "
                  f"update scripts/check_core_contracts.py instead of letting the gate go dark.",
                  file=sys.stderr)
            sys.exit(2)

    violations: list[Violation] = []
    violations.extend(check_voice_identity_contracts(root))
    violations.extend(check_fail_closed_chokepoint(core_dir))
    violations.extend(check_session_lock_atomicity(core_dir, manager_path))
    init_tree = parse(init_path)
    facade_names = facade_top_level_names(init_tree)
    facade_owners = facade_owner_modules(init_tree)

    # -- the core package must stay flat: a subpackage would define classes the
    #    *.py discovery below never scans, so its mixins/state escape every
    #    shape/routing check. Reject any subdirectory carrying Python modules.
    for sub in sorted(p for p in core_dir.iterdir() if p.is_dir() and p.name != "__pycache__"):
        # rglob, not glob: a nested tree (helpers/nested/mod.py) is still
        # importable as main_logic.core.helpers.nested.mod and must be rejected.
        if any(q for q in sub.rglob("*.py") if "__pycache__" not in q.parts):
            violations.append(Violation(sub / "__init__.py", 1, 0, "CORE_MIXIN_SHAPE",
                                        f"core subpackage '{sub.name}/' is not allowed — the core package is "
                                        f"flat so every module is covered by the contract checks; keep new "
                                        f"code in a top-level core/*.py mixin or owner submodule"))

    # -- discover mixin modules (any core/*.py defining a single *Mixin class)
    mixin_files: dict[Path, ast.ClassDef] = {}
    manager_class = None
    for path in sorted(core_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = parse(path)
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        if path == manager_path:
            manager_class = classes[0] if len(classes) == 1 and classes[0].name == "LLMSessionManager" else None
            if manager_class is None:
                violations.append(Violation(path, 1, 0, "CORE_MANAGER_SHAPE",
                                            "manager.py must define exactly one class: LLMSessionManager"))
            else:
                # A class decorator or metaclass runs at class creation and can
                # inject/replace methods after the AST is counted — invisible to
                # every body/base/import check below.
                if manager_class.decorator_list:
                    d = manager_class.decorator_list[0]
                    violations.append(Violation(path, d.lineno, d.col_offset, "CORE_MANAGER_SHAPE",
                                                "LLMSessionManager must not be decorated — a class decorator "
                                                "can inject methods/state the shape checks cannot see"))
                if manager_class.keywords:  # metaclass= or other class kwargs
                    k = manager_class.keywords[0]
                    violations.append(Violation(path, k.value.lineno, k.value.col_offset, "CORE_MANAGER_SHAPE",
                                                f"LLMSessionManager must not set class keyword "
                                                f"'{k.arg or '**kwargs'}' (metaclass/kwargs) — it can rewrite "
                                                f"the class outside the mixin contract"))
            for i, node in enumerate(tree.body):
                if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    continue  # module docstring
                if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef)):
                    continue
                violations.append(Violation(path, node.lineno, node.col_offset, "CORE_MANAGER_SHAPE",
                                            f"manager.py top level allows only docstring/imports/class, "
                                            f"found {type(node).__name__}"))
            continue
        mixins = [c for c in classes if c.name.endswith("Mixin")]
        if mixins:
            expected_classes = {
                mixins[0].name,
                *MIXIN_SUPPORT_CLASSES.get(path.stem, set()),
            }
            actual_classes = {klass.name for klass in classes}
            if len(mixins) != 1 or actual_classes != expected_classes:
                violations.append(Violation(
                    path,
                    classes[0].lineno if classes else 1,
                    classes[0].col_offset if classes else 0,
                    "CORE_MIXIN_SHAPE",
                    f"{path.name} classes must be exactly {sorted(expected_classes)} "
                    f"(found {sorted(actual_classes)})",
                ))
            # A base on a mixin drags inherited methods/state into
            # LLMSessionManager's MRO uncounted by CORE_MIXIN_DISJOINT/BASES.
            if mixins[0].bases or mixins[0].keywords:
                b = (mixins[0].bases or [kw.value for kw in mixins[0].keywords])[0]
                violations.append(Violation(path, b.lineno, b.col_offset, "CORE_MIXIN_SHAPE",
                                            f"mixin class {mixins[0].name} must have an empty base list — a "
                                            f"base would smuggle behavior into LLMSessionManager's MRO "
                                            f"outside the mixin contract"))
            # A class decorator runs at class creation and can inject/replace
            # methods after the AST body is counted — invisible to DISJOINT and
            # the manager-base checks. Mixins must be plain, undecorated bags.
            if mixins[0].decorator_list:
                d = mixins[0].decorator_list[0]
                violations.append(Violation(path, d.lineno, d.col_offset, "CORE_MIXIN_SHAPE",
                                            f"mixin class {mixins[0].name} must not be decorated — a class "
                                            f"decorator can add methods/state into the MRO uncounted by the "
                                            f"other checks"))
            mixin_files[path] = mixins[0]
        elif path.stem not in OWNER_SUBMODULES:
            violations.append(Violation(path, 1, 0, "CORE_MIXIN_SHAPE",
                                        f"unknown core module '{path.name}': every core module must either "
                                        f"define one *Mixin class or be a registered owner submodule — add it "
                                        f"to OWNER_SUBMODULES in scripts/check_core_contracts.py if it is a "
                                        f"deliberate new owner module"))

    # -- CORE_MIXIN_SHAPE: top level and class body
    for path, klass in mixin_files.items():
        tree = parse(path)
        for i, node in enumerate(tree.body):
            if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # module docstring
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef)):
                continue
            violations.append(Violation(path, node.lineno, node.col_offset, "CORE_MIXIN_SHAPE",
                                        f"mixin module top level allows only docstring/imports/class, "
                                        f"found {type(node).__name__}"))
        for i, node in enumerate(klass.body):
            if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # class docstring
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "__init__":
                    violations.append(Violation(path, node.lineno, node.col_offset, "CORE_MIXIN_SHAPE",
                                                "mixins must not define __init__ — instance state has a "
                                                "single home in LLMSessionManager.__init__ (manager.py)"))
                # A mutable-container default (``def f(self, cache={})``) is
                # allocated once at import and SHARED across every instance —
                # the exact hidden mixin state this contract keeps out (same
                # rationale as the module-level/class-body state rejections).
                # GeneratorExp too: a generator default is a single-use iterator
                # allocated once at import and shared, so a second instance sees
                # it already exhausted — the same shared-state hazard.
                MUT = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp,
                       ast.SetComp, ast.GeneratorExp)
                for dflt in [d for d in node.args.defaults if d] + [d for d in node.args.kw_defaults if d]:
                    bad = next((s for s in ast.walk(dflt) if isinstance(s, MUT)), None)
                    if bad is not None:
                        violations.append(Violation(path, dflt.lineno, dflt.col_offset, "CORE_MIXIN_SHAPE",
                                                    f"mixin method '{node.name}' has a mutable default argument "
                                                    f"— it is allocated once at import and shared across all "
                                                    f"instances; use None and build inside the method"))
                continue
            violations.append(Violation(path, node.lineno, node.col_offset, "CORE_MIXIN_SHAPE",
                                        f"mixin class body allows only docstring/methods, "
                                        f"found {type(node).__name__} — state belongs in manager.__init__"))

    # -- ASR_LAYERING: keep microphone ingress, Core integration, and provider
    # runtime ownership on their explicit sides of the composition boundary.
    asr_bridge_path = core_dir / "asr_runtime.py"
    tts_path = core_dir / "tts_runtime.py"
    streaming_path = core_dir / "streaming.py"
    asr_client_dir = root / "main_logic" / "asr_client"
    asr_component_path = asr_client_dir / "runtime.py"
    asr_audio_path = asr_client_dir / "audio.py"
    asr_registry_path = asr_client_dir / "_registry_meta.py"
    voice_turn_dir = root / "main_logic" / "voice_turn"
    voice_input_path = voice_turn_dir / "audio_input.py"
    transcript_registry_dir = root / "main_logic" / "voice_input"
    endpointing_dir = asr_client_dir / "endpointing"
    speaker_shadow_dir = asr_client_dir / "speaker_shadow"
    speaker_shadow_init = speaker_shadow_dir / "__init__.py"
    speaker_shadow_contracts = speaker_shadow_dir / "contracts.py"
    speaker_shadow_runtime = speaker_shadow_dir / "runtime.py"
    for required, violation_code in (
        (asr_bridge_path, "ASR_LAYERING"),
        (tts_path, "ASR_LAYERING"),
        (streaming_path, "ASR_LAYERING"),
        (asr_client_dir, "ASR_LAYERING"),
        (asr_component_path, "ASR_LAYERING"),
        (asr_audio_path, "ASR_LAYERING"),
        (asr_registry_path, "ASR_LAYERING"),
        (speaker_shadow_dir, "ASR_LAYERING"),
        (speaker_shadow_init, "ASR_LAYERING"),
        (speaker_shadow_contracts, "ASR_LAYERING"),
        (speaker_shadow_runtime, "ASR_LAYERING"),
        (voice_input_path, "ASR_LAYERING"),
        (transcript_registry_dir, "VOICE_INPUT_LAYERING"),
    ):
        if not required.exists():
            violations.append(Violation(
                required,
                1,
                0,
                violation_code,
                f"required layering path is missing ({violation_code})",
            ))

    if tts_path.exists():
        tts_tree = parse(tts_path)
        tts_pkg = ".".join(tts_path.relative_to(root).parts[:-1])
        tts_alias_paths = module_alias_paths(tts_tree, tts_pkg)
        forbidden_ingress_methods = {
            "_ensure_audio_stream_worker",
            "_clear_audio_stream_queue",
            "_cancel_audio_stream_worker",
            "_enqueue_audio_stream_data",
            "_audio_stream_worker_loop",
        }
        for node in ast.walk(tts_tree):
            if any(
                imported == "main_logic.asr_client"
                or imported.startswith("main_logic.asr_client.")
                for imported in _imported_paths(node, tts_pkg, tts_alias_paths)
            ):
                violations.append(Violation(
                    tts_path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    "tts_runtime.py must not import main_logic.asr_client",
                ))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in forbidden_ingress_methods
            ):
                violations.append(Violation(
                    tts_path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    f"microphone ingress method '{node.name}' belongs in core/asr_runtime.py",
                ))
        violations.extend(_dynamic_import_violations(
            tts_path, tts_tree, tts_alias_paths,
            "main_logic.asr_client", "tts_runtime.py",
        ))

    if asr_client_dir.exists():
        for path in sorted(asr_client_dir.rglob("*.py")):
            tree = parse(path)
            pkg = ".".join(path.relative_to(root).parts[:-1])
            alias_paths = module_alias_paths(tree, pkg)
            for node in ast.walk(tree):
                for module in _imported_paths(node, pkg, alias_paths):
                    forbidden = next(
                        (
                            prefix
                            for prefix in (
                                "main_logic.core",
                                "main_logic.voice_input",
                            )
                            if module == prefix
                            or module.startswith(f"{prefix}.")
                        ),
                        None,
                    )
                    if forbidden is not None:
                        violations.append(Violation(
                            path,
                            node.lineno,
                            node.col_offset,
                            "ASR_LAYERING",
                            f"asr_client must not import {forbidden}",
                        ))
            violations.extend(_dynamic_import_violations(
                path, tree, alias_paths,
                ("main_logic.core", "main_logic.voice_input"),
                "asr_client",
                report_generic=not path.is_relative_to(endpointing_dir),
            ))

    if transcript_registry_dir.exists():
        allowed_dependency_prefixes = (
            "main_logic.voice_input",
            "main_logic.voice_turn.contracts",
            "utils.game_route_state",
        )
        # Resolve first-party roots from the repository instead of maintaining
        # a narrow allowlist. Any importable sibling package/module (plugin,
        # config, scripts, or a future root) must pass the same frozen
        # dependency allowlist rather than silently bypassing the gate.
        guarded_roots = tuple(sorted({
            entry.name if entry.is_dir() else entry.stem
            for entry in root.iterdir()
            if (
                entry.is_dir()
                and entry.name.isidentifier()
            ) or (
                entry.is_file()
                and entry.suffix == ".py"
                and entry.stem.isidentifier()
            )
        }))
        for path in sorted(transcript_registry_dir.rglob("*.py")):
            tree = parse(path)
            pkg = ".".join(path.relative_to(root).parts[:-1])
            alias_paths = module_alias_paths(tree, pkg)
            dynamic_alias_paths = {
                **alias_paths,
                **_importlib_alias_paths(tree),
            }
            for node in ast.walk(tree):
                for module in _imported_paths(node, pkg, alias_paths):
                    if not any(
                        module == root_name
                        or module.startswith(f"{root_name}.")
                        for root_name in guarded_roots
                    ):
                        continue
                    if any(
                        module == allowed
                        or module.startswith(f"{allowed}.")
                        for allowed in allowed_dependency_prefixes
                    ):
                        continue
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "VOICE_INPUT_LAYERING",
                        "voice_input may depend only on its own package, "
                        "voice_turn.contracts, and utils.game_route_state "
                        f"(found {module})",
                    ))
                targets, dynamic = _dynamic_import_target(
                    node,
                    dynamic_alias_paths,
                )
                if dynamic:
                    detail = (
                        ", ".join(targets)
                        if targets is not None
                        else "a non-literal module name"
                    )
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "VOICE_INPUT_LAYERING",
                        "dynamic imports are not allowed in voice_input "
                        f"(found {detail})",
                    ))
    if voice_turn_dir.exists():
        for path in sorted(voice_turn_dir.rglob("*.py")):
            tree = parse(path)
            pkg = ".".join(path.relative_to(root).parts[:-1])
            alias_paths = module_alias_paths(tree, pkg)
            for node in ast.walk(tree):
                if any(
                    module == "main_logic.asr_client"
                    or module.startswith("main_logic.asr_client.")
                    for module in _imported_paths(node, pkg, alias_paths)
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "voice_turn must not import main_logic.asr_client",
                    ))
            violations.extend(_dynamic_import_violations(
                path,
                tree,
                alias_paths,
                "main_logic.asr_client",
                "voice_turn",
            ))

    if core_dir.exists():
        for path in sorted(core_dir.rglob("*.py")):
            tree = parse(path)
            pkg = ".".join(path.relative_to(root).parts[:-1])
            alias_paths = module_alias_paths(tree, pkg)
            for node in ast.walk(tree):
                imported_paths = _imported_paths(node, pkg, alias_paths)
                for forbidden in (
                    "main_logic.asr_client.endpointing",
                    "main_logic.asr_client.speaker_shadow",
                ):
                    if any(
                        module == forbidden
                        or module.startswith(f"{forbidden}.")
                        for module in imported_paths
                    ):
                        violations.append(Violation(
                            path,
                            node.lineno,
                            node.col_offset,
                            "ASR_LAYERING",
                            f"Core must not import {forbidden}",
                        ))
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name == "SpeakerShadowFactory" for alias in node.names
                ):
                    imported_from = (
                        node.module
                        if node.level == 0
                        else _resolve_relative(pkg, node.level, node.module)
                    )
                    if imported_from != "main_logic.asr_client.runtime":
                        violations.append(Violation(
                            path,
                            node.lineno,
                            node.col_offset,
                            "ASR_LAYERING",
                            "Core must obtain SpeakerShadowFactory only from "
                            "main_logic.asr_client.runtime",
                        ))
            for index, forbidden in enumerate((
                "main_logic.asr_client.endpointing",
                "main_logic.asr_client.speaker_shadow",
            )):
                violations.extend(_dynamic_import_violations(
                    path,
                    tree,
                    alias_paths,
                    forbidden,
                    "Core",
                    report_generic=(index == 0),
                ))

    if endpointing_dir.exists():
        endpointing_init = endpointing_dir / "__init__.py"
        if endpointing_init.exists():
            endpointing_init_tree = parse(endpointing_init)
            doc_only = (
                len(endpointing_init_tree.body) == 1
                and isinstance(endpointing_init_tree.body[0], ast.Expr)
                and isinstance(endpointing_init_tree.body[0].value, ast.Constant)
                and isinstance(endpointing_init_tree.body[0].value.value, str)
            )
            if not doc_only:
                node = (
                    endpointing_init_tree.body[1]
                    if len(endpointing_init_tree.body) > 1
                    else (
                        endpointing_init_tree.body[0]
                        if endpointing_init_tree.body
                        else None
                    )
                )
                violations.append(Violation(
                    endpointing_init,
                    getattr(node, "lineno", 1),
                    getattr(node, "col_offset", 0),
                    "ASR_LAYERING",
                    "endpointing/__init__.py may contain only a package docstring",
                ))

        for path in sorted(endpointing_dir.rglob("*.py")):
            tree = parse(path)
            pkg = ".".join(path.relative_to(root).parts[:-1])
            alias_paths = module_alias_paths(tree, pkg)
            for node in ast.walk(tree):
                imported_paths = _imported_paths(node, pkg, alias_paths)
                if any(
                    module == "main_logic.core"
                    or module.startswith("main_logic.core.")
                    for module in imported_paths
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "endpointing must not import main_logic.core",
                    ))
                if any(
                    module == "main_logic.asr_client.workers"
                    or module.startswith("main_logic.asr_client.workers.")
                    for module in imported_paths
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "endpointing must not import provider workers",
                    ))
                if any(
                    module == "scripts" or module.startswith("scripts.")
                    for module in imported_paths
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "endpointing must not import scripts",
                    ))
                if any(
                    (
                        module == "main_logic.asr_client.speaker_shadow"
                        or module.startswith(
                            "main_logic.asr_client.speaker_shadow."
                        )
                    )
                    and not (
                        module
                        == "main_logic.asr_client.speaker_shadow.contracts"
                        or module.startswith(
                            "main_logic.asr_client.speaker_shadow.contracts."
                        )
                    )
                    for module in imported_paths
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "endpointing may import only speaker_shadow.contracts",
                    ))
            for index, (forbidden, owner) in enumerate((
                ("main_logic.core", "endpointing"),
                ("main_logic.asr_client.workers", "endpointing"),
                ("scripts", "endpointing"),
            )):
                violations.extend(_dynamic_import_violations(
                    path,
                    tree,
                    alias_paths,
                    forbidden,
                    owner,
                    report_generic=(index == 0),
                ))
            dynamic_alias_paths = {
                **alias_paths,
                **_importlib_alias_paths(tree),
            }
            for node in ast.walk(tree):
                targets, dynamic = _dynamic_import_target(
                    node,
                    dynamic_alias_paths,
                )
                if not dynamic or targets is None:
                    continue
                if any(
                    (
                        target == "main_logic.asr_client.speaker_shadow"
                        or target.startswith(
                            "main_logic.asr_client.speaker_shadow."
                        )
                    )
                    and not (
                        target
                        == "main_logic.asr_client.speaker_shadow.contracts"
                        or target.startswith(
                            "main_logic.asr_client.speaker_shadow.contracts."
                        )
                    )
                    for target in targets
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "endpointing may import only speaker_shadow.contracts "
                        "(dynamic import)",
                    ))

        onnx_runtime_path = endpointing_dir / "onnx_runtime.py"
        if onnx_runtime_path.exists():
            onnx_tree = parse(onnx_runtime_path)
            for node in _module_scope_nodes(onnx_tree):
                imports_onnxruntime = (
                    isinstance(node, ast.Import)
                    and any(alias.name == "onnxruntime" for alias in node.names)
                ) or (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "onnxruntime"
                )
                if imports_onnxruntime:
                    violations.append(Violation(
                        onnx_runtime_path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "onnxruntime must remain a lazy function-local import",
                    ))

    if speaker_shadow_dir.exists():
        if speaker_shadow_init.exists():
            speaker_shadow_init_tree = parse(speaker_shadow_init)
            doc_only = (
                len(speaker_shadow_init_tree.body) == 1
                and isinstance(speaker_shadow_init_tree.body[0], ast.Expr)
                and isinstance(
                    speaker_shadow_init_tree.body[0].value,
                    ast.Constant,
                )
                and isinstance(
                    speaker_shadow_init_tree.body[0].value.value,
                    str,
                )
            )
            if not doc_only:
                node = (
                    speaker_shadow_init_tree.body[1]
                    if len(speaker_shadow_init_tree.body) > 1
                    else (
                        speaker_shadow_init_tree.body[0]
                        if speaker_shadow_init_tree.body
                        else None
                    )
                )
                violations.append(Violation(
                    speaker_shadow_init,
                    getattr(node, "lineno", 1),
                    getattr(node, "col_offset", 0),
                    "ASR_LAYERING",
                    "speaker_shadow/__init__.py may contain only a package docstring",
                ))

        forbidden_shadow_dependencies = (
            "main_logic.asr_client.runtime",
            "main_logic.asr_client.endpointing",
            "main_logic.asr_client.workers",
            "main_logic.asr_client.provider_policy",
            "main_logic.asr_client.lifecycle",
            "main_logic.voice_turn",
            "main_logic.voice_input",
            "main_routers",
            "scripts",
        )
        for path in sorted(speaker_shadow_dir.rglob("*.py")):
            tree = parse(path)
            pkg = ".".join(path.relative_to(root).parts[:-1])
            alias_paths = module_alias_paths(tree, pkg)
            for node in ast.walk(tree):
                imported_paths = _imported_paths(node, pkg, alias_paths)
                forbidden = next(
                    (
                        prefix
                        for module in imported_paths
                        for prefix in forbidden_shadow_dependencies
                        if module == prefix or module.startswith(f"{prefix}.")
                    ),
                    None,
                )
                if forbidden is not None:
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        f"speaker_shadow must not import {forbidden}",
                    ))
            violations.extend(_dynamic_import_violations(
                path,
                tree,
                alias_paths,
                forbidden_shadow_dependencies,
                "speaker_shadow",
                report_generic=False,
            ))
            dynamic_alias_paths = {
                **alias_paths,
                **_importlib_alias_paths(tree),
            }
            for node in _module_scope_nodes(tree):
                imports_onnxruntime = (
                    isinstance(node, ast.Import)
                    and any(alias.name == "onnxruntime" for alias in node.names)
                ) or (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "onnxruntime"
                )
                targets, dynamic = _dynamic_import_target(
                    node,
                    dynamic_alias_paths,
                )
                imports_onnxruntime = imports_onnxruntime or bool(
                    dynamic
                    and targets is not None
                    and any(
                        target == "onnxruntime"
                        or target.startswith("onnxruntime.")
                        for target in targets
                    )
                )
                if imports_onnxruntime:
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "speaker_shadow onnxruntime must remain a lazy "
                        "function-local import",
                    ))

    for forbidden_legacy_path in (
        root / "data" / "speaker_models",
        root / "tools" / "voice_eval",
        asr_client_dir / "detector_runtime.py",
    ):
        if forbidden_legacy_path.exists():
            violations.append(Violation(
                forbidden_legacy_path,
                1,
                0,
                "ASR_LAYERING",
                "legacy speaker-shadow path must not be restored",
            ))

    workers_dir = asr_client_dir / "workers"
    if workers_dir.exists():
        for path in sorted(workers_dir.rglob("*.py")):
            tree = parse(path)
            pkg = ".".join(path.relative_to(root).parts[:-1])
            alias_paths = module_alias_paths(tree, pkg)
            for node in ast.walk(tree):
                if any(
                    module == "main_logic.asr_client.endpointing"
                    or module.startswith("main_logic.asr_client.endpointing.")
                    for module in _imported_paths(node, pkg, alias_paths)
                ):
                    violations.append(Violation(
                        path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "provider workers must not import endpointing implementations",
                    ))
            violations.extend(_dynamic_import_violations(
                path,
                tree,
                alias_paths,
                "main_logic.asr_client.endpointing",
                "provider workers",
            ))

    for path in (
        asr_client_dir / "lifecycle.py",
        asr_client_dir / "provider_policy.py",
    ):
        if not path.exists():
            continue
        tree = parse(path)
        pkg = ".".join(path.relative_to(root).parts[:-1])
        alias_paths = module_alias_paths(tree, pkg)
        for node in ast.walk(tree):
            if any(
                module == "main_logic.asr_client.endpointing"
                or module.startswith("main_logic.asr_client.endpointing.")
                for module in _imported_paths(node, pkg, alias_paths)
            ):
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    f"{path.name} must not import endpointing",
                ))
        violations.extend(_dynamic_import_violations(
            path,
            tree,
            alias_paths,
            "main_logic.asr_client.endpointing",
            path.name,
        ))

    if asr_bridge_path.exists():
        bridge_tree = parse(asr_bridge_path)
        provider_literals = (
            _registry_provider_keys(asr_registry_path)
            if asr_registry_path.exists()
            else frozenset()
        )
        route_setter_found = False
        forbidden_runtime_reads = {"lifecycle", "route_mode", "required"}
        for node in ast.walk(bridge_tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.strip().lower() in provider_literals
            ):
                violations.append(Violation(
                    asr_bridge_path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    f"provider literal '{node.value}' must stay below the Core ASR bridge",
                ))
            if (
                isinstance(node, ast.Attribute)
                and node.attr in forbidden_runtime_reads
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"
                and node.value.attr == "_asr_runtime"
            ):
                violations.append(Violation(
                    asr_bridge_path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    f"Core must not read IndependentAsrRuntime.{node.attr}",
                ))
        alias_read_sites: set[tuple[int, int, str]] = set()
        for node in ast.walk(bridge_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                alias_read_sites.update(
                    _asr_runtime_alias_reads(node, forbidden_runtime_reads)
                )
        for line, col, attr in sorted(alias_read_sites):
            violations.append(Violation(
                asr_bridge_path,
                line,
                col,
                "ASR_LAYERING",
                f"Core must not read IndependentAsrRuntime.{attr} "
                f"(via a local alias of self._asr_runtime)",
            ))
        for node in ast.walk(bridge_tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "_set_microphone_route":
                route_setter_found = True
            if node.name in {"_init_asr_runtime_state", "_set_microphone_route"}:
                continue
            for child in ast.walk(node):
                targets = []
                if isinstance(child, ast.Assign):
                    targets = child.targets
                elif isinstance(child, ast.AnnAssign):
                    targets = [child.target]
                if any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr == "_asr_route_mode"
                    for target in targets
                ):
                    violations.append(Violation(
                        asr_bridge_path,
                        child.lineno,
                        child.col_offset,
                        "ASR_LAYERING",
                        "Core route changes must go through _set_microphone_route()",
                    ))
        if not route_setter_found:
            violations.append(Violation(
                asr_bridge_path,
                1,
                0,
                "ASR_LAYERING",
                "core/asr_runtime.py must define _set_microphone_route()",
            ))

    for path in (asr_bridge_path, asr_component_path):
        if not path.exists():
            continue
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "ProcessedVoiceFrame" for alias in node.names
            ) and node.module != "main_logic.voice_turn.audio_input":
                violations.append(Violation(
                    path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    "ProcessedVoiceFrame must come from voice_turn.audio_input",
                ))

    if asr_audio_path.exists():
        for node in ast.walk(parse(asr_audio_path)):
            if isinstance(node, ast.ClassDef) and node.name in {
                "ProcessedVoiceFrame",
                "VoiceInputAudioPipeline",
            }:
                violations.append(Violation(
                    asr_audio_path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    f"provider-neutral {node.name} belongs in voice_turn/audio_input.py",
                ))

    if asr_component_path.exists():
        component_tree = parse(asr_component_path)
        component_class = next(
            (
                node
                for node in component_tree.body
                if isinstance(node, ast.ClassDef)
                and node.name == "IndependentAsrRuntime"
            ),
            None,
        )
        if component_class is None:
            violations.append(Violation(
                asr_component_path,
                1,
                0,
                "ASR_LAYERING",
                "asr_client/runtime.py must define IndependentAsrRuntime",
            ))
        elif component_class.bases or component_class.keywords:
            violations.append(Violation(
                asr_component_path,
                component_class.lineno,
                component_class.col_offset,
                "ASR_LAYERING",
                "IndependentAsrRuntime must be a plain composed object, not a mixin subclass",
            ))
        if component_class is not None:
            forbidden_fields = {
                "_asr_route_mode",
                "_asr_required",
                "_voice_lease_connection_id",
                "_voice_lease_generation",
                "_voice_lease_synchronized",
                "_voice_lease_owner",
                "_voice_lease_hard_muted",
                "_voice_lease_focus_suppressed",
                "_voice_input_suppressed",
            }
            forbidden_methods = {
                "activate_native_route",
                "deactivate_audio_route",
                "block_audio_route",
                "sync_voice_lease",
                "apply_voice_lease_state",
                "process_audio",
                "_process_microphone_audio",
            }
            public_methods = {
                "__init__",
                "display_name",
                "close",
                "capture_ingress_token",
                "suspend",
                "resume",
                "abort",
                "wait_transcript_idle",
                "has_pending_transcript_delivery",
                # Read-only, identity-checked permission for bounded Core PCM
                # storage while Runtime serializes an exact turn handoff.
                "has_pending_turn_handoff",
                "set_speaker_verifier_factory",
                # Typed installation lifecycle API; Runtime still composes its
                # implementation and may not inherit or expose arbitrary APIs.
                "create_speaker_verifier_install_identity",
                "install_speaker_verifier",
                "retire_speaker_verifier_authority",
                "speaker_verifier_installation_permits_evidence",
                "request_speaker_candidate_rejection",
                "start",
                "stop_session",
                "submit",
            }
            for node in ast.walk(component_class):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                    and node.attr in forbidden_fields
                ):
                    violations.append(Violation(
                        asr_component_path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        f"IndependentAsrRuntime must not mirror Core state {node.attr}",
                    ))
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node in component_class.body
                ):
                    if node.name in forbidden_methods:
                        violations.append(Violation(
                            asr_component_path,
                            node.lineno,
                            node.col_offset,
                            "ASR_LAYERING",
                            f"IndependentAsrRuntime must not define {node.name}()",
                        ))
                    if not node.name.startswith("_") and node.name not in public_methods:
                        violations.append(Violation(
                            asr_component_path,
                            node.lineno,
                            node.col_offset,
                            "ASR_LAYERING",
                            f"unexpected IndependentAsrRuntime public method {node.name}()",
                        ))
            for node in ast.walk(component_tree):
                if (
                    isinstance(node, ast.Name)
                    and node.id == "VoiceInputAudioPipeline"
                ):
                    violations.append(Violation(
                        asr_component_path,
                        node.lineno,
                        node.col_offset,
                        "ASR_LAYERING",
                        "IndependentAsrRuntime must not own the Core PCM pipeline",
                    ))

        callbacks_class = next(
            (
                node for node in component_tree.body
                if isinstance(node, ast.ClassDef)
                and node.name == "AsrRuntimeCallbacks"
            ),
            None,
        )
        callback_events = {
            "on_partial": "VoicePartialEvent",
            "on_final": "VoiceTranscriptEvent",
            "on_failure": "AsrFailureEvent",
            "on_status": "AsrStatusEvent",
            "on_lifecycle": "AsrLifecycleNotification",
        }
        annotations = {
            node.target.id: {name.id for name in ast.walk(node.annotation) if isinstance(name, ast.Name)}
            for node in (callbacks_class.body if callbacks_class is not None else [])
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        for callback_name, event_name in callback_events.items():
            if event_name not in annotations.get(callback_name, set()):
                violations.append(Violation(
                    asr_component_path,
                    getattr(callbacks_class, "lineno", 1),
                    0,
                    "ASR_LAYERING",
                    f"{callback_name} must receive immutable {event_name}",
                ))
        for node in ast.walk(component_tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                violations.append(Violation(
                    asr_component_path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    "asr_client/runtime.py must not define a manager mixin",
                ))
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr
                in {
                    "session",
                    "handle_new_message",
                    "handle_input_transcript",
                    "send_status",
                }
            ):
                violations.append(Violation(
                    asr_component_path,
                    node.lineno,
                    node.col_offset,
                    "ASR_LAYERING",
                    f"IndependentAsrRuntime must not access Core attribute self.{node.attr}",
                ))

    if streaming_path.exists():
        streaming_tree = parse(streaming_path)
        stream_data_method = next(
            (
                node
                for node in ast.walk(streaming_tree)
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "stream_data"
            ),
            None,
        )
        audio_branch = None
        if stream_data_method is not None:
            for node in ast.walk(stream_data_method):
                if (
                    isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and len(node.test.ops) == 1
                    and isinstance(node.test.ops[0], ast.Eq)
                    and len(node.test.comparators) == 1
                    and isinstance(node.test.comparators[0], ast.Constant)
                    and node.test.comparators[0].value == "audio"
                ):
                    audio_branch = node
                    break
        valid_audio_branch = False
        if audio_branch is not None and len(audio_branch.body) == 2:
            call_stmt, return_stmt = audio_branch.body
            awaited = call_stmt.value if isinstance(call_stmt, ast.Expr) else None
            call = awaited.value if isinstance(awaited, ast.Await) else None
            valid_audio_branch = bool(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and call.func.attr == "_enqueue_audio_stream_data"
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "message"
                and not call.keywords
                and isinstance(return_stmt, ast.Return)
                and return_stmt.value is None
            )
        if not valid_audio_branch:
            violations.append(Violation(
                streaming_path,
                getattr(audio_branch or stream_data_method, "lineno", 1),
                getattr(audio_branch or stream_data_method, "col_offset", 0),
                "ASR_LAYERING",
                "stream_data audio branch may only await _enqueue_audio_stream_data(message) and return",
            ))

    # -- CORE_MANAGER_SHAPE: class body is only docstring / class constants /
    #    __init__. Any other statement (extra method, nested class, class-level
    #    cache, executable logic) is behavior/state that belongs in a mixin.
    mixin_method_names = {n.name for k in mixin_files.values() for n in k.body
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if manager_class is not None:
        methods = [n for n in manager_class.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for i, node in enumerate(manager_class.body):
            if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # class docstring
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                # Class-level CONSTANTS are allowed, but a mutable container
                # literal (``CACHE = {}``) is shared state, a Call
                # (``TOKEN = open_socket()``) runs behavior at import, and a
                # Lambda (``extra = lambda self: ...``) is a method in disguise
                # — all drift this contract prevents. Recurse: a nested literal
                # (``TOKEN = (open_socket(),)``) hides it behind an outer Tuple.
                # ``node.value`` is None for an annotation-only attr (``x: int``)
                # — nothing to evaluate, so it is fine.
                MUTABLE = (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp,
                           ast.SetComp, ast.GeneratorExp, ast.Call, ast.Lambda)
                bad = None if node.value is None else \
                    next((s for s in ast.walk(node.value) if isinstance(s, MUTABLE)), None)
                if bad is not None:
                    kind = ("a Call (import-time behavior)" if isinstance(bad, ast.Call)
                            else "a lambda (a method in disguise)" if isinstance(bad, ast.Lambda)
                            else "a mutable container (shared instance state)")
                    violations.append(Violation(manager_path, node.lineno, node.col_offset, "CORE_MANAGER_SHAPE",
                                                f"manager class attribute is/contains {kind} — only immutable "
                                                f"class constants are allowed; move state into __init__ (per "
                                                f"instance) or behavior into a mixin"))
                # A class attribute that shares a name with a mixin method (or
                # with the manager's own __init__) wins attribute lookup and
                # silently removes/replaces it — ``__init__ = external`` after
                # ``def __init__`` overwrites the initializer while the method
                # count still passes. An annotation-ONLY entry (``foo: int`` with
                # no value) creates no attribute — only ``__annotations__`` — so
                # it shadows nothing and is skipped.
                if isinstance(node, ast.AnnAssign) and node.value is None:
                    targets = []
                else:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and (t.id in mixin_method_names or t.id == "__init__"):
                        what = "the manager's __init__" if t.id == "__init__" else "a mixin method"
                        violations.append(Violation(manager_path, node.lineno, node.col_offset, "CORE_MANAGER_SHAPE",
                                                    f"manager class attribute '{t.id}' shadows {what} of the same "
                                                    f"name — it would win attribute lookup and drop/replace it in "
                                                    f"LLMSessionManager's API"))
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__":
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                violations.append(Violation(manager_path, node.lineno, node.col_offset, "CORE_MANAGER_SHAPE",
                                            f"manager class defines method '{node.name}' — behavior belongs "
                                            f"in a domain mixin; manager.py keeps only __init__"))
            else:
                violations.append(Violation(manager_path, node.lineno, node.col_offset, "CORE_MANAGER_SHAPE",
                                            f"manager class body allows only docstring/constants/__init__, "
                                            f"found {type(node).__name__} — state/behavior belongs in a mixin"))
        if not any(m.name == "__init__" for m in methods):
            violations.append(Violation(manager_path, manager_class.lineno, manager_class.col_offset,
                                        "CORE_MANAGER_SHAPE", "LLMSessionManager must define __init__ here"))

    # -- CORE_MIXIN_DISJOINT: a method name defined in two DIFFERENT mixins is
    #    an MRO shadowing bug. A property group legitimately repeats the name
    #    WITHIN one mixin, but a VALID group fills each of Python's three
    #    property slots at most once: getter (@property / @x.getter), setter
    #    (@x.setter), deleter (@x.deleter). Two of any slot — or any plain
    #    method sharing the name — is a real shadow (Python keeps only the last).
    def _prop_role(fn):
        for d in fn.decorator_list:
            if isinstance(d, ast.Name) and d.id == "property":
                return "getter"
            if isinstance(d, ast.Attribute) and d.attr in ("getter", "setter", "deleter"):
                return d.attr
        return None  # a plain method

    seen: dict[str, str] = {}
    for path, klass in sorted(mixin_files.items()):
        here = f"{path.name}:{klass.name}"
        groups: dict[str, list] = {}
        for node in klass.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                groups.setdefault(node.name, []).append(node)
        for name, defs in groups.items():
            if len(defs) > 1:
                roles = [_prop_role(d) for d in defs]
                valid_group = (all(r is not None for r in roles)
                               and roles.count("getter") <= 1
                               and roles.count("setter") <= 1
                               and roles.count("deleter") <= 1)
                if not valid_group:
                    last = defs[-1]
                    violations.append(Violation(path, last.lineno, last.col_offset, "CORE_MIXIN_DISJOINT",
                                                f"method '{name}' is defined {len(defs)}× in {here} and is not a "
                                                f"valid property group (at most one each of @property getter / "
                                                f"@{name}.setter / @{name}.deleter) — a later definition shadows "
                                                f"an earlier one"))
            if name in seen:
                first = defs[0]
                violations.append(Violation(path, first.lineno, first.col_offset, "CORE_MIXIN_DISJOINT",
                                            f"method '{name}' already defined in {seen[name]} — "
                                            f"MRO would shadow one of them silently"))
            else:
                seen[name] = here

    # -- CORE_MIXIN_BASES
    if manager_class is not None:
        # Two files defining the same *Mixin name collapse in the set below and
        # only one enters the real MRO; the other is silently orphaned.
        by_name: dict[str, Path] = {}
        for mpath, mklass in sorted(mixin_files.items()):
            if mklass.name in by_name:
                violations.append(Violation(mpath, mklass.lineno, mklass.col_offset, "CORE_MIXIN_BASES",
                                            f"mixin class {mklass.name} is also defined in "
                                            f"{by_name[mklass.name].name} — duplicate names collapse and only "
                                            f"one enters the MRO; give each mixin a unique name"))
            else:
                by_name[mklass.name] = mpath
        for b in manager_class.bases:
            if not isinstance(b, ast.Name):
                violations.append(Violation(manager_path, b.lineno, b.col_offset, "CORE_MIXIN_BASES",
                                            f"non-Name base '{ast.unparse(b)}' — LLMSessionManager bases must "
                                            f"be plain *Mixin names so this check can verify the exact set"))
        base_names = {b.id for b in manager_class.bases if isinstance(b, ast.Name)}
        mixin_names = {k.name for k in mixin_files.values()}
        for missing in sorted(mixin_names - base_names):
            violations.append(Violation(manager_path, manager_class.lineno, manager_class.col_offset,
                                        "CORE_MIXIN_BASES",
                                        f"mixin class {missing} is defined in the package but is not a "
                                        f"base of LLMSessionManager"))
        for extra in sorted(base_names - mixin_names):
            violations.append(Violation(manager_path, manager_class.lineno, manager_class.col_offset,
                                        "CORE_MIXIN_BASES",
                                        f"base {extra} has no *Mixin class defined in the package"))
        # A base name matching a package mixin is not enough: the NAME must be
        # bound to THE module that actually defines it, i.e.
        # ``from .<defining_module> import <Mixin>`` (level 1, module == the
        # mixin's own file stem). Binding the same name from any other sibling
        # (``from ._shared import FocusMixin``) or a level-2+ import
        # (``from .. import ...``) would put a different/outside class in the
        # MRO while the real package mixin sits orphaned, yet the set check
        # would still pass. ``defining_stem`` comes from where the class was
        # discovered.
        defining_imports = {
            mklass.name: (1, mpath.stem, mklass.name)
            for mpath, mklass in mixin_files.items()
        }
        # bound name -> (relative level, module, ORIGINAL imported symbol).
        import_binds = {}
        for node in parse(manager_path).body:
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    import_binds[a.asname or a.name] = (
                        node.level,
                        node.module,
                        a.name,
                    )
        for name in sorted(base_names & mixin_names):
            want = defining_imports[name]
            if import_binds.get(name) != want:
                got = import_binds.get(name)
                if got is None:
                    where = "not bound via an accepted import"
                else:
                    prefix = "." * got[0]
                    where = f"bound from '{prefix}{got[1]}' as symbol '{got[2]}'"
                prefix = "." * want[0]
                violations.append(Violation(manager_path, manager_class.lineno, manager_class.col_offset,
                                            "CORE_MIXIN_BASES",
                                            f"base {name} must be imported as the class named {name} from its "
                                            f"defining module (from {prefix}{want[1]} import {name}) but is "
                                            f"{where}; the "
                                            f"MRO may be using a different/outside class while the package mixin "
                                            f"is orphaned"))

    # -- CORE_FACADE_LAYOUT: the facade only re-exports — docstring + imports.
    #    A top-level function, assignment or executable statement would put
    #    behavior/state in the facade (and, for a facade read of a patched
    #    symbol, freeze it at import) — reject anything but docstring/imports.
    for i, node in enumerate(init_tree.body):
        if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ClassDef):
            violations.append(Violation(init_path, node.lineno, node.col_offset, "CORE_FACADE_LAYOUT",
                                        f"__init__.py must not define classes (found {node.name}) — the "
                                        f"facade only re-exports; the class lives in manager.py"))
        else:
            violations.append(Violation(init_path, node.lineno, node.col_offset, "CORE_FACADE_LAYOUT",
                                        f"__init__.py top level allows only docstring/imports, found "
                                        f"{type(node).__name__} — the facade only re-exports, no behavior/state"))
    last = init_tree.body[-1] if init_tree.body else None
    is_manager_import = (isinstance(last, ast.ImportFrom) and last.level == 1 and last.module == "manager"
                         and [a.name for a in last.names] == ["LLMSessionManager"]
                         and last.names[0].asname is None)
    if not is_manager_import:
        violations.append(Violation(init_path, getattr(last, "lineno", 1), 0, "CORE_FACADE_LAYOUT",
                                    "the last statement of __init__.py must be "
                                    "'from .manager import LLMSessionManager' so the facade namespace is "
                                    "fully populated before the class modules bind it"))
    # An EARLIER ``.manager`` import (before the re-export block finishes) would
    # import manager/mixins against a half-populated facade namespace, defeating
    # the ordering contract — the manager import must appear only as the last line.
    for node in init_tree.body[:-1]:
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "manager":
            violations.append(Violation(init_path, node.lineno, node.col_offset, "CORE_FACADE_LAYOUT",
                                        "'.manager' is imported before the end of __init__.py — it must appear "
                                        "only as the final statement, after every re-export, or the mixins bind "
                                        "the facade before it is fully populated"))

    # -- patch-target checks
    targets = collect_patch_targets(tests_dir)
    # Facade patch routing applies only to core-owned modules. The explicitly
    # registered ASR mixin must read its real owner modules and must not depend
    # on the core facade.
    routing_files = sorted(
        set(mixin_files) | {manager_path}
    )
    module_info = {}
    for path in routing_files:
        tree = parse(path)
        pkg = ".".join(path.resolve().relative_to(root.resolve()).parts[:-1])
        module_info[path] = (tree, facade_snapshot_imports(tree, pkg, facade_owners),
                             module_alias_paths(tree, pkg))

    for attr, sites in sorted(targets.items()):
        if attr not in facade_names:
            for site_path, line, exempt in sites:
                if not exempt:
                    violations.append(Violation(site_path, line, 0, "CORE_PATCH_TARGET_EXISTS",
                                                f"test patches main_logic.core.{attr} but the facade defines "
                                                f"no such name (typo? pass raising=False / create=True for "
                                                f"intentional absent-name guards)"))
            continue
        # The attr exists on the facade, so every patch of it is real (a
        # raising=False / create=True guard only waives the existence check,
        # not the routing requirement) — route ALL consumers.
        for path, (tree, snapshot, alias_paths) in module_info.items():
            if attr in snapshot:
                violations.append(Violation(path, snapshot[attr], 0, "CORE_PATCH_ROUTING",
                                            f"'{attr}' is a test patch target on the facade but its real symbol "
                                            f"is from-imported here at module level — the import snapshots the "
                                            f"pre-patch value and facade patches no longer reach this module; "
                                            f"read it as {FACADE_MODULE_ALIAS}.{attr} instead"))
            # Reads through some OTHER imported module — attribute chains
            # (``bus.dispatch_...``) and string getattr (``getattr(bus,
            # "dispatch_...")``) — dodge facade patches the same way. Reads
            # through main_logic.core itself ARE the facade contract and are
            # skipped inside the helper.
            for line, col, resolved in owner_module_reads(tree, alias_paths, facade_owners.get(attr)):
                violations.append(Violation(
                    path, line, col, "CORE_PATCH_ROUTING",
                    f"'{attr}' is a test patch target on the facade but is read here "
                    f"through module '{resolved}' — that read follows the owner module, "
                    f"not the facade; read it as {FACADE_MODULE_ALIAS}.{attr} instead"))
            for line, col in def_time_facade_reads(tree, alias_paths, attr):
                violations.append(Violation(
                    path, line, col, "CORE_PATCH_ROUTING",
                    f"'{attr}' is read from the facade inside a default/decorator/annotation/"
                    f"class attribute — that expression runs once at import and freezes the "
                    f"value, so facade patches no longer reach it; move the "
                    f"{FACADE_MODULE_ALIAS}.{attr} read into the method body"))
    return violations


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repository root (default: cwd)")
    ap.add_argument("--list-targets", action="store_true",
                    help="print the harvested facade patch-target set and exit")
    args = ap.parse_args()
    root = Path(args.root)

    if args.list_targets:
        for attr, sites in sorted(collect_patch_targets(root / "tests").items()):
            files = sorted({p.name for p, _, _ in sites})
            print(f"{attr:50s} {files}")
        return 0

    violations = run(root)
    for v in violations:
        print(v.render(root))
    if violations:
        print(f"\n{len(violations)} core-contract violation(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
