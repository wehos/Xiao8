"""A forked plugin child must not inherit other hosts' uplink credentials.

Plugin processes are started with a bare ``multiprocessing.Process``, so on
POSIX they are forked from the process that owns ``state.plugin_hosts``. That
dict holds every running host's ``HostTransport``, including its per-host uplink
token and its endpoint strings -- enough for plugin B to build a
``ChildTransport`` with plugin A's credential and send frames A will accept as
its own.

``plane_bridge``'s hook covers only the single module-level ingest token; these
are per-host and live on shared state, so they need their own scrub.

Note what this can and cannot claim: the scrub removes the reachable path, not
the bytes. A plugin that goes looking through its own heap is not stopped by it
-- only a non-inheriting start method would be.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin.core import zmq_transport


class _FakeTransport:
    def __init__(self, token: str) -> None:
        self._uplink_token = token
        self.downlink_endpoint = "tcp://127.0.0.1:5555"


@pytest.mark.plugin_unit
def test_the_scrub_blanks_tokens_and_empties_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: drop the ``hosts.clear()`` or the token overwrite."""
    victim = _FakeTransport("victim-uplink-secret")
    host = SimpleNamespace(transport=victim, _model_gateway_token="victim-model-secret")
    hosts = {"victim": host}
    fake_state_mod = SimpleNamespace(state=SimpleNamespace(plugin_hosts=hosts))
    monkeypatch.setitem(sys.modules, "plugin.core.state", fake_state_mod)

    zmq_transport._scrub_inherited_host_credentials()

    assert hosts == {}, "继承来的 host 表还在，另一个插件的 transport 直接可读"
    assert host._model_gateway_token == ""
    assert victim._uplink_token == "", (
        "只丢了引用没打掉值——子进程里别处还引着这个对象就白清了"
    )


@pytest.mark.plugin_unit
def test_the_scrub_survives_a_child_that_never_imported_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It runs in an ``after_in_child`` hook, where raising is not an option.

    Reading ``sys.modules`` rather than importing is deliberate: an import
    inside a fork hook can deadlock on the import lock a parent thread was
    holding at fork time.
    """
    monkeypatch.delitem(sys.modules, "plugin.core.state", raising=False)

    zmq_transport._scrub_inherited_host_credentials()  # must not raise


@pytest.mark.plugin_unit
def test_the_hook_is_wired_wherever_fork_exists() -> None:
    """Both pytest jobs run windows-latest, where ``os.fork`` does not exist.

    A fork-based behavioural test therefore skips everywhere it would run, and
    "the hook was never registered" would be an invisible mutation. This asserts
    the wiring from the source instead, and the registration flag on POSIX.
    """
    source = Path(zmq_transport.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    wired = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "register_at_fork"):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "after_in_child"
                and isinstance(kw.value, ast.Name)
                and kw.value.id == "_scrub_inherited_host_credentials"
            ):
                wired = True

    assert wired, (
        "zmq_transport 里没有把 _scrub_inherited_host_credentials 挂到 "
        "register_at_fork(after_in_child=...) 上——POSIX 下子进程会继承别的 "
        "host 的 uplink token"
    )

    if hasattr(os, "register_at_fork"):
        assert zmq_transport._HOST_CREDENTIAL_FORK_HOOK_REGISTERED, (
            "支持 fork 的平台上却没注册"
        )
