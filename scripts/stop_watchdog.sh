#!/bin/bash
# Stop the watchdog and the vLLM server it supervises.
cd "$(dirname "$0")/.." || exit 1
model="$1"
pidfile="logs/watchdog_${model}.pid"
[ -f "$pidfile" ] || { echo "no pid file for ${model}"; exit 1; }
wd=$(cat "$pidfile")
server=$(pgrep -P "$wd" | head -1)
kill "$wd" 2>/dev/null
[ -n "$server" ] && kill -- "-$server" 2>/dev/null
rm -f "$pidfile"
echo "stopped watchdog ${wd} and server group ${server:-none}"
