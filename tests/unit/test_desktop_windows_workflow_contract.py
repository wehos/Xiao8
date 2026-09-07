import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CROSS_PLATFORM_WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop.yml"
WINDOWS_WORKFLOW = ROOT / ".github" / "workflows" / "build-desktop-windows.yml"
SYNC_UPDATE_WORKFLOW = ROOT / ".github" / "workflows" / "sync-update-release.yml"
LOCAL_RELEASE_SCRIPT = ROOT / "scripts" / "build-desktop-release.ps1"
LOCAL_ASSET_PUBLISH_SCRIPT = ROOT / "scripts" / "publish-desktop-release-assets.ps1"
MANUAL_DESKTOP_RELEASE_DOC = (
    ROOT / "docs" / "zh-CN" / "deployment" / "manual-desktop-release.md"
)


def _load_workflow(path: Path) -> dict:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _steps_by_name(workflow: dict, job_name: str) -> dict[str, dict]:
    steps = workflow["jobs"][job_name]["steps"]
    return {step["name"]: step for step in steps if "name" in step}


def test_windows_workflow_calls_cross_platform_workflow_in_windows_only_mode() -> None:
    workflow = WINDOWS_WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "uses: ./.github/workflows/build-desktop.yml" in workflow
    assert "version: ${{ inputs.version }}" in workflow
    assert "electron_repo: ${{ inputs.electron_repo }}" in workflow
    assert "electron_ref: ${{ inputs.electron_ref }}" in workflow
    assert "previous_portable_release: ${{ inputs.previous_portable_release }}" in workflow
    assert "allow_fork_build: ${{ inputs.allow_fork_build }}" in workflow
    assert "windows_only: true" in workflow
    assert "secrets: inherit" in workflow
    assert "permissions:" in workflow
    assert "contents: write" in workflow
    assert "macos-" not in workflow
    assert "ubuntu-" not in workflow


def test_cross_platform_workflow_limits_both_matrices_for_windows_only_calls() -> None:
    workflow = _load_workflow(CROSS_PLATFORM_WORKFLOW)
    assert workflow["concurrency"] == {
        "group": "build-desktop-nightly-release",
        "cancel-in-progress": False,
    }
    jobs = workflow["jobs"]
    matrices = [
        jobs["build-python"]["strategy"]["matrix"]["include"],
        jobs["build-electron"]["strategy"]["matrix"]["include"],
    ]

    assert all("inputs.windows_only &&" in matrix for matrix in matrices)
    assert '"artifact_name":"python-backend-win"' in matrices[0]
    assert '"artifact_name":"desktop-win-x64"' in matrices[1]


