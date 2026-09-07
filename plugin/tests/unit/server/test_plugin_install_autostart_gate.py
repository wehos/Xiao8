"""Every install path withholds autostart until the user starts the plugin.

The gate exists because ``plugin_runtime.auto_start`` defaults to true and is
declared by the plugin itself: without it, a freshly installed plugin runs its
own module-level code at the next greeting without ever having been started.

The guard here is on ``install()`` rather than on any one source-recording
helper. Hanging it off ``_record_requested_install_source`` looked equivalent
and was not: ``upload_and_install`` records its source separately and never
calls that helper, so uploaded plugins — the ones most worth gating — skipped
the check entirely.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.neko_plugin_cli.core.metadata_probe import write_packaged_metadata
from plugin.server.application.plugin_cli import service as cli_service
from plugin.server.infrastructure import packaged_metadata

pytestmark = pytest.mark.plugin_unit


@pytest.mark.asyncio
async def test_a_plain_install_marks_the_plugin_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh-install exit of ``install()`` records the plugin.

    Mutation: move the call back into ``_record_requested_install_source``.
    """
    marked: list[dict] = []
    monkeypatch.setattr(
        cli_service, "_mark_new_install_awaiting_autostart", marked.append
    )
    monkeypatch.setattr(cli_service, "get_install_source_manager", lambda: None)

    install_result = {"installed_plugins": [{"plugin_id": "brand_new"}]}
    service = cli_service.PluginCliService()

    async def _plan_install(**_kwargs):
        return {"action": "install"}

    monkeypatch.setattr(service, "plan_install", _plan_install)
    monkeypatch.setattr(service, "_install_sync", lambda **_kwargs: install_result)

    async def _record(*, install_result, package, source):
        return install_result

    monkeypatch.setattr(service, "_record_requested_install_source", _record)

    await service.install(package="whatever.neko-plugin")

    assert marked == [install_result], (
        "全新安装没有登记待批准，插件会在下一次开机自己跑起来"
    )


@pytest.mark.asyncio
async def test_the_mark_lands_before_any_source_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate must not depend on which source-recording path an install takes.

    ``upload_and_install`` records its own install source and never calls
    ``_record_requested_install_source``; the first version of this gate lived
    inside that helper and so did nothing for uploaded plugins. Rather than
    driving the whole upload stack, this pins the invariant that made it break:
    the plugin is marked *before* source recording, so a caller that records
    its source elsewhere is still covered.

    Mutation: move the call back into ``_record_requested_install_source`` —
    this test fails while the plain-install one above still passes.
    """
    marked: list[dict] = []
    monkeypatch.setattr(
        cli_service, "_mark_new_install_awaiting_autostart", marked.append
    )
    monkeypatch.setattr(cli_service, "get_install_source_manager", lambda: None)

    install_result = {"installed_plugins": [{"plugin_id": "uploaded_one"}]}
    service = cli_service.PluginCliService()

    async def _plan_install(**_kwargs):
        return {"action": "install"}

    monkeypatch.setattr(service, "plan_install", _plan_install)
    monkeypatch.setattr(service, "_install_sync", lambda **_kwargs: install_result)

    # 上传路径自己登记来源，走的是 _record_install_source_best_effort，
    # 完全不经过 _record_requested_install_source。
    async def _record(*, install_result, package, source):
        raise AssertionError(
            "前提没成立：这条路本不该经过 _record_requested_install_source"
        )

    monkeypatch.setattr(service, "_record_requested_install_source", _record)

    with pytest.raises(AssertionError):
        await service.install(package="uploaded.neko-plugin", install_source=None)

    assert marked == [install_result], (
        "登记发生在来源登记之后：上传安装的插件因此完全绕过了这道闸"
    )


def _installed(tmp_path: Path, directory_name: str, manifest_id: str) -> dict:
    """One ``installed_plugins`` row, with a real manifest on disk behind it."""
    target = tmp_path / directory_name
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.toml").write_text(
        "\n".join(["[plugin]", f"id = '{manifest_id}'", ""]), encoding="utf-8"
    )
    # 安装结果里没有 plugin_id 字段：InstalledPlugin 带的是 target_plugin_id，
    # 而那个值就是目录名。
    return {"target_plugin_id": directory_name, "target_dir": str(target)}


def test_the_gate_does_not_consult_the_registry_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Is this plugin new?" is the install plan's answer, not the registry's.

    A refresh that overlaps the install can register the freshly written
    directory before the gate runs; if the gate asked ``state.plugins`` it would
    see the plugin as pre-existing and skip it, so a race would silently grant
    autostart to a plugin the user never started (greptile). A stale registry
    produces the mirror error (codex). Upgrades are excluded at the call site
    instead — the replace exit does not call this at all.

    Mutation: re-add an ``already_known`` check against ``state.plugins``.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "plugin.server.infrastructure.autostart_approvals.mark_autostart_pending",
        calls.append,
    )
    from plugin.core.state import state

    # 并发刷新已经把它登记进注册表了——这不该让它逃过批准闸。
    monkeypatch.setattr(
        state, "plugins", {"already_seen": {"id": "already_seen"}}, raising=False
    )

    cli_service._mark_new_install_awaiting_autostart(
        {"installed_plugins": [_installed(tmp_path, "already_seen", "already_seen")]}
    )

    assert calls == ["already_seen"], (
        f"并发刷新抢先登记之后，这个插件就绕过了批准闸：{calls}"
    )


def test_an_upgrade_never_reaches_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replace exit must not re-gate a plugin the user already runs.

    Mutation: call ``_mark_new_install_awaiting_autostart`` from the replace
    exit as well.
    """
    import inspect

    source = inspect.getsource(cli_service.PluginCliService.install)
    marks = source.count("_mark_new_install_awaiting_autostart")
    assert marks == 1, (
        f"install() 里登记待批准的调用点有 {marks} 个；升级路径也登记的话，"
        "用户早就在用的插件会因为一次升级失去自启动资格"
    )


def test_a_builtin_override_install_is_gated_too() -> None:
    """Overriding a builtin swaps trusted code for uploaded code.

    ``install_builtin_override`` is a separate entry point — ``upload_and_install``
    calls it directly for ``override_builtin`` packages, so it never passes
    through ``install()``. The id existed before, as a builtin, and therefore
    already carries autostart eligibility; without gating, one override install
    makes never-started third-party code run at the next startup (greptile).

    Mutation: drop the ``_mark_new_install_awaiting_autostart`` call from
    ``install_builtin_override``.
    """
    import inspect

    source = inspect.getsource(cli_service.PluginCliService.install_builtin_override)
    assert "mark_autostart_pending" in source, (
        "覆盖安装没有登记待批准：一次覆盖就能让未经启动的第三方代码在下次开机自动执行"
    )


def test_the_gate_records_the_manifest_id_not_the_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry keys plugins by their manifest id, so the gate must too.

    ``InstalledPlugin`` only carries ``target_plugin_id``, which is the
    directory name; this repo supports a directory whose name differs from
    ``[plugin].id``. Recording the directory name means the autostart filter
    looks up an id that was never registered and the gate silently does nothing
    (coderabbit).

    Mutation: drop ``_installed_manifest_plugin_id`` and fall back to the
    directory name.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "plugin.server.infrastructure.autostart_approvals.mark_autostart_pending",
        calls.append,
    )
    from plugin.core.state import state

    monkeypatch.setattr(state, "plugins", {}, raising=False)

    cli_service._mark_new_install_awaiting_autostart(
        {"installed_plugins": [_installed(tmp_path, "some_folder_2", "real_plugin_id")]}
    )

    assert calls == ["real_plugin_id"], (
        f"登记的是目录名而不是 manifest 里的 id，这道闸对该插件完全不生效：{calls}"
    )


