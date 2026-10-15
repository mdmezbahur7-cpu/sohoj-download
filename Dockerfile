FROM denoland/deno:bin AS deno
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DENO_DIR=/tmp/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt && useradd --create-home appuser
COPY cloud.py egress.py gunicorn.conf.py ./
COPY web ./web
USER appuser
EXPOSE 10000
CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 8 --bind 0.0.0.0:${PORT:-10000} --timeout 120 --graceful-timeout 15 --error-logfile - cloud:app"]
