"""Собственный браузер агента: Edge без окна (Playwright), отдельный профиль.

Агент открывает сайты, кликает, заполняет поля и скачивает файлы в нём — твой браузер, мышь и
клавиатура не задействованы. Элементы страницы агент получает списком с номерами (как screen_elements),
а «посмотреть» может снимком страницы через модель зрения.

SKYAGENT_BROWSER_SHOW=1 — показывать окно браузера (посмотреть, что делает агент).
SKYAGENT_BROWSER=chrome — взять Chrome вместо Edge.
"""
import base64
import os
import queue
import re
import threading
import time
from pathlib import Path

DOWNLOADS = Path.home() / "SkyAgent" / "downloads"
PROFILE = Path(os.environ.get("SKYAGENT_DATA") or Path(__file__).resolve().parents[1] / "data") / "browser"
CHANNEL = {"chrome": "chrome", "edge": "msedge", "msedge": "msedge"}.get(
    os.environ.get("SKYAGENT_BROWSER", "edge").lower(), "msedge")
SHOW = os.environ.get("SKYAGENT_BROWSER_SHOW", "0") == "1"
ACTION_TIMEOUT = 20_000
BLOCKED_TITLES = ("один момент", "just a moment", "что-то пошло не так", "access denied", "attention required",
                  "доступ запрещ", "are you a robot", "captcha")
MAX_ELEMENTS = 80

# Пронумеровать видимые интерактивные элементы: атрибут data-sky-id, по нему потом кликаем
ELEMENTS_JS = r"""
(query) => {
  document.querySelectorAll('[data-sky-id]').forEach(e => e.removeAttribute('data-sky-id'));
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [role=button], [role=link],' +
              '[role=tab], [role=menuitem], [role=checkbox], [role=option], [role=switch], [contenteditable=true]';
  const vh = innerHeight, vw = innerWidth, out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 3 || r.height < 3) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    const tag = el.tagName.toLowerCase();
    let text = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') ||
                el.getAttribute('title') || el.getAttribute('alt') || '').trim().replace(/\s+/g, ' ');
    if (!text) { const img = el.querySelector('img[alt]'); if (img) text = (img.alt || '').trim(); }
    if (!text && tag === 'a') text = decodeURIComponent((el.getAttribute('href') || '').split('/').filter(Boolean).pop() || '');
    const type = (el.getAttribute('type') || '').toLowerCase();
    const kind = el.getAttribute('role') || (tag === 'a' ? 'ссылка' : tag === 'button' ? 'кнопка' :
                 tag === 'select' ? 'список' : tag === 'textarea' ? 'поле' :
                 tag === 'input' ? (['checkbox', 'radio'].includes(type) ? 'флажок' :
                                   ['submit', 'button'].includes(type) ? 'кнопка' : 'поле') : tag);
    out.push({el, kind, text: text.slice(0, 90), href: tag === 'a' ? (el.href || '').slice(0, 140) : '',
              value: ['input', 'textarea'].includes(tag) && !['checkbox', 'radio', 'submit', 'button'].includes(type)
                     ? (el.value || '').slice(0, 60) : '',
              inView: r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw, y: r.top + scrollY});
  }
  let items = out, matched = true;
  if (query) {
    const ws = query.toLowerCase().split(/\s+/).filter(w => w.length > 1);
    items = out.filter(o => ws.some(w => (o.text + ' ' + o.href + ' ' + o.kind).toLowerCase().includes(w)));
    if (!items.length) { items = out; matched = false; }
  }
  items.sort((a, b) => (b.inView - a.inView) || (a.y - b.y));
  items = items.slice(0, %d);
  items.forEach((o, i) => o.el.setAttribute('data-sky-id', String(i + 1)));
  return {matched, total: out.length, items: items.map((o, i) => ({id: i + 1, kind: o.kind, text: o.text,
          href: o.href, value: o.value, inView: o.inView}))};
}
""" % MAX_ELEMENTS


