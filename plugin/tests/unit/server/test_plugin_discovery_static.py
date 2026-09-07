"""Discovery reads plugin metadata off disk and never imports a plugin.

Reading a plugin's metadata used to mean importing it, once per plugin, in a
throwaway subprocess, on every registry refresh — so a plugin only had to sit
in the plugins directory to get its module-level code executed, and starting
one plugin executed every other one. The derivation now happens once on the
author's machine (``neko-plugin build``) and ships as ``plugin.meta.json``.

The load-bearing guard here is behavioural, not structural: a refresh must
spawn zero subprocesses. A structural check for "does registry_service mention
the scanner" would pass the moment someone reached the scanner through a
helper, which is exactly the shape of the regression worth catching.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from plugin.server.application.plugins import registry_service as module
from plugin.server.infrastructure import autostart_approvals, packaged_metadata
from plugin.settings import BUILTIN_PLUGIN_CONFIG_ROOT

pytestmark = pytest.mark.plugin_unit


class _PopenPoisoned(AssertionError):
    """Raised if anything tries to start a process during discovery."""


@pytest.fixture
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []

    def _poisoned(*args, **kwargs):
        attempts.append(args[0] if args else kwargs.get("args"))
        raise _PopenPoisoned(
            "discovery started a subprocess; it must read packaged metadata "
            f"instead of importing plugins: {attempts[-1]!r}"
        )

    monkeypatch.setattr(subprocess, "Popen", _poisoned)
    return attempts


def test_a_full_discovery_never_starts_a_process(_no_subprocess: list[object]) -> None:
    """The whole point, checked against the real builtin plugin tree.

    Mutation: put ``scan_plugin_metadata_isolated`` back into
    ``_build_discovery_payload`` and this fails with ``_PopenPoisoned``.
    """
    root = Path(BUILTIN_PLUGIN_CONFIG_ROOT)
    if not root.is_dir():
        pytest.skip("builtin plugin root is not present in this checkout")

    snapshot = module._discover_registry_snapshot_sync((root,))

    assert _no_subprocess == [], (
        f"discovery spawned {len(_no_subprocess)} process(es): {_no_subprocess}"
    )
    assert snapshot.records, "没有发现任何插件，这条守卫就不知道自己在盯什么"


def test_discovery_recovers_the_real_entries_from_packaged_metadata(
    _no_subprocess: list[object],
) -> None:
    """Not importing must not mean not knowing.

    The builtin tree ships ``plugin.meta.json`` for every plugin, so a refresh
    that imports nothing still has to produce the same entries the old scan
    produced. Zero subprocesses with zero entries would satisfy the guard above
    while having thrown the metadata away.
    """
    root = Path(BUILTIN_PLUGIN_CONFIG_ROOT)
    if not root.is_dir():
        pytest.skip("builtin plugin root is not present in this checkout")

    snapshot = module._discover_registry_snapshot_sync((root,))
    entries = [
        entry
        for record in snapshot.records
        for entry in (record.meta_payload.get("entries_preview") or [])
    ]
    assert entries, (
        "一个入口都没读出来：不 import 是达成了，但元数据也一起丢了"
    )
    unnamed = [entry for entry in entries if not entry.get("id")]
    assert not unnamed, f"有入口没有 id：{unnamed[:3]}"


def test_the_placeholder_schema_has_no_properties_key() -> None:
    """An empty ``properties`` map is worse than none at all.

    The plugin manager decides whether to render a generated form with
    ``!!(schema?.properties && typeof schema.properties === 'object')``, and
    ``!!{}`` is true in JavaScript. A placeholder carrying ``properties: {}``
    therefore renders a form with zero fields, submits ``{}`` as the arguments,
    and takes away the raw-JSON box the user needs — strictly worse than
    admitting we do not know.

    Mutation: add ``"properties": {}`` to ``PLACEHOLDER_INPUT_SCHEMA``.
    """
    assert "properties" not in packaged_metadata.PLACEHOLDER_INPUT_SCHEMA
    assert packaged_metadata.PLACEHOLDER_INPUT_SCHEMA.get("additionalProperties") is True

    normalized = module._normalize_entry_input_schema({"id": "x"})
    assert "properties" not in normalized["input_schema"]


def test_a_known_but_empty_schema_is_left_alone() -> None:
    """A parameterless entry keeps its empty ``properties`` map.

    ``properties: {}`` from the packager means "we looked, it takes nothing":
    the UI renders an empty form and submits ``{}``, which is right. Replacing
    it with the placeholder would hand the user a raw JSON box for an entry
    that accepts no arguments.

    Mutation: normalise on truthiness (``if schema:``) instead of on the
    presence of the ``properties`` key.
    """
    normalized = module._normalize_entry_input_schema(
        {"id": "ping", "input_schema": {"type": "object", "properties": {}}}
    )
    assert normalized["input_schema"]["properties"] == {}


def _write_plugin(tmp_path: Path, *, entries: list[dict], sdk_version: str | None = None,
                  source_sha: str | None = None,
                  build_env: dict | None = None) -> Path:
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    payload = {
        "schema_version": packaged_metadata.PACKAGED_METADATA_SCHEMA_VERSION,
        "sdk_version": sdk_version if sdk_version is not None else packaged_metadata.SDK_VERSION,
        "source_sha256": (
            source_sha
            if source_sha is not None
            else packaged_metadata.compute_source_sha256(plugin_dir)
        ),
        "source_files": packaged_metadata.source_file_names(plugin_dir)[0],
        "source_bytes": packaged_metadata.source_stat_summary(plugin_dir).total_bytes,
        "build_env": (
            build_env
            if build_env is not None
            else packaged_metadata.build_environment()
        ),
        "entries": entries,
        # v3 一定会写这三张表，缺哪张都算包坏了。
        "handlers": {},
        "entry_methods": {},
        "entries_config_sha256": packaged_metadata.entries_config_digest({}, {}),
    }
    (plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return plugin_dir


def test_packaged_metadata_is_read_back(tmp_path: Path) -> None:
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go", "input_schema": {"properties": {}}}])
    result = packaged_metadata.read_packaged_metadata(plugin_dir)
    assert result is not None
    assert [entry["id"] for entry in result.entries] == ["go"]


def test_a_newer_mtime_alone_does_not_reject_the_metadata(tmp_path: Path) -> None:
    """A fresh clone has arbitrary mtimes; content is what decides.

    git does not preserve modification times, so on any newly cloned checkout
    the sources can easily look newer than the generated file. Rejecting on
    mtime alone would silently degrade every builtin plugin to placeholders on
    every new machine.

    Mutation: drop the content-hash confirmation and reject on mtime alone.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    future = time.time() + 60
    os.utime(plugin_dir / "main.py", (future, future))

    assert (plugin_dir / "main.py").stat().st_mtime_ns > meta_path.stat().st_mtime_ns, (
        "前提没成立：源文件并没有比生成物新"
    )
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None, (
        "内容没变却因为时间戳被判过时，新 clone 上所有内置插件都会退化成占位"
    )


