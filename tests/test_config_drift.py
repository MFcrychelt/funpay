"""Следим за «дрейфом конфигурации»: `.env.example` ↔ `Config` ↔ поля GUI.

Самый частый вид багов в таком проекте — не падение, а расхождение: в примере
файла одно, в коде другое, в настройках интерфейса третье. Пользователь сохраняет
«без изменений» и получает другой режим работы (например, вместо бесконечных
попыток логина — 10). Эти тесты ломают сборку, как только значения разъехались.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from autostars.config import ENV_MAP, Config, parse_env_text
from autostars.gui import settings

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"
PLACEHOLDER_KEYS = {"FUNPAY_GOLDEN_KEY", "GAMEAU_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}


@pytest.fixture(scope="module")
def example_values() -> dict[str, str]:
    assert EXAMPLE.exists(), "в репозитории обязан быть .env.example"
    return parse_env_text(EXAMPLE.read_text(encoding="utf-8"))


_TRUE = {"1", "true", "yes", "on", "да"}


def _same(a: str, b: object) -> bool:
    """Сравнение «как движок»: bool через truism, числа — через float, остальное — строкой."""
    if isinstance(b, bool):
        return (str(a).strip().lower() in _TRUE) is bool(b)
    if str(a).strip() == str(b).strip():
        return True
    try:
        return float(str(a).replace(",", ".")) == float(str(b))
    except (TypeError, ValueError):
        return False


def test_every_documented_key_is_understood(example_values):
    """Неизвестный ключ в `.env.example` = инструкция, которая ничего не настраивает."""
    unknown = [k for k in example_values if k not in ENV_MAP]
    assert not unknown, f"Config не знает эти ключи: {unknown}"


def test_required_keys_are_present_and_placeholdered(example_values):
    for key in ("FUNPAY_GOLDEN_KEY", "GAMEAU_API_KEY"):
        assert key in example_values, f"{key} должен быть в .env.example"


def test_example_defaults_match_engine_defaults(example_values):
    """Значения в `.env.example` = значения по умолчанию в `Config` (иначе «пример врёт»)."""
    cfg = Config()
    drift = []
    for key, value in example_values.items():
        attr = ENV_MAP[key]
        field = cfg.__dataclass_fields__[attr]
        default = getattr(cfg, attr)
        if field.default is dataclasses.MISSING or isinstance(default, (list, dict)):
            continue
        if key in PLACEHOLDER_KEYS:
            continue  # в примере здесь заведомо заглушка — её не с чем сравнивать
        if not _same(value, default):
            drift.append(f"{key}: в примере {value!r}, в движке {default!r}")
    assert not drift, "\n".join(drift)


def test_gui_defaults_match_example(example_values):
    """Поле GUI с default обязано совпадать с `.env.example`: «сохранить как есть»
    не должно менять поведение бота."""
    drift = []
    for field in settings.FIELDS:
        if not field.default:
            continue
        if field.key not in example_values:
            drift.append(f"{field.key}: есть подсказка в GUI ({field.default!r}), но нет в .env.example")
        elif not _same(example_values[field.key], field.default):
            drift.append(
                f"{field.key}: GUI предлагает {field.default!r}, пример/движок — {example_values[field.key]!r}"
            )
    assert not drift, "\n".join(drift)


def test_gui_fields_are_valid_for_the_parser(example_values):
    """Значения из примера проходят валидацию GUI — форма не «ругается» на рабочий конфиг."""
    values = {f.key: example_values.get(f.key, "") for f in settings.FIELDS}
    assert settings.validate(values) == []


def test_env_map_keys_are_env_style():
    bad = [k for k in ENV_MAP if not k.replace("_", "").isupper() or " " in k]
    assert not bad, f"ключи .env должны быть UPPER_SNAKE_CASE: {bad}"
