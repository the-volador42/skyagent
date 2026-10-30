"""Голос «я / другие»: голосовой отпечаток пользователя и метка у каждой фразы.

Модель — WeSpeaker ResNet34 (ONNX через sherpa-onnx, процессор, ~20 мс на фразу), отпечаток —
вектор 256 чисел. Образец записывается командой /enroll: 25–60 секунд речи пользователя,
отпечаток — среднее по фразам. Каждая новая фраза сравнивается с ним по косинусу:
  ≥ ME       — «я»;  < OTHER — «другой»;  между или фраза короче MIN_SEC — «не уверен».
Отпечаток — биометрия: хранится только локально (data/voice/), /enroll reset удаляет его.
"""
import json
import os
import threading
import time
from pathlib import Path

import numpy as np

SR = 16000
MIN_SEC = 1.2                 # по более короткому звуку отпечаток ненадёжен
ENROLL_SEC = 25.0             # столько речи нужно для образца
ENROLL_TIMEOUT = 120.0
ME = float(os.environ.get("SKYAGENT_SPEAKER_ME", 0.55))
OTHER = float(os.environ.get("SKYAGENT_SPEAKER_OTHER", 0.40))
MODEL = Path(__file__).resolve().parents[2] / "models" / "wespeaker_en_voxceleb_resnet34_LM.onnx"
LABELS = {"me": "я", "other": "другой", "unsure": "?"}


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else v


class VoicePrint:
    def __init__(self, data_dir, model=None):
        import sherpa_onnx
        path = Path(model or os.environ.get("SKYAGENT_SPEAKER_MODEL", MODEL))
        if not path.exists():
            raise FileNotFoundError(f"нет модели голосовых отпечатков: {path}")
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=2)
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.dir = Path(data_dir) / "voice"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.me = None
        self.info = {}
        self._lock = threading.Lock()
        self.load()

    # ------------------------------------------------------------ отпечаток
    def embed(self, audio):
        with self._lock:
            s = self.extractor.create_stream()
            s.accept_waveform(SR, np.asarray(audio, dtype=np.float32))
            s.input_finished()
            return _norm(self.extractor.compute(s))

    def load(self):
        p = self.dir / "me.npy"
        if p.exists():
            self.me = _norm(np.load(p))
            try:
                self.info = json.loads((self.dir / "me.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.info = {}

    @property
    def enrolled(self):
        return self.me is not None

    def save(self, embs, seconds):
        mean = _norm(np.mean(embs, axis=0))
        self_sim = [float(e @ mean) for e in embs]
        np.save(self.dir / "me.npy", mean)
        self.info = {"created": time.time(), "phrases": len(embs), "seconds": round(seconds, 1),
                     "self_sim_min": round(min(self_sim), 3), "self_sim_mean": round(float(np.mean(self_sim)), 3)}
        (self.dir / "me.json").write_text(json.dumps(self.info, ensure_ascii=False, indent=1), encoding="utf-8")
        self.me = mean
        return self.info

    def reset(self):
        for name in ("me.npy", "me.json"):
            try:
                (self.dir / name).unlink()
            except OSError:
                pass
        self.me, self.info = None, {}

    # ------------------------------------------------------------ метка фразы
    def identify(self, audio):
        """('me' | 'other' | 'unsure' | None, сходство). None — образец не записан."""
        if self.me is None:
            return None, None
        if len(audio) < MIN_SEC * SR:
            return "unsure", None
        score = float(self.embed(audio) @ self.me)
        label = "me" if score >= ME else "other" if score < OTHER else "unsure"
        return label, round(score, 3)


class Enroller:
    """Запись образца голоса из живого микрофона: копит фразы, пока не наберётся ENROLL_SEC речи."""

    def __init__(self, voice, mic, on_progress=None, on_done=None):
        self.voice, self.mic = voice, mic
        self.on_progress, self.on_done = on_progress, on_done
        self.embs, self.seconds = [], 0.0
        self.started = time.time()
        self.active = True
        mic.segment_hooks.append(self._on_segment)
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _on_segment(self, seg):
        if not self.active:
            return
        dur = len(seg.audio) / SR
        if dur < 1.5:
            return
        self.embs.append(self.voice.embed(seg.audio))
        self.seconds += dur
        if self.on_progress:
            self.on_progress(self.seconds, ENROLL_SEC)
        if self.seconds >= ENROLL_SEC:
            self._finish(ok=True)

    def _watchdog(self):
        while self.active and time.time() - self.started < ENROLL_TIMEOUT:
            time.sleep(1)
        if self.active:
            self._finish(ok=len(self.embs) >= 4)

    def _finish(self, ok):
        self.active = False
        try:
            self.mic.segment_hooks.remove(self._on_segment)
        except ValueError:
            pass
        info = self.voice.save(self.embs, self.seconds) if ok and self.embs else None
        if self.on_done:
            self.on_done(info)
