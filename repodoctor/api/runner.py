"""Run long operations (a whole debugging session) off the request thread.

Tasks for ONE session run strictly in order: a task submitted while an earlier one is still finishing is queued
behind it. (Without this, an approval arriving in the milliseconds after the session reaches WAITING_APPROVAL,
but before the worker that got it there has returned, would be refused after the approval was already recorded.)
Different sessions run in parallel, up to max_workers.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger("repodoctor.api")

Task = tuple[Callable[..., Any], tuple[Any, ...]]


class BackgroundRunner:
    def __init__(
        self, max_workers: int, on_error: Callable[[str, BaseException], None]
    ) -> None:
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="rd-session"
        )
        self._active: set[str] = set()
        self._queued: dict[str, deque[Task]] = {}
        self._lock = threading.Lock()
        self._on_error = on_error

    def is_running(self, sid: str) -> bool:
        with self._lock:
            return sid in self._active

    def submit(self, sid: str, fn: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            if sid in self._active:
                self._queued.setdefault(sid, deque()).append((fn, args))
                return
            self._active.add(sid)
        self._pool.submit(self._drain, sid, (fn, args))

    def _drain(self, sid: str, task: Task | None) -> None:
        while task is not None:
            self._run_one(sid, *task)
            with self._lock:
                queue = self._queued.get(sid)
                if queue:
                    task = queue.popleft()
                else:
                    self._queued.pop(sid, None)
                    self._active.discard(sid)
                    task = None

    def _run_one(self, sid: str, fn: Callable[..., Any], args: tuple[Any, ...]) -> None:
        try:
            fn(*args)
        except BaseException as exc:  # noqa: BLE001 - nothing may kill the worker silently
            logger.exception("background_error session=%s", sid)
            try:
                self._on_error(sid, exc)
            except Exception:  # noqa: BLE001
                logger.exception("background_error_handler_failed session=%s", sid)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
