# syntax=docker/dockerfile:1
# Build natif sur Raspberry Pi (linux/arm64) ou cross-build :
#   docker buildx build --platform linux/arm64 -t cocobot .

# ---------- Stage 1 : compilation des wheels ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------- Stage 2 : image finale légère ----------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
RUN useradd --uid 1000 --create-home cocobot \
    && mkdir -p /data/logs && chown -R cocobot:cocobot /data
WORKDIR /app
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels
COPY --chown=cocobot:cocobot app ./app
USER cocobot
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('APP_PORT','12080'),timeout=3)" || exit 1
CMD ["python", "-m", "app.run"]
