"""Рабочее окно агента: GUI-действия, которые не мешают пользователю работать за компьютером.

Агент работает со «своим» окном (то, что он открыл или выбрал), а не с активным окном пользователя:
- элементы окна читаются через UI Automation, даже если окно перекрыто другими;
- снимок окна — через PrintWindow: видно содержимое окна под другими окнами;
- клик по элементу — через шаблоны UI Automation (Invoke, Toggle, Select, Expand): ни мышь, ни фокус
  не двигаются, пользователь продолжает печатать в своём окне;
- поле ввода заполняется через ValuePattern — тоже без клавиатуры;
- где без настоящей мыши и клавиатуры нельзя (игры, рисунки, сочетания клавиш), агент «одалживает»
  фокус: ждёт паузы во вводе пользователя, на долю секунды выводит своё окно, делает действие и
  возвращает пользователю его окно и курсор на место.

SKYAGENT_GUI=foreground — старое поведение: агент работает с активным окном и настоящей мышью.
"""
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time
from contextlib import contextmanager

from . import winctl
from .winctl import user32

MODE = os.environ.get("SKYAGENT_GUI", "background").strip().lower()
OWN_TITLES = ("SkyAgent",)          # окно самого агента (терминал, Electron) — не рабочее окно
IDLE_BEFORE_BORROW = 0.6            # столько секунд без ввода пользователя — тогда одалживаем фокус
MAX_WAIT_IDLE = 6.0

gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")
for fn, res, args in (
        ("GetWindowDC", ctypes.c_void_p, (wt.HWND,)),
        ("ReleaseDC", ctypes.c_int, (wt.HWND, ctypes.c_void_p)),
        ("PrintWindow", wt.BOOL, (wt.HWND, ctypes.c_void_p, wt.UINT)),
        ("GetForegroundWindow", wt.HWND, ()),
        ("IsWindow", wt.BOOL, (wt.HWND,)),
        ("IsWindowVisible", wt.BOOL, (wt.HWND,)),
        ("IsIconic", wt.BOOL, (wt.HWND,)),
        ("GetWindowRect", wt.BOOL, (wt.HWND, ctypes.POINTER(wt.RECT))),
        ("GetConsoleWindow", wt.HWND, ())):
    lib = ctypes.windll.kernel32 if fn == "GetConsoleWindow" else user32
    getattr(lib, fn).restype = res
    getattr(lib, fn).argtypes = args
for fn, res, args in (
        ("CreateCompatibleDC", ctypes.c_void_p, (ctypes.c_void_p,)),
        ("CreateCompatibleBitmap", ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)),
        ("SelectObject", ctypes.c_void_p, (ctypes.c_void_p, ctypes.c_void_p)),
        ("DeleteObject", wt.BOOL, (ctypes.c_void_p,)),
        ("DeleteDC", wt.BOOL, (ctypes.c_void_p,)),
        ("GetDIBits", ctypes.c_int, (ctypes.c_void_p, ctypes.c_void_p, wt.UINT, wt.UINT, ctypes.c_void_p,
                                     ctypes.c_void_p, wt.UINT))):
    getattr(gdi32, fn).restype = res
    getattr(gdi32, fn).argtypes = args

_lock = threading.Lock()
_target = {"hwnd": None, "ts": 0.0, "by": ""}


def background():
    return MODE != "foreground"


# ------------------------------------------------------------ какое окно — рабочее
def _own(hwnd):
    if not hwnd:
        return True
    if hwnd == ctypes.windll.kernel32.GetConsoleWindow():
        return True
    title = winctl._title(hwnd)
    return any(t.lower() in title.lower() for t in OWN_TITLES)


def _usable(hwnd):
    return bool(hwnd) and user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd) and not winctl._cloaked(hwnd)


