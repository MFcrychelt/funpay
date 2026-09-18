"""Точка входа CLI/цикла автовыдачи (для PyInstaller и для запуска «из папки»).

    python autostars_bot.py --check
    python autostars_bot.py            # живой цикл
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autostars.main import main

if __name__ == "__main__":
    main()
