"""
YouTube Shorts search using yt-dlp.

Replaces the old Playwright-based scraper entirely. yt-dlp talks to
YouTube's internal player/search API directly (no headless browser, no
rendering, no 15s page-load race), so it is far more reliable inside a
small Koyeb worker container and does not need Chromium binaries at all.

Bot-detection fallback strategy
--------------------------------
Datacenter IPs (Koyeb included) regularly get hit with YouTube's
"Sign in to confirm you're not a bot" challenge. There is no single
extractor option that escapes this 100% of the time, since it's an
IP-reputation check, not a missing-header bug. What does help is that
different yt-dlp "player clients" (web, android, ios, tv embedded, etc.)
talk to different internal YouTube API surfaces with different bot-checks,
and different search prefixes (ytsearch vs ytsearchdate) sometimes hit
different backends entirely. So instead of one fixed configuration, each
query is tried against an ordered list of (search-prefix, player-client)
strategies. The first strategy that returns results wins; every other
strategy failing is expected and only logged at debug/warning level, not
treated as fatal. If every strategy fails for every query, the function
still returns an empty list rather than raising, so the bot's scheduled
scan and /search command never crash — they just get zero new videos that
cycle, same as a transient network blip.

Output schema is unchanged from the previous implementation, so
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
(skip_download=True). Playwright is not used anywhere in this module.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, NamedTuple

import yt_dlp

from config.settings import settings

logger = logging.getLogger("gadgetbot.services.youtube")

# Concurrency cap — keep this modest so we don't hammer YouTube from a single
# small worker instance and trip rate limiting/bot-detection even harder.
_SEM = asyncio.Semaphore(2)

# Substring used to detect YouTube's bot-check error message so we can log
# it distinctly and fall through to the next strategy instead of aborting.
_BOT_CHECK_MARKERS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm you are not a bot",
    "confirm you're not a bot",
)

# Errors that mean "this strategy is structurally broken, don't retry it for
# every remaining query" are not special-cased — every failure is per-query,
# per-strategy, and always falls through. Simpler and safer than trying to
# guess which errors are retryable.


class _Strategy(NamedTuple):
    label: str
    search_prefix: str  # e.g. "ytsearch" or "ytsearchdate"
    player_clients: list[str]


# Ordered fallback chain. Tried top-to-bottom per query; first one that
# returns at least one video wins for that query.
#
#   - "web"            : most metadata-complete, but most likely to trip
#                         the bot-check from a datacenter IP.
#   - "android"        : mobile API surface, frequently bypasses the
#                         "sign in to confirm" wall that hits the web client.
#   - "ios"             : a second, independently-throttled mobile surface.
#   - "tv_embedded"     : embedded-player surface, different rate limiting
#                         and historically resistant to the bot-check.
#   ytsearchdate        : same search but sorted by upload date; sometimes
#                         routed differently and succeeds when plain
#                         relevance-sorted ytsearch is being throttled.
_STRATEGIES: list[_Strategy] = [
    _Strategy("ytsearch+web", "ytsearch", ["web"]),
    _Strategy("ytsearch+android", "ytsearch", ["android"]),
    _Strategy("ytsearch+ios", "ytsearch", ["ios"]),
    _Strategy("ytsearch+tv_embedded", "ytsearch", ["tv_embedded"]),
    _Strategy("ytsearchdate+android", "ytsearchdate", ["android"]),
    _Strategy("ytsearchdate+web", "ytsearchdate", ["web"]),
    _Strategy("ytsearch+android_ios", "ytsearch", ["android", "ios"]),
]


def _base_ydl_opts(player_clients: list[str]) -> dict[str, Any]:
    """
    Build a fresh yt-dlp options dict for one strategy attempt.

    quiet/no_warnings keep stdout clean on Koyeb; skip_download guarantees
    we never pull media, only metadata. ignoreerrors is left off here (set
    per-call where appropriate) so a hard failure raises and is caught
    explicitly by the strategy loop, letting us log and move on cleanly.
    """
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,  # we want per-video metadata (views, duration, etc.)
        "noplaylist": False,
        "socket_timeout": 20,
        "extractor_args": {
            "youtube": {
                "player_client": player_clients,
            }
        },
    }


def _is_bot_check_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _BOT_CHECK_MARKERS)


def _stable_id(video_id: str) -> str:
    return f"yt_{video_id}"


def _shorts_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def _best_thumbnail(entry: dict[str, Any]) -> str:
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        # yt-dlp returns thumbnails roughly smallest -> largest;
        # take the last one that actually has a URL.
        for t in reversed(thumbs):
            url = t.get("url")
            if url:
                return url
    return entry.get("thumbnail") or ""


def _to_upload_time(entry: dict[str, Any]) -> str:
    """
    Normalise yt-dlp's upload_date ('YYYYMMDD') into an ISO date string.
    Falls back to empty string if missing/unparseable.
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
        # heuristic default downstream like before.
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


