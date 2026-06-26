"""
YouTube Data API v3 service.
Searches for Shorts using configurable query strings, then fetches full
video statistics (views, likes, comments, duration) in a single batch
contentDetails + statistics call to minimise quota usage.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from config.settings import settings

logger = logging.getLogger("gadgetbot.services.youtube")

_YT_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

_ISO_RE = re.compile(
    r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
    re.IGNORECASE,
)


def _parse_duration(iso: str) -> int:
    """Return duration in seconds from ISO 8601 duration string."""
    m = _ISO_RE.match(iso or "")
    if not m:
        return 0
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s


def _build_hashtags(description: str, tags: list[str]) -> str:
    hashtags = [t for t in (tags or []) if t]
    from_desc = re.findall(r"#\w+", description or "")
    combined = list(dict.fromkeys(hashtags + from_desc))
    return " ".join(combined[:20])


async def _search_videos(
    session: aiohttp.ClientSession,
    query: str,
    max_results: int,
) -> list[str]:
    """Return a list of video IDs matching `query`."""
    params = {
        "part": "id",
        "q": query,
        "type": "video",
        "videoDuration": "short",
        "order": "viewCount",
        "maxResults": min(max_results, 50),
        "key": settings.youtube_api_key,
    }
    async with session.get(_YT_SEARCH_URL, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            logger.warning("YouTube search error %s: %s", resp.status, text[:200])
            return []
        data = await resp.json()
    return [item["id"]["videoId"] for item in data.get("items", []) if "videoId" in item.get("id", {})]


async def _fetch_details(
    session: aiohttp.ClientSession,
    video_ids: list[str],
) -> list[dict[str, Any]]:
    """Fetch full details for up to 50 video IDs in one API call."""
    if not video_ids:
        return []
    params = {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
        "key": settings.youtube_api_key,
    }
    async with session.get(_YT_VIDEOS_URL, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            logger.warning("YouTube videos error %s: %s", resp.status, text[:200])
            return []
        data = await resp.json()

    results = []
    for item in data.get("items", []):
        vid_id = item["id"]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        content = item.get("contentDetails", {})

        duration_s = _parse_duration(content.get("duration", ""))
        # Keep only true Shorts (≤ 60 s) — be generous to 90 s
        if duration_s > 90:
            continue

        thumbnails = snippet.get("thumbnails", {})
        thumb = (
            thumbnails.get("maxres", {}).get("url")
            or thumbnails.get("high", {}).get("url")
            or thumbnails.get("medium", {}).get("url")
            or thumbnails.get("default", {}).get("url")
        )

        description = snippet.get("description", "")
        tags = snippet.get("tags", [])

        results.append({
            "video_id": f"yt_{vid_id}",
            "source": "youtube",
            "url": f"https://www.youtube.com/shorts/{vid_id}",
            "title": snippet.get("title", ""),
            "description": description,
            "hashtags": _build_hashtags(description, tags),
            "channel": snippet.get("channelTitle", ""),
            "thumbnail": thumb,
            "upload_time": snippet.get("publishedAt", ""),
            "views": int(stats.get("viewCount", 0)),
            "likes": int(stats.get("likeCount", 0)),
            "comments": int(stats.get("commentCount", 0)),
            "duration_seconds": duration_s,
            "has_thumbnail": bool(thumb),
        })

    return results


async def fetch_youtube_videos() -> list[dict[str, Any]]:
    """
    Run all configured search queries in sequence, deduplicate by video ID,
    and return enriched metadata dicts ready for classification.
    """
    if not settings.youtube_api_key:
        logger.warning("YOUTUBE_API_KEY not set — skipping YouTube fetch.")
        return []

    seen: set[str] = set()
    all_videos: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for query in settings.youtube_search_queries:
            try:
                ids = await _search_videos(session, query, settings.youtube_max_results)
                new_ids = [vid for vid in ids if f"yt_{vid}" not in seen]
                if not new_ids:
                    continue
                for vid in new_ids:
                    seen.add(f"yt_{vid}")

                # Batch up to 50 IDs per detail call
                for i in range(0, len(new_ids), 50):
                    batch = new_ids[i:i + 50]
                    details = await _fetch_details(session, batch)
                    all_videos.extend(details)

            except Exception:
                logger.exception("YouTube query failed: %r", query)

    logger.info("YouTube: fetched %d candidate videos", len(all_videos))
    return all_videos
