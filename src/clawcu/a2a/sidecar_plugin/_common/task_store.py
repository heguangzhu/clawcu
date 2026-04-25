"""A2A task-model persistence (Phase 1).

Per-peer task snapshots + append-only events JSONL at
``<storage_dir>/<peer>/<task_id>{.json,.events.jsonl}``. Enabled only when
``storage_dir`` is truthy; otherwise all mutations are no-ops. Peer and task
ids must match :func:`_common.thread.safe_id` to prevent path traversal.

Shape mirrors ``_common.thread.ThreadStore``: dataclass-free, dict-shaped
records written via ``json.dumps`` one line / one file at a time. The only
delta is that snapshots are overwrite-atomic (write temp + rename) because
readers poll them from ``GET /a2a/tasks/:id``, while events are append-only
for SSE replay.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from _common.thread import safe_id

STATE_SUBMITTED = "submitted"
STATE_WORKING = "working"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELED = "canceled"

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELED})
VALID_STATES = frozenset(
    {STATE_SUBMITTED, STATE_WORKING, STATE_COMPLETED, STATE_FAILED, STATE_CANCELED}
)

_VALID_TRANSITIONS = {
    STATE_SUBMITTED: {STATE_WORKING, STATE_CANCELED, STATE_FAILED},
    STATE_WORKING: {STATE_COMPLETED, STATE_FAILED, STATE_CANCELED},
}


def mint_task_id() -> str:
    """``task_`` + 32 hex chars (uuid4). Fits inside :func:`safe_id`'s regex."""
    return f"task_{uuid.uuid4().hex}"


