"""Unit tests for _common.task_store (TaskStore state machine + persistence)."""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

_COMMON_PARENT = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
    )
)
if _COMMON_PARENT not in sys.path:
    sys.path.insert(0, _COMMON_PARENT)

from _common.task_store import (  # noqa: E402
    STATE_CANCELED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_SUBMITTED,
    STATE_WORKING,
    TaskError,
    create_task_store,
    mint_task_id,
)
from _common.task_worker import TaskWorker  # noqa: E402


# ---- Unit: TaskStore -----------------------------------------------------


def test_disabled_store_rejects_writes():
    store = create_task_store(storage_dir="")
    assert store.enabled is False
    assert store.get(peer="alice", task_id="task_x") is None
    with pytest.raises(TaskError) as ei:
        store.create(peer="alice", message="hi")
    assert ei.value.http_status == 503


def test_mint_task_id_shape():
    tid = mint_task_id()
    assert tid.startswith("task_")
    assert len(tid) == 5 + 32
    assert all(c in "0123456789abcdef" for c in tid[5:])


def test_create_and_get_roundtrip(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="do X", thread_id="t1", request_id="r1")
    assert snap["state"] == STATE_SUBMITTED
    assert snap["peer"] == "alice"
    assert snap["thread_id"] == "t1"
    assert snap["input"] == {"message": "do X", "from": "alice"}
    assert snap["request_ids"] == ["r1"]

    fetched = store.get(peer="alice", task_id=snap["task_id"])
    assert fetched == snap


