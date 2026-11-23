"""The user's own message stays one message: a first line that starts "1." is not a menu's cursor row, and
lines the user indented continue it, and so do blank lines inside it. Fixtures are 46-column captures after
clean() (2026-11-22), raw captures (2026-11-23) and Pi's 100-column panels."""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLAUDE = ('❯ 先说一件事\n\n⏺ 好\n\n'
          '❯ 1. 第一项\n  2. 第二项\n     缩进的补充说明\n  - 横杠列表\n    只回复收到\n\n⏺ 收到\n\n✻ Brewed for 4s · done 10:13 PM\n\n'
          '❯ 看这个：\n  code line\n      indented code\n  只回复收到\n\n⏺ 收到\n\n✻ Baked for 1s · done 10:13 PM')
CODEBUDDY = ('> 1. 第一项\n  2. 第二项\n  缩进的补充说明\n  - 横杠列表\n  只回复收到\n\n∴ Thought\n\n● 收到\n\n✔ Worked for 1s')
CODEX = '› 1. 第一项\n  2. 第二项\n     缩进的补充说明\n\n  • 横杠列表\n    只回复收到\n\n• 收到\n\n  Worked for 2s • 22:14'
# Menus whose cursor row looks like a message that starts with "1.".
CLAUDE_MENU = '❯ 删掉临时文件\n\n⏺ Bash(rm -rf build)\n\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend'
CODEX_MENU = 'Usage\nAccount usage and resets.\n› 1. View analytics\n2. Redeem reset\nenter open · esc back'


# Raw 46-column captures (before clean()) of CodeBuddy 2.184 and Codex 0.171, 2026-11-23. Both show a blank
# line inside the message as an empty line; Codex's startup redraw leaves its input box in the scrollback with
# a line of one space under it.
CODEBUDDY_RAW = ('> 第一段第一行 \n  第一段第二行，这一行也比较长，会在窄窗格里 \n  折成两行显示出来 \n\n'
                 '  第二段在空行后面 \n  1. 编号一 \n  缩进四格的补充 \n  - 横杠列表 \n  只回复收到 \n\n'
                 '∴ Thought\n\n● 收到\n\n✔ Worked for 1s\n\n' + '─' * 46 + '\n'
                 '> 解释 f 函数功能                      ↵ send\n' + '─' * 46 + '\n'
                 'hy4…  ⏵⏵ bypass permissions on (⇧tab to cycle)\n\n\n')
CODEX_RAW = (' \n  >_ OpenAI Codex (v0.171.0)\n \n \n› Ask Codex to do anything\n \n  ? for shortcuts\n \n'
             '  >_ OpenAI Codex (v0.171.0)\n  permissions: YOLO mode\n \n \n› Ask Codex to do anything\n \n'
             '  GPT-6.2-Sol default · ~\n  ← for agents       ⚠ 1 warning · f2 to view\n\n\n'
             '› 第一段第一行\n  第一段第二行\n\n  第二段在空行后面\n\n  1. 编号一\n     缩进四格的补充\n\n'
             '  • 横杠列表\n    只回复收到\n\n\n• 收到\n\n  Worked for 1s • 08:19\n\n\n'
             ' \n \n› Ask Codex to do anything\n \n  GPT-6.2-Sol low · ~\n'
             '  ← for agents       ⚠ 1 warning · f2 to view\n')

# Pi 1.6.0 at 100 columns: the user's message is a panel, a line of spaces above and below text indented one
# column; tool cards are framed the same way. Its editor sits between two rules above the usage footer.
PAD = ' ' * 100


def panel(*rows):
    return ['', PAD, *((' ' + row).ljust(100) if row is not None else PAD for row in rows), PAD, '']


