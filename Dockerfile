# OntoKit API Dockerfile
FROM python:3.13-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies (including libgit2 for pygit2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    libgit2-dev \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and git repos directory
RUN useradd --create-home --shell /bin/bash ontokit && \
    mkdir -p /data/repos && \
    chown -R ontokit:ontokit /data/repos
WORKDIR /home/ontokit/app

# Install locked runtime dependencies into the system Python environment.
COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /usr/local/bin/uv
# Pre-create the package directory: a nested COPY --chmod=0644 would otherwise
# create its parent without traversal permission for the runtime user.
RUN mkdir -m 0755 ontokit
COPY --chmod=0644 pyproject.toml uv.lock README.md ./
COPY --chmod=0644 ontokit/version.py ./ontokit/version.py
# --frozen skips freshness validation, so check for manifest drift first.
RUN uv lock --check --offline && \
    uv export --frozen --no-dev --no-hashes --no-emit-project \
        --output-file /tmp/requirements.txt && \
    python -m pip install --no-deps -r /tmp/requirements.txt && \
    python -m pip install --no-deps . && \
    sha256sum uv.lock | cut -d ' ' -f 1 > /home/ontokit/app/.uv-lock.sha256 && \
    chmod 0644 /home/ontokit/app/.uv-lock.sha256 && \
    rm /tmp/requirements.txt

# Copy application code
COPY --chown=ontokit:ontokit ontokit/ ./ontokit/

# Copy alembic configuration for migrations
COPY --chown=ontokit:ontokit --chmod=0644 alembic.ini ./
COPY --chown=ontokit:ontokit alembic/ ./alembic/

# Copy entrypoint script (runs migrations before starting the app)
COPY --chown=ontokit:ontokit --chmod=0644 scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=ontokit:ontokit --chmod=0644 scripts/rewrap_pr_party_credentials.py /usr/local/bin/rewrap-pr-party-credentials
RUN chmod +x /usr/local/bin/entrypoint.sh

# Switch to non-root user
USER ontokit

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Auto-migrate on startup, then run the app
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "ontokit.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
