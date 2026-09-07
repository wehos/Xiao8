"""Bounded off-loop delivery to the application's existing logging backend."""

from concurrent.futures import Future
from queue import Full, Queue
from threading import Lock, Thread

from utils.logger_config import get_module_logger


logger = get_module_logger("asr_diagnostics", "Main")
_QUEUE: Queue = Queue(maxsize=32)
_START_LOCK = Lock()
_worker: Thread | None = None


def _write_records() -> None:
    while True:
        future, metadata, kind = _QUEUE.get()
        try:
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if kind == "pipeline":
                    for record in metadata:
                        logger.info("ASR resolution %s", record)
                elif kind == "incident":
                    logger.warning("ASR incident %s", metadata)
                elif kind == "cleanup":
                    logger.info("ASR cleanup %s", metadata)
                else:
                    logger.info("ASR resolution %s", metadata)
            except Exception as error:
                future.set_exception(error)
            else:
                future.set_result(None)
        finally:
            _QUEUE.task_done()


def submit_resolution_log(metadata: dict | tuple[dict, ...], *, kind: str = "resolution") -> Future | None:
    """At most one writer and 32 queued records, across resets and runtimes.

    A stalled filesystem may occupy this single daemon, but cannot grow the
    default executor, hold process exit, or block the audio event loop. No
    handler or alternate log file is installed here.
    """
    if kind not in {"resolution", "incident", "cleanup", "pipeline"}:
        raise ValueError("unknown ASR diagnostic record kind")
    if kind == "pipeline" and (type(metadata) is not tuple or not 1 <= len(metadata) <= 16):
        raise ValueError("pipeline diagnostic batch must contain 1..16 records")
    global _worker
    with _START_LOCK:
        if _worker is None:
            _worker = Thread(target=_write_records, name="asr-resolution-writer", daemon=True)
            _worker.start()
    future: Future = Future()
    try:
        _QUEUE.put_nowait((future, metadata, kind))
    except Full:
        return None
    return future
