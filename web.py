"""SkyAgent Web — небесный интерфейс для Ollama.  Запуск:  python web.py  [--port 7860] [--no-browser]

Режимы:
  chat  — обычный чат с моделью Ollama (стриминг, размышления, картинки)
  agent — SkyAgent из пакета skyagent (инструменты, подтверждения опасных действий)

Сервер слушает только 127.0.0.1: агент умеет выполнять команды на компьютере.
"""
import argparse
import json
import mimetypes
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

from skyagent import Agent, OLLAMA_URL, pick_vision_model
from skyagent.tools import VISION as TOOLS_VISION

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = {"srv": None}

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


# ---------------------------------------------------------------- агентские сессии

class AgentSession:
    """Один агент на один разговор в интерфейсе."""

    def __init__(self, model, history=None):
        self.agent = Agent(model)
        self.busy = False
        self.lock = threading.Lock()
        self.pending = {}  # id подтверждения -> [Event, решение]
        for m in history or []:  # восстановить контекст после перезапуска сервера
            if m.get("role") in ("user", "assistant") and m.get("content"):
                self.agent.messages.append({"role": m["role"], "content": m["content"]})

    def resolve(self, cid, allow):
        p = self.pending.get(cid)
        if p:
            p[1] = bool(allow)
            p[0].set()

    def cancel_all(self):
        for p in list(self.pending.values()):
            p[1] = False
            p[0].set()


