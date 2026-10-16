"""Repeat a load test N times under identical conditions and compare with a baseline (CPU mock).

Each run: warm-up requests (excluded from metrics) then the measured requests, against ONE mock server instance.
Per-run SLO verdicts and the regression verdict are reported separately; the exit code is their combination
(FAIL > UNVERIFIED > PASS). Without --baseline the regression verdict is UNVERIFIED, so the exit code is never 0:
such a run is meant to become a baseline for later comparisons.

Mock results = artificial delay + HTTP overhead; not LLM inference performance; no TTFT/TPOT.

Exit codes: 0 = PASS, 1 = FAIL, 2 = UNVERIFIED (also for invalid arguments).

Examples:
    python -m perf.repeat --spawn-mock --mock-delay-ms 100 --runs 5 --requests 200 --concurrency 10
    python -m perf.repeat --spawn-mock --mock-delay-ms 100 --runs 5 --requests 200 --concurrency 10 \
        --baseline reports/repeat-<timestamp>/experiment.json
"""

import argparse
import asyncio
import json
import math
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from perf.loadtest import NOTE, new_report_dir, source_hashes, write_atomic
from perf.metrics import DEFAULT_MIN_LATENCY_SAMPLES, SLO, display, evaluate_slo, summarize
from perf.mock_server import PROJECT_ROOT, spawn_mock
from perf.regression import NOT_VERIFIABLE, METHOD, Thresholds, aggregate, compare, rebuild_from_evidence, verdict_of
from perf.runner import MAX_CONCURRENCY, MAX_REQUESTS, require_localhost, run_load

EXIT = {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}
MAX_RUNS = 10
MAX_WARMUP = 1000


def parse_args(argv):
    p = argparse.ArgumentParser(prog="python -m perf.repeat", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="existing localhost mock server")
    target.add_argument("--spawn-mock", action="store_true", help="start app.main on a free 127.0.0.1 port")
    p.add_argument("--mock-delay-ms", type=int, default=0)
    p.add_argument("--mock-error-rate", type=float, default=0.0)
    p.add_argument("--mock-seed", type=int)
    p.add_argument("--runs", type=int, default=5, help=f"measured repetitions (1..{MAX_RUNS})")
    p.add_argument("--warmup-requests", type=int, default=20, help="per run, excluded from metrics")
    p.add_argument("--requests", type=int, default=200)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--model", default="mock-model")
    # per-run SLO (same meaning as perf.loadtest)
    p.add_argument("--slo-p95-ms", type=float, default=1000.0)
    p.add_argument("--slo-error-rate-pct", type=float, default=1.0)
    p.add_argument("--slo-min-requests", type=int, default=100)
    p.add_argument("--slo-min-latency-samples", type=int, default=DEFAULT_MIN_LATENCY_SAMPLES)
    # regression
    p.add_argument("--baseline", help="experiment.json (or its directory) of an earlier perf.repeat run")
    p.add_argument("--allow-server-change", action="store_true",
                   help="declare that a different mock server setting is the change under test")
    d = Thresholds()
    p.add_argument("--max-p95-increase-pct", type=float, default=d.p95_increase_pct)
    p.add_argument("--max-success-rps-decrease-pct", type=float, default=d.success_rps_decrease_pct)
    p.add_argument("--max-error-rate-increase-pp", type=float, default=d.error_rate_increase_pp)
    p.add_argument("--max-spread-pct", type=float, default=d.max_spread_pct)
    p.add_argument("--min-runs", type=int, default=d.min_runs)
    p.add_argument("--report-root", default=str(PROJECT_ROOT / "reports"))
    args = p.parse_args(argv)  # argparse exits 2 (= UNVERIFIED) on bad arguments
    if not 1 <= args.runs <= MAX_RUNS:
        p.error(f"--runs must be 1..{MAX_RUNS}")
    if not 0 <= args.warmup_requests <= MAX_WARMUP:
        p.error(f"--warmup-requests must be 0..{MAX_WARMUP}")
    if not 1 <= args.requests <= MAX_REQUESTS or not 1 <= args.concurrency <= MAX_CONCURRENCY:
        p.error("--requests / --concurrency out of range")
    if not args.spawn_mock and (args.mock_delay_ms or args.mock_error_rate or args.mock_seed is not None):
        p.error("--mock-* options require --spawn-mock")
    if args.url:
        try:
            require_localhost(args.url)
        except ValueError as e:
            p.error(str(e))
    # Every float must be finite: NaN compares False with everything and would turn checks into PASS.
    for name, value, lo, hi in (("--timeout-s", args.timeout_s, 1e-6, 3600.0),
                                ("--slo-p95-ms", args.slo_p95_ms, 0.0, 1e9),
                                ("--slo-error-rate-pct", args.slo_error_rate_pct, 0.0, 100.0),
                                ("--mock-error-rate", args.mock_error_rate, 0.0, 1.0)):
        if not math.isfinite(value) or not lo <= value <= hi:
            p.error(f"{name} must be a finite number in [{lo}, {hi}], got {value}")
    if args.slo_min_requests < 1 or args.slo_min_latency_samples < 1:
        p.error("--slo-min-requests / --slo-min-latency-samples must be >= 1")
    try:
        args.thresholds = Thresholds(args.max_p95_increase_pct, args.max_success_rps_decrease_pct,
                                     args.max_error_rate_increase_pp, args.max_spread_pct, args.min_runs)
    except ValueError as e:
        p.error(f"invalid regression threshold: {e}")
    return args


