from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from click.shell_completion import get_completion_class
from rich.console import Console
from rich.table import Table
from typer.main import get_command

from clawcu import __version__
from clawcu.service import ClawCUService

app = typer.Typer(
    help="ClawCU manages local multi-agent instances with versioned Docker workflows.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
    add_completion=False,
)
pull_app = typer.Typer(
    help="Pull and build managed services.",
    subcommand_metavar="SERVICE",
    add_completion=False,
)
create_app = typer.Typer(
    help="Create managed services.",
    subcommand_metavar="SERVICE",
    add_completion=False,
)
provider_app = typer.Typer(
    help="Collect and reuse model configuration assets from managed instances and local homes.",
    add_completion=False,
)
provider_models_app = typer.Typer(
    help="Inspect the models stored in a collected provider.",
    add_completion=False,
)
app.add_typer(pull_app, name="pull")
app.add_typer(create_app, name="create")
app.add_typer(provider_app, name="provider")
provider_app.add_typer(provider_models_app, name="models")
console = Console()


def get_service() -> ClawCUService:
    return ClawCUService()


def _exit_with_error(message: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {message}")
    raise typer.Exit(code=1)


def _show_help_and_exit(ctx: typer.Context) -> None:
    console.print(ctx.get_help(), end="")
    raise typer.Exit(code=0)


def _show_passthrough_help(
    command: str,
    description: str,
    examples: list[str],
    *,
    usage: str | None = None,
) -> None:
    examples_text = "\n".join(f"  {example}" for example in examples)
    usage_text = usage or f"Usage: clawcu {command} [OPTIONS] [NAME] [-- OPENCLAW_ARGS...]"
    console.print(
        (
            f"{usage_text}\n\n"
            f"{description}\n\n"
            "Examples:\n"
            f"{examples_text}\n"
        )
    )
    raise typer.Exit(code=0)


def _print_progress(message: str) -> None:
    console.print(f"[cyan]{message}[/cyan]")


def _is_interactive_stdin() -> bool:
    return sys.stdin.isatty()


def _mask_secret(value: str) -> str:
    if not value:
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:6]}...{value[-4:]}"


def _redact_provider_payload(value):
    if isinstance(value, str):
        if "\n" in value and any(marker in value for marker in ("_KEY=", "_TOKEN=", "_SECRET=")):
            redacted_lines: list[str] = []
            for raw_line in value.splitlines():
                if "=" not in raw_line:
                    redacted_lines.append(raw_line)
                    continue
                key, item = raw_line.split("=", 1)
                upper_key = key.strip().upper()
                if any(token in upper_key for token in ("KEY", "TOKEN", "SECRET")):
                    redacted_lines.append(f"{key}={_mask_secret(item)}")
                else:
                    redacted_lines.append(raw_line)
            return "\n".join(redacted_lines)
        return value
    if isinstance(value, dict):
        redacted: dict = {}
        for key, item in value.items():
            if key in {"apiKey", "key"} and isinstance(item, str):
                redacted[key] = _mask_secret(item)
            else:
                redacted[key] = _redact_provider_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_payload(item) for item in value]
    return value


def _print_access_url(service: ClawCUService, name: str) -> None:
    try:
        url = service.dashboard_url(name)
    except Exception:
        return
    console.print(f"[blue]Open URL:[/blue] {url}")


