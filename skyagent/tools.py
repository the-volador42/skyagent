"""Инструменты агента: командная строка, Python, клавиатура, мышь, браузер."""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser

import requests

MAX_OUTPUT = 6000
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def _cut(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text.strip()
    if len(text) > limit:
        return text[:limit] + f"\n...[обрезано, всего {len(text)} символов]"
    return text or "(пустой вывод)"


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _gui():
    try:
        import pyautogui
    except ImportError as e:
        raise RuntimeError("Не установлен pyautogui: pip install pyautogui") from e
    pyautogui.FAILSAFE = True  # увести мышь в левый верхний угол = аварийная остановка
    return pyautogui


# ---------------------------------------------------------------- процессы и фоновые задачи
# Агент запускает инструменты в потоках-«задачах». Процесс, запущенный задачей,
# регистрируется здесь, чтобы его можно было остановить (stop_job).
_local = threading.local()
PROCS = {}


def set_job(job_id):
    _local.job = job_id


def kill_tree(p):
    if p.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        p.kill()


def kill_job(job_id) -> bool:
    p = PROCS.get(job_id)
    if not p:
        return False
    kill_tree(p)
    return True


def _run_proc(args, shell, timeout, env=None, cwd=None):
    p = subprocess.Popen(args, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, env=env, cwd=cwd)
    jid = getattr(_local, "job", None)
    if jid is not None:
        PROCS[jid] = p
    timed = False
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed = True
        kill_tree(p)
        out, err = p.communicate()
    finally:
        PROCS.pop(jid, None)
    res = f"[код возврата {p.returncode}]\n{_decode(out)}"
    if err.strip():
        res += f"\n[stderr]\n{_decode(err)}"
    if timed:
        res = (f"ТАЙМАУТ {timeout} c — процесс остановлен (GUI-программы запускай через start). "
               "Частичный вывод:\n" + res)
    return _cut(res)


# ---------------------------------------------------------------- инструменты

def run_command(command: str, shell: str = "cmd", timeout: int = 180) -> str:
    if shell == "powershell":
        args = ["powershell", "-NoProfile", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
        return _run_proc(args, False, timeout, cwd=os.path.expanduser("~"))
    args = f"chcp 65001>nul & {command}" if os.name == "nt" else command
    return _run_proc(args, True, timeout, cwd=os.path.expanduser("~"))


def run_python(code: str, timeout: int = 600) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        return _run_proc([sys.executable, path], False, timeout, env=env)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _win():
    if os.name != "nt":
        return None
    from . import winctl
    return winctl


def press_keys(keys: str, presses: int = 1) -> str:
    combo = [k for k in re.split(r"\s*\+\s*", keys.strip().lower()) if k]
    if not combo:
        return "Ошибка: не указаны клавиши"
    w = _win()
    if not w:
        for _ in range(max(1, int(presses))):
            _gui().hotkey(*combo)
            time.sleep(0.12)
        return f"Нажато: {'+'.join(combo)} ×{presses}"
    from . import bgui
    # системные сочетания (win+…, alt+tab) — про весь компьютер, а не про рабочее окно агента
    system = combo[0] in ("win", "windows", "winleft", "lwin", "cmd") or combo == ["alt", "tab"]
    with (_nullcontext() if system else bgui.borrow()):
        for _ in range(max(1, int(presses))):
            w.hotkey(combo)
            time.sleep(0.12)
        time.sleep(0.3)
    return f"Нажато: {'+'.join(combo)} ×{presses} в окне «{bgui.title()}»"


def type_text(text: str, enter: bool = False) -> str:
    w = _win()
    if w:
        from . import bgui
        target = bgui.title()
        with bgui.borrow():          # фокус — на время ввода, потом окно пользователя возвращается
            w.type_unicode(text)
            if enter:
                w.hotkey(["enter"])
        return f"Напечатано {len(text)} символов в окно «{target}»" + (" + Enter" if enter else "")
    gui = _gui()
    gui.write(text, interval=0.015)
    if enter:
        gui.press("enter")
    return f"Напечатано {len(text)} символов"


def _nullcontext():
    from contextlib import nullcontext
    return nullcontext()


def _real_click(x, y, button="left", clicks=1):
    """Настоящий клик мышью: в фоновом режиме — с «одолженным» на мгновение фокусом."""
    from . import bgui
    gui = _gui()
    with bgui.borrow():
        gui.click(x=int(x), y=int(y), button=button, clicks=int(clicks), interval=0.1)


def _outside_active(x, y):
    """Точка вне активного окна (с небольшим запасом на выпадающие меню) — клик туда не делаем:
    иначе промах модели зрения попадает в чужую программу на заднем плане."""
    from . import screen
    win, r = screen.foreground_rect()
    pad = 250
    inside = (r["left"] - pad <= x <= r["left"] + r["width"] + pad and r["top"] - pad <= y <= r["top"] + r["height"] + pad)
    return None if inside else win.Name


def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    from . import screen
    other = _outside_active(int(x), int(y))
    if other is not None:
        return (f"ОТМЕНЕНО: точка ({x}, {y}) вне активного окна «{other}» — клик попал бы в другую программу. "
                "Сначала focus_window нужного окна, потом screen_elements / click_on_screen.")
    res = screen.act_and_verify(lambda: _real_click(x, y, button, clicks))
    return f"Клик {button} ×{clicks} в ({x}, {y}): {res}"


# ---------------------------------------------------------------- GUI: элементы, поиск, клик с проверкой
def screen_elements(query: str = "", text: bool = True) -> str:
    from . import screen
    title, found = screen.elements(query, include_text=bool(text))
    note = ""
    if not found and query:          # фильтр промахнулся — полный список полезнее пустоты
        title, found = screen.elements("", include_text=bool(text))
        note = f"По запросу «{query}» ничего — вот все элементы окна.\n"
    if not found:
        return (f"В окне «{title}» элементов UI Automation не найдено. Возможно, это игра или нестандартный "
                "интерфейс — используй click_on_screen / find_on_screen.")
    lines = [screen.describe(e) for e in found]
    return (f"{note}Окно «{title}». Элементов: {len(found)} (номер — для click_element; в скобках — центр на экране):\n"
            + "\n".join(lines))


def _fresh_element(eid):
    """Элемент из последнего списка; если окно сменилось или список устарел — найти его заново по типу и имени."""
    from . import bgui, screen
    e, snap = screen.element(eid)
    if not e:
        return None, "Нет элемента с таким номером — сначала вызови screen_elements."
    if snap["hwnd"] == bgui.target() and time.time() - snap["ts"] < 30:
        return e, None
    _, found = screen.elements(e["name"] or "")
    same = [x for x in found if x["type"] == e["type"] and x["name"] == e["name"]]
    if not same:
        return None, "Окно изменилось, элемент не найден — вызови screen_elements заново."
    return same[0], None


def click_element(id: int, button: str = "left", double: bool = False) -> str:
    from . import screen
    e, err = _fresh_element(id)
    if err:
        return err
    if not e["enabled"]:
        return f"{screen.describe(e)} — элемент неактивен, клик не сработает."
    from . import bgui
    how = {}

    def act():
        # фоновый режим: нажать элемент через UI Automation — мышь и фокус пользователя не трогаем
        if bgui.background() and button == "left":
            ctrl = bgui.find_control(e)
            how["uia"] = bgui.uia_click(ctrl, double) if ctrl else None
            if how["uia"]:
                return
        _real_click(e["x"], e["y"], button, 2 if double else 1)

    res = screen.act_and_verify(act)
    way = f" (через UI Automation: {how['uia']}, мышь не двигалась)" if how.get("uia") else ""
    return f"Клик по {screen.describe(e)}{way}: {res}"


def fill_element(id: int, text: str, enter: bool = False) -> str:
    """Вписать текст в поле из screen_elements. В фоновом режиме — через UI Automation, без клавиатуры."""
    from . import bgui, screen
    e, err = _fresh_element(id)
    if err:
        return err
    ctrl = bgui.find_control(e) if bgui.background() else None
    filled = bool(ctrl) and bgui.uia_set_text(ctrl, text)
    if filled and not enter:
        return f"В {screen.describe(e)} вписано {len(text)} символов (через UI Automation, клавиатура не нужна)."
    w = _win()
    with bgui.borrow():
        _gui().click(x=e["x"], y=e["y"])
        time.sleep(0.15)
        if not filled:
            w.hotkey(["ctrl", "a"])
            w.type_unicode(text)
        if enter:
            w.hotkey(["enter"])
    return f"В {screen.describe(e)} вписано {len(text)} символов" + (" + Enter" if enter else "")


def _vision_model():
    from .core import pick_vision_model
    return VISION["model"] or pick_vision_model()


def find_on_screen(description: str, where: str = "window") -> str:
    from . import screen
    from .core import OLLAMA_URL
    model = _vision_model()
    if not model:
        return "ОШИБКА: нет модели со зрением (ollama pull qwen3-vl:4b-instruct)."
    try:
        hit, img, region = screen.locate(description, model, OLLAMA_URL, "screen" if where == "screen" else "window")
    except Exception as e:  # noqa: BLE001
        return f"ОШИБКА модели зрения {model}: {e}"
    LAST_IMAGE["b64"] = screen.to_b64(img, 900)
    if not hit:
        return f"«{description}» не найдено на {'экране' if where == 'screen' else 'активном окне'} (модель {model})."
    return (f"Найдено «{hit['label']}»: центр ({hit['x']}, {hit['y']}), рамка {hit['rect']} — абсолютные координаты, "
            f"для mouse_click подставляй как есть. Модель зрения {model}; координаты от модели — приблизительные, "
            "после клика проверь результат.")


def click_on_screen(description: str, button: str = "left", double: bool = False, where: str = "window") -> str:
    from . import screen
    from .core import OLLAMA_URL
    model = _vision_model()
    if not model:
        return "ОШИБКА: нет модели со зрением (ollama pull qwen3-vl:4b-instruct)."
    try:
        hit, img, _ = screen.locate(description, model, OLLAMA_URL, "screen" if where == "screen" else "window")
    except Exception as e:  # noqa: BLE001
        return f"ОШИБКА модели зрения {model}: {e}"
    LAST_IMAGE["b64"] = screen.to_b64(img, 900)
    if not hit:
        return f"«{description}» не найдено — клик не сделан. Попробуй screen_elements или опиши иначе."
    other = _outside_active(hit["x"], hit["y"])
    if other is not None:
        return (f"ОТМЕНЕНО: «{hit['label']}» найдено в ({hit['x']}, {hit['y']}), это вне активного окна «{other}». "
                "Сначала focus_window нужного окна.")
    res = screen.act_and_verify(lambda: _real_click(hit["x"], hit["y"], button, 2 if double else 1))
    return f"Клик по «{hit['label']}» в ({hit['x']}, {hit['y']}) (нашла модель {model}): {res}"


def screen_info() -> str:
    gui = _gui()
    w, h = gui.size()
    x, y = gui.position()
    res = f"Экран {w}x{h}, курсор в ({x}, {y})"
    if _win():
        from . import bgui
        res += (f", активное окно пользователя: «{_win().active_title()}», "
                f"рабочее окно агента: {bgui.describe_target()}")
    return res


def list_windows() -> str:
    w = _win()
    if not w:
        return "Доступно только в Windows"
    from . import bgui
    titles = sorted(set(w.windows().values()))
    return (f"Активное (у пользователя): «{w.active_title()}»\nРабочее окно агента: {bgui.describe_target()}\n"
            "Открытые окна:\n" + "\n".join(f"- {t}" for t in titles))


def focus_window(title: str, front: bool = False) -> str:
    w = _win()
    if not w:
        return "Доступно только в Windows"
    from . import bgui
    if bgui.background() and not front:
        match = [(h, t) for h, t in w.windows().items() if title.lower() in t.lower() and not bgui._own(h)]
        if not match:
            return f"ОШИБКА: окно с «{title}» в заголовке не найдено. Вызови list_windows."
        bgui.set_target(match[0][0], "focus_window")
        return (f"Окно «{match[0][1]}» — теперь рабочее окно агента. На передний план НЕ выносилось: "
                "пользователь продолжает работать, а screen_elements / click_element / fill_element "
                "работают с этим окном.")
    got = w.focus(title)
    if got and not got.startswith("!"):
        bgui.set_target(w.user32.GetForegroundWindow(), "focus_window")
    if got is None:
        return f"ОШИБКА: окно с «{title}» в заголовке не найдено. Вызови list_windows."
    if got.startswith("!"):
        return f"Окно «{got[1:]}» найдено, но Windows не дала перевести на него фокус."
    return f"Активировано окно «{got}»"


def _start_menu_apps():
    roots = [os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.path.expanduser("~"), "Desktop"),
             os.path.join(os.environ.get("PUBLIC", ""), "Desktop")]
    for root in roots:
        for dp, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".lnk", ".url")):
                    yield os.path.splitext(f)[0], os.path.join(dp, f)