def _reject_constant(name):
    raise ValueError(f"non-finite number {name} in JSON")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def load_baseline(path: str):
    """Returns (verified experiment or None, problems, info). Never raises.

    The stored metrics/aggregate are not trusted: they are recomputed from the per-run request records and
    must match. Anything missing, malformed or inconsistent makes the baseline unusable (comparison UNVERIFIED).
    """
    f = Path(path)
    if f.is_dir():
        f = f / "experiment.json"
    info = {"path": str(f), "verified": False, "not_verifiable": list(NOT_VERIFIABLE)}
    try:
        exp = _read_json(f)
    except (OSError, ValueError) as e:
        return None, [f"baseline unreadable: {e}"], info
    records_by_run = {}
    runs = exp.get("runs") if isinstance(exp, dict) else None
    for entry in runs if isinstance(runs, list) else []:
        run_no = entry.get("run") if isinstance(entry, dict) else None
        if not isinstance(run_no, int):
            continue
        try:
            text = (f.parent / "runs" / f"run-{run_no}-requests.jsonl").read_text(encoding="utf-8")
            records_by_run[run_no] = [json.loads(line, parse_constant=_reject_constant)
                                      for line in text.splitlines() if line.strip()]
        except (OSError, ValueError):
            records_by_run[run_no] = None  # reported as missing/unreadable by rebuild_from_evidence
    try:
        rebuilt, problems = rebuild_from_evidence(exp, records_by_run)
    except (KeyError, TypeError, AttributeError, ValueError) as e:  # defensive: never a traceback
        rebuilt, problems = None, [f"baseline verification error: {type(e).__name__}: {e}"]
    if isinstance(exp, dict):
        info.update(started_at=exp.get("started_at"), server_config=exp.get("server_config"),
                    source_hash=(exp.get("source_hash") or {}).get("combined")
                    if isinstance(exp.get("source_hash"), dict) else None)
    if problems:
        return None, problems, info
    info["verified"] = True
    info["verified_items"] = ["structure and types", "finite numbers", "run count",
                              "per-run metrics recomputed from request records",
                              "aggregate recomputed from per-run metrics"]
    return rebuilt, [], info


def write_requests(path: Path, results):
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.to_record()) + "\n")


async def measure(url, args):
    async with httpx.AsyncClient(timeout=5) as c:  # reachability preflight, not measured
        await c.get(url.rstrip("/") + "/healthz")
    warm = None
    if args.warmup_requests:
        warm = await run_load(url, args.warmup_requests, min(args.concurrency, args.warmup_requests),
                              args.timeout_s, model=args.model)
    run = await run_load(url, args.requests, args.concurrency, args.timeout_s, model=args.model)
    return warm, run


