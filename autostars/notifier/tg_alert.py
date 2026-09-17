"""Модуль отправки уведомлений и алертов владельцу в Telegram с расчётом чистой прибыли (Whitebird USDT + TRON)."""

from __future__ import annotations

import logging
from typing import Optional
import httpx

logger = logging.getLogger("autostars.notifier")


class TelegramNotifier:
    """Telegram бот для уведомления владельца о сделках и алертах."""

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        whitebird_rate: float = 87.63,
        tron_energy_fee_rub: float = 0.0,
        timeout: float = 10.0,
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.whitebird_rate = float(whitebird_rate)
        self.tron_energy_fee_rub = float(tron_energy_fee_rub)
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        """Проверяет, настроены ли токен и chat_id."""
        return bool(self.bot_token and self.chat_id)

    def calculate_profit(
        self,
        order_price_rub: float,
        usdt_cost: float,
        tron_energy_rub: Optional[float] = None,
    ) -> float:
        """
        Калькуляция чистой прибыли:
        Выручка FunPay в RUB минус себестоимость покупки USDT на Whitebird
        с учетом комиссии сети TRON Energy.
        """
        tron_cost = self.tron_energy_fee_rub if tron_energy_rub is None else tron_energy_rub
        cost_rub = (usdt_cost * self.whitebird_rate) + tron_cost
        profit_rub = order_price_rub - cost_rub
        return round(profit_rub, 2)

    async def send_alert(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Отправляет текстовое сообщение в Telegram владельцу.
        Если бот не настроен, сообщение логируется.
        """
        if not self.is_configured:
            logger.info(f"[TG Alert (не настроен)]: {text}")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    logger.debug("Telegram alert успешно доставлен")
                    return True
                logger.error(
                    f"Ошибка Telegram API: {response.status_code} - {response.text}"
                )
                return False
        except Exception as exc:
            logger.error(f"Не удалось отправить уведомление в Telegram: {exc}")
            return False

    async def alert_order_completed(
        self,
        order_id: str,
        username: str,
        profit_rub: float = 572.97,
    ) -> bool:
        """Уведомление об успешном выполнении заказа с чистой прибылью."""
        msg = (
            f"💰 Заказ #{order_id} выполнен! +{profit_rub:.2f} RUB чистой прибыли "
            f"(Отправлено @{username})"
        )
        return await self.send_alert(msg)

    async def alert_low_balance(self, order_id: str) -> bool:
        """Критический алерт о низком балансе Gameau."""
        msg = (
            f"🚨 <b>ОШИБКА: НИЗКИЙ БАЛАНС GAMEAU!</b> Заказ #{order_id} остановлен. "
            f"Срочно пополните USDT через Whitebird!"
        )
        return await self.send_alert(msg)

    async def alert_price_exceeded(self, order_id: str, max_charge: float) -> bool:
        """Предупреждение о превышении maxCharge."""
        msg = (
            f"⚠️ <b>ВНИМАНИЕ:</b> Превышен лимит стоимости maxCharge ({max_charge:.2f} USDT) "
            f"для заказа #{order_id}! Покупка отменена во избежание убытка."
        )
        return await self.send_alert(msg)
