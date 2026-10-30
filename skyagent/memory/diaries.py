"""Дневники: по одному файлу на источник и день — то, куда смотрит модель.

Рабочая память (working.py) — срез «сейчас»; дневники — летопись по дням:
  data/diaries/ГГГГ-ММ-ДД/<имя>.md
Это проекции хронологии и эпизодов: их можно удалить и пересобрать. Помеченное фазой сна как
мусор в дневники не попадает. Сегодняшние дневники обновляются в фоне, прошлые — по запросу
(и пересобираются фазой сна после чистки).

Новый дневник — одна регистрация:
    register_diary("имя", "Заголовок", builder, clear=[("источник", ("тип", …))])
builder(diaries, start, end) -> str. clear — какие события хронологии удалить, чтобы очистить дневник
(или функция clear(diaries, start, end) -> int); без clear дневник не чистится — он собран из других.

Свои дневники пользователя: telegram (переписка из Telegram, sensors/telegram.py) и journal (личный
дневник — записи пишет сам пользователь, memory/journal.py).
"""
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

REGISTRY = {}       # имя → (заголовок, функция сборки)
CLEARERS = {}       # имя → [(источник, типы | None)] или функция (diaries, start, end) -> int


def register_diary(name, title, builder, clear=None):
    REGISTRY[name] = (title, builder)
    if clear:
        CLEARERS[name] = clear


def _hm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


# ------------------------------------------------------------ встроенные дневники
def _windows(d, s, e):
    from .tools import activity_report
    return activity_report(d.memory, s, e, max_lines=300) or "Окна не записаны."


def _audio(d, s, e):
    from .tools import format_rows
    rows = d.tl.range(s, e, "mic", ("speech",))
    if not rows:
        return "Речи не записано."
    me = sum(1 for r in rows if r["meta"].get("speaker") == "me")
    other = sum(1 for r in rows if r["meta"].get("speaker") == "other")
    head = f"Фраз: {len(rows)} (я: {me}, другие: {other}, неизвестно: {len(rows) - me - other})."
    return head + "\n" + format_rows(rows, memory=d.memory)


def _dialog(d, s, e):
    who = {("user", "message"): "ты", ("agent", "action"): "ассистент сделал",
           ("agent", "answer"): "ассистент ответил", ("observer", "notify"): "наблюдатель напомнил"}
    rows = [r for r in d.tl.range(s, e, None, ("message", "action", "answer", "notify"))
            if (r["source"], r["kind"]) in who]
    if not rows:
        return "Разговоров с ассистентом не было."
    lines = []
    for r in rows:
        via = " (голосом)" if r["meta"].get("via") == "voice" else ""
        lines.append(f"[{_hm(r['ts'])}] {who[(r['source'], r['kind'])]}{via}: {r['text'][:400]}")
    return "\n".join(lines)


def _episodes(d, s, e):
    from .tools import episode_lines
    if not d.episodes:
        return "Эпизоды не подключены."
    eps = [x for x in d.episodes.list(s, e) if x["ts"] >= s]
    return episode_lines(eps) or "Эпизодов нет."


def _observer(d, s, e):
    rows = d.tl.range(s, e, "observer", ("decision",))
    if not rows:
        return "Наблюдатель не просыпался."
    count = {}
    lines = []
    for r in rows:
        m = r["meta"]
        count[m.get("action", "?")] = count.get(m.get("action", "?"), 0) + 1
        if m.get("action") != "none":
            extra = f" → «{m.get('message')}» ({m.get('delivery')})" if m.get("action") == "notify" else ""
            lines.append(f"[{_hm(r['ts'])}] {m.get('action')}: {m.get('why', '')[:200]}{extra} [причина: {m.get('reason')}]")
    head = "Проверок: " + ", ".join(f"{k} — {v}" for k, v in count.items())
    return head + ("\n" + "\n".join(lines) if lines else "\nВсё время — «ничего не делать».")


def _triggers(d, s, e):
    rows = d.tl.range(s, e, "trigger", ("fired", "error"))
    if not rows:
        return "Триггеры не срабатывали."
    return "\n".join(f"[{_hm(r['ts'])}] {r['text'][:300]}" for r in rows)


