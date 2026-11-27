# mailflow-mcp, run as a sidecar next to MailFlow.
# Multi-stage: uv resolves the locked dependencies into a venv; the runtime image ships only the
# venv and server.py (no uv).

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
# The image's own CPython, never a downloaded one, so the venv points at /usr/local/bin/python3.13,
# which the runtime image has too.
ENV UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY app/pyproject.toml app/uv.lock ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/server.py ./
ENV PATH="/app/.venv/bin:$PATH"
# No root inside the container: the server needs no files and no privileged port.
USER 65534:65534
EXPOSE 8082
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/health', timeout=4).status == 200 else 1)"
# All configuration is via environment variables (README).
CMD ["python", "server.py"]
