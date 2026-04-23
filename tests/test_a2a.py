from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
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
    localize_endpoint_for_host,
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


def test_cards_from_service_skips_starting_record_when_probe_fails():
    """Review-12 P2-D2: a ``starting`` instance whose sidecar probe fails
    must be dropped from federation entirely, not published with a
    placeholder endpoint. Running instances with failed probes still
    get the placeholder (they've passed a healthcheck at least once)."""

    @dataclass
    class FakeRecordWithStatus:
        name: str
        service: str
        port: int
        status: str

    fresh = FakeRecordWithStatus(
        name="fresh", service="openclaw", port=0, status="starting"
    )
    healthy = FakeRecordWithStatus(
        name="healthy", service="openclaw", port=0, status="running"
    )
    # Both point at port 1 (reserved / unreachable) so the probe fails
    # deterministically. Only ``healthy`` should survive the filter.
    service = _FakeService([fresh, healthy], port_map={"fresh": 1, "healthy": 1})
    cards = cards_from_service(service, timeout=0.05)
    names = {c.name for c in cards}
    assert "fresh" not in names, "starting record with failed probe should be skipped"
    assert "healthy" in names, "running record should still get placeholder fallback"


def test_cards_from_service_keeps_starting_record_when_probe_succeeds():
    """P2-D2 inverse: a starting record whose sidecar actually responds
    gets federated — the fix must not regress the iter-10 P2-A4 win."""

    @dataclass
    class FakeRecordWithStatus:
        name: str
        service: str
        port: int
        status: str

    plugin_card = {
        "name": "fresh",
        "role": "r",
        "skills": ["s"],
        "endpoint": "http://127.0.0.1:18819/a2a/send",
    }
    server, thread = _start_well_known_server(body=json.dumps(plugin_card).encode())
    try:
        _, plugin_port = server.server_address
        fresh = FakeRecordWithStatus(
            name="fresh", service="openclaw", port=0, status="starting"
        )
        service = _FakeService([fresh], port_map={"fresh": plugin_port})
        cards = cards_from_service(service, timeout=1.0)
        assert [c.name for c in cards] == ["fresh"]
    finally:
        _stop(server, thread)


def test_cards_from_service_placeholder_uses_advertise_host(monkeypatch):
    """Review-13 P2-E1: when the sidecar probe fails on a running record,
    the placeholder card must use the record's advertise_host (so peer
    containers can actually reach it via host.docker.internal), not the
    registry's bind host which is just the host-side loopback."""

    @dataclass
    class FakeRecordWithAdvertise:
        name: str
        service: str
        port: int
        a2a_advertise_host: str | None = None

    # Force Darwin default via explicit per-record advertise_host so the
    # test doesn't depend on the host OS. This mirrors what iter-9 P1-A3
    # would stamp into the record on macOS.
    record = FakeRecordWithAdvertise(
        name="ghost", service="openclaw", port=0, a2a_advertise_host="host.docker.internal"
    )
    service = _FakeService([record], port_map={"ghost": 1})  # unreachable
    cards = cards_from_service(service, host="127.0.0.1", timeout=0.05)
    assert len(cards) == 1
    endpoint = cards[0].endpoint
    assert "host.docker.internal" in endpoint, (
        f"placeholder must use advertise host, got {endpoint!r}"
    )
    assert "127.0.0.1" not in endpoint, (
        f"placeholder must not leak the registry bind host, got {endpoint!r}"
    )


def test_cards_from_service_placeholder_respects_linux_default(monkeypatch):
    """P2-E1 Linux path: when default_advertise_host resolves to 127.0.0.1
    (Linux without docker-desktop), the placeholder still carries that
    loopback — this is the correct behavior because on Linux peers reach
    the host via --add-host host-gateway mapping, not via
    host.docker.internal. No regression from the Darwin fix."""
    import clawcu.a2a.sidecar_plugin as sidecar_plugin

    sidecar_plugin.default_advertise_host.cache_clear()
    monkeypatch.setattr(sidecar_plugin.platform, "system", lambda: "Linux")
    monkeypatch.delenv("CLAWCU_A2A_ADVERTISE_HOST", raising=False)
    try:
        @dataclass
        class FakeRecordLinux:
            name: str
            service: str
            port: int

        record = FakeRecordLinux(name="ghost", service="openclaw", port=0)
        service = _FakeService([record], port_map={"ghost": 1})
        cards = cards_from_service(service, timeout=0.05)
        assert len(cards) == 1
        assert "127.0.0.1" in cards[0].endpoint
    finally:
        sidecar_plugin.default_advertise_host.cache_clear()


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

    def _load_env_file(self, path):
        """Mirror ClawCUService._load_env_file for adapter unit tests.

        Minimal KEY=VALUE parser — adapter code only reads keys back out, not
        writes, so we don't need quote handling or escape sequences.
        """
        if path is None:
            return {}
        p = Path(path)
        if not p.exists():
            return {}
        out: dict[str, str] = {}
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
        return out


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


# Review-1 P0-B: registry URL auto-discovery. All four tests here guard the
# adapter-layer fix: --add-host flag on Linux + auto-injected default
# registry URL with user-override precedence.


def test_openclaw_adapter_a2a_adds_host_gateway_flag(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)
    assert ("host.docker.internal", "host-gateway") in spec.extra_hosts, (
        "A2A-enabled instances must map host.docker.internal so Linux hosts "
        "can reach the clawcu registry on the host network"
    )


def test_openclaw_adapter_does_not_add_host_flag_when_a2a_disabled(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=False)
    spec = adapter.run_spec(_StubService(), record)
    assert spec.extra_hosts == [], (
        "stock instances stay clean — no mesh-specific docker args"
    )


def test_openclaw_adapter_a2a_injects_default_registry_url(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)
    assert spec.extra_env.get("A2A_REGISTRY_URL") == "http://host.docker.internal:9100"


def test_openclaw_adapter_preserves_user_registry_url_override(tmp_path):
    """Parity with review-12 P2-B: user-set env in the file wins."""
    from clawcu.openclaw.adapter import OpenClawAdapter

    env_file = tmp_path / "writer.env"
    env_file.write_text("A2A_REGISTRY_URL=http://proxy.internal:9100\n", encoding="utf-8")
    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(env_file=env_file), record)
    assert "A2A_REGISTRY_URL" not in spec.extra_env, (
        "adapter must not shadow a user-set registry URL; user env file wins"
    )


def test_openclaw_adapter_a2a_injects_mcp_bootstrap_env(tmp_path):
    """a2a-design-4.md §P0-A: auto-wiring env for the MCP bootstrap."""
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(), record)
    assert spec.extra_env.get("A2A_ENABLED") == "true"
    assert (
        spec.extra_env.get("A2A_SERVICE_MCP_CONFIG_PATH")
        == "/home/node/.openclaw/openclaw.json"
    )
    assert spec.extra_env.get("A2A_SERVICE_MCP_CONFIG_FORMAT") == "json"


def test_openclaw_adapter_stock_omits_mcp_bootstrap_env(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=False)
    spec = adapter.run_spec(_StubService(), record)
    for key in ("A2A_ENABLED", "A2A_SERVICE_MCP_CONFIG_PATH", "A2A_SERVICE_MCP_CONFIG_FORMAT"):
        assert key not in spec.extra_env, f"{key} must not leak into stock instance spec"


def test_openclaw_adapter_user_override_of_mcp_bootstrap_env_wins(tmp_path):
    from clawcu.openclaw.adapter import OpenClawAdapter

    env_file = tmp_path / "writer.env"
    env_file.write_text(
        "A2A_SERVICE_MCP_CONFIG_PATH=/custom/path.json\n"
        "A2A_SERVICE_MCP_CONFIG_FORMAT=json\n",
        encoding="utf-8",
    )
    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    spec = adapter.run_spec(_StubService(env_file=env_file), record)
    assert "A2A_SERVICE_MCP_CONFIG_PATH" not in spec.extra_env
    assert "A2A_SERVICE_MCP_CONFIG_FORMAT" not in spec.extra_env


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


# Review-1 P0-B mirrors for hermes adapter.


def test_hermes_adapter_a2a_adds_host_gateway_flag(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)
    assert ("host.docker.internal", "host-gateway") in spec.extra_hosts


def test_hermes_adapter_does_not_add_host_flag_when_a2a_disabled(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=False)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)
    assert spec.extra_hosts == []


def test_hermes_adapter_a2a_injects_default_registry_url(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)
    assert spec.extra_env.get("A2A_REGISTRY_URL") == "http://host.docker.internal:9100"


def test_hermes_adapter_preserves_user_registry_url_override(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {
        "API_SERVER_KEY": "test-key",
        "A2A_REGISTRY_URL": "http://proxy.internal:9100",
    }  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)
    assert "A2A_REGISTRY_URL" not in spec.extra_env, (
        "user-set override wins over adapter default"
    )


def test_hermes_adapter_a2a_injects_mcp_bootstrap_env(tmp_path):
    """a2a-design-4.md §P0-A: auto-wiring env for the Hermes MCP bootstrap."""
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)
    assert spec.extra_env.get("A2A_ENABLED") == "true"
    assert spec.extra_env.get("A2A_SERVICE_MCP_CONFIG_PATH") == "/opt/data/config.yaml"
    assert spec.extra_env.get("A2A_SERVICE_MCP_CONFIG_FORMAT") == "yaml"


def test_hermes_adapter_stock_omits_mcp_bootstrap_env(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=False)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)
    for key in ("A2A_ENABLED", "A2A_SERVICE_MCP_CONFIG_PATH", "A2A_SERVICE_MCP_CONFIG_FORMAT"):
        assert key not in spec.extra_env, f"{key} must not leak into stock hermes spec"


def test_hermes_adapter_user_override_of_mcp_bootstrap_env_wins(tmp_path):
    from clawcu.hermes.adapter import HermesAdapter

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {
        "API_SERVER_KEY": "test-key",
        "A2A_SERVICE_MCP_CONFIG_PATH": "/alt/config.yaml",
    }  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)
    assert "A2A_SERVICE_MCP_CONFIG_PATH" not in spec.extra_env


# -- P1-I: adapter × bootstrap integration (a2a-design-5.md) ----------------


def test_hermes_adapter_extra_env_drives_bootstrap_merge_into_yaml(tmp_path):
    """Operator flow: adapter computes env → bootstrap merges `mcp.servers.a2a`
    into config.yaml. Asserts the contract end-to-end (Hermes YAML path)."""
    yaml = pytest.importorskip("yaml")
    from clawcu.hermes.adapter import HermesAdapter

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openrouter\napi_server:\n  host: 0.0.0.0\n",
        encoding="utf-8",
    )

    adapter = HermesAdapter(manager=None)
    record = _make_hermes_record(tmp_path, a2a_enabled=True)
    stub = _StubServiceHermes()
    stub._load_env_file = lambda path: {"API_SERVER_KEY": "test-key"}  # type: ignore[method-assign]
    spec = adapter.run_spec(stub, record)

    # Container-path override: point the bootstrap at the host temp file.
    env = dict(spec.extra_env)
    env["A2A_SERVICE_MCP_CONFIG_PATH"] = str(config_path)

    mod = _load_hermes_bootstrap_module()
    result = mod.run_bootstrap(env=env)
    assert result["ok"] is True
    assert result["action"] in {"create", "merge"}

    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert merged["mcp"]["servers"]["a2a"]["url"].startswith("http://127.0.0.1:")
    assert merged["mcp"]["servers"]["a2a"]["url"].endswith("/mcp")
    # Unrelated sections must survive the merge.
    assert merged["model"] == {"provider": "openrouter"}
    assert merged["api_server"] == {"host": "0.0.0.0"}


