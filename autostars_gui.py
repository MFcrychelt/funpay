"""Точка входа GUI-приложения (удобно и для PyInstaller, и без установки пакета).

    python autostars_gui.py       # то же самое, что python -m autostars.gui
"""

from __future__ import annotations

import sys
from pathlib import Path

# запуск «из папки»: пакет должен быть импортируемым без pip install
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    try:
        from autostars.gui.app import main as gui_main
    except ImportError as exc:  # flet не установлен
        print(f"GUI требует flet. Установите: {sys.executable} -m pip install -e \".[gui]\"\n({exc})")
        raise SystemExit(2) from exc
    gui_main()


if __name__ == "__main__":
    main()
