"""Read completed chat replies, and whether a turn is running, from the records bound to a live CLI process.

Terminal output is for display only: renderers cannot distinguish progress text,
streaming answers and tools, and narrow panes clip status hints. If a CLI doesn't
expose a known session/format, omit the metadata instead of attributing another
pane's state to this one.
"""
import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime
from functools import lru_cache
from pathlib import Path


SESSION_ID = re.compile(r'[0-9a-f-]{36}', re.I)


def reply_status(scope, reply=None):
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    ident, stamp = reply or (None, None)
    at = stamp if type(stamp) is int else None  # CodeBuddy uses Unix milliseconds.
    if isinstance(stamp, str):
        try:
            at = int(datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() * 1000)
        except (ValueError, OverflowError):
            pass
    return {'scope': digest(scope), 'id': digest(f'{scope}:{ident}') if ident else None, 'at': at}


def text_block(content, kind):
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') == kind and (block.get('text') or '').strip()
        for block in content
    )


def reverse_lines(path):
    """Raw lines from the end, excluding the line the agent may still be writing."""
    with path.open('rb') as log:
        pos = log.seek(0, 2)
        remainder = b''
        first = True
        while pos:
            count = min(pos, 64 * 1024)
            pos -= count
            log.seek(pos)
            remainder, *lines = (log.read(count) + remainder).split(b'\n')
            if first and lines:
                lines.pop()  # empty if the file ends in a newline, otherwise a partial record
                first = False
            if not pos and not first:
                lines.insert(0, remainder)
            yield from reversed(lines)


def reverse_records(path, needles=()):
    """JSON records from the end; with needles, only lines containing one of them are decoded."""
    for line in reverse_lines(path):
        if needles and not any(needle in line for needle in needles):
            continue
        try:
            row = json.loads(line)
        except (UnicodeError, ValueError):
            continue
        if isinstance(row, dict):
            yield row


@lru_cache(maxsize=48)
def latest_reply(path, agent, modified_ns, size):
    completed = None
    for row in reverse_records(path):
        kind = row.get('type')
        if agent == 'claude':
            msg = row.get('message') or {}
            if (kind == 'assistant' and not any(row.get(flag) for flag in ('isSidechain', 'isMeta', 'isApiErrorMessage'))
                    and row.get('uuid') and msg.get('role') == 'assistant' and msg.get('stop_reason') == 'end_turn'
                    and text_block(msg.get('content'), 'text')):
                return row['uuid'], row.get('timestamp')
        elif agent == 'codebuddy':
            # An assistant text item is completed at the end of each model call, even
            # before tools run. It is a final reply only at the end of the outer turn.
            if kind == 'turn-metrics':
                completed = row
            elif kind in ('function_call', 'function_call_result', 'reasoning', 'message'):
                if (completed and kind == 'message' and row.get('role') == 'assistant'
                        and row.get('status') == 'completed' and row.get('id')
                        and text_block(row.get('content'), 'output_text')):
                    return row['id'], completed.get('timestamp')
                completed = None
        else:  # codex
            payload = row.get('payload') or {}
            if kind == 'event_msg':
                event = payload.get('type')
                if event == 'task_complete':
                    completed = (payload.get('turn_id'), row.get('timestamp')) if (payload.get('last_agent_message') or '').strip() else None
                elif event in ('task_started', 'turn_aborted'):
                    completed = None
            if (completed and completed[0] and kind == 'response_item' and payload.get('type') == 'message'
                    and payload.get('role') == 'assistant' and payload.get('phase') == 'final_answer'
                    and text_block(payload.get('content'), 'output_text')):
                return completed
    return None


# Claude publishes its own presence in the session registry: 'waiting' is a permission prompt or an
# open dialog/menu, 'shell' is idle with background shell tasks. Unknown values fall back to the screen.
CLAUDE_WORKING = {'busy': True, 'idle': False, 'waiting': False, 'shell': False}
CODEX_TURN_EVENTS = (b'task_started', b'task_complete', b'turn_aborted')


codex_turns = {}


