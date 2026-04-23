"""OpenClaw sidecar config-source adapters.

The sidecar runs in two shapes depending on how it was launched:

* ``--instance`` mode (default in the Docker-packaged plugin): the
  gateway lives inside a sibling container; adapters read
  ``openclaw.json`` / ``auth.json`` via ``docker exec cat`` on that
  container, and look up container-side env vars via
  ``docker exec printenv``. That is :class:`HostAdapter`.

* ``--local`` mode (used by integration tests and by developers running
  the sidecar next to a host-installed gateway): the same files live on
  the local filesystem and env vars live in ``os.environ``. That is
  :class:`LocalAdapter`.

Both adapters share a two-method interface — ``read_file(path)`` and
``get_env(name)`` — so downstream helpers like :func:`read_gateway_auth`
don't need to know which mode they're running in.

Lifted out of ``server.py`` so the config-source concern has its own
file. ``server.py`` re-imports the names so existing callers (and tests
that poke at ``mod.HostAdapter`` / ``mod.OPENCLAW_CONFIG_PATH``) keep
working unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict

OPENCLAW_CONFIG_PATH = "/home/node/.openclaw/openclaw.json"
OPENCLAW_AUTH_PATH = "/home/node/.openclaw/auth.json"


class HostAdapter:
    mode = "host"

    def __init__(self, container: str) -> None:
        self.container = container

    def _exec(self, args):
        try:
            res = subprocess.run(
                ["docker", "exec", self.container, *args],
                capture_output=True,
                check=False,
                text=True,
            )
            if res.returncode != 0:
                return None
            return res.stdout
        except Exception:
            return None

    def read_file(self, path: str):
        return self._exec(["cat", path])

    def get_env(self, name: str):
        v = self._exec(["printenv", name])
        if v is None:
            return None
        v = v.strip()
        return v or None


class LocalAdapter:
    mode = "local"

    def read_file(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def get_env(self, name: str):
        v = os.environ.get(name)
        return v or None


def make_host_adapter(container: str) -> HostAdapter:
    return HostAdapter(container)


def make_local_adapter() -> LocalAdapter:
    return LocalAdapter()


def read_gateway_auth(adapter) -> Dict[str, Any]:
    """Resolve the gateway's bearer token via the adapter.

    Reads ``openclaw.json`` first — that's the canonical source for
    ``gateway.auth.{mode,token}``. If the config declares
    ``mode == "token"`` but the token field is empty (older installs
    wrote only ``auth.json``), falls back to ``auth.json`` and accepts
    either nested ``gateway.auth.token`` or a flat top-level ``token``.
    Raises :class:`RuntimeError` if neither path yields a token for
    token mode — the caller turns that into a 503 so the peer sees a
    clean "gateway not configured" signal.
    """
    raw = adapter.read_file(OPENCLAW_CONFIG_PATH)
    if not raw:
        raise RuntimeError("could not read openclaw.json")
    cfg = json.loads(raw)
    gateway_cfg = cfg.get("gateway") if isinstance(cfg, dict) else None
    auth_cfg = gateway_cfg.get("auth") if isinstance(gateway_cfg, dict) else None
    auth_mode = "token"
    token = None
    if isinstance(auth_cfg, dict):
        auth_mode = auth_cfg.get("mode") or "token"
        token = auth_cfg.get("token")
    if auth_mode == "token" and not token:
        auth_raw = adapter.read_file(OPENCLAW_AUTH_PATH)
        if auth_raw:
            try:
                auth_cfg2 = json.loads(auth_raw)
                if isinstance(auth_cfg2, dict):
                    inner = auth_cfg2.get("gateway", {}) if isinstance(auth_cfg2.get("gateway"), dict) else {}
                    inner_auth = inner.get("auth", {}) if isinstance(inner.get("auth"), dict) else {}
                    token = inner_auth.get("token") or auth_cfg2.get("token")
            except Exception:
                pass
    if auth_mode == "token" and not token:
        raise RuntimeError(
            "gateway.auth.token missing in openclaw.json (and no fallback auth.json)"
        )
    return {"authMode": auth_mode, "token": token}
