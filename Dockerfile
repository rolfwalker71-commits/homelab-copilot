# syntax=docker/dockerfile:1
# Homelab Operations Copilot — linux/amd64 (x86_64 servers/PCs)
FROM --platform=linux/amd64 python:3.12-slim-bookworm AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM --platform=linux/amd64 python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=6655 \
    DATA_DIR=/data \
    MODULES_DIR=/app/modules \
    TZ=Europe/Berlin

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data /data/ssh \
    && chown -R appuser:appuser /data

COPY --from=builder /install /usr/local
COPY app /app/app
COPY modules /app/modules

USER appuser
EXPOSE 6655

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6655/api/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "6655"]
