"""MCP ``a2a_call_peer`` tool descriptor + env flags.

Extracted from the monolithic ``_common.mcp`` (review-2 §10) so the
peer-summary rendering and its two env tunables live with the shape
they produce, separately from the JSON-RPC dispatcher. Callers import
these through ``_common.mcp`` — see the package ``__init__`` for the
re-export list.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

MCP_PROTOCOL_VERSION = "2024-11-05"
TOOL_NAME = "a2a_call_peer"
TOOL_NAME_ASYNC = "a2a_call_peer_async"
TOOL_NAME_GET_TASK = "a2a_get_task"
TOOL_NAME_CANCEL_TASK = "a2a_cancel_task"
ASYNC_TOOL_NAMES = frozenset({TOOL_NAME_ASYNC, TOOL_NAME_GET_TASK, TOOL_NAME_CANCEL_TASK})
MAX_PEERS_IN_DESCRIPTION = 16
MAX_SKILLS_IN_PEER_LINE = 3

BASE_DESCRIPTION = (
    "Call another agent in the A2A federation and wait inline for its reply. "
    "IMPORTANT: this tool blocks the calling MCP client, which times out at "
    "~60 seconds. Use it only for short, fast replies — lookups, status "
    "checks, one-shot factual answers a peer can produce in well under a "
    "minute. For analysis, research, web browsing, multi-step reasoning, "
    "code generation, or anything where the duration is unknown, use "
    "a2a_call_peer_async instead."
)


def is_tool_desc_static(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return ``True`` when the operator has forced the static tool description.

    ``A2A_TOOL_DESC_MODE=static`` opts out of the peer-list injection into
    the MCP tool description (a2a-design-5.md §P1-H): tests or deployments
    with a flaky registry prefer the baked-in description over a cache
    miss stampede. Both sidecars gate ``list_peers_fn`` on this flag, so
    one parser keeps the semantics aligned.
    """
    source = env if env is not None else os.environ
    return source.get("A2A_TOOL_DESC_MODE") == "static"


def tool_desc_include_role(env: Optional[Mapping[str, str]] = None) -> bool:
    """Return ``True`` when peer lines in the tool description should
    include ``role`` next to ``name``.

    Opt-in via ``A2A_TOOL_DESC_INCLUDE_ROLE=true`` (case-insensitive, with
    surrounding whitespace tolerated via the ``or ""`` guard). Default off
    to keep the baseline description terse.
    """
    source = env if env is not None else os.environ
    return str(source.get("A2A_TOOL_DESC_INCLUDE_ROLE") or "").strip().lower() == "true"


def format_peer_line(peer: Dict[str, Any], *, include_role: bool = False) -> str:
    skills_raw = peer.get("skills")
    skills = skills_raw if isinstance(skills_raw, list) else []
    head = ", ".join(str(s) for s in skills[:MAX_SKILLS_IN_PEER_LINE])
    tail = ", ..." if len(skills) > MAX_SKILLS_IN_PEER_LINE else ""
    role_raw = peer.get("role") if include_role else None
    role = f" [{role_raw}]" if isinstance(role_raw, str) and role_raw else ""
    name = peer.get("name", "")
    if not head:
        return f"  - {name}{role}"
    return f"  - {name}{role} ({head}{tail})"


def format_peer_summary(
    peers: Any, self_name: Optional[str], *, include_role: bool = False
) -> str:
    """Multi-line summary injected into the tool description.

    Excludes self and caps at ``MAX_PEERS_IN_DESCRIPTION``. Returns ``""``
    when the list is empty or only contains self — callers then fall
    back to the static description.
    """
    if not isinstance(peers, list) or not peers:
        return ""
    others = [
        p for p in peers
        if isinstance(p, dict)
        and isinstance(p.get("name"), str)
        and p.get("name") != self_name
    ]
    if not others:
        return ""
    shown = others[:MAX_PEERS_IN_DESCRIPTION]
    hidden = len(others) - len(shown)
    lines = ["", "Available peers:"] + [
        format_peer_line(p, include_role=include_role) for p in shown
    ]
    if hidden > 0:
        lines.append(f"  ...and {hidden} more")
    return "\n".join(lines)