def test_openclaw_adapter_extra_env_drives_bootstrap_merge_into_json(tmp_path):
    """Operator flow: OpenClaw adapter (Python) → bootstrap.py → merged
    openclaw.json. Both sides are Python now, so the contract is exercised
    in-process — no subprocess fork needed."""
    import importlib.util

    from clawcu.openclaw.adapter import OpenClawAdapter

    bootstrap_py = (
        Path(__file__).resolve().parent.parent
        / "src" / "clawcu" / "a2a" / "sidecar_plugin"
        / "_common" / "bootstrap.py"
    )
    spec = importlib.util.spec_from_file_location("a2a_common_bootstrap", bootstrap_py)
    bootstrap_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap_mod)

    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps({"gateway": {"mode": "local"}, "llm": {"provider": "openrouter"}}),
        encoding="utf-8",
    )

    adapter = OpenClawAdapter(manager=None)
    record = _make_openclaw_record(tmp_path, a2a_enabled=True)
    run_spec = adapter.run_spec(_StubService(), record)
    env = dict(run_spec.extra_env)
    env["A2A_SERVICE_MCP_CONFIG_PATH"] = str(config_path)  # container → host path

    class _NullLogger:
        def info(self, *_a): pass
        def warn(self, *_a): pass
        def error(self, *_a): pass
        log = info

    result = bootstrap_mod.run_bootstrap(env=env, logger=_NullLogger())
    assert result["ok"] is True
    assert result["action"] in {"create", "merge"}

    merged = json.loads(config_path.read_text(encoding="utf-8"))
    assert merged["mcp"]["servers"]["a2a"]["url"].startswith("http://127.0.0.1:")
    assert merged["mcp"]["servers"]["a2a"]["url"].endswith("/mcp")
    # Sibling sections untouched.
    assert merged["gateway"] == {"mode": "local"}
    assert merged["llm"] == {"provider": "openrouter"}


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

        def read(self, n=-1):
            if n is not None and n >= 0:
                return self._payload[:n]
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
    real_common = plugin_mod._PLUGIN_ROOT / "_common"
    baseline_sha = plugin_mod.plugin_source_sha("hermes")

    # Clone the plugin tree into tmp_path, add bytecode/garbage, point
    # plugin_source_dir at the clone, and recompute. ``_common/`` is folded
    # into the service sha, so it also needs to live at the fake root.
    fake_root = tmp_path / "plugin"
    fake_root.mkdir()
    fake_hermes = fake_root / "hermes"
    shutil.copytree(real_source, fake_hermes)
    shutil.copytree(real_common, fake_root / "_common")

    pycache = fake_hermes / "__pycache__"
    pycache.mkdir(exist_ok=True)
    (pycache / "sidecar.cpython-312.pyc").write_bytes(b"compiled bytecode garbage\0")
    # Also a .pyc at top level (belt & suspenders).
    (fake_hermes / "stale.pyc").write_bytes(b"more garbage")
    # And inside _common/ — the filter must apply there too.
    (fake_root / "_common" / "__pycache__").mkdir(exist_ok=True)
    (fake_root / "_common" / "__pycache__" / "ratelimit.cpython-312.pyc").write_bytes(b"x\0")

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
    real_common = plugin_mod._PLUGIN_ROOT / "_common"

    fake_root = tmp_path / "plugin"
    fake_root.mkdir()
    fake_hermes = fake_root / "hermes"
    shutil.copytree(real_source, fake_hermes)
    shutil.copytree(real_common, fake_root / "_common")

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
    """Review-11 guard: the sidecar is split into multiple modules
    (server / readiness / ratelimit / logsink / ...) and server.py
    imports its siblings at ``__file__``'s directory. If the Dockerfile
    copied only server.py, the runtime would crash on import resolution
    even though unit tests would pass. The cheapest enforcement is:
    every source file in ``sidecar/`` must appear in a ``COPY``
    directive — and the simplest way to guarantee that is to copy the
    directory, not individual files.
    """
    from clawcu.a2a import sidecar_plugin as plugin_mod

    source_dir = plugin_mod.plugin_source_dir("openclaw")
    dockerfile = (source_dir / "Dockerfile").read_text(encoding="utf-8")
    sidecar_dir = source_dir / "sidecar"
    py_files = sorted(p.name for p in sidecar_dir.glob("*.py") if p.name != "__init__.py")
    assert py_files, "test preconditions broken: no sidecar .py files found"

    copy_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.lstrip().startswith("COPY") and "sidecar" in line
    ]
    joined = "\n".join(copy_lines)
    # Either the whole directory is copied (preferred), or every file is.
    directory_copy = "sidecar/ /opt/a2a" in joined or "sidecar /opt/a2a" in joined
    per_file = all(f"sidecar/{name}" in joined for name in py_files)
    assert directory_copy or per_file, (
        f"Dockerfile must COPY all sidecar .py files; "
        f"found COPY lines:\n{joined}\n"
        f"expected files: {py_files}"
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


# ---------------------------------------------------------------------------
# Iter-1 (0.3.1) outbound primitive — a2a-design-1.md §Protocol.
# Exercises lookup_peer / forward_to_peer / read_hop_header plus the full
# POST /a2a/outbound handler against stub registry + stub peer HTTP servers.
# ---------------------------------------------------------------------------


def _start_http(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _stop_http(srv, t):
    srv.shutdown()
    srv.server_close()
    t.join(timeout=2)


def _stub_registry(cards: dict) -> type[BaseHTTPRequestHandler]:
    """Return a handler class that serves `/agents/<name>` from the given map."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_GET(self):  # noqa: N802
            prefix = "/agents/"
            if self.path.startswith(prefix):
                name = self.path[len(prefix):]
                card = cards.get(name)
                if card is None:
                    body = json.dumps({"error": "not_found"}).encode()
                    self.send_response(404)
                else:
                    body = json.dumps(card).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return H


def test_hermes_sidecar_lookup_peer_returns_card_on_200():
    mod = _load_hermes_sidecar_module()
    cards = {
        "analyst": {
            "name": "analyst",
            "role": "hermes",
            "skills": ["chat"],
            "endpoint": "http://127.0.0.1:9129/a2a/send",
        }
    }
    srv, t = _start_http(_stub_registry(cards))
    try:
        host, port = srv.server_address
        got = mod.lookup_peer(f"http://{host}:{port}", "analyst", timeout=2.0)
        assert got["endpoint"] == "http://127.0.0.1:9129/a2a/send"
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_lookup_peer_404_raises_outbounderror_404():
    mod = _load_hermes_sidecar_module()
    srv, t = _start_http(_stub_registry({}))
    try:
        host, port = srv.server_address
        with pytest.raises(mod.OutboundError) as exc:
            mod.lookup_peer(f"http://{host}:{port}", "missing", timeout=2.0)
        assert exc.value.http_status == 404
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_lookup_peer_registry_5xx_maps_to_503():
    mod = _load_hermes_sidecar_module()

    class Boom(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_GET(self):  # noqa: N802
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv, t = _start_http(Boom)
    try:
        host, port = srv.server_address
        with pytest.raises(mod.OutboundError) as exc:
            mod.lookup_peer(f"http://{host}:{port}", "analyst", timeout=2.0)
        assert exc.value.http_status == 503
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_forward_to_peer_propagates_hop_header_and_body():
    mod = _load_hermes_sidecar_module()
    observed = {}

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            observed["hop"] = self.headers.get("X-A2A-Hop")
            length = int(self.headers.get("Content-Length") or 0)
            observed["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {"from": "analyst", "reply": "42", "thread_id": None}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv, t = _start_http(PeerH)
    try:
        host, port = srv.server_address
        got = mod.forward_to_peer(
            endpoint=f"http://{host}:{port}/a2a/send",
            self_name="writer",
            peer_name="analyst",
            message="hi",
            thread_id=None,
            hop=3,
            timeout=2.0,
        )
        assert got["reply"] == "42"
        assert observed["hop"] == "3"
        assert observed["body"]["from"] == "writer"
        assert observed["body"]["to"] == "analyst"
        assert observed["body"]["message"] == "hi"
        assert "thread_id" not in observed["body"]
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_forward_to_peer_thread_id_propagated():
    mod = _load_hermes_sidecar_module()
    observed = {}

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            observed["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps(
                {"from": "analyst", "reply": "ok", "thread_id": "t-1"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv, t = _start_http(PeerH)
    try:
        host, port = srv.server_address
        mod.forward_to_peer(
            endpoint=f"http://{host}:{port}/a2a/send",
            self_name="writer",
            peer_name="analyst",
            message="hi",
            thread_id="t-1",
            hop=1,
            timeout=2.0,
        )
        assert observed["body"]["thread_id"] == "t-1"
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_forward_to_peer_508_surfaces_as_508():
    mod = _load_hermes_sidecar_module()

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            body = json.dumps({"error": "hop budget exceeded"}).encode()
            self.send_response(508)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv, t = _start_http(PeerH)
    try:
        host, port = srv.server_address
        with pytest.raises(mod.OutboundError) as exc:
            mod.forward_to_peer(
                endpoint=f"http://{host}:{port}/a2a/send",
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=9,
                timeout=2.0,
            )
        assert exc.value.http_status == 508
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_forward_to_peer_peer_500_maps_to_502():
    mod = _load_hermes_sidecar_module()

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv, t = _start_http(PeerH)
    try:
        host, port = srv.server_address
        with pytest.raises(mod.OutboundError) as exc:
            mod.forward_to_peer(
                endpoint=f"http://{host}:{port}/a2a/send",
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=1,
                timeout=2.0,
            )
        assert exc.value.http_status == 502
        assert exc.value.peer_status == 500
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_read_hop_header_variants():
    mod = _load_hermes_sidecar_module()

    class HM:
        def __init__(self, **kv):
            self.kv = {k: str(v) for k, v in kv.items()}

        def get(self, k, default=None):
            return self.kv.get(k, default)

    assert mod.read_hop_header(HM()) == 0
    assert mod.read_hop_header(HM(**{"X-A2A-Hop": "3"})) == 3
    assert mod.read_hop_header(HM(**{"X-A2A-Hop": "-2"})) == 0
    assert mod.read_hop_header(HM(**{"X-A2A-Hop": "abc"})) == 0


def test_hermes_sidecar_outbound_handler_end_to_end(monkeypatch):
    """Boot the handler inside a fake HTTP server, fire /a2a/outbound at it.

    Mirrors the openclaw node-test path: stub registry + stub peer sit on
    two separate ephemeral ports. The outbound handler resolves 'analyst'
    via the registry, then POSTs /a2a/send on the stub peer and relays the
    reply to the caller.
    """
    mod = _load_hermes_sidecar_module()
    monkeypatch.setattr(mod, "_GATEWAY_READY_UNTIL", 0.0, raising=False)
    monkeypatch.setenv("A2A_SELF_NAME", "writer-hermes")
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_ALLOW_CLIENT_REGISTRY_URL", "1")

    # --- stub peer ---
    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            body = json.dumps(
                {"from": "analyst", "reply": "3,421 rows", "thread_id": None}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer_srv, peer_t = _start_http(PeerH)
    peer_host, peer_port = peer_srv.server_address

    # --- stub registry ---
    registry_srv, registry_t = _start_http(
        _stub_registry(
            {
                "analyst": {
                    "name": "analyst",
                    "role": "hermes",
                    "skills": ["chat"],
                    "endpoint": f"http://{peer_host}:{peer_port}/a2a/send",
                }
            }
        )
    )
    reg_host, reg_port = registry_srv.server_address

    # --- sidecar handler under test, bound to ephemeral port ---
    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = json.dumps(
            {
                "to": "analyst",
                "message": "ingest counts?",
                "registry_url": f"http://{reg_host}:{reg_port}",
                "timeout_ms": 3000,
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            out = json.loads(resp.read().decode("utf-8"))
        assert out["from"] == "writer-hermes"
        assert out["to"] == "analyst"
        assert out["reply"] == "3,421 rows"
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)
        _stop_http(peer_srv, peer_t)
        _stop_http(registry_srv, registry_t)


def test_hermes_sidecar_send_rejects_hop_budget(monkeypatch):
    """Inbound /a2a/send with X-A2A-Hop >= budget returns 508 before any gateway work."""
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_HOP_BUDGET", "4")

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = json.dumps({"from": "x", "to": cfg.self_name, "message": "y"}).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/send",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "X-A2A-Hop": "4"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 508
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_outbound_rejects_bad_body(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        # missing 'to' → 400
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=b'{"message": "x"}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 400
        # thread_id wrong type → 400
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=b'{"to":"a","message":"x","thread_id":123}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 400
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


# -----------------------------------------------------------------------------
# Review-2 P1-D: X-A2A-Request-Id correlation (Hermes sidecar)
# -----------------------------------------------------------------------------


def test_hermes_sidecar_read_or_mint_request_id_accepts_valid_header():
    mod = _load_hermes_sidecar_module()

    class HM:
        def __init__(self, **kw):
            self._kw = kw

        def get(self, k, default=None):
            return self._kw.get(k, default)

    assert mod.read_or_mint_request_id(HM(**{"X-A2A-Request-Id": "abc-42"})) == "abc-42"
    # Surrounding whitespace is trimmed.
    assert mod.read_or_mint_request_id(HM(**{"X-A2A-Request-Id": "  zzz "})) == "zzz"


def test_hermes_sidecar_read_or_mint_request_id_mints_when_missing_or_invalid():
    mod = _load_hermes_sidecar_module()

    class HM:
        def __init__(self, **kw):
            self._kw = kw

        def get(self, k, default=None):
            return self._kw.get(k, default)

    # Missing → minted, non-empty.
    minted = mod.read_or_mint_request_id(HM())
    assert isinstance(minted, str) and len(minted) >= 16
    # Control chars → minted.
    rejected_in = "bad\nvalue"
    minted_again = mod.read_or_mint_request_id(
        HM(**{"X-A2A-Request-Id": rejected_in})
    )
    assert minted_again != rejected_in
    # Oversized → minted.
    huge = "x" * 200
    minted3 = mod.read_or_mint_request_id(HM(**{"X-A2A-Request-Id": huge}))
    assert minted3 != huge
    assert len(minted3) <= 64


def test_hermes_sidecar_forward_to_peer_propagates_request_id_header(monkeypatch):
    mod = _load_hermes_sidecar_module()

    seen = {"header": None}

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            seen["header"] = self.headers.get("X-A2A-Request-Id")
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            body = json.dumps(
                {"from": "peer", "reply": "ok", "thread_id": None}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer_srv, peer_t = _start_http(PeerH)
    peer_host, peer_port = peer_srv.server_address
    try:
        mod.forward_to_peer(
            endpoint=f"http://{peer_host}:{peer_port}/a2a/send",
            self_name="caller",
            peer_name="peer",
            message="hi",
            thread_id=None,
            hop=1,
            timeout=2.0,
            request_id="corr-99",
        )
        assert seen["header"] == "corr-99"
    finally:
        _stop_http(peer_srv, peer_t)


def test_hermes_sidecar_send_echoes_request_id_in_body_and_header(monkeypatch):
    """Caller sends X-A2A-Request-Id; sidecar echoes it in body + response header."""
    mod = _load_hermes_sidecar_module()
    # hop-budget check fires first (508) before any gateway work — we use
    # hop >= budget so the sidecar refuses without needing a real gateway,
    # but still sees the request_id and echoes it.
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_HOP_BUDGET", "2")
    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = json.dumps({"from": "x", "to": cfg.self_name, "message": "y"}).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/send",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-A2A-Hop": "2",
                "X-A2A-Request-Id": "caller-42",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 508
        # Response header carries it back.
        assert exc.value.headers.get("X-A2A-Request-Id") == "caller-42"
        # And the body too so JSON clients can pick it up.
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert payload["request_id"] == "caller-42"
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_outbound_mints_request_id_when_absent(monkeypatch):
    """Outbound with no X-A2A-Request-Id header mints one and returns it."""
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_SELF_NAME", "writer")
    monkeypatch.setenv("A2A_ALLOW_CLIENT_REGISTRY_URL", "1")

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            body = json.dumps(
                {"from": "analyst", "reply": "ok", "thread_id": None}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer_srv, peer_t = _start_http(PeerH)
    peer_host, peer_port = peer_srv.server_address
    registry_srv, registry_t = _start_http(
        _stub_registry(
            {
                "analyst": {
                    "name": "analyst",
                    "role": "hermes",
                    "skills": ["chat"],
                    "endpoint": f"http://{peer_host}:{peer_port}/a2a/send",
                }
            }
        )
    )
    reg_host, reg_port = registry_srv.server_address

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = json.dumps(
            {
                "to": "analyst",
                "message": "ping",
                "registry_url": f"http://{reg_host}:{reg_port}",
                "timeout_ms": 3000,
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            header_id = resp.headers.get("X-A2A-Request-Id")
            out = json.loads(resp.read().decode("utf-8"))
        assert header_id, "response must include X-A2A-Request-Id header"
        assert out["request_id"] == header_id
        # Minted id is opaque but stable: non-empty, >=16 chars, no whitespace.
        assert len(out["request_id"]) >= 16
        assert out["request_id"].strip() == out["request_id"]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)
        _stop_http(peer_srv, peer_t)
        _stop_http(registry_srv, registry_t)


def test_hermes_sidecar_outbound_forwards_caller_request_id_to_peer(monkeypatch):
    """Outbound with a caller-supplied request_id forwards it as X-A2A-Request-Id."""
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_SELF_NAME", "writer")
    monkeypatch.setenv("A2A_ALLOW_CLIENT_REGISTRY_URL", "1")

    seen = {"header": None}

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            seen["header"] = self.headers.get("X-A2A-Request-Id")
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            body = json.dumps(
                {"from": "analyst", "reply": "ok", "thread_id": None}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer_srv, peer_t = _start_http(PeerH)
    peer_host, peer_port = peer_srv.server_address
    registry_srv, registry_t = _start_http(
        _stub_registry(
            {
                "analyst": {
                    "name": "analyst",
                    "role": "hermes",
                    "skills": ["chat"],
                    "endpoint": f"http://{peer_host}:{peer_port}/a2a/send",
                }
            }
        )
    )
    reg_host, reg_port = registry_srv.server_address

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = json.dumps(
            {
                "to": "analyst",
                "message": "ping",
                "registry_url": f"http://{reg_host}:{reg_port}",
                "timeout_ms": 3000,
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-A2A-Request-Id": "caller-777",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            assert resp.headers.get("X-A2A-Request-Id") == "caller-777"
            out = json.loads(resp.read().decode("utf-8"))
        assert out["request_id"] == "caller-777"
        # Peer saw the same ID when the outbound forwarded its request.
        assert seen["header"] == "caller-777"
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)
        _stop_http(peer_srv, peer_t)
        _stop_http(registry_srv, registry_t)


# ---------------------------------------------------------------------------
# Iter-3 (0.3.3) MCP server — a2a-design-3.md §P0-A.
# Exercises handle_mcp_request dispatch + tool-call round-trip + /mcp
# over the full HTTP server.
# ---------------------------------------------------------------------------


def _mcp_deps(mod, **overrides):
    return {
        "self_name": "writer",
        "registry_url": "http://127.0.0.1:9100",
        "timeout": 2.0,
        "request_id": "req-mcp-1",
        "plugin_version": "0.3.3.testsha",
        "lookup_peer_fn": overrides.pop(
            "lookup_peer_fn",
            lambda _reg, _name, _t: {
                "name": "analyst",
                "endpoint": "http://127.0.0.1:9999/a2a/send",
            },
        ),
        "forward_to_peer_fn": overrides.pop(
            "forward_to_peer_fn",
            lambda *_a, **_kw: {"from": "analyst", "reply": "42", "thread_id": None},
        ),
        **overrides,
    }


def test_hermes_mcp_initialize_returns_protocol_version_and_info():
    mod = _load_hermes_sidecar_module()
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    resp = mod.handle_mcp_request(body, **_mcp_deps(mod))
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == mod.MCP_PROTOCOL_VERSION
    assert resp["result"]["serverInfo"] == {
        "name": "clawcu-a2a",
        "version": "0.3.3.testsha",
    }
    assert "tools" in resp["result"]["capabilities"]


def test_hermes_mcp_tools_list_exposes_call_peer_only():
    mod = _load_hermes_sidecar_module()
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    resp = mod.handle_mcp_request(body, **_mcp_deps(mod))
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "a2a_call_peer"
    assert tools[0]["inputSchema"]["required"] == ["to", "message"]


def test_hermes_mcp_tools_call_happy_path():
    mod = _load_hermes_sidecar_module()
    captured: dict = {}

    def fake_lookup(reg, name, timeout):
        captured["lookup"] = (reg, name, timeout)
        return {"name": name, "endpoint": "http://peer/a2a/send"}

    def fake_forward(endpoint, self_name, peer, message, thread_id, hop, timeout, request_id):
        captured["forward"] = {
            "endpoint": endpoint,
            "self_name": self_name,
            "peer": peer,
            "message": message,
            "thread_id": thread_id,
            "hop": hop,
            "request_id": request_id,
        }
        return {"from": "analyst", "reply": "Q1 was +18%", "thread_id": "t-9"}

    body = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "a2a_call_peer",
            "arguments": {"to": "analyst", "message": "Q1?", "thread_id": "t-9"},
        },
    }
    resp = mod.handle_mcp_request(
        body,
        **_mcp_deps(
            mod,
            lookup_peer_fn=fake_lookup,
            forward_to_peer_fn=fake_forward,
        ),
    )
    assert "error" not in resp
    assert resp["result"]["isError"] is False
    assert resp["result"]["content"] == [{"type": "text", "text": "Q1 was +18%"}]
    sc = resp["result"]["structuredContent"]
    assert sc["to"] == "analyst"
    assert sc["reply"] == "Q1 was +18%"
    assert sc["thread_id"] == "t-9"
    assert sc["request_id"] == "req-mcp-1"
    assert captured["lookup"] == ("http://127.0.0.1:9100", "analyst", 2.0)
    assert captured["forward"]["hop"] == 1
    assert captured["forward"]["request_id"] == "req-mcp-1"
    assert captured["forward"]["self_name"] == "writer"


def test_hermes_mcp_tools_call_missing_to_returns_invalid_params():
    mod = _load_hermes_sidecar_module()
    body = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "a2a_call_peer", "arguments": {"message": "hi"}},
    }
    resp = mod.handle_mcp_request(body, **_mcp_deps(mod))
    assert resp["error"]["code"] == mod.MCP_ERR_INVALID_PARAMS


def test_hermes_mcp_tools_call_missing_message_returns_invalid_params():
    mod = _load_hermes_sidecar_module()
    body = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "a2a_call_peer", "arguments": {"to": "analyst"}},
    }
    resp = mod.handle_mcp_request(body, **_mcp_deps(mod))
    assert resp["error"]["code"] == mod.MCP_ERR_INVALID_PARAMS


def test_hermes_mcp_tools_call_unknown_tool_name():
    mod = _load_hermes_sidecar_module()
    body = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    }
    resp = mod.handle_mcp_request(body, **_mcp_deps(mod))
    assert resp["error"]["code"] == mod.MCP_ERR_METHOD_NOT_FOUND


def test_hermes_mcp_tools_call_registry_lookup_failure_surfaces_http_status():
    mod = _load_hermes_sidecar_module()

    def failing_lookup(_reg, _name, _t):
        raise mod.OutboundError(404, "peer 'analyst' not found in registry")

    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "a2a_call_peer",
            "arguments": {"to": "analyst", "message": "hi"},
        },
    }
    resp = mod.handle_mcp_request(
        body, **_mcp_deps(mod, lookup_peer_fn=failing_lookup)
    )
    assert resp["error"]["code"] == mod.MCP_ERR_A2A_UPSTREAM
    assert resp["error"]["data"]["httpStatus"] == 404
    assert "registry lookup failed" in resp["error"]["message"]


def test_hermes_mcp_tools_call_peer_forward_failure_includes_peer_status():
    mod = _load_hermes_sidecar_module()

    def failing_forward(*_a, **_kw):
        raise mod.OutboundError(502, "peer HTTP 500", peer_status=500)

    body = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {
            "name": "a2a_call_peer",
            "arguments": {"to": "analyst", "message": "hi"},
        },
    }
    resp = mod.handle_mcp_request(
        body, **_mcp_deps(mod, forward_to_peer_fn=failing_forward)
    )
    assert resp["error"]["code"] == mod.MCP_ERR_A2A_UPSTREAM
    assert resp["error"]["data"]["httpStatus"] == 502
    assert resp["error"]["data"]["peerStatus"] == 500


def test_hermes_mcp_unknown_method_returns_method_not_found():
    mod = _load_hermes_sidecar_module()
    body = {"jsonrpc": "2.0", "id": 9, "method": "resources/list"}
    resp = mod.handle_mcp_request(body, **_mcp_deps(mod))
    assert resp["error"]["code"] == mod.MCP_ERR_METHOD_NOT_FOUND


def test_hermes_mcp_non_jsonrpc_returns_invalid_request():
    mod = _load_hermes_sidecar_module()
    resp = mod.handle_mcp_request({"method": "initialize"}, **_mcp_deps(mod))
    assert resp["error"]["code"] == mod.MCP_ERR_INVALID_REQUEST


def test_hermes_mcp_ping_acknowledged_with_empty_result():
    mod = _load_hermes_sidecar_module()
    body = {"jsonrpc": "2.0", "id": 10, "method": "ping"}
    resp = mod.handle_mcp_request(body, **_mcp_deps(mod))
    assert "error" not in resp
    assert resp["result"] == {}


# ---------------------------------------------------------------------------
# Iter-3 (0.3.3) P1-C — socket-error status code unification.
#   - Network-layer failures (URLError/timeout/connection refused) → 504.
#   - Peer HTTP errors (HTTPError/non-2xx status) → 502 (unchanged).
# Unifies /a2a/send and /a2a/outbound surface so operators can grep for
# "504" = "network broken" vs "502" = "peer broken".
# ---------------------------------------------------------------------------


def test_hermes_sidecar_forward_to_peer_unreachable_maps_to_504():
    """Connect refused at the peer → OutboundError(504)."""
    mod = _load_hermes_sidecar_module()
    # Bind-then-close a server to grab a truly-unused port.
    probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = probe.server_address[1]
    probe.server_close()
    with pytest.raises(mod.OutboundError) as excinfo:
        mod.forward_to_peer(
            f"http://127.0.0.1:{port}/a2a/send",
            "writer",
            "analyst",
            "hi",
            None,
            1,
            2.0,
        )
    assert excinfo.value.http_status == 504


def test_hermes_sidecar_forward_to_peer_hangs_maps_to_504():
    """Peer accepts the connection but never responds → OutboundError(504)."""
    mod = _load_hermes_sidecar_module()

    class Hang(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            time.sleep(2)  # longer than the caller's timeout

    srv, t = _start_http(Hang)
    try:
        host, port = srv.server_address
        with pytest.raises(mod.OutboundError) as excinfo:
            mod.forward_to_peer(
                f"http://{host}:{port}/a2a/send",
                "writer",
                "analyst",
                "hi",
                None,
                1,
                0.3,
            )
        assert excinfo.value.http_status == 504
    finally:
        _stop_http(srv, t)


def test_hermes_mcp_endpoint_end_to_end(monkeypatch):
    """Boot the handler, POST /mcp tools/call, watch it reach the peer.

    Exercises the actual /mcp HTTP route (not just handle_mcp_request in
    isolation) — so a regression in routing or request-id wiring is
    caught. Mirrors the outbound end-to-end test.
    """
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("A2A_SELF_NAME", "writer-hermes")
    monkeypatch.setenv("API_SERVER_KEY", "k")

    class PeerH(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            body = json.dumps(
                {"from": "analyst", "reply": "mcp ok", "thread_id": None}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    peer_srv, peer_t = _start_http(PeerH)
    peer_host, peer_port = peer_srv.server_address

    registry_srv, registry_t = _start_http(
        _stub_registry(
            {
                "analyst": {
                    "name": "analyst",
                    "role": "hermes",
                    "skills": ["chat"],
                    "endpoint": f"http://{peer_host}:{peer_port}/a2a/send",
                }
            }
        )
    )
    reg_host, reg_port = registry_srv.server_address
    monkeypatch.setenv(
        "A2A_REGISTRY_URL", f"http://{reg_host}:{reg_port}"
    )

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        rpc_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {
                    "name": "a2a_call_peer",
                    "arguments": {"to": "analyst", "message": "tell me"},
                },
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/mcp",
            data=rpc_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-A2A-Request-Id": "corr-mcp-e2e",
            },
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            assert resp.headers.get("X-A2A-Request-Id") == "corr-mcp-e2e"
            out = json.loads(resp.read().decode("utf-8"))
        assert out["jsonrpc"] == "2.0"
        assert out["id"] == 42
        assert out["result"]["content"] == [{"type": "text", "text": "mcp ok"}]
        assert out["result"]["structuredContent"]["request_id"] == "corr-mcp-e2e"
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)
        _stop_http(peer_srv, peer_t)
        _stop_http(registry_srv, registry_t)


def test_hermes_mcp_endpoint_tools_list_end_to_end():
    """Plain tools/list over /mcp — no peer, no registry; verifies route works."""
    mod = _load_hermes_sidecar_module()
    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    try:
        side_host, side_port = sidecar_srv.server_address
        rpc_body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/mcp",
            data=rpc_body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
            out = json.loads(resp.read().decode("utf-8"))
        assert [tool["name"] for tool in out["result"]["tools"]] == [
            "a2a_call_peer"
        ]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


# ---------------------------------------------------------------------------
# iter 4 P0-A — auto-wiring bootstrap for the Hermes sidecar (bootstrap.py).
# ---------------------------------------------------------------------------


def _load_hermes_bootstrap_module():
    """Load the shared bootstrap module (lives in _common/ after the refactor).

    The ``_load_hermes_*`` name is kept for test-history grep-ability even
    though the implementation is now shared between hermes and openclaw.
    """
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "clawcu"
        / "a2a"
        / "sidecar_plugin"
        / "_common"
        / "bootstrap.py"
    )
    spec = importlib.util.spec_from_file_location("_a2a_common_bootstrap_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hermes_bootstrap_build_mcp_url_shape():
    mod = _load_hermes_bootstrap_module()
    assert mod.build_mcp_url(port=9119) == "http://127.0.0.1:9119/mcp"


def test_hermes_bootstrap_plan_merges_when_enabled_and_absent():
    mod = _load_hermes_bootstrap_module()
    plan = mod.plan_bootstrap(
        enabled=True, config={"model": {"provider": "openrouter"}}, url="http://127.0.0.1:9119/mcp"
    )
    assert plan["action"] == "merge"
    assert plan["config"]["mcp"]["servers"]["a2a"] == {"url": "http://127.0.0.1:9119/mcp"}
    assert plan["config"]["model"] == {"provider": "openrouter"}


def test_hermes_bootstrap_plan_noops_when_already_present():
    mod = _load_hermes_bootstrap_module()
    plan = mod.plan_bootstrap(
        enabled=True,
        config={"mcp": {"servers": {"a2a": {"url": "http://127.0.0.1:9119/mcp"}}}},
        url="http://127.0.0.1:9119/mcp",
    )
    assert plan["action"] == "noop"


def test_hermes_bootstrap_plan_rewrites_when_url_differs():
    mod = _load_hermes_bootstrap_module()
    plan = mod.plan_bootstrap(
        enabled=True,
        config={"mcp": {"servers": {"a2a": {"url": "http://127.0.0.1:1/mcp"}}}},
        url="http://127.0.0.1:9119/mcp",
    )
    assert plan["action"] == "merge"
    assert plan["config"]["mcp"]["servers"]["a2a"]["url"] == "http://127.0.0.1:9119/mcp"


def test_hermes_bootstrap_plan_removes_stale_entry_when_disabled():
    mod = _load_hermes_bootstrap_module()
    plan = mod.plan_bootstrap(
        enabled=False,
        config={"mcp": {"servers": {"a2a": {"url": "x"}, "keep": {"url": "y"}}}},
        url=None,
    )
    assert plan["action"] == "remove"
    assert "a2a" not in plan["config"]["mcp"]["servers"]
    assert plan["config"]["mcp"]["servers"]["keep"] == {"url": "y"}


def test_hermes_bootstrap_plan_noop_disabled_and_absent():
    mod = _load_hermes_bootstrap_module()
    plan = mod.plan_bootstrap(
        enabled=False, config={"model": {"provider": "x"}}, url=None
    )
    assert plan["action"] == "noop"


def test_hermes_bootstrap_runs_against_yaml_file(tmp_path):
    mod = _load_hermes_bootstrap_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  provider: openrouter\n", encoding="utf-8")
    result = mod.run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_SERVICE_MCP_CONFIG_FORMAT": "yaml",
            "A2A_ENABLED": "true",
            "A2A_BIND_PORT": "9119",
        }
    )
    assert result["ok"] and result["action"] == "merge"
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["model"]["provider"] == "openrouter"
    assert data["mcp"]["servers"]["a2a"]["url"] == "http://127.0.0.1:9119/mcp"


def test_hermes_bootstrap_creates_yaml_when_absent_and_enabled(tmp_path):
    mod = _load_hermes_bootstrap_module()
    config_path = tmp_path / "nested" / "config.yaml"
    result = mod.run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_SERVICE_MCP_CONFIG_FORMAT": "yaml",
            "A2A_ENABLED": "true",
            "A2A_BIND_PORT": "9119",
        }
    )
    assert result["ok"] and result["action"] == "create"
    assert config_path.exists()


def test_hermes_bootstrap_skips_when_absent_and_disabled(tmp_path):
    mod = _load_hermes_bootstrap_module()
    config_path = tmp_path / "config.yaml"
    result = mod.run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_SERVICE_MCP_CONFIG_FORMAT": "yaml",
            "A2A_ENABLED": "false",
        }
    )
    assert result["action"] == "skip"
    assert not config_path.exists()


def test_hermes_bootstrap_removes_stale_entry_from_yaml(tmp_path):
    mod = _load_hermes_bootstrap_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mcp:\n  servers:\n    a2a:\n      url: http://127.0.0.1:9999/mcp\n    keep:\n      url: y\n",
        encoding="utf-8",
    )
    result = mod.run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_SERVICE_MCP_CONFIG_FORMAT": "yaml",
            "A2A_ENABLED": "false",
        }
    )
    assert result["action"] == "remove"
    import yaml

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "a2a" not in data["mcp"]["servers"]
    assert data["mcp"]["servers"]["keep"] == {"url": "y"}


def test_hermes_bootstrap_skips_on_unset_path():
    mod = _load_hermes_bootstrap_module()
    result = mod.run_bootstrap(env={"A2A_ENABLED": "true", "A2A_BIND_PORT": "9119"})
    assert result["action"] == "skip"
    assert result["reason"] == "no-config-path"


def test_hermes_bootstrap_refuses_to_overwrite_malformed_yaml(tmp_path):
    mod = _load_hermes_bootstrap_module()
    config_path = tmp_path / "config.yaml"
    bad = "foo: [not closed\n"
    config_path.write_text(bad, encoding="utf-8")
    result = mod.run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_SERVICE_MCP_CONFIG_FORMAT": "yaml",
            "A2A_ENABLED": "true",
            "A2A_BIND_PORT": "9119",
        }
    )
    assert result["ok"] is False
    assert config_path.read_text(encoding="utf-8") == bad


def test_hermes_bootstrap_idempotent_across_two_runs(tmp_path):
    mod = _load_hermes_bootstrap_module()
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model: {}\n", encoding="utf-8")
    env = {
        "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
        "A2A_SERVICE_MCP_CONFIG_FORMAT": "yaml",
        "A2A_ENABLED": "true",
        "A2A_BIND_PORT": "9119",
    }
    first = mod.run_bootstrap(env=env)
    second = mod.run_bootstrap(env=env)
    assert first["action"] == "merge"
    assert second["action"] == "noop"


def test_hermes_bootstrap_handles_json_format_roundtrip(tmp_path):
    mod = _load_hermes_bootstrap_module()
    config_path = tmp_path / "config.json"
    config_path.write_text('{"gateway":{"port":18789}}', encoding="utf-8")
    result = mod.run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_SERVICE_MCP_CONFIG_FORMAT": "json",
            "A2A_ENABLED": "true",
            "A2A_BIND_PORT": "9119",
        }
    )
    assert result["ok"] and result["action"] == "merge"
    import json as _json

    data = _json.loads(config_path.read_text(encoding="utf-8"))
    assert data["gateway"]["port"] == 18789
    assert data["mcp"]["servers"]["a2a"]["url"] == "http://127.0.0.1:9119/mcp"


# --- Hermes outbound rate limiter (a2a-design-4.md §P1-B) --------------------

def _load_hermes_outbound_limit_module():
    """Load the shared outbound_limit.py standalone. Historically this loaded
    hermes-specific outbound_limit.py; the implementation has moved to the
    shared ``_common/`` package so both sidecars use one copy."""
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "clawcu"
        / "a2a"
        / "sidecar_plugin"
        / "_common"
        / "outbound_limit.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_hermes_outbound_limit_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec_module so @dataclass can resolve
    # string annotations (PEP 563) via sys.modules[cls.__module__].
    import sys as _sys
    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hermes_outbound_limit_read_rpm_defaults_on_unset_or_invalid():
    mod = _load_hermes_outbound_limit_module()
    assert mod.read_rpm({}) == mod.DEFAULT_RPM
    assert mod.read_rpm({"A2A_OUTBOUND_RATE_LIMIT": ""}) == mod.DEFAULT_RPM
    assert mod.read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "abc"}) == mod.DEFAULT_RPM
    assert mod.read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "-5"}) == mod.DEFAULT_RPM
    assert mod.read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "3.7"}) == mod.DEFAULT_RPM


def test_hermes_outbound_limit_read_rpm_parses_positive_integers():
    mod = _load_hermes_outbound_limit_module()
    assert mod.read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "10"}) == 10
    assert mod.read_rpm({"A2A_OUTBOUND_RATE_LIMIT": "1000"}) == 1000


def test_hermes_outbound_limit_key_prefers_thread_over_self():
    mod = _load_hermes_outbound_limit_module()
    assert mod.key_for(thread_id="t-1", self_name="javis") == "thread:t-1"
    assert mod.key_for(thread_id="", self_name="javis") == "self:javis"
    assert mod.key_for(self_name="javis") == "self:javis"
    assert mod.key_for() == "self:anon"


def test_hermes_outbound_limit_allows_up_to_rpm_then_rejects():
    mod = _load_hermes_outbound_limit_module()
    now = [1000.0]
    lim = mod.create_outbound_limiter(rpm=3, now_fn=lambda: now[0])
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is True
    r = lim.check("k")
    assert r.allowed is False
    assert 0 < r.retry_after_ms <= mod.WINDOW_MS
    assert r.limit == 3


def test_hermes_outbound_limit_prunes_after_window_slides_past():
    mod = _load_hermes_outbound_limit_module()
    now = [1000.0]
    lim = mod.create_outbound_limiter(rpm=2, now_fn=lambda: now[0])
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is True
    assert lim.check("k").allowed is False
    now[0] += mod.WINDOW_MS + 1
    assert lim.check("k").allowed is True


def test_hermes_outbound_limit_buckets_are_per_key():
    mod = _load_hermes_outbound_limit_module()
    now = [1000.0]
    lim = mod.create_outbound_limiter(rpm=1, now_fn=lambda: now[0])
    assert lim.check("thread:a").allowed is True
    assert lim.check("thread:b").allowed is True
    assert lim.check("thread:a").allowed is False
    assert lim.check("thread:b").allowed is False


def test_hermes_outbound_limit_default_rpm_when_no_args():
    mod = _load_hermes_outbound_limit_module()
    lim = mod.create_outbound_limiter()
    assert lim.limit == mod.DEFAULT_RPM


def test_hermes_outbound_limit_reset_clears_buckets():
    mod = _load_hermes_outbound_limit_module()
    now = [1000.0]
    lim = mod.create_outbound_limiter(rpm=1, now_fn=lambda: now[0])
    lim.check("k")
    assert lim.check("k").allowed is False
    lim.reset()
    assert lim.check("k").allowed is True


# -- P1-J: empty-bucket sweep (a2a-design-5.md) ------------------------------


def test_hermes_outbound_limit_sweep_drops_empty_buckets_past_window():
    mod = _load_hermes_outbound_limit_module()
    now = [1000.0]
    lim = mod.create_outbound_limiter(rpm=5, now_fn=lambda: now[0])
    lim.check("a")
    lim.check("b")
    lim.check("c")
    assert lim.size() == 3
    now[0] += mod.WINDOW_MS + 1
    lim.sweep()
    assert lim.size() == 0


def test_hermes_outbound_limit_sweep_leaves_active_buckets_alone():
    mod = _load_hermes_outbound_limit_module()
    now = [1000.0]
    lim = mod.create_outbound_limiter(rpm=5, now_fn=lambda: now[0])
    lim.check("a")
    now[0] += mod.WINDOW_MS + 1
    lim.check("b")
    lim.sweep()
    assert lim.size() == 1


# -- P2-L: sweep timer (a2a-design-6.md) ------------------------------------


def test_hermes_read_sweep_interval_ms_default_and_invalid():
    mod = _load_hermes_outbound_limit_module()
    assert mod.read_sweep_interval_ms({}) == mod.DEFAULT_SWEEP_INTERVAL_MS
    assert mod.read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": ""}) == mod.DEFAULT_SWEEP_INTERVAL_MS
    assert mod.read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "abc"}) == mod.DEFAULT_SWEEP_INTERVAL_MS
    assert mod.read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "1.5"}) == mod.DEFAULT_SWEEP_INTERVAL_MS


def test_hermes_read_sweep_interval_ms_parses_valid_values():
    mod = _load_hermes_outbound_limit_module()
    assert mod.read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "60000"}) == 60000
    assert mod.read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "0"}) == 0
    assert mod.read_sweep_interval_ms({"A2A_OUTBOUND_SWEEP_INTERVAL_MS": "-10"}) == 0


def test_hermes_create_sweep_thread_returns_none_when_disabled():
    mod = _load_hermes_outbound_limit_module()
    lim = mod.create_outbound_limiter(rpm=1)
    assert mod.create_sweep_thread(lim, 0) is None
    assert mod.create_sweep_thread(lim, -5) is None


def test_hermes_create_sweep_thread_fires_and_calls_sweep():
    """Verify the daemon thread actually calls sweep(): populate the limiter,
    advance the monotonic clock used by the limiter past the window, then let
    the sweep thread tick once (short 50 ms interval) and confirm size → 0."""
    import threading
    mod = _load_hermes_outbound_limit_module()
    now = [1000.0]
    lim = mod.create_outbound_limiter(rpm=5, now_fn=lambda: now[0])
    lim.check("a")
    lim.check("b")
    lim.check("c")
    assert lim.size() == 3
    now[0] += mod.WINDOW_MS + 1  # slide past the limiter's window
    stop = threading.Event()
    t = mod.create_sweep_thread(lim, 50, stop_event=stop)
    assert t is not None
    # Poll for the sweep to land; bounded loop so a stuck thread can't hang CI.
    for _ in range(40):  # up to ~2s
        if lim.size() == 0:
            break
        time.sleep(0.05)
    stop.set()
    t.join(timeout=2.0)
    assert lim.size() == 0


def test_hermes_outbound_sweep_logs_on_failure(caplog):
    """a2a-design-7.md §P2-N: when sweep() raises, the daemon thread must
    emit a one-line warning and keep looping (swallowing the exception
    would hide degraded behavior; re-raising would kill cleanup)."""
    import logging as _logging
    import threading
    mod = _load_hermes_outbound_limit_module()

    class _ExplodingLimiter:
        def __init__(self):
            self.calls = 0

        def sweep(self):
            self.calls += 1
            raise RuntimeError("boom from sweep")

    lim = _ExplodingLimiter()
    stop = threading.Event()
    caplog.set_level(_logging.WARNING, logger="clawcu.a2a.outbound_limit")
    t = mod.create_sweep_thread(lim, 50, stop_event=stop)
    assert t is not None
    # Poll for at least one call to land; bounded so a stuck thread can't hang CI.
    for _ in range(40):  # up to ~2s
        if lim.calls >= 1:
            break
        time.sleep(0.05)
    stop.set()
    t.join(timeout=2.0)
    assert lim.calls >= 1, "sweep must have been invoked at least once"
    matching = [
        rec for rec in caplog.records
        if rec.name == "clawcu.a2a.outbound_limit"
        and "outbound-sweep failed" in rec.getMessage()
        and "boom from sweep" in rec.getMessage()
    ]
    assert matching, f"expected a sweep-failure warning; got {[r.getMessage() for r in caplog.records]}"


def test_hermes_mcp_tool_call_rate_limits_after_rpm_and_returns_retry_after():
    """Integration-style: fire rpm+1 MCP tool/call invocations sharing the
    limiter instance and assert the last one comes back as JSON-RPC error with
    httpStatus=429. Covers the /mcp path (a2a-design-4.md §P1-B)."""
    sidecar_mod = _load_hermes_sidecar_module()
    limit_mod = _load_hermes_outbound_limit_module()
    limiter = limit_mod.create_outbound_limiter(rpm=2)

    peer_card = {
        "name": "analyst",
        "endpoint": "http://stub-peer/a2a/send",
        "role": "r",
        "skills": [],
    }

    def _fake_lookup(registry_url, peer, timeout):
        return peer_card

    def _fake_forward(endpoint, self_name, peer_name, message, thread_id, hop, timeout, request_id):
        return {"from": peer_name, "reply": "ok", "thread_id": thread_id}

    def _call():
        return sidecar_mod.handle_mcp_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": sidecar_mod.MCP_TOOL_NAME,
                    "arguments": {"to": "analyst", "message": "hi", "thread_id": "t-lim"},
                },
            },
            self_name="javis",
            registry_url="http://stub-registry",
            timeout=5.0,
            request_id="req-lim",
            plugin_version="test",
            lookup_peer_fn=_fake_lookup,
            forward_to_peer_fn=_fake_forward,
            outbound_limiter=limiter,
        )

    r1 = _call()
    r2 = _call()
    r3 = _call()
    assert "result" in r1 and "result" in r2
    assert "error" in r3
    assert r3["error"]["code"] == sidecar_mod.MCP_ERR_A2A_UPSTREAM
    assert r3["error"]["data"]["httpStatus"] == 429
    assert r3["error"]["data"]["retryAfterMs"] > 0


# --- Hermes MCP templated tool description (a2a-design-5.md §P1-H) ---------

def _mk_mcp_req(rpc_id=1, method="tools/list", params=None):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_hermes_mcp_tools_list_without_list_peers_is_static():
    mod = _load_hermes_sidecar_module()
    resp = mod.handle_mcp_request(
        _mk_mcp_req(method="tools/list"),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "Available peers" not in desc
    assert "A2A registry" in desc


def test_hermes_mcp_tools_list_injects_peers_and_excludes_self():
    mod = _load_hermes_sidecar_module()
    peers = [
        {"name": "javis", "skills": ["chat"]},  # self — filtered
        {"name": "analyst", "skills": ["market data", "charts"]},
        {"name": "editor", "skills": ["copyedit"]},
    ]
    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=lambda: peers,
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "Available peers:" in desc
    assert "- analyst (market data, charts)" in desc
    assert "- editor (copyedit)" in desc
    assert "- javis" not in desc


def test_hermes_mcp_tools_list_truncates_long_peer_list():
    mod = _load_hermes_sidecar_module()
    peers = [{"name": f"p-{i}", "skills": [f"s{i}"]} for i in range(20)]
    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=lambda: peers,
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "- p-0 " in desc
    assert "- p-15 " in desc
    assert "...and 4 more" in desc
    assert "- p-16 " not in desc


def test_hermes_mcp_tools_list_elides_skill_tail():
    mod = _load_hermes_sidecar_module()
    peers = [{"name": "poly", "skills": ["a", "b", "c", "d", "e"]}]
    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=lambda: peers,
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "- poly (a, b, c, ...)" in desc


def test_hermes_mcp_tools_list_survives_list_peers_exception():
    mod = _load_hermes_sidecar_module()

    def _boom():
        raise RuntimeError("registry down")

    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=_boom,
    )
    assert "result" in resp, "tools/list must never fail on registry errors"
    desc = resp["result"]["tools"][0]["description"]
    assert "Available peers" not in desc


def test_hermes_mcp_tools_list_only_self_registered_renders_static():
    mod = _load_hermes_sidecar_module()
    peers = [{"name": "javis", "skills": []}]
    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=lambda: peers,
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "Available peers" not in desc


# --- P1-M: optional role in peer summary (a2a-design-6.md) -----------------


def test_hermes_mcp_tools_list_omits_role_by_default():
    mod = _load_hermes_sidecar_module()
    peers = [{"name": "analyst", "role": "senior market analyst", "skills": ["market data"]}]
    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=lambda: peers,
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "- analyst (market data)" in desc
    assert "[senior market analyst]" not in desc


def test_hermes_mcp_tools_list_renders_role_when_include_role_true():
    mod = _load_hermes_sidecar_module()
    peers = [{"name": "analyst", "role": "senior market analyst", "skills": ["market data"]}]
    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=lambda: peers,
        include_role=True,
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "- analyst [senior market analyst] (market data)" in desc


def test_hermes_mcp_tools_list_include_role_empty_role_omits_brackets():
    mod = _load_hermes_sidecar_module()
    peers = [{"name": "analyst", "role": "", "skills": ["market data"]}]
    resp = mod.handle_mcp_request(
        _mk_mcp_req(),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="r",
        plugin_version="test",
        list_peers_fn=lambda: peers,
        include_role=True,
    )
    desc = resp["result"]["tools"][0]["description"]
    assert "- analyst (market data)" in desc
    assert "[]" not in desc


# --- Hermes peer-list fetcher + cache (a2a-design-5.md §P1-H) --------------

def test_hermes_fetch_peer_list_returns_array_on_ok(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import json as _json

    mod = _load_hermes_sidecar_module()

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            assert self.path == "/agents"
            body = _json.dumps([{"name": "a", "skills": ["s"]}]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **k):  # noqa: N802
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_port
        peers = mod.fetch_peer_list(f"http://127.0.0.1:{port}", timeout=2.0)
        assert peers == [{"name": "a", "skills": ["s"]}]
    finally:
        srv.shutdown()
        srv.server_close()


def test_hermes_fetch_peer_list_returns_none_on_404(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    mod = _load_hermes_sidecar_module()

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a, **k):  # noqa: N802
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_port
        peers = mod.fetch_peer_list(f"http://127.0.0.1:{port}", timeout=2.0)
        assert peers is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_hermes_peer_cache_serves_cached_result_within_fresh_window():
    mod = _load_hermes_sidecar_module()
    calls = [0]
    now = [1000.0]

    def _fake_fetch(url, timeout):
        calls[0] += 1
        return [{"name": "a"}]

    cache = mod.create_peer_cache(
        "http://stub",
        fresh_s=30,
        stale_s=300,
        now_fn=lambda: now[0],
        fetch_fn=_fake_fetch,
    )
    assert cache.get() == [{"name": "a"}]
    assert cache.get() == [{"name": "a"}]
    assert calls[0] == 1


def test_hermes_peer_cache_refetches_after_ttl():
    mod = _load_hermes_sidecar_module()
    calls = [0]
    now = [1000.0]

    def _fake_fetch(url, timeout):
        calls[0] += 1
        return [{"name": "a"}]

    cache = mod.create_peer_cache(
        "http://stub",
        fresh_s=30,
        stale_s=300,
        now_fn=lambda: now[0],
        fetch_fn=_fake_fetch,
    )
    cache.get()
    now[0] += 31.0
    cache.get()
    assert calls[0] == 2


def test_hermes_peer_cache_serves_stale_on_failure_in_stale_window():
    mod = _load_hermes_sidecar_module()
    calls = [0]
    now = [1000.0]

    def _fake_fetch(url, timeout):
        calls[0] += 1
        return [{"name": "a"}] if calls[0] == 1 else None

    cache = mod.create_peer_cache(
        "http://stub",
        fresh_s=30,
        stale_s=300,
        now_fn=lambda: now[0],
        fetch_fn=_fake_fetch,
    )
    assert cache.get() == [{"name": "a"}]
    now[0] += 60.0
    assert cache.get() == [{"name": "a"}]  # stale-OK


def test_hermes_peer_cache_returns_none_past_stale_window():
    mod = _load_hermes_sidecar_module()
    calls = [0]
    now = [1000.0]

    def _fake_fetch(url, timeout):
        calls[0] += 1
        return [{"name": "a"}] if calls[0] == 1 else None

    cache = mod.create_peer_cache(
        "http://stub",
        fresh_s=30,
        stale_s=300,
        now_fn=lambda: now[0],
        fetch_fn=_fake_fetch,
    )
    cache.get()
    now[0] += 400.0
    assert cache.get() is None


# --- Hermes MCP request_id on error data (a2a-design-5.md §P2-K) -----------

def test_hermes_mcp_tool_call_error_carries_request_id_in_data():
    mod = _load_hermes_sidecar_module()

    def _lookup(registry_url, peer, timeout):
        raise mod.OutboundError(404, f"peer '{peer}' not found")

    def _forward(*a, **k):
        raise AssertionError("should not be called")

    resp = mod.handle_mcp_request(
        _mk_mcp_req(
            rpc_id=77,
            method="tools/call",
            params={
                "name": mod.MCP_TOOL_NAME,
                "arguments": {"to": "ghost", "message": "hi"},
            },
        ),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="rid-77",
        plugin_version="test",
        lookup_peer_fn=_lookup,
        forward_to_peer_fn=_forward,
    )
    assert resp["error"]["code"] == mod.MCP_ERR_A2A_UPSTREAM
    assert resp["error"]["data"]["httpStatus"] == 404
    assert resp["error"]["data"]["requestId"] == "rid-77"


def test_hermes_mcp_invalid_params_error_also_carries_request_id():
    mod = _load_hermes_sidecar_module()
    resp = mod.handle_mcp_request(
        _mk_mcp_req(
            rpc_id=78,
            method="tools/call",
            params={"name": mod.MCP_TOOL_NAME, "arguments": {"message": "hi"}},
        ),
        self_name="javis",
        registry_url="http://stub",
        timeout=1.0,
        request_id="rid-78",
        plugin_version="test",
    )
    assert resp["error"]["code"] == mod.MCP_ERR_INVALID_PARAMS
    assert resp["error"]["data"]["requestId"] == "rid-78"


def test_hermes_mcp_tool_call_no_limiter_is_permissive():
    """Without a limiter wired in, the MCP tool call must not error out —
    proves the feature is opt-in via the deps kwarg."""
    sidecar_mod = _load_hermes_sidecar_module()

    def _fake_lookup(registry_url, peer, timeout):
        return {"name": peer, "endpoint": "http://peer/a2a/send", "role": "r", "skills": []}

    def _fake_forward(*a, **k):
        return {"from": "analyst", "reply": "ok", "thread_id": None}

    r = sidecar_mod.handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": sidecar_mod.MCP_TOOL_NAME,
                "arguments": {"to": "analyst", "message": "hi"},
            },
        },
        self_name="javis",
        registry_url="http://stub-registry",
        timeout=5.0,
        request_id="req",
        plugin_version="test",
        lookup_peer_fn=_fake_lookup,
        forward_to_peer_fn=_fake_forward,
    )
    assert "result" in r


# ---------- Iter 11: inbound rate limit / host-endpoint localize / client error ----------


def test_hermes_peer_rate_limiter_allows_below_cap_then_denies():
    """Review-11 P1-B1: ``PeerRateLimiter.allow`` must admit requests up to
    the configured per-minute cap and deny the next one with a positive
    reset window so peers can back off deterministically."""
    sidecar_mod = _load_hermes_sidecar_module()

    now = [1_000_000]  # monotonic-ish ms, advanced explicitly
    limiter = sidecar_mod.PeerRateLimiter(
        per_minute=3, window_ms=60_000, now_fn=lambda: now[0]
    )
    peer = "alpha"
    for _ in range(3):
        d = limiter.allow(peer)
        assert d.ok is True
        now[0] += 1  # 1 ms apart
    denied = limiter.allow(peer)
    assert denied.ok is False
    assert denied.remaining == 0
    # First hit was at 1_000_000, now is 1_000_003 → reset = 60_000 - 3 = 59_997
    assert denied.reset_ms == 59_997
    # After the window rolls past the oldest, traffic flows again.
    now[0] += 60_000
    assert limiter.allow(peer).ok is True


def test_hermes_peer_rate_limiter_zero_disables():
    sidecar_mod = _load_hermes_sidecar_module()
    limiter = sidecar_mod.PeerRateLimiter(per_minute=0)
    # 100 "hits" in a row must all succeed when quota is 0 (disabled).
    for _ in range(100):
        assert limiter.allow("spammy").ok is True


def test_hermes_peer_rate_limiter_is_keyed_by_peer():
    """Review-11 P1-B1: quota is per-peer. One noisy peer must not starve
    others. Proves the hits map isn't bucketed globally."""
    sidecar_mod = _load_hermes_sidecar_module()
    now = [2_000_000]
    limiter = sidecar_mod.PeerRateLimiter(
        per_minute=2, window_ms=60_000, now_fn=lambda: now[0]
    )
    assert limiter.allow("alpha").ok is True
    assert limiter.allow("alpha").ok is True
    assert limiter.allow("alpha").ok is False  # alpha is full
    # beta has its own bucket, fully available.
    assert limiter.allow("beta").ok is True
    assert limiter.allow("beta").ok is True


def test_hermes_peer_rate_limiter_evicts_stalest_at_max_peers():
    """``max_peers`` caps memory so a rotating-name peer can't grow the
    hits map unboundedly. When full, the stalest bucket is evicted; the
    previously-capped peer then re-enters fresh."""
    sidecar_mod = _load_hermes_sidecar_module()
    now = [3_000_000]
    limiter = sidecar_mod.PeerRateLimiter(
        per_minute=1, window_ms=60_000, max_peers=2, now_fn=lambda: now[0]
    )
    # Fill the map with alpha + beta, each at quota.
    assert limiter.allow("alpha").ok is True
    now[0] += 10
    assert limiter.allow("beta").ok is True
    # Bringing in gamma evicts alpha (the stalest).
    now[0] += 10
    assert limiter.allow("gamma").ok is True
    # alpha should now have a fresh bucket; admitted again despite same name.
    assert limiter.allow("alpha").ok is True


def test_hermes_sidecar_post_a2a_send_returns_429_when_peer_over_cap():
    """Review-11 P1-B1 end-to-end: drive ``build_handler`` through the real
    HTTP server with a 1-per-minute quota and assert the second request
    surfaces 429 + Retry-After + resetMs. Exercises the actual code path
    that a container-resident sidecar runs."""
    sidecar_mod = _load_hermes_sidecar_module()

    # Monkeypatch the upstream LLM call so the test doesn't need a gateway.
    def _stub_call_hermes(cfg, message, peer_from, history=None):  # noqa: ARG001
        return "ok"

    orig_call = sidecar_mod.call_hermes
    sidecar_mod.call_hermes = _stub_call_hermes  # type: ignore[assignment]
    orig_wait = sidecar_mod.wait_for_gateway_ready
    sidecar_mod.wait_for_gateway_ready = lambda *a, **k: True  # type: ignore[assignment]
    try:
        cfg = sidecar_mod.Config()
        cfg.rate_limit_per_minute = 1
        handler_cls = sidecar_mod.build_handler(cfg)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            host, port = srv.server_address
            url = f"http://{host}:{port}/a2a/send"
            # First request from peer "flood" should succeed.
            status1, body1 = _http_json(
                url, method="POST", body={"from": "flood", "message": "hi"}
            )
            assert status1 == 200, body1
            # Second request from the same peer within the window must be
            # denied with 429 and a resetMs hint.
            try:
                status2, body2 = _http_json(
                    url, method="POST", body={"from": "flood", "message": "hi2"}
                )
            except urllib.error.HTTPError as exc:
                status2 = exc.code
                body2 = json.loads(exc.read().decode("utf-8"))
            assert status2 == 429
            assert "rate limit exceeded" in body2["error"]
            assert body2["resetMs"] >= 0
            # Different peer should not share the bucket.
            status3, _ = _http_json(
                url, method="POST", body={"from": "calm", "message": "hi"}
            )
            assert status3 == 200
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=2)
    finally:
        sidecar_mod.call_hermes = orig_call  # type: ignore[assignment]
        sidecar_mod.wait_for_gateway_ready = orig_wait  # type: ignore[assignment]


def test_client_localize_endpoint_rewrites_docker_host_to_loopback(monkeypatch):
    """Review-11 P1-C1: the registry advertises container endpoints at
    ``host.docker.internal``; the CLI runs on the host and cannot resolve
    that name. ``localize_endpoint_for_host`` must rewrite to loopback
    while preserving port + path."""
    monkeypatch.delenv("CLAWCU_A2A_HOST_HOSTNAME", raising=False)
    from clawcu.a2a.client import localize_endpoint_for_host

    out = localize_endpoint_for_host("http://host.docker.internal:18850/a2a/send")
    assert out == "http://127.0.0.1:18850/a2a/send"
    # Mixed case still matches (docker hostnames aren't case-sensitive).
    assert (
        localize_endpoint_for_host("http://Host.Docker.Internal:9129/a2a/send")
        == "http://127.0.0.1:9129/a2a/send"
    )
    # gateway.docker.internal is the other container-visible alias.
    assert (
        localize_endpoint_for_host("http://gateway.docker.internal:9100/agents")
        == "http://127.0.0.1:9100/agents"
    )


def test_client_localize_endpoint_passthrough_for_ordinary_host(monkeypatch):
    """Any host that isn't a known docker-only alias passes through
    unchanged. Required so registries that advertise real LAN IPs /
    127.0.0.1 aren't accidentally rewritten."""
    monkeypatch.delenv("CLAWCU_A2A_HOST_HOSTNAME", raising=False)
    from clawcu.a2a.client import localize_endpoint_for_host

    for url in (
        "http://127.0.0.1:18850/a2a/send",
        "http://10.0.0.5:18850/a2a/send",
        "https://agent.example.com/a2a/send",
    ):
        assert localize_endpoint_for_host(url) == url


def test_client_localize_endpoint_honors_env_override(monkeypatch):
    """``CLAWCU_A2A_HOST_HOSTNAME`` lets operators point the CLI at a
    non-loopback replacement (a devcontainer socket, a tailnet hostname,
    etc.). Env wins over the default, still only triggered by a matching
    alias."""
    monkeypatch.setenv("CLAWCU_A2A_HOST_HOSTNAME", "docker.for.mac.localhost")
    from clawcu.a2a.client import localize_endpoint_for_host

    assert (
        localize_endpoint_for_host("http://host.docker.internal:18850/a2a/send")
        == "http://docker.for.mac.localhost:18850/a2a/send"
    )
    # Non-matching host still unmodified despite env being set.
    assert (
        localize_endpoint_for_host("http://127.0.0.1:18850/a2a/send")
        == "http://127.0.0.1:18850/a2a/send"
    )


def test_client_send_via_registry_localizes_before_posting(monkeypatch):
    """End-to-end: stand up a fake registry that advertises
    host.docker.internal + a fake target server bound to 127.0.0.1.
    ``send_via_registry`` must rewrite the endpoint and successfully
    reach the target despite the advertised host being container-only."""
    monkeypatch.delenv("CLAWCU_A2A_HOST_HOSTNAME", raising=False)

    # Target /a2a/send — bound to 127.0.0.1 so host traffic can hit it.
    class _Target(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = b'{"from":"analyst","reply":"pong"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    tgt = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    tgt_thread = threading.Thread(target=tgt.serve_forever, daemon=True)
    tgt_thread.start()
    try:
        tgt_port = tgt.server_address[1]

        # Registry card advertises host.docker.internal, not 127.0.0.1.
        cards = [
            AgentCard(
                name="analyst",
                role="r",
                skills=["chat"],
                endpoint=f"http://host.docker.internal:{tgt_port}/a2a/send",
            )
        ]
        reg, reg_thread = run_registry_in_thread(_registry_provider(cards))
        try:
            reg_host, reg_port = reg.server_address
            reply = send_via_registry(
                registry_url=f"http://{reg_host}:{reg_port}",
                sender="cli",
                target="analyst",
                message="ping",
            )
            assert reply["reply"] == "pong"
        finally:
            reg.shutdown()
            reg.server_close()
            reg_thread.join(timeout=2)
    finally:
        tgt.shutdown()
        tgt.server_close()
        tgt_thread.join(timeout=2)


def test_client_post_message_error_includes_endpoint_and_hint(monkeypatch):
    """Review-11 P2-C2: a failing post_message must surface the endpoint
    URL plus a parsed error hint so CLI operators can triage. Previously
    rendered as 'send failed (502): None' when the upstream returned no
    body."""
    # Case 1: endpoint returns 502 + JSON error body — hint should be the
    # ``error`` field, not a repr.
    class _Upstream502(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = b'{"error":"upstream Hermes HTTP 500: No credentials"}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream502)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        endpoint = f"http://{host}:{port}/a2a/send"
        with pytest.raises(A2AClientError) as exc_info:
            post_message(endpoint, sender="cli", target="x", message="hi")
        msg = str(exc_info.value)
        assert "502" in msg
        assert endpoint in msg
        assert "No credentials" in msg
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_client_post_message_error_includes_endpoint_when_body_empty():
    """Pathological upstream: non-2xx + empty body. Error must still name
    the endpoint and give *some* hint rather than 'None'."""

    class _Upstream500Empty(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream500Empty)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        host, port = srv.server_address
        endpoint = f"http://{host}:{port}/a2a/send"
        with pytest.raises(A2AClientError) as exc_info:
            post_message(endpoint, sender="cli", target="x", message="hi")
        msg = str(exc_info.value)
        assert "500" in msg
        assert endpoint in msg
        assert "empty body" in msg
        # Must never render the useless legacy repr.
        assert "None" not in msg.split("empty body")[0]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


# Review-14 P2-F1: IPv6 replacement hosts must be bracket-wrapped in the
# URL netloc, otherwise urlsplit can't parse the resulting URL back out.
def test_localize_endpoint_ipv6_replacement_is_bracket_wrapped(monkeypatch):
    monkeypatch.setenv("CLAWCU_A2A_HOST_HOSTNAME", "::1")
    out = localize_endpoint_for_host("http://host.docker.internal:9149/a2a/send")
    assert out == "http://[::1]:9149/a2a/send"
    # Round-trip through urlsplit: this would raise or misparse before the fix.
    parsed = urllib.parse.urlsplit(out)
    assert parsed.hostname == "::1"
    assert parsed.port == 9149


def test_localize_endpoint_ipv6_link_local_bracket_wrapped(monkeypatch):
    monkeypatch.setenv("CLAWCU_A2A_HOST_HOSTNAME", "fe80::1")
    out = localize_endpoint_for_host("http://host.docker.internal:1234/a2a/send")
    assert out == "http://[fe80::1]:1234/a2a/send"
    parsed = urllib.parse.urlsplit(out)
    assert parsed.hostname == "fe80::1"
    assert parsed.port == 1234


def test_localize_endpoint_ipv4_replacement_unchanged(monkeypatch):
    # No colon in replacement → no brackets; regression guard on the common path.
    monkeypatch.delenv("CLAWCU_A2A_HOST_HOSTNAME", raising=False)
    out = localize_endpoint_for_host("http://host.docker.internal:9100/a2a/send")
    assert out == "http://127.0.0.1:9100/a2a/send"


# Review-14 P1-F1: hermes sidecar must cap the body size it will read. Before
# this fix, /a2a/send, /a2a/outbound, and /mcp did self.rfile.read(length) with
# no upper bound — an attacker on the LAN (Linux default) could trigger OOM by
# POSTing Content-Length: 10_000_000_000 with a matching body stream.
def test_hermes_sidecar_max_body_bytes_default():
    mod = _load_hermes_sidecar_module()
    assert mod.DEFAULT_MAX_BODY_BYTES == 64 * 1024
    assert mod._max_body_bytes() == 64 * 1024


def test_hermes_sidecar_max_body_bytes_env_override(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("A2A_MAX_BODY_BYTES", "2048")
    assert mod._max_body_bytes() == 2048
    # Invalid values fall back to default.
    monkeypatch.setenv("A2A_MAX_BODY_BYTES", "not-a-number")
    assert mod._max_body_bytes() == 64 * 1024
    monkeypatch.setenv("A2A_MAX_BODY_BYTES", "0")
    assert mod._max_body_bytes() == 64 * 1024


def test_hermes_sidecar_send_rejects_oversized_content_length(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    # Tight cap so a small body trips the check without actually sending MB.
    monkeypatch.setenv("A2A_MAX_BODY_BYTES", "128")

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = b"x" * 256  # over the 128-byte cap
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/send",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 413
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert "exceeds" in payload["error"]
        assert "128" in payload["error"]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_outbound_rejects_oversized_content_length(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_MAX_BODY_BYTES", "128")

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = b"x" * 256
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 413
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert "exceeds" in payload["error"]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


# Review-15 P1-G1: Content-Length that fails to parse or is negative used
# to either drop the connection (non-numeric → uncaught ValueError) or
# wedge the worker thread in `rfile.read(-1)` (negative). Both are DoS
# vectors. Sidecar must now reject these with a proper 400 before reading.


def _raw_socket_post(host: str, port: int, path: str, headers: list[bytes]) -> bytes:
    """POST with explicit header bytes so we can send malformed Content-Length.

    urllib's client validates Content-Length, so we need a raw socket to
    reproduce the exploit shape. Returns the status line (bytes up to \\r\\n)
    or b"" if the server dropped the connection.
    """
    import socket

    sock = socket.socket()
    sock.settimeout(3)
    try:
        sock.connect((host, port))
        req = b"POST " + path.encode() + b" HTTP/1.1\r\nHost: " + host.encode() + b"\r\n"
        req += b"\r\n".join(headers) + b"\r\n\r\n"
        sock.sendall(req)
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        sock.close()


def test_hermes_sidecar_rejects_negative_content_length(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    host, port = sidecar_srv.server_address
    try:
        resp = _raw_socket_post(
            host,
            port,
            "/a2a/send",
            [b"Content-Type: application/json", b"Content-Length: -1"],
        )
        # Must respond with 400 — NOT hang (would TimeoutError before 3s).
        # Must NOT be a 5xx or empty response.
        assert b" 400 " in resp[:32], resp[:200]
        assert b"negative" in resp.lower() or b"content-length" in resp.lower()
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_rejects_non_numeric_content_length(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    host, port = sidecar_srv.server_address
    try:
        resp = _raw_socket_post(
            host,
            port,
            "/a2a/send",
            [b"Content-Type: application/json", b"Content-Length: garbage"],
        )
        assert b" 400 " in resp[:32], resp[:200]
        assert b"invalid" in resp.lower() or b"content-length" in resp.lower()
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_rejects_negative_content_length_on_outbound(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    host, port = sidecar_srv.server_address
    try:
        resp = _raw_socket_post(
            host,
            port,
            "/a2a/outbound",
            [b"Content-Type: application/json", b"Content-Length: -1"],
        )
        assert b" 400 " in resp[:32], resp[:200]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_rejects_negative_content_length_on_mcp(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    host, port = sidecar_srv.server_address
    try:
        resp = _raw_socket_post(
            host,
            port,
            "/mcp",
            [b"Content-Type: application/json", b"Content-Length: -1"],
        )
        assert b" 400 " in resp[:32], resp[:200]
        # /mcp returns JSON-RPC body, so error must be the MCP_ERR_PARSE code.
        assert b'"jsonrpc"' in resp
        assert b"-32700" in resp
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


# Review-16 P1-H1: a slow / incomplete request must not pin the worker
# thread indefinitely. We set a short timeout and confirm the handler
# releases the socket within the bound rather than blocking forever.


def test_hermes_sidecar_slowloris_body_times_out(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    # 1 second so the test finishes quickly.
    monkeypatch.setenv("A2A_INBOUND_REQUEST_TIMEOUT_S", "1")

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    host, port = sidecar_srv.server_address
    try:
        import socket
        import time as _t

        s = socket.socket()
        s.settimeout(5)
        s.connect((host, port))
        # Valid Content-Length but we never send the body.
        s.sendall(
            b"POST /a2a/send HTTP/1.1\r\n"
            b"Host: " + host.encode() + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 100\r\n"
            b"\r\n"
        )
        start = _t.monotonic()
        # Server should close or error within ~1 second (timeout + a
        # small scheduling slack). Without the fix the recv hangs until
        # the 5 s client-side timeout trips.
        try:
            data = s.recv(4096)
        except socket.timeout:
            data = b""
        elapsed = _t.monotonic() - start
        assert elapsed < 4.0, f"handler still blocking after {elapsed:.1f}s"
        s.close()
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_slowloris_headers_times_out(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_INBOUND_REQUEST_TIMEOUT_S", "1")

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    host, port = sidecar_srv.server_address
    try:
        import socket
        import time as _t

        s = socket.socket()
        s.settimeout(5)
        s.connect((host, port))
        # Partial request line, never terminated — BaseHTTPRequestHandler
        # blocks in readline() until the socket closes or times out.
        s.sendall(b"POST /a2a/send HTTP/1.1\r\n")
        start = _t.monotonic()
        try:
            data = s.recv(4096)
        except socket.timeout:
            data = b""
        elapsed = _t.monotonic() - start
        assert elapsed < 4.0, f"handler still blocking after {elapsed:.1f}s"
        s.close()
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_inbound_timeout_zero_disables(monkeypatch):
    # 0 → no timeout applied (for bench / local debug). Helper sanity.
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("A2A_INBOUND_REQUEST_TIMEOUT_S", "0")
    cfg = mod.Config()
    assert cfg.inbound_request_timeout_s == 0.0
    monkeypatch.setenv("A2A_INBOUND_REQUEST_TIMEOUT_S", "-5")
    cfg = mod.Config()
    assert cfg.inbound_request_timeout_s == 0.0
    monkeypatch.setenv("A2A_INBOUND_REQUEST_TIMEOUT_S", "7.5")
    cfg = mod.Config()
    assert cfg.inbound_request_timeout_s == 7.5


def test_hermes_sidecar_parse_content_length_helper():
    mod = _load_hermes_sidecar_module()

    class _H:
        def __init__(self, v):
            self._v = v
        def get(self, name):
            return self._v

    assert mod._parse_content_length(_H(None), cap=1024) == 0
    assert mod._parse_content_length(_H(""), cap=1024) == 0
    assert mod._parse_content_length(_H("  500  "), cap=1024) == 500
    with pytest.raises(mod._BadContentLength, match="negative"):
        mod._parse_content_length(_H("-1"), cap=1024)
    with pytest.raises(mod._BadContentLength, match="invalid"):
        mod._parse_content_length(_H("0x10"), cap=1024)
    with pytest.raises(mod._BadContentLength, match="exceeds"):
        mod._parse_content_length(_H("2048"), cap=1024)


def test_hermes_sidecar_mcp_rejects_oversized_content_length(monkeypatch):
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_MAX_BODY_BYTES", "128")

    cfg = mod.Config()
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = b"x" * 256
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/mcp",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 413
        # /mcp returns JSON-RPC error shape, not a plain {"error": ...}.
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert payload["jsonrpc"] == "2.0"
        assert payload["error"]["code"] == mod.MCP_ERR_PARSE
        assert "exceeds" in payload["error"]["message"]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


# --- iter-17 P1-I1: SSRF via client-supplied registry_url ---------------


def test_validate_outbound_url_allows_http_and_https():
    mod = _load_hermes_sidecar_module()
    # Happy cases.
    assert mod._validate_outbound_url("http://example/") == "http://example/"
    assert mod._validate_outbound_url("https://example/") == "https://example/"
    assert mod._validate_outbound_url("HTTP://Example:9100/agents/x") == (
        "HTTP://Example:9100/agents/x"
    )
    # Reject non-http schemes.
    for url in [
        "file:///etc/passwd",
        "ftp://host/",
        "gopher://host/",
        "dict://host/",
        "javascript:alert(1)",
    ]:
        with pytest.raises(mod._BadOutboundUrl, match="not allowed"):
            mod._validate_outbound_url(url)
    # Reject empty / malformed.
    with pytest.raises(mod._BadOutboundUrl, match="empty"):
        mod._validate_outbound_url("")
    with pytest.raises(mod._BadOutboundUrl, match="missing host"):
        mod._validate_outbound_url("http://")


def test_forward_to_peer_rejects_non_http_endpoint():
    mod = _load_hermes_sidecar_module()
    # Bad scheme → OutboundError(502) before any network call.
    with pytest.raises(mod.OutboundError) as exc:
        mod.forward_to_peer(
            endpoint="ftp://host/",
            self_name="me",
            peer_name="x",
            message="hi",
            thread_id=None,
            hop=1,
            timeout=1.0,
        )
    assert exc.value.http_status == 502
    assert "peer card endpoint rejected" in str(exc.value)


def test_lookup_peer_rejects_non_http_registry():
    mod = _load_hermes_sidecar_module()
    with pytest.raises(mod.OutboundError) as exc:
        mod.lookup_peer("file:///etc/passwd", "x", timeout=1.0)
    assert exc.value.http_status == 400
    assert "invalid registry url" in str(exc.value)


def test_hermes_sidecar_client_registry_url_rejected_by_default(monkeypatch):
    """POST /a2a/outbound with a body registry_url → 400 when flag is off."""
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_SELF_NAME", "writer")
    monkeypatch.delenv("A2A_ALLOW_CLIENT_REGISTRY_URL", raising=False)

    cfg = mod.Config()
    assert cfg.allow_client_registry_url is False
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = json.dumps(
            {
                "to": "analyst",
                "message": "hello",
                "registry_url": "http://attacker.example/",
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 400
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert "disabled by server policy" in payload["error"]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_hermes_sidecar_outbound_rejects_non_http_registry_scheme(monkeypatch):
    """With the flag on, a non-http registry URL still trips the scheme check."""
    mod = _load_hermes_sidecar_module()
    monkeypatch.setenv("API_SERVER_KEY", "k")
    monkeypatch.setenv("A2A_SELF_NAME", "writer")
    monkeypatch.setenv("A2A_ALLOW_CLIENT_REGISTRY_URL", "1")

    cfg = mod.Config()
    assert cfg.allow_client_registry_url is True
    sidecar_srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.build_handler(cfg))
    sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
    sidecar_t.start()
    side_host, side_port = sidecar_srv.server_address
    try:
        body = json.dumps(
            {
                "to": "analyst",
                "message": "hello",
                "registry_url": "file:///etc/passwd",
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{side_host}:{side_port}/a2a/outbound",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=2)
        assert exc.value.code == 400
        payload = json.loads(exc.value.read().decode("utf-8"))
        assert "invalid 'registry_url'" in payload["error"]
        assert "not allowed" in payload["error"]
    finally:
        sidecar_srv.shutdown()
        sidecar_srv.server_close()
        sidecar_t.join(timeout=2)


def test_client_validate_outbound_url_allow_list():
    """Review-19 P2-K1: CLI-side scheme allow-list parallels the hermes
    sidecar's `_validate_outbound_url`. Accept http/https (case-
    insensitive); reject any other scheme, empty input, missing host,
    and malformed URLs."""
    from clawcu.a2a.client import _BadClientUrl, _validate_outbound_url

    # Happy path: http + https, upper / mixed case allowed.
    assert _validate_outbound_url("http://example/") == "http://example/"
    assert _validate_outbound_url("https://example/") == "https://example/"
    assert _validate_outbound_url("HTTP://example/") == "HTTP://example/"
    assert _validate_outbound_url("HttpS://example/path?q=1") == "HttpS://example/path?q=1"

    # Reject smuggle / stdlib-supported non-http schemes.
    for bad in (
        "file:///etc/passwd",
        "ftp://attacker/",
        "gopher://attacker/",
        "dict://attacker/",
        "javascript:alert(1)",
        "data:text/plain,pwn",
    ):
        with pytest.raises(_BadClientUrl):
            _validate_outbound_url(bad)

    # Empty / non-string / missing-host / malformed.
    with pytest.raises(_BadClientUrl):
        _validate_outbound_url("")
    with pytest.raises(_BadClientUrl):
        _validate_outbound_url(None)  # type: ignore[arg-type]
    with pytest.raises(_BadClientUrl):
        _validate_outbound_url("http:///nohost")
    with pytest.raises(_BadClientUrl):
        _validate_outbound_url("nothing-here")

    # _BadClientUrl inherits A2AClientError so call sites keep working.
    assert issubclass(_BadClientUrl, A2AClientError)


def test_client_post_message_rejects_non_http_endpoint():
    """Review-19 P2-K1: post_message must refuse a non-http/https
    endpoint before any urlopen attempt — covers the registry-poisoning
    variant where a malicious card.endpoint would otherwise be POSTed
    to by the CLI."""
    with pytest.raises(A2AClientError) as exc:
        post_message(
            "ftp://attacker.example/drop",
            sender="cli",
            target="analyst",
            message="probe",
        )
    assert "not allowed" in str(exc.value)
    assert "ftp" in str(exc.value)

    with pytest.raises(A2AClientError):
        post_message(
            "file:///etc/passwd",
            sender="cli",
            target="analyst",
            message="probe",
        )


def test_client_send_via_registry_rejects_non_http_card_endpoint():
    """Review-19 P2-K1: if a registry card advertises a non-http scheme
    (the concrete poisoning attack), `send_via_registry` must reject
    the endpoint at the client edge — no file/ftp/etc. POSTs leave the
    CLI."""
    cards = [
        AgentCard(
            name="analyst",
            role="r",
            skills=["chat"],
            endpoint="file:///etc/passwd",
        )
    ]
    reg, reg_thread = run_registry_in_thread(_registry_provider(cards))
    try:
        reg_host, reg_port = reg.server_address
        with pytest.raises(A2AClientError) as exc:
            send_via_registry(
                registry_url=f"http://{reg_host}:{reg_port}",
                sender="cli",
                target="analyst",
                message="ping",
            )
        msg = str(exc.value)
        assert "not allowed" in msg
        assert "file" in msg
    finally:
        reg.shutdown()
        reg.server_close()
        reg_thread.join(timeout=2)


def _redirect_server_to(location: str):
    """Return (server, thread) for a ThreadingHTTPServer that 302s every
    request to `location`. Used to verify no-redirect behavior."""

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def _redirect(self):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            self._redirect()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._redirect()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_client_rejects_redirect_to_non_http_scheme():
    """Review-20 P1-L1: urllib's default redirect handler would follow
    302 → ftp:// and bypass the iter-19 scheme allow-list. The
    `_NoRedirectHandler` opener must surface the 302 as an error
    without issuing a second request against the ftp:// URL."""
    srv, t = _redirect_server_to("ftp://ftp.gnu.org/README")
    try:
        host, port = srv.server_address
        reg_url = f"http://{host}:{port}"
        # lookup_agent (GET) must not follow to ftp://
        with pytest.raises(A2AClientError) as exc:
            lookup_agent(reg_url, "analyst", timeout=2.0)
        # Either "302" in the status, or "ftp" appears only as the
        # rejected Location — never a content leak from ftp.gnu.org.
        assert "GNU" not in str(exc.value)
        # list_agents (GET) must also not follow.
        with pytest.raises(A2AClientError) as exc2:
            list_agents(reg_url, timeout=2.0)
        assert "GNU" not in str(exc2.value)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_client_post_message_does_not_follow_redirect():
    """Review-20 P1-L1: post_message with a target that 302s to ftp://
    must surface the redirect as an error; the CLI must not touch the
    ftp:// URL."""
    srv, t = _redirect_server_to("ftp://ftp.gnu.org/README")
    try:
        host, port = srv.server_address
        endpoint = f"http://{host}:{port}/a2a/send"
        with pytest.raises(A2AClientError) as exc:
            post_message(
                endpoint,
                sender="cli",
                target="analyst",
                message="probe",
                timeout=2.0,
            )
        assert "GNU" not in str(exc.value)
        assert "(302)" in str(exc.value) or "invalid JSON" in str(exc.value)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_hermes_sidecar_lookup_peer_does_not_follow_redirect():
    """Review-20 P1-L1: sidecar's registry lookup must not follow a
    302 → ftp:// redirect. The 3xx response surfaces as OutboundError
    (503) via the existing HTTPError arm."""
    mod = _load_hermes_sidecar_module()
    srv, t = _redirect_server_to("ftp://ftp.gnu.org/README")
    try:
        host, port = srv.server_address
        with pytest.raises(mod.OutboundError) as exc:
            mod.lookup_peer(f"http://{host}:{port}", "analyst", timeout=2.0)
        assert "GNU" not in str(exc.value)
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_forward_to_peer_does_not_follow_redirect():
    """Review-20 P1-L1: sidecar's peer POST must not follow a 302 →
    ftp:// redirect. Surfaces as OutboundError (502) via the existing
    HTTPError arm."""
    mod = _load_hermes_sidecar_module()
    srv, t = _redirect_server_to("ftp://ftp.gnu.org/README")
    try:
        host, port = srv.server_address
        endpoint = f"http://{host}:{port}/a2a/send"
        with pytest.raises(mod.OutboundError) as exc:
            mod.forward_to_peer(
                endpoint=endpoint,
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=1,
                timeout=2.0,
            )
        assert "GNU" not in str(exc.value)
    finally:
        _stop_http(srv, t)


def test_registry_fetch_card_at_does_not_follow_redirect():
    """Review-20 P1-L1: registry's plugin-card probe must not follow
    a 302 → ftp:// redirect. Returns None consistent with every other
    failure path in _fetch_card_at."""
    from clawcu.a2a.registry import _fetch_card_at

    srv, t = _redirect_server_to("ftp://ftp.gnu.org/README")
    try:
        host, port = srv.server_address
        url = f"http://{host}:{port}/.well-known/agent-card.json"
        assert _fetch_card_at(url, timeout=2.0) is None
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Review-21 P2-M1: outbound response size cap. Every outbound call must
# refuse to buffer more than 4 MiB so a compromised registry / peer can't
# OOM the sidecar (or CLI) process.
# ---------------------------------------------------------------------------


def _oversized_body_server(path_prefix: str = "", status: int = 200):
    """Streams ~5 MiB of `a` as the response body (over the 4 MiB cap).

    Accepts any request method. `path_prefix` lets callers route different
    endpoints (e.g. /agents/<name>, /.well-known/agent-card.json) to the
    same oversized-body handler.
    """
    _OVERSIZE = 5 * 1024 * 1024

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def _write_oversized(self):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(_OVERSIZE))
            self.end_headers()
            chunk = b"a" * 65536
            sent = 0
            try:
                while sent < _OVERSIZE:
                    n = min(len(chunk), _OVERSIZE - sent)
                    self.wfile.write(chunk[:n])
                    sent += n
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            self._write_oversized()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._write_oversized()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_client_rejects_oversized_response():
    """Review-21 P2-M1: `lookup_agent` must refuse to buffer a 5 MiB
    response from a compromised registry."""
    srv, t = _oversized_body_server()
    try:
        host, port = srv.server_address
        with pytest.raises(A2AClientError) as exc:
            lookup_agent(f"http://{host}:{port}", "analyst", timeout=5.0)
        assert "too large" in str(exc.value) or "exceeds" in str(exc.value)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_client_post_message_rejects_oversized_response():
    """Review-21 P2-M1: `post_message` must refuse to buffer a 5 MiB
    reply from a compromised peer."""
    srv, t = _oversized_body_server()
    try:
        host, port = srv.server_address
        endpoint = f"http://{host}:{port}/a2a/send"
        with pytest.raises(A2AClientError) as exc:
            post_message(
                endpoint,
                sender="cli",
                target="analyst",
                message="ping",
                timeout=5.0,
            )
        assert "too large" in str(exc.value) or "exceeds" in str(exc.value)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_hermes_sidecar_lookup_peer_rejects_oversized_response():
    """Review-21 P2-M1: `lookup_peer` must fail fast on 5 MiB registry
    payload, surfacing as OutboundError(503, 'too large')."""
    mod = _load_hermes_sidecar_module()
    srv, t = _oversized_body_server()
    try:
        host, port = srv.server_address
        with pytest.raises(mod.OutboundError) as exc:
            mod.lookup_peer(f"http://{host}:{port}", "analyst", timeout=5.0)
        assert "too large" in str(exc.value) or "exceeds" in str(exc.value)
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_forward_to_peer_rejects_oversized_response():
    """Review-21 P2-M1: `forward_to_peer` must fail fast on 5 MiB peer
    reply, surfacing as OutboundError(502, 'too large')."""
    mod = _load_hermes_sidecar_module()
    srv, t = _oversized_body_server()
    try:
        host, port = srv.server_address
        endpoint = f"http://{host}:{port}/a2a/send"
        with pytest.raises(mod.OutboundError) as exc:
            mod.forward_to_peer(
                endpoint=endpoint,
                self_name="writer",
                peer_name="analyst",
                message="hi",
                thread_id=None,
                hop=1,
                timeout=5.0,
            )
        assert "too large" in str(exc.value) or "exceeds" in str(exc.value)
    finally:
        _stop_http(srv, t)


def test_hermes_sidecar_fetch_peer_list_rejects_oversized_response():
    """Review-21 P2-M1: `fetch_peer_list` returns None (consistent with
    other failure paths) on oversized registry response."""
    mod = _load_hermes_sidecar_module()
    srv, t = _oversized_body_server()
    try:
        host, port = srv.server_address
        assert mod.fetch_peer_list(f"http://{host}:{port}", timeout=5.0) is None
    finally:
        _stop_http(srv, t)


def test_registry_fetch_card_at_rejects_oversized_response():
    """Review-21 P2-M1: registry plugin-card probe must return None on
    oversized response rather than buffering into registry memory."""
    from clawcu.a2a.registry import _fetch_card_at

    srv, t = _oversized_body_server()
    try:
        host, port = srv.server_address
        url = f"http://{host}:{port}/.well-known/agent-card.json"
        assert _fetch_card_at(url, timeout=5.0) is None
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Review-22 P2-N1: call_hermes local-upstream response cap
# ---------------------------------------------------------------------------


def _oversized_chat_server():
    """Streams >64 MiB as a fake chat completion (over the local upstream cap).

    Accepts POST, drains the body, then streams the oversized payload.
    """
    _OVERSIZE = 65 * 1024 * 1024

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(_OVERSIZE))
            self.end_headers()
            chunk = b"a" * 65536
            sent = 0
            try:
                while sent < _OVERSIZE:
                    n = min(len(chunk), _OVERSIZE - sent)
                    self.wfile.write(chunk[:n])
                    sent += n
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):  # noqa: N802
            return self.do_POST()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _oversized_error_server():
    """Returns a 500 with >4 KiB error body (over the HTTPError cap)."""

    _OVERSIZE = 8192

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(_OVERSIZE))
            self.end_headers()
            try:
                self.wfile.write(b"e" * _OVERSIZE)
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def test_call_hermes_rejects_oversized_2xx_response(monkeypatch):
    """Review-22 P2-N1: `call_hermes` must refuse to buffer a local
    upstream response larger than ``A2A_LOCAL_UPSTREAM_CAP``."""
    mod = _load_hermes_sidecar_module()

    monkeypatch.setenv("HERMES_API_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_API_PORT", "0")
    monkeypatch.setenv("API_SERVER_KEY", "")

    srv, t = _oversized_chat_server()
    try:
        host, port = srv.server_address
        monkeypatch.setenv("HERMES_API_PORT", str(port))
        cfg = mod.Config()
        with pytest.raises(mod._ResponseTooLarge, match="exceeds"):
            mod.call_hermes(cfg, "ping", "test-peer")
    finally:
        _stop_http(srv, t)


def test_call_hermes_rejects_oversized_http_error_body(monkeypatch):
    """Review-22 P2-N1: the HTTPError branch in /a2a/send must cap the
    upstream error body at 4 KiB rather than unbounded ``e.read()``."""
    mod = _load_hermes_sidecar_module()

    monkeypatch.setenv("HERMES_API_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_API_PORT", "0")
    monkeypatch.setenv("API_SERVER_KEY", "")

    srv, t = _oversized_error_server()
    try:
        host, port = srv.server_address
        monkeypatch.setenv("HERMES_API_PORT", str(port))
        cfg = mod.Config()

        # Pre-warm the gateway readiness cache so the sidecar doesn't
        # block for 30 s probing a /health that doesn't exist.
        mod._GATEWAY_READY_UNTIL = time.time() + 300

        from http.server import ThreadingHTTPServer as _ThSrv

        handler = mod.build_handler(cfg)
        sidecar_srv = _ThSrv(("127.0.0.1", 0), handler)
        sidecar_t = threading.Thread(target=sidecar_srv.serve_forever, daemon=True)
        sidecar_t.start()
        try:
            shost, sport = sidecar_srv.server_address
            import urllib.request
            import urllib.error

            body = json.dumps(
                {"from": "tester", "message": "hi", "thread_id": None}
            ).encode()
            req = urllib.request.Request(
                f"http://{shost}:{sport}/a2a/send",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as resp:
                # Should get 502 with a capped error body, not hang/OOM.
                assert resp.code == 502
                raw = resp.read()
                assert b"upstream" in raw.lower()
        finally:
            sidecar_srv.shutdown()
            sidecar_srv.server_close()
            sidecar_t.join(timeout=2)
    finally:
        _stop_http(srv, t)

