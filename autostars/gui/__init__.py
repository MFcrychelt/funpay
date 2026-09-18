"""Настольный интерфейс AutoStars (flet) поверх движка `autostars`.

Запуск:
    python -m autostars.gui          # или скрипт autostars-gui после pip install -e .

Слой `bridge`/`settings` от flet не зависит: они работают и в тестах, и в CLI.
`app` — единственное место, где нужен flet (опциональный extra `[gui]`).
"""

from __future__ import annotations

from .bridge import CommandResult, EngineProcess, engine_command, run_cli, status_snapshot

__all__ = [
    "CommandResult",
    "EngineProcess",
    "engine_command",
    "run_cli",
    "status_snapshot",
]
