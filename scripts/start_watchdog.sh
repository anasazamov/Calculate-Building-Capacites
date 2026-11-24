#!/bin/bash
# Start the watchdog (which starts and supervises the vLLM server) in the background.
# usage: scripts/start_watchdog.sh model1|model2 ; stop with scripts/stop_watchdog.sh model1|model2
cd "$(dirname "$0")/.." || exit 1
model="$1"
mkdir -p logs
nohup "${PYTHON:-.venv/bin/python}" scripts/watchdog.py "$model" --launch "scripts/vllm_${model}.sh" \
  >> "logs/watchdog_${model}.log" 2>&1 &
echo $! > "logs/watchdog_${model}.pid"
echo "watchdog for ${model} started, pid $(cat "logs/watchdog_${model}.pid")"
