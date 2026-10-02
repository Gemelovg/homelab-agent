"""Agent loop tests with a scripted fake Claude and a fake MCP session: no API calls, no homelab."""

import asyncio
import json
from types import SimpleNamespace as NS

from homelab_agent.agent.loop import run_loop
from homelab_agent.agent.prompts import BUDGET_EXHAUSTED
from homelab_agent.agent.report import REPORT_SCHEMA, Report
from homelab_agent.config import AgentConfig

REPORT = {
    "status": "degraded",
    "summary": "linkwarden disk is filling up.",
    "findings": [
        {
            "severity": "warning",
            "target": "lxc 113 linkwarden",
            "issue": "Root disk at 72%",
            "evidence": ["disk 5.8/8.0 GB"],
            "root_cause": "Archived pages accumulate",
            "proposed_fix": "pct resize 113 rootfs +4G",
            "confidence": "medium",
        }
    ],
}


def usage(i=100, o=20):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def tool_use(id, name, input=None):
    return NS(type="tool_use", id=id, name=name, input=input or {})


def tool_turn(*blocks):
    return NS(content=list(blocks), stop_reason="tool_use", usage=usage(), model="claude-opus-5-5")


def final_turn(report=REPORT):
    return NS(content=[NS(type="text", text=json.dumps(report))], stop_reason="end_turn", usage=usage(), model="claude-opus-5-5")


class FakeClient:
    """Plays back scripted responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        # Snapshot messages: the loop keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


class FakeSession:
    def __init__(self, fail_tools=()):
        self.calls = []
        self.fail_tools = fail_tools

    async def list_tools(self):
        return NS(tools=[NS(name="list_guests", description="d", input_schema={"type": "object", "properties": {}})])

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name in self.fail_tools:
            raise ConnectionError("proxy unreachable")
        return NS(content=[NS(type="text", text=f"result of {name}")], is_error=False)


def run(responses, session=None, **settings):
    client = FakeClient(responses)
    session = session or FakeSession()
    result = asyncio.run(run_loop(client, session, "check things", AgentConfig(**settings)))
    return result, client, session


def test_tool_calls_then_report():
    result, client, session = run(
        [tool_turn(tool_use("a", "list_guests"), tool_use("b", "storage_usage")), final_turn()]
    )
    assert result.report == Report.model_validate(REPORT)
    assert result.steps == 2
    assert [c[0] for c in session.calls] == ["list_guests", "storage_usage"]
    # both results go back in ONE user message, matched by tool_use_id
    results_msg = client.requests[1]["messages"][-1]
    assert results_msg["role"] == "user"
    assert [r["tool_use_id"] for r in results_msg["content"]] == ["a", "b"]
    assert result.usage.input == 200 and result.usage.output == 40


def test_step_budget_forces_a_report():
    result, client, _ = run(
        [tool_turn(tool_use("a", "list_guests")), tool_turn(tool_use("b", "list_guests")), final_turn()],
        max_steps=2,
    )
    assert result.report is not None
    assert result.hit_step_budget
    last = client.requests[-1]
    assert last["tool_choice"] == {"type": "none"}
    assert last["messages"][-1] == {"role": "system", "content": BUDGET_EXHAUSTED}
    # earlier requests could still use tools
    assert client.requests[0]["tool_choice"] == {"type": "auto"}


def test_tool_failure_is_reported_to_model_not_raised():
    result, client, _ = run(
        [tool_turn(tool_use("a", "list_guests")), final_turn()], session=FakeSession(fail_tools={"list_guests"})
    )
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "proxy unreachable" in tool_result["content"]
    assert result.tool_calls[0].is_error
    assert result.report is not None


def test_invalid_report_is_an_error_not_a_crash():
    result, _, _ = run([final_turn({"status": "fine"})])
    assert result.report is None
    assert "validation" in result.error


def test_refusal_is_surfaced():
    refusal = NS(content=[], stop_reason="refusal", usage=usage(), model="claude-opus-5-5", stop_details=NS(category="cyber", explanation="x"))
    result, _, _ = run([refusal])
    assert result.report is None
    assert "cyber" in result.error


def test_history_is_append_only():
    """Each request's messages must start with the previous request's messages, unchanged."""
    _, client, _ = run([tool_turn(tool_use("a", "list_guests")), tool_turn(tool_use("b", "list_guests")), final_turn()])
    for prev, nxt in zip(client.requests, client.requests[1:]):
        assert nxt["messages"][: len(prev["messages"])] == prev["messages"]


def test_schema_matches_pydantic_model():
    assert set(REPORT_SCHEMA["required"]) == set(Report.model_fields)
    Report.model_validate(REPORT)
