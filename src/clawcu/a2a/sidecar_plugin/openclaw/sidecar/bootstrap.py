"""Auto-wire mcp.servers.a2a into openclaw.json (Python port of bootstrap.js).

Runs in server.main() before the HTTP listener starts. Merges or removes the
a2a MCP entry based on A2A_ENABLED. Refuses to overwrite invalid JSON; all
failures log a warning and return gracefully.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Mapping, Optional

MCP_ENTRY_NAME = "a2a"


def _deep_get(obj, keys):
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


def build_mcp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/mcp"


def plan_bootstrap(enabled: bool, config, url: Optional[str]):
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


def _atomic_write_json(file_path: str, obj) -> None:
    tmp = f"{file_path}.a2a-bootstrap.{os.getpid()}.tmp"
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, file_path)


def run_bootstrap(env: Optional[Mapping[str, str]] = None, logger=None) -> dict:
    e = env if env is not None else os.environ
    log = logger
    if log is None:
        # Fallback to the module default_logger lazily to avoid import cycles.
        from .logsink import default_logger as _dl

        log = _dl

    config_path = e.get("A2A_SERVICE_MCP_CONFIG_PATH")
    if not config_path:
        log.info("[sidecar:bootstrap] A2A_SERVICE_MCP_CONFIG_PATH unset — skipping MCP auto-wire")
        return {"ok": True, "action": "skip", "reason": "no-config-path"}

    fmt = (e.get("A2A_SERVICE_MCP_CONFIG_FORMAT") or "json").lower()
    if fmt != "json":
        log.info(
            f"[sidecar:bootstrap] unsupported config format \"{fmt}\" — skipping (Python bootstrap handles JSON only)"
        )
        return {"ok": True, "action": "skip", "reason": "unsupported-format"}

    enabled = str(e.get("A2A_ENABLED") or "").lower() == "true"

    raw_port = e.get("A2A_SIDECAR_PORT") or e.get("A2A_BIND_PORT")
    port = None
    try:
        if raw_port not in (None, ""):
            p = int(raw_port)
            if p > 0:
                port = p
    except (TypeError, ValueError):
        port = None

    if enabled and port is None:
        log.warn(
            "[sidecar:bootstrap] A2A_ENABLED=true but sidecar port is unknown — skipping MCP auto-wire"
        )
        return {"ok": True, "action": "skip", "reason": "no-port"}

    url = build_mcp_url(port) if enabled else None

    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        if not enabled:
            return {"ok": True, "action": "skip", "reason": "file-absent-disabled"}
        nxt = {"mcp": {"servers": {MCP_ENTRY_NAME: {"url": url}}}}
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            _atomic_write_json(config_path, nxt)
            log.info(f"[sidecar:bootstrap] created {config_path} with a2a MCP entry → {url}")
            return {"ok": True, "action": "create", "path": config_path}
        except OSError as write_err:
            log.warn(f"[sidecar:bootstrap] failed to create {config_path}: {write_err}")
            return {"ok": False, "action": "error", "error": str(write_err)}
    except OSError as err:
        log.warn(f"[sidecar:bootstrap] cannot read {config_path}: {err}")
        return {"ok": False, "action": "error", "error": str(err)}

    try:
        config = {} if text.strip() == "" else json.loads(text)
    except Exception as err:
        log.warn(
            f"[sidecar:bootstrap] {config_path} is not valid JSON — refusing to overwrite ({err})"
        )
        return {"ok": False, "action": "error", "error": str(err)}

    plan = plan_bootstrap(enabled, config, url)
    if plan["action"] == "noop":
        log.info(f"[sidecar:bootstrap] {config_path} already in desired state ({plan['reason']})")
        return {"ok": True, "action": "noop", "reason": plan["reason"]}

    try:
        _atomic_write_json(config_path, plan["config"])
        suffix = f" → {url}" if url else ""
        log.info(f"[sidecar:bootstrap] {plan['action']} a2a MCP entry in {config_path}{suffix}")
        return {"ok": True, "action": plan["action"], "path": config_path}
    except OSError as err:
        log.warn(f"[sidecar:bootstrap] write failed for {config_path}: {err}")
        return {"ok": False, "action": "error", "error": str(err)}
