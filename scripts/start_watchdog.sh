#!/bin/bash
# Start the watchdog (which starts and supervises the vLLM server) and wait until the server is healthy.
# usage: scripts/start_watchdog.sh model1|vlm ; stop with scripts/stop_watchdog.sh <model>
# model1: the reading model (Gemma, vllm_model1.sh, port 8001); vlm: the vision model (Qwen3-VL, vllm_vlm.sh, 8002).
#
# Start them ONE AFTER ANOTHER (model1 first). Two servers loading at the same moment on a shared card do
# not respect their memory shares: the one that initialises first takes far more than its share.
cd "$(dirname "$0")/.." || exit 1
model="${1:-model1}"
case "$model" in
  model1) port=8001 ;;
  vlm) port=8002 ;;
  *) echo "usage: $0 model1|vlm"; exit 1 ;;
esac
mkdir -p logs
nohup "${PYTHON:-.venv/bin/python}" scripts/watchdog.py "$model" --launch "scripts/vllm_${model}.sh" \
  >> "logs/watchdog_${model}.log" 2>&1 &
echo $! > "logs/watchdog_${model}.pid"
echo "watchdog for ${model} started, pid $(cat "logs/watchdog_${model}.pid"); waiting for the server on port ${port}"
for _ in $(seq 1 240); do
  if [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/health")" = "200" ]; then
    echo "${model} is ready"
    exit 0
  fi
  sleep 5
done
echo "${model} did not become ready in 20 minutes; see logs/vllm_${model}.log and logs/incidents.jsonl"
exit 1
