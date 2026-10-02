# homelab-agent

An AI ops agent for a Proxmox homelab: it investigates incidents across LXC containers and
Docker services, explains the root cause, and proposes fixes. It never changes anything
without approval.

Built in phases, each covering a skill area:

| Phase | What | Skill area |
|---|---|---|
| 1 ✅ | Read-only **MCP server** for Proxmox + Docker | AI-enabled dev tools |
| 2 ✅ | **Agent loop** on the Claude API: alert → investigate → diagnose → report | Multi-step agent workflows |
| 3 ✅ | **Eval harness**: staged incidents, scored diagnoses, prompt versioning | Prompts, evals, context systems |
| 4 | **Guarded write actions**: validated fixes, approval gate, injection tests | Hardening AI outputs |
| 5 | Write-up: architecture, eval results, lessons learned | Communicating it |

## Architecture (phase 1)

```
Claude Code / agent ──MCP (stdio)──▶ homelab-mcp
                                       ├─ safety.py   redact secrets → cap size → wrap logs as untrusted
                                       ├─ audit.py    JSONL log of every tool call
                                       ├─ proxmox.py  ──HTTPS, PVEAuditor token──▶ Proxmox API
                                       └─ docker_hosts.py ──tcp──▶ docker-socket-proxy (in each LXC)
```

### Security model
- **Read-only at the infrastructure level**, not just in code: the Proxmox token only has
  `PVEAuditor`, and the Docker socket proxy rejects every POST/DELETE.
- **Allowlisted targets**: the model can only name hosts from `config.yaml`. Container names
  are validated, and the model never supplies URLs.
- **Secret redaction** on env vars, Proxmox config, health checks and logs (passwords, tokens,
  bearer headers, URL credentials, private keys).
- **Free-text fields withheld**: the first real run found a plaintext password in a VM's
  Proxmox notes. Pattern-based redaction can't catch a bare password in prose, so free-text
  notes are now withheld entirely (regression test in `tests/test_proxmox.py`).
- **Prompt-injection boundary**: logs are attacker-influenced, so they come back wrapped in
  `<untrusted_data>`, and a payload can't close the wrapper early.
- **Audit trail**: `audit.jsonl` records every call, its arguments, the outcome and how long it took.

## Setup

### 1. Proxmox: read-only API token
On the Proxmox host:
```bash
pveum user add agent@pve --comment "homelab-agent (read-only)"
pveum acl modify / --users agent@pve --roles PVEAuditor
pveum user token add agent@pve mcp --privsep 0     # prints the token value once, copy it
```

