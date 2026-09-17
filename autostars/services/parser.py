"""Парсер Telegram Handles с высокой точностью и поддержкой различных форматов."""

from __future__ import annotations

import re
from typing import Optional

# Комплексное регулярное выражение для извлечения Telegram Username
TG_REGEX = re.compile(
    r'(?:^|[^a-zA-Z0-9_])@(?=.{5,32}(?:[^a-zA-Z0-9_]|$))(?!.*__)(?![0-9_])([a-zA-Z0-9_]*[a-zA-Z][a-zA-Z0-9_]*){3,}(?<!_)(?=[^a-zA-Z0-9_]|$)',
    re.IGNORECASE,
)

# Запасной парсер для ссылок t.me/username и telegram.me/username
TG_URL_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|@)?([a-zA-Z0-9_]{4,32})',
    re.IGNORECASE,
)

# Парсер для конструкций «мой тг: username», «telegram: username»
KEYWORD_REGEX = re.compile(
    r'(?:тг|телега|телеграм|телеграмм|telegram|tg|юзернейм|никнейм|ник|аккаунт)[:\s\-]+@?([a-zA-Z][a-zA-Z0-9_]{3,31})',
    re.IGNORECASE,
)

# Одиночный токен, полностью совпадающий с форматом юзернейма
BARE_REGEX = re.compile(r'^[a-zA-Z][a-zA-Z0-9_]{3,31}$')

# Стоп-слова (технические термины и частые слова, не являющиеся юзернеймом)
STOPWORDS = {
    "telegram", "teiegram", "telegramm", "funpay", "support", "username",
    "admin", "order", "orders", "stars", "premium", "account", "привет",
    "спасибо", "здравствуйте", "готово", "оплатил", "жду", "отзыв",
}

# Регулярные выражения для извлечения количества звёзд
_STARS_WORDS = (
    r"(?:зв[её]зд(?:а|ы|очек|очк[иау])?|зв[её]зды|"
    r"тел[её]грам(?:м)?\s*зв[её]зд(?:ы)?|тг\s*зв[её]зды|"
    r"telegram\s+stars?|tg\s+stars?|stars?)"
)
RE_NUM_THEN_WORD = re.compile(r"(\d{1,9})\s*" + _STARS_WORDS, re.IGNORECASE)
RE_WORD_THEN_NUM = re.compile(_STARS_WORDS + r"\s*(\d{1,9})", re.IGNORECASE)
RE_NUM_THEN_STAR = re.compile(r"(\d{1,9})\s*(?:⭐|✯)", re.IGNORECASE)
RE_STAR_THEN_NUM = re.compile(r"(?:⭐|✯)\s*(\d{1,9})", re.IGNORECASE)


def extract_telegram_username(text: Optional[str]) -> Optional[str]:
    """
    Извлекает чистый юзернейм из текста сообщения покупателя.
    Возвращает 'username' без символа '@' или None.

    Приоритет:
    1. Прямая ссылка t.me/username или telegram.me/username
    2. Упоминание с символом @username (по эталонному TG_REGEX)
    3. Ключевые слова («тг: username», «юзернейм: username»)
    4. Одиночный токен в сообщении (фоллбэк для сообщений, состоящих только из ника)
    """
    if not text:
        return None

    # 1. Проверка по прямой ссылке t.me/ или telegram.me/
    url_match = TG_URL_REGEX.search(text)
    if url_match:
        cand = url_match.group(1).lstrip("@").strip()
        if cand.lower() not in STOPWORDS:
            return cand

    # 2. Проверка по тэгу @username
    tag_match = TG_REGEX.search(text)
    if tag_match:
        raw_handle = tag_match.group(0).strip()
        # Удаление префиксов/суффиксов
        clean_handle = re.sub(r'^[^a-zA-Z0-9_]+|[^a-zA-Z0-9_]+$', '', raw_handle)
        if clean_handle.lower() not in STOPWORDS:
            return clean_handle

    # 3. Проверка по ключевым словам (тг: username)
    kw_match = KEYWORD_REGEX.search(text)
    if kw_match:
        cand = kw_match.group(1).strip()
        if cand.lower() not in STOPWORDS:
            return cand

    # 4. Фоллбэк: если всё сообщение — одиночный юзернейм
    stripped = text.strip().lstrip("@")
    if BARE_REGEX.match(stripped) and stripped.lower() not in STOPWORDS:
        return stripped

    return None


def extract_stars_quantity(text: Optional[str]) -> Optional[int]:
    """
    Извлекает количество звёзд из описания лота или сообщения.
    Например: '1000 звёзд', '⭐ 500', '250 Stars'.
    """
    if not text:
        return None
    if m := RE_NUM_THEN_WORD.search(text):
        return int(m.group(1))
    if m := RE_NUM_THEN_STAR.search(text):
        return int(m.group(1))
    if m := RE_WORD_THEN_NUM.search(text):
        return int(m.group(1))
    if m := RE_STAR_THEN_NUM.search(text):
        return int(m.group(1))
    return None
