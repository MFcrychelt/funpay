"""Тесты моста GUI ↔ движок (без flet: это чистая логика, её надо проверять).

Закрепляет ключевые свойства « GUI = оболочка над боевым движком»:
  • команда строится из реальных исполняемых файлов (исходники / exe);
  • разовые команды (``--calc``, ``--stats --json``) выполняются и парсятся;
  • читается та же SQLite, что пишет движок, — без «своей» статистики;
  • фоновый процесс запускается, логируются его строки, и он корректно
    останавливается (иначе GUI оставлял бы сироту, который тратит USDT).
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from autostars.gui import bridge


# --------------------------------------------------------------------------- #
# команда запуска
# --------------------------------------------------------------------------- #
def test_engine_command_uses_current_interpreter(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = bridge.engine_command("--check")
    assert cmd[0] == sys.executable, "нельзя зависеть от `python` в PATH"
    assert cmd[1:3] == ["-m", "autostars.main"]
    assert cmd[-1] == "--check"


def test_engine_command_in_frozen_build(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    gui_exe = tmp_path / "AutoStars.exe"
    gui_exe.write_text("", encoding="utf-8")
    bot_exe = tmp_path / "AutoStarsBot.exe"
    bot_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(gui_exe), raising=False)
    cmd = bridge.engine_command("--once")
    assert cmd[0] == str(bot_exe), "GUI должен дёргать CLI-exe соседа, а не самого себя"
    assert cmd[-1] == "--once"


WRITE_KEYS = {
    "FUNPAY_GOLDEN_KEY": "fake-key-for-tests",
    "GAMEAU_API_KEY": "fake-gameau-key",
    "EXCHANGE_VARIANT": "2",
    "RATE_VARIANT_2": "87.63",
    "RATE_VARIANT_1": "110",
    "USDT_PER_1000_STARS": "9.10",
}


def test_run_cli_executes_engine_command(tmp_path, monkeypatch):
    """Разовая команда движка: тот же вывод, что в терминале (важно для доверия)."""
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in WRITE_KEYS.items()) + "\n", encoding="utf-8"
    )
    result = bridge.run_cli("--calc", "1370.4", "1000", timeout=90)
    assert result.ok, result.output
    assert "КАЛЬКУЛЯТОР ПРИБЫЛИ" in result.output
    assert result.returncode == 0


def test_run_cli_timeout_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    # time.sleep в child: берём команду, которая заведомо не завершится за 1с
    monkeypatch.setattr(bridge, "engine_command", lambda *a: [sys.executable, "-c", "import time; time.sleep(30)"])
    result = bridge.run_cli("--never", timeout=1)
    assert not result.ok and "не завершилась" in result.output


def test_parse_json_output_skips_log_lines():
    payload = {"windows": [{"label": "1 час"}]}
    text = "2026-01-01 [INFO] что-то в stderr\n" + json.dumps(payload)
    assert bridge.parse_json_output(text) == payload
    assert bridge.parse_json_output("не json вообще") is None


# --------------------------------------------------------------------------- #
# чтение состояния движка
# --------------------------------------------------------------------------- #
async def _make_db_with_orders(db_path: Path) -> None:
    from autostars.database.db_manager import DBManager

    db = DBManager(str(db_path))
    await db.init_db()
    await db.save_order_status(order_id="G-1", username="durov", status="COMPLETED", quantity=1000,
                               price_rub=1370.4, cost_usdt=9.1, cost_rub=797.4, profit_rub=573.0)
    await db.save_order_status(order_id="G-2", username=None, status="WAITING_USERNAME", quantity=500)
    await db.save_order_status(order_id="G-3", username="olga", status="FAILED_LOW_BALANCE", quantity=1000)
    await db.set_setting("bot_paused", "1")
    await db.close()


def test_reads_state_from_engine_database(tmp_path):
    asyncio_run = __import__("asyncio").run
    db_path = tmp_path / "autostars.db"
    asyncio_run(_make_db_with_orders(db_path))

    orders = bridge.read_orders(db_path, limit=10)
    assert {o["order_id"] for o in orders} == {"G-1", "G-2", "G-3"}
    g1 = next(o for o in orders if o["order_id"] == "G-1")
    assert g1["profit_rub"] == pytest.approx(573.0)

    counts = bridge.read_status_counts(db_path)
    assert counts["COMPLETED"] == 1 and counts["FAILED_LOW_BALANCE"] == 1

    assert bridge.is_paused(db_path) is True

    # GUI ничего не пишет в базу движка
    before = db_path.read_bytes()
    bridge.read_orders(db_path, limit=10)
    assert db_path.read_bytes() == before


def test_state_helpers_survive_missing_database(tmp_path):
    missing = tmp_path / "нет-такой.db"
    assert bridge.read_orders(missing) == []
    assert bridge.read_status_counts(missing) == {}
    assert bridge.is_paused(missing) is False


def test_read_orders_tolerates_legacy_schema(tmp_path):
    """Старая база без колонки gameau_order_id — GUI не должен падать."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE orders (order_id TEXT PRIMARY KEY, username TEXT, quantity INTEGER,"
        " price_rub REAL, cost_usdt REAL, cost_rub REAL, profit_rub REAL, status TEXT,"
        " error TEXT, created_ts INTEGER, updated_ts INTEGER)"
    )
    conn.execute("INSERT INTO orders VALUES ('X-1','bob',100,100.0,1.0,80.0,20.0,'COMPLETED',NULL,1,1)")
    conn.commit()
    conn.close()
    orders = bridge.read_orders(db, limit=5)
    assert orders and orders[0]["order_id"] == "X-1"


