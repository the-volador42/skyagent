"""Рабочая память (L2): небольшие файлы, которые постоянно обновляются и лежат перед моделью.

Принципы (docs/ARCHITECTURE.md, раздел 6):
  - файлы — ПРОЕКЦИИ хронологии, эпизодов и (позже) графа: их можно удалить и пересобрать;
  - у каждого файла один писатель (owner): code — этот модуль, observer — наблюдатель (этап 3),
    user — пользователь (код такие файлы только создаёт по шаблону и никогда не перезаписывает);
  - запись атомарная: временный файл + переименование;
  - у каждого файла бюджет символов: старое не копится, а сворачивается (подробности остаются
    в хронологии и эпизодах);
  - manifest.json — версия и время изменения каждого файла: по ним наблюдатель берёт только дельту.

Обязательства (commitments) — единственное состояние, которое здесь хранится, а не проецируется:
у задачи есть статус (open/done/dropped), который меняют агент, наблюдатель или пользователь.
"""
import hashlib
import json
import os
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

RECENT_MIN = 15          # «последние минуты» в recent.md
LOOKBACK_DAYS = 14       # за сколько дней собирать обязательства и людей

# имя: (писатель, бюджет символов, заголовок)
FILES = {
    "now.md": ("code", 900, "Сейчас"),
    "recent.md": ("code", 3500, "Последние минуты"),
    "commitments.md": ("code", 3000, "Обязательства и задачи"),
    "people.md": ("code", 1800, "Люди"),
    "agent.md": ("code", 2000, "Разговор с ассистентом"),
    "open.md": ("observer", 2000, "Открытые вопросы"),
    "preferences.md": ("user", 2000, "Предпочтения"),
}
ORDER = list(FILES)

