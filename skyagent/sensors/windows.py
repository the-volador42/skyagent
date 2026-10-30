"""Сенсор окон: какое приложение и окно в фокусе и сколько времени.

Каждый интервал фокуса — одно событие `focus` (ts — начало, ts_end — конец), его конец
продлевается, пока окно активно. От дешёвого к дорогому — всё здесь L0, без моделей:
  - опрос активного окна раз в POLL секунд (микросекунды WinAPI);
  - антидребезг: окно засчитывается, только если продержалось MIN_FOCUS секунд
    (Alt+Tab через три окна не оставляет три записи);
  - заголовки нормализуются: «(3) Telegram» и «Telegram» — одно и то же;
  - нет ввода IDLE_AFTER секунд или экран заблокирован — пользователь отошёл, интервал закрывается;
  - приватные окна (менеджеры паролей, инкогнито) пишутся как «скрыто».
"""
import os
import re
import threading
import time

from .. import winctl
from ..memory.events import Event

POLL = 0.5
MIN_FOCUS = 1.5
IDLE_AFTER = 300
FLUSH_EVERY = 30          # как часто сохранять конец текущего интервала (на случай падения)

# Понятные имена приложений по имени процесса
APP_NAMES = {
    "code": "VS Code", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "opera": "Opera",
    "browser": "Яндекс Браузер", "telegram": "Telegram", "explorer": "Проводник",
    "windowsterminal": "Терминал", "cmd": "Командная строка", "powershell": "PowerShell",
    "claude": "Claude", "notepad": "Блокнот", "winword": "Word", "excel": "Excel",
    "powerpnt": "PowerPoint", "outlook": "Outlook", "discord": "Discord", "slack": "Slack",
    "zoom": "Zoom", "ms-teams": "Teams", "teams": "Teams", "spotify": "Spotify", "obsidian": "Obsidian",
    "pycharm64": "PyCharm", "idea64": "IntelliJ IDEA", "devenv": "Visual Studio", "steam": "Steam",
    "electron": "Electron", "systemsettings": "Параметры", "taskmgr": "Диспетчер задач",
}
# Хвосты заголовков с именем приложения — убираем, приложение и так известно
TITLE_SUFFIX = re.compile(
    r"\s+[-—–]\s+(?:[^-—–:]{1,30}(?::\s*|\s+[-—–]\s+))?Microsoft​?\s?Edge$"   # Edge с профилем: «— Личный: …»
    r"|\s+[-—–]\s+(Google Chrome|Mozilla Firefox|Opera|Яндекс Браузер|Visual Studio Code|Блокнот|Notepad|"
    r"Word|Excel|PowerPoint|Obsidian.*|PyCharm.*|Discord)$")
# «и ещё 3 страницы», «and 2 more pages» — счётчик вкладок в группе
TITLE_TABS = re.compile(r"\s+(и ещ[её] \d+ (страниц\w*|вкладк\w*)|and \d+ more (pages?|tabs?))", re.I)
TITLE_NOISE = re.compile(r"^\(\d+\)\s*|^[●•*]\s*|\s*[●•]$")      # счётчики непрочитанного, «изменено»

PRIVATE_APPS = {"keepass", "keepassxc", "1password", "bitwarden", "lastpass", "enpass", "dashlane"}
PRIVATE_TITLE = re.compile(r"inprivate|incognito|инкогнито|private browsing|приватн", re.I)
LOCK_APPS = {"lockapp", "logonui"}


def app_name(exe: str) -> str:
    stem = exe.rsplit(".", 1)[0].lower()
    return APP_NAMES.get(stem, exe.rsplit(".", 1)[0] or "?")


def clean_title(title: str) -> str:
    t = TITLE_NOISE.sub("", title.strip())
    t = TITLE_SUFFIX.sub("", t)
    t = TITLE_TABS.sub("", t)
    return t.strip()


def _extra(env, default):
    extra = {x.strip().lower() for x in os.environ.get(env, "").split(",") if x.strip()}
    return default | extra


