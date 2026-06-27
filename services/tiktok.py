"""
TikTok hashtag scraper — server-rendered HTML extraction.

The old implementation called TikTok's unofficial mobile-app JSON API
(`/api/challenge/detail/`, `/api/challenge/item_list/`) directly. That API
now requires a signed `_signature`/X-Bogus payload that only TikTok's own
JS produces, so plain server-side requests to it are rejected outright —
that's why every hashtag failed to resolve.

This version instead requests the *public, server-rendered* hashtag page
(https://www.tiktok.com/tag/<hashtag>) the same way a logged-out browser
or search-engine crawler would. TikTok renders the first page of videos
for each hashtag directly into the HTML as a JSON blob inside a
<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"> tag (with a fallback to
the older `SIGI_STATE` tag some regions/CDN nodes still serve). We parse
that JSON — no headless browser, no login, no cookies, no proxy, and no
captcha involved, since this is the literal HTML TikTok ships to anyone
loading the hashtag page.

This is inherently a bit fragile (TikTok can reshape this JSON without
notice), so every step is defensive: a failed hashtag is logged and
skipped, never raised, exactly like the previous implementation.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp

from config.settings import settings

logger = logging.getLogger("gadgetbot.services.tiktok")

_HASHTAG_PAGE_URL = "https://www.tiktok.com/tag/{tag}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tiktok.com/",
}

# Matches the modern rehydration payload TikTok embeds in hashtag pages.
_UNIVERSAL_DATA_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)

# Older/alternate payload some edge nodes still serve.
_SIGI_STATE_RE = re.compile(
    r'<script id="SIGI_STATE"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _extract_json_blob(html: str) -> dict[str, Any] | None:
    """Pull whichever embedded state blob is present out of the raw HTML."""
    m = _UNIVERSAL_DATA_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            data = None
        if data:
            return {"kind": "universal", "data": data}

    m = _SIGI_STATE_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            data = None
        if data:
            return {"kind": "sigi", "data": data}

    return None


def _items_from_universal(data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Walk the __UNIVERSAL_DATA_FOR_REHYDRATION__ structure to find the list
    of item (video) dicts for the hashtag/challenge page. The exact nesting
    has shifted across TikTok releases, so we search defensively instead of
    hardcoding one fixed path.
    """
    try:
        scope = data["__DEFAULT_SCOPE__"]
    except (KeyError, TypeError):
        return []

    candidates: list[dict[str, Any]] = []
    for key, value in scope.items():
        if not isinstance(value, dict):
            continue
        if "challenge" not in key and "hashtag" not in key.lower():
            continue
        items = value.get("itemList") or value.get("items") or []
        if isinstance(items, list) and items:
            candidates = items
            break

    if not candidates:
        # Fall back: scan every dict value in scope for an itemList, in case
        # the page used a key name we didn't anticipate.
        for value in scope.values():
            if isinstance(value, dict):
                items = value.get("itemList")
                if isinstance(items, list) and items:
                    candidates = items
                    break

    return candidates


def _items_from_sigi(data: dict[str, Any]) -> list[dict[str, Any]]:
    item_module = data.get("ItemModule") or {}
    return list(item_module.values()) if isinstance(item_module, dict) else []


def _extract_video(item: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one TikTok item dict (either payload shape) into our schema."""
    try:
        video_id = item.get("id") or (item.get("video") or {}).get("id")
        if not video_id:
            return None

        stats = item.get("stats") or item.get("statsV2") or {}
        author = item.get("author") or {}
        if isinstance(author, str):
            # SIGI_STATE sometimes stores author as just a user id string;
            # without a join to UserModule we only have the id to work with.
            author = {"uniqueId": author, "nickname": author}
        video = item.get("video") or {}
        desc = item.get("desc") or ""

        cover = (
            video.get("originCover")
            or video.get("cover")
            or video.get("dynamicCover")
            or ""
        )

        def _num(key: str) -> int:
            val = stats.get(key, 0)
            try:
                return int(val)
            except (TypeError, ValueError):
                return 0

        unique_id = author.get("uniqueId") or author.get("nickname") or "unknown"

        return {
            "video_id": f"tt_{video_id}",
            "source": "tiktok",
            "url": f"https://www.tiktok.com/@{unique_id}/video/{video_id}",
            "title": desc[:200],
            "description": desc,
            "hashtags": " ".join(
                f"#{c.get('hashtagName')}"
                for c in item.get("challenges", []) or []
                if isinstance(c, dict) and c.get("hashtagName")
            ),
            "channel": author.get("nickname") or unique_id,
            "thumbnail": cover,
            "upload_time": str(item.get("createTime", "")),
            "views": _num("playCount"),
            "likes": _num("diggCount"),
            "comments": _num("commentCount"),
            "duration_seconds": int((video.get("duration") or 0) or 0),
            "has_thumbnail": bool(cover),
        }
    except Exception:
        logger.debug("Failed to parse TikTok item", exc_info=True)
        return None


async def _fetch_hashtag_page(
    session: aiohttp.ClientSession, hashtag: str
) -> str | None:
    url = _HASHTAG_PAGE_URL.format(tag=hashtag)
    try:
        async with session.get(
            url,
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("TikTok hashtag page #%s returned HTTP %s", hashtag, resp.status)
                return None
            return await resp.text()
    except Exception:
        logger.warning("TikTok hashtag page request failed for #%s", hashtag, exc_info=True)
        return None


async def _fetch_hashtag_videos(
    session: aiohttp.ClientSession,
    hashtag: str,
    max_count: int,
) -> list[dict[str, Any]]:
    html = await _fetch_hashtag_page(session, hashtag)
    if not html:
        logger.warning("TikTok: could not resolve #%s — skipping", hashtag)
        return []

    blob = _extract_json_blob(html)
    if not blob:
        logger.warning("TikTok: could not resolve #%s — skipping", hashtag)
        return []

    if blob["kind"] == "universal":
        raw_items = _items_from_universal(blob["data"])
    else:
        raw_items = _items_from_sigi(blob["data"])

    if not raw_items:
        logger.warning("TikTok: #%s page loaded but contained no videos", hashtag)
        return []

    videos: list[dict[str, Any]] = []
    for item in raw_items:
        parsed = _extract_video(item)
        if parsed:
            videos.append(parsed)
        if len(videos) >= max_count:
            break

    return videos


async def fetch_tiktok_videos() -> list[dict[str, Any]]:
    """
    Iterate over all configured hashtags, collect videos from each
    hashtag's server-rendered page, and return a deduplicated list.
    Never raises — failures per hashtag are logged and skipped.
    """
    seen: set[str] = set()
    all_videos: list[dict[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        for tag in settings.tiktok_hashtags:
            try:
                vids = await _fetch_hashtag_videos(session, tag, settings.tiktok_max_per_hashtag)
                new_count = 0
                for v in vids:
                    if v["video_id"] not in seen:
                        seen.add(v["video_id"])
                        all_videos.append(v)
                        new_count += 1
                logger.debug("Hashtag #%s returned %d new videos", tag, new_count)
            except Exception:
                logger.exception("TikTok hashtag #%s failed", tag)

    logger.info("TikTok: fetched %d candidate videos", len(all_videos))
    return all_videos
