FROM python:3.12-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[postgres]"

COPY . .

RUN mkdir -p /app/runtime/storage/uploads /app/runtime/storage/results /app/runtime/storage/temp /app/runtime/logs

ENV ASR_PROJECT_ROOT=/app

EXPOSE 15797

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "15797"]
