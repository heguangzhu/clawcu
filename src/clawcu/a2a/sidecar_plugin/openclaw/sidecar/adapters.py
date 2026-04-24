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
import threading
import time
from typing import Any, Dict

OPENCLAW_CONFIG_PATH = "/home/node/.openclaw/openclaw.json"
OPENCLAW_AUTH_PATH = "/home/node/.openclaw/auth.json"

# Review-1 §10 / review-2 §7: ``read_gateway_auth`` runs on every inbound
# ``/a2a/send``. Without a cache the host adapter forks ``docker exec
# <container> cat openclaw.json`` for each peer message — tens of
# milliseconds of subprocess setup in the latency path, and the kind of
# thing that rots into a bottleneck if a peer pushes chat traffic.
# A small TTL cache (default 60s, tunable via ``A2A_HOST_ADAPTER_TTL_S``)
# drops the subprocess out of steady-state while keeping config-rotation
# cheap — the operator just waits one TTL after editing ``openclaw.json``.
_DEFAULT_HOST_ADAPTER_TTL_S = 60.0


def _host_adapter_ttl_s() -> float:
    raw = os.environ.get("A2A_HOST_ADAPTER_TTL_S")
    if raw is None or raw.strip() == "":
        return _DEFAULT_HOST_ADAPTER_TTL_S
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_HOST_ADAPTER_TTL_S
    return v if v >= 0 else _DEFAULT_HOST_ADAPTER_TTL_S


class HostAdapter:
    mode = "host"

    def __init__(
        self,
        container: str,
        *,
        ttl_s: float | None = None,
        now: Any = time.monotonic,
    ) -> None:
        self.container = container
        self._ttl_s = _host_adapter_ttl_s() if ttl_s is None else ttl_s
        self._now = now
        self._cache: Dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()

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

    def _cached_exec(self, key: tuple, args: list[str]):
        if self._ttl_s <= 0:
            return self._exec(args)
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None and self._now() < entry[0]:
                return entry[1]
        value = self._exec(args)
        with self._lock:
            self._cache[key] = (self._now() + self._ttl_s, value)
        return value

    def invalidate_cache(self) -> None:
        """Drop the ``read_file``/``get_env`` cache. Useful for tests and for
        an operator-triggered config reload — subsequent reads pay the
        subprocess cost once to refresh."""
        with self._lock:
            self._cache.clear()

    def read_file(self, path: str):
        return self._cached_exec(("file", path), ["cat", path])

    def get_env(self, name: str):
        v = self._cached_exec(("env", name), ["printenv", name])
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
