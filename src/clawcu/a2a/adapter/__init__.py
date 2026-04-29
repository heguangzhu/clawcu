"""ClawCU A2A adapter — companion container speaking the standard A2A protocol."""

from .executor import GatewayExecutor
from .card import build_agent_card
from .server import create_app

__all__ = ["GatewayExecutor", "build_agent_card", "create_app"]
