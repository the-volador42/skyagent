"""Сенсор микрофона: запись → VAD (Silero) → фразы → распознавание → события speech в памяти.

Уровни «от дешёвого к дорогому»:
  L0  VAD на каждые 32 мс звука (CPU, доли миллисекунды) — тишина дальше не идёт;
      фразы короче MIN_SPEECH отбрасываются, длинные режутся по MAX_SEGMENT.
  —   распознавание только фраз с речью. По умолчанию Vosk на процессоре (SKYAGENT_STT=vosk):
      видеокарта целиком остаётся рабочей модели агента. SKYAGENT_STT=whisper — Whisper на видеокарте
      (точнее и с пунктуацией, но занимает ~1 ГБ видеопамяти).
  L0  после распознавания: фильтр «пустых» фраз (у Whisper — ещё и типичных галлюцинаций).

Сырое аудио не сохраняется — только текст. Пауза закрывает поток микрофона полностью.
"""
import glob
import json
import os
import queue
import re
import site
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from ..memory.events import Event

SR = 16000
FRAME = 512                       # 32 мс — окно Silero VAD при 16 кГц
FPS = SR / FRAME

START_PROB = 0.5                  # с этой вероятности речи фраза начинается
END_PROB = 0.35                   # ниже — считаем тишиной
MIN_SILENCE = 0.7                 # столько тишины — конец фразы, с
PRE_ROLL = 0.3                    # захват звука до начала речи, с
KEEP_TAIL = 0.2                   # тишины в конце фразы оставляем, с
MIN_SPEECH = 0.4                  # короче — шум, а не речь, с
MAX_SEGMENT = 25.0                # длиннее — режем (Whisper лучше на коротком)

# Whisper на тишине и шуме «досочиняет» титры из обучающих видео
HALLUCINATIONS = (
    "продолжение следует", "субтитры сделал", "субтитры создавал", "субтитры подогнал", "dimatorzok",
    "редактор субтитров", "корректор", "спасибо за просмотр", "подписывайтесь на канал", "ставьте лайк",
    "игорь негода", "amara.org", "thanks for watching", "thank you for watching",
)


def is_hallucination(text: str) -> bool:
    t = text.lower().strip()
    if not re.search(r"\w", t):
        return True
    for h in HALLUCINATIONS:
        if h in t and len(t) < len(h) + 25:
            return True
    words = re.findall(r"\w+", t)
    return len(words) >= 6 and len(set(words)) <= 2      # «да да да да да да»


def _add_cuda_dlls():
    """ctranslate2 ищет cuBLAS/cuDNN в PATH — добавляем папки из pip-пакетов nvidia-*."""
    bases = set(site.getsitepackages()) | {os.path.join(sys.prefix, "Lib", "site-packages")}
    for base in bases:
        for d in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            if d not in os.environ.get("PATH", ""):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


