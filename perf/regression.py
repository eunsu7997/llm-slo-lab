"""Repeat-run aggregation and baseline comparison (pure functions, no I/O).

Rules (also in docs/perf-harness-plan.md section 8):
- Statistics across runs use the MEDIAN of per-run values plus min/max; the mean is never used for judgement.
- Error rate is POOLED over all runs (sum failed / sum attempted), so one bad run cannot hide behind a median.
- Spread of a metric = (max - min) / median * 100 (%).
- Latency regression (P95) is FAIL only if the median increase exceeds the threshold AND every candidate run is
  slower than every baseline run (ranges do not overlap). A large increase with overlapping ranges, or a small
  change with too much run-to-run spread, is UNVERIFIED: the data cannot support either claim.
- Throughput regression (success RPS) mirrors the latency rule (decrease, every candidate run below every baseline run).
- Error-rate regression: pooled increase in percentage points above the threshold -> FAIL.
- Too few successful latency samples in any run makes only the latency check UNVERIFIED; error rate and
  throughput are still judged, so an error regression is never hidden by missing latency data.
- Conditions must match (workload, measurement method); otherwise the comparison is UNVERIFIED.
  The mock server settings (delay / error rate / seed) describe the system under test; a difference there is
  accepted only when the user explicitly declares it as the change being tested (allow_server_change).
- Precedence everywhere: FAIL > UNVERIFIED > PASS. No baseline -> UNVERIFIED, never PASS.
"""

import math
import statistics
from dataclasses import asdict, dataclass

from perf.metrics import OUTCOMES, SLO, RequestResult, evaluate_slo, summarize

METHOD = "closed-loop; nearest-rank percentiles; successful-request latency; perf.repeat v1"
WORKLOAD_KEYS = ("target_kind", "method", "requests", "concurrency", "timeout_s", "warmup_requests", "model",
                 "min_latency_samples")
RUN_METRICS = ("p50", "p95", "p99", "success_rps", "rps", "error_rate_pct")


@dataclass(frozen=True)
class Thresholds:
    p95_increase_pct: float = 20.0
    success_rps_decrease_pct: float = 20.0
    error_rate_increase_pp: float = 1.0
    max_spread_pct: float = 25.0
    min_runs: int = 3

    def __post_init__(self):
        problems = threshold_problems(self)
        if problems:
            raise ValueError("; ".join(problems))

    def as_dict(self):
        return asdict(self)


# (name, lower bound, upper bound) — NaN/Infinity never pass: a comparison with NaN is always False,
# which would silently turn every regression check into PASS.
_THRESHOLD_RANGES = (("p95_increase_pct", 0.0, 1e6), ("success_rps_decrease_pct", 0.0, 100.0),
                     ("error_rate_increase_pp", 0.0, 100.0), ("max_spread_pct", 0.0, 1e6))


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def threshold_problems(t) -> list[str]:
    problems = []
    for name, lo, hi in _THRESHOLD_RANGES:
        v = getattr(t, name, None)
        if not _is_number(v) or not math.isfinite(v):
            problems.append(f"{name} must be a finite number, got {v!r}")
        elif not lo <= v <= hi:
            problems.append(f"{name} must be in [{lo}, {hi}], got {v!r}")
    runs = getattr(t, "min_runs", None)
    if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= 10:
        problems.append(f"min_runs must be an integer in [1, 10], got {runs!r}")
    return problems


def verdict_of(statuses) -> str:
    statuses = list(statuses)
    if "FAIL" in statuses:
        return "FAIL"
    if "UNVERIFIED" in statuses or not statuses:
        return "UNVERIFIED"
    return "PASS"


def run_values(metrics: dict) -> dict:
    lat = metrics["latency_ms"]
    return {"p50": lat["p50"], "p95": lat["p95"], "p99": lat["p99"], "success_rps": metrics["success_rps"],
            "rps": metrics["rps"], "error_rate_pct": metrics["error_rate_pct"]}


def aggregate(run_metrics: list[dict]) -> dict:
    """Per-metric median/min/max/spread over runs. A metric missing in any run is not aggregated (None)."""
    out = {}
    for key in RUN_METRICS:
        values = [run_values(m)[key] for m in run_metrics]
        if not values or any(v is None for v in values):
            out[key] = {"values": values, "median": None, "min": None, "max": None, "spread_pct": None}
            continue
        med = statistics.median(values)
        spread = (max(values) - min(values)) / med * 100 if med else None
        out[key] = {"values": values, "median": med, "min": min(values), "max": max(values), "spread_pct": spread}
    attempted = sum(m["total_requests"] for m in run_metrics)
    failed = sum(m["failed_requests"] for m in run_metrics)
    out["pooled_error_rate_pct"] = failed * 100 / attempted if attempted else None
    out["min_latency_samples_in_any_run"] = min((m["latency_ms"]["samples"] for m in run_metrics), default=0)
    return out