def _print_instance_table(records: list[dict]) -> None:
    table = Table(title="ClawCU Instances")
    table.add_column("SOURCE", no_wrap=True)
    table.add_column("SERVICE", no_wrap=True)
    table.add_column("NAME", no_wrap=True)
    table.add_column("HOME", overflow="fold")
    table.add_column("VERSION", no_wrap=True)
    table.add_column("PORT", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("ACCESS", overflow="fold")
    table.add_column("PROVIDERS", overflow="fold")
    table.add_column("MODELS", overflow="fold")
    table.add_column("SNAPSHOT", overflow="fold")
    for record in records:
        table.add_row(
            record.get("source", "-"),
            record.get("service", "-"),
            record["name"],
            record.get("home", "-"),
            record["version"],
            str(record["port"]),
            record["status"],
            record.get("access_url", "-"),
            record.get("providers", "-"),
            record.get("models", "-"),
            record.get("snapshot", "-"),
        )
    console.print(table)


def _print_agent_table(records: list[dict]) -> None:
    table = Table(title="ClawCU Agents")
    table.add_column("SOURCE", no_wrap=True)
    table.add_column("SERVICE", no_wrap=True)
    table.add_column("INSTANCE", no_wrap=True)
    table.add_column("HOME", overflow="fold")
    table.add_column("AGENT", no_wrap=True)
    table.add_column("PRIMARY", overflow="fold")
    table.add_column("FALLBACKS", overflow="fold")
    for record in records:
        table.add_row(
            record.get("source", "-"),
            record.get("service", "-"),
            record["instance"],
            record.get("home", "-"),
            record["agent"],
            record.get("primary", "-"),
            record.get("fallbacks", "-"),
        )
    console.print(table)


def _print_provider_table(records: list[dict]) -> None:
    table = Table(title="ClawCU Providers")
    for column in ("SERVICE", "NAME", "PROVIDER", "API_STYLE", "API_KEY", "ENDPOINT", "MODELS"):
        table.add_column(column)
    for record in records:
        table.add_row(
            record.get("service", "-"),
            record["name"],
            record.get("provider") or "-",
            record["api_style"],
            _mask_secret(str(record.get("api_key") or "")) or "-",
            record.get("endpoint") or "-",
            ", ".join(record.get("models", [])) or "-",
        )
    console.print(table)


def _print_setup_checks(checks: list[dict[str, str | bool]]) -> bool:
    all_ok = True
    for check in checks:
        status_name = str(check.get("status", "") or "").lower()
        if not status_name:
            status_name = "ok" if bool(check.get("ok")) else "fail"
        if status_name == "ok":
            status = "[green]OK[/green]"
        elif status_name == "warn":
            status = "[yellow]WARN[/yellow]"
        else:
            status = "[bold red]FAIL[/bold red]"
        console.print(f"{status} {check['summary']}")
        hint = str(check.get("hint", "") or "").strip()
        details = str(check.get("details", "") or "").strip()
        if status_name == "fail":
            all_ok = False
        if hint:
            console.print(f"  Hint: {hint}")
        if details:
            console.print(f"  Details: {details}")
    return all_ok


def _detect_shell_name() -> str | None:
    shell_path = os.environ.get("SHELL", "").strip()
    shell_name = Path(shell_path).name.lower()
    if shell_name in {"zsh", "bash", "fish"}:
        return shell_name
    return None


def _completion_check(service: ClawCUService) -> dict[str, str | bool]:
    shell_name = _detect_shell_name()
    if not shell_name:
        return {
            "name": "shell_completion",
            "status": "warn",
            "summary": "Shell completion could not be checked automatically because the current shell is not recognized.",
            "hint": "Use zsh, bash, or fish if you want ClawCU to guide completion setup.",
        }

    completion_class = get_completion_class(shell_name)
    if completion_class is None:
        return {
            "name": "shell_completion",
            "status": "warn",
            "summary": f"Shell completion for {shell_name} is not supported by the current Click runtime.",
            "hint": "Use zsh, bash, or fish for built-in completion support.",
        }

    command = get_command(app)
    completion = completion_class(command, {}, "clawcu", "_CLAWCU_COMPLETE")
    script = completion.source()
    completion_dir = service.store.paths.home / "completions"
    completion_dir.mkdir(parents=True, exist_ok=True)

    if shell_name == "zsh":
        script_path = completion_dir / "_clawcu"
        rc_path = Path.home() / ".zshrc"
        install_hint = (
            f"Add this to {rc_path}: "
            f"fpath=({completion_dir} $fpath) && autoload -Uz compinit && compinit"
        )
        configured = rc_path.exists() and (
            str(completion_dir) in rc_path.read_text(encoding="utf-8", errors="ignore")
            or "_clawcu" in rc_path.read_text(encoding="utf-8", errors="ignore")
        )
    elif shell_name == "bash":
        script_path = completion_dir / "clawcu.bash"
        rc_path = Path.home() / ".bashrc"
        install_hint = f"Add this to {rc_path}: source {script_path}"
        configured = rc_path.exists() and (
            str(script_path) in rc_path.read_text(encoding="utf-8", errors="ignore")
            or "clawcu.bash" in rc_path.read_text(encoding="utf-8", errors="ignore")
        )
    else:
        script_path = Path.home() / ".config" / "fish" / "completions" / "clawcu.fish"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        rc_path = None
        install_hint = ""
        configured = True

    script_path.write_text(script, encoding="utf-8")
    if configured:
        return {
            "name": "shell_completion",
            "status": "ok",
            "summary": f"Shell completion script is ready for {shell_name} at {script_path}.",
            "hint": "",
        }

    return {
        "name": "shell_completion",
        "status": "warn",
        "summary": f"Shell completion script is ready for {shell_name} at {script_path}, but your shell profile does not appear to load it yet.",
        "hint": install_hint,
    }


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


@app.command("setup", help="Check local prerequisites and configure the default ClawCU home and service sources.")
def setup_environment(
    completion: Annotated[
        bool,
        typer.Option("--completion", help="Also show shell completion guidance."),
    ] = False,
) -> None:
    console.print("Checking local prerequisites for ClawCU...")
    service = get_service()
    checks = service.check_setup()
    if completion:
        checks.append(_completion_check(service))
    if _print_setup_checks(checks):
        if _is_interactive_stdin():
            configured_home = typer.prompt(
                "ClawCU home",
                default=service.get_clawcu_home(),
            ).strip()
            saved_home = service.set_clawcu_home(configured_home)
            console.print(f"[green]Saved ClawCU home:[/green] {saved_home}")
            configured_repo = typer.prompt(
                "OpenClaw image repo",
                default=service.suggest_openclaw_image_repo(),
            ).strip()
            saved_repo = service.set_openclaw_image_repo(configured_repo)
            console.print(f"[green]Saved OpenClaw image repo:[/green] {saved_repo}")
            configured_hermes_repo = typer.prompt(
                "Hermes source repo",
                default=service.get_hermes_source_repo(),
            ).strip()
            saved_hermes_repo = service.set_hermes_source_repo(configured_hermes_repo)
            console.print(f"[green]Saved Hermes source repo:[/green] {saved_hermes_repo}")
            configured_hermes_proxy = typer.prompt(
                "Hermes build proxy (optional)",
                default=service.get_hermes_proxy(),
            ).strip()
            saved_hermes_proxy = service.set_hermes_proxy(configured_hermes_proxy)
            if saved_hermes_proxy:
                console.print(f"[green]Saved Hermes build proxy:[/green] {saved_hermes_proxy}")
            else:
                console.print("[green]Hermes build proxy:[/green] not configured")
        console.print("[green]ClawCU setup check passed.[/green] Docker and the ClawCU runtime layout are ready.")
        return
    raise typer.Exit(code=1)


@pull_app.callback(invoke_without_command=True)
def pull_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_help_and_exit(ctx)


@create_app.callback(invoke_without_command=True)
def create_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_help_and_exit(ctx)


@provider_app.callback(invoke_without_command=True)
def provider_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_help_and_exit(ctx)


@provider_models_app.callback(invoke_without_command=True)
def provider_models_callback(ctx: typer.Context) -> None:
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


@pull_app.command("hermes")
def pull_hermes(
    ctx: typer.Context,
    version: Annotated[str | None, typer.Option("--version", help="Hermes git ref to pull and build.")] = None,
) -> None:
    if not version:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        image_tag = service.pull_hermes(version)
    except Exception as exc:
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
        f"[green]Created instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
    )
    _print_access_url(service, record.name)


