"""Самопроверка: агент не говорит «готово», пока не проверил результат своих действий.

Правило в системном промпте модель иногда пропускает, поэтому его держит код:
  - каждый вызов инструмента — изменение (записал файл, запустил команду, кликнул) или проверка
    (прочитал файл, посмотрел на экран, запустил программу и увидел вывод);
  - если модель отвечает пользователю, а после последнего изменения не было проверки, — ответ не
    принимается: модели уходит напоминание с подсказками, ЧТО именно проверить для этой задачи
    (калькулятор — посчитать контрольные примеры, обои — посмотреть картинку и тему, файл — лежит ли там,
    где просили), и она продолжает работу. Напоминание одно на ход: дальше решает модель.
SKYAGENT_VERIFY=0 — выключить напоминания (правило в промпте остаётся).
"""
import os
import re

ENABLED = os.environ.get("SKYAGENT_VERIFY", "1") != "0"

# Только смотрят — значит, проверяют
READ_TOOLS = {"read_file", "list_dir", "look_at_screen", "look_at_image", "wallpaper_path", "screen_elements", "find_on_screen",
              "screen_info", "list_windows", "fetch_page", "browser_elements", "browser_read", "browser_look",
              "check_jobs"}
# Меняют систему
CHANGE_TOOLS = {"write_file", "run_command", "run_python", "run_file", "press_keys", "type_text", "mouse_click",
                "click_element", "fill_element", "click_on_screen", "open_app", "focus_window",
                "browser_click", "browser_type", "browser_press", "browser_download"}
# Команды, которые ничего не меняют, а только смотрят (тогда run_command — это проверка)
READONLY_CMD = re.compile(
    r"^\s*(dir|type|more|where|tree|findstr|tasklist|ipconfig|systeminfo|ver|echo|reg\s+query|"
    r"get-\w+|test-path|select-string|resolve-path|python\s+-c\s+\"?print|cat|ls)\b", re.I)
BACKGROUND = ("выполняется в фоне", "Запущено в отдельном окне")

PROGRAM_EXT = (".py", ".pyw", ".js", ".html", ".htm", ".bat", ".cmd", ".ps1", ".cs", ".cpp", ".c", ".java", ".ts")
WALLPAPER = re.compile(r"wallpaper|обо[ия]|SystemParametersInfo|SPI_SETDESKWALLPAPER|Desktop\\\\?WallPaper|"
                       r"фон рабочего стола", re.I)
CALC = re.compile(r"калькулятор|calculator|calc\b|посчита|вычисл|формул|сумм|итог|процент", re.I)


def kind(name, args, result=""):
    """'change', 'check' или None (ни то ни другое: память, поиск, ожидание)."""
    if name in READ_TOOLS:
        return "check"
    if name in ("run_command", "run_python"):
        code = str(args.get("command") or args.get("code") or "")
        if name == "run_command" and READONLY_CMD.match(code) and not re.search(r"[>|]\s*\S|&&\s*(del|rd|move|copy)", code):
            return "check"
        return "change"
    if name == "run_file":
        return "check"          # запуск программы и её вывод — это проверка написанного кода
    if name in CHANGE_TOOLS:
        return "change"
    return None


class Tracker:
    """Следит за одним ходом агента: были ли изменения и проверены ли они."""

    def __init__(self, request=""):
        self.request = request or ""
        self.changes = []           # (инструмент, аргументы) изменений после последней проверки
        self.all_changes = []
        self.nudged = False

    def note(self, name, args, result):
        k = kind(name, args, result)
        res = str(result or "")
        if k == "change":
            if res.startswith(("Пользователь запретил", "ПОВТОР", "ОШИБКА")) or any(b in res for b in BACKGROUND):
                return
            self.changes.append((name, args))
            self.all_changes.append((name, args))
        elif k == "check":
            self.changes = []

    def needs_check(self):
        return ENABLED and bool(self.changes) and not self.nudged

    def hints(self):
        text = " ".join(f"{n} {args}" for n, args in self.all_changes) + " " + self.request
        out = []
        files = [str(a.get("path", "")) for n, a in self.all_changes if n == "write_file"]
        if files:
            out.append("файл: list_dir / read_file — лежит ли он ровно там, где просил пользователь (папка, имя, "
                       "расширение), и правильное ли в нём содержимое: " + ", ".join(files[:3]))
        if any(f.lower().endswith(PROGRAM_EXT) for f in files) or any(n == "run_python" for n, _ in self.all_changes):
            out.append("программа: запусти её (run_file / run_python) и убедись, что работает без ошибок")
        if CALC.search(text):
            out.append("расчёты: посчитай сам 2–3 контрольных примера (например, 2+2=4, 7×8=56, 10/4=2.5, деление на "
                       "ноль) и сверь с тем, что выдаёт программа; если не сходится — исправь")
        if WALLPAPER.search(text):
            out.append("обои: проверь, что они действительно сменились (wallpaper_path) и посмотри на саму картинку (look_at_image) — та ли тематика, "
                       "что просил пользователь")
        if any(n in ("click_element", "click_on_screen", "mouse_click", "press_keys", "type_text", "fill_element",
                     "open_app", "focus_window") for n, _ in self.all_changes):
            out.append("окно: screen_elements или look_at_screen — изменилось ли оно так, как нужно")
        if any(n.startswith("browser_") for n, _ in self.all_changes):
            out.append("сайт: browser_elements / browser_read — получилось ли действие на странице")
        if any(n == "run_command" for n, _ in self.all_changes) and not out:
            out.append("команда: отдельной командой убедись, что результат есть (файл/папка существует, настройка "
                       "изменилась, процесс запущен или закрыт)")
        return out or ["посмотри на результат подходящим инструментом и сравни с тем, что просил пользователь"]

    def nudge(self):
        self.nudged = True
        req = self.request.strip().replace("\n", " ")
        req = (req[:300] + "…") if len(req) > 300 else req
        return ("[Системное] САМОПРОВЕРКА. Ты что-то изменил, но ещё не проверил результат — не отвечай «готово» "
                "вслепую. " + (f"Пользователь просил: «{req}». " if req else "")
                + "Проверь инструментами:\n- " + "\n- ".join(self.hints())
                + "\nЕсли нашлась ошибка — исправь и проверь снова. В ответе пользователю коротко скажи, что "
                  "проверил и что увидел. Если проверить нельзя — честно скажи почему.")


PROMPT_RULE = """- САМОПРОВЕРКА: сделал — проверь, и только потом отвечай. Задача не выполнена, пока результат
  не подтверждён инструментом:
  написал калькулятор/программу → запусти и сверь 2–3 примера, посчитанных в уме (2+2=4, 7×8=56);
  сменил обои → wallpaper_path: сменились ли, и посмотри на картинку (look_at_image) — та ли тематика;
  создал файл → list_dir/read_file: лежит ли он там, где просил пользователь, и что в нём;
  кликнул или ввёл в окне → screen_elements/look_at_screen: изменилось ли окно.
  Если проверка показала ошибку — исправь. В ответе коротко скажи, что проверил."""
