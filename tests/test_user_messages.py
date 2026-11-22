"""The user's own message stays one message: a first line that starts "1." is not a menu's cursor row, and
lines the user indented continue it. Fixtures are 46-column captures after clean() (2026-11-22)."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLAUDE = ('❯ 先说一件事\n\n⏺ 好\n\n'
          '❯ 1. 第一项\n  2. 第二项\n     缩进的补充说明\n  - 横杠列表\n    只回复收到\n\n⏺ 收到\n\n✻ Brewed for 4s · done 10:13 PM\n\n'
          '❯ 看这个：\n  code line\n      indented code\n  只回复收到\n\n⏺ 收到\n\n✻ Baked for 1s · done 10:13 PM')
CODEBUDDY = ('> 1. 第一项\n  2. 第二项\n  缩进的补充说明\n  - 横杠列表\n  只回复收到\n\n∴ Thought\n\n● 收到\n\n✔ Worked for 1s')
CODEX = '› 1. 第一项\n  2. 第二项\n     缩进的补充说明\n\n  • 横杠列表\n    只回复收到\n\n• 收到\n\n  Worked for 2s • 22:14'
# Menus whose cursor row looks like a message that starts with "1.".
CLAUDE_MENU = '❯ 删掉临时文件\n\n⏺ Bash(rm -rf build)\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend'
CODEX_MENU = 'Usage\nAccount usage and resets.\n› 1. View analytics\n2. Redeem reset\nenter open · esc back'


class UserMessages(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        os.environ['AGENT_CHAT_HOME'] = self.home.name
        self.server = importlib.reload(importlib.import_module('server'))

    def tearDown(self):
        del os.environ['AGENT_CHAT_HOME']
        self.home.cleanup()

    def test_message_starting_with_a_number_is_the_users(self):
        lines = CLAUDE.split('\n')
        self.assertFalse(self.server.menu_row(lines, lines.index('❯ 1. 第一项')))
        cut = CLAUDE[:CLAUDE.index('❯ 看这个')].rstrip()
        self.assertEqual(self.server.last_request(cut), '1. 第一项2. 第二项缩进的补充说明- 横杠列表只回复收到')
        self.assertEqual(self.server.last_request(CODEBUDDY), '1. 第一项2. 第二项缩进的补充说明- 横杠列表只回复收到')
        self.assertTrue(self.server.last_request(CODEX).startswith('1. 第一项2. 第二项缩进的补充说明'))

    def test_indented_lines_continue_the_message(self):
        self.assertEqual(self.server.last_request(CLAUDE), '看这个：code line indented code只回复收到')

    def test_menu_cursor_row_is_not_a_message(self):
        for menu, row in ((CLAUDE_MENU, ' ❯ 1. Yes'), (CODEX_MENU, '› 1. View analytics')):
            lines = menu.split('\n')
            self.assertTrue(self.server.menu_row(lines, lines.index(row)), row)
        self.assertEqual(self.server.last_request(CLAUDE_MENU), '删掉临时文件')
        self.assertEqual(self.server.last_request(CODEX_MENU), '')


if __name__ == '__main__':
    unittest.main()