def test_changed_sources_reject_the_metadata(tmp_path: Path) -> None:
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    (plugin_dir / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    future = time.time() + 60
    os.utime(plugin_dir / "main.py", (future, future))

    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "插件代码改了却仍然用打包时的 schema，作者改完签名看不到任何变化"
    )


def test_a_foreign_sdk_major_rejects_the_metadata(tmp_path: Path) -> None:
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}], sdk_version="99.0.0")
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None


def test_a_patch_level_sdk_difference_is_accepted(tmp_path: Path) -> None:
    """Otherwise every SDK release invalidates the whole ecosystem's metadata."""
    major = packaged_metadata.SDK_VERSION.split(".", 1)[0]
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}], sdk_version=f"{major}.99.99")
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None


def test_a_plugin_the_user_never_started_is_not_autostarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installing and running are different acts; only the second is the user's.

    ``plugin_runtime.auto_start`` defaults to true and is declared by the
    plugin itself, so without this a freshly installed plugin runs its own code
    at the next greeting without ever having been started.

    Mutation: drop the ``is_autostart_approved`` check from
    ``_get_autostart_plugin_ids_sync``.
    """
    pending = {"just_installed"}
    monkeypatch.setattr(
        module, "is_autostart_approved", lambda plugin_id: plugin_id not in pending
    )
    monkeypatch.setattr(
        module,
        "_build_ordered_plugin_ids_sync",
        sorted,
    )
    monkeypatch.setattr(
        module.state,
        "plugins",
        {
            "old_timer": {"runtime_enabled": True, "runtime_auto_start": True},
            "just_installed": {"runtime_enabled": True, "runtime_auto_start": True},
        },
        raising=False,
    )

    assert module._get_autostart_plugin_ids_sync() == ["old_timer"]


def _isolated_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Keep the approval store out of the developer's real config directory.

    Without this the suite writes a real ``plugin_autostart_pending.json`` under
    the user's app config dir, and the entries one test leaves behind change how
    autostart behaves for every test after it.
    """
    store: dict[str, object] = {}

    class _FakeConfigManager:
        def load_json_config(self, name):
            if name not in store:
                raise FileNotFoundError(name)
            return store[name]

        def save_json_config(self, name, payload):
            store[name] = payload

    import utils.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _FakeConfigManager
    )
    autostart_approvals._reset_cache_for_testing()
    return store


def test_a_plugin_with_no_record_is_allowed_to_autostart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence of a record means "not our business", never "denied".

    The record is a pending-list rather than an approved-list precisely so that
    every failure in this file errs towards a plugin autostarting the way it
    always did. An approved-list needs a baseline, and getting that baseline
    wrong silences the user's whole autostart set.

    Mutation: invert the store to an approved-list.
    """
    _isolated_store(monkeypatch)
    try:
        assert autostart_approvals.is_autostart_approved("never_seen_before")
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_an_unreadable_store_still_allows_autostart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt or unreadable store must not take everyone's plugins away.

    Mutation: re-raise instead of falling back to an empty pending set.
    """
    class _BrokenConfigManager:
        def load_json_config(self, name):
            raise OSError("disk said no")

        def save_json_config(self, name, payload):
            raise OSError("disk said no")

    import utils.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _BrokenConfigManager
    )
    autostart_approvals._reset_cache_for_testing()
    try:
        assert autostart_approvals.is_autostart_approved("anything")
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_a_freshly_installed_plugin_waits_for_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marked at install, cleared the first time the user starts it.

    Mutation: drop the ``clear_autostart_pending`` call from
    ``_persist_user_runtime_intent``.
    """
    _isolated_store(monkeypatch)
    try:
        autostart_approvals.mark_autostart_pending("just_installed")
        assert not autostart_approvals.is_autostart_approved("just_installed")
        assert autostart_approvals.is_autostart_approved("some_other_plugin"), (
            "一个插件的待批准记录不该影响别的插件"
        )
        autostart_approvals.clear_autostart_pending("just_installed")
        assert autostart_approvals.is_autostart_approved("just_installed")
    finally:
        autostart_approvals._reset_cache_for_testing()


@pytest.mark.asyncio
async def test_the_refresh_lock_covers_reading_disk_not_just_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading and publishing must happen under the same lock acquisition.

    Locking only the publish step is not enough: two overlapping refreshes can
    each read the same stale ``existing_snapshot`` outside the lock, then enter
    it one after the other, and the second one reconciles additions and
    removals against a registry that the first has already changed — deleting
    records it should have kept (codex).

    Behavioural rather than structural: discovery itself asserts the lock is
    held while it runs, so moving either read back outside fails here.

    Mutation: hoist ``_discover_registry_snapshot_sync`` or
    ``_get_registered_plugin_snapshot_sync`` above the ``with`` statement.
    """
    held: list[bool] = []

    def _discover(roots):
        held.append(module._REGISTRY_REFRESH_LOCK._is_owned())
        return module.PluginDiscoverySnapshot(
            records=[], failures=[], config_paths=set(), shadowed=[]
        )

    def _snapshot():
        held.append(module._REGISTRY_REFRESH_LOCK._is_owned())
        return {}

    monkeypatch.setattr(module, "_discover_registry_snapshot_sync", _discover)
    monkeypatch.setattr(module, "_get_registered_plugin_snapshot_sync", _snapshot)
    monkeypatch.setattr(module, "_list_running_plugin_ids_sync", set)
    monkeypatch.setattr(module, "_collect_missing_plugin_ids_sync", lambda snapshot: set())
    monkeypatch.setattr(
        module, "_remove_stale_plugin_metadata_sync", lambda ids, running_ids: ([], [])
    )

    await module.PluginRegistryService().refresh_registry()

    assert held and all(held), (
        f"读盘发生在锁外，两次重叠刷新会拿着过时快照互相覆盖：{held}"
    )


