"""Тесты извлечения юзернеймов и определения количества звёзд."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.delivery import stars_from_description  # noqa: E402
from bot.username_parser import best_username, extract_usernames, is_valid_username  # noqa: E402


# --------------------------------------------------------------------------- #
# Валидация юзернейма
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("token,expected", [
    ("alex2024x", True),
    ("user_name123", True),
    ("IvanPetrov", True),
    ("short", True),
    ("username", False),        # стоп-слово (ключевое слово, а не ник)
    ("ab", False),              # слишком короткий
    ("1username", False),       # начинается с цифры
    ("user name", False),       # пробел
    ("юзер", False),            # кириллица
    ("telegram", False),        # стоп-слово
    ("funpay", False),          # стоп-слово
    ("a" * 33, False),          # слишком длинный
    ("_lead", False),           # начинается с подчёркивания
])
def test_is_valid_username(token: str, expected: bool) -> None:
    assert is_valid_username(token) is expected


# --------------------------------------------------------------------------- #
# Извлечение из разных форматов
# --------------------------------------------------------------------------- #
def test_at_mention() -> None:
    assert extract_usernames("Мой ник @ivan_petrov, кидай туда") == ["ivan_petrov"]


def test_tme_link() -> None:
    assert extract_usernames("Вот ссылка: https://t.me/cool_seller") == ["cool_seller"]


def test_tme_link_no_scheme() -> None:
    assert extract_usernames("пиши на t.me/cool_seller") == ["cool_seller"]


def test_keyword_colon() -> None:
    assert extract_usernames("Телеграм: my_account") == ["my_account"]


def test_keyword_tg() -> None:
    assert extract_usernames("тг: seller_777") == ["seller_777"]


def test_bare_username() -> None:
    assert extract_usernames("just_username") == ["just_username"]


def test_bare_token_in_sentence_low_priority() -> None:
    # Одиночный токен в длинном тексте без маркеров — низкий приоритет,
    # но если других кандидатов нет, он должен быть найден.
    assert extract_usernames("отправь на mylogin24") == ["mylogin24"]


def test_multiple_prefers_link() -> None:
    text = "тг @second_one или лучше ссылка https://t.me/primary_user"
    result = extract_usernames(text)
    assert result[0] == "primary_user"
    assert "second_one" in result


def test_stopword_not_matched() -> None:
    assert extract_usernames("это просто слово telegram тут") == []


def test_empty() -> None:
    assert extract_usernames("") == []
    assert extract_usernames(None) == []  # type: ignore[arg-type]


def test_dedup_case_insensitive() -> None:
    text = "@Ivan_Petrov и ещё раз ivan_petrov"
    result = extract_usernames(text)
    assert result == ["Ivan_Petrov"]


def test_best_username_first_non_empty() -> None:
    texts = ["спасибо за покупку", "@real_user", "t.me/another_one"]
    assert best_username(texts) == "real_user"


def test_best_username_all_empty() -> None:
    assert best_username(["", "привет", "нет юзернейма"]) is None


# --------------------------------------------------------------------------- #
# Определение количества звёзд из описания лота
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,expected", [
    ("100 звёзд", 100),
    ("250 Stars", 250),
    ("50⭐", 50),
    ("Telegram Stars 100", 100),
    ("500 звёзд телеграм", 500),
    ("1000 stars", 1000),
    ("Звёзды 250", 250),
    ("⭐ 750", 750),
    ("тг звёзды 300", 300),
])
def test_stars_from_description(desc: str, expected: int) -> None:
    assert stars_from_description(desc) == expected, f"не распознано: {desc!r}"


def test_stars_no_match() -> None:
    assert stars_from_description("аккаунт гта 5") is None
    assert stars_from_description("") is None
    assert stars_from_description(None) is None  # type: ignore[arg-type]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
