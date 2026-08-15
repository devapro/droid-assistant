# Two stages: the client is built with Node and copied in, so the runtime image
# carries no Node runtime at all (SRS §6.2 — one deployable, one port).
FROM node:22-alpine AS client
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# Vite writes to ../src/droid_assistant/static, i.e. /src/... in this stage.
RUN npm run build

FROM python:3.12-slim AS runtime

# ffmpeg is what turns session audio into Opus rather than WAV; libgomp is
# needed by CTranslate2 and ONNX Runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    DROID_DATA_DIR=/data \
    DROID_MODELS_DIR=/models \
    DROID_LOG_JSON=1

# Dependencies first, and without the project itself, so editing a source file
# does not re-resolve or re-download several gigabytes of wheels.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra local --extra cloud

COPY src/ ./src/
COPY eval/ ./eval/
# The built client is force-included in the wheel, so it must be present before
# the project itself is installed.
COPY --from=client /src/droid_assistant/static ./src/droid_assistant/static
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra local --extra cloud

ENV PATH="/app/.venv/bin:$PATH"

# Models and data are volumes: the image stays small, and rebuilding it never
# costs a re-download or a lost session.
VOLUME ["/data", "/models"]
EXPOSE 8000

# Binding to 0.0.0.0 inside a container is correct — the container boundary is
# the isolation. Publish the port only to where you want it reachable.
ENV DROID_SERVER__HOST=0.0.0.0

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["droid-assistant"]
CMD ["serve"]
