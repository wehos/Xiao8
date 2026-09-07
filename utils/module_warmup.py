# -*- coding: utf-8 -*-
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

"""Start background module warmup.

The startup chain imports only what greeting really needs; heavy SDKs (google-genai plus
the mcp, translatepy etc. it drags in) became lazy imports on first use. After the
service is ready, this module starts a daemon thread that runs those "first-use" imports
ahead of time in the background, so users don't pay the import latency mid-interaction
when they actually use the features.

GIL note: pure-Python modules hold the GIL while being parsed, but the bulk here is C
extension dlopen (which releases the GIL) and file IO, while the event loop is mostly
awaiting IO, so a low-priority daemon thread can make progress in the gaps without
stalling the loop. This is best-effort warmup, not a correctness path — any failure is
swallowed; the lazy import on first use remains the single source of truth.
"""
from __future__ import annotations

import importlib
import os
import threading
import time

from utils.logger_config import get_module_logger

logger = get_module_logger(__name__)

# main_server 进程 ready 后要预热的重模块。genai 会在 import 时捎带 mcp，
# 所以列了 genai 就不必单列 mcp。translatepy 的子翻译器各自有数据表，逐个列出
# 让它们都进 sys.modules 缓存。
MAIN_SERVER_WARMUP: tuple[str, ...] = (
    # openai/anthropic：曾在 utils/llm_client 顶层 import、坐在 merged 启动串行链
    # 最前端（合计 ~0.7s，大头是 pydantic 模型类构建）。改惰性后在此预热，首次真实
    # LLM 调用（greeting 等）不等 import。
    "openai",
    "anthropic",
    "google.genai",
    "google.genai.types",
    "translatepy",
    "translatepy.translators.microsoft",
    "translatepy.translators.bing",
    "translatepy.translators.reverso",
    "translatepy.translators.libre",
    "translatepy.translators.mymemory",
    "translatepy.translators.translatecom",
    "googletrans",
    # 功能路由的重依赖（声音克隆 TTS / 网易云 / 网页抓取 / B 站），从各 router /
    # util 模块顶层下放到 handler 后，在这里预热，保证首次点功能时不等 import。
    "dashscope",
    "dashscope.audio.tts_v2",
    "pyncm_async",
    "bs4",
    "bilibili_api",
    # 21 条封禁话题模板的 compile（其中 4 条各约 51 KB 正则源码），实测
    # 294-298 ms。原来在 config/prompts/prompts_directives 模块级做，坐在
    # memory_server 的 eager 导入链上、也就是端口 bind 之前。改惰性之后在
    # 这里预热，首次指令抽取不用等。
    "config.prompts.prompts_directives:DIRECTIVE_PATTERNS",
)

_warmup_lock = threading.Lock()
_warmup_started = False


def _warm_one(name: str) -> None:
    """Warm one entry: ``"module"``, or ``"module:attribute"``.

    The attribute form is for lazily-evaluated heavyweights -- a batch of regex
    compilations, say. Such a module is usually in ``sys.modules`` already,
    dragged in by something else, with only the expensive part deferred to first
    access; importing it again is a cache hit that runs no code, so warming it
    would do nothing. The attribute has to actually be read.
    """
    module_name, _, attr = name.partition(":")
    module = importlib.import_module(module_name)
    if attr:
        getattr(module, attr)


def start_background_warmup(modules, *, label: str = "server") -> bool:
    """Start a daemon thread to warm up ``modules``; runs only once per process.

    Returns whether a thread was actually started (``False`` if it already ran).
    """
    # 测试环境下不预热：daemon 线程跑真实重 import 既拖慢测试、又会在测试 logging
    # 拆除后回写日志报错，且预热是纯优化无行为契约，跳过完全安全。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False

    global _warmup_started
    with _warmup_lock:
        if _warmup_started:
            return False
        _warmup_started = True

    module_list = tuple(modules)

    def _run() -> None:
        t0 = time.monotonic()
        loaded = 0
        for name in module_list:
            start = time.monotonic()
            try:
                _warm_one(name)
                loaded += 1
                logger.debug(
                    "[warmup:%s] %s (%.0f ms)",
                    label, name, (time.monotonic() - start) * 1000,
                )
            except Exception as exc:
                logger.debug("[warmup:%s] skip %s: %s", label, name, exc)
            # 每个模块之间主动让出，给正在跑的事件循环一个抢回 GIL 的机会。
            time.sleep(0)
        logger.info(
            "[warmup:%s] done: %d/%d modules in %.0f ms",
            label, loaded, len(module_list), (time.monotonic() - t0) * 1000,
        )

    threading.Thread(target=_run, name=f"module-warmup-{label}", daemon=True).start()
    return True