SESSIONS: dict[str, AgentSession] = {}
SESSIONS_LOCK = threading.Lock()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "SkyAgentWeb/1.0"

    def log_message(self, fmt, *args):  # тише в консоли
        if "/api/" in (str(args[0]) if args else ""):  # при ошибках args[0] — HTTPStatus, не строка
            return
        super().log_message(fmt, *args)

    # --- защита: только локальный хост (от DNS-rebinding и чужих сайтов)
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _emit(self, obj):
        """Отправить одну строку NDJSON. False — клиент отключился."""
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return False

    # --- GET
    def do_GET(self):
        if not self._host_ok():
            return self.send_error(403)
        path = urlparse(self.path).path
        if path == "/api/models":
            return self._models()
        if path == "/api/health":
            try:
                v = requests.get(f"{OLLAMA_URL}/api/version", timeout=3).json().get("version")
                return self._json({"ok": True, "ollama": v, "url": OLLAMA_URL})
            except Exception as e:
                return self._json({"ok": False, "error": str(e), "url": OLLAMA_URL})
        self._static(path)

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        full = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            return self.send_error(404)
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _models(self):
        try:
            r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            r.raise_for_status()
        except Exception as e:
            return self._json({"error": f"Ollama недоступна ({OLLAMA_URL}): {e}"}, 502)
        out = []
        for m in r.json().get("models", []):
            d = m.get("details", {})
            out.append({"name": m["name"], "size": m.get("size", 0),
                        "params": d.get("parameter_size", ""), "family": d.get("family", ""),
                        "quant": d.get("quantization_level", ""),
                        "caps": m.get("capabilities") or []})
        out.sort(key=lambda x: x["name"])
        self._json({"models": out})

    # --- POST
    def do_POST(self):
        if not self._host_ok():
            return self.send_error(403)
        # JSON-only: браузер не даст чужому сайту отправить такой запрос без CORS-preflight
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_error(415)
        path = urlparse(self.path).path
        try:
            body = self._body()
        except json.JSONDecodeError:
            return self.send_error(400)
        routes = {"/api/chat": self._chat, "/api/agent/run": self._agent_run,
                  "/api/agent/confirm": self._agent_confirm, "/api/agent/stop": self._agent_stop,
                  "/api/agent/reset": self._agent_reset, "/api/open-cli": self._open_cli,
                  "/api/vision/default": lambda b: self._json({"model": pick_vision_model()})}
        fn = routes.get(path)
        if not fn:
            return self.send_error(404)
        fn(body)

    # ------------------------------------------------ простой чат
    def _chat(self, b):
        payload = {"model": b["model"], "messages": b.get("messages", []), "stream": True,
                   "options": {k: v for k, v in (b.get("options") or {}).items() if v is not None}}
        if b.get("think") is not None:
            payload["think"] = bool(b["think"])
        self._stream_start()
        t0 = time.time()
        try:
            r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=600)
        except Exception as e:
            self._emit({"type": "error", "data": f"Ollama недоступна: {e}"})
            return
        if r.status_code != 200:
            msg = r.text[:400]
            if r.status_code == 400 and "does not support thinking" in msg:
                payload.pop("think", None)
                r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=600)
            if r.status_code != 200:
                self._emit({"type": "error", "data": f"Ollama {r.status_code}: {r.text[:400]}"})
                return
        try:
            for line in r.iter_lines():
                if not line:
                    continue
                c = json.loads(line)
                if c.get("error"):
                    self._emit({"type": "error", "data": c["error"]})
                    break
                m = c.get("message", {})
                ok = True
                if m.get("thinking"):
                    ok = self._emit({"type": "think", "data": m["thinking"]})
                if m.get("content"):
                    ok = ok and self._emit({"type": "token", "data": m["content"]})
                if not ok:
                    break  # клиент нажал «стоп» или закрыл вкладку
                if c.get("done"):
                    ev = c.get("eval_count") or 0
                    dur = (c.get("eval_duration") or 0) / 1e9
                    self._emit({"type": "stats", "data": {
                        "tokens": ev, "tps": round(ev / dur, 1) if dur else None,
                        "seconds": round(time.time() - t0, 1)}})
        finally:
            r.close()

    # ------------------------------------------------ агент
    def _session(self, b):
        sid, model = b.get("session"), b.get("model")
        with SESSIONS_LOCK:
            s = SESSIONS.get(sid)
            if s is None:
                s = SESSIONS[sid] = AgentSession(model, b.get("history"))
            elif model and s.agent.model != model:
                s.agent.set_model(model)
        return s

    def _agent_run(self, b):
        s = self._session(b)
        with s.lock:
            if s.busy:
                return self._json({"error": "Агент уже работает в этом разговоре"}, 409)
            s.busy = True
        agent = s.agent
        agent.auto_approve = bool(b.get("auto"))
        TOOLS_VISION["model"] = b.get("visionModel") or None
        q = queue.Queue()

        def on_event(kind, data):
            if kind == "job_start":
                kind, data = "info", f"⏳ задача #{data['id']} ({data['name']}) продолжает работать в фоне"
            elif kind == "job_done":
                # итог агент сообщит в следующем ответе (результат ждёт в очереди агента)
                kind, data = "info", f"✔ фоновая задача #{data['id']} ({data['name']}) завершена за {data['seconds']:.0f} c"
            q.put({"type": kind, "data": data})

        def confirm(name, args):
            cid = uuid.uuid4().hex[:10]
            ev = threading.Event()
            s.pending[cid] = [ev, False]
            q.put({"type": "confirm", "data": {"id": cid, "name": name, "args": args}})
            while not ev.wait(0.25):
                if agent.stop_flag:
                    break
            allowed = s.pending.pop(cid, [None, False])[1]
            q.put({"type": "confirmed", "data": {"id": cid, "allow": allowed}})
            return allowed

        agent.on_event, agent.confirm = on_event, confirm

        def work():
            try:
                agent.run(b.get("text", ""))
            except Exception as e:
                q.put({"type": "error", "data": f"{type(e).__name__}: {e}"})
            finally:
                q.put({"type": "end"})

        self._stream_start()
        t0 = time.time()
        threading.Thread(target=work, daemon=True).start()
        try:
            while True:
                try:
                    item = q.get(timeout=10)
                except queue.Empty:
                    item = {"type": "ping"}
                if item["type"] == "end":
                    self._emit({"type": "end", "data": {"seconds": round(time.time() - t0, 1)}})
                    break
                if not self._emit(item):  # вкладку закрыли — останавливаем агента
                    agent.stop_flag = True
                    s.cancel_all()
                    break
        finally:
            s.busy = False

    def _agent_confirm(self, b):
        s = SESSIONS.get(b.get("session"))
        if s:
            s.resolve(b.get("id"), b.get("allow"))
        self._json({"ok": bool(s)})

    def _agent_stop(self, b):
        s = SESSIONS.get(b.get("session"))
        if s:
            s.agent.stop_flag = True
            s.cancel_all()
        self._json({"ok": True})

    def _open_cli(self, b):
        """Открыть CLI в новом окне консоли и выключить веб-интерфейс."""
        args = [sys.executable, os.path.join(HERE, "cli.py")]
        if b.get("model"):
            args += ["--model", b["model"]]
        if b.get("auto"):
            args.append("--yes")
        subprocess.Popen(args, cwd=HERE, creationflags=0x00000010 if os.name == "nt" else 0)
        self._json({"ok": True})
        for s in list(SESSIONS.values()):
            s.agent.stop_flag = True
            s.cancel_all()
        threading.Timer(0.4, SERVER["srv"].shutdown).start()

    def _agent_reset(self, b):
        with SESSIONS_LOCK:
            s = SESSIONS.pop(b.get("session"), None)
        if s:
            s.agent.stop_flag = True
            s.cancel_all()
        self._json({"ok": True})


def main():
    ap = argparse.ArgumentParser(description="SkyAgent Web")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):  # консоль Windows в cp1251 падает на ☁
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    SERVER["srv"] = srv
    url = f"http://127.0.0.1:{a.port}"
    print(f"\n  ☁  SkyAgent Web  →  {url}\n     Ollama: {OLLAMA_URL}\n     Ctrl+C — выход\n")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  До встречи в облаках ☁")


if __name__ == "__main__":
    main()
