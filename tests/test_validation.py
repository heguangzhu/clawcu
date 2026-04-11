from __future__ import annotations

import pytest

from clawcu.validation import normalize_version, validate_cpu, validate_memory, validate_name, validate_port


def test_normalize_version_strips_v_prefix() -> None:
    assert normalize_version("v2026.4.1") == "2026.4.1"
    assert normalize_version("2026.4.1") == "2026.4.1"


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
