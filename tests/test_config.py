"""Тесты конфигурации: парсинг .env, приоритеты, безопасные числа, деньги.

Зачем это нужно (реальные поломки, которые здесь закреплены):
  • `POLL_INTERVAL=abc` раньше ронял процесс с голым ValueError — теперь
    значение игнорируется и попадает в `parse_errors`;
  • плейсхолдеры из .env.example («paste_your_…») считались настоящим значением,
    и бот уходил в сеть с мусорным ключом вместо внятной ошибки;
  • `DEFAULT_MAX_CHARGE_USDT` для заказа на 5000⭐ давал PRICE_EXCEEDED,
    то есть оплаченный заказ и спор на FunPay — лимит теперь считается от пакета.
"""

from __future__ import annotations

import json

import pytest

from autostars.config import Config, ensure_env_file, parse_env_text
from autostars.paths import app_dir, find_env_file, resolve_user_path


# --------------------------------------------------------------------------- #
# парсер .env
# --------------------------------------------------------------------------- #
def test_parse_env_text_basics():
    text = """
# комментарий
export FUNPAY_GOLDEN_KEY=abc123
GAMEAU_API_KEY="quoted-value"
POLL_INTERVAL=7.5   # inline-комментарий
EMPTY=
МНОГОСТРОЧНАЯ=игнор
"""
    values = parse_env_text(text)
    assert values["FUNPAY_GOLDEN_KEY"] == "abc123"
    assert values["GAMEAU_API_KEY"] == "quoted-value"
    assert values["POLL_INTERVAL"] == ["7.5  ", " inline-комментарий"][0].strip()
    assert values["EMPTY"] == ""


def test_parse_env_text_keeps_hashes_inside_quoted_value():
    values = parse_env_text('KEY="pa#ss#word"')
    assert values["KEY"] == "pa#ss#word"


