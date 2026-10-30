"""Хронология — источник истины памяти: все события в одной SQLite-базе.

Полнотекстовый поиск — FTS5, поиск по смыслу — векторы в sqlite-vec.
Векторы считаются фоном (см. embed.py), поэтому у свежих событий их может ещё не быть.
"""
import json
import re
import sqlite3
import threading

import sqlite_vec

from .events import Event

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    ts       REAL NOT NULL,
    ts_end   REAL,
    source   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    text     TEXT NOT NULL DEFAULT '',
    meta     TEXT NOT NULL DEFAULT '{}',
    trust    TEXT NOT NULL DEFAULT 'system',
    embedded INTEGER NOT NULL DEFAULT 0      -- 0 ждёт вектора, 1 есть, 2 не нужен
);
CREATE INDEX IF NOT EXISTS events_ts     ON events(ts);
CREATE INDEX IF NOT EXISTS events_src_ts ON events(source, ts);
CREATE INDEX IF NOT EXISTS events_wait   ON events(embedded) WHERE embedded = 0;

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

# Поисковые индексы (создаются и мигрируют в Timeline._migrate):
#   events_fts — слова (unicode61), events_tri — триграммы (кусочки по 3 буквы: устойчивы к окончаниям
#   и к части ошибок распознавания). Оба индексируют text и tags (теги проставляет фаза сна).
FTS_SCHEMA = """
CREATE VIRTUAL TABLE events_fts USING fts5(
    text, tags, content = 'events', content_rowid = 'id', tokenize = 'unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE events_tri USING fts5(
    text, tags, content = 'events', content_rowid = 'id', tokenize = 'trigram');
CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
    INSERT INTO events_tri(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
    INSERT INTO events_tri(events_tri, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER events_au AFTER UPDATE OF text, tags ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
    INSERT INTO events_tri(events_tri, rowid, text, tags) VALUES ('delete', old.id, old.text, old.tags);
    INSERT INTO events_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
    INSERT INTO events_tri(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
INSERT INTO events_fts(events_fts) VALUES ('rebuild');
INSERT INTO events_tri(events_tri) VALUES ('rebuild');
"""

NO_EMBED_KINDS = {"state", "decision", "action"}   # служебные события искать по смыслу незачем

# Слова вопроса, которые не несут темы: «о чём я говорил вчера» → искать нечего, кроме темы
STOPWORDS = {
    "и", "в", "во", "на", "с", "со", "о", "об", "обо", "по", "за", "к", "ко", "у", "от", "до", "из",
    "я", "мы", "ты", "вы", "он", "она", "они", "мне", "меня", "нам", "нас", "мой", "моя", "мои",
    "что", "чём", "чем", "как", "где", "когда", "это", "этот", "эта", "то", "ли", "не", "да", "нет",
    "был", "была", "были", "было", "есть", "про", "там", "тут", "так", "ну", "же", "бы", "все", "всё",
    "говорил", "говорила", "говорили", "сказал", "сказала", "сказали", "обсуждал", "обсуждали",
    "упоминал", "упоминали", "рассказывал", "рассказывали", "речь", "разговор", "вчера", "сегодня",
}


def fts_query(text: str) -> str:
    """Запрос для FTS5: слова через OR, с грубым «стеммингом» — обрезкой окончаний под префиксный поиск."""
    terms = []
    for tok in re.findall(r"\w+", text.lower()):
        if len(tok) < 2 or tok in STOPWORDS:
            continue
        stem = tok if len(tok) <= 4 else tok[:max(4, len(tok) - 2)]
        terms.append(f'"{stem}"*')
    return " OR ".join(dict.fromkeys(terms))


def trigrams(word):
    w = word.lower()
    return {w[i:i + 3] for i in range(len(w) - 2)}


def tri_query(text):
    """Запрос для триграммного индекса: кусочки по 3 буквы из значимых слов (≥ 4 букв)."""
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) >= 4 and w not in STOPWORDS]
    grams = []
    for w in words:
        grams += sorted(trigrams(w))
    grams = list(dict.fromkeys(g for g in grams if '"' not in g))[:40]
    return " OR ".join(f'"{g}"' for g in grams), words


