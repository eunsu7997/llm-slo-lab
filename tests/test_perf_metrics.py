"""Metric and SLO logic on fixed, hand-computable data (no network, no timing)."""

import math

import pytest

from perf.metrics import (
    SLO,
    RequestResult,
    display,
    evaluate_slo,
    percentile_nearest_rank,
    status_class,
    summarize,
)


@pytest.mark.parametrize("p, expected", [(5, 15), (30, 20), (40, 20), (50, 35), (100, 50)])
def test_nearest_rank_textbook_example(p, expected):
    # rank = ceil(p/100 * 5): p5->1, p30->2, p40->2, p50->3, p100->5
    assert percentile_nearest_rank([15, 20, 35, 40, 50], p) == expected


def test_nearest_rank_1_to_20():
    data = list(range(1, 21))
    assert percentile_nearest_rank(data, 50) == 10  # ceil(10.0)
    assert percentile_nearest_rank(data, 95) == 19  # ceil(19.0), no float drift to 20
    assert percentile_nearest_rank(data, 99) == 20  # ceil(19.8)


def test_nearest_rank_1_to_100_exact_ranks():
    data = list(range(100, 0, -1))  # unsorted input
    assert [percentile_nearest_rank(data, p) for p in (50, 95, 99, 100)] == [50, 95, 99, 100]


def test_nearest_rank_edge_cases():
    assert percentile_nearest_rank([7.5], 99) == 7.5
    assert percentile_nearest_rank([], 95) is None  # not computable, never 0
    for bad in (0, -1, 101):
        with pytest.raises(ValueError):
            percentile_nearest_rank([1, 2], bad)


def ok(i, start, ms):
    return RequestResult(i, start, start + ms / 1000, 200, "success")


def bad(i, start, ms, outcome, status=None):
    return RequestResult(i, start, start + ms / 1000, status, outcome, None)


FIXED = [
    ok(0, 0.0, 10), ok(1, 0.0, 20), ok(2, 0.1, 30), ok(3, 0.2, 40),
    bad(4, 0.0, 5000, "timeout"),          # slow failure must not affect latency stats
    bad(5, 0.3, 1, "http_error", 500),
]


def test_summarize_counts_and_error_rate():
    s = summarize(FIXED)
    assert (s["total_requests"], s["successful_requests"], s["failed_requests"]) == (6, 4, 2)
    assert s["error_rate_pct"] == pytest.approx(2 / 6 * 100, abs=1e-3)
    assert s["failures_by_outcome"] == {"http_error": 1, "invalid_response": 0, "timeout": 1,
                                        "connect_error": 0, "transport_error": 0}


def test_summarize_latency_uses_successful_requests_only():
    lat = summarize(FIXED)["latency_ms"]
    assert lat["samples"] == 4
    # Raw float values (no rounding in summarize); offsets like 0.2 + 0.04 carry binary float error.
    assert (lat["p50"], lat["p95"], lat["p99"]) == pytest.approx((20.0, 40.0, 40.0), abs=1e-9)
    assert (lat["min"], lat["max"], lat["mean"]) == pytest.approx((10.0, 40.0, 25.0), abs=1e-9)


def test_summarize_window_and_rps():
    s = summarize(FIXED)
    assert s["duration_s"] == pytest.approx(5.0)  # first start 0.0 -> last end 5.0 (the timeout)
    assert s["rps"] == pytest.approx(6 / 5.0, abs=1e-3)
    assert s["success_rps"] == pytest.approx(4 / 5.0, abs=1e-3)


def test_summarize_empty_is_not_zero():
    s = summarize([])
    assert s["total_requests"] == 0
    assert s["error_rate_pct"] is None and s["rps"] is None and s["latency_ms"]["p95"] is None


def summary_of(results):
    return summarize(results)


ALL_OK = [ok(i, 0.0, 10 + i) for i in range(100)]  # latencies 10..109 ms -> p95 = 104


def test_slo_pass():
    r = evaluate_slo(summary_of(ALL_OK), SLO(p95_ms_max=1000, error_rate_pct_max=1, min_requests=100), 100)
    assert r["verdict"] == "PASS" and r["run_valid"]
    assert {c["name"]: c["status"] for c in r["checks"]} == {"min_requests": "PASS", "p95_ms": "PASS",
                                                            "error_rate_pct": "PASS"}


