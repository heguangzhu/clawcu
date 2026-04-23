"""Auto-wire the ``a2a`` MCP entry into a service's MCP config file on start.

Mirror of ``bootstrap.js`` on the Node side (a2a-design-4.md §P0-A). Shared
between the Hermes and OpenClaw sidecars — supports both YAML (Hermes
``config.yaml``) and JSON (OpenClaw ``openclaw.json``).

Safe by construction: any parse / read / write failure logs a warning and
returns without touching the file so the sidecar can still come up.

The caller wires this in ``main()`` before ``serve_forever`` so the service
reads the merged config on its next MCP-config load. Each call takes an
optional ``logger`` — stdlib ``logging.Logger`` (uses ``.warning``) or the
OpenClaw ``logsink.Logger`` (uses ``.warn``) both work.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping, Optional

MCP_ENTRY_NAME = "a2a"

_DEFAULT_LOG = logging.getLogger("a2a.bootstrap")


def _log_info(log, msg: str) -> None:
    fn = getattr(log, "info", None)
    if fn:
        fn(msg)


def _log_warn(log, msg: str) -> None:
    # stdlib Logger exposes both .warning (modern) and .warn (deprecated alias);
    # the OpenClaw logsink.Logger only exposes .warn. Prefer .warning so we
    # don't trip DeprecationWarning on stdlib 3.13+, fall back to .warn.
    fn = getattr(log, "warning", None) or getattr(log, "warn", None)
    if fn:
        fn(msg)


def build_mcp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def _deep_get(obj: Any, keys) -> Any:
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _ensure_dict(parent: dict, key: str) -> dict:
    val = parent.get(key)
    if not isinstance(val, dict):
        parent[key] = {}
    return parent[key]


def plan_bootstrap(enabled: bool, config: Any, url: Optional[str]) -> dict:
    """Pure function: compute the next config and an action label.

    Returns ``{action, config, reason?}`` where ``action`` is one of
    ``merge`` / ``remove`` / ``noop``. Accepts positional or keyword args so
    both the Hermes and OpenClaw test suites (which use different calling
    styles) keep working.
    """
    safe_config = config if isinstance(config, dict) else {}
    current = _deep_get(safe_config, ["mcp", "servers", MCP_ENTRY_NAME])

    if enabled:
        desired = {"url": url}
        same = (
            isinstance(current, dict)
            and current.get("url") == desired["url"]
            and len(current) == 1
        )
        if same:
            return {"action": "noop", "reason": "already-present", "config": safe_config}
        nxt = copy.deepcopy(safe_config)
        mcp = _ensure_dict(nxt, "mcp")
        servers = _ensure_dict(mcp, "servers")
        servers[MCP_ENTRY_NAME] = desired
        return {"action": "merge", "config": nxt}

    if current is None:
        return {"action": "noop", "reason": "absent", "config": safe_config}
    nxt = copy.deepcopy(safe_config)
    servers = _deep_get(nxt, ["mcp", "servers"])
    if isinstance(servers, dict):
        servers.pop(MCP_ENTRY_NAME, None)
    return {"action": "remove", "config": nxt}


def _load_yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except Exception:  # pragma: no cover - env without PyYAML
        return None


def _read_config(config_path: Path, fmt: str, log) -> tuple[Any, bool]:
    """Return ``(config_obj, parsed_ok)``. Missing file → ``({}, True)``."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except OSError as exc:
        _log_warn(log, f"[sidecar:bootstrap] cannot read {config_path}: {exc}")
        return None, False
    if text.strip() == "":
        return {}, True
    if fmt == "json":
        try:
            return json.loads(text), True
        except json.JSONDecodeError as exc:
            _log_warn(
                log,
                f"[sidecar:bootstrap] {config_path} is not valid JSON — refusing to overwrite ({exc})",
            )
            return None, False
    if fmt == "yaml":
        yaml = _load_yaml()
        if yaml is None:
            _log_warn(
                log,
                "[sidecar:bootstrap] PyYAML unavailable — cannot handle YAML MCP config",
            )
            return None, False
        try:
            obj = yaml.safe_load(text)
            return (obj if isinstance(obj, dict) else {}), True
        except yaml.YAMLError as exc:
            _log_warn(
                log,
                f"[sidecar:bootstrap] {config_path} is not valid YAML — refusing to overwrite ({exc})",
            )
            return None, False
    _log_warn(log, f"[sidecar:bootstrap] unsupported A2A_SERVICE_MCP_CONFIG_FORMAT={fmt}")
    return None, False


