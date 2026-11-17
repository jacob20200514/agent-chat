"""A pick-one list on the CLI screen is reported as waiting for a choice, and blocks slash commands."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MENU = """Which model should I use?
  ❯ 1. Opus
    2. Haiku
    Enter to select · ↑/↓ to navigate · Esc to cancel
"""

PLAIN_PROMPT = """Done. Anything else?

❯ /model
"""


class ChoiceMenuTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ['AGENT_CHAT_HOME'] = self.home.name
        self.server = importlib.reload(importlib.import_module('server'))

    def tearDown(self):
        del os.environ['AGENT_CHAT_HOME']
        self.home.cleanup()

    def test_menu_is_detected(self):
        self.assertTrue(self.server.choice_menu(MENU))

    def test_plain_screen_is_not_a_menu(self):
        self.assertFalse(self.server.choice_menu(PLAIN_PROMPT))

    def test_slash_command_refused_while_menu_is_open(self):
        sent = []
        self.server.screen = lambda name: MENU
        self.server.tmux = lambda *args, **kwargs: sent.append(args)
        with self.assertRaisesRegex(RuntimeError, '选择菜单'):
            self.server.slash('sess', '/model')
        self.assertEqual(sent, [], 'nothing may be typed into the menu')


if __name__ == '__main__':
    unittest.main()
