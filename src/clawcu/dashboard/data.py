from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from clawcu.core.service import ClawCUService


def _normalize_version(value: str | None) -> str:
    if not value:
        return ""
    return value[1:] if value.startswith("v") else value


@dataclass
class DashboardData:
    env: dict[str, Any]
    managed: list[dict[str, Any]]
    local: list[dict[str, Any]]
    agents: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    providers: list[dict[str, Any]]
    upgrades: dict[str, dict[str, Any]]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        managed_by_name = {row["name"]: row for row in self.managed}
        agents_by_instance: dict[str, list[dict[str, Any]]] = {}
        for row in self.agents:
            agents_by_instance.setdefault(row["instance"], []).append(row)

        for name, rows in agents_by_instance.items():
            managed_by_name.setdefault(name, {})["agents"] = rows

        upgrade_lane = []
        for row in self.managed:
            versions = self.upgrades.get(row["name"], {})
            stable_remote = versions.get("stable_remote_versions") or []
            latest_remote = stable_remote[-1] if stable_remote else None
            current = str(row.get("version") or "")
            if latest_remote and _normalize_version(latest_remote) != _normalize_version(current):
                upgrade_lane.append(
                    {
                        "name": row["name"],
                        "service": row.get("service", "-"),
                        "current_version": current,
                        "target_version": latest_remote,
                        "local_image_available": latest_remote in (versions.get("local_images") or []),
                        "history_versions": versions.get("history") or [],
                    }
                )

        attention_queue: list[dict[str, str]] = []
        for row in self.managed:
            history = row.get("history") or []
            if any(event.get("action") == "rollback" for event in history if isinstance(event, dict)):
                attention_queue.append(
                    {
                        "kind": "rollback",
                        "name": row["name"],
                        "service": row.get("service", "-"),
                        "title": f"{row['name']} has rollback history",
                        "body": "Snapshot and history should stay visible because this instance has already moved backward once.",
                    }
                )
            failures = [
                event
                for event in history
                if isinstance(event, dict) and str(event.get("action", "")).endswith("failed")
            ]
            if failures:
                attention_queue.append(
                    {
                        "kind": "failure-history",
                        "name": row["name"],
                        "service": row.get("service", "-"),
                        "title": f"{row['name']} has startup or create failures in history",
                        "body": "Current running status is not enough on its own; the dashboard should still surface how this instance got here.",
                    }
                )

        for row in self.removed:
            attention_queue.append(
                {
                    "kind": "removed",
                    "name": row["name"],
                    "service": row.get("service", "-"),
                    "title": f"{row['name']} can be recovered",
                    "body": "Removed datadir is still present under CLAWCU_HOME and should stay visible in the recovery shelf.",
                }
            )

        summary = {
            "managed_count": len(self.managed),
            "local_count": len(self.local),
            "running_count": sum(1 for row in self.managed if row.get("status") == "running"),
            "removed_count": len(self.removed),
            "provider_count": len(self.providers),
            "upgrade_ready_count": len(upgrade_lane),
            "attention_count": len(attention_queue),
        }

        return {
            "env": self.env,
            "managed": self.managed,
            "local": self.local,
            "agents": self.agents,
            "removed": self.removed,
            "providers": self.providers,
            "upgrades": self.upgrades,
            "upgrade_lane": upgrade_lane,
            "attention_queue": attention_queue[:8],
            "summary": summary,
            "generated_at": self.generated_at,
        }


def collect_dashboard(service: ClawCUService) -> dict[str, Any]:
    env = {
        "clawcu_version": "clawcu",
        "clawcu_home": service.get_clawcu_home(),
        "openclaw_image_repo": service.get_openclaw_image_repo(),
        "hermes_image_repo": service.get_hermes_image_repo(),
    }
    managed = service.list_instance_summaries()
    local = service.list_local_instance_summaries()
    agents = service.list_agent_summaries()
    removed = service.list_removed_instance_summaries()
    providers = service.list_providers()
    available_versions = service.list_service_available_versions(include_remote=True)

    upgrades: dict[str, dict[str, Any]] = {}
    for row in managed:
        versions = service.list_upgradable_versions(row["name"], include_remote=False)
        remote_payload = available_versions.get(row["service"], {}) if isinstance(available_versions, dict) else {}
        remote_versions = remote_payload.get("versions")
        versions["remote_versions"] = remote_versions
        versions["remote_error"] = remote_payload.get("error")
        versions["remote_registry"] = remote_payload.get("registry")
        versions["stable_remote_versions"] = [
            version for version in (remote_versions or []) if "-" not in version and version != "latest"
        ]
        upgrades[row["name"]] = versions

    return DashboardData(
        env=env,
        managed=managed,
        local=local,
        agents=agents,
        removed=removed,
        providers=providers,
        upgrades=upgrades,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    ).to_dict()


def instance_inspect(service: ClawCUService, name: str) -> dict[str, Any]:
    record = service._persist_live_status(service.store.load_record(name))
    adapter = service.adapter_for_record(record)
    access = adapter.access_info(service, record)
    container = service.docker.inspect_container(record.container_name)
    return {
        "instance": record.to_dict(),
        "snapshots": service._snapshot_summary(record),
        "access": {
            "base_url": access.base_url,
            "readiness_label": access.readiness_label,
            "auth_hint": access.auth_hint,
        },
        "container": {
            "status": (container or {}).get("State", {}).get("Status") if isinstance(container, dict) else None,
            "running": (container or {}).get("State", {}).get("Running") if isinstance(container, dict) else None,
            "started_at": (container or {}).get("State", {}).get("StartedAt") if isinstance(container, dict) else None,
            "finished_at": (container or {}).get("State", {}).get("FinishedAt") if isinstance(container, dict) else None,
            "health": ((container or {}).get("State", {}).get("Health", {}) or {}).get("Status") if isinstance(container, dict) else None,
            "config": (container or {}).get("Config", {}) if isinstance(container, dict) else {},
            "network_mode": (container or {}).get("HostConfig", {}).get("NetworkMode") if isinstance(container, dict) else None,
        },
    }


def instance_versions(service: ClawCUService, name: str) -> dict[str, Any]:
    payload = service.list_upgradable_versions(name)
    payload["stable_remote_versions"] = [
        version for version in (payload.get("remote_versions") or []) if "-" not in version and version != "latest"
    ]
    return payload


def instance_token(service: ClawCUService, name: str) -> dict[str, Any]:
    return {"name": name, "token": service.token(name), "url": service.dashboard_url(name)}
