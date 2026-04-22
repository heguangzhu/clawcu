from __future__ import annotations

import re

import click
from typer.testing import CliRunner

import clawcu.cli as cli_module
from clawcu.cli import app

runner = CliRunner()


def _plain(text: str) -> str:
    return click.unstyle(re.sub(r"\s+", " ", text))


def test_dashboard_help_mentions_port_and_browser_options() -> None:
    result = runner.invoke(app, ["dashboard", "--help"])
    output = _plain(result.stdout)

    assert result.exit_code == 0
    assert "--host" in output
    assert "--port" in output
    assert "--open" in output
    assert "--foreground" in output


def test_dashboard_prints_concise_error_without_traceback(monkeypatch) -> None:
    def fail_dashboard(**_: object) -> None:
        raise RuntimeError("Port 8765 is already in use.")

    monkeypatch.setattr(cli_module, "serve_dashboard", fail_dashboard)

    result = runner.invoke(app, ["dashboard", "--foreground"])

    assert result.exit_code == 1
    assert "Port 8765 is already in use." in result.output
    assert "Traceback" not in result.output


def test_dashboard_background_starts_and_opens_browser(monkeypatch) -> None:
    calls: list[object] = []
    health_checks = iter([False, True])

    class DummyPopen:
        def __init__(self, cmd, **kwargs):
            calls.append(("popen", cmd, kwargs))

    monkeypatch.setattr(cli_module, "_dashboard_is_healthy", lambda _: next(health_checks))
    monkeypatch.setattr(cli_module.subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(cli_module.webbrowser, "open", lambda url: calls.append(("open", url)))
    monkeypatch.setattr(cli_module.shutil, "which", lambda _: "/tmp/clawcu")
    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    result = runner.invoke(app, ["dashboard"])

    assert result.exit_code == 0
    assert "ClawCU dashboard is running at http://127.0.0.1:8765" in result.output
    assert calls[0][0] == "popen"
    assert calls[0][1] == [
        "/tmp/clawcu",
        "dashboard",
        "--host",
        "127.0.0.1",
        "--port",
        "8765",
        "--foreground",
        "--no-open",
    ]
    assert calls[1] == ("open", "http://127.0.0.1:8765")


def test_dashboard_foreground_uses_current_terminal(monkeypatch) -> None:
    recorded: list[tuple[str, object]] = []

    monkeypatch.setattr(
        cli_module,
        "serve_dashboard",
        lambda **kwargs: recorded.append(("serve", kwargs)),
    )

    result = runner.invoke(app, ["dashboard", "--foreground", "--no-open"])

    assert result.exit_code == 0
    assert recorded == [
        (
            "serve",
            {
                "host": "127.0.0.1",
                "port": 8765,
                "open_browser": False,
            },
        )
    ]
