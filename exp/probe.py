"""
Usage:
  python exp/probe.py --stage pilot     # 1 short call per model, then stop
  python exp/probe.py --stage full      # 7 short + 1 medium + 1 large per model
  python exp/probe.py --stage analyze   # read the log; no API calls
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
URL = "https://api.groq.com/openai/v1/chat/completions"
LOG = ROOT / "results" / "probe_log.jsonl"

# Lowest reasoning setting per model, from https://console.groq.com/docs/reasoning (2026-11-14).
MODELS = {"openai/gpt-oss-120b": "low", "openai/gpt-oss-20b": "low", "qwen/qwen3.8-27b": "none"}
MAX_TOKENS = 300
STAGE_MAX_TOKENS = {"tpm429": 8000,     # 1.2: prompt + 8000 > the whole 8K TPM bucket (was accepted: 200)
                    "maxtok2000": 2000}  # 1.2(a): does the TPM drop scale with max_tokens?
CALL_CAP = 50          # all stages combined, counted from the log
TOKEN_CAP = 20_000     # all stages combined, counted from the log
PAUSE_S = 8
STOP_STATUSES = {401, 403, 429}

now = time.perf_counter  # module-level so tests can substitute a fake clock

# Distinct questions so no call can hit a provider-side prompt cache.
PILOT_Q = "In one sentence, what does a hash table do?"
SHORT_QS = [
    "In one sentence, what is a binary search?",
    "In one sentence, what does TCP guarantee that UDP does not?",
    "In one sentence, what is a mutex?",
    "In one sentence, what is gradient descent?",
    "In one sentence, what does a load balancer do?",
    "In one sentence, what is a race condition?",
    "In one sentence, what is an index in a database?",
]


def passage_prompt(name, question):
    text = (ROOT / "data" / "probe" / f"{name}.txt").read_text()
    return f"Using only the passage below, answer in at most two sentences: {question}\n\nPassage:\n{text}"


def plan(stage):
    """List of (model, kind, prompt), interleaved round-robin across models."""
    if stage == "pilot":
        return [(m, "short", PILOT_Q) for m in MODELS]
    if stage in STAGE_MAX_TOKENS:
        return [("openai/gpt-oss-20b", stage, "Reply with the single word OK.")]
    rounds = [("short", q) for q in SHORT_QS] + [
        ("medium", passage_prompt("medium", "How many identical layers make up the encoder, and what are its two sub-layers?")),
        ("large", passage_prompt("large", "How many attention heads are used, and what are d_k and d_v?")),
    ]
    return [(m, kind, prompt) for kind, prompt in rounds for m in MODELS]


def worst_case_tokens(prompt, max_tokens=MAX_TOKENS):
    # Conservative: ~3 chars/token (English is ~4) + chat-template overhead + full max_tokens.
    # 80 overhead: pilot measured 82 prompt tokens on gpt-oss for a 43-char question (~60 template tokens).
    return len(prompt) // 3 + 80 + max_tokens


def prior_usage(log_path):
    """(calls, tokens) already spent by earlier runs, so the caps hold across stages."""
    if not log_path.exists():
        return 0, 0
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    return len(rows), sum(r["tokens_charged"] for r in rows)


def parse_stream(lines, t0, row):
    """Fill timing, text heads, finish_reason and usage from SSE lines."""
    content, reasoning = "", ""
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
            reasoning += r
            content += c
            if choice.get("finish_reason"):
                row["finish_reason"] = choice["finish_reason"]
    row["content_head"] = content[:200]
    row["reasoning_head"] = reasoning[:200]
    usage = row.get("usage") or {}
    row["prompt_tokens"] = usage.get("prompt_tokens")
    row["completion_tokens"] = usage.get("completion_tokens")
    row["reasoning_tokens"] = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")


def call_once(client, key, model, prompt, max_tokens=MAX_TOKENS):
    row = {"model": model, "reasoning_effort": MODELS[model], "max_tokens": max_tokens,
           "http_status": None, "finish_reason": None, "first_chunk_ms": None, "first_content_ms": None}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": max_tokens,
        "temperature": 0,
        "reasoning_effort": MODELS[model],
    }
    t0 = now()
    try:
        with client.stream("POST", URL, json=body, timeout=60,
                           headers={"Authorization": f"Bearer {key}"}) as r:
            row["http_status"] = r.status_code
            # all headers, incl. duplicates; cookie values are session data, never logged
            row["response_headers"] = [[k, "<redacted>" if k.lower() == "set-cookie" else v]
                                       for k, v in r.headers.multi_items()]
            if r.status_code == 200:
                parse_stream(r.iter_lines(), t0, row)
            else:
                row["error_body"] = r.read().decode(errors="replace")
    except httpx.HTTPError as e:
        row["error"] = type(e).__name__  # type only: message text is never logged
    row["total_ms"] = round((now() - t0) * 1000, 1)
    total = (row.get("usage") or {}).get("total_tokens")
    # No usage reported (error, cut stream) → charge the worst case so the cap stays safe.
    row["tokens_charged"] = total if total is not None else worst_case_tokens(prompt, max_tokens)
    return row


def run(stage, client, key, log_path=LOG, sleep=time.sleep, out=print):
    def say(msg):
        out(str(msg).replace(key, "***"))

    log_path.parent.mkdir(parents=True, exist_ok=True)
    calls, tokens = prior_usage(log_path)
    max_tokens = STAGE_MAX_TOKENS.get(stage, MAX_TOKENS)
    dead, rows = set(), []
    for model, kind, prompt in plan(stage):
        if model in dead:
            continue
        if calls >= CALL_CAP:
            say(f"STOP: call cap reached ({calls}/{CALL_CAP})")
            break
        worst = worst_case_tokens(prompt, max_tokens)
        if tokens + worst > TOKEN_CAP:
            say(f"STOP: token cap would be exceeded ({tokens} used + {worst} worst case > {TOKEN_CAP})")
            break
        if rows:
            sleep(PAUSE_S)
        ts = datetime.now(timezone.utc).isoformat()
        row = {"ts": ts, "stage": stage, "call_no": calls + 1, "prompt_kind": kind, **call_once(client, key, model, prompt, max_tokens)}
        calls += 1
        tokens += row["tokens_charged"]
        rows.append(row)
        with log_path.open("a") as f:
            f.write(json.dumps(row).replace(key, "***") + "\n")
            f.flush()
        status = row["http_status"]
        if status in STOP_STATUSES:
            say(f"STOP: HTTP {status} from {model}")
            break
        if status == 400:
            dead.add(model)
            say(f"DEAD: {model} returned 400, skipping it from now on")
    say(f"{'model':<22}{'kind':<8}{'status':<8}{'reason_tok':<12}{'compl_tok':<11}{'finish':<9}{'1st_chunk':<11}{'1st_content':<12}total_ms")
    for r in rows:
        say(f"{r['model']:<22}{r['prompt_kind']:<8}{str(r['http_status']):<8}{str(r.get('reasoning_tokens')):<12}"
            f"{str(r.get('completion_tokens')):<11}{str(r['finish_reason']):<9}{str(r['first_chunk_ms']):<11}"
            f"{str(r['first_content_ms']):<12}{r['total_ms']}")
    say(f"probe totals (all stages): {calls} calls, {tokens} tokens charged")
    return rows


def header(row, name):
    for k, v in row.get("response_headers") or []:
        if k.lower() == name:
            return v
    return None


def analyze(log_path=LOG, out=print):
    """Per model: drop in remaining-tokens (TPM) and remaining-requests (RPD) between consecutive calls."""
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    out(f"{'model':<22}{'call':<6}{'gap_s':<8}{'usage_total':<13}{'Δremaining_tok':<16}{'Δremaining_req':<16}max_tokens")
    prev = {}
    for r in rows:
        if r["http_status"] != 200:
            continue
        p = prev.get(r["model"])
        if p:
            gap = (datetime.fromisoformat(r["ts"]) - datetime.fromisoformat(p["ts"])).total_seconds()
            d_tok, d_req = (
                (int(a) - int(b)) if a is not None and b is not None else None
                for a, b in ((header(p, "x-ratelimit-remaining-tokens"), header(r, "x-ratelimit-remaining-tokens")),
                             (header(p, "x-ratelimit-remaining-requests"), header(r, "x-ratelimit-remaining-requests")))
            )
            out(f"{r['model']:<22}{r['call_no']:<6}{gap:<8.1f}{str((r.get('usage') or {}).get('total_tokens')):<13}"
                f"{str(d_tok):<16}{str(d_req):<16}{r['max_tokens']}")
        prev[r["model"]] = r
    out("Note: TPM refills during gap_s, so Δremaining_tok understates the charge; compare against usage_total.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["pilot", "full", "tpm429", "maxtok2000", "analyze"], required=True)
    args = ap.parse_args()
    if args.stage == "analyze":
        return analyze()
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set")
    try:
        with httpx.Client() as client:
            run(args.stage, client, key)
    except Exception as e:  # last resort: type only, message redacted
        sys.exit(f"probe crashed: {type(e).__name__}: {str(e).replace(key, '***')}")


if __name__ == "__main__":
    main()
