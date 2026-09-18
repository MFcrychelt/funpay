"""AutoStars GUI (flet) — настольный интерфейс поверх движка `autostars`.

Открыто, но честно: GUI не «свой маленький бот». Он управляет тем же процессом,
что и консоль (`python -m autostars.main`), читает ту же SQLite-базу и пишет ту же
конфигурацию (.env). Кнопка «Запустить» = `--once`-не-режим, а живой цикл;
«Пауза» = `--pause` (флаг в БД, переживает рестарт); «Повторить заказ» =
`--retry-order`. Поэтому расхождений «в окне одно, в логах другое» не бывает.

Вкладка «Настройки» редактирует .env (с валидацией), «Диагностика» показывает
то, что видит `--check`, «Логи» — хвост файла лога и вывод дочернего процесса.

Зависимость от flet — опциональная: `pip install -e ".[gui]"`.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import bridge, settings

try:  # flet — опциональное зависимость (extra [gui])
    import flet as ft
except ImportError as exc:  # pragma: no cover - сообщение пользователю
    raise SystemExit(
        "Для GUI нужен flet:  pip install -e \".[gui]\"\n"
        f"(импорт flet завершился ошибкой: {exc})"
    ) from exc

APP_TITLE = "AutoStars — автовыдача Telegram Stars"
REFRESH_SECONDS = 2.5

COLORS = ft.Colors
ICONS = ft.Icons

THEME = {
    "bg": "#0B0F17",
    "surface": "#141B2A",
    "surface2": "#1D2739",
    "line": "#26314A",
    "text": "#E9EEF8",
    "muted": "#93A0B8",
    "primary": "#4FD1C5",
    "accent": "#63B3ED",
    "ok": "#48BB78",
    "warn": "#F6AD55",
    "err": "#F56565",
}

STATUS_LABELS = {
    "COMPLETED": ("выдано", THEME["ok"]),
    "PROCESSING": ("в работе", THEME["accent"]),
    "WAITING_USERNAME": ("ждём ник", THEME["warn"]),
    "FAILED": ("провал", THEME["err"]),
    "FAILED_LOW_BALANCE": ("нет баланса", THEME["err"]),
    "FAILED_PRICE_EXCEEDED": ("цена выше лимита", THEME["warn"]),
    "FAILED_DELIVERY": ("GAMEAU отклонил", THEME["err"]),
    "CANCELLED": ("отменён", THEME["muted"]),
    "HOLD_MANUAL": ("задержан политикой", THEME["warn"]),
}


def _nick(value: Any) -> str:
    """Ник покупателя для таблиц: в БД он без '@', в выгрузке может быть с '@'."""
    text = str(value or "").strip().lstrip("@")
    return f"@{text}" if text else "?"


def _icon(name: str) -> Any:
    """Иконка по имени с фоллбэком: enum flet меняется от версии к версии."""
    return getattr(ICONS, name, getattr(ICONS, "HELP", None))


def _mono(text: str, size: int = 12, color: str | None = None) -> ft.Text:
    return ft.Text(text, size=size, color=color or THEME["text"], font_family="Consolas")


class AutoStarsApp:
    """Состояние и виджеты одного экземпляра приложения."""

    def __init__(self, process: bridge.EngineProcess | None = None) -> None:
        self.process = process or bridge.EngineProcess()
        self.page: Any | None = None
        self.status_badge: ft.Text | None = None
        self.status_meta: ft.Text | None = None
        self.action_status: ft.Text | None = None
        self.cards: dict[str, ft.Text] = {}
        self.logs_view: ft.ListView | None = None
        self.orders_table: ft.DataTable | None = None
        self.stats_table: ft.DataTable | None = None
        self.diag_view: ft.Markdown | None = None
        self.settings_fields: dict[str, Any] = {}
        self.settings_error: ft.Text | None = None
        self.last_stats: dict[str, Any] | None = None
        self.selected_order: str | None = None
        # деньги с вкладки «Выдача» тратятся только вторым кликом по той же кнопке
        self._release_armed: str | None = None
        self._cancel_armed: str | None = None
        self.spend_tile: Any = None
        self.spend_hint: Any = None
        self.policy_tile: Any = None
        self.held_table: Any = None
        self.held_note: Any = None
        self.blacklist_input: Any = None
        self.blacklist_note_field: Any = None
        self.blacklist_view: Any = None
        self.export_period: Any = None
        self.export_note: Any = None

    # ------------------------------------------------------------------ #
    # утилиты UI
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str, color: str = THEME["muted"]) -> None:
        if self.action_status is not None:
            self.action_status.value = text
            self.action_status.color = color
            with contextlib.suppress(Exception):
                self.action_status.update()

    def _run_async(self, work: Callable[[], Any], on_done: Callable[[Any], None] | None = None) -> None:
        """Блокирующую работу (subprocess/SQLite) уводим из UI-потока."""
        if self.page is None:
            return

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                result = bridge.CommandResult(False, f"{type(exc).__name__}: {exc}")
            if on_done is not None:
                with contextlib.suppress(Exception):
                    self.page.run_thread(on_done, result)

        with contextlib.suppress(Exception):
            self.page.run_thread(runner)

    def _card(self, title: str, key: str, color: str = THEME["text"]) -> ft.Container:
        text = ft.Text("—", size=19, weight=ft.FontWeight.BOLD, color=color)
        self.cards[key] = text
        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(title.upper(), size=10.5, color=THEME["muted"], weight=ft.FontWeight.BOLD),
                    text,
                ],
                spacing=2,
            ),
            bgcolor=THEME["surface2"],
            border_radius=10,
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            expand=True,
        )

    def _panel(self, *controls: Any, title: str | None = None, expand: bool = False) -> ft.Container:
        content: list[Any] = []
        if title:
            content.append(ft.Text(title, size=13, weight=ft.FontWeight.BOLD, color=THEME["primary"]))
        content.extend(controls)
        return ft.Container(
            content=ft.Column(content, spacing=10),
            bgcolor=THEME["surface"],
            border=ft.Border.all(1, THEME["line"]),
            border_radius=12,
            padding=16,
            expand=expand,
        )

    def _button(self, label: str, handler: Callable[[Any], None], icon: Any = None, bg: str | None = None) -> ft.Button:
        return ft.Button(
            content=label,
            icon=icon,
            on_click=handler,
            bgcolor=bg or THEME["surface2"],
            color=THEME["text"],
            height=40,
        )

    # ------------------------------------------------------------------ #
    # вкладка «Главная»
    # ------------------------------------------------------------------ #
    def build_dashboard(self) -> ft.Container:
        self.status_badge = ft.Text("опрашиваю…", size=15, weight=ft.FontWeight.BOLD, color=THEME["muted"])
        self.status_meta = ft.Text("", size=12, color=THEME["muted"])
        self.action_status = ft.Text(
            "Готово к работе. Нажмите «Запустить цикл», чтобы начать выдачу.",
            size=12,
            color=THEME["muted"],
        )

        controls = ft.Row(
            [
                self._button("▶  Запустить цикл", self.on_start, _icon("PLAY_ARROW"), THEME["ok"]),
                self._button("⏹  Остановить", self.on_stop, _icon("STOP"), THEME["err"]),
                self._button("⏸  Пауза приёма", self.on_pause, _icon("PAUSE")),
                self._button("▶  Возобновить", self.on_resume, _icon("PLAY_CIRCLE")),
                self._button("🔍  Диагностика (--check)", self.on_check, _icon("WIFI_FIND")),
                self._button("💰  Баланс GAMEAU", self.on_balance, _icon("ACCOUNT_BALANCE")),
                self._button("📦  Каталог", self.on_catalog, _icon("STOREFRONT")),
            ],
            wrap=True,
            spacing=8,
            run_spacing=8,
        )

        info = ft.Container(
            content=ft.Row(
                [
                    ft.Column([self.status_badge, self.status_meta], spacing=2),
                ],
                alignment=ft.MainAxisAlignment.START,
            ),
            bgcolor=THEME["surface2"],
            border_radius=10,
            padding=14,
            expand=True,
        )

        cards = ft.Row(
            [
                self._card("заказов выдано", "completed", THEME["ok"]),
                self._card("в работе", "in_progress", THEME["accent"]),
                self._card("ждут @username", "waiting", THEME["warn"]),
                self._card("провалено", "failed", THEME["err"]),
            ],
            spacing=10,
            wrap=True,
        )

        paths = ft.Column(
            [
                _mono("конфигурация: не загружена"),
            ],
            spacing=2,
        )
        self.paths_view = paths

        return ft.Container(
            content=ft.Column(
                [
                    info,
                    cards,
                    self._panel(controls, self.action_status, title="Управление"),
                    self._panel(title="Пути и параметры"),
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            padding=16,
        )

    # ------------------------------------------------------------------ #
    # вкладка «Статистика»
    # ------------------------------------------------------------------ #
    def build_stats(self) -> ft.Container:
        self.stats_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("окно")),
                ft.DataColumn(ft.Text("заказы")),
                ft.DataColumn(ft.Text("выдано")),
                ft.DataColumn(ft.Text("провал")),
                ft.DataColumn(ft.Text("звёзды")),
                ft.DataColumn(ft.Text("выручка ₽")),
                ft.DataColumn(ft.Text("затраты ₽")),
                ft.DataColumn(ft.Text("прибыль ₽")),
                ft.DataColumn(ft.Text("маржа")),
            ],
            rows=[],
            heading_row_color=THEME["surface2"],
            border=ft.Border.all(1, THEME["line"]),
            border_radius=8,
        )
        self.pl_line = ft.Text("Нажмите «Обновить» — посчитаю по данным из базы.", size=12, color=THEME["muted"])

        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._button("🔄  Обновить статистику", self.on_stats, _icon("REFRESH")),
                            self._button("🧾  Полный отчёт (--report)", self.on_report, _icon("RECEIPT_LONG")),
                        ]
                    ),
                    self.pl_line,
                    ft.Column([self.stats_table], scroll=ft.ScrollMode.AUTO, expand=True),
                    ft.Container(
                        content=ft.Column([ft.Text("Отчёт движка", size=13, color=THEME["primary"]),
                                           ft.Text("", size=12, selectable=True, color=THEME["text"])],
                                          spacing=6),
                        bgcolor=THEME["surface"],
                        border_radius=10,
                        padding=14,
                        visible=False,
                    ),
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            padding=16,
        )

    # ------------------------------------------------------------------ #
    # вкладка «Заказы»
    # ------------------------------------------------------------------ #
    def build_orders(self) -> ft.Container:
        self.orders_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("заказ")),
                ft.DataColumn(ft.Text("получатель")),
                ft.DataColumn(ft.Text("⭐")),
                ft.DataColumn(ft.Text("₽ выручка")),
                ft.DataColumn(ft.Text("USDT")),
                ft.DataColumn(ft.Text("прибыль ₽")),
                ft.DataColumn(ft.Text("статус")),
                ft.DataColumn(ft.Text("действия")),
            ],
            rows=[],
            heading_row_color=THEME["surface2"],
            border=ft.Border.all(1, THEME["line"]),
            border_radius=8,
            column_spacing=16,
        )
        self.orders_note = ft.Text("", size=12, color=THEME["muted"])
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._button("🔄  Обновить", self.on_orders, _icon("REFRESH")),
                            self._button("🧭  Зависшие задачи (--tasks)", self.on_tasks, _icon("MAP")),
                        ]
                    ),
                    self.orders_note,
                    ft.Column([self.orders_table], scroll=ft.ScrollMode.AUTO, expand=True),
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            padding=16,
        )

    # ------------------------------------------------------------------ #
    # вкладка «Выдача»: политика, задержанные заказы, стоп-лист, выгрузка
    # ------------------------------------------------------------------ #
    def build_delivery(self) -> ft.Container:
        self.spend_tile = ft.Text("—", size=17, weight=ft.FontWeight.BOLD)
        self.spend_hint = ft.Text("", size=11.5, color=THEME["muted"])
        self.policy_tile = ft.Text("—", size=12.5, color=THEME["text"])
        self.held_note = ft.Text("", size=12, color=THEME["muted"])
        self.held_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("заказ")),
                ft.DataColumn(ft.Text("получатель")),
                ft.DataColumn(ft.Text("⭐")),
                ft.DataColumn(ft.Text("₽ выручка")),
                ft.DataColumn(ft.Text("почему держим")),
                ft.DataColumn(ft.Text("действия")),
            ],
            rows=[],
            heading_row_color=THEME["surface2"],
            border=ft.Border.all(1, THEME["line"]),
            border_radius=8,
            column_spacing=16,
        )
        self.blacklist_input = ft.TextField(label="@ник покупателя", width=220, text_size=13, dense=True,
                                            border_radius=8)
        self.blacklist_note_field = ft.TextField(label="причина (необязательно)", width=260, text_size=13,
                                                 dense=True, border_radius=8)
        self.blacklist_view = ft.ListView(height=110, spacing=2)
        self.export_period = ft.Dropdown(
            label="период",
            width=150,
            value="7d",
            options=[ft.dropdown.Option(key=k, text=t) for k, t in
                     (("all", "всё"), ("1d", "сутки"), ("7d", "неделя"), ("30d", "30 дней"))],
            dense=True,
            text_size=13,
        )
        self.export_note = ft.Text(
            "CSV в кодировке utf-8-sig — открывается в Excel без «кракозябр». "
            "Файл пишется в папку рядом с базой.",
            size=11.5,
            color=THEME["muted"],
        )

        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._button("🔄  Обновить", self.on_delivery, _icon("REFRESH")),
                            self._button("🛡  Что проверяет политика (--limits)", self.on_limits, _icon("SHIELD")),
                        ]
                    ),
                    self._panel(
                        ft.Row(
                            [
                                ft.Column(
                                    [ft.Text("ПОТРАЧЕНО С ПОЛУНОЧИ", size=10.5, color=THEME["muted"],
                                            weight=ft.FontWeight.BOLD), self.spend_tile, self.spend_hint],
                                    spacing=2,
                                ),
                                ft.VerticalDivider(width=1, color=THEME["line"]),
                                ft.Column([self.policy_tile], spacing=2, expand=True),
                            ],
                            spacing=18,
                        ),
                        title="Политика выдачи",
                    ),
                    self._panel(
                        self.held_note,
                        ft.Column([self.held_table], scroll=ft.ScrollMode.AUTO),
                        title="⛔ Задержанные заказы",
                    ),
                    self._panel(
                        ft.Row([self.blacklist_input, self.blacklist_note_field]),
                        ft.Row(
                            [
                                self._button("🚫  В стоп-лист", self.on_blacklist_add, _icon("BLOCK")),
                                self._button("✅  Убрать из списка", self.on_blacklist_remove, _icon("CHECK")),
                            ]
                        ),
                        self.blacklist_view,
                        title="Чёрный список покупателей",
                    ),
                    self._panel(
                        ft.Row(
                            [
                                self.export_period,
                                self._button("⬇  Выгрузить CSV", self.on_export, _icon("DOWNLOAD")),
                                self._button("⬇  Только проблемные", self.on_export_failed, _icon("WARNING")),
                            ]
                        ),
                        self.export_note,
                        title="Выгрузка истории",
                    ),
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            padding=16,
        )

    def on_delivery(self, _e: Any = None) -> None:
        """Снимок по политике: сколько потрачено, что задержано, кто в стоп-листе."""

        def worker() -> dict[str, Any]:
            cfg = bridge.load_config()
            return {
                "held": bridge.read_held_orders(cfg.db_path, limit=20),
                "spent": bridge.read_spend_today_usdt(cfg.db_path),
                "limit": float(cfg.daily_spend_limit_usdt or 0),
                "flags": bridge.read_buyer_flags(cfg.db_path),
                "max_order": float(cfg.max_order_revenue_rub or 0),
                "min_margin": float(cfg.min_margin_pct or 0),
                "duplicates": int(cfg.duplicate_window_min or 0),
                "duplicate_action": str(cfg.duplicate_action or "alert"),
                "blacklist": bool(cfg.blacklist_enabled),
                "mute": str(cfg.mute_hours or ""),
            }

        self._run_async(worker, self._render_delivery)

    def _render_delivery(self, data: dict[str, Any]) -> None:
        spent, limit = data.get("spent"), float(data.get("limit") or 0)
        if self.spend_tile is not None:
            if spent is None:
                self.spend_tile.value = "нет базы"
                self.spend_hint.value = "движок ещё ничего не писал в SQLite"
            else:
                self.spend_tile.value = f"{spent:.2f} USDT"
                if limit > 0:
                    over = spent > limit
                    self.spend_tile.color = THEME["err"] if over else THEME["ok"]
                    self.spend_hint.value = (
                        f"лимит {limit:.2f} · {'превышен — новые сделки держим' if over else f'осталось {max(0.0, limit - spent):.2f}'}"
                    )
                else:
                    self.spend_tile.color = THEME["text"]
                    self.spend_hint.value = "суточный лимит выключен (DAILY_SPEND_LIMIT_USDT=0)"
        if self.policy_tile is not None:
            bits = [
                f"сделка ≤ {data['max_order']:.0f} ₽" if data["max_order"] > 0 else "лимит сделки выключен",
                f"маржа ≥ {data['min_margin']:.1f}%" if data["min_margin"] > 0 else "маржа не проверяется",
                f"дубли {data['duplicates']} мин → {data['duplicate_action']}" if data["duplicates"] > 0
                else "дубли не ищем",
                f"стоп-лист {'вкл' if data['blacklist'] else 'выкл'}",
            ]
            if data.get("mute"):
                bits.append(f"тихие часы {data['mute']}")
            self.policy_tile.value = "\n".join(bits)
        if self.held_table is not None:
            rows: list[ft.DataRow] = []
            for item in data.get("held") or []:
                order_id = str(item.get("order_id") or "")
                rows.append(
                    ft.DataRow(
                        cells=[
                            ft.DataCell(ft.Text(order_id, size=12, selectable=True)),
                            ft.DataCell(ft.Text(_nick(item.get("username")), size=12)),
                            ft.DataCell(ft.Text(str(item.get("quantity") or 0), size=12)),
                            ft.DataCell(ft.Text(f"{float(item.get('price_rub') or 0):.2f}", size=12)),
                            ft.DataCell(ft.Text(str(item.get("error") or "")[:90], size=11.5,
                                                 color=THEME["warn"])),
                            ft.DataCell(
                                ft.Row(
                                    [
                                        ft.IconButton(
                                            _icon("PLAY_CIRCLE"),
                                            tooltip="отпустить и выдать (купит звёзды!)",
                                            on_click=lambda _e, oid=order_id: self.release_order(oid),
                                        ),
                                        ft.IconButton(
                                            _icon("CLOSE"),
                                            tooltip="отклонить заказ (деньги не списывались)",
                                            on_click=lambda _e, oid=order_id: self.cancel_order(oid),
                                        ),
                                    ],
                                    spacing=0,
                                )
                            ),
                        ]
                    )
                )
            self.held_table.rows = rows
            held_count = len(rows)
            self.held_note.value = (
                "Заказов ждёт решения: "
                + (f"{held_count}. ▶ — выдать (идемпотентно, дубль покупки невозможен), ✖ — отклонить."
                   if held_count else "нет 🎉")
            )
        if self.blacklist_view is not None:
            self.blacklist_view.controls.clear()
            flags = data.get("flags") or []
            if not flags:
                self.blacklist_view.controls.append(
                    ft.Text("стоп-лист пуст", size=12, color=THEME["muted"])
                )
            for row in flags:
                self.blacklist_view.controls.append(
                    ft.Text(
                        f"{row.get('username')}  •  {row.get('note') or 'без причины'}",
                        size=12,
                    )
                )
        with contextlib.suppress(Exception):
            if self.page is not None:
                self.page.update()

    def on_limits(self, _e: Any = None) -> None:
        """Вывод `--limits` в «Диагностику» — там же, где остальной вывод движка."""
        self._run_cli_button(("--limits", "политика выдачи", THEME["accent"]), into_diag=True)

    def release_order(self, order_id: str) -> None:
        """Второй клик = подтверждение: трата денег с вкладки не должна быть случайной."""
        if self._release_armed != order_id:
            self._release_armed = order_id
            self._set_status(f"нажмите ещё раз для #{order_id} — будут куплены звёзды", THEME["warn"])
            return
        self._release_armed = None
        self._set_status(f"отпускаю заказ #{order_id}…", THEME["accent"])

        def worker() -> bridge.CommandResult:
            return bridge.run_cli("--release", order_id, "-y", timeout=240)

        def done(result: bridge.CommandResult) -> None:
            self._show_diag(result.output or "(нет вывода)")
            self._set_status(
                ("✅ " if result.ok else "⚠️ ") + f"заказ #{order_id} ({result.returncode})",
                THEME["ok"] if result.ok else THEME["err"],
            )
            self.on_delivery()
            self.on_orders()

        self._run_async(worker, done)

    def cancel_order(self, order_id: str) -> None:
        if self._cancel_armed != order_id:
            self._cancel_armed = order_id
            self._set_status(f"нажмите ещё раз, чтобы отклонить #{order_id}", THEME["warn"])
            return
        self._cancel_armed = None

        def worker() -> bridge.CommandResult:
            return bridge.run_cli("--cancel", order_id, "-y", timeout=120)

        def done(result: bridge.CommandResult) -> None:
            self._show_diag(result.output or "(нет вывода)")
            self._set_status("отклонено" if result.ok else "не отклонено", THEME["ok"] if result.ok else THEME["err"])
            self.on_delivery()
            self.on_orders()

        self._run_async(worker, done)

    def on_blacklist_add(self, _e: Any = None) -> None:
        nick = (self.blacklist_input.value or "").strip()
        if not nick:
            self._set_status("укажите @ник покупателя", THEME["warn"])
            return
        note = (self.blacklist_note_field.value or "").strip()
        args = ["--blacklist", "add", nick] + ([note] if note else [])

        def worker() -> bridge.CommandResult:
            return bridge.run_cli(*args, timeout=90)

        self._run_async(worker, lambda result: (self._show_diag(result.output), self.on_delivery()))
        self._set_status(f"добавляю @{nick.lstrip('@')} в стоп-лист…", THEME["accent"])

    def on_blacklist_remove(self, _e: Any = None) -> None:
        nick = (self.blacklist_input.value or "").strip()
        if not nick:
            self._set_status("укажите @ник покупателя", THEME["warn"])
            return

        def worker() -> bridge.CommandResult:
            return bridge.run_cli("--blacklist", "remove", nick, timeout=90)

        self._run_async(worker, lambda result: (self._show_diag(result.output), self.on_delivery()))
        self._set_status(f"убираю @{nick.lstrip('@')} из стоп-листа…", THEME["accent"])

    def on_export(self, _e: Any = None) -> None:
        self._export(failed=False)

    def on_export_failed(self, _e: Any = None) -> None:
        self._export(failed=True)

    def _export(self, *, failed: bool) -> None:
        period = str(self.export_period.value or "all")

        def worker() -> bridge.CommandResult:
            cfg = bridge.load_config()
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            name = f"autostars-{'failed' if failed else 'orders'}-{stamp}.csv"
            target = Path(cfg.db_path).resolve().parent / name
            args = ["--export", str(target)]
            if period and period != "all":
                args += ["--since", period]
            if failed:
                args.append("--failed")
            return bridge.run_cli(*args, timeout=180)

        def done(result: bridge.CommandResult) -> None:
            self._show_diag(result.output or "(нет вывода)")
            self.export_note.value = (result.output or "").strip().splitlines()[0] if result.output else "—"
            self._set_status("выгрузка готова" if result.ok else "выгрузка не удалась",
                             THEME["ok"] if result.ok else THEME["err"])
            with contextlib.suppress(Exception):
                self.export_note.update()

        self._run_async(worker, done)

    # ------------------------------------------------------------------ #
    # вкладка «Настройки»
    # ------------------------------------------------------------------ #
    def build_settings(self) -> ft.Container:
        self.settings_fields = {}
        self.settings_error = ft.Text("", size=12, color=THEME["err"])
        values = settings.read_settings()

        blocks: list[Any] = []
        for section, fields in settings.FIELDS_BY_SECTION.items():
            rows: list[Any] = []
            for field in fields:
                widget = self._setting_widget(field, values)
                self.settings_fields[field.key] = (field, widget)
                rows.append(widget)
            blocks.append(
                ft.ExpansionTile(
                    title=ft.Text(section, size=13, weight=ft.FontWeight.BOLD, color=THEME["primary"]),
                    expanded=section in ("FunPay", "GAMEAU", "Финансы"),
                    controls_padding=12,
                    controls=[ft.Column(rows, spacing=10)],
                    bgcolor=THEME["surface"],
                    collapsed_icon_color=THEME["muted"],
                    icon_color=THEME["primary"],
                    shape=ft.RoundedRectangleBorder(radius=12),
                )
            )

        target = settings.target_env_file()
        buttons = ft.Row(
            [
                self._button("💾  Сохранить в .env", self.on_save_settings, _icon("SAVE"), THEME["ok"]),
                self._button("↺  Перечитать", self.on_reload_settings, _icon("RESTORE")),
            ],
            spacing=8,
        )

        return ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        f"Правки пишутся в {target}. Пустые поля секретов означают «не изменять». "
                        "После смены ключей перезапустите цикл.",
                        size=12,
                        color=THEME["muted"],
                    ),
                    *blocks,
                    self.settings_error,
                    buttons,
                ],
                spacing=10,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            ),
            padding=16,
        )

    def _setting_widget(self, field: settings.SettingField, values: dict[str, str]) -> Any:
        value = settings.display_value(field, values)
        label = field.label + (f" — {field.help}" if field.help else "")
        if field.kind == "bool":
            return ft.Switch(
                label=label,
                value=value == "да",
                active_color=THEME["primary"],
                label_text_style=ft.TextStyle(color=THEME["text"], size=13),
            )
        if field.kind == "choice":
            return ft.Dropdown(
                label=field.label,
                value=value or (field.choices[0] if field.choices else ""),
                options=[ft.dropdown.Option(key=c, text=c) for c in field.choices],
                width=320,
                text_size=13,
                dense=True,
            )
        if field.kind == "longtext":
            return ft.TextField(
                label=field.label,
                value=value,
                multiline=True,
                min_lines=2,
                max_lines=8,
                shift_enter=True,
                helper=ft.Text(field.help, size=11, color=THEME["muted"]) if field.help else None,
                text_size=13,
                dense=True,
                border_radius=8,
                width=560,
            )
        return ft.TextField(
            label=field.label,
            value=value,
            password=field.kind == "secret",
            can_reveal_password=field.kind == "secret",
            helper=ft.Text(field.help, size=11, color=THEME["muted"]) if field.help else None,
            text_size=13,
            dense=True,
            border_radius=8,
            width=460 if field.kind != "secret" else 520,
            keyboard_type=(
                ft.KeyboardType.NUMBER if field.kind in ("number", "int") else ft.KeyboardType.TEXT
            ),
        )

    # ------------------------------------------------------------------ #
    # вкладка «Логи»
    # ------------------------------------------------------------------ #
    def build_logs(self) -> ft.Container:
        self.logs_view = ft.ListView(spacing=1, expand=True, auto_scroll=True)
        self.auto_logs = ft.Switch(
            label="Автообновление каждые 3 с", value=True, active_color=THEME["primary"],
            label_text_style=ft.TextStyle(color=THEME["text"], size=12),
        )
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._button("🔄  Обновить", self.on_logs, _icon("REFRESH")),
                            self._button("🧹  Очистить вид", lambda e: self._render_logs([]), _icon("CLEANING_SERVICES")),
                            self.auto_logs,
                        ],
                        spacing=10,
                    ),
                    ft.Container(content=self.logs_view, expand=True, border_radius=10, padding=8,
                                 bgcolor=THEME["surface"]),
                ],
                spacing=12,
                expand=True,
            ),
            padding=16,
        )

    # ------------------------------------------------------------------ #
    # вкладка «Диагностика»
    # ------------------------------------------------------------------ #
    def build_diag(self) -> ft.Container:
        self.diag_view = ft.Markdown("", selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB)
        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            self._button("🩺  Проверить окружение", self.on_env_check, _icon("BOLT")),
                            self._button("🔍  --check движка", self.on_check, _icon("SEARCH")),
                        ]
                    ),
                    ft.Column([self.diag_view], scroll=ft.ScrollMode.AUTO, expand=True),
                ],
                spacing=12,
                expand=True,
            ),
            padding=16,
        )

    # ------------------------------------------------------------------ #
    # обработчики
    # ------------------------------------------------------------------ #
    def on_start(self, _e: Any = None) -> None:
        self._set_status("запускаю цикл автовыдачи…", THEME["accent"])
        result = self.process.start()
        self._set_status(("✅ " if result.ok else "❌ ") + result.output, THEME["ok"] if result.ok else THEME["err"])
        self.refresh_status()
        self.on_logs()

    def on_stop(self, _e: Any = None) -> None:
        self._set_status("останавливаю корректно (жду текущие выдачи)…", THEME["warn"])

        def worker() -> bridge.CommandResult:
            return self.process.stop(timeout=60)

        def done(result: bridge.CommandResult) -> None:
            self._set_status(("✅ " if result.ok else "❌ ") + result.output, THEME["ok"] if result.ok else THEME["err"])
            self.refresh_status()

        self._run_async(worker, done)

    def on_pause(self, _e: Any = None) -> None:
        self._run_cli_button(("--pause", "⏸ приём новых заказов остановлен", THEME["warn"]))

    def on_resume(self, _e: Any = None) -> None:
        self._run_cli_button(("--resume", "🟢 приём новых заказов возобновлён", THEME["ok"]))

    def on_check(self, _e: Any = None) -> None:
        self._run_cli_button(("--check", "🔍 диагностика завершена", THEME["accent"]), into_diag=True)

    def on_balance(self, _e: Any = None) -> None:
        self._run_cli_button(("--balance", "💰 баланс обновлён", THEME["accent"]), into_diag=True)

    def on_catalog(self, _e: Any = None) -> None:
        self._run_cli_button(("--catalog", "📦 каталог GAMEAU загружен", THEME["accent"]), into_diag=True)

    def on_report(self, _e: Any = None) -> None:
        self._run_cli_button(("--report", "🧾 отчёт готов", THEME["accent"]), into_diag=True)

    def on_tasks(self, _e: Any = None) -> None:
        self._run_cli_button(("--tasks", "🧭 список задач готов", THEME["accent"]), into_diag=True)

    def on_env_check(self, _e: Any = None) -> None:
        def worker() -> bridge.CommandResult:
            if bridge.is_frozen():
                return bridge.CommandResult(True, "Сборка exe: check_env.py не требуется (все зависимости внутри).")
            script = bridge.ROOT / "check_env.py"
            if not script.exists():
                return bridge.CommandResult(False, f"check_env.py не найден рядом с проектом: {script}")
            proc = __import__("subprocess").run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(bridge.ROOT),
            )
            return bridge.CommandResult(proc.returncode == 0, (proc.stdout + proc.stderr).strip(), ("check_env",))

        def done(result: bridge.CommandResult) -> None:
            self._show_diag(result.output or "(пусто)")
            self._set_status(("✅ " if result.ok else "❌ ") + "проверка окружения завершена",
                             THEME["ok"] if result.ok else THEME["err"])

        self._run_async(worker, done)

    def _run_cli_button(self, spec: tuple[str, str, str], into_diag: bool = False) -> None:
        flag, message, color = spec

        def worker() -> bridge.CommandResult:
            return bridge.run_cli(flag, timeout=120)

        def done(result: bridge.CommandResult) -> None:
            self._set_status(f"{message} ({'код 0' if result.ok else 'код ' + str(result.returncode)})", color)
            if into_diag:
                self._show_diag(result.output or "(нет вывода)")
            else:
                self.refresh_status()

        self._run_async(worker, done)

    def _show_diag(self, text: str) -> None:
        if self.diag_view is not None:
            self.diag_view.value = f"```\n{text[:20000]}\n```"
            with contextlib.suppress(Exception):
                self.diag_view.update()

    def on_stats(self, _e: Any = None) -> None:
        def worker() -> dict[str, Any] | None:
            return bridge.stats_json()

        def done(payload: dict[str, Any] | None) -> None:
            if not payload:
                self._set_status("не удалось получить статистику (база пуста или движок не установлен)", THEME["warn"])
                return
            self.last_stats = payload
            self._render_stats(payload)
            self._set_status("статистика обновлена", THEME["ok"])

        self._run_async(worker, done)

    def _render_stats(self, payload: dict[str, Any]) -> None:
        if self.stats_table is None:
            return
        rows = []
        for window in payload.get("windows", []):
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(window.get("label", ""), size=12)),
                        ft.DataCell(ft.Text(str(window.get("orders_total", 0)), size=12)),
                        ft.DataCell(ft.Text(str(window.get("orders_completed", 0)), size=12, color=THEME["ok"])),
                        ft.DataCell(ft.Text(str(window.get("orders_failed", 0)), size=12, color=THEME["err"])),
                        ft.DataCell(ft.Text(f"{window.get('stars', 0):,}".replace(",", " "), size=12)),
                        ft.DataCell(ft.Text(f"{window.get('revenue_rub', 0):,.2f}".replace(",", " "), size=12)),
                        ft.DataCell(ft.Text(f"{window.get('cost_rub', 0):,.2f}".replace(",", " "), size=12)),
                        ft.DataCell(
                            ft.Text(
                                f"{window.get('profit_rub', 0):,.2f}".replace(",", " "),
                                size=12,
                                color=THEME["ok"] if window.get("profit_rub", 0) >= 0 else THEME["err"],
                            )
                        ),
                        ft.DataCell(ft.Text(f"{window.get('margin_pct', 0)}%", size=12)),
                    ]
                )
            )
        self.stats_table.rows = rows
        total = next((w for w in payload.get("windows", []) if w.get("label") == "Всего"), None)
        if total:
            self.pl_line.value = (
                f"Всего: {total.get('orders_completed', 0)} сделок на {total.get('stars', 0)}⭐, "
                f"прибыль {total.get('profit_rub', 0):.2f} ₽ при курсе "
                f"{payload.get('active_rate', 0)} ₽/USDT (вариант {payload.get('active_variant')}). "
                f"Убыточные сделки: {total.get('loss_orders', 0)} на {total.get('loss_rub', 0):.2f} ₽."
            )
        with contextlib.suppress(Exception):
            self.stats_table.update()
            self.pl_line.update()

    def on_orders(self, _e: Any = None) -> None:
        def worker() -> list[dict[str, Any]]:
            cfg = bridge.load_config()
            return bridge.read_orders(cfg.db_path, limit=25)

        self._run_async(worker, self._render_orders)

    def _render_orders(self, orders: list[dict[str, Any]]) -> None:
        if self.orders_table is None:
            return
        rows: list[ft.DataRow] = []
        for order in orders:
            label, color = STATUS_LABELS.get(str(order.get("status")), (str(order.get("status") or "?"), THEME["muted"]))
            order_id = str(order.get("order_id") or "")
            rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(order_id, size=12, selectable=True)),
                        ft.DataCell(ft.Text(f"@{order.get('username') or '?'}", size=12)),
                        ft.DataCell(ft.Text(str(order.get("quantity") or 0), size=12)),
                        ft.DataCell(ft.Text(f"{float(order.get('price_rub') or 0):.2f}", size=12)),
                        ft.DataCell(ft.Text(f"{float(order.get('cost_usdt') or 0):.4f}", size=12)),
                        ft.DataCell(
                            ft.Text(
                                f"{float(order.get('profit_rub') or 0):.2f}",
                                size=12,
                                color=THEME["ok"] if float(order.get("profit_rub") or 0) >= 0 else THEME["err"],
                            )
                        ),
                        ft.DataCell(ft.Text(label, size=12, color=color)),
                        ft.DataCell(
                            ft.Row(
                                [
                                    ft.IconButton(
                                        _icon("TIMELINE"),
                                        tooltip="журнал задачи (--timeline)",
                                        on_click=lambda _e, oid=order_id: self.show_timeline(oid),
                                    ),
                                    ft.IconButton(
                                        _icon("PLAY_CIRCLE"),
                                        tooltip="повторить (--retry-order)",
                                        on_click=lambda _e, oid=order_id: self.retry_order(oid),
                                    ),
                                ],
                                spacing=0,
                            )
                        ),
                    ]
                )
            )
        self.orders_table.rows = rows
        self.orders_note.value = (
            f"Показано {len(rows)} записей из {Path(bridge.load_config().db_path).name}. "
            "⏱ — журнал задачи, ▶ — повтор проваленного заказа (идемпотентно)."
        )
        with contextlib.suppress(Exception):
            self.orders_table.update()
            self.orders_note.update()

    def show_timeline(self, order_id: str) -> None:
        def worker() -> bridge.CommandResult:
            return bridge.run_cli("--timeline", order_id, timeout=60)

        self._run_async(worker, lambda result: self._show_diag(result.output or "(нет данных)"))
        self._set_status(f"журнал заказа #{order_id}", THEME["accent"])

    def retry_order(self, order_id: str) -> None:
        self._set_status(f"повторяю заказ #{order_id}…", THEME["warn"])

        def worker() -> bridge.CommandResult:
            return bridge.run_cli("--retry-order", order_id, timeout=180)

        def done(result: bridge.CommandResult) -> None:
            self._show_diag(result.output or "(нет вывода)")
            self._set_status(
                ("✅ " if result.ok else "⚠️ ") + f"ретрай #{order_id} завершён (код {result.returncode})",
                THEME["ok"] if result.ok else THEME["warn"],
            )
            self.on_orders()

        self._run_async(worker, done)

    def on_logs(self, _e: Any = None) -> None:
        def worker() -> list[str]:
            cfg = bridge.load_config()
            file_lines = bridge.read_log_tail(cfg.log_file, lines=250)
            proc_lines = self.process.drain_logs(limit=120)
            return file_lines + [f"[gui] {line}" for line in proc_lines]

        self._run_async(worker, self._render_logs)

    def _render_logs(self, lines: list[str]) -> None:
        if self.logs_view is None:
            return
        self.logs_view.controls.clear()
        for line in lines[-250:]:
            color = THEME["text"]
            if "[ERROR]" in line or "❌" in line:
                color = THEME["err"]
            elif "[WARNING]" in line or "⚠️" in line:
                color = THEME["warn"]
            elif "[OK]" in line or "✅" in line:
                color = THEME["ok"]
            self.logs_view.controls.append(ft.Text(line, size=11.5, color=color, font_family="Consolas", selectable=True))
        with contextlib.suppress(Exception):
            self.logs_view.update()

    def on_save_settings(self, _e: Any = None) -> None:
        collected = self._collect_settings()
        errors = settings.validate(collected)
        if errors:
            if self.settings_error is not None:
                self.settings_error.value = "исправьте: " + "; ".join(errors[:4])
                with contextlib.suppress(Exception):
                    self.settings_error.update()
            self._set_status("настройки не сохранены — есть ошибки валидации", THEME["err"])
            return

        def worker() -> dict[str, Any]:
            current = settings.read_settings()
            updates = settings.plan_updates(collected, current)
            if not updates:
                return {"path": str(settings.target_env_file()), "changed": 0}
            path, changed = settings.apply_updates(updates)
            return {"path": str(path), "changed": changed, "keys": sorted(updates)}

        def done(info: dict[str, Any]) -> None:
            if not info.get("changed"):
                self._set_status("изменений нет — .env уже содержит эти значения", THEME["muted"])
                return
            note = f"сохранено {info['changed']} ключ(ей) в {info['path']}"
            if self.process.running:
                note += ". Цикл запущен со старыми значениями — перезапустите, чтобы применилось."
            self._set_status("💾 " + note, THEME["ok"])
            self.refresh_status()

        self._run_async(worker, done)

    def on_reload_settings(self, _e: Any = None) -> None:
        values = settings.read_settings()
        for _key, (field, widget) in self.settings_fields.items():
            value = settings.display_value(field, values)
            try:
                if field.kind == "bool":
                    widget.value = value == "да"
                else:
                    widget.value = value
            except Exception:
                continue
        with contextlib.suppress(Exception):
            if self.page is not None:
                self.page.update()
        self._set_status("значения перечитаны из .env", THEME["muted"])

    def _collect_settings(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, (field, widget) in self.settings_fields.items():
            value = getattr(widget, "value", None)
            if field.kind == "bool":
                value = "true" if value else "false"
            out[key] = "" if value is None else str(value)
        return out

    # ------------------------------------------------------------------ #
    # автообновление статуса
    # ------------------------------------------------------------------ #
    def refresh_status(self) -> None:
        snapshot = bridge.status_snapshot(self.process)
        running = snapshot["running"]
        if self.status_badge is not None:
            if running and snapshot["paused"]:
                self.status_badge.value = "⏸ в паузе (цикл работает)"
                self.status_badge.color = THEME["warn"]
            elif running:
                self.status_badge.value = "🟢 цикл работает"
                self.status_badge.color = THEME["ok"]
            else:
                self.status_badge.value = "🔴 цикл остановлен"
                self.status_badge.color = THEME["err"]
        if self.status_meta is not None:
            counts = snapshot["status_counts"]
            bits = [f"pid {snapshot['pid']}" if running else "процесс не запущен"]
            if running:
                bits.append(f"аптайм {bridge.humanize_duration(snapshot['uptime_seconds'])}")
            elif snapshot["last_exit_code"] not in (None, 0):
                bits.append(f"последний код выхода {snapshot['last_exit_code']}")
            bits.append(f"заказов в базе: {sum(counts.values())}")
            bits.append(f"курс {snapshot['rate']:.2f} ₽/USDT · {snapshot['rate_label']}")
            if not snapshot["config_ok"]:
                bits.append("⚠️ не заданы обязательные ключи")
            self.status_meta.value = " · ".join(bits)

        counts = snapshot["status_counts"]
        mapping = {
            "completed": counts.get("COMPLETED", 0),
            "in_progress": counts.get("PROCESSING", 0),
            "waiting": counts.get("WAITING_USERNAME", 0),
            "failed": sum(v for k, v in counts.items() if k.startswith("FAILED")),
        }
        for key, value in mapping.items():
            widget = self.cards.get(key)
            if widget is not None:
                widget.value = str(value)

        if getattr(self, "paths_view", None) is not None:
            self.paths_view.controls.clear()
            for line in (
                f"база:      {snapshot['db_path']}",
                f"лог:       {snapshot['log_path']}",
                f"команда:   {' '.join(bridge.engine_command())}",
                f"режим:     {'exe (PyInstaller)' if bridge.is_frozen() else 'исходники'}",
            ):
                self.paths_view.controls.append(_mono(line, 11.5, THEME["muted"]))
            for issue in snapshot["issues"][:4]:
                self.paths_view.controls.append(_mono(f"⚠️  {issue}", 11.5, THEME["warn"]))
        with contextlib.suppress(Exception):
            if self.page is not None:
                self.page.update()

    async def watch(self) -> None:
        """Фоновый тикер: статус, логи, счётчики — без нажатия кнопок."""
        tick = 0
        while self.page is not None:
            try:
                await asyncio.sleep(REFRESH_SECONDS)
                self.refresh_status()
                tick += 1
                if tick % 2 == 0 and self.auto_logs is not None and self.auto_logs.value:
                    self.on_logs()
                if tick % 8 == 1:
                    self.on_orders()
                    self.on_stats()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    # ------------------------------------------------------------------ #
    # сборка окна
    # ------------------------------------------------------------------ #
    def build(self, page: ft.Page) -> None:  # pragma: no cover - UI-обвязка
        self.page = page
        page.title = APP_TITLE
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = ft.Theme(color_scheme_seed=THEME["primary"], canvas_color=THEME["bg"])
        with contextlib.suppress(Exception):
            page.window.width = 1240
            page.window.height = 840
            page.window.min_width = 900
            page.window.min_height = 640
            icon = Path(bridge.ROOT) / "ico.ico"
            if icon.exists():
                page.window.icon = str(icon)

        tab_names = ["Главная", "Статистика", "Заказы", "Выдача", "Настройки", "Логи", "Диагностика"]
        tab_builders = [
            self.build_dashboard,
            self.build_stats,
            self.build_orders,
            self.build_delivery,
            self.build_settings,
            self.build_logs,
            self.build_diag,
        ]
        tab_bar = ft.TabBar(
            scrollable=True,
            divider_color=THEME["line"],
            indicator_color=THEME["primary"],
            label_color=THEME["primary"],
            unselected_label_color=THEME["muted"],
            tabs=[ft.Tab(label=name) for name in tab_names],
        )
        tab_view = ft.TabBarView(expand=True, controls=[builder() for builder in tab_builders])
        tabs = ft.Tabs(selected_index=0, length=len(tab_names), expand=True, content=ft.Column([tab_bar, tab_view], expand=True))

        def on_tab_change(event: Any) -> None:
            tabs.selected_index = event.control.selected_index
            with contextlib.suppress(Exception):
                page.update()

        tabs.on_change = on_tab_change

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(_icon("STAR"), color=THEME["primary"], size=26),
                    ft.Text(APP_TITLE, size=17, weight=ft.FontWeight.BOLD, color=THEME["text"]),
                    ft.Container(expand=True),
                    ft.Text(f"AutoStars {version()}", size=11, color=THEME["muted"]),
                ],
                spacing=10,
            ),
            bgcolor=THEME["surface"],
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
        )

        page.add(ft.Container(content=ft.Column([header, tabs], spacing=0, expand=True), bgcolor=THEME["bg"], expand=True))

        self.refresh_status()
        self.on_orders()
        self.on_stats()
        with contextlib.suppress(Exception):
            page.run_thread(self.watch)

    def close(self) -> None:
        """Закрытие окна НЕ останавливает цикл: выдача остаётся работать.

        Если нужно остановить и цикл — кнопка «Остановить» (graceful shutdown)
        или /stop в Telegram.
        """
        return


def version() -> str:
    try:
        from autostars import __version__

        return __version__
    except Exception:
        return "?"


def main() -> None:
    """Точка входа: `python -m autostars.gui` (или autostars-gui после установки)."""
    app = AutoStarsApp()
    ft.run(app.build)


if __name__ == "__main__":  # pragma: no cover
    main()
