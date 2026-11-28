"""Local transcription uses a separate, bounded request and never sends to a pane."""
import concurrent.futures
from contextlib import closing
import http.client
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice_transcription as voice

FFMPEG = shutil.which('ffmpeg')


class VoiceAPI(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {'AGENT_CHAT_HOME': self.home}))
        self.server = importlib.reload(importlib.import_module('server'))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.server.Handler)
        self.httpd.daemon_threads = True
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.02), daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.cookie = 'agent_chat=' + self.server.new_login(False)
        self.config = Path(self.home) / 'transcription.json'
        self.enterContext(patch.object(self.server, 'tmux', side_effect=AssertionError('voice must not touch tmux')))

    def request(self, data=b'', *, path='/api/transcribe', method='POST', logged_in=True, **overrides):
        with self.connect() as connection:
            self.start_request(connection, path, method, data, logged_in, overrides)
            return self.response(connection)

    def connect(self):
        return closing(http.client.HTTPConnection(*self.httpd.server_address, timeout=7))

    def start_request(self, connection, path, method, data, logged_in, overrides):
        headers = {'X-Agent-Chat': '1', 'Content-Length': str(len(data))}
        if logged_in:
            headers['Cookie'] = self.cookie
        connection.request(method, path, body=data, headers=headers | overrides)

    def response(self, connection):
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def configure(self, script):
        self.config.write_text(json.dumps({'command': [sys.executable, '-c', script], 'ffmpeg': FFMPEG or sys.executable}))

    def audio(self, fmt, seconds=1):
        if not FFMPEG:
            self.skipTest('ffmpeg required for decoder integration')
        opts = ['-c:a', 'aac', '-movflags', '+frag_keyframe+empty_moov'] if fmt == 'mp4' else ['-c:a', 'libopus']
        return subprocess.check_output([FFMPEG, '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=400',
                                        '-t', str(seconds), *opts, '-f', fmt, '-'])

    def test_auth_origin_length_and_disabled_reject_without_reading(self):
        self.assertEqual(self.request(logged_in=False, **{'Content-Length': '10'})[0], 401)
        self.assertEqual(self.request(Origin='https://cross-site.invalid', **{'Content-Length': '10'})[0], 403)
        self.assertEqual(self.request(**{'X-Agent-Chat': '0', 'Content-Length': '10'})[0], 403)
        self.assertEqual(self.request(**{'Content-Length': '10'})[0], 503)
        self.configure('print(1)')
        for length in ('-1', 'NaN', '0'):
            self.assertEqual(self.request(**{'Content-Length': length})[0], 400)
        self.assertEqual(self.request(**{'Content-Length': str(voice.MAX_BYTES + 1)})[0], 413)
        self.assertEqual(self.request(**{'Content-Length': '10', 'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertEqual(self.request(b'not audio')[0], 415)
        self.assertEqual(self.request(b'\x00\x00\x00\x18ftypM4A ', **{'Content-Type': 'image/png'})[0], 422)

    def test_mp4_and_webm_return_text_without_persisting_or_sending_anything(self):
        for fmt in ('mp4', 'webm'):
            self.configure('from pathlib import Path; import sys,wave; p=Path(sys.argv[1]); '
                           'assert p.parent.stat().st_mode & 0o777 == 0o700; '
                           'assert wave.open(str(p)).getframerate()==16000; print("Termius 和 tmux")')
            self.assertEqual(self.request(self.audio(fmt), **{'Content-Type': 'application/octet-stream'}),
                             (200, {'text': 'Termius 和 tmux'}))
        self.assertEqual(sorted(p.name for p in Path(self.home).iterdir()), ['auth.json', 'transcription.json'])

    def test_decoded_duration_and_silence(self):
        self.configure('print(1)')
        audio = self.audio('webm', 2)
        with patch.object(voice, 'MAX_SECONDS', 1):
            self.assertEqual(self.request(audio)[1]['code'], 'VOICE_DURATION')
        self.configure('pass')
        self.assertEqual(self.request(audio)[1]['code'], 'VOICE_EMPTY')

    def test_timeout_cleans_private_files_and_releases_slot(self):
        audio = self.audio('mp4')
        self.configure('import subprocess,sys; subprocess.run([sys.executable,"-c","import time; time.sleep(30)"])')
        with tempfile.TemporaryDirectory() as scratch, patch.object(tempfile, 'tempdir', scratch), patch.object(voice, 'TRANSCRIBE_TIMEOUT', 1):
            self.assertEqual(self.request(audio)[1]['code'], 'VOICE_TIMEOUT')
            self.assertEqual(list(Path(scratch).iterdir()), [])
        self.configure('import sys; print("sensitive path", file=sys.stderr); sys.exit(1)')
        self.assertEqual(self.request(audio), (503, {'code': 'VOICE_ENGINE', 'error': '本地转写暂不可用，请稍后重试或直接打字。'}))

    def test_busy_upload_does_not_block_other_requests(self):
        self.configure('print(1)')
        start = threading.Event()
        with patch.object(voice, 'UPLOAD_TIMEOUT', 1), concurrent.futures.ThreadPoolExecutor() as pool:
            def stalled():
                with self.connect() as connection:
                    self.start_request(connection, '/api/transcribe', 'POST', b'', True, {'Content-Length': '10'})
                    start.set()
                    return self.response(connection)
            stalled_request = pool.submit(stalled)
            self.assertTrue(start.wait(1))
            for _ in range(100):
                if self.server.voice_lock.locked():
                    break
                time.sleep(0.005)
            self.assertEqual(self.request(**{'Content-Length': '10'})[0], 429)
            self.assertTrue(self.request(path='/api/me', method='GET')[1]['login'])
            self.assertEqual(stalled_request.result(timeout=4)[0], 408)
        self.assertFalse(self.server.voice_lock.locked())


if __name__ == '__main__':
    unittest.main()