VOWELS = set("аеёиоуыэюяьъaeiouy")


def skeleton(word):
    """Согласный «скелет» слова: распознавание чаще всего путает гласные («докер» / «дакера» → дкр)."""
    s = "".join(c for c in word.lower() if c not in VOWELS)
    return re.sub(r"(.)\1+", r"\1", s)


def fuzzy_hit(words, text, need=0.5):
    """Хотя бы одно слово запроса похоже на слово текста: доля общих триграмм ≥ need
    или совпал согласный скелет (у слов от 4 букв со скелетом от 3 согласных — иначе слишком широко)."""
    tw = {w for w in re.findall(r"\w+", text.lower()) if len(w) >= 3}
    for q in words:
        qg, qs = trigrams(q), skeleton(q)
        for t in tw:
            tg = trigrams(t)
            if qg and tg and len(qg & tg) / len(qg) >= need:
                return True
            if len(qs) >= 3 and len(t) >= 4 and skeleton(t) == qs:
                return True
    return False


def _row(r) -> dict:
    return {"id": r[0], "ts": r[1], "ts_end": r[2], "source": r[3], "kind": r[4], "text": r[5],
            "meta": json.loads(r[6] or "{}"), "trust": r[7], "tags": r[8], "hidden": r[9]}


COLS = "id, ts, ts_end, source, kind, text, meta, trust, tags, hidden"


