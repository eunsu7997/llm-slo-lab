"""API contract tests.

Limits are written as literals on purpose (not imported from app.main) so that an
accidental change to an implementation constant is caught. Keep in sync with README.md.
"""

import asyncio
import json
import random
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app

CONTENT_MAX = 32_000
MESSAGES_MAX = 256
MODEL_MAX = 256
BODY_MAX = 1_000_000
DELAY_MAX_MS = 60_000

URL = "/v1/chat/completions"


def client(**kwargs) -> TestClient:
    return TestClient(create_app(Settings(**kwargs)))


def body(model="m", content="hi", n_messages=1):
    return {"model": model, "messages": [{"role": "user", "content": content}] * n_messages}


@pytest.mark.parametrize(
    "payload, expected",
    [
        (body(content="x" * CONTENT_MAX), 200),
        (body(content="x" * (CONTENT_MAX + 1)), 422),
        (body(n_messages=MESSAGES_MAX), 200),
        (body(n_messages=MESSAGES_MAX + 1), 422),
        (body(model="m" * MODEL_MAX), 200),
        (body(model="m" * (MODEL_MAX + 1)), 422),
    ],
    ids=["content-max", "content-over", "messages-max", "messages-over", "model-max", "model-over"],
)
def test_field_limits(payload, expected):
    assert client().post(URL, json=payload).status_code == expected


def padded_body(size: int) -> bytes:
    """Valid JSON padded with trailing whitespace to exactly `size` bytes."""
    raw = json.dumps(body()).encode()
    return raw + b" " * (size - len(raw))


@pytest.mark.parametrize("size, expected", [(BODY_MAX - 1, 200), (BODY_MAX, 200), (BODY_MAX + 1, 413)])
def test_body_size_boundary_content_length(size, expected):
    data = padded_body(size)
    assert len(data) == size
    r = client().post(URL, content=data, headers={"content-type": "application/json"})
    assert r.status_code == expected


@pytest.mark.parametrize("delay_ms, ok", [(0, True), (DELAY_MAX_MS, True), (DELAY_MAX_MS + 1, False)])
def test_delay_setting_boundary(delay_ms, ok):
    if ok:
        Settings(delay_ms=delay_ms)
    else:
        with pytest.raises(ValueError):
            Settings(delay_ms=delay_ms)


def test_unknown_path_is_exactly_404():
    r = client().get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "http_404"


def test_wrong_method_is_exactly_405_with_allow_header():
    r = client().get(URL)
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"
    assert r.json()["error"]["code"] == "http_405"


def expected_pattern(seed: int, rate: float, n: int) -> list[int]:
    # Contract: one random.Random(seed).random() draw per valid chat request, in order.
    rng = random.Random(seed)
    return [500 if rng.random() < rate else 200 for _ in range(n)]


def sequential_pattern(seed: int, rate: float, n: int) -> list[int]:
    c = client(error_rate=rate, seed=seed)
    return [c.post(URL, json=body()).status_code for _ in range(n)]


def test_seed_sequential_pattern_matches_contract():
    assert sequential_pattern(42, 0.5, 40) == expected_pattern(42, 0.5, 40)


def test_different_seeds_give_different_patterns():
    assert sequential_pattern(1, 0.5, 40) != sequential_pattern(2, 0.5, 40)


def test_concurrent_requests_individual_latency():
    app = create_app(Settings(delay_ms=500))
    n = 10

    async def timed(ac):
        t = time.perf_counter()
        r = await ac.post(URL, json=body())
        return r.status_code, time.perf_counter() - t

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            t = time.perf_counter()
            results = await asyncio.gather(*[timed(ac) for _ in range(n)])
            return results, time.perf_counter() - t

    results, total = asyncio.run(run())
    assert all(code == 200 for code, _ in results)
    # Each request must actually wait (lower bound) but not be serialized (upper bound).
    assert all(0.5 <= dt < 1.0 for _, dt in results), results
    assert 0.5 <= total < 1.0
