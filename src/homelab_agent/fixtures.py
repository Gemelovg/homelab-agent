"""Fixture mode: serve a fake homelab from YAML instead of real Proxmox/Docker.

Used by the eval suite. The fakes sit at the lowest level, emulating the proxmoxer
API and the docker SDK, so the real Proxmox/DockerHosts code, the safety layer and
the MCP server all run unchanged on top. Enabled with HOMELAB_FIXTURE=<scenario.yaml>.

A scenario file holds a `patch` that is deep-merged over `_base.yaml` in the same
directory. Anything else in the scenario (prompt, rubric, expected status) is eval
metadata and never reaches the server's tools.
"""

import copy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import docker.errors
import yaml

from .config import Config
from .docker_hosts import DockerHosts
from .proxmox import GB, MB, Proxmox

# The fake homelab's "now". Fixture timestamps are written relative to this.
FIXTURE_NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_homelab(scenario_path: str | Path) -> dict:
    scenario_path = Path(scenario_path)
    base = yaml.safe_load((scenario_path.parent / "_base.yaml").read_text())
    scenario = yaml.safe_load(scenario_path.read_text()) or {}
    return deep_merge(base, scenario.get("patch") or {})


def fixture_config(homelab: dict) -> Config:
    return Config(proxmox=None, docker_hosts={h: "tcp://fixture:2375" for h in homelab["docker_hosts"]}, audit_log=Path("/dev/null"))


# ---------------------------------------------------------------- Proxmox


class _ApiPath:
    """Mimics proxmoxer's chained access: api.nodes("pve")("lxc")(101).config.get()."""

    def __init__(self, resolve, parts=()):
        self._resolve, self._parts = resolve, parts

    def __getattr__(self, name):
        return _ApiPath(self._resolve, (*self._parts, name))

    def __call__(self, *args):
        return _ApiPath(self._resolve, (*self._parts, *(str(a) for a in args)))

    def get(self, **params):
        return self._resolve(self._parts, params)


class FixtureProxmox(Proxmox):
    def __init__(self, homelab: dict):
        self.px = homelab["proxmox"]
        self.api = _ApiPath(self._resolve)

    def _raw_guest(self, vmid, g: dict) -> dict:
        running = g.get("status", "running") == "running"
        return {
            "vmid": int(vmid),
            "name": g["name"],
            "type": g.get("type", "lxc"),
            "node": self.px["node"],
            "status": g.get("status", "running"),
            "cpu": g.get("cpu_pct", 0.5) / 100 if running else 0,
            "maxcpu": g.get("cores", 2),
            "mem": int(g.get("mem_used_mb", 256) * MB) if running else 0,
            "maxmem": int(g.get("mem_max_mb", 1024) * MB),
            "disk": int(g.get("disk_used_gb", 1) * GB),
            "maxdisk": int(g.get("disk_max_gb", 8) * GB),
            "uptime": int(g.get("uptime_h", 300) * 3600) if running else 0,
        }

    def _resolve(self, parts: tuple, params: dict):
        guests, node = self.px["guests"], self.px["node"]
        tasks = sorted(self.px.get("tasks", {}).values(), key=lambda t: -t["starttime"])
        match parts:
            case ("cluster", "resources"):
                return [self._raw_guest(vmid, g) for vmid, g in guests.items()]
            case ("nodes",):
                return [{"node": node}]
            case ("nodes", _, "storage"):
                return [
                    {
                        "storage": name,
                        "type": s["type"],
                        "active": int(s.get("active", True)),
                        "total": int(s["total_gb"] * GB),
                        "used": int(s["used_gb"] * GB),
                        "avail": int((s["total_gb"] - s["used_gb"]) * GB),
                    }
                    for name, s in self.px["storage"].items()
                ]
            case ("cluster", "tasks"):
                return [{k: v for k, v in t.items() if k != "log"} | {"node": node} for t in tasks]
            case ("nodes", _, "tasks", upid, "log"):
                task = next((t for t in tasks if t["upid"] == upid), None)
                if task is None:
                    raise ValueError(f"No such task: {upid}")
                lines = task.get("log", "").splitlines()[: params.get("limit", 500)]
                return [{"n": i + 1, "t": line} for i, line in enumerate(lines)]
            case ("nodes", _, _, vmid, "config"):
                return dict(guests[int(vmid)].get("config", {}))
        raise NotImplementedError(f"Fixture has no Proxmox endpoint {'/'.join(parts)}")


