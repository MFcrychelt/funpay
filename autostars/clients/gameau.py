"""Клиент B2B API GAMEAU (https://gameau.us/api-docs.html).

Каталог, заказы telegramStars, статусы, баланс. Ключевые свойства:
  • `Idempotency-Key` = детерминированный UUID v5 от ID заказа FunPay —
    ретрай не создаёт вторую покупку;
  • `maxCharge` — лимит списания на заказ (защита от «списали весь баланс»);
  • каталог кешируется (по типу товара) и используется для расчёта
    фактической себестоимости вместо захардкоженной «9.10 USDT за 1000⭐»;
  • 429/502/503/504 повторяются с backoff и учётом `Retry-After`;
  • персистентный httpx.AsyncClient (пул соединений, без TLS-хендшейка на запрос);
  • секреты и простыни HTML/JSON в лог не пишутся (см. `autostars.security`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
import uuid
from typing import Any

import httpx

from ..security import redact, truncate

logger = logging.getLogger("GameauClient")

SUCCESS_STATUSES = {"success", "completed", "complete", "delivered", "paid"}
FAILED_STATUSES = {"failed", "refunded", "cancelled", "canceled", "rejected", "error"}
RETRYABLE_STATUS_CODES = (429, 502, 503, 504)

# себестоимость «по умолчанию», когда каталог недоступен (1000 звёзд)
FALLBACK_USDT_PER_1000_STARS = 9.10


class GameauError(RuntimeError):
    """Ошибка GAMEAU с текстом, понятным человеку, а не только стектрейсу."""

    def __init__(self, message: str, *, code: str = "", status_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def parse_balance_usdt(account: dict[str, Any] | None) -> float | None:
    """Достаёт баланс USDT из ответа /account (нормализуя варианты ключей)."""
    if not account:
        return None
    for key in ("balance", "balanceUsdt", "balance_usdt", "amount", "usdt"):
        value = account.get(key)
        if value is None:
            continue
        try:
            return float(str(value).replace("\u00a0", "").replace(" ", ""))
        except (TypeError, ValueError):
            continue
    return None


class GameauClient:
    """Асинхронный клиент GAMEAU REST API v1."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://gameau.us/api/v1",
        max_retries: int = 3,
        timeout: float = 20.0,
        cache_ttl: float = 60.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AutoStars/2.4",
        }
        self.max_retries = max(1, int(max_retries))
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._client: httpx.AsyncClient | None = None
        self._secrets: tuple[str, ...] = tuple(s for s in (self.api_key,) if s)

    # ------------------------------------------------------------------ #
    # транспорт
    # ------------------------------------------------------------------ #
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._client = None

    def _idempotency_key(self, order_id: str) -> str:
        """UUID v5 от ID заказа FunPay: одинаковый для одинакового заказа."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))

    def _candidate_urls(self, path: str) -> list[str]:
        """Пробуем и v1-префикс, и «голый» путь — API мигрировал между схемами."""
        urls = [f"{self.base_url}/{path.lstrip('/')}"]
        if "/v1" not in self.base_url:
            urls.append(f"{self.base_url}/v1/{path.lstrip('/')}")
        else:
            urls.append(f"{self.base_url.replace('/v1', '')}/{path.lstrip('/')}")
        # нормализуем возможные двойные слеши
        return [re.sub(r"(?<!:)//+", "/", url) for url in dict.fromkeys(urls)]

    @staticmethod
    def _retry_after(response: httpx.Response, default: float) -> float:
        value = response.headers.get("Retry-After")
        if not value:
            return default
        try:
            return max(1.0, min(float(value), 120.0))
        except ValueError:
            return default

    # ------------------------------------------------------------------ #
    # каталог
    # ------------------------------------------------------------------ #
    async def get_catalog(
        self,
        item_type: str = "telegramStars",
        limit: int = 100,
        ttl: float | None = None,
    ) -> list[dict[str, Any]]:
        """Каталог товаров (GET /catalog?type=...), с кешем на `ttl` секунд."""
        ttl = self.cache_ttl if ttl is None else ttl
        now = time.time()
        cached = self._catalog_cache.get(item_type)
        if cached and (now - cached[0]) < ttl:
            return cached[1]

        params = {"type": item_type, "limit": limit}
        client = await self._http()
        last_status = 0
        for url in self._candidate_urls("catalog"):
            try:
                response = await client.get(url, params=params, headers=self.headers)
            except httpx.RequestError as exc:
                logger.error(f"Сетевой сбой при запросе каталога ({url}): {exc}")
                return cached[1] if cached else []
            if response.status_code == 200:
                items = self._extract_catalog_items(response.json())
                self._catalog_cache[item_type] = (now, items)
                logger.debug(f"Каталог GAMEAU: {len(items)} позиций типа {item_type}")
                return items
            last_status = response.status_code
            continue

        logger.warning(
            f"Не удалось загрузить каталог GAMEAU (HTTP {last_status or 'нет ответа'}) — "
            f"будет использована оценочная себестоимость"
        )
        return cached[1] if cached else []

    @staticmethod
    def _extract_catalog_items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            cat = data.get("catalog") or {}
            if isinstance(cat, dict):
                return cat.get("items") or []
            return data.get("items") or []
        return []

    @staticmethod
    def extract_item_stars(item: dict[str, Any]) -> int | None:
        """Сколько звёзд в пакете: явные поля, иначе — цифры из названия."""
        order_info = item.get("order") or {}
        body = order_info.get("body") or {} if isinstance(order_info, dict) else {}
        for key in ("quantity", "stars", "amount"):
            val = body.get(key) if isinstance(body, dict) else None
            val = val if val is not None else item.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)

        name = str(item.get("name") or item.get("title") or "")
        match = re.search(r"(\d[\d\s\u00a0]*)", name)
        if match:
            with contextlib.suppress(ValueError):
                return int(match.group(1).replace(" ", "").replace("\u00a0", ""))
        return None

    @staticmethod
    def item_price_usdt(item: dict[str, Any]) -> float | None:
        """Цена пакета в USDT (USD) из каталога."""
        for key in ("price", "cost", "priceUsdt", "price_usdt"):
            value = item.get(key)
            if value is None:
                continue
            try:
                price = float(str(value).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None

    async def find_stars_package(self, stars: int) -> dict[str, Any] | None:
        """Пакет на `stars` звёзд; если точного нет — ближайший больший.

        «Ближайший больший» принципиален: покупатель должен получить не меньше
        оплаченного, иначе это спор на FunPay и потерянная репутация.
        """
        items = await self.get_catalog(item_type="telegramStars")
        orderable = [i for i in items if i.get("orderable", True)]

        exact = [i for i in orderable if self.extract_item_stars(i) == stars]
        if exact:
            return min(exact, key=lambda x: self.item_price_usdt(x) or 10**9)

        greater = [i for i in orderable if (self.extract_item_stars(i) or 0) >= stars]
        if greater:
            greater.sort(key=lambda x: (self.extract_item_stars(x) or 0, self.item_price_usdt(x) or 10**9))
            chosen = greater[0]
            logger.info(
                f"Точного пакета на {stars}⭐ нет, выбран ближайший: "
                f"{self.extract_item_stars(chosen)}⭐ за {self.item_price_usdt(chosen)} USDT"
            )
            return chosen
        return None

    async def price_for_stars(self, stars: int, fallback_usdt_per_1000: float = FALLBACK_USDT_PER_1000_STARS) -> float:
        """Цена N звёзд в USDT: по каталогу, при недоступности — пропорционально эталону."""
        package = await self.find_stars_package(stars)
        if package is not None:
            price = self.item_price_usdt(package)
            if price:
                package_stars = self.extract_item_stars(package) or stars
                # пересчёт на фактическое количество звёзд, если пакет больше заказа
                return round(price * (stars / package_stars) if package_stars else price, 4)
        return round(stars / 1000.0 * fallback_usdt_per_1000, 4)

    # ------------------------------------------------------------------ #
    # заказы
    # ------------------------------------------------------------------ #
    async def buy_telegram_stars(
        self,
        username: str,
        quantity: int,
        order_id: str,
        max_charge_usdt: float = 9.50,
        wait_completion: bool = False,
        hide_sender: bool = False,
    ) -> dict[str, Any]:
        """Создаёт заказ telegramStars (идемпотентно по Idempotency-Key)."""
        idempotency_key = self._idempotency_key(order_id)
        clean_username = str(username).replace("@", "").strip()
        if not clean_username:
            return {"success": False, "error": "EMPTY_USERNAME"}

        payload = {
            "telegram_username": clean_username,
            "username": clean_username,
            "quantity": int(quantity),
            "maxCharge": round(float(max_charge_usdt), 2),
        }
        if hide_sender:
            payload["hide_sender"] = 1

        request_headers = {**self.headers, "Idempotency-Key": idempotency_key}
        urls = self._candidate_urls("orders/telegramStars") + self._candidate_urls("telegramStars")
        urls = list(dict.fromkeys(urls))

        last_error = "NETWORK_TIMEOUT"
        last_detail = ""
        client = await self._http()

        for attempt in range(self.max_retries):
            retry_wait: float | None = None
            for url in urls:
                try:
                    response = await client.post(url, json=payload, headers=request_headers)
                except httpx.RequestError as exc:
                    last_error, last_detail = "NETWORK_TIMEOUT", str(exc)
                    retry_wait = 2**attempt + random.uniform(0, 0.5)
                    logger.warning(f"[ORDER {order_id}] Сетевой сбой GAMEAU ({url}): {exc}")
                    break

                if response.status_code == 404:
                    continue  # пробуем следующий вариант адреса

                if response.status_code in (200, 201):
                    data = self._safe_json(response)
                    order_obj = (data.get("order") if isinstance(data, dict) else None) or data or {}
                    created_order_id = str(
                        order_obj.get("id") or order_obj.get("orderId") or order_obj.get("order_id") or ""
                    )
                    logger.info(
                        f"[ORDER {order_id}] Заказ создан в GAMEAU: {created_order_id or '?'} "
                        f"({quantity}⭐ для @{clean_username}, maxCharge {payload['maxCharge']} USDT)"
                    )
                    if wait_completion and created_order_id:
                        final_order = await self.wait_for_completion(created_order_id)
                        return {"success": True, "data": final_order, "idempotency_key": idempotency_key}
                    return {"success": True, "data": order_obj, "idempotency_key": idempotency_key}

                body = redact(truncate(response.text, 300), self._secrets)

                if response.status_code in (400, 409) and (
                    "maxCharge" in body or "PRICE_CHANGED" in body or "price" in body.lower()
                ):
                    logger.error(f"[ORDER {order_id}] Цена выросла/превышен maxCharge: {body}")
                    return {"success": False, "error": "PRICE_EXCEEDED", "detail": body}

                if response.status_code == 402 or (response.status_code == 400 and "INSUFFICIENT_BALANCE" in body):
                    logger.critical(f"[ORDER {order_id}] На балансе GAMEAU не хватает USDT!")
                    return {"success": False, "error": "LOW_BALANCE", "detail": body}

                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_error, last_detail = f"API_ERROR_{response.status_code}", body
                    retry_wait = self._retry_after(response, 2**attempt + random.uniform(0, 0.5))
                    logger.warning(
                        f"[ORDER {order_id}] Временная ошибка GAMEAU {response.status_code} "
                        f"(попытка {attempt + 1}/{self.max_retries})"
                    )
                    break  # идём на новый виток с backoff, а не долбим тот же URL

                logger.error(f"[ORDER {order_id}] Ошибка GAMEAU API {response.status_code}: {body}")
                return {"success": False, "error": f"API_ERROR_{response.status_code}", "detail": body}

            if retry_wait is not None and attempt < self.max_retries - 1:
                await asyncio.sleep(retry_wait)

        return {"success": False, "error": last_error, "detail": last_detail}

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {}

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Текущий статус заказа GAMEAU (для reconciliation)."""
        client = await self._http()
        for path in (f"orders/status/{order_id}", f"status/{order_id}", f"orders/{order_id}"):
            for url in self._candidate_urls(path):
                try:
                    response = await client.get(url, headers=self.headers)
                except httpx.RequestError as exc:
                    logger.debug(f"Ошибка запроса статуса {url}: {exc}")
                    continue
                if response.status_code == 404:
                    continue
                if response.status_code == 200:
                    data = self._safe_json(response)
                    if isinstance(data, dict):
                        return data.get("order") or data
                logger.debug(f"GAMEAU статус #{order_id}: HTTP {response.status_code}")
        return {"status": "unknown"}

    async def wait_for_completion(
        self,
        order_id: str,
        poll_interval: float = 3.0,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Ждёт терминального статуса. При отказе GAMEAU бросает RuntimeError."""
        deadline = time.time() + max(1.0, timeout)
        last_status: str | None = None

        while time.time() < deadline:
            order_data = await self.get_order_status(order_id)
            status = str(order_data.get("status") or "").lower()

            if status != last_status:
                logger.info(f"Заказ GAMEAU {order_id}: статус '{status or 'нет данных'}'")
                last_status = status

            if status in SUCCESS_STATUSES:
                return order_data
            if status in FAILED_STATUSES:
                raise RuntimeError(f"Заказ Gameau {order_id} завершился со статусом '{status}'")

            await asyncio.sleep(poll_interval)

        logger.warning(f"Истёк тайм-аут ожидания заказа GAMEAU {order_id} ({timeout:.0f}s)")
        return {"id": order_id, "status": "timeout"}

    async def get_account(self) -> dict[str, Any]:
        """Аккаунт GAMEAU: баланс, статус (для --balance и алертов)."""
        client = await self._http()
        for url in self._candidate_urls("account"):
            try:
                response = await client.get(url, headers=self.headers)
            except httpx.RequestError as exc:
                return {"error": str(exc)}
            if response.status_code == 200:
                data = self._safe_json(response)
                if isinstance(data, dict):
                    account = data.get("account") or data
                    if isinstance(account, dict) and "balance" not in account:
                        balance = parse_balance_usdt(account)
                        if balance is not None:
                            account = {**account, "balance": balance}
                    return account if isinstance(account, dict) else {}
            if response.status_code != 404:
                return {
                    "error": f"HTTP_{response.status_code}",
                    "status_code": response.status_code,
                    "detail": redact(truncate(response.text, 200), self._secrets),
                }
        return {"error": "NOT_FOUND", "status_code": 404}

    async def ping(self) -> dict[str, Any]:
        """Дешёвая проверка доступности API и ключа (для --check)."""
        account = await self.get_account()
        if "error" in account:
            return {"ok": False, "error": account.get("error"), "detail": account.get("detail", "")}
        return {"ok": True, "account": account, "balance": parse_balance_usdt(account)}
