"""Starlette app and uvicorn entrypoint for the ClawCU A2A adapter."""

from __future__ import annotations

import logging

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette

from .card import build_agent_card
from .executor import GatewayExecutor

log = logging.getLogger("clawcu-a2a-adapter")


def create_app() -> Starlette:
    """Build the Starlette application with JSON-RPC and agent-card routes."""
    agent_card = build_agent_card()
    executor = GatewayExecutor()
    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    routes = [
        *create_agent_card_routes(agent_card),
        *create_jsonrpc_routes(handler, rpc_url="/"),
    ]

    return Starlette(routes=routes)


def main() -> None:
    """Run the adapter via uvicorn (entrypoint for ``python -m clawcu.a2a.adapter.server``)."""
    import os

    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    host = os.environ.get("A2A_ADAPTER_HOST", "0.0.0.0")
    port = int(os.environ.get("A2A_ADAPTER_PORT", "18790"))
    log.info("starting clawcu-a2a-adapter on %s:%s", host, port)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
