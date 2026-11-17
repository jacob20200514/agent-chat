'use strict';
const $ = (id) => document.getElementById(id);
const state = { view: 'login', sessions: [], options: {}, agents: {}, current: null, text: null, timer: null, failures: 0, stick: true };

async function api(path, body) {
  const options = body === undefined ? { headers: { 'X-Agent-Chat': '1' } }
    : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Agent-Chat': '1' }, body: JSON.stringify(body) };
  const response = await fetch(path, { ...options, credentials: 'same-origin', cache: 'no-store' });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== '/api/login') { show('login'); throw new Error('请先登录'); }
  if (!response.ok) throw new Error(data.error || '请求失败（' + response.status + '）');
  return data;
}

// Desktop (>= 900px): the session list stays on the left as a sidebar while a chat is open.
const wideQuery = matchMedia('(min-width: 900px)');
function layout() {
  document.body.classList.toggle('wide', wideQuery.matches);
  $('home').classList.toggle('side', wideQuery.matches && state.view === 'chat');
  $('empty').classList.toggle('on', wideQuery.matches && state.view === 'home');
}
wideQuery.addEventListener('change', () => { layout(); if (state.view !== 'login') pollHome(); });
function show(view) {
  state.view = view;
  if (view === 'home') state.current = null;
  for (const name of ['login', 'home', 'chat']) $(name).classList.toggle('on', name === view);
  clearTimeout(state.timer);
  layout();
  if (view === 'home' || (view === 'chat' && wideQuery.matches)) pollHome();
  if (view === 'chat') pollChat();
}

