"""QQ Open Platform connector -- official QQ Bot API."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import re as _re
import httpx
import websockets

from .qq_connection import QQConnectionBase

_CQ_CODE_RE = _re.compile(r"\[CQ:(\w+),([^\]]+)\]")

# ==========================================
# R11 identity scope: resolved
# (docs/design/speaker-trust-entity-semantics.md §2.15.4)
# ==========================================
#
# Conclusion: in Open Platform group/private events there is **no ``id`` key under
# ``author``**. This rests on two official first-party Tencent sources that
# corroborate each other:
#
# - The official docs bot-docs `server-inter/message/send-receive/event.md` field
#   table and example JSON: `C2C_MESSAGE_CREATE`'s author has only `user_openid`;
#   `GROUP_AT_MESSAGE_CREATE`'s author has only `member_openid`, and the group
#   identifier is `group_openid`;
# - The official SDK botpy `message.py`: `C2CMessage._User` reads only
#   `user_openid`, `GroupMessage._User` reads only `member_openid`; only the
#   **channel**-system `Message._User` has `id` -- and this connector does not
#   handle channel message events.
#
# Yet `author.get("id")` was what this file used to read (copied from
# napcat/OneBot's `sender.user_id`; even the `<@!(\d+)>` pure-digit regex was a
# product of the same copy). As a result both paths' user_id was **always empty**:
# every speaker collapsed into one empty identity, `_maybe_reserve_open_platform_admin`
# never fired (because `not sender_id`), and private replies POSTed to
# `/v2/users//messages`.
#
# The official "unique identity mechanism" also answers the scope question: *the
# same bot sees a different per-group unique id (member_openid) for the same user*.
# So R11 is confirmed and `actor_scope = per_conversation`, taking the degraded
# path per §2.15.4.3 (registration in `settings_service.declare_identity_scope`;
# the manual-assertion UI is on the trusted-user page).
#
# The forensics constants / functions below are **kept**: the official doc field
# table does not guarantee an exhaustive enumeration of the real payload (e.g. an
# undocumented union_openid sibling key), so keeping them lets us confirm on a live
# bot. Default off; never participates in any decision.
_IDENTITY_PROBE_TAG = "[R11]"
_IDENTITY_PROBE_EVENTS = ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE")
#: Max lines recorded per connection. Forensics only needs three (group X, group Y,
#: private each); the cap exists so the log doesn't grow unbounded if the switch is
#: left on, not to limit forensics.
_IDENTITY_PROBE_MAX_LINES = 200
#: Char cap per field value, defending against an abnormally long payload blowing
#: up the log.
_IDENTITY_PROBE_VALUE_MAX_CHARS = 128


def _is_identifier_key(name: str) -> bool:
    """Judge whether a field name looks like an identifier field, **by shape**.

    Deliberately not an enum ``{"id", "member_openid", ...}``: one of the questions
    forensics must answer is exactly "is there another openid sibling key under
    author?", and an enum would keep the value of that unanticipated key out of the
    log, defeating the forensics.
    """
    lowered = str(name).lower()
    return lowered == "id" or lowered.endswith("_id") or "openid" in lowered


def _probe_identifier_values(mapping: Any) -> str:
    """Pick out the **values** of identifier fields; read nothing else."""
    if not isinstance(mapping, dict):
        return "{}"
    picked: dict[str, str] = {}
    for raw_key, raw_value in mapping.items():
        key = str(raw_key)
        if not _is_identifier_key(key):
            continue
        value = str(raw_value)
        if len(value) > _IDENTITY_PROBE_VALUE_MAX_CHARS:
            value = value[:_IDENTITY_PROBE_VALUE_MAX_CHARS] + "…"
        picked[key] = value
    return json.dumps(picked, ensure_ascii=False, sort_keys=True)


def _probe_key_names(mapping: Any) -> str:
    """Take only field **names**. Names contain no user content, so the whole set can be logged."""
    if not isinstance(mapping, dict):
        return "[]"
    return json.dumps(sorted(str(k) for k in mapping), ensure_ascii=False)


def build_identity_probe_line(event_type: str, data: Any) -> str:
    """Build one forensics log line. Pure function, no side effects.

    Emits four items, mapping exactly to the four criteria in the §2.15.4.2 table:

    - ``author.ids``  -- ① author.id itself; ② values of sibling keys like
      member_openid / user_openid / union_openid (which one is equal across groups
      is decided by comparing here);
    - ``author.keys`` -- ② the **full** key names of those siblings (incl. the
      unanticipated ones);
    - ``group.ids``   -- ③ which key holds the group id and what its value is;
    - ``data.keys``   -- ③ fallback: in case the group-id key name doesn't even
      contain "group".

    **Logs identifier-field values only.** Body, attachment URLs, @ lists never
    enter the log -- this line lands on a persistent file
    (my docs/N.E.K.O/logs/, survives restart, which forensics needs it to), and
    the instrumentation can be switched off after forensics ends, but rows already
    written won't roll back.
    """
    payload = data if isinstance(data, dict) else {}
    author = payload.get("author")
    group_fields = {
        str(k): v for k, v in payload.items() if "group" in str(k).lower()
    }
    return (
        f"{_IDENTITY_PROBE_TAG} event={event_type} "
        f"author.ids={_probe_identifier_values(author)} "
        f"author.keys={_probe_key_names(author)} "
        f"group.ids={_probe_identifier_values(group_fields)} "
        f"data.keys={_probe_key_names(payload)}"
    )


#: Order in which speaker ids are read, first non-empty wins. The two paths
#: **must be kept separate**: the same real person is user_openid in private and a
#: distinct member_openid in every group; no single key has a value on both sides
#: (see module top).
#:
#: `id` stays last as a fallback, not because we doubt the official docs, but
#: because removing it would silently return "" when the protocol adds a key --
#: and exactly that (an empty speaker id) is the shape of this defect: permission,
#: memory, and send paths all silently do the wrong thing. A possibly-wrong id is
#: better than an empty one.
_C2C_ACTOR_ID_KEYS = ("user_openid", "id")
_GROUP_ACTOR_ID_KEYS = ("member_openid", "id")


def pick_actor_id(author: Any, keys: tuple[str, ...]) -> str:
    """Take the first non-empty speaker identifier per ``keys`` order. Pure function."""
    if not isinstance(author, dict):
        return ""
    for key in keys:
        value = str(author.get(key) or "").strip()
        if value:
            return value
    return ""


class QQOpenPlatformConnection(QQConnectionBase):
    #: Observed transport (see QQClient.CHANNEL). Never a key.
    CHANNEL: str = "open"

    """Official QQ Open Platform Bot API connection.

    WebSocket events -> internal unified message format -> upper-layer pipeline;
    HTTP API -> send messages.
    """

    _API_BASE = "https://api.sgroup.qq.com"
    _TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

    def __init__(
        self,
        *,
        app_id: str,
        client_secret: str,
        logger: Any = None,
        message_queue_size: int = 100,
        identity_probe: Any = None,
        emit_log: Any = None,
    ):
        #: Zero-arg callback; only records R11 forensics when it returns truthy
        #: (see module top). A callback (not a bool) so the toggle takes effect
        #: immediately without reconnecting.
        self._identity_probe = identity_probe
        #: The plugin's in-memory log ring (what the UI "runtime logs" page reads).
        #: No-op by default, same convention as QQClient.
        self._emit_log = emit_log or (lambda level, msg: None)
        self._identity_probe_emitted = 0
        #: Connection-mode marker, so runtime knows whether it must rebuild the
        #: connection (= "open_platform").
        self.mode = "open_platform"
        self._app_id = str(app_id or "").strip()
        self._client_secret = str(client_secret or "").strip()
        self.token = ""
        self.logger = logger
        self.ws = None
        self._ws = None
        self._http = None
        self._access_token = ""
        self._token_expires_at: float = 0
        self._heartbeat_task = None
        self._receive_task = None
        self._heartbeat_interval: float = 30.0
        self._closing = False
        self._self_id = ""
        self._self_nickname = ""
        self._last_seq = 0
        self._session_id = ""  # needed for Resume reconnect
        self._sent_message_ids: dict[str, float] = {}
        self._message_queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, message_queue_size))

    @property
    def needs_attention(self) -> bool:
        return False  # Open Platform only gets @bot; no attention competition

    @property
    def supports_voice(self) -> bool:
        return False  # Open Platform does not support voice messages

    @property
    def supports_poke(self) -> bool:
        return False  # Open Platform does not support poke

    @property
    def receives_all_messages(self) -> bool:
        return False  # Open Platform only receives @bot messages

    @property
    def supports_ark_cards(self) -> bool:
        return True  # Open Platform natively supports Ark rich cards

    async def get_login_info(self) -> dict[str, Any]:
        return {"user_id": self._self_id, "nickname": self._self_nickname}

    async def get_friend_list(self) -> list[dict[str, Any]]:
        return []

    async def get_group_list(self) -> list[dict[str, Any]]:
        return []

    # ==========================================
    # connection lifecycle
    # ==========================================

    async def connect(self) -> None:
        if not self._app_id or not self._client_secret:
            raise RuntimeError("QQ 开放平台: app_id 和 client_secret 未配置")
        self._closing = False
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        await self._refresh_token()
        if self.logger:
            self.logger.info(f"[QQOpenPlatform] token 已获取")
        ws_url = await self._get_gateway_url()
        if self.logger:
            self.logger.info(f"[QQOpenPlatform] 连接网关: {ws_url[:60]}...")
        self._ws = await websockets.connect(ws_url, max_size=2 ** 23)
        self.ws = self._ws
        if self.logger:
            self.logger.info("[QQOpenPlatform] WebSocket 已连接")
        await self._handshake(is_reconnect=False)
        self._receive_task = asyncio.create_task(self._receive_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _handshake(self, *, is_reconnect: bool) -> None:
        """WebSocket handshake: Hello -> [Resume] -> Identify -> READY."""
        # Hello
        raw = await self._ws.recv()
        hello = json.loads(raw)
        if hello.get("op") == 10:
            self._heartbeat_interval = max(10.0, float(hello["d"]["heartbeat_interval"]) / 1000.0 - 2.0)
            if self.logger:
                self.logger.info(f"[QQOpenPlatform] Hello 收到, 心跳间隔: {self._heartbeat_interval:.0f}s")
        # On reconnect prefer Resume; fall back to Identify on failure.
        if is_reconnect and self._session_id:
            await self._ws.send(json.dumps({
                "op": 6, "d": {"token": f"QQBot {self._access_token}",
                                "session_id": self._session_id,
                                "seq": self._last_seq},
            }))
            try:
                resp = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
                event = json.loads(resp)
                if event.get("op") == 0 and event.get("t") == "RESUMED":
                    if self.logger:
                        self.logger.info("[QQOpenPlatform] Resume 成功，事件已补发")
                    return
                if self.logger:
                    self.logger.warning(f"[QQOpenPlatform] Resume 失败(op={event.get('op')} t={event.get('t')})，回退 Identify")
            except asyncio.TimeoutError:
                if self.logger:
                    self.logger.warning("[QQOpenPlatform] Resume 超时，回退 Identify")
        # Identify
        await self._ws.send(json.dumps({
            "op": 2, "d": {
                "token": f"QQBot {self._access_token}",
                "intents": (1 << 25) | (1 << 12),
                "shard": [0, 1],
            },
        }))
        resp = await self._ws.recv()
        ready = json.loads(resp)
        if ready.get("op") == 0 and ready.get("t") == "READY":
            user = ready["d"].get("user") or {}
            self._self_id = str(user.get("id") or "")
            self._self_nickname = str(user.get("username") or "")
            self._session_id = str(ready["d"].get("session_id") or "")
            if self.logger:
                self.logger.info(f"[QQOpenPlatform] 已就绪: {self._self_nickname} ({self._self_id})")
        else:
            raise RuntimeError(f"鉴权失败: op={ready.get('op')} t={ready.get('t')}")

    async def disconnect(self) -> None:
        self._closing = True
        self._cancel_inbound_sink_tasks()
        for task in [self._heartbeat_task, self._receive_task]:
            if task and not task.done():
                task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None
            self.ws = None
        if self._http:
            await self._http.aclose()
            self._http = None

    def is_connected(self) -> bool:
        return self._ws is not None

    # ==========================================
    # message receive
    # ==========================================

    def _identity_probe_enabled(self) -> bool:
        probe = getattr(self, "_identity_probe", None)
        return bool(probe()) if callable(probe) else False

    def _write_identity_probe(self, text: str) -> None:
        """One forensics line must go to both pools; either alone is not enough.

        - ``self.logger``: file log (my docs/N.E.K.O/logs/), **survives restart**; this
          is the copy you can send a whole to the developer;
        - ``self._emit_log``: the plugin's 500-entry memory ring, i.e. what the UI
          "runtime logs" page reads. Without it, after the user flips the switch
          they see nothing on the log page -- while the adjacent "trusted users"
          page's existing copy just taught them "ID... can be viewed in the log".
          (``get_recent_logs`` only falls back to the file when the ring is empty,
          and the ring is never empty from startup, so writing only the file means
          being completely invisible on the UI.)
        """
        # Single-arg call: the line carries a raw platform id that may contain %,
        # which would blow up logging's %-formatting.
        self.logger.info(text)
        self._emit_log("INFO", text)

    def _log_identity_probe(self, event_type: str, data: Any) -> None:
        """Record one R11 forensics log line (see module top).

        **Never leaks an exception**: ``_receive_loop``'s catch-all ``except Exception``
        treats any exception as a disconnect and reconnects; a forensics line does not
        deserve to trigger a reconnect.
        """
        try:
            if event_type not in _IDENTITY_PROBE_EVENTS:
                return
            if not self.logger or not self._identity_probe_enabled():
                return
            emitted = getattr(self, "_identity_probe_emitted", 0)
            if emitted > _IDENTITY_PROBE_MAX_LINES:
                return
            self._identity_probe_emitted = emitted + 1
            if emitted == _IDENTITY_PROBE_MAX_LINES:
                # The counter lives on this connection object, and qq_auto_reply only
                # sets it to None (rebuilding) when the **connection mode** changes
                # (runtime_ops_service.py:44-48) -- the sidebar "stop -> start" does
                # NOT rebuild it. So the only correct instruction here is "restart the
                # app"; saying "restart auto-reply" would be a lie.
                self._write_identity_probe(
                    f"{_IDENTITY_PROBE_TAG} 已记录 {_IDENTITY_PROBE_MAX_LINES} "
                    "条，达到上限，后续不再记录；重启应用后重新计数。"
                )
                return
            self._write_identity_probe(build_identity_probe_line(event_type, data))
        except Exception:
            # Deliberately swallowed: re-raising would hit _receive_loop's catch-all
            # and be treated as a disconnect, so a failed diagnostic line could drop
            # the bot offline once.
            pass

    async def _receive_loop(self) -> None:
        while not self._closing:
            if not self._ws:
                await asyncio.sleep(1)
                continue
            try:
                raw = await self._ws.recv()
                payload = json.loads(raw)
                op = payload.get("op")
                if op == 0:  # Dispatch
                    self._last_seq = payload.get("s", self._last_seq)
                    event_type = payload.get("t", "")
                    # The R11 forensics instrumentation must land here, not inside
                    # _convert_event: (a) to sidestep uncertainty about the group-id
                    # key name (_convert_event reads only group_id, which sees nothing
                    # if the platform ships group_openid), and (b) to run before the
                    # trusted-group whitelist gate so an unconfigured group can still
                    # be sampled.
                    self._log_identity_probe(event_type, payload.get("d"))
                    if event_type in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
                        msg = self._convert_event(event_type, payload["d"])
                        if msg:
                            try:
                                self._message_queue.put_nowait(msg)
                            except asyncio.QueueFull:
                                self._message_queue.get_nowait()
                                self._message_queue.put_nowait(msg)
                    continue  # handled; skip reconnect
                elif op == 1:  # Heartbeat
                    await self._ws.send(json.dumps({"op": 11, "d": self._last_seq}))
                    continue  # handled; skip reconnect
                elif op == 11:  # Heartbeat ACK -> ignore
                    continue
                elif op == 7:  # Reconnect -> close current ws; rebuilt below in _try_reconnect()
                    if self.logger:
                        self.logger.warning("[QQOpenPlatform] 服务端要求重连")
                    if self._ws:
                        try: await self._ws.close()
                        except Exception: pass
                    self._ws = None
                    self.ws = None
                # op==7 and other unknown ops -> no continue; fall through to reconnect
            except websockets.ConnectionClosed:
                if self.logger:
                    self.logger.warning("[QQOpenPlatform] WebSocket 断开")
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"[QQOpenPlatform] 接收异常: {e}")
            # Disconnected -> reconnect
            if not self._closing:
                await self._try_reconnect()

    async def receive_message(self, timeout: float = 1.0) -> Optional[dict[str, Any]]:
        try:
            message = await asyncio.wait_for(self._message_queue.get(), timeout=timeout)  # noqa: ASYNC_BLOCK — _message_queue 是 asyncio.Queue，.get() 可 await，不阻塞事件循环
        except asyncio.TimeoutError:
            return None
        # Inbound broadcast hook (optional): hand the normalized message to the
        # registered sink. Best-effort.
        if isinstance(message, dict):
            await self._dispatch_inbound(message)
        return message

    # ==========================================
    # message send
    # ==========================================

    @staticmethod
    def _expand_cq_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expand CQ codes inside a text segment into typed segments.

        reply_delivery_node embeds CQ-code strings (e.g. ``[CQ:reply,id=...]``) in the
        text field; NapCat's OneBot protocol natively understands these codes, but the
        Open Platform needs real typed segments. Here the CQ codes inside a text segment
        are split out.
        """
        expanded: list[dict[str, Any]] = []
        for seg in segments:
            if seg.get("type") != "text":
                expanded.append(seg)
                continue
            raw = str(seg.get("data", {}).get("text", "") or "")
            if not raw:
                expanded.append(seg)
                continue
            # No CQ code -> put back as-is.
            if "[CQ:" not in raw:
                expanded.append(seg)
                continue
            # Has CQ codes -> split segment by segment.
            pos = 0
            for m in _CQ_CODE_RE.finditer(raw):
                if m.start() > pos:
                    expanded.append({"type": "text", "data": {"text": raw[pos:m.start()]}})
                cq_type = m.group(1)
                params_str = m.group(2)
                data: dict[str, str] = {}
                for param in params_str.split(","):
                    param = param.strip()
                    if "=" in param:
                        k, v = param.split("=", 1)
                        data[k.strip()] = v.strip()
                expanded.append({"type": cq_type, "data": data})
                pos = m.end()
            if pos < len(raw):
                expanded.append({"type": "text", "data": {"text": raw[pos:]}})
        return expanded

    async def send_group_message_segments(
        self, group_id: str, segments: list[dict[str, Any]], *, record_sent: bool = True, keyboard: str = ""
    ) -> Optional[str]:
        """Convert OneBot segments to QQ Open Platform format and send."""
        content_parts: list[str] = []
        reply_msg_id = ""
        at_user_id = ""
        image_url = ""

        for seg in self._expand_cq_segments(segments):
            seg_type = str(seg.get("type") or "").strip()
            data = seg.get("data") or {}
            if seg_type == "reply":
                reply_msg_id = str(data.get("id") or "")
            elif seg_type == "at":
                at_user_id = str(data.get("qq") or "")
                content_parts.append(f"<@!{at_user_id}>")
            elif seg_type == "text":
                content_parts.append(str(data.get("text") or ""))
            elif seg_type == "image":
                image_url = str(data.get("file") or "")
            elif seg_type == "face":
                # small emoji -> text placeholder
                content_parts.append(f"[表情{data.get('id','')}]")
            elif seg_type == "record":
                content_parts.append("[语音消息]")

        content = "".join(content_parts).strip()
        if not content and not image_url:
            return None

        await self._ensure_token()
        body: dict[str, Any] = {}
        # Group images must be uploaded first to get file_info, then sent via msg_type=7 + media.
        if image_url:
            file_info = await self._upload_group_image(group_id, image_url)
            if file_info:
                body["msg_type"] = 7
                body["media"] = {"file_info": file_info}
                if content:
                    body["content"] = content
            else:
                # upload failed -> degrade to text
                if not content:
                    content = "[图片]"
                body["content"] = content
        else:
            # Auto-detect Markdown syntax (only clear format markers, avoid misjudging plain text).
            _MD_PATTERNS = (r'\*\*[^*]+\*\*', r'\*[^*]+\*', r'~~[^~]+~~', r'^> ', r'`[^`]+`', r'\[.+\]\(.+\)', r'^#{1,3} ')
            import re as _re
            is_md = any(_re.search(p, content, _re.MULTILINE) for p in _MD_PATTERNS)
            if is_md:
                body["msg_type"] = 2
                body["markdown"] = {"content": content}
            else:
                body["content"] = content

        if reply_msg_id:
            body["msg_id"] = reply_msg_id

        if keyboard and body.get("msg_type") == 7:
            # Rich-media (image) payloads can't carry buttons: buttons only apply to
            # type-2 rich text; forcing them on might have the platform reject the whole
            # message. Degrade the options to readable body so the user doesn't get a
            # "asked but no options" message.
            labels = " / ".join(
                b.strip() for b in keyboard.split("|") if b.strip()
            )
            if labels:
                existing = str(body.get("content") or "")
                body["content"] = (existing + "\n" + labels).strip()
            keyboard = ""
        if keyboard:
            buttons = [b.strip() for b in keyboard.split("|") if b.strip()][:4]
            if buttons:
                body.setdefault("msg_type", 2)
                body["keyboard"] = {
                    "content": {
                        "rows": [{
                            "buttons": [
                                {
                                    "id": f"btn_{i}",
                                    "render_data": {"label": b, "visited_label": b},
                                    "action": {"type": 2, "permission": {"type": 2}, "data": b, "unsupport_tips": "请升级QQ版本"},
                                }
                                for i, b in enumerate(buttons)
                            ]
                        }]
                    }
                }
                if content:
                    body.pop("content", None)
                    body["markdown"] = {"content": content}

        try:
            resp = await self._http.post(
                f"{self._API_BASE}/v2/groups/{group_id}/messages",
                json=body,
                headers=self._auth_headers(),
            )
            data = resp.json()
            msg_id = str(data.get("id") or "")
            if msg_id and record_sent:
                self.record_sent_message_id(msg_id)
            return msg_id if msg_id else None
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[QQOpenPlatform] 发送群消息失败: {e}")
            return None

    async def send_message(self, user_id: str, message: str) -> Optional[str]:
        """Send private plain text (for voice_reply_service compatibility)."""
        return await self.send_private_message_segments(
            user_id, [{"type": "text", "data": {"text": message}}],
        )

    async def send_group_message(self, group_id: str, message: str) -> Optional[str]:
        """Send group plain text (legacy interface compat)."""
        return await self.send_group_message_segments(
            group_id, [{"type": "text", "data": {"text": message}}],
        )

    async def send_private_record(self, user_id: str, file_uri: str, *, reply_message_id: str = "") -> None:
        """Send a private voice -- not supported by the Open Platform; returns None so the upper layer degrades to text."""

    async def send_private_message_segments(
        self, user_id: str, segments: list[dict[str, Any]], *, record_sent: bool = True
    ) -> Optional[str]:
        """Convert OneBot segments to QQ Open Platform private format and send.

        QQ Open Platform private chat supports only plain text + images; other types
        degrade to text placeholders.
        """
        content_parts: list[str] = []
        image_url = ""

        for seg in self._expand_cq_segments(segments):
            seg_type = str(seg.get("type") or "").strip()
            data = seg.get("data") or {}
            if seg_type == "text":
                content_parts.append(str(data.get("text") or ""))
            elif seg_type == "image":
                image_url = str(data.get("file") or "")
            elif seg_type == "reply":
                content_parts.append("[回复]")
            elif seg_type == "at":
                at_qq = str(data.get("qq") or "")
                content_parts.append(f"[@{at_qq}]" if at_qq else "[@某人]")
            elif seg_type == "face":
                content_parts.append(f"[表情{data.get('id','')}]")
            elif seg_type == "record":
                content_parts.append("[语音]")
            elif seg_type == "rps":
                content_parts.append("[猜拳]")
            elif seg_type == "dice":
                content_parts.append("[骰子]")
            elif seg_type == "contact":
                content_parts.append("[推荐联系人]")
            elif seg_type == "music":
                content_parts.append("[音乐分享]")
            elif seg_type == "mface":
                content_parts.append("[动画表情]")
            elif seg_type == "file":
                content_parts.append(f"[文件 {data.get('name', '')}]")
            elif seg_type == "json":
                content_parts.append("[卡片消息]")
            else:
                pass  # ignore unknown types

        content = "".join(content_parts).strip()
        if not content and not image_url:
            return None
        if image_url and not content:
            content = "[图片]"

        await self._ensure_token()
        try:
            resp = await self._http.post(
                f"{self._API_BASE}/v2/users/{user_id}/messages",
                json={"content": content},
                headers=self._auth_headers(),
            )
            data = resp.json()
            msg_id = str(data.get("id") or "")
            if msg_id and record_sent:
                self.record_sent_message_id(msg_id)
            return msg_id if msg_id else None
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[QQOpenPlatform] 发送私聊失败: {e}")
            return None

    async def send_group_poke(self, group_id: str, user_id: str) -> Optional[str]:
        # QQ Open Platform does not support poke -- degrade to text. The result
        # propagates (None = swallowed failure); the delivery-confirmation chain
        # uses it to decide whether to clear the undelivered marker / mention.
        return await self.send_group_message_segments(
            group_id,
            [{"type": "text", "data": {"text": f" (戳了戳 {user_id})"}}],
            record_sent=False,
        )

    async def send_group_image(
        self, group_id: str, image_data: str, *, reply_message_id: str = "", at_user_id: str = "", sub_type: str = ""
    ) -> Optional[str]:
        segments: list[dict[str, Any]] = []
        if reply_message_id:
            segments.append({"type": "reply", "data": {"id": reply_message_id}})
        if at_user_id:
            segments.append({"type": "at", "data": {"qq": at_user_id}})
        segments.append({"type": "image", "data": {"file": image_data}})
        return await self.send_group_message_segments(group_id, segments, record_sent=False)

    async def send_group_record(
        self, group_id: str, file_uri: str, *, reply_message_id: str = "", at_user_id: str = ""
    ) -> None:
        """Send a group voice -- not supported by the Open Platform; returns None so the upper layer degrades to text."""

    async def send_group_ark_card(
        self, group_id: str, ark_obj: dict[str, Any], **_: Any
    ) -> bool:
        """Send a group Ark rich card (Open Platform only).

        Called by ``reply_pipeline`` when ``supports_ark_cards`` is True; OneBot
        backends degrade to text before reaching here. Card images must be pre-resolved
        to URLs in ``ark_obj``; no upload needed here.
        """
        try:
            await self._ensure_token()
            resp = await self._http.post(
                f"{self._API_BASE}/v2/groups/{group_id}/messages",
                json=ark_obj,
                headers=self._auth_headers(),
            )
            data = resp.json()
            return bool(data.get("id"))
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[QQOpenPlatform] 发送群 Ark 卡片失败: {e}")
            return False

    async def get_login_status(self) -> dict[str, Any]:
        if self._ws and self._self_id:
            return {"status": "online", "self_id": self._self_id, "nickname": self._self_nickname or None}
        return {"status": "offline", "self_id": None, "nickname": None}

    def record_sent_message_id(self, message_id: str) -> None:
        mid = str(message_id or "").strip()
        if mid:
            self._sent_message_ids[mid] = time.time()

    @property
    def onebot_url(self) -> str:
        return self._API_BASE

    @onebot_url.setter
    def onebot_url(self, value: str) -> None:
        pass  # Open Platform needs no external URL set

    async def _try_reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        delay = 1.0
        while not self._closing:
            try:
                if self.logger:
                    self.logger.info(f"[QQOpenPlatform] 尝试重连 ({delay:.0f}s)...")
                await asyncio.sleep(delay)
                if self._closing:
                    return
                # clean old connection
                if self._ws:
                    try: await self._ws.close()
                    except Exception: pass
                self._ws = None; self.ws = None
                # reconnect + handshake (prefer Resume to replay missed events)
                await self._refresh_token()
                ws_url = await self._get_gateway_url()
                self._ws = await websockets.connect(ws_url, max_size=2 ** 23)
                self.ws = self._ws
                await self._handshake(is_reconnect=True)
                if self.logger:
                    self.logger.info("[QQOpenPlatform] 重连成功")
                return  # back to _receive_loop
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"[QQOpenPlatform] 重连失败: {e}")
            delay = min(delay * 2, 60.0)  # exponential backoff, cap 60s

    # ==========================================
    # internal helpers
    # ==========================================

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"QQBot {self._access_token}",
            "Content-Type": "application/json",
        }

    async def _refresh_token(self) -> None:
        if self._http is None:
            raise RuntimeError("QQ 开放平台未连接，请先调用 connect()")
        try:
            resp = await self._http.post(self._TOKEN_URL, json={
                "appId": self._app_id,
                "clientSecret": self._client_secret,
            })
            data = resp.json()
            self._access_token = str(data.get("access_token") or "")
            expires_in = int(data.get("expires_in") or 7200)
            self._token_expires_at = time.time() + expires_in - 300  # refresh 5 min early
            if self.logger:
                self.logger.info("[QQOpenPlatform] access_token 已获取")
        except Exception as e:
            if self.logger:
                self.logger.error(f"[QQOpenPlatform] 获取 access_token 失败: {e}")
            raise

    async def _ensure_token(self) -> None:
        if time.time() >= self._token_expires_at:
            await self._refresh_token()

    async def _upload_group_image(self, group_id: str, image_url: str) -> str:
        """Upload a group image to the Open Platform; returns file_info or empty."""
        import os, mimetypes
        image_url = str(image_url or "").strip()
        if not image_url:
            return ""
        # Get local file path (file:// or a raw path).
        file_path = image_url
        if file_path.startswith("file://"):
            file_path = file_path[7:]
        if not os.path.isfile(file_path):
            if self.logger:
                self.logger.warning(f"[QQOpenPlatform] 图片文件不存在: {file_path}")
            return ""
        try:
            mime_type = mimetypes.guess_type(file_path)[0] or "image/png"
            file_size = os.path.getsize(file_path)
            # Step 1: request upload
            resp = await self._http.post(
                f"{self._API_BASE}/v2/groups/{group_id}/files",
                json={"file_type": 1, "file_name": os.path.basename(file_path),
                      "file_size": file_size, "mime_type": mime_type},
                headers=self._auth_headers(),
            )
            data = resp.json()
            upload_url = str(data.get("upload_url") or "")
            if not upload_url:
                if self.logger:
                    self.logger.warning(f"[QQOpenPlatform] 申请上传URL失败: {data}")
                return ""
            # Step 2: upload file
            with open(file_path, "rb") as f:
                upload_resp = await self._http.put(
                    upload_url,
                    content=f.read(),
                    headers={"Content-Type": mime_type},
                )
            upload_data = upload_resp.json() if upload_resp.text else {}
            file_info = str(upload_data.get("file_info") or data.get("file_info") or "")
            if file_info:
                if self.logger:
                    self.logger.info(f"[QQOpenPlatform] 图片上传成功: {file_info}")
                return file_info
            if self.logger:
                self.logger.warning(f"[QQOpenPlatform] 图片上传失败: {upload_data}")
            return ""
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[QQOpenPlatform] 图片上传异常: {e}")
            return ""

    async def _get_gateway_url(self) -> str:
        await self._ensure_token()
        resp = await self._http.get(
            f"{self._API_BASE}/gateway/bot",
            headers=self._auth_headers(),
        )
        data = resp.json()
        return str(data.get("url") or f"{self._API_BASE}/websocket")

    async def _heartbeat_loop(self) -> None:
        while not self._closing:
            if not self._ws:
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(self._heartbeat_interval)
            if self._ws:
                try:
                    await self._ws.send(json.dumps({"op": 1, "d": self._last_seq}))
                except Exception:
                    pass  # _receive_loop handles reconnection

    # ==========================================
    # event conversion
    # ==========================================

    def _convert_event(self, event_type: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
        """QQ Open Platform event -> internal unified message format."""
        author = data.get("author", {})
        # The Open Platform author has only an openid key, no username; this read
        # finds a value only if the protocol adds a key later. Missing nickname is
        # bucketed by display_name_service.
        user_nickname = str(author.get("username") or "") or None

        if event_type == "C2C_MESSAGE_CREATE":
            return {
                "message_type": "private",
                "channel": self.CHANNEL,
                "user_id": pick_actor_id(author, _C2C_ACTOR_ID_KEYS),
                "user_nickname": user_nickname,
                "content": str(data.get("content") or ""),
                "message_id": str(data.get("id") or ""),
                "timestamp": int(time.time()),
                "is_at_bot": True,
                "is_reply_to_bot": False,
                "group_id": "",
                "quoted_message_id": "",
                "mentioned_user_ids": [],
                "mentions_other_user": False,
                "mentions_all": False,
                "raw": data,
                "attachments": self._extract_attachments(data),
            }

        if event_type == "GROUP_AT_MESSAGE_CREATE":
            content = str(data.get("content") or "")
            # This channel's group identifier is itself an openid (see
            # display_name_service's note); official v2 puts it on group_openid rather
            # than group_id (bot-docs' GROUP_AT_MESSAGE_CREATE field table and example
            # JSON both have only group_openid), so the real effective branch has
            # always been this fallback. The order still cannot be reversed: when
            # group_id has a value it must keep being used, otherwise the group
            # subject_id switches keys entirely, and memory/scopes.py matches by exact
            # bytes with no aliases, so existing scoped group memory would disconnect
            # all at once. Falling back only when the original key is empty gives both
            # zero behavior change and guards against total message loss.
            group_id = str(data.get("group_id") or data.get("group_openid") or "")
            mentioned_ids: list[str] = []
            mentions_all = False
            # Check @ targets (<@!id> format in content).
            import re
            for m in re.finditer(r"<@!(\d+)>", content):
                mentioned_ids.append(m.group(1))
            # Clean text after removing <@!id> placeholders.
            clean_content = re.sub(r"<@!\d+>", "", content).strip()
            if self._self_id:
                mentions_other_user = any(mid != self._self_id for mid in mentioned_ids)
            else:
                # GROUP_AT_MESSAGE_CREATE always includes the bot mention; without
                # READY self_id, only multiple mentions prove another user was named.
                mentions_other_user = len(mentioned_ids) > 1

            return {
                "message_type": "group",
                "channel": self.CHANNEL,
                "user_id": pick_actor_id(author, _GROUP_ACTOR_ID_KEYS),
                "user_nickname": user_nickname,
                "content": clean_content,
                "message_id": str(data.get("id") or ""),
                "timestamp": int(time.time()),
                "is_at_bot": True,
                "is_reply_to_bot": False,
                "group_id": group_id,
                "quoted_message_id": "",
                "mentioned_user_ids": mentioned_ids,
                "mentions_other_user": mentions_other_user,
                "mentions_all": mentions_all,
                "raw": data,
                "attachments": self._extract_attachments(data),
            }
        return None

    @staticmethod
    def _extract_attachments(data: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []
        for att in data.get("attachments") or []:
            if isinstance(att, dict):
                url = att.get("url") or ""
                content_type = str(att.get("content_type") or "")
                if url:
                    att_type = "image" if content_type.startswith("image/") else "file"
                    attachments.append({"type": att_type, "url": url})
        return attachments

    # ==========================================
    # Stub API methods (not supported by the Open Platform; no-op returns)
    # ==========================================

    # Message operations
    async def set_msg_emoji_like(self, **kw) -> dict: return {}
    async def delete_msg(self, **kw) -> dict: return {}
    async def get_msg(self, **kw) -> dict: return {}
    async def get_forward_msg(self, **kw) -> dict: return {}
    async def send_like(self, **kw) -> dict: return {}
    async def mark_msg_as_read(self, **kw) -> dict: return {}
    async def mark_private_msg_as_read(self, **kw) -> dict: return {}
    async def mark_group_msg_as_read(self, **kw) -> dict: return {}
    async def _mark_all_as_read(self, **kw) -> dict: return {}
    async def send_group_forward_msg(self, **kw) -> dict: return {}
    async def send_private_forward_msg(self, **kw) -> dict: return {}
    async def send_forward_msg(self, **kw) -> dict: return {}
    async def forward_friend_single_msg(self, **kw) -> dict: return {}
    async def forward_group_single_msg(self, **kw) -> dict: return {}
    async def get_friend_msg_history(self, **kw) -> dict: return {}
    async def get_group_msg_history(self, **kw) -> dict: return {}

    # Friend operations
    async def set_friend_add_request(self, **kw) -> dict: return {}
    async def delete_friend(self, **kw) -> dict: return {}
    async def get_friends_with_category(self, **kw) -> dict: return {}
    async def friend_poke(self, **kw) -> dict: return {}
    async def get_profile_like(self, **kw) -> dict: return {}

    # Group operations
    async def set_group_kick(self, **kw) -> dict: return {}
    async def set_group_ban(self, **kw) -> dict: return {}
    async def set_group_whole_ban(self, **kw) -> dict: return {}
    async def set_group_admin(self, **kw) -> dict: return {}
    async def set_group_card(self, **kw) -> dict: return {}
    async def set_group_name(self, **kw) -> dict: return {}
    async def set_group_leave(self, **kw) -> dict: return {}
    async def set_group_special_title(self, **kw) -> dict: return {}
    async def set_group_add_request(self, **kw) -> dict: return {}
    async def set_group_sign(self, **kw) -> dict: return {}
    async def send_group_sign(self, **kw) -> dict: return {}
    async def set_group_portrait(self, **kw) -> dict: return {}
    async def get_group_at_all_remain(self, **kw) -> dict: return {}
    async def get_group_ignore_add_request(self, **kw) -> dict: return {}
    async def get_group_system_msg(self, **kw) -> dict: return {}
    async def _send_group_notice(self, **kw) -> dict: return {}
    async def _get_group_notice(self, **kw) -> dict: return {}
    async def _del_group_notice(self, **kw) -> dict: return {}
    async def group_poke(self, **kw) -> dict: return {}
    async def send_group_ai_record(self, **kw) -> dict: return {}

    # Group file operations
    async def upload_group_file(self, **kw) -> dict: return {}
    async def delete_group_file(self, **kw) -> dict: return {}
    async def create_group_file_folder(self, **kw) -> dict: return {}
    async def delete_group_folder(self, **kw) -> dict: return {}
    async def get_group_file_system_info(self, **kw) -> dict: return {}
    async def get_group_root_files(self, **kw) -> dict: return {}
    async def get_group_files_by_folder(self, **kw) -> dict: return {}
    async def get_group_file_url(self, **kw) -> dict: return {}

    # Info queries
    async def get_stranger_info(self, **kw) -> dict: return {}
    async def get_group_info(self, **kw) -> dict: return {}
    async def get_group_member_info(self, **kw) -> dict: return {}
    async def get_group_member_list(self, **kw) -> list: return []
    async def get_group_honor_info(self, **kw) -> dict: return {}
    async def get_group_shut_list(self, **kw) -> list: return []
    async def get_group_info_ex(self, **kw) -> dict: return {}
    async def get_essence_msg_list(self, **kw) -> dict: return {}
    async def set_essence_msg(self, **kw) -> dict: return {}
    async def delete_essence_msg(self, **kw) -> dict: return {}

    # Credentials / cookies
    async def get_cookies(self, **kw) -> dict: return {}
    async def get_csrf_token(self, **kw) -> dict: return {}
    async def get_credentials(self, **kw) -> dict: return {}

    # Image / file
    async def get_image(self, **kw) -> dict: return {}
    async def upload_private_file(self, **kw) -> dict: return {}
    async def download_file(self, **kw) -> dict: return {}
    async def get_file(self, **kw) -> dict: return {}

    # Status
    async def can_send_image(self) -> bool: return True
    async def can_send_record(self) -> bool: return False
    async def get_status(self) -> dict: return {"online": self._ws is not None}