def test_packaging_probes_the_tree_it_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import and fingerprint must land on the staged copy, not the source.

    Build rules can exclude a file that decides which entries get registered.
    Importing the author's tree while fingerprinting the staged one produces a
    package carrying handlers derived from a file it does not contain — and the
    host's verification passes, because the hash was taken on what shipped
    (codex). So the probe reads the staged tree too.

    Mutation: probe ``source_dir`` and fingerprint ``target_dir``, the split
    this replaces.
    """
    from plugin.neko_plugin_cli.core import metadata_probe

    source_dir = tmp_path / "src"
    staged_dir = tmp_path / "staged"
    source_dir.mkdir()
    staged_dir.mkdir()
    for directory in (source_dir, staged_dir):
        (directory / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
        (directory / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    # 只在源目录里、被构建规则排除在包外的文件。
    (source_dir / "dev_only.py").write_text("SECRET = 2\n", encoding="utf-8")
    assert packaged_metadata.compute_source_sha256(
        source_dir
    ) != packaged_metadata.compute_source_sha256(staged_dir), (
        "前提没成立：两棵树内容一样，这条守卫证明不了任何事"
    )

    probed: list[Path] = []

    class _Ctx:
        pid = "demo"
        entry = "demo.main:Plugin"
        conf: dict = {}
        pdata: dict = {}
        python_requirement_paths: list = []

    class _Isolated:
        entries_preview: list = []
        handlers: dict = {}
        entry_methods: dict = {}

    def _record_ctx(config_path, processed, logger, *, apply_user_overlays=True):
        probed.append(Path(config_path).parent)
        # 打包必须不带用户覆盖，否则作者机器上的 profile 会被导出去。
        assert apply_user_overlays is False, (
            "打包期解析带上了用户覆盖：作者的 profile 会被写进发出去的元数据"
        )
        return _Ctx()

    monkeypatch.setattr(
        "plugin.core.registry._parse_single_plugin_config", _record_ctx
    )
    monkeypatch.setattr(
        "plugin.server.application.plugins.metadata_scanner"
        ".scan_plugin_metadata_isolated",
        lambda **_kwargs: _Isolated(),
    )

    written = write_packaged_metadata(source_dir=source_dir, target_dir=staged_dir)
    assert written is not None
    payload = json.loads(written.read_text(encoding="utf-8"))

    assert probed == [staged_dir.resolve()], (
        f"import 的是作者的源目录：包里可能带上一个自己都没装的文件推出来的 "
        f"handler，而哈希算的是装出来的那份，宿主校验还会通过：{probed}"
    )
    assert payload["source_sha256"] == packaged_metadata.compute_source_sha256(
        staged_dir
    ), "摘要算的是作者的源目录，用户机器上哈希的却是装出来的那份，两边永远对不上"
    assert "dev_only.py" not in payload["source_files"], (
        f"文件清单里有一个根本没进包的文件：{payload['source_files']}"
    )


def test_a_stale_metadata_file_that_cannot_be_removed_fails_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better no package than a package that lies about its own contents.

    The exporter archives whatever is in the staging directory. If the probe
    failed and the stale copy survives, the package ships an earlier build's
    handlers while the warning claims it shipped none (codex).

    Mutation: warn and return ``None`` instead of raising.
    """
    from plugin.neko_plugin_cli.core import metadata_probe

    source = tmp_path / "src"
    source.mkdir()
    (source / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    target = tmp_path / "staged"
    target.mkdir()
    stale = target / packaged_metadata.PACKAGED_METADATA_FILENAME
    stale.write_text('{"schema_version": 1}', encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise metadata_probe.MetadataProbeError("optional dependency missing")

    monkeypatch.setattr(metadata_probe, "derive_plugin_metadata", _boom)

    def _refuse_unlink(_self):
        raise PermissionError("read-only attribute")

    monkeypatch.setattr(Path, "unlink", _refuse_unlink)

    with pytest.raises(metadata_probe.MetadataProbeError) as excinfo:
        write_packaged_metadata(source_dir=source, target_dir=target)
    assert "stale" in str(excinfo.value)


def test_reload_counts_as_the_user_starting_a_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reload is a button the user presses, and it works on a stopped plugin.

    ``reload_plugin`` stops then starts; the frontend offers Reload even while a
    plugin is stopped. Starting a pending plugin that way is the same act as
    pressing Start, so it has to clear the pending record — otherwise the plugin
    can be run by hand forever and still never autostart (codex).

    Mutation: drop ``persist_user_intent=True`` from ``reload_plugin``.
    """
    import inspect

    from plugin.server.application.plugins import lifecycle_service

    source = inspect.getsource(lifecycle_service.PluginLifecycleService.reload_plugin)
    assert "persist_user_intent=True" in source, (
        "reload 启动插件时没有带用户意图，待批准记录不会被清掉"
    )


def test_renaming_clears_the_pending_record_under_the_old_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approval must follow the plugin across an id-conflict rename.

    Installation records the manifest id, but a plugin can register under a
    conflict-resolved runtime id. Clearing only the runtime id leaves the old
    entry behind, and once the conflict goes away that stale record blocks
    autostart forever (coderabbit).

    Mutation: drop the ``previous_plugin_ids`` loop.
    """
    from plugin.server.application.plugins import lifecycle_service

    cleared: list[str] = []

    def _clear(plugin_id: str) -> bool:
        cleared.append(plugin_id)
        return True

    monkeypatch.setattr(lifecycle_service, "clear_autostart_pending", _clear)
    monkeypatch.setattr(
        lifecycle_service, "migrate_runtime_override", lambda *a, **k: None
    )
    monkeypatch.setattr(lifecycle_service, "set_runtime_override", lambda *a, **k: None)

    lifecycle_service._persist_user_runtime_intent(
        "demo_1", True, previous_plugin_ids=("demo",)
    )

    assert cleared == ["demo_1", "demo"], (
        f"改名前的 id 没被一起清掉，冲突消失后它会继续挡着自启：{cleared}"
    )


def test_metadata_is_obtained_before_the_host_process_starts(tmp_path: Path) -> None:
    """The metadata import must not run concurrently with the plugin's own.

    ``start_plugin`` starts the real process, which imports the plugin. Doing
    the metadata import after that means two concurrent imports of the same
    module: a plugin that takes a file lock, binds a port or starts a singleton
    at import time fails the second one, lifecycle cleanup kills the healthy
    host, and the start is reported as failed (codex). Before this PR the scan
    happened inside ``refresh_plugin``, ahead of the host; refresh no longer
    scans, so the ordering has to be restored here.

    Mutation: move the metadata block back below ``_start_host_with_timeout``.
    """
    import inspect

    from plugin.server.application.plugins import lifecycle_service

    source = inspect.getsource(lifecycle_service.PluginLifecycleService.start_plugin)
    metadata_at = source.find("_read_packaged_isolated_metadata")
    host_start_at = source.find("_start_host_with_timeout(")
    clamp_at = source.find("startup_timeout_value = _clamp_step_timeout(")
    assert -1 not in (metadata_at, host_start_at, clamp_at), "前提没成立：三个点都要在"
    assert metadata_at < host_start_at, (
        "元数据 import 排在 host 启动之后，会和插件进程自己的 import 并发"
    )
    # 取元数据自己要花时间（最多一个 scan_timeout），所以启动上限必须在它之后再
    # 算——算在前面的话，等真正启动时那个上限已经是过期快照，reload 的启动阶段
    # 会比设计值多出"每个插件一次扫描"。
    assert metadata_at < clamp_at < host_start_at, (
        "启动超时的钳位没有夹在取元数据和启动之间，算出来的是过期预算"
    )


def test_the_packaged_metadata_file_is_staged_only_once(tmp_path: Path) -> None:
    """Repo plugins already ship the file, so copying then writing double-counts.

    ``PayloadBuildResult`` sorts its file list but does not de-duplicate, so a
    path recorded twice inflates ``staged_file_count`` and makes
    ``--keep-staging`` list the same file twice (coderabbit).

    Mutation: append unconditionally instead of going through
    ``_record_staged_file``.
    """
    from plugin.neko_plugin_cli.core import build as build_module

    already = tmp_path / "plugin.meta.json"
    already.write_text("{}", encoding="utf-8")
    staged = [already]

    build_module._record_staged_file(staged, already)
    assert staged == [already], f"同一个文件被记了两次：{staged}"

    another = tmp_path / "main.py"
    another.write_text("", encoding="utf-8")
    build_module._record_staged_file(staged, another)
    assert staged == [already, another], "新文件反而没被记上"


def test_approval_is_not_granted_when_the_preference_fails_to_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start that could not be recorded must not grant autostart.

    If the runtime override write fails, the call raises and is reported as
    ``partial_success`` — but that machine now has no user override, so after a
    restart the registry falls back to the manifest defaults, where both
    ``enabled`` and ``auto_start`` are true. Clearing the pending record first
    would hand out a permanent autostart approval on the strength of an intent
    that never landed (greptile).

    Failing closed here costs nothing a user had: pending records only exist for
    freshly installed plugins, which never autostarted in the first place.

    Mutation: move the ``clear_autostart_pending`` calls back above the ``try``.
    """
    from plugin.server.application.plugins import lifecycle_service
    from plugin.server.domain.errors import ServerDomainError
    from plugin.server.infrastructure.runtime_overrides import (
        RuntimeOverridePersistenceError,
    )

    cleared: list[str] = []

    def _clear(plugin_id: str) -> bool:
        cleared.append(plugin_id)
        return True

    monkeypatch.setattr(lifecycle_service, "clear_autostart_pending", _clear)

    def _boom(*_args, **_kwargs):
        raise RuntimeOverridePersistenceError("disk said no")

    monkeypatch.setattr(lifecycle_service, "set_runtime_override", _boom)
    monkeypatch.setattr(lifecycle_service, "migrate_runtime_override", _boom)

    with pytest.raises(ServerDomainError):
        lifecycle_service._persist_user_runtime_intent("brand_new", True)

    assert cleared == [], (
        "偏好没写成却已经把批准位清了，重启后这个插件会凭 manifest 默认值自启"
    )


def test_an_unpersisted_approval_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start whose approval did not reach disk must not look fully persisted.

    The runtime preference half can succeed while the approval file cannot be
    written. The plugin then stays pending, so the autostart filter holds it
    back again after a restart — and if this returned quietly the response would
    still say ``preference_persisted=true``, leaving the user with no
    explanation (greptile). It goes out through the same channel as a failed
    preference write, which callers downgrade to ``partial_success`` rather than
    failing the start.

    Mutation: ignore ``clear_autostart_pending``'s return value.
    """
    from plugin.server.application.plugins import lifecycle_service
    from plugin.server.domain.errors import ServerDomainError

    monkeypatch.setattr(
        lifecycle_service, "clear_autostart_pending", lambda plugin_id: False
    )
    monkeypatch.setattr(lifecycle_service, "set_runtime_override", lambda *a, **k: None)
    monkeypatch.setattr(
        lifecycle_service, "migrate_runtime_override", lambda *a, **k: None
    )

    with pytest.raises(ServerDomainError) as excinfo:
        lifecycle_service._persist_user_runtime_intent("stuck", True)

    assert excinfo.value.code == "PLUGIN_AUTOSTART_APPROVAL_PERSIST_FAILED", (
        f"批准没落地却没有上报，调用方会把这次启动当成完全持久化：{excinfo.value.code}"
    )


def test_the_override_gate_is_written_before_the_source_switch() -> None:
    """Third-party code must not be promoted and started before it is gated.

    ``switch_builtin_source`` commits the new install-source lock, refreshes the
    registry and can start the replacement while the builtin is running. A
    pending record written only after it returns leaves a window where the
    process can die with the new code already promoted — and it then inherits
    the builtin's autostart eligibility (greptile and coderabbit, independently).

    Mutation: move the mark back below the ``switch_builtin_source`` call.
    """
    import inspect

    source = inspect.getsource(cli_service.PluginCliService.install_builtin_override)
    mark_at = source.find("mark_autostart_pending")
    switch_at = source.find("switched = await switch_builtin_source(")
    assert -1 not in (mark_at, switch_at), "前提没成立：两个调用点都要在"
    assert mark_at < switch_at, (
        "待批准登记排在源切换之后，中间那段窗口里第三方代码已经被提升甚至启动了"
    )
    # 失败要还原：切换回滚到内置插件之后，这条记录会把一个用户本来就在自启的内置
    # 插件拦下来。
    assert "clear_autostart_pending, plan.plugin_id" in source, (
        "切换失败后没有还原批准状态，一次失败的覆盖安装会误伤内置插件的自启动"
    )


def test_uninstalling_clears_the_pending_record() -> None:
    """The record belongs to the code that was just removed.

    Uninstalling an override restores the builtin — which the user had
    autostarting before — but a leftover pending record keyed on that id keeps
    holding it back. When the plugin is removed outright the record is equally
    stale and would ambush a later reinstall (codex).

    Mutation: drop the ``clear_autostart_pending`` call from the uninstall
    transaction.
    """
    import inspect

    from plugin.server.application.plugins.installation_transactions import uninstall

    source = inspect.getsource(uninstall.uninstall_plugin)
    assert "clear_autostart_pending" in source, (
        "卸载没有清掉待批准记录：恢复出来的内置插件会被一条属于已删除代码的记录拦住"
    )


def test_a_failed_rollback_keeps_the_override_gated() -> None:
    """Restoring approval is conditional on the override actually being gone.

    A rollback can fail to delete the user directory — the file is in use, the
    permission is wrong, the disk is bad. The third-party source is then still
    on disk as the effective source, and restoring approval would let it run at
    the next startup before the user ever approved it (greptile). The condition
    is what is on disk, not what we intended.

    Mutation: restore approval on ``override_was_approved`` alone.
    """
    import inspect

    source = inspect.getsource(cli_service.PluginCliService.install_builtin_override)
    assert "override_removed" in source, (
        "回滚失败时没有检查覆盖是否真的消失，残留的第三方源会绕过批准闸"
    )
    assert "if override_was_approved and override_removed:" in source, (
        "恢复批准的条件没有把「覆盖真的没留在盘上」算进去"
    )


def test_the_override_refuses_to_promote_without_a_durable_gate() -> None:
    """A lost gate write must stop the promotion, not just get logged.

    The mark now precedes ``switch_builtin_source``, so refusing is clean —
    nothing has been promoted yet. Proceeding would put third-party code in
    place as the effective source with no pending record, and it would autostart
    unapproved at the next boot (coderabbit).

    The ordering half of this stays a source check: driving the real transaction
    would need the whole market-override stack. The durability half is covered
    behaviourally by ``test_mark_reports_whether_the_gate_is_durable``.

    Mutation: ignore ``mark_autostart_pending``'s return value.
    """
    import inspect

    source = inspect.getsource(cli_service.PluginCliService.install_builtin_override)
    assert "if not await asyncio.to_thread(mark_autostart_pending" in source, (
        "登记写盘失败时仍然继续切换：第三方代码会成为有效源却没有待批准记录"
    )
    refuse_at = source.find("PLUGIN_AUTOSTART_GATE_UNAVAILABLE")
    switch_at = source.find("switched = await switch_builtin_source(")
    assert -1 not in (refuse_at, switch_at) and refuse_at < switch_at, (
        "拒绝发生在切换之后就不干净了——那时第三方源已经被提升"
    )


def test_uninstall_fails_when_the_approval_record_cannot_be_cleared() -> None:
    """A stale record outlives the code it was written for.

    If the clear does not reach disk, the restored builtin keeps being held back
    from autostart on every subsequent start, and a later reinstall under the
    same id inherits the record too. Committing the uninstall and reporting
    success hides that (coderabbit); raising hands it to the existing
    pre-commit rollback instead.

    Mutation: ignore ``clear_autostart_pending``'s return value in the uninstall
    transaction.
    """
    import inspect

    from plugin.server.application.plugins.installation_transactions import uninstall

    source = inspect.getsource(uninstall.uninstall_plugin)
    assert "if not await asyncio.to_thread(clear_autostart_pending" in source, (
        "卸载忽略了批准清除的失败，会带着一条过时记录报成功"
    )
    assert "PLUGIN_AUTOSTART_APPROVAL_PERSIST_FAILED" in source


@pytest.mark.asyncio
async def test_a_lost_install_gate_is_reported_in_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plain install path cannot refuse — but it must not stay silent.

    By the time the gate runs the plugin is already on disk, so unlike the
    override path there is nothing to refuse. If the pending record cannot be
    written, the next startup treats the missing record as approval and the
    plugin runs before the user ever started it, so the result has to say so
    (greptile).

    Mutation: discard ``_mark_new_install_awaiting_autostart``'s return value.
    """
    monkeypatch.setattr(
        cli_service, "_mark_new_install_awaiting_autostart", lambda result: ["stuck"]
    )
    monkeypatch.setattr(cli_service, "get_install_source_manager", lambda: None)

    install_result = {"installed_plugins": [{"target_plugin_id": "stuck"}]}
    service = cli_service.PluginCliService()

    async def _plan_install(**_kwargs):
        return {"action": "install"}

    monkeypatch.setattr(service, "plan_install", _plan_install)
    monkeypatch.setattr(service, "_install_sync", lambda **_kwargs: install_result)

    async def _record(*, install_result, package, source):
        return install_result

    monkeypatch.setattr(service, "_record_requested_install_source", _record)

    result = await service.install(package="whatever.neko-plugin")

    assert "stuck" in str(result.get("autostart_gate_warning", "")), (
        f"登记失败没有出现在安装结果里，调用方会以为这个插件被拦住了：{result}"
    )


@pytest.mark.asyncio
async def test_a_plain_install_is_refused_when_the_gate_cannot_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate before promoting, so a lost write can still be refused.

    Marking after ``_install_sync`` meant a failed write could only be reported
    as a warning — the plugin was already on disk, and the next startup reads a
    missing pending record as approval (coderabbit / greptile). The plan carries
    the manifest plugin id before anything is promoted, so the gate can go
    first.

    Mutation: move the mark back below ``_install_sync``.
    """
    from plugin.server.domain.errors import ServerDomainError

    monkeypatch.setattr(cli_service, "get_install_source_manager", lambda: None)
    monkeypatch.setattr(cli_service, "is_autostart_approved", lambda plugin_id: True)
    monkeypatch.setattr(cli_service, "mark_autostart_pending", lambda plugin_id: False)

    installed: list[str] = []
    service = cli_service.PluginCliService()

    async def _plan_install(**_kwargs):
        return {"action": "install", "plugin_id": "newcomer"}

    def _install_sync(**_kwargs):
        installed.append("ran")
        return {"installed_plugins": [{"target_plugin_id": "newcomer"}]}

    monkeypatch.setattr(service, "plan_install", _plan_install)
    monkeypatch.setattr(service, "_install_sync", _install_sync)

    with pytest.raises(ServerDomainError) as excinfo:
        await service.install(package="whatever.neko-plugin")

    assert excinfo.value.code == "PLUGIN_AUTOSTART_GATE_UNAVAILABLE"
    assert installed == [], (
        "闸门写不进盘却已经把插件提升上去了，拒绝就不再干净"
    )


@pytest.mark.asyncio
async def test_a_failed_install_restores_the_previous_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed install must not leave a pending record behind.

    The gate now runs before promotion, so an install that then fails would
    otherwise strand a record that ambushes whatever later takes that id.

    Mutation: drop the ``clear_autostart_pending`` call from the except branch.
    """
    monkeypatch.setattr(cli_service, "get_install_source_manager", lambda: None)
    monkeypatch.setattr(cli_service, "is_autostart_approved", lambda plugin_id: True)
    monkeypatch.setattr(cli_service, "mark_autostart_pending", lambda plugin_id: True)

    restored: list[str] = []
    monkeypatch.setattr(
        cli_service, "clear_autostart_pending", lambda plugin_id: restored.append(plugin_id)
    )

    service = cli_service.PluginCliService()

    async def _plan_install(**_kwargs):
        return {"action": "install", "plugin_id": "doomed"}

    def _install_sync(**_kwargs):
        raise RuntimeError("install blew up")

    monkeypatch.setattr(service, "plan_install", _plan_install)
    monkeypatch.setattr(service, "_install_sync", _install_sync)

    with pytest.raises(RuntimeError):
        await service.install(package="whatever.neko-plugin")

    assert restored == ["doomed"], (
        f"安装失败后没有还原批准状态，这条记录会误伤将来占用同一个 id 的插件：{restored}"
    )


def test_every_approval_write_checks_whether_it_persisted() -> None:
    """No production call site may discard the durability result.

    ``mark_autostart_pending`` and ``clear_autostart_pending`` return whether
    the change reached disk. Ignoring that is how the gate silently stops
    working: a lost mark lets unapproved code autostart, a lost clear strands a
    record that blocks a plugin forever. Both directions have been shipped
    broken in this PR at different call sites, each time found by a reviewer
    rather than by the point tests — this checks all of them at once.

    Mutation: drop the result check from any single call site.
    """
    import ast
    from pathlib import Path as _Path

    import plugin as plugin_pkg

    root = _Path(plugin_pkg.__file__).parent
    watched = {"mark_autostart_pending", "clear_autostart_pending"}
    offenders: list[str] = []

    for path in root.rglob("*.py"):
        parts = path.parts
        if "tests" in parts or path.name == "autostart_approvals.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            # 裸表达式语句 = 调用了但把返回值扔了。
            if not isinstance(node, ast.Expr):
                continue
            for inner in ast.walk(node):
                name = None
                if isinstance(inner, ast.Name):
                    name = inner.id
                elif isinstance(inner, ast.Attribute):
                    name = inner.attr
                if name in watched:
                    offenders.append(f"{path.name}:{node.lineno}")
                    break

    assert not offenders, (
        "这些调用点把批准写入的成败扔掉了，闸门会在磁盘出问题时静默失效："
        f"{offenders}"
    )


@pytest.mark.asyncio
async def test_every_plugin_in_a_bundle_is_gated_before_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle's plan id is the package id, not any plugin's manifest id.

    ``build_install_plan`` sets ``plugin_id`` to the package id for bundles,
    while the registry looks approval up per contained plugin. Gating only the
    package id therefore gates nobody, and a crash between promotion and the
    post-install mark leaves every bundled plugin autostart-eligible
    (coderabbit).

    Mutation: gate ``plan_dict["plugin_id"]`` instead of ``bundle_plugin_ids``.
    """
    monkeypatch.setattr(cli_service, "get_install_source_manager", lambda: None)
    monkeypatch.setattr(cli_service, "is_autostart_approved", lambda plugin_id: True)

    marked: list[str] = []

    def _mark(plugin_id: str) -> bool:
        marked.append(plugin_id)
        return True

    monkeypatch.setattr(cli_service, "mark_autostart_pending", _mark)
    monkeypatch.setattr(
        cli_service, "_mark_new_install_awaiting_autostart", lambda result: []
    )

    service = cli_service.PluginCliService()

    async def _plan_install(**_kwargs):
        return {
            "action": "install",
            "package_type": "bundle",
            "plugin_id": "some_bundle_package",
            "bundle_plugin_ids": ["alpha", "beta"],
        }

    marked_before_install: list[list[str]] = []

    def _install_sync(**_kwargs):
        marked_before_install.append(list(marked))
        return {"installed_plugins": []}

    monkeypatch.setattr(service, "plan_install", _plan_install)
    monkeypatch.setattr(service, "_install_sync", _install_sync)

    async def _record(*, install_result, package, source):
        return install_result

    monkeypatch.setattr(service, "_record_requested_install_source", _record)

    await service.install(package="bundle.neko-bundle")

    assert marked_before_install and set(marked_before_install[0]) == {"alpha", "beta"}, (
        f"提升之前没有把 bundle 里每个插件都拦住：{marked_before_install}"
    )
    assert "some_bundle_package" not in marked, (
        "登记的是包 id，而注册表按每个插件自己的 manifest id 查批准状态"
    )


def test_a_leftover_install_directory_is_found_by_its_manifest_id(
    tmp_path: Path,
) -> None:
    """The rollback probe answers "is this plugin's code still here".

    Staging cleanup runs with ``ignore_errors=True``, so a failed install can
    leave a runnable copy behind — and the directory name is not the answer,
    because a conflict rename gives the same plugin a different one.

    Mutation: compare directory names instead of manifest ids.
    """
    root = tmp_path / "plugins"
    root.mkdir()
    assert not cli_service._plugin_directory_exists(root, "demo")

    leftover = root / "demo_2"
    leftover.mkdir()
    (leftover / "plugin.toml").write_text(
        '[plugin]\nid = "demo"\n', encoding="utf-8"
    )
    assert cli_service._plugin_directory_exists(root, "demo"), (
        "改名过的残骸没被认出来，安装失败之后它会拿回批准位并在下次开机自己跑起来"
    )


def test_the_remnant_probe_fails_closed(tmp_path: Path) -> None:
    """Every uncertainty keeps the id gated.

    A false positive costs one manual start. A false negative hands third-party
    code its autostart approval back.

    Mutation: return ``False`` from the unreadable-manifest branch.
    """
    root = tmp_path / "plugins"
    root.mkdir()
    nameless = root / "demo"
    nameless.mkdir()
    assert cli_service._plugin_directory_exists(root, "demo"), (
        "manifest 读不出来的同名目录被当成不存在"
    )
    assert not cli_service._plugin_directory_exists(root / "gone", "demo"), (
        "根目录不存在时反而报有残骸，等于任何一次失败安装都永久拦住这个 id"
    )
    staging = root / ".neko_staging_x"
    staging.mkdir()
    (staging / "plugin.toml").write_text(
        '[plugin]\nid = "other"\n', encoding="utf-8"
    )
    assert not cli_service._plugin_directory_exists(root, "other"), (
        "暂存目录被当成安装产物：注册表根本不扫它们"
    )


async def _drive_failed_install(
    monkeypatch: pytest.MonkeyPatch,
    gate_root: Path,
    plugin_id: str,
    gate_root_override=None,
) -> dict:
    """Run ``install()`` through its rollback branch and return the store."""
    store: dict[str, object] = {}

    class _FakeConfigManager:
        def load_json_config(self, name):
            if name not in store:
                raise FileNotFoundError(name)
            return store[name]

        def save_json_config(self, name, payload):
            store[name] = payload

    import utils.config_manager as config_manager_module

    from plugin.server.infrastructure import autostart_approvals

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _FakeConfigManager
    )
    autostart_approvals._reset_cache_for_testing()
    monkeypatch.setattr(cli_service, "get_install_source_manager", lambda: None)

    service = cli_service.PluginCliService()

    async def _plan_install(**_kwargs):
        return {"action": "install", "plugin_id": plugin_id}

    def _boom(**_kwargs):
        raise RuntimeError("install blew up after writing files")

    monkeypatch.setattr(service, "plan_install", _plan_install)
    monkeypatch.setattr(service, "_install_sync", _boom)
    monkeypatch.setattr(
        service,
        "_autostart_gate_root",
        gate_root_override or (lambda _root: gate_root),
    )

    with pytest.raises(RuntimeError):
        await service.install(package="whatever.neko-plugin")
    return store


@pytest.mark.asyncio
async def test_install_rollback_keeps_remnants_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed install that left code behind keeps its gate.

    ``_install_via_staging_sync`` cleans up with ``ignore_errors=True``, so the
    directory can survive the failure. Clearing the pending record for every id
    the attempt touched hands that leftover copy its autostart approval back
    (codex) — the same judgement the override rollback already makes: look at
    disk, not at intent.

    Mutation: clear the pending record unconditionally in the except branch.
    """
    from plugin.server.infrastructure import autostart_approvals

    root = tmp_path / "plugins"
    root.mkdir()
    leftover = root / "demo"
    leftover.mkdir()
    (leftover / "plugin.toml").write_text(
        "\n".join(["[plugin]", 'id = "demo"', ""]), encoding="utf-8"
    )
    try:
        await _drive_failed_install(monkeypatch, root, "demo")
        assert not autostart_approvals.is_autostart_approved("demo"), (
            "安装失败留下了可运行的目录，批准位却被还原了：这份第三方代码会在"
            "下次开机自己跑起来"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


@pytest.mark.asyncio
async def test_install_rollback_releases_an_id_it_left_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other direction: nothing on disk means nothing to gate.

    A pending record left behind by a failed install ambushes whatever later
    takes that id — the user has to start it once by hand for no reason.

    Mutation: keep the record whether or not the directory survived.
    """
    from plugin.server.infrastructure import autostart_approvals

    root = tmp_path / "plugins"
    root.mkdir()
    try:
        await _drive_failed_install(monkeypatch, root, "demo")
        assert autostart_approvals.is_autostart_approved("demo"), (
            "安装失败什么也没留下，批准位却没还原：将来占用这个 id 的插件会被误伤"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_a_failed_probe_removes_metadata_copied_from_the_source_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that could not derive metadata must not ship the old file.

    Plugin trees can already contain ``plugin.meta.json`` — every builtin one in
    this repo does — and the build pipelines copy the source tree into the
    staging directory before this runs. Leaving that copy in place when the
    probe fails ships the previous build's handlers and schemas as if they were
    this build's, with a source hash that can still match, so the host trusts
    them instead of falling back to the manifest (codex).

    Mutation: return without unlinking the staged copy.
    """
    from plugin.neko_plugin_cli.core import metadata_probe

    source = tmp_path / "src"
    source.mkdir()
    (source / "plugin.toml").write_text("id = 'demo'\n", encoding="utf-8")
    target = tmp_path / "staged"
    target.mkdir()
    stale = target / packaged_metadata.PACKAGED_METADATA_FILENAME
    stale.write_text('{"schema_version": 1}', encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise metadata_probe.MetadataProbeError("optional dependency missing")

    monkeypatch.setattr(metadata_probe, "derive_plugin_metadata", _boom)

    assert write_packaged_metadata(source_dir=source, target_dir=target) is None
    assert not stale.exists(), (
        "探测失败却把源树里那份旧 plugin.meta.json 留在包里，宿主会当成这次打包的结果"
    )


@pytest.mark.asyncio
async def test_an_unresolvable_root_keeps_the_ids_gated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rollback probe must not throw from inside the except block.

    ``_autostart_gate_root`` runs while the install's real failure is in flight.
    Letting a path-policy error escape would replace that cause and skip every
    restore in the loop (coderabbit). It returns ``None`` instead, and ``None``
    means the same thing a surviving directory means: keep the id gated.

    Mutation: raise out of ``_autostart_gate_root`` instead of returning None.
    """
    from plugin.server.infrastructure import autostart_approvals

    def _no_root(_plugins_root):
        return None

    try:
        await _drive_failed_install(
            monkeypatch, tmp_path, "demo", gate_root_override=_no_root
        )
        assert not autostart_approvals.is_autostart_approved("demo"), (
            "查不出残骸在不在却还是把批准位还了回去"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_the_gate_root_helper_swallows_a_broken_path_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every lookup in that helper is inside the try, including the policy.

    Mutation: move ``self._path_policy()`` back above the try.
    """
    service = cli_service.PluginCliService()

    def _broken():
        raise RuntimeError("settings are unreadable")

    monkeypatch.setattr(service, "_path_policy", _broken)

    assert service._autostart_gate_root(None) is None, (
        "路径策略的异常从 except 分支里逃了出去：安装失败的真实原因会被它顶掉，"
        "而且这一轮的批准位一个都不会还原"
    )


def test_a_lost_pending_restore_shows_up_in_the_uninstall_result() -> None:
    """A compensation failure belongs in the result, not only in the log.

    Re-raising would replace the uninstall's real failure cause, so it does not;
    but staying silent leaves the caller believing the rollback was complete
    (coderabbit). It rides along the same way ``preference_rollback`` does.

    CodeRabbit also asked for a persisted blocking state; that half is not
    implemented, and the reason is in the code: the only thing available to
    persist it is the store whose write just failed.

    Mutation: drop ``autostart_gate_rollback`` from the raised details.
    """
    import inspect

    from plugin.server.application.plugins.installation_transactions import uninstall

    source = inspect.getsource(uninstall.uninstall_plugin)
    assert 'autostart_gate_rollback = "incomplete"' in source, (
        "补偿失败没有被记下来"
    )
    assert source.count("**gate_details,") == 2, (
        "两个构造出来的 UninstallPluginError 必须都带上它，只带一个等于看运气"
    )
    assert "exc.details.update(gate_details)" in source, (
        "直接透传的 UninstallPluginError 没有带上补偿失败，那条路占了卸载失败的大头"
    )


async def _drive_failed_uninstall(
    monkeypatch: pytest.MonkeyPatch,
    plugin_dir: Path,
    plugin_id: str,
    *,
    seed_pending: bool,
    code_survives_the_failure: bool = True,
) -> tuple[dict, Exception]:
    """Run ``uninstall_plugin`` into its pre-commit rollback branch.

    Everything before the commit is stubbed; the parts under test — clearing the
    pending record, and putting it back when the rollback restores the plugin —
    are the real code.
    """
    from plugin.server.application.plugins.installation_transactions import uninstall

    store: dict[str, object] = {}

    class _FakeConfigManager:
        def load_json_config(self, name):
            if name not in store:
                raise FileNotFoundError(name)
            return store[name]

        def save_json_config(self, name, payload):
            store[name] = payload

    import utils.config_manager as config_manager_module

    from plugin.server.infrastructure import autostart_approvals

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _FakeConfigManager
    )
    autostart_approvals._reset_cache_for_testing()
    if seed_pending:
        # 卸载之前这个插件从没被用户启动过。
        assert autostart_approvals.mark_autostart_pending(plugin_id)

    class _Manager:
        def load(self):
            return None

        def mark_removed(self, *, directory_path):
            return None

        def restore_entry_for_rollback(self, entry):
            return None

    config_path = plugin_dir / "plugin.toml"

    def _noop(*_args, **_kwargs):
        return None

    async def _anoop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(uninstall, "ensure_plugin_exec_state_roots_separated", _noop)
    monkeypatch.setattr(
        uninstall, "_get_plugin_meta_sync", lambda _pid: {"id": plugin_id}
    )
    monkeypatch.setattr(
        uninstall, "_resolve_plugin_config_path_sync", lambda _pid, _meta: config_path
    )
    monkeypatch.setattr(uninstall, "_path_within_plugin_roots_sync", lambda _p: True)
    monkeypatch.setattr(uninstall, "get_install_source_manager", _Manager)
    monkeypatch.setattr(uninstall, "require_uninstall_ownership", _noop)
    monkeypatch.setattr(uninstall, "_registry_refresh_target", _noop)
    monkeypatch.setattr(uninstall, "_plugin_is_running_sync", lambda _pid: False)
    monkeypatch.setattr(uninstall, "_snapshot_runtime_preference", _noop)
    monkeypatch.setattr(uninstall, "_stage_orphaned_package_profile_sync", _noop)
    monkeypatch.setattr(uninstall, "_stage_plugin_code_sync", _noop)
    monkeypatch.setattr(uninstall, "_remove_runtime_metadata_sync", _noop)
    monkeypatch.setattr(uninstall, "_refresh_registry", _anoop)
    monkeypatch.setattr(uninstall, "clear_runtime_override", _noop)

    def _commit_fails(_staged):
        if not code_survives_the_failure:
            # 半途提交：代码已经删掉了，收尾才炸。回滚拿不回任何东西。
            import shutil

            shutil.rmtree(plugin_dir, ignore_errors=True)
        raise RuntimeError("commit blew up after the approval record was cleared")

    monkeypatch.setattr(uninstall, "_commit_staged_plugin_code_sync", _commit_fails)

    async def _rollback(**_kwargs):
        # 真实回滚会把文件放回去；这里文件根本没被删掉，等价于恢复成功。
        return uninstall._RollbackOutcome(
            filesystem_rollback="restored",
            runtime_restart="not_needed",
            preference_restored=True,
        )

    monkeypatch.setattr(uninstall, "_rollback_precommit", _rollback)

    with pytest.raises(uninstall.UninstallPluginError) as excinfo:
        await uninstall.uninstall_plugin(plugin_id)
    return store, excinfo.value


@pytest.mark.asyncio
async def test_a_rolled_back_uninstall_puts_the_pending_record_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Behavioural: the plugin comes back, so its gate comes back with it.

    The record is cleared partway through the pre-commit steps. When a later
    step fails, ``_rollback_precommit`` restores the plugin's files and
    preferences and knows nothing about the approval record — a plugin the user
    had never started would come back approved and autostart at the next boot
    (codex).

    This replaces a source-text guard that only pinned the order of a few
    strings; renaming a variable broke it and a real inversion could slip past
    it (coderabbit).

    Mutation: drop the ``mark_autostart_pending`` call from the except branch.
    """
    from plugin.server.infrastructure import autostart_approvals

    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(["[plugin]", 'id = "demo"', ""]), encoding="utf-8"
    )
    try:
        await _drive_failed_uninstall(
            monkeypatch, plugin_dir, "demo", seed_pending=True
        )
        assert not autostart_approvals.is_autostart_approved("demo"), (
            "回滚把插件放回来了，批准位却没跟着回来：一个用户从没启动过的插件"
            "在一次失败的卸载之后变成了已批准"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


@pytest.mark.asyncio
async def test_a_rolled_back_uninstall_leaves_a_started_plugin_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restore is conditional on the record having been there.

    A plugin the user had already started carries no pending record. Writing one
    during a rollback would take away an autostart the user had earned.

    Mutation: re-mark unconditionally, ignoring ``autostart_was_pending``.
    """
    from plugin.server.infrastructure import autostart_approvals

    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(["[plugin]", 'id = "demo"', ""]), encoding="utf-8"
    )
    try:
        await _drive_failed_uninstall(
            monkeypatch, plugin_dir, "demo", seed_pending=False
        )
        assert autostart_approvals.is_autostart_approved("demo"), (
            "用户早就启动过的插件在一次失败的卸载之后被拦下来了"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


@pytest.mark.asyncio
async def test_a_rollback_that_lost_the_code_writes_no_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the code really is gone, the gate must not be re-armed.

    A pending record outlives the code it was written for: whatever later takes
    that plugin id inherits it and has to be started by hand once, for a plugin
    the user never installed under that id. Same judgement as everywhere else in
    this gate — look at the disk, not at what we intended.

    Mutation: re-mark on ``autostart_was_pending`` alone, without checking that
    the directory came back.
    """
    from plugin.server.infrastructure import autostart_approvals

    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(["[plugin]", 'id = "demo"', ""]), encoding="utf-8"
    )
    try:
        await _drive_failed_uninstall(
            monkeypatch,
            plugin_dir,
            "demo",
            seed_pending=True,
            code_survives_the_failure=False,
        )
        assert not plugin_dir.exists(), "前提没成立：这个用例要的是代码真的没了"
        assert autostart_approvals.is_autostart_approved("demo"), (
            "代码已经不在盘上了还补了一条待批准记录：将来占用这个 id 的插件"
            "会被它误伤，第一次得手动启动"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_a_deleted_stale_metadata_leaves_the_staged_list(tmp_path: Path) -> None:
    """The staged-file list must describe the staging tree as it ends up.

    ``copy_plugin_runtime_files`` lists the ``plugin.meta.json`` it copied in
    from the source tree; when the probe fails that copy is deleted. A listed
    path that no longer exists makes ``BuildResult._validate_layout`` raise
    ``FileNotFoundError`` under ``keep_staging=True`` — after the archive has
    already been written (codex).

    Mutation: keep using ``_record_staged_file`` on the failure path.
    """
    from plugin.neko_plugin_cli.core.build import _settle_staged_metadata

    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    copied = staged_dir / packaged_metadata.PACKAGED_METADATA_FILENAME
    other = staged_dir / "main.py"
    other.write_text("VALUE = 1", encoding="utf-8")
    staged_files = [other, copied]

    _settle_staged_metadata(staged_files, staged_dir, None)
    assert staged_files == [other], (
        f"删掉的 plugin.meta.json 还留在清单里，--keep-staging 的布局校验会炸：{staged_files}"
    )

    written = copied
    written.write_text("{}", encoding="utf-8")
    _settle_staged_metadata(staged_files, staged_dir, written)
    assert staged_files == [other, written], (
        f"成功写出的元数据没有被记进清单：{staged_files}"
    )


def test_a_manifestless_state_replacement_is_gated() -> None:
    """Putting code where there was none is a fresh install, not a replacement.

    A directory holding only ``config``/``data``/``cache`` and no
    ``plugin.toml`` makes ``build_install_plan`` return ``reinstall``, so it
    travels the replacement exit — which deliberately skips the gate because a
    replacement takes over a plugin the user already runs. That reasoning does
    not hold here: no plugin code was ever in that directory, so the default
    enabled/auto_start settings would run brand-new third-party code at the next
    startup without the user having started it (codex).

    The ordering half stays a source check — driving the whole replacement
    transaction would need the entire upgrade stack. The durability half is
    covered behaviourally by ``test_mark_reports_whether_the_gate_is_durable``.

    Mutation: drop the ``plan.manifestless_state`` branch.
    """
    import inspect

    source = inspect.getsource(cli_service.PluginCliService.install)
    gate_at = source.find("if plan.manifestless_state:")
    mark_at = source.find("mark_autostart_pending, plan.plugin_id")
    replace_at = source.find("replacement_transaction.replace_plugin(")
    assert -1 not in (gate_at, mark_at, replace_at), (
        "无 manifest 的遗留状态目录没有过闸：那里从没装过插件代码，这次是全新"
        "把可执行代码放进去，默认设置会让它在下次开机自己跑起来"
    )
    assert gate_at < mark_at < replace_at, (
        "闸设在提升之后就晚了——代码已经在盘上，拒绝也收不回来"
    )
    assert "PLUGIN_AUTOSTART_GATE_UNAVAILABLE" in source, (
        "登记写盘失败时没有拒绝，等于放一份没有待批准记录的第三方代码上盘"
    )
    restore_at = source.find("clear_autostart_pending, plan.plugin_id")
    manifest_check_at = source.find('(target_dir / "plugin.toml").exists')
    assert -1 not in (restore_at, manifest_check_at), (
        "回滚没有还原批准位，或者还原时没看盘"
    )
    assert manifest_check_at < restore_at, (
        "先还原再看盘：代码留在盘上时批准位已经还回去了"
    )


def test_a_renamed_plugin_keeps_its_pending_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate follows the plugin when the registry renames it.

    Install can only write the id the manifest declares. When a second plugin
    declares an id that is already taken, the registry runs it as ``demo_1`` —
    and the autostart check asks about *that* id, so the record written at
    install time missed it and the new code was free to start itself (codex).

    Mutation: skip the move and leave the record under the declared id.
    """
    from plugin.server.application.plugins import registry_service
    from plugin.server.infrastructure import autostart_approvals

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
    try:
        assert autostart_approvals.mark_autostart_pending("demo")

        registry_service._move_autostart_gate_to_runtime_id("demo", "demo_1")

        assert not autostart_approvals.is_autostart_approved("demo_1"), (
            "改名之后新装的插件不再被拦：自启动检查问的是运行时 id，"
            "而记录还留在声明 id 上"
        )
        assert autostart_approvals.is_autostart_approved("demo"), (
            "记录是搬走不是复制：留一份在声明 id 上会拦住本来就拥有这个 id 的"
            "那个插件，而清掉它又会顺手批准这一个"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_the_gate_move_leaves_an_approved_plugin_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pending record means nothing to move.

    Mutation: mark the runtime id unconditionally.
    """
    from plugin.server.application.plugins import registry_service
    from plugin.server.infrastructure import autostart_approvals

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
    try:
        registry_service._move_autostart_gate_to_runtime_id("demo", "demo_1")
        assert autostart_approvals.is_autostart_approved("demo_1"), (
            "声明 id 上根本没有待批准记录，却给运行时 id 记了一条：一次改名就能"
            "把用户早就在用的插件拦下来"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_the_gate_move_runs_before_registration() -> None:
    """It has to happen where the runtime id is decided.

    Mutation: move the call after ``state.plugins`` is written.
    """
    import inspect

    from plugin.server.application.plugins import registry_service

    source = inspect.getsource(registry_service._apply_discovery_record_sync)
    resolve_at = source.find("runtime_plugin_id = target_plugin_id if source_replacement")
    move_at = source.find("_move_autostart_gate_to_runtime_id(")
    meta_at = source.find("plugin_meta = _build_plugin_meta(")
    assert -1 not in (resolve_at, move_at, meta_at), "注册路径上没有搬迁批准位"
    assert resolve_at < move_at < meta_at, (
        "搬迁必须在运行时 id 定下来之后、登记进注册表之前"
    )
    assert "declared_id_is_taken=_declared_id_taken_by_another_plugin(" in source, (
        "「声明 id 是不是别人的」用的不是实时注册表——刷新开始时的快照看不见"
        "同一轮里先注册的那个同 id 插件，搬迁会把它的待批准记录抢走"
    )


def test_packaging_parses_the_manifest_without_local_overlays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The author's own profile must not travel inside the package.

    ``_parse_single_plugin_config`` resolves the machine's runtime config and
    activated profile for that plugin id. Producing distributable metadata
    through it would export entry ids, schemas and handlers derived from the
    author's private configuration, and consumers would trust them because the
    source fingerprint still matches (codex).

    Mutation: ignore ``apply_user_overlays`` and apply the overlay anyway.
    """
    from plugin.core import registry

    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                'id = "demo"',
                'entry = "main:Plugin"',
                'name = "Demo"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    consulted: list[str] = []

    def _resolver(*_args, **_kwargs):
        consulted.append("profile")
        return {"effective_config": {"plugin": {"id": "demo", "entry": "main:Plugin"}}}

    monkeypatch.setattr(registry, "resolve_plugin_config_from_path", _resolver)
    monkeypatch.setattr(
        registry, "get_runtime_override", lambda _pid: consulted.append("enabled")
    )
    monkeypatch.setattr(
        registry,
        "get_runtime_auto_start_override",
        lambda _pid: consulted.append("auto_start"),
    )

    from plugin.logging_config import get_logger

    registry._parse_single_plugin_config(
        plugin_dir / "plugin.toml",
        set(),
        get_logger("test"),
        apply_user_overlays=False,
    )
    assert consulted == [], (
        f"打包期解析读了这台机器上的用户覆盖：{consulted}——作者的 profile 会被"
        "写进发出去的元数据，而源码指纹还是对得上的"
    )

    registry._parse_single_plugin_config(
        plugin_dir / "plugin.toml", set(), get_logger("test")
    )
    assert consulted, "前提没成立：默认路径本来就该读用户覆盖"


def test_a_failed_gate_move_refuses_to_register_the_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate that could not be written must stop the plugin being published.

    Leaving the record under the declared id and registering anyway is the same
    as no gate at all: registration and the autostart filter both use the
    runtime id, and no record there means approved (coderabbit). The refresh
    loop catches ``ServerDomainError`` per record, so refusing costs this one
    plugin a failed registration, not the whole refresh.

    Mutation: log and return instead of raising.
    """
    from plugin.server.application.plugins import registry_service
    from plugin.server.domain.errors import ServerDomainError
    from plugin.server.infrastructure import autostart_approvals

    store: dict[str, object] = {}

    class _WriteOnceConfigManager:
        def load_json_config(self, name):
            if name not in store:
                raise FileNotFoundError(name)
            return store[name]

        def save_json_config(self, name, payload):
            if store.get("_sealed"):
                raise OSError("disk full")
            store[name] = payload

    import utils.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _WriteOnceConfigManager
    )
    autostart_approvals._reset_cache_for_testing()
    try:
        assert autostart_approvals.mark_autostart_pending("demo")
        store["_sealed"] = True

        with pytest.raises(ServerDomainError) as excinfo:
            registry_service._move_autostart_gate_to_runtime_id("demo", "demo_1")
        assert excinfo.value.code == "PLUGIN_AUTOSTART_GATE_UNAVAILABLE"
        assert not autostart_approvals.is_autostart_approved("demo"), (
            "搬迁失败之后声明 id 上那条记录也丢了"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_the_scan_budget_is_computed_after_the_packaged_read() -> None:
    """The worker's allowance must reflect time the metadata read consumed.

    Reading packaged metadata can hash a whole changed tree before falling back,
    so a budget captured before it is a stale snapshot and the worker still gets
    a near-full ``scan_timeout`` — the reload then overruns its advertised wall
    clock by the validation time plus the obsolete allowance (codex). Same
    judgement already applied to ``startup_timeout_value`` one block below.

    Driving this behaviourally would need the whole reload-all stack; the
    ordering is what broke, so the ordering is what is pinned.

    Mutation: compute ``scan_timeout`` before ``_read_packaged_isolated_metadata``,
    or drop the clamp entirely.
    """
    import inspect

    from plugin.server.application.plugins.lifecycle_service import (
        PluginLifecycleService,
    )

    source = inspect.getsource(PluginLifecycleService.start_plugin)
    read_at = source.find("_read_packaged_isolated_metadata,")
    clamp_at = source.find("scan_timeout = _clamp_step_timeout(")
    use_at = source.find("timeout=scan_timeout,")
    assert -1 not in (read_at, clamp_at, use_at), (
        "扫描预算没有钳位，或者钳位调用点不见了"
    )
    assert read_at < clamp_at < use_at, (
        "扫描预算算在读包内元数据之前：读那一步可能哈希整棵树，算出来的上限"
        "到真正调 worker 时已经是过期快照"
    )


def test_the_gate_move_does_not_steal_an_incumbent_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record under a live runtime id may belong to the plugin already there.

    If the plugin holding ``demo`` is itself pending, moving that record to the
    newcomer's ``demo_1`` makes the never-started incumbent autostart-eligible
    (codex). The store is keyed by declared id and cannot say whose record it
    is, so when the declared id is already someone's runtime id the record is
    copied, not moved: both plugins stay gated, and starting either clears only
    its own.

    Mutation: move unconditionally, ignoring ``declared_id_is_taken``.
    """
    from plugin.server.application.plugins import registry_service
    from plugin.server.infrastructure import autostart_approvals

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
    try:
        assert autostart_approvals.mark_autostart_pending("demo")

        registry_service._move_autostart_gate_to_runtime_id(
            "demo", "demo_1", declared_id_is_taken=True
        )

        assert not autostart_approvals.is_autostart_approved("demo"), (
            "把已经在跑的那个插件的待批准记录搬走了：它从没被启动过，现在却"
            "可以自启了"
        )
        assert not autostart_approvals.is_autostart_approved("demo_1"), (
            "新来的那个没被拦住"
        )
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_the_gate_move_reads_the_live_registry_not_the_round_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two same-id plugins first seen in one refresh must not share a verdict.

    ``existing_snapshot`` is taken once at the start of a refresh and never
    updated, so both plugins looked unclaimed and the second one's gate move
    took the first one's pending record (coderabbit). The question is about now,
    so it is asked of the registry.

    Mutation: answer from a snapshot dict instead of ``state.plugins``.
    """
    from plugin.core.state import state
    from plugin.server.application.plugins import registry_service

    mine = tmp_path / "a" / "plugin.toml"
    theirs = tmp_path / "b" / "plugin.toml"
    for path in (mine, theirs):
        path.parent.mkdir(parents=True)
        path.write_text("id = 'demo'" + chr(10), encoding="utf-8")

    monkeypatch.setattr(state, "plugins", {}, raising=False)
    assert not registry_service._declared_id_taken_by_another_plugin("demo", mine), (
        "注册表里根本没有 demo，却说这个 id 被占了"
    )

    # 同一轮里先注册的那个——快照里没有它，注册表里有。
    monkeypatch.setattr(
        state, "plugins", {"demo": {"config_path": str(theirs)}}, raising=False
    )
    assert registry_service._declared_id_taken_by_another_plugin("demo", mine), (
        "同一轮里先注册的同 id 插件没被看见：搬迁会把它的待批准记录抢走"
    )

    monkeypatch.setattr(
        state, "plugins", {"demo": {"config_path": str(mine)}}, raising=False
    )
    assert not registry_service._declared_id_taken_by_another_plugin("demo", mine), (
        "插件自己重新注册被当成和自己抢 id"
    )


def test_a_failed_load_refuses_to_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write must never use the empty set a failed read cached.

    Saving replaces the whole file. Basing it on the empty set left by a
    transient read failure drops every id already on disk, and those plugins —
    installed, never started — silently become autostart-eligible at the next
    boot (codex). Reads stay fail-open; writes do not.

    Mutation: drop the ``_reload_after_failure_locked`` guard from ``_save_locked``.
    """
    from plugin.server.infrastructure import autostart_approvals

    store: dict[str, object] = {"plugin_autostart_pending.json": {"pending": ["old"]}}
    readable = {"value": False}

    class _FlakyConfigManager:
        def load_json_config(self, name):
            if not readable["value"]:
                raise OSError("transient read failure")
            return store[name]

        def save_json_config(self, name, payload):
            store[name] = payload

    import utils.config_manager as config_manager_module

    monkeypatch.setattr(
        config_manager_module, "get_config_manager", _FlakyConfigManager
    )
    autostart_approvals._reset_cache_for_testing()
    try:
        assert autostart_approvals.is_autostart_approved("anything"), (
            "读侧应当照常 fail-open"
        )
        assert not autostart_approvals.mark_autostart_pending("brand_new"), (
            "读盘失败之后仍然写了盘：那次写是整文件替换，会把盘上已有的记录全抹掉"
        )
        assert store["plugin_autostart_pending.json"] == {"pending": ["old"]}, (
            f"盘上的记录被覆盖了：{store}"
        )

        readable["value"] = True
        assert autostart_approvals.mark_autostart_pending("brand_new"), (
            "读盘恢复之后应该能正常写入"
        )
        assert set(store["plugin_autostart_pending.json"]["pending"]) == {
            "old",
            "brand_new",
        }, f"重读之后没有把已有记录保留下来：{store}"
    finally:
        autostart_approvals._reset_cache_for_testing()


def test_a_refused_override_gate_cleans_its_staging(tmp_path: Path) -> None:
    """Refusing to promote must not leave the extracted package behind.

    ``_stage_builtin_override_sync`` has already unpacked and renamed the whole
    package by then, and the cleanup lives in a ``finally`` further down that the
    raise never reaches — so every approval-store failure leaks a full copy, and
    retries pile them up (codex).

    Mutation: raise without cleaning up.
    """
    import inspect

    source = inspect.getsource(cli_service.PluginCliService.install_builtin_override)
    gate_at = source.find("if not await asyncio.to_thread(mark_autostart_pending")
    cleanup_at = source.find("self._cleanup_builtin_override_staging_sync, staged", gate_at)
    raise_at = source.find("PLUGIN_AUTOSTART_GATE_UNAVAILABLE", gate_at)
    assert -1 not in (gate_at, cleanup_at, raise_at), (
        "登记被拒的那条路没有清理暂存目录：解开的整包会留在盘上"
    )
    assert cleanup_at < raise_at, "清理写在 raise 之后就永远执行不到"


def test_a_probe_that_rewrites_the_tree_yields_no_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handlers are derived before the import's side effects; the hash is taken after.

    A plugin whose module-level code writes a runtime file (initialising state,
    generating a cache) is probed in one shape and fingerprinted in another, so
    the installed tree verifies while importing it registers different entries
    (codex).

    Mutation: skip the before/after digest comparison.
    """
    from plugin.neko_plugin_cli.core import metadata_probe

    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text("id = 'demo'" + chr(10), encoding="utf-8")
    (plugin_dir / "main.py").write_text("VALUE = 1" + chr(10), encoding="utf-8")

    class _Ctx:
        pid = "demo"
        entry = "main:Plugin"
        conf: dict = {}
        pdata: dict = {}
        python_requirement_paths: list = []

    class _Isolated:
        entries_preview: list = []
        handlers: dict = {}
        entry_methods: dict = {}

    monkeypatch.setattr(
        "plugin.core.registry._parse_single_plugin_config",
        lambda *_a, **_k: _Ctx(),
    )

    def _scan_with_side_effect(**_kwargs):
        # 模块级代码在 import 时初始化了一个状态文件。
        (plugin_dir / "state.json").write_text("{}", encoding="utf-8")
        return _Isolated()

    marker = plugin_dir / "marker"

    def _scan_that_only_moves_a_directory(**_kwargs):
        # 按空标记目录的存在与否注册入口，然后把它删掉——文件一个没动，
        # 前后内容摘要完全相同。
        marker.rmdir()
        return _Isolated()

    monkeypatch.setattr(
        "plugin.server.application.plugins.metadata_scanner"
        ".scan_plugin_metadata_isolated",
        _scan_with_side_effect,
    )

    with pytest.raises(metadata_probe.MetadataProbeError) as excinfo:
        metadata_probe.derive_plugin_metadata(plugin_dir)
    assert "changed the staged tree" in str(excinfo.value), (
        "import 期改动了暂存树却照常出元数据：装出来的树能过校验，"
        "import 它却会注册出别的入口"
    )

    (plugin_dir / "state.json").unlink()
    marker.mkdir()
    monkeypatch.setattr(
        "plugin.server.application.plugins.metadata_scanner"
        ".scan_plugin_metadata_isolated",
        _scan_that_only_moves_a_directory,
    )
    with pytest.raises(metadata_probe.MetadataProbeError) as dir_exc:
        metadata_probe.derive_plugin_metadata(plugin_dir)
    assert "changed the staged tree" in str(dir_exc.value), (
        "只动目录、不动文件的那种改法躲过了检查：内容摘要覆盖不到目录"
    )
