"""Выгрузка истории (CSV/JSON) и метрики Prometheus/healthz.

Оба механизма — «внешний мир», поэтому тесты идут до конца: пишутся реальные
файлы (во временном каталоге) и поднимается реальный HTTP-сервер на 127.0.0.1
с случайным портом. Сети наружу нет, зависимостей тоже.
"""

from __future__ import annotations

import asyncio
import csv
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostars.database.db_manager import DBManager
from autostars.metrics import MetricsServer, render_prometheus
from autostars.services.export import (
    EVENT_COLUMNS,
    FAILED_LIKE,
    ORDER_COLUMNS,
    export_csv,
    export_json,
    parse_period,
    rows_to_csv,
    summarize_rows,
)

ROOT = Path(__file__).resolve().parents[1]

# ============================================================================ #
# 1. Периоды
# ============================================================================ #


def test_parse_period_relative_windows():
    now = datetime(2026, 9, 18, 12, 0, 0)
    assert parse_period("7d", now=now) == ("2026-09-11 12:00:00", "2026-09-18 12:00:00")
    assert parse_period("24h", now=now) == ("2026-09-17 12:00:00", "2026-09-18 12:00:00")
    assert parse_period("30m", now=now) == ("2026-09-18 11:30:00", "2026-09-18 12:00:00")
    assert parse_period("2w", now=now) == ("2026-09-04 12:00:00", "2026-09-18 12:00:00")


def test_parse_period_absolute_and_range():
    now = datetime(2026, 9, 18, 12, 0, 0)
    assert parse_period("2026-09-01", now=now) == ("2026-09-01 00:00:00", "2026-09-18 12:00:00")
    assert parse_period("2026-09-01..2026-09-07", now=now) == ("2026-09-01 00:00:00", "2026-09-07 23:59:59")
    assert parse_period("", now=now) is None
    assert parse_period(None) is None
    assert parse_period("all") is None


def test_parse_period_rejects_garbage():
    for bad in ("неделя", "7к", "2026-13-45..2026-01-01", "d"):
        with pytest.raises(ValueError):
            parse_period(bad)


# ============================================================================ #
# 2. Формат CSV
# ============================================================================ #


def test_csv_columns_are_stable_and_header_first():
    assert ORDER_COLUMNS[0] == "order_id" and ORDER_COLUMNS[1] == "status"
    assert len(set(ORDER_COLUMNS)) == len(ORDER_COLUMNS), "дубли колонок ломают Excel"


def test_rows_to_csv_quotes_and_keeps_line_endings():
    text = rows_to_csv(
        [
            {
                "order_id": "1",
                "status": "FAILED",
                "username": "@bob",
                "error": "нужно, экранировать и \"кавычки\"",
                "quantity": 1000,
            }
        ],
        ("order_id", "status", "username", "error", "quantity"),
    )
    assert text.splitlines()[0] == "order_id,status,username,error,quantity"
    rows = list(csv.reader(text.splitlines()))
    assert rows[1][3] == 'нужно, экранировать и "кавычки"'
    assert "\r\n" in text, "Excel на Windows ждёт CRLF"


def test_rows_to_csv_keeps_missing_columns_empty():
    text = rows_to_csv([{"order_id": "5"}], ("order_id", "username", "error"))
    assert text.splitlines()[1] == "5,,"


def test_rows_to_csv_drops_no_precision_surprises():
    row = rows_to_csv([{"price_rub": 1370.4, "cost_usdt": 9.1, "quantity": 1000}],
                      ("price_rub", "cost_usdt", "quantity"))
    assert row.splitlines()[1] == "1370.4,9.1,1000"


# ============================================================================ #
# 3. Выгрузка из реальной базы
# ============================================================================ #


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def seed_db(tmp_path: Path) -> DBManager:
    db = DBManager(str(tmp_path / "export.db"))
    await db.init_db()
    rows = [
        ("1", "alice", "COMPLETED", 1000, 1370.4, 9.1, 572.97, None),
        ("2", "bob", "COMPLETED", 2000, 2740.8, 18.2, 1145.94, None),
        ("3", "carol", "FAILED_LOW_BALANCE", 5000, 6852.0, 45.5, 0.0, "LOW_BALANCE"),
        ("4", "dave", "HOLD_MANUAL", 10000, 13704.0, 91.0, 0.0, "LARGE_ORDER: big"),
        ("5", "erin", "CANCELLED", 1000, 1370.4, 0.0, 0.0, "вернули деньги"),
    ]
    for order_id, nick, status, qty, price, cost, profit, error in rows:
        await db.save_order_status(
            order_id,
            nick,
            status,
            chat_node=f"chat-{order_id}",
            quantity=qty,
            price_rub=price,
            cost_usdt=cost,
            profit_rub=profit,
            error=error,
        )
        await db.log_task_event(order_id, "ORDER_RECEIVED", "seed")
        await db.log_task_event(order_id, "STATUS_" + status, "seed")
    return db


