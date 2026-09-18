"""Тесты редактора .env для GUI.

Главный риск графических настроек — «сохранил и всё сломалось»: файл портится,
комментарии и секреты теряются, значение вне диапазона уезжает в движок.
Отсюда и проверки: сохранение атомарно-предсказуемо, секреты не затираются
пустым полем, битые числа не доходят до движка.
"""

from __future__ import annotations

import pytest

from autostars.config import Config
from autostars.gui import settings

ENV_TEXT = """# ключи доступа
FUNPAY_GOLDEN_KEY=oldkey
GAMEAU_API_KEY=gameau-secret
POLL_INTERVAL=5.0   # опрос очереди
# вариант завода USDT
EXCHANGE_VARIANT=2
"""


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    monkeypatch.setattr(settings, "target_env_file", lambda: path)
    # движок и GUI должны читать/писать один и тот же файл
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOSTARS_ENV", str(path))
    monkeypatch.setenv("AUTOSTARS_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "autostars.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "autostars.log"))
    for key in ("FUNPAY_GOLDEN_KEY", "GAMEAU_API_KEY", "POLL_INTERVAL", "EXCHANGE_VARIANT",
                "MAX_CONCURRENT_ORDERS", "HIDE_SENDER", "DB_PATH", "LOG_FILE"):
        monkeypatch.delenv(key, raising=False)
    return path


def test_read_settings(env_file):
    values = settings.read_settings()
    assert values["FUNPAY_GOLDEN_KEY"] == "oldkey"
    assert values["POLL_INTERVAL"] == "5.0", "инлайн-комментарий не должен попадать в поле формы"


def test_validation_rejects_bad_numbers(env_file):
    errors = settings.validate({"POLL_INTERVAL": "abc", "MAX_CONCURRENT_ORDERS": "0", "STUCK_TASK_MINUTES": "1.5"})
    assert len(errors) == 3
    assert any("ожидалось число" in e for e in errors)
    assert any("не меньше 1" in e for e in errors)
    assert any("целое число" in e for e in errors)


def test_validation_accepts_good_values(env_file):
    assert settings.validate({"POLL_INTERVAL": "7,5", "MAX_CONCURRENT_ORDERS": "6", "EXCHANGE_VARIANT": "2"}) == []
    assert settings.validate({"EXCHANGE_VARIANT": "3"}) != []


def test_display_value_hides_secrets_and_normalizes_bool(env_file):
    values = settings.read_settings()
    golden = next(f for f in settings.FIELDS if f.key == "FUNPAY_GOLDEN_KEY")
    hide = next(f for f in settings.FIELDS if f.key == "HIDE_SENDER")
    poll = next(f for f in settings.FIELDS if f.key == "POLL_INTERVAL")
    assert settings.display_value(golden, values) == "", "секрет не показывается в форме"
    assert settings.display_value(hide, values) == "нет"
    assert settings.display_value(poll, values) == "5.0"


def test_plan_skips_empty_secret_and_unchanged(env_file):
    current = settings.read_settings()
    plan = settings.plan_updates(
        {
            "FUNPAY_GOLDEN_KEY": "",  # пользователь не трогал поле секрета
            "GAMEAU_API_KEY": "gameau-secret",  # то же значение
            "POLL_INTERVAL": "9",  # реально изменили
            "MAX_CONCURRENT_ORDERS": "3",  # новый ключ
        },
        current,
    )
    assert plan == {"POLL_INTERVAL": "9", "MAX_CONCURRENT_ORDERS": "3"}


def test_apply_preserves_comments_and_order(env_file):
    updates = settings.plan_updates(
        {"FUNPAY_GOLDEN_KEY": "newkey", "POLL_INTERVAL": "9", "MAX_CONCURRENT_ORDERS": "3"},
        settings.read_settings(),
    )
    path, changed = settings.apply_updates(updates)
    text = path.read_text(encoding="utf-8")

    assert changed == 3
    assert "# ключи доступа" in text and "# вариант завода USDT" in text
    assert text.index("FUNPAY_GOLDEN_KEY") < text.index("GAMEAU_API_KEY") < text.index("POLL_INTERVAL")
    assert "FUNPAY_GOLDEN_KEY=newkey" in text
    assert "POLL_INTERVAL=9" in text
    assert "MAX_CONCURRENT_ORDERS=3" in text.splitlines()[-1], "новый ключ — в конец файла"
    assert path.with_name(path.name + ".bak").exists(), "без бэкапа правка .env не должна уходить"


def test_applied_values_reach_the_engine(env_file):
    """Конфигурация, сохранённая GUI, действительно читается Config.load()."""
    path, _ = settings.apply_updates({"POLL_INTERVAL": "9", "EXCHANGE_VARIANT": "1", "RATE_VARIANT_1": "120"})
    cfg = Config.load()
    assert cfg.poll_interval == 9.0
    assert cfg.exchange_variant == 1
    assert cfg.active_usdt_rate == 120.0
    assert str(path) in cfg.source_files


def test_noop_save_does_not_touch_file(env_file):
    before = env_file.read_text(encoding="utf-8")
    path, changed = settings.apply_updates({"POLL_INTERVAL": "5.0"})
    assert changed == 0
    assert env_file.read_text(encoding="utf-8") == before
    assert not path.with_name(path.name + ".bak").exists()


def test_missing_required(env_file, tmp_path):
    assert settings.missing_required(env_file) == []
    empty = tmp_path / "fresh.env"
    empty.write_text("FUNPAY_GOLDEN_KEY=paste_your_funpay_golden_key_here\n", encoding="utf-8")
    assert settings.missing_required(empty) == ["FUNPAY_GOLDEN_KEY", "GAMEAU_API_KEY"]


def test_bool_and_choice_normalization(env_file):
    hide = next(f for f in settings.FIELDS if f.key == "HIDE_SENDER")
    variant = next(f for f in settings.FIELDS if f.key == "EXCHANGE_VARIANT")
    assert settings.normalize(hide, "да") == "true"
    assert settings.normalize(hide, "нет") == "false"
    assert settings.normalize(variant, "1") == "1"


def test_no_duplicate_or_unknown_fields():
    """Два поля на один ключ = «сохранил одно, в .env попало другое»."""
    keys = [f.key for f in settings.FIELDS]
    assert len(keys) == len(set(keys)), f"дублируются поля: {sorted({k for k in keys if keys.count(k) > 1})}"
    from autostars.config import ENV_MAP

    unknown = [k for k in keys if k not in ENV_MAP]
    assert not unknown, f"движок не знает таких настроек: {unknown}"
    assert set(settings.FIELDS_BY_SECTION) == set(settings.SECTION_KEYS) - {"Общее"} | {
        f.section for f in settings.FIELDS
    }, "секции в форме и в словаре секций должны совпадать"


def test_fields_cover_env_documentation():
    """Ключи из .env.example должны иметь поле в GUI (иначе «нет такого настроечного поля»)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    keys = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    known = {f.key for f in settings.FIELDS}
    missing = {k for k in keys if k not in known and not k.startswith("WHITEBIRD")}
    assert not missing, f"в GUI нет полей для: {sorted(missing)}"
