"""TCP smoke test against real Uvicorn processes bound to 127.0.0.1.

Exit codes (see docs/quality-gates.md):
    0  PASS        every check ran and passed
    1  FAIL        a check failed or the server could not start
    2  UNVERIFIED  the environment prevented running the checks (e.g. no localhost socket)

Latency numbers printed here are the artificial mock delay plus HTTP overhead.
They are NOT LLM TTFT/TPOT measurements.
"""

import argparse
import asyncio
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

URL = "/v1/chat/completions"
BODY = {"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
BODY_MAX = 1_000_000
ERROR_KEYS = {"message", "type", "param", "code"}


class Unverified(Exception):
    pass


class Smoke:
    def __init__(self, project_root: Path, log_dir: Path):
        self.root = project_root
        self.log_dir = log_dir
        self.results = []

    def check(self, name, ok, detail=""):
        ok = bool(ok)
        self.results.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}".rstrip(), flush=True)

    # --- server management -------------------------------------------------

    @staticmethod
    def free_port() -> int:
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
        except OSError as e:
            raise Unverified(f"cannot bind a localhost socket: {e}") from e

    def start(self, label, **env):
        port = self.free_port()
        log = open(self.log_dir / f"server_{label}.log", "wb")
        child_env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_MOCK_")}
        child_env.update({k: str(v) for k, v in env.items()})
        cmd = [sys.executable, "-m", "uvicorn", "app.main:app",
               "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
        try:
            proc = subprocess.Popen(cmd, cwd=self.root, env=child_env, stdout=log, stderr=log)
        except OSError as e:
            raise Unverified(f"cannot spawn server process: {e}") from e
        finally:
            log.close()  # the child keeps its own handle
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return proc, base, f"server exited with code {proc.returncode} (see {log.name})"
                try:
                    if httpx.get(base + "/healthz", timeout=0.5).status_code == 200:
                        return proc, base, None
                except httpx.HTTPError:
                    time.sleep(0.2)
        except BaseException:
            self.stop(proc)  # never leave a server behind on an unexpected error
            raise
        self.stop(proc)
        return proc, base, f"server not healthy within 15s (see {log.name})"

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def error_ok(r, status, code):
        try:
            err = r.json()["error"]
        except (ValueError, KeyError, TypeError):
            return False
        return r.status_code == status and set(err) == ERROR_KEYS and err["code"] == code

    @staticmethod
    def padded(size: int) -> bytes:
        raw = json.dumps(BODY).encode()
        return raw + b" " * (size - len(raw))

    @staticmethod
    def post_chunked(port: int, data: bytes, chunk=65536):
        """Raw HTTP/1.1 request with Transfer-Encoding: chunked, split into many chunks."""
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(
                b"POST " + URL.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n"
            )
            try:
                for i in range(0, len(data), chunk):
                    part = data[i:i + chunk]
                    s.sendall(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                s.sendall(b"0\r\n\r\n")
            except OSError:
                pass  # server may answer 413 and close before the upload finishes
            resp = b""
            try:
                while True:
                    buf = s.recv(65536)
                    if not buf:
                        break
                    resp += buf
            except OSError:
                pass
        head, _, payload = resp.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head else 0
        try:
            body = json.loads(payload.decode()) if payload else None
        except ValueError:
            body = None  # e.g. chunked-encoded response body; status is what matters
        return status, body

    # --- scenarios -----------------------------------------------------------

    def basic(self):
        proc, base, err = self.start("basic")
        if err:
            self.check("server start (basic)", False, err)
            return
        try:
            port = int(base.rsplit(":", 1)[1])
            r = httpx.get(base + "/healthz")
            self.check("healthz 200", r.status_code == 200 and r.json() == {"status": "ok"})

            r = httpx.post(base + URL, json=BODY)
            j = r.json()
            self.check("chat 200 + structure", r.status_code == 200
                       and j.get("object") == "chat.completion"
                       and j["choices"][0]["message"]["content"].startswith("[MOCK]")
                       and j["choices"][0]["finish_reason"] == "stop")

            r = httpx.post(base + URL, json={**BODY, "max_tokens": 5})
            self.check("unsupported param -> 422", self.error_ok(r, 422, "unsupported_parameter"))

            r = httpx.post(base + URL, json={"model": "m", "messages": []})
            self.check("invalid value -> 422", self.error_ok(r, 422, "invalid_value"))

            r = httpx.get(base + "/nope")
            self.check("unknown path -> exactly 404", self.error_ok(r, 404, "http_404"))

            r = httpx.get(base + URL)
            self.check("wrong method -> exactly 405 + Allow",
                       self.error_ok(r, 405, "http_405") and r.headers.get("allow") == "POST",
                       f"allow={r.headers.get('allow')!r}")

            for size, expected in [(BODY_MAX - 1, 200), (BODY_MAX, 200), (BODY_MAX + 1, 413)]:
                status, body = self.post_chunked(port, self.padded(size))
                ok = status == expected
                if expected == 413:
                    ok = ok and body is not None and body.get("error", {}).get("code") == "body_too_large"
                self.check(f"chunked {size:,} bytes -> {expected}", ok, f"got {status}")

            r = httpx.post(base + URL, content=self.padded(BODY_MAX + 1),
                           headers={"content-type": "application/json"})
            self.check("content-length 1,000,001 bytes -> 413", self.error_ok(r, 413, "body_too_large"))
        finally:
            self.stop(proc)

    def concurrency(self, n=10):
        proc, base, err = self.start("delay", LLM_MOCK_DELAY_MS=500)
        if err:
            self.check("server start (delay=500ms)", False, err)
            return
        try:
            async def timed(c):
                t = time.perf_counter()
                r = await c.post(URL, json=BODY)
                return r.status_code, time.perf_counter() - t

            async def run():
                async with httpx.AsyncClient(base_url=base, timeout=10) as c:
                    t = time.perf_counter()
                    res = await asyncio.gather(*[timed(c) for _ in range(n)])
                    total = time.perf_counter() - t
                    slow = asyncio.create_task(c.post(URL, json=BODY))
                    await asyncio.sleep(0.1)
                    t = time.perf_counter()
                    h = await c.get("/healthz")
                    h_dt = time.perf_counter() - t
                    await slow
                    return res, total, h.status_code, h_dt

            res, total, h_status, h_dt = asyncio.run(run())
            lat = sorted(dt for _, dt in res)
            self.check(f"{n} concurrent @500ms: all 200", all(code == 200 for code, _ in res))
            self.check(f"{n} concurrent @500ms: each 0.5s <= latency < 1.5s",
                       lat[0] >= 0.5 and lat[-1] < 1.5, f"min={lat[0]:.3f}s max={lat[-1]:.3f}s")
            self.check(f"{n} concurrent @500ms: total < 1.5s (not serialized)",
                       total < 1.5, f"total={total:.3f}s")
            self.check("healthz not blocked by delayed request",
                       h_status == 200 and h_dt < 0.2, f"{h_dt * 1000:.1f}ms")
        finally:
            self.stop(proc)

    def errors(self):
        proc, base, err = self.start("error", LLM_MOCK_ERROR_RATE=1)
        if err:
            self.check("server start (error_rate=1)", False, err)
            return
        try:
            r = httpx.post(base + URL, json=BODY)
            self.check("simulated 500 + error shape", self.error_ok(r, 500, "simulated_error"))
        finally:
            self.stop(proc)

        seed, rate, n = 42, 0.5, 20
        rng = random.Random(seed)
        expected = [500 if rng.random() < rate else 200 for _ in range(n)]
        proc, base, err = self.start("seed", LLM_MOCK_ERROR_RATE=rate, LLM_MOCK_SEED=seed)
        if err:
            self.check("server start (seed=42)", False, err)
            return
        try:
            got = [httpx.post(base + URL, json=BODY).status_code for _ in range(n)]
            self.check("seed=42 sequential pattern matches contract", got == expected)
        finally:
            self.stop(proc)

    def bad_config(self):
        proc, _, err = self.start("badconfig", LLM_MOCK_DELAY_MS=999999)
        self.stop(proc)
        log = (self.log_dir / "server_badconfig.log").read_text(errors="replace")
        self.check("invalid config -> server refuses to start",
                   err is not None and proc.returncode not in (None, 0)
                   and "delay_ms must be between" in log, err or "server started unexpectedly")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--log-dir", type=Path)
    ap.add_argument("--json-out", type=Path)
    args = ap.parse_args()

    log_dir = args.log_dir or Path(tempfile.mkdtemp(prefix="smoke_"))
    log_dir.mkdir(parents=True, exist_ok=True)
    smoke = Smoke(args.project_root.resolve(), log_dir)
    print(f"python={sys.version.split()[0]} executable={sys.executable}")

    # Exceptions are recorded as results; earlier results are never discarded.
    try:
        smoke.basic()
        smoke.concurrency()
        smoke.errors()
        smoke.bad_config()
    except Unverified as e:
        smoke.results.append({"name": "environment", "status": "UNVERIFIED", "detail": str(e)})
    except Exception as e:  # a crash in the smoke run itself is a failed check, not a pass
        smoke.results.append({"name": "smoke script", "status": "FAIL",
                              "detail": f"{type(e).__name__}: {e}"})

    # Precedence: FAIL > UNVERIFIED > PASS (docs/quality-gates.md).
    statuses = [r["status"] for r in smoke.results]
    if "FAIL" in statuses:
        verdict, reason = "FAIL", "one or more checks failed"
    elif "UNVERIFIED" in statuses:
        verdict, reason = "UNVERIFIED", "environment prevented some checks"
    elif not statuses:
        verdict, reason = "FAIL", "no checks ran"
    else:
        verdict, reason = "PASS", ""

    passed = statuses.count("PASS")
    print(f"\nSMOKE {verdict}: {passed}/{len(statuses)} checks passed {reason}".rstrip())
    if args.json_out:
        try:
            args.json_out.write_text(json.dumps(
                {"verdict": verdict, "reason": reason, "checks": smoke.results}, indent=2))
        except OSError as e:
            print(f"cannot write {args.json_out}: {e}")
            if verdict == "PASS":
                verdict = "UNVERIFIED"  # no evidence saved -> cannot claim PASS
    sys.exit({"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}[verdict])


if __name__ == "__main__":
    main()
