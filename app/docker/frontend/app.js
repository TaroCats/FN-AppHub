/* AppHub 前端逻辑 */
const state = {
  apps: [],
  groups: [],
  filter: '全部',
  query: '',
  statuses: {},
  scanItems: [],
  editingId: null,
  current: null,
  groupList: [],
  baseHost: '',
  hostSynced: false,
  selfPort: '',
};


const $ = (sel) => document.querySelector(sel);
const grid = $('#grid');
const chips = $('#chips');
const statsBar = $('#stats');
const emptyBox = $('#empty');
const toastEl = $('#toast');

/* ---------------- 工具函数 ---------------- */
function toast(message, ms = 2200) {
  toastEl.textContent = message;
  toastEl.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toastEl.hidden = true; }, ms);
}

function escapeHtml(str) {
  return String(str || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* 复制文本：非安全上下文（http + IP 访问）下 navigator.clipboard 不可用，回退 execCommand */
async function copyText(text) {
  const value = String(text || '');
  if (!value) throw new Error('内容为空');
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (e) { /* 继续走回退 */ }
  }
  const ta = document.createElement('textarea');
  ta.value = value;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '-9999px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, value.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  document.body.removeChild(ta);
  if (!ok) throw new Error('浏览器拒绝写入剪贴板');
  return true;
}

function hostOf(url) {
  try {
    const u = new URL(url);
    return u.host;
  } catch (e) {
    return url;
  }
}

/* 卡片上的地址文案：
   - 走统一配置主机（url_mode=auto）的应用只显示端口 + 主页路径
   - 自定义完整地址的应用全量显示（含协议、主机、端口、主页路径） */
function isUnifiedAddr(app) {
  if (!app) return false;
  const mode = app.url_mode || (app.port ? 'auto' : 'custom');
  return mode === 'auto';
}

function addrLabel(app) {
  if (!app) return '';
  const path = homePath(app);
  if (isUnifiedAddr(app)) {
    const p = parseAddr(app.url);
    const port = String(app.port || p.port || '').trim();
    const label = port ? `:${port}` : hostOf(app.url);
    return `${label}${path}`;
  }
  const p = parseAddr(app.url);
  if (!p.host) return openUrl(app);
  return `${p.scheme}://${p.host}${p.port ? ':' + p.port : ''}${path}`;
}

/* ---- 访问地址（主机）工具 ---- */
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '0.0.0.0', '::1', '[::1]']);
const HOST_KEY = 'apphub.host';
/* 统一配置应用直接用局域网 IP 访问：不再有域名、hosts、mDNS 与反代 */
const IPV4_RE = /^\d{1,3}(\.\d{1,3}){3}$/;
function isIpv4(host) {
  return IPV4_RE.test(String(host || '').trim());
}

/* 统一配置里只认 IPv4；没配置或存了域名时，退回当前访问 fnOS 的 host */
function accessHost() {
  const saved = String(state.baseHost || '').trim();
  return isIpv4(saved) ? saved : currentHost();
}

