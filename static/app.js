'use strict';
const $ = (id) => document.getElementById(id);
const state = { view: 'login', sessions: [], options: {}, agents: {}, current: null, text: null, version: null,
  timer: null, homeTimer: null, pollEpoch: 0, homeRequest: 0, visit: 0, failures: 0, stick: true,
  usageSupported: false, transcriptionSupported: false };

// A Blob body (a picture or recording) is sent as it is; anything else as JSON.
async function api(path, body, isCurrent = () => true, signal) {
  const raw = body instanceof Blob;
  const options = body === undefined ? { headers: { 'X-Agent-Chat': '1' } }
    : { method: 'POST', headers: { 'Content-Type': raw ? body.type || 'application/octet-stream' : 'application/json', 'X-Agent-Chat': '1' },
        body: raw ? body : JSON.stringify(body) };
  const response = await fetch(path, { ...options, credentials: 'same-origin', cache: 'no-store', signal });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== '/api/login') { if (isCurrent()) show('login'); throw new Error('请先登录'); }
  if (!response.ok) {
    const error = new Error(data.error || '请求失败（' + response.status + '）');
    error.code = data.code;
    throw error;
  }
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
function stopPolling() {
  // 清掉定时器，并让已发出的请求失效。同名会话重进或从后台恢复也会开启新一轮。
  state.pollEpoch++;
  clearTimeout(state.timer); clearTimeout(state.homeTimer);
}
function show(view) {
  stopPolling();
  if (view !== state.view && $('sheet').open) $('sheet').close();
  if (view !== 'chat') {
    leaveComposer();
    if (view === 'login') drafts.clear();
    state.current = null; state.visit++;
  }
  state.view = view;
  for (const name of ['login', 'home', 'chat']) $(name).classList.toggle('on', name === view);
  layout();
  if (listVisible()) pollHome();
  if (view === 'chat') pollChat();
}

