"""Голос ассистента (TTS): локально, на процессоре — видеокарта остаётся модели.

Движки (SKYAGENT_TTS_ENGINE):
  piper — нейросетевой голос Piper (пакет piper-tts, onnxruntime на CPU); голос ~60 МБ, быстрее
          реального времени. Голос — папка models/vits-piper-ru_RU-*; скачать:
          .venv\\Scripts\\python.exe scripts\\get_tts_voice.py [irina|denis|dmitri|ruslan]
  sapi  — голос Windows (Microsoft Irina), ничего скачивать не надо; звучит роботизированно.
По умолчанию — piper, если голос скачан, иначе sapi.

Когда говорить (SKYAGENT_TTS, в CLI — /tts):
  off   — молчать;
  voice — отвечать голосом на голосовые команды «Скай, …» и озвучивать напоминания (по умолчанию);
  all   — озвучивать каждый ответ.
Пока ассистент говорит, микрофон не записывает его же голос в память (эхо) и не ловит «Скай» из динамика.

SKYAGENT_TTS_SPEED=1.0 — скорость; SKYAGENT_TTS_VOICE — номер голоса Piper (sid) или часть имени голоса SAPI.
"""
import os
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np

MODELS = Path(__file__).resolve().parents[1] / "models"
MODES = ("off", "voice", "all")
MAX_CHARS = 900                 # длинные ответы читаем до этой длины — остальное на экране
ECHO_TAIL = 0.6                 # после конца фразы ещё столько секунд считаем эхом


# ------------------------------------------------------------ текст → то, что читается вслух
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍]")


