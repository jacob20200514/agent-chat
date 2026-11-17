#!/usr/bin/env python3
"""Agent Chat: a phone-sized web front for the agent CLIs living in this Mac's tmux sessions.

Standard library only. Listens on 127.0.0.1 with Tailscale Serve's tailnet-only HTTPS in front, or, with
AGENT_CHAT_TAILNET=1, also answers plain HTTP on this Mac's Tailscale address (WireGuard carries it).
The web and a terminal (Termius) drive the same tmux session: output is read with capture-pane,
input goes in with paste-buffer / send-keys. The only programs this server starts are tmux and the
three fixed agent commands below; text sent to a session that sits at a shell prompt is run by that
shell, so the page is a remote terminal for whoever holds the password.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
# Claude and Codex run inline, not on the alternate screen: there tmux keeps no scrollback, so the page
# could only ever show the last screenful.
# Codex's update check is off for these sessions: its "Update available" prompt opens with "Update now"
# selected, so the first chat message's Enter ran a self-update and Codex exited, closing the tmux
# session (2026-11-14, when 0.169.0 came out). Updating stays a deliberate step in a terminal.
AGENTS = {
    'claude': {'label': 'Claude', 'command': 'CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1 claude --permission-mode bypassPermissions', 'new': '/clear'},
    'codex': {'label': 'Codex', 'command': 'codex -c check_for_update_on_startup=false --no-alt-screen --sandbox danger-full-access --ask-for-approval never', 'new': '/new'},
    'codebuddy': {'label': 'CodeBuddy', 'command': 'codebuddy -y', 'new': '/clear'},
}
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


def list_sessions():
    out = tmux('list-panes', '-a', '-F', '#{session_name}\t#{session_created}\t#{session_attached}\t#{pane_current_command}\t#{pane_tty}\t#{window_active}\t#{pane_active}\t#{pane_dead}', check=False)
    rows = {}
    for line in out.splitlines():
        name, created, attached, command, tty, window_active, pane_active, dead = line.split('\t')
        if name in rows and not (window_active == '1' and pane_active == '1'):
            continue
        rows[name] = {'name': name, 'created': int(created), 'attached': int(attached), 'command': command, 'tty': tty, 'dead': dead == '1'}
    meta = load('sessions.json', {})
    result = []
    for row in rows.values():
        saved = meta.get(row['name'], {})
        agent = detect_agent(row) or saved.get('agent')
        shell = row['command'] in SHELLS or row['dead']
        result.append({'name': row['name'], 'nickname': saved.get('nickname') or row['name'], 'agent': None if shell else agent,
                       'agent_label': AGENTS[agent]['label'] if agent and not shell else ('Shell' if shell else '未知'),
                       'created': row['created'], 'attached': row['attached'], 'model': saved.get('model'), 'effort': saved.get('effort'),
                       'state': 'exited' if shell else 'unknown', 'dead': row['dead']})
    return sorted(result, key=lambda r: -r['created'])


def detect_agent(row):
    """What runs in the pane's foreground, from the processes on its tty."""
    try:
        ps = subprocess.run(['ps', '-o', 'command=', '-t', row['tty'].replace('/dev/', '')], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in ps.splitlines():
        words = line.split()
        head = ' '.join(words[:6])
        if 'codebuddy' in head:
            return 'codebuddy'
        if re.search(r'(^|[/ ])codex( |$)', head):
            return 'codex'
        if re.search(r'(^|[/ ])claude(\.exe)?( |$)', head):
            return 'claude'
    return None


def find_session(name):
    if not NAME_RE.match(name or ''):
        raise KeyError(name)
    for row in list_sessions():
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


def drop_composer(text):
    """The agent CLIs keep their input box and hint footer at the bottom of the pane; it is not chat.
    The last prompt-marked line is that input box, so it and the few footer lines under it are cut."""
    lines = text.split('\n')
    for i in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        if PROMPT_LINE.match(lines[i]):
            if MENU_ROW.match(lines[i]):
                return text
            return '\n'.join(lines[:i]).rstrip('\n')
    return text


USER_LINE = re.compile(r'^(\s*[❯›]|>)\s+(\S.*)')
USER_MORE = re.compile(r'^ {2}([^\s└⎿├│⏺●•].*)')
NEW_CONVERSATION = {agent['new'] for agent in AGENTS.values()}


def last_request(text, limit=120):
    """The user's latest message in a cleaned pane, on one line, for the session list. Slash commands are
    skipped: '/model' says nothing about what the session is working on. The search stops at '/clear' or
    '/new': what was asked before it belongs to a conversation that is over, and showing it makes the
    session look like it still has work open. Same line rules as blocks() in app.js."""
    lines = text.split('\n')
    for i in range(len(lines) - 1, -1, -1):
        match = USER_LINE.match(lines[i])
        if not match or MENU_ROW.match(lines[i]):
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


def busy(text):
    tail = '\n'.join(text.splitlines()[-12:]).lower()
    return 'esc to interrupt' in tail or bool(re.search(r'\(\d+s\b[^)]*esc', tail))


def choice_menu(text):
    """True while the CLI shows a pick-one list (e.g. a question with numbered options): either its key hint
    is on screen or the cursor sits on a numbered option. Only the bottom of the pane is read."""
    tail = text.splitlines()[-20:]
    hint = any(re.search(r'enter to select|esc to cancel', line, re.I) for line in tail)
    cursor = any(re.search(r'[❯›]\s*\d+\.\s', line) for line in tail)
    return hint or cursor


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


def wait_for(name, pattern, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = screen(name)
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


def walk_to(name, label):
    """Move a menu's cursor onto the row starting with label. Rows are matched, never counted, so a
    reordered or longer list cannot select a neighbour."""
    want = label.lower()
    hit = lambda: cursor_line(name).lower().startswith(want)
    for _ in range(30):
        if hit():
            return
        send_keys(name, 'Up', pause=0.06)
    for _ in range(60):
        if hit():
            return
        send_keys(name, 'Down', pause=0.08)
    send_keys(name, 'Escape')
    raise RuntimeError('本机 CLI 的菜单里没有「' + label + '」，请更新 models.json 或用方向键手动选择')


def slash(name, command):
    """Type a slash command into an empty input box. If a draft is already sitting there (typed in the
    terminal, or a message the CLI has not taken yet), the command would be glued onto it and sent as a
    prompt -- seen with Codex on 2026-11-14 -- so what was typed is taken back and the action refused."""
    if choice_menu(screen(name)):
        raise RuntimeError('CLI 正停在选择菜单里，请先在输入框回复或在终端里选完，再切换')
    tmux('send-keys', '-t', target(name), '-l', command)
    time.sleep(0.4)
    typed = [line.strip() for line in screen(name).splitlines() if command in line]
    if not any(re.fullmatch(r'[│|]?\s*[❯›>]\s*' + re.escape(command) + r'(\s.*)?', line) for line in typed):
        send_keys(name, *(['BSpace'] * len(command)), pause=0.02)
        raise RuntimeError('输入框里有还没发送的内容，请先发送或清空后再试')
    tmux('send-keys', '-t', target(name), 'Enter')


# Every switch below is made for the session only where the CLI offers that. The plain commands
# (/model <name>, /effort <level>, a digit or Enter in the menus) rewrite the CLI's saved default
# for every other session on this Mac -- found while probing on 2026-11-14.

def set_model(session, model):
    name, agent = session['name'], session['agent']
    option = next((m for m in models()[agent]['models'] if m['id'] == model), None)
    if not option:
        raise ValueError('unknown model')
    if agent == 'claude':
        slash(name, '/model')
        wait_for(name, r'Select model')
        walk_to(name, option['menu'])
        send_keys(name, 's')
        time.sleep(1.0)
        if 'Switch model?' in screen(name):
            send_keys(name, 'Enter')
    elif agent == 'codex':
        slash(name, '/model')
        wait_for(name, r'Select Model')
        walk_to(name, option['menu'])
        send_keys(name, 'Enter')
        wait_for(name, r'Select Reasoning Level')
        effort = next((e for e in models()[agent]['efforts'] if e['id'] == session.get('effort')), None)
        if effort:
            try:
                walk_to_level(name, effort['menu'])
            except RuntimeError:
                if 'Select Reasoning Level' not in screen(name):
                    slash(name, '/model'); wait_for(name, r'Select Model'); walk_to(name, option['menu'])
                    send_keys(name, 'Enter'); wait_for(name, r'Select Reasoning Level')
        send_keys(name, 's')
    else:
        slash(name, '/model')
        wait_for(name, r'Select Model')
        if "this session only" not in screen(name):
            send_keys(name, 'Tab')
        wait_for(name, r'this session only')
        walk_to(name, option['menu'])
        send_keys(name, 'Enter')
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
    levels = models()[agent]['efforts']
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
    name = re.sub(r'[\s.:]+', '-', nickname).strip('-') or 'web-' + agent + '-' + secrets.token_hex(3)
    if name in {r['name'] for r in list_sessions()}:
        raise RuntimeError('已有同名 tmux 会话「' + name + '」，请换个昵称')
    if agent == SHELL_ONLY:
        # A plain login shell in ~: typed into from the page's message box or from Termius.
        tmux('new-session', '-d', '-s', name, '-c', str(Path.home()))
        remember(name, nickname=(nickname or '').strip()[:40] or name)
        return name
    # Phone-wide until a terminal attaches (tmux then follows that client's size): the CLI wraps its own
    # lines, and at 80 columns every paragraph would be re-wrapped raggedly on a phone.
    tmux('new-session', '-d', '-s', name, '-x', '46', '-y', '50', '-c', str(Path.home()), 'zsh', '-ic', AGENTS[agent]['command'])
    remember(name, agent=agent, nickname=(nickname or '').strip()[:40] or name)
    threading.Thread(target=accept_trust_prompt, args=(name,), daemon=True).start()
    return name


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
            if 'Yes, I trust this folder' in text:
                walk_to(name, 'Yes, I trust this folder'); send_keys(name, 'Enter'); return
            if 'Trust folder only' in text:
                if cursor_line(name).startswith('Trust folder only'): send_keys(name, 'Enter')
                return
            if re.search(r'bypass permissions on|Ask Codex to do anything', text):
                return


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
                    if row['state'] != 'exited':
                        # One capture serves both: the tail tells busy from idle, the history holds the last request.
                        text = clean(capture(row['name']))
                        row['state'] = 'busy' if busy(text) else 'idle'
                        row['choice'] = choice_menu(text)
                        row['preview'] = last_request(drop_composer(text)) if row['agent'] else ''
                return self.reply(200, {'sessions': rows, 'agents': {k: v['label'] for k, v in AGENTS.items()}, 'options': models()})
            match = re.fullmatch(r'/api/sessions/([^/]+)/output', path)
            if match:
                session = find_session(match.group(1))
                text = capture(session['name'])
                session['state'] = 'exited' if session['state'] == 'exited' else ('busy' if busy(text) else 'idle')
                session['choice'] = session['state'] != 'exited' and choice_menu(text)
                cleaned = clean(text)
                if session.get('agent') and session['state'] != 'exited':
                    cleaned = drop_composer(cleaned)
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
            match = re.fullmatch(r'/api/sessions/([^/]+)/(send|key|model|effort|new|kill|rename)', path)
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
                        sent.append(message_id); del sent[:-200]
                    send_text(name, text)
                elif action == 'new':
                    slash(name, AGENTS[session['agent']]['new'])
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
