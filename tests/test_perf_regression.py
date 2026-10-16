"""Repeat aggregation and regression judgement on synthetic experiments (no network, no timing)."""

import pytest

from perf.regression import METHOD, Thresholds, aggregate, compare

T = Thresholds()  # p95 +20 %, success RPS -20 %, error +1 pp, spread 25 %, min 3 runs


def experiment(p95s, rps=None, errors=None, requests=100, samples=None, server=None, complete=True,
               aborted=None, valid=None, **conditions):
    runs = []
    for i, p95 in enumerate(p95s):
        failed = errors[i] if errors else 0
        metrics = {
            "total_requests": requests, "failed_requests": failed, "successful_requests": requests - failed,
            "error_rate_pct": failed * 100 / requests, "rps": 100.0,
            "success_rps": rps[i] if rps else 100.0,
            "latency_ms": {"samples": samples if samples is not None else requests - failed,
                           "p50": p95 * 0.8, "p95": p95, "p99": p95 * 1.1},
        }
        runs.append({"run": i + 1, "metrics": metrics,
                     "slo": {"run_valid": True if valid is None else valid[i], "verdict": "PASS"}})
    cond = {"target_kind": "mock", "method": METHOD, "requests": requests, "concurrency": 10, "timeout_s": 30.0,
            "warmup_requests": 20, "model": "mock-model", "min_latency_samples": 20}
    cond.update(conditions)
    exp = {"kind": "repeat-experiment", "complete": complete, "aborted": aborted, "conditions": cond,
           "server_config": server if server is not None else {"delay_ms": 100, "error_rate": 0.0, "seed": None},
           "runs": runs}
    exp["aggregate"] = aggregate([r["metrics"] for r in runs])
    return exp


def statuses(result):
    return {c["name"]: c["status"] for c in result["checks"]}


BASE = experiment([100, 102, 104])


# ---- aggregation ---------------------------------------------------------------------------------------


def test_aggregate_median_min_max_spread():
    a = aggregate([r["metrics"] for r in experiment([100, 120, 110])["runs"]])["p95"]
    assert (a["median"], a["min"], a["max"]) == (110, 100, 120)
    assert a["spread_pct"] == pytest.approx(20 / 110 * 100)


def test_aggregate_even_number_of_runs_uses_middle_mean():
    assert aggregate([r["metrics"] for r in experiment([100, 110, 120, 130])["runs"]])["p95"]["median"] == 115


def test_pooled_error_rate_is_not_hidden_by_median():
    a = experiment([100] * 5, errors=[0, 0, 0, 0, 50])["aggregate"]
    assert a["error_rate_pct"]["median"] == 0  # the median alone would hide the bad run
    assert a["pooled_error_rate_pct"] == pytest.approx(10.0)


# ---- regression checks ---------------------------------------------------------------------------------


def test_no_baseline_is_unverified_not_pass():
    r = compare(None, BASE, T)
    assert r["verdict"] == "UNVERIFIED" and "no baseline" in r["reason"]


def test_same_performance_is_pass():
    r = compare(BASE, experiment([101, 103, 103]), T)
    assert r["verdict"] == "PASS" and set(statuses(r).values()) == {"PASS"}


def test_clear_latency_regression_is_fail():
    r = compare(BASE, experiment([150, 152, 155]), T)
    assert statuses(r)["p95_increase_pct"] == "FAIL" and r["verdict"] == "FAIL"


def test_latency_increase_with_overlapping_runs_is_unverified():
    # median +28 % but one candidate run (103) is faster than the slowest baseline run (104)
    r = compare(BASE, experiment([103, 131, 140]), T)
    assert statuses(r)["p95_increase_pct"] == "UNVERIFIED" and r["verdict"] == "UNVERIFIED"


def test_small_change_but_noisy_runs_is_unverified_not_pass():
    noisy = experiment([80, 105, 125])  # spread ~43 %
    r = compare(BASE, noisy, T)
    assert statuses(r)["p95_increase_pct"] == "UNVERIFIED" and r["verdict"] == "UNVERIFIED"


def test_throughput_regression_is_fail():
    r = compare(experiment([100] * 3, rps=[100, 101, 99]), experiment([100] * 3, rps=[70, 72, 71]), T)
    assert statuses(r)["success_rps_decrease_pct"] == "FAIL" and r["verdict"] == "FAIL"


def test_error_rate_increase_in_one_run_is_fail():
    r = compare(BASE, experiment([100, 102, 104], errors=[0, 0, 6]), T)  # pooled 2 % vs 0 %
    assert statuses(r)["error_rate_increase_pp"] == "FAIL" and r["verdict"] == "FAIL"


def test_fail_wins_over_unverified():
    r = compare(BASE, experiment([80, 105, 125], errors=[5, 5, 5]), T)
    assert statuses(r)["p95_increase_pct"] == "UNVERIFIED" and statuses(r)["error_rate_increase_pp"] == "FAIL"
    assert r["verdict"] == "FAIL"


def test_thresholds_are_configurable():
    cand = experiment([115, 116, 117])  # ~ +13 %, no overlap
    assert compare(BASE, cand, T)["verdict"] == "PASS"
    assert compare(BASE, cand, Thresholds(p95_increase_pct=10))["verdict"] == "FAIL"


# ---- comparability and sample sufficiency ----------------------------------------------------------------


