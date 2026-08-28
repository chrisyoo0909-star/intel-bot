"""
Gemini-powered conviction grading.

Uses the google-genai SDK (header auth via ``x-goog-api-key``) with
GEMINI_API_KEY read strictly from the local .env. Google's current keys start
with ``AQ.`` (the legacy ``AIza`` prefix is deprecated); both are accepted here
and passed only as an HTTP header, never as a ``?key=`` query parameter.

If the SDK call fails, a raw REST fallback is attempted against the same
endpoint using the ``x-goog-api-key`` header.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip().strip("'").strip('"')
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY must be set in the local .env file.")

MODEL = "gemini-2.5-flash"
_REST_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
)
_REST_TIMEOUT = 45

_client = genai.Client(api_key=GEMINI_API_KEY)


class GeminiQuotaError(RuntimeError):
    """Raised when Gemini reports the request quota is exhausted (HTTP 429)."""


_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    required=["conviction_score", "headline", "analysis"],
    properties={
        "conviction_score": types.Schema(type=types.Type.INTEGER),
        "headline": types.Schema(type=types.Type.STRING),
        "analysis": types.Schema(type=types.Type.STRING),
    },
)

# Plain-dict schema for the REST fallback body.
_REST_SCHEMA = {
    "type": "OBJECT",
    "required": ["conviction_score", "headline", "analysis"],
    "properties": {
        "conviction_score": {"type": "INTEGER"},
        "headline": {"type": "STRING"},
        "analysis": {"type": "STRING"},
    },
}

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
    "Return ONLY JSON with keys: conviction_score (int 1-10), headline (a crisp "
    "Bloomberg/Reuters-style headline), analysis (concise two-sentence Wall Street "
    "commentary)."
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
    return (
        "429" in t
        or "resource_exhausted" in t
        or "quota" in t
        or "rate limit" in t
        or "rate-limit" in t
    )


def _looks_like_auth(text: str) -> bool:
    t = (text or "").lower()
    return (
        "api key not valid" in t
        or "api_key_invalid" in t
        or "unauthenticated" in t
        or "permission_denied" in t
        or "401" in t
        or "403" in t
    )


def _rest_generate(prompt: str) -> str:
    """
    Call Gemini over plain HTTPS with header auth. Returns the raw JSON text.

    The API key is sent ONLY in the 'x-goog-api-key' header — never as ?key=.
    """
    resp = requests.post(
        _REST_ENDPOINT,
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "system_instruction": {"parts": [{"text": _SYSTEM_INSTRUCTION}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json",
                "responseSchema": _REST_SCHEMA,
            },
        },
        timeout=_REST_TIMEOUT,
    )
    if resp.status_code == 429:
        raise GeminiQuotaError(f"REST 429: {resp.text[:300]}")
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _parse_verdict(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _fallback(f"unparseable response: {raw[:200]}", kind="parse")

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
    Grade a single raw item with Gemini.

    Returns a dict with 'conviction_score' (1-10), 'headline', 'analysis', and
    'valid' (True only when score >= 8). On a handled failure the same shape is
    returned with 'error' and 'error_kind' set and 'valid' False.

    Raises:
        GeminiQuotaError: when the API quota/rate limit is exhausted, so the
            caller can stop the run cleanly instead of burning the whole batch.
    """
    snippet = (text_snippet or "").strip()
    if len(snippet) < 20:
        return _fallback("snippet too short", kind="parse")

    prompt = (
        f"COMPANY: {company_name}\n"
        f"DOMAIN: {domain}\n"
        f"SOURCE URL: {source_url}\n"
        f"RAW ITEM:\n\"\"\"\n{snippet[:4000]}\n\"\"\"\n\n"
        "Evaluate this item now and return the JSON object."
    )

    # 1) Primary path: google-genai SDK (header auth).
    try:
        response = _client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                temperature=0.2,
            ),
        )
        return _parse_verdict(response.text or "")
    except GeminiQuotaError:
        raise
    except Exception as sdk_exc:  # noqa: BLE001 - broad on purpose
        msg = str(sdk_exc)
        if _looks_like_quota(msg):
            raise GeminiQuotaError(msg) from sdk_exc

        # 2) Fallback path: raw REST with x-goog-api-key.
        try:
            return _parse_verdict(_rest_generate(prompt))
        except GeminiQuotaError:
            raise
        except requests.HTTPError as http_exc:
            body = getattr(http_exc.response, "text", "") or str(http_exc)
            if _looks_like_quota(body):
                raise GeminiQuotaError(body[:300]) from http_exc
            kind = "auth" if _looks_like_auth(body) else "network"
            return _fallback(f"gemini rest failed: {body[:200]}", kind=kind)
        except Exception as rest_exc:  # noqa: BLE001
            kind = "auth" if _looks_like_auth(f"{msg} {rest_exc}") else "network"
            return _fallback(
                f"gemini failed (sdk: {msg[:120]} | rest: {rest_exc})", kind=kind
            )