def render_markdown(exp: dict) -> str:
    d = lambda v, n=3: "n/a" if v is None else display(v, n)  # noqa: E731
    c, agg, cmp_ = exp["conditions"], exp["aggregate"], exp["comparison"]
    lines = [
        "# Repeat experiment & regression check (CPU mock)", "", f"> {NOTE}", "",
        f"- **Verdict: {exp['verdict']}** (exit {exp['exit_code']}) = SLO {exp['slo_verdict']} + regression {exp['regression_verdict']}",
        f"- Started: {exp['started_at']}  Runs: {len(exp['runs'])}/{exp['runs_requested']}"
        + (f"  **ABORTED: {exp['aborted']}**" if exp["aborted"] else ""),
        f"- Conditions: {json.dumps(c)}",
        f"- Server config: {json.dumps(exp['server_config'])}",
        f"- Source hash: `{exp['source_hash']['combined'][:16]}`", "",
        "## Runs (warm-up excluded)", "",
        "| Run | OK/Total | Error % | P50 | P95 | P99 (ms) | Success RPS | Latency samples | Warm-up failed | SLO |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in exp["runs"]:
        m, lat = r["metrics"], r["metrics"]["latency_ms"]
        lines.append(f"| {r['run']} | {m['successful_requests']}/{m['total_requests']} | {d(m['error_rate_pct'], 4)} | "
                     f"{d(lat['p50'])} | {d(lat['p95'])} | {d(lat['p99'])} | {d(m['success_rps'])} | {lat['samples']} | "
                     f"{r['warmup']['failed']}/{r['warmup']['total']} | {r['slo']['verdict']} |")
    lines += ["", "## Across runs (median [min - max], spread %)", "", "| Metric | Median | Min | Max | Spread % |",
              "|---|---|---|---|---|"]
    for key in ("p50", "p95", "p99", "success_rps", "error_rate_pct"):
        a = agg.get(key) or {}
        lines.append(f"| {key} | {d(a.get('median'))} | {d(a.get('min'))} | {d(a.get('max'))} | {d(a.get('spread_pct'), 1)} |")
    lines += [f"| pooled error rate % | {d(agg.get('pooled_error_rate_pct'), 4)} | | | |", "",
              "## Regression vs baseline", ""]
    base = exp["baseline"]
    if base is None:
        lines.append("- Baseline: none")
    elif base.get("error"):
        lines.append(f"- Baseline: {base['path']} - **rejected: {base['error']}**")
    else:
        lines.append(f"- Baseline: {base['path']} (started {base.get('started_at')}); verified: "
                     + ", ".join(base.get("verified_items", [])))
        lines.append("- Not verifiable from stored evidence: " + "; ".join(base.get("not_verifiable", [])))
    for err in exp.get("evidence_errors", []):
        lines.append(f"- **Evidence error (verdict cannot be PASS):** {err}")
    lines.append(f"- Thresholds: {json.dumps(exp['thresholds'])}")
    for diff in cmp_["accepted_differences"]:
        lines.append(f"- Accepted difference: {diff}")
    if not cmp_["comparable"]:
        lines.append(f"- **Not comparable -> UNVERIFIED:** {cmp_['reason']}")
    else:
        lines += ["", "| Check | Threshold | Change | Baseline median | Candidate median | Status | Reason |",
                  "|---|---|---|---|---|---|---|"]
        for ch in cmp_["checks"]:
            bm = ch["baseline"]["median"] if isinstance(ch["baseline"], dict) else ch["baseline"]
            cm = ch["candidate"]["median"] if isinstance(ch["candidate"], dict) else ch["candidate"]
            lines.append(f"| {ch['name']} | {ch['threshold']} | {d(ch['change'])} | {d(bm)} | {d(cm)} | {ch['status']} | {ch['reason']} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    args = parse_args(argv)
    thresholds = args.thresholds  # validated (finite, in range) in parse_args
    slo =SLO(None, args.slo_p95_ms, None, args.slo_error_rate_pct, args.slo_min_requests, args.slo_min_latency_samples)
    try:
        report_dir = new_report_dir(Path(args.report_root), prefix="repeat")
    except OSError as e:
        print(f"UNVERIFIED: cannot create report directory: {e}")
        return EXIT["UNVERIFIED"]

    exp = {
        "kind": "repeat-experiment", "schema": 1, "complete": False, "note": NOTE,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "conditions": {"target_kind": "mock", "method": METHOD, "requests": args.requests,
                       "concurrency": args.concurrency, "timeout_s": args.timeout_s,
                       "warmup_requests": args.warmup_requests, "model": args.model,
                       "min_latency_samples": args.slo_min_latency_samples},
        "server_config": ({"delay_ms": args.mock_delay_ms, "error_rate": args.mock_error_rate, "seed": args.mock_seed}
                          if args.spawn_mock else f"external {args.url} (settings unknown)"),
        "slo_objectives": slo.as_dict(), "thresholds": thresholds.as_dict(),
        "runs_requested": args.runs, "runs": [], "aborted": None, "evidence_errors": [],
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                        "cpu_count": os.cpu_count(), "httpx": httpx.__version__},
        "source_hash": source_hashes(), "ttft_ms": None, "tpot_ms": None,
    }
    (report_dir / "runs").mkdir()

    def do_runs(url):
        for i in range(1, args.runs + 1):
            warm, run = asyncio.run(measure(url, args))
            # 1) judge and keep the run in memory first, 2) then save its request records.
            metrics = summarize(run.results)
            metrics["max_in_flight"] = run.max_in_flight
            wsum = summarize(warm.results) if warm else None
            exp["runs"].append({
                "run": i, "metrics": metrics, "slo": evaluate_slo(metrics, slo, args.requests),
                "warmup": {"total": wsum["total_requests"] if wsum else 0, "failed": wsum["failed_requests"] if wsum else 0,
                           "failures_by_outcome": wsum["failures_by_outcome"] if wsum else {}},
                "requests_saved": False,
            })
            try:
                write_requests(report_dir / "runs" / f"run-{i}-requests.jsonl", run.results)
                exp["runs"][-1]["requests_saved"] = True
            except OSError as e:  # missing evidence is UNVERIFIED, but never erases the run or its FAIL
                exp["evidence_errors"].append(f"run {i}: request records not saved: {e}")
            p95 = metrics["latency_ms"]["p95"]
            print(f"run {i}/{args.runs}: ok={metrics['successful_requests']}/{metrics['total_requests']} "
                  f"p95={display(p95)}ms success_rps={display(metrics['success_rps'])} "
                  f"error={display(metrics['error_rate_pct'], 4)}%", flush=True)

    try:
        if args.spawn_mock:
            with spawn_mock(args.mock_delay_ms, args.mock_error_rate, args.mock_seed) as url:
                do_runs(url)
        else:
            do_runs(args.url)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        exp["aborted"] = f"{type(e).__name__}: {e}"
    except KeyboardInterrupt:
        exp["aborted"] = "interrupted"

    runs = exp["runs"]
    exp["aggregate"] = aggregate([r["metrics"] for r in runs])
    slo_statuses = [r["slo"]["verdict"] for r in runs]
    if exp["aborted"] or len(runs) < args.runs:
        slo_statuses.append("UNVERIFIED")  # missing runs are never silently dropped
    exp["slo_verdict"] = verdict_of(slo_statuses)

    exp["complete"] = True  # for this experiment's own suitability check; reset below if saving fails
    if args.baseline:
        baseline, base_problems, base_info = load_baseline(args.baseline)
        if base_problems:
            base_info["error"] = "; ".join(base_problems)
            exp["comparison"] = {"verdict": "UNVERIFIED", "comparable": False,
                                 "reason": "baseline rejected: " + "; ".join(base_problems), "checks": [],
                                 "accepted_differences": []}
        else:
            exp["comparison"] = compare(baseline, exp, thresholds, args.allow_server_change)
        exp["baseline"] = base_info
    else:
        exp["comparison"] = compare(None, exp, thresholds, args.allow_server_change)
        exp["baseline"] = None
    exp["regression_verdict"] = exp["comparison"]["verdict"]
    statuses = [exp["slo_verdict"], exp["regression_verdict"]]
    if exp["evidence_errors"]:
        statuses.append("UNVERIFIED")  # this run's own evidence is incomplete: never PASS
    exp["verdict"] = verdict_of(statuses)
    exp["exit_code"] = EXIT[exp["verdict"]]

    try:
        write_atomic(report_dir / "report.md", render_markdown(exp))
        write_atomic(report_dir / "experiment.json", json.dumps(exp, indent=2))  # written last
    except OSError as e:
        if exp["verdict"] == "PASS":
            exp["verdict"], exp["exit_code"] = "UNVERIFIED", EXIT["UNVERIFIED"]
        exp["complete"], exp["report_error"] = False, str(e)
        try:
            (report_dir / "experiment.json").write_text(json.dumps(exp, indent=2), encoding="utf-8")
        except OSError:
            pass

    cmp_ = exp["comparison"]
    print(NOTE)
    for ch in cmp_["checks"]:
        print(f"  [{ch['status']}] {ch['name']} change={display(ch['change'])} threshold {ch['threshold']} {ch['reason']}".rstrip())
    if cmp_["reason"]:
        print(f"  regression: {cmp_['reason']}")
    print(f"SLO: {exp['slo_verdict']}  REGRESSION: {exp['regression_verdict']}")
    print(f"VERDICT: {exp['verdict']} (exit {exp['exit_code']})  report: {report_dir}")
    return exp["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
