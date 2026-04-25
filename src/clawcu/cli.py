from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Annotated

import click
import typer
import typer.core
from click.shell_completion import get_completion_class
from rich.console import Console
from rich.table import Table
from typer.main import get_command

from clawcu import __version__
from clawcu.a2a.cli import a2a_app
from clawcu.dashboard.server import _dashboard_is_healthy, serve_dashboard
from clawcu.core.registry import is_semver_release_tag
from clawcu.service import ClawCUService

# When a command with required options gets no CLI args at all, show help
# and exit 0 — the user was asking "what does this command take?", not
# invoking it. When the user DID pass some args but still missed a required
# one, show help followed by the targeted error (exit 2). Click's default
# prints only a one-line usage + "Try --help", which leaves users one more
# guessing round away from a working command.
_original_parse_args = typer.core.TyperCommand.parse_args


def _parse_args_with_help(self, ctx, args):  # type: ignore[no-untyped-def]
    if not args and any(getattr(p, "required", False) for p in self.params):
        click.echo(ctx.get_help())
        ctx.exit(0)
    try:
        return _original_parse_args(self, ctx, args)
    except click.MissingParameter as exc:
        click.echo(ctx.get_help(), err=True)
        click.echo("", err=True)
        click.secho(
            f"Error: {exc.format_message()}", err=True, fg="red", bold=True
        )
        ctx.exit(2)


typer.core.TyperCommand.parse_args = _parse_args_with_help  # type: ignore[assignment]


app = typer.Typer(
    help="ClawCU manages local multi-agent instances with versioned Docker workflows.",
    rich_markup_mode="markdown",
    add_completion=False,
)
pull_app = typer.Typer(
    help=(
        "Pull and build managed services.\n\n"
        "**Examples:**\n\n"
        "```\n"
        "clawcu pull openclaw --version 2026.4.15\n"
        "clawcu pull hermes --version 2026.4.13\n"
        "clawcu pull --service openclaw --version 2026.4.15  # alt form\n"
        "```"
    ),
    rich_markup_mode="markdown",
    subcommand_metavar="SERVICE",
    add_completion=False,
)
create_app = typer.Typer(
    help=(
        "Create managed services.\n\n"
        "**Examples:**\n\n"
        "```\n"
        "clawcu create openclaw --name demo --version 2026.4.15\n"
        "clawcu create hermes --name agent --version 2026.4.13\n"
        "clawcu create --service openclaw --name demo --version 2026.4.15  # alt form\n"
        "```"
    ),
    rich_markup_mode="markdown",
    subcommand_metavar="SERVICE",
    add_completion=False,
)
provider_app = typer.Typer(
    help="Collect and reuse model configuration assets from managed instances and local homes.",
    add_completion=False,
)
hermes_app = typer.Typer(
    help="Hermes-specific instance operations.",
    subcommand_metavar="COMMAND",
    add_completion=False,
)
hermes_identity_app = typer.Typer(
    help="Manage the SOUL.md persona file for a hermes instance.",
    subcommand_metavar="ACTION",
    add_completion=False,
)
hermes_app.add_typer(hermes_identity_app, name="identity")
app.add_typer(pull_app, name="pull", rich_help_panel="Setup")
app.add_typer(create_app, name="create", rich_help_panel="Lifecycle")
app.add_typer(provider_app, name="provider", rich_help_panel="Providers")
app.add_typer(hermes_app, name="hermes", rich_help_panel="Lifecycle")
app.add_typer(a2a_app, name="a2a", rich_help_panel="A2A")
console = Console()
_DISPLAY_DATE_RE = re.compile(r"(\d{4}\.\d{1,2}\.\d{1,2})")


def get_service() -> ClawCUService:
    return ClawCUService()


_HINT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Anchored to the specific error shapes raised by the service layer.
    # Substring matching was too loose — docker stderr routinely mentions
    # "image" or embeds the word "instance" in unrelated failure text,
    # triggering misleading "Run clawcu list" hints.
    (
        re.compile(r"[Pp]rovider bundle '[^']+' was not found"),
        "Run `clawcu provider list` to see collected providers, or `clawcu provider collect` to import new ones.",
    ),
    (
        re.compile(r"[Pp]rovider '[^']+' was not found"),
        "Run `clawcu provider list` to see collected providers.",
    ),
    (
        re.compile(r"[Rr]emoved instance '[^']+' was not found"),
        "Run `clawcu list --removed` to see recoverable leftovers.",
    ),
    (
        re.compile(r"[Ii]nstance '[^']+' was not found"),
        "Run `clawcu list` to see managed instances.",
    ),
    (
        re.compile(r"has no rollback snapshot"),
        "Run `clawcu rollback <name> --list` to see available rollback targets.",
    ),
)


def _actionable_hint_for(message: str) -> str | None:
    """Return a short 'Run X to see Y' hint for well-known error shapes.

    The runtime raises naked ``ValueError`` / ``RuntimeError`` messages
    like ``Instance 'foo' was not found.`` which are readable but not
    actionable — the user has to guess the fix. Matching anchors on the
    specific phrasings the service layer emits rather than on loose
    substrings, so arbitrary docker stderr (e.g. "repository does not
    exist", "pull access denied") does not trigger a misleading hint.
    """
    for pattern, hint in _HINT_PATTERNS:
        if pattern.search(message):
            return hint
    return None