class StreamingVAD:
    """Silero VAD (ONNX из faster-whisper) по кадрам с сохранением состояния между вызовами."""
    CONTEXT = 64

    def __init__(self):
        import onnxruntime
        from faster_whisper.utils import get_assets_path
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = onnxruntime.InferenceSession(
            os.path.join(get_assets_path(), "silero_vad_v6.onnx"),
            providers=["CPUExecutionProvider"], sess_options=opts)
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(self.CONTEXT, dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self.ctx, frame]).astype(np.float32)[None, :]
        out, self.h, self.c = self.session.run(None, {"input": x, "h": self.h, "c": self.c})
        self.ctx = frame[-self.CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


@dataclass
class Segment:
    audio: np.ndarray
    start: float      # unix-время начала
    end: float


class Segmenter:
    """Режет поток кадров на фразы по VAD. feed() вызывается на каждый кадр 512 сэмплов."""

    def __init__(self, vad, on_segment):
        self.vad = vad
        self.on_segment = on_segment
        self.pre = deque(maxlen=int(PRE_ROLL * FPS))
        self.buf, self.start = [], None
        self.silence = self.speech = 0
        self.prob = 0.0
        self.level = 0.0

    @property
    def active(self):
        return self.start is not None

    def feed(self, frame, ts):
        p = self.vad(frame)
        self.prob = p
        self.level = float(np.sqrt(np.mean(frame ** 2)))
        if not self.active:
            self.pre.append((frame, ts))
            if p >= START_PROB:
                self.buf = [f for f, _ in self.pre]
                self.start = self.pre[0][1]
                self.pre.clear()
                self.silence, self.speech = 0, 1
            return
        self.buf.append(frame)
        if p >= END_PROB:
            self.silence = 0
            self.speech += 1
        else:
            self.silence += 1
        if self.silence >= MIN_SILENCE * FPS or len(self.buf) >= MAX_SEGMENT * FPS:
            self.flush()

    def flush(self):
        if not self.active:
            return
        cut = max(0, self.silence - int(KEEP_TAIL * FPS))
        frames = self.buf[:len(self.buf) - cut] if cut else self.buf
        if self.speech / FPS >= MIN_SPEECH and frames:
            audio = np.concatenate(frames)
            self.on_segment(Segment(audio, self.start, self.start + len(audio) / SR))
        self.buf, self.start = [], None
        self.silence = self.speech = 0


class Rechunker:
    """Приводит блоки любой длины и частоты к кадрам 16 кГц × 512 сэмплов."""

    def __init__(self, rate):
        self.rate = rate
        self.buf = np.zeros(0, dtype=np.float32)
        self.t0 = None       # время первого сэмпла в buf

    def push(self, block, ts):
        if self.rate != SR:
            n = int(round(len(block) * SR / self.rate))
            block = np.interp(np.linspace(0, len(block) - 1, n), np.arange(len(block)), block).astype(np.float32)
        if self.t0 is None or not len(self.buf):
            self.t0 = ts
        self.buf = np.concatenate([self.buf, block])
        while len(self.buf) >= FRAME:
            frame, self.buf = self.buf[:FRAME], self.buf[FRAME:]
            yield frame, self.t0
            self.t0 += FRAME / SR


MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models")


def find_vosk_model():
    """Папка модели Vosk: SKYAGENT_VOSK_MODEL или самая большая vosk-model-* в models/ (большая — точнее)."""
    path = os.environ.get("SKYAGENT_VOSK_MODEL")
    if path:
        return path
    found = [d for d in glob.glob(os.path.join(MODELS_DIR, "vosk-model-*")) if os.path.isdir(d)]
    lang = os.environ.get("SKYAGENT_LANG", "ru")
    found = [d for d in found if f"-{lang}" in os.path.basename(d)] or found
    if not found:
        return None
    size = lambda d: sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(d) for f in fs)  # noqa: E731
    return max(found, key=size)


