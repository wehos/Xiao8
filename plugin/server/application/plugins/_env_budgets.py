"""One parser for the seconds-valued environment overrides in the plugin server.

These are read at import time, so anything they raise stops the server from
starting — which is the opposite of what a timeout override should be able to
do. Every budget in this package goes through :func:`env_seconds`.
"""

from __future__ import annotations

import math
import os

__all__ = ["env_int", "env_seconds"]


def env_seconds(name: str, default: float, *, minimum: float = 1.0) -> float:
    """Read a seconds-valued override, falling back on anything unusable.

    Rejects, and logs, three shapes that would each break a budget:

    * unparseable (``20s``) — would raise ``ValueError`` during import;
    * non-finite (``inf``, or a value that overflows to it) — ``max()`` keeps it
      infinite, and an infinite timeout means the budget has no deadline at all,
      which is worse than the default because it looks configured;
    * below ``minimum`` — clamped rather than rejected.

    Lives in its own module because five call sites had a copy each: the first
    person to change the semantics here would have silently left four budgets
    behaving differently, and inconsistent timeouts are very hard to reason
    back to from the symptom.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _warn(name, raw, default)
        return default
    if not math.isfinite(value):
        _warn(name, raw, default)
        return default
    return max(minimum, value)


def env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a count-valued override, falling back on anything unusable.

    Same contract as :func:`env_seconds`, for the overrides that count things
    (workers, concurrent interpreters) rather than measure time. Kept beside it
    so the two never drift into disagreeing about what a bad value means.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _warn(name, raw, default)
        return default
    return max(minimum, value)


def _warn(name: str, raw: str, default: float | int) -> None:
    # 懒导入：metadata_scanner 会被每个扫描 worker 子进程导入，而它的模块级导入
    # 面正是单次扫描里最贵的一段。为一条几乎走不到的 warning 在模块级拉进日志栈，
    # 等于给每次扫描都加钱。
    from plugin.logging_config import get_logger

    get_logger("server.application.plugins.env").warning(
        "ignoring unusable {}={!r}; using {}", name, raw, default
    )
