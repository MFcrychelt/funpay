"""Ядро автовыдачи.

Цикл обработки:
    1. Получаем список заказов со статусом «оплачен» с FunPay.
    2. Для каждого заказа определяем количество звёзд (правила по лоту /
       парсинг описания / значение по умолчанию).
    3. Ищем юзернейм Telegram: сначала в описании заказа, затем в чате
       заказа (сообщения покупателя, новые — первыми).
    4. Если юзернейма нет — просим покупателя прислать его и ждём.
    5. Когда юзернейм найден — отправляем звёзды через GAMEAU
       (с сохранённым `Idempotency-Key`, чтобы перезапуск не привёл
       к повторному списанию), дожидаемся завершения.
    6. Отвечаем покупателю и отмечаем заказ выданным в состоянии.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import requests

from .config import Config
from .funpay_client import FunPayClient, FunPayError
from .gameau import (
    GameauClient,
    GameauError,
    GameauTimeoutError,
    InsufficientBalanceError,
    PriceChangedError,
)
from .logger_setup import get_logger
from .state import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEW,
    STATUS_NO_STARS_RULE,
    STATUS_SENDING,
    STATUS_WAITING_USERNAME,
    State,
)
from .username_parser import best_username

logger = get_logger("delivery")

_STARS_WORDS = (
    r"(?:зв[её]зд(?:а|ы|очек|очк[иау])?|зв[её]зды|"
    r"тел[её]грам(?:м)?\s*зв[её]зд(?:ы)?|тг\s*зв[её]зды|"
    r"telegram\s+stars?|tg\s+stars?|stars?)"
)

#: «100 звёзд», «250 stars».
RE_NUM_THEN_WORD = re.compile(r"(\d{1,9})\s*" + _STARS_WORDS, re.IGNORECASE)
#: «Звёзды 100», «Telegram Stars 250».
RE_WORD_THEN_NUM = re.compile(_STARS_WORDS + r"\s*(\d{1,9})", re.IGNORECASE)
#: «50⭐», «250 ✯».
RE_NUM_THEN_STAR = re.compile(r"(\d{1,9})\s*(?:⭐|✯)", re.IGNORECASE)
#: «⭐ 750», «✯ 250».
RE_STAR_THEN_NUM = re.compile(r"(?:⭐|✯)\s*(\d{1,9})", re.IGNORECASE)


def stars_from_description(text: str) -> int | None:
    """Определяет количество звёзд из названия/описания лота.

    Поддерживает порядок «100 звёзд», «Звёзды 100» и символы «50⭐»/«⭐ 50».
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