class MicSensor:
    """Сенсор микрофона. Состояния: stopped, starting, recording, paused, error."""
    source = "mic"

    def __init__(self, publish, gate=None, model=None, compute_type=None, language=None, device=None, voice=None):
        self.publish = publish
        self.gate = gate
        self.voice = voice                # VoicePrint — метка «я / другой» у фраз (если образец записан)
        self.segment_hooks = []           # подписчики на нарезанные фразы (запись образца голоса)
        self.engine = os.environ.get("SKYAGENT_STT", "vosk").lower()
        self.model_name = model or (os.environ.get("SKYAGENT_WHISPER", "large-v3-turbo") if self.engine == "whisper"
                                    else os.path.basename(find_vosk_model() or "vosk (модель не найдена)"))
        # int8_float16: ~0.9 ГБ видеопамяти вместо ~1.9 у float16 при почти том же качестве и скорости —
        # так рядом помещается LLM на 12B (см. замер в docs/ARCHITECTURE.md)
        self.compute_type = compute_type or os.environ.get("SKYAGENT_WHISPER_COMPUTE", "int8_float16")
        lang = language or os.environ.get("SKYAGENT_LANG", "ru")
        self.language = None if lang == "auto" else lang
        self.device = device
        self.state = "stopped"
        self.error = None
        self.whisper_device = None
        self.stats = {"phrases": 0, "dropped": 0, "echo": 0, "last_text": "", "last_at": None, "transcribe_sec": 0.0}
        # когда говорит сам ассистент (tts.py): его голос из динамиков — не речь пользователя
        self.echo = []                    # [(начало, конец)] за последние минуты
        self.echo_filter = os.environ.get("SKYAGENT_TTS_ECHO", "1") != "0"
        self._frames = queue.Queue(maxsize=2000)
        self._segments = queue.Queue()
        self._stream = None
        self._running = threading.Event()
        self._model = None
        self._model_ready = threading.Event()
        self.segmenter = None

    # ------------------------------------------------------------ управление
    def start(self):
        self.state = "starting"
        self._running.set()
        self.segmenter = Segmenter(StreamingVAD(), self._on_segment)
        threading.Thread(target=self._load_model, name="sky-whisper-load", daemon=True).start()
        threading.Thread(target=self._segment_loop, name="sky-mic-vad", daemon=True).start()
        threading.Thread(target=self._transcribe_loop, name="sky-mic-stt", daemon=True).start()
        self._open()

    def pause(self):
        if self.state != "recording":
            return
        self._close()
        self.state = "paused"
        self._state_event("пауза")

    def resume(self):
        if self.state in ("paused", "error"):
            self._open()

    def stop(self):
        was = self.state
        self._close()
        self._running.clear()
        self.state = "stopped"
        if was == "recording":
            self._state_event("запись выключена")

    def mark_echo(self, start, end):
        now = time.time()
        self.echo = [(s, e) for s, e in self.echo if e > now - 300] + [(start, end)]

    def is_echo(self, start, end):
        """Фраза пришлась на речь ассистента больше чем на треть — это его же голос из динамиков."""
        if not self.echo_filter or end <= start:
            return False
        overlap = sum(max(0.0, min(end, e) - max(start, s)) for s, e in self.echo)
        return overlap / (end - start) > 0.33

    @property
    def backlog(self):
        return self._segments.qsize()

    @property
    def model_ready(self):
        return self._model_ready.is_set()

    # ------------------------------------------------------------ поток микрофона
    def _open(self):
        import sounddevice as sd
        try:
            info = sd.query_devices(self.device, "input")
            try:
                rate = SR
                stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=FRAME,
                                        device=self.device, callback=self._callback)
            except Exception:  # noqa: BLE001 — устройство не умеет 16 кГц: пишем как есть и пересчитываем
                rate = int(info["default_samplerate"])
                stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                        device=self.device, callback=self._callback)
            self._rechunker = Rechunker(rate)
            stream.start()
            self._stream = stream
            self.device_name = info["name"]
            self.state = "recording"
            self.error = None
            self._state_event("запись включена")
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = f"{type(e).__name__}: {e}"

    def _close(self):
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        self._frames.put(None)   # сигнал сегментатору: дописать начатую фразу

    def _callback(self, indata, frames, time_info, status):
        try:
            self._frames.put_nowait((indata[:, 0].copy(), time.time() - frames / self._rechunker.rate))
        except queue.Full:
            pass

    def _state_event(self, text):
        self.publish(Event(self.source, "state", text, trust="system",
                           meta={"device": getattr(self, "device_name", None)}))

    # ------------------------------------------------------------ L0: VAD и нарезка
    def _on_segment(self, seg):
        self._segments.put(seg)
        for hook in list(self.segment_hooks):
            try:
                hook(seg)
            except Exception as e:  # noqa: BLE001
                self.error = f"обработчик фраз: {type(e).__name__}: {e}"

    def _segment_loop(self):
        while self._running.is_set():
            try:
                item = self._frames.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self.segmenter.flush()
                self.segmenter.vad.reset()
                continue
            block, ts = item
            for frame, fts in self._rechunker.push(block, ts):
                self.segmenter.feed(frame, fts)

    # ------------------------------------------------------------ распознавание
    def _load_model(self):
        if self.engine != "whisper":
            return self._load_vosk()
        _add_cuda_dlls()
        from faster_whisper import WhisperModel
        try:
            self._model = WhisperModel(self.model_name, device="cuda", compute_type=self.compute_type)
            self.whisper_device = "cuda"
        except Exception as e:  # noqa: BLE001 — нет CUDA: работаем на процессоре, медленнее
            self.error = f"Whisper на видеокарте не запустился ({e}); работаю на CPU"
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            self.whisper_device = "cpu"
        self._model_ready.set()

    def _load_vosk(self):
        path = find_vosk_model()
        if not path:      # модели нет (ещё не скачана) — распознаём Whisper'ом, чтобы речь не терялась
            self.error = (f"нет модели Vosk в {MODELS_DIR} — пока работает Whisper. Скачай с "
                          "alphacephei.com/vosk/models (например vosk-model-ru-0.42) и распакуй туда")
            self.engine = "whisper"
            self.model_name = os.environ.get("SKYAGENT_WHISPER", "large-v3-turbo")
            err = self.error
            self._load_model()
            self.error = self.error or err
            return
        try:
            from vosk import Model, SetLogLevel
            SetLogLevel(-1)
            self._model = Model(path)
            self.whisper_device = "cpu"
        except Exception as e:  # noqa: BLE001
            self.error = f"Vosk не загрузился: {type(e).__name__}: {e}"
            self._model = None
        self._model_ready.set()

    def _transcribe_vosk(self, audio):
        from vosk import KaldiRecognizer
        if self._model is None:
            raise RuntimeError(self.error or "модель Vosk не загружена")
        t = time.time()
        rec = KaldiRecognizer(self._model, SR)
        rec.SetWords(True)
        rec.AcceptWaveform((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        res = json.loads(rec.FinalResult())
        took = time.time() - t
        text = (res.get("text") or "").strip()
        words = res.get("result") or []
        if not text:
            return None, "пусто"
        conf = sum(w.get("conf", 1.0) for w in words) / len(words) if words else 1.0
        # на шуме Vosk выдаёт одно-два коротких слова с низкой уверенностью
        if conf < 0.55 and len(words) <= 2:
            return None, "не речь"
        if len(text) > 1:
            text = text[0].upper() + text[1:]
        return text, {"lang": self.language or "auto", "conf": round(conf, 3), "stt_sec": round(took, 2),
                      "model": self.model_name, "engine": "vosk"}

    def transcribe(self, audio: np.ndarray):
        """Распознать фразу. Возвращает (текст, метаданные) или (None, причина отбраковки)."""
        self._model_ready.wait()
        if self.engine != "whisper":
            return self._transcribe_vosk(audio)
        t = time.time()
        parts, info = self._model.transcribe(audio, language=self.language, beam_size=5, vad_filter=False,
                                             condition_on_previous_text=False, without_timestamps=True)
        parts = list(parts)
        took = time.time() - t
        text = " ".join(p.text.strip() for p in parts).strip()
        if not text:
            return None, "пусто"
        no_speech = max(p.no_speech_prob for p in parts)
        logprob = sum(p.avg_logprob for p in parts) / len(parts)
        meta = {"lang": info.language, "no_speech": round(no_speech, 3), "logprob": round(logprob, 3),
                "stt_sec": round(took, 2), "model": self.model_name}
        if is_hallucination(text):
            return None, "галлюцинация"
        if no_speech > 0.6 and logprob < -0.8:
            return None, "не речь"
        return text, meta

    def _transcribe_loop(self):
        while self._running.is_set():
            try:
                seg = self._segments.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.is_echo(seg.start, seg.end):
                self.stats["echo"] += 1
                continue
            try:
                if self.gate and self.engine == "whisper":
                    with self.gate.background():
                        text, meta = self.transcribe(seg.audio)
                else:
                    text, meta = self.transcribe(seg.audio)
            except Exception as e:  # noqa: BLE001
                self.error = f"распознавание: {type(e).__name__}: {e}"
                continue
            if text is None:
                self.stats["dropped"] += 1
                continue
            meta["audio_sec"] = round(seg.end - seg.start, 2)
            if self.voice is not None:
                try:
                    label, score = self.voice.identify(seg.audio)
                except Exception as e:  # noqa: BLE001
                    label, score = None, None
                    self.error = f"голосовой отпечаток: {type(e).__name__}: {e}"
                if label:
                    meta["speaker"], meta["speaker_score"] = label, score
            self.stats["transcribe_sec"] += meta["stt_sec"]
            self.publish(Event(self.source, "speech", text, ts=seg.start, ts_end=seg.end, trust="heard", meta=meta))
            self.stats["phrases"] += 1
            self.stats["last_text"] = text
            self.stats["last_at"] = seg.start
