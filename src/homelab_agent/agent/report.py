"""The agent's final output: a JSON schema the API enforces, and a Pydantic model we validate with."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..actions.models import ACTION_SCHEMA, parse

Severity = Literal["info", "warning", "critical"]
Confidence = Literal["low", "medium", "high"]
Status = Literal["healthy", "degraded", "incident"]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Severity
    target: str
    issue: str
    evidence: list[str]
    root_cause: str
    proposed_fix: str
    confidence: Confidence


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status
    summary: str
    findings: list[Finding]
    # Raw proposals: each one is validated on its own (actions.models.parse), so a malformed
    # proposal is rejected individually instead of throwing away the whole diagnosis.
    actions: list[dict] = []


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values.__args__)}


REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": _enum(Status),
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": _enum(Severity),
                    "target": {"type": "string", "description": "Guest, storage pool or host/container affected"},
                    "issue": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "root_cause": {"type": "string"},
                    "proposed_fix": {"type": "string"},
                    "confidence": _enum(Confidence),
                },
                "required": list(Finding.model_fields),
                "additionalProperties": False,
            },
        },
        "actions": {"type": "array", "items": ACTION_SCHEMA},
    },
    "required": list(Report.model_fields),
    "additionalProperties": False,
}

SEVERITY_ICON = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
STATUS_ICON = {"healthy": "✅", "degraded": "⚠️", "incident": "🚨"}


def to_markdown(report: Report) -> str:
    lines = [f"{STATUS_ICON[report.status]} **{report.status.upper()}**: {report.summary}"]
    for f in report.findings:
        lines += [
            "",
            f"{SEVERITY_ICON[f.severity]} **{f.target}**: {f.issue}",
            f"- Root cause ({f.confidence} confidence): {f.root_cause}",
            *(f"- Evidence: {e}" for e in f.evidence),
            f"- Fix: {f.proposed_fix}",
        ]
    if report.actions:
        lines += ["", "**Proposed actions:**"]
        for raw in report.actions:
            action, error = parse(raw)
            lines.append(f"- {action.describe()} ({action.reason})" if action else f"- (invalid proposal: {error})")
    return "\n".join(lines)
