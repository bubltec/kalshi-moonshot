FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# pg_dump (for the nightly database backup). It must be at least the database server's major version, so the client comes from the Postgres
# project's own apt repository (version 18, matching the database service) instead of Debian's older one.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-common ca-certificates \
    && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*
# Runtime dependencies come from the lock file so a rebuild cannot silently pick up new versions.
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .
# The bot never needs root. /data is the mounted volume.
RUN useradd --create-home --uid 1000 bot && mkdir /data /tape && chown bot /data /tape
USER bot
ENV DATA_DIR=/data
VOLUME ["/data", "/tape"]
CMD ["python", "-m", "kalshi_moonshot", "run"]
# Runs the bot. Ollama is expected on the host (Apple GPU); see README.
# On a Linux server, start the bundled Ollama instead:  docker compose --profile ollama up -d
