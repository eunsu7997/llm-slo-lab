"""Pure metric and SLO functions (no I/O), unit-tested with fixed data.

Definitions (also in docs/perf-harness-plan.md section 7):
- latency: client-side HTTP round trip of SUCCESSFUL requests only, in ms
- percentile: nearest-rank, rank = ceil(p/100 * n) on ascending values (1-based); undefined (None) for n = 0
- error rate: failed requests / attempted requests * 100
- RPS: completed requests (any outcome) / measurement window; window = first request start -> last request end

All values returned by summarize() are RAW (full float precision). SLO judgement uses these raw values;
rounding is applied only when rendering (see display()).
"""

import math
from dataclasses import dataclass
from fractions import Fraction

SUCCESS = "success"
FAILURE_OUTCOMES = ("http_error", "invalid_response", "timeout", "connect_error", "transport_error")
OUTCOMES = (SUCCESS, *FAILURE_OUTCOMES)

# HTTP status classification of http_error outcomes (all of them count as failures in the error rate).
RATE_LIMITED = "rate_limited_429"      # server rejected/throttled the request: a genuine SLO failure
REQUEST_INVALID = "request_invalid_4xx"  # other 4xx: the request or configuration is suspect
SERVER_ERROR = "server_error_5xx"
OTHER_STATUS = "other_status"


def status_class(status: int) -> str:
    if status == 429:
        return RATE_LIMITED
    if 400 <= status < 500:
        return REQUEST_INVALID
    if 500 <= status < 600:
        return SERVER_ERROR
    return OTHER_STATUS


def display(value, digits: int = 3):
    """Rounding for human-readable output only. Never use the result for SLO comparisons."""
    return None if value is None else round(value, digits)


@dataclass(frozen=True)
class RequestResult:
    index: int
    start_s: float  # offset from run start
    end_s: float
    status_code: int | None
    outcome: str
    error_type: str | None = None

    @property
    def latency_ms(self) -> float:
        return (self.end_s - self.start_s) * 1000

    def to_record(self) -> dict:
        # Deliberately excludes request/response bodies.
        return {
            "index": self.index,
            "start_ms": self.start_s * 1000,
            "end_ms": self.end_s * 1000,
            "latency_ms": self.latency_ms,
            "status_code": self.status_code,
            "outcome": self.outcome,
            "error_type": self.error_type,
        }


def percentile_nearest_rank(values, p):
    """Nearest-rank percentile. Returns None for an empty sample (never 0)."""
    if not 0 < p <= 100:
        raise ValueError("p must be in (0, 100]")
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(Fraction(str(p)) * len(ordered) / 100)  # exact arithmetic, no float rounding
    return ordered[max(rank, 1) - 1]


def summarize(results: list[RequestResult]) -> dict:
    """Raw (unrounded) metrics."""
    total = len(results)
    ok = [r for r in results if r.outcome == SUCCESS]
    failed = total - len(ok)
    latencies = [r.latency_ms for r in ok]
    window = (max(r.end_s for r in results) - min(r.start_s for r in results)) if results else None
    by_outcome = {name: sum(r.outcome == name for r in results) for name in FAILURE_OUTCOMES}
    http_classes = {name: 0 for name in (RATE_LIMITED, REQUEST_INVALID, SERVER_ERROR, OTHER_STATUS)}
    status_counts = {}
    for r in results:
        if r.outcome == "http_error" and r.status_code is not None:
            http_classes[status_class(r.status_code)] += 1
            status_counts[str(r.status_code)] = status_counts.get(str(r.status_code), 0) + 1

    return {
        "total_requests": total,
        "successful_requests": len(ok),
        "failed_requests": failed,
        "error_rate_pct": failed * 100 / total if total else None,
        "failures_by_outcome": by_outcome,
        "http_error_classes": http_classes,
        "http_error_status_counts": status_counts,
        "latency_ms": {
            "samples": len(latencies),
            "p50": percentile_nearest_rank(latencies, 50),
            "p95": percentile_nearest_rank(latencies, 95),
            "p99": percentile_nearest_rank(latencies, 99),
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": sum(latencies) / len(latencies) if latencies else None,
        },
        "duration_s": window,
        "rps": total / window if window else None,
        "success_rps": len(ok) / window if window else None,
    }


