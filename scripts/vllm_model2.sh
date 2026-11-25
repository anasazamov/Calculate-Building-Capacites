#!/bin/bash
# Model 2 (independent second reader): Gemma-4-31B-it, ready-made FP8 build, on 127.0.0.1:8002.
# Shares the single 96 GB card with model 1 (see vllm_model1.sh). Gemma needs a 15.8 GiB KV cache for a
# 32768-token context on top of 33 GB of weights, hence the 0.55 share (model 1 gets 0.40).
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES="${GPU2:-0}"
exec "${VLLM_BIN:-/workspace/venvs/vllm/bin/vllm}" serve RedHatAI/gemma-4-31B-it-FP8-block \
  --served-model-name gemma-4-31b-it \
  --host 127.0.0.1 --port 8002 \
  --reasoning-parser gemma4 \
  --max-model-len "${MAX_LEN2:-32768}" --max-num-seqs "${MAX_SEQS:-16}" \
  --gpu-memory-utilization "${GPU_FRAC2:-0.55}"
