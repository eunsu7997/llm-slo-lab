"""LLM-SLO Lab load test CLI (CPU mock target).

Measures client-side HTTP latency / RPS / error rate against a localhost OpenAI-compatible mock and judges SLOs.
Results describe the MOCK (artificial delay + HTTP overhead), not LLM inference. No TTFT/TPOT is measured.

Exit codes: 0 = PASS, 1 = FAIL, 2 = UNVERIFIED (also for invalid arguments).

Examples:
    python -m perf.loadtest --spawn-mock --mock-delay-ms 200 --requests 100 --concurrency 10
    python -m perf.loadtest --url http://127.0.0.1:8000 --requests 200 --concurrency 20 --slo-p95-ms 500
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from perf.metrics import DEFAULT_MIN_LATENCY_SAMPLES, SLO, display, evaluate_slo, summarize
from perf.mock_server import PROJECT_ROOT, spawn_mock
from perf.runner import MAX_CONCURRENCY, MAX_REQUESTS, require_localhost, run_load

EXIT = {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}
NOTE = "Mock target: latency = artificial delay + HTTP overhead. Not LLM inference performance; no TTFT/TPOT."


def source_hashes() -> dict:
    files = [PROJECT_ROOT / "app" / "main.py", *sorted((PROJECT_ROOT / "perf").glob("*.py"))]
    hashes = {str(f.relative_to(PROJECT_ROOT)).replace("\\", "/"): hashlib.sha256(f.read_bytes()).hexdigest()
              for f in files}
    combined = hashlib.sha256("".join(f"{k}={v}\n" for k, v in sorted(hashes.items())).encode()).hexdigest()
    return {"files": hashes, "combined": combined}


def parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m perf.loadtest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="existing localhost mock server, e.g. http://127.0.0.1:8000")
    target.add_argument("--spawn-mock", action="store_true", help="start app.main on a free 127.0.0.1 port")
    p.add_argument("--mock-delay-ms", type=int, default=0, help="with --spawn-mock")
    p.add_argument("--mock-error-rate", type=float, default=0.0, help="with --spawn-mock")
    p.add_argument("--mock-seed", type=int, help="with --spawn-mock")
    p.add_argument("--requests", type=int, default=100, help=f"total requests (1..{MAX_REQUESTS})")
    p.add_argument("--concurrency", type=int, default=10, help=f"max in-flight requests (1..{MAX_CONCURRENCY})")
    p.add_argument("--timeout-s", type=float, default=30.0, help="per-request timeout")
    p.add_argument("--model", default="mock-model")
    p.add_argument("--slo-p50-ms", type=float)
    p.add_argument("--slo-p95-ms", type=float, default=1000.0)
    p.add_argument("--slo-p99-ms", type=float)
    p.add_argument("--slo-error-rate-pct", type=float, default=1.0)
    p.add_argument("--slo-min-requests", type=int, default=100)
    p.add_argument("--slo-min-latency-samples", type=int, default=DEFAULT_MIN_LATENCY_SAMPLES,
                   help="successful samples required before latency objectives are judged")
    p.add_argument("--report-root", default=str(PROJECT_ROOT / "reports"))
    args = p.parse_args(argv)  # argparse exits 2 (= UNVERIFIED) on bad arguments
    if not args.spawn_mock and (args.mock_delay_ms or args.mock_error_rate or args.mock_seed is not None):
        p.error("--mock-* options require --spawn-mock")
    if args.url:
        try:
            require_localhost(args.url)
        except ValueError as e:
            p.error(str(e))
    return args


def new_report_dir(root: Path, prefix: str = "loadtest") -> Path:
    """Always a new directory; existing reports are never overwritten."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path, n = root / f"{prefix}-{stamp}", 1
    while path.exists():
        path, n = root / f"{prefix}-{stamp}-{n}", n + 1
    path.mkdir(parents=True)
    return path