def test_a_packaged_plugin_does_not_need_a_second_import_to_start(
    tmp_path: Path,
) -> None:
    """``start_plugin`` reuses packaged handlers instead of re-importing.

    The plugin process imports the plugin; the metadata worker used to import
    it a second time for a result the package already carries.

    The emptiness of ``handlers`` is deliberately not what decides this — see
    ``test_a_packaged_plugin_with_no_entries_still_skips_the_scan``.

    Mutation: ignore ``packaged.handlers`` and return an empty mapping.
    """
    from plugin.server.application.plugins import lifecycle_service

    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["handlers"] = {"demo.go": {"event_type": "plugin_entry", "id": "go"}}
    payload["entry_methods"] = {"go": "go"}
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = lifecycle_service._read_packaged_isolated_metadata(
        plugin_dir / "plugin.toml", "demo"
    )
    assert recovered is not None
    assert recovered.entry_methods == {"go": "go"}
    assert list(recovered.handlers) == ["demo.go"]


def test_packaged_handlers_minted_under_another_id_are_not_reused(
    tmp_path: Path,
) -> None:
    """A conflict-renamed plugin must rescan, not register nothing.

    Handler keys embed the plugin id, and an id conflict renames a plugin at
    registration time (``demo`` becomes ``demo_1``).
    ``install_isolated_plugin_metadata`` silently drops every key that does not
    belong to the runtime id, so reusing packaged keys minted under the
    manifest id would register zero handlers — the plugin starts, reports
    success, and exposes no entries at all (coderabbit).

    Mutation: drop the ownership check and return the packaged object anyway.
    """
    from plugin.server.application.plugins import lifecycle_service

    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["handlers"] = {"demo.go": {"event_type": "plugin_entry", "id": "go"}}
    payload["entry_methods"] = {"go": "go"}
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        lifecycle_service._read_packaged_isolated_metadata(
            plugin_dir / "plugin.toml", "demo_1"
        )
        is None
    ), "改名后的插件复用了按原 id 铸的 handler key，会一个 handler 都注册不上"


def test_config_declared_entries_force_a_real_scan(tmp_path: Path) -> None:
    """Packaged handlers are derived from the author's manifest, not this machine's.

    A runtime config or an active profile can carry its own ``entries`` table.
    The packager never saw it, so its handlers are not the set this machine
    should register (codex). Those plugins have to scan.

    Mutation: drop the ``_config_declares_entries`` check.
    """
    from plugin.server.application.plugins import lifecycle_service

    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["handlers"] = {"demo.go": {"event_type": "plugin_entry", "id": "go"}}
    payload["entry_methods"] = {"go": "go"}
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        lifecycle_service._read_packaged_isolated_metadata(
            plugin_dir / "plugin.toml", "demo"
        )
        is not None
    ), "前提没成立：没有配置覆盖时本来就该用打包元数据"

    assert (
        lifecycle_service._read_packaged_isolated_metadata(
            plugin_dir / "plugin.toml",
            "demo",
            conf={"entries": [{"id": "from_profile"}]},
        )
        is None
    ), "生效配置自带 entries 时仍然用了包里的 handler，注册的会是另一套"


def test_the_freshness_fingerprint_watches_every_file(tmp_path: Path) -> None:
    """Entries can be derived from data files, not just code.

    A plugin whose module-level code builds entries from a YAML, CSV or
    template invalidates nothing if the fingerprint only looks at
    ``.py``/``.toml``/``.json`` — the host keeps serving a schema derived from
    data that has since changed (codex).

    Mutation: put a suffix filter back into ``_iter_source_files``.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    before = packaged_metadata.compute_source_sha256(plugin_dir)

    (plugin_dir / "entries.yaml").write_text("go: {}\n", encoding="utf-8")
    after = packaged_metadata.compute_source_sha256(plugin_dir)

    assert before != after, (
        "加了一个数据文件而指纹没变，从它派生条目的插件会一直用旧 schema"
    )


def _store_that_fails_to_write(monkeypatch: pytest.MonkeyPatch, seed: list[str]) -> None:
    """A config manager that reads fine but cannot write."""
    state = {"pending": list(seed)}

    class _ReadOnlyConfigManager:
        def load_json_config(self, name):
            return dict(state)

        def save_json_config(self, name, payload):
            raise OSError("no space left on device")

    import utils.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _ReadOnlyConfigManager
    )
    autostart_approvals._reset_cache_for_testing()


def test_a_failed_mark_does_not_pretend_the_plugin_is_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-memory state must not claim more than what reached disk.

    If the pending record cannot be written, this process would believe the
    plugin is held back while the file says nothing — and after a restart it
    autostarts, unapproved, with nothing left to retry the write. Rolling the
    mutation back keeps memory and disk telling the same story: this plugin was
    not gated, and the log says why.

    Mutation: ignore ``_save_locked``'s return value in ``mark_autostart_pending``.
    """
    _store_that_fails_to_write(monkeypatch, [])
    try:
        autostart_approvals.mark_autostart_pending("newcomer")
        assert autostart_approvals.is_autostart_approved("newcomer"), (
            "写盘失败却在内存里当成已拦下：重启后它会未经批准自启，而没人重试"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_a_failed_clear_keeps_the_plugin_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror direction: a lost approval must be retried, not assumed.

    Mutation: ignore ``_save_locked``'s return value in
    ``clear_autostart_pending``.
    """
    _store_that_fails_to_write(monkeypatch, ["waiting"])
    try:
        assert not autostart_approvals.is_autostart_approved("waiting"), (
            "前提没成立：这个插件本来就该是待批准的"
        )
        autostart_approvals.clear_autostart_pending("waiting")
        assert not autostart_approvals.is_autostart_approved("waiting"), (
            "批准没写成却在内存里当成已完成：重启后旧文件又把它拦下来，没人知道为什么"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


class _FakeFifoEntry:
    """A directory entry shaped like a FIFO: not a dir, not a symlink, not a file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = Path(path).name

    def is_symlink(self) -> bool:
        return False

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return False

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return False

    def stat(self, follow_symlinks: bool = True):
        raise AssertionError("stat() reached a non-regular entry")


def test_a_named_pipe_never_reaches_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hashing must not read anything that can block.

    ``entry.stat()`` succeeds on a FIFO, socket or device node, so without an
    explicit regular-file check they land in the file list and the digest step
    calls ``Path.read_bytes()`` on them. A FIFO with no writer blocks there
    forever — and registry refresh now holds ``_REGISTRY_REFRESH_LOCK`` across
    the whole operation, so one named pipe in a plugin directory would wedge the
    entire plugin registry (coderabbit).

    Driven through a fake dir entry rather than ``os.mkfifo`` so the guard also
    runs on Windows, where there is no mkfifo.

    Mutation: drop the ``entry.is_file(follow_symlinks=False)`` check — the fake
    entry's ``stat()`` then raises and this fails.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    fifo_path = str(plugin_dir / "control.pipe")

    real_scandir = os.scandir

    class _Scan:
        def __init__(self, path):
            self._path = path

        def __enter__(self):
            entries = list(real_scandir(self._path))
            if Path(self._path).resolve() == plugin_dir.resolve():
                entries.append(_FakeFifoEntry(fifo_path))
            return iter(entries)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        packaged_metadata.os, "scandir", lambda path: _Scan(path)
    )

    files, untrustworthy, _dirs = packaged_metadata._iter_source_files(plugin_dir)

    assert fifo_path not in [str(plugin_dir / real) for _key, real, _s in files], (
        "命名管道进了摘要列表，read_bytes() 会在没有写端时永久阻塞"
    )
    assert untrustworthy, (
        "非普通文件没有把这棵树标成不可信——摘要覆盖不到它，就不该拿包里的元数据当真"
    )


