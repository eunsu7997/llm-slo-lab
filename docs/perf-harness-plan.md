# Performance Harness — Interface & Extension Plan

Status: **MVP implemented** (`perf/`, CLI `python -m perf.loadtest`) for the CPU mock: fixed request count,
bounded concurrency, P50/P95/P99, RPS, error rate, SLO verdict. Sections 2–3 below are the original design;
the implemented definitions are in section 7. Nothing in this document is a measured result.

## 1. What can and cannot be measured

| Target | Measurable metrics | Not measurable |
|---|---|---|
| Mock server (this repo) | End-to-end request latency, RPS, error rate, behaviour under concurrency | TTFT, TPOT, tokens/s, GPU utilisation |
| Real LLM server (e.g. vLLM, future) | Above + TTFT/TPOT via SSE streaming | — |

Mock latency = configured `LLM_MOCK_DELAY_MS` sleep + HTTP/framework overhead. Results against the mock
validate the **harness and SLO logic**, not model performance. Reports must state the target type.

## 2. Run configuration (input)

```yaml
target:
  base_url: http://127.0.0.1:8000     # localhost only
  kind: mock                          # mock | openai-compatible
  model: mock-model
workload:
  concurrency: [1, 4, 16]             # sweep
  duration_s: 60
  warmup_s: 10                        # excluded from metrics
  request_timeout_s: 30
  retries: 0                          # retries hide errors; default off
  payload: {messages: [{role: user, content: "hi"}]}
slo:
  p95_latency_ms: 800
  error_rate_max: 0.01
  min_rps: 10
```

## 3. Result schema (output, one per concurrency level)

```json
{
  "target_kind": "mock",
  "concurrency": 16,
  "window": {"start": "...", "end": "...", "warmup_excluded_s": 10},
  "requests": {"sent": 0, "ok": 0, "http_error": 0, "timeout": 0, "client_error": 0},
  "latency_ms": {"p50": 0, "p95": 0, "p99": 0, "max": 0},
  "rps": 0,
  "error_rate": 0,
  "ttft_ms": null,
  "tpot_ms": null,
  "slo": {"p95_latency": "PASS|FAIL", "error_rate": "PASS|FAIL", "rps": "PASS|FAIL", "overall": "PASS|FAIL|UNVERIFIED"},
  "environment": {"os": "...", "python": "...", "server_config": {"delay_ms": 0, "error_rate": 0, "seed": null}}
}
```

`ttft_ms`/`tpot_ms` stay `null` for the mock and for any non-streaming target.

## 4. Definitions to fix before implementation

- **Latency**: client-side send → full response received, successful requests only; failures counted separately.
- **Error rate** = (HTTP 5xx + timeouts + connection errors) / requests sent in the measurement window.
  4xx caused by the harness's own payload is a harness bug → run is UNVERIFIED.
- **Percentiles**: computed from raw samples (no averaging of percentiles across runs); report sample count.
- **SLO overall**: FAIL if any SLO fails; UNVERIFIED if the run aborted, sample count is below a minimum,
  or the target was unreachable (consistent with `docs/quality-gates.md`).
- Client and server on the same CPU machine interfere; record CPU count/load and treat results as relative.

## 5. Known limits of the mock for load testing

- Error injection is reproducible only for **sequential** requests with a fixed `LLM_MOCK_SEED`.
  Under concurrency the RNG draw order depends on scheduling, so which request fails is not reproducible
  (only the long-run rate). Multiple workers each seed their own RNG.
- No concurrency limit / queueing in the mock, so saturation behaviour differs from a real inference server.

## 6. Extension steps (future, each needs approval)

1. ~~Load generator using the existing `httpx` async client~~ — done (MVP, section 7).
2. ~~SLO evaluator: pure function `result + slo config → verdicts`~~ — done (`perf/metrics.py`).
3. Optional streaming support in a separate real-server adapter for TTFT/TPOT (requires a real LLM server).
4. Not yet: duration-based runs, warm-up exclusion, concurrency sweeps in one command, min-RPS objective.

## 7. Implemented MVP — definitions

