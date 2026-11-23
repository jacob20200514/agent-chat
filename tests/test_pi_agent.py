"""Pi 1.5.2 renders a plain editor between horizontal rules in tmux."""
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server


BORDER = '─' * 46
FOOTER = '0.0%/272k (auto)                gpt-5.5 • high'


def pane(editor='', below_editor=''):
    return f'Previous output\n{BORDER}\n{editor}\n{BORDER}\n{below_editor}~\n{FOOTER}\n\n\n'


class PiAgentTest(unittest.TestCase):
    def test_session_launch_leaves_room_for_the_full_footer(self):
        with patch.object(server, 'tmux') as tmux, patch.object(server, 'list_sessions', return_value=[]), \
                patch.object(server, 'remember'), patch.object(server.threading, 'Thread') as thread:
            name = server.create_session('pi', 'try pi')
        self.assertEqual(name, 'try-pi')
        args = tmux.call_args.args
        self.assertEqual(args[args.index('-x') + 1], '100')
        self.assertEqual(args[-3:-1], ('zsh', '-ic'))
        self.assertEqual(shlex.split(args[-1]), ['pi', '--tui-mode', 'regular', '--extension',
                                                 str(server.ROOT / 'pi-session-status.ts')])
        thread.assert_not_called()

    def test_busy_spinner_is_read_only_from_the_editor(self):
        spinner = '── ⠹ Working ' + '─' * 34
        active = pane().replace(BORDER, spinner, 1) + '\n' * 30
        self.assertTrue(server.busy(active, 'pi'))
        self.assertFalse(server.busy(active, 'codex'))
        self.assertFalse(server.busy(active + pane(), 'pi'))

    def test_preview_and_reply_come_from_the_bound_transcript(self):
        def message(entry, parent, role, text, reason=None):
            return dict(type='message', id=entry, parentId=parent,
                        message={'role': role, 'stopReason': reason, 'content': [{'type': 'text', 'text': text}]})

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pi.jsonl'
            def snapshot(*entries, pending=''):
                path.write_text(''.join(json.dumps(e) + '\n' for e in ({'type': 'session', 'id': 's'}, *entries)), encoding='utf-8')
                with patch.object(server, 'tmux', return_value=f'\t{path}\t{pending}\n'):
                    session = {'name': 'test'}
                    server.update_pi_turn(session)
                return session

            user = message('1', None, 'user', '看一下\n 当前任务')
            tool_use = message('2', '1', 'assistant', '我先看看', 'toolUse')
            tool_result = message('3', '2', 'toolResult', '工具输出')
            first = snapshot(user, tool_use, tool_result)
            self.assertEqual((first['preview'], first['reply']['id']), ('看一下 当前任务', None))
            final = message('4', '3', 'assistant', '这是正式回复', 'stop')
            second = snapshot(user, tool_use, tool_result, final)
            self.assertTrue(second['reply']['id'])
            self.assertEqual(second['reply']['excerpt'], '这是正式回复')
            new_user = message('5', '4', 'user', '下一条请求')
            aborted = message('6', '5', 'assistant', '没说完', 'aborted')
            third = snapshot(user, tool_use, tool_result, final, new_user, aborted)
            self.assertEqual(third['preview'], '下一条请求')
            # The user asked again: the same reply, without the excerpt the session list showed.
            self.assertEqual(third['reply'], {k: v for k, v in second['reply'].items() if k != 'excerpt'})
            # The page marks these messages as the user's bubbles.
            with patch.object(server, 'tmux', return_value=f'\t{path}\t下一条请求\n'):
                self.assertEqual(server.pi_user_messages('test'), (('下一条请求', '看一下\n 当前任务'), '下一条请求'))
            path.unlink()
            with patch.object(server, 'tmux', return_value=f'\t{path}\t刚开始的任务\n'):
                session = {'name': 'test'}
                server.update_pi_turn(session)
                self.assertEqual(server.pi_user_messages('test'), ((), '刚开始的任务'))
            self.assertEqual((session['preview'], session['reply']['id']), ('刚开始的任务', None))

    def test_recognizes_pi_process_without_saved_session_metadata(self):
        for command in ('123 01:02 pi   \n', '123 00:00 node /opt/homebrew/bin/pi --tui-mode regular\n'):
            with self.subTest(command=command), patch.object(server.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, command, '')):
                self.assertEqual(server.detect_agent({'tty': '/dev/ttys000'}), 'pi')

    def test_footer_displays_current_model_and_reasoning(self):
        session = {'agent': 'pi', 'state': 'idle', 'model': 'stale', 'effort': 'stale'}
        server.update_agent_status(session, server.clean(pane()))
        self.assertEqual((session['model'], session['effort']), ('gpt-5.5', 'high'))
        off = '↑4 ↓8 1.1%/272k (auto)  (openai) gpt-5.5 • thinking off'
        server.update_agent_status(session, server.clean(pane().replace(FOOTER, off)))
        self.assertEqual((session['model'], session['effort']), ('gpt-5.5', 'off'))
        used = '↑29k ↓1.3k R22k CH66.6% $0.073 (sub) 3.8%/512k (auto)    (openai-codex) gpt-6.2-sol • xhigh'
        server.update_agent_status(session, server.clean(pane().replace(FOOTER, used)))
        self.assertEqual((session['model'], session['effort'], session['model_provider']), ('gpt-6.2-sol', 'xhigh', 'openai-codex'))
        server.update_agent_status(session, server.clean(pane().replace(FOOTER, '0.0%/0 (auto)   no-model')))
        self.assertEqual((session['model'], session['effort'], session['model_provider']), (None, None, None))

    def test_quota_provider_for_panes_with_old_and_new_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pi.jsonl'
            path.write_text('{"type":"session","id":"s"}\n'
                            '{"type":"model_change","id":"1","provider":"openai-codex","modelId":"demo"}\n')
            for live, model, visible, expected in [
                ('', 'demo', None, 'openai-codex'), ('', 'other', None, None),
                ('', 'demo', 'openai', 'openai'), ('openai', 'demo', None, 'openai'),
                ('openai-codex', None, None, 'openai-codex'), ('-', 'demo', 'openai-codex', None),
            ]:
                with self.subTest(live=live, model=model, visible=visible), \
                        patch.object(server, 'tmux', return_value=f'{live}\t{path}\t\n'):
                    row = {'name': 'test', 'model': model, 'model_provider': visible}
                    server.update_pi_turn(row)
                    self.assertEqual(row['model_provider'], expected)

    def test_pi_slash_uses_plain_editor_above_autocomplete(self):
        with patch.object(server, 'screen', side_effect=[pane(), pane('/model', '→ model  Select model\n')]), \
                patch.object(server, 'tmux') as tmux, patch.object(server.time, 'sleep'):
            server.slash('pi-session', '/model', agent='pi')
        self.assertEqual([call.args[-1] for call in tmux.call_args_list], ['/model', 'Enter'])

    def test_pi_slash_leaves_terminal_draft_and_native_menu_untouched(self):
        menu = pane('Thinking Level\n>\n→ ✓ high\nEnter to select · Ctrl+S to set as default ·\nEscape/Ctrl+C to cancel')
        for text in (pane('draft starts here\non a second line'), menu):
            with self.subTest(text=text), patch.object(server, 'screen', return_value=text), \
                    patch.object(server, 'tmux') as tmux:
                with self.assertRaises(RuntimeError):
                    server.slash('pi-session', '/new', agent='pi')
                tmux.assert_not_called()

    def test_picker_reads_native_rows_and_does_not_touch_a_changed_menu(self):
        hints = '  Enter to select · Ctrl+S to set as default · Escape/Ctrl+C to cancel'
        model = pane('Only showing models from configured providers.\n>\n→ ✓ demo (Demo Model) [openai]\n'
                     '    demo [openai-codex] · default\n  (2/12)\n' + hints)
        menu = server.pi_picker(model)
        self.assertEqual((menu['kind'], menu['cursor'], menu['position']), ('model', 0, '2/12'))
        self.assertEqual([(o['id'], o['current']) for o in menu['options']],
                         [('openai/demo', True), ('openai-codex/demo', False)])
        thinking = pane('Thinking Level\n>\n    medium   Moderate reasoning\n→ ✓ high     Deep reasoning\n' + hints)
        self.assertEqual(server.pi_picker(thinking)['options'][1]['id'], 'high')
        self.assertIsNone(server.pi_picker(model + pane()))
        with patch.object(server, 'screen', return_value=thinking), patch.object(server, 'send_keys') as keys:
            with self.assertRaises(RuntimeError):
                server.handle_pi_picker('pi-session', 'model', {'op': 'select', 'id': 'openai/demo'})
            keys.assert_not_called()

    def test_pi_login_menu_is_detected_even_with_trailing_blank_pane_rows(self):
        self.assertTrue(server.choice_menu('Select authentication method:\n'
                                          '↑↓ navigate enter select escape/ctrl+c cancel\n' + '\n' * 35))
