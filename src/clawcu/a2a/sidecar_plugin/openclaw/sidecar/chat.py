"""OpenClaw sidecar chat-completions client.

Named ``chat`` rather than ``gateway`` because hermes/sidecar/gateway.py
already owns that module name on ``sys.path`` — both sidecars bootstrap
by prepending their own directory, so shared module names would collide
in tests that import both in one process. The hermes ``gateway`` module
is about *serving* the co-resident gateway surface; this module is about
*calling* the co-resident OpenClaw gateway, so the name difference also
reflects the different direction of flow.

The sidecar's core routing path forwards ``/a2a/send`` to the OpenClaw
gateway's own OpenAI-compatible endpoint at ``/v1/chat/completions`` —
so an A2A peer gets the agent's "native" reply (persona, skills, tools,
provider) rather than a bare LLM completion. Two helpers make that
split clean:

* :func:`post_chat_completion` — POSTs an OpenAI-format
  ``{messages, model, stream=false}`` body, handles
  ``Authorization: Bearer <token>``, and extracts
  ``choices[0].message.content`` from the response. Raises
  :class:`RuntimeError` on non-200 / non-json / empty-content. The
  caller turns those into the appropriate A2A error surface.
* :func:`build_a2a_context` — the system-prompt preamble that tells
  the agent it is being addressed by a peer over the A2A bridge, so
  the reply stays on-topic and doesn't re-introduce the agent's name.

Lifted out of ``server.py`` so the gateway-facing concern lives beside
:mod:`outbound` (peer/registry calls) and :mod:`http_client` (the HTTP
primitive they both sit on). ``server.py`` re-exports the names so any
caller that used ``sidecar.post_chat_completion`` keeps working.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

from http_client import post_json, _connection_for, A2A_MAX_RESPONSE_BYTES


def post_chat_completion(
    gateway_host: str,
    gateway_port: int,
    token: Optional[str],
    user_message: str,
    system_prompt: Optional[str],
    history: list,
    model: str,
    timeout_ms: int,
) -> str:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})
    payload = {
        "model": model or "openclaw",
        "stream": False,
        "messages": messages,
    }
    headers = {"authorization": f"Bearer {token}"} if token else {}
    resp = post_json(
        host=gateway_host,
        port=gateway_port,
        path="/v1/chat/completions",
        headers=headers,
        body_obj=payload,
        timeout_ms=timeout_ms,
    )
    status = resp["status"]
    body = resp["body"]
    if status != 200:
        raise RuntimeError(f"gateway /v1/chat/completions {status}: {body[:400]}")
    try:
        parsed = json.loads(body)
    except Exception as exc:
        raise RuntimeError(f"gateway returned non-json: {exc}")
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    content = None
    if isinstance(choice, dict):
        msg = choice.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
    if not isinstance(content, str) or not content:
        raise RuntimeError(f"gateway returned empty content: {body[:400]}")
    return content


def post_chat_completion_streaming(
    gateway_host: str,
    gateway_port: int,
    token: Optional[str],
    user_message: str,
    system_prompt: Optional[str],
    history: list,
    model: str,
    timeout_ms: int,
    *,
    progress: Callable[[str], None],
    progress_interval_s: float = 3.0,
    response_cap: int = A2A_MAX_RESPONSE_BYTES,
) -> str:
    """Streaming variant of :func:`post_chat_completion`.

    Uses ``stream: true`` and reads the SSE response chunk-by-chunk so the
    caller can surface live progress (a2a-async layer 3). Calls
    ``progress("streaming: N chars · …<tail>")`` no more often than every
    ``progress_interval_s`` seconds. Returns the concatenated assistant text.

    A separate function — rather than a flag on :func:`post_chat_completion`
    — because the streaming path uses a manually-managed HTTP connection
    (so it doesn't fully read the body before returning), while the original
    call goes through ``post_json``'s capped-read shortcut.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})
    body = json.dumps(
        {
            "model": model or "openclaw",
            "stream": True,
            "messages": messages,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "content-length": str(len(body)),
        "user-agent": "a2a-bridge-sidecar/0.3",
        "accept": "text/event-stream",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"

    conn = _connection_for(gateway_host, gateway_port, timeout_ms / 1000.0)
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status or 0
        if status != 200:
            preview = resp.read(400).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"gateway /v1/chat/completions {status}: {preview}"
            )

        chunks: list[str] = []
        total = 0
        last_emit = time.monotonic()
        for raw_line in resp:
            line = raw_line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            data = line[len(b"data:"):].strip()
            if data == b"[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:
                continue
            try:
                content = event["choices"][0]["delta"].get("content")
            except (KeyError, IndexError, TypeError):
                content = None
            if not isinstance(content, str) or not content:
                continue
            chunks.append(content)
            total += len(content)
            if total > response_cap:
                raise RuntimeError(
                    f"streaming response exceeded {response_cap} bytes"
                )
            now = time.monotonic()
            if now - last_emit >= progress_interval_s:
                _emit_stream_progress(progress, chunks, total)
                last_emit = now

        full = "".join(chunks)
        if not full:
            raise RuntimeError("gateway streamed empty content")
        return full
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _emit_stream_progress(
    progress: Callable[[str], None],
    chunks: list,
    total: int,
) -> None:
    tail = "".join(chunks)[-60:].replace("\n", " ").replace("\r", " ").strip()
    note = f"streaming: {total} chars · …{tail}" if tail else f"streaming: {total} chars"
    try:
        progress(note[:200])
    except Exception:
        pass


def build_a2a_context(self_name: str, from_agent: str) -> str:
    return (
        f'You are being addressed by a peer agent named "{from_agent}" '
        f'over the A2A bridge as "{self_name}". Respond in plain text, '
        f"preserving your own persona and skills. Keep the reply focused on "
        f"the peer's request; do not prefix with your own name."
    )
