import { Sky } from './sky.js';
import { renderMarkdown, esc } from './md.js';

const $ = s => document.querySelector(s);
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
};
const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

const I = {
  chat: '<svg viewBox="0 0 24 24"><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v7a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A2.5 2.5 0 0 1 4 13.5z"/></svg>',
  agent: '<svg viewBox="0 0 24 24"><path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/></svg>',
  trash: '<svg viewBox="0 0 24 24"><path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/></svg>',
  copy: '<svg viewBox="0 0 24 24"><rect x="8" y="8" width="11" height="11" rx="2.5"/><path d="M5 15V6a1 1 0 0 1 1-1h9"/></svg>',
  redo: '<svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5M20 12a8 8 0 0 1-14 5.3L4 15"/></svg>',
  chev: '<svg class="chev" viewBox="0 0 24 24"><path d="m7 10 5 5 5-5"/></svg>',
  spark: '<svg class="sparkle" viewBox="0 0 24 24"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
};
const TOOL_ICONS = {
  run_command: '<path d="m5 8 4 4-4 4M11 17h8"/>',
  run_python: '<path d="M8 4h5a3 3 0 0 1 3 3v3H9a3 3 0 0 0-3 3v1a3 3 0 0 1-2-3V7a3 3 0 0 1 3-3zM16 20h-5a3 3 0 0 1-3-3v-3h7a3 3 0 0 0 3-3v-1a3 3 0 0 1 2 3v4a3 3 0 0 1-3 3z"/>',
  press_keys: '<rect x="3" y="6" width="18" height="12" rx="2.5"/><path d="M7 10h.01M11 10h.01M15 10h.01M8 14h8"/>',
  type_text: '<path d="M5 6h14M12 6v13M9 19h6"/>',
  mouse_click: '<rect x="7" y="3" width="10" height="18" rx="5"/><path d="M12 7v3"/>',
  screen_info: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  list_windows: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M3 9h18"/>',
  focus_window: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="12" cy="13" r="3"/>',
  open_app: '<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><rect x="13" y="13" width="7" height="7" rx="2"/>',
  web_search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  open_url: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  fetch_page: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h6"/>',
  wait: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  write_file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 15l2 2 4-4"/>',
  read_file: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h4"/>',
  list_dir: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  run_file: '<path d="M7 3h7l5 5v13H7z"/><path d="m11 11 4 2.5-4 2.5z"/>',
  look_at_screen: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
};
const toolIcon = n => `<svg viewBox="0 0 24 24">${TOOL_ICONS[n] || '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>'}</svg>`;

const HERO = {
  chat: {
    title: 'Над облаками',
    sub: 'Спокойный разговор с локальной моделью. Ничего не уходит дальше твоего компьютера.',
    chips: [['✨', 'Объясни квантовую запутанность простыми словами'], ['📝', 'Помоги написать вежливое письмо коллеге'],
            ['💡', 'Придумай 5 идей для пет-проекта'], ['🐍', 'Напиши на Python парсер CSV с примером']],
  },
  agent: {
    title: 'Агент на связи',
    sub: 'SkyAgent запускает команды, открывает программы и ищет в интернете. Опасные действия — только с твоего разрешения.',
    chips: [['🌤', 'Какая сейчас погода в Москве?'], ['📂', 'Покажи, что лежит у меня на рабочем столе'],
            ['🧮', 'Открой калькулятор'], ['💱', 'Узнай текущий курс доллара к рублю']],
  },
};

// ---------------------------------------------------------------- состояние
const S = {
  convs: LS.get('sky.convs', []),
  settings: Object.assign({ mode: 'chat', modelChat: '', modelAgent: '', temp: 0.7, ctx: 8192, think: true, system: '', auto: false, theme: 'sky', quality: 1, vision: '' },
                          LS.get('sky.settings', {})),
  models: [],
  currentId: null,
  draft: null,
  busy: null,       // { convId, abort, mode }
  attachments: [],
  filter: '',
};
const saveSettings = () => LS.set('sky.settings', S.settings);
let saveTimer = null;
function saveConvs(now) {
  clearTimeout(saveTimer);
  const doSave = () => { if (!LS.set('sky.convs', S.convs)) toast('Хранилище браузера переполнено — старые картинки не сохранятся'); };
  now ? doSave() : (saveTimer = setTimeout(doSave, 800));
}
const current = () => S.convs.find(c => c.id === S.currentId) || S.draft;
const model = () => S.settings.mode === 'agent' ? S.settings.modelAgent : S.settings.modelChat;

