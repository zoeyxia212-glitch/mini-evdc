# syntax=docker/dockerfile:1
FROM python:3.15-slim

LABEL org.opencontainers.image.title="Mini EvDC" \
      org.opencontainers.image.description="Enterprise virtual data center + multi-cloud integration service" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY evdc ./evdc
# Pull in Debian security fixes released after the base image was built (e.g. zlib CVE-2026-85091)
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# setuptools, wheel and pip are only needed to build the package, so remove them from the
# runtime image (old vendored copies get flagged by scanners, e.g. CVE-2025-47273).
# pip's SBOM lists the setuptools it vendors, so pip and ensurepip go too.
RUN pip install --upgrade pip \
    && pip install . \
    && (pip uninstall -y setuptools wheel || true) \
    && rm -rf /usr/local/lib/python3*/site-packages/setuptools* \
              /usr/local/lib/python3*/site-packages/pkg_resources \
              /usr/local/lib/python3*/site-packages/_distutils_hack \
              /usr/local/lib/python3*/site-packages/distutils-precedence.pth \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3*/ensurepip \
    && useradd --create-home --uid 10001 evdc \
    && mkdir /data && chown evdc /data

# runs as a fixed non-root UID so Kubernetes runAsNonRoot / runAsUser policies can check it
USER 10001

# credentials (cloud keys, EVDC_ADMIN_KEY) come from the environment, never baked into the image
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2).status == 200 else 1)"
# exec form: the Python process is PID 1 and receives SIGTERM directly (graceful shutdown)
CMD ["evdc", "serve", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/evdc.db", "--sync-interval", "300", "--config", "/config/accounts.json"]
