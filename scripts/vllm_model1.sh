#!/bin/bash
# The reading model: Gemma-4-31B-it, ready-made FP8 build, on 127.0.0.1:8001.
# Shares the single 96 GB card with the vision model (vllm_vlm.sh, 0.45): 33 GB of weights plus ~12 GB of
# KV cache at 0.47. (The second reader, Qwen3.8-27B, was removed on 2026-11-26.)
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES="${GPU1:-0}"
exec "${VLLM_BIN:-/workspace/venvs/vllm/bin/vllm}" serve RedHatAI/gemma-4-31B-it-FP8-block \
  --served-model-name gemma-4-31b-it \
  --host 127.0.0.1 --port 8001 \
  --reasoning-parser gemma4 \
  --max-model-len "${MAX_LEN1:-32768}" --max-num-seqs "${MAX_SEQS:-16}" \
  --gpu-memory-utilization "${GPU_FRAC1:-0.47}"
