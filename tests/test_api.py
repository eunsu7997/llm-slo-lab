import asyncio
import random
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import (
    MAX_BODY_BYTES,
    MAX_CONTENT_CHARS,
    MAX_DELAY_MS,
    MAX_MESSAGES,
    MOCK_CONTENT,
    Settings,
    create_app,
)

VALID_BODY = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}


def client(**kwargs) -> TestClient:
    return TestClient(create_app(Settings(**kwargs), rng=random.Random(0)))


def assert_error_shape(r, type_):
    assert r.headers["content-type"].startswith("application/json")
    err = r.json()["error"]
    assert set(err) == {"message", "type", "param", "code"}
    assert err["type"] == type_
    return err


def test_healthz():
    r = client().get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_chat_completion_ok_structure():
    r = client().post("/v1/chat/completions", json=VALID_BODY)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["id"].startswith("chatcmpl-mock-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == "mock-model"
    assert len(body["choices"]) == 1
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"] == {"role": "assistant", "content": MOCK_CONTENT}
    assert choice["finish_reason"] == "stop"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"model": "m"},
        {"model": "", "messages": [{"role": "user", "content": "hi"}]},
        {"model": "m", "messages": []},
        {"model": "m", "messages": [{"role": "robot", "content": "hi"}]},
        {"model": "m", "messages": [{"role": "user"}]},
        {**VALID_BODY, "max_tokens": 0},
        {**VALID_BODY, "temperature": 3},
        {**VALID_BODY, "stream": True},
    ],
)
def test_invalid_input_returns_422(body):
    r = client().post("/v1/chat/completions", json=body)
    assert r.status_code == 422
    assert_error_shape(r, "invalid_request_error")


