from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pytest
from typer.testing import CliRunner

from clawcu.a2a.bridge import build_bridge_server, echo_reply, run_bridge_in_thread
from clawcu.a2a.card import (
    AgentCard,
    bridge_endpoint_for,
    bridge_port_for,
    card_from_record,
    skills_for_service,
)
from clawcu.a2a.client import (
    A2AClientError,
    list_agents,
    lookup_agent,
    post_message,
    send_via_registry,
)
from clawcu.a2a.detect import detect_plugin_or_none
from clawcu.a2a.registry import (
    build_registry_server,
    cards_from_service,
    make_cards_provider,
    run_registry_in_thread,
    try_fetch_plugin_card,
)
from clawcu.cli import app

runner = CliRunner()


@dataclass
class FakeRecord:
    name: str
    service: str
    port: int


def _http_json(url: str, *, method: str = "GET", body=None, timeout: float = 2.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def _wait_ready(host: str, port: int, path: str = "/", attempts: int = 50) -> None:
    url = f"http://{host}:{port}{path}"
    for _ in range(attempts):
        try:
            urllib.request.urlopen(url, timeout=0.2)
            return
        except urllib.error.HTTPError:
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.02)
    raise RuntimeError(f"server at {url} never came up")


# ---------- AgentCard ----------


def test_agent_card_round_trip():
    card = AgentCard(
        name="writer",
        role="long-form writing",
        skills=["draft.polish", "outline"],
        endpoint="http://127.0.0.1:19799/a2a/send",
    )
    payload = card.to_json()
    restored = AgentCard.from_json(payload)
    assert restored == card
    assert restored.to_dict() == {
        "name": "writer",
        "role": "long-form writing",
        "skills": ["draft.polish", "outline"],
        "endpoint": "http://127.0.0.1:19799/a2a/send",
    }


def test_agent_card_rejects_missing_fields():
    with pytest.raises(ValueError):
        AgentCard.from_dict({"name": "a", "role": "b", "skills": []})
    with pytest.raises(ValueError):
        AgentCard.from_dict(
            {"name": "", "role": "b", "skills": [], "endpoint": "http://x"}
        )


def test_skills_for_service_has_placeholder_map():
    assert skills_for_service("openclaw") == ["chat", "tools"]
    assert skills_for_service("hermes") == ["chat", "analysis"]
    assert skills_for_service("unknown") == ["chat"]


def test_card_from_record_uses_display_port_fallback_map():
    record = FakeRecord(name="alpha", service="openclaw", port=18799)
    card = card_from_record(record)
    assert card.name == "alpha"
    assert card.skills == ["chat", "tools"]
    # Without a service handle, card_from_record falls back to the
    # service-type default map (openclaw → 18819), not record.port and not
    # the deprecated bridge port (record.port + 1000).
    assert card.endpoint == "http://127.0.0.1:18819/a2a/send"


def test_card_from_record_prefers_adapter_display_port():
    class FakeAdapter:
        def display_port(self, service, record):  # noqa: ARG002
            return 27000

    class FakeService:
        def adapter_for_record(self, record):  # noqa: ARG002
            return FakeAdapter()

    record = FakeRecord(name="alpha", service="openclaw", port=18799)
    card = card_from_record(record, service=FakeService())
    assert card.endpoint == "http://127.0.0.1:27000/a2a/send"


def test_bridge_endpoint_helper_unchanged():
    # bridge_endpoint_for is the old record.port+1000 rule, retained for
    # bridge serve's internal use. display_port is the registry/card rule.
    record = FakeRecord(name="alpha", service="openclaw", port=18799)
    assert bridge_endpoint_for(record).endswith(f":{bridge_port_for(record)}/a2a/send")


# ---------- Registry ----------


def _registry_provider(cards: Iterable[AgentCard]):
    snapshot = list(cards)

    def provide():
        return list(snapshot)

    return provide


def test_registry_serves_list_and_single():
    cards = [
        AgentCard(
            name="alpha",
            role="r1",
            skills=["chat"],
            endpoint="http://127.0.0.1:19799/a2a/send",
        ),
        AgentCard(
            name="beta",
            role="r2",
            skills=["chat", "analysis"],
            endpoint="http://127.0.0.1:9652/a2a/send",
        ),
    ]
    server, thread = run_registry_in_thread(_registry_provider(cards))
    try:
        host, port = server.server_address
        status, body = _http_json(f"http://{host}:{port}/agents")
        assert status == 200
        assert sorted(c["name"] for c in body) == ["alpha", "beta"]

        status, body = _http_json(f"http://{host}:{port}/agents/alpha")
        assert status == 200
        assert body["endpoint"] == "http://127.0.0.1:19799/a2a/send"

        status, body = _http_json(f"http://{host}:{port}/agents/missing")
        assert status == 404
        assert body["error"] == "not_found"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_registry_client_helpers():
    cards = [
        AgentCard(
            name="alpha",
            role="r1",
            skills=["chat"],
            endpoint="http://127.0.0.1:19799/a2a/send",
        ),
    ]
    server, thread = run_registry_in_thread(_registry_provider(cards))
    try:
        host, port = server.server_address
        registry_url = f"http://{host}:{port}"
        listed = list_agents(registry_url)
        assert [c.name for c in listed] == ["alpha"]
        got = lookup_agent(registry_url, "alpha")
        assert got == cards[0]
        with pytest.raises(A2AClientError):
            lookup_agent(registry_url, "missing")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------- Bridge ----------


def test_bridge_serves_card_and_echoes():
    card = AgentCard(
        name="alpha",
        role="r1",
        skills=["chat"],
        endpoint="http://127.0.0.1:0/a2a/send",
    )
    server, thread = run_bridge_in_thread(card)
    try:
        host, port = server.server_address
        status, body = _http_json(f"http://{host}:{port}/.well-known/agent-card.json")
        assert status == 200
        assert body["name"] == "alpha"

        status, body = _http_json(
            f"http://{host}:{port}/a2a/send",
            method="POST",
            body={"from": "beta", "to": "alpha", "message": "hi"},
        )
        assert status == 200
        assert body == {"from": "alpha", "reply": echo_reply("alpha", "hi")}

        status, body = _http_json(
            f"http://{host}:{port}/a2a/send",
            method="POST",
            body={"from": "beta", "to": "alpha", "message": 123},
        )
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------- End-to-end ----------


def test_end_to_end_send_through_registry_and_bridge():
    bridge_card = AgentCard(
        name="target",
        role="r",
        skills=["chat"],
        endpoint="http://127.0.0.1:0/a2a/send",
    )
    bridge_server, bridge_thread = run_bridge_in_thread(bridge_card)
    try:
        b_host, b_port = bridge_server.server_address
        registered_card = AgentCard(
            name="target",
            role="r",
            skills=["chat"],
            endpoint=f"http://{b_host}:{b_port}/a2a/send",
        )
        reg_server, reg_thread = run_registry_in_thread(
            _registry_provider([registered_card])
        )
        try:
            r_host, r_port = reg_server.server_address
            reply = send_via_registry(
                registry_url=f"http://{r_host}:{r_port}",
                sender="caller",
                target="target",
                message="ping",
            )
            assert reply == {"from": "target", "reply": echo_reply("target", "ping")}

            direct = post_message(
                registered_card.endpoint,
                sender="caller",
                target="target",
                message="pong",
            )
            assert direct == {"from": "target", "reply": echo_reply("target", "pong")}
        finally:
            reg_server.shutdown()
            reg_server.server_close()
            reg_thread.join(timeout=2)
    finally:
        bridge_server.shutdown()
        bridge_server.server_close()
        bridge_thread.join(timeout=2)


# ---------- CLI wiring ----------


def test_cli_a2a_card_with_no_instances(monkeypatch, temp_clawcu_home):
    class FakeService:
        def list_instances(self, *, running_only=False):
            return []

    monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: FakeService())
    result = runner.invoke(app, ["a2a", "card"])
    assert result.exit_code == 0, result.output
    assert "[]" in result.output