def tool_descriptor(
    peers: Any = None,
    self_name: Optional[str] = None,
    *,
    include_role: bool = False,
) -> Dict[str, Any]:
    description = BASE_DESCRIPTION
    summary = format_peer_summary(peers, self_name, include_role=include_role)
    if summary:
        description += summary
        description += (
            "\n\nThe `to` field must match one of the peers above "
            "(case-sensitive)."
        )
    else:
        description += " The target agent name must be registered in the A2A registry."
    return {
        "name": TOOL_NAME,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Peer agent name as registered in the A2A registry.",
                },
                "message": {
                    "type": "string",
                    "description": "The question or task for the peer agent.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional. Reuse a prior conversation thread with the peer.",
                },
            },
            "required": ["to", "message"],
        },
    }


ASYNC_CALL_DESCRIPTION = (
    "Dispatch a task to another agent and return immediately with a task_id. "
    "PREFER THIS over a2a_call_peer for any non-trivial work — analysis, "
    "research, web browsing, multi-step reasoning, code generation, content "
    "drafting, or any request where the peer might take more than a few "
    "seconds. The sync variant blocks the calling MCP client and dies at "
    "~60s; this variant has no such cap and works for tasks that run for "
    "minutes or hours.\n\n"
    "Workflow:\n"
    "  1. Call this and receive {task_id, state}.\n"
    "  2. IMMEDIATELY tell the user the task was dispatched and roughly "
    "how long peer work typically takes — the user is otherwise staring "
    "at a blank screen and will cancel.\n"
    "  3. Poll a2a_get_task until state is terminal "
    "(completed/failed/canceled).\n"
    "  4. Use a2a_cancel_task only if the user asks to abort."
)

GET_TASK_DESCRIPTION = (
    "Poll an async task on a peer agent. Returns a snapshot with state "
    "(submitted/working/completed/failed/canceled). On completed, the "
    "peer's reply is included in the response text. On failed, the error "
    "message is included.\n\n"
    "Polling discipline (IMPORTANT — clients will cancel if you poll too "
    "fast or go silent):\n"
    "  - Wait at least 20 seconds before the first poll. Analyst-style "
    "work rarely finishes faster.\n"
    "  - Between polls, wait 20–60 seconds. Polling every 3–5 seconds is "
    "wasteful and triggers stuck-session diagnostics on the host.\n"
    "  - Every 1–2 polls while still working, send the user a brief "
    "progress note (\"still working on the analyst task, ~Xm elapsed\") so "
    "they don't think the chat froze.\n"
    "  - Stop polling as soon as state is terminal — the response text "
    "already contains the reply or error."
)

CANCEL_TASK_DESCRIPTION = (
    "Request cancellation of an async task on a peer agent. Cooperative — "
    "the peer stops at its next checkpoint; the snapshot flips to canceled "
    "immediately. Use when the user no longer wants the result, or when a "
    "task has clearly hung."
)


def async_call_tool_descriptor() -> Dict[str, Any]:
    return {
        "name": TOOL_NAME_ASYNC,
        "description": ASYNC_CALL_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Peer agent name as registered in the A2A registry.",
                },
                "message": {
                    "type": "string",
                    "description": "The question or task for the peer agent.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional. Reuse a prior conversation thread with the peer.",
                },
            },
            "required": ["to", "message"],
        },
    }


def get_task_tool_descriptor() -> Dict[str, Any]:
    return {
        "name": TOOL_NAME_GET_TASK,
        "description": GET_TASK_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Peer agent name that owns the task.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID previously returned by a2a_call_peer_async.",
                },
            },
            "required": ["peer", "task_id"],
        },
    }


def cancel_task_tool_descriptor() -> Dict[str, Any]:
    return {
        "name": TOOL_NAME_CANCEL_TASK,
        "description": CANCEL_TASK_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer": {
                    "type": "string",
                    "description": "Peer agent name that owns the task.",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID to cancel.",
                },
            },
            "required": ["peer", "task_id"],
        },
    }


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "TOOL_NAME",
    "TOOL_NAME_ASYNC",
    "TOOL_NAME_GET_TASK",
    "TOOL_NAME_CANCEL_TASK",
    "ASYNC_TOOL_NAMES",
    "MAX_PEERS_IN_DESCRIPTION",
    "MAX_SKILLS_IN_PEER_LINE",
    "BASE_DESCRIPTION",
    "ASYNC_CALL_DESCRIPTION",
    "GET_TASK_DESCRIPTION",
    "CANCEL_TASK_DESCRIPTION",
    "is_tool_desc_static",
    "tool_desc_include_role",
    "format_peer_line",
    "format_peer_summary",
    "tool_descriptor",
    "async_call_tool_descriptor",
    "get_task_tool_descriptor",
    "cancel_task_tool_descriptor",
]