def _app_paths(name):
    import winreg
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                return winreg.QueryValue(k, None).strip('"')
        except OSError:
            pass
    return None


def _store_app(name):
    """Приложения из Microsoft Store и меню Пуск (Telegram, Spotify, Калькулятор...)."""
    ps = ("Get-StartApps | Where-Object { $_.Name -like '*" + name.replace("'", "") +
          "*' } | Select-Object -First 1 | ForEach-Object { $_.Name + '|' + $_.AppID }")
    p = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + ps],
                       capture_output=True, timeout=20)
    out = _decode(p.stdout).strip()
    return out.split("|", 1) if "|" in out else None


BROWSER_WORDS = {"browser", "браузер", "default browser", "веб-браузер", "интернет"}


def open_app(target: str) -> str:
    target = target.strip().strip('"')
    if os.name != "nt":
        subprocess.Popen(["xdg-open", target])
        return f"Отправлена команда открыть: {target}"
    w = _win()
    before = set(w.windows())
    how = None
    try:
        path = os.path.expandvars(target)
        if target.lower() in BROWSER_WORDS:
            webbrowser.open("https://www.google.com", new=2)
            how = "браузер по умолчанию"
        elif os.path.exists(path):
            os.startfile(path)
            how = path
        else:
            name = target.lower().removesuffix(".exe")
            exe = shutil.which(name) or _app_paths(name)
            if exe:
                subprocess.Popen([exe], close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
                how = exe
            else:
                links = [(t, p) for t, p in _start_menu_apps() if name in t.lower()]
                if links:
                    t, p = min(links, key=lambda x: len(x[0]))
                    os.startfile(p)
                    how = f"ярлык «{t}»"
                else:
                    app = _store_app(target)
                    if app:
                        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + app[1]])
                        how = f"приложение «{app[0]}»"
    except Exception as e:
        return f"ОШИБКА запуска «{target}»: {e}. Программа НЕ открыта."
    if not how:
        return (f"ОШИБКА: программа «{target}» не найдена на компьютере (нет в PATH, реестре, "
                "меню Пуск и Microsoft Store). Программа НЕ открыта. Попробуй другое название "
                "или найди exe через run_command (where / dir).")
    for _ in range(40):  # ждём появления окна до 10 c
        time.sleep(0.25)
        new = {h: t for h, t in w.windows().items() if h not in before}
        if new:
            from . import bgui
            bgui.set_target(next(iter(new)), "open_app")
            return f"Запущено ({how}). Появилось окно: «{list(new.values())[0]}» — теперь это рабочее окно агента"
    key = os.path.basename(target).lower().split(".")[0]
    same = [t for t in w.windows().values() if key and key in t.lower()]
    if same:
        return f"Команда запуска отправлена ({how}). Новое окно не появилось, но уже открыто: «{same[0]}»"
    return (f"Команда запуска отправлена ({how}), но окно за 10 c НЕ появилось. "
            "Нельзя утверждать, что программа открылась — проверь через list_windows.")


