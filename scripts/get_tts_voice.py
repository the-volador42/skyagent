"""Скачать русский голос Piper для SkyAgent:  .venv\Scripts\python.exe scripts\get_tts_voice.py [голос]

Голоса (≈60–75 МБ, официальные сборки sherpa-onnx на GitHub):
  irina  — женский (по умолчанию)      denis  — мужской
  dmitri — мужской                     ruslan — мужской
Распаковывается в models/vits-piper-ru_RU-<голос>-medium. Если голосов несколько, SkyAgent берёт
первый по алфавиту; конкретный — SKYAGENT_TTS_MODEL=путь к папке.
"""
import sys
import tarfile
import urllib.request
from pathlib import Path

VOICES = ("irina", "denis", "dmitri", "ruslan")
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
MODELS = Path(__file__).resolve().parents[1] / "models"


def main():
    voice = (sys.argv[1] if len(sys.argv) > 1 else "irina").lower()
    if voice not in VOICES:
        print(f"Нет голоса «{voice}». Есть: {', '.join(VOICES)}")
        return 1
    name = f"vits-piper-ru_RU-{voice}-medium"
    target = MODELS / name
    if target.exists() and list(target.glob("*.onnx")):
        print(f"Уже есть: {target}")
        return 0
    MODELS.mkdir(exist_ok=True)
    arc = MODELS / f"{name}.tar.bz2"
    url = BASE + arc.name
    print(f"Скачиваю {url}")

    def hook(n, size, total):
        if total > 0:
            print(f"\r  {min(100, n * size * 100 // total)}%  ({total / 1e6:.0f} МБ)", end="", flush=True)

    urllib.request.urlretrieve(url, arc, hook)
    print("\nРаспаковываю…")
    with tarfile.open(arc, "r:bz2") as tar:
        tar.extractall(MODELS, filter="data")       # filter=data: без путей за пределы папки
    arc.unlink()
    print(f"Готово: {target}. Проверить: /tts test в SkyAgent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
