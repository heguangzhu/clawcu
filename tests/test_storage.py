from __future__ import annotations

from pathlib import Path

from clawcu.models import InstanceRecord
from clawcu.paths import get_paths
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
