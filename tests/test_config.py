import pytest

from homelab_agent.config import ConfigError, load_config
from homelab_agent.docker_hosts import DockerHosts


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_loads_docker_hosts(tmp_path):
    cfg = load_config(write(tmp_path, "docker_hosts:\n  media: tcp://10.0.0.20:2375\n"))
    assert cfg.docker_hosts == {"media": "tcp://10.0.0.20:2375"}
    assert cfg.proxmox is None


def test_rejects_unsupported_scheme(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "docker_hosts:\n  media: http://10.0.0.20\n"))


def test_requires_token_in_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PROXMOX_TOKEN_VALUE", raising=False)
    monkeypatch.chdir(tmp_path)  # keep a real .env from leaking in
    path = write(tmp_path, "proxmox:\n  host: pve\n  user: agent@pve\n  token_name: mcp\n")
    with pytest.raises(ConfigError, match="PROXMOX_TOKEN_VALUE"):
        load_config(path)


def test_unknown_docker_host_is_rejected():
    with pytest.raises(ValueError, match="Unknown docker host"):
        DockerHosts({"media": "tcp://x:2375"}).client("evil")


def test_container_names_are_validated():
    hosts = DockerHosts({"media": "tcp://x:2375"})
    with pytest.raises(ValueError, match="Invalid container"):
        hosts._container("media", "../../etc")