def test_an_oversized_metadata_file_is_refused_before_parsing(tmp_path: Path) -> None:
    """``plugin.meta.json`` comes from a third-party package; cap it.

    ``json.loads`` materialises the whole document, and registry refresh now
    holds ``_REGISTRY_REFRESH_LOCK`` across the operation, so an enormous
    metadata file in one installed package can exhaust memory while everything
    else waits on the lock (codex).

    Mutation: drop the ``MAX_PACKAGED_METADATA_BYTES`` check.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME

    # 除了体积，这份元数据其它方面完全合法——否则"被拒"可能是缺字段导致的，
    # 去掉大小闸门测试照样通过，守卫等于没守（本轮变异验证抓到过这一点）。
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None, (
        "前提没成立：这份元数据在放大之前就该是可用的"
    )
    payload["padding"] = "x" * packaged_metadata.MAX_PACKAGED_METADATA_BYTES
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    assert meta_path.stat().st_size > packaged_metadata.MAX_PACKAGED_METADATA_BYTES, (
        "前提没成立：文件没有超过上限"
    )
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "超大的第三方元数据被原样解析：刷新整段持锁，一份够大的文件能把进程撑爆"
    )


def test_binary_files_are_hashed_byte_for_byte(tmp_path: Path) -> None:
    """CR is a meaningful byte in a binary asset, not a line ending.

    Line-ending normalisation exists so a package built on Windows still
    verifies on Linux — a text-only problem. Applying it to binary assets makes
    two different files hash the same (codex).

    Mutation: normalise every file regardless of suffix.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    asset = plugin_dir / "model.bin"

    asset.write_bytes(bytes([0, 13, 10, 1]))
    with_crlf = packaged_metadata.compute_source_sha256(plugin_dir)
    asset.write_bytes(bytes([0, 10, 1]))
    with_lf = packaged_metadata.compute_source_sha256(plugin_dir)

    assert with_crlf != with_lf, (
        "两份不同的二进制资源算出了同一个摘要，改动它不会让元数据失效"
    )


def test_deleting_a_source_file_invalidates_the_metadata(tmp_path: Path) -> None:
    """A deletion leaves every surviving file untouched.

    Directory mtimes do move when an entry is removed, but only strictly-newer
    counts, so a delete landing in the same timestamp tick as the metadata write
    is invisible — this test passed locally and failed on CI for exactly that
    reason. The recorded file list settles it without depending on timing at
    all, and as a bonus makes freshness independent of the order an archive
    happens to extract in (codex).

    Mutation: drop the ``source_files`` comparison from
    ``read_packaged_metadata``.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    extra = plugin_dir / "helper.py"
    extra.write_text("HELPER = 1\n", encoding="utf-8")
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["source_sha256"] = packaged_metadata.compute_source_sha256(plugin_dir)
    payload["source_files"] = packaged_metadata.source_file_names(plugin_dir)[0]
    payload["source_bytes"] = packaged_metadata.source_stat_summary(
        plugin_dir
    ).total_bytes
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None, (
        "前提没成立：这份元数据本来就该是可用的"
    )

    extra.unlink()

    # 把 meta.json 的时间戳推到未来，关掉 mtime 那条快路径。不这样做的话，目录
    # mtime 变新也能拒掉这次读取，测试就分辨不出到底是哪条判据在起作用——而 mtime
    # 那条恰恰是不可靠的那条（本机过、CI 挂）。这里要单独证明清单校验有效。
    future = time.time() + 3600
    os.utime(meta_path, (future, future))
    assert packaged_metadata.source_stat_summary(plugin_dir).newest_mtime_ns <= (
        meta_path.stat().st_mtime_ns
    ), "前提没成立：mtime 快路径还在生效，这条守卫测不到清单校验"

    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "删掉一个源文件之后元数据仍被当成新鲜的，宿主会继续用删除前推出来的 schema"
    )

def test_mark_reports_whether_the_gate_is_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers that promote new code need to know the gate actually landed.

    ``install_builtin_override`` marks before promoting the third-party source.
    If that write is lost, the promotion would go ahead with no pending record
    and the new code autostarts unapproved at the next boot, so the mark has to
    report failure rather than only logging it (coderabbit).

    Mutation: return ``None``/``True`` unconditionally from
    ``mark_autostart_pending``.
    """
    _store_that_fails_to_write(monkeypatch, [])
    try:
        assert autostart_approvals.mark_autostart_pending("newcomer") is False
    finally:
        autostart_approvals._reset_cache_for_testing()

    store: dict[str, object] = {}

    class _WorkingConfigManager:
        def load_json_config(self, name):
            if name not in store:
                raise FileNotFoundError(name)
            return store[name]

        def save_json_config(self, name, payload):
            store[name] = payload

    import utils.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _WorkingConfigManager
    )
    autostart_approvals._reset_cache_for_testing()
    try:
        assert autostart_approvals.mark_autostart_pending("newcomer") is True
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_a_non_regular_metadata_file_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plugin.meta.json`` itself can be a named pipe.

    The regular-file check added for the source walk deliberately skips this
    file — a generated artefact does not take part in its own freshness check —
    so nothing was checking the metadata file itself. ``stat()`` succeeds on a
    FIFO and ``read_text()`` then blocks forever with no writer, while registry
    refresh holds the lock (coderabbit).

    Mutation: drop the ``stat.S_ISREG`` check.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    real_stat = Path.stat

    class _FifoStat:
        st_mode = 0o010600  # S_IFIFO
        st_size = 128
        st_mtime_ns = 1

    def _fake_stat(self, *args, **kwargs):
        if Path(self) == meta_path:
            return _FifoStat()
        return real_stat(self, *args, **kwargs)

    def _boom(*_args, **_kwargs):
        raise AssertionError("read_text() reached a non-regular metadata file")

    monkeypatch.setattr(Path, "stat", _fake_stat)
    monkeypatch.setattr(Path, "read_text", _boom)

    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None