def _atomic_write(config_path: Path, obj: Any, fmt: str) -> None:
    tmp = config_path.with_suffix(
        config_path.suffix + f".a2a-bootstrap.{os.getpid()}.tmp"
    )
    if fmt == "json":
        text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    else:  # yaml
        yaml = _load_yaml()
        if yaml is None:
            raise RuntimeError("PyYAML not available for YAML write")
        text = yaml.safe_dump(obj, sort_keys=False)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, config_path)


def run_bootstrap(env: Optional[Mapping[str, str]] = None, logger=None) -> dict:
    """Execute the bootstrap against the configured MCP config file.

    Returns ``{ok, action, ...}`` suitable for testing. Never raises for
    operational errors — always returns with a logged warning.
    """
    e = env if env is not None else os.environ
    log = logger if logger is not None else _DEFAULT_LOG

    config_path_raw = e.get("A2A_SERVICE_MCP_CONFIG_PATH")
    if not config_path_raw:
        _log_info(
            log,
            "[sidecar:bootstrap] A2A_SERVICE_MCP_CONFIG_PATH unset — skipping MCP auto-wire",
        )
        return {"ok": True, "action": "skip", "reason": "no-config-path"}

    fmt = (e.get("A2A_SERVICE_MCP_CONFIG_FORMAT") or "json").lower()
    if fmt not in {"yaml", "json"}:
        _log_warn(
            log,
            f'[sidecar:bootstrap] unsupported config format "{fmt}" — skipping',
        )
        return {"ok": True, "action": "skip", "reason": "unsupported-format"}

    enabled = str(e.get("A2A_ENABLED") or "").lower() == "true"

    raw_port = e.get("A2A_SIDECAR_PORT") or e.get("A2A_BIND_PORT")
    port: Optional[int] = None
    try:
        if raw_port not in (None, ""):
            p = int(raw_port)
            if p > 0:
                port = p
    except (TypeError, ValueError):
        port = None

    if enabled and port is None:
        _log_warn(
            log,
            "[sidecar:bootstrap] A2A_ENABLED=true but sidecar port is unknown — skipping MCP auto-wire",
        )
        return {"ok": True, "action": "skip", "reason": "no-port"}

    url = build_mcp_url(port) if enabled else None

    config_path = Path(config_path_raw)
    if not config_path.exists() and not enabled:
        return {"ok": True, "action": "skip", "reason": "file-absent-disabled"}

    config, parsed_ok = _read_config(config_path, fmt, log)
    if not parsed_ok:
        return {"ok": False, "action": "error"}

    if not config_path.exists() and enabled:
        nxt = {"mcp": {"servers": {MCP_ENTRY_NAME: {"url": url}}}}
        try:
            _atomic_write(config_path, nxt, fmt)
            _log_info(
                log,
                f"[sidecar:bootstrap] created {config_path} with a2a MCP entry → {url}",
            )
            return {"ok": True, "action": "create", "path": str(config_path)}
        except OSError as exc:
            _log_warn(log, f"[sidecar:bootstrap] failed to create {config_path}: {exc}")
            return {"ok": False, "action": "error", "error": str(exc)}

    plan = plan_bootstrap(enabled, config, url)
    if plan["action"] == "noop":
        _log_info(
            log,
            f"[sidecar:bootstrap] {config_path} already in desired state ({plan.get('reason')})",
        )
        return {"ok": True, "action": "noop", "reason": plan.get("reason")}
    try:
        _atomic_write(config_path, plan["config"], fmt)
        suffix = f" → {url}" if url else ""
        _log_info(
            log,
            f"[sidecar:bootstrap] {plan['action']} a2a MCP entry in {config_path}{suffix}",
        )
        return {"ok": True, "action": plan["action"], "path": str(config_path)}
    except OSError as exc:
        _log_warn(log, f"[sidecar:bootstrap] write failed for {config_path}: {exc}")
        return {"ok": False, "action": "error", "error": str(exc)}


__all__ = [
    "MCP_ENTRY_NAME",
    "build_mcp_url",
    "plan_bootstrap",
    "run_bootstrap",
]