class DeliveryEngine:
    """Основной обработчик заказов."""

    def __init__(self, cfg: Config, funpay: FunPayClient, gameau: GameauClient,
                 state: State):
        self.cfg = cfg
        self.funpay = funpay
        self.gameau = gameau
        self.state = state
        self._last_balance_check = 0.0
        self._balance: float | None = None

    # ------------------------------------------------------------------ #
    # Публичный интерфейс
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        """Бесконечный цикл автовыдачи."""
        logger.info("Автовыдача запущена. Опрашиваю заказы каждые %.0f с.",
                    self.cfg.poll_interval)
        self._startup_report()
        removed = self.state.prune()
        if removed:
            logger.info("Удалено старых записей состояния: %d", removed)
        while True:
            started = time.time()
            try:
                self.funpay.ensure_session()
                orders = self.funpay.pending_orders()
                logger.debug("Оплаченных заказов в очереди: %d", len(orders))
                for order in orders:
                    try:
                        self.process_order(order)
                    except FunPayError as exc:
                        logger.error("FunPay: %s", exc)
                    except GameauError as exc:
                        logger.error("GAMEAU: %s", exc)
                    except Exception:  # noqa: BLE001
                        logger.exception("Непредвиденная ошибка при обработке заказа #%s",
                                         getattr(order, "id", "?"))
            except FunPayError as exc:
                logger.error("Не удалось получить заказы: %s", exc)
                self._try_relogin()
            except Exception:  # noqa: BLE001
                logger.exception("Ошибка цикла опроса заказов")
            elapsed = time.time() - started
            time.sleep(max(1.0, self.cfg.poll_interval - elapsed))

    def process_pending_once(self) -> int:
        """Однократный прогон (для режима `once`). Возвращает число заказов в очереди."""
        self.funpay.ensure_session()
        orders = self.funpay.pending_orders()
        for order in orders:
            try:
                self.process_order(order)
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка обработки заказа #%s: %s",
                             getattr(order, "id", "?"), exc)
        return len(orders)

    # ------------------------------------------------------------------ #
    # Обработка одного заказа
    # ------------------------------------------------------------------ #
    def process_order(self, order: Any) -> None:
        order_id = str(order.id)
        rec = self.state.get_order(order_id)

        if rec and rec.get("status") == STATUS_DONE:
            return  # уже выдан
        if rec and rec.get("status") == STATUS_FAILED and not rec.get("retry_allowed"):
            return  # ждёт ручного вмешательства

        # 1) Сколько звёзд выдавать?
        stars = self._resolve_stars(order)
        if stars is None:
            if not rec or rec.get("status") != STATUS_NO_STARS_RULE:
                logger.warning(
                    "Заказ #%s («%s»): не удалось определить количество звёзд. "
                    "Добавьте правило в lot_rules или default_stars.",
                    order_id, order.description,
                )
                self.state.upsert_order(order_id, status=STATUS_NO_STARS_RULE,
                                        description=order.description,
                                        price=order.price)
            return

        # 2) Ищем юзернейм: описание заказа, затем чат.
        username = self._find_username(order)

        if not username:
            self._handle_missing_username(order, stars)
            return

        # 3) Выдаём звёзды.
        self._deliver(order, stars, username)

    # ------------------------------------------------------------------ #
    # Шаг 1: количество звёзд
    # ------------------------------------------------------------------ #
    def _resolve_stars(self, order: Any) -> int | None:
        description = order.description or ""

        amount = getattr(order, "amount", None) or 1
        try:
            amount = max(1, int(amount))
        except (TypeError, ValueError):
            amount = 1

        # Правила из конфига: {"pattern": "...", "stars": N}.
        for rule in self.cfg.lot_rules:
            pattern = rule.get("pattern")
            stars = rule.get("stars")
            if not pattern or stars is None:
                continue
            try:
                match = re.search(pattern, description, re.IGNORECASE)
            except re.error:
                logger.warning("Некорректный regex в правиле лота: %s", pattern)
                continue
            if match:
                base = int(stars) * amount
                logger.debug("Заказ #%s: правило «%s» → %d звёзд (×%d).",
                             order.id, pattern, base, amount)
                return base

        # Парсинг описания лота: «100 звёзд», «250 stars», «Звёзды 250», «50⭐».
        parsed = stars_from_description(description)
        if parsed is not None:
            base = parsed * amount
            logger.debug("Заказ #%s: из описания «%s» → %d звёзд.",
                         order.id, description, base)
            return base

        if self.cfg.default_stars:
            logger.debug("Заказ #%s: использую default_stars=%d.",
                         order.id, self.cfg.default_stars)
            return int(self.cfg.default_stars) * amount
        return None

    # ------------------------------------------------------------------ #
    # Шаг 2: поиск юзернейма
    # ------------------------------------------------------------------ #
    def _find_username(self, order: Any) -> str | None:
        # Сначала — описание заказа (покупатели часто пишут юзернейм там).
        username = best_username([order.description or ""])
        if username:
            logger.info("Заказ #%s: юзернейм найден в описании: @%s", order.id, username)
            return username

        # Затем — чат заказа (только сообщения покупателя).
        messages = self.funpay.order_messages(order)
        seller_id = self.funpay.account.id if self.funpay.account else None
        buyer_texts = [
            m.text for m in reversed(messages)
            if m.text and m.author_id != seller_id
        ]
        username = best_username(buyer_texts)
        if username:
            logger.info("Заказ #%s: юзернейм найден в чате: @%s", order.id, username)
            return username
        return None

    def _handle_missing_username(self, order: Any, stars: int) -> None:
        order_id = str(order.id)
        now = time.time()
        rec = self.state.get_order(order_id) or {}

        first_seen = rec.get("created_ts") or now
        asked_at = rec.get("asked_username_at") or 0.0

        if self.cfg.ask_username_in_chat and not asked_at:
            if self.funpay.send_message(order, self.cfg.msg_need_username):
                self.state.upsert_order(
                    order_id, status=STATUS_WAITING_USERNAME, stars=stars,
                    asked_username_at=now, description=order.description,
                )
                logger.info("Заказ #%s: запросил юзернейм у покупателя (%d звёзд ожидаем).",
                            order_id, stars)
                self._notify_seller(
                    f"🕐 Заказ #{order_id} оплачен ({stars} звёзд), "
                    f"жду юзернейм от покупателя."
                )
            return

        # Напоминание раз в reminder_interval.
        if self.cfg.remind_username and asked_at:
            if now - asked_at >= self.cfg.reminder_interval:
                if self.funpay.send_message(order, self.cfg.msg_username_reminder):
                    self.state.upsert_order(order_id, asked_username_at=now)

        # Тайм-аут ожидания.
        if now - first_seen >= self.cfg.username_wait_timeout:
            if not rec.get("timeout_reported"):
                logger.error(
                    "Заказ #%s: юзернейм не получен за %.0f ч — требуется участие продавца.",
                    order_id, self.cfg.username_wait_timeout / 3600,
                )
                self._notify_seller(
                    f"⚠️ Заказ #{order_id}: покупатель не прислал юзернейм за "
                    f"{self.cfg.username_wait_timeout / 3600:.0f} ч. Решите вручную."
                )
                self.state.upsert_order(order_id, timeout_reported=True)
        elif not rec:
            self.state.upsert_order(order_id, status=STATUS_WAITING_USERNAME, stars=stars)

    # ------------------------------------------------------------------ #
    # Шаг 3: отправка звёзд
    # ------------------------------------------------------------------ #
    def _deliver(self, order: Any, stars: int, username: str) -> None:
        order_id = str(order.id)
        rec = self.state.get_order(order_id) or self.state.upsert_order(
            order_id, status=STATUS_NEW, stars=stars,
        )

        if not self._balance_ok():
            logger.error(
                "Заказ #%s: недостаточно средств на балансе GAMEAU — выдача на паузе.",
                order_id,
            )
            self._notify_seller(f"⚠️ Низкий баланс GAMEAU — заказ #{order_id} не выдан.")
            return

        idem_key = rec.get("idempotency_key")
        if not idem_key:
            idem_key = str(uuid.uuid4())
            rec = self.state.upsert_order(order_id, idempotency_key=idem_key,
                                          username=username, stars=stars)

        # Подбираем пакет в каталоге: точное совпадение либо ближайший больший,
        # чтобы покупателю пришло не меньше оплаченного.
        try:
            package = self.gameau.find_stars_package(stars)
        except GameauError as exc:
            logger.error("Заказ #%s: не удалось получить каталог: %s", order_id, exc)
            return
        if package is None:
            logger.error("Заказ #%s: в каталоге GAMEAU нет пакета на %d+ звёзд.",
                         order_id, stars)
            self.state.upsert_order(order_id, status=STATUS_FAILED, username=username,
                                    error="нет пакета в каталоге")
            self._notify_seller(
                f"⚠️ Заказ #{order_id}: в каталоге GAMEAU нет пакета на {stars} звёзд."
            )
            return
        actual_stars = int(self.gameau.package_stars(package) or stars)
        price = float(package.get("price") or 0)
        max_charge = round(price * self.cfg.gameau_max_charge_multiplier, 2)
        if actual_stars != stars:
            logger.warning("Заказ #%s: точного пакета на %d нет, будет отправлено %d.",
                           order_id, stars, actual_stars)

        # Создание заказа в GAMEAU (или повтор с тем же ключом идемпотентности).
        gameau_order_id = rec.get("gameau_order_id")
        if rec.get("status") != STATUS_SENDING or not gameau_order_id:
            try:
                gameau_order = self._create_gameau_order(
                    order_id, username, actual_stars, max_charge, idem_key,
                )
            except InsufficientBalanceError:
                logger.error("Заказ #%s: недостаточно средств на балансе GAMEAU.", order_id)
                self.state.upsert_order(order_id, status=STATUS_FAILED, username=username,
                                        stars=actual_stars,
                                        error="недостаточно средств GAMEAU")
                self._notify_seller(
                    f"⚠️ GAMEAU: недостаточно средств для заказа #{order_id} "
                    f"({actual_stars} звёзд → @{username})."
                )
                return
            except GameauError as exc:
                # 4xx (кроме смены цены/конфликта) — скорее всего неисправимая
                # ошибка (например, неверный юзернейм). Остальное — повторим.
                attempts = int(rec.get("create_attempts") or 0) + 1
                fatal = exc.status_code is not None and 400 <= exc.status_code < 500 \
                    and exc.status_code not in (408, 409, 429)
                if fatal or attempts >= self.cfg.max_create_attempts:
                    self._handle_gameau_failure(order, order_id, username,
                                                actual_stars, exc)
                    return
                self.state.upsert_order(order_id, create_attempts=attempts,
                                        username=username, stars=actual_stars)
                logger.error(
                    "Заказ #%s: ошибка создания заказа GAMEAU (попытка %d/%d): %s",
                    order_id, attempts, self.cfg.max_create_attempts, exc,
                )
                return
            gameau_order_id = str(gameau_order.get("id") or gameau_order.get("orderId") or "")
            if not gameau_order_id:
                logger.error("Заказ #%s: GAMEAU не вернул ID заказа: %s",
                             order_id, gameau_order)
                return
            self.state.upsert_order(order_id, status=STATUS_SENDING,
                                    gameau_order_id=gameau_order_id, username=username,
                                    stars=actual_stars, price_usd=price)

        # Ожидание завершения.
        logger.info("Заказ #%s: ожидаю отправку %d звёзд → @%s (GAMEAU %s)…",
                    order_id, actual_stars, username, gameau_order_id)
        try:
            self.gameau.wait_for_completion(
                gameau_order_id,
                poll_interval=self.cfg.gameau_status_poll_interval,
                timeout=self.cfg.gameau_status_timeout,
            )
        except GameauTimeoutError as exc:
            # Заказ может всё ещё выполняться на стороне GAMEAU. Не считаем
            # это ошибкой: на следующем круге продолжим ждать по тому же
            # сохранённому ID и ключу идемпотентности.
            logger.warning(
                "Заказ #%s: истёк тайм-аут ожидания (%s). Продолжу ждать на "
                "следующем круге.", order_id, exc,
            )
            self.state.upsert_order(order_id, status=STATUS_SENDING,
                                    gameau_order_id=gameau_order_id,
                                    username=username, stars=actual_stars)
            return
        except GameauError as exc:
            self._handle_gameau_failure(order, order_id, username, actual_stars, exc)
            return

        self.state.upsert_order(order_id, status=STATUS_DONE, username=username,
                                stars=actual_stars, completed_at=time.time(),
                                gameau_order_id=gameau_order_id)
        logger.info("Заказ #%s: %d звёзд отправлено на @%s ✅",
                    order_id, actual_stars, username)
        self._notify_seller(
            f"✅ Заказ #{order_id}: {actual_stars} звёзд отправлено @{username}."
        )

        if self.cfg.reply_on_delivery:
            text = self.cfg.msg_stars_sent.format(username=username.lstrip("@"),
                                                  stars=actual_stars)
            self.funpay.send_message(order, text)

    def _create_gameau_order(self, order_id: str, username: str, stars: int,
                             max_charge: float, idem_key: str) -> dict[str, Any]:
        """Создание заказа с одним безопасным повтором при изменении цены."""
        try:
            return self.gameau.send_stars(username, stars, max_charge, idem_key)
        except PriceChangedError as exc:
            new_price = None
            if isinstance(exc.details, dict):
                new_price = exc.details.get("price") or exc.details.get("actualPrice")
            if new_price:
                max_charge = round(float(new_price) * self.cfg.gameau_max_charge_multiplier, 2)
                logger.warning("Заказ #%s: цена изменилась, повторяю с maxCharge=%.2f.",
                               order_id, max_charge)
                return self.gameau.send_stars(username, stars, max_charge, idem_key)
            raise

    def _handle_gameau_failure(self, order: Any, order_id: str, username: str,
                               stars: int, exc: GameauError) -> None:
        logger.error("Заказ #%s: выдача не удалась: %s", order_id, exc)
        refund_done = False
        if self.cfg.refund_on_failure:
            refund_done = self.funpay.refund(order)
        self.state.upsert_order(order_id, status=STATUS_FAILED, username=username,
                                stars=stars, error=str(exc), refund_done=refund_done)
        if self.cfg.reply_on_delivery and not refund_done:
            self.funpay.send_message(order, self.cfg.msg_error)
        self._notify_seller(
            f"⚠️ Заказ #{order_id}: выдача {stars} звёзд на @{username} не удалась: {exc}."
            + (" Оформлен возврат." if refund_done else "")
        )

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #
    def _balance_ok(self) -> bool:
        if not self.cfg.stop_on_low_balance:
            return True
        now = time.time()
        if self._balance is None or now - self._last_balance_check > 300:
            try:
                account = self.gameau.get_account()
                self._balance = float(account.get("balance") or 0)
                self._last_balance_check = now
                logger.debug("Баланс GAMEAU: %.2f %s", self._balance,
                             account.get("currency"))
            except GameauError as exc:
                logger.warning("Не удалось проверить баланс: %s", exc)
                return True  # не блокируем выдачу из-за сбоя проверки
        return self._balance >= self.cfg.min_gameau_balance

    def _try_relogin(self) -> None:
        logger.info("Пробую переподключиться к FunPay через 30 с…")
        time.sleep(30)
        try:
            self.funpay.login()
        except FunPayError as exc:
            logger.error("Переподключение не удалось: %s", exc)

    def _startup_report(self) -> None:
        try:
            account = self.gameau.get_account()
            logger.info(
                "GAMEAU: аккаунт %s, баланс %.2f %s, тариф %s.",
                account.get("username"), float(account.get("balance") or 0),
                account.get("currency"), account.get("plan"),
            )
            self._balance = float(account.get("balance") or 0)
            self._last_balance_check = time.time()
        except GameauError as exc:
            logger.warning("Не удалось получить данные аккаунта GAMEAU: %s", exc)
        try:
            packages = [
                (self.gameau.package_stars(i), i.get("price"))
                for i in self.gameau.get_stars_catalog()
            ]
            packages = [(s, p) for s, p in packages if s]
            if packages:
                logger.info("Каталог звёзд (%d пакетов): %s", len(packages),
                            ", ".join(f"{s}★=${p}" for s, p in sorted(packages)[:12]))
        except GameauError as exc:
            logger.warning("Не удалось получить каталог звёзд: %s", exc)

    def _notify_seller(self, text: str) -> None:
        """Уведомление продавцу в Telegram (если настроено)."""
        token = self.cfg.seller_notify_token
        chat_id = self.cfg.seller_notify_chat_id
        if not token or not chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.warning("Не удалось отправить уведомление продавцу: %s", exc)
