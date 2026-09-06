"""
OneBot-protocol QQ client (reverse WebSocket server / forward WebSocket client).

Supports two WS directions (``direction``):
- **reverse** (default): start a reverse WebSocket server and wait for any
  OneBot v11 implementation (NapCat / LLOneBot / go-cqhttp / Lagrange etc.) to
  connect as a WS client. Matches AstrBot's aiocqhttp reverse-WS mode.
- **forward**: dial out to the OneBot implementation's WS server
  (``ws://host:port``) as a WS client, for setups where the OneBot runtime runs
  on a remote device and the plugin side need not expose a port. The mode flag
  is ``napcat_forward`` (a ``qq_connection_mode`` value, historical naming).

The forward mode reuses the whole reverse-mode inbound/outbound pipeline
(``_process_incoming`` / ``receive_message`` / echo→future correlation / all
send wrappers); the only difference is the transport: it puts the single
outbound socket into ``_main_client`` / ``_connected_clients`` and has
``_forward_receive_loop`` do a single-task receive loop with auto-redial on
disconnect.
"""

import asyncio
import json
import re
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import websockets
from websockets.exceptions import ConnectionClosed

from .qq_connection import QQConnectionBase


class QQClient(QQConnectionBase):
    #: Observed transport for this connection. Stamped at INGEST, never read
    #: from live config at flush time: a session buffer can span a transport
    #: switch (the switch is immediate and does not clear buffers), so a
    #: flush-time read would attribute old messages to the new transport.
    #: Purely an observed attribute — it is never a key and never affects a
    #: score. See the kill list in ``memory/trust_store.py``.
    CHANNEL: str = "onebot"

    """OneBot-protocol client (reverse WebSocket server)."""

    def __init__(self, *, onebot_url: str, token: str = "", logger: Any = None,
                 emit_log: Any = None, message_queue_size: int = 100,
                 direction: str = "reverse"):
        self._onebot_url = str(onebot_url or "").strip()
        self.token = str(token or "")
        self.logger = logger
        self._emit_log = emit_log or (lambda level, msg: None)

        #: WS direction: "reverse"=reverse WS server (default) / "forward"=forward WS client.
        self.direction = str(direction or "reverse").strip().lower()
        #: Connection-mode flag the runtime uses to decide whether a reconnect is needed
        #: (reverse→"napcat", forward→"napcat_forward"). See the mode comparison in runtime_ops_service.
        self.mode = "napcat_forward" if self.direction == "forward" else "napcat"

        if self.direction == "forward":
            # Forward: onebot_url is the dial-out target of the OneBot WS server, not a listen address
            self._listen_host = "0.0.0.0"
            self._listen_port = 6199
        else:
            # Reverse: parse the listen address from onebot_url
            self._listen_host = "0.0.0.0"
            self._listen_port = 6199
            parsed = urlparse(self._onebot_url) if self._onebot_url else None
            if parsed and parsed.hostname:
                self._listen_host = parsed.hostname
                if parsed.port:
                    self._listen_port = parsed.port

        self._server: Optional[websockets.WebSocketServer] = None
        # Reverse mode holds the server-side protocols (many); forward mode holds the
        # single outbound client protocol -- both have .send/.close_code/.close() and
        # are iterable, so the code is shared.
        self._connected_clients: set[Any] = set()
        self._main_client: Optional[Any] = None  # latest connection, used for API calls
        # Forward mode's single outbound socket (None in reverse mode)
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None
        #: Forward-mode dial/reconnect parameters
        self._dial_timeout: float = 10.0
        self._reconnect_initial_backoff: float = 1.0
        self._reconnect_max_backoff: float = 30.0
        self._message_queue_maxsize = max(1, int(message_queue_size or 100))
        self._message_queue: asyncio.Queue = None  # lazy init in connect()
        self._pending_actions: Dict[str, asyncio.Future] = {}
        self._closing = False
        self._sent_message_ids: Dict[str, float] = {}  # message_id → sent_at timestamp
        self._group_muted: Dict[str, float] = {}    # group_id → muted_until (0=not muted, >0=muted until this timestamp)
        self._self_id: str = ""
        self._self_nickname: str = ""

    @property
    def onebot_url(self) -> str:
        return self._onebot_url

    @onebot_url.setter
    def onebot_url(self, value: str) -> None:
        self._onebot_url = str(value or "").strip()
        if self.direction == "forward":
            # Forward mode: onebot_url is the dial-out target, used as-is, not split as a listen address
            return
        self._listen_host = "0.0.0.0"
        self._listen_port = 6199
        parsed = urlparse(self._onebot_url) if self._onebot_url else None
        if parsed and parsed.hostname:
            self._listen_host = parsed.hostname
        if parsed and parsed.port:
            self._listen_port = parsed.port

    def is_connected(self) -> bool:
        # Prune disconnected connections
        dead = {c for c in self._connected_clients if getattr(c, 'close_code', None) is not None}
        self._connected_clients -= dead
        if self._main_client in dead:
            self._main_client = next(iter(self._connected_clients), None)
        return len(self._connected_clients) > 0

    async def get_login_status(self) -> dict[str, Any]:
        if self._connected_clients and self._self_id:
            return {"status": "online", "self_id": self._self_id, "nickname": self._self_nickname or None}
        return {"status": "offline", "self_id": None, "nickname": None}

    def is_group_muted(self, group_id: str) -> bool:
        """Whether the bot is muted in this group (incl. whole-group mute)."""
        gid = str(group_id or "").strip()
        if not gid:
            return False
        until = self._group_muted.get(gid, 0)
        if until and until > 0:
            import time
            if time.time() < until:
                return True
            # Mute expired; clean up
            del self._group_muted[gid]
        return False

    def _handle_group_ban_notice(self, notice: dict[str, Any]) -> None:
        """Handle group-ban notice: track whether the bot is muted/unmuted."""
        import time
        gid = str(notice.get("group_id") or "").strip()
        if not gid:
            return
        sub_type = str(notice.get("sub_type") or "").strip()  # "ban" / "lift_ban"
        user_id = str(notice.get("user_id") or "").strip()
        duration = int(notice.get("duration") or 0)  # seconds, only valid on ban

        # Whole-group mute (user_id=0) or self being muted
        is_whole_group = (user_id == "0")
        is_self = bool(self._self_id and user_id == str(self._self_id))

        if not is_whole_group and not is_self:
            return  # someone else muted; not our concern

        if sub_type == "ban":
            until = time.time() + max(duration, 1) if duration > 0 else float("inf")
            self._group_muted[gid] = until
            who = "全体" if is_whole_group else "自己"
            self._emit_log("INFO", f"[Mute] {who}被禁言: group={gid} duration={duration}s")
        elif sub_type == "lift_ban":
            self._group_muted.pop(gid, None)
            who = "全体" if is_whole_group else "自己"
            self._emit_log("INFO", f"[Mute] {who}解除禁言: group={gid}")

    @staticmethod
    def _looks_like_path(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text.startswith("file://"):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", text):
            return True
        return text.startswith("/")

    @classmethod
    def _build_image_attachment(cls, segment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            return None
        data = segment.get("data")
        if not isinstance(data, dict):
            return None
        raw_url = str(data.get("url") or "").strip()
        raw_path = str(data.get("path") or "").strip()
        raw_file = str(data.get("file") or "").strip()
        locator_type = ""
        locator_value = ""
        if raw_url:
            locator_type = "url"
            locator_value = raw_url
        elif raw_path:
            locator_type = "path"
            locator_value = raw_path
        elif raw_file:
            locator_type = "path" if cls._looks_like_path(raw_file) else "file"
            locator_value = raw_file
        if not locator_value:
            return None
        attachment = {
            "type": "image_url",
            "url": locator_value,
            "locator_type": locator_type,
            "source": "onebot:image",
        }
        if raw_path:
            attachment["path"] = raw_path
        if raw_file:
            attachment["file"] = raw_file
        return attachment

    @classmethod
    def _extract_attachments(cls, raw_msg: Dict[str, Any]) -> list[Dict[str, Any]]:
        segments = raw_msg.get("message")
        if not isinstance(segments, list):
            return []
        attachments: list[Dict[str, Any]] = []
        for segment in segments:
            attachment = cls._build_image_attachment(segment)
            if attachment:
                attachments.append(attachment)
            elif isinstance(segment, dict) and segment.get("type") == "record":
                data = segment.get("data")
                if isinstance(data, dict):
                    file_id = str(data.get("file") or "").strip()
                    if file_id:
                        attachments.append({"type": "record", "file": file_id})
        return attachments

    @classmethod
    def _extract_interaction_context(cls, raw_msg: Dict[str, Any]) -> Dict[str, Any]:
        segments = raw_msg.get("message")
        self_id = str(raw_msg.get("self_id") or "").strip()
        if not isinstance(segments, list):
            return {
                "quoted_message_id": "",
                "quoted_sender_id": "",
                "mentioned_user_ids": [],
                "mentions_other_user": False,
                "mentions_all": False,
                "mentions_bot": False,
            }

        quoted_message_id = ""
        quoted_sender_id = ""
        mentioned_user_ids: list[str] = []
        mentions_other_user = False
        mentions_all = False
        mentions_bot = False

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "").strip()
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            if segment_type == "reply":
                quoted_message_id = str(data.get("id") or data.get("message_id") or quoted_message_id).strip()
                quoted_sender_id = str(data.get("user_id") or data.get("qq") or "").strip()
                continue
            if segment_type != "at":
                continue
            mentioned_id = str(data.get("qq") or "").strip()
            if not mentioned_id:
                continue
            if mentioned_id == "all":
                mentions_all = True
                continue
            mentioned_user_ids.append(mentioned_id)
            if self_id and mentioned_id == self_id:
                mentions_bot = True
            else:
                mentions_other_user = True

        return {
            "quoted_message_id": quoted_message_id,
            "quoted_sender_id": quoted_sender_id,
            "mentioned_user_ids": mentioned_user_ids,
            "mentions_other_user": mentions_other_user,
            "mentions_all": mentions_all,
            "mentions_bot": mentions_bot,
        }

    # ── Connection lifecycle ─────────────────────────────────────────

    async def connect(self):
        """Establish the connection.

        Reverse mode: start a reverse WebSocket server and wait for OneBot clients
        to dial in; forward mode: start the background receive/redial loop (the
        loop owns the dial-out) and return idempotently. Forward mode does **not**
        block on dialing: the OneBot implementation's WS server only starts
        listening after its local process has booted and logged in, so it may not
        be ready at start time -- ``_forward_receive_loop`` retries with backoff,
        matching the reverse mode's "wait for OneBot to dial in" semantics.
        """
        self._closing = False
        if self.direction == "forward":
            if self._receive_task is not None and not self._receive_task.done():
                return  # receive/redial loop already running; idempotent
            self._message_queue = asyncio.Queue(maxsize=self._message_queue_maxsize)
            self._receive_task = asyncio.create_task(self._forward_receive_loop())
            return
        if self._server is not None:
            return
        # Re-create the queue in the current event loop (avoid cross-loop binding errors)
        self._message_queue = asyncio.Queue(maxsize=self._message_queue_maxsize)
        self._server = await websockets.serve(
            self._handle_client,
            host=self._listen_host,
            port=self._listen_port,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        )
        if self.logger:
            self.logger.info(f"Reverse WS server listening on {self._listen_host}:{self._listen_port}")

    async def disconnect(self):
        """Tear down the connection and clean up resources."""
        self._closing = True
        self._cancel_inbound_sink_tasks()

        # Cancel all pending requests
        for future in list(self._pending_actions.values()):
            if not future.done():
                future.cancel()
        self._pending_actions.clear()

        if self.direction == "forward":
            if self._receive_task:
                self._receive_task.cancel()
                try:
                    await self._receive_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._receive_task = None
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None
            self._connected_clients.clear()
            self._main_client = None
            if self.logger:
                self.logger.info("Forward WS client stopped")
            return

        # Close all connected clients
        for client in list(self._connected_clients):
            try:
                await client.close()
            except Exception:
                pass
        self._connected_clients.clear()
        self._main_client = None

        # Stop the server
        if self._server:
            try:
                self._server.close()
            except RuntimeError:
                pass  # event loop already closed
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

        if self.logger:
            self.logger.info("Reverse WS server stopped")

    # ── Forward WebSocket client ─────────────────────────────────

    def _forward_ws_url(self) -> str:
        """Build the forward dial-out URL: besides the Authorization header the
        token is also appended to the query (?access_token=, idempotent: skipped
        if already present), for OneBot implementations that only read query."""
        url = self._onebot_url
        if not self.token:
            return url
        parsed = urlparse(url)
        if "access_token" in parse_qs(parsed.query):
            return url
        sep = "&" if parsed.query else "?"
        return f"{url}{sep}{urlencode({'access_token': self.token})}"

    def _redact_url(self, url: str) -> str:
        """Redact the access_token from the URL query before logging, so the token
        never hits disk in plaintext.

        ``_forward_ws_url`` appends the token to the query; if the full URL were
        written to a file log after a successful connect, the token would stay on
        disk forever. Keep only the host/path in the log and replace the token
        with ``***`` (matching the plugin's ``_mask_token`` sanitizing habit).
        """
        if not url:
            return url
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "access_token" not in params:
                return url
            cleaned = "&".join(
                f"{k}={'***' if k == 'access_token' else v}"
                for k, values in params.items()
                for v in values
            )
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, cleaned, parsed.fragment))
        except Exception:
            return url

    def _redact_text(self, text: str) -> str:
        """Redact any plaintext token appearing in text (error messages may embed
        the full URL with the token).

        Replace both the raw value and the URL-encoded value: if the token contains
        ``+``/``/``/``=`` etc., the URL holds the encoded form (%2B / %2F / %3D),
        so replacing only the raw value would miss it.
        """
        try:
            raw = str(text)
            if self.token:
                for variant in (self.token, quote(self.token, safe="")):
                    if variant and variant in raw:
                        raw = raw.replace(variant, "***")
            return raw
        except Exception:
            return str(text)

    async def _dial_forward(self) -> bool:
        """Dial out to the OneBot implementation's WS server (called by the
        receive loop).

        Do not raise on failure: log and return False, letting
        ``_forward_receive_loop`` retry with backoff -- the WS server may not be
        ready yet (its process is still booting/logging in).
        """
        url = self._forward_ws_url()
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    url,
                    additional_headers=(
                        {"Authorization": f"Bearer {self.token}"} if self.token else None
                    ),
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=2 ** 23,
                ),
                timeout=self._dial_timeout,
            )
        except Exception as e:
            # Error messages may embed the full URL with the token (InvalidURI/InvalidStatus etc.); sanitize first
            self._emit_log("WARN", f"OneBot(正向) 拨出失败: {self._redact_text(e)}")
            return False
        self._ws = ws
        self._main_client = ws
        self._connected_clients = {ws}
        if self.logger:
            # Do not log the full URL with access_token; the token would sit in plaintext in log files
            self.logger.info(f"Forward WS connected to {self._redact_url(url)}")
        self._emit_log("INFO", "OneBot(正向) 已连接")
        # Asynchronously fetch login info on first connect (does not block the receive loop)
        if not self._self_id:
            asyncio.create_task(self._fetch_login_info_async())
        return True

    async def _forward_receive_loop(self):
        """Forward-mode receive + reconnect loop.

        A single task owns both recv and redial: after a disconnect it redials
        with backoff and keeps receiving on the new socket, equivalent to the
        OneBot implementation reconnecting on its own in reverse mode (only the
        initiator is on our side).
        """
        backoff = self._reconnect_initial_backoff
        while not self._closing:
            if self._ws is None or getattr(self._ws, "close_code", None) is not None:
                ok = await self._dial_forward()
                if not ok:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self._reconnect_max_backoff)
                    continue
                backoff = self._reconnect_initial_backoff
            ws = self._ws
            try:
                async for raw_message in ws:
                    try:
                        await self._process_incoming(raw_message)
                    except Exception:
                        if self.logger:
                            self.logger.exception("Error processing incoming message")
            except ConnectionClosed:
                pass
            except asyncio.CancelledError:
                break
            except Exception:
                if self.logger and not self._closing:
                    self.logger.exception("Unexpected error in forward receive loop")
            if self._closing:
                break
            self._emit_log("WARN", "OneBot(正向) 连接断开，等待重连...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max_backoff)

    # ── Client connection handling ────────────────────────────────────────

    def _check_token(self, websocket: websockets.WebSocketServerProtocol) -> bool:
        """Verify the client token; allow all connections if no token is configured."""
        if not self.token:
            return True

        # Extract access_token from the query string
        try:
            request_path = websocket.request.path if websocket.request else "/"
        except Exception:
            request_path = "/"
        parsed = urlparse(request_path)
        params = parse_qs(parsed.query)
        access_token = params.get("access_token", [None])[0]
        if access_token == self.token:
            return True

        # Extract the Bearer token from the Authorization header
        try:
            auth_header = websocket.request.headers.get("Authorization", "") if websocket.request else ""
        except Exception:
            auth_header = ""
        if auth_header.startswith("Bearer ") and auth_header[7:] == self.token:
            return True

        return False

    async def _handle_client(self, websocket: websockets.WebSocketServerProtocol):
        """Handle one OneBot client connection."""
        # Token auth
        if not self._check_token(websocket):
            if self.logger:
                addr = websocket.remote_address if hasattr(websocket, 'remote_address') else "unknown"
                self.logger.warning(f"Rejected unauthorized client from {addr}")
            await websocket.close(1008, "Unauthorized")
            return

        # Register the client
        self._connected_clients.add(websocket)
        was_first = self._main_client is None
        self._main_client = websocket
        addr = websocket.remote_address if hasattr(websocket, 'remote_address') else "unknown"
        if self.logger:
            self.logger.info(f"OneBot client connected from {addr}")
        if was_first:
            self._emit_log("INFO", "OneBot 已连接")
        else:
            self._emit_log("INFO", f"OneBot 重连成功(共{len(self._connected_clients)}个客户端)")

        # Asynchronously fetch login info on first connect (does not block the message loop)
        if not self._self_id:
            import asyncio
            asyncio.create_task(self._fetch_login_info_async())

        try:
            async for raw_message in websocket:
                try:
                    await self._process_incoming(raw_message)
                except Exception:
                    if self.logger:
                        self.logger.exception("Error processing incoming message")
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            if self.logger and not self._closing:
                self.logger.exception("Unexpected error in client handler")
        finally:
            self._connected_clients.discard(websocket)
            was_main = self._main_client is websocket
            if was_main:
                self._main_client = next(iter(self._connected_clients), None)
            addr = websocket.remote_address if hasattr(websocket, 'remote_address') else "unknown"
            if self.logger:
                self.logger.info(f"OneBot client disconnected from {addr}")
            if was_main:
                remaining = len(self._connected_clients)
                if remaining > 0:
                    self._emit_log("WARN", f"OneBot 主连接断开(剩余{remaining})，已切换备用")
                else:
                    self._emit_log("ERROR", "OneBot 已断开，等待重连...")

    # ── Message handling ──────────────────────────────────────────────






    _MAX_REPLY_DEPTH = 3  # max depth for recursively expanding quote chains








    #: text-file content injection cap (truncate and annotate beyond it, to avoid blowing up the context)
    _FILE_TEXT_MAX_BYTES = 100 * 1024
    #: file extensions treated as images and routed to the VLM path
    _IMAGE_FILE_EXTENSIONS = frozenset(
        {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic", ".svg"}
    )




    async def _process_incoming(self, raw_message: str):
        """Process one message from a OneBot client."""
        message = json.loads(raw_message)

        # echo match: this is a response to a previous call_action
        echo = message.get("echo")
        if echo and echo in self._pending_actions:
            future = self._pending_actions.pop(str(echo), None)
            if future and not future.done():
                future.set_result(message)
            return

        # Event routing
        if message.get("post_type") == "message":
            msg_type = message.get("message_type")
            if msg_type in {"private", "group"}:
                if not self._message_queue:
                    return
                try:
                    self._message_queue.put_nowait(message)
                except asyncio.QueueFull:
                    if self.logger:
                        self.logger.warning("Message queue full; dropping oldest message")
                    _ = self._message_queue.get_nowait()
                    self._message_queue.put_nowait(message)
                if self.logger:
                    if msg_type == "private":
                        self.logger.info(f"Queued private message from {message.get('user_id')}")
                    else:
                        self.logger.info(f"Queued group message from group {message.get('group_id')}, user {message.get('user_id')}")
        elif message.get("post_type") == "notice" and message.get("notice_type") == "notify" and message.get("sub_type") == "poke":
            # Poke event: enqueue so the bot can auto-poke back
            if not self._message_queue:
                return
            try:
                self._message_queue.put_nowait(message)
            except asyncio.QueueFull:
                pass
            if self.logger:
                self.logger.info(f"Queued poke notice: group {message.get('group_id')}, target {message.get('target_id')}, user {message.get('user_id')}")
        elif message.get("post_type") == "notice" and message.get("notice_type") == "group_ban":
            # Group ban notice: bot self muted/unmuted, or whole-group mute/unmute
            self._handle_group_ban_notice(message)

    async def receive_message(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Receive one message and return the normalized form."""
        if not self._message_queue:
            return None
        try:
            raw_msg = await asyncio.wait_for(self._message_queue.get(), timeout=timeout)  # noqa: ASYNC_BLOCK — _message_queue is asyncio.Queue, .get() is awaitable and does not block the event loop

            # Track our own QQ id
            self_id = raw_msg.get("self_id")
            if self_id:
                self._self_id = str(self_id)

            # Poke notice event
            if raw_msg.get("post_type") == "notice":
                return {
                    "message_type": "notice",
                    "channel": self.CHANNEL,
                    "notice_type": raw_msg.get("sub_type", ""),
                    "user_id": str(raw_msg.get("user_id") or ""),
                    "group_id": str(raw_msg.get("group_id") or ""),
                    "target_id": str(raw_msg.get("target_id") or ""),
                    "timestamp": raw_msg.get("time"),
                    "raw": raw_msg,
                }

            msg_type = raw_msg.get("message_type")
            sender_info = raw_msg.get("sender", {})
            user_nickname = sender_info.get("nickname") or sender_info.get("card") or None

            # Voice-message annotation
            content = str(raw_msg.get("raw_message") or "")
            has_record = any(
                isinstance(s, dict) and s.get("type") == "record"
                for s in (raw_msg.get("message") or [])
                if isinstance(s, dict)
            )
            if has_record and not content.strip():
                content = "[语音]"
            elif has_record:
                content = f"[语音] {content}"

            result = {
                "message_type": msg_type,
                "channel": self.CHANNEL,
                "user_id": str(raw_msg.get("user_id")),
                "user_nickname": user_nickname,
                "content": content,
                "message_id": raw_msg.get("message_id"),
                "timestamp": raw_msg.get("time"),
                "raw": raw_msg,
                "attachments": self._extract_attachments(raw_msg),
            }

            if msg_type == "group":
                interaction_context = self._extract_interaction_context(raw_msg)
                result["group_id"] = str(raw_msg.get("group_id"))
                result["quoted_message_id"] = interaction_context["quoted_message_id"]
                result["quoted_sender_id"] = interaction_context["quoted_sender_id"]
                result["mentioned_user_ids"] = interaction_context["mentioned_user_ids"]
                result["mentions_other_user"] = interaction_context["mentions_other_user"]
                result["mentions_all"] = interaction_context["mentions_all"]
                is_reply_to_bot = await self._is_reply_to_bot_message(
                    interaction_context["quoted_message_id"],
                )
                result["is_at_bot"] = (
                    interaction_context["mentions_bot"]
                    or interaction_context["mentions_all"]
                )
                # Store is_reply_to_bot separately, not merged into is_at_bot:
                #   - replying to the catgirl should not skip the buffer (only a direct @ does)
                #   - Attention Gate distinguishes @ vs. reply via is_direct_at
                result["is_reply_to_bot"] = is_reply_to_bot

            # Inbound broadcast hook (optional): hand the normalized message to the registered sink. Best-effort.
            await self._dispatch_inbound(result)
            return result
        except asyncio.TimeoutError:
            return None

    def _check_at_bot(self, raw_msg: Dict[str, Any]) -> bool:
        """Check whether the message @-mentions the bot."""
        message = raw_msg.get("message", [])
        if isinstance(message, list):
            for seg in message:
                if seg.get("type") == "at":
                    at_qq = seg.get("data", {}).get("qq")
                    if at_qq == "all":
                        return True
                    if str(at_qq) == str(raw_msg.get("self_id")):
                        return True
        return False

    _SENT_MSG_TTL_SECONDS = 3600  # sent-message IDs cached for 1 hour

    def record_sent_message_id(self, message_id: str) -> None:
        """Record a sent message ID (to detect whether a reply is to the bot)."""
        mid = str(message_id or "").strip()
        if mid:
            import time
            self._sent_message_ids[mid] = time.time()
            self._cleanup_sent_message_cache()

    async def _is_reply_to_bot_message(self, quoted_message_id: str) -> bool:
        """Check via the get_msg API whether the quoted message was sent by the bot (KiraAI-compatible)."""
        qid = str(quoted_message_id or "").strip()
        if not qid:
            return False
        # Fast path: local cache hit
        if qid in self._sent_message_ids:
            self._emit_log("DEBUG", f"[ReplyCheck] 缓存命中: msg_id={qid}")
            return True
        # API query (fallback: still correct after a restart loses the local cache)
        try:
            data = await self.get_msg(qid)
            sender_id = str(data.get("sender", {}).get("user_id") or data.get("user_id") or "")
            self._emit_log("DEBUG", f"[ReplyCheck] API查询: msg_id={qid} sender={sender_id} self={self._self_id}")
            if sender_id and sender_id == str(self._self_id or ""):
                self.record_sent_message_id(qid)  # cache the result
                return True
        except Exception as e:
            self._emit_log("DEBUG", f"[ReplyCheck] API失败: msg_id={qid} err={e}")
        return False

    def _cleanup_sent_message_cache(self) -> None:
        """Evict expired sent-message IDs."""
        import time
        now = time.time()
        expired = [
            mid for mid, ts in self._sent_message_ids.items()
            if now - ts > self._SENT_MSG_TTL_SECONDS
        ]
        for mid in expired:
            del self._sent_message_ids[mid]

    # ── OneBot API calls ────────────────────────────────────────

    async def call_action(self, action: str, params: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        if not self._main_client:
            raise RuntimeError("No OneBot client connected")
        # ServerConnection has no .open; use close_code to detect it
        if getattr(self._main_client, 'close_code', None) is not None:
            self._connected_clients.discard(self._main_client)
            self._main_client = next(iter(self._connected_clients), None)
            if not self._main_client:
                raise RuntimeError("No OneBot client connected")

        echo = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        self._pending_actions[echo] = future
        payload = {
            "action": action,
            "params": params or {},
            "echo": echo,
        }
        try:
            await self._main_client.send(json.dumps(payload))
            if self.logger:
                self.logger.info(f"call_action sent: {action} echo={echo}")
        except Exception:
            self._pending_actions.pop(echo, None)
            if not future.done():
                future.cancel()
            raise
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            if self.logger:
                self.logger.info(f"call_action response: {action} status={response.get('status')}")
            if response.get("status") == "failed":
                raise RuntimeError(response.get("wording") or f"OneBot action failed: {action}")
            return response.get("data") or {}
        except asyncio.TimeoutError:
            self._emit_log("ERROR", f"call_action 超时: {action} (10秒未响应)")
            if self.logger:
                self.logger.warning(f"call_action timeout: {action} echo={echo}")
            raise
        finally:
            self._pending_actions.pop(echo, None)

    async def _fetch_login_info_async(self) -> None:
        """Background task: fetch login info and cache it (does not block message handling)."""
        try:
            await asyncio.sleep(0.5)
            await self.get_login_info()
        except Exception as e:
            self._emit_log("ERROR", f"获取账号信息失败: {e}")
            if self.logger:
                self.logger.warning(f"Background login info fetch failed: {e}")

    async def get_login_info(self) -> Dict[str, Any]:
        data = await self.call_action("get_login_info", timeout=5.0)
        uid = str(data.get("user_id") or "").strip()
        nick = str(data.get("nickname") or "").strip()
        if uid:
            self._self_id = uid
        if nick:
            self._self_nickname = nick
        if self.logger:
            self.logger.debug(f"get_login_info: self_id={uid}, nickname={nick}")
        return data

    async def get_friend_list(self) -> list[Dict[str, Any]]:
        data = await self.call_action("get_friend_list", timeout=10.0)
        return data if isinstance(data, list) else []

    async def get_group_list(self) -> list[Dict[str, Any]]:
        data = await self.call_action("get_group_list", timeout=10.0)
        return data if isinstance(data, list) else []

    async def send_message(self, user_id: str, message: str) -> Optional[str]:
        """Send a private message (CQ-code string); returns the message_id.

        Same action as the segments variant, only the message field is a string --
        the encoding must not change (putting CQ codes in a text segment would show
        [CQ:at,qq=…] verbatim to the user), but the receipt can be requested: without
        it we can't tell "sent" from "not sent", so the delivery-confirmation chain
        would unconditionally judge success, and undelivered replies would be cleared
        as delivered, dropping the exclusion mark and entering scoped memory."""
        return await self._send_text_action(
            "send_private_msg", {"user_id": int(user_id), "message": message},
            log_target=f"private {user_id}",
        )

    async def send_group_message(self, group_id: str, message: str) -> Optional[str]:
        """Send a group message (CQ-code string); returns the message_id. See send_message."""
        return await self._send_text_action(
            "send_group_msg", {"group_id": int(group_id), "message": message},
            log_target=f"group {group_id}", record_sent=True,
        )

    async def _send_text_action(
        self, action: str, params: Dict[str, Any], *,
        log_target: str, record_sent: bool = False,
    ) -> Optional[str]:
        """One echo round-trip for the CQ-string senders (same plumbing as
        the segment senders; responses are dispatched by echo, not action)."""
        if not self._main_client:
            raise RuntimeError("No OneBot client connected")

        echo = secrets.token_hex(8)
        payload = {"action": action, "params": params, "echo": echo}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_actions[echo] = future
        try:
            await self._main_client.send(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout=10.0)
            message_id = str((response.get("data") or {}).get("message_id") or "")
            if message_id and record_sent:
                self.record_sent_message_id(message_id)
            if self.logger:
                self.logger.debug(f"Sent message to {log_target}")
            return message_id if message_id else None
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_actions.pop(echo, None)

    async def send_private_message_segments(self, user_id: str, segments: list[Dict[str, Any]], *, record_sent: bool = True) -> Optional[str]:
        """Send a private message as segments; returns the message_id."""
        if not self._main_client:
            raise RuntimeError("No OneBot client connected")

        echo = secrets.token_hex(8)
        payload = {
            "action": "send_private_msg",
            "params": {"user_id": int(user_id), "message": segments},
            "echo": echo,
        }
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_actions[echo] = future
        try:
            await self._main_client.send(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout=10.0)
            message_id = str((response.get("data") or {}).get("message_id") or "")
            if message_id and record_sent:
                self.record_sent_message_id(message_id)
            return message_id if message_id else None
        except asyncio.TimeoutError:
            return None
        except Exception:
            raise
        finally:
            self._pending_actions.pop(echo, None)
        if self.logger:
            self.logger.debug(f"Sent segmented private message to {user_id}")

    async def send_private_record(self, user_id: str, file_uri: str, *, reply_message_id: str = "") -> Optional[str]:
        """Send a private-message voice note."""
        segments: list[Dict[str, Any]] = []
        if str(reply_message_id or "").strip():
            segments.append({"type": "reply", "data": {"id": str(reply_message_id)}})
        segments.append({"type": "record", "data": {"file": str(file_uri or "")}})
        return await self.send_private_message_segments(user_id, segments)

    async def send_group_record(self, group_id: str, file_uri: str, *, reply_message_id: str = "", at_user_id: str = "") -> Optional[str]:
        """Send a group-message voice note."""
        segments: list[Dict[str, Any]] = []
        if str(reply_message_id or "").strip():
            segments.append({"type": "reply", "data": {"id": str(reply_message_id)}})
        if str(at_user_id or "").strip():
            segments.append({"type": "at", "data": {"qq": str(at_user_id)}})
        segments.append({"type": "record", "data": {"file": str(file_uri or "")}})
        return await self.send_group_message_segments(group_id, segments)

    async def send_group_message_segments(self, group_id: str, segments: list[Dict[str, Any]], *, record_sent: bool = True, keyboard: str = "") -> Optional[str]:
        """Send a group message as segments; returns the message_id."""
        if not self._main_client:
            raise RuntimeError("No OneBot client connected")

        echo = secrets.token_hex(8)
        payload = {
            "action": "send_group_msg",
            "params": {
                "group_id": int(group_id),
                "message": segments,
            },
            "echo": echo,
        }

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_actions[echo] = future
        try:
            await self._main_client.send(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout=10.0)
            message_id = str((response.get("data") or {}).get("message_id") or "")
            if message_id and record_sent:
                self.record_sent_message_id(message_id)
            return message_id if message_id else None
        except asyncio.TimeoutError:
            return None
        except Exception:
            raise
        finally:
            self._pending_actions.pop(echo, None)
        if self.logger:
            self.logger.debug(f"Sent segmented group message to {group_id}")

    async def send_group_poke(self, group_id: str, user_id: str) -> bool:
        """Send a group poke."""
        if not self._main_client:
            raise RuntimeError("No OneBot client connected")
        try:
            payload = {
                "action": "send_poke",
                "params": {
                    "group_id": int(group_id),
                    "user_id": int(user_id),
                },
            }
            await self._main_client.send(json.dumps(payload))
            if self.logger:
                self.logger.info(f"Sent poke to user {user_id} in group {group_id}")
            return True
        except Exception as e:
            if self.logger:
                self.logger.warning(f"Failed to send poke: {e}")
            return False

    async def get_record(self, file_id: str, output_format: str = "mp3") -> dict[str, Any]:
        """Fetch a voice file (returns base64-encoded audio)."""
        return await self.call_action("get_record", {"file": file_id, "out_format": output_format}, timeout=15.0)

    async def send_group_image(self, group_id: str, image_data: str, *, reply_message_id: str = "", at_user_id: str = "", sub_type: str = "") -> Optional[str]:
        """Send a group image.

        Args:
            group_id: the group id
            image_data: image URL, base64 string (with base64:// prefix), or a local file path
            sub_type: image subtype; "1"=emoji-sticker (non-regular image)
        """
        if not self._main_client:
            raise RuntimeError("No OneBot client connected")
        segments: list[Dict[str, Any]] = []
        if str(reply_message_id or "").strip():
            segments.append({"type": "reply", "data": {"id": str(reply_message_id)}})
        if str(at_user_id or "").strip():
            segments.append({"type": "at", "data": {"qq": str(at_user_id)}})
        img_data: dict[str, str] = {"file": str(image_data)}
        if sub_type:
            img_data["sub_type"] = sub_type
        segments.append({"type": "image", "data": img_data})
        return await self.send_group_message_segments(group_id, segments, record_sent=False)

    async def send_private_image(self, user_id: str, image_data: str) -> Optional[str]:
        """Send a private image."""
        if not self._main_client:
            raise RuntimeError("No OneBot client connected")
        segments: list[Dict[str, Any]] = [
            {"type": "image", "data": {"file": str(image_data)}}
        ]
        return await self.send_private_message_segments(user_id, segments)

    # ── Message operations ────────────────────────────────────────────────

    async def set_msg_emoji_like(self, message_id: str, emoji_id: str) -> Dict[str, Any]:
        """Set an emoji reaction on a message."""
        return await self.call_action("set_msg_emoji_like", {"message_id": int(message_id), "emoji_id": str(emoji_id)}, timeout=5.0)

    async def delete_msg(self, message_id: str) -> Dict[str, Any]:
        """Recall a message."""
        return await self.call_action("delete_msg", {"message_id": int(message_id)}, timeout=5.0)

    async def get_msg(self, message_id: str) -> Dict[str, Any]:
        """Fetch message details."""
        return await self.call_action("get_msg", {"message_id": int(message_id)}, timeout=10.0)

    async def get_forward_msg(self, forward_id: str) -> Dict[str, Any]:
        """Fetch the content of a merged-forward message."""
        return await self.call_action("get_forward_msg", {"id": str(forward_id)}, timeout=5.0)

    # ── Friend operations ────────────────────────────────────────────────

    async def send_like(self, user_id: str, times: int = 1) -> Dict[str, Any]:
        """Like a friend."""
        return await self.call_action("send_like", {"user_id": int(user_id), "times": max(1, int(times))}, timeout=5.0)

    async def set_friend_add_request(self, flag: str, approve: bool, remark: str = "") -> Dict[str, Any]:
        """Handle a friend-add request."""
        return await self.call_action("set_friend_add_request", {"flag": str(flag), "approve": bool(approve), "remark": str(remark or "")}, timeout=5.0)

    # ── Group management operations ──────────────────────────────────────────────

    async def set_group_kick(self, group_id: str, user_id: str, reject_add_request: bool = False) -> Dict[str, Any]:
        """Kick a user from a group."""
        return await self.call_action("set_group_kick", {"group_id": int(group_id), "user_id": int(user_id), "reject_add_request": bool(reject_add_request)}, timeout=5.0)

    async def set_group_ban(self, group_id: str, user_id: str, duration: int = 1800) -> Dict[str, Any]:
        """Mute a single group member (duration in seconds; 0 = unmute)."""
        return await self.call_action("set_group_ban", {"group_id": int(group_id), "user_id": int(user_id), "duration": int(duration)}, timeout=5.0)

    async def set_group_whole_ban(self, group_id: str, enable: bool = True) -> Dict[str, Any]:
        """Mute the entire group."""
        return await self.call_action("set_group_whole_ban", {"group_id": int(group_id), "enable": bool(enable)}, timeout=5.0)

    async def set_group_admin(self, group_id: str, user_id: str, enable: bool = True) -> Dict[str, Any]:
        """Set or revoke a group admin."""
        return await self.call_action("set_group_admin", {"group_id": int(group_id), "user_id": int(user_id), "enable": bool(enable)}, timeout=5.0)

    async def set_group_card(self, group_id: str, user_id: str, card: str = "") -> Dict[str, Any]:
        """Set a group member's card (remark)."""
        return await self.call_action("set_group_card", {"group_id": int(group_id), "user_id": int(user_id), "card": str(card or "")}, timeout=5.0)

    async def set_group_name(self, group_id: str, group_name: str) -> Dict[str, Any]:
        """Set the group name."""
        return await self.call_action("set_group_name", {"group_id": int(group_id), "group_name": str(group_name)}, timeout=5.0)

    async def set_group_leave(self, group_id: str, is_dismiss: bool = False) -> Dict[str, Any]:
        """Leave or dismiss a group."""
        return await self.call_action("set_group_leave", {"group_id": int(group_id), "is_dismiss": bool(is_dismiss)}, timeout=5.0)

    async def set_group_special_title(self, group_id: str, user_id: str, special_title: str = "", duration: int = -1) -> Dict[str, Any]:
        """Set a group member's exclusive title (duration in seconds; -1 = permanent)."""
        return await self.call_action("set_group_special_title", {"group_id": int(group_id), "user_id": int(user_id), "special_title": str(special_title), "duration": int(duration)}, timeout=5.0)

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = "") -> Dict[str, Any]:
        """Handle a group-add request/invitation."""
        return await self.call_action("set_group_add_request", {"flag": str(flag), "sub_type": str(sub_type), "approve": bool(approve), "reason": str(reason or "")}, timeout=5.0)

    # ── Info fetch ────────────────────────────────────────────────

    async def get_stranger_info(self, user_id: str, no_cache: bool = False) -> Dict[str, Any]:
        """Fetch stranger info."""
        return await self.call_action("get_stranger_info", {"user_id": int(user_id), "no_cache": bool(no_cache)}, timeout=5.0)

    async def get_group_info(self, group_id: str, no_cache: bool = False) -> Dict[str, Any]:
        """Fetch group info."""
        return await self.call_action("get_group_info", {"group_id": int(group_id), "no_cache": bool(no_cache)}, timeout=5.0)

    async def get_group_member_info(self, group_id: str, user_id: str, no_cache: bool = False) -> Dict[str, Any]:
        """Fetch a group member's info."""
        return await self.call_action("get_group_member_info", {"group_id": int(group_id), "user_id": int(user_id), "no_cache": bool(no_cache)}, timeout=5.0)

    async def get_group_member_list(self, group_id: str, no_cache: bool = False) -> list[Dict[str, Any]]:
        """Fetch the group member list."""
        data = await self.call_action("get_group_member_list", {"group_id": int(group_id), "no_cache": bool(no_cache)}, timeout=10.0)
        return data if isinstance(data, list) else []

    async def get_group_honor_info(self, group_id: str, type: str = "all") -> Dict[str, Any]:
        """Fetch group honor info."""
        return await self.call_action("get_group_honor_info", {"group_id": int(group_id), "type": str(type)}, timeout=5.0)

    # ── Cookies / credentials ──────────────────────────────────────────

    async def get_cookies(self, domain: str = "") -> Dict[str, Any]:
        """Fetch cookies."""
        return await self.call_action("get_cookies", {"domain": str(domain)}, timeout=5.0)

    async def get_csrf_token(self) -> Dict[str, Any]:
        """Fetch the CSRF token."""
        return await self.call_action("get_csrf_token", timeout=5.0)

    async def get_credentials(self, domain: str = "") -> Dict[str, Any]:
        """Fetch QQ credentials."""
        return await self.call_action("get_credentials", {"domain": str(domain)}, timeout=5.0)

    # ── Resource fetch ────────────────────────────────────────────────

    async def get_image(self, file: str) -> Dict[str, Any]:
        """Fetch image file data."""
        return await self.call_action("get_image", {"file": str(file)}, timeout=10.0)

    async def can_send_image(self) -> Dict[str, Any]:
        """Check whether sending images is allowed."""
        return await self.call_action("can_send_image", timeout=5.0)

    async def can_send_record(self) -> Dict[str, Any]:
        """Check whether sending voice notes is allowed."""
        return await self.call_action("can_send_record", timeout=5.0)

    # ── Status / version ─────────────────────────────────────────────

    async def get_status(self) -> Dict[str, Any]:
        """Fetch the runtime status."""
        return await self.call_action("get_status", timeout=5.0)

    async def get_version_info(self) -> Dict[str, Any]:
        """Fetch version info."""
        return await self.call_action("get_version_info", timeout=5.0)

    async def clean_cache(self) -> Dict[str, Any]:
        """Clear the cache."""
        return await self.call_action("clean_cache", timeout=5.0)

    # ── Account settings ────────────────────────────────────────────────

    async def set_qq_profile(self, nickname: str = "", company: str = "", email: str = "", college: str = "", personal_note: str = "") -> Dict[str, Any]:
        """Set the QQ profile."""
        return await self.call_action("set_qq_profile", {"nickname": str(nickname or ""), "company": str(company or ""), "email": str(email or ""), "college": str(college or ""), "personal_note": str(personal_note or "")}, timeout=5.0)

    async def set_qq_avatar(self, file: str) -> Dict[str, Any]:
        """Set the QQ avatar."""
        return await self.call_action("set_qq_avatar", {"file": str(file)}, timeout=10.0)

    async def set_self_longnick(self, longnick: str) -> Dict[str, Any]:
        """Set one's own signature."""
        return await self.call_action("set_self_longnick", {"longnick": str(longnick)}, timeout=5.0)

    async def set_online_status(self, status: int) -> Dict[str, Any]:
        """Set the online status."""
        return await self.call_action("set_online_status", {"status": int(status)}, timeout=5.0)

    async def get_online_clients(self) -> Dict[str, Any]:
        """Fetch the list of currently online clients."""
        return await self.call_action("get_online_clients", timeout=5.0)

    async def get_robot_uin_range(self) -> Dict[str, Any]:
        """Fetch the robot UIN range."""
        return await self.call_action("get_robot_uin_range", timeout=5.0)

    # ── Friend management ────────────────────────────────────────────────

    async def delete_friend(self, user_id: str) -> Dict[str, Any]:
        """Delete a friend."""
        return await self.call_action("delete_friend", {"user_id": int(user_id)}, timeout=5.0)

    async def get_friends_with_category(self) -> Dict[str, Any]:
        """Fetch the friends list grouped by category."""
        return await self.call_action("get_friends_with_category", timeout=10.0)

    async def friend_poke(self, user_id: str) -> Dict[str, Any]:
        """Poke a friend."""
        return await self.call_action("friend_poke", {"user_id": int(user_id)}, timeout=5.0)

    async def get_profile_like(self) -> Dict[str, Any]:
        """Fetch one's own like list."""
        return await self.call_action("get_profile_like", timeout=5.0)

    # ── Message read ────────────────────────────────────────────────

    async def mark_msg_as_read(self, message_id: str) -> Dict[str, Any]:
        """Mark a message as read."""
        return await self.call_action("mark_msg_as_read", {"message_id": int(message_id)}, timeout=5.0)

    async def mark_private_msg_as_read(self, user_id: str) -> Dict[str, Any]:
        """Mark a private message as read."""
        return await self.call_action("mark_private_msg_as_read", {"user_id": int(user_id)}, timeout=5.0)

    async def mark_group_msg_as_read(self, group_id: str) -> Dict[str, Any]:
        """Mark a group message as read."""
        return await self.call_action("mark_group_msg_as_read", {"group_id": int(group_id)}, timeout=5.0)

    async def _mark_all_as_read(self) -> Dict[str, Any]:
        """Mark all messages as read."""
        return await self.call_action("_mark_all_as_read", timeout=5.0)

    # ── Merged forward ────────────────────────────────────────────────

    async def send_group_forward_msg(self, group_id: str, messages: list[Dict[str, Any]]) -> Dict[str, Any]:
        """Send a merged-forward message to a group."""
        return await self.call_action("send_group_forward_msg", {"group_id": int(group_id), "messages": messages}, timeout=10.0)

    async def send_private_forward_msg(self, user_id: str, messages: list[Dict[str, Any]]) -> Dict[str, Any]:
        """Send a merged-forward message to a friend."""
        return await self.call_action("send_private_forward_msg", {"user_id": int(user_id), "messages": messages}, timeout=10.0)

    async def send_forward_msg(self, message_type: str, user_id: str = "", group_id: str = "", messages: Optional[list[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Send a merged-forward message (generic)."""
        params: Dict[str, Any] = {"message_type": str(message_type), "messages": messages or []}
        if user_id:
            params["user_id"] = int(user_id)
        if group_id:
            params["group_id"] = int(group_id)
        return await self.call_action("send_forward_msg", params, timeout=10.0)

    async def forward_friend_single_msg(self, user_id: str, message_id: str) -> Dict[str, Any]:
        """Forward a single friend message."""
        return await self.call_action("forward_friend_single_msg", {"user_id": int(user_id), "message_id": int(message_id)}, timeout=5.0)

    async def forward_group_single_msg(self, group_id: str, message_id: str) -> Dict[str, Any]:
        """Forward a single group message."""
        return await self.call_action("forward_group_single_msg", {"group_id": int(group_id), "message_id": int(message_id)}, timeout=5.0)

    # ── Message history / input status / recent contacts ───────────────────────

    async def get_friend_msg_history(self, user_id: str, message_seq: int = 0, count: int = 20) -> Dict[str, Any]:
        """Fetch a friend's message history."""
        return await self.call_action("get_friend_msg_history", {"user_id": int(user_id), "message_seq": int(message_seq), "count": int(count)}, timeout=10.0)

    async def get_group_msg_history(self, group_id: str, message_seq: int = 0, count: int = 20) -> Dict[str, Any]:
        """Fetch a group's message history."""
        return await self.call_action("get_group_msg_history", {"group_id": int(group_id), "message_seq": int(message_seq), "count": int(count)}, timeout=10.0)

    async def set_input_status(self, user_id: str = "", group_id: str = "", event_type: int = 1) -> Dict[str, Any]:
        """Set the input status (1: typing, 2: stop typing)."""
        params: Dict[str, Any] = {"event_type": int(event_type)}
        if user_id:
            params["user_id"] = int(user_id)
        if group_id:
            params["group_id"] = int(group_id)
        return await self.call_action("set_input_status", params, timeout=5.0)

    async def get_recent_contact(self) -> Dict[str, Any]:
        """Fetch the recent contact list."""
        return await self.call_action("get_recent_contact", timeout=10.0)

    # ── Group system messages / announcements ──────────────────────────────────────

    async def get_group_system_msg(self) -> Dict[str, Any]:
        """Fetch group system messages."""
        return await self.call_action("get_group_system_msg", timeout=5.0)

    async def _send_group_notice(self, group_id: str, content: str, image: str = "") -> Dict[str, Any]:
        """Send a group announcement."""
        return await self.call_action("_send_group_notice", {"group_id": int(group_id), "content": str(content), "image": str(image)}, timeout=5.0)

    async def _get_group_notice(self, group_id: str) -> Dict[str, Any]:
        """Fetch a group announcement."""
        return await self.call_action("_get_group_notice", {"group_id": int(group_id)}, timeout=5.0)

    async def _del_group_notice(self, group_id: str, notice_id: str) -> Dict[str, Any]:
        """Delete a group announcement."""
        return await self.call_action("_del_group_notice", {"group_id": int(group_id), "notice_id": str(notice_id)}, timeout=5.0)

    async def get_group_at_all_remain(self, group_id: str) -> Dict[str, Any]:
        """Fetch the remaining @-all count for a group."""
        return await self.call_action("get_group_at_all_remain", {"group_id": int(group_id)}, timeout=5.0)

    async def get_group_ignore_add_request(self, group_id: str) -> Dict[str, Any]:
        """Fetch the list of ignored group-add requests."""
        return await self.call_action("get_group_ignore_add_request", {"group_id": int(group_id)}, timeout=5.0)

    async def get_group_shut_list(self, group_id: str) -> list[Dict[str, Any]]:
        """Fetch the group's mute list."""
        data = await self.call_action("get_group_shut_list", {"group_id": int(group_id)}, timeout=5.0)
        return data if isinstance(data, list) else []

    # ── Group sign-in / avatar ───────────────────────────────────────────

    async def set_group_sign(self, group_id: str, sign: str = "") -> Dict[str, Any]:
        """Set the group sign-in."""
        return await self.call_action("set_group_sign", {"group_id": int(group_id), "sign": str(sign)}, timeout=5.0)

    async def send_group_sign(self, group_id: str) -> Dict[str, Any]:
        """Perform group sign-in."""
        return await self.call_action("send_group_sign", {"group_id": int(group_id)}, timeout=5.0)

    async def set_group_portrait(self, group_id: str, file: str, is_set: bool = True) -> Dict[str, Any]:
        """Set the group avatar."""
        return await self.call_action("set_group_portrait", {"group_id": int(group_id), "file": str(file), "is_set": bool(is_set)}, timeout=10.0)

    async def get_group_info_ex(self, group_id: str) -> Dict[str, Any]:
        """Fetch extended group info."""
        return await self.call_action("get_group_info_ex", {"group_id": int(group_id)}, timeout=5.0)

    # ── Essence messages ────────────────────────────────────────────────

    async def get_essence_msg_list(self, group_id: str) -> Dict[str, Any]:
        """Fetch the essence message list."""
        return await self.call_action("get_essence_msg_list", {"group_id": int(group_id)}, timeout=5.0)

    async def set_essence_msg(self, message_id: str) -> Dict[str, Any]:
        """Set an essence message."""
        return await self.call_action("set_essence_msg", {"message_id": int(message_id)}, timeout=5.0)

    async def delete_essence_msg(self, message_id: str) -> Dict[str, Any]:
        """Remove an essence message."""
        return await self.call_action("delete_essence_msg", {"message_id": int(message_id)}, timeout=5.0)

    # ── Group files ──────────────────────────────────────────────────

    async def upload_group_file(self, group_id: str, file: str, name: str = "", folder: str = "") -> Dict[str, Any]:
        """Upload a group file."""
        return await self.call_action("upload_group_file", {"group_id": int(group_id), "file": str(file), "name": str(name), "folder": str(folder)}, timeout=30.0)

    async def delete_group_file(self, group_id: str, file_id: str, busid: int = 0) -> Dict[str, Any]:
        """Delete a group file."""
        return await self.call_action("delete_group_file", {"group_id": int(group_id), "file_id": str(file_id), "busid": int(busid)}, timeout=5.0)

    async def create_group_file_folder(self, group_id: str, name: str) -> Dict[str, Any]:
        """Create a group file folder."""
        return await self.call_action("create_group_file_folder", {"group_id": int(group_id), "name": str(name)}, timeout=5.0)

    async def delete_group_folder(self, group_id: str, folder_id: str) -> Dict[str, Any]:
        """Delete a group file folder."""
        return await self.call_action("delete_group_folder", {"group_id": int(group_id), "folder_id": str(folder_id)}, timeout=5.0)

    async def get_group_file_system_info(self, group_id: str) -> Dict[str, Any]:
        """Fetch the group file system info."""
        return await self.call_action("get_group_file_system_info", {"group_id": int(group_id)}, timeout=5.0)

    async def get_group_root_files(self, group_id: str) -> Dict[str, Any]:
        """Fetch the group root-file list."""
        return await self.call_action("get_group_root_files", {"group_id": int(group_id)}, timeout=10.0)

    async def get_group_files_by_folder(self, group_id: str, folder_id: str) -> Dict[str, Any]:
        """Fetch the group folder file list."""
        return await self.call_action("get_group_files_by_folder", {"group_id": int(group_id), "folder_id": str(folder_id)}, timeout=10.0)

    async def get_group_file_url(self, group_id: str, file_id: str, busid: int = 0) -> Dict[str, Any]:
        """Fetch a group file download URL."""
        return await self.call_action("get_group_file_url", {"group_id": int(group_id), "file_id": str(file_id), "busid": int(busid)}, timeout=5.0)

    async def get_private_file_url(self, user_id: str, file_id: str) -> Dict[str, Any]:
        """Fetch a private file download URL (NapCat requires both user_id and file_id)."""
        return await self.call_action(
            "get_private_file_url",
            {"user_id": int(user_id), "file_id": str(file_id)},
            timeout=5.0,
        )

    async def upload_private_file(self, user_id: str, file: str, name: str = "") -> Dict[str, Any]:
        """Upload a private file."""
        return await self.call_action("upload_private_file", {"user_id": int(user_id), "file": str(file), "name": str(name)}, timeout=30.0)

    async def download_file(self, url: str, thread_count: int = 3, headers: Optional[list[str]] = None) -> Dict[str, Any]:
        """Download a file to local."""
        return await self.call_action("download_file", {"url": str(url), "thread_count": int(thread_count), "headers": headers or []}, timeout=60.0)

    async def get_file(self, url: str, thread_count: int = 3, headers: Optional[list[str]] = None) -> Dict[str, Any]:
        """Fetch file data."""
        return await self.call_action("get_file", {"url": str(url), "thread_count": int(thread_count), "headers": headers or []}, timeout=60.0)

    async def get_file_by_id(self, file_id: str) -> Dict[str, Any]:
        """Fetch file info by file_id (OneBot v11 standard ``get_file``).

        Used when a group-file message has only ``file_id`` and no ``busid``, as a
        substitute for ``get_group_file_url`` (NapCat's needs a real busid; passing 0
        is rejected).
        """
        return await self.call_action("get_file", {"file_id": str(file_id)}, timeout=5.0)

    # ── AI / OCR / translation ─────────────────────────────────────────

    async def ocr_image(self, image: str) -> Dict[str, Any]:
        """OCR an image."""
        return await self.call_action("ocr_image", {"image": str(image)}, timeout=10.0)

    async def check_url_safely(self, url: str) -> Dict[str, Any]:
        """Check link safety."""
        return await self.call_action("check_url_safely", {"url": str(url)}, timeout=5.0)

    async def translate_en2zh(self, words: str) -> Dict[str, Any]:
        """Translate English to Chinese."""
        return await self.call_action("translate_en2zh", {"words": str(words)}, timeout=5.0)

    async def fetch_custom_face(self, count: int = 10) -> Dict[str, Any]:
        """Fetch the favorite-emoji list."""
        return await self.call_action("fetch_custom_face", {"count": int(count)}, timeout=5.0)

    async def fetch_emoji_like(self, message_id: str, emoji_id: str, emoji_type: str = "", set: bool = True) -> Dict[str, Any]:
        """Fetch the emoji-like list on a message."""
        return await self.call_action("fetch_emoji_like", {"message_id": int(message_id), "emoji_id": str(emoji_id), "emoji_type": str(emoji_type), "set": bool(set)}, timeout=5.0)

    async def create_collection(self, rawdata: str, brief: str = "") -> Dict[str, Any]:
        """Create a collection."""
        return await self.call_action("create_collection", {"rawdata": str(rawdata), "brief": str(brief)}, timeout=5.0)

    async def get_collection_list(self, category: int = 0) -> Dict[str, Any]:
        """Fetch the collection list."""
        return await self.call_action("get_collection_list", {"category": int(category)}, timeout=5.0)

    # ── Model display ────────────────────────────────────────────────

    async def _get_model_show(self, model: str) -> Dict[str, Any]:
        """Fetch the model-display config."""
        return await self.call_action("_get_model_show", {"model": str(model)}, timeout=5.0)

    async def _set_model_show(self, model: str, model_show: str) -> Dict[str, Any]:
        """Set the model display."""
        return await self.call_action("_set_model_show", {"model": str(model), "model_show": str(model_show)}, timeout=5.0)

    # ── NapCat extensions ─────────────────────────────────────────────

    async def ArkSharePeer(self, user_id: str, ark_json: str) -> Dict[str, Any]:
        """Share an ARK message to a friend."""
        return await self.call_action("ArkSharePeer", {"user_id": int(user_id), "ark_json": str(ark_json)}, timeout=10.0)

    async def ArkShareGroup(self, group_id: str, ark_json: str) -> Dict[str, Any]:
        """Share an ARK message to a group."""
        return await self.call_action("ArkShareGroup", {"group_id": int(group_id), "ark_json": str(ark_json)}, timeout=10.0)

    async def handle_quick_operation(self, context: Dict[str, Any], operation: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a quick operation."""
        return await self.call_action(".handle_quick_operation", {"context": context, "operation": operation}, timeout=5.0)

    async def get_mini_app_ark(self, appid: str = "", app_type: str = "", app_path: str = "", title: str = "", desc: str = "", pic_url: str = "", jump_url: str = "", scene: int = 0) -> Dict[str, Any]:
        """Fetch a mini-app ARK message."""
        return await self.call_action("get_mini_app_ark", {"appid": str(appid), "app_type": str(app_type), "app_path": str(app_path), "title": str(title), "desc": str(desc), "pic_url": str(pic_url), "jump_url": str(jump_url), "scene": int(scene)}, timeout=10.0)

    async def nc_get_packet_status(self) -> Dict[str, Any]:
        """Fetch the packet status."""
        return await self.call_action("nc_get_packet_status", timeout=5.0)

    async def nc_get_user_status(self, user_id: str) -> Dict[str, Any]:
        """Fetch a user's status."""
        return await self.call_action("nc_get_user_status", {"user_id": int(user_id)}, timeout=5.0)

    async def nc_get_rkey(self) -> Dict[str, Any]:
        """Fetch the rkey."""
        return await self.call_action("nc_get_rkey", timeout=5.0)

    # ── AI voice ─────────────────────────────────────────────────

    async def get_ai_record(self, group_id: str, character_id: str, text: str) -> Dict[str, Any]:
        """Fetch AI voice."""
        return await self.call_action("get_ai_record", {"group_id": int(group_id), "character_id": str(character_id), "text": str(text)}, timeout=30.0)

    async def get_ai_characters(self, group_id: str, chat_type: int = 1) -> Dict[str, Any]:
        """Fetch the AI role list."""
        return await self.call_action("get_ai_characters", {"group_id": int(group_id), "chat_type": int(chat_type)}, timeout=10.0)

    async def send_group_ai_record(self, group_id: str, character_id: str, text: str) -> Dict[str, Any]:
        """Send a group-message AI voice note."""
        return await self.call_action("send_group_ai_record", {"group_id": int(group_id), "character_id": str(character_id), "text": str(text)}, timeout=30.0)

    async def group_poke(self, group_id: str, user_id: str) -> Dict[str, Any]:
        """Poke in a group (via API)."""
        return await self.call_action("group_poke", {"group_id": int(group_id), "user_id": int(user_id)}, timeout=5.0)
