# CI / GitHub Actions

## Описание

Workflow (`ci.yml`) запускается при пуше в `main` и на каждом Pull Request.

## Что проверяется

| Job | Что делает |
|---|---|
| `test` | Устанавливает зависимости, запускает `ruff check` (lint) и `pytest` для Python 3.11 и 3.12 |
| `docker` | Собирает Docker-образ и проверяет, что контейнер стартует (только на пуше в main) |

## Как включить

Workflow уже лежит в `.github/workflows/ci.yml`. GitHub Actions подхватит его автоматически.

> **Примечание:** Если в списке проверок PR показывает "no checks reported" —
> это ожидаемо, если workflow был добавлен в PR, но ещё не смержен в `main`.
> После мержа проверки начнут отрабатывать.

## Локальный запуск

```bash
# Линтер
ruff check autostars/ bot/ tests/

# Тесты
pytest tests/ -v
```

## Добавление новых проверок

Добавьте стэп в `.github/workflows/ci.yml` → коммит → PR → workflow запустится автоматически.
