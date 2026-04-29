"""Tests for clawcu.a2a.adapter.compose — companion container helpers."""

from pathlib import Path

import pytest
from unittest.mock import MagicMock, patch


def test_adapter_dockerfile_installs_after_package_sources_are_copied():
    dockerfile = Path("src/clawcu/a2a/adapter/Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.index("COPY src/clawcu/ ./src/clawcu/") < dockerfile.index(
        'RUN pip install --no-cache-dir ".[a2a]"'
    )


class TestCompanionHelpers:
    def test_companion_container_name(self):
        from clawcu.a2a.adapter.compose import companion_container_name

        assert companion_container_name("writer") == "clawcu-a2a-writer"
        assert companion_container_name("my-instance") == "clawcu-a2a-my-instance"

    def test_adapter_image_tag(self):
        from clawcu.a2a.adapter.compose import adapter_image_tag

        assert adapter_image_tag("0.4.2") == "clawcu/a2a-adapter:0.4.2"
        assert adapter_image_tag("1.0.0") == "clawcu/a2a-adapter:1.0.0"

    def test_build_adapter_image_skips_existing(self):
        from clawcu.a2a.adapter.compose import build_adapter_image

        docker = MagicMock()
        docker.image_exists.return_value = True

        tag = build_adapter_image(docker, "0.4.2", reporter=MagicMock())
        assert tag == "clawcu/a2a-adapter:0.4.2"
        docker.build_image.assert_not_called()

    def test_build_adapter_image_builds_when_missing(self):
        from clawcu.a2a.adapter.compose import build_adapter_image

        docker = MagicMock()
        docker.image_exists.return_value = False
        reporter = MagicMock()

        tag = build_adapter_image(docker, "0.4.2", reporter=reporter)
        assert tag == "clawcu/a2a-adapter:0.4.2"
        docker.build_image.assert_called_once()
        reporter.assert_called()

    def test_stop_companion(self):
        from clawcu.a2a.adapter.compose import stop_companion

        docker = MagicMock()
        stop_companion(docker, "writer")
        docker.stop_container.assert_called_once_with("clawcu-a2a-writer")
        docker.remove_container.assert_called_once_with("clawcu-a2a-writer", missing_ok=True)

    def test_companion_status(self):
        from clawcu.a2a.adapter.compose import companion_status

        docker = MagicMock()
        docker.container_status.return_value = "running"
        assert companion_status(docker, "writer") == "running"
        docker.container_status.assert_called_once_with("clawcu-a2a-writer")

    def test_start_companion_omits_add_host_with_container_network(self):
        from clawcu.a2a.adapter.compose import CompanionSpec, start_companion

        commands = []
        docker = MagicMock()
        docker.RUN_TIMEOUT_SECONDS = 30
        docker.runner = lambda cmd, timeout_seconds: commands.append(cmd)

        start_companion(
            docker,
            CompanionSpec(
                name="writer",
                instance_name="writer",
                adapter_image="clawcu/a2a-adapter:test",
                gateway_url="http://127.0.0.1:18789",
                gateway_auth_token="",
                gateway_ready_path="/healthz",
                agent_url="http://127.0.0.1:18790",
                extra_hosts=[("host.docker.internal", "host-gateway")],
            ),
            "clawcu-openclaw-writer",
        )

        command = commands[0]
        assert "--network" in command
        assert "container:clawcu-openclaw-writer" in command
        assert "--add-host" not in command
        assert "-eA2A_GATEWAY_TIMEOUT=86400" in command
        assert "-eA2A_SEND_TIMEOUT=86400" in command