| Item | Definition |
|---|---|
| Workload | Exactly `--requests` requests, closed loop: `min(concurrency, requests)` workers, each sends the next request after the previous one finishes, so in-flight ≤ `--concurrency`. No retries (`httpx` transport `retries=0`), no warm-up. |
| Request | `POST /v1/chat/completions` with only `model`, `messages` (fixed short prompt), i.e. parameters the mock supports. |
| Target | `http://127.0.0.1` / `localhost` / `::1` only (enforced). `--spawn-mock` starts `app.main` on a free port. |
| Timeout | `--timeout-s` is a **whole-request deadline** (`asyncio.timeout`: connect → full response body) and also each httpx phase timeout. A server that trickles bytes cannot exceed it. Exceeding it → `timeout` (`error_type` `DeadlineExceeded` or the httpx timeout class). |
| Outcome | `success` = HTTP 200 **and** valid `chat.completion` structure. Failures: `http_error`, `invalid_response`, `timeout`, `connect_error`, `transport_error`. |
| HTTP error classes | Every `http_error` counts in the error rate and is also classified: `rate_limited_429` (server rejection/throttling — a genuine failure), `request_invalid_4xx` (any other 4xx — request/config suspect), `server_error_5xx`, `other_status`. Per-status counts are in `http_error_status_counts`. |
| Latency | Client-side round trip (`perf_counter`, request start → response fully read) of **successful** requests only, ms. Failed/timed-out requests are excluded, so more timeouts can make P95 look better — always read latency together with the error rate. |
| Percentiles | Nearest-rank: sort ascending, rank = ⌈p/100 · n⌉ (1-based, exact arithmetic). n = 0 → not computable (`null`), never 0. With n < 20, P95 is the maximum sample; with n < 100, P99 is the maximum sample — tail percentiles from small samples are unstable. |
| Error rate | failed / attempted × 100 (%). |
| RPS | completed requests (any outcome) / window, where window = first request start → last request end. `success_rps` uses successes only. |
| Precision | `summary.json` and `requests.jsonl` keep **raw** (unrounded) values and the SLO is judged on them. Only `report.md` and console output are rounded (`perf.metrics.display`). |
| SLO objectives | `--slo-p95-ms` (default 1000), `--slo-error-rate-pct` (default 1), `--slo-min-requests` (default 100, total completed requests), `--slo-min-latency-samples` (default 20, successful samples required to judge latency objectives), optional `--slo-p50-ms`, `--slo-p99-ms`. Comparisons are inclusive (≤). |
| Verdict | FAIL > UNVERIFIED > PASS over all checks. **Run invalid → UNVERIFIED, objectives not evaluated**: nothing completed, run incomplete, target unreachable (preflight `/healthz` fails or every request fails to connect). Otherwise: latency objective with 0 successes (not computable) or fewer than `min_latency_samples` successes → UNVERIFIED; total below `min_requests` → UNVERIFIED; any 4xx other than 429 adds a `request_validity` check = UNVERIFIED **while the objectives are still evaluated**, so a confirmed FAIL (e.g. error rate) still makes the run FAIL; no objectives configured → UNVERIFIED. HTTP 429 is never treated as a configuration problem. |
| Exit code | 0 PASS, 1 FAIL, 2 UNVERIFIED (including invalid CLI arguments). Equals `exit_code` in `summary.json`. |
| Report | `reports/loadtest-<timestamp>/`: `config.json` (settings, Python/OS/CPU, source hashes), `requests.jsonl` (per-request timings/status/outcome, no bodies), `report.md`, `summary.json` (written last, atomically; `complete: false` and never PASS if the report could not be saved). |

The mock has no queueing and runs on the same CPU as the load generator, so results are relative and
describe the mock + HTTP stack only.

## 8. Repeat experiments & regression check (`python -m perf.repeat`)