def _tasks(d, s, e):
    if not d.working:
        return "Рабочая память не подключена."
    rows = d.working.commitments(days=60)
    new = [r for r in rows if s <= r["ts"] < e]
    changed = [r for r in rows if r["updated"] and s <= r["updated"] < e and r["status"] != "open"]
    lines = []
    if new:
        lines.append("Появились:")
        lines += [f"- #{r['id']} {r['text']}" + (" (напоминание)" if r["note"] == "напоминание" else "") for r in new]
    if changed:
        lines.append("Закрыты:")
        lines += [f"- #{r['id']} {r['text']} — {'выполнено' if r['status'] == 'done' else 'отменено'}"
                  + (f" ({r['note']})" if r["note"] and r["note"] != "напоминание" else "") for r in changed]
    return "\n".join(lines) or "Задачи не менялись."


def _people(d, s, e):
    if not d.episodes:
        return "Эпизоды не подключены."
    seen = {}
    for ep in d.episodes.list(s, e):
        for p in ep["meta"].get("people", []):
            seen.setdefault(p, []).append(f"[{_hm(ep['ts'])}] {ep['title']}")
    if not seen:
        return "Люди в эпизодах не встречались."
    return "\n".join(f"- {p}: " + "; ".join(v) for p, v in seen.items())


def _sleep(d, s, e):
    rows = d.tl.range(s, e, "memory", ("sleep",))
    if not rows:
        return "Фаза сна не запускалась."
    return "\n\n".join(f"[{_hm(r['ts'])}] {r['text']}" for r in rows)


def _telegram(d, s, e):
    rows = d.tl.range(s, e, "telegram", ("message",))
    if not rows:
        return "Сообщений из Telegram не записано."
    chats = {}
    for r in rows:
        chats.setdefault(r["meta"].get("chat") or "?", []).append(r)
    head = (f"Сообщений: {len(rows)} (мои: {sum(1 for r in rows if r['meta'].get('out'))}) в {len(chats)} чатах: "
            + ", ".join(f"{c} — {len(v)}" for c, v in sorted(chats.items(), key=lambda kv: -len(kv[1]))))
    parts = [head]
    for chat, msgs in sorted(chats.items(), key=lambda kv: kv[1][0]["ts"]):
        kind = {"user": "личный", "group": "группа", "channel": "канал"}.get(msgs[0]["meta"].get("chat_type"), "")
        lines = [f"\n## {chat}" + (f" ({kind})" if kind else "")]
        for r in msgs:
            m = r["meta"]
            who = "я" if m.get("out") else (m.get("sender") or chat)
            media = f" [{m['media']}]" if m.get("media") else ""
            reply = " ↩" if m.get("reply_to") else ""
            text = r["text"] if len(r["text"]) <= 600 else r["text"][:600] + "…"
            lines.append(f"[{_hm(r['ts'])}] {who}{reply}:{media} {text}".rstrip())
        parts.append("\n".join(lines))
    return "\n".join(parts)


def _journal(d, s, e):
    rows = d.tl.range(s, e, "journal", ("entry",))
    if not rows:
        return ("Записей нет. Добавить: /note текст в CLI, вкладка «Дневник» в мини-окне, "
                "«Скай, запиши в дневник …» или файл journal-inbox.txt.")
    return "\n\n".join(f"[{_hm(r['ts'])}] (#{r['id']}) {r['text']}" for r in rows)


def _clear_episodes(d, s, e):
    if not d.episodes:
        return 0
    n = len([x for x in d.episodes.list(s, e) if x["kind"] == "block"])
    d.episodes.forget(s or 0, e)
    return n