function urlHostname(url) {
  const raw = String(url || '').trim();
  if (!raw) return '';
  try {
    const u = new URL(/^[a-zA-Z][\w+.\-]*:\/\//.test(raw) ? raw : `http://${raw}`);
    return u.hostname;
  } catch (e) {
    return '';
  }
}

function withHost(url, host) {
  const raw = String(url || '').trim();
  if (!raw || !host) return raw;
  let u;
  try {
    u = new URL(/^[a-zA-Z][\w+.\-]*:\/\//.test(raw) ? raw : `http://${raw}`);
  } catch (e) {
    return raw;
  }
  let target = host;
  if (target.includes(':') && !target.startsWith('[')) target = `[${target}]`;
  if (u.port) target = `${target}:${u.port}`;
  if (u.username) target = `${u.username}${u.password ? `:${u.password}` : ''}@${target}`;
  const path = u.pathname === '/' ? '' : u.pathname;
  return `${u.protocol}//${target}${path}${u.search}${u.hash}`;
}

function initial(name) {
  return (name || '?').trim().charAt(0).toUpperCase();
}

function colorOf(app) {
  return app.color || ['#4f7cff', '#7c5cff', '#0ea5e9', '#14b8a6', '#22c55e', '#f59e0b'][app.name.length % 6];
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = '请求失败';
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}

/* ---------------- 渲染 ---------------- */
function visibleApps() {
  const q = state.query.trim().toLowerCase();
  return state.apps.filter((app) => {
    if (state.filter !== '全部' && app.group_name !== state.filter) return false;
    if (!q) return true;
    return [app.name, app.url, app.group_name, app.note].join(' ').toLowerCase().includes(q);
  });
}

function renderChips() {
  const counts = {};
  state.apps.forEach((app) => { counts[app.group_name] = (counts[app.group_name] || 0) + 1; });
  const order = (state.groups || []).filter((g) => g !== '全部');
  const names = order.filter((g) => counts[g])
    .concat(Object.keys(counts).filter((g) => !order.includes(g)));
  const groups = ['全部', ...names];
  chips.innerHTML = groups.map((group) => {
    const n = group === '全部' ? state.apps.length : counts[group];
    const active = state.filter === group ? ' active' : '';
    return `<button class="chip${active}" data-group="${escapeHtml(group)}">${escapeHtml(group)}<span class="n">${n}</span></button>`;
  }).join('');
  chips.querySelectorAll('.chip').forEach((el) => {
    el.addEventListener('click', () => {
      state.filter = el.dataset.group;
      renderChips();
      renderGrid();
    });
  });
}

function renderStats() {
  const online = Object.values(state.statuses).filter(Boolean).length;
  statsBar.innerHTML = `<span>共 <b>${state.apps.length}</b> 个应用</span><span>在线 <b>${online}</b></span><span>分组 <b>${Object.keys(state.groups || {}).length || 1}</b></span>`;
}

function renderGrid() {
  const list = visibleApps();
  grid.innerHTML = list.map((app) => {
    const status = state.statuses[app.id];
    const dotClass = status === true ? '' : (status === false ? ' off' : ' unknown');
    const color = colorOf(app);
    const icon = app.icon_url
      ? `<img src="${app.icon_url}?v=${Math.floor(app.updated_at)}" alt="" onerror="this.classList.add('hidden')">`
      : '';
    return `
      <article class="card" data-id="${app.id}">
        <div class="card-icon" style="--c:${color}">
          ${icon}<span>${escapeHtml(initial(app.name))}</span>
          <i class="dot${dotClass}"></i>
        </div>
        <div class="card-body">
          <h3 title="${escapeHtml(app.name)}">${escapeHtml(app.name)}</h3>
          <p title="${escapeHtml(openUrl(app))}" class="${isUnifiedAddr(app) ? 'unified' : 'full'}">${escapeHtml(addrLabel(app))}${app.note ? ' · ' + escapeHtml(app.note) : ''}</p>
        </div>
        <button class="card-more" title="更多">
          <svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/></svg>
        </button>
      </article>`;
  }).join('');

  emptyBox.hidden = list.length !== 0;
  grid.hidden = list.length === 0;
  renderStats();
  bindCards();
}

const cardMenu = document.getElementById('card-menu');
const MENU_ITEMS = [
  ['open', '打开', '<path d="M5 12h14M13 6l6 6-6 6"/>'],
  ['newtab', '新窗口打开', '<path d="M14 5h5v5"/><path d="M19 5l-8 8"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>'],
  ['top', '置顶', '<path d="M12 19V5M6 11l6-6 6 6"/>'],
  ['edit', '编辑', '<path d="M4 20h4l10-10-4-4L4 16v4z"/>'],
  ['delete', '删除', '<path d="M5 7h14M9 7V5h6v2M7 7l1 12h8l1-12"/>'],
];
let menuApp = null;

function closeCardMenu() {
  cardMenu.classList.remove('open');
  menuApp = null;
}

function openCardMenu(app, anchor) {
  menuApp = app;
  cardMenu.innerHTML = MENU_ITEMS.map(([act, label, path]) =>
    `<button data-act="${act}"${act === 'delete' ? ' class="danger"' : ''}><svg viewBox="0 0 24 24">${path}</svg>${label}</button>`).join('');
  cardMenu.classList.add('open');
  const r = anchor.getBoundingClientRect();
  const mw = cardMenu.offsetWidth;
  const mh = cardMenu.offsetHeight;
  let left = Math.max(8, Math.min(r.right - mw, window.innerWidth - mw - 8));
  let top = r.bottom + 6;
  if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 6);
  cardMenu.style.left = `${left}px`;
  cardMenu.style.top = `${top}px`;
}

cardMenu.addEventListener('click', (event) => {
  const btn = event.target.closest('button[data-act]');
  if (!btn) return;
  event.stopPropagation();
  const app = menuApp;
  closeCardMenu();
  if (app) handleAction(btn.dataset.act, app);
});

window.addEventListener('scroll', closeCardMenu, true);
window.addEventListener('resize', closeCardMenu);

function bindCards() {
  closeCardMenu();
  grid.querySelectorAll('.card').forEach((card) => {
    const id = Number(card.dataset.id);
    const app = state.apps.find((a) => a.id === id);
    if (!app) return;

    card.addEventListener('click', (event) => {
      if (event.target.closest('.card-more')) return;
      openApp(app);
    });

    const more = card.querySelector('.card-more');
    more.addEventListener('click', (event) => {
      event.stopPropagation();
      const reopening = cardMenu.classList.contains('open') && menuApp === app;
      closeCardMenu();
      if (!reopening) openCardMenu(app, more);
    });
  });
}

document.addEventListener('click', () => closeCardMenu());

/* ---------------- 操作 ---------------- */
async function handleAction(action, app) {
  if (action === 'open') return openApp(app);
  if (action === 'newtab') return window.open(openUrl(app), '_blank', 'noopener');
  if (action === 'top') return pinApp(app);
  if (action === 'edit') return openModal(app);
  if (action === 'delete') return removeApp(app);
}

async function pinApp(app) {
  const rest = state.apps.filter((a) => a.id !== app.id).map((a) => a.id);
  try {
    await api('/api/apps/reorder', { method: 'POST', body: JSON.stringify({ ids: [app.id, ...rest] }) });
    await load();
    toast('已置顶');
  } catch (err) {
    toast(err.message);
  }
}

async function removeApp(app) {
  if (!confirm(`确定从列表中删除「${app.name}」？\n仅移除访问入口，不会影响容器本身。`)) return;
  try {
    await api(`/api/apps/${app.id}`, { method: 'DELETE' });
    await load();
    toast('已删除');
  } catch (err) {
    toast(err.message);
  }
}

/* ---------------- 内嵌多窗口浏览 ---------------- */
/* 三种布局：
   - solo  全屏：当前窗口占满舞台，其余隐藏（多窗口时用顶部标签切换）
   - split 分屏：所有窗口平铺，2 个左右各半，3 个左二右一，更多按列均分
   - float 缩小：窗口变成可拖动、可缩放、可叠放的浮动窗 */

const workspace = $('#workspace');
const wsStage = $('#ws-stage');
const wsTabs = $('#ws-tabs');
const WS_LAYOUT_KEY = 'apphub.ws.layout';
const MIN_W = 260;
const MIN_H = 180;

const ws = {
  wins: [],
  activeId: null,
  layout: 'solo',
  seq: 0,
  picker: false,
  pickerIndex: -1,
  zTop: 0,
  split: null,
};

function wsWin(id) {
  return ws.wins.find((w) => w.id === id) || null;
}

function wsActive() {
  return wsWin(ws.activeId);
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

function readLayout() {
  const saved = localStorage.getItem(WS_LAYOUT_KEY);
  return ['solo', 'split', 'float'].includes(saved) ? saved : 'solo';
}

/* ---- 打开 / 关闭窗口 ---- */
function openApp(app) {
  /* 打开方式=新窗口：直接新标签页；打开方式=内嵌：进多窗口工作区 */
  if ((app.open_mode || 'inline') === 'window') {
    window.open(openUrl(app), '_blank', 'noopener');
    return;
  }
  wsOpen(app);
}

function wsOpen(app) {
  if (!app) return;
  workspace.hidden = false;
  document.body.style.overflow = 'hidden';
  wsEmptyHint();

  const exist = ws.wins.find((w) => w.app.id === app.id);
  if (exist) {
    ws.activeId = exist.id;
    renderTabs();
    applyLayout();
    return;
  }

  const win = buildWindow(app);
  ws.wins.push(win);
  ws.activeId = win.id;
  loadWin(win);
  renderTabs();
  applyLayout();
  closePicker();
}

function buildWindow(app) {
  const id = `w${++ws.seq}`;
  const color = colorOf(app);
  const el = document.createElement('div');
  el.className = 'win';
  el.dataset.id = id;
  el.innerHTML = `
    <div class="win-head">
      <span class="win-dot" style="background:${color}">${escapeHtml(initial(app.name))}</span>
      <span class="win-name">${escapeHtml(app.name)}</span>
      <span class="win-url"></span>
      <div class="win-ops">
        <button class="icon-btn" data-act="reload" title="刷新">
          <svg viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 6.3"/><path d="M20 5v6h-6"/></svg>
        </button>
        <button class="icon-btn" data-act="max" title="该窗口全屏">
          <svg viewBox="0 0 24 24"><path d="M4 9V5h4M20 9V5h-4M4 15v4h4M20 15v4h-4"/></svg>
        </button>
        <button class="icon-btn" data-act="newtab" title="在新窗口打开">
          <svg viewBox="0 0 24 24"><path d="M14 5h5v5"/><path d="M19 5l-8 8"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/></svg>
        </button>
        <button class="icon-btn" data-act="close" title="关闭窗口">
          <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>
        </button>
      </div>
    </div>
    <div class="win-body">
      <div class="win-hint" hidden>
        <span>页面加载较慢或被站点拒绝内嵌：用右上角「新窗口」在浏览器里直接打开。</span>
        <button class="mini" data-act="newtab">新窗口打开</button>
      </div>
    </div>
    <span class="win-grip" title="拖动缩放"></span>`;

  const win = {
    id,
    app,
    el,
    url: openUrl(app),
    hint: el.querySelector('.win-hint'),
    body: el.querySelector('.win-body'),
    frame: null,
    timer: 0,
    rect: null,
  };
  el.querySelector('.win-url').textContent = win.url;
  wsStage.appendChild(el);

  el.addEventListener('mousedown', () => activate(id, false));
  el.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-act]');
    if (btn) {
      event.stopPropagation();
      winAction(btn.dataset.act, win);
      return;
    }
    activate(id, false);
  });
  /* 浮动窗：拖标题栏移动、双击标题栏放大 */
  const head = el.querySelector('.win-head');
  head.addEventListener('dblclick', (event) => {
    if (event.target.closest('button')) return;
    if (ws.layout !== 'float') return;
    setLayout('solo');
    activate(id, true);
  });
  head.addEventListener('pointerdown', (event) => beginDrag(event, win));
  head.addEventListener('mousedown', (event) => beginDrag(event, win));
  const grip = el.querySelector('.win-grip');
  grip.addEventListener('pointerdown', (event) => beginResize(event, win));
  grip.addEventListener('mousedown', (event) => beginResize(event, win));

  return win;
}

function winAction(act, win) {
  if (act === 'reload') return loadWin(win);
  if (act === 'newtab') return window.open(win.url, '_blank', 'noopener');
  if (act === 'max') {
    ws.activeId = win.id;
    setLayout('solo');
    renderTabs();
    applyLayout();
    return;
  }
  if (act === 'close') return closeWin(win.id);
}

function closeWin(id) {
  const index = ws.wins.findIndex((w) => w.id === id);
  if (index < 0) return;
  const win = ws.wins[index];
  clearTimeout(win.timer);
  if (win.frame) {
    win.frame.src = 'about:blank';
    win.frame.remove();
  }
  win.el.remove();
  ws.wins.splice(index, 1);
  if (ws.activeId === id) {
    const next = ws.wins[Math.min(index, ws.wins.length - 1)];
    ws.activeId = next ? next.id : null;
  }
  if (!ws.wins.length) {
    closeWorkspace();
    return;
  }
  renderTabs();
  applyLayout();
}

function closeWorkspace() {
  ws.wins.forEach((w) => {
    clearTimeout(w.timer);
    if (w.frame) w.frame.src = 'about:blank';
  });
  ws.wins = [];
  ws.activeId = null;
  wsStage.innerHTML = '';
  wsTabs.innerHTML = '';
  workspace.hidden = true;
  document.body.style.overflow = '';
  closePicker();
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}

function wsEmptyHint() {
  $('#ws-empty').hidden = ws.wins.length > 0;
}

/* ---- 加载 iframe ---- */
function loadWin(win) {
  if (!win) return;
  win.hint.hidden = true;
  const src = win.url;
  if (!win.frame) {
    win.frame = document.createElement('iframe');
    win.frame.title = win.app.name;
    win.frame.setAttribute('allow', 'fullscreen; clipboard-read; clipboard-write');
    win.frame.addEventListener('load', () => {
      win.hint.hidden = true;
      clearTimeout(win.timer);
    });
    win.body.appendChild(win.frame);
  }
  win.frame.src = src;
  clearTimeout(win.timer);
  win.timer = setTimeout(() => { if (wsIsOpen() && wsWin(win.id)) win.hint.hidden = false; }, 8000);
  if (/^https?:\/\//i.test(src)) {
    probeUrl(src).then((ok) => {
      if (!ok && wsIsOpen() && wsWin(win.id)) win.hint.hidden = false;
    });
  }
}

/* 探活直连地址：no-cors 下只要能建立连接就算通 */
async function probeUrl(url) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2500);
  try {
    await fetch(url, { mode: 'no-cors', cache: 'no-store', signal: ctrl.signal });
    return true;
  } catch (err) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

