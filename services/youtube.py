"""
YouTube Shorts scraper using Playwright.

Replaces the YouTube Data API v3 entirely — no API key required.
Uses a headless Chromium browser (via playwright-async) to search YouTube
the same way a real user would, then parses the rendered HTML for video
cards from the Shorts shelf and regular search results.

Koyeb deployment note
---------------------
The Docker image must have the Playwright browser binaries installed.
Add to your Dockerfile (after `pip install playwright`):

    RUN playwright install chromium --with-deps

Or if using a pre-built image, set the environment variable:

    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any
from urllib.parse import quote_plus, urlencode

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from config.settings import settings

logger = logging.getLogger("gadgetbot.services.youtube")

# ── Constants ─────────────────────────────────────────────────────────────────

_YT_SEARCH_BASE = "https://www.youtube.com/results?"
_SHORTS_URL_RE = re.compile(r"https://www\.youtube\.com/shorts/([\w-]+)")

# How long to wait (ms) for the search-results container to appear
_NAV_TIMEOUT = settings.playwright_timeout_ms

# Selector that signals the results have rendered
_RESULTS_SELECTOR = "ytd-video-renderer, ytd-reel-item-renderer, ytd-shorts"

# Number of times to scroll down to load more results
_SCROLL_PASSES = 2

# Concurrency cap — YouTube will soft-ban parallel headless sessions quickly
_SEM = asyncio.Semaphore(1)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _shorts_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def _parse_view_count(raw: str) -> int:
    """
    Turn strings like '1.2M views', '345K views', '12,345 views' into ints.
    Returns 0 on parse failure.
    """
    raw = raw.strip().lower().replace(",", "").replace(" views", "").replace(" view", "")
    try:
        if raw.endswith("m"):
            return int(float(raw[:-1]) * 1_000_000)
        if raw.endswith("k"):
            return int(float(raw[:-1]) * 1_000)
        return int(raw) if raw.isdigit() or raw.replace(".", "").isdigit() else 0
    except (ValueError, TypeError):
        return 0


def _stable_id(video_id: str) -> str:
    return f"yt_{video_id}"


# ── Page extraction helpers ───────────────────────────────────────────────────

async def _extract_from_search_page(page: Page, max_results: int) -> list[dict[str, Any]]:
    """
    Parse video cards from a YouTube search-results page that is already loaded.
    Handles both ytd-video-renderer (standard) and ytd-reel-item-renderer (Shorts shelf).
    """
    videos: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # ── Standard video renderers ──────────────────────────────────────────
    renderers = await page.query_selector_all("ytd-video-renderer")
    for el in renderers:
        if len(videos) >= max_results:
            break
        try:
            # Title + URL
            title_el = await el.query_selector("#video-title")
            if not title_el:
                continue
            title = (await title_el.inner_text()).strip()
            href = await title_el.get_attribute("href") or ""

            # Only keep Shorts
            m = _SHORTS_URL_RE.search(href)
            if not m:
                # Try /watch?v= links — skip non-Shorts
                continue
            video_id = m.group(1)

            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)

            # Thumbnail
            thumb_el = await el.query_selector("img#img")
            thumbnail = await thumb_el.get_attribute("src") if thumb_el else ""

            # Channel name
            channel_el = await el.query_selector(
                "ytd-channel-name yt-formatted-string a, "
                "#channel-name a, "
                ".ytd-channel-name a"
            )
            channel = (await channel_el.inner_text()).strip() if channel_el else ""

            # Views + upload time from metadata line
            meta_el = await el.query_selector(
                "#metadata-line span.inline-metadata-item, "
                "ytd-video-meta-block #metadata-line span"
            )
            meta_texts: list[str] = []
            meta_els = await el.query_selector_all(
                "#metadata-line span.inline-metadata-item"
            )
            for m_el in meta_els:
                t = (await m_el.inner_text()).strip()
                if t:
                    meta_texts.append(t)

            views_raw = meta_texts[0] if meta_texts else "0"
            upload_time = meta_texts[1] if len(meta_texts) > 1 else ""
            views = _parse_view_count(views_raw)

            videos.append({
                "video_id": _stable_id(video_id),
                "source": "youtube",
                "url": _shorts_url(video_id),
                "title": title,
                "description": "",
                "hashtags": "",
                "channel": channel,
                "thumbnail": thumbnail or "",
                "upload_time": upload_time,
                "views": views,
                "likes": 0,
                "comments": 0,
                "duration_seconds": 45,  # Shorts are ≤60 s; heuristic default
                "has_thumbnail": bool(thumbnail),
            })
        except Exception:
            logger.debug("Failed to parse video renderer", exc_info=True)

    # ── Shorts shelf / reel renderers ─────────────────────────────────────
    reel_renderers = await page.query_selector_all("ytd-reel-item-renderer")
    for el in reel_renderers:
        if len(videos) >= max_results:
            break
        try:
            href_el = await el.query_selector("a#thumbnail")
            if not href_el:
                continue
            href = await href_el.get_attribute("href") or ""
            m = _SHORTS_URL_RE.search(href)
            if not m:
                vid_match = re.search(r"/shorts/([\w-]+)", href)
                if not vid_match:
                    continue
                video_id = vid_match.group(1)
            else:
                video_id = m.group(1)

            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)

            title_el = await el.query_selector(
                "#video-title, yt-formatted-string#video-title, span#video-title"
            )
            title = (await title_el.inner_text()).strip() if title_el else ""

            thumb_el = await el.query_selector("img")
            thumbnail = await thumb_el.get_attribute("src") if thumb_el else ""

            views_el = await el.query_selector(
                "p.ytd-reel-item-renderer, #metadata-line span, [aria-label]"
            )
            views_raw = ""
            if views_el:
                aria = await views_el.get_attribute("aria-label") or ""
                inner = (await views_el.inner_text()).strip()
                views_raw = aria or inner

            videos.append({
                "video_id": _stable_id(video_id),
                "source": "youtube",
                "url": _shorts_url(video_id),
                "title": title,
                "description": "",
                "hashtags": "",
                "channel": "",
                "thumbnail": thumbnail or "",
                "upload_time": "",
                "views": _parse_view_count(views_raw),
                "likes": 0,
                "comments": 0,
                "duration_seconds": 45,
                "has_thumbnail": bool(thumbnail),
            })
        except Exception:
            logger.debug("Failed to parse reel renderer", exc_info=True)

    return videos


# ── Per-query scraper ─────────────────────────────────────────────────────────

async def _scrape_query(
    context: BrowserContext,
    query: str,
    max_results: int,
) -> list[dict[str, Any]]:
    """Open a new tab, load YouTube search, scroll, and extract results."""
    page = await context.new_page()
    try:
        params = urlencode({"search_query": query, "sp": "EgQIAxAB"})  # sp filter = Shorts
        url = _YT_SEARCH_BASE + params
        logger.debug("Scraping YouTube: %s", url)

        await page.goto(url, timeout=_NAV_TIMEOUT, wait_until="domcontentloaded")

        # Wait for at least one result card
        try:
            await page.wait_for_selector(_RESULTS_SELECTOR, timeout=_NAV_TIMEOUT)
        except Exception:
            logger.warning("Timed out waiting for results for query %r", query)
            return []

        # Scroll a couple of times to surface more cards
        for _ in range(_SCROLL_PASSES):
            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await page.wait_for_timeout(1_500)

        return await _extract_from_search_page(page, max_results)

    except Exception:
        logger.exception("Playwright scrape failed for query %r", query)
        return []
    finally:
        await page.close()


# ── Public entry point ────────────────────────────────────────────────────────

async def fetch_youtube_videos() -> list[dict[str, Any]]:
    """
    Iterate over configured search queries using a single Playwright browser
    session, dedup by video_id, and return enriched metadata dicts ready for
    classification.

    All failures are caught per-query so one bad page never aborts the run.
    """
    seen: set[str] = set()
    all_videos: list[dict] = []

    browser_type = settings.playwright_browser  # "chromium" | "firefox" | "webkit"

    async with _SEM:  # serialise browser sessions to avoid rate-limiting
        async with async_playwright() as pw:
            browser_engine = getattr(pw, browser_type)
            browser: Browser = await browser_engine.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--single-process",          # important for Koyeb/Docker
                    "--no-zygote",
                ],
            )

            # One context with a realistic UA; stealth-ish but no extension needed
            context: BrowserContext = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
            )

            # Block images/fonts/media to load pages faster
            await context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in ("image", "font", "media")
                    else route.continue_()
                ),
            )

            for query in settings.youtube_search_queries:
                try:
                    vids = await _scrape_query(
                        context, query, settings.youtube_max_results
                    )
                    for v in vids:
                        if v["video_id"] not in seen:
                            seen.add(v["video_id"])
                            all_videos.append(v)
                    # Brief pause between queries to be polite
                    await asyncio.sleep(2)
                except Exception:
                    logger.exception("Query failed: %r", query)

            await context.close()
            await browser.close()

    logger.info("YouTube (Playwright): fetched %d candidate videos", len(all_videos))
    return all_videos