def _default_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deadline_from_now(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


class TaskError(Exception):
    """Raised for bad task_id / peer, illegal state transitions, etc."""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


class TaskStore:
    """Snapshot + event persistence for async A2A tasks.

    Callers pass ``peer`` + ``task_id`` as plain strings; the store validates
    them with :func:`safe_id` on every call and raises :class:`TaskError` on
    rejection. Disabled stores (empty ``storage_dir``) return ``None`` from
    all read paths and raise on all write paths — async mode simply
    shouldn't be advertised when storage is off.
    """

    def __init__(
        self,
        storage_dir: str = "",
        *,
        default_deadline_s: int = 86400,
        retain_s: int = 86400,
        now_fn: Callable[[], str] = _default_now,
    ) -> None:
        self.storage_dir = storage_dir or ""
        self.enabled = bool(self.storage_dir)
        self.default_deadline_s = default_deadline_s
        self.retain_s = retain_s
        self.now_fn = now_fn
        # One lock per task_id, guards both snapshot write and events append.
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # In-memory pub/sub for SSE subscribers. Keyed by task_id.
        self._conditions: Dict[str, threading.Condition] = {}
        self._conditions_guard = threading.Lock()

    # ---- path / lock helpers --------------------------------------------

    def _paths(self, peer: str, task_id: str) -> Tuple[str, str, str]:
        p = safe_id(peer)
        t = safe_id(task_id)
        if not p or not t:
            raise TaskError(f"invalid peer/task_id: {peer!r}/{task_id!r}", http_status=400)
        directory = os.path.join(self.storage_dir, p)
        snapshot = os.path.join(directory, f"{t}.json")
        events = os.path.join(directory, f"{t}.events.jsonl")
        return directory, snapshot, events

    def _lock_for(self, task_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(task_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[task_id] = lock
            return lock

    def _condition_for(self, task_id: str) -> threading.Condition:
        with self._conditions_guard:
            cond = self._conditions.get(task_id)
            if cond is None:
                cond = threading.Condition()
                self._conditions[task_id] = cond
            return cond

    # ---- public API -----------------------------------------------------

    def create(
        self,
        *,
        peer: str,
        task_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        message: str,
        request_id: Optional[str] = None,
        deadline_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a fresh ``submitted`` task, persist it, return the snapshot."""
        if not self.enabled:
            raise TaskError("task store disabled", http_status=503)
        tid = task_id or mint_task_id()
        directory, snapshot_path, _ = self._paths(peer, tid)
        now = self.now_fn()
        deadline = _deadline_from_now(deadline_s or self.default_deadline_s)
        snapshot: Dict[str, Any] = {
            "task_id": tid,
            "peer": peer,
            "thread_id": thread_id,
            "state": STATE_SUBMITTED,
            "created_at": now,
            "updated_at": now,
            "input": {"message": message, "from": peer},
            "result": None,
            "error": None,
            "deadline_at": deadline,
            "request_ids": [request_id] if request_id else [],
        }
        os.makedirs(directory, exist_ok=True)
        with self._lock_for(tid):
            self._write_snapshot(snapshot_path, snapshot)
            self._append_event(peer, tid, {"state": STATE_SUBMITTED, "ts": now})
        return snapshot

    def get(self, *, peer: str, task_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        try:
            _, snapshot_path, _ = self._paths(peer, task_id)
        except TaskError:
            return None
        try:
            with open(snapshot_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def transition(
        self,
        *,
        peer: str,
        task_id: str,
        to_state: str,
        result: Any = None,
        error: Any = None,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move a task to ``to_state`` + persist snapshot + emit event.

        Illegal transitions raise :class:`TaskError`. Terminal → anything is
        rejected (a ``canceled`` task stays canceled even if the worker later
        produces a result). ``message`` is an optional human-readable progress
        hint emitted as part of the event frame; snapshots don't store it.
        """
        if not self.enabled:
            raise TaskError("task store disabled", http_status=503)
        if to_state not in VALID_STATES:
            raise TaskError(f"unknown target state: {to_state}", http_status=400)
        _, snapshot_path, _ = self._paths(peer, task_id)
        with self._lock_for(task_id):
            try:
                with open(snapshot_path, "r", encoding="utf-8") as fh:
                    snapshot = json.loads(fh.read())
            except FileNotFoundError:
                raise TaskError("task not found", http_status=404)
            current = snapshot.get("state")
            if current in TERMINAL_STATES:
                # Idempotent: repeated terminal writes are no-ops.
                if current == to_state:
                    return snapshot
                raise TaskError(
                    f"task already terminal ({current}); cannot transition to {to_state}",
                    http_status=409,
                )
            allowed = _VALID_TRANSITIONS.get(current, set())
            if to_state != current and to_state not in allowed:
                raise TaskError(
                    f"illegal transition {current} → {to_state}", http_status=409
                )
            now = self.now_fn()
            snapshot["state"] = to_state
            snapshot["updated_at"] = now
            if result is not None:
                snapshot["result"] = result
            if error is not None:
                snapshot["error"] = error
            self._write_snapshot(snapshot_path, snapshot)
            event: Dict[str, Any] = {"state": to_state, "ts": now}
            if message:
                event["message"] = message
            if to_state == STATE_COMPLETED and result is not None:
                event["result"] = result
            if to_state == STATE_FAILED and error is not None:
                event["error"] = error
            self._append_event(peer, task_id, event)
        # Notify SSE subscribers outside the file lock.
        cond = self._condition_for(task_id)
        with cond:
            cond.notify_all()
        return snapshot

    def progress(
        self,
        *,
        peer: str,
        task_id: str,
        message: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Heartbeat / breadcrumb on an in-flight task.

        Bumps ``updated_at`` and ``last_progress_at`` on the snapshot,
        appends a ``progress`` event, and notifies SSE subscribers — but
        does NOT change ``state``. No-op (returns the current snapshot or
        ``None``) if the task is missing or already terminal; that lets
        the worker's heartbeat thread race safely against cancel.

        Use cases:
          - Periodic heartbeat from the worker so pollers can see the
            task is alive (``message=None``).
          - Per-stage breadcrumb from ``run_fn`` (``message="calling
            hermes"``) so the user gets a meaningful poll response.
        """
        if not self.enabled:
            return None
        try:
            _, snapshot_path, _ = self._paths(peer, task_id)
        except TaskError:
            return None
        text = None
        if message is not None:
            # Cap to keep a runaway peer from bloating the snapshot.
            text = str(message)[:200] or None
        with self._lock_for(task_id):
            try:
                with open(snapshot_path, "r", encoding="utf-8") as fh:
                    snapshot = json.loads(fh.read())
            except FileNotFoundError:
                return None
            if snapshot.get("state") in TERMINAL_STATES:
                return snapshot
            now = self.now_fn()
            snapshot["updated_at"] = now
            snapshot["last_progress_at"] = now
            if text:
                snapshot["last_progress_message"] = text
            self._write_snapshot(snapshot_path, snapshot)
            event: Dict[str, Any] = {"event": "progress", "ts": now}
            if text:
                event["message"] = text
            self._append_event(peer, task_id, event)
        cond = self._condition_for(task_id)
        with cond:
            cond.notify_all()
        return snapshot

    def request_cancel(self, *, peer: str, task_id: str) -> Dict[str, Any]:
        """Mark a task as cancel-requested and transition to ``canceled``.

        Phase 1 cancel is cooperative: the snapshot flips immediately so GET
        reflects terminal state, and the worker checks this on its next
        check-point. If the worker already wrote ``completed``, this raises
        (terminal → anything is refused).
        """
        return self.transition(
            peer=peer,
            task_id=task_id,
            to_state=STATE_CANCELED,
            error={"message": "canceled by client", "http_status": 499},
        )

    def is_canceled(self, *, peer: str, task_id: str) -> bool:
        snapshot = self.get(peer=peer, task_id=task_id)
        return bool(snapshot) and snapshot.get("state") == STATE_CANCELED

    def condition_for(self, task_id: str) -> threading.Condition:
        """Expose the per-task Condition for SSE subscribers to wait on."""
        return self._condition_for(task_id)

    def load_events(
        self,
        *,
        peer: str,
        task_id: str,
        after_index: int = -1,
    ) -> List[Dict[str, Any]]:
        """Return events with index strictly greater than ``after_index``.

        Index is the 0-based line number in the events file — the SSE
        handler uses it as the ``id:`` field so ``Last-Event-ID`` can resume.
        """
        if not self.enabled:
            return []
        try:
            _, _, events_path = self._paths(peer, task_id)
        except TaskError:
            return []
        try:
            with open(events_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return []
        except OSError:
            return []
        out: List[Dict[str, Any]] = []
        for idx, line in enumerate(raw.split("\n")):
            if idx <= after_index:
                continue
            trimmed = line.strip()
            if not trimmed:
                continue
            try:
                parsed = json.loads(trimmed)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            parsed.setdefault("_index", idx)
            out.append(parsed)
        return out

    def sweep(self, *, now_fn: Callable[[], datetime] = None) -> Dict[str, int]:
        """Walk the store, timing out stale in-flight tasks + purging old
        terminal ones. Safe to call on a timer."""
        if not self.enabled:
            return {"expired": 0, "purged": 0}
        now = (now_fn or (lambda: datetime.now(timezone.utc)))()
        expired = 0
        purged = 0
        try:
            peers = os.listdir(self.storage_dir)
        except OSError:
            return {"expired": 0, "purged": 0}
        for peer_dir in peers:
            if not safe_id(peer_dir):
                continue
            peer_path = os.path.join(self.storage_dir, peer_dir)
            try:
                entries = os.listdir(peer_path)
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith(".json") or entry.endswith(".events.jsonl"):
                    continue
                task_id = entry[: -len(".json")]
                if not safe_id(task_id):
                    continue
                snap = self.get(peer=peer_dir, task_id=task_id)
                if not snap:
                    continue
                state = snap.get("state")
                updated_at = self._parse_iso(snap.get("updated_at"))
                deadline_at = self._parse_iso(snap.get("deadline_at"))
                if state in TERMINAL_STATES:
                    if updated_at and (now - updated_at).total_seconds() >= self.retain_s:
                        self._purge(peer_dir, task_id)
                        purged += 1
                    continue
                if deadline_at and now >= deadline_at:
                    try:
                        self.transition(
                            peer=peer_dir,
                            task_id=task_id,
                            to_state=STATE_FAILED,
                            error={"message": "task deadline exceeded", "http_status": 504},
                        )
                        expired += 1
                    except TaskError:
                        pass
        return {"expired": expired, "purged": purged}

    # ---- internals ------------------------------------------------------

    def _write_snapshot(self, path: str, snapshot: Dict[str, Any]) -> None:
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(snapshot, ensure_ascii=False))
            os.replace(tmp, path)
        except OSError as exc:
            sys.stderr.write(f"a2a-sidecar: task snapshot write failed for {path}: {exc}\n")
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise TaskError("snapshot write failed", http_status=500)

    def _append_event(self, peer: str, task_id: str, event: Dict[str, Any]) -> None:
        _, _, events_path = self._paths(peer, task_id)
        try:
            with open(events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            sys.stderr.write(
                f"a2a-sidecar: task event append failed for {peer}/{task_id}: {exc}\n"
            )

    def _purge(self, peer: str, task_id: str) -> None:
        try:
            _, snapshot_path, events_path = self._paths(peer, task_id)
        except TaskError:
            return
        for p in (snapshot_path, events_path):
            try:
                os.remove(p)
            except OSError:
                pass

    @staticmethod
    def _parse_iso(value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


def create_task_store(
    storage_dir: str = "",
    *,
    default_deadline_s: int = 86400,
    retain_s: int = 86400,
    now_fn: Callable[[], str] = _default_now,
) -> TaskStore:
    return TaskStore(
        storage_dir=storage_dir,
        default_deadline_s=default_deadline_s,
        retain_s=retain_s,
        now_fn=now_fn,
    )


__all__ = [
    "STATE_SUBMITTED",
    "STATE_WORKING",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_CANCELED",
    "TERMINAL_STATES",
    "VALID_STATES",
    "TaskError",
    "TaskStore",
    "create_task_store",
    "mint_task_id",
]
