from __future__ import annotations

import json
import threading
from enum import Enum
from typing import Annotated, Any

import typer
from rich.console import Console

from clawcu.a2a.bridge import build_bridge_server, echo_reply, serve_bridge_forever
from clawcu.a2a.card import (
    DEFAULT_BRIDGE_PORT,
    DEFAULT_REGISTRY_PORT,
    AgentCard,
    card_from_record,
    display_port_for_record,
    role_for_service,
    skills_for_service,
)
from clawcu.a2a.client import A2AClientError, send_via_registry
from clawcu.a2a.detect import detect_plugin_or_none
from clawcu.a2a.registry import make_cards_provider, serve_registry_forever
from clawcu.service import ClawCUService


class BridgeMode(str, Enum):
    echo = "echo"


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
    help=(
        "Run a local fallback bridge exposing AgentCard + /a2a/send for one "
        "instance. Demo/offline only — once a real OpenClaw/Hermes plugin is "
        "loaded, the registry federates its card directly and this bridge is "
        "not needed."
    ),
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


def _parse_skills(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


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


def _resolve_bridge_card(
    *,
    service: ClawCUService | None,
    instance: str,
    host: str,
    port: int | None,
    role_override: str | None,
    skills_override: list[str] | None,
    endpoint_override: str | None,
) -> tuple[AgentCard, int]:
    """Build the AgentCard + serving port for `bridge serve`.

    Falls through three cases:
      1. instance is managed → derive role/skills/port from the record,
         apply any overrides on top.
      2. instance is not managed but all three overrides (role, skills,
         endpoint) were supplied → allow it for pure-protocol demos.
      3. else → raise typer.Exit(1) with a pointer to clawcu list.
    """
    record = _find_record(service, instance) if service is not None else None
    if record is not None:
        display_port = display_port_for_record(record, service=service)
        resolved_port = port if port is not None else display_port
        role = role_override or role_for_service(getattr(record, "service", "") or "")
        skills = skills_override if skills_override is not None else skills_for_service(
            getattr(record, "service", "") or ""
        )
        endpoint = endpoint_override or f"http://{host}:{resolved_port}/a2a/send"
        card = AgentCard(name=instance, role=role, skills=list(skills), endpoint=endpoint)
        return card, resolved_port

    # no record — require full override set
    missing = []
    if role_override is None:
        missing.append("--role")
    if skills_override is None:
        missing.append("--skills")
    if endpoint_override is None:
        missing.append("--endpoint")
    if missing:
        console.print(
            f"[bold red]Error:[/bold red] instance '{instance}' not found. "
            f"Supply {', '.join(missing)} to run a virtual bridge."
        )
        raise typer.Exit(code=1)

    resolved_port = port if port is not None else DEFAULT_BRIDGE_PORT
    card = AgentCard(
        name=instance,
        role=role_override,
        skills=list(skills_override or []),
        endpoint=endpoint_override,
    )
    return card, resolved_port


@bridge_app.command("serve")
def bridge_serve(
    instance: Annotated[str, typer.Option("--instance", help="Instance name to represent.")],
    port: Annotated[
        int | None,
        typer.Option("--port", help="Port to bind. Default: the instance's display_port."),
    ] = None,
    host: Annotated[str, typer.Option("--host", help="Host interface to bind.")] = "127.0.0.1",
    mode: Annotated[
        BridgeMode,
        typer.Option("--mode", help="Bridge behaviour. 'echo' replies with a canned string."),
    ] = BridgeMode.echo,
    role: Annotated[
        str | None,
        typer.Option("--role", help="Override the role string in the served AgentCard."),
    ] = None,
    skills: Annotated[
        str | None,
        typer.Option("--skills", help="Comma-separated skills override for the AgentCard."),
    ] = None,
    endpoint: Annotated[
        str | None,
        typer.Option("--endpoint", help="Override the endpoint URL in the AgentCard."),
    ] = None,
) -> None:
    """Serve /.well-known/agent-card.json + /a2a/send for one instance.

    Demo / fallback only. When a real plugin runs inside the instance, the
    registry federates its self-reported card and this bridge is unused.
    """
    service: ClawCUService | None
    try:
        service = _get_service()
    except Exception:  # noqa: BLE001 — clawcu may be uninitialised in demo mode
        service = None
    card, resolved_port = _resolve_bridge_card(
        service=service,
        instance=instance,
        host=host,
        port=port,
        role_override=role,
        skills_override=_parse_skills(skills),
        endpoint_override=endpoint,
    )
    reply_fn = echo_reply  # only mode today; switch on BridgeMode when we grow more.
    assert mode is BridgeMode.echo
    console.print(
        f"[bold]A2A bridge[/bold] ({mode.value}) for '{instance}' "
        f"listening on http://{host}:{resolved_port}"
    )
    try:
        serve_bridge_forever(card, host=host, port=resolved_port, reply_fn=reply_fn)
    except KeyboardInterrupt:
        console.print("bridge: stopped")


@a2a_app.command("up")
def up_command(
    host: Annotated[str, typer.Option("--host", help="Host interface for all A2A servers.")] = "127.0.0.1",
    registry_port: Annotated[
        int, typer.Option("--registry-port", help="Port to bind the registry on.")
    ] = DEFAULT_REGISTRY_PORT,
    probe_timeout: Annotated[
        float, typer.Option("--probe-timeout", help="Plugin probe per-attempt timeout (seconds).")
    ] = 0.5,
    probe_attempts: Annotated[
        int, typer.Option("--probe-attempts", help="Plugin probe retry count.")
    ] = 3,
    probe_delay: Annotated[
        float, typer.Option("--probe-delay", help="Delay between probe attempts (seconds).")
    ] = 1.0,
) -> None:
    """Start echo bridges for every instance lacking a plugin, then the registry.

    One-command spin-up: probe each running instance at its display_port for
    a plugin-served AgentCard; start an echo bridge for those that don't
    have one; finally serve the registry in the foreground. Ctrl+C tears
    everything down.
    """
    service = _get_service()
    records = service.list_instances(running_only=True)
    bridge_servers: list[tuple[str, Any]] = []
    bridge_threads: list[threading.Thread] = []

    for record in records:
        card = detect_plugin_or_none(
            record,
            service=service,
            host=host,
            timeout=probe_timeout,
            attempts=probe_attempts,
            retry_delay=probe_delay,
        )
        if card is not None:
            console.print(f"[green]OK[/green] {record.name} (plugin-backed on :{display_port_for_record(record, service=service)})")
            continue

        echo_card = card_from_record(record, service=service, host=host)
        port = display_port_for_record(record, service=service)
        console.print(
            f"[yellow]WARN[/yellow] {record.name}: plugin not detected, "
            f"starting echo bridge on :{port}"
        )
        try:
            server = build_bridge_server(echo_card, host=host, port=port, reply_fn=echo_reply)
        except OSError as exc:
            console.print(
                f"[bold red]Error:[/bold red] {record.name}: cannot bind :{port} ({exc})."
            )
            _shutdown_servers(bridge_servers, bridge_threads)
            raise typer.Exit(code=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        bridge_servers.append((record.name, server))
        bridge_threads.append(thread)

    provider = make_cards_provider(service, host=host)
    console.print(
        f"[bold]A2A registry[/bold] listening on http://{host}:{registry_port} "
        f"(Ctrl+C to stop)"
    )
    try:
        serve_registry_forever(provider, host=host, port=registry_port)
    except KeyboardInterrupt:
        console.print("a2a up: stopping")
    finally:
        _shutdown_servers(bridge_servers, bridge_threads)


def _shutdown_servers(servers, threads) -> None:
    for name, server in servers:
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            console.print(f"[dim]warn: failed to stop bridge for {name}[/dim]")
    for thread in threads:
        thread.join(timeout=2)


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
