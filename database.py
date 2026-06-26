
"""
Async MongoDB Atlas data-access layer for GadgetBot.

Built on Motor (async pymongo wrapper). Exposes every function imported or
called by bot.py:

    connect, disconnect, get_db,
    upsert_user, get_user_settings, update_user_settings,
    upsert_video, upsert_videos, mark_video_sent,
    get_videos_by_tier, get_recent_sent,
    add_favorite, remove_favorite, get_favorites,
    get_stats

Collections
-----------
videos      – one document per scraped/classified video, keyed by video_id
favorites   – one document per (user_id, video_id) favourite
history     – one document per "sent" event (audit trail of what was pushed)
users       – one document per Telegram user
settings    – one document per user's bot preferences (notify / min_score / tiers)

Duplicate handling
-------------------
All writes use `video_id` (videos), `(user_id, video_id)` (favorites), and
`user_id` (users / settings) as natural keys with unique indexes, and all
upserts use `update_one(..., upsert=True)` so re-running the scraper or
re-favouriting something never creates duplicate rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from config.settings import settings

logger = logging.getLogger("gadgetbot.database")

# ── Module-level connection state ───────────────────────────────────────────

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None

# Default per-user settings document (merged with whatever is stored)
_DEFAULT_USER_SETTINGS: dict[str, Any] = {
    "notify": True,
    "min_score": None,  # falls back to settings.min_quality_score if None
    "tiers": ["viral", "trending", "rising"],
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Connection lifecycle ────────────────────────────────────────────────────

async def connect() -> None:
    """Open the MongoDB connection and ensure indexes exist. Idempotent."""
    global _client, _db

    if _client is not None:
        return

    logger.info("Connecting to MongoDB Atlas (db=%s)…", settings.mongodb_db)
    _client = AsyncIOMotorClient(settings.mongodb_uri)
    _db = _client[settings.mongodb_db]

    # Fail fast if the URI/credentials are bad
    await _client.admin.command("ping")
    logger.info("MongoDB connection established.")

    await _ensure_indexes(_db)
    logger.info("MongoDB indexes ensured.")


async def disconnect() -> None:
    """Close the MongoDB connection. Safe to call even if never connected."""
    global _client, _db
    if _client is not None:
        _client.close()
        logger.info("MongoDB connection closed.")
    _client = None
    _db = None


def get_db() -> AsyncIOMotorDatabase:
    """
    Return the active database handle.

    bot.py calls this directly (e.g. `db.get_db().videos.find_one(...)`),
    so this must return a real Motor database object, not a coroutine.
    """
    if _db is None:
        raise RuntimeError(
            "Database not connected. Call database.connect() before use "
            "(this normally happens in bot.py's post_init hook)."
        )
    return _db


async def _ensure_indexes(database: AsyncIOMotorDatabase) -> None:
    """Create all required indexes. Safe to call repeatedly (no-op if present)."""

    # videos: video_id is the natural primary key; query patterns also need
    # views, view_tier, quality_score, sent_at for tier listings and stats.
    await database.videos.create_index("video_id", unique=True, name="uniq_video_id")
    await database.videos.create_index(
        [("view_tier", ASCENDING), ("quality_score", DESCENDING)],
        name="tier_quality",
    )
    await database.videos.create_index([("sent_at", DESCENDING)], name="sent_at_desc")
    await database.videos.create_index([("is_gadget", ASCENDING)], name="is_gadget")
    await database.videos.create_index([("processed_at", DESCENDING)], name="processed_at_desc")

    # favorites: one row per user+video, fast lookups both directions
    await database.favorites.create_index(
        [("user_id", ASCENDING), ("video_id", ASCENDING)],
        unique=True,
        name="uniq_user_video",
    )
    await database.favorites.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)], name="user_recent")

    # history: audit trail of sends, most-recent-first lookups
    await database.history.create_index([("sent_at", DESCENDING)], name="history_sent_at_desc")
    await database.history.create_index("video_id", name="history_video_id")

    # users: telegram user_id is the natural primary key
    await database.users.create_index("user_id", unique=True, name="uniq_user_id")

    # settings: one document per user
    await database.settings.create_index("user_id", unique=True, name="uniq_settings_user_id")


# ── Users ────────────────────────────────────────────────────────────────────

async def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Insert or update a Telegram user's profile. Called on every command."""
    database = get_db()
    await database.users.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "last_seen_at": _now(),
            },
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )


async def get_user(user_id: int) -> dict | None:
    database = get_db()
    return await database.users.find_one({"user_id": user_id})


# ── Per-user settings ────────────────────────────────────────────────────────

async def get_user_settings(user_id: int) -> dict:
    """
    Return this user's settings, merged with sane defaults so callers never
    need to worry about missing keys (notify / min_score / tiers).
    """
    database = get_db()
    doc = await database.settings.find_one({"user_id": user_id})
    merged = dict(_DEFAULT_USER_SETTINGS)
    if doc:
        for key in _DEFAULT_USER_SETTINGS:
            if key in doc and doc[key] is not None:
                merged[key] = doc[key]
    if merged.get("min_score") is None:
        merged["min_score"] = settings.min_quality_score
    return merged


