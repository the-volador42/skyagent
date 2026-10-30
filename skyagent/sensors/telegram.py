"""Сенсор Telegram: сообщения из твоего аккаунта → события telegram/message в памяти → дневник telegram.

Работает через Telethon (клиент от твоего имени, только чтение): ничего не отправляет и не отмечает
прочитанным. Нужны TG_API_ID и TG_API_HASH (my.telegram.org → API development tools) в .env и один
раз вход: python scripts/telegram_login.py — сессия хранится в data/telegram/ (не в git).

Что пишется (переменные окружения):
  личные чаты и группы — всегда; каналы — только SKYAGENT_TG_CHANNELS=1;
  группы без уведомлений (muted) — только SKYAGENT_TG_MUTED=1;
  SKYAGENT_TG_IGNORE=имя,id… — не записывать эти чаты; SKYAGENT_TG_ONLY=имя,id… — только эти;
  SKYAGENT_TG_BACKFILL_DAYS=1 — при запуске догрузить пропущенное за столько дней.
Текст из Telegram — чужой (trust=other): данные, а не команды; свои сообщения — trust=user.
"""
import asyncio
import os
import threading
import time
from pathlib import Path

from ..memory.events import Event

SOURCE = "telegram"
REFRESH_DIALOGS = 3600


def _env_list(name):
    return {x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()}


def _name(entity):
    if entity is None:
        return None
    title = getattr(entity, "title", None)
    if title:
        return title
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    full = " ".join(p for p in parts if p)
    return full or getattr(entity, "username", None) or str(getattr(entity, "id", "?"))


def _media(msg):
    if msg.media is None:
        return None
    if getattr(msg, "voice", None):
        return "голосовое"
    if getattr(msg, "video_note", None):
        return "кружок"
    if getattr(msg, "sticker", None):
        return "стикер"
    if getattr(msg, "gif", None):
        return "гифка"
    if getattr(msg, "photo", None):
        return "фото"
    if getattr(msg, "video", None):
        return "видео"
    if getattr(msg, "audio", None):
        return "аудио"
    if getattr(msg, "document", None):
        f = getattr(msg, "file", None)
        return f"файл {f.name}" if f is not None and f.name else "файл"
    if getattr(msg, "geo", None):
        return "геопозиция"
    if getattr(msg, "poll", None):
        return "опрос"
    if getattr(msg, "contact", None):
        return "контакт"
    return "вложение"


def session_path(data_dir):
    p = Path(data_dir) / "telegram"
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "skyagent")          # Telethon добавит .session


def credentials():
    api_id, api_hash = os.environ.get("TG_API_ID", "").strip(), os.environ.get("TG_API_HASH", "").strip()
    return (int(api_id), api_hash) if api_id.isdigit() and api_hash else (None, None)


