"""AutoStars — автовыдача Telegram Stars на FunPay через GAMEAU B2B API.

Пакет содержит только поддерживаемый движок:

    autostars.main      — CLI и главный цикл (python -m autostars.main)
    autostars.config    — конфигурация (.env / config.json / переменные окружения)
    autostars.clients   — FunPay и GAMEAU
    autostars.database  — SQLite (заказы, журнал задач, настройки)
    autostars.services  — парсер, пайплайн выдачи, статистика, трекинг, контроль
    autostars.notifier  — Telegram-алерты и интерактивные команды
    autostars.gui       — настольный интерфейс (flet), управляющий этим же движком

Несовместимые исторические реализации лежат в `legacy/` и не поддерживаются.
"""

from __future__ import annotations

import contextlib

__all__ = ["__version__"]

__version__ = "2.3.0"

with contextlib.suppress(Exception):  # версия из упаковки (pip install -e .)
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("autostars") or __version__
