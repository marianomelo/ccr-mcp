#!/usr/bin/env python3
"""Print a one-line statusline segment summarizing ccr usage today.

Reads the most recent ccr log file under ~/.claude-code-router/logs/,
sums tokens from response entries since the start of today (local time),
and prints something like:

    ccr 12.4K↑ 3.1K↓ · 7 req · ds-flash

Designed to be fast (stdlib only, no deps) and to fail silently — printing
nothing on any error so it never breaks the statusline.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(
    os.environ.get("CCR_LOG_DIR", Path.home() / ".claude-code-router" / "logs")
)


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _short_model(name: str) -> str:
    n = name.lower().replace("deepseek-", "ds-").replace("deepseek,", "")
    return n


def main() -> int:
    if not LOG_DIR.exists():
        return 0

    logs = sorted(LOG_DIR.glob("ccr-*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return 0
    latest = logs[-1]

    today_start_ms = int(
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        * 1000
    )

    in_tokens = 0
    out_tokens = 0
    cached = 0
    requests = 0
    last_model = ""

    try:
        with latest.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line or '"input_tokens"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("time", 0) < today_start_ms:
                    continue
                result = entry.get("result")
                if not isinstance(result, dict):
                    continue
                usage = result.get("usage") or {}
                in_tokens += int(usage.get("input_tokens", 0))
                out_tokens += int(usage.get("output_tokens", 0))
                cached += int(usage.get("cache_read_input_tokens", 0))
                requests += 1
                last_model = result.get("model", last_model)
    except OSError:
        return 0

    if requests == 0:
        return 0

    parts = [
        f"ccr {_fmt(in_tokens)}↑ {_fmt(out_tokens)}↓",
        f"{requests} req",
    ]
    if cached:
        parts.append(f"{_fmt(cached)} cached")
    if last_model:
        parts.append(_short_model(last_model))

    print(" · ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