// ---------------------------------------------------------------- сеть
async function* ndjson(resp) {
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buf.trim()) yield JSON.parse(buf);
}
const post = (url, body, signal) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal });

async function checkHealth() {
  const el = $('#status');
  try {
    const h = await (await fetch('/api/health')).json();
    el.className = 'status ' + (h.ok ? 'ok' : 'bad');
    el.querySelector('span').textContent = h.ok ? `Ollama ${h.ollama} · онлайн` : 'Ollama недоступна';
    if (h.ok && !S.models.length) loadModels();
  } catch {
    el.className = 'status bad'; el.querySelector('span').textContent = 'сервер SkyAgent не отвечает';
  }
}

async function loadModels() {
  try {
    const r = await (await fetch('/api/models')).json();
    if (r.error) throw new Error(r.error);
    S.models = r.models;
  } catch (e) { S.models = []; }
  const names = S.models.map(m => m.name);
  const pick = pref => S.models.find(m => pref(m))?.name || names[0] || '';
  if (!names.includes(S.settings.modelChat)) S.settings.modelChat = pick(m => !m.caps.includes('embedding'));
  if (!names.includes(S.settings.modelAgent)) S.settings.modelAgent = pick(m => m.caps.includes('tools'));
  saveSettings(); renderModels();
}

// ---------------------------------------------------------------- модели
function renderVision() {
  const sel = $('#vision'), vis = S.models.filter(m => m.caps.includes('vision'));
  const pref = ['qwen3-vl', 'qwen2.5vl', 'gemma3', 'gemma4', 'minicpm-v', 'llava', 'moondream'];
  const rank = m => { const i = pref.findIndex(p => m.name.includes(p)); return [i < 0 ? 99 : i, m.caps.includes('thinking') ? 1 : 0, m.size]; };
  const best = [...vis].sort((a, b) => { const x = rank(a), y = rank(b); return x[0] - y[0] || x[1] - y[1] || x[2] - y[2]; })[0];
  sel.innerHTML = `<option value="">Авто${best ? ' — ' + esc(best.name) : ' (нет моделей)'}</option>` +
    vis.map(m => `<option value="${esc(m.name)}">${esc(m.name)} · ${(m.size / 1e9).toFixed(1)} ГБ</option>`).join('');
  sel.value = vis.some(m => m.name === S.settings.vision) ? S.settings.vision : '';
  const hasVL = vis.some(m => /qwen[\d.]*-?vl/.test(m.name));
  $('#visionHint').innerHTML = hasVL ? 'Qwen-VL установлена — отлично понимает интерфейсы.'
    : 'Лучше всего экран понимает лёгкая <b>qwen3-vl</b>: <code>ollama pull qwen3-vl:4b</code> (или <code>:2b</code> — ещё легче).';
}
$('#vision').onchange = e => { S.settings.vision = e.target.value; saveSettings(); };

function renderModels() {
  renderVision();
  const cur = model();
  $('#modelName').textContent = cur || (S.models.length ? 'выбери модель' : 'нет моделей');
  const menu = $('#modelMenu');
  if (!S.models.length) {
    menu.innerHTML = `<div class="menu-empty">Модели не найдены. Убедись, что Ollama запущена, и установи модель:<br><code>ollama pull qwen3:4b</code></div>`;
    return;
  }
  const agent = S.settings.mode === 'agent';
  menu.innerHTML = S.models.map(m => {
    const caps = m.caps.filter(c => ['tools', 'thinking', 'vision'].includes(c))
      .map(c => `<span class="cap ${c}">${{ tools: 'инструменты', thinking: 'размышления', vision: 'зрение' }[c]}</span>`).join('');
    const warn = agent && !m.caps.includes('tools') ? '<span class="cap">текстовые вызовы</span>' : '';
    return `<button class="model-item ${m.name === cur ? 'active' : ''}" data-model="${esc(m.name)}">
      <div class="mi-ic">${esc((m.family || m.name).slice(0, 2))}</div>
      <div class="mi-body"><div class="mi-name">${esc(m.name)}</div>
      <div class="mi-meta"><span>${esc(m.params)} · ${(m.size / 1e9).toFixed(1)} ГБ</span>${caps}${warn}</div></div></button>`;
  }).join('');
}
$('#modelBtn').onclick = e => { e.stopPropagation(); closeMenus('#modelPicker'); $('#modelPicker').classList.toggle('open'); };
$('#modelMenu').onclick = e => {
  const b = e.target.closest('[data-model]'); if (!b) return;
  S.settings[S.settings.mode === 'agent' ? 'modelAgent' : 'modelChat'] = b.dataset.model;
  saveSettings(); renderModels(); $('#modelPicker').classList.remove('open'); Sky.pulse();
};

