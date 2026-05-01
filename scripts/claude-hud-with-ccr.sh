#!/bin/bash
# Wraps claude-hud-launcher.sh and appends a ccr usage segment on a new line.
# Logs diagnostics to /tmp/ccr-hud-wrapper.log so we can debug if it breaks.

LOG=/tmp/ccr-hud-wrapper.log
{
  echo "---- $(date +%FT%T) invoked, COLUMNS=$COLUMNS, TERM=$TERM, has-tty=$( [ -t 0 ] && echo yes || echo no )"
} >>"$LOG" 2>&1

INPUT=$(cat)
echo "stdin bytes: ${#INPUT}" >>"$LOG"

printf '%s' "$INPUT" | /home/admin/.claude/plugins/claude-hud-launcher.sh 2>>"$LOG"
HUD_EXIT=$?
echo "HUD exit: $HUD_EXIT" >>"$LOG"

SEG=$(/usr/bin/python3 /home/admin/Escritorio/experimentos/ccrouter/scripts/ccr_segment.py 2>>"$LOG")
echo "segment: $SEG" >>"$LOG"

if [ -n "$SEG" ]; then
  printf '\n%s' "$SEG"
fi
