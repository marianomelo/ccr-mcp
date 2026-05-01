# ccr-mcp

MCP server that lets Claude Code (Anthropic CLI) delegate work to **subagents
routed through [claude-code-router](https://github.com/musistudio/claude-code-router)
(ccr)** — typically backed by DeepSeek or another non-Anthropic model.

The point: keep the orchestration in your paid Claude Code (Opus) session, but
push the heavy I/O (file reads, code generation, tool loops) into a child
Claude Code that talks to a cheaper model. The child's tool I/O **never enters
your parent context** — only a short summary returns. That's what makes the
routing economically meaningful.

## Architecture

```
your Claude Code (Opus, paid sub)
        │
        │  tool: spawn_agent / chat
        ▼
ccr-mcp (this repo, Python stdio)
        │
        │  spawn:  ccr code -p "<task>" --output-format json --dangerously-skip-permissions
        │  http:   POST 127.0.0.1:3456/v1/messages
        ▼
ccr router (musistudio/claude-code-router)
        │
        ▼
DeepSeek V4 / Gemini / Ollama / etc.
```

## Tools exposed to Claude Code

| Tool | Purpose |
|---|---|
| `spawn_agent(task, cwd?, timeout?)` | Launch a Claude Code subagent in the background. Returns `agent_id`. |
| `agent_status(agent_id)` | Non-blocking status check. |
| `agent_result(agent_id, wait=True, max_wait?)` | Block until done (or up to `max_wait`), return final summary + token/cost meta. |
| `agent_logs(agent_id, tail=60)` | Last N stdout/stderr lines for debugging. |
| `agent_kill(agent_id)` | SIGTERM the subagent. |
| `list_agents()` | All known agents with status + elapsed time. |
| `quick_chat(prompt, model?, system?, max_tokens?)` | Single short HTTP call (no subagent, no tools, no memory). For quick factual queries. |
| `list_providers()` | Show providers, models, and routing from ccr config. |
| `health()` | Check ccr router reachability. |

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (or pip)
- Node 18+ and `npm install -g @musistudio/claude-code-router`
- Claude Code CLI (`@anthropic-ai/claude-code`)
- A DeepSeek API key (or any provider supported by ccr)

## Install

```bash
git clone https://github.com/marianomelo/ccr-mcp.git
cd ccr-mcp
uv sync
```

Register with Claude Code (user scope, available in any project):

```bash
claude mcp add ccr -s user -- uv --directory "$PWD" run ccr-mcp
```

Verify:

```bash
claude mcp list   # should show: ccr ... ✓ Connected
```

## Configure ccr (one-time)

Create `~/.claude-code-router/config.json`. A working starter for DeepSeek V4 is in
[`scripts/config.example.json`](scripts/config.example.json) — drop in your API key.

> **Note**: DeepSeek V4 thinking-mode breaks in multi-turn agentic loops with the
> stock ccr setup. Use the companion fix:
> [`marianomelo/ccr-deepseek-thinking-fix`](https://github.com/marianomelo/ccr-deepseek-thinking-fix).
> The example config already references it.

Run ccr as a systemd user service so it survives terminal close:

```bash
cp scripts/ccr.service ~/.config/systemd/user/ccr.service
# edit Environment=PATH and ExecStart paths to match your `which ccr` and `which node`
systemctl --user daemon-reload
systemctl --user enable --now ccr.service
loginctl enable-linger "$USER"   # so the service runs without a login session
```

## Optional: usage segment in Claude HUD

If you use [Claude HUD](https://github.com/jarrodwatts/claude-hud), the
[`scripts/claude-hud-with-ccr.sh`](scripts/claude-hud-with-ccr.sh) wrapper
appends a one-line ccr usage segment under HUD's statusline:

```
[Opus 4.7] admin · Context ░░░░░░ 0%
ccr 12.4K↑ 3.1K↓ · 7 req · ds-v4-pro
```

Setup:

```bash
cp scripts/claude-hud-with-ccr.sh ~/.claude/plugins/claude-hud-with-ccr.sh
chmod +x ~/.claude/plugins/claude-hud-with-ccr.sh
# edit ~/.claude/settings.json statusLine.command to point at the wrapper
```

The segment is produced by [`scripts/ccr_segment.py`](scripts/ccr_segment.py),
which parses ccr's JSON-Lines log and sums today's tokens.

## Token economics — read this

`spawn_agent` is what gives you actual savings: tool I/O lives in the subagent.
`quick_chat` returns the full text into your parent context, so it only saves
on tasks where DeepSeek's response is short.

Rule of thumb:
- **Big input → small output** (read 10 files, summarize) → use a subagent.
- **Big input → big output** (generate a long file) → use a subagent that
  writes the file directly. Do not Read it back into Opus unless you need to.
- **Short factual question** → `quick_chat` is fine.

## Caveats

- `--dangerously-skip-permissions` is passed to the subagent (it's headless,
  no human to approve tool prompts). Only spawn it in directories you control.
- Sessions live only in MCP process memory; they don't survive `claude mcp` restarts.
- `spawn_agent` startup is ~3-5s (full Claude Code bootstrap inside the subprocess).
- DeepSeek V4 thinking-mode works only with the companion transformer fix.
- Tool-use reliability with non-Anthropic models is lower than with Claude.
  Some agent tasks will fail and need retry.

## Cost reference (observed in stress tests)

| Task | Model | Time | Cost |
|---|---|---|---|
| Read CSV + stats + write report | deepseek-v4-pro | 31s | $0.12 |
| Debug Python bug + fix + verify | deepseek-v4-pro | 36s | $0.20 |
| Generate module + tests + run + verify | deepseek-v4-pro | 33s | $0.06 |

## Related

- [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) — the router
- [marianomelo/ccr-deepseek-thinking-fix](https://github.com/marianomelo/ccr-deepseek-thinking-fix) — fix for DeepSeek V4 thinking-mode multi-turn bug
- [jarrodwatts/claude-hud](https://github.com/jarrodwatts/claude-hud) — statusline plugin

## License

MIT
