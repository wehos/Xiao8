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

"""Behaviour tests for the dist architecture gate that guards issue #2898."""
from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "check_dist_arch", ROOT / "scripts" / "check_dist_arch.py"
)
assert _SPEC is not None and _SPEC.loader is not None
check_dist_arch = importlib.util.module_from_spec(_SPEC)
sys.modules["check_dist_arch"] = check_dist_arch
_SPEC.loader.exec_module(check_dist_arch)


_MACHO_CPU_BY_ARCH = {"x64": 0x01000007, "arm64": 0x0100000C, "x86": 0x00000007}
_ELF_MACHINE_BY_ARCH = {"x64": 62, "arm64": 183}
_PE_MACHINE_BY_ARCH = {"x64": 0x8664, "arm64": 0xAA64}


def macho(arch: str) -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", _MACHO_CPU_BY_ARCH[arch]) + b"\x00" * 56


def macho_fat(*arches: str) -> bytes:
    blob = b"\xca\xfe\xba\xbe" + struct.pack(">I", len(arches))
    for arch in arches:
        blob += struct.pack(">I", _MACHO_CPU_BY_ARCH[arch]) + b"\x00" * 16
    return blob + b"\x00" * 64


def elf(arch: str) -> bytes:
    head = bytearray(b"\x7fELF" + b"\x00" * 60)
    head[4] = 2  # ELFCLASS64
    head[5] = 1  # ELFDATA2LSB
    head[18:20] = struct.pack("<H", _ELF_MACHINE_BY_ARCH[arch])
    return bytes(head)


def pe(arch: str) -> bytes:
    pe_offset = 0x80
    head = bytearray(b"\x00" * (pe_offset + 8))
    head[0:2] = b"MZ"
    head[0x3C:0x40] = struct.pack("<I", pe_offset)
    head[pe_offset : pe_offset + 4] = b"PE\x00\x00"
    head[pe_offset + 4 : pe_offset + 6] = struct.pack("<H", _PE_MACHINE_BY_ARCH[arch])
    return bytes(head)


def write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.fixture()
def mac_bundle(tmp_path: Path):
    """A macOS-shaped dist: shell wrapper at the root, real Mach-O inside the .app."""

    def build(entry_arch: str, lib_arch: str | None = None) -> Path:
        root = tmp_path / "Xiao8"
        write(
            root / "projectneko_server",
            b'#!/bin/bash\nexec "$(dirname "$0")/projectneko_server.app/Contents/MacOS/projectneko_server" "$@"\n',
        )
        write(
            root / "projectneko_server.app" / "Contents" / "MacOS" / "projectneko_server",
            macho(entry_arch),
        )
        write(
            root / "projectneko_server.app" / "Contents" / "MacOS" / "unicodedata.so",
            macho(lib_arch or entry_arch),
        )
        return root

    return build


def test_clean_x64_mac_dist_passes(mac_bundle) -> None:
    root = mac_bundle("x64")
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") == []


def test_issue_2898_shape_is_rejected(mac_bundle) -> None:
    """The exact #2898 build: mac-x64 artifact whose Mach-O is arm64."""
    root = mac_bundle("arm64")
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert issues, "an arm64 binary in an x64 bundle must not pass"
    assert any("main binary is arm64" in issue for issue in issues)


def test_shell_wrapper_at_root_does_not_shadow_the_real_binary(mac_bundle) -> None:
    """The root `projectneko_server` is a text wrapper, not the Mach-O to inspect.

    Without the "skip candidates that are not binaries" guard, the wrapper is
    found first, `read_arches` returns None, and the arm64 payload sails through.
    """
    root = mac_bundle("arm64")
    entry = check_dist_arch._entry_binary(root, "mac")
    assert entry is not None
    assert entry.name == "projectneko_server"
    assert entry.parent.name == "MacOS"
    assert check_dist_arch.read_arches(entry) == ["arm64"]


def test_mismatched_native_library_is_reported(mac_bundle) -> None:
    root = mac_bundle("x64", lib_arch="arm64")
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert any("native binary" in issue and "unicodedata.so" in issue for issue in issues)


