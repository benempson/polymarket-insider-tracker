# =============================================================================
# Stage 1 — Builder: install Python dependencies with uv
# =============================================================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./

# Install production dependencies only (no dev extras)
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code and install the project itself
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# =============================================================================
# Stage 2 — Runtime: lean final image
# =============================================================================
FROM python:3.11-slim AS runtime

# Non-root user for container security
RUN useradd --system --uid 1001 --create-home appuser

WORKDIR /app

# Copy the virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application source and migration files
COPY --from=builder /app/src ./src
COPY --from=builder /app/alembic ./alembic
COPY --from=builder /app/alembic.ini ./

# Put the venv on PATH so `python` resolves to the venv's interpreter
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Health check port (configurable via HEALTH_PORT env var)
EXPOSE 8080

USER appuser

# Run database migrations then start the tracker
CMD ["sh", "-c", "alembic upgrade head && python -m polymarket_insider_tracker"]
