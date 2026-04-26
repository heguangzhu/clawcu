"""Structured handler context for the OpenClaw sidecar.

Review-2 §1: ``_make_handler_class`` used to take ``ctx: Dict[str, Any]``
and unpack twenty keys by string name, which left no type safety, no
IDE surface, and no compile-time guard against a typo like
``ctx["gatway_host"]``. Hermes's equivalent — ``hermes/sidecar/config.py
::Config`` — has always been a proper class; pulling the openclaw side
up to the same shape makes the two factories symmetric and the handler
body a plain ``ctx.gateway_host`` instead of a dict lookup.

This object is intentionally a bag of pre-built dependencies, not a
parser of env vars: ``main()`` does the env-reading and hands finished
values in. That preserves the current separation — env parsing stays
linear and testable, the handler closure stays a dependency sink.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class Context:
    """All the state the request handler closure needs.

    Field types are intentionally ``Any`` for ``logger``, ``adapter``,
    ``rate_limiter``, ``thread_store``: each is a protocol-shaped
    runtime object whose full interface lives in its own module
    (``logsink``, ``adapters``, ``_common.ratelimit``, ``_common.thread``).
    Stronger typing would require a ``Protocol`` per dependency and
    pays off less than the switch from dict-key access does on its own.
    """

    logger: Any
    self_name: str
    card: Dict[str, Any]
    adapter: Any
    gateway_host: str
    gateway_port: int
    gateway_ready_path: str
    gateway_ready_deadline_ms: int
    request_timeout_ms: int
    model: str
    rate_limiter: Any
    thread_store: Any
    # Async-task plumbing (Phase 1). Both ``None`` when A2A_TASK_DIR is unset —
    # the handler then refuses ``mode=async`` with 503 rather than advertising
    # a feature backed by nothing.
    task_store: Any = None
    task_worker: Any = None
    task_heartbeat_s: float = 15.0