def test_universal_binary_containing_the_expected_arch_passes(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho_fat("x64", "arm64"))
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") == []
    assert check_dist_arch.check_dist_arch(root, "arm64", "mac") == []


def test_vendored_browser_directory_with_opposite_arch_token_is_rejected(tmp_path: Path) -> None:
    """#2898 also shipped chromium-1208/chrome-mac-arm64 inside the x64 bundle."""
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    (root / "playwright_browsers" / "chromium-1208" / "chrome-mac-arm64").mkdir(parents=True)
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert any("vendored browser tree" in issue for issue in issues)


def test_vendored_browser_directory_matching_the_build_passes(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    (root / "playwright_browsers" / "chromium-1208" / "chrome-mac-x64").mkdir(parents=True)
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") == []


def test_tokenless_browser_directory_with_wrong_arch_binary_is_rejected(tmp_path: Path) -> None:
    """Playwright also uses tokenless names like `chrome-mac`.

    Directory-name matching alone would let a wrong-arch Chromium through, so
    the browser tree is header-scanned as well.
    """
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    browser = root / "playwright_browsers" / "chromium-1208" / "chrome-mac"
    write(browser / "libEGL.dylib", macho("arm64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert any("vendored browser tree contains" in issue for issue in issues)


def test_suffixless_browser_executable_is_scanned(tmp_path: Path) -> None:
    """Chromium's own executable has no filename suffix; dispatch on magic."""
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("arm64"))
    browser = (
        root / "playwright_browsers" / "chromium-1208" / "chrome-mac"
        / "Chromium.app" / "Contents" / "MacOS"
    )
    write(browser / "Chromium", macho("x64"))
    issues = check_dist_arch.check_dist_arch(root, "arm64", "mac")
    assert any("vendored browser tree contains" in issue for issue in issues)


def test_matching_browser_tree_passes(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    browser = root / "playwright_browsers" / "chromium-1208" / "chrome-mac"
    write(browser / "libEGL.dylib", macho("x64"))
    write(browser / "Chromium.app" / "Contents" / "MacOS" / "Chromium", macho("x64"))
    write(browser / "icudtl.dat", b"binary blob, not an executable\n")
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") == []


@pytest.mark.parametrize(
    ("payload", "platform", "suffix"),
    [(elf, "linux", ".so"), (pe, "win", ".dll")],
)
def test_elf_and_pe_dists_are_checked_too(tmp_path: Path, payload, platform, suffix) -> None:
    root = tmp_path / "Xiao8"
    entry = "projectneko_server.exe" if platform == "win" else "projectneko_server"
    write(root / entry, payload("x64"))
    write(root / f"native{suffix}", payload("arm64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", platform)
    assert any("native binary" in issue for issue in issues)

    write(root / f"native{suffix}", payload("x64"))
    assert check_dist_arch.check_dist_arch(root, "x64", platform) == []


def test_linux_arm64_dist_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", elf("arm64"))
    write(root / "native.so", elf("arm64"))
    assert check_dist_arch.check_dist_arch(root, "arm64", "linux") == []
    assert check_dist_arch.check_dist_arch(root, "x64", "linux") != []


def test_missing_entry_binary_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    root.mkdir(parents=True)
    issues = check_dist_arch.check_dist_arch(root, "x64", "linux")
    assert any("main server binary not found" in issue for issue in issues)


def test_non_binary_files_with_native_suffixes_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", elf("x64"))
    write(root / "placeholder.so", b"not a binary at all\n")
    assert check_dist_arch.check_dist_arch(root, "x64", "linux") == []


def test_cli_exit_codes(tmp_path: Path, capsys) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", elf("arm64"))
    assert (
        check_dist_arch.main([str(root), "--expect-arch", "arm64", "--expect-platform", "linux"])
        == 0
    )
    assert (
        check_dist_arch.main([str(root), "--expect-arch", "x64", "--expect-platform", "linux"]) == 1
    )
    assert "#2898" in capsys.readouterr().err


def macho_fat64(*arches: str) -> bytes:
    """FAT_MAGIC_64 universal binary: 32-byte fat_arch_64 records."""
    blob = b"\xca\xfe\xba\xbf" + struct.pack(">I", len(arches))
    for arch in arches:
        blob += struct.pack(">I", _MACHO_CPU_BY_ARCH[arch]) + b"\x00" * 28
    return blob + b"\x00" * 64


def test_fat64_universal_binary_is_understood(tmp_path: Path) -> None:
    """FAT_MAGIC_64 (cafebabf) is as valid a universal binary as the 32-bit header."""
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho_fat64("x64", "arm64"))
    info = check_dist_arch.read_binary(root / "projectneko_server")
    assert info is not None and info.format == check_dist_arch.MACHO
    assert set(info.arches) == {"x64", "arm64"}
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") == []


def test_fat64_binary_with_only_the_wrong_arch_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho_fat64("arm64"))
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") != []


def test_versioned_elf_soname_is_scanned(tmp_path: Path) -> None:
    """`libpython3.11.so.1.0` has Path.suffix `.0`, so a suffix allowlist misses it."""
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", elf("x64"))
    write(root / "libpython3.11.so.1.0", elf("arm64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "linux")
    assert any("libpython3.11.so.1.0" in issue for issue in issues)


def test_extensionless_helper_executable_is_scanned(tmp_path: Path) -> None:
    """Nuitka's Playwright plugin packages a bare `playwright/driver/node`."""
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", elf("x64"))
    write(root / "playwright" / "driver" / "node", elf("arm64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "linux")
    assert any("node" in issue for issue in issues)


def test_windows_helper_exe_is_scanned(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server.exe", pe("x64"))
    write(root / "playwright" / "driver" / "node.exe", pe("arm64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "win")
    assert any("node.exe" in issue for issue in issues)


def test_foreign_container_format_is_rejected_even_when_the_arch_matches(tmp_path: Path) -> None:
    """A leftover Windows DLL in a mac bundle is PE x64: the arch check alone passes it."""
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    write(root / "steam_api64.dll", pe("x64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert any("wrong platform" in issue and "steam_api64.dll" in issue for issue in issues)


def test_entry_binary_of_the_wrong_container_format_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server.exe", elf("x64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "win")
    assert any("must be pe" in issue for issue in issues)


def test_workspace_path_containing_an_arch_token_does_not_trip_the_gate(tmp_path: Path) -> None:
    """Only paths below the dist root may be matched against arch tokens.

    A CI workspace directory named e.g. `runner-arm64-workspace` must not make a
    correct x64 bundle fail.
    """
    root = tmp_path / "runner-arm64-workspace" / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    (root / "playwright_browsers" / "chromium-1208" / "chrome-mac-x64").mkdir(parents=True)
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") == []


def test_vendored_browser_exclusion_is_relative_to_the_dist_root(tmp_path: Path) -> None:
    """An ancestor named playwright_browsers must not hide the whole dist from the scan."""
    root = tmp_path / "playwright_browsers" / "Xiao8"
    write(root / "projectneko_server", elf("x64"))
    write(root / "native.so", elf("arm64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "linux")
    assert any("native.so" in issue for issue in issues)


def test_short_mz_file_does_not_crash_the_gate(tmp_path: Path) -> None:
    """A 20..63 byte data file starting with `MZ` used to raise struct.error.

    The browser scan feeds every ordinary file to the header reader, so a data
    blob that merely begins with those two bytes must be shrugged off, not
    allowed to take the whole build down.
    """
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    write(root / "playwright_browsers" / "chrome-mac" / "blob.dat", b"MZ" + b"\x00" * 30)
    assert check_dist_arch.read_binary(root / "playwright_browsers" / "chrome-mac" / "blob.dat") is None
    assert check_dist_arch.check_dist_arch(root, "x64", "mac") == []


def test_forged_pe_offset_is_not_followed(tmp_path: Path, monkeypatch) -> None:
    """A bogus e_lfanew must not turn into an unbounded read.

    Asserting only that the file is rejected proves nothing: a 4 GiB read
    request against a 64-byte file also returns 64 bytes and is rejected the
    same way. The invariant worth holding is the *size we ask for*.
    """
    root = tmp_path / "Xiao8"
    blob = bytearray(b"\x00" * 64)
    blob[0:2] = b"MZ"
    blob[0x3C:0x40] = struct.pack("<I", 0xFFFFFFF0)
    path = write(root / "forged.dll", bytes(blob))

    requested: list[int] = []
    real_read_head = check_dist_arch._read_head

    def recording_read_head(target: Path, size: int = 64) -> bytes:
        requested.append(size)
        return real_read_head(target, size)

    monkeypatch.setattr(check_dist_arch, "_read_head", recording_read_head)
    assert check_dist_arch.read_binary(path) is None
    assert max(requested) <= check_dist_arch._MAX_PE_HEADER_OFFSET + 8, (
        f"read a forged PE offset unbounded: asked for {max(requested)} bytes"
    )


def test_browser_scan_honours_its_file_cap(tmp_path: Path, monkeypatch) -> None:
    """The cap must bound the walk itself, not just the reporting."""
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    browser = root / "playwright_browsers" / "chrome-mac"
    for index in range(12):
        write(browser / f"lib{index}.dylib", macho("arm64"))

    seen: list[Path] = []
    real_read_binary = check_dist_arch.read_binary

    def counting_read_binary(path: Path):
        seen.append(path)
        return real_read_binary(path)

    monkeypatch.setattr(check_dist_arch, "read_binary", counting_read_binary)
    monkeypatch.setattr(check_dist_arch, "_BROWSER_SCAN_FILE_LIMIT", 3)
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    browser_reads = [p for p in seen if "playwright_browsers" in p.parts]
    assert len(browser_reads) <= 3
    # 截断即「没验完」，必须 fail-closed：否则越过上限的错架构二进制会静默放行。
    assert any("could not be fully verified" in issue for issue in issues)


def test_browser_tree_with_wrong_container_format_is_rejected(tmp_path: Path) -> None:
    """A Windows browser build inside a macOS bundle is PE x64: the arch test passes it.

    `_iter_native_binaries()` skips `playwright_browsers`, so nothing else would
    look at this file either.
    """
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    write(root / "playwright_browsers" / "chromium-1208" / "chrome-mac" / "chrome.dll", pe("x64"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert any("wrong platform" in issue and "chrome.dll" in issue for issue in issues)


def test_directory_token_walk_is_capped_too(tmp_path: Path, monkeypatch) -> None:
    """The cap must bound the directory-name pass as well as the byte pass.

    Two separate walks would leave this one unbounded, which makes the cap --
    and the fail-closed message that goes with it -- theatre.
    """
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    browsers = root / "playwright_browsers"
    for index in range(40):
        (browsers / f"chrome-mac-arm64-{index}").mkdir(parents=True)

    monkeypatch.setattr(check_dist_arch, "_BROWSER_SCAN_FILE_LIMIT", 3)
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert any("could not be fully verified" in issue for issue in issues)
    reported = [issue for issue in issues if "non-x64 directories" in issue]
    # 上限是 3，40 个坏目录不可能全被看到；真全看到了就说明这道遍历没受限。
    assert not reported or reported[0].count("chrome-mac-arm64-") <= 3


def test_browser_binary_of_a_third_architecture_is_rejected(tmp_path: Path) -> None:
    """Neither the expected arch nor its opposite: an x86 helper in an x64 bundle.

    The browser tree used to run a denylist ("only the opposite arch fails"),
    which let this through. It now uses the same strict rule as the rest of the
    dist: not carrying the expected architecture is enough to fail.
    """
    root = tmp_path / "Xiao8"
    write(root / "projectneko_server", macho("x64"))
    browser = root / "playwright_browsers" / "chromium-1208" / "chrome-mac"
    write(browser / "helper", macho("x86"))
    issues = check_dist_arch.check_dist_arch(root, "x64", "mac")
    assert any("non-x64" in issue and "helper" in issue for issue in issues)