async def update_user_settings(user_id: int, updates: dict[str, Any]) -> dict:
    """Patch a user's settings document (upserting it if it doesn't exist yet)."""
    database = get_db()
    doc = await database.settings.find_one_and_update(
        {"user_id": user_id},
        {
            "$set": {**updates, "user_id": user_id, "updated_at": _now()},
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc


# ── Videos ───────────────────────────────────────────────────────────────────

async def upsert_video(video: dict[str, Any]) -> bool:
    """
    Insert or update a single video document, keyed by video_id.

    Returns True if this was a brand-new video (first time seen), False if
    it already existed and was merely refreshed (e.g. updated view count).
    This lets the scraping pipeline distinguish "new" videos from re-scrapes.
    """
    database = get_db()
    video_id = video["video_id"]

    payload = dict(video)
    payload.pop("_id", None)
    payload["updated_at"] = _now()

    try:
        result = await database.videos.update_one(
            {"video_id": video_id},
            {
                "$set": payload,
                "$setOnInsert": {"first_seen_at": _now()},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        # Extremely unlikely race (two concurrent upserts for a brand-new
        # video_id); treat as "already exists" and retry as a plain update.
        await database.videos.update_one({"video_id": video_id}, {"$set": payload})
        return False

    return result.upserted_id is not None


async def upsert_videos(videos: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Bulk-upsert many video documents (used by the scrape→classify→score
    pipeline). Returns the subset of input videos that were brand new,
    so callers can decide what to notify/send.
    """
    new_videos: list[dict[str, Any]] = []
    for video in videos:
        if "video_id" not in video:
            logger.warning("Skipping video without video_id: %r", video)
            continue
        is_new = await upsert_video(video)
        if is_new:
            new_videos.append(video)
    return new_videos


async def get_video(video_id: str) -> dict | None:
    database = get_db()
    return await database.videos.find_one({"video_id": video_id})


async def mark_video_sent(video_id: str) -> None:
    """
    Flag a video as sent (so it isn't re-sent on the next scan) and append
    a record to the history collection for /history and audit purposes.
    """
    database = get_db()
    now = _now()

    await database.videos.update_one(
        {"video_id": video_id},
        {"$set": {"sent": True, "sent_at": now}},
    )
    await database.history.update_one(
        {"video_id": video_id, "sent_at": now},
        {"$set": {"video_id": video_id, "sent_at": now}},
        upsert=True,
    )


async def get_videos_by_tier(tier: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return the highest-quality gadget videos in a given view tier."""
    database = get_db()
    cursor = (
        database.videos.find({"view_tier": tier, "is_gadget": True})
        .sort("quality_score", DESCENDING)
        .limit(limit)
    )
    return [doc async for doc in cursor]


async def get_recent_sent(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recently sent videos, newest first."""
    database = get_db()
    cursor = (
        database.videos.find({"sent": True})
        .sort("sent_at", DESCENDING)
        .limit(limit)
    )
    return [doc async for doc in cursor]

async def video_exists(video_id: str) -> bool:
    database = get_db()
    return (
        await database.videos.count_documents(
            {"video_id": video_id},
            limit=1,
        )
        > 0
    )

async def add_history(video: dict):
    database = get_db()

    await database.history.insert_one({
        "video_id": video["video_id"],
        "title": video.get("title"),
        "url": video.get("url"),
        "source": video.get("source"),
        "sent_at": _now(),
    })


async def get_history(limit: int = 20):
    database = get_db()

    cursor = (
        database.history.find()
        .sort("sent_at", DESCENDING)
        .limit(limit)
    )

    return [doc async for doc in cursor]


# ── Favorites ────────────────────────────────────────────────────────────────

async def add_favorite(user_id: int, video_id: str) -> bool:
    """
    Add a video to a user's favourites.

    Returns True if newly added, False if it was already a favourite
    (so bot.py can show "Already in favourites." instead of duplicating).
    """
    database = get_db()
    try:
        await database.favorites.insert_one(
            {
                "user_id": user_id,
                "video_id": video_id,
                "created_at": _now(),
            }
        )
        return True
    except DuplicateKeyError:
        return False


async def remove_favorite(user_id: int, video_id: str) -> bool:
    """Remove a favourite. Returns True if something was actually deleted."""
    database = get_db()
    result = await database.favorites.delete_one({"user_id": user_id, "video_id": video_id})
    return result.deleted_count > 0


async def get_favorites(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """
    Return this user's favourite videos (full video documents, newest
    favourite first), for /favorites and CSV export.
    """
    database = get_db()
    fav_cursor = (
        database.favorites.find({"user_id": user_id})
        .sort("created_at", DESCENDING)
        .limit(limit)
    )
    favorite_docs = [doc async for doc in fav_cursor]
    if not favorite_docs:
        return []

    video_ids = [doc["video_id"] for doc in favorite_docs]
    videos_cursor = database.videos.find({"video_id": {"$in": video_ids}})
    videos_by_id = {v["video_id"]: v async for v in videos_cursor}

    # Preserve favourite order (most recently favourited first)
    ordered: list[dict[str, Any]] = []
    for doc in favorite_docs:
        video = videos_by_id.get(doc["video_id"])
        if video:
            ordered.append(video)
    return ordered


async def is_favorite(user_id: int, video_id: str) -> bool:
    database = get_db()
    doc = await database.favorites.find_one({"user_id": user_id, "video_id": video_id})
    return doc is not None


# ── Stats ────────────────────────────────────────────────────────────────────

async def get_stats() -> dict[str, int]:
    """Aggregate counters for /stats."""
    database = get_db()

    total_videos = await database.videos.count_documents({})
    gadget_videos = await database.videos.count_documents({"is_gadget": True})
    sent_videos = await database.videos.count_documents({"sent": True})
    viral = await database.videos.count_documents({"view_tier": "viral"})
    trending = await database.videos.count_documents({"view_tier": "trending"})
    rising = await database.videos.count_documents({"view_tier": "rising"})

    return {
        "total_videos": total_videos,
        "gadget_videos": gadget_videos,
        "sent_videos": sent_videos,
        "viral": viral,
        "trending": trending,
        "rising": rising,
    }
