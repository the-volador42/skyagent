"""Мост CLI ↔ мини-окно: маленький HTTP-сервер внутри процесса CLI (только 127.0.0.1).

CLI — главный процесс: в нём живут память, сенсоры и агент. Мини-окно (mini.py) — отдельный лёгкий
процесс, который говорит с ним через мост:
  GET  /events?after=N  — длинный опрос: события для окна (ответ по кускам, напоминания, подтверждения…)
  остальное             — маршруты, которые регистрирует CLI (bridge.route).
Адрес и одноразовый ключ лежат в data/bridge.json; без ключа (заголовок X-Sky-Token) сервер не отвечает —
чужие сайты в браузере до агента не дотянутся.
"""
import json
import os
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KEEP = 400          # сколько последних событий помнить (окно, открытое позже, догонит хвост)


class Bridge:
    def __init__(self, data_dir, port=None):
        self.file = Path(data_dir) / "bridge.json"
        self.token = secrets.token_urlsafe(24)
        self.routes = {}
        self.events = deque(maxlen=KEEP)
        self.seq = 0
        self.cond = threading.Condition()
        self.port = int(port if port is not None else os.environ.get("SKYAGENT_BRIDGE_PORT", 0))
        self.srv = None
        self.clients = {}       # последний опрос от окна: время — чтобы знать, открыто ли оно

    def route(self, method, path):
        def deco(fn):
            self.routes[(method, path)] = fn
            return fn
        return deco

    def emit(self, kind, data=None):
        with self.cond:
            self.seq += 1
            self.events.append({"seq": self.seq, "ts": time.time(), "kind": kind, "data": data})
            self.cond.notify_all()

    @property
    def gui_connected(self):
        return any(time.time() - t < 40 for t in self.clients.values())

    def poll(self, after, wait=25.0, client="gui"):
        self.clients[client] = time.time()
        end = time.time() + wait
        with self.cond:
            while True:
                out = [e for e in self.events if e["seq"] > after]
                if out:
                    return {"seq": self.seq, "events": out}
                left = end - time.time()
                if left <= 0:
                    return {"seq": self.seq, "events": []}
                self.cond.wait(left)

    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SkyBridge/1.0"

            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                if host not in ("127.0.0.1", "localhost") or not secrets.compare_digest(
                        self.headers.get("X-Sky-Token", ""), bridge.token):
                    return self._reply(403, {"error": "forbidden"})
                url = urlparse(self.path)
                q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                body = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    try:
                        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    except json.JSONDecodeError:
                        return self._reply(400, {"error": "bad json"})
                if method == "GET" and url.path == "/events":
                    try:
                        after = int(q.get("after", 0))
                    except ValueError:
                        after = 0
                    return self._reply(200, bridge.poll(after, min(float(q.get("wait", 25)), 30)))
                fn = bridge.routes.get((method, url.path))
                if not fn:
                    return self._reply(404, {"error": "no route"})
                try:
                    return self._reply(200, fn({**q, **body}) or {"ok": True})
                except Exception as e:  # noqa: BLE001 — ошибка маршрута не роняет сервер
                    return self._reply(500, {"error": f"{type(e).__name__}: {e}"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, name="sky-bridge", daemon=True).start()
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"port": self.port, "token": self.token, "pid": os.getpid(),
                                         "started": time.time()}), encoding="utf-8")
        return self

    def stop(self):
        self.emit("bye")
        try:
            if self.file.exists() and json.loads(self.file.read_text(encoding="utf-8")).get("pid") == os.getpid():
                self.file.unlink()
        except Exception:  # noqa: BLE001
            pass
        if self.srv:
            threading.Thread(target=self.srv.shutdown, daemon=True).start()
