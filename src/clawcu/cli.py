from __future__ import annotations

import json
import os
import re
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
_DISPLAY_DATE_RE = re.compile(r"(\d{4}\.\d{1,2}\.\d{1,2})")


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


_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SENSITIVE_ENV_MARKERS)


def _mask_env_value(key: str, value: str, *, reveal: bool) -> str:
    if reveal:
        return value
    if not _is_sensitive_env_key(key):
        return value
    return _mask_secret(value) if value else value


def _parse_env_file(path: Path) -> list[tuple[str, str]]:
    """Parse a .env-style file into an ordered list of (key, value) pairs.

    Rules: skip blank lines and ``#`` comments; lines without ``=`` are
    ignored. Leading/trailing whitespace around keys is stripped. Values
    are kept verbatim (trailing whitespace preserved is a user choice we
    do not second-guess). Duplicate keys follow last-write-wins.
    """
    if not path.exists():
        raise typer.BadParameter(f"env file not found: {path}")
    if path.is_dir():
        raise typer.BadParameter(f"env file is a directory: {path}")
    text = path.read_text(encoding="utf-8")
    pairs: dict[str, str] = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        pairs[key] = value
    return list(pairs.items())


def _render_env_diff(
    before: dict[str, str],
    after: dict[str, str],
    *,
    reveal: bool,
) -> None:
    """Print a colored before/after diff for env changes.

    ``after`` is the projected state (post-apply). Keys present in
    ``before`` but not ``after`` are treated as removals.
    """
    before_keys = set(before)
    after_keys = set(after)
    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)
    kept = sorted(after_keys & before_keys)

    updated: list[str] = []
    unchanged: list[str] = []
    for key in kept:
        if before[key] != after[key]:
            updated.append(key)
        else:
            unchanged.append(key)

    if not (added or removed or updated):
        console.print("[dim]No changes.[/dim]")
        return

    for key in added:
        value = _mask_env_value(key, after[key], reveal=reveal)
        console.print(f"[green]+ {key}={value}[/green]")
    for key in updated:
        old_value = _mask_env_value(key, before[key], reveal=reveal)
        new_value = _mask_env_value(key, after[key], reveal=reveal)
        console.print(f"[yellow]~ {key}: {old_value} -> {new_value}[/yellow]")
    for key in removed:
        value = _mask_env_value(key, before[key], reveal=reveal)
        console.print(f"[red]- {key}={value}[/red]")

    if unchanged:
        console.print(f"[dim]({len(unchanged)} unchanged)[/dim]")


def _strip_token_fragment(url: str) -> str:
    if not url or "#" not in url:
        return url
    base, _, fragment = url.partition("#")
    if "token=" in fragment.lower():
        return base
    return url


def _confirm_destructive(summary: str, yes: bool) -> None:
    """Prompt for confirmation before a destructive action.

    If ``yes`` is True, skip the prompt. If stdin is not a TTY and
    ``yes`` is not set, abort with a clear error — refusing to silently
    proceed in non-interactive contexts.
    """
    if yes:
        return
    if not _is_interactive_stdin():
        _exit_with_error(
            f"{summary}\n"
            "Refusing destructive action in non-interactive shell. "
            "Re-run with --yes to confirm."
        )
    console.print(f"[yellow]{summary}[/yellow]")
    if not typer.confirm("Proceed?", default=False):
        console.print("Aborted.")
        raise typer.Exit(code=1)


def _display_version(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "-"
    date_match = _DISPLAY_DATE_RE.search(raw)
    if date_match:
        return date_match.group(1)
    return raw


def _redact_provider_payload(value, *, reveal: bool = False):
    if reveal:
        return value
    if isinstance(value, str):
        if "\n" in value and any(marker in value for marker in ("_KEY=", "_TOKEN=", "_SECRET=")):
            redacted_lines: list[str] = []
            for raw_line in value.splitlines():
                if "=" not in raw_line:
                    redacted_lines.append(raw_line)
                    continue
                key, item = raw_line.split("=", 1)
                if _is_sensitive_env_key(key.strip()):
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
                redacted[key] = _redact_provider_payload(item, reveal=reveal)
        return redacted
    if isinstance(value, list):
        return [_redact_provider_payload(item, reveal=reveal) for item in value]
    return value


def _print_access_url(service: ClawCUService, name: str) -> None:
    try:
        url = service.dashboard_url(name)
    except Exception:
        return
    console.print(f"[blue]Open URL:[/blue] {url}")


_ACCESS_AUTHORITY_RE = re.compile(r"^\w+://([^/#?]+)")


def _access_host_port(access_url: str) -> str:
    """Extract the host:port portion of an access URL for compact display."""
    if not access_url or access_url == "-":
        return "-"
    match = _ACCESS_AUTHORITY_RE.match(access_url)
    if match:
        return match.group(1)
    return access_url


def _print_instance_table(records: list[dict], *, wide: bool = False, reveal: bool = False) -> None:
    table = Table(title="ClawCU Instances")
    # Narrow default: NAME / SERVICE / VERSION / PORT / STATUS / ACCESS (host:port only).
    # Wide adds SOURCE / HOME / PROVIDERS / MODELS / SNAPSHOT and shows full URL.
    if wide:
        table.add_column("SOURCE", no_wrap=True)
    table.add_column("NAME", no_wrap=True)
    table.add_column("SERVICE", no_wrap=True)
    if wide:
        table.add_column("HOME", overflow="fold")
    table.add_column("VERSION", no_wrap=True)
    table.add_column("PORT", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("ACCESS", overflow="fold")
    if wide:
        table.add_column("PROVIDERS", overflow="fold")
        table.add_column("MODELS", overflow="fold")
        table.add_column("SNAPSHOT", overflow="fold")
    for record in records:
        access_url = record.get("access_url", "-")
        if not reveal:
            access_url = _strip_token_fragment(access_url)
        if not wide:
            access_cell = _access_host_port(access_url)
        else:
            access_cell = access_url
        row: list[str] = []
        if wide:
            row.append(record.get("source", "-"))
        row.append(record["name"])
        row.append(record.get("service", "-"))
        if wide:
            row.append(record.get("home", "-"))
        row.append(_display_version(record["version"]))
        row.append(str(record["port"]))
        row.append(record["status"])
        row.append(access_cell)
        if wide:
            row.append(record.get("providers", "-"))
            row.append(record.get("models", "-"))
            row.append(record.get("snapshot", "-"))
        table.add_row(*row)
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


def _print_provider_table(records: list[dict], *, wide: bool = False, reveal: bool = False) -> None:
    table = Table(title="ClawCU Providers")
    table.add_column("SERVICE", no_wrap=True)
    table.add_column("NAME", no_wrap=True)
    if wide:
        table.add_column("PROVIDER", no_wrap=True)
    table.add_column("API_STYLE", no_wrap=True)
    table.add_column("API_KEY", no_wrap=True)
    if wide:
        table.add_column("ENDPOINT", overflow="fold")
    table.add_column("MODELS", overflow="fold")
    for record in records:
        raw_key = str(record.get("api_key") or "")
        if reveal:
            key_cell = raw_key or "-"
        elif raw_key:
            key_cell = "[green]set[/green]" if not wide else (_mask_secret(raw_key) or "-")
        else:
            key_cell = "[dim]unset[/dim]"
        models = record.get("models") or []
        if wide:
            models_cell = ", ".join(models) or "-"
        else:
            models_cell = f"{len(models)} models" if models else "-"
        row: list[str] = []
        row.append(record.get("service", "-"))
        row.append(record["name"])
        if wide:
            row.append(record.get("provider") or "-")
        row.append(record["api_style"])
        row.append(key_cell)
        if wide:
            row.append(record.get("endpoint") or "-")
        row.append(models_cell)
        table.add_row(*row)
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


_OUTPUT_STATE: dict[str, bool] = {"json": False}


def _json_mode() -> bool:
    return bool(_OUTPUT_STATE.get("json"))


def _set_json_mode(enabled: bool) -> None:
    """Merge a per-command --json flag into the global output state.

    Commands that accept a local ``--json`` option call this at entry,
    so both ``clawcu --json inspect foo`` and ``clawcu inspect foo --json``
    reach ``_json_mode() == True`` by the time the render path runs.
    """
    if enabled:
        _OUTPUT_STATE["json"] = True


def _print_json(payload) -> None:
    """Emit a machine-readable JSON payload to stdout."""
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))