/* ---- 标签栏 ---- */
function renderTabs() {
  wsTabs.innerHTML = ws.wins.map((w) => {
    const active = w.id === ws.activeId ? ' active' : '';
    const name = escapeHtml(w.app.name);
    return `<div class="ws-tab${active}" data-id="${w.id}" role="tab" tabindex="0"
        aria-selected="${w.id === ws.activeId}" title="${name}">
      <span class="tab-dot" style="background:${colorOf(w.app)}">${escapeHtml(initial(w.app.name))}</span>
      <span class="tab-name">${name}</span>
      <button type="button" class="tab-x" data-close="${w.id}" title="关闭 ${name}" aria-label="关闭 ${name}">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>
      </button>
    </div>`;
  }).join('');
  wsEmptyHint();
}

wsTabs.addEventListener('click', (event) => {
  const close = event.target.closest('[data-close]');
  if (close) {
    event.stopPropagation();
    closeWin(close.dataset.close);
    return;
  }
  const tab = event.target.closest('.ws-tab');
  if (tab) activate(tab.dataset.id, true);
});

/* 标签支持键盘：Enter/Space 切换，Delete/Backspace 关闭 */
wsTabs.addEventListener('keydown', (event) => {
  const tab = event.target.closest('.ws-tab');
  if (!tab) return;
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    activate(tab.dataset.id, true);
  } else if (event.key === 'Delete' || event.key === 'Backspace') {
    event.preventDefault();
    closeWin(tab.dataset.id);
  }
});

