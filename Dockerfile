FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Основной движок автовыдачи (async)
COPY autostars ./autostars

# Конфигурация монтируется/копируется отдельно (секреты не в образе):
#   .env — ключи и параметры (см. .env.example)
CMD ["python", "-m", "autostars.main"]