def test_malformed_json_returns_422():
    r = client().post(
        "/v1/chat/completions", content="{not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 422


def test_delay_is_applied():
    c = client(delay_ms=300)
    start = time.perf_counter()
    r = c.post("/v1/chat/completions", json=VALID_BODY)
    elapsed = time.perf_counter() - start
    assert r.status_code == 200
    assert elapsed >= 0.3


def test_concurrent_requests_are_not_serialized():
    app = create_app(Settings(delay_ms=500))
    n = 10

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            start = time.perf_counter()
            rs = await asyncio.gather(
                *[ac.post("/v1/chat/completions", json=VALID_BODY) for _ in range(n)]
            )
            return rs, time.perf_counter() - start

    rs, elapsed = asyncio.run(run())
    assert all(r.status_code == 200 for r in rs)
    # Serialized execution would take n * 0.5 = 5s; concurrent should take ~0.5s.
    assert elapsed < 1.5


def test_error_simulation_always():
    r = client(error_rate=1.0).post("/v1/chat/completions", json=VALID_BODY)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "simulated_error"


def test_error_simulation_never():
    c = client(error_rate=0.0)
    assert all(c.post("/v1/chat/completions", json=VALID_BODY).status_code == 200 for _ in range(20))


def test_error_simulation_partial_rate():
    c = client(error_rate=0.5)
    codes = [c.post("/v1/chat/completions", json=VALID_BODY).status_code for _ in range(200)]
    assert set(codes) == {200, 500}
    assert 60 <= codes.count(500) <= 140


def test_healthz_unaffected_by_error_simulation():
    assert client(error_rate=1.0).get("/healthz").status_code == 200


@pytest.mark.parametrize("kwargs", [{"delay_ms": -1}, {"error_rate": 1.5}, {"error_rate": -0.1}])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


# --- Regression tests for QA review findings ---


@pytest.mark.parametrize(
    "param, value",
    [
        ("n", 2),
        ("max_tokens", 1),
        ("max_completion_tokens", 0),
        ("temperature", 0.5),
        ("response_format", {"type": "json_object"}),
        ("tools", []),
        ("modle", "typo"),
    ],
)
def test_unsupported_parameters_rejected(param, value):
    r = client().post("/v1/chat/completions", json={**VALID_BODY, param: value})
    assert r.status_code == 422
    err = assert_error_shape(r, "invalid_request_error")
    assert err["code"] == "unsupported_parameter"
    assert err["param"] == param


def test_unsupported_message_field_rejected():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi", "name": "x"}]}
    r = client().post("/v1/chat/completions", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["param"] == "messages.0.name"


@pytest.mark.parametrize(
    "body",
    [
        {"model": "   ", "messages": [{"role": "user", "content": "hi"}]},
        {"model": 123, "messages": [{"role": "user", "content": "hi"}]},
        {"model": "m", "messages": [{"role": "user", "content": 123}]},
        {**VALID_BODY, "stream": "false"},
        {**VALID_BODY, "stream": 0},
    ],
)
def test_strict_types_and_blank_model_rejected(body):
    r = client().post("/v1/chat/completions", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_value"


def test_stream_false_explicitly_accepted():
    r = client().post("/v1/chat/completions", json={**VALID_BODY, "stream": False})
    assert r.status_code == 200


def test_validation_error_does_not_echo_input():
    secret = "sk-SHOULD-NOT-BE-ECHOED"
    body = {"model": "m", "messages": [{"role": "robot", "content": secret}]}
    r = client().post("/v1/chat/completions", json=body)
    assert r.status_code == 422
    assert secret not in r.text
    assert "robot" not in r.text


def test_malformed_json_error_shape():
    r = client().post(
        "/v1/chat/completions", content="{not json", headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    assert_error_shape(r, "invalid_request_error")


def test_simulated_500_error_shape():
    r = client(error_rate=1.0).post("/v1/chat/completions", json=VALID_BODY)
    err = assert_error_shape(r, "server_error")
    assert err["code"] == "simulated_error"


def test_unexpected_exception_returns_json_500():
    app = create_app(Settings())

    @app.get("/boom")
    async def boom():
        raise RuntimeError("internal detail")

    r = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert r.status_code == 500
    err = assert_error_shape(r, "server_error")
    assert err["code"] == "internal_error"
    assert "internal detail" not in r.text


@pytest.mark.parametrize("method, path", [("get", "/nope"), ("get", "/v1/chat/completions")])
def test_http_errors_use_same_shape(method, path):
    r = getattr(client(), method)(path)
    assert r.status_code in (404, 405)
    assert_error_shape(r, "invalid_request_error")


def test_too_many_messages_rejected():
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}] * (MAX_MESSAGES + 1)}
    r = client().post("/v1/chat/completions", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["param"] == "messages"


def test_content_too_long_rejected():
    body = {"model": "m", "messages": [{"role": "user", "content": "x" * (MAX_CONTENT_CHARS + 1)}]}
    r = client().post("/v1/chat/completions", json=body)
    assert r.status_code == 422


def test_body_too_large_rejected_by_content_length():
    payload = '{"model":"m","messages":[{"role":"user","content":"' + "x" * MAX_BODY_BYTES + '"}]}'
    r = client().post(
        "/v1/chat/completions", content=payload, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "body_too_large"


def test_body_too_large_rejected_when_chunked():
    def chunks():
        yield b'{"model":"m","messages":[{"role":"user","content":"'
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"x" * 65536
        yield b'"}]}'

    r = client().post(
        "/v1/chat/completions", content=chunks(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "body_too_large"


def test_delay_upper_bound():
    Settings(delay_ms=MAX_DELAY_MS)
    with pytest.raises(ValueError):
        Settings(delay_ms=MAX_DELAY_MS + 1)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("LLM_MOCK_DELAY_MS", "250")
    monkeypatch.setenv("LLM_MOCK_ERROR_RATE", "0.25")
    monkeypatch.setenv("LLM_MOCK_SEED", "42")
    assert Settings.from_env() == Settings(delay_ms=250, error_rate=0.25, seed=42)


def test_settings_from_env_defaults(monkeypatch):
    for name in ("LLM_MOCK_DELAY_MS", "LLM_MOCK_ERROR_RATE", "LLM_MOCK_SEED"):
        monkeypatch.delenv(name, raising=False)
    assert Settings.from_env() == Settings()


@pytest.mark.parametrize(
    "name, value",
    [
        ("LLM_MOCK_DELAY_MS", "abc"),
        ("LLM_MOCK_DELAY_MS", "999999999"),
        ("LLM_MOCK_ERROR_RATE", "nan"),
        ("LLM_MOCK_ERROR_RATE", "2"),
        ("LLM_MOCK_SEED", "x"),
    ],
)
def test_invalid_env_rejected_with_clear_message(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_seed_makes_error_pattern_reproducible():
    def pattern():
        c = TestClient(create_app(Settings(error_rate=0.5, seed=123)))
        return [c.post("/v1/chat/completions", json=VALID_BODY).status_code for _ in range(30)]

    assert pattern() == pattern()