class Timeline:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.executescript(SCHEMA)
        self._migrate()
        self.dim = int(self.kv_get("vec_dim") or 0) or None

    def _migrate(self):
        """Колонки для фазы сна (теги, пометка «мусор») и индексы слов + триграмм. Идемпотентно."""
        with self._lock:
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
            if "tags" not in cols:
                self.db.execute("ALTER TABLE events ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
            if "hidden" not in cols:                     # 1 — фаза сна пометила как мусор
                self.db.execute("ALTER TABLE events ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
                self.db.execute("ALTER TABLE events ADD COLUMN hidden_at REAL")
            sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name = 'events_tri'").fetchone()
            if not sql:                                  # старая схема (только слова) или новая база
                for t in ("events_ai", "events_ad", "events_au"):
                    self.db.execute(f"DROP TRIGGER IF EXISTS {t}")
                self.db.execute("DROP TABLE IF EXISTS events_fts")
                self.db.executescript(FTS_SCHEMA)
            self.db.commit()

    # ------------------------------------------------------------ служебное
    def kv_get(self, key):
        r = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def kv_set(self, key, value):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, str(value)))
            self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------ запись
    def add(self, ev: Event) -> int:
        embedded = 2 if (not ev.text.strip() or ev.kind in NO_EMBED_KINDS) else 0
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO events(ts, ts_end, source, kind, text, meta, trust, embedded) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.ts, ev.ts_end, ev.source, ev.kind, ev.text, json.dumps(ev.meta, ensure_ascii=False),
                 ev.trust, embedded))
            self.db.commit()
            return cur.lastrowid

    def update_text(self, event_id, text, meta=None):
        """Переписать текст производного события (сводки): FTS обновит триггер, вектор пересчитается."""
        with self._lock:
            if self.dim:
                self.db.execute("DELETE FROM vec_events WHERE rowid = ?", (event_id,))
            if meta is None:
                self.db.execute("UPDATE events SET text = ?, embedded = 0 WHERE id = ?", (text, event_id))
            else:
                self.db.execute("UPDATE events SET text = ?, meta = ?, embedded = 0 WHERE id = ?",
                                (text, json.dumps(meta, ensure_ascii=False), event_id))
            self.db.commit()

    def set_end(self, event_id, ts_end):
        """Продлить событие-интервал (окно в фокусе): сенсор обновляет конец, пока окно активно."""
        with self._lock:
            self.db.execute("UPDATE events SET ts_end = ? WHERE id = ?", (ts_end, event_id))
            self.db.commit()

    def delete_ids(self, ids):
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            if self.dim:
                self.db.executemany("DELETE FROM vec_events WHERE rowid = ?", [(i,) for i in ids])
            self.db.executemany("DELETE FROM events WHERE id = ?", [(i,) for i in ids])
            self.db.commit()

    def last_before(self, ts, source, kinds=None):
        """Последнее событие источника, начавшееся не позже ts (например, какое окно было активно)."""
        where, args = self._where(None, ts, source, kinds)
        with self._lock:
            r = self.db.execute(f"SELECT {COLS} FROM events {where} ORDER BY ts DESC LIMIT 1", args).fetchone()
        return _row(r) if r else None

    def delete_range(self, start, end, source=None, kinds=None) -> int:
        """Настоящее удаление (команда «забудь», очистка дневника): события, полнотекстовый индекс и векторы."""
        where, args = self._where(start, end, source, kinds, hidden=True)   # и помеченное мусором тоже
        with self._lock:
            ids = [r[0] for r in self.db.execute(f"SELECT id FROM events {where}", args)]
            if ids and self.dim:
                self.db.executemany("DELETE FROM vec_events WHERE rowid = ?", [(i,) for i in ids])
            self.db.execute(f"DELETE FROM events {where}", args)
            self.db.commit()
        return len(ids)

    # ------------------------------------------------------------ векторы
    def ensure_vec(self, dim: int):
        if self.dim == dim:
            return
        if self.dim and self.dim != dim:
            raise ValueError(f"Размер векторов сменился ({self.dim} → {dim}): нужна переиндексация")
        with self._lock:
            # ts — колонка-метаданные: позволяет фильтровать поиск по времени внутри KNN
            self.db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_events USING vec0("
                            f"embedding float[{dim}] distance_metric=cosine, ts float)")
            self.db.commit()
        self.kv_set("vec_dim", dim)
        self.dim = dim

    def pending_embeddings(self, limit=32):
        with self._lock:
            return self.db.execute("SELECT id, text, ts FROM events WHERE embedded = 0 ORDER BY id LIMIT ?",
                                   (limit,)).fetchall()

    def store_embeddings(self, items):
        """items: [(id, ts, vector)]"""
        if not items:
            return
        self.ensure_vec(len(items[0][2]))
        with self._lock:
            for eid, ts, vec in items:
                self.db.execute("INSERT OR REPLACE INTO vec_events(rowid, embedding, ts) VALUES (?, ?, ?)",
                                (eid, sqlite_vec.serialize_float32(vec), float(ts)))
            self.db.executemany("UPDATE events SET embedded = 1 WHERE id = ?", [(i,) for i, _, _ in items])
            self.db.commit()

    def mark_embedded(self, ids, status):
        with self._lock:
            self.db.executemany("UPDATE events SET embedded = ? WHERE id = ?", [(status, i) for i in ids])
            self.db.commit()

    # ------------------------------------------------------------ чтение
    @staticmethod
    def _where(start=None, end=None, source=None, kinds=None, prefix="", hidden=False):
        """hidden=False — без событий, помеченных фазой сна как мусор (они ждут удаления)."""
        conds, args = ([] if hidden else [f"{prefix}hidden = 0"]), []
        if start is not None:
            conds.append(f"{prefix}ts >= ?")
            args.append(start)
        if end is not None:
            conds.append(f"{prefix}ts <= ?")
            args.append(end)
        if source:
            conds.append(f"{prefix}source = ?")
            args.append(source)
        if kinds:
            conds.append(f"{prefix}kind IN ({','.join('?' * len(kinds))})")
            args.extend(kinds)
        return ("WHERE " + " AND ".join(conds)) if conds else "", args

    def range(self, start=None, end=None, source=None, kinds=None, limit=None, newest=False, hidden=False):
        """События за период по времени. newest=True — последние limit штук (всё равно по порядку)."""
        where, args = self._where(start, end, source, kinds, hidden=hidden)
        order = "DESC" if newest else "ASC"
        sql = f"SELECT {COLS} FROM events {where} ORDER BY ts {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = [_row(r) for r in self.db.execute(sql, args)]
        return rows[::-1] if newest else rows

    def count(self, start=None, end=None, source=None, kinds=None) -> int:
        where, args = self._where(start, end, source, kinds)
        with self._lock:
            return self.db.execute(f"SELECT COUNT(*) FROM events {where}", args).fetchone()[0]

    def count_pending(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM events WHERE embedded = 0").fetchone()[0]

    def get(self, ids) -> list:
        if not ids:
            return []
        with self._lock:
            rows = self.db.execute(f"SELECT {COLS} FROM events WHERE id IN ({','.join('?' * len(ids))})",
                                   list(ids)).fetchall()
        by_id = {r[0]: _row(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def fts(self, query, start=None, end=None, source=None, kinds=None, limit=40):
        """[(id, bm25)] — чем меньше bm25, тем лучше совпадение."""
        q = fts_query(query)
        if not q:
            return []
        where, args = self._where(start, end, source, kinds, prefix="e.")
        where = (where + " AND " if where else "WHERE ") + "events_fts MATCH ?"
        sql = (f"SELECT e.id, bm25(events_fts) AS score FROM events_fts JOIN events e ON e.id = events_fts.rowid "
               f"{where} ORDER BY score LIMIT ?")
        with self._lock:
            return self.db.execute(sql, args + [q, limit]).fetchall()

    def fts_tri(self, query, start=None, end=None, source=None, kinds=None, limit=40):
        """Нечёткий поиск по триграммам: кандидаты из индекса, затем проверка похожести слов в Python
        (иначе частые кусочки вроде «ени» тянут всё подряд). [(id, bm25)]."""
        q, words = tri_query(query)
        if not q:
            return []
        where, args = self._where(start, end, source, kinds, prefix="e.")
        where = (where + " AND " if where else "WHERE ") + "events_tri MATCH ?"
        sql = (f"SELECT e.id, bm25(events_tri) AS score, e.text, e.tags FROM events_tri "
               f"JOIN events e ON e.id = events_tri.rowid {where} ORDER BY score LIMIT ?")
        with self._lock:
            rows = self.db.execute(sql, args + [q, limit * 3]).fetchall()
        return [(i, s) for i, s, text, tags in rows if fuzzy_hit(words, f"{text} {tags}")][:limit]

    # ------------------------------------------------------------ фаза сна: теги, мусор, исправления
    def set_tags(self, event_id, tags):
        with self._lock:
            self.db.execute("UPDATE events SET tags = ? WHERE id = ?", (" ".join(tags), event_id))
            self.db.commit()

    def hide(self, ids, ts):
        """Пометить как мусор: пропадает из поиска, дневников и контекста; удаляется через срок."""
        with self._lock:
            self.db.executemany("UPDATE events SET hidden = 1, hidden_at = ? WHERE id = ?", [(ts, i) for i in ids])
            self.db.commit()

    def unhide(self, ids):
        with self._lock:
            self.db.executemany("UPDATE events SET hidden = 0, hidden_at = NULL WHERE id = ?", [(i,) for i in ids])
            self.db.commit()

    def purge_hidden(self, before_ts):
        with self._lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM events WHERE hidden = 1 AND hidden_at < ?",
                                                 (before_ts,))]
        self.delete_ids(ids)
        return len(ids)

    def knn(self, vec, start=None, end=None, limit=40, max_distance=None):
        """[(id, косинусное расстояние)] ближайших по смыслу; фильтр по времени — внутри KNN."""
        if not self.dim:
            return []
        conds, args = ["embedding MATCH ?", "k = ?"], [sqlite_vec.serialize_float32(vec), limit]
        if start is not None:
            conds.append("ts >= ?")
            args.append(float(start))
        if end is not None:
            conds.append("ts <= ?")
            args.append(float(end))
        sql = f"SELECT rowid, distance FROM vec_events WHERE {' AND '.join(conds)} ORDER BY distance"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        if max_distance is not None:
            rows = [r for r in rows if r[1] <= max_distance]
        return rows
