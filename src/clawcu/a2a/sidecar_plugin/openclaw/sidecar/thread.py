"""Per-peer / per-thread conversation history (Python port of thread.js).

Append-only JSONL at `<storage_dir>/<peer>/<thread_id>.jsonl`. Enabled only
when `storage_dir` is truthy; otherwise load/append no-op. Peer and thread
ids must match SAFE_ID to prevent path traversal.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Callable, List, Optional

SAFE_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def safe_id(value) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    if not SAFE_ID.match(value):
        return None
    if value == "." or value == "..":
        return None
    return value


def _default_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


class ThreadStore:
    def __init__(
        self,
        storage_dir: str = "",
        max_history_pairs: int = 10,
        now_fn: Callable[[], str] = _default_now,
    ) -> None:
        self.storage_dir = storage_dir
        self.max_history_pairs = max_history_pairs
        self.now_fn = now_fn
        self.enabled = bool(storage_dir)

    def _thread_paths(self, peer: str, thread_id: str):
        p = safe_id(peer)
        t = safe_id(thread_id)
        if not p or not t:
            return None
        directory = os.path.join(self.storage_dir, p)
        return directory, os.path.join(directory, f"{t}.jsonl")

    def load_history(self, peer: str, thread_id: str) -> List[dict]:
        if not self.enabled:
            return []
        paths = self._thread_paths(peer, thread_id)
        if paths is None:
            return []
        _, file_path = paths
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return []
        except OSError as exc:
            sys.stderr.write(
                f"a2a-sidecar: thread load failed for {peer}/{thread_id}: {exc}\n"
            )
            return []
        out: List[dict] = []
        for line in raw.split("\n"):
            trimmed = line.strip()
            if not trimmed:
                continue
            try:
                parsed = json.loads(trimmed)
            except Exception:
                continue
            if (
                not isinstance(parsed, dict)
                or not isinstance(parsed.get("content"), str)
                or parsed.get("role") not in ("user", "assistant")
            ):
                continue
            out.append({"role": parsed["role"], "content": parsed["content"]})
        cap = max(0, self.max_history_pairs) * 2
        if cap > 0 and len(out) > cap:
            return out[len(out) - cap :]
        return out

    def append_turn(self, peer: str, thread_id: str, user_msg: str, assistant_msg: str) -> bool:
        if not self.enabled:
            return False
        paths = self._thread_paths(peer, thread_id)
        if paths is None:
            return False
        if not isinstance(user_msg, str) or not isinstance(assistant_msg, str):
            return False
        directory, file_path = paths
        try:
            os.makedirs(directory, exist_ok=True)
            ts = self.now_fn()
            line_u = json.dumps({"role": "user", "content": user_msg, "ts": ts}, ensure_ascii=False)
            line_a = json.dumps({"role": "assistant", "content": assistant_msg, "ts": ts}, ensure_ascii=False)
            with open(file_path, "a", encoding="utf-8") as fh:
                fh.write(line_u + "\n")
                fh.write(line_a + "\n")
            return True
        except OSError as exc:
            sys.stderr.write(
                f"a2a-sidecar: thread append failed for {peer}/{thread_id}: {exc}\n"
            )
            return False


def create_thread_store(
    storage_dir: str = "",
    max_history_pairs: int = 10,
    now_fn: Callable[[], str] = _default_now,
) -> ThreadStore:
    return ThreadStore(storage_dir=storage_dir, max_history_pairs=max_history_pairs, now_fn=now_fn)
