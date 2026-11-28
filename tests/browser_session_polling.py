"""Run with python3 tests/browser_session_polling.py and an installed Google Chrome."""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
<script>
const rows = Object.fromEntries(['A', 'B'].map((name) => [name, {
  name, nickname: '会话' + name, agent: name === 'A' ? 'claude' : 'codex', agent_label: name === 'A' ? 'Claude' : 'Codex',
  created: 1, state: 'idle', dead: false, reply: {scope: name, id: 'old', at: Date.now() - 60000},
}]));
const output = {A: ['a1', 'A 的旧回复'], B: ['b1', 'B 的旧回复']};
rows.B.preview = 'B 的消息预览';
const calls = [], usageReads = [];
let hold, hidden = false, resetCount = 3;
Object.defineProperty(document, 'hidden', {get: () => hidden});
function visibility(value) { hidden = value; document.dispatchEvent(new Event('visibilitychange')); }
const response = (data, status = 200) => ({ok: status === 200, status, json: async () => data});
window.fetch = async (url, options) => {
  if (url === '/api/me') return response({login: true, password_set: true});
  if (url === '/api/sessions') {
    calls.push({action: 'list', preview: rows.A.preview});
    return response({sessions: Object.values(rows).map((row) => ({...row})), agents: {}, options: {}, usage_supported: true});
  }
  if (url.startsWith('/api/usage/')) {
    const codex = url.endsWith('/codex');
    usageReads.push(`${options.method || 'GET'}:${url.split('/').at(-1)}`);
    const now = Date.now() / 1000;
    const windows = codex ? [
      {minutes: 10080, used: 25, reset_at: now + 93785},
      {minutes: 300, used: 0, reset_at: now + 1.5},
    ] : [
      {minutes: 300, used: 37.5, reset_at: now + 4505},
      {minutes: 10080, used: 100, reset_at: null},
    ];
    return response({windows, reset_count: codex ? resetCount : null, account_label: 'ab***@example.com'});
  }
  const [, name, action, since] = url.match(/^\/api\/sessions\/([ABPC])\/(\w+)(?:\?since=(.*))?$/);
  const active = document.querySelector('#chat.on') && title() === rows[name].nickname;
  const call = {name, action, since, active, body: options.body && JSON.parse(options.body)};
  calls.push(call);
  if (hold && active && hold.name === name && hold.action === action) {
    const pending = hold; hold = null;
    pending.call = call;
    return new Promise((resolve) => { pending.release = (data, status) => resolve(response(data, status)); });
  }
  if (name === 'P' && ['model', 'effort'].includes(action)) {
    if (call.body.op === 'open') return response({ options: action === 'model' ? [
      {id: 'openai/demo', label: 'demo', detail: 'openai', current: true},
      {id: 'openai/new-model', label: 'new-model', detail: 'openai', current: false},
    ] : [{id: 'medium', label: 'medium', detail: 'Moderate reasoning', current: true}] });
    if (call.body.op === 'select') rows.P.model = call.body.id.split('/').at(-1);
    return response({});
  }
  if (action !== 'output') throw Error('unexpected request ' + url);
  const [version, text] = output[name]; call.version = version;
  return response({session: {...rows[name]}, version, ...(since === version ? {} : {text})});
};
function defer(name, action = 'output') { hold = {name, action}; return hold; }
function check(value, label) { if (!value) throw new Error(label); }
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(condition, label) {
  const deadline = Date.now() + 7500;
  while (!condition()) {
    check(Date.now() < deadline, label);
    await sleep(10);
  }
}
const card = (name) => document.querySelector('.card[data-name="' + name + '"]');
const dot = (name) => card(name)?.querySelector('.dot.unread');
const title = () => document.querySelector('#chat-name').textContent;
const message = () => document.querySelector('#log').textContent;
const activeCalls = (name) => calls.filter((c) => c.active && c.name === name && c.action === 'output');
const usageText = () => document.querySelector('#sheet').textContent;
const closeSheet = () => document.querySelector('#sheet-actions button').click();
function open(name) {
  if (!document.body.classList.contains('wide') && document.querySelector('#chat.on')) {
    document.querySelector('#back').click();
  }
  card(name).click();
}
async function run() {
  await until(() => card('B'), 'session list did not load');
  check(calls.some((c) => c.action === 'list'), 'initial baseline was not sampled');
  check(!dot('A'), 'existing history is unread on first visit');
  let pending = defer('A');
  open('A');
  await until(() => pending.release, 'A request was not held');
  open('B');
  await until(() => message().includes('B 的旧回复'), 'opening B did not render');
  check(activeCalls('B')[0].since === undefined, 'B should always open with a full snapshot');
  pending.release({session: rows.A, version: 'a1', text: output.A[1]});
  await sleep(80);
  check(title() === '会话B' && message().includes('B 的旧回复'), 'late A response replaced B');
  const bCount = activeCalls('B').length;
  output.B = ['b2', 'B 的新回复'];
  await until(() => message().includes('B 的新回复'), 'B stopped polling after the switch');
  await sleep(80);
  check(activeCalls('B').length === bCount + 1 && activeCalls('B').at(-1).since === 'b1', 'B has a duplicate chain or an invalid since');
  check(usageReads.length === 0, 'normal polling requested account usage');
  const newButton = document.querySelector('#new'), usageButton = document.querySelector('#usage');
  check(newButton.nextElementSibling === usageButton && !usageButton.hidden &&
    usageButton.getBoundingClientRect().left > newButton.getBoundingClientRect().left, 'usage button is not visible to the right of New');
  usageButton.click();
  await until(() => usageText().includes('剩余重置 3 次'), 'Codex reset count was not shown');
  check(usageText().includes('/usage') && usageText().includes('Esc') &&
    usageText().includes('7 天额度 · 剩余 75% · 距离刷新 1 天 2 小时 3 分钟') &&
    usageText().includes('5 小时额度 · 剩余 100% · 距离刷新不足 1 分钟') &&
    usageText().includes('ab***@example.com'), 'Codex quota lacks its window, account or command hint');
  await until(() => usageText().includes('已到刷新时间'), 'countdown did not reach the reset time while the sheet was open');
  check(usageReads.length === 1, 'the countdown fetched account usage again');
  closeSheet();
  resetCount = 0;
  document.querySelector('#usage').click();
  await until(() => usageText().includes('剩余重置 0 次'), 'a confirmed zero reset count was lost');
  closeSheet();

  const compose = document.querySelector('#compose'), draft = document.querySelector('#input');
  const esc = document.querySelector('[data-key="Escape"]');
  for (const [code, panel] of [['CODEX_USAGE_OPEN', '用量页'], ['CODEX_WARNINGS_OPEN', '警告面板']]) {
    if (document.querySelector('#keys.on')) document.querySelector('#keys-toggle').click();
    const sendCount = calls.filter((c) => c.action === 'send').length;
    const keyCount = calls.filter((c) => c.action === 'key').length;
    pending = defer('B', 'send');
    draft.value = '保留这条草稿';
    compose.requestSubmit();
    await until(() => pending.release, `${panel}: send was not held`);
    pending.release({code, error: `未发送。请点 Esc 退出${panel}。`}, 409);
    await until(() => document.querySelector('#status.error')?.textContent.includes('未发送'), `${panel}: blocked send was not explained`);
    await sleep(80);
    check(draft.value === '保留这条草稿' && esc.getBoundingClientRect().width > 0 &&
      calls.filter((c) => c.action === 'send').length === sendCount + 1 &&
      calls.filter((c) => c.action === 'key').length === keyCount, `${panel}: blocked send lost the draft, hid Esc or sent extra input`);
    pending = defer('B', 'key');
    esc.click();
    await until(() => pending.release, 'manual Esc was not sent');
    check(pending.call.body.key === 'Escape', 'manual Esc sent an unexpected key');
    pending.release({});
    pending = defer('B', 'send');
    compose.requestSubmit();
    await until(() => pending.release, 'draft was not sent after Esc');
    check(pending.call.body.text === '保留这条草稿', 'the retried draft was changed');
    pending.release({});
    await until(() => !draft.value, 'successful send after Esc did not clear the draft');
  }

  pending = defer('B');
  await until(() => pending.release, 'old B request was not held');
  output.B = ['b3', 'B 再进入的新回复'];
  open('B');
  await until(() => message().includes('B 再进入的新回复'), 'reopening B did not refresh its log');
  pending.release({session: rows.B, version: 'b2', text: 'B 再进入前的旧回复'});
  await sleep(80);
  check(message().includes('B 再进入的新回复'), 'same-name old response replaced the new visit');

  pending = defer('B');
  await until(() => pending.release, 'B request before hiding was not held');
  visibility(true);
  const hiddenCount = calls.length;
  await sleep(1200);
  check(calls.length === hiddenCount, 'a timer polled while the page was hidden');
  output.B = ['b4', 'B 恢复后的新回复'];
  visibility(false);
  await until(() => message().includes('B 恢复后的新回复'), 'restoring visibility did not refresh B');
  pending.release({error: '会话不存在或已关闭'}, 404);
  await sleep(1700);
  check(document.querySelector('#chat.on') && message().includes('B 恢复后的新回复'), 'stale 404 exited the resumed chat');

  if (!document.body.classList.contains('wide')) document.querySelector('#back').click();
  output.A = ['a2', 'A 的旧回复\n\n❯ 只有用户新输入'];
  rows.A.preview = '只有用户新输入';
  await until(() => card('A').textContent.includes(rows.A.preview), 'list did not resample A after the new prompt');
  check(!dot('A'), 'user input alone was marked unread');
  rows.A.state = 'busy';
  output.A = ['a3', output.A[1] + '\n\n· 运行中'];
  await until(() => card('A').querySelector('.dot.busy'), 'A busy update was not shown');
  const busyStyle = getComputedStyle(card('A').querySelector('.dot.busy'));
  check(busyStyle.backgroundColor === 'rgb(224, 160, 48)' && busyStyle.animationName === 'pulse',
    'working dot is not flashing yellow');
  check(!dot('A'), 'busy redraw was marked unread');
  rows.A.state = 'idle';
  output.A = ['a4', 'A 的旧回复\n\n❯ 只有用户新输入\n\n● 工具输出和部分文字'];
  await until(() => card('A').querySelector('.dot.idle'), 'A idle update was not shown');
  const idleStyle = getComputedStyle(card('A').querySelector('.dot.idle'));
  check(idleStyle.backgroundColor === 'rgb(16, 163, 127)' && idleStyle.animationName === 'none',
    'idle dot did not return to steady green');
  check(!dot('A'), 'idle between tools was marked unread');
  rows.A.reply = {scope: 'A', id: 'completed', at: Date.now()};
  output.A = ['a5', 'A 的旧回复\n\n❯ 只有用户新输入\n\n● A 的未读回复'];
  await until(() => dot('A'), 'completed reply did not show an unread dot');
  check(dot('A').getAttribute('aria-label') === '有未读消息' &&
    getComputedStyle(dot('A')).backgroundColor === 'rgb(59, 130, 246)', 'unread dot is missing its label or color');
  open('A');
  await until(() => message().includes('A 的未读回复'), 'opening unread A did not display the reply');
  if (!document.body.classList.contains('wide')) document.querySelector('#back').click();
  await until(() => !dot('A'), 'viewed reply remained unread');
  check(!localStorage.getItem('agentChatReadReplies').includes('未读回复'), 'local storage contained message text');

  open('A');
  await until(() => message().includes('A 的未读回复'), 'A did not reopen before testing actions');
  document.querySelector('#usage').click();
  await until(() => usageText().includes('Claude 官方用量页'), 'Claude official reset information was not shown');
  check(usageText().includes('5 小时额度 · 剩余 62.5% · 距离刷新 1 小时 15 分钟') &&
    usageText().includes('7 天额度 · 剩余 0% · 刷新时间未提供') && usageReads.join(',') === 'GET:codex,GET:codex,GET:claude',
    'Claude quota lacks its window or fallback, or the usage sheet wrote to the API');
  closeSheet();
  const sendCount = calls.filter((c) => c.action === 'send').length;
  pending = defer('A', 'send');
  document.querySelector('#input').value = 'a test draft';
  document.querySelector('#compose').requestSubmit();
  await until(() => pending.release, 'send was not held');
  open('B');
  pending.release({error: '模拟连接中断'}, 500);
  await sleep(80);
  check(calls.filter((c) => c.action === 'send').length === sendCount + 1, 'a message retry ran after leaving A');
  open('A');
  pending = defer('A', 'kill');
  document.querySelector('#kill').click();
  document.querySelector('#sheet-actions .danger').click();
  await until(() => pending.release, 'kill was not held');
  open('B');
  pending.release({});
  await sleep(80);
  check(document.querySelector('#chat.on') && title() === '会话B', 'a late kill acknowledgement closed B');

  pending = defer('A');
  open('A');
  await until(() => pending.release, 'current A request was not held');
  pending.release({error: '会话不存在或已关闭'}, 404);
  await until(() => document.querySelector('#home.on'), 'a 404 for the current chat did not return to the session list');

  // Pi's transcript metadata, rather than tool output or terminal redraws, controls its unread dot.
  rows.P = {name: 'P', nickname: 'Pi 会话', agent: 'pi', agent_label: 'Pi', created: 1, state: 'busy',
    preview: '正在处理的请求', reply: {scope: 'P', id: null}, model_provider: 'openai-codex'};
  output.P = ['p1', 'Pi 的工具输出'];
  await until(() => card('P')?.querySelector('.dot.busy'), 'Pi did not show as busy');
  check(card('P').textContent.includes('运行中') && card('P').textContent.includes('正在处理的请求') &&
    getComputedStyle(card('P').querySelector('.dot.busy')).animationName === 'pulse', 'Pi lacks a preview or animated busy dot');
  check(!dot('P'), 'Pi tool output lit the unread dot');
  rows.P.reply = {scope: 'P', id: 'completed', at: Date.now()};
  rows.P.preview = '正式回复还在生成';
  await until(() => card('P').textContent.includes(rows.P.preview), 'Pi busy update was not rendered');
  check(!dot('P'), 'Pi lit the unread dot before settling');
  rows.P.state = 'idle';
  output.P = ['p2', 'Pi 的正式回复'];
  await until(() => dot('P'), 'Pi did not mark the completed reply unread');
  open('P');
  await until(() => message().includes('Pi 的正式回复'), 'Pi reply did not render');
  check(newButton.nextElementSibling === usageButton && !usageButton.hidden, 'Pi usage button is not next to New');
  usageButton.click();
  await until(() => usageText().includes('剩余重置 0 次'), 'Pi did not reuse Codex quota');
  check(usageReads.at(-1) === 'GET:codex' && usageReads.length === 4, 'Pi should make one read-only Codex quota query');
  closeSheet();
  for (const [provider, expected] of [['openai', '未接入额度查询'], [null, '暂未识别']]) {
    const before = activeCalls('P').length;
    rows.P.model_provider = provider;
    await until(() => activeCalls('P').length > before, 'Pi model provider did not repoll');
    usageButton.click();
    check(usageText().includes(expected) && usageReads.length === 4, 'Pi queried Codex without a Codex provider');
    closeSheet();
  }
  if (!document.body.classList.contains('wide')) document.querySelector('#back').click();
  await until(() => card('P') && !dot('P'), 'Pi reply still unread after opening');
  open('P');
  draft.value = '保留 Pi 草稿';
  document.querySelector('#model').click();
  check(!document.querySelector('#sheet').open && draft.value === '保留 Pi 草稿' &&
    document.querySelector('#status').textContent.includes('草稿'), 'Pi picker disturbed an unsent draft');
  draft.value = '';
  document.querySelector('#model').click();
  const selectedModel = '[data-pi-pick="select"][value="openai/new-model"]';
  await until(() => document.querySelector(selectedModel), 'Pi model dialog did not render the choices');
  check(document.querySelector('[data-pi-pick][aria-current="true"]').value === 'openai/demo', 'Pi model dialog did not mark the current choice');
  document.querySelector(selectedModel).click();
  await until(() => !document.querySelector('#sheet').open && document.querySelector('#chat-sub').textContent.includes('new-model'),
    'direct Pi selection did not close or update the header');
  check(calls.some((c) => c.action === 'model' && c.body?.op === 'select' && c.body.id === 'openai/new-model'), 'wrong Pi model posted');
  document.querySelector('#effort').click();
  await until(() => document.querySelector('[data-pi-pick][value="medium"]'), 'Pi effort dialog did not render the choices');
  closeSheet();
  await until(() => calls.some((c) => c.action === 'effort' && c.body?.op === 'cancel'), 'Pi effort dialog did not cancel the native menu');

  if (!document.body.classList.contains('wide')) document.querySelector('#back').click();
  rows.C = {name: 'C', nickname: 'CodeBuddy', agent: 'codebuddy', agent_label: 'CodeBuddy', created: 2, state: 'idle',
    reply: {scope: 'C', id: 'first-answer', at: Date.now()}};
  output.C = ['c1', '相同终端快照'];
  await until(() => dot('C'), 'first CodeBuddy reply completed during this visit was missed');
  open('C');
  await until(() => message().includes('相同终端快照'), 'CodeBuddy output did not render');
  rows.C.reply = {scope: 'C', id: 'same-text-next-turn', at: Date.now()};
  await until(() => JSON.parse(localStorage.getItem('agentChatReadReplies'))['C:2']?.id === 'same-text-next-turn',
    'new reply metadata was ignored when terminal version did not change');
  if (!document.body.classList.contains('wide')) document.querySelector('#back').click();
  await until(() => card('C') && !dot('C'), 'viewed CodeBuddy reply was not cleared');
  rows.B.reply = {scope: 'B', id: 'completed', at: Date.now()};
  await until(() => dot('B'), 'completed Codex reply was missed');
  check(!calls.some((c) => c.action === 'output' && !c.active), 'list still fetched full output for unread');

  // Panes are 100 columns wide and there is no width toggle: a phone wraps each long line inside the block
  // rather than scrolling sideways, and a desktop window shows the whole line.
  open('B');
  await until(() => title() === '会话B', 'B did not open');
  check(!document.querySelector('#width'), 'the width toggle is still on the page');
  const block = document.createElement('div');
  block.className = 'block agent'; block.innerHTML = '<pre>' + '0'.repeat(100) + '</pre><pre>0</pre>';
  document.querySelector('#log').append(block);
  const [long, short] = block.querySelectorAll('pre');
  check(long.scrollWidth <= long.clientWidth, 'a 100-column line overflows sideways');
  check(document.body.classList.contains('wide') || long.getBoundingClientRect().height > short.getBoundingClientRect().height,
    'a phone did not wrap a 100-column line');
  block.remove();

  // The user's message stays one bubble when it starts with "1." or has indented lines; a menu's "❯ 1." row
  // (its key hint follows) stays with the agent's question.
  output.B = ['b9', '❯ 1. 第一项\n  2. 第二项\n     缩进的补充说明\n\n⏺ 收到\n\n❯ 看这个：\n  code line\n      indented code\n  只回复收到' +
    '\n\n⏺ 收到\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend'];
  await until(() => message().includes('Esc to cancel'), 'B did not render the message fixture');
  const bubbles = [...document.querySelectorAll('#log .block.user')].map((b) => b.dataset.text);
  check(JSON.stringify(bubbles) === JSON.stringify(['1. 第一项\n2. 第二项\n   缩进的补充说明', '看这个：\ncode line\n    indented code\n只回复收到']),
    'user bubbles are split wrongly: ' + JSON.stringify(bubbles));
}
function checkTextSize(offset, chat = false) {
  const sizes = {'#home h1': 17, '.card b': 16, '.card .muted': 13, '.card .preview': 13, '.card .tag': 12};
  if (chat) sizes['#log pre'] = 12.5;
  for (const [selector, baseline] of Object.entries(sizes)) {
    const size = getComputedStyle(document.querySelector(selector)).fontSize;
    check(size === `${baseline + offset}px`, `${selector} has size ${size}, expected ${baseline + offset}px`);
  }
}
function adjustTextSize(view, step) {
  const button = document.querySelector(`#${view} [data-font-step="${step}"]`);
  const rect = button?.getBoundingClientRect();
  check(rect?.width > 0 && rect.right <= innerWidth, `font step ${step} is not visible on ${view}`);
  button.click();
}
window.addEventListener('load', async () => {
  const result = document.body.appendChild(document.createElement('pre'));
  result.id = 'browser-result';
  try {
    const reloaded = sessionStorage.getItem('fontReload');
    await until(() => card('B'), 'session list did not load for font test');
    checkTextSize(reloaded ? 2 : 0);
    if (!reloaded) {
      adjustTextSize('home', -1);
      checkTextSize(-1);
      adjustTextSize('home', 1);
      adjustTextSize('home', 1);
    }
    open('B');
    await until(() => message().includes('B 的旧回复'), 'font test chat did not render');
    checkTextSize(reloaded ? 2 : 1, true);
    if (!reloaded) {
      adjustTextSize('chat', -1);
      checkTextSize(0, true);
      adjustTextSize('chat', 1);
      checkTextSize(1, true);
      adjustTextSize('chat', 1);
    }
    document.querySelector('#back').click();
    check(document.querySelector('#home.on'), 'font test did not return to home');
    checkTextSize(2);
    if (!reloaded) {
      sessionStorage.setItem('fontReload', '1');
      location.reload();
      return;
    }
    await run();
    result.textContent = 'PASS';
  } catch (error) { result.textContent = 'FAIL ' + error.message; }
});
</script>
"""


def main():
    chrome = (os.environ.get('CHROME_BIN') or shutil.which('google-chrome')
              or '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not Path(chrome).is_file():
        raise SystemExit('Install Google Chrome or set CHROME_BIN to its executable.')
    html = (ROOT / 'static/index.html').read_text()
    html = html.replace('<script src="/app.js"></script>', HARNESS + '<script src="app.js"></script>')
    with tempfile.TemporaryDirectory(prefix='agentchat-race-') as directory:
        path = Path(directory)
        (path / 'index.html').write_text(html)
        shutil.copyfile(ROOT / 'static/app.js', path / 'app.js')
        for width, label in [(1200, 'desktop'), (375, 'mobile layout')]:
            result = subprocess.run([
                chrome, '--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking',
                f'--user-data-dir={path / str(width)}', f'--window-size={width},820',
                '--virtual-time-budget=62000', '--dump-dom', (path / 'index.html').as_uri(),
            ], capture_output=True, text=True, timeout=30, check=True)
            marker = re.search(r'<pre id="browser-result">(.*?)</pre>', result.stdout)
            status = marker.group(1) if marker else 'no browser result'
            print(f'{label} ({width}px): {status}', flush=True)
            if status != 'PASS':
                raise SystemExit(1)


if __name__ == '__main__':
    main()
