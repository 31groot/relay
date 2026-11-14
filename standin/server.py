"""Stand-in model service: OpenAI-compatible POST /openai/v1/chat/completions with SSE, calibrated from real Groq logs.

  python standin/server.py --port 8099 [--speed 20] [--tpm 8000] [--rpm 30] [--rpd 1000] [--tpd 200000]
                           [--tpd-remaining N] [--alpha 0.05] [--p5xx 0] [--pslow 0] [--seed 1]

Behaviour (assumptions listed in NOTES.md, Phase 6):
- Latency: (first_content_ms, total_ms) resampled from the 131 real 200-responses (probe + Phase 4 + Phase 5 logs) of
  the same model and prompt-size bucket (<300, 300–1300, ≥1300 prompt tokens), × (1 + alpha × (in_flight − 1)).
- Limits per model: TPM bucket refilling continuously at limit/60 per simulated second; admission reserves
  prompt + max_tokens (capped at the limit) and settles to actual usage on completion; RPM over a sliding 60 s;
  RPD and TPD daily counters. Over a limit → 429 shaped like the real one (body, retry-after, x-ratelimit-*), ~75 ms.
- Content: replays the real answers/verdicts recorded for the 17 answer questions (default setting); unknown
  question → generator "NOT_IN_PAPERS", judge "supported".
- Failures: --p5xx returns 500; --pslow multiplies latency by 10 (slow success). Both default 0 (none observed).
- Time: everything runs on simulated seconds = wall seconds × --speed; headers and sleeps use simulated seconds.
"""
import argparse
import asyncio
import collections
import json
import math
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL_LOGS = [ROOT / "results" / f for f in ("probe_log.jsonl", "phase4_log.jsonl", "phase5_log.jsonl")]
CHARS_PER_TOKEN, TEMPLATE_OVERHEAD = 3.4, 80   # measured on logged pipeline prompts (Phase 4)
GEN_MODEL, JUDGE_MODEL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"


def bucket(prompt_tokens):
    return 0 if prompt_tokens < 300 else (1 if prompt_tokens < 1300 else 2)


def load_latency(paths=REAL_LOGS):
    """{(model, bucket): [(first_content_ms, total_ms)]} from real 200 responses."""
    out = collections.defaultdict(list)
    for path in paths:
        if not path.exists():
            continue
        for r in map(json.loads, path.read_text().splitlines()):
            if r.get("type", "call") == "call" and r.get("http_status") == 200 and r.get("usage") and r.get("first_content_ms"):
                out[(r["model"], bucket(r["usage"]["prompt_tokens"]))].append((r["first_content_ms"], r["total_ms"]))
    return out


def load_replay(path=ROOT / "results" / "phase5_log.jsonl"):
    """question → {"gen": [raw outputs per attempt], "judge": {normalized answer: verdict json}, "completion": [...]}"""
    replay = {}
    if not path.exists():
        return replay
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    latest = {r["request_id"]: r for r in rows if r["type"] == "request" and r["request_id"].startswith("default:")}
    comp = collections.defaultdict(list)
    for r in rows:
        if r["type"] == "call" and r["request_id"].startswith("default:") and r["step"].startswith("generate") and r.get("usage"):
            comp[r["request_id"]].append(r["usage"]["completion_tokens"])
    for rid, req in latest.items():
        e = replay.setdefault(req["question"], {"gen": [], "judge": {}, "completion": comp[rid]})
        for a in req["attempts"]:
            e["gen"].append(a["raw_output"])
            if "judge" in a and a["judge"]["verdict"] in ("supported", "unsupported"):
                j = a["judge"]
                e["judge"][a["normalized_output"]] = json.dumps({"verdict": j["verdict"], "claim": j["claim"], "reason": j["reason"]})
    return replay


class Model:
    def __init__(self, tpm, rpm, rpd, tpd, tpd_remaining):
        self.tpm, self.rpm, self.rpd, self.tpd = tpm, rpm, rpd, tpd
        self.level, self.at = float(tpm), None
        self.minute = collections.deque()
        self.req_left, self.tok_left = rpd, tpd if tpd_remaining is None else tpd_remaining
        self.in_flight = 0

    def refill(self, now):
        if self.at is not None:
            self.level = min(self.tpm, self.level + (now - self.at) * self.tpm / 60)
        self.at = now
        while self.minute and now - self.minute[0] >= 60:
            self.minute.popleft()


