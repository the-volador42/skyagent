"""Личный дневник: записи, которые пишешь ты сам (в CLI — /note, в мини-окне, голосом через агента).

Каждая запись — событие хронологии source=journal, kind=entry, trust=user: её находит поиск и автоконтекст,
по ней строится дневник journal. Ещё один способ писать — любым редактором в файл
data/diaries/journal-inbox.txt: раз в минуту записи из него переносятся в дневник, а файл очищается
(записи разделяются пустой строкой; строка «ЧЧ:ММ текст» — запись на это время сегодня).
"""
import re
import threading
from datetime import date, datetime
from pathlib import Path

from .events import Event

SOURCE, KIND = "journal", "entry"
INBOX_HINT = ("# Пиши сюда что угодно — раз в минуту записи уйдут в личный дневник, а файл очистится.\n"
              "# Записи разделяй пустой строкой. «14:30 текст» — запись на 14:30 сегодня. Строки с # не попадут.\n")
TIME_PREFIX = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s+(.+)$", re.S)


class Journal:
    def __init__(self, memory):
        self.memory = memory
        self.tl = memory.timeline
        self.inbox = Path(memory.data_dir) / "diaries" / "journal-inbox.txt"
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        if not self.inbox.exists():
            self.inbox.write_text(INBOX_HINT, encoding="utf-8")
        self._lock = threading.Lock()

    def add(self, text, ts=None, via="cli"):
        text = (text or "").strip()
        if not text:
            raise ValueError("пустая запись")
        return self.memory.publish(Event(SOURCE, KIND, text, ts=ts or datetime.now().timestamp(),
                                         trust="user", meta={"via": via}))

    def entries(self, start=None, end=None):
        return self.tl.range(start, end, SOURCE, (KIND,))

    def day(self, d: date):
        s = datetime.combine(d, datetime.min.time()).timestamp()
        return self.entries(s, s + 86400 - 0.001)

    def _own(self, eid):
        rows = self.tl.get([int(eid)])
        return rows[0] if rows and rows[0]["source"] == SOURCE else None

    def edit(self, eid, text):
        r = self._own(eid)
        if not r or not text.strip():
            return False
        self.tl.update_text(r["id"], text.strip(), meta={**r["meta"], "edited": datetime.now().timestamp()})
        return True

    def delete(self, eid):
        r = self._own(eid)
        if not r:
            return False
        self.tl.delete_ids([r["id"]])
        return True

    def import_inbox(self):
        """Перенести записи из journal-inbox.txt в дневник. Возвращает число новых записей."""
        with self._lock:
            try:
                raw = self.inbox.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                raw = ""
            body = "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("#")).strip()
            if not body:
                return 0
            n = 0
            for chunk in re.split(r"\n\s*\n", body):
                chunk = chunk.strip()
                if not chunk:
                    continue
                ts = None
                m = TIME_PREFIX.match(chunk)
                if m and int(m[1]) < 24 and int(m[2]) < 60:
                    ts = datetime.combine(date.today(), datetime.min.time()).replace(
                        hour=int(m[1]), minute=int(m[2])).timestamp()
                    chunk = m[3].strip()
                self.add(chunk, ts=ts, via="file")
                n += 1
            self.inbox.write_text(INBOX_HINT, encoding="utf-8")
            return n
