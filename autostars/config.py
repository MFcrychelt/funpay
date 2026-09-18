"""Загрузка конфигурации AutoStars.

Источники (по возрастанию приоритета):

    1. значения по умолчанию в dataclass `Config`;
    2. `config.json` (плоские ключи, см. config.example.json) — рядом с exe/проектом;
    3. файл `.env` (или AUTOSTARS_ENV) — рядом с exe/проектом;
    4. переменные окружения процесса.

Что исправлено относительно прошлой версии:
  • относительные пути больше не зависят от текущей папки (см. `autostars.paths`) —
    раньше exe, запущенный двойным кликом, не находил ни .env, ни базу;
  • числовые значения парсятся безопасно: ``POLL_INTERVAL=abc`` больше не роняет
    процесс с ``ValueError`` — значение игнорируется и попадает в `Config.parse_errors`;
  • добавлены недостающие переменные окружения (таймауты, hide_sender, курсы,
    себестоимость Stars, запас maxCharge) и `Config.issues()` для диагностики;
  • секреты не светятся в выводе: `Config.to_safe_dict()` маскирует ключи.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .paths import app_dir, example_file, find_env_file, resolve_user_path
from .security import mask_secret as _mask_secret

logger = logging.getLogger("autostars.config")

ENV_FILE = ".env"
CONFIG_FILE = "config.json"

# Плейсхолдеры из .env.example — считываются как «не задано».
PLACEHOLDERS = {
    "",
    "paste_your_funpay_golden_key_here",
    "paste_your_gameau_api_key_here",
    "your_telegram_bot_token_here",
    "your_telegram_chat_id_here",
    "changeme",
    "none",
}


def env_path() -> Path:
    """Путь к активному .env (там, где файл найден, либо где его следует создать)."""
    return find_env_file(ENV_FILE)


def config_file_path() -> Path:
    """Путь к config.json: app_dir, если нет — cwd."""
    override = os.environ.get("AUTOSTARS_CONFIG", "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    candidate = app_dir() / CONFIG_FILE
    if candidate.exists():
        return candidate
    cwd_candidate = Path.cwd() / CONFIG_FILE
    return cwd_candidate if cwd_candidate.exists() else candidate


def ensure_env_file(quiet: bool = False) -> Path | None:
    """Если ни .env, ни config.json нет — создаём .env из .env.example.

    Возвращает путь к созданному файлу (или None, если создавать нечего).
    """
    if env_path().exists() or config_file_path().exists():
        return None
    example = example_file(f"{ENV_FILE}.example")
    target = env_path()
    if example is None or not target.parent.exists():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Не удалось создать {target} из примера: {exc}")
        return None
    if not quiet:
        logger.warning(
            f"Конфигурация не найдена — создан {target}. Заполните FUNPAY_GOLDEN_KEY "
            "и GAMEAU_API_KEY, затем запустите снова."
        )
    return target


def parse_env_text(text: str) -> dict[str, str]:
    """Мини-парсер .env: KEY=VALUE, комментарии, кавычки, `export `."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        quote = value[:1]
        if quote in ('"', "'") and value[-1:] == quote and len(value) >= 2:
            value = value[1:-1]
            if quote == '"':
                # двойные кавычки: экранирование работает, как в python-dotenv —
                # иначе многострочные шаблоны ответов было не записать
                value = value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
        elif "#" in value:
            # inline-комментарий вида KEY=5  # пояснение (в кавычках — не трогаем)
            for marker in (" #", "\t#"):
                if marker in value:
                    value = value.split(marker, 1)[0].strip()
        if key:
            values[key] = value
    return values


# Ключи, которые в os.environ положил именно .env (а не оператор процесса):
# key -> последнее «файловое» значение.
# Нужен, чтобы перечитывание файла (GUI после «Сохранить») обновляло значения,
# но переменная, выставленная в shell/systemd, осталась главнее файла.
_ENV_INJECTED: dict[str, str] = {}


