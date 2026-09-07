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

"""Ban-topic templates must compile lazily, and the warmup must really compile them.

These two guards cover the two ends of one change. Four of the 21 templates in
``config/prompts/prompts_directives`` carry roughly 51 KB of regex source each;
compiling the set measures 294-298 ms. That module sits on memory_server's eager
import chain (``app/__init__.py`` -> ``app/runtime_bindings`` ->
``memory.user_directives``), and memory_server is the first app module imported
in merged mode. uvicorn awaits ``lifespan.startup()`` before ``create_server()``,
so the time is spent while the port does not exist yet -- the user sees
connection-refused, not slowness.

Making the compile lazy on its own would just move the cost onto the first
directive extraction, so the second guard watches the warmup: the
``"module:attribute"`` entry in ``MAIN_SERVER_WARMUP`` has to actually be
evaluated. Otherwise this change silently relocates 300 ms from startup to the
user's first sentence.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_WARMUP_ENTRY = "config.prompts.prompts_directives:DIRECTIVE_PATTERNS"


@pytest.mark.unit
def test_directive_patterns_are_not_compiled_at_import_time() -> None:
    """Importing the module must not trigger the 294 ms compile.

    Asked in a subprocess rather than judged in-process, deliberately.
    Module-level state outlives a test, so if any earlier case in the same
    pytest session has already touched ``DIRECTIVE_PATTERNS`` the cache is
    filled and an in-process assertion would be true forever -- a guard that
    can never fail.
    """
    probe = (
        "import config.prompts.prompts_directives as D;"
        "print('CACHE=%s' % (D._DIRECTIVE_PATTERNS_CACHE is None));"
        "n = len(D.DIRECTIVE_PATTERNS);"
        "print('AFTER=%s' % (D._DIRECTIVE_PATTERNS_CACHE is not None));"
        "print('COUNT=%d' % n);"
        "print('RAW=%d' % len(D._PATTERNS_RAW))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    out = result.stdout

    assert "CACHE=True" in out, (
        "import 完就已经编译好了——那 294 ms 又回到了端口 bind 之前的启动路径上"
    )
    assert "AFTER=True" in out, "取过 DIRECTIVE_PATTERNS 之后缓存仍是空的"

    # 惰性不能顺手改变模板集合：条数必须和原始表一一对应。少一条就是少一条封禁
    # 规则，而那种缺失在功能测试里只表现为"这句没被拦住"，很难归因。
    count = next(line for line in out.splitlines() if line.startswith("COUNT="))
    raw = next(line for line in out.splitlines() if line.startswith("RAW="))
    assert count.split("=")[1] == raw.split("=")[1], (
        f"编译出来的模板数和 _PATTERNS_RAW 对不上：{count} vs {raw}"
    )


@pytest.mark.unit
def test_warmup_entry_for_directive_patterns_is_registered() -> None:
    """The warmup table must carry this entry.

    Without it the first directive extraction compiles the 294 ms of regex
    inline, on a user turn.
    """
    from utils.module_warmup import MAIN_SERVER_WARMUP

    assert _WARMUP_ENTRY in MAIN_SERVER_WARMUP, (
        "DIRECTIVE_PATTERNS 改成惰性之后没登记预热：启动是快了，代价原样落到"
        "用户第一句话上"
    )


@pytest.mark.unit
def test_warmup_touches_the_attribute_not_just_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``"module:attribute"`` entry must actually read the attribute.

    Importing alone is not enough, and for this entry it does nothing at all:
    the module is already in ``sys.modules`` by then, dragged in by
    memory_server's import chain, so ``import_module`` is a cache hit that runs
    no code. The expensive part is the attribute evaluation.

    Mutation: drop the ``getattr`` in ``_warm_one``; this must go red.
    """
    from utils import module_warmup

    touched: list[str] = []

    class _Recording(types.ModuleType):
        def __getattr__(self, name: str) -> object:
            touched.append(name)
            return object()

    fake = _Recording("neko_fake_warm_target")
    monkeypatch.setitem(sys.modules, "neko_fake_warm_target", fake)

    module_warmup._warm_one("neko_fake_warm_target:LAZY_THING")
    assert touched == ["LAZY_THING"], (
        f"带属性的预热条目没有真的求值，只是 import 了一遍：touched={touched}"
    )

    # 不带冒号的条目保持原样：只 import，不去乱碰属性。
    touched.clear()
    module_warmup._warm_one("neko_fake_warm_target")
    assert touched == [], f"无属性条目不该访问任何属性，却碰了 {touched}"


