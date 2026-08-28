"""
LLM-powered supply-side conviction grading.

Talks to any OpenAI-compatible chat-completions endpoint. Defaults to Groq's
free API (https://console.groq.com/keys) — no billing, no credit card required.

Configure via the local .env:

    LLM_API_KEY          = <key>                          # or GROQ_API_KEY
    LLM_BASE_URL         = https://api.groq.com/openai/v1  # optional override
    LLM_MODEL            = qwen/qwen3.8-27b                # optional override
    LLM_REASONING_EFFORT = none                           # none|low|medium|high; "off" omits it

qwen3.8-27b with reasoning disabled is ~600 tokens/call and fits Groq's free
8k TPM budget. groq/compound-mini has a 70k TPM budget if you need headroom.

The model returns a strict JSON schema (auditable evidence quotes + an
earnings bridge); Python then *code-gates* the verdict — the LLM cannot make
an item "valid" unless it is the primary issuer, the catalyst is quantified,
and it supplied >= 2 verbatim evidence quotes for a real supply-side catalyst.
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

_re_env = _clean(os.environ.get("LLM_REASONING_EFFORT")) or "none"
LLM_REASONING_EFFORT = "" if _re_env.lower() == "off" else _re_env

_ENDPOINT = f"{LLM_BASE_URL}/chat/completions"
_TIMEOUT = 60
_MAX_RETRY_AFTER = 30  # seconds; longer waits are treated as daily exhaustion
_MAX_ATTEMPTS = 4
_SNIPPET_CHARS = 2600

_MAX_ABS_DELTA_PCT = 25.0
_CATALYST_TYPES = [
    "committed_capex", "ppa", "offtake", "capacity_addition",
    "bottleneck", "lead_time", "plan_or_rumor", "not_supply_side",
]
_DISQUALIFYING_CATALYSTS = {"plan_or_rumor", "not_supply_side"}
_RECOMMENDATIONS = {"HIGH CONVICTION BUY", "BUY", "NEUTRAL / WATCH", "AVOID"}


class LLMQuotaError(RuntimeError):
    """Raised when the LLM API reports quota/rate-limit exhaustion (HTTP 429)."""


class _AuthError(RuntimeError):
    """Internal: 401/403 from the LLM API."""


# --------------------------------------------------------------- strict schema

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "conviction_score", "is_primary_issuer", "catalyst_type",
        "evidence_quotes", "quantified", "cited_capex_usd_m", "cited_capacity",
        "earnings_bridge", "price_target_delta_pct", "recommendation",
        "headline", "supply_chain_driver", "analysis", "financial_impact_thesis",
    ],
    "properties": {
        "conviction_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "is_primary_issuer": {"type": "boolean"},
        "catalyst_type": {"type": "string", "enum": _CATALYST_TYPES},
        "evidence_quotes": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {"type": "string"},
        },
        "quantified": {"type": "boolean"},
        "cited_capex_usd_m": {"type": ["number", "null"]},
        "cited_capacity": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["value", "unit"],
            "properties": {
                "value": {"type": ["number", "string", "null"]},
                "unit": {"type": ["string", "null"]},
            },
        },
        "earnings_bridge": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "revenue_growth_bps", "gross_margin_bps",
                "ebitda_margin_bps", "backlog_growth_pct",
            ],
            "properties": {
                "revenue_growth_bps": {"type": ["number", "null"]},
                "gross_margin_bps": {"type": ["number", "null"]},
                "ebitda_margin_bps": {"type": ["number", "null"]},
                "backlog_growth_pct": {"type": ["number", "null"]},
            },
        },
        "price_target_delta_pct": {"type": "number"},
        "recommendation": {"type": "string", "enum": sorted(_RECOMMENDATIONS)},
        "headline": {"type": "string"},
        "supply_chain_driver": {"type": "string"},
        "analysis": {"type": "string"},
        "financial_impact_thesis": {"type": "string"},
    },
}

_SYSTEM_INSTRUCTION = (
    "You are a Senior Supply-Chain Equity Analyst on a Wall Street "
    "infrastructure desk. You read full news articles and regulatory filings "
    "and isolate SUPPLY-SIDE catalysts: committed CapEx, capacity additions, "
    "lead-time shifts, long-term off-take / take-or-pay agreements, power PPAs, "
    "and manufacturing bottlenecks — across advanced compute, hyperscale cloud, "
    "critical energy, critical minerals, and physical AI / robotics.\n\n"
    "RULES:\n"
    "- Judge ONLY on what the supplied text substantiates. Put 1-3 VERBATIM "
    "quotes from the text into 'evidence_quotes' that support your catalyst "
    "call. If you cannot find a concrete quote, the item is not decision-grade.\n"
    "- 'is_primary_issuer' is true only if the article is chiefly about THIS "
    "company (named in the header), not a peer.\n"
    "- 'quantified' is true only if the text gives a hard figure: a dollar "
    "CapEx amount, a capacity number (GW, tons, wafers, units), or a contract "
    "size/term. Fill 'cited_capex_usd_m' (USD millions) and 'cited_capacity' "
    "when present, else null.\n"
    "- 'catalyst_type': use 'plan_or_rumor' for 'could/may/exploring/considering' "
    "items and 'not_supply_side' for opinion columns, price-target/rating "
    "changes, stock-move recaps, and generic PR.\n"
    "- 'earnings_bridge': your estimate of the impact (basis points / percent), "
    "nulls where you cannot estimate.\n"
    "- 'price_target_delta_pct': signed 12-month percent from multiple + "
    "earnings impact. Do NOT invent a spot price; the system computes the "
    "implied target from the article text.\n"
    "- 'conviction_score' 1-10: 1-4 noise, 5-7 real but soft, 8 concrete "
    "committed company-specific catalyst, 9-10 decisive quantified shift with "
    "named assets/counterparties.\n"
    "- 'recommendation': one of HIGH CONVICTION BUY, BUY, NEUTRAL / WATCH, AVOID.\n"
    "- 'headline': crisp Bloomberg/Reuters style, grounded in the text. "
    "'analysis': two sentences. 'financial_impact_thesis': revenue / margin "
    "(bps) / backlog breakdown.\n\n"
    "Return ONLY the JSON object matching the schema."
)


# ------------------------------------------------------------------- helpers

def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))


def _num(value: Any) -> float | None:
    """Coerce an LLM-supplied number to float, or None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if _finite(float(value)) else None
    text = str(value).strip().replace("%", "").replace("$", "").replace(",", "").lstrip("+")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        num = float(m.group(0))
    except ValueError:
        return None
    return num if _finite(num) else None