# ---------------------------------------------------------------- Docker


def _timestamp(line: str) -> datetime | None:
    try:
        return datetime.fromisoformat(line.split(" ", 1)[0])
    except ValueError:  # continuation line of a multi-line entry
        return None


class FakeContainer:
    def __init__(self, name: str, c: dict):
        self.name = name
        self._c = c
        health = c.get("health")
        state = {
            "Status": c.get("status", "running"),
            "ExitCode": c.get("exit_code", 0),
            "Error": c.get("error", ""),
            "OOMKilled": c.get("oom_killed", False),
            "StartedAt": c.get("started_at", "2026-09-17T03:12:44Z"),
            "FinishedAt": c.get("finished_at", "0001-01-01T00:00:00Z"),
        }
        if health:
            state["Health"] = {
                "Status": health,
                "FailingStreak": c.get("failing_streak", 0),
                "Log": [{"ExitCode": h["exit_code"], "Output": h["output"]} for h in c.get("health_log", [])],
            }
        compose = c.get("compose", {"project": name, "service": name})
        self.attrs = {
            "Name": f"/{name}",
            "Created": c.get("created", "2026-09-17T03:12:40Z"),
            "Config": {
                "Image": c["image"],
                "Env": c.get("env", ["TZ=Europe/Madrid"]),
                "Labels": {f"com.docker.compose.{k}": v for k, v in compose.items()},
            },
            "State": state,
            "RestartCount": c.get("restart_count", 0),
            "HostConfig": {
                "RestartPolicy": {"Name": c.get("restart_policy", "unless-stopped"), "MaximumRetryCount": 0},
                "Memory": int(c.get("memory_limit_mb", 0) * MB),
            },
            "NetworkSettings": {
                "Ports": {
                    port: [{"HostIp": "0.0.0.0", "HostPort": str(host_port)}]
                    for port, host_port in c.get("ports", {}).items()
                },
                "Networks": {n: {} for n in c.get("networks", [f"{compose['project']}_default"])},
            },
            "Mounts": [{"Source": src, "Destination": dst, "RW": True} for src, dst in c.get("mounts", {}).items()],
        }

    def logs(self, tail="all", timestamps=False, since=None, **_):
        lines = self._c.get("logs", "").strip().splitlines()
        if since is not None:
            # `since` is computed from the real clock; map it onto the fixture clock.
            cutoff = FIXTURE_NOW - (datetime.now(UTC) - since)
            lines = [line for line in lines if (ts := _timestamp(line)) is None or ts >= cutoff]
        if tail != "all":
            lines = lines[-tail:]
        if not timestamps:
            lines = [line.split(" ", 1)[1] if " " in line else line for line in lines]
        return ("\n".join(lines) + "\n").encode()


class _FakeContainers:
    def __init__(self, containers: dict):
        self._containers = {name: FakeContainer(name, c) for name, c in containers.items()}

    def list(self, all=False):
        return [c for c in self._containers.values() if all or c.attrs["State"]["Status"] == "running"]

    def get(self, name):
        if name not in self._containers:
            raise docker.errors.NotFound(f"No such container: {name}")
        return self._containers[name]


class FixtureDockerHosts(DockerHosts):
    def __init__(self, homelab: dict):
        super().__init__({h: "tcp://fixture:2375" for h in homelab["docker_hosts"]})
        self._fakes = {h: _FakeContainers(d["containers"]) for h, d in homelab["docker_hosts"].items()}

    def client(self, host: str):
        if host not in self.hosts:  # same allowlist behaviour as the real class
            raise ValueError(f"Unknown docker host {host!r}. Known hosts: {sorted(self.hosts)}")
        return SimpleNamespace(containers=self._fakes[host])
