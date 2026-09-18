"""Персистентное состояние заказов.

Хранится в `state.json` рядом с точкой запуска. Нужно для того, чтобы:
    * не выдать один и тот же заказ дважды после перезапуска;
    * переиспользовать тот же `Idempotency-Key` при повторе запроса к GAMEAU;
    * не спамить покупателя повторными просьбами юзернейма.

Файл пишется атомарно (запись во временный файл + переименование).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Статусы обработки заказа ботом.
STATUS_NEW = "new"                      # заказ замечен, юзернейм ищем
STATUS_WAITING_USERNAME = "waiting"     # ждём юзернейм от покупателя
STATUS_SENDING = "sending"              # звёзды заказаны в GAMEAU
STATUS_DONE = "done"                    # звёзды отправлены, покупателю отвечено
STATUS_FAILED = "failed"                # неустранимая ошибка (нужно участие продавца)
STATUS_NO_STARS_RULE = "no_rule"        # не найдено правило количества звёзд


class State:
    """Потокобезопасное хранилище состояния в JSON-файле."""

    def __init__(self, path: str | Path = "state.json"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"orders": {}, "meta": {}}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
                self._data.setdefault("orders", {})
                self._data.setdefault("meta", {})
            except Exception:  # noqa: BLE001
                # Битый файл не должен ронять бота — начинаем заново,
                # старую версию сохраняем рядом для разбора.
                backup = self.path.with_suffix(f".broken-{int(time.time())}.json")
                try:
                    self.path.rename(backup)
                except OSError:
                    pass
                self._data = {"orders": {}, "meta": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.path)
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ #
    # Работа с заказами
    # ------------------------------------------------------------------ #
    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._data["orders"].get(order_id)
            return dict(rec) if rec else None

    def upsert_order(self, order_id: str, **fields: Any) -> dict[str, Any]:
        """Создаёт/обновляет запись заказа и сразу сохраняет файл."""
        with self._lock:
            rec = self._data["orders"].setdefault(
                order_id,
                {
                    "order_id": order_id,
                    "status": STATUS_NEW,
                    "created_ts": time.time(),
                    "idempotency_key": None,
                    "gameau_order_id": None,
                    "username": None,
                    "stars": None,
                },
            )
            rec.update(fields)
            rec["updated_ts"] = time.time()
            self._save()
            return dict(rec)

    def all_orders(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data["orders"].items()}

    def forget(self, order_id: str) -> None:
        with self._lock:
            if order_id in self._data["orders"]:
                del self._data["orders"][order_id]
                self._save()

    def prune(self, max_age_days: float = 30.0) -> int:
        """Удаляет старые завершённые записи, чтобы файл не рос бесконечно."""
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        with self._lock:
            for order_id in list(self._data["orders"]):
                rec = self._data["orders"][order_id]
                if rec.get("status") in (STATUS_DONE,) and rec.get("updated_ts", 0) < cutoff:
                    del self._data["orders"][order_id]
                    removed += 1
            if removed:
                self._save()
        return removed