def open_url(url: str) -> str:
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url
    webbrowser.open(url, new=2)
    return f"Открыто в браузере: {url}"


def _search_ddgs(query, n):
    from ddgs import DDGS
    res = DDGS(timeout=8).text(query, region="ru-ru", max_results=n)
    return [(r.get("title", ""), r.get("href", ""), r.get("body", "")) for r in res]


def _search_ddg_html(query, n):
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "ru-ru"}, headers=UA, timeout=8)
    clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()  # noqa: E731
    items = []
    for block in r.text.split('class="result__body"')[1:n + 1]:
        a = re.search(r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        link = html.unescape(a.group(1))
        m = re.search(r"uddg=([^&]+)", link)
        if m:
            link = urllib.parse.unquote(m.group(1))
        sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        items.append((clean(a.group(2)), link, clean(sn.group(1)) if sn else ""))
    return items


def _search(query: str, n: int = 6):
    """Несколько поисковиков по очереди, у каждого короткий таймаут: медленная сеть не должна
    растягивать поиск дольше, чем агент его ждёт (иначе он уходит в фон и агент теряется)."""
    errors = []
    for name, fn in (("ddgs", _search_ddgs), ("bing", _search_bing), ("duckduckgo", _search_ddg_html)):
        try:
            items = fn(query, n)
            if items:
                return items
            errors.append(f"{name}: пусто")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}")
    raise RuntimeError("; ".join(errors))