function esc(text) { return String(text).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function stateLabel(s) { return { busy: '运行中', idle: '空闲', exited: 'Agent 未运行' }[s] || '未知'; }
// When the message on a card was sent, to the minute: the clock alone today, with the date before that.
function sentAt(ms) {
  const d = new Date(ms), now = new Date(), pad = (n) => String(n).padStart(2, '0');
  const time = pad(d.getHours()) + ':' + pad(d.getMinutes());
  if (d.toDateString() === now.toDateString()) return time;
  return (d.getFullYear() === now.getFullYear() ? '' : d.getFullYear() + '年') + (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + time;
}

// ---- home ----
function listVisible() { return state.view === 'home' || (state.view === 'chat' && wideQuery.matches); }
async function pollHome() {
  clearTimeout(state.homeTimer);
  if (!listVisible() || document.hidden) return;
  const epoch = state.pollEpoch, request = ++state.homeRequest;
  const isCurrent = () => epoch === state.pollEpoch && request === state.homeRequest && listVisible() && !document.hidden;
  try {
    const data = await api('/api/sessions', undefined, isCurrent);
    if (!isCurrent()) return;
    Object.assign(state, { sessions: data.sessions, options: data.options, agents: data.agents,
      usageSupported: data.usage_supported === true, transcriptionSupported: data.transcription_supported === true });
    if (state.current) renderVoice();
    for (const session of data.sessions) {
      if (session.name !== state.current?.name) recordReply(session, false);
    }
    renderCards();
    $('home-status').textContent = '';
  } catch (error) {
    if (!isCurrent()) return;
    $('home-status').textContent = '连接中断，正在重试… ' + error.message;
  }
  state.homeTimer = setTimeout(pollHome, 3000);
}
function renderCards() {
  // 手指按在卡片上时保留原节点，否则会吞掉触摸。
  if (tapActive()) return;
  const cards = state.sessions.map((s) => {
    const unread = hasUnread(s) ? '<span class="dot unread" role="img" aria-label="有未读消息" title="有未读消息"></span>' : '';
    return [s.name, `<div class="card${state.current?.name === s.name ? ' current' : ''}" data-name="${esc(s.name)}"><span class="dot ${s.state}"></span><div class="main"><b>${esc(s.nickname)}</b><span class="muted">${esc(s.name)} · ${stateLabel(s.state)}${s.attached ? ' · 终端已连接' : ''}</span>${s.preview ? `<span class="preview">${s.preview_at ? `<time>${sentAt(s.preview_at)}</time>` : ''}${esc(s.preview)}</span>` : ''}</div>${unread}<span class="tag">${esc(s.agent_label)}</span></div>`];
  });
  const list = $('list'), shown = [...list.querySelectorAll('.card')];
  if (!cards.length || shown.length !== cards.length || shown.some((el, n) => el.dataset.name !== cards[n][0])) {
    list.innerHTML = cards.map((c) => c[1]).join('') || '<p class="muted">还没有 tmux 会话，点右上角「新建」。</p>';
    for (const [n, el] of [...list.querySelectorAll('.card')].entries()) el.dataset.html = cards[n][1];
  } else shown.forEach((el, n) => {
    if (el.dataset.html === cards[n][1]) return;
    el.insertAdjacentHTML('afterend', cards[n][1]); el.nextElementSibling.dataset.html = cards[n][1]; el.remove();
  });
}

const observedReplies = new Map(), observingSince = Date.now();
let readReplies = {};
try { readReplies = JSON.parse(localStorage.getItem('agentChatReadReplies')) || {}; } catch (_) {}
function sessionKey(s) { return s.name + ':' + s.created; }
function hasUnread(s) {
  const key = sessionKey(s), reply = observedReplies.get(key), seen = readReplies[key];
  return !!(s.agent && reply?.id && (reply.scope !== seen?.scope || reply.id !== seen?.id));
}
function recordReply(s, read) {
  // Metadata is independent of the rendered text and arrives even with an unchanged output version.
  // Pi can journal the final message just before its editor stops showing the spinner.
  const reply = s.reply;
  if (!s.agent || !reply || (s.agent === 'pi' && s.state !== 'idle')) return false;
  const key = sessionKey(s), wasUnread = hasUnread(s), seen = readReplies[key];
  observedReplies.set(key, reply);
  const initialHistory = reply.scope !== seen?.scope && (!reply.at || reply.at < observingSince);
  const scopeChanged = reply.scope !== seen?.scope;
  // If the first reply completed during this visit, persist an unread baseline for reloads.
  const id = read || initialHistory || !reply.id ? reply.id : scopeChanged ? null : seen.id;
  if (scopeChanged || id !== seen?.id) {
    readReplies[key] = { scope: reply.scope, id };
    try { localStorage.setItem('agentChatReadReplies', JSON.stringify(readReplies)); } catch (_) {}
  }
  return wasUnread !== hasUnread(s);
}
function openCard(card) {
  const session = state.sessions.find((s) => s.name === card.dataset.name);
  if (!session) return;
  leaveComposer();
  state.current = session; state.visit++;
  input.value = drafts.get(sessionKey(session)) || '';
  input.dispatchEvent(new Event('input'));
  state.text = null; state.version = null; state.stick = true; state.failures = 0;
  clearAttached();
  $('log').innerHTML = ''; setStatus(''); renderHeader(); show('chat');
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
  const model = state.options[s.agent]?.models.find((o) => o.id === s.model)?.label || s.model;
  $('chat-sub').textContent = [model?.replace(/\s+/g, '').toLowerCase(), s.effort].filter(Boolean).join(' ');
  $('chat-dot').className = 'dot ' + s.state;
  const usable = !!s.agent && s.state !== 'exited';
  for (const id of ['model', 'effort', 'new']) $(id).disabled = !usable;
  $('usage').hidden = !state.usageSupported || !usable || !['codex', 'claude', 'pi'].includes(s.agent);
  // Text goes to any live pane: in a Shell session it is typed at the prompt and run.
  renderVoice();
  $('attach').hidden = !usable;
  $('input').placeholder = s.choice ? '在这里回复选项（如数字）后发送…' : usable ? '发消息…' : s.dead ? '这个会话里的程序已退出' : '输入命令，发送后在 Shell 里执行';
}
// The pane is one stream of text. Lines the CLI marks as the user's prompt become right-hand bubbles and
// split it into blocks, so each stretch of agent output can be copied on its own. The mark is at the margin:
// tool output the agents show indented may hold a "›" of its own. server.py turns Pi's panels into this form.
// A menu's cursor row ("❯ 1. Yes") carries the prompt mark too and belongs to the agent's question: its key hint
// ("Enter to select", "Esc to cancel", "esc back") follows. A message of the user's that starts with "1." is
// followed by the reply or the next prompt instead. Same rule as menu_row() in server.py.
const MENU_HINT = /\b(?:enter|esc)\b.*\b(?:select|confirm|cancel|back|exit)\b/i;
function menuRow(lines, i) {
  if (!/^\s*[❯›>]\s+\d+\.\s/.test(lines[i])) return false;
  for (const line of lines.slice(i + 1, i + 40)) {
    if (MENU_HINT.test(line)) return true;
    if (/^\s*[❯›>⏺●•✻✶✳✢✽·∴✔⎿└](\s|$)/.test(line)) return false;
  }
  return false;
}
// A long or multi-line message continues on lines indented two spaces, plus any indent of the user's own.
// Replies that are indented too start with a tree glyph (└ ⎿) or a dot (⏺ ●); Codex shows the user's "- " as "•".
// Claude's folded tool summary ("Read 2 files (ctrl+o to expand)") sits under the message at the same indent.
// Same rule as USER_MORE in server.py.
const moreOfMessage = (line) => /^ {2}(?!.*\(ctrl\+o to expand\)\s*$)\s*[^\s└⎿├│⏺●]/.test(line || '');
function blocks(text) {
  const out = []; let current = null;
  const push = (kind, line) => { if (!current || current.kind !== kind) { current = { kind, lines: [] }; out.push(current); } current.lines.push(line); };
  const lines = text.split('\n');
  let inUser = false;
  lines.forEach((line, i) => {
    const user = /^[❯›>]\s+\S/.test(line) && !menuRow(lines, i);
    if (inUser && !user && moreOfMessage(line)) { current.lines.push(line.slice(2)); return; }
    // A blank line inside the message arrives as two spaces (server.py PADDED_BLANK; keep_message_gaps() turns
    // Codex's and CodeBuddy's empty ones into it); a truly empty line ends it.
    if (inUser && line === '  ') { current.lines.push(''); return; }
    inUser = user;
    if (user) { current = null; push('user', line.replace(/^\s*[❯›>]\s+/, '')); } else push('agent', line === '  ' ? '' : line);
  });
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
function outputPath(name, version) {
  return '/api/sessions/' + encodeURIComponent(name) + '/output' + (version ? '?since=' + version : '');
}
async function pollChat() {
  if (state.view !== 'chat' || !state.current || document.hidden) return;
  const name = state.current.name, epoch = state.pollEpoch;
  const isCurrent = () => epoch === state.pollEpoch && state.view === 'chat' && state.current?.name === name && !document.hidden;
  let delay = 1000;
  try {
    const data = await api(outputPath(name, state.version), undefined, isCurrent);
    if (!isCurrent()) return;
    state.current = data.session; renderHeader();
    const textChanged = data.text !== undefined && data.text !== state.text;
    if (textChanged) { state.text = data.text; renderLog(data.text); }
    if (recordReply(data.session, true) && listVisible()) renderCards();
    state.version = data.version;
    if (state.failures) setStatus('已重新连接');
    state.failures = 0;
  } catch (error) {
    if (!isCurrent()) return;
    state.failures += 1; delay = Math.min(8000, 1000 * state.failures);
    if (/不存在/.test(error.message)) {
      setStatus(error.message, true);
      state.timer = setTimeout(() => { if (isCurrent()) show('home'); }, 1500);
      return;
    }
    setStatus('连接中断，正在重连…（Agent 仍在 Mac 上运行）', true);
  }
  state.timer = setTimeout(pollChat, delay);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { cancelVoice(); stopPolling(); }
  else if (state.view !== 'login') show(state.view);
});
window.addEventListener('pagehide', () => cancelVoice());

async function act(action, body, working, visit = state.visit) {
  const isCurrent = () => state.view === 'chat' && state.visit === visit;
  const attempts = action === 'send' ? 3 : 1;
  for (let attempt = 0; attempt < attempts && isCurrent(); attempt++) {
    setStatus(working || '处理中…');
    try {
      await api('/api/sessions/' + encodeURIComponent(state.current.name) + '/' + action, body || {}, isCurrent);
      if (!isCurrent()) return false;
      setStatus(''); return true;
    } catch (error) {
      if (!isCurrent()) return false;
      setStatus(error.message, true);
      if (['CODEX_USAGE_OPEN', 'CODEX_WARNINGS_OPEN', 'PI_PICKER_OPEN'].includes(error.code)) {
        $('keys').classList.add('on');
        return false;
      }
    }
  }
  return false;
}
const input = $('input'), drafts = new Map();
input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, innerHeight * 0.4) + 'px'; });
function leaveComposer() {
  if (state.current) {
    const key = sessionKey(state.current);
    if (input.value) drafts.set(key, input.value);
    else drafts.delete(key);
  }
  composing = false;
  cancelVoice();
  voiceStatus('');
}

