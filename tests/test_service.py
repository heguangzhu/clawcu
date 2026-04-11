from __future__ import annotations

from pathlib import Path

import pytest

from clawcu.models import InstanceRecord
from clawcu.paths import get_paths
from clawcu.service import ClawCUService
from clawcu.storage import StateStore
from clawcu.subprocess_utils import CommandError


class FakeDockerManager:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.status_map: dict[str, str] = {}
        self.fail_next_run = False
        self.fail_next_start = False
        self.run_errors: list[Exception] = []

    def image_exists(self, image_tag: str) -> bool:
        return True

    def run_container(self, record: InstanceRecord) -> None:
        self.commands.append(("run", record.container_name))
        if self.run_errors:
            raise self.run_errors.pop(0)
        if self.fail_next_run:
            self.fail_next_run = False
            raise RuntimeError("boom")
        self.status_map[record.container_name] = "running"

    def container_status(self, container_name: str) -> str:
        return self.status_map.get(container_name, "missing")

    def inspect_container(self, container_name: str) -> dict | None:
        status = self.status_map.get(container_name)
        if not status:
            return None
        return {"Name": container_name, "State": {"Status": status}}

    def start_container(self, container_name: str) -> None:
        self.commands.append(("start", container_name))
        if self.fail_next_start:
            self.fail_next_start = False
            raise RuntimeError("port is already allocated")
        self.status_map[container_name] = "running"

    def stop_container(self, container_name: str) -> None:
        self.commands.append(("stop", container_name))
        self.status_map[container_name] = "exited"

    def restart_container(self, container_name: str) -> None:
        self.commands.append(("restart", container_name))
        self.status_map[container_name] = "running"

    def remove_container(self, container_name: str, *, missing_ok: bool = False) -> None:
        self.commands.append(("rm", container_name))
        self.status_map.pop(container_name, None)

    def stream_logs(self, container_name: str, *, follow: bool = False) -> None:
        self.commands.append(("logs", container_name))


class FakeOpenClawManager:
    def __init__(self) -> None:
        self.versions: list[str] = []

    def build_image(self, version: str) -> str:
        self.versions.append(version)
        return f"clawcu/openclaw:{version}"

    def ensure_image(self, version: str) -> str:
        self.versions.append(version)
        return f"clawcu/openclaw:{version}"


def make_service(temp_clawcu_home) -> tuple[ClawCUService, FakeDockerManager, FakeOpenClawManager, StateStore]:
    store = StateStore(get_paths())
    docker = FakeDockerManager()
    openclaw = FakeOpenClawManager()
    service = ClawCUService(store=store, docker=docker, openclaw=openclaw)
    return service, docker, openclaw, store


def test_create_openclaw_saves_record(temp_clawcu_home, tmp_path) -> None:
    service, docker, openclaw, store = make_service(temp_clawcu_home)

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=3000,
        cpu="1",
        memory="2g",
    )

    assert record.status == "running"
    assert openclaw.versions == ["2026.4.1"]
    assert store.load_record("writer").image_tag == "clawcu/openclaw:2026.4.1"
    assert docker.commands[0][0] == "run"


def test_create_openclaw_defaults_datadir_and_port(temp_clawcu_home) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port == 18789

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        cpu="1",
        memory="2g",
    )

    assert record.datadir.endswith("/.clawcu/writer")
    assert record.port == 18789


def test_create_openclaw_rejects_duplicate_name(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    with pytest.raises(ValueError, match="Instance 'writer' already exists."):
        service.create_openclaw(
            name="writer",
            version="2026.4.2",
            datadir=str(tmp_path / "writer-2"),
            port=18799,
            cpu="1",
            memory="2g",
        )

    assert docker.commands.count(("run", "clawcu-openclaw-writer")) == 1


def test_create_openclaw_rejects_existing_docker_container_without_record(temp_clawcu_home) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    docker.status_map["clawcu-openclaw-writer"] = "created"

    with pytest.raises(
        ValueError,
        match="Instance 'writer' already exists. Docker container 'clawcu-openclaw-writer' is already present.",
        ):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            port=18789,
            cpu="1",
            memory="2g",
        )

    assert ("run", "clawcu-openclaw-writer") not in docker.commands


def test_create_openclaw_searches_next_port_by_ten(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port == 18809

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        cpu="1",
        memory="2g",
    )

    assert record.port == 18809


def test_create_openclaw_retries_next_port_when_docker_bind_races(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service._is_port_available = lambda port: port in {18789, 18799}
    docker.run_errors.append(
        CommandError(
            ["docker", "run"],
            125,
            "",
            "Bind for 0.0.0.0:18789 failed: port is already allocated",
        )
    )

    record = service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        cpu="1",
        memory="2g",
    )

    assert record.port == 18799
    assert ("rm", "clawcu-openclaw-writer") in docker.commands
    stored = store.load_record("writer")
    assert stored.port == 18799
    assert stored.last_error is None
    assert [event["action"] for event in stored.history] == [
        "create_requested",
        "create_failed",
        "created",
    ]


