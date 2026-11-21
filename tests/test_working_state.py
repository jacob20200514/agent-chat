"""Working/idle detection from current CLI status, including narrow tmux panes."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server


def composer(agent, width=46, draft=''):
    rule = '─' * width
    prompt = '❯\u00a0' if agent == 'claude' else '> '
    return f'{rule}\n{prompt}{draft}\n{rule}\n  ⏵⏵ bypass permissions on\n'


CLAUDE_RUNNING = '''  ⎿ \u00a0Running… (1m 34s · timeout 8m)

✶ Working… (1m 34s)
  ⎿ \u00a0Use /btw to ask a quick side
     question without interrupting Claude's
     current work.

''' + composer('claude')
CODEBUDDY_RUNNING = '''● REPL
      ⠦ TaskOutput  {timeout, task_id, …  2m

 (164s · running REPL ·   12 tokens · Task ru…

''' + composer('codebuddy')
CODEBUDDY_STREAMING = ' (68s · streaming · ↓ 2.6k tokens · Task runn…\n\n' + composer('codebuddy')
CODEBUDDY_WRITING = ' (70s · writing REPL · ↓ 2.9k tokens · Task r…\n\n' + composer('codebuddy')


# Real 46-column captures (2026-11-21). Claude clips "esc to interrupt" in its footer.
CLAUDE_NARROW = '''  ⎿ \u00a0Running…

✽ Polishing… (26s · ↓ 2.1k tokens)

──────────────────────────────────────────────
❯\u00a0
──────────────────────────────────────────────
  ⏵⏵ bypass permissions on · esc to interru…
'''
CODEBUDDY_TIP = '''      ⠋ Bash  sleep 80  18.0s

✹ Monitori… (20s · running REPL ·   14 tokens)
  └ Tip: Start with small features or bug
    fixes, tell CodeBuddy to propose a plan,
    and verify its suggested edits

──────────────────────────────────────────────
>  
──────────────────────────────────────────────
dee…  ⏵⏵ bypass permissions on (⇧tab to cycle)
'''
CODEBUDDY_DONE = '''● OK

✔ Worked for 1m 23s

──────────────────────────────────────────────
> Run sleep 80 shell command           ↵ send
──────────────────────────────────────────────
dee…  ⏵⏵ bypass permissions on (⇧tab to cycle)
'''


def codex_screen(status):
    return f'• 我会执行 sleep 80。\n \n{status}\n \n \n› Ask Codex to do anything\n \n  GPT-6.2-Sol xhigh · ~\n'


class WorkingState(unittest.TestCase):
    def test_claude_running_tool_and_wrapped_hint(self):
        self.assertTrue(server.busy(CLAUDE_RUNNING, 'claude'))
        for status in (
            '  ⎿ \u00a0Running… (1m 34s · timeout 8m)',
            '✶ Working…\n  ⎿ Tip: Ask a quick question without interrupting Claude\'s work.',
        ):
            with self.subTest(status=status):
                self.assertTrue(server.busy(status + '\n\n' + composer('claude', 100, 'next question'), 'claude'))

    def test_codebuddy_running_timer_with_truncated_hint(self):
        self.assertTrue(server.busy(CODEBUDDY_RUNNING, 'codebuddy'))
        self.assertFalse(server.busy(CODEBUDDY_RUNNING, 'claude'))
        # CodeBuddy also draws Claude's spinner-verb row before its timer (CODEBUDDY_TIP), so a Claude
        # spinner row reads as busy for both.

    def test_codebuddy_turn_timer_does_not_depend_on_the_phase(self):
        for capture in (
            CODEBUDDY_STREAMING,
            CODEBUDDY_WRITING,
            ' (0s)\n\n' + composer('codebuddy', 100),
            ' (8s · editing notebook)\n\n' + composer('codebuddy', 100),
        ):
            with self.subTest(status=capture.splitlines()[0]):
                self.assertTrue(server.busy(capture, 'codebuddy'))

    def test_completed_turns_and_status_like_output_are_idle(self):
        for agent, busy_capture in [('claude', CLAUDE_RUNNING), ('codebuddy', CODEBUDDY_RUNNING)]:
            with self.subTest(agent=agent):
                done = '✻ Cooked for 18m 8s · done 5:35 PM' if agent == 'claude' else '● Finished.'
                self.assertFalse(server.busy(busy_capture + '\n' + done + '\n\n' + composer(agent), agent))
                self.assertFalse(server.busy('The docs say Running… or (164s · running REPL)\n\n' + composer(agent), agent))
                self.assertFalse(server.busy(composer(agent, draft='(164s · running REPL · 12 tokens)'), agent))
        self.assertFalse(server.busy('  ⎿ Running… (2s)\n❯ 1. Yes\nEnter to select · Esc to cancel\n', 'claude'))
        self.assertFalse(server.busy(' (68s · streaming · ↓ 2.6k tokens · Task runn…\n', 'codebuddy'))
        self.assertFalse(server.busy('(68s · streaming)\n\n' + composer('codebuddy'), 'codebuddy'))
        self.assertFalse(server.busy(CODEBUDDY_WRITING + '\n● Finished.\n\n' + composer('codebuddy'), 'codebuddy'))

    def test_keeps_legacy_interrupt_detection(self):
        for agent in ('claude', 'codebuddy', 'codex'):
            with self.subTest(agent=agent):
                self.assertTrue(server.busy('Working (42s · esc)\n', agent))
                self.assertTrue(server.busy('Working · esc to interrupt\n', agent))
                self.assertFalse(server.busy('Ready\n', agent))

    def test_clipped_hints_and_tip_blocks_in_narrow_panes(self):
        self.assertTrue(server.busy(CLAUDE_NARROW, 'claude'))
        self.assertTrue(server.busy(CODEBUDDY_TIP, 'codebuddy'))
        self.assertTrue(server.busy(CODEBUDDY_TIP.replace('✹ Monitori… (20s', ' (64s'), 'codebuddy'))
        self.assertFalse(server.busy(CODEBUDDY_DONE, 'codebuddy'))
        for status in (
            '✽ Fixing the busy detection… (2m 3s · ↓ 9.1k tokens · esc…',
            '✳ Polishing…',
            '· Thinking… (1h 2m 5s)',
        ):
            with self.subTest(status=status):
                self.assertTrue(server.busy(status + '\n  ⎿  ☐ Fix narrow panes\n     ☐ Add tests\n\n' + composer('claude'), 'claude'))
        for status in (
            '✻ Cooked for 18m 8s · done 5:35 P…',
            '✻ Worked for 26s',
            '⏺ Polishing… (26s)',
        ):
            with self.subTest(status=status):
                self.assertFalse(server.busy(status + '\n\n' + composer('claude'), 'claude'))

    def test_codex_status_row_clipped_before_the_interrupt_hint(self):
        for status in (
            '• Working (1m 05s • esc to interrupt) · 1 bac…',
            '• Inspecting the busy detection rules (1m 05s •…',
            '• Inspecting the busy detection rules (42s…',
        ):
            with self.subTest(status=status):
                self.assertTrue(server.busy(codex_screen(status), 'codex'))
        for status in ('  Worked for 2m 6s • 20:27', '• Ran sleep 80', '• Done in (12s).'):
            with self.subTest(status=status):
                self.assertFalse(server.busy(codex_screen(status), 'codex'))

    def test_native_turn_state_overrides_the_screen(self):
        for native, screen, expected in [(True, '● Done.\n\n' + composer('claude'), 'busy'),
                                         (False, CLAUDE_NARROW, 'idle'),
                                         (None, CLAUDE_NARROW, 'busy'),
                                         (None, '● Done.\n\n' + composer('claude'), 'idle')]:
            with self.subTest(native=native, screen=screen.splitlines()[0]):
                session = dict(name='s', agent='claude', state='unknown', _agent_process=(1, 2))
                with (patch.object(server.reply_status, 'native_status', return_value=(None, native)) as native_status,
                      patch.object(server, 'update_agent_status')):
                    server.session_snapshot(session, screen)
                native_status.assert_called_once_with('claude', (1, 2), Path.home())
                self.assertEqual(session['state'], expected)
        exited = dict(name='s', agent='claude', state='exited')
        with patch.object(server.reply_status, 'native_status') as native_status:
            server.session_snapshot(exited, '% ')
        native_status.assert_not_called()
        self.assertEqual(exited['state'], 'exited')

    def test_home_and_chat_report_busy_then_idle(self):
        captures = {'claude': CLAUDE_RUNNING, 'codebuddy': CODEBUDDY_RUNNING}

        def rows(only=None):  # the full list; find_session picks by name
            return [dict(name=agent, agent=agent, state='unknown') for agent in captures] + [
                dict(name='shell', agent=None, state='exited')]

        with (
            tempfile.TemporaryDirectory() as data,
            patch.object(server, 'DATA', Path(data)),
            patch.object(server, 'list_sessions', side_effect=rows),
            patch.object(server, 'capture', side_effect=lambda name: captures.get(name, CLAUDE_RUNNING)),
            ThreadingHTTPServer(('127.0.0.1', 0), server.Handler) as httpd,
        ):
            cookie = server.COOKIE + '=' + server.new_login(False)
            worker = threading.Thread(target=httpd.serve_forever)
            worker.start()

            def get(path):
                req = Request(f'http://127.0.0.1:{httpd.server_port}/api/sessions' + path,
                              headers={'Cookie': cookie})
                with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
                    return json.load(response)

            try:
                self.assertEqual({s['name']: s['state'] for s in get('')['sessions']},
                                 {'claude': 'busy', 'codebuddy': 'busy', 'shell': 'exited'})
                for capture in (CODEBUDDY_STREAMING, CODEBUDDY_WRITING, CODEBUDDY_RUNNING):
                    captures['codebuddy'] = capture
                    listed = {s['name']: s['state'] for s in get('')['sessions']}
                    self.assertEqual(listed['codebuddy'], 'busy')
                    self.assertEqual(get('/codebuddy/output')['session']['state'], 'busy')
                for agent in captures:
                    self.assertEqual(get(f'/{agent}/output')['session']['state'], 'busy')
                    captures[agent] = '● Done.\n\n' + composer(agent)
                    self.assertEqual(get(f'/{agent}/output')['session']['state'], 'idle')
                self.assertEqual({s['name']: s['state'] for s in get('')['sessions']},
                                 {'claude': 'idle', 'codebuddy': 'idle', 'shell': 'exited'})
            finally:
                httpd.shutdown()
                worker.join()


if __name__ == '__main__':
    unittest.main()
