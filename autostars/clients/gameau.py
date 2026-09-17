"""Клиент B2B API Gameau (https://gameau.us/api-docs.html) с поддержкой каталога, Idempotency-Key и maxCharge."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger("GameauClient")

SUCCESS_STATUSES = {"success", "completed", "complete", "delivered", "paid"}
FAILED_STATUSES = {"failed", "refunded", "cancelled", "canceled", "rejected", "error"}


class GameauClient:
    """Асинхронный клиент для взаимодействия с Gameau B2B API v1."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://gameau.us/api/v1",
        max_retries: int = 3,
        timeout: float = 20.0,
    ):
        self.api_key = api_key
        # Нормализация URL: если передан /api без /v1, проверяем совместимость
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.max_retries = max_retries
        self.timeout = timeout
        self._catalog_cache: Optional[tuple[float, list[dict[str, Any]]]] = None

    def _get_idempotency_key(self, order_id: str) -> str:
        """Генерирует детерминированный UUID v5 для заказа FunPay."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))

    async def get_catalog(
        self,
        item_type: str = "telegramStars",
        limit: int = 100,
        ttl: float = 60.0,
    ) -> List[Dict[str, Any]]:
        """
        Получает каталог товаров с Gameau API (GET /catalog?type=telegramStars).
        Результаты кешируются на ttl секунд.
        """
        import time

        now = time.time()
        if self._catalog_cache and (now - self._catalog_cache[0]) < ttl:
            return self._catalog_cache[1]

        url = f"{self.base_url}/catalog"
        params = {"type": item_type, "limit": limit}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(url, params=params, headers=self.headers)
                if response.status_code == 404 and "/v1" in self.base_url:
                    # Попытка фоллбэка без /v1
                    fallback_url = self.base_url.replace("/v1", "") + "/catalog"
                    response = await client.get(fallback_url, params=params, headers=self.headers)

                if response.status_code == 200:
                    data = response.json()
                    items = []
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        cat = data.get("catalog") or {}
                        items = cat.get("items") or data.get("items") or []

                    self._catalog_cache = (now, items)
                    logger.debug(f"Загружен каталог Gameau: {len(items)} позиций типа {item_type}")
                    return items
                else:
                    logger.warning(f"Ошибка загрузки каталога Gameau: {response.status_code} - {response.text}")
                    return []
            except Exception as exc:
                logger.error(f"Сетевой сбой при запросе каталога: {exc}")
                return []

    @staticmethod
    def extract_item_stars(item: Dict[str, Any]) -> Optional[int]:
        """Извлекает количество звёзд из элемента каталога."""
        order_info = item.get("order") or {}
        body = order_info.get("body") or {}
        for key in ("quantity", "stars", "amount"):
            val = body.get(key) or item.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)

        name = item.get("name") or item.get("title") or ""
        match = re.search(r"(\d[\d\s]*)", str(name))
        if match:
            try:
                return int(match.group(1).replace(" ", ""))
            except ValueError:
                pass
        return None

    async def find_stars_package(self, stars: int) -> Optional[Dict[str, Any]]:
        """
        Ищет в каталоге пакет на `stars` звёзд.
        Если точного совпадения нет — выбирает ближайший больший пакет,
        чтобы покупатель получил не меньше оплаченного.
        """
        items = await self.get_catalog(item_type="telegramStars")
        orderable_items = [i for i in items if i.get("orderable", True)]

        # 1. Точное совпадение
        exact = [i for i in orderable_items if self.extract_item_stars(i) == stars]
        if exact:
            return min(exact, key=lambda x: float(x.get("price") or 10**9))

        # 2. Ближайший больший пакет
        greater = [
            i for i in orderable_items
            if (self.extract_item_stars(i) or 0) >= stars
        ]
        if greater:
            greater.sort(key=lambda x: (self.extract_item_stars(x) or 0, float(x.get("price") or 10**9)))
            chosen = greater[0]
            logger.info(
                f"Точного пакета на {stars} звёзд нет в каталоге, выбран ближайший: "
                f"{self.extract_item_stars(chosen)} звёзд за {chosen.get('price')} USD"
            )
            return chosen

        return None

    async def buy_telegram_stars(
        self,
        username: str,
        quantity: int,
        order_id: str,
        max_charge_usdt: float = 9.50,
        wait_completion: bool = False,
        hide_sender: bool = False,
    ) -> Dict[str, Any]:
        """
        Отправляет запрос на покупку Stars с защитой по Idempotency Key и maxCharge.
        Использует экспоненциальный backoff при временных ошибках 429/503.
        """
        idempotency_key = self._get_idempotency_key(order_id)
        clean_username = username.replace("@", "").strip()

        # Формируем тело запроса согласно документации Gameau REST API v1
        payload = {
            "telegram_username": clean_username,
            "username": clean_username,
            "quantity": int(quantity),
            "maxCharge": float(max_charge_usdt),
        }
        if hide_sender:
            payload["hide_sender"] = 1

        request_headers = {
            **self.headers,
            "Idempotency-Key": idempotency_key,
        }

        # Согласно документации Gameau v1, адрес: POST /api/v1/orders/telegramStars
        # Также поддерживается /telegramStars для совместимости
        order_urls = [
            f"{self.base_url}/orders/telegramStars",
            f"{self.base_url}/telegramStars",
        ]
        if "/v1" not in self.base_url:
            order_urls.append(f"{self.base_url}/v1/orders/telegramStars")

        last_error = "NETWORK_TIMEOUT"

        for attempt in range(self.max_retries):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for url in order_urls:
                    try:
                        response = await client.post(
                            url,
                            json=payload,
                            headers=request_headers,
                        )

                        if response.status_code == 404:
                            # Пробуем следующий альтернативный URL
                            continue

                        # Успешный ответ
                        if response.status_code in (200, 201):
                            data = response.json()
                            order_obj = data.get("order") or data
                            created_order_id = str(order_obj.get("id") or order_obj.get("orderId") or "")
                            logger.info(
                                f"[ORDER {order_id}] Успешно создан заказ в Gameau: {created_order_id} "
                                f"({quantity} Stars для @{clean_username})"
                            )

                            if wait_completion and created_order_id:
                                final_order = await self.wait_for_completion(created_order_id)
                                return {
                                    "success": True,
                                    "data": final_order,
                                    "idempotency_key": idempotency_key,
                                }

                            return {
                                "success": True,
                                "data": order_obj,
                                "idempotency_key": idempotency_key,
                            }

                        # Превышение maxCharge или смена цены
                        elif response.status_code in (400, 409) and (
                            "maxCharge" in response.text
                            or "PRICE_CHANGED" in response.text
                            or "price" in response.text.lower()
                        ):
                            logger.error(f"[ORDER {order_id}] Превышен лимит стоимости maxCharge!")
                            return {"success": False, "error": "PRICE_EXCEEDED"}

                        # Недостаточно средств
                        elif response.status_code == 402 or (
                            response.status_code == 400 and "INSUFFICIENT_BALANCE" in response.text
                        ):
                            logger.critical(f"[ORDER {order_id}] Недостаточно USDT на балансе Gameau!")
                            return {"success": False, "error": "LOW_BALANCE"}

                        # Временные ошибки (429 Too Many Requests, 502/503/504)
                        elif response.status_code in (429, 502, 503, 504):
                            logger.warning(
                                f"[ORDER {order_id}] Временная ошибка Gameau ({response.status_code}). "
                                f"Попытка {attempt + 1}/{self.max_retries}"
                            )
                            last_error = f"API_ERROR_{response.status_code}"
                            break  # Переходим к следующему циклу retry с backoff

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
                            f"[ORDER {order_id}] Сетевой сбой Gameau ({url}): {exc}"
                        )
                        last_error = "NETWORK_TIMEOUT"
                        break

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2**attempt)

        return {"success": False, "error": last_error}

    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Получает актуальный статус заказа (GET /orders/status/{order_id})."""
        status_urls = [
            f"{self.base_url}/orders/status/{order_id}",
            f"{self.base_url}/status/{order_id}",
        ]
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for url in status_urls:
                try:
                    response = await client.get(url, headers=self.headers)
                    if response.status_code == 200:
                        data = response.json()
                        return data.get("order") or data
                except Exception as exc:
                    logger.debug(f"Ошибка запроса статуса по {url}: {exc}")
        return {"status": "unknown"}

    async def wait_for_completion(
        self,
        order_id: str,
        poll_interval: float = 3.0,
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """
        Ожидает терминального статуса выполнения заказа (success / completed).
        """
        import time

        deadline = time.time() + timeout
        last_status = None

        while time.time() < deadline:
            order_data = await self.get_order_status(order_id)
            status = str(order_data.get("status") or "").lower()

            if status != last_status:
                logger.info(f"Заказ Gameau {order_id}: статус '{status}'")
                last_status = status

            if status in SUCCESS_STATUSES:
                return order_data
            if status in FAILED_STATUSES:
                raise RuntimeError(f"Заказ Gameau {order_id} завершился со статусом '{status}'")

            await asyncio.sleep(poll_interval)

        logger.warning(f"Истёк тайм-аут ожидания заказа Gameau {order_id}")
        return {"id": order_id, "status": "timeout"}

    async def get_account(self) -> Dict[str, Any]:
        """Получает информацию об аккаунте Gameau (баланс USDT, статус)."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(
                    f"{self.base_url}/account",
                    headers=self.headers,
                )
                if response.status_code == 200:
                    data = response.json()
                    return data.get("account") or data
                return {"error": f"HTTP_{response.status_code}", "status_code": response.status_code}
            except Exception as exc:
                return {"error": str(exc)}
