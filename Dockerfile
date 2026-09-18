# AutoStars — образ боевого цикла автовыдачи.
#
#   docker compose up -d --build
#   docker compose logs -f
#   docker compose run --rm autostars --check
#
# Секреты в образ НЕ попадают: .env читается через env_file (docker-compose.yml),
# база и логи — в томе ./data.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AUTOSTARS_HOME=/app/data

WORKDIR /app

# зависимости ядра отдельно — слой кэшируется и не пересобирается от правки кода
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY autostars ./autostars
# пример конфигурации: ensure_env_file() копирует его в /app/data/.env, если .env нет
COPY .env.example ./

RUN mkdir -p /app/data && useradd --create-home --uid 10001 autostars \
    && chown -R autostars:autostars /app
USER autostars

VOLUME ["/app/data"]

# SIGTERM от `docker stop` → graceful shutdown (ждёт текущие выдачи, cfg: SHUTDOWN_TIMEOUT_SEC)
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=5m --timeout=40s --start-period=2m --retries=3 \
  CMD python -m autostars.main --check || exit 1

ENTRYPOINT ["python", "-m", "autostars.main"]
