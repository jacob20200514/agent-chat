"""Pictures sent from the page: stored under DATA/uploads, path returned, old day folders removed."""
import http.client
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class Upload(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ['AGENT_CHAT_HOME'] = self.home.name
        self.server = importlib.reload(importlib.import_module('server'))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        status, _, headers = self.post('/api/setup', json.dumps({'password': 'first-password'}).encode())
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.uploads = Path(self.home.name) / 'uploads'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home.cleanup()
        del os.environ['AGENT_CHAT_HOME']

    def post(self, path, data, cookie=None, length=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_address[1], timeout=20)
        headers = {'X-Agent-Chat': '1', 'Content-Length': str(len(data) if length is None else length)}
        if cookie:
            headers['Cookie'] = cookie
        connection.request('POST', path, body=data, headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read() or b'{}')
        connection.close()
        return response.status, body, response.headers

    def test_picture_is_saved_privately_and_its_path_returned(self):
        status, body, _ = self.post('/api/upload', PNG, self.cookie)
        self.assertEqual(status, 200)
        path = Path(body['path'])
        self.assertEqual(path.parent, self.uploads / time.strftime('%Y%m%d'))
        self.assertRegex(path.name, r'^\d{6}-[0-9a-f]{6}\.png$')
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(self.uploads.stat().st_mode & 0o777), '0o700')
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), '0o700')

    def test_formats_are_told_by_content(self):
        self.assertEqual(self.server.image_type(b'\xff\xd8\xff\xe0rest'), 'jpg')
        self.assertEqual(self.server.image_type(b'GIF89a...'), 'gif')
        self.assertEqual(self.server.image_type(b'RIFF\x10\x00\x00\x00WEBPVP8 '), 'webp')
        self.assertIsNone(self.server.image_type(b'\x00\x00\x00\x18ftypheic'))
        status, body, _ = self.post('/api/upload', b'#!/bin/sh\necho hi\n', self.cookie)
        self.assertEqual(status, 400)
        self.assertIn('PNG', body['error'])
        self.assertFalse(any(self.uploads.rglob('*')) if self.uploads.exists() else False)

    def test_refused_before_the_body_is_read(self):
        # Only headers go out: the server must answer without waiting for the body.
        self.assertEqual(self.post('/api/upload', b'', length=10)[0], 401)
        status, body, _ = self.post('/api/upload', b'', self.cookie, length=self.server.UPLOAD_LIMIT + 1)
        self.assertEqual(status, 413)
        self.assertIn('20 MB', body['error'])
        self.assertFalse(self.uploads.exists())

    def test_day_folders_past_the_keep_period_go_with_the_next_picture(self):
        old = time.strftime('%Y%m%d', time.localtime(time.time() - (self.server.UPLOAD_DAYS + 1) * 86400))
        kept = time.strftime('%Y%m%d', time.localtime(time.time() - (self.server.UPLOAD_DAYS - 1) * 86400))
        for name in (old, kept, 'notes'):
            (self.uploads / name).mkdir(parents=True)
            (self.uploads / name / 'a.png').write_bytes(PNG)
        self.assertEqual(self.post('/api/upload', PNG, self.cookie)[0], 200)
        self.assertFalse((self.uploads / old).exists())
        self.assertTrue((self.uploads / kept / 'a.png').exists())
        self.assertTrue((self.uploads / 'notes' / 'a.png').exists())


if __name__ == '__main__':
    unittest.main()
