FROM python:3.14-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install production dependencies only (dev group excluded)
COPY pyproject.toml uv.lock .
RUN uv export --frozen --no-group dev --no-hashes -o /tmp/requirements.txt && \
    uv pip install --system -r /tmp/requirements.txt

# Copy application source
COPY src/ ./src/
COPY main.py .

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

# -u forces unbuffered stdout so Docker log tailing works immediately
CMD ["python", "-u", "main.py"]
