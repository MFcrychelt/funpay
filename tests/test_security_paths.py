"""Тесты redaction/маскирования секретов и путей приложения.

Лог AutoStars пересылают в чаты и поддержку; если в нём окажется golden_key,
аккаунт продавца на FunPay приходится восстанавливать через поддержку и менять
ключ. Поэтому маскирование проверяется тестом, а не «на глаз».
"""

from __future__ import annotations

from autostars.config import mask_secret
from autostars.paths import app_dir, example_file, find_env_file, resolve_user_path
from autostars.security import MASK, redact, truncate
from autostars.security import mask_secret as mask2


def test_redact_removes_known_secrets():
    secret = "abcd1234efgh5678"
    line = f"cookie: golden_key={secret}; path=/"
    out = redact(line, secrets=[secret])
    assert secret not in out
    assert MASK in out


def test_redact_catches_key_value_patterns_without_list():
    out = redact('{"api_key": "super-long-token-value", "id": 5}')
    assert "super-long-token-value" not in out
    assert "id" in out  # обычные поля не трогаем


def test_redact_telegram_bot_token_shape():
    token = "123456789:AAH3kQm0pLsVx7yZ0cBnDfEtGhIjKlMnOpQ"
    assert token not in redact(f"send to https://api.telegram.org/bot{token}/sendMessage")


def test_mask_secret_keeps_context():
    masked = mask_secret("abcdefghijklmnop")
    assert masked.startswith("abcd") and masked.endswith("mnop") and "*" in masked
    assert "efghijkl" not in masked
    assert mask_secret("") == "(не задан)"
    assert mask_secret("short") == MASK
    assert mask2("abcdefghijklmnop") == masked


def test_truncate_collapses_whitespace():
    out = truncate("a\n\n  b   c", limit=200)
    assert out == "a b c"
    long_out = truncate("x" * 500, limit=50)
    assert long_out.startswith("x" * 50)
    assert "симв." in long_out


# --------------------------------------------------------------------------- #
# пути: exe-сценарий (двойной клик), AUTOSTARS_HOME, поиск примеров
# --------------------------------------------------------------------------- #
def test_app_dir_respects_autostars_home(isolated_env):
    assert app_dir() == isolated_env


def test_resolve_user_path_relative_and_absolute(isolated_env):
    assert resolve_user_path("autostars.db") == isolated_env / "autostars.db"
    assert resolve_user_path("/var/lib/x/db.sqlite").as_posix().endswith("/var/lib/x/db.sqlite")
    assert resolve_user_path("~/db.sqlite").is_absolute()


def test_find_env_file_prefers_override(isolated_env, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere.env"
    other.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setenv("AUTOSTARS_ENV", str(other))
    assert find_env_file() == other


def test_find_env_file_falls_back_to_app_dir(isolated_env, monkeypatch):
    monkeypatch.delenv("AUTOSTARS_ENV", raising=False)
    (isolated_env / ".env").write_text("X=1\n", encoding="utf-8")
    assert find_env_file() == isolated_env / ".env"


def test_example_file_lookup(isolated_env, monkeypatch):
    monkeypatch.delenv("AUTOSTARS_ENV", raising=False)
    assert example_file("не-существует.example") is None
    (isolated_env / ".env.example").write_text("A=1\n", encoding="utf-8")
    assert example_file(".env.example") == isolated_env / ".env.example"


def test_cwd_does_not_change_resolved_paths(isolated_env, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert app_dir() == isolated_env
    assert resolve_user_path("x.db") == isolated_env / "x.db"
    assert not (tmp_path / "x.db").exists()