function esc(text) { return String(text).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function stateLabel(s) { return { busy: '运行中', idle: '空闲', exited: 'Agent 未运行' }[s] || '未知'; }

// ---- home ----
async function pollHome() {
  clearTimeout(state.homeTimer);
  if (state.view !== 'home' && !(state.view === 'chat' && wideQuery.matches)) return;
  try {
    const data = await api('/api/sessions');
    Object.assign(state, { sessions: data.sessions, options: data.options, agents: data.agents });
    // Each card is rewritten only when it changed: replacing a card while a finger is on it swallows the tap.
    const cards = data.sessions.map((s) => [s.name, `<div class="card${state.current && state.current.name === s.name ? ' current' : ''}" data-name="${esc(s.name)}"><span class="dot ${s.state}"></span><div class="main"><b>${esc(s.nickname)}</b><span class="muted">${esc(s.name)} · ${stateLabel(s.state)}${s.attached ? ' · 终端已连接' : ''}</span>${s.preview ? `<span class="preview">${esc(s.preview)}</span>` : ''}</div><span class="tag">${esc(s.agent_label)}</span></div>`]);
    const list = $('list'), shown = [...list.querySelectorAll('.card')];
    if (tapActive()) { /* a finger is on a card: leave the list alone until the next refresh */ }
    else if (!cards.length || shown.length !== cards.length || shown.some((el, n) => el.dataset.name !== cards[n][0])) {
      list.innerHTML = cards.map((c) => c[1]).join('') || '<p class="muted">还没有 tmux 会话，点右上角「新建」。</p>';
      for (const [n, el] of [...list.querySelectorAll('.card')].entries()) el.dataset.html = cards[n][1];
    } else shown.forEach((el, n) => {
      if (el.dataset.html === cards[n][1]) return;
      el.insertAdjacentHTML('afterend', cards[n][1]); el.nextElementSibling.dataset.html = cards[n][1]; el.remove();
    });
    $('home-status').textContent = '';
  } catch (error) { $('home-status').textContent = '连接中断，正在重试… ' + error.message; }
  if (!document.hidden) state.homeTimer = setTimeout(pollHome, 3000);
}
function openCard(card) {
  state.current = state.sessions.find((s) => s.name === card.dataset.name); state.text = null; state.stick = true;
  $('log').innerHTML = ''; renderHeader(); show('chat');
  for (const el of $('list').querySelectorAll('.card')) el.classList.toggle('current', el === card);
}
$('list').addEventListener('click', (event) => { const card = event.target.closest('.card'); if (card) openCard(card); });
// Phone: a card opens when the finger lifts. iOS does not always turn a tap on a card into a click, so the
// first tap could do nothing; the click that would follow is cancelled so it cannot land on the chat page.
let tap = null;
const tapActive = () => !!tap && Date.now() - tap.at < 2000;
$('list').addEventListener('touchstart', (event) => {
  const touch = event.touches[0], card = event.target.closest('.card');
  tap = event.touches.length === 1 && card ? { card, x: touch.clientX, y: touch.clientY, at: Date.now() } : null;
}, { passive: true });
$('list').addEventListener('touchcancel', () => { tap = null; });
$('list').addEventListener('touchend', (event) => {
  const started = tap; tap = null; if (!started) return;
  const touch = event.changedTouches[0], moved = Math.hypot(touch.clientX - started.x, touch.clientY - started.y), held = Date.now() - started.at;
  if (moved > 10 || held > 600 || !started.card.isConnected) return;
  event.preventDefault(); openCard(started.card);
});

// ---- chat ----
function renderHeader() {
  const s = state.current; if (!s) return;
  $('chat-name').textContent = s.nickname;
  $('chat-sub').textContent = [s.agent_label, s.choice ? '等你选择' : stateLabel(s.state), s.model, s.effort].filter(Boolean).join(' · ');
  $('chat-dot').className = 'dot ' + s.state;
  const usable = !!s.agent && s.state !== 'exited';
  for (const id of ['model', 'effort', 'new']) $(id).disabled = !usable;
  // Text goes to any live pane: in a Shell session it is typed at the prompt and run.
  $('send').disabled = s.dead;
  $('input').placeholder = s.choice ? '在这里回复选项（如数字）后发送…' : usable ? '发消息…' : s.dead ? '这个会话里的程序已退出' : '输入命令，发送后在 Shell 里执行';
}
// The pane is one stream of text. Lines the CLI marks as the user's prompt become right-hand bubbles and
// split it into blocks, so each stretch of agent output can be copied on its own.
function blocks(text) {
  const out = []; let current = null;
  const push = (kind, line) => { if (!current || current.kind !== kind) { current = { kind, lines: [] }; out.push(current); } current.lines.push(line); };
  let inUser = false;
  for (const line of text.split('\n')) {
    // A menu's cursor row ("❯ 1. Yes") carries the prompt mark too; it belongs to the agent's question.
    const user = (/^\s*[❯›]\s+\S/.test(line) || /^>\s+\S/.test(line)) && !/^\s*[❯›>]\s+\d+\.\s/.test(line);
    // A long or multi-line message continues on lines indented two spaces, up to the first empty line;
    // CodeBuddy's replies are indented too but start with a tree glyph (└ ⎿).
    if (inUser && !user && /^ {2}[^\s└⎿├│⏺●•]/.test(line)) { current.lines.push(line.slice(2)); continue; }
    // A blank line inside the message arrives as two spaces (server.py PADDED_BLANK); a truly empty line ends it.
    if (inUser && line === '  ') { current.lines.push(''); continue; }
    inUser = user;
    if (user) { current = null; push('user', line.replace(/^\s*[❯›>]\s+/, '')); } else push('agent', line === '  ' ? '' : line);
  }
  return out.map((b) => ({ kind: b.kind, text: b.lines.join('\n').replace(/^\n+|\n+$/g, '') })).filter((b) => b.text.trim());
}
function renderLog(text) {
  const log = $('log'), list = blocks(text);
  // Only the blocks that changed are rewritten, so a selection or scroll position higher up survives the refresh.
  while (log.children.length > list.length) log.lastChild.remove();
  list.forEach((block, i) => {
    let node = log.children[i];
    if (!node) { node = document.createElement('div'); node.innerHTML = '<pre></pre>'; log.append(node); }
    if (node.dataset.text === block.text && node.dataset.kind === block.kind) return;
    node.className = 'block ' + block.kind; node.dataset.kind = block.kind; node.dataset.text = block.text;
    node.firstChild.textContent = block.text;
    const old = node.querySelector('.copy'); if (old) old.remove();
    if (block.kind === 'agent') { const copy = document.createElement('button'); copy.className = 'copy'; copy.type = 'button'; copy.textContent = '复制'; node.append(copy); }
  });
  if (state.stick) log.scrollTop = log.scrollHeight;
}
$('log').addEventListener('scroll', () => { const log = $('log'); state.stick = log.scrollHeight - log.scrollTop - log.clientHeight < 80; });
$('log').addEventListener('click', async (event) => {
  const button = event.target.closest('.copy'); if (!button) return;
  const text = button.parentNode.dataset.text;
  try { await navigator.clipboard.writeText(text); }
  catch (_) { const area = document.createElement('textarea'); area.value = text; document.body.append(area); area.select(); document.execCommand('copy'); area.remove(); }
  button.textContent = '已复制'; setTimeout(() => { button.textContent = '复制'; }, 1200);
});
function setStatus(text, error = false) { $('status').textContent = text || ''; $('status').classList.toggle('error', error); }
async function pollChat() {
  if (state.view !== 'chat' || !state.current) return;
  let delay = 1000;
  try {
    const since = state.text === null ? '' : '?since=' + state.version;
    const data = await api('/api/sessions/' + encodeURIComponent(state.current.name) + '/output' + since);
    state.current = data.session; renderHeader();
    if (data.text !== undefined && data.text !== state.text) { state.text = data.text; renderLog(data.text); }
    state.version = data.version;
    if (state.failures) setStatus('已重新连接');
    state.failures = 0;
  } catch (error) {
    state.failures += 1; delay = Math.min(8000, 1000 * state.failures);
    if (/不存在/.test(error.message)) { setStatus(error.message, true); state.timer = setTimeout(() => show('home'), 1500); return; }
    setStatus('连接中断，正在重连…（Agent 仍在 Mac 上运行）', true);
  }
  if (document.hidden) return; // in the background nothing is fetched; coming back refreshes at once
  state.timer = setTimeout(pollChat, delay);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { clearTimeout(state.timer); clearTimeout(state.homeTimer); }
  else if (state.view !== 'login') show(state.view);
});

