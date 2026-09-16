FROM python:3.11-slim

# Set up curl for HTTPS to yfinance / SEC / AlphaVantage
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Fly.io mounts a persistent volume at /data — see fly.toml.
# We point the SQLite cache there so it survives redeploys.
ENV CACHE_DB_PATH=/data/cache.db
ENV PORT=8080
ENV FLASK_ENV=production

EXPOSE 8080

# Gunicorn — one worker with threads is plenty for a demo; SSE streams
# work fine as long as gunicorn is thread-based (gthread) or async.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--worker-class", "gthread", \
     "-b", "0.0.0.0:8080", "--timeout", "300", "--access-logfile", "-", "app:app"]
