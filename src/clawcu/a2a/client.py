from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from clawcu.a2a.card import AgentCard
from clawcu.a2a.sidecar_plugin._common import streams as _streams

DEFAULT_TIMEOUT = 5.0
# Library default: generous cap for long agent turns (tool use + big LLM
# responses can push past a minute). Integration callers that know their
# workload should leave this alone.
DEFAULT_SEND_TIMEOUT = 300.0
# CLI default: review-1 §4 / review-2 §5 — an interactive `clawcu a2a send`
# user with no progress spinner should not silently wait five minutes for a
# peer that is already gone. Long-running turns are opt-in via ``--timeout``.
DEFAULT_CLI_SEND_TIMEOUT = 60.0

_log = logging.getLogger("clawcu.a2a.client")

# Review-11 P1-C1: the registry federates sidecar endpoints using the
# container-advertise host (`host.docker.internal` on Darwin so
# container→container hops resolve). When the clawcu CLI running on the
# host reaches a card via `send_via_registry`, that hostname does not
# resolve from the host itself on macOS/Linux (it is a docker-only name).
# Rewrite it to a loopback literal so the CLI path works without asking
# the operator to add a hosts-file entry.
_CONTAINER_HOSTNAME_ALIASES = frozenset({"host.docker.internal", "gateway.docker.internal"})

# RFC 1123 hostname: labels of letters/digits/hyphens, labels don't start
# or end with a hyphen, labels ≤63 chars, whole name ≤253. We accept
# unqualified names (no dot) — matches existing test fixtures like
# ``docker.for.mac.localhost`` and bare ``localhost``.
_HOSTNAME_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_PATTERN = re.compile(rf"^{_HOSTNAME_LABEL}(\.{_HOSTNAME_LABEL})*$")


def _is_valid_host_literal(raw: str) -> bool:
    """Accept only a bare IP literal or RFC-1123 hostname.

    Review-1 §12: ``CLAWCU_A2A_HOST_HOSTNAME`` was consumed with only a
    ``.strip()`` — ``http://evil/`` or ``127.0.0.1:8080/path`` would be
    spliced into the rewritten URL's netloc and yield garbage like
    ``http://http://evil/:9100/a2a/send``. We guard the one-way flow into
    ``localize_endpoint_for_host`` by accepting only values that could
    safely live as a bare host token.
    """
    if not raw or len(raw) > 253:
        return False
    try:
        ipaddress.ip_address(raw)
        return True
    except ValueError:
        pass
    return _HOSTNAME_PATTERN.match(raw) is not None


def _host_localize_env_override() -> str | None:
    raw = os.environ.get("CLAWCU_A2A_HOST_HOSTNAME")
    if not (isinstance(raw, str) and raw.strip()):
        return None
    cleaned = raw.strip()
    if not _is_valid_host_literal(cleaned):
        _log.warning(
            "CLAWCU_A2A_HOST_HOSTNAME=%r is not a valid IP literal or hostname; "
            "falling back to 127.0.0.1",
            cleaned,
        )
        return None
    return cleaned


def localize_endpoint_for_host(endpoint: str) -> str:
    """Rewrite a container-advertised endpoint so the host CLI can reach it.

    The registry stores endpoints using the container-visible hostname
    (e.g. ``host.docker.internal``); that name is not resolvable from the
    host loopback itself. Replace the host component with ``127.0.0.1``
    (override via ``CLAWCU_A2A_HOST_HOSTNAME``) when it matches a known
    container-only alias. All other hostnames pass through unchanged so
    a registry that already serves loopback / LAN endpoints still works.
    """
    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return endpoint
    host = (parsed.hostname or "").lower()
    if host not in _CONTAINER_HOSTNAME_ALIASES:
        return endpoint
    replacement = _host_localize_env_override() or "127.0.0.1"
    # IPv6 literals must be bracket-wrapped in a URL netloc. Detect by
    # presence of ':' in the replacement (unambiguous — hostnames and
    # IPv4 literals never contain ':').
    host_token = f"[{replacement}]" if ":" in replacement else replacement
    netloc = host_token
    if parsed.port is not None:
        netloc = f"{host_token}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


class A2AClientError(RuntimeError):
    pass


class _BadClientUrl(A2AClientError):
    """Raised when an outbound URL fails the client-side scheme allow-list.

    Review-19 P2-K1: defense-in-depth parity with the iter-17/18 sidecar
    fix. `post_message` accepts an ``endpoint`` field that flows in from
    registry card data; if the registry is poisoned (or a sidecar's
    ``A2A_SELF_ENDPOINT`` env was tampered), the CLI could be directed
    to POST its sender/message body to any stdlib-urllib-supported URL
    scheme (http/https/ftp). Gate with an http/https allow-list so the
    CLI and the sidecar behave identically under adversarial registry
    input.
    """


_ALLOWED_OUTBOUND_SCHEMES = frozenset({"http", "https"})


def _validate_outbound_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        raise _BadClientUrl("empty url")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise _BadClientUrl(f"malformed url: {exc}") from exc
    if parsed.scheme.lower() not in _ALLOWED_OUTBOUND_SCHEMES:
        raise _BadClientUrl(
            f"scheme {parsed.scheme!r} not allowed (only http/https)"
        )
    if not parsed.hostname:
        raise _BadClientUrl("missing host")
    return url


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return None from redirect_request so urllib surfaces 3xx as an
    HTTPError instead of following.

    Review-20 P1-L1: CPython's default ``HTTPRedirectHandler`` admits
    redirects into ``{"http", "https", "ftp", ""}``. Our iter-19
    ``_validate_outbound_url`` only gates the URL passed into
    ``urlopen``, so a peer/registry returning ``302 Location:
    ftp://attacker/`` would bypass the allow-list by redirecting into
    ftp:// from within urlopen. Short-circuiting the redirect chain
    here keeps the CLI pinned to the operator-trusted URL; the 3xx
    response surfaces through the existing HTTPError handling in
    ``_http_json`` as ``send failed (302) at <url>: <hint>``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


