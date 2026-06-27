"""
YouTube Shorts search using yt-dlp.

Replaces the old Playwright-based scraper entirely. yt-dlp talks to
YouTube's internal player/search API directly (no headless browser, no
rendering, no 15s page-load race), so it is far more reliable inside a
small Koyeb worker container and does not need Chromium binaries at all.

Output schema is unchanged from the old Playwright implementation, so
helpers.py, classifier.py, and bot.py require no changes:

    {
        "video_id": str,          # "yt_<id>"
        "source": "youtube",
        "url": str,
        "title": str,
        "description": str,
        "hashtags": str,
        "channel": str,
        "thumbnail": str,
        "upload_time": str,
        "views": int,
        "likes": int,
        "comments": int,
        "duration_seconds": int,
        "has_thumbnail": bool,
    }

No video or audio is ever downloaded — only metadata is requested
(skip_download=True, extract_flat where possible).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import yt_dlp

from config.settings import settings

logger = logging.getLogger("gadgetbot.services.youtube")

# Concurrency cap — keep this modest so we don't hammer YouTube from a single
# small worker instance and trip rate limiting.
_SEM = asyncio.Semaphore(2)

# Base yt-dlp options shared by every call. quiet/no_warnings keep stdout
# clean on Koyeb; skip_download guarantees we never pull media, only info.
_YDL_BASE_OPTS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": False,  # we want per-video metadata (views, duration, etc.)
    "noplaylist": False,
    "ignoreerrors": True,
    "socket_timeout": 20,
    "extractor_args": {
        "youtube": {
            # "web" client is the most metadata-complete and least likely to
            # be throttled compared to android/ios clients for search use.
            "player_client": ["web"],
        }
    },
}


def _stable_id(video_id: str) -> str:
    return f"yt_{video_id}"


def _shorts_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def _best_thumbnail(entry: dict[str, Any]) -> str:
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        # yt-dlp returns thumbnails sorted roughly smallest -> largest;
        # take the last one that actually has a URL.
        for t in reversed(thumbs):
            url = t.get("url")
            if url:
                return url
    return entry.get("thumbnail") or ""


def _to_upload_time(entry: dict[str, Any]) -> str:
    """
    Normalise yt-dlp's upload_date ('YYYYMMDD') into an ISO date string.
    Falls back to empty string if missing/unparseable, matching old behaviour
    of returning "" when no upload time was found.
    """
    raw = entry.get("upload_date")
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%Y%m%d").date().isoformat()
    except (ValueError, TypeError):
        return str(raw)


def _is_shorts_duration(entry: dict[str, Any]) -> bool:
    """Shorts are <= ~60s. Be lenient (<=75s) to tolerate metadata drift."""
    duration = entry.get("duration")
    if duration is None:
        # Unknown duration — don't reject, let it through and use the
        # heuristic default downstream like the old scraper did.
        return True
    try:
        return float(duration) <= 75
    except (TypeError, ValueError):
        return True


def _entry_to_video(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one yt-dlp result entry into our normalised schema."""
    if not entry:
        return None

    video_id = entry.get("id")
    if not video_id:
        return None

    if not _is_shorts_duration(entry):
        return None

    title = (entry.get("title") or "").strip()
    description = (entry.get("description") or "") or ""
    channel = (entry.get("uploader") or entry.get("channel") or "").strip()
    thumbnail = _best_thumbnail(entry)

    views = entry.get("view_count")
    views = int(views) if isinstance(views, (int, float)) else 0

    likes = entry.get("like_count")
    likes = int(likes) if isinstance(likes, (int, float)) else 0

    comments = entry.get("comment_count")
    comments = int(comments) if isinstance(comments, (int, float)) else 0

    duration = entry.get("duration")
    duration_seconds = int(duration) if isinstance(duration, (int, float)) else 45

    # Pull hashtags out of yt-dlp's parsed tags/categories if present
    tags = entry.get("tags") or []
    hashtag_str = " ".join(f"#{t}" for t in tags if isinstance(t, str))[:300]

    return {
        "video_id": _stable_id(video_id),
        "source": "youtube",
        "url": _shorts_url(video_id),
        "title": title,
        "description": description,
        "hashtags": hashtag_str,
        "channel": channel,
        "thumbnail": thumbnail or "",
        "upload_time": _to_upload_time(entry),
        "views": views,
        "likes": likes,
        "comments": comments,
        "duration_seconds": duration_seconds,
        "has_thumbnail": bool(thumbnail),
    }


def _run_search_sync(query: str, max_results: int) -> list[dict[str, Any]]:
    """
    Blocking yt-dlp call — executed inside a thread pool executor by the
    async wrapper below. Uses YouTube's "Shorts" search filter the same way
    a real client query string would, via yt-dlp's ytsearch prefix.
    """
    search_term = f"{query} shorts"
    search_spec = f"ytsearch{max_results}:{search_term}"

    opts = dict(_YDL_BASE_OPTS)
    videos: list[dict[str, Any]] = []

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_spec, download=False)
    except Exception:
        logger.exception("yt-dlp search failed for query %r", query)
        return []

    if not info:
        return []

    entries = info.get("entries") or []
    for entry in entries:
        try:
            video = _entry_to_video(entry)
        except Exception:
            logger.debug("Failed to parse yt-dlp entry", exc_info=True)
            continue
        if video:
            videos.append(video)

    return videos


async def _search_query(query: str, max_results: int) -> list[dict[str, Any]]:
    """Async wrapper: run the blocking yt-dlp search in a worker thread."""
    async with _SEM:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _run_search_sync, query, max_results)
        except Exception:
            logger.exception("Query failed: %r", query)
            return []


# ── Public entry point ────────────────────────────────────────────────────────

async def fetch_youtube_videos() -> list[dict[str, Any]]:
    """
    Iterate over every configured search query via yt-dlp, dedup by
    video_id, and return enriched metadata dicts ready for classification.

    All failures are caught per-query so one bad query never aborts the run.
    No media is ever downloaded — metadata only.
    """
    seen: set[str] = set()
    all_videos: list[dict[str, Any]] = []

    for query in settings.youtube_search_queries:
        try:
            vids = await _search_query(query, settings.youtube_max_results)
            new_count = 0
            for v in vids:
                if v["video_id"] not in seen:
                    seen.add(v["video_id"])
                    all_videos.append(v)
                    new_count += 1
            logger.debug("Query %r returned %d new videos", query, new_count)
        except Exception:
            logger.exception("Query failed: %r", query)

    logger.info("YouTube (yt-dlp): fetched %d candidate videos", len(all_videos))
    return all_videos
