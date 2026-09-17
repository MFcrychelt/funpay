"""Асинхронный клиент FunPay с поддержкой Long Polling (runner/), динамического извлечения csrf_token и защиты от 403."""

from __future__ import annotations

import json
import logging
import random
import string
from typing import Any, Dict, List, Optional
from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger("FunPayClient")


def random_tag(length: int = 8) -> str:
    """Генерирует случайный тег для runner/."""
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


class FunPayClient:
    """Асинхронный клиент для работы с платформой FunPay."""

    def __init__(
        self,
        golden_key: str,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        base_url: str = "https://funpay.com",
        timeout: float = 20.0,
    ):
        self.golden_key = golden_key
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.csrf_token: Optional[str] = None
        self.user_id: Optional[int] = None
        self.username: Optional[str] = None

        self.orders_tag: str = random_tag()
        self.chats_tag: str = random_tag()

        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        """Возвращает или создает экземпляр httpx.AsyncClient с куками и заголовками."""
        if self._client is None or self._client.is_closed:
            cookies = {"golden_key": self.golden_key}
            headers = {
                "User-Agent": self.user_agent,
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                cookies=cookies,
                headers=headers,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def refresh_csrf(self) -> str:
        """
        Динамически извлекает csrf_token и user_id из data-app-data на странице FunPay.
        Обновляет внутреннее состояние клиента.
        """
        client = await self.get_client()
        try:
            response = await client.get("/orders/trade")
            if response.status_code != 200:
                response = await client.get("/")

            soup = BeautifulSoup(response.text, "html.parser")
            body = soup.find("body")
            if not body or not body.get("data-app-data"):
                raise ValueError("Тег <body> не содержит атрибут data-app-data")

            app_data_str = body.get("data-app-data")
            app_data = json.loads(app_data_str)

            self.csrf_token = app_data.get("csrf-token")
            self.user_id = app_data.get("userId")

            user_elem = soup.find("div", {"class": "user-link-name"})
            if user_elem:
                self.username = user_elem.text.strip()

            logger.info(
                f"Успешно получен csrf_token: {self.csrf_token[:10]}... | User ID: {self.user_id} | Продавец: {self.username}"
            )
            return self.csrf_token or ""

        except Exception as exc:
            logger.error(f"Ошибка при динамическом извлечении CSRF: {exc}")
            raise

    async def login(self) -> None:
        """Инициализация сессии и первичное получение CSRF токена."""
        await self.refresh_csrf()

    async def _post_runner_request(self, payload: dict) -> httpx.Response:
        """
        Отправляет POST запрос к runner/ с автоматической защитой от 403 Forbidden.
        При получении 403 переполучает csrf_token и повторяет запрос.
        """
        client = await self.get_client()
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }

        # Гарантируем актуальный CSRF токен
        if not self.csrf_token:
            await self.refresh_csrf()

        data = dict(payload)
        data["csrf_token"] = self.csrf_token

        response = await client.post("/runner/", data=data, headers=headers)

        if response.status_code == 403 or (
            response.status_code == 200 and "csrf" in response.text.lower() and "error" in response.text.lower()
        ):
            logger.warning("Получен 403 Forbidden или устаревший CSRF. Автоматическое обновление токена...")
            await self.refresh_csrf()
            data["csrf_token"] = self.csrf_token
            response = await client.post("/runner/", data=data, headers=headers)

        return response

    async def send_message(self, node: str | int, text: str) -> bool:
        """
        Отправляет сообщение в чат FunPay (runner/ -> chat_message).
        """
        node_id = int(node) if str(node).isdigit() else str(node)
        request_obj = {
            "action": "chat_message",
            "data": {
                "node": node_id,
                "last_message": -1,
                "content": text,
            },
        }
        objects = [
            {
                "type": "chat_node",
                "id": node_id,
                "tag": "00000000",
                "data": {
                    "node": node_id,
                    "last_message": -1,
                    "content": "",
                },
            }
        ]

        payload = {
            "objects": json.dumps(objects),
            "request": json.dumps(request_obj),
        }

        try:
            response = await self._post_runner_request(payload)
            if response.status_code == 200:
                data = response.json()
                resp = data.get("response")
                if resp and not resp.get("error"):
                    logger.info(f"Сообщение успешно отправлено в чат {node_id}")
                    return True
                logger.error(f"FunPay runner вернул ошибку при отправке сообщения: {resp}")
                return False
            logger.error(f"HTTP ошибка при отправке сообщения: {response.status_code} - {response.text}")
            return False
        except Exception as exc:
            logger.error(f"Сбой отправки сообщения в FunPay: {exc}")
            return False

    async def poll_runner(self) -> Dict[str, Any]:
        """
        Осуществляет Long Polling запрос к runner/ для получения свежих событий.
        Обновляет теги orders_tag и chats_tag.
        """
        if not self.user_id or not self.csrf_token:
            await self.refresh_csrf()

        orders_obj = {
            "type": "orders_counters",
            "id": self.user_id,
            "tag": self.orders_tag,
            "data": False,
        }
        chats_obj = {
            "type": "chat_bookmarks",
            "id": self.user_id,
            "tag": self.chats_tag,
            "data": False,
        }

        payload = {
            "objects": json.dumps([orders_obj, chats_obj]),
            "request": False,
        }

        try:
            response = await self._post_runner_request(payload)
            if response.status_code == 200:
                data = response.json()
                for obj in data.get("objects", []):
                    if obj.get("type") == "orders_counters" and obj.get("tag"):
                        self.orders_tag = obj["tag"]
                    elif obj.get("type") == "chat_bookmarks" and obj.get("tag"):
                        self.chats_tag = obj["tag"]
                return data
            logger.warning(f"runner/ вернул статус {response.status_code}")
            return {}
        except Exception as exc:
            logger.error(f"Ошибка при вызове poll_runner: {exc}")
            return {}

    async def get_paid_orders(self) -> List[Dict[str, Any]]:
        """
        Получает и парсит список оплаченных заказов со страницы https://funpay.com/orders/trade?state=paid.
        """
        client = await self.get_client()
        try:
            response = await client.get("/orders/trade?state=paid")
            if response.status_code == 403:
                logger.warning("403 Forbidden при получении заказов. Обновляю CSRF...")
                await self.refresh_csrf()
                response = await client.get("/orders/trade?state=paid")

            if response.status_code != 200:
                logger.error(f"Не удалось получить список заказов: {response.status_code}")
                return []

            soup = BeautifulSoup(response.text, "html.parser")
            items = soup.find_all("a", {"class": "tc-item"})
            paid_orders = []

            for item in items:
                class_names = item.get("class", [])
                # Оплаченные заказы имеют класс "info"
                if "info" not in class_names:
                    continue

                order_id_elem = item.find("div", {"class": "tc-order"})
                order_id = order_id_elem.text.strip().lstrip("#") if order_id_elem else None

                desc_elem = item.find("div", {"class": "order-desc"})
                description = desc_elem.text.strip() if desc_elem else ""

                price_elem = item.find("div", {"class": "tc-price"})
                price = 0.0
                if price_elem:
                    try:
                        price = float(price_elem.text.strip().split()[0].replace(",", "."))
                    except (ValueError, IndexError):
                        price = 0.0

                buyer_elem = item.find("div", {"class": "media-user-name"})
                buyer_span = buyer_elem.find("span") if buyer_elem else None
                buyer_username = buyer_span.text.strip() if buyer_span else ""
                buyer_id = None
                if buyer_span and buyer_span.get("data-href"):
                    data_href = buyer_span.get("data-href")
                    if "users/" in data_href:
                        try:
                            buyer_id = int(data_href.rstrip("/").split("users/")[-1])
                        except ValueError:
                            buyer_id = None

                paid_orders.append({
                    "id": order_id,
                    "chat_node": buyer_id,
                    "buyer_username": buyer_username,
                    "description": description,
                    "price": price,
                    "last_message": description,  # По умолчанию из описания
                })

            return paid_orders

        except Exception as exc:
            logger.error(f"Ошибка при получении оплаченных заказов: {exc}")
            return []

    async def get_last_buyer_message(self, node: str | int) -> Optional[str]:
        """
        Получает историю чата по ноде и возвращает последнее сообщение от покупателя.
        """
        client = await self.get_client()
        try:
            url = f"/chat/history?node={node}&last_message=99999999999999999999999"
            response = await client.get(
                url,
                headers={"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"},
            )
            if response.status_code == 403:
                await self.refresh_csrf()
                response = await client.get(
                    url,
                    headers={"Accept": "*/*", "X-Requested-With": "XMLHttpRequest"},
                )

            if response.status_code != 200:
                return None

            data = response.json()
            messages = (data.get("chat") or {}).get("messages", [])
            for msg in reversed(messages):
                author_id = msg.get("author")
                if self.user_id and author_id == self.user_id:
                    continue
                html = msg.get("html", "")
                soup = BeautifulSoup(html, "html.parser")
                text_elem = soup.find("div", {"class": "message-text"})
                if text_elem:
                    return text_elem.text.strip()
            return None
        except Exception as exc:
            logger.debug(f"Не удалось получить историю чата {node}: {exc}")
            return None

    async def close(self) -> None:
        """Закрывает сессию клиента."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
