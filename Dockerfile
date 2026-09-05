# SentinelAI backend — multi-stage build.
#
# Two stages, one purpose each: `builder` compiles wheels (some dependencies need
# a C toolchain), `runtime` copies only the installed packages. The compiler never
# ships, which removes both ~400 MB and a large class of CVEs from the image that
# actually runs in production.
#
# Build from the repository root so the build context can see backend/:
#   docker build -f backend/Dockerfile -t sentinelai-api:local backend

# ---------------------------------------------------------------------------
# Stage 1 — build wheels
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential for any sdist that has no wheel for this platform. Removed
# along with the whole stage.
RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copied alone, before the source, so the dependency layer is cached and a code
# change does not reinstall the world.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# PYTHONUNBUFFERED so log lines reach the container's stdout immediately —
# without it a crash can lose the buffered lines that explain it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Defaults, not secrets. Every value in `app/core/config.py` that must not
    # appear in an image is read from the environment at boot and has no default.
    ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    WEB_CONCURRENCY=2

# curl for the HEALTHCHECK below. Nothing else is added: no shell utilities, no
# package manager cache, no compiler.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/*

# Runs as a non-root user with no login shell and no home directory to write to.
# A container process that does not need to write to its own filesystem should not
# be able to, which is also what makes `--read-only` viable at run time.
RUN groupadd --system --gid 10001 sentinel \
 && useradd --system --uid 10001 --gid sentinel --no-create-home \
            --shell /usr/sbin/nologin sentinel

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=sentinel:sentinel app ./app
COPY --chown=sentinel:sentinel pyproject.toml ./

USER sentinel

EXPOSE 8000

# Hits the liveness probe, which is deliberately dependency-free: it answers 200
# as long as the process is serving. Readiness — "are Supabase and the caches
# reachable" — is a separate endpoint, because a pod that cannot reach Supabase
# should stop receiving traffic without being killed and restarted.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:${PORT}/api/health/live || exit 1

# Uvicorn directly rather than behind Gunicorn: the workload is I/O-bound async
# code, so process management buys nothing that the orchestrator does not already
# provide, and one less supervisor is one less thing to misconfigure.
#
# --proxy-headers with --forwarded-allow-ips is required for the client IP used by
# rate limiting to be the real one behind a load balancer. It is scoped to the
# private ranges a sidecar or ingress would come from — trusting `*` would let any
# caller spoof `X-Forwarded-For` and dodge the limiter entirely.
CMD ["sh", "-c", "exec uvicorn app.main:app \
    --host ${HOST} \
    --port ${PORT} \
    --workers ${WEB_CONCURRENCY} \
    --proxy-headers \
    --forwarded-allow-ips '10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1' \
    --no-server-header \
    --timeout-keep-alive 30"]
