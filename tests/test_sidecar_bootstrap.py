"""pytest port of tests/sidecar_bootstrap.test.js."""
from __future__ import annotations

import json
import os
import sys

import pytest

_SIDECAR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "src",
        "clawcu",
        "a2a",
        "sidecar_plugin",
        "openclaw",
        "sidecar",
    )
)
if _SIDECAR not in sys.path:
    sys.path.insert(0, _SIDECAR)

from bootstrap import MCP_ENTRY_NAME, build_mcp_url, plan_bootstrap, run_bootstrap  # noqa: E402


class CapturingLogger:
    def __init__(self):
        self.info_msgs = []
        self.warn_msgs = []

    def info(self, *args):
        self.info_msgs.append(" ".join(str(a) for a in args))

    # The bootstrap module calls `.info` for successes and `.warn` for
    # failures; expose `.log` as an alias for tests that check either stream.
    log = info

    def warn(self, *args):
        self.warn_msgs.append(" ".join(str(a) for a in args))

    def error(self, *args):  # noqa: D401 - parity with logsink.Logger
        self.warn_msgs.append(" ".join(str(a) for a in args))


def test_build_mcp_url_renders_127_0_0_1_form():
    assert build_mcp_url(9129) == "http://127.0.0.1:9129/mcp"


def test_plan_bootstrap_merges_when_enabled_and_absent():
    plan = plan_bootstrap(True, {"other": 1}, "http://127.0.0.1:18790/mcp")
    assert plan["action"] == "merge"
    assert plan["config"]["mcp"]["servers"][MCP_ENTRY_NAME] == {"url": "http://127.0.0.1:18790/mcp"}
    assert plan["config"]["other"] == 1


def test_plan_bootstrap_noops_when_already_desired():
    plan = plan_bootstrap(
        True,
        {"mcp": {"servers": {"a2a": {"url": "http://127.0.0.1:18790/mcp"}}}},
        "http://127.0.0.1:18790/mcp",
    )
    assert plan["action"] == "noop"


def test_plan_bootstrap_rewrites_when_url_differs():
    plan = plan_bootstrap(
        True,
        {"mcp": {"servers": {"a2a": {"url": "http://127.0.0.1:1111/mcp"}}}},
        "http://127.0.0.1:2222/mcp",
    )
    assert plan["action"] == "merge"
    assert plan["config"]["mcp"]["servers"]["a2a"]["url"] == "http://127.0.0.1:2222/mcp"


def test_plan_bootstrap_removes_stale_when_disabled():
    plan = plan_bootstrap(
        False,
        {"mcp": {"servers": {"a2a": {"url": "http://127.0.0.1:18790/mcp"}, "other": {"url": "x"}}}},
        None,
    )
    assert plan["action"] == "remove"
    assert "a2a" not in plan["config"]["mcp"]["servers"]
    assert plan["config"]["mcp"]["servers"]["other"] == {"url": "x"}


def test_plan_bootstrap_noop_when_disabled_and_absent():
    plan = plan_bootstrap(False, {"mcp": {"servers": {"other": {"url": "x"}}}}, None)
    assert plan["action"] == "noop"


def test_plan_bootstrap_preserves_sibling_keys():
    plan = plan_bootstrap(
        True,
        {"mcp": {"servers": {"context7": {"command": "uvx"}}}},
        "http://127.0.0.1:18790/mcp",
    )
    assert plan["config"]["mcp"]["servers"]["context7"]["command"] == "uvx"
    assert plan["config"]["mcp"]["servers"]["a2a"]["url"] == "http://127.0.0.1:18790/mcp"


def test_plan_bootstrap_tolerates_missing_mcp_block():
    plan = plan_bootstrap(True, {}, "http://127.0.0.1:18790/mcp")
    assert plan["action"] == "merge"
    assert plan["config"]["mcp"]["servers"]["a2a"] == {"url": "http://127.0.0.1:18790/mcp"}


def test_run_bootstrap_skips_when_config_path_unset():
    logger = CapturingLogger()
    r = run_bootstrap(env={"A2A_ENABLED": "true"}, logger=logger)
    assert r["ok"] is True
    assert r["action"] == "skip"
    assert r["reason"] == "no-config-path"
    assert any("A2A_SERVICE_MCP_CONFIG_PATH unset" in m for m in logger.info_msgs)


