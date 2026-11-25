# Infrastructure fee pilot: reading building volumes from design documents

A pilot that answers one question: can open models running on our own server reliably read
building volumes (m³) out of a project album (PDF) and a state expertise conclusion (PDF)?
The volumes feed the infrastructure fee defined by Cabinet of Ministers Resolution No. 151
(08.04.2026), Annex 4:

    Ti = BHM × ((Hb + Hyu) − (Ha + Ht + Hu)) × Kt × Ko × Kz × Kj × Kcouncil

The task statement is in `TASK.md` (Uzbek). The test report is in `HISOBOT.md` (Uzbek).
The user interface is in Uzbek (Latin script); code, comments and commit messages are in English.

## Principles

1. **The model never chooses coefficients and never computes money.** It only finds values in
   the documents and classifies rooms. Coefficients are chosen by staff; the fee is computed
   by code in `Decimal` (`feecalc/fee.py`).
2. **Every value keeps its source**: file, page and the verbatim text fragment. If the fragment
   is not found verbatim on that page, or the number cannot be parsed out of it, the value is
   not accepted (`feecalc/verify.py`).
3. **The model never sees both documents in one request.** The project and the conclusion are
   read separately; the comparison is done by code.
4. **Document content never leaves the server.** No external API, no telemetry; the model
   servers and the web server listen on `127.0.0.1` only.
5. **No silent guessing.** A missing value is "not found", a value that fails verification is
   "unverified", a malformed model response stops the job with the full error.

## Layout

| Path | Purpose |
|---|---|
| `feecalc/ingest.py` | page text layer with coordinates, PNG rendering, table and title-block extraction |
| `feecalc/classify.py` | sheet type (rules, then model) and room category dictionary (Ha / Ht / Hu / other) |
| `feecalc/extract.py` | prompts, JSON schemas, model requests, verified value records |
| `feecalc/verify.py` | verbatim source check, strict number parsing, sums, equality, address, model agreement |
| `feecalc/volumes.py` | Hb per building, Ha / Ht / Hu components, Hyu proposal, industrial 3 m indicator |
| `feecalc/fee.py` | the fee formula in `Decimal`, coefficient options, rounding |
| `feecalc/pipeline.py` | extraction run (model-backed) and assessment (pure code) |
| `feecalc/llm.py` | client for the local vLLM servers: raw response store, schema check, failure detection |
| `feecalc/web/` | FastAPI + Jinja2 + plain JS interface, SQLite storage, audit log |
| `scripts/` | model launch scripts, watchdog, smoke test, sample reading and scoring |
| `tests/` | pytest suite; the full pipeline is replayed offline from recorded model responses |
| `test_results/` | committed test summaries (counts only, no document content) |

## Requirements

