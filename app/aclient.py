"""Async model call through the limiter, with the agreed retry policy (Phase 6).

Retry policy: retry only 429 (per-minute), 5xx and transport errors; never a per-day 429, never 4xx otherwise.
Wait = max(retry-after, backoff) for 429; backoff = base × 2^n × jitter(0.5–1.5) for 5xx/transport. At most MAX_RETRIES
per call, only while a GLOBAL retry budget allows (≤ RETRY_RATIO of calls, + a small burst), and only if the wait fits
the request's deadline. Every retry re-enters the limiter (it waits its turn like a new call).
"""
import asyncio
import random
import re

import limiter as lim
import llm

MAX_RETRIES = 2
BACKOFF_BASE_S = 1.0
RETRY_RATIO, RETRY_BURST = 0.10, 5
PACING_MARGIN = 100


class RetryBudget:
    """Global: retries allowed ≤ RETRY_RATIO × calls + RETRY_BURST (stops retry storms under overload)."""

    def __init__(self, ratio=RETRY_RATIO, burst=RETRY_BURST):
        self.ratio, self.burst, self.calls, self.retries = ratio, burst, 0, 0

    def allow(self):
        return self.retries < self.ratio * self.calls + self.burst


def is_daily(row):
    return row.get("http_status") == 429 and bool(re.search(r"per day", row.get("error_body") or "", re.I))


def header(row, name):
    return next((v for k, v in row.get("response_headers") or [] if k.lower() == name), None)


async def call(client, key, url, limiter, budget, model, messages, max_tokens, worst, deadline=None, priority=1,
               rng=random, stats=None):
    """→ (row, retries). Raises lim.Shed when capacity cannot arrive before the deadline."""
    clock = limiter.clock
    retries, attempt = 0, 0
    while True:
        budget.calls += 1
        res = await limiter.acquire(model, worst + PACING_MARGIN, deadline=deadline, priority=priority)
        res.sent = True
        try:
            row = await llm.achat(client, key, model, messages, max_tokens, url=url)
        except asyncio.CancelledError:
            limiter.cancel(res, prompt_estimate=worst - max_tokens)   # sent: the provider may have charged the prompt
            raise
        status = row["http_status"]
        actual = (row.get("usage") or {}).get("total_tokens")
        limiter.settle(res, actual if status == 200 else 0, row.get("response_headers"))
        if status == 200 or attempt >= MAX_RETRIES or is_daily(row):
            return row, retries
        retryable = status == 429 or status is None or (status is not None and status >= 500)
        if not retryable or not budget.allow():
            return row, retries
        wait = BACKOFF_BASE_S * (2 ** attempt) * rng.uniform(0.5, 1.5)
        if status == 429:
            wait = max(wait, float(header(row, "retry-after") or 0))
        if deadline is not None and clock.now() + wait > deadline:
            return row, retries                                       # retrying could not finish in time
        budget.retries += 1
        retries += 1
        attempt += 1
        if stats is not None:
            stats["retry_wait_s"] = stats.get("retry_wait_s", 0) + wait
        await clock.sleep(wait)