function activate(id, scrollIntoView) {
  if (!wsWin(id)) return;
  const changed = ws.activeId !== id;
  ws.activeId = id;
  renderTabs();
  /* 浮动模式：激活的窗口必须压在最上面，否则点被遮挡的窗口看不到反应 */
  if (ws.layout === 'float') bumpOrder();
  if (ws.layout === 'solo' || changed) applyLayout();
  if (scrollIntoView) {
    const tab = wsTabs.querySelector(`.ws-tab[data-id="${id}"]`);
    if (tab) tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
}

/* 浮动窗叠放：只抬升激活窗，不动 ws.wins 的顺序（标签栏必须保持打开顺序） */
function bumpOrder() {
  const active = wsWin(ws.activeId);
  if (!active) return;
  ws.zTop = (ws.zTop || ws.wins.length) + 1;
  active.el.style.zIndex = String(ws.zTop);
  ws.wins.forEach((w) => w.el.classList.toggle('active', w === active));
}

/* ---- 布局计算 ---- */
function setLayout(layout) {
  if (!['solo', 'split', 'float'].includes(layout)) return;
  ws.layout = layout;
  localStorage.setItem(WS_LAYOUT_KEY, layout);
  document.querySelectorAll('#ws-layout button').forEach((b) => {
    b.classList.toggle('active', b.dataset.layout === layout);
  });
  applyLayout();
}

function stageSize() {
  return { W: wsStage.clientWidth, H: wsStage.clientHeight };
}

/* 分屏时的列分配：
   列数同时受窗口数与可用宽度限制（每列至少 MIN_W），
   2 个左右各半；3 个左二右一；更多按列均分。
   分屏不留间隔，所以按整宽计算，不再扣 pad/gap。 */
function splitColumns(n, W) {
  const fit = Math.max(1, Math.floor(W / 220));
  let cols = n <= 2 ? n : (n <= 6 ? 2 : 3);
  cols = Math.min(cols, n, fit);
  const base = Math.floor(n / cols);
  const extra = n % cols;
  return Array.from({ length: cols }, (_, c) => base + (c < extra ? 1 : 0));
}

/* ---- 分屏轨道：列宽 / 行高按比例存，分隔线拖动只改比例 ---- */
/* 单格下限：需要容得下标题栏（图标 + 名称 + 四个操作按钮）。
   太小时标题栏会被裁切、按钮点不到，所以按实测给 220px，行高按 160px。 */
const SPLIT_MIN_W = 220;
const SPLIT_MIN_H = 160;

function resetSplitState(cols) {
  ws.split = {
    cols: cols.slice(),
    colRatios: cols.map(() => 1 / cols.length),
    rowRatios: cols.map((count) => Array.from({ length: count }, () => 1 / count)),
  };
}

/* 窗口数变化导致列结构变化时才重置比例，单纯切换布局保留用户调好的尺寸 */
function ensureSplitState(cols) {
  const s = ws.split;
  const same = s && s.cols.length === cols.length && s.cols.every((v, i) => v === cols[i]);
  if (!same) resetSplitState(cols);
}

function splitWidths(W) {
  return ws.split.colRatios.map((r) => r * W);
}

function splitHeights(H, col) {
  return ws.split.rowRatios[col].map((r) => r * H);
}

/* 把比例归一化（防止拖动后累计误差导致窗口溢出/留缝） */
function normalizeRatios(ratios) {
  const total = ratios.reduce((a, b) => a + b, 0) || 1;
  return ratios.map((r) => r / total);
}

function applyLayout() {
  const { W, H } = stageSize();
  const n = ws.wins.length;
  if (!n) return;

  const solo = ws.layout === 'solo';
  wsStage.classList.toggle('float-mode', ws.layout === 'float');
  wsStage.classList.toggle('split-mode', ws.layout === 'split');

  if (!solo) ws.wins.forEach((w) => { w.el.classList.remove('solo'); });
  ws.wins.forEach((w) => {
    w.el.classList.toggle('solo', solo && w.id === ws.activeId);
    w.el.classList.toggle('float', ws.layout === 'float');
    w.el.classList.toggle('active', w.id === ws.activeId);
  });

  if (solo) {
    clearSplitHandles();
    const active = wsActive() || ws.wins[0];
    ws.wins.forEach((w) => {
      const on = w === active;
      w.el.style.display = on ? 'flex' : 'none';
      if (!on) return;
      w.el.style.left = '0px';
      w.el.style.top = '0px';
      w.el.style.width = `${W}px`;
      w.el.style.height = `${H}px`;
    });
    return;
  }

  if (ws.layout === 'split') {
    /* 分屏不留间隔：窗口紧贴铺满整个舞台，分隔线浮在交界处供拖动 */
    const cols = splitColumns(n, W);
    ensureSplitState(cols);
    const widths = splitWidths(W);
    let index = 0;
    let x = 0;
    cols.forEach((count, c) => {
      const heights = splitHeights(H, c);
      let y = 0;
      for (let r = 0; r < count; r += 1) {
        const w = ws.wins[index++];
        if (!w) return;
        w.el.style.display = 'flex';
        w.el.style.left = `${x}px`;
        w.el.style.top = `${y}px`;
        w.el.style.width = `${widths[c]}px`;
        w.el.style.height = `${heights[r]}px`;
        y += heights[r];
      }
      x += widths[c];
    });
    renderSplitHandles(W, H);
    return;
  }

  clearSplitHandles();

  /* float：恢复各自的位置与尺寸，首次出现时级联摆放 */
  ws.wins.forEach((w, i) => {
    w.el.style.display = 'flex';
    if (!w.rect) {
      const fw = Math.max(MIN_W, Math.round(W * 0.64));
      const fh = Math.max(MIN_H, Math.round(H * 0.7));
      const maxX = Math.max(8, W - fw - 8);
      const maxY = Math.max(8, H - fh - 8);
      w.rect = {
        x: clamp(16 + i * 30, 8, maxX),
        y: clamp(14 + i * 26, 8, maxY),
        w: fw,
        h: fh,
      };
    }
    w.rect = clipRect(w.rect);
    w.el.style.left = `${w.rect.x}px`;
    w.el.style.top = `${w.rect.y}px`;
    w.el.style.width = `${w.rect.w}px`;
    w.el.style.height = `${w.rect.h}px`;
  });
  ws.wins.forEach((w, i) => { w.el.style.zIndex = String(i + 1); });
  ws.zTop = ws.wins.length;
  bumpOrder();
}

/* ---- 分屏分隔线：拖交界处调整列宽 / 行高 ---- */
const SPLIT_HIT = 10;   /* 命中区厚度（透明，便于抓取） */

function splitLayer() {
  let layer = wsStage.querySelector('.split-layer');
  if (!layer) {
    layer = document.createElement('div');
    layer.className = 'split-layer';
    wsStage.appendChild(layer);
  }
  return layer;
}

function clearSplitHandles() {
  const layer = wsStage.querySelector('.split-layer');
  if (!layer) return;
  layer.innerHTML = '';
  /* 必须同步清掉签名：否则再次进入分屏时签名"看起来没变"，
     会跳过重建，结果是分隔条全没了 */
  delete layer.dataset.sig;
}

/* 建 / 更新分隔条。
   注意：拖动过程中**不能重建**这些元素 —— 正在接收指针事件的那个元素一旦被
   innerHTML 清掉，指针捕获就丢了，后续 pointermove 会落到 iframe 上被吞掉，
   表现为拖到一半卡住。所以这里按需复用已有元素，只改几何属性。 */
function renderSplitHandles(W, H) {
  const layer = splitLayer();
  const s = ws.split;
  if (!s) return;
  const widths = splitWidths(W);

  /* 收集需要的位置：竖向（列之间）+ 横向（每列内部行之间） */
  const specs = [];
  let x = 0;
  for (let c = 0; c < widths.length - 1; c += 1) {
    x += widths[c];
    specs.push({ axis: 'col', c, r: -1, left: x - SPLIT_HIT / 2, top: 0, width: SPLIT_HIT, height: H });
  }
  x = 0;
  for (let c = 0; c < widths.length; c += 1) {
    const heights = splitHeights(H, c);
    let y = 0;
    for (let r = 0; r < heights.length - 1; r += 1) {
      y += heights[r];
      specs.push({ axis: 'row', c, r, left: x, top: y - SPLIT_HIT / 2, width: widths[c], height: SPLIT_HIT });
    }
    x += widths[c];
  }

  /* 复用：元素数量不一致或结构变了才整体重建 */
  const existing = [...layer.children];
  const sig = specs.map((s2) => `${s2.axis}:${s2.c}:${s2.r}`).join('|');
  if (existing.length !== specs.length || layer.dataset.sig !== sig) {
    layer.innerHTML = '';
    specs.forEach((sp) => {
      const el = document.createElement('div');
      el.className = `split-handle ${sp.axis === 'col' ? 'v' : 'h'}`;
      el.title = sp.axis === 'col' ? '拖动调整左右宽度' : '拖动调整上下高度';
      el.dataset.axis = sp.axis;
      el.dataset.c = String(sp.c);
      el.dataset.r = String(sp.r);
      el.addEventListener('pointerdown', (e) => beginSplitDrag(e, sp.axis, sp.c, sp.r));
      el.addEventListener('mousedown', (e) => beginSplitDrag(e, sp.axis, sp.c, sp.r));
      layer.appendChild(el);
    });
    layer.dataset.sig = sig;
  }

  [...layer.children].forEach((el, i) => {
    const sp = specs[i];
    if (!sp) return;
    el.style.left = `${sp.left}px`;
    el.style.top = `${sp.top}px`;
    el.style.width = `${sp.width}px`;
    el.style.height = `${sp.height}px`;
  });
}

let splitDrag = null;

function beginSplitDrag(event, axis, col, row) {
  if (ws.layout !== 'split' || event.button !== 0) return;
  if (splitDrag) return;
  const { W, H } = stageSize();
  splitDrag = axis === 'col'
    ? { axis, col, start: event.clientX, base: splitWidths(W), W, H }
    : { axis, col, row, start: event.clientY, base: splitHeights(H, col), W, H };

  wsStage.classList.add('split-dragging');
  event.preventDefault();
  event.stopPropagation();
  if (event.currentTarget && event.currentTarget.setPointerCapture && event.pointerId !== undefined) {
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch (e) { /* ignore */ }
  }
  window.addEventListener('pointermove', onSplitDrag);
  window.addEventListener('mousemove', onSplitDrag);
  window.addEventListener('pointerup', endSplitDrag, { once: true });
  window.addEventListener('mouseup', endSplitDrag, { once: true });
}

function onSplitDrag(event) {
  if (!splitDrag) return;
  if (event.cancelable) event.preventDefault();
  const { axis, col, row, start, base, W, H } = splitDrag;
  const delta = (axis === 'col' ? event.clientX : event.clientY) - start;

  if (axis === 'col') {
    /* 只动相邻两列：c 变宽 dx，c+1 变窄 dx，其余不变 */
    const cur = base[col];
    const next = base[col + 1];
    const maxGrow = next - SPLIT_MIN_W;
    const minGrow = SPLIT_MIN_W - cur;
    const dx = clamp(delta, minGrow, maxGrow);
    const a = cur + dx;
    const b = next - dx;
    const ratios = base.map((px, i) => {
      if (i === col) return a / W;
      if (i === col + 1) return b / W;
      return px / W;
    });
    ws.split.colRatios = normalizeRatios(ratios);
  } else {
    const cur = base[row];
    const next = base[row + 1];
    const maxGrow = next - SPLIT_MIN_H;
    const minGrow = SPLIT_MIN_H - cur;
    const dy = clamp(delta, minGrow, maxGrow);
    const ratios = base.map((px, i) => {
      if (i === row) return (cur + dy) / H;
      if (i === row + 1) return (next - dy) / H;
      return px / H;
    });
    ws.split.rowRatios[col] = normalizeRatios(ratios);
  }
  relayoutSplit();
}

/* 拖动过程中只重排分屏（不重建 DOM），保持跟手 */
function relayoutSplit() {
  const { W, H } = stageSize();
  const n = ws.wins.length;
  if (!n || ws.layout !== 'split') return;
  const widths = splitWidths(W);
  let index = 0;
  let x = 0;
  ws.split.cols.forEach((count, c) => {
    const heights = splitHeights(H, c);
    let y = 0;
    for (let r = 0; r < count; r += 1) {
      const w = ws.wins[index++];
      if (!w) return;
      w.el.style.left = `${x}px`;
      w.el.style.top = `${y}px`;
      w.el.style.width = `${widths[c]}px`;
      w.el.style.height = `${heights[r]}px`;
      y += heights[r];
    }
    x += widths[c];
  });
  renderSplitHandles(W, H);
}

function endSplitDrag() {
  splitDrag = null;
  wsStage.classList.remove('split-dragging');
  window.removeEventListener('pointermove', onSplitDrag);
  window.removeEventListener('mousemove', onSplitDrag);
}

function clipRect(rect) {
  const { W, H } = stageSize();
  const w = clamp(rect.w, MIN_W, Math.max(MIN_W, W - 8));
  const h = clamp(rect.h, MIN_H, Math.max(MIN_H, H - 8));
  return {
    x: clamp(rect.x, 0, Math.max(0, W - w)),
    y: clamp(rect.y, 0, Math.max(0, H - h)),
    w,
    h,
  };
}

/* ---- 拖动 / 缩放（仅浮动窗） ---- */
/* pointerdown 与 mousedown 会先后触发同一次按下（后者是兼容 MouseEvent 的），
   用 startedAt 去重，避免同一次按下建两份监听互相覆盖起始坐标。 */
let dragState = null;

function beginDrag(event, win) {
  if (ws.layout !== 'float' || event.button !== 0) return;
  if (event.target.closest('button')) return;
  if (!win.rect) return;
  if (dragState && dragState.win === win && dragState.mode === 'move') return;
  activate(win.id, false);
  const { W, H } = stageSize();
  dragState = {
    mode: 'move',
    win,
    startX: event.clientX,
    startY: event.clientY,
    origin: { ...win.rect },
    W,
    H,
  };
  wsStage.classList.add('dragging');
  if (event.currentTarget && event.currentTarget.setPointerCapture && event.pointerId !== undefined) {
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch (e) { /* ignore */ }
  }
  event.preventDefault();
  window.addEventListener('pointermove', onDragMove);
  window.addEventListener('mousemove', onDragMove);
  window.addEventListener('pointerup', endDrag, { once: true });
  window.addEventListener('mouseup', endDrag, { once: true });
}

function beginResize(event, win) {
  if (ws.layout !== 'float' || event.button !== 0) return;
  if (!win.rect) return;
  if (dragState && dragState.win === win && dragState.mode === 'resize') return;
  activate(win.id, false);
  const { W, H } = stageSize();
  dragState = {
    mode: 'resize',
    win,
    startX: event.clientX,
    startY: event.clientY,
    origin: { ...win.rect },
    W,
    H,
  };
  wsStage.classList.add('resizing');
  if (event.currentTarget && event.currentTarget.setPointerCapture && event.pointerId !== undefined) {
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch (e) { /* ignore */ }
  }
  event.preventDefault();
  event.stopPropagation();
  window.addEventListener('pointermove', onDragMove);
  window.addEventListener('mousemove', onDragMove);
  window.addEventListener('pointerup', endDrag, { once: true });
  window.addEventListener('mouseup', endDrag, { once: true });
}

function onDragMove(event) {
  if (!dragState) return;
  if (event.cancelable) event.preventDefault();
  const { mode, win, startX, startY, origin, W, H } = dragState;
  const dx = event.clientX - startX;
  const dy = event.clientY - startY;

  if (mode === 'move') {
    win.rect = {
      ...origin,
      x: clamp(origin.x + dx, 0, Math.max(0, W - origin.w)),
      y: clamp(origin.y + dy, 0, Math.max(0, H - origin.h)),
    };
  } else {
    win.rect = {
      x: origin.x,
      y: origin.y,
      w: clamp(origin.w + dx, MIN_W, Math.max(MIN_W, W - origin.x)),
      h: clamp(origin.h + dy, MIN_H, Math.max(MIN_H, H - origin.y)),
    };
  }
  win.el.style.left = `${win.rect.x}px`;
  win.el.style.top = `${win.rect.y}px`;
  win.el.style.width = `${win.rect.w}px`;
  win.el.style.height = `${win.rect.h}px`;
}

function endDrag() {
  dragState = null;
  wsStage.classList.remove('dragging', 'resizing');
  window.removeEventListener('pointermove', onDragMove);
  window.removeEventListener('mousemove', onDragMove);
}

/* ---- 工具栏事件 ---- */
$('#ws-back').addEventListener('click', closeWorkspace);
$('#ws-close-all').addEventListener('click', () => {
  if (ws.wins.length > 1 && !confirm(`关闭全部 ${ws.wins.length} 个窗口？`)) return;
  closeWorkspace();
});
$('#ws-full').addEventListener('click', () => {
  const target = workspace;
  if (!document.fullscreenElement) {
    (target.requestFullscreen ? target.requestFullscreen() : Promise.resolve()).catch(() => {});
  } else {
    document.exitFullscreen().catch(() => {});
  }
});
$('#ws-add').addEventListener('click', (event) => {
  event.stopPropagation();
  togglePicker();
});
$('#ws-empty-add').addEventListener('click', (event) => {
  event.stopPropagation();
  togglePicker();
});

document.querySelectorAll('#ws-layout button').forEach((btn) => {
  btn.addEventListener('click', () => setLayout(btn.dataset.layout));
});

window.addEventListener('resize', () => {
  if (wsIsOpen()) applyLayout();
});

document.addEventListener('fullscreenchange', () => {
  if (wsIsOpen()) setTimeout(applyLayout, 60);
});

function wsIsOpen() {
  return !workspace.hidden;
}

/* ---- 打开新窗口的选择器 ---- */
function togglePicker() {
  if (ws.picker) return closePicker();
  ws.picker = true;
  workspace.hidden = false;
  document.body.style.overflow = 'hidden';
  const picker = $('#app-picker');
  picker.hidden = false;
  ws.pickerIndex = -1;
  const input = $('#picker-search');
  input.value = '';
  renderPicker('');
  setTimeout(() => input.focus(), 20);
  wsEmptyHint();
}

function closePicker() {
  ws.picker = false;
  ws.pickerIndex = -1;
  $('#app-picker').hidden = true;
}

/* 用户主动关掉选择器（Esc / 点外面）时，若一个窗口都没开就退出工作区 */
function dismissPicker() {
  closePicker();
  if (!ws.wins.length) closeWorkspace();
}

function pickerApps(keyword) {
  const q = String(keyword || '').trim().toLowerCase();
  return state.apps.filter((app) => {
    if (!q) return true;
    return [app.name, app.url, app.group_name, app.note].join(' ').toLowerCase().includes(q);
  });
}

function renderPicker(keyword) {
  const list = $('#picker-list');
  const apps = pickerApps(keyword);
  if (!apps.length) {
    list.innerHTML = '<div class="picker-empty">没有匹配的应用</div>';
    return;
  }
  const opened = new Set(ws.wins.map((w) => w.app.id));
  list.innerHTML = apps.slice(0, 60).map((app, i) => `
    <div class="picker-item${i === ws.pickerIndex ? ' active' : ''}" data-id="${app.id}">
      <span class="pi-dot" style="background:${colorOf(app)}">${escapeHtml(initial(app.name))}</span>
      <span class="pi-name">${escapeHtml(app.name)}</span>
      ${opened.has(app.id) ? '<span class="pi-open">已打开</span>' : ''}
    </div>`).join('');
}

$('#picker-search').addEventListener('input', (event) => {
  ws.pickerIndex = -1;
  renderPicker(event.target.value);
});

$('#picker-search').addEventListener('keydown', (event) => {
  const items = [...document.querySelectorAll('#picker-list .picker-item')];
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!items.length) return;
    const step = event.key === 'ArrowDown' ? 1 : -1;
    ws.pickerIndex = (ws.pickerIndex + step + items.length) % items.length;
    items.forEach((el, i) => el.classList.toggle('active', i === ws.pickerIndex));
    items[ws.pickerIndex].scrollIntoView({ block: 'nearest' });
  } else if (event.key === 'Enter') {
    event.preventDefault();
    if (!items.length) return;
    const target = items[ws.pickerIndex >= 0 ? ws.pickerIndex : 0];
    openFromPicker(Number(target.dataset.id));
  } else if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    dismissPicker();
  }
});

