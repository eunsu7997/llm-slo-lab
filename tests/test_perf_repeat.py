"""perf.repeat CLI against real mock servers on 127.0.0.1 (small, loose-timing experiments)."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from perf import repeat
from perf.mock_server import PROJECT_ROOT

SMALL = ["--spawn-mock", "--runs", "3", "--requests", "40", "--concurrency", "5", "--warmup-requests", "5",
         "--slo-min-requests", "40"]


@pytest.fixture(scope="module")
def root():
    d = Path(tempfile.mkdtemp(prefix="llm-slo-repeat-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def run(root, *args):
    before = set(root.iterdir())
    code = repeat.main([*SMALL, *args, "--report-root", str(root)])
    (new,) = set(root.iterdir()) - before  # a new directory every time, nothing overwritten
    return code, new, json.loads((new / "experiment.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def baseline(root):
    code, path, exp = run(root, "--mock-delay-ms", "20")
    return code, path, exp


def test_baseline_run_without_baseline_is_unverified(baseline):
    code, path, exp = baseline
    assert code == 2 == exp["exit_code"]
    assert exp["slo_verdict"] == "PASS" and exp["regression_verdict"] == "UNVERIFIED" and exp["complete"]
    assert len(exp["runs"]) == 3
    for r in exp["runs"]:
        assert r["metrics"]["total_requests"] == 40  # warm-up excluded
        assert r["warmup"]["total"] == 5
    for i in (1, 2, 3):
        assert len((path / "runs" / f"run-{i}-requests.jsonl").read_text(encoding="utf-8").splitlines()) == 40
    assert (path / "report.md").exists() and len(exp["source_hash"]["combined"]) == 64


def test_latency_regression_detected_and_exit_code_matches(root, baseline):
    _, base_path, _ = baseline
    before = set(root.iterdir())
    p = subprocess.run([sys.executable, "-m", "perf.repeat", *SMALL, "--mock-delay-ms", "150",
                        "--baseline", str(base_path), "--allow-server-change", "--report-root", str(root)],
                       cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=180)
    (new,) = set(root.iterdir()) - before
    exp = json.loads((new / "experiment.json").read_text(encoding="utf-8"))
    checks = {c["name"]: c["status"] for c in exp["comparison"]["checks"]}
    assert checks["p95_increase_pct"] == "FAIL"
    assert exp["regression_verdict"] == "FAIL" and p.returncode == 1 == exp["exit_code"]


def test_error_rate_increase_detected(root, baseline):
    code, _, exp = run(root, "--mock-delay-ms", "20", "--mock-error-rate", "0.5", "--mock-seed", "1",
                       "--baseline", str(baseline[1]), "--allow-server-change")
    checks = {c["name"]: c["status"] for c in exp["comparison"]["checks"]}
    assert checks["error_rate_increase_pp"] == "FAIL" and code == 1 == exp["exit_code"]


def test_different_concurrency_is_unverified(root, baseline):
    code, _, exp = run(root, "--mock-delay-ms", "20", "--concurrency", "10", "--baseline", str(baseline[1]))
    assert exp["comparison"]["comparable"] is False and "concurrency" in exp["comparison"]["reason"]
    assert code == 2 == exp["exit_code"]


def test_server_change_without_declaration_is_unverified(root, baseline):
    code, _, exp = run(root, "--mock-delay-ms", "150", "--baseline", str(baseline[1]))
    assert exp["regression_verdict"] == "UNVERIFIED" and code == 2


def test_too_few_runs_is_unverified(root, baseline):
    code, _, exp = run(root, "--mock-delay-ms", "20", "--runs", "2", "--baseline", str(baseline[1]))
    assert "only 2 runs" in exp["comparison"]["reason"] and code == 2


def test_unreadable_baseline_is_unverified(root):
    code, _, exp = run(root, "--mock-delay-ms", "20", "--baseline", str(root / "does-not-exist.json"))
    assert exp["regression_verdict"] == "UNVERIFIED" and "unreadable" in exp["comparison"]["reason"] and code == 2


@pytest.mark.parametrize("args", [["--runs", "0"], ["--runs", "11"], ["--warmup-requests", "-1"],
                                  ["--url", "http://example.com"]])
def test_bad_arguments_exit_2(args, root):
    with pytest.raises(SystemExit) as e:
        repeat.main([*([] if "--url" in args else ["--spawn-mock"]), *args, "--report-root", str(root)])
    assert e.value.code == 2


# ---- Regression (Codex audit): baseline evidence, NaN thresholds, save failures ------------------------

import math  # noqa: E402

from perf.regression import Thresholds, compare  # noqa: E402


def baseline_copy(root, baseline, name, mutate=None):
    """Copy the genuine baseline directory and optionally corrupt the COPY."""
    dst = root / f"base-{name}"
    shutil.copytree(baseline[1], dst)
    if mutate:
        mutate(dst)
    return dst


def edit_json(path, fn):
    data = json.loads(path.read_text(encoding="utf-8"))
    data = fn(data)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def test_genuine_baseline_is_verified_and_comparable(root, baseline):
    rebuilt, problems, info = repeat.load_baseline(str(baseline[1]))
    assert problems == [] and info["verified"] and info["not_verifiable"]
    r = compare(rebuilt, baseline[2], Thresholds())
    assert r["comparable"]


def tamper_aggregate(d):
    def fn(e):
        e["aggregate"]["p95"]["median"] *= 0.5
        return e
    edit_json(d / "experiment.json", fn)


def delete_jsonl(d):
    (d / "runs" / "run-2-requests.jsonl").unlink()


def mismatch_record(d):
    f = d / "runs" / "run-1-requests.jsonl"
    lines = f.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["end_ms"] += 5000  # one request now 5 s slower than the stored metrics say
    lines[0] = json.dumps(rec)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def inject_nan(d):
    p = d / "experiment.json"
    p.write_text(p.read_text(encoding="utf-8").replace('"pooled_error_rate_pct": 0.0', '"pooled_error_rate_pct": NaN'),
                 encoding="utf-8")


def inject_infinity(d):
    def fn(e):
        e["runs"][0]["metrics"]["latency_ms"]["p95"] = math.inf
        return json.dumps(e)  # json.dumps writes Infinity
    edit_json(d / "experiment.json", fn)


def drop(key):
    def mutate(d):
        edit_json(d / "experiment.json", lambda e: {k: v for k, v in e.items() if k != key})
    return mutate


def write_text(content):
    def mutate(d):
        (d / "experiment.json").write_text(content, encoding="utf-8")
    return mutate


def wrong_types(d):
    def fn(e):
        e["conditions"]["requests"] = "40"
        return e
    edit_json(d / "experiment.json", fn)


@pytest.mark.parametrize("name, mutate, expected", [
    ("tampered-aggregate", tamper_aggregate, "aggregate p95.median differs"),
    ("deleted-jsonl", delete_jsonl, "run 2: request records missing"),
    ("record-mismatch", mismatch_record, "run 1: stored metrics differ from request records"),
    ("nan", inject_nan, "non-finite number NaN"),
    ("infinity", inject_infinity, "non-finite number Infinity"),
    ("empty-list", write_text("[]"), "must be an object"),
    ("empty-object", write_text("{}"), "not a perf.repeat experiment"),
    ("no-aggregate", drop("aggregate"), "stored aggregate missing"),
    ("no-runs", drop("runs"), "runs missing"),
    ("wrong-types", wrong_types, "conditions.requests invalid"),
    ("corrupt-json", write_text('{"kind": "repeat-experiment", "runs": ['), "baseline unreadable"),
])
def test_corrupted_baseline_is_rejected_without_traceback(root, baseline, name, mutate, expected):
    rebuilt, problems, info = repeat.load_baseline(str(baseline_copy(root, baseline, name, mutate)))
    assert rebuilt is None and not info["verified"]
    assert any(expected in p for p in problems), problems


def test_missing_baseline_file_is_rejected(root):
    rebuilt, problems, _ = repeat.load_baseline(str(root / "nope" / "experiment.json"))
    assert rebuilt is None and "baseline unreadable" in problems[0]


def test_tampered_baseline_via_cli_is_unverified_not_pass(root, baseline):
    d = baseline_copy(root, baseline, "cli-tampered", tamper_aggregate)
    code, _, exp = run(root, "--mock-delay-ms", "20", "--baseline", str(d))
    assert exp["regression_verdict"] == "UNVERIFIED" and "baseline rejected" in exp["comparison"]["reason"]
    assert code == 2 == exp["exit_code"]


def test_rejected_baseline_keeps_candidate_slo_fail(root, baseline):
    d = baseline_copy(root, baseline, "cli-empty", write_text("[]"))
    code, _, exp = run(root, "--mock-delay-ms", "20", "--mock-error-rate", "1", "--baseline", str(d))
    assert exp["slo_verdict"] == "FAIL" and exp["regression_verdict"] == "UNVERIFIED"
    assert code == 1 == exp["exit_code"]


@pytest.mark.parametrize("args", [
    ["--max-p95-increase-pct", "nan"], ["--max-success-rps-decrease-pct", "nan"],
    ["--max-p95-increase-pct", "inf"], ["--max-error-rate-increase-pp", "-inf"],
    ["--max-spread-pct", "NaN"], ["--max-p95-increase-pct", "-5"], ["--max-success-rps-decrease-pct", "150"],
    ["--min-runs", "0"], ["--slo-p95-ms", "nan"], ["--slo-error-rate-pct", "inf"], ["--timeout-s", "nan"],
])
def test_non_finite_or_out_of_range_numbers_exit_2(args, root):
    with pytest.raises(SystemExit) as e:
        repeat.main(["--spawn-mock", *args, "--report-root", str(root)])
    assert e.value.code == 2


def fail_saving(monkeypatch):
    def broken(path, results):
        raise OSError("injected JSONL write failure")
    monkeypatch.setattr(repeat, "write_requests", broken)


def test_save_failure_after_all_500_keeps_fail(root, monkeypatch):
    fail_saving(monkeypatch)
    code, _, exp = run(root, "--mock-error-rate", "1")
    assert len(exp["runs"]) == 3 and all(not r["requests_saved"] for r in exp["runs"])
    assert len(exp["evidence_errors"]) == 3 and exp["slo_verdict"] == "FAIL"
    assert code == 1 == exp["exit_code"] and exp["verdict"] == "FAIL"


def test_save_failure_after_good_runs_is_never_pass(root, baseline, monkeypatch):
    fail_saving(monkeypatch)
    code, _, exp = run(root, "--mock-delay-ms", "20", "--baseline", str(baseline[1]))
    assert exp["slo_verdict"] == "PASS" and exp["evidence_errors"]
    assert exp["verdict"] == "UNVERIFIED" and code == 2 == exp["exit_code"]


def test_save_failure_after_partial_failures_keeps_slo_violation(root, monkeypatch):
    fail_saving(monkeypatch)
    code, _, exp = run(root, "--mock-error-rate", "0.5", "--mock-seed", "3")
    assert exp["slo_verdict"] == "FAIL" and code == 1 == exp["exit_code"]
