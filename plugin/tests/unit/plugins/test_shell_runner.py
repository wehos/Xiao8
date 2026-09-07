# -*- coding: utf-8 -*-
# Copyright 2026 Project N.E.K.O. Team
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

"""Unit tests for the shell_runner plugin (platform-independent parts).

Execution of PowerShell / cmd is Windows-only and intentionally not
covered here; only the output-decoding helpers are tested.
"""

from plugin.plugins.shell_runner import _decode_bytes


def test_decode_bytes_utf8():
    assert _decode_bytes("中文测试".encode("utf-8")) == "中文测试"


def test_decode_bytes_gbk():
    assert _decode_bytes("中文测试".encode("gbk")) == "中文测试"


def test_decode_bytes_utf8_with_ascii():
    assert _decode_bytes(b"ipconfig /all") == "ipconfig /all"


def test_decode_bytes_empty():
    assert _decode_bytes(b"") == ""


def test_decode_bytes_unknown_encoding():
    # 既非 UTF-8 也非 GBK 的字节流 → errors=replace，不抛异常
    out = _decode_bytes(b"\xff\xfe\x00\x80\x41")
    assert isinstance(out, str)
    assert out.endswith("A")
