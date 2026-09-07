#!/usr/bin/env python3
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

"""One-shot probe for Qwen realtime TTS: verify `language_type` field placement.

Connects to wss://dashscope.aliyuncs.com/..., sends session.update with optional
session.language_type, then input_text_buffer.append + commit, and saves received
PCM audio to a .pcm file.

Usage:
  uv run python scripts/probe_qwen_tts.py --text "こんにちは、今日はいい天気ですね" \
      --language-type Japanese --save-audio /tmp/tts_probe/qwen_ja.pcm
  uv run python scripts/probe_qwen_tts.py --text "..." --language-type Auto \
      --save-audio /tmp/tts_probe/qwen_auto.pcm

Token is loaded from utils.config_manager.get_core_config()["ASSIST_API_KEY_QWEN"]
(the DashScope API key), not the AUDIO_API_KEY used by the lanlan.app free proxy.
"""  # noqa: DOCSTRING_CJK
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
import time
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from utils.config_manager import get_config_manager

TTS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen3-tts-flash-realtime"


async def run(args: argparse.Namespace, log: logging.Logger) -> int:
    cfg = get_config_manager().get_core_config()
    # DashScope key（走 ASSIST_API_KEY_QWEN），而非 AUDIO_API_KEY（= lanlan.app 免费代理 token）
    token = (cfg.get("ASSIST_API_KEY_QWEN") or "").strip()
    if not token:
        log.error("ASSIST_API_KEY_QWEN not set in config")
        return 2

    session_block = {
        "mode": "server_commit",
        "voice": args.voice,
        "response_format": "pcm",
        "sample_rate": 24000,
        "channels": 1,
        "bit_depth": 16,
    }
    if args.language_type and args.language_type.lower() != "auto":
        session_block["language_type"] = args.language_type

    headers = {"Authorization": f"Bearer {token}"}
    async with websockets.connect(TTS_URL, additional_headers=headers) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "event_id": f"event_{int(time.time() * 1000)}",
            "session": session_block,
        }))
        log.info("sent session.update (language_type=%s)", session_block.get("language_type", "<unset/Auto>"))

        # Wait for session.updated
        session_ready = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                continue
            evt = json.loads(raw)
            t = evt.get("type")
            log.info("recv %s", t)
            if t in ("session.created", "session.updated"):
                session_ready = True
                break
            if t == "error":
                log.error("server error: %s", evt)
                return 1
        if not session_ready:
            log.error("session not ready")
            return 1

        await ws.send(json.dumps({
            "type": "input_text_buffer.append",
            "event_id": f"event_{int(time.time() * 1000)}",
            "text": args.text,
        }))
        await ws.send(json.dumps({
            "type": "input_text_buffer.commit",
            "event_id": f"event_{int(time.time() * 1000)}_commit",
        }))
        log.info("sent append + commit (%d chars)", len(args.text))

        audio_chunks: list[bytes] = []
        deadline = time.monotonic() + 15.0
        done = False
        while time.monotonic() < deadline and not done:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                continue
            evt = json.loads(raw)
            t = evt.get("type")
            if t == "response.audio.delta":
                b = base64.b64decode(evt.get("delta", ""))
                audio_chunks.append(b)
            elif t in ("response.done", "response.audio.done", "output.done"):
                log.info("recv %s", t)
                done = True
            elif t == "error":
                log.error("server error: %s", evt)
                return 1
            else:
                log.debug("recv other: %s", t)

        total = b"".join(audio_chunks)
        log.info("audio chunks=%d, total=%d bytes", len(audio_chunks), len(total))

        if args.save_audio and total:
            out = Path(args.save_audio)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(total)
            log.info("saved PCM (24kHz mono int16) to %s", out.resolve())

        if not done:
            log.error("timed out waiting for response.done (recv %d chunks, %d bytes)",
                      len(audio_chunks), len(total))
            return 1

    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--text", required=True)
    p.add_argument("--voice", default="Momo")
    p.add_argument("--language-type", default="Auto", help="Japanese / Chinese / English / Auto")
    p.add_argument("--save-audio", default="")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("probe")
    return asyncio.run(run(args, log))


if __name__ == "__main__":
    raise SystemExit(main())