$('#picker-list').addEventListener('click', (event) => {
  const item = event.target.closest('.picker-item');
  if (!item) return;
  openFromPicker(Number(item.dataset.id));
});

function openFromPicker(id) {
  const app = state.apps.find((a) => a.id === id);
  if (!app) return;
  closePicker();
  wsOpen(app);
}

document.addEventListener('click', (event) => {
  if (!ws.picker) return;
  const picker = $('#app-picker');
  if (picker.contains(event.target)) return;
  if (event.target.closest('#ws-add') || event.target.closest('#ws-empty-add')) return;
  dismissPicker();
});

/* ---------------- 访问地址选择 ---------------- */
function currentHost() {
  return location.hostname || '127.0.0.1';
}

/* 统一配置的主机：主面板（访问地址 / 扫描）选定什么 IP 就用什么 */
function unifiedHost() {
  const mode = localStorage.getItem(HOST_KEY) || 'auto';
  if (mode === 'custom') {
    return (localStorage.getItem(`${HOST_KEY}.custom`) || '').trim() || accessHost();
  }
  if (mode === 'auto') return accessHost();
  return mode;
}

function hostForUrl(host) {
  const h = String(host || '').trim();
  if (!h) return '';
  return h.includes(':') && !h.startsWith('[') ? `[${h}]` : h;
}

