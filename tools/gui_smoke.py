"""Смоук GUI без окна: собираем всё дерево виджетов и прогоняем логику вкладки.

Зачем: flet-интерфейс — самый «недостающий» слой в CI, потому что окно в
headless-среде не открыть. При этом 90% поломок GUI — это не «окно не открылось»,
а «код вкладки упал при построении» (изменился API flet, опечатка в параметре).
Этот скрипт ловит ровно это: он собирает каждую вкладку, рендерит таблицы и
статус, но не запускает flet.run().

    python tools/gui_smoke.py          # выход 0, если GUI собирается
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        import flet as ft  # noqa: F401
    except ImportError:
        print("flet не установлен — пропускаем (pip install -e '.[gui]')")
        return 0

    from autostars.gui import bridge
    from autostars.gui.app import AutoStarsApp

    app = AutoStarsApp(process=bridge.EngineProcess())
    failures: list[str] = []

    tabs = [
        "build_dashboard",
        "build_stats",
        "build_orders",
        "build_delivery",
        "build_settings",
        "build_logs",
        "build_diag",
    ]
    for name in tabs:
        try:
            control = getattr(app, name)()
            assert control is not None, "вкладка вернула None"
            print(f"  ok  {name:<16} → {type(control).__name__}")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  FAIL {name:<16} → {type(exc).__name__}: {exc}")

    # рендер данных (таблицы/счётчики) без реального окна
    try:
        app._render_orders(
            [
                {
                    "order_id": "1",
                    "username": "user",
                    "quantity": 1000,
                    "price_rub": 1370.4,
                    "cost_usdt": 9.1,
                    "profit_rub": 570.0,
                    "status": "COMPLETED",
                },
                {"order_id": "2", "username": None, "status": "WAITING_USERNAME"},
            ]
        )
        app._render_stats({"windows": [{"label": "Всего", "orders_total": 2, "stars": 1000}], "active_rate": 87.63})
        app._render_delivery(
            {
                "held": [{"order_id": "7", "username": "@dave", "quantity": 10000, "price_rub": 13704.0,
                          "error": "LARGE_ORDER: выручка больше лимита"}],
                "spent": 163.8,
                "limit": 100.0,
                "flags": [{"username": "@mallory", "note": "кидалово", "created_at": "2026-09-18 12:00:00"}],
                "max_order": 5000.0,
                "min_margin": 12.0,
                "duplicates": 15,
                "duplicate_action": "alert",
                "blacklist": True,
                "mute": "23-07",
            }
        )
        app._render_delivery({"held": [], "spent": None, "limit": 0.0, "flags": [], "max_order": 0.0,
                              "min_margin": 0.0, "duplicates": 0, "duplicate_action": "alert",
                              "blacklist": False, "mute": ""})
        app.refresh_status()
        print("  ok  рендер таблиц и статуса")
    except Exception as exc:
        failures.append(f"render: {type(exc).__name__}: {exc}")
        print(f"  FAIL рендер: {type(exc).__name__}: {exc}")

    # настройки: валидация и «план» изменений не должны падать на пустых полях
    try:
        from autostars.gui import settings

        collected = app._collect_settings()
        settings.validate(collected)
        settings.plan_updates(collected, settings.read_settings())
        print(f"  ok  поля настроек: {len(collected)}")
    except Exception as exc:
        failures.append(f"settings: {type(exc).__name__}: {exc}")
        print(f"  FAIL настройки: {type(exc).__name__}: {exc}")

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".env").write_text("POLL_INTERVAL=5\n", encoding="utf-8")

    if failures:
        print(f"\nGUI-смоук: ПРОВАЛЕНО {len(failures)}")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nGUI-смоук: OK — интерфейс собирается без ошибок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
