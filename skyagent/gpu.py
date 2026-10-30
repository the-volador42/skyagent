"""Диспетчер видеокарты.

1. GpuGate — запрос пользователя важнее фоновой работы. Пока пользователь ждёт ответа, фоновые задачи
   (распознавание речи, сводки, наблюдатель) не начинаются, а уже идущие запросы к модели прерываются
   (см. memory/llm.py) — иначе Ollama обслуживает их по очереди и ответ пользователю ждёт.

2. Vram — следит, чтобы модели помещались в видеопамять. Если модель не влезает, Windows молча
   переносит часть её весов в обычную ОЗУ («shared GPU memory»), и генерация падает до ~1 токена/с.
   Ollama сама этого не видит (для gemma4 она считает размер неверно), поэтому:
   - перед запросом к незагруженной модели выгружаем другие большие модели, если места не хватает;
   - если места всё равно мало — грузим модель частично (num_gpu слоёв на видеокарте, остальное на CPU:
     медленнее, но в разы быстрее, чем веса в shared memory);
   - фоновый сторож раз в 15 с смотрит, не вылезла ли загруженная модель в shared memory
     (например, после неё загрузилось что-то ещё), и тогда перезагружает её с меньшим num_gpu.

3. В памяти постоянно только РАБОЧАЯ модель (vram.primary, keep_alive=-1). Модель зрения, тяжёлая
   модель сна, эмбеддинги грузятся по требованию и выгружаются сами через keep_alive (KEEP_OTHER).
"""
import ctypes
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager

import requests

# Окно контекста. У gemma4 5 из 6 слоёв — со скользящим окном 1024, поэтому длинный контекст почти
# не стоит видеопамяти (32K ≈ +0.3 ГБ к 8K). Одно значение для всех вызовов: иначе Ollama перезагружает модель.
NUM_CTX = int(os.environ.get("SKYAGENT_CTX", "32768"))
# Сколько держать в памяти модели, кроме рабочей: рабочая (агента) — всегда, остальные выгружаются сами
KEEP_PRIMARY = -1
KEEP_OTHER = os.environ.get("SKYAGENT_KEEP_OTHER", "1m")
KEEP_EMBED = "2m"
MARGIN_MB = 450            # запас видеопамяти: экран, браузер, рост буферов
SPILL_MB = 600             # столько shared memory у модели — уже «вылезла»
EMBED_HINTS = ("bge", "embed", "minilm", "nomic", "e5-", "gte-")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class Preempted(RuntimeError):
    """Фоновый запрос к модели прерван, потому что пользователь ждёт ответа."""


class GpuGate:
    def __init__(self):
        self._cond = threading.Condition()
        self._local = threading.local()
        self.user_active = 0
        self.background_active = 0

    @contextmanager
    def user(self):
        with self._cond:
            self.user_active += 1
        try:
            yield
        finally:
            with self._cond:
                self.user_active -= 1
                self._cond.notify_all()

    @contextmanager
    def background(self):
        with self._cond:
            while self.user_active:
                self._cond.wait(1.0)
            self.background_active += 1
        self._local.bg = getattr(self._local, "bg", 0) + 1
        try:
            yield
        finally:
            self._local.bg -= 1
            with self._cond:
                self.background_active -= 1

    def in_background(self):
        """Текущий поток выполняет фоновую задачу (внутри gate.background())."""
        return getattr(self._local, "bg", 0) > 0


gate = GpuGate()


def is_embed(model):
    return any(h in (model or "").lower() for h in EMBED_HINTS)


# ------------------------------------------------------------------ сколько видеопамяти у процессов (Windows)
class _Val(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("largeValue", ctypes.c_longlong)]


class _Item(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _Val)]