def speakable(text: str) -> str:
    """Убрать из ответа то, что вслух звучит плохо: код, разметку, ссылки, эмодзи."""
    t = text or ""
    t = re.sub(r"<think>.*?</think>", " ", t, flags=re.S)
    t = re.sub(r"```.*?(```|$)", " (код — на экране) ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", " ссылка ", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\|.*\|\s*$", " ", t, flags=re.M)          # таблицы не читаем
    t = re.sub(r"[*_~>|]+", " ", t)
    t = EMOJI.sub(" ", t)
    t = re.sub(r"\s*\n\s*", ". ", t.strip())
    t = re.sub(r"([:;,!?])\s*\.", r"\1", t)
    t = re.sub(r"\.(\s*\.)+", ".", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" .") + "."
    if len(t) > MAX_CHARS:
        cut = t.rfind(". ", 0, MAX_CHARS)
        t = t[:cut + 1 if cut > 200 else MAX_CHARS] + " Остальное — на экране."
    return "" if t == "." else t


def chunks(text: str, limit=220):
    """Разбить на куски по предложениям: первый кусок синтезируется быстро — голос начинается сразу."""
    parts = re.split(r"(?<=[.!?…;:])\s+", text)
    buf = ""
    for p in parts:
        if buf and len(buf) + len(p) > limit:
            yield buf
            buf = p
        else:
            buf = f"{buf} {p}".strip()
    if buf:
        yield buf


# ------------------------------------------------------------ движки
def find_piper():
    want = os.environ.get("SKYAGENT_TTS_MODEL")
    if want:
        return Path(want) if Path(want).exists() else None
    dirs = sorted(MODELS.glob("vits-piper-ru_RU-*")) if MODELS.exists() else []
    for d in dirs:
        if list(d.glob("*.onnx")) and (d / "tokens.txt").exists():
            return d
    return None


class PiperEngine:
    """Голос Piper. Основной путь — пакет piper-tts (свой espeak-ng для фонем, onnxruntime на CPU).
    Запасной — sherpa-onnx до 2.0: в 2.0 из неё убрали espeak, и голоса Piper без лексикона не читаются."""
    name = "piper"

    def __init__(self, folder: Path):
        onnx = next(p for p in folder.glob("*.onnx"))
        self.voice = folder.name.replace("vits-piper-", "")
        sid = os.environ.get("SKYAGENT_TTS_VOICE", "0")
        self.sid = int(sid) if sid.isdigit() else 0
        self.piper = self.tts = None
        try:
            from piper import PiperVoice
            cfg = onnx.with_name(onnx.name + ".json")
            self.piper = PiperVoice.load(onnx, config_path=cfg if cfg.exists() else None)
            return
        except ImportError:
            pass
        import sherpa_onnx
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(model=str(onnx), lexicon="", tokens=str(folder / "tokens.txt"),
                                                     data_dir=str(folder / "espeak-ng-data"))
        cfg = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
            vits=vits, num_threads=max(1, min(4, (os.cpu_count() or 4) // 4)), provider="cpu"), max_num_sentences=1)
        self.tts = sherpa_onnx.OfflineTts(cfg)

    def synth(self, text, speed):
        if self.piper is not None:
            from piper import SynthesisConfig
            cfg = SynthesisConfig(speaker_id=self.sid or None, length_scale=1.0 / max(0.3, speed))
            parts = [c.audio_float_array for c in self.piper.synthesize(text, syn_config=cfg)]
            rate = self.piper.config.sample_rate
            return (np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)), rate
        a = self.tts.generate(text, sid=self.sid, speed=speed)
        if not len(a.samples):
            raise RuntimeError("sherpa-onnx не смогла прочитать текст (в версии 2.0 нет espeak): "
                               "uv pip install piper-tts")
        return np.asarray(a.samples, dtype=np.float32), a.sample_rate


class SapiEngine:
    name = "sapi"
    RATE = 22050                # SAFT22kHz16BitMono = 22

    def __init__(self):
        import comtypes
        import comtypes.client as cc
        comtypes.CoInitialize()
        self.cc = cc
        self.v = cc.CreateObject("SAPI.SpVoice")
        want = (os.environ.get("SKYAGENT_TTS_VOICE") or "").lower()
        voices = list(self.v.GetVoices())
        pick = next((t for t in voices if want and want in t.GetDescription().lower()), None) \
            or next((t for t in voices if "russian" in t.GetDescription().lower()), None)
        if pick is not None:
            self.v.Voice = pick
        self.voice = self.v.Voice.GetDescription()

    def synth(self, text, speed):
        st = self.cc.CreateObject("SAPI.SpMemoryStream")
        fmt = self.cc.CreateObject("SAPI.SpAudioFormat")
        fmt.Type = 22
        st.Format = fmt
        self.v.AudioOutputStream = st
        self.v.Rate = int(max(-10, min(10, round((speed - 1.0) * 10))))
        self.v.Speak(text, 0)
        pcm = np.frombuffer(bytes(st.GetData()), dtype=np.int16)
        return pcm.astype(np.float32) / 32768.0, self.RATE


# ------------------------------------------------------------ диктор
class Speaker:
    """Очередь фраз и проигрывание в своём потоке. say() не ждёт; stop() обрывает речь и очередь.
    mic — MicSensor: на время речи ему отмечаются окна эха. on_state(speaking: bool, text) — для интерфейсов."""

    def __init__(self, mic=None, mode=None, on_state=None):
        self.mic = mic
        self.mode = (mode or os.environ.get("SKYAGENT_TTS", "voice")).strip().lower()
        if self.mode not in MODES:
            self.mode = "voice"
        self.speed = float(os.environ.get("SKYAGENT_TTS_SPEED", 1.0))
        self.on_state = on_state or (lambda speaking, text: None)
        self.engine = None
        self.error = None
        self.speaking = False
        self.stats = {"said": 0, "chars": 0}
        self._q = queue.Queue()
        self._gen = 0                   # поколение: stop() делает устаревшим всё, что уже в очереди
        self._stop_now = threading.Event()
        threading.Thread(target=self._loop, name="sky-tts", daemon=True).start()

    @property
    def engine_name(self):
        if self.engine:
            return f"{self.engine.name} ({self.engine.voice})"
        want = os.environ.get("SKYAGENT_TTS_ENGINE", "").lower()
        return "sapi" if want == "sapi" or not find_piper() else f"piper ({find_piper().name})"

    def wants(self, via):
        """Озвучивать ли ответ: via — 'voice' (команда голосом), 'notify' (напоминание) или 'text'."""
        return self.mode == "all" or (self.mode == "voice" and via in ("voice", "notify"))

    def say(self, text, force=False):
        if self.mode == "off" and not force:
            return False
        t = speakable(text)
        if not t:
            return False
        self._q.put((self._gen, t))
        return True

    def stop(self):
        self._gen += 1
        self._stop_now.set()
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:  # noqa: BLE001
            pass

    def set_mode(self, mode):
        if mode in MODES:
            self.mode = mode
            if mode == "off":
                self.stop()
        return self.mode

    # ------------------------------------------------------------ поток
    def _load(self):
        want = os.environ.get("SKYAGENT_TTS_ENGINE", "").lower()
        folder = find_piper()
        if want != "sapi" and folder:
            try:
                return PiperEngine(folder)
            except Exception as e:  # noqa: BLE001 — сломанный голос Piper: остаётся голос Windows
                self.error = f"piper: {type(e).__name__}: {e}"
        return SapiEngine()

    def _play(self, audio, rate):
        import sounddevice as sd
        dur = len(audio) / rate
        start = time.time()
        if self.mic is not None and hasattr(self.mic, "mark_echo"):
            self.mic.mark_echo(start, start + dur + ECHO_TAIL)
        sd.play(audio, rate)
        while time.time() - start < dur + 0.05:
            if self._stop_now.wait(0.05):
                sd.stop()
                if self.mic is not None and hasattr(self.mic, "mark_echo"):
                    self.mic.mark_echo(start, time.time() + ECHO_TAIL)
                return False
        sd.wait()
        return True

    def _loop(self):
        while True:
            gen, text = self._q.get()
            if gen != self._gen:
                if self.speaking and self._q.empty():
                    self.speaking = False
                    self.on_state(False, "")
                continue
            self._stop_now.clear()
            try:
                if self.engine is None:
                    self.engine = self._load()
                self.speaking = True
                self.on_state(True, text)
                for part in chunks(text):
                    if gen != self._gen:
                        break
                    audio, rate = self.engine.synth(part, self.speed)
                    if gen != self._gen or not len(audio):
                        break
                    if not self._play(audio, rate):
                        break
                self.stats["said"] += 1
                self.stats["chars"] += len(text)
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
            finally:
                if self._q.empty() or gen != self._gen:
                    self.speaking = False
                    self.on_state(False, text)
