# 课程后端：依赖在镜像里装好，源码运行时挂载进来，改了立刻重载。
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 只复制锁文件装依赖：源码变化不会让这一层失效。
COPY backend/pyproject.toml backend/uv.lock /app/backend/
RUN uv sync --project /app/backend --frozen --no-install-project

# 源码由 compose 挂载；这里只声明它在包路径上。
ENV PYTHONPATH=/app/backend/src:/app

EXPOSE 8000
CMD ["uvicorn", "--factory", "flowpay.cli:app_factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app/backend/src"]