def pi_editor(slot=PAD, rule='─' * 100):
    return ['', rule, slot, '─' * 100, '~', '↑8.8k ↓510 R18k CH97.8% $0.024 (sub) 1.7%/500k (auto)' + ' ' * 28 + 'gpt-6.2-sol • xhigh', '']


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

    def snapshot(self, agent, raw):
        session = dict(name='s', agent=agent, state='idle')
        with patch.object(self.server.reply_status, 'native_status', return_value=(None, False)):
            text = self.server.session_snapshot(session, raw)
        return session, text

    def test_blank_line_inside_a_codebuddy_or_codex_message(self):
        """Last_request() follows the same line rules as the page's bubbles."""
        for agent, raw, whole in (
                ('codebuddy', CODEBUDDY_RAW, '第一段第一行第一段第二行，这一行也比较长，会在窄窗格里折成两行显示出来'
                                             '第二段在空行后面1. 编号一缩进四格的补充- 横杠列表只回复收到'),
                ('codex', CODEX_RAW, '第一段第一行第一段第二行第二段在空行后面1. 编号一缩进四格的补充• 横杠列表只回复收到')):
            with self.subTest(agent=agent):
                _, text = self.snapshot(agent, raw)
                chat = self.server.drop_composer(text, agent)
                self.assertEqual(self.server.last_request(chat), whole)
                self.assertIn('\n' + self.server.PADDED_BLANK + '\n  第二段在空行后面', chat)

    def test_reply_right_after_a_message_is_not_taken_in(self):
        for agent, raw in (('codex', '› 看一下\n\n• 好的\n  继续说明\n'),
                           ('codebuddy', '> 看一下 \n\n  ⎿  Interrupted · What should CodeBuddy do instead?\n')):
            with self.subTest(agent=agent):
                _, text = self.snapshot(agent, raw)
                self.assertEqual(text.split('\n')[1], '')
                self.assertEqual(self.server.last_request(text), '看一下')

    def test_prompt_mark_in_indented_tool_output_is_not_a_message(self):
        claude = ('❯ 看看 Codex 的屏幕\n\n⏺ Bash(tmux capture-pane -p -t cx)\n  ⎿  /review - review any changes\n'
                  '     › Ask Codex to do anything\n     … +54 lines (ctrl+o to expand)\n\n⏺ 看完了')
        self.assertEqual(self.server.last_request(claude), '看看 Codex 的屏幕')

    def test_codex_startup_input_box_is_not_a_message(self):
        session, text = self.snapshot('codex', CODEX_RAW)
        self.assertEqual(text.count('Ask Codex to do anything'), 1)
        self.assertNotIn('Ask Codex to do anything', self.server.drop_composer(text, 'codex'))
        self.assertNotIn('GPT-6.2-Sol default', text)
        self.assertEqual((session['model'], session['effort']), ('gpt-6.2-sol', 'low'))

    def test_pi_message_panels_match_the_transcript(self):
        tool = 'read docs README.md:1-2000 (ctrl+o to expand)'
        raw = '\n'.join([' Pi can explain its own features.', *panel('第一段第一行', '第一段第二行', None, '第二段'), ' 收到',
                         *panel(tool), *panel('好'), ' 好', *panel('？'), *panel('**加粗**'.replace('*', '')),
                         *panel('刚发出的消息'), *pi_editor(rule='── ⠏ Working ' + '─' * 87)])
        messages = ('第一段第一行\n第一段第二行\n\n第二段', '好', '？', '**加粗**')
        text = self.server.clean(self.server.drop_pi_editor(self.server.mark_pi_messages(raw, messages, '刚发出的消息')))
        lines = text.split('\n')
        start = lines.index('› 第一段第一行')
        self.assertEqual(lines[start:start + 4], ['› 第一段第一行', '  第一段第二行', self.server.PADDED_BLANK, '  第二段'])
        self.assertIn(' ' + tool, lines)
        self.assertEqual((lines.count('› 好'), lines.count(' 好')), (1, 1))
        for mark in ('› ？', '› 加粗', '› 刚发出的消息'):
            self.assertIn(mark, lines)
        self.assertEqual(self.server.last_request(text), '刚发出的消息')
        self.assertNotIn('Working', text)
        self.assertNotIn('gpt-6.2-sol', text)
        # The extension publishes 120 characters of a longer request.
        long = '很长的请求' * 30
        marked = self.server.mark_pi_messages('\n'.join(panel(long[:95], long[95:])), (), long[:120])
        self.assertIn('› ' + long[:95], marked)
        self.assertNotIn('›', self.server.mark_pi_messages('\n'.join(panel('别的消息')), (), long[:120]))

    def test_pi_editor_with_a_draft_or_selector_stays(self):
        chat = [*panel('看一下'), ' 好']
        for slot in ('draft starts here'.ljust(100), 'Thinking Level'):
            with self.subTest(slot=slot):
                raw = '\n'.join(chat + pi_editor(slot))
                self.assertEqual(self.server.drop_pi_editor(raw), raw)
        self.assertEqual(self.server.drop_pi_editor('\n'.join(chat + pi_editor())), '\n'.join(chat + ['']))


if __name__ == '__main__':
    unittest.main()
