"""Клиент B2B API Gameau с защитой по Idempotency Key, maxCharge и экспоненциальным backoff."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger("GameauClient")


class GameauClient:
    """Асинхронный клиент для взаимодействия с Gameau B2B API."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://gameau.us/api",
        max_retries: int = 3,
        timeout: float = 15.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.max_retries = max_retries
        self.timeout = timeout

    async def buy_telegram_stars(
        self,
        username: str,
        quantity: int,
        order_id: str,
        max_charge_usdt: float = 9.50,
    ) -> Dict[str, Any]:
        """
        Отправляет запрос на покупку Stars с защитой по Idempotency Key и maxCharge.
        Использует экспоненциальный backoff при временных сетевых ошибках и 429/503.
        """
        # Идемпотентный ключ на основе ID заказа FunPay
        idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))

        clean_username = username.replace("@", "").strip()
        payload = {
            "username": clean_username,
            "quantity": int(quantity),
            "maxCharge": float(max_charge_usdt),
        }

        request_headers = {
            **self.headers,
            "Idempotency-Key": idempotency_key,
        }

        url = f"{self.base_url}/telegramStars"

        last_error = "NETWORK_TIMEOUT"
        for attempt in range(self.max_retries):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                try:
                    response = await client.post(
                        url,
                        json=payload,
                        headers=request_headers,
                    )

                    # Обработка статусов
                    if response.status_code == 200:
                        try:
                            data = response.json()
                        except Exception:
                            data = {"raw": response.text}
                        logger.info(
                            f"[ORDER {order_id}] Успешно отправлено {quantity} Stars для @{clean_username}"
                        )
                        return {
                            "success": True,
                            "data": data,
                            "idempotency_key": idempotency_key,
                        }

                    elif response.status_code == 400 and "maxCharge" in response.text:
                        logger.error(
                            f"[ORDER {order_id}] Превышен лимит стоимости maxCharge!"
                        )
                        return {"success": False, "error": "PRICE_EXCEEDED"}

                    elif response.status_code == 402:
                        logger.critical(
                            f"[ORDER {order_id}] Недостаточно USDT на балансе Gameau!"
                        )
                        return {"success": False, "error": "LOW_BALANCE"}

                    elif response.status_code in (429, 502, 503, 504):
                        logger.warning(
                            f"[ORDER {order_id}] Временная ошибка Gameau ({response.status_code}). "
                            f"Попытка {attempt + 1}/{self.max_retries}"
                        )
                        last_error = f"API_ERROR_{response.status_code}"
                        if attempt < self.max_retries - 1:
                            await asyncio.sleep(2**attempt)
                            continue
                        return {"success": False, "error": last_error}

                    else:
                        logger.error(
                            f"[ORDER {order_id}] Ошибка API Gameau: {response.status_code} - {response.text}"
                        )
                        return {
                            "success": False,
                            "error": f"API_ERROR_{response.status_code}",
                        }

                except httpx.RequestError as exc:
                    logger.warning(
                        f"[ORDER {order_id}] Сетевой сбой при вызове Gameau (попытка {attempt + 1}/{self.max_retries}): {exc}"
                    )
                    last_error = "NETWORK_TIMEOUT"
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2**attempt)
                        continue
                    return {"success": False, "error": "NETWORK_TIMEOUT"}

        return {"success": False, "error": last_error}

    async def get_account(self) -> Dict[str, Any]:
        """Получает информацию об аккаунте Gameau (баланс USDT, статус)."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/account",
                    headers=self.headers,
                )
                if response.status_code == 200:
                    return response.json()
                logger.error(f"Ошибка получения аккаунта: {response.status_code} - {response.text}")
                return {"error": f"HTTP_{response.status_code}", "status_code": response.status_code}
            except Exception as exc:
                logger.error(f"Сетевая ошибка при запросе аккаунта Gameau: {exc}")
                return {"error": str(exc)}

    async def get_catalog(self) -> list[Dict[str, Any]]:
        """Получает каталог пакетов Telegram Stars с актуальными ценами."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/catalog",
                    params={"type": "telegramStars"},
                    headers=self.headers,
                )
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, list):
                        return data
                    return data.get("items") or (data.get("catalog") or {}).get("items") or []
                return []
            except Exception as exc:
                logger.error(f"Ошибка получения каталога Gameau: {exc}")
                return []