_SPOT_RE = re.compile(
    r"(?:shares?|stock|share price)\b(?![^.$\n]{0,40}target)[^.$\n]{0,40}?"
    r"\$\s?(\d{1,5}(?:\.\d{1,2})?)"
    r"|(?:trade[sd]?|trading|closed?|changed hands|last (?:traded|closed))\b"
    r"(?![^.$\n]{0,40}target)[^.$\n]{0,40}?\$\s?(\d{1,5}(?:\.\d{1,2})?)"
    r"|\$\s?(\d{1,5}(?:\.\d{1,2})?)\s?(?:per share|/ ?share|a share)",
    re.I,
)


def parse_spot_price(text: str) -> float | None:
    """
    Extract a plausible current share price from article text.

    Only matches a '$' figure sitting next to price/trading/share language and
    NOT next to the word "target", so the model can't smuggle in an invented
    number and analyst-target figures don't leak through. Returns None if
    nothing clearly price-like is found or the value is out of equity range.
    """
    if not text:
        return None
    for m in _SPOT_RE.finditer(text):
        raw = m.group(1) or m.group(2) or m.group(3)
        val = _num(raw)
        if val is not None and 0.5 <= val <= 10000:
            return round(val, 2)
    return None


def _fallback(reason: str, kind: str = "other") -> dict[str, Any]:
    return {
        "conviction_score": 1,
        "recommendation": "AVOID",
        "headline": "",
        "supply_chain_driver": "",
        "catalyst_type": "not_supply_side",
        "is_primary_issuer": False,
        "quantified": False,
        "evidence_quotes": [],
        "cited_capex_usd_m": None,
        "cited_capacity": None,
        "earnings_bridge": {},
        "price_target_delta_pct": 0.0,
        "implied_price_target": None,
        "analysis": "",
        "financial_impact_thesis": "",
        "valid": False,
        "gate": "error",
        "error": reason,
        "error_kind": kind,  # auth | quota | parse | network | other
    }


def _looks_like_quota(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in ("429", "resource_exhausted", "quota", "rate limit", "rate_limit"))


def _looks_like_auth(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in (
        "invalid api key", "api key not valid", "invalid_api_key", "unauthenticated",
        "unauthorized", "permission_denied", "401", "403",
    ))


def _mentions_daily(text: str) -> bool:
    t = (text or "").lower()
    return "per day" in t or "daily" in t or "rpd" in t or "tpd" in t


_RETRY_DELAY_RE = re.compile(r"try again in ([\d.]+)\s*(m|min|s|ms)?", re.I)


def _retry_delay(resp: requests.Response, body: str) -> float:
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


_USE_JSON_SCHEMA = True  # flipped off at runtime if the backend rejects it