@create_app.command("hermes")
def create_hermes(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Option("--name", help="Managed instance name.")] = None,
    version: Annotated[str | None, typer.Option("--version", help="Hermes git ref to run.")] = None,
    datadir: Annotated[
        str | None,
        typer.Option("--datadir", help="Host data directory. Defaults to ~/.clawcu/{name}."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            help="Host port exposed for the instance. Defaults to 8642, then probes 8652, 8662, ... until free.",
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
        record = service.create_hermes(
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
        f"[green]Created instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
    )
    _print_access_url(service, record.name)


@provider_app.command("collect", help="Collect model configuration assets from managed instances or local agent homes.")
def collect_providers(
    ctx: typer.Context,
    all_instances: Annotated[
        bool,
        typer.Option("--all", help="Collect model configs from all ClawCU-managed instances plus local ~/.openclaw and ~/.hermes when present."),
    ] = False,
    instance: Annotated[
        str | None,
        typer.Option("--instance", help="Collect providers from a specific managed instance."),
    ] = None,
    path: Annotated[
        str | None,
        typer.Option("--path", help="Collect model configs from an external OpenClaw or Hermes home directory."),
    ] = None,
) -> None:
    if not all_instances and not instance and not path:
        _show_help_and_exit(ctx)
    try:
        result = get_service().collect_providers(
            all_instances=all_instances,
            instance=instance,
            path=path,
        )
    except Exception as exc:
        _exit_with_error(str(exc))
    for saved in result["saved"]:
        console.print(f"[green]Collected provider:[/green] {saved}")
    for merged in result.get("merged", []):
        console.print(f"[blue]Merged duplicate:[/blue] {merged}")
    for skipped in result["skipped"]:
        console.print(f"[yellow]Skipped duplicate:[/yellow] {skipped}")
    saved_count = len(result["saved"])
    merged_count = len(result.get("merged", []))
    skipped_count = len(result["skipped"])
    scanned_count = len(result.get("scanned", []))
    if not saved_count and not merged_count and not skipped_count:
        console.print("No provider assets were found.")
        return
    console.print(
        "Collect summary: "
        f"scanned {scanned_count} source(s), "
        f"collected {saved_count}, "
        f"merged {merged_count}, "
        f"skipped {skipped_count}."
    )


@provider_app.command("list", help="List all collected provider assets.")
def list_providers() -> None:
    try:
        records = get_service().list_providers()
    except Exception as exc:
        _exit_with_error(str(exc))
    if not records:
        console.print("No providers found.")
        return
    _print_provider_table(records)


@provider_app.command("show", help="Show the collected auth-profiles.json and models.json for a provider.")
def show_provider(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Provider name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        payload = get_service().show_provider(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print_json(json.dumps(_redact_provider_payload(payload), ensure_ascii=False))


@provider_app.command("apply", help="Apply a collected provider to a managed instance agent.")
def apply_provider(
    ctx: typer.Context,
    provider: Annotated[str | None, typer.Argument(help="Collected provider name.")] = None,
    instance: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    agent: Annotated[str, typer.Option("--agent", help="Target agent name. Defaults to main.")] = "main",
    persist: Annotated[
        bool,
        typer.Option(
            "--persist",
            help="Also persist the provider secret to the instance env file and write an env reference into root openclaw.json.",
        ),
    ] = False,
    primary: Annotated[str | None, typer.Option("--primary", help="Set the agent primary model.")] = None,
    fallbacks: Annotated[
        str | None,
        typer.Option("--fallbacks", help="Comma-separated fallback model list for the agent."),
    ] = None,
) -> None:
    if not provider or not instance:
        _show_help_and_exit(ctx)
    fallback_list = None
    if fallbacks is not None:
        fallback_list = [item.strip() for item in fallbacks.split(",") if item.strip()]
    try:
        result = get_service().apply_provider(
            provider,
            instance,
            agent,
            persist=persist,
            primary=primary,
            fallbacks=fallback_list,
        )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Applied provider:[/green] {result['provider']} -> {result['instance']}/{result['agent']}"
    )
    if persist:
        if result.get("env_key") and result.get("env_key") != "-":
            console.print(
                f"Persistence: config now uses [blue]${{{result.get('env_key', '-')}}}[/blue] and the secret was stored in the instance env file."
            )
        elif result.get("env_path"):
            console.print(
                f"Persistence: config and env were updated in [blue]{result['env_path']}[/blue]."
            )
    if primary or fallback_list is not None:
        console.print(
            "Agent models: "
            f"primary={result.get('primary', '-')} "
            f"fallbacks={result.get('fallbacks', '-')}"
        )


@provider_app.command("remove", help="Remove a collected provider directory.")
def remove_provider(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Provider name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        get_service().remove_provider(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[yellow]Removed provider:[/yellow] {name}")


@provider_models_app.command("list", help="List the models stored in a collected provider.")
def list_provider_models(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Provider name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        models = get_service().list_provider_models(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    if not models:
        console.print("No models configured.")
        return
    for model in models:
        console.print(model)
@app.command("list", help="List all managed instances and their current status.")
def list_instances(
    running: Annotated[bool, typer.Option("--running", help="Only show running instances.")] = False,
    agents: Annotated[bool, typer.Option("--agents", help="Expand the list to one row per agent.")] = False,
    local: Annotated[bool, typer.Option("--local", help="Show the local ~/.openclaw overview.")] = False,
    managed: Annotated[bool, typer.Option("--managed", help="Show ClawCU-managed instances instead of ~/.openclaw.")] = False,
    all_sources: Annotated[bool, typer.Option("--all", help="Show both local ~/.openclaw and ClawCU-managed entries.")] = False,
) -> None:
    try:
        service = get_service()
        use_all = all_sources or (not local and not managed)
        records: list[dict]
        if agents:
            records = []
            if use_all or local:
                records.extend(service.list_local_agent_summaries())
            if use_all or managed:
                records.extend(service.list_agent_summaries(running_only=running))
        else:
            records = []
            if use_all or local:
                records.extend(service.list_local_instance_summaries())
            if use_all or managed:
                records.extend(service.list_instance_summaries(running_only=running))
    except Exception as exc:
        _exit_with_error(str(exc))
    if not records:
        if agents:
            console.print("No agents found.")
        else:
            console.print("No instances found.")
        return
    if agents:
        _print_agent_table(records)
    else:
        _print_instance_table(records)


@app.command("inspect", help="Show detailed state for a managed instance.")
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


@app.command("token", help="Print the dashboard token for a managed instance.")
def token_for_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        token = get_service().token(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(token)


@app.command("setenv", help="Set environment variables for a managed instance.")
def set_instance_env(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    assignments: Annotated[list[str] | None, typer.Argument(help="One or more KEY=VALUE assignments.")] = None,
    apply_now: Annotated[
        bool,
        typer.Option("--apply", help="Recreate the instance immediately so the new env takes effect now."),
    ] = False,
) -> None:
    if not name or not assignments:
        _show_help_and_exit(ctx)
    service = get_service()
    if apply_now and hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        result = service.set_instance_env(name, list(assignments))
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Updated env file:[/green] {result['path']} ({', '.join(result['updated_keys'])})"
    )
    if apply_now:
        try:
            record = service.recreate_instance(str(result["instance"]))
        except Exception as exc:
            _exit_with_error(
                f"Environment variables were written, but recreate failed: {exc}"
            )
        console.print(
            f"[green]Recreated instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
        )
        _print_access_url(service, record.name)
        return
    console.print(
        "Changes will apply the next time the container is recreated. "
        f"Run `clawcu recreate {result['instance']}` if you want them to take effect now."
    )


@app.command("getenv", help="List environment variables configured for a managed instance.")
def get_instance_env(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        result = get_service().get_instance_env(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    values = result.get("values", {})
    if not isinstance(values, dict) or not values:
        console.print("No environment variables configured.")
        return
    for key in sorted(values):
        console.print(f"{key}={values[key]}")


@app.command("unsetenv", help="Remove environment variables configured for a managed instance.")
def unset_instance_env(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    keys: Annotated[list[str] | None, typer.Argument(help="One or more environment variable names.")] = None,
    apply_now: Annotated[
        bool,
        typer.Option("--apply", help="Recreate the instance immediately so the env change takes effect now."),
    ] = False,
) -> None:
    if not name or not keys:
        _show_help_and_exit(ctx)
    service = get_service()
    if apply_now and hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        result = service.unset_instance_env(name, list(keys))
    except Exception as exc:
        _exit_with_error(str(exc))
    removed_keys = result.get("removed_keys", [])
    if removed_keys:
        console.print(
            f"[green]Updated env file:[/green] {result['path']} (removed: {', '.join(removed_keys)})"
        )
    else:
        console.print(f"[yellow]No matching env keys were removed:[/yellow] {result['path']}")
    if apply_now:
        try:
            record = service.recreate_instance(str(result["instance"]))
        except Exception as exc:
            _exit_with_error(
                f"Environment variables were updated, but recreate failed: {exc}"
            )
        console.print(
            f"[green]Recreated instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
        )
        _print_access_url(service, record.name)
        return
    console.print(
        "Changes will apply the next time the container is recreated. "
        f"Run `clawcu recreate {result['instance']}` if you want them to take effect now."
    )


@app.command("approve", help="Approve a pending browser pairing request for an instance.")
def approve_pairing(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    request_id: Annotated[str | None, typer.Argument(help="Specific pairing request id to approve.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        approved_request_id = service.approve_pairing(name, request_id=request_id)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Approved pairing:[/green] {approved_request_id} for {name}")
    _print_access_url(service, name)


@app.command(
    "config",
    help="Run the native configuration flow inside a managed instance.",
    add_help_option=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def configure_instance(
    ctx: typer.Context,
    help_flag: Annotated[
        bool,
        typer.Option("--help", "-h", help="Show passthrough usage and examples.", is_eager=True),
    ] = False,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if help_flag or not name:
        _show_passthrough_help(
            "config",
            "This command runs the service-native configuration flow inside the managed instance container.",
            [
                "clawcu config <instance>",
                "clawcu config <instance> -- --section model",
                "clawcu config <instance> -- --help",
            ],
        )
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    extra_args = list(ctx.args)
    try:
        service.configure_instance(name, extra_args=extra_args)
    except Exception as exc:
        _exit_with_error(str(exc))


@app.command(
    "exec",
    help="Run an arbitrary command inside a managed instance container.",
    add_help_option=False,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def exec_instance(
    ctx: typer.Context,
    help_flag: Annotated[
        bool,
        typer.Option("--help", "-h", help="Show passthrough usage and examples.", is_eager=True),
    ] = False,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if help_flag or not name or not ctx.args:
        _show_passthrough_help(
            "exec",
            "This command runs the provided command inside the managed instance container.",
            [
                "clawcu exec <instance> openclaw config",
                "clawcu exec <instance> pwd",
                "clawcu exec <instance> ls",
            ],
            usage="Usage: clawcu exec [OPTIONS] [NAME] COMMAND [ARGS]...",
        )
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    extra_args = list(ctx.args)
    try:
        service.exec_instance(name, extra_args)
    except Exception as exc:
        _exit_with_error(str(exc))


@app.command(
    "tui",
    help="Launch the native interactive TUI or chat flow for a managed instance.",
)
def tui_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    agent: Annotated[
        str,
        typer.Option("--agent", help="Target agent name. Defaults to main."),
    ] = "main",
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        service.tui_instance(name, agent=agent)
    except Exception as exc:
        _exit_with_error(str(exc))


@app.command("start", help="Start a stopped managed instance.")
def start_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    try:
        record = service.start_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Started instance:[/green] {record.name}")
    _print_access_url(service, record.name)


@app.command("stop", help="Stop a running managed instance.")
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


@app.command("restart", help="Restart a managed instance.")
def restart_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    try:
        record = service.restart_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Restarted instance:[/green] {record.name}")
    _print_access_url(service, record.name)


@app.command("retry", help="Retry creating an instance that is in create_failed status.")
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
    console.print(
        f"[green]Retried instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
    )
    _print_access_url(service, record.name)


@app.command("recreate", help="Recreate an existing instance with its saved configuration.")
def recreate_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Instance to recreate.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.recreate_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Recreated instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
    )
    _print_access_url(service, record.name)


@app.command(
    "upgrade",
    help="Upgrade an instance to a newer service version with a safety snapshot of its data directory and env file.",
)
def upgrade_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    version: Annotated[str | None, typer.Option("--version", help="Target service version or git ref.")] = None,
) -> None:
    if not name or not version:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.upgrade_instance(name, version=version)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Upgraded instance:[/green] {record.name} -> {record.version}")
    _print_access_url(service, record.name)


@app.command(
    "rollback",
    help="Roll an instance back to its previous version and restore the matching data-directory and env snapshot.",
)
def rollback_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.rollback_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Rolled back instance:[/green] {record.name} -> {record.version}")
    _print_access_url(service, record.name)


@app.command("clone", help="Clone an existing instance into a separate experiment instance.")
def clone_instance(
    ctx: typer.Context,
    source_name: Annotated[str | None, typer.Argument(help="Source instance name.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="New cloned instance name.")] = None,
    datadir: Annotated[str | None, typer.Option("--datadir", help="Target cloned data directory.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Target host port.")] = None,
) -> None:
    if not source_name or not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.clone_instance(
            source_name,
            name=name,
            datadir=datadir,
            port=port,
        )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Cloned instance:[/green] {source_name} -> {record.name}")
    _print_access_url(service, record.name)


@app.command("logs", help="Stream or print Docker logs for a managed instance.")
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


@app.command("remove", help="Remove an instance and optionally delete its data directory.")
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
