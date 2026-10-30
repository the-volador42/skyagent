"""Зрение для работы через GUI: что на экране и где — с точными координатами.

Цикл агента: «посмотреть → найти элемент → кликнуть → убедиться, что сработало → посмотреть снова».
От точного и дешёвого к дорогому:
  1. UI Automation — Windows сам сообщает кнопки, поля, пункты меню активного окна с точными
     границами (десятые доли секунды, без модели). Агент кликает по номеру элемента.
  2. Модель зрения — где UIA нет (игры, канвасы, нестандартный интерфейс): модель отдаёт рамку
     элемента в относительных координатах 0..1000, в пиксели экрана их переводит КОД, а не агент.
  3. Проверка после действия: насколько изменилась картинка, какое окно активно, — агент видит,
     сработал ли клик, и решает, смотреть ли снова.
Координаты везде — абсолютные пиксели виртуального рабочего стола (несколько мониторов, масштаб
учтены: процесс помечается как DPI-aware, снимки — через mss с учётом смещения монитора).
"""
import base64
import ctypes
import io
import json
import re
import threading
import time

import numpy as np

try:      # точные пиксели при масштабе 125–200%: иначе снимок и клики разъедутся
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass

INTERACTIVE = {"ButtonControl", "MenuItemControl", "EditControl", "ComboBoxControl", "CheckBoxControl",
               "RadioButtonControl", "HyperlinkControl", "ListItemControl", "TabItemControl", "TreeItemControl",
               "SplitButtonControl", "MenuBarControl", "DocumentControl", "SliderControl", "SpinnerControl",
               "DataItemControl", "HeaderItemControl"}
TEXTUAL = {"TextControl"}
TYPE_RU = {"ButtonControl": "кнопка", "MenuItemControl": "пункт меню", "EditControl": "поле ввода",
           "ComboBoxControl": "список", "CheckBoxControl": "флажок", "RadioButtonControl": "переключатель",
           "HyperlinkControl": "ссылка", "ListItemControl": "элемент списка", "TabItemControl": "вкладка",
           "TreeItemControl": "элемент дерева", "SplitButtonControl": "кнопка-меню", "MenuBarControl": "меню",
           "DocumentControl": "документ", "SliderControl": "ползунок", "SpinnerControl": "счётчик",
           "DataItemControl": "ячейка", "HeaderItemControl": "заголовок столбца", "TextControl": "текст"}
MAX_ELEMENTS = 150
WALK_SEC = 2.0

_lock = threading.Lock()
_elements = {}          # номер → элемент последнего снимка UIA
_snapshot = {"ts": 0, "hwnd": None, "title": ""}


# ------------------------------------------------------------ снимки экрана
def _mss():
    import mss
    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def monitors():
    with _mss() as s:
        return [dict(m) for m in s.monitors[1:]]


def grab(region=None):
    """Снимок области {left, top, width, height} (абсолютные пиксели) → (PIL.Image, область)."""
    from PIL import Image
    with _mss() as s:
        mon = region or s.monitors[1]
        shot = s.grab({k: int(mon[k]) for k in ("left", "top", "width", "height")})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, {k: int(mon[k]) for k in ("left", "top", "width", "height")}


def foreground_rect():
    """Рабочее окно агента (в фоновом режиме — не обязательно активное) и его границы."""
    import uiautomation as auto
    from . import bgui
    c = auto.ControlFromHandle(bgui.target())
    return c, bgui.rect(c.NativeWindowHandle)


def monitor_of(rect):
    """Монитор, на котором центр области (для снимка «весь экран» там, где работает пользователь)."""
    cx, cy = rect["left"] + rect["width"] / 2, rect["top"] + rect["height"] / 2
    mons = monitors()
    for m in mons:
        if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
            return m
    return mons[0]


def capture(target="window"):
    """target: window — рабочее окно агента (детальнее для модели зрения), screen — его монитор целиком.
    В фоновом режиме окно снимается через PrintWindow — видно, даже если его перекрыл пользователь."""
    from . import bgui
    _, wr = foreground_rect()
    if target == "screen" or wr["width"] < 50 or wr["height"] < 50:
        return grab(monitor_of(wr))
    if bgui.background():
        shot = bgui.capture()
        if shot:
            return shot
    m = monitor_of(wr)                       # окно может частично уходить за край монитора
    left, top = max(wr["left"], m["left"]), max(wr["top"], m["top"])
    right = min(wr["left"] + wr["width"], m["left"] + m["width"])
    bottom = min(wr["top"] + wr["height"], m["top"] + m["height"])
    return grab({"left": left, "top": top, "width": max(right - left, 1), "height": max(bottom - top, 1)})


