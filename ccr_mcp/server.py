"""MCP server: spawn Claude Code subagents routed through ccr.

Design:
- The expensive content (large file reads, generated code, tool I/O) lives in
  the subagent's process. Only a compact summary returns to the parent's
  context — that's what makes the routing economically meaningful.
- Subagents are launched as background processes with their own stdout/stderr
  captured to disk, so we can poll status without blocking and inspect logs
  on demand.
- A small `quick_chat` tool stays for cheap one-shot factual queries where you
  do want the answer in context.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

CONFIG_PATH = Path(
    os.environ.get("CCR_CONFIG", Path.home() / ".claude-code-router" / "config.json")
)
ENDPOINT = os.environ.get("CCR_ENDPOINT", "http://127.0.0.1:3456/v1/messages")
HTTP_TIMEOUT = float(os.environ.get("CCR_HTTP_TIMEOUT", "180"))

CCR_BIN = os.environ.get("CCR_BIN") or shutil.which("ccr") or "ccr"
AGENT_BASE = Path(os.environ.get("CCR_MCP_AGENTS_DIR", "/tmp/ccr-mcp-agents"))
AGENT_BASE.mkdir(parents=True, exist_ok=True)
DEFAULT_AGENT_TIMEOUT = int(os.environ.get("CCR_AGENT_TIMEOUT", "600"))

mcp = FastMCP("ccr")


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"ccr config not found at {CONFIG_PATH}. "
            "Run `ccr ui` or create it manually."
        )
    return json.loads(CONFIG_PATH.read_text())


def _headers() -> dict[str, str]:
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    try:
        api_key = _load_config().get("APIKEY")
    except FileNotFoundError:
        api_key = None
    if api_key:
        headers["x-api-key"] = api_key
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def _default_model() -> str:
    router = _load_config().get("Router", {})
    model = router.get("default")
    if not model:
        raise ValueError("No model in args and no Router.default in ccr config.")
    return model


# --------------------------------------------------------------------------
# Subagent runtime
# --------------------------------------------------------------------------

@dataclass
class Agent:
    id: str
    task: str
    cwd: str
    proc: subprocess.Popen
    started_at: float
    timeout: int
    log_dir: Path

    @property
    def stdout_path(self) -> Path:
        return self.log_dir / "stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self.log_dir / "stderr.log"

    @property
    def is_running(self) -> bool:
        return self.proc.poll() is None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    @property
    def status(self) -> str:
        if self.is_running:
            if self.elapsed > self.timeout:
                return "timeout"
            return "running"
        return "completed" if self.proc.returncode == 0 else "failed"


AGENTS: dict[str, Agent] = {}


def _read_tail(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text(errors="ignore").splitlines()[-lines:])
    except FileNotFoundError:
        return ""


def _parse_final_result(stdout_text: str) -> dict[str, Any] | None:
    """Find the final result JSON object emitted by `claude -p --output-format json`."""
    if not stdout_text.strip():
        return None
    for line in reversed(stdout_text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") in {"result", "message"} or "result" in obj:
            return obj
    try:
        return json.loads(stdout_text.strip())
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# Tools — Subagent
# --------------------------------------------------------------------------

@mcp.tool()
def spawn_agent(task: str, cwd: str = "", timeout: int = 0) -> str:
    """Spawn a Claude Code subagent (routed through ccr) to handle a task.

    The subagent has its own Read/Edit/Bash/etc tools and its own context
    window. Use this for multi-step coding work — the heavy I/O stays in
    the subagent and never enters this conversation.

    IMPORTANT — non-blocking usage pattern:
        spawn_agent returns immediately with an agent_id. The subagent
        keeps running in the background. DO NOT then immediately call
        agent_result with wait=True — that defeats the purpose and blocks
        the conversation. Instead:

            1. spawn_agent(task)           -> returns agent_id instantly
            2. (do other work or respond to the user)
            3. agent_status(agent_id)      -> non-blocking, see if done
            4. agent_logs(agent_id)        -> peek at progress if needed
            5. agent_result(agent_id)      -> only when status says completed
                                              (default wait=False, returns
                                               "still running" if not done)

        Use wait=True on agent_result ONLY if you have explicit user
        instruction to block until completion.

    Args:
        task: Instruction for the subagent.
        cwd: Working directory. Empty = current dir of this MCP process.
        timeout: Seconds before the subagent is killed. 0 = default (600).

    Returns:
        agent_id — pass to agent_status, agent_result, agent_logs.
    """
    aid = uuid.uuid4().hex[:8]
    log_dir = AGENT_BASE / aid
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "task.txt").write_text(task)

    workdir = cwd or os.getcwd()
    cmd = [
        CCR_BIN,
        "code",
        "-p",
        task,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
    ]

    stdout_f = (log_dir / "stdout.log").open("wb")
    stderr_f = (log_dir / "stderr.log").open("wb")

    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        stdout=stdout_f,
        stderr=stderr_f,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    AGENTS[aid] = Agent(
        id=aid,
        task=task,
        cwd=workdir,
        proc=proc,
        started_at=time.time(),
        timeout=timeout or DEFAULT_AGENT_TIMEOUT,
        log_dir=log_dir,
    )
    return aid


@mcp.tool()
def agent_status(agent_id: str) -> str:
    """Non-blocking status check for a subagent."""
    a = AGENTS.get(agent_id)
    if a is None:
        return f"Unknown agent: {agent_id}"
    return f"{a.id}: {a.status} ({a.elapsed:.1f}s) — {a.task[:80]}"


@mcp.tool()
def agent_result(agent_id: str, wait: bool = False, max_wait: int = 0) -> str:
    """Get the subagent's final result. NON-BLOCKING by default.

    Default behavior (wait=False): returns immediately. If the subagent is
    still running, returns "still running after Ns" — call again later, or
    poll agent_status first. This is the recommended pattern: do not block
    the conversation waiting for a subagent.

    Set wait=True ONLY when you have explicit instruction to block until
    completion (e.g., the user asked you to "wait for it"). With wait=True,
    blocks up to max_wait seconds (or the subagent's timeout) until done.

    On completion, returns the final assistant text plus a one-line
    metadata footer (status, elapsed, tokens, cost). The full transcript
    is NOT returned — call agent_logs for that.
    """
    a = AGENTS.get(agent_id)
    if a is None:
        return f"Unknown agent: {agent_id}"

    if wait and a.is_running:
        deadline = time.time() + (max_wait or a.timeout)
        while a.is_running and time.time() < deadline:
            time.sleep(0.5)
        if a.is_running and a.elapsed > a.timeout:
            try:
                os.killpg(os.getpgid(a.proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    if a.is_running:
        return f"{a.id}: still running after {a.elapsed:.1f}s"

    stdout_text = a.stdout_path.read_text(errors="ignore")
    final = _parse_final_result(stdout_text)

    if final is None:
        tail = _read_tail(a.stdout_path, 60)
        return f"[no parseable result]\n{tail}\n\n[{a.status} in {a.elapsed:.1f}s]"

    text = (
        final.get("result")
        or final.get("text")
        or (final.get("message", {}) or {}).get("content")
        or json.dumps(final, ensure_ascii=False)[:1000]
    )
    if isinstance(text, list):
        text = "\n".join(
            b.get("text", "") for b in text if isinstance(b, dict) and b.get("type") == "text"
        ) or json.dumps(text, ensure_ascii=False)[:1000]

    usage = final.get("usage") or {}
    cost = final.get("total_cost_usd")
    meta = [f"{a.status} in {a.elapsed:.1f}s"]
    if usage:
        meta.append(
            f"in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}"
        )
    if cost is not None:
        meta.append(f"${cost:.4f}")

    return f"{text}\n\n[{' · '.join(meta)}]"


@mcp.tool()
def agent_logs(agent_id: str, tail: int = 60) -> str:
    """Tail of stdout and stderr for a subagent. For debugging only."""
    a = AGENTS.get(agent_id)
    if a is None:
        return f"Unknown agent: {agent_id}"
    out = _read_tail(a.stdout_path, tail)
    err = _read_tail(a.stderr_path, tail)
    return f"## stdout (last {tail})\n{out}\n\n## stderr (last {tail})\n{err}"


@mcp.tool()
def agent_kill(agent_id: str) -> str:
    """Terminate a running subagent (SIGTERM)."""
    a = AGENTS.get(agent_id)
    if a is None:
        return f"Unknown agent: {agent_id}"
    if not a.is_running:
        return f"{a.id} not running ({a.status})"
    try:
        os.killpg(os.getpgid(a.proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    return f"{a.id} terminated."


@mcp.tool()
def list_agents() -> str:
    """All known subagents with status and elapsed time."""
    if not AGENTS:
        return "No agents."
    lines = ["# Agents"]
    for a in AGENTS.values():
        lines.append(f"- {a.id}: {a.status} ({a.elapsed:.1f}s) — {a.task[:60]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Tools — Quick HTTP call (for short factual queries)
# --------------------------------------------------------------------------

@mcp.tool()
def quick_chat(
    prompt: str,
    model: str = "",
    system: str = "",
    max_tokens: int = 1024,
) -> str:
    """Single short HTTP call to the ccr router. No subagent, no tools, no memory.

    Use only for short factual questions where you want the answer in your
    context. For anything that involves reading files or producing code,
    use spawn_agent instead.
    """
    target_model = model or _default_model()
    payload: dict[str, Any] = {
        "model": target_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        payload["system"] = system

    try:
        resp = httpx.post(
            ENDPOINT, headers=_headers(), json=payload, timeout=HTTP_TIMEOUT
        )
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"Cannot reach ccr at {ENDPOINT}. Is the service running?"
        ) from e

    if resp.status_code != 200:
        raise RuntimeError(f"ccr HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    parts = [
        b.get("text", "")
        for b in data.get("content", [])
        if b.get("type") == "text"
    ]
    return "\n".join(parts) if parts else json.dumps(data, ensure_ascii=False)


# --------------------------------------------------------------------------
# Tools — Inspection
# --------------------------------------------------------------------------

@mcp.tool()
def list_providers() -> str:
    """Show providers, models, and scenario routing from the ccr config."""
    config = _load_config()
    providers = config.get("Providers") or config.get("providers") or []
    router = config.get("Router", {})

    lines = ["# Providers"]
    for p in providers:
        lines.append(f"\n## {p.get('name', '?')}")
        lines.append(f"- endpoint: {p.get('api_base_url', '?')}")
        models = p.get("models", [])
        lines.append(f"- models: {', '.join(models) if models else '(none)'}")

    if router:
        lines.append("\n# Routing")
        for k, v in router.items():
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)


@mcp.tool()
def health() -> str:
    """Check that ccr router is reachable."""
    base = ENDPOINT.rsplit("/v1/", 1)[0]
    try:
        resp = httpx.get(base, timeout=5.0)
        return f"ccr reachable at {base} (HTTP {resp.status_code})"
    except httpx.ConnectError:
        return f"ccr NOT reachable at {base}. Check `systemctl --user status ccr`."


# --------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