# --------------------------------------------------------------------------- #
# устойчивость к битым значениям
# --------------------------------------------------------------------------- #
def test_bad_numeric_env_does_not_crash(isolated_env, monkeypatch):
    (isolated_env / ".env").write_text(
        "FUNPAY_GOLDEN_KEY=k\nGAMEAU_API_KEY=g\nPOLL_INTERVAL=abc\nSTUCK_TASK_MINUTES=12.9\n",
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.poll_interval == 5.0  # значение по умолчанию, не исключение
    assert cfg.stuck_task_minutes == 12
    assert any("POLL_INTERVAL" in message for message in cfg.parse_errors)


def test_placeholders_count_as_unset(isolated_env, monkeypatch):
    (isolated_env / ".env").write_text(
        "FUNPAY_GOLDEN_KEY=paste_your_funpay_golden_key_here\nGAMEAU_API_KEY=real-key\n",
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.gameau_key == "real-key"
    issues = cfg.issues()
    assert any("FUNPAY_GOLDEN_KEY" in issue for issue in issues)
    with pytest.raises(ValueError):
        cfg.validate()


def test_env_overrides_file_value(isolated_env):
    (isolated_env / ".env").write_text("EXCHANGE_VARIANT=1\nRATE_VARIANT_1=99\n", encoding="utf-8")
    cfg = Config.load()
    assert cfg.exchange_variant == 1
    assert cfg.active_usdt_rate == 99.0


def test_process_env_wins_over_file(isolated_env, monkeypatch):
    (isolated_env / ".env").write_text("EXCHANGE_VARIANT=1\n", encoding="utf-8")
    monkeypatch.setenv("EXCHANGE_VARIANT", "2")
    cfg = Config.load()
    assert cfg.exchange_variant == 2


def test_config_json_flat_and_sections(isolated_env):
    (isolated_env / "config.json").write_text(
        json.dumps(
            {
                "funpay_golden_key": "flat-key",
                "API": {"token": "gameau-from-section", "max_charge_usdt": 12.5},
                "FINANCE": {"rate_variant_2": 90.0, "hide_sender": True},
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.load()
    assert cfg.golden_key == "flat-key"
    assert cfg.gameau_key == "gameau-from-section"
    assert cfg.default_max_charge_usdt == 12.5
    assert cfg.rate_variant_2 == 90.0
    assert cfg.hide_sender is True


def test_broken_config_json_is_reported_not_fatal(isolated_env):
    (isolated_env / "config.json").write_text("{ это не json", encoding="utf-8")
    cfg = Config.load()
    assert any("config.json" in message for message in cfg.parse_errors)


def test_bool_parsing(isolated_env):
    (isolated_env / ".env").write_text("HIDE_SENDER=да\n", encoding="utf-8")
    assert Config.load().hide_sender is True
    (isolated_env / ".env").write_text("HIDE_SENDER=0\n", encoding="utf-8")
    assert Config.load().hide_sender is False


# --------------------------------------------------------------------------- #
# деньги: себестоимость и maxCharge
# --------------------------------------------------------------------------- #
def test_estimate_cost_scales_with_quantity(isolated_env):
    (isolated_env / ".env").write_text("USDT_PER_1000_STARS=10\n", encoding="utf-8")
    cfg = Config.load()
    assert cfg.estimate_cost_usdt(1000) == 10.0
    assert cfg.estimate_cost_usdt(250) == 2.5
    assert cfg.estimate_cost_usdt(0) == 0.0


def test_max_charge_covers_big_orders(isolated_env):
    """Дефолтный лимит 9.5 USDT не должен «резать» заказ на 5000⭐."""
    cfg = Config.load(isolated_env / "missing.json")
    small = cfg.compute_max_charge(1000, catalog_price_usdt=9.1)
    big = cfg.compute_max_charge(5000, catalog_price_usdt=45.5)
    assert small >= 9.1
    assert big >= 45.5, "лимит меньше цены пакета → PRICE_EXCEEDED и невыданный заказ"
    assert big > small


def test_max_charge_without_catalog_falls_back_to_estimate(isolated_env, monkeypatch):
    monkeypatch.setenv("USDT_PER_1000_STARS", "9.1")
    monkeypatch.setenv("DEFAULT_MAX_CHARGE_USDT", "9.5")
    monkeypatch.setenv("MAX_CHARGE_MARGIN_PCT", "5")
    cfg = Config.load()
    assert cfg.compute_max_charge(2000, None) >= 2 * 9.1


def test_negative_margin_is_clamped_and_reported(isolated_env):
    (isolated_env / ".env").write_text("MAX_CHARGE_MARGIN_PCT=-3\n", encoding="utf-8")
    cfg = Config.load()
    assert cfg.max_charge_margin == 0.0, "отрицательный запас обязан обрезаться до нуля"
    assert any("MAX_CHARGE_MARGIN_PCT" in issue for issue in cfg.issues())


# --------------------------------------------------------------------------- #
# секреты и диагностика
# --------------------------------------------------------------------------- #
def test_safe_dict_masks_secrets(isolated_env):
    (isolated_env / ".env").write_text(
        "FUNPAY_GOLDEN_KEY=supersecretgoldenkey\nGAMEAU_API_KEY=anothersecretkey\n"
        "TELEGRAM_BOT_TOKEN=***\n",
        encoding="utf-8",
    )
    cfg = Config.load()
    safe = cfg.to_safe_dict()
    for field in ("golden_key", "gameau_key", "telegram_bot_token"):
        assert "secret" not in safe[field] or safe[field].count("*") > 0
        assert "supersecretgoldenkey" not in json.dumps(safe)
        assert "anothersecretkey" not in json.dumps(safe)
    assert "…" in safe["golden_key"]


def test_summary_mentions_paths(isolated_env):
    cfg = Config.load()
    assert "DB:" in cfg.summary()


def test_issues_warn_on_aggressive_polling(isolated_env):
    (isolated_env / ".env").write_text("POLL_INTERVAL=0.2\n", encoding="utf-8")
    cfg = Config.load()
    assert any("POLL_INTERVAL" in issue for issue in cfg.issues())


# --------------------------------------------------------------------------- #
# пути и создание .env
# --------------------------------------------------------------------------- #
def test_paths_resolved_against_app_dir(isolated_env):
    cfg = Config.load()
    assert cfg.db_path.startswith(str(isolated_env))
    assert cfg.log_file.startswith(str(isolated_env))
    assert app_dir() == isolated_env
    assert find_env_file() == isolated_env / ".env"


def test_relative_paths_absolute_when_cwd_is_elsewhere(isolated_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Config.load()
    assert resolve_user_path("autostars.db").parent == isolated_env


def test_ensure_env_file_copies_example_and_nothing_if_absent(isolated_env):
    """Нет ни .env, ни config.json — создаём .env из примера рядом с приложением."""
    example = isolated_env / ".env.example"
    example.write_text("FUNPAY_GOLDEN_KEY=from-example\n", encoding="utf-8")

    created = ensure_env_file(quiet=True)
    assert created is not None and created.exists()
    assert created.read_text(encoding="utf-8").startswith("FUNPAY_GOLDEN_KEY=from-example")

    # повторный вызов ничего не перезаписывает
    assert ensure_env_file(quiet=True) is None
    assert "from-example" in created.read_text(encoding="utf-8")


def test_env_is_not_overwritten_by_defaults(isolated_env):
    env = isolated_env / ".env"
    env.write_text("POLL_INTERVAL=11\n", encoding="utf-8")
    Config.load()
    import os

    assert os.environ["POLL_INTERVAL"] == "11"


# --------------------------------------------------------------------------- #
# config.json: три формы ключей и «строковые числа»
# --------------------------------------------------------------------------- #
def _load_json(isolated_env, payload: dict) -> Config:
    import json

    path = isolated_env / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return Config.load(path)


def test_config_json_accepts_env_style_keys(isolated_env):
    """Ключи из .env можно перенести в JSON дословно — иначе «файл не работает»."""
    cfg = _load_json(
        isolated_env,
        {
            "FUNPAY_GOLDEN_KEY": "json-key",
            "GAMEAU_API_KEY": "json-gameau",
            "POLL_INTERVAL": "11.5",
            "MAX_CONCURRENT_ORDERS": 7,
            "EXCHANGE_VARIANT": "1",
            "HIDE_SENDER": "да",
        },
    )
    assert cfg.golden_key == "json-key" and cfg.gameau_key == "json-gameau"
    assert cfg.poll_interval == 11.5
    assert cfg.max_concurrent_orders == 7 and isinstance(cfg.max_concurrent_orders, int)
    assert cfg.exchange_variant == 1
    assert cfg.hide_sender is True
    assert cfg.parse_errors == []


def test_config_json_sections_and_aliases(isolated_env):
    cfg = _load_json(
        isolated_env,
        {
            "API": {"token": "t-1", "url": "https://gameau.example/api/v1", "max_charge_usdt": 12.5},
            "funpay": {"golden_key": "sec-key", "user_agent": "UA/2.0"},
            "BOT": {"bot_token": "123:AAAtoken", "chat_id": "42"},
            "LOT_RULES": '[{"kind": "quantity", "min": 100}]',
        },
    )
    assert cfg.gameau_key == "t-1"
    assert cfg.gameau_base_url == "https://gameau.example/api/v1"
    assert cfg.default_max_charge_usdt == 12.5
    assert cfg.golden_key == "sec-key" and cfg.funpay_user_agent == "UA/2.0"
    assert cfg.telegram_bot_token == "123:AAAtoken" and cfg.telegram_chat_id == "42"
    assert cfg.lot_rules == [{"kind": "quantity", "min": 100}], "JSON-массив строкой тоже валиден"


def test_config_json_bad_number_keeps_default_and_reports(isolated_env):
    cfg = _load_json(isolated_env, {"RATE_VARIANT_2": "abc", "POLL_INTERVAL": 3})
    assert cfg.rate_variant_2 == Config().rate_variant_2
    assert cfg.poll_interval == 3
    assert any("rate_variant_2" in e for e in cfg.parse_errors)


def test_shipped_config_example_is_loadable(isolated_env):
    """config.example.json обязан читаться тем же загрузчиком — иначе пример врёт."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.json"
    cfg = Config.load(example)
    assert cfg.parse_errors == [], cfg.parse_errors
    assert cfg.exchange_variant in (1, 2) and cfg.poll_interval > 0
