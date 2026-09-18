"""Пути приложения: обычные запуск, venv и PyInstaller (--onefile, sys.frozen).

Проблема, которую это решает: exe, запущенный двойным кликом из проводника,
имеет рабочую папкой каталог по умолчанию, поэтому относительные пути
``.env`` / ``autostars.db`` / ``autostars.log`` ищутся «не там», и бот
выглядит сломанным. Здесь всё привязано к каталогу исполняемого файла.

  RESOURCE_DIR — только чтение: файлы, упакованные в exe (.env.example, ico.ico)
  APP_DIR      — запись: .env, config.json, autostars.db, autostars.log
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _module_dir() -> Path:
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Каталог с ресурсами (для PyInstaller — временный каталог _MEIPASS)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return _module_dir().parent


def app_dir() -> Path:
    """Каталог, рядом с которым лежат .env, БД и логи.

    Приоритет: переменная окружения AUTOSTARS_HOME (для systemd/Docker),
    затем каталог exe, затем каталог проекта.
    """
    override = os.environ.get("AUTOSTARS_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return resource_dir()


def resolve_user_path(raw: str | os.PathLike[str]) -> Path:
    """Абсолютный путь для записи: относительный трактуется относительно app_dir()."""
    path = Path(os.path.expandvars(str(raw))).expanduser()
    if not path.is_absolute():
        path = app_dir() / path
    return path


def find_env_file(filename: str = ".env") -> Path:
    """Где искать .env: AUTOSTARS_ENV > app_dir > cwd > корень ресурсов."""
    override = os.environ.get("AUTOSTARS_ENV", "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    candidates = [app_dir() / filename, Path.cwd() / filename, resource_dir() / filename]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return app_dir() / filename


def example_file(name: str) -> Path | None:
    """Первый существующий пример конфига (.env.example / config.example.json)."""
    for candidate in (app_dir() / name, Path.cwd() / name, resource_dir() / name):
        if candidate.exists():
            return candidate
    return None