def test_cli_a2a_card_named(monkeypatch, temp_clawcu_home):
    record = FakeRecord(name="writer", service="openclaw", port=18799)

    class FakeService:
        def list_instances(self, *, running_only=False):
            return [record]

    monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: FakeService())
    result = runner.invoke(app, ["a2a", "card", "--name", "writer"])
    assert result.exit_code == 0, result.output
    assert "writer" in result.output
    assert "chat" in result.output

    missing = runner.invoke(app, ["a2a", "card", "--name", "nope"])
    assert missing.exit_code == 1


def test_cli_a2a_send_against_real_servers(monkeypatch, temp_clawcu_home):
    card = AgentCard(
        name="target",
        role="r",
        skills=["chat"],
        endpoint="http://127.0.0.1:0/a2a/send",
    )
    bridge_server, bridge_thread = run_bridge_in_thread(card)
    try:
        b_host, b_port = bridge_server.server_address
        registered = AgentCard(
            name="target",
            role="r",
            skills=["chat"],
            endpoint=f"http://{b_host}:{b_port}/a2a/send",
        )
        reg_server, reg_thread = run_registry_in_thread(
            _registry_provider([registered])
        )
        try:
            r_host, r_port = reg_server.server_address
            result = runner.invoke(
                app,
                [
                    "a2a",
                    "send",
                    "--to",
                    "target",
                    "--message",
                    "hi",
                    "--registry",
                    f"http://{r_host}:{r_port}",
                    "--from",
                    "cli",
                ],
            )
            assert result.exit_code == 0, result.output
            assert "target" in result.output
            assert "got: hi" in result.output
        finally:
            reg_server.shutdown()
            reg_server.server_close()
            reg_thread.join(timeout=2)
    finally:
        bridge_server.shutdown()
        bridge_server.server_close()
        bridge_thread.join(timeout=2)


def test_cli_a2a_send_unknown_target_fails():
    # Use an unreachable port: registry connection refused → CLI exits non-zero.
    result = runner.invoke(
        app,
        [
            "a2a",
            "send",
            "--to",
            "ghost",
            "--message",
            "hi",
            "--registry",
            "http://127.0.0.1:1",
        ],
    )
    assert result.exit_code == 1