// ---------------------------------------------------------------- режимы
function placePill() {
  const a = document.querySelector('.mode.active'), pill = $('#modePill');
  pill.style.width = a.offsetWidth + 'px';
  pill.style.transform = `translateX(${a.offsetLeft - 4}px)`;
}
function applyMode(mode) {
  S.settings.mode = mode; saveSettings();
  document.body.classList.toggle('mode-agent', mode === 'agent');
  document.querySelectorAll('.mode').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  placePill(); Sky.setMode(mode); renderModels(); renderHero();
  $('#input').placeholder = mode === 'agent' ? 'Что сделать на компьютере?' : 'Спроси что угодно…';
}
document.querySelectorAll('.mode').forEach(b => b.onclick = () => {
  if (b.dataset.mode === S.settings.mode) return;
  applyMode(b.dataset.mode);
  const c = current();
  if (!c || c.mode !== b.dataset.mode) newConv();
});
addEventListener('resize', placePill);

function renderHero() {
  const c = current(), h = HERO[S.settings.mode];
  $('#hero').classList.toggle('hidden', !!(c && c.messages.length));
  $('#heroTitle').textContent = h.title; $('#heroSub').textContent = h.sub;
  $('#chips').innerHTML = h.chips.map(([e, t], i) => `<button class="chip" style="animation-delay:${0.1 + i * 0.06}s" data-chip="${esc(t)}"><span>${e}</span>${esc(t)}</button>`).join('');
}
$('#chips').onclick = e => { const c = e.target.closest('[data-chip]'); if (c) { $('#input').value = c.dataset.chip; send(); } };

// ---------------------------------------------------------------- разговоры
function newConv() {
  S.draft = { id: uid(), mode: S.settings.mode, title: '', messages: [], created: Date.now(), updated: Date.now() };
  S.currentId = S.draft.id;
  renderSidebar(); renderThread(); $('#input').focus();
  document.body.classList.remove('side-open');
}
function selectConv(id) {
  const c = S.convs.find(x => x.id === id); if (!c) return;
  S.currentId = id; S.draft = null;
  if (c.mode !== S.settings.mode) applyMode(c.mode);
  renderSidebar(); renderThread(true);
  document.body.classList.remove('side-open');
}
function deleteConv(id) {
  if (S.busy?.convId === id) stop();
  S.convs = S.convs.filter(c => c.id !== id);
  post('/api/agent/reset', { session: id }).catch(() => {});
  saveConvs(true);
  if (S.currentId === id) newConv(); else renderSidebar();
}
function renderSidebar() {
  const q = S.filter.toLowerCase();
  const list = S.convs.filter(c => !q || c.title.toLowerCase().includes(q)).sort((a, b) => b.updated - a.updated);
  if (!list.length) { $('#convs').innerHTML = `<div class="convs-empty">${q ? 'Ничего не найдено' : 'Здесь появятся твои разговоры'}</div>`; return; }
  const day = 864e5, now = new Date(); now.setHours(0, 0, 0, 0);
  const group = t => t >= +now ? 'Сегодня' : t >= +now - day ? 'Вчера' : t >= +now - 7 * day ? 'На этой неделе' : 'Раньше';
  let html = '', g = '';
  for (const c of list) {
    const gg = group(c.updated);
    if (gg !== g) { html += `<div class="conv-group">${gg}</div>`; g = gg; }
    html += `<div class="conv ${c.mode} ${c.id === S.currentId ? 'active' : ''}" data-id="${c.id}">
      <span class="ci">${I[c.mode]}</span><span class="ct">${esc(c.title || 'Без названия')}</span>
      <button class="cdel" data-del="${c.id}" title="Удалить">${I.trash}</button></div>`;
  }
  $('#convs').innerHTML = html;
}
$('#convs').onclick = e => {
  const d = e.target.closest('[data-del]'); if (d) { e.stopPropagation(); deleteConv(d.dataset.del); return; }
  const c = e.target.closest('[data-id]'); if (c) selectConv(c.dataset.id);
};
$('#search').oninput = e => { S.filter = e.target.value; renderSidebar(); };
$('#newBtn').onclick = newConv;

