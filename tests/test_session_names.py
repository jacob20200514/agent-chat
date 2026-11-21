import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server


class SessionNames(unittest.TestCase):
    def test_blank_nickname_uses_lowercase_agent_name_and_three_letters(self):
        for agent, prefix in [('codex', 'codex'), ('claude', 'claude'), ('codebuddy', 'codebuddy'),
                              ('pi', 'pi'), ('shell', 'zsh')]:
            with self.subTest(agent=agent), patch.object(server, 'tmux') as tmux, \
                    patch.object(server, 'remember') as remember, patch.object(server.threading, 'Thread'):
                name = server.create_session(agent, '  ')
                self.assertRegex(name, rf'^{prefix}-[a-z]{{3}}$')
                self.assertEqual(tmux.call_args.args[:4], ('new-session', '-d', '-s', name))
                self.assertEqual(remember.call_args.kwargs['nickname'], name)

    def test_generated_name_retries_a_tmux_collision(self):
        with patch.object(server.secrets, 'choice', side_effect='abcdef'), \
                patch.object(server, 'tmux', side_effect=[RuntimeError('duplicate session: zsh-abc'), '']) as tmux, \
                patch.object(server, 'remember') as remember:
            self.assertEqual(server.create_session('shell', None), 'zsh-def')
        self.assertEqual([call.args[3] for call in tmux.call_args_list], ['zsh-abc', 'zsh-def'])
        remember.assert_called_once_with('zsh-def', nickname='zsh-def')

    def test_supplied_nickname_is_preserved_and_duplicate_is_rejected(self):
        with patch.object(server, 'tmux', side_effect=['', RuntimeError('duplicate session: release-plan')]) as tmux, \
                patch.object(server, 'remember') as remember, patch.object(server.secrets, 'choice') as choice:
            self.assertEqual(server.create_session('shell', 'release plan'), 'release-plan')
            with self.assertRaisesRegex(RuntimeError, '已有同名 tmux 会话'):
                server.create_session('shell', 'release plan')
        self.assertEqual([call.args[3] for call in tmux.call_args_list], ['release-plan', 'release-plan'])
        remember.assert_called_once_with('release-plan', nickname='release plan')
        choice.assert_not_called()
