"""Общие фикстуры тестов движка.

Путь к корню проекта подставляется и в самих тестах (их можно запускать напрямую),
но pytest-import всех модулей начинается отсюда — так `import autostars`
работает и без `pip install -e .`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def anyio_backend() -> str:
    """Движок асинхронный; прогоняем async-тесты на asyncio-бэкенде anyio."""
    return "asyncio"


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Конфигурация «в вакууме»: .env и БД в tmp, окружение очищено.

    Иначе тесты зависят от реального .env/переменных окружения разработчика
    и CI-агента — классический «у меня работало».
    """
    # Все ключи, которые понимает движок: иначе значение, попавшее в os.environ
    # в предыдущем тесте (через .env), молча переопределит конфигурацию следующего.
    from autostars.config import ENV_MAP

    for key in ENV_MAP:
        monkeypatch.delenv(key, raising=False)

    env_file = tmp_path / ".env"
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOSTARS_ENV", str(env_file))
    monkeypatch.setenv("AUTOSTARS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "autostars.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "autostars.log"))
    return tmp_path
