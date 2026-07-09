# ── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

# ── Runtime stage ───────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="arena-web2api" \
      org.opencontainers.image.description="OpenAI-compatible API for arena.ai" \
      org.opencontainers.image.source="https://github.com/tenmay/arena-web2api" \
      org.opencontainers.image.licenses="MIT"

# non-root user
RUN useradd --create-home --uid 1000 arena
WORKDIR /app

# copy deps
COPY --from=builder /install /usr/local

# copy app
COPY --chown=arena:arena . .

# data dir for conversation persistence
RUN mkdir -p /app/data && chown arena:arena /app/data
USER arena

ENV HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONVERSATION_STORE_FILE=/app/data/conversations.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:8000/health', timeout=3); sys.exit(0)" || exit 1

CMD ["python", "main.py"]
