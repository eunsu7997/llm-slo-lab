"""Closed-loop HTTP load generator for the mock chat-completions endpoint.

- exactly `total` requests, at most `concurrency` in flight, no retries, no infinite loops
- `timeout_s` bounds each request end to end (asyncio deadline) and every httpx phase
- localhost targets only
- every request ends in exactly one outcome (see perf.metrics.OUTCOMES)
"""

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from perf.metrics import RequestResult

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
CHAT_PATH = "/v1/chat/completions"
MAX_REQUESTS = 100_000
MAX_CONCURRENCY = 1_000


def require_localhost(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname not in LOCAL_HOSTS:
        raise ValueError(f"only http://localhost targets are allowed, got {base_url!r}")
    return base_url.rstrip("/")


def is_valid_completion(payload) -> bool:
    try:
        choice = payload["choices"][0]
        return (payload["object"] == "chat.completion"
                and isinstance(choice["message"]["content"], str)
                and choice["message"]["role"] == "assistant")
    except (KeyError, IndexError, TypeError):
        return False


@dataclass
class LoadRun:
    results: list[RequestResult] = field(default_factory=list)
    max_in_flight: int = 0


async def run_load(base_url: str, total: int, concurrency: int, timeout_s: float,
                   model: str = "mock-model", prompt: str = "ping",
                   transport: httpx.AsyncBaseTransport | None = None) -> LoadRun:
    base_url = require_localhost(base_url)
    if not 1 <= total <= MAX_REQUESTS:
        raise ValueError(f"total must be 1..{MAX_REQUESTS}")
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY}")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0")

    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    run = LoadRun()
    in_flight = 0
    next_index = 0
    t0 = time.perf_counter()

    async def one(client: httpx.AsyncClient, idx: int) -> RequestResult:
        nonlocal in_flight
        in_flight += 1
        run.max_in_flight = max(run.max_in_flight, in_flight)
        start = time.perf_counter() - t0
        status, outcome, err = None, "transport_error", None
        try:
            # Whole-request deadline (connect -> full body). httpx timeouts are per phase / per read,
            # so a server trickling bytes could otherwise exceed timeout_s and still "succeed".
            async with asyncio.timeout(timeout_s):
                resp = await client.post(CHAT_PATH, json=body)  # non-streaming: body fully read here
            status = resp.status_code
            if status != 200:
                outcome = "http_error"
            else:
                try:
                    outcome = "success" if is_valid_completion(resp.json()) else "invalid_response"
                except ValueError:
                    outcome = "invalid_response"
        except TimeoutError:
            outcome, err = "timeout", "DeadlineExceeded"
        except httpx.TimeoutException as e:
            outcome, err = "timeout", type(e).__name__
        except httpx.ConnectError as e:
            outcome, err = "connect_error", type(e).__name__
        except httpx.TransportError as e:
            outcome, err = "transport_error", type(e).__name__
        finally:
            in_flight -= 1
        return RequestResult(idx, start, time.perf_counter() - t0, status, outcome, err)

    async def worker(client):
        nonlocal next_index
        while next_index < total:  # single-threaded event loop: check-and-increment is atomic
            idx = next_index
            next_index += 1
            run.results.append(await one(client, idx))

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    transport = transport or httpx.AsyncHTTPTransport(retries=0, limits=limits)
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout_s),
                                 limits=limits, transport=transport) as client:
        await asyncio.gather(*[worker(client) for _ in range(min(concurrency, total))])

    run.results.sort(key=lambda r: r.index)
    return run
