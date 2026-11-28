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
let allowed = false, uploads = [], sends = [], releases = 0;
const response = (data, status = 200) => ({ok: status === 200, status, json: async () => data});
Object.defineProperty(navigator, 'mediaDevices', {value: {getUserMedia: async () => {
  if (!allowed) throw new DOMException('denied', 'NotAllowedError');
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
  if (url === '/api/upload') return response({path: '/tmp/test.png'});
  const [,name,action] = url.match(/^\/api\/sessions\/([^/]+)\/(\w+)/);
  if (action === 'output') return response({session: sessions[name], text: 'Ready', version: 'v1'});
  if (action === 'send') { sends.push({name, ...JSON.parse(options.body)}); return response({}); }
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
const recording = () => q('mic').classList.contains('recording');
const enter = (opts) => q('input').dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', cancelable:true, ...opts}));
window.addEventListener('load', async () => {
  const result = document.body.appendChild(document.createElement('pre')); result.id = 'voice-test';
  try {
    await wait(() => document.querySelector('.card'), 'no sessions');
    open('A');
    check(q('attach').nextElementSibling === q('mic') && !q('mic').hidden &&
      q('input').getBoundingClientRect().width >= 100 && q('send').getBoundingClientRect().right <= innerWidth, 'composer layout');
    q('mic').click(); await wait(() => status().includes('权限未开启'), 'permission failure');
    allowed = true;
    q('input').value = '原草稿';
    const files = new DataTransfer(); files.items.add(new File(['img'], 'one.png', {type:'image/png'}));
    q('pick').files = files.files; q('pick').dispatchEvent(new Event('change'));
    q('mic').click(); await wait(recording, 'did not record');
    q('compose').requestSubmit(); enter({});
    check(q('send').disabled && sends.length === 0 && uploads.length === 0, 'recording sent a message');
    q('mic').click(); await wait(() => uploads.length === 1, 'no upload after stop');
    check(releases === 1 && uploads[0].options.body instanceof Blob &&
      uploads[0].options.body.type.includes(innerWidth < 900 ? 'mp4' : 'webm'), 'wrong recording body or mic left open');
    q('input').dispatchEvent(new CompositionEvent('compositionstart'));
    uploads[0].reply({text:'Termius 和 tmux'});
    await sleep(50);
    check(q('input').value === '原草稿' && sends.length === 0, 'IME was overwritten or text auto-sent');
    q('input').value += '，还在编辑'; q('input').dispatchEvent(new CompositionEvent('compositionend'));
    await wait(() => q('input').value.endsWith('Termius 和 tmux'), 'no transcript after IME finished');
    check(q('input').value === '原草稿，还在编辑\nTermius 和 tmux' && !q('attached').hidden, 'lost a draft or picture');
    q('mic').click(); await wait(recording, 'second recording failed');
    q('voice-cancel').click(); await sleep(30);
    check(releases === 2 && uploads.length === 1 && !q('send').disabled, 'cancel uploaded or leaked mic');
    q('mic').click(); await wait(recording, 'third recording failed');
    q('mic').click(); await wait(() => uploads.length === 2, 'missing second upload');
    uploads[1].reply({error:'测试转写失败'}, 503);
    await wait(() => status().includes('测试转写失败'), 'failure not displayed');
    check(!q('attached').hidden && q('input').value.endsWith('Termius 和 tmux'), 'failure lost draft or image');
    q('mic').click(); await wait(recording, 'fourth recording failed');
    q('mic').click(); await wait(() => uploads.length === 3, 'missing third upload');
    open('B'); q('input').value = 'B 的草稿'; open('A');
    uploads[2].reply({text:'这是迟到的录音'});
    await sleep(60);
    check(uploads[2].options.signal.aborted && q('input').value.endsWith('Termius 和 tmux') && !q('input').value.includes('迟到'), 'stale response modified revisited draft');
    check(sends.length === 0, 'unexpected send');
    q('send').click(); await wait(() => sends.length === 1 && !q('input').value && !q('send').disabled, 'manual send failed');
    check(sends[0].name === 'A' && sends[0].text.endsWith('Termius 和 tmux'), 'incorrect manual send');
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
