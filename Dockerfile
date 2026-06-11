FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.20 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
RUN mkdir -p reports exports

ENTRYPOINT ["uv", "run", "--frozen", "python", "aks_report.py"]
CMD ["--help"]
