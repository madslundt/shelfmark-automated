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

# Run as non-root user; create /data so state.db can be written without a volume mount
RUN useradd -m -u 1000 appuser && chown -R appuser /app && mkdir -p /data && chown appuser /data
USER appuser

ENV PATH="/app/.venv/bin:$PATH"

# -u forces unbuffered stdout so Docker log tailing works immediately
CMD ["python", "-u", "main.py"]