_JSON_OPTION = typer.Option(
    "--json",
    help="Emit machine-readable JSON instead of the default table/view.",
)


@app.callback(invoke_without_command=True)
def root_callback(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the installed ClawCU version and exit."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help=(
                "Emit machine-readable JSON where supported (list, inspect, token, getenv, provider list/models). "
                "Also accepted as a per-command flag (e.g. `clawcu inspect <name> --json`)."
            ),
        ),
    ] = False,
) -> None:
    # Only overwrite if set, so a per-command --json that runs later can still
    # toggle the state without this callback clobbering it back to False.
    if json_output:
        _OUTPUT_STATE["json"] = True
    else:
        _OUTPUT_STATE["json"] = False
    if version:
        if json_output:
            _print_json({"clawcu": __version__})
        else:
            console.print(f"clawcu {__version__}")
        raise typer.Exit()


@app.command("setup", help="Check local prerequisites and configure the default ClawCU home and service image repos.")
def setup_environment(
    completion: Annotated[
        bool,
        typer.Option("--completion", help="Also show shell completion guidance."),
    ] = False,
    clawcu_home: Annotated[
        str | None,
        typer.Option("--clawcu-home", help="Save the default ClawCU home without prompting."),
    ] = None,
    openclaw_image_repo: Annotated[
        str | None,
        typer.Option("--openclaw-image-repo", help="Save the default OpenClaw image repo without prompting."),
    ] = None,
    hermes_image_repo: Annotated[
        str | None,
        typer.Option("--hermes-image-repo", help="Save the default Hermes image repo without prompting."),
    ] = None,
) -> None:
    console.print("Checking local prerequisites for ClawCU...")
    service = get_service()
    checks = service.check_setup()
    if completion:
        checks.append(_completion_check(service))
    if _print_setup_checks(checks):
        is_interactive = _is_interactive_stdin()
        has_explicit_config = any(
            value is not None
            for value in (
                clawcu_home,
                openclaw_image_repo,
                hermes_image_repo,
            )
        )
        if is_interactive or has_explicit_config:
            configured_home = clawcu_home
            if configured_home is None and is_interactive:
                configured_home = typer.prompt(
                    "ClawCU home",
                    default=service.get_clawcu_home(),
                ).strip()
            elif configured_home is None:
                configured_home = service.get_clawcu_home()
            saved_home = service.set_clawcu_home(configured_home)
            console.print(f"[green]Saved ClawCU home:[/green] {saved_home}")
            configured_repo = openclaw_image_repo
            if configured_repo is None and is_interactive:
                configured_repo = typer.prompt(
                    "OpenClaw image repo",
                    default=service.suggest_openclaw_image_repo(),
                ).strip()
            elif configured_repo is None:
                configured_repo = service.get_openclaw_image_repo()
            saved_repo = service.set_openclaw_image_repo(configured_repo)
            console.print(f"[green]Saved OpenClaw image repo:[/green] {saved_repo}")
            configured_hermes_repo = hermes_image_repo
            if configured_hermes_repo is None and is_interactive:
                configured_hermes_repo = typer.prompt(
                    "Hermes image repo",
                    default=service.get_hermes_image_repo(),
                ).strip()
            elif configured_hermes_repo is None:
                configured_hermes_repo = service.get_hermes_image_repo()
            saved_hermes_repo = service.set_hermes_image_repo(configured_hermes_repo)
            console.print(f"[green]Saved Hermes image repo:[/green] {saved_hermes_repo}")
        elif not is_interactive:
            console.print(
                "[yellow]Non-interactive shell detected.[/yellow] "
                "Pass setup options such as "
                "`--clawcu-home`, `--openclaw-image-repo`, or `--hermes-image-repo` to save config without prompts."
            )
        console.print("[green]ClawCU setup check passed.[/green] Docker and the ClawCU runtime layout are ready.")
        return
    raise typer.Exit(code=1)


_KNOWN_SERVICES = ("openclaw", "hermes")


def _do_pull(service_name: str, version: str) -> None:
    if service_name not in _KNOWN_SERVICES:
        _exit_with_error(
            f"Unknown service '{service_name}'. Expected one of: {', '.join(_KNOWN_SERVICES)}."
        )
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        if service_name == "openclaw":
            image_tag = service.pull_openclaw(version)
        else:
            image_tag = service.pull_hermes(version)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Built image:[/green] {image_tag}")


def _do_create(
    service_name: str,
    *,
    name: str,
    version: str,
    datadir: str | None,
    port: int | None,
    cpu: str,
    memory: str,
) -> None:
    if service_name not in _KNOWN_SERVICES:
        _exit_with_error(
            f"Unknown service '{service_name}'. Expected one of: {', '.join(_KNOWN_SERVICES)}."
        )
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        if service_name == "openclaw":
            record = service.create_openclaw(
                name=name, version=version, datadir=datadir, port=port, cpu=cpu, memory=memory,
            )
        else:
            record = service.create_hermes(
                name=name, version=version, datadir=datadir, port=port, cpu=cpu, memory=memory,
            )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Created instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
    )
    _print_access_url(service, record.name)


