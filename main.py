"""AutoStars — Бот автовыдачи Telegram Stars на FunPay через GAMEAU API.

Основан на AutoStars-New, переписан на GAMEAU REST API v1 с:
- Idempotency-Key (UUID v5 от order_id)
- maxCharge контроль
- hide_sender опция
- Расчёт прибыли (2 варианта завода USDT)
- История транзакций (db.json)
"""

import re
import json
import uuid
import time
import logging
import aiohttp
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from FunPayAPI import Account

logger = logging.getLogger("autostars")


def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print("Файл config.json не найден. Создайте его на основе примера.")
        exit(1)
    except json.JSONDecodeError:
        print("Ошибка чтения config.json. Проверьте синтаксис JSON.")
        exit(1)


class GameauClient:
    """Клиент GAMEAU REST API v1 для покупки Telegram Stars."""

    def __init__(self, api_key: str, base_url: str = "https://gameau.us/api/v1",
                 timeout: int = 30, max_retries: int = 3):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get_idempotency_key(self, order_id: str) -> str:
        """Детерминированный UUID v5 на основе ID заказа FunPay."""
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"funpay_{order_id}"))

    async def ping(self) -> dict:
        """Проверка доступности API и данных аккаунта."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/account",
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ok"):
                            return {"ok": True, "account": data.get("account", {})}
                    return {"ok": False}
        except Exception:
            return {"ok": False}

    async def get_catalog(self) -> list:
        """Получение каталога товаров."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/catalog",
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("catalog", data if isinstance(data, list) else [])
                    return []
        except Exception:
            return []

    async def buy_stars(
        self,
        username: str,
        quantity: int,
        order_id: str,
        max_charge_usdt: float = 9.50,
        hide_sender: bool = False,
    ) -> Dict[str, Any]:
        """
        Отправляет запрос на покупку Stars.
        Защита: Idempotency-Key (UUID v5), maxCharge контроль.
        """
        idempotency_key = self._get_idempotency_key(order_id)
        clean_username = username.replace("@", "").strip()

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

        order_urls = [
            f"{self.base_url}/orders/telegramStars",
            f"{self.base_url}/telegramStars",
        ]

        last_error = "NETWORK_TIMEOUT"

        for attempt in range(self.max_retries):
            async with aiohttp.ClientSession() as session:
                for url in order_urls:
                    try:
                        async with session.post(
                            url,
                            json=payload,
                            headers=request_headers,
                            timeout=aiohttp.ClientTimeout(total=self.timeout),
                        ) as resp:
                            if resp.status == 404:
                                continue

                            if resp.status in (200, 201):
                                data = await resp.json()
                                order_obj = data.get("order") or data
                                created_id = str(order_obj.get("id") or order_obj.get("orderId") or "")
                                logger.info(
                                    f"[ORDER {order_id}] GAMEAU: заказ создан {created_id} "
                                    f"({quantity} Stars для @{clean_username})"
                                )
                                return {
                                    "success": True,
                                    "order_id": created_id,
                                    "idempotency_key": idempotency_key,
                                    "data": data,
                                }

                            if resp.status == 409:
                                error_data = await resp.json()
                                error_msg = error_data.get("error", "")
                                if "PRICE_CHANGED" in str(error_msg).upper():
                                    logger.warning(
                                        f"[ORDER {order_id}] GAMEAU: цена изменилась (409). "
                                        f"maxCharge текущий: {max_charge_usdt} USDT"
                                    )
                                    return {
                                        "success": False,
                                        "error": "PRICE_CHANGED",
                                        "message": "Цена изменилась. Увеличьте maxCharge.",
                                        "idempotency_key": idempotency_key,
                                    }

                            try:
                                error_data = await resp.json()
                                last_error = json.dumps(error_data, ensure_ascii=False)
                            except Exception:
                                last_error = await resp.text()

                            logger.warning(
                                f"[ORDER {order_id}] GAMEAU {resp.status}: {last_error}"
                            )

                    except asyncio.TimeoutError:
                        last_error = "TIMEOUT"
                    except aiohttp.ClientError as e:
                        last_error = str(e)

            # Экспоненциальный backoff
            if attempt < self.max_retries - 1:
                wait = 2 ** attempt
                logger.info(f"[ORDER {order_id}] Retry {attempt + 1}/{self.max_retries}, ожидание {wait}s...")
                await asyncio.sleep(wait)

        return {
            "success": False,
            "error": last_error,
            "idempotency_key": idempotency_key,
        }


