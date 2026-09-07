from __future__ import annotations

import argparse
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from plugin.neko_plugin_cli import cli as neko_plugin_cli
from plugin.neko_plugin_cli.commands import init_cmd
from plugin.neko_plugin_cli.commands.validate_cmd import validate_plugin_dir
from plugin.neko_plugin_cli.paths import CliDefaults
from plugin.neko_plugin_cli.templates.generator import PluginSpec, generate_plugin

pytestmark = pytest.mark.plugin_unit


def _make_plugin_dir(tmp_path: Path, plugin_id: str = "cli_demo") -> Path:
    plugin_dir = tmp_path / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)

    (plugin_dir / "plugin.toml").write_text(
        "\n".join(
            [
                "[plugin]",
                f'id = "{plugin_id}"',
                'name = "CLI Demo"',
                'version = "0.0.1"',
                'type = "plugin"',
                f'entry = "plugin.plugins.{plugin_id}:DemoPlugin"',
                "",
                "[plugin.sdk]",
                'recommended = ">=0.1.0,<0.2.0"',
                'supported = ">=0.1.0,<0.3.0"',
                "",
                "[plugin_runtime]",
                "auto_start = false",
                "",
                f"[{plugin_id}]",
                'token = "demo"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from plugin.sdk.plugin import neko_plugin\n\n"
        "@neko_plugin\n"
        "class DemoPlugin: pass\n",
        encoding="utf-8",
    )
    return plugin_dir


def _tamper_package(package_path: Path, target_name: str) -> None:
    entries: list[tuple[zipfile.ZipInfo, bytes]] = []
    with zipfile.ZipFile(package_path) as src:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == target_name:
                data += b"\n# tampered\n"
            entries.append((info, data))

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for info, data in entries:
            dst.writestr(info, data)


def _append_install_declaration(
    plugin_dir: Path,
    *,
    ui_i18n_dir: str = "i18n/ui",
    timeout: str = "600.0",
) -> None:
    (plugin_dir / "i18n" / "ui").mkdir(parents=True, exist_ok=True)
    manifest_path = plugin_dir / "plugin.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\n[plugin.install]\n"
        + "enabled = true\n"
        + f'ui_i18n_dir = "{ui_i18n_dir}"\n'
        + "tutorial_enabled = true\n"
        + "\n[plugin.install.kinds.rapidocr_models]\n"
        + 'entry_id = "cli_demo_download_rapidocr_models"\n'
        + 'label = "RapidOCR Models"\n'
        + 'queued_message = "RapidOCR model download queued"\n'
        + f"entry_timeout = {timeout}\n",
        encoding="utf-8",
    )


def _append_raw_install_declaration(plugin_dir: Path, declaration: str) -> None:
    (plugin_dir / "i18n" / "ui").mkdir(parents=True, exist_ok=True)
    manifest_path = plugin_dir / "plugin.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\n"
        + declaration.strip()
        + "\n",
        encoding="utf-8",
    )


def test_cli_runtime_install_is_disabled_without_writing_plugin_directories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    target_dir = tmp_path / "target"
    plugins_root = tmp_path / "plugins"
    profiles_root = tmp_path / "profiles"

    exit_code = neko_plugin_cli.main(
        ["build", str(plugin_dir), "-t", str(target_dir)]
    )
    assert exit_code == 0
    package_path = target_dir / "cli_demo.neko-plugin"
    assert package_path.is_file()

    install_exit = neko_plugin_cli.main(
        [
            "install",
            str(package_path),
            "--plugins-root",
            str(plugins_root),
            "--profiles-root",
            str(profiles_root),
            "--on-conflict",
            "fail",
        ]
    )
    assert install_exit == 2
    assert not plugins_root.exists()
    assert not profiles_root.exists()

    captured = capsys.readouterr()
    assert "Plugin Center" in captured.err
    assert "does not write plugin runtime directories" in captured.err


def test_cli_build_profile_prefers_config_example_over_legacy_manifest_config(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "config.example.toml").write_text(
        "[plugin_runtime]\n"
        "enabled = true\n"
        "auto_start = true\n"
        "\n"
        "[cli_demo]\n"
        'token = "portable-default"\n',
        encoding="utf-8",
    )
    package_path = tmp_path / "cli_demo.neko-plugin"

    assert neko_plugin_cli.main(["build", str(plugin_dir), "-o", str(package_path)]) == 0

    with zipfile.ZipFile(package_path) as archive:
        assert "payload/plugins/cli_demo/config.example.toml" in archive.namelist()
        profile = archive.read("payload/profiles/default.toml").decode("utf-8")
    assert "auto_start = true" in profile
    assert 'token = "portable-default"' in profile
    assert 'token = "demo"' not in profile