def test_reusable_build_honors_signing_inputs_and_distribution_wrapper() -> None:
    workflow = _load_workflow(CROSS_PLATFORM_WORKFLOW)
    build_steps = _steps_by_name(workflow, "build-electron")

    disable_macos_signing = build_steps["Disable macOS code signing"]
    assert disable_macos_signing["if"] == (
        "runner.os == 'macOS' && "
        "(github.event_name == 'schedule' || inputs.skip_signing == 'true')"
    )

    unsigned_windows = build_steps[
        "Build Electron app (Windows ZIP Portable directory, unsigned)"
    ]
    assert unsigned_windows["if"] == (
        "runner.os == 'Windows' && "
        "(github.event_name == 'schedule' || inputs.skip_signing == 'true')"
    )
    assert unsigned_windows["run"] == (
        "node scripts/build-electron-distribution.js windows --dir --publish never"
    )
    assert unsigned_windows["env"]["CSC_IDENTITY_AUTO_DISCOVERY"] == "false"
    assert "WIN_CSC_LINK" not in unsigned_windows["env"]
    assert "WIN_CSC_KEY_PASSWORD" not in unsigned_windows["env"]

    signed_windows = build_steps[
        "Build Electron app (Windows ZIP Portable directory, signed)"
    ]
    assert signed_windows["if"] == (
        "runner.os == 'Windows' && github.event_name != 'schedule' "
        "&& inputs.skip_signing != 'true'"
    )
    assert signed_windows["run"] == (
        "node scripts/build-electron-distribution.js windows --dir --publish never"
    )
    assert signed_windows["env"]["WIN_CSC_LINK"] == "${{ secrets.WIN_CSC_LINK }}"
    assert signed_windows["env"]["WIN_CSC_KEY_PASSWORD"] == (
        "${{ secrets.WIN_CSC_KEY_PASSWORD }}"
    )

    distribution = build_steps["Build Electron app (macOS/Linux)"]
    assert distribution["run"] == (
        "node scripts/build-electron-distribution.js "
        "${{ matrix.builder_platform }} ${{ matrix.portable_arch_args }} "
        "${{ matrix.builder_target_args }} --publish never"
    )

    nightly_steps = _steps_by_name(workflow, "nightly")
    windows_nightly = nightly_steps["Create or update Windows nightly release"]
    assert windows_nightly["if"] == "${{ inputs.windows_only }}"
    assert "gh release upload nightly release/* --clobber" in windows_nightly["run"]
    expected_windows_assets = (
        "python-backend-win-*.zip",
        "N.E.K.O_*_win.zip",
        "N.E.K.O_*_win_delta.zip",
        "N.E.K.O_*_win_delta.bin",
        "N.E.K.O_*_win_manifest.json",
        "N.E.K.O_*_win_manifest.json.sig",
    )
    assert all(pattern in windows_nightly["run"] for pattern in expected_windows_assets)
    assert windows_nightly["run"].index("gh release delete-asset nightly") < (
        windows_nightly["run"].index("gh release upload nightly")
    )
    assert windows_nightly["env"]["REQUESTED_SKIP_SIGNING"] == (
        "${{ inputs.skip_signing }}"
    )
    assert (
        'if [[ "$REQUESTED_SKIP_SIGNING" == "true" ]]; then\n'
        '  SIGNING_NOTE="Unsigned"\n'
        "else\n"
        '  SIGNING_NOTE="Signed"\n'
        "fi"
    ) in windows_nightly["run"]
    assert '"${SIGNING_NOTE} Windows-only nightly build (${VERSION})."' in (
        windows_nightly["run"]
    )
    full_nightly = nightly_steps["Create nightly release"]
    assert full_nightly["env"]["REQUESTED_SKIP_SIGNING"] == "${{ inputs.skip_signing }}"
    assert (
        'if [[ "$REQUESTED_SKIP_SIGNING" == "true" || "$GITHUB_EVENT_NAME" == "schedule" ]]; then\n'
        '  SIGNING_NOTE="unsigned"\n'
        "else\n"
        '  SIGNING_NOTE="signed"\n'
        "fi"
    ) in full_nightly["run"]
    assert "This is a **${SIGNING_NOTE}** nightly build" in full_nightly["run"]
    assert "N.E.K.O_*_win.zip" in full_nightly["run"]
    organize_release = nightly_steps["Organize release files"]
    assert '-name "*.zip"' in organize_release["run"]
    assert '-name "*.exe"' not in organize_release["run"]
    assert "N.E.K.O.exe" not in organize_release["run"]


def test_workflow_values_are_passed_to_shell_through_environment_variables() -> None:
    workflow = _load_workflow(CROSS_PLATFORM_WORKFLOW)
    version_steps = _steps_by_name(workflow, "version")
    calculate_version = version_steps["Calculate version"]

    assert calculate_version["env"]["INPUT_VERSION"] == "${{ inputs.version }}"
    assert "${{ inputs.version }}" not in calculate_version["run"]
    assert 'VERSION="$INPUT_VERSION"' in calculate_version["run"]

    build_steps = _steps_by_name(workflow, "build-electron")
    update_package_version = build_steps["Update version in package.json"]
    assert update_package_version["env"]["RELEASE_VERSION"] == (
        "${{ needs.version.outputs.version }}"
    )
    assert "${{ needs.version.outputs.version }}" not in update_package_version["run"]
    assert "process.env.RELEASE_VERSION" in update_package_version["run"]

    nightly_steps = _steps_by_name(workflow, "nightly")
    for step_name in (
        "Organize release files",
        "Create nightly release",
        "Create or update Windows nightly release",
    ):
        step = nightly_steps[step_name]
        assert step["env"]["RELEASE_VERSION"] == (
            "${{ needs.version.outputs.version }}"
        )
        assert "${{ needs.version.outputs.version }}" not in step["run"]
        assert 'VERSION="$RELEASE_VERSION"' in step["run"]


def test_debug_build_values_are_runtime_inputs_not_test_defaults() -> None:
    windows_workflow = WINDOWS_WORKFLOW.read_text(encoding="utf-8")
    cross_platform_workflow = CROSS_PLATFORM_WORKFLOW.read_text(encoding="utf-8")

    assert "allow_fork_build:" in windows_workflow
    assert "allow_fork_build:" in cross_platform_workflow
    assert "inputs.allow_fork_build" in cross_platform_workflow
    assert "'Project-N-E-K-O/N.E.K.O.-PC'" in cross_platform_workflow
    assert "default: 'Project-N-E-K-O/N.E.K.O.-PC'" in windows_workflow
    assert "default: 'main'" in windows_workflow
    assert "default: false" in windows_workflow