def _pdh_by_pid(path):
    """Счётчик «GPU Process Memory» по процессам: {pid: байты}. Пусто, если недоступно."""
    if sys.platform != "win32":
        return {}
    pdh = ctypes.WinDLL("pdh")
    q, c = ctypes.c_void_p(), ctypes.c_void_p()
    if pdh.PdhOpenQueryW(None, None, ctypes.byref(q)):
        return {}
    try:
        if pdh.PdhAddEnglishCounterW(q, path, None, ctypes.byref(c)) or pdh.PdhCollectQueryData(q):
            return {}
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        pdh.PdhGetFormattedCounterArrayW(c, 0x400, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            return {}
        buf = (ctypes.c_byte * size.value)()
        if pdh.PdhGetFormattedCounterArrayW(c, 0x400, ctypes.byref(size), ctypes.byref(count), buf):
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(_Item))
        out = {}
        for i in range(count.value):
            m = re.match(r"pid_(\d+)_", items[i].szName or "")
            if m:
                pid = int(m.group(1))
                out[pid] = out.get(pid, 0) + max(0, items[i].FmtValue.largeValue)
        return out
    finally:
        pdh.PdhCloseQuery(q)


def process_vram():
    """{pid: (выделенная МБ, shared МБ)} для процессов, занимающих видеокарту."""
    ded = _pdh_by_pid(r"\GPU Process Memory(*)\Dedicated Usage")
    sh = _pdh_by_pid(r"\GPU Process Memory(*)\Shared Usage")
    return {pid: (ded.get(pid, 0) >> 20, sh.get(pid, 0) >> 20) for pid in set(ded) | set(sh)}


