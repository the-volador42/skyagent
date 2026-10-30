from pathlib import Path

from dotenv import load_dotenv

# Настройки (адрес Ollama, пароль Neo4j, модели) — из .env в корне репозитория.
# Переменные окружения, заданные явно, важнее файла.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from .core import Agent, list_models, pick_vision_model, OLLAMA_URL  # noqa: E402,F401
