"""One streaming chat call to an OpenAI-compatible endpoint (Groq, later the stand-in). No retries.
Same parsing/redaction approach as exp/probe.py (copied, not imported: the probe is a frozen experiment)."""
import json
import time

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
STOP_STATUSES = {401, 403, 429}
now = time.perf_counter  # module-level so tests can substitute a clock


class Stopped(Exception):
    """401/403/429: stop the whole run (Phase 4 has no retries)."""


def chat(client, key, model, messages, max_tokens, url=GROQ_URL, reasoning_effort="low"):
    """→ row dict: http_status, text, reasoning, usage, finish_reason, first_chunk_ms, first_content_ms, total_ms,
    response_headers (set-cookie redacted), error_body / error. Never contains the key (caller redacts logs too)."""
    row = {"model": model, "max_tokens": max_tokens, "http_status": None, "text": "", "reasoning": "", "usage": None,
           "finish_reason": None, "first_chunk_ms": None, "first_content_ms": None}
    body = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True},
            "max_completion_tokens": max_tokens, "temperature": 0, "reasoning_effort": reasoning_effort}
    t0 = now()
    try:
        with client.stream("POST", url, json=body, timeout=60, headers={"Authorization": f"Bearer {key}"}) as r:
            row["http_status"] = r.status_code
            row["response_headers"] = [[k, "<redacted>" if k.lower() == "set-cookie" else v] for k, v in r.headers.multi_items()]
            if r.status_code == 200:
                _parse(r.iter_lines(), t0, row)
            else:
                row["error_body"] = r.read().decode(errors="replace")
    except httpx.HTTPError as e:
        row["error"] = type(e).__name__  # type only, never the message
    row["total_ms"] = round((now() - t0) * 1000, 1)
    return row


def _parse(lines, t0, row):
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if "error" in chunk:
            row["stream_error"] = chunk["error"]
        usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
        if usage:
            row["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            r, c = delta.get("reasoning") or "", delta.get("content") or ""
            t = round((now() - t0) * 1000, 1)
            if (r or c) and row["first_chunk_ms"] is None:
                row["first_chunk_ms"] = t
            if c and row["first_content_ms"] is None:
                row["first_content_ms"] = t
            row["reasoning"] += r
            row["text"] += c
            if choice.get("finish_reason"):
                row["finish_reason"] = choice["finish_reason"]
