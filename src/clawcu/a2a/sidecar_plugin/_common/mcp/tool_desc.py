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
MAX_PEERS_IN_DESCRIPTION = 16
MAX_SKILLS_IN_PEER_LINE = 3

BASE_DESCRIPTION = (
    "Call another agent in the A2A federation and return its reply. "
    "Use when the current task needs data or work owned by a different "
    "agent (e.g., an analyst for market data, a writer for prose)."
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


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "TOOL_NAME",
    "MAX_PEERS_IN_DESCRIPTION",
    "MAX_SKILLS_IN_PEER_LINE",
    "BASE_DESCRIPTION",
    "is_tool_desc_static",
    "tool_desc_include_role",
    "format_peer_line",
    "format_peer_summary",
    "tool_descriptor",
]