def _search_bing(query, n):
    import base64
    r = requests.get("https://www.bing.com/search", params={"q": query, "setlang": "ru"},
                     headers=UA, timeout=8)
    clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    items = []
    for block in r.text.split('class="b_algo"')[1:n + 1]:
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not a:
            continue
        link = html.unescape(a.group(1))
        m = re.search(r"[?&]u=a1([^&]+)", link)
        if m:
            b = m.group(1)
            link = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8", "replace")
        sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        items.append((clean(a.group(2)), link, clean(sn.group(1)) if sn else ""))
    return items


def web_search(query: str, open_browser: bool = False) -> str:
    if open_browser:
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query), new=2)
    head = "Окно браузера с поиском Google открыто. " if open_browser else ""
    try:
        items = _search(query)
    except Exception as e:
        return head + (f"ОШИБКА: поиск не ответил ({e}) — похоже, медленный интернет. Не повторяй тот же запрос: "
                       "открой поиск в своём браузере — browser_open('https://duckduckgo.com/?q=…') — "
                       "или сразу нужный сайт.")
    if not items:
        return head + "Текстовых результатов не найдено."
    lines = [f"{i}. {t}\n   {u}\n   {s}" for i, (t, u, s) in enumerate(items, 1)]
    return (head + "Ниже результаты поиска, которые видишь ТЫ. Ответь на вопрос пользователя по ним "
            "(цифры, факты). Если в описаниях нет ответа — вызови fetch_page для нужной ссылки.\n\n"
            + "\n".join(lines))


def fetch_page(url: str) -> str:
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url
    r = requests.get(url, headers=UA, timeout=20)
    if "json" in r.headers.get("content-type", ""):
        # API: компактный JSON без экранированных «\/» — модели проще переписать ссылку без ошибок
        try:
            return _cut(json.dumps(r.json(), ensure_ascii=False, separators=(",", ":")), 6000)
        except ValueError:
            pass
    r.encoding = r.apparent_encoding or r.encoding
    text = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", r.text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text)
    return _cut(text, 5000)


