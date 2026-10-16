"""LLM-SLO Lab - mock OpenAI-compatible chat completions server.

This server does NOT run an LLM. It returns a fixed mock response so that
latency/error handling can be exercised without a GPU or a real model.
The configured delay is an artificial wait, not a TTFT/TPOT measurement.

Configuration (environment variables):
    LLM_MOCK_DELAY_MS    fake response delay in milliseconds, 0..60000 (default 0)
    LLM_MOCK_ERROR_RATE  probability in [0, 1] of returning HTTP 500 (default 0)
    LLM_MOCK_SEED        optional integer seed for reproducible error injection
"""

import asyncio
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, field_validator
from starlette.exceptions import HTTPException

logger = logging.getLogger("llm_slo_lab")

MOCK_CONTENT = "[MOCK] This is a mock response. No LLM inference was performed."

MAX_DELAY_MS = 60_000
MAX_BODY_BYTES = 1_000_000
MAX_MESSAGES = 256
MAX_CONTENT_CHARS = 32_000
MAX_MODEL_CHARS = 256
SUPPORTED_PARAMS = ("model", "messages", "stream")


@dataclass(frozen=True)
class Settings:
    delay_ms: int = 0
    error_rate: float = 0.0
    seed: int | None = None

    def __post_init__(self):
        if not 0 <= self.delay_ms <= MAX_DELAY_MS:
            raise ValueError(f"delay_ms must be between 0 and {MAX_DELAY_MS}")
        if not 0.0 <= self.error_rate <= 1.0:
            raise ValueError("error_rate must be between 0 and 1")

    @classmethod
    def from_env(cls) -> "Settings":
        def read(name: str, default: str, cast):
            raw = os.getenv(name, default)
            try:
                return cast(raw)
            except ValueError:
                raise ValueError(f"invalid {name}={raw!r}") from None

        return cls(
            delay_ms=read("LLM_MOCK_DELAY_MS", "0", int),
            error_rate=read("LLM_MOCK_ERROR_RATE", "0", float),
            seed=read("LLM_MOCK_SEED", "", int) if os.getenv("LLM_MOCK_SEED") else None,
        )


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: Annotated[str, StringConstraints(strict=True, max_length=MAX_CONTENT_CHARS)]


class ChatCompletionRequest(BaseModel):
    """Only the fields below are supported; any other OpenAI parameter is rejected."""

    model_config = ConfigDict(extra="forbid")

    model: Annotated[
        str,
        StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=MAX_MODEL_CHARS),
    ]
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    stream: StrictBool = False

    @field_validator("stream")
    @classmethod
    def reject_streaming(cls, v: bool) -> bool:
        if v:
            raise ValueError("streaming is not supported by this mock server")
        return v


def error_response(
    status: int, message: str, type_: str, code: str, param: str | None = None, headers=None
):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "param": param, "code": code}},
        headers=headers,
    )


class BodySizeLimitMiddleware:
    """Rejects request bodies larger than max_bytes, including chunked uploads."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        for name, value in scope["headers"]:
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                response = error_response(
                    413, "Request body too large", "invalid_request_error", "body_too_large"
                )
                return await response(scope, receive, send)

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                raise HTTPException(413, "Request body too large")
            return message

        await self.app(scope, limited_receive, send)


def create_app(settings: Settings | None = None, rng: random.Random | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    rng = rng or random.Random(settings.seed)
    app = FastAPI(title="LLM-SLO Lab Mock Server")
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        # Input values are intentionally not echoed back.
        err = exc.errors()[0]
        loc = [str(p) for p in err["loc"] if p != "body"]
        param = ".".join(loc) or None
        if err["type"] == "extra_forbidden":
            message = (
                f"Unsupported parameter: '{param}'. "
                f"This mock server supports only: {', '.join(SUPPORTED_PARAMS)} (stream=false)."
            )
            code = "unsupported_parameter"
        else:
            message = f"{param}: {err['msg']}" if param else err["msg"]
            code = "invalid_value"
        return error_response(422, message, "invalid_request_error", code, param)

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException):
        code = "body_too_large" if exc.status_code == 413 else f"http_{exc.status_code}"
        # Preserve headers such as Allow on 405.
        return error_response(
            exc.status_code, str(exc.detail), "invalid_request_error", code, headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def on_unexpected_error(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return error_response(500, "Internal server error", "server_error", "internal_error")

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest):
        if settings.delay_ms:
            # Non-blocking: other requests keep being served during the delay.
            await asyncio.sleep(settings.delay_ms / 1000)

        if settings.error_rate and rng.random() < settings.error_rate:
            return error_response(
                500, "Simulated internal server error", "server_error", "simulated_error"
            )

        return {
            "id": f"chatcmpl-mock-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": MOCK_CONTENT},
                    "finish_reason": "stop",
                }
            ],
        }

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
