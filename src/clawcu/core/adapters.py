from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from clawcu.core.models import AccessInfo, ContainerRunSpec, InstanceRecord, InstanceSpec

if TYPE_CHECKING:
    from clawcu.core.service import ClawCUService


class ServiceAdapter(ABC):
    service_name: str
    display_name: str
    default_port: int

    @abstractmethod
    def prepare_artifact(self, version: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def default_datadir(self, service: "ClawCUService", name: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def default_auth_mode(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def build_spec(
        self,
        service: "ClawCUService",
        *,
        name: str,
        version: str,
        datadir: str | None,
        port: int | None,
        cpu: str,
        memory: str,
    ) -> InstanceSpec:
        raise NotImplementedError

    @abstractmethod
    def env_path(self, service: "ClawCUService", record: InstanceRecord | str) -> Path:
        raise NotImplementedError

    @abstractmethod
    def run_spec(self, service: "ClawCUService", record: InstanceRecord) -> ContainerRunSpec:
        raise NotImplementedError

    @abstractmethod
    def configure_before_run(self, service: "ClawCUService", record: InstanceRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_readiness(self, service: "ClawCUService", record: InstanceRecord) -> InstanceRecord:
        raise NotImplementedError

    @abstractmethod
    def access_info(self, service: "ClawCUService", record: InstanceRecord) -> AccessInfo:
        raise NotImplementedError

    def display_port(self, service: "ClawCUService", record: InstanceRecord) -> int:
        return record.port

    @abstractmethod
    def lifecycle_summary(self, service: "ClawCUService", action: str, record: InstanceRecord) -> str:
        raise NotImplementedError

    @abstractmethod
    def configure_instance(
        self,
        service: "ClawCUService",
        name: str,
        extra_args: list[str] | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def exec_env(self, service: "ClawCUService", record: InstanceRecord) -> dict[str, str]:
        raise NotImplementedError

    def container_env_matches(
        self,
        service: "ClawCUService",
        record: InstanceRecord,
        inspection: dict | None,
    ) -> bool:
        return True

    def normalize_exec_command(
        self,
        service: "ClawCUService",
        record: InstanceRecord,
        command: list[str],
    ) -> list[str]:
        return command

    @abstractmethod
    def tui_instance(self, service: "ClawCUService", name: str, *, agent: str = "main") -> None:
        raise NotImplementedError

    def token(self, service: "ClawCUService", name: str) -> str:
        raise ValueError(f"`clawcu token` is not supported for {self.display_name} instances.")

    def approve_pairing(
        self,
        service: "ClawCUService",
        name: str,
        request_id: str | None = None,
    ) -> str:
        raise ValueError(f"`clawcu approve` is not supported for {self.display_name} instances.")

    def list_pending_pairings(
        self,
        service: "ClawCUService",
        name: str,
    ) -> list[dict[str, object]]:
        """Return pending pairing requests as a list of dicts.

        Each dict should at minimum contain ``requestId``. Services that
        do not implement device pairing raise ``ValueError`` (mirrors the
        default ``approve_pairing`` behavior).
        """
        raise ValueError(f"`clawcu approve --list` is not supported for {self.display_name} instances.")

    def list_agents(
        self,
        service: "ClawCUService",
        record: "InstanceRecord",
    ) -> list[str]:
        """Return the list of agent names available for this instance.

        Default falls back to the agents discovered by
        ``instance_agent_summaries`` — subclasses can override for a
        service-native listing. Returns the canonical ``main`` when
        nothing is configured yet, so `--list-agents` always has at
        least one entry.
        """
        summaries = self.instance_agent_summaries(service, record)
        names: list[str] = []
        for summary in summaries:
            agent_name = str(summary.get("agent") or "").strip()
            if agent_name and agent_name not in names:
                names.append(agent_name)
        if "main" not in names:
            names.insert(0, "main")
        return names

    @abstractmethod
    def instance_provider_summary(self, service: "ClawCUService", record: InstanceRecord) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def instance_agent_summaries(self, service: "ClawCUService", record: InstanceRecord) -> list[dict[str, str]]:
        raise NotImplementedError

    @abstractmethod
    def local_instance_summaries(self, service: "ClawCUService") -> list[dict]:
        raise NotImplementedError

    def removed_instance_summary(self, service: "ClawCUService", root: Path) -> dict | None:
        return None

    @abstractmethod
    def local_agent_summaries(self, service: "ClawCUService") -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def scan_model_config_bundles(
        self,
        service: "ClawCUService",
        root: Path,
        env_values: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        raise NotImplementedError

    @abstractmethod
    def apply_provider(
        self,
        service: "ClawCUService",
        bundle: dict[str, object],
        instance: str,
        *,
        agent: str = "main",
        primary: str | None = None,
        fallbacks: list[str] | None = None,
        persist: bool = False,
    ) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def provider_models(self, service: "ClawCUService", bundle: dict[str, object]) -> list[str]:
        raise NotImplementedError
