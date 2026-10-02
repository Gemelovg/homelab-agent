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
