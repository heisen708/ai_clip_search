"""
Single Claude call per video that returns gadget-relevance classification,
product detection, and a one-sentence summary together (cheaper and faster
than three separate calls). Falls back to a conservative keyword check if
the AI call fails, so the pipeline never hard-crashes on an API hiccup.
"""
import json
import logging

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings

logger = logging.getLogger("gadgetbot.ai.classifier")

_client = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
) if settings.openrouter_api_key else None

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


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
async def _call_claude(title: str, description: str, hashtags: str) -> dict:
    message = await _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Title: {title}\n"
                f"Description: {description[:500]}\n"
                f"Hashtags: {hashtags}"
            ),
        }],
    )
    text = "".join(block.text for block in message.content if hasattr(block, "text")).strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


async def classify_video(title: str, description: str, hashtags: str, reject_keywords: list[str]) -> dict:
    if _client is None:
        logger.warning("ANTHROPIC_API_KEY not set; using keyword-only fallback classifier.")
        return _fallback_result(title, description, reject_keywords)

    try:
        return await _call_claude(title, description or "", hashtags or "")
    except Exception:
        logger.exception("Claude classification failed, using fallback for %r", title)
        return _fallback_result(title, description, reject_keywords)
