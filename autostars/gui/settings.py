"""Редактор `.env` для GUI: безопасно править конфигурацию, не зная синтаксис.

`.env` — единственный «источник истины» движка (см. autostars.config), поэтому GUI
пишет именно его, а не ещё какой-то свой JSON: меньше шансов получить ситуацию,
«в интерфейсе одно, а бот работает по другому».

Правила:
  • комментарии, порядок и неизвестные ключи сохраняются как были;
  • пустое поле = «не трогать» (пароли/ключи не затираются случайным «очистить»);
  • перед записью — бэкап `.env.bak` и валидация чисел (кривой POLL_INTERVAL
    раньше ронял процесс с голым ValueError).
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from autostars.config import ENV_MAP, PLACEHOLDERS, Config, env_path, parse_env_text

BACKUP_SUFFIX = ".bak"


@dataclass
class SettingField:
    """Описание одного поля формы настроек."""

    key: str
    label: str
    kind: str = "text"  # text | secret | number | int | bool | choice | longtext
    help: str = ""
    choices: tuple[str, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    section: str = "Общее"
    default: str = ""


SECTION_KEYS = (
    "Общее",
    "FunPay",
    "GAMEAU",
    "Финансы",
    "Telegram",
    "Надёжность",
    "Политика выдачи",
    "Ответы покупателю",
    "Логи и БД",
)


FIELDS: list[SettingField] = [
    # --- FunPay ---
    SettingField("FUNPAY_GOLDEN_KEY", "Golden key продавца (FunPay)", "secret",
                 "Cookies funpay.com → golden_key. Обязателен.", section="FunPay"),
    SettingField("FUNPAY_BASE_URL", "Адрес FunPay", "text", "Обычно не меняется.",
                 default="https://funpay.com", section="FunPay"),
    SettingField("FUNPAY_USER_AGENT", "User-Agent для FunPay", "text",
                 "403 от FunPay часто лечится актуальным UA браузера.", section="FunPay"),
    # --- GAMEAU ---
    SettingField("GAMEAU_API_KEY", "API-ключ GAMEAU", "secret",
                 "gameau.us → «API для интеграций». Обязателен.", section="GAMEAU"),
    SettingField("GAMEAU_BASE_URL", "Базовый адрес API", "text", "По умолчанию https://gameau.us/api/v1",
                 default="https://gameau.us/api/v1", section="GAMEAU"),
    SettingField("GAMEAU_TIMEOUT", "GAMEAU: таймаут запроса, сек", "number",
                 "При медленном канале увеличьте (например 30–45).", min_value=1, max_value=300,
                 default="20", section="GAMEAU"),
    SettingField("GAMEAU_MAX_RETRIES", "GAMEAU: повторов при сетевом сбое", "int",
                 "Только сетевые ошибки; бизнес-отказ не повторяется, чтобы не купить дважды.",
                 min_value=0, max_value=10, default="3", section="GAMEAU"),
    SettingField("DEFAULT_MAX_CHARGE_USDT", "Лимит списания на заказ, USDT", "number",
                 "Минимальный maxCharge; для крупных заказов считается автоматически от цены пакета.",
                 min_value=0.1, default="9.50", section="GAMEAU"),
    SettingField("MAX_CHARGE_MARGIN_PCT", "Запас к цене пакета, %", "number",
                 "На сколько % превышаем цену каталога в maxCharge (защита от роста цены).",
                 min_value=0, max_value=100, default="5", section="GAMEAU"),
    SettingField("LOW_BALANCE_THRESHOLD_USDT", "Порог низкого баланса, USDT", "number",
                 "Ниже — алерт владельцу и предупреждение в GUI.", min_value=0, default="50", section="GAMEAU"),
    # --- Финансы ---
    SettingField("EXCHANGE_VARIANT", "Активный курс завода USDT", "choice",
                 "1 = анонимный обмен/P2P, 2 = Whitebird (РБ)", choices=("1", "2"),
                 default="2", section="Финансы"),
    SettingField("RATE_VARIANT_1", "Курс варианта 1, ₽/USDT", "number", "", min_value=1, default="110.00",
                 section="Финансы"),
    SettingField("RATE_VARIANT_2", "Курс варианта 2, ₽/USDT", "number", "", min_value=1, default="87.63",
                 section="Финансы"),
    SettingField("USDT_PER_1000_STARS", "Себестоимость 1000⭐, USDT", "number",
                 "Оценка, когда каталог GAMEAU недоступен.", min_value=0.01, default="9.10", section="Финансы"),
    SettingField("TRON_ENERGY_FEE_RUB", "Комиссия завода (₽ на сделку)", "number",
                 "Учитывается в прибыли/убытках.", min_value=0, default="0", section="Финансы"),
    SettingField("COST_PER_STAR_USD", "Себестоимость 1⭐, USD", "number",
                 "Тонкая настройка оценок (см. docs/ARCHITECTURE.md).", min_value=0.0001, default="0.017",
                 section="Финансы"),
    SettingField("REVENUE_PER_STAR_RUB", "Выручка за 1⭐, ₽", "number",
                 "Средняя цена продажи звезды на FunPay (для маржи).", min_value=0.01, default="1.5",
                 section="Финансы"),
    SettingField("USD_TO_RUB", "Курс USD→₽ (грубая оценка)", "number",
                 "Используется, когда не задан активный вариант курса.", min_value=1, default="100",
                 section="Финансы"),
    SettingField("HIDE_SENDER", "Скрывать отправителя", "bool", "hide_sender в GAMEAU.",
                 default="false", section="Финансы"),
    # --- Telegram ---
    SettingField("TELEGRAM_BOT_TOKEN", "Токен Telegram-бота", "secret",
                 "@BotFather → /newbot. Без него нет алертов и TG-команд.", section="Telegram"),
    SettingField("TELEGRAM_CHAT_ID", "Chat ID владельца", "text",
                 "Только этот chat_id может слать команды боту.", section="Telegram"),
    # --- Надёжность / цикл ---
    SettingField("POLL_INTERVAL", "Интервал опроса, сек", "number", "", min_value=1, max_value=600,
                 default="5.0", section="Надёжность"),
    SettingField("DEFAULT_STARS_QUANTITY", "Звёзд по умолчанию", "int",
                 "Если количество не распознано в сообщении покупателя.", min_value=50,
                 default="1000", section="Надёжность"),
    SettingField("MAX_ORDER_RETRIES", "Повторы при сетевых сбоях", "int", "", min_value=0, max_value=10,
                 default="2", section="Надёжность"),
    SettingField("WAIT_COMPLETION_TIMEOUT", "Ожидание статуса GAMEAU, сек", "int", "", min_value=10,
                 max_value=3600, default="120", section="Надёжность"),
    SettingField("RECONCILIATION_INTERVAL_SEC", "Reconciliation, сек", "int", "", min_value=10, default="60",
                 section="Надёжность"),
    SettingField("CHAT_MONITOR_INTERVAL_SEC", "Чат-мониторинг @username, сек", "int", "", min_value=5,
                 default="15", section="Надёжность"),
    SettingField("MAX_CONCURRENT_ORDERS", "Параллельно обрабатывать заказов", "int",
                 "Семафор выдачи: не больше N заказов одновременно.", min_value=1, max_value=32,
                 default="4", section="Надёжность"),
    SettingField("STUCK_TASK_MINUTES", "Задача «зависла», минут", "int", "", min_value=1, default="20",
                 section="Надёжность"),
    SettingField("STATS_PUSH_INTERVAL_MIN", "Статистика в Telegram, минут (0 = выкл)", "int",
                 "", min_value=0, max_value=1440, default="0", section="Надёжность"),
    SettingField("STARTUP_RETRY_DELAY", "Пауза повторов логина FunPay, сек", "number", "", min_value=1,
                 default="15", section="Надёжность"),
    SettingField("STARTUP_MAX_RETRIES", "Старт: попыток логина всего", "int",
                 "0 — бесконечно (рекомендуется с systemd/Docker, которые и так перезапускают сервис).",
                 min_value=0, max_value=100000, default="0", section="Надёжность"),
    SettingField("BALANCE_CHECK_INTERVAL_MIN", "Баланс: период проверки, мин", "int",
                 "Как часто перечитывать баланс GAMEAU (0 — только при списании).",
                 min_value=0, max_value=1440, default="10", section="Надёжность"),
    SettingField("SHUTDOWN_TIMEOUT_SEC", "Таймаут graceful shutdown, сек", "int",
                 "Сколько ждать текущие выдачи при остановке/`stop`.", min_value=1, max_value=600,
                 default="30", section="Надёжность"),
    # --- Политика выдачи (защита от убытка) ---
    SettingField("MAX_ORDER_REVENUE_RUB", "Лимит сделки, ₽ (0 = выкл)", "number",
                 "Заказ с выручкой больше этой суммы задерживается до вашего решения.",
                 min_value=0, default="0", section="Политика выдачи"),
    SettingField("MIN_MARGIN_PCT", "Минимальная маржа, % (0 = выкл)", "number",
                 "Ниже порога заказ задерживается. Считается по курсу выбранного варианта обмена.",
                 min_value=0, max_value=100, default="0", section="Политика выдачи"),
    SettingField("DAILY_SPEND_LIMIT_USDT", "Суточный бюджет закупки, USDT (0 = выкл)", "number",
                 "Суммарные списания с полуночи: превысили — держим, а не покупаем.",
                 min_value=0, default="0", section="Политика выдачи"),
    SettingField("DUPLICATE_WINDOW_MIN", "Окно поиска дублей, минут", "int",
                 "0 — не искать. Тот же ник и то же количество в окне = подозрение на дубль.",
                 min_value=0, max_value=1440, default="15", section="Политика выдачи"),
    SettingField("DUPLICATE_ACTION", "Дубль: что делать", "choice",
                 "alert — предупредить и выдать; hold — задержать; ignore — не реагировать.",
                 choices=("alert", "hold", "ignore"), default="alert", section="Политика выдачи"),
    SettingField("BLACKLIST_ENABLED", "Учитывать чёрный список покупателей", "bool",
                 "Задерживает заказы ником из стоп-листа (кнопка на вкладке «Выдача»).",
                 default="true", section="Политика выдачи"),
    SettingField("MUTE_HOURS", "Тихие часы для некритичных алертов", "text",
                 "Например 23-07 или 23:00-07:00. Ошибки, низкий баланс и задержки приходят всегда.",
                 default="", section="Политика выдачи"),
    SettingField("METRICS_ENABLED", "Метрики Prometheus / healthz", "bool",
                 "Выключено по умолчанию; слушает только METRICS_HOST (127.0.0.1).",
                 default="false", section="Политика выдачи"),
    SettingField("METRICS_HOST", "Метрики: адрес", "text", "Наружу не выставляйте без reverse-proxy.",
                 default="127.0.0.1", section="Политика выдачи"),
    SettingField("METRICS_PORT", "Метрики: порт", "int", "", min_value=1, max_value=65535,
                 default="9155", section="Политика выдачи"),
    SettingField("METRICS_TOKEN", "Метрики: Bearer-токен", "secret",
                 "Пусто — доступ без пароля (ок для localhost).", section="Политика выдачи"),
    # --- Ответы покупателю (шаблоны) ---
    SettingField("REPLY_NEED_USERNAME", "Ответ: нужен @username", "longtext",
                 "Пусто — не отправляем. Подстановки: {username} {quantity} {quantity_spaces} {order_id} {price_rub}",
                 default="", section="Ответы покупателю"),
    SettingField("REPLY_DELIVERED", "Ответ: звёзды зачислены", "longtext",
                 "Пусто — не отправляем.", default="", section="Ответы покупателю"),
    SettingField("REPLY_HOLD", "Ответ: заказ задержан", "longtext",
                 "Пусто — молчим. Есть смысл, если покупатели спрашивают, почему долго.",
                 default="", section="Ответы покупателю"),
    SettingField("REPLY_ERROR", "Ответ: выдача не удалась", "longtext",
                 "Пусто — молчим. Помните об обещаниях возврата: текст = публичное обязательство.",
                 default="", section="Ответы покупателю"),
    # --- Логи и БД ---
    SettingField("DB_PATH", "Путь к базе SQLite", "text", "Можно абсолютный (для Docker/systemd).",
                 default="autostars.db", section="Логи и БД"),
    SettingField("LOG_FILE", "Файл лога", "text", "", default="autostars.log", section="Логи и БД"),
    SettingField("LOG_MAX_BYTES", "Размер лога до ротации, байт", "int", "", min_value=100_000,
                 default="5000000", section="Логи и БД"),
    SettingField("LOG_BACKUP_COUNT", "Число архивов лога", "int", "", min_value=0, max_value=50,
                 default="5", section="Логи и БД"),
]

FIELDS_BY_SECTION: dict[str, list[SettingField]] = {}
for _field in FIELDS:
    FIELDS_BY_SECTION.setdefault(_field.section, []).append(_field)

_BOOL_TRUE = {"1", "true", "yes", "on", "да", "истина"}


def target_env_file() -> Path:
    """Файл, который GUI будет править (тот же, что читает движок)."""
    return env_path()


#: Плейсхолдеры, которые понимает `ReplyTemplates.render` (services/policy.py).
TEMPLATE_PLACEHOLDERS = ("username", "quantity", "quantity_spaces", "order_id", "price_rub", "reason")


def engine_default_for(key: str) -> str:
    """Значение по умолчанию самого движка для ключа .env (для шаблонов).

    Нужно, чтобы поле формы показывало реальное поведение: если REPLY_DELIVERED в
    файле нет, движок использует встроенный текст, и пустое поле означало бы
    «выключить ответ покупателю» — обидная ловушка при «сохранил как есть».
    """
    attr = ENV_MAP.get(key)
    if not attr:
        return ""
    default = getattr(Config(), attr, "")
    return "" if isinstance(default, bool) else str(default or "")


def read_settings(path: Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    file_path = path or target_env_file()
    if file_path.exists():
        with contextlib.suppress(OSError):
            values = parse_env_text(file_path.read_text(encoding="utf-8"))
    for key in os.environ:
        if key in {f.key for f in FIELDS} and key not in values:
            values[key] = os.environ[key]
    return values


def display_value(field: SettingField, values: dict[str, str]) -> str:
    """Значение для поля формы ('' для секрета — чтобы не показывать и не затирать)."""
    if field.kind == "longtext":
        # ключ отсутствовал в .env → показываем текст движка, а не пустоту
        raw = values.get(field.key)
        return engine_default_for(field.key) if raw is None else raw
    raw = values.get(field.key, "")
    if raw.strip() in PLACEHOLDERS:
        raw = ""
    if field.kind == "bool":
        return "да" if raw.strip().lower() in _BOOL_TRUE else "нет"
    if field.kind == "secret":
        return ""  # секрет в поле не подставляем: пустое поле = «без изменений»
    return raw or field.default


def validate(fields_values: dict[str, str], known: list[SettingField] | None = None) -> list[str]:
    """Возвращает список ошибок (пусто — сохранять можно)."""
    errors: list[str] = []
    for field in known or FIELDS:
        raw = (fields_values.get(field.key) or "").strip()
        if not raw:
            continue
        if field.kind in ("number", "int"):
            try:
                value = float(raw.replace(",", "."))
            except ValueError:
                errors.append(f"{field.label}: «{raw}» — ожидалось число")
                continue
            if field.kind == "int" and abs(value - int(value)) > 1e-9:
                errors.append(f"{field.label}: нужно целое число (получено {raw})")
            if field.min_value is not None and value < field.min_value:
                errors.append(f"{field.label}: не меньше {field.min_value:g}")
            if field.max_value is not None and value > field.max_value:
                errors.append(f"{field.label}: не больше {field.max_value:g}")
        elif field.kind == "choice" and field.choices and raw not in field.choices:
            errors.append(f"{field.label}: допустимо {', '.join(field.choices)}")
        elif field.kind == "bool" and raw.lower() not in _BOOL_TRUE | {"0", "false", "no", "off", "нет"}:
            errors.append(f"{field.label}: для переключателя нужно «да» или «нет»")
        elif field.kind == "longtext":
            unknown = sorted(
                {
                    name
                    for name in re.findall(r"\{(\w+)\}", raw)
                    if name not in TEMPLATE_PLACEHOLDERS
                }
            )
            if unknown:
                errors.append(
                    f"{field.label}: неизвестная подстановка {{{unknown[0]}}} — "
                    "такой текст уйдёт покупателю как есть"
                )
    return errors


def normalize(field: SettingField, raw: str) -> str:
    """Приводит ввод к виду, который понимает парсер .env движка."""
    value = (raw or "").strip()
    if field.kind == "bool":
        return "true" if value.lower() in _BOOL_TRUE else "false"
    if field.kind == "int":
        with contextlib.suppress(ValueError):
            return str(int(float(value.replace(",", "."))))
    if field.kind == "number":
        with contextlib.suppress(ValueError):
            return f"{float(value.replace(',', '.')):g}"
    if field.kind == "longtext":
        return "\n".join(line.rstrip() for line in value.splitlines()).strip()
    return value


def plan_updates(fields_values: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    """Что реально менять: пропускаем пустые секреты и неизменённые значения."""
    updates: dict[str, str] = {}
    for field in FIELDS:
        if field.key not in fields_values:
            continue
        raw = fields_values[field.key]
        if field.kind in ("secret",) and not (raw or "").strip():
            continue
        new_value = normalize(field, raw)
        if new_value == (current.get(field.key) or "").strip():
            continue
        if field.kind == "longtext" and field.key not in current and new_value == engine_default_for(field.key):
            # ключ в .env отсутствует и пользователь ничего не поменял:
            # движок и так использует этот текст — не плодим строку и лишний .bak
            continue
        updates[field.key] = new_value
    return updates


def _render_value(value: str) -> str:
    """Как записать значение в .env, чтобы многострочный текст не сломал файл.

    Движок (parse_env_text) разворачивает \n и \" только внутри двойных кавычек,
    поэтому переносы строк и кавычки экранируем и оборачиваем — файл остаётся
    построчным, а шаблон сохраняется как был.
    """
    text = value if value is not None else ""
    if "\n" not in text and "\r" not in text and '"' not in text:
        return text
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def apply_updates(updates: dict[str, str], path: Path | None = None, backup: bool = True) -> tuple[Path, int]:
    """Перезаписывает .env, сохраняя комментарии/порядок; новые ключи — в конец.

    Возвращает (путь к файлу, количество изменённых ключей).
    """
    file_path = Path(path or target_env_file())
    file_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if file_path.exists():
        lines = file_path.read_text(encoding="utf-8").splitlines()

    seen: dict[str, int] = {}
    key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for index, line in enumerate(lines):
        match = key_re.match(line)
        if match:
            seen[match.group(1)] = index

    current = parse_env_text("\n".join(lines)) if lines else {}
    changed = 0
    for key, value in updates.items():
        rendered = f"{key}={_render_value(value)}"
        if key in seen:
            # сравнение по смыслу, а не по строке: «5.0» уже записано, хотя в файле
            # стоит «5.0  # коммент» — не теряем комментарий и не делаем лишний бэкап
            if current.get(key) is not None and current[key].strip() == value:
                continue
            lines[seen[key]] = rendered
            changed += 1
        else:
            lines.append(rendered)
            changed += 1

    if changed:
        if backup and file_path.exists():
            # бэкап — только когда реально что-то меняем (иначе GUI плодит .bak на пустяках)
            with contextlib.suppress(OSError):
                file_path.with_name(file_path.name + BACKUP_SUFFIX).write_text(
                    "\n".join(file_path.read_text(encoding="utf-8").splitlines()) + "\n",
                    encoding="utf-8",
                )
        text = "\n".join(lines).rstrip("\n") + "\n"
        file_path.write_text(text, encoding="utf-8")
        # чтобы последующие чтения (Config.load) видели новые значения
        for key, value in updates.items():
            os.environ[key] = value
    return file_path, changed


def missing_required(path: Path | None = None) -> list[str]:
    values = read_settings(path)
    required = {"FUNPAY_GOLDEN_KEY": "FUNPAY_GOLDEN_KEY", "GAMEAU_API_KEY": "GAMEAU_API_KEY"}
    out: list[str] = []
    for key, label in required.items():
        value = (values.get(key) or "").strip()
        if not value or value in PLACEHOLDERS:
            out.append(label)
    return out
