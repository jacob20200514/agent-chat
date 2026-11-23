#!/usr/bin/env python3
"""Agent Chat: a phone-sized web front for the agent CLIs living in this Mac's tmux sessions.

Standard library only. Listens on 127.0.0.1 with Tailscale Serve's tailnet-only HTTPS in front, or, with
AGENT_CHAT_TAILNET=1, also answers plain HTTP on this Mac's Tailscale address (WireGuard carries it).
The web and a terminal (Termius) drive the same tmux session: output is read with capture-pane,
input goes in with paste-buffer / send-keys. Text sent to a session at a shell prompt is run by that
shell, so the page is a remote terminal for whoever holds the password. Model pickers query the
configured agent CLI's model catalog.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import string
import subprocess
import sys
import threading
import time
from functools import lru_cache
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import model_catalogs
import reply_status

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('AGENT_CHAT_HOME', Path.home() / '.agent-chat'))
HOST = '127.0.0.1'
# Direct access by Tailscale IP, for a tailnet without HTTPS certificates. The socket then has to listen on
# every interface (the Tailscale address may not exist yet at boot), so each request is filtered instead:
# see direct_allowed.
TAILNET = os.environ.get('AGENT_CHAT_TAILNET') == '1'
TAILSCALE_RANGE = ipaddress.ip_network('100.64.0.0/10')
PORT = int(os.environ.get('AGENT_CHAT_PORT', '18795'))
TMUX = os.environ.get('AGENT_CHAT_TMUX') or shutil.which('tmux') or '/opt/homebrew/bin/tmux'
COOKIE = 'agent_chat'
MIN_PASSWORD = 8
REMEMBER_SECONDS = 30 * 86400
SESSION_SECONDS = 12 * 3600
HISTORY_LINES = 3000

# Default commands; zsh -ic gives them the login PATH and proxy env. A machine whose CLI needs another
# launch line (a node version manager, other flags) overrides 'command' per agent in DATA/agents.json.
SHELL_ONLY = 'shell'  # "new session" choice that starts no agent
# Agent TUIs run inline so tmux keeps scrollback for the page.
# Codex's update check is off for these sessions: its "Update available" prompt opens with "Update now"
# selected, so the first chat message's Enter ran a self-update and Codex exited, closing the tmux
# session (2026-11-14, when 0.169.0 came out). Updating stays a deliberate step in a terminal.
AGENTS = {
    'claude': {'label': 'Claude', 'command': 'CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 claude --permission-mode bypassPermissions', 'new': '/clear', 'width': 46},
    'codex': {'label': 'Codex', 'command': 'codex -c check_for_update_on_startup=false --no-alt-screen --sandbox danger-full-access --ask-for-approval never', 'new': '/new', 'width': 46},
    'codebuddy': {'label': 'CodeBuddy', 'command': 'codebuddy -y', 'new': '/clear', 'width': 46},
    # Pi truncates its footer from the right after adding token and usage counters: at 46 columns the model
    # and thinking level fall off it.
    'pi': {'label': 'Pi', 'command': 'pi --tui-mode regular', 'new': '/new', 'width': 100},
}
WIDE_MAX = 120  # the page's wide mode fits the pane to the window, up to this many columns
for _agent, _override in json.loads((DATA / 'agents.json').read_text()).items() if (DATA / 'agents.json').exists() else ():
    AGENTS[_agent]['command'] = _override['command']
SHELLS = {'zsh', 'bash', 'sh', 'fish', 'login', '-zsh', '-bash'}
KEYS = {'Up', 'Down', 'Left', 'Right', 'Enter', 'Escape', 'Tab', 'BTab', 'C-c', 'BSpace', 'Space',
        '1', '2', '3', '4', '5', '6', '7', '8', '9', 'y', 'n', 's'}
NAME_RE = re.compile(r'^[A-Za-z0-9_.@%+=-]{1,80}$')
BOX_ONLY = re.compile(r'^[\s─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║▔▁▏▕╌╍]+$')
PADDED_BLANK = '  '  # static/app.js blocks() matches this exact string
CURSOR = re.compile(r'^\s*(?:[│|]\s*)?[❯›>]\s*(?:\d+\.\s+)?(\S.*)$')

state_lock = threading.Lock()
session_locks = {}
seen_messages = {}
login_failures = {'count': 0, 'until': 0.0}


def load(name, default):
    try:
        return json.loads((DATA / name).read_text())
    except FileNotFoundError:
        return default


def save(name, value):
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = DATA / (name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(DATA / name)


def models():
    return json.loads((ROOT / 'models.json').read_text())


# ---- password and login -------------------------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=128 * 1024 * 1024, dklen=32)
    return {'salt': salt.hex(), 'hash': digest.hex()}


def check_password(password):
    stored = load('auth.json', {}).get('password')
    if not stored:
        return False
    return hmac.compare_digest(hash_password(password, bytes.fromhex(stored['salt']))['hash'], stored['hash'])


def first_password(password):
    """First run: the page sets the password. Checked and written under one lock, and only while none is
    stored, so this can never replace an existing password; changing one stays a terminal step."""
    with state_lock:
        if load('auth.json', {}).get('password'):
            return False
        save('auth.json', {'password': hash_password(password), 'logins': {}})
    return True


def new_login(remember):
    token = secrets.token_urlsafe(32)
    with state_lock:
        auth = load('auth.json', {})
        now = time.time()
        logins = {k: v for k, v in auth.get('logins', {}).items() if v > now}
        logins[hashlib.sha256(token.encode()).hexdigest()] = now + (REMEMBER_SECONDS if remember else SESSION_SECONDS)
        auth['logins'] = logins
        save('auth.json', auth)
    return token


def login_valid(token):
    if not token:
        return False
    return load('auth.json', {}).get('logins', {}).get(hashlib.sha256(token.encode()).hexdigest(), 0) > time.time()


def drop_login(token):
    with state_lock:
        auth = load('auth.json', {})
        auth.get('logins', {}).pop(hashlib.sha256((token or '').encode()).hexdigest(), None)
        save('auth.json', auth)


def direct_allowed(peer, local, host):
    """A request that did not come through loopback is served only if it is tailnet traffic: sent from a
    Tailscale address, to this Mac's Tailscale address, and asked for by that IP. The first two keep the
    LAN and the internet out although the socket listens everywhere. The Host rule stops DNS rebinding,
    where a web page's own domain is pointed at this address so its scripts count as same-origin -- on a
    fresh install that would let the page set the first password."""
    try:
        tailnet = ipaddress.ip_address(peer) in TAILSCALE_RANGE and ipaddress.ip_address(local) in TAILSCALE_RANGE
    except ValueError:
        return False
    return TAILNET and tailnet and (host or '').rsplit(':', 1)[0] == local


# ---- tmux -----------------------------------------------------------------------------------------

def tmux(*args, data=None, check=True):
    proc = subprocess.run([TMUX, '-u', *args], input=data, capture_output=True, text=True, timeout=15)
    if check and proc.returncode:
        raise RuntimeError(proc.stderr.strip() or 'tmux failed')
    return proc.stdout


def target(name):
    return '=' + name + ':'


def session_lock(name):
    with state_lock:
        return session_locks.setdefault(name, threading.Lock())


def list_sessions(only=None):
    """All tmux sessions, or with only= just that one: the chat page polls every second and needs no ps
    run for the other panes."""
    out = tmux('list-panes', '-a', '-F', '#{session_name}\t#{session_created}\t#{session_attached}\t#{pane_current_command}\t#{pane_tty}\t#{window_active}\t#{pane_active}\t#{pane_dead}\t#{history_size}\t#{history_limit}\t#{window_width}', check=False)
    rows = {}
    for line in out.splitlines():
        name, created, attached, command, tty, window_active, pane_active, dead, history_size, history_limit, width = line.split('\t')
        if only is not None and name != only:
            continue
        if name in rows and not (window_active == '1' and pane_active == '1'):
            continue
        rows[name] = {'name': name, 'created': int(created), 'attached': int(attached), 'command': command, 'tty': tty, 'dead': dead == '1', 'width': int(width),
                      'history_complete': int(history_size) <= HISTORY_LINES and int(history_size) < int(history_limit)}
    meta = load('sessions.json', {})
    result = []
    for row in rows.values():
        saved = meta.get(row['name'], {})
        agent = detect_agent(row) or saved.get('agent')
        shell = row['command'] in SHELLS or row['dead']
        result.append({'name': row['name'], 'nickname': saved.get('nickname') or row['name'], 'agent': None if shell else agent,
                       'agent_label': AGENTS[agent]['label'] if agent and not shell else ('Shell' if shell else '未知'),
                       'created': row['created'], 'attached': row['attached'], 'model': saved.get('model'), 'effort': saved.get('effort'),
                       'state': 'exited' if shell else 'unknown', 'dead': row['dead'], 'history_complete': row['history_complete'],
                       'width': row['width'], 'default_width': AGENTS[agent]['width'] if agent and not shell else None,
                       '_agent_process': row.get('_agent_process')})
    return sorted(result, key=lambda r: -r['created'])


def detect_agent(row):
    """What runs in the pane's foreground, from the processes on its tty."""
    try:
        ps = subprocess.run(['ps', '-o', 'pid=,etime=,command=', '-t', row['tty'].replace('/dev/', '')],
                            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in ps.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or not fields[0].isdigit():
            continue
        pid, elapsed, command = fields
        words = command.split()
        executable = Path(words[0]).name
        if executable == 'node' and len(words) > 1:
            executable = Path(words[1]).name
        agent = 'claude' if executable == 'claude.exe' else executable
        if agent not in AGENTS:
            continue
        days, sep, clock = elapsed.partition('-')
        seconds = int(days) * 86400 if sep else 0
        for i, part in enumerate(reversed((clock if sep else days).split(':'))):
            seconds += int(part) * 60 ** i
        # ps elapsed time has one-second precision.
        row['_agent_process'] = int(pid), int(time.time()) - seconds - 2
        return agent
    return None


def find_session(name):
    if not NAME_RE.match(name or ''):
        raise KeyError(name)
    for row in list_sessions(name):
        if row['name'] == name:
            return row
    raise KeyError(name)


def capture(name, lines=HISTORY_LINES):
    return tmux('capture-pane', '-p', '-J', '-S', '-' + str(lines), '-t', target(name))


def screen(name):
    """Only what is on the pane now: menus are read from here, never from the scrollback."""
    return tmux('capture-pane', '-p', '-J', '-t', target(name))


def clean(text):
    lines = []
    for line in text.replace(' ', ' ').splitlines():
        # Claude pads every line of the user's message to the pane width, its blank lines included, while
        # the gap after the message is a truly empty line. A padded blank is kept as PADDED_BLANK so the
        # page can tell a paragraph break inside a message from the end of it.
        padded = len(line) >= 2 and not line.strip()
        line = line.rstrip()
        if BOX_ONLY.match(line) and line.strip():
            continue
        if line.startswith('│') and line.endswith('│'):
            line = line[1:-1].rstrip()
        if not line and lines and not lines[-1].strip():
            if not padded:
                lines[-1] = ''
            continue
        lines.append(PADDED_BLANK if padded and not line else line)
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines)


