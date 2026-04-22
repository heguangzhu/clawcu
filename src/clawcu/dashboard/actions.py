from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

from clawcu.core.service import ClawCUService


def _apple_script_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _open_terminal(command: str) -> None:
    escaped = _apple_script_escape(command)
    script = (
        'tell application "Terminal"\n'
        "activate\n"
        f'do script "{escaped}"\n'
        "end tell\n"
    )
    subprocess.run(["osascript", "-e", script], check=True)


def action_setup_check(service: ClawCUService) -> dict[str, Any]:
    checks = service.check_setup()
    failed = [row for row in checks if not bool(row.get("ok"))]
    output = "\n".join(f"{row['name']}: {row['summary']}" for row in checks)
    return {
        "ok": not failed,
        "message": "Setup check finished.",
        "output": output,
    }


def action_open_cli(service: ClawCUService) -> dict[str, Any]:
    home = service.store.paths.home
    _open_terminal(f"cd {shlex.quote(str(home))}")
    return {"ok": True, "message": "Opened a local terminal in CLAWCU_HOME.", "output": f"cd {home}"}


def action_open_config(service: ClawCUService, name: str) -> dict[str, Any]:
    _open_terminal(f"cd {shlex.quote(str(Path.cwd()))} && clawcu config {shlex.quote(name)}")
    return {"ok": True, "message": f"Launched config for `{name}` in Terminal.", "output": f"clawcu config {name}"}


def action_open_tui(service: ClawCUService, name: str) -> dict[str, Any]:
    _open_terminal(f"cd {shlex.quote(str(Path.cwd()))} && clawcu tui {shlex.quote(name)}")
    return {"ok": True, "message": f"Launched TUI for `{name}` in Terminal.", "output": f"clawcu tui {name}"}


def action_clone_for_upgrade(service: ClawCUService, name: str, clone_name: str, target_version: str | None) -> dict[str, Any]:
    record = service.clone_instance(name, name=clone_name, version=target_version or None)
    return {"ok": True, "message": f"Clone flow finished for `{record.name}`.", "record": record.to_dict(), "output": ""}


def action_rollback(service: ClawCUService, name: str) -> dict[str, Any]:
    record = service.rollback_instance(name)
    return {"ok": True, "message": f"Rollback finished for `{record.name}`.", "record": record.to_dict(), "output": ""}
