"""Read model catalogs and account usage through the local agent CLIs."""

import json
import os
import select
import shlex
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path


def messages(stdout, deadline, label, subject):
    pending = b''
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stdout], [], [], remaining)[0]:
            raise RuntimeError(f'读取 {label} {subject}超时，请重试')
        chunk = os.read(stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError(f'{label} 提前退出，未能读取{subject}')
        pending += chunk
        while b'\n' in pending:
            line, _, pending = pending.partition(b'\n')
            # Interactive zsh startup files may write informational text before Codex starts.
            if line.startswith(b'{'):
                yield json.loads(line)


@contextmanager
def catalog_process(command, cwd, label, subject='模型目录'):
    # The launch command comes from local configuration. Keep its CLI version and login environment.
    with subprocess.Popen(
        ['zsh', '-ic', command],
        cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as proc:
        responses = messages(proc.stdout, time.monotonic() + 10, label, subject)

        def send(message):
            proc.stdin.write(json.dumps(message).encode() + b'\n')
            proc.stdin.flush()

        def result(request_id):
            for message in responses:
                if message.get('id') == request_id:
                    if 'error' in message:
                        raise RuntimeError(f'{label} {subject}请求失败（{message["error"]["code"]}）')
                    return message['result']

        try:
            yield send, result, responses
        except (BrokenPipeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f'未能读取 {label} {subject}，请重试') from exc
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass  # Codex already closed its side of the transport.
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)


CLIENT = {'name': 'agent-chat', 'version': '1'}


@contextmanager
def codex_server(command, cwd, subject='模型目录'):
    with catalog_process(command + ' app-server --listen stdio://', cwd, 'Codex', subject) as (send, result, _):
        send({'method': 'initialize', 'id': 0, 'params': {'clientInfo': CLIENT}})
        result(0)
        send({'method': 'initialized'})
        yield send, result


def codex_models(command, cwd):
    with codex_server(command, cwd) as (send, result):
        params = {'includeHidden': False}
        models = []
        request_id = 1
        while True:
            send({'method': 'model/list', 'id': request_id, 'params': params})
            page = result(request_id)
            models.extend(page['data'])
            if not page.get('nextCursor'):
                return models
            params['cursor'] = page['nextCursor']
            request_id += 1


def masked_email(email):
    local, _, domain = (email or '').partition('@')
    return f'{local[:2]}***@{domain}' if local and domain else None


def codex_usage(command, cwd):
    with codex_server(command, cwd, '额度') as (send, result):
        send({'method': 'account/read', 'id': 1, 'params': {'refreshToken': False}})
        account = result(1).get('account') or {}
        if account.get('type') != 'chatgpt':
            return {'windows': [], 'reset_count': None}
        send({'method': 'account/rateLimits/read', 'id': 2})
        data = result(2)
    limits = data.get('rateLimits') or {}
    windows = []
    for key in ('primary', 'secondary'):
        row = limits.get(key) or {}
        if type(row.get('usedPercent')) in (int, float) and type(row.get('windowDurationMins')) is int:
            windows.append({'minutes': row['windowDurationMins'], 'used': row['usedPercent'], 'reset_at': row.get('resetsAt')})
    credits = data.get('rateLimitResetCredits') or {}
    count = credits.get('availableCount')
    return {'windows': windows, 'reset_count': count if type(count) is int and count >= 0 else None,
            'account_label': masked_email(account.get('email'))}


def claude_query(command, cwd, mode, subject):
    script = Path(__file__).with_name('claude_catalog.mjs')
    if not (script.parent / 'node_modules' / '@anthropic-ai' / 'claude-agent-sdk').is_dir():
        raise RuntimeError('尚未安装 Claude Agent SDK，请在 AgentsChat 目录安装依赖')
    launch = f'exec node {shlex.quote(str(script))} "$(command -v claude)" {shlex.quote(command)} {mode}'
    with catalog_process(launch, cwd, 'Claude', subject) as (_, _, responses):
        for response in responses:
            if 'error' in response:
                raise RuntimeError(response['error'])
            if mode in response:
                return response[mode]


def claude_models(command, cwd):
    return claude_query(command, cwd, 'models', '模型目录')


def claude_usage(command, cwd):
    result = claude_query(command, cwd, 'usage', '额度')
    result['account_label'] = masked_email(result.pop('email', None))
    return result


def codebuddy_models(command, cwd):
    launch = command + ' --acp --no-session-persistence --strict-mcp-config'
    with catalog_process(launch, cwd, 'CodeBuddy') as (send, result, _):
        send({'jsonrpc': '2.0', 'method': 'initialize', 'id': 1,
              'params': {'protocolVersion': 1, 'clientCapabilities': {}, 'clientInfo': CLIENT}})
        result(1)
        send({'jsonrpc': '2.0', 'method': 'session/new', 'id': 2,
              'params': {'cwd': str(cwd), 'mcpServers': []}})
        return result(2)['models']['availableModels']