DEFAULT_MIN_LATENCY_SAMPLES = 20  # smallest n for which nearest-rank P95 is not simply the maximum


@dataclass(frozen=True)
class SLO:
    p50_ms_max: float | None = None
    p95_ms_max: float | None = None
    p99_ms_max: float | None = None
    error_rate_pct_max: float | None = None
    min_requests: int | None = None
    min_latency_samples: int = DEFAULT_MIN_LATENCY_SAMPLES

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _verdict(statuses) -> str:
    if "FAIL" in statuses:
        return "FAIL"
    if "UNVERIFIED" in statuses or not statuses:
        return "UNVERIFIED"
    return "PASS"


def evaluate_slo(summary: dict, slo: SLO, planned_requests: int) -> dict:
    """FAIL > UNVERIFIED > PASS, using RAW metric values.

    A run without usable data (nothing completed, incomplete, target unreachable) is UNVERIFIED and its
    objectives are not evaluated. Suspect requests (4xx other than 429) add an UNVERIFIED 'request_validity'
    check but do not stop the objectives from being evaluated, so an independent FAIL stays visible.
    """
    total = summary["total_requests"]
    invalid = None
    if total == 0:
        invalid = "no requests completed"
    elif total < planned_requests:
        invalid = f"run incomplete: {total} of {planned_requests} requests completed"
    elif summary["failures_by_outcome"]["connect_error"] == total:
        invalid = "target unreachable: every request failed to connect"
    if invalid:
        return {"verdict": "UNVERIFIED", "run_valid": False, "reason": invalid, "checks": []}

    checks = []

    def add(name, target, actual, status, reason=""):
        checks.append({"name": name, "target": target, "actual": actual, "status": status, "reason": reason})

    suspect = summary["http_error_classes"][REQUEST_INVALID]
    if suspect:
        codes = {k: v for k, v in summary["http_error_status_counts"].items()
                 if status_class(int(k)) == REQUEST_INVALID}
        add("request_validity", "no 4xx other than 429", suspect, "UNVERIFIED",
            f"target rejected requests as invalid {codes}: request/config problem; results may not reflect the SLO")

    if slo.min_requests is not None:
        enough = total >= slo.min_requests
        add("min_requests", f">= {slo.min_requests}", total, "PASS" if enough else "UNVERIFIED",
            "" if enough else "insufficient sample size")

    lat = summary["latency_ms"]
    samples = lat["samples"]
    for key, limit in (("p50", slo.p50_ms_max), ("p95", slo.p95_ms_max), ("p99", slo.p99_ms_max)):
        if limit is None:
            continue
        value = lat[key]
        if samples == 0:
            add(f"{key}_ms", f"<= {limit}", None, "UNVERIFIED",
                "no successful requests; latency percentile not computable")
        elif samples < slo.min_latency_samples:
            add(f"{key}_ms", f"<= {limit}", value, "UNVERIFIED",
                f"insufficient latency samples: {samples} successful < {slo.min_latency_samples} required")
        else:
            add(f"{key}_ms", f"<= {limit}", value, "PASS" if value <= limit else "FAIL")

    if slo.error_rate_pct_max is not None:
        value = summary["error_rate_pct"]
        add("error_rate_pct", f"<= {slo.error_rate_pct_max}", value,
            "PASS" if value <= slo.error_rate_pct_max else "FAIL")

    objectives = [c for c in checks if c["name"] != "request_validity"]
    verdict = _verdict([c["status"] for c in checks]) if objectives else "UNVERIFIED"
    reason = "" if objectives else "no SLO objectives configured"
    return {"verdict": verdict, "run_valid": True, "reason": reason, "checks": checks}
