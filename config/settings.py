"""
Central configuration loaded from environment variables via pydantic-settings.
All fields have sane defaults so the bot fails loudly only when a truly
required secret is absent at runtime.
"""
from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram ────────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(..., description="BotFather token")
    telegram_chat_id: str = Field(..., description="Channel or group chat ID")

    # ── MongoDB Atlas ────────────────────────────────────────────────────────
    mongodb_uri: str = Field(..., description="MongoDB Atlas connection string")
    mongodb_db: str = Field("gadget_bot", description="Database name")

    # ── Gemini AI ────────────────────────────────────────────────────────────
    gemini_api_key: str | None = Field(None, description="Google Gemini API key")
    gemini_model: str = Field(
        "gemini-2.0-flash",
        description="Gemini model name",
    )

    # ── YouTube Playwright scraper ───────────────────────────────────────────
    youtube_search_queries: List[str] = Field(
        default=[
            "gadget review shorts",
            "cool gadget shorts",
            "kitchen gadget shorts",
            "smart home gadget shorts",
            "travel gadget shorts",
            "camping gadget shorts",
            "phone accessory shorts",
            "desk gadget shorts",
            "novelty gadget shorts",
        ],
        description="Search queries used to scrape YouTube Shorts",
    )
    youtube_max_results: int = Field(20, description="Results per query")
    # Playwright browser: chromium, firefox, or webkit
    playwright_browser: str = Field("chromium", description="Playwright browser engine")
    # Seconds to wait for YouTube search results to load
    playwright_timeout_ms: int = Field(15_000, description="Playwright navigation timeout (ms)")

    # ── TikTok hashtags ──────────────────────────────────────────────────────
    tiktok_hashtags: List[str] = Field(
        default=[
            "gadget",
            "coolgadget",
            "kitchengadget",
            "smarthome",
            "travelgadget",
            "techgadget",
            "gadgetreview",
        ],
        description="TikTok hashtags to scrape",
    )
    tiktok_max_per_hashtag: int = Field(15, description="Max videos per hashtag")

    # ── Pipeline thresholds ──────────────────────────────────────────────────
    min_gadget_score: int = Field(50, description="Minimum AI gadget score to keep")
    min_quality_score: int = Field(40, description="Minimum composite quality score")
    scan_interval_minutes: int = Field(30, description="Automatic scan interval")
    max_videos_per_scan: int = Field(5, description="Max videos sent per scan cycle")

    # ── View-tier thresholds ────────────────────────────────────────────────
    viral_views: int = Field(500_000)
    trending_views: int = Field(100_000)
    rising_views: int = Field(10_000)

    # ── Reject keywords ─────────────────────────────────────────────────────
    reject_keywords: List[str] = Field(
        default=[
            "gaming",
            "minecraft",
            "fortnite",
            "iphone review",
            "macbook review",
            "laptop review",
            "crypto",
            "politics",
            "podcast",
            "software",
        ],
        description="Hard-reject keywords for fallback classifier",
    )

    @property
    def view_tiers(self) -> dict:
        return {
            "viral": self.viral_views,
            "trending": self.trending_views,
            "rising": self.rising_views,
        }


settings = Settings()