def test_metadata_without_a_file_list_is_refused(tmp_path: Path) -> None:
    """A missing ``source_files`` must fail the read, not skip the check.

    Treating it as optional at the same ``schema_version`` means a package that
    simply omits the field keeps the old, timing-dependent behaviour: add or
    remove a source file without moving any surviving mtime past the metadata
    and the stale schema is accepted (coderabbit). The field is required, so the
    schema version carries it.

    Mutation: make the ``source_files`` check conditional on the field being
    present.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None, (
        "前提没成立：带清单的元数据本来就该可用"
    )

    payload.pop("source_files")
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "没有文件清单的元数据仍被接受，那道确定性判据就整个静默失效了"
    )


def test_a_malformed_file_list_is_refused(tmp_path: Path) -> None:
    """A malformed file list must be refused, whatever shape it takes.

    This pins behaviour, not implementation: the previous ``str()`` coercion
    rejected every case below too, because the comparison failed. Mutating the
    explicit type check away therefore does **not** turn this red, and no
    honest mutation would — the only input that separates the two is a file
    literally named ``17`` paired with the JSON number ``17``, which is
    contorting the test to fit the guard rather than testing anything worth
    protecting.

    The type check still earns its place by stating the contract for a file
    that arrives from a third-party package (coderabbit); this test guards the
    outcome that actually matters.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    good_names = list(payload["source_files"])

    for broken in ("not-a-list", {"a": 1}, good_names + [17], [None]):
        payload["source_files"] = broken
        meta_path.write_text(json.dumps(payload), encoding="utf-8")
        assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
            f"畸形的文件清单被接受了：{broken!r}"
        )

    payload["source_files"] = good_names
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None, (
        "前提没成立：合法清单本来就该通过"
    )


def test_packaged_handlers_from_another_machine_are_not_reused(
    tmp_path: Path,
) -> None:
    """The registered entry set must describe *this* machine.

    A plugin may register different entries under a different OS or Python
    version — an optional import that only resolves on Windows, an entry gated
    on ``sys.version_info``. The packaged handlers are one build machine's
    answer, and they are what lands in ``state.event_handlers``: get them wrong
    and the model calls an entry the running plugin does not have (codex). The
    display-side entries are allowed to be that snapshot; the callable set is
    not.

    Mutation: reuse the packaged handlers without checking ``build_env``.
    """
    from plugin.server.application.plugins import lifecycle_service

    foreign = dict(packaged_metadata.build_environment())
    foreign["os"] = "some-other-os"
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}], build_env=foreign)
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["handlers"] = {"demo.go": {"event_type": "plugin_entry", "id": "go"}}
    payload["entry_methods"] = {"go": "go"}
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        lifecycle_service._read_packaged_isolated_metadata(
            plugin_dir / "plugin.toml", "demo"
        )
        is None
    ), "复用了别的环境上导出的 handler：条件注册的插件会注册出这台机器上不存在的入口"

    payload["build_env"] = packaged_metadata.build_environment()
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        lifecycle_service._read_packaged_isolated_metadata(
            plugin_dir / "plugin.toml", "demo"
        )
        is not None
    ), "前提没成立：同环境的包本来就该复用"


def test_a_missing_build_env_is_not_treated_as_a_match(tmp_path: Path) -> None:
    """A package that never recorded its environment cannot claim to match.

    Mutation: treat an absent ``build_env`` as the current one.
    """
    from plugin.server.application.plugins import lifecycle_service

    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload.pop("build_env")
    payload["handlers"] = {"demo.go": {"event_type": "plugin_entry", "id": "go"}}
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        lifecycle_service._read_packaged_isolated_metadata(
            plugin_dir / "plugin.toml", "demo"
        )
        is None
    ), "没有记录打包环境的包被当成同环境，等于这道检查从来没跑过"


def test_a_verified_read_stops_the_next_one_from_re_hashing(tmp_path: Path) -> None:
    """Confirming the hash once must restore the cheap path.

    Extraction writes the files in archive order, so whichever source lands
    after ``plugin.meta.json`` is permanently newer than it. Without stamping
    the metadata, that state never resolves: every refresh re-hashes the whole
    tree of every installed plugin, and it does it while holding the registry
    lock (codex).

    Mutation: drop the ``_stamp_metadata_verified`` call after the hash matches.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    future = time.time() + 60
    os.utime(plugin_dir / "main.py", (future, future))

    hashed: list[Path] = []
    real_hash = packaged_metadata.compute_source_sha256

    def _counting(path: Path) -> str:
        hashed.append(path)
        return real_hash(path)

    original = packaged_metadata.compute_source_sha256
    packaged_metadata.compute_source_sha256 = _counting
    try:
        assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None
        assert len(hashed) == 1, "前提没成立：第一次读本来就该走内容哈希"
        assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None
        assert len(hashed) == 1, (
            "每次刷新都在持锁状态下重算整棵树的哈希：解包顺序留下的时间戳关系"
            "一直成立，慢路径再也回不去"
        )
    finally:
        packaged_metadata.compute_source_sha256 = original


def test_stamping_does_not_hide_a_later_edit(tmp_path: Path) -> None:
    """The stamp asserts what was true at that moment, not from then on.

    Mutation: stamp with a far-future timestamp instead of the sources' own.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    future = time.time() + 60
    os.utime(plugin_dir / "main.py", (future, future))
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None

    (plugin_dir / "main.py").write_text("VALUE = 3\n", encoding="utf-8")
    later = time.time() + 3600
    os.utime(plugin_dir / "main.py", (later, later))
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "盖过时间戳之后源码再改也不再被发现，作者改完签名看不到任何变化"
    )


