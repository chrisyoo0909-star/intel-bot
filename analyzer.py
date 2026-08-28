"""
LLM-powered conviction grading.

Talks to any OpenAI-compatible chat-completions endpoint. Defaults to Groq's
free API (https://console.groq.com/keys) — no billing, no credit card required.

Configure via the local .env:

    LLM_API_KEY          = <key>                          # or GROQ_API_KEY
    LLM_BASE_URL         = https://api.groq.com/openai/v1  # optional override
    LLM_MODEL            = openai/gpt-oss-20b              # optional override
    LLM_REASONING_EFFORT = low                            # low|medium|high; "off" omits it

Other zero-cost backends that work by changing only those vars:
    OpenRouter free tier -> https://openrouter.ai/api/v1   + a ":free" model id
    Cerebras             -> https://api.cerebras.ai/v1
    Local Ollama         -> http://localhost:11434/v1   (set LLM_REASONING_EFFORT=off)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


def _clean(value: str | None) -> str:
    return (value or "").strip().strip("'").strip('"').strip()


LLM_API_KEY = _clean(os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY"))
if not LLM_API_KEY:
    raise RuntimeError(
        "Set LLM_API_KEY (or GROQ_API_KEY) in the local .env file. "
        "Get a free key at https://console.groq.com/keys"
    )

LLM_BASE_URL = (_clean(os.environ.get("LLM_BASE_URL")) or "https://api.groq.com/openai/v1").rstrip("/")
LLM_MODEL = _clean(os.environ.get("LLM_MODEL")) or "openai/gpt-oss-20b"

# Reasoning models (gpt-oss, qwen3) otherwise emit thousands of hidden tokens
# per call and blow small free-tier per-minute token budgets. "low" keeps
# grading quality while cutting usage ~15x. Any non-empty value is sent as-is;
# the special value "off" omits the field for backends that reject it.
_re_env = _clean(os.environ.get("LLM_REASONING_EFFORT")) or "low"
LLM_REASONING_EFFORT = "" if _re_env.lower() == "off" else _re_env

_ENDPOINT = f"{LLM_BASE_URL}/chat/completions"
_TIMEOUT = 45
_MAX_RETRY_AFTER = 30  # seconds; longer waits are treated as daily exhaustion
_MAX_ATTEMPTS = 3
_SNIPPET_CHARS = 2000


class LLMQuotaError(RuntimeError):
    """Raised when the LLM API reports quota/rate-limit exhaustion (HTTP 429)."""


class _AuthError(RuntimeError):
    """Internal: 401/403 from the LLM API."""


_SYSTEM_INSTRUCTION = (
    "You are a Senior Wall Street Infrastructure Analyst. You evaluate news and "
    "regulatory filings for strategic leverage shifts, hard physical CapEx "
    "commitments, and supply-chain chokepoints across advanced compute, "
    "hyperscale cloud, critical energy, critical minerals, and physical AI / "
    "robotics.\n\n"
    "Grade each item strictly from 1 to 10 on the 'conviction_score' scale:\n"
    "  1-4  = noise, recycled commentary, price-target chatter, PR fluff.\n"
    "  5-7  = mildly relevant but not decision-grade.\n"
    "  8    = a concrete, material infrastructure or supply-chain development.\n"
    "  9-10 = a decisive strategic shift with quantified capital or capacity impact.\n"
    "Any item that is filler, speculative, or lacks a concrete physical / capital "
    "signal MUST score below 8 and is considered invalid.\n\n"
    "Respond with ONLY a JSON object with exactly these keys: "
    "\"conviction_score\" (integer 1-10), \"headline\" (a crisp Bloomberg/Reuters-"
    "style headline string), \"analysis\" (a concise two-sentence Wall Street "
    "commentary string)."
)


def _fallback(reason: str, kind: str = "other") -> dict[str, Any]:
    return {
        "conviction_score": 1,
        "headline": "",
        "analysis": "",
        "valid": False,
        "error": reason,
        "error_kind": kind,  # auth | quota | parse | network | other
    }


def _looks_like_quota(text: str) -> bool:
    t = (text or "").lower()
    return any(
        s in t
        for s in ("429", "resource_exhausted", "quota", "rate limit", "rate_limit")
    )


def _looks_like_auth(text: str) -> bool:
    t = (text or "").lower()
    return any(
        s in t
        for s in (
            "invalid api key",
            "api key not valid",
            "invalid_api_key",
            "unauthenticated",
            "unauthorized",
            "permission_denied",
            "401",
            "403",
        )
    )


def _mentions_daily(text: str) -> bool:
    t = (text or "").lower()
    return "per day" in t or "daily" in t or "rpd" in t or "tpd" in t


_RETRY_DELAY_RE = re.compile(r"try again in ([\d.]+)\s*(m|min|s|ms)?", re.I)


def _retry_delay(resp: requests.Response, body: str) -> float:
    """Seconds to wait before retrying a 429 — from the header or the body text."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = _RETRY_DELAY_RE.search(body or "")
    if not m:
        return 0.0
    value = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit in ("m", "min"):
        return value * 60
    if unit == "ms":
        return value / 1000
    return value


