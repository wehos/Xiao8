from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path

import pytest

from plugin.core.state import state
from plugin.server import install_registry


@pytest.fixture(autouse=True)
def isolated_install_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with state.acquire_plugins_read_lock():
        plugins_backup = copy.deepcopy(state.plugins)
    registry_backup = dict(install_registry._install_plugin_registry)
    hooks = install_registry._tutorial_migration_hooks
    hooks_backup = list(hooks) if isinstance(hooks, list) else {
        key: list(value) for key, value in hooks.items()
    }
    monkeypatch.setattr(install_registry, "_install_plugin_registry", {})
    monkeypatch.setattr(install_registry, "_tutorial_migration_hooks", {})
    with state.acquire_plugins_write_lock():
        state.plugins.clear()
    try:
        yield
    finally:
        with state.acquire_plugins_write_lock():
            state.plugins.clear()
            state.plugins.update(plugins_backup)
        install_registry._install_plugin_registry = registry_backup
        install_registry._tutorial_migration_hooks = hooks_backup


def _write_manifest(
    root: Path,
    plugin_id: str,
    *,
    declaration: str | None,
) -> Path:
    root.mkdir(parents=True)
    manifest = root / "plugin.toml"
    text = (
        "[plugin]\n"
        f'id = "{plugin_id}"\n'
        f'name = "{plugin_id}"\n'
        'version = "1.0.0"\n'
        'type = "plugin"\n'
        f'entry = "plugin.plugins.{plugin_id}:Plugin"\n'
    )
    if declaration is not None:
        text += "\n" + declaration.strip() + "\n"
    manifest.write_text(text, encoding="utf-8")
    return manifest


def _select_plugin(
    plugin_id: str,
    manifest: Path,
    *,
    entries: tuple[str, ...],
    source: str = "user",
    load_state: str | None = None,
) -> None:
    meta: dict[str, object] = {
        "id": plugin_id,
        "config_path": str(manifest),
        "entries_preview": [{"id": entry_id} for entry_id in entries],
        "effective_source": source,
    }
    if load_state is not None:
        meta["runtime_load_state"] = load_state
    with state.acquire_plugins_write_lock():
        state.plugins[plugin_id] = meta


_DEMO_DECLARATION = """
[plugin.install]
enabled = true
ui_i18n_dir = "i18n/ui"
tutorial_enabled = true

[plugin.install.kinds.models]
entry_id = "demo_install_models"
label = "Models"
queued_message = "Models queued"
entry_timeout = 600.0
"""


