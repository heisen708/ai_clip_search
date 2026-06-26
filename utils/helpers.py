"""
Shared utility helpers used across the pipeline.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("gadgetbot.utils.helpers")


# ── Pipeline orchestration ───────────────────────────────────────────────────

async def run_pipeline() -> list[dict]:
    """
    Full scan-classify-score pipeline.
    Returns the list of newly processed video documents (not just sent ones).
    """
    from ai.classifier import classify_video
    from ai.scorer import classify_view_tier, quality_score, suggest_clip, virality_score
    from config.settings import settings
    from database import upsert_video, video_exists
    from services.tiktok import fetch_tiktok_videos
    from services.youtube import fetch_youtube_videos

    yt_videos = await fetch_youtube_videos()
    tt_videos = await fetch_tiktok_videos()
    candidates = yt_videos + tt_videos

    logger.info("Pipeline: %d candidates before dedup", len(candidates))

    processed: list[dict] = []

    for raw in candidates:
        vid_id = raw.get("video_id", "")
        if not vid_id:
            continue

        # Skip already-known videos
        if await video_exists(vid_id):
            continue

        # AI classification
        clf = await classify_video(
            title=raw.get("title", ""),
            description=raw.get("description", ""),
            hashtags=raw.get("hashtags", ""),
            reject_keywords=settings.reject_keywords,
        )

        # Scoring
        v_score = virality_score(
            views=raw.get("views", 0),
            likes=raw.get("likes", 0),
            comments=raw.get("comments", 0),
            upload_time_iso=raw.get("upload_time", ""),
        )
        q_score = quality_score(
            ai_gadget_score=clf.get("gadget_score", 0),
            virality=v_score,
            title=raw.get("title", ""),
            has_thumbnail=raw.get("has_thumbnail", False),
            comments=raw.get("comments", 0),
        )
        tier = classify_view_tier(raw.get("views", 0), settings.view_tiers)
        clip = suggest_clip(raw.get("duration_seconds", 0), raw.get("title", ""))

        doc: dict[str, Any] = {
            **raw,
            **clf,
            "virality_score": v_score,
            "quality_score": q_score,
            "view_tier": tier,
            "clip_start": clip.get("clip_start"),
            "clip_end": clip.get("clip_end"),
            "clip_note": clip.get("note"),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }

        await upsert_video(doc)

        if clf.get("is_gadget") and q_score >= settings.min_quality_score:
            processed.append(doc)

    logger.info("Pipeline: %d videos passed filters", len(processed))
    return processed


# ── Export helpers ───────────────────────────────────────────────────────────

def videos_to_csv(videos: list[dict]) -> bytes:
    """Serialise a list of video dicts to CSV bytes (UTF-8 with BOM for Excel)."""
    fields = [
        "video_id", "source", "title", "product_name", "brand",
        "product_category", "estimated_price", "views", "likes", "comments",
        "quality_score", "virality_score", "view_tier", "url", "upload_time",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for v in videos:
        writer.writerow(v)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ── Formatting helpers ───────────────────────────────────────────────────────

def fmt_video_short(v: dict) -> str:
    """One-line summary for list displays."""
    src = "▶️" if v.get("source") == "youtube" else "🎵"
    title = (v.get("title") or "Untitled")[:60]
    score = v.get("quality_score", 0)
    url = v.get("url", "")
    return f"{src} [{score}/100] <a href=\"{url}\">{title}</a>"


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def chunk_list(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]