def test_cli_runtime_install_is_disabled_before_reading_package_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    package_path = tmp_path / "cli_demo.neko-plugin"
    neko_plugin_cli.main(["build", str(plugin_dir), "-o", str(package_path)])
    _tamper_package(package_path, "payload/profiles/default.toml")

    exit_code = neko_plugin_cli.main(
        [
            "install",
            str(package_path),
            "--plugins-root",
            str(tmp_path / "plugins"),
            "--profiles-root",
            str(tmp_path / "profiles"),
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "Plugin Center" in captured.err
    assert "payload hash mismatch" not in captured.err
    assert not (tmp_path / "plugins").exists()
    assert not (tmp_path / "profiles").exists()


def test_cli_build_bundle_reports_package_shape(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_plugin = _make_plugin_dir(tmp_path, plugin_id="bundle_cli_one")
    second_plugin = _make_plugin_dir(tmp_path, plugin_id="bundle_cli_two")
    target_dir = tmp_path / "target"

    exit_code = neko_plugin_cli.main(
        [
            "build",
            str(first_plugin),
            str(second_plugin),
            "-b",
            "--bundle-id",
            "bundle_cli_demo",
            "--target-dir",
            str(target_dir),
        ]
    )
    assert exit_code == 0

    package_path = target_dir / "bundle_cli_demo.neko-bundle"
    assert package_path.is_file()

    captured = capsys.readouterr()
    assert "package_type=bundle" in captured.out
    assert "plugin_count=2" in captured.out


def test_cli_build_multiple_plugins_without_bundle_builds_individual_packages(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_plugin = _make_plugin_dir(tmp_path, plugin_id="multi_one")
    second_plugin = _make_plugin_dir(tmp_path, plugin_id="multi_two")
    target_dir = tmp_path / "target"

    exit_code = neko_plugin_cli.main(["build", str(first_plugin), str(second_plugin), "-t", str(target_dir)])

    assert exit_code == 0
    assert (target_dir / "multi_one.neko-plugin").is_file()
    assert (target_dir / "multi_two.neko-plugin").is_file()
    assert not list(target_dir.glob("*.neko-bundle"))
    captured = capsys.readouterr()
    assert "Completed: built=2, failed=0" in captured.out


def test_cli_build_out_does_not_create_unused_target_dir(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    package_path = tmp_path / "cli_demo.neko-plugin"
    unused_target = tmp_path / "unused-target"

    exit_code = neko_plugin_cli.main(["build", str(plugin_dir), "-o", str(package_path), "-t", str(unused_target)])

    assert exit_code == 0
    assert package_path.is_file()
    assert not unused_target.exists()


def test_cli_check_uses_new_label(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)

    exit_code = neko_plugin_cli.main(["check", str(plugin_dir)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "[OK] cli_demo: check found" in captured.out


def test_cli_check_release_uses_release_check_flow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)

    exit_code = neko_plugin_cli.main(["check", str(plugin_dir), "--release", "--skip-tests"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "check --release blocked by validation errors" in captured.err


@pytest.mark.parametrize(
    "removed_command",
    [
        "add",
        "doctor",
        "inspect",
        "pack",
        "release-check",
        "unpack",
        "validate",
        "verify",
    ],
)
def test_cli_removed_commands_are_rejected(
    removed_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        neko_plugin_cli.main([removed_command, "cli_demo"])

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert f"invalid choice: '{removed_command}'" in captured.err


def test_cli_help_only_lists_supported_package_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert neko_plugin_cli.main([]) == 1

    help_text = capsys.readouterr().out
    assert "neko-plugin sync <plugin>" in help_text
    assert "neko-plugin check -r <plugin>" in help_text
    assert "neko-plugin add " not in help_text
    assert "inspect" not in help_text
    assert "verify" not in help_text


def test_check_warns_when_builtin_source_directory_does_not_match_plugin_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_root = tmp_path / "plugin"
    plugins_root = plugin_root / "plugins"
    plugin_dir = _make_plugin_dir(plugins_root, "right_id")
    wrong_dir = plugins_root / "wrong_dir"
    plugin_dir.rename(wrong_dir)
    defaults = CliDefaults(
        plugin_root=plugin_root,
        target_dir=plugin_root / "neko_plugin_cli" / "target",
        plugins_root=plugins_root,
        profiles_root=plugin_root / ".neko-package-profiles",
    )
    monkeypatch.setattr(neko_plugin_cli, "resolve_default_paths", lambda: defaults)

    exit_code = neko_plugin_cli.main(["check", "wrong_dir"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "plugin.id 'right_id' does not match directory name 'wrong_dir'" in output
    assert "plugin.id 'right_id' 与目录名 'wrong_dir' 不一致" in output
    assert "plugin.id 'right_id' がディレクトリ名 'wrong_dir' と一致しません" in output
    assert "将目录重命名为与插件 ID 相同的名称" in output


def test_check_allows_standalone_source_directory_name_to_differ_from_plugin_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path, "right_id")
    standalone_dir = tmp_path / "standalone_source"
    plugin_dir.rename(standalone_dir)
    plugin_root = tmp_path / "neko" / "plugin"
    defaults = CliDefaults(
        plugin_root=plugin_root,
        target_dir=plugin_root / "neko_plugin_cli" / "target",
        plugins_root=plugin_root / "plugins",
        profiles_root=plugin_root / ".neko-package-profiles",
    )
    monkeypatch.setattr(neko_plugin_cli, "resolve_default_paths", lambda: defaults)

    exit_code = neko_plugin_cli.main(["check", str(standalone_dir)])

    assert exit_code == 0
    assert "does not match directory name" not in capsys.readouterr().out


def test_validate_plugin_dir_reports_invalid_toml_without_crashing(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "bad_toml"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.toml").write_text("[plugin\n", encoding="utf-8")

    issues = validate_plugin_dir(plugin_dir)

    assert any(level == "error" and "plugin.toml could not be read" in message for level, message in issues)


def test_validate_plugin_dir_accepts_install_declaration_and_i18n_directory(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_install_declaration(plugin_dir)

    issues = validate_plugin_dir(plugin_dir)

    assert not any(level == "error" and "install" in message for level, message in issues)
    assert not any("[plugin].install is not a recognized" in message for _level, message in issues)


def test_validate_plugin_dir_accepts_missing_and_empty_disabled_install_declaration(
    tmp_path: Path,
) -> None:
    without_install = _make_plugin_dir(tmp_path / "without")
    assert not any(
        level == "error" and "install" in message
        for level, message in validate_plugin_dir(without_install)
    )

    disabled = _make_plugin_dir(tmp_path / "disabled")
    _append_raw_install_declaration(
        disabled,
        """
        [plugin.install]
        enabled = false
        """,
    )
    assert not any(
        level == "error" and "install" in message
        for level, message in validate_plugin_dir(disabled)
    )


@pytest.mark.parametrize(
    "declaration",
    [
        """
        [plugin.install]
        enabled = true
        unknown = true
        """,
        """
        [plugin.install]
        enabled = true
        [plugin.install.kinds.models]
        entry_id = "demo_install_models"
        label = "Models"
        queued_message = "Models queued"
        entry_timeout = 600.0
        unknown = true
        """,
    ],
)
def test_validate_plugin_dir_rejects_unknown_install_fields(
    tmp_path: Path,
    declaration: str,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_raw_install_declaration(plugin_dir, declaration)

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "not a recognized plugin.toml field" in message
        for level, message in issues
    )


@pytest.mark.parametrize("kind", ["RapidOCR", "rapid-ocr", "1ocr"])
def test_validate_plugin_dir_rejects_noncanonical_install_kind(
    tmp_path: Path,
    kind: str,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_raw_install_declaration(
        plugin_dir,
        f"""
        [plugin.install]
        enabled = true
        [plugin.install.kinds.{kind}]
        entry_id = "demo_install_models"
        label = "Models"
        queued_message = "Models queued"
        entry_timeout = 600.0
        """,
    )

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "must match ^[a-z][a-z0-9_]*$" in message
        for level, message in issues
    )


@pytest.mark.parametrize(
    "declaration",
    [
        """
        [plugin.install]
        tutorial_enabled = false
        """,
        """
        [plugin.install]
        enabled = "true"
        """,
        """
        [plugin.install]
        enabled = false
        tutorial_enabled = true
        """,
        """
        [plugin.install]
        enabled = false
        ui_i18n_dir = "i18n/ui"
        """,
        """
        [plugin.install]
        enabled = false
        [plugin.install.kinds.models]
        entry_id = "demo_install_models"
        label = "Models"
        queued_message = "Models queued"
        entry_timeout = 600.0
        """,
    ],
)
def test_validate_plugin_dir_rejects_invalid_or_contradictory_install_enablement(
    tmp_path: Path,
    declaration: str,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_raw_install_declaration(plugin_dir, declaration)

    issues = validate_plugin_dir(plugin_dir)

    assert any(level == "error" and "install" in message for level, message in issues)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entry_id", ""),
        ("entry_id", " padded"),
        ("label", ""),
        ("label", "padded "),
        ("queued_message", ""),
        ("queued_message", " padded "),
    ],
)
def test_validate_plugin_dir_rejects_empty_or_padded_install_text(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    fields = {
        "entry_id": "demo_install_models",
        "label": "Models",
        "queued_message": "Models queued",
    }
    fields[field] = value
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_raw_install_declaration(
        plugin_dir,
        "\n".join(
            [
                "[plugin.install]",
                "enabled = true",
                "[plugin.install.kinds.models]",
                f'entry_id = {json.dumps(fields["entry_id"])}',
                f'label = {json.dumps(fields["label"])}',
                f'queued_message = {json.dumps(fields["queued_message"])}',
                "entry_timeout = 600.0",
            ]
        ),
    )

    issues = validate_plugin_dir(plugin_dir)

    assert any(level == "error" and field in message for level, message in issues)


@pytest.mark.parametrize("ui_i18n_dir", ["../outside", "/absolute/i18n"])
def test_validate_plugin_dir_rejects_install_i18n_escape(
    tmp_path: Path,
    ui_i18n_dir: str,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_install_declaration(plugin_dir, ui_i18n_dir=ui_i18n_dir)

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "ui_i18n_dir" in message
        for level, message in issues
    )


def test_validate_plugin_dir_rejects_missing_install_i18n_directory(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_install_declaration(plugin_dir, ui_i18n_dir="missing-i18n")

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "ui_i18n_dir" in message
        for level, message in issues
    )


def test_validate_plugin_dir_rejects_install_i18n_symlink_escape(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    outside = tmp_path / "outside-i18n"
    outside.mkdir()
    link = plugin_dir / "linked-i18n"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    _append_install_declaration(plugin_dir, ui_i18n_dir="linked-i18n")

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "ui_i18n_dir" in message
        for level, message in issues
    )


@pytest.mark.parametrize("timeout", ["true", "0", "-1", '"600"', "nan", "inf"])
def test_validate_plugin_dir_rejects_invalid_install_timeout(
    tmp_path: Path,
    timeout: str,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    _append_install_declaration(plugin_dir, timeout=timeout)

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "entry_timeout" in message
        for level, message in issues
    )


def test_validate_plugin_dir_reports_invalid_config_example_without_crashing(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "config.example.toml").write_text("[plugin_runtime\n", encoding="utf-8")

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "config.example.toml could not be read" in message
        for level, message in issues
    )


def test_validate_plugin_dir_rejects_plugin_identity_in_config_example(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "config.example.toml").write_text(
        "[plugin]\n"
        'id = "hijack"\n'
        "\n"
        "[plugin_runtime]\n"
        "enabled = true\n",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "config.example.toml must not contain [plugin]" in message
        for level, message in issues
    )


def test_validate_plugin_dir_checks_runtime_fields_in_config_example(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "config.example.toml").write_text(
        "[plugin_runtime]\n"
        "enabled = true\n"
        "timeout = 0\n",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "[plugin_runtime].timeout must be > 0" in message
        for level, message in issues
    )


def test_validate_plugin_dir_warns_without_rejecting_legacy_mixed_config(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)

    issues = validate_plugin_dir(plugin_dir)

    assert not any(level == "error" for level, _message in issues)
    assert any(
        level == "warning" and "move mutable defaults to config.example.toml" in message
        for level, message in issues
    )


def test_validate_plugin_dir_reports_invalid_utf8_optional_files(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / ".vscode").mkdir()
    (plugin_dir / ".vscode" / "settings.json").write_bytes(b"\xff")
    (plugin_dir / ".gitignore").write_bytes(b"\xff")

    issues = validate_plugin_dir(plugin_dir, strict=False)
    messages = [message for _level, message in issues]

    assert any(".vscode/settings.json is not valid UTF-8" in message for message in messages)
    assert any(".gitignore is not valid UTF-8" in message for message in messages)


def test_validate_plugin_dir_accepts_previous_plugin_ids(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    manifest_path = plugin_dir / "plugin.toml"
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        manifest.replace('name = "CLI Demo"', 'name = "CLI Demo"\nprevious_ids = ["legacy_cli_demo"]'),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir, strict=False)

    assert not any("previous_ids is not a recognized" in message for _level, message in issues)


def test_validate_plugin_dir_accepts_i18n_ui_surface_title(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "onboarding.md").write_text("Guide\n", encoding="utf-8")
    manifest_path = plugin_dir / "plugin.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\n"
        + "[plugin.ui]\n"
        + "enabled = true\n"
        + "\n"
        + "[[plugin.ui.docs]]\n"
        + 'id = "onboarding"\n'
        + 'title = { "$i18n" = "docs.onboarding.title", default = "Onboarding" }\n'
        + 'entry = "onboarding.md"\n',
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    assert not any(
        level == "error" and "[plugin.ui].docs[0].title" in message
        for level, message in issues
    )


@pytest.mark.parametrize("removed_type", ["script", "extension"])
def test_validate_plugin_dir_rejects_removed_plugin_types(
    tmp_path: Path,
    removed_type: str,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_toml_path = plugin_dir / "plugin.toml"
    plugin_toml_path.write_text(
        plugin_toml_path.read_text(encoding="utf-8").replace(
            'type = "plugin"',
            f'type = "{removed_type}"',
        ),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    assert any(
        level == "error" and "[plugin].type must be one of" in message
        for level, message in issues
    )



def test_validate_plugin_dir_warns_for_each_literal_push_message_v1_keyword(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "__init__.py").write_text(
        """from plugin.sdk.plugin import neko_plugin

@neko_plugin
class DemoPlugin:
    def emit(self):
        self.ctx.push_message(
            message_type="proactive_notification",
            description="label",
            content="payload",
            binary_data=b"image",
            binary_url="https://example.test/image.png",
            mime="image/png",
            unsafe=True,
            fast_mode=True,
            delivery="proactive",
            reply=True,
        )
""",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    warnings = [
        message
        for level, message in issues
        if level == "warning" and "push_message v1 keyword" in message
    ]
    expected_fields = {
        "message_type",
        "description",
        "content",
        "binary_data",
        "binary_url",
        "mime",
        "unsafe",
        "fast_mode",
        "delivery",
        "reply",
    }
    assert len(warnings) == len(expected_fields)
    for field in expected_fields:
        warning = next(message for message in warnings if f"'{field}'" in message)
        path, line, detail = warning.split(":", 2)
        assert path == "__init__.py"
        assert line.isdigit()
        assert "migrate to" in detail
        assert "before removal in v0.9" in detail
        assert "已弃用" in detail
        assert "非推奨" in detail


def test_validate_plugin_dir_warns_for_existing_style_push_message_call(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "__init__.py").write_text(
        """from plugin.sdk.plugin import neko_plugin

@neko_plugin
class DemoPlugin:
    def emit(self):
        self.ctx.push_message(source="demo", message_type="text", content="payload")
""",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    warnings = [
        message
        for level, message in issues
        if level == "warning" and "push_message v1 keyword" in message
    ]
    assert len(warnings) == 2
    assert all(message.startswith("__init__.py:6:") for message in warnings)
    assert any("'message_type'" in message for message in warnings)
    assert any("'content'" in message for message in warnings)


def test_validate_plugin_dir_ignores_push_message_v2_and_inactive_legacy_defaults(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "__init__.py").write_text(
        """from plugin.sdk.plugin import neko_plugin

def push_message(*, visibility, ai_behavior, parts):
    return visibility, ai_behavior, parts

@neko_plugin
class DemoPlugin:
    def emit(self):
        push_message(visibility=["chat"], ai_behavior="blind", parts=[])
        self.ctx.push_message(
            visibility=["chat"],
            ai_behavior="blind",
            parts=[{"type": "text", "text": "payload"}],
            content=None,
            unsafe=False,
            fast_mode=False,
        )
""",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    assert not any("push_message v1 keyword" in message for _level, message in issues)


def test_validate_plugin_dir_warns_for_dynamic_legacy_boolean_flags(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "__init__.py").write_text(
        """from plugin.sdk.plugin import neko_plugin

@neko_plugin
class DemoPlugin:
    def emit(self, enabled):
        self.ctx.push_message(parts=[], unsafe=enabled, fast_mode=enabled)
""",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    warnings = [
        message
        for level, message in issues
        if level == "warning" and "push_message v1 keyword" in message
    ]
    assert len(warnings) == 2
    assert any("'unsafe'" in message for message in warnings)
    assert any("'fast_mode'" in message for message in warnings)


def test_validate_plugin_dir_ignores_unrelated_local_push_message_function(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "__init__.py").write_text(
        """def push_message(*, content):
    return content

push_message(content="not an SDK call")
""",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    assert not any("push_message v1 keyword" in message for _level, message in issues)


def test_validate_plugin_dir_warns_for_legacy_positional_push_message_fields(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    (plugin_dir / "__init__.py").write_text(
        """def emit(ctx):
    ctx.push_message(
        "demo", "text", "label", 5, "payload", None, None, {}, True, True
    )
""",
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir)

    warnings = [
        message
        for level, message in issues
        if level == "warning" and "push_message v1 keyword" in message
    ]
    assert len(warnings) == 5
    for field in ("message_type", "description", "content", "unsafe", "fast_mode"):
        assert any(f"'{field}'" in message for message in warnings)


@pytest.mark.parametrize("unsupported_scaffold_type", ["script", "extension"])
def test_init_rejects_removed_or_deprecated_scaffold_types(
    unsupported_scaffold_type: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        neko_plugin_cli.main(["init", "demo", "--type", unsupported_scaffold_type])

    assert exc_info.value.code == 2
    assert f"invalid choice: '{unsupported_scaffold_type}'" in capsys.readouterr().err


@pytest.mark.parametrize("unsupported_scaffold_type", ["script", "extension"])
def test_generator_rejects_removed_or_deprecated_scaffold_types(
    tmp_path: Path,
    unsupported_scaffold_type: str,
) -> None:
    target_dir = tmp_path / unsupported_scaffold_type

    with pytest.raises(ValueError) as exc_info:
        generate_plugin(
            PluginSpec(
                plugin_id="demo_plugin",
                plugin_type=unsupported_scaffold_type,
            ),
            target_dir,
            repo_root=tmp_path,
        )

    message = str(exc_info.value)
    assert "不支持" in message and "not supported" in message and "サポートされていません" in message
    assert not target_dir.exists()


def test_validate_plugin_dir_accepts_startup_failure_policy(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_toml_path = plugin_dir / "plugin.toml"
    plugin_toml_path.write_text(
        plugin_toml_path.read_text(encoding="utf-8").replace(
            "auto_start = false",
            "auto_start = false\nstartup_failure = \"warn\"\ntimeout = 1.5",
        ),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir, strict=True)
    messages = [message for _level, message in issues]

    assert not any("[plugin_runtime].startup_failure is not a recognized" in message for message in messages)
    assert not any(level == "error" and "startup_failure" in message for level, message in issues)


def test_validate_plugin_dir_rejects_invalid_startup_failure_policy(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_toml_path = plugin_dir / "plugin.toml"
    plugin_toml_path.write_text(
        plugin_toml_path.read_text(encoding="utf-8").replace(
            "auto_start = false",
            "auto_start = false\nstartup_failure = \"strict\"",
        ),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir, strict=True)

    assert any(
        level == "error" and "[plugin_runtime].startup_failure must be one of" in message
        for level, message in issues
    )


def test_validate_plugin_dir_rejects_non_positive_runtime_timeout(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_toml_path = plugin_dir / "plugin.toml"
    plugin_toml_path.write_text(
        plugin_toml_path.read_text(encoding="utf-8").replace(
            "auto_start = false",
            "auto_start = false\ntimeout = 0",
        ),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir, strict=True)

    assert any(
        level == "error" and "[plugin_runtime].timeout must be > 0" in message
        for level, message in issues
    )


def test_validate_plugin_dir_rejects_too_large_runtime_timeout(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_toml_path = plugin_dir / "plugin.toml"
    plugin_toml_path.write_text(
        plugin_toml_path.read_text(encoding="utf-8").replace(
            "auto_start = false",
            "auto_start = false\ntimeout = 300.1",
        ),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir, strict=True)

    assert any(
        level == "error" and "[plugin_runtime].timeout must be <= 300" in message
        for level, message in issues
    )


def test_validate_accepts_nested_module_under_canonical_plugin_package(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    nested_package = plugin_dir / "runtime"
    nested_package.mkdir()
    (plugin_dir / "__init__.py").replace(nested_package / "__init__.py")
    plugin_toml = plugin_dir / "plugin.toml"
    plugin_toml.write_text(
        plugin_toml.read_text(encoding="utf-8").replace(
            "plugin.plugins.cli_demo:DemoPlugin",
            "plugin.plugins.cli_demo.runtime:DemoPlugin",
        ),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir, strict=True)

    assert not any(
        "plugin.entry should usually target" in message
        for _level, message in issues
    )
    assert not any(
        level == "error" and "plugin.entry" in message
        for level, message in issues
    )


@pytest.mark.parametrize("timeout_literal", ["nan", "inf", "-inf"])
def test_validate_plugin_dir_rejects_non_finite_runtime_timeout(
    tmp_path: Path,
    timeout_literal: str,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path)
    plugin_toml_path = plugin_dir / "plugin.toml"
    plugin_toml_path.write_text(
        plugin_toml_path.read_text(encoding="utf-8").replace(
            "auto_start = false",
            f"auto_start = false\ntimeout = {timeout_literal}",
        ),
        encoding="utf-8",
    )

    issues = validate_plugin_dir(plugin_dir, strict=True)

    assert any(
        level == "error" and "[plugin_runtime].timeout must be finite" in message
        for level, message in issues
    )


def test_init_uses_market_repository_name_and_keeps_plugin_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = neko_plugin_cli.main(
        [
            "init",
            "market_demo",
            "--output",
            str(tmp_path / "n.e.k.o_plugin_market_demo"),
        ]
    )

    repo_dir = tmp_path / "n.e.k.o_plugin_market_demo"
    assert exit_code == 0
    assert repo_dir.is_dir()
    assert not (tmp_path / "market_demo").exists()
    plugin_toml_text = (repo_dir / "plugin.toml").read_text(encoding="utf-8")
    config_example_text = (repo_dir / "config.example.toml").read_text(encoding="utf-8")
    assert 'id = "market_demo"' in plugin_toml_text
    assert 'entry = "plugin.plugins.market_demo:MarketDemoPlugin"' in plugin_toml_text
    assert "[plugin_runtime]" not in plugin_toml_text
    assert config_example_text == "[plugin_runtime]\nenabled = true\nauto_start = false\n"
    assert "store.db" in (repo_dir / ".gitignore").read_text(encoding="utf-8")
    assert (repo_dir / ".github" / "workflows" / "verify.yml").is_file()
    release_workflow = repo_dir / ".github" / "workflows" / "release.yml"
    assert release_workflow.is_file()
    release_workflow_text = release_workflow.read_text(encoding="utf-8")
    assert (
        "uses: Project-N-E-K-O/N.E.K.O/.github/workflows/"
        "plugin-market-release.yml@main"
    ) in release_workflow_text
    assert "plugin-id: market_demo" in release_workflow_text

    messages = [message for _level, message in validate_plugin_dir(repo_dir, strict=True)]
    assert not any("does not match directory name" in message for message in messages)
    assert not any("plugin.entry should usually start with" in message for message in messages)

    check_exit = neko_plugin_cli.main(["check", "market_demo", "--plugins-root", str(tmp_path)])
    assert check_exit == 0
    captured = capsys.readouterr()
    assert "n.e.k.o_plugin_market_demo" in captured.out
    assert "[OK] market_demo: check found" in captured.out


def test_init_generates_market_compatible_ruff_gate(tmp_path: Path) -> None:
    exit_code = neko_plugin_cli.main(
        [
            "init",
            "ruff_demo",
            "--output",
            str(tmp_path / "n.e.k.o_plugin_ruff_demo"),
        ]
    )

    assert exit_code == 0
    repo_dir = tmp_path / "n.e.k.o_plugin_ruff_demo"
    ruff_config = (repo_dir / "ruff.toml").read_text(encoding="utf-8")
    verify_workflow = (
        repo_dir / ".github" / "workflows" / "verify.yml"
    ).read_text(encoding="utf-8")
    release_workflow = (
        repo_dir / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    readme = (repo_dir / "README.md").read_text(encoding="utf-8")

    assert ruff_config == '''# Generated by neko-plugin
# managed-template: plugin-market-actions-v1
target-version = "py311"
line-length = 120
extend-exclude = ["vendor"]
respect-gitignore = false

[lint]
select = ["E4", "E7", "E9", "F", "I"]
'''

    assert "plugin-market-verify.yml@main" in verify_workflow
    assert "plugin-market-release.yml@main" in release_workflow
    assert "uvx ruff" not in verify_workflow
    assert "uvx ruff" not in release_workflow
    assert '''From this plugin repository root:

```bash
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```''' in readme


def test_generated_ruff_config_ignores_vendor_but_checks_plugin_source(
    tmp_path: Path,
) -> None:
    assert (
        neko_plugin_cli.main(
            [
                "init",
                "ruff_scope_demo",
                "--output",
                str(tmp_path / "n.e.k.o_plugin_ruff_scope_demo"),
            ]
        )
        == 0
    )

    repo_dir = tmp_path / "n.e.k.o_plugin_ruff_scope_demo"
    vendor_file = repo_dir / "vendor" / "dependency.py"
    vendor_file.parent.mkdir()
    vendor_file.write_text("import os\n", encoding="utf-8")
    ruff_command = [
        "uvx",
        "ruff==0.12.4",
        "check",
        "--ignore-noqa",
        "--config",
        "ruff.toml",
        ".",
    ]

    vendor_result = subprocess.run(
        ruff_command,
        cwd=repo_dir,
        capture_output=True,
        check=False,
        text=True,
    )
    assert vendor_result.returncode == 0, vendor_result.stdout + vendor_result.stderr

    (repo_dir / "bad_plugin.py").write_text("import os\n", encoding="utf-8")
    plugin_result = subprocess.run(
        ruff_command,
        cwd=repo_dir,
        capture_output=True,
        check=False,
        text=True,
    )
    assert plugin_result.returncode == 1
    assert "bad_plugin.py" in plugin_result.stdout
    assert "F401" in plugin_result.stdout


def test_init_adapter_scaffold_uses_ruff_clean_imports(tmp_path: Path) -> None:
    assert (
        neko_plugin_cli.main(
            [
                "init",
                "adapter_demo",
                "--type",
                "adapter",
                "--output",
                str(tmp_path / "n.e.k.o_plugin_adapter_demo"),
            ]
        )
        == 0
    )

    plugin_source = (
        tmp_path / "n.e.k.o_plugin_adapter_demo" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert plugin_source.startswith(
        '''from typing import Any

from plugin.sdk.adapter import NekoAdapterPlugin
from plugin.sdk.plugin import Ok, lifecycle, neko_plugin, plugin_entry
'''
    )


def test_init_adapter_can_build_market_release_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = tmp_path / "n.e.k.o_plugin_adapter_demo"
    assert (
        neko_plugin_cli.main(
            [
                "init",
                "adapter_demo",
                "--type",
                "adapter",
                "--output",
                str(plugin_dir),
            ]
        )
        == 0
    )
    monkeypatch.setenv(
        "GITHUB_REPOSITORY",
        "alice/n.e.k.o_plugin_adapter_demo",
    )
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.1.0")
    target_dir = tmp_path / "target"

    exit_code = neko_plugin_cli.main(
        [
            "check",
            "--release",
            "--market-release",
            "--skip-tests",
            "--target-dir",
            str(target_dir),
            str(plugin_dir),
        ]
    )

    assert exit_code == 0
    with zipfile.ZipFile(target_dir / "adapter_demo.neko-plugin") as archive:
        plugin_toml = archive.read(
            "payload/plugins/adapter_demo/plugin.toml"
        ).decode("utf-8")
    assert 'type = "adapter"' in plugin_toml


def test_setup_repo_github_actions_preserves_files_unless_overwrite(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "setup_demo")
    setup_args = [
        "setup-repo",
        str(plugin_dir),
        "--github-actions",
        "--no-readme",
        "--no-tests",
        "--no-gitignore",
        "--no-vscode",
    ]

    assert neko_plugin_cli.main(setup_args) == 0
    ruff_config = plugin_dir / "ruff.toml"
    verify_workflow = plugin_dir / ".github" / "workflows" / "verify.yml"
    release_workflow = plugin_dir / ".github" / "workflows" / "release.yml"
    assert ruff_config.is_file()
    assert verify_workflow.is_file()
    assert release_workflow.is_file()

    ruff_config.write_text("# custom Ruff policy\n", encoding="utf-8")
    verify_workflow.write_text("# custom verify workflow\n", encoding="utf-8")
    assert neko_plugin_cli.main(setup_args) == 0
    assert ruff_config.read_text(encoding="utf-8") == "# custom Ruff policy\n"
    assert verify_workflow.read_text(encoding="utf-8") == "# custom verify workflow\n"

    assert neko_plugin_cli.main([*setup_args, "--overwrite"]) == 0
    assert "target-version" in ruff_config.read_text(encoding="utf-8")
    assert "plugin-market-verify.yml" in verify_workflow.read_text(encoding="utf-8")


def test_setup_repo_generates_support_templates_for_explicit_plugin_path(
    tmp_path: Path,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "setup_layout")

    assert neko_plugin_cli.main(["setup-repo", str(plugin_dir)]) == 0

    readme = (plugin_dir / "README.md").read_text(encoding="utf-8")
    settings = (plugin_dir / ".vscode" / "settings.json").read_text(
        encoding="utf-8"
    )
    tasks = (plugin_dir / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    assert "From this plugin repository root" in readme
    assert "uv run --project" in readme
    assert "neko-plugin check ." in readme
    assert "neko-plugin publish ." in readme
    assert '"nekoPlugin.repoRoot":' in settings
    assert '"cwd": "${config:nekoPlugin.repoRoot}"' in tasks
    assert "${workspaceFolder}" in tasks


def test_setup_repo_upgrade_github_actions_adds_only_managed_action_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "upgrade_demo")

    assert (
        neko_plugin_cli.main(
            ["setup-repo", str(plugin_dir), "--upgrade-github-actions"]
        )
        == 0
    )

    assert (plugin_dir / "ruff.toml").is_file()
    assert (plugin_dir / ".github" / "workflows" / "verify.yml").is_file()
    assert (plugin_dir / ".github" / "workflows" / "release.yml").is_file()
    assert not (plugin_dir / "README.md").exists()
    assert not (plugin_dir / "tests").exists()
    assert not (plugin_dir / ".gitignore").exists()
    assert not (plugin_dir / ".vscode").exists()

    captured = capsys.readouterr()
    assert "[ADD] ruff.toml" in captured.out
    assert "[ADD] .github/workflows/verify.yml" in captured.out
    assert "[ADD] .github/workflows/release.yml" in captured.out


def test_setup_repo_upgrade_github_actions_aborts_atomically_on_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "conflict_demo")
    verify_workflow = plugin_dir / ".github" / "workflows" / "verify.yml"
    verify_workflow.parent.mkdir(parents=True)
    verify_workflow.write_text("# custom verification\n", encoding="utf-8")

    assert (
        neko_plugin_cli.main(
            ["setup-repo", str(plugin_dir), "--upgrade-github-actions"]
        )
        == 1
    )

    assert verify_workflow.read_text(encoding="utf-8") == "# custom verification\n"
    assert not (plugin_dir / "ruff.toml").exists()
    assert not (plugin_dir / ".github" / "workflows" / "release.yml").exists()
    captured = capsys.readouterr()
    assert "[CONFLICT] .github/workflows/verify.yml" in captured.err
    assert "No files were changed." in captured.err


def test_setup_repo_upgrade_github_actions_treats_invalid_parent_as_conflict(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "parent_conflict_demo")
    github_path = plugin_dir / ".github"
    github_path.write_text("not a directory\n", encoding="utf-8")

    assert (
        neko_plugin_cli.main(
            ["setup-repo", str(plugin_dir), "--upgrade-github-actions"]
        )
        == 1
    )

    assert github_path.read_text(encoding="utf-8") == "not a directory\n"
    assert not (plugin_dir / "ruff.toml").exists()
    captured = capsys.readouterr()
    assert "[CONFLICT] .github/workflows/verify.yml" in captured.err
    assert "[CONFLICT] .github/workflows/release.yml" in captured.err
    assert "No files were changed." in captured.err


def test_setup_repo_upgrade_github_actions_dry_run_only_prints_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "dry_run_demo")

    assert (
        neko_plugin_cli.main(
            [
                "setup-repo",
                str(plugin_dir),
                "--upgrade-github-actions",
                "--dry-run",
            ]
        )
        == 0
    )

    assert not (plugin_dir / "ruff.toml").exists()
    assert not (plugin_dir / ".github").exists()
    captured = capsys.readouterr()
    assert "[ADD] ruff.toml" in captured.out
    assert "Dry run; no files were changed." in captured.out


def test_setup_repo_upgrade_github_actions_replaces_recognized_legacy_templates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "legacy_demo")
    ruff_config = plugin_dir / "ruff.toml"
    verify_workflow = plugin_dir / ".github" / "workflows" / "verify.yml"
    release_workflow = plugin_dir / ".github" / "workflows" / "release.yml"
    verify_workflow.parent.mkdir(parents=True)
    ruff_config.write_text(
        'target-version = "py311"\n'
        "line-length = 120\n"
        'extend-exclude = ["vendor"]\n'
        "respect-gitignore = false\n\n"
        "[lint]\n"
        'select = ["E4", "E7", "E9", "F", "I"]\n',
        encoding="utf-8",
    )
    verify_workflow.write_text(
        "name: Verify N.E.K.O Plugin\n\n"
        "on:\n  push:\n  pull_request:\n  workflow_dispatch:\n\n"
        "permissions:\n  contents: read\n\n"
        "jobs:\n  verify:\n"
        "    uses: Project-N-E-K-O/N.E.K.O/.github/workflows/"
        "plugin-market-verify.yml@main\n"
        "    with:\n      plugin-id: legacy_demo\n      neko-ref: main\n",
        encoding="utf-8",
    )
    release_workflow.write_text(
        "name: Release N.E.K.O Plugin\n\n"
        "on:\n  push:\n    tags:\n      - \"v*\"\n  workflow_dispatch:\n\n"
        "permissions:\n  contents: write\n\n"
        "jobs:\n  release:\n"
        "    uses: Project-N-E-K-O/N.E.K.O/.github/workflows/"
        "plugin-market-release.yml@main\n"
        "    with:\n      plugin-id: legacy_demo\n      neko-ref: main\n",
        encoding="utf-8",
    )

    assert (
        neko_plugin_cli.main(
            ["setup-repo", str(plugin_dir), "--upgrade-github-actions"]
        )
        == 0
    )

    for path in (ruff_config, verify_workflow, release_workflow):
        assert "managed-template: plugin-market-actions-v1" in path.read_text(
            encoding="utf-8"
        )
    captured = capsys.readouterr()
    assert captured.out.count("[UPGRADE]") == 3


def test_setup_repo_upgrade_github_actions_replaces_real_inline_legacy_workflows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "legacy_demo")
    fixture_dir = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "neko_plugin_cli"
        / "legacy_market_actions"
        / "v0"
    )
    workflow_dir = plugin_dir / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    for workflow_name in ("verify.yml", "release.yml"):
        (workflow_dir / workflow_name).write_text(
            (fixture_dir / workflow_name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    assert (
        neko_plugin_cli.main(
            ["setup-repo", str(plugin_dir), "--upgrade-github-actions"]
        )
        == 0
    )

    assert (plugin_dir / "ruff.toml").is_file()
    for workflow_name in ("verify.yml", "release.yml"):
        workflow = (workflow_dir / workflow_name).read_text(encoding="utf-8")
        assert "managed-template: plugin-market-actions-v1" in workflow
        assert "runs-on:" not in workflow
    captured = capsys.readouterr()
    assert captured.out.count("[ADD]") == 1
    assert captured.out.count("[UPGRADE]") == 2


def test_setup_repo_upgrade_github_actions_rejects_modified_legacy_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "legacy_demo")
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "neko_plugin_cli"
        / "legacy_market_actions"
        / "v0"
        / "verify.yml"
    ).read_text(encoding="utf-8")
    verify_workflow = plugin_dir / ".github" / "workflows" / "verify.yml"
    verify_workflow.parent.mkdir(parents=True)
    modified = fixture.replace(
        "name: Verify N.E.K.O Plugin",
        "name: Custom Plugin Verification",
        1,
    )
    verify_workflow.write_text(modified, encoding="utf-8")

    assert (
        neko_plugin_cli.main(
            ["setup-repo", str(plugin_dir), "--upgrade-github-actions"]
        )
        == 1
    )

    assert verify_workflow.read_text(encoding="utf-8") == modified
    assert not (plugin_dir / "ruff.toml").exists()
    assert not (plugin_dir / ".github" / "workflows" / "release.yml").exists()
    captured = capsys.readouterr()
    assert "[CONFLICT] .github/workflows/verify.yml" in captured.err
    assert "No files were changed." in captured.err


def test_setup_repo_upgrade_github_actions_rejects_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo", "overwrite_demo")

    assert (
        neko_plugin_cli.main(
            [
                "setup-repo",
                str(plugin_dir),
                "--upgrade-github-actions",
                "--overwrite",
            ]
        )
        == 1
    )

    assert not (plugin_dir / "ruff.toml").exists()
    captured = capsys.readouterr()
    assert "--upgrade-github-actions cannot be used with --overwrite" in captured.err


def test_advanced_scaffold_with_github_actions_uses_ruff_clean_imports(
    tmp_path: Path,
) -> None:
    target_dir = tmp_path / "advanced_demo"
    generate_plugin(
        PluginSpec(
            plugin_id="advanced_demo",
            features=[
                "lifecycle",
                "entry_point",
                "timer",
                "message",
                "store",
                "settings",
            ],
            create_github_actions=True,
        ),
        target_dir,
        repo_root=tmp_path,
    )

    plugin_source = (target_dir / "__init__.py").read_text(encoding="utf-8")
    assert plugin_source.startswith(
        '''from typing import Any

from plugin.sdk.plugin import (
    NekoPluginBase,
    Ok,
    PluginSettings,
    PluginStore,
    lifecycle,
    message,
    neko_plugin,
    plugin_entry,
    timer_interval,
)
'''
    )
    assert "    class Settings(PluginSettings):\n        pass\n" in plugin_source


def test_init_documents_and_exposes_dependency_sync(tmp_path: Path) -> None:
    exit_code = neko_plugin_cli.main(
        [
            "init",
            "dependency_demo",
            "--output",
            str(tmp_path / "n.e.k.o_plugin_dependency_demo"),
        ]
    )

    assert exit_code == 0
    repo_dir = tmp_path / "n.e.k.o_plugin_dependency_demo"
    readme = (repo_dir / "README.md").read_text(encoding="utf-8")
    tasks = (repo_dir / ".vscode" / "tasks.json").read_text(encoding="utf-8")

    sync_command = "uv run --with pip --project"
    assert sync_command in readme
    assert "neko-plugin sync . --clean" in readme
    assert "`vendor/`" in readme
    assert "not committed" in readme
    assert "neko-plugin publish ." in readme
    assert "neko-plugin publish github ." in readme
    assert (
        "neko-plugin publish market "
        "https://github.com/owner/repo/releases/tag/v0.1.0"
    ) in readme
    assert "Use that GitHub Release URL when publishing" not in readme
    assert "N.E.K.O: sync dependency_demo" in tasks
    assert (
        'uv run --with pip neko-plugin sync \\"${workspaceFolder}\\" --clean'
        in tasks
    )


def test_sync_without_pyproject_is_successful_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path, "no_pyproject")
    vendor_file = plugin_dir / "vendor" / "legacy_dependency.py"
    vendor_file.parent.mkdir()
    vendor_file.write_text("value = 1\n", encoding="utf-8")

    exit_code = neko_plugin_cli.main(["sync", str(plugin_dir), "--clean"])

    assert exit_code == 0
    assert vendor_file.is_file()
    captured = capsys.readouterr()
    assert "[OK] no_pyproject: no external dependencies to sync" in captured.out


def test_market_release_check_enforces_repo_and_tag_conventions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        neko_plugin_cli.main(
            [
                "init",
                "market_demo",
                "--output",
                str(tmp_path / "n.e.k.o_plugin_market_demo"),
            ]
        )
        == 0
    )

    monkeypatch.setenv("GITHUB_REPOSITORY", "alice/n.e.k.o_plugin_market_demo")
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.1.0")
    assert (
        neko_plugin_cli.main(
            [
                "check",
                str(tmp_path / "n.e.k.o_plugin_market_demo"),
                "--release",
                "--market-release",
                "--skip-tests",
                "--target-dir",
                str(tmp_path / "target"),
            ]
        )
        == 0
    )

    monkeypatch.setenv("GITHUB_REF_NAME", "v9.9.9")
    assert (
        neko_plugin_cli.main(
            [
                "check",
                str(tmp_path / "n.e.k.o_plugin_market_demo"),
                "--release",
                "--market-release",
                "--skip-tests",
                "--target-dir",
                str(tmp_path / "target-bad"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "release tag v9.9.9 does not match plugin.toml version 0.1.0" in captured.err


def test_market_release_check_rejects_legacy_non_market_plugin_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plugin_dir = tmp_path / "n.e.k.o_plugin_market_demo"
    assert (
        neko_plugin_cli.main(
            ["init", "market_demo", "--output", str(plugin_dir)]
        )
        == 0
    )
    plugin_toml = plugin_dir / "plugin.toml"
    plugin_toml.write_text(
        plugin_toml.read_text(encoding="utf-8").replace(
            'id = "market_demo"',
            'id = "Demo"',
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "alice/n.e.k.o_plugin_Demo")
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.1.0")

    exit_code = neko_plugin_cli.main(
        [
            "check",
            str(plugin_dir),
            "--release",
            "--market-release",
            "--skip-tests",
            "--target-dir",
            str(tmp_path / "target"),
        ]
    )

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "Market plugin ID must match ^[a-z][a-z0-9_]*$" in error


def test_setup_repo_git_skips_when_inside_existing_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo")
    (tmp_path / "repo" / ".git").mkdir()
    calls: list[list[str]] = []

    def fake_run_git(command: list[str], *, cwd: Path) -> None:
        calls.append(command)

    monkeypatch.setattr(init_cmd, "_run_git", fake_run_git)

    assert init_cmd._initialize_git_repo(plugin_dir) is False

    assert calls == []


def test_git_remote_requires_new_repository(tmp_path: Path) -> None:
    plugin_dir = _make_plugin_dir(tmp_path / "repo")
    (tmp_path / "repo" / ".git").mkdir()

    with pytest.raises(RuntimeError, match="--remote"):
        init_cmd._initialize_git_repo(plugin_dir, remote="https://example.invalid/demo.git")


def test_git_preflight_remote_fails_before_writing_files(tmp_path: Path) -> None:
    target_dir = tmp_path / "repo" / "demo_plugin"
    (tmp_path / "repo" / ".git").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="--remote"):
        init_cmd._preflight_git_request(
            target_dir,
            initialize_git=True,
            remote="https://example.invalid/demo.git",
        )

    assert not target_dir.exists()


def test_git_preflight_skips_git_binary_check_inside_existing_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_dir = tmp_path / "repo" / "demo_plugin"
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setattr(init_cmd.shutil, "which", lambda _: None)

    init_cmd._preflight_git_request(target_dir, initialize_git=True)