def wait(seconds: float) -> str:
    time.sleep(min(float(seconds), 30))
    return f"Подождал {seconds} c"


# ---------------------------------------------------------------- файлы

WORKSPACE = os.path.join(os.path.expanduser("~"), "SkyAgent")


def _path(path: str) -> str:
    path = os.path.expandvars(os.path.expanduser(path.strip().strip('"')))
    if not os.path.isabs(path):
        path = os.path.join(WORKSPACE, path)
    return os.path.normpath(path)


def write_file(path: str, content: str, append: bool = False) -> str:
    full = _path(path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "a" if append else "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return f"Файл {'дополнен' if append else 'сохранён'}: {full} ({os.path.getsize(full)} байт)"


def read_file(path: str, max_chars: int = 8000) -> str:
    full = _path(path)
    if not os.path.isfile(full):
        return f"ОШИБКА: файл не найден: {full}"
    with open(full, "rb") as f:
        data = f.read(max_chars * 4)
    return f"{full}:\n" + _cut(_decode(data), max_chars)


def list_dir(path: str = "") -> str:
    full = _path(path) if path else WORKSPACE
    if not os.path.isdir(full):
        if path:
            return f"ОШИБКА: папка не найдена: {full}"
        os.makedirs(full, exist_ok=True)
    rows = []
    for e in sorted(os.scandir(full), key=lambda e: (not e.is_dir(), e.name.lower()))[:300]:
        rows.append(f"[папка] {e.name}" if e.is_dir() else f"{e.name}  ({e.stat().st_size} байт)")
    return f"{full}:\n" + ("\n".join(rows) or "(пусто)")


RUNNERS = {".py": lambda p: [sys.executable, p], ".pyw": lambda p: [sys.executable, p],
           ".bat": lambda p: ["cmd", "/c", p], ".cmd": lambda p: ["cmd", "/c", p],
           ".ps1": lambda p: ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", p],
           ".js": lambda p: ["node", p], ".exe": lambda p: [p]}


def run_file(path: str, args: str = "", wait: bool = True, timeout: int = 600) -> str:
    full = _path(path)
    if not os.path.isfile(full):
        return f"ОШИБКА: файл не найден: {full}"
    runner = RUNNERS.get(os.path.splitext(full)[1].lower())
    if not runner:  # html, txt, картинки — программой по умолчанию
        if os.name == "nt":
            os.startfile(full)
        else:
            subprocess.Popen(["xdg-open", full])
        return f"Открыто программой по умолчанию: {full}"
    cmd = runner(full) + (args.split() if args else [])
    cwd = os.path.dirname(full)
    if not wait:  # оконные и долгие программы — в отдельной консоли
        subprocess.Popen(cmd, cwd=cwd, creationflags=0x00000010 if os.name == "nt" else 0)
        return f"Запущено в отдельном окне: {' '.join(cmd)}"
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return _run_proc(cmd, False, timeout, env=env, cwd=cwd)


# ---------------------------------------------------------------- зрение

VISION = {"model": None}     # модель для распознавания экрана (задаёт интерфейс)
LAST_IMAGE = {"b64": None}   # последний скриншот — интерфейс показывает миниатюру


def _is_blank(b64: str) -> bool:
    import base64
    import io
    from PIL import Image, ImageStat
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("L")
    st = ImageStat.Stat(img)
    return st.mean[0] < 3 and st.stddev[0] < 3


def describe_image(b64: str, prompt: str) -> str:
    """Описание картинки моделью зрения (текст без координат) или строка с ОШИБКОЙ."""
    from .core import OLLAMA_URL, pick_vision_model
    from .gpu import vram
    # рабочая модель со зрением (gemma4) смотрит сама: вторую модель грузить не нужно
    own = vram.primary if vram.primary and "vision" in vram.capabilities(vram.primary) else None
    model = VISION["model"] or own or pick_vision_model()
    if not model:
        return ("ОШИБКА: нет модели со зрением. Установи, например: ollama pull qwen3-vl:4b "
                "(или qwen3-vl:2b — полегче).")
    vram.prepare(model)       # модель зрения и агент вместе не помещаются — освободить место
    body = {"model": model, "stream": False, "think": False, "keep_alive": vram.keep_alive(model),
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            # лимит и штраф за повторы — маленькие VL-модели иногда зацикливаются на списках
            "options": vram.options(model, temperature=0.1, num_predict=700, repeat_penalty=1.15)}
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=300)
        if r.status_code == 400 and "think" in r.text:
            body.pop("think")
            r = requests.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=300)
        r.raise_for_status()
        msg = r.json().get("message", {})
        text = re.sub(r"<think>.*?(</think>|$)", "", msg.get("content", ""), flags=re.S).strip()
        if not text and "think" in body:
            # thinking-модели иногда тратят весь ответ на размышления — повторяем в их родном режиме
            body.pop("think")
            msg = requests.post(f"{OLLAMA_URL}/api/chat", json=body, timeout=300).json().get("message", {})
            text = msg.get("content", "").strip()
        if not text:
            text = (msg.get("thinking") or "").strip()[-2500:]
        if not text:
            return f"ОШИБКА: модель {model} вернула пустой ответ. Попробуй ещё раз или выбери другую модель зрения."
    except Exception as e:
        return f"ОШИБКА распознавания картинки моделью {model}: {e}"
    # Модель зрения в свободном тексте путает системы координат — координат отсюда не берём совсем
    text = re.sub(r"\(\s*(x\s*=\s*)?\d{2,4}\s*,\s*(y\s*=\s*)?\d{2,4}\s*\)", "", text)
    return f"[модель зрения {model}]\n{text}"


