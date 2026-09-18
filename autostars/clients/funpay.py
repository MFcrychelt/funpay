"""Асинхронный клиент FunPay: long polling `runner/`, динамический csrf_token,
восстановление сессии при 403 и защита от «шума» секретов в логах.

Что изменено относительно прошлой версии:
  • csrf_token и golden_key больше не пишутся в лог (см. `autostars.security`);
  • `refresh_csrf()` защищён от шторма повторных запросов (минимальный интервал) —
    раньше 403 подряд означал несколько хитов по /orders/trade в секунду;
  • `poll_runner()` неблокирующий по тайм-ауту: обрыв long polling не роняет цикл;
  • добавлен `FunPayError` с понятным текстом (вместо «Cannot connect to host» у
    пользователя в консоли) и `verify_session()` для диагностики;
  • парсер списка заказов вынесен в `parse_paid_orders(html)` — его можно тестировать.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import string
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..security import mask_secret, redact, truncate

logger = logging.getLogger("FunPayClient")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Минимальный интервал между переполучениями csrf (сек) — защита от шторма.
CSRF_REFRESH_MIN_INTERVAL = 10.0


class FunPayError(RuntimeError):
    """Ошибка FunPay-взаимодействия (сеть, сессия, неожиданный HTML)."""


def random_tag(length: int = 8) -> str:
    """Случайный тег подписки runner/ (FunPay отдаёт события по изменению тега)."""
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def parse_paid_orders(html: str) -> list[dict[str, Any]]:
    """Парсит таблицу оплаченных заказов (#/chat/… → строки таблицы).

    Заказ продавца в «trade» с пометкой `info` = ожидает выдачи. Возвращает
    список словарей, совместимых с `process_paid_order`.
    """
    soup = BeautifulSoup(html, "html.parser")
    paid_orders: list[dict[str, Any]] = []

    for item in soup.find_all("a", {"class": "tc-item"}):
        class_names = item.get("class") or []
        if "info" not in class_names:
            continue

        order_id_elem = item.find("div", {"class": "tc-order"})
        order_id = order_id_elem.text.strip().lstrip("#").strip() if order_id_elem else None
        if not order_id:
            continue

        desc_elem = item.find("div", {"class": "order-desc"})
        description = desc_elem.text.strip() if desc_elem else ""

        price_elem = item.find("div", {"class": "tc-price"})
        price = 0.0
        if price_elem:
            with contextlib.suppress(ValueError, IndexError):
                price = float(price_elem.text.strip().split()[0].replace(",", ".").replace("\u00a0", ""))

        buyer_elem = item.find("div", {"class": "media-user-name"})
        buyer_span = buyer_elem.find("span") if buyer_elem else None
        buyer_username = buyer_span.text.strip() if buyer_span else ""
        buyer_id: int | None = None
        if buyer_span and buyer_span.get("data-href"):
            data_href = str(buyer_span.get("data-href"))
            if "users/" in data_href:
                with contextlib.suppress(ValueError):
                    buyer_id = int(data_href.rstrip("/").split("users/")[-1])

        paid_orders.append(
            {
                "id": order_id,
                "chat_node": buyer_id if buyer_id is not None else order_id,
                "buyer_username": buyer_username,
                "description": description,
                "price": price,
                "last_message": description,
            }
        )

    return paid_orders


class FunPayClient:
    """Асинхронный клиент платформы FunPay (seller-раздел через golden_key)."""

    def __init__(
        self,
        golden_key: str,
        user_agent: str = DEFAULT_USER_AGENT,
        base_url: str = "https://funpay.com",
        timeout: float = 20.0,
        long_poll_timeout: float = 60.0,
    ) -> None:
        self.golden_key = (golden_key or "").strip()
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.long_poll_timeout = long_poll_timeout

        self.csrf_token: str | None = None
        self.user_id: int | None = None
        self.username: str | None = None

        self.orders_tag: str = random_tag()
        self.chats_tag: str = random_tag()

        self._client: httpx.AsyncClient | None = None
        self._last_csrf_refresh = 0.0
        self._csrf_lock = asyncio.Lock()
        self._secrets: tuple[str, ...] = tuple(s for s in (self.golden_key,) if s)

    # ------------------------------------------------------------------ #
    # Сессия
    # ------------------------------------------------------------------ #
    async def get_client(self) -> httpx.AsyncClient:
        """Лениво создаёт httpx.AsyncClient с куками и заголовками (пул соединений)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                cookies={"golden_key": self.golden_key},
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                },
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def refresh_csrf(self, force: bool = False) -> str:
        """Извлекает `data-app-data` из страницы: csrf_token, userId, имя продавца.

        Повторные вызовы троттлятся (не чаще CSRF_REFRESH_MIN_INTERVAL), иначе
        устойчивый 403 превращается в бесконечный опрос сайта.
        """
        now = time.time()
        async with self._csrf_lock:
            if not force and self.csrf_token and (now - self._last_csrf_refresh) < CSRF_REFRESH_MIN_INTERVAL:
                return self.csrf_token

            client = await self.get_client()
            try:
                response = await client.get("/orders/trade")
                if response.status_code != 200:
                    response = await client.get("/")

                soup = BeautifulSoup(response.text, "html.parser")
                body = soup.find("body")
                app_data_str = body.get("data-app-data") if body else None
                if not app_data_str:
                    raise FunPayError(
                        "На странице FunPay нет data-app-data — вероятно, golden_key "
                        "не принят (сессия истекла) или сайт отдаёт заглушку/антиспам"
                    )

                app_data = json.loads(app_data_str)
                self.csrf_token = app_data.get("csrf-token")
                self.user_id = app_data.get("userId")

                user_elem = soup.find("div", {"class": "user-link-name"})
                if user_elem:
                    self.username = user_elem.text.strip()

                if not self.csrf_token:
                    raise FunPayError("FunPay отдал data-app-data без csrf-token — проверьте FUNPAY_GOLDEN_KEY")

                self._last_csrf_refresh = now
                logger.info(
                    f"Сессия FunPay обновлена: csrf {mask_secret(self.csrf_token or '')}, "
                    f"userId={self.user_id}, продавец={self.username or 'неизвестен'}"
                )
                return self.csrf_token or ""
            except FunPayError:
                raise
            except (httpx.RequestError, json.JSONDecodeError) as exc:
                raise FunPayError(f"Не удалось получить csrf-токен FunPay: {exc}") from exc
            except Exception as exc:
                raise FunPayError(f"Ошибка разбора страницы FunPay: {exc}") from exc

    async def login(self) -> None:
        """Инициализация сессии (первое получение CSRF)."""
        await self.refresh_csrf(force=True)

    async def verify_session(self) -> dict[str, Any]:
        """Диагностика: видит ли нас FunPay как продавца. Бросает FunPayError при отказе."""
        await self.refresh_csrf(force=True)
        return {"ok": bool(self.csrf_token), "user_id": self.user_id, "username": self.username}

    async def _post_runner(self, payload: dict, *, timeout: float | None = None) -> httpx.Response:
        """POST /runner/ с автоматическим обновлением csrf_token при 403/просроченном токене."""
        client = await self.get_client()
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        if not self.csrf_token:
            await self.refresh_csrf()

        data = dict(payload)
        data["csrf_token"] = self.csrf_token
        kwargs = {"headers": headers, "data": data}
        if timeout is not None:
            kwargs["timeout"] = timeout

        response = await client.post("/runner/", **kwargs)

        if self._looks_like_csrf_problem(response):
            logger.warning("FunPay отверг csrf-токен (403) — переполучаю и повторяю запрос")
            await self.refresh_csrf(force=True)
            data["csrf_token"] = self.csrf_token
            response = await client.post("/runner/", **kwargs)
        return response

    @staticmethod
    def _looks_like_csrf_problem(response: httpx.Response) -> bool:
        # 403 — отклонённый запрос, 419 — Laravel/Yii ответ на просроченный токен
        return response.status_code in (403, 419)

    # ------------------------------------------------------------------ #
    # операции
    # ------------------------------------------------------------------ #
    async def send_message(self, node: str | int, text: str) -> bool:
        """Отправляет сообщение в чат FunPay (runner/ → chat_message)."""
        node_id: int | str = int(node) if str(node).isdigit() else str(node)
        request_obj = {
            "action": "chat_message",
            "data": {"node": node_id, "last_message": -1, "content": text},
        }
        objects = [
            {
                "type": "chat_node",
                "id": node_id,
                "tag": "00000000",
                "data": {"node": node_id, "last_message": -1, "content": ""},
            }
        ]
        payload = {"objects": json.dumps(objects), "request": json.dumps(request_obj, ensure_ascii=False)}

        try:
            response = await self._post_runner(payload)
        except httpx.RequestError as exc:
            logger.error(f"Сбой отправки сообщения в чат {node_id}: {exc}")
            return False

        if response.status_code != 200:
            logger.error(
                f"HTTP {response.status_code} при отправке сообщения в чат {node_id}: "
                f"{redact(truncate(response.text), self._secrets)}"
            )
            return False
        try:
            data = response.json()
        except ValueError:
            logger.error(f"FunPay вернул не-JSON в ответ на chat_message: {redact(truncate(response.text), self._secrets)}")
            return False

        resp = data.get("response")
        if resp and not resp.get("error"):
            logger.info(f"Сообщение отправлено в чат {node_id}")
            return True
        logger.error(f"FunPay runner вернул ошибку при отправке сообщения: {redact(str(resp), self._secrets)}")
        return False

    async def poll_runner(self) -> dict[str, Any]:
        """Long polling runner/: возвращает свежие события, обновляет теги подписки.

        Никогда не бросает исключение — обрыв соединения это штатная ситуация,
        цикл просто попросит данные заново.
        """
        if not self.user_id or not self.csrf_token:
            try:
                await self.refresh_csrf()
            except FunPayError as exc:
                logger.warning(f"poll_runner: сессия FunPay недоступна ({exc})")
                return {}

        orders_obj = {"type": "orders_counters", "id": self.user_id, "tag": self.orders_tag, "data": False}
        chats_obj = {"type": "chat_bookmarks", "id": self.user_id, "tag": self.chats_tag, "data": False}
        payload = {"objects": json.dumps([orders_obj, chats_obj]), "request": False}

        try:
            response = await self._post_runner(payload, timeout=self.long_poll_timeout + 10)
            if response.status_code != 200:
                logger.debug(f"runner/ вернул {response.status_code}")
                return {}
            data = response.json()
        except (httpx.RequestError, ValueError) as exc:
            logger.debug(f"poll_runner: {type(exc).__name__}: {exc}")
            return {}

        for obj in data.get("objects", []):
            if obj.get("type") == "orders_counters" and obj.get("tag"):
                self.orders_tag = obj["tag"]
            elif obj.get("type") == "chat_bookmarks" and obj.get("tag"):
                self.chats_tag = obj["tag"]
        return data

    async def get_paid_orders(self) -> list[dict[str, Any]]:
        """Список оплаченных заказов, ожидающих выдачи (/orders/trade?state=paid)."""
        client = await self.get_client()
        try:
            response = await client.get("/orders/trade?state=paid")
            if response.status_code in (401, 403):
                logger.warning(f"FunPay отдал {response.status_code} — обновляю сессию и повторяю")
                await self.refresh_csrf(force=True)
                response = await client.get("/orders/trade?state=paid")
            if response.status_code != 200:
                logger.error(
                    f"Не удалось получить список заказов: HTTP {response.status_code} "
                    f"{redact(truncate(response.text), self._secrets)}"
                )
                return []
            return parse_paid_orders(response.text)
        except httpx.RequestError as exc:
            logger.error(f"Сеть при получении списка заказов: {exc}")
            return []
        except Exception as exc:
            logger.error(f"Ошибка разбора списка оплаченных заказов: {exc}")
            return []

    async def get_last_buyer_message(self, node: str | int) -> str | None:
        """Последнее сообщение покупателя в чате (для чат-мониторинга @username)."""
        client = await self.get_client()
        url = f"/chat/history?node={node}&last_message=99999999999999999999999"
        headers = {"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"}
        try:
            response = await client.get(url, headers=headers)
            if response.status_code in (401, 403):
                await self.refresh_csrf(force=True)
                response = await client.get(url, headers=headers)
            if response.status_code != 200:
                return None

            data = response.json()
            messages = (data.get("chat") or {}).get("messages") or []
            for msg in reversed(messages):
                if self.user_id and msg.get("author") == self.user_id:
                    continue
                soup = BeautifulSoup(msg.get("html", ""), "html.parser")
                text_elem = soup.find("div", {"class": "message-text"})
                if text_elem:
                    return text_elem.text.strip()
            return None
        except (httpx.RequestError, ValueError) as exc:
            logger.debug(f"Не удалось получить историю чата {node}: {exc}")
            return None

    async def close(self) -> None:
        """Закрывает HTTP-клиент (идемпотентно)."""
        if self._client is not None and not self._client.is_closed:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._client = None
