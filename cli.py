"""SkyAgent — цветной CLI.  Запуск:  python cli.py  [--model имя] [--yes] [--no-mic] [--no-windows] [--live]"""
import argparse
import atexit
import json
import os
import queue
import subprocess
import sys
import re
import threading
import time
from datetime import date, datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from skyagent import Agent, OLLAMA_URL, list_models
from skyagent.gpu import gate, vram
from skyagent.memory import Event, Memory
from skyagent.memory.context import ContextBuilder
from skyagent.memory.diaries import CLEARERS as DIARY_CLEARERS, REGISTRY as DIARY_REGISTRY, Diaries
from skyagent.memory.journal import Journal
from skyagent.memory.episodes import Episodes
from skyagent.memory.graph import Graph
from skyagent.memory.observer import THRESHOLD as OBS_THRESHOLD, Observer
from skyagent.memory.sleep import Sleep
from skyagent.memory.tools import (about_text, activity_report, episode_lines, fmt_ts, format_rows, parse_day,
                                   register as register_memory_tools)
from skyagent.memory.working import WorkingMemory
from skyagent.triggers import TriggerHub, WakePhrase
from skyagent.sensors.windows import WindowSensor
from skyagent.bridge import Bridge
from skyagent.tts import Speaker
from skyagent import tools as T

console = Console(highlight=False)
SENSOR_NAMES = {"mic": "Микрофон", "windows": "Окна", "telegram": "Telegram"}
HERE = os.path.dirname(os.path.abspath(__file__))
# Чтение памяти не пишем в журнал действий агента — это не действия
MEMORY_READ_TOOLS = {"speech", "recall", "activity", "episodes", "diary", "day_summary", "working_memory", "about",
                     "telegram"}


def today0():
    return datetime.combine(date.today(), datetime.min.time()).timestamp()

BANNER = r"""
   _____ __           ___                    __
  / ___// /____  __  /   | ____ ____  ____  / /_
  \__ \/ //_/ / / / / /| |/ __ `/ _ \/ __ \/ __/
 ___/ / ,< / /_/ / / ___ / /_/ /  __/ / / / /_
/____/_/|_|\__, / /_/  |_\__, /\___/_/ /_/\__/
          /____/        /____/
"""
GRAD = ["#ffffff", "#e0f2ff", "#b9e2ff", "#8fd0ff", "#5fb8ff", "#3a9cff", "#2b7fff"]

HELP = """[bold #8fd0ff]/model[/]  — сменить модель      [bold #8fd0ff]/clear[/] — очистить историю
[bold #8fd0ff]/auto[/]   — авто-подтверждение   [bold #8fd0ff]/jobs[/]  — фоновые задачи
[bold #8fd0ff]/stop N[/] — остановить задачу N  [bold #8fd0ff]/help[/]  — помощь
[bold #8fd0ff]/gui[/]    — открыть окно SkyAgent [bold #8fd0ff]/exit[/]  — выход
[bold #8fd0ff]/mini[/]   — мини-окно поверх всех окон (Ctrl+Alt+Space — показать/спрятать)
[bold #ff9e9e]Память[/]
[bold #8fd0ff]/status[/]  — сенсоры, память, видеопамять
[bold #8fd0ff]/pause[/] [bold #8fd0ff]/resume[/] [mic|windows|telegram] — выключить / включить запись (все или один)
[bold #8fd0ff]/today[/] [N] — день: приложения и речь  [bold #8fd0ff]/find[/] текст — поиск без LLM
[bold #8fd0ff]/episodes[/] [N] — эпизоды сегодня  [bold #8fd0ff]/summary[/] [вчера|дата] — сводка дня
[bold #8fd0ff]/diary[/] [имя] [вчера|дата] — дневники: окна, речь, разговор, telegram, journal…
[bold #8fd0ff]/diary clear[/] имя [дата|все] — очистить дневник  [bold #8fd0ff]/note[/] текст — запись в личный дневник
[bold #8fd0ff]/note[/] — записи сегодня  [bold #8fd0ff]/note del[/] N — удалить запись
[bold #8fd0ff]/memory[/] [now,commitments…] — рабочая память  [bold #8fd0ff]/done[/] [bold #8fd0ff]/drop[/] N — закрыть задачу
[bold #8fd0ff]/graph[/] имя — что память знает о человеке, проекте, теме
[bold #8fd0ff]/observer[/] [now|pause|resume] — наблюдатель: состояние, проверить сейчас, пауза
[bold #ff9e9e]Голос[/] «Скай, <команда>» — команда голосом   [bold #8fd0ff]/enroll[/] [reset] — образец твоего голоса
[bold #8fd0ff]/tts[/] [off|voice|all|stop|test] — голос ассистента: молчать / на голосовые команды / всегда
[bold #8fd0ff]/sleep[/] [now|pause|resume] — фаза сна   [bold #8fd0ff]/junk[/] — помеченное мусором   [bold #8fd0ff]/restore[/] N — вернуть
[bold #8fd0ff]/live[/]    — показывать речь сразу    [bold #8fd0ff]/forget[/] N — удалить последние N минут
[dim]Ctrl+C — прервать ответ. Долгие команды идут в фоне — можно писать дальше.[/]"""


def banner():
    for i, line in enumerate(BANNER.strip("\n").splitlines()):
        console.print(Text(line, style=f"bold {GRAD[i % len(GRAD)]}"))
    console.print(Text("  ☁  локальный ИИ-агент для Ollama — cmd · Python · клавиатура · браузер\n",
                       style="italic #b9e2ff"))


def choose_model(current=None):
    try:
        models = list_models()
    except Exception as e:
        console.print(f"[bold red]Ollama недоступна ({OLLAMA_URL}):[/] {e}\n"
                      "Запусти её командой [bold]ollama serve[/]")
        sys.exit(1)
    if not models:
        console.print("[red]Нет моделей. Установи: ollama pull qwen3:4b[/]")
        sys.exit(1)
    t = Table(title="☁ Установленные модели", border_style="#5fb8ff",
              title_style="bold #8fd0ff", header_style="bold #e0f2ff")
    t.add_column("#", style="bold #3a9cff", justify="right")
    t.add_column("Модель", style="bold white")
    t.add_column("Параметры", style="#8fd0ff")
    t.add_column("Размер", style="#b9e2ff", justify="right")
    for i, (name, gb, params) in enumerate(models, 1):
        mark = " [green]●[/]" if name == current else ""
        t.add_row(str(i), name + mark, params, f"{gb:.1f} ГБ")
    console.print(t)
    default = next((str(i) for i, m in enumerate(models, 1) if m[0] == current), "1")
    while True:
        ans = Prompt.ask("[bold #8fd0ff]Номер модели[/]", default=default)
        if ans.isdigit() and 1 <= int(ans) <= len(models):
            return models[int(ans) - 1][0]
        match = [m[0] for m in models if ans in m[0]]
        if match:
            return match[0]
        console.print("[red]Нет такой модели[/]")