def _run_one_strategy(
    query: str, max_results: int, strategy: _Strategy
) -> list[dict[str, Any]]:
    """
    Blocking single-attempt yt-dlp call for one (search_prefix, player_client)
    strategy. Raises on failure so the caller can distinguish "this strategy
    failed, try the next one" from "this strategy returned zero results
    legitimately". Never downloads media.
    """
    search_term = f"{query} shorts"
    search_spec = f"{strategy.search_prefix}{max_results}:{search_term}"

    opts = _base_ydl_opts(strategy.player_clients)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search_spec, download=False)

    if not info:
        return []

    entries = info.get("entries") or []
    videos: list[dict[str, Any]] = []
    for entry in entries:
        try:
            video = _entry_to_video(entry)
        except Exception:
            logger.debug("Failed to parse yt-dlp entry", exc_info=True)
            continue
        if video:
            videos.append(video)

    return videos


def _run_search_sync(query: str, max_results: int) -> list[dict[str, Any]]:
    """
    Try each fallback strategy in order for one query, returning as soon as
    a strategy produces at least one video. Logs which strategy succeeded.
    Bot-check errors are recognised and logged distinctly. If every
    strategy fails, returns an empty list — never raises.
    """
    for strategy in _STRATEGIES:
        try:
            videos = _run_one_strategy(query, max_results, strategy)
        except Exception as exc:
            if _is_bot_check_error(exc):
                logger.warning(
                    "YouTube bot-check triggered for query %r using strategy %r; "
                    "trying next strategy.",
                    query,
                    strategy.label,
                )
            else:
                logger.warning(
                    "Strategy %r failed for query %r (%s); trying next strategy.",
                    strategy.label,
                    query,
                    exc.__class__.__name__,
                )
            continue

        if videos:
            logger.info(
                "Query %r succeeded using strategy %r (%d videos)",
                query,
                strategy.label,
                len(videos),
            )
            return videos

        logger.debug(
            "Strategy %r returned zero results for query %r; trying next strategy.",
            strategy.label,
            query,
        )

    logger.warning(
        "All yt-dlp strategies failed or returned nothing for query %r", query
    )
    return []


async def _search_query(query: str, max_results: int) -> list[dict[str, Any]]:
    """Async wrapper: run the blocking yt-dlp strategy chain in a worker thread."""
    async with _SEM:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, _run_search_sync, query, max_results)
        except Exception:
            # _run_search_sync already catches everything internally, but
            # guard here too so a freak executor-level error can never
            # propagate up and abort the whole scan.
            logger.exception("Unexpected failure searching query %r", query)
            return []


# ── Public entry point ────────────────────────────────────────────────────────

async def fetch_youtube_videos() -> list[dict[str, Any]]:
    """
    Iterate over every configured search query via yt-dlp (using the
    multi-strategy fallback chain above), dedup by video_id, and return
    enriched metadata dicts ready for classification.

    All failures — including YouTube's bot-check challenge — are caught per
    query/strategy so one bad query, or even a total YouTube lockout, never
    aborts the run or crashes the bot. Worst case this returns [].

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
            logger.debug("Query %r contributed %d new videos", query, new_count)
        except Exception:
            # Belt-and-suspenders: even an unforeseen error here must not
            # prevent the remaining queries (or the rest of the bot) from
            # running.
            logger.exception("Query failed unexpectedly: %r", query)

    if not all_videos:
        logger.warning(
            "YouTube (yt-dlp): fetched 0 candidate videos across %d queries "
            "(likely bot-detection or transient YouTube issue); continuing gracefully.",
            len(settings.youtube_search_queries),
        )
    else:
        logger.info("YouTube (yt-dlp): fetched %d candidate videos", len(all_videos))

    return all_videos