const VOICE_SECONDS = 120, VOICE_BYTES = 8 * 1024 * 1024;
const mic = $('mic');
let voice = null, composing = false;
const recordTypes = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4'];
function recordType() {
  return window.isSecureContext && navigator.mediaDevices?.getUserMedia && window.MediaRecorder?.isTypeSupported
    ? recordTypes.find((type) => MediaRecorder.isTypeSupported(type)) : undefined;
}
function voiceStatus(text, error = false) {
  const status = $('voice-status');
  if (status.textContent !== text) status.textContent = text;
  $('voice-row').hidden = !text && !voice;
  $('voice-row').classList.toggle('error', error);
}
function renderVoice() {
  const session = state.current;
  const usable = !!session?.agent && !session.dead && session.state !== 'exited';
  if (voice && (!usable || voice.key !== sessionKey(session))) cancelVoice('会话已变化，录音未使用。');
  mic.hidden = !usable || !state.transcriptionSupported || !recordType();
  const recording = voice?.phase === 'recording';
  mic.classList.toggle('recording', recording);
  mic.disabled = !!(sending || (voice && !recording));
  mic.title = recording ? '停止并转写' : '开始录音';
  mic.setAttribute('aria-label', mic.title);
  $('voice-cancel').hidden = !voice;
  $('send').disabled = !!(session?.dead || sending || voice);
}
function activeVoice(job) {
  return voice === job && state.view === 'chat' && state.visit === job.visit && state.current && sessionKey(state.current) === job.key;
}
function releaseMic(job) {
  const stream = job.stream;
  job.stream = null;
  for (const track of stream?.getTracks() || []) track.stop();
}
function cancelVoice(message = '', error = false) {
  const job = voice;
  if (!job) return;
  voice = null;
  clearInterval(job.timer);
  job.controller?.abort();
  if (job.recorder && job.recorder.state !== 'inactive') job.recorder.stop();
  releaseMic(job);
  job.chunks.length = 0;
  voiceStatus(message, error);
  renderVoice();
}
function finishRecording(job) {
  if (!activeVoice(job) || job.phase !== 'recording') return;
  if (job.recorder.state === 'inactive') return cancelVoice('录音中断，请重新录制。', true);
  job.phase = 'finishing';
  clearInterval(job.timer);
  job.recorder.stop();
  releaseMic(job);
  voiceStatus('转写中…'); renderVoice();
}
function insertTranscript(job) {
  if (!activeVoice(job) || !job.text || composing) return;
  const text = job.text;
  input.value += (input.value && !input.value.endsWith('\n') ? '\n' : '') + text;
  input.dispatchEvent(new Event('input'));
  cancelVoice('已转写，请检查文字后发送。');
}
input.addEventListener('compositionstart', () => { composing = true; });
input.addEventListener('compositionend', () => {
  composing = false;
  const job = voice;
  if (job?.text) setTimeout(() => insertTranscript(job), 0);
});
async function uploadVoice(job) {
  if (!activeVoice(job)) return;
  if (job.phase !== 'finishing') return cancelVoice('录音中断，请重新录制。', true);
  const blob = new Blob(job.chunks, { type: job.recorder.mimeType || job.type });
  job.chunks.length = 0;
  if (!blob.size) return cancelVoice('没有收到录音，请重新录制。', true);
  job.controller = new AbortController();
  try {
    const { text } = await api('/api/transcribe', blob, () => activeVoice(job), job.controller.signal);
    if (!activeVoice(job)) return;
    if (typeof text !== 'string' || !text.trim()) return cancelVoice('没有听到语音，请重新录制。', true);
    job.text = text;
    insertTranscript(job);
  } catch (error) {
    if (activeVoice(job)) cancelVoice(error instanceof TypeError ? '录音上传失败，请检查连接后重录。' : error.message, true);
  }
}
mic.addEventListener('click', async () => {
  if (voice) return finishRecording(voice);
  const type = recordType();
  if (!type || mic.hidden || sending || !state.current) return;
  const job = voice = { type, visit: state.visit, key: sessionKey(state.current), phase: 'permission', bytes: 0, chunks: [] };
  voiceStatus('等待麦克风权限…'); renderVoice();
  try {
    job.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!activeVoice(job)) { releaseMic(job); return; }
    for (const track of job.stream.getTracks()) track.addEventListener('ended', () => {
      if (activeVoice(job) && job.phase === 'recording') cancelVoice('麦克风已断开，请重新录制。', true);
    });
    job.recorder = new MediaRecorder(job.stream, { mimeType: type, audioBitsPerSecond: 64000 });
    job.recorder.ondataavailable = ({ data }) => {
      if (!activeVoice(job) || !data.size) return;
      job.bytes += data.size;
      if (job.bytes > VOICE_BYTES) cancelVoice('录音超过 8 MB，请录短一点。', true);
      else job.chunks.push(data);
    };
    job.recorder.onstop = () => uploadVoice(job);
    job.recorder.onerror = () => { if (activeVoice(job)) cancelVoice('录音中断，请重新录制。', true); };
    job.recorder.start(1000);
    job.phase = 'recording';
    const start = performance.now();
    voiceStatus('录音中 0 秒'); renderVoice();
    job.timer = setInterval(() => {
      const elapsed = (performance.now() - start) / 1000;
      // Leave room for the codec's final frame so the server accepts the auto-stopped clip.
      if (elapsed >= VOICE_SECONDS - 0.5) finishRecording(job);
      else voiceStatus(`录音中 ${Math.floor(elapsed)} 秒`);
    }, 250);
  } catch (error) {
    if (activeVoice(job)) cancelVoice(error.name === 'NotAllowedError' ? '麦克风权限未开启，请在浏览器设置中允许。' : '无法打开麦克风，请检查设备。', true);
  }
});
$('voice-cancel').addEventListener('click', () => cancelVoice('已取消录音。'));
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
// Pictures for an agent: each is uploaded to the Mac when the message is sent, and the message ends with one
// "图片：<path>" line per picture for the agent to open. The picked ones belong to the open conversation.
const UPLOAD_LIMIT = 20 * 1024 * 1024; // server.py UPLOAD_LIMIT
let attached = []; // { file, url, path }
// Polling re-enables the send button every second; a second tap while pictures upload must not send twice.
let sending = false;
function renderAttached() {
  $('attached').innerHTML = attached.map((item, i) =>
    `<span class="thumb"><img src="${item.url}" alt=""><button type="button" data-drop="${i}" aria-label="移除这张图片">✕</button></span>`).join('');
  $('attached').hidden = !attached.length;
}
function clearAttached() {
  for (const item of attached) URL.revokeObjectURL(item.url);
  attached = []; renderAttached();
}
$('attach').addEventListener('click', () => $('pick').click());
$('pick').addEventListener('change', () => {
  for (const file of $('pick').files) {
    if (file.size > UPLOAD_LIMIT) setStatus('图片超过 20 MB，没有加入：' + file.name, true);
    else attached.push({ file, url: URL.createObjectURL(file), path: null });
  }
  $('pick').value = ''; renderAttached();
});
$('attached').addEventListener('click', (event) => {
  const button = event.target.closest('[data-drop]'); if (!button) return;
  URL.revokeObjectURL(attached.splice(Number(button.dataset.drop), 1)[0].url); renderAttached();
});
$('compose').addEventListener('submit', async (event) => {
  event.preventDefault();
  const pictures = attached, visit = state.visit, isCurrent = () => visit === state.visit;
  let text = input.value; if ((!text.trim() && !pictures.length) || $('send').disabled || sending || voice) return;
  // One id per message: a retry after a dropped connection cannot type it into the agent twice.
  const id = Date.now().toString(36) + Math.random().toString(36).slice(2);
  sending = true; renderVoice(); state.stick = true;
  try {
    try {
      // A picture already uploaded by a send that failed afterwards keeps its path.
      for (const [i, item] of pictures.entries()) {
        if (item.path) continue;
        setStatus(`上传图片 ${i + 1}/${pictures.length}…`);
        item.path = (await api('/api/upload', item.file, isCurrent)).path;
      }
    } catch (error) {
      if (isCurrent()) setStatus(error.message, true);
      return;
    }
    if (!isCurrent()) return;
    if (pictures.length) text = (text.trim() ? text.trimEnd() : '请看图片') + '\n' + pictures.map((item) => '图片：' + item.path).join('\n');
    const sent = await act('send', { text, id }, '发送中…', visit);
    if (sent && isCurrent()) { input.value = ''; input.style.height = 'auto'; clearAttached(); voiceStatus(''); }
  } finally {
    sending = false;
    if (state.current) renderVoice();
  }
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
function sheet(title, note, bodyHtml, actions, onCancel) {
  const dialog = $('sheet');
  const cancel = onCancel || (() => dialog.close());
  dialog.oncancel = onCancel ? (event) => { event.preventDefault(); cancel(); } : null;
  $('sheet-title').textContent = title; $('sheet-note').textContent = note || ''; $('sheet-body').innerHTML = bodyHtml;
  $('sheet-actions').innerHTML = '';
  for (const [label, handler, cls] of [['取消', cancel, ''], ...(actions || [])]) {
    const button = document.createElement('button'); button.textContent = label; button.className = cls || ''; button.type = 'button';
    button.addEventListener('click', handler); $('sheet-actions').append(button);
  }
  if (!$('sheet').open) $('sheet').showModal();
}
function usageResetText(resetAt, now) {
  if (!Number.isFinite(resetAt) || resetAt <= 0) return '刷新时间未提供';
  const remainingMs = resetAt * 1000 - now;
  if (remainingMs <= 0) return '已到刷新时间';
  const totalMinutes = Math.floor(remainingMs / 60000);
  if (totalMinutes === 0) return '距离刷新不足 1 分钟';
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor(totalMinutes / 60) % 24;
  const minutes = totalMinutes % 60;
  const parts = [days && `${days} 天`, hours && `${hours} 小时`, minutes && `${minutes} 分钟`].filter(Boolean);
  return '距离刷新 ' + parts.join(' ');
}
async function showUsage() {
  const session = state.current, visit = state.visit;
  if (!session || $('usage').hidden) return;
  const isPi = session.agent === 'pi';
  if (isPi && session.model_provider !== 'openai-codex') {
    sheet('Pi 模型额度', '', session.model_provider
      ? '<p>当前模型提供方「' + esc(session.model_provider) + '」未接入额度查询。</p>'
      : '<p>暂未识别当前模型的提供方，未查询 Codex 额度。</p>');
    return;
  }
  const agent = isPi ? 'codex' : session.agent;
  const title = (agent === 'codex' ? 'Codex' : 'Claude') + ' 账号额度';
  const note = isPi ? 'Pi 当前使用 Codex 提供方；这里显示本机 Codex CLI 登录账号的额度。' : '显示 Agent Chat 配置的本机登录额度。';
  sheet(title, note, '<p class="muted">正在读取额度…</p>');
  const loading = $('sheet-body').firstElementChild;
  const isCurrent = () => visit === state.visit && loading.isConnected && $('sheet').open;
  const hint = agent === 'codex' ? '在 Codex 窗口（不是 Pi）输入 <code>/usage</code>，选择 Redeem reset。操作完后点「按键」里的「Esc」回到对话。'
    : '免费重置资格和可用次数请查看 <a href="https://claude.ai/settings/usage" target="_blank" rel="noopener noreferrer">Claude 官方用量页</a>。';
  try {
    const data = await api('/api/usage/' + agent, undefined, isCurrent);
    if (!isCurrent()) return;
    const windows = data.windows.map(({ minutes, used }) => {
      const duration = minutes % 1440 === 0 ? minutes / 1440 + ' 天' : minutes % 60 === 0 ? minutes / 60 + ' 小时' : minutes + ' 分钟';
      const remaining = Math.min(100, Math.max(0, 100 - used)).toLocaleString('zh-CN', { maximumFractionDigits: 1 });
      return `<li>${duration}额度 · 剩余 <b>${remaining}%</b> · <span class="usage-reset"></span></li>`;
    });
    const count = agent === 'codex' ? '<p>' + (data.reset_count === null ? '重置次数未返回' : '剩余重置 ' + data.reset_count + ' 次') + '</p>' : '';
    const account = data.account_label ? '本机登录账号 ' + data.account_label : note;
    sheet(title, account, (windows.length ? '<ul>' + windows.join('') + '</ul>' : '<p>暂无订阅额度数据。</p>')
      + count + `<p class="muted">${hint}</p>`);
    const countdowns = $('sheet-body').querySelectorAll('.usage-reset');
    function updateCountdowns() {
      if (!$('sheet').open || !countdowns[0]?.isConnected) return;
      const now = Date.now();
      let pending = false;
      data.windows.forEach(({ reset_at }, index) => {
        const countdown = countdowns[index];
        const label = usageResetText(reset_at, now);
        if (countdown.textContent !== label) countdown.textContent = label;
        pending ||= Number.isFinite(reset_at) && reset_at * 1000 > now;
      });
      if (pending) setTimeout(updateCountdowns, 1000);
    }
    updateCountdowns();
  } catch (error) {
    if (isCurrent()) sheet(title, note, `<p>${esc(error.message)}</p><p class="muted">${hint}</p>`);
  }
}
$('usage').addEventListener('click', showUsage);

async function choosePi(kind, title, name, visit) {
  if (input.value.trim()) { setStatus('请先发送或清空草稿', true); return; }
  setStatus('');
  const path = '/api/sessions/' + encodeURIComponent(name) + '/' + kind;
  const dialog = $('sheet'), body = $('sheet-body');
  const isCurrent = () => visit === state.visit && dialog.open && body.onclick === click;
  const note = '仅本会话生效，不修改默认设置。';
  let pending;
  const click = (event) => {
    const button = event.target.closest('[data-pi-pick]');
    if (button && !pending) load({ op: button.dataset.piPick, id: button.value });
  };
  async function load(request) {
    body.querySelectorAll('button').forEach((button) => { button.disabled = true; });
    const result = api(path, request, isCurrent);
    pending = result;
    try {
      const picker = await result;
      if (!isCurrent()) return;
      if (request.op === 'select') { body.onclick = null; dialog.close(); return; }
      const page = picker.position ? `<div class="row"><button data-pi-pick="prev">上一页</button><span>${esc(picker.position)}</span><button data-pi-pick="next">下一页</button></div>` : '';
      body.innerHTML = `<div class="options">${picker.options.map((o) => `<button type="button" data-pi-pick="select" value="${esc(o.id)}"${o.current ? ' class="current" aria-current="true"' : ''}>${esc(o.label)} <small class="muted">${esc(o.detail)}${o.current ? ' · 当前' : ''}</small></button>`).join('') || '<p class="muted">没有可选项。</p>'}</div>${page}`;
    } catch (error) {
      if (isCurrent()) body.innerHTML = `<p>${esc(error.message)}</p>`;
    } finally { pending = null; }
  }
  sheet(title, note, '<p class="muted">正在读取 Pi 的选项…</p>', [], async () => {
    const stillCurrent = () => visit === state.visit && !dialog.open;
    body.onclick = null;
    dialog.close();
    try { await pending; } catch (_) {}
    if (dialog.open || state.view === 'login') return;
    try { await api(path, { op: 'cancel' }, stillCurrent); }
    catch (error) { if (stillCurrent()) setStatus(error.message, true); }
  });
  body.onclick = click;
  await load({ op: 'open' });
}

async function choose(kind) {
  const s = state.current, visit = state.visit;
  const title = kind === 'model' ? '选择模型' : '选择推理深度';
  if (s.agent === 'pi') return choosePi(kind, title, s.name, visit);
  let options = state.options[s.agent];
  if (kind === 'model' || s.agent === 'codex') {
    sheet(title, '', '<p class="muted">正在读取可用选项…</p>');
    const loading = $('sheet-body').firstElementChild;
    try {
      options = await api('/api/sessions/' + encodeURIComponent(s.name) + '/options', undefined, () => visit === state.visit);
    } catch (error) {
      if (loading.isConnected && $('sheet').open && visit === state.visit) loading.textContent = error.message;
      return;
    }
    if (!loading.isConnected || !$('sheet').open || visit !== state.visit) return;
  }
  if (!options) return;
  const list = kind === 'model' ? options.models : options.efforts;
  const current = options.current || s;
  sheet(title, kind === 'model' ? '仅本会话生效，不改其他会话的默认模型。切换约需几秒。' : options.effort_note,
    list.length ? '<div class="options">' + list.map((o) => `<button type="button" data-id="${esc(o.id)}" class="${current[kind] === o.id ? 'current' : ''}">${esc(o.label)}</button>`).join('') + '</div>'
      : '<p class="muted">当前模型没有可切换的推理档位。</p>');
  $('sheet-body').onclick = async (event) => {
    const id = event.target.dataset.id; if (!id) return;
    $('sheet').close(); state.stick = true;
    await act(kind, { [kind]: id }, '正在切换到 ' + id + '…', visit);
  };
}
$('model').addEventListener('click', () => choose('model'));
$('effort').addEventListener('click', () => choose('effort'));
$('new').addEventListener('click', () => {
  const visit = state.visit;
  sheet('开启新对话？', '当前 Agent 清空上下文开始新对话，tmux 会话保留。', '', [['开启', async () => {
    $('sheet').close(); await act('new', {}, '正在开启新对话…', visit);
  }, 'primary']]);
});
$('kill').addEventListener('click', () => {
  const s = state.current, visit = state.visit;
  sheet('关闭 tmux 会话？', '将终止「' + s.nickname + '」（' + s.name + '）和其中运行的 Agent，无法恢复。', '', [['确认关闭', async () => {
    $('sheet').close(); if (await act('kill', { confirm: s.name }, '正在关闭…', visit)) show('home');
  }, 'danger']]);
});
$('rename').addEventListener('click', () => {
  const s = state.current, visit = state.visit;
  sheet('修改昵称', '只改网页里显示的名字，不改 tmux 会话名（' + s.name + '）。', `<input id="nick" maxlength="40" value="${esc(s.nickname)}">`, [['保存', async () => {
    const nickname = $('nick').value; $('sheet').close();
    if (await act('rename', { nickname }, undefined, visit)) { state.current.nickname = nickname.trim() || s.name; renderHeader(); }
  }, 'primary']]);
});
$('create-open').addEventListener('click', () => {
  const agents = Object.entries(state.agents).map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join('')
    + '<option value="shell">Zsh</option>';
  sheet('新建 Agent 会话', '在主目录 ~ 下启动一个新的 tmux 长对话。', `<p><select id="new-agent">${agents}</select></p><input id="new-nick" maxlength="40" placeholder="昵称">`, [['创建', async () => {
    const body = { agent: $('new-agent').value, nickname: $('new-nick').value }; $('sheet').close();
    try { await api('/api/sessions', body); pollHome(); } catch (error) { $('home-status').textContent = error.message; }
  }, 'primary']]);
});

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

// ---- reading size shared by the chat and home (per browser) ----
const clampReadingSize = (px) => Math.min(20, Math.max(10, px));
let readingSize = 12.5;
function applyReadingSize() { document.documentElement.style.setProperty('--reading-size', readingSize + 'px'); }
try {
  // Keep the original key so sizes chosen before the home page used it still apply.
  const saved = Number(localStorage.getItem('logSize')?.trim() || NaN);
  if (Number.isFinite(saved)) readingSize = clampReadingSize(saved);
} catch (_) {}
applyReadingSize();
function changeReadingSize(step) {
  readingSize = clampReadingSize(readingSize + step);
  applyReadingSize();
  try { localStorage.setItem('logSize', String(readingSize)); } catch (_) {}
}
for (const button of document.querySelectorAll('[data-font-step]')) {
  button.addEventListener('click', () => changeReadingSize(Number(button.dataset.fontStep)));
}

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
