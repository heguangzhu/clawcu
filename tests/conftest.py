from __future__ import annotations

import pytest


@pytest.fixture
def temp_clawcu_home(monkeypatch, tmp_path):
    home = tmp_path / ".clawcu"
    monkeypatch.setenv("CLAWCU_HOME", str(home))
    return home
