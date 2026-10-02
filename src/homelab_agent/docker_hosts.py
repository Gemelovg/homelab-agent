"""Read-only Docker access to containers running inside the Proxmox LXCs.

Only hosts listed in config.yaml can be reached; the model can't pass in arbitrary URLs.
"""

from datetime import UTC, datetime, timedelta

import docker

from .config import validate_name
from .safety import redact, redact_env

COMPOSE_LABELS = (
    "com.docker.compose.project",
    "com.docker.compose.service",
    "com.docker.compose.project.config_files",
    "com.docker.compose.project.working_dir",
)


class DockerHosts:
    def __init__(self, hosts: dict[str, str]):
        self.hosts = hosts
        self._clients: dict[str, docker.DockerClient] = {}

    def client(self, host: str) -> docker.DockerClient:
        if host not in self.hosts:
            raise ValueError(f"Unknown docker host {host!r}. Known hosts: {sorted(self.hosts)}")
        if host not in self._clients:
            self._clients[host] = docker.DockerClient(base_url=self.hosts[host], timeout=20)
        return self._clients[host]

    def _container(self, host: str, name: str):
        validate_name(name, "container")
        return self.client(host).containers.get(name)

    def containers(self, host: str) -> list[dict]:
        out = []
        for c in self.client(host).containers.list(all=True):
            state = c.attrs["State"]
            out.append(
                {
                    "name": c.name,
                    "image": c.attrs["Config"]["Image"],
                    "status": state["Status"],
                    "health": state.get("Health", {}).get("Status"),
                    "exit_code": state.get("ExitCode"),
                    "oom_killed": state.get("OOMKilled"),
                    "restart_count": c.attrs.get("RestartCount"),
                    "started_at": state.get("StartedAt"),
                }
            )
        return sorted(out, key=lambda c: c["name"])

    def logs(self, host: str, name: str, tail: int, since_minutes: int | None) -> str:
        kwargs = {"tail": tail, "timestamps": True}
        if since_minutes:
            kwargs["since"] = datetime.now(UTC) - timedelta(minutes=since_minutes)
        return self._container(host, name).logs(**kwargs).decode("utf-8", errors="replace")

    def inspect(self, host: str, name: str) -> dict:
        a = self._container(host, name).attrs
        state = a["State"]
        health = state.get("Health") or {}
        labels = a["Config"].get("Labels") or {}
        return {
            "name": a["Name"].lstrip("/"),
            "image": a["Config"]["Image"],
            "created": a["Created"],
            "state": {k: state.get(k) for k in ("Status", "ExitCode", "Error", "OOMKilled", "StartedAt", "FinishedAt")},
            "restart_count": a.get("RestartCount"),
            "restart_policy": a["HostConfig"].get("RestartPolicy"),
            "health": {
                "status": health.get("Status"),
                "failing_streak": health.get("FailingStreak"),
                "recent_checks": [
                    {"exit_code": h.get("ExitCode"), "output": redact(h.get("Output", ""))[:500]}
                    for h in (health.get("Log") or [])[-3:]
                ],
            },
            "memory_limit_mb": (a["HostConfig"].get("Memory") or 0) // 1024**2 or None,
            "ports": a["NetworkSettings"].get("Ports"),
            "networks": list((a["NetworkSettings"].get("Networks") or {}).keys()),
            "mounts": [
                {"source": m.get("Source"), "destination": m.get("Destination"), "rw": m.get("RW")}
                for m in a.get("Mounts", [])
            ],
            "env": redact_env(a["Config"].get("Env") or []),
            "compose": {k.removeprefix("com.docker.compose."): labels[k] for k in COMPOSE_LABELS if k in labels},
        }
