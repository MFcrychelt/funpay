"""Настройка логирования: консоль + файл."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(log_file: str | None = None, verbose: bool = False) -> None:
    """Инициализирует логирование (идемпотентно)."""
    global _configured
    if _configured:
        return

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        root.addHandler(file_handler)

    # Приглушаем болтливые библиотеки.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