def test_slo_boundary_is_inclusive():
    p95 = summary_of(ALL_OK)["latency_ms"]["p95"]  # raw value, ~104 ms
    assert evaluate_slo(summary_of(ALL_OK), SLO(p95_ms_max=p95), 100)["verdict"] == "PASS"
    just_below = math.nextafter(p95, 0)  # the next representable float below the actual value
    assert evaluate_slo(summary_of(ALL_OK), SLO(p95_ms_max=just_below), 100)["verdict"] == "FAIL"


def test_slo_fail_on_error_rate():
    results = ALL_OK[:98] + [bad(98, 0, 1, "http_error", 500), bad(99, 0, 1, "timeout")]  # 2 %
    r = evaluate_slo(summary_of(results), SLO(p95_ms_max=1000, error_rate_pct_max=1), 100)
    assert r["verdict"] == "FAIL"
    assert [c["status"] for c in r["checks"] if c["name"] == "error_rate_pct"] == ["FAIL"]


def test_slo_all_failed_is_fail_and_latency_unverified():
    results = [bad(i, 0, 1, "http_error", 500) for i in range(10)]
    r = evaluate_slo(summary_of(results), SLO(p95_ms_max=1000, error_rate_pct_max=1), 10)
    statuses = {c["name"]: c["status"] for c in r["checks"]}
    assert statuses == {"p95_ms": "UNVERIFIED", "error_rate_pct": "FAIL"}
    assert r["verdict"] == "FAIL"  # FAIL > UNVERIFIED


def test_slo_latency_only_without_successes_is_unverified():
    results = [bad(i, 0, 1, "timeout") for i in range(10)]
    r = evaluate_slo(summary_of(results), SLO(p95_ms_max=1000), 10)
    assert r["verdict"] == "UNVERIFIED"
    assert r["checks"][0]["actual"] is None


def test_slo_insufficient_sample_is_unverified():
    r = evaluate_slo(summary_of(ALL_OK[:50]), SLO(p95_ms_max=1000, min_requests=100), 50)
    assert r["verdict"] == "UNVERIFIED"


@pytest.mark.parametrize(
    "results, planned, reason",
    [
        ([bad(i, 0, 1, "connect_error") for i in range(5)], 5, "unreachable"),
        (ALL_OK[:5], 10, "incomplete"),
        ([], 10, "no requests"),
    ],
    ids=["unreachable", "incomplete", "empty"],
)
def test_invalid_runs_are_unverified(results, planned, reason):
    r = evaluate_slo(summary_of(results), SLO(p95_ms_max=1000, error_rate_pct_max=1), planned)
    assert r["verdict"] == "UNVERIFIED" and not r["run_valid"]
    assert reason in r["reason"]


def test_no_objectives_is_unverified():
    assert evaluate_slo(summary_of(ALL_OK), SLO(), 100)["verdict"] == "UNVERIFIED"


# ---- Regression: judgement uses raw values, never display-rounded ones ---------------------------------


def uniform(n, latency_s):
    return [RequestResult(i, 0.0, latency_s, 200, "success") for i in range(n)]


def test_p95_just_above_limit_fails_even_if_rounding_would_hide_it():
    s = summarize(uniform(100, 1.0000004))  # 1000.0004 ms
    assert display(s["latency_ms"]["p95"]) == 1000.0  # what a 3-decimal report shows
    assert s["latency_ms"]["p95"] > 1000
    r = evaluate_slo(s, SLO(p95_ms_max=1000), 100)
    assert r["verdict"] == "FAIL"


def test_p95_exactly_at_limit_passes():
    s = summarize(uniform(100, 1.0))  # exactly 1000.0 ms
    assert s["latency_ms"]["p95"] == 1000.0
    assert evaluate_slo(s, SLO(p95_ms_max=1000), 100)["verdict"] == "PASS"


def test_tiny_error_rate_above_limit_fails():
    results = uniform(29_999, 0.01) + [RequestResult(29_999, 0.0, 0.01, 500, "http_error")]
    s = summarize(results)
    assert s["error_rate_pct"] == pytest.approx(100 / 30_000)  # 0.003333...%
    assert display(s["error_rate_pct"], 4) == 0.0033  # rounded value would be <= 0.00331
    r = evaluate_slo(s, SLO(error_rate_pct_max=0.00331), 30_000)
    assert r["verdict"] == "FAIL"


