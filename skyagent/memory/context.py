"""Автоконтекст: до ответа агента код сам подбирает релевантное запросу из памяти.

Агенту не нужно «догадываться» вызвать память — блок контекста уже лежит рядом с запросом.
От дешёвого к дорогому:
  1. поиск в три канала (слова, триграммы, смысл) — миллисекунды, без модели;
  2. если нашлось меньше MIN_HITS — модель расширяет запрос вариантами (другие формы слов,
     синонимы, возможные искажения распознавания) и поиск повторяется;
  3. граф: сущности, которые упомянуты в запросе, — коротко (связи и факты).
Всё укладывается в бюджет символов. Блок помечен как данные, а не команды.
"""
import re
import time

from .llm import chat_json

BUDGET = 2500
MIN_HITS = 2
KINDS = ("speech", "focus", "episode", "message", "entry")   # entry — личный дневник
# Системные команды («открой хром») — поиск по памяти им не нужен, только краткое «сейчас»
ACTION = re.compile(r"^\s*(открой|запусти|закрой|выключи|включи|нажми|напечатай|перезагрузи|сверни|"
                    r"разверни|сделай громче|сделай тише|поставь на паузу)\b", re.I)

EXPAND_JSON = {"type": "object", "required": ["variants"], "properties": {
    "variants": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "string", "maxLength": 60}}}}
EXPAND_PROMPT = """Перепиши поисковый запрос к памяти ассистента (расшифровки живой речи с ошибками \
распознавания, заголовки окон, сводки). Дай до 5 РАЗНЫХ коротких вариантов (2–4 слова), не повторяй \
запрос с другим окончанием. Смешай:
- ключевое слово в именительном падеже и полные/уменьшительные формы имён (Петя, Пётр, Петька);
- разговорные сокращения и синонимы (репозиторий → репа, репо, гит; компьютер → комп, ноут);
- как слово могло быть неверно распознано на слух (Петя → Пятя; докер → дакер);
- о чём это обычно говорят (обещал → «сделаю», «скину», «пришлю»)."""


class ContextBuilder:
    def __init__(self, memory, working=None, graph=None, model_fn=None):
        self.memory = memory
        self.working = working
        self.graph = graph
        self.model_fn = model_fn
        self.last = None
        self.error = None

    def expand(self, query):
        if not self.model_fn:
            return []
        try:
            data, _ = chat_json(self.memory.embedder.url, self.model_fn(), EXPAND_PROMPT, query, EXPAND_JSON,
                                num_predict=400, timeout=60)
            return [v for v in data.get("variants", []) if isinstance(v, str) and v.strip()][:5]
        except Exception as e:  # noqa: BLE001 — расширение — запасной путь, без него просто меньше найдено
            self.error = f"расширение запроса: {type(e).__name__}: {e}"
            return []

    def _graph(self, query):
        if not self.graph:
            return ""
        try:
            found = self.graph.find(query, limit=2)
        except Exception:  # noqa: BLE001
            return ""
        lines = []
        for f in found[:2]:
            prof = self.graph.about(f["name"])
            if not prof:
                continue
            rel = ", ".join(r["name"] for r in prof["related"][:4])
            facts = "; ".join(x["text"] for x in prof["facts"][:3])
            lines.append(f"- {prof['name']} ({prof['type']}, упоминаний {prof['mentions']})"
                         + (f"; связано: {rel}" if rel else "") + (f"; факты: {facts}" if facts else ""))
        return "\n".join(lines)

    def build(self, query):
        """Текст блока контекста ('' — если нечего добавить) и сводка, что сделано (для /status и тестов)."""
        from .tools import with_context
        t = time.time()
        info = {"query": query, "hits": 0, "expanded": [], "graph": False}
        parts = []
        if self.working:
            now = self.working.read(["now.md"]).split("\n", 1)[-1].strip()
            if now:
                parts.append("Сейчас:\n" + now[:500])
        if not ACTION.match(query):
            hits = self.memory.search(query, kinds=KINDS, limit=6)
            if len(hits) < MIN_HITS:
                info["expanded"] = self.expand(query)
                seen = {h["id"] for h in hits}
                for v in info["expanded"]:
                    for h in self.memory.search(v, kinds=KINDS, limit=3):
                        if h["id"] not in seen:
                            seen.add(h["id"])
                            hits.append(h)
            hits = hits[:6]
            info["hits"] = len(hits)
            if hits:
                parts.append("Найдено в памяти по запросу (► — совпадение, рядом — контекст; у речи метки: "
                             "«я:» — сказал пользователь, «другой:» — НЕ пользователь, «?:» — неизвестно):\n"
                             + with_context(self.memory, hits)[:1400])
            if len(hits) < 3:
                parts.append("Найдено мало. Если вопрос о прошлом пользователя — поищи инструментами recall / speech "
                             "другими словами, прежде чем отвечать.")
            g = self._graph(query)
            if g:
                info["graph"] = True
                parts.append("Из графа памяти:\n" + g)
            if self.working:
                open_ = self.working.commitments("open")[-5:]
                if open_:
                    parts.append("Открытые обязательства:\n" + "\n".join(f"- #{c['id']} {c['text']}" for c in open_))
        text = "\n\n".join(parts)[:BUDGET]
        info["sec"] = round(time.time() - t, 2)
        info["chars"] = len(text)
        self.last = info
        return text, info
