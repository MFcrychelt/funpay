FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot ./bot

# Конфигурация монтируется/копируется отдельно (секреты не в образе):
#   config.json — настройки, .env — ключи.
CMD ["python", "main.py"]
