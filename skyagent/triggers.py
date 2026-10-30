"""Триггеры: условие на событие из шины → действие, сразу и без модели (уровень L0).

Наблюдатель решает «нужно ли что-то сделать» редко и обдуманно; триггер — мгновенная реакция
на однозначный сигнал. Новый триггер — подкласс Trigger с match() и fire().

Первый триггер — фраза-триггер «Скай, <команда>»:
  - ищется нечётко (распознавание путает: «скаи», «скаю», «sky», «скай агент») и только в НАЧАЛЕ фразы;
  - «Скай.» без команды — «слушаю»: следующая фраза в течение LISTEN_SEC считается командой;
  - исполняется только голосом пользователя (метка «я» по голосовому отпечатку). Чужой голос и звук
    из видео — никогда. Пока образец голоса не записан, команда требует подтверждения.
"""
import os
import re
import threading
import time

from .memory.events import Event

WAKE = re.compile(
    r"^\s*(?:(?:эй|ну|так|слушай|окей|ok)[\s,.!]+)?"
    r"(?:скай\s*агент|скайагент|скай|скаи|скайю|скаю|sky\s*agent|sky)"   # «скайп» — нет: «Скайп не работает»
    r"(?![а-яёa-z])[\s,.!?:—-]*(?P<cmd>.*)$", re.I | re.S)
LISTEN_SEC = 8.0
# Команды — строже, чем метка «я» в памяти: без подтверждения только при уверенном совпадении голоса
TRIGGER_ME = float(os.environ.get("SKYAGENT_TRIGGER_ME", 0.70))


def match_wake(text):
    """Команда после слова-активатора ('' — активатор без команды) или None, если активатора нет."""
    m = WAKE.match(text or "")
    if not m:
        return None
    return m.group("cmd").strip(" .,!?…")


class Trigger:
    name = "trigger"

    def match(self, ev):
        """Вернуть данные срабатывания или None."""
        raise NotImplementedError

    def fire(self, ev, data):
        raise NotImplementedError


class WakePhrase(Trigger):
    """«Скай, открой хром» → команда агенту от имени пользователя."""
    name = "wake_phrase"

    def __init__(self, on_command, voice=None, video_at=None):
        self.on_command = on_command          # on_command(text, need_confirm, info)
        self.voice = voice
        self.video_at = video_at              # функция ts → открыто ли видео (звук из него — не команда)
        self.listen_until = 0.0
        self.stats = {"fired": 0, "ignored": 0}
        self.last = None

    def match(self, ev):
        if ev.source != "mic" or ev.kind != "speech":
            return None
        cmd = match_wake(ev.text)
        if cmd is None:
            if time.time() < self.listen_until:   # после «Скай.» — следующая фраза и есть команда
                cmd = ev.text.strip(" .,!?…")
                self.listen_until = 0.0
            else:
                return None
        return {"cmd": cmd}

    def _decide(self, ev):
        """Можно ли исполнять: (ok, нужно ли подтверждение, причина)."""
        if self.video_at and self.video_at(ev.ts):
            return False, False, "звук из видео"
        spk = ev.meta.get("speaker")
        if self.voice is not None and self.voice.enrolled:
            score = ev.meta.get("speaker_score") or 0
            if spk == "me" and score >= TRIGGER_ME:
                return True, False, f"голос пользователя ({score:.2f})"
            if spk == "me":
                return True, True, f"голос похож, но не уверенно ({score:.2f})"
            if spk == "unsure":
                return True, True, "голос не распознан уверенно"
            return False, False, "чужой голос"
        return True, True, "образец голоса не записан (/enroll)"

    def fire(self, ev, data):
        ok, confirm, why = self._decide(ev)
        self.last = {"ts": ev.ts, "text": ev.text, "cmd": data["cmd"], "ok": ok, "why": why}
        if not ok:
            self.stats["ignored"] += 1
            return {"status": "ignored", "why": why}
        if not data["cmd"]:
            self.listen_until = time.time() + LISTEN_SEC
            self.on_command("", False, {"listening": True, "why": why})
            return {"status": "listening", "why": why}
        self.stats["fired"] += 1
        self.on_command(data["cmd"], confirm, {"why": why, "score": ev.meta.get("speaker_score")})
        return {"status": "fired", "why": why}


class TriggerHub:
    """Подписка на шину и прогон триггеров. Срабатывания пишутся в хронологию (trigger/fired)."""

    def __init__(self, memory):
        self.memory = memory
        self.triggers = []
        self._lock = threading.Lock()
        memory.bus.subscribe(self._on_event)

    def add(self, trigger):
        self.triggers.append(trigger)
        return trigger

    def _on_event(self, ev):
        if ev.source == "trigger":
            return
        for t in list(self.triggers):
            try:
                data = t.match(ev)
                if data is None:
                    continue
                with self._lock:
                    res = t.fire(ev, data)
                self.memory.publish(Event("trigger", "fired", f"{t.name}: {res.get('status')} — {ev.text[:160]}",
                                          ts=ev.ts, trust="system",
                                          meta={"trigger": t.name, "event_id": ev.id, **(res or {}), **data}))
            except Exception as e:  # noqa: BLE001 — сломанный триггер не должен ронять шину
                self.memory.publish(Event("trigger", "error", f"{t.name}: {type(e).__name__}: {e}", trust="system"))