def _apply_env_file(path: Path) -> dict[str, str]:
    """Загружает .env в os.environ: переменные процесса важнее файла."""
    if not path.exists():
        return {}
    try:
        loaded = parse_env_text(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning(f"Не удалось прочитать {path}: {exc}")
        return {}
    for key, value in loaded.items():
        current = os.environ.get(key)
        if current is None:
            os.environ[key] = value
            _ENV_INJECTED[key] = value
        elif _ENV_INJECTED.get(key) == current:
            # значение в окружении — это то, что ранее положил файл, а не оператор:
            # перечитали .env (например, GUI сохранил настройки) — подтягиваем.
            os.environ[key] = value
            _ENV_INJECTED[key] = value
    return loaded


def _coerce(current: Any, raw: str, key: str, errors: list[str]) -> Any:
    """Приводит строку к типу поля dataclass; вместо падения — запись в errors."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "да")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(float(raw.strip()))
        except (TypeError, ValueError):
            errors.append(f"{key}={raw!r}: ожидалось целое число — использовано значение по умолчанию ({current})")
            return current
    if isinstance(current, float):
        try:
            return float(raw.strip().replace(",", "."))
        except (TypeError, ValueError):
            errors.append(f"{key}={raw!r}: ожидалось число — использовано значение по умолчанию ({current})")
            return current
    if isinstance(current, list):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else current
        except json.JSONDecodeError:
            errors.append(f"{key}: ожидается JSON-массив — использовано значение по умолчанию")
            return current
    return raw


# env-имя -> атрибут Config (значения из config.json мапятся тем же словарём ALIASES)
ENV_MAP: dict[str, str] = {
    # FunPay
    "FUNPAY_GOLDEN_KEY": "golden_key",
    "FUNPAY_USER_AGENT": "funpay_user_agent",
    "FUNPAY_BASE_URL": "funpay_base_url",
    # GAMEAU
    "GAMEAU_API_KEY": "gameau_key",
    "GAMEAU_BASE_URL": "gameau_base_url",
    "GAMEAU_TIMEOUT": "gameau_timeout",
    "GAMEAU_MAX_RETRIES": "gameau_max_retries",
    "DEFAULT_MAX_CHARGE_USDT": "default_max_charge_usdt",
    "MAX_CHARGE_MARGIN_PCT": "max_charge_margin_pct",
    "DEFAULT_STARS_QUANTITY": "default_stars_quantity",
    "HIDE_SENDER": "hide_sender",
    # финансы
    "EXCHANGE_VARIANT": "exchange_variant",
    "RATE_VARIANT_1": "rate_variant_1",
    "RATE_VARIANT_2": "rate_variant_2",
    "WHITEBIRD_USDT_RATE": "rate_variant_2",
    "USDT_PER_1000_STARS": "usdt_per_1000_stars",
    "TRON_ENERGY_FEE_RUB": "tron_energy_fee_rub",
    "COST_PER_STAR_USD": "cost_per_star_usd",
    "REVENUE_PER_STAR_RUB": "revenue_per_star_rub",
    "USD_TO_RUB": "usd_to_rub",
    # telegram
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
    # цикл / надёжность
    "POLL_INTERVAL": "poll_interval",
    "STUCK_TASK_MINUTES": "stuck_task_minutes",
    "RECONCILIATION_INTERVAL_SEC": "reconciliation_interval_sec",
    "MAX_ORDER_RETRIES": "max_order_retries",
    "WAIT_COMPLETION_TIMEOUT": "wait_completion_timeout",
    "STARTUP_RETRY_DELAY": "startup_retry_delay",
    "STARTUP_MAX_RETRIES": "startup_max_retries",
    "CHAT_MONITOR_INTERVAL_SEC": "chat_monitor_interval_sec",
    "LOW_BALANCE_THRESHOLD_USDT": "low_balance_threshold_usdt",
    "BALANCE_CHECK_INTERVAL_MIN": "balance_check_interval_min",
    "STATS_PUSH_INTERVAL_MIN": "stats_push_interval_min",
    "MAX_CONCURRENT_ORDERS": "max_concurrent_orders",
    "SHUTDOWN_TIMEOUT_SEC": "shutdown_timeout",
    # политика выдачи
    "MAX_ORDER_REVENUE_RUB": "max_order_revenue_rub",
    "MIN_MARGIN_PCT": "min_margin_pct",
    "DAILY_SPEND_LIMIT_USDT": "daily_spend_limit_usdt",
    "DUPLICATE_WINDOW_MIN": "duplicate_window_min",
    "DUPLICATE_ACTION": "duplicate_action",
    "BLACKLIST_ENABLED": "blacklist_enabled",
    "REPLY_NEED_USERNAME": "reply_need_username",
    "REPLY_DELIVERED": "reply_delivered",
    "REPLY_HOLD": "reply_hold",
    "REPLY_ERROR": "reply_error",
    "MUTE_HOURS": "mute_hours",
    "METRICS_ENABLED": "metrics_enabled",
    "METRICS_HOST": "metrics_host",
    "METRICS_PORT": "metrics_port",
    "METRICS_TOKEN": "metrics_token",
    # хранилище / логи
    "DB_PATH": "db_path",
    "LOG_FILE": "log_file",
    "LOG_MAX_BYTES": "log_max_bytes",
    "LOG_BACKUP_COUNT": "log_backup_count",
    "LOT_RULES": "lot_rules",
}

# Ключи config.json (плоские и вложенные) -> атрибуты Config
JSON_ALIASES: dict[str, str] = {
    "funpay_golden_key": "golden_key",
    "golden_key": "golden_key",
    "gameau_api_key": "gameau_key",
    "gameau_key": "gameau_key",
    "seller_notify_token": "telegram_bot_token",
    "seller_notify_chat_id": "telegram_chat_id",
    "telegram_bot_token": "telegram_bot_token",
    "telegram_chat_id": "telegram_chat_id",
    "max_charge_usdt": "default_max_charge_usdt",
    "usdt_rate": "rate_variant_2",
}

SECRET_ATTRS = ("golden_key", "gameau_key", "telegram_bot_token")


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
    # запас к цене пакета из каталога для maxCharge (защита от пересписания,
    # но без отказа выдачи при умеренном росте цены)
    max_charge_margin_pct: float = 5.0
    default_stars_quantity: int = 1000
    gameau_timeout: float = 20.0
    gameau_max_retries: int = 3
    hide_sender: bool = False

    # --- Финансовая модель (завод USDT TRC-20 на gameau.us) ---
    # 1 = анонимный обмен / P2P, 2 = Whitebird (РБ), легально, дешевле
    exchange_variant: int = 2
    rate_variant_1: float = 110.00
    rate_variant_2: float = 87.63
    tron_energy_fee_rub: float = 0.0
    # оценка себестоимости, когда каталог GAMEAU недоступен
    usdt_per_1000_stars: float = 9.10
    cost_per_star_usd: float = 0.017
    revenue_per_star_rub: float = 1.5
    usd_to_rub: float = 100.0

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Цикл, надёжность ---
    poll_interval: float = 5.0
    stuck_task_minutes: int = 20
    reconciliation_interval_sec: int = 60
    max_order_retries: int = 2
    wait_completion_timeout: float = 120.0
    chat_monitor_interval_sec: int = 15
    low_balance_threshold_usdt: float = 50.0
    balance_check_interval_min: int = 10
    stats_push_interval_min: int = 0
    startup_retry_delay: float = 15.0
    startup_max_retries: int = 0  # 0 = бесконечно (systemd Restart=always)
    # Сколько параллельно обслуживать заказов (семафор): защита от «шторма»
    # FunPay и от одновременных покупок, съедающих баланс GAMEAU
    max_concurrent_orders: int = 4
    # сколько секунд при остановке ждать завершения начатых выдач
    shutdown_timeout: float = 30.0

    # --- Политика выдачи (риск-менеджмент) ---
    # Проверки идут ДО запроса в GAMEAU: списанные USDT обратно не вернуть.
    # 0 = проверка выключена.
    max_order_revenue_rub: float = 0.0
    min_margin_pct: float = 0.0
    daily_spend_limit_usdt: float = 0.0
    # «тот же ник и то же количество» в этом окне = подозрение на дубль заказа
    duplicate_window_min: int = 15
    duplicate_action: str = "alert"  # alert | hold | ignore
    blacklist_enabled: bool = True  # стоп-лист покупателей (--blacklist)
    # Шаблоны ответов покупателю; пустая строка = не отправлять
    reply_need_username: str = (
        "Здравствуйте! Не удалось автоматически распознать ваш Telegram @username. "
        "Пожалуйста, напишите его ответным сообщением в формате: @username"
    )
    reply_delivered: str = (
        "✅ Здравствуйте! {quantity_spaces} Telegram Stars успешно зачислены на аккаунт @{username}.\n\n"
        "Пожалуйста, проверьте баланс в Telegram и подтвердите успешное выполнение заказа на FunPay! "
        "Спасибо за покупку!"
    )
    reply_hold: str = ""
    reply_error: str = ""

    # --- Уведомления и мониторинг ---
    # «Тихие часы» для некритичных алертов, напр. 23-07 (кросс-полуночные работают);
    # ошибки выдачи, низкий баланс и зависшие задачи проходят всегда
    mute_hours: str = ""
    metrics_enabled: bool = False
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9155
    metrics_token: str = ""

    # --- Хранилище / логи ---
    db_path: str = "autostars.db"
    log_file: str = "autostars.log"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 5

    # --- Правила соответствия лота -> количества звёзд ---
    lot_rules: list[dict[str, Any]] = field(default_factory=list)

    # --- Служебное (не настраивается пользователем) ---
    source_files: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)
    base_dir: str = ""

    # ------------------------------------------------------------------ #
    # Загрузка
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Config:
        """Собирает конфигурацию: config.json -> .env -> переменные окружения."""
        cfg = cls()
        cfg.base_dir = str(app_dir())
        errors: list[str] = []

        # 1. config.json
        path = Path(config_path) if config_path else None
        if path is None:
            path = config_file_path()
        if path and path.exists():
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                cfg._apply_json(data, errors)
                cfg.source_files.append(str(path))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"config.json: не разобран ({exc}) — файл пропущен")

        # 2. .env
        env_file = env_path()
        if env_file.exists():
            _apply_env_file(env_file)
            cfg.source_files.append(str(env_file))

        # 3. os.environ (включая то, что пришло из .env, и то, что задал оператор)
        known = {f.name for f in fields(cls)}
        for env_var, attr in ENV_MAP.items():
            if attr not in known:
                continue
            raw = os.environ.get(env_var)
            if raw is None or raw.strip() == "" or raw.strip() in PLACEHOLDERS:
                continue
            setattr(cfg, attr, _coerce(getattr(cfg, attr), raw, env_var, errors))

        # пути — абсолютные, привязанные к каталогу приложения
        cfg.db_path = str(resolve_user_path(cfg.db_path))
        cfg.log_file = str(resolve_user_path(cfg.log_file))
        cfg.parse_errors = errors
        for message in errors:
            logger.warning(f"Конфигурация: {message}")
        return cfg

    def _apply_json(self, data: dict[str, Any], errors: list[str]) -> None:
        """Плоские и вложенные ключи config.json -> атрибуты."""
        known = {f.name for f in fields(self)}
        flat: dict[str, Any] = {}
        section_map = {
            "API": {"token": "gameau_key", "url": "gameau_base_url", "max_charge_usdt": "default_max_charge_usdt"},
            "FUNPAY": {"golden_key": "golden_key", "user_agent": "funpay_user_agent"},
            "BOT": {"bot_token": "telegram_bot_token", "chat_id": "telegram_chat_id"},
            "FINANCE": {
                "rate_variant_1": "rate_variant_1",
                "rate_variant_2": "rate_variant_2",
                "hide_sender": "hide_sender",
                "cost_per_star_usd": "cost_per_star_usd",
                "revenue_per_star_rub": "revenue_per_star_rub",
            },
            "SETTINGS": {
                "db_path": "db_path",
                "log_file": "log_file",
                "poll_interval": "poll_interval",
                "order_check_interval": "poll_interval",
                "default_stars_quantity": "default_stars_quantity",
                "admin_telegram_id": "telegram_chat_id",
            },
        }
        env_to_attr = {env.lower(): attr for env, attr in ENV_MAP.items()}
        for key, value in data.items():
            if isinstance(value, dict) and key.upper() in section_map:
                for sub_key, sub_value in value.items():
                    attr = section_map[key.upper()].get(str(sub_key).lower(), JSON_ALIASES.get(str(sub_key).lower()))
                    if attr:
                        flat[attr] = sub_value
                continue
            # Плоский ключ может быть именем атрибута (poll_interval), алиасом
            # (max_charge_usdt) или именем из .env (POLL_INTERVAL) — все три формы
            # равнозначны, чтобы не приходилось учить наизусть, «как правильно».
            normalized = str(key).lower()
            flat[JSON_ALIASES.get(normalized) or env_to_attr.get(normalized) or normalized] = value

        for attr, value in flat.items():
            if attr not in known or value is None:
                continue
            current = getattr(self, attr)
            if isinstance(value, str) and isinstance(current, (bool, int, float, list)):
                # значение-строка в JSON трактуется как строка из .env: приводим к
                # типу поля вместо «невероятно»-ошибки «не число, пропущено»
                setattr(self, attr, _coerce(current, value, f"config.json: {attr}", errors))
            elif isinstance(current, bool):
                setattr(self, attr, bool(value))
            elif isinstance(current, (int, float)) and not isinstance(current, bool):
                if isinstance(value, (int, float)):
                    setattr(self, attr, int(value) if isinstance(current, int) else float(value))
                else:
                    errors.append(f"config.json: {attr}={value!r} — ожидалось число, пропущено")
            elif isinstance(current, list) and not isinstance(value, list):
                errors.append(f"config.json: {attr} — ожидается список, пропущено")
            else:
                setattr(self, attr, value)

    # ------------------------------------------------------------------ #
    # Производные значения
    # ------------------------------------------------------------------ #
    @property
    def active_usdt_rate(self) -> float:
        """Курс ₽/USDT активного варианта завода крипты."""
        return self.rate_variant_1 if self.exchange_variant == 1 else self.rate_variant_2

    @property
    def active_rate_label(self) -> str:
        return "Вариант 1 (анонимный обмен)" if self.exchange_variant == 1 else "Вариант 2 (Whitebird)"

    @property
    def max_charge_margin(self) -> float:
        return max(0.0, self.max_charge_margin_pct) / 100.0

    def estimate_cost_usdt(self, stars: int) -> float:
        """Оценка себестоимости звёзд, когда каталог GAMEAU недоступен."""
        per_star = self.usdt_per_1000_stars / 1000.0 if self.usdt_per_1000_stars else self.cost_per_star_usd
        return round(max(0, int(stars)) * per_star, 4)

    def compute_max_charge(self, quantity: int, catalog_price_usdt: float | None = None) -> float:
        """Лимит списания для заказа: не меньше цены пакета + запас.

        Дефолтный `default_max_charge_usdt` рассчитан на пакет 1000⭐; для
        больших заказов он гарантированно поднял бы PRICE_EXCEEDED, поэтому
        лимит всегда >= фактической цены пакета из каталога с запасом.
        """
        estimated = catalog_price_usdt
        if estimated is None:
            estimated = self.estimate_cost_usdt(quantity)
        floor = float(estimated) * (1.0 + self.max_charge_margin)
        limit = max(float(self.default_max_charge_usdt), floor)
        # округление вверх до 0.01 — чтобы запас не съедался округлением вниз
        return round(limit + 0.009999, 2)

    @property
    def resolved_db_path(self) -> str:
        return self.db_path

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.strip() and str(self.telegram_chat_id).strip())

    # ------------------------------------------------------------------ #
    # Политика выдачи и уведомления
    # ------------------------------------------------------------------ #
    def reply_templates(self) -> ReplyTemplates:  # noqa: F821 - тип из services.policy
        from .services.policy import ReplyTemplates

        return ReplyTemplates(
            need_username=self.reply_need_username,
            delivered=self.reply_delivered,
            hold=self.reply_hold,
            error=self.reply_error,
        )

    def order_policy(self) -> OrderPolicy:  # noqa: F821
        """Собирает объект политики (см. autostars/services/policy.py)."""
        from .services.policy import OrderPolicy

        return OrderPolicy(
            max_order_revenue_rub=float(self.max_order_revenue_rub or 0),
            min_margin_pct=float(self.min_margin_pct or 0),
            daily_spend_limit_usdt=float(self.daily_spend_limit_usdt or 0),
            duplicate_window_min=int(self.duplicate_window_min or 0),
            duplicate_action=str(self.duplicate_action or "alert"),
            blacklist_enabled=bool(self.blacklist_enabled),
            rate_rub_per_usdt=self.active_usdt_rate,
            templates=self.reply_templates(),
        )

    def mute_window(self) -> tuple[int, int] | None:
        """«Тихие часы» из `MUTE_HOURS` ("23-07", "23:00-07:00") или None."""
        return parse_mute_window(self.mute_hours)

    def is_muted(self, moment: float | None = None) -> bool:
        """Сейчас «тихие часы»? (критичные алерты этим не подавляются)"""
        window = self.mute_window()
        if not window:
            return False
        import time as _time

        hour = _time.localtime(moment if moment is not None else _time.time()).tm_hour
        start, end = window
        return hour >= start or hour < end if start > end else start <= hour < end

    # ------------------------------------------------------------------ #
    # Диагностика
    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        """Бросает ValueError при отсутствии обязательных ключей (обратная совместимость)."""
        missing = list(self.issues(fatal_only=True))
        if missing:
            raise ValueError(
                "Отсутствуют обязательные параметры конфигурации: "
                + ", ".join(m.split(": ", 1)[1] for m in missing)
                + ". Укажите их в .env или config.json."
            )

    def issues(self, fatal_only: bool = False) -> list[str]:
        """Список замечаний; строки начинаются с 'missing:' — критичные."""
        out: list[str] = []
        if not self.golden_key.strip() or self.golden_key.strip() in PLACEHOLDERS:
            out.append("missing: FUNPAY_GOLDEN_KEY")
        if not self.gameau_key.strip() or self.gameau_key.strip() in PLACEHOLDERS:
            out.append("missing: GAMEAU_API_KEY")
        if fatal_only:
            return out

        if self.poll_interval < 1:
            out.append("POLL_INTERVAL < 1 сек — слишком агрессивный опрос FunPay, поднимите до 3-5")
        if self.wait_completion_timeout < 10:
            out.append("WAIT_COMPLETION_TIMEOUT < 10 сек — выдача не успеет подтвердиться")
        if self.reconciliation_interval_sec < 10:
            out.append("RECONCILIATION_INTERVAL_SEC < 10 сек — лишние запросы к GAMEAU")
        if self.max_concurrent_orders < 1:
            out.append("MAX_CONCURRENT_ORDERS < 1 — должно быть не меньше 1 (используется 4)")
        if self.startup_retry_delay < 1:
            out.append("STARTUP_RETRY_DELAY < 1 сек — при недоступном FunPay будет спам в лог")
        if self.default_max_charge_usdt <= 0:
            out.append("DEFAULT_MAX_CHARGE_USDT <= 0 — лимит списания должен быть положительным")
        if self.max_charge_margin_pct < 0:
            out.append("MAX_CHARGE_MARGIN_PCT < 0 — запас не может быть отрицательным")
        if not 1 <= int(self.exchange_variant) <= 2:
            out.append(f"EXCHANGE_VARIANT={self.exchange_variant} — допустимо 1 или 2, используется 2")
        if self.rate_variant_1 <= 0 or self.rate_variant_2 <= 0:
            out.append("Курс USDT (RATE_VARIANT_1/2) должен быть положительным")
        if self.telegram_enabled and not str(self.telegram_chat_id).lstrip("-").isdigit():
            out.append("TELEGRAM_CHAT_ID выглядит нечисловым — алерты могут не дойти")
        if not self.telegram_enabled:
            out.append("Telegram не настроен — уведомления владельцу и TG-команды выключены")
        # политика выдачи и мелкая операционка
        if self.duplicate_action.lower() not in {"alert", "hold", "ignore"}:
            out.append(
                f"DUPLICATE_ACTION={self.duplicate_action!r} — допустимо alert | hold | ignore "
                "(используется alert)"
            )
        if self.max_order_revenue_rub <= 0 and self.daily_spend_limit_usdt <= 0:
            out.append(
                "Все лимиты выдачи (MAX_ORDER_REVENUE_RUB, DAILY_SPEND_LIMIT_USDT) выключены — "
                "политика guardит только дубли, стоп-лист и отрицательную маржу"
            )
        if self.min_margin_pct > 90:
            out.append(f"MIN_MARGIN_PCT={self.min_margin_pct:g} подозрительно высокий — заказы будут задерживаться")
        if self.mute_hours and parse_mute_window(self.mute_hours) is None:
            out.append(f"MUTE_HOURS={self.mute_hours!r} не читается — нужен формат «23-07» (часы 0..23); подавления нет")
        if self.metrics_enabled and not self.metrics_token:
            host = str(self.metrics_host or "").strip().lower()
            if host not in {"127.0.0.1", "localhost", "::1"}:
                out.append(
                    f"METRICS_HOST={self.metrics_host!r} с открытым портом без METRICS_TOKEN — "
                    "снимок читают все; поставьте токен или верните 127.0.0.1"
                )
        for message in self.parse_errors:
            out.append(message)
        return out

    def to_safe_dict(self) -> dict[str, Any]:
        """Словарь для GUI/диагностики: секреты замаскированы, пути развёрнуты."""
        data: dict[str, Any] = {}
        for f in fields(self):
            if f.name in ("source_files", "parse_errors"):
                continue
            value = getattr(self, f.name)
            if f.name in SECRET_ATTRS:
                value = mask_secret(str(value))
            data[f.name] = value
        data["source_files"] = list(self.source_files)
        data["parse_errors"] = list(self.parse_errors)
        return data

    def summary(self) -> str:
        rate = self.active_usdt_rate
        return (
            f"DB: {self.db_path} | курс {rate:.2f} ₽/USDT ({self.active_rate_label}) | "
            f"maxCharge ≥ {self.default_max_charge_usdt:.2f} USDT (+{self.max_charge_margin_pct:g}%) | "
            f"poll {self.poll_interval:g}s | TG {'вкл' if self.telegram_enabled else 'выкл'}"
        )


def parse_mute_window(spec: object) -> tuple[int, int] | None:
    """
    Часы «тихих часов» владельца: `"23-07"`, `"23:00-07:00"`, `"23 до 7"` → `(23, 7)`.

    Пустое и нечитаемое значение → None, то есть подавление выключено (безопаснее по
    умолчанию: владельцу приходят все алерты). Начало больше конца — окно через
    полночь; «23-23» — нулевая длина, тоже считается выключенным.
    """
    raw = str(spec or "").strip()
    if not raw:
        return None
    match = re.match(
        r"^(\d{1,2})(?:[:.]?(\d{2}))?\s*(?:-|–|—|to|до)\s*(\d{1,2})(?:[:.]?(\d{2}))?$",
        raw,
        re.IGNORECASE,
    )
    if not match:
        return None
    start, end = int(match.group(1)) % 24, int(match.group(3)) % 24
    if start == end:
        return None
    return start, end


def mask_secret(value: str, keep: int = 4) -> str:
    """'abcd…********…wxyz' — чтобы ключ можно было узнать в логе, но не украсть."""
    return _mask_secret(value, keep)