def to_b64(img, max_side=1400):
    k = min(1.0, max_side / max(img.size))
    if k < 1:
        img = img.resize((int(img.size[0] * k), int(img.size[1] * k)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def diff(a, b, region):
    """Что изменилось между снимками одной области — в полном разрешении (смена короткой надписи
    на большом мониторе — доли процента, сжатие бы её потеряло). → (пикселей, доля, рамка или None)."""
    sa = np.asarray(a.convert("L"), dtype=np.int16)
    sb = np.asarray(b.convert("L"), dtype=np.int16)
    mask = np.abs(sa - sb) > 30
    n = int(mask.sum())
    if not n:
        return 0, 0.0, None
    ys, xs = np.nonzero(mask)
    box = (region["left"] + int(xs.min()), region["top"] + int(ys.min()),
           region["left"] + int(xs.max()), region["top"] + int(ys.max()))
    return n, n / mask.size, box


# ------------------------------------------------------------ UI Automation
def elements(query="", include_text=True):
    """Элементы активного окна (а также открытых меню и всплывающих списков) с точными границами.
    Нумерация сохраняется до следующего вызова — по номеру кликает click_element."""
    import uiautomation as auto
    from . import bgui
    win = auto.ControlFromHandle(bgui.target())
    t0 = time.time()
    found = []
    roots = [win]
    try:      # открытые меню и выпадающие списки — отдельные окна верхнего уровня
        for top in auto.GetRootControl().GetChildren():
            if top.ControlTypeName in ("MenuControl", "WindowControl") and top.ClassName in (
                    "#32768", "Xaml_WindowedPopupClass", "Popup") and not top.IsOffscreen:
                roots.append(top)
    except Exception:  # noqa: BLE001
        pass
    seen = set()
    for root in roots:
        for c, depth in auto.WalkControl(root, includeTop=False, maxDepth=25):
            if time.time() - t0 > WALK_SEC or len(found) >= MAX_ELEMENTS * 2:
                break
            try:
                ct = c.ControlTypeName
                if ct not in INTERACTIVE and not (include_text and ct in TEXTUAL):
                    continue
                if c.IsOffscreen:
                    continue
                r = c.BoundingRectangle
                if r.width() <= 2 or r.height() <= 2:
                    continue
                name = (c.Name or "").strip()
                if ct in TEXTUAL and not name:
                    continue
                key = (ct, name, r.left, r.top)
                if key in seen:
                    continue
                seen.add(key)
                value = ""
                if ct == "EditControl":
                    try:
                        value = (c.GetValuePattern().Value or "")[:80]
                    except Exception:  # noqa: BLE001
                        pass
                found.append({"type": ct, "name": name[:120], "value": value, "enabled": bool(c.IsEnabled),
                              "rect": (r.left, r.top, r.right, r.bottom), "aid": c.AutomationId or "",
                              "x": (r.left + r.right) // 2, "y": (r.top + r.bottom) // 2})
            except Exception:  # noqa: BLE001 — элемент исчез во время обхода
                continue
    if query:
        q = query.lower()
        words = [w for w in re.findall(r"\w+", q) if len(w) >= 2]
        found = [e for e in found if any(w in f"{e['name']} {e['value']} {e['aid']}".lower() for w in words)
                 or q in TYPE_RU.get(e["type"], "")]
    found = found[:MAX_ELEMENTS]
    with _lock:
        _elements.clear()
        for i, e in enumerate(found, 1):
            e["id"] = i
            _elements[i] = e
        _snapshot.update(ts=time.time(), hwnd=win.NativeWindowHandle, title=win.Name)
    return win.Name, found


def element(eid):
    with _lock:
        return _elements.get(int(eid)), dict(_snapshot)


def describe(e):
    kind = TYPE_RU.get(e["type"], e["type"].replace("Control", ""))
    name = f"«{e['name']}»" if e["name"] else "(без подписи)"
    val = f" = «{e['value']}»" if e["value"] else ""
    off = " [неактивна]" if not e["enabled"] else ""
    return f"#{e['id']} {kind} {name}{val}{off} — ({e['x']}, {e['y']})"


# ------------------------------------------------------------ модель зрения: найти элемент
LOCATE_JSON = {"type": "object", "required": ["found", "bbox", "label"], "properties": {
    "found": {"type": "boolean"},
    "bbox": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "integer", "minimum": 0, "maximum": 1000}},
    "label": {"type": "string", "maxLength": 80}}}


def locate(description, model, url, target="window"):
    """Найти на снимке описанный элемент моделью зрения. Модель отвечает рамкой в координатах 0..1000
    относительно изображения; в абсолютные пиксели экрана переводит код."""
    import requests
    from .gpu import vram
    img, region = capture(target)
    b64 = to_b64(img)
    prompt = (f"Это снимок {'окна программы' if target == 'window' else 'экрана'}. Найди: {description}.\n"
              "Ответь JSON: found — нашёл ли; bbox — рамка [x1, y1, x2, y2] в координатах 0..1000 относительно "
              "изображения (0,0 — левый верхний угол, 1000,1000 — правый нижний); label — что это (по-русски, кратко).")
    vram.prepare(model)
    body = {"model": model, "stream": False, "think": False, "format": LOCATE_JSON,
            "keep_alive": vram.keep_alive(model),
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            "options": vram.options(model, temperature=0, num_predict=200)}
    r = requests.post(f"{url}/api/chat", json=body, timeout=180)
    if r.status_code == 400 and "think" in r.text:
        body.pop("think")
        r = requests.post(f"{url}/api/chat", json=body, timeout=180)
    r.raise_for_status()
    data = json.loads(r.json()["message"]["content"])
    if not data.get("found"):
        return None, img, region
    x1, y1, x2, y2 = data["bbox"]
    sx, sy = region["width"] / 1000, region["height"] / 1000
    box = (region["left"] + int(min(x1, x2) * sx), region["top"] + int(min(y1, y2) * sy),
           region["left"] + int(max(x1, x2) * sx), region["top"] + int(max(y1, y2) * sy))
    return {"label": data.get("label", ""), "rect": box, "x": (box[0] + box[2]) // 2,
            "y": (box[1] + box[3]) // 2}, img, region


# ------------------------------------------------------------ действие и проверка
def around_window(margin=250):
    """Активное окно с запасом по краям (выпадающие меню и подсказки выходят за его границы),
    в пределах его монитора: изменения в других программах (часы, терминал) не мешают проверке."""
    _, wr = foreground_rect()
    m = monitor_of(wr)
    left, top = max(wr["left"] - margin, m["left"]), max(wr["top"] - margin, m["top"])
    right = min(wr["left"] + wr["width"] + margin, m["left"] + m["width"])
    bottom = min(wr["top"] + wr["height"] + margin, m["top"] + m["height"])
    return {"left": left, "top": top, "width": max(right - left, 1), "height": max(bottom - top, 1)}


def _shot_for_verify():
    """Снимок для проверки «что изменилось»: в фоновом режиме — само окно (PrintWindow), иначе экран."""
    from . import bgui
    if bgui.background():
        shot = bgui.capture()
        if shot:
            return shot
    return grab(around_window())


def act_and_verify(action, wait=0.8):
    """Выполнить действие и сказать, что изменилось в рабочем окне и какое окно у агента рабочее."""
    from . import bgui, winctl
    before_title = bgui.title()
    before_wins = set(winctl.windows())
    pid = winctl._pid(bgui.target())
    before, region = _shot_for_verify()
    action()
    time.sleep(wait)
    # действие открыло новое окно той же программы (диалог, вкладка-окно) — дальше работаем в нём
    opened = [h for h in winctl.windows() if h not in before_wins and winctl._pid(h) == pid]
    if opened and bgui.background():
        bgui.set_target(opened[0], "открылось после действия")
    after, region2 = _shot_for_verify()
    if after.size != before.size:        # окно сменило размер — сравнивать нечего, явно что-то произошло
        n, ratio, box = 10 ** 6, 1.0, (region2["left"], region2["top"], region2["left"] + region2["width"],
                                       region2["top"] + region2["height"])
    else:
        n, ratio, box = diff(before, after, region)
    after_title = bgui.title()
    if n < 30:
        parts = ["на экране ничего не изменилось"]
    else:
        parts = [f"изменилась область {box[0]},{box[1]}–{box[2]},{box[3]} "
                 f"({'крупно: ' if ratio > 0.15 else ''}{ratio * 100:.1f}% окна)"]
    if after_title != before_title:
        parts.append(f"рабочее окно сменилось: «{before_title}» → «{after_title}»")
    else:
        parts.append(f"рабочее окно: «{after_title}»")
    if n < 30 and after_title == before_title:
        parts.append("ПОХОЖЕ, КЛИК НИЧЕГО НЕ СДЕЛАЛ — проверь элемент (screen_elements / look_at_screen)")
    else:
        parts.append("чтобы увидеть результат — screen_elements или look_at_screen")
    return "; ".join(parts)
