#!/usr/bin/env python3
"""GUI-панель управления FunPay Stars Bot.

Запуск:
    python gui.py                # обычная панель
    python gui.py --port 8080    # веб-панель на http://localhost:8080

Веб-панель (Flask) — независимый процесс, читает bot.log и state.json,
показывает статус, логи и заказы в реальном времени.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# --------------------------------------------------------------------------- #
# Утилиты чтения данных бота
# --------------------------------------------------------------------------- #

PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / os.environ.get("FUNPAY_STARS_STATE", "state.json")
LOG_FILE = PROJECT_DIR / "bot.log"
CONFIG_FILE = PROJECT_DIR / "config.json"
ENV_FILE = PROJECT_DIR / ".env"
PID_FILE = PROJECT_DIR / ".bot.pid"
DB_FILE = PROJECT_DIR / "db.json"


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_log_tail(lines: int = 200) -> list[str]:
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return all_lines[-lines:]
    except FileNotFoundError:
        return []


def _read_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_db() -> dict:
    try:
        return json.loads(DB_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"transactions": [], "stats": {}}


def _is_bot_running() -> bool:
    pid = _read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        PID_FILE.unlink(missing_ok=True)
        return False


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# .env / config.json чтение-запись
# --------------------------------------------------------------------------- #

def _read_env() -> dict[str, str]:
    """Читает .env в словарь KEY→VALUE."""
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _write_env(data: dict[str, str]) -> None:
    """Записывает .env, сохраняя комментарии."""
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    # Обновляем или добавляем ключи
    keys_written = set()
    new_lines: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in data:
                new_lines.append(f"{key}={data[key]}")
                keys_written.add(key)
                continue
        new_lines.append(raw)

    # Добавляем новые ключи
    for key, value in data.items():
        if key not in keys_written:
            new_lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _write_config(data: dict) -> None:
    CONFIG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _is_setup_done() -> bool:
    env = _read_env()
    return bool(env.get("FUNPAY_GOLDEN_KEY")) and bool(env.get("GAMEAU_API_KEY"))


# --------------------------------------------------------------------------- #
# Управление процессом бота
# --------------------------------------------------------------------------- #

_bot_process: subprocess.Popen | None = None


def start_bot() -> str:
    global _bot_process
    if _is_bot_running():
        return "⚠️ Бот уже запущен"
    if not _is_setup_done():
        return "❌ Сначала заполните ключи API (вкладка ⚙️ Настройки)"
    try:
        _bot_process = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=str(PROJECT_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        PID_FILE.write_text(str(_bot_process.pid))
        return f"✅ Бот запущен (PID {_bot_process.pid})"
    except Exception as exc:
        return f"❌ Ошибка запуска: {exc}"


def stop_bot() -> str:
    global _bot_process
    pid = _read_pid()
    if pid is None:
        return "ℹ️ Бот не запущен"
    try:
        if _bot_process and _bot_process.poll() is None:
            _bot_process.terminate()
            _bot_process.wait(timeout=10)
        else:
            os.kill(pid, 2)  # SIGINT
        PID_FILE.unlink(missing_ok=True)
        return "✅ Бот остановлен"
    except Exception as exc:
        return f"❌ Ошибка остановки: {exc}"
    finally:
        _bot_process = None


def run_check() -> str:
    try:
        result = subprocess.run(
            [sys.executable, "main.py", "--check"],
            cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout + result.stderr
    except Exception as exc:
        return f"❌ Ошибка: {exc}"


def run_once() -> str:
    try:
        result = subprocess.run(
            [sys.executable, "main.py", "--once"],
            cwd=str(PROJECT_DIR),
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout + result.stderr
    except Exception as exc:
        return f"❌ Ошибка: {exc}"


# --------------------------------------------------------------------------- #
# Tkinter GUI
# --------------------------------------------------------------------------- #

def run_tkinter_gui() -> None:
    try:
        import tkinter as tk
        from tkinter import scrolledtext, messagebox, ttk
    except ImportError:
        print("tkinter недоступен. Попробуйте: python gui.py --port 8080")
        sys.exit(1)

    root = tk.Tk()
    root.title("FunPay Stars Bot — Управление")
    root.geometry("950x750")
    root.minsize(750, 550)
    root.configure(bg="#1e1e2e")

    # ── Палитра ──
    C = {
        "bg": "#1e1e2e", "fg": "#cdd6f4", "dim": "#6c7086",
        "accent": "#89b4fa", "green": "#a6e3a1", "red": "#f38ba8",
        "yellow": "#f9e2af", "peach": "#fab387", "mauve": "#cba6f7",
        "surface": "#313244", "surface2": "#45475a", "mantle": "#181825",
        "base": "#11111b",
        "font": ("Consolas", 10), "font_sm": ("Segoe UI", 9),
        "font_md": ("Segoe UI", 10), "font_title": ("Segoe UI", 11, "bold"),
        "font_header": ("Segoe UI", 14, "bold"),
    }

    # ── Стиль ttk ──
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("TNotebook", background=C["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=C["surface"], foreground=C["fg"],
                     padding=[14, 6], font=("Segoe UI", 10))
    style.map("TNotebook.Tab",
              background=[("selected", C["surface2"])],
              foreground=[("selected", C["accent"])])
    style.configure("TFrame", background=C["bg"])
    style.configure("TLabel", background=C["bg"], foreground=C["fg"])
    style.configure("TEntry", fieldbackground=C["surface"], foreground=C["fg"],
                     insertcolor=C["fg"])
    style.configure("TCheckbutton", background=C["bg"], foreground=C["fg"])
    style.configure("TSpinbox", fieldbackground=C["surface"], foreground=C["fg"])
    style.configure("TButton", background=C["surface"], foreground=C["fg"])

    # ── Хелперы ──
    def labeled_entry(parent, label_text, default="", width=40, show="",
                      row=0, col=0, tooltip=""):
        lbl = tk.Label(parent, text=label_text, font=C["font_md"],
                       bg=C["bg"], fg=C["fg"], anchor="w")
        lbl.grid(row=row, column=col * 2, sticky="w", padx=(12, 4), pady=4)
        entry = tk.Entry(parent, width=width, font=C["font"],
                         bg=C["surface"], fg=C["fg"], insertbackground=C["fg"],
                         relief="flat", show=show)
        entry.grid(row=row, column=col * 2 + 1, sticky="ew", padx=(0, 12), pady=4)
        entry.insert(0, default)
        if tooltip:
            tip = tk.Label(parent, text=tooltip, font=C["font_sm"],
                           bg=C["bg"], fg=C["dim"], anchor="w")
            tip.grid(row=row + 1, column=col * 2, columnspan=2,
                     sticky="w", padx=(12, 0), pady=(0, 2))
            return entry, tip
        return entry

    def section_label(parent, text, row=0):
        lbl = tk.Label(parent, text=text, font=C["font_title"],
                       bg=C["bg"], fg=C["accent"], anchor="w")
        lbl.grid(row=row, column=0, columnspan=4, sticky="w",
                 padx=12, pady=(14, 4))

    # ══════════════════════════════════════════════════════════════════════ #
    # Верхняя панель статуса
    # ══════════════════════════════════════════════════════════════════════ #
    top = tk.Frame(root, bg=C["mantle"], padx=12, pady=8)
    top.pack(fill="x", padx=8, pady=(8, 4))

    status_label = tk.Label(top, text="⚫ Неизвестно", font=C["font_header"],
                            bg=C["mantle"], fg=C["yellow"])
    status_label.pack(side="left")

    balance_label = tk.Label(top, text="GAMEAU: —", font=C["font"],
                             bg=C["mantle"], fg=C["fg"])
    balance_label.pack(side="right", padx=(12, 0))

    # Кнопки управления
    btn_frame = tk.Frame(root, bg=C["bg"])
    btn_frame.pack(fill="x", padx=8, pady=4)

    def make_btn(parent, text, command, color=C["accent"]):
        b = tk.Button(parent, text=text, command=command, font=C["font_title"],
                      bg=color, fg="#1e1e1e", activebackground=color,
                      relief="flat", padx=14, pady=6, cursor="hand2")
        b.pack(side="left", padx=(0, 6))
        return b

    make_btn(btn_frame, "▶ Старт", start_bot, C["green"])
    make_btn(btn_frame, "⏹ Стоп", stop_bot, C["red"])
    make_btn(btn_frame, "🔍 Проверка", run_check, C["accent"])
    make_btn(btn_frame, "⚡ Once", run_once, C["yellow"])

    # ══════════════════════════════════════════════════════════════════════ #
    # Notebook
    # ══════════════════════════════════════════════════════════════════════ #
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=(4, 8))

    # ──────────────── Вкладка 1: Настройки (API ключи) ──────────────── #
    settings_frame = tk.Frame(notebook, bg=C["bg"])
    notebook.add(settings_frame, text=" 🔑 API Ключи ")

    # Canvas + Scrollbar для прокрутки
    settings_canvas = tk.Canvas(settings_frame, bg=C["bg"], highlightthickness=0)
    settings_scroll = ttk.Scrollbar(settings_frame, orient="vertical",
                                     command=settings_canvas.yview)
    settings_inner = tk.Frame(settings_canvas, bg=C["bg"])
    settings_inner.bind("<Configure>",
                         lambda e: settings_canvas.configure(
                             scrollregion=settings_canvas.bbox("all")))
    settings_canvas.create_window((0, 0), window=settings_inner, anchor="nw")
    settings_canvas.configure(yscrollcommand=settings_scroll.set)
    settings_scroll.pack(side="right", fill="y")
    settings_canvas.pack(side="left", fill="both", expand=True)

    # Прокрутка колёсиком (только над canvas, не глобально)
    def _on_mousewheel(event):
        settings_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    settings_canvas.bind("<MouseWheel>", _on_mousewheel)

    env = _read_env()

    section_label(settings_inner, "🔑 FunPay", row=0)
    fp_key_entry = labeled_entry(
        settings_inner, "Golden Key:", env.get("FUNPAY_GOLDEN_KEY", ""),
        width=55, row=1, col=0,
        tooltip="Куки → funpay.com → F12 → Application → Cookies → golden_key"
    )

    section_label(settings_inner, "🔑 GAMEAU API", row=3)
    ga_key_entry = labeled_entry(
        settings_inner, "API Key:", env.get("GAMEAU_API_KEY", ""),
        width=55, row=4, col=0,
        tooltip="gameau.us → Профиль → «API для интеграций» → «Выпустить ключ»"
    )
    ga_url_entry = labeled_entry(
        settings_inner, "Base URL:", env.get("GAMEAU_BASE_URL", "https://gameau.us/api/v1"),
        width=55, row=5, col=0,
        tooltip="Не меняйте, если не знаете зачем"
    )

    section_label(settings_inner, "👤 Telegram Уведомления (опционально)", row=7)
    tg_token_entry = labeled_entry(
        settings_inner, "Bot Token:", env.get("TELEGRAM_BOT_TOKEN", ""),
        width=55, row=8, col=0,
        tooltip="@BotFather → /newbot → скопируйте токен"
    )
    tg_chat_entry = labeled_entry(
        settings_inner, "Chat ID:", env.get("TELEGRAM_CHAT_ID", ""),
        width=55, row=9, col=0,
        tooltip="@userinfobot или @getmyid_bot → ваш chat_id"
    )

    # Кнопка сохранения ключей
    save_keys_frame = tk.Frame(settings_inner, bg=C["bg"])
    save_keys_frame.grid(row=11, column=0, columnspan=2, pady=(20, 8))

    save_status = tk.Label(save_keys_frame, text="", font=C["font_md"],
                           bg=C["bg"], fg=C["green"])

    def save_keys():
        data = {
            "FUNPAY_GOLDEN_KEY": fp_key_entry.get().strip(),
            "GAMEAU_API_KEY": ga_key_entry.get().strip(),
        }
        if ga_url_entry.get().strip():
            data["GAMEAU_BASE_URL"] = ga_url_entry.get().strip()
        if tg_token_entry.get().strip():
            data["TELEGRAM_BOT_TOKEN"] = tg_token_entry.get().strip()
        if tg_chat_entry.get().strip():
            data["TELEGRAM_CHAT_ID"] = tg_chat_entry.get().strip()
        _write_env(data)
        save_status.config(text="✅ Ключи сохранены в .env", fg=C["green"])
        root.after(3000, lambda: save_status.config(text=""))

    make_btn(save_keys_frame, "💾 Сохранить ключи", save_keys, C["green"])
    save_status.pack(side="left", padx=(12, 0))

    # ──────────────── Вкладка 2: Настройки поведения ──────────────── #
    behavior_frame = tk.Frame(notebook, bg=C["bg"])
    notebook.add(behavior_frame, text=" ⚙️ Поведение ")

    cfg = _read_config()

    b_canvas = tk.Canvas(behavior_frame, bg=C["bg"], highlightthickness=0)
    b_scroll = ttk.Scrollbar(behavior_frame, orient="vertical", command=b_canvas.yview)
    b_inner = tk.Frame(b_canvas, bg=C["bg"])
    b_inner.bind("<Configure>",
                  lambda e: b_canvas.configure(scrollregion=b_canvas.bbox("all")))
    b_canvas.create_window((0, 0), window=b_inner, anchor="nw")
    b_canvas.configure(yscrollcommand=b_scroll.set)
    b_scroll.pack(side="right", fill="y")
    b_canvas.pack(side="left", fill="both", expand=True)

    def _on_mousewheel2(event):
        b_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    b_canvas.bind("<MouseWheel>", _on_mousewheel2)

    row = 0
    section_label(b_inner, "⏱ Интервалы", row=row); row += 1

    poll_label = tk.Label(b_inner, text="Опрос заказов (сек):", font=C["font_md"],
                          bg=C["bg"], fg=C["fg"], anchor="w")
    poll_label.grid(row=row, column=0, sticky="w", padx=(12, 4), pady=4)
    poll_var = tk.StringVar(value=str(cfg.get("poll_interval", 15)))
    poll_spin = tk.Spinbox(b_inner, from_=5, to=300, width=10, textvariable=poll_var,
                            font=C["font"], bg=C["surface"], fg=C["fg"],
                            buttonbackground=C["surface2"], relief="flat")
    poll_spin.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
    row += 1

    session_label = tk.Label(b_inner, text="Обновление сессии (сек):", font=C["font_md"],
                             bg=C["bg"], fg=C["fg"], anchor="w")
    session_label.grid(row=row, column=0, sticky="w", padx=(12, 4), pady=4)
    session_var = tk.StringVar(value=str(cfg.get("session_refresh_interval", 2700)))
    session_spin = tk.Spinbox(b_inner, from_=300, to=7200, width=10,
                               textvariable=session_var, font=C["font"],
                               bg=C["surface"], fg=C["fg"],
                               buttonbackground=C["surface2"], relief="flat")
    session_spin.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
    row += 1

    section_label(b_inner, "⭐ Звёзды", row=row); row += 1

    default_label = tk.Label(b_inner, text="Звёзды по умолчанию:", font=C["font_md"],
                             bg=C["bg"], fg=C["fg"], anchor="w")
    default_label.grid(row=row, column=0, sticky="w", padx=(12, 4), pady=4)
    default_var = tk.StringVar(value=str(cfg.get("default_stars") or ""))
    default_spin = tk.Spinbox(b_inner, from_=0, to=10000, width=10,
                               textvariable=default_var, font=C["font"],
                               bg=C["surface"], fg=C["fg"],
                               buttonbackground=C["surface2"], relief="flat")
    default_spin.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
    default_hint = tk.Label(b_inner, text="Пусто = пропустить заказ если лот не распознан",
                            font=C["font_sm"], bg=C["bg"], fg=C["dim"], anchor="w")
    default_hint.grid(row=row, column=2, sticky="w", padx=(4, 0))
    row += 1

    max_charge_label = tk.Label(b_inner, text="Макс. множитель цены:", font=C["font_md"],
                                bg=C["bg"], fg=C["fg"], anchor="w")
    max_charge_label.grid(row=row, column=0, sticky="w", padx=(12, 4), pady=4)
    max_charge_var = tk.StringVar(value=str(cfg.get("gameau_max_charge_multiplier", 1.5)))
    max_charge_spin = tk.Spinbox(b_inner, from_=1.0, to=5.0, increment=0.1,
                                  width=10, textvariable=max_charge_var,
                                  font=C["font"], bg=C["surface"], fg=C["fg"],
                                  buttonbackground=C["surface2"], relief="flat")
    max_charge_spin.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
    row += 1

    section_label(b_inner, "💬 Поведение чата", row=row); row += 1

    ask_var = tk.BooleanVar(value=cfg.get("ask_username_in_chat", True))
    ask_cb = tk.Checkbutton(b_inner, text="Просить @username у покупателя",
                             variable=ask_var, font=C["font_md"],
                             bg=C["bg"], fg=C["fg"], selectcolor=C["surface"],
                             activebackground=C["bg"])
    ask_cb.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 0), pady=4)
    row += 1

    remind_var = tk.BooleanVar(value=cfg.get("remind_username", True))
    remind_cb = tk.Checkbutton(b_inner, text="Напоминать раз в час",
                                variable=remind_var, font=C["font_md"],
                                bg=C["bg"], fg=C["fg"], selectcolor=C["surface"],
                                activebackground=C["bg"])
    remind_cb.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 0), pady=4)
    row += 1

    reply_var = tk.BooleanVar(value=cfg.get("reply_on_delivery", True))
    reply_cb = tk.Checkbutton(b_inner, text="Отвечать покупателю после выдачи",
                               variable=reply_var, font=C["font_md"],
                               bg=C["bg"], fg=C["fg"], selectcolor=C["surface"],
                               activebackground=C["bg"])
    reply_cb.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 0), pady=4)
    row += 1

    refund_var = tk.BooleanVar(value=cfg.get("refund_on_failure", False))
    refund_cb = tk.Checkbutton(b_inner, text="Автовозврат при ошибке выдачи",
                                variable=refund_var, font=C["font_md"],
                                bg=C["bg"], fg=C["fg"], selectcolor=C["surface"],
                                activebackground=C["bg"])
    refund_cb.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 0), pady=4)
    row += 1

    section_label(b_inner, "🛡 Ограничения", row=row); row += 1

    low_bal_var = tk.BooleanVar(value=cfg.get("stop_on_low_balance", False))
    low_bal_cb = tk.Checkbutton(b_inner, text="Пауза при низком балансе GAMEAU",
                                 variable=low_bal_var, font=C["font_md"],
                                 bg=C["bg"], fg=C["fg"], selectcolor=C["surface"],
                                 activebackground=C["bg"])
    low_bal_cb.grid(row=row, column=0, columnspan=2, sticky="w", padx=(12, 0), pady=4)
    row += 1

    min_bal_label = tk.Label(b_inner, text="Мин. баланс:", font=C["font_md"],
                             bg=C["bg"], fg=C["fg"], anchor="w")
    min_bal_label.grid(row=row, column=0, sticky="w", padx=(12, 4), pady=4)
    min_bal_var = tk.StringVar(value=str(cfg.get("min_gameau_balance", 0)))
    min_bal_spin = tk.Spinbox(b_inner, from_=0, to=1000, width=10,
                               textvariable=min_bal_var, font=C["font"],
                               bg=C["surface"], fg=C["fg"],
                               buttonbackground=C["surface2"], relief="flat")
    min_bal_spin.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
    row += 1

    max_attempts_label = tk.Label(b_inner, text="Попыток создания заказа:", font=C["font_md"],
                                  bg=C["bg"], fg=C["fg"], anchor="w")
    max_attempts_label.grid(row=row, column=0, sticky="w", padx=(12, 4), pady=4)
    max_attempts_var = tk.StringVar(value=str(cfg.get("max_create_attempts", 3)))
    max_attempts_spin = tk.Spinbox(b_inner, from_=1, to=10, width=10,
                                    textvariable=max_attempts_var, font=C["font"],
                                    bg=C["surface"], fg=C["fg"],
                                    buttonbackground=C["surface2"], relief="flat")
    max_attempts_spin.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=4)
    row += 1

    # Кнопка сохранения
    save_cfg_frame = tk.Frame(b_inner, bg=C["bg"])
    save_cfg_frame.grid(row=row, column=0, columnspan=3, pady=(20, 8))
    save_cfg_status = tk.Label(save_cfg_frame, text="", font=C["font_md"],
                               bg=C["bg"], fg=C["green"])

    def save_behavior():
        new_cfg = {
            "poll_interval": float(poll_var.get()),
            "session_refresh_interval": float(session_var.get()),
            "default_stars": int(default_var.get()) if default_var.get().strip() else None,
            "gameau_max_charge_multiplier": float(max_charge_var.get()),
            "ask_username_in_chat": ask_var.get(),
            "remind_username": remind_var.get(),
            "reminder_interval": cfg.get("reminder_interval", 3600),
            "reply_on_delivery": reply_var.get(),
            "refund_on_failure": refund_var.get(),
            "stop_on_low_balance": low_bal_var.get(),
            "min_gameau_balance": float(min_bal_var.get()),
            "max_create_attempts": int(max_attempts_var.get()),
            "msg_need_username": cfg.get("msg_need_username", ""),
            "msg_username_reminder": cfg.get("msg_username_reminder", ""),
            "msg_stars_sent": cfg.get("msg_stars_sent", ""),
            "msg_error": cfg.get("msg_error", ""),
            "lot_rules": cfg.get("lot_rules", []),
            "username_wait_timeout": cfg.get("username_wait_timeout", 86400),
            "username_check_interval": cfg.get("username_check_interval", 30),
            "gameau_status_poll_interval": cfg.get("gameau_status_poll_interval", 5),
            "gameau_status_timeout": cfg.get("gameau_status_timeout", 600),
            "seller_notify_token": tg_token_entry.get().strip(),
            "seller_notify_chat_id": tg_chat_entry.get().strip(),
            "log_file": cfg.get("log_file", "bot.log"),
        }
        _write_config(new_cfg)
        save_cfg_status.config(text="✅ Настройки сохранены в config.json", fg=C["green"])
        root.after(3000, lambda: save_cfg_status.config(text=""))

    make_btn(save_cfg_frame, "💾 Сохранить настройки", save_behavior, C["accent"])
    save_cfg_status.pack(side="left", padx=(12, 0))

    # ──────────────── Вкладка 3: Логи ──────────────── #
    log_frame = tk.Frame(notebook, bg=C["bg"])
    notebook.add(log_frame, text=" 📜 Логи ")

    log_text = scrolledtext.ScrolledText(
        log_frame, wrap="word", font=C["font"],
        bg=C["base"], fg=C["fg"], insertbackground=C["fg"],
        relief="flat", selectbackground=C["accent"], height=20,
    )
    log_text.pack(fill="both", expand=True, padx=4, pady=4)
    log_text.tag_configure("ERROR", foreground=C["red"])
    log_text.tag_configure("WARNING", foreground=C["yellow"])
    log_text.tag_configure("INFO", foreground=C["green"])
    log_text.tag_configure("DEBUG", foreground=C["surface2"])

    # ──────────────── Вкладка 4: Заказы ──────────────── #
    orders_frame = tk.Frame(notebook, bg=C["bg"])
    notebook.add(orders_frame, text=" 📦 Заказы ")

    orders_columns = ("id", "status", "stars", "username", "price", "error")
    orders_tree = ttk.Treeview(
        orders_frame, columns=orders_columns, show="headings", height=15,
    )
    orders_tree.heading("id", text="ID")
    orders_tree.heading("status", text="Статус")
    orders_tree.heading("stars", text="⭐")
    orders_tree.heading("username", text="Username")
    orders_tree.heading("price", text="Цена $")
    orders_tree.heading("error", text="Ошибка")
    orders_tree.column("id", width=100)
    orders_tree.column("status", width=100)
    orders_tree.column("stars", width=50)
    orders_tree.column("username", width=120)
    orders_tree.column("price", width=70)
    orders_tree.column("error", width=200)

    scrollbar = ttk.Scrollbar(orders_frame, orient="vertical", command=orders_tree.yview)
    orders_tree.configure(yscrollcommand=scrollbar.set)
    orders_tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
    scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=4)

    # ──────────────── Вкладка 5: Lot Rules ──────────────── #
    rules_frame = tk.Frame(notebook, bg=C["bg"])
    notebook.add(rules_frame, text=" 🎯 Лоты ")

    r_canvas = tk.Canvas(rules_frame, bg=C["bg"], highlightthickness=0)
    r_scroll = ttk.Scrollbar(rules_frame, orient="vertical", command=r_canvas.yview)
    r_inner = tk.Frame(r_canvas, bg=C["bg"])
    r_inner.bind("<Configure>",
                  lambda e: r_canvas.configure(scrollregion=r_canvas.bbox("all")))
    r_canvas.create_window((0, 0), window=r_inner, anchor="nw")
    r_canvas.configure(yscrollcommand=r_scroll.set)
    r_scroll.pack(side="right", fill="y")
    r_canvas.pack(side="left", fill="both", expand=True)

    def _on_mousewheel3(event):
        r_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    r_canvas.bind("<MouseWheel>", _on_mousewheel3)

    section_label(r_inner, "🎯 Правила лотов: какой лот → сколько звёзд", row=0)

    rules_hint = tk.Label(
        r_inner,
        text="pattern — регулярное выражение, ищется в названии заказа. "
             "Проверяются по порядку, первое совпадение выигрывает.",
        font=C["font_sm"], bg=C["bg"], fg=C["dim"], anchor="w", wraplength=700,
    )
    rules_hint.grid(row=1, column=0, columnspan=6, sticky="w", padx=12, pady=(0, 8))

    rule_entries: list[tuple[tk.Entry, tk.Entry]] = []

    def add_rule_row(parent, pattern="", stars="", r=0):
        pe = tk.Entry(parent, width=30, font=C["font"], bg=C["surface"],
                       fg=C["fg"], insertbackground=C["fg"], relief="flat")
        pe.grid(row=r, column=0, sticky="w", padx=(12, 4), pady=3)
        pe.insert(0, pattern)
        se = tk.Entry(parent, width=8, font=C["font"], bg=C["surface"],
                       fg=C["fg"], insertbackground=C["fg"], relief="flat")
        se.grid(row=r, column=1, sticky="w", padx=(0, 12), pady=3)
        se.insert(0, stars)
        rule_entries.append((pe, se))
        return pe, se

    existing_rules = cfg.get("lot_rules", [])
    if existing_rules:
        for i, rule in enumerate(existing_rules):
            add_rule_row(r_inner, rule.get("pattern", ""), str(rule.get("stars", "")), r=i + 3)
    else:
        add_rule_row(r_inner, "", "", r=3)

    rules_btn_frame = tk.Frame(r_inner, bg=C["bg"])
    rules_btn_frame.grid(row=100, column=0, columnspan=3, pady=(12, 8))

    def add_new_rule():
        idx = len(rule_entries)
        add_rule_row(r_inner, "", "", r=idx + 3)

    make_btn(rules_btn_frame, "➕ Добавить правило", add_new_rule, C["accent"])

    rules_status = tk.Label(rules_btn_frame, text="", font=C["font_md"],
                            bg=C["bg"], fg=C["green"])

    def save_rules():
        new_rules = []
        for pe, se in rule_entries:
            p = pe.get().strip()
            s = se.get().strip()
            if p and s:
                try:
                    new_rules.append({"pattern": p, "stars": int(s)})
                except ValueError:
                    pass
        cfg["lot_rules"] = new_rules
        _write_config(cfg)
        rules_status.config(text=f"✅ Сохранено ({len(new_rules)} правил)", fg=C["green"])
        root.after(3000, lambda: rules_status.config(text=""))

    make_btn(rules_btn_frame, "💾 Сохранить лоты", save_rules, C["green"])
    rules_status.pack(side="left", padx=(12, 0))

    # ──────────────── Вкладка 6: История ──────────────── #
    history_frame = tk.Frame(notebook, bg=C["bg"])
    notebook.add(history_frame, text=" 📊 История ")

    hist_columns = ("date", "order_id", "username", "stars", "cost", "revenue", "profit", "status")
    hist_tree = ttk.Treeview(
        history_frame, columns=hist_columns, show="headings", height=18,
    )
    hist_tree.heading("date", text="Дата")
    hist_tree.heading("order_id", text="Заказ")
    hist_tree.heading("username", text="Username")
    hist_tree.heading("stars", text="⭐")
    hist_tree.heading("cost", text="Затраты $")
    hist_tree.heading("revenue", text="Доход ₽")
    hist_tree.heading("profit", text="Прибыль ₽")
    hist_tree.heading("status", text="Статус")
    hist_tree.column("date", width=130)
    hist_tree.column("order_id", width=80)
    hist_tree.column("username", width=110)
    hist_tree.column("stars", width=50)
    hist_tree.column("cost", width=70)
    hist_tree.column("revenue", width=80)
    hist_tree.column("profit", width=80)
    hist_tree.column("status", width=70)

    h_scroll = ttk.Scrollbar(history_frame, orient="vertical", command=hist_tree.yview)
    hist_tree.configure(yscrollcommand=h_scroll.set)
    hist_tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
    h_scroll.pack(side="right", fill="y", padx=(0, 4), pady=4)

    # ──────────────── Вкладка 7: Прибыль ──────────────── #
    profit_frame = tk.Frame(notebook, bg=C["bg"])
    notebook.add(profit_frame, text=" 💰 Прибыль ")

    profit_inner = tk.Frame(profit_frame, bg=C["bg"])
    profit_inner.pack(fill="both", expand=True, padx=16, pady=16)

    # Карточки статистики
    def stat_card(parent, title, value, color, row, col):
        card = tk.Frame(parent, bg=C["mantle"], padx=16, pady=12,
                        highlightbackground=C["surface"], highlightthickness=1)
        card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
        parent.grid_columnconfigure(col, weight=1)
        tk.Label(card, text=title, font=C["font_sm"], bg=C["mantle"],
                 fg=C["dim"]).pack(anchor="w")
        lbl = tk.Label(card, text=value, font=("Segoe UI", 20, "bold"),
                       bg=C["mantle"], fg=color)
        lbl.pack(anchor="w")
        return lbl

    total_orders_lbl = stat_card(profit_inner, "Всего заказов", "0", C["accent"], 0, 0)
    total_stars_lbl = stat_card(profit_inner, "Всего звёзд", "0", C["yellow"], 0, 1)
    total_cost_lbl = stat_card(profit_inner, "Затраты USD", "$0.00", C["peach"], 0, 2)
    total_revenue_lbl = stat_card(profit_inner, "Доход ₽", "0 ₽", C["green"], 1, 0)
    total_profit_lbl = stat_card(profit_inner, "Прибыль ₽", "0 ₽", C["mauve"], 1, 1)
    today_profit_lbl = stat_card(profit_inner, "Сегодня ₽", "0 ₽", C["green"], 1, 2)

    # Настройки маржи
    margin_frame = tk.Frame(profit_inner, bg=C["mantle"], padx=16, pady=12,
                            highlightbackground=C["surface"], highlightthickness=1)
    margin_frame.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
    profit_inner.grid_rowconfigure(2, weight=1)

    tk.Label(margin_frame, text="⚙️ Настройки маржи", font=C["font_title"],
             bg=C["mantle"], fg=C["accent"]).pack(anchor="w", pady=(0, 8))

    # Под-frame для grid (чтобы не конфликтовать с pack выше)
    margin_grid = tk.Frame(margin_frame, bg=C["mantle"])
    margin_grid.pack(fill="x")

    m_row = 0
    for label_text, key, default in [
        ("Закупка 1⭐ (USD):", "cost_per_star_usd", "0.017"),
        ("Продажа 1⭐ (₽):", "revenue_per_star_rub", "1.5"),
        ("Курс USD→₽:", "usd_to_rub", "100"),
    ]:
        tk.Label(margin_grid, text=label_text, font=C["font_md"],
                 bg=C["mantle"], fg=C["fg"]).grid(row=m_row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=str(cfg.get(key, default)))
        entry = tk.Entry(margin_grid, width=12, textvariable=var, font=C["font"],
                         bg=C["surface"], fg=C["fg"], insertbackground=C["fg"], relief="flat")
        entry.grid(row=m_row, column=1, sticky="w", padx=(8, 0), pady=3)
        m_row += 1

    hide_var = tk.BooleanVar(value=cfg.get("hide_sender", False))
    tk.Checkbutton(margin_grid, text="Скрывать отправителя звёзд",
                    variable=hide_var, font=C["font_md"],
                    bg=C["mantle"], fg=C["fg"], selectcolor=C["surface"],
                    activebackground=C["mantle"]).grid(row=m_row, column=0,
                    columnspan=2, sticky="w", pady=6)

    margin_btn_frame = tk.Frame(margin_frame, bg=C["mantle"])
    margin_btn_frame.pack(anchor="w", pady=(8, 0))

    margin_status = tk.Label(margin_btn_frame, text="", font=C["font_md"],
                             bg=C["mantle"], fg=C["green"])

    def save_margin():
        entries = [w for w in margin_grid.winfo_children() if isinstance(w, tk.Entry)]
        if len(entries) >= 3:
            cfg["cost_per_star_usd"] = float(entries[0].get())
            cfg["revenue_per_star_rub"] = float(entries[1].get())
            cfg["usd_to_rub"] = float(entries[2].get())
        cfg["hide_sender"] = hide_var.get()
        _write_config(cfg)
        margin_status.config(text="✅ Сохранено", fg=C["green"])
        root.after(3000, lambda: margin_status.config(text=""))

    save_margin_btn = tk.Button(margin_btn_frame, text="💾 Сохранить", command=save_margin,
                                 font=C["font_title"], bg=C["accent"], fg="#1e1e1e",
                                 relief="flat", padx=12, pady=4, cursor="hand2")
    save_margin_btn.pack(side="left")
    margin_status.pack(side="left", padx=(12, 0))

    # ══════════════════════════════════════════════════════════════════════ #
    # Обновление данных
    # ══════════════════════════════════════════════════════════════════════ #
    last_log_pos = {"pos": 0}

    def refresh():
        running = _is_bot_running()
        if running:
            status_label.config(text="🟢 Бот работает", fg=C["green"])
        else:
            status_label.config(text="🔴 Бот остановлен", fg=C["red"])

        # Логи
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                f.seek(last_log_pos["pos"])
                new_lines = f.readlines()
                last_log_pos["pos"] = f.tell()
            for line in new_lines:
                line = line.rstrip()
                tag = "INFO"
                if "ERROR" in line or "ERR " in line:
                    tag = "ERROR"
                elif "WARNING" in line or "WARN" in line:
                    tag = "WARNING"
                elif "DEBUG" in line:
                    tag = "DEBUG"
                log_text.insert("end", line + "\n", tag)
            log_text.see("end")
        except FileNotFoundError:
            pass

        # Заказы
        state = _read_state()
        orders = state.get("orders", {})
        for item in orders_tree.get_children():
            orders_tree.delete(item)
        for oid, rec in sorted(orders.items(), key=lambda x: x[0]):
            orders_tree.insert("", "end", values=(
                oid, rec.get("status", "?"), rec.get("stars", ""),
                rec.get("username", ""), rec.get("price_usd", ""),
                rec.get("error", ""),
            ))

        # Баланс
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "баланс" in line.lower() and "gameau" in line.lower():
                        balance_label.config(text=line.strip()[:70])
        except FileNotFoundError:
            pass

        # История транзакций
        db = _read_db()
        txs = db.get("transactions", [])
        for item in hist_tree.get_children():
            hist_tree.delete(item)
        for tx in reversed(txs[-100:]):
            profit_val = tx.get("profit", 0)
            profit_color = C["green"] if profit_val >= 0 else C["red"]
            hist_tree.insert("", "end", values=(
                tx.get("date", ""),
                tx.get("order_id", ""),
                tx.get("username", ""),
                tx.get("stars", ""),
                f"${tx.get('cost_usd', 0):.4f}",
                f"{tx.get('revenue_rub', 0):.2f}",
                f"{profit_val:.2f}",
                tx.get("status", ""),
            ))

        # Статистика прибыли
        stats = db.get("stats", {})
        today = time.strftime("%Y-%m-%d")
        today_txs = [t for t in txs if t.get("date", "").startswith(today)]
        total_orders_lbl.config(text=str(stats.get("total_orders", 0)))
        total_stars_lbl.config(text=str(stats.get("total_stars", 0)))
        total_cost_lbl.config(text=f"${stats.get('total_cost_usd', 0):.2f}")
        total_revenue_lbl.config(text=f"{stats.get('total_revenue_rub', 0):.0f} ₽")
        tp = stats.get("total_profit_rub", 0)
        total_profit_lbl.config(text=f"{tp:.0f} ₽",
                                fg=C["green"] if tp >= 0 else C["red"])
        today_p = sum(t.get("profit", 0) for t in today_txs)
        today_profit_lbl.config(text=f"{today_p:.0f} ₽",
                                fg=C["green"] if today_p >= 0 else C["red"])

        root.after(2000, refresh)

    # Глобальная вставка Ctrl+V для всех Entry/Text виджетов
    def _global_paste(event):
        try:
            widget = event.widget
            if isinstance(widget, (tk.Entry, tk.Text)):
                clipboard = root.clipboard_get()
                if isinstance(widget, tk.Entry):
                    widget.insert("insert", clipboard)
                else:
                    widget.insert("insert", clipboard)
        except tk.TclError:
            pass
        return "break"

    root.bind("<Control-v>", _global_paste)
    root.bind("<Control-V>", _global_paste)

    root.bind("<F5>", lambda e: refresh())
    root.bind("<Control-q>", lambda e: root.destroy())

    # Если ключей нет — сразу на вкладку настроек
    if not _is_setup_done():
        notebook.select(0)
        root.after(100, lambda: messagebox.showinfo(
            "Добро пожаловать!",
            "首次 запуск — заполните API ключи на вкладке «🔑 API Ключи».\n\n"
            "1. FunPay Golden Key — из куки сайта\n"
            "2. GAMEAU API Key — из профиля gameau.us\n\n"
            "После сохранения нажмите «🔍 Проверка» для теста."
        ))

    refresh()
    root.mainloop()


# --------------------------------------------------------------------------- #
# Веб-панель (Flask)
# --------------------------------------------------------------------------- #

def run_web_gui(port: int = 8080) -> None:
    try:
        from flask import Flask, render_template_string, jsonify, request
    except ImportError:
        print("Flask не установлен. Выполните: pip install flask")
        sys.exit(1)

    app = Flask(__name__)

    HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FunPay Stars Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0f0f1a;color:#cdd6f4;
  display:flex;min-height:100vh}
.sidebar{width:240px;background:#181825;padding:20px 16px;display:flex;flex-direction:column;gap:8px;
  border-right:1px solid #313244}
.sidebar h1{font-size:16px;color:#89b4fa;margin-bottom:12px}
.sidebar button{background:#313244;color:#cdd6f4;border:none;padding:10px 14px;border-radius:8px;
  cursor:pointer;font-size:13px;text-align:left;transition:background .15s}
.sidebar button:hover{background:#45475a}
.sidebar button.green{background:#a6e3a1;color:#1e1e1e}
.sidebar button.green:hover{background:#94d89c}
.sidebar button.red{background:#f38ba8;color:#1e1e1e}
.sidebar button.red:hover{background:#e07a96}
.sidebar .sep{height:1px;background:#313244;margin:8px 0}
.main{flex:1;padding:20px;overflow-y:auto}
.status-bar{display:flex;gap:16px;align-items:center;padding:12px 16px;background:#181825;
  border-radius:10px;margin-bottom:16px}
.status-dot{width:12px;height:12px;border-radius:50%;flex-shrink:0}
.status-dot.on{background:#a6e3a1;box-shadow:0 0 8px #a6e3a1}
.status-dot.off{background:#f38ba8;box-shadow:0 0 8px #f38ba8}
.card{background:#181825;border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{font-size:14px;color:#89b4fa;margin-bottom:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:6px 10px;color:#89b4fa;border-bottom:1px solid #313244}
td{padding:6px 10px;border-bottom:1px solid #1e1e1e}
.log-box{background:#11111b;border-radius:8px;padding:12px;font-family:Consolas,monospace;
  font-size:12px;max-height:400px;overflow-y:auto;line-height:1.6;white-space:pre-wrap}
.log-ERROR{color:#f38ba8}.log-WARNING{color:#f9e2af}.log-INFO{color:#a6e3a1}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-done{background:#a6e3a1;color:#1e1e1e}.badge-failed{background:#f38ba8;color:#1e1e1e}
.badge-waiting{background:#f9e2af;color:#1e1e1e}.badge-sending{background:#89b4fa;color:#1e1e1e}
.badge-new{background:#45475a;color:#cdd6f4}
#check-output{background:#11111b;border-radius:8px;padding:12px;font-family:Consolas,monospace;
  font-size:12px;white-space:pre-wrap;max-height:300px;overflow-y:auto;display:none;margin-top:10px}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid #45475a;
  border-top-color:#89b4fa;border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.form-group{margin-bottom:12px}
.form-group label{display:block;font-size:12px;color:#89b4fa;margin-bottom:4px}
.form-group input,.form-group select{width:100%;padding:8px 12px;background:#313244;border:none;
  border-radius:6px;color:#cdd6f4;font-size:13px;font-family:Consolas,monospace}
.form-group input:focus{outline:2px solid #89b4fa}
.form-group .hint{font-size:11px;color:#6c7086;margin-top:2px}
.form-row{display:flex;gap:12px}
.form-row .form-group{flex:1}
.btn{padding:8px 16px;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
.btn-green{background:#a6e3a1;color:#1e1e1e}.btn-blue{background:#89b4fa;color:#1e1e1e}
.tabs{display:flex;gap:4px;margin-bottom:16px}
.tab{padding:8px 16px;border-radius:8px 8px 0 0;cursor:pointer;font-size:13px;
  background:#181825;color:#6c7086;border:1px solid #313244;border-bottom:none}
.tab.active{background:#313244;color:#89b4fa}
.tab-content{display:none}.tab-content.active{display:block}
.toast{position:fixed;bottom:20px;right:20px;padding:12px 20px;border-radius:8px;
  font-size:13px;font-weight:600;z-index:1000;animation:fadeIn .3s}
.toast-ok{background:#a6e3a1;color:#1e1e1e}
.toast-err{background:#f38ba8;color:#1e1e1e}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
</style>
</head>
<body>
<div class="sidebar">
  <h1>⭐ FunPay Stars Bot</h1>
  <button class="green" onclick="api('/api/start')">▶ Старт</button>
  <button class="red" onclick="api('/api/stop')">⏹ Стоп</button>
  <button onclick="runCheck()">🔍 Проверка</button>
  <button onclick="api('/api/once')">⚡ One-shot</button>
  <div class="sep"></div>
  <button onclick="showTab('dashboard')" id="nav-dash">📊 Дашборд</button>
  <button onclick="showTab('settings')" id="nav-settings">🔑 API Ключи</button>
  <button onclick="showTab('behavior')" id="nav-behavior">⚙️ Поведение</button>
  <button onclick="showTab('lots')" id="nav-lots">🎯 Лоты</button>
  <button onclick="showTab('history')" id="nav-history">📊 История</button>
  <button onclick="showTab('profit')" id="nav-profit">💰 Прибыль</button>
  <button onclick="location.reload()" style="margin-top:auto;color:#89b4fa">🔄 Обновить</button>
</div>
<div class="main">
  <div class="status-bar">
    <div class="status-dot" id="dot"></div>
    <span id="status-text">Загрузка…</span>
    <span style="margin-left:auto;font-size:12px;color:#6c7086" id="clock"></span>
  </div>

  <!-- ═══ Дашборд ═══ -->
  <div class="tab-content active" id="tab-dashboard">
    <div class="card">
      <h2>📦 Заказы в очереди</h2>
      <table>
        <thead><tr><th>ID</th><th>Статус</th><th>⭐</th><th>Username</th><th>Цена</th><th>Ошибка</th></tr></thead>
        <tbody id="orders-body"><tr><td colspan="6" style="color:#6c7086">Нет данных</td></tr></tbody>
      </table>
    </div>
    <div class="card">
      <h2>📜 Последние логи <span id="log-count" style="color:#6c7086;font-weight:normal"></span></h2>
      <div class="log-box" id="logs"></div>
    </div>
    <div class="card">
      <h2>🔍 Диагностика</h2>
      <button class="btn btn-blue" onclick="runCheck()">Запустить проверку</button>
      <div id="check-output"></div>
    </div>
  </div>

  <!-- ═══ API Ключи ═══ -->
  <div class="tab-content" id="tab-settings">
    <div class="card">
      <h2>🔑 API Ключи</h2>
      <div class="form-group">
        <label>FunPay Golden Key</label>
        <input type="password" id="fp-key" placeholder="Вставьте golden_key из куки">
        <div class="hint">funpay.com → F12 → Application → Cookies → golden_key</div>
      </div>
      <div class="form-group">
        <label>GAMEAU API Key</label>
        <input type="password" id="ga-key" placeholder="Вставьте API ключ GAMEAU">
        <div class="hint">gameau.us → Профиль → «API для интеграций» → «Выпустить ключ»</div>
      </div>
      <div class="form-group">
        <label>GAMEAU Base URL</label>
        <input type="text" id="ga-url" value="https://gameau.us/api/v1">
        <div class="hint">Не меняйте, если не знаете зачем</div>
      </div>
      <div style="margin-top:16px">
        <button class="btn btn-green" onclick="saveKeys()">💾 Сохранить ключи</button>
      </div>
    </div>
    <div class="card">
      <h2>👤 Telegram Уведомления (опционально)</h2>
      <div class="form-group">
        <label>Bot Token</label>
        <input type="password" id="tg-token" placeholder="123456:ABC-DEF...">
        <div class="hint">@BotFather → /newbot → скопируйте токен</div>
      </div>
      <div class="form-group">
        <label>Chat ID</label>
        <input type="text" id="tg-chat" placeholder="Ваш числовой Chat ID">
        <div class="hint">@userinfobot или @getmyid_bot → ваш chat_id</div>
      </div>
      <div style="margin-top:16px">
        <button class="btn btn-green" onclick="saveNotify()">💾 Сохранить</button>
      </div>
    </div>
  </div>

  <!-- ═══ Поведение ═══ -->
  <div class="tab-content" id="tab-behavior">
    <div class="card">
      <h2>⏱ Интервалы</h2>
      <div class="form-row">
        <div class="form-group">
          <label>Опрос заказов (сек)</label>
          <input type="number" id="poll" value="15" min="5" max="300">
        </div>
        <div class="form-group">
          <label>Обновление сессии (сек)</label>
          <input type="number" id="session-refresh" value="2700" min="300" max="7200">
        </div>
      </div>
    </div>
    <div class="card">
      <h2>⭐ Звёзды</h2>
      <div class="form-row">
        <div class="form-group">
          <label>По умолчанию (если лот не распознан)</label>
          <input type="number" id="default-stars" placeholder="Пусто = пропустить" min="0">
        </div>
        <div class="form-group">
          <label>Макс. множитель цены GAMEAU</label>
          <input type="number" id="max-charge" value="1.5" min="1" max="5" step="0.1">
        </div>
      </div>
    </div>
    <div class="card">
      <h2>💬 Поведение</h2>
      <label style="display:block;margin:6px 0"><input type="checkbox" id="ask-username" checked> Просить @username у покупателя</label>
      <label style="display:block;margin:6px 0"><input type="checkbox" id="remind-username" checked> Напоминать раз в час</label>
      <label style="display:block;margin:6px 0"><input type="checkbox" id="reply-on-delivery" checked> Отвечать покупателю после выдачи</label>
      <label style="display:block;margin:6px 0"><input type="checkbox" id="refund-on-failure"> Автовозврат при ошибке</label>
    </div>
    <div class="card">
      <h2>🛡 Ограничения</h2>
      <label style="display:block;margin:6px 0"><input type="checkbox" id="stop-low-balance"> Пауза при низком балансе GAMEAU</label>
      <div class="form-row">
        <div class="form-group">
          <label>Мин. баланс</label>
          <input type="number" id="min-balance" value="0" min="0">
        </div>
        <div class="form-group">
          <label>Попыток создания заказа</label>
          <input type="number" id="max-attempts" value="3" min="1" max="10">
        </div>
      </div>
    </div>
    <button class="btn btn-green" onclick="saveBehavior()">💾 Сохранить настройки</button>
  </div>

  <!-- ═══ Лоты ═══ -->
  <div class="tab-content" id="tab-lots">
    <div class="card">
      <h2>🎯 Правила лотов</h2>
      <p style="font-size:12px;color:#6c7086;margin-bottom:12px">
        pattern — regex, ищется в названии заказа. Проверяются по порядку, первое совпадение выигрывает.
      </p>
      <div id="rules-list"></div>
      <div style="margin-top:12px;display:flex;gap:8px">
        <button class="btn btn-blue" onclick="addRule()">➕ Добавить</button>
        <button class="btn btn-green" onclick="saveRules()">💾 Сохранить лоты</button>
      </div>
    </div>
  </div>

  <!-- ═══ История ═══ -->
  <div class="tab-content" id="tab-history">
    <div class="card">
      <h2>📊 История транзакций</h2>
      <table>
        <thead><tr><th>Дата</th><th>Заказ</th><th>Username</th><th>⭐</th><th>Затраты $</th><th>Доход ₽</th><th>Прибыль ₽</th><th>Статус</th></tr></thead>
        <tbody id="history-body"><tr><td colspan="8" style="color:#6c7086">Нет транзакций</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ═══ Прибыль ═══ -->
  <div class="tab-content" id="tab-profit">
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px">
      <div class="card" style="text-align:center">
        <div style="font-size:12px;color:#6c7086">Всего заказов</div>
        <div style="font-size:28px;font-weight:bold;color:#89b4fa" id="p-total-orders">0</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:12px;color:#6c7086">Всего звёзд</div>
        <div style="font-size:28px;font-weight:bold;color:#f9e2af" id="p-total-stars">0</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:12px;color:#6c7086">Затраты USD</div>
        <div style="font-size:28px;font-weight:bold;color:#fab387" id="p-total-cost">$0</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:12px;color:#6c7086">Доход ₽</div>
        <div style="font-size:28px;font-weight:bold;color:#a6e3a1" id="p-total-revenue">0 ₽</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:12px;color:#6c7086">Прибыль ₽</div>
        <div style="font-size:28px;font-weight:bold" id="p-total-profit">0 ₽</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:12px;color:#6c7086">Сегодня ₽</div>
        <div style="font-size:28px;font-weight:bold;color:#a6e3a1" id="p-today-profit">0 ₽</div>
      </div>
    </div>
    <div class="card">
      <h2>⚙️ Настройки маржи</h2>
      <div class="form-row">
        <div class="form-group">
          <label>Закупка 1⭐ (USD)</label>
          <input type="number" id="cost-star" step="0.001" min="0">
        </div>
        <div class="form-group">
          <label>Продажа 1⭐ (₽)</label>
          <input type="number" id="revenue-star" step="0.01" min="0">
        </div>
        <div class="form-group">
          <label>Курс USD→₽</label>
          <input type="number" id="usd-rub" step="0.01" min="0">
        </div>
      </div>
      <label style="display:block;margin:8px 0"><input type="checkbox" id="hide-sender"> Скрывать отправителя звёзд</label>
      <button class="btn btn-green" onclick="saveMargin()" style="margin-top:8px">💾 Сохранить</button>
    </div>
  </div>
</div>

<script>
const BADGE={done:'badge-done',failed:'badge-failed',waiting:'badge-waiting',
  sending:'badge-sending',new:'badge-new',no_rule:'badge-failed'};
let rulesData=[];

function showTab(name){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  document.querySelectorAll('.sidebar button').forEach(b=>b.style.color='');
  const nav=document.getElementById('nav-'+name);
  if(nav)nav.style.color='#89b4fa';
}

function toast(msg,ok=true){
  const d=document.createElement('div');
  d.className='toast '+(ok?'toast-ok':'toast-err');
  d.textContent=msg;document.body.appendChild(d);
  setTimeout(()=>d.remove(),3000);
}

async function api(url){
  try{const r=await fetch(url,{method:'POST'});const d=await r.json();toast(d.message||JSON.stringify(d))}
  catch(e){toast('Ошибка: '+e.message,false)}
}

async function runCheck(){
  const el=document.getElementById('check-output');
  el.style.display='block';el.innerHTML='<div class="spinner"></div> Выполняется…';
  try{const r=await fetch('/api/check',{method:'POST'});const d=await r.json();el.textContent=d.output||'Пусто'}
  catch(e){el.textContent='Ошибка: '+e.message}
}

async function saveKeys(){
  const data={
    FUNPAY_GOLDEN_KEY:document.getElementById('fp-key').value.trim(),
    GAMEAU_API_KEY:document.getElementById('ga-key').value.trim(),
    GAMEAU_BASE_URL:document.getElementById('ga-url').value.trim(),
  };
  try{const r=await fetch('/api/save-env',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();toast(d.message||'Сохранено',d.ok!==false)}
  catch(e){toast('Ошибка: '+e.message,false)}
}

async function saveNotify(){
  const data={
    TELEGRAM_BOT_TOKEN:document.getElementById('tg-token').value.trim(),
    TELEGRAM_CHAT_ID:document.getElementById('tg-chat').value.trim(),
  };
  try{const r=await fetch('/api/save-env',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();toast(d.message||'Сохранено',d.ok!==false)}
  catch(e){toast('Ошибка: '+e.message,false)}
}

async function saveBehavior(){
  const data={
    poll_interval:parseFloat(document.getElementById('poll').value)||15,
    session_refresh_interval:parseFloat(document.getElementById('session-refresh').value)||2700,
    default_stars:document.getElementById('default-stars').value?parseInt(document.getElementById('default-stars').value):null,
    gameau_max_charge_multiplier:parseFloat(document.getElementById('max-charge').value)||1.5,
    ask_username_in_chat:document.getElementById('ask-username').checked,
    remind_username:document.getElementById('remind-username').checked,
    reply_on_delivery:document.getElementById('reply-on-delivery').checked,
    refund_on_failure:document.getElementById('refund-on-failure').checked,
    stop_on_low_balance:document.getElementById('stop-low-balance').checked,
    min_gameau_balance:parseFloat(document.getElementById('min-balance').value)||0,
    max_create_attempts:parseInt(document.getElementById('max-attempts').value)||3,
  };
  try{const r=await fetch('/api/save-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();toast(d.message||'Сохранено',d.ok!==false)}
  catch(e){toast('Ошибка: '+e.message,false)}
}

function renderRules(){
  const el=document.getElementById('rules-list');
  el.innerHTML=rulesData.map((r,i)=>`<div style="display:flex;gap:8px;margin:4px 0;align-items:center">
    <input value="${escHtml(r.pattern)}" onchange="rulesData[${i}].pattern=this.value"
      style="flex:2;padding:6px 10px;background:#313244;border:none;border-radius:6px;color:#cdd6f4;font-family:Consolas,monospace;font-size:13px"
      placeholder="regex (например \\b100\\b)">
    <input type="number" value="${r.stars}" onchange="rulesData[${i}].stars=parseInt(this.value)"
      style="flex:0 0 80px;padding:6px 10px;background:#313244;border:none;border-radius:6px;color:#cdd6f4;font-family:Consolas,monospace;font-size:13px"
      placeholder="⭐">
    <button onclick="rulesData.splice(${i},1);renderRules()"
      style="background:#f38ba8;color:#1e1e1e;border:none;padding:6px 10px;border-radius:6px;cursor:pointer">✕</button>
  </div>`).join('');
}

function addRule(){rulesData.push({pattern:'',stars:0});renderRules()}

async function saveRules(){
  try{const r=await fetch('/api/save-rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(rulesData)});
    const d=await r.json();toast(d.message||'Сохранено',d.ok!==false)}
  catch(e){toast('Ошибка: '+e.message,false)}
}

function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

async function saveMargin(){
  const data={
    cost_per_star_usd:parseFloat(document.getElementById('cost-star').value)||0.017,
    revenue_per_star_rub:parseFloat(document.getElementById('revenue-star').value)||1.5,
    usd_to_rub:parseFloat(document.getElementById('usd-rub').value)||100,
    hide_sender:document.getElementById('hide-sender').checked,
  };
  try{const r=await fetch('/api/save-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();toast(d.message||'Сохранено',d.ok!==false)}
  catch(e){toast('Ошибка: '+e.message,false)}
}

async function refresh(){
  try{
    const r=await fetch('/api/status');const d=await r.json();
    document.getElementById('dot').className='status-dot '+(d.running?'on':'off');
    document.getElementById('status-text').textContent=d.running?'🟢 Бот работает':'🔴 Бот остановлен';
    const tb=document.getElementById('orders-body');
    if(d.orders.length){
      tb.innerHTML=d.orders.map(o=>`<tr>
        <td>${o.id}</td>
        <td><span class="badge ${BADGE[o.status]||'badge-new'}">${o.status}</span></td>
        <td>${o.stars||''}</td><td>${o.username||''}</td>
        <td>${o.price||''}</td><td style="color:#f38ba8">${o.error||''}</td>
      </tr>`).join('');
    }else{tb.innerHTML='<tr><td colspan="6" style="color:#6c7086">Нет заказов</td></tr>'}
    const lb=document.getElementById('logs');
    lb.innerHTML=d.logs.map(l=>{
      let c='';if(l.includes('ERROR')||l.includes('ERR '))c='log-ERROR';
      else if(l.includes('WARNING')||l.includes('WARN'))c='log-WARNING';
      else if(l.includes('INFO'))c='log-INFO';
      return `<span class="${c}">${escHtml(l)}</span>`;
    }).join('\\n');
    lb.scrollTop=lb.scrollHeight;
    document.getElementById('log-count').textContent=`(${d.logs.length} строк)`;
    // Заполнить поля настроек из сервера
    if(d.config){
      const c=d.config;
      document.getElementById('poll').value=c.poll_interval||15;
      document.getElementById('session-refresh').value=c.session_refresh_interval||2700;
      document.getElementById('default-stars').value=c.default_stars||'';
      document.getElementById('max-charge').value=c.gameau_max_charge_multiplier||1.5;
      document.getElementById('ask-username').checked=c.ask_username_in_chat!==false;
      document.getElementById('remind-username').checked=c.remind_username!==false;
      document.getElementById('reply-on-delivery').checked=c.reply_on_delivery!==false;
      document.getElementById('refund-on-failure').checked=!!c.refund_on_failure;
      document.getElementById('stop-low-balance').checked=!!c.stop_on_low_balance;
      document.getElementById('min-balance').value=c.min_gameau_balance||0;
      document.getElementById('max-attempts').value=c.max_create_attempts||3;
    }
    if(d.env){
      document.getElementById('fp-key').value=d.env.FUNPAY_GOLDEN_KEY||'';
      document.getElementById('ga-key').value=d.env.GAMEAU_API_KEY||'';
      document.getElementById('ga-url').value=d.env.GAMEAU_BASE_URL||'https://gameau.us/api/v1';
      document.getElementById('tg-token').value=d.env.TELEGRAM_BOT_TOKEN||'';
      document.getElementById('tg-chat').value=d.env.TELEGRAM_CHAT_ID||'';
    }
    if(d.rules){rulesData=d.rules;renderRules()}
    // История
    if(d.transactions){
      const hb=document.getElementById('history-body');
      if(d.transactions.length){
        hb.innerHTML=d.transactions.map(t=>`<tr>
          <td>${t.date||''}</td><td>${t.order_id||''}</td><td>${t.username||''}</td>
          <td>${t.stars||''}</td><td>$${(t.cost_usd||0).toFixed(4)}</td>
          <td>${(t.revenue_rub||0).toFixed(0)} ₽</td>
          <td style="color:${(t.profit||0)>=0?'#a6e3a1':'#f38ba8'}">${(t.profit||0).toFixed(0)} ₽</td>
          <td>${t.status||''}</td></tr>`).join('');
      }else{hb.innerHTML='<tr><td colspan="8" style="color:#6c7086">Нет транзакций</td></tr>'}
    }
    // Прибыль
    if(d.stats){
      const s=d.stats;
      document.getElementById('p-total-orders').textContent=s.total_orders||0;
      document.getElementById('p-total-stars').textContent=s.total_stars||0;
      document.getElementById('p-total-cost').textContent='$'+(s.total_cost_usd||0).toFixed(2);
      document.getElementById('p-total-revenue').textContent=(s.total_revenue_rub||0).toFixed(0)+' ₽';
      const tp=s.total_profit_rub||0;
      const tpe=document.getElementById('p-total-profit');
      tpe.textContent=tp.toFixed(0)+' ₽';
      tpe.style.color=tp>=0?'#a6e3a1':'#f38ba8';
    }
    if(d.today){
      const td=d.today;
      const tpe=document.getElementById('p-today-profit');
      tpe.textContent=td.profit.toFixed(0)+' ₽';
      tpe.style.color=td.profit>=0?'#a6e3a1':'#f38ba8';
    }
    if(d.config){
      document.getElementById('cost-star').value=d.config.cost_per_star_usd||0.017;
      document.getElementById('revenue-star').value=d.config.revenue_per_star_rub||1.5;
      document.getElementById('usd-rub').value=d.config.usd_to_rub||100;
      document.getElementById('hide-sender').checked=!!d.config.hide_sender;
    }
  }catch(e){console.error(e)}
  document.getElementById('clock').textContent=new Date().toLocaleTimeString('ru');
}

refresh();setInterval(refresh,3000);
</script>
</body>
</html>"""

    @app.route("/")
    def index():
        return render_template_string(HTML_TEMPLATE)

    @app.route("/api/status")
    def api_status():
        state = _read_state()
        orders_raw = state.get("orders", {})
        orders = [
            {"id": oid, "status": rec.get("status", "?"),
             "stars": rec.get("stars", ""), "username": rec.get("username", ""),
             "price": rec.get("price_usd", ""), "error": rec.get("error", "")}
            for oid, rec in sorted(orders_raw.items(), key=lambda x: x[0])
        ]
        logs = [l.rstrip() for l in _read_log_tail(150)]
        env = _read_env()
        cfg = _read_config()
        db = _read_db()
        txs = db.get("transactions", [])[-50:]
        stats = db.get("stats", {})
        today = time.strftime("%Y-%m-%d")
        today_txs = [t for t in db.get("transactions", []) if t.get("date", "").startswith(today)]
        today_stats = {
            "orders": len(today_txs),
            "stars": sum(t.get("stars", 0) for t in today_txs),
            "profit": round(sum(t.get("profit", 0) for t in today_txs), 2),
        }
        return jsonify({
            "running": _is_bot_running(), "orders": orders, "logs": logs,
            "config": cfg, "env": env, "rules": cfg.get("lot_rules", []),
            "transactions": list(reversed(txs)), "stats": stats,
            "today": today_stats,
        })

    @app.route("/api/start", methods=["POST"])
    def api_start():
        return jsonify({"message": start_bot()})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        return jsonify({"message": stop_bot()})

    @app.route("/api/check", methods=["POST"])
    def api_check():
        return jsonify({"output": run_check()})

    @app.route("/api/once", methods=["POST"])
    def api_once():
        return jsonify({"message": run_once()})

    @app.route("/api/save-env", methods=["POST"])
    def api_save_env():
        data = request.get_json(force=True)
        _write_env(data)
        return jsonify({"ok": True, "message": "✅ Ключи сохранены в .env"})

    @app.route("/api/save-config", methods=["POST"])
    def api_save_config():
        data = request.get_json(force=True)
        cfg = _read_config()
        cfg.update(data)
        _write_config(cfg)
        return jsonify({"ok": True, "message": "✅ Настройки сохранены в config.json"})

    @app.route("/api/save-rules", methods=["POST"])
    def api_save_rules():
        data = request.get_json(force=True)
        cfg = _read_config()
        cfg["lot_rules"] = data
        _write_config(cfg)
        return jsonify({"ok": True, "message": f"✅ Сохранено ({len(data)} правил)"})

    print(f"🌐 Веб-панель: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)


# --------------------------------------------------------------------------- #
# Точка входа
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="GUI FunPay Stars Bot")
    parser.add_argument("--port", type=int, default=0,
                        help="Запустить веб-панель на порту (если не указан — tkinter GUI)")
    args = parser.parse_args()

    if args.port:
        run_web_gui(args.port)
    else:
        run_tkinter_gui()


if __name__ == "__main__":
    main()
