FROM python:3.14-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install production dependencies only (dev group excluded)
COPY pyproject.toml uv.lock .
RUN uv sync --frozen --no-dev

# Copy application source
COPY src/ ./src/
COPY main.py .

# Install gosu for privilege dropping; create runtime user and data directory
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 appuser \
    && chown -R appuser /app \
    && mkdir -p /data && chown appuser /data

ENV PATH="/app/.venv/bin:$PATH"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Entrypoint runs as root to fix /data permissions then drops to appuser,
# ensuring state.db is writable even when a volume is mounted with root-owned root dir
ENTRYPOINT ["/entrypoint.sh"]
# -u forces unbuffered stdout so Docker log tailing works immediately
CMD ["python", "-u", "main.py"]