| Item | Definition |
|---|---|
| Runs | `--runs` 1..10 (default 5) against ONE mock server instance. Before each run, `--warmup-requests` (default 20, 0..1000) are sent and excluded from metrics; their failures are still recorded (`warmup.failed`). An aborted/interrupted experiment keeps the completed runs and is marked `aborted`; missing runs make the SLO verdict UNVERIFIED. |
| Per run | Same metrics and SLO judgement as `perf.loadtest` (raw values). Per-request data in `runs/run-<n>-requests.jsonl`. |
| Across runs | Median, min, max and spread `(max-min)/median·100` of P50/P95/P99, RPS, success RPS, error rate. The **mean is never used** for judgement. Error rate is also **pooled** (Σ failed / Σ attempted) so one bad run cannot hide behind a median. |
| Baseline | `--baseline <experiment.json or its directory>` from an earlier `perf.repeat` run. **The stored numbers are not trusted:** structure, types and finiteness (NaN/Infinity rejected at parse time) are validated, `runs_requested` must equal the stored runs, every run's metrics are **recomputed from `runs/run-<n>-requests.jsonl`** (records must cover indices 0..requests-1 exactly) and must match the stored metrics, and the aggregate is recomputed from those runs and must match the stored aggregate (tolerance 1e-9). Only the recomputed values are used for the comparison. Any missing, malformed or inconsistent item → baseline rejected → regression UNVERIFIED (a candidate's own SLO FAIL is kept). Not verifiable from the evidence and therefore only reported, not claimed as verified: `server_config`, warm-up results, timestamps/environment/source hash. **No baseline → regression UNVERIFIED (never PASS)**, so a baseline-creating run exits 2 by design. |
| Thresholds | All regression thresholds must be finite and in range (`p95` ≥ 0, `success_rps` 0..100, `error_rate_pp` 0..100, `spread` ≥ 0, `min_runs` integer 1..10); `Thresholds(...)` raises otherwise and `compare()` re-checks (returns UNVERIFIED) in case the object was bypassed. CLI floats (`--timeout-s`, `--slo-*`, `--mock-error-rate`, `--max-*`) must be finite and in range; otherwise exit 2 before any measurement. |
| Evidence saving | Each run is judged and kept in memory **before** its request records are written. A write failure is recorded in `evidence_errors` (run keeps `requests_saved: false`) and makes the overall verdict at least UNVERIFIED; an SLO FAIL already found is kept. |
| Comparable only if | identical `target_kind`, `method`, `requests`, `concurrency`, `timeout_s`, `warmup_requests`, `model`, `min_latency_samples`; both sides ≥ `--min-runs` (default 3) runs, complete, not aborted, all runs valid. Mock server settings (delay/error rate/seed) must also match, unless `--allow-server-change` explicitly declares the server change as the thing under test (recorded as an accepted difference). External `--url` targets have unknown settings → UNVERIFIED. |
| P95 check | change = (cand median − base median)/base median·100. FAIL if change > `--max-p95-increase-pct` (default 20) **and** every candidate run's P95 > every baseline run's P95. Above threshold with overlapping ranges → UNVERIFIED. Within threshold but either side's spread > `--max-spread-pct` (default 25) → UNVERIFIED (cannot rule out a regression). Too few successful latency samples in any run → this check UNVERIFIED only. |
| Success RPS check | Mirror of the P95 rule with `--max-success-rps-decrease-pct` (default 20): FAIL only if every candidate run is below every baseline run. |
| Error-rate check | pooled candidate − pooled baseline (percentage points) > `--max-error-rate-increase-pp` (default 1.0) → FAIL. |
| Verdicts | `slo_verdict` (per-run SLOs) and `regression_verdict` are reported separately; `verdict` = FAIL > UNVERIFIED > PASS of both; exit 0/1/2 equals `exit_code` in `experiment.json` (written last; never PASS if saving failed). |

**Measurement limits (not yet addressed):** the closed-loop model lowers the offered load when the server slows
down, so queueing delay at a fixed arrival rate is not observed (coordinated omission); client/server CPU
interference adds noise. `perf.loadtest` is a single run without warm-up, so one result cannot distinguish a
regression from noise — use `perf.repeat` (warm-up + repeated runs) for comparisons, and even then baseline and
candidate are measured at different times on a shared machine. Numbers are valid only for the exact closed-loop
configuration recorded in the report.
