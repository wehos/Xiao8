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

"""Derive a plugin's entry metadata at packaging time.

``@plugin_entry`` does not declare ``input_schema``, it derives it — from a
pydantic model, or by inferring one from the handler's own type annotations.
Deriving means importing, and importing means executing the plugin.

That derivation happens here, once, on the author's machine, and the result is
written into the package as ``plugin.meta.json``. The user's machine reads that
file and never imports a plugin it has not been asked to run.

The subprocess isolation is reused as-is from the runtime scanner: the author
is running their own code, but a plugin that hangs or crashes on import should
produce a message rather than wedge the CLI.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

from plugin._types.version import SDK_VERSION
from plugin.server.infrastructure.packaged_metadata import (
    build_environment,
    entries_config_digest,
    PACKAGED_METADATA_FILENAME,
    PACKAGED_METADATA_SCHEMA_VERSION,
    compute_source_sha256,
    empty_source_directories,
    source_directory_names,
    unicode_renamed_source_files,
    source_stat_summary,
)


class MetadataProbeError(RuntimeError):
    """The plugin's metadata could not be derived at build time."""


def _load_logger() -> Any:
    from plugin.logging_config import get_logger

    return get_logger("neko_plugin_cli.metadata_probe")


def derive_plugin_metadata(plugin_dir: Path) -> dict[str, object]:
    """Import ``plugin_dir``'s entry class and return its packaged metadata.

    One tree, imported and fingerprinted. Deriving from one tree and
    fingerprinting another is precisely the shape that lets a package advertise
    handlers derived from a file it does not carry, with a hash that verifies
    (codex), so there is no way to ask for it.

    Raises :class:`MetadataProbeError` with the underlying reason when the
    plugin cannot be imported. Callers decide what that means;
    :func:`write_packaged_metadata` turns it into a warning so packaging keeps
    working, while a caller that needs the metadata can treat it as fatal.
    """
    # 延迟导入：CLI 的其它命令不该为这条路付框架导入的钱。
    from plugin.core.registry import _parse_single_plugin_config
    from plugin.server.application.plugins.metadata_scanner import (
        PluginMetadataScanError,
        scan_plugin_metadata_isolated,
    )

    plugin_dir = Path(plugin_dir).expanduser().resolve()
    config_path = plugin_dir / "plugin.toml"
    if not config_path.is_file():
        raise MetadataProbeError(f"missing plugin.toml in {plugin_dir}")

    logger = _load_logger()
    try:
        # 不带用户覆盖：这份元数据要发给别人，不能掺进作者机器上的 profile
        # 和运行时配置（codex）。
        ctx = _parse_single_plugin_config(
            config_path, set(), logger, apply_user_overlays=False
        )
    except Exception as exc:
        raise MetadataProbeError(
            f"plugin.toml could not be parsed: {type(exc).__name__}: {exc}"
        ) from exc
    if ctx is None:
        raise MetadataProbeError("plugin.toml could not be parsed or validated")

    entry = str(ctx.entry or "")
    if ":" not in entry:
        raise MetadataProbeError(
            f"entry point must be 'module:Class', got {entry!r}"
        )
    module_path, class_name = entry.split(":", 1)

    # 探测之前先记下这棵树的样子。插件的模块级代码完全可能在 import 时写文件
    # （初始化状态文件、生成缓存），而 handler 是在那之前推出来的、指纹是在那之后
    # 算的：装出来的树能通过校验，import 它却会注册出和 plugin.meta.json 不一样的
    # 入口（codex）。
    digest_before_probe = compute_source_sha256(plugin_dir)
    dirs_before_probe = source_directory_names(plugin_dir)

    try:
        isolated = scan_plugin_metadata_isolated(
            plugin_id=ctx.pid,
            module_path=module_path,
            class_name=class_name,
            config_path=config_path,
            conf=ctx.conf,
            pdata=ctx.pdata,
            python_requirement_paths=ctx.python_requirement_paths,
        )
    except PluginMetadataScanError as exc:
        raise MetadataProbeError(
            f"importing the plugin failed ({exc.error_type}): {exc}"
        ) from exc

    empty_dirs = empty_source_directories(plugin_dir)
    if empty_dirs:
        # ZIP 不存目录条目，所以空目录装不到用户机器上；而指纹只覆盖文件，
        # 装出来的那棵树照样和 source_files/source_bytes/source_sha256 全对得上。
        # 一个按目录存在与否条件注册入口的插件，因此会拿着一份按「有这个目录」
        # 推出来的 handler 在没有这个目录的机器上跑（codex）。
        raise MetadataProbeError(
            "staged tree contains directories that cannot survive packaging "
            f"({empty_dirs[:3]}); packaging without metadata so the host rescans"
        )
    if (
        compute_source_sha256(plugin_dir) != digest_before_probe
        or source_directory_names(plugin_dir) != dirs_before_probe
    ):
        # 目录也要比。内容摘要只覆盖文件，所以"建一个空标记目录、按它注册入口、
        # 再把它删掉"这一串前后摘要完全相同（codex）。
        raise MetadataProbeError(
            "importing the plugin changed the staged tree; packaging without "
            "metadata so the host rescans"
        )
    renamed = unicode_renamed_source_files(plugin_dir)
    if renamed:
        # 指纹按 NFC 记名，打包器写进档案的也是 NFC——但探测这一步 import 的是
        # 文件系统上那个分解形式的名字。装到保留原拼写的文件系统上，插件里写死
        # 分解形式字面量的代码会打不开文件，注册出来的东西和这里探到的不是一回事，
        # 而两棵树的指纹又恰好一样，宿主会照单全收（codex）。宁可不带元数据。
        raise MetadataProbeError(
            "staged file names change under NFC normalization "
            f"({renamed[:3]}); packaging without metadata so the host rescans"
        )
    stat_summary = source_stat_summary(plugin_dir)
    return {
        "schema_version": PACKAGED_METADATA_SCHEMA_VERSION,
        "sdk_version": SDK_VERSION,
        # 摘要算在真正打进包里的那棵树上，不是作者的源目录。构建规则
        # （tool.neko.build 的 exclude/exclude_dirs/exclude_files）可以把 .py /
        # .toml / .json 排除在包外，而用户机器上哈希的是装出来的那份——两边算的
        # 树不一样，一旦走到内容校验就会条条判成"源码变了"，把好好的 schema 换成
        # 占位（greptile）。扫描现在也在同一棵树上，见函数开头。
        "source_sha256": compute_source_sha256(plugin_dir),
        # 文件清单让"少了一个文件"这件事不依赖时间戳，也不依赖解包顺序。
        "source_files": stat_summary.names,
        "source_bytes": stat_summary.total_bytes,
        "build_env": build_environment(),
        # 打包时那份 manifest 声明的 entries 表；宿主拿它判断用户的覆盖有没有
        # 动过入口表，而不是判断「存不存在这张表」。
        "entries_config_sha256": entries_config_digest(ctx.conf, ctx.pdata),
        "entries": list(isolated.entries_preview),
        "handlers": dict(isolated.handlers),
        "entry_methods": dict(isolated.entry_methods),
    }


