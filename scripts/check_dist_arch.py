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

"""Assert that a built dist actually contains the CPU architecture it claims.

Run right after the Nuitka build, before the artifact is uploaded.

## Why this exists

Issue #2898: the nightly ``mac-x64`` backend was arm64. The ``mac-x64`` matrix
leg ran on the ``macos-15`` runner, which is Apple Silicon -- ``actions/setup-python``
with ``architecture: x64`` only makes *Python* run under Rosetta, while Nuitka
still emits a native arm64 binary and Playwright still downloads the host's
browser build. Nothing in the pipeline looked at the produced Mach-O, so a
completely unusable Intel build shipped for months.

The runner label is fixed separately; this gate is what keeps the class of bug
from coming back silently on any platform, including the Linux arm64 leg where
a mis-resolved electron-builder/Nuitka target would fail the same way.

## What it checks

- The main entry binary's CPU type and container format.
- Every packaged native binary. Three filename shapes, because a plain suffix
  allowlist misses two real ones: versioned ELF sonames such as
  ``libpython3.11.so.1.0`` (whose ``Path.suffix`` is ``.0``) and the
  extensionless helpers Nuitka's Playwright plugin packages
  (``playwright/driver/node``).
- That the container format matches the target platform. A Windows PE that
  survived inside a macOS bundle has a perfectly matching *architecture*, so
  only the format check catches it -- and this repo does keep all three
  platforms' Steam natives in the source tree, stripped by ``rm -f`` at build
  time.
- Inside ``playwright_browsers/``, both that no path component carries the
  *opposite* architecture token (#2898's bad build shipped
  ``chromium-1208/chrome-mac-arm64`` inside an x64 bundle) and that every
  bundled binary carries the expected one. The directory check alone is not
  enough: Playwright also uses tokenless names such as ``chrome-mac``, under
  which a wrong-arch Chromium would pass unnoticed.

Headers are parsed directly, so this runs identically on all three CI hosts and
needs neither ``lipo`` nor ``file``.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import NamedTuple

MACHO = "mach-o"
ELF = "elf"
PE = "pe"

# Mach-O
_MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe": "<",  # MH_MAGIC_64, little-endian host order
    b"\xfe\xed\xfa\xcf": ">",  # MH_CIGAM_64
    b"\xce\xfa\xed\xfe": "<",  # MH_MAGIC (32-bit)
    b"\xfe\xed\xfa\xce": ">",
}
# FAT_MAGIC/FAT_CIGAM carry 20-byte fat_arch records; FAT_MAGIC_64/FAT_CIGAM_64
# (trailing byte 0xbf, not 0xbe) carry 32-byte fat_arch_64 records. Both are
# valid universal binaries -- reading only the 32-bit form reports a 64-bit fat
# main binary as "not an executable" and skips a 64-bit fat dylib entirely.
_MACHO_FAT_MAGICS = {
    b"\xca\xfe\xba\xbe": (">", 20),
    b"\xbe\xba\xfe\xca": ("<", 20),
    b"\xca\xfe\xba\xbf": (">", 32),
    b"\xbf\xba\xfe\xca": ("<", 32),
}
_MACHO_CPU = {0x01000007: "x64", 0x0100000C: "arm64", 0x00000007: "x86", 0x0000000C: "arm"}

# ELF
_ELF_MACHINE = {62: "x64", 183: "arm64", 3: "x86", 40: "arm"}

# PE
_PE_MACHINE = {0x8664: "x64", 0xAA64: "arm64", 0x014C: "x86", 0x01C0: "arm"}

_FORMAT_BY_PLATFORM = {"mac": MACHO, "linux": ELF, "win": PE}

_NATIVE_SUFFIXES = (".so", ".dylib", ".pyd", ".dll", ".exe")
_ARCH_TOKENS = ("x64", "arm64")
# 供应商目录里出现的架构写法不止一种（chrome-mac-x64 / chrome-linux-arm64 / mac-arm64 ...），
# 用「对立架构的词元」做黑名单，比要求某个确切目录名更耐得住 Playwright 改布局。
_OPPOSITE_TOKENS = {
    "x64": ("-arm64", "_arm64", "aarch64"),
    "arm64": ("-x64", "_x64", "-x86_64", "_x86_64", "amd64"),
}
_VENDORED_BROWSER_DIR = "playwright_browsers"
# 只是别让畸形目录把这一步拖死；正常 Chromium 包也就几千个文件。
_BROWSER_SCAN_FILE_LIMIT = 50000
# PE 的 e_lfanew 指向可执行头。真实文件里它是几百字节；给个宽松上界，免得
# 一个伪造值让我们去读整个文件。
_MAX_PE_HEADER_OFFSET = 4 * 1024 * 1024


class BinaryInfo(NamedTuple):
    """The container format and the CPU architectures a binary declares."""

    format: str
    arches: list[str]


def _read_head(path: Path, size: int = 64) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def read_binary(path: Path) -> BinaryInfo | None:
    """Inspect a file's header, or return None when it is not an executable.

    A Mach-O fat binary reports every slice it carries; every other format
    reports a single entry.
    """
    try:
        head = _read_head(path)
    except OSError:
        return None
    if len(head) < 20:
        return None

    magic = head[:4]
    if magic in _MACHO_FAT_MAGICS:
        endian, record_size = _MACHO_FAT_MAGICS[magic]
        (count,) = struct.unpack(endian + "I", head[4:8])
        # 限个上界，别让畸形头把内存吃穿。
        if count > 64:
            return None
        blob = _read_head(path, 8 + record_size * count)
        arches: list[str] = []
        for index in range(count):
            offset = 8 + record_size * index
            if offset + 4 > len(blob):
                break
            (cpu,) = struct.unpack(endian + "I", blob[offset : offset + 4])
            arches.append(_MACHO_CPU.get(cpu, f"unknown(0x{cpu:08x})"))
        return BinaryInfo(MACHO, arches) if arches else None

    if magic in _MACHO_MAGICS:
        endian = _MACHO_MAGICS[magic]
        (cpu,) = struct.unpack(endian + "I", head[4:8])
        return BinaryInfo(MACHO, [_MACHO_CPU.get(cpu, f"unknown(0x{cpu:08x})")])

    if magic == b"\x7fELF":
        endian = "<" if head[5] == 1 else ">"
        (machine,) = struct.unpack(endian + "H", head[18:20])
        return BinaryInfo(ELF, [_ELF_MACHINE.get(machine, f"unknown({machine})")])

    if magic[:2] == b"MZ":
        # 浏览器树里每个普通文件都会流经这里，其中不乏「恰好以 MZ 开头」的数据文件。
        # 头不足 0x40 字节时切片是空的，struct.unpack 会抛 struct.error 把闸门整个
        # 打崩；伪造的巨大 pe_offset 还会让 _read_head 去要一个无界的读。两处都挡住。
        if len(head) < 0x40:
            return None
        (pe_offset,) = struct.unpack("<I", head[0x3C:0x40])
        if pe_offset > _MAX_PE_HEADER_OFFSET:
            return None
        blob = _read_head(path, pe_offset + 8)
        if len(blob) < pe_offset + 8 or blob[pe_offset : pe_offset + 4] != b"PE\x00\x00":
            return None
        (machine,) = struct.unpack("<H", blob[pe_offset + 4 : pe_offset + 6])
        return BinaryInfo(PE, [_PE_MACHINE.get(machine, f"unknown(0x{machine:04x})")])

    return None


def read_arches(path: Path) -> list[str] | None:
    """Convenience wrapper returning just the architectures."""
    info = read_binary(path)
    return None if info is None else info.arches


def _entry_binary(dist_root: Path, platform: str) -> Path | None:
    """Locate the main server binary for a platform, or None when absent.

    macOS wraps the Nuitka output in ``projectneko_server.app``; the runtime dir
    handed to this script may be either the bundle root or its ``MacOS`` dir.
    """
    candidates = [
        dist_root / "projectneko_server.exe",
        dist_root / "projectneko_server",
        dist_root / "projectneko_server.app" / "Contents" / "MacOS" / "projectneko_server",
    ]
    if platform == "win":
        candidates = [dist_root / "projectneko_server.exe"]
    for candidate in candidates:
        # macOS 的 dist 根上还有个同名 shell wrapper（exec 进 .app），它不是 Mach-O，
        # read_binary 返回 None —— 跳过继续找真的那个，别把 wrapper 当主程序放行。
        if candidate.is_file() and read_binary(candidate) is not None:
            return candidate
    return None


def _looks_native(path: Path) -> bool:
    """Is this filename shaped like something that could be a native binary?"""
    return path.suffix.lower() in _NATIVE_SUFFIXES or ".so." in path.name.lower() or not path.suffix


def _iter_native_binaries(dist_root: Path):
    for path in dist_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        # 用相对路径判断，别把 dist_root 的祖先目录名算进来。
        if _VENDORED_BROWSER_DIR in path.relative_to(dist_root).parts:
            continue
        if _looks_native(path):
            yield path


def check_dist_arch(dist_root: Path, expect_arch: str, platform: str) -> list[str]:
    """Return a list of human-readable problems; empty means the dist is clean."""
    issues: list[str] = []
    expect_format = _FORMAT_BY_PLATFORM[platform]

    entry = _entry_binary(dist_root, platform)
    if entry is None:
        issues.append(
            "main server binary not found (or not a recognisable executable) under "
            f"{dist_root}"
        )
    else:
        info = read_binary(entry)
        assert info is not None  # _entry_binary only returns readable binaries
        if expect_arch not in info.arches:
            issues.append(
                f"main binary is {'/'.join(info.arches) or 'unreadable'}, expected "
                f"{expect_arch}: {entry.relative_to(dist_root)}"
            )
        if info.format != expect_format:
            issues.append(
                f"main binary is a {info.format} file but {platform} builds must be "
                f"{expect_format}: {entry.relative_to(dist_root)}"
            )

    mismatched: list[str] = []
    foreign_format: list[str] = []
    for lib in _iter_native_binaries(dist_root):
        info = read_binary(lib)
        if info is None:
            # 名字像原生库但根本不是可执行格式（占位文件、文本 stub）——不是架构问题。
            continue
        relative = lib.relative_to(dist_root)
        if info.format != expect_format:
            # 跨平台库混进来时架构反而是「对」的（Windows DLL 也是 PE x64），
            # 只有格式这一维能抓住它，所以单独归一类报。
            foreign_format.append(f"{relative} is {info.format}")
        elif expect_arch not in info.arches:
            mismatched.append(f"{relative} is {'/'.join(info.arches)}")
    if mismatched:
        shown = ", ".join(sorted(mismatched)[:10])
        issues.append(
            f"{len(mismatched)} native binary/binaries are not {expect_arch}: {shown}"
            + (" ..." if len(mismatched) > 10 else "")
        )
    if foreign_format:
        shown = ", ".join(sorted(foreign_format)[:10])
        issues.append(
            f"{len(foreign_format)} native binary/binaries are not {expect_format} "
            f"(wrong platform): {shown}" + (" ..." if len(foreign_format) > 10 else "")
        )

    browser_root = dist_root / _VENDORED_BROWSER_DIR
    if browser_root.is_dir():
        tokens = _OPPOSITE_TOKENS.get(expect_arch, ())
        bad_paths: list[str] = []
        wrong_binaries: list[str] = []
        wrong_format: list[str] = []
        # 目录名和字节两道检查走同一次遍历。分两次走的话上限只约束得住第二次，
        # 第一次照样把整棵树走完，那个上限就是摆设。也别先 sorted() 把树物化，
        # 只对最后要打印的结果排序。
        scanned = 0
        truncated = False
        for path in browser_root.rglob("*"):
            if scanned >= _BROWSER_SCAN_FILE_LIMIT:
                truncated = True
                break
            scanned += 1
            relative = path.relative_to(dist_root)
            if path.is_dir():
                if any(
                    token in part.lower()
                    for part in path.relative_to(browser_root).parts
                    for token in tokens
                ):
                    bad_paths.append(str(relative))
                continue
            if not path.is_file() or path.is_symlink():
                continue
            info = read_binary(path)
            if info is None:
                continue
            if info.format != expect_format:
                # 和 dist 主体同一条判据：装错平台的浏览器架构反而是「对」的
                # （chrome-mac 底下的 PE x64 满足 x64），只有格式这维抓得住。
                wrong_format.append(f"{relative} is {info.format}")
            elif expect_arch not in info.arches:
                # 和 dist 主体同一条判据：不含期望架构就是错的，不只是「恰好是对立
                # 架构」才算。原来这里放宽成只禁对立架构，理由是「Chromium 带一两个
                # 32 位小工具属正常」—— 那是推测，拿不出证据，而且让 x86 的浏览器
                # 二进制在 x64 包里畅通无阻。
                wrong_binaries.append(f"{relative} is {'/'.join(info.arches)}")
        if bad_paths:
            issues.append(
                f"vendored browser tree carries non-{expect_arch} directories: "
                + ", ".join(sorted(bad_paths)[:5])
            )
        if wrong_binaries:
            shown = ", ".join(sorted(wrong_binaries)[:5])
            issues.append(
                f"vendored browser tree contains {len(wrong_binaries)} non-{expect_arch} "
                f"binary/binaries: {shown}"
                + (" ..." if len(wrong_binaries) > 5 else "")
            )
        if wrong_format:
            shown = ", ".join(sorted(wrong_format)[:5])
            issues.append(
                f"vendored browser tree contains {len(wrong_format)} non-{expect_format} "
                f"binary/binaries (wrong platform): {shown}"
                + (" ..." if len(wrong_format) > 5 else "")
            )
        if truncated:
            # 截断了就等于「后面那截没验过」。这里必须 fail-closed：否则一个越过
            # 上限的错架构二进制会让整个闸门静默放行，比不设上限更糟。
            issues.append(
                f"vendored browser tree exceeds the {_BROWSER_SCAN_FILE_LIMIT}-file scan "
                "cap, so it could not be fully verified"
            )

    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "dist_root",
        nargs="?",
        default="dist/Xiao8",
        help="Path to the built dist root (default: dist/Xiao8)",
    )
    parser.add_argument(
        "--expect-arch",
        required=True,
        choices=_ARCH_TOKENS,
        help="CPU architecture this build is supposed to be",
    )
    parser.add_argument(
        "--expect-platform",
        required=True,
        choices=tuple(_FORMAT_BY_PLATFORM),
        help="Host platform this build targets",
    )
    args = parser.parse_args(argv)

    dist_root = Path(args.dist_root).resolve()
    if not dist_root.is_dir():
        print(f"[FAIL] dist root does not exist or is not a directory: {dist_root}", file=sys.stderr)
        return 1

    issues = check_dist_arch(dist_root, args.expect_arch, args.expect_platform)
    if issues:
        print(
            f"[FAIL] {dist_root} does not match {args.expect_platform}/{args.expect_arch}:",
            file=sys.stderr,
        )
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        print(
            "\nHint: this almost always means the matrix leg ran on a runner whose "
            "native architecture differs from the one it claims to build (see #2898, "
            "where mac-x64 ran on the Apple Silicon `macos-15` image). Check the "
            "`runs-on` label for this leg before touching anything else.",
            file=sys.stderr,
        )
        return 1

    print(f"[OK] dist matches {args.expect_platform}/{args.expect_arch}: {dist_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
