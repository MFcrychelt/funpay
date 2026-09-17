#!/usr/bin/env python3
"""AutoStars — Асинхронный бот автовыдачи Telegram Stars на FunPay через Gameau API.

Использование:
    python main.py            # бесконечный асинхронный цикл автовыдачи
    python main.py --once     # один прогон по текущей очереди заказов
    python main.py --check    # проверить конфиг и подключение к FunPay / Gameau
    python main.py --test-order durov --test-qty 50  # тестовый заказ
"""

import sys
from autostars.main import main

if __name__ == "__main__":
    main()
