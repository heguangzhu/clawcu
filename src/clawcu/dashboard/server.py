from __future__ import annotations

import json
import socket
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from clawcu.core.service import ClawCUService

from .actions import (
    action_clone_for_upgrade,
    action_open_cli,
    action_open_config,
    action_open_tui,
    action_rollback,
    action_setup_check,
)
from .data import collect_dashboard, instance_inspect, instance_token, instance_versions


def _static_bytes(name: str) -> bytes:
    return files("clawcu.dashboard").joinpath("static", name).read_bytes()


def _dashboard_page_name(lang: str) -> str:
    return "clawcu-dashboard-design.en.html" if lang.startswith("en") else "clawcu-dashboard-design.html"


def _workspace_page_name(lang: str) -> str:
    return "clawcu-instance-workspace.en.html" if lang.startswith("en") else "clawcu-instance-workspace.html"


def _dashboard_is_healthy(url: str) -> bool:
    try:
        with urlopen(url, timeout=3.0) as response:
            server_header = str(response.headers.get("Server") or "")
            return response.status == HTTPStatus.OK and "ClawCUDashboard" in server_header
    except (OSError, URLError):
        return False


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def _find_fallback_port(host: str, start_port: int, *, attempts: int = 20) -> int | None:
    for port in range(start_port + 1, start_port + attempts + 1):
        if _port_is_available(host, port):
            return port
    return None


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ClawCUDashboard/0.1"

    def __init__(self, *args: Any, service: ClawCUService, **kwargs: Any) -> None:
        self.service = service
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            lang = (query.get("lang") or ["zh-CN"])[0].strip().lower()
            if parsed.path == "/":
                self._serve_bytes(_static_bytes(_dashboard_page_name(lang)), "text/html; charset=utf-8")
                return
            if parsed.path == "/workspace":
                self._serve_bytes(_static_bytes(_workspace_page_name(lang)), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/dashboard":
                self._json_response(collect_dashboard(self.service))
                return
            if parsed.path == "/api/inspect":
                name = self._required_query(query, "name")
                self._json_response(instance_inspect(self.service, name))
                return
            if parsed.path == "/api/logs":
                name = self._required_query(query, "name")
                tail = int(query.get("tail", ["120"])[0])
                record = self.service.store.load_record(name)
                result = self.service.runner(
                    ["docker", "logs", "--tail", str(tail), record.container_name],
                    timeout_seconds=self.service.docker.LOGS_TIMEOUT_SECONDS,
                )
                self._json_response({"name": name, "ok": True, "output": (getattr(result, "stdout", "") or "").strip()})
                return
            if parsed.path == "/api/versions":
                name = self._required_query(query, "name")
                self._json_response(instance_versions(self.service, name))
                return
            if parsed.path == "/api/token":
                name = self._required_query(query, "name")
                self._json_response(instance_token(self.service, name))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
        except ValueError as exc:
            # `ValueError` in this module is exclusively for client-side
            # input problems (missing query param, unparseable `tail`,
            # …). Return 400 so browsers / scripts can tell "you asked
            # wrong" apart from a real 500.
            self._json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            self._json_response({"ok": False, "error": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            if parsed.path != "/api/action":
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            action = str(payload.get("action") or "")
            instance = str(payload.get("instance") or "")
            if action == "setup_check":
                result = action_setup_check(self.service)
            elif action == "open_cli":
                result = action_open_cli(self.service)
            elif action == "config":
                result = action_open_config(self.service, instance)
            elif action == "tui":
                result = action_open_tui(self.service, instance)
            elif action == "clone_for_upgrade":
                clone_name = str(payload.get("clone_name") or "").strip()
                if not clone_name:
                    raise ValueError("clone_name is required for clone_for_upgrade")
                target_version = str(payload.get("target_version") or "").strip() or None
                result = action_clone_for_upgrade(self.service, instance, clone_name, target_version)
            elif action == "rollback":
                result = action_rollback(self.service, instance)
            else:
                raise ValueError(f"Unsupported action `{action}`")
            self._json_response(result)
        except ValueError as exc:
            self._json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            self._json_response({"ok": False, "error": str(exc)}, status=500)

    def _serve_bytes(self, content: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json_response(self, payload: Any, *, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _required_query(self, query: dict[str, list[str]], key: str) -> str:
        value = (query.get(key) or [""])[0].strip()
        if not value:
            raise ValueError(f"Missing `{key}` query parameter")
        return value


def serve_dashboard(*, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    url = f"http://{host}:{port}"
    if _dashboard_is_healthy(url):
        print(f"ClawCU dashboard is already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return

    service = ClawCUService()
    handler = partial(DashboardHandler, service=service)
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        if exc.errno == 48 and port == 8765:
            fallback_port = _find_fallback_port(host, port)
            if fallback_port is not None:
                fallback_url = f"http://{host}:{fallback_port}"
                print(f"Port {port} is already in use; starting dashboard on {fallback_url} instead.")
                server = ThreadingHTTPServer((host, fallback_port), handler)
                url = fallback_url
            else:
                raise RuntimeError(
                    f"Port {port} is already in use and no fallback port was found nearby."
                ) from exc
        elif exc.errno == 48 and _dashboard_is_healthy(url):
            print(f"ClawCU dashboard is already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return
        else:
            raise RuntimeError(f"Unable to start dashboard on {url}: {exc.strerror or exc}") from exc
    if open_browser:
        webbrowser.open(url)
    print(f"ClawCU dashboard listening on {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
