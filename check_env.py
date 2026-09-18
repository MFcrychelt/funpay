"""Предполётная проверка окружения AutoStars (без сторонних зависимостей).

Запуск:  python check_env.py [--net]

Проверяет то, из-за чего бот обычно «не стартует»:
  • версия Python;
  • наличие и версии зависимостей ядра (httpx, aiosqlite, beautifulsoup4);
  • наличие GUI-зависимости flet (опционально);
  • конфигурация: .env / config.json, обязательные ключи;
  • доступность каталога для БД и лога (запись);
  • синтаксис .env (числовые значения) — до падения цикла;
  • с --net: доступность FunPay, GAMEAU и Telegram API (HEAD/GET, без ключей).

Код возврата 0 — всё готово, 1 — что-то нужно починить.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
CORE_DEPS = (("httpx", "httpx"), ("aiosqlite", "aiosqlite"), ("bs4", "beautifulsoup4"))
GUI_DEPS = (("flet", "flet"),)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

OK, FAIL, WARN = "[ OK ]", "[FAIL]", "[WARN]"
_results: list[tuple[str, str]] = []


def report(status: str, message: str) -> None:
    print(f"{status} {message}")
    _results.append((status, message))


def check_python() -> None:
    cur = sys.version_info[:3]
    if cur >= MIN_PYTHON:
        report(OK, f"Python {cur[0]}.{cur[1]}.{cur[2]} ({sys.executable})")
    else:
        need = ".".join(map(str, MIN_PYTHON))
        report(FAIL, f"Python {cur[0]}.{cur[1]} слишком старый, нужно >= {need}")


def _dist_version(module: str) -> str:
    try:
        from importlib.metadata import version

        return version(module)
    except Exception:
        return "?"


def check_deps(deps) -> bool:
    ok = True
    for import_name, dist_name in deps:
        found = importlib.util.find_spec(import_name) is not None
        if found:
            report(OK, f"{dist_name} {_dist_version(dist_name)}")
        else:
            ok = False
            report(FAIL, f"{dist_name} не установлен — pip install -r requirements.txt")
    return ok


def check_gui() -> None:
    for import_name, dist_name in GUI_DEPS:
        if importlib.util.find_spec(import_name) is None:
            report(WARN, f"{dist_name} не установлен — GUI недоступен (pip install -e '.[gui]')")
        else:
            report(OK, f"{dist_name} {_dist_version(dist_name)} (GUI)")


def check_config() -> None:
    from autostars.config import Config, env_path

    env = env_path()
    cfg = Config.load(ROOT / "config.json")
    if not env.exists() and not (ROOT / "config.json").exists():
        report(FAIL, f"нет конфигурации: создайте {env.name} из .env.example")
    else:
        report(OK, f"конфигурация: {env if env.exists() else ROOT / 'config.json'}")

    for problem in cfg.issues():
        if problem.startswith("missing:"):
            report(FAIL, f"не задан {problem[len('missing:') :].strip()}")
        else:
            report(WARN, problem)

    if env.exists():
        try:
            env.read_text(encoding="utf-8")
            report(OK, f"{env.name} читается ({len(env.read_text(encoding='utf-8').splitlines())} строк)")
        except OSError as exc:
            report(FAIL, f"{env.name} не читается: {exc}")


def check_storage() -> None:
    from autostars.config import Config

    cfg = Config.load(ROOT / "config.json")
    for label, raw in (("DB_PATH", cfg.db_path), ("LOG_FILE", cfg.log_file)):
        target = Path(raw)
        target = target if target.is_absolute() else ROOT / target
        parent = target.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            probe = parent / ".autostars-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            report(OK, f"{label} доступен для записи: {target}")
        except OSError as exc:
            report(FAIL, f"{label}: {parent} недоступен для записи ({exc})")


def check_network() -> None:
    """Без ключей: только проверяем, что хосты резолвятся и отвечают."""
    targets = {
        "FunPay": "https://funpay.com/orders/trade",
        "GAMEAU API": "https://gameau.us/api-docs.html",
        "Telegram API": "https://api.telegram.org",
    }
    for name, url in targets.items():
        try:
            import urllib.request

            req = urllib.request.Request(url, headers={"User-Agent": "AutoStars-CheckEnv/2.3"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                report(OK, f"{name}: HTTP {resp.status}")
        except Exception as exc:
            report(FAIL, f"{name}: {exc}")


def check_imports() -> None:
    try:
        from autostars import __version__
        from autostars.main import build_parser  # noqa: F401

        report(OK, f"пакет autostars импортируется (версия {__version__})")
    except Exception as exc:
        report(FAIL, f"пакет autostars не импортируется: {type(exc).__name__}: {exc}")


def check_cli() -> None:
    """--help должен работать даже без ключей."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "autostars.main", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT),
            env={**os.environ},
        )
        if proc.returncode == 0:
            report(OK, "python -m autostars.main --help")
        else:
            report(FAIL, f"CLI вернул {proc.returncode}: {proc.stderr.strip()[:200]}")
    except Exception as exc:
        report(FAIL, f"CLI не запускается: {exc}")


def main() -> int:
    check_network_enabled = "--net" in sys.argv[1:]

    print("=" * 68)
    print("  AutoStars — проверка окружения")
    print("=" * 68)
    print("\n— Python и зависимости —")
    check_python()
    core_ok = check_deps(CORE_DEPS)
    check_gui()

    print("\n— Код —")
    if core_ok:
        check_imports()
        check_cli()
    else:
        report(WARN, "пропуск проверки импортов: нет зависимостей ядра")

    print("\n— Конфигурация и хранилище —")
    if core_ok:
        check_config()
        check_storage()
    else:
        report(WARN, "пропуск проверки конфигурации: нет зависимостей ядра")

    if check_network_enabled:
        print("\n— Сеть (--net) —")
        check_network()

    failed = [m for s, m in _results if s == FAIL]
    print("\n" + "=" * 68)
    if failed:
        print(f"  НЕ ГОТОВО: проблем {len(failed)}")
        for m in failed:
            print(f"   - {m}")
        return 1
    warned = sum(1 for s, _ in _results if s == WARN)
    print("  ГОТОВО К ЗАПУСКУ: python -m autostars.main" + (f"  (предупреждений: {warned})" if warned else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