PROMPT_LINE = re.compile(r'^\s*[❯›>](\s|$)')
# The cursor row of a confirmation menu ("> 1. Yes") carries the same mark as the input box, but it is a
# question the user has to see and answer, not the composer.
MENU_ROW = re.compile(r'^\s*[❯›>]\s+\d+\.\s')


def drop_composer(text, agent=None):
    """The agent CLIs keep their input box and hint footer at the bottom of the pane; it is not chat.
    The last prompt-marked line is that input box, so it and the few footer lines under it are cut."""
    if agent == 'pi':
        # Pi's editor has no prompt marker (a bare > belongs to its native menu's search field); its rules are
        # gone after clean(), so drop_pi_editor() cuts it from the raw capture.
        return text
    lines = text.split('\n')
    for i in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        if PROMPT_LINE.match(lines[i]):
            if MENU_ROW.match(lines[i]):
                return text
            return '\n'.join(lines[:i]).rstrip('\n')
    return text


# At the margin: the agents indent their tool output, which may hold a prompt mark of its own.
USER_LINE = re.compile(r'^([❯›>])\s+(\S.*)')
# Continuation of a message: two spaces plus any indent of the user's own. Replies that are indented too start
# with a tree glyph or a dot; Codex shows the user's "- " as "•".
USER_MORE = re.compile(r'^ {2}(\s*[^\s└⎿├│⏺●].*)')
MENU_HINT = re.compile(r'\b(?:enter|esc)\b.*\b(?:select|confirm|cancel|back|exit)\b', re.I)
REPLY_OR_PROMPT = re.compile(r'^\s*[❯›>⏺●•✻✶✳✢✽·∴✔⎿└](\s|$)')


def menu_row(lines, i):
    """A menu's cursor row ("❯ 1. Yes") is followed by the menu's key hint; a message of the user's that starts
    with "1." is followed by the reply or the next prompt instead. Same rule as menuRow() in app.js."""
    if not MENU_ROW.match(lines[i]):
        return False
    for line in lines[i + 1:i + 40]:
        if MENU_HINT.search(line):
            return True
        if REPLY_OR_PROMPT.match(line):
            return False
    return False


def keep_message_gaps(raw, agent):
    """Runs on the raw capture, before clean() turns every blank line into one kind.
    Codex and CodeBuddy show a blank line inside the user's message as an empty line, like the gap after the
    message; the message goes on below it with two-space-indented text, while a reply starts at the margin or
    with a tree glyph. Such a line becomes PADDED_BLANK, what Claude's padded blank lines become, so the page
    keeps the message in one bubble. Codex leaves the input box it drew before its startup redraw in the
    scrollback, with a line of one space under it, never an empty one: that box and its footer are dropped.
    The last input box is the live one; drop_composer() cuts it and the model is read from its footer."""
    lines = raw.split('\n')
    last_prompt = max((i for i, line in enumerate(lines) if PROMPT_LINE.match(line)), default=-1)
    out = []
    in_user = False
    i = 0
    while i < len(lines):
        line = lines[i]
        after = lines[i + 1] if i + 1 < len(lines) else None
        if agent == 'codex' and i < last_prompt and after == ' ' and USER_LINE.match(line):
            i += 2
            while i < len(lines) and lines[i].strip():
                i += 1
            in_user = False
            continue
        if USER_LINE.match(line):
            in_user = True
        elif in_user and line == '' and after is not None and USER_MORE.match(after):
            line = PADDED_BLANK
        elif not (in_user and (USER_MORE.match(line) or (len(line) >= 2 and not line.strip()))):
            in_user = False
        out.append(line)
        i += 1
    return '\n'.join(out)


NEW_CONVERSATION = {agent['new'] for agent in AGENTS.values()}


def last_request(text, limit=120):
    """The user's latest message in a cleaned pane, on one line, for the session list. Slash commands are
    skipped: '/model' says nothing about what the session is working on. The search stops at '/clear' or
    '/new': what was asked before it belongs to a conversation that is over, and showing it makes the
    session look like it still has work open. Same line rules as blocks() in app.js."""
    lines = text.split('\n')
    for i in range(len(lines) - 1, -1, -1):
        match = USER_LINE.match(lines[i])
        if not match or menu_row(lines, i):
            continue
        if match.group(2).split()[0] in NEW_CONVERSATION:
            return ''
        if match.group(2).startswith('/'):
            continue
        parts = [match.group(2).strip()]
        for line in lines[i + 1:]:
            more = USER_MORE.match(line)
            if more:
                parts.append(more.group(1).strip())
            elif line != PADDED_BLANK:
                break
        out = parts[0]
        for part in parts[1:]:
            # The CLI hard-wraps the message: Chinese continues without a space, English words need one back.
            out += (' ' if out[-1].isascii() and part[0].isascii() else '') + part
        return out[:limit]
    return ''


