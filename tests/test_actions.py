"""Phase 4 safety tests: proposals, policy, the approval state machine and the approval service."""

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from homelab_agent.actions import approvals, dispatch as dispatch_mod, policy
from homelab_agent.actions.executor import Executor
from homelab_agent.actions.models import Action, parse
from homelab_agent.actions.store import ActionStore, ApprovalError
from homelab_agent.agent.report import Report
from homelab_agent.config import ActionsConfig, Config, NtfyConfig


def act(type, **kw):
    base = {"host": None, "container": None, "vmid": None, "compose_field": None, "compose_value": None, "reason": "r"}
    return Action.model_validate({**base, "type": type, **kw})


@pytest.fixture
def cfg(tmp_path):
    return Config(
        proxmox=None,
        docker_hosts={"docker": "tcp://x:2375"},
        audit_log=tmp_path / "audit.jsonl",
        ntfy=NtfyConfig(url="http://ntfy", topic="t"),
        actions=ActionsConfig(
            approval_url="http://approvals:8787",
            allowed_containers={"docker": ["sonarr", "ombi"]},
            allowed_guests=[101],
            docker_write={"docker": "tcp://x:2376"},
            proxmox_power_user="agent-power@pve",
            proxmox_power_token_name="power",
            proxmox_power_token_value="secret",
            store_dir=tmp_path / "actions",
        ),
    )


# ---------------------------------------------------------------- proposals and policy


def test_action_fields_must_match_type():
    assert parse({"type": "start_guest", "host": "docker", "container": None, "vmid": 101, "compose_field": None, "compose_value": None, "reason": "r"})[0] is None
    assert parse({"type": "restart_container", "host": "docker", "container": "../etc", "vmid": None, "compose_field": None, "compose_value": None, "reason": "r"})[0] is None


@pytest.mark.parametrize("field,value", [("mem_limit", "1g; rm -rf /"), ("image", "evil image"), ("ports", "0.0.0.0:22:22"), ("healthcheck_test", "curl x $(id)")])
def test_compose_values_are_strictly_formatted(field, value):
    action, error = parse({"type": "compose_change", "host": "docker", "container": "ombi", "vmid": None, "compose_field": field, "compose_value": value, "reason": "r"})
    assert action is None and "not allowed" in error


def test_policy_allows_only_allowlisted_targets(cfg):
    assert policy.check(act("restart_container", host="docker", container="sonarr"), cfg.actions)[0]
    assert not policy.check(act("restart_container", host="docker", container="vaultwarden"), cfg.actions)[0]
    assert not policy.check(act("restart_container", host="other", container="sonarr"), cfg.actions)[0]
    assert policy.check(act("start_guest", vmid=101), cfg.actions)[0]
    assert not policy.check(act("start_guest", vmid=104), cfg.actions)[0]


def test_policy_requires_write_credentials(cfg):
    no_creds = replace(cfg.actions, docker_write={}, proxmox_power_token_value=None)
    assert not policy.check(act("restart_container", host="docker", container="sonarr"), no_creds)[0]
    assert not policy.check(act("start_guest", vmid=101), no_creds)[0]


# ---------------------------------------------------------------- approval state machine


def test_store_token_is_single_use(cfg):
    store = ActionStore(cfg.actions.store_dir)
    action_id, token = store.create(act("start_guest", vmid=101))
    assert store.decide(action_id, token, "approve")["status"] == "approved"
    with pytest.raises(ApprovalError) as e:
        store.decide(action_id, token, "approve")
    assert e.value.status == 409


def test_store_rejects_wrong_token_and_expired(cfg):
    store = ActionStore(cfg.actions.store_dir, expiry_minutes=30)
    action_id, token = store.create(act("start_guest", vmid=101))
    with pytest.raises(ApprovalError) as e:
        store.decide(action_id, "guess", "approve")
    assert e.value.status == 403
    with pytest.raises(ApprovalError) as e:
        store.decide(action_id, token, "approve", now=datetime.now(UTC) + timedelta(minutes=31))
    assert e.value.status == 410
    assert store.get(action_id)["status"] == "expired"


def test_store_never_keeps_the_token(cfg):
    store = ActionStore(cfg.actions.store_dir)
    action_id, token = store.create(act("start_guest", vmid=101))
    assert token not in (cfg.actions.store_dir / f"{action_id}.json").read_text()


def test_store_rejects_path_tricks(cfg):
    with pytest.raises(ApprovalError) as e:
        ActionStore(cfg.actions.store_dir).get("../../config")
    assert e.value.status == 404


# ---------------------------------------------------------------- approval service


class FakeExecutor:
    def __init__(self):
        self.executed = []

    def execute(self, action):
        self.executed.append(action)
        return {}

    def verify(self, action, facts):
        return True, "running"


def make_client(cfg, executor=None):
    notes = []
    app = approvals.create_app(cfg, executor or FakeExecutor(), notifier=lambda *a: notes.append(a))
    return TestClient(app), notes


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