def test_galgame_legacy_registration_uses_selected_market_source(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "galgame_plugin"
    manifest = _write_manifest(plugin_root, "galgame_plugin", declaration=None)
    (plugin_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin(
        "galgame_plugin",
        manifest,
        entries=("galgame_install_textractor", "galgame_download_rapidocr_models"),
        source="user",
    )

    galgame = install_registry.get_install_plugin_registration("galgame_plugin")

    assert galgame is not None
    assert set(galgame.install_kinds) == {"rapidocr_models", "textractor"}
    assert galgame.install_kinds["textractor"].entry_id == (
        "galgame_install_textractor"
    )
    assert galgame.install_kinds["rapidocr_models"].entry_id == (
        "galgame_download_rapidocr_models"
    )
    assert all(kind.entry_timeout == 600.0 for kind in galgame.install_kinds.values())
    assert galgame.tutorial_enabled is True
    assert galgame.ui_i18n_dir == (plugin_root / "i18n" / "ui").resolve()
    assert galgame.config_path == manifest.resolve()
    assert galgame.effective_source == "user"


@pytest.mark.parametrize(
    "entries, missing_entry",
    [
        (("galgame_download_rapidocr_models",), "galgame_install_textractor"),
        (("galgame_install_textractor",), "galgame_download_rapidocr_models"),
    ],
)
def test_galgame_legacy_registration_fails_closed_when_entry_is_missing(
    tmp_path: Path,
    entries: tuple[str, ...],
    missing_entry: str,
) -> None:
    plugin_root = tmp_path / "market" / "galgame_plugin"
    manifest = _write_manifest(plugin_root, "galgame_plugin", declaration=None)
    (plugin_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin("galgame_plugin", manifest, entries=entries, source="user")

    with pytest.raises(ValueError, match=missing_entry):
        install_registry.get_install_plugin_registration("galgame_plugin")


def test_galgame_explicit_disabled_declaration_does_not_use_legacy(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "galgame_plugin"
    manifest = _write_manifest(
        plugin_root,
        "galgame_plugin",
        declaration="[plugin.install]\nenabled = false",
    )
    (plugin_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin(
        "galgame_plugin",
        manifest,
        entries=("galgame_install_textractor", "galgame_download_rapidocr_models"),
    )

    assert install_registry.get_install_plugin_registration("galgame_plugin") is None


def test_galgame_invalid_explicit_declaration_fails_closed_without_legacy(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "galgame_plugin"
    manifest = _write_manifest(
        plugin_root,
        "galgame_plugin",
        declaration='[plugin.install]\nenabled = "yes"',
    )
    (plugin_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin(
        "galgame_plugin",
        manifest,
        entries=("galgame_install_textractor", "galgame_download_rapidocr_models"),
    )

    with pytest.raises(ValueError, match="install declaration rejected"):
        install_registry.get_install_plugin_registration("galgame_plugin")


def test_study_legacy_registration_uses_selected_market_source(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "study_companion"
    manifest = _write_manifest(
        plugin_root,
        "study_companion",
        declaration=None,
    )
    (plugin_root / "i18n").mkdir(parents=True)
    _select_plugin(
        "study_companion",
        manifest,
        entries=("study_download_rapidocr_models",),
        source="user",
    )

    study = install_registry.get_install_plugin_registration("study_companion")

    assert study is not None
    assert set(study.install_kinds) == {"rapidocr_models"}
    assert "tesseract" not in study.install_kinds
    assert study.install_kinds["rapidocr_models"].entry_id == (
        "study_download_rapidocr_models"
    )
    assert study.install_kinds["rapidocr_models"].entry_timeout == 600.0
    assert study.ui_i18n_dir == (plugin_root / "i18n").resolve()
    assert study.tutorial_enabled is True
    assert study.config_path == manifest.resolve()
    assert study.effective_source == "user"


def test_study_legacy_registration_fails_closed_when_entry_is_missing(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "study_companion"
    manifest = _write_manifest(
        plugin_root,
        "study_companion",
        declaration=None,
    )
    (plugin_root / "i18n").mkdir(parents=True)
    _select_plugin("study_companion", manifest, entries=(), source="user")

    with pytest.raises(ValueError, match="study_download_rapidocr_models"):
        install_registry.get_install_plugin_registration("study_companion")


def test_study_explicit_disabled_declaration_does_not_use_legacy(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "study_companion"
    manifest = _write_manifest(
        plugin_root,
        "study_companion",
        declaration="[plugin.install]\nenabled = false",
    )
    (plugin_root / "i18n").mkdir(parents=True)
    _select_plugin(
        "study_companion",
        manifest,
        entries=("study_download_rapidocr_models",),
        source="user",
    )

    assert install_registry.get_install_plugin_registration("study_companion") is None


def test_study_invalid_explicit_declaration_fails_closed_without_legacy(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "study_companion"
    manifest = _write_manifest(
        plugin_root,
        "study_companion",
        declaration="[plugin.install]\nenabled = \"yes\"",
    )
    (plugin_root / "i18n").mkdir(parents=True)
    _select_plugin(
        "study_companion",
        manifest,
        entries=("study_download_rapidocr_models",),
        source="user",
    )

    with pytest.raises(ValueError, match="install declaration rejected"):
        install_registry.get_install_plugin_registration("study_companion")


def test_explicit_install_declaration_uses_selected_registry_source(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "market" / "demo_plugin"
    manifest = _write_manifest(
        plugin_root,
        "demo_plugin",
        declaration=_DEMO_DECLARATION,
    )
    (plugin_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin(
        "demo_plugin",
        manifest,
        entries=("demo_install_models",),
        source="user",
    )

    registration = install_registry.get_install_plugin_registration("demo_plugin")

    assert registration is not None
    assert set(registration.install_kinds) == {"models"}
    assert registration.ui_i18n_dir == (plugin_root / "i18n" / "ui").resolve()
    assert registration.tutorial_enabled is True
    assert registration.config_path == manifest.resolve()
    assert registration.effective_source == "user"
    assert registration.install_kinds["models"].entry_timeout == 600.0


def test_explicit_disabled_declaration_does_not_fall_back_to_dynamic(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "demo_plugin"
    manifest = _write_manifest(
        plugin_root,
        "demo_plugin",
        declaration="[plugin.install]\nenabled = false",
    )
    _select_plugin("demo_plugin", manifest, entries=("demo_install_models",))
    install_registry.register_install_plugin(
        "demo_plugin",
        install_kinds={
            "models": install_registry.InstallKindRegistration(
                entry_id="demo_install_models",
                label="Models",
                queued_message="Models queued",
            )
        },
    )

    assert install_registry.get_install_plugin_registration("demo_plugin") is None


def test_invalid_explicit_entry_fails_closed_without_dynamic_fallback(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "demo_plugin"
    manifest = _write_manifest(
        plugin_root,
        "demo_plugin",
        declaration=_DEMO_DECLARATION,
    )
    (plugin_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin("demo_plugin", manifest, entries=("different_entry",))
    install_registry.register_install_plugin(
        "demo_plugin",
        install_kinds={},
    )

    with pytest.raises(ValueError, match="demo_install_models"):
        install_registry.get_install_plugin_registration("demo_plugin")


def test_invalid_explicit_i18n_escape_fails_closed(tmp_path: Path) -> None:
    plugin_root = tmp_path / "demo_plugin"
    declaration = _DEMO_DECLARATION.replace(
        'ui_i18n_dir = "i18n/ui"',
        'ui_i18n_dir = "../outside"',
    )
    manifest = _write_manifest(
        plugin_root,
        "demo_plugin",
        declaration=declaration,
    )
    _select_plugin("demo_plugin", manifest, entries=("demo_install_models",))

    with pytest.raises(ValueError, match="ui_i18n_dir"):
        install_registry.get_install_plugin_registration("demo_plugin")


def test_failed_selected_runtime_does_not_expose_install_api(tmp_path: Path) -> None:
    plugin_root = tmp_path / "demo_plugin"
    manifest = _write_manifest(
        plugin_root,
        "demo_plugin",
        declaration=_DEMO_DECLARATION,
    )
    (plugin_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin(
        "demo_plugin",
        manifest,
        entries=("demo_install_models",),
        load_state="failed",
    )

    assert install_registry.get_install_plugin_registration("demo_plugin") is None


def test_dynamic_registration_remains_available_for_selected_third_party_plugin(
    tmp_path: Path,
) -> None:
    plugin_root = tmp_path / "third_party"
    manifest = _write_manifest(plugin_root, "third_party", declaration=None)
    _select_plugin("third_party", manifest, entries=("third_party_install",))
    install_registry.register_install_plugin(
        "third_party",
        install_kinds={
            "models": install_registry.InstallKindRegistration(
                entry_id="third_party_install",
                label="Models",
                queued_message="Models queued",
            )
        },
        tutorial_enabled=True,
    )

    registration = install_registry.get_install_plugin_registration("third_party")

    assert registration is not None
    assert set(registration.install_kinds) == {"models"}
    assert registration.tutorial_enabled is True


def test_stale_dynamic_registration_is_hidden_when_plugin_is_not_selected() -> None:
    install_registry.register_install_plugin("third_party", install_kinds={})

    assert install_registry.get_install_plugin_registration("third_party") is None


def test_selected_source_change_during_parse_retries_complete_read_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first" / "demo_plugin"
    second_root = tmp_path / "second" / "demo_plugin"
    first_manifest = _write_manifest(
        first_root,
        "demo_plugin",
        declaration=_DEMO_DECLARATION,
    )
    second_manifest = _write_manifest(
        second_root,
        "demo_plugin",
        declaration=_DEMO_DECLARATION,
    )
    (first_root / "i18n" / "ui").mkdir(parents=True)
    (second_root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin(
        "demo_plugin",
        first_manifest,
        entries=("demo_install_models",),
        source="builtin",
    )
    original = install_registry._registration_for_selected_source
    calls = 0

    def switch_after_first_parse(plugin_id: str, selected):
        nonlocal calls
        calls += 1
        registration = original(plugin_id, selected)
        if calls == 1:
            _select_plugin(
                "demo_plugin",
                second_manifest,
                entries=("demo_install_models",),
                source="user",
            )
        return registration

    monkeypatch.setattr(
        install_registry,
        "_registration_for_selected_source",
        switch_after_first_parse,
    )

    registration = install_registry.get_install_plugin_registration("demo_plugin")

    assert calls == 2
    assert registration is not None
    assert registration.config_path == second_manifest.resolve()
    assert registration.effective_source == "user"


def test_selected_source_continuously_changing_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = [
        tmp_path / "first" / "demo_plugin",
        tmp_path / "second" / "demo_plugin",
    ]
    manifests = [
        _write_manifest(root, "demo_plugin", declaration=_DEMO_DECLARATION)
        for root in roots
    ]
    for root in roots:
        (root / "i18n" / "ui").mkdir(parents=True)
    _select_plugin(
        "demo_plugin",
        manifests[0],
        entries=("demo_install_models",),
        source="builtin",
    )
    original = install_registry._registration_for_selected_source
    calls = 0

    def keep_switching(plugin_id: str, selected):
        nonlocal calls
        registration = original(plugin_id, selected)
        calls += 1
        next_index = calls % 2
        _select_plugin(
            "demo_plugin",
            manifests[next_index],
            entries=("demo_install_models",),
            source="user" if next_index else "builtin",
        )
        return registration

    monkeypatch.setattr(
        install_registry,
        "_registration_for_selected_source",
        keep_switching,
    )

    assert install_registry.get_install_plugin_registration("demo_plugin") is None
    assert calls == 2


def test_tutorial_migration_hooks_for_normalizes_plugin_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(install_registry, "_tutorial_migration_hooks", {})

    def migrate(_store_path: Path) -> None:
        pass

    install_registry.register_tutorial_migration_hook(
        migrate,
        plugin_id="study_companion",
    )

    assert install_registry.tutorial_migration_hooks_for(" study_companion ") == [
        migrate
    ]
