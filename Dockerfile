# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# System packages needed to compile some Python wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Playwright system deps (chromium) — installed by the official helper script
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Chromium runtime libraries
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libxkbcommon0 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libgtk-3-0 \
        # Font support (avoids Chromium glyph-rendering warnings)
        fonts-liberation \
        # Misc
        ca-certificates \
        wget \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Install Playwright browser binaries into the image
# (playwright package is already in /usr/local from builder stage)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium --with-deps || \
    python -m playwright install chromium --with-deps

# Copy application source
COPY . .

# Koyeb reads PORT from environment; default to 8080 for health-check probes.
# The bot itself is long-polling (no HTTP server), so we expose a minimal
# health-check endpoint via the HEALTHCHECK instruction.
ENV PORT=8080
EXPOSE 8080

# Non-root user for security
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app /ms-playwright
USER botuser

# Start the bot
CMD ["python", "bot.py"]
