"""
Gemini-powered conviction grading.

Uses the google-genai SDK with GEMINI_API_KEY read strictly from the local .env.
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY must be set in the local .env file.")

MODEL = "gemini-2.5-flash"
_client = genai.Client(api_key=GEMINI_API_KEY)

_RESPONSE_SCHEMA = types.Schema(
    type=types.Type.OBJECT,
    required=["conviction_score", "headline", "analysis"],
    properties={
        "conviction_score": types.Schema(type=types.Type.INTEGER),
        "headline": types.Schema(type=types.Type.STRING),
        "analysis": types.Schema(type=types.Type.STRING),
    },
)

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


def _fallback(reason: str) -> dict[str, Any]:
    return {
        "conviction_score": 1,
        "headline": "",
        "analysis": "",
        "valid": False,
        "error": reason,
    }


def evaluate_item(
    company_name: str,
    domain: str,
    text_snippet: str,
    source_url: str,
) -> dict[str, Any]:
    """
    Grade a single raw item with Gemini.

    Returns:
        {
          "conviction_score": int (1-10),
          "headline": str,
          "analysis": str,
          "valid": bool,   # True only when conviction_score >= 8
        }
    """
    snippet = (text_snippet or "").strip()
    if len(snippet) < 20:
        return _fallback("snippet too short")

    prompt = (
        f"COMPANY: {company_name}\n"
        f"DOMAIN: {domain}\n"
        f"SOURCE URL: {source_url}\n"
        f"RAW ITEM:\n\"\"\"\n{snippet[:4000]}\n\"\"\"\n\n"
        "Evaluate this item now and return the JSON object."
    )

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
    except Exception as exc:  # network / quota / SDK errors
        return _fallback(f"gemini call failed: {exc}")

    raw = (response.text or "").strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _fallback(f"unparseable response: {raw[:200]}")

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