def look_at_screen(question: str = "", where: str = "auto") -> str:
    """where: screen — монитор целиком (что видит пользователь), window — рабочее окно агента
    (снимается, даже если перекрыто), auto — окно агента, если он сейчас с ним работает, иначе экран."""
    from . import bgui, screen
    if where not in ("screen", "window"):
        where = "window" if bgui.background() and bgui.recent() else "screen"
    img, region = screen.capture(where)
    b64 = screen.to_b64(img)
    LAST_IMAGE["b64"] = b64
    if _is_blank(b64):
        return ("Скриншот полностью чёрный: экран заблокирован, выключен или открыт защищённый рабочий стол "
                "(UAC/экран входа). Увидеть содержимое сейчас нельзя — сообщи об этом пользователю.")
    import platform
    what = (f"окна «{bgui.title()}»" if where == "window" else "экрана")
    prompt = (f"Это скриншот {what} компьютера с ОС {platform.system()} {platform.release()}. Опиши по-русски: "
              "какие окна открыты, какое активно, что в нём видно, важные кнопки, поля, текст и ошибки. Точно и кратко.")
    if question:
        prompt += f"\nОбязательно ответь: {question}"
    prompt += ("\nПоложение элементов описывай словами (слева вверху, под полем ввода…), координаты НЕ пиши.")
    text = describe_image(b64, prompt)
    if text.startswith("ОШИБКА"):
        return text
    shot = f"рабочее окно агента «{bgui.title()}»" if where == "window" else f"монитор {region['width']}x{region['height']}"
    return (f"[{shot}. Это описание БЕЗ координат: чтобы кликнуть — screen_elements + click_element, "
            f"а если элемента там нет — click_on_screen]\n{text}")


def look_at_image(path: str, question: str = "") -> str:
    """Посмотреть на картинку-файл моделью зрения: что на ней (обои, скачанная картинка, график)."""
    import base64
    import io
    from PIL import Image
    full = _path(path)
    if not os.path.isfile(full):
        return f"ОШИБКА: файл не найден: {full}"
    try:
        img = Image.open(full).convert("RGB")
    except Exception as e:  # noqa: BLE001
        return f"ОШИБКА: это не картинка или файл повреждён ({type(e).__name__}: {e})"
    w, h = img.size
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    LAST_IMAGE["b64"] = b64
    prompt = "Опиши по-русски, что изображено на картинке: сюжет, тематика, цвета, настроение. Кратко и точно."
    if question:
        prompt += f"\nОбязательно ответь: {question}"
    text = describe_image(b64, prompt)
    return text if text.startswith("ОШИБКА") else f"[картинка {full}, {w}x{h}]\n{text}"


def wallpaper_path() -> str:
    """Текущие обои рабочего стола (путь к файлу) — для проверки после смены."""
    if os.name != "nt":
        return "ОШИБКА: только для Windows"
    import ctypes
    buf = ctypes.create_unicode_buffer(520)
    ctypes.windll.user32.SystemParametersInfoW(0x0073, 520, buf, 0)     # SPI_GETDESKWALLPAPER
    p = buf.value
    if not p:
        return "Обои не заданы (сплошной цвет или слайд-шоу)."
    return f"Текущие обои: {p}" + ("" if os.path.isfile(p) else " (ВНИМАНИЕ: файла по этому пути нет)")


# ---------------------------------------------------------------- описание для модели

def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


S = lambda d: {"type": "string", "description": d}
I = lambda d: {"type": "integer", "description": d}
B = lambda d: {"type": "boolean", "description": d}

