#!/bin/bash
# Candidate model 3 (comparison only): IBM Granite 4.1 30B, ready-made FP8 build, on 127.0.0.1:8003.
# The card cannot hold three models: stop model 2 first (scripts/stop_watchdog.sh model2).
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES="${GPU3:-0}"
exec "${VLLM_BIN:-/workspace/venvs/vllm/bin/vllm}" serve ibm-granite/granite-4.1-30b-fp8 \
  --served-model-name granite-4.1-30b \
  --host 127.0.0.1 --port 8003 \
  --max-model-len 32768 --max-num-seqs 8 \
  --gpu-memory-utilization "${GPU_FRAC3:-0.55}"