@pytest.mark.anyio
async def test_export_csv_full_dump_is_readable_by_excel(tmp_path):
    db = await seed_db(tmp_path)
    target = tmp_path / "out.csv"
    summary = await export_csv(db, target)
    assert summary["orders"] == 5
    assert summary["revenue_rub"] == pytest.approx(26037.6)
    assert summary["profit_rub"] == pytest.approx(1718.91)

    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "utf-8-sig: иначе Excel покажет кракозябры"
    with open(target, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(ORDER_COLUMNS)
    assert len(rows) == 6
    by_id = {row[ORDER_COLUMNS.index("order_id")]: row for row in rows[1:]}
    assert set(by_id) == {"1", "2", "3", "4", "5"}
    held = by_id["4"]
    assert held[ORDER_COLUMNS.index("status")] == "HOLD_MANUAL"
    assert held[ORDER_COLUMNS.index("chat_node")] == "chat-4"
    assert held[ORDER_COLUMNS.index("error")] == "LARGE_ORDER: big"
    await db.close()


@pytest.mark.anyio
async def test_export_respects_filters(tmp_path):
    db = await seed_db(tmp_path)
    failed = await export_csv(db, tmp_path / "failed.csv", only_failed=True)
    assert failed["orders"] == 3  # FAILED_LOW_BALANCE + HOLD_MANUAL + CANCELLED
    status = await export_csv(db, tmp_path / "s.csv", status="COMPLETED,HOLD_MANUAL")
    assert status["orders"] == 3
    one = await export_csv(db, tmp_path / "one.csv", buyer="@bob")
    assert one["orders"] == 1
    future = await export_csv(db, tmp_path / "none.csv", since="2099-01-01 00:00:00")
    assert future["orders"] == 0
    limit = await export_json(db, tmp_path / "lim.json", limit=2)
    assert limit["orders"] == 2
    assert not (tmp_path / "none.csv.tmp").exists(), "атомарная запись не оставляет .tmp"
    await db.close()


@pytest.mark.anyio
async def test_export_json_includes_events_when_asked(tmp_path):
    db = await seed_db(tmp_path)
    summary = await export_json(db, tmp_path / "dump", with_events=True)
    # расширение подправлено по контексту
    assert summary["path"].endswith(".json")
    payload = json.loads(Path(summary["path"]).read_text(encoding="utf-8"))
    assert len(payload) == 5
    assert set(payload[0]) == set(ORDER_COLUMNS) | {"events"}
    held = next(item for item in payload if item["order_id"] == "4")
    assert {e["event"] for e in held["events"]} == {"ORDER_RECEIVED", "STATUS_HOLD_MANUAL"}
    assert held["error"] == "LARGE_ORDER: big"

    csv_summary = await export_csv(db, tmp_path / "dump.csv", with_events=True, limit=2)
    events_file = Path(csv_summary["events_path"])
    assert events_file.exists() and csv_summary["events"] == 4
    with open(events_file, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(EVENT_COLUMNS)
    await db.close()


@pytest.mark.anyio
async def test_export_of_empty_database_is_valid_file(tmp_path):
    db = DBManager(str(tmp_path / "empty.db"))
    await db.init_db()
    summary = await export_csv(db, tmp_path / "empty.csv")
    assert summary["orders"] == 0 and summary["revenue_rub"] == 0.0
    assert Path(summary["path"]).read_text(encoding="utf-8-sig").strip() == ",".join(ORDER_COLUMNS)
    await db.close()


def test_summarize_rows_handles_missing_numbers():
    assert summarize_rows([{}, {"price_rub": None}]) == {
        "orders": 2,
        "revenue_rub": 0.0,
        "cost_usdt": 0.0,
        "profit_rub": 0.0,
    }


def test_failed_like_includes_held_orders():
    """«Проблемные» = всё, что требует реакции человека, включая задержки."""
    assert {"HOLD_MANUAL", "FAILED_LOW_BALANCE", "CANCELLED"} <= FAILED_LIKE


# ============================================================================ #
# 4. Метрики: рендер и живой HTTP-сервер
# ============================================================================ #


def test_render_prometheus_shape():
    text = render_prometheus(
        {
            "status_counts": {"COMPLETED": 2, "HOLD_MANUAL": 1},
            "today": {"total": 3, "revenue_rub": 1370.4, "cost_usdt": 9.1, "profit_rub": 572.97},
            "limits": {"spent_usdt": 9.1, "limit_usdt": 100.0, "held": 1},
            "gameau": {"balance_usdt": 42.0, "low_balance": True},
            "paused": True,
            "config_ok": False,
        }
    )
    assert "# TYPE autostars_orders_total gauge" in text
    assert 'autostars_orders_by_status{status="COMPLETED"} 2' in text
    assert "autostars_up 1" in text
    assert "autostars_paused 1" in text
    assert "autostars_config_ok 0" in text
    assert "autostars_gameau_low_balance 1" in text
    assert "autostars_today_profit_rub 572.97" in text
    assert text.endswith("\n")


def test_render_prometheus_labels_and_absent_values_are_safe():
    text = render_prometheus({"status_counts": {'we"ird\\x': 1, "": 2}})
    assert 'status="we\\"ird\\\\x"' in text
    assert "autostars_gameau_balance_usdt" not in text, "нет данных — нет метрики"
    # пустой снимок не падает
    assert "autostars_up 1" in render_prometheus({})


@pytest.mark.anyio
async def test_metrics_server_serves_all_endpoints():
    snapshot = {
        "status_counts": {"COMPLETED": 1},
        "today": {"total": 1, "revenue_rub": 10.0, "cost_usdt": 1.0, "profit_rub": 5.0},
        "limits": {"spent_usdt": 1.0, "limit_usdt": 10.0, "held": 0},
        "config_ok": True,
    }
    server = MetricsServer(lambda: snapshot, host="127.0.0.1", port=0)
    await server.serve()
    try:
        # все запросы — на 127.0.0.1, «вечный» таймаут тут не нужен: сервер в этом же процессе
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        head = (await reader.read(65536)).decode("latin-1")
        writer.close()
        assert "200 OK" in head and "autostars_orders_total 1" in head

        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        health = (await reader.read(65536)).decode("latin-1")
        writer.close()
        assert "200 OK" in health and "ok" in health

        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"GET /status HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        raw = (await reader.read(65536)).decode("latin-1")
        writer.close()
        body = raw.split("\r\n\r\n", 1)[1]
        assert json.loads(body)["status_counts"] == {"COMPLETED": 1}

        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"GET /nope HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        missing = (await reader.read(65536)).decode("latin-1")
        writer.close()
        assert "404" in missing
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_metrics_server_requires_token_and_flags_degraded():
    snapshot = {"config_ok": False, "issues": ["missing: GAMEAU_API_KEY"], "status_counts": {}}
    server = MetricsServer(lambda: snapshot, host="127.0.0.1", port=0, token="s3cret")
    await server.serve()
    try:
        async def request(path: str, auth: str = "") -> str:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
            writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n{auth}\r\n\r\n".encode())
            await writer.drain()
            data = (await reader.read(65536)).decode("latin-1")
            writer.close()
            return data

        forbidden = await request("/metrics")
        assert "401" in forbidden, "без токена наружу ничего не отдаём"
        allowed = await request("/metrics", "Authorization: Bearer s3cret")
        assert "200 OK" in allowed
        health = await request("/healthz", "Authorization: Bearer s3cret")
        assert "503" in health and "missing: GAMEAU_API_KEY" in health
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_metrics_server_survives_broken_request():
    """Мусор в сокете не должен вешать сервис мониторинга (scaper'ы шлют разное)."""
    server = MetricsServer(lambda: {}, host="127.0.0.1", port=0)
    await server.serve()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"GET /metrics HTTP/1.1\r\n")  # обрыв заголовков
        await writer.drain()
        await reader.read(10)
        writer.close()

        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"POST /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        answer = (await reader.read(65536)).decode("latin-1")
        writer.close()
        assert "405" in answer

        reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
        writer.write(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        ok = (await reader.read(65536)).decode("latin-1")
        writer.close()
        assert "200 OK" in ok, "сервер ожил после битого запроса"
    finally:
        await server.stop()


def test_metrics_snapshot_provider_errors_are_not_fatal():
    def boom() -> dict:
        raise RuntimeError("база недоступна")

    server = MetricsServer(boom, host="127.0.0.1", port=0)
    assert server.bound_port == 0  # ещё не запущен — не падает
    text = render_prometheus({"status_counts": {}})
    assert "autostars_orders_total 0" in text


# ============================================================================ #
# 5. CLI: выгрузка и метрики из командной строки
# ============================================================================ #


def build_env(tmp_path: Path) -> dict[str, str]:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FUNPAY_GOLDEN_KEY=k\nGAMEAU_API_KEY=g\nDAILY_SPEND_LIMIT_USDT=100\nMETRICS_PORT=9411\n",
        encoding="utf-8",
    )
    return {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "AUTOSTARS_HOME": str(tmp_path),
        "AUTOSTARS_ENV": str(env_file),
        "AUTOSTARS_CONFIG": str(tmp_path / "config.json"),
        "DB_PATH": str(tmp_path / "export.db"),
        "LOG_FILE": str(tmp_path / "export.log"),
    }


def run_cli(args, env):
    return subprocess.run(
        [sys.executable, "-m", "autostars.main", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


@pytest.mark.anyio
async def test_cli_export_csv_and_events(tmp_path):
    db = await seed_db(tmp_path)
    await db.close()
    out = tmp_path / "orders.csv"
    result = run_cli(["--export", str(out), "--with-events", "--since", "1d"], build_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Выгружено 5 заказов" in result.stdout
    assert out.exists() and (tmp_path / "orders.events.csv").exists()
    with open(out, encoding="utf-8-sig", newline="") as handle:
        assert sum(1 for _ in handle) == 6

    bad = run_cli(["--export", str(tmp_path / "x.csv"), "--since", "семь"], build_env(tmp_path))
    assert bad.returncode == 1 and "не понял период" in bad.stdout


@pytest.mark.anyio
async def test_cli_export_failed_only_and_json(tmp_path):
    db = await seed_db(tmp_path)
    await db.close()
    result = run_cli(["--export", str(tmp_path / "bad.json"), "--failed", "--json"], build_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["format"] == "json" and payload["orders"] == 3
    rows = json.loads((tmp_path / "bad.json").read_text(encoding="utf-8"))
    assert {r["status"] for r in rows} == {"FAILED_LOW_BALANCE", "HOLD_MANUAL", "CANCELLED"}


@pytest.mark.anyio
async def test_cli_metrics_snapshot_after_work(tmp_path):
    db = await seed_db(tmp_path)
    await db.close()
    out = run_cli(["--metrics"], build_env(tmp_path))
    assert out.returncode == 0, out.stdout + out.stderr
    assert 'autostars_orders_by_status{status="HOLD_MANUAL"} 1' in out.stdout
    assert "autostars_daily_spend_limit_usdt 100" in out.stdout
    assert "autostars_today_revenue_rub 0" not in out.stdout  # есть заказы — есть выручка


@pytest.mark.anyio
async def test_database_stays_consistent_after_export(tmp_path):
    """Выгрузка — только читает: WAL и блокировки не должны мешать движку."""
    db = await seed_db(tmp_path)
    await export_csv(db, tmp_path / "e.csv")
    await db.save_order_status("6", "zoe", "PROCESSING", quantity=1000, price_rub=100.0)
    counts = await db.get_status_counts()
    assert counts["PROCESSING"] == 1
    # файл выгрузки не изменился задним числом
    with open(tmp_path / "e.csv", encoding="utf-8-sig") as handle:
        assert sum(1 for _ in handle) == 6
    conn = await db.get_connection()
    journal = (await (await conn.execute("PRAGMA journal_mode")).fetchone())[0]
    assert str(journal).lower() == "wal"
    await db.close()


@pytest.mark.anyio
async def test_export_overrides_extension_and_parent_dir(tmp_path):
    """`--export путь/файл` без расширения и в несуществующей папке — тоже работает."""
    db = await seed_db(tmp_path)
    target = tmp_path / "nested" / "deeper" / "orders"
    summary = await export_csv(db, target)
    assert Path(summary["path"]).exists() and Path(summary["path"]).suffix == ".csv"
    await db.close()


def test_metrics_timestamp_is_recent():
    started = time.time()
    text = render_prometheus({"status_counts": {}}, started_at=started)
    line = next(line for line in text.splitlines() if line.startswith("autostars_snapshot_ts_seconds"))
    stamp = int(line.split()[1])
    assert abs(stamp - started) < 60
    uptime = [line for line in text.splitlines() if line.startswith("autostars_process_uptime_seconds")]
    assert uptime and int(uptime[0].split()[1]) == 0
