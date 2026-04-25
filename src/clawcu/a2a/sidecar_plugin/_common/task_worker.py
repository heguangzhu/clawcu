"""Thread-pool worker that runs async A2A tasks to terminal state.

One :class:`TaskWorker` is spun up per sidecar, closed over:

- a :class:`_common.task_store.TaskStore` for state persistence
- a ``run_fn`` that actually does the work (for OpenClaw: wrap
  ``post_chat_completion``; for Hermes: wrap the hermes equivalent)
- a logger for audit trails

The handler for ``/a2a/send?mode=async`` creates a snapshot then calls
:meth:`submit`. The worker picks it up off its pool, transitions the
task to ``working``, runs ``run_fn``, and writes the terminal snapshot.

Cancel is cooperative: the worker checks ``store.is_canceled`` before
writing the completed snapshot. If the snapshot has already flipped
to ``canceled`` (via ``POST /cancel``), the worker discards its result
rather than overwriting terminal state.
"""
from __future__ import annotations

import inspect
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from _common.task_store import (
    STATE_CANCELED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_WORKING,
    TaskError,
    TaskStore,
)


RunResult = Dict[str, Any]
RunFn = Callable[..., RunResult]
"""``run_fn(snapshot, *, progress=cb)`` → ``{"reply": str, "thread_id": Optional[str]}``.

Called on a worker thread. Expected to raise on failure; the worker
catches and marks the task ``failed``. Receives a **copy** of the
current snapshot so it can read ``input.message`` / ``thread_id`` /
``peer`` without racing the store.

If ``run_fn`` declares a ``progress`` parameter (or ``**kwargs``), the
worker passes a ``progress(message: str)`` callback so the function
can emit breadcrumbs at meaningful boundaries ("calling hermes",
"received reply"). Functions that don't declare it are still called
positionally — backward compatible.
"""


class TaskWorker:
    def __init__(
        self,
        *,
        store: TaskStore,
        run_fn: RunFn,
        logger: Any,
        self_name: str,
        max_workers: int = 4,
        heartbeat_s: float = 15.0,
    ) -> None:
        self.store = store
        self.run_fn = run_fn
        self.logger = logger
        self.self_name = self_name
        self.heartbeat_s = max(0.0, float(heartbeat_s))
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="a2a-task"
        )
        # Detect once whether run_fn accepts a ``progress`` kwarg so we
        # don't re-introspect on every task. Built-ins / C functions where
        # ``inspect.signature`` blows up fall back to positional-only.
        self._run_fn_accepts_progress = _accepts_progress_kwarg(run_fn)

    def submit(self, *, peer: str, task_id: str) -> None:
        """Queue ``task_id`` for execution. Non-blocking."""
        self._executor.submit(self._run, peer, task_id)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait)

    # ---- internals ------------------------------------------------------

    def _run(self, peer: str, task_id: str) -> None:
        snapshot = self.store.get(peer=peer, task_id=task_id)
        if snapshot is None:
            self.logger.warn(
                f"[sidecar:{self.self_name}] task disappeared before start "
                f"peer={peer} task_id={task_id}"
            )
            return
        if snapshot.get("state") == STATE_CANCELED:
            # Cancelled before we got a chance; nothing to do.
            return
        try:
            self.store.transition(peer=peer, task_id=task_id, to_state=STATE_WORKING)
        except TaskError as exc:
            # Raced with cancel or deadline sweep; bail.
            self.logger.info(
                f"[sidecar:{self.self_name}] task could not start "
                f"peer={peer} task_id={task_id} reason={exc}"
            )
            return

        # Heartbeat: while run_fn is running, periodically write a
        # progress event so pollers see activity rather than a stale
        # ``working`` state. Stop signals via ``stop_event``.
        stop_event = threading.Event()
        heartbeat_thread: Optional[threading.Thread] = None
        if self.heartbeat_s > 0:
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(peer, task_id, stop_event),
                daemon=True,
                name=f"a2a-task-hb-{task_id[:8]}",
            )
            heartbeat_thread.start()

        def progress_cb(message: str) -> None:
            try:
                self.store.progress(peer=peer, task_id=task_id, message=message)
            except Exception:  # noqa: BLE001
                # Progress is best-effort — never fail the task on a
                # heartbeat write hiccup.
                pass

        try:
            try:
                if self._run_fn_accepts_progress:
                    result = self.run_fn(snapshot, progress=progress_cb)
                else:
                    result = self.run_fn(snapshot)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    f"[sidecar:{self.self_name}] task failed peer={peer} task_id={task_id}: {exc}\n"
                    + (traceback.format_exc() or "")
                )
                self._safe_terminate(
                    peer=peer,
                    task_id=task_id,
                    to_state=STATE_FAILED,
                    error={"message": str(exc), "http_status": 502},
                )
                return

            if self.store.is_canceled(peer=peer, task_id=task_id):
                self.logger.info(
                    f"[sidecar:{self.self_name}] task canceled after completion "
                    f"peer={peer} task_id={task_id}"
                )
                return

            self._safe_terminate(
                peer=peer,
                task_id=task_id,
                to_state=STATE_COMPLETED,
                result=result,
            )
        finally:
            stop_event.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)

    def _heartbeat_loop(
        self,
        peer: str,
        task_id: str,
        stop_event: threading.Event,
    ) -> None:
        # ``Event.wait`` returns True when set, False on timeout — loop
        # until the worker signals completion.
        while not stop_event.wait(self.heartbeat_s):
            try:
                self.store.progress(peer=peer, task_id=task_id)
            except Exception:  # noqa: BLE001
                pass

    def _safe_terminate(
        self,
        *,
        peer: str,
        task_id: str,
        to_state: str,
        result: Optional[RunResult] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            self.store.transition(
                peer=peer,
                task_id=task_id,
                to_state=to_state,
                result=result,
                error=error,
            )
        except TaskError as exc:
            # Terminal collision (raced with cancel): log and drop.
            self.logger.info(
                f"[sidecar:{self.self_name}] task terminal write skipped "
                f"peer={peer} task_id={task_id} reason={exc}"
            )


def _accepts_progress_kwarg(run_fn: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(run_fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.name == "progress":
            return True
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


__all__ = ["TaskWorker", "RunFn", "RunResult"]