for _name, _title, _fn, _clear in (
        ("windows", "Окна и приложения", _windows, [("windows", None)]),
        ("audio", "Речь у микрофона", _audio, [("mic", ("speech",))]),
        ("dialog", "Разговор с ассистентом", _dialog,
         [("user", ("message",)), ("agent", ("action", "answer")), ("observer", ("notify",))]),
        ("episodes", "Эпизоды и сводки", _episodes, _clear_episodes),
        ("observer", "Наблюдатель", _observer, [("observer", ("decision",))]),
        ("triggers", "Триггеры", _triggers, [("trigger", ("fired", "error"))]),
        ("tasks", "Задачи и обязательства", _tasks, None),
        ("people", "Люди", _people, None),
        ("sleep", "Фаза сна", _sleep, [("memory", ("sleep",))]),
        ("telegram", "Telegram", _telegram, [("telegram", None)]),
        ("journal", "Личный дневник", _journal, [("journal", ("entry",))])):
    register_diary(_name, _title, _fn, _clear)

NOT_CLEARABLE = {"tasks": "задачи закрываются по одной: /done N, /drop N",
                 "people": "люди берутся из эпизодов — очисти дневник episodes"}


class Diaries:
    def __init__(self, memory, episodes=None, working=None, interval=60, journal=None):
        self.memory = memory
        self.journal = journal          # личный дневник: импорт записей из journal-inbox.txt
        self.tl = memory.timeline
        self.episodes = episodes
        self.working = working
        self.interval = interval
        self.dir = Path(memory.data_dir) / "diaries"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.error = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    @staticmethod
    def bounds(d: date):
        s = datetime.combine(d, datetime.min.time()).timestamp()
        return s, datetime.combine(d + timedelta(days=1), datetime.min.time()).timestamp()

    def path(self, name, d: date):
        return self.dir / d.isoformat() / f"{name}.md"

    def build(self, d: date, names=None):
        """Собрать дневники дня (только изменившиеся файлы переписываются). Возвращает изменившиеся."""
        s, e = self.bounds(d)
        changed = []
        with self._lock:
            for name, (title, fn) in REGISTRY.items():
                if names and name not in names:
                    continue
                try:
                    body = fn(self, s, e)
                except Exception as ex:  # noqa: BLE001 — один дневник не должен ронять остальные
                    body = f"(не собрался: {type(ex).__name__}: {ex})"
                    self.error = f"{name}: {ex}"
                text = f"# {title} — {d.strftime('%d.%m.%Y')}\n\n{body.strip()}\n"
                p = self.path(name, d)
                if p.exists() and p.read_text(encoding="utf-8") == text:
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(p)
                changed.append(name)
        return changed

    def read(self, name, d: date):
        if name not in REGISTRY:
            raise KeyError(name)
        p = self.path(name, d)
        if d == date.today() or not p.exists():
            self.build(d, [name])
        return p.read_text(encoding="utf-8")

    def days(self):
        return sorted((p.name for p in self.dir.iterdir() if p.is_dir()), reverse=True)

    def clear(self, name, d: date | None = None) -> int:
        """Очистить дневник за день (d=None — за всё время): удалить его события из памяти и файлы.
        Удаление настоящее, вместе с поиском и векторами. Возвращает число удалённых записей."""
        if name not in REGISTRY:
            raise KeyError(name)
        spec = CLEARERS.get(name)
        if not spec:
            raise ValueError(NOT_CLEARABLE.get(name, "этот дневник собирается из других и сам не чистится"))
        s, e = self.bounds(d) if d else (None, None)
        if e is not None:
            e -= 0.001                      # конец дня не включаем: timeline берёт ts <= end
        with self._lock:
            if callable(spec):
                n = spec(self, s, e)
            else:
                n = sum(self.tl.delete_range(s, e, source, kinds) for source, kinds in spec)
            files = [self.path(name, d)] if d else list(self.dir.glob(f"*/{name}.md"))
            for p in files:
                p.unlink(missing_ok=True)
        self.build(date.today(), [name])
        return n

    def start(self):
        threading.Thread(target=self._loop, name="sky-diaries", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.journal:
                    self.journal.import_inbox()
                self.build(date.today())
                y = date.today() - timedelta(days=1)       # вчера — дописать хвост после полуночи один раз
                if not self.path("windows", y).exists() or time.time() - self.bounds(date.today())[0] < 3600:
                    self.build(y)
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
            self._stop.wait(self.interval)
