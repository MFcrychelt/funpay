"""Смоук-тесты CLI: команды должны запускаться и отвечать кодами, а не трейсбеком.

CLI — это то, чем пользователь проверяет конфигурацию ДО первого реального заказа
(``--check``, ``--calc``, ``--deposit-info``) и то, что дёргает GUI через мост.
Тесты идут через настоящий ``python -m autostars.main`` в изолированной папке
(AUTOSTARS_HOME), без сети и без боевых ключей: так проверяется и упаковочный
путь (sys.path), и коды возврата, и то, что опасные команды не выполняются молча.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BASE_ENV = {
    "FUNPAY_GOLDEN_KEY": "fake-key-for-tests",
    "GAMEAU_API_KEY": "fake-gameau-key",
    "EXCHANGE_VARIANT": "2",
    "RATE_VARIANT_1": "110",
    "RATE_VARIANT_2": "87.63",
    "USDT_PER_1000_STARS": "9.10",
    "DB_PATH": "autostars.db",
    "LOG_FILE": "autostars.log",
}


def run_cli(*args: str, home: Path | None = None, env: dict[str, str] | None = None,
            timeout: float = 60.0, base: bool = True) -> subprocess.CompletedProcess:
    """Запуск движка как отдельного процесса (тот же путь, что у install.bat/GUI).

    base=False — «боевые» ключи не подмешиваются: так проверяется поведение
    при незаполненной конфигурации.
    """
    environ = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "AUTOSTARS_HOME": str(home) if home else str(Path.cwd()),
        "AUTOSTARS_CONFIG": str((home or Path.cwd()) / "config.json"),
        **(BASE_ENV if base else {}),
        **(env or {}),
    }
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MAX_CONCURRENT_ORDERS", "AUTOSTARS_ENV"):
        environ.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "autostars.main", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ROOT),
        env=environ,
    )


@pytest.fixture
def home(tmp_path):
    (tmp_path / ".env").write_text(
        "# тестовая конфигурация\n" + "\n".join(f"{k}={v}" for k, v in BASE_ENV.items()) + "\n",
        encoding="utf-8",
    )
    return tmp_path


def test_version(home):
    result = run_cli("--version", home=home)
    assert result.returncode == 0
    assert result.stdout.strip().startswith("AutoStars ")


def test_help_lists_flags_without_network(home):
    result = run_cli("--help", home=home)
    assert result.returncode == 0
    for flag in ("--once", "--check", "--calc", "--stats", "--report", "--timeline",
                 "--test-order", "--dry-run", "--config-show", "--json", "--pause", "--resume"):
        assert flag in result.stdout, f"в --help нет флага {flag}"


def test_unknown_flag_exits_2(home):
    result = run_cli("--definitely-not-a-flag", home=home)
    assert result.returncode == 2
    assert "unrecognized arguments" in (result.stderr + result.stdout)


def test_check_reports_config_and_financial_model(home):
    """`--check` печатает локальную диагностику даже когда сети нет (код возврата — за сетью)."""
    result = run_cli("--check", home=home, timeout=120)
    out = result.stdout + result.stderr
    assert "ДИАГНОСТИКА СИСТЕМЫ AUTOSTARS" in out, out
    assert "[OK]   Конфигурация" in out
    assert "активный: вариант 2 → 87.63 ₽/USDT" in out, out
    assert "Лимит maxCharge" in out and "Трекинг" in out
    assert "Traceback" not in out


def test_check_fails_loudly_without_keys(tmp_path):
    (tmp_path / ".env").write_text("FUNPAY_GOLDEN_KEY=paste_your_funpay_golden_key_here\n", encoding="utf-8")
    result = run_cli("--check", home=tmp_path, base=False, env={"FUNPAY_GOLDEN_KEY": "", "GAMEAU_API_KEY": ""},
                     timeout=120)
    out = result.stdout + result.stderr
    assert result.returncode != 0
    assert "GAMEAU_API_KEY" in out
    assert "traceback" not in out.lower()


def test_creates_env_from_example_on_first_run(tmp_path):
    result = run_cli("--check", home=tmp_path, base=False, timeout=120)
    env_file = tmp_path / ".env"
    assert env_file.exists(), "пустая папка должна получить .env из .env.example"
    assert "FUNPAY_GOLDEN_KEY" in env_file.read_text(encoding="utf-8")
    assert result.returncode != 0, "первый запуск нельзя считать успехом"
    assert "заполните" in (result.stdout + result.stderr).lower()
    # --check при незаполненных ключах всё равно печатает диагностику, а не «тихий» выход
    assert "ДИАГНОСТИКА СИСТЕМЫ AUTOSTARS" in result.stdout


def test_first_run_of_once_exits_nonzero(tmp_path):
    """`--once` на пустой папке: создал .env, сказал что делать, вышел с ошибкой."""
    result = run_cli("--once", home=tmp_path, base=False, timeout=60)
    assert (tmp_path / ".env").exists()
    assert result.returncode == 1
    assert "GAMEAU_API_KEY" in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_calc_compares_both_rate_variants(home):
    result = run_cli("--calc", "1370.4", "1000", home=home)
    out = result.stdout
    assert result.returncode == 0, out
    assert "КАЛЬКУЛЯТОР ПРИБЫЛИ" in out
    assert "1001.00 ₽" in out  # 9.10 USDT × 110
    assert "797.43 ₽" in out  # 9.10 USDT × 87.63
    assert "экономит" in out


def test_config_show_never_leaks_secrets(home):
    result = run_cli("--config-show", home=home)
    out = result.stdout + result.stderr
    assert result.returncode == 0
    assert "fake-key-for-tests" not in out
    assert "fake-gameau-key" not in out
    assert "golden_key" in out and "gameau_key" in out  # параметры видны, значения — нет


def test_stats_json_is_machine_readable(home):
    result = run_cli("--stats", "--json", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            payload = json.loads(line)
            break
    assert payload, "в stdout нет JSON-строки"
    labels = [w["label"] for w in payload["windows"]]
    assert "1 час" in labels and "Всего" in labels
    assert payload["active_rate"] == pytest.approx(87.63)
    assert payload["windows"][0]["orders_total"] == 0


def test_stats_and_report_work_on_empty_database(home):
    for args in (("--stats",), ("--report",), ("--tasks",)):
        result = run_cli(*args, home=home)
        assert result.returncode == 0, f"{args}: {result.stdout}{result.stderr}"


def test_timeline_unknown_order_exits_1(home):
    result = run_cli("--timeline", "NOPE-123", home=home)
    assert result.returncode == 1
    assert "не найден" in result.stdout + result.stderr


def test_pause_and_resume_toggle_persisted_flag(home):
    db = home / "autostars.db"
    paused = run_cli("--pause", home=home)
    assert paused.returncode == 0, paused.stdout + paused.stderr
    assert db.exists()
    import sqlite3

    with sqlite3.connect(db) as conn:
        value = conn.execute("SELECT value FROM settings WHERE key='bot_paused'").fetchone()
    assert value and str(value[0]) == "1"

    resumed = run_cli("--resume", home=home)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    with sqlite3.connect(db) as conn:
        value = conn.execute("SELECT value FROM settings WHERE key='bot_paused'").fetchone()
    assert value and str(value[0]) == "0"


def test_test_order_requires_explicit_confirmation(home):
    """Без --yes и --dry-run тестовая выдажа не запускается — и это видно по коду возврата."""
    result = run_cli("--test-order", "tester", "--test-qty", "100", home=home)
    assert result.returncode == 2
    assert "Подтвердите" in result.stdout + result.stderr
    assert "POST" not in result.stdout  # ничего не отправляли


def test_test_order_dry_run_costs_only(home):
    result = run_cli("--test-order", "1000", "--dry-run", home=home)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "dry-run" in out.lower() or "реальная выдача не выполнена" in out
    assert "maxCharge" in out or "MAXCHARGE" in out.upper()


def test_deposit_info_without_network_fails_cleanly(home):
    result = run_cli("--deposit-info", home=home, timeout=90)
    out = result.stdout + result.stderr
    assert "traceback" not in out.lower()
    if result.returncode != 0:
        assert "Не удалось" in out or "ошибк" in out.lower()


def test_once_without_required_keys_exits_1(tmp_path):
    (tmp_path / ".env").write_text("POLL_INTERVAL=1\n", encoding="utf-8")
    monkey_home = tmp_path
    result = run_cli("--once", home=monkey_home, env={"FUNPAY_GOLDEN_KEY": "", "GAMEAU_API_KEY": ""})
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "GAMEAU_API_KEY" in out and "FUNPAY_GOLDEN_KEY" in out
    assert "Traceback" not in out


def test_json_flag_with_report(home):
    result = run_cli("--report", "--json", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    text = result.stdout
    line = next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip().startswith("{")), "")
    assert line, "ожидался JSON-отчёт"
    assert json.loads(line)


def test_catalog_and_balance_report_errors_without_traceback(home):
    for args in (("--catalog",), ("--balance",)):
        result = run_cli(*args, home=home, timeout=90)
        out = result.stdout + result.stderr
        assert "Traceback" not in out, out[-800:]
        assert "GAMEAU" in out or "FunPay" in out


# ============================================================================ #
# v2.4: политика выдачи, ручная выдача, стоп-лист, выгрузка, метрики
# ============================================================================ #

V24_FLAGS = ("--held", "--release", "--cancel", "--order", "--limits", "--blacklist",
             "--customers", "--export", "--since", "--metrics", "--templates", "--with-events")


def test_help_lists_v24_flags(home):
    result = run_cli("--help", home=home)
    assert result.returncode == 0
    missing = [flag for flag in V24_FLAGS if flag not in result.stdout]
    assert not missing, f"в --help нет флагов {missing}"


def test_local_policy_commands_run_without_keys(tmp_path):
    """Локальные команды (БД + конфиг) не требуют боевых ключей и не падают."""
    (tmp_path / ".env").write_text("POLL_INTERVAL=5\nDAILY_SPEND_LIMIT_USDT=500\n", encoding="utf-8")
    for args in (("--limits",), ("--held",), ("--templates",), ("--customers",), ("--blacklist", "list")):
        result = run_cli(*args, home=tmp_path, base=False,
                         env={"FUNPAY_GOLDEN_KEY": "", "GAMEAU_API_KEY": ""})
        out = result.stdout + result.stderr
        assert result.returncode == 0, f"{args}: {out[-500:]}"
        assert "Traceback" not in out


def test_limits_json_shape(home):
    result = run_cli("--limits", "--json", home=home,
                     env={"MAX_ORDER_REVENUE_RUB": "4000", "MIN_MARGIN_PCT": "18"})
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["limits"]["max_order_revenue_rub"] == 4000.0
    assert payload["limits"]["min_margin_pct"] == 18.0
    assert payload["spent_today_usdt"] == 0.0
    assert payload["held"] == 0


def test_release_and_cancel_refuse_without_confirmation(home):
    """Опасные команды обязаны тормозить: нет заказа → 1, нет -y → 2, и всегда без трейсбека."""
    for args in (("--release", "NOPE"), ("--cancel", "NOPE")):
        result = run_cli(*args, home=home)
        out = result.stdout + result.stderr
        assert "Traceback" not in out, out[-500:]
        assert result.returncode in (1, 2), f"{args}: {out[-300:]}"


def test_manual_order_dry_run_is_free(home):
    result = run_cli("--order", "@someone", "1500", "--dry-run", home=home)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "Ручная выдача" in out
    assert "1 500" in out or "1500" in out, out[-300:]
    assert "НЕ отправлялся" in out


def test_manual_order_without_confirmation_is_refused(home):
    result = run_cli("--order", "@someone", home=home, env={"DEFAULT_STARS_QUANTITY": "1000"})
    out = result.stdout + result.stderr
    assert result.returncode in (1, 2), out[-400:]
    assert "Traceback" not in out


def test_blacklist_roundtrip_via_cli(home):
    assert run_cli("--blacklist", "add", "@spammer", "спам", "в", "личке", home=home).returncode == 0
    listing = run_cli("--blacklist", "list", "--json", home=home)
    rows = json.loads(listing.stdout)
    assert [r["username"] for r in rows] == ["@spammer"]
    assert rows[0]["note"] == "спам в личке"
    assert run_cli("--blacklist", "remove", "spammer", home=home).returncode == 0
    assert json.loads(run_cli("--blacklist", "list", "--json", home=home).stdout) == []


def test_export_creates_csv_and_json(home, tmp_path):
    csv_path = tmp_path / "dump.csv"
    result = run_cli("--export", str(csv_path), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert csv_path.exists()
    text = csv_path.read_text(encoding="utf-8-sig")
    assert text.splitlines()[0].startswith("order_id,status,username")
    assert "Выгружено 0 заказов" in result.stdout

    json_path = tmp_path / "dump.json"
    assert run_cli("--export", str(json_path), "--json", home=home).returncode == 0
    assert json.loads(json_path.read_text(encoding="utf-8")) == []


def test_export_bad_period_reports_human_error(home, tmp_path):
    result = run_cli("--export", str(tmp_path / "x.csv"), "--since", "на прошлой неделе", home=home)
    assert result.returncode == 1
    out = result.stdout + result.stderr
    assert "не понял период" in out and "Traceback" not in out


def test_metrics_snapshot_is_prometheus(home):
    result = run_cli("--metrics", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    text = result.stdout
    assert "autostars_up 1" in text
    assert "# TYPE autostars_orders_total gauge" in text
    assert "autostars_daily_spend_limit_usdt 0" in text