def test_a_same_mtime_rewrite_is_caught_by_size(tmp_path: Path) -> None:
    """Content can change without the timestamp moving.

    A metadata-preserving restore, or an edit inside one tick of a coarse
    filesystem clock, leaves the path set identical and the mtime no newer, so
    the timestamp fast path would keep serving the packaged handlers forever
    (codex). Sizes come from the stat walk that already runs.

    Mutation: drop ``source_bytes`` from the freshness check.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    stamp = meta_path.stat()
    source = plugin_dir / "main.py"
    before = source.stat()

    source.write_text("VALUE = 1 + 1 + 1\n", encoding="utf-8")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.utime(plugin_dir, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    summary = packaged_metadata.source_stat_summary(plugin_dir)
    assert summary.newest_mtime_ns <= meta_path.stat().st_mtime_ns, (
        "前提没成立：改完之后时间戳还是变新了，这个用例就没有在测尺寸那条路"
    )
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "源码换了内容但时间戳没动，宿主继续端着上一版的 schema"
    )


def test_the_fingerprint_uses_one_spelling_for_a_decomposed_name(
    tmp_path: Path,
) -> None:
    """NFC and NFD spellings of one filename must fingerprint the same.

    The package exporter writes archive names in NFC. A macOS filesystem hands
    back the decomposed form, so recording the raw spelling makes the extracted
    tree's file list and digest disagree with the packaged ones on every read —
    the plugin permanently loses its static schemas (codex).

    Mutation: record ``rel_path`` instead of its NFC form.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "café.py")
    composed = unicodedata.normalize("NFC", "café.py")
    assert decomposed != composed, "前提没成立：这两种拼写在字节上是一样的"

    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    try:
        (plugin_dir / decomposed).write_text("VALUE = 1\n", encoding="utf-8")
    except OSError:
        pytest.skip("this filesystem cannot hold a decomposed filename")

    names, _untrustworthy = packaged_metadata.source_file_names(plugin_dir)
    assert composed in names, f"记录的是分解形式，和包里的档案名对不上：{names}"
    # 摘要必须真的能读到文件——归一化后的名字在保留原拼写的文件系统上打不开。
    assert packaged_metadata.compute_source_sha256(plugin_dir)


def test_a_config_declared_entry_table_wins_over_the_package(
    tmp_path: Path,
) -> None:
    """Discovery previews must describe the plugin *this* machine would run.

    Packaging reads the author's ``plugin.toml`` and never sees the user's
    runtime config or activated profile. When those declare their own entries,
    publishing the packaged list shows a stopped plugin with the wrong entry ids
    and schemas until it is started (codex). The start path already made this
    call; the discovery path had no equivalent.

    Mutation: drop the ``config_declares_entries`` check from
    ``_packaged_entries_preview``.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "packaged_only"}])

    class _Ctx:
        toml_path = plugin_dir / "plugin.toml"
        conf: dict = {}
        pdata: dict = {"entries": {"from_config": {"description": "x"}}}

    preview = module._packaged_entries_preview(_Ctx(), "demo")
    ids = [entry.get("id") for entry in preview]
    assert "packaged_only" not in ids, (
        f"生效配置自己声明了 entries，发现侧却还在端打包机那份：{ids}"
    )


def test_a_v3_package_without_a_byte_total_is_refused(tmp_path: Path) -> None:
    """The size field is part of the schema, not an optional extra.

    Skipping the comparison when the field is missing or the wrong type puts
    the decision back on mtime alone — and mtime being unreliable is the whole
    reason the field exists (coderabbit). Same judgement as ``source_files``.

    Mutation: treat a missing or non-int ``source_bytes`` as "no size change".
    """
    for bad in (None, "1234", 12.5, True):
        plugin_dir = _write_plugin(
            tmp_path / str(bad), entries=[{"id": "go"}]
        )
        meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        if bad is None:
            payload.pop("source_bytes")
        else:
            payload["source_bytes"] = bad
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
            f"source_bytes={bad!r} 被放过了，尺寸比对静默失效，判定退回只看 mtime"
        )


def test_a_bundled_node_modules_makes_the_tree_untrustworthy(tmp_path: Path) -> None:
    """What ships must be fingerprinted, or the metadata must not be trusted.

    ``node_modules`` is not excluded by any packaging rule, so it goes into the
    package — while the fingerprint walk skips it. A plugin that reads a bundled
    JS file or package manifest while registering entries could then change that
    file with no visible effect (codex). Walking a whole npm tree on every
    refresh is the cost this module exists to avoid, so the third option is
    taken: say so, and let the plugin fall back to the manifest.

    Mutation: put ``node_modules`` back in ``SOURCE_IGNORED_DIRS``.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None, (
        "前提没成立：这棵树本来就不该被信任"
    )

    bundled = plugin_dir / "node_modules" / "left-pad"
    bundled.mkdir(parents=True)
    (bundled / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")

    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "捆进包里的 node_modules 不进指纹却照样发布元数据：改了里面的文件，"
        "宿主一点都看不见"
    )


def test_dev_only_directories_stay_out_of_the_fingerprint(tmp_path: Path) -> None:
    """Pruning must stay cheap for what packaging never ships.

    Mutation: drop ``__pycache__`` from ``SOURCE_IGNORED_DIRS`` — every plugin
    would then be re-fingerprinted whenever Python rewrote a .pyc.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    cache = plugin_dir / "__pycache__"
    cache.mkdir()
    (cache / "main.cpython-311.pyc").write_bytes(bytes([0, 1]))

    assert packaged_metadata.read_packaged_metadata(plugin_dir) is not None, (
        "开发产物把元数据判废了：这些目录根本不会进包"
    )


def test_deeply_nested_metadata_falls_back_instead_of_failing(
    tmp_path: Path,
) -> None:
    """A third-party file must not be able to break discovery.

    ``json.loads`` raises ``RecursionError`` — not a ``ValueError`` — on a deep
    enough document, and it fits well under the size cap. Uncaught, discovery
    records the whole plugin as failed instead of taking the documented manifest
    fallback (codex).

    Mutation: catch only ``(OSError, ValueError)``.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    depth = sys.getrecursionlimit() * 3
    meta_path.write_text("[" * depth + "]" * depth, encoding="utf-8")

    assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
        "嵌套过深的第三方 plugin.meta.json 把整个插件搞成了失败，而不是回落 manifest"
    )