function parseAddr(url) {
  const raw = String(url || '').trim();
  if (!raw) return { scheme: 'http', host: '', port: '', path: '' };
  try {
    const u = new URL(/^[a-zA-Z][\w+.\-]*:\/\//.test(raw) ? raw : `http://${raw}`);
    const isDefault = (u.protocol === 'http:' && u.port === '80') || (u.protocol === 'https:' && u.port === '443');
    return {
      scheme: u.protocol.replace(':', '') || 'http',
      host: u.hostname,
      port: u.port || (isDefault ? '' : ''),
      path: u.pathname === '/' ? '' : u.pathname,
    };
  } catch (e) {
    return { scheme: 'http', host: '', port: '', path: '' };
  }
}

function homePath(app) {
  const raw = String((app && app.home_path) || '').trim();
  if (!raw || raw === '/') return '';
  return raw.startsWith('/') ? raw : `/${raw}`;
}

/* 应用实际打开地址：基础地址 + 主页路径 */
function openUrl(app) {
  if (!app) return '';
  const base = `${String(app.url || '').replace(/\/+$/, '')}${homePath(app)}`;
  /* 内嵌/新窗口默认走「IP + 端口」：IP 取统一配置（访问地址）里的值，
     默认即当前访问 fnOS 的 host；自定义地址的应用保持用户填写的原地址 */
  if ((app.url_mode || 'auto') !== 'auto') return base;
  return withHost(base, accessHost());
}

/* 内嵌地址与服务端返回的 frame_url 完全一致：就是应用的 IP 直连地址 */
function frameUrlOf(app) {
  if (!app) return 'about:blank';
  return app.frame_url || openUrl(app);
}

function initHostPicker() {
  const sel = $('#scan-host');
  const custom = $('#scan-host-custom');
  sel.options[0].textContent = `统一配置（${unifiedHost()}）`;
  const saved = localStorage.getItem(HOST_KEY) || 'auto';
  if (saved === 'custom') {
    sel.value = 'custom';
    custom.hidden = false;
    custom.value = localStorage.getItem(`${HOST_KEY}.custom`) || '';
  } else if (saved === '127.0.0.1') {
    sel.value = '127.0.0.1';
  } else {
    sel.value = 'auto';
  }
}

function scanHost() {
  const mode = $('#scan-host').value;
  if (mode === 'custom') return ($('#scan-host-custom').value || '').trim() || currentHost();
  return mode === 'auto' ? unifiedHost() : mode;
}

$('#scan-host').addEventListener('change', () => {
  const mode = $('#scan-host').value;
  $('#scan-host-custom').hidden = mode !== 'custom';
  localStorage.setItem(HOST_KEY, mode);
  if (mode === 'custom') return $('#scan-host-custom').focus();
  runScan();
});

$('#scan-host-custom').addEventListener('change', () => {
  localStorage.setItem(`${HOST_KEY}.custom`, $('#scan-host-custom').value.trim());
  runScan();
});

/* ---- 编辑弹窗：访问地址可切换 ---- */
function hostFromMode(mode, customEl) {
  if (mode === 'custom') return (customEl.value || '').trim();
  if (mode === 'auto') return currentHost();
  if (mode === 'keep') return '';
  return mode;
}

function setUrlMode(mode) {
  const auto = mode !== 'custom';
  $('#form-auto-field').hidden = !auto;
  $('#form-custom-field').hidden = auto;
  refreshAddrPreview();
}

function refreshAddrPreview() {
  const host = accessHost();
  $('#form-host-label').textContent = host;
  const port = ($('#form-port').value || '').trim();
  const scheme = $('#form-scheme').value;
  $('#form-url-preview').textContent = port
    ? `将保存为 ${scheme}://${host}:${port}，浏览器直接访问该 IP`
    : `只填端口即可，地址统一为当前局域网 IP:端口（${host}）`;
}

$('#form-url-mode').addEventListener('change', () => {
  const mode = $('#form-url-mode').value;
  if (mode === 'custom') {
    // 切到自定义时，用当前 IP 拼一个初始地址方便修改
    const port = ($('#form-port').value || '').trim();
    const input = $('#form-url');
    if (!input.value.trim() && port) {
      input.value = `${$('#form-scheme').value}://${accessHost()}:${port}`;
    }
  }
  setUrlMode(mode);
});
$('#form-port').addEventListener('input', refreshAddrPreview);
$('#form-scheme').addEventListener('change', refreshAddrPreview);

/* ---- 统一配置的映射 IP ---- */
function bulkHost() {
  return hostFromMode($('#bulk-host').value, $('#bulk-host-custom'));
}

function bulkTargets() {
  /* 统一配置的应用地址 = http://<访问地址>:端口，换 IP 时整体重写 */
  return state.apps.filter((app) => (app.url_mode || 'auto') === 'auto');
}

function refreshBulkPreview() {
  const host = bulkHost();
  const targets = bulkTargets();
  if (!host) {
    $('#host-preview').textContent = '请填写局域网 IP';
  } else {
    $('#host-preview').textContent = `将把 ${targets.length} 个统一配置应用的地址改为 http://${host}:对应端口`
      + '（自定义地址的应用不受影响）';
  }
  $('#host-apply').disabled = !host;
}

function openHostModal() {
  const sel = $('#bulk-host');
  const custom = $('#bulk-host-custom');
  const mapped = accessHost();
  sel.options[0].textContent = `当前访问地址（${currentHost()}）`;
  sel.options[1].textContent = '127.0.0.1（仅本机）';
  const saved = localStorage.getItem(HOST_KEY) || 'auto';
  if (saved === 'custom') {
    sel.value = 'custom';
    custom.value = localStorage.getItem(`${HOST_KEY}.custom`) || mapped;
  } else if (saved === '127.0.0.1') {
    sel.value = '127.0.0.1';
  } else {
    sel.value = 'auto';
  }
  custom.hidden = sel.value !== 'custom';
  $('#host-mask').hidden = false;
  $('#host-modal').hidden = false;
  refreshBulkPreview();
}

function closeHostModal() {
  $('#host-mask').hidden = true;
  $('#host-modal').hidden = true;
}

$('#btn-host').addEventListener('click', openHostModal);
$('#host-close').addEventListener('click', closeHostModal);
$('#host-cancel').addEventListener('click', closeHostModal);

$('#host-mask').addEventListener('click', closeHostModal);
$('#bulk-host').addEventListener('change', () => {
  const mode = $('#bulk-host').value;
  $('#bulk-host-custom').hidden = mode !== 'custom';
  if (mode === 'custom') $('#bulk-host-custom').focus();
  refreshBulkPreview();
});
$('#bulk-host-custom').addEventListener('input', refreshBulkPreview);
$('#host-apply').addEventListener('click', async () => {
  const host = bulkHost();
  if (!host) return;
  const btn = $('#host-apply');
  btn.disabled = true;
  try {
    const data = await api('/api/apps/rebind-host', {
      method: 'POST',
      body: JSON.stringify({ host }),
    });
    const mode = $('#bulk-host').value;
    localStorage.setItem(HOST_KEY, mode);
    if (mode === 'custom') localStorage.setItem(`${HOST_KEY}.custom`, host);
    state.baseHost = data.host || host;
    closeHostModal();
    await load();
    initHostPicker();
    const count = (data.updated || []).length;
    toast(`访问地址已设为 ${state.baseHost}，${count} 个应用地址已按该 IP 重写`);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
});

/* ---------------- 扫描 ---------------- */
async function openScan() {
  initHostPicker();
  $('#scan-mask').hidden = false;
  $('#scan-drawer').hidden = false;
  await runScan();
}

function closeScan() {
  $('#scan-mask').hidden = true;
  $('#scan-drawer').hidden = true;
}

async function runScan() {
  const list = $('#scan-list');
  list.innerHTML = '<div class="scan-empty">正在读取 Docker 容器…</div>';
  $('#scan-count').textContent = '已选 0 项';
  try {
    const includeInternal = $('#scan-internal').checked ? 'true' : 'false';
    const host = encodeURIComponent(scanHost());
    const data = await api(`/api/scan?include_internal=${includeInternal}&host=${host}`);
    state.scanItems = data.items || [];
    renderScan();
  } catch (err) {
    list.innerHTML = `<div class="scan-empty">扫描失败：${escapeHtml(err.message)}<br>请确认应用已挂载 docker.sock 且拥有访问权限。</div>`;
  }
}

function renderScan() {
  const list = $('#scan-list');
  if (!state.scanItems.length) {
    list.innerHTML = '<div class="scan-empty">没有发现暴露到宿主机的 TCP 端口。<br>容器内的数据库 / 缓存等内部服务默认隐藏。</div>';
    return;
  }
  list.innerHTML = state.scanItems.map((item, index) => {
    const disabled = item.added ? 'disabled' : '';
    const checked = item.added || !item.online ? '' : 'checked';
    const badge = item.added
      ? '<span class="badge added">已添加</span>'
      : (item.online ? '<span class="badge on">可访问</span>' : '<span class="badge off">无响应</span>');
    return `
      <label class="scan-item">
        <input type="checkbox" data-index="${index}" ${checked} ${disabled}>
        <span class="icon" style="background:${item.suggest_color || '#4f7cff'}">${escapeHtml(initial(item.suggest_name))}</span>
        <span class="info">
          <b>${escapeHtml(item.suggest_name)}</b>
          <span>${escapeHtml(hostOf(item.url))} · 端口 ${item.port} · ${escapeHtml(item.container)}</span>
        </span>
        ${badge}
      </label>`;
  }).join('');
  list.querySelectorAll('input[type=checkbox]').forEach((cb) => {
    cb.addEventListener('change', updateScanCount);
  });
  updateScanCount();
}

function selectedScan() {
  return [...document.querySelectorAll('#scan-list input[type=checkbox]:checked')]
    .map((cb) => state.scanItems[Number(cb.dataset.index)])
    .filter(Boolean);
}

function updateScanCount() {
  $('#scan-count').textContent = `已选 ${selectedScan().length} 项`;
}

async function importSelected() {
  const items = selectedScan().filter((i) => !i.added);
  if (!items.length) return toast('请先选择要导入的应用');
  try {
    const result = await api('/api/import', {
      method: 'POST',
      body: JSON.stringify({
        items: items.map((i) => ({
          name: i.suggest_name,
          url: i.url,
          group_name: i.suggest_group,
          color: i.suggest_color,
          container: i.container,
        })),
      }),
    });
    closeScan();
    await load();
    toast(`已导入 ${result.created} 个应用${result.skipped ? `，跳过 ${result.skipped} 个重复项` : ''}`);
  } catch (err) {
    toast(err.message);
  }
}

$('#btn-scan').addEventListener('click', openScan);
$('#empty-scan').addEventListener('click', openScan);
$('#scan-close').addEventListener('click', closeScan);
$('#scan-cancel').addEventListener('click', closeScan);
$('#scan-mask').addEventListener('click', closeScan);
$('#scan-reload').addEventListener('click', runScan);
$('#scan-internal').addEventListener('change', runScan);
$('#scan-allports').addEventListener('change', runScan);
$('#scan-import').addEventListener('click', importSelected);

/* ---------------- 分组管理 ---------------- */
const groupModal = $('#group-modal');
const groupMask = $('#group-mask');
const groupListEl = $('#groups-box');
let groupEditing = null;

function openGroupModal() {
  groupEditing = null;
  groupMask.hidden = false;
  groupModal.hidden = false;
  loadGroups();
}

function closeGroupModal() {
  groupMask.hidden = true;
  groupModal.hidden = true;
  groupEditing = null;
}

async function loadGroups() {
  try {
    const data = await api('/api/groups');
    state.groupList = data.groups || [];
    renderGroups();
  } catch (err) {
    toast(err.message);
  }
}

function renderGroups() {
  const rows = state.groupList || [];
  if (!rows.length) {
    groupListEl.innerHTML = '<p class="group-empty">暂无分组</p>';
    return;
  }
  groupListEl.innerHTML = rows.map((g, index) => {
    const fixed = g.name === '未分组';
    if (groupEditing === g.name) {
      return `<div class="group-row editing" data-name="${escapeHtml(g.name)}">
        <input class="group-rename-input" value="${escapeHtml(g.name)}" maxlength="30">
        <div class="group-ops">
          <button class="btn primary small" data-act="save">保存</button>
          <button class="btn ghost small" data-act="cancel">取消</button>
        </div>
      </div>`;
    }
    return `<div class="group-row" data-name="${escapeHtml(g.name)}">
      <span class="group-name">${escapeHtml(g.name)}${fixed ? '<em>默认</em>' : ''}</span>
      <span class="group-count">${g.count} 个应用</span>
      <div class="group-ops">
        <button class="icon-btn" data-act="up" title="上移"${index === 0 ? ' disabled' : ''}>
          <svg viewBox="0 0 24 24"><path d="M6 14l6-6 6 6"/></svg>
        </button>
        <button class="icon-btn" data-act="down" title="下移"${index === rows.length - 1 ? ' disabled' : ''}>
          <svg viewBox="0 0 24 24"><path d="M6 10l6 6 6-6"/></svg>
        </button>
        <button class="btn ghost small" data-act="rename"${fixed ? ' disabled' : ''}>重命名</button>
        <button class="btn ghost small danger" data-act="delete"${fixed ? ' disabled' : ''}>删除</button>
      </div>
    </div>`;
  }).join('');
  if (groupEditing) {
    const input = groupListEl.querySelector('.group-rename-input');
    if (input) { input.focus(); input.select(); }
  }
}

async function createGroup() {
  const input = $('#group-new');
  const name = input.value.trim();
  if (!name) return toast('请输入分组名称');
  try {
    const data = await api('/api/groups', { method: 'POST', body: JSON.stringify({ name }) });
    state.groupList = data.groups || [];
    input.value = '';
    renderGroups();
    await load();
    toast('已新建分组');
  } catch (err) {
    toast(err.message);
  }
}

$('#btn-groups').addEventListener('click', openGroupModal);
$('#group-close').addEventListener('click', closeGroupModal);
$('#group-done').addEventListener('click', closeGroupModal);
$('#group-mask').addEventListener('click', closeGroupModal);
$('#group-create').addEventListener('click', createGroup);
$('#group-new').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') { event.preventDefault(); createGroup(); }
});
groupListEl.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && event.target.classList.contains('group-rename-input')) {
    event.preventDefault();
    const save = groupListEl.querySelector('[data-act="save"]');
    if (save) save.click();
  }
  if (event.key === 'Escape' && event.target.classList.contains('group-rename-input')) {
    groupEditing = null;
    renderGroups();
  }
});

