"""Build a standard A2A AgentCard from environment variables."""

from __future__ import annotations

import os

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)


def build_agent_card() -> AgentCard:
    """Construct an AgentCard from ``A2A_*`` env vars.

    Required env vars:
      A2A_AGENT_NAME   — agent display name
      A2A_AGENT_URL    — base URL where this adapter is reachable

    Optional env vars:
      A2A_AGENT_DESCRIPTION — one-line description (default: "<name> agent")
      A2A_AGENT_ROLE        — role tag included in the default skill
      A2A_AGENT_SKILLS      — comma-separated skill tags
    """
    name = os.environ["A2A_AGENT_NAME"]
    url = os.environ["A2A_AGENT_URL"]
    description = os.environ.get("A2A_AGENT_DESCRIPTION", f"{name} agent")
    role = os.environ.get("A2A_AGENT_ROLE", "")
    raw_skills = os.environ.get("A2A_AGENT_SKILLS", "chat")

    skill_desc = f"{role} — send a message to {name}" if role else f"Send a message to {name}"
    skills = [
        AgentSkill(
            id="a2a-chat",
            name="chat",
            description=skill_desc,
            tags=[s.strip() for s in raw_skills.split(",") if s.strip()],
        ),
    ]

    return AgentCard(
        name=name,
        description=description,
        supported_interfaces=[
            AgentInterface(url=url, protocol_version="0.1"),
        ],
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True),
        skills=skills,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