def test_error_rate_exactly_at_limit_passes():
    results = uniform(99, 0.01) + [RequestResult(99, 0.0, 0.01, 500, "http_error")]
    s = summarize(results)
    assert s["error_rate_pct"] == 1.0
    assert evaluate_slo(s, SLO(error_rate_pct_max=1.0), 100)["verdict"] == "PASS"


# ---- Regression: HTTP 429 and other 4xx ------------------------------------------------------------------


def statuses(codes):
    return [RequestResult(i, 0.0, 0.01, c, "success" if c == 200 else "http_error") for i, c in enumerate(codes)]


@pytest.mark.parametrize("code, expected", [(429, "rate_limited_429"), (400, "request_invalid_4xx"),
                                            (422, "request_invalid_4xx"), (503, "server_error_5xx"),
                                            (302, "other_status")])
def test_status_class(code, expected):
    assert status_class(code) == expected


def test_all_429_is_fail_not_unverified():
    s = summarize(statuses([429] * 100))
    assert s["http_error_classes"]["rate_limited_429"] == 100 and s["error_rate_pct"] == 100.0
    r = evaluate_slo(s, SLO(p95_ms_max=1000, error_rate_pct_max=1, min_requests=100), 100)
    assert r["verdict"] == "FAIL" and r["run_valid"]
    assert "request_validity" not in {c["name"] for c in r["checks"]}  # 429 is a server-side rejection


def test_server_errors_plus_one_422_is_still_fail():
    r = evaluate_slo(summarize(statuses([500] * 99 + [422])), SLO(error_rate_pct_max=1, min_requests=100), 100)
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks["error_rate_pct"] == "FAIL" and checks["request_validity"] == "UNVERIFIED"
    assert r["verdict"] == "FAIL"  # the suspect 422 must not hide the confirmed failure


def test_one_422_with_all_objectives_met_is_unverified():
    s = summarize(statuses([200] * 99 + [422]))
    r = evaluate_slo(s, SLO(p95_ms_max=1000, error_rate_pct_max=1, min_requests=100), 100)
    checks = {c["name"]: c["status"] for c in r["checks"]}
    assert checks == {"request_validity": "UNVERIFIED", "min_requests": "PASS", "p95_ms": "PASS",
                      "error_rate_pct": "PASS"}
    assert r["verdict"] == "UNVERIFIED"


def test_all_200_is_pass():
    r = evaluate_slo(summarize(statuses([200] * 100)), SLO(p95_ms_max=1000, error_rate_pct_max=1, min_requests=100), 100)
    assert r["verdict"] == "PASS"


# ---- Regression: latency objectives need enough successful samples -------------------------------------


def one_success_rest_timeouts():
    return [RequestResult(0, 0.0, 0.01, 200, "success")] + [
        RequestResult(i, 0.0, 30.0, None, "timeout") for i in range(1, 100)]


def test_latency_with_too_few_successes_is_unverified():
    s = summarize(one_success_rest_timeouts())
    r = evaluate_slo(s, SLO(p99_ms_max=1e9, error_rate_pct_max=100, min_requests=100), 100)
    checks = {c["name"]: c for c in r["checks"]}
    assert checks["p99_ms"]["status"] == "UNVERIFIED" and "insufficient latency samples" in checks["p99_ms"]["reason"]
    assert checks["min_requests"]["status"] == "PASS"  # total requests are enough; latency samples are not
    assert r["verdict"] == "UNVERIFIED"


def test_too_few_latency_samples_does_not_hide_error_rate_fail():
    r = evaluate_slo(summarize(one_success_rest_timeouts()), SLO(p95_ms_max=1000, error_rate_pct_max=1), 100)
    assert {c["name"]: c["status"] for c in r["checks"]} == {"p95_ms": "UNVERIFIED", "error_rate_pct": "FAIL"}
    assert r["verdict"] == "FAIL"


def test_latency_judged_at_exactly_min_samples():
    results = uniform(20, 0.01) + [RequestResult(i, 0.0, 0.01, 500, "http_error") for i in range(20, 25)]
    r = evaluate_slo(summarize(results), SLO(p95_ms_max=1000, min_latency_samples=20), 25)
    assert r["checks"][0]["status"] == "PASS"
    r = evaluate_slo(summarize(results), SLO(p95_ms_max=1000, min_latency_samples=21), 25)
    assert r["checks"][0]["status"] == "UNVERIFIED"
