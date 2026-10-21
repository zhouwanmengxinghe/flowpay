FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_HTTP_TIMEOUT=120 \
    PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/backend/src:/app \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock /app/backend/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project backend --frozen --no-install-project --no-dev
COPY backend /app/backend
COPY scripts /app/scripts
COPY README.md /app/README.md
RUN mkdir /app/runtime && chown 10001:10001 /app/runtime
USER 10001
ENV FLOWPAY_DATA_DIR=/app/runtime
CMD ["uvicorn", "--factory", "flowpay.business.app:app_factory", "--host", "0.0.0.0", "--port", "8010"]