class AgentBrowser:
    """Playwright (sync API) живёт в одном потоке — инструменты агента вызываются из разных, поэтому
    все действия передаются в этот поток через очередь."""

    def __init__(self):
        self._q = queue.Queue()
        self._thread = None
        self._ctx = None
        self._page = None
        self._downloads = []      # скачивания, начатые страницей и ещё не сохранённые
        self.saved = []           # пути сохранённых файлов

    # ------------------------------------------------------------ поток браузера
    def _run(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            while True:
                fn, box, done = self._q.get()
                if fn is None:
                    break
                try:
                    box["result"] = fn(p)
                except Exception as e:  # noqa: BLE001 — ошибку отдаём вызвавшему
                    box["error"] = e
                done.set()
            if self._ctx:
                try:
                    self._ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def call(self, fn, timeout=90):
        if not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name="sky-browser", daemon=True)
            self._thread.start()
        box, done = {}, threading.Event()
        self._q.put((fn, box, done))
        if not done.wait(timeout):
            raise TimeoutError(f"браузер не ответил за {timeout} c")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def close(self):
        if self._thread and self._thread.is_alive():
            self._q.put((None, None, None))

    # ------------------------------------------------------------ внутри потока браузера
    def _ensure(self, p):
        if self._ctx is None:
            PROFILE.mkdir(parents=True, exist_ok=True)
            DOWNLOADS.mkdir(parents=True, exist_ok=True)
            self._ctx = p.chromium.launch_persistent_context(
                str(PROFILE), channel=CHANNEL, headless=not SHOW, accept_downloads=True,
                viewport={"width": 1366, "height": 900}, locale="ru-RU",
                args=["--disable-features=Translate", "--no-first-run", "--no-default-browser-check"])
            self._ctx.set_default_timeout(ACTION_TIMEOUT)
            self._ctx.on("page", self._on_page)
            for pg in self._ctx.pages:
                self._on_page(pg)
            self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # служебные вкладки браузера («Загрузки» после скачивания) — не наши; закрывать их нельзя (зависает)
        internal = lambda pg: pg.url.startswith(("edge://", "chrome://"))   # noqa: E731
        if self._page is None or self._page.is_closed() or internal(self._page):
            live = [pg for pg in self._ctx.pages if not pg.is_closed() and not internal(pg)]
            self._page = live[-1] if live else self._ctx.new_page()
        return self._page

    def _on_page(self, page):
        page.on("download", lambda dl: self._downloads.append(dl))
        self._page = page          # новая вкладка (target=_blank) — дальше работаем в ней

    def _save_downloads(self):
        out = []
        for dl in self._downloads:
            name = re.sub(r'[<>:"/\\|?*]', "_", dl.suggested_filename or "download")
            path = DOWNLOADS / name
            stem, ext, n = path.stem, path.suffix, 1
            while path.exists():
                path = DOWNLOADS / f"{stem} ({n}){ext}"
                n += 1
            try:
                dl.save_as(str(path))
                out.append(str(path))
            except Exception as e:  # noqa: BLE001
                out.append(f"(не скачалось {name}: {e})")
        self._downloads.clear()
        self.saved += [x for x in out if not x.startswith("(")]
        return out

    @staticmethod
    def _settle(page, ms=6000):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=ms)
        except Exception:  # noqa: BLE001 — страница грузится долго — это не ошибка
            pass
        page.wait_for_timeout(600)

    def _status(self, page, extra=""):
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        files = self._save_downloads()
        s = f"Страница: «{title}» ({page.url})"
        if files:
            s += "\nСкачано: " + "; ".join(files)
        return s + (f"\n{extra}" if extra else "")

    # ------------------------------------------------------------ действия
    def open(self, url):
        if not re.match(r"^[a-z]+://", url) and url != "back":
            url = "https://" + url

        def go(p):
            page = self._ensure(p)
            if url == "back":
                page.go_back()
            else:
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except Exception as e:  # noqa: BLE001 — ссылка на файл: goto падает, но скачивание началось
                    if not self._downloads:
                        raise
                    page.wait_for_timeout(500)
                    return self._status(page, f"(переход вызвал скачивание: {type(e).__name__})")
            self._settle(page)
            code = resp.status if url != "back" and resp else 200
            title = (page.title() or "").lower()
            if code in (401, 403, 429, 503) or any(w in title for w in BLOCKED_TITLES):
                return self._status(page, f"ОШИБКА: сайт не пускает браузер агента (HTTP {code}, защита от ботов). "
                                          "Не открывай его снова — возьми другой источник.")
            return self._status(page, "Дальше: browser_elements — элементы страницы с номерами; "
                                      "browser_read — текст; browser_look — посмотреть глазами.")
        return self.call(go)

    def elements(self, query=""):
        def run(p):
            page = self._ensure(p)
            data = page.evaluate(ELEMENTS_JS, query or "")
            lines = []
            for e in data["items"]:
                s = f"#{e['id']} {e['kind']} «{e['text'] or '(без подписи)'}»"
                if e["value"]:
                    s += f" = «{e['value']}»"
                if e["href"]:
                    s += f" → {e['href']}"
                if not e["inView"]:
                    s += " (ниже/выше экрана)"
                lines.append(s)
            head = self._status(page)
            if query and not data["matched"]:
                head += f"\nПо запросу «{query}» ничего — вот все элементы."
            if not lines:
                return head + "\nИнтерактивных элементов не найдено (страница ещё грузится?)."
            return (head + f"\nЭлементов: {len(lines)} из {data['total']} (номер — для browser_click / "
                    "browser_type):\n" + "\n".join(lines))
        return self.call(run)

    def _loc(self, page, eid):
        loc = page.locator(f'[data-sky-id="{int(eid)}"]')
        if not loc.count():
            raise LookupError("нет элемента с таким номером — страница изменилась, вызови browser_elements")
        return loc.first

    def click(self, eid):
        def run(p):
            page = self._ensure(p)
            loc = self._loc(page, eid)
            label = (loc.inner_text(timeout=2000) or loc.get_attribute("aria-label") or "").strip()[:60]
            pages_before = len(self._ctx.pages)
            loc.click()
            page.wait_for_timeout(800)
            cur = self._ensure(p)
            self._settle(cur, 4000)
            if self._downloads:
                cur.wait_for_timeout(500)
            note = "Открылась новая вкладка — дальше работаю в ней." if len(self._ctx.pages) > pages_before else ""
            return f"Клик по #{eid} «{label}». " + self._status(cur, note)
        return self.call(run)

    def type(self, eid, text, enter=False):
        def run(p):
            page = self._ensure(p)
            loc = self._loc(page, eid)
            loc.fill(text)
            if enter:
                loc.press("Enter")
                self._settle(page)
            return f"В #{eid} вписано «{text[:60]}»" + (" + Enter" if enter else "") + ". " + self._status(self._ensure(p))
        return self.call(run)

    def press(self, keys):
        def run(p):
            page = self._ensure(p)
            combo = "+".join(k.strip().capitalize() if len(k.strip()) > 1 else k.strip()
                             for k in re.split(r"\s*\+\s*", keys) if k.strip())
            combo = combo.replace("Ctrl", "Control").replace("Esc+", "Escape+")
            if combo == "Esc":
                combo = "Escape"
            page.keyboard.press(combo)
            self._settle(page, 3000)
            return f"Нажато {combo}. " + self._status(page)
        return self.call(run)

    def read(self, max_chars=6000):
        def run(p):
            page = self._ensure(p)
            text = page.evaluate("() => document.body ? document.body.innerText : ''")
            text = re.sub(r"\n\s*\n+", "\n", text or "").strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…(обрезано)"
            return self._status(page) + "\n\n" + text
        return self.call(run)

    def screenshot(self, full=False):
        def run(p):
            page = self._ensure(p)
            png = page.screenshot(full_page=full, type="jpeg", quality=80)
            return base64.b64encode(png).decode(), self._status(page)
        return self.call(run)

    def download(self, url, filename=""):
        def run(p):
            page = self._ensure(p)
            resp = self._ctx.request.get(url, timeout=60_000)
            if not resp.ok:
                return f"ОШИБКА: {resp.status} {resp.status_text} — файл не скачан ({url})"
            name = filename or url.split("?")[0].rstrip("/").split("/")[-1] or "download"
            name = re.sub(r'[<>:"/\\|?*]', "_", name)
            ctype = resp.headers.get("content-type", "")
            if "." not in name:
                ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
                name += next((v for k, v in ext.items() if k in ctype), "")
            DOWNLOADS.mkdir(parents=True, exist_ok=True)
            path = DOWNLOADS / name
            path.write_bytes(resp.body())
            self.saved.append(str(path))
            size = path.stat().st_size
            warn = " ⚠ это HTML-страница, а не файл — ищи прямую ссылку" if "text/html" in ctype else ""
            return f"Скачано: {path} ({size / 1024:.0f} КБ, {ctype or 'тип неизвестен'}){warn}. Текущая: {page.url}"
        return self.call(run, timeout=120)


