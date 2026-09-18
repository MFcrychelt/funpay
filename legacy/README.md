# legacy/ — историческая реализация (не поддерживается)

Здесь лежит код, из которого когда-то вырос AutoStars: вторая, синхронная и
«одноклассовая» реализация той же автовыдачи Stars.

| Файл | Что это |
|---|---|
| `main.py` | async-бот на `FunPayAPI` + `aiohttp`, конфигурация `config.json`, учёт в `db.json` |
| `bot/` | синхронный `DeliveryEngine` на `requests` (тот же пайплайн: парсинг → GAMEAU → ответ покупателю) |
| `gui.py` | первый GUI на flet, который запускал `main.StarBot` |
| `config.example.json` | схема конфига legacy-версии (вложенные секции API/FUNPAY/BOT/FINANCE) |
| `tests/` | тесты парсеров и `bot/DeliveryEngine` (не требуют сети) |
| `photo.png` | картинка из «О проекте» старого GUI |

## Почему это в отдельной папке, а не в корне

В корне `main.py` + `bot/` + `autostars/` давали **три независимые реализации
одного и того же** с разными конфигами и разными хранилищами (`db.json` и
`autostars.db`). Практические последствия, из-за которых их и разнесли:

- GUI запускал старый `main.StarBot`, а не боевой движок: «в окне одно, в логах другое»;
- учёт сделок шёл в два разных хранилища, статика и отчёты расходились;
- `requirements.txt` должен был тянуть зависимости всех трёх версий (и ломался).

## Поддерживается ли это

Нет. Здесь нет ни reconciliation, ни журнала задач, ни идемпотентных ретраев,
ни статистики — `autostars/` умеет всё это, и развивать надо его.
Код оставлен, чтобы:

1. можно было сравнить поведение при переносе старого `db.json`;
2. старые инструкции («запусти main.py») не вели в никуда.

## Если всё-таки нужно запустить

```bash
pip install -r legacy/requirements.txt
pip install --no-deps "FunPayAPI>=1.0.5"

python legacy/main.py            # старый цикл (конфиг: config.json)
python legacy/gui.py             # старый GUI (flet)
python -m pytest legacy/tests -q # тесты legacy (сеть не нужна)
```

## Как перенести историю сделок из `db.json` в боевую базу

```python
# переносит legacy-транзакции в таблицу orders движка
import json, asyncio
from autostars.database.db_manager import DBManager

db = DBManager("autostars.db")
asyncio.run(db.init_db())
for tx in json.load(open("db.json", encoding="utf-8")).get("transactions", []):
    asyncio.run(db.save_order_status(
        order_id=tx["order_id"], username=tx.get("username"), status="COMPLETED",
        quantity=tx.get("stars"), price_rub=tx.get("revenue_rub"),
        cost_usdt=tx.get("cost_usdt"), cost_rub=tx.get("cost_rub"),
        profit_rub=tx.get("profit_rub"),
    ))
```

(Однократная операция: `save_order_status` идемпотентна по `order_id`.)