def test_run_bootstrap_skips_when_format_not_json(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("mcp:\n  servers: {}\n", encoding="utf-8")
    logger = CapturingLogger()
    r = run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_SERVICE_MCP_CONFIG_FORMAT": "yaml",
            "A2A_ENABLED": "true",
            "A2A_SIDECAR_PORT": "18790",
        },
        logger=logger,
    )
    assert r["action"] == "skip"
    assert r["reason"] == "unsupported-format"


def test_run_bootstrap_creates_file_when_enabled_and_absent(tmp_path):
    config_path = tmp_path / "nested" / "config.json"
    logger = CapturingLogger()
    r = run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_ENABLED": "true",
            "A2A_SIDECAR_PORT": "18790",
        },
        logger=logger,
    )
    assert r["ok"] is True
    assert r["action"] == "create"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["mcp"]["servers"]["a2a"]["url"] == "http://127.0.0.1:18790/mcp"


def test_run_bootstrap_noops_on_absent_file_when_disabled(tmp_path):
    config_path = tmp_path / "config.json"
    logger = CapturingLogger()
    r = run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_ENABLED": "false",
        },
        logger=logger,
    )
    assert r["action"] == "skip"
    assert r["reason"] == "file-absent-disabled"
    assert not config_path.exists()


def test_run_bootstrap_merges_into_existing_config(tmp_path):
    config_path = tmp_path / "config.json"
    original = {
        "gateway": {"port": 18789},
        "mcp": {"servers": {"context7": {"command": "uvx", "args": ["context7-mcp"]}}},
    }
    config_path.write_text(json.dumps(original, indent=2), encoding="utf-8")
    logger = CapturingLogger()
    r = run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_ENABLED": "true",
            "A2A_SIDECAR_PORT": "18790",
        },
        logger=logger,
    )
    assert r["ok"] is True
    assert r["action"] == "merge"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["gateway"]["port"] == 18789
    assert written["mcp"]["servers"]["context7"]["command"] == "uvx"
    assert written["mcp"]["servers"]["a2a"]["url"] == "http://127.0.0.1:18790/mcp"


def test_run_bootstrap_removes_stale_entry_when_disabled(tmp_path):
    config_path = tmp_path / "config.json"
    original = {
        "mcp": {
            "servers": {
                "a2a": {"url": "http://127.0.0.1:9999/mcp"},
                "keep": {"url": "x"},
            }
        }
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    logger = CapturingLogger()
    r = run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_ENABLED": "false",
        },
        logger=logger,
    )
    assert r["action"] == "remove"
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert "a2a" not in written["mcp"]["servers"]
    assert written["mcp"]["servers"]["keep"] == {"url": "x"}


def test_run_bootstrap_does_not_overwrite_malformed_json(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{not json", encoding="utf-8")
    logger = CapturingLogger()
    r = run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_ENABLED": "true",
            "A2A_SIDECAR_PORT": "18790",
        },
        logger=logger,
    )
    assert r["ok"] is False
    assert config_path.read_text(encoding="utf-8") == "{not json"
    assert any("not valid JSON" in m for m in logger.warn_msgs)


def test_run_bootstrap_bails_when_port_missing(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    logger = CapturingLogger()
    r = run_bootstrap(
        env={
            "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
            "A2A_ENABLED": "true",
        },
        logger=logger,
    )
    assert r["action"] == "skip"
    assert r["reason"] == "no-port"
    assert config_path.read_text(encoding="utf-8") == "{}"


def test_run_bootstrap_is_idempotent(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    logger = CapturingLogger()
    env = {
        "A2A_SERVICE_MCP_CONFIG_PATH": str(config_path),
        "A2A_ENABLED": "true",
        "A2A_SIDECAR_PORT": "18790",
    }
    first = run_bootstrap(env=env, logger=logger)
    second = run_bootstrap(env=env, logger=logger)
    assert first["action"] == "merge"
    assert second["action"] == "noop"