async function act(action, body, working) {
  setStatus(working || '处理中…');
  try { await api('/api/sessions/' + encodeURIComponent(state.current.name) + '/' + action, body || {}); setStatus(''); return true; }
  catch (error) { setStatus(error.message, true); return false; }
}
const input = $('input');
input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, innerHeight * 0.4) + 'px'; });
// With a hardware keyboard Enter sends and Ctrl+Enter (or Cmd/Shift+Enter) breaks the line. On a phone the
// on-screen Enter stays a newline -- there is no Ctrl there -- and the send button sends.
const hardwareKeys = matchMedia('(hover: hover) and (pointer: fine)');
input.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229 || !hardwareKeys.matches) return;
  if (event.shiftKey) return;
  event.preventDefault();
  if (event.ctrlKey || event.metaKey) { input.setRangeText('\n', input.selectionStart, input.selectionEnd, 'end'); input.dispatchEvent(new Event('input')); }
  else $('compose').requestSubmit();
});
$('compose').addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = input.value; if (!text.trim() || $('send').disabled) return;
  // One id per message: a retry after a dropped connection cannot type it into the agent twice.
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2);
  $('send').disabled = true; state.stick = true;
  let sent = false;
  for (let attempt = 0; attempt < 3 && !sent; attempt++) sent = await act('send', { text, id }, '发送中…');
  if (sent) { input.value = ''; input.style.height = 'auto'; }
  $('send').disabled = false;
});
// Phone: a quick swipe to the right over the chat goes back to the session list. Rows that scroll sideways
// and the input box keep their own horizontal gestures; a swipe that selected text is left alone.
let swipe = null;
$('chat').addEventListener('touchstart', (event) => {
  const touch = event.touches[0];
  swipe = event.touches.length === 1 && !event.target.closest('.bar, #keys, textarea') ? { x: touch.clientX, y: touch.clientY, at: Date.now() } : null;
}, { passive: true });
$('chat').addEventListener('touchend', (event) => {
  if (!swipe || wideQuery.matches) return;
  const touch = event.changedTouches[0], dx = touch.clientX - swipe.x, dy = Math.abs(touch.clientY - swipe.y), quick = Date.now() - swipe.at < 600;
  swipe = null;
  if (dx > 80 && dy < dx / 2 && quick && getSelection().isCollapsed) $('back').click();
}, { passive: true });
$('keys-toggle').addEventListener('click', () => $('keys').classList.toggle('on'));
$('keys').addEventListener('click', (event) => { const key = event.target.dataset.key; if (key) act('key', { key }, ' '); });
$('back').addEventListener('click', () => show('home'));