A2A_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _ResponseTooLarge(A2AClientError):
    """Raised when an outbound response exceeds ``A2A_MAX_RESPONSE_BYTES``.

    Review-21 P2-M1: ``resp.read()`` with no byte bound let a compromised
    registry or peer stream GBs into the CLI process (loopback reads
    ~3 GB/s; 30 s of budget is ~90 GB) and OOM it before the timeout
    fires. The cap is deliberately a compile-time constant so an
    attacker who can flip env vars cannot widen it.

    Subclasses ``A2AClientError`` (not the neutral ``_streams.ResponseTooLarge``)
    so the CLI's ``except A2AClientError`` arm still catches a cap violation
    and renders a clean error instead of dumping a traceback.
    """


def _read_capped(response, cap: int = A2A_MAX_RESPONSE_BYTES) -> bytes:
    # Batch 24: delegate to the shared chunked reader so a peer claiming
    # ``Content-Length: 10GB`` aborts after the first 64 KiB chunk instead
    # of pre-allocating ``cap+1`` bytes in one shot. Translate the neutral
    # ``streams.ResponseTooLarge`` to ``_ResponseTooLarge`` so the
    # ``A2AClientError`` inheritance — and therefore the CLI catch arm —
    # is preserved.
    try:
        return _streams.read_capped_bytes(response, cap=cap)
    except _streams.ResponseTooLarge as exc:
        raise _ResponseTooLarge(str(exc)) from exc


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    token: str | None = None,
) -> tuple[int, Any]:
    _validate_outbound_url(url)
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = _read_capped(response)
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = _read_capped(exc)
        status = exc.code
    except urllib.error.URLError as exc:
        raise A2AClientError(f"request failed: {url}: {exc.reason}") from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise A2AClientError(f"invalid JSON from {url}: {exc}") from exc


def _registry_token_from_env() -> str | None:
    """Read the optional ``A2A_REGISTRY_TOKEN`` so client and registry pair
    off the same env var. ``None`` means "no auth header" (default)."""
    raw = os.environ.get("A2A_REGISTRY_TOKEN")
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def lookup_agent(
    registry_url: str,
    name: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    token: str | None = None,
) -> AgentCard:
    base = registry_url.rstrip("/")
    url = f"{base}/agents/{urllib.parse.quote(name, safe='')}"
    status, payload = _http_json(
        url, timeout=timeout, token=token if token is not None else _registry_token_from_env()
    )
    if status == 404:
        raise A2AClientError(f"agent '{name}' not found in registry {registry_url}")
    if status >= 400 or not isinstance(payload, dict):
        raise A2AClientError(f"registry lookup failed ({status}): {payload!r}")
    return AgentCard.from_dict(payload)


def list_agents(
    registry_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    token: str | None = None,
) -> list[AgentCard]:
    base = registry_url.rstrip("/")
    url = f"{base}/agents"
    status, payload = _http_json(
        url, timeout=timeout, token=token if token is not None else _registry_token_from_env()
    )
    if status >= 400 or not isinstance(payload, list):
        raise A2AClientError(f"registry list failed ({status}): {payload!r}")
    return [AgentCard.from_dict(item) for item in payload]


def _summarize_error_payload(payload: Any) -> str:
    """Produce a short human-readable hint from an error-response body.

    ``post_message``'s failure message used to be ``f"({status}): {payload!r}"``
    which rendered as ``(502): None`` when the upstream returned an empty
    body or non-dict JSON — useless for an operator. Preserve the known
    ``error`` field when present (sidecar-shaped body), otherwise fall back
    to a truncated repr so non-dict replies still produce *some* signal.
    """
    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        return json.dumps(payload, ensure_ascii=False)[:200]
    if payload is None:
        return "empty body"
    if isinstance(payload, str):
        return payload[:200] or "empty string"
    return repr(payload)[:200]


def post_message(
    endpoint: str,
    *,
    sender: str,
    target: str,
    message: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    body = {"from": sender, "to": target, "message": message}
    status, payload = _http_json(endpoint, method="POST", body=body, timeout=timeout)
    if status >= 400 or not isinstance(payload, dict):
        # Review-11 P2-C2: the endpoint URL + a parsed hint make a CLI
        # failure actionable (the old format rendered as "send failed (502):
        # None" when an upstream returned no body). Keep the status so
        # callers can still string-match for test purposes.
        hint = _summarize_error_payload(payload)
        raise A2AClientError(f"send failed ({status}) at {endpoint}: {hint}")
    return payload


def send_via_registry(
    *,
    registry_url: str,
    sender: str,
    target: str,
    message: str,
    lookup_timeout: float = DEFAULT_TIMEOUT,
    send_timeout: float = DEFAULT_SEND_TIMEOUT,
) -> dict[str, Any]:
    card = lookup_agent(registry_url, target, timeout=lookup_timeout)
    # Review-11 P1-C1: registry endpoints use the container-advertise host
    # so peer sidecars can reach each other through docker DNS. The CLI
    # runs on the host and that hostname doesn't resolve there; rewrite it
    # to loopback (override via CLAWCU_A2A_HOST_HOSTNAME) so `clawcu a2a
    # send` just works. No-op for any endpoint that doesn't match a known
    # container-only alias.
    endpoint = localize_endpoint_for_host(card.endpoint)
    return post_message(
        endpoint,
        sender=sender,
        target=target,
        message=message,
        timeout=send_timeout,
    )