def _app_window(hwnd):
    """Обычное окно программы, а не оверлей/панель (NVIDIA Overlay, подсказки): у тех — служебные стили."""
    ex = user32.GetWindowLongW(hwnd, -20)        # GWL_EXSTYLE
    return _usable(hwnd) and not ex & (0x80 | 0x20 | 0x08000000)   # TOOLWINDOW, TRANSPARENT, NOACTIVATE


def set_target(hwnd, by=""):
    """Сделать окно рабочим окном агента (его открыл агент или выбрал по заголовку)."""
    if hwnd and not _own(hwnd):
        with _lock:
            _target.update(hwnd=int(hwnd), ts=time.time(), by=by)


def recent(seconds=600):
    """Агент сам выбирал рабочее окно недавно (тогда «посмотри» — про его окно, а не весь экран)."""
    return bool(_target["hwnd"]) and _usable(_target["hwnd"]) and time.time() - _target["ts"] < seconds


def target():
    """hwnd рабочего окна. Если агент окна не выбирал или оно закрыто — самое верхнее окно,
    кроме окна самого агента (иначе агент «видит» свой терминал, в котором ты только что печатал)."""
    if not background():
        return user32.GetForegroundWindow()
    h = _target["hwnd"]
    if _usable(h):
        return h
    fg = user32.GetForegroundWindow()
    if _app_window(fg) and not _own(fg):
        return fg
    for hwnd in winctl.windows():          # EnumWindows идёт сверху вниз по Z-порядку
        if _app_window(hwnd) and not _own(hwnd):
            return hwnd
    return fg


def title():
    return winctl._title(target()) or "(рабочий стол)"


def rect(hwnd=None):
    """Видимые границы окна без невидимых рамок DWM: {left, top, width, height}."""
    hwnd = hwnd or target()
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)):
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return {"left": r.left, "top": r.top, "width": max(r.right - r.left, 1), "height": max(r.bottom - r.top, 1)}


# ------------------------------------------------------------ снимок окна, даже перекрытого
class _BMI(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def capture(hwnd=None):
    """Снимок окна через PrintWindow → (PIL.Image, область на экране). None — если не получилось."""
    from PIL import Image
    hwnd = hwnd or target()
    if not _usable(hwnd):
        return None
    if user32.IsIconic(hwnd):                # свёрнутое окно не рисуется — разворачиваем без активации
        user32.ShowWindow(hwnd, 4)           # SW_SHOWNOACTIVATE
        time.sleep(0.4)
    wr = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wr))
    w, h = wr.right - wr.left, wr.bottom - wr.top
    if w < 10 or h < 10:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mdc, 2):          # PW_RENDERFULLCONTENT — и для Chromium/DirectX
            return None
        bmi = _BMI(ctypes.sizeof(_BMI), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
            return None
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    vis = rect(hwnd)                          # обрезаем невидимые рамки DWM, чтобы пиксели совпали с экраном
    dx, dy = vis["left"] - wr.left, vis["top"] - wr.top
    img = img.crop((dx, dy, dx + vis["width"], dy + vis["height"]))
    if max(img.convert("L").getextrema()) < 5:           # окно не умеет рисоваться в фоне
        return None
    return img, vis


# ------------------------------------------------------------ клик без мыши: UI Automation
def uia_click(ctrl, double=False):
    """Нажать элемент шаблоном UI Automation. → название способа или None, если элемент так не умеет."""
    if double:
        return None
    tries = (("InvokePattern", lambda p: p.Invoke(), "Invoke"),
             ("TogglePattern", lambda p: p.Toggle(), "Toggle"),
             ("SelectionItemPattern", lambda p: p.Select(), "Select"),
             ("ExpandCollapsePattern", lambda p: p.Collapse() if p.ExpandCollapseState == 1 else p.Expand(),
              "Expand"))
    for getter, act, name in tries:
        try:
            pat = getattr(ctrl, "Get" + getter)()
            if pat:
                with keep_user_focus():
                    act(pat)
                return name
        except Exception:  # noqa: BLE001 — шаблон есть, но не сработал: пробуем следующий
            continue
    return None


def uia_set_text(ctrl, text):
    """Вписать текст в поле без клавиатуры. → True, если получилось.
    Обычным полям Windows (класс *EDIT*) — сообщением WM_SETTEXT: ValuePattern у них сам ставит фокус."""
    try:
        hwnd = ctrl.NativeWindowHandle
        if hwnd and "edit" in (ctrl.ClassName or "").lower():
            user32.SendMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPCWSTR)
            user32.SendMessageW(hwnd, 0x000C, 0, text)                 # WM_SETTEXT
            return True
        pat = ctrl.GetValuePattern()
        if not pat or pat.IsReadOnly:
            return False
        with keep_user_focus():
            pat.SetValue(text)
        return True
    except Exception:  # noqa: BLE001
        return False