def test_windows_only_nightly_preserves_other_platform_assets() -> None:
    workflow = _load_workflow(CROSS_PLATFORM_WORKFLOW)
    nightly_steps = _steps_by_name(workflow, "nightly")

    assert nightly_steps["Delete old nightly release"]["if"] == (
        "${{ !inputs.windows_only }}"
    )
    assert nightly_steps["Create nightly release"]["if"] == (
        "${{ !inputs.windows_only }}"
    )
    windows_nightly = nightly_steps["Create or update Windows nightly release"]
    assert windows_nightly["if"] == "${{ inputs.windows_only }}"
    assert "gh release upload nightly release/* --clobber" in windows_nightly["run"]


def test_published_stable_release_validation_never_contacts_update_service() -> None:
    workflow = _load_workflow(SYNC_UPDATE_WORKFLOW)
    condition = workflow["jobs"]["validate"]["if"]

    assert "!github.event.release.draft" in condition
    assert "!github.event.release.prerelease" in condition
    assert "startsWith(github.event.release.tag_name, 'v')" in condition
    validate = _steps_by_name(workflow, "validate")["Validate stable release assets"]
    expected_signatures = (
        "N.E.K.O_${VERSION}_win_manifest.json.sig",
        "N.E.K.O_${VERSION}_mac_x64_manifest.json.sig",
        "N.E.K.O_${VERSION}_mac_arm64_manifest.json.sig",
        "N.E.K.O_${VERSION}_linux_x64_manifest.json.sig",
        "N.E.K.O_${VERSION}_linux_x64_appimage_manifest.json.sig",
    )
    assert all(f'"{asset}"' in validate["run"] for asset in expected_signatures)

    raw_workflow = SYNC_UPDATE_WORKFLOW.read_text(encoding="utf-8")
    assert "ALIYUN_OSS_" not in raw_workflow
    assert "ossutil" not in raw_workflow
    assert "NEKO_UPDATE_" not in raw_workflow
    assert "/v1/admin/" not in raw_workflow


def test_local_asset_publish_uses_staged_build_output_without_downloading_release_assets() -> None:
    script = LOCAL_ASSET_PUBLISH_SCRIPT.read_text(encoding="utf-8")

    assert "release-assets" in script
    assert "Get-ChildItem -LiteralPath $AssetsDirectory -Recurse -File" in script
    assert "gh release download" not in script
    assert "Compare-Object -ReferenceObject" in script
    assert "function Test-OssObjectExists" in script
    assert "function Get-Sha256" in script
    assert "Get-FileHash -LiteralPath $Path -Algorithm SHA256" in script
    assert "Portable manifest is missing its signature asset" in script
    assert "Portable signature has no matching manifest" in script
    assert "function Assert-PortableManifestSignature" in script
    assert "verifyPortableManifestSignature" in script
    assert "Manifest verifier not found at $ManifestVerifierPath" in script
    assert "pass -ManifestVerifierPath explicitly" in script
    assert "Unable to determine whether OSS object exists" in script
    assert "NoSuchKey" in script
    assert "PSObject.Properties['digest']" in script
    assert "GitHub Release asset content differs from staged asset" in script
    assert "Refusing to overwrite immutable OSS object" in script
    assert "--max-time', '1800'" in script
    assert "Invoke-UpdateMirrorSync" in script
    assert "-TimeoutSec 30" in script
    assert not re.search(
        r"\[ValidateSet\(\s*['\"]stable['\"]\s*,\s*['\"]nightly['\"]\s*\)\]",
        script,
    )
    assert "/stable/sync" in script
    assert "NEKO_UPDATE_ADMIN_TOKEN" in script

    staged_hashes = script.index("$assetHashes[$asset.Name] = Get-Sha256")
    verifier_exists = script.index(
        "Test-Path -LiteralPath $ManifestVerifierPath -PathType Leaf"
    )
    verifier_resolved = script.index(
        "$ManifestVerifierPath = (Resolve-Path -LiteralPath $ManifestVerifierPath).Path"
    )
    signature_check = script.index(
        "Assert-PortableManifestSignature -VerifierPath $ManifestVerifierPath"
    )
    release_fetch = script.index(
        '$releaseJson = ((& gh api "repos/$Repository/releases/tags/$Tag")'
    )
    github_digest = script.index('$expectedDigest = "sha256:$($assetHashes[$asset.Name])"')
    latest_release = script.index('$latestTag = ((& gh api "repos/$Repository/releases/latest"')
    object_check = script.index("if (Test-OssObjectExists -ObjectUrl $objectUrl)")
    upload = script.index("Invoke-Checked -FilePath ossutil -Arguments @('cp', $asset.FullName")
    cdn_download = script.index("Invoke-Checked -FilePath curl.exe -Arguments @(")
    cdn_hash = script.index("if ((Get-Sha256 -Path $downloadedAsset)")
    sync = script.rindex("Invoke-UpdateMirrorSync -Endpoint $endpoint")
    assert (
        verifier_exists
        < verifier_resolved
        < staged_hashes
        < signature_check
        < release_fetch
        < github_digest
        < latest_release
        < object_check
        < upload
        < cdn_download
        < cdn_hash
        < sync
    )


