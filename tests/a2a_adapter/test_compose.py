"""Tests for clawcu.a2a.adapter.compose — companion container helpers."""

import pytest
from unittest.mock import MagicMock, patch


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