- Linux with an NVIDIA GPU. Tested on one RTX PRO 6000 Blackwell (96 GB), driver 580.95,
  Python 3.12, [uv](https://docs.astral.sh/uv/).
- About 100 GB of free disk for three models (30–33 GB each).
- Internet access is needed only to install packages and download model weights.

## Setup from scratch

```bash
git clone https://github.com/anasazamov/Calculate-Building-Capacites.git
cd Calculate-Building-Capacites

# 1. application environment
uv venv .venv --python 3.12
uv pip install -p .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q                      # no GPU or model needed

# 2. model serving environment (separate, large)
mkdir -p /workspace/venvs
uv venv /workspace/venvs/vllm --python 3.12
uv pip install -p /workspace/venvs/vllm/bin/python vllm==0.34.0 --torch-backend=auto

# 3. model weights (public, no token required)
export HF_HOME=/workspace/.hf_home HF_HUB_DISABLE_TELEMETRY=1
uv venv /workspace/venvs/hf --python 3.12
uv pip install -p /workspace/venvs/hf/bin/python "huggingface_hub[hf_xet]"
/workspace/venvs/hf/bin/hf download Qwen/Qwen3.8-27B-FP8                # model 1
/workspace/venvs/hf/bin/hf download RedHatAI/gemma-4-31B-it-FP8-block   # model 2 (optional)
```

If a download stalls on its last file, stop it and run the same command again: it resumes.

## Running

```bash
# model servers, each supervised by a watchdog (canary every 60 s, restart on failure).
# Each command returns when its server is healthy (1.5-4 minutes). Start them one after another,
# model 1 first: two servers loading at the same moment on a shared card ignore their memory shares.
scripts/start_watchdog.sh model1        # Qwen3.8-27B-FP8      on 127.0.0.1:8001
scripts/start_watchdog.sh model2        # Gemma-4-31B-it (FP8) on 127.0.0.1:8002, optional
tail logs/incidents.jsonl               # start, restart and failure events

# web interface on 127.0.0.1:8000 (foreground; add `nohup ... &` to keep it running)
.venv/bin/python -m feecalc.web
```

From your workstation open an SSH tunnel and browse to <http://127.0.0.1:8000>:

```bash
ssh -N -L 8000:127.0.0.1:8000 user@server
```

Listening on a public address is a deviation from principle 4 and is off by default. If it is
needed, set `FEECALC_HOST=0.0.0.0` together with a password (16+ characters) and a TLS pair;
the server refuses to start on a non-loopback address without them. The model servers stay on
`127.0.0.1` in every case.

The interface works with model 1 alone; when model 2 is running, its answers are shown next
to model 1 and disagreements are flagged.

Stop the servers with `scripts/stop_watchdog.sh model1` / `model2`.

Both models share one 96 GB card: model 1 gets 40% of the memory and model 2 gets 55%
(`GPU_FRAC1`, `GPU_FRAC2`). On a machine with two cards set `GPU1=0 GPU2=1` and raise both
fractions to 0.90.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `FEECALC_DATA` | `data` | uploads, page images, raw model responses, SQLite database |
| `FEECALC_MODEL1_URL` / `FEECALC_MODEL1_NAME` | `http://127.0.0.1:8001/v1` / `qwen3.8-27b` | model 1 |
| `FEECALC_MODEL2_URL` / `FEECALC_MODEL2_NAME` | `http://127.0.0.1:8002/v1` / `gemma-4-31b-it` | model 2 |
| `FEECALC_HOST` / `FEECALC_PORT` | `127.0.0.1` / `8000` | web listen address |
| `FEECALC_PASSWORD` | empty | HTTP Basic password (any user name); required on a non-loopback address |
| `FEECALC_TLS_CERT` / `FEECALC_TLS_KEY` | unset | TLS pair; required on a non-loopback address |
| `FEECALC_LOCAL_PASSWORD` | same as `FEECALC_PASSWORD` | password asked on the loopback listener when `FEECALC_HOST` is public |
| `FEECALC_LOCAL_PORT` | `8000` | plain-HTTP loopback listener kept for SSH tunnels when `FEECALC_HOST` is public |
| `FEECALC_DEGENERATE_MARKER` | `logs/degenerate_marker.jsonl` | marker file watched by the watchdog |

## Tests and measurements

```bash
.venv/bin/pytest -q                                   # unit and replay tests
.venv/bin/python scripts/smoke_test.py model1         # canary, 50 sequential + 16 parallel requests
.venv/bin/python scripts/read_samples.py model1       # read every pair in ~/samples
.venv/bin/python scripts/score_samples.py model1      # score against the etalon files
.venv/bin/python scripts/load_test.py model1          # realistic requests at 1, 8 and 16 in parallel
.venv/bin/python -m tests.record_replay               # re-record replay data after prompt changes
```

`~/samples` holds `<id>_xulosa.pdf` (conclusion) and optionally `<id>_loyiha.pdf` (project).
Sample documents, uploads and raw model responses are never committed (`data/` is ignored).

## Request settings

Thinking off (`chat_template_kwargs: {"enable_thinking": false}`), response constrained by a
JSON schema with bounded string lengths, fixed `max_tokens`, `temperature 0.2`, `top_p 0.8`,
`top_k 20`, `presence_penalty 0`. The prompt always states the response shape explicitly:
without it, schema-constrained output from Qwen degrades (see `HISOBOT.md`).

## Notes for maintainers

- PyMuPDF is not thread-safe: every use goes through `ingest.MUPDF_LOCK`.
- PyMuPDF 2.1 runs a bundled layout model (ONNX) inside `find_tables()`; `feecalc/ingest.py`
  switches it off (`pymupdf._layout_enabled = False`). Re-check this after upgrading PyMuPDF.
- After changing prompts, schemas or the test fixtures, re-record the replay data
  (`python -m tests.record_replay`, model 1 must be running).
- `no try/except` is a project rule: errors must surface with their full trace. The only catch
  is the job boundary in `feecalc/web/app.py` (and the per-sample boundary of the test scripts).
