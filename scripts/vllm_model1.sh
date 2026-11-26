#!/bin/bash
# The reading model: Gemma-4-31B-it, ready-made FP8 build, on 127.0.0.1:8001.
# It is the only model on the single 96 GB card (the second reader, Qwen3.8-27B, was removed on
# 2026-11-26), so it takes 0.90 of the memory: 33 GB of weights, the rest is KV cache, which lets far
# more requests run at once than the former 0.55 share did.
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES="${GPU1:-0}"
exec "${VLLM_BIN:-/workspace/venvs/vllm/bin/vllm}" serve RedHatAI/gemma-4-31B-it-FP8-block \
  --served-model-name gemma-4-31b-it \
  --host 127.0.0.1 --port 8001 \
  --reasoning-parser gemma4 \
  --max-model-len "${MAX_LEN1:-32768}" --max-num-seqs "${MAX_SEQS:-32}" \
  --gpu-memory-utilization "${GPU_FRAC1:-0.90}"
