FROM python:3.12-slim

# Create a non-root user so the bot does not run as root in the container.
RUN groupadd --system mombot && useradd --system --gid mombot mombot

WORKDIR /app

# Copy only the files needed for installation first (layer-cache friendly).
COPY pyproject.toml ./
COPY src/ ./src/

# Install the package in editable mode so the src-layout is on sys.path.
# We install as root here only for the pip step; the process runs as mombot.
RUN pip install --no-cache-dir -e .

# Switch to the non-root user for runtime.
USER mombot

# `python -m mom_bot` invokes src/mom_bot/__main__.py.
# The real Discord client will be wired in Epic 0.3 (issue #13).
CMD ["python", "-m", "mom_bot"]
