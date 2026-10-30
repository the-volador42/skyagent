"""SkyAgent Mini — маленькое стеклянное окно поверх всех окон.  Запуск: pythonw mini.py  (или /mini в CLI)

Свёрнуто — «пилюля» в углу экрана: состояние агента, запись, голос. Щелчок — панель:
  Чат       — написать агенту, ответ по мере набора, подтверждения опасных действий, «стоп»;
  Дневник   — личный дневник: записать мысль, удалить запись;
  Дневники  — любой дневник за любой день, очистка.
Ctrl+Alt+Space — раскрыть и сразу писать / свернуть. Окно перетаскивается за шапку, место запоминается.
Само окно ничего не знает и не хранит: память, агент и голос — в CLI (cli.py), связь через мост
(skyagent/bridge.py, только 127.0.0.1 с ключом). Без запущенного CLI окно предложит его запустить.

SKYAGENT_MINI_GLASS=acrylic (по умолчанию) | blur | off — вид стекла.
"""
import ctypes
import ctypes.wintypes as wt
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen, QShortcut, \
    QRadialGradient
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QPlainTextEdit, QPushButton, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SKYAGENT_DATA") or HERE / "data")
BRIDGE_FILE = DATA / "bridge.json"
STATE_FILE = DATA / "mini.json"
PILL = QSize(236, 46)
PANEL = QSize(384, 540)
MARGIN = 14

ACCENT = "#8fd0ff"
STATE_COLORS = {"offline": "#7a8597", "idle": "#5fb8ff", "busy": "#c792ea", "speaking": "#7ee2a8",
                "listening": "#ff9e9e", "attention": "#ffd166"}