def test_manual_release_guide_matches_workflow_triggers_and_script_parameters() -> None:
    guide = MANUAL_DESKTOP_RELEASE_DOC.read_text(encoding="utf-8")
    publish_script = LOCAL_ASSET_PUBLISH_SCRIPT.read_text(encoding="utf-8")

    assert all(
        event in guide
        for event in ("`schedule`", "`workflow_dispatch`", "`workflow_call`")
    )
    assert "`refs/tags/v*` 仅在工作流已被调用时参与版本计算" in guide
    assert "发布 GitHub Release 后，它只校验必需的 Portable 资产" in guide
    assert "-ManifestVerifierPath" in guide
    assert "[string]$ManifestVerifierPath" in publish_script
    assert "向 `publish-desktop-release-assets.ps1` 传入 `-ManifestVerifierPath`" in guide


def test_portable_manifest_signing_is_required_for_nightly_and_local_stable_builds() -> None:
    workflow = _load_workflow(CROSS_PLATFORM_WORKFLOW)

    signing = _steps_by_name(workflow, "nightly")["Sign Portable manifests"]
    assert signing["env"]["PORTABLE_UPDATE_MANIFEST_ED25519_PRIVATE_KEY"] == (
        "${{ secrets.PORTABLE_UPDATE_MANIFEST_ED25519_PRIVATE_KEY }}"
    )
    assert signing["env"]["PORTABLE_MANIFEST_SIGNING_KEY_ID"] == (
        "portable-manifest-2026-07"
    )
    assert "is required to publish Portable updates" in signing["run"]
    assert "openssl pkeyutl -sign -rawin" in signing["run"]
    assert '"${manifest}.sig"' in signing["run"]
    assert "shopt -s nullglob" in signing["run"]

    local_script = LOCAL_RELEASE_SCRIPT.read_text(encoding="utf-8")
    assert "function Sign-PortableManifests" in local_script
    assert "openssl 'pkeyutl' '-sign' '-rawin'" in local_script
    assert "ManifestSigningKeyPath" in local_script


def test_local_release_build_clears_stale_electron_dist_output() -> None:
    local_script = LOCAL_RELEASE_SCRIPT.read_text(encoding="utf-8")

    assert "$distDirectory = Join-Path $ElectronPath 'dist'" in local_script
    assert "Remove-Item -LiteralPath $distDirectory -Recurse -Force" in local_script
    assert local_script.index("Portable output already exists") < local_script.index(
        "Remove-Item -LiteralPath $distDirectory -Recurse -Force"
    )


def test_local_release_build_falls_back_when_previous_manifest_is_absent() -> None:
    local_script = LOCAL_RELEASE_SCRIPT.read_text(encoding="utf-8")

    release_probe = local_script.index("$assetNames = @(& gh release view")
    missing_guard = local_script.index("if ($manifestNames.Count -eq 0)")
    full_only = local_script.index("building a full package only")
    manifest_download = local_script.index(
        "Invoke-Checked gh 'release' 'download' $PreviousReleaseTag"
    )
    assert release_probe < missing_guard < full_only < manifest_download
    assert "if ($appImageManifestNames.Count -eq 1)" in local_script


def test_local_release_build_pins_output_directory_before_changing_location() -> None:
    local_script = LOCAL_RELEASE_SCRIPT.read_text(encoding="utf-8")

    resolve_output = local_script.index(
        "$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath("
        "$OutputDirectory)"
    )
    staged_output = local_script.index("$versionOutputDirectory = Join-Path")
    change_location = local_script.index("Push-Location $ElectronPath")
    assert resolve_output < staged_output < change_location


def test_local_release_build_metadata_does_not_expose_absolute_backend_path() -> None:
    local_script = LOCAL_RELEASE_SCRIPT.read_text(encoding="utf-8")

    assert 'backend = "bin/$([System.IO.Path]::GetFileName($backendPath))"' in (
        local_script
    )
    assert "backend = $backendPath" not in local_script


def test_local_release_build_rejects_unsupported_architecture_and_handles_missing_linux_config() -> None:
    local_script = LOCAL_RELEASE_SCRIPT.read_text(encoding="utf-8")

    assert "$buildPlatform -ne 'macos' -and $Architecture -ne 'x64'" in local_script
    assert "$package.PSObject.Properties['build']" in local_script
    assert "$buildConfig.Value.PSObject.Properties['linux']" in local_script


def test_delta_baseline_selects_a_preceding_stable_release() -> None:
    workflow = _load_workflow(CROSS_PLATFORM_WORKFLOW)
    steps = _steps_by_name(workflow, "build-electron")
    download = steps["Download previous Portable manifests"]

    assert "releases?per_page=100" in download["run"]
    assert "select(.tag_name != env.GITHUB_REF_NAME)" in download["run"]