TOOL_SCHEMAS = [
    _fn("run_command", "ГЛАВНЫЙ инструмент. Выполнить команду в cmd или PowerShell и получить вывод. "
        "Всё системное делай им: выключить ПК — 'shutdown /s /t 0', перезагрузка — 'shutdown /r /t 0', "
        "отмена выключения — 'shutdown /a', сон — 'rundll32.exe powrprof.dll,SetSuspendState 0,1,0', "
        "блокировка — 'rundll32.exe user32.dll,LockWorkStation', закрыть программу — "
        "'taskkill /im имя.exe /f', список процессов — 'tasklist'. GUI-программы запускай через 'start'.",
        {"command": S("Команда"), "shell": S("'cmd' (по умолчанию) или 'powershell'"),
         "timeout": I("Таймаут в секундах, по умолчанию 180")}, ["command"]),
    _fn("run_python", "Выполнить Python-код в интерпретаторе. Результат — то, что напечатано через print.",
        {"code": S("Полный код на Python")}, ["code"]),
    _fn("press_keys", "Нажать клавишу или сочетание в рабочем окне агента ('enter', 'ctrl+c', 'win+d', 'alt+tab'). "
        "Только если пользователь прямо просит нажать клавиши или нужно действие внутри открытого окна. "
        "НЕ используй для системных задач (выключение, перезагрузка, сон, закрытие программ) — для них "
        "run_command. Alt+F4 лишь закрывает активное окно и НЕ выключает компьютер.",
        {"keys": S("Клавиши через +"), "presses": I("Сколько раз нажать")}, ["keys"]),
    _fn("type_text", "Напечатать текст в рабочее окно агента там, где курсор (кириллица поддерживается). "
        "Для полей из screen_elements лучше fill_element — он не трогает клавиатуру пользователя.",
        {"text": S("Текст"), "enter": B("Нажать Enter после ввода")}, ["text"]),
    _fn("mouse_click", "Кликнуть мышью по координатам экрана.",
        {"x": I("X"), "y": I("Y"), "button": S("left/right/middle"), "clicks": I("Число кликов")},
        ["x", "y"]),
    _fn("screen_info", "Узнать разрешение экрана и позицию курсора.", {}, []),
    _fn("open_app", "Открыть программу, файл или папку: notepad, calc, chrome, telegram, 'браузер', "
        "C:\\path. Ищет программу в PATH, реестре, меню Пуск и Store и проверяет, что окно появилось.",
        {"target": S("Что открыть")}, ["target"]),
    _fn("list_windows", "Список открытых окон и активное окно — чтобы проверить, что программа открылась.", {}, []),
    _fn("focus_window", "Выбрать рабочее окно агента по части заголовка: дальше screen_elements, click_element, "
        "fill_element, type_text работают с ним. На передний план НЕ выносит (пользователь не отвлекается); "
        "front=true — вывести на передний план (только если пользователь просит показать/переключить окно).",
        {"title": S("Часть заголовка окна"), "front": B("Вывести окно на передний план")}, ["title"]),
    _fn("web_search", "Найти в интернете: возвращает список результатов текстом.",
        {"query": S("Поисковый запрос"),
         "open_browser": B("Ещё и открыть поиск в браузере ПОЛЬЗОВАТЕЛЯ (только если он просит; по умолчанию нет)")},
        ["query"]),
    _fn("open_url", "Открыть адрес в браузере ПОЛЬЗОВАТЕЛЯ — чтобы показать ему страницу. Для своей работы "
        "с сайтами (кликать, скачивать) — browser_open.", {"url": S("URL")}, ["url"]),
    _fn("fetch_page", "Скачать страницу и вернуть её текст (чтобы прочитать содержимое сайта).",
        {"url": S("URL")}, ["url"]),
    _fn("wait", "Подождать N секунд (например, пока откроется окно).",
        {"seconds": I("Секунды, максимум 30")}, ["seconds"]),
    _fn("write_file", "Создать или перезаписать файл (код, текст, скрипт). Относительный путь — внутри "
        "рабочей папки ~/SkyAgent. Пиши ПОЛНОЕ содержимое файла.",
        {"path": S("Путь: 'hello.py' или полный путь"), "content": S("Полное содержимое"),
         "append": B("Дописать в конец вместо перезаписи")}, ["path", "content"]),
    _fn("read_file", "Прочитать текстовый файл.", {"path": S("Путь к файлу")}, ["path"]),
    _fn("list_dir", "Показать содержимое папки (по умолчанию — рабочая папка ~/SkyAgent).",
        {"path": S("Путь к папке")}, []),
    _fn("run_file", "Запустить файл: .py, .bat, .cmd, .ps1, .js, .exe и вернуть вывод; html, txt, "
        "картинки откроются программой по умолчанию.",
        {"path": S("Путь к файлу"), "args": S("Аргументы командной строки"),
         "wait": B("Ждать завершения и вернуть вывод (по умолчанию да). false — для оконных/долгих программ")},
        ["path"]),
    _fn("look_at_screen", "Сделать скриншот и получить описание (окна, кнопки, текст, ошибки). Используй, "
        "чтобы понять общую картину и проверить результат действий. Для кнопок и полей точнее screen_elements.",
        {"question": S("Что именно найти или проверить"),
         "where": S("'window' — рабочее окно агента (видно, даже если его перекрыли), 'screen' — весь монитор, "
                    "как его видит пользователь; по умолчанию — окно, если агент с ним работает")}, []),
    _fn("look_at_image", "Посмотреть на картинку-файл (обои, скачанное изображение, график) моделью зрения: что "
        "на ней и какая тематика. Для проверки результата: та ли картинка, что просил пользователь.",
        {"path": S("Путь к картинке"), "question": S("Что проверить (например, 'это горы на закате?')")}, ["path"]),
    _fn("wallpaper_path", "Какие обои сейчас стоят на рабочем столе (путь к файлу). Проверка после смены обоев.",
        {}, []),
    _fn("screen_elements", "ТОЧНЫЙ список элементов рабочего окна агента через UI Automation: кнопки, поля, пункты "
        "меню, вкладки, ссылки с номерами и точными координатами. Для кнопок и полей — первым делом, до "
        "look_at_screen. Открытые меню и выпадающие списки тоже видны.",
        {"query": S("Фильтр по названию (например 'Сохранить'); пусто — всё"),
         "text": B("Включать текстовые надписи (по умолчанию да)")}, []),
    _fn("click_element", "Кликнуть по элементу из screen_elements по его номеру и узнать, что изменилось. "
        "Обычный клик идёт через UI Automation — мышь пользователя не двигается.",
        {"id": I("Номер элемента из screen_elements"), "button": S("left/right"),
         "double": B("Двойной клик")}, ["id"]),
    _fn("fill_element", "Вписать текст в поле ввода из screen_elements по номеру (заменяет содержимое поля). "
        "Без клавиатуры и мыши — пользователь не замечает.",
        {"id": I("Номер поля из screen_elements"), "text": S("Текст"),
         "enter": B("Нажать Enter после ввода")}, ["id", "text"]),
    _fn("find_on_screen", "Найти на экране описанный элемент моделью зрения (когда его нет в screen_elements: "
        "игры, рисунки, нестандартный интерфейс). Возвращает абсолютные координаты центра.",
        {"description": S("Что найти: 'красная кнопка Play', 'иконка корзины в правом верхнем углу'"),
         "where": S("'window' — активное окно (по умолчанию, точнее) или 'screen' — весь монитор")}, ["description"]),
    _fn("click_on_screen", "Найти элемент моделью зрения и сразу кликнуть, с проверкой результата. Для того, "
        "чего нет в screen_elements.",
        {"description": S("Что нажать"), "button": S("left/right"), "double": B("Двойной клик"),
         "where": S("'window' или 'screen'")}, ["description"]),
]

