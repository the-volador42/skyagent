"""Долговременная память (R1): граф в Neo4j. Подготовка слоя — docs/ARCHITECTURE.md, раздел 7.

Граф хранит СМЫСЛ, SQLite — ДОКАЗАТЕЛЬСТВА: у каждого узла и факта есть ссылки на эпизоды и события.
Граф — производные данные: его можно стереть и пересобрать из эпизодов (другим экстрактором,
с другими коэффициентами). Пространство имён ns позволяет держать несколько версий рядом
(и не смешивать тесты с настоящей памятью — в Community-версии Neo4j одна база).

Узлы:   (:Entity:<Тип> {id, ns, type, name, norm, aliases, weight, mentions, first_seen, last_seen, embedding})
        (:Episode {id, ns, ep_id, ts, ts_end, title, summary})
        (:Fact {id, ns, kind, text, observed_at, valid_from, valid_to, trust, source_episode,
                source_events, extractor, weight, embedding})
Связи:  (Entity)-[:MENTIONED_IN {count}]->(Episode)
        (Fact)-[:FROM]->(Episode),  (Fact)-[:ABOUT]->(Entity)
        (Entity)-[:RELATED {thickness, length, count, updated}]->(Entity)   — направление: id a < id b
        (Entity)-[:POSSIBLY_SAME {score}]->(Entity)                         — сомнение, а не слияние

Механика памяти:
  длина     — смысловое расстояние 1 − cos(эмбеддинги имён); не меняется при обращениях;
  толщина   — опыт, правило Хебба: совместное появление укрепляет связь,
              t ← t + η·(1 − t) / инерция, инерция = 1 + ln(1 + min(вес a, вес b));
  вес узла  — инерция: число подтверждений (упоминаний); тяжёлый узел сдвигается медленнее;
  круг 1    — распространение активации от якорей (текущий контекст), связь передаёт
              толщина·(1 − длина) / степень узла (хабы не стягивают всё к себе), бюджет — top-K;
  круг 2    — явное совпадение (полнотекстовый и векторный индексы);
  затухание — толщина падает по времени АКТИВНОСТИ (а не календаря), полураспад HALF_LIFE_H.
Каждое изменение толщины и веса пишется в журнал graph_journal (SQLite) — чтобы потом
прогнать память с другими коэффициентами.
"""
import json
import math
import os
import re
import threading
import time

ETA = 0.3                    # скорость укрепления связи
HALF_LIFE_H = 14 * 8         # полураспад толщины: ~2 недели по 8 активных часов
SAME_SIM = 0.93              # не-люди: такое сходство названий — та же сущность (псевдоним)
MAYBE_SIM = 0.85             # такое — «возможно, то же» (ребро-сомнение, не слияние)
FIND_SIM = 0.8               # круг 2 по смыслу: у коротких названий эмбеддинги «шумят» ниже 0.8
CIRCLE1_BUDGET = 12
EXTRACTOR = "episode-summary-v0"

# Тип → метка Neo4j (только из этого списка: метки подставляются в запрос строкой)
TYPES = {"person": "Person", "topic": "Topic", "project": "Project", "app": "App",
         "organization": "Organization", "place": "Place", "document": "Document"}
IDE_APPS = {"VS Code", "PyCharm", "IntelliJ IDEA", "Visual Studio"}

JOURNAL = """
CREATE TABLE IF NOT EXISTS graph_journal (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, ns TEXT NOT NULL, op TEXT NOT NULL,
    a TEXT, b TEXT, old REAL, new REAL, episode INTEGER, note TEXT
);
"""


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower().replace("ё", "е"))).strip()


def cosine(a, b):
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def projects_from_titles(titles):
    """«main.py - skyagent» в IDE → проект skyagent (дёшево, без модели)."""
    out = set()
    for t in titles:
        parts = [p.strip() for p in re.split(r"\s+[-—–]\s+", t) if p.strip()]
        if len(parts) >= 2 and len(parts[-1]) <= 40:
            out.add(parts[-1])
    return out


