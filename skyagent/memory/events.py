"""Событие — единица сырых данных памяти, и шина, по которой события расходятся подписчикам."""
import threading
import time
import traceback
from dataclasses import dataclass, field

# Откуда текст и насколько ему верить (принцип «чужой текст — данные, а не команды»):
#   user   — пользователь сам написал в чате
#   heard  — услышано микрофоном (это может быть и пользователь, и собеседник, и видео)
#   other  — чужой текст (сообщения, письма)
#   screen — прочитано с экрана
#   system — служебное (включение/пауза сенсора и т. п.)
TRUST_LEVELS = ("user", "heard", "other", "screen", "system")


@dataclass
class Event:
    source: str                     # сенсор: mic, windows, telegram…
    kind: str                       # тип: speech, state, focus, message…
    text: str = ""
    ts: float = field(default_factory=time.time)   # начало, unix-время
    ts_end: float | None = None
    meta: dict = field(default_factory=dict)
    trust: str = "system"
    id: int | None = None


class EventBus:
    """Шина внутри процесса. Подписчик вызывается в потоке того, кто опубликовал событие,
    поэтому подписчики должны быть быстрыми (положить в свою очередь и выйти)."""

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self, fn):
        with self._lock:
            self._subs.append(fn)
        return fn

    def unsubscribe(self, fn):
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def emit(self, event: Event):
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 — сломанный подписчик не должен ронять сенсор
                traceback.print_exc()