def test_read_log_tail(tmp_path):
    log = tmp_path / "autostars.log"
    log.write_text("\n".join(f"line {i}" for i in range(1, 301)), encoding="utf-8")
    tail = bridge.read_log_tail(log, lines=5)
    assert tail[-1] == "line 300" and len(tail) == 5
    assert bridge.read_log_tail(tmp_path / "absent.log") == []


def test_humanize_duration():
    assert bridge.humanize_duration(45) == "45 с"
    assert "мин" in bridge.humanize_duration(180)
    assert "ч" in bridge.humanize_duration(7200)
    assert "дн" in bridge.humanize_duration(200000)


# --------------------------------------------------------------------------- #
# фоновый процесс
# --------------------------------------------------------------------------- #
def test_engine_process_start_stop(tmp_path, monkeypatch):
    """Фоновый цикл: реальный subprocess, сбор строк, корректная остановка."""
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    script = "import time\nprint('READY')\nprint('цикл автовыдачи: шаг 5s')\ntime.sleep(60)"
    monkeypatch.setattr(bridge, "engine_command", lambda *a: [sys.executable, "-u", "-c", script])

    proc = bridge.EngineProcess()
    assert not proc.running and proc.pid is None

    start = proc.start()
    assert start.ok, start.output
    assert proc.running and proc.pid
    assert proc.uptime_seconds >= 0

    deadline = time.time() + 15
    while time.time() < deadline and "READY" not in "\n".join(proc.drain_logs()):
        time.sleep(0.2)
    logs = proc.drain_logs(limit=50)
    assert any("READY" in line for line in logs), logs
    assert any("цикл автовыдачи" in line for line in logs), logs

    stop = proc.stop(timeout=10)
    assert stop.ok
    assert not proc.running and proc.pid is None
    assert proc.last_exit_code is not None
    assert proc.stop(timeout=5).ok  # повторная остановка — без падения
    assert proc.start().ok  # запуск после остановки возможен
    proc.stop(timeout=10)


def test_engine_process_double_start_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    monkeypatch.setattr(bridge, "engine_command", lambda *a: [sys.executable, "-c", "import time; time.sleep(10)"])
    proc = bridge.EngineProcess()
    assert proc.start().ok
    assert proc.running
    second = proc.start()
    assert second.ok and "уже запущен" in second.output
    proc.stop(timeout=10)


def test_status_snapshot_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSTARS_HOME", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "snap.db"))
    for key, value in WRITE_KEYS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "snap.log"))
    snapshot = bridge.status_snapshot(bridge.EngineProcess())
    for key in ("running", "paused", "status_counts", "orders", "config_ok", "issues", "db_path"):
        assert key in snapshot
    assert snapshot["running"] is False
    assert isinstance(snapshot["issues"], list)
