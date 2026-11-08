# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADINGBOT_DB=/data/tradingbot.sqlite3

WORKDIR /app
COPY requirements-server.txt ./

# Cloud builds can mount their trusted, combined CA bundle as proxy_ca.
# Ordinary Fly builds use the image's system roots. Never disable TLS checks.
RUN --mount=type=secret,id=proxy_ca,required=false \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python3 -m pip install --no-cache-dir --disable-pip-version-check -r requirements-server.txt

COPY tradingbot/ ./tradingbot/
COPY dashboard/ ./dashboard/
COPY deploy/entrypoint.py ./deploy/entrypoint.py
# Managed checkouts may have private (0600/0700) modes. The unprivileged
# runtime needs read/traverse access to source, without owning writable code.
RUN chmod -R a+rX /app/tradingbot /app/dashboard /app/deploy

EXPOSE 8080
STOPSIGNAL SIGTERM

# The bootstrap owns only /data itself, then drops to numeric UID/GID 10001.
# The web server, SQLite connections, and background worker run without root.
ENTRYPOINT ["python3", "/app/deploy/entrypoint.py"]
CMD ["python3", "-m", "tradingbot", "serve", "--host", "0.0.0.0", "--port", "8080"]
