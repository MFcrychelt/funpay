#!/usr/bin/env python3
"""AutoStars — Асинхронный бот автовыдачи Telegram Stars на FunPay через Gameau API.

Использование:
    python main.py                # бесконечный асинхронный цикл автовыдачи
    python main.py --once         # один прогон по текущей очереди заказов
    python main.py --check        # проверить конфиг и подключение
    python main.py --catalog      # просмотр каталога Gameau с ценами
    python main.py --calc 1370 1000  # калькулятор прибыли
    python main.py --deposit-info # инструкция по заводу крипты
    python main.py --test-order durov  # тестовый заказ
    python gui.py                 # GUI панель управления
"""

import sys

from autostars.main import main

if __name__ == "__main__":
    sys.exit(main() or 0)
