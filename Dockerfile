FROM python:3.14-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies using uv (layer-cached before source copy)
COPY pyproject.toml uv.lock .
RUN uv sync --frozen --no-dev

# Copy application source
COPY src/ ./src/
COPY main.py .

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser /app
USER appuser

# -u forces unbuffered stdout so Docker log tailing works immediately
CMD ["uv", "run", "python", "-u", "main.py"]