TEMPLATES = {
    "open.md": "# Открытые вопросы\n\n_Ведёт наблюдатель (этап 3): незакрытые вопросы, ожидания ответа, "
               "что требует внимания._\n\n(пока пусто)\n",
    "preferences.md": "# Предпочтения\n\n_Этот файл правишь ты сам — ассистент его только читает._\n\n"
                      "- (например) Не беспокоить, когда я в IDE или в звонке, если это не срочно.\n",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS commitments (
    id         INTEGER PRIMARY KEY,
    text       TEXT NOT NULL,
    norm       TEXT NOT NULL UNIQUE,
    episode_id INTEGER,
    ts         REAL NOT NULL,             -- когда прозвучало (начало эпизода)
    status     TEXT NOT NULL DEFAULT 'open',   -- open, done, dropped
    updated    REAL,
    note       TEXT NOT NULL DEFAULT ''
);
"""
WD = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower().replace("ё", "е"))).strip()


def _dt(ts, fmt="%d.%m %H:%M"):
    d = datetime.fromtimestamp(ts)
    return f"{WD[d.weekday()]} {d.strftime(fmt)}"


def _fit(text: str, budget: int, keep="tail") -> str:
    """Уложить в бюджет: для лент оставляем хвост (свежее), для списков — голову."""
    if len(text) <= budget:
        return text
    if keep == "tail":
        cut = text[-budget:]
        return "…\n" + cut[cut.find("\n") + 1:]
    cut = text[:budget]
    return cut[:cut.rfind("\n")] + "\n…"


class WorkingMemory:
    def __init__(self, memory, episodes=None, sensors=None, interval=15):
        self.memory = memory
        self.tl = memory.timeline
        self.episodes = episodes
        self.sensors = sensors or {}
        self.interval = interval
        self.dir = Path(memory.data_dir) / "working"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self.manifest = self._load_manifest()
        self.error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        with self.tl._lock:
            self.tl.db.executescript(SCHEMA)
            self.tl.db.commit()
        memory.episode_hooks.append(self._episodes_deleted)
        for name, text in TEMPLATES.items():       # файлы чужих писателей — только создать
            if not (self.dir / name).exists():
                self._write(name, text, force=True)

    # ------------------------------------------------------------ файлы
    def _load_manifest(self):
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _atomic(self, path: Path, text: str):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _write(self, name, text, force=False):
        """Записать файл, если содержимое изменилось; поднять версию в manifest."""
        owner, budget, title = FILES[name]
        if owner != "code" and not force:
            raise PermissionError(f"{name} ведёт {owner}, не этот модуль")
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        entry = self.manifest.get(name, {})
        if entry.get("hash") == digest and (self.dir / name).exists():
            return False
        self._atomic(self.dir / name, text)
        self.manifest[name] = {"version": entry.get("version", 0) + 1, "updated": time.time(), "hash": digest,
                               "chars": len(text), "owner": owner, "title": title}
        self._atomic(self.manifest_path, json.dumps(self.manifest, ensure_ascii=False, indent=1))
        return True

    def read(self, names=None):
        """Содержимое файлов (для агента и, позже, наблюдателя). Файлы пользователя и наблюдателя
        тоже читаются — их версии отслеживаются по времени изменения."""
        out = []
        for name in names or ORDER:
            p = self.dir / name
            if p.exists():
                self._track_foreign(name, p)
                out.append(p.read_text(encoding="utf-8").strip())
        return "\n\n".join(out)

    def _track_foreign(self, name, p):
        owner = FILES[name][0]
        if owner == "code":
            return
        mtime = p.stat().st_mtime
        entry = self.manifest.get(name, {})
        if entry.get("updated", 0) + 0.5 < mtime:            # правили снаружи (пользователь)
            entry = {**entry, "version": entry.get("version", 0) + 1, "updated": mtime, "owner": owner,
                     "title": FILES[name][2], "chars": p.stat().st_size}
            self.manifest[name] = entry
            self._atomic(self.manifest_path, json.dumps(self.manifest, ensure_ascii=False, indent=1))

    def changed_since(self, versions: dict):
        """Какие файлы изменились относительно известных версий {имя: версия} — для дельт наблюдателя."""
        for name in ORDER:
            p = self.dir / name
            if p.exists():
                self._track_foreign(name, p)
        return {n: e["version"] for n, e in self.manifest.items() if versions.get(n) != e.get("version")}

    # ------------------------------------------------------------ обязательства
    def sync_commitments(self):
        """Задачи из сводок эпизодов → таблица обязательств (без дублей; статус не трогаем)."""
        if not self.episodes:
            return 0
        since = time.time() - LOOKBACK_DAYS * 86400
        added = 0
        for ep in self.episodes.list(since, None):
            if ep["status"] != "done":
                continue
            for todo in ep["meta"].get("todos", []):
                n = norm_text(todo)
                if not n:
                    continue
                with self.tl._lock:
                    cur = self.tl.db.execute(
                        "INSERT OR IGNORE INTO commitments(text, norm, episode_id, ts, updated) VALUES (?, ?, ?, ?, ?)",
                        (todo.strip(), n, ep["id"], ep["ts"], time.time()))
                    self.tl.db.commit()
                added += cur.rowcount
        return added

    def commitments(self, status=None, days=LOOKBACK_DAYS):
        sql = "SELECT id, text, episode_id, ts, status, updated, note FROM commitments WHERE ts >= ?"
        args = [time.time() - days * 86400]
        if status:
            sql += " AND status = ?"
            args.append(status)
        with self.tl._lock:
            rows = self.tl.db.execute(sql + " ORDER BY ts", args).fetchall()
        return [dict(zip(("id", "text", "episode_id", "ts", "status", "updated", "note"), r)) for r in rows]

    def add_commitment(self, text, note=""):
        """Задача или напоминание не из эпизода (по просьбе пользователя агенту). Возвращает номер."""
        n = norm_text(text)
        with self.tl._lock:
            cur = self.tl.db.execute("INSERT OR IGNORE INTO commitments(text, norm, ts, updated, note) "
                                     "VALUES (?, ?, ?, ?, ?)", (text.strip(), n, time.time(), time.time(), note))
            self.tl.db.commit()
            cid = cur.lastrowid if cur.rowcount else self.tl.db.execute(
                "SELECT id FROM commitments WHERE norm = ?", (n,)).fetchone()[0]
        self.refresh(["commitments.md"])
        return cid

    def set_commitment(self, cid, status, note=""):
        if status not in ("open", "done", "dropped"):
            raise ValueError("статус: open, done или dropped")
        with self.tl._lock:
            cur = self.tl.db.execute("UPDATE commitments SET status = ?, note = ?, updated = ? WHERE id = ?",
                                     (status, note, time.time(), int(cid)))
            self.tl.db.commit()
        if cur.rowcount:
            self.refresh(["commitments.md"])
        return cur.rowcount

    def _episodes_deleted(self, ids):
        with self.tl._lock:
            self.tl.db.executemany("DELETE FROM commitments WHERE episode_id = ?", [(i,) for i in ids])
            self.tl.db.commit()
        self.refresh()

    # ------------------------------------------------------------ сборка файлов
    def _now(self):
        from .tools import activity_report, fmt_dur, window_at
        now = time.time()
        lines = [f"# Сейчас — {_dt(now, '%d.%m.%Y %H:%M')}"]
        win = self.sensors.get("windows")
        w = window_at(self.memory, now)
        if win and win.state == "away":
            last = self.tl.last_before(now, "windows", ("state",))
            lines.append(f"- Пользователь отошёл ({last['text']} с {_dt(last['ts'], '%H:%M')})" if last
                         else "- Пользователь отошёл")
        elif w:
            start = w["ts"]
            lines.append(f"- Окно: {w['text']} ({fmt_dur(now - start)})")
        else:
            lines.append("- Окно: неизвестно")
        rec = []
        for key, name in (("mic", "микрофон"), ("windows", "окна")):
            s = self.sensors.get(key)
            rec.append(f"{name} — {s.state if s else 'выключен'}")
        lines.append("- Запись: " + ", ".join(rec))
        if self.episodes:
            op = self.episodes.open_episode()
            if op:
                lines.append(f"- Текущий эпизод: с {_dt(op[0], '%H:%M')} ({fmt_dur(now - op[0])}), сводки ещё нет")
        day0 = datetime.combine(date.today(), datetime.min.time()).timestamp()
        rep = activity_report(self.memory, day0, now)
        if rep:
            lines.append("- Сегодня: " + rep.splitlines()[0].replace("Активное время у компьютера: ", "за компьютером ")
                         .rstrip("."))
        last = self.tl.range(now - 3600, now, "mic", ("speech",), limit=1, newest=True)
        if last:
            lines.append(f"- Последняя фраза у микрофона: [{_dt(last[0]['ts'], '%H:%M')}] {last[0]['text'][:160]}")
        return "\n".join(lines) + "\n"

    def _recent(self):
        from .tools import activity_report, format_rows
        now = time.time()
        start = now - RECENT_MIN * 60
        win = self.sensors.get("windows")
        cur_id = (win.current or {}).get("id") if win else None
        parts = [f"# Последние {RECENT_MIN} минут"]
        rep = activity_report(self.memory, start, now, current_id=cur_id, max_lines=12)
        parts.append(rep or "Окна не записаны.")
        rows = self.tl.range(start, now, "mic", ("speech",))
        user = self.tl.range(start, now, "user", ("message",))
        if rows:
            parts.append("Речь:\n" + format_rows(rows, memory=self.memory))
        if user:
            parts.append("Писал ассистенту:\n" + "\n".join(f" [{_dt(r['ts'], '%H:%M')}] {r['text'][:200]}" for r in user))
        if self.episodes:                       # старше — уже свёрнуто в эпизоды
            eps = [x for x in self.episodes.list(now - 6 * 3600, start) if x["status"] == "done"][-3:]
            if eps:
                parts.append("До этого (эпизоды):\n" + "\n".join(
                    f" [{_dt(x['ts'], '%H:%M')}–{datetime.fromtimestamp(x['ts_end']):%H:%M}] {x['title']}" for x in eps))
        return _fit("\n\n".join(parts) + "\n", FILES["recent.md"][1], keep="tail")

    def _commitments(self):
        rows = self.commitments()
        open_ = [r for r in rows if r["status"] == "open"]
        closed = [r for r in rows if r["status"] != "open" and (r["updated"] or 0) > time.time() - 3 * 86400][-5:]
        titles = {}
        if self.episodes:
            titles = {x["id"]: x["title"] for x in self.episodes.get(list({r["episode_id"] for r in rows if r["episode_id"]}))}
        lines = ["# Обязательства и задачи",
                 "_Из сводок эпизодов. Статус меняют ассистент (по твоим словам) и наблюдатель. "
                 "Открытые не забываются, пока не закрыты._", ""]
        if open_:
            lines.append("Открытые:")
            for r in open_:
                src = f" — из «{titles[r['episode_id']]}»" if r["episode_id"] in titles else ""
                lines.append(f"- #{r['id']} [{_dt(r['ts'], '%d.%m')}] {r['text']}{src}")
        else:
            lines.append("Открытых задач нет.")
        if closed:
            lines.append("\nНедавно закрыто:")
            for r in closed:
                mark = "выполнено" if r["status"] == "done" else "отменено"
                lines.append(f"- #{r['id']} {r['text']} — {mark}" + (f" ({r['note']})" if r["note"] else ""))
        return _fit("\n".join(lines) + "\n", FILES["commitments.md"][1], keep="head")

    def _people(self):
        if not self.episodes:
            return "# Люди\n\n(эпизоды не подключены)\n"
        seen = {}
        for ep in self.episodes.list(time.time() - LOOKBACK_DAYS * 86400, None):
            for p in ep["meta"].get("people", []):
                d = seen.setdefault(norm_text(p), {"name": p, "n": 0, "last": 0, "title": "", "topics": []})
                d["n"] += 1
                if ep["ts"] >= d["last"]:
                    d.update(last=ep["ts"], title=ep["title"], topics=ep["meta"].get("topics", [])[:4])
        lines = ["# Люди", f"_Кто встречался в эпизодах за {LOOKBACK_DAYS} дней (позже — из графа)._", ""]
        for d in sorted(seen.values(), key=lambda x: -x["last"]):
            topics = f"; темы: {', '.join(d['topics'])}" if d["topics"] else ""
            lines.append(f"- {d['name']} — эпизодов: {d['n']}, последний: {_dt(d['last'])} «{d['title']}»{topics}")
        if not seen:
            lines.append("Пока никого.")
        return _fit("\n".join(lines) + "\n", FILES["people.md"][1], keep="head")

    def _agent(self):
        now = time.time()
        rows = [r for r in self.tl.range(now - 86400, now, None, ("message", "action", "answer", "notify"))
                if r["source"] in ("user", "agent", "observer")][-30:]
        lines = ["# Разговор с ассистентом", "_Что ты просил и что ассистент сделал за сутки — чтобы не повторяться._", ""]
        who = {"message": "ты", "action": "ассистент сделал", "answer": "ассистент ответил",
               "notify": "наблюдатель напомнил"}
        for r in rows:
            lines.append(f"- [{_dt(r['ts'], '%H:%M')}] {who[r['kind']]}: {r['text'][:220]}")
        if not rows:
            lines.append("Пока ничего.")
        return _fit("\n".join(lines) + "\n", FILES["agent.md"][1], keep="tail")

    BUILDERS = {"now.md": "_now", "recent.md": "_recent", "commitments.md": "_commitments",
                "people.md": "_people", "agent.md": "_agent"}

    def refresh(self, names=None):
        """Пересобрать файлы писателя code. Возвращает список изменившихся."""
        changed = []
        with self._lock:
            if names is None or "commitments.md" in names:
                self.sync_commitments()
            for name, method in self.BUILDERS.items():
                if names and name not in names:
                    continue
                try:
                    if self._write(name, getattr(self, method)()):
                        changed.append(name)
                except Exception as e:  # noqa: BLE001 — один файл не должен ронять остальные
                    self.error = f"{name}: {type(e).__name__}: {e}"
        return changed

    # ------------------------------------------------------------ фон
    def start(self):
        threading.Thread(target=self._loop, name="sky-working", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.interval)
