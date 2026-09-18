"""Мелочи безопасности, которые легко забыть.

В лог AutoStars попадают тела ответов FunPay/GAMEAU и сообщения покупателей.
Там же однажды оказался golden_key (кук FunPay) и API-ключ GAMEAU — а лог
обычно пересылают скриншотом «в поддержку». Поэтому секрет вырезается из
всего, что уходит в лог или в GUI.
"""

from __future__ import annotations

import re

MASK = "********"

# «ключ = значение» в HTML/JSON/куках.
KEY_VALUE_RE = re.compile(
    r"(?i)\b[\"']?(golden_key|api[_-]?key|authorization|bearer|csrf[_-]?token|bot_token|token)"
    r"[\"']?\s*[=:]\s*[\"']?([^\s\"',;&}]+)"
)
# Telegram bot token вида 123456789:AAExy... (в т.ч. внутри URL .../bot<token>/...)
TG_BOT_TOKEN_RE = re.compile(r"\d{8,11}:[A-Za-z0-9_-]{30,}")
# HTML-мета FunPay: <meta name="csrf-token" content="…">
HTML_TOKEN_RE = re.compile(
    r"(?i)name=[\"']csrf[-_]?token[\"']\s+content=[\"']([^\"']+)[\"']"
)

# (паттерн, номер группы со значением) — заменяем только значение, не ломая строку
_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (KEY_VALUE_RE, 2),
    (TG_BOT_TOKEN_RE, 0),
    (HTML_TOKEN_RE, 1),
)


def mask_secret(value: str, keep: int = 4) -> str:
    """'abcd…********…wxyz' — секрет можно узнать глазами, но нельзя украсть из лога."""
    value = (value or "").strip()
    if not value:
        return "(не задан)"
    if len(value) <= keep * 2:
        return MASK
    return f"{value[:keep]}…{MASK}…{value[-keep:]}"


def redact(text: str, secrets: tuple[str, ...] | list[str] | None = None) -> str:
    """Вырезает из текста известные секреты и всё, что на них похоже."""
    if not text:
        return text
    out = str(text)
    for secret in secrets or ():
        secret = str(secret).strip()
        if len(secret) >= 6 and secret in out:
            out = out.replace(secret, MASK)
    for pattern, value_group in _PATTERNS:
        out = pattern.sub(
            lambda m, g=value_group: (
                m.group(0)[: m.start(g) - m.start(0)] + MASK + m.group(0)[m.end(g) - m.start(0) :]
            ),
            out,
        )
    return out


def truncate(text: str, limit: int = 400) -> str:
    """Короткий фрагмент для лога: без пачки HTML в каждой строке."""
    if text is None:
        return ""
    out = " ".join(str(text).split())
    return out if len(out) <= limit else out[:limit] + f"…(+{len(out) - limit} симв.)"
