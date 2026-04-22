#!/usr/bin/env python3
"""Auto-wire the a2a MCP entry into the Hermes service MCP config on start.

Mirror of ``bootstrap.js`` on the Node side (a2a-design-4.md §P0-A).
Handles YAML (Hermes ``config.yaml``) and JSON; safe by construction —
any parse / read / write failure logs a warning and returns without
touching the file.

The caller wires this in ``sidecar.py::main()`` before ``serve_forever``
so the service reads the merged config on its next MCP-config load.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

MCP_ENTRY_NAME = "a2a"

log = logging.getLogger("a2a.bootstrap")


def build_mcp_url(*, port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def _desired_matches(current: Any, desired: dict) -> bool:
    return (
        isinstance(current, dict)
        and current.get("url") == desired["url"]
        and set(current.keys()) == {"url"}
    )


def plan_bootstrap(*, enabled: bool, config: Any, url: str | None) -> dict:
    """Pure function: compute the next config and an action label.

    Returns a dict ``{action, config, reason?}`` where ``action`` is one
    of ``merge`` / ``remove`` / ``noop``.
    """
    if not isinstance(config, dict):
        config = {}

    current = None
    mcp = config.get("mcp") if isinstance(config.get("mcp"), dict) else None
    if mcp is not None:
        servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else None
        if servers is not None:
            current = servers.get(MCP_ENTRY_NAME)

    if enabled:
        desired = {"url": url}
        if _desired_matches(current, desired):
            return {"action": "noop", "reason": "already-present", "config": config}
        nxt = copy.deepcopy(config)
        mcp_nxt = nxt.setdefault("mcp", {})
        if not isinstance(mcp_nxt, dict):
            mcp_nxt = {}
            nxt["mcp"] = mcp_nxt
        servers_nxt = mcp_nxt.setdefault("servers", {})
        if not isinstance(servers_nxt, dict):
            servers_nxt = {}
            mcp_nxt["servers"] = servers_nxt
        servers_nxt[MCP_ENTRY_NAME] = desired
        return {"action": "merge", "config": nxt}

    if current is None:
        return {"action": "noop", "reason": "absent", "config": config}
    nxt = copy.deepcopy(config)
    servers_nxt = nxt.get("mcp", {}).get("servers", {}) if isinstance(nxt.get("mcp"), dict) else {}
    if isinstance(servers_nxt, dict) and MCP_ENTRY_NAME in servers_nxt:
        del servers_nxt[MCP_ENTRY_NAME]
    return {"action": "remove", "config": nxt}


def _load_yaml():
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover - env without PyYAML
        log.warning("PyYAML unavailable (%s) — cannot handle YAML MCP config", exc)
        return None
    return yaml


def _read_config(config_path: Path, fmt: str) -> tuple[Any, bool]:
    """Return (config_obj, parsed_ok). Missing file → ({}, True)."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, True
    except OSError as exc:
        log.warning("cannot read %s: %s", config_path, exc)
        return None, False
    if text.strip() == "":
        return {}, True
    if fmt == "json":
        try:
            return json.loads(text), True
        except json.JSONDecodeError as exc:
            log.warning("%s is not valid JSON — refusing to overwrite (%s)", config_path, exc)
            return None, False
    if fmt == "yaml":
        yaml = _load_yaml()
        if yaml is None:
            return None, False
        try:
            obj = yaml.safe_load(text)
            return (obj if isinstance(obj, dict) else {}), True
        except yaml.YAMLError as exc:
            log.warning("%s is not valid YAML — refusing to overwrite (%s)", config_path, exc)
            return None, False
    log.warning("unsupported A2A_SERVICE_MCP_CONFIG_FORMAT=%s", fmt)
    return None, False


def _atomic_write(config_path: Path, obj: Any, fmt: str) -> None:
    tmp = config_path.with_suffix(config_path.suffix + f".a2a-bootstrap.{os.getpid()}.tmp")
    if fmt == "json":
        text = json.dumps(obj, indent=2) + "\n"
    else:  # yaml
        yaml = _load_yaml()
        if yaml is None:
            raise RuntimeError("PyYAML not available for YAML write")
        text = yaml.safe_dump(obj, sort_keys=False)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, config_path)


def run_bootstrap(*, env: dict[str, str] | None = None) -> dict:
    """Execute the bootstrap against the configured MCP config file.

    Returns a dict ``{ok, action, ...}`` suitable for testing. Never
    raises for operational errors — always returns with a logged
    warning.
    """
    e = env if env is not None else os.environ
    config_path_raw = e.get("A2A_SERVICE_MCP_CONFIG_PATH")
    if not config_path_raw:
        log.info("A2A_SERVICE_MCP_CONFIG_PATH unset — skipping MCP auto-wire")
        return {"ok": True, "action": "skip", "reason": "no-config-path"}
    fmt = (e.get("A2A_SERVICE_MCP_CONFIG_FORMAT") or "yaml").lower()
    if fmt not in {"yaml", "json"}:
        log.warning("unsupported config format %s — skipping", fmt)
        return {"ok": True, "action": "skip", "reason": "unsupported-format"}
    enabled = str(e.get("A2A_ENABLED") or "").lower() == "true"
    port_raw = e.get("A2A_BIND_PORT") or e.get("A2A_SIDECAR_PORT")
    try:
        port = int(port_raw) if port_raw else None
    except ValueError:
        port = None
    if enabled and (port is None or port <= 0):
        log.warning("A2A_ENABLED=true but sidecar port unknown — skipping MCP auto-wire")
        return {"ok": True, "action": "skip", "reason": "no-port"}
    url = build_mcp_url(port=port) if enabled else None

    config_path = Path(config_path_raw)
    if not config_path.exists() and not enabled:
        return {"ok": True, "action": "skip", "reason": "file-absent-disabled"}

    config, parsed_ok = _read_config(config_path, fmt)
    if not parsed_ok:
        return {"ok": False, "action": "error"}
    if not config_path.exists() and enabled:
        nxt = {"mcp": {"servers": {MCP_ENTRY_NAME: {"url": url}}}}
        try:
            _atomic_write(config_path, nxt, fmt)
            log.info("created %s with a2a MCP entry → %s", config_path, url)
            return {"ok": True, "action": "create", "path": str(config_path)}
        except OSError as exc:
            log.warning("failed to create %s: %s", config_path, exc)
            return {"ok": False, "action": "error", "error": str(exc)}

    plan = plan_bootstrap(enabled=enabled, config=config, url=url)
    if plan["action"] == "noop":
        log.info("%s already in desired state (%s)", config_path, plan.get("reason"))
        return {"ok": True, "action": "noop", "reason": plan.get("reason")}
    try:
        _atomic_write(config_path, plan["config"], fmt)
        log.info(
            "%s a2a MCP entry in %s%s",
            plan["action"],
            config_path,
            f" → {url}" if url else "",
        )
        return {"ok": True, "action": plan["action"], "path": str(config_path)}
    except OSError as exc:
        log.warning("write failed for %s: %s", config_path, exc)
        return {"ok": False, "action": "error", "error": str(exc)}


__all__ = [
    "MCP_ENTRY_NAME",
    "build_mcp_url",
    "plan_bootstrap",
    "run_bootstrap",
]
