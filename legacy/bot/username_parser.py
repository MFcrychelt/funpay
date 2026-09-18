"""Извлечение юзернеймов Telegram из текста заказов/сообщений.

Поддерживаемые форматы:
    @username
    username (отдельным сообщением/строкой)
    https://t.me/username, t.me/username, telegram.me/username
    "мой тг: username", "телеграм: username" и т.п.

Юзернейм валидируется по правилам Telegram:
5–32 символа, только латиница, цифры и подчёркивание.
"""

from __future__ import annotations

import re
from typing import Iterable

# Минимальная/максимальная длина юзернейма Telegram.
MIN_LEN = 5
MAX_LEN = 32

# Служебные «никнеймы» и стоп-слова, которые не могут быть юзернеймом покупателя.
STOPWORDS = {
    "telegram", "telegramm", "teiegram", "funpay", "support", "username",
    "usernam", "user", "users", "admin", "bot", "bots", "https", "http",
    "www", "t.me", "tme", "telegram.me", "message", "messages", "order",
    "orders", "stars", "star", "premium", "account", "acc", "login",
    "password", "пароль", "логин", "юзернейм", "ник", "никнейм",
}

# Явно именованные упоминания: "@username" или "t.me/username".
RE_AT = re.compile(r"@([A-Za-z][A-Za-z0-9_]{%d,%d})" % (MIN_LEN - 1, MAX_LEN - 1))
RE_TME = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|@)?([A-Za-z][A-Za-z0-9_]{"
    + str(MIN_LEN - 1) + "," + str(MAX_LEN - 1) + r"})(?=$|[^A-Za-z0-9_])",
    re.IGNORECASE,
)

# «мой тг: username», "telegram - username", "юзернейм: username" и т.п.
RE_KEYWORD = re.compile(
    r"(?:юзернейм|юзер|никнейм|ник|телеграм|телега|тг|аккаунт|акк|логин|телеграмм|"
    r"username|telegram|tg)\s*[:\-–—=]?\s*@?([A-Za-z][A-Za-z0-9_]{%d,%d})"
    % (MIN_LEN - 1, MAX_LEN - 1),
    re.IGNORECASE,
)

# Одиночный токен, полностью совпадающий с форматом юзернейма.
RE_BARE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{%d,%d}$" % (MIN_LEN - 1, MAX_LEN - 1))

# Кандидаты «в лоб»: любые токены формата юзернейма (последний приоритет).
RE_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{%d,%d})\b" % (MIN_LEN - 1, MAX_LEN - 1))


def is_valid_username(token: str) -> bool:
    """Проверяет токен на соответствие формату юзернейма Telegram."""
    if not token or not (MIN_LEN <= len(token) <= MAX_LEN):
        return False
    if token.lower() in STOPWORDS:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", token):
        return False
    return True


def _clean(candidate: str) -> str | None:
    candidate = candidate.strip().rstrip(".,;:!?").strip("()[]{}<>\"'")
    return candidate if is_valid_username(candidate) else None


def extract_usernames(text: str) -> list[str]:
    """Возвращает юзернеймы, найденные в тексте, в порядке убывания уверенности.

    Порядок приоритетов:
        1. ссылки вида t.me/username;
        2. упоминания @username;
        3. конструкции «тг: username», «телеграм: username»;
        4. сообщение, целиком состоящее из юзернейма;
        5. прочие токены подходящего формата.

    Дубликаты убираются (без учёта регистра), порядок первого вхождения сохраняется.
    """
    if not text:
        return []

    found: list[str] = []

    def add(token: str | None) -> None:
        if not token:
            return
        lowered = token.lower()
        for existing in found:
            if existing.lower() == lowered:
                return
        found.append(token)

    # 1) t.me-ссылки.
    for m in RE_TME.finditer(text):
        add(_clean(m.group(1)))

    # 2) @упоминания.
    for m in RE_AT.finditer(text):
        add(_clean(m.group(1)))

    # 3) именованные конструкции.
    for m in RE_KEYWORD.finditer(text):
        add(_clean(m.group(1)))

    # 4) сообщение целиком — юзернейм.
    stripped = text.strip()
    if RE_BARE.match(stripped):
        add(stripped)

    # 5) одиночные токены — только если пока ничего не нашли
    # (иначе слишком высок риск ложных срабатываний на обычном тексте).
    if not found:
        for m in RE_TOKEN.finditer(text):
            add(_clean(m.group(1)))
            if found:
                break

    return found


def best_username(texts: Iterable[str]) -> str | None:
    """Ищет юзернейм по списку текстов (новые — первыми).

    Тексты просматриваются по порядку; возвращается первый найденный
    юзернейм из самого «свежего» подходящего текста.
    """
    for text in texts:
        candidates = extract_usernames(text or "")
        if candidates:
            return candidates[0]
    return None