// ---- sheets ----
function sheet(title, note, bodyHtml, actions) {
  $('sheet-title').textContent = title; $('sheet-note').textContent = note || ''; $('sheet-body').innerHTML = bodyHtml;
  $('sheet-actions').innerHTML = '';
  for (const [label, handler, cls] of [['取消', () => $('sheet').close(), ''], ...(actions || [])]) {
    const button = document.createElement('button'); button.textContent = label; button.className = cls || ''; button.type = 'button';
    button.addEventListener('click', handler); $('sheet-actions').append(button);
  }
  $('sheet').showModal();
}
function choose(kind) {
  const s = state.current, options = state.options[s.agent]; if (!options) return;
  const list = kind === 'model' ? options.models : options.efforts;
  sheet(kind === 'model' ? '选择模型' : '选择推理深度', kind === 'model' ? '仅本会话生效，不改其他会话的默认模型。切换约需几秒。' : options.effort_note,
    '<div class="options">' + list.map((o) => `<button type="button" data-id="${esc(o.id)}" class="${s[kind] === o.id ? 'current' : ''}">${esc(o.label)}</button>`).join('') + '</div>');
  $('sheet-body').onclick = async (event) => {
    const id = event.target.dataset.id; if (!id) return;
    $('sheet').close(); state.stick = true;
    await act(kind, { [kind]: id }, '正在切换到 ' + id + '…');
  };
}
$('model').addEventListener('click', () => choose('model'));
$('effort').addEventListener('click', () => choose('effort'));
$('new').addEventListener('click', () => sheet('开启新对话？', '当前 Agent 清空上下文开始新对话，tmux 会话保留。', '', [['开启', async () => { $('sheet').close(); await act('new', {}, '正在开启新对话…'); }, 'primary']]));
$('kill').addEventListener('click', () => {
  const s = state.current;
  sheet('关闭 tmux 会话？', '将终止「' + s.nickname + '」（' + s.name + '）和其中运行的 Agent，无法恢复。', '', [['确认关闭', async () => {
    $('sheet').close(); if (await act('kill', { confirm: s.name }, '正在关闭…')) show('home');
  }, 'danger']]);
});
$('rename').addEventListener('click', () => {
  const s = state.current;
  sheet('修改昵称', '只改网页里显示的名字，不改 tmux 会话名（' + s.name + '）。', `<input id="nick" maxlength="40" value="${esc(s.nickname)}">`, [['保存', async () => {
    const nickname = $('nick').value; $('sheet').close(); if (await act('rename', { nickname })) { s.nickname = nickname.trim() || s.name; renderHeader(); }
  }, 'primary']]);
});
$('create-open').addEventListener('click', () => {
  const agents = Object.entries(state.agents).map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join('')
    + '<option value="shell">Zsh</option>';
  sheet('新建 Agent 会话', '在主目录 ~ 下启动一个新的 tmux 长对话。', `<p><select id="new-agent">${agents}</select></p><input id="new-nick" maxlength="40" placeholder="昵称">`, [['创建', async () => {
    const body = { agent: $('new-agent').value, nickname: $('new-nick').value }; $('sheet').close();
    try { await api('/api/sessions', body); pollHomeNow(); } catch (error) { $('home-status').textContent = error.message; }
  }, 'primary']]);
});
function pollHomeNow() { clearTimeout(state.timer); pollHome(); }

