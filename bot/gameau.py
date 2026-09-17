"""Клиент GAMEAU REST API (https://gameau.us/api-docs.html).

Реализует только то, что нужно для отправки Telegram Stars:
    * GET  /account                      — баланс и статус аккаунта;
    * GET  /catalog?type=telegramStars   — каталог пакетов звёзд с ценами;
    * POST /orders/telegramStars         — создание заказа на звёзды
                                           (обязателен заголовок Idempotency-Key);
    * GET  /orders/status/{order_id}     — статус заказа.

Документация требует для каждого нового заказа уникальный `Idempotency-Key`
(UUID). Повтор того же запроса с тем же ключом не создаёт второй заказ,
поэтому при сбоях/перезапусках бот безопасно повторяет запрос с сохранённым
ключом — звёзды не списываются дважды.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import requests

from .logger_setup import get_logger

logger = get_logger("gameau")

#: Статусы, означающие успешное завершение заказа поставщиком.
SUCCESS_STATUSES = {"success", "completed", "complete", "delivered", "paid"}
#: Статусы, означающие окончательный отказ.
FAILED_STATUSES = {"failed", "refunded", "cancelled", "canceled", "rejected", "error"}
#: Всё, что не в терминальных множествах (например, "processing"), — в работе.


class GameauError(Exception):
    """Ошибка API GAMEAU."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 code: str | None = None, details: Any = None, request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details
        self.request_id = request_id


class InsufficientBalanceError(GameauError):
    """Недостаточно средств на балансе GAMEAU."""


class PriceChangedError(GameauError):
    """Цена изменилась; в `details` может быть актуальная цена."""


class GameauTimeoutError(GameauError):
    """Истёк тайм-аут ожидания статуса; заказ может ещё выполняться."""


