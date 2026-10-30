"""Вызов локальной модели с ответом строго по JSON-схеме (сводки, наблюдатель)."""
import http.client
import json
import socket
import threading
from urllib.parse import urlparse

from ..gpu import Preempted, gate, vram


def _stream_chat(url, payload, timeout, cancel):
    """POST /api/chat со stream=true; возвращает (статус, [чанки] или текст ошибки).
    cancel() → True обрывает соединение (Ollama при этом сразу прекращает генерацию) и бросает Preempted."""
    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    stop, cancelled = threading.Event(), threading.Event()

    def watch():
        while not stop.wait(0.3):
            if cancel():
                cancelled.set()
                try:
                    if conn.sock:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    threading.Thread(target=watch, daemon=True).start()
    try:
        conn.request("POST", "/api/chat", json.dumps(payload).encode(), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            return resp.status, resp.read().decode("utf-8", "replace")
        chunks = []
        for line in resp:
            if cancelled.is_set():
                break
            if line.strip():
                chunks.append(json.loads(line))
        if cancelled.is_set():
            raise Preempted("пользователь ждёт ответа — фоновый запрос прерван")
        return 200, chunks
    except (OSError, http.client.HTTPException, ValueError):
        if cancelled.is_set():
            raise Preempted("пользователь ждёт ответа — фоновый запрос прерван") from None
        raise
    finally:
        stop.set()
        conn.close()


def chat_json(url, model, system, user, schema, num_predict=1500, temperature=0.2, timeout=300):
    """think=false и предел длины — предохранители: модели с размышлениями (gemma4) без них
    размышляют до бесконечности, а структурированный вывод иногда зацикливается на пробелах.
    Вызов внутри gate.background() прерывается (Preempted), как только пользователь отправил запрос."""
    bg = gate.in_background()
    cancel = (lambda: gate.user_active > 0) if bg else (lambda: False)
    if bg and gate.user_active:
        raise Preempted("пользователь ждёт ответа")
    vram.prepare(model)
    payload = {"model": model, "stream": True, "format": schema, "think": False, "keep_alive": vram.keep_alive(model),
               "options": vram.options(model, temperature=temperature, num_predict=num_predict),
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    status, data = _stream_chat(url, payload, timeout, cancel)
    if status == 400 and "think" in data:      # модель без режима размышлений
        payload.pop("think")
        status, data = _stream_chat(url, payload, timeout, cancel)
    if status != 200:
        raise RuntimeError(f"Ollama {status}: {data[:200]}")
    errors = [c["error"] for c in data if c.get("error")]
    if errors:
        raise RuntimeError(f"Ollama: {errors[0]}")
    content = "".join(c.get("message", {}).get("content", "") for c in data)
    body = dict(data[-1]) if data else {}
    body["message"] = {"role": "assistant", "content": content}
    if body.get("done_reason") == "length" or not content.strip():
        raise RuntimeError(f"модель не дала ответа (done_reason={body.get('done_reason')}, длина {len(content)})")
    return json.loads(content), body
