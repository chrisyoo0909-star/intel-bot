"""
LLM-powered conviction grading.

Talks to any OpenAI-compatible chat-completions endpoint. Defaults to Groq's
free API (https://console.groq.com/keys) — no billing, no credit card required.

Configure via the local .env:

    LLM_API_KEY          = <key>                          # or GROQ_API_KEY
    LLM_BASE_URL         = https://api.groq.com/openai/v1  # optional override
    LLM_MODEL            = qwen/qwen3.8-27b                # optional override
    LLM_REASONING_EFFORT = none                           # none|low|medium|high; "off" omits it

qwen3.8-27b with reasoning disabled is ~600 tokens/call and fits Groq's free
8k TPM budget. groq/compound-mini has a 70k TPM budget if you need headroom.

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
LLM_MODEL = _clean(os.environ.get("LLM_MODEL")) or "qwen/qwen3.8-27b"

# Reasoning models otherwise emit thousands of hidden tokens per call and blow
# small free-tier per-minute token budgets. "none" disables it (qwen3); use
# "low" for gpt-oss. Any non-empty value is sent as-is; "off" omits the field
# for backends that reject it.
_re_env = _clean(os.environ.get("LLM_REASONING_EFFORT")) or "none"
LLM_REASONING_EFFORT = "" if _re_env.lower() == "off" else _re_env

_ENDPOINT = f"{LLM_BASE_URL}/chat/completions"
_TIMEOUT = 60
_MAX_RETRY_AFTER = 30  # seconds; longer waits are treated as daily exhaustion
_MAX_ATTEMPTS = 4
_SNIPPET_CHARS = 2600


class LLMQuotaError(RuntimeError):
    """Raised when the LLM API reports quota/rate-limit exhaustion (HTTP 429)."""


class _AuthError(RuntimeError):
    """Internal: 401/403 from the LLM API."""


_SYSTEM_INSTRUCTION = (
    "You are a Senior Supply-Chain Equity Analyst on a Wall Street "
    "infrastructure desk. You read full news articles and regulatory filings "
    "and isolate SUPPLY-SIDE catalysts: capacity additions, lead-time shifts, "
    "physical CapEx deployments, long-term off-take / take-or-pay agreements, "
    "power PPAs, and manufacturing bottlenecks — across advanced compute, "
    "hyperscale cloud, critical energy, critical minerals, and physical AI / "
    "robotics.\n\n"
    "METHOD:\n"
    "1. Judge the article ONLY on what its text substantiates — specific dollar "
    "amounts, capacity figures (GW, tons, wafers, units), named facilities, "
    "counterparties, and timelines. A strong headline with no supporting body "
    "is noise.\n"
    "2. Project the estimated financial impact: shift in revenue growth rate, "
    "gross/EBITDA margin expansion in basis points, and/or backlog growth.\n"
    "3. Derive a 12-month price-target delta as a percent, from plausible "
    "EV/EBITDA or P/E multiple change plus the earnings impact. If a current "
    "share price appears in the text, also compute implied_price_target = "
    "current_price * (1 + price_target_delta_pct/100); otherwise set it null.\n\n"
    "conviction_score (1-10), strict:\n"
    "  1-4  = noise: opinion/analysis columns, analyst price-target or rating "
    "changes, stock-move recaps, rumor/'could'/'may' pieces, listicles, "
    "specifics-free PR.\n"
    "  5-7  = a real but not decision-grade supply development (incremental, "
    "vague, small or unquantified).\n"
    "  8    = a concrete, committed capacity / supply-chain catalyst specific "
    "to THIS company.\n"
    "  9-10 = a decisive supply-side shift with quantified capital or capacity "
    "and named assets / counterparties.\n"
    "If the article is primarily about a different company, cap at 5. "
    "Speculation or no concrete physical/capital fact => below 8.\n\n"
    "recommendation must be one of: \"HIGH CONVICTION BUY\", \"BUY\", "
    "\"NEUTRAL / WATCH\", \"AVOID\".\n"
    "supply_chain_driver: a short phrase, ideally one of \"CapEx Expansion\", "
    "\"Bottleneck Relief\", \"Power PPA\", \"Raw Material Off-take\", "
    "\"Capacity Addition\", \"Lead-Time Shift\" (or a close variant).\n\n"
    "Respond with ONLY a JSON object with EXACTLY these keys:\n"
    "{\n"
    '  "conviction_score": int 1-10,\n'
    '  "recommendation": string,\n'
    '  "headline": string,  // crisp Bloomberg/Reuters style, grounded in the text\n'
    '  "supply_chain_driver": string,\n'
    '  "price_target_delta_pct": number,  // signed percent, e.g. 12.5 or -4.0\n'
    '  "implied_price_target": number or null,\n'
    '  "analysis": string,  // two concise sentences of Wall Street commentary\n'
    '  "financial_impact_thesis": string  // revenue / EBITDA margin / backlog breakdown\n'
    "}"
)


def _fallback(reason: str, kind: str = "other") -> dict[str, Any]:
    return {
        "conviction_score": 1,
        "recommendation": "AVOID",
        "headline": "",
        "supply_chain_driver": "",
        "price_target_delta_pct": None,
        "implied_price_target": None,
        "analysis": "",
        "financial_impact_thesis": "",
        "valid": False,
        "error": reason,
        "error_kind": kind,  # auth | quota | parse | network | other
    }


_RECOMMENDATIONS = {
    "HIGH CONVICTION BUY", "BUY", "NEUTRAL / WATCH", "AVOID",
}


def _num(value: Any) -> float | None:
    """Coerce an LLM-supplied number to float, or None if it isn't usable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if _finite(float(value)) else None
    text = str(value).strip().replace("%", "").replace("$", "").replace(",", "")
    text = text.lstrip("+")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        num = float(m.group(0))
    except ValueError:
        return None
    return num if _finite(num) else None


