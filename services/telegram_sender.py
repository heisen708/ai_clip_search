"""
Telegram sender.
Formats a scored video document into a Telegram message with thumbnail,
inline buttons (⭐ Favourite / 🔗 Open), and structured metadata.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import TelegramError

from config.settings import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger("gadgetbot.services.telegram_sender")


def _tier_emoji(tier: str) -> str:
    return {"viral": "🔥", "trending": "📈", "rising": "⬆️"}.get(tier, "📹")


def _source_emoji(source: str) -> str:
    return "▶️" if source == "youtube" else "🎵"


def _format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def build_caption(video: dict) -> str:
    tier = video.get("view_tier", "")
    tier_emoji = _tier_emoji(tier)
    src_emoji = _source_emoji(video.get("source", ""))
    title = video.get("title", "Untitled")[:200]
    product = video.get("product_name") or "—"
    brand = video.get("brand") or "—"
    category = video.get("product_category") or "—"
    price = video.get("estimated_price") or "—"
    summary = video.get("summary") or "—"
    views = _format_number(video.get("views", 0))
    likes = _format_number(video.get("likes", 0))
    comments = _format_number(video.get("comments", 0))
    quality = video.get("quality_score", 0)
    virality = video.get("virality_score", 0)
    channel = video.get("channel", "—")
    clip_start = video.get("clip_start")
    clip_end = video.get("clip_end")
    clip_note = video.get("clip_note", "")

    clip_line = ""
    if clip_start is not None and clip_end is not None:
        clip_line = f"\n⏱ <b>Suggested clip:</b> {clip_start}s – {clip_end}s  <i>({clip_note})</i>"

    return (
        f"{tier_emoji} <b>{tier.upper() if tier else 'VIDEO'}</b>  {src_emoji}\n\n"
        f"🎬 <b>{title}</b>\n\n"
        f"📦 <b>Product:</b> {product}\n"
        f"🏷 <b>Brand:</b> {brand}\n"
        f"📂 <b>Category:</b> {category}\n"
        f"💵 <b>Price:</b> {price}\n\n"
        f"💡 {summary}\n"
        f"{clip_line}\n\n"
        f"👁 <b>Views:</b> {views}   ❤️ {likes}   💬 {comments}\n"
        f"📊 <b>Quality:</b> {quality}/100   ⚡ <b>Virality:</b> {virality}/100\n"
        f"📺 <b>Channel:</b> {channel}"
    )


def build_keyboard(video: dict, is_fav: bool = False) -> InlineKeyboardMarkup:
    vid_id = video["video_id"]
    url = video.get("url", "")
    fav_text = "💛 Unfavourite" if is_fav else "⭐ Favourite"
    fav_cb = f"unfav:{vid_id}" if is_fav else f"fav:{vid_id}"

    rows = [
        [
            InlineKeyboardButton(fav_text, callback_data=fav_cb),
            InlineKeyboardButton("🔗 Open", url=url),
        ],
        [
            InlineKeyboardButton("📋 Details", callback_data=f"detail:{vid_id}"),
            InlineKeyboardButton("🚫 Skip", callback_data=f"skip:{vid_id}"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def send_video(bot: Bot, video: dict, chat_id: str | int | None = None) -> bool:
    """
    Send one video to Telegram.  Returns True on success.
    Tries to send the thumbnail as a photo; falls back to text-only if it
    fails (e.g. TikTok CDN blocks the direct URL).
    """
    target = chat_id or settings.telegram_chat_id
    caption = build_caption(video)
    keyboard = build_keyboard(video, is_fav=False)
    thumb = video.get("thumbnail")

    try:
        if thumb:
            try:
                await bot.send_photo(
                    chat_id=target,
                    photo=thumb,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return True
            except TelegramError as exc:
                logger.debug("Thumbnail send failed (%s) — falling back to text", exc)

        # Text fallback
        await bot.send_message(
            chat_id=target,
            text=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )
        return True

    except TelegramError:
        logger.exception("Failed to send video %s to Telegram", video.get("video_id"))
        return False


async def send_video_list(
    bot: Bot,
    videos: list[dict],
    chat_id: str | int | None = None,
    header: str | None = None,
) -> int:
    """Send multiple videos. Returns count of successfully sent messages."""
    target = chat_id or settings.telegram_chat_id
    if header:
        try:
            await bot.send_message(chat_id=target, text=header, parse_mode="HTML")
        except TelegramError:
            logger.warning("Could not send header message")

    sent = 0
    for v in videos:
        ok = await send_video(bot, v, chat_id=target)
        if ok:
            sent += 1
    return sent
