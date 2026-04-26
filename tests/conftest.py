from __future__ import annotations

import os
from pathlib import Path

import pytest

from clawcu.core import paths as _paths
from clawcu.core import storage as _storage

REAL_CLAWCU_HOME = Path("~/.clawcu").expanduser().resolve()

_real_resolve = _paths.resolve_clawcu_home
_real_build = _paths.build_paths
_real_storage_build = _storage.build_paths


def _guarded_resolve_clawcu_home() -> Path:
    home = _real_resolve()
    if home == REAL_CLAWCU_HOME:
        raise AssertionError(
            f"Test attempted to resolve real CLAWCU_HOME at {REAL_CLAWCU_HOME}. "
            "Use the temp_clawcu_home fixture or monkeypatch.setenv('CLAWCU_HOME', ...)."
        )
    return home


def _guarded_build_paths(home: Path) -> _paths.ClawCUPaths:
    resolved = Path(home).expanduser().resolve()
    if resolved == REAL_CLAWCU_HOME:
        raise AssertionError(
            f"Test attempted to build paths under real CLAWCU_HOME at {REAL_CLAWCU_HOME}."
        )
    return _real_build(home)


@pytest.fixture(autouse=True, scope="session")
def _isolate_clawcu_home(tmp_path_factory):
    sentinel = tmp_path_factory.mktemp("clawcu-session-home")
    previous = os.environ.get("CLAWCU_HOME")
    os.environ["CLAWCU_HOME"] = str(sentinel)
    _paths.resolve_clawcu_home = _guarded_resolve_clawcu_home
    _paths.build_paths = _guarded_build_paths
    _storage.build_paths = _guarded_build_paths
    try:
        yield sentinel
    finally:
        _paths.resolve_clawcu_home = _real_resolve
        _paths.build_paths = _real_build
        _storage.build_paths = _real_storage_build
        if previous is None:
            os.environ.pop("CLAWCU_HOME", None)
        else:
            os.environ["CLAWCU_HOME"] = previous


@pytest.fixture
def temp_clawcu_home(monkeypatch, tmp_path):
    home = tmp_path / ".clawcu"
    monkeypatch.setenv("CLAWCU_HOME", str(home))
    return home
