from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from clawcu.a2a.card import (
    DEFAULT_REGISTRY_PORT,
    card_from_record,
)
from clawcu.a2a.client import (
    DEFAULT_CLI_SEND_TIMEOUT,
    DEFAULT_TIMEOUT,
    A2AClientError,
    send_via_registry,
)
from clawcu.a2a.registry import make_cards_provider, serve_registry_forever
from clawcu.a2a.registry_store import make_redis_cards_provider
from clawcu.a2a.adapter.tasks import DEFAULT_REDIS_URL
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

a2a_app.add_typer(registry_app, name="registry")


console = Console()


def _get_service() -> ClawCUService:
    return ClawCUService()


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
    provider_mode: Annotated[str, typer.Option("--provider", help="Card provider: probe or redis.")] = "probe",
    redis_url: Annotated[str, typer.Option("--redis-url", help="Redis URL for --provider redis.")] = DEFAULT_REDIS_URL,
) -> None:
    """Serve /agents and /agents/{name} over HTTP."""
    if provider_mode == "redis":
        provider = make_redis_cards_provider(redis_url)
    elif provider_mode == "probe":
        service = _get_service()
        provider = make_cards_provider(service, host=host)
    else:
        console.print("[bold red]Error:[/bold red] --provider must be 'probe' or 'redis'.")
        raise typer.Exit(code=2)
    console.print(f"[bold]A2A registry[/bold] listening on http://{host}:{port} ({provider_mode})")
    try:
        serve_registry_forever(provider, host=host, port=port)
    except KeyboardInterrupt:
        console.print("registry: stopped")


@a2a_app.command("send", hidden=True)
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
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help=(
                "Seconds to wait for the LLM reply (A2A JSON-RPC message/send). "
                "Does not affect the registry lookup; see --lookup-timeout. "
                "Raise this for long agent turns (tool use, large outputs)."
            ),
        ),
    ] = DEFAULT_CLI_SEND_TIMEOUT,
    lookup_timeout: Annotated[
        float,
        typer.Option(
            "--lookup-timeout",
            help="Seconds to wait for the registry card lookup.",
        ),
    ] = DEFAULT_TIMEOUT,
) -> None:
    """Look up TARGET in the registry and POST a message to its bridge.

    Review-1 §7: ``--timeout`` covers the LLM reply only; the registry
    lookup has its own small budget via ``--lookup-timeout`` because the
    registry is local and slow lookups almost always mean "no peer
    running", not "wait longer."
    """
    try:
        reply = send_via_registry(
            registry_url=registry,
            sender=sender,
            target=to,
            message=message,
            lookup_timeout=lookup_timeout,
            send_timeout=timeout,
        )
    except A2AClientError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1)
    console.print_json(json.dumps(reply))
