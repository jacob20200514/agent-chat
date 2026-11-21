"""Codex headers follow the current TUI footer, including switches made in the terminal."""
import importlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CATALOG = [
    {'model': 'gpt-6.2-sol', 'displayName': 'GPT-6.2-Sol',
     'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'ultra'}]},
    {'model': 'gpt-6-luna', 'displayName': 'GPT-6-Luna',
     'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]},
]


def pane(model='GPT-6.1-Sol', effort='xhigh'):
    return 'Assistant reply\n\n› Ask Codex to do anything\n\n  ' + model + ' ' + effort + ' · ~ · Current task\n  ← for agents · ? for shortcuts\n'


class CodexLiveStatus(unittest.TestCase):
    def setUp(self):
        self.server = importlib.import_module('server')
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.data = Path(self.home.name)
        self.raw = pane()
        self.saved = dict(name='codex-test', nickname='Original title', agent='codex',
                          agent_label='Codex', state='unknown', dead=False,
                          model='gpt-6-luna', effort='low')
        for mock in (
            patch.object(self.server, 'DATA', self.data),
            patch.object(self.server, 'list_sessions', side_effect=lambda *_: [dict(self.saved)]),
            patch.object(self.server, 'capture', side_effect=lambda name: self.raw),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        self.server.save('sessions.json', {'codex-test': dict(self.saved)})
        self.original_meta = (self.data / 'sessions.json').read_bytes()
        token = self.server.new_login(False)
        self.cookie = self.server.COOKIE + '=' + token
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self.server.Handler)
        self.addCleanup(self.httpd.server_close)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_port

    def get(self, path):
        return self.request(path)

    def request(self, path, body=None):
        request = urllib.request.Request(self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Cookie': self.cookie, 'X-Agent-Chat': '1', 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            return json.load(response)

    def test_list_and_output_use_live_footer_without_changing_saved_metadata(self):
        listed = self.get('/api/sessions')['sessions'][0]
        output = self.get('/api/sessions/codex-test/output')['session']
        for session in (listed, output):
            self.assertEqual((session['model'], session['effort']), ('gpt-6.1-sol', 'xhigh'))
            self.assertEqual(session['nickname'], 'Original title')
        self.assertEqual((self.data / 'sessions.json').read_bytes(), self.original_meta)

    def test_usage_reads_reset_count_and_omits_private_account_details(self):
        with patch('model_catalogs.codex_server') as rpc:
            send, result = Mock(), Mock()
            rpc.return_value.__enter__.return_value = (send, result)
            for credits, expected in [({'availableCount': 3, 'credits': []}, 3), ({'availableCount': 0}, 0), (None, None)]:
                with self.subTest(expected=expected):
                    send.reset_mock()
                    result.side_effect = [
                        {'account': {'ignored': 'private', 'type': 'chatgpt', 'email': 'alice@example.com'}},
                        {'rateLimitResetCredits': credits, 'rateLimits': {
                            'primary': {'usedPercent': 25, 'windowDurationMins': 10080, 'resetsAt': 1795015559},
                            'credits': {'hasCredits': True},
                        }},
                    ]
                    self.assertEqual(self.get('/api/usage/codex'), {
                        'windows': [{'minutes': 10080, 'used': 25, 'reset_at': 1795015559}],
                        'reset_count': expected, 'account_label': 'al***@example.com',
                    })
                    self.assertEqual([call.args[0]['method'] for call in send.call_args_list],
                                     ['account/read', 'account/rateLimits/read'])

    def test_send_keeps_the_message_unsent_until_the_usage_screen_is_closed(self):
        menu = 'Usage\nAccount usage and resets.\n› 1. View analytics\n2. Redeem reset\nenter open · esc back\n'
        loading = 'Usage\nLoading…\nesc back\n'
        report = 'Usage · Plus\nToken activity\npgup/pgdn scroll · esc back\ntab report · z all · ? · R refresh\n\n'
        path = '/api/sessions/codex-test/send'
        payload = {'id': 'draft-1', 'text': '继续上一项'}
        with patch.object(self.server, 'seen_messages', {}), \
                patch.object(self.server, 'screen') as screen, \
                patch.object(self.server, 'send_text') as send:
            for raw in (menu, loading, report):
                screen.return_value = raw
                with self.assertRaises(HTTPError) as failure:
                    self.request(path, payload)
                with failure.exception as response:
                    self.assertEqual(response.code, 409)
                    self.assertEqual(json.load(response)['code'], 'CODEX_USAGE_OPEN')
            send.assert_not_called()

            screen.return_value = report + '\n› Ask Codex to do anything\n'
            self.assertEqual(self.request(path, payload), {})
            send.assert_called_once_with('codex-test', payload['text'])
            screen.return_value = report
            self.assertEqual(self.request(path, payload), {'duplicate': True})
            send.assert_called_once()

    def test_terminal_switch_refreshes_header_even_when_chat_text_is_unchanged(self):
        first = self.get('/api/sessions/codex-test/output')
        self.raw = pane('GPT-6-Astra', 'high')
        second = self.get('/api/sessions/codex-test/output?since=' + first['version'])
        self.assertEqual(second['version'], first['version'])
        self.assertNotIn('text', second)
        self.assertEqual((second['session']['model'], second['session']['effort']), ('gpt-6-astra', 'high'))

    def test_hidden_footer_does_not_report_a_saved_model_as_current(self):
        self.raw = 'Select Model\n❯ 1. GPT-6-Astra\n  Enter to select · Esc to cancel\n'
        session = self.get('/api/sessions/codex-test/output')['session']
        self.assertIsNone(session['model'])
        self.assertIsNone(session['effort'])

    def test_history_is_not_mistaken_for_current_status(self):
        self.raw = pane() + '\nSelect Model\n❯ 1. GPT-6-Astra\n  Enter to select · Esc to cancel\n'
        session = self.get('/api/sessions/codex-test/output')['session']
        self.assertIsNone(session['model'])
        self.assertIsNone(session['effort'])

    def test_footer_display_label_maps_to_the_model_id(self):
        self.raw = pane('GPT-6.1 Terra', 'max')
        session = self.get('/api/sessions/codex-test/output')['session']
        self.assertEqual((session['model'], session['effort']), ('gpt-6.1-terra', 'max'))

    def test_other_agents_do_not_read_a_codex_footer(self):
        self.saved.update(agent='claude', agent_label='Claude', model='opus', effort='high')
        session = self.get('/api/sessions/codex-test/output')['session']
        self.assertEqual((session['model'], session['effort']), (None, None))

    def test_picker_refreshes_and_uses_current_models_efforts(self):
        self.raw = pane('GPT-6-Luna', 'low')
        path = '/api/sessions/codex-test/options'
        with patch.object(self.server, 'screen', side_effect=lambda name: self.raw), \
                patch('model_catalogs.codex_models', side_effect=[CATALOG[:1], CATALOG]):
            first = self.get(path)
            second = self.get(path)
        self.assertEqual([model['id'] for model in first['models']], ['gpt-6.2-sol'])
        self.assertEqual([model['id'] for model in second['models']], ['gpt-6.2-sol', 'gpt-6-luna'])
        self.assertEqual([effort['id'] for effort in second['efforts']], ['low'])
        self.assertEqual((second['current']['model'], second['current']['effort']), ('gpt-6-luna', 'low'))

    def test_dynamic_model_applies_only_to_the_current_session(self):
        self.raw = pane('GPT-6-Luna', 'low')
        session = dict(self.saved)
        with patch('model_catalogs.codex_models', return_value=CATALOG), \
                patch.object(self.server, 'screen', return_value=self.raw), \
                patch.object(self.server, 'slash') as slash, \
                patch.object(self.server, 'wait_for'), \
                patch.object(self.server, 'walk_to') as walk, \
                patch.object(self.server, 'send_keys') as keys:
            self.server.set_model(session, 'gpt-6.2-sol')
        slash.assert_called_once_with('codex-test', '/model')
        self.assertEqual(walk.call_args_list[0].args, ('codex-test', 'GPT-6.2-Sol'))
        self.assertEqual(walk.call_args_list[1].args, ('codex-test', 'Low'))
        self.assertEqual([call.args for call in keys.call_args_list],
                         [('codex-test', 'Enter'), ('codex-test', 's')])

    def test_model_row_does_not_match_a_similarly_named_variant(self):
        rows = ['GPT-6.2-Sol-Preview', 'GPT-6.2-Sol (current)   ']
        with patch.object(self.server, 'cursor_line', side_effect=rows), \
                patch.object(self.server, 'send_keys') as keys:
            self.server.walk_to('codex-test', 'GPT-6.2-Sol', pattern=r'GPT-6\.2-Sol(?: \(current\))?$')
        keys.assert_called_once_with('codex-test', 'Up', pause=0.06)

    def test_claude_list_and_output_read_live_status(self):
        self.saved.update(agent='claude', agent_label='Claude')
        self.raw = 'Claude Code v2.1.322\n ▐▛███▛█ Opus 5.5 with high effort\n\n❯\n'
        for session in (self.get('/api/sessions')['sessions'][0], self.get('/api/sessions/codex-test/output')['session']):
            self.assertEqual((session['model'], session['effort']), ('opus', 'high'))

    def test_codebuddy_list_and_output_read_live_status(self):
        self.saved.update(agent='codebuddy', agent_label='CodeBuddy')
        self.raw = '>\n hy4std low · ← for agents · ? for shortcuts\n'
        for session in (self.get('/api/sessions')['sessions'][0], self.get('/api/sessions/codex-test/output')['session']):
            self.assertEqual((session['model'], session['effort']), ('hy4std', 'low'))


if __name__ == '__main__':
    unittest.main()
