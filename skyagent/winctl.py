"""Низкоуровневое управление Windows через WinAPI (SendInput, окна)."""
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
ULONG_PTR = ctypes.c_size_t

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT

VK = {
    "ctrl": 0xA2, "control": 0xA2, "lctrl": 0xA2, "rctrl": 0xA3,
    "shift": 0xA0, "lshift": 0xA0, "rshift": 0xA1,
    "alt": 0xA4, "lalt": 0xA4, "ralt": 0xA5, "altgr": 0xA5,
    "win": 0x5B, "windows": 0x5B, "winleft": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C, "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "pause": 0x13, "apps": 0x5D, "menu": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD, "mute": 0xAD,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
EXTENDED = {0xA3, 0xA5, 0x5B, 0x5C, 0x5D, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22,
            0x25, 0x26, 0x27, 0x28, 0x2C, 0x90, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"SendInput заблокирован (ошибка {ctypes.get_last_error()}). "
                      "Если активное окно запущено от администратора — запусти агента тоже от администратора.")


def _key(vk, up=False):
    flags = (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0) | (KEYEVENTF_KEYUP if up else 0)
    scan = user32.MapVirtualKeyW(vk, 0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


def vk_of(name: str) -> int:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1:
        if n.isalnum() and n.isascii():
            return ord(n.upper())
        r = user32.VkKeyScanW(ord(n))
        if r != -1 and (r & 0xFF) != 0xFF:
            return r & 0xFF
    raise ValueError(f"Неизвестная клавиша: '{name}'")


def hotkey(names, hold=0.04):
    """Нажать сочетание: модификаторы зажимаются по очереди и отпускаются в обратном порядке."""
    vks = [vk_of(n) for n in names]
    for vk in vks:
        _send(_key(vk))
        time.sleep(hold)
    for vk in reversed(vks):
        _send(_key(vk, up=True))
        time.sleep(hold / 2)


def type_unicode(text: str, delay=0.008):
    """Печатает любой текст независимо от раскладки (кириллица, emoji)."""
    for ch in text:
        if ch == "\n":
            hotkey(["enter"], 0.01)
            continue
        if ch == "\t":
            hotkey(["tab"], 0.01)
            continue
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            down = INPUT(type=INPUT_KEYBOARD,
                         u=_INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
            up = INPUT(type=INPUT_KEYBOARD,
                       u=_INPUTUNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
            _send(down, up)
        time.sleep(delay)


# ---------------------------------------------------------------- окна
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _cloaked(hwnd):
    val = wt.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
    return bool(val.value)


def windows():
    """Видимые окна верхнего уровня: {hwnd: заголовок}."""
    res = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not _cloaked(hwnd) and user32.GetWindow(hwnd, 4) == 0:
            t = _title(hwnd)
            if t and t not in ("Program Manager", "Windows Input Experience"):
                res[hwnd] = t
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return res


def active_title():
    return _title(user32.GetForegroundWindow()) or "(рабочий стол)"


def focus(part: str):
    """Вывести на передний план окно, в заголовке которого есть part."""
    part_l = part.lower()
    match = [(h, t) for h, t in windows().items() if part_l in t.lower()]
    if not match:
        return None
    hwnd, title = match[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows не даёт фоновому процессу забрать фокус — короткое нажатие Alt снимает блокировку
    _send(_key(0xA4), _key(0xA4, up=True))
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    return title if user32.GetForegroundWindow() == hwnd else f"!{title}"


# ---------------------------------------------------------------- наблюдение (для сенсора окон)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


kernel32.GetTickCount64.restype = ctypes.c_ulonglong


def idle_seconds() -> float:
    """Сколько секунд не было ввода с клавиатуры и мыши."""
    lii = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    now = kernel32.GetTickCount64() & 0xFFFFFFFF          # dwTime — 32-битные тики
    return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def _pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_path(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _uwp_child_pid(hwnd, host_pid):
    """У UWP-приложений окно принадлежит ApplicationFrameHost; настоящий процесс — у дочернего окна."""
    found = []

    def cb(child, _):
        p = _pid(child)
        if p and p != host_pid:
            found.append(p)
            return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return found[0] if found else host_pid


def foreground():
    """Активное окно: {hwnd, title, pid, exe, path} или None (рабочий стол / экран блокировки)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = _pid(hwnd)
    path = process_path(pid)
    if path.lower().endswith("applicationframehost.exe"):
        pid = _uwp_child_pid(hwnd, pid)
        path = process_path(pid) or path
    exe = path.rsplit("\\", 1)[-1]
    return {"hwnd": hwnd, "title": _title(hwnd), "pid": pid, "exe": exe, "path": path}