TOOLS = {f.__name__: f for f in (run_command, run_python, press_keys, type_text, mouse_click,
                                 screen_info, list_windows, focus_window, open_app, web_search, open_url, fetch_page, wait,
                                 write_file, read_file, list_dir, run_file, look_at_screen, look_at_image, wallpaper_path,
                                 screen_elements, click_element, fill_element, find_on_screen, click_on_screen)}

# Эти действия меняют систему — спрашиваем подтверждение (если не включён авто-режим)
DANGEROUS = {"run_command", "run_python", "press_keys", "type_text", "mouse_click", "open_app",
             "focus_window", "write_file", "run_file", "click_element", "fill_element", "click_on_screen"}


TOOL_WAIT = {}      # сколько ждать инструмент, прежде чем отправить в фон (для зарегистрированных)
PROMPT_HINTS = []   # правила для системного промпта от подключённых модулей (память и т. п.)


def register(schema: dict, fn, *, dangerous=False, wait=None, hint=None):
    """Подключить инструмент извне (например, из памяти) без правки этого файла."""
    name = schema["function"]["name"]
    TOOL_SCHEMAS[:] = [s for s in TOOL_SCHEMAS if s["function"]["name"] != name] + [schema]
    TOOLS[name] = fn
    if dangerous:
        DANGEROUS.add(name)
    else:
        DANGEROUS.discard(name)
    if wait:
        TOOL_WAIT[name] = wait
    if hint and hint not in PROMPT_HINTS:
        PROMPT_HINTS.append(hint)


def execute(name: str, args: dict) -> str:
    fn = TOOLS.get(name)
    if not fn:
        return f"Ошибка: инструмента '{name}' нет. Доступны: {', '.join(TOOLS)}"
    try:
        return str(fn(**(args or {})))
    except TypeError as e:
        return f"Ошибка аргументов: {e}"
    except Exception as e:
        return f"Ошибка: {type(e).__name__}: {e}"


# Собственный браузер агента (browser_*): сайты без окна пользователя, мыши и клавиатуры
from .browser import register_browser_tools  # noqa: E402

register_browser_tools()