def test_a_packaged_plugin_with_no_entries_still_skips_the_scan(
    tmp_path: Path,
) -> None:
    """An empty handler set is an answer, not a missing answer.

    A background-only plugin registers nothing. Treating its empty ``handlers``
    as "no metadata" sent it back through the worker, so starting it imported
    the module twice — once for the scan, once for the host — and any
    module-level side effect happened twice (codex). Schema v3 always writes the
    key, and v1/v2 were never released, so there is no older package to protect.

    Mutation: fall back when ``handlers`` is empty.
    """
    from plugin.server.application.plugins import lifecycle_service

    plugin_dir = _write_plugin(tmp_path, entries=[])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["handlers"] = {}
    payload["entry_methods"] = {}
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = lifecycle_service._read_packaged_isolated_metadata(
        plugin_dir / "plugin.toml", "demo"
    )
    assert recovered is not None, (
        "没有入口的插件被当成没有元数据：启动它会把模块 import 两遍，"
        "模块级副作用跟着做两遍"
    )
    assert recovered.handlers == {}


def test_a_malformed_handler_table_is_refused(tmp_path: Path) -> None:
    """"Empty" and "malformed" must not collapse into the same answer.

    Now that an empty ``handlers`` mapping is trusted as an authoritative "this
    plugin registers nothing", coercing a missing or non-object table into an
    empty one would let a broken package start a plugin with its tools
    advertised in ``entries`` but nothing dispatchable behind them (codex).

    Mutation: coerce the tables instead of validating them.
    """
    cases = [
        ("handlers", None, "missing"),
        ("handlers", [], "list"),
        ("handlers", {"demo.go": "not-a-mapping"}, "string value"),
        ("entry_methods", 5, "int"),
        ("entry_methods", {"go": 1}, "non-string value"),
        ("entries", {}, "mapping instead of list"),
    ]
    for index, (field, value, label) in enumerate(cases):
        plugin_dir = _write_plugin(tmp_path / f"case{index}", entries=[{"id": "go"}])
        meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload.setdefault("handlers", {})
        payload.setdefault("entry_methods", {})
        if value is None:
            payload.pop(field, None)
        else:
            payload[field] = value
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
            f"{field} 是 {label} 却被当成合法的空表：插件会带着一份广告了工具、"
            "却一个 handler 都注册不上的元数据跑起来"
        )


def test_every_pruned_directory_is_one_packaging_drops(tmp_path: Path) -> None:
    """The fingerprint may only skip what no package can contain.

    ``SOURCE_IGNORED_DIRS`` is justified entirely by "packaging never ships
    these", and that justification is only true as long as both pipelines
    actually exclude them. ``.ruff_cache`` sat in the ignore set while neither
    rule set excluded it, so it shipped inside packages and stayed outside every
    fingerprint (codex). Rather than re-checking by hand, this pins the subset
    relation.

    Mutation: put a name in ``SOURCE_IGNORED_DIRS`` that packaging ships.
    """
    from plugin.neko_plugin_cli.core import build_rules
    from plugin.neko_plugin_cli.public import pack_rules

    build_excluded = build_rules._DEFAULT_EXCLUDE_DIR_NAMES
    pack_excluded = pack_rules._DEFAULT_EXCLUDE_DIR_NAMES
    shipped = packaged_metadata.SOURCE_IGNORED_DIRS - (build_excluded & pack_excluded)
    assert not shipped, (
        f"这些目录会被打进包，却不进指纹也不让整棵树失效：{sorted(shipped)}。"
        "要么两条打包管线都排除它们，要么把它们移进 SOURCE_UNFINGERPRINTABLE_DIRS"
    )


def test_a_manifest_declared_entry_table_is_not_an_override(tmp_path: Path) -> None:
    """A plugin declaring its own entries must still get its packaged schemas.

    ``conf``/``pdata`` carry the *effective* configuration, which already
    includes the package's own ``entries`` table. Testing for the presence of
    that table labelled every such plugin as user-overridden: previews lost the
    schemas derived at build time and every start re-imported the module
    (codex). The comparison is against what the package was built from.

    Mutation: go back to "does an entries table exist".
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    declared = {"go": {"description": "from the manifest"}}
    payload["entries_config_sha256"] = packaged_metadata.entries_config_digest(
        {"entries": declared}, {}
    )
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    packaged = packaged_metadata.read_packaged_metadata(plugin_dir)
    assert packaged is not None

    assert not module.config_overrides_packaged_entries(
        {"entries": declared}, {}, packaged
    ), "插件自己 manifest 里声明的 entries 被当成用户覆盖，打包期的 schema 全丢了"


def test_an_emptied_entry_table_is_an_override(tmp_path: Path) -> None:
    """``entries = []`` removes them, and removal is a change.

    An overlay can set an empty list to drop the manifest's entries;
    ``deep_merge`` keeps that empty list. A truthiness test reads it as "no
    override" and keeps serving the packaged entries the configuration just
    removed (codex).

    Mutation: compare truthiness instead of digests.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["entries_config_sha256"] = packaged_metadata.entries_config_digest(
        {"entries": {"go": {}}}, {}
    )
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    packaged = packaged_metadata.read_packaged_metadata(plugin_dir)
    assert packaged is not None

    assert module.config_overrides_packaged_entries({"entries": []}, {}, packaged), (
        "配置显式清空了 entries，宿主还在端着包里那份被删掉的入口表"
    )


def test_metadata_without_a_valid_digest_is_refused(tmp_path: Path) -> None:
    """No digest means nothing to verify against, ever.

    The fast path (file list, byte total, timestamps) can all agree while the
    content check never runs, so a package with a missing or malformed
    ``source_sha256`` would install authoritative handlers having never been
    verified at all (codex).

    Mutation: keep coercing ``source_sha256`` with ``str(...)``.
    """
    for bad in (None, "", "not-a-digest", "ABC" * 21 + "D", 12345):
        plugin_dir = _write_plugin(
            tmp_path / str(bad)[:12], entries=[{"id": "go"}]
        )
        meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        if bad is None:
            payload.pop("source_sha256")
        else:
            payload["source_sha256"] = bad
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
            f"source_sha256={bad!r} 也被接受了：这份元数据从头到尾没做过内容校验"
        )


def test_an_emptied_entry_table_hides_the_manifest_entries(tmp_path: Path) -> None:
    """Removing entries must actually remove them from what the UI shows.

    Detecting the override was only half of it: the fallback path selected the
    table with ``conf.get("entries") or pdata.get("entries")``, so an explicit
    empty list fell through to the manifest and the registry kept advertising
    the entries the configuration had removed (coderabbit).

    Mutation: go back to the ``or`` chain in ``_effective_entries``.
    """
    from plugin.core.registry import _extract_entries_preview

    manifest_entries = [{"id": "go", "description": "from the manifest"}]
    stub = type("Stub", (), {})

    kept = _extract_entries_preview("demo", stub, {}, {"entries": manifest_entries})
    assert [entry["id"] for entry in kept] == ["go"], (
        "前提没成立：manifest 里声明的入口本来就该显示出来"
    )

    removed = _extract_entries_preview(
        "demo", stub, {"entries": []}, {"entries": manifest_entries}
    )
    assert removed == [], (
        f"配置把入口删空了，注册表还在展示 manifest 里那份：{removed}"
    )


