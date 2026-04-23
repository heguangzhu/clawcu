"""pytest port of tests/sidecar_thread.test.js."""
from __future__ import annotations

import os
import sys

_SIDECAR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
        "openclaw",
        "sidecar",
    )
)
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)

from thread import create_thread_store, safe_id  # noqa: E402


def test_disabled_store_is_noop():
    store = create_thread_store(storage_dir="")
    assert store.enabled is False
    assert store.load_history("peer", "tid") == []
    assert store.append_turn("peer", "tid", "hi", "hello") is False


def test_load_history_returns_empty_when_file_missing(tmp_path):
    store = create_thread_store(storage_dir=str(tmp_path))
    assert store.load_history("peer-a", "thread-1") == []


def test_append_then_load_roundtrip(tmp_path):
    store = create_thread_store(storage_dir=str(tmp_path))
    assert store.append_turn("peer-a", "tid-1", "hi", "hello") is True
    assert store.append_turn("peer-a", "tid-1", "how are you", "fine") is True
    history = store.load_history("peer-a", "tid-1")
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you"},
        {"role": "assistant", "content": "fine"},
    ]


def test_load_history_caps_at_max_pairs_from_tail(tmp_path):
    store = create_thread_store(storage_dir=str(tmp_path), max_history_pairs=2)
    for i in range(5):
        store.append_turn("peer-a", "tid-1", f"u{i}", f"a{i}")
    history = store.load_history("peer-a", "tid-1")
    assert len(history) == 4
    assert history == [
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]


def test_path_traversal_rejected(tmp_path):
    store = create_thread_store(storage_dir=str(tmp_path))
    attempts = [
        ("../escape", "tid"),
        ("peer", "../escape"),
        ("peer/sub", "tid"),
        ("peer", "tid/sub"),
        ("..", "tid"),
        ("peer", ".."),
        ("", "tid"),
        ("peer", ""),
    ]
    for peer, tid in attempts:
        assert store.append_turn(peer, tid, "x", "y") is False, f"append {peer}/{tid}"
        assert store.load_history(peer, tid) == [], f"load {peer}/{tid}"
    siblings = os.listdir(os.path.dirname(str(tmp_path)))
    assert not any(name.startswith("escape") for name in siblings), "no files escaped storage dir"


def test_per_peer_isolation_for_same_thread_id(tmp_path):
    store = create_thread_store(storage_dir=str(tmp_path))
    store.append_turn("peer-a", "tid-1", "A-msg", "A-reply")
    store.append_turn("peer-b", "tid-1", "B-msg", "B-reply")
    assert store.load_history("peer-a", "tid-1") == [
        {"role": "user", "content": "A-msg"},
        {"role": "assistant", "content": "A-reply"},
    ]
    assert store.load_history("peer-b", "tid-1") == [
        {"role": "user", "content": "B-msg"},
        {"role": "assistant", "content": "B-reply"},
    ]


def test_corrupt_json_line_is_skipped(tmp_path):
    store = create_thread_store(storage_dir=str(tmp_path))
    store.append_turn("peer-a", "tid-1", "hi", "hello")
    file_path = tmp_path / "peer-a" / "tid-1.jsonl"
    with open(file_path, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    store.append_turn("peer-a", "tid-1", "still there?", "yes")
    assert store.load_history("peer-a", "tid-1") == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "still there?"},
        {"role": "assistant", "content": "yes"},
    ]


def test_non_string_content_is_rejected(tmp_path):
    store = create_thread_store(storage_dir=str(tmp_path))
    assert store.append_turn("peer-a", "tid-1", 42, "ok") is False
    assert store.append_turn("peer-a", "tid-1", "ok", None) is False
    assert store.load_history("peer-a", "tid-1") == []


def test_safe_id_accepts_and_rejects():
    assert safe_id("0194c3f0-7d1a-7a3e-8b8e-7e0e7a1f6d42") == "0194c3f0-7d1a-7a3e-8b8e-7e0e7a1f6d42"
    assert safe_id("peer.name_01") == "peer.name_01"
    assert safe_id("") is None
    assert safe_id(".") is None
    assert safe_id("..") is None
    assert safe_id("peer/with/slash") is None
    assert safe_id("peer with space") is None
    assert safe_id(None) is None
    assert safe_id("x" * 129) is None