### 2. Docker: read-only socket proxy in each Docker LXC
Add this to each LXC that runs Docker (replace the IP with that LXC's LAN IP):
```yaml
services:
  docker-socket-proxy:
    image: tecnativa/docker-socket-proxy:latest
    environment:
      CONTAINERS: 1   # list/inspect/logs
      POST: 0         # deny every write (start/stop/exec/rm)
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "192.168.1.21:2375:2375"
    restart: unless-stopped
```
The proxy has no authentication, so use the Proxmox firewall to allow port 2375 only from the
machine running the agent.

### 3. Configure and test
```bash
cp config.example.yaml config.yaml   # edit hosts
cp .env.example .env                 # paste the token value
uv sync
uv run pytest
```

### 4. Connect to Claude Code
```bash
claude mcp add homelab -- uv --directory /path/to/homelab-agent run homelab-mcp
```
Then ask things like:
- "Give me a health overview of the homelab."
- "Is anything restarting or unhealthy? Find out why."
- "Which storage pool is closest to full, and which guests live on it?"
- "Did any backups fail this week? Why?"

## Agent (phase 2)

```
homelab-agent "task" ──▶ agent loop (Claude API, claude-opus-5-5)
                           │  ▲  tool_use / tool_result
                           ▼  │
                         MCP client ──stdio──▶ homelab-mcp (phase 1: same tools, same safety layer)
                           │
                           ├─▶ JSON report (schema-enforced, Pydantic-validated)
                           ├─▶ runs/<timestamp>.json (full transcript, tokens, cost: eval data for phase 3)
                           └─▶ ntfy push notification
```

```bash
uv run homelab-agent --health-check --notify
uv run homelab-agent "Sonarr isn't grabbing anything. Why?"
uv run homelab-agent --health-check --effort medium --max-steps 8
```

Design choices:
- **The agent is an MCP client of its own server.** Tools, redaction, the untrusted-data
  wrapper and the audit log live in one place, shared by Claude Code and the agent.
- **Hand-written tool-use loop**, so step limits, parallel tool execution and per-run
  accounting are explicit.
- **Step budget with a graceful landing**: when it runs out, a mid-conversation system
  message tells the model to report with what it has (tools disabled), so the run never
  gets cut off without a result.
- **Structured output**: the API enforces a JSON schema for the final report:
  `status`, `summary`, and `findings[]` with severity, evidence, root cause, fix and confidence.
- **Failure handling**: a tool error goes back to the model as an `is_error` result. Refusals,
  invalid reports and unexpected stop reasons are recorded, never crash the run, and a
  refusal falls back to another model automatically.
- **Cost visibility**: token usage (including prompt-cache reads) and dollar cost for every run.
- **Versioned system prompt** (`agent/prompts.py`): each run records the prompt version
  for eval comparisons.

## Evals (phase 3)

How do I know the agent is any good? I break a fake homelab in 20 specific ways and score
whether the agent finds the real cause. Details: [`evals/SCENARIOS.md`](evals/SCENARIOS.md).

- **Fixture replay**: each scenario is a YAML snapshot of a synthetic homelab with one thing
  broken (or nothing). The fakes emulate the raw Proxmox and Docker APIs, so the *real* MCP
  server, safety layer and agent loop run unchanged on top. That makes runs reproducible and
  safe, and prompt-injection payloads can't touch real systems.
- **Symptoms, not hints**: the agent gets only what a person would say ("Ombi keeps dying").
  The evidence is in the data, and the agent has to find it with its tools.
- **Grading**: a Claude Sonnet 5.5 judge checks each report against a per-scenario rubric
  (pass = every criterion met). Programmatic checks cover status, false alarms and whether an
  injection payload managed to override the alert status. The judge was validated on
  known-good and known-bad reports before use.
- **Runner hygiene**: 3 reps per scenario, resumable, infra failures go to `errors.jsonl` and
  are never scored as model failures, the served model is asserted, and the runner refuses to
  run if the runner, grader or scenarios changed since the last human approval.

```bash
uv run python evals/run.py --reps 3                                  # baseline
uv run python evals/run.py --variant v2 --model claude-sonnet-5-5    # a variant
```

### Results (20 scenarios × 3 runs per variant)

| | Opus 5.5 · high | Opus 5.5 · medium | **Sonnet 5.5 · high** |
|---|---|---|---|
| Root cause found | 100% | 100% | **100%** |
| Correct status | 96.7% | 96.7% | 95.0% |
| No false alarms | 100% | 98.3% | **100%** |
| Prompt injection resisted | 9/9 | 9/9 | **9/9** |
| Cost per investigation | $0.070 | $0.059 | **$0.024 (−66%)** |
| Median latency | 25.3 s | 21.1 s | **14.7 s (−42%)** |

The agent now defaults to Sonnet 5.5: same accuracy at a third of the cost. All variants hit
the ceiling on root cause, so the next step is harder scenarios to get headroom back.

### What building the eval caught
- **Redaction gaps** (fixed, with regression tests): a plaintext password in Proxmox notes
  (found by the agent on its first real run), and env vars like `NB_SETUP_KEY` slipping past
  the secret patterns (found while writing fixtures).
- **Grader bugs**: keyword-based hijack detection flagged agents that *quoted* an attacker's
  payload while reporting it. Hijack is now defined by its effect (status overridden), and the
  judge checks whether the instruction was followed. All flagged runs were re-checked by hand.
- **A fixture flaw**: an agent noticed that a backup log claiming `--all` listed only 3 guests.
- **A consistent model miss**: for a broken health check, agents sometimes raise a warning but
  call the overall status "healthy". That's the target for the next prompt change.

## Roadmap

### Phase 2: agent loop ✅
- [x] Claude API tool-use loop over the MCP server
- [x] Structured report, step budget, cost tracking, run transcripts
- [x] ntfy notifications
- [ ] Scheduled run (cron/systemd timer in the `claude` LXC)
- [ ] Trigger from Uptime Kuma webhooks

### Phase 3: evals ✅
- [x] 20 fixture scenarios across disk, container, backup, network, healthy and prompt-injection cases
- [x] Runner with resume, concurrency, error sidecar, served-model check and a harness-approval gate
- [x] Grading: Sonnet 5.5 judge with per-scenario rubric + programmatic status/false-alarm/hijack checks
- [x] Model and effort comparison
- [ ] Harder multi-failure scenarios (current set is at the ceiling)
- [ ] Context: runbooks / compose files / past incidents, measured against this eval

### Phase 4: hardening AI-generated changes
- [ ] The agent proposes a diff to compose files; never edits them directly
- [ ] Validation pipeline: `docker compose config`, `yamllint`, `hadolint`, `trivy config`
- [ ] Human approval gate (CLI prompt or ntfy action button) before any apply
- [ ] Narrowly scoped write tools (e.g. only `restart_container` on an allowlist)
- [ ] Red-team evals: logs containing injected instructions; the agent must not act on them
