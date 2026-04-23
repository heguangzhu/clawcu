"""OpenClaw sidecar outbound-HTTP client helpers.

Centralises the sidecar's outgoing HTTP primitives so the handler code
above can stay focused on A2A protocol dispatch:

* :func:`parse_http_url` — stdlib ``urlparse`` wrapper that normalises
  scheme / host / port / pathname / search and raises ``RuntimeError``
  on unsupported schemes (anything that's not http/https), missing host,
  or parse errors. Callers get a flat dict back rather than a tuple so
  they can ``resp["host"]`` without remembering positions.
* :func:`_connection_for` — picks ``HTTPConnection`` vs
  ``HTTPSConnection`` with a timeout in seconds.
* :attr:`ResponseTooLarge` + :func:`_read_capped` — thin aliases over
  :mod:`_common.streams` so cap overflow has the same class across
  sidecars and both return ``str`` (the gateway and registry send
  JSON).
* :func:`_http_call` — the shared connect/request/read/close skeleton
  with uniform ``socket.timeout`` / ``ResponseTooLarge`` →
  ``RuntimeError`` translation.
* :func:`post_json` + :func:`http_request_raw` — thin public wrappers
  layered on ``_http_call``. ``post_json`` serialises a JSON body and
  adds the content headers; ``http_request_raw`` passes the method
  through for GET/etc.

Lifted out of ``server.py`` so the outbound-HTTP concern has its own
file; server.py re-imports the names so the public surface tests rely
on (``mod.post_json`` / ``mod.ResponseTooLarge`` / ``mod.parse_http_url``)
stays unchanged.
"""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from _common import streams as _streams

A2A_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def parse_http_url(url: str) -> Dict[str, Any]:
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise RuntimeError(f"invalid url '{url}': {exc}")
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(f"unsupported protocol in '{url}'")
    if not parsed.hostname:
        raise RuntimeError(f"invalid url '{url}': missing host")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    pathname = parsed.path or "/"
    search = f"?{parsed.query}" if parsed.query else ""
    return {
        "host": parsed.hostname,
        "port": int(port),
        "pathname": pathname,
        "search": search,
        "scheme": parsed.scheme,
    }


def _connection_for(host: str, port: int, timeout_s: float, scheme: str = "http"):
    if scheme == "https":
        return http.client.HTTPSConnection(host=host, port=port, timeout=timeout_s)
    return http.client.HTTPConnection(host=host, port=port, timeout=timeout_s)


# Reader + exception live in _common/streams.py so both sidecars share one
# implementation. ``_read_capped`` keeps its str-returning shape so existing
# callers (``raw = _read_capped(resp); json.loads(raw)``) need no change.
ResponseTooLarge = _streams.ResponseTooLarge


def _read_capped(resp, limit: int = A2A_MAX_RESPONSE_BYTES) -> str:
    return _streams.read_capped_text(resp, cap=limit)


def _http_call(
    *,
    method: str,
    host: str,
    port: int,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout_ms: int,
    scheme: str = "http",
) -> Dict[str, Any]:
    """Shared connect/request/read/close skeleton for outbound HTTP.

    Both ``post_json`` and ``http_request_raw`` were 30-line near-identical
    copies of this shape (connect → request → capped-read → timeout/cap
    translation → close). The single difference — whether the caller sends
    a serialized body — is expressed here as an optional ``body`` argument.
    Exception translation (``ResponseTooLarge`` → ``RuntimeError``,
    ``socket.timeout`` → ``RuntimeError``) lives in one place so the two
    public wrappers stay thin.
    """
    conn = _connection_for(host, port, timeout_ms / 1000.0, scheme=scheme)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        status = resp.status or 0
        try:
            raw = _read_capped(resp)
        except ResponseTooLarge as exc:
            raise RuntimeError(str(exc))
        return {"status": status, "body": raw}
    except socket.timeout:
        raise RuntimeError(f"request timed out after {timeout_ms}ms")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def post_json(
    host: str,
    port: int,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    body_obj=None,
    timeout_ms: int = 300000,
    scheme: str = "http",
) -> Dict[str, Any]:
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    merged = {
        "content-type": "application/json",
        "content-length": str(len(body)),
        "user-agent": "a2a-bridge-sidecar/0.3",
    }
    if headers:
        merged.update(headers)
    return _http_call(
        method="POST",
        host=host,
        port=port,
        path=path,
        headers=merged,
        body=body,
        timeout_ms=timeout_ms,
        scheme=scheme,
    )


def http_request_raw(
    method: str,
    host: str,
    port: int,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    timeout_ms: int = 30000,
    scheme: str = "http",
) -> Dict[str, Any]:
    return _http_call(
        method=method,
        host=host,
        port=port,
        path=path,
        headers=headers,
        timeout_ms=timeout_ms,
        scheme=scheme,
    )
