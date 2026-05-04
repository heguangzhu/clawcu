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
        from clawcu.a2a.adapter.compose import (
            companion_container_name,
            redis_companion_container_name,
            worker_companion_container_name,
        )

        assert companion_container_name("writer") == "clawcu-a2a-writer"
        assert companion_container_name("my-instance") == "clawcu-a2a-my-instance"
        assert redis_companion_container_name("writer") == "clawcu-a2a-redis"
        assert worker_companion_container_name("writer") == "clawcu-a2a-worker-writer"

    def test_redis_and_worker_companion_specs(self):
        from clawcu.a2a.adapter.compose import (
            CompanionSpec,
            redis_companion_spec,
            worker_companion_spec,
        )

        redis_spec = redis_companion_spec("writer")
        assert redis_spec.instance_name == "writer"
        assert redis_spec.redis_image == "redis:7-alpine"
        assert redis_spec.redis_port == 6379

        companion_spec = CompanionSpec(
            name="writer",
            instance_name="writer",
            adapter_image="clawcu/a2a-adapter:test",
            gateway_url="http://127.0.0.1:18789",
            gateway_auth_token="secret",
            gateway_ready_path="/healthz",
            agent_url="http://127.0.0.1:18790",
            redis_url="redis://redis.internal:6380/1",
            queue_name="custom-queue",
            async_enabled=True,
            default_mode="async",
            task_workers=2,
            task_deadline_seconds=17,
            task_retain_seconds=23,
        )

        worker_spec = worker_companion_spec(companion_spec)
        assert worker_spec.name == "writer"
        assert worker_spec.instance_name == "writer"
        assert worker_spec.worker_image == "clawcu/a2a-adapter:test"
        assert worker_spec.gateway_url == "http://127.0.0.1:18789"
        assert worker_spec.gateway_auth_token == "secret"
        assert worker_spec.redis_url == "redis://redis.internal:6380/1"
        assert worker_spec.queue_name == "custom-queue"
        assert worker_spec.task_workers == 2
        assert worker_spec.task_deadline_seconds == 17
        assert worker_spec.task_retain_seconds == 23

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
        from clawcu.a2a.adapter.compose import (
            stop_companion,
            stop_redis_companion,
            stop_worker_companion,
        )

        docker = MagicMock()
        stop_companion(docker, "writer")
        docker.stop_container.assert_called_once_with("clawcu-a2a-writer")
        docker.remove_container.assert_called_once_with("clawcu-a2a-writer", missing_ok=True)

        docker = MagicMock()
        stop_redis_companion(docker, "writer")
        docker.stop_container.assert_called_once_with("clawcu-a2a-redis")
        docker.remove_container.assert_called_once_with(
            "clawcu-a2a-redis", missing_ok=True
        )

        docker = MagicMock()
        stop_worker_companion(docker, "writer")
        docker.stop_container.assert_called_once_with("clawcu-a2a-worker-writer")
        docker.remove_container.assert_called_once_with(
            "clawcu-a2a-worker-writer", missing_ok=True
        )

    def test_companion_status(self):
        from clawcu.a2a.adapter.compose import (
            companion_status,
            redis_companion_status,
            worker_companion_status,
        )

        docker = MagicMock()
        docker.container_status.return_value = "running"
        assert companion_status(docker, "writer") == "running"
        docker.container_status.assert_called_once_with("clawcu-a2a-writer")

        docker = MagicMock()
        docker.container_status.return_value = "running"
        assert redis_companion_status(docker, "writer") == "running"
        docker.container_status.assert_called_once_with("clawcu-a2a-redis")

        docker = MagicMock()
        docker.container_status.return_value = "running"
        assert worker_companion_status(docker, "writer") == "running"
        docker.container_status.assert_called_once_with("clawcu-a2a-worker-writer")

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
                async_enabled=True,
            ),
            "clawcu-openclaw-writer",
        )

        command = commands[0]
        assert "--network" in command
        assert "container:clawcu-openclaw-writer" in command
        assert "--add-host" not in command
        assert "-eA2A_GATEWAY_TIMEOUT=86400" in command
        assert "-eA2A_SEND_TIMEOUT=86400" in command
        assert "-eA2A_ASYNC_ENABLED=true" in command
        assert "-eA2A_DEFAULT_MODE=sync" in command
        assert "-eA2A_REDIS_URL=redis://host.docker.internal:6379/0" in command
        assert "-eA2A_QUEUE_NAME=clawcu:a2a:writer" in command
        assert "-eA2A_TASK_WORKERS=4" in command
        assert "-eA2A_TASK_DEADLINE_S=86400" in command
        assert "-eA2A_TASK_RETAIN_S=86400" in command

    def test_start_redis_companion_generates_docker_run(self):
        from clawcu.a2a.adapter.compose import RedisCompanionSpec, start_redis_companion

        commands = []
        docker = MagicMock()
        docker.RUN_TIMEOUT_SECONDS = 30
        docker.runner = lambda cmd, timeout_seconds: commands.append(cmd)

        start_redis_companion(
            docker,
            RedisCompanionSpec(instance_name="writer"),
            "clawcu-openclaw-writer",
        )

        docker.remove_container.assert_called_once_with(
            "clawcu-a2a-redis", missing_ok=True
        )
        assert commands == [
            [
                "docker", "run", "-d",
                "--name", "clawcu-a2a-redis",
                "-p", "127.0.0.1:6379:6379",
                "--restart", "unless-stopped",
                "redis:7-alpine",
                "redis-server",
                "--save", "",
                "--appendonly", "no",
                "--port", "6379",
            ]
        ]

    def test_start_worker_companion_generates_docker_run(self):
        from clawcu.a2a.adapter.compose import WorkerCompanionSpec, start_worker_companion

        commands = []
        docker = MagicMock()
        docker.RUN_TIMEOUT_SECONDS = 30
        docker.runner = lambda cmd, timeout_seconds: commands.append(cmd)

        start_worker_companion(
            docker,
            WorkerCompanionSpec(
                name="writer",
                instance_name="writer",
                worker_image="clawcu/a2a-adapter:test",
                gateway_url="http://127.0.0.1:18789",
                gateway_auth_token="secret",
                gateway_ready_path="/healthz",
            ),
            "clawcu-openclaw-writer",
        )

        docker.remove_container.assert_called_once_with(
            "clawcu-a2a-worker-writer", missing_ok=True
        )
        command = commands[0]
        assert command[:9] == [
            "docker", "run", "-d",
            "--name", "clawcu-a2a-worker-writer",
            "--network", "container:clawcu-openclaw-writer",
            "--restart", "unless-stopped",
        ]
        assert "-eA2A_AGENT_NAME=writer" in command
        assert "-eA2A_GATEWAY_URL=http://127.0.0.1:18789" in command
        assert "-eA2A_GATEWAY_AUTH_TOKEN=secret" in command
        assert "-eA2A_GATEWAY_READY_PATH=/healthz" in command
        assert "-eA2A_GATEWAY_TIMEOUT=86400" in command
        assert "-eA2A_SEND_TIMEOUT=86400" in command
        assert "-eA2A_REGISTRY_URL=http://host.docker.internal:9100" in command
        assert "-eA2A_REDIS_URL=redis://host.docker.internal:6379/0" in command
        assert "-eA2A_QUEUE_NAME=clawcu:a2a:writer" in command
        assert "-eA2A_TASK_WORKERS=4" in command
        assert "-eA2A_TASK_DEADLINE_S=86400" in command
        assert "-eA2A_TASK_RETAIN_S=86400" in command
        assert command[-5:] == [
            "clawcu/a2a-adapter:test",
            "python",
            "-m",
            "arq",
            "clawcu.a2a.adapter.worker.WorkerSettings",
        ]