// ---- login, or on a fresh install the first password ----
let firstRun = false;
function loginMode(setup) {
  firstRun = setup;
  $('login-note').textContent = setup ? '首次使用：请设置登录密码（至少 8 位）。' : '';
  $('password').autocomplete = setup ? 'new-password' : 'current-password';
  $('password').placeholder = setup ? '新密码' : '密码';
  $('password2').hidden = !setup; $('password2').required = setup;
  $('login-submit').textContent = setup ? '设置密码并进入' : '登录';
}
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault(); $('login-error').textContent = '';
  const password = $('password').value;
  if (firstRun && password.length < 8) { $('login-error').textContent = '密码至少 8 位'; return; }
  if (firstRun && password !== $('password2').value) { $('login-error').textContent = '两次输入不一样'; return; }
  try {
    await api(firstRun ? '/api/setup' : '/api/login', { password, remember: $('remember').checked });
    $('password').value = ''; $('password2').value = ''; loginMode(false); show('home');
  } catch (error) {
    $('login-error').textContent = error.message;
    if (firstRun && /已经设置/.test(error.message)) loginMode(false);
  }
});
$('logout').addEventListener('click', async () => { try { await api('/api/logout', {}); } catch (_) {} show('login'); });
(async () => {
  try {
    const me = await api('/api/me');
    loginMode(!me.password_set);
    show(me.login ? 'home' : 'login');
  } catch (_) { show('login'); }
})();

// ---- chat text size (per device) ----
function setLogSize(px) {
  px = Math.min(20, Math.max(10, px));
  document.documentElement.style.setProperty('--log-size', px + 'px');
  try { localStorage.setItem('logSize', px); } catch (_) {}
  return px;
}
let logSize = 12.5;
try { logSize = Number(localStorage.getItem('logSize')) || logSize; } catch (_) {}
logSize = setLogSize(logSize);
$('font-down').addEventListener('click', () => { logSize = setLogSize(logSize - 1); });
$('font-up').addEventListener('click', () => { logSize = setLogSize(logSize + 1); });

// ---- light / dark (per device; follows the system until chosen). Icons are autotrade's. ----
function themeIconSvg(iconName) {
  const iconPath = iconName === 'sun'
    ? '<circle cx="12" cy="12" r="3.5" stroke-width="1.8"></circle><path d="M12 2.5V5M12 19V21.5M2.5 12H5M19 12H21.5M5.3 5.3L7.1 7.1M16.9 16.9L18.7 18.7M18.7 5.3L16.9 7.1M7.1 16.9L5.3 18.7" stroke-width="1.8" stroke-linecap="round"></path>'
    : '<path d="M20.2 15.2A8.5 8.5 0 0 1 8.8 3.8 8.5 8.5 0 1 0 20.2 15.2Z" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path>';
  return `<span class="ui-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" focusable="false">${iconPath}</svg></span>`;
}
function currentTheme() {
  return document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
}
function syncTheme() {
  const current = currentTheme(), label = current === 'dark' ? '切换到亮色主题' : '切换到暗色主题';
  const btn = $('theme-toggle'); btn.innerHTML = themeIconSvg(current === 'dark' ? 'sun' : 'moon'); btn.title = label; btn.setAttribute('aria-label', label);
  document.querySelector('meta[name="theme-color"]').content = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
}
try { const saved = localStorage.getItem('theme'); if (saved === 'light' || saved === 'dark') document.documentElement.setAttribute('data-theme', saved); } catch (_) {}
syncTheme();
matchMedia('(prefers-color-scheme: light)').addEventListener('change', syncTheme);
$('theme-toggle').addEventListener('click', () => {
  const next = currentTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('theme', next); } catch (_) {}
  syncTheme();
});