def gpu_free_total():
    """(свободно МБ, всего МБ) по nvidia-smi или None."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW).stdout
        free, total = (int(x) for x in out.strip().splitlines()[0].split(","))
        return free, total
    except Exception:  # noqa: BLE001 — нет NVIDIA или драйвера
        return None


def _runner_blobs():
    """{pid: sha256-блоб модели} для запущенных llama-server/ollama runner."""
    return {pid: blob for pid, (blob, _) in _runners().items()}


def _runners():
    """{pid: (sha256-блоб модели, -ngl или None)} для запущенных llama-server/ollama runner."""
    if sys.platform != "win32":
        return {}
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('llama-server.exe','ollama.exe') } | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=15, creationflags=_NO_WINDOW).stdout
    except Exception:  # noqa: BLE001
        return {}
    res = {}
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        m = re.search(r"--model\s+\"?[^\s\"]*?(sha256-[0-9a-f]{64})", cmd)
        ngl = re.search(r"(?:-ngl|--n-gpu-layers)\s+(\d+)", cmd)
        if pid.strip().isdigit() and m:
            res[int(pid)] = (m.group(1), int(ngl.group(1)) if ngl else None)
    return res


def _models_dir():
    return os.environ.get("OLLAMA_MODELS") or os.path.join(os.path.expanduser("~"), ".ollama", "models")


def _model_blob(name):
    """sha256-блоб весов модели по её манифесту Ollama."""
    base, _, tag = name.partition(":")
    parts = base.split("/")
    if len(parts) == 1:
        parts = ["registry.ollama.ai", "library"] + parts
    elif len(parts) == 2:
        parts = ["registry.ollama.ai"] + parts
    path = os.path.join(_models_dir(), "manifests", *parts, tag or "latest")
    try:
        with open(path, encoding="utf-8") as f:
            for layer in json.load(f).get("layers", []):
                if layer.get("mediaType") == "application/vnd.ollama.image.model":
                    return layer["digest"].replace(":", "-")
    except (OSError, ValueError, KeyError):
        return None
    return None


# ------------------------------------------------------------------ менеджер видеопамяти
class Vram:
    def __init__(self, url=None, gate=None):
        url = url or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        self.url = (url if url.startswith("http") else "http://" + url).rstrip("/")
        self.gate = gate
        self._lock = threading.RLock()
        self.caps = {}          # модель → num_gpu (сколько слоёв держать на видеокарте); нет — все
        self.footprint = {}     # модель → сколько МБ она реально заняла целиком (выделенная + shared)
        self.log = deque(maxlen=20)
        self.last = {}          # последний замер сторожа: модель → (выделенная, shared)
        self._layers, self._sizes, self._blobs, self._caps = {}, {}, {}, {}
        self.primary = None     # рабочая модель агента — единственная, что держится в памяти постоянно
        self._adopted, self._adopted_ok = {}, set()   # num_gpu уже загруженной модели сверен с её процессом
        self._thread = None
        self._stop = threading.Event()

    # ---------------------------------------------------------------- сведения о моделях
    def _note(self, text):
        self.log.append((time.time(), text))

    def layers(self, model):
        """Сколько слоёв у модели (+1 — выходной слой: так считает num_gpu)."""
        if model not in self._layers:
            n = None
            try:
                info = requests.post(f"{self.url}/api/show", json={"model": model}, timeout=10).json()
                n = next((v for k, v in (info.get("model_info") or {}).items()
                          if k.endswith(".block_count") and isinstance(v, int)), None)
            except Exception:  # noqa: BLE001
                pass
            self._layers[model] = n + 1 if n else None
        return self._layers[model]

    def capabilities(self, model):
        if model not in self._caps:
            try:
                info = requests.post(f"{self.url}/api/show", json={"model": model}, timeout=10).json()
                self._caps[model] = info.get("capabilities") or []
            except Exception:  # noqa: BLE001
                return []
        return self._caps[model]

    def keep_alive(self, model):
        """Рабочая модель — навсегда; эмбеддинги — пару минут; остальные — пока идёт работа."""
        if model == self.primary:
            return KEEP_PRIMARY
        return KEEP_EMBED if is_embed(model) else KEEP_OTHER

    def set_primary(self, model):
        """Сменить рабочую модель; прежняя больше не «вечная» — выгружаем её."""
        old, self.primary = self.primary, model
        if old and old != model:
            threading.Thread(target=self.unload, args=(old,), daemon=True).start()

    def file_mb(self, model):
        if model not in self._sizes:
            try:
                tags = requests.get(f"{self.url}/api/tags", timeout=5).json().get("models", [])
                for m in tags:
                    self._sizes[m["name"]] = m.get("size", 0) >> 20
            except Exception:  # noqa: BLE001
                pass
        return self._sizes.get(model, 0)

    def need_mb(self, model):
        """Сколько видеопамяти нужно модели целиком: замер, если был, иначе оценка по размеру файла."""
        if model in self.footprint:
            return self.footprint[model]
        size = self.file_mb(model)
        return int(size * 1.25 + 1000 + NUM_CTX / 1024 * 12) if size else 0

    def loaded(self):
        try:
            return [m["name"] for m in requests.get(f"{self.url}/api/ps", timeout=5).json().get("models", [])]
        except Exception:  # noqa: BLE001
            return []

    def options(self, model, **extra):
        """options для запроса к Ollama: одинаковые у всех вызовов одной модели, иначе Ollama её перезагружает."""
        opts = {"num_ctx": NUM_CTX}
        if model in self.caps:
            opts["num_gpu"] = self.caps[model]
        opts.update(extra)
        return opts

    def unload(self, model):
        self._adopted_ok.discard(model)
        try:
            requests.post(f"{self.url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
        except requests.RequestException:
            pass

    # ---------------------------------------------------------------- перед запросом
    def prepare(self, model):
        """Вызывать перед запросом к модели. Если модели нет в видеопамяти — освободить место под неё
        (выгрузить другие большие модели), а если места всё равно не хватит — ограничить num_gpu."""
        if not model or is_embed(model):
            return
        with self._lock:
            loaded = self.loaded()
            if model in loaded:
                self._adopt(model)
                return
            free = gpu_free_total()
            if not free:
                return
            need = self.need_mb(model)
            others = [m for m in loaded if not is_embed(m)]
            if others and free[0] < need + MARGIN_MB:
                for m in others:
                    self.unload(m)
                for _ in range(40):          # выгрузка занимает до пары секунд
                    if not set(others) & set(self.loaded()):
                        break
                    time.sleep(0.25)
                time.sleep(0.3)
                self._note(f"выгружена {', '.join(others)}, чтобы поместилась {model}")
                free = gpu_free_total() or free
            layers = self.layers(model)
            if not need or not layers:
                return
            if free[0] >= need + MARGIN_MB:
                if self.caps.pop(model, None) is not None:
                    self._note(f"{model}: места хватает — снова целиком на видеокарте")
            else:
                cap = max(0, int(layers * (free[0] - MARGIN_MB) / need))
                if self.caps.get(model) != cap:
                    self.caps[model] = cap
                    self._note(f"{model}: свободно {free[0]} МБ из нужных ~{need} — на видеокарте {cap} "
                               f"из {layers} слоёв, остальное на CPU")

    def _adopt(self, model):
        """Модель уже загружена (может, другим процессом SkyAgent или веб-интерфейсом) — берём её num_gpu.
        Иначе каждый процесс просит свой num_gpu и Ollama перезагружает модель на каждом запросе:
        ответы идут минутами, а запрос, попавший на перезагрузку, может выдать мусор (<unused…>)."""
        blob = _model_blob(model)
        if not blob:
            return
        if self._adopted.get(model) == blob and model in self._adopted_ok:
            return
        runners = [ngl for b, ngl in _runners().values() if b == blob]
        if not runners:
            return
        ngl = runners[0]
        layers = self.layers(model)
        if ngl is None or (layers and ngl >= layers):
            self.caps.pop(model, None)
        else:
            self.caps[model] = ngl
        self._adopted[model] = blob
        self._adopted_ok.add(model)

    # ---------------------------------------------------------------- сторож
    def start(self):
        if sys.platform != "win32" or not gpu_free_total():
            return self
        if not (self._thread and self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="sky-vram", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(15):
            try:
                self.check()
            except Exception as e:  # noqa: BLE001 — сторож не должен падать
                self._note(f"ошибка сторожа: {type(e).__name__}: {e}")

    def usage(self):
        """{модель: (выделенная МБ, shared МБ)} для загруженных в Ollama моделей."""
        per_pid = {pid: v for pid, v in process_vram().items() if v[0] + v[1] > 200}
        if set(per_pid) - set(self._blobs):
            self._blobs = _runner_blobs()
        by_blob = {}
        for pid, blob in self._blobs.items():
            if pid in per_pid:
                d, s = per_pid[pid]
                od, os_ = by_blob.get(blob, (0, 0))
                by_blob[blob] = (od + d, os_ + s)
        out = {}
        for m in self.loaded():
            blob = _model_blob(m)
            if blob in by_blob:
                out[m] = by_blob[blob]
        return out

    def check(self):
        """Найти модели, вылезшие в shared memory, и перезагрузить их с меньшим num_gpu."""
        with self._lock:
            usage = self.usage()
            self.last = usage
            free = gpu_free_total()
            for model, (ded, sh) in usage.items():
                if model not in self.caps:
                    self.footprint[model] = max(self.footprint.get(model, 0), ded + sh)
                if is_embed(model) or sh < SPILL_MB or not free or free[0] > MARGIN_MB:
                    continue
                layers = self.layers(model)
                if not layers:
                    continue
                cur = self.caps.get(model, layers)
                per_layer = max(1, self.need_mb(model) / layers)
                new = max(0, cur - math.ceil((sh + MARGIN_MB) / per_layer))
                self.caps[model] = new
                self._adopted_ok.discard(model)
                self._note(f"{model} вылезла из видеопамяти ({sh} МБ в общей ОЗУ, ~1 токен/с) — "
                           f"теперь {new} из {layers} слоёв на видеокарте")
                if not (self.gate and self.gate.user_active):
                    self.reload(model)

    def reload(self, model):
        """Перезагрузить модель с текущими options (num_gpu), пока пользователь ничего не ждёт."""
        try:
            requests.post(f"{self.url}/api/generate", timeout=300,
                          json={"model": model, "options": self.options(model), "keep_alive": self.keep_alive(model)})
        except requests.RequestException:
            pass

    def report(self):
        """Строки для /status."""
        lines = []
        for m, (d, s) in self.last.items():
            cap = f", слоёв на GPU: {self.caps[m]}/{self.layers(m)}" if m in self.caps else ""
            warn = "  ⚠ вылезла в общую ОЗУ" if s >= SPILL_MB and not is_embed(m) else ""
            lines.append(f"{m}: {d} МБ видеопамяти, {s} МБ общей{cap}{warn}")
        for ts, text in list(self.log)[-5:]:
            lines.append(f"[{time.strftime('%H:%M', time.localtime(ts))}] {text}")
        return lines


vram = Vram(gate=gate)
