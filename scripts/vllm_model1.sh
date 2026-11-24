#!/bin/bash
# Model 1: Qwen3.8-27B-FP8 on 127.0.0.1:8001. Speculative decoding (MTP) stays off.
# The server has a single 96 GB card (RTX PRO 6000 Blackwell), so both models share it:
# each gets a fraction of memory (0.45 x 96 GB ~ 43 GB, above the required 38 GB).
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES="${GPU1:-0}"
exec "${VLLM_BIN:-/workspace/venvs/vllm/bin/vllm}" serve Qwen/Qwen3.8-27B-FP8 \
  --served-model-name qwen3.8-27b \
  --host 127.0.0.1 --port 8001 \
  --reasoning-parser qwen3 \
  --max-model-len 32768 --max-num-seqs 8 \
  --gpu-memory-utilization "${GPU_FRAC1:-0.45}"