def latency_sample_problems(exp: dict) -> list[str]:
    """Too few successful samples blocks only the LATENCY check (error rate / RPS stay judgeable)."""
    need = exp["conditions"]["min_latency_samples"]
    short = [r["run"] for r in exp.get("runs", []) if r["metrics"]["latency_ms"]["samples"] < need]
    return [f"runs {short} have fewer than {need} successful latency samples"] if short else []


def experiment_problems(exp: dict, thresholds: Thresholds) -> list[str]:
    """Reasons why an experiment cannot be used for any regression comparison."""
    problems = []
    if not exp.get("complete"):
        problems.append("experiment report incomplete")
    runs = exp.get("runs", [])
    if exp.get("aborted"):
        problems.append(f"experiment aborted: {exp['aborted']}")
    if len(runs) < thresholds.min_runs:
        problems.append(f"only {len(runs)} runs (< {thresholds.min_runs} required)")
    invalid = [r["run"] for r in runs if not r["slo"]["run_valid"]]
    if invalid:
        problems.append(f"invalid runs {invalid} (incomplete / unreachable)")
    return problems


def condition_differences(base: dict, cand: dict, allow_server_change: bool) -> tuple[list[str], list[str]]:
    """Returns (blocking differences, accepted differences)."""
    blocking, accepted = [], []
    for key in WORKLOAD_KEYS:
        b, c = base["conditions"].get(key), cand["conditions"].get(key)
        if b != c:
            blocking.append(f"{key}: baseline={b!r} candidate={c!r}")
    if not isinstance(base["server_config"], dict) or not isinstance(cand["server_config"], dict):
        # External targets: their delay/error settings are not known, so equality cannot be confirmed.
        blocking.append("server settings unknown (external target); cannot confirm identical conditions")
    elif base["server_config"] != cand["server_config"]:
        diff = f"server_config: baseline={base['server_config']} candidate={cand['server_config']}"
        if allow_server_change:
            accepted.append(diff + " (declared as the change under test)")
        else:
            blocking.append(diff)
    return blocking, accepted


def _check(name, threshold, status, reason, **values):
    return {"name": name, "threshold": threshold, "status": status, "reason": reason, **values}


def _unverified(reason, accepted=()):
    return {"verdict": "UNVERIFIED", "comparable": False, "reason": reason, "checks": [],
            "accepted_differences": list(accepted)}


def compare(base: dict | None, cand: dict, thresholds: Thresholds, allow_server_change: bool = False) -> dict:
    # Re-validated here too: Thresholds can be bypassed (object.__setattr__, duck-typed objects).
    problems = threshold_problems(thresholds)
    if problems:
        return _unverified("invalid thresholds: " + "; ".join(problems))
    if base is None:
        return _unverified("no baseline given")
    try:
        return _compare(base, cand, thresholds, allow_server_change)
    except (KeyError, TypeError, AttributeError, ValueError, ZeroDivisionError) as e:
        return _unverified(f"malformed experiment data: {type(e).__name__}: {e}")


