"""
Single Gemini call per video that returns gadget-relevance classification,
product detection, and a one-sentence summary together (cheaper and faster
than three separate calls). Falls back to a conservative keyword check if
the AI call fails, so the pipeline never hard-crashes on an API hiccup.

Uses the Google Gemini API directly (google-generativeai SDK).
Set GEMINI_API_KEY in your .env file.
"""
from __future__ import annotations

import json
import logging

from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings

logger = logging.getLogger("gadgetbot.ai.classifier")

# Lazy-initialise the Gemini client only when an API key is present
_gemini_model = None


def _get_model():
    global _gemini_model
    if _gemini_model is not None:
        return _gemini_model
    if not settings.gemini_api_key:
        return None
    try:
        import google.generativeai as genai  # type: ignore[import]
        genai.configure(api_key=settings.gemini_api_key)
        _gemini_model = genai.GenerativeModel(settings.gemini_model)
        logger.info("Gemini model initialised: %s", settings.gemini_model)
    except Exception:
        logger.exception("Failed to initialise Gemini model")
        _gemini_model = None
    return _gemini_model


SYSTEM_PROMPT = """You are a content classifier for a YouTube Shorts creator who makes \
gadget-reveal videos by adding voiceover/commentary on top of short clips of physical \
consumer products. You are given a candidate video's title, description, and hashtags. \
Decide whether it shows a physical consumer gadget/product (kitchen tools, smart home \
devices, travel gear, camping gear, phone/desk accessories that are NOT phones/laptops \
themselves, novelty items, etc).

REJECT (gadget_score should be low, under 40) if it is primarily about: gaming, phone \
reviews, laptop reviews, software/apps, crypto, cars, politics, podcasts, or general tech \
news/commentary with no specific physical product shown.

Respond with ONLY a JSON object, no other text, in this exact shape:
{
  "is_gadget": true/false,
  "gadget_score": 0-100,
  "reject_reason": "short reason or null",
  "product_name": "string or null",
  "brand": "string or null (Unknown if not stated)",
  "product_category": "string or null, e.g. Kitchen Gadget / Travel Gadget / Smart Home",
  "estimated_price": "string or null, e.g. $15-25 (omit currency guess if no signal at all)",
  "product_confidence": 0-100,
  "summary": "one sentence, plain language, explaining what the product does"
}"""


def _fallback_result(title: str, description: str, reject_keywords: list[str]) -> dict:
    text = f"{title} {description}".lower()
    hit = next((kw for kw in reject_keywords if kw in text), None)
    return {
        "is_gadget": hit is None,
        "gadget_score": 20 if hit else 55,
        "reject_reason": f"keyword match: {hit}" if hit else None,
        "product_name": None,
        "brand": None,
        "product_category": None,
        "estimated_price": None,
        "product_confidence": 0,
        "summary": None,
    }


def _strip_fences(text: str) -> str:
    """Remove markdown code fences that Gemini sometimes wraps JSON in."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
async def _call_gemini(title: str, description: str, hashtags: str) -> dict:
    import asyncio

    model = _get_model()
    if model is None:
        raise RuntimeError("Gemini model not available")

    user_content = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Title: {title}\n"
        f"Description: {description[:500]}\n"
        f"Hashtags: {hashtags}"
    )

    # google-generativeai is synchronous; run in executor to avoid blocking
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: model.generate_content(user_content),
    )

    text = _strip_fences(response.text)
    return json.loads(text)


async def classify_video(
    title: str,
    description: str,
    hashtags: str,
    reject_keywords: list[str],
) -> dict:
    if _get_model() is None:
        logger.warning("GEMINI_API_KEY not set or model unavailable; using keyword-only fallback classifier.")
        return _fallback_result(title, description, reject_keywords)

    try:
        return await _call_gemini(title, description or "", hashtags or "")
    except Exception:
        logger.exception("Gemini classification failed, using fallback for %r", title)
        return _fallback_result(title, description, reject_keywords)