def render_markdown(summary: dict) -> str:
    m, s, c = summary["metrics"], summary["slo"], summary["config"]
    lat = m["latency_ms"]
    def fmt(v):  # display rounding only; summary.json keeps raw values
        if v is None:
            return "n/a (not computable)"
        return display(v) if isinstance(v, float) else v

    lines = [
        "# Load test report (CPU mock)", "",
        f"> {NOTE}", "",
        f"- **Verdict: {summary['verdict']}** (exit {summary['exit_code']})",
        f"- Started: {summary['started_at']}",
        f"- Target: {c['target_url']} ({c['target_kind']}; server config: {json.dumps(c['server_config'])})",
        f"- Requests: {c['requests']}, concurrency: {c['concurrency']}, timeout: {c['timeout_s']}s, retries: 0",
        f"- Source hash: `{summary['source_hash']['combined'][:16]}`", "",
        "## Metrics", "",
        "| Metric | Value |", "|---|---|",
        f"| Total / successful / failed | {m['total_requests']} / {m['successful_requests']} / {m['failed_requests']} |",
        f"| Error rate (%) | {fmt(m['error_rate_pct'])} |",
        f"| Failures by outcome | {json.dumps(m['failures_by_outcome'])} |",
        f"| HTTP error classes | {json.dumps(m['http_error_classes'])} |",
        f"| Latency p50 / p95 / p99 (ms, successful only, nearest-rank; {lat['samples']} samples) | {fmt(lat['p50'])} / {fmt(lat['p95'])} / {fmt(lat['p99'])} |",
        f"| Latency min / mean / max (ms) | {fmt(lat['min'])} / {fmt(lat['mean'])} / {fmt(lat['max'])} |",
        f"| Duration (s) | {fmt(m['duration_s'])} |",
        f"| RPS (completed) / success RPS | {fmt(m['rps'])} / {fmt(m['success_rps'])} |",
        f"| Max in-flight observed | {m['max_in_flight']} |", "",
        "## SLO", "",
    ]
    if not s["run_valid"]:
        lines.append(f"Run not valid for SLO judgement: {s['reason']}")
    elif not s["checks"]:
        lines.append(s["reason"])
    else:
        lines += ["| Objective | Target | Actual | Status | Note |", "|---|---|---|---|---|"]
        lines += [f"| {k['name']} | {k['target']} | {fmt(k['actual'])} | {k['status']} | {k['reason']} |"
                  for k in s["checks"]]
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None) -> int:
    args = parse_args(argv)
    slo = SLO(args.slo_p50_ms, args.slo_p95_ms, args.slo_p99_ms, args.slo_error_rate_pct, args.slo_min_requests,
              args.slo_min_latency_samples)
    started = datetime.now(timezone.utc).isoformat()
    server_config = ({"delay_ms": args.mock_delay_ms, "error_rate": args.mock_error_rate, "seed": args.mock_seed}
                     if args.spawn_mock else "external (not recorded by load generator)")
    config = {
        "target_kind": "mock", "target_url": args.url, "server_config": server_config,
        "requests": args.requests, "concurrency": args.concurrency, "timeout_s": args.timeout_s,
        "model": args.model, "retries": 0, "slo": slo.as_dict(),
    }
    environment = {"python": sys.version.split()[0], "platform": platform.platform(),
                   "cpu_count": os.cpu_count(), "httpx": httpx.__version__}

    run, abort = None, None
    try:
        report_dir = new_report_dir(Path(args.report_root))
    except OSError as e:
        print(f"UNVERIFIED: cannot create report directory: {e}")
        return EXIT["UNVERIFIED"]

    async def measure(url):
        config["target_url"] = url
        async with httpx.AsyncClient(timeout=5) as c:  # reachability preflight, not part of the measurement
            await c.get(url.rstrip("/") + "/healthz")
        return await run_load(url, args.requests, args.concurrency, args.timeout_s, model=args.model)

    try:
        if args.spawn_mock:
            with spawn_mock(args.mock_delay_ms, args.mock_error_rate, args.mock_seed) as url:
                run = asyncio.run(measure(url))
        else:
            run = asyncio.run(measure(args.url))
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        abort = f"{type(e).__name__}: {e}"
    except KeyboardInterrupt:
        abort = "interrupted"

    if run is not None:
        metrics = summarize(run.results)
        metrics["max_in_flight"] = run.max_in_flight
        judgement = evaluate_slo(metrics, slo, args.requests)
    else:
        metrics = summarize([])
        metrics["max_in_flight"] = 0
        judgement = {"verdict": "UNVERIFIED", "run_valid": False, "reason": f"measurement not performed: {abort}",
                     "checks": []}

    summary = {
        "verdict": judgement["verdict"], "exit_code": EXIT[judgement["verdict"]], "complete": True,
        "note": NOTE, "started_at": started, "config": config, "environment": environment,
        "source_hash": source_hashes(), "metrics": metrics, "slo": judgement,
        "ttft_ms": None, "tpot_ms": None,
    }
    try:
        (report_dir / "config.json").write_text(json.dumps(
            {"config": config, "environment": environment, "source_hash": summary["source_hash"]}, indent=2),
            encoding="utf-8")
        with open(report_dir / "requests.jsonl", "w", encoding="utf-8") as f:
            for r in (run.results if run else []):
                f.write(json.dumps(r.to_record()) + "\n")
        write_atomic(report_dir / "report.md", render_markdown(summary))
        write_atomic(report_dir / "summary.json", json.dumps(summary, indent=2))  # written last
    except OSError as e:
        # An incomplete report is never PASS evidence (docs/quality-gates.md rule 7).
        if summary["verdict"] == "PASS":
            summary["verdict"], summary["exit_code"] = "UNVERIFIED", EXIT["UNVERIFIED"]
        summary["complete"] = False
        summary["report_error"] = str(e)
        try:
            (report_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        except OSError:
            pass

    lat = metrics["latency_ms"]
    print(NOTE)
    d = display
    print(f"requests={metrics['total_requests']} ok={metrics['successful_requests']} "
          f"failed={metrics['failed_requests']} error_rate={d(metrics['error_rate_pct'], 4)}% "
          f"p50={d(lat['p50'])}ms p95={d(lat['p95'])}ms p99={d(lat['p99'])}ms rps={d(metrics['rps'])} "
          f"max_in_flight={metrics['max_in_flight']}")
    for c in judgement["checks"]:
        actual = d(c["actual"], 6) if isinstance(c["actual"], float) else c["actual"]
        print(f"  [{c['status']}] {c['name']} target {c['target']} actual {actual} {c['reason']}".rstrip())
    if judgement["reason"]:
        print(f"  {judgement['reason']}")
    print(f"VERDICT: {summary['verdict']} (exit {summary['exit_code']})  report: {report_dir}")
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