groupListEl.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-act]');
  const row = event.target.closest('.group-row');
  if (!button || !row) return;
  const name = row.dataset.name;
  const act = button.dataset.act;

  if (act === 'rename') {
    groupEditing = name;
    renderGroups();
    return;
  }
  if (act === 'cancel') {
    groupEditing = null;
    renderGroups();
    return;
  }
  if (act === 'save') {
    const value = row.querySelector('.group-rename-input').value.trim();
    if (!value) return toast('分组名不能为空');
    if (value === name) { groupEditing = null; return renderGroups(); }
    try {
      const data = await api('/api/groups/rename', {
        method: 'POST', body: JSON.stringify({ old: name, new: value }),
      });
      state.groupList = data.groups || [];
      groupEditing = null;
      if (state.filter === name) state.filter = value;
      renderGroups();
      await load();
      toast(`已重命名，${data.affected} 个应用已更新`);
    } catch (err) {
      toast(err.message);
    }
    return;
  }
  if (act === 'delete') {
    if (!confirm(`删除分组「${name}」？\n组内应用会移到「未分组」，应用本身不会被删除。`)) return;
    try {
      const data = await api('/api/groups/delete', {
        method: 'POST', body: JSON.stringify({ name }),
      });
      state.groupList = data.groups || [];
      if (state.filter === name) state.filter = '全部';
      renderGroups();
      await load();
      toast(`已删除，${data.affected} 个应用移到未分组`);
    } catch (err) {
      toast(err.message);
    }
    return;
  }
  if (act === 'up' || act === 'down') {
    const names = (state.groupList || []).map((g) => g.name);
    const index = names.indexOf(name);
    const target = act === 'up' ? index - 1 : index + 1;
    if (index < 0 || target < 0 || target >= names.length) return;
    [names[index], names[target]] = [names[target], names[index]];
    try {
      const data = await api('/api/groups/reorder', {
        method: 'POST', body: JSON.stringify({ names }),
      });
      state.groupList = data.groups || [];
      renderGroups();
      await load();
    } catch (err) {
      toast(err.message);
    }
  }
});

/* ---------------- 新增 / 编辑 ---------------- */
/* 分组下拉：自定义 combobox（原生 datalist 的弹层样式无法控制） */
const groupCombo = { active: -1 };

function groupNameOptions() {
  return [...new Set([
    ...(state.groups || []),
    ...state.apps.map((a) => a.group_name),
  ])].filter((g) => g && g !== '全部');
}

function renderGroupPanel(keyword = '') {
  const panel = $('#group-panel');
  const current = $('#form-group').value.trim();
  const kw = keyword.trim();
  const q = kw.toLowerCase();
  let names = groupNameOptions().filter((n) => !q || n.toLowerCase().includes(q));
  if (!q && !names.includes('未分组')) names = ['未分组', ...names];
  const exact = names.some((n) => n.toLowerCase() === q);
  let html = names.map((n) => {
    const selected = n === current || (n === '未分组' && !current);
    return `<div class="combo-opt${selected ? ' selected' : ''}" role="option" data-value="${escapeHtml(n)}">
      <b>${escapeHtml(n)}</b>${n === '未分组' ? '<i>默认</i>' : ''}</div>`;
  }).join('');
  if (kw && !exact) {
    html += `<div class="combo-opt" role="option" data-value="${escapeHtml(kw)}">
      <b>${escapeHtml(kw)}</b><i>新建</i></div>`;
  }
  panel.innerHTML = html || '<div class="combo-empty">没有匹配的分组</div>';
  panel.scrollTop = 0;
  groupCombo.active = -1;
}

function openGroupPanel(filterByValue = false) {
  const combo = $('#group-combo');
  const panel = $('#group-panel');
  const input = $('#form-group');
  renderGroupPanel(filterByValue ? input.value : '');
  panel.hidden = false;
  combo.classList.add('open');
  input.setAttribute('aria-expanded', 'true');
  const modal = $('#modal').getBoundingClientRect();
  const rect = input.getBoundingClientRect();
  const need = Math.min(panel.scrollHeight, 244) + 12;
  panel.classList.toggle('up', modal.bottom - rect.bottom < need && rect.top - modal.top > modal.bottom - rect.bottom);
}

function closeGroupPanel() {
  $('#group-panel').hidden = true;
  $('#group-combo').classList.remove('open');
  $('#form-group').setAttribute('aria-expanded', 'false');
  groupCombo.active = -1;
}

function selectGroup(value) {
  const input = $('#form-group');
  input.value = value === '未分组' ? '' : value;
  closeGroupPanel();
  input.focus();
}

function moveGroupActive(step) {
  const opts = [...$('#group-panel').querySelectorAll('.combo-opt')];
  if (!opts.length) return;
  groupCombo.active = (groupCombo.active + step + opts.length) % opts.length;
  opts.forEach((el, i) => el.classList.toggle('active', i === groupCombo.active));
  opts[groupCombo.active].scrollIntoView({ block: 'nearest' });
}

$('#form-group').addEventListener('focus', () => openGroupPanel(false));
$('#form-group').addEventListener('input', () => openGroupPanel(true));
$('#group-caret').addEventListener('click', () => {
  if ($('#group-panel').hidden) {
    $('#form-group').focus();
    openGroupPanel(false);
  } else {
    closeGroupPanel();
  }
});
$('#group-panel').addEventListener('mousedown', (event) => {
  const opt = event.target.closest('.combo-opt');
  if (!opt) return;
  event.preventDefault();
  selectGroup(opt.dataset.value);
});
$('#form-group').addEventListener('keydown', (event) => {
  const panel = $('#group-panel');
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (panel.hidden) openGroupPanel();
    moveGroupActive(event.key === 'ArrowDown' ? 1 : -1);
  } else if (event.key === 'Enter') {
    const opts = [...panel.querySelectorAll('.combo-opt')];
    if (!panel.hidden && groupCombo.active >= 0 && opts[groupCombo.active]) {
      event.preventDefault();
      selectGroup(opts[groupCombo.active].dataset.value);
    } else if (!panel.hidden) {
      closeGroupPanel();
    }
  } else if (event.key === 'Escape') {
    if (!panel.hidden) {
      event.preventDefault();
      event.stopPropagation();
      closeGroupPanel();
    }
  }
});
$('#form-group').addEventListener('blur', () => setTimeout(closeGroupPanel, 120));
document.addEventListener('mousedown', (event) => {
  if (!$('#group-combo').contains(event.target)) closeGroupPanel();
});

