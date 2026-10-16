"""Start the project's mock server (app.main) as a real Uvicorn process on 127.0.0.1."""

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def spawn_mock(delay_ms: int = 0, error_rate: float = 0.0, seed: int | None = None, startup_timeout_s: float = 15):
    """Yields the base URL. Raises RuntimeError if the server does not become healthy."""
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_MOCK_")}
    env.update(LLM_MOCK_DELAY_MS=str(delay_ms), LLM_MOCK_ERROR_RATE=str(error_rate))
    if seed is not None:
        env["LLM_MOCK_SEED"] = str(seed)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + startup_timeout_s
        while True:
            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace")[-500:]
                raise RuntimeError(f"mock server exited with code {proc.returncode}: {err}")
            try:
                if httpx.get(base + "/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("mock server not healthy in time")
            time.sleep(0.1)
        yield base
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stderr.close()
