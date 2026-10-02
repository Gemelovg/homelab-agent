"""Read-only Proxmox VE access. The API token should only have the PVEAuditor role."""

from proxmoxer import ProxmoxAPI

from .config import ProxmoxConfig
from .safety import redact

GB = 1024**3
MB = 1024**2

# Free-text fields where people jot down anything, credentials included. Pattern-based
# redaction can't recognise a bare password in prose, so these are withheld entirely.
FREE_TEXT_KEYS = {"description"}


def sanitize_guest_config(config: dict) -> dict:
    out = {}
    for key, value in config.items():
        if key == "digest":
            continue
        if key in FREE_TEXT_KEYS:
            out[key] = f"[withheld: free-text notes, {len(str(value))} chars]"
        else:
            out[key] = redact(str(value))
    return out


class Proxmox:
    def __init__(self, cfg: ProxmoxConfig):
        self.api = ProxmoxAPI(
            cfg.host,
            user=cfg.user,
            token_name=cfg.token_name,
            token_value=cfg.token_value,
            verify_ssl=cfg.verify_ssl,
        )

    def guests(self) -> list[dict]:
        out = []
        for r in self.api.cluster.resources.get(type="vm"):
            out.append(
                {
                    "vmid": r["vmid"],
                    "name": r.get("name"),
                    "type": r["type"],  # lxc | qemu
                    "node": r["node"],
                    "status": r.get("status"),
                    "cpu_pct": round(r.get("cpu", 0) * 100, 1),
                    "cores": r.get("maxcpu"),
                    "mem_used_mb": round(r.get("mem", 0) / MB),
                    "mem_max_mb": round(r.get("maxmem", 0) / MB),
                    "disk_used_gb": round(r.get("disk", 0) / GB, 1),
                    "disk_max_gb": round(r.get("maxdisk", 0) / GB, 1),
                    "uptime_h": round(r.get("uptime", 0) / 3600, 1),
                }
            )
        return sorted(out, key=lambda g: g["vmid"])

    def guest(self, vmid: int) -> dict:
        match = next((g for g in self.guests() if g["vmid"] == vmid), None)
        if match is None:
            raise ValueError(f"No guest with vmid {vmid}")
        endpoint = self.api.nodes(match["node"])(match["type"])(vmid)
        config = endpoint.config.get()
        return {
            **match,
            "config": sanitize_guest_config(config),
        }

    def storage(self) -> list[dict]:
        out = []
        for node in self.api.nodes.get():
            for s in self.api.nodes(node["node"]).storage.get():
                total = s.get("total") or 0
                used = s.get("used") or 0
                out.append(
                    {
                        "node": node["node"],
                        "storage": s["storage"],
                        "type": s.get("type"),
                        "active": bool(s.get("active")),
                        "used_pct": round(used / total * 100, 1) if total else None,
                        "avail_gb": round((s.get("avail") or 0) / GB, 1),
                        "total_gb": round(total / GB, 1),
                    }
                )
        return out

    def recent_tasks(self, errors_only: bool, limit: int) -> list[dict]:
        tasks = self.api.cluster.tasks.get()
        if errors_only:
            tasks = [t for t in tasks if t.get("status") not in (None, "OK")]
        return [
            {
                "upid": t["upid"],
                "node": t["node"],
                "type": t.get("type"),
                "id": t.get("id"),
                "status": t.get("status", "running"),
                "starttime": t.get("starttime"),
                "endtime": t.get("endtime"),
            }
            for t in tasks[:limit]
        ]

    def task_log(self, node: str, upid: str, limit: int) -> str:
        lines = self.api.nodes(node).tasks(upid).log.get(limit=limit)
        return "\n".join(line.get("t", "") for line in lines)
