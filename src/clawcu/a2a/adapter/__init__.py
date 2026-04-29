"""ClawCU A2A adapter — companion container speaking the standard A2A protocol."""

__all__ = ["GatewayExecutor", "build_agent_card", "create_app"]


def __getattr__(name):
    if name == "GatewayExecutor":
        from .executor import GatewayExecutor
        return GatewayExecutor
    if name == "build_agent_card":
        from .card import build_agent_card
        return build_agent_card
    if name == "create_app":
        from .server import create_app
        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
