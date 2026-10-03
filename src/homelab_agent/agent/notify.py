"""Push the report to ntfy."""

import requests

from ..config import NtfyConfig
from .report import Report, to_markdown

PRIORITY = {"healthy": "low", "degraded": "default", "incident": "high"}
TAGS = {"healthy": "white_check_mark", "degraded": "warning", "incident": "rotating_light"}


def send(cfg: NtfyConfig, report: Report, footer: str = "") -> None:
    headers = {
        # HTTP headers must be latin-1; keep the title ASCII and let Tags supply the emoji.
        "Title": f"Homelab: {report.status}",
        "Priority": PRIORITY[report.status],
        "Tags": TAGS[report.status],
        "Markdown": "yes",
    }
    if cfg.token:
        headers["Authorization"] = f"Bearer {cfg.token}"
    body = to_markdown(report) + (f"\n\n_{footer}_" if footer else "")
    resp = requests.post(f"{cfg.url}/{cfg.topic}", data=body.encode(), headers=headers, timeout=10)
    resp.raise_for_status()


def _post_json(cfg: NtfyConfig, payload: dict) -> None:
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    resp = requests.post(cfg.url, json={"topic": cfg.topic, **payload}, headers=headers, timeout=10)
    resp.raise_for_status()


def send_text(cfg: NtfyConfig, title: str, message: str, tags: str = "") -> None:
    _post_json(cfg, {"title": title, "message": message, "tags": [tags] if tags else []})


def send_action_request(cfg: NtfyConfig, approval_url: str, action_id: str, token: str, description: str, reason: str, expiry_minutes: int) -> None:
    """One notification per action, with Approve/Deny buttons that call the approval service."""

    def button(label: str, decision: str) -> dict:
        return {
            "action": "http",
            "label": label,
            "url": f"{approval_url}/actions/{action_id}/{decision}",
            "method": "POST",
            "headers": {"Authorization": f"Bearer {token}"},
            "clear": True,
        }

    _post_json(cfg, {
        "title": f"Approve? {description}",
        "message": f"{reason}\n\nExpires in {expiry_minutes} min. Action id {action_id}.",
        "tags": ["question"],
        "priority": 4,
        "actions": [button("Approve", "approve"), button("Deny", "deny")],
    })