def test_create_rejects_unsafe_peer(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    with pytest.raises(TaskError):
        store.create(peer="../evil", message="hi")
    with pytest.raises(TaskError):
        store.create(peer="alice", task_id="../evil", message="hi")


def test_valid_transition_submitted_to_working_to_completed(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    store.transition(peer="alice", task_id=tid, to_state=STATE_WORKING)
    s2 = store.transition(
        peer="alice",
        task_id=tid,
        to_state=STATE_COMPLETED,
        result={"reply": "hello", "thread_id": None},
    )
    assert s2["state"] == STATE_COMPLETED
    assert s2["result"] == {"reply": "hello", "thread_id": None}


def test_illegal_transition_rejected(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    # submitted → completed is not allowed (must go through working)
    with pytest.raises(TaskError) as ei:
        store.transition(peer="alice", task_id=snap["task_id"], to_state=STATE_COMPLETED)
    assert ei.value.http_status == 409


def test_terminal_is_sticky(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    store.transition(peer="alice", task_id=snap["task_id"], to_state=STATE_WORKING)
    store.transition(
        peer="alice",
        task_id=snap["task_id"],
        to_state=STATE_COMPLETED,
        result={"reply": "done"},
    )
    # Writing terminal again to a different terminal state is rejected.
    with pytest.raises(TaskError):
        store.transition(
            peer="alice", task_id=snap["task_id"], to_state=STATE_FAILED,
            error={"message": "oops"},
        )
    # Idempotent same-state write is allowed (returns current snapshot).
    again = store.transition(
        peer="alice", task_id=snap["task_id"], to_state=STATE_COMPLETED
    )
    assert again["state"] == STATE_COMPLETED


def test_request_cancel_flips_to_canceled(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    store.transition(peer="alice", task_id=snap["task_id"], to_state=STATE_WORKING)
    c = store.request_cancel(peer="alice", task_id=snap["task_id"])
    assert c["state"] == STATE_CANCELED
    assert c["error"]["http_status"] == 499
    assert store.is_canceled(peer="alice", task_id=snap["task_id"]) is True


def test_load_events_replay(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    store.transition(peer="alice", task_id=tid, to_state=STATE_WORKING)
    store.transition(
        peer="alice", task_id=tid, to_state=STATE_COMPLETED, result={"reply": "ok"}
    )
    events = store.load_events(peer="alice", task_id=tid)
    # submitted + working + completed
    assert [e["state"] for e in events] == [
        STATE_SUBMITTED, STATE_WORKING, STATE_COMPLETED
    ]
    # Each event has a monotonic _index used as SSE id.
    assert [e["_index"] for e in events] == [0, 1, 2]
    # After-index resume.
    tail = store.load_events(peer="alice", task_id=tid, after_index=0)
    assert [e["state"] for e in tail] == [STATE_WORKING, STATE_COMPLETED]


def test_sweep_purges_old_terminal(tmp_path):
    # Clock that moves 25h forward for terminal-retention check.
    now_box = [datetime.now(timezone.utc)]

    def now_fn():
        return now_box[0].isoformat()

    store = create_task_store(storage_dir=str(tmp_path), retain_s=3600, now_fn=now_fn)
    snap = store.create(peer="alice", message="hi")
    store.transition(peer="alice", task_id=snap["task_id"], to_state=STATE_WORKING)
    store.transition(
        peer="alice",
        task_id=snap["task_id"],
        to_state=STATE_COMPLETED,
        result={"reply": "ok"},
    )
    # Advance clock beyond retention.
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    stats = store.sweep(now_fn=lambda: future)
    assert stats["purged"] == 1
    assert store.get(peer="alice", task_id=snap["task_id"]) is None


def test_sweep_times_out_in_flight_past_deadline(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path), default_deadline_s=1)
    snap = store.create(peer="alice", message="hi")
    store.transition(peer="alice", task_id=snap["task_id"], to_state=STATE_WORKING)
    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    stats = store.sweep(now_fn=lambda: future)
    assert stats["expired"] == 1
    fetched = store.get(peer="alice", task_id=snap["task_id"])
    assert fetched["state"] == STATE_FAILED
    assert fetched["error"]["http_status"] == 504


def test_condition_notifies_on_transition(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    cond = store.condition_for(snap["task_id"])
    seen = []

    def _waiter():
        with cond:
            cond.wait(timeout=1.0)
        seen.append(store.get(peer="alice", task_id=snap["task_id"])["state"])

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.05)  # let waiter park
    store.transition(peer="alice", task_id=snap["task_id"], to_state=STATE_WORKING)
    t.join(timeout=2.0)
    assert seen == [STATE_WORKING]


def test_progress_appends_event_and_bumps_updated_at(tmp_path):
    # Wall-clock that ticks 1s per call so we can assert ordering.
    box = [datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc)]

    def now_fn():
        cur = box[0]
        box[0] = cur + timedelta(seconds=1)
        return cur.isoformat()

    store = create_task_store(storage_dir=str(tmp_path), now_fn=now_fn)
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    store.transition(peer="alice", task_id=tid, to_state=STATE_WORKING)

    after = store.progress(peer="alice", task_id=tid, message="calling hermes")
    assert after is not None
    assert after["state"] == STATE_WORKING
    assert after["last_progress_message"] == "calling hermes"
    assert after["updated_at"] == after["last_progress_at"]
    # updated_at advanced past the working transition's timestamp.
    fetched = store.get(peer="alice", task_id=tid)
    assert fetched["last_progress_message"] == "calling hermes"

    events = store.load_events(peer="alice", task_id=tid)
    progress_events = [e for e in events if e.get("event") == "progress"]
    assert len(progress_events) == 1
    assert progress_events[0]["message"] == "calling hermes"


def test_progress_is_no_op_when_terminal(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    store.transition(peer="alice", task_id=tid, to_state=STATE_WORKING)
    store.transition(
        peer="alice", task_id=tid, to_state=STATE_COMPLETED, result={"reply": "ok"}
    )
    out = store.progress(peer="alice", task_id=tid, message="ignored")
    assert out["state"] == STATE_COMPLETED
    fetched = store.get(peer="alice", task_id=tid)
    assert "last_progress_message" not in fetched
    events = store.load_events(peer="alice", task_id=tid)
    assert all(e.get("event") != "progress" for e in events)


def test_progress_caps_message_length(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    store.transition(peer="alice", task_id=tid, to_state=STATE_WORKING)
    long_msg = "x" * 500
    after = store.progress(peer="alice", task_id=tid, message=long_msg)
    assert len(after["last_progress_message"]) == 200


def test_progress_disabled_store_returns_none():
    store = create_task_store(storage_dir="")
    out = store.progress(peer="alice", task_id="task_x", message="hi")
    assert out is None


# ---- Integration-lite: TaskWorker end-to-end ----------------------------


class _FakeLogger:
    def __init__(self):
        self.entries = []

    def info(self, msg):
        self.entries.append(("info", msg))

    def warn(self, msg):
        self.entries.append(("warn", msg))

    def error(self, msg):
        self.entries.append(("error", msg))


def test_worker_runs_task_to_completed(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    logger = _FakeLogger()

    def run_fn(snapshot):
        return {"reply": f"echo: {snapshot['input']['message']}", "thread_id": snapshot.get("thread_id")}

    worker = TaskWorker(
        store=store, run_fn=run_fn, logger=logger, self_name="bob", max_workers=1
    )
    snap = store.create(peer="alice", message="hello")
    worker.submit(peer="alice", task_id=snap["task_id"])
    # Poll for terminal state.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        fetched = store.get(peer="alice", task_id=snap["task_id"])
        if fetched["state"] == STATE_COMPLETED:
            break
        time.sleep(0.02)
    worker.shutdown(wait=True)
    fetched = store.get(peer="alice", task_id=snap["task_id"])
    assert fetched["state"] == STATE_COMPLETED
    assert fetched["result"] == {"reply": "echo: hello", "thread_id": None}


def test_worker_marks_failed_on_exception(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    logger = _FakeLogger()

    def run_fn(snapshot):
        raise RuntimeError("boom")

    worker = TaskWorker(
        store=store, run_fn=run_fn, logger=logger, self_name="bob", max_workers=1
    )
    snap = store.create(peer="alice", message="hi")
    worker.submit(peer="alice", task_id=snap["task_id"])
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        fetched = store.get(peer="alice", task_id=snap["task_id"])
        if fetched["state"] == STATE_FAILED:
            break
        time.sleep(0.02)
    worker.shutdown(wait=True)
    fetched = store.get(peer="alice", task_id=snap["task_id"])
    assert fetched["state"] == STATE_FAILED
    assert "boom" in fetched["error"]["message"]


def test_worker_emits_heartbeat_while_run_fn_runs(tmp_path):
    """Long-running run_fn must produce progress events at heartbeat cadence
    so pollers can see the worker is alive."""
    store = create_task_store(storage_dir=str(tmp_path))
    logger = _FakeLogger()
    release = threading.Event()

    def run_fn(snapshot):
        # Block long enough to receive at least 2 heartbeats at 0.05s.
        release.wait(timeout=0.5)
        return {"reply": "done", "thread_id": None}

    worker = TaskWorker(
        store=store,
        run_fn=run_fn,
        logger=logger,
        self_name="bob",
        max_workers=1,
        heartbeat_s=0.05,
    )
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    worker.submit(peer="alice", task_id=tid)
    # Let heartbeats accumulate.
    time.sleep(0.2)
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if store.get(peer="alice", task_id=tid)["state"] == STATE_COMPLETED:
            break
        time.sleep(0.02)
    worker.shutdown(wait=True)
    events = store.load_events(peer="alice", task_id=tid)
    progress_events = [e for e in events if e.get("event") == "progress"]
    assert len(progress_events) >= 2, f"expected ≥2 heartbeats, got {progress_events}"


def test_worker_passes_progress_callback_when_run_fn_declares_it(tmp_path):
    store = create_task_store(storage_dir=str(tmp_path))
    logger = _FakeLogger()
    seen = {"calls": []}

    def run_fn(snapshot, *, progress):
        progress("step-one")
        progress("step-two")
        seen["calls"].append("ran")
        return {"reply": "ok", "thread_id": None}

    worker = TaskWorker(
        store=store,
        run_fn=run_fn,
        logger=logger,
        self_name="bob",
        max_workers=1,
        heartbeat_s=0,  # disable timer; assert only run_fn-emitted breadcrumbs
    )
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    worker.submit(peer="alice", task_id=tid)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if store.get(peer="alice", task_id=tid)["state"] == STATE_COMPLETED:
            break
        time.sleep(0.02)
    worker.shutdown(wait=True)
    assert seen["calls"] == ["ran"]
    events = store.load_events(peer="alice", task_id=tid)
    msgs = [e.get("message") for e in events if e.get("event") == "progress"]
    assert msgs == ["step-one", "step-two"]
    fetched = store.get(peer="alice", task_id=tid)
    # Snapshot retains only the latest message; events file has the full list.
    assert fetched["result"] == {"reply": "ok", "thread_id": None}


def test_worker_does_not_pass_progress_when_run_fn_omits_it(tmp_path):
    """Backward-compat: a run_fn with positional-only signature still works."""
    store = create_task_store(storage_dir=str(tmp_path))
    logger = _FakeLogger()

    def run_fn(snapshot):
        return {"reply": "ok", "thread_id": None}

    worker = TaskWorker(
        store=store,
        run_fn=run_fn,
        logger=logger,
        self_name="bob",
        max_workers=1,
        heartbeat_s=0,
    )
    snap = store.create(peer="alice", message="hi")
    tid = snap["task_id"]
    worker.submit(peer="alice", task_id=tid)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if store.get(peer="alice", task_id=tid)["state"] == STATE_COMPLETED:
            break
        time.sleep(0.02)
    worker.shutdown(wait=True)
    fetched = store.get(peer="alice", task_id=tid)
    assert fetched["state"] == STATE_COMPLETED


def test_worker_honors_prior_cancel(tmp_path):
    """If cancel lands before run_fn returns, terminal write is canceled
    rather than completed."""
    store = create_task_store(storage_dir=str(tmp_path))
    logger = _FakeLogger()
    started = threading.Event()
    release = threading.Event()

    def run_fn(snapshot):
        started.set()
        release.wait(timeout=2.0)
        return {"reply": "late", "thread_id": None}

    worker = TaskWorker(
        store=store, run_fn=run_fn, logger=logger, self_name="bob", max_workers=1
    )
    snap = store.create(peer="alice", message="hi")
    worker.submit(peer="alice", task_id=snap["task_id"])
    assert started.wait(timeout=2.0)
    # Cancel while run_fn is still parked.
    store.request_cancel(peer="alice", task_id=snap["task_id"])
    release.set()
    # Give worker time to wake up.
    time.sleep(0.1)
    worker.shutdown(wait=True)
    fetched = store.get(peer="alice", task_id=snap["task_id"])
    assert fetched["state"] == STATE_CANCELED
