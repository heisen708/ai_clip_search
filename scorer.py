"""
Rule-based scoring. The AI gadget-relevance score comes from ai/classifier.py;
everything here is deterministic so it's cheap to run on every candidate and
easy to tune without touching prompts.
"""
import math
from datetime import datetime, timezone


def _age_hours(upload_time_iso: str) -> float:
    if not upload_time_iso:
        return 999.0
    try:
        ts = datetime.fromisoformat(upload_time_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return max(delta.total_seconds() / 3600.0, 0.0)
    except Exception:
        return 999.0


def classify_view_tier(views: int, tiers: dict) -> str:
    if views >= tiers["viral"]:
        return "viral"
    if views >= tiers["trending"]:
        return "trending"
    if views >= tiers["rising"]:
        return "rising"
    return "low"


def virality_score(views: int, likes: int, comments: int, upload_time_iso: str) -> int:
    """
    0-100. Rewards high view *velocity* (views relative to age) over raw view
    count, since a 6-hour-old video at 50K views is a stronger signal than a
    3-day-old video at 50K views.
    """
    age = max(_age_hours(upload_time_iso), 0.5)
    velocity = views / age  # views per hour

    # log-scale velocity into 0-70 range (tuned so ~5000 views/hr ~ near top)
    velocity_component = min(70, 14 * math.log10(velocity + 1))

    engagement_rate = (likes + comments * 2) / max(views, 1)
    engagement_component = min(30, engagement_rate * 1500)  # typical good rate ~1-2%

    return round(min(100, velocity_component + engagement_component))


def quality_score(
    ai_gadget_score: int,
    virality: int,
    title: str,
    has_thumbnail: bool,
    comments: int,
) -> int:
    """
    Final composite out of 100, weighting the AI relevance check heaviest
    since an off-topic viral video is still useless for this channel.
    """
    title_quality = 0
    if title:
        length = len(title)
        if 20 <= length <= 100:
            title_quality += 10
        if any(c in title for c in "!?"):
            title_quality += 5
        if any(w in title.lower() for w in ("you need", "this is", "incredible", "amazing", "genius", "hack")):
            title_quality += 5
    title_quality = min(20, title_quality)

    thumb_component = 5 if has_thumbnail else 0
    comments_component = min(5, math.log10(comments + 1) * 2)

    composite = (
        ai_gadget_score * 0.45
        + virality * 0.35
        + title_quality
        + thumb_component
        + comments_component
    )
    return round(min(100, composite))


def suggest_clip(duration_seconds: int, title: str) -> dict:
    """
    Heuristic best-clip-segment guess based ONLY on title/duration metadata
    (no transcript or frame analysis available from search APIs alone).

    For genuinely accurate "most interesting moment" detection you'd need to
    download the video and either run scene-detection or transcribe the audio
    and have Claude point at the line that lands the reveal - see README for
    how to bolt that on as a v2 enhancement. Until then this gives a
    reasonable starting point: skip the first ~15% (intro/hook) and suggest a
    window through to ~70%, which is where most reveal-style Shorts put the
    payoff.
    """
    if not duration_seconds or duration_seconds <= 0:
        return {"clip_start": None, "clip_end": None, "note": "Unknown duration - inspect manually."}

    start = round(duration_seconds * 0.15)
    end = round(duration_seconds * 0.70)
    if end <= start:
        end = duration_seconds
    return {
        "clip_start": start,
        "clip_end": end,
        "note": "Heuristic estimate from duration only - verify the actual reveal moment manually.",
    }
