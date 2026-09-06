"""QQ connection abstract base -- unifies NapCat (OneBot) and the QQ Open Platform."""

from __future__ import annotations

import asyncio
import collections
from abc import ABC, abstractmethod
from typing import Any, Optional


class QQConnectionBase(ABC):
    """Abstract base for every QQ access method.

    Both NapCat (OneBot) and the QQ Open Platform implement this interface and
    output a unified internal message format to the upper layers
    (message_dispatcher, pipeline, ...).
    """

    # Internal message format fields (every subclass receive_message() must
    # return this shape):
    # {
    #     "message_type": "group" | "private",
    #     "user_id": str,
    #     "user_nickname": str | None,
    #     "content": str,
    #     "message_id": str,
    #     "timestamp": int,
    #     "is_at_bot": bool,
    #     "is_reply_to_bot": bool,
    #     "group_id": str,             # group only
    #     "quoted_message_id": str,
    #     "mentioned_user_ids": [str],
    #     "mentions_other_user": bool,
    #     "mentions_all": bool,
    #     "raw": dict,
    #     "attachments": [dict],
    # }

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection (WebSocket + auth + heartbeat)."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect and clean up resources."""
        ...

    @abstractmethod
    async def receive_message(self, timeout: float = 1.0) -> Optional[dict[str, Any]]:
        """Blocking-receive one message; returns the normalized dict or None on timeout."""
        ...

    @abstractmethod
    async def send_group_message_segments(
        self, group_id: str, segments: list[dict[str, Any]], *, record_sent: bool = True
    ) -> Optional[str]:
        """Send a group message (platform-native format); returns the message_id."""
        ...

    @abstractmethod
    async def send_private_message_segments(
        self, user_id: str, segments: list[dict[str, Any]]
    ) -> Optional[str]:
        """Send a private message (platform-native format); returns the message_id."""
        ...

    @abstractmethod
    async def send_group_poke(self, group_id: str, user_id: str) -> bool:
        """Send a group poke; returns whether it succeeded."""
        ...

    @abstractmethod
    async def send_group_image(
        self, group_id: str, image_data: str, *, reply_message_id: str = "", at_user_id: str = "", sub_type: str = ""
    ) -> Optional[str]:
        """Send a group image."""
        ...

    @abstractmethod
    async def send_group_record(
        self, group_id: str, file_uri: str, *, reply_message_id: str = "", at_user_id: str = ""
    ) -> Optional[str]:
        """Send a group voice message."""
        ...

    @abstractmethod
    async def get_login_status(self) -> dict[str, Any]:
        """Return login status: {"status": "online"|"offline", "self_id": str|None, "nickname": str|None}"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the connection is established."""
        ...

    @abstractmethod
    def record_sent_message_id(self, message_id: str) -> None:
        """Record a sent message id (used by is_reply_to_bot detection)."""
        ...

    token: str = ""  # access token (for settings_service direct attribute access)

    @property
    def needs_attention(self) -> bool:
        """Whether the attention mechanism is needed (NapCat yes, Open Platform no)."""
        return True

    @property
    def supports_voice(self) -> bool:
        """Whether voice replies are supported."""
        return True

    @property
    def supports_poke(self) -> bool:
        """Whether poke is supported."""
        return True

    @property
    def receives_all_messages(self) -> bool:
        """Whether all group messages are received (Open Platform only gets @bot)."""
        return True

    @property
    def supports_ark_cards(self) -> bool:
        """Whether Ark rich cards are supported (Open Platform only; OneBot degrades to text)."""
        return False

    def is_group_muted(self, group_id: str) -> bool:
        """Check whether the bot is muted in this group (incl. whole-group mute).

        NapCat tracks mute state via OneBot notice events; the Open Platform does
        not track it and returns False by default.
        """
        return False

    @property
    def self_id(self) -> str:
        """The bot's own user id ("" when unknown). Public alias for ``_self_id``."""
        return str(getattr(self, "_self_id", "") or "")

    @property
    def sent_message_ids(self) -> dict[str, float]:
        """Sent message id -> sent timestamp. Public alias for ``_sent_message_ids``."""
        return getattr(self, "_sent_message_ids", {})

    async def send_group_ark_card(
        self, group_id: str, ark_obj: dict[str, Any], **_: Any
    ) -> bool:
        """Send a group Ark rich card. Open Platform only (``supports_ark_cards`` True);
        OneBot backends degrade to text before reaching this method, so it raises
        NotImplementedError as a fallback."""
        raise NotImplementedError("Ark rich cards are supported only on the Open Platform channel")

    # ── inbound broadcast hook (adapter -> subscribers) ────────
    # Any plugin may attach a sink via ``set_inbound_sink``: the connection layer
    # calls it once per normalized inbound message. qq_auto_reply uses it to push
    # inbound QQ messages to other plugins; a plugin that owns its own connection
    # can also attach its own sink. Never blocks the message pipeline (best-effort).
    _INBOUND_SINK_ATTR = "_inbound_sink"
    #: 入站 sink 任务集上限;满了 cancel + 丢弃最旧(与 SSE 通道的 drop-oldest 一致)。
    #: 否则一个慢/永不完成的 sink 会让未完成任务集按消息量无限增长,拖垮连接器进程。
    _INBOUND_SINK_MAX_BACKLOG = 100

    @property
    def inbound_sink(self) -> Any | None:
        """The registered inbound sink (None = not registered)."""
        return getattr(self, self._INBOUND_SINK_ATTR, None)

    def set_inbound_sink(self, sink: Any | None) -> None:
        """Register an inbound sink ``async sink(message: dict) -> None``.

        After each ``receive_message()`` yields a normalized message, the connection
        layer calls it and swallows every exception (the broadcast is best-effort and
        must never stall the pipeline). Pass ``None`` to unregister.
        """
        setattr(self, self._INBOUND_SINK_ATTR, sink)

    async def _dispatch_inbound(self, message: dict[str, Any]) -> None:
        """Internal: hand one inbound message to the registered sink.

        Fire-and-forget and fault-tolerant: the sink runs on an independent task,
        so a slow or never-completing subscriber can never stall the receive loop.
        Every exception is swallowed (the broadcast is best-effort).
        """
        sink = self.inbound_sink
        if sink is None:
            return
        try:
            self._spawn_inbound_sink(sink, message)
        except Exception:
            pass

    def _spawn_inbound_sink(self, sink: Any, message: dict[str, Any]) -> None:
        """Schedule ``_run_inbound_sink`` on a background task, keeping a strong ref.

        ``create_task`` must run inside a live event loop (``receive_message`` is
        always awaited on one); without a reference the task could be collected
        before it runs, so we track it in a FIFO deque and discard it on completion
        (or when it is dropped as the oldest occupant of the bounded backlog).
        """
        task = asyncio.create_task(self._run_inbound_sink(sink, message))
        tasks = getattr(self, "_inbound_sink_tasks", None)
        if tasks is None:
            tasks = collections.deque()
            setattr(self, "_inbound_sink_tasks", tasks)
        # 有界 backlog:满了就 cancel + 丢弃最旧(队首)未完成任务。这是 best-effort
        # 广播,压力下丢旧保新(与 SSE 通道的 drop-oldest 语义一致);否则慢/永不完成
        # 的 sink 会让任务集按消息量无限增长,最终拖垮连接器进程。
        while len(tasks) >= self._INBOUND_SINK_MAX_BACKLOG:
            tasks.popleft().cancel()
        tasks.append(task)
        task.add_done_callback(self._discard_inbound_sink_task)

    def _discard_inbound_sink_task(self, task: asyncio.Task) -> None:
        """Drop a finished (or already-dropped) sink task from the tracked backlog."""
        tasks = getattr(self, "_inbound_sink_tasks", None)
        if tasks is None:
            return
        try:
            tasks.remove(task)
        except ValueError:
            # 已在 drop-oldest 时从队首清除;cancel 后 done_callback 又触发一次,忽略。
            pass

    def _cancel_inbound_sink_tasks(self) -> None:
        """Cancel lingering inbound-sink tasks (called from each ``disconnect()``).

        Drop-oldest bounds the backlog, but tasks still running when the
        connection tears down would otherwise linger until their sink finishes;
        cancel them so nothing outlives the connection.
        """
        tasks = getattr(self, "_inbound_sink_tasks", None)
        if not tasks:
            return
        for task in list(tasks):
            task.cancel()
        tasks.clear()

    async def _run_inbound_sink(self, sink: Any, message: dict[str, Any]) -> None:
        """Run one sink call; swallow any failure so the broadcast never raises."""
        try:
            result = sink(message)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger = getattr(self, "logger", None)
            if logger:
                logger.exception("Inbound sink failed (swallowed; broadcast is best-effort)")

    @property
    @abstractmethod
    def onebot_url(self) -> str:
        """Reverse WebSocket listen address (NapCat connects here as a WS client)."""
        ...

    @onebot_url.setter
    @abstractmethod
    def onebot_url(self, value: str) -> None:
        ...
