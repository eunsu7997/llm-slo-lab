# Quality Gates (single source of truth)

`CLAUDE.md` and `AGENTS.md` refer to this file. If anything conflicts, **this file wins**.

## 1. Verdicts

| Verdict | Meaning | Requires |
|---|---|---|
| **PASS** | The required check actually ran and succeeded | Execution evidence: command, exit code 0, log |
| **FAIL** | A required check ran and failed, **or** a defect was confirmed | Failing output, or a reproducible defect with file/line |
| **UNVERIFIED** | The check could not be performed (permission, environment, missing tool, skipped) | The reason it could not run |

Rules:

1. A check that was not executed is **never** PASS.
2. UNVERIFIED is **never** silently converted to PASS or FAIL. Re-run it in a working environment instead.
3. "Python/venv cannot be launched" (e.g. sandbox access denied, `did not find executable`, launcher exit 103)
   is an **environment** problem → UNVERIFIED. It is **not** a project test failure.
4. Static code reading can produce a FAIL only for a concrete, reproducible defect; otherwise label it
   "static analysis" and keep runtime claims UNVERIFIED.
5. Overall verdict precedence: any FAIL → **FAIL**; else any UNVERIFIED → **UNVERIFIED**; else **PASS**.
   This applies on **every** path: a later exception, environment error or report-writing problem is recorded
   as an extra UNVERIFIED gate (`harness`, `report`, `arguments`, smoke `environment` check) and can never
   remove or downgrade an already recorded FAIL.
6. An exit code is not enough for PASS: the gate's evidence file must exist and agree
   (pytest JUnit XML; `smoke.json` with verdict PASS, at least `-MinSmokeChecks` checks, none failed).
   Missing/unreadable evidence → UNVERIFIED; evidence that records a failure or is inconsistent → FAIL.
   The exit-code verdict is recorded **before** the evidence is read; an evidence problem is added as a separate
   UNVERIFIED gate (e.g. `pytest_evidence`) and never replaces a FAIL.
7. A report is valid only when `summary.json` exists with `"complete": true`. `summary.md` and `summary.json`
   are written to `.tmp` files first and `summary.json` is moved into place last. If saving fails the run ends
   as UNVERIFIED (or FAIL if a FAIL was already recorded) with a fallback `summary.json` marked `"complete": false`.
   A missing `summary.json` or `"complete": false` is never PASS.
8. Bad or unknown `verify.ps1` arguments → UNVERIFIED (exit 2), not FAIL.

## 2. Required gates

Run with `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1`
(exit 0 = PASS, 1 = FAIL, 2 = UNVERIFIED). Reports go to `reports/<timestamp>/`.

| Gate | Command | PASS | FAIL | UNVERIFIED |
|---|---|---|---|---|
| preflight | `.venv\Scripts\python.exe -c "import fastapi, uvicorn, pytest, httpx, pydantic"` | exit 0 | — | interpreter missing / cannot launch / imports fail |
| pytest | `python -m pytest --junitxml=...` | exit 0, 0 failures/errors, executed ≥ `-MinTests` (currently **223**) | exit 1, 2, 5, or fewer tests than minimum | exit 3/4 (pytest internal/usage error), not executed, skipped |
| smoke | `python scripts/smoke_tcp.py` (real Uvicorn on `127.0.0.1`) | exit 0 **and** `smoke.json` verdict PASS with ≥ `-MinSmokeChecks` (currently **17**) checks, all PASS | any FAIL check recorded (even with exit 0 or after a later environment error), server failed to start, script crashed, fewer checks than minimum | exit 2 with no FAIL check (no localhost socket / cannot spawn), exit 0 without `smoke.json`, not executed, skipped |

`-SkipPytest` / `-SkipSmoke` exist only for diagnosis; they always yield UNVERIFIED for that gate.

When tests or smoke checks are added, raise `-MinTests` / `-MinSmokeChecks` defaults in `scripts/verify.ps1`
and the numbers above in the same change. A count guards against missing tests, not against renamed/replaced ones.

## 3. Harness self-test

`scripts/selftest_verify.ps1` injects failures into **temporary copies** (never the original), runs the copy's
`verify.ps1`, and checks exit code, `summary.json` verdict/exit_code consistency and per-gate statuses.
Run it after changing `verify.ps1` or `smoke_tcp.py`.

| Case | Expected |
|---|---|
| baseline | PASS (0) |
| pytest failure / server start failure / no tests collected / too few tests | FAIL (1) |
| A: pytest FAIL, then exception while producing the report | FAIL (1) |
| B: smoke check FAIL, then environment error inside smoke | FAIL (1) |
| D: smoke exit 0 but `smoke.json` records a failed check | FAIL (1) |
| E2: smoke exit 0, `smoke.json` PASS with no checks | FAIL (1) |
| Python missing / launcher exit 103 (bad base-Python home) / execute permission denied (process never starts) | UNVERIFIED (2) |
| nothing executed (both gates skipped) | UNVERIFIED (2) |
| C1/C2: `summary.md` or final `summary.json` cannot be saved | UNVERIFIED (2), `complete: false` |
| E1: smoke exit 0 but no `smoke.json` | UNVERIFIED (2) |
| G: invalid / unknown arguments | UNVERIFIED (2) |
| H1/H2/H5: pytest exit 1 / 2 / 5 **and** corrupted JUnit XML | FAIL (1), plus `pytest_evidence` UNVERIFIED |
| H0: pytest exit 0 but corrupted JUnit XML | UNVERIFIED (2), never PASS |

## 4. Severity of findings

| Severity | Definition | Effect on approval |
|---|---|---|
| **Critical** | Wrong results reported as correct (e.g. failure shown as PASS), data/secret leak, exposure beyond localhost | Blocks approval; must be reported to the user before final approval |
| **High** | Required gate FAIL; contract violation in a supported API path | Blocks approval |
| **Medium** | Weak test that can miss a regression; inconsistent documentation of limits | Fix or explicitly accept |
| **Low** | Cosmetic, clarity, non-blocking hygiene | Optional |

Every finding states: severity, file:line, reproduction steps, whether it was **executed** or **static**.

## 5. Measurement honesty

- This project's server is a **mock**. Measured latency = artificial `asyncio.sleep` + HTTP/framework overhead.
- Never label mock latency as TTFT, TPOT, tokens/s or LLM inference performance.
- Never invent, round up, or estimate numbers that were not produced by an executed command.

## 6. Safety constraints (all agents)

- Bind servers to `127.0.0.1` only. No public exposure.
- Never delete, recreate or reinstall `.venv` without explicit user approval.
- No automatic `git commit` / `push`. No automatic Review Gate activation.
- No API keys, tokens or personal data in code, logs or reports.
- Do not modify files outside the project folder (temporary files go to the system temp/scratch dir).
