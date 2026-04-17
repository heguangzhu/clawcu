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

    @abstractmethod
    def instance_provider_summary(self, service: "ClawCUService", record: InstanceRecord) -> dict[str, str]:
        raise NotImplementedError

    @abstractmethod
    def instance_agent_summaries(self, service: "ClawCUService", record: InstanceRecord) -> list[dict[str, str]]:
        raise NotImplementedError

    @abstractmethod
    def local_instance_summaries(self, service: "ClawCUService") -> list[dict]:
        raise NotImplementedError

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