class Graph:
    def __init__(self, memory, episodes=None, ns=None, uri=None, user=None, password=None):
        from neo4j import GraphDatabase
        self.memory = memory
        self.episodes = episodes
        self.tl = memory.timeline
        self.ns = ns or os.environ.get("SKYAGENT_GRAPH_NS", "main")
        self.driver = GraphDatabase.driver(uri or os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687"),
                                           auth=(user or os.environ.get("NEO4J_USER", "neo4j"),
                                                 password or os.environ.get("NEO4J_PASSWORD", "")),
                                           notifications_min_severity="OFF")   # «свойства ещё нет» — не ошибка
        self.driver.verify_connectivity()
        self.error = None
        self.stats = {"ingested": 0}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.ensure_schema()
        with self.tl._lock:
            self.tl.db.executescript(JOURNAL)
            self.tl.db.commit()
        memory.episode_hooks.append(self.forget_episodes)

    def close(self):
        self._stop.set()
        self.driver.close()

    def q(self, cypher, **params):
        with self.driver.session() as s:
            return [r.data() for r in s.run(cypher, ns=self.ns, **params)]

    def _id(self, *parts):
        return ":".join([self.ns, *map(str, parts)])

    # ------------------------------------------------------------ схема
    def ensure_schema(self):
        stmts = [
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT fact_id IF NOT EXISTS FOR (n:Fact) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT episode_id IF NOT EXISTS FOR (n:Episode) REQUIRE n.id IS UNIQUE",
            "CREATE INDEX entity_ns_norm IF NOT EXISTS FOR (n:Entity) ON (n.ns, n.norm)",
            "CREATE INDEX entity_ns_type IF NOT EXISTS FOR (n:Entity) ON (n.ns, n.type)",
            "CREATE INDEX episode_ns IF NOT EXISTS FOR (n:Episode) ON (n.ns, n.ep_id)",
            "CREATE VECTOR INDEX entity_emb IF NOT EXISTS FOR (n:Entity) ON n.embedding OPTIONS {indexConfig: "
            "{`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}}",
            "CREATE VECTOR INDEX fact_emb IF NOT EXISTS FOR (n:Fact) ON n.embedding OPTIONS {indexConfig: "
            "{`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}}",
            "CREATE FULLTEXT INDEX entity_text IF NOT EXISTS FOR (n:Entity) ON EACH [n.name, n.aliases_text]",
            "CREATE FULLTEXT INDEX fact_text IF NOT EXISTS FOR (n:Fact) ON EACH [n.text]",
        ]
        for s in stmts:
            self.q(s)

    def _journal(self, op, a=None, b=None, old=None, new=None, episode=None, note=None):
        with self.tl._lock:
            self.tl.db.execute("INSERT INTO graph_journal(ts, ns, op, a, b, old, new, episode, note) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (time.time(), self.ns, op, a, b, old, new, episode, note))
            self.tl.db.commit()

    # ------------------------------------------------------------ сопоставление сущностей (v0)
    def resolve(self, etype, name, emb):
        """Найти существующую сущность или создать новую. Возвращает (id, создана ли).
        Правила v0: точное совпадение имени или псевдонима — та же. Для не-людей очень близкое
        по смыслу название — псевдоним. Людей по смыслу не склеиваем (Петя ≠ Паша), только
        отмечаем сомнение ребром POSSIBLY_SAME."""
        n = norm_name(name)
        hit = self.q("MATCH (e:Entity {ns: $ns, type: $t}) WHERE e.norm = $n OR $n IN e.aliases_norm "
                     "RETURN e.id AS id LIMIT 1", t=etype, n=n)
        if hit:
            return hit[0]["id"], False
        similar = self.q("CALL db.index.vector.queryNodes('entity_emb', 10, $emb) YIELD node, score "
                         "WHERE node.ns = $ns AND node.type = $t RETURN node.id AS id, node.name AS name, score "
                         "ORDER BY score DESC LIMIT 3", emb=emb, t=etype)
        if similar and etype != "person" and similar[0]["score"] >= SAME_SIM:
            eid = similar[0]["id"]
            self.q("MATCH (e:Entity {id: $id}) SET e.aliases = coalesce(e.aliases, []) + $name, "
                   "e.aliases_norm = coalesce(e.aliases_norm, []) + $n, "
                   "e.aliases_text = coalesce(e.aliases_text, '') + ' ' + $name", id=eid, name=name, n=n)
            self._journal("alias", eid, name, similar[0]["score"])
            return eid, False
        eid = self._id("entity", etype, n)
        label = TYPES[etype]
        self.q(f"MERGE (e:Entity {{id: $id}}) ON CREATE SET e:{label}, e.ns = $ns, e.type = $t, e.name = $name, "
               "e.norm = $n, e.aliases = [], e.aliases_norm = [], e.aliases_text = '', e.weight = 0.0, "
               "e.mentions = 0, e.first_seen = timestamp() / 1000.0, e.embedding = $emb, e.archived = false",
               id=eid, t=etype, name=name, n=n, emb=emb)
        if etype == "person":       # «Петя» и «Петя Смирнов»: имя совпало — сомнение, а не слияние
            first = n.split()[0]
            for p in self.q("MATCH (e:Entity {ns: $ns, type: 'person'}) WHERE e.id <> $id AND "
                            "split(e.norm, ' ')[0] = $f RETURN e.id AS id", id=eid, f=first):
                similar.append({"id": p["id"], "score": MAYBE_SIM})
        for s in similar:
            if s["score"] >= MAYBE_SIM and s["id"] != eid:
                self.q("MATCH (a:Entity {id: $a}), (b:Entity {id: $b}) MERGE (a)-[r:POSSIBLY_SAME]->(b) "
                       "SET r.score = $s", a=eid, b=s["id"], s=s["score"])
                self._journal("possibly_same", eid, s["id"], s["score"])
        return eid, True

    # ------------------------------------------------------------ перенос эпизода
    def episode_entities(self, ep):
        """Сущности эпизода дёшево, без новой LLM: люди и темы — из сводки, приложения и проекты — из окон."""
        ents = [("person", p) for p in ep["meta"].get("people", [])]
        ents += [("topic", t) for t in ep["meta"].get("topics", [])]
        apps, ide_titles = {}, []
        for r in self.tl.range(ep["ts"], ep["ts_end"], "windows", ("focus",)):
            app = r["meta"].get("app")
            if not app or r["meta"].get("private"):
                continue
            dur = (r["ts_end"] or r["ts"]) - r["ts"]
            apps[app] = apps.get(app, 0) + dur
            if app in IDE_APPS:
                ide_titles.append(r["meta"].get("title", ""))
        ents += [("app", a) for a, d in apps.items() if d >= 60]
        ents += [("project", p) for p in projects_from_titles(ide_titles)]
        seen, out = set(), []
        for t, name in ents:
            key = (t, norm_name(name))
            if name.strip() and key[1] and key not in seen:
                seen.add(key)
                out.append((t, name.strip()))
        return out

    def ingest(self, ep):
        """Перенести эпизод в граф. Повторно не переносит (Хебб не идемпотентен)."""
        ep_node = self._id("episode", ep["id"])
        done = self.q("MATCH (e:Episode {id: $id}) RETURN e.extractor AS x", id=ep_node)
        if done and done[0]["x"] == EXTRACTOR:
            return False
        ents = self.episode_entities(ep)
        facts = [("decision", d) for d in ep["meta"].get("decisions", [])] + \
                [("todo", t) for t in ep["meta"].get("todos", [])]
        texts = [n for _, n in ents] + [t for _, t in facts]
        embs = self.memory.embedder.embed(texts) if texts else []
        ent_embs, fact_embs = embs[:len(ents)], embs[len(ents):]

        self.q("MERGE (e:Episode {id: $id}) SET e.ns = $ns, e.ep_id = $ep, e.ts = $ts, e.ts_end = $te, "
               "e.title = $title, e.summary = $summary, e.extractor = $x",
               id=ep_node, ep=ep["id"], ts=ep["ts"], te=ep["ts_end"], title=ep["title"], summary=ep["summary"],
               x=EXTRACTOR)
        ids = []
        for (etype, name), emb in zip(ents, ent_embs):
            eid, _ = self.resolve(etype, name, emb)
            ids.append((eid, emb))
            old = self.q("MATCH (e:Entity {id: $id}) RETURN e.weight AS w", id=eid)[0]["w"]
            self.q("MATCH (e:Entity {id: $id}), (p:Episode {id: $ep}) MERGE (e)-[m:MENTIONED_IN]->(p) "
                   "ON CREATE SET m.count = 1 SET e.weight = e.weight + 1, e.mentions = e.mentions + 1, "
                   "e.last_seen = $te, e.archived = false", id=eid, ep=ep_node, te=ep["ts_end"])
            self._journal("weight", eid, None, old, old + 1, ep["id"])

        for i, ((kind, text), emb) in enumerate(zip(facts, fact_embs)):
            fid = self._id("fact", ep["id"], i)
            self.q("MERGE (f:Fact {id: $id}) SET f.ns = $ns, f.kind = $kind, f.text = $text, f.observed_at = $te, "
                   "f.valid_from = $ts, f.valid_to = null, f.trust = 'derived', f.source_episode = $ep, "
                   "f.source_events = $evs, f.extractor = $x, f.weight = coalesce(f.weight, 1.0), f.embedding = $emb "
                   "WITH f MATCH (p:Episode {id: $epn}) MERGE (f)-[:FROM]->(p)",
                   id=fid, kind=kind, text=text, te=ep["ts_end"], ts=ep["ts"], ep=ep["id"],
                   evs=[ep["event_id"]] if ep.get("event_id") else [], x=EXTRACTOR, emb=emb, epn=ep_node)
            tn = norm_name(text)
            for (etype, name), (eid, _) in zip(ents, ids):     # о ком/о чём факт — по упоминанию имени
                first = norm_name(name).split()[0] if norm_name(name) else ""
                if first and len(first) >= 3 and first[:max(3, len(first) - 2)] in tn:
                    self.q("MATCH (f:Fact {id: $f}), (e:Entity {id: $e}) MERGE (f)-[:ABOUT]->(e)", f=fid, e=eid)

        uniq = {eid: emb for eid, emb in ids}
        keys = sorted(uniq)
        for i, a in enumerate(keys):                          # Хебб: совместное появление укрепляет связь
            for b in keys[i + 1:]:
                self.thicken(a, b, uniq[a], uniq[b], ep["id"])
        self.stats["ingested"] += 1
        return True

    def thicken(self, a, b, emb_a=None, emb_b=None, episode=None, eta=ETA):
        r = self.q("MATCH (x:Entity {id: $a}), (y:Entity {id: $b}) OPTIONAL MATCH (x)-[r:RELATED]-(y) "
                   "RETURN x.weight AS wa, y.weight AS wb, r.thickness AS t", a=a, b=b)
        if not r:
            return None
        wa, wb, t = r[0]["wa"] or 0, r[0]["wb"] or 0, r[0]["t"] or 0.0
        inertia = 1 + math.log1p(min(wa, wb))
        new = t + eta * (1 - t) / inertia
        length = 1 - cosine(emb_a, emb_b) if emb_a and emb_b else None
        self.q("MATCH (x:Entity {id: $a}), (y:Entity {id: $b}) MERGE (x)-[r:RELATED]->(y) "
               "ON CREATE SET r.count = 0, r.length = $len SET r.thickness = $t, r.count = r.count + 1, "
               "r.updated = timestamp() / 1000.0", a=a, b=b, t=new, len=length if length is not None else 0.5)
        self._journal("thicken", a, b, t, new, episode)
        return new

    def sync(self, limit=20):
        """Перенести в граф готовые эпизоды, которых там ещё нет (курсор по id эпизода)."""
        if self.episodes is None:
            return 0
        cursor = int(self.tl.kv_get(f"graph_cursor:{self.ns}") or 0)
        todo = [x for x in self.episodes._query("WHERE kind = 'block' AND status = 'done' AND id > ? ORDER BY id LIMIT ?",
                                      (cursor, limit))]
        n = 0
        for ep in todo:
            with self._lock:
                if self.ingest(ep):
                    n += 1
            self.tl.kv_set(f"graph_cursor:{self.ns}", ep["id"])
        return n

    # ------------------------------------------------------------ экстрактор v1 (фаза сна, тяжёлая модель)
    def ingest_llm(self, ep, entities, relations, facts, same_as, eta=0.15):
        """Сущности, связи с подписью и факты, извлечённые тяжёлой моделью во сне, плюс решения по
        сомнительным парам. Хебб здесь мягче (η = 0.15): эпизод уже укрепил связи на экстракторе v0."""
        ep_node = self._id("episode", ep["id"])
        self.q("MERGE (e:Episode {id: $id}) ON CREATE SET e.ns = $ns, e.ep_id = $ep, e.ts = $ts, e.ts_end = $te, "
               "e.title = $title, e.summary = $s SET e.extractor_v1 = 'sleep-llm-v1'",
               id=ep_node, ep=ep["id"], ts=ep["ts"], te=ep["ts_end"], title=ep["title"], s=ep["summary"])
        ents = [(x["type"] if x.get("type") in TYPES else "topic", x["name"].strip(), x.get("aliases") or [])
                for x in entities if x.get("name", "").strip()]
        embs = self.memory.embedder.embed([n for _, n, _ in ents]) if ents else []
        ids = {}
        for (etype, name, aliases), emb in zip(ents, embs):
            eid, _ = self.resolve(etype, name, emb)
            ids[norm_name(name)] = (eid, emb)
            for al in aliases:
                ids[norm_name(al)] = (eid, emb)
                self.q("MATCH (e:Entity {id: $id}) WHERE NOT $n IN coalesce(e.aliases_norm, []) AND e.norm <> $n "
                       "SET e.aliases = coalesce(e.aliases, []) + $a, e.aliases_norm = coalesce(e.aliases_norm, []) + $n, "
                       "e.aliases_text = coalesce(e.aliases_text, '') + ' ' + $a", id=eid, a=al, n=norm_name(al))
            self.q("MATCH (e:Entity {id: $id}), (p:Episode {id: $ep}) MERGE (e)-[m:MENTIONED_IN]->(p) "
                   "ON CREATE SET m.count = 1, e.weight = e.weight + 1, e.mentions = e.mentions + 1 "
                   "SET e.last_seen = $te", id=eid, ep=ep_node, te=ep["ts_end"])
        merged = 0
        for (etype, name, aliases), emb in zip(ents, embs):     # псевдоним = имя другой сущности → это она же
            keep = ids[norm_name(name)][0]
            for al in aliases:
                other = self.q("MATCH (e:Entity {ns: $ns, type: $t, norm: $n}) WHERE e.id <> $id RETURN e.id AS id",
                               t=etype, n=norm_name(al), id=keep)
                for o in other:
                    new_id = self.merge(keep, o["id"], episode=ep["id"])
                    if new_id:
                        merged += 1
                        keep = new_id
                        for k, (eid, e2) in list(ids.items()):
                            if eid == o["id"]:
                                ids[k] = (keep, e2)
        look = lambda n: ids.get(norm_name(n or ""), (None, None))
        n_rel = 0
        for r in relations:
            (a, ea), (b, eb) = look(r.get("a")), look(r.get("b"))
            if a and b and a != b:
                x, y = sorted([a, b])
                self.thicken(x, y, ea if x == a else eb, eb if y == b else ea, ep["id"], eta=eta)
                label = (r.get("label") or "").strip()[:60]
                if label:
                    self.q("MATCH (x:Entity {id: $x})-[r:RELATED]->(y:Entity {id: $y}) "
                           "SET r.labels = CASE WHEN $l IN coalesce(r.labels, []) THEN r.labels "
                           "ELSE coalesce(r.labels, []) + $l END", x=x, y=y, l=label)
                n_rel += 1
        fembs = self.memory.embedder.embed([f["text"] for f in facts]) if facts else []
        for i, (f, emb) in enumerate(zip(facts, fembs)):
            fid = self._id("fact", ep["id"], f"s{i}")
            self.q("MERGE (f:Fact {id: $id}) SET f.ns = $ns, f.kind = $kind, f.text = $text, f.observed_at = $te, "
                   "f.valid_from = $ts, f.trust = 'derived', f.source_episode = $ep, f.source_events = $evs, "
                   "f.extractor = 'sleep-llm-v1', f.weight = coalesce(f.weight, 1.0), f.embedding = $emb "
                   "WITH f MATCH (p:Episode {id: $epn}) MERGE (f)-[:FROM]->(p)",
                   id=fid, kind=f.get("kind", "info"), text=f["text"], te=ep["ts_end"], ts=ep["ts"], ep=ep["id"],
                   evs=[ep["event_id"]] if ep.get("event_id") else [], emb=emb, epn=ep_node)
            for name in f.get("about") or []:
                eid, _ = look(name)
                if eid:
                    self.q("MATCH (f:Fact {id: $f}), (e:Entity {id: $e}) MERGE (f)-[:ABOUT]->(e)", f=fid, e=eid)
        for s in same_as:
            a = self.find_one(s.get("a"))
            b = self.find_one(s.get("b"))
            if not a or not b or a == b:
                continue
            if s.get("same"):
                self.merge(a, b, episode=ep["id"])
                merged += 1
            else:          # сомнение разрешено «разные» — убрать ребро-сомнение
                self.q("MATCH (x:Entity {id: $a})-[r:POSSIBLY_SAME]-(y:Entity {id: $b}) DELETE r", a=a, b=b)
                self._journal("not_same", a, b, episode=ep["id"])
        return {"entities": len(set(e for e, _ in ids.values())), "relations": n_rel, "facts": len(facts),
                "merged": merged}

    def find_one(self, name):
        if not name:
            return None
        n = norm_name(name)
        r = self.q("MATCH (e:Entity {ns: $ns}) WHERE e.norm = $n OR $n IN coalesce(e.aliases_norm, []) "
                   "RETURN e.id AS id ORDER BY e.weight DESC LIMIT 1", n=n)
        return r[0]["id"] if r else None

    def merge(self, a, b, episode=None):
        """Слить две сущности в одну (APOC): остаётся та, у которой имя полнее; вес и упоминания
        складываются, второе имя становится псевдонимом, связи переезжают, петли удаляются."""
        rows = self.q("MATCH (e:Entity) WHERE e.id IN [$a, $b] RETURN e.id AS id, e.name AS name, e.weight AS w, "
                      "e.mentions AS m, coalesce(e.aliases, []) AS al", a=a, b=b)
        if len(rows) != 2:
            return None
        keep, drop = sorted(rows, key=lambda r: (len(r["name"]), r["w"] or 0), reverse=True)
        aliases = list(dict.fromkeys(keep["al"] + [drop["name"]] + drop["al"]))
        self.q("MATCH (k:Entity {id: $k}), (d:Entity {id: $d}) "
               "CALL apoc.refactor.mergeNodes([k, d], {properties: 'discard', mergeRels: true}) YIELD node "
               "SET node.weight = $w, node.mentions = $m, node.aliases = $al, node.aliases_norm = $aln, "
               "node.aliases_text = $alt RETURN node.id", k=keep["id"], d=drop["id"],
               w=(keep["w"] or 0) + (drop["w"] or 0), m=(keep["m"] or 0) + (drop["m"] or 0), al=aliases,
               aln=[norm_name(x) for x in aliases], alt=" ".join(aliases))
        self.q("MATCH (n:Entity {id: $k})-[r]-(n) DELETE r", k=keep["id"])
        self._journal("merge", keep["id"], drop["id"], episode=episode, note=drop["name"])
        return keep["id"]

    def possibly_same_pairs(self, names):
        """Сомнительные пары, где участвует кто-то из names — их решает модель во сне."""
        ns_ = [norm_name(n) for n in names if n]
        return self.q("MATCH (a:Entity {ns: $ns})-[r:POSSIBLY_SAME]-(b:Entity) WHERE (a.norm IN $n OR b.norm IN $n) "
                      "AND a.id < b.id RETURN a.name AS a, b.name AS b, r.score AS score", n=ns_)

    def known_names(self, limit=60):
        return [r["name"] for r in self.q("MATCH (e:Entity {ns: $ns}) WHERE e.type IN ['person', 'project', "
                                          "'organization'] RETURN e.name AS name ORDER BY e.weight DESC LIMIT $k",
                                          k=limit)]

    # ------------------------------------------------------------ круги
    def find(self, text, limit=5):
        """Круг 2: явное совпадение — по имени/псевдониму, полнотекстово и по смыслу."""
        n = norm_name(text)
        exact = self.q("MATCH (e:Entity {ns: $ns}) WHERE e.norm = $n OR $n IN e.aliases_norm "
                       "RETURN e.id AS id, e.name AS name, e.type AS type, 1.0 AS score", n=n)
        if exact:
            return exact
        words = " OR ".join(f"{w}*" for w in re.findall(r"\w{3,}", text))
        ft = self.q("CALL db.index.fulltext.queryNodes('entity_text', $q) YIELD node, score WHERE node.ns = $ns "
                    "RETURN node.id AS id, node.name AS name, node.type AS type, score LIMIT $k",
                    q=words, k=limit) if words else []
        emb = self.memory.embedder.embed([text])[0]
        vec = self.q("CALL db.index.vector.queryNodes('entity_emb', $k2, $emb) YIELD node, score "
                     "WHERE node.ns = $ns AND score >= $min RETURN node.id AS id, node.name AS name, "
                     "node.type AS type, score LIMIT $k", emb=emb, k=limit, k2=limit * 4, min=FIND_SIM)
        seen, out = set(), []
        for r in ft + vec:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
        return out[:limit]

    def circle1(self, anchors, budget=CIRCLE1_BUDGET, hops=2, decay=0.6):
        """Круг 1: распространение активации от якорей (id сущностей текущего контекста).
        Связь передаёт толщина·(1 − длина)/степень — узлы-хабы делят активацию на всех соседей."""
        act = {a: 1.0 for a in anchors}
        frontier = dict(act)
        for _ in range(hops):
            if not frontier:
                break
            rows = self.q("MATCH (a:Entity)-[r:RELATED]-(b:Entity) WHERE a.id IN $ids AND b.ns = $ns "
                          "AND NOT coalesce(b.archived, false) "
                          "RETURN a.id AS a, b.id AS b, r.thickness AS t, r.length AS l, "
                          "COUNT { (a)-[:RELATED]-() } AS deg", ids=list(frontier))
            nxt = {}
            for r in rows:
                flow = frontier[r["a"]] * decay * (r["t"] or 0) * (1 - min(r["l"] or 0.5, 0.95)) / max(r["deg"], 1)
                nxt[r["b"]] = nxt.get(r["b"], 0) + flow
            for k, v in nxt.items():
                act[k] = act.get(k, 0) + v
            frontier = nxt
        ranked = sorted(((k, v) for k, v in act.items() if k not in anchors), key=lambda x: -x[1])[:budget]
        if not ranked:
            return []
        names = {r["id"]: r for r in self.q("MATCH (e:Entity) WHERE e.id IN $ids RETURN e.id AS id, e.name AS name, "
                                            "e.type AS type, e.weight AS weight", ids=[k for k, _ in ranked])}
        return [{**names[k], "activation": round(v, 4)} for k, v in ranked if k in names]

    def reinforce(self, entity_ids, amount=0.5, episode=None):
        """Факт пригодился (использован в ответе) — вес растёт, связи между использованными крепнут.
        Вызывать только при реальном использовании, не при каждом попадании в поиск."""
        for eid in entity_ids:
            r = self.q("MATCH (e:Entity {id: $id}) SET e.weight = e.weight + $x, e.archived = false "
                       "RETURN e.weight AS w", id=eid, x=amount)
            if r:
                self._journal("reinforce", eid, None, r[0]["w"] - amount, r[0]["w"], episode)
        ids = sorted(entity_ids)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                self.thicken(a, b, episode=episode, eta=ETA * amount)

    # ------------------------------------------------------------ затухание и архив
    def active_hours(self, since, until=None):
        """Часы активности за компьютером (по окнам) — время затухания, а не календарь."""
        until = until or time.time()
        total = 0.0
        for r in self.tl.range(since, until, "windows", ("focus",)):
            total += max(0.0, min(r["ts_end"] or r["ts"], until) - max(r["ts"], since))
        return total / 3600

    def decay(self, hours=None):
        """Затухание толщины за прошедшее время активности. Вызывается редко (раз в сутки)."""
        last = float(self.tl.kv_get(f"graph_decay:{self.ns}") or time.time())
        h = self.active_hours(last) if hours is None else hours
        k = 0.5 ** (h / HALF_LIFE_H)
        n = self.q("MATCH (:Entity {ns: $ns})-[r:RELATED]->() SET r.thickness = r.thickness * $k "
                   "RETURN count(r) AS n", k=k)[0]["n"]
        self.tl.kv_set(f"graph_decay:{self.ns}", time.time())
        self._journal("decay", note=json.dumps({"hours": round(h, 2), "factor": round(k, 4), "edges": n}))
        return {"hours": h, "factor": k, "edges": n}

    def archive_candidates(self, max_weight=1.5, max_thickness=0.1, idle_days=30):
        """За вторым кругом: лёгкие, давно не встречавшиеся и слабо связанные сущности.
        Только список — переносом в архив займётся ночное уплотнение (этап 4)."""
        return self.q("MATCH (e:Entity {ns: $ns}) WHERE e.weight <= $w AND e.last_seen < $old "
                      "AND NOT EXISTS { (e)-[r:RELATED]-() WHERE r.thickness > $t } "
                      "AND NOT EXISTS { (:Fact {kind: 'todo'})-[:ABOUT]->(e) } "
                      "RETURN e.id AS id, e.name AS name, e.type AS type, e.weight AS weight",
                      w=max_weight, t=max_thickness, old=time.time() - idle_days * 86400)

    # ------------------------------------------------------------ «забудь»
    def forget_episodes(self, ep_ids):
        """Каскад: эпизоды и их факты удаляются; сущности теряют вес; сущности без единого
        упоминания удаляются. Толщину связей точно откатить нельзя (Хебб) — связи удалённых
        сущностей уходят вместе с ними."""
        nodes = [self._id("episode", i) for i in ep_ids]
        ents = self.q("MATCH (e:Entity)-[m:MENTIONED_IN]->(p:Episode) WHERE p.id IN $ids "
                      "RETURN e.id AS id, count(m) AS n", ids=nodes)
        self.q("MATCH (f:Fact)-[:FROM]->(p:Episode) WHERE p.id IN $ids DETACH DELETE f", ids=nodes)
        self.q("MATCH (p:Episode) WHERE p.id IN $ids DETACH DELETE p", ids=nodes)
        for e in ents:
            self.q("MATCH (e:Entity {id: $id}) SET e.weight = e.weight - $n, e.mentions = e.mentions - $n",
                   id=e["id"], n=e["n"])
        self.q("MATCH (e:Entity {ns: $ns}) WHERE NOT (e)-[:MENTIONED_IN]->() DETACH DELETE e")
        self._journal("forget", note=json.dumps(ep_ids))

    def wipe(self):
        """Стереть пространство имён целиком (пересборка с нуля, тесты)."""
        self.q("MATCH (n {ns: $ns}) DETACH DELETE n")
        self.tl.kv_set(f"graph_cursor:{self.ns}", 0)

    # ------------------------------------------------------------ профиль сущности
    def about(self, name):
        found = self.find(name, limit=3)
        if not found:
            return None
        e = found[0]
        prof = self.q("MATCH (e:Entity {id: $id}) RETURN e.name AS name, e.type AS type, e.weight AS weight, "
                      "e.mentions AS mentions, e.first_seen AS first, e.last_seen AS last, e.aliases AS aliases",
                      id=e["id"])[0]
        related = self.q("MATCH (e:Entity {id: $id})-[r:RELATED]-(o:Entity) RETURN o.name AS name, o.type AS type, "
                         "r.thickness AS t, r.length AS l, r.count AS n ORDER BY r.thickness DESC LIMIT 8", id=e["id"])
        facts = self.q("MATCH (f:Fact)-[:ABOUT]->(e:Entity {id: $id}) RETURN f.kind AS kind, f.text AS text, "
                       "f.observed_at AS ts ORDER BY f.observed_at DESC LIMIT 8", id=e["id"])
        eps = self.q("MATCH (e:Entity {id: $id})-[:MENTIONED_IN]->(p:Episode) RETURN p.ep_id AS id, p.title AS title, "
                     "p.ts AS ts ORDER BY p.ts DESC LIMIT 5", id=e["id"])
        maybe = self.q("MATCH (e:Entity {id: $id})-[r:POSSIBLY_SAME]-(o) RETURN o.name AS name, r.score AS s", id=e["id"])
        return {"id": e["id"], **prof, "related": related, "facts": facts, "episodes": eps, "maybe_same": maybe,
                "other_matches": [f["name"] for f in found[1:]]}

    def stats_counts(self):
        r = self.q("MATCH (n {ns: $ns}) RETURN labels(n) AS l, count(*) AS c")
        out = {}
        for x in r:
            key = "Entity" if "Entity" in x["l"] else x["l"][0]
            out[key] = out.get(key, 0) + x["c"]
        out["RELATED"] = self.q("MATCH (:Entity {ns: $ns})-[r:RELATED]->() RETURN count(r) AS c")[0]["c"]
        return out

    # ------------------------------------------------------------ фон
    def start(self, interval=60):
        def loop():
            while not self._stop.is_set():
                try:
                    self.sync()
                    last = self.tl.kv_get(f"graph_decay:{self.ns}")
                    if last is None:
                        self.tl.kv_set(f"graph_decay:{self.ns}", time.time())
                    elif time.time() - float(last) > 86400:
                        self.decay()
                    self.error = None
                except Exception as e:  # noqa: BLE001 — граф не обязателен: память работает и без него
                    self.error = f"{type(e).__name__}: {e}"
                self._stop.wait(interval)
        threading.Thread(target=loop, name="sky-graph", daemon=True).start()
        return self
