"""Загрузка конфигурации для AutoStars (FunPay <-> Gameau Engine).

Параметры и секреты читаются из переменных окружения, файла .env
и опционального config.json.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("autostars.config")

ENV_FILE = ".env"


def _load_env_file(path: str | Path = ENV_FILE) -> None:
    """Парсинг .env файла без перезаписи уже выставленных переменных."""
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
    golden_key: str = ""
    funpay_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    funpay_base_url: str = "https://funpay.com"

    # --- Gameau ---
    gameau_key: str = ""
    gameau_base_url: str = "https://gameau.us/api/v1"
    default_max_charge_usdt: float = 9.50
    default_stars_quantity: int = 1000
    gameau_timeout: float = 15.0
    gameau_max_retries: int = 3

    # --- Financial Model (Crypto USDT TRC-20 Exchange Variants) ---
    # Вариант 1: Анонимный обмен / P2P (курс ~110.00 RUB за 1 USDT)
    # Вариант 2: Беларусь Whitebird без P2P (курс ~87.63 RUB за 1 USDT)
    exchange_variant: int = 2
    rate_variant_1: float = 110.00  # RUB за 1 USDT (анонимный обмен)
    rate_variant_2: float = 87.63   # RUB за 1 USDT (Whitebird Беларусь)
    whitebird_usdt_rate: float = 87.63  # Синоним rate_variant_2
    tron_energy_fee_rub: float = 0.0

    @property
    def active_usdt_rate(self) -> float:
        """Возвращает курс USDT в зависимости от выбранного варианта пополнения."""
        if self.exchange_variant == 1:
            return self.rate_variant_1
        return self.rate_variant_2

    # --- Telegram Notifier ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Database & Polling ---
    db_path: str = "autostars.db"
    poll_interval: float = 5.0
    log_file: str = "autostars.log"

    # --- Дополнительные правила для лотов ---
    lot_rules: list[dict[str, Any]] = field(default_factory=list)

    # --- Прибыль и настройки выдачи ---
    hide_sender: bool = False
    cost_per_star_usd: float = 0.017
    revenue_per_star_rub: float = 1.5
    usd_to_rub: float = 100.0

    @classmethod
    def load(cls, config_path: str | Path = "config.json") -> "Config":
        """Загружает конфигурацию из .env, config.json и переменных окружения."""
        _load_env_file()
        cfg = cls()

        # 1. Загрузка из config.json если существует
        path = Path(config_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for key, val in data.items():
                    # Маппинг альтернативных имён
                    target_key = key
                    if key == "funpay_golden_key":
                        target_key = "golden_key"
                    elif key == "gameau_api_key":
                        target_key = "gameau_key"
                    elif key == "seller_notify_token":
                        target_key = "telegram_bot_token"
                    elif key == "seller_notify_chat_id":
                        target_key = "telegram_chat_id"

                    if hasattr(cfg, target_key):
                        setattr(cfg, target_key, val)
            except Exception as exc:
                logger.warning(f"Ошибка чтения {config_path}: {exc}")

        # 2. Переменные окружения (приоритет над файлами)
        env_map = {
            "FUNPAY_GOLDEN_KEY": "golden_key",
            "golden_key": "golden_key",
            "GAMEAU_API_KEY": "gameau_key",
            "gameau_key": "gameau_key",
            "GAMEAU_BASE_URL": "gameau_base_url",
            "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
            "telegram_bot_token": "telegram_bot_token",
            "TELEGRAM_CHAT_ID": "telegram_chat_id",
            "EXCHANGE_VARIANT": "exchange_variant",
            "RATE_VARIANT_1": "rate_variant_1",
            "RATE_VARIANT_2": "rate_variant_2",
            "WHITEBIRD_USDT_RATE": "rate_variant_2",
            "DB_PATH": "db_path",
            "DEFAULT_MAX_CHARGE_USDT": "default_max_charge_usdt",
            "DEFAULT_STARS_QUANTITY": "default_stars_quantity",
            "POLL_INTERVAL": "poll_interval",
            "FUNPAY_USER_AGENT": "funpay_user_agent",
        }

        for env_var, attr in env_map.items():
            val = os.environ.get(env_var)
            if val is not None and val != "":
                current_val = getattr(cfg, attr)
                if isinstance(current_val, float):
                    setattr(cfg, attr, float(val))
                elif isinstance(current_val, int):
                    setattr(cfg, attr, int(val))
                elif isinstance(current_val, bool):
                    setattr(cfg, attr, val.lower() in ("true", "1", "yes"))
                else:
                    setattr(cfg, attr, val)

        return cfg

    def validate(self) -> None:
        """Проверяет наличие обязательных ключей."""
        missing = []
        if not self.golden_key:
            missing.append("golden_key (FUNPAY_GOLDEN_KEY)")
        if not self.gameau_key:
            missing.append("gameau_key (GAMEAU_API_KEY)")
        if missing:
            raise ValueError(
                f"Отсутствуют обязательные параметры конфигурации: {', '.join(missing)}. "
                "Укажите их в .env или config.json."
            )