# ---------------------------------------------------------------- Windows: стекло, хоткей, один экземпляр
class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def apply_glass(hwnd, mode):
    """Размытие того, что под окном (acrylic — как в меню Windows 11), и скруглённые углы."""
    if os.name != "nt":
        return
    try:
        state = {"acrylic": 4, "blur": 3}.get(mode, 0)
        tint = 0x40_2E_1C_10            # ABGR: тёмно-синий, полупрозрачный
        accent = ACCENT_POLICY(state, 2, tint, 0)
        data = WINCOMPATTRDATA(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
        ctypes.windll.user32.SetWindowCompositionAttribute(wt.HWND(hwnd), ctypes.byref(data))
        pref = ctypes.c_int(2)          # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), 33, ctypes.byref(pref), 4)
    except Exception:  # noqa: BLE001 — старая Windows: окно просто полупрозрачное
        pass


def single_instance():
    if os.name != "nt":
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\SkyAgentMini")
    return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS


WM_HOTKEY, MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_SPACE = 0x0312, 0x1, 0x2, 0x4000, 0x20


# ---------------------------------------------------------------- связь с CLI
class Net(QObject):
    event = Signal(dict)
    online = Signal(bool)
    reply = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.cfg = None
        self.seq = 0
        self.is_online = False
        self.s = requests.Session()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _load(self):
        try:
            cfg = json.loads(BRIDGE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not self.cfg or cfg.get("token") != self.cfg.get("token"):
            self.seq = 0            # CLI перезапустился — события начинаются заново
        self.cfg = cfg
        return cfg

    def _url(self, path):
        return f"http://127.0.0.1:{self.cfg['port']}{path}"

    def _headers(self):
        return {"X-Sky-Token": self.cfg["token"]}

    def _set_online(self, on):
        if on != self.is_online:
            self.is_online = on
            self.online.emit(on)

    def _poll_loop(self):
        while True:
            if not self._load():
                self._set_online(False)
                time.sleep(2)
                continue
            try:
                r = self.s.get(self._url("/events"), params={"after": self.seq, "wait": 25},
                               headers=self._headers(), timeout=35)
                r.raise_for_status()
                data = r.json()
                self._set_online(True)
                if data["seq"] < self.seq:           # счётчик сбросился
                    self.seq = 0
                    continue
                for ev in data["events"]:
                    self.event.emit(ev)
                self.seq = data["seq"]
            except Exception:  # noqa: BLE001 — CLI закрыт или перезапускается
                self._set_online(False)
                time.sleep(2)

    def call(self, method, path, payload=None, cb=None):
        def run():
            res = None
            try:
                if not self.cfg and not self._load():
                    raise ConnectionError("агент не запущен")
                if method == "GET":
                    r = self.s.get(self._url(path), params=payload or {}, headers=self._headers(), timeout=60)
                else:
                    r = self.s.post(self._url(path), json=payload or {}, headers=self._headers(), timeout=60)
                res = r.json()
            except Exception as e:  # noqa: BLE001
                res = {"error": str(e)}
            if cb:
                self.reply.emit(cb, res)
        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- оформление
QSS = f"""
QWidget {{ color: #eaf4ff; font-family: 'Segoe UI Variable Text', 'Segoe UI'; font-size: 10pt; background: transparent; }}
QLabel#title {{ font-size: 11pt; font-weight: 600; }}
QLabel#dim {{ color: rgba(234,244,255,0.55); font-size: 9pt; }}
QPushButton {{ background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.10);
  border-radius: 9px; padding: 5px 10px; }}
QPushButton:hover {{ background: rgba(255,255,255,0.14); }}
QPushButton:pressed {{ background: rgba(255,255,255,0.05); }}
QPushButton:disabled {{ color: rgba(234,244,255,0.3); }}
QPushButton#icon {{ padding: 3px; min-width: 26px; max-width: 26px; min-height: 22px; border-radius: 8px; }}
QPushButton#tab {{ background: transparent; border: none; border-radius: 8px; padding: 5px 12px;
  color: rgba(234,244,255,0.6); }}
QPushButton#tab:checked {{ background: rgba(143,208,255,0.18); color: #ffffff; }}
QPushButton#primary {{ background: rgba(95,184,255,0.35); border-color: rgba(143,208,255,0.5); }}
QPushButton#primary:hover {{ background: rgba(95,184,255,0.5); }}
QPushButton#danger {{ background: rgba(255,107,107,0.22); border-color: rgba(255,107,107,0.45); }}
QPushButton#danger:hover {{ background: rgba(255,107,107,0.38); }}
QLineEdit, QPlainTextEdit, QComboBox {{ background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12);
  border-radius: 10px; padding: 6px 9px; selection-background-color: rgba(95,184,255,0.5); }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: rgba(143,208,255,0.6); }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: #16243a; border: 1px solid rgba(255,255,255,0.15);
  selection-background-color: rgba(95,184,255,0.4); outline: none; }}
QTextBrowser, QListWidget {{ background: rgba(0,0,0,0.12); border: 1px solid rgba(255,255,255,0.07);
  border-radius: 12px; padding: 6px; }}
QListWidget::item {{ padding: 6px 4px; border-bottom: 1px solid rgba(255,255,255,0.06); }}
QListWidget::item:selected {{ background: rgba(95,184,255,0.18); color: #fff; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 4px 0; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.22); border-radius: 3px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QMenu {{ background: #16243a; border: 1px solid rgba(255,255,255,0.15); padding: 4px; }}
QMenu::item {{ padding: 5px 16px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(95,184,255,0.35); }}
QWidget#card {{ background: rgba(255,209,102,0.12); border: 1px solid rgba(255,209,102,0.4); border-radius: 12px; }}
QWidget#warn {{ background: rgba(255,107,107,0.12); border: 1px solid rgba(255,107,107,0.4); border-radius: 12px; }}
"""

BUBBLE_CSS = """
<style>
 .u { color:#ffffff; background-color: rgba(95,184,255,0.28); }
 .a { color:#eaf4ff; }
 .s { color: rgba(234,244,255,0.5); font-size: 9pt; }
 .n { color:#ffd166; }
 code { font-family: Consolas, monospace; background-color: rgba(255,255,255,0.08); }
</style>"""


def md_lite(text):
    """Чуть-чуть Markdown для ответа: код, жирный, переносы. Остальное — как есть."""
    t = html.escape(text or "")
    t = re.sub(r"```(?:\w+)?\n?(.*?)(```|$)", lambda m: "<pre><code>" + m[1] + "</code></pre>", t, flags=re.S)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", t, flags=re.M)
    return t.replace("\n", "<br>")


class Reactor:
    """Чем живёт анимация: громкость голоса у микрофона и нажатия клавиш.

    Голос — свой лёгкий поток с микрофона (только уровень громкости, звук никуда не пишется и не
    сохраняется; запись в память делает CLI). Включается, только когда микрофон в CLI пишет.
    Клавиши — системный хук Windows, который считает нажатия и НЕ смотрит, какая клавиша нажата.
    SKYAGENT_MINI_REACT=0 — без микрофона и клавиш (только состояния агента)."""

    def __init__(self):
        self.enabled = os.environ.get("SKYAGENT_MINI_REACT", "1") != "0"
        self.level = 0.0            # 0..1, сглаженная громкость
        self.keys = 0               # нажатий с прошлого кадра
        self._raw = 0.0
        self._stream = None
        self._hook = None
        self._proc = None

    # --- голос
    def mic(self, on):
        if not self.enabled or on == (self._stream is not None):
            return
        if not on:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream, self._raw = None, 0.0
            return
        try:
            import numpy as np
            import sounddevice as sd

            def cb(indata, frames, t, status):
                rms = float(np.sqrt(np.mean(indata[:, 0] ** 2)) + 1e-7)
                db = 20 * np.log10(rms)
                self._raw = min(1.0, max(0.0, (db + 52) / 36))     # -52 дБ — тишина, -16 дБ — громко
            self._stream = sd.InputStream(samplerate=16000, channels=1, blocksize=800, dtype="float32", callback=cb)
            self._stream.start()
        except Exception:  # noqa: BLE001 — микрофон занят или его нет: анимация без голоса
            self._stream = None

    def step(self):
        """Кадр анимации: плавно догнать громкость (быстро вверх, медленно вниз)."""
        k = 0.55 if self._raw > self.level else 0.12
        self.level += (self._raw - self.level) * k
        n, self.keys = self.keys, 0
        return self.level, n

    # --- клавиши
    def hook_keys(self):
        if not self.enabled or os.name != "nt" or self._hook:
            return
        user32 = ctypes.windll.user32
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        user32.CallNextHookEx.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
        user32.SetWindowsHookExW.restype = wt.HHOOK

        def proc(code, wparam, lparam):
            if code == 0 and wparam in (0x0100, 0x0104):           # WM_KEYDOWN, WM_SYSKEYDOWN — и всё
                self.keys += 1
            return user32.CallNextHookEx(None, code, wparam, lparam)
        self._proc = HOOKPROC(proc)          # держим ссылку, иначе сборщик мусора уронит процесс
        self._hook = user32.SetWindowsHookExW(13, self._proc, None, 0)      # WH_KEYBOARD_LL

    def close(self):
        self.mic(False)
        if self._hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._hook)
            self._hook = None


class Orb(QWidget):
    """Светящийся шар — лицо ассистента. Цвет — состояние; дышит, когда думает или говорит;
    раздувается от твоего голоса; от нажатий клавиш расходятся круги."""

    def __init__(self, size=22, pad=9):
        super().__init__()
        self.core = size
        self.setFixedSize(size + 2 * pad, size + 2 * pad)
        self.color = QColor(STATE_COLORS["offline"])
        self.pulse = False
        self.phase = 0.0
        self.level = 0.0
        self.ripples = []            # моменты нажатий: круг расходится 0.7 с

    def set_state(self, color, pulse):
        self.color = QColor(color)
        self.pulse = pulse
        self.update()

    def frame(self, level, keys, now):
        """Вызывается из общего таймера. Возвращает True, если есть что анимировать."""
        if keys:
            self.ripples = (self.ripples + [now] * min(keys, 2))[-6:]
        self.ripples = [t for t in self.ripples if now - t < 0.7]
        busy = self.pulse or level > 0.02 or self.ripples or abs(level - self.level) > 0.01
        if self.pulse:
            self.phase = (self.phase + 0.11) % 6.283
        self.level = level
        if busy:
            self.update()
        return bool(busy)

    def paintEvent(self, _):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c0 = self.width() / 2
        now = time.time()
        # круги от клавиш
        for t in self.ripples:
            k = (now - t) / 0.7
            rad = self.core / 2 + 2 + k * (c0 - self.core / 2 - 2)
            col = QColor(self.color)
            col.setAlphaF(max(0.0, 0.9 * (1 - k) ** 1.5))
            p.setPen(QPen(col.lighter(130), 2.2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QRect(int(c0 - rad), int(c0 - rad), int(2 * rad), int(2 * rad)))
        # свечение: дыхание + голос
        breath = 0.5 + 0.5 * math.sin(self.phase) if self.pulse else 0.3
        flash = max(0.0, 1 - (now - self.ripples[-1]) / 0.25) if self.ripples else 0.0    # вспышка от клавиши
        glow = min(1.0, breath * 0.8 + self.level * 0.9 + flash * 0.6)
        gr = self.core / 2 + 3 + (c0 - self.core / 2 - 3) * (0.45 + 0.55 * max(self.level, breath * 0.6))
        g = QRadialGradient(c0, c0, gr)
        c = QColor(self.color)
        c.setAlphaF(0.6 * glow)
        g.setColorAt(0.5, c)
        c2 = QColor(self.color)
        c2.setAlphaF(0.0)
        g.setColorAt(1.0, c2)
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(QRect(int(c0 - gr), int(c0 - gr), int(2 * gr), int(2 * gr)))
        # ядро чуть растёт от голоса
        rc = self.core / 2 * (1 + 0.16 * self.level)
        core = QRadialGradient(c0 - rc * 0.35, c0 - rc * 0.45, rc * 1.6)
        core.setColorAt(0.0, QColor("#ffffff"))
        core.setColorAt(0.4, self.color.lighter(int(115 + 70 * flash)))
        core.setColorAt(1.0, self.color.darker(170))
        p.setBrush(core)
        p.drawEllipse(QRect(int(c0 - rc), int(c0 - rc), int(2 * rc), int(2 * rc)))


class Wave(QWidget):
    """Пять столбиков-эквалайзер: живут от голоса у микрофона, а когда говорит Скай — сами."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(30, 22)
        self.h = [0.12] * 5
        self.color = QColor(ACCENT)
        self.t0 = time.time()

    def frame(self, level, speaking, now):
        import math
        target = []
        for i in range(5):
            if speaking:
                v = 0.35 + 0.3 * math.sin(now * 9 + i * 1.3) + 0.2 * math.sin(now * 5.3 + i * 2.1)
            else:
                v = level * (0.55 + 0.45 * math.sin(now * 11 + i * 1.7))
            target.append(max(0.12, min(1.0, v)))
        moved = any(abs(a - b) > 0.01 for a, b in zip(self.h, target))
        self.h = [a + (b - a) * 0.5 for a, b in zip(self.h, target)]
        if moved:
            self.update()
        return moved

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        bw, gap = 3, 3
        x0 = (self.width() - (5 * bw + 4 * gap)) / 2
        for i, v in enumerate(self.h):
            hh = max(3.0, v * self.height())
            p.drawRoundedRect(int(x0 + i * (bw + gap)), int((self.height() - hh) / 2), bw, int(hh), 1.5, 1.5)


# ---------------------------------------------------------------- окно
class Mini(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("SkyAgent Mini")
        self.setStyleSheet(QSS)
        self.net = Net()
        self.net.event.connect(self.on_event)
        self.net.online.connect(self.on_online)
        self.net.reply.connect(lambda cb, res: cb(res))
        self.online = False
        self.busy = False
        self.speaking = False
        self.listening_until = 0
        self.attention = None
        self.expanded = False
        self.chat = []                    # [(роль, текст)]: u — я, a — ассистент, s — служебное, n — напоминание
        self.status = {}
        self.confirm_id = None
        self.anchor = self._load_anchor()
        self._drag = None

        # не QStackedWidget: он держит минимальный размер самой большой страницы, и пилюля раздувается
        self.pill = self._build_pill()
        self.panel = self._build_panel()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.pill)
        lay.addWidget(self.panel)

        self.render_timer = QTimer(self, singleShot=True, interval=70, timeout=self._render_chat)
        self.status_timer = QTimer(self, interval=4000, timeout=self.refresh_status)
        self.status_timer.start()
        self.toast_timer = QTimer(self, singleShot=True, timeout=self._clear_toast)
        self.reactor = Reactor()
        self.reactor.hook_keys()
        self.anim = QTimer(self, interval=33, timeout=self._animate)      # ~30 кадров/с, дёшево
        self.anim.start()
        QShortcut(QKeySequence("Escape"), self, activated=self.collapse)
        self.collapse()
        self._update_face()

    # ------------------------------------------------------------ вид
    def showEvent(self, e):
        super().showEvent(e)
        hwnd = int(self.winId())
        apply_glass(hwnd, os.environ.get("SKYAGENT_MINI_GLASS", "acrylic").lower())
        if os.name == "nt" and not getattr(self, "_hotkey", False):
            self._hotkey = bool(ctypes.windll.user32.RegisterHotKey(
                wt.HWND(hwnd), 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_SPACE))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        path = QPainterPath()
        radius = 22 if not self.expanded else 14
        path.addRoundedRect(rect, radius, radius)
        p.fillPath(path, QColor(16, 30, 52, 70 if os.environ.get("SKYAGENT_MINI_GLASS", "acrylic") != "off" else 225))
        p.setPen(QPen(QColor(255, 255, 255, 46), 1))
        p.drawPath(path)

    def nativeEvent(self, event_type, message):
        if os.name == "nt" and event_type == b"windows_generic_MSG":
            msg = wt.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == 1:
                if self.expanded and self.isActiveWindow():
                    self.collapse()
                else:
                    self.expand("chat", focus=True)
                return True, 0
        return super().nativeEvent(event_type, message)

    def _animate(self):
        level, keys = self.reactor.step()
        now = time.time()
        if not self.isVisible():
            return
        if self.expanded:
            moving = self.orb2.frame(level, keys, now)
        else:
            moving = self.orb.frame(level, keys, now)
            moving = self.wave.frame(level, self.speaking, now) or moving
        # в покое — 10 кадров/с (только ловить начало голоса и нажатия), в движении — 30
        want = 33 if moving or level > 0.01 else 100
        if self.anim.interval() != want:
            self.anim.setInterval(want)

    def closeEvent(self, e):
        self.reactor.close()
        super().closeEvent(e)

    # ------------------------------------------------------------ свёрнутое
    def _build_pill(self):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 2, 12, 2)
        h.setSpacing(4)
        self.orb = Orb(22, 9)
        h.addWidget(self.orb)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.pill_title = QLabel("Скай")
        self.pill_title.setObjectName("title")
        self.pill_text = QLabel("")
        self.pill_text.setObjectName("dim")
        col.addWidget(self.pill_title)
        col.addWidget(self.pill_text)
        h.addLayout(col, 1)
        self.wave = Wave()
        h.addWidget(self.wave)
        self.pill_badges = QLabel("")
        self.pill_badges.setObjectName("dim")
        h.addWidget(self.pill_badges)
        return w

    # ------------------------------------------------------------ панель
    def _build_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(12, 10, 12, 12)
        v.setSpacing(8)

        head = QHBoxLayout()
        self.orb2 = Orb(18, 7)
        head.addWidget(self.orb2)
        t = QLabel("Скай")
        t.setObjectName("title")
        head.addWidget(t)
        self.head_text = QLabel("")
        self.head_text.setObjectName("dim")
        head.addWidget(self.head_text, 1)
        self.btn_mic = self._icon("🎙", "Микрофон: запись / пауза", lambda: self.toggle_sensor("mic"))
        self.btn_tg = self._icon("✈", "Telegram: запись / пауза", lambda: self.toggle_sensor("telegram"))
        self.btn_tts = self._icon("🔈", "Голос: молчать → на голосовые команды → всегда", self.cycle_tts)
        self.btn_min = self._icon("—", "Свернуть (Esc)", self.collapse)
        for b in (self.btn_mic, self.btn_tg, self.btn_tts, self.btn_min):
            head.addWidget(b)
        v.addLayout(head)

        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        self.tab_btns = {}
        for key, label in (("chat", "Чат"), ("journal", "Дневник"), ("diaries", "Дневники")):
            b = QPushButton(label)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self.show_tab(k))
            tabs.addWidget(b)
            self.tab_btns[key] = b
        tabs.addStretch(1)
        v.addLayout(tabs)

        self.tabs = QStackedWidget()
        self.tabs.addWidget(self._build_chat())
        self.tabs.addWidget(self._build_journal())
        self.tabs.addWidget(self._build_diaries())
        v.addWidget(self.tabs, 1)

        self.offline_bar = QWidget()
        self.offline_bar.setObjectName("warn")
        ob = QHBoxLayout(self.offline_bar)
        ob.setContentsMargins(10, 6, 6, 6)
        lbl = QLabel("Агент не запущен")
        ob.addWidget(lbl, 1)
        b = QPushButton("Запустить")
        b.setObjectName("primary")
        b.clicked.connect(self.launch_cli)
        ob.addWidget(b)
        v.addWidget(self.offline_bar)
        self.show_tab("chat")
        return w

    def _icon(self, text, tip, fn):
        b = QPushButton(text)
        b.setObjectName("icon")
        b.setToolTip(tip)
        b.clicked.connect(fn)
        return b

    def show_tab(self, key):
        idx = {"chat": 0, "journal": 1, "diaries": 2}[key]
        self.tabs.setCurrentIndex(idx)
        for k, b in self.tab_btns.items():
            b.setChecked(k == key)
        if key == "journal":
            self.load_journal()
        elif key == "diaries":
            self.load_diaries()

    # --- чат
    def _build_chat(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        v.addWidget(self.view, 1)

        self.card = QWidget()
        self.card.setObjectName("card")
        cv = QVBoxLayout(self.card)
        cv.setContentsMargins(10, 8, 10, 8)
        self.card_text = QLabel("")
        self.card_text.setWordWrap(True)
        cv.addWidget(self.card_text)
        ch = QHBoxLayout()
        yes = QPushButton("Разрешить")
        yes.setObjectName("primary")
        yes.clicked.connect(lambda: self.answer_confirm(True))
        no = QPushButton("Отклонить")
        no.clicked.connect(lambda: self.answer_confirm(False))
        ch.addStretch(1)
        ch.addWidget(no)
        ch.addWidget(yes)
        cv.addLayout(ch)
        self.card.hide()
        v.addWidget(self.card)

        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("Напиши Скай…  (Enter)")
        self.input.returnPressed.connect(self.send)
        row.addWidget(self.input, 1)
        self.btn_send = QPushButton("➤")
        self.btn_send.setObjectName("primary")
        self.btn_send.setFixedWidth(40)
        self.btn_send.clicked.connect(self.send)
        row.addWidget(self.btn_send)
        v.addLayout(row)
        return w

    # --- личный дневник
    def _build_journal(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        self.j_head = QLabel("Сегодня")
        self.j_head.setObjectName("dim")
        v.addWidget(self.j_head)
        self.j_list = QListWidget()
        self.j_list.setWordWrap(True)
        self.j_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.j_list.customContextMenuRequested.connect(self._journal_menu)
        v.addWidget(self.j_list, 1)
        self.j_edit = QPlainTextEdit()
        self.j_edit.setPlaceholderText("Что у тебя на уме? Ctrl+Enter — записать")
        self.j_edit.setFixedHeight(84)
        QShortcut(QKeySequence("Ctrl+Return"), self.j_edit, activated=self.add_journal)
        v.addWidget(self.j_edit)
        row = QHBoxLayout()
        hint = QLabel("Правый щелчок по записи — удалить")
        hint.setObjectName("dim")
        row.addWidget(hint, 1)
        b = QPushButton("Записать")
        b.setObjectName("primary")
        b.clicked.connect(self.add_journal)
        row.addWidget(b)
        v.addLayout(row)
        return w

    # --- все дневники
    def _build_diaries(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        row = QHBoxLayout()
        self.d_name = QComboBox()
        self.d_day = QComboBox()
        self.d_name.currentIndexChanged.connect(self.load_diary)
        self.d_day.currentIndexChanged.connect(self.load_diary)
        row.addWidget(self.d_name, 3)
        row.addWidget(self.d_day, 2)
        v.addLayout(row)
        self.d_view = QTextBrowser()
        v.addWidget(self.d_view, 1)

        self.d_confirm = QWidget()
        self.d_confirm.setObjectName("warn")
        cv = QVBoxLayout(self.d_confirm)
        cv.setContentsMargins(10, 8, 10, 8)
        self.d_confirm_text = QLabel("")
        self.d_confirm_text.setWordWrap(True)
        cv.addWidget(self.d_confirm_text)
        ch = QHBoxLayout()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.d_confirm.hide)
        day = QPushButton("За этот день")
        day.setObjectName("danger")
        day.clicked.connect(lambda: self.clear_diary(False))
        allb = QPushButton("За всё время")
        allb.setObjectName("danger")
        allb.clicked.connect(lambda: self.clear_diary(True))
        ch.addWidget(cancel)
        ch.addStretch(1)
        ch.addWidget(day)
        ch.addWidget(allb)
        cv.addLayout(ch)
        self.d_confirm.hide()
        v.addWidget(self.d_confirm)

        row2 = QHBoxLayout()
        self.d_info = QLabel("")
        self.d_info.setObjectName("dim")
        row2.addWidget(self.d_info, 1)
        ref = QPushButton("Обновить")
        ref.clicked.connect(self.load_diary)
        row2.addWidget(ref)
        self.d_clear = QPushButton("Очистить…")
        self.d_clear.setObjectName("danger")
        self.d_clear.clicked.connect(self.ask_clear)
        row2.addWidget(self.d_clear)
        v.addLayout(row2)
        return w

    # ------------------------------------------------------------ геометрия
    def _screen(self):
        scr = QGuiApplication.screenAt(self.anchor) or QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def _load_anchor(self):
        try:
            a = json.loads(STATE_FILE.read_text(encoding="utf-8"))["anchor"]
            pt = QPoint(int(a[0]), int(a[1]))
            if QGuiApplication.screenAt(pt - QPoint(10, 10)):
                return pt
        except (OSError, ValueError, KeyError, TypeError):
            pass
        g = QGuiApplication.primaryScreen().availableGeometry()
        return QPoint(g.right() - MARGIN, g.bottom() - MARGIN)

    def _save_anchor(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps({"anchor": [self.anchor.x(), self.anchor.y()]}), encoding="utf-8")
        except OSError:
            pass

    def _place(self, size):
        g = self._screen()
        x = min(max(self.anchor.x() - size.width() + 1, g.left()), g.right() - size.width() + 1)
        y = min(max(self.anchor.y() - size.height() + 1, g.top()), g.bottom() - size.height() + 1)
        self.setGeometry(QRect(QPoint(x, y), size))

    def collapse(self):
        self.expanded = False
        self.panel.hide()
        self.pill.show()
        self.setMinimumSize(PILL)
        self._place(PILL)
        self.update()

    def expand(self, tab=None, focus=False):
        self.expanded = True
        self.pill.hide()
        self.panel.show()
        self.setMinimumSize(PANEL)
        self._place(PANEL)
        if tab:
            self.show_tab(tab)
        self.show()
        self.raise_()
        if focus:
            self.activateWindow()
            self.input.setFocus()
        self._clear_toast()
        self.update()

    # перетаскивание за шапку / пилюлю; щелчок по пилюле — раскрыть
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = (e.globalPosition().toPoint(), self.pos(), False)

    def mouseMoveEvent(self, e):
        if self._drag:
            start, pos, moved = self._drag
            delta = e.globalPosition().toPoint() - start
            if moved or delta.manhattanLength() > 4:
                self._drag = (start, pos, True)
                self.move(pos + delta)

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        _, _, moved = self._drag
        self._drag = None
        if moved:
            self.anchor = self.geometry().bottomRight()
            self._save_anchor()
        elif not self.expanded:
            self.expand("chat", focus=True)

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a_open = m.addAction("Свернуть" if self.expanded else "Раскрыть")
        a_reset = m.addAction("Вернуть в угол экрана")
        m.addSeparator()
        a_quit = m.addAction("Закрыть мини-окно")
        act = m.exec(e.globalPos())
        if act == a_open:
            self.collapse() if self.expanded else self.expand("chat", focus=True)
        elif act == a_reset:
            g = QGuiApplication.primaryScreen().availableGeometry()
            self.anchor = QPoint(g.right() - MARGIN, g.bottom() - MARGIN)
            self._save_anchor()
            self.expand() if self.expanded else self.collapse()
        elif act == a_quit:
            self.reactor.close()
            QApplication.quit()

    # ------------------------------------------------------------ состояние
    def _update_face(self):
        now = time.time()
        if not self.online:
            key, text = "offline", "агент не запущен"
        elif self.confirm_id:
            key, text = "attention", "ждёт разрешения"
        elif self.busy:
            key, text = "busy", "думаю…"
        elif self.speaking:
            key, text = "speaking", "говорю…"
        elif now < self.listening_until:
            key, text = "listening", "слушаю…"
        elif self.attention:
            key, text = "attention", self.attention
        else:
            key, text = "idle", "готов · Ctrl+Alt+Space"
        pulse = key in ("busy", "speaking", "listening", "attention")
        self.orb.set_state(STATE_COLORS[key], pulse)
        self.orb2.set_state(STATE_COLORS[key], pulse)
        short = text if len(text) <= 30 else text[:29] + "…"
        self.pill_text.setText(self.pill_text.fontMetrics().elidedText(text, Qt.ElideRight, 140))
        self.pill_text.setToolTip(text)
        self.head_text.setText(short if key != "idle" else (self.status.get("model") or ""))
        sens = self.status.get("sensors", {})
        self.reactor.mic(self.online and sens.get("mic", {}).get("state") == "recording")
        badges = []
        if sens.get("mic", {}).get("state") == "recording":
            badges.append("●")
        if sens.get("telegram", {}).get("state") == "recording":
            badges.append("✈")
        tts = self.status.get("tts", {}).get("mode")
        badges.append({"off": "🔇", "voice": "🔈", "all": "🔊"}.get(tts, ""))
        self.pill_badges.setText(" ".join(b for b in badges if b))
        self.offline_bar.setVisible(not self.online)
        self.btn_mic.setText("🎙" if sens.get("mic", {}).get("state") == "recording" else "🚫")
        self.btn_mic.setEnabled("mic" in sens)
        self.btn_tg.setEnabled("telegram" in sens)
        self.btn_tg.setToolTip("Telegram: " + (sens.get("telegram", {}).get("state") or "не настроен")
                               + (f" — {sens['telegram']['error']}" if sens.get("telegram", {}).get("error") else ""))
        self.btn_tg.setText("✈" if sens.get("telegram", {}).get("state") == "recording" else "✈̸")
        self.btn_tts.setText({"off": "🔇", "voice": "🔈", "all": "🔊"}.get(tts, "🔈"))
        self.btn_tts.setToolTip(f"Голос: {tts or '?'} · {self.status.get('tts', {}).get('engine', '')}\n"
                                "молчать → на голосовые команды → всегда")
        self.btn_send.setText("■" if self.busy or self.speaking else "➤")
        self.btn_send.setToolTip("Остановить" if self.busy or self.speaking else "Отправить")

    def on_online(self, on):
        self.online = on
        if on:
            self.refresh_status()
        else:
            self.busy = self.speaking = False
            self.confirm_id = None
            self.card.hide()
        self._update_face()

    def refresh_status(self):
        if not self.online:
            return

        def done(res):
            if "error" not in res:
                self.status = res
                self.busy = res.get("busy", self.busy)
                self.speaking = res.get("tts", {}).get("speaking", self.speaking)
            self._update_face()
        self.net.call("GET", "/status", cb=done)

    def toast(self, text, sec=8):
        self.attention = text
        self.toast_timer.start(sec * 1000)
        self._update_face()

    def _clear_toast(self):
        self.attention = None
        self._update_face()

    # ------------------------------------------------------------ события от CLI
    def on_event(self, ev):
        k, d = ev["kind"], ev["data"]
        if k == "busy":
            self.busy = d["on"]
            if d["on"] and d.get("text"):
                self._push("u", ("🎙 " if d.get("via") == "voice" else "") + d["text"])
                self._push("a", "")
        elif k == "token":
            if not self.chat or self.chat[-1][0] != "a":
                self._push("a", "")
            self.chat[-1] = ("a", self.chat[-1][1] + d)
        elif k == "done":
            if self.chat and self.chat[-1][0] == "a":
                self.chat[-1] = ("a", d)
            else:
                self._push("a", d)
            if not self.expanded:
                self.toast("ответ: " + (d or "").replace("\n", " ")[:60])
        elif k == "tool":
            self._push("s", f"⚙ {d['name']}", before_answer=True)
        elif k == "info":
            self._push("s", str(d)[:300], before_answer=True)
        elif k == "notify":
            self._push("n", "🔔 " + d["message"])
            self.toast("🔔 " + d["message"], 15)
        elif k == "voice":
            if d.get("listening"):
                self.listening_until = time.time() + 8
                QTimer.singleShot(8100, self._update_face)
        elif k == "speaking":
            self.speaking = d["on"]
        elif k == "confirm":
            self.confirm_id = d["id"]
            self.card_text.setText(f"Разрешить <b>{html.escape(d['name'])}</b>?<br>"
                                   f"<span style='color:rgba(234,244,255,0.6)'>{html.escape(d['args'])}</span>")
            self.card.show()
            self.expand("chat")
        elif k == "confirm_done":
            if self.confirm_id == d["id"]:
                self.confirm_id = None
                self.card.hide()
        elif k == "job_done":
            self._push("s", f"✔ фоновая задача {d.get('name')} готова")
        elif k == "bye":
            self.online = False
            self.busy = self.speaking = False
        self._update_face()
        self.render_timer.start()

    def _push(self, role, text, before_answer=False):
        # служебные строки (инструменты) — перед пустым ответом, который сейчас набирается
        if before_answer and self.chat and self.chat[-1] == ("a", ""):
            self.chat.insert(len(self.chat) - 1, (role, text))
        else:
            self.chat.append((role, text))
        del self.chat[:-80]
        self.render_timer.start()

    def _render_chat(self):
        parts = [BUBBLE_CSS]
        if not self.chat:
            parts.append("<p class='s'>Здесь будет разговор с агентом. Пиши — или скажи «Скай, …».</p>")
        for role, text in self.chat:
            if role == "u":
                parts.append(f"<table width='100%' cellpadding='7'><tr><td width='18%'></td>"
                             f"<td class='u'>{md_lite(text)}</td></tr></table>")
            elif role == "a":
                parts.append(f"<p class='a'>{md_lite(text) if text else '<span class=s>…</span>'}</p>")
            elif role == "n":
                parts.append(f"<p class='n'>{md_lite(text)}</p>")
            else:
                parts.append(f"<p class='s'>{html.escape(text)}</p>")
        bar = self.view.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 30
        self.view.setHtml("".join(parts))
        if at_end:
            bar.setValue(bar.maximum())

    # ------------------------------------------------------------ действия
    def send(self):
        if self.busy or self.speaking:
            self.net.call("POST", "/stop")
            return
        text = self.input.text().strip()
        if not text:
            return
        if not self.online:
            self.toast("агент не запущен")
            return
        self.input.clear()
        self.net.call("POST", "/say", {"text": text})

    def answer_confirm(self, allow):
        self.net.call("POST", "/confirm", {"allow": allow})
        self.card.hide()
        self.confirm_id = None
        self._update_face()

    def toggle_sensor(self, name):
        st = self.status.get("sensors", {}).get(name, {}).get("state")
        self.net.call("POST", "/sensor", {"name": name, "on": st != "recording"}, cb=lambda _: self.refresh_status())

    def cycle_tts(self):
        order = ["off", "voice", "all"]
        cur = self.status.get("tts", {}).get("mode", "voice")
        nxt = order[(order.index(cur) + 1) % 3] if cur in order else "voice"
        self.net.call("POST", "/tts", {"mode": nxt}, cb=lambda _: self.refresh_status())
        self.toast({"off": "голос выключен", "voice": "голос: на голосовые команды", "all": "голос: на всё"}[nxt], 3)

    def launch_cli(self):
        bat = HERE / "SkyAgentCLI.bat"
        subprocess.Popen(["cmd", "/c", "start", "SkyAgent", str(bat)], cwd=str(HERE))
        self.toast("запускаю агента…", 20)

    # --- личный дневник
    def load_journal(self):
        def done(res):
            self.j_list.clear()
            entries = res.get("entries") or []
            self.j_head.setText(f"Сегодня, {datetime.now().strftime('%d.%m')} · записей: {len(entries)}"
                                if "error" not in res else "Агент не запущен — записи недоступны")
            for e in entries:
                it = QListWidgetItem(f"{datetime.fromtimestamp(e['ts']).strftime('%H:%M')}   {e['text']}")
                it.setData(Qt.UserRole, e["id"])
                self.j_list.addItem(it)
            self.j_list.scrollToBottom()
        self.net.call("GET", "/journal", cb=done)

    def add_journal(self):
        text = self.j_edit.toPlainText().strip()
        if not text:
            return

        def done(res):
            if "error" in res:
                self.toast("не записано: агент не запущен")
                return
            self.j_edit.clear()
            self.load_journal()
        self.net.call("POST", "/journal/add", {"text": text}, cb=done)

    def _journal_menu(self, pos):
        it = self.j_list.itemAt(pos)
        if not it:
            return
        m = QMenu(self)
        act = m.addAction("Удалить запись")
        if m.exec(self.j_list.mapToGlobal(pos)) == act:
            self.net.call("POST", "/journal/del", {"id": it.data(Qt.UserRole)}, cb=lambda _: self.load_journal())

    # --- дневники
    def load_diaries(self):
        def done(res):
            if "error" in res:
                self.d_view.setPlainText("Агент не запущен.")
                return
            cur_n, cur_d = self.d_name.currentData(), self.d_day.currentData()
            self.meta = {x["name"]: x for x in res["diaries"]}
            for combo in (self.d_name, self.d_day):
                combo.blockSignals(True)
                combo.clear()
            for x in res["diaries"]:
                self.d_name.addItem(x["title"], x["name"])
            today = datetime.now().date().isoformat()
            self.d_day.addItem("сегодня", "сегодня")
            for day in res["days"]:
                if day != today:
                    self.d_day.addItem(datetime.fromisoformat(day).strftime("%d.%m.%Y"), day)
            i = self.d_name.findData(cur_n or "journal")
            self.d_name.setCurrentIndex(max(0, i))
            j = self.d_day.findData(cur_d)
            self.d_day.setCurrentIndex(max(0, j))
            for combo in (self.d_name, self.d_day):
                combo.blockSignals(False)
            self.load_diary()
        self.net.call("GET", "/diaries", cb=done)

    def load_diary(self):
        name, day = self.d_name.currentData(), self.d_day.currentData()
        if not name:
            return
        self.d_confirm.hide()
        self.d_clear.setEnabled(bool(getattr(self, "meta", {}).get(name, {}).get("clearable")))

        def done(res):
            if "error" in res:
                self.d_view.setPlainText(f"Не получилось: {res['error']}")
                return
            self.d_view.setHtml(BUBBLE_CSS + "<div class='a'>" + md_lite(res["text"]) + "</div>")
            self.d_info.setText(f"{len(res['text'])} знаков")
        self.net.call("GET", "/diary", {"name": name, "day": day}, cb=done)

    def ask_clear(self):
        self.d_confirm_text.setText(f"Очистить дневник «{self.d_name.currentText()}»? Записи удалятся из памяти "
                                    f"насовсем (поиск и векторы тоже).")
        self.d_confirm.show()

    def clear_diary(self, whole):
        name, day = self.d_name.currentData(), self.d_day.currentData()
        payload = {"name": name, "all": True} if whole else {"name": name, "day": day}

        def done(res):
            self.d_confirm.hide()
            if "error" in res:
                self.toast(f"не очищено: {res['error']}"[:60])
            else:
                self.d_info.setText(f"удалено записей: {res.get('deleted', 0)}")
                self.load_diary()
        self.net.call("POST", "/diary/clear", payload, cb=done)


def main():
    if not single_instance():
        return 0
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SkyAgent.Mini")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    app.setFont(QFont("Segoe UI", 10))
    w = Mini()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
