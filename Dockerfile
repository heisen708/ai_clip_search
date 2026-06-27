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

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Install Playwright browser binaries into the image
# (playwright package is already in /usr/local from builder stage)


# Copy application source
COPY . .

# Koyeb reads PORT from environment; default to 8080 for health-check probes.
# The bot itself is long-polling (no HTTP server), so we expose a minimal
# health-check endpoint via the HEALTHCHECK instruction.
ENV PORT=8080
EXPOSE 8080

# Non-root user for security
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# Start the bot
CMD ["python", "bot.py"]
