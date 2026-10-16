# CLAUDE.md — Developer agent (Claude Code)

Verdicts, required gates, severity and safety rules are defined **only** in
[docs/quality-gates.md](docs/quality-gates.md). Follow it; do not restate or override it here.

## Role

- **User**: decides goals, approves plans and final results.
- **Claude Code (this agent)**: design, implementation, fixes, running verification.
- **Codex**: independent QA / review (see `AGENTS.md`). Codex is invoked by the user, not automatically.

## Working rules

1. Propose a short plan and wait for approval before non-trivial changes.
2. Keep changes minimal; do not change `app/main.py` behaviour unless the task requires it.
3. After any change, re-run the relevant tests; before reporting completion run
   `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1` and report its verdict and exit code.
   After changing `verify.ps1` or `smoke_tcp.py`, also run `scripts\selftest_verify.ps1`.
4. Never delete or weaken existing tests to make a gate pass. Adding tests/smoke checks → update
   `-MinTests` / `-MinSmokeChecks`.
5. Contract limits in `tests/test_contract.py` are literals on purpose; change them only together with README.
6. Report PASS / FAIL / UNVERIFIED exactly as executed; list unverified items explicitly.
7. Do not add external dependencies without approval; the harness uses only the existing venv.

## File ownership (no concurrent edits)

- While a Codex review is running, Claude Code does **not** edit project files; wait for the report.
- Codex is read-only by default, so only Claude Code edits source, tests, scripts and docs.
- If the user explicitly gives Codex write access for a task, Claude Code stays off those files until it finishes.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest -v                                        # unit/contract tests
.\.venv\Scripts\python.exe scripts\smoke_tcp.py                                 # real TCP smoke test
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify.ps1          # quality gate
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\selftest_verify.ps1 # harness self-test
.\.venv\Scripts\python.exe -m perf.loadtest --spawn-mock --requests 100 --concurrency 10  # mock load test + SLO
```