# ---------- D5: plugin federation ----------


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _start_well_known_server(
    *,
    status: int = 200,
    body: bytes | None = None,
    delay: float = 0.0,
    content_type: str = "application/json",
):
    """Tiny http.server that replies to GET /.well-known/agent-card.json."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            if delay:
                time.sleep(delay)
            if self.path != "/.well-known/agent-card.json":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = body if body is not None else b"{}"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


class _FixedPortAdapter:
    def __init__(self, port: int) -> None:
        self._port = port

    def display_port(self, service, record):  # noqa: ARG002
        return self._port


class _FakeService:
    def __init__(self, records, *, port_map: dict[str, int] | None = None):
        self._records = list(records)
        self._port_map = dict(port_map or {})

    def list_instances(self, *, running_only: bool = False):  # noqa: ARG002
        return list(self._records)

    def adapter_for_record(self, record):
        port = self._port_map.get(record.name, getattr(record, "port", 0))
        return _FixedPortAdapter(port)


def test_try_fetch_plugin_card_success():
    card_payload = {
        "name": "alpha",
        "role": "real plugin role",
        "skills": ["s1", "s2"],
        "endpoint": "http://127.0.0.1:18819/a2a/send",
    }
    server, thread = _start_well_known_server(body=json.dumps(card_payload).encode())
    try:
        _, port = server.server_address
        record = FakeRecord(name="alpha", service="openclaw", port=0)
        service = _FakeService([record], port_map={"alpha": port})
        got = try_fetch_plugin_card(record, service=service, timeout=1.0)
        assert got is not None
        assert got.role == "real plugin role"
        assert got.skills == ["s1", "s2"]
    finally:
        _stop(server, thread)


def test_try_fetch_plugin_card_timeout_returns_none():
    server, thread = _start_well_known_server(delay=0.3, body=b"{}")
    try:
        _, port = server.server_address
        record = FakeRecord(name="alpha", service="openclaw", port=0)
        service = _FakeService([record], port_map={"alpha": port})
        got = try_fetch_plugin_card(record, service=service, timeout=0.05)
        assert got is None
    finally:
        _stop(server, thread)


def test_try_fetch_plugin_card_404_returns_none():
    server, thread = _start_well_known_server(status=404, body=b"nope")
    try:
        _, port = server.server_address
        record = FakeRecord(name="alpha", service="openclaw", port=0)
        service = _FakeService([record], port_map={"alpha": port})
        got = try_fetch_plugin_card(record, service=service, timeout=1.0)
        assert got is None
    finally:
        _stop(server, thread)


def test_try_fetch_plugin_card_bad_schema_returns_none():
    server, thread = _start_well_known_server(body=b'{"name": "only-name"}')
    try:
        _, port = server.server_address
        record = FakeRecord(name="alpha", service="openclaw", port=0)
        service = _FakeService([record], port_map={"alpha": port})
        assert try_fetch_plugin_card(record, service=service, timeout=1.0) is None
    finally:
        _stop(server, thread)


def test_try_fetch_plugin_card_malformed_json_returns_none():
    server, thread = _start_well_known_server(body=b"not-json-at-all")
    try:
        _, port = server.server_address
        record = FakeRecord(name="alpha", service="openclaw", port=0)
        service = _FakeService([record], port_map={"alpha": port})
        assert try_fetch_plugin_card(record, service=service, timeout=1.0) is None
    finally:
        _stop(server, thread)


def test_try_fetch_plugin_card_unreachable_returns_none():
    # Port 1 is reserved/refused on every host. Exercises the URLError path.
    record = FakeRecord(name="ghost", service="openclaw", port=0)
    service = _FakeService([record], port_map={"ghost": 1})
    assert try_fetch_plugin_card(record, service=service, timeout=0.2) is None


def test_cards_from_service_mixes_plugin_and_fallback():
    plugin_card = {
        "name": "alpha",
        "role": "real plugin role",
        "skills": ["s1"],
        "endpoint": "http://127.0.0.1:18819/a2a/send",
    }
    server, thread = _start_well_known_server(body=json.dumps(plugin_card).encode())
    try:
        _, plugin_port = server.server_address
        alpha = FakeRecord(name="alpha", service="openclaw", port=0)
        beta = FakeRecord(name="beta", service="hermes", port=0)
        service = _FakeService(
            [alpha, beta],
            port_map={"alpha": plugin_port, "beta": 1},  # beta unreachable
        )
        cards = cards_from_service(service, timeout=1.0)
        by_name = {c.name: c for c in cards}
        assert by_name["alpha"].role == "real plugin role"
        assert by_name["alpha"].skills == ["s1"]
        # beta had no plugin → fallback role/skills from service-type map
        assert by_name["beta"].role == "Hermes local analyst"
        assert by_name["beta"].skills == ["chat", "analysis"]
    finally:
        _stop(server, thread)


def test_make_cards_provider_caches_within_ttl():
    alpha = FakeRecord(name="alpha", service="openclaw", port=0)
    call_count = {"n": 0}

    class CountingService:
        def list_instances(self, *, running_only: bool = False):  # noqa: ARG002
            call_count["n"] += 1
            return [alpha]

        def adapter_for_record(self, record):  # noqa: ARG002
            return _FixedPortAdapter(1)  # unreachable → fallback path

    now_state = {"t": 0.0}

    def fake_now():
        return now_state["t"]

    provider = make_cards_provider(
        CountingService(), ttl=5.0, now=fake_now, timeout=0.05
    )
    first = provider()
    second = provider()  # within TTL → no refetch
    assert first == second
    assert call_count["n"] == 1

    now_state["t"] = 10.0  # past TTL
    third = provider()
    assert third == first
    assert call_count["n"] == 2


def test_make_cards_provider_ttl_zero_disables_cache():
    alpha = FakeRecord(name="alpha", service="openclaw", port=0)
    call_count = {"n": 0}

    class CountingService:
        def list_instances(self, *, running_only: bool = False):  # noqa: ARG002
            call_count["n"] += 1
            return [alpha]

        def adapter_for_record(self, record):  # noqa: ARG002
            return _FixedPortAdapter(1)

    provider = make_cards_provider(CountingService(), ttl=0.0, timeout=0.05)
    provider()
    provider()
    assert call_count["n"] == 2


def test_cache_state_does_not_leak_between_providers():
    alpha = FakeRecord(name="alpha", service="openclaw", port=0)

    class Svc:
        def __init__(self, label):
            self.label = label

        def list_instances(self, *, running_only: bool = False):  # noqa: ARG002
            return [FakeRecord(name=self.label, service="openclaw", port=0)]

        def adapter_for_record(self, record):  # noqa: ARG002
            return _FixedPortAdapter(1)

    p1 = make_cards_provider(Svc("one"), ttl=5.0, timeout=0.05)
    p2 = make_cards_provider(Svc("two"), ttl=5.0, timeout=0.05)
    assert p1()[0].name == "one"
    assert p2()[0].name == "two"


# ---------- D8: bridge UX ----------


def test_bridge_serve_virtual_instance_with_full_overrides(monkeypatch, temp_clawcu_home):
    class EmptyService:
        def list_instances(self, *, running_only=False):  # noqa: ARG002
            return []

        def adapter_for_record(self, record):  # noqa: ARG002
            raise AssertionError("virtual bridge should not touch adapters")

    monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: EmptyService())

    captured: dict[str, Any] = {}

    def fake_serve(card, *, host, port, reply_fn):
        captured["card"] = card
        captured["host"] = host
        captured["port"] = port
        raise KeyboardInterrupt  # exit the serve loop cleanly

    monkeypatch.setattr("clawcu.a2a.cli.serve_bridge_forever", fake_serve)

    result = runner.invoke(
        app,
        [
            "a2a",
            "bridge",
            "serve",
            "--instance",
            "virtual",
            "--role",
            "demo",
            "--skills",
            "chat,analysis",
            "--endpoint",
            "http://example.test/a2a/send",
            "--port",
            "12345",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["port"] == 12345
    card = captured["card"]
    assert card.name == "virtual"
    assert card.role == "demo"
    assert card.skills == ["chat", "analysis"]
    assert card.endpoint == "http://example.test/a2a/send"


def test_bridge_serve_unknown_instance_without_overrides_fails(monkeypatch, temp_clawcu_home):
    class EmptyService:
        def list_instances(self, *, running_only=False):  # noqa: ARG002
            return []

    monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: EmptyService())
    result = runner.invoke(app, ["a2a", "bridge", "serve", "--instance", "ghost"])
    assert result.exit_code == 1
    assert "--role" in result.output
    assert "--skills" in result.output
    assert "--endpoint" in result.output


def test_bridge_serve_default_port_uses_display_port(monkeypatch, temp_clawcu_home):
    record = FakeRecord(name="writer", service="openclaw", port=0)

    class FakeService:
        def list_instances(self, *, running_only=False):  # noqa: ARG002
            return [record]

        def adapter_for_record(self, r):  # noqa: ARG002
            return _FixedPortAdapter(18839)

    monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: FakeService())

    captured: dict[str, Any] = {}

    def fake_serve(card, *, host, port, reply_fn):
        captured["card"] = card
        captured["port"] = port
        raise KeyboardInterrupt

    monkeypatch.setattr("clawcu.a2a.cli.serve_bridge_forever", fake_serve)

    result = runner.invoke(app, ["a2a", "bridge", "serve", "--instance", "writer"])
    assert result.exit_code == 0, result.output
    assert captured["port"] == 18839
    assert captured["card"].endpoint == "http://127.0.0.1:18839/a2a/send"


def test_bridge_serve_overrides_on_managed_instance(monkeypatch, temp_clawcu_home):
    record = FakeRecord(name="writer", service="openclaw", port=0)

    class FakeService:
        def list_instances(self, *, running_only=False):  # noqa: ARG002
            return [record]

        def adapter_for_record(self, r):  # noqa: ARG002
            return _FixedPortAdapter(18839)

    monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: FakeService())

    captured: dict[str, Any] = {}

    def fake_serve(card, *, host, port, reply_fn):
        captured["card"] = card
        raise KeyboardInterrupt

    monkeypatch.setattr("clawcu.a2a.cli.serve_bridge_forever", fake_serve)

    result = runner.invoke(
        app,
        [
            "a2a",
            "bridge",
            "serve",
            "--instance",
            "writer",
            "--role",
            "custom role",
            "--skills",
            "one,two",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["card"].role == "custom role"
    assert captured["card"].skills == ["one", "two"]


# ---------- D9: clawcu a2a up ----------


def test_detect_plugin_or_none_success():
    card_payload = {
        "name": "alpha",
        "role": "plugin",
        "skills": ["s"],
        "endpoint": "http://127.0.0.1:18819/a2a/send",
    }
    server, thread = _start_well_known_server(body=json.dumps(card_payload).encode())
    try:
        _, port = server.server_address
        record = FakeRecord(name="alpha", service="openclaw", port=0)
        service = _FakeService([record], port_map={"alpha": port})
        sleeps: list[float] = []
        got = detect_plugin_or_none(
            record,
            service=service,
            timeout=1.0,
            attempts=3,
            retry_delay=0.01,
            sleep=sleeps.append,
        )
        assert got is not None
        assert got.role == "plugin"
        # first attempt succeeded → no sleeps
        assert sleeps == []
    finally:
        _stop(server, thread)


def test_detect_plugin_or_none_retries_then_gives_up():
    record = FakeRecord(name="ghost", service="openclaw", port=0)
    service = _FakeService([record], port_map={"ghost": 1})  # refused
    sleeps: list[float] = []
    got = detect_plugin_or_none(
        record,
        service=service,
        timeout=0.05,
        attempts=3,
        retry_delay=0.5,
        sleep=sleeps.append,
    )
    assert got is None
    # Three attempts, sleep between first two pairs → two sleeps total.
    assert sleeps == [0.5, 0.5]


def test_detect_plugin_or_none_single_attempt():
    record = FakeRecord(name="ghost", service="openclaw", port=0)
    service = _FakeService([record], port_map={"ghost": 1})
    sleeps: list[float] = []
    got = detect_plugin_or_none(
        record,
        service=service,
        timeout=0.05,
        attempts=1,
        retry_delay=99.0,
        sleep=sleeps.append,
    )
    assert got is None
    assert sleeps == []


def test_a2a_up_starts_echo_bridges_for_instances_without_plugin(monkeypatch, temp_clawcu_home):
    # One instance with a real plugin at a live server, one instance with
    # an unreachable probe port → expect one echo bridge started.
    plugin_payload = {
        "name": "writer",
        "role": "plugin role",
        "skills": ["chat"],
        "endpoint": "http://127.0.0.1:1/a2a/send",
    }
    plugin_server, plugin_thread = _start_well_known_server(
        body=json.dumps(plugin_payload).encode()
    )
    try:
        _, plugin_port = plugin_server.server_address

        # Pick a free port for the echo bridge by opening and closing a socket.
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            bridge_port = s.getsockname()[1]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            registry_port = s.getsockname()[1]

        writer = FakeRecord(name="writer", service="openclaw", port=0)
        ghost = FakeRecord(name="ghost", service="hermes", port=0)

        class Svc:
            def list_instances(self, *, running_only=False):  # noqa: ARG002
                return [writer, ghost]

            def adapter_for_record(self, record):
                return _FixedPortAdapter(
                    plugin_port if record.name == "writer" else bridge_port
                )

        monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: Svc())

        # Stop the registry promptly so the test doesn't hang. We hit
        # /agents once from a background thread, then send KeyboardInterrupt
        # via a monkeypatched serve_registry_forever.
        agents_capture: dict[str, Any] = {}

        def fake_registry_serve(provider, *, host, port, on_ready=None):
            # Call provider() exactly to prove the up command wired it.
            agents_capture["cards"] = list(provider())
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "clawcu.a2a.cli.serve_registry_forever", fake_registry_serve
        )

        result = runner.invoke(
            app,
            [
                "a2a",
                "up",
                "--registry-port",
                str(registry_port),
                "--probe-timeout",
                "0.5",
                "--probe-attempts",
                "1",
                "--probe-delay",
                "0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "OK" in result.output and "writer" in result.output
        assert "WARN" in result.output and "ghost" in result.output
        # provider returned cards for both instances, ghost card came from
        # the echo bridge we started.
        by_name = {c.name: c for c in agents_capture["cards"]}
        assert set(by_name) == {"writer", "ghost"}
        assert by_name["writer"].role == "plugin role"
    finally:
        _stop(plugin_server, plugin_thread)


# ---------- fix-up: neighbor-port federation + bind-probe ----------


def test_try_fetch_plugin_card_tries_neighbor_port_for_openclaw():
    # Only the neighbor port (display_port + 1) serves a card — matches the
    # real OpenClaw sidecar layout where the container itself occupies
    # display_port with its gateway UI.
    card_payload = {
        "name": "james.simons",
        "role": "openclaw sidecar",
        "skills": ["chat"],
        "endpoint": "http://127.0.0.1:18820/a2a/send",
    }
    server, thread = _start_well_known_server(body=json.dumps(card_payload).encode())
    try:
        _, sidecar_port = server.server_address
        display_port = sidecar_port - 1
        record = FakeRecord(name="james.simons", service="openclaw", port=0)
        service = _FakeService([record], port_map={"james.simons": display_port})
        got = try_fetch_plugin_card(record, service=service, timeout=1.0)
        assert got is not None
        assert got.role == "openclaw sidecar"
        # The fetched card is whatever the sidecar self-reports; the probe
        # did not rewrite its endpoint. Proves the neighbor port was hit.
        assert got.endpoint == "http://127.0.0.1:18820/a2a/send"
    finally:
        _stop(server, thread)


def test_try_fetch_plugin_card_prefers_display_port_over_neighbor():
    # Both ports respond with distinct cards; display_port must win because
    # the true plugin (gateway-in-process) always takes precedence over the
    # sidecar fallback.
    display_payload = {
        "name": "james.simons",
        "role": "gateway-in-process",
        "skills": ["chat"],
        "endpoint": "http://127.0.0.1:18819/a2a/send",
    }
    sidecar_payload = {
        "name": "james.simons",
        "role": "sidecar",
        "skills": ["chat"],
        "endpoint": "http://127.0.0.1:18820/a2a/send",
    }
    display_server, display_thread = _start_well_known_server(
        body=json.dumps(display_payload).encode()
    )
    sidecar_server = sidecar_thread = None
    try:
        _, display_port = display_server.server_address

        # Pin the sidecar to display_port + 1. If the OS refuses, skip.
        sidecar_port = display_port + 1

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_GET(self):  # noqa: N802
                if self.path != "/.well-known/agent-card.json":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                raw = json.dumps(sidecar_payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        try:
            sidecar_server = ThreadingHTTPServer(("127.0.0.1", sidecar_port), Handler)
        except OSError:
            pytest.skip(f"neighbor port :{sidecar_port} unavailable on this host")
        sidecar_thread = threading.Thread(
            target=sidecar_server.serve_forever, daemon=True
        )
        sidecar_thread.start()

        record = FakeRecord(name="james.simons", service="openclaw", port=0)
        service = _FakeService([record], port_map={"james.simons": display_port})
        got = try_fetch_plugin_card(record, service=service, timeout=1.0)
        assert got is not None
        assert got.role == "gateway-in-process"
    finally:
        _stop(display_server, display_thread)
        if sidecar_server is not None:
            _stop(sidecar_server, sidecar_thread)


def test_a2a_up_skips_echo_bridge_when_port_already_bound(
    monkeypatch, temp_clawcu_home, caplog
):
    import socket as _socket

    # Bind a socket on a random free IPv4 localhost port and hold it open
    # while `a2a up` runs. The echo bridge must notice and step aside.
    holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    holder.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(128)
    held_port = holder.getsockname()[1]
    try:
        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            registry_port = s.getsockname()[1]

        ghost = FakeRecord(name="ghost", service="openclaw", port=0)

        class Svc:
            def list_instances(self, *, running_only=False):  # noqa: ARG002
                return [ghost]

            def adapter_for_record(self, record):  # noqa: ARG002
                # display_port → held port; neighbor (held+1) won't respond
                # either, so detect_plugin_or_none returns None → up tries
                # to start an echo bridge on held_port.
                return _FixedPortAdapter(held_port)

        monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: Svc())

        bind_calls: list[int] = []
        original_build = build_bridge_server

        def spy_build(card, *, host, port, reply_fn):
            bind_calls.append(port)
            return original_build(card, host=host, port=port, reply_fn=reply_fn)

        monkeypatch.setattr("clawcu.a2a.cli.build_bridge_server", spy_build)

        def fake_registry_serve(provider, *, host, port, on_ready=None):  # noqa: ARG001
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "clawcu.a2a.cli.serve_registry_forever", fake_registry_serve
        )

        result = runner.invoke(
            app,
            [
                "a2a",
                "up",
                "--registry-port",
                str(registry_port),
                "--probe-timeout",
                "0.2",
                "--probe-attempts",
                "1",
                "--probe-delay",
                "0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "already in use" in result.output
        assert "ghost" in result.output
        # No bridge was built on the held port (or any port at all — only
        # one instance was under test).
        assert bind_calls == []
    finally:
        holder.close()


# ---------- iter 4: native-agent routing (P0-3 regression) ----------


def _make_openclaw_record(tmp_path, *, a2a_enabled: bool = True):
    """Build an InstanceRecord against a temp datadir for adapter tests."""
    from clawcu.models import InstanceRecord

    datadir = tmp_path / "inst"
    datadir.mkdir()
    return InstanceRecord(
        service="openclaw",
        name="writer",
        version="2026.4.12",
        datadir=str(datadir),
        port=18799,
        cpu="1",
        memory="2g",
        auth_mode="token",
        a2a_enabled=a2a_enabled,
        upstream_ref="ghcr.io/openclaw/openclaw:2026.4.12",
        image_tag="clawcu/openclaw-a2a:2026.4.12-plugin0.2.10",
        container_name="clawcu-openclaw-writer",
        status="created",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    )


class _StubService:
    """Minimal ClawCUService stand-in for the adapter config/run-spec path."""

    def __init__(self, env_file: Path | None = None):
        self.messages: list[str] = []
        self.reporter = self.messages.append

        class _Store:
            def __init__(self, env_file: Path | None):
                self._env_file = env_file

            def instance_env_path(self, name):  # noqa: ARG002
                return self._env_file or Path("/tmp/nonexistent-env-file")

        self.store = _Store(env_file)

    def _make_runtime_tree_writable(self, datadir):
        return None


def test_openclaw_adapter_writes_chat_completions_flag_when_a2a_enabled(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    # configure_before_run / run_spec don't touch the manager — only the
    # image-build path does — so we can skip constructing a real one.
    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    adapter.configure_before_run(_StubService(), record)

    cfg = json.loads((Path(record.datadir) / "openclaw.json").read_text())
    assert (
        cfg["gateway"]["http"]["endpoints"]["chatCompletions"]["enabled"] is True
    ), "a2a_enabled must flip the gateway chatCompletions endpoint on"


def test_openclaw_adapter_omits_chat_completions_flag_when_a2a_disabled(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    # configure_before_run / run_spec don't touch the manager — only the
    # image-build path does — so we can skip constructing a real one.
    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=False)
    adapter.configure_before_run(_StubService(), record)

    cfg = json.loads((Path(record.datadir) / "openclaw.json").read_text())
    endpoints = cfg.get("gateway", {}).get("http", {}).get("endpoints", {})
    assert "chatCompletions" not in endpoints, (
        "non-a2a instances must not expose the OpenAI-compat endpoint"
    )


def test_openclaw_adapter_run_spec_exposes_gateway_port_when_a2a(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    # configure_before_run / run_spec don't touch the manager — only the
    # image-build path does — so we can skip constructing a real one.
    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)

    assert spec.extra_env["A2A_GATEWAY_PORT"] == str(adapter.internal_port), (
        "sidecar needs A2A_GATEWAY_PORT to reach the in-container gateway"
    )
    assert spec.extra_env["A2A_SIDECAR_NAME"] == "writer"
    host_ports = [host for host, _ in spec.additional_ports]
    assert record.port + 1 in host_ports, (
        "sidecar must be published on record.port + 1 for neighbor-port federation"
    )


def _make_hermes_record(tmp_path, *, a2a_enabled: bool = True):
    from clawcu.models import InstanceRecord

    datadir = tmp_path / "inst"
    datadir.mkdir()
    return InstanceRecord(
        service="hermes",
        name="scribe",
        version="v0.10.0",
        datadir=str(datadir),
        port=8652,
        cpu="1",
        memory="2g",
        auth_mode="native",
        dashboard_port=9129,
        a2a_enabled=a2a_enabled,
        upstream_ref="ghcr.io/openclaw/hermes-agent:v0.10.0",
        image_tag="clawcu/hermes-agent-a2a:v0.10.0-plugin0.2.10",
        container_name="clawcu-hermes-scribe",
        status="created",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    )


class _StubServiceHermes:
    """Stub for hermes adapter — provides the env loader helpers it calls."""

    def __init__(self):
        self.messages: list[str] = []
        self.reporter = self.messages.append

    def _load_env_file(self, path):  # noqa: ARG002
        return {}

    def _dump_env_file(self, values):
        return "\n".join(f"{k}={v}" for k, v in values.items()) + "\n"

    def _make_runtime_tree_writable(self, datadir):  # noqa: ARG002
        return None


def test_hermes_adapter_scaffolds_soul_md_when_a2a_enabled(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter, HERMES_SOUL_FILENAME

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    adapter.configure_before_run(stub, record)

    soul_path = Path(record.datadir) / HERMES_SOUL_FILENAME
    assert soul_path.exists(), (
        "a2a-enabled hermes instances must have a SOUL.md placeholder at datadir "
        "so the persona-injection contract is documented and usable"
    )
    assert "persona" in soul_path.read_text().lower()
    assert any("SOUL.md" in m for m in stub.messages), (
        "reporter should mention SOUL.md so the user knows where to edit"
    )


def test_hermes_adapter_does_not_scaffold_soul_md_when_a2a_disabled(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter, HERMES_SOUL_FILENAME

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=False)
    adapter.configure_before_run(_StubServiceHermes(), record)

    soul_path = Path(record.datadir) / HERMES_SOUL_FILENAME
    assert not soul_path.exists()


def test_hermes_adapter_preserves_user_soul_md(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter, HERMES_SOUL_FILENAME

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    soul_path = Path(record.datadir) / HERMES_SOUL_FILENAME
    soul_path.write_text("# Custom\nYou are Scribe.\n", encoding="utf-8")

    adapter.configure_before_run(_StubServiceHermes(), record)

    assert "You are Scribe." in soul_path.read_text(), (
        "an existing user-authored SOUL.md must never be overwritten"
    )


def test_openclaw_adapter_run_spec_no_a2a_ports_when_disabled(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    # configure_before_run / run_spec don't touch the manager — only the
    # image-build path does — so we can skip constructing a real one.
    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=False)
    spec = adapter.run_spec(_StubService(), record)

    assert spec.extra_env == {}
    assert spec.additional_ports == []


def test_openclaw_adapter_run_spec_sets_gateway_ready_path_to_healthz(tmp_path):
    # Review-7 P2-E: each adapter declares its own readiness path so the
    # sidecar stays gateway-agnostic. OpenClaw's gateway serves /healthz.
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)

    assert spec.extra_env["A2A_GATEWAY_READY_PATH"] == "/healthz"


def test_hermes_adapter_run_spec_sets_gateway_ready_path_to_health(tmp_path):
    # Review-7 P2-E mirror of the openclaw test: hermes' API server serves
    # /health, and the adapter has to tell the (gateway-agnostic) sidecar.
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    # The stub's bare _load_env_file returns {}, so we bypass it with one
    # that behaves like the real service: auto-fills API_SERVER_KEY.
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)

    assert spec.extra_env["A2A_GATEWAY_READY_PATH"] == "/health"


def test_openclaw_adapter_run_spec_sets_sidecar_log_dir_under_mount(tmp_path):
    # Review-10 P2-C: the adapter points the sidecar at a log dir that lives
    # inside the datadir bind-mount, so logs survive `clawcu recreate`.
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)

    # Must sit under the openclaw mount_target so it lands on the host datadir.
    assert spec.extra_env["A2A_SIDECAR_LOG_DIR"] == "/home/node/.openclaw/logs"


def test_openclaw_adapter_run_spec_omits_sidecar_log_dir_without_a2a(tmp_path):
    # When a2a is disabled the sidecar isn't running, so the log-dir env
    # must not leak — it's a sidecar-only concern.
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=False)
    spec = adapter.run_spec(_StubService(), record)

    assert "A2A_SIDECAR_LOG_DIR" not in spec.extra_env


def test_openclaw_adapter_run_spec_sets_thread_dir_under_mount(tmp_path):
    # Review-13 P1-C: the adapter points the sidecar at a thread-history
    # directory living inside the datadir mount, so threaded conversations
    # survive `clawcu recreate`.
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)

    assert spec.extra_env["A2A_THREAD_DIR"] == "/home/node/.openclaw/threads"


def test_openclaw_adapter_run_spec_omits_thread_dir_without_a2a(tmp_path):
    # When a2a is off, thread storage is a sidecar-only concern and must
    # not leak into the container env.
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=False)
    spec = adapter.run_spec(_StubService(), record)

    assert "A2A_THREAD_DIR" not in spec.extra_env


def test_openclaw_adapter_does_not_shadow_user_a2a_thread_max_history(tmp_path):
    # Review-13 P1-C: like A2A_MODEL (review-12 P2-B), the history cap is
    # intentionally user-tunable via the instance env file. Adapter must
    # NOT inject A2A_THREAD_MAX_HISTORY_PAIRS in extra_env — otherwise
    # --env would overlay the env-file value.
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)

    assert "A2A_THREAD_MAX_HISTORY_PAIRS" not in spec.extra_env


def test_openclaw_adapter_does_not_shadow_user_a2a_model_env(tmp_path):
    """Review-12 P2-B: the sidecar hardcodes the openclaw gateway default
    (``model = "openclaw"``), but users can redirect A2A traffic to a
    specific agent by putting ``A2A_MODEL=openclaw/<agentId>`` in the
    instance env file. Docker reads --env-file first and then overlays
    --env, so the adapter must NOT inject A2A_MODEL in ``extra_env`` —
    otherwise we'd silently shadow the user's env-file override.
    """
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)

    assert "A2A_MODEL" not in spec.extra_env, (
        "A2A_MODEL must be controllable via the user's env file; the adapter "
        "must not shadow it with an explicit --env entry"
    )


def test_hermes_adapter_does_not_shadow_user_hermes_model_env(tmp_path):
    """Review-12 P2-B mirror: hermes sidecar defaults to HERMES_MODEL=hermes-agent
    but users can point at a different model via the env file. Adapter
    must not set it in extra_env (which would override the env-file value).
    """
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)

    assert "HERMES_MODEL" not in spec.extra_env


def test_hermes_adapter_run_spec_sets_sidecar_log_dir_under_mount(tmp_path):
    # Review-10 P2-C mirror of the openclaw test.
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)

    assert spec.extra_env["A2A_SIDECAR_LOG_DIR"] == "/opt/data/logs"


def test_hermes_adapter_run_spec_sets_thread_dir_under_mount(tmp_path):
    # Review-14 P1-C (hermes mirror of iter 13): the adapter points the
    # sidecar at a thread-history directory inside the /opt/data mount,
    # so threaded conversations survive `clawcu recreate`.
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)

    assert spec.extra_env["A2A_THREAD_DIR"] == "/opt/data/threads"


def test_hermes_adapter_run_spec_omits_thread_dir_without_a2a(tmp_path):
    # Without a2a the sidecar isn't running, so A2A_THREAD_DIR must not
    # leak into the container env.
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=False)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)

    assert "A2A_THREAD_DIR" not in spec.extra_env


def test_hermes_adapter_does_not_shadow_user_a2a_thread_max_history(tmp_path):
    # Like A2A_MODEL (review-12 P2-B), the history cap is user-tunable via
    # the instance env file. Adapter must NOT inject it in extra_env —
    # docker --env would overlay the env-file value.
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)

    assert "A2A_THREAD_MAX_HISTORY_PAIRS" not in spec.extra_env


def _load_hermes_sidecar_module():
    """Load the hermes sidecar as a module without adding a package __init__."""
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "clawcu"
        / "a2a"
        / "sidecar_plugin"
        / "hermes"
        / "sidecar.py"
    )
    spec = importlib.util.spec_from_file_location("_hermes_sidecar_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hermes_sidecar_wait_for_gateway_ready_succeeds_when_health_live(monkeypatch):
    mod = _load_hermes_sidecar_module()
    # Reset the cache so prior tests don't short-circuit.
    monkeypatch.setattr(mod, "_GATEWAY_READY_UNTIL", 0.0, raising=False)

    class _OKHandler(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_GET(self):  # noqa: N802
            if self.path != "/health":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _OKHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        monkeypatch.setenv("HERMES_API_HOST", host)
        monkeypatch.setenv("HERMES_API_PORT", str(port))
        monkeypatch.setenv("A2A_GATEWAY_READY_DEADLINE_S", "2")
        monkeypatch.setenv("A2A_GATEWAY_READY_PROBE_S", "1")
        cfg = mod.Config()
        assert mod.wait_for_gateway_ready(cfg) is True
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_hermes_sidecar_honors_a2a_gateway_ready_path_env(monkeypatch):
    # Review-7 P2-E: sidecar must probe whatever path the adapter declared,
    # not a hardcoded /health. This test points the sidecar at /custompath
    # and asserts that's what ends up in the HTTP request URL.
    mod = _load_hermes_sidecar_module()
    monkeypatch.setattr(mod, "_GATEWAY_READY_UNTIL", 0.0, raising=False)

    probed_paths: list[str] = []

    class _PathProbingHandler(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_GET(self):  # noqa: N802
            probed_paths.append(self.path)
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PathProbingHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        monkeypatch.setenv("HERMES_API_HOST", host)
        monkeypatch.setenv("HERMES_API_PORT", str(port))
        monkeypatch.setenv("A2A_GATEWAY_READY_DEADLINE_S", "2")
        monkeypatch.setenv("A2A_GATEWAY_READY_PROBE_S", "1")
        monkeypatch.setenv("A2A_GATEWAY_READY_PATH", "/custompath")
        cfg = mod.Config()
        assert mod.wait_for_gateway_ready(cfg) is True
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)

    assert probed_paths == ["/custompath"], (
        f"sidecar ignored A2A_GATEWAY_READY_PATH; probed {probed_paths!r}"
    )


def test_hermes_sidecar_wait_for_gateway_ready_times_out_when_unreachable(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setattr(mod, "_GATEWAY_READY_UNTIL", 0.0, raising=False)
    # Port 1 is refused on every host.
    monkeypatch.setenv("HERMES_API_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_API_PORT", "1")
    monkeypatch.setenv("A2A_GATEWAY_READY_DEADLINE_S", "0.3")
    monkeypatch.setenv("A2A_GATEWAY_READY_PROBE_S", "0.1")
    monkeypatch.setenv("A2A_GATEWAY_READY_POLL_S", "0.05")
    cfg = mod.Config()
    assert mod.wait_for_gateway_ready(cfg) is False


def test_hermes_sidecar_call_hermes_sends_system_prompt_and_parses_reply(monkeypatch):
    mod = _load_hermes_sidecar_module()

    monkeypatch.setenv("A2A_SYSTEM_PROMPT", "be terse")
    monkeypatch.setenv("HERMES_API_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_API_PORT", "9999")
    monkeypatch.setenv("API_SERVER_KEY", "secret-token")
    monkeypatch.setenv("HERMES_MODEL", "hermes-agent")

    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __init__(self, payload: bytes):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        payload = {
            "choices": [{"message": {"content": "hi from hermes"}}]
        }
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    cfg = mod.Config()
    reply = mod.call_hermes(cfg, "ping", "peer-agent")
    assert reply == "hi from hermes"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    body = captured["body"]
    assert body["model"] == "hermes-agent"
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["messages"][-1]["role"] == "user"
    assert "[from agent 'peer-agent']" in body["messages"][-1]["content"]
    assert body["messages"][-1]["content"].endswith("ping")


def test_a2a_up_skips_echo_bridge_when_ipv6_bound(monkeypatch, temp_clawcu_home):
    import socket as _socket

    if not _socket.has_ipv6:
        pytest.skip("IPv6 not available on this host")
    try:
        holder = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
    except OSError:
        pytest.skip("cannot create IPv6 socket")
    try:
        holder.bind(("::1", 0))
    except OSError:
        holder.close()
        pytest.skip("IPv6 localhost unavailable")
    holder.listen(128)
    held_port = holder.getsockname()[1]
    try:
        with _socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            registry_port = s.getsockname()[1]

        ghost = FakeRecord(name="ghost", service="openclaw", port=0)

        class Svc:
            def list_instances(self, *, running_only=False):  # noqa: ARG002
                return [ghost]

            def adapter_for_record(self, record):  # noqa: ARG002
                return _FixedPortAdapter(held_port)

        monkeypatch.setattr("clawcu.a2a.cli.ClawCUService", lambda: Svc())

        bind_calls: list[int] = []
        original_build = build_bridge_server

        def spy_build(card, *, host, port, reply_fn):
            bind_calls.append(port)
            return original_build(card, host=host, port=port, reply_fn=reply_fn)

        monkeypatch.setattr("clawcu.a2a.cli.build_bridge_server", spy_build)

        def fake_registry_serve(provider, *, host, port, on_ready=None):  # noqa: ARG001
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "clawcu.a2a.cli.serve_registry_forever", fake_registry_serve
        )

        result = runner.invoke(
            app,
            [
                "a2a",
                "up",
                "--registry-port",
                str(registry_port),
                "--probe-timeout",
                "0.2",
                "--probe-attempts",
                "1",
                "--probe-delay",
                "0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "already in use" in result.output
        assert bind_calls == []
    finally:
        holder.close()


# ---------------------------------------------------------------------------
# Iter 6 — plugin fingerprint defeats image-tag staleness under editable-install
# ---------------------------------------------------------------------------


def test_plugin_fingerprint_is_stable_and_short():
    """plugin_fingerprint is deterministic for the current working tree."""
    from clawcu.a2a.sidecar_plugin import plugin_fingerprint, plugin_source_sha

    first = plugin_fingerprint("hermes", "9.9.9")
    second = plugin_fingerprint("hermes", "9.9.9")
    assert first == second
    assert first.startswith("9.9.9.")
    sha_part = first.split(".", 3)[-1]
    # sha_part is the 10-char hex from plugin_source_sha.
    assert len(sha_part) == 10
    assert sha_part == plugin_source_sha("hermes")
    assert all(c in "0123456789abcdef" for c in sha_part)


def test_plugin_fingerprint_differs_per_service():
    """openclaw and hermes have different sidecar sources → different shas."""
    from clawcu.a2a.sidecar_plugin import plugin_fingerprint

    assert plugin_fingerprint("openclaw", "1.0.0") != plugin_fingerprint(
        "hermes", "1.0.0"
    )


def test_plugin_source_sha_ignores_pycache_and_pyc(tmp_path, monkeypatch):
    """Review-8 P2-H: runtime-generated bytecode must not shift the fingerprint.

    If an import side-effect (e.g. CLI help path that touches the hermes
    sidecar module) materialises ``__pycache__/sidecar.cpython-*.pyc``
    inside the plugin tree, the image tag would churn on every dev run and
    we'd re-bake for no reason. This test writes fake bytecode into a copy
    of the plugin tree, then asserts the sha matches the pristine copy.
    """
    import shutil

    from clawcu.a2a import sidecar_plugin as plugin_mod

    real_source = plugin_mod.plugin_source_dir("hermes")
    baseline_sha = plugin_mod.plugin_source_sha("hermes")

    # Clone the plugin tree into tmp_path, add bytecode/garbage, point
    # plugin_source_dir at the clone, and recompute.
    fake_root = tmp_path / "plugin"
    fake_root.mkdir()
    fake_hermes = fake_root / "hermes"
    shutil.copytree(real_source, fake_hermes)

    pycache = fake_hermes / "__pycache__"
    pycache.mkdir(exist_ok=True)
    (pycache / "sidecar.cpython-312.pyc").write_bytes(b"compiled bytecode garbage\0")
    # Also a .pyc at top level (belt & suspenders).
    (fake_hermes / "stale.pyc").write_bytes(b"more garbage")

    monkeypatch.setattr(plugin_mod, "_PLUGIN_ROOT", fake_root)
    perturbed_sha = plugin_mod.plugin_source_sha("hermes")

    assert perturbed_sha == baseline_sha, (
        "plugin_source_sha must ignore __pycache__ and .pyc so transient "
        "bytecode doesn't force image rebuilds"
    )


def test_plugin_source_sha_still_reacts_to_real_source_edits(tmp_path, monkeypatch):
    """Positive control for P2-H filter: a real file edit must change the sha.

    Without this, an over-eager filter could silently swallow real changes
    (e.g. someone adds ``*.py`` to the ignore list and breaks the contract).
    """
    import shutil

    from clawcu.a2a import sidecar_plugin as plugin_mod

    real_source = plugin_mod.plugin_source_dir("hermes")

    fake_root = tmp_path / "plugin"
    fake_root.mkdir()
    fake_hermes = fake_root / "hermes"
    shutil.copytree(real_source, fake_hermes)

    monkeypatch.setattr(plugin_mod, "_PLUGIN_ROOT", fake_root)
    baseline = plugin_mod.plugin_source_sha("hermes")

    # Touch a real source file.
    sidecar = fake_hermes / "sidecar.py"
    sidecar.write_text(sidecar.read_text(encoding="utf-8") + "\n# perturbation\n", encoding="utf-8")
    perturbed = plugin_mod.plugin_source_sha("hermes")

    assert perturbed != baseline, (
        "editing a real sidecar file must still change the fingerprint"
    )


def test_openclaw_dockerfile_copies_whole_sidecar_dir():
    """Review-11 guard: the sidecar is split into multiple .js modules
    (server / readiness / ratelimit / logsink) and server.js ``require``s
    its siblings at ``__dirname``. If the Dockerfile copied only
    server.js, the runtime would crash on module resolution even though
    unit tests would pass. The cheapest enforcement is: every *.js file
    in ``sidecar/`` must appear in a ``COPY`` directive — and the
    simplest way to guarantee that is to copy the directory, not
    individual files.
    """
    from clawcu.a2a import sidecar_plugin as plugin_mod

    source_dir = plugin_mod.plugin_source_dir("openclaw")
    dockerfile = (source_dir / "Dockerfile").read_text(encoding="utf-8")
    sidecar_dir = source_dir / "sidecar"
    js_files = sorted(p.name for p in sidecar_dir.glob("*.js"))
    assert js_files, "test preconditions broken: no sidecar .js files found"

    copy_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.lstrip().startswith("COPY") and "sidecar" in line
    ]
    joined = "\n".join(copy_lines)
    # Either the whole directory is copied (preferred), or every file is.
    directory_copy = "sidecar/ /opt/a2a" in joined or "sidecar /opt/a2a" in joined
    per_file = all(f"sidecar/{name}" in joined for name in js_files)
    assert directory_copy or per_file, (
        f"Dockerfile must COPY all sidecar .js files; "
        f"found COPY lines:\n{joined}\n"
        f"expected files: {js_files}"
    )


def test_a2a_image_tag_embeds_source_sha_not_raw_version(tmp_path, monkeypatch):
    """Tag component after ``plugin`` is ``<ver>.<sha>``, not bare ``<ver>``.

    Regression against review-5 P0-c: when clawcu is editable-installed and
    the user bumps ``__version__`` without reinstalling, the old code path
    produced a stale tag because ``clawcu.__version__`` stayed pinned to the
    install metadata. Including the plugin source sha makes the tag change
    whenever the sidecar code changes, regardless of version staleness.
    """
    from clawcu.a2a.builder import a2a_image_tag
    from clawcu.a2a.sidecar_plugin import plugin_source_sha

    tag = a2a_image_tag("hermes", "v2026.4.13", "0.2.11")
    sha = plugin_source_sha("hermes")
    assert tag == f"clawcu/hermes-agent-a2a:v2026.4.13-plugin0.2.11.{sha}"
    # Must NOT be the old bare-version tag format.
    assert tag != "clawcu/hermes-agent-a2a:v2026.4.13-plugin0.2.11"


# ---------------------------------------------------------------------------
# Iter 6 — gateway-ready cache invalidation on upstream failure (P1-A)
# ---------------------------------------------------------------------------


def test_hermes_sidecar_invalidate_gateway_ready_resets_cache():
    """invalidate_gateway_ready_cache drops the TTL so next call re-probes.

    Review-4 P1-A: the 5-minute TTL was too eager — a gateway that went
    sick would keep getting blind pushes for up to 5 minutes before the next
    probe. The sidecar now explicitly invalidates after upstream failures
    that suggest the gateway is unhealthy (URLError, 5xx HTTPError).
    """
    sidecar_mod = _load_hermes_sidecar_module()
    # Prime the cache: pretend we just saw a 200 /health.
    sidecar_mod._GATEWAY_READY_UNTIL = 9e18  # far future
    assert sidecar_mod._GATEWAY_READY_UNTIL > 0
    sidecar_mod.invalidate_gateway_ready_cache()
    assert sidecar_mod._GATEWAY_READY_UNTIL == 0.0


# ---------------------------------------------------------------------------
# Iter 7 — `clawcu hermes identity set` CLI contract (review-5 P1-E)
# ---------------------------------------------------------------------------


def test_hermes_identity_set_writes_soul_md_to_datadir(tmp_path):
    """set_hermes_identity copies source into ``<datadir>/SOUL.md`` verbatim."""
    from clawcu.core.models import InstanceRecord

    source = tmp_path / "my-persona.md"
    source.write_text("# My Scribe\n\nSign off with ZZZZ.\n", encoding="utf-8")
    datadir = tmp_path / "datadir"
    datadir.mkdir()

    record = InstanceRecord(
        name="scribe",
        service="hermes",
        version="2026.4.13",
        port=8642,
        datadir=str(datadir),
        cpu="1",
        memory="2g",
        auth_mode="token",
        upstream_ref="v2026.4.13",
        image_tag="clawcu/hermes-agent:v2026.4.13",
        container_name="clawcu-hermes-scribe",
        status="running",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        a2a_enabled=True,
    )

    class _Store:
        def load_record(self_, name):  # noqa: N805
            assert name == "scribe"
            return record

        def append_log(self_, _line):  # noqa: N805
            pass

    class _Service:
        store = _Store()

    from clawcu.core.service import ClawCUService

    result = ClawCUService.set_hermes_identity(_Service(), "scribe", source)
    written = (datadir / "SOUL.md").read_text(encoding="utf-8")
    assert written == "# My Scribe\n\nSign off with ZZZZ.\n"
    assert result["target"] == str(datadir / "SOUL.md")
    assert result["bytes"] == len(written)
    assert result["instance"] == "scribe"


def test_hermes_identity_set_rejects_non_hermes_service(tmp_path):
    from clawcu.core.models import InstanceRecord

    source = tmp_path / "p.md"
    source.write_text("x\n", encoding="utf-8")
    record = InstanceRecord(
        name="jim",
        service="openclaw",
        version="2026.4.12",
        port=18789,
        datadir=str(tmp_path),
        cpu="1",
        memory="2g",
        auth_mode="token",
        upstream_ref="2026.4.12",
        image_tag="clawcu/openclaw-a2a:2026.4.12",
        container_name="clawcu-openclaw-jim",
        status="running",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        a2a_enabled=False,
    )

    class _Store:
        def load_record(self_, _n):  # noqa: N805
            return record

        def append_log(self_, _l):  # noqa: N805
            pass

    class _Service:
        store = _Store()

    from clawcu.core.service import ClawCUService

    with pytest.raises(ValueError, match="only available for hermes"):
        ClawCUService.set_hermes_identity(_Service(), "jim", source)


def test_hermes_identity_set_rejects_empty_file(tmp_path):
    from clawcu.core.models import InstanceRecord

    source = tmp_path / "empty.md"
    source.write_text("   \n\n", encoding="utf-8")
    record = InstanceRecord(
        name="scribe",
        service="hermes",
        version="2026.4.13",
        port=8642,
        datadir=str(tmp_path),
        cpu="1",
        memory="2g",
        auth_mode="token",
        upstream_ref="v2026.4.13",
        image_tag="clawcu/hermes-agent:v2026.4.13",
        container_name="clawcu-hermes-scribe",
        status="running",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
        a2a_enabled=True,
    )

    class _Store:
        def load_record(self_, _n):  # noqa: N805
            return record

        def append_log(self_, _l):  # noqa: N805
            pass

    class _Service:
        store = _Store()

    from clawcu.core.service import ClawCUService

    with pytest.raises(ValueError, match="empty"):
        ClawCUService.set_hermes_identity(_Service(), "scribe", source)


def test_hermes_sidecar_wait_for_gateway_ready_uses_cache_until_invalidated(
    monkeypatch,
):
    """wait_for_gateway_ready short-circuits while cache valid; re-probes after invalidation."""
    sidecar_mod = _load_hermes_sidecar_module()

    probe_calls = {"n": 0}

    def fake_probe(_cfg):
        probe_calls["n"] += 1
        return True

    monkeypatch.setattr(sidecar_mod, "_probe_gateway_ready", fake_probe)
    monkeypatch.setenv("HERMES_API_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_API_PORT", "1")
    monkeypatch.setenv("A2A_GATEWAY_READY_DEADLINE_S", "2")
    monkeypatch.setenv("A2A_GATEWAY_READY_POLL_S", "0.01")
    cfg = sidecar_mod.Config()

    # Reset module-level cache first; pristine state.
    sidecar_mod._GATEWAY_READY_UNTIL = 0.0

    # 1st call → probes once, caches success.
    assert sidecar_mod.wait_for_gateway_ready(cfg) is True
    assert probe_calls["n"] == 1

    # 2nd call → cache hit, no new probe.
    assert sidecar_mod.wait_for_gateway_ready(cfg) is True
    assert probe_calls["n"] == 1

    # Invalidate → next call re-probes.
    sidecar_mod.invalidate_gateway_ready_cache()
    assert sidecar_mod.wait_for_gateway_ready(cfg) is True
    assert probe_calls["n"] == 2


# -- Review-14 P1-C hermes ThreadStore unit tests ----------------------------
# Parallel of tests/sidecar_thread.test.js (node --test). We reuse the
# `_load_hermes_sidecar_module` harness so the class is tested in the same
# shape it runs in production (inline in sidecar.py, no separate module).


def test_hermes_thread_store_disabled_when_dir_empty():
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir="")
    assert store.enabled is False
    assert store.load_history("peer", "tid") == []
    assert store.append_turn("peer", "tid", "hi", "hello") is False


def test_hermes_thread_store_load_missing_returns_empty(tmp_path):
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir=str(tmp_path))
    assert store.load_history("peer-a", "tid-1") == []


def test_hermes_thread_store_roundtrip_preserves_order(tmp_path):
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir=str(tmp_path))
    assert store.append_turn("peer-a", "tid-1", "hi", "hello") is True
    assert store.append_turn("peer-a", "tid-1", "how are you", "fine") is True
    history = store.load_history("peer-a", "tid-1")
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you"},
        {"role": "assistant", "content": "fine"},
    ]


def test_hermes_thread_store_caps_at_max_history_pairs(tmp_path):
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir=str(tmp_path), max_history_pairs=2)
    for i in range(5):
        store.append_turn("peer-a", "tid-1", f"u{i}", f"a{i}")
    history = store.load_history("peer-a", "tid-1")
    # 2 pairs = last 4 messages.
    assert history == [
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]


def test_hermes_thread_store_rejects_path_traversal(tmp_path):
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir=str(tmp_path))
    attempts = [
        ("../escape", "tid"),
        ("peer", "../escape"),
        ("peer/sub", "tid"),
        ("peer", "tid/sub"),
        ("..", "tid"),
        ("peer", ".."),
        ("", "tid"),
        ("peer", ""),
    ]
    for peer, tid in attempts:
        assert store.append_turn(peer, tid, "x", "y") is False
        assert store.load_history(peer, tid) == []
    # Sibling directories of tmp_path must be untouched — nothing escaped.
    parent_children = [p.name for p in tmp_path.parent.iterdir()]
    assert not any(name.startswith("escape") for name in parent_children)


def test_hermes_thread_store_per_peer_isolation(tmp_path):
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir=str(tmp_path))
    store.append_turn("peer-a", "tid-1", "A-msg", "A-reply")
    store.append_turn("peer-b", "tid-1", "B-msg", "B-reply")
    assert store.load_history("peer-a", "tid-1") == [
        {"role": "user", "content": "A-msg"},
        {"role": "assistant", "content": "A-reply"},
    ]
    assert store.load_history("peer-b", "tid-1") == [
        {"role": "user", "content": "B-msg"},
        {"role": "assistant", "content": "B-reply"},
    ]


def test_hermes_thread_store_skips_corrupt_lines(tmp_path):
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir=str(tmp_path))
    store.append_turn("peer-a", "tid-1", "hi", "hello")
    # Corrupt the jsonl file by hand.
    file_path = tmp_path / "peer-a" / "tid-1.jsonl"
    with open(file_path, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    store.append_turn("peer-a", "tid-1", "still there?", "yes")
    history = store.load_history("peer-a", "tid-1")
    assert history == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "still there?"},
        {"role": "assistant", "content": "yes"},
    ]


def test_hermes_thread_store_rejects_non_string_content(tmp_path):
    mod = _load_hermes_sidecar_module()
    store = mod.ThreadStore(storage_dir=str(tmp_path))
    assert store.append_turn("peer-a", "tid-1", 42, "ok") is False  # type: ignore[arg-type]
    assert store.append_turn("peer-a", "tid-1", "ok", None) is False  # type: ignore[arg-type]
    assert store.load_history("peer-a", "tid-1") == []


def test_hermes_safe_id_contract():
    mod = _load_hermes_sidecar_module()
    assert mod.safe_id("0194c3f0-7d1a-7a3e-8b8e-7e0e7a1f6d42") == "0194c3f0-7d1a-7a3e-8b8e-7e0e7a1f6d42"
    assert mod.safe_id("peer.name_01") == "peer.name_01"
    assert mod.safe_id("") is None
    assert mod.safe_id(".") is None
    assert mod.safe_id("..") is None
    assert mod.safe_id("peer/with/slash") is None
    assert mod.safe_id("peer with space") is None
    assert mod.safe_id(None) is None
    assert mod.safe_id("x" * 129) is None  # length cap at 128