SPINNER = '·✢✳✶✻✽✷✸✹✺*'
ELAPSED = r'\((?:\d+h )?(?:\d+m )?\d+s'


def composer_busy(lines, agent):
    # Read native status immediately above the current composer, not old tool output or a typed draft.
    borders = [i for i, line in enumerate(lines) if re.fullmatch(r'\s*─{5,}\s*', line)]
    if len(borders) < 2:
        return False
    top, bottom = borders[-2:]
    if not any(PROMPT_LINE.match(line) and not MENU_ROW.match(line) for line in lines[top + 1:bottom]):
        return False
    status = [line for line in lines[:top] if line.strip()]
    if not status:
        return False
    if re.match(r'\s*⎿\s+Running(?:…|\.\.\.)\s+\(\d+[hms]\b', status[-1], re.I):
        return True
    # The status row can carry a hanging block below it: a tip, the /btw hint or a todo list.
    i = len(status) - 1
    while i > 0 and re.match(r' {3,}\S', status[i]):
        i -= 1
    if i > 0 and re.match(r'\s*[⎿└]\s', status[i]):
        i -= 1
    row = status[i].rstrip()
    if agent == 'codebuddy':
        # The CLI renders its turn timer one space in, optionally after a spinner and the clipped verb,
        # with optional phase/tokens and an ellipsis when the pane clips the row. The timer is present
        # across phases, not just while a tool runs.
        return bool(re.fullmatch(f'(?:[{SPINNER}] [^()]+)? {ELAPSED}(?: · .+)?[)…]', row))
    # Claude's spinner row is the verb or the active task with an ellipsis, then the timer (possibly clipped
    # with the interrupt hint); before the timer shows it is a single verb. A finished turn reads
    # "✻ Worked for 26s", which a narrow pane may also clip with an ellipsis.
    return bool(re.fullmatch(f'[{SPINNER}] (?:\\S[^()]*… {ELAPSED}.*|\\S+…)', row))


def busy(text, agent=None):
    lines = text.rstrip().splitlines()
    if agent == 'pi':
        # Pi's spinner is embedded in the editor border, even without an Esc hint.
        borders = [line for line in lines[-32:] if re.match(r'^─{2,}(?:\s*[\u2801-\u28ff]|─*\s*$)', line)]
        if len(borders) >= 2 and re.search(r'[\u2801-\u28ff]', borders[-2]):
            return True
    tail = '\n'.join(lines[-12:]).lower()
    if 'esc to interrupt' in tail or re.search(r'\(\d+s\b[^)]*esc', tail):
        return True
    # Codex clips "• Working (1m 05s • esc to interrupt)" to the pane width; a long header hides the hint.
    if agent == 'codex' and any(re.match(f'• .*{ELAPSED}(?: •.*|…)$', line.rstrip()) for line in lines[-12:]):
        return True
    return agent in ('claude', 'codebuddy') and composer_busy(lines[-24:], agent)


def pi_message_text(message):
    content = message.get('content', [])
    return content if isinstance(content, str) else ' '.join(part['text'] for part in content if part.get('type') == 'text')


@lru_cache(maxsize=16)
def pi_transcript(path, modified_ns, size):
    """Pi journals contain typed messages: neither terminal redraws nor tool output are replies."""
    with path.open(encoding='utf-8') as log:
        header = json.loads(next(log))
        if header.get('type') != 'session':
            return '', None, None, None
        entries = {}
        leaf = None
        for line in log:
            if not line.endswith('\n'):
                break  # the Pi process may still be appending this entry
            entry = json.loads(line)
            if entry.get('id'):
                leaf = entry['id']
                entries[leaf] = entry
    preview, reply, selection, asked = None, None, None, []
    while leaf:
        entry = entries.get(leaf, {})
        leaf = entry.get('parentId')
        if selection is None and entry.get('type') == 'model_change':
            selection = (entry.get('provider'), entry.get('modelId'))
        if entry.get('type') != 'message':
            continue
        msg = entry['message']
        if msg['role'] == 'user':
            asked.append(pi_message_text(msg))
            text = ' '.join(asked[-1].split())
            if preview is None and text and not text.startswith('/'):
                preview = text[:120]
        elif reply is None and msg['role'] == 'assistant' and msg.get('stopReason') == 'stop' and pi_message_text(msg).strip():
            reply = (entry['id'], entry.get('timestamp'))
    return preview or '', reply, selection, header.get('id'), tuple(asked)


def pi_pane_settings(name):
    """What the pane's own Pi published through pi-session-status.ts: provider, transcript path, latest request."""
    settings = tmux('display-message', '-p', '-t', target(name),
                    '#{@agent-chat-pi-provider}\t#{@agent-chat-pi-session}\t#{@agent-chat-pi-request}',
                    check=False).removesuffix('\n')
    provider, _, rest = settings.partition('\t')
    filename, _, request = rest.partition('\t')
    return provider, filename, request


def pi_user_messages(name):
    """The user's messages in the pane's transcript, and the request it is sending now: on the first turn the
    transcript may not exist until the assistant answers."""
    _, filename, request = pi_pane_settings(name)
    path = Path(filename)
    if path.is_absolute() and path.suffix == '.jsonl':
        try:
            info = path.stat()
            return pi_transcript(path, info.st_mtime_ns, info.st_size)[4], request
        except (OSError, ValueError, StopIteration):
            pass  # not written yet, or being rewritten; the request still marks the newest message
    return (), request


def message_key(text):
    """Pi renders the user's Markdown (bold, code spans, links) and wraps it to the pane: compare the letters
    and digits. A message of punctuation only ("？") is compared without its whitespace."""
    return re.sub(r'[\W_]+', '', text) or re.sub(r'\s+', '', text)


def mark_pi_messages(raw, messages, request=''):
    """Pi draws the user's message as a panel: a line of spaces above and below the text, between empty lines,
    and the text indented one column. Tool cards are drawn the same way, so a panel is the user's only when its
    text is one of the user's messages. It is rewritten the way Codex shows a message ("› ", then lines indented
    two spaces) so the page needs no Pi rule. Runs on the raw capture: clean() merges the panel's edges into
    the empty lines around it, and then an answer that repeats the message would look the same."""
    keys = {message_key(text) for text in messages if text.strip()}
    flat = ' '.join(request.split())
    newest = message_key(flat) if flat else ''

    def padding(line):
        return len(line) >= 2 and not line.strip()

    lines = raw.split('\n')
    out = []
    i = 0
    while i < len(lines):
        if lines[i] == '' and i + 1 < len(lines) and padding(lines[i + 1]):
            end = next((k for k in range(i + 2, len(lines) - 1) if padding(lines[k]) and lines[k + 1] == ''), None)
            body = lines[i + 2:end] if end else []
            if body and body[0].strip() and all(line.startswith(' ') for line in body):
                key = message_key('\n'.join(body))
                # The extension publishes at most 120 characters of the request.
                if key in keys or (newest and (key == newest or (len(flat) >= 120 and key.startswith(newest)))):
                    out += ['', '› ' + body[0][1:]] + [('  ' + line[1:]) if line.strip() else line for line in body[1:]]
                    i = end + 1
                    continue
        out.append(lines[i])
        i += 1
    return '\n'.join(out)