def _chat(prompt: str) -> str:
    """
    One chat completion returning the raw JSON message content.

    Retries a short per-minute 429 up to _MAX_ATTEMPTS times. Raises
    LLMQuotaError on a daily cap / exhausted retries.
    """
    global _USE_JSON_SCHEMA

    def _payload() -> dict[str, Any]:
        p: dict[str, Any] = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.15,
            "max_tokens": 1100,
        }
        if _USE_JSON_SCHEMA:
            p["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "supply_side_verdict",
                    "schema": _RESPONSE_SCHEMA,
                    "strict": True,
                },
            }
        else:
            p["response_format"] = {"type": "json_object"}
        if LLM_REASONING_EFFORT:
            p["reasoning_effort"] = LLM_REASONING_EFFORT
        return p

    resp = _post(_payload())

    # One-time downgrade if the backend can't do json_schema response_format.
    if (resp.status_code == 400 and _USE_JSON_SCHEMA
            and "response_format" in resp.text.lower()
            and "schema" in resp.text.lower()):
        print(f"[analyzer] json_schema rejected, falling back to json_object: "
              f"{resp.text[:200]}")
        _USE_JSON_SCHEMA = False
        resp = _post(_payload())

    for _ in range(1, _MAX_ATTEMPTS):
        if resp.status_code != 429:
            break
        body = resp.text[:500]
        delay = _retry_delay(resp, body)
        if _mentions_daily(body) or not delay or delay > _MAX_RETRY_AFTER:
            raise LLMQuotaError(body)
        time.sleep(delay + 1.0)
        resp = _post(_payload())
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

    return resp.json()["choices"][0]["message"]["content"]


def _parse_verdict(raw: str, source_text: str) -> dict[str, Any]:
    raw = (raw or "").strip()
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

    model_score = _num(data.get("conviction_score"))
    model_score = int(round(model_score)) if model_score is not None else 0
    model_score = max(1, min(10, model_score))

    headline = _text("headline")
    analysis = _text("analysis")
    thesis = _text("financial_impact_thesis")
    driver = _text("supply_chain_driver")

    is_primary = bool(data.get("is_primary_issuer"))
    quantified = bool(data.get("quantified"))
    catalyst = str(data.get("catalyst_type", "not_supply_side")).strip().lower()
    if catalyst not in _CATALYST_TYPES:
        catalyst = "not_supply_side"

    quotes_raw = data.get("evidence_quotes") or []
    if isinstance(quotes_raw, str):
        quotes_raw = [quotes_raw]
    quotes = [q.strip() for q in quotes_raw if isinstance(q, str) and q.strip()][:3]

    # ---- code-level conviction gate -------------------------------------
    gate_pass = (
        is_primary
        and quantified
        and len(quotes) >= 2
        and catalyst not in _DISQUALIFYING_CATALYSTS
    )
    if gate_pass:
        score = model_score
        gate = "pass"
    else:
        score = min(model_score, 7)  # can never be "valid"
        gate = "blocked"

    # ---- price target: quantified-gated, clamped, spot from TEXT only ---
    delta = _num(data.get("price_target_delta_pct")) or 0.0
    if not quantified or not gate_pass:
        delta = 0.0
    delta = max(-_MAX_ABS_DELTA_PCT, min(_MAX_ABS_DELTA_PCT, delta))

    spot = parse_spot_price(source_text)
    implied = None
    if spot is not None and delta:
        implied = round(spot * (1 + delta / 100.0), 2)

    rec = str(data.get("recommendation", "")).strip().upper()
    if rec not in _RECOMMENDATIONS:
        rec = "HIGH CONVICTION BUY" if score >= 9 else (
            "BUY" if score >= 8 else "NEUTRAL / WATCH" if score >= 5 else "AVOID"
        )
    if not gate_pass and rec in ("HIGH CONVICTION BUY", "BUY"):
        rec = "NEUTRAL / WATCH"

    capex = _num(data.get("cited_capex_usd_m"))
    bridge = data.get("earnings_bridge")
    if not isinstance(bridge, dict):
        bridge = {}

    return {
        "conviction_score": score,
        "recommendation": rec,
        "headline": headline,
        "supply_chain_driver": driver,
        "catalyst_type": catalyst,
        "is_primary_issuer": is_primary,
        "quantified": quantified,
        "evidence_quotes": quotes,
        "cited_capex_usd_m": capex,
        "cited_capacity": data.get("cited_capacity") if isinstance(data.get("cited_capacity"), dict) else None,
        "earnings_bridge": bridge,
        "price_target_delta_pct": round(delta, 2),
        "implied_price_target": implied,
        "spot_price": spot,
        "analysis": analysis,
        "financial_impact_thesis": thesis,
        "gate": gate,
        "valid": gate_pass and score >= 8 and bool(headline) and bool(analysis),
    }


def evaluate_item(
    company_name: str,
    domain: str,
    text_snippet: str,
    source_url: str,
    ticker: str | None = None,
) -> dict[str, Any]:
    """
    Grade a single raw item with the configured LLM, then code-gate the verdict.

    Returns a dict with conviction_score, recommendation, headline,
    supply_chain_driver, catalyst_type, is_primary_issuer, quantified,
    evidence_quotes, cited_capex_usd_m, cited_capacity, earnings_bridge,
    price_target_delta_pct (clamped ±25), implied_price_target (spot parsed
    from the text × delta, else None), analysis, financial_impact_thesis,
    'gate' ('pass' | 'blocked' | 'error'), and 'valid'.

    Raises:
        LLMQuotaError: when the API quota/rate limit is exhausted.
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

    return _parse_verdict(content, snippet)
