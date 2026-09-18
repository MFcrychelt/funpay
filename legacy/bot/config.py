"""Загрузка и валидация конфигурации.

Секреты читаются из переменных окружения / `.env` файла:
    FUNPAY_GOLDEN_KEY   — golden_key FunPay-аккаунта (обязательно)
    GAMEAU_API_KEY      — API-ключ GAMEAU (обязательно)
    FUNPAY_USER_ID      — числовой ID аккаунта продавца (опционально)

Настройки поведения читаются из `config.json` (см. `config.example.json`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logger_setup import get_logger

logger = get_logger("config")

ENV_FILE = ".env"


def _load_env_file(path: str | Path = ENV_FILE) -> None:
    """Мини-парсер .env (KEY=VALUE), не подменяет уже выставленные переменные."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    # --- FunPay ---
    funpay_golden_key: str = ""
    funpay_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    # --- GAMEAU ---
    gameau_api_key: str = ""
    gameau_base_url: str = "https://gameau.us/api/v1"
    gameau_max_charge_multiplier: float = 1.5
    gameau_request_timeout: int = 30
    gameau_status_poll_interval: float = 5.0
    gameau_status_timeout: float = 600.0
    # --- Поведение автовыдачи ---
    poll_interval: float = 15.0
    session_refresh_interval: float = 45 * 60  # секунд
    # Сопоставление лотов и количества звёзд.
    # Каждое правило: {"pattern": <regex подстановки>, "stars": <int или "from_lot">}
    lot_rules: list[dict[str, Any]] = field(default_factory=list)
    # Сколько ждать юзернейм от покупателя, если он не пришёл сразу (секунд).
    username_wait_timeout: float = 24 * 3600
    # Интервал повторной проверки чата в ожидании юзернейма.
    username_check_interval: float = 30.0
    # Отвечать ли покупателю сообщением после успешной выдачи.
    reply_on_delivery: bool = True
    # Сообщения.
    msg_need_username: str = (
        "Спасибо за покупку! 🙌\nЧтобы я отправил звёзды, пришлите в этот чат ваш "
        "юзернейм Telegram (например, @username)."
    )
    msg_username_reminder: str = (
        "Напоминаю: для выдачи звёзд жду ваш юзернейм Telegram (например, @username)."
    )
    msg_stars_sent: str = (
        "⭐ Звёзды отправлены на @{username}!\n"
        "Количество: {stars}.\n"
        "Проверьте баланс и подтвердите заказ, пожалуйста. Спасибо за покупку! 🙏"
    )
    msg_error: str = (
        "Произошла ошибка при выдаче звёзд. Я уже сообщил продавцу, вопрос решим — "
        "ожидайте, пожалуйста."
    )
    # Просить ли юзернейм в чате, если его нет в описании заказа.
    ask_username_in_chat: bool = True
    # Напоминать ли покупателю периодически, пока юзернейм не получен.
    remind_username: bool = True
    reminder_interval: float = 3600.0
    # Количество звёзд по умолчанию, если ни правило, ни парсинг не сработали.
    default_stars: int | None = None
    # Автоматический возврат средств покупателю, если выдача звёзд провалилась.
    refund_on_failure: bool = False
    # Сколько попыток создания заказа в GAMEAU до признания ошибки фатальной.
    max_create_attempts: int = 3
    # Уведомления продавцу через Telegram-бота (опционально).
    seller_notify_token: str = ""
    seller_notify_chat_id: str = ""
    # --- Прибыль ---
    # Цена закупки 1 звезды в USD (сколько платишь GAMEAU).
    cost_per_star_usd: float = 0.017
    # Цена продажи 1 звезды в RUB (сколько берёшь с покупателя).
    revenue_per_star_rub: float = 1.5
    # Курс USD→RUB для пересчёта (если 0 — не считать).
    usd_to_rub: float = 100.0
    # Скрывать ли имя отправителя у получателя звёзд.
    hide_sender: bool = False
    # --- Баланс/контроль ---
    min_gameau_balance: float = 0.0
    stop_on_low_balance: bool = False
    # Файл журнала.
    log_file: str = "bot.log"

    @classmethod
    def load(cls, config_path: str | Path = "config.json") -> "Config":
        """Собирает конфиг из .env + config.json + переменных окружения."""
        _load_env_file()
        cfg = cls()

        # 1) config.json (если существует) — базовые настройки поведения.
        path = Path(config_path)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise SystemExit(f"Не удалось разобрать {config_path}: {exc}") from exc
        else:
            logger.warning("Файл %s не найден — использую значения по умолчанию.", config_path)

        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
            else:
                logger.warning("Неизвестный параметр в конфиге: %s", key)

        # 2) Переменные окружения — перекрывают всё (секреты).
        env_map = {
            "FUNPAY_GOLDEN_KEY": "funpay_golden_key",
            "FUNPAY_USER_AGENT": "funpay_user_agent",
            "GAMEAU_API_KEY": "gameau_api_key",
            "GAMEAU_BASE_URL": "gameau_base_url",
        }
        for env_name, attr in env_map.items():
            val = os.environ.get(env_name)
            if val:
                setattr(cfg, attr, val)

        return cfg

    def validate(self) -> None:
        """Проверяет обязательные параметры, падает с понятной ошибкой."""
        missing = []
        if not self.funpay_golden_key:
            missing.append("FUNPAY_GOLDEN_KEY")
        if not self.gameau_api_key:
            missing.append("GAMEAU_API_KEY")
        if missing:
            raise SystemExit(
                "Не заданы обязательные переменные окружения: "
                + ", ".join(missing)
                + ". Скопируйте .env.example в .env и заполните значения."
            )
        if self.gameau_max_charge_multiplier < 1.0:
            raise SystemExit("gameau_max_charge_multiplier должен быть >= 1.0")