def drop_pi_editor(raw):
    """Pi's editor sits between two rules at the bottom of the pane, the working spinner on the upper rule and the
    directory and usage footer under the lower one; like the other agents' input boxes it is not chat. Only an
    empty editor is cut: a draft, or a selector (/model, /resume) drawn in its place, stays on the page."""
    lines = raw.split('\n')
    bottom = next((i for i in range(len(lines) - 1, max(-1, len(lines) - 12), -1)
                   if re.fullmatch(r'─{8,}', lines[i].strip())), None)
    if bottom is None:
        return raw
    top = next((i for i in range(bottom - 1, max(-1, bottom - 4), -1) if lines[i].startswith('──')), None)
    if top is None or any(line.strip() for line in lines[top + 1:bottom]):
        return raw
    return '\n'.join(lines[:top])


def update_pi_turn(session):
    # Only use metadata explicitly published by this pane, not another Pi session in the same directory.
    provider, filename, request = pi_pane_settings(session['name'])
    if provider:
        session['model_provider'] = provider if re.fullmatch(r'[\w.-]+', provider) and provider != '-' else None
    session['preview'] = request
    path = Path(filename)
    if not path.is_absolute() or path.suffix != '.jsonl':
        return
    try:
        info = path.stat()
        preview, reply, selection, session_id, _ = pi_transcript(path, info.st_mtime_ns, info.st_size)
        session['preview'] = preview or request
        if session_id:
            session['reply'] = reply_status.reply_status(f'pi:{path}', reply)
        # Older instances of the extension only publish the transcript path. The footer may omit
        # the provider, so match the recorded selection against the visible model before using it.
        if not provider and not session.get('model_provider') and selection and selection[1] == session.get('model'):
            session['model_provider'] = selection[0]
    except FileNotFoundError:
        # Pi has a session path before persisting the first turn.
        session['reply'] = reply_status.reply_status(f'pi:{path}')
    except (OSError, ValueError, StopIteration):
        pass  # concurrent rewrite; retry on the next poll


def session_snapshot(session, raw):
    process = session.pop('_agent_process', None)
    text = clean(keep_message_gaps(raw, session['agent']) if session['agent'] not in (None, 'pi') else raw)
    working = None
    if session['agent'] and session['agent'] != 'pi' and session['state'] != 'exited':
        # The CLI's own records decide when it exposes them; the screen is the fallback.
        reply, working = reply_status.native_status(session['agent'], process, Path.home())
        if reply is not None:
            session['reply'] = reply
    if session['state'] != 'exited':
        if working is None:
            working = busy(raw, session['agent'])
        session['state'] = 'busy' if working else 'idle'
    session['choice'] = session['state'] != 'exited' and choice_menu(text)
    update_agent_status(session, text)
    if session['agent'] == 'pi' and session['state'] != 'exited':
        update_pi_turn(session)
    return text


def choice_menu(text):
    """True while the CLI shows a pick-one list (e.g. a question with numbered options): either its key hint
    is on screen or the cursor sits on a numbered option. Only the bottom of the pane is read."""
    tail = text.rstrip().splitlines()[-20:]
    hint = any(re.search(r'enter to select|esc to cancel|escape/ctrl\+c cancel', line, re.I) for line in tail)
    cursor = any(re.search(r'[❯›]\s*\d+\.\s', line) for line in tail)
    return hint or cursor


def codex_usage_view(text):
    """识别 Codex 当前屏幕的 /usage 菜单或报表，避免把消息发给覆盖对话的界面。"""
    title = re.search(r'(?m)^[ \t]*Usage(?:[ \t]+·[^\n]*)?[ \t]*$', text)
    footer = re.search(
        r'(?m)^[ \t]*(?:(?:enter open|pgup/pgdn scroll)\s*·\s*)?esc back[ \t]*'
        r'(?:\n[ \t]*tab report\b[^\n]*)?\s*\Z', text)
    return bool(title and footer)


def codex_warnings_view(text):
    """Match the open F2 panel, whose footer takes over the chat input."""
    title = re.search(r'(?m)^[ \t]*Warnings · \d+ of \d+(?: ·[^\n]*)?[ \t]*$', text)
    footer = re.search(r'(?m)^[ \t]*k keep & next · esc dismiss & close\s*\Z', text)
    return bool(title and footer)


MODEL_FOOTER = re.compile(r'\s*([A-Za-z0-9][A-Za-z0-9_. -]*?)\s+(minimal|low|medium|high|xhigh|max|ultra)\s+·(?:\s.*)?', re.I)
CLAUDE_MODEL_LABEL = re.compile(r'\b((?:Opus|Sonnet|Fable|Haiku)\s+\d+(?:\.\d+)*)\b', re.I)
PI_FOOTER = re.compile(r'(?:^|\s)(?:\?|\d+(?:\.\d+)?%)/\S+(?: \(auto\))?(?: • xp)? {2,}'
                       r'(?:\(([\w.-]+)\) )?([\w./:-]+)'
                       r'(?: • (?:thinking )?(off|minimal|low|medium|high|xhigh|max|ultra))?(?: → .*)?$')


def welcome_model_status(agent, lines, *, complete_history=True):
    """Claude and existing CodeBuddy processes print a welcome block, then local command results.
    Claude can reprint its startup defaults after a redraw or /clear: its latest session-only
    confirmations take precedence. Only a result following /model or /effort can update them;
    prose in an agent reply is not a model switch."""
    label, effort, command = None, None, None
    confirmed_label, confirmed_effort = None, None
    for i, line in enumerate(lines):
        if agent == 'claude' and re.match(r'^[^A-Za-z0-9]*Claude Code v\d', line):
            label, effort, command = None, None, None
            # With a partial capture, a preceding session switch may have scrolled out. The welcome
            # defaults cannot then establish the current value, even if a redraw kept them visible.
            header = ' '.join(lines[i:i + 4]) if complete_history else ''
            model = CLAUDE_MODEL_LABEL.search(header)
            level = re.search(r'\bwith (low|medium|high|xhigh|max) effort\b', header, re.I)
            if model:
                label, effort = model.group(1), level.group(1).lower() if level else None
        elif agent == 'codebuddy' and re.match(r'^╭[─ ]*CodeBuddy Code\b', line):
            label, effort, command = None, None, None
            cells = []
            for row in lines[i + 1:i + 22]:
                if PROMPT_LINE.match(row):
                    break
                cells.append(row.split('│', 1)[0].strip())
            for model, level in zip(cells, cells[1:]):
                if re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', model) and level in ('minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
                    label, effort = model, level
        prompt = USER_LINE.match(line)
        if prompt:
            command = prompt.group(2).split()[0]
        if command == '/model':
            if agent == 'claude':
                result = re.match(r'^\s*⎿\s+Set model to (.+)$', line)
                model = CLAUDE_MODEL_LABEL.search(result.group(1)) if result else None
                if model:
                    label, command = model.group(1), None
                    confirmed_label = label
            else:
                result = re.match(r'^\s*└\s+Switched (?:this session|model) to (.+?)\.(?:\s.*)?$', line)
                if result:
                    label, command = result.group(1), None
        elif command == '/effort':
            pattern = (r'^\s*⎿\s+Set effort level to (low|medium|high|xhigh|max)\b' if agent == 'claude'
                       else r'^\s*└\s+Model reasoning effort set to (minimal|low|medium|high|xhigh|max)\.')
            result = re.match(pattern, line)
            if result:
                effort, command = result.group(1), None
                if agent == 'claude':
                    confirmed_effort = effort
    return confirmed_label or label, confirmed_effort or effort