// ---------------------------------------------------------------- отрисовка сообщений
const isErr = r => /^(ошибка|таймаут)/i.test(r.trim()) || /ОШИБКА|НЕ открыта|не найдена/.test(r.slice(0, 200));
function toolSummary(p) {
  const a = p.args || {};
  return a.command || a.path || a.query || a.url || a.target || a.keys || a.title || a.question || a.text || (a.code ? a.code.split('\n')[0] : '') || '';
}
function partHTML(p, i, live, isLast) {
  if (p.type === 'think') {
    const liveT = live && isLast;
    return `<details class="think ${liveT ? 'live' : ''}" ${liveT || p.open ? 'open' : ''} data-think="${i}">
      <summary>${I.spark}<span>${liveT ? 'Размышляю' : 'Ход мыслей'}</span>${I.chev}</summary>
      <div class="think-body">${esc(p.text.trim())}</div></details>`;
  }
  if (p.type === 'text') {
    let t = p.text.replace(/<tool>[\s\S]*?(<\/tool>|$)/g, '');
    return t.trim() ? `<div class="md">${renderMarkdown(t)}${live && isLast ? '<span class="caret"></span>' : ''}</div>` : '';
  }
  if (p.type === 'tool') {
    const st = { run: ['run', 'выполняется'], wait: ['wait', 'ждёт разрешения'], ok: ['ok', 'готово'], err: ['err', 'ошибка'], deny: ['deny', 'запрещено'] }[p.status] || ['run', ''];
    const args = JSON.stringify(p.args || {}, null, 2);
    const confirm = p.status === 'wait' && p.confirmId ? `<div class="confirm">
        <p>Агент хочет выполнить <b>${esc(p.name)}</b>. Разрешить?</p>
        <div class="row"><button class="btn primary" data-confirm="${p.confirmId}" data-allow="1">Разрешить</button>
        <button class="btn soft" data-confirm="${p.confirmId}" data-allow="0">Запретить</button>
        <button class="btn always" data-confirm="${p.confirmId}" data-allow="1" data-always="1">Разрешать всё</button></div></div>` : '';
    return `<div class="tool ${p.open || p.status === 'wait' ? 'open' : ''}" data-tool="${i}">
      <div class="tool-head"><span class="tool-ic">${toolIcon(p.name)}</span><span class="tool-name">${esc(p.name)}</span>
      <span class="tool-sum">${esc(toolSummary(p))}</span><span class="tool-state ${st[0]}">${st[1]}</span></div>
      <div class="tool-body"><div class="lbl">Аргументы</div><pre>${esc(args)}</pre>
      ${p.result != null ? `<div class="lbl">Результат</div><pre>${esc(p.result.length > 4000 ? p.result.slice(0, 4000) + '\n…' : p.result)}</pre>` : ''}</div>
      ${p.image ? `<img class="shot" src="${p.image}" alt="скриншот">` : ''}${confirm}</div>`;
  }
  if (p.type === 'info') return `<div class="info">${I.info}${esc(p.text)}</div>`;
  return '';
}
function splitThink(parts) {
  // некоторые модели пишут <think>…</think> прямо в тексте
  const out = [];
  for (const p of parts) {
    if (p.type !== 'text' || !p.text.includes('<think>')) { out.push(p); continue; }
    const re = /<think>([\s\S]*?)(<\/think>|$)/g; let last = 0, m;
    while ((m = re.exec(p.text))) {
      if (m.index > last) out.push({ type: 'text', text: p.text.slice(last, m.index) });
      out.push({ type: 'think', text: m[1] }); last = re.lastIndex;
    }
    if (last < p.text.length) out.push({ type: 'text', text: p.text.slice(last) });
  }
  return out;
}
function aiInner(m, live) {
  const parts = splitThink(m.parts);
  let html = parts.map((p, i) => partHTML(p, m.parts.indexOf(p) >= 0 ? m.parts.indexOf(p) : -1, live, i === parts.length - 1)).join('');
  if (m.error) html += `<div class="err-card">${esc(m.error)}</div>`;
  if (live && !html) html = '<div class="typing"><i></i><i></i><i></i></div>';
  if (!live) {
    const st = m.stats || {};
    const bits = [st.tps && `${st.tps} ток/с`, st.tokens && `${st.tokens} токенов`, st.seconds && `${st.seconds} с`, m.model].filter(Boolean);
    html += `<div class="meta">${bits.map(b => `<span>${esc(String(b))}</span>`).join('·')}
      <button data-act="copy">${I.copy}Копировать</button>${m.canRedo ? `<button data-act="redo">${I.redo}Ещё раз</button>` : ''}</div>`;
  }
  return html;
}
function msgEl(m, idx, live) {
  const el = document.createElement('div');
  el.dataset.idx = idx;
  if (m.role === 'user') {
    el.className = 'msg user';
    el.innerHTML = `<div class="bubble-user">${(m.images || []).map(s => `<img src="${s}" alt="">`).join('')}${esc(m.content)}</div>`;
  } else {
    el.className = 'msg ai' + (live ? ' live' : '');
    el.innerHTML = `<div class="avatar"></div><div class="bubble-ai">${aiInner(m, live)}</div>`;
  }
  return el;
}
function renderThread(jump) {
  const c = current(), th = $('#thread');
  th.innerHTML = '';
  if (c) {
    const lastAi = c.messages.map(m => m.role).lastIndexOf('assistant');
    c.messages.forEach((m, i) => {
      if (m.role === 'assistant') m.canRedo = i === lastAi && c.mode === 'chat';
      th.appendChild(msgEl(m, i, S.busy?.convId === c.id && i === c.messages.length - 1));
    });
  }
  renderHero();
  if (jump) requestAnimationFrame(() => { const s = $('#scroll'); s.style.scrollBehavior = 'auto'; s.scrollTop = s.scrollHeight; s.style.scrollBehavior = ''; });
}

