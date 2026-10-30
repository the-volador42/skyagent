"""Один раз войти в Telegram для SkyAgent:  .venv\Scripts\python.exe scripts\telegram_login.py

Нужны TG_API_ID и TG_API_HASH в .env (my.telegram.org → API development tools → создать приложение).
Telegram пришлёт код в приложение; если включена двухэтапная проверка — спросит облачный пароль.
Всё вводится здесь, в твоём терминале: SkyAgent код и пароль не видит и не хранит — только файл сессии
data/telegram/skyagent.session (это ключ к аккаунту: никому не отдавай, в git он не попадает).
Выйти: python scripts/telegram_login.py --logout
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from skyagent.memory import DEFAULT_DATA  # noqa: E402
from skyagent.sensors.telegram import credentials, session_path  # noqa: E402


def main():
    api_id, api_hash = credentials()
    if not api_id:
        print("Нет TG_API_ID / TG_API_HASH в .env. Получи их на https://my.telegram.org → API development tools.")
        return 1
    from telethon.sync import TelegramClient
    import os
    data = os.environ.get("SKYAGENT_DATA") or DEFAULT_DATA
    with TelegramClient(session_path(data), api_id, api_hash, device_model="SkyAgent", app_version="skyagent") as client:
        if "--logout" in sys.argv:
            client.log_out()
            print("Сессия SkyAgent закрыта.")
            return 0
        client.start()
        me = client.get_me()
        print(f"Готово: вход выполнен как {me.first_name or ''} (@{me.username or '—'}). Перезапусти SkyAgent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