def write_packaged_metadata(
    *,
    source_dir: Path,
    target_dir: Path,
) -> Path | None:
    """Derive metadata from the staged tree and write it into ``target_dir``.

    Returns the written path, or ``None`` when the plugin could not be imported
    here. ``source_dir`` is only named in the warnings; the probe itself runs on
    ``target_dir``, the copy that actually goes into the package. Importing the
    author's tree instead lets a build rule exclude a file that decides which
    entries get registered — the package would then carry handlers derived from
    a file it does not contain, and because the fingerprint is taken on the
    staged tree the host's verification passes and it advertises entries the
    installed plugin never registers (codex). Dependencies resolve from the
    staged ``vendor/`` directory, which is copied with everything else before
    this runs.

    A failure warns rather than failing the build. Packaging is not the place to
    insist that a plugin imports: the build machine may be missing an optional
    dependency, the plugin may target another OS, and refusing to produce the
    package at all would turn a metadata optimisation into a packaging gate. The
    host has defined behaviour for a package without metadata — it falls back to
    what the manifest declares — so shipping without it costs a degraded
    parameter form, not a broken plugin.

    The one thing it will not do is ship a package that lies: if a stale
    ``plugin.meta.json`` copied in from the source tree cannot be removed, the
    build fails.
    """
    try:
        payload = derive_plugin_metadata(Path(target_dir))
    except MetadataProbeError as exc:
        stale = Path(target_dir) / PACKAGED_METADATA_FILENAME
        if stale.exists():
            # 源树里本来就有一份（内置插件的就在仓库里），打包管线会先把它抄进
            # target_dir。这次没能重新生成却把那份旧的留在包里，等于拿上一次的
            # handler 和 schema 冒充这次的——而它的 source_sha 完全可能还对得上，
            # 宿主于是照单全收，本该走的 manifest 回落根本不会发生（codex）。
            try:
                stale.unlink()
            except OSError:
                # Windows 上只读属性会让 unlink 直接失败。清掉属性再试一次。
                try:
                    stale.chmod(stat.S_IWRITE | stat.S_IREAD)
                    stale.unlink()
                except OSError as unlink_exc:
                    # 这条不能只警告。归档器照样会把这份旧文件打进包，而下面那句
                    # 警告说的是"这次不带元数据"——包和说法对不上，用户机器上则
                    # 拿着上一次构建的 handler 当真（codex）。出不了诚实的包就
                    # 不出包。
                    raise MetadataProbeError(
                        f"a stale {PACKAGED_METADATA_FILENAME} from an earlier "
                        f"build is in the package directory and cannot be "
                        f"removed ({unlink_exc}); delete "
                        f"{stale} and build again"
                    ) from unlink_exc
        print(
            f"[WARN] {Path(source_dir).name}: could not derive plugin metadata "
            f"({exc}); packaging without {PACKAGED_METADATA_FILENAME}. Entry "
            "parameter schemas stay unavailable until the plugin runs.",
            file=sys.stderr,
        )
        return None
    meta_path = Path(target_dir) / PACKAGED_METADATA_FILENAME
    # newline="" 而不是默认：默认会在 Windows 上把 LF 翻成 CRLF，于是盘上这份生成物
    # 和仓库里存的那份天生不一致，每次 git status 都报行尾要被改写。
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="",
    )
    return meta_path
