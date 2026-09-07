"""测试日志导出文件过滤逻辑"""
from pathlib import Path

import pytest

from plugin.server.logs import list_plugin_log_files_for_export


def test_export_filter_prevents_prefix_leak(tmp_path: Path):
    """插件 ID 前缀关系不应导致跨插件导出"""
    # 创建两个插件的日志文件：foo 和 foo_bar
    (tmp_path / "N.E.K.O_Plugin_foo_20260906.log").touch()
    (tmp_path / "N.E.K.O_Plugin_foo_error.log").touch()
    (tmp_path / "N.E.K.O_Plugin_foo_bar_20260906.log").touch()
    (tmp_path / "N.E.K.O_Plugin_foo_bar_error.log").touch()

    # 导出 foo 不应包含 foo_bar
    foo_files = list_plugin_log_files_for_export(tmp_path, "foo")
    foo_names = {f.name for f in foo_files}

    assert "N.E.K.O_Plugin_foo_20260906.log" in foo_names
    assert "N.E.K.O_Plugin_foo_error.log" in foo_names
    assert "N.E.K.O_Plugin_foo_bar_20260906.log" not in foo_names
    assert "N.E.K.O_Plugin_foo_bar_error.log" not in foo_names


def test_export_filter_includes_rotated_logs(tmp_path: Path):
    """导出应包含轮转日志和错误日志"""
    (tmp_path / "N.E.K.O_Plugin_demo_20260906.log").touch()
    (tmp_path / "N.E.K.O_Plugin_demo_20260905.log.1").touch()  # RotatingFileHandler 数字后缀
    (tmp_path / "N.E.K.O_Plugin_demo_20260904.log.2026-09-04").touch()  # TimedRotatingFileHandler 日期后缀
    (tmp_path / "N.E.K.O_Plugin_demo_error.log").touch()
    (tmp_path / "N.E.K.O_Plugin_demo_error.log.2").touch()  # RotatingFileHandler 数字后缀
    (tmp_path / "N.E.K.O_Plugin_demo_error.log.2026-09-03").touch()  # TimedRotatingFileHandler 日期后缀

    files = list_plugin_log_files_for_export(tmp_path, "demo")
    names = {f.name for f in files}

    assert len(names) == 6
    assert "N.E.K.O_Plugin_demo_20260906.log" in names
    assert "N.E.K.O_Plugin_demo_20260905.log.1" in names
    assert "N.E.K.O_Plugin_demo_20260904.log.2026-09-04" in names
    assert "N.E.K.O_Plugin_demo_error.log" in names
    assert "N.E.K.O_Plugin_demo_error.log.2" in names
    assert "N.E.K.O_Plugin_demo_error.log.2026-09-03" in names


def test_export_filter_uses_sanitized_id(tmp_path: Path):
    """长 ID 应使用 sanitized 后的文件名匹配"""
    # 模拟一个被截断哈希的长 ID
    long_id = "a" * 70  # 超过 64 字符限制
    from plugin.core.host import _sanitize_plugin_id
    safe_id = _sanitize_plugin_id(long_id, max_len=64)

    # 创建使用 safe_id 的日志文件
    (tmp_path / f"N.E.K.O_Plugin_{safe_id}_20260906.log").touch()

    # 使用原始 long_id 查询应能找到
    files = list_plugin_log_files_for_export(tmp_path, long_id)
    assert len(files) == 1
    assert files[0].name == f"N.E.K.O_Plugin_{safe_id}_20260906.log"


def test_export_filter_rejects_invalid_suffix(tmp_path: Path):
    """拒绝不符合日志命名规范的文件"""
    (tmp_path / "N.E.K.O_Plugin_demo_20260906.log").touch()
    (tmp_path / "N.E.K.O_Plugin_demo_badfile.txt").touch()  # 不符合规范
    (tmp_path / "N.E.K.O_Plugin_demo_20260906").touch()  # 缺少 .log
    # 能通过 *.log* 初筛但会被二次过滤拒绝
    (tmp_path / "N.E.K.O_Plugin_demo_20260906X.log").touch()  # 日期后有非法字符
    (tmp_path / "N.E.K.O_Plugin_demo_errorX.log").touch()  # error 后有非法字符
    (tmp_path / "N.E.K.O_Plugin_demo_20260906.log.tmp").touch()  # .log 后有非数字后缀
    (tmp_path / "N.E.K.O_Plugin_demo_error.logX").touch()  # .log 后有非数字后缀

    files = list_plugin_log_files_for_export(tmp_path, "demo")
    names = {f.name for f in files}

    assert len(names) == 1
    assert "N.E.K.O_Plugin_demo_20260906.log" in names
    assert "N.E.K.O_Plugin_demo_badfile.txt" not in names
    assert "N.E.K.O_Plugin_demo_20260906" not in names
    assert "N.E.K.O_Plugin_demo_20260906X.log" not in names
    assert "N.E.K.O_Plugin_demo_errorX.log" not in names
    assert "N.E.K.O_Plugin_demo_20260906.log.tmp" not in names
    assert "N.E.K.O_Plugin_demo_error.logX" not in names
