"""AutoStars GUI — Flet-интерфейс для управления ботом автовыдачи Stars.

Вкладки:
- 🎮 Управление: старт/стоп бота, статус
- ⚙️ Настройки: API ключи, поведение, финансы
- 📊 Статистика: заказы, прибыль, транзакции
- 📝 Логи: последние события
"""

import flet as ft
import json
import asyncio
import threading
import os
from datetime import datetime
from typing import Optional

from main import StarBot, load_config, GameauClient


CONFIG_FILE = "config.json"
DB_FILE = "db.json"


def read_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def read_db() -> dict:
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


class AutoStarsGUI:
    def __init__(self):
        self.config = read_config()
        self.bot: Optional[StarBot] = None
        self.bot_thread: Optional[threading.Thread] = None
        self.is_running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Flet-элементы (заполняются при построении UI)
        self.status_icon: Optional[ft.Icon] = None
        self.status_text: Optional[ft.Text] = None
        self.log_list: Optional[ft.ListView] = None
        self.stats_text: Optional[ft.Text] = None

        # Themed colors
        self.theme = {
            "primary": ft.Colors.CYAN_400,
            "bg": ft.Colors.BLACK,
            "surface": ft.Colors.GREY_900,
            "accent": ft.Colors.BLUE_400,
            "green": ft.Colors.GREEN_400,
            "red": ft.Colors.RED_400,
            "yellow": ft.Colors.AMBER_400,
        }

    def _add_log(self, msg: str, level: str = "info"):
        """Добавить строку в лог-виджет (потокобезопасно)."""
        if not self.log_list:
            return
        colors = {
            "info": ft.Colors.WHITE,
            "success": ft.Colors.GREEN_400,
            "warning": ft.Colors.AMBER_400,
            "error": ft.Colors.RED_400,
        }
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_list.controls.append(
            ft.Text(f"[{ts}] {msg}", color=colors.get(level, ft.Colors.WHITE), size=12)
        )
        if len(self.log_list.controls) > 200:
            self.log_list.controls = self.log_list.controls[-200:]

    def _start_bot(self, e):
        """Запуск бота в отдельном потоке."""
        if self.is_running:
            return

        self.config = read_config()
        if not self.config.get("API", {}).get("token"):
            self._add_log("❌ API токен не задан! Заполните настройки.", "error")
            return
        if not self.config.get("FUNPAY", {}).get("golden_key"):
            self._add_log("❌ FunPay golden_key не задан!", "error")
            return

        self.is_running = True
        if self.status_icon:
            self.status_icon.color = ft.Colors.GREEN_400
            self.status_icon.name = ft.Icons.PLAY_CIRCLE
        if self.status_text:
            self.status_text.value = "🟢 Работает"
            self.status_text.color = ft.Colors.GREEN_400

        self._add_log("🚀 Бот запускается...", "success")

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.bot = StarBot(self.config)
                self.loop.run_until_complete(self.bot.init_gameau())

                # FunPay логин
                from FunPayAPI import Account
                account = Account(self.config["FUNPAY"]["golden_key"])
                account.login()

                self._add_log(f"✅ FunPay: {account.username} (ID {account.id})", "success")
                self._add_log("🤖 Бот запущен, опрашиваю заказы...", "success")

                self.loop.run_until_complete(self.bot.run_polling(account))
            except Exception as ex:
                self._add_log(f"❌ Ошибка: {ex}", "error")
            finally:
                self.is_running = False
                if self.status_icon:
                    self.status_icon.color = ft.Colors.RED_400
                    self.status_icon.name = ft.Icons.STOP_CIRCLE
                if self.status_text:
                    self.status_text.value = "🔴 Остановлен"
                    self.status_text.color = ft.Colors.RED_400

        self.bot_thread = threading.Thread(target=run, daemon=True)
        self.bot_thread.start()

    def _stop_bot(self, e):
        """Остановка бота."""
        if not self.is_running:
            return
        self.is_running = False
        if self.bot and self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.status_icon:
            self.status_icon.color = ft.Colors.RED_400
            self.status_icon.name = ft.Icons.STOP_CIRCLE
        if self.status_text:
            self.status_text.value = "🔴 Остановлен"
            self.status_text.color = ft.Colors.RED_400
        self._add_log("⏹ Бот остановлен", "warning")

    def _check_connection(self, e):
        """Проверка подключения к GAMEAU."""
        cfg = read_config()
        api_key = cfg.get("API", {}).get("token", "")
        base_url = cfg.get("API", {}).get("url", "https://gameau.us/api/v1")
        if not api_key:
            self._add_log("❌ API токен не задан", "error")
            return

        import urllib.request
        import json as _json
        try:
            req = urllib.request.Request(
                f"{base_url}/ping",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    self._add_log("✅ GAMEAU API доступен", "success")
                else:
                    self._add_log(f"❌ GAMEAU ответил: {resp.status}", "error")
        except Exception as ex:
            self._add_log(f"❌ GAMEAU недоступен: {ex}", "error")

    def _save_settings(self, e, fields: dict):
        """Сохранение настроек из полей ввода."""
        cfg = read_config()
        for key, field in fields.items():
            # Switch — булево значение
            if isinstance(field, ft.Switch):
                cfg.setdefault("FINANCE", {})[key] = field.value
                continue
            val = field.value.strip() if isinstance(field.value, str) else str(field.value)
            if not val:
                continue
            # Определяем тип
            if key in ("request_timeout", "order_check_interval"):
                cfg.setdefault("SETTINGS", {})[key] = int(val)
            elif key in ("max_charge_usdt",):
                cfg.setdefault("API", {})[key] = float(val)
            elif key == "hide_sender":
                cfg.setdefault("FINANCE", {})[key] = field.value
            else:
                if "API" in key or key == "url" or key == "token":
                    cfg.setdefault("API", {})[key] = val
                elif "FUNPAY" in key or key == "golden_key":
                    cfg.setdefault("FUNPAY", {})[key] = val
                elif "BOT" in key or key == "bot_token":
                    cfg.setdefault("BOT", {})[key] = val
                else:
                    cfg.setdefault("SETTINGS", {})[key] = val

        write_config(cfg)
        self.config = cfg
        self._add_log("✅ Настройки сохранены", "success")

    def _refresh_stats(self, e):
        """Обновление статистики."""
        db = read_db()
        txs = db.get("transactions", [])
        total = len(txs)
        success = sum(1 for t in txs if t.get("status") == "SUCCESS")
        total_stars = sum(t.get("stars", 0) for t in txs if t.get("status") == "SUCCESS")
        total_profit_v1 = sum(t.get("profit_rub", 0) for t in txs if t.get("status") == "SUCCESS")
        total_cost = sum(t.get("cost_usdt", 0) for t in txs if t.get("status") == "SUCCESS")

        # Сегодня
        today = datetime.now().strftime("%Y-%m-%d")
        today_txs = [t for t in txs if t.get("timestamp", "").startswith(today)]
        today_profit = sum(t.get("profit_rub", 0) for t in today_txs if t.get("status") == "SUCCESS")
        today_count = sum(1 for t in today_txs if t.get("status") == "SUCCESS")

        stats = (
            f"📊 СТАТИСТИКА\n\n"
            f"Всего заказов: {total} (✅ {success})\n"
            f"Всего звёзд: {total_stars}\n"
            f"Затраты: ${total_cost:.2f}\n"
            f"Прибыль (общая): {total_profit_v1:.0f} ₽\n\n"
            f"📅 Сегодня:\n"
            f"  Заказов: {today_count}\n"
            f"  Прибыль: {today_profit:.0f} ₽\n\n"
            f"🔄 Последние 5 транзакций:\n"
        )
        for tx in txs[-5:]:
            status = "✅" if tx.get("status") == "SUCCESS" else "❌"
            stats += (
                f"  {status} #{tx.get('order_id', '?')} | "
                f"@{tx.get('username', '?')} | "
                f"{tx.get('stars', 0)}⭐ | "
                f"{tx.get('profit_rub', 0):.0f}₽\n"
            )

        if self.stats_text:
            self.stats_text.value = stats

    def _calc_profit_dialog(self, e, page: ft.Page):
        """Диалог калькулятора прибыли."""
        cfg = read_config()
        finance = cfg.get("FINANCE", {})
        cost_per_star = finance.get("cost_per_star_usd", 0.017)
        revenue_per_star = finance.get("revenue_per_star_rub", 1.5)
        rate_v1 = cfg.get("rate_variant_1", 110.0)
        rate_v2 = cfg.get("rate_variant_2", 87.63)

        stars_field = ft.TextField(label="Количество звёзд", value="1000", width=200)
        result_text = ft.Text("", size=14)

        def calc(e):
            try:
                stars = int(stars_field.value)
            except ValueError:
                result_text.value = "Введите число"
                page.update()
                return
            cost_usd = stars * cost_per_star
            revenue_rub = stars * revenue_per_star
            profit_v1 = revenue_rub - cost_usd * rate_v1
            profit_v2 = revenue_rub - cost_usd * rate_v2
            result_text.value = (
                f"Звёзд: {stars}\n"
                f"Затраты: ${cost_usd:.2f}\n"
                f"Доход: {revenue_rub:.0f} ₽\n\n"
                f"Прибыль (110₽/$): {profit_v1:.0f} ₽\n"
                f"Прибыль (87.63₽/$): {profit_v2:.0f} ₽"
            )
            page.update()

        dlg = ft.AlertDialog(
            title=ft.Text("🧮 Калькулятор прибыли"),
            content=ft.Column([
                stars_field,
                ft.ElevatedButton("Рассчитать", on_click=calc),
                result_text,
            ]),
            actions=[ft.TextButton("Закрыть", on_click=lambda e: page.close(dlg))],
        )
        page.open(dlg)

    def build(self, page: ft.Page):
        """Построение основного интерфейса."""
        page.title = "⭐ AutoStars — Управление ботом"
        page.bgcolor = ft.Colors.BLACK
        page.window.width = 800
        page.window.height = 700
        page.padding = 0
        page.theme_mode = ft.ThemeMode.DARK

        # ═══════════════════ ЗАГОЛОВОК ═══════════════════ #
        self.status_icon = ft.Icon(ft.Icons.STOP_CIRCLE,
                                   color=ft.Colors.RED_400, size=20)
        self.status_text = ft.Text("🔴 Остановлен",
                                   color=ft.Colors.RED_400, size=14)

        header = ft.Container(
            content=ft.Row([
                ft.Text("⭐ AutoStars", size=24, weight=ft.FontWeight.BOLD,
                        color=ft.Colors.CYAN_400),
                ft.Row([
                    self.status_icon,
                    self.status_text,
                ]),
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            bgcolor=ft.Colors.GREY_900,
            padding=ft.padding.symmetric(horizontal=20, vertical=12),
        )

        # ═══════════════════ ВКЛАДКИ ═══════════════════ #
        tabs = ft.Tabs(
            expand=True,
            tabs=[
                self._build_management_tab(page),
                self._build_settings_tab(page),
                self._build_stats_tab(page),
                self._build_logs_tab(),
            ],
        )

        page.add(
            ft.Column([
                header,
                ft.Divider(height=1, color=ft.Colors.GREY_800),
                tabs,
            ], expand=True, spacing=0)
        )

    def _build_management_tab(self, page: ft.Page) -> ft.Tab:
        """Вкладка управления ботом."""
        return ft.Tab(
            text="🎮 Управление",
            content=ft.Container(
                content=ft.Column([
                    ft.Container(
                        content=ft.Column([
                            ft.Text("Управление ботом", size=18, weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.WHITE),
                            ft.Row([
                                ft.ElevatedButton(
                                    "▶ Запустить",
                                    on_click=self._start_bot,
                                    bgcolor=ft.Colors.GREEN_800,
                                    color=ft.Colors.WHITE,
                                    icon=ft.Icons.PLAY_ARROW,
                                ),
                                ft.ElevatedButton(
                                    "⏹ Остановить",
                                    on_click=self._stop_bot,
                                    bgcolor=ft.Colors.RED_800,
                                    color=ft.Colors.WHITE,
                                    icon=ft.Icons.STOP,
                                ),
                                ft.ElevatedButton(
                                    "🔍 Проверить API",
                                    on_click=self._check_connection,
                                    bgcolor=ft.Colors.BLUE_800,
                                    color=ft.Colors.WHITE,
                                    icon=ft.Icons.WIFI_FIND,
                                ),
                                ft.ElevatedButton(
                                    "🧮 Калькулятор",
                                    on_click=lambda e: self._calc_profit_dialog(e, page),
                                    bgcolor=ft.Colors.PURPLE_800,
                                    color=ft.Colors.WHITE,
                                    icon=ft.Icons.CALCULATE,
                                ),
                            ]),
                        ]),
                        bgcolor=ft.Colors.GREY_900,
                        border_radius=8,
                        padding=16,
                    ),
                ], expand=True),
                padding=16,
            ),
        )

    def _build_settings_tab(self, page: ft.Page) -> ft.Tab:
        """Вкладка настроек."""
        cfg = read_config()
        api = cfg.get("API", {})
        fp = cfg.get("FUNPAY", {})
        bot_cfg = cfg.get("BOT", {})
        settings = cfg.get("SETTINGS", {})
        finance = cfg.get("FINANCE", {})

        # Поля ввода
        fields = {}

        def make_field(label, key, default="", section="API"):
            val = api.get(key, fp.get(key, bot_cfg.get(key, settings.get(key, finance.get(key, default)))))
            field = ft.TextField(label=label, value=str(val), width=350, text_size=13,
                                 bgcolor=ft.Colors.GREY_800, color=ft.Colors.WHITE,
                                 border_color=ft.Colors.GREY_700)
            fields[key] = field
            return field

        # Hide sender switch (создаём отдельно, т.к. walrus не работает с атрибутами в списке)
        hide_switch = ft.Switch(
            label="Скрывать отправителя",
            value=finance.get("hide_sender", False),
            active_color=ft.Colors.CYAN_400,
        )
        fields["hide_sender"] = hide_switch

        return ft.Tab(
            text="⚙️ Настройки",
            content=ft.Container(
                content=ft.Column([
                    # API секция
                    ft.Text("🔑 API GAMEAU", size=16, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.CYAN_400),
                    make_field("API Token", "token", "", "API"),
                    make_field("API URL", "url", "https://gameau.us/api/v1", "API"),
                    make_field("Max Charge (USDT)", "max_charge_usdt", "9.50", "API"),
                    ft.Divider(color=ft.Colors.GREY_800),

                    # FunPay секция
                    ft.Text("🎮 FunPay", size=16, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.AMBER_400),
                    make_field("Golden Key", "golden_key", "", "FUNPAY"),
                    ft.Divider(color=ft.Colors.GREY_800),

                    # Bot секция
                    ft.Text("🤖 Бот", size=16, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.GREEN_400),
                    make_field("Bot Token (Telegram)", "bot_token", "", "BOT"),
                    make_field("Интервал опроса (сек)", "order_check_interval", "10", "SETTINGS"),
                    ft.Divider(color=ft.Colors.GREY_800),

                    # Финансы
                    ft.Text("💰 Финансы", size=16, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.PURPLE_400),
                    make_field("Закупка 1⭐ (USD)", "cost_per_star_usd", "0.017", "FINANCE"),
                    make_field("Продажа 1⭐ (₽)", "revenue_per_star_rub", "1.5", "FINANCE"),
                    make_field("Курс Variant 1 (₽/$)", "rate_variant_1", "110.0"),
                    make_field("Курс Variant 2 (₽/$)", "rate_variant_2", "87.63"),
                    hide_switch,
                    ft.Divider(color=ft.Colors.GREY_800),

                    # Кнопка сохранения
                    ft.ElevatedButton(
                        "💾 Сохранить настройки",
                        on_click=lambda e: self._save_settings(e, fields),
                        bgcolor=ft.Colors.CYAN_800,
                        color=ft.Colors.WHITE,
                        icon=ft.Icons.SAVE,
                    ),
                ], spacing=8, scroll=ft.ScrollMode.AUTO),
                padding=16,
            ),
        )

    def _build_stats_tab(self, page: ft.Page) -> ft.Tab:
        """Вкладка статистики."""
        self.stats_text = ft.Text("Нажмите 'Обновить' для загрузки", size=13,
                                   color=ft.Colors.WHITE, font_family="monospace")

        return ft.Tab(
            text="📊 Статистика",
            content=ft.Container(
                content=ft.Column([
                    ft.Row([
                        ft.ElevatedButton(
                            "🔄 Обновить",
                            on_click=self._refresh_stats,
                            bgcolor=ft.Colors.BLUE_800,
                            color=ft.Colors.WHITE,
                        ),
                    ]),
                    ft.Container(
                        content=self.stats_text,
                        bgcolor=ft.Colors.GREY_900,
                        border_radius=8,
                        padding=16,
                        expand=True,
                    ),
                ], expand=True),
                padding=16,
            ),
        )

    def _build_logs_tab(self) -> ft.Tab:
        """Вкладка логов."""
        self.log_list = ft.ListView(expand=True, spacing=2)
        return ft.Tab(
            text="📝 Логи",
            content=ft.Container(
                content=ft.Column([
                    ft.ElevatedButton(
                        "🗑 Очистить",
                        on_click=lambda e: setattr(self.log_list, "controls", []) or e.page.update(),
                        bgcolor=ft.Colors.GREY_800,
                        color=ft.Colors.WHITE,
                        icon=ft.Icons.DELETE_SWEEP,
                    ),
                    ft.Container(
                        content=self.log_list,
                        bgcolor=ft.Colors.GREY_900,
                        border_radius=8,
                        padding=12,
                        expand=True,
                    ),
                ], expand=True),
                padding=16,
            ),
        )


def main():
    def run_flet():
        ft.app(target=AutoStarsGUI().build)

    run_flet()


if __name__ == "__main__":
    main()
