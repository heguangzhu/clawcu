from __future__ import annotations

import json
from pathlib import Path

from clawcu.models import InstanceRecord
from clawcu.paths import bootstrap_config_path, get_paths
from clawcu.storage import StateStore


def make_record(datadir: Path) -> InstanceRecord:
    return InstanceRecord(
        service="openclaw",
        name="writer",
        version="2026.4.1",
        upstream_ref="v2026.4.1",
        image_tag="clawcu/openclaw:2026.4.1",
        container_name="clawcu-openclaw-writer",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
        auth_mode="token",
        status="running",
        created_at="2026-04-11T00:00:00+00:00",
        updated_at="2026-04-11T00:00:00+00:00",
        history=[],
    )


def test_store_round_trip(temp_clawcu_home, tmp_path) -> None:
    store = StateStore(get_paths())
    record = make_record(tmp_path / "writer")
    store.save_record(record)

    loaded = store.load_record("writer")

    assert loaded.name == "writer"
    assert loaded.image_tag == "clawcu/openclaw:2026.4.1"


def test_snapshot_restore_replaces_directory(temp_clawcu_home, tmp_path) -> None:
    store = StateStore(get_paths())
    datadir = tmp_path / "writer-data"
    datadir.mkdir()
    (datadir / "state.txt").write_text("before", encoding="utf-8")

    snapshot = store.create_snapshot("writer", datadir, "upgrade-test")
    (datadir / "state.txt").write_text("after", encoding="utf-8")

    store.restore_snapshot(snapshot, datadir)

    assert (datadir / "state.txt").read_text(encoding="utf-8") == "before"


def test_snapshot_restore_replaces_directory_and_instance_env(temp_clawcu_home, tmp_path) -> None:
    store = StateStore(get_paths())
    datadir = tmp_path / "writer-data"
    datadir.mkdir()
    (datadir / "state.txt").write_text("before", encoding="utf-8")
    env_path = store.instance_env_path("writer")
    env_path.write_text("OPENAI_API_KEY=before\n", encoding="utf-8")

    snapshot = store.create_snapshot("writer", datadir, "upgrade-test", env_path=env_path)
    (datadir / "state.txt").write_text("after", encoding="utf-8")
    env_path.write_text("OPENAI_API_KEY=after\n", encoding="utf-8")

    store.restore_snapshot(snapshot, datadir, env_path=env_path)

    assert (datadir / "state.txt").read_text(encoding="utf-8") == "before"
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=before\n"


def test_bootstrap_home_can_be_saved_and_read(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAWCU_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    store = StateStore(get_paths())

    store.set_bootstrap_home("/tmp/clawcu-custom-home")

    assert store.get_bootstrap_home() == "/tmp/clawcu-custom-home"
    assert json.loads(bootstrap_config_path().read_text(encoding="utf-8")) == {
        "clawcu_home": "/tmp/clawcu-custom-home"
    }
