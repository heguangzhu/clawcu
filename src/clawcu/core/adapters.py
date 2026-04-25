from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from clawcu.core.models import AccessInfo, ContainerRunSpec, InstanceRecord, InstanceSpec
from clawcu.core.provider_models import CanonicalProvider  # noqa: F401

if TYPE_CHECKING:
    from clawcu.core.service import ClawCUService


class ServiceAdapter(ABC):
    service_name: str
    display_name: str
    default_port: int

    # Review-1 §3: A2A protocol defaults live on the adapter so adding a
    # third service doesn't require editing ``clawcu.a2a.card`` — the
    # control-plane layer stays free of per-service knowledge. The
    # in-module tables in ``card.py`` are now a fallback used only when
    # a ``ClawCUService`` handle is not available (e.g. unit tests that
    # construct cards from lightweight ``FakeRecord`` fixtures).
    a2a_skills: tuple[str, ...] = ("chat",)
    a2a_role: str = ""  # empty → card.py templates "{service} local agent"
    a2a_plugin_port_offsets: tuple[int, ...] = (0,)

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

    def removed_instance_spec(
        self,
        service: "ClawCUService",
        root: Path,
        *,
        version: str | None = None,
    ):
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

    # ── canonical-provider contract (new) ───────────────────────────
    @abstractmethod
    def bundle_to_canonical(
        self,
        service: "ClawCUService",
        bundle: dict[str, object],
    ) -> "CanonicalProvider":
        """Read this service's native bundle shape into canonical form.

        Raises ``ProviderTranslationError`` if the bundle is malformed
        or missing required fields for this service.
        """
        raise NotImplementedError

    @abstractmethod
    def write_canonical(
        self,
        service: "ClawCUService",
        canonical: "CanonicalProvider",
        record: InstanceRecord,
        *,
        agent: str = "main",
        persist: bool = False,
        dry_run: bool = False,
    ) -> dict[str, str]:
        """Render canonical into this service's instance files.

        Returns a result dict (provider, instance, agent, runtime_dir or
        config_path/env_path, env_key, persist, primary, fallbacks).
        Raises ``IncompatibleCredentialError`` if canonical's auth_type
        is unsupported by this service. When ``dry_run=True`` no files
        are written; the result dict still lists planned paths so
        ``plan_apply_provider`` can render them.
        """
        raise NotImplementedError

    @abstractmethod
    def provider_models(self, service: "ClawCUService", bundle: dict[str, object]) -> list[str]:
        raise NotImplementedError