def test_create_openclaw_persists_failed_record(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError, match="Failed to create instance 'writer'"):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    stored = store.load_record("writer")
    assert stored.status == "create_failed"
    assert stored.last_error == "boom"
    assert stored.port == 18789
    assert stored.history[-1]["action"] == "create_failed"
    assert "boom" in stored.history[-1]["error"]


def test_list_instances_preserves_failed_create_status(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    records = service.list_instances()
    assert len(records) == 1
    assert records[0].name == "writer"
    assert records[0].status == "create_failed"
    assert records[0].last_error == "boom"


def test_retry_instance_recreates_failed_instance(temp_clawcu_home, tmp_path) -> None:
    service, docker, openclaw, store = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    retried = service.retry_instance("writer")

    assert retried.status == "running"
    assert retried.port == 18789
    assert openclaw.versions[-1] == "2026.4.1"
    stored = store.load_record("writer")
    assert stored.status == "running"
    assert stored.last_error is None
    assert [event["action"] for event in stored.history[-3:]] == [
        "create_failed",
        "retry_requested",
        "created",
    ]
    assert ("rm", "clawcu-openclaw-writer") in docker.commands


def test_start_instance_persists_start_failed_status(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.stop_instance("writer")
    docker.fail_next_start = True

    with pytest.raises(RuntimeError, match="Failed to start instance 'writer'"):
        service.start_instance("writer")

    stored = store.load_record("writer")
    assert stored.status == "start_failed"
    assert stored.last_error == "port is already allocated"
    assert stored.history[-1]["action"] == "start_failed"
    listed = service.list_instances()
    assert listed[0].status == "start_failed"


def test_start_instance_clears_start_failed_after_successful_retry(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )
    service.stop_instance("writer")
    docker.fail_next_start = True

    with pytest.raises(RuntimeError):
        service.start_instance("writer")

    started = service.start_instance("writer")

    assert started.status == "running"
    assert store.load_record("writer").last_error is None


def test_retry_instance_rejects_non_failed_instance(temp_clawcu_home, tmp_path) -> None:
    service, _, _, _ = make_service(temp_clawcu_home)
    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(tmp_path / "writer"),
        port=18789,
        cpu="1",
        memory="2g",
    )

    with pytest.raises(
        ValueError,
        match="Instance 'writer' is in status 'running'. Only create_failed instances can be retried.",
    ):
        service.retry_instance("writer")


def test_create_openclaw_rejects_duplicate_name_after_failed_create(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, _ = make_service(temp_clawcu_home)
    docker.fail_next_run = True

    with pytest.raises(RuntimeError):
        service.create_openclaw(
            name="writer",
            version="2026.4.1",
            datadir=str(tmp_path / "writer"),
            port=18789,
            cpu="1",
            memory="2g",
        )

    with pytest.raises(ValueError, match="Instance 'writer' already exists."):
        service.create_openclaw(
            name="writer",
            version="2026.4.2",
            datadir=str(tmp_path / "writer-2"),
            port=18799,
            cpu="1",
            memory="2g",
        )


def test_upgrade_rolls_back_when_new_container_fails(temp_clawcu_home, tmp_path) -> None:
    service, docker, _, store = make_service(temp_clawcu_home)
    datadir = tmp_path / "writer"
    datadir.mkdir()
    (datadir / "state.txt").write_text("stable", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(datadir),
        port=3000,
        cpu="1",
        memory="2g",
    )
    docker.fail_next_run = True

    with pytest.raises(RuntimeError):
        service.upgrade_instance("writer", version="2026.4.2")

    record = store.load_record("writer")
    assert record.version == "2026.4.1"
    assert record.status == "running"
    assert record.history[-1]["action"] == "upgrade_failed"


def test_clone_instance_copies_data_and_starts_new_instance(temp_clawcu_home, tmp_path) -> None:
    service, _, _, store = make_service(temp_clawcu_home)
    source_dir = tmp_path / "writer"
    source_dir.mkdir()
    (source_dir / "memory.txt").write_text("hello", encoding="utf-8")

    service.create_openclaw(
        name="writer",
        version="2026.4.1",
        datadir=str(source_dir),
        port=3000,
        cpu="1",
        memory="2g",
    )

    clone = service.clone_instance(
        "writer",
        name="writer-exp",
        datadir=str(tmp_path / "writer-exp"),
        port=3001,
    )

    assert clone.name == "writer-exp"
    assert (Path(clone.datadir) / "memory.txt").read_text(encoding="utf-8") == "hello"
    assert store.load_record("writer-exp").port == 3001
