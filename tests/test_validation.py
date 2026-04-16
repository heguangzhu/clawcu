from __future__ import annotations

import pytest

from clawcu.validation import (
    container_name_for_service,
    image_tag_for_service,
    normalize_hermes_tag,
    normalize_ref,
    normalize_service_version,
    normalize_version,
    validate_cpu,
    validate_memory,
    validate_name,
    validate_port,
)


def test_normalize_version_strips_v_prefix() -> None:
    assert normalize_version("v2026.4.1") == "2026.4.1"
    assert normalize_version("2026.4.1") == "2026.4.1"


def test_normalize_ref_preserves_non_version_git_ref() -> None:
    assert normalize_ref("v0.9.0") == "v0.9.0"
    assert normalize_service_version("hermes", "main") == "main"


def test_normalize_hermes_tag_adds_v_prefix_for_numeric_tags() -> None:
    assert normalize_hermes_tag("2026.4.8") == "v2026.4.8"
    assert normalize_hermes_tag("0.9.0") == "v0.9.0"
    assert normalize_hermes_tag("v2026.4.8") == "v2026.4.8"


def test_service_aware_image_and_container_names() -> None:
    assert image_tag_for_service("openclaw", "2026.4.1") == "clawcu/openclaw:2026.4.1"
    assert image_tag_for_service("hermes", "2026.4.8") == "clawcu/hermes-agent:v2026.4.8"
    assert image_tag_for_service("hermes", "v0.9.0") == "clawcu/hermes-agent:v0.9.0"
    assert container_name_for_service("openclaw", "writer") == "clawcu-openclaw-writer"
    assert container_name_for_service("hermes", "writer") == "clawcu-hermes-writer"


@pytest.mark.parametrize("name", ["writer", "agent-1", "demo.alpha"])
def test_validate_name_accepts_reasonable_values(name: str) -> None:
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["bad name", "-oops", ""])
def test_validate_name_rejects_bad_values(name: str) -> None:
    with pytest.raises(ValueError):
        validate_name(name)


@pytest.mark.parametrize("port", [1, 3000, 65535])
def test_validate_port_accepts_valid_range(port: int) -> None:
    assert validate_port(port) == port


@pytest.mark.parametrize("port", [0, 65536])
def test_validate_port_rejects_out_of_range(port: int) -> None:
    with pytest.raises(ValueError):
        validate_port(port)


def test_validate_cpu_requires_positive_number() -> None:
    assert validate_cpu("1") == "1"
    with pytest.raises(ValueError):
        validate_cpu("0")


def test_validate_memory_requires_docker_like_string() -> None:
    assert validate_memory("2g") == "2g"
    with pytest.raises(ValueError):
        validate_memory("two gigs")
