FROM python:3.12-slim

# Create a non-root user so the bot does not run as root in the container.
RUN groupadd --system mombot && useradd --system --gid mombot mombot

WORKDIR /app

# Copy only the files needed for installation first (layer-cache friendly).
# Alembic migrations are NOT copied here — CI owns migration-apply after
# Phase 3 (#91).  The runtime image has no alembic.ini or migrations/.
COPY pyproject.toml ./
COPY uv.lock ./
COPY src/ ./src/

# Install the package and its runtime deps from the locked lockfile.
# --no-dev omits test/lint tooling from the image.
RUN pip install uv --no-cache-dir && uv sync --frozen --no-dev

# Switch to the non-root user for runtime.
USER mombot

# `python -m mom_bot` invokes src/mom_bot/__main__.py.
# The real Discord client will be wired in Epic 0.3 (issue #13).
CMD ["python", "-m", "mom_bot"]