class StarBot:
    """Основной класс бота автовыдачи Stars."""

    def __init__(self, config: dict):
        self.config = config
        self.gameau = None
        self.bot = None
        self.dp = None
        self.api_config = self.config.get("API", {})
        self.bot_config = self.config.get("BOT", {})
        self.funpay_config = self.config.get("FUNPAY", {})
        self.settings = self.config.get("SETTINGS", {})
        self.finance = self.config.get("FINANCE", {})

    async def init_gameau(self):
        """Инициализация GAMEAU клиента."""
        self.gameau = GameauClient(
            api_key=self.api_config.get("token", ""),
            base_url=self.api_config.get("url", "https://gameau.us/api/v1"),
            timeout=self.api_config.get("request_timeout", 30),
        )

    async def load_db(self) -> dict:
        try:
            with open(self.settings.get("db_path", "db.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    async def save_db(self, db: dict):
        with open(self.settings.get("db_path", "db.json"), "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)

    async def log_transaction(self, order_id: str, username: str, stars: int,
                               cost_usdt: float, revenue_rub: float, status: str):
        """Логирование транзакции с расчётом прибыли."""
        db = await self.load_db()
        if "transactions" not in db:
            db["transactions"] = []

        usd_to_rub = self.finance.get("usd_to_rub", 100.0)
        cost_rub = cost_usdt * usd_to_rub
        profit_rub = revenue_rub - cost_rub

        tx = {
            "order_id": order_id,
            "username": username,
            "stars": stars,
            "cost_usdt": cost_usdt,
            "cost_rub": round(cost_rub, 2),
            "revenue_rub": round(revenue_rub, 2),
            "profit_rub": round(profit_rub, 2),
            "usd_to_rub": usd_to_rub,
            "status": status,
            "timestamp": datetime.now().isoformat(),
        }
        db["transactions"].append(tx)
        await self.save_db(db)
        return tx

    def calc_profit(self, stars: int) -> Dict[str, float]:
        """Расчёт прибыли для варианта 1 и 2."""
        cost_per_star = self.finance.get("cost_per_star_usd", 0.017)
        revenue_per_star = self.finance.get("revenue_per_star_rub", 1.5)
        rate_v1 = self.config.get("rate_variant_1", 110.0)
        rate_v2 = self.config.get("rate_variant_2", 87.63)

        total_cost_usd = stars * cost_per_star
        total_revenue_rub = stars * revenue_per_star

        cost_v1_rub = total_cost_usd * rate_v1
        cost_v2_rub = total_cost_usd * rate_v2

        return {
            "stars": stars,
            "cost_usd": round(total_cost_usd, 2),
            "revenue_rub": round(total_revenue_rub, 2),
            "profit_v1_rub": round(total_revenue_rub - cost_v1_rub, 2),
            "profit_v2_rub": round(total_revenue_rub - cost_v2_rub, 2),
            "rate_v1": rate_v1,
            "rate_v2": rate_v2,
        }

    async def extract_order_info(self, description: str) -> Tuple[Optional[str], Optional[int]]:
        """Извлечение логина и количества звёзд из описания заказа."""
        try:
            login = None
            if "," in description:
                login = description.split(",")[-1].strip().lstrip("@")

            if not login:
                match = re.search(r"@([A-Za-z][A-Za-z0-9_]{4,31})", description)
                if match:
                    login = match.group(1)

            stars_match = re.search(r"(\d+)\s*зв", description, re.IGNORECASE)
            stars = int(stars_match.group(1)) if stars_match else None

            return login, stars
        except Exception as e:
            logger.error(f"Ошибка извлечения данных: {e}")
            return None, None

    async def send_funpay_message(self, account: Account, username: str, message: str) -> bool:
        """Отправка сообщения в чат FunPay."""
        try:
            chat = account.get_chat_by_name(username, make_request=True)
            if not chat or not hasattr(chat, "id"):
                logger.warning(f"Чат с {username} не найден")
                return False
            account.send_message(chat_id=chat.id, text=message)
            return True
        except Exception as e:
            logger.error(f"Ошибка отправки в FunPay: {e}")
            return False

    async def create_funpay_message(self, order_id, login, stars, tx_hash) -> str:
        return (
            f"🌟 ЗАКАЗ #{order_id} ВЫПОЛНЕН! 🌟\n\n"
            f"✔ Получатель: @{login}\n"
            f"✔ Количество: {stars} шт.\n"
            f"✔ ID транзакции: {tx_hash}\n\n"
            "Звёзды зачислены автоматически.\n"
            "Пожалуйста, подтвердите получение.\n\n"
            f"Подробнее: https://funpay.com/orders/{order_id}/"
        )

    async def create_telegram_message(self, order_id, login, stars, tx_hash, profit_info=None) -> str:
        msg = (
            f"✨ УСПЕШНАЯ ВЫДАЧА ЗВЁЗД ✨\n\n"
            f"🆔 Заказ: #{order_id}\n"
            f"👤 Получатель: @{login}\n"
            f"⭐ Количество: {stars} шт.\n"
            f"📜 Транзакция: {tx_hash}\n"
        )
        if profit_info:
            msg += (
                f"\n💰 Расчёт прибыли:\n"
                f"  Затраты: ${profit_info['cost_usd']}\n"
                f"  Доход: {profit_info['revenue_rub']} ₽\n"
                f"  Прибыль (110₽/$): {profit_info['profit_v1_rub']} ₽\n"
                f"  Прибыль (87.63₽/$): {profit_info['profit_v2_rub']} ₽\n"
            )
        msg += f"\n🔗 https://funpay.com/orders/{order_id}/"
        return msg

    async def process_order(self, order, account: Account, admin_telegram_id: int):
        """Обработка одного заказа: парсинг → GAMEAU → FunPay ответ → Telegram alert."""
        login, stars = await self.extract_order_info(order.description)
        if not login or not stars or stars < 50:
            logger.warning(f"Неверные данные заказа #{order.id}: login={login}, stars={stars}")
            return

        count = getattr(order, "amount", 1) or 1
        total_stars = stars * count
        hide_sender = self.finance.get("hide_sender", False)
        max_charge = self.api_config.get("max_charge_usdt", 9.50)

        # Инициализация GAMEAU если ещё нет
        if not self.gameau:
            await self.init_gameau()

        # Отправка Stars через GAMEAU
        result = await self.gameau.buy_stars(
            username=login,
            quantity=total_stars,
            order_id=str(order.id),
            max_charge_usdt=max_charge,
            hide_sender=hide_sender,
        )

        # Расчёт прибыли
        profit_info = self.calc_profit(total_stars)

        if result["success"]:
            tx_hash = result.get("order_id", "N/A")

            # Логируем транзакцию
            await self.log_transaction(
                order_id=str(order.id),
                username=login,
                stars=total_stars,
                cost_usdt=profit_info["cost_usd"],
                revenue_rub=profit_info["revenue_rub"],
                status="SUCCESS",
            )

            # Ответ покупателю в FunPay
            fp_msg = await self.create_funpay_message(order.id, login, total_stars, tx_hash)
            await self.send_funpay_message(account, order.buyer_username, fp_msg)

            # Telegram alert админу
            tg_msg = await self.create_telegram_message(
                order.id, login, total_stars, tx_hash, profit_info
            )
            logger.info(f"[ORDER {order.id}] ✅ Успешно: {total_stars} Stars → @{login}")

            return {"success": True, "profit": profit_info}
        else:
            # Логируем ошибку
            await self.log_transaction(
                order_id=str(order.id),
                username=login,
                stars=total_stars,
                cost_usdt=0,
                revenue_rub=0,
                status=f"FAILED: {result.get('error', 'UNKNOWN')}",
            )

            error_msg = await self.create_error_message(order.id, login, total_stars, result.get("error", ""))
            logger.error(f"[ORDER {order.id}] ❌ Ошибка: {result.get('error')}")
            return {"success": False, "error": result.get("error")}

    async def create_error_message(self, order_id, login, stars, error) -> str:
        return (
            f"⚠️ ОШИБКА ВЫДАЧИ ⚠️\n\n"
            f"Заказ: #{order_id}\n"
            f"Получатель: @{login}\n"
            f"Звёзды: {stars}\n"
            f"Ошибка: {error}\n\n"
            "Рекомендации:\n"
            "1. Проверьте баланс GAMEAU\n"
            "2. Убедитесь в правильности логина\n"
            "3. Свяжитесь с поддержкой"
        )

    async def run_polling(self, account: Account, once: bool = False):
        """Основной цикл опроса заказов FunPay."""
        poll_interval = self.settings.get("order_check_interval", 10)
        admin_id = self.settings.get("admin_telegram_id")
        db = await self.load_db()
        processed = set(db.get("processed_orders", []))

        logger.info(f"Бот запущен. Интервал опроса: {poll_interval}s")
        if once:
            logger.info("Режим: однократный прогон")

        try:
            while True:
                try:
                    orders = account.get_orders(make_request=True)
                    if orders:
                        for order in orders:
                            if order.id in processed:
                                continue
                            if getattr(order, "status", None) != "new":
                                continue

                            logger.info(f"Новый заказ #{order.id}: {order.description}")
                            result = await self.process_order(order, account, admin_id)

                            processed.add(order.id)
                            db.setdefault("processed_orders", []).append(order.id)
                            await self.save_db(db)

                except Exception as e:
                    logger.error(f"Ошибка опроса: {e}")

                if once:
                    break

                await asyncio.sleep(poll_interval)

        except KeyboardInterrupt:
            logger.info("Бот остановлен")
