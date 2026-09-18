"""Обёртка над библиотекой FunPayAPI.

Реализует только нужные для автовыдачи операции:
    * вход по golden_key и периодическое обновление сессии;
    * список заказов, ожидающих выполнения (статус «оплачен»);
    * чтение чата заказа (поиск юзернейма);
    * отправка сообщений покупателю;
    * оформление возврата.

Чат заказа на FunPay — это личный чат с покупателем, поэтому в качестве
`chat_id` используется `buyer_id` заказа.
"""

from __future__ import annotations

import time
from typing import Any

import FunPayAPI
from FunPayAPI.common import exceptions as fp_exceptions

from .config import Config
from .logger_setup import get_logger

logger = get_logger("funpay")


class FunPayError(Exception):
    """Ошибка работы с FunPay."""


class FunPayClient:
    """Тонкая обёртка над :class:`FunPayAPI.account.Account`."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.account: FunPayAPI.Account | None = None
        self._last_session_update = 0.0

    # ------------------------------------------------------------------ #
    # Сессия
    # ------------------------------------------------------------------ #
    def login(self) -> None:
        """Создаёт/пересоздаёт сессию по golden_key."""
        logger.info("Вхожу в аккаунт FunPay…")
        account = FunPayAPI.Account(
            golden_key=self.cfg.funpay_golden_key,
            user_agent=self.cfg.funpay_user_agent,
        )
        try:
            account.get()
        except fp_exceptions.UnauthorizedError as exc:
            raise FunPayError(
                "FunPay не принял golden_key. Проверьте значение FUNPAY_GOLDEN_KEY "
                "(Профиль → Безопасность → ключ доступа)."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise FunPayError(f"Не удалось войти в FunPay: {exc}") from exc
        self.account = account
        self._last_session_update = time.time()
        logger.info(
            "Вход выполнен: продавец %s (ID %s). Активные продажи: %s.",
            account.username, account.id, account.active_sales,
        )

    def ensure_session(self) -> None:
        """Обновляет сессию, если она старше `session_refresh_interval`.

        FunPayAPI требует обновлять PHPSESSID раз в 40–60 минут.
        """
        assert self.account is not None, "Сначала вызовите login()"
        if time.time() - self._last_session_update < self.cfg.session_refresh_interval:
            return
        logger.info("Обновляю сессию FunPay…")
        try:
            self.account.get(update_phpsessid=True)
            self._last_session_update = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось обновить сессию: %s. Пробую перелогиниться.", exc)
            self.login()

    # ------------------------------------------------------------------ #
    # Заказы
    # ------------------------------------------------------------------ #
    def pending_orders(self) -> list[Any]:
        """Заказы в статусе «оплачен и ожидает выполнения»."""
        assert self.account is not None, "Сначала вызовите login()"
        try:
            _, orders = self.account.get_sells(state="paid")
        except fp_exceptions.UnauthorizedError as exc:
            raise FunPayError("Сессия FunPay протухла (401).") from exc
        except Exception as exc:  # noqa: BLE001
            raise FunPayError(f"Не удалось получить список заказов: {exc}") from exc
        return orders

    def order_messages(self, order: Any) -> list[Any]:
        """История чата заказа (до 100 последних сообщений)."""
        assert self.account is not None
        try:
            return self.account.get_chat_history(
                order.buyer_id, interlocutor_username=order.buyer_username
            )
        except Exception as exc:  # noqa: BLE001
            raise FunPayError(f"Не удалось получить чат заказа {order.id}: {exc}") from exc

    def send_message(self, order: Any, text: str) -> bool:
        """Отправляет сообщение покупателю в чат заказа."""
        assert self.account is not None
        try:
            self.account.send_message(order.buyer_id, text, chat_name=order.buyer_username)
            logger.info("Сообщение покупателю %s отправлено (заказ #%s).",
                        order.buyer_username, order.id)
            return True
        except fp_exceptions.MessageNotDeliveredError as exc:
            logger.error("Сообщение не доставлено (заказ #%s): %s", order.id, exc)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка отправки сообщения (заказ #%s): %s", order.id, exc)
            return False

    def refund(self, order: Any) -> bool:
        """Возврат средств по заказу."""
        assert self.account is not None
        try:
            self.account.refund(order.id)
            logger.info("Оформлен возврат по заказу #%s.", order.id)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось оформить возврат по заказу #%s: %s", order.id, exc)
            return False
