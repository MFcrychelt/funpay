"""Мост GUI ↔ движок. НЕ зависит от flet — чтобы логику можно было тестировать.

Почему subprocess, а не «бот внутри окна»:
  • цикл автовыдачи живёт в отдельном процессе и переживает закрытие окна;
  • GUI не может «задавить» выдачу своим событием, и наоборот;
  • один и тот же код работает и в исходниках, и в PyInstaller-exe (там
    `python -m autostars.main` недоступен, поэтому вызывается сам exe-файл);
  • вывод команд (`--check`, `--stats --json`) ровно тот же, что в терминале,
    — нет расхождений «в GUI одно, в консоли другое».
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # запуск из исходников без установки пакета
    sys.path.insert(0, str(ROOT))

from autostars.config import Config  # noqa: E402
from autostars.paths import app_dir  # noqa: E402

#: Белый список флагов, которые GUI разрешает подмешивать к запуску цикла
#: (лишний флаг из формы — не аргумент; опасные --release/--cancel идут через run_cli).
CLI_FLAGS = ("--once", "--check", "--catalog", "--deposit-info", "--stats", "--report",
             "--tasks", "--stats-push", "--balance", "--pause", "--resume",
             "--config-show", "--json", "-v", "--verbose",
             "--limits", "--held", "--customers", "--templates", "--metrics")


@dataclasses.dataclass
class CommandResult:
    """Результат разовой CLI-команды движка."""

    ok: bool
    output: str
    args: Sequence[str] = ()
    returncode: int | None = None

    @property
    def args_str(self) -> str:
        return " ".join(self.args)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def engine_command(*args: str) -> list[str]:
    """Команда запуска движка: exe-файл в сборке, иначе `python -m autostars.main`."""
    if is_frozen():
        exe = Path(sys.executable)
        # GUI-сборка (AutoStars.exe) — это то же приложение, что и CLI:
        # PyInstaller-аргументы не мешают, но для надёжности ищем соседний CLI-exe.
        cli_name = exe.stem.replace("GUI", "").replace("Gui", "")
        for candidate in (
            exe.with_name(f"{cli_name}Bot.exe") if cli_name else None,
            exe.with_name("AutoStarsBot.exe"),
            exe,
        ):
            if candidate and candidate.exists():
                return [str(candidate), *args]
        return [str(exe), *args]
    return [sys.executable, "-m", "autostars.main", *args]


def _popen_kwargs() -> dict[str, Any]:
    """Окружение дочернего процесса движка.

    PYTHONPATH с корнем проекта нужен, чтобы GUI, запущенный «из исходников»
    двойным кликом (или из чужой рабочей папки), находил пакет autostars.
    На Windows дополнительно подавляем консольное окно.
    """
    env = os.environ.copy()
    cwd = app_dir()
    if not is_frozen():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), existing]))
        cwd = ROOT if (ROOT / "autostars").exists() else app_dir()
    kwargs: dict[str, Any] = {"cwd": str(cwd), "env": env}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
    return kwargs


def run_cli(*args: str, timeout: float = 90.0) -> CommandResult:
    """Выполняет разовую команду движка и возвращает её вывод (stdout+stderr)."""
    cmd = engine_command(*args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        return CommandResult(False, f"Не удалось запустить движок: {exc}", args)
    except subprocess.TimeoutExpired:
        return CommandResult(False, f"Команда не завершилась за {timeout:.0f}с: {' '.join(args)}", args)

    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return CommandResult(proc.returncode == 0, output.strip(), args, proc.returncode)


def parse_json_output(output: str) -> Any | None:
    """Достаёт JSON из вывода CLI (лог-строки идут в stderr, но подстрахуемся)."""
    for line in reversed([ln.strip() for ln in output.splitlines() if ln.strip()]):
        if line[:1] in "[{":
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(line)
    return None


# --------------------------------------------------------------------------- #
# Фоновый процесс автовыдачи
# --------------------------------------------------------------------------- #


class EngineProcess:
    """Запуск/остановка цикла выдачи как отдельного процесса + хвост его логов."""

    def __init__(self, extra_args: Sequence[str] = (), logs_limit: int = 400) -> None:
        self.extra_args: list[str] = [a for a in extra_args if a in CLI_FLAGS or a.startswith("-")]
        self._proc: subprocess.Popen | None = None
        self._lines: deque[str] = deque(maxlen=logs_limit)
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._started_at: float | None = None
        self._last_exit: int | None = None

    # -- состояние --
    @property
    def running(self) -> bool:
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        code = proc.poll()
        if code is not None:
            with self._lock:
                self._last_exit = code
                self._proc = None
            return False
        return True

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def uptime_seconds(self) -> float:
        if not self.running or self._started_at is None:
            return 0.0
        return max(0.0, time.time() - self._started_at)

    @property
    def last_exit_code(self) -> int | None:
        return self._last_exit

    # -- управление --
    def start(self) -> CommandResult:
        if self.running:
            return CommandResult(True, "Цикл автовыдачи уже запущен", ("status",))
        cmd = engine_command(*self.extra_args)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_popen_kwargs(),
            )
        except OSError as exc:
            return CommandResult(False, f"Не удалось запустить движок: {exc}", cmd)

        with self._lock:
            self._proc = proc
            self._started_at = time.time()
            self._last_exit = None
        self._reader = threading.Thread(target=self._pump, args=(proc,), name="engine-log-reader", daemon=True)
        self._reader.start()
        return CommandResult(True, f"Запущено (pid {proc.pid}): {' '.join(cmd)}", cmd)

    def _pump(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        if stream is None:
            return
        for raw in iter(stream.readline, ""):
            line = raw.rstrip()
            if line:
                with self._lock:
                    self._lines.append(line)
        with contextlib.suppress(Exception):
            stream.close()

    def stop(self, timeout: float = 35.0) -> CommandResult:
        """Корректная остановка: SIGTERM (graceful shutdown), затем SIGKILL."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return CommandResult(True, "Цикл автовыдачи не запущен", ("stop",))

        with self._lock:
            self._lines.append("[gui] запрошена корректная остановка…")
        try:
            if os.name == "nt":
                # В Windows нет SIGTERM-семантики asyncio — закрываем окно/посылаем CTRL_BREAK
                proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            else:
                proc.terminate()
            proc.wait(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
            code = None
            with self._lock:
                self._lines.append("[gui] процесс не отреагировал — принудительно завершён (kill)")
        except OSError as exc:
            return CommandResult(False, f"Ошибка остановки: {exc}", ("stop",))

        with self._lock:
            self._proc = None
            self._last_exit = code
        return CommandResult(True, f"Остановлен (код {code if code is not None else 'terminated'})", ("stop",))

    def drain_logs(self, limit: int = 200) -> list[str]:
        """Последние строки вывода процесса (для вкладки «Логи»)."""
        with self._lock:
            items = list(self._lines)
        return items[-limit:]


# --------------------------------------------------------------------------- #
# Чтение состояния (БД движка) без запуска цикла
# --------------------------------------------------------------------------- #


def load_config() -> Config:
    """Конфигурация так же, как её видит CLI (.env → env → config.json)."""
    return Config.load()


_ORDER_COLUMNS = (
    "order_id", "username", "quantity", "price_rub", "cost_usdt", "cost_rub",
    "profit_rub", "status", "error", "created_ts", "updated_ts", "gameau_order_id",
)


def read_orders(db_path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    """Последние заказы из БД (только чтение). Не падает, если базы ещё нет."""
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
    except sqlite3.Error:
        return []
    try:
        conn.execute("PRAGMA busy_timeout=3000")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(orders)").fetchall()
        }
        if not columns:  # таблицы ещё нет (движок не запускался)
            return []
        wanted = [c for c in _ORDER_COLUMNS if c in columns]
        if not wanted:  # таблица есть, но не с ожидаемой структурой
            return []
        order = (
            " ORDER BY COALESCE(updated_ts, created_ts) DESC"
            if {"updated_ts", "created_ts"} & columns
            else ""
        )
        rows = conn.execute(
            f"SELECT {', '.join(wanted)} FROM orders{order} LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(zip(wanted, row)) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def read_status_counts(db_path: str | Path) -> dict[str, int]:
    """Счётчики заказов по статусам (для «плиток» на дашборде)."""
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            rows = conn.execute("SELECT status, COUNT(*) AS c FROM orders GROUP BY status").fetchall()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return {str(r[0]): int(r[1]) for r in rows}
    except sqlite3.Error:
        return {}


def is_paused(db_path: str | Path) -> bool:
    """Флаг паузы из таблицы settings (его же использует --pause/--resume)."""
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            row = conn.execute("SELECT value FROM settings WHERE key = 'bot_paused'").fetchone()
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        return bool(row) and str(row[0]) == "1"
    except sqlite3.Error:
        return False



def read_held_orders(db_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """Заказы, задержанные политикой выдачи (HOLD_MANUAL) — только чтение."""
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            rows = conn.execute(
                "SELECT order_id, username, quantity, price_rub, cost_usdt, error, created_ts"
                " FROM orders WHERE status = 'HOLD_MANUAL'"
                " ORDER BY COALESCE(updated_ts, created_ts, 0) DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        wanted = ("order_id", "username", "quantity", "price_rub", "cost_usdt", "error", "created_ts")
        return [dict(zip(wanted, row)) for row in rows]
    except sqlite3.Error:
        return []


def read_spend_today_usdt(db_path: str | Path) -> float | None:
    """Сколько USDT списано с полуночи (тот же запрос, что у суточного лимита)."""
    import time

    path = Path(db_path)
    if not path.exists():
        return None
    now = time.localtime()
    midnight = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)))
    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usdt), 0) FROM orders "
                "WHERE status <> 'CANCELLED' AND COALESCE(updated_ts, created_ts, 0) >= ?",
                (midnight,),
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        return float(row[0] or 0.0) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def read_buyer_flags(db_path: str | Path, kind: str = "blacklist") -> list[dict[str, Any]]:
    """Стоп-лист покупателей (пусто, если таблицы ещё нет — она создаётся с v2.4)."""
    path = Path(db_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path), timeout=3.0)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            rows = conn.execute(
                "SELECT username, note, created_at FROM buyer_flags WHERE kind = ?"
                " ORDER BY created_ts DESC LIMIT 50",
                (kind,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [{"username": r[0], "note": r[1], "created_at": r[2]} for r in rows]
    except sqlite3.Error:
        return []


def export_orders(out_path: str | Path, *, period: str = "", failed: bool = False,
                  with_events: bool = False, timeout: float = 120.0) -> CommandResult:
    """Выгрузка истории (тот же `--export`, что и в консоли)."""
    args = ["--export", str(out_path)]
    if period:
        args += ["--since", period]
    if failed:
        args.append("--failed")
    if with_events:
        args.append("--with-events")
    return run_cli(*args, timeout=timeout)


def read_log_tail(log_path: str | Path, lines: int = 200) -> list[str]:
    """Хвост файла лога (файл пишет и CLI, и фоновый цикл)."""
    path = Path(log_path)
    if not path.exists() or not lines:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 512_000:
                fh.seek(-512_000, os.SEEK_END)
                fh.readline()  # отбрасываем возможный неполный хвост строки
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [line for line in data.splitlines() if line.strip()][-lines:]


def status_snapshot(process: EngineProcess) -> dict[str, Any]:
    """Одна структура для всех виджетов статуса (дешёвая, без сети)."""
    cfg = load_config()
    return {
        "running": process.running,
        "pid": process.pid,
        "uptime_seconds": process.uptime_seconds,
        "last_exit_code": process.last_exit_code,
        "paused": is_paused(cfg.db_path),
        "status_counts": read_status_counts(cfg.db_path),
        "orders": read_orders(cfg.db_path, limit=25),
        "config_ok": not [i for i in cfg.issues() if i.startswith("missing:")],
        "issues": [i for i in cfg.issues() if not i.startswith("missing:")],
        "db_path": cfg.db_path,
        "log_path": cfg.log_file,
        "rate": cfg.active_usdt_rate,
        "rate_label": cfg.active_rate_label,
    }


def stats_json() -> dict[str, Any] | None:
    """Статистика по окнам в машиночитаемом виде (движок сам считает агрегаты)."""
    result = run_cli("--stats", "--json", timeout=60)
    if not result.ok:
        return None
    return parse_json_output(result.output)


def humanize_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин {seconds % 60:02d} с"
    hours, rest = divmod(seconds, 3600)
    if hours < 48:
        return f"{hours} ч {rest // 60:02d} мин"
    return f"{hours // 24} дн {hours % 24} ч"


def python_ok() -> bool:
    """Для диагностики в GUI: есть ли рядом интерпретатор (актуально не для exe)."""
    return is_frozen() or bool(shutil.which("python3") or shutil.which("python"))