@pull_app.callback(invoke_without_command=True)
def pull_callback(
    ctx: typer.Context,
    service: Annotated[
        str | None,
        typer.Option("--service", help=f"Service name ({' | '.join(_KNOWN_SERVICES)}). Unified alternative to the 'clawcu pull <service>' subcommand form."),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Service version or git ref."),
    ] = None,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if service:
        if not version:
            _exit_with_error("--service requires --version.")
        _do_pull(service, version)
        return
    _show_help_and_exit(ctx)


@create_app.callback(invoke_without_command=True)
def create_callback(
    ctx: typer.Context,
    service: Annotated[
        str | None,
        typer.Option("--service", help=f"Service name ({' | '.join(_KNOWN_SERVICES)}). Unified alternative to the 'clawcu create <service>' subcommand form."),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Managed instance name.")] = None,
    version: Annotated[str | None, typer.Option("--version", help="Service version or git ref.")] = None,
    datadir: Annotated[
        str | None,
        typer.Option("--datadir", help="Host data directory. Defaults to ~/.clawcu/{name}."),
    ] = None,
    port: Annotated[int | None, typer.Option("--port", help="Host port exposed for the instance.")] = None,
    cpu: Annotated[str, typer.Option("--cpu", help="Docker CPU limit.")] = "1",
    memory: Annotated[str, typer.Option("--memory", help="Docker memory limit.")] = "2g",
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if service:
        if not name or not version:
            _exit_with_error("--service requires --name and --version.")
        _do_create(
            service, name=name, version=version, datadir=datadir, port=port, cpu=cpu, memory=memory,
        )
        return
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
    _do_pull("openclaw", version)


@pull_app.command("hermes")
def pull_hermes(
    ctx: typer.Context,
    version: Annotated[str | None, typer.Option("--version", help="Hermes git ref to pull and build.")] = None,
) -> None:
    if not version:
        _show_help_and_exit(ctx)
    _do_pull("hermes", version)


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
    _do_create("openclaw", name=name, version=version, datadir=datadir, port=port, cpu=cpu, memory=memory)


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
    _do_create("hermes", name=name, version=version, datadir=datadir, port=port, cpu=cpu, memory=memory)


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
def list_providers(
    wide: Annotated[bool, typer.Option("--wide", help="Show all columns (PROVIDER, ENDPOINT, full model list).")] = False,
    reveal: Annotated[bool, typer.Option("--reveal", help="Show full API keys. Off by default for safety.")] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    try:
        records = get_service().list_providers()
    except Exception as exc:
        _exit_with_error(str(exc))
    if _json_mode():
        if not reveal:
            sanitized: list[dict] = []
            for record in records:
                copy = dict(record)
                raw_key = str(copy.get("api_key") or "")
                copy["api_key"] = _mask_secret(raw_key) if raw_key else ""
                sanitized.append(copy)
            _print_json(sanitized)
        else:
            _print_json(records)
        return
    if not records:
        console.print("No providers found.")
        return
    _print_provider_table(records, wide=wide, reveal=reveal)


@provider_app.command("show", help="Show the collected auth-profiles.json and models.json for a provider.")
def show_provider(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Provider name.")] = None,
    reveal: Annotated[bool, typer.Option("--reveal", help="Show unmasked secrets. Off by default for safety.")] = False,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        payload = get_service().show_provider(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print_json(json.dumps(_redact_provider_payload(payload, reveal=reveal), ensure_ascii=False))


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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    _confirm_destructive(
        f"About to delete collected provider '{name}'. Its auth-profiles.json and models.json will be removed.",
        yes,
    )
    try:
        get_service().remove_provider(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[yellow]Removed provider:[/yellow] {name}")


@provider_models_app.command("list", help="List the models stored in a collected provider.")
def list_provider_models(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Provider name.")] = None,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    if not name:
        _show_help_and_exit(ctx)
    try:
        models = get_service().list_provider_models(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    if _json_mode():
        _print_json({"provider": name, "models": models})
        return
    if not models:
        console.print("No models configured.")
        return
    for model in models:
        console.print(model)
_LIST_SOURCES = ("managed", "local", "all")


def _resolve_list_source(
    source: str | None, *, local_flag: bool, managed_flag: bool, all_flag: bool
) -> str:
    """Reconcile the new --source flag with legacy --local/--managed/--all shortcuts.

    Default is ``managed`` so local pseudo-instances under ~/.openclaw /
    ~/.hermes are hidden unless the user opts in — they are not managed by
    ClawCU and the mix was causing confusion.
    """
    if source is not None:
        if source not in _LIST_SOURCES:
            _exit_with_error(
                f"Unknown --source '{source}'. Expected one of: {', '.join(_LIST_SOURCES)}."
            )
        return source
    if all_flag:
        return "all"
    if local_flag and not managed_flag:
        return "local"
    # `--managed` or no flag at all → managed (new default).
    return "managed"


def _apply_list_filters(
    records: list[dict],
    *,
    service: str | None,
    status: str | None,
) -> list[dict]:
    filtered = records
    if service:
        filtered = [r for r in filtered if str(r.get("service", "")).lower() == service.lower()]
    if status:
        filtered = [r for r in filtered if str(r.get("status", "")).lower() == status.lower()]
    return filtered


@app.command(
    "list",
    help=(
        "List managed instances. By default shows ClawCU-managed instances only; "
        "pass --source local or --source all to include ~/.openclaw / ~/.hermes pseudo-entries."
    ),
)
def list_instances(
    running: Annotated[
        bool,
        typer.Option("--running", help="Shortcut for --status=running."),
    ] = False,
    agents: Annotated[bool, typer.Option("--agents", help="Expand the list to one row per agent.")] = False,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="managed | local | all. Default: managed (hides local pseudo-instances).",
        ),
    ] = None,
    local: Annotated[
        bool,
        typer.Option("--local", help="Shortcut for --source local."),
    ] = False,
    managed: Annotated[
        bool,
        typer.Option("--managed", help="Shortcut for --source managed (default)."),
    ] = False,
    all_sources: Annotated[
        bool,
        typer.Option("--all", help="Shortcut for --source all."),
    ] = False,
    service_filter: Annotated[
        str | None,
        typer.Option("--service", help="Only show instances of this service (e.g. openclaw, hermes)."),
    ] = None,
    status_filter: Annotated[
        str | None,
        typer.Option("--status", help="Only show instances in this status (e.g. running, stopped, create_failed)."),
    ] = None,
    wide: Annotated[
        bool,
        typer.Option("--wide", help="Show all columns (SOURCE, HOME, PROVIDERS, MODELS, SNAPSHOT) and full ACCESS URL."),
    ] = False,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Show full dashboard tokens inside ACCESS URLs. Off by default for safety."),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    resolved_source = _resolve_list_source(
        source, local_flag=local, managed_flag=managed, all_flag=all_sources
    )
    effective_status = status_filter
    if running and not effective_status:
        effective_status = "running"
    elif running and effective_status and effective_status.lower() != "running":
        _exit_with_error("--running conflicts with --status; use one or the other.")
    try:
        service = get_service()
        records: list[dict]
        if agents:
            records = []
            if resolved_source in {"local", "all"}:
                records.extend(service.list_local_agent_summaries())
            if resolved_source in {"managed", "all"}:
                records.extend(service.list_agent_summaries(running_only=running))
        else:
            records = []
            if resolved_source in {"local", "all"}:
                records.extend(service.list_local_instance_summaries())
            if resolved_source in {"managed", "all"}:
                records.extend(service.list_instance_summaries(running_only=running))
    except Exception as exc:
        _exit_with_error(str(exc))
    records = _apply_list_filters(
        records, service=service_filter, status=effective_status
    )
    if _json_mode():
        if not reveal and not agents:
            for record in records:
                if "access_url" in record:
                    record["access_url"] = _strip_token_fragment(record.get("access_url") or "")
        _print_json(records)
        return
    if not records:
        if agents:
            console.print("No agents found.")
        else:
            console.print("No instances found.")
        return
    if agents:
        _print_agent_table(records)
    else:
        _print_instance_table(records, wide=wide, reveal=reveal)


def _print_inspect_human(payload: dict, *, reveal: bool, show_history: bool) -> None:
    """Render the inspect payload as a compact human view.

    History is folded by default (the review's chief complaint about
    the old default was that dumping 100+ lines of JSON at someone was
    a lazy developer-first design). Pass ``--show-history`` or
    ``--json`` to see the full record.
    """
    instance = payload.get("instance") or {}
    name = instance.get("name", "-")
    console.print(f"[bold]Instance:[/bold] {name}")

    # --- Summary ---
    summary = Table(show_header=False, box=None, pad_edge=False)
    summary.add_column("key", style="cyan", no_wrap=True)
    summary.add_column("value", overflow="fold")
    for key, label in [
        ("service", "Service"),
        ("version", "Version"),
        ("status", "Status"),
        ("port", "Port"),
        ("dashboard_port", "Dashboard port"),
        ("cpu", "CPU"),
        ("memory", "Memory"),
        ("datadir", "Data dir"),
        ("image_tag", "Image"),
        ("container_name", "Container"),
        ("auth_mode", "Auth mode"),
        ("created_at", "Created"),
        ("updated_at", "Updated"),
    ]:
        value = instance.get(key)
        if value is None or value == "":
            continue
        summary.add_row(label, str(value))
    last_error = instance.get("last_error")
    if last_error:
        summary.add_row("Last error", f"[red]{last_error}[/red]")
    console.print(summary)

    # --- Access ---
    access = payload.get("access") or {}
    if any(access.get(k) for k in ("base_url", "readiness_label", "auth_hint", "token")):
        console.print()
        console.print("[bold]Access[/bold]")
        base_url = access.get("base_url") or "-"
        if not reveal:
            base_url = _strip_token_fragment(base_url)
        access_table = Table(show_header=False, box=None, pad_edge=False)
        access_table.add_column("key", style="cyan", no_wrap=True)
        access_table.add_column("value", overflow="fold")
        access_table.add_row("URL", base_url)
        readiness = access.get("readiness_label")
        if readiness:
            access_table.add_row("Readiness", str(readiness))
        auth_hint = access.get("auth_hint")
        if auth_hint:
            access_table.add_row("Auth hint", str(auth_hint))
        raw_token = access.get("token") or ""
        if raw_token:
            access_table.add_row("Token", raw_token if reveal else _mask_secret(raw_token))
        console.print(access_table)

    # --- Snapshots ---
    snapshots = payload.get("snapshots") or {}
    snapshot_items = [(k, v) for k, v in snapshots.items() if v]
    if snapshot_items:
        console.print()
        console.print("[bold]Snapshots[/bold]")
        snap_table = Table(show_header=False, box=None, pad_edge=False)
        snap_table.add_column("key", style="cyan", no_wrap=True)
        snap_table.add_column("value", overflow="fold")
        for key, value in snapshot_items:
            snap_table.add_row(key, str(value))
        console.print(snap_table)

    # --- Container (compact) ---
    container = payload.get("container")
    if container:
        state = container.get("State") or {}
        image = container.get("Config", {}).get("Image") or container.get("Image") or "-"
        restart = container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") or "-"
        status = state.get("Status") or container.get("Status") or "-"
        health = (state.get("Health") or {}).get("Status") or "-"
        started_at = state.get("StartedAt") or "-"
        console.print()
        console.print("[bold]Container[/bold]")
        c_table = Table(show_header=False, box=None, pad_edge=False)
        c_table.add_column("key", style="cyan", no_wrap=True)
        c_table.add_column("value", overflow="fold")
        c_table.add_row("Docker status", str(status))
        c_table.add_row("Health", str(health))
        c_table.add_row("Image", str(image))
        c_table.add_row("Restart policy", str(restart))
        c_table.add_row("Started at", str(started_at))
        console.print(c_table)

    # --- History ---
    history = instance.get("history") or []
    if history:
        console.print()
        console.print(f"[bold]History[/bold] ({len(history)} event(s))")
        if show_history:
            hist_table = Table(box=None, pad_edge=False)
            hist_table.add_column("TIMESTAMP", no_wrap=True, style="cyan")
            hist_table.add_column("ACTION", no_wrap=True)
            hist_table.add_column("DETAILS", overflow="fold")
            for event in history:
                timestamp = str(event.get("timestamp", "-"))
                action = str(event.get("action", "-"))
                details = ", ".join(
                    f"{k}={v}"
                    for k, v in event.items()
                    if k not in {"timestamp", "action"}
                )
                hist_table.add_row(timestamp, action, details or "-")
            console.print(hist_table)
        else:
            latest = history[-1]
            console.print(
                f"  latest: [cyan]{latest.get('action', '-')}[/cyan] at {latest.get('timestamp', '-')}"
            )
            console.print("  (pass --show-history to expand, or --json for the full payload)")


@app.command(
    "inspect",
    help="Show detailed state for a managed instance. Default is a compact readable view; pass --json for the full payload.",
)
def inspect_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    show_history: Annotated[
        bool,
        typer.Option("--show-history", help="Expand the full history timeline (folded by default)."),
    ] = False,
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Show full dashboard tokens and access URLs. Off by default for safety."),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    if not name:
        _show_help_and_exit(ctx)
    try:
        payload = get_service().inspect_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    if _json_mode():
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return
    _print_inspect_human(payload, reveal=reveal, show_history=show_history)


def _copy_to_clipboard(value: str) -> tuple[bool, str]:
    """Best-effort clipboard copy.

    Returns (ok, backend_name_or_error). Tries pbcopy / xclip / xsel /
    wl-copy / clip in order. Short-circuits if none are found so the
    caller can fall back to printing.
    """
    import shutil
    import subprocess

    candidates: list[tuple[str, list[str]]] = [
        ("pbcopy", ["pbcopy"]),
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
        ("clip", ["clip"]),
    ]
    for label, cmd in candidates:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, input=value, text=True, check=True, timeout=5)
        except Exception as exc:
            return False, f"{label} failed: {exc}"
        return True, label
    return False, "no clipboard backend found (tried pbcopy, wl-copy, xclip, xsel, clip)"


@app.command(
    "token",
    help=(
        "Print the dashboard token for a managed instance. "
        "Default shows both the token and the access URL with the `#token=…` anchor."
    ),
)
def token_for_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    copy: Annotated[
        bool,
        typer.Option("--copy", "-c", help="Copy the token to the system clipboard (pbcopy/xclip/wl-copy/clip)."),
    ] = False,
    url_only: Annotated[
        bool,
        typer.Option("--url-only", help="Print only the access URL (with #token=… anchor). Scripting-friendly."),
    ] = False,
    token_only: Annotated[
        bool,
        typer.Option("--token-only", help="Print only the token, no labels. Scripting-friendly."),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    if not name:
        _show_help_and_exit(ctx)
    if url_only and token_only:
        _exit_with_error("--url-only and --token-only are mutually exclusive.")
    service = get_service()
    try:
        token = service.token(name)
    except Exception as exc:
        message = str(exc)
        if "not supported" in message.lower():
            _exit_with_error(
                f"{message}\n"
                "Hint: Hermes uses native auth — run "
                f"`clawcu config {name}` to configure it, or "
                f"`clawcu exec {name} hermes auth` for the service-native flow."
            )
        _exit_with_error(message)
    dashboard_url: str | None = None
    try:
        dashboard_url = service.dashboard_url(name)
    except Exception:
        dashboard_url = None

    if _json_mode():
        _print_json({"name": name, "token": token, "url": dashboard_url})
        return
    if url_only:
        if not dashboard_url:
            _exit_with_error(f"Instance '{name}' does not expose a dashboard URL.")
        console.print(dashboard_url)
    elif token_only:
        console.print(token)
    else:
        console.print(f"[bold]Token:[/bold] {token}")
        if dashboard_url:
            console.print(f"[blue]URL:[/blue]   {dashboard_url}")

    if copy:
        ok, backend = _copy_to_clipboard(token)
        if ok:
            console.print(f"[green]Copied token to clipboard ({backend}).[/green]")
        else:
            console.print(
                f"[yellow]Could not copy to clipboard:[/yellow] {backend}"
            )


@app.command("setenv", help="Set environment variables for a managed instance.")
def set_instance_env(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    assignments: Annotated[list[str] | None, typer.Argument(help="One or more KEY=VALUE assignments.")] = None,
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--from-file",
            "-f",
            help="Load KEY=VALUE pairs from a .env-style file. Mutually exclusive with inline assignments.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show a colored diff of env changes without writing the file or recreating.",
        ),
    ] = False,
    reveal: Annotated[
        bool,
        typer.Option(
            "--reveal",
            help="Show unmasked values in --dry-run output for KEY/TOKEN/SECRET/PASSWORD entries.",
        ),
    ] = False,
    apply_now: Annotated[
        bool,
        typer.Option("--apply", help="Recreate the instance immediately so the new env takes effect now."),
    ] = False,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    if from_file is not None and assignments:
        _exit_with_error("Use either inline KEY=VALUE arguments or --from-file, not both.")
    if from_file is None and not assignments:
        _show_help_and_exit(ctx)
    if dry_run and apply_now:
        _exit_with_error("--dry-run and --apply are mutually exclusive.")

    effective_assignments: list[str]
    if from_file is not None:
        try:
            pairs = _parse_env_file(from_file)
        except typer.BadParameter as exc:
            _exit_with_error(str(exc))
        if not pairs:
            _exit_with_error(f"No KEY=VALUE entries found in {from_file}.")
        effective_assignments = [f"{key}={value}" for key, value in pairs]
    else:
        effective_assignments = list(assignments or [])

    service = get_service()

    if dry_run:
        try:
            current = service.get_instance_env(name)
        except Exception as exc:
            _exit_with_error(str(exc))
        before = dict(current.get("values") or {})
        after = dict(before)
        for assignment in effective_assignments:
            if "=" not in assignment:
                _exit_with_error(f"Invalid assignment '{assignment}'. Use KEY=VALUE.")
            key, value = assignment.split("=", 1)
            key = key.strip()
            if not key:
                _exit_with_error(f"Invalid assignment '{assignment}'. Empty key.")
            after[key] = value
        console.print(f"[cyan]Dry run:[/cyan] would update {current.get('path')}")
        _render_env_diff(before, after, reveal=reveal)
        console.print("[dim](no changes written; re-run without --dry-run to apply)[/dim]")
        return

    if apply_now and hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        result = service.set_instance_env(name, effective_assignments)
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
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Show unmasked values for KEY/TOKEN/SECRET/PASSWORD entries. Off by default."),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    if not name:
        _show_help_and_exit(ctx)
    try:
        result = get_service().get_instance_env(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    values = result.get("values", {})
    if not isinstance(values, dict):
        values = {}
    display_values: dict[str, str] = {}
    masked_any = False
    for key in sorted(values):
        raw = str(values[key])
        displayed = _mask_env_value(key, raw, reveal=reveal)
        if displayed != raw:
            masked_any = True
        display_values[key] = displayed
    if _json_mode():
        _print_json({"instance": name, "path": result.get("path"), "values": display_values, "masked": masked_any and not reveal})
        return
    if not display_values:
        console.print("No environment variables configured.")
        return
    for key, displayed in display_values.items():
        console.print(f"{key}={displayed}")
    if masked_any and not reveal:
        console.print("[dim](sensitive values masked; re-run with --reveal to show)[/dim]")


@app.command("unsetenv", help="Remove environment variables configured for a managed instance.")
def unset_instance_env(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    keys: Annotated[list[str] | None, typer.Argument(help="One or more environment variable names.")] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show which keys would be removed without writing the file or recreating.",
        ),
    ] = False,
    reveal: Annotated[
        bool,
        typer.Option(
            "--reveal",
            help="Show unmasked values in --dry-run output for KEY/TOKEN/SECRET/PASSWORD entries.",
        ),
    ] = False,
    apply_now: Annotated[
        bool,
        typer.Option("--apply", help="Recreate the instance immediately so the env change takes effect now."),
    ] = False,
) -> None:
    if not name or not keys:
        _show_help_and_exit(ctx)
    if dry_run and apply_now:
        _exit_with_error("--dry-run and --apply are mutually exclusive.")
    service = get_service()

    if dry_run:
        try:
            current = service.get_instance_env(name)
        except Exception as exc:
            _exit_with_error(str(exc))
        before = dict(current.get("values") or {})
        after = dict(before)
        present: list[str] = []
        missing: list[str] = []
        for raw_key in keys:
            key = raw_key.strip()
            if key in after:
                after.pop(key, None)
                present.append(key)
            else:
                missing.append(key)
        console.print(f"[cyan]Dry run:[/cyan] would update {current.get('path')}")
        _render_env_diff(before, after, reveal=reveal)
        if missing:
            console.print(
                f"[dim]Not present (no-op): {', '.join(sorted(missing))}[/dim]"
            )
        console.print("[dim](no changes written; re-run without --dry-run to apply)[/dim]")
        return

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
            "This command runs the service-native setup or configuration flow inside the managed instance container.",
            [
                "clawcu config <instance>",
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
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
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
    time: Annotated[
        int | None,
        typer.Option(
            "--time",
            "-t",
            help=(
                "Graceful shutdown window in seconds (passed to `docker stop --time`). "
                "Default is 5s; raise it to let long OpenClaw/Hermes tasks finish before SIGKILL."
            ),
            min=0,
        ),
    ] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    try:
        record = get_service().stop_instance(name, timeout=time)
    except Exception as exc:
        _exit_with_error(str(exc))
    suffix = f" (grace {time}s)" if time is not None else ""
    console.print(f"[yellow]Stopped instance:[/yellow] {record.name}{suffix}")


@app.command("restart", help="Restart a managed instance.")
def restart_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    recreate_if_config_changed: Annotated[
        bool,
        typer.Option(
            "--recreate-if-config-changed/--no-recreate-if-config-changed",
            help=(
                "Default ON: if the container's env/config has drifted from "
                "the saved record (e.g. after `clawcu setenv` without `--apply`) "
                "or the container is missing, ClawCU promotes the restart to a "
                "full `recreate` so the new env file takes effect — matching "
                "how `clawcu start` already behaves. Pass "
                "`--no-recreate-if-config-changed` to force a plain "
                "`docker restart` even when drift is detected."
            ),
        ),
    ] = True,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if recreate_if_config_changed and hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.restart_instance(
            name,
            recreate_if_config_changed=recreate_if_config_changed,
        )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Restarted instance:[/green] {record.name}")
    _print_access_url(service, record.name)


def _do_recreate(service: ClawCUService, name: str) -> None:
    """Unified recreate logic.

    Tries retry_instance first (cheap auto-port recovery path for
    create_failed records); if the service rejects it with a
    ValueError ("Only create_failed ..."), falls back to the regular
    recreate_instance flow. Prints the appropriate verb based on which
    path succeeded.
    """
    try:
        record = service.retry_instance(name)
    except ValueError as exc:
        message = str(exc)
        if "create_failed" not in message:
            _exit_with_error(message)
        try:
            record = service.recreate_instance(name)
        except Exception as exc2:
            _exit_with_error(str(exc2))
        console.print(
            f"[green]Recreated instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
        )
        _print_access_url(service, record.name)
        return
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Retried instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
    )
    _print_access_url(service, record.name)


@app.command(
    "recreate",
    help="Recreate an existing instance. Auto-retries instances in create_failed status.",
)
def recreate_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Instance to recreate.")] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    _do_recreate(service, name)


def _print_upgrade_plan(plan: dict) -> None:
    """Render an upgrade_plan payload as a compact human-readable summary."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("Instance", str(plan.get("instance", "-")))
    table.add_row("Service", str(plan.get("service", "-")))
    table.add_row(
        "Version",
        f"{plan.get('current_version', '-')}  ->  [bold green]{plan.get('target_version', '-')}[/bold green]",
    )
    table.add_row("Datadir", str(plan.get("datadir", "-")))
    env_path = plan.get("env_path", "-")
    env_exists = bool(plan.get("env_exists"))
    env_keys = plan.get("env_keys") or []
    if env_exists:
        env_line = f"{env_path} (preserved; {len(env_keys)} key(s))"
    else:
        env_line = f"{env_path} (does not exist; nothing to preserve)"
    table.add_row("Env file", env_line)
    table.add_row("Projected image", str(plan.get("projected_image", "-")))
    table.add_row(
        "Safety snapshot",
        f"{plan.get('snapshot_root', '-')}/<timestamp>-{plan.get('snapshot_label', '-')}",
    )
    console.print(table)


_REMOTE_VERSION_PREVIEW_LIMIT = 10


def _print_upgradable_versions(payload: dict, *, show_all: bool = False) -> None:
    """Render list_upgradable_versions output as a small report.

    When the remote registry returns more than
    ``_REMOTE_VERSION_PREVIEW_LIMIT`` tags, only the most recent batch is
    shown; pass ``show_all=True`` (wired to ``--all-versions`` in the CLI)
    to render the complete list. The service-layer payload is always
    complete — truncation is presentational only, so ``--json`` consumers
    still see every tag.
    """
    console.print(
        f"[bold]Instance:[/bold] {payload.get('instance', '-')}  "
        f"([dim]{payload.get('service', '-')}[/dim])"
    )
    console.print(
        f"[bold]Image repo:[/bold] {payload.get('image_repo', '-') or '-'}"
    )
    current = payload.get("current_version", "-")
    console.print(f"[bold]Current version:[/bold] {current}")

    history = payload.get("history") or []
    if history:
        console.print("[bold]History:[/bold]")
        for version in history:
            marker = " [dim](current)[/dim]" if version == current else ""
            console.print(f"  - {version}{marker}")
    else:
        console.print("[bold]History:[/bold] [dim]-[/dim]")

    local = payload.get("local_images") or []
    local_set = set(local)
    if local:
        console.print("[bold]Local images (no pull needed):[/bold]")
        for tag in local:
            marker = " [dim](current)[/dim]" if tag == current else ""
            console.print(f"  - {tag}{marker}")
    else:
        console.print(
            "[bold]Local images:[/bold] [dim]none found for this repo; Docker will pull on upgrade[/dim]"
        )

    remote_requested = bool(payload.get("remote_requested"))
    remote = payload.get("remote_versions")
    remote_error = payload.get("remote_error")
    remote_registry = payload.get("remote_registry")
    if not remote_requested:
        console.print(
            "[bold]Remote:[/bold] [dim]skipped (--no-remote)[/dim]"
        )
    elif remote is None:
        # Remote was asked for but failed. Show the reason so the user
        # knows why they are only seeing local/history.
        if remote_error:
            console.print(
                f"[bold]Remote:[/bold] [yellow]fetch failed: {remote_error}[/yellow]"
            )
        else:
            console.print(
                "[bold]Remote:[/bold] [yellow]fetch failed (no details)[/yellow]"
            )
        console.print(
            "[dim]  try --no-remote to skip the registry query, "
            "or check network / mirror configuration[/dim]"
        )
    elif not remote:
        registry_hint = f" on {remote_registry}" if remote_registry else ""
        console.print(
            f"[bold]Remote{registry_hint}:[/bold] [dim]no release tags matched[/dim]"
        )
    else:
        registry_hint = f" on {remote_registry}" if remote_registry else ""
        total = len(remote)
        # remote_versions is sorted ascending; the tail is the newest
        # batch of releases. Truncate to keep the default view scannable
        # and point power users at --all-versions for the full list.
        if not show_all and total > _REMOTE_VERSION_PREVIEW_LIMIT:
            display = remote[-_REMOTE_VERSION_PREVIEW_LIMIT:]
            truncated = True
        else:
            display = remote
            truncated = False
        header_suffix = (
            f" (showing {len(display)} of {total} release tags, most recent)"
            if truncated
            else f" ({total} release tags)"
        )
        console.print(
            f"[bold]Remote{registry_hint}{header_suffix}:[/bold]"
        )
        for tag in display:
            markers: list[str] = []
            if tag == current:
                markers.append("current")
            if tag in local_set:
                markers.append("local")
            suffix = f" [dim]({', '.join(markers)})[/dim]" if markers else ""
            console.print(f"  - {tag}{suffix}")
        if truncated:
            console.print(
                "[dim]  ... pass --all-versions to see the full list[/dim]"
            )


@app.command(
    "upgrade",
    help="Upgrade an instance to a newer service version with a safety snapshot of its data directory and env file.",
)
def upgrade_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    version: Annotated[str | None, typer.Option("--version", help="Target service version or git ref.")] = None,
    list_versions: Annotated[
        bool,
        typer.Option(
            "--list-versions",
            help=(
                "List candidate versions for this instance: the configured "
                "registry's release tags (best-effort remote query), the "
                "local Docker images you've already pulled, and this "
                "instance's version history. Does not require --version."
            ),
        ),
    ] = False,
    include_remote: Annotated[
        bool,
        typer.Option(
            "--remote/--no-remote",
            help=(
                "Query the configured image registry for available release "
                "tags (default on). Use --no-remote for a strictly offline "
                "view — e.g. in CI or when the registry is unreachable."
            ),
        ),
    ] = True,
    all_versions: Annotated[
        bool,
        typer.Option(
            "--all-versions",
            help=(
                "Show every remote release tag returned by the registry. "
                "By default --list-versions truncates the remote section "
                "to the 10 most recent releases to keep the output "
                "scannable."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Show the upgrade plan (current->target, datadir, env carry-over, "
                "projected image, snapshot path) and exit without touching Docker "
                "or the data directory."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the interactive confirmation prompt before starting the upgrade.",
        ),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()

    if list_versions:
        try:
            payload = service.list_upgradable_versions(
                name, include_remote=include_remote
            )
        except Exception as exc:
            _exit_with_error(str(exc))
        if _json_mode():
            _print_json(payload)
            return
        _print_upgradable_versions(payload, show_all=all_versions)
        return

    if not version:
        _show_help_and_exit(ctx)

    try:
        plan = service.upgrade_plan(name, version=version)
    except Exception as exc:
        _exit_with_error(str(exc))

    if dry_run:
        if _json_mode():
            _print_json(plan)
            return
        console.print("[cyan]Dry run:[/cyan] no container or snapshot will be created.")
        _print_upgrade_plan(plan)
        console.print(
            "[dim](re-run without --dry-run, and pass --yes to skip the confirm prompt)[/dim]"
        )
        return

    # Normal path: show the plan + confirmation prompt (unless --yes).
    if not _json_mode():
        _print_upgrade_plan(plan)
    _confirm_destructive(
        (
            f"About to upgrade instance '{plan['instance']}' from "
            f"{plan['current_version']} to {plan['target_version']}. A safety "
            f"snapshot will be written under {plan['snapshot_root']}; the env "
            f"file at {plan['env_path']} will be preserved."
        ),
        yes,
    )

    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.upgrade_instance(name, version=version)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Upgraded instance:[/green] {record.name} -> {record.version}")
    _print_access_url(service, record.name)


def _print_rollback_plan(plan: dict) -> None:
    """Render a rollback_plan payload as a compact human-readable summary."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("Instance", str(plan.get("instance", "-")))
    table.add_row("Service", str(plan.get("service", "-")))
    table.add_row(
        "Version",
        f"{plan.get('current_version', '-')}  ->  [bold green]{plan.get('target_version', '-')}[/bold green]",
    )
    table.add_row("Datadir", str(plan.get("datadir", "-")))
    env_path = plan.get("env_path", "-")
    env_line = (
        f"{env_path} (will be restored from snapshot)"
        if plan.get("restore_snapshot_exists")
        else f"{env_path} (no env snapshot to restore)"
    )
    table.add_row("Env file", env_line)
    table.add_row("Projected image", str(plan.get("projected_image", "-")))
    restore = plan.get("restore_snapshot") or "-"
    exists_tag = (
        "[green]present[/green]"
        if plan.get("restore_snapshot_exists")
        else "[red]missing on disk[/red]"
    )
    table.add_row("Restore snapshot", f"{restore}  {exists_tag}")
    table.add_row(
        "Safety snapshot",
        f"{plan.get('snapshot_root', '-')}/<timestamp>-{plan.get('snapshot_label', '-')}",
    )
    selected_action = plan.get("selected_action")
    selected_ts = plan.get("selected_timestamp")
    if selected_action or selected_ts:
        table.add_row(
            "Source event",
            f"{selected_action or '-'} @ {selected_ts or '-'}",
        )
    console.print(table)


def _print_rollback_targets(payload: dict) -> None:
    """Render list_rollback_targets output as a small table."""
    console.print(
        f"[bold]Instance:[/bold] {payload.get('instance', '-')}  "
        f"([dim]{payload.get('service', '-')}[/dim])"
    )
    console.print(
        f"[bold]Current version:[/bold] {payload.get('current_version', '-')}"
    )
    targets = payload.get("targets") or []
    if not targets:
        console.print(
            "[dim]No rollback targets recorded yet. Run 'clawcu upgrade' "
            "to produce a snapshot first.[/dim]"
        )
        return
    table = Table(show_header=True, box=None, pad_edge=False)
    table.add_column("#", style="bold")
    table.add_column("Action")
    table.add_column("Restores to", style="bold green")
    table.add_column("From -> To")
    table.add_column("When")
    table.add_column("Snapshot")
    for idx, entry in enumerate(targets):
        snapshot = entry.get("snapshot_dir") or "-"
        exists = entry.get("snapshot_exists")
        exists_marker = (
            "[green]present[/green]" if exists else "[red]missing[/red]"
        )
        table.add_row(
            str(idx),
            entry.get("action") or "-",
            entry.get("restores_to") or "-",
            f"{entry.get('from_version') or '-'} -> {entry.get('to_version') or '-'}",
            entry.get("timestamp") or "-",
            f"{snapshot}  {exists_marker}",
        )
    console.print(table)
    console.print(
        "[dim]rollback --to <version> restores the most recent event whose "
        "'restores to' matches. omit --to to pick the newest entry.[/dim]"
    )


@app.command(
    "rollback",
    help="Roll an instance back to an earlier snapshot. Defaults to the most recent transition; pass --to <version> to target a specific one, or --list to enumerate available targets.",
)
def rollback_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    to_version: Annotated[
        str | None,
        typer.Option(
            "--to",
            help=(
                "Target version to restore. Matches the most recent history "
                "event whose 'restores to' equals this value. Omit to use the "
                "latest reversible transition."
            ),
        ),
    ] = None,
    list_targets: Annotated[
        bool,
        typer.Option(
            "--list",
            help=(
                "List every snapshot target recorded for this instance "
                "(action, restore version, snapshot path, whether the "
                "snapshot still exists on disk). Does not touch Docker."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Show the rollback plan (current->target, env restore, "
                "snapshot source, safety snapshot path) and exit without "
                "touching Docker or the data directory."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the interactive confirmation prompt before starting the rollback.",
        ),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    if not name:
        _show_help_and_exit(ctx)
    service = get_service()

    if list_targets:
        try:
            payload = service.list_rollback_targets(name)
        except Exception as exc:
            _exit_with_error(str(exc))
        if _json_mode():
            _print_json(payload)
            return
        _print_rollback_targets(payload)
        return

    try:
        plan = service.rollback_plan(name, to_version=to_version)
    except Exception as exc:
        _exit_with_error(str(exc))

    if dry_run:
        if _json_mode():
            _print_json(plan)
            return
        console.print("[cyan]Dry run:[/cyan] no container or snapshot will be touched.")
        _print_rollback_plan(plan)
        console.print(
            "[dim](re-run without --dry-run, and pass --yes to skip the confirm prompt)[/dim]"
        )
        return

    if not _json_mode():
        _print_rollback_plan(plan)
    _confirm_destructive(
        (
            f"About to roll back instance '{plan['instance']}' from "
            f"{plan['current_version']} to {plan['target_version']}. The current "
            f"data directory will be replaced by the snapshot at "
            f"{plan.get('restore_snapshot') or '-'}; a fresh safety snapshot "
            f"will be written under {plan['snapshot_root']}."
        ),
        yes,
    )

    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.rollback_instance(name, to_version=to_version)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Rolled back instance:[/green] {record.name} -> {record.version}")
    _print_access_url(service, record.name)


@app.command(
    "clone",
    help=(
        "Clone an existing instance into a separate experiment instance. "
        "The source's data directory is always copied. By default the "
        "source's env file (API keys / tokens) is ALSO copied — pass "
        "--exclude-secrets to start with an empty env instead. "
        "Use --version to switch the clone to a different service "
        "version at copy time, e.g. to preview an upgrade without "
        "touching the original."
    ),
)
def clone_instance(
    ctx: typer.Context,
    source_name: Annotated[str | None, typer.Argument(help="Source instance name.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="New cloned instance name.")] = None,
    datadir: Annotated[str | None, typer.Option("--datadir", help="Target cloned data directory.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Target host port.")] = None,
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            help=(
                "Switch the clone to this service version or tag instead "
                "of inheriting the source's version. Useful for 'clone "
                "then upgrade' experiments — the source is untouched."
            ),
        ),
    ] = None,
    include_secrets: Annotated[
        bool,
        typer.Option(
            "--include-secrets/--exclude-secrets",
            help=(
                "Whether to copy the source's env file (API keys, tokens, "
                "provider secrets) into the clone. Default ON (env IS "
                "copied) to match pre-v0.2 behavior. Pass --exclude-secrets "
                "when sharing the clone or using a different credential "
                "scope — the clone will boot with an empty env and you "
                "re-seed it via `clawcu setenv` or the service's native "
                "config flow."
            ),
        ),
    ] = True,
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
            version=version,
            include_secrets=include_secrets,
        )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Cloned instance:[/green] {source_name} -> {record.name}")
    if version and version != record.version:
        # Defensive: if the service resolved a different version than
        # requested (normalization, etc.), surface it so the user sees
        # what actually got built.
        console.print(
            f"[dim]  requested version: {version}, resolved: {record.version}[/dim]"
        )
    elif version:
        console.print(f"[dim]  version: {record.version} (switched from source)[/dim]")
    if not include_secrets:
        console.print(
            "[yellow]  env file was NOT copied (--exclude-secrets).[/yellow] "
            "[dim]Seed credentials with `clawcu setenv` before using the clone.[/dim]"
        )
    _print_access_url(service, record.name)


@app.command("logs", help="Stream or print Docker logs for a managed instance.")
def logs_instance(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    follow: Annotated[bool, typer.Option("--follow", help="Follow the Docker log stream.")] = False,
    tail: Annotated[
        int,
        typer.Option(
            "--tail",
            help=(
                "Number of trailing lines to print. Defaults to 200; pass 0 (or a negative value) "
                "to stream the full log history."
            ),
        ),
    ] = 200,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "Only show logs more recent than this relative duration (e.g. 10m, 1h) or "
                "RFC3339 timestamp. Passed through to `docker logs --since`."
            ),
        ),
    ] = None,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    effective_tail: int | None = tail if tail > 0 else None
    try:
        service = get_service()
        try:
            service.stream_logs(name, follow=follow, tail=effective_tail, since=since)
        except TypeError:
            # Support older ClawCUService builds that don't accept tail/since.
            service.stream_logs(name, follow=follow)
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
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    if not name:
        _show_help_and_exit(ctx)
    summary = (
        f"About to remove instance '{name}' and delete its data directory."
        if delete_data
        else f"About to remove instance '{name}' (data directory will be kept)."
    )
    _confirm_destructive(summary, yes)
    try:
        get_service().remove_instance(name, delete_data=delete_data)
    except Exception as exc:
        _exit_with_error(str(exc))
    action = "and data directory" if delete_data else "but kept data directory"
    console.print(f"[green]Removed instance:[/green] {name} {action}")


def main() -> None:
    app()
