"""
TikTok hashtag scraper.

TikTok has no official public API, so we hit the same JSON endpoint
the mobile web app uses.  This is inherently fragile; failures are
caught and logged gracefully so they never crash the pipeline.

If TikTok starts requiring a login cookie, set TIKTOK_SESSION_ID in
.env and it will be forwarded automatically.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

from config.settings import settings

logger = logging.getLogger("gadgetbot.services.tiktok")

# Unofficial TikTok hashtag challenge API (used by the web client)
_HASHTAG_INFO_URL = "https://www.tiktok.com/api/challenge/detail/"
_HASHTAG_FEED_URL = "https://www.tiktok.com/api/challenge/item_list/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; Pixel 6) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/112.0.0.0 Mobile Safari/537.36 TikTok/27.8.5"
    ),
    "Referer": "https://www.tiktok.com/",
}


def _cookies() -> dict[str, str]:
    sid = os.getenv("TIKTOK_SESSION_ID", "")
    return {"sessionid": sid} if sid else {}


async def _get_challenge_id(session: aiohttp.ClientSession, hashtag: str) -> str | None:
    params = {
        "challengeName": hashtag,
        "aid": "1988",
    }
    try:
        async with session.get(
            _HASHTAG_INFO_URL,
            params=params,
            headers=_HEADERS,
            cookies=_cookies(),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            return data.get("challengeInfo", {}).get("challenge", {}).get("id")
    except Exception:
        logger.debug("Could not resolve TikTok challenge ID for #%s", hashtag)
        return None


def _extract_video(item: dict) -> dict[str, Any] | None:
    """Parse one TikTok item dict into our normalised schema."""
    try:
        vid_id = item.get("id") or item.get("video", {}).get("id")
        if not vid_id:
            return None

        stats = item.get("stats", {})
        author = item.get("author", {})
        video = item.get("video", {})
        desc = item.get("desc", "")

        cover = (
            video.get("originCover")
            or video.get("cover")
            or video.get("dynamicCover")
            or ""
        )

        return {
            "video_id": f"tt_{vid_id}",
            "source": "tiktok",
            "url": f"https://www.tiktok.com/@{author.get('uniqueId', 'unknown')}/video/{vid_id}",
            "title": desc[:200],
            "description": desc,
            "hashtags": " ".join(
                f"#{c['hashtagName']}" for c in item.get("challenges", []) if c.get("hashtagName")
            ),
            "channel": author.get("nickname") or author.get("uniqueId", ""),
            "thumbnail": cover,
            "upload_time": str(item.get("createTime", "")),
            "views": int(stats.get("playCount", 0)),
            "likes": int(stats.get("diggCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "duration_seconds": int(video.get("duration", 0)),
            "has_thumbnail": bool(cover),
        }
    except Exception:
        logger.debug("Failed to parse TikTok item", exc_info=True)
        return None


async def _fetch_hashtag_videos(
    session: aiohttp.ClientSession,
    hashtag: str,
    max_count: int,
) -> list[dict[str, Any]]:
    challenge_id = await _get_challenge_id(session, hashtag)
    if not challenge_id:
        logger.warning("TikTok: could not resolve #%s — skipping", hashtag)
        return []

    videos: list[dict] = []
    cursor = 0

    while len(videos) < max_count:
        params = {
            "challengeID": challenge_id,
            "count": min(30, max_count - len(videos)),
            "cursor": cursor,
            "aid": "1988",
        }
        try:
            async with session.get(
                _HASHTAG_FEED_URL,
                params=params,
                headers=_HEADERS,
                cookies=_cookies(),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("TikTok feed %s returned HTTP %s", hashtag, resp.status)
                    break
                data = await resp.json(content_type=None)
        except Exception:
            logger.exception("TikTok feed request failed for #%s", hashtag)
            break

        items = data.get("itemList") or data.get("item_list") or []
        if not items:
            break

        for item in items:
            parsed = _extract_video(item)
            if parsed:
                videos.append(parsed)

        if not data.get("hasMore", False):
            break
        cursor = data.get("cursor", cursor + len(items))

    return videos[:max_count]


async def fetch_tiktok_videos() -> list[dict[str, Any]]:
    """
    Iterate over all configured hashtags, collect videos, and return
    a deduplicated list.  Never raises — failures per hashtag are logged.
    """
    seen: set[str] = set()
    all_videos: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for tag in settings.tiktok_hashtags:
            try:
                vids = await _fetch_hashtag_videos(session, tag, settings.tiktok_max_per_hashtag)
                for v in vids:
                    if v["video_id"] not in seen:
                        seen.add(v["video_id"])
                        all_videos.append(v)
            except Exception:
                logger.exception("TikTok hashtag #%s failed", tag)

    logger.info("TikTok: fetched %d candidate videos", len(all_videos))
    return all_videos
