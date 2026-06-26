"""
GadgetBot — main entry point.

Commands
--------
/start       – welcome + register user
/help        – command list
/search      – manual scan now
/viral       – show viral videos
/trending    – show trending videos
/rising      – show rising videos
/favorites   – list saved favourites
/history     – recently sent videos
/stats       – database stats
/settings    – show/change user settings

Inline callbacks
----------------
fav:<id>     – add to favourites
unfav:<id>   – remove from favourites
detail:<id>  – show full classification details
skip:<id>    – mark as skipped (no-op placeholder)
export_fav   – export favourites as CSV
"""
from __future__ import annotations
from aiohttp import web

import asyncio
import io
import logging
import os

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import database as db
from config.settings import settings
from services.telegram_sender import build_keyboard, send_video, send_video_list
from utils.helpers import (
    chunk_list,
    fmt_video_short,
    run_pipeline,
    videos_to_csv,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("gadgetbot.bot")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _register_user(update: Update) -> None:
    user = update.effective_user
    if user:
        await db.upsert_user(user.id, user.username, user.first_name)


async def _reply(update: Update, text: str, **kwargs) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, **kwargs)


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update)
    name = update.effective_user.first_name or "there"
    await _reply(
        update,
        f"👋 Hey <b>{name}</b>! I'm <b>GadgetBot</b> 🤖\n\n"
        "I automatically find trending gadget videos from YouTube & TikTok, "
        "rank them with AI, and send the best ones here every 30 minutes.\n\n"
        "Type /help to see all commands.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        "🛠 <b>Available Commands</b>\n\n"
        "/search — run a manual scan right now\n"
        "/viral — show viral gadget videos 🔥\n"
        "/trending — show trending videos 📈\n"
        "/rising — show rising videos ⬆️\n"
        "/favorites — your saved favourites ⭐\n"
        "/history — recently sent videos 📋\n"
        "/stats — database statistics 📊\n"
        "/settings — view or change your settings ⚙️\n\n"
        "Use the inline buttons on any video card to ⭐ save or 🔗 open it.",
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update)
    msg = await _reply(update, "🔍 Running scan… this may take a minute.")
    try:
        new_videos = await run_pipeline()
        to_send = sorted(new_videos, key=lambda v: v.get("quality_score", 0), reverse=True)[
            : settings.max_videos_per_scan
        ]
        if not to_send:
            await update.effective_message.edit_text("✅ Scan complete — no new gadget videos found.")
            return

        await update.effective_message.edit_text(
            f"✅ Found <b>{len(new_videos)}</b> new gadget videos. Sending top {len(to_send)}…",
            parse_mode=ParseMode.HTML,
        )
        for v in to_send:
            await send_video(context.bot, v, chat_id=update.effective_chat.id)
            await db.mark_video_sent(v["video_id"])
    except Exception:
        logger.exception("Manual scan failed")
        await update.effective_message.edit_text("❌ Scan failed. Check logs.")


async def _send_tier(update: Update, context: ContextTypes.DEFAULT_TYPE, tier: str) -> None:
    await _register_user(update)
    videos = await db.get_videos_by_tier(tier, limit=5)
    if not videos:
        await _reply(update, f"No <b>{tier}</b> videos in the database yet. Try /search first.")
        return
    header = {"viral": "🔥 <b>VIRAL GADGET VIDEOS</b>", "trending": "📈 <b>TRENDING GADGET VIDEOS</b>", "rising": "⬆️ <b>RISING GADGET VIDEOS</b>"}.get(tier, "📹")
    await send_video_list(context.bot, videos, chat_id=update.effective_chat.id, header=header)


async def cmd_viral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_tier(update, context, "viral")


async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_tier(update, context, "trending")


