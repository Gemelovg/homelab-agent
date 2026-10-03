"""Load and validate config. Secrets come from the environment, never from the YAML file."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Host and container names the model may pass to tools. Anything else is rejected
# before it reaches Docker or Proxmox.
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
ALLOWED_DOCKER_SCHEMES = ("ssh://", "tcp://")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProxmoxConfig:
    host: str
    user: str
    token_name: str
    token_value: str
    verify_ssl: bool = True


@dataclass(frozen=True)
class AgentConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_steps: int = 15


@dataclass(frozen=True)
class NtfyConfig:
    url: str
    topic: str
    token: str | None = None


@dataclass(frozen=True)
class ActionsConfig:
    """Phase 4: what the agent may propose, and the separate write credentials to carry it out."""

    approval_url: str  # where the phone reaches the approval service, e.g. http://192.168.1.50:8787
    allowed_containers: dict[str, list[str]]  # docker host -> containers the agent may restart/start
    allowed_guests: list[int]  # Proxmox vmids the agent may start
    docker_write: dict[str, str]  # docker host -> restart/start-only socket proxy URL
    proxmox_power_user: str | None = None
    proxmox_power_token_name: str | None = None
    proxmox_power_token_value: str | None = None
    expiry_minutes: int = 30
    store_dir: Path = Path("actions")


@dataclass(frozen=True)
class Config:
    proxmox: ProxmoxConfig | None
    docker_hosts: dict[str, str]
    audit_log: Path
    max_log_lines: int = 500
    agent: AgentConfig = AgentConfig()
    ntfy: NtfyConfig | None = None
    actions: ActionsConfig | None = None


def validate_name(value: str, kind: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"Invalid {kind} name: {value!r}")
    return value


def load_config(path: str | Path | None = None) -> Config:
    # Only the project's .env (never a parent directory's), and it wins over stale shell variables.
    load_dotenv(Path(".env"), override=True)
    path = Path(path or os.environ.get("HOMELAB_CONFIG", "config.yaml"))
    if not path.exists():
        raise ConfigError(f"Config file not found: {path} (copy config.example.yaml)")
    raw = yaml.safe_load(path.read_text()) or {}

    proxmox = None
    if px := raw.get("proxmox"):
        token_value = os.environ.get("PROXMOX_TOKEN_VALUE")
        if not token_value:
            raise ConfigError("proxmox is configured but PROXMOX_TOKEN_VALUE is not set")
        proxmox = ProxmoxConfig(
            host=px["host"],
            user=px["user"],
            token_name=px["token_name"],
            token_value=token_value,
            verify_ssl=px.get("verify_ssl", True),
        )

    docker_hosts = {}
    for name, url in (raw.get("docker_hosts") or {}).items():
        try:
            validate_name(name, "docker host")
        except ValueError as e:
            raise ConfigError(str(e)) from None
        if not url.startswith(ALLOWED_DOCKER_SCHEMES):
            raise ConfigError(f"docker_hosts.{name}: URL must start with one of {ALLOWED_DOCKER_SCHEMES}")
        docker_hosts[name] = url

    ntfy = None
    if nt := raw.get("ntfy"):
        ntfy = NtfyConfig(url=nt["url"].rstrip("/"), topic=nt["topic"], token=os.environ.get("NTFY_TOKEN"))

    actions = None
    if ac := raw.get("actions"):
        allow = ac.get("allow") or {}
        write = ac.get("write") or {}
        for host, url in (write.get("docker_hosts") or {}).items():
            if not url.startswith(ALLOWED_DOCKER_SCHEMES):
                raise ConfigError(f"actions.write.docker_hosts.{host}: URL must start with one of {ALLOWED_DOCKER_SCHEMES}")
        actions = ActionsConfig(
            approval_url=ac["approval_url"].rstrip("/"),
            allowed_containers={h: list(names) for h, names in (allow.get("containers") or {}).items()},
            allowed_guests=[int(v) for v in allow.get("guests") or []],
            docker_write=dict(write.get("docker_hosts") or {}),
            proxmox_power_user=write.get("proxmox_user"),
            proxmox_power_token_name=write.get("proxmox_token_name"),
            proxmox_power_token_value=os.environ.get("PROXMOX_POWER_TOKEN_VALUE"),
            expiry_minutes=int(ac.get("expiry_minutes", 30)),
            store_dir=Path(ac.get("store_dir", "actions")),
        )

    return Config(
        proxmox=proxmox,
        docker_hosts=docker_hosts,
        audit_log=Path(raw.get("audit_log", "audit.jsonl")),
        max_log_lines=int(raw.get("max_log_lines", 500)),
        agent=AgentConfig(**(raw.get("agent") or {})),
        ntfy=ntfy,
        actions=actions,
    )