let rafPending = false;
function liveUpdate(conv) {
  if (rafPending) return; rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    if (current()?.id !== conv.id) return;
    const idx = conv.messages.length - 1, el = $(`#thread [data-idx="${idx}"] .bubble-ai`);
    if (!el) return;
    const s = $('#scroll'), stick = s.scrollHeight - s.scrollTop - s.clientHeight < 140;
    const thinkScroll = el.querySelector('.think.live .think-body');
    el.innerHTML = aiInner(conv.messages[idx], S.busy?.convId === conv.id);
    const tb = el.querySelector('.think.live .think-body'); if (tb && thinkScroll) tb.scrollTop = tb.scrollHeight;
    if (stick) { s.style.scrollBehavior = 'auto'; s.scrollTop = s.scrollHeight; s.style.scrollBehavior = ''; }
  });
}

// клики внутри ленты
$('#thread').addEventListener('click', e => {
  const shot = e.target.closest('.shot');
  if (shot) {
    const lb = document.createElement('div'); lb.className = 'lightbox';
    lb.innerHTML = `<img src="${shot.src}">`; lb.onclick = () => lb.remove(); document.body.appendChild(lb); return;
  }
  const cp = e.target.closest('[data-copy]');
  if (cp) { navigator.clipboard.writeText(cp.closest('.code').querySelector('code').textContent); cp.textContent = 'Скопировано'; setTimeout(() => cp.textContent = 'Копировать', 1400); return; }
  const c = current(); if (!c) return;
  const msgNode = e.target.closest('[data-idx]'), m = msgNode && c.messages[+msgNode.dataset.idx];
  const cf = e.target.closest('[data-confirm]');
  if (cf) {
    if (cf.dataset.always) { S.settings.auto = true; $('#autoToggle').checked = true; saveSettings(); toast('Авто-подтверждение включено'); }
    post('/api/agent/confirm', { session: c.id, id: cf.dataset.confirm, allow: cf.dataset.allow === '1' });
    return;
  }
  const th = e.target.closest('.tool-head');
  if (th && m) { const p = m.parts[+th.parentElement.dataset.tool]; if (p) { p.open = !th.parentElement.classList.contains('open'); th.parentElement.classList.toggle('open', p.open); } return; }
  const act = e.target.closest('[data-act]');
  if (act && m) {
    if (act.dataset.act === 'copy') { navigator.clipboard.writeText(m.parts.filter(p => p.type === 'text').map(p => p.text).join('\n').trim()); toast('Ответ скопирован'); }
    if (act.dataset.act === 'redo' && !S.busy) { c.messages.pop(); renderThread(); runTurn(c); }
  }
});
$('#thread').addEventListener('toggle', e => {
  const d = e.target.closest?.('[data-think]'); if (!d || d.classList.contains('live')) return;
  const c = current(), m = c?.messages[+d.closest('[data-idx]').dataset.idx];
  const p = m?.parts[+d.dataset.think]; if (p) p.open = d.open;
}, true);

