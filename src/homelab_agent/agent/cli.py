"""homelab-agent: investigate the homelab from the command line.

    homelab-agent --health-check --notify
    homelab-agent "Why is sonarr not grabbing anything?"
"""

import argparse
import asyncio
import sys
from dataclasses import replace

from ..config import ConfigError, load_config
from . import notify
from .loop import run_agent
from .prompts import HEALTH_CHECK_TASK
from .report import to_markdown


def _print_tool_call(name: str, args: dict) -> None:
    rendered = ", ".join(f"{k}={v!r}" for k, v in args.items())
    print(f"  → {name}({rendered})", file=sys.stderr, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="AI ops agent for a Proxmox/Docker homelab")
    parser.add_argument("task", nargs="?", help="What to investigate, in plain language")
    parser.add_argument("--health-check", action="store_true", help="Run a full health check")
    parser.add_argument("--notify", action="store_true", help="Send the report to ntfy")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--max-steps", type=int)
    args = parser.parse_args()

    if not args.task and not args.health_check:
        parser.error("give a task or use --health-check")
    try:
        cfg = load_config()
    except ConfigError as e:
        sys.exit(f"Config error: {e}")
    if args.notify and not cfg.ntfy:
        sys.exit("--notify needs an ntfy section in config.yaml")

    settings = cfg.agent
    if args.effort:
        settings = replace(settings, effort=args.effort)
    if args.max_steps:
        settings = replace(settings, max_steps=args.max_steps)

    task = args.task or HEALTH_CHECK_TASK
    print(f"Investigating with {settings.model} (effort {settings.effort}, max {settings.max_steps} steps)…", file=sys.stderr)
    run = asyncio.run(run_agent(task, settings, on_tool_call=_print_tool_call))
    path = run.save()

    footer = (
        f"{run.steps} steps · {len(run.tool_calls)} tool calls · "
        f"{run.usage.input + run.usage.cache_read + run.usage.cache_write:,} in / {run.usage.output:,} out tokens · "
        f"${run.usage.cost_usd:.3f} · {run.duration_s}s"
        + (" · step budget hit" if run.hit_step_budget else "")
    )
    print()
    if run.report:
        print(to_markdown(run.report))
    else:
        print(f"Run failed: {run.error}")
    print(f"\n{footer}\nRun saved to {path}", file=sys.stderr)

    if args.notify and run.report:
        notify.send(cfg.ntfy, run.report, footer)
        print("Sent to ntfy.", file=sys.stderr)
    sys.exit(0 if run.report else 1)


if __name__ == "__main__":
    main()
