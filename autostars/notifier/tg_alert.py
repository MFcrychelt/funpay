"""Модуль отправки уведомлений и алертов владельцу в Telegram с расчётом чистой прибыли (Вариант 1: Анонимный 110 ₽ vs Вариант 2: Whitebird 87.63 ₽)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("autostars.notifier")


class TelegramNotifier:
    """Telegram бот для уведомления владельца о сделках, балансе и финансовых метриках."""

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        whitebird_rate: float = 87.63,
        rate_variant_1: float = 110.00,
        rate_variant_2: float = 87.63,
        active_variant: int = 2,
        tron_energy_fee_rub: float = 0.0,
        timeout: float = 10.0,
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.rate_variant_1 = float(rate_variant_1)
        self.rate_variant_2 = float(whitebird_rate if whitebird_rate else rate_variant_2)
        self.whitebird_rate = self.rate_variant_2
        self.active_variant = active_variant
        self.tron_energy_fee_rub = float(tron_energy_fee_rub)
        self.timeout = timeout
        # Персистентный HTTP-клиент (пул соединений) — алерты уходят быстрее
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @property
    def is_configured(self) -> bool:
        """Проверяет, настроены ли токен и chat_id."""
        return bool(self.bot_token and self.chat_id)

    @property
    def current_rate(self) -> float:
        """Текущий активный курс обмена в зависимости от выбранного варианта."""
        return self.rate_variant_1 if self.active_variant == 1 else self.rate_variant_2

    def calculate_profit(
        self,
        order_price_rub: float,
        usdt_cost: float,
        rate: float | None = None,
        tron_energy_rub: float | None = None,
    ) -> float:
        """
        Калькуляция чистой прибыли:
        Выручка FunPay в RUB минус себестоимость покупки USDT на бирже/обменнике
        с учетом комиссии сети TRON Energy.
        """
        effective_rate = self.current_rate if rate is None else float(rate)
        tron_cost = self.tron_energy_fee_rub if tron_energy_rub is None else float(tron_energy_rub)
        cost_rub = (usdt_cost * effective_rate) + tron_cost
        profit_rub = order_price_rub - cost_rub
        return round(profit_rub, 2)

    def get_profit_summary(self, order_price_rub: float, usdt_cost: float) -> dict[str, Any]:
        """Возвращает детальную калькуляцию прибыли для обоих вариантов пополнения."""
        profit_v1 = self.calculate_profit(order_price_rub, usdt_cost, rate=self.rate_variant_1)
        profit_v2 = self.calculate_profit(order_price_rub, usdt_cost, rate=self.rate_variant_2)
        return {
            "variant_1": {
                "name": "Анонимный обмен (P2P)",
                "rate": self.rate_variant_1,
                "cost_rub": round(usdt_cost * self.rate_variant_1, 2),
                "profit_rub": profit_v1,
            },
            "variant_2": {
                "name": "Беларусь Whitebird (без P2P)",
                "rate": self.rate_variant_2,
                "cost_rub": round(usdt_cost * self.rate_variant_2, 2),
                "profit_rub": profit_v2,
            },
            "active_variant": self.active_variant,
            "active_profit_rub": profit_v1 if self.active_variant == 1 else profit_v2,
        }

    async def send_alert(self, text: str, parse_mode: str = "HTML") -> bool:
        """Отправляет текстовое сообщение в Telegram владельцу."""
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
            client = await self._http()
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                logger.debug("Telegram alert успешно доставлен")
                return True
            logger.error(f"Ошибка Telegram API: {response.status_code} - {response.text}")
            return False
        except Exception as exc:
            logger.error(f"Не удалось отправить уведомление в Telegram: {exc}")
            return False

    async def send_stats_report(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        Отправляет развёрнутый статистический отчёт (1ч/2ч/3ч/4ч/6ч/день).
        Делит длинный текст на части (лимит Telegram — 4096 символов).
        """
        if not text.strip():
            return True
        chunks: list[str] = []
        current = ""
        for line in text.splitlines():
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > 4000 and current:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        ok = True
        for i, chunk in enumerate(chunks, 1):
            suffix = f"\n\n— часть {i}/{len(chunks)}" if len(chunks) > 1 else ""
            ok = await self.send_alert(chunk + suffix, parse_mode=parse_mode) and ok
        return ok

    async def alert_order_completed(
        self,
        order_id: str,
        username: str,
        profit_rub: float = 572.97,
        order_price_rub: float | None = None,
        usdt_cost: float = 9.10,
    ) -> bool:
        """
        Уведомление об успешном выполнении заказа с калькуляцией для обоих вариантов.
        """
        if order_price_rub and order_price_rub > 0:
            summary = self.get_profit_summary(order_price_rub, usdt_cost)
            msg = (
                f"💰 <b>Заказ #{order_id} выполнен!</b> (+{summary['active_profit_rub']:.2f} RUB чистой прибыли)\n"
                f"👤 Получатель: @{username}\n\n"
                f"📊 <b>Сравнение доходности по вариантам обмена:</b>\n"
                f"• <b>Вариант 2 (Whitebird {self.rate_variant_2:.2f} ₽):</b> +{summary['variant_2']['profit_rub']:.2f} RUB\n"
                f"• <b>Вариант 1 (Анонимный {self.rate_variant_1:.2f} ₽):</b> +{summary['variant_1']['profit_rub']:.2f} RUB"
            )
        else:
            msg = f"💰 Заказ #{order_id} выполнен! +{profit_rub:.2f} RUB чистой прибыли (Отправлено @{username})"

        return await self.send_alert(msg)

    async def alert_low_balance(self, order_id: str) -> bool:
        """Критический алерт о низком балансе Gameau с инструкциями по обоим вариантам."""
        msg = (
            f"🚨 <b>ОШИБКА: НИЗКИЙ БАЛАНС GAMEAU!</b>\n"
            f"Заказ #{order_id} остановлен. Срочно пополните кошелёк <b>USDT TRC-20</b> на gameau.us!\n\n"
            f"💳 <b>Способы пополнения USDT TRC-20:</b>\n\n"
            f"1️⃣ <b>Вариант 1 (Анонимный обмен, курс ~{self.rate_variant_1:.2f} ₽):</b>\n"
            f"• Покупка через P2P (Telegram Wallet, Bybit, BestChange, наличные).\n"
            f"• Отправка напрямую на TRC-20 адрес из кабинета Gameau.\n"
            f"• Себестоимость 1 000 Stars: ~{9.10 * self.rate_variant_1:.2f} RUB.\n\n"
            f"2️⃣ <b>Вариант 2 (Беларусь Whitebird без P2P, курс ~{self.rate_variant_2:.2f} ₽):</b>\n"
            f"• Легальная покупка на <b>whitebird.io</b> с банковской карты (BYN/RUB).\n"
            f"• Нет риска блокировок 115-ФЗ, прямой вывод на TRC-20 адрес Gameau.\n"
            f"• Себестоимость 1 000 Stars: ~{9.10 * self.rate_variant_2:.2f} RUB (+173.37 ₽ экономии на каждом заказе!)."
        )
        return await self.send_alert(msg)

    async def alert_price_exceeded(self, order_id: str, max_charge: float) -> bool:
        """Предупреждение о превышении maxCharge."""
        msg = (
            f"⚠️ <b>ВНИМАНИЕ:</b> Превышен лимит стоимости maxCharge ({max_charge:.2f} USDT) "
            f"для заказа #{order_id}! Покупка отменена во избежание списания баланса в минус."
        )
        return await self.send_alert(msg)
