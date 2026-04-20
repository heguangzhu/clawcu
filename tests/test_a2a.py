from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
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