// ---------------------------------------------------------------- отправка
function appendText(m, type, chunk) {
  const last = m.parts[m.parts.length - 1];
  if (last && last.type === type) last.text += chunk; else m.parts.push({ type, text: chunk });
}
function setBusy(b) {
  S.busy = b;
  document.body.classList.toggle('busy', !!b);
  Sky.setActivity(b ? 0.55 : 0);
  updateSend();
}
function updateSend() { $('#sendBtn').disabled = !S.busy && !$('#input').value.trim() && !S.attachments.length; }

async function send() {
  if (S.busy) return stop();
  const input = $('#input'), text = input.value.trim();
  if (!text && !S.attachments.length) return;
  if (!model()) { toast('Сначала выбери модель'); $('#modelPicker').classList.add('open'); return; }
  let c = current();
  if (!c || c.mode !== S.settings.mode) { newConv(); c = current(); }
  if (S.draft && S.draft.id === c.id) { S.convs.push(c); S.draft = null; }
  if (!c.title) c.title = text.slice(0, 60) || 'Изображение';
  const um = { role: 'user', content: text };
  if (S.attachments.length) um.images = S.attachments.splice(0);
  renderAttachments();
  c.messages.push(um);
  input.value = ''; autoGrow();
  renderSidebar();
  runTurn(c);
}

async function runTurn(c) {
  const am = { role: 'assistant', parts: [], model: model() };
  c.messages.push(am); c.updated = Date.now();
  const ctrl = new AbortController();
  setBusy({ convId: c.id, abort: ctrl, mode: c.mode });
  renderThread(true); Sky.pulse();
  try {
    if (c.mode === 'agent') await runAgent(c, am, ctrl); else await runChat(c, am, ctrl);
  } catch (e) {
    if (e.name !== 'AbortError') am.error = String(e.message || e);
  }
  if (!am.parts.length && !am.error && ctrl.signal.aborted) am.parts.push({ type: 'info', text: 'Остановлено' });
  for (const p of am.parts) if (p.type === 'tool' && (p.status === 'run' || p.status === 'wait')) p.status = 'deny';
  setBusy(null);
  c.updated = Date.now(); saveConvs(true); renderSidebar();
  if (current()?.id === c.id) renderThread();
}

async function runChat(c, am, ctrl) {
  const msgs = [];
  if (S.settings.system.trim()) msgs.push({ role: 'system', content: S.settings.system.trim() });
  for (const m of c.messages.slice(0, -1)) {
    if (m.role === 'user') {
      const o = { role: 'user', content: m.content };
      if (m.images?.length) o.images = m.images.map(s => s.split(',')[1]);
      msgs.push(o);
    } else {
      const t = m.parts.filter(p => p.type === 'text').map(p => p.text).join('');
      if (t) msgs.push({ role: 'assistant', content: t });
    }
  }
  const mi = S.models.find(m => m.name === am.model);
  const body = { model: am.model, messages: msgs, options: { temperature: S.settings.temp, num_ctx: S.settings.ctx } };
  if (mi?.caps.includes('thinking')) body.think = S.settings.think;
  const r = await post('/api/chat', body, ctrl.signal);
  for await (const ev of ndjson(r)) {
    if (ev.type === 'token') { appendText(am, 'text', ev.data); Sky.setActivity(1); }
    else if (ev.type === 'think') { appendText(am, 'think', ev.data); Sky.setActivity(0.8); }
    else if (ev.type === 'stats') am.stats = ev.data;
    else if (ev.type === 'error') am.error = ev.data;
    liveUpdate(c); saveConvs();
  }
}