def test_a_lying_byte_total_is_refused_not_re_hashed(tmp_path: Path) -> None:
    """A package must not be able to make every refresh hash its whole tree.

    A wrong ``source_bytes`` next to a valid digest kept the slow path armed
    forever: the mismatch is true on every refresh, and each one re-reads the
    whole tree (packages may carry up to 1 GiB) while holding the registry lock
    — no plugin code required (codex). If the digest matches, the stated size is
    simply false, and a package that misdescribes itself is one to fall back on.

    Mutation: accept the metadata and only re-stamp the timestamp.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    meta_path = plugin_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["source_bytes"] = payload["source_bytes"] + 4096
    meta_path.write_text(json.dumps(payload), encoding="utf-8")

    hashed: list[Path] = []
    real_hash = packaged_metadata.compute_source_sha256
    packaged_metadata.compute_source_sha256 = lambda path: (
        hashed.append(path) or real_hash(path)
    )
    try:
        assert packaged_metadata.read_packaged_metadata(plugin_dir) is None, (
            "字节数和这棵树对不上还被接受了"
        )
        assert packaged_metadata.read_packaged_metadata(plugin_dir) is None
        assert hashed == [], (
            "拒绝发生在整树哈希之后：拦住的是结论，拦不住开销——而这条意见针对的"
            f"就是开销：{hashed}"
        )
    finally:
        packaged_metadata.compute_source_sha256 = real_hash


def test_a_nested_metadata_file_is_fingerprinted(tmp_path: Path) -> None:
    """Only the root ``plugin.meta.json`` is the generated artifact.

    A plugin can ship ``data/plugin.meta.json`` as a runtime file, and both
    packaging pipelines copy it. Skipping every file with that name left it
    outside the fingerprint while it shipped, so editing it kept stale handlers
    alive (codex).

    Mutation: skip the name at any depth again.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    nested_dir = plugin_dir / "data"
    nested_dir.mkdir()
    nested = nested_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    nested.write_text('{"payload": 1}', encoding="utf-8")

    names, _untrustworthy = packaged_metadata.source_file_names(plugin_dir)
    assert "data/plugin.meta.json" in names, (
        f"插件自己带的运行时文件被当成生成物排除了：{names}"
    )
    assert packaged_metadata.PACKAGED_METADATA_FILENAME not in names, (
        "根部那份生成物不该参与它自己的新鲜度判定"
    )


def test_a_decomposed_filename_is_reported_by_spelling(tmp_path: Path) -> None:
    """The NFC check compares spellings, not path existence.

    macOS resolves canonically equivalent names, so ``(dir / nfc_name).exists()``
    is true even when the file on disk is decomposed — the check would never
    fire on the filesystems it exists for (codex).

    ⚠️ The ``exists()`` mutation survives this test *on NTFS and ext4*, and that
    is not a weakness worth contorting the test to hide: those filesystems do
    not resolve canonical equivalence, so both implementations answer the same
    there. The difference is observable only on the filesystem the bug is about.
    What this does pin is the output shape — a mutant returning ``[]``, or
    comparing the wrong pair of names, dies here.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFD", "café.py")
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text("id = 'demo'" + chr(10), encoding="utf-8")
    try:
        (plugin_dir / decomposed).write_text("VALUE = 1", encoding="utf-8")
    except OSError:
        pytest.skip("this filesystem cannot hold a decomposed filename")
    if not any(
        entry.name == decomposed for entry in plugin_dir.iterdir()
    ):  # pragma: no cover - filesystem normalises on write
        pytest.skip("this filesystem normalises filenames on write")

    renamed = packaged_metadata.unicode_renamed_source_files(plugin_dir)
    assert renamed == [unicodedata.normalize("NFC", "café.py")], (
        f"分解形式的文件名没被报出来：{renamed}"
    )
    assert packaged_metadata.unicode_renamed_source_files(
        plugin_dir / "does-not-exist"
    ) == []


def test_a_bare_cr_rewrite_changes_the_digest(tmp_path: Path) -> None:
    """Line-ending normalisation must not fold two different files together.

    Folding bare CR into LF made "replace every LF with a CR" hash identically
    to the original: same paths, same byte count, same digest — the slow path
    could not catch it either (codex). git only ever translates between LF and
    CRLF, so dropping that fold costs nothing it was added for.

    Mutation: normalise bare CR to LF again.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    source = plugin_dir / "main.py"
    source.write_bytes(b"A = 1\nB = 2\n")
    lf_digest = packaged_metadata.compute_source_sha256(plugin_dir)

    source.write_bytes(b"A = 1\r\nB = 2\r\n")
    assert packaged_metadata.compute_source_sha256(plugin_dir) == lf_digest, (
        "前提没成立：CRLF 和 LF 本来就该算出同一个摘要，否则 Windows 上打的包"
        "到 Linux 上会条条失效"
    )

    source.write_bytes(b"A = 1\rB = 2\r")
    assert packaged_metadata.compute_source_sha256(plugin_dir) != lf_digest, (
        "把每个 LF 换成裸 CR 之后摘要没变：内容真的改了，宿主却还端着旧 schema"
    )


def test_an_empty_directory_is_reported_before_packaging(tmp_path: Path) -> None:
    """A directory with no files cannot survive the archive.

    ZIP stores files, so an empty staged directory never reaches the installed
    tree — while the fingerprint, which covers files, still matches. A plugin
    that registers entries based on a directory's presence gets probed with it
    and runs without it (codex).

    Mutation: report only directories directly under the root, or none at all.
    """
    plugin_dir = _write_plugin(tmp_path, entries=[{"id": "go"}])
    assert packaged_metadata.empty_source_directories(plugin_dir) == [], (
        "前提没成立：这棵树本来就没有空目录"
    )

    (plugin_dir / "runtime" / "logs").mkdir(parents=True)
    (plugin_dir / "assets").mkdir()
    (plugin_dir / "assets" / "icon.txt").write_text("x", encoding="utf-8")

    reported = packaged_metadata.empty_source_directories(plugin_dir)
    assert reported == ["runtime", "runtime/logs"], (
        f"空目录没被完整报出来（装不到用户机器上的正是它们）：{reported}"
    )