def _check(source: str):
    """Run the startup-chain gate over one snippet of source."""
    import ast

    from scripts.check_startup_import_lazy import check_source

    return check_source(Path("app/fake_module.py"), source, ast.parse(source))


@pytest.mark.unit
def test_the_startup_gate_catches_a_module_scope_import_of_the_lazy_symbol() -> None:
    """The gate knew module names; this symbol needed it to know names too.

    ``check_startup_import_lazy`` bans heavy *modules* from the startup import
    chain. This change created the same class of regression in a shape the gate
    could not see: a cheap module exporting one name whose *evaluation* costs
    294 ms. Importing that name at module scope puts the cost straight back
    before the port binds, and nothing would have gone red.

    Banning the whole module is not an option -- both production importers of
    ``config.prompts.prompts_directives`` legitimately pull other, cheap names
    from it, so a module-level ban would fail main immediately.

    Mutation: drop the ``BANNED_SYMBOLS`` lookup, or make it a prefix match.
    """
    offending = "from config.prompts.prompts_directives import DIRECTIVE_PATTERNS\n"
    violations = _check(offending)
    assert len(violations) == 1, f"gate missed the module-scope import: {violations}"
    assert "DIRECTIVE_PATTERNS" in violations[0][2]

    # 同一个模块里的其它名字必须放行——生产的两处 importer 就是这么用的，规则一宽
    # 就会立刻把 main 打红。
    innocent = "from config.prompts.prompts_directives import extract_directives\n"
    assert _check(innocent) == [], "规则太宽，合法的轻量导入被打红了"

    # 函数体内的导入正是被认可的惰性写法。
    lazy = (
        "def f():\n"
        "    from config.prompts.prompts_directives import DIRECTIVE_PATTERNS\n"
        "    return DIRECTIVE_PATTERNS\n"
    )
    assert _check(lazy) == [], "函数内导入被误判——那是这条规则要引导人去写的形式"

    # 逃生阀照旧生效。
    escaped = (
        "from config.prompts.prompts_directives import (  # noqa: STARTUP_LAZY_IMPORT\n"
        "    DIRECTIVE_PATTERNS,\n"
        ")\n"
    )
    assert _check(escaped) == [], "noqa 逃生阀对符号规则失效了"


@pytest.mark.unit
def test_the_startup_gate_is_not_bypassed_by_an_aliased_attribute_read() -> None:
    """A from-import ban that only sees from-imports is a ban with a doorway.

    ``import ... as d`` followed by a module-scope ``d.DIRECTIVE_PATTERNS`` runs
    the same module ``__getattr__`` and pays the same 294 ms, but it is an
    ``ast.Attribute`` read rather than an import alias, so the first version of
    this rule never saw it (CodeRabbit). A guard whose bypass can be named in one
    line does not guard anything.

    Mutation: drop the ``ast.Attribute`` walk, or stop resolving aliases.
    """
    aliased = (
        "import config.prompts.prompts_directives as directives\n"
        "PATTERNS = directives.DIRECTIVE_PATTERNS\n"
    )
    violations = _check(aliased)
    assert len(violations) == 1, f"别名属性读法绕过了闸门：{violations}"
    assert "DIRECTIVE_PATTERNS" in violations[0][2]

    # 不带 as 的写法：`import a.b.c` 只绑定 `a`，使用点把全路径又拼了一遍。
    spelled_out = (
        "import config.prompts.prompts_directives\n"
        "PATTERNS = config.prompts.prompts_directives.DIRECTIVE_PATTERNS\n"
    )
    assert len(_check(spelled_out)) == 1, "全路径属性读法没被认出来"

    # 函数体内读它正是被引导的写法，必须放行。
    lazy = (
        "import config.prompts.prompts_directives as directives\n"
        "def f():\n"
        "    return directives.DIRECTIVE_PATTERNS\n"
    )
    assert _check(lazy) == [], "函数内属性读被误判"

    # 同名符号挂在别的模块上，不该命中——规则是 (模块, 符号) 对，不是符号名。
    lookalike = (
        "import some.other.module as d\n"
        "PATTERNS = d.DIRECTIVE_PATTERNS\n"
    )
    assert _check(lookalike) == [], "只按符号名匹配了，模块没参与判断"


