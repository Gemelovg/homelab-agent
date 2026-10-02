# homelab-agent

An AI ops agent for a Proxmox homelab: it investigates incidents across LXC containers and
Docker services, explains the root cause, and proposes fixes. It never changes anything
without approval.

Built in phases, each covering a skill area:

| Phase | What | Skill area |
|---|---|---|
| 1 ✅ | Read-only **MCP server** for Proxmox + Docker | AI-enabled dev tools |
| 2 | **Agent loop** on the Claude API: alert → investigate → diagnose → report | Multi-step agent workflows |
| 3 | **Eval harness**: staged incidents, scored diagnoses, prompt versioning | Prompts, evals, context systems |
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

## Roadmap

### Phase 2: agent loop
- [ ] `agent/` package: Claude API tool-use loop that reuses the same tool functions
- [ ] Structured output: `{summary, root_cause, evidence[], confidence, proposed_fix}`
- [ ] Limits: max steps, max tokens, and a cost report per run
- [ ] Trigger it from Uptime Kuma / Alertmanager webhooks; send the report to ntfy/Discord

### Phase 3: evals and context
- [ ] `evals/scenarios/`: break things on purpose in a test LXC (crash-loop, OOM, full disk,
      bad compose env var, expired cert, DNS failure) and record the expected root cause
- [ ] Scorer: correct root cause (LLM-as-judge + keyword checks), steps used, tokens, cost
- [ ] Version system prompts, track scores per version and model in a results table
- [ ] Context: give the agent your compose files, runbooks and past incident reports, and
      measure whether accuracy actually improves

### Phase 4: hardening AI-generated changes
- [ ] The agent proposes a diff to compose files; never edits them directly
- [ ] Validation pipeline: `docker compose config`, `yamllint`, `hadolint`, `trivy config`
- [ ] Human approval gate (CLI prompt or ntfy action button) before any apply
- [ ] Narrowly scoped write tools (e.g. only `restart_container` on an allowlist)
- [ ] Red-team evals: logs containing injected instructions; the agent must not act on them
