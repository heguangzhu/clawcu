"""Server-Sent Events writer for A2A task streams (Phase 1, stdlib only).

Handlers call :func:`stream_task_events` inside a ``do_GET`` for
``/a2a/tasks/:id/events``; the helper owns the full response lifecycle
(response headers, replay of historical events, heartbeat loop, terminal
frame, socket close).

Framing
-------
- ``id: <int>``                  — 0-based line index in events.jsonl
- ``event: status|end|heartbeat``
- ``data: <json>``               — one line (events are small; wrap-free)
- blank line separator

A single ``id:`` keyed on the events-file index lets clients resume via
``Last-Event-ID`` — on reconnect the handler replays events with index
strictly greater than the supplied ID before entering the live loop.

Why hand-written instead of a framework:
  Both sidecars run ``http.server`` only; pulling Starlette/FastAPI in
  just for SSE would drag an async runtime into a stdlib-only codebase.
  The writer is ~80 lines and doesn't need backpressure or multi-client
  fan-out — each handler thread owns exactly one connection.
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, Optional

from _common.task_store import TERMINAL_STATES, TaskStore


def _write_frame(
    handler: BaseHTTPRequestHandler,
    *,
    event: str,
    data: Any,
    event_id: Optional[int] = None,
) -> bool:
    """Write one SSE frame. Returns ``False`` if the client has gone away.

    Matched to ``text/event-stream`` semantics: lines terminated with
    ``\\n``, frames separated by a blank line. ``data`` is one-line JSON.
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    lines.append("")
    lines.append("")
    payload = ("\n".join(lines)).encode("utf-8")
    try:
        handler.wfile.write(payload)
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def parse_last_event_id(headers: Any) -> int:
    """Read ``Last-Event-ID`` as an int; returns ``-1`` on any failure."""
    raw = None
    if hasattr(headers, "get"):
        raw = headers.get("Last-Event-ID") or headers.get("last-event-id")
    if raw is None:
        return -1
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return -1
    return v if v >= 0 else -1


def stream_task_events(
    handler: BaseHTTPRequestHandler,
    *,
    store: TaskStore,
    peer: str,
    task_id: str,
    heartbeat_s: float = 15.0,
    idle_timeout_s: float = 60.0,
) -> None:
    """Own the full SSE response for ``/a2a/tasks/:task_id/events``.

    Life cycle:
      1. Write ``200 OK`` + ``text/event-stream`` headers.
      2. Replay any existing events after ``Last-Event-ID`` (or all, if none).
      3. If the task is already terminal, emit the ``end`` frame and close.
      4. Otherwise enter the live loop: wait on the store's Condition;
         on wake, flush new events. Emit heartbeat every ``heartbeat_s``.
      5. On terminal state transition, emit ``end`` and close.

    ``idle_timeout_s`` bounds the handler's lifetime so a wedged task
    doesn't keep a socket open forever — the default is ``2×heartbeat``,
    override via caller for tests.
    """
    after_index = parse_last_event_id(handler.headers)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
    handler.end_headers()

    # 1. Replay historical events.
    historical = store.load_events(peer=peer, task_id=task_id, after_index=after_index)
    last_index = after_index
    for event in historical:
        idx = event.get("_index", last_index + 1)
        if not _write_frame(handler, event="status", data=_strip_meta(event), event_id=idx):
            return
        last_index = idx

    # If task is already terminal, we're done.
    snapshot = store.get(peer=peer, task_id=task_id)
    if snapshot and snapshot.get("state") in TERMINAL_STATES:
        _write_frame(handler, event="end", data={}, event_id=None)
        return

    # 2. Live loop on the store's Condition.
    cond = store.condition_for(task_id)
    last_beat = time.monotonic()
    deadline = time.monotonic() + idle_timeout_s
    while True:
        now = time.monotonic()
        if now >= deadline:
            _write_frame(handler, event="end", data={"reason": "idle-timeout"}, event_id=None)
            return
        wait_for = min(heartbeat_s, deadline - now)
        with cond:
            cond.wait(timeout=wait_for)
        # Drain any new events.
        new_events = store.load_events(
            peer=peer, task_id=task_id, after_index=last_index
        )
        for event in new_events:
            idx = event.get("_index", last_index + 1)
            if not _write_frame(handler, event="status", data=_strip_meta(event), event_id=idx):
                return
            last_index = idx
            deadline = time.monotonic() + idle_timeout_s
        # Check terminal after draining.
        snapshot = store.get(peer=peer, task_id=task_id)
        if snapshot and snapshot.get("state") in TERMINAL_STATES:
            _write_frame(handler, event="end", data={}, event_id=None)
            return
        # Heartbeat if no event in the last interval.
        if time.monotonic() - last_beat >= heartbeat_s and not new_events:
            if not _write_frame(
                handler, event="heartbeat", data={"ts": time.time()}, event_id=None
            ):
                return
            last_beat = time.monotonic()


def _strip_meta(event: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``event`` without internal bookkeeping keys."""
    return {k: v for k, v in event.items() if not k.startswith("_")}


__all__ = ["parse_last_event_id", "stream_task_events"]