def _compare(base: dict, cand: dict, thresholds: Thresholds, allow_server_change: bool) -> dict:
    problems = [f"baseline: {p}" for p in experiment_problems(base, thresholds)]
    problems += [f"candidate: {p}" for p in experiment_problems(cand, thresholds)]
    blocking, accepted = condition_differences(base, cand, allow_server_change)
    if problems or blocking:
        return {"verdict": "UNVERIFIED", "comparable": False,
                "reason": "; ".join(problems + [f"conditions differ: {d}" for d in blocking]),
                "checks": [], "accepted_differences": accepted}

    b, c = base["aggregate"], cand["aggregate"]
    checks = []

    # P95 latency (higher is worse)
    bp, cp = b["p95"], c["p95"]
    sample_problems = ([f"baseline: {p}" for p in latency_sample_problems(base)]
                       + [f"candidate: {p}" for p in latency_sample_problems(cand)])
    change = (cp["median"] - bp["median"]) / bp["median"] * 100 if bp["median"] and cp["median"] is not None else None
    noisy = max(bp["spread_pct"] or 0, cp["spread_pct"] or 0) > thresholds.max_spread_pct
    if sample_problems:
        status, reason = "UNVERIFIED", "; ".join(sample_problems)
    elif change is None:
        status, reason = "UNVERIFIED", "baseline P95 median is zero or missing"
    elif change > thresholds.p95_increase_pct:
        if cp["min"] > bp["max"]:
            status, reason = "FAIL", "median increase above threshold and every candidate run slower than every baseline run"
        else:
            status, reason = "UNVERIFIED", "median increase above threshold but run ranges overlap"
    elif noisy:
        status, reason = "UNVERIFIED", f"run-to-run spread above {thresholds.max_spread_pct}%: cannot rule out a regression"
    else:
        status, reason = "PASS", ""
    checks.append(_check("p95_increase_pct", f"<= {thresholds.p95_increase_pct}", status, reason, change=change,
                         baseline=bp, candidate=cp))

    # Success RPS (lower is worse)
    br, cr = b["success_rps"], c["success_rps"]
    decrease = (br["median"] - cr["median"]) / br["median"] * 100 if br["median"] else None
    noisy = max(br["spread_pct"] or 0, cr["spread_pct"] or 0) > thresholds.max_spread_pct
    if decrease is None:
        status, reason = "UNVERIFIED", "baseline success RPS is zero"
    elif decrease > thresholds.success_rps_decrease_pct:
        if cr["max"] < br["min"]:
            status, reason = "FAIL", "median decrease above threshold and every candidate run below every baseline run"
        else:
            status, reason = "UNVERIFIED", "median decrease above threshold but run ranges overlap"
    elif noisy:
        status, reason = "UNVERIFIED", f"run-to-run spread above {thresholds.max_spread_pct}%: cannot rule out a regression"
    else:
        status, reason = "PASS", ""
    checks.append(_check("success_rps_decrease_pct", f"<= {thresholds.success_rps_decrease_pct}", status, reason,
                         change=decrease, baseline=br, candidate=cr))

    # Pooled error rate (percentage points)
    be, ce = b["pooled_error_rate_pct"], c["pooled_error_rate_pct"]
    increase = ce - be
    status = "FAIL" if increase > thresholds.error_rate_increase_pp else "PASS"
    checks.append(_check("error_rate_increase_pp", f"<= {thresholds.error_rate_increase_pp}", status,
                         "pooled error rate increase above threshold" if status == "FAIL" else "",
                         change=increase, baseline=be, candidate=ce))

    return {"verdict": verdict_of(ch["status"] for ch in checks), "comparable": True, "reason": "",
            "checks": checks, "accepted_differences": accepted}


# ---- Baseline evidence verification -------------------------------------------------------------------
# A stored baseline is never trusted as-is: per-run metrics and the aggregate are RECOMPUTED from the
# request records (runs/run-<n>-requests.jsonl) and compared with what experiment.json claims.

NOT_VERIFIABLE = (
    "server_config (mock settings are recorded by the tool, not measured)",
    "warm-up results (not stored per request)",
    "timestamps, environment and source_hash (recorded metadata only)",
)
_INT_KEYS = {"requests": 1, "concurrency": 1, "warmup_requests": 0, "min_latency_samples": 1}
_STR_KEYS = ("target_kind", "method", "model")


