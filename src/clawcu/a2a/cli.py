from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from clawcu.a2a.bridge import echo_reply, serve_bridge_forever
from clawcu.a2a.card import (
    DEFAULT_BRIDGE_PORT,
    DEFAULT_REGISTRY_PORT,
    AgentCard,
    card_from_record,
)
from clawcu.a2a.client import A2AClientError, send_via_registry
from clawcu.a2a.registry import make_cards_provider, serve_registry_forever
from clawcu.service import ClawCUService

a2a_app = typer.Typer(
    help="Agent-to-agent discovery and messaging (proto, stdlib-only).",
    add_completion=False,
    no_args_is_help=True,
)

registry_app = typer.Typer(
    help="Run the A2A registry that aggregates AgentCards for running instances.",
    add_completion=False,
    no_args_is_help=True,
)
bridge_app = typer.Typer(
    help="Run a local echo bridge that exposes an AgentCard + /a2a/send for one instance.",
    add_completion=False,
    no_args_is_help=True,
)

a2a_app.add_typer(registry_app, name="registry")
a2a_app.add_typer(bridge_app, name="bridge")


console = Console()


def _get_service() -> ClawCUService:
    return ClawCUService()


def _find_record(service: ClawCUService, name: str):
    for record in service.list_instances():
        if record.name == name:
            return record
    return None


@a2a_app.command("card")
def card_command(
    name: Annotated[
        str | None,
        typer.Option("--name", help="Instance name. Omit to print cards for all managed instances."),
    ] = None,
    host: Annotated[str, typer.Option("--host", help="Host used in the derived endpoint URL.")] = "127.0.0.1",
) -> None:
    """Print the AgentCard JSON for a local clawcu instance."""
    service = _get_service()
    records = service.list_instances()
    if name is not None:
        matches = [r for r in records if r.name == name]
        if not matches:
            console.print(f"[bold red]Error:[/bold red] instance '{name}' not found.")
            raise typer.Exit(code=1)
        card = card_from_record(matches[0], service=service, host=host)
        console.print_json(card.to_json())
        return
    cards = [card_from_record(r, service=service, host=host).to_dict() for r in records]
    console.print_json(json.dumps(cards))


@registry_app.command("serve")
def registry_serve(
    port: Annotated[int, typer.Option("--port", help="Port to bind the registry server on.")] = DEFAULT_REGISTRY_PORT,
    host: Annotated[str, typer.Option("--host", help="Host interface to bind.")] = "127.0.0.1",
) -> None:
    """Serve /agents and /agents/{name} over HTTP (stdlib-only)."""
    service = _get_service()
    provider = make_cards_provider(service, host=host)
    console.print(f"[bold]A2A registry[/bold] listening on http://{host}:{port}")
    try:
        serve_registry_forever(provider, host=host, port=port)
    except KeyboardInterrupt:
        console.print("registry: stopped")


@bridge_app.command("serve")
def bridge_serve(
    instance: Annotated[str, typer.Option("--instance", help="Instance name to represent.")],
    port: Annotated[int, typer.Option("--port", help="Port to bind the bridge server on.")] = DEFAULT_BRIDGE_PORT,
    host: Annotated[str, typer.Option("--host", help="Host interface to bind.")] = "127.0.0.1",
) -> None:
    """Serve /.well-known/agent-card.json + /a2a/send for one instance."""
    service = _get_service()
    record = _find_record(service, instance)
    if record is None:
        console.print(f"[bold red]Error:[/bold red] instance '{instance}' not found.")
        raise typer.Exit(code=1)
    card = card_from_record(record, host=host)
    # Override endpoint to match the actual serving port so the well-known
    # card matches where the bridge is listening.
    card = AgentCard(
        name=card.name,
        role=card.role,
        skills=list(card.skills),
        endpoint=f"http://{host}:{port}/a2a/send",
    )
    console.print(f"[bold]A2A bridge[/bold] for '{instance}' listening on http://{host}:{port}")
    try:
        serve_bridge_forever(card, host=host, port=port, reply_fn=echo_reply)
    except KeyboardInterrupt:
        console.print("bridge: stopped")


@a2a_app.command("send")
def send_command(
    to: Annotated[str, typer.Option("--to", help="Target instance name.")],
    message: Annotated[str, typer.Option("--message", help="Message text.")],
    registry: Annotated[
        str,
        typer.Option("--registry", help="Registry base URL."),
    ] = f"http://127.0.0.1:{DEFAULT_REGISTRY_PORT}",
    sender: Annotated[
        str,
        typer.Option("--from", help="Self name reported in the message envelope."),
    ] = "clawcu-cli",
) -> None:
    """Look up TARGET in the registry and POST a message to its bridge."""
    try:
        reply = send_via_registry(
            registry_url=registry,
            sender=sender,
            target=to,
            message=message,
        )
    except A2AClientError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1)
    console.print_json(json.dumps(reply))
