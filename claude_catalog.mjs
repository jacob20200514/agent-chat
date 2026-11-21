import { spawn } from 'node:child_process';
import { query } from '@anthropic-ai/claude-agent-sdk';

const [executable, launchCommand, mode = 'models'] = process.argv.slice(2);
const abortController = new AbortController();
const timeout = setTimeout(() => abortController.abort(), 8000);
const session = query({
  prompt: (async function* () {})(),
  options: {
    abortController,
    pathToClaudeCodeExecutable: executable,
    persistSession: false,
    settingSources: ['user', 'project', 'local'],
    strictMcpConfig: true,
    tools: [],
    spawnClaudeCodeProcess: ({ args, cwd, env, signal }) =>
      spawn('zsh', ['-c', launchCommand + ' "$@"', 'agent-chat', ...args], { cwd, env, signal }),
  },
});

try {
  if (mode === 'usage') {
    const getUsage = session.usage_EXPERIMENTAL_MAY_CHANGE_DO_NOT_RELY_ON_THIS_API_YET;
    if (typeof getUsage !== 'function') {
      console.log(JSON.stringify({ error: '当前 Claude Agent SDK 无法读取额度，请在窗口输入 /usage' }));
    } else {
      const [data, account] = await Promise.all([getUsage.call(session, { skipBehaviors: true }), session.accountInfo()]);
      const windows = [];
      if (data.rate_limits_available) {
        for (const [key, minutes] of [['five_hour', 300], ['seven_day', 10080]]) {
          const row = data.rate_limits?.[key];
          if (typeof row?.utilization === 'number') {
            const reset = Date.parse(row.resets_at);
            windows.push({ minutes, used: row.utilization, reset_at: Number.isFinite(reset) ? reset / 1000 : null });
          }
        }
      }
      console.log(JSON.stringify({ usage: { windows, reset_count: null, email: account.email } }));
    }
  } else {
    const models = await session.supportedModels();
    console.log(JSON.stringify({ models: models.map(({ value, displayName }) => ({ value, displayName })) }));
  }
} finally {
  clearTimeout(timeout);
  session.close();
}