@pytest.mark.unit
def test_the_startup_gate_does_not_fire_on_a_relative_import() -> None:
    """A relative import's ``stmt.module`` is a relative path, not an absolute one.

    ``from ...config.prompts.prompts_directives import X`` parses with
    ``module == "config.prompts.prompts_directives"`` and ``level == 3``, but it
    resolves to an entirely different module. Matching it against the absolute
    ``BANNED_SYMBOLS`` key would be a false positive on a shared gate that every
    PR runs (CodeRabbit).

    Mutation: drop the ``stmt.level == 0`` condition.
    """
    relative = (
        "from ...config.prompts.prompts_directives import DIRECTIVE_PATTERNS\n"
    )
    assert _check(relative) == [], "相对导入被当成绝对路径匹配了——共享闸门上的误报"


@pytest.mark.unit
def test_the_lazy_symbol_still_survives_a_wildcard_import() -> None:
    """``import *`` does not consult ``__getattr__``, so the name needs ``__all__``.

    With no ``__all__``, Python builds a wildcard import by enumerating module
    globals. ``DIRECTIVE_PATTERNS`` used to be a global; once it became lazy it
    silently vanished from ``from ... import *``, and a downstream consumer would
    get a ``NameError`` at the point of use rather than at import (codex).

    Declaring ``__all__`` fixes it because Python then iterates that list and
    ``getattr``s each name, which does reach ``__getattr__``. The list is computed
    from the module's actual public globals rather than hand-written, so declaring
    it cannot quietly narrow the wildcard surface that already existed.

    Run in a subprocess: a wildcard import here would compile the patterns and
    fill the cache for the rest of the session, which is exactly what the
    laziness guard above needs to observe as empty.

    Mutation: delete the ``__all__`` block.
    """
    probe = (
        "import config.prompts.prompts_directives as D;"
        "print('LAZY=%s' % (D._DIRECTIVE_PATTERNS_CACHE is None));"
        "ns = {};"
        "exec('from config.prompts.prompts_directives import *', ns);"
        "print('WILDCARD=%s' % ('DIRECTIVE_PATTERNS' in ns));"
        "print('SIBLING=%s' % ('extract_directives' in ns));"
        "print('SAME=%s' % (ns.get('DIRECTIVE_PATTERNS') is D.DIRECTIVE_PATTERNS))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    out = result.stdout

    # 声明 __all__ 不能把惰性弄没了——它只是一张名字表，不该触发求值。
    assert "LAZY=True" in out, "加了 __all__ 之后 import 期就编译了"
    assert "WILDCARD=True" in out, (
        "通配导入里没有 DIRECTIVE_PATTERNS——下游 `import *` 的消费者会在用到时才 NameError"
    )
    assert "SIBLING=True" in out, "声明 __all__ 顺手把通配面收窄了"
    assert "SAME=True" in out, "通配拿到的不是同一个对象"


@pytest.mark.unit
def test_the_startup_gate_does_not_fire_inside_type_checking() -> None:
    """A read under ``if TYPE_CHECKING:`` costs nothing at runtime.

    ``_iter_module_scope_stmts`` already excludes that branch — but it yields the
    ``If`` node itself before deciding not to descend, so an unconditional child
    walk lands right back inside the body it just excluded (codex). On a gate
    every PR runs, that is a false positive on code with no runtime cost, which
    is worse than a miss.

    Mutation: drop the ``TYPE_CHECKING`` skip in ``_iter_eager_nodes``.
    """
    type_checking_only = (
        "from typing import TYPE_CHECKING\n"
        "import config.prompts.prompts_directives as d\n"
        "if TYPE_CHECKING:\n"
        "    P = d.DIRECTIVE_PATTERNS\n"
    )
    assert _check(type_checking_only) == [], "TYPE_CHECKING 分支被当成运行时访问了"

    # 但真会执行的模块作用域分支必须照抓——别把豁免开得比 TYPE_CHECKING 还宽。
    really_runs = (
        "import config.prompts.prompts_directives as d\n"
        "if True:\n"
        "    P = d.DIRECTIVE_PATTERNS\n"
    )
    assert len(_check(really_runs)) == 1, "会执行的 if 分支里的访问被漏掉了"

    # TYPE_CHECKING 的 else 分支是会执行的，同样要抓。
    else_branch = (
        "from typing import TYPE_CHECKING\n"
        "import config.prompts.prompts_directives as d\n"
        "if TYPE_CHECKING:\n"
        "    pass\n"
        "else:\n"
        "    P = d.DIRECTIVE_PATTERNS\n"
    )
    assert len(_check(else_branch)) == 1, "TYPE_CHECKING 的 else 分支被一起豁免了"

    # if 的判断表达式一定执行，而且它是唯一只能从 If 节点本身看到的位置——body 里的
    # 语句外层迭代器会单独 yield 一遍，test 不会。跳过 If 时把 test 一起丢掉，这里就
    # 漏了。
    in_the_test = (
        "import config.prompts.prompts_directives as d\n"
        "if d.DIRECTIVE_PATTERNS:\n"
        "    pass\n"
    )
    assert len(_check(in_the_test)) == 1, "if 判断表达式里的访问被漏掉了"


@pytest.mark.unit
def test_the_startup_gate_catches_a_wildcard_import_of_the_lazy_module() -> None:
    """Adding ``__all__`` handed the rule a doorway it had to be taught about.

    Declaring ``__all__`` is what put the lazy name back into ``from ... import *``
    -- which also means a wildcard import now ``getattr``s it and compiles the
    regexes on the pre-bind path. The rule looked up the literal alias name
    ``"*"`` in ``BANNED_SYMBOLS``, which never matches, so the fix for one finding
    opened a bypass for the guard added for another (codex).

    Mutation: drop the ``"*"`` branch.
    """
    wildcard = "from config.prompts.prompts_directives import *\n"
    violations = _check(wildcard)
    assert len(violations) == 1, f"通配导入绕过了闸门：{violations}"
    assert "DIRECTIVE_PATTERNS" in violations[0][2]

    # 函数体内的通配导入是语法错误，所以没有对应的放行用例；换一个方向：别的模块
    # 的通配导入不该命中。
    other = "from some.other.module import *\n"
    assert _check(other) == [], "任何通配导入都被打红了"

    # 逃生阀对通配这条同样要管用。
    escaped = (
        "from config.prompts.prompts_directives import *  # noqa: STARTUP_LAZY_IMPORT\n"
    )
    assert _check(escaped) == [], "noqa 对通配规则失效"


@pytest.mark.unit
def test_a_nested_noqa_cannot_suppress_an_import_time_read() -> None:
    """The statement line range is the wrong scope for an expression read.

    A ``FunctionDef``'s range covers the whole body, so a ``noqa`` anywhere
    inside it was suppressing reads in the decorator and default arguments --
    which do run at import time. An ``if`` had the same problem: a ``noqa`` in
    the body suppressed a read in the test expression (CodeRabbit).

    The full-statement range is still right for imports, where a ``noqa`` on any
    line of a multi-line import should count.

    Mutation: go back to one shared suppression scope for both kinds.
    """
    decorated = (
        "import config.prompts.prompts_directives as d\n"
        "@deco(d.DIRECTIVE_PATTERNS)\n"
        "def f():\n"
        "    x = 1  # noqa: STARTUP_LAZY_IMPORT\n"
    )
    assert len(_check(decorated)) == 1, "函数体里的 noqa 压掉了装饰器里的 import 期读取"

    in_if_test = (
        "import config.prompts.prompts_directives as d\n"
        "if d.DIRECTIVE_PATTERNS:\n"
        "    pass  # noqa: STARTUP_LAZY_IMPORT\n"
    )
    assert len(_check(in_if_test)) == 1, "if 体里的 noqa 压掉了判断表达式里的读取"

    # 写在读取那一行的 noqa 必须照常生效——否则这条规则就没有逃生阀了。
    on_the_line = (
        "import config.prompts.prompts_directives as d\n"
        "P = d.DIRECTIVE_PATTERNS  # noqa: STARTUP_LAZY_IMPORT\n"
    )
    assert _check(on_the_line) == [], "读取点自己那行的 noqa 失效了"

    # 多行 import 仍然整条语句范围生效。
    multiline = (
        "from config.prompts.prompts_directives import (  # noqa: STARTUP_LAZY_IMPORT\n"
        "    DIRECTIVE_PATTERNS,\n"
        ")\n"
    )
    assert _check(multiline) == [], "多行 import 的 noqa 范围被收窄了"