def update_agent_status(session, text):
    """Reuse the polling capture to read the CLI's status, without typing or trusting Web-saved values."""
    agent = session.get('agent')
    if agent not in AGENTS:
        return
    session.update(model=None, effort=None)
    if session['state'] == 'exited':
        return
    lines = text.rstrip().splitlines()
    if agent == 'pi':
        session['model_provider'] = None
        for line in reversed(lines[-3:]):
            match = PI_FOOTER.search(line)
            if match and match[2] not in ('no-model', 'unknown'):
                session.update(model_provider=match[1], model=match[2], effort=match[3])
                break
        return
    label, effort = None, None
    if agent in ('claude', 'codebuddy'):
        label, effort = welcome_model_status(agent, lines, complete_history=session.get('history_complete', True))
    if agent in ('codex', 'codebuddy'):
        for line in reversed(lines[-2:]):
            match = MODEL_FOOTER.fullmatch(line)
            if match:
                label, effort = match.groups()
                break
    if label:
        key = re.sub(r'\s+', '', label).lower()
        label = next((o['id'] for o in models()[agent]['models']
                      if key in (re.sub(r'\s+', '', o['label']).lower(), o['id'].lower())), label.lower())
    session.update(model=label, effort=effort.lower() if effort else None)


def send_keys(name, *keys, pause=0.12):
    for key in keys:
        tmux('send-keys', '-t', target(name), key)
        time.sleep(pause)


def send_text(name, text):
    """Bracketed paste keeps a multi-line message one message; Enter follows once the CLI has taken it in."""
    buffer = 'agent-chat-' + secrets.token_hex(4)
    tmux('load-buffer', '-b', buffer, '-', data=text)
    tmux('paste-buffer', '-p', '-d', '-b', buffer, '-t', target(name))
    time.sleep(0.5 if len(text) < 2000 else 1.0)
    tmux('send-keys', '-t', target(name), 'Enter')


