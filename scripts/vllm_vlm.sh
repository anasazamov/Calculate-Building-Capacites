#!/bin/bash
# The vision model: Qwen3-VL-32B-Instruct (official FP8 build) on 127.0.0.1:8002. It reads the writing inside
# raster images of the project (scans, pictures of tables, raster drawings) with a box for every item
# (feecalc/ingest.py). Shares the single 96 GB card with the reading model (vllm_model1.sh, 0.55), so it gets
# 0.42: ~35 GiB of weights (the vision tower stays in BF16), the rest is KV cache, kept in FP8 to fit.
# One image of at most 2048 px is ~4k tokens and an answer at most ingest.VLM_MAX_TOKENS: hence 16384.
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES="${GPU_VLM:-0}"
exec "${VLLM_BIN:-/workspace/venvs/vllm/bin/vllm}" serve Qwen/Qwen3-VL-32B-Instruct-FP8 \
  --served-model-name qwen3-vl-32b \
  --host 127.0.0.1 --port 8002 \
  --max-model-len "${MAX_LEN_VLM:-16384}" --max-num-seqs "${MAX_SEQS_VLM:-8}" \
  --kv-cache-dtype fp8 \
  --limit-mm-per-prompt '{"image": 1}' \
  --gpu-memory-utilization "${GPU_FRAC_VLM:-0.42}"
