"""Load generator and CLI against real Uvicorn servers on 127.0.0.1 (plus a few in-process cases).

Timing assertions are deliberately loose (lower bounds / relative comparisons only).
Measured values come from the mock and are not LLM inference performance.
"""

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.main import Settings, create_app
from perf import loadtest
from perf.metrics import summarize
from perf.mock_server import PROJECT_ROOT, free_port, spawn_mock
from perf.runner import run_load


@pytest.fixture(scope="module")
def servers():
    """Lazily started real mock servers, keyed by (delay_ms, error_rate); all stopped at module end."""
    stack, cache = [], {}

    def get(delay_ms=0, error_rate=0.0):
        key = (delay_ms, error_rate)
        if key not in cache:
            cm = spawn_mock(delay_ms, error_rate)
            cache[key] = cm.__enter__()
            stack.append(cm)
        return cache[key]

    yield get
    for cm in reversed(stack):
        cm.__exit__(None, None, None)


@pytest.fixture
def report_root():
    # Own temp dir instead of pytest's report_root: report_root needs %TEMP%\pytest-of-<user>, which can be
    # unusable on shared Windows machines and would turn an environment issue into test errors.
    d = Path(tempfile.mkdtemp(prefix="llm-slo-loadtest-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def load(url, total, concurrency, timeout_s=30.0, **kw):
    return asyncio.run(run_load(url, total, concurrency, timeout_s, **kw))


# ---- real TCP --------------------------------------------------------------------------------------


@pytest.mark.parametrize("concurrency", [1, 5, 10])
def test_exact_total_and_concurrency_bound_over_tcp(servers, concurrency):
    run = load(servers(delay_ms=10), total=100, concurrency=concurrency)
    assert len(run.results) == 100
    assert [r.index for r in run.results] == list(range(100))
    assert all(r.outcome == "success" and r.status_code == 200 for r in run.results)
    assert run.max_in_flight == concurrency


def test_delay_200ms_vs_0ms(servers):
    fast = summarize(load(servers(delay_ms=0), 30, 5).results)["latency_ms"]
    slow = summarize(load(servers(delay_ms=200), 30, 5).results)["latency_ms"]
    assert slow["min"] >= 200  # every request waited at least the configured delay
    assert slow["p50"] > fast["p50"]


def test_error_rate_1_all_http_500(servers):
    run = load(servers(error_rate=1.0), 20, 5)
    s = summarize(run.results)
    assert all(r.outcome == "http_error" and r.status_code == 500 for r in run.results)
    assert s["error_rate_pct"] == 100.0
    assert s["latency_ms"]["samples"] == 0 and s["latency_ms"]["p95"] is None


def test_timeout_is_classified(servers):
    run = load(servers(delay_ms=1000), total=4, concurrency=4, timeout_s=0.2)
    assert len(run.results) == 4
    assert all(r.outcome == "timeout" and r.status_code is None for r in run.results)


def test_connection_failure_is_classified():
    run = load(f"http://127.0.0.1:{free_port()}", total=3, concurrency=2, timeout_s=5)
    assert len(run.results) == 3
    assert all(r.outcome == "connect_error" for r in run.results)


# ---- in-process / validation -------------------------------------------------------------------------


def test_concurrency_never_exceeded_in_process():
    transport = httpx.ASGITransport(app=create_app(Settings(delay_ms=20)))
    run = load("http://127.0.0.1", total=23, concurrency=5, transport=transport)
    assert len(run.results) == 23 and run.max_in_flight == 5


def test_invalid_response_structure_is_failure():
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def wrong():
        return {"object": "something.else"}

    run = load("http://127.0.0.1", total=3, concurrency=1, transport=httpx.ASGITransport(app=app))
    assert all(r.outcome == "invalid_response" and r.status_code == 200 for r in run.results)


@pytest.mark.parametrize("url, total, conc", [
    ("http://example.com", 1, 1), ("https://127.0.0.1", 1, 1), ("http://10.0.0.1:8000", 1, 1),
    ("http://127.0.0.1", 0, 1), ("http://127.0.0.1", 1, 0),
])
def test_run_load_rejects_bad_config(url, total, conc):
    with pytest.raises(ValueError):
        load(url, total, conc)


# ---- CLI: verdicts, exit codes, reports -----------------------------------------------------------------


def read_report(root):
    (d,) = [p for p in root.iterdir() if p.is_dir()]
    return d, json.loads((d / "summary.json").read_text(encoding="utf-8"))


def run_cli_process(args, root):
    return subprocess.run([sys.executable, "-m", "perf.loadtest", *args, "--report-root", str(root)],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120)


def test_cli_pass_process_exit_matches_report(report_root):
    p = run_cli_process(["--spawn-mock", "--requests", "100", "--concurrency", "10",
                         "--slo-p95-ms", "5000", "--slo-error-rate-pct", "1", "--slo-min-requests", "100"], report_root)
    d, s = read_report(report_root)
    assert p.returncode == 0 == s["exit_code"] and s["verdict"] == "PASS" and s["complete"] is True
    assert s["metrics"]["total_requests"] == 100 and s["ttft_ms"] is None and s["tpot_ms"] is None
    assert len(s["source_hash"]["combined"]) == 64 and "perf/runner.py" in s["source_hash"]["files"]
    lines = (d / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100 and json.loads(lines[0])["outcome"] == "success"
    assert (d / "report.md").exists() and (d / "config.json").exists()
    for f in d.iterdir():  # request bodies are not recorded
        assert '"messages"' not in f.read_text(encoding="utf-8")


def test_cli_fail_process_exit_matches_report(report_root):
    p = run_cli_process(["--spawn-mock", "--mock-delay-ms", "50", "--requests", "20", "--concurrency", "5",
                         "--slo-p95-ms", "1", "--slo-min-requests", "20"], report_root)
    _, s = read_report(report_root)
    assert p.returncode == 1 == s["exit_code"] and s["verdict"] == "FAIL"


def test_cli_error_rate_1_is_fail(report_root):
    code = loadtest.main(["--spawn-mock", "--mock-error-rate", "1", "--requests", "20", "--concurrency", "5",
                          "--slo-min-requests", "20", "--report-root", str(report_root)])
    _, s = read_report(report_root)
    assert code == 1 == s["exit_code"]
    assert {c["name"]: c["status"] for c in s["slo"]["checks"]}["p95_ms"] == "UNVERIFIED"


def test_cli_unreachable_target_is_unverified(report_root):
    code = loadtest.main(["--url", f"http://127.0.0.1:{free_port()}", "--requests", "5",
                          "--report-root", str(report_root)])
    _, s = read_report(report_root)
    assert code == 2 == s["exit_code"] and s["verdict"] == "UNVERIFIED"
    assert s["metrics"]["latency_ms"]["p95"] is None and s["metrics"]["error_rate_pct"] is None


def test_cli_report_write_failure_is_never_pass(report_root, monkeypatch):
    def broken(path, text):
        raise OSError("injected write failure")

    monkeypatch.setattr(loadtest, "write_atomic", broken)
    code = loadtest.main(["--spawn-mock", "--requests", "100", "--concurrency", "10", "--slo-p95-ms", "5000",
                          "--report-root", str(report_root)])
    _, s = read_report(report_root)
    assert code == 2 == s["exit_code"] and s["verdict"] == "UNVERIFIED" and s["complete"] is False


@pytest.mark.parametrize("args", [
    ["--url", "http://example.com"],
    ["--url", "http://127.0.0.1:8000", "--mock-delay-ms", "5"],
    ["--spawn-mock", "--requests", "abc"],
    [],
])
def test_cli_bad_arguments_exit_2(args, report_root):
    with pytest.raises(SystemExit) as e:
        loadtest.main([*args, "--report-root", str(report_root)])
    assert e.value.code == 2


# ---- Real HTTP servers with test-only behaviour (429, trickling body, mixed 422) ------------------------
# The project's mock API is not changed; these apps run in a background thread on 127.0.0.1.

import contextlib  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import uvicorn  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

from perf.metrics import SLO, evaluate_slo  # noqa: E402

COMPLETION = {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
              "choices": [{"index": 0, "message": {"role": "assistant", "content": "[MOCK] test"},
                           "finish_reason": "stop"}]}


@contextlib.contextmanager
def serve(app):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=free_port(), log_level="warning",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{server.config.port}"
    finally:
        server.should_exit = True
        thread.join(15)


def test_app(handler):
    app = FastAPI()
    counter = {"n": 0}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat():
        counter["n"] += 1
        return await handler(counter["n"])

    return app


test_app.__test__ = False  # helper, not a pytest test


def trickle(total_s, interval=0.1):
    """Valid JSON delivered slowly: one whitespace byte every `interval`, so no single read is slow."""
    async def body():
        for _ in range(int(total_s / interval)):
            yield b" "
            await asyncio.sleep(interval)
        yield json.dumps(COMPLETION).encode()
    return StreamingResponse(body(), media_type="application/json")


async def ok_response():
    return JSONResponse(COMPLETION)


def test_whole_request_deadline_cuts_trickling_response():
    async def handler(n):
        return trickle(2.0)  # 2 s total, a byte every 0.1 s: per-read timeout of 0.2 s never fires

    with serve(test_app(handler)) as url:
        run = load(url, total=2, concurrency=2, timeout_s=0.2)
    assert [r.outcome for r in run.results] == ["timeout", "timeout"]
    assert all(r.error_type == "DeadlineExceeded" for r in run.results)
    assert all(r.latency_ms < 1500 for r in run.results)  # cut long before the 2 s completion (loose bound)
    s = summarize(run.results)
    assert s["successful_requests"] == 0
    assert evaluate_slo(s, SLO(p95_ms_max=10_000, error_rate_pct_max=1, min_latency_samples=1), 2)["verdict"] == "FAIL"


def test_trickling_response_within_deadline_succeeds():
    async def handler(n):
        return trickle(0.3)

    with serve(test_app(handler)) as url:
        run = load(url, total=2, concurrency=2, timeout_s=5)
    assert [r.outcome for r in run.results] == ["success", "success"]


def test_other_requests_continue_after_a_timeout():
    async def handler(n):
        return trickle(2.0) if n == 1 else await ok_response()

    with serve(test_app(handler)) as url:
        run = load(url, total=6, concurrency=2, timeout_s=0.5)
    outcomes = [r.outcome for r in run.results]
    assert len(outcomes) == 6 and outcomes.count("timeout") == 1 and outcomes.count("success") == 5
    assert run.max_in_flight <= 2


def test_real_http_429_is_fail(report_root):
    async def handler(n):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    with serve(test_app(handler)) as url:
        run = load(url, total=100, concurrency=10)
        code = loadtest.main(["--url", url, "--requests", "100", "--concurrency", "10",
                              "--slo-error-rate-pct", "1", "--report-root", str(report_root)])
    s = summarize(run.results)
    assert s["http_error_classes"]["rate_limited_429"] == 100
    _, report = read_report(report_root)
    assert code == 1 == report["exit_code"] and report["verdict"] == "FAIL"


def test_real_http_one_422_is_unverified_with_matching_exit_code(report_root):
    async def handler(n):
        return JSONResponse({"error": "bad"}, status_code=422) if n == 50 else await ok_response()

    with serve(test_app(handler)) as url:
        code = loadtest.main(["--url", url, "--requests", "100", "--concurrency", "5",
                              "--slo-error-rate-pct", "1", "--report-root", str(report_root)])
    _, report = read_report(report_root)
    checks = {c["name"]: c["status"] for c in report["slo"]["checks"]}
    assert checks["request_validity"] == "UNVERIFIED" and checks["error_rate_pct"] == "PASS"
    assert code == 2 == report["exit_code"] and report["verdict"] == "UNVERIFIED"
