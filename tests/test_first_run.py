"""First-run password setup over HTTP, against a throwaway data directory."""
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FirstRun(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ['AGENT_CHAT_HOME'] = self.home.name
        self.server_module = importlib.reload(importlib.import_module('server'))
        self.assertEqual(str(self.server_module.DATA), self.home.name)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.server_module.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home.cleanup()
        del os.environ['AGENT_CHAT_HOME']

    def call(self, path, body=None, cookie=None):
        headers = {'X-Agent-Chat': '1', 'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = cookie
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read()), response.headers.get('Set-Cookie')
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()), None

    def test_page_sets_the_first_password_once(self):
        self.assertEqual(self.call('/api/me')[1], {'login': False, 'password_set': False})

        status, body, _ = self.call('/api/setup', {'password': 'short'})
        self.assertEqual(status, 400)
        self.assertFalse(self.call('/api/me')[1]['password_set'])

        status, _, cookie = self.call('/api/setup', {'password': 'first-password', 'remember': True})
        self.assertEqual(status, 200)
        cookie = cookie.split(';')[0]
        self.assertEqual(self.call('/api/me', cookie=cookie)[1], {'login': True, 'password_set': True})
        stored = json.loads((Path(self.home.name) / 'auth.json').read_text())
        self.assertNotIn('first-password', json.dumps(stored))
        self.assertEqual(oct((Path(self.home.name) / 'auth.json').stat().st_mode & 0o777), '0o600')

        # The door closes once a password exists: a second setup must not replace it.
        status, _, _ = self.call('/api/setup', {'password': 'other-password'})
        self.assertEqual(status, 409)
        self.assertEqual(self.call('/api/login', {'password': 'other-password'})[0], 401)
        self.assertEqual(self.call('/api/login', {'password': 'first-password'})[0], 200)

    def test_setup_needs_the_same_origin_header(self):
        request = urllib.request.Request(self.base + '/api/setup', data=b'{"password": "first-password"}')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(request, timeout=20)
        self.assertEqual(caught.exception.code, 403)
        self.assertFalse(self.call('/api/me')[1]['password_set'])


if __name__ == '__main__':
    unittest.main()
