# ---------- build stage (gcc for native extensions) ----------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt \
    && pip install --no-cache-dir --prefix=/install --upgrade yt-dlp

# ---------- runtime stage (no compiler, non-root) ----------
FROM python:3.11-slim

WORKDIR /app

# ffmpeg decodes every stream the music player sends, and libopus is what makes a
# **live** volume possible (with it the PCM path applies volume in-process; without
# it the volume is baked into ffmpeg's command line and a change re-renders the
# source). Neither is strictly mandatory — ``imageio-ffmpeg`` ships a static ffmpeg
# and an Opus-encoded stream needs no encoder from ``discord.py`` — which is why
# this image is the *upgrade* rather than the requirement. See
# ``utils/voice_caps.py`` for the probe that reports which of the two a host is.
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    ffmpeg \
    libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --no-create-home meowbot

COPY --from=builder /install /usr/local
COPY . .

# Bot writes bot.db into the working directory
RUN chown -R meowbot:meowbot /app

USER meowbot

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["python", "bot.py"]
