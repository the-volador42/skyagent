"""Эмбеддинги через Ollama (bge-m3). Фоновый поток досчитывает векторы для новых событий пачками."""
import os
import threading
import time

import requests


class Embedder:
    def __init__(self, timeline, url, model="bge-m3", gate=None, batch=32):
        self.timeline = timeline
        self.url = url.rstrip("/")
        self.model = model
        self.gate = gate
        self.batch = batch
        self.on_gpu = os.environ.get("SKYAGENT_EMBED_GPU", "0") == "1"
        self.error = None
        self.done = 0
        self._stop = threading.Event()
        self._thread = None

    def embed(self, texts):
        """Синхронно: список векторов. Используется и для запросов поиска."""
        body = {"model": self.model, "input": list(texts), "keep_alive": "2m"}   # выгружается сама без дела
        if not self.on_gpu:   # эмбеддинги на CPU: ~1.5 ГБ видеопамяти остаются агенту, а скорость им не важна
            body["options"] = {"num_gpu": 0}
        r = requests.post(f"{self.url}/api/embed", json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"Ollama {r.status_code}: {r.text[:200]}")
        return r.json()["embeddings"]

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sky-embed", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            rows = self.timeline.pending_embeddings(self.batch)
            if not rows:
                self._stop.wait(2)
                continue
            try:
                if self.gate:
                    with self.gate.background():
                        vecs = self.embed([t for _, t, _ in rows])
                else:
                    vecs = self.embed([t for _, t, _ in rows])
                self.timeline.store_embeddings([(i, ts, v) for (i, _, ts), v in zip(rows, vecs)])
                self.done += len(rows)
                self.error = None
            except Exception as e:  # noqa: BLE001 — Ollama может быть выключена; повторим позже
                self.error = f"{type(e).__name__}: {e}"
                self._stop.wait(15)
            time.sleep(0.05)