def wait_for(name, pattern, timeout=6.0, *, after=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = screen(name)
        if after:
            text = text.rsplit(after, 1)[-1]
        if re.search(pattern, text):
            return text
        time.sleep(0.2)
    raise RuntimeError('菜单没有出现，CLI 可能正忙；请稍后再试，或用方向键手动操作')


def cursor_line(name):
    # The menu is the lowest thing on the pane; earlier prompts of the conversation sit above it.
    found = ''
    for line in screen(name).splitlines():
        match = CURSOR.match(line)
        if match and not line.strip().startswith(('> /', '❯ /', '› /')):
            found = match.group(1)
    return found


def walk_to(name, label, *, pattern=None):
    """Find the menu row by its label so reordering cannot select a neighbour."""
    match = re.compile(pattern or re.escape(label), re.I).match

    def hit():
        return match(cursor_line(name).rstrip())
    for _ in range(30):
        if hit():
            return
        send_keys(name, 'Up', pause=0.06)
    for _ in range(60):
        if hit():
            return
        send_keys(name, 'Down', pause=0.08)
    send_keys(name, 'Escape')
    raise RuntimeError('本机 CLI 的菜单里没有「' + label + '」，请重新打开选项或用方向键手动选择')


def pi_editor_text(text):
    """Read the plain editor above Pi's slash-command suggestions and status footer."""
    lines = text.splitlines()
    borders = [i for i, line in enumerate(lines) if re.fullmatch(r'─{8,}', line.strip())]
    if len(borders) >= 2:
        start, end = borders[-2:]
        return '\n'.join(line.rstrip() for line in lines[start + 1:end]).strip('\n')
    return None


def slash(name, command, *, agent=None):
    """Type a slash command into an empty input box. If a draft is already sitting there (typed in the
    terminal, or a message the CLI has not taken yet), the command would be glued onto it and sent as a
    prompt -- seen with Codex on 2026-11-14 -- so what was typed is taken back and the action refused."""
    before = screen(name)
    if agent == 'pi' and busy(before, agent):
        raise RuntimeError('Pi 正在处理消息，请等这轮结束后再试')
    if choice_menu(before):
        raise RuntimeError('CLI 正停在选择菜单里，请先在输入框回复或在终端里选完，再切换')
    if agent == 'pi' and pi_editor_text(before) != '':
        raise RuntimeError('Pi 输入框里有草稿或当前界面无法操作，请返回空白输入框后再试')
    tmux('send-keys', '-t', target(name), '-l', command)
    time.sleep(0.4)
    after = screen(name)
    if agent == 'pi':
        ready = pi_editor_text(after) == command
    else:
        ready = any(re.fullmatch(r'[│|]?\s*[❯›>]\s*' + re.escape(command) + r'(\s.*)?', line.strip())
                    for line in after.splitlines() if command in line)
    if not ready:
        send_keys(name, *(['BSpace'] * len(command)), pause=0.02)
        raise RuntimeError('输入框里有还没发送的内容，请先发送或清空后再试')
    tmux('send-keys', '-t', target(name), 'Enter')


PI_MODEL_ROW = re.compile(r'^([→ ]) (✓ )?\s*([\w./:-]+)(?: \(.*\))? \[([\w.-]+)\](?: · default)?\s*$')
PI_EFFORT_ROW = re.compile(r'^([→ ]) (✓ )?\s*(off|minimal|low|medium|high|xhigh|max|ultra)\s{2,}(.+)$')


def pi_picker(text):
    """Read the live selector between Pi's editor borders, not mentions in old chat output."""
    editor = pi_editor_text(text)
    if not editor or not re.search(r'to select.*to set as default', ' '.join(editor.splitlines()), re.I):
        return None
    lines = editor.splitlines()
    kind = 'effort' if 'Thinking Level' in [line.strip() for line in lines] else 'model'
    options, cursor = [], None
    for line in lines:
        match = (PI_MODEL_ROW if kind == 'model' else PI_EFFORT_ROW).fullmatch(line)
        if not match:
            continue
        marker, current, value, detail = match.groups()
        id_ = detail + '/' + value if kind == 'model' else value
        if marker == '→':
            cursor = len(options)
        options.append({'id': id_, 'label': value, 'detail': detail.strip(), 'current': bool(current)})
    if kind == 'model' and not options and not any('models from configured providers' in line or line.startswith('Scope: ') for line in lines):
        return None
    pages = next((line.strip()[1:-1] for line in lines if re.fullmatch(r'\s*\(\d+/\d+\)\s*', line)), '')
    return {'kind': kind, 'options': options, 'cursor': cursor, 'position': pages}


def handle_pi_picker(name, kind, body):
    raw = screen(name)
    menu = pi_picker(raw)
    operation = body.get('op', 'open')
    if operation == 'cancel':
        if menu and menu['kind'] == kind:
            send_keys(name, 'Escape')
        return {}
    if operation == 'open' and not menu:
        slash(name, '/model' if kind == 'model' else '/thinking', agent='pi')
        for _ in range(20):
            menu = pi_picker(screen(name))
            if menu:
                break
            time.sleep(.1)
    if not menu or menu['kind'] != kind:
        raise RuntimeError('Pi 选择菜单未打开或已经变化，请关闭弹窗后重试')
    if operation == 'open':
        return menu
    if operation in ('prev', 'next'):
        send_keys(name, 'PageUp' if operation == 'prev' else 'PageDown')
        updated = pi_picker(screen(name))
        if not updated or updated['kind'] != kind:
            raise RuntimeError('Pi 菜单已经关闭，请重新打开')
        return updated
    if operation != 'select' or not isinstance(body.get('id'), str):
        raise ValueError('invalid picker operation')
    requested = body['id']
    for _ in range(10):
        if not menu or menu['kind'] != kind or menu['cursor'] is None:
            break
        index = next((i for i, item in enumerate(menu['options']) if item['id'] == requested), None)
        if index is None:
            break
        if index == menu['cursor']:
            send_keys(name, 'Enter')  # Pi's Ctrl+S, deliberately not used, changes the global default.
            for _ in range(20):
                raw = screen(name)
                if not pi_picker(raw) and pi_editor_text(raw) == '':
                    status = {'agent': 'pi', 'state': 'idle'}
                    update_agent_status(status, clean(raw))
                    expected = requested.split('/', 1)[1] if kind == 'model' else requested
                    if status[kind] == expected:
                        return {}
                time.sleep(.1)
            raise RuntimeError('未能从 Pi 确认切换结果，请查看对话顶部或终端')
        send_keys(name, 'Up' if index < menu['cursor'] else 'Down')
        menu = pi_picker(screen(name))
    raise RuntimeError('Pi 菜单的选项已变化，请关闭弹窗后重试')


def codex_options(session):
    rows = model_catalogs.codex_models(AGENTS['codex']['command'], Path.home())
    if not rows:
        raise RuntimeError('Codex 未返回可用模型，请检查 CLI 登录状态后重试')
    try:
        choices = [{'id': row['model'], 'label': row['displayName'],
                    'effort_ids': [item['reasoningEffort'] for item in row['supportedReasoningEfforts']]}
                   for row in rows]
    except (KeyError, TypeError) as exc:
        raise RuntimeError('Codex 模型目录格式无法识别') from exc
    current = dict(session)
    update_agent_status(current, clean(screen(session['name'])))
    key = re.sub(r'\s+', '', current['model'] or '').lower()
    selected = next((option for option in choices
                     if key in (option['id'].lower(), re.sub(r'\s+', '', option['label']).lower())), None)
    levels = selected['effort_ids'] if selected else {level for option in choices for level in option['effort_ids']}
    note = '仅本会话生效；max/ultra 消耗额度更快'
    if not selected:
        note += '。当前模型未识别，可选档位以终端菜单为准。'
    return {'models': choices, 'efforts': [level for level in models()['codex']['efforts'] if level['id'] in levels],
            'current': {'model': selected['id'] if selected else None, 'effort': current['effort']},
            'effort_note': note}


def model_options(session):
    agent = session['agent']
    if agent == 'codex':
        return codex_options(session)
    cwd = tmux('display-message', '-p', '-t', target(session['name']), '#{pane_current_path}').rstrip('\n')
    if not cwd:
        raise RuntimeError('无法读取当前会话的工作目录')
    launch = AGENTS[agent]['command']
    try:
        if agent == 'claude':
            choices = [{'id': row['value'], 'label': row['displayName']}
                       for row in model_catalogs.claude_models(launch, cwd) if row['value'] != 'default']
        else:
            choices = [{'id': row['modelId'], 'label': row['modelId']}
                       for row in model_catalogs.codebuddy_models(launch, cwd)]
    except (KeyError, TypeError) as exc:
        raise RuntimeError('CLI 模型目录格式无法识别') from exc
    if not choices:
        raise RuntimeError('CLI 未返回可用模型，请检查登录状态后重试')
    current = dict(session)
    update_agent_status(current, clean(capture(session['name'])))
    key = (current['model'] or '').lower()
    selected = next((row['id'] for row in choices if key in (row['id'].lower(), row['label'].lower())), None)
    return {'models': choices, 'current': {'model': selected}}


# Every switch below is made for the session only where the CLI offers that. The plain commands
# (/model <name>, /effort <level>, a digit or Enter in the menus) rewrite the CLI's saved default
# for every other session on this Mac -- found while probing on 2026-11-14.

def set_model(session, model):
    name, agent = session['name'], session['agent']
    options = model_options(session)
    option = next((m for m in options['models'] if m['id'] == model), None)
    if not option:
        raise ValueError('unknown model')
    label = option['label']
    literal = re.escape(label)
    if agent == 'claude':
        slash(name, '/model')
        menu = wait_for(name, r'Select model')
        label_pattern = literal + r'(?=\s{2,}|\s+✔|$)'
        if not re.search(r'^\s*(?:❯\s*)?\d+\.\s+' + label_pattern, menu, re.I | re.M) and '▸' in menu:
            walk_to(name, 'More models', pattern=r'▸\s+More models\b')
            send_keys(name, 'Enter')
            wait_for(name, r'▾\s+More models')
        walk_to(name, label, pattern=label_pattern)
        wait_for(name, r's\s+to\s+use\s+this\s+session\s+only')
        send_keys(name, 's')
        changed = r'⎿\s+Set model to\s+' + literal + r'(?=\s|$)'
        reply = wait_for(name, changed + r'|Switch model\?|Manage usage credits', after='❯ /model')
        if 'Manage usage credits' in reply:
            send_keys(name, 'Escape')
            raise RuntimeError(f'{label} 需要 Claude 用量积分，当前会话未切换')
        if 'Switch model?' in reply:
            send_keys(name, 'Enter')
            wait_for(name, changed, after='❯ /model')
    elif agent == 'codex':
        label_pattern = literal + r'(?: \(current\))?$'
        slash(name, '/model')
        wait_for(name, r'Select Model')
        walk_to(name, label, pattern=label_pattern)
        send_keys(name, 'Enter')
        wait_for(name, r'Select Reasoning Level')
        effort = next((e for e in options['efforts']
                       if e['id'] == options['current']['effort'] and e['id'] in option['effort_ids']), None)
        if effort:
            try:
                walk_to_level(name, effort['menu'])
            except RuntimeError:
                if 'Select Reasoning Level' not in screen(name):
                    slash(name, '/model'); wait_for(name, r'Select Model'); walk_to(name, label, pattern=label_pattern)
                    send_keys(name, 'Enter'); wait_for(name, r'Select Reasoning Level')
        send_keys(name, 's')
    else:
        slash(name, '/model')
        wait_for(name, r'Select Model')
        if "this session only" not in screen(name):
            send_keys(name, 'Tab')
        wait_for(name, r'this session only')
        walk_to(name, label, pattern=literal + r'(?=\s|$)')
        send_keys(name, 'Enter')
        switched = r'Switched this session to'
        reply = wait_for(name, switched + r'|Select Reasoning Level', after='> /model')
        if 'Select Reasoning Level' in reply:
            # CodeBuddy adds this step for some models. Apply its highlighted level in Session scope.
            send_keys(name, 'Enter')
            wait_for(name, switched, after='> /model')
    remember(name, model=model)


def walk_to_level(name, menu):
    """Codex keeps Max and Ultra behind "More reasoning…" (models.json gives them a two-step menu path).
    If the model lacks the level, walk_to escapes the submenu, leaving the reasoning menu open."""
    if isinstance(menu, list):
        walk_to(name, menu[0])
        send_keys(name, 'Enter')
        wait_for(name, r'Advanced Reasoning')
        try:
            walk_to(name, menu[1])
        except RuntimeError:
            wait_for(name, r'Select Reasoning Level')
            raise
    else:
        walk_to(name, menu)


def set_effort(session, effort):
    name, agent = session['name'], session['agent']
    levels = (codex_options(session) if agent == 'codex' else models()[agent])['efforts']
    option = next((e for e in levels if e['id'] == effort), None)
    if not option:
        raise ValueError('unknown effort')
    if agent == 'claude':
        slash(name, '/effort')
        wait_for(name, r'to adjust')
        send_keys(name, *(['Left'] * len(levels)), *(['Right'] * levels.index(option)), pause=0.1)
        send_keys(name, 's')
    elif agent == 'codex':
        slash(name, '/model')
        wait_for(name, r'Select Model')
        send_keys(name, 'Enter')  # the cursor starts on the current model
        wait_for(name, r'Select Reasoning Level')
        walk_to_level(name, option['menu'])
        send_keys(name, 's')
    else:
        # CodeBuddy has no session-only effort: this one is saved as its default (the page says so).
        slash(name, '/effort ' + option['id'])
    remember(name, effort=effort)


def remember(name, **fields):
    with state_lock:
        meta = load('sessions.json', {})
        meta.setdefault(name, {}).update(fields)
        save('sessions.json', meta)


def create_session(agent, nickname):
    if agent != SHELL_ONLY and agent not in AGENTS:
        raise ValueError('unknown agent')
    nickname = (nickname or '').strip()[:40]
    # The tmux session takes the nickname (tmux itself turns '.' and ':' into '_'), so Termius shows the same name.
    supplied_name = re.sub(r'[\s.:]+', '-', nickname).strip('-')
    if nickname and not supplied_name:
        supplied_name = 'web-' + agent + '-' + secrets.token_hex(3)

    launch_args = ['-c', str(Path.home())]
    prefix = 'zsh' if agent == SHELL_ONLY else agent
    if agent != SHELL_ONLY:
        width = str(AGENTS[agent]['width'])
        command = AGENTS[agent]['command']
        if agent == 'pi':
            command += ' --extension ' + shlex.quote(str(ROOT / 'pi-session-status.ts'))
        launch_args = ['-x', width, '-y', '50', *launch_args, 'zsh', '-ic', command]

    # tmux checks the name at creation, including races with other requests or terminals.
    for _ in range(10):
        name = supplied_name or prefix + '-' + ''.join(secrets.choice(string.ascii_lowercase) for _ in range(3))
        try:
            tmux('new-session', '-d', '-s', name, *launch_args)
        except RuntimeError as error:
            if str(error) != f'duplicate session: {name}':
                raise
            if nickname:
                raise RuntimeError(f'已有同名 tmux 会话「{name}」，请换个昵称') from error
        else:
            break
    else:
        raise RuntimeError('暂时无法生成不重复的会话昵称，请重试或填写昵称')

    if agent == SHELL_ONLY:
        remember(name, nickname=nickname or name)
        return name
    remember(name, agent=agent, nickname=nickname or name)
    if agent != 'pi':
        threading.Thread(target=accept_trust_prompt, args=(name,), daemon=True).start()
    return name


def set_width(session, columns):
    """The CLIs break their own lines at the pane width, so wider text on the page needs a wider pane.
    Codex, CodeBuddy and Pi clear their scrollback and reprint the conversation at the new width. Claude
    redraws only what it can still reach: its older output keeps the old width, and each switch leaves a
    second copy of the latest output, from a few lines to the whole last turn. Clearing the history would
    drop everything above the screen; a temporarily taller window still left copies (2026-11-22)."""
    if type(columns) is not int:
        raise ValueError('columns')
    columns = max(AGENTS[session['agent']]['width'], min(WIDE_MAX, columns))
    # resize-window pins the window size. Unsetting it right away keeps the size while no terminal is
    # attached, and Termius still resizes the window to its own screen when it attaches.
    tmux('resize-window', '-t', target(session['name']), '-x', str(columns), ';',
         'set-option', '-uw', '-t', target(session['name']), 'window-size')
    return columns


def accept_trust_prompt(name):
    """Claude and CodeBuddy ask on every start in ~ whether the folder is trusted, and Claude's default
    answer is to exit: the first chat message's Enter used to close the new session. The user chose ~ as
    the working directory, so the prompt is answered with trust for this folder only."""
    lock = session_lock(name)
    with lock:
        for _ in range(40):
            time.sleep(0.5)
            try:
                text = screen(name)
            except RuntimeError:
                return
            if answer_trust_prompt(name, text):
                return
            if re.search(r'bypass permissions on|Ask Codex to do anything', text):
                return


TRUST_PROMPTS = ('Yes, I trust this folder', 'Trust folder only')


def answer_trust_prompt(name, text):
    """Choose trust when the start-up question is on screen; False when it is not there."""
    if 'Yes, I trust this folder' in text:
        walk_to(name, 'Yes, I trust this folder'); send_keys(name, 'Enter'); return True
    if 'Trust folder only' in text:
        if cursor_line(name).startswith('Trust folder only'): send_keys(name, 'Enter')
        return True
    return False


def clear_trust_prompt(name):
    """The start-up thread stops watching after 20 s. A message sent while the question is still up
    used to answer it: its Enter picked Claude's default "No, exit" and the session closed
    (2026-11-21), so the question is answered here before any text goes in."""
    if not answer_trust_prompt(name, screen(name)):
        return
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(0.3)
        if not any(prompt in screen(name) for prompt in TRUST_PROMPTS):
            time.sleep(1.0)  # the input box comes up just after the question clears
            return
    raise RuntimeError('未发送：信任此目录的提示还没关掉，请用方向键选信任后重发')


# ---- http -----------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'AgentChat'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') and '/output' not in self.path and self.command != 'GET':
            sys.stdout.write('%s %s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), self.command, self.path.split('?')[0]))
            sys.stdout.flush()

    def reply(self, code, body=None, content_type='application/json', headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type + ('; charset=utf-8' if 'json' in content_type or 'text' in content_type or 'javascript' in content_type else ''))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        return cookie[COOKIE].value if COOKIE in cookie else None

    def body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > 200_000:
            raise ValueError('too large')
        return json.loads(self.rfile.read(length) or b'{}')

    def direct(self):
        return self.client_address[0] != HOST

    def refused(self):
        if self.direct() and not direct_allowed(self.client_address[0], self.connection.getsockname()[0], self.headers.get('Host')):
            self.reply(403, {'error': '只能通过本机或 Tailscale 地址（http://100.x.x.x:%d）访问' % PORT})
            return True
        return False

    def cookie_flags(self):
        # Secure cookies are not kept over plain HTTP; a direct tailnet request is plain HTTP by design.
        return '; Path=/; HttpOnly; SameSite=Strict' + ('' if self.direct() else '; Secure')

    def same_origin(self):
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        return self.headers.get('X-Agent-Chat') == '1' and (not origin or origin.split('://', 1)[-1] == host)

    def do_GET(self):
        if self.refused():
            return
        path = self.path.split('?')[0]
        static = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'application/javascript'), '/manifest.json': ('manifest.json', 'application/json'),
                  '/icon.svg': ('icon.svg', 'image/svg+xml'), '/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png')}
        if path in static:
            name, kind = static[path]
            return self.reply(200, (ROOT / 'static' / name).read_bytes(), kind)
        if not path.startswith('/api/'):
            return self.reply(404, {'error': 'not found'})
        if path == '/api/me':
            return self.reply(200, {'login': login_valid(self.token()), 'password_set': bool(load('auth.json', {}).get('password'))})
        if not login_valid(self.token()):
            return self.reply(401, {'error': '请先登录'})
        try:
            if path == '/api/sessions':
                rows = list_sessions()
                for row in rows:
                    if row['state'] == 'exited':
                        row.pop('_agent_process', None)
                        continue
                    text = session_snapshot(row, capture(row['name']))
                    if row['agent'] and row['agent'] != 'pi':
                        row['preview'] = last_request(drop_composer(text, row['agent']))
                return self.reply(200, {'sessions': rows, 'agents': {k: v['label'] for k, v in AGENTS.items()},
                                        'options': models(), 'usage_supported': True})
            match = re.fullmatch(r'/api/usage/(codex|claude)', path)
            if match:
                agent = match.group(1)
                reader = model_catalogs.codex_usage if agent == 'codex' else model_catalogs.claude_usage
                return self.reply(200, reader(AGENTS[agent]['command'], Path.home()))
            match = re.fullmatch(r'/api/sessions/([^/]+)/options', path)
            if match:
                session = find_session(match.group(1))
                if not session['agent'] or session['state'] == 'exited':
                    return self.reply(409, {'error': 'Agent 没有在这个会话里运行'})
                if session['agent'] == 'pi':
                    return self.reply(409, {'error': 'Pi 使用终端里的 /model 和 /thinking 菜单'})
                return self.reply(200, model_options(session))
            match = re.fullmatch(r'/api/sessions/([^/]+)/output', path)
            if match:
                session = find_session(match.group(1))
                raw = capture(session['name'])
                cleaned = session_snapshot(session, raw)
                if session.get('agent') == 'pi':
                    # Only the page's copy: the menu check above must not read "› 1." in a message as a menu.
                    marked = mark_pi_messages(raw, *pi_user_messages(session['name']))
                    cleaned = clean(drop_pi_editor(marked) if session['state'] != 'exited' else marked)
                if session.get('agent') and session['state'] != 'exited':
                    cleaned = drop_composer(cleaned, session['agent'])
                # The phone polls every second; when the screen is unchanged only the small session header is sent.
                version = hashlib.sha1(cleaned.encode()).hexdigest()[:16]
                reply = {'session': session, 'version': version}
                if self.path.partition('since=')[2].split('&')[0] != version:
                    reply['text'] = cleaned
                return self.reply(200, reply)
        except KeyError:
            return self.reply(404, {'error': '会话不存在或已关闭'})
        except Exception as exc:
            return self.reply(500, {'error': str(exc)[:300]})
        return self.reply(404, {'error': 'not found'})

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            if self.refused():
                return
            if not self.same_origin():
                return self.reply(403, {'error': 'forbidden'})
            body = self.body()
            if path == '/api/login':
                return self.login(body)
            if path == '/api/setup':
                password = str(body.get('password') or '')
                if len(password) < MIN_PASSWORD:
                    return self.reply(400, {'error': '密码至少 %d 位' % MIN_PASSWORD})
                if not first_password(password):
                    return self.reply(409, {'error': '密码已经设置过了，请直接登录'})
                return self.logged_in(bool(body.get('remember')))
            if not login_valid(self.token()):
                return self.reply(401, {'error': '请先登录'})
            if path == '/api/logout':
                drop_login(self.token())
                return self.reply(200, {}, headers=[('Set-Cookie', COOKIE + '=; Max-Age=0' + self.cookie_flags())])
            if path == '/api/sessions':
                return self.reply(200, {'name': create_session(body.get('agent'), body.get('nickname'))})
            match = re.fullmatch(r'/api/sessions/([^/]+)/(send|key|model|effort|new|kill|rename|width)', path)
            if not match:
                return self.reply(404, {'error': 'not found'})
            session, action = find_session(match.group(1)), match.group(2)
            name = session['name']
            if action == 'rename':
                remember(name, nickname=str(body.get('nickname') or '').strip()[:40] or name)
                return self.reply(200, {})
            if action == 'kill':
                if body.get('confirm') != name:
                    return self.reply(400, {'error': '需要确认'})
                tmux('kill-session', '-t', '=' + name)
                with state_lock:
                    meta = load('sessions.json', {}); meta.pop(name, None); save('sessions.json', meta)
                return self.reply(200, {})
            # One writer at a time per session: a second phone or tab waits its turn instead of interleaving keys.
            lock = session_lock(name)
            if not lock.acquire(timeout=20):
                return self.reply(409, {'error': '这个会话正在处理另一次操作，请稍后再试'})
            try:
                if action == 'key':
                    if body.get('key') not in KEYS:
                        return self.reply(400, {'error': 'key not allowed'})
                    send_keys(name, body['key'], pause=0)
                    return self.reply(200, {})
                if session['dead']:
                    return self.reply(409, {'error': '这个会话里的程序已退出'})
                # Text goes to whatever holds the pane, a bare shell included (2026-11-14). The slash
                # commands below are only ever typed into an agent.
                if action != 'send' and (session['state'] == 'exited' or not session['agent']):
                    return self.reply(409, {'error': 'Agent 没有在这个会话里运行'})
                if action == 'send':
                    text, message_id = str(body.get('text') or ''), str(body.get('id') or '')
                    if not text.strip():
                        return self.reply(400, {'error': '内容为空'})
                    with state_lock:
                        sent = seen_messages.setdefault(name, [])
                        if message_id and message_id in sent:
                            return self.reply(200, {'duplicate': True})
                    if session['agent'] == 'codex':
                        view = screen(name)
                        if codex_warnings_view(view):
                            return self.reply(409, {'code': 'CODEX_WARNINGS_OPEN',
                                                    'error': '未发送。请点「Esc」退出 Codex 警告面板后重发。'})
                        if codex_usage_view(view):
                            return self.reply(409, {'code': 'CODEX_USAGE_OPEN',
                                                    'error': '未发送。请点「Esc」退出 Codex 用量页后重发。'})
                    if session['agent'] == 'pi' and pi_picker(screen(name)):
                        return self.reply(409, {'code': 'PI_PICKER_OPEN', 'error': '未发送。请点 Esc 退出 Pi 选择菜单后重发。'})
                    if session['agent'] in ('claude', 'codebuddy'):
                        clear_trust_prompt(name)
                    with state_lock:
                        sent.append(message_id); del sent[:-200]
                    send_text(name, text)
                elif action == 'width':
                    if session['attached']:
                        return self.reply(409, {'error': '终端正连着这个会话，宽度跟随终端；断开终端后再切换'})
                    return self.reply(200, {'width': set_width(session, body.get('columns'))})
                elif action == 'new':
                    agent = session['agent']
                    slash(name, AGENTS[agent]['new'], agent=agent)
                elif session['agent'] == 'pi':
                    return self.reply(200, handle_pi_picker(name, action, body))
                elif action == 'model':
                    set_model(session, body.get('model'))
                elif action == 'effort':
                    set_effort(session, body.get('effort'))
                return self.reply(200, {})
            finally:
                lock.release()
        except KeyError:
            return self.reply(404, {'error': '会话不存在或已关闭'})
        except (ValueError, json.JSONDecodeError):
            return self.reply(400, {'error': '请求无效'})
        except Exception as exc:
            return self.reply(500, {'error': str(exc)[:300]})

    def login(self, body):
        now = time.time()
        with state_lock:
            if login_failures['until'] > now:
                return self.reply(429, {'error': '尝试次数过多，请 %d 秒后再试' % int(login_failures['until'] - now + 1)})
        if not check_password(str(body.get('password') or '')):
            with state_lock:
                login_failures['count'] += 1
                if login_failures['count'] >= 5:
                    login_failures['until'] = now + min(3600, 60 * 2 ** (login_failures['count'] - 5))
            time.sleep(0.5)
            return self.reply(401, {'error': '密码不正确'})
        with state_lock:
            login_failures.update(count=0, until=0.0)
        return self.logged_in(bool(body.get('remember')))

    def logged_in(self, remember_me):
        cookie = COOKIE + '=' + new_login(remember_me) + self.cookie_flags() + ('; Max-Age=%d' % REMEMBER_SECONDS if remember_me else '')
        return self.reply(200, {}, headers=[('Set-Cookie', cookie)])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'set-password':
        import getpass
        if not sys.stdin.isatty():
            sys.exit('请在 Mac 自己的终端或 Termius 里运行（这里没有可输入密码的终端）')
        print('输入时屏幕上不会显示任何字符，这是正常的；输完按回车。')
        while True:
            first = getpass.getpass('新密码（至少 %d 位）: ' % MIN_PASSWORD)
            if len(first) < MIN_PASSWORD:
                print('太短了：只有 %d 位，至少要 %d 位。再来一次。' % (len(first), MIN_PASSWORD)); continue
            if first != getpass.getpass('再输入一次: '):
                print('两次输入不一样。再来一次。'); continue
            break
        with state_lock:
            save('auth.json', {'password': hash_password(first), 'logins': {}})
        print('密码已设置，所有已登录设备需重新登录')
        return
    server = ThreadingHTTPServer(('0.0.0.0' if TAILNET else HOST, PORT), Handler)
    server.daemon_threads = True
    print('agent-chat listening on http://%s:%d%s' % (HOST, PORT, ' and this Mac\'s Tailscale address' if TAILNET else ''), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