function openModal(app) {
  state.editingId = app ? app.id : null;
  $('#modal-title').textContent = app ? '编辑应用' : '新增应用';
  const form = $('#form');
  form.reset();
  form.elements.name.value = app ? app.name : '';
  const parsed = parseAddr(app ? app.url : '');
  const mode = app ? (app.url_mode || 'auto') : 'auto';
  $('#form-url-mode').value = mode;
  $('#form-scheme').value = parsed.scheme || 'http';
  $('#form-port').value = app ? (app.port || parsed.port || '') : '';
  $('#form-url').value = app && mode === 'custom' ? app.url : '';
  $('#form-url').placeholder = `http://${accessHost()}:端口`;
  $('#form-home-path').value = app ? (app.home_path || '') : '';
  setUrlMode(mode);
  form.elements.group_name.value = app ? app.group_name : '';
  form.elements.open_mode.value = app ? (app.open_mode || 'inline') : 'inline';
  form.elements.note.value = app ? app.note || '' : '';
  $('#form-group').value = app && app.group_name && app.group_name !== '未分组' ? app.group_name : '';
  closeGroupPanel();
  $('#modal-mask').hidden = false;
  $('#modal').hidden = false;
  setTimeout(() => form.elements.name.focus(), 40);
}

function closeModal() {
  closeGroupPanel();
  $('#modal-mask').hidden = true;
  $('#modal').hidden = true;
  state.editingId = null;
}

$('#btn-add').addEventListener('click', () => openModal(null));
$('#empty-add').addEventListener('click', () => openModal(null));
$('#modal-close').addEventListener('click', closeModal);
$('#modal-cancel').addEventListener('click', closeModal);
$('#modal-mask').addEventListener('click', closeModal);


$('#form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const urlMode = $('#form-url-mode').value;
  const port = ($('#form-port').value || '').trim();
  let url = '';
  if (urlMode === 'auto') {
    if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
      toast('请填写 1-65535 之间的端口');
      $('#form-port').focus();
      return;
    }
    url = `${$('#form-scheme').value}://${accessHost()}:${Number(port)}`;
  } else {
    url = ($('#form-url').value || '').trim();
    if (!url) {
      toast('请填写应用地址');
      $('#form-url').focus();
      return;
    }
  }
  let home_path = ($('#form-home-path').value || '').trim();
  if (home_path && home_path !== '/') {
    if (!home_path.startsWith('/')) home_path = `/${home_path}`;
    home_path = home_path.replace(/\/+$/, '');
  } else {
    home_path = '';
  }
  const payload = {
    name: form.elements.name.value.trim(),
    url,
    url_mode: urlMode,
    port: urlMode === 'auto' ? String(Number(port)) : '',
    home_path,
    group_name: form.elements.group_name.value.trim() || '未分组',
    open_mode: form.elements.open_mode.value,
    mode: 'direct',
    note: form.elements.note.value.trim(),
  };
  const submit = $('#modal-submit');
  submit.disabled = true;
  try {
    if (state.editingId) {
      await api(`/api/apps/${state.editingId}`, { method: 'PUT', body: JSON.stringify(payload) });
      toast('已保存');
    } else {
      await api('/api/apps', { method: 'POST', body: JSON.stringify(payload) });
      toast('已添加');
    }
    closeModal();
    await load();
  } catch (err) {
    toast(err.message);
  } finally {
    submit.disabled = false;
  }
});

/* ---------------- 加载与探测 ---------------- */
async function reportHost() {
  const host = currentHost();
  // 只上报 IPv4：域名访问、本机回环地址都不写入，避免把应用地址带回域名
  if (!isIpv4(host) || LOOPBACK_HOSTS.has(host.toLowerCase())) return;
  try {
    // 主面板已统一配置过 IP 时尊重面板选择，不被当前访问地址覆盖
    const setting = await api('/api/settings/base-host');
    const recorded = String(setting.host || '').trim();
    if (isIpv4(recorded) && !LOOPBACK_HOSTS.has(recorded.toLowerCase())) {
      state.baseHost = recorded;
      return;
    }
    await api('/api/settings/base-host', {
      method: 'POST',
      body: JSON.stringify({ host }),
    });
    state.baseHost = host;
  } catch (err) {
    /* 记录失败不影响主流程 */
  }
}

async function loadVersion() {
  try {
    const info = await api('/api/version');
    const version = String((info && info.version) || '').trim();
    if (!version || version === 'unknown') return;
    const tag = `v${version}`;
    document.title = `AppHub ${tag} · 应用聚合中心`;
    const badge = $('#brand-version');
    if (badge) {
      badge.textContent = tag;
      badge.title = `当前版本 ${tag}`;
      badge.hidden = false;
    }
    state.version = version;
  } catch (e) { /* 版本读取失败不影响使用 */ }
}

async function load() {
  try {
    const data = await api('/api/apps');
    state.apps = data.apps || [];
    state.groups = data.groups || [];
    state.selfPort = data.self_port || state.selfPort;
    if (!state.baseHost) {
      try {
        const setting = await api('/api/settings/base-host');
        state.baseHost = setting.host || '';
      } catch (e) { /* 读取失败时退回当前访问主机 */ }
    }
    // 统一配置的主机已确定时，把仍指向回环地址的统一配置类应用拉齐一次
    if (!state.hostSynced) {
      state.hostSynced = true;
      const base = String(state.baseHost || '').toLowerCase();
      if (base && !LOOPBACK_HOSTS.has(base)) {
        const stale = state.apps
          .filter((a) => (a.url_mode || 'auto') === 'auto' && LOOPBACK_HOSTS.has(urlHostname(a.url).toLowerCase()))
          .map((a) => a.id);
        if (stale.length) {
          try {
            const res = await api('/api/apps/rebind-host', {
              method: 'POST',
              body: JSON.stringify({ host: state.baseHost, app_ids: stale, only_loopback: true }),
            });
            if (res.updated && res.updated.length) {
              const again = await api('/api/apps');
              state.apps = again.apps || state.apps;
              state.groups = again.groups || state.groups;
            }
          } catch (e) { /* 拉齐失败不阻塞加载 */ }
        }
      }
    }
    if (state.filter !== '全部' && !state.groups.includes(state.filter)) state.filter = '全部';
    renderChips();
    renderGrid();
    probeAll();
  } catch (err) {
    toast(`加载失败：${err.message}`);
  }
}

async function probeAll() {
  const ids = state.apps.map((a) => a.id);
  if (!ids.length) return;
  const chunks = [];
  for (let i = 0; i < ids.length; i += 8) chunks.push(ids.slice(i, i + 8));
  for (const chunk of chunks) {
    try {
      const data = await api('/api/probe', { method: 'POST', body: JSON.stringify({ ids: chunk }) });
      Object.assign(state.statuses, data.status || {});
    } catch (e) { /* ignore */ }
  }
  renderGrid();
}

/* ---------------- 全局事件 ---------------- */
$('#search').addEventListener('input', (event) => {
  state.query = event.target.value;
  renderGrid();
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (ws.picker) return dismissPicker();
  if (!$('#modal').hidden) return closeModal();
  if (!$('#scan-drawer').hidden) return closeScan();
  if (!$('#host-modal').hidden) return;
  if (!$('#group-modal').hidden) return;
  /* 工作区里 Esc 只关单窗口场景，避免多窗口时误伤全部标签 */
  if (wsIsOpen()) {
    if (document.fullscreenElement) return;
    if (ws.wins.length <= 1) return closeWorkspace();
    toast(`还有 ${ws.wins.length} 个窗口：用左上角返回或右上角「关闭全部」`);
  }
});

/* 快捷键：Alt+T 打开新窗口选择器，Alt+1..9 切换标签
   （不用 Ctrl/Cmd+T、Ctrl/Cmd+1..9：这些是浏览器自己的新建标签 / 切标签快捷键，
     页面拿不到，会被浏览器先拦截） */
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') return;
  const mod = event.altKey && !event.metaKey && !event.ctrlKey;
  if (!mod) return;
  const inModal = !$('#modal').hidden || !$('#scan-drawer').hidden || !$('#host-modal').hidden || !$('#group-modal').hidden;
  if (!inModal && event.key.toLowerCase() === 't') {
    event.preventDefault();
    if (!wsIsOpen()) {
      workspace.hidden = false;
      document.body.style.overflow = 'hidden';
      setLayout(readLayout());
    }
    togglePicker();
    return;
  }
  if (wsIsOpen() && /^[1-9]$/.test(event.key)) {
    const win = ws.wins[Number(event.key) - 1];
    if (!win) return;
    event.preventDefault();
    if (ws.layout !== 'solo') setLayout('solo');
    activate(win.id, true);
  }
});

$('#btn-add').addEventListener('keydown', (e) => e.stopPropagation());

/* 工作区布局模式在启动时恢复 */
document.querySelectorAll('#ws-layout button').forEach((b) => {
  b.classList.toggle('active', b.dataset.layout === readLayout());
});
ws.layout = readLayout();

initHostPicker();
loadVersion();
reportHost();
load();
setInterval(probeAll, 60000);