async function runAgent(c, am, ctrl) {
  const history = [];
  for (const m of c.messages.slice(0, -2)) {
    if (m.role === 'user') history.push({ role: 'user', content: m.content });
    else { const t = m.parts.filter(p => p.type === 'text').map(p => p.text).join('').trim(); if (t) history.push({ role: 'assistant', content: t }); }
  }
  const text = c.messages[c.messages.length - 2].content;
  const r = await post('/api/agent/run', { session: c.id, model: am.model, text, auto: S.settings.auto, history, visionModel: S.settings.vision }, ctrl.signal);
  if (r.status === 409) { am.error = 'Агент уже работает в этом разговоре'; return; }
  const lastTool = name => [...am.parts].reverse().find(p => p.type === 'tool' && p.name === name && p.result == null);
  for await (const ev of ndjson(r)) {
    const d = ev.data;
    switch (ev.type) {
      case 'token': appendText(am, 'text', d); Sky.setActivity(1); break;
      case 'think': appendText(am, 'think', d); Sky.setActivity(0.8); break;
      case 'tool': am.parts.push({ type: 'tool', name: d.name, args: d.args, status: 'run' }); Sky.pulse(); break;
      case 'confirm': { const t = lastTool(d.name); if (t) { t.status = 'wait'; t.confirmId = d.id; } Sky.setActivity(0.25); break; }
      case 'confirmed': { const t = am.parts.find(p => p.confirmId === d.id); if (t) { t.status = d.allow ? 'run' : 'deny'; delete t.confirmId; } break; }
      case 'result': { const t = lastTool(d.name); if (t) { t.result = d.result; if (t.status !== 'deny') t.status = isErr(d.result) ? 'err' : 'ok'; } break; }
      case 'info': am.parts.push({ type: 'info', text: d }); break;
      case 'image': {
        const t = [...am.parts].reverse().find(p => p.type === 'tool' && p.name === 'look_at_screen');
        if (t) shrink(d).then(u => { t.image = u; liveUpdate(c); saveConvs(); });
        break;
      }
      case 'error': am.error = d; break;
      case 'end': am.stats = { seconds: d.seconds }; break;
    }
    liveUpdate(c); saveConvs();
  }
}

// уменьшить скриншот, чтобы история не переполняла хранилище
function shrink(b64, w = 720) {
  return new Promise(res => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, w / img.width), cv = document.createElement('canvas');
      cv.width = img.width * k; cv.height = img.height * k;
      cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
      res(cv.toDataURL('image/jpeg', 0.8));
    };
    img.onerror = () => res('');
    img.src = 'data:image/jpeg;base64,' + b64;
  });
}

function stop() {
  const b = S.busy; if (!b) return;
  if (b.mode === 'agent') post('/api/agent/stop', { session: b.convId }).catch(() => {});
  b.abort.abort();
}

// ---------------------------------------------------------------- композер
const input = $('#input');
function autoGrow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 220) + 'px'; updateSend(); }
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!S.busy) send(); }
});
$('#sendBtn').onclick = send;

function addFiles(files) {
  if (S.settings.mode !== 'chat') return toast('Картинки доступны в режиме чата');
  const mi = S.models.find(m => m.name === model());
  if (mi && !mi.caps.includes('vision')) toast('Эта модель не видит изображения — выбери модель с меткой «зрение»');
  for (const f of files) {
    if (!f.type.startsWith('image/')) continue;
    const fr = new FileReader();
    fr.onload = () => { S.attachments.push(fr.result); renderAttachments(); };
    fr.readAsDataURL(f);
  }
}
function renderAttachments() {
  $('#attachments').innerHTML = S.attachments.map((s, i) => `<div class="att"><img src="${s}" alt=""><button data-rm="${i}">${I.x}</button></div>`).join('');
  updateSend();
}
$('#attachments').onclick = e => { const b = e.target.closest('[data-rm]'); if (b) { S.attachments.splice(+b.dataset.rm, 1); renderAttachments(); } };
$('#attachBtn').onclick = () => $('#file').click();
$('#file').onchange = e => { addFiles(e.target.files); e.target.value = ''; };
input.addEventListener('paste', e => { const f = [...e.clipboardData.files]; if (f.length) { e.preventDefault(); addFiles(f); } });
let dragN = 0;
addEventListener('dragenter', e => { if (e.dataTransfer.types.includes('Files') && S.settings.mode === 'chat') { dragN++; $('#drop').classList.add('show'); } });
addEventListener('dragleave', () => { if (--dragN <= 0) { dragN = 0; $('#drop').classList.remove('show'); } });
addEventListener('dragover', e => e.preventDefault());
addEventListener('drop', e => { e.preventDefault(); dragN = 0; $('#drop').classList.remove('show'); addFiles(e.dataTransfer.files); });