def _post(payload: dict[str, Any]) -> requests.Response:
    return requests.post(
        _ENDPOINT,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=_TIMEOUT,
    )


def _chat(prompt: str) -> str:
    """
    One chat completion. Returns the raw message content (expected to be JSON).

    Retries a short per-minute 429 up to _MAX_ATTEMPTS times, sleeping the
    delay the API asks for. Raises LLMQuotaError only when the limit is a daily
    cap, the wait exceeds _MAX_RETRY_AFTER, or the retries are exhausted.
    """
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
    }
    if LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = LLM_REASONING_EFFORT

    resp = _post(payload)
    for attempt in range(1, _MAX_ATTEMPTS):
        if resp.status_code != 429:
            break
        body = resp.text[:500]
        delay = _retry_delay(resp, body)
        if _mentions_daily(body) or not delay or delay > _MAX_RETRY_AFTER:
            raise LLMQuotaError(body)
        time.sleep(delay + 1.0)
        resp = _post(payload)
    if resp.status_code == 429:
        raise LLMQuotaError(resp.text[:500])

    if resp.status_code in (401, 403):
        raise _AuthError(resp.text[:300])

    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        body = resp.text or str(exc)
        if _looks_like_quota(body):
            raise LLMQuotaError(body[:300]) from exc
        raise

    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _parse_verdict(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    # Some models wrap JSON in ```json fences.
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:] if raw.lower().startswith("json") else raw
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _fallback(f"unparseable response: {raw[:200]}", kind="parse")
    if not isinstance(data, dict):
        return _fallback(f"non-object response: {raw[:200]}", kind="parse")

    try:
        score = int(data.get("conviction_score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(1, min(10, score))

    headline = str(data.get("headline", "")).strip()
    analysis = str(data.get("analysis", "")).strip()

    return {
        "conviction_score": score,
        "headline": headline,
        "analysis": analysis,
        "valid": score >= 8 and bool(headline) and bool(analysis),
    }


def evaluate_item(
    company_name: str,
    domain: str,
    text_snippet: str,
    source_url: str,
) -> dict[str, Any]:
    """
    Grade a single raw item with the configured LLM.

    Returns a dict with 'conviction_score' (1-10), 'headline', 'analysis', and
    'valid' (True only when score >= 8). On a handled failure the same shape is
    returned with 'error' and 'error_kind' set and 'valid' False.

    Raises:
        LLMQuotaError: when the API quota/rate limit is exhausted, so the caller
            can stop the run cleanly instead of burning the whole batch.
    """
    snippet = (text_snippet or "").strip()
    if len(snippet) < 20:
        return _fallback("snippet too short", kind="parse")

    prompt = (
        f"COMPANY: {company_name}\n"
        f"DOMAIN: {domain}\n"
        f"SOURCE URL: {source_url}\n"
        f"RAW ITEM:\n\"\"\"\n{snippet[:_SNIPPET_CHARS]}\n\"\"\"\n\n"
        "Evaluate this item now and return the JSON object."
    )

    try:
        content = _chat(prompt)
    except LLMQuotaError:
        raise
    except _AuthError as exc:
        return _fallback(f"llm auth failed: {exc}", kind="auth")
    except requests.HTTPError as exc:
        body = getattr(exc.response, "text", "") or str(exc)
        kind = "auth" if _looks_like_auth(body) else "network"
        return _fallback(f"llm http error: {body[:200]}", kind=kind)
    except requests.RequestException as exc:
        return _fallback(f"llm request failed: {exc}", kind="network")
    except (KeyError, IndexError, ValueError) as exc:
        return _fallback(f"malformed llm response: {exc}", kind="parse")

    return _parse_verdict(content)