def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


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
        "max_tokens": 900,
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

    def _text(key: str) -> str:
        val = str(data.get(key, "") or "").strip()
        return "" if val.lower() in ("none", "null", "n/a", "na", "-") else val

    score_num = _num(data.get("conviction_score"))
    score = int(round(score_num)) if score_num is not None else 0
    score = max(1, min(10, score))

    headline = _text("headline")
    analysis = _text("analysis")
    thesis = _text("financial_impact_thesis")
    driver = _text("supply_chain_driver")

    rec = str(data.get("recommendation", "")).strip().upper()
    if rec not in _RECOMMENDATIONS:
        rec = "HIGH CONVICTION BUY" if score >= 9 else (
            "BUY" if score >= 8 else "NEUTRAL / WATCH" if score >= 5 else "AVOID"
        )

    delta = _num(data.get("price_target_delta_pct"))
    if delta is not None and abs(delta) > 200:  # implausible model output
        delta = None
    target = _num(data.get("implied_price_target"))
    if target is not None and target <= 0:
        target = None

    return {
        "conviction_score": score,
        "recommendation": rec,
        "headline": headline,
        "supply_chain_driver": driver,
        "price_target_delta_pct": delta,
        "implied_price_target": target,
        "analysis": analysis,
        "financial_impact_thesis": thesis,
        "valid": score >= 8 and bool(headline) and bool(analysis),
    }


def evaluate_item(
    company_name: str,
    domain: str,
    text_snippet: str,
    source_url: str,
    ticker: str | None = None,
) -> dict[str, Any]:
    """
    Grade a single raw item with the configured LLM.

    Returns a dict with 'conviction_score' (1-10), 'recommendation', 'headline',
    'supply_chain_driver', 'price_target_delta_pct' (float|None),
    'implied_price_target' (float|None), 'analysis', 'financial_impact_thesis',
    and 'valid' (True only when score >= 8). On a handled failure the same shape
    is returned with 'error'/'error_kind' set and 'valid' False.

    Raises:
        LLMQuotaError: when the API quota/rate limit is exhausted, so the caller
            can stop the run cleanly instead of burning the whole batch.
    """
    snippet = (text_snippet or "").strip()
    if len(snippet) < 20:
        return _fallback("snippet too short", kind="parse")

    prompt = (
        f"COMPANY: {company_name}\n"
        f"TICKER: {ticker or 'n/a'}\n"
        f"DOMAIN: {domain}\n"
        f"SOURCE URL: {source_url}\n"
        f"ARTICLE / FILING TEXT:\n\"\"\"\n{snippet[:_SNIPPET_CHARS]}\n\"\"\"\n\n"
        "Analyze the supply-side catalyst and return the JSON object."
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