@contextmanager
def keep_user_focus():
    """Фоновое действие не должно отнимать фокус: если программа агента вылезла вперёд — вернуть окно пользователя."""
    fg = user32.GetForegroundWindow()
    try:
        yield
    finally:
        if background() and fg:
            for _ in range(5):
                time.sleep(0.05)
                if user32.GetForegroundWindow() != fg:
                    break
            if user32.GetForegroundWindow() != fg and user32.IsWindow(fg):
                _activate(fg)


def find_control(e, hwnd=None):
    """Найти живой UIA-элемент по записи из screen.elements (тип, имя, положение)."""
    import uiautomation as auto
    try:
        c = auto.ControlFromPoint(e["x"], e["y"])
    except Exception:  # noqa: BLE001
        c = None
    # ControlFromPoint видит то, что сверху на экране; если окно перекрыто — ищем в дереве окна
    if c and c.ControlTypeName == e["type"] and (c.Name or "").strip()[:120] == e["name"]:
        return c
    root = auto.ControlFromHandle(hwnd or target())
    best = None
    for c, _ in auto.WalkControl(root, includeTop=False, maxDepth=25):
        try:
            if c.ControlTypeName != e["type"] or (c.Name or "").strip()[:120] != e["name"]:
                continue
            r = c.BoundingRectangle
            if (r.left, r.top, r.right, r.bottom) == tuple(e["rect"]):
                return c
            best = best or c
        except Exception:  # noqa: BLE001
            continue
    return best


# ------------------------------------------------------------ одолжить фокус на мгновение
def _wait_user_idle():
    t0 = time.time()
    while time.time() - t0 < MAX_WAIT_IDLE and winctl.idle_seconds() < IDLE_BEFORE_BORROW:
        time.sleep(0.1)


@contextmanager
def borrow(hwnd=None):
    """Вывести рабочее окно на передний план только на время действия, потом вернуть пользователю
    его окно и курсор. В режиме foreground — ничего не делает."""
    hwnd = hwnd or target()
    if not background() or not hwnd:
        yield
        return
    _wait_user_idle()
    user_fg = user32.GetForegroundWindow()
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    switched = user_fg != hwnd
    if switched:
        _activate(hwnd)
    try:
        yield
    finally:
        if switched:
            time.sleep(0.15)
            user32.SetCursorPos(pt.x, pt.y)
            if user_fg and user32.IsWindow(user_fg):
                _activate(user_fg)


def _activate(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)           # SW_RESTORE
    winctl._send(winctl._key(0xA4), winctl._key(0xA4, up=True))   # Alt снимает запрет на смену фокуса
    user32.SetForegroundWindow(hwnd)
    for _ in range(10):
        if user32.GetForegroundWindow() == hwnd:
            break
        time.sleep(0.03)
    time.sleep(0.08)


def describe_target():
    h = target()
    how = "выбрано агентом" if _target["hwnd"] == h else "верхнее окно (агент своё не выбирал)"
    return f"«{winctl._title(h) or '(без заголовка)'}» ({how})"
