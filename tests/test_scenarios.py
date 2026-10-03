"""Every eval scenario must load and serve all tools through the real server code path."""

import json
from pathlib import Path

import pytest
import yaml

from homelab_agent import server

SCENARIOS = sorted(p for p in (Path(__file__).parent.parent / "evals" / "scenarios").glob("*.yaml") if not p.name.startswith("_"))
REQUIRED = {"category", "prompt", "expected_status", "expected_targets", "rubric", "patch", "actions"}


@pytest.fixture
def serve(monkeypatch):
    def load(path):
        monkeypatch.setenv("HOMELAB_FIXTURE", str(path))
        for f in (server.fixture, server.config, server.proxmox, server.docker_hosts):
            f.cache_clear()
        return server

    yield load
    for f in (server.fixture, server.config, server.proxmox, server.docker_hosts):
        f.cache_clear()


def test_scenario_count():
    assert len(SCENARIOS) == 21


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario_serves_every_tool(path, serve):
    meta = yaml.safe_load(path.read_text())
    assert REQUIRED <= meta.keys(), REQUIRED - meta.keys()
    assert set(meta["expected_status"]) <= {"healthy", "degraded", "incident"}

    s = serve(path)
    guests = s.list_guests()
    assert guests and all(g["name"] for g in guests)
    for g in guests:
        s.guest_details(g["vmid"])
    s.storage_usage()
    for t in s.recent_tasks(errors_only=False, limit=50):
        assert "<untrusted_data" in s.task_log(t["node"], t["upid"])
    for host in s.list_docker_hosts():
        for c in s.list_containers(host):
            s.inspect_container(host, c["name"])
            logs = s.container_logs(host, c["name"], tail=50, since_minutes=60 * 24 * 60)
            assert logs.count("</untrusted_data>") == 1  # payloads can't close the wrapper

    # Secrets in the fixtures must never come out of the tools.
    everything = json.dumps([s.inspect_container(h, c["name"]) for h in s.list_docker_hosts() for c in s.list_containers(h)])
    assert "Sup3rS3cret" not in everything and "7F3B-AAAA" not in everything