class Printer:
    """Красиво выводит события агента."""

    def __init__(self):
        self.mode = None  # token / think

    def end(self):
        if self.mode:
            console.print()
            self.mode = None

    def __call__(self, kind, data):
        if kind == "think":
            if self.mode != "think":
                self.end()
                console.print("[dim italic #8fd0ff]💭 [/]", end="")
                self.mode = "think"
            console.print(Text(data, style="dim italic #8fb8d8"), end="")
        elif kind == "token":
            if self.mode != "token":
                self.end()
                console.print("[bold #5fb8ff]☁ SkyAgent:[/] ", end="")
                self.mode = "token"
            console.print(Text(data, style="#e8f6ff"), end="")
        elif kind == "tool":
            self.end()
            args = json.dumps(data["args"], ensure_ascii=False, indent=1)
            console.print(Panel(Text(args, style="#ffe9a8"),
                                title=f"[bold #ffd166]⚡ {data['name']}[/]",
                                border_style="#ffd166", expand=False))
        elif kind == "result":
            self.end()
            res = data["result"]
            if res.startswith("Задача #") and "в ФОНЕ" in res:
                return  # об этом уже сообщило событие job_start
            if len(res) > 1500:
                res = res[:1500] + "\n…"
            console.print(Panel(Text(res, style="#c8facc"), title="[bold #7ee08a]✔ результат[/]",
                                border_style="#4caf50", expand=False))
        elif kind == "job_start":
            self.end()
            console.print(f"[bold #c792ea]⏳ задача #{data['id']} ({data['name']}) продолжает работать "
                          "в фоне — можно писать дальше[/]")
        elif kind == "job_done":
            self.end()
            res = data["result"]
            if len(res) > 1500:
                res = res[:1500] + "\n…"
            console.print(Panel(Text(res, style="#c8facc"),
                                title=f"[bold #c792ea]✔ фоновая задача #{data['id']} ({data['name']}) "
                                      f"завершена за {data['seconds']:.0f} c[/]",
                                border_style="#c792ea", expand=False))
        elif kind == "info":
            self.end()
            console.print(f"[bold magenta]ℹ {data}[/]")
        elif kind == "done":
            self.end()


class Inbox:
    """Единая очередь: строки, введённые пользователем, и завершённые фоновые задачи.
    Ввод читает отдельный поток, поэтому агент может сообщить итог задачи,
    даже когда пользователь ничего не пишет."""

    def __init__(self):
        self.q = queue.Queue()
        self.deferred = []
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        eofs = []
        while True:
            try:
                self.q.put(("line", input().lstrip("﻿")))
            except EOFError:  # Ctrl+C в Windows или закрытый stdin
                now = time.time()
                eofs = [t for t in eofs if now - t < 1] + [now]
                if len(eofs) >= 3:
                    self.q.put(("eof", None))
                    return
                self.q.put(("ctrlc", None))
                time.sleep(0.1)
            except KeyboardInterrupt:
                self.q.put(("ctrlc", None))

    def put_job(self, data):
        self.q.put(("job", data))

    def put_notify(self, message, urgency):
        self.q.put(("notify", (message, urgency)))

    def put_voice(self, cmd, confirm, info):
        self.q.put(("voice", (cmd, confirm, info)))

    def put_info(self, text):
        self.q.put(("info", text))

    def put_memory(self, event):
        self.q.put(("mem", event))

    def put_gui(self, text):
        self.q.put(("gui", text))

    def put_confirm(self, allow):
        self.q.put(("confirm", bool(allow)))

    def get(self, kinds):
        for i, item in enumerate(self.deferred):
            if item[0] in kinds:
                return self.deferred.pop(i)
        while True:
            try:
                item = self.q.get(timeout=0.2)  # таймаут — чтобы Ctrl+C работал в Windows
            except queue.Empty:
                continue
            if item[0] in kinds:
                return item
            self.deferred.append(item)