def _exit_with_error(message: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {message}")
    hint = _actionable_hint_for(message)
    if hint:
        console.print(f"[dim]Hint:[/dim] {hint}")
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


_WIDE_INSTANCE_MIN_COLS = 120
_NARROW_INSTANCE_MIN_COLS = 72


def _compress_home_path(path: str) -> str:
    """Render an absolute path with ``$HOME`` collapsed to ``~``.

    The wide table's HOME column repeats the same ``/Users/<name>/``
    prefix on every row, which eats ~18 columns and forces the column
    to fold onto multiple lines even at 140 cols. Compressing to ``~``
    keeps the useful suffix readable without losing any information.
    """
    if not path or path == "-":
        return path or "-"
    home = os.path.expanduser("~")
    if home and home != "/" and path == home:
        return "~"
    if home and home != "/" and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _print_instance_stacked(records: list[dict], *, reveal: bool) -> None:
    """Key/value layout used when the terminal is too narrow for the
    6-column table (e.g. tmux split-pane, SSH). Each instance prints as
    a short block so columns never collapse into 1-char cells."""
    console.print("[bold]ClawCU Instances[/bold]")
    for idx, record in enumerate(records):
        access_url = record.get("access_url", "-")
        if not reveal:
            access_url = _strip_token_fragment(access_url)
        console.print(f"[bold]{record['name']}[/bold]  [dim]({record.get('service', '-')})[/dim]")
        console.print(f"  version: {_display_version(record['version'])}")
        console.print(f"  port:    {record['port']}")
        console.print(f"  status:  {record['status']}")
        console.print(f"  access:  {_access_host_port(access_url)}")
        if idx != len(records) - 1:
            console.print()


def _print_instance_table(records: list[dict], *, wide: bool = False, reveal: bool = False) -> None:
    # --wide expects ~120+ columns to render readably. At narrower widths
    # Rich collapses trailing columns to a single character, producing a
    # worse view than the default 6-column layout. Degrade to the default
    # with a hint instead of silently shipping an unreadable table.
    width = console.size.width
    effective_wide = wide
    if wide and width < _WIDE_INSTANCE_MIN_COLS:
        console.print(
            f"[yellow]--wide needs at least {_WIDE_INSTANCE_MIN_COLS} columns; "
            f"terminal is {width}. Falling back to the default view.[/yellow]"
        )
        effective_wide = False

    if not effective_wide and width < _NARROW_INSTANCE_MIN_COLS and records:
        _print_instance_stacked(records, reveal=reveal)
        return

    table = Table(title="ClawCU Instances")
    if effective_wide:
        table.add_column("SOURCE", no_wrap=True)
    table.add_column("NAME", no_wrap=True)
    table.add_column("SERVICE", no_wrap=True)
    if effective_wide:
        table.add_column("HOME", overflow="fold")
    table.add_column("VERSION", no_wrap=True)
    table.add_column("PORT", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("ACCESS", overflow="fold", min_width=14)
    if effective_wide:
        table.add_column("PROVIDERS", overflow="fold")
        table.add_column("MODELS", overflow="fold")
        table.add_column("SNAPSHOT", overflow="fold")
    for record in records:
        access_url = record.get("access_url", "-")
        if not reveal:
            access_url = _strip_token_fragment(access_url)
        if not effective_wide:
            access_cell = _access_host_port(access_url)
        else:
            access_cell = access_url
        row: list[str] = []
        if effective_wide:
            row.append(record.get("source", "-"))
        row.append(record["name"])
        row.append(record.get("service", "-"))
        if effective_wide:
            row.append(_compress_home_path(record.get("home", "-")))
        row.append(_display_version(record["version"]))
        row.append(str(record["port"]))
        row.append(record["status"])
        row.append(access_cell)
        if effective_wide:
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
            _compress_home_path(record.get("home", "-")),
            record["agent"],
            record.get("primary", "-"),
            record.get("fallbacks", "-"),
        )
    console.print(table)


def _print_available_versions(payload: dict, *, limit: int = 10) -> None:
    """Render the per-service remote version block below `clawcu list`.

    Each row shows the most recent ``limit`` tags with the newest first.
    When the user opted out (`--no-remote`), the row renders as a dim
    "offline" line matching the `upgrade --list-versions` convention.
    When the remote fetch truly failed, the error is surfaced in yellow.
    In both offline cases a second indented line surfaces local Docker
    images so the user still sees something actionable.
    """
    console.print()
    console.print(
        f"[bold]Available versions[/bold] [dim](top {limit} by semver, newest first)[/dim]"
    )
    for name in ("openclaw", "hermes"):
        entry = payload.get(name) or {}
        label = f"  [cyan]{name:<9}[/cyan]"
        indent = "             "  # aligns continuation lines under the first tag
        versions = entry.get("versions")
        local_versions = entry.get("local_versions") or []
        if versions is None:
            if not entry.get("remote_requested", True):
                # User opted out with --no-remote — dim status, not a warning.
                console.print(
                    f"{label} [dim]offline (remote skipped by --no-remote)[/dim]"
                )
            else:
                error = entry.get("error") or "remote fetch failed"
                console.print(f"{label} [yellow]fetch failed: {error}[/yellow]")
            if local_versions:
                total_local = len(local_versions)
                newest_local = list(reversed(local_versions[-limit:]))
                local_suffix = (
                    f" [dim]({total_local} total)[/dim]" if total_local > limit else ""
                )
                console.print(
                    f"{indent}[dim]local images:[/dim] {', '.join(newest_local)}{local_suffix}"
                )
            continue
        if not versions:
            console.print(f"{label} [dim]no release tags[/dim]")
            continue
        total = len(versions)
        # versions is sorted oldest -> newest; reverse the tail so the
        # user sees the newest release in the leftmost position.
        newest_first = list(reversed(versions[-limit:]))
        suffix = f" [dim]({total} total)[/dim]" if total > limit else ""
        console.print(f"{label} {', '.join(newest_first)}{suffix}")


_WIDE_PROVIDER_MIN_COLS = 110
_REVEAL_PROVIDER_MIN_COLS = 130
_API_KEY_ENV_REF = re.compile(r"^\$\{[^}]+\}$|^\$[A-Z_][A-Z0-9_]*$")

_API_KEY_STATE_LABELS = {
    "set": "[green]set[/green] = literal key captured",
    "env-ref": "[cyan]env-ref[/cyan] = ${ENV_VAR} placeholder",
    "empty": "[yellow]empty[/yellow] = source had the field but it was blank",
    "missing": "unset = no apiKey field in the source",
}


def _provider_api_key_cell(
    raw_key: str,
    *,
    reveal: bool,
    wide: bool,
    state: str | None = None,
) -> str:
    """Classify provider api_key cell so users can tell apart four
    distinct states: ``set`` (literal key present, rendered masked or
    as ``set``), ``env-ref`` (placeholder like ``${OPENAI_API_KEY}`` —
    normal when collected from an instance that sources keys from env),
    ``empty`` (source had the field but it was blank — a template that
    still needs a value), and ``missing`` (no apiKey field anywhere in
    the source). ``state`` is the service-layer classification; if not
    provided, we fall back to inspecting ``raw_key`` alone (coarser)."""
    if reveal:
        if raw_key:
            return raw_key
        # No literal to show. Preserve the state distinction so callers
        # using --reveal specifically to debug provisioning still see
        # "no value because the field was blank" vs "no value because
        # the field was absent". Without this annotation, --reveal
        # collapses both into a flat ``-``.
        if state == "empty":
            return "- [dim](empty)[/dim]"
        if state == "missing":
            return "- [dim](unset)[/dim]"
        if state == "env-ref":
            # raw_key should not normally be blank when the state is
            # env-ref, but guard for the pathological case.
            return "- [dim](env-ref)[/dim]"
        return "-"
    if state == "env-ref":
        return "[cyan]env-ref[/cyan]"
    if state == "empty":
        return "[yellow]empty[/yellow]"
    if state == "missing":
        return "[dim]unset[/dim]"
    if state == "set":
        return "[green]set[/green]" if not wide else (_mask_secret(raw_key) or "-")
    # Fallback: derive from raw_key alone (used by tests that predate
    # the state field and by any caller that doesn't pass it through).
    if not raw_key:
        return "[dim]unset[/dim]"
    if _API_KEY_ENV_REF.match(raw_key.strip()):
        return "[cyan]env-ref[/cyan]"
    return "[green]set[/green]" if not wide else (_mask_secret(raw_key) or "-")


def _print_provider_stacked(records: list[dict], *, reveal: bool) -> None:
    """Key/value layout used when the terminal is too narrow to render
    the provider table readably — most commonly triggered by ``--reveal``
    at ~80 columns where a literal 40+ char API key crushes every other
    column to 1 character wide. Each provider prints as a short block
    rendered as a two-column Table so long values fold with a proper
    hanging indent (aligned under the value column) instead of wrapping
    flush-left where they read as separate records."""
    console.print("[bold]ClawCU Providers[/bold]")
    for idx, record in enumerate(records):
        raw_key = str(record.get("api_key") or "")
        state = record.get("api_key_state")
        key_cell = _provider_api_key_cell(
            raw_key,
            reveal=reveal,
            wide=True,  # stacked has no width pressure; show the literal key
            state=state if isinstance(state, str) else None,
        )
        console.print(
            f"[bold]{record['name']}[/bold]  [dim]({record.get('service', '-')})[/dim]"
        )
        detail = Table(show_header=False, box=None, pad_edge=False)
        detail.add_column(style="cyan", no_wrap=True)
        detail.add_column(overflow="fold")
        detail.add_row("  api_style", record["api_style"])
        detail.add_row("  api_key", key_cell)
        detail.add_row("  endpoint", record.get("endpoint") or "-")
        models = record.get("models") or []
        detail.add_row("  models", ", ".join(models) if models else "-")
        console.print(detail)
        if idx != len(records) - 1:
            console.print()


def _print_provider_legend(records: list[dict]) -> None:
    """Emit a legend describing only the api_key states that actually
    appear in the current view. Listing states that aren't present
    confuses users who then look for missing ``env-ref`` rows."""
    states_in_view = {
        str(r.get("api_key_state") or "").strip() for r in records
    }
    non_default = states_in_view - {"", "set"}
    if not non_default:
        return
    # Stable order: set, env-ref, empty, missing — matches the docs.
    ordered_states = [s for s in ("set", "env-ref", "empty", "missing")
                      if s in states_in_view]
    parts = [_API_KEY_STATE_LABELS[s] for s in ordered_states
             if s in _API_KEY_STATE_LABELS]
    if not parts:
        return
    console.print(
        "[dim]API_KEY legend: "
        + "; ".join(parts)
        + ". Pass --reveal to see the literal values.[/dim]"
    )


def _print_provider_table(records: list[dict], *, wide: bool = False, reveal: bool = False) -> None:
    # --reveal expands the API_KEY column to the literal key (40+ chars
    # for most providers) which crushes the rest of the table below
    # ~100 columns. Fall back to the stacked layout when the terminal
    # can't accommodate it; the info stays readable and the user sees
    # the full key per-line.
    if reveal and records and console.size.width < _REVEAL_PROVIDER_MIN_COLS:
        _print_provider_stacked(records, reveal=reveal)
        return

    effective_wide = wide
    if wide and console.size.width < _WIDE_PROVIDER_MIN_COLS:
        console.print(
            f"[yellow]--wide needs at least {_WIDE_PROVIDER_MIN_COLS} columns; "
            f"terminal is {console.size.width}. Falling back to the default view.[/yellow]"
        )
        effective_wide = False

    # Drop the redundant PROVIDER column by default — NAME and PROVIDER are
    # the same value in practice; the wide view used to carry both even at
    # narrow widths.
    table = Table(title="ClawCU Providers")
    table.add_column("SERVICE", no_wrap=True)
    table.add_column("NAME", no_wrap=True)
    table.add_column("API_STYLE", no_wrap=True)
    table.add_column("API_KEY", no_wrap=True)
    if effective_wide:
        table.add_column("ENDPOINT", overflow="fold")
    table.add_column("MODELS", overflow="fold")
    for record in records:
        raw_key = str(record.get("api_key") or "")
        state = record.get("api_key_state")
        key_cell = _provider_api_key_cell(
            raw_key, reveal=reveal, wide=effective_wide, state=state if isinstance(state, str) else None
        )
        models = record.get("models") or []
        if effective_wide:
            models_cell = ", ".join(models) or "-"
        else:
            model_count = len(models)
            models_cell = f"{model_count} {'model' if model_count == 1 else 'models'}" if models else "-"
        row: list[str] = []
        row.append(record.get("service", "-"))
        row.append(record["name"])
        row.append(record["api_style"])
        row.append(key_cell)
        if effective_wide:
            row.append(record.get("endpoint") or "-")
        row.append(models_cell)
        table.add_row(*row)
    console.print(table)
    # Legend line — only print it when the table actually uses one of
    # the non-default states, and only describe the states that appear
    # in this view so users don't hunt for rows that aren't there.
    if not reveal:
        _print_provider_legend(records)


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

# Help grouping panels — Typer renders options/commands under these headings.
_PANEL_INFO = "Info"
_PANEL_SETUP = "Setup"
_PANEL_LIFECYCLE = "Lifecycle"
_PANEL_CONFIG = "Configuration"
_PANEL_ACCESS = "Access"
_PANEL_DATA = "Environment & Data"
_PANEL_PROVIDERS = "Providers"
_PANEL_DIAG = "Diagnostics"


def _collect_environment_info() -> dict[str, str]:
    """Gather version / environment info used by --version and diagnostics."""
    info: dict[str, str] = {
        "clawcu": __version__,
        "python": f"{platform.python_version()} ({platform.python_implementation()})",
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
    }
    docker_path = shutil.which("docker") or ""
    info["docker_cli"] = docker_path or "not found"
    if docker_path:
        try:
            out = subprocess.run(
                [docker_path, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            server = (out.stdout or "").strip()
            info["docker_server"] = server or "unreachable"
        except Exception:
            info["docker_server"] = "unreachable"
    else:
        info["docker_server"] = "unreachable"
    try:
        service = ClawCUService()
        info["clawcu_home"] = str(service.get_clawcu_home())
        info["openclaw_image_repo"] = str(service.get_openclaw_image_repo() or "-")
        info["hermes_image_repo"] = str(service.get_hermes_image_repo() or "-")
        # "configured" if the bootstrap config file exists (setup has written
        # at least one persistent value). Otherwise the home path may be the
        # default location without any user-applied settings.
        info["setup_status"] = (
            "configured" if service.store.paths.config_path.exists() else "uninitialized"
        )
    except Exception:
        info.setdefault("clawcu_home", "-")
        info.setdefault("openclaw_image_repo", "-")
        info.setdefault("hermes_image_repo", "-")
        info.setdefault("setup_status", "unknown")
    return info


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
    ctx: typer.Context,
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
        info = _collect_environment_info()
        if json_output:
            _print_json(info)
        else:
            console.print(f"clawcu {info['clawcu']}")
            console.print(f"  python        : {info['python']}")
            console.print(f"  platform      : {info['platform']}")
            console.print(f"  docker cli    : {info['docker_cli']}")
            console.print(f"  docker server : {info['docker_server']}")
            console.print(f"  clawcu home   : {info['clawcu_home']} ({info['setup_status']})")
            console.print(f"  openclaw repo : {info['openclaw_image_repo']}")
            console.print(f"  hermes repo   : {info['hermes_image_repo']}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _show_help_and_exit(ctx)


@app.command(
    "setup",
    help=(
        "Check local prerequisites and configure the default ClawCU home and service image repos. "
        "Pass --non-interactive to accept all existing defaults without prompting (safe for CI)."
    ),
    rich_help_panel=_PANEL_SETUP,
)
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
    non_interactive: Annotated[
        bool,
        typer.Option(
            "--non-interactive",
            "--accept-defaults",
            help=(
                "Skip every prompt and accept the current saved value (or the built-in default when "
                "no saved value exists). Use in CI or scripts to bootstrap ClawCU without a TTY."
            ),
        ),
    ] = False,
) -> None:
    console.print("Checking local prerequisites for ClawCU...")
    service = get_service()
    checks = service.check_setup()
    if completion:
        checks.append(_completion_check(service))
    if _print_setup_checks(checks):
        is_interactive = _is_interactive_stdin() and not non_interactive
        has_explicit_config = any(
            value is not None
            for value in (
                clawcu_home,
                openclaw_image_repo,
                hermes_image_repo,
            )
        )
        if is_interactive or has_explicit_config or non_interactive:
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
                configured_repo = service.get_openclaw_image_repo() or service.suggest_openclaw_image_repo()
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
        else:
            console.print(
                "[yellow]Non-interactive shell detected.[/yellow] "
                "Pass `--non-interactive` to accept the current defaults, or pass one of "
                "`--clawcu-home`, `--openclaw-image-repo`, `--hermes-image-repo` to save explicit values."
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
    image: str | None,
    datadir: str | None,
    port: int | None,
    cpu: str,
    memory: str,
    a2a: bool = False,
    a2a_hop_budget: int | None = None,
    a2a_advertise_host: str | None = None,
    apply_provider: str | None = None,
    apply_agent: str = "main",
    apply_persist: bool = False,
) -> None:
    if service_name not in _KNOWN_SERVICES:
        _exit_with_error(
            f"Unknown service '{service_name}'. Expected one of: {', '.join(_KNOWN_SERVICES)}."
        )
    if a2a_hop_budget is not None and not a2a:
        _exit_with_error(
            "--a2a-hop-budget requires --a2a. Add --a2a or drop --a2a-hop-budget."
        )
    if a2a_advertise_host is not None and not a2a:
        _exit_with_error(
            "--a2a-advertise-host requires --a2a. Add --a2a or drop --a2a-advertise-host."
        )
    # a2a-design-5.md §P2-I: warn (not error) past the soft ceiling — above
    # 16 hops the budget stops being a useful loop-protection knob.
    if a2a_hop_budget is not None and a2a_hop_budget > 16:
        console.print(
            f"[yellow]Warning:[/yellow] --a2a-hop-budget={a2a_hop_budget} exceeds "
            "the soft ceiling of 16 — hop budget is intended to cap runaway loops, "
            "not to scale delegation depth."
        )
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        if service_name == "openclaw":
            record = service.create_openclaw(
                name=name, version=version, image=image, datadir=datadir, port=port, cpu=cpu, memory=memory, a2a=a2a, a2a_hop_budget=a2a_hop_budget, a2a_advertise_host=a2a_advertise_host,
            )
        else:
            record = service.create_hermes(
                name=name, version=version, image=image, datadir=datadir, port=port, cpu=cpu, memory=memory, a2a=a2a, a2a_hop_budget=a2a_hop_budget, a2a_advertise_host=a2a_advertise_host,
            )
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Created instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
    )
    if apply_provider:
        try:
            service.apply_provider(
                apply_provider,
                record.name,
                agent=apply_agent,
                persist=apply_persist,
            )
        except Exception as exc:
            console.print(
                f"[yellow]Instance created but --apply-provider failed:[/yellow] {exc}\n"
                f"[dim]Run `clawcu provider apply {apply_provider} {record.name}` to retry.[/dim]"
            )
        else:
            console.print(
                f"[green]Applied provider:[/green] {apply_provider} -> {record.name}/{apply_agent}"
            )
            # The container started before --apply-persist wrote the env
            # file, so its docker --env-file never saw the provider secret
            # (e.g. CLAWCU_PROVIDER_*_API_KEY). Recreate so the next start
            # mounts the freshly written env. Skip when persist is off —
            # nothing in the container env changed.
            if apply_persist:
                try:
                    service.recreate_instance(record.name, prepare_artifact=False)
                except Exception as exc:
                    console.print(
                        f"[yellow]Provider persisted but recreate failed:[/yellow] {exc}\n"
                        f"[dim]Run `clawcu recreate {record.name}` so the container picks up the new env.[/dim]"
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


_APPLY_PROVIDER_OPTION = typer.Option(
    "--apply-provider",
    help=(
        "Apply a collected provider to the new instance's default agent immediately "
        "after creation. The create succeeds even if apply fails; the error is surfaced "
        "and a retry command is printed."
    ),
)
_APPLY_AGENT_OPTION = typer.Option(
    "--apply-agent",
    help="Target agent name for --apply-provider. Defaults to main.",
)
_APPLY_PERSIST_OPTION = typer.Option(
    "--apply-persist",
    help=(
        "When set with --apply-provider, also persist the provider secret into the "
        "instance env file (matches `provider apply --persist`)."
    ),
)
_A2A_OPTION = typer.Option(
    "--a2a",
    help=(
        "Bake the A2A sidecar into the instance image so it speaks the A2A v0 "
        "protocol (AgentCard + /a2a/send) on a neighbor port. The base image "
        "is wrapped with the clawcu.a2a.sidecar_plugin assets for the service; the "
        "result is tagged clawcu/{service}-a2a:{base}-plugin{clawcu-version}."
    ),
)
_A2A_HOP_BUDGET_OPTION = typer.Option(
    "--a2a-hop-budget",
    min=1,
    help=(
        "Maximum number of A2A hops the sidecar will forward a single outbound "
        "call through before returning 508 Loop Detected. Persisted to the "
        "instance env file as A2A_HOP_BUDGET; defaults to 8 when unset. Requires "
        "--a2a."
    ),
)
_A2A_ADVERTISE_HOST_OPTION = typer.Option(
    "--a2a-advertise-host",
    help=(
        "Hostname peers will use to reach this sidecar. Default: "
        "host.docker.internal on macOS/Windows (Docker Desktop), 127.0.0.1 on "
        "Linux. Override when peers live on a different host or a named "
        "docker network. Requires --a2a."
    ),
)
_A2A_TRISTATE_OPTION = typer.Option(
    "--a2a/--no-a2a",
    help=(
        "Toggle the A2A flavor when recreating. Omit to preserve the instance's "
        "current flavor; pass --a2a to switch to the baked a2a image, or "
        "--no-a2a to drop back to the stock image. Flipping the flag re-runs "
        "artifact preparation."
    ),
)


@create_app.callback(invoke_without_command=True)
def create_callback(
    ctx: typer.Context,
    service: Annotated[
        str | None,
        typer.Option("--service", help=f"Service name ({' | '.join(_KNOWN_SERVICES)}). Unified alternative to the 'clawcu create <service>' subcommand form."),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Managed instance name.")] = None,
    version: Annotated[str | None, typer.Option("--version", help="Service version or git ref.")] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Optional runtime image override. When set, Docker starts this image while --version remains the recorded service version."),
    ] = None,
    datadir: Annotated[
        str | None,
        typer.Option("--datadir", help="Host data directory. Defaults to ~/.clawcu/{name}."),
    ] = None,
    port: Annotated[int | None, typer.Option("--port", help="Host port exposed for the instance.")] = None,
    cpu: Annotated[str, typer.Option("--cpu", help="Docker CPU limit.")] = "1",
    memory: Annotated[str, typer.Option("--memory", help="Docker memory limit.")] = "2g",
    a2a: Annotated[bool, _A2A_OPTION] = False,
    a2a_hop_budget: Annotated[int | None, _A2A_HOP_BUDGET_OPTION] = None,
    a2a_advertise_host: Annotated[str | None, _A2A_ADVERTISE_HOST_OPTION] = None,
    apply_provider: Annotated[str | None, _APPLY_PROVIDER_OPTION] = None,
    apply_agent: Annotated[str, _APPLY_AGENT_OPTION] = "main",
    apply_persist: Annotated[bool, _APPLY_PERSIST_OPTION] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if service:
        if not name or not version:
            _exit_with_error("--service requires --name and --version.")
        _do_create(
            service,
            name=name,
            version=version,
            image=image,
            datadir=datadir,
            port=port,
            cpu=cpu,
            memory=memory,
            a2a=a2a,
            a2a_hop_budget=a2a_hop_budget,
            a2a_advertise_host=a2a_advertise_host,
            apply_provider=apply_provider,
            apply_agent=apply_agent,
            apply_persist=apply_persist,
        )
        return
    _show_help_and_exit(ctx)


@provider_app.callback(invoke_without_command=True)
def provider_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _show_help_and_exit(ctx)


@pull_app.command("openclaw", rich_help_panel=_PANEL_SETUP)
def pull_openclaw(
    version: Annotated[str, typer.Option("--version", help="OpenClaw version to pull.")],
) -> None:
    _do_pull("openclaw", version)


@pull_app.command("hermes", rich_help_panel=_PANEL_SETUP)
def pull_hermes(
    version: Annotated[str, typer.Option("--version", help="Hermes git ref to pull and build.")],
) -> None:
    _do_pull("hermes", version)


@create_app.command("openclaw", rich_help_panel=_PANEL_LIFECYCLE)
def create_openclaw(
    name: Annotated[str, typer.Option("--name", help="Managed instance name.")],
    version: Annotated[str, typer.Option("--version", help="OpenClaw version to run.")],
    image: Annotated[
        str | None,
        typer.Option("--image", help="Optional runtime image override. When set, Docker starts this image while --version remains the recorded OpenClaw version."),
    ] = None,
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
    a2a: Annotated[bool, _A2A_OPTION] = False,
    a2a_hop_budget: Annotated[int | None, _A2A_HOP_BUDGET_OPTION] = None,
    a2a_advertise_host: Annotated[str | None, _A2A_ADVERTISE_HOST_OPTION] = None,
    apply_provider: Annotated[str | None, _APPLY_PROVIDER_OPTION] = None,
    apply_agent: Annotated[str, _APPLY_AGENT_OPTION] = "main",
    apply_persist: Annotated[bool, _APPLY_PERSIST_OPTION] = False,
) -> None:
    _do_create(
        "openclaw",
        name=name,
        version=version,
        image=image,
        datadir=datadir,
        port=port,
        cpu=cpu,
        memory=memory,
        a2a=a2a,
        a2a_hop_budget=a2a_hop_budget,
        a2a_advertise_host=a2a_advertise_host,
        apply_provider=apply_provider,
        apply_agent=apply_agent,
        apply_persist=apply_persist,
    )


@create_app.command("hermes", rich_help_panel=_PANEL_LIFECYCLE)
def create_hermes(
    name: Annotated[str, typer.Option("--name", help="Managed instance name.")],
    version: Annotated[str, typer.Option("--version", help="Hermes git ref to run.")],
    image: Annotated[
        str | None,
        typer.Option("--image", help="Optional runtime image override. When set, Docker starts this image while --version remains the recorded Hermes version."),
    ] = None,
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
    a2a: Annotated[bool, _A2A_OPTION] = False,
    a2a_hop_budget: Annotated[int | None, _A2A_HOP_BUDGET_OPTION] = None,
    a2a_advertise_host: Annotated[str | None, _A2A_ADVERTISE_HOST_OPTION] = None,
    apply_provider: Annotated[str | None, _APPLY_PROVIDER_OPTION] = None,
    apply_agent: Annotated[str, _APPLY_AGENT_OPTION] = "main",
    apply_persist: Annotated[bool, _APPLY_PERSIST_OPTION] = False,
) -> None:
    _do_create(
        "hermes",
        name=name,
        version=version,
        image=image,
        datadir=datadir,
        port=port,
        cpu=cpu,
        memory=memory,
        a2a=a2a,
        a2a_hop_budget=a2a_hop_budget,
        a2a_advertise_host=a2a_advertise_host,
        apply_provider=apply_provider,
        apply_agent=apply_agent,
        apply_persist=apply_persist,
    )


@hermes_identity_app.command(
    "set",
    help=(
        "Install a file as the persona (SOUL.md) for a hermes instance.\n\n"
        "The file is copied into the instance's datadir where the container's "
        "HERMES_HOME mount makes it `$HERMES_HOME/SOUL.md`. Hermes "
        "re-reads it on every chat turn — no restart required."
    ),
)
def hermes_identity_set(
    name: Annotated[str, typer.Argument(help="Managed hermes instance name.")],
    source: Annotated[str, typer.Argument(help="Path to a local markdown/text file.")],
) -> None:
    service = get_service()
    try:
        result = service.set_hermes_identity(name, source)
    except ValueError as exc:
        _exit_with_error(str(exc))
    except FileNotFoundError as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Installed persona[/green] for [bold]{result['instance']}[/bold]: "
        f"{result['source']} -> {result['target']} ({result['bytes']} bytes)."
    )
    console.print(
        "[dim]Next chat turn will pick it up automatically; no restart needed.[/dim]"
    )


@provider_app.command(
    "collect",
    help=(
        "Collect model configuration assets from managed instances or local agent homes. "
        "--all / --instance / --path are mutually exclusive."
    ),
)
def collect_providers(
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
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help=(
                "Overwrite existing collected assets that share the same provider signature, "
                "rather than merging (default) or skipping identical copies."
            ),
        ),
    ] = False,
) -> None:
    if not all_instances and not instance and not path:
        _exit_with_error(
            "provider collect needs a target scope: pass one of --all, --instance <name>, or --path <home>."
        )
    try:
        result = get_service().collect_providers(
            all_instances=all_instances,
            instance=instance,
            path=path,
            overwrite=overwrite,
        )
    except TypeError:
        # Older service builds don't accept overwrite — fall back silently.
        try:
            result = get_service().collect_providers(
                all_instances=all_instances,
                instance=instance,
                path=path,
            )
        except Exception as exc:
            _exit_with_error(str(exc))
    except Exception as exc:
        _exit_with_error(str(exc))
    for saved in result["saved"]:
        console.print(f"[green]Collected provider:[/green] {saved}")
    for merged in result.get("merged", []):
        console.print(f"[blue]Merged duplicate:[/blue] {merged}")
    for overwritten in result.get("overwritten", []):
        console.print(f"[magenta]Overwrote existing:[/magenta] {overwritten}")
    for skipped in result["skipped"]:
        console.print(f"[yellow]Skipped duplicate:[/yellow] {skipped}")
    saved_count = len(result["saved"])
    merged_count = len(result.get("merged", []))
    overwritten_count = len(result.get("overwritten", []))
    skipped_count = len(result["skipped"])
    scanned_count = len(result.get("scanned", []))
    if not saved_count and not merged_count and not overwritten_count and not skipped_count:
        console.print("No provider assets were found.")
        return
    summary = (
        "Collect summary: "
        f"scanned {scanned_count} source(s), "
        f"collected {saved_count}, "
        f"merged {merged_count}, "
    )
    if overwritten_count:
        summary += f"overwrote {overwritten_count}, "
    summary += f"skipped {skipped_count}."
    console.print(summary)


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
    name: Annotated[str, typer.Argument(help="Provider name.")],
    reveal: Annotated[bool, typer.Option("--reveal", help="Show unmasked secrets. Off by default for safety.")] = False,
) -> None:
    try:
        payload = get_service().show_provider(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print_json(json.dumps(_redact_provider_payload(payload, reveal=reveal), ensure_ascii=False))


@provider_app.command("apply", help="Apply a collected provider to a managed instance agent.")
def apply_provider(
    provider: Annotated[str, typer.Argument(help="Collected provider name.")],
    instance: Annotated[str, typer.Argument(help="Managed instance name.")],
    agent: Annotated[str, typer.Option("--agent", help="Target agent name. Defaults to main.")] = "main",
    persist: Annotated[
        bool,
        typer.Option(
            "--persist",
            help="Also persist the provider secret to the instance env file and write an env reference into root openclaw.json.",
        ),
    ] = False,
    primary: Annotated[str | None, typer.Option("--primary", help="Set the agent primary model.")] = None,
    fallback: Annotated[
        list[str] | None,
        typer.Option(
            "--fallback",
            help="Fallback model for the agent. Repeat the flag to add more (e.g. --fallback a --fallback b).",
        ),
    ] = None,
    fallbacks: Annotated[
        str | None,
        typer.Option(
            "--fallbacks",
            help="Comma-separated fallback model list (legacy; prefer repeating --fallback).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview which files would be written without touching disk.",
        ),
    ] = False,
) -> None:
    # Merge the two fallback flags. --fallback (repeatable) takes precedence;
    # fall back to --fallbacks (csv) for back-compat with earlier scripts.
    fallback_list: list[str] | None = None
    if fallback:
        fallback_list = [item.strip() for item in fallback if item and item.strip()]
    elif fallbacks is not None:
        fallback_list = [item.strip() for item in fallbacks.split(",") if item.strip()]

    service = get_service()

    if dry_run:
        try:
            plan = service.plan_apply_provider(
                provider,
                instance,
                agent,
                persist=persist,
                primary=primary,
                fallbacks=fallback_list,
            )
        except Exception as exc:
            _exit_with_error(str(exc))
        _print_apply_provider_plan(plan)
        return

    try:
        result = service.apply_provider(
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
    if result.get("runtime_dir"):
        console.print(f"  Runtime dir: [blue]{result['runtime_dir']}[/blue]")
    if persist:
        if result.get("env_key") and result.get("env_key") != "-":
            console.print(
                f"  Persistence: config now uses [blue]${{{result.get('env_key', '-')}}}[/blue] and the secret was stored in the instance env file."
            )
        elif result.get("env_path"):
            console.print(
                f"  Persistence: config and env were updated in [blue]{result['env_path']}[/blue]."
            )
    if primary or fallback_list is not None:
        console.print(
            "  Agent models: "
            f"primary={result.get('primary', '-')} "
            f"fallbacks={result.get('fallbacks', '-')}"
        )


def _print_apply_provider_plan(plan: dict) -> None:
    """Render an apply_provider_plan payload as a compact summary."""
    table = Table(show_header=False, box=None, pad_edge=False, title="Apply plan (dry-run)")
    table.add_column(style="bold cyan")
    table.add_column()
    table.add_row("Provider", str(plan.get("provider", "-")))
    table.add_row("Service", str(plan.get("service", "-")))
    table.add_row("Instance", str(plan.get("instance", "-")))
    table.add_row("Agent", str(plan.get("agent", "-")))
    table.add_row("Runtime dir", str(plan.get("runtime_dir", "-")))
    writes = plan.get("writes") or []
    if writes:
        table.add_row("Would write", "\n".join(str(item) for item in writes))
    env_key = plan.get("env_key") or "-"
    if plan.get("persist"):
        table.add_row("Env key", f"${{{env_key}}}" if env_key != "-" else "-")
        table.add_row("Env file", str(plan.get("env_path", "-")))
    table.add_row("Primary", str(plan.get("primary", "-")))
    table.add_row("Fallbacks", str(plan.get("fallbacks", "-")))
    console.print(table)
    console.print("[dim]Dry run: nothing on disk was modified.[/dim]")


@provider_app.command("remove", help="Remove a collected provider directory.")
def remove_provider(
    name: Annotated[str, typer.Argument(help="Provider name.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Remove even if managed instances still reference this provider.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    service = get_service()
    try:
        in_use = service.find_instances_using_provider(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    summary = (
        f"About to delete collected provider '{name}'. Its auth-profiles.json and models.json will be removed."
    )
    if in_use:
        refs = ", ".join(f"{row['instance']}/{row['agent']}" for row in in_use)
        if not force:
            _exit_with_error(
                f"Provider '{name}' is in use by: {refs}.\n"
                "Re-run with --force to delete the bundle anyway "
                "(the referenced instances will keep their existing env values "
                "but will lose access to the collected models.json)."
            )
        summary += f"\n[bold red]Warning:[/bold red] in use by {refs}."
    _confirm_destructive(summary, yes)
    try:
        service.remove_provider(name, force=force)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[yellow]Removed provider:[/yellow] {name}")


def _list_provider_models_impl(name: str, json_output: bool) -> None:
    _set_json_mode(json_output)
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


@provider_app.command(
    "models",
    help=(
        "List the models stored in a collected provider. "
        "Replaces the older `clawcu provider models list <name>` form — "
        "the trailing `list` level is no longer required."
    ),
)
def list_provider_models(
    name: Annotated[str, typer.Argument(help="Provider name.")],
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _list_provider_models_impl(name, json_output)
_LIST_SOURCES = ("managed", "local", "removed", "all")


def _resolve_list_source(
    source: str | None,
    *,
    local_flag: bool,
    managed_flag: bool,
    removed_flag: bool,
    all_flag: bool,
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
        if removed_flag and source != "removed":
            _exit_with_error(
                f"--removed cannot be combined with --source {source}; drop one of them."
            )
        if local_flag and source not in {"local", "all"}:
            _exit_with_error(
                f"--local cannot be combined with --source {source}; drop one of them."
            )
        if managed_flag and source not in {"managed", "all"}:
            _exit_with_error(
                f"--managed cannot be combined with --source {source}; drop one of them."
            )
        if all_flag and source != "all":
            _exit_with_error(
                f"--all cannot be combined with --source {source}; drop one of them."
            )
        return source
    if removed_flag and (local_flag or managed_flag or all_flag):
        _exit_with_error(
            "--removed cannot be combined with --local/--managed/--all; drop one of them."
        )
    if all_flag:
        return "all"
    if removed_flag:
        return "removed"
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
    "dashboard",
    help="Serve the local dashboard for managed, local, and removed instances.",
    rich_help_panel=_PANEL_INFO,
)
def dashboard(
    host: Annotated[
        str,
        typer.Option("--host", help="Host interface to bind the dashboard server to."),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", min=1, max=65535, help="Port to bind the dashboard server to."),
    ] = 8765,
    open_browser: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open the dashboard URL in the default browser after starting."),
    ] = True,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground/--background",
            help="Run the dashboard server in the current terminal instead of detaching it.",
        ),
    ] = False,
) -> None:
    try:
        if foreground:
            serve_dashboard(host=host, port=port, open_browser=open_browser)
            return

        primary_url = f"http://{host}:{port}"
        if _dashboard_is_healthy(primary_url):
            typer.echo(f"ClawCU dashboard is already running at {primary_url}")
            if open_browser:
                webbrowser.open(primary_url)
            return

        clawcu_bin = shutil.which("clawcu") or sys.argv[0]
        cmd = [clawcu_bin, "dashboard", "--host", host, "--port", str(port), "--foreground", "--no-open"]
        subprocess.Popen(  # noqa: S603
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        candidate_ports = [port]
        if port == 8765:
            candidate_ports.extend(range(port + 1, port + 21))

        healthy_url = None
        deadline = time.time() + 12.0
        while time.time() < deadline:
            for candidate_port in candidate_ports:
                candidate_url = f"http://{host}:{candidate_port}"
                if _dashboard_is_healthy(candidate_url):
                    healthy_url = candidate_url
                    break
            if healthy_url:
                break
            time.sleep(0.2)

        if healthy_url:
            typer.echo(f"ClawCU dashboard is running at {healthy_url}")
            if open_browser:
                webbrowser.open(healthy_url)
        else:
            typer.echo(
                "ClawCU dashboard is starting in the background. "
                f"Check http://{host}:{port} in a moment if the browser does not open automatically."
            )
    except Exception as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


@app.command(
    "list",
    help=(
        "List managed instances. By default shows ClawCU-managed instances only; "
        "pass --source local or --source all to include ~/.openclaw / ~/.hermes pseudo-entries, "
        "or use --removed to show orphaned instance homes left behind after record deletion. "
        "ACCESS URLs have the #token=... fragment masked by default; pass --reveal to show the literal token. "
        "Alias: `ls`."
    ),
    rich_help_panel=_PANEL_INFO,
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
            help="managed | local | removed | all. Default: managed (hides local pseudo-instances).",
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
    removed: Annotated[
        bool,
        typer.Option(
            "--removed",
            help="Show orphaned instance data directories under CLAWCU_HOME whose records were deleted.",
        ),
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
    include_remote: Annotated[
        bool,
        typer.Option(
            "--remote/--no-remote",
            help=(
                "Append a top-10 'Available versions' block per service (OpenClaw, Hermes) "
                "fetched from the configured image registries. Default on; pass --no-remote "
                "for a strictly offline view (CI, airgapped, slow networks)."
            ),
        ),
    ] = True,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    resolved_source = _resolve_list_source(
        source,
        local_flag=local,
        managed_flag=managed,
        removed_flag=removed,
        all_flag=all_sources,
    )
    effective_status = status_filter
    if running and not effective_status:
        effective_status = "running"
    elif running and effective_status and effective_status.lower() != "running":
        _exit_with_error("--running conflicts with --status; use one or the other.")
    if agents and resolved_source == "removed":
        _exit_with_error("--removed does not support --agents.")
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
            if resolved_source in {"removed", "all"}:
                records.extend(service.list_removed_instance_summaries())
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

    # Versions block is a human-only footer; skip for --json (scripts
    # already know which versions they care about), --agents (narrow
    # per-agent view), and --removed (archival view, not an upgrade
    # surface). Guarded with hasattr so older service implementations
    # and test stubs without the method degrade to "silently skip".
    if (
        not agents
        and resolved_source in {"managed", "local", "all"}
        and hasattr(service, "list_service_available_versions")
    ):
        try:
            versions_payload = service.list_service_available_versions(
                include_remote=include_remote
            )
        except Exception as exc:
            console.print(
                f"[yellow]Skipped available-versions fetch: {exc}[/yellow]"
            )
        else:
            _print_available_versions(versions_payload, limit=10)


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

    # --- A2A (review-2 P1-F / iter 3) ---
    a2a = payload.get("a2a")
    if a2a and a2a.get("enabled"):
        console.print()
        console.print("[bold]A2A[/bold]")
        a2a_table = Table(show_header=False, box=None, pad_edge=False)
        a2a_table.add_column("key", style="cyan", no_wrap=True)
        a2a_table.add_column("value", overflow="fold")
        a2a_table.add_row("Enabled", "yes")
        a2a_table.add_row("Port", str(a2a.get("port", "-")))
        a2a_table.add_row(
            "Registry URL", str(a2a.get("registry_url") or "-")
        )
        budget = a2a.get("hop_budget")
        default_budget = a2a.get("hop_budget_default", 8)
        if budget is None:
            a2a_table.add_row("Hop budget", f"{default_budget} (default)")
        else:
            a2a_table.add_row("Hop budget", str(budget))
        mcp_url = a2a.get("mcp_url")
        if mcp_url:
            a2a_table.add_row("MCP server", f"{mcp_url} (auto)")
        console.print(a2a_table)

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
    help=(
        "Show detailed state for a managed instance. Default is a compact readable view; "
        "pass --json for the full payload. Like `list`, `inspect` masks the dashboard token "
        "and strips the `#token=...` fragment from the URL by default — pass --reveal to "
        "render the raw token and keep the full URL."
    ),
    rich_help_panel=_PANEL_INFO,
)
def inspect_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
    show_history: Annotated[
        bool,
        typer.Option("--show-history", help="Expand the full history timeline (folded by default)."),
    ] = False,
    reveal: Annotated[
        bool,
        typer.Option(
            "--reveal",
            help=(
                "Show the raw dashboard token and keep the `#token=...` fragment on the "
                "access URL. Off by default for safety (masked token, stripped fragment)."
            ),
        ),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
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
        "Default shows both the token and the access URL with the `#token=…` anchor. "
        "Unlike `list`, `token` always prints the literal value (that's the whole point); "
        "avoid piping its output to shared logs."
    ),
    rich_help_panel=_PANEL_ACCESS,
)
def token_for_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
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


@app.command(
    "setenv",
    help="Set environment variables for a managed instance.",
    rich_help_panel=_PANEL_DATA,
)
def set_instance_env(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
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
    if from_file is not None and assignments:
        _exit_with_error("Use either inline KEY=VALUE arguments or --from-file, not both.")
    if from_file is None and not assignments:
        _exit_with_error(
            "setenv needs env input: pass one or more KEY=VALUE assignments, or use --from-file <path>."
        )
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
    # Flag empty-value assignments. These are legal (they write KEY= to
    # the env file) but indistinguishable from a typo — surface them so
    # the user can pick between `unsetenv KEY` (delete) and a real value.
    empty_value_keys: list[str] = []
    for assignment in effective_assignments:
        if "=" in assignment:
            key, value = assignment.split("=", 1)
            if not value.strip():
                empty_value_keys.append(key.strip())
    try:
        result = service.set_instance_env(name, effective_assignments)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(
        f"[green]Updated env file:[/green] {result['path']} ({', '.join(result['updated_keys'])})"
    )
    if empty_value_keys:
        joined = ", ".join(empty_value_keys)
        console.print(
            f"[yellow]Note:[/yellow] wrote empty string for [bold]{joined}[/bold]. "
            f"Use `clawcu unsetenv {result['instance']} {empty_value_keys[0]}` to remove the key entirely."
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


@app.command(
    "getenv",
    help="List environment variables configured for a managed instance.",
    rich_help_panel=_PANEL_DATA,
)
def get_instance_env(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
    reveal: Annotated[
        bool,
        typer.Option("--reveal", help="Show unmasked values for KEY/TOKEN/SECRET/PASSWORD entries. Off by default."),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
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


@app.command(
    "unsetenv",
    help="Remove environment variables configured for a managed instance.",
    rich_help_panel=_PANEL_DATA,
)
def unset_instance_env(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
    keys: Annotated[list[str], typer.Argument(help="One or more environment variable names.")],
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


@app.command(
    "approve",
    help=(
        "Approve a pending browser pairing request for an instance. "
        "Pass --list to enumerate pending requests without approving."
    ),
    rich_help_panel=_PANEL_ACCESS,
)
def approve_pairing(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
    request_id: Annotated[str | None, typer.Argument(help="Specific pairing request id to approve.")] = None,
    list_pending: Annotated[
        bool,
        typer.Option(
            "--list",
            help="List pending pairing requests for this instance without approving any of them.",
        ),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)

    if list_pending:
        try:
            pending = service.list_pending_pairings(name)
        except Exception as exc:
            _exit_with_error(str(exc))
        if _json_mode():
            _print_json({"instance": name, "pending": pending})
            return
        if not pending:
            console.print(f"No pending pairing requests for '{name}'.")
            return
        table = Table(title=f"Pending pairings for {name}")
        table.add_column("REQUEST_ID", no_wrap=True)
        table.add_column("DEVICE", overflow="fold")
        table.add_column("REQUESTED", no_wrap=True)
        for entry in pending:
            req_id = str(entry.get("requestId", "-"))
            device = str(entry.get("device") or entry.get("deviceName") or entry.get("userAgent") or "-")
            ts = entry.get("ts") or entry.get("timestamp") or "-"
            table.add_row(req_id, device, str(ts))
        console.print(table)
        console.print(
            f"[dim]Run `clawcu approve {name} <REQUEST_ID>` to approve a specific request.[/dim]"
        )
        return

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
    rich_help_panel=_PANEL_ACCESS,
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
    rich_help_panel=_PANEL_ACCESS,
)
def exec_instance(
    ctx: typer.Context,
    help_flag: Annotated[
        bool,
        typer.Option("--help", "-h", help="Show passthrough usage and examples.", is_eager=True),
    ] = False,
    name: Annotated[str | None, typer.Argument(help="Managed instance name.")] = None,
    workdir: Annotated[
        str | None,
        typer.Option("--workdir", "-w", help="Working directory inside the container (docker exec --workdir)."),
    ] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", "-u", help="User inside the container, e.g. 1000:1000 (docker exec --user)."),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env", "-e",
            help="Extra environment variable as KEY=VALUE. Repeat to set more. Overrides the adapter's default env.",
        ),
    ] = None,
) -> None:
    if help_flag or not name or not ctx.args:
        _show_passthrough_help(
            "exec",
            "This command runs the provided command inside the managed instance container.",
            [
                "clawcu exec <instance> openclaw config",
                "clawcu exec <instance> pwd",
                "clawcu exec <instance> ls",
                "clawcu exec --workdir /tmp <instance> ls",
                "clawcu exec --user 1000:1000 <instance> id",
                "clawcu exec --env FOO=bar <instance> env",
            ],
            usage="Usage: clawcu exec [OPTIONS] [NAME] COMMAND [ARGS]...",
        )
    extra_env: dict[str, str] = {}
    for item in env or []:
        if "=" not in item:
            _exit_with_error(f"--env expects KEY=VALUE, got: {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            _exit_with_error(f"--env key must be non-empty: {item!r}")
        extra_env[key] = value
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    extra_args = list(ctx.args)
    try:
        try:
            service.exec_instance(
                name,
                extra_args,
                workdir=workdir,
                user=user,
                extra_env=extra_env or None,
            )
        except TypeError:
            # Older service builds don't accept workdir/user/extra_env.
            if workdir or user or extra_env:
                _exit_with_error(
                    "The installed ClawCU service does not support --workdir / --user / --env yet."
                )
            service.exec_instance(name, extra_args)
    except Exception as exc:
        _exit_with_error(str(exc))


@app.command(
    "tui",
    help=(
        "Launch the native interactive TUI or chat flow for a managed instance. "
        "Pass --list-agents to print the available agent names without launching."
    ),
    rich_help_panel=_PANEL_ACCESS,
)
def tui_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
    agent: Annotated[
        str,
        typer.Option(
            "--agent",
            help=(
                "Target agent name. Defaults to 'main'. For OpenClaw this maps to "
                "the agent runtime directory; for Hermes it maps to the chat profile."
            ),
        ),
    ] = "main",
    list_agents: Annotated[
        bool,
        typer.Option(
            "--list-agents",
            help="List the agent names configured for this instance and exit without launching.",
        ),
    ] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    _set_json_mode(json_output)
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)

    if list_agents:
        try:
            agents = service.list_agents(name)
        except Exception as exc:
            _exit_with_error(str(exc))
        if _json_mode():
            _print_json({"instance": name, "agents": agents})
            return
        if not agents:
            console.print(f"No agents configured for '{name}'.")
            return
        for agent_name in agents:
            marker = " [dim](default)[/dim]" if agent_name == "main" else ""
            console.print(f"- {agent_name}{marker}")
        return

    try:
        service.tui_instance(name, agent=agent)
    except Exception as exc:
        _exit_with_error(str(exc))


@app.command(
    "start",
    help="Start a stopped managed instance.",
    rich_help_panel=_PANEL_LIFECYCLE,
)
def start_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
) -> None:
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.start_instance(name)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Started instance:[/green] {record.name}")
    _print_access_url(service, record.name)


@app.command(
    "stop",
    help="Stop a running managed instance.",
    rich_help_panel=_PANEL_LIFECYCLE,
)
def stop_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
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
    try:
        record = get_service().stop_instance(name, timeout=time)
    except Exception as exc:
        _exit_with_error(str(exc))
    suffix = f" (grace {time}s)" if time is not None else ""
    console.print(f"[yellow]Stopped instance:[/yellow] {record.name}{suffix}")


@app.command(
    "restart",
    help="Restart a managed instance.",
    rich_help_panel=_PANEL_LIFECYCLE,
)
def restart_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
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


def _do_recreate(
    service: ClawCUService,
    name: str,
    *,
    fresh: bool = False,
    timeout: int | None = None,
    version: str | None = None,
    a2a: bool | None = None,
) -> None:
    """Unified recreate logic.

    Tries retry_instance first (cheap auto-port recovery path for
    create_failed records); if the service rejects it with a
    ValueError ("Only create_failed ..."), falls back to the regular
    recreate_instance flow. When ``fresh``, ``timeout``, ``version``,
    or ``a2a`` is provided, skip the retry shortcut and go straight to
    recreate so the toggles take effect.
    """
    if fresh or timeout is not None or version is not None or a2a is not None:
        try:
            record = service.recreate_instance(
                name, fresh=fresh, timeout=timeout, version=version, a2a=a2a,
            )
        except Exception as exc:
            _exit_with_error(str(exc))
        console.print(
            f"[green]Recreated instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
        )
        _print_access_url(service, record.name)
        return
    try:
        record = service.retry_instance(name)
    except FileNotFoundError:
        try:
            record = service.recreate_instance(name, version=version, a2a=a2a)
        except Exception as exc2:
            _exit_with_error(str(exc2))
        console.print(
            f"[green]Recreated instance:[/green] {record.name} ({record.version}) on port {record.port} (status: {record.status})"
        )
        _print_access_url(service, record.name)
        return
    except ValueError as exc:
        message = str(exc)
        if "create_failed" not in message:
            _exit_with_error(message)
        try:
            record = service.recreate_instance(name, version=version, a2a=a2a)
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
    help="Recreate an existing instance, or recover a removed instance from its leftover datadir. Auto-retries instances in create_failed status.",
    rich_help_panel=_PANEL_LIFECYCLE,
)
def recreate_instance(
    name: Annotated[str, typer.Argument(help="Instance to recreate.")],
    fresh: Annotated[
        bool,
        typer.Option(
            "--fresh",
            help="Wipe the instance datadir before recreating. Destructive: data is irrecoverable.",
        ),
    ] = False,
    timeout: Annotated[
        int | None,
        typer.Option(
            "--timeout",
            min=0,
            help="Seconds to wait for the container to stop gracefully before force-removing.",
        ),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            help="Version to use when recreating a removed instance whose datadir no longer has an instance record.",
        ),
    ] = None,
    a2a: Annotated[bool | None, _A2A_TRISTATE_OPTION] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt for --fresh."),
    ] = False,
) -> None:
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    if fresh:
        # Validate the instance exists *before* the destructive prompt.
        # Otherwise a typo prints the "--yes required" warning even though
        # there is nothing to wipe, hiding the real "not found" failure.
        try:
            service.store.load_record(name)
        except Exception:
            try:
                service._build_removed_instance_spec(name, version=version)
            except Exception as exc:
                _exit_with_error(str(exc))
        _confirm_destructive(
            f"About to wipe the datadir of instance '{name}' before recreating. "
            "All instance data under the datadir will be permanently deleted. "
            "Historical snapshots under ~/.clawcu/snapshots/ are NOT touched — "
            "use 'clawcu rollback --list' after the wipe to see what remains.",
            yes,
        )
    _do_recreate(service, name, fresh=fresh, timeout=timeout, version=version, a2a=a2a)


def _print_upgrade_plan(plan: dict) -> None:
    """Render an upgrade_plan payload as a compact human-readable summary."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(overflow="fold")
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
    remote_requested_preview = bool(payload.get("remote_requested"))
    image_repo_display = payload.get("image_repo", "-") or "-"
    if not remote_requested_preview:
        # Keep the repo on screen for reference but cross-link it to the
        # Remote line so the two rows read as one thought instead of
        # ``here's the registry / oh by the way we didn't ask it``.
        console.print(
            f"[bold]Image repo:[/bold] {image_repo_display} "
            "[dim](remote skipped by --no-remote)[/dim]"
        )
    else:
        console.print(f"[bold]Image repo:[/bold] {image_repo_display}")
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
            markers: list[str] = []
            if tag == current:
                markers.append("current")
            if not is_semver_release_tag(tag):
                markers.append("non-release")
            suffix = f" [dim]({', '.join(markers)})[/dim]" if markers else ""
            console.print(f"  - {tag}{suffix}")
    else:
        console.print(
            "[bold]Local images:[/bold] [dim]none found for this repo; Docker will pull on upgrade[/dim]"
        )

    remote_requested = bool(payload.get("remote_requested"))
    remote = payload.get("remote_versions")
    remote_error = payload.get("remote_error")
    remote_registry = payload.get("remote_registry")
    if not remote_requested:
        # Already communicated inline on the Image repo row above — no
        # separate Remote section needed when the flag disabled the
        # registry query entirely.
        pass
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
            f" (showing {len(display)} of {total} release tags, newest by semver)"
            if truncated
            else f" ({total} release tags, oldest → newest)"
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
    rich_help_panel=_PANEL_LIFECYCLE,
)
def upgrade_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
    version: Annotated[str | None, typer.Option("--version", help="Target service version or git ref. Required unless --list-versions is passed.")] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Optional runtime image override. When set, Docker starts this image while --version remains the recorded target version."),
    ] = None,
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
        _exit_with_error(
            "upgrade needs a target version: pass --version <v>, or use --list-versions to see candidates."
        )

    try:
        plan = service.upgrade_plan(name, version=version, image=image)
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
        record = service.upgrade_instance(name, version=version, image=image)
    except Exception as exc:
        _exit_with_error(str(exc))
    console.print(f"[green]Upgraded instance:[/green] {record.name} -> {record.version}")
    _print_access_url(service, record.name)


def _print_rollback_plan(plan: dict) -> None:
    """Render a rollback_plan payload as a compact human-readable summary."""
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(overflow="fold")
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


_ROLLBACK_TARGETS_MIN_COLS = 120


def _print_rollback_targets_stacked(targets: list[dict]) -> None:
    """Key/value layout for `rollback --list` in narrow terminals.

    The Snapshot column holds absolute paths that typically don't fit
    alongside timestamp + version columns under 120 cols; printing the
    path on its own line keeps it copy-pasteable.
    """
    for idx, entry in enumerate(targets):
        snapshot = entry.get("snapshot_dir") or "-"
        exists = entry.get("snapshot_exists")
        exists_marker = (
            "[green]present[/green]" if exists else "[red]missing[/red]"
        )
        console.print(
            f"[bold]#{idx}[/bold]  action: {entry.get('action') or '-'}  "
            f"restores to: [bold green]{entry.get('restores_to') or '-'}[/bold green]"
        )
        console.print(
            f"    from -> to: {entry.get('from_version') or '-'} -> {entry.get('to_version') or '-'}"
        )
        console.print(f"    when:       {entry.get('timestamp') or '-'}")
        console.print(f"    snapshot:   {snapshot}  {exists_marker}")
        if idx != len(targets) - 1:
            console.print()


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
    width = console.size.width
    if width < _ROLLBACK_TARGETS_MIN_COLS:
        _print_rollback_targets_stacked(targets)
        console.print(
            "[dim]rollback --to <version> restores the most recent event whose "
            "'restores to' matches. omit --to to pick the newest entry.[/dim]"
        )
        return
    table = Table(show_header=True, box=None, pad_edge=False)
    table.add_column("#", style="bold")
    table.add_column("Action")
    table.add_column("Restores to", style="bold green")
    table.add_column("From -> To")
    table.add_column("When")
    table.add_column("Snapshot", overflow="fold")
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
    rich_help_panel=_PANEL_LIFECYCLE,
)
def rollback_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
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
        # Under --dry-run, "no rollback history" is not a hard failure
        # — surface the same empty-state hint that `--list` prints so
        # the user's next step is obvious.
        message = str(exc)
        if dry_run and "has no rollback history" in message:
            console.print(f"[bold]Instance:[/bold] {name}")
            console.print(
                "[dim]No rollback targets recorded yet. Run 'clawcu upgrade' "
                "to produce a snapshot first, or 'clawcu rollback --list' to "
                "enumerate available targets once they exist.[/dim]"
            )
            return
        _exit_with_error(message)

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
        "touching the original. Clone always creates a new instance "
        "and never overwrites the source; no --yes confirmation is "
        "needed (clone will refuse if --name already exists)."
    ),
    rich_help_panel=_PANEL_LIFECYCLE,
)
def clone_instance(
    source_name: Annotated[str, typer.Argument(help="Source instance name.")],
    target_name: Annotated[
        str | None,
        typer.Argument(
            metavar="[TARGET]",
            help=(
                "Target clone name. REQUIRED unless --name is passed — one of "
                "the two must be provided, and passing both is an error. Using "
                "the second positional matches the `git clone <source> <target>` "
                "convention; the --name option stays for backward compatibility."
            ),
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(
            "--name",
            help=(
                "New cloned instance name. REQUIRED unless the TARGET positional "
                "is supplied — pre-0.2.5 scripts that used this option keep working."
            ),
        ),
    ] = None,
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
    # Resolve the new clone name from either the TARGET positional
    # (docker/git-style) or the original --name option. Exactly one must
    # be set; passing both is ambiguous and we'd rather fail loud than
    # silently pick one. The original --name form stays so pre-0.2.5
    # scripts keep working.
    if target_name and name:
        _exit_with_error(
            "Pass either the TARGET positional or --name, not both."
        )
    resolved_name = target_name or name
    if not resolved_name:
        _exit_with_error(
            "clone needs a target name: `clawcu clone <source> <target>` or "
            "`clawcu clone <source> --name <target>`."
        )
    service = get_service()
    if hasattr(service, "set_reporter"):
        service.set_reporter(_print_progress)
    try:
        record = service.clone_instance(
            source_name,
            name=resolved_name,
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
        try:
            providers = [
                entry for entry in service.list_providers()
                if entry.get("service") == record.service
            ]
        except Exception:
            providers = []
        if providers:
            # Show up to 3 names so the line stays scannable, but promote
            # the provider the source was actually using to the front of
            # the list (labeled) — that's the one the user most likely
            # wants to re-apply, and without promotion it may not even
            # land in the 3-name window.
            try:
                active = service.active_provider_for_instance(source_name)
            except Exception:
                active = None
            all_names = sorted(entry.get("name", "?") for entry in providers)
            if active and active in all_names:
                others = [n for n in all_names if n != active]
                ordered_names = [active] + others
                active_set = True
            else:
                ordered_names = all_names
                active_set = False
            head = ordered_names[:3]
            extra = len(ordered_names) - len(head)
            if active_set and head and head[0] == active:
                # "first on source" is deliberately understated — the
                # heuristic reads the source's models.json and picks
                # ``agents.defaults.model.primary`` if set, otherwise the
                # first entry in the providers dict. Both are "the one
                # most likely in use" but neither is a guarantee, so the
                # label doesn't overclaim.
                head_cells = [f"{head[0]} [dim](first on source)[/dim]"] + head[1:]
            else:
                head_cells = list(head)
            names_cell = ", ".join(head_cells)
            if extra > 0:
                names_cell += f" (+{extra} more — see `clawcu provider list`)"
            console.print(
                f"[dim]  collected providers for {record.service}: {names_cell}. "
                f"Apply one with `clawcu provider apply <name> {record.name}`.[/dim]"
            )
    _print_access_url(service, record.name)


@app.command(
    "logs",
    help="Stream or print Docker logs for a managed instance.",
    rich_help_panel=_PANEL_DIAG,
)
def logs_instance(
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
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


@app.command(
    "remove",
    help="Remove a managed instance, or pass --removed to permanently delete an orphaned leftover from `list --removed`. Alias: `rm`.",
    rich_help_panel=_PANEL_LIFECYCLE,
)
def remove_instance(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Managed instance name.")],
    removed: Annotated[
        bool,
        typer.Option(
            "--removed",
            help="Treat NAME as an orphaned leftover from `clawcu list --removed` and permanently delete its datadir.",
        ),
    ] = False,
    delete_data: Annotated[
        bool,
        typer.Option(
            "--delete-data/--keep-data",
            help="Delete or preserve the instance data directory. Cannot be combined with --removed, which always deletes.",
        ),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    if removed:
        source = ctx.get_parameter_source("delete_data")
        if source is not None and source.name == "COMMANDLINE":
            _exit_with_error(
                "--removed already implies permanent deletion; drop --delete-data/--keep-data."
            )
        summary = (
            f"About to permanently delete removed instance '{name}' and its leftover data directory."
        )
    else:
        summary = (
            f"About to remove instance '{name}' and delete its data directory."
            if delete_data
            else f"About to remove instance '{name}' (data directory will be kept)."
        )
    _confirm_destructive(summary, yes)
    try:
        service = get_service()
        if removed:
            service.remove_removed_instance(name)
        else:
            service.remove_instance(name, delete_data=delete_data)
    except Exception as exc:
        _exit_with_error(str(exc))
    if removed:
        console.print(f"[green]Removed orphaned instance data:[/green] {name}")
    else:
        action = "and data directory" if delete_data else "but kept data directory"
        console.print(f"[green]Removed instance:[/green] {name} {action}")


def _register_command_aliases() -> None:
    """Register docker-style short aliases for the most common verbs.

    Aliases are marked ``hidden=True`` so ``--help`` stays terse, but
    they show up in tab-completion and work identically to the full
    form. Users coming from ``docker ps`` / ``git rm`` / ``kubectl get``
    have the muscle memory — this removes the "No such command 'rm'"
    wall without clutter.
    """
    aliases: list[tuple[str, str, str]] = [
        ("rm", "remove", "Alias for `remove`."),
        ("ls", "list", "Alias for `list`."),
    ]
    # Typer's CommandInfo list is populated by the @app.command() calls
    # above. We copy the CommandInfo for each target and re-register it
    # under the alias name with hidden=True.
    by_name = {cmd.name: cmd for cmd in app.registered_commands if cmd.name}
    for alias, target, help_text in aliases:
        source = by_name.get(target)
        if source is None:
            continue
        app.command(
            alias,
            help=help_text,
            hidden=True,
            rich_help_panel=source.rich_help_panel,
        )(source.callback)


_register_command_aliases()


def main() -> None:
    app()