class StandIn:
    def __init__(self, speed=1.0, tpm=8000, rpm=30, rpd=1000, tpd=200000, tpd_remaining=None, alpha=0.05, p5xx=0.0,
                 pslow=0.0, seed=1, latency=None, replay=None):
        self.speed, self.alpha, self.p5xx, self.pslow = speed, alpha, p5xx, pslow
        self.rng = random.Random(seed)
        self.latency = load_latency() if latency is None else latency
        self.replay = load_replay() if replay is None else replay
        self.models = collections.defaultdict(lambda: Model(tpm, rpm, rpd, tpd, tpd_remaining))
        self.log = []  # (sim_time, model, status, prompt_tokens, completion_tokens)

    def now(self):
        return time.monotonic() * self.speed

    async def sleep(self, sim_s):
        await asyncio.sleep(max(0.0, sim_s) / self.speed)

    # ---- content -------------------------------------------------------------------------------------------
    def content(self, model, messages):
        user = messages[-1]["content"] if messages else ""
        if model == JUDGE_MODEL or "Cited sources:" in user:
            q = re.search(r"Question: (.*?)\n\nAnswer:\n(.*?)\n\nCited sources:", user, re.S)
            e = self.replay.get(q.group(1)) if q else None
            verdict = e["judge"].get(q.group(2).strip()) if e else None
            return verdict or json.dumps({"verdict": "supported", "claim": "", "reason": "stand-in default verdict"}), 90
        q = re.search(r"Question: (.*)$", messages[1]["content"], re.S) if len(messages) > 1 else None
        e = self.replay.get(q.group(1).strip()) if q else None
        attempt = sum(m["role"] == "assistant" for m in messages)          # correction turn → attempt 2
        if not e or not e["gen"]:
            return "NOT_IN_PAPERS", 30
        i = min(attempt, len(e["gen"]) - 1)
        comp = e["completion"][i] if i < len(e["completion"]) else int(len(e["gen"][i]) / CHARS_PER_TOKEN)
        return e["gen"][i], comp

    def sample_latency(self, model, prompt_tokens, in_flight):
        pool = self.latency.get((model, bucket(prompt_tokens))) or [x for (m, _), v in self.latency.items() if m == model for x in v] \
            or [x for v in self.latency.values() for x in v] or [(600.0, 700.0)]
        first, total = self.rng.choice(pool)
        k = (1 + self.alpha * max(0, in_flight - 1)) * (10 if self.rng.random() < self.pslow else 1)
        return first * k / 1000, max(total, first) * k / 1000           # simulated seconds

    # ---- limits --------------------------------------------------------------------------------------------
    def rate_headers(self, m):
        return [("x-ratelimit-limit-requests", str(m.rpd)), ("x-ratelimit-limit-tokens", str(m.tpm)),
                ("x-ratelimit-remaining-requests", str(max(0, m.req_left))), ("x-ratelimit-remaining-tokens", str(max(0, int(m.level)))),
                ("x-ratelimit-reset-requests", f"{86400 / m.rpd:.1f}s"), ("x-ratelimit-reset-tokens", f"{(m.tpm - m.level) * 60 / m.tpm:.3f}s")]

    def admit(self, model, prompt_tokens, max_tokens):
        """→ (None, reserved) if admitted, else (429 body, headers)."""
        m = self.models[model]
        now = self.now()
        m.refill(now)
        requested = min(prompt_tokens + max_tokens, m.tpm)
        limit_kind = None
        if m.tok_left <= 0 or m.req_left <= 0:
            kind, lim, used = ("tokens per day (TPD)", m.tpd, m.tpd - m.tok_left) if m.tok_left <= 0 else ("requests per day (RPD)", m.rpd, m.rpd)
            wait, limit_kind = 3600.0, kind
        elif len(m.minute) >= m.rpm:
            kind, lim, used, wait = "requests per minute (RPM)", m.rpm, len(m.minute), 60 - (now - m.minute[0])
            limit_kind = kind
        elif requested > m.level:
            kind, lim, used, wait = "tokens per minute (TPM)", m.tpm, int(m.tpm - m.level), (requested - m.level) * 60 / m.tpm
            limit_kind = kind
        if limit_kind:
            body = {"error": {"message": f"Rate limit reached for model `{model}` in organization `org_<redacted>` service tier "
                                         f"`on_demand` on {kind}: Limit {lim}, Used {used}, Requested {requested}. "
                                         f"Please try again in {wait:.4f}s.",
                              "type": "tokens" if "token" in kind else "requests", "code": "rate_limit_exceeded"}}
            return body, [("retry-after", str(math.ceil(wait)))] + self.rate_headers(m)
        m.level -= requested
        m.minute.append(now)
        m.req_left -= 1
        return None, requested

    def settle(self, model, reserved, actual):
        m = self.models[model]
        m.refill(self.now())
        m.level = min(m.tpm, m.level + max(0, reserved - actual))
        m.tok_left -= actual

    # ---- HTTP ------------------------------------------------------------------------------------------------
    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            length = next((int(h.split(":", 1)[1]) for h in lines if h.lower().startswith("content-length:")), 0)
            body = json.loads(await reader.readexactly(length)) if length else {}
            await self.respond(body, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def respond(self, body, writer):
        model, messages = body.get("model", ""), body.get("messages", [])
        max_tokens = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1024)
        prompt = int(sum(len(m.get("content", "")) for m in messages) / CHARS_PER_TOKEN) + TEMPLATE_OVERHEAD
        err, reserved_or_headers = self.admit(model, prompt, max_tokens)
        if err:
            await self.sleep(0.075)                                    # real 429 came back in ~75 ms
            self.log.append((self.now(), model, 429, prompt, 0))
            return self.send_json(writer, 429, err, reserved_or_headers)
        reserved, m = reserved_or_headers, self.models[model]
        m.in_flight += 1
        try:
            text, completion = self.content(model, messages)
            first, total = self.sample_latency(model, prompt, m.in_flight)
            if self.rng.random() < self.p5xx:
                await self.sleep(first)
                self.settle(model, reserved, 0)
                self.log.append((self.now(), model, 500, prompt, 0))
                return self.send_json(writer, 500, {"error": {"message": "stand-in injected server error", "type": "server_error"}}, [])
            headers = [("content-type", "text/event-stream"), ("transfer-encoding", "chunked")] + self.rate_headers(m)
            writer.write(("HTTP/1.1 200 OK\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "connection: close\r\n\r\n").encode())
            await self.sleep(first)
            pieces = [text[i:i + max(1, len(text) // 6)] for i in range(0, len(text), max(1, len(text) // 6))] or [""]
            gap = max(0.0, total - first) / len(pieces)
            for i, piece in enumerate(pieces):
                finish = "stop" if i == len(pieces) - 1 else None
                self.chunk(writer, {"choices": [{"delta": {"content": piece}, "finish_reason": finish}]})
                await writer.drain()
                if gap and i < len(pieces) - 1:
                    await self.sleep(gap)
            usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
                     "queue_time": 0.0, "completion_tokens_details": {"reasoning_tokens": 0}}
            self.chunk(writer, {"choices": [], "usage": usage})
            self.chunk_raw(writer, "data: [DONE]\n\n")
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            self.settle(model, reserved, prompt + completion)
            self.log.append((self.now(), model, 200, prompt, completion))
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            self.settle(model, reserved, prompt)                          # client went away: charge the prompt only
            self.log.append((self.now(), model, 499, prompt, 0))
            raise
        finally:
            m.in_flight -= 1

    @staticmethod
    def chunk_raw(writer, text):
        data = text.encode()
        writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

    def chunk(self, writer, obj):
        self.chunk_raw(writer, f"data: {json.dumps(obj)}\n\n")

    @staticmethod
    def send_json(writer, status, obj, headers):
        data = json.dumps(obj).encode()
        reason = {429: "Too Many Requests", 500: "Internal Server Error"}[status]
        hdr = [("content-type", "application/json"), ("content-length", str(len(data)))] + list(headers)
        writer.write((f"HTTP/1.1 {status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr) + "connection: close\r\n\r\n").encode() + data)

    async def serve(self, host="127.0.0.1", port=8099):
        return await asyncio.start_server(self.handle, host, port)


def main(argv):
    ap = argparse.ArgumentParser()
    for name, typ, default in (("port", int, 8099), ("speed", float, 1.0), ("tpm", int, 8000), ("rpm", int, 30), ("rpd", int, 1000),
                               ("tpd", int, 200000), ("tpd-remaining", int, None), ("alpha", float, 0.05), ("p5xx", float, 0.0),
                               ("pslow", float, 0.0), ("seed", int, 1)):
        ap.add_argument(f"--{name}", type=typ, default=default)
    a = ap.parse_args(argv)
    s = StandIn(a.speed, a.tpm, a.rpm, a.rpd, a.tpd, a.tpd_remaining, a.alpha, a.p5xx, a.pslow, a.seed)

    async def run():
        server = await s.serve(port=a.port)
        print(f"stand-in on :{a.port} speed {a.speed} tpm {a.tpm} rpm {a.rpm} tpd_remaining {a.tpd_remaining}", flush=True)
        async with server:
            await server.serve_forever()
    asyncio.run(run())


if __name__ == "__main__":
    main(sys.argv[1:])
