from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from plugin.neko_plugin_cli.core.build_rules import _DEFAULT_EXCLUDE_DIR_NAMES
from scripts.check_nuitka_dist import (
    _MARKETPLACE_ONLY_PLUGIN_IDS,
    _check_plugin_stage,
    _check_plugin_tomls,
    _plugin_manifest_id,
)
from scripts.prepare_nuitka_plugins import install_plugins, prepare_plugins


def _write(path: Path, content: str = "runtime\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_prepare_and_install_plugins_apply_neko_build_rules(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    plugin_dir = project_root / "plugin" / "plugins" / "demo_plugin"
    _write(project_root / "launcher.py", "print('launcher')\n")
    _write(plugin_dir / "plugin.toml", '[plugin]\nid = "demo_plugin"\n')
    _write(
        plugin_dir / "pyproject.toml",
        "\n".join(
            [
                "[project]",
                'name = "demo-plugin"',
                'version = "0.1.0"',
                "dependencies = []",
                "",
                "[tool.neko.build]",
                'exclude_dirs = ["tests", "local_logs"]',
                'exclude_files = ["README.md"]',
                'exclude = ["*.tmp"]',
                "",
            ]
        ),
    )
    _write(plugin_dir / "__init__.py")
    _write(plugin_dir / "runtime.py")
    _write(plugin_dir / "data layer" / "worker.py")
    _write(plugin_dir / "tests" / "__init__.py")
    _write(plugin_dir / "tests" / "test_runtime.py")
    _write(plugin_dir / ".github" / "workflows" / "verify.yml")
    _write(plugin_dir / "local_logs" / "private.txt")
    _write(plugin_dir / "README.md")
    _write(plugin_dir / "scratch.tmp")
    _write(plugin_dir / "store.db")
    _write(plugin_dir / "runtime.log")

    result = prepare_plugins(
        project_root=project_root,
        plugins_root=Path("plugin/plugins"),
        stage_dir=Path("build/nuitka-plugins"),
        source_launcher=Path("launcher.py"),
        generated_launcher=Path("build_nuitka_launcher.py"),
    )

    stage_plugin = result.stage_dir / "demo_plugin"
    assert (stage_plugin / "runtime.py").is_file()
    assert (stage_plugin / "data layer" / "worker.py").is_file()
    assert not (stage_plugin / "tests").exists()
    assert not (stage_plugin / ".github").exists()
    assert not (stage_plugin / "local_logs").exists()
    assert not (stage_plugin / "README.md").exists()
    assert not (stage_plugin / "scratch.tmp").exists()
    assert not (stage_plugin / "store.db").exists()
    assert not (stage_plugin / "runtime.log").exists()

    generated = result.generated_launcher.read_text(encoding="utf-8")
    assert "--nofollow-import-to=plugin.plugins.demo_plugin.tests" in generated
    assert "plugin.plugins.demo_plugin.data layer" not in generated
    assert generated.endswith("print('launcher')\n")

    manifest = json.loads(
        (result.stage_dir.parent / "nuitka-plugin-stage.json").read_text(encoding="utf-8")
    )
    assert "demo_plugin/tests/" in manifest["excluded_paths"]
    assert "demo_plugin/.github/" in manifest["excluded_paths"]
    assert "plugin.plugins.demo_plugin.tests" in manifest["excluded_modules"]

    destination = project_root / "dist" / "Xiao8" / "plugin" / "plugins"
    _write(destination / "stale_plugin" / "plugin.toml")
    install_plugins(stage_dir=result.stage_dir, destination_dir=destination)

    assert not (destination / "stale_plugin").exists()
    assert (destination / "demo_plugin" / "runtime.py").is_file()
    assert not (destination / ".nuitka-stage.json").exists()


def test_prepare_skips_plugin_directory_missing_plugin_toml(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    plugins_root = project_root / "plugin" / "plugins"
    _write(project_root / "launcher.py")
    _write(plugins_root / "demo" / "plugin.toml", '[plugin]\nid = "demo"\n')
    _write(plugins_root / "demo" / "runtime.py")
    # A plugin dropped from the index leaves its __pycache__ behind; the husk
    # used to reach the payload and trip the dist gate at the end of the build.
    _write(plugins_root / "husk" / "__pycache__" / "runtime.pyc")

    result = prepare_plugins(
        project_root=project_root,
        plugins_root=Path("plugin") / "plugins",
        stage_dir=Path("build") / "stage",
        source_launcher=Path("launcher.py"),
        generated_launcher=Path("build") / "launcher_gen.py",
    )

    assert result.plugin_dirs == ("demo",)
    assert not (result.stage_dir / "husk").exists()
    assert "husk/ (missing plugin.toml)" in result.skipped_entries


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_prepare_skips_entries_absent_from_the_git_index(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    plugins_root = project_root / "plugin" / "plugins"
    _write(project_root / "launcher.py")
    _write(plugins_root / "__init__.py")
    _write(plugins_root / "_shared" / "helper.py")
    _write(plugins_root / "demo" / "plugin.toml", '[plugin]\nid = "demo"\n')
    _write(plugins_root / "demo" / "runtime.py")
    # Complete, loadable and local-only: a private experiment, or a plugin that
    # moved to the marketplace.  Only the index separates it from a built-in.
    _write(plugins_root / "local_only" / "plugin.toml", '[plugin]\nid = "local_only"\n')
    _write(plugins_root / "local_only" / "runtime.py")
    _write(plugins_root / "scratch.txt")

    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "add",
            "launcher.py",
            "plugin/plugins/__init__.py",
            "plugin/plugins/_shared",
            "plugin/plugins/demo",
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
    )

    result = prepare_plugins(
        project_root=project_root,
        plugins_root=Path("plugin") / "plugins",
        stage_dir=Path("build") / "stage",
        source_launcher=Path("launcher.py"),
        generated_launcher=Path("build") / "launcher_gen.py",
    )

    assert result.plugin_dirs == ("_shared", "demo")
    assert not (result.stage_dir / "local_only").exists()
    assert not (result.stage_dir / "scratch.txt").exists()
    assert (result.stage_dir / "demo" / "runtime.py").is_file()
    assert (result.stage_dir / "_shared" / "helper.py").is_file()
    assert (result.stage_dir / "__init__.py").is_file()
    assert set(result.skipped_entries) == {
        "local_only/ (untracked by git)",
        "scratch.txt (untracked by git)",
    }
    manifest_path = result.stage_dir.parent / "nuitka-plugin-stage.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["skipped_entries"] == sorted(result.skipped_entries)


def test_prepare_keeps_shared_plugin_runtime_directory(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    _write(project_root / "launcher.py", "pass\n")
    _write(project_root / "plugin" / "plugins" / "__init__.py")
    _write(project_root / "plugin" / "plugins" / "_shared" / "__init__.py")
    _write(project_root / "plugin" / "plugins" / "_shared" / "helper.py")

    result = prepare_plugins(
        project_root=project_root,
        plugins_root=Path("plugin/plugins"),
        stage_dir=Path("build/nuitka-plugins"),
        source_launcher=Path("launcher.py"),
        generated_launcher=Path("build_nuitka_launcher.py"),
    )

    assert result.plugin_dirs == ("_shared",)
    assert (result.stage_dir / "__init__.py").is_file()
    assert (result.stage_dir / "_shared" / "helper.py").is_file()


def test_prepare_preserves_allowed_bundled_napcat_launcher(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    _write(project_root / "launcher.py", "pass\n")
    plugin_dir = project_root / "plugin" / "plugins" / "qq_auto_reply"
    _write(plugin_dir / "plugin.toml", '[plugin]\nid = "qq_auto_reply"\n')
    _write(plugin_dir / "NapCat.Shell" / "launcher.bat", "@echo off\n")

    result = prepare_plugins(
        project_root=project_root,
        plugins_root=Path("plugin/plugins"),
        stage_dir=Path("build/nuitka-plugins"),
        source_launcher=Path("launcher.py"),
        generated_launcher=Path("build_nuitka_launcher.py"),
    )

    assert (result.stage_dir / "qq_auto_reply" / "NapCat.Shell" / "launcher.bat").is_file()


def test_dist_check_matches_stage_and_allows_shared_directory(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    dist_root = tmp_path / "dist"
    installed = dist_root / "plugin" / "plugins"
    _write(stage / "demo" / "plugin.toml")
    _write(stage / "demo" / "runtime.py")
    _write(stage / "_shared" / "helper.py")
    install_plugins(stage_dir=stage, destination_dir=installed)

    assert _check_plugin_tomls(dist_root) == []
    assert _check_plugin_stage(dist_root, stage) == []

    _write(installed / "demo" / "README.md")
    issues = _check_plugin_stage(dist_root, stage)
    assert len(issues) == 1
    assert "unstaged file" in issues[0]


@pytest.mark.parametrize("plugin_id", sorted(_MARKETPLACE_ONLY_PLUGIN_IDS))
def test_nuitka_dist_rejects_marketplace_only_plugin(
    tmp_path: Path,
    plugin_id: str,
) -> None:
    dist_root = tmp_path / "dist"
    _write(
        dist_root / "plugin" / "plugins" / "demo" / "plugin.toml",
        '[plugin]\nid = "demo"\n',
    )
    _write(
        dist_root / "plugin" / "plugins" / plugin_id / "plugin.toml",
        f'[plugin]\nid = "{plugin_id}"\n',
    )

    issues = _check_plugin_tomls(dist_root)

    assert issues == [
        f"marketplace-only plugin bundled: plugin/plugins/{plugin_id}"
    ]


@pytest.mark.parametrize("plugin_id", sorted(_MARKETPLACE_ONLY_PLUGIN_IDS))
def test_nuitka_dist_rejects_marketplace_only_manifest_id_after_directory_rename(
    tmp_path: Path,
    plugin_id: str,
) -> None:
    dist_root = tmp_path / "dist"
    renamed_dir = f"renamed_{plugin_id}"
    _write(
        dist_root / "plugin" / "plugins" / renamed_dir / "plugin.toml",
        f'[plugin]\nid = "{plugin_id}"\n',
    )

    assert _check_plugin_tomls(dist_root) == [
        f"marketplace-only plugin bundled: plugin/plugins/{renamed_dir}"
    ]


def test_marketplace_only_plugins_are_not_vendored_in_source_tree() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    plugins_root = repo_root / "plugin" / "plugins"

    assert not [
        plugin_dir.name
        for plugin_dir in sorted(path for path in plugins_root.iterdir() if path.is_dir())
        if plugin_dir.name in _MARKETPLACE_ONLY_PLUGIN_IDS
        or _plugin_manifest_id(plugin_dir / "plugin.toml")
        in _MARKETPLACE_ONLY_PLUGIN_IDS
    ]


def test_install_plugins_restores_previous_payload_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    destination = tmp_path / "dist" / "plugin" / "plugins"
    _write(stage / "demo" / "new.py", "new")
    _write(destination / "demo" / "old.py", "old")
    temporary = destination.with_name(destination.name + ".staging")
    original_replace = Path.replace

    def fail_new_payload(path: Path, target: Path) -> Path:
        if path == temporary:
            raise OSError("simulated interrupted replacement")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_new_payload)

    with pytest.raises(OSError, match="simulated interrupted replacement"):
        install_plugins(stage_dir=stage, destination_dir=destination)

    assert (destination / "demo" / "old.py").read_text(encoding="utf-8") == "old"
    assert not (destination / "demo" / "new.py").exists()


def test_desktop_workflows_use_filtered_plugin_stage() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workflow_paths = (
        repo_root / ".github" / "workflows" / "build-desktop.yml",
        repo_root / ".github" / "workflows" / "build-desktop-linux.yml",
    )

    for workflow_path in workflow_paths:
        workflow = workflow_path.read_text(encoding="utf-8")
        workflow_lines = {line.strip() for line in workflow.splitlines()}
        assert "scripts/prepare_nuitka_plugins.py prepare" in workflow
        assert "build_nuitka_launcher.py" in workflow
        assert "scripts/prepare_nuitka_plugins.py install" in workflow
        assert "--plugin-stage build/nuitka-plugins" in workflow
        assert "--include-data-dir=plugin/plugins=plugin/plugins" not in workflow
        assert 'NUITKA_OPTS="$NUITKA_OPTS --nofollow-import-to=plugin.plugins"' not in workflow
        assert "set NUITKA_OPTS=%NUITKA_OPTS% --nofollow-import-to=plugin.plugins" not in workflow_lines


def test_prepare_helper_is_directly_executable_without_pythonpath(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    helper = repo_root / "scripts" / "prepare_nuitka_plugins.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(helper), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "stage plugins and generate launcher" in completed.stdout


def test_prepare_drops_editor_directories_that_break_macos_codesign(tmp_path: Path) -> None:
    """codesign reads any dotted directory under MacOS/ as a nested bundle.

    A plugin that ships .vscode/ or .idea/ used to reach the staged payload
    verbatim, and `codesign --deep` then aborted the whole backend seal with
    "bundle format unrecognized, invalid, or unsuitable" — see the exclusion
    list in plugin/neko_plugin_cli/core/build_rules.py.
    """

    project_root = tmp_path / "repo"
    plugin_dir = project_root / "plugin" / "plugins" / "demo_plugin"
    _write(project_root / "launcher.py", "print('launcher')\n")
    _write(plugin_dir / "plugin.toml", '[plugin]\nid = "demo_plugin"\n')
    _write(plugin_dir / "__init__.py")
    _write(plugin_dir / "runtime.py")
    _write(plugin_dir / ".vscode" / "settings.json", "{}\n")
    _write(plugin_dir / ".vscode" / "tasks.json", "{}\n")
    _write(plugin_dir / ".idea" / "workspace.xml", "<project/>\n")

    result = prepare_plugins(
        project_root=project_root,
        plugins_root=Path("plugin/plugins"),
        stage_dir=Path("build/nuitka-plugins"),
        source_launcher=Path("launcher.py"),
        generated_launcher=Path("build_nuitka_launcher.py"),
    )

    stage_plugin = result.stage_dir / "demo_plugin"
    assert (stage_plugin / "runtime.py").is_file()
    assert not (stage_plugin / ".vscode").exists()
    assert not (stage_plugin / ".idea").exists()

    # Nothing dotted may survive anywhere in the staged tree, whatever its depth.
    assert not [
        path
        for path in result.stage_dir.rglob("*")
        if path.is_dir() and "." in path.name
    ]


def test_no_bundled_plugin_ships_a_dotted_directory_besides_github() -> None:
    """Guard the real tree, not just the staging helper.

    build_rules only skips names it knows. A plugin adding some other dotted
    directory would sail past it and kill the mac build again, so pin the set
    of dotted directories that actually exist under plugin/plugins/.
    """

    repo_root = Path(__file__).resolve().parents[2]
    plugins_root = repo_root / "plugin" / "plugins"
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", str(plugins_root)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )

    offenders = sorted(
        {
            part
            for entry in listed.stdout.split("\0")
            if entry
            for part in Path(entry).parts[:-1]
            if "." in part
        }
        - set(_DEFAULT_EXCLUDE_DIR_NAMES)
    )

    assert not offenders, (
        "dotted directories under plugin/plugins/ break `codesign --deep` on the "
        f"macOS backend bundle; add them to _DEFAULT_EXCLUDE_DIR_NAMES: {offenders}"
    )
