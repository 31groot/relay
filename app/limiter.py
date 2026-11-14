"""Per-model tokens-per-minute pacing from Groq's response headers (Phase 4; grows into the Phase 6 limiter).

Groq's TPM bucket refills linearly at limit/60 tokens/s (Phase 1: reset-tokens = charged / 133.3).
Before a call: wait until (last x-ratelimit-remaining-tokens + refill since then) covers this call's
prompt + max_tokens + margin, then reserve that amount locally. After the call, re-sync from the headers.
Unknown state = EMPTY bucket at start-up: costs one refill wait, but is safe after any earlier activity.
"""
import time

DEFAULT_LIMIT = 8000   # free-plan TPM for all three models (console, 2026-11-14); headers override


class Pacer:
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.t0 = clock()
        self.state = {}  # model -> (remaining_tokens, at_time, limit)

    def available(self, model):
        remaining, at, limit = self.state.get(model, (0, self.t0, DEFAULT_LIMIT))
        return min(limit, remaining + (self.clock() - at) * limit / 60)

    def wait(self, model, need):
        """Block until `need` tokens fit, reserve them. → seconds waited."""
        limit = self.state.get(model, (0, 0, DEFAULT_LIMIT))[2]
        need = min(need, limit)  # a call larger than the whole bucket can only wait for a full bucket
        short = need - self.available(model)
        waited = 0.0
        if short > 0:
            waited = short * 60 / limit
            self.sleep(waited)
        self.state[model] = (self.available(model) - need, self.clock(), limit)  # reserve at admission
        return round(waited, 3)

    def update(self, model, headers):
        """Re-sync from x-ratelimit-*-tokens (Groq's own view) when the response carries them."""
        h = {k.lower(): v for k, v in headers or []}
        if "x-ratelimit-remaining-tokens" in h:
            limit = int(h.get("x-ratelimit-limit-tokens", DEFAULT_LIMIT))
            self.state[model] = (int(h["x-ratelimit-remaining-tokens"]), self.clock(), limit)
