import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reply_status

STAMP = '2026-11-21T09:00:00Z'
STAMP_MS = 1795251600000
SID = '01234567-89ab-cdef-0123-456789abcdef'


def text(kind='text'):
    return [{'type': kind, 'text': 'reply body'}]


class ReplyStatusTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()

    def snapshot(self, agent, *rows, incomplete=None):
        log = self.home / f'{agent}.jsonl'
        self.journal(log, rows, incomplete)
        info = log.stat()
        return reply_status.latest_reply(log, agent, info.st_mtime_ns, info.st_size)

    def journal(self, log, rows, incomplete=None):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(''.join(json.dumps(row) + '\n' for row in rows)
                       + (json.dumps(incomplete) if incomplete is not None else ''), encoding='utf-8')

    def test_claude_only_counts_top_level_nonempty_end_turn_text(self):
        def assistant(uuid, reason, content, **fields):
            return dict(type='assistant', uuid=uuid, timestamp=STAMP,
                        message=dict(role='assistant', stop_reason=reason, content=content), **fields)
        progress = assistant('1', 'tool_use', text())
        thinking = assistant('2', 'end_turn', text('thinking'))
        abort = assistant('3', None, text())
        final = assistant('4', 'end_turn', text())
        side = assistant('5', 'end_turn', text(), isSidechain=True)
        self.assertIsNone(self.snapshot('claude', progress, thinking, abort, side, incomplete=final))
        self.assertEqual(self.snapshot('claude', progress, thinking, final, abort, side), ('4', STAMP, 'reply body'))
        self.assertEqual(reply_status.reply_status('c', ('4', STAMP))['at'], STAMP_MS)

    def test_codebuddy_waits_for_turn_metrics_after_assistant_output(self):
        def assistant(ident):
            return dict(type='message', role='assistant', status='completed', id=ident, content=text('output_text'))
        progress = assistant('progress')
        tool = dict(type='function_call')
        result = dict(type='function_call_result')
        final = assistant('final')
        finished = dict(type='turn-metrics', timestamp=STAMP_MS)
        self.assertIsNone(self.snapshot('codebuddy', progress, tool, result, final))
        self.assertIsNone(self.snapshot('codebuddy', progress, tool, result, finished))
        self.assertEqual(self.snapshot('codebuddy', progress, tool, result, final, finished, progress, tool), ('final', STAMP_MS, 'reply body'))
        self.assertEqual(reply_status.reply_status('b', ('final', STAMP_MS))['at'], STAMP_MS)

    def test_codex_requires_final_answer_and_successful_turn_completion(self):
        def event(kind, **fields):
            return dict(type='event_msg', timestamp=STAMP, payload=dict(type=kind, **fields))
        def assistant(phase):
            return dict(type='response_item', payload=dict(type='message', role='assistant', phase=phase,
                                                            content=text('output_text')))
        started = event('task_started')
        aborted = event('turn_aborted')
        commentary, final = assistant('commentary'), assistant('final_answer')
        large_tool_result = dict(type='response_item', payload={'type': 'function_call_output', 'output': 'x' * 130000})
        finished = event('task_complete', turn_id='turn-1', last_agent_message='reply body')
        self.assertIsNone(self.snapshot('codex', started, commentary, large_tool_result, finished))
        self.assertIsNone(self.snapshot('codex', started, final, aborted))
        self.assertIsNone(self.snapshot('codex', started, final, incomplete=finished))
        self.assertEqual(self.snapshot('codex', started, commentary, large_tool_result, final, finished),
                         ('turn-1', STAMP, 'reply body'))
        # A later turn means the user asked again: the reply stays read or unread, but its excerpt is gone.
        self.assertEqual(self.snapshot('codex', started, commentary, large_tool_result, final, finished,
                                       started, commentary, aborted), ('turn-1', STAMP, None))

    def test_excerpt_lasts_until_the_user_sends_the_next_message(self):
        markdown = '## 结论\n**已修好**，`869c97c` 已推送\n- [打开页面](https://example.com/a)看第二点'
        final = dict(type='assistant', uuid='4', timestamp=STAMP,
                     message=dict(role='assistant', stop_reason='end_turn', content=[{'type': 'text', 'text': markdown}]))
        def claude_user(content, **fields):
            return dict(type='user', message=dict(role='user', content=content), **fields)
        human = claude_user('下一条', origin={'kind': 'human'})
        tool_result = claude_user([{'type': 'tool_result', 'content': 'x'}])
        command = claude_user('<command-name>/model</command-name>')
        notice = claude_user('<task-notification>', origin={'kind': 'task-notification'})
        self.assertEqual(self.snapshot('claude', final, tool_result, command, notice)[2], '结论 已修好，869c97c 已推送 打开页面看第二点')
        self.assertEqual(self.snapshot('claude', final, human, tool_result), ('4', STAMP, None))

        answer = dict(type='message', role='assistant', status='completed', id='final', content=text('output_text'))
        finished = dict(type='turn-metrics', timestamp=STAMP_MS)
        def codebuddy_user(**meta):
            return dict(type='message', role='user', content=[{'type': 'input_text', 'text': '下一条'}], providerData=meta)
        for meta, expected in (({'agent': 'cli'}, None), ({'skipRun': True}, 'reply body'),
                               ({'agent': 'cli', 'isMeta': True}, 'reply body')):
            with self.subTest(meta=meta):
                self.assertEqual(self.snapshot('codebuddy', answer, finished, codebuddy_user(**meta)), ('final', STAMP_MS, expected))

    def test_codex_process_binding_ignores_other_panes_old_processes_and_subagents(self):
        root = self.home / '.codex'
        root.mkdir()
        log = root / 'sessions' / f'rollout-{SID}.jsonl'
        self.journal(log, [dict(type='response_item', payload=dict(type='message', role='assistant', phase='final_answer',
                                                                   content=text('output_text'))),
                           dict(type='event_msg', timestamp=STAMP, payload=dict(type='task_complete', turn_id='a',
                                                                                last_agent_message='reply body'))])
        with sqlite3.connect(root / 'logs_2.sqlite') as db:
            db.execute('CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INT, process_uuid TEXT, thread_id TEXT)')
            db.executemany('INSERT INTO logs (ts, process_uuid, thread_id) VALUES (?, ?, ?)',
                           [(99, 'pid:99:old', SID), (100, 'pid:99:live', SID), (101, 'pid:99:live', 'sub'),
                            (102, 'pid:999:other', SID)])
        with sqlite3.connect(root / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)')
            db.executemany('INSERT INTO threads VALUES (?, ?, ?)', [(SID, str(log), 'cli'), ('sub', str(log), 'subagent')])
        self.assertEqual(reply_status.native_status('codex', (99, 100), self.home),
                         (reply_status.reply_status(f'codex:{SID}', ('a', STAMP, 'reply body')), False))
        self.assertEqual(reply_status.native_status('codex', (9, 100), self.home), (None, None))
        self.assertEqual(reply_status.native_status('codex', (99, 101), self.home), (None, None))

    def test_claude_and_codebuddy_are_bound_by_process_registry_not_latest_file(self):
        for agent in ('claude', 'codebuddy'):
            with self.subTest(agent=agent):
                root = self.home / f'.{agent}'
                (root / 'sessions').mkdir(parents=True)
                (root / 'sessions/99.json').write_text(json.dumps(dict(pid=99, kind='interactive', sessionId=SID, startedAt=200000)))
                chosen = root / 'projects' / 'one' / f'{SID}.jsonl'
                self.journal(chosen, [])
                if agent == 'claude':
                    final = dict(type='assistant', uuid='reply', timestamp=STAMP,
                                 message=dict(role='assistant', stop_reason='end_turn', content=text()))
                    self.journal(chosen.parent / 'other.jsonl', [final])
                self.assertEqual(reply_status.native_status(agent, (99, 199), self.home),
                                 (reply_status.reply_status(f'{agent}:{SID}'), None))
                self.assertEqual(reply_status.native_status(agent, (98, 199), self.home), (None, None))
                self.assertEqual(reply_status.native_status(agent, (99, 201), self.home), (None, None))


    def test_claude_turn_state_comes_from_its_registry_status(self):
        root = self.home / '.claude'
        (root / 'sessions').mkdir(parents=True)
        for status, working in [('busy', True), ('idle', False), ('waiting', False), ('shell', False), (None, None),
                                ('future-state', None)]:
            with self.subTest(status=status):
                record = dict(pid=99, kind='interactive', sessionId=SID, startedAt=200000)
                if status:
                    record['status'] = status
                (root / 'sessions/99.json').write_text(json.dumps(record))
                self.assertEqual(reply_status.native_status('claude', (99, 199), self.home)[1], working)
        # Another process's record never decides this pane's state.
        self.assertEqual(reply_status.native_status('claude', (98, 199), self.home), (None, None))

    def test_codex_turn_is_open_from_start_until_complete_or_abort(self):
        def event(kind, turn, root=None):
            return dict(type='event_msg', timestamp=STAMP, payload=dict(type=kind, turn_id=turn, root_turn_id=root or turn))

        def open_turn(*rows, incomplete=None):
            log = self.home / f'codex-turns-{next(names)}.jsonl'
            self.journal(log, rows, incomplete)
            return reply_status.codex_turn_open(log, log.stat().st_size)

        names = iter(range(100))

        filler = dict(type='response_item', payload=dict(type='reasoning', summary=[]))
        self.assertFalse(open_turn())
        self.assertTrue(open_turn(event('task_started', 'a'), filler, filler))
        self.assertFalse(open_turn(event('task_started', 'a'), filler, event('task_complete', 'a')))
        self.assertFalse(open_turn(event('task_started', 'a'), event('turn_aborted', 'a')))
        self.assertTrue(open_turn(event('task_started', 'a'), event('task_complete', 'a'), event('task_started', 'b')))
        # A nested turn ending does not end the root turn around it.
        self.assertTrue(open_turn(event('task_started', 'a'), event('task_started', 'c', 'a'), event('task_complete', 'c', 'a')))
        # The record still being written is not read yet.
        self.assertFalse(open_turn(event('task_started', 'a'), event('task_complete', 'a'), incomplete=event('task_started', 'b')))
        # Message text that mentions an event name is not an event.
        said = dict(type='response_item', payload=dict(type='message', role='assistant', content=text('task_started')))
        self.assertFalse(open_turn(event('task_started', 'a'), event('task_complete', 'a'), said))
        # A start whose end was never written is superseded by the next root turn.
        self.assertFalse(open_turn(event('task_started', 'a'), event('task_started', 'b'), event('task_complete', 'b')))

    def test_codex_turn_state_reads_only_appended_records(self):
        def event(kind, turn):
            return json.dumps(dict(type='event_msg', payload=dict(type=kind, turn_id=turn, root_turn_id=turn))) + '\n'

        log = self.home / 'codex-append.jsonl'
        log.write_text(event('task_started', 'a') + '{"type": "event_msg", "payload": {"type": "task_comp')
        self.assertTrue(reply_status.codex_turn_open(log, log.stat().st_size))
        with log.open('a') as out:
            out.write('lete", "turn_id": "a", "root_turn_id": "a"}}\n')
        self.assertFalse(reply_status.codex_turn_open(log, log.stat().st_size))
        with log.open('a') as out:
            out.write(event('task_started', 'b'))
        self.assertTrue(reply_status.codex_turn_open(log, log.stat().st_size))
        self.assertEqual(reply_status.codex_turns[log][0], log.stat().st_size)
        with patch.object(Path, 'open', side_effect=AssertionError('unchanged file was read')):
            self.assertTrue(reply_status.codex_turn_open(log, log.stat().st_size))


if __name__ == '__main__':
    unittest.main()