def main():
    ap = argparse.ArgumentParser(description="SkyAgent CLI")
    ap.add_argument("--model", help="имя модели Ollama")
    ap.add_argument("--yes", action="store_true", help="выполнять всё без подтверждения")
    ap.add_argument("--no-mic", action="store_true", help="не включать микрофон")
    ap.add_argument("--no-windows", action="store_true", help="не записывать активные окна")
    ap.add_argument("--no-episodes", action="store_true", help="не делать сводки эпизодов в фоне")
    ap.add_argument("--no-graph", action="store_true", help="не подключать граф памяти (Neo4j)")
    ap.add_argument("--no-observer", action="store_true", help="не запускать наблюдателя")
    ap.add_argument("--no-context", action="store_true", help="не подкладывать агенту контекст из памяти")
    ap.add_argument("--no-sleep", action="store_true", help="не запускать фазу сна")
    ap.add_argument("--live", action="store_true", help="сразу показывать распознанную речь")
    ap.add_argument("--no-telegram", action="store_true", help="не читать Telegram")
    ap.add_argument("--tts", choices=("off", "voice", "all"), help="голос ассистента (по умолчанию SKYAGENT_TTS или voice)")
    ap.add_argument("--mini", action="store_true", help="сразу открыть мини-окно поверх всех окон")
    a = ap.parse_args()

    banner()
    model = a.model or choose_model()
    printer = Printer()
    inbox = Inbox()
    status = {"spinner": None}

    # ---------------------------------------------------------------- память и микрофон
    memory = Memory(gate=gate).start()
    vram.start()   # сторож: модель не должна вылезать из видеопамяти в общую ОЗУ (тогда ~1 токен/с)
    # рабочая модель держится в памяти, пока агент запущен (keep_alive=-1); на выходе — отпустить видеокарту
    atexit.register(lambda: vram.primary and vram.unload(vram.primary))
    mic = voice = None
    if not a.no_mic:
        from skyagent.sensors.audio import MicSensor   # тяжёлый импорт — только если нужен
        try:
            from skyagent.sensors.speaker import VoicePrint
            voice = VoicePrint(memory.data_dir)
        except Exception as e:  # noqa: BLE001 — без отпечатка: фразы без метки «я / другой»
            console.print(f"[yellow]Голосовой отпечаток недоступен: {e}[/]")
        mic = MicSensor(memory.publish, gate=gate, voice=voice)
        mic.start()
        if mic.state == "error":
            console.print(f"[bold red]Микрофон не включился:[/] {mic.error}")
        else:
            console.print(f"[#ff9e9e]● микрофон пишет:[/] {mic.device_name} · "
                          f"[dim]{'Whisper' if mic.engine == 'whisper' else 'Vosk (CPU)'} {mic.model_name} загружается в фоне[/]")
            if voice and not voice.enrolled:
                console.print("[yellow]Образец твоего голоса не записан — команды «Скай, …» будут спрашивать "
                              "подтверждение. Запиши: /enroll[/]")
    win = None
    if not a.no_windows:
        win = WindowSensor(memory)
        win.start()
        console.print("[#8fd0ff]● окна записываются[/] [dim](приватные — как «скрыто»)[/]")
    tg = None
    if not a.no_telegram:
        from skyagent.sensors.telegram import TelegramSensor, credentials
        if credentials()[0]:
            tg = TelegramSensor(memory).start()
            console.print("[#8fd0ff]● Telegram подключается[/] [dim](только чтение; /status — состояние)[/]")
        else:
            console.print("[dim]Telegram не настроен: TG_API_ID и TG_API_HASH в .env, затем "
                          "scripts\\telegram_login.py (см. .env.example)[/]")
    sensors = {k: s for k, s in (("mic", mic), ("windows", win), ("telegram", tg)) if s}
    holder = {}
    # Сводки пишет та же модель, что и агент (одна модель в видеопамяти), если не задана своя
    episodes = Episodes(memory, model_fn=lambda: os.environ.get("SKYAGENT_SUMMARY_MODEL") or holder["agent"].model,
                        gate=gate)
    working = WorkingMemory(memory, episodes, sensors).start()
    journal = Journal(memory)
    diaries = Diaries(memory, episodes, working, journal=journal).start()
    graph = None
    if not a.no_graph:
        try:
            graph = Graph(memory, episodes).start()
            console.print(f"[#8fd0ff]● граф памяти:[/] Neo4j, пространство «{graph.ns}»")
        except Exception as e:  # noqa: BLE001 — без графа память работает, просто без R1
            console.print(f"[yellow]Граф памяти выключен: Neo4j недоступен ({type(e).__name__}). "
                          r"Запусти: scripts\neo4j.ps1 start[/]")
    register_memory_tools(memory, mic, win, episodes, working, graph, diaries, journal)
    context_builder = ContextBuilder(memory, working, graph,
                                     model_fn=lambda: os.environ.get("SKYAGENT_SUMMARY_MODEL") or holder["agent"].model)
    live = {"on": a.live}
    memory.bus.subscribe(lambda ev: inbox.put_memory(ev) if live["on"] and ev.kind == "speech" else None)

    # ---------------------------------------------------------------- мост к мини-окну и голос
    bridge = Bridge(memory.data_dir)
    try:
        bridge.start()
    except OSError as e:
        console.print(f"[yellow]Мост для мини-окна не поднялся: {e}[/]")
    speaker = Speaker(mic, mode=a.tts, on_state=lambda on, text: bridge.emit("speaking", {"on": on, "text": text}))

    def say_aloud(text):
        return "Говорю вслух." if speaker.say(text, force=True) else "Нечего произносить."

    T.register(T._fn("say_aloud", "Произнести текст вслух голосом ассистента (через динамики). Только когда "
                     "пользователь просит сказать/прочитать вслух; обычные ответы озвучиваются сами.",
                     {"text": T.S("Что сказать")}, ["text"]), say_aloud)

    def on_event(kind, data):
        if kind in ("token", "info", "done", "job_start"):
            bridge.emit(kind, data)
        elif kind == "job_done":
            bridge.emit(kind, {k: data.get(k) for k in ("id", "name", "seconds")})
        elif kind == "tool":
            bridge.emit("tool", {"name": data["name"]})
        elif kind == "think":
            bridge.emit("think", None)
        # Разговор с агентом — тоже память: что просил, что сделано (рабочая память agent.md)
        if kind == "tool" and data["name"] not in MEMORY_READ_TOOLS:
            memory.publish(Event("agent", "action", f"{data['name']}({json.dumps(data['args'], ensure_ascii=False)})"[:300],
                                 trust="system", meta={"tool": data["name"]}))
        elif kind == "done" and data:
            memory.publish(Event("agent", "answer", str(data)[:600], trust="system"))
        if kind == "job_done":  # приходит из фонового потока — обработаем в главном
            inbox.put_job(data)
            return
        if status["spinner"]:
            status["spinner"].stop()
            status["spinner"] = None
        printer(kind, data)

    def confirm(name, args):
        # Спрашиваем и здесь, и в мини-окне: какой ответ придёт первым, тот и решает
        if status["spinner"]:
            status["spinner"].stop()
            status["spinner"] = None
        cid = bridge.seq + 1
        bridge.emit("confirm", {"id": cid, "name": name, "args": json.dumps(args, ensure_ascii=False)[:400]})
        console.print(f"[bold yellow]Разрешить [white]{name}[/]? [y/n] (y):[/] ", end="")
        kind, line = inbox.get({"line", "eof", "confirm"})
        bridge.emit("confirm_done", {"id": cid})
        if kind == "confirm":
            console.print("да [dim](из мини-окна)[/]" if line else "нет [dim](из мини-окна)[/]")
            return line
        return kind == "line" and line.strip().lower() in ("", "y", "yes", "д", "да")

    agent = Agent(model, on_event=on_event, confirm=confirm, auto_approve=a.yes)
    holder["agent"] = agent
    if not a.no_episodes:
        episodes.start()
    # Наблюдатель — та же модель, что у агента (одна модель в видеопамяти), если не задана своя
    observer = Observer(memory, working,
                        model_fn=lambda: os.environ.get("SKYAGENT_OBSERVER_MODEL") or agent.model,
                        gate=gate, sensors=sensors, on_notify=inbox.put_notify)
    if not a.no_observer:
        observer.start()
    # Фаза сна: когда тебя нет — тяжёлая модель чистит и уточняет память
    sleep = Sleep(memory, episodes, working, graph, diaries, gate=gate, sensors=sensors, on_info=inbox.put_info,
                  fallback_fn=lambda: os.environ.get("SKYAGENT_SUMMARY_MODEL") or agent.model)
    sleep.pick_model()
    if not a.no_sleep:
        sleep.start()
    # Триггеры: «Скай, <команда>» голосом → агенту, как будто ты это написал
    triggers = TriggerHub(memory)
    wake = triggers.add(WakePhrase(on_command=inbox.put_voice, voice=voice, video_at=observer.video_at))
    console.print(Panel(HELP, title=f"[bold]модель: [#8fd0ff]{model}[/][/]",
                        subtitle="авто-подтверждение: " + ("[green]ВКЛ[/]" if a.yes else "[yellow]выкл[/]"),
                        border_style="#3a9cff"))

    # ---------------------------------------------------------------- маршруты моста (мини-окно)
    busy = {"on": False}
    bridge.emit("hello", {"model": model})

    def open_mini():
        pyw = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        subprocess.Popen([exe, os.path.join(HERE, "mini.py")], cwd=HERE,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))

    @bridge.route("GET", "/status")
    def _status(q):
        sens = {k: {"state": v.state, "error": v.error} for k, v in sensors.items()}
        return {"model": agent.model, "busy": busy["on"], "sensors": sens, "jobs": len(agent.running_jobs()),
                "tts": {"mode": speaker.mode, "engine": speaker.engine_name, "speaking": speaker.speaking,
                        "error": speaker.error},
                "observer": observer.state, "auto": agent.auto_approve}

    @bridge.route("POST", "/say")
    def _say(q):
        text = (q.get("text") or "").strip()
        if text:
            inbox.put_gui(text)
        return {"ok": bool(text)}

    @bridge.route("POST", "/confirm")
    def _confirm(q):
        inbox.put_confirm(bool(q.get("allow")))

    @bridge.route("POST", "/stop")
    def _stop(q):
        speaker.stop()
        if busy["on"]:
            agent.stop_flag = True

    @bridge.route("POST", "/tts")
    def _tts(q):
        if q.get("mode"):
            speaker.set_mode(q["mode"])
        if q.get("action") == "stop":
            speaker.stop()
        elif q.get("action") == "test":
            speaker.say("Привет! Я Скай. Так звучит мой голос.", force=True)
        return {"mode": speaker.mode, "engine": speaker.engine_name}

    @bridge.route("POST", "/sensor")
    def _sensor(q):
        sens = sensors.get(q.get("name"))
        if not sens:
            return {"error": "нет такого сенсора"}
        sens.resume() if q.get("on") else sens.pause()
        return {"state": sens.state}

    def _day(q):
        return parse_day(q.get("day") or "")

    @bridge.route("GET", "/journal")
    def _journal(q):
        return {"entries": [{"id": r["id"], "ts": r["ts"], "text": r["text"], "via": r["meta"].get("via")}
                            for r in journal.day(_day(q))]}

    @bridge.route("POST", "/journal/add")
    def _journal_add(q):
        return {"id": journal.add(q.get("text") or "", via="gui")}

    @bridge.route("POST", "/journal/edit")
    def _journal_edit(q):
        return {"ok": journal.edit(int(q["id"]), q.get("text") or "")}

    @bridge.route("POST", "/journal/del")
    def _journal_del(q):
        return {"ok": journal.delete(int(q["id"]))}

    @bridge.route("GET", "/diaries")
    def _diaries(q):
        return {"diaries": [{"name": n, "title": t, "clearable": n in DIARY_CLEARERS}
                            for n, (t, _) in DIARY_REGISTRY.items()], "days": diaries.days()[:30]}

    @bridge.route("GET", "/diary")
    def _diary(q):
        return {"text": diaries.read(q.get("name", ""), _day(q))}

    @bridge.route("POST", "/diary/clear")
    def _diary_clear(q):
        d = None if q.get("all") else _day(q)
        n = diaries.clear(q.get("name", ""), d)
        console.print(f"\n[dim]⧉ мини-окно: очищен дневник {q.get('name')} "
                      f"({'за всё время' if d is None else d.isoformat()}), удалено {n}[/]")
        return {"deleted": n}

    if a.mini:
        open_mini()

    def run_agent(text, via="text"):
        speaker.stop()          # новый вопрос обрывает недоговорённый ответ
        status["spinner"] = console.status("[#8fd0ff]думаю…[/]", spinner="dots")
        status["spinner"].start()
        busy["on"] = True
        bridge.emit("busy", {"on": True, "text": text, "via": via})
        # Пока пользователь ждёт, фоновые запросы к модели прерываются и не стартуют: иначе Ollama
        # обслуживает их по очереди, а «думаю…» висит, пока не допишется чья-то сводка
        user_turn = gate.user()
        user_turn.__enter__()
        try:
            ctx = ""
            if text and not a.no_context:
                try:     # контекст — до записи вопроса в память, иначе поиск найдёт сам вопрос
                    ctx, _ = context_builder.build(text)
                except Exception as e:  # noqa: BLE001 — без автоконтекста агент всё равно ответит
                    printer.end()
                    console.print(f"[dim yellow]автоконтекст не собран: {e}[/]")
            if text:
                memory.publish(Event("user", "message", text, trust="user", meta={"via": via}))
            answer = agent.run(text, context=ctx)
            if answer and speaker.wants(via):
                speaker.say(answer)
        except KeyboardInterrupt:
            agent.stop_flag = True
            printer.end()
            console.print("[yellow]⏹ ответ прерван (фоновые задачи продолжают работать — /jobs)[/]")
        except Exception as e:
            printer.end()
            console.print(f"[bold red]Ошибка:[/] {e}")
        finally:
            user_turn.__exit__(None, None, None)
            busy["on"] = False
            bridge.emit("busy", {"on": False})
            if status["spinner"]:
                status["spinner"].stop()
                status["spinner"] = None
            printer.end()

    def prompt():
        n = len(agent.running_jobs())
        jobs = f" [#c792ea]⏳{n}[/]" if n else ""
        states = {name: s.state for name, s in sensors.items()}
        if any(v in ("recording", "away") for v in states.values()):
            rec = " [bold #ff6b6b]● REC[/]"
            off = [SENSOR_NAMES[k] for k, v in states.items() if v not in ("recording", "away")]
            rec += f" [dim](без: {', '.join(off)})[/]" if off else ""
        elif states:
            rec = " [yellow]⏸ пауза[/]"
        else:
            rec = ""
        console.print(f"\n[bold #ffffff on #2b7fff] Ты [/]{rec}{jobs} ", end="")

    def show_status():
        t = Table(border_style="#5fb8ff", show_header=False, title="☁ Состояние", title_style="bold #8fd0ff")
        t.add_column(style="bold #8fd0ff")
        t.add_column(style="white")
        if mic:
            st = mic.stats
            t.add_row("Микрофон", f"{mic.state} · {getattr(mic, 'device_name', '?')}")
            t.add_row("Распознавание", f"{mic.engine}: {mic.model_name} на {mic.whisper_device or '…'} · "
                      + ("готов" if mic.model_ready else "загружается"))
            t.add_row("Фраз", f"{st['phrases']} записано, {st['dropped']} отброшено, в очереди {mic.backlog}")
            if st["last_at"]:
                t.add_row("Последняя", f"[{fmt_ts(st['last_at'])}] {st['last_text'][:80]}")
            if mic.error:
                t.add_row("Ошибка", f"[red]{mic.error}[/]")
        else:
            t.add_row("Микрофон", "выключен (--no-mic)")
        if win:
            t.add_row("Окна", f"{win.state} · сейчас: {win.now_text[:70]}")
            t.add_row("Переключений", f"{win.stats['switches']} (мельканий отброшено: {win.stats['ignored']})")
            if win.error:
                t.add_row("Ошибка окон", f"[red]{win.error}[/]")
        else:
            t.add_row("Окна", "выключены (--no-windows)")
        if tg:
            st = tg.stats
            t.add_row("Telegram", f"{tg.state} · сообщений {st['messages']} (догружено {st['backfilled']}, "
                      f"пропущено {st['skipped']})" + (f" · [red]{tg.error}[/]" if tg.error else ""))
            if st["last_at"]:
                t.add_row("", f"[{fmt_ts(st['last_at'])}] {st['last_text']}")
        t.add_row("Голос", f"{speaker.mode} · {speaker.engine_name} · сказано {speaker.stats['said']}"
                  + (f" · эхо отброшено {mic.stats['echo']}" if mic else "")
                  + (f" · [red]{speaker.error}[/]" if speaker.error else ""))
        t.add_row("Мини-окно", ("подключено" if bridge.gui_connected else "закрыто (/mini)") + f" · мост :{bridge.port}")
        day0 = today0()
        tl = memory.timeline
        t.add_row("Память", f"{tl.count()} событий; сегодня: речь {tl.count(day0, None, 'mic', ('speech',))}, "
                            f"окна {tl.count(day0, None, 'windows', ('focus',))}")
        st_ep = episodes.stats
        t.add_row("Эпизоды", f"сегодня {len(episodes.list(day0, None))}, ждут сводки: {episodes.pending_count()}"
                  + (f" · модель {episodes.model_fn()}" if holder.get("agent") else "")
                  + (f" · [red]{episodes.error}[/]" if episodes.error else "")
                  + ("" if not a.no_episodes else " · [yellow]фон выключен[/]"))
        if st_ep["failed"]:
            t.add_row("", f"[yellow]сводок не удалось: {st_ep['failed']}[/]")
        if voice:
            t.add_row("Голос", (f"образец записан ({voice.info.get('phrases')} фраз, {voice.info.get('seconds')} c)"
                                if voice.enrolled else "[yellow]образец не записан — /enroll[/]")
                      + f" · «Скай»: выполнено {wake.stats['fired']}, отклонено {wake.stats['ignored']}")
        t.add_row("Сон", f"{sleep.state}, ждут {len(sleep.pending())} эпизодов, модель {sleep.model}"
                  + (f" · [red]{sleep.error}[/]" if sleep.error else ""))
        t.add_row("Наблюдатель", f"{observer.state}, следующая проверка через "
                                 f"{max(0, observer.next_check - time.time()) / 60:.0f} мин, проверок {observer.stats['checks']}"
                  + (f" · [red]{observer.error}[/]" if observer.error else ""))
        t.add_row("Рабочая память", f"{working.dir} · открытых задач: {len(working.commitments('open'))}"
                  + (f" · [red]{working.error}[/]" if working.error else ""))
        if graph:
            try:
                c = graph.stats_counts()
                t.add_row("Граф", f"сущностей {c.get('Entity', 0)}, фактов {c.get('Fact', 0)}, эпизодов "
                          f"{c.get('Episode', 0)}, связей {c.get('RELATED', 0)}"
                          + (f" · [red]{graph.error}[/]" if graph.error else ""))
            except Exception as e:  # noqa: BLE001
                t.add_row("Граф", f"[red]недоступен: {e}[/]")
        else:
            t.add_row("Граф", "выключен")
        t.add_row("Векторы", f"ждут: {tl.count_pending()}"
                  + (f" · [red]{memory.embedder.error}[/]" if memory.embedder.error else ""))
        t.add_row("База", str(tl.path))
        try:
            used, total = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout.strip().split(", ")
            t.add_row("Видеопамять", f"{int(used) / 1024:.1f} / {int(total) / 1024:.1f} ГБ")
        except Exception:  # noqa: BLE001
            pass
        for line in vram.report():
            t.add_row("", line)
        console.print(t)

    def show_observer():
        t = Table(border_style="#5fb8ff", show_header=False, title="👁 Наблюдатель", title_style="bold #8fd0ff")
        t.add_column(style="bold #8fd0ff")
        t.add_column(style="white")
        now = time.time()
        t.add_row("Состояние", observer.state + (f" · [red]{observer.error}[/]" if observer.error else ""))
        t.add_row("Значимость", f"{observer.salience:.1f} из порога {OBS_THRESHOLD}, событий: {len(observer.pending)}")
        t.add_row("Следующая", f"через {max(0, observer.next_check - now) / 60:.0f} мин (check_interval)"
                  if observer.state != "paused" else "на паузе")
        due = sorted(d for d, _ in observer.due_list() if d > now - 3600)
        if due:
            t.add_row("Ближайший срок", fmt_ts(due[0], full=True))
        s = observer.stats
        t.add_row("Проверок", f"{s['checks']}: ничего {s['none']}, запомнил {s['remember']}, "
                              f"сообщил {s['notified']}, подавлено {s['suppressed']}")
        if observer.last:
            d = observer.last
            t.add_row("Последнее", f"[{fmt_ts(d['ts'])}] {d['action']} ({d['reason']}, {d['sec']} c) — {d['why'][:120]}")
        if observer.held:
            t.add_row("Отложено", f"{len(observer.held)} — покажу, когда вернёшься")
        t.add_row("Журнал", str(observer.snap_dir))
        console.print(t)

    def show_rows(rows, empty="Пусто"):
        console.print(Text(format_rows(rows, memory=memory), style="#e8f6ff") if rows else f"[dim]{empty}[/]")

    prompt()
    while True:
        try:
            kind, data = inbox.get({"line", "job", "eof", "ctrlc", "mem", "notify", "voice", "info", "gui", "confirm"})
        except KeyboardInterrupt:
            break
        if kind == "eof":
            break
        if kind == "ctrlc":
            continue
        if kind == "info":
            console.print(f"\n[#8fd0ff]{data}[/]")
            bridge.emit("info", data)
            prompt()
            continue
        if kind == "confirm":   # ответ на подтверждение, которое уже никто не ждёт
            continue
        via = "text"
        if kind == "gui":       # написано в мини-окне — дальше как строка, введённая здесь
            console.print(f"[dim]⧉ из мини-окна:[/] {data}")
            kind, via = "line", "gui"
        if kind == "voice":
            cmd, need_confirm, info = data
            console.print()
            bridge.emit("voice", {"cmd": cmd, "listening": bool(info.get("listening"))})
            if info.get("listening"):
                console.print("[bold #ff9e9e]🎙 Слушаю…[/] [dim](скажи команду)[/]")
                if speaker.mode != "off":
                    speaker.say("Да?")
                prompt()
                continue
            console.print(f"[bold #ff9e9e]🎙 Скай:[/] [bold white]{cmd}[/] [dim]({info.get('why')})[/]")
            if need_confirm:
                console.print("[bold yellow]Выполнить голосовую команду? [y/n] (n):[/] ", end="")
                k, ans = inbox.get({"line", "eof"})
                if not (k == "line" and ans.strip().lower() in ("y", "yes", "д", "да")):
                    console.print("[dim]Не выполняю[/]")
                    prompt()
                    continue
            run_agent(cmd, via="voice")
            prompt()
            continue
        if kind == "notify":
            message, urgency = data
            bridge.emit("notify", {"message": message, "urgency": urgency})
            if speaker.wants("notify"):
                speaker.say(message)
            color = {3: "#ff6b6b", 2: "#ffd166"}.get(urgency, "#8fd0ff")
            console.print()
            console.print(Panel(Text(message, style="bold white"), title=f"[bold {color}]🔔 SkyAgent[/]",
                                border_style=color, expand=False))
            if os.name == "nt" and not speaker.wants("notify"):
                try:
                    import winsound
                    winsound.MessageBeep()
                except Exception:  # noqa: BLE001
                    pass
            prompt()
            continue
        if kind == "mem":
            console.print(f"\n[dim #ff9e9e]🎙 [{fmt_ts(data.ts)}][/] [dim]{data.text}[/]")
            prompt()
            continue
        if kind == "job":
            console.print()
            printer("job_done", data)
            if agent.has_pending():  # агент ещё не видел результат — пусть прокомментирует
                run_agent(None)
            prompt()
            continue

        q = data.strip()
        if not q:
            prompt()
            continue
        if q in ("/exit", "/quit", "/q"):
            break
        if q == "/gui":
            bat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SkyAgent.bat")
            subprocess.Popen(["cmd", "/c", "start", "", bat], cwd=os.path.dirname(bat))
            console.print("[#8fd0ff]Открываю окно SkyAgent ☁[/]")
            break
        if q == "/help":
            console.print(HELP)
        elif q == "/clear":
            agent.reset()
            console.print("[green]История очищена[/]")
        elif q == "/auto":
            agent.auto_approve = not agent.auto_approve
            console.print("Авто-подтверждение: " + ("[green]ВКЛ[/]" if agent.auto_approve else "[yellow]выкл[/]"))
        elif q == "/status":
            show_status()
        elif q.split()[0] in ("/pause", "/resume"):
            cmd, *which = q.split()
            targets = {k: s for k, s in sensors.items() if not which or k in which}
            if not targets:
                console.print(f"[yellow]Нет такого сенсора. Есть: {', '.join(sensors) or 'никаких'}[/]")
            for name, s in targets.items():
                s.pause() if cmd == "/pause" else s.resume()
                console.print(f"{SENSOR_NAMES[name]}: [bold]{s.state}[/]" + (f" [red]{s.error}[/]" if s.error else ""))
        elif q == "/live":
            live["on"] = not live["on"]
            console.print("Показ речи сразу: " + ("[green]ВКЛ[/]" if live["on"] else "[yellow]выкл[/]"))
        elif q.startswith("/today"):
            n = q[6:].strip()
            day0 = today0()
            rep = activity_report(memory, day0, time.time(), current_id=(win.current or {}).get("id") if win else None)
            console.print(Panel(Text(rep or "Активность окон сегодня не записана", style="#e8f6ff"),
                                title="[bold #8fd0ff]🪟 Сегодня за компьютером[/]", border_style="#3a9cff"))
            console.print("[bold #ff9e9e]🎙 Речь[/]")
            show_rows(memory.timeline.range(day0, None, "mic", ("speech",),
                                            limit=int(n) if n.isdigit() else 20, newest=True),
                      "Сегодня речи ещё не записано")
        elif q.startswith("/sleep"):
            arg = q[6:].strip()
            if arg == "now":
                if sleep.state == "sleeping":
                    console.print("[yellow]Сон уже идёт[/]")
                else:
                    n = len(sleep.pending())
                    console.print(f"[#8fd0ff]🌙 Засыпаю: эпизодов к обработке {n}, модель {sleep.model}. "
                                  "Прогресс появится здесь; можно писать дальше.[/]")
                    threading.Thread(target=lambda: inbox.put_info(
                        Sleep.report_text(r) if (r := sleep.run(manual=True)) else f"Сон не удался: {sleep.error}"),
                        daemon=True).start()
            elif arg in ("pause", "resume"):
                sleep.pause() if arg == "pause" else sleep.resume()
            t = Table(border_style="#5fb8ff", show_header=False, title="🌙 Фаза сна", title_style="bold #8fd0ff")
            t.add_column(style="bold #8fd0ff")
            t.add_column(style="white")
            t.add_row("Состояние", sleep.state + (f" · [red]{sleep.error}[/]" if sleep.error else ""))
            t.add_row("Модель", sleep.model + (" [yellow](запасная: тяжёлая модель не скачана — "
                                               "ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M)[/]" if sleep.fallback else ""))
            t.add_row("Ждут обработки", f"{len(sleep.pending())} эпизодов · сплю, если тебя нет ≥ 20 мин (ночью ≥ 10)")
            s = sleep.stats
            t.add_row("Всего", f"снов {s['runs']}, эпизодов {s['episodes']}, исправлено {s['fixes']}, мусор {s['noise']}, "
                               f"тегов {s['tags']}, слияний {s['merged']}, удалено старого мусора {s['purged']}")
            if sleep.last:
                t.add_row("Последний", f"[{fmt_ts(sleep.last['ts'])}] эпизодов {sleep.last['episodes']}, {sleep.last['sec']} c")
            console.print(t)
        elif q.startswith("/junk"):
            rows = sleep.junk()
            for r in rows[:40]:
                console.print(f"  [dim]#{r['id']}[/] [{fmt_ts(r['ts'])}] {r['text'][:100]}")
            console.print(f"[dim]Помечено мусором: {len(rows)}. Удалится через 14 дней после пометки. "
                          "Вернуть: /restore N[,N…][/]" if rows else "[dim]Мусора нет[/]")
        elif q.startswith("/restore"):
            ids = [int(x) for x in re.findall(r"\d+", q[8:])]
            if ids:
                memory.timeline.unhide(ids)
                console.print(f"[green]Возвращено: {len(ids)}[/]")
            else:
                console.print("[yellow]/restore N[,N…] — номера из /junk[/]")
        elif q.startswith("/enroll"):
            if not mic or not voice:
                console.print("[yellow]Нужны микрофон и модель голосовых отпечатков (models/)[/]")
            elif q[7:].strip() == "reset":
                voice.reset()
                console.print("[green]Образец голоса удалён[/]")
            else:
                from skyagent.sensors.speaker import ENROLL_SEC, Enroller
                console.print(Panel(Text(
                    f"Говори обычным голосом {ENROLL_SEC:.0f}–60 секунд: о чём угодно, фразами по несколько слов. "
                    "Лучше в тишине, без музыки и видео. Прогресс появится здесь; можно писать дальше.",
                    style="white"), title="[bold #ff9e9e]🎙 Запись образца голоса[/]", border_style="#ff9e9e"))
                Enroller(voice, mic,
                         on_progress=lambda s, need: inbox.put_info(f"🎙 образец: {min(s, need):.0f} из {need:.0f} c речи"),
                         on_done=lambda info: inbox.put_info(
                             f"✔ Образец голоса записан: {info['phrases']} фраз, {info['seconds']} c, сходство фраз "
                             f"с отпечатком {info['self_sim_min']}–{info['self_sim_mean']}. Теперь у фраз будет "
                             "метка «я / другой», а «Скай, …» работает без подтверждения."
                             if info else "✖ Мало речи для образца — попробуй /enroll ещё раз"))
        elif q.startswith("/observer"):
            arg = q[9:].strip()
            if arg == "now":
                with console.status("[#8fd0ff]наблюдатель смотрит…[/]", spinner="dots"), gate.user():
                    dec = observer.check("вручную")
                if dec is None:
                    console.print(f"[red]Проверка не удалась: {observer.error}[/]")
            elif arg in ("pause", "resume"):
                observer.pause() if arg == "pause" else observer.resume()
            show_observer()
        elif q.startswith("/memory"):
            names = [n.strip() + ("" if n.strip().endswith(".md") else ".md") for n in q[7:].split(",") if n.strip()]
            working.refresh()
            console.print(Panel(Text(working.read(names or None) or "Пусто", style="#e8f6ff"),
                                title=f"[bold #8fd0ff]🧠 Рабочая память[/] [dim]{working.dir}[/]", border_style="#3a9cff"))
        elif q.startswith("/done") or q.startswith("/drop"):
            n = q[5:].strip()
            if not n.lstrip("#").isdigit():
                console.print("[yellow]/done N или /drop N — номер задачи из /memory commitments[/]")
            else:
                ok = working.set_commitment(int(n.lstrip("#")), "done" if q.startswith("/done") else "dropped")
                console.print("[green]Готово[/]" if ok else "[yellow]Нет такой задачи[/]")
        elif q.startswith("/graph"):
            name = q[6:].strip()
            if not graph:
                console.print("[yellow]Граф выключен (Neo4j недоступен или --no-graph)[/]")
            elif not name:
                console.print("[yellow]/graph имя — что память знает о человеке, проекте, теме[/]")
            else:
                prof = about_text(graph.about(name))
                console.print(Panel(Text(prof or f"В графе нет «{name}»", style="#e8f6ff"),
                                    title=f"[bold #8fd0ff]🕸 {name}[/]", border_style="#3a9cff"))
        elif q.startswith("/episodes"):
            n = q[9:].strip()
            eps = episodes.list(today0(), None)
            eps = eps[-int(n):] if n.isdigit() else eps
            text = episode_lines(eps, episodes.open_episode())
            console.print(Panel(Text(text or "Сегодня эпизодов ещё нет", style="#e8f6ff"),
                                title="[bold #8fd0ff]📒 Эпизоды сегодня[/]", border_style="#3a9cff"))
        elif q.startswith("/diary clear"):
            args = q[12:].split()
            if not args:
                console.print(f"[yellow]/diary clear имя [сегодня|вчера|дата|все]; можно: {', '.join(DIARY_CLEARERS)}[/]")
            else:
                name, when = args[0].lower(), " ".join(args[1:]).lower()
                try:
                    d = None if when in ("все", "всё", "all") else parse_day(when)
                except ValueError:
                    d = False
                if d is False or name not in DIARY_REGISTRY:
                    console.print(f"[yellow]/diary clear имя [сегодня|вчера|дата|все]; можно: {', '.join(DIARY_CLEARERS)}[/]")
                elif name not in DIARY_CLEARERS:
                    from skyagent.memory.diaries import NOT_CLEARABLE
                    console.print(f"[yellow]Дневник {name} не чистится: {NOT_CLEARABLE.get(name, 'он собран из других')}[/]")
                else:
                    period = "за ВСЁ время" if d is None else f"за {d.isoformat()}"
                    console.print(f"[bold yellow]Очистить дневник {name} {period}? Записи удалятся из памяти "
                                  "насовсем. [y/n] (n):[/] ", end="")
                    k, ans = inbox.get({"line", "eof"})
                    if k == "line" and ans.strip().lower() in ("y", "yes", "д", "да"):
                        try:
                            console.print(f"[green]Удалено записей: {diaries.clear(name, d)}[/]")
                        except ValueError as e:
                            console.print(f"[yellow]Не чистится: {e}[/]")
                    else:
                        console.print("[dim]Ничего не удалено[/]")
        elif q.startswith("/diary"):
            args = q[6:].split()
            if not args:
                console.print("Дневники: " + ", ".join(f"[bold #8fd0ff]{n}[/] — {t}" for n, (t, _) in DIARY_REGISTRY.items()))
                console.print(f"[dim]Дни: {', '.join(diaries.days()[:10]) or 'пока нет'} · /diary имя [вчера|дата] · "
                              f"файлы: {diaries.dir}[/]")
            else:
                try:
                    d = parse_day(" ".join(args[1:]))
                    text = diaries.read(args[0].lower(), d)
                    console.print(Panel(Text(text, style="#e8f6ff"), border_style="#3a9cff"))
                except (ValueError, KeyError):
                    console.print(f"[yellow]/diary имя [сегодня|вчера|YYYY-MM-DD]; имена: {', '.join(DIARY_REGISTRY)}[/]")
        elif q.startswith("/note"):
            arg = q[5:].strip()
            if not arg:
                rows = journal.day(date.today())
                for r in rows:
                    console.print(f"  [dim]#{r['id']}[/] [#8fd0ff][{datetime.fromtimestamp(r['ts']):%H:%M}][/] {r['text']}")
                console.print(f"[dim]Записей сегодня: {len(rows)}. /note текст — добавить, /note del N — удалить. "
                              f"Можно писать и в файл: {journal.inbox}[/]")
            elif arg.split()[0] in ("del", "rm", "удалить"):
                n = arg.split()[1:] if len(arg.split()) > 1 else []
                ok = n and n[0].lstrip("#").isdigit() and journal.delete(int(n[0].lstrip("#")))
                console.print("[green]Запись удалена[/]" if ok else "[yellow]/note del N — номер из /note[/]")
            else:
                eid = journal.add(arg, via="cli")
                console.print(f"[green]✎ Записано в личный дневник (#{eid})[/]")
        elif q.startswith("/tts"):
            arg = q[4:].strip()
            if arg in ("off", "voice", "all"):
                speaker.set_mode(arg)
            elif arg == "stop":
                speaker.stop()
            elif arg.startswith("test"):
                speaker.say(arg[4:].strip() or "Привет! Я Скай. Так звучит мой голос.", force=True)
            modes = {"off": "молчу", "voice": "отвечаю голосом на «Скай, …» и озвучиваю напоминания",
                     "all": "озвучиваю каждый ответ"}
            console.print(f"Голос: [bold]{speaker.mode}[/] — {modes[speaker.mode]} · движок {speaker.engine_name}"
                          + (f" · [red]{speaker.error}[/]" if speaker.error else ""))
            if "piper" not in speaker.engine_name:
                console.print("[dim]Голос получше (Piper, ~60 МБ): .venv\\Scripts\\python.exe scripts\\get_tts_voice.py[/]")
        elif q == "/mini":
            open_mini()
            console.print("[#8fd0ff]Мини-окно открыто — справа внизу. Ctrl+Alt+Space — показать/спрятать.[/]")
        elif q.startswith("/summary"):
            try:
                d = parse_day(q[8:].strip())
            except ValueError:
                console.print("[yellow]/summary [сегодня|вчера|YYYY-MM-DD][/]")
            else:
                with console.status("[#8fd0ff]собираю сводку дня…[/]", spinner="dots"), gate.user():
                    try:
                        rec = episodes.day(d)
                    except Exception as e:  # noqa: BLE001
                        rec = None
                        console.print(f"[red]Сводка дня не получилась: {e}[/]")
                s0 = datetime.combine(d, datetime.min.time()).timestamp()
                body = []
                if rec:
                    body.append(rec["summary"])
                    for key, name in (("highlights", "Главное"), ("todos", "Осталось"), ("people", "Люди")):
                        if rec["meta"].get(key):
                            body.append(f"\n{name}:\n" + "\n".join(f"  • {x}" for x in rec["meta"][key]))
                eps = [x for x in episodes.list(s0, s0 + 86400) if x["ts"] >= s0]
                if eps:
                    body.append("\nЭпизоды:\n" + episode_lines(eps))
                console.print(Panel(Text("\n".join(body) or "За этот день в памяти ничего нет", style="#e8f6ff"),
                                    title=f"[bold #8fd0ff]📔 {d.isoformat()}[/]", border_style="#3a9cff"))
        elif q.startswith("/find"):
            text = q[5:].strip()
            if not text:
                console.print("[yellow]/find текст[/]")
            else:
                hits = memory.search(text, limit=10)
                for h in hits:
                    console.print(f"[#8fd0ff][{fmt_ts(h['ts'])}][/] {h['text']} "
                                  f"[dim]({'+'.join(h['by'])}, {h['score']})[/]")
                if not hits:
                    console.print("[dim]Ничего не найдено[/]")
        elif q.startswith("/forget"):
            n = q[7:].strip()
            if not n.isdigit():
                console.print("[yellow]/forget N — удалить всё, что записано за последние N минут[/]")
            else:
                console.print(f"[bold yellow]Удалить из памяти всё за последние {n} мин? Это нельзя "
                              "отменить. [y/n] (n):[/] ", end="")
                k, ans = inbox.get({"line", "eof"})
                if k == "line" and ans.strip().lower() in ("y", "yes", "д", "да"):
                    gone = memory.forget(time.time() - int(n) * 60, None)
                    console.print(f"[green]Удалено событий: {gone}[/]")
                else:
                    console.print("[dim]Ничего не удалено[/]")
        elif q == "/jobs":
            console.print(Text(agent.jobs_report(), style="#c792ea"))
        elif q.startswith("/stop"):
            console.print(Text(agent.stop_job(q[5:].strip() or 0), style="#c792ea"))
        elif q == "/model":
            # выбор модели читает stdin сам — временно забираем строку из очереди
            console.print("[dim]Введи номер модели и Enter[/]")
            try:
                models = list_models()
                for i, (name, gb, params) in enumerate(models, 1):
                    console.print(f"  [bold #3a9cff]{i}[/] {name} [#8fd0ff]{params}[/]")
                console.print("[bold #8fd0ff]Номер модели:[/] ", end="")
                _, ans = inbox.get({"line"})
                ans = ans.strip()
                if ans.isdigit() and 1 <= int(ans) <= len(models):
                    agent.set_model(models[int(ans) - 1][0])
                    console.print(f"[green]Модель: {agent.model}[/]")
                else:
                    console.print("[red]Модель не изменена[/]")
            except Exception as e:
                console.print(f"[red]Ошибка: {e}[/]")
        else:
            run_agent(q, via=via)
        prompt()

    bridge.stop()
    speaker.stop()
    agent.shutdown()
    for s in sensors.values():
        s.stop()
    episodes.stop()
    working.stop()
    diaries.stop()
    sleep.stop()
    observer.stop()
    if graph:
        graph.close()
    memory.close()
    console.print("\n[#8fd0ff]До встречи в облаках ☁[/]")
    os._exit(0)  # поток чтения ввода висит в input() — выходим сразу


if __name__ == "__main__":
    main()