async def cmd_rising(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_tier(update, context, "rising")


async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update)
    user_id = update.effective_user.id
    favs = await db.get_favorites(user_id, limit=20)
    if not favs:
        await _reply(update, "You have no favourites yet. Tap ⭐ on any video card to save it.")
        return

    lines = [f"⭐ <b>Your Favourites</b> ({len(favs)})\n"]
    for v in favs:
        lines.append(fmt_video_short(v))

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 Export as CSV", callback_data="export_fav"),
    ]])
    await _reply(update, "\n".join(lines), reply_markup=keyboard)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update)
    videos = await db.get_recent_sent(limit=20)
    if not videos:
        await _reply(update, "No videos have been sent yet. Try /search.")
        return
    lines = [f"📋 <b>Recently Sent</b> ({len(videos)})\n"]
    for v in videos:
        lines.append(fmt_video_short(v))
    await _reply(update, "\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update)
    s = await db.get_stats()
    await _reply(
        update,
        "📊 <b>Database Statistics</b>\n\n"
        f"📁 Total videos: <b>{s['total_videos']}</b>\n"
        f"🔧 Gadget videos: <b>{s['gadget_videos']}</b>\n"
        f"📤 Sent: <b>{s['sent_videos']}</b>\n\n"
        f"🔥 Viral: <b>{s['viral']}</b>\n"
        f"📈 Trending: <b>{s['trending']}</b>\n"
        f"⬆️ Rising: <b>{s['rising']}</b>",
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _register_user(update)
    user_id = update.effective_user.id
    s = await db.get_user_settings(user_id)
    notify = "✅ On" if s.get("notify") else "❌ Off"
    min_score = s.get("min_score", settings.min_quality_score)
    tiers = ", ".join(s.get("tiers", []))
    await _reply(
        update,
        f"⚙️ <b>Your Settings</b>\n\n"
        f"🔔 Notifications: {notify}\n"
        f"📊 Min quality score: {min_score}\n"
        f"📂 Active tiers: {tiers}\n\n"
        "To change settings, use:\n"
        "<code>/settings notify off</code>\n"
        "<code>/settings score 60</code>",
    )
    # Simple arg parsing
    args = context.args or []
    if len(args) >= 2:
        key, val = args[0].lower(), args[1].lower()
        if key == "notify":
            await db.update_user_settings(user_id, {"notify": val == "on"})
            await _reply(update, f"✅ Notifications set to <b>{val}</b>.")
        elif key == "score":
            try:
                await db.update_user_settings(user_id, {"min_score": int(val)})
                await _reply(update, f"✅ Min quality score set to <b>{val}</b>.")
            except ValueError:
                await _reply(update, "❌ Invalid score. Use a number, e.g. /settings score 60")


# ── Callback query handlers ──────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id

    if data.startswith("fav:"):
        vid_id = data[4:]
        added = await db.add_favorite(user_id, vid_id)
        text = "⭐ Added to favourites!" if added else "Already in favourites."
        await query.answer(text, show_alert=False)
        # Update button to "unfavourite"
        try:
            video = await db.get_db().videos.find_one({"video_id": vid_id})
            if video:
                await query.edit_message_reply_markup(build_keyboard(video, is_fav=True))
        except Exception:
            pass

    elif data.startswith("unfav:"):
        vid_id = data[6:]
        removed = await db.remove_favorite(user_id, vid_id)
        text = "💔 Removed from favourites." if removed else "Not in favourites."
        await query.answer(text, show_alert=False)
        try:
            video = await db.get_db().videos.find_one({"video_id": vid_id})
            if video:
                await query.edit_message_reply_markup(build_keyboard(video, is_fav=False))
        except Exception:
            pass

    elif data.startswith("detail:"):
        vid_id = data[7:]
        video = await db.get_db().videos.find_one({"video_id": vid_id})
        if not video:
            await query.answer("Video not found in database.", show_alert=True)
            return
        text = (
            f"🔍 <b>Classification Details</b>\n\n"
            f"🆔 ID: <code>{vid_id}</code>\n"
            f"✅ Is Gadget: {video.get('is_gadget')}\n"
            f"🎯 Gadget Score: {video.get('gadget_score')}/100\n"
            f"📊 Quality Score: {video.get('quality_score')}/100\n"
            f"⚡ Virality Score: {video.get('virality_score')}/100\n"
            f"🏷 Tier: {video.get('view_tier', '—')}\n"
            f"❌ Reject Reason: {video.get('reject_reason') or 'None'}\n"
            f"🔧 Product Confidence: {video.get('product_confidence')}/100\n"
            f"📅 Processed: {video.get('processed_at', '—')}"
        )
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)

    elif data == "skip:":
        await query.answer("Skipped.", show_alert=False)

    elif data == "export_fav":
        favs = await db.get_favorites(user_id, limit=200)
        if not favs:
            await query.answer("No favourites to export.", show_alert=True)
            return
        csv_bytes = videos_to_csv(favs)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=io.BytesIO(csv_bytes),
            filename="gadgetbot_favourites.csv",
            caption=f"📥 Your {len(favs)} favourite videos exported as CSV.",
        )


# ── Scheduled job ────────────────────────────────────────────────────────────

async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every scan_interval_minutes and pushes top new videos to the channel."""
    logger.info("⏱ Scheduled scan starting…")
    try:
        new_videos = await run_pipeline()
        to_send = sorted(new_videos, key=lambda v: v.get("quality_score", 0), reverse=True)[
            : settings.max_videos_per_scan
        ]
        for v in to_send:
            ok = await send_video(context.bot, v, chat_id=settings.telegram_chat_id)
            if ok:
                await db.mark_video_sent(v["video_id"])
        logger.info("Scheduled scan done — sent %d videos", len(to_send))
    except Exception:
        logger.exception("Scheduled scan error")


# ── Application setup & run ──────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await db.connect()
    await start_health_server()
    logger.info("Bot started. Commands registered.")


async def post_shutdown(app: Application) -> None:
    await db.disconnect()
    
async def health(request):
    return web.Response(text="OK")


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info(f"Health server running on port {port}")



def main() -> None:
    token = settings.telegram_bot_token
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("viral", cmd_viral))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("rising", cmd_rising))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("settings", cmd_settings))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # Scheduler
    interval = settings.scan_interval_minutes * 60
    app.job_queue.run_repeating(scheduled_scan, interval=interval, first=60)

    logger.info(
        "GadgetBot polling — scan every %d min", settings.scan_interval_minutes
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