class WindowSensor:
    """Состояния: stopped, recording, paused, away (нет ввода / экран заблокирован)."""
    source = "windows"

    def __init__(self, memory, probe=None, idle_probe=None, clock=None):
        self.memory = memory
        self.probe = probe or winctl.foreground          # для тестов можно подменить
        self.idle_probe = idle_probe or winctl.idle_seconds
        self.clock = clock or time.time
        self.private_apps = _extra("SKYAGENT_PRIVATE_APPS", PRIVATE_APPS)
        self.state = "stopped"
        self.error = None
        self.current = None        # {"id", "key", "app", "title", "ts", "flushed"}
        self.candidate = None      # (key, since, info)
        self.stats = {"switches": 0, "ignored": 0}
        self._running = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ управление
    def start(self):
        self._running.set()
        self.state = "recording"
        self._state_event("запись включена")
        threading.Thread(target=self._loop, name="sky-windows", daemon=True).start()

    def pause(self):
        if self.state not in ("recording", "away"):
            return
        with self._lock:
            self._close(self.clock())
            self.state = "paused"
        self._state_event("пауза")

    def resume(self):
        if self.state == "paused":
            self.state = "recording"
            self._state_event("запись включена")

    def stop(self):
        with self._lock:
            self._close(self.clock())
        was = self.state
        self._running.clear()
        self.state = "stopped"
        if was in ("recording", "away"):
            self._state_event("запись выключена")

    @property
    def now_text(self):
        c = self.current
        return f"{c['app']} — {c['title']}" if c else "—"

    # ------------------------------------------------------------ события
    def _state_event(self, text, ts=None):
        self.memory.publish(Event(self.source, "state", text, ts=ts or self.clock(), trust="system"))

    def _close(self, ts):
        if self.current:
            if ts - self.current["ts"] < MIN_FOCUS:      # переключился и сразу ушёл — пустой интервал
                self.memory.timeline.delete_ids([self.current["id"]])
            else:
                self.memory.timeline.set_end(self.current["id"], ts)
            self.current = None

    def _open(self, info, key, ts):
        app = app_name(info["exe"])
        private = info["exe"].rsplit(".", 1)[0].lower() in self.private_apps or bool(
            PRIVATE_TITLE.search(info["title"]))
        title = "скрыто" if private else (key[1] or "(без заголовка)")
        ev = Event(self.source, "focus", f"{app} — {title}", ts=ts, ts_end=ts, trust="screen",
                   meta={"app": app, "exe": info["exe"], "title": title, "private": private})
        self.memory.publish(ev)
        self.current = {"id": ev.id, "key": key, "app": app, "title": title, "ts": ts, "flushed": ts}
        self.stats["switches"] += 1

    # ------------------------------------------------------------ цикл
    def tick(self):
        """Один шаг опроса (вынесен отдельно, чтобы тестировать без потоков)."""
        if self.state == "paused":
            return
        now = self.clock()
        try:
            idle = self.idle_probe()
            info = self.probe()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            return
        locked = info is None or info["exe"].rsplit(".", 1)[0].lower() in LOCK_APPS
        with self._lock:
            if idle >= IDLE_AFTER or locked:
                if self.state != "away":
                    since = now - idle if idle >= IDLE_AFTER else now
                    self._close(since)
                    self.candidate = None
                    self.state = "away"
                    self._state_event("экран заблокирован" if locked else "нет активности", ts=since)
                return
            if self.state == "away":
                self.state = "recording"
                self._state_event("снова за компьютером", ts=now)
            key = (info["exe"].lower(), clean_title(info["title"]))
            if self.current and key == self.current["key"]:
                self.candidate = None
                if now - self.current["flushed"] >= FLUSH_EVERY:
                    self.memory.timeline.set_end(self.current["id"], now)
                    self.current["flushed"] = now
                return
            if not self.candidate or self.candidate[0] != key:
                if self.candidate:
                    self.stats["ignored"] += 1          # мелькнуло и ушло — не засчитываем
                self.candidate = (key, now, info)
                return
            if now - self.candidate[1] >= MIN_FOCUS:
                key, since, info = self.candidate
                self.candidate = None
                self._close(since)
                self._open(info, key, since)

    def _loop(self):
        while self._running.is_set():
            self.tick()
            time.sleep(POLL)
