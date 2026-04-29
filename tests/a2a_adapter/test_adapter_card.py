"""Tests for clawcu.a2a.adapter.card — AgentCard construction."""

import os
import pytest

a2a_sdk = pytest.importorskip("a2a", reason="a2a-sdk not installed")


@pytest.fixture
def env(monkeypatch):
    def _set(**kwargs):
        for k, v in kwargs.items():
            monkeypatch.setenv(k, v)
    return _set


class TestBuildAgentCard:
    def test_minimal(self, env):
        env(A2A_AGENT_NAME="writer", A2A_AGENT_URL="http://127.0.0.1:18800")
        from clawcu.a2a.adapter.card import build_agent_card

        card = build_agent_card()
        assert card.name == "writer"
        assert card.skills
        assert card.capabilities.streaming is True

    def test_all_options(self, env):
        env(
            A2A_AGENT_NAME="analyst",
            A2A_AGENT_URL="http://host.docker.internal:9129",
            A2A_AGENT_DESCRIPTION="Hermes analyst",
            A2A_AGENT_ROLE="senior analyst",
            A2A_AGENT_SKILLS="chat,analysis,forecasting",
        )
        from clawcu.a2a.adapter.card import build_agent_card

        card = build_agent_card()
        assert card.name == "analyst"
        assert card.description == "Hermes analyst"
        tags = card.skills[0].tags
        assert "chat" in tags
        assert "analysis" in tags

    def test_missing_name_raises(self, monkeypatch):
        from clawcu.a2a.adapter.card import build_agent_card

        monkeypatch.delenv("A2A_AGENT_NAME", raising=False)
        with pytest.raises(KeyError):
            build_agent_card()
