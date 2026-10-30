"""Память SkyAgent. Сейчас — первый слой: шина событий и хронология (SQLite + FTS5 + векторы).

Дальше сюда добавятся эпизоды, рабочая память (файлы L2) и граф в Neo4j (R1) — см. docs/ARCHITECTURE.md.
"""
import os
from pathlib import Path

from .embed import Embedder
from .events import Event, EventBus
from .timeline import Timeline

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DATA = REPO / "data"

RRF_K = 60              # сглаживание при слиянии рангов (Reciprocal Rank Fusion)
MAX_VEC_DISTANCE = 0.55  # косинусное расстояние: дальше — уже не про то
TRI_WEIGHT = 0.7         # нечёткий канал чуть слабее точного


class Memory:
    def __init__(self, data_dir=None, ollama_url=None, embed_model=None, gate=None):
        self.data_dir = Path(data_dir or os.environ.get("SKYAGENT_DATA") or DEFAULT_DATA)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.timeline = Timeline(self.data_dir / "timeline.db")
        self.bus = EventBus()
        url = ollama_url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        if not url.startswith("http"):
            url = "http://" + url
        self.embedder = Embedder(self.timeline, url, embed_model or os.environ.get("SKYAGENT_EMBED", "bge-m3"),
                                 gate=gate)
        # «Забудь» идёт сверху вниз: хронология → эпизоды → обязательства → граф.
        # forget_hooks(start, end) — для слоёв над хронологией; episode_hooks(ids) — для слоёв над эпизодами.
        self.forget_hooks = []
        self.episode_hooks = []

    def forget(self, start, end=None) -> int:
        """Настоящее удаление за период во всех слоях памяти. Возвращает число удалённых событий."""
        n = self.timeline.delete_range(start, end)
        for hook in list(self.forget_hooks):
            hook(start, end)
        return n

    def episodes_deleted(self, ids):
        for hook in list(self.episode_hooks):
            hook(ids)

    def start(self):
        self.embedder.start()
        return self

    def close(self):
        self.embedder.stop()
        self.timeline.close()

    def publish(self, event: Event) -> int:
        """Записать событие в хронологию (источник истины) и разослать подписчикам."""
        event.id = self.timeline.add(event)
        self.bus.emit(event)
        return event.id

    def search(self, query, start=None, end=None, source=None, kinds=None, limit=8):
        """Гибридный поиск в три канала — слова, триграммы (окончания, часть ошибок распознавания),
        смысл (векторы); ранги сливаются через RRF. Помеченное мусором не ищется.
        Возвращает события с полями score и by ('text', 'fuzzy', 'meaning')."""
        pool = max(limit * 5, 40)
        scores, by = {}, {}
        for rank, (eid, _) in enumerate(self.timeline.fts(query, start, end, source, kinds, pool)):
            scores[eid] = scores.get(eid, 0) + 1 / (RRF_K + rank)
            by.setdefault(eid, set()).add("text")
        for rank, (eid, _) in enumerate(self.timeline.fts_tri(query, start, end, source, kinds, pool)):
            scores[eid] = scores.get(eid, 0) + TRI_WEIGHT / (RRF_K + rank)
            by.setdefault(eid, set()).add("fuzzy")
        try:
            qvec = self.embedder.embed([query])[0]
            hits = self.timeline.knn(qvec, start, end, pool * 2, MAX_VEC_DISTANCE)
        except Exception as e:  # noqa: BLE001 — без Ollama остаётся полнотекстовый поиск
            self.embedder.error = f"{type(e).__name__}: {e}"
            hits = []
        rows = {r["id"]: r for r in self.timeline.get([eid for eid, _ in hits])}
        rank = 0
        for eid, _ in hits:     # фильтр по источнику и типу — после KNN, в нём их нет
            r = rows.get(eid)
            if not r or r["hidden"] or (source and r["source"] != source) or (kinds and r["kind"] not in kinds):
                continue
            scores[eid] = scores.get(eid, 0) + 1 / (RRF_K + rank)
            by.setdefault(eid, set()).add("meaning")
            rank += 1
        top = sorted(scores, key=scores.get, reverse=True)[:limit]
        out = []
        for r in self.timeline.get(top):
            r["score"] = round(scores[r["id"]], 4)
            r["by"] = sorted(by[r["id"]])
            out.append(r)
        return out


__all__ = ["Memory", "Event", "EventBus", "Timeline"]
