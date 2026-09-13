FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    HF_HOME=/app/.hf-cache \
    # Applies to every pip invocation below: a slow/congested connection has
    # been dropping mid-download here, showing up as spurious "no matching
    # distribution" errors once pip gives up early.
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=5

WORKDIR /app

# CPU-only torch first: sentence-transformers would otherwise pull the CUDA
# wheels (~2.5GB) that a self-hosted CPU box never uses. Unpinned on purpose —
# this index only serves CPU builds, and pinning it here would fight the
# sentence-transformers pin in requirements.txt.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

COPY pyproject.toml alembic.ini ./
COPY migrations ./migrations
COPY src ./src
COPY tests ./tests

RUN pip install --no-deps -e .

# Production API, migrations, worker, and maintenance commands use this
# unprivileged account. New named volumes inherit these ownership bits on first
# mount; the development Compose file alone overrides the user for its legacy
# host data bind mount.
RUN addgroup --system --gid 10001 mouseion \
    && adduser --system --uid 10001 --ingroup mouseion --home /app --no-create-home mouseion \
    && mkdir -p /app/data /app/backups /app/.hf-cache \
    && chown -R mouseion:mouseion /app/data /app/backups /app/.hf-cache

USER mouseion

EXPOSE 8000

CMD ["uvicorn", "mouseion.main:app", "--host", "0.0.0.0", "--port", "8000"]