class GameauClient:
    """Обёртка над GAMEAU REST API."""

    def __init__(self, api_key: str, base_url: str = "https://gameau.us/api/v1",
                 timeout: int = 30, session: requests.Session | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = session or requests.Session()
        self._catalog_cache: tuple[float, list[dict[str, Any]]] | None = None

    # ------------------------------------------------------------------ #
    # Низкий уровень
    # ------------------------------------------------------------------ #
    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _request(self, method: str, path: str, *,
                 json_body: dict[str, Any] | None = None,
                 idempotency_key: str | None = None) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._headers(idempotency_key)
        try:
            response = self._session.request(
                method, url, json=json_body, headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise GameauError(f"Сетевая ошибка при запросе к GAMEAU: {exc}") from exc

        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}

        if response.status_code >= 400:
            code = payload.get("code") if isinstance(payload, dict) else None
            error = payload.get("error") if isinstance(payload, dict) else None
            request_id = payload.get("requestId") if isinstance(payload, dict) else None
            message = f"GAMEAU HTTP {response.status_code}: {code or ''} {error or response.text[:200]}"
            kwargs = {
                "status_code": response.status_code,
                "code": code,
                "details": payload.get("details") if isinstance(payload, dict) else None,
                "request_id": request_id,
            }
            if code == "INSUFFICIENT_BALANCE":
                raise InsufficientBalanceError(message, **kwargs)
            if code == "PRICE_CHANGED":
                raise PriceChangedError(message, **kwargs)
            raise GameauError(message, **kwargs)

        if isinstance(payload, dict) and payload.get("ok") is False:
            raise GameauError(
                f"GAMEAU вернул ok=false: {payload.get('code')} {payload.get('error', '')}",
                code=payload.get("code"),
                details=payload.get("details"),
                request_id=payload.get("requestId"),
            )
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ #
    # Публичные методы
    # ------------------------------------------------------------------ #
    def get_account(self) -> dict[str, Any]:
        """Возвращает информацию об аккаунте (баланс, тариф, статус)."""
        data = self._request("GET", "account")
        return data.get("account", data)

    def get_stars_catalog(self, ttl: float = 120.0) -> list[dict[str, Any]]:
        """Каталог пакетов Telegram Stars с ценами по вашему тарифу.

        Результат кешируется на `ttl` секунд, чтобы не дёргать API на каждый заказ.
        """
        now = time.time()
        if self._catalog_cache and now - self._catalog_cache[0] < ttl:
            return self._catalog_cache[1]

        items: list[dict[str, Any]] = []
        data = self._get_catalog_page(limit=200)
        items.extend((data.get("catalog") or {}).get("items") or data.get("items") or [])

        self._catalog_cache = (now, items)
        return items

    def _get_catalog_page(self, limit: int) -> dict[str, Any]:
        """Каталог позиций типа Telegram Stars (одним запросом)."""
        url = f"{self.base_url}/catalog"
        params = {"type": "telegramStars", "limit": limit}
        try:
            response = self._session.get(
                url, params=params, headers=self._headers(), timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise GameauError(f"Сетевая ошибка при запросе каталога: {exc}") from exc
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise GameauError(
                f"GAMEAU каталог HTTP {response.status_code}: {code or response.text[:200]}",
                status_code=response.status_code, code=code,
            )
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _item_stars(item: dict[str, Any]) -> int | None:
        """Определяет количество звёзд в позиции каталога."""
        order = item.get("order") or {}
        body = order.get("body") or {}
        for key in ("quantity", "stars", "amount"):
            value = body.get(key) or item.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        # Пытаемся вытащить число из названия, например "Telegram Stars 250".
        name = item.get("name") or item.get("title") or ""
        match = re.search(r"(\d[\d\s]*)", str(name))
        if match:
            try:
                return int(match.group(1).replace(" ", ""))
            except ValueError:
                return None
        return None

    def package_stars(self, item: dict[str, Any]) -> int | None:
        """Количество звёзд в позиции каталога (публичная обёртка)."""
        return self._item_stars(item)

    def find_stars_package(self, stars: int) -> dict[str, Any] | None:
        """Ищет в каталоге пакет на `stars` звёзд.

        Если точного пакета нет — возвращает ближайший больший, чтобы покупателю
        пришло не меньше оплаченного. Возвращает позицию каталога или `None`.
        """
        items = [i for i in self.get_stars_catalog() if i.get("orderable", True)]
        exact = [i for i in items if self._item_stars(i) == stars]
        if exact:
            return min(exact, key=lambda i: float(i.get("price") or 10**9))
        greater = sorted(
            (i for i in items if (self._item_stars(i) or 0) >= stars),
            key=lambda i: self._item_stars(i) or 0,
        )
        if greater:
            cheapest = min(greater, key=lambda i: float(i.get("price") or 10**9))
            logger.info(
                "Точного пакета на %d звёзд нет, использую ближайший больший (%d).",
                stars, self._item_stars(cheapest),
            )
            return cheapest
        return None

    def send_stars(self, telegram_username: str, quantity: int, max_charge: float,
                   idempotency_key: str | None = None) -> dict[str, Any]:
        """Создаёт заказ на отправку звёзд. Возвращает объект заказа.

        :param telegram_username: юзернейм получателя (без @).
        :param quantity: количество звёзд.
        :param max_charge: максимальная сумма списания в USD.
        :param idempotency_key: UUID идемпотентности (обязателен для API).
        """
        username = telegram_username.strip().lstrip("@")
        key = idempotency_key or str(uuid.uuid4())
        body = {
            "telegram_username": username,
            "quantity": int(quantity),
            "maxCharge": round(float(max_charge), 2),
        }
        logger.info(
            "Создаю заказ GAMEAU: %d звёзд → @%s (maxCharge=%.2f USD, idem=%s)",
            quantity, username, max_charge, key,
        )
        data = self._request("POST", "orders/telegramStars", json_body=body,
                             idempotency_key=key)
        order = data.get("order") or data
        logger.info("Заказ GAMEAU создан: %s (статус: %s)",
                    order.get("id") or order.get("orderId"), order.get("status"))
        return order

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Возвращает актуальный статус заказа."""
        data = self._request("GET", f"orders/status/{order_id}")
        return data.get("order") or data

    def wait_for_completion(self, order_id: str, *, poll_interval: float = 5.0,
                            timeout: float = 600.0) -> dict[str, Any]:
        """Ждёт терминального статуса заказа.

        :raises GameauError: если заказ завершился ошибкой или истёк тайм-аут.
        """
        deadline = time.time() + timeout
        last_status = None
        while True:
            order = self.get_order_status(order_id)
            status = str(order.get("status") or "").lower()
            if status != last_status:
                logger.info("Заказ GAMEAU %s: статус %s", order_id, status or "неизвестен")
                last_status = status
            if status in SUCCESS_STATUSES:
                return order
            if status in FAILED_STATUSES:
                raise GameauError(
                    f"Заказ GAMEAU {order_id} завершился со статусом '{status}'",
                    code=status, details=order,
                )
            if time.time() >= deadline:
                raise GameauTimeoutError(
                    f"Не дождались завершения заказа GAMEAU {order_id} за {timeout:.0f} с "
                    f"(последний статус: {status or 'неизвестен'})"
                )
            time.sleep(poll_interval)
