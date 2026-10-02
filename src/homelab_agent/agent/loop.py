"""The agent loop: Claude decides which MCP tools to call, we execute them, repeat until it reports.

The agent is a real MCP client of our own server (homelab_agent.server over stdio), so tool
schemas, redaction, untrusted-data wrapping and the audit log all live in one place.
"""

import asyncio
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import AgentConfig
from .prompts import BUDGET_EXHAUSTED, PROMPT_VERSION, SYSTEM_PROMPT
from .report import REPORT_SCHEMA, Report

# $ per million tokens for claude-opus-5-5 (5-minute cache writes cost 1.25x input).
PRICES = {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20}
MAX_TOKENS = 16000


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0

    def add(self, u) -> None:
        self.input += u.input_tokens or 0
        self.output += u.output_tokens or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0

    @property
    def cost_usd(self) -> float:
        return sum(getattr(self, k) * price for k, price in PRICES.items()) / 1_000_000


@dataclass
class ToolCall:
    step: int
    name: str
    input: dict
    is_error: bool
    result_chars: int
    duration_ms: int


@dataclass
class RunResult:
    task: str
    model: str
    effort: str
    prompt_version: str
    report: Report | None = None
    error: str | None = None
    steps: int = 0
    hit_step_budget: bool = False
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    duration_s: float = 0.0
    transcript: list[dict] = field(default_factory=list)

    def save(self, runs_dir: Path = Path("runs")) -> Path:
        runs_dir.mkdir(exist_ok=True)
        path = runs_dir / f"{datetime.now():%Y%m%d-%H%M%S}.json"
        data = asdict(self)
        data["report"] = self.report.model_dump() if self.report else None
        data["cost_usd"] = round(self.usage.cost_usd, 4)
        path.write_text(json.dumps(data, indent=2, default=str))
        return path


def _block_to_dict(block) -> dict:
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return block if isinstance(block, dict) else dict(vars(block))


async def _call_tool(session: ClientSession, block, step: int, run: RunResult, on_tool_call) -> dict:
    if on_tool_call:
        on_tool_call(block.name, block.input)
    start = time.monotonic()
    try:
        result = await session.call_tool(block.name, block.input)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        is_error = bool(getattr(result, "is_error", False))
    except Exception as e:  # transport-level failure: tell the model instead of crashing the run
        text, is_error = f"Tool call failed: {type(e).__name__}: {e}", True
    run.tool_calls.append(
        ToolCall(step, block.name, block.input, is_error, len(text), round((time.monotonic() - start) * 1000))
    )
    return {"type": "tool_result", "tool_use_id": block.id, "content": text or "(empty)", "is_error": is_error}


async def run_loop(
    client: anthropic.AsyncAnthropic,
    session: ClientSession,
    task: str,
    settings: AgentConfig,
    on_tool_call: Callable[[str, dict], None] | None = None,
) -> RunResult:
    run = RunResult(task, settings.model, settings.effort, PROMPT_VERSION)
    start = time.monotonic()
    listed = await session.list_tools()
    tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listed.tools]
    messages: list[dict] = [{"role": "user", "content": task}]

    # One extra request past the budget, so the agent always gets to write a report.
    for step in range(1, settings.max_steps + 2):
        final_step = step > settings.max_steps
        if final_step:
            run.hit_step_budget = True
            # Mid-conversation system message: operator instruction without touching the cached prefix.
            messages.append({"role": "system", "content": BUDGET_EXHAUSTED})

        response = await client.beta.messages.create(
            model=settings.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tools,
            tool_choice={"type": "none"} if final_step else {"type": "auto"},
            messages=messages,
            output_config={"effort": settings.effort, "format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines (e.g. security-flavoured log content), retry on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        run.steps = step
        run.usage.add(response.usage)
        # Append the full content unchanged (thinking blocks included): the history must stay append-only.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use":
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            results = await asyncio.gather(*(_call_tool(session, b, step, run, on_tool_call) for b in tool_uses))
            messages.append({"role": "user", "content": list(results)})
            continue

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                run.report = Report.model_validate_json(text)
            except ValueError as e:
                run.error = f"Report failed validation: {e}"
        elif response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            run.error = f"Refused ({getattr(details, 'category', None)}): {getattr(details, 'explanation', '')}"
        else:
            run.error = f"Unexpected stop_reason: {response.stop_reason}"
        break

    run.duration_s = round(time.monotonic() - start, 1)
    run.transcript = [
        {"role": m["role"], "content": m["content"] if isinstance(m["content"], str) else [_block_to_dict(b) for b in m["content"]]}
        for m in messages
    ]
    return run


async def run_agent(task: str, settings: AgentConfig, on_tool_call=None, project_dir: Path | None = None) -> RunResult:
    project_dir = project_dir or Path.cwd()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "homelab_agent.server"],
        cwd=str(project_dir),  # server reads config.yaml and .env from here
    )
    with (project_dir / "mcp-server.log").open("a") as errlog:
        async with stdio_client(server, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await run_loop(anthropic.AsyncAnthropic(), session, task, settings, on_tool_call)