browser = AgentBrowser()


# ------------------------------------------------------------ инструменты агента
def _guard(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except ImportError:
            return "ОШИБКА: не установлен playwright (uv pip install playwright)"
        except Exception as e:  # noqa: BLE001
            msg = str(e).split("\n")[0][:300]
            return f"ОШИБКА браузера агента: {type(e).__name__}: {msg}"
    wrapper.__name__ = fn.__name__
    return wrapper


@_guard
def browser_open(url: str) -> str:
    return browser.open(url)


@_guard
def browser_elements(query: str = "") -> str:
    return browser.elements(query)


@_guard
def browser_click(id: int) -> str:
    return browser.click(id)


@_guard
def browser_type(id: int, text: str, enter: bool = False) -> str:
    return browser.type(id, text, enter)


@_guard
def browser_press(keys: str) -> str:
    return browser.press(keys)


@_guard
def browser_read() -> str:
    return browser.read()


@_guard
def browser_download(url: str, filename: str = "") -> str:
    return browser.download(url, filename)


@_guard
def browser_look(question: str = "") -> str:
    from . import tools
    b64, status = browser.screenshot()
    tools.LAST_IMAGE["b64"] = b64
    prompt = ("Это снимок веб-страницы в браузере. Опиши по-русски: что на странице, главные кнопки, ссылки, "
              "картинки, поля, ошибки. Точно и кратко. Координаты НЕ пиши.")
    if question:
        prompt += f"\nОбязательно ответь: {question}"
    desc = tools.describe_image(b64, prompt)
    return f"{status}\n{desc}\n(кликать — по номерам из browser_elements)"


def register_browser_tools():
    from .tools import S, I, B, _fn, register
    hint = ("САЙТЫ — в СВОЁМ браузере (browser_*): он невидимый и не мешает пользователю. "
            "browser_open → browser_elements → browser_click / browser_type → снова browser_elements. "
            "Текст страницы — browser_read, картинка — browser_look, файл по прямой ссылке — browser_download "
            f"(сохраняется в {DOWNLOADS}). open_url и web_search(open_browser=true) открывают браузер "
            "ПОЛЬЗОВАТЕЛЯ — только если он сам просит что-то ему открыть или показать. "
            "Если сайт не пускает браузер агента (защита от ботов) — не пробуй его снова, бери другой источник. "
            "КАРТИНКИ И ОБОИ — из открытых API (отдают JSON со ссылками, читай через fetch_page): "
            "wallhaven.cc/api/v1/search?q=nature&categories=100&purity=100&sorting=toplist&atleast=1920x1080 (поле path — прямая ссылка), "
            "www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=ru-RU (url — дописать к https://www.bing.com, "
            "заменив 1920x1080 на UHD), commons.wikimedia.org. Скачивай browser_download. "
            "Установить обои: run_python с ctypes.windll.user32.SystemParametersInfoW(20, 0, r'путь', 3).")
    tools = [
        (_fn("browser_open", "Открыть сайт в собственном (невидимом) браузере агента. 'back' — назад.",
             {"url": S("Адрес")}, ["url"]), browser_open, False, None),
        (_fn("browser_elements", "Кнопки, ссылки, поля текущей страницы браузера агента с номерами.",
             {"query": S("Фильтр по тексту (например 'Download'); пусто — всё")}, []), browser_elements, False, None),
        (_fn("browser_click", "Кликнуть элемент страницы по номеру из browser_elements. Скачивания сохраняются сами.",
             {"id": I("Номер из browser_elements")}, ["id"]), browser_click, True, None),
        (_fn("browser_type", "Вписать текст в поле страницы по номеру из browser_elements.",
             {"id": I("Номер поля"), "text": S("Текст"), "enter": B("Нажать Enter после ввода")},
             ["id", "text"]), browser_type, True, None),
        (_fn("browser_press", "Нажать клавиши на странице браузера агента: 'PageDown', 'Enter', 'Escape', 'ctrl+f'.",
             {"keys": S("Клавиши через +")}, ["keys"]), browser_press, False, None),
        (_fn("browser_read", "Текст текущей страницы браузера агента.", {}, []), browser_read, False, None),
        (_fn("browser_look", "Посмотреть на страницу браузера агента моделью зрения (картинки, вёрстка).",
             {"question": S("Что найти или проверить")}, []), browser_look, False, 180),
        (_fn("browser_download", f"Скачать файл по прямой ссылке (картинка, pdf, архив) в {DOWNLOADS}.",
             {"url": S("Прямая ссылка на файл"), "filename": S("Имя файла (необязательно)")}, ["url"]),
         browser_download, True, 150),
    ]
    for i, (schema, fn, dangerous, wait) in enumerate(tools):
        register(schema, fn, dangerous=dangerous, wait=wait, hint=hint if i == 0 else None)
