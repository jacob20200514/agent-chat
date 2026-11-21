"""A message sent while Claude's start-up trust question is still up must not answer it with "No, exit"."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROWS = ['No, exit', 'Yes, I trust this folder']


class FakePane:
    def __init__(self):
        self.cursor, self.trusted, self.keys = 0, False, []

    def screen(self, name):
        if self.trusted:
            return '❯ \n  ⏵⏵ bypass permissions on\n'
        rows = ''.join((' ❯ ' if i == self.cursor else '   ') + row + '\n' for i, row in enumerate(ROWS))
        return ' Accessing workspace:\n\n' + rows + '\n Enter to confirm · Esc to cancel\n'

    def tmux(self, *args, **kwargs):
        if args[0] == 'send-keys':
            key = args[-1]
            self.keys.append(key)
            if key == 'Down':
                self.cursor = min(self.cursor + 1, len(ROWS) - 1)
            elif key == 'Up':
                self.cursor = max(self.cursor - 1, 0)
            elif key == 'Enter':
                if self.cursor == 0:
                    raise AssertionError('Enter picked "No, exit"')
                self.trusted = True
        return ''


class TrustPromptTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ['AGENT_CHAT_HOME'] = self.home.name
        self.server = importlib.reload(importlib.import_module('server'))
        self.pane = FakePane()
        self.server.screen = self.pane.screen
        self.server.tmux = self.pane.tmux

    def tearDown(self):
        del os.environ['AGENT_CHAT_HOME']
        self.home.cleanup()

    def test_send_answers_trust_before_typing(self):
        self.server.clear_trust_prompt('sess')
        self.assertTrue(self.pane.trusted)
        self.assertEqual(self.pane.keys[-1], 'Enter')

    def test_no_trust_question_sends_nothing(self):
        self.pane.trusted = True
        self.server.clear_trust_prompt('sess')
        self.assertEqual(self.pane.keys, [])


if __name__ == '__main__':
    unittest.main()