// ---------------------------------------------------------------- настройки
function closeMenus(except) {
  for (const sel of ['#modelPicker', '#settings']) if (sel !== except) $(sel).classList.remove('open');
}
$('#settingsBtn').onclick = e => { e.stopPropagation(); closeMenus('#settings'); $('#settings').classList.toggle('open'); };
$('#settings').onclick = e => e.stopPropagation();
document.addEventListener('click', () => closeMenus());
const bindRange = (id, key, fmt) => {
  const el = $('#' + id), out = $('#' + id + 'Val');
  el.value = S.settings[key]; out.textContent = fmt(S.settings[key]);
  el.oninput = () => { S.settings[key] = +el.value; out.textContent = fmt(+el.value); saveSettings(); };
};
bindRange('temp', 'temp', v => v.toFixed(2));
bindRange('ctx', 'ctx', v => v.toLocaleString('ru-RU'));
$('#think').checked = S.settings.think;
$('#think').onchange = e => { S.settings.think = e.target.checked; saveSettings(); };
$('#system').value = S.settings.system;
$('#system').oninput = e => { S.settings.system = e.target.value; saveSettings(); };
$('#autoToggle').checked = S.settings.auto;
$('#autoToggle').onchange = e => { S.settings.auto = e.target.checked; saveSettings(); toast(e.target.checked ? 'Агент выполняет действия без вопросов' : 'Агент будет спрашивать разрешение'); };
$('#menuBtn').onclick = e => { e.stopPropagation(); document.body.classList.toggle('side-open'); };
$('#sidebar').onclick = e => e.stopPropagation();
document.addEventListener('click', () => document.body.classList.remove('side-open'));

addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); newConv(); }
  if (e.key === 'Escape') { if (S.busy) stop(); closeMenus(); }
});

let toastT;
function toast(t) { const el = $('#toast'); el.textContent = t; el.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove('show'), 2600); }

// ---------------------------------------------------------------- сцены и качество
function applyTheme(name) {
  S.settings.theme = name; saveSettings(); Sky.setTheme(name);
  document.querySelectorAll('.theme-tile').forEach(b => b.classList.toggle('active', b.dataset.theme === name));
}
$('#themes').onclick = e => { const b = e.target.closest('[data-theme]'); if (b) applyTheme(b.dataset.theme); };
function applyQuality(q) {
  S.settings.quality = q; saveSettings(); Sky.setQuality(q);
  document.querySelectorAll('#quality button').forEach(b => b.classList.toggle('active', +b.dataset.q === q));
}
$('#quality').onclick = e => { const b = e.target.closest('[data-q]'); if (b) applyQuality(+b.dataset.q); };

// ---------------------------------------------------------------- CLI
$('#cliBtn').onclick = async () => {
  if (S.busy) stop();
  try { await post('/api/open-cli', { model: S.settings.modelAgent, auto: S.settings.auto }); }
  catch { return toast('Не удалось открыть CLI'); }
  $('#bye').classList.add('show');
  setTimeout(() => window.close(), 1200);
};

// ---------------------------------------------------------------- старт
if (new URLSearchParams(location.search).has('desktop')) document.body.classList.add('desktop');
applyTheme(S.settings.theme);
applyQuality(S.settings.quality);
applyMode(S.settings.mode);
const lastConv = S.convs.filter(c => c.mode === S.settings.mode).sort((a, b) => b.updated - a.updated)[0];
if (lastConv) selectConv(lastConv.id); else newConv();
// незавершённые карточки после перезагрузки страницы
for (const c of S.convs) for (const m of c.messages) for (const p of m.parts || []) if (p.type === 'tool' && (p.status === 'run' || p.status === 'wait')) p.status = 'deny';
checkHealth(); loadModels();
setInterval(checkHealth, 15000);
requestAnimationFrame(placePill);
document.fonts?.ready.then(placePill);
updateSend();