@pytest.mark.parametrize("key, value", [("concurrency", 20), ("requests", 50), ("timeout_s", 5.0),
                                        ("warmup_requests", 0), ("target_kind", "openai-compatible"),
                                        ("method", "open-loop"), ("min_latency_samples", 50)])
def test_different_workload_conditions_are_unverified(key, value):
    cand = experiment([150, 152, 155], **{key: value})  # would be a clear regression if comparable
    r = compare(BASE, cand, T)
    assert r["verdict"] == "UNVERIFIED" and not r["comparable"] and key in r["reason"]


def test_server_change_requires_explicit_declaration():
    cand = experiment([150, 152, 155], server={"delay_ms": 150, "error_rate": 0.0, "seed": None})
    assert compare(BASE, cand, T)["verdict"] == "UNVERIFIED"
    declared = compare(BASE, cand, T, allow_server_change=True)
    assert declared["verdict"] == "FAIL" and declared["accepted_differences"]


def test_external_targets_cannot_be_confirmed_identical():
    ext = "external http://127.0.0.1:8000 (settings unknown)"
    r = compare(experiment([100, 102, 104], server=ext), experiment([101, 102, 103], server=ext), T,
                allow_server_change=True)
    assert r["verdict"] == "UNVERIFIED" and "unknown" in r["reason"]


@pytest.mark.parametrize("cand, reason", [
    (experiment([101, 103]), "only 2 runs"),
    (experiment([101, 103, 103], aborted="interrupted"), "aborted"),
    (experiment([101, 103, 103], valid=[True, False, True]), "invalid runs"),
    (experiment([101, 103, 103], complete=False), "incomplete"),
], ids=["too-few-runs", "aborted", "invalid-run", "incomplete"])
def test_unusable_candidate_is_unverified(cand, reason):
    r = compare(BASE, cand, T)
    assert r["verdict"] == "UNVERIFIED" and reason in r["reason"]


def test_unusable_baseline_is_unverified():
    r = compare(experiment([100, 102]), experiment([150, 152, 155]), T)
    assert r["verdict"] == "UNVERIFIED" and "baseline: only 2 runs" in r["reason"]


def test_few_latency_samples_blocks_only_the_latency_check():
    r = compare(BASE, experiment([101, 103, 103], samples=5), T)
    s = statuses(r)
    assert s["p95_increase_pct"] == "UNVERIFIED" and s["error_rate_increase_pp"] == "PASS"
    assert "fewer than 20 successful latency samples" in r["checks"][0]["reason"]
    assert r["verdict"] == "UNVERIFIED"


def test_few_latency_samples_does_not_hide_error_regression():
    # many errors -> few successes; the error-rate regression must still be reported as FAIL
    r = compare(BASE, experiment([101, 103, 103], errors=[90, 90, 90], samples=10), T)
    s = statuses(r)
    assert s["p95_increase_pct"] == "UNVERIFIED" and s["error_rate_increase_pp"] == "FAIL"
    assert r["verdict"] == "FAIL"

# ---- Regression (Codex audit): thresholds must be finite and in range ----------------------------------

from perf.regression import rebuild_from_evidence, threshold_problems  # noqa: E402


@pytest.mark.parametrize("field, value", [
    ("p95_increase_pct", float("nan")), ("success_rps_decrease_pct", float("nan")),
    ("p95_increase_pct", float("inf")), ("error_rate_increase_pp", float("-inf")),
    ("max_spread_pct", float("nan")), ("p95_increase_pct", -1.0), ("success_rps_decrease_pct", 101.0),
    ("error_rate_increase_pp", 100.5), ("min_runs", 0), ("min_runs", 2.5), ("min_runs", True),
])
def test_invalid_thresholds_are_rejected(field, value):
    with pytest.raises(ValueError):
        Thresholds(**{field: value})


def test_bypassed_nan_threshold_blocks_comparison():
    t = Thresholds()
    object.__setattr__(t, "p95_increase_pct", float("nan"))  # bypasses __post_init__
    object.__setattr__(t, "success_rps_decrease_pct", float("nan"))
    clear_regression = experiment([150, 152, 155], rps=[60, 61, 62])
    r = compare(BASE, clear_regression, t)
    assert r["verdict"] == "UNVERIFIED" and "invalid thresholds" in r["reason"]
    assert threshold_problems(t)


def test_valid_thresholds_keep_existing_verdicts():
    assert compare(BASE, experiment([150, 152, 155]), Thresholds())["verdict"] == "FAIL"
    assert compare(BASE, experiment([101, 103, 103]), Thresholds())["verdict"] == "PASS"


def test_compare_on_malformed_experiment_is_unverified_not_crash():
    broken = experiment([101, 103, 103])
    del broken["aggregate"]
    assert compare(BASE, broken, T)["verdict"] == "UNVERIFIED"


# ---- Regression (Codex audit): stored baseline structure ------------------------------------------------


@pytest.mark.parametrize("exp, expected", [
    ([], "must be an object"),
    ({}, "not a perf.repeat experiment"),
    ({"kind": "repeat-experiment", "complete": True, "conditions": "x"}, "conditions missing"),
    ({"kind": "repeat-experiment", "complete": True, "conditions": {}, "runs": []}, "conditions.requests invalid"),
], ids=["list", "empty-object", "conditions-wrong-type", "empty-conditions"])
def test_rebuild_rejects_malformed_structure(exp, expected):
    rebuilt, problems = rebuild_from_evidence(exp, {})
    assert rebuilt is None and any(expected in p for p in problems)
