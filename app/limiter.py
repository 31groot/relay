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


# ---------------------------------------------------------------------------------------------------------------
# Phase 6: async limiter for concurrent load (the pacer above stays for the sequential Phase 4/5 runs).

import asyncio  # noqa: E402
import heapq  # noqa: E402
import itertools  # noqa: E402


class SimClock:
    """Simulated seconds = wall seconds × speed (load runs compress time; speed=1 is real time)."""

    def __init__(self, speed=1.0):
        self.speed = speed

    def now(self):
        return time.monotonic() * self.speed

    async def sleep(self, sim_seconds):
        await asyncio.sleep(max(0.0, sim_seconds) / self.speed)


class Shed(Exception):
    """Refused early: the expected wait for capacity exceeds the request's remaining deadline."""


class Reservation:
    __slots__ = ("model", "need", "sent", "settled")

    def __init__(self, model, need):
        self.model, self.need, self.sent, self.settled = model, need, False, False


class Limiter:
    """Per-model TPM bucket (limit/60 tokens per simulated second, continuous refill, Phase 1 measurement).
    acquire(): reserve `need` (prompt + max_tokens + margin) only when the bucket covers it — the bucket never goes
    negative. Admission is one-at-a-time in (priority, arrival) order: lower priority number first, so requests already
    in progress (judge, correction) overtake new ones. settle(): refund the unused part, re-sync from the response
    headers. cancel(): an unsent reservation is fully refunded; a sent one keeps only its prompt estimate.
    """

    def __init__(self, clock=None, limit=DEFAULT_LIMIT):
        self.clock = clock or SimClock()
        self.default_limit = limit
        self.level, self.limit, self.at = {}, {}, {}
        self.queue, self.seq = {}, itertools.count()
        self.outstanding = {}          # model -> tokens reserved and not yet settled
        self.max_waiters = 0
        self.cond = asyncio.Condition()

    def _refill(self, model):
        lim = self.limit.setdefault(model, self.default_limit)
        now = self.clock.now()
        level = self.level.get(model, lim)
        self.level[model] = min(lim, level + (now - self.at.get(model, now)) * lim / 60)
        self.at[model] = now
        return self.level[model]

    def waiters(self, model=None):
        return sum(len(q) for m, q in self.queue.items() if model in (None, m))

    async def acquire(self, model, need, deadline=None, priority=1):
        """→ Reservation. Raises Shed if the wait would pass `deadline` (simulated seconds, clock.now() scale)."""
        async with self.cond:
            lim = self.limit.setdefault(model, self.default_limit)
            need = min(need, lim)              # a request larger than the bucket waits for a full bucket
            entry = (priority, next(self.seq))
            q = self.queue.setdefault(model, [])
            heapq.heappush(q, entry)
            self.max_waiters = max(self.max_waiters, self.waiters())
            admitted = False
            try:
                while True:
                    level = self._refill(model)
                    if q[0] == entry and level >= need:
                        heapq.heappop(q)
                        self.level[model] = level - need
                        self.outstanding[model] = self.outstanding.get(model, 0) + need
                        admitted = True
                        self.cond.notify_all()
                        return Reservation(model, need)
                    rate = self.limit[model] / 60
                    wait = (need - level) / rate if q[0] == entry else 0.05 * 60 / rate  # head waits for refill
                    if deadline is not None and self.clock.now() + max(wait, 0) > deadline:
                        raise Shed(f"{model}: needs {need}, has {level:.0f}; wait {wait:.1f}s passes the deadline")
                    try:
                        await asyncio.wait_for(self.cond.wait(), timeout=max(wait, 0.001) / self.clock.speed)
                    except asyncio.TimeoutError:
                        pass
            finally:
                if not admitted:               # shed, cancelled or errored while waiting: leave the queue, no reservation
                    q.remove(entry)
                    heapq.heapify(q)
                    self.cond.notify_all()

    def _release(self, res, keep):
        if res.settled:
            return
        res.settled = True
        self._refill(res.model)
        self.outstanding[res.model] -= res.need
        self.level[res.model] = min(self.limit[res.model], self.level[res.model] + max(0, res.need - keep))

    def settle(self, res, actual_tokens, headers=None):
        """Refund need − actual (actual None → keep the whole reservation), then sync from x-ratelimit-* headers."""
        self._release(res, res.need if actual_tokens is None else actual_tokens)
        h = {k.lower(): v for k, v in headers or []}
        if "x-ratelimit-limit-tokens" in h:
            self.limit[res.model] = int(h["x-ratelimit-limit-tokens"])
        if "x-ratelimit-remaining-tokens" in h:     # the provider's own view; never trust more than it shows
            self._refill(res.model)
            self.level[res.model] = min(self.level[res.model], float(h["x-ratelimit-remaining-tokens"]))
        self._notify()

    def cancel(self, res, prompt_estimate=0):
        """Cancelled after admission: unsent → full refund; sent (provider may have charged) → keep the prompt estimate."""
        self._release(res, prompt_estimate if res.sent else 0)
        self._notify()

    def _notify(self):
        async def _n():
            async with self.cond:
                self.cond.notify_all()
        try:
            asyncio.get_running_loop().create_task(_n())
        except RuntimeError:
            pass
