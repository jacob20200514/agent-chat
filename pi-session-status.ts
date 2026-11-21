// Tell Agents Chat which transcript belongs to this pane, including after /new or /resume.
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

export default function (pi: ExtensionAPI) {
    const pane = process.env.TMUX_PANE;
    if (!pane || !process.env.TMUX) return;
    const set = async (option: string, value: string) => {
        await pi.exec('tmux', ['set-option', '-pq', '-t', pane, option, value]);
    };
    pi.on('session_start', async (_event, ctx) => {
        await set('@agent-chat-pi-provider', ctx.model?.provider ?? '-');
        await set('@agent-chat-pi-session', ctx.sessionManager.getSessionFile() ?? '');
        await set('@agent-chat-pi-request', '');
    });
    pi.on('model_select', async ({ model }) => {
        await set('@agent-chat-pi-provider', model.provider);
    });
    pi.on('session_shutdown', async () => {
        await set('@agent-chat-pi-provider', '-');
        await set('@agent-chat-pi-session', '');
        await set('@agent-chat-pi-request', '');
    });
    pi.on('message_start', async ({ message }) => {
        if (message.role !== 'user') return;
        const text = typeof message.content === 'string' ? message.content : message.content
            .map((part) => part.type === 'text' ? part.text : '').join(' ');
        // On the first turn Pi may not create the transcript until the assistant responds.
        await set('@agent-chat-pi-request', text.replace(/\s+/g, ' ').trim().slice(0, 120));
    });
}