def codex_turn_open(path, size):
    """Codex records every turn's start and its completion or abort. One root turn runs at a time, and a nested
    turn's end is not the root's. The rollout only grows, so each call reads just the bytes appended since."""
    offset, running = codex_turns.get(path, (0, frozenset()))
    if size < offset:
        offset, running = 0, frozenset()
    if size > offset:
        with path.open('rb') as log:
            log.seek(offset)
            chunk = log.read(size - offset)
        end = chunk.rfind(b'\n') + 1  # the record after the last newline may still be being written
        for line in chunk[:end].split(b'\n'):
            if not any(needle in line for needle in CODEX_TURN_EVENTS):
                continue
            try:
                row = json.loads(line)
            except (UnicodeError, ValueError):
                continue
            payload = row.get('payload') if isinstance(row, dict) and row.get('type') == 'event_msg' else None
            if not isinstance(payload, dict):
                continue
            event, turn = payload.get('type'), payload.get('turn_id')
            root = turn == payload.get('root_turn_id', turn)
            if event == 'task_started':
                running = frozenset({turn}) if root else running | {turn}
            elif event in ('task_complete', 'turn_aborted'):
                running = frozenset() if root else running - {turn}
        if len(codex_turns) >= 64:
            codex_turns.clear()
        codex_turns[path] = (offset + end, running)
    return bool(running)


def codex_worker_pids(pid):
    # Newer Codex TUIs run a private app-server child without a controlling tty.
    try:
        result = subprocess.run(['ps', '-axo', 'pid=,ppid=,comm='], capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return [pid]
    parents = {str(pid)}
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[1] == str(pid) and fields[0].isdigit() and Path(fields[2]).name == 'codex':
            parents.add(fields[0])
    return [int(p) for p in sorted(parents)]


def bound_codex_transcript(root, pid, started):
    """Resolve the CLI's top-level thread from the Codex process's own log records."""
    def database(prefix):
        paths = [p for p in root.glob(f'{prefix}_*.sqlite') if p.stem.split('_')[-1].isdigit()]
        return max(paths, key=lambda p: int(p.stem.split('_')[-1]), default=None)

    logs, state = database('logs'), database('state')
    if not logs or not state:
        return None, None
    pids = codex_worker_pids(pid)
    process_filter = ' OR '.join('process_uuid GLOB ?' for _ in pids)
    with closing(sqlite3.connect(logs.as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
        candidates = db.execute(f'''SELECT thread_id FROM logs WHERE ts >= ? AND thread_id IS NOT NULL
          AND ({process_filter}) GROUP BY thread_id ORDER BY max(id) DESC''',
                                (started, *(f'pid:{p}:*' for p in pids))).fetchall()
    if not candidates:
        return None, None
    with closing(sqlite3.connect(state.as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
        has_originator = any(row[1] == 'originator' for row in db.execute('PRAGMA table_info(threads)'))
        source = "source = 'cli'"
        if has_originator:
            source += " OR (source = 'vscode' AND originator = 'codex-tui')"
        for (thread,) in candidates:
            row = db.execute(f'SELECT rollout_path FROM threads WHERE id = ? AND ({source})', (thread,)).fetchone()
            if not row:
                continue
            path = Path(row[0]).resolve()
            if path.is_relative_to((root / 'sessions').resolve()) and path.name.endswith(f'-{thread}.jsonl'):
                return thread, path
    return None, None


def native_status(agent, process, home):
    """(reply status, whether a turn is running) for the CLI process; either is None when not exposed.
    CodeBuddy's turn state is not read from its transcript: an interrupted turn writes no end record."""
    if not process:
        return None, None
    pid, started = process
    root = home / f'.{agent}'
    try:
        working = None
        if agent == 'codex':
            session, path = bound_codex_transcript(root, pid, started)
        else:
            registry = json.loads((root / 'sessions' / f'{pid}.json').read_text(encoding='utf-8'))
            session = registry.get('sessionId', '')
            if (registry.get('pid') != pid or registry.get('kind') not in (None, 'interactive')
                    or not SESSION_ID.fullmatch(session) or registry.get('startedAt', 0) / 1000 < started):
                return None, None
            if agent == 'claude':
                working = CLAUDE_WORKING.get(registry.get('status'))
            matches = list((root / 'projects').glob(f'*/{session}.jsonl'))
            if len(matches) > 1:
                return None, working
            path = matches[0] if matches else None
        if not session:
            return None, None
        reply = None
        if path is not None:
            try:
                info = path.stat()
                reply = latest_reply(path, agent, info.st_mtime_ns, info.st_size)
                if agent == 'codex':
                    working = codex_turn_open(path, info.st_size)
            except FileNotFoundError:
                pass  # Session is live but has not created its transcript yet.
        return reply_status(f'{agent}:{session}', reply), working
    except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, AttributeError):
        return None, None
