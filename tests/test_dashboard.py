from __future__ import annotations

import json
import re
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from importlib.resources import files
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.request import urlopen

import click
import pytest
from typer.testing import CliRunner

import clawcu.cli as cli_module
from clawcu import __version__ as clawcu_version
from clawcu.cli import app
from clawcu.dashboard.data import collect_dashboard
from clawcu.dashboard.server import DashboardHandler, _dashboard_is_healthy

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


def test_dashboard_help_mentions_port_and_container_options() -> None:
    result = runner.invoke(app, ["dashboard", "--help"])
    output = _plain(result.stdout)

    assert result.exit_code == 0
    assert "--host" in output
    assert "--port" in output
    assert "--open" in output
    assert "--stop" in output
    assert "--restart" in output
    assert "--status" in output
    assert "--rebuild" in output


def test_dashboard_status_shows_container_info(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "_dashboard_container_info",
        lambda: {
            "State": {"Status": "running", "StartedAt": "2024-01-01T00:00:00Z"},
            "Config": {"Image": "clawcu-dashboard:0.5.1"},
        },
    )
    monkeypatch.setattr(cli_module, "_dashboard_is_healthy", lambda _: True)

    result = runner.invoke(app, ["dashboard", "--status"])

    assert result.exit_code == 0
    assert "running" in result.output
    assert "clawcu-dashboard:0.5.1" in result.output
    assert "healthy" in result.output


def test_dashboard_status_no_container(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "_dashboard_container_info", lambda: None)

    result = runner.invoke(app, ["dashboard", "--status"])

    assert result.exit_code == 0
    assert "not running" in result.output


def test_dashboard_stop_removes_container(monkeypatch) -> None:
    stops: list[list[str]] = []
    monkeypatch.setattr(
        cli_module.subprocess,
        "run",
        lambda cmd, **_: stops.append(cmd) or MagicMock(),
    )

    result = runner.invoke(app, ["dashboard", "--stop"])

    assert result.exit_code == 0
    assert any("stop" in c for c in stops)
    assert any("rm" in c for c in stops)


def test_dashboard_starts_container_when_healthy(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    health_checks = iter([False, True])

    monkeypatch.setattr(cli_module, "_docker_image_exists", lambda _: True)
    monkeypatch.setattr(cli_module, "_dashboard_container_info", lambda: None)
    monkeypatch.setattr(cli_module, "_dashboard_is_healthy", lambda _: next(health_checks))
    monkeypatch.setattr(
        cli_module,
        "_start_dashboard_container",
        lambda *args, **kwargs: calls.append(("start", args, kwargs)),
    )
    monkeypatch.setattr(cli_module.webbrowser, "open", lambda url: calls.append(("open", url)))
    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    result = runner.invoke(app, ["dashboard", "--no-open"])

    assert result.exit_code == 0
    assert any(c[0] == "start" for c in calls)
    assert "running at http://127.0.0.1:8765" in result.output


def test_dashboard_rebuild_triggers_build(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(cli_module, "_dashboard_container_info", lambda: None)
    monkeypatch.setattr(cli_module, "_dashboard_is_healthy", lambda _: True)
    monkeypatch.setattr(
        cli_module,
        "_build_dashboard_image",
        lambda tag, root: calls.append(("build", tag, root)),
    )
    monkeypatch.setattr(
        cli_module,
        "_start_dashboard_container",
        lambda *args, **kwargs: calls.append(("start", args, kwargs)),
    )
    monkeypatch.setattr(cli_module.webbrowser, "open", lambda _: None)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)

    result = runner.invoke(app, ["dashboard", "--rebuild", "--no-open"])

    assert result.exit_code == 0
    assert any(c[0] == "build" for c in calls)


def test_dashboard_prints_concise_error_without_traceback(monkeypatch) -> None:
    def fail_build(image_tag: str, project_root: Path) -> None:
        raise RuntimeError("Docker daemon is not running.")

    monkeypatch.setattr(cli_module, "_docker_image_exists", lambda _: False)
    monkeypatch.setattr(cli_module, "_build_dashboard_image", fail_build)

    result = runner.invoke(app, ["dashboard"])

    assert result.exit_code == 1
    assert "Docker daemon is not running." in result.output
    assert "Traceback" not in result.output


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


@pytest.mark.parametrize(
    "page",
    [
        "clawcu-dashboard-design.html",
        "clawcu-dashboard-design.en.html",
        "clawcu-instance-workspace.html",
        "clawcu-instance-workspace.en.html",
    ],
)
def test_static_pages_escape_log_lines_to_prevent_xss(page: str) -> None:
    """Container stdout goes straight into the logs view. If we render it
    raw, a container that writes `<img src=x onerror=fetch('/api/action',...)>`
    gets same-origin code execution — bypassing review-14's CSRF gate and
    reaching the destructive `/api/action` handlers. Every page that shows
    container logs must go through an `escapeHtml` helper on the way in.
    """
    content = files("clawcu.dashboard").joinpath("static", page).read_text(encoding="utf-8")

    assert "function escapeHtml(" in content, (
        f"{page} must define an escapeHtml() helper"
    )
    assert 'log-line">${escapeHtml(' in content, (
        f"{page} must route log lines through escapeHtml() before innerHTML insertion"
    )
    assert 'log-line">${line}' not in content, (
        f"{page} still renders raw log lines into innerHTML — XSS regression"
    )
    assert 'log-line">${logLine}' not in content
    assert 'log-line">${logLines.join' not in content
