#!/bin/bash
# The vision model: Qwen3-VL-32B-Instruct (official FP8 build) on 127.0.0.1:8002. It reads the writing inside
# raster images of the project (scans, pictures of tables, raster drawings) with a box for every item
# (feecalc/ingest.py). Shares the single 96 GB card with the reading model (vllm_model1.sh, 0.47):
# ~36 GB of weights (the vision tower stays in BF16) plus ~7 GB of KV cache at 0.45.
# One image of at most 2048 px is ~4k tokens; a dense table answer needs up to 12k: hence 20480.
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 VLLM_NO_USAGE_STATS=1 VLLM_DO_NOT_TRACK=1
export CUDA_VISIBLE_DEVICES="${GPU_VLM:-0}"
exec "${VLLM_BIN:-/workspace/venvs/vllm/bin/vllm}" serve Qwen/Qwen3-VL-32B-Instruct-FP8 \
  --served-model-name qwen3-vl-32b \
  --host 127.0.0.1 --port 8002 \
  --max-model-len "${MAX_LEN_VLM:-20480}" --max-num-seqs "${MAX_SEQS_VLM:-8}" \
  --limit-mm-per-prompt '{"image": 1}' \
  --gpu-memory-utilization "${GPU_FRAC_VLM:-0.45}"
