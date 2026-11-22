"""Wide mode: the page asks for a pane width; tmux resizes the window and stays free to follow a terminal."""
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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class WidthOverHttp(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ['AGENT_CHAT_HOME'] = self.home.name
        self.server = importlib.reload(importlib.import_module('server'))
        self.calls = []
        self.session = {'name': 'claude-abc', 'agent': 'claude', 'state': 'idle', 'dead': False, 'attached': 0}
        # Other test modules share this module object: patch, never assign.
        self.enterContext(patch.object(self.server, 'tmux', lambda *args, **kwargs: self.calls.append(args) or ''))
        self.enterContext(patch.object(self.server, 'find_session', lambda name: self.session))
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        self.cookie = None
        status, _, cookie = self.call('/api/setup', {'password': 'width-password'})
        self.assertEqual(status, 200)
        self.cookie = cookie.split(';')[0]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.home.cleanup()
        del os.environ['AGENT_CHAT_HOME']

    def call(self, path, body):
        headers = {'X-Agent-Chat': '1', 'Content-Type': 'application/json'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read()), response.headers.get('Set-Cookie')
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read()), None

    def width(self, columns):
        return self.call('/api/sessions/claude-abc/width', {'columns': columns})[:2]

    def test_resize_then_hand_the_size_back_to_tmux(self):
        self.assertEqual(self.width(96), (200, {'width': 96}))
        # One tmux command: the window is never left pinned, so Termius can still resize it.
        self.assertEqual(self.calls, [('resize-window', '-t', '=claude-abc:', '-x', '96', ';',
                                       'set-option', '-uw', '-t', '=claude-abc:', 'window-size')])
        self.assertNotIn('clear-history', self.calls[0])

    def test_width_stays_between_phone_width_and_the_cap(self):
        self.assertEqual(self.width(400)[1], {'width': 120})
        self.assertEqual(self.width(10)[1], {'width': 46})
        self.session['agent'] = 'pi'
        self.assertEqual(self.width(60)[1], {'width': 100})

    def test_rejects_anything_but_a_whole_number(self):
        for columns in ('96', 96.5, True, None):
            self.assertEqual(self.width(columns)[0], 400, columns)
        self.assertEqual(self.calls, [])

    def test_attached_terminal_owns_the_width(self):
        self.session['attached'] = 1
        status, body = self.width(96)
        self.assertEqual(status, 409)
        self.assertIn('终端', body['error'])
        self.assertEqual(self.calls, [])

    def test_only_for_a_running_agent(self):
        self.session.update(agent=None, state='exited')
        self.assertEqual(self.width(96)[0], 409)
        self.assertEqual(self.calls, [])


class WidthInSessionList(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ['AGENT_CHAT_HOME'] = self.home.name
        self.server = importlib.reload(importlib.import_module('server'))

    def tearDown(self):
        self.home.cleanup()
        del os.environ['AGENT_CHAT_HOME']

    def test_reports_pane_width_and_phone_width(self):
        panes = ''.join(f'{name}\t{created}\t0\t{command}\t/dev/ttys00{created}\t1\t1\t0\t10\t50000\t{width}\n'
                        for name, created, command, width in [('pi-x', 1, 'node', 100), ('claude-y', 2, 'claude', 120),
                                                              ('zsh-z', 3, 'zsh', 80)])
        self.enterContext(patch.object(self.server, 'tmux', lambda *args, **kwargs: panes))
        self.enterContext(patch.object(self.server, 'detect_agent', lambda row: {'node': 'pi', 'claude': 'claude'}.get(row['command'])))
        rows = {row['name']: (row['width'], row['default_width']) for row in self.server.list_sessions()}
        self.assertEqual(rows, {'pi-x': (100, 100), 'claude-y': (120, 46), 'zsh-z': (80, None)})


if __name__ == '__main__':
    unittest.main()