def has_non_finite(obj) -> bool:
    if isinstance(obj, float):
        return not math.isfinite(obj)
    if isinstance(obj, dict):
        return any(has_non_finite(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_non_finite(v) for v in obj)
    return False


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return _is_number(a) and _is_number(b) and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def _records_to_results(records, requests: int, run_no: int):
    results, problems = [], []
    for i, rec in enumerate(records):
        try:
            idx, start, end = rec["index"], rec["start_ms"], rec["end_ms"]
            status, outcome = rec["status_code"], rec["outcome"]
            ok = (isinstance(idx, int) and _is_number(start) and _is_number(end) and math.isfinite(start)
                  and math.isfinite(end) and end >= start and outcome in OUTCOMES
                  and (status is None or (isinstance(status, int) and not isinstance(status, bool))))
        except (KeyError, TypeError):
            ok = False
        if not ok:
            problems.append(f"run {run_no}: malformed request record at line {i + 1}")
            return None, problems
        results.append(RequestResult(idx, start / 1000, end / 1000, status, outcome, rec.get("error_type")))
    if sorted(r.index for r in results) != list(range(requests)):
        problems.append(f"run {run_no}: request records do not cover indices 0..{requests - 1} exactly "
                        f"({len(results)} records)")
        return None, problems
    return results, problems


def rebuild_from_evidence(exp, records_by_run: dict) -> tuple[dict | None, list[str]]:
    """Validate a stored experiment against its request records.

    Returns (rebuilt experiment using only recomputed metrics, problems). Any problem means the baseline
    must not be used for a comparison.
    """
    if not isinstance(exp, dict):
        return None, [f"baseline JSON must be an object, got {type(exp).__name__}"]
    problems = []
    if has_non_finite(exp):
        problems.append("baseline contains NaN or Infinity values")
    if exp.get("kind") != "repeat-experiment":
        problems.append("not a perf.repeat experiment (kind != 'repeat-experiment')")
    if exp.get("complete") is not True:
        problems.append("baseline report not marked complete")
    if exp.get("aborted"):
        problems.append(f"baseline experiment was aborted: {exp.get('aborted')}")

    cond = exp.get("conditions")
    if not isinstance(cond, dict):
        return None, problems + ["conditions missing or not an object"]
    for key, low in _INT_KEYS.items():
        v = cond.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or v < low:
            problems.append(f"conditions.{key} invalid: {v!r}")
    t = cond.get("timeout_s")
    if not _is_number(t) or not math.isfinite(t) or t <= 0:
        problems.append(f"conditions.timeout_s invalid: {t!r}")
    for key in _STR_KEYS:
        if not isinstance(cond.get(key), str):
            problems.append(f"conditions.{key} invalid: {cond.get(key)!r}")
    server = exp.get("server_config")
    if not isinstance(server, (dict, str)):
        problems.append("server_config missing or invalid")

    runs = exp.get("runs")
    if not isinstance(runs, list) or not runs:
        return None, problems + ["runs missing, empty or not a list"]
    if exp.get("runs_requested") != len(runs):
        problems.append(f"run count mismatch: runs_requested={exp.get('runs_requested')!r}, stored runs={len(runs)}")
    if problems:
        return None, problems

    requests = cond["requests"]
    rebuilt_runs, seen = [], set()
    for entry in runs:
        run_no = entry.get("run") if isinstance(entry, dict) else None
        if not isinstance(run_no, int) or run_no in seen:
            problems.append(f"run entry invalid or duplicated: {run_no!r}")
            continue
        seen.add(run_no)
        records = records_by_run.get(run_no)
        if records is None:
            problems.append(f"run {run_no}: request records missing or unreadable")
            continue
        results, rec_problems = _records_to_results(records, requests, run_no)
        problems += rec_problems
        if results is None:
            continue
        metrics = summarize(results)
        stored = entry.get("metrics")
        if not isinstance(stored, dict):
            problems.append(f"run {run_no}: stored metrics missing")
            continue
        mismatched = []
        for key in ("total_requests", "successful_requests", "failed_requests"):
            if stored.get(key) != metrics[key]:
                mismatched.append(key)
        for key in ("error_rate_pct", "rps", "success_rps"):
            if not _close(stored.get(key), metrics[key]):
                mismatched.append(key)
        stored_lat = stored.get("latency_ms") if isinstance(stored.get("latency_ms"), dict) else {}
        for key in ("samples", "p50", "p95", "p99"):
            if not _close(stored_lat.get(key), metrics["latency_ms"][key]):
                mismatched.append(f"latency_ms.{key}")
        if mismatched:
            problems.append(f"run {run_no}: stored metrics differ from request records: {', '.join(mismatched)}")
        rebuilt_runs.append({"run": run_no, "metrics": metrics,
                             "slo": {"run_valid": evaluate_slo(metrics, SLO(), requests)["run_valid"]}})
    if problems:
        return None, problems

    agg = aggregate([r["metrics"] for r in rebuilt_runs])
    stored_agg = exp.get("aggregate")
    if not isinstance(stored_agg, dict):
        problems.append("stored aggregate missing")
    else:
        for key in ("p95", "success_rps", "error_rate_pct"):
            s = stored_agg.get(key) if isinstance(stored_agg.get(key), dict) else {}
            for stat in ("median", "min", "max"):
                if not _close(s.get(stat), agg[key][stat]):
                    problems.append(f"stored aggregate {key}.{stat} differs from recomputed value")
        if not _close(stored_agg.get("pooled_error_rate_pct"), agg["pooled_error_rate_pct"]):
            problems.append("stored aggregate pooled_error_rate_pct differs from recomputed value")
    if problems:
        return None, problems

    rebuilt = {"kind": "repeat-experiment", "complete": True, "aborted": None, "conditions": cond,
               "server_config": server, "runs": rebuilt_runs, "aggregate": agg}
    return rebuilt, []
