"""Read Claude and CodeBuddy status from their own UI, without Web-saved defaults."""
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLAUDE = ''' ▐▛███▛█   Claude Code v2.1.322
▝▜██████▀  Opus 5.5 with high effort · Claude Pro
 ▝▝   ▝▝   ~/CODE

❯
  ⏵⏵ auto mode on
'''

CODEBUDDY = '''╭─── CodeBuddy Code ─────────────────────────╮
│                                            │
│           http://127.0.0.1:61086             │
│                  hy4free                   │
│                    high                    │
│              /Users/someone                 │
╰────────────────────────────────────────────╯

>
⏵⏵ bypass permissions on
'''


class AgentLiveStatus(unittest.TestCase):
    def setUp(self):
        self.server = importlib.import_module('server')

    def status(self, agent, text, state='idle', history_complete=True):
        session = dict(agent=agent, state=state, model='stale-model', effort='max', history_complete=history_complete)
        self.server.update_agent_status(session, self.server.clean(text))
        return session['model'], session['effort']

    def test_claude_current_banner(self):
        self.assertEqual(self.status('claude', CLAUDE), ('opus', 'high'))

    def test_claude_catalog_lists_live_versions_and_preserves_the_current_highlight(self):
        rows = [{'value': 'default', 'displayName': 'Default'},
                {'value': 'opus', 'displayName': 'Opus 5.5'},
                {'value': 'claude-fable-5-1', 'displayName': 'Fable 5.1'}]
        session = {'agent': 'claude', 'state': 'idle', 'name': 'claude-test'}
        with (patch.object(self.server, 'tmux', return_value='/tmp/claude-project\n'),
              patch.object(self.server, 'capture', return_value=CLAUDE),
              patch('model_catalogs.claude_models', return_value=rows) as catalog):
            options = self.server.model_options(session)
        self.assertEqual(options['current']['model'], 'opus')
        self.assertEqual([row['id'] for row in options['models']], ['opus', 'claude-fable-5-1'])
        self.assertEqual(catalog.call_args.args[1], '/tmp/claude-project')

    def test_codebuddy_paid_model_does_not_match_the_free_variant(self):
        session = {'agent': 'codebuddy', 'state': 'idle', 'name': 'codebuddy-test'}
        choices = [{'id': 'hy4flash', 'label': 'hy4flash'}]
        with (patch.object(self.server, 'model_options', return_value={'models': choices}),
              patch.object(self.server, 'slash'),
              patch.object(self.server, 'wait_for'),
              patch.object(self.server, 'remember'),
              patch.object(self.server, 'screen', return_value='this session only'),
              patch.object(self.server, 'cursor_line', side_effect=['hy4flash-free', 'hy4flash  (x0.03 credits)']),
              patch.object(self.server, 'send_keys') as keys):
            self.server.set_model(session, 'hy4flash')
        self.assertEqual([call.args for call in keys.call_args_list],
                         [('codebuddy-test', 'Up'), ('codebuddy-test', 'Enter')])

    def test_codebuddy_completes_the_optional_reasoning_menu_before_acknowledging(self):
        session = {'agent': 'codebuddy', 'state': 'idle', 'name': 'codebuddy-test'}
        choices = [{'id': 'deepseek-v4.1-flash', 'label': 'deepseek-v4.1-flash'}]
        replies = ['Select Model', 'this session only', 'Select Reasoning Level', 'Switched this session to DeepSeek']
        with (patch.object(self.server, 'model_options', return_value={'models': choices}),
              patch.object(self.server, 'slash'),
              patch.object(self.server, 'wait_for', side_effect=replies) as wait_for,
              patch.object(self.server, 'screen', return_value='this session only'),
              patch.object(self.server, 'walk_to'),
              patch.object(self.server, 'send_keys') as keys,
              patch.object(self.server, 'remember') as remember):
            self.server.set_model(session, 'deepseek-v4.1-flash')
        self.assertEqual([call.args for call in keys.call_args_list],
                         [('codebuddy-test', 'Enter'), ('codebuddy-test', 'Enter')])
        wait_for.assert_called_with('codebuddy-test', 'Switched this session to', after='> /model')
        remember.assert_called_once_with('codebuddy-test', model='deepseek-v4.1-flash')

    def test_claude_credits_prompt_leaves_the_current_model_unchanged(self):
        session = {'agent': 'claude', 'state': 'idle', 'name': 'claude-test'}
        choices = [{'id': 'claude-fable-5-1', 'label': 'Fable 5.1'}]
        with (patch.object(self.server, 'model_options', return_value={'models': choices}),
              patch.object(self.server, 'slash'),
              patch.object(self.server, 'wait_for', side_effect=['Select model', 'session only', 'Manage usage credits']),
              patch.object(self.server, 'walk_to'),
              patch.object(self.server, 'remember') as remember,
              patch.object(self.server, 'send_keys') as keys):
            with self.assertRaisesRegex(RuntimeError, '需要 Claude 用量积分'):
                self.server.set_model(session, 'claude-fable-5-1')
        self.assertEqual([call.args for call in keys.call_args_list], [('claude-test', 's'), ('claude-test', 'Escape')])
        remember.assert_not_called()

    def test_claude_terminal_model_and_effort_switches(self):
        text = CLAUDE + '''❯ /model
  ⎿  Set model to Sonnet 5.5 for this session only
❯ /effort
  ⎿  Set effort level to low (this session only): Quick, straightforward
❯
'''
        self.assertEqual(self.status('claude', text), ('sonnet', 'low'))

    def test_claude_default_confirmation_uses_the_named_model(self):
        text = CLAUDE + '❯ /model\n  ⎿  Set model to Default (Fable 5.2) for this session only\n'
        self.assertEqual(self.status('claude', text), ('fable', 'high'))

    def test_claude_banner_is_a_fallback_without_switch_confirmations(self):
        text = CLAUDE + 'Claude Code v2.1.322\n ▐▛███▛█ Fable 5.2 with low effort\n'
        self.assertEqual(self.status('claude', text), ('fable', 'low'))

    def test_claude_redrawn_startup_banner_does_not_override_confirmed_session_settings(self):
        text = CLAUDE + '''❯ /model
  ⎿  Set model to Opus 5.5 for this session only
❯ /effort
  ⎿  Set effort level to xhigh (this session only): Deeper reasoning than high
Claude Code v2.1.322
 ▐▛███▛█ Fable 5.2 with low effort
❯
'''
        self.assertEqual(self.status('claude', text), ('opus', 'xhigh'))

    def test_claude_latest_confirmations_win_after_redraw_and_clear(self):
        text = CLAUDE + '''❯ /model
  ⎿  Set model to Opus 5.5 for this session only
❯ /effort
  ⎿  Set effort level to xhigh (this session only): Deeper reasoning than high
❯ /clear
Claude Code v2.1.322
 ▐▛███▛█ Fable 5.2 with low effort
❯ /model
  ⎿  Set model to Sonnet 5.5 for this session only
❯ /effort
  ⎿  Set effort level to high (this session only): High reasoning
Claude Code v2.1.322
 ▐▛███▛█ Fable 5.2 with low effort
❯
'''
        self.assertEqual(self.status('claude', text), ('sonnet', 'high'))

    def test_claude_startup_defaults_are_not_current_when_older_commands_may_be_missing(self):
        self.assertEqual(self.status('claude', CLAUDE, history_complete=False), (None, None))

    def test_claude_confirmation_survives_an_incomplete_history(self):
        text = CLAUDE + '''❯ /model
  ⎿  Set model to Opus 5.5 for this session only
❯ /effort
  ⎿  Set effort level to xhigh (this session only): Deeper reasoning than high
'''
        self.assertEqual(self.status('claude', text, history_complete=False), ('opus', 'xhigh'))

    def test_history_completeness_accounts_for_capture_and_tmux_limits(self):
        for size, limit, expected in [(100, 50000, True), (6745, 50000, False), (2000, 2000, False)]:
            row = f'qa\t1\t0\tclaude.exe\t/dev/ttys099\t1\t1\t0\t{size}\t{limit}\n'
            with (self.subTest(size=size, limit=limit),
                  patch.object(self.server, 'tmux', return_value=row),
                  patch.object(self.server, 'detect_agent', return_value='claude'),
                  patch.object(self.server, 'load', return_value={})):
                self.assertEqual(self.server.list_sessions()[0]['history_complete'], expected)

    def test_codebuddy_existing_welcome_box(self):
        self.assertEqual(self.status('codebuddy', CODEBUDDY), ('hy4free', 'high'))

    def test_codebuddy_wide_welcome_box(self):
        text = CODEBUDDY.replace('hy4free                   │', 'hy4free        │ Tips for getting started │')
        text = text.replace('high                    │', 'high           │ Recent activity          │')
        self.assertEqual(self.status('codebuddy', text), ('hy4free', 'high'))

    def test_codebuddy_terminal_changes_override_initial_welcome_box(self):
        text = CODEBUDDY + '''> /model
  └ Switched this session to hy4std.
> /effort max
  └ Model reasoning effort set to max.
>
'''
        self.assertEqual(self.status('codebuddy', text), ('hy4std', 'max'))

    def test_codebuddy_current_footer_overrides_earlier_state(self):
        text = CODEBUDDY + '>\n  hy4flash-free minimal · ← for agents · ? for shortcuts\n'
        self.assertEqual(self.status('codebuddy', text), ('hy4flash-free', 'minimal'))

    def test_unavailable_status_does_not_use_web_saved_values(self):
        for agent in ('claude', 'codebuddy'):
            with self.subTest(agent=agent):
                self.assertEqual(self.status(agent, 'Reply only\n❯\n'), (None, None))

    def test_response_text_is_not_a_cli_switch_confirmation(self):
        for agent, initial, expected in [('claude', CLAUDE, ('opus', 'high')), ('codebuddy', CODEBUDDY, ('hy4free', 'high'))]:
            with self.subTest(agent=agent):
                text = initial + '''❯ Explain this example
  ⎿  Set model to Haiku 5.5 for this session only
  └ Switched this session to hy4std.
'''
                self.assertEqual(self.status(agent, text), expected)

    def test_dead_agent_does_not_read_its_old_welcome_box(self):
        self.assertEqual(self.status('claude', CLAUDE, state='exited'), (None, None))


if __name__ == '__main__':
    unittest.main()