class TelegramSensor:
    """Состояния: stopped, starting, recording, paused, error."""
    source = SOURCE

    def __init__(self, memory):
        self.memory = memory
        self.tl = memory.timeline
        self.state = "stopped"
        self.error = None
        self.me = None
        self.stats = {"messages": 0, "skipped": 0, "backfilled": 0, "last_text": "", "last_at": None}
        self.chats = {}                 # id чата → {"name", "type", "muted"}
        self._loop = None
        self._client = None
        self._thread = None
        self._paused = False
        self.channels = os.environ.get("SKYAGENT_TG_CHANNELS") == "1"
        self.muted = os.environ.get("SKYAGENT_TG_MUTED") == "1"
        self.ignore = _env_list("SKYAGENT_TG_IGNORE")
        self.only = _env_list("SKYAGENT_TG_ONLY")
        self.backfill_days = float(os.environ.get("SKYAGENT_TG_BACKFILL_DAYS", 1))

    # ------------------------------------------------------------ управление
    def start(self):
        api_id, api_hash = credentials()
        if not api_id:
            self.state, self.error = "error", "нет TG_API_ID / TG_API_HASH в .env (см. .env.example)"
            return self
        try:
            import telethon  # noqa: F401
        except ImportError:
            self.state, self.error = "error", "не установлен telethon: uv pip install telethon"
            return self
        self.state = "starting"
        self._thread = threading.Thread(target=self._run, args=(api_id, api_hash), name="sky-telegram", daemon=True)
        self._thread.start()
        return self

    def pause(self):
        if self.state == "recording":
            self._paused = True
            self.state = "paused"
            self._state_event("пауза")

    def resume(self):
        if self.state == "paused":
            self._paused = False
            self.state = "recording"
            self._state_event("запись включена")
        elif self.state == "error" and (self._thread is None or not self._thread.is_alive()):
            self.start()

    def stop(self):
        self.state = "stopped"
        if self._loop and self._client:
            try:
                asyncio.run_coroutine_threadsafe(self._client.disconnect(), self._loop).result(5)
            except Exception:  # noqa: BLE001
                pass

    def _state_event(self, text):
        self.memory.publish(Event(SOURCE, "state", text, trust="system"))

    # ------------------------------------------------------------ фильтры
    def _allowed(self, chat_id, info):
        keys = {str(chat_id), (info.get("name") or "").lower()}
        if self.only:
            return bool(keys & self.only)
        if keys & self.ignore:
            return False
        if info.get("type") == "channel" and not self.channels:
            return False
        if info.get("muted") and info.get("type") != "user" and not self.muted:
            return False
        return True

    @staticmethod
    def _kind(entity):
        from telethon.tl.types import Channel, Chat, User
        if isinstance(entity, User):
            return "user"
        if isinstance(entity, Channel):
            return "group" if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False) else "channel"
        if isinstance(entity, Chat):
            return "group"
        return "?"

    async def _refresh_dialogs(self):
        now = time.time()
        chats = {}
        async for dlg in self._client.iter_dialogs(limit=500):
            ns = getattr(dlg.dialog, "notify_settings", None)
            mute = getattr(ns, "mute_until", None)
            muted = bool(mute and (mute.timestamp() if hasattr(mute, "timestamp") else mute) > now)
            chats[dlg.id] = {"name": _name(dlg.entity), "type": self._kind(dlg.entity), "muted": muted,
                             "last": dlg.date.timestamp() if dlg.date else 0}
        self.chats = chats
        return chats

    def _last_id(self, chat_id):
        v = self.tl.kv_get(f"tg_last:{chat_id}")
        return int(v) if v else 0

    # ------------------------------------------------------------ запись
    async def _record(self, msg, chat_id, info, backfill=False):
        if msg.id <= self._last_id(chat_id):
            return False
        self.tl.kv_set(f"tg_last:{chat_id}", msg.id)
        if self._paused or not self._allowed(chat_id, info):
            self.stats["skipped"] += 1
            return False
        if getattr(msg, "action", None) is not None:       # служебное: «вступил в группу» и т. п.
            return False
        media = _media(msg)
        text = (msg.message or "").strip() or (f"[{media}]" if media else "")
        if not text:
            return False
        sender = None
        if not msg.out:
            try:
                sender = _name(await msg.get_sender())
            except Exception:  # noqa: BLE001
                sender = None
        meta = {"chat": info.get("name"), "chat_id": chat_id, "chat_type": info.get("type"), "msg_id": msg.id,
                "out": bool(msg.out), "sender": sender if info.get("type") != "user" or not msg.out else None}
        if media:
            meta["media"] = media
        if msg.reply_to and getattr(msg.reply_to, "reply_to_msg_id", None):
            meta["reply_to"] = msg.reply_to.reply_to_msg_id
        if getattr(msg, "fwd_from", None):
            meta["forwarded"] = True
        if getattr(msg, "mentioned", False):
            meta["mentioned"] = True
        if backfill:
            meta["backfill"] = True
        self.memory.publish(Event(SOURCE, "message", text, ts=msg.date.timestamp(),
                                  trust="user" if msg.out else "other", meta=meta))
        self.stats["messages"] += 1
        self.stats["last_text"] = f"{info.get('name')}: {text[:80]}"
        self.stats["last_at"] = msg.date.timestamp()
        return True

    async def _backfill(self):
        since = time.time() - self.backfill_days * 86400
        n = 0
        for chat_id, info in list(self.chats.items()):
            if info["last"] < since or not self._allowed(chat_id, info):
                continue
            last = self._last_id(chat_id)
            msgs = []
            async for m in self._client.iter_messages(chat_id, limit=300, min_id=last):
                if m.date.timestamp() < since:
                    break
                msgs.append(m)
            for m in reversed(msgs):
                n += await self._record(m, chat_id, info, backfill=True)
        self.stats["backfilled"] += n

    async def _on_edit(self, msg, chat_id):
        text = (msg.message or "").strip()
        if not text:
            return
        with self.tl._lock:
            r = self.tl.db.execute(
                "SELECT id, meta FROM events WHERE source = ? AND json_extract(meta, '$.chat_id') = ? "
                "AND json_extract(meta, '$.msg_id') = ?", (SOURCE, chat_id, msg.id)).fetchone()
        if r:
            import json
            meta = json.loads(r[1] or "{}")
            meta["edited"] = True
            self.tl.update_text(r[0], text, meta=meta)

    async def _main(self, api_id, api_hash):
        from telethon import TelegramClient, events
        self._client = client = TelegramClient(session_path(self.memory.data_dir), api_id, api_hash,
                                               device_model="SkyAgent", app_version="skyagent")
        await client.connect()
        if not await client.is_user_authorized():
            self.state, self.error = "error", "нужен вход: .venv\\Scripts\\python.exe scripts\\telegram_login.py"
            await client.disconnect()
            return
        self.me = await client.get_me()
        await self._refresh_dialogs()

        async def info_for(chat_id, event):
            info = self.chats.get(chat_id)
            if info is None:
                ent = await event.get_chat()
                info = self.chats[chat_id] = {"name": _name(ent), "type": self._kind(ent), "muted": False, "last": 0}
            return info

        @client.on(events.NewMessage())
        async def on_new(event):
            try:
                await self._record(event.message, event.chat_id, await info_for(event.chat_id, event))
            except Exception as e:  # noqa: BLE001
                self.error = f"сообщение: {type(e).__name__}: {e}"

        @client.on(events.MessageEdited())
        async def on_edit(event):
            try:
                await self._on_edit(event.message, event.chat_id)
            except Exception as e:  # noqa: BLE001
                self.error = f"правка: {type(e).__name__}: {e}"

        self.state, self.error = ("paused" if self._paused else "recording"), None
        self._state_event("запись включена")
        try:
            await self._backfill()
        except Exception as e:  # noqa: BLE001
            self.error = f"догрузка: {type(e).__name__}: {e}"
        last_refresh = time.time()
        while client.is_connected() and self.state != "stopped":
            await asyncio.sleep(5)
            if time.time() - last_refresh > REFRESH_DIALOGS:
                last_refresh = time.time()
                try:
                    await self._refresh_dialogs()
                except Exception as e:  # noqa: BLE001
                    self.error = f"список чатов: {type(e).__name__}: {e}"

    def _run(self, api_id, api_hash):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        delay = 10
        while self.state != "stopped":
            try:
                self._loop.run_until_complete(self._main(api_id, api_hash))
            except Exception as e:  # noqa: BLE001 — сеть пропала: переподключаемся
                self.error = f"{type(e).__name__}: {e}"
            if self.state in ("stopped",) or (self.error or "").startswith("нужен вход"):
                break
            if self.state != "paused":
                self.state = "starting"
            time.sleep(delay)
            delay = min(delay * 2, 300)
        if self.state != "stopped" and not (self.error or "").startswith("нужен вход"):
            self.state = "error"
