# 开仓控制器 — container image for free cloud hosting (Render free web service).
# Defaults: MODE=mock, live trading disabled. No secrets are baked in; users
# enter their own Binance API key in their own browser.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODE=mock \
    ALLOW_LIVE=no \
    HOST=0.0.0.0 \
    PORT=8787 \
    EPHEMERAL_STORAGE=yes \
    TRUST_PROXY=yes \
    REGION_PROBE=yes

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser

COPY app ./app
COPY static ./static
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data

USER appuser
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8787'),timeout=4)" || exit 1

# Render injects $PORT; shell form so it expands at runtime.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8787} --proxy-headers --forwarded-allow-ips="*"
