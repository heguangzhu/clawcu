from __future__ import annotations

import json
import re
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import urlopen

import click
from typer.testing import CliRunner

import clawcu.cli as cli_module
from clawcu import __version__ as clawcu_version
from clawcu.cli import app
from clawcu.dashboard.data import collect_dashboard
from clawcu.dashboard.server import DashboardHandler

runner = CliRunner()


def _plain(text: str) -> str:
    return click.unstyle(re.sub(r"\s+", " ", text))


def _make_empty_dashboard_service() -> MagicMock:
    """Minimal stub service that satisfies `collect_dashboard` when no
    instances exist. Kept inline so the dashboard tests stay a single
    file and don't drag in the CLI FakeService fixture."""
    service = MagicMock()
    service.get_clawcu_home.return_value = "/tmp/clawcu"
    service.get_openclaw_image_repo.return_value = "ghcr.io/openclaw/openclaw"
    service.get_hermes_image_repo.return_value = "clawcu/hermes-agent"
    service.list_instance_summaries.return_value = []
    service.list_local_instance_summaries.return_value = []
    service.list_agent_summaries.return_value = []
    service.list_removed_instance_summaries.return_value = []
    service.list_providers.return_value = []
    service.list_service_available_versions.return_value = {
        "openclaw": {"versions": [], "error": None, "registry": None},
        "hermes": {"versions": [], "error": None, "registry": None},
    }
    return service


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


def test_collect_dashboard_env_reports_real_clawcu_version() -> None:
    """`env.clawcu_version` is the package version, not the literal
    string "clawcu". The dashboard UI surfaces this in its header; the
    previous hard-coded value made every install look the same.
    """
    payload = collect_dashboard(_make_empty_dashboard_service())

    assert payload["env"]["clawcu_version"] == clawcu_version
    assert payload["env"]["clawcu_version"] != "clawcu"


def test_api_versions_without_name_returns_400_not_500() -> None:
    """Client errors (missing query params, unparseable ints) come back
    as HTTP 400 so browsers/scripts can tell "bad input" apart from a
    real server fault. The error body still carries a hint.
    """
    service = _make_empty_dashboard_service()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(DashboardHandler, service=service)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        try:
            urlopen(f"http://127.0.0.1:{port}/api/versions", timeout=5)
        except HTTPError as exc:
            assert exc.code == 400, f"expected 400, got {exc.code}"
            body = json.loads(exc.read().decode("utf-8"))
            assert body["ok"] is False
            assert "Missing `name`" in body["error"]
        else:
            raise AssertionError("expected HTTP 400 response")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_api_post_unsupported_action_returns_400_not_500() -> None:
    """Same contract for POST: an unknown action is a client error."""
    import urllib.request

    service = _make_empty_dashboard_service()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(DashboardHandler, service=service)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/action",
            data=json.dumps({"action": "bogus", "instance": "x"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(req, timeout=5)
        except HTTPError as exc:
            assert exc.code == 400, f"expected 400, got {exc.code}"
            body = json.loads(exc.read().decode("utf-8"))
            assert "Unsupported action" in body["error"]
        else:
            raise AssertionError("expected HTTP 400 response")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_api_action_rejects_non_json_content_type_and_skips_handler() -> None:
    """Cross-origin pages can only send POST with Content-Types that qualify
    as "simple requests" (text/plain, form-urlencoded, multipart). We reject
    anything that isn't application/json so a malicious page can't fire
    rollback/clone_for_upgrade without a CORS preflight. Critical: the
    action handler must not have been invoked.
    """
    import urllib.request

    service = _make_empty_dashboard_service()
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(DashboardHandler, service=service)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/action",
            data=json.dumps({"action": "setup_check", "instance": ""}).encode("utf-8"),
            headers={"Content-Type": "text/plain", "Origin": "http://evil.example.com"},
            method="POST",
        )
        try:
            urlopen(req, timeout=5)
        except HTTPError as exc:
            assert exc.code == 415, f"expected 415, got {exc.code}"
            body = json.loads(exc.read().decode("utf-8"))
            assert "application/json" in body["error"]
        else:
            raise AssertionError("expected HTTP 415 response")
        service.check_setup.assert_not_called()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_api_action_accepts_application_json_with_charset() -> None:
    """The gate uses only the media type; `application/json; charset=utf-8`
    is the exact string fetch() sends from a same-origin page.
    """
    import urllib.request

    service = _make_empty_dashboard_service()
    service.check_setup.return_value = [{"name": "probe", "summary": "ok", "ok": True}]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(DashboardHandler, service=service)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/action",
            data=json.dumps({"action": "setup_check"}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(req, timeout=5) as response:
            assert response.status == 200
            body = json.loads(response.read().decode("utf-8"))
            assert body["ok"] is True
        service.check_setup.assert_called_once()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_api_inspect_nonexistent_instance_returns_404_not_500() -> None:
    """`store.load_record(name)` raises `FileNotFoundError` for a missing
    instance. That's a client-side "gone away" case (user clicked a stale
    row, or the instance was removed in another terminal) — return 404
    so the UI can tell it apart from a real server fault.
    """
    service = _make_empty_dashboard_service()
    service.store = MagicMock()
    service.store.load_record.side_effect = FileNotFoundError(
        "Instance 'ghost' was not found."
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(DashboardHandler, service=service)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        try:
            urlopen(f"http://127.0.0.1:{port}/api/inspect?name=ghost", timeout=5)
        except HTTPError as exc:
            assert exc.code == 404, f"expected 404, got {exc.code}"
            body = json.loads(exc.read().decode("utf-8"))
            assert body["ok"] is False
            assert "was not found" in body["error"]
        else:
            raise AssertionError("expected HTTP 404 response")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
