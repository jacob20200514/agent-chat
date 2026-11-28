"""Run with Python and Google Chrome; deterministic recorder and network events."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
<script>
const session = (name, agent) => ({name, nickname: name, agent, state: 'idle', created: 1});
const sessions = {A: session('A', 'claude'), B: session('B', 'codex'), Shell: session('Shell', null)};
let allowed = false, uploads = [], sends = [], releases = 0, requests = 0, pausePermission = false, grant, voiceMenu = false;
const output = {A: [], B: [], Shell: []};
let imageUploads = 0;
const response = (data, status = 200) => ({ok: status === 200, status, json: async () => data});
Object.defineProperty(navigator, 'mediaDevices', {value: {getUserMedia: async () => {
  requests++;
  if (!allowed) throw new DOMException('denied', 'NotAllowedError');
  if (pausePermission) await new Promise(resolve => { grant = resolve; });
  return {getTracks: () => [{stop: () => releases++, addEventListener() {}}]};
}}});
// Safari can advertise both formats, but its WebM can contain broken audio timestamps.
// Exercise MP4 preference and the fallback on browsers without MP4.
window.MediaRecorder = class {
  static isTypeSupported(type) { return type === 'audio/webm;codecs=opus' || (innerWidth < 900 && type === 'audio/mp4'); }
  constructor(_, {mimeType}) { this.mimeType = mimeType; this.state = 'inactive'; }
  start() { this.state = 'recording'; }
  stop() {
    this.state = 'inactive';
    queueMicrotask(() => { this.ondataavailable({data: new Blob(['recorded'])}); this.onstop(); });
  }
};
window.fetch = async (url, options) => {
  if (url === '/api/me') return response({login: true, password_set: true});
  if (url === '/api/sessions') return response({sessions: Object.values(sessions), agents:{}, options:{}, transcription_supported: true});
  if (url === '/api/transcribe') return new Promise(resolve => uploads.push({options, reply: (data, status) => resolve(response(data, status))}));
  if (url === '/api/upload') { imageUploads++; return response({path: '/tmp/test.png'}); }
  const [,name,action] = url.match(/^\/api\/sessions\/([^/]+)\/(\w+)/);
  if (action === 'output') return response({session: sessions[name], text: ['Ready', ...output[name]].join('\n\n'), version: String(output[name].length)});
  if (action === 'send') {
    const body = JSON.parse(options.body);
    sends.push({name, ...body});
    if (voiceMenu) return response({code: 'VOICE_CHOICE_OPEN', error: '请先退出菜单'}, 409);
    output[name].push('❯ ' + body.text);
    return response({});
  }
  throw Error(url);
};
const q = (id) => document.getElementById(id);
const check = (condition, error) => { if (!condition) throw Error(error); };
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
async function wait(fn, error) {
  const limit = Date.now() + 3500;
  while (!fn()) { check(Date.now() < limit, error); await sleep(20); }
}
function open(name) {
  if (q('chat').classList.contains('on') && innerWidth < 900) q('back').click();
  document.querySelector(`.card[data-name="${name}"]`).click();
}
const status = () => q('voice-status').textContent;
const recording = () => q('voice-hold').classList.contains('listening');
function pointer(type, y = 160) {
  q('voice-hold').dispatchEvent(new PointerEvent(type, {pointerId: 1, isPrimary: true, button: 0, clientY: y, cancelable: true, bubbles: true}));
}
const press = () => pointer('pointerdown');
const release = () => pointer('pointerup');
const enter = (opts) => q('input').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', cancelable:true, ...opts}));
window.addEventListener('load', async () => {
  const result = document.body.appendChild(document.createElement('pre')); result.id = 'voice-test';
  try {
    await wait(() => document.querySelector('.card'), 'no sessions');
    open('A');
    check(q('attach').nextElementSibling === q('mic') && !q('mic').hidden &&
      q('input').getBoundingClientRect().width >= 100 && q('send').getBoundingClientRect().right <= innerWidth, 'composer layout');
    q('input').value = '原草稿';
    const files = new DataTransfer(); files.items.add(new File(['img'], 'one.png', {type:'image/png'}));
    q('pick').files = files.files; q('pick').dispatchEvent(new Event('change'));
    q('mic').click();
    const hold = q('voice-hold');
    check(q('input').hidden && q('send').hidden && !hold.hidden && hold.getBoundingClientRect().width >= 180 &&
      hold.textContent === '按住说话' && requests === 0 && q('mic').title === '切换到文字输入', 'mode must replace input without starting the microphone');
    press(); await wait(() => status().includes('权限未开启'), 'permission failure');
    allowed = true; pausePermission = true;
    press(); await wait(() => grant, 'permission request not pending'); release(); grant();
    await wait(() => releases === 1, 'late permission leaked microphone');
    check(uploads.length === 0 && !recording(), 'releasing before permission uploaded an audio clip');
    pausePermission = false;
    press(); await wait(recording, 'did not record');
    q('compose').requestSubmit(); enter({});
    check(q('send').disabled && sends.length === 0 && uploads.length === 0, 'holding sent a message');
    release(); await wait(() => uploads.length === 1, 'no upload after release');
    check(releases === 2 && uploads[0].options.body instanceof Blob &&
      uploads[0].options.body.type.includes(innerWidth < 900 ? 'mp4' : 'webm'), 'wrong recording body or mic left open');
    uploads[0].reply({text:'Termius 和 tmux'});
    await wait(() => status().includes('已发送'), 'transcript not sent after release');
    check(q('input').hidden && hold.textContent === '按住说话' && sends.length === 1 && sends[0].name === 'A' &&
      sends[0].text === 'Termius 和 tmux' && sends[0].voice_session.agent === 'claude' && sends[0].voice_session.created === 1 && imageUploads === 0,
      'release did not send just the transcript to the original agent');
    await wait(() => document.querySelector('#log .block.user')?.textContent.includes('Termius 和 tmux'), 'transcript did not become a user bubble');
    q('mic').click();
    check(!q('input').hidden && hold.hidden && q('input').value === '原草稿' && !q('attached').hidden, 'switching back lost draft or picture');
    q('mic').click(); press(); await wait(recording, 'second recording failed');
    pointer('pointermove', 60); check(hold.textContent === '松开取消', 'upward cancellation not indicated');
    pointer('pointerup', 60); await sleep(30);
    check(releases === 3 && uploads.length === 1, 'slide to cancel uploaded or leaked mic');
    press(); await wait(recording, 'third recording failed');
    pointer('pointercancel'); await sleep(30);
    check(releases === 4 && uploads.length === 1, 'interrupted press uploaded or leaked mic');
    hold.focus(); hold.dispatchEvent(new KeyboardEvent('keydown', {key:' '}));
    await wait(recording, 'keyboard hold failed');
    hold.dispatchEvent(new KeyboardEvent('keyup', {key:' '})); await wait(() => uploads.length === 2, 'missing keyboard upload');
    q('mic').click();
    q('input').value += '，还在编辑';
    voiceMenu = true;
    uploads[1].reply({text:'第二段'});
    await wait(() => !q('voice-retry').hidden, 'voice retry not shown after a blocked send');
    check(sends.length === 2 && q('input').value === '原草稿，还在编辑' && !q('attached').hidden, 'blocked voice overwrote draft or retried the menu');
    voiceMenu = false; q('voice-retry').click();
    await wait(() => sends.length === 3 && status().includes('已发送'), 'voice retry failed');
    check(sends[2].id === sends[1].id && sends[2].text === '第二段' && imageUploads === 0 && q('input').value === '原草稿，还在编辑', 'voice retry changed text or its identity');
    q('mic').click(); press(); await wait(recording, 'failure recording did not start');
    release(); await wait(() => uploads.length === 3, 'missing third upload');
    uploads[2].reply({error:'测试转写失败'}, 503);
    await wait(() => status().includes('测试转写失败'), 'failure not displayed');
    check(!q('attached').hidden && q('input').value === '原草稿，还在编辑' && sends.length === 3, 'failure lost draft or image or sent something');
    press(); await wait(recording, 'stale recording did not start');
    release(); await wait(() => uploads.length === 4, 'missing fourth upload');
    open('B'); q('input').value = 'B 的草稿'; open('A');
    uploads[3].reply({text:'这是迟到的录音'});
    await sleep(60);
    check(uploads[3].options.signal.aborted && q('input').value === '原草稿，还在编辑' && hold.hidden, 'stale response modified revisited draft');
    check(sends.length === 3, 'stale transcription caused an unexpected send');
    q('send').click(); await wait(() => sends.length === 4 && !q('input').value && !q('send').disabled, 'manual send failed');
    check(sends[3].name === 'A' && sends[3].text === '原草稿，还在编辑' && !sends[3].voice_session, 'incorrect manual send');
    open('B'); check(q('input').value === 'B 的草稿', 'draft entered the wrong chat');
    open('Shell'); check(q('mic').hidden && !q('send').disabled, 'Shell unexpectedly got a microphone or lost send');
    result.textContent = 'PASS';
  } catch (error) { result.textContent = 'FAIL: ' + error.message; }
});
</script>
"""


def main():
    chrome = (os.environ.get('CHROME_BIN') or shutil.which('google-chrome')
              or '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    html = (ROOT / 'static/index.html').read_text().replace('<script src="/app.js"></script>', HARNESS + '<script src="app.js"></script>')
    with tempfile.TemporaryDirectory(prefix='agentchat-voice-test-') as directory:
        path = Path(directory)
        (path / 'index.html').write_text(html)
        shutil.copyfile(ROOT / 'static/app.js', path / 'app.js')
        for width in (1200, 375):
            result = subprocess.run([
                chrome, '--headless', '--disable-gpu', '--no-first-run', '--disable-background-networking',
                f'--user-data-dir={path / str(width)}', f'--window-size={width},820',
                '--virtual-time-budget=16000', '--dump-dom', (path / 'index.html').as_uri(),
            ], capture_output=True, text=True, timeout=30, check=True)
            marker = re.search(r'<pre id="voice-test">(.*?)</pre>', result.stdout)
            status = marker[1] if marker else 'no browser result'
            print(f'{width}px: {status}', flush=True)
            if status != 'PASS':
                raise SystemExit(1)


if __name__ == '__main__':
    main()