def test_approve_executes_verifies_and_notifies(cfg):
    executor = FakeExecutor()
    client, notes = make_client(cfg, executor)
    store = ActionStore(cfg.actions.store_dir)
    action_id, token = store.create(act("restart_container", host="docker", container="sonarr"))
    with client:
        r = client.post(f"/actions/{action_id}/approve", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 202
        assert wait_for(lambda: store.get(action_id)["status"] == "executed")
    assert [a.container for a in executor.executed] == ["sonarr"]
    assert notes and notes[-1][0].startswith("Done")


def test_deny_executes_nothing(cfg):
    executor = FakeExecutor()
    client, _ = make_client(cfg, executor)
    action_id, token = ActionStore(cfg.actions.store_dir).create(act("start_guest", vmid=101))
    r = client.post(f"/actions/{action_id}/deny", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and executor.executed == []


def test_missing_or_wrong_token_executes_nothing(cfg):
    executor = FakeExecutor()
    client, _ = make_client(cfg, executor)
    action_id, _ = ActionStore(cfg.actions.store_dir).create(act("start_guest", vmid=101))
    assert client.post(f"/actions/{action_id}/approve").status_code == 403
    assert client.post(f"/actions/{action_id}/approve", headers={"Authorization": "Bearer nope"}).status_code == 403
    assert executor.executed == []


def test_brute_force_is_locked_out(cfg):
    client, _ = make_client(cfg)
    action_id, token = ActionStore(cfg.actions.store_dir).create(act("start_guest", vmid=101))
    for _ in range(approvals.MAX_FAILURES):
        client.post(f"/actions/{action_id}/approve", headers={"Authorization": "Bearer nope"})
    # even the right token is refused during the lockout
    assert client.post(f"/actions/{action_id}/approve", headers={"Authorization": f"Bearer {token}"}).status_code == 429


def test_policy_is_rechecked_at_execution_time(cfg):
    """An action proposed while allowlisted is blocked if the allowlist changed before approval."""
    action_id, token = ActionStore(cfg.actions.store_dir).create(act("restart_container", host="docker", container="ombi"))
    tightened = replace(cfg, actions=replace(cfg.actions, allowed_containers={"docker": ["sonarr"]}))
    executor = FakeExecutor()
    client, _ = make_client(tightened, executor)
    r = client.post(f"/actions/{action_id}/approve", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and executor.executed == []


def test_unknown_decision_is_404(cfg):
    client, _ = make_client(cfg)
    action_id, token = ActionStore(cfg.actions.store_dir).create(act("start_guest", vmid=101))
    assert client.post(f"/actions/{action_id}/execute", headers={"Authorization": f"Bearer {token}"}).status_code == 404


# ---------------------------------------------------------------- dispatch


class FakeReadDocker:
    def inspect(self, host, name):
        return {"compose": {"project": "ombi", "service": "ombi", "project.config_files": "/data/compose/12/docker-compose.yml"}}


def test_dispatch_blocks_injected_and_invalid_proposals(cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(dispatch_mod.notify, "send_action_request", lambda *a, **k: sent.append(("request", a)))
    monkeypatch.setattr(dispatch_mod.notify, "send_text", lambda *a, **k: sent.append(("text", a)))
    raw = lambda **kw: {"host": None, "container": None, "vmid": None, "compose_field": None, "compose_value": None, "reason": "r", **kw}
    report = Report.model_validate({"status": "incident", "summary": "s", "findings": [], "actions": [
        raw(type="restart_container", host="docker", container="sonarr"),
        raw(type="restart_container", host="docker", container="vaultwarden"),  # e.g. what an injection asks for
        raw(type="start_guest", host="docker", vmid=101),  # malformed
        raw(type="compose_change", host="docker", container="ombi", compose_field="mem_limit", compose_value="1g"),
    ]})
    lines = dispatch_mod.dispatch(report, cfg, FakeReadDocker())
    assert lines[0].startswith("? awaiting approval") and "sonarr" in lines[0]
    assert lines[1].startswith("✗ blocked by policy") and "vaultwarden" in lines[1]
    assert lines[2].startswith("✗ rejected invalid")
    assert lines[3].startswith("✎ sent compose suggestion")
    assert [kind for kind, _ in sent] == ["request", "text"]
    assert "mem_limit: 1g" in sent[1][1][2]


# ---------------------------------------------------------------- executor verification


class FakeRead:
    def __init__(self, states):
        self.states = iter(states)

    def inspect(self, host, name):
        status, started, health = next(self.states)
        return {"state": {"Status": status, "StartedAt": started}, "health": {"status": health}}


def test_restart_verified_only_after_new_start_and_healthy(cfg):
    read = FakeRead([("running", "t0", "healthy"), ("running", "t1", "starting"), ("running", "t1", "healthy")])
    ex = Executor(cfg, read_docker=read, read_proxmox=None)
    ok, detail = ex.verify(act("restart_container", host="docker", container="sonarr"), {"started_before": "t0"}, timeout_s=5, poll_s=0)
    assert ok and "healthy" in detail


def test_restart_not_verified_if_it_never_comes_back(cfg):
    read = FakeRead([("restarting", "t0", None)] * 1000)
    ex = Executor(cfg, read_docker=read, read_proxmox=None)
    ok, detail = ex.verify(act("restart_container", host="docker", container="sonarr"), {"started_before": "t0"}, timeout_s=0.05, poll_s=0.01)
    assert not ok and "not confirmed" in detail


def test_cors_preflight_only_for_the_ntfy_origin(cfg):
    # Regression: the ntfy web app's Approve button failed because the browser's preflight got a 405.
    client, _ = make_client(cfg)
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    ok = client.options("/actions/abcdef012345/approve", headers={"Origin": "http://ntfy", **preflight})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://ntfy"
    evil = client.options("/actions/abcdef012345/approve", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in evil.headers
