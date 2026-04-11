from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from clawcu import __version__
from clawcu.service import ClawCUService

app = typer.Typer(
    help="ClawCU manages local OpenClaw instances with versioned Docker workflows.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)
pull_app = typer.Typer(
    help="Pull and build managed services.",
    subcommand_metavar="SERVICE",
)
create_app = typer.Typer(
    help="Create managed services.",
    subcommand_metavar="SERVICE",
)
app.add_typer(pull_app, name="pull")
app.add_typer(create_app, name="create")
console = Console()


def get_service() -> ClawCUService:
    return ClawCUService()


def _exit_with_error(message: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=1)


def _show_help_and_exit(ctx: typer.Context) -> None:
    console.print(ctx.get_help(), end="")
    raise typer.Exit(code=0)


def _print_progress(message: str) -> None:
    console.print(f"[cyan]{message}[/cyan]")


def _print_instance_table(records: list[dict]) -> None:
    table = Table(title="ClawCU Instances")
    for column in ("NAME", "SERVICE", "VERSION", "PORT", "CPU", "MEMORY", "STATUS", "DATADIR"):
        table.add_column(column)
    for record in records:
        table.add_row(
            record["name"],
            record["service"],
            record["version"],
            str(record["port"]),
            str(record["cpu"]),
            record["memory"],
            record["status"],
            record["datadir"],
        )
    console.print(table)


@app.callback(invoke_without_command=True)
def root_callback(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the installed ClawCU version and exit."),
    ] = False,
) -> None:
    if version:
        console.print(f"clawcu {__version__}")
        raise typer.Exit()


@pull_app.callback(invoke_without_command=True)
def pull_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_help_and_exit(ctx)


@create_app.callback(invoke_without_command=True)
def create_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_help_and_exit(ctx)


@pull_app.command("openclaw")
def pull_openclaw(
    ctx: typer.Context,
    version: Annotated[str | None, typer.Option("--version", help="OpenClaw version to pull.")] = None,
) -> None:
    if not version:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        image_tag = service.pull_openclaw(version)
    except Exception as exc:  # pragma: no cover - exercised through tests via error path
        _exit_with_error(str(exc))
    console.print(f"[green]Built image:[/green] {image_tag}")


@create_app.command("openclaw")
def create_openclaw(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Option("--name", help="Managed instance name.")] = None,
    version: Annotated[str | None, typer.Option("--version", help="OpenClaw version to run.")] = None,
    datadir: Annotated[
        str | None,
        typer.Option("--datadir", help="Host data directory. Defaults to ~/.clawcu/{name}."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help="Host port exposed for the instance. Defaults to 18789, then probes 18799, 18809, ... until free.",
        ),
    ] = None,
    cpu: Annotated[str, typer.Option("--cpu", help="Docker CPU limit.")] = "1",
    memory: Annotated[str, typer.Option("--memory", help="Docker memory limit.")] = "2g",
) -> None:
    if not name or not version:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.create_openclaw(
            name=name,
            version=version,
            datadir=datadir,
            port=port,
            cpu=cpu,
            memory=memory,
        )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Created instance:[/green] {record.name} ({record.version}) on port {record.port}"
    )
@app.command("list")
def list_instances(
    running: Annotated[bool, typer.Option("--running", help="Only show running instances.")] = False,
) -> None:
    try:
        records = [record.to_dict() for record in get_service().list_instances(running_only=running)]
    except Exception as exc:
        _exit_with_error(str(exc))
    if not records:
        console.print("No managed instances found.")
        return
    _print_instance_table(records)


@app.command("inspect")
def inspect_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        payload = get_service().inspect_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print_json(json.dumps(payload, ensure_ascii=False))


@app.command("start")
def start_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        record = get_service().start_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Started instance:[/green] {record.name}")


@app.command("stop")
def stop_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        record = get_service().stop_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[yellow]Stopped instance:[/yellow] {record.name}")


@app.command("restart")
def restart_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        record = get_service().restart_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Restarted instance:[/green] {record.name}")


@app.command("retry")
def retry_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Failed instance name to retry.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.retry_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Retried instance:[/green] {record.name} ({record.version}) on port {record.port}")


@app.command("upgrade")
def upgrade_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    version: Annotated[str | None, typer.Option("--version", help="Target OpenClaw version.")] = None,
) -> None:
    if not name or not version:
        _show_help_and_exit(ctx)
    try:
        record = get_service().upgrade_instance(name, version=version)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Upgraded instance:[/green] {record.name} -> {record.version}")


@app.command("rollback")
def rollback_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        record = get_service().rollback_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Rolled back instance:[/green] {record.name} -> {record.version}")


@app.command("clone")
def clone_instance(
    ctx: typer.Context,
    source_name: Annotated[str | None, typer.Argument(help="Source instance name.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="New cloned instance name.")] = None,
    datadir: Annotated[str | None, typer.Option("--datadir", help="Target cloned data directory.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Target host port.")] = None,
) -> None:
    if not source_name or not all([name, datadir, port is not None]):
        _show_help_and_exit(ctx)
    try:
        record = get_service().clone_instance(
            source_name,
            name=name,
            datadir=datadir,
            port=port,
        )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Cloned instance:[/green] {source_name} -> {record.name}")


@app.command("logs")
def logs_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    follow: Annotated[bool, typer.Option("--follow", help="Follow the Docker log stream.")] = False,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        get_service().stream_logs(name, follow=follow)
    except Exception as exc:
        _exit_with_error(str(exc))


@app.command("remove")
def remove_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    delete_data: Annotated[
        bool,
        typer.Option(
            "--delete-data/--keep-data",
            help="Delete or preserve the instance data directory.",
        ),
    ] = False,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        get_service().remove_instance(name, delete_data=delete_data)
    except Exception as exc:
        _exit_with_error(str(exc))
    action = "and data directory" if delete_data else "but kept data directory"
    console.print(f"[green]Removed instance:[/green] {name} {action}")


def main() -> None:
    app()
