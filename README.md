# LLM-SLO Lab — Mock Inference Server (Stage 1)

A minimal **mock** server that mimics a small subset of the OpenAI Chat Completions API
for exercising latency/error handling of LLM clients and infrastructure on CPU.

> **This server does not run an LLM.** Every successful response is the fixed string
> `[MOCK] This is a mock response. No LLM inference was performed.`
> The configured delay is an artificial `asyncio.sleep`, **not** a TTFT/TPOT measurement.
> No TTFT, TPOT or tokens/s metrics are produced by this project.

## Requirements

- Python **3.11+**. The pinned dependencies require it (`starlette==1.8.0` and `websockets==17.2`
  declare `Requires-Python >=3.11`). Verified on **Python 3.13.15 / Windows 11** only.
- `requirements-lock.txt` was generated on Windows (includes `colorama`); other OSes are unverified.
- No GPU, no Docker.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Exact reproduction of the verified environment:
# .\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

## Run (localhost only)

```powershell
$env:LLM_MOCK_DELAY_MS = '200'     # optional
$env:LLM_MOCK_ERROR_RATE = '0.1'   # optional
$env:LLM_MOCK_SEED = '42'          # optional
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Do not bind to `0.0.0.0` or expose it publicly: there is no authentication or rate limiting.

| Variable | Range | Default | Meaning |
|---|---|---|---|
| `LLM_MOCK_DELAY_MS` | 0 – 60000 | 0 | Artificial non-blocking delay before each chat response |
| `LLM_MOCK_ERROR_RATE` | 0.0 – 1.0 | 0 | Probability of returning a simulated HTTP 500 |
| `LLM_MOCK_SEED` | integer | unset | Seeds the error-injection RNG for reproducible patterns |

Invalid values make the server fail at startup with `ValueError: invalid <NAME>=...` or a range message.

## Test & verify

```powershell
.\.venv\Scripts\python.exe -m pytest -v                                         # 223 tests
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1           # full quality gate
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\selftest_verify.ps1  # harness self-test
```

`verify.ps1` runs pytest and a real-Uvicorn TCP smoke test on `127.0.0.1`, writes logs and the environment
to `reports/<timestamp>/`, and exits 0 = PASS, 1 = FAIL, 2 = UNVERIFIED.
Rules: [docs/quality-gates.md](docs/quality-gates.md). Agent roles: `CLAUDE.md`, `AGENTS.md`.
Future load-test/SLO design: [docs/perf-harness-plan.md](docs/perf-harness-plan.md).

## Load test & SLO check (CPU mock)

```powershell
# start a mock with 200 ms artificial delay, send 100 requests with 10 in flight, judge default SLOs
.\.venv\Scripts\python.exe -m perf.loadtest --spawn-mock --mock-delay-ms 200 --requests 100 --concurrency 10
# against an already running localhost mock, with custom objectives
.\.venv\Scripts\python.exe -m perf.loadtest --url http://127.0.0.1:8000 --requests 200 --concurrency 20 `
    --slo-p95-ms 500 --slo-error-rate-pct 0.5 --slo-min-requests 200
```

Exit code 0 = PASS, 1 = FAIL, 2 = UNVERIFIED. Results go to `reports/loadtest-<timestamp>/`.

Repeat runs and compare with a baseline (regression check):

```powershell
# 1) baseline: 5 runs (exits 2 by design: no baseline to compare with yet)
.\.venv\Scripts\python.exe -m perf.repeat --spawn-mock --mock-delay-ms 100 --runs 5 --requests 200 --concurrency 10
# 2) candidate under identical conditions, compared with the baseline
.\.venv\Scripts\python.exe -m perf.repeat --spawn-mock --mock-delay-ms 100 --runs 5 --requests 200 --concurrency 10 `
    --baseline reports\repeat-<timestamp>\experiment.json
```

Results go to `reports/repeat-<timestamp>/`. Rules: [docs/perf-harness-plan.md](docs/perf-harness-plan.md) §8.
Definitions (nearest-rank percentiles, RPS, error rate, verdict rules): [docs/perf-harness-plan.md](docs/perf-harness-plan.md) §7.
**Numbers measured against the mock are artificial delay + HTTP overhead, not LLM inference performance.**

## API scope

### `GET /healthz`
Returns `200 {"status": "ok"}`. Not affected by delay or error injection.

### `POST /v1/chat/completions` (non-streaming only)

Accepted request fields — **any other field is rejected** with `422 unsupported_parameter`:

| Field | Rule |
|---|---|
| `model` | string, 1–256 chars after trimming whitespace; echoed back |
| `messages` | 1–256 items, each `{role, content}` only |
| `messages[].role` | `system`, `user`, `assistant` |
| `messages[].content` | string, max 32,000 chars (content-part arrays not supported) |
| `stream` | optional, must be `false` |

Response:

```json
{
  "id": "chatcmpl-mock-<hex>",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "<as requested>",
  "choices": [{"index": 0,
               "message": {"role": "assistant", "content": "[MOCK] ..."},
               "finish_reason": "stop"}]
}
```

### Errors

All errors use one JSON shape:

```json
{"error": {"message": "...", "type": "invalid_request_error | server_error", "param": "... | null", "code": "..."}}
```

| Status | `code` | When |
|---|---|---|
| 422 | `unsupported_parameter` | Unknown / unsupported field (e.g. `n`, `max_tokens`, `temperature`, `tools`, typos) |
| 422 | `invalid_value` | Wrong type, missing field, out-of-range value, malformed JSON, `stream: true` |
| 413 | `body_too_large` | Request body > 1,000,000 bytes (also enforced for chunked uploads) |
| 404 / 405 | `http_404` / `http_405` | Unknown path / wrong method (405 includes `Allow: POST`) |
| 500 | `simulated_error` | Injected by `LLM_MOCK_ERROR_RATE` |
| 500 | `internal_error` | Unexpected exception (details logged server-side, not returned) |

Validation error messages do not echo the submitted input values.

## Not supported / known limitations

- Streaming (SSE), `n`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `stop`,
  `response_format`, `tools` / function calling, `logprobs`, `developer` / `tool` roles,
  multimodal content parts — all rejected rather than silently ignored.
- Response omits `usage`, `system_fingerprint`, `choices[].logprobs`, `message.refusal`.
  Clients that require these fields may fail.
- Validation errors return **422** (OpenAI returns 400 for invalid requests).
- Not tested against the official OpenAI SDK.
- Only a request-body size limit is enforced; there is no concurrency limit, rate limit or auth.
- `LLM_MOCK_SEED` reproduces the error pattern only for **sequential** requests (one RNG draw per valid
  chat request, in arrival order). With concurrent requests, which request gets the error depends on
  scheduling and is **not** reproducible; with multiple workers each worker has its own RNG.
- The body limit is 1,000,000 bytes (decimal), not 1 MiB.
- Latency measured against this server reflects the artificial delay plus framework overhead
  only and must not be reported as LLM inference performance.
