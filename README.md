# Agent Chat

在手机上用的 Agent 聊天网页（PWA）。它不自己跑 Agent，只是这台 Mac 上 tmux 会话的一个窗口：
网页和终端（比如 Termius）操作的是同一个 tmux 会话，关掉网页不影响 Agent。
支持 Claude Code、Codex、CodeBuddy、Pi Coding Agent，也能开一个普通 Shell 会话。

> **先看清楚再装**：登录网页的人可以在这台 Mac 上执行任意命令（Shell 会话就是远程终端），
> Agent 默认以免确认模式启动（Claude `bypassPermissions`、Codex `danger-full-access`、CodeBuddy `-y`，Pi 默认用当前用户权限执行工具）。
> 服务只接受本机和 Tailscale 网络里的访问（局域网和公网的请求一律拒绝），不要用别的方式把它暴露到公网。

## 安装

需要：macOS、[Homebrew](https://brew.sh)、[Tailscale](https://tailscale.com)（Mac 和手机登录同一个账号），以及至少一个已登录好的 Agent 命令行（`claude` / `codex` / `codebuddy` / `pi`）。Pi 安装及登录见 [官方快速上手](https://docs.pi.dev/latest/quickstart)。

使用 Claude 模型自动同步时，需在仓库目录用 Node.js 和 npm 安装 Claude Agent SDK，首次安装或依赖更新后运行 `npm ci --omit=optional --ignore-scripts`。SDK 使用本机已登录的 Claude Code。

```bash
brew install tmux python
git clone https://github.com/jacob20200514/Agent-chat.git ~/agent-chat
~/agent-chat/agentchat install
```

装完会直接打印访问地址，形如 `http://100.x.x.x:18795`（这台 Mac 的 Tailscale 地址）。在登录了同一个 Tailscale 账号的手机或电脑上打开它即可，不需要在 Tailscale 后台做任何配置。iPhone 上用 Safari 打开后可以"添加到主屏幕"。以后想再看地址：`agentchat url`。

- `agentchat install` 会生成并加载 LaunchAgent `com.openclaw.agent-chat`（开机自启、崩溃自动拉起），并把 `agentchat` 命令链接到 `~/.local/bin`。移动了仓库目录或换了 Python 后重新运行一次。
- 地址是 `http://` 不是 `https://`：Tailscale 设备之间的流量本身是加密的，所以不需要证书。服务只回应"从 Tailscale 地址发来、发往本机 Tailscale 地址、并且用这个 IP 打开"的请求。
- 手机打不开时：确认手机的 Tailscale 已连接；Mac 开了系统防火墙的话，在「系统设置 → 网络 → 防火墙 → 选项」里允许 Python 接收传入连接。
- 想用 Tailscale 的 HTTPS 域名（需要在 Tailscale 后台开启 HTTPS 证书）：改用 `agentchat install --https-only`，服务就只监听 `127.0.0.1`，再运行 `tailscale serve --bg --https=11443 http://127.0.0.1:18795`，地址见 `tailscale serve status`。

## 设置密码

**第一次打开网页时直接在页面上设置登录密码**（至少 8 位，输两遍），设完即登录。装好后请马上去设：设好之前，能打开这个地址的人都可以抢先设置。

- 只有还没有密码时页面才允许设置；已有密码后这个入口自动关闭。
- 忘记或想修改密码：在 Mac 的终端里运行 `agentchat passwd`（会让所有设备重新登录）。
- 只存加盐哈希；连续输错 5 次后会限速。

## 日常

- `agentchat start | stop | restart | status | logs`
- 数据在 `~/.agent-chat/`：密码哈希与登录状态 `auth.json`、会话昵称 `sessions.json`、日志 `server.log`。
- 更新：`git pull` 后 `agentchat restart`。

## 行为

- 首页列出本机全部 tmux 会话（含不是网页建的）；Agent 类型按会话里实际运行的进程判断。
- 新建：Claude / Codex / CodeBuddy / Pi，或「Zsh」（在 `~` 开一个普通 Shell 会话，输入框里发的文字会作为 Shell 命令执行）。工作目录为 `~`，tmux 会话名取昵称（空格、点、冒号换成 `-`，重名会拒绝）。昵称留空时用小写 Agent 名称、`-` 和三个随机小写英文字母，例如 `codebuddy-bpd`，重名时自动重选。
  Claude、CodeBuddy 在 `~` 下启动时的"是否信任此目录"提示会自动选信任（否则第一条消息的回车会选中"退出"）：新建后盯 20 秒，发送消息时若提示还在，先选信任再发。
- Agent 的启动命令写在 `server.py` 的 `AGENTS` 里，用 `zsh -ic` 启动（会读你的 `~/.zshrc`）。你的机器需要别的启动方式（比如经 node 版本管理器启动，或想去掉免确认参数）时，不用改代码，在 `~/.agent-chat/agents.json` 里按 Agent 覆盖，然后 `agentchat restart`：

  ```json
  {"codex": {"command": "fnm exec --using=default codex --no-alt-screen"}}
  ```

  Claude 和 Codex 要保留"不用备用屏幕"的设置（`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`、`--no-alt-screen`），否则网页只能看到最后一屏；Codex 建议保留 `-c check_for_update_on_startup=false`，否则出新版时第一条消息的回车会触发自更新并退出。
  Pi 用 `pi --tui-mode regular` 启动以保留 tmux 历史，新建时用 100 列而非 46 列，避免用量数据挤掉底部的模型和思考级别。旧会话可在终端未连接时在电脑上点「宽屏」加宽（无需重启 Pi）。`--approve` 用于信任项目配置和扩展，仅在想主动信任当前项目时使用。
- 输出每秒读一次 `tmux capture-pane`（纯文本，最近 3000 行）；输入用带括号粘贴的 `paste-buffer` 再回车，多行消息不会被拆开。你发的消息显示为右侧气泡：Claude、Codex、CodeBuddy 认输入行的 ❯ / › / > 标记，消息里的空行、缩进行不会把它拆开；Pi 的消息没有标记，按它画的消息框认，并且要和该会话的 Pi 对话记录逐字对上（工具卡片的框一样，不会被认成你的消息），所以不是从网页新建的 Pi 会话只显示左侧气泡。Pi 底部的输入框和用量栏不放进对话，输入框里有草稿或开着选择菜单时照常显示。会话列表每张卡片下显示该会话里你最近发的一条消息（最多两行，斜杠命令不算）；手机上在对话页向右滑返回列表。电脑上回车发送，Ctrl+回车（或 Cmd/Shift+回车）换行；手机上回车是换行，点「发送」发送。
- 同一会话同一时刻只处理一个写操作；每条消息带编号，断线重试不会重复发送。
- 会话里跑的是 Shell 而不是 Agent 时，输入框发的文字直接敲进 Shell 并回车执行。切模型、切档位、新对话只对 Agent 会话可用。
- Claude、Codex 和 CodeBuddy 的网页 Model / Effort 走各自 CLI 菜单的会话内切换。CodeBuddy 的 Effort 会改全局默认（页面上有提示）。在终端直接操作时，以 CLI 菜单标注的会话内与全局保存方式为准。
- 打开 Model 时，Codex 通过本机 App Server、Claude 通过 Agent SDK、CodeBuddy 通过本机 ACP 读取模型。读取失败可关闭菜单后重试。Codex 的 Effort 也自动读取，Claude 和 CodeBuddy 的 Effort 选项来自 `models.json`。
- Pi 的 Model 和 Effort 在网页弹窗里直接点选，选项和当前值来自该会话的原生菜单；取消会退出菜单。切换只对当前会话生效，不改默认值。有草稿或 Pi 正在工作时，不会把切换命令发进对话。
- 「运行中 / 空闲」优先用 CLI 自己的记录：Claude 读会话登记文件 `~/.claude/sessions/<pid>.json` 的状态（等你确认或打开菜单时算空闲），Codex 读当前对话记录里这一轮的开始与完成/中断事件。读不到时（旧版本、CodeBuddy、Pi）看屏幕：Pi 看输入框边框上的转圈，CodeBuddy 和 Claude 看输入框上方带计时的状态行（下面挂着提示或待办也算），Codex 看「Working (时长 •」一行；窄窗格把「esc to interrupt」截掉也能认出。CodeBuddy 被打断时对话记录不写结束标记，所以不用它的记录判断。
- 未读蓝点只根据各 Agent 当前对话的完成记录显示；思考、工具输出、终端重绘和流式生成不会触发。打开对话后清除蓝点。识别不到该窗格的对话记录时不猜测。通过网页新建的 Pi 会话用 `pi-session-status.ts` 关联其记录。
- 对话顶部副行只显示模型和推理档位，从各自终端读取，终端里切换也会同步。Pi、Codex 和新版 CodeBuddy 读底部状态行；Claude 和已有 CodeBuddy 会话读欢迎区及切换确认，Claude 的会话切换确认优先于重绘的欢迎信息。Claude 历史不完整且缺少切换确认、或可读输出中缺少状态信息时留空，不把启动默认值或网页旧记录当作当前值。
- New：Claude、CodeBuddy 发 `/clear`，Codex、Pi 发 `/new`。输入框里有没发出的草稿时拒绝执行，避免命令被拼到草稿后面发出去。
- Pi 的「额度」在 New 旁。当前模型的提供方是 `openai-codex` 时，显示本机 Codex CLI 登录账号的剩余额度；不确认 Pi 与 Codex 是否登录同一账号。其他或未知提供方不查 Codex。
- 「按键」一排是方向键、Enter、Esc、Tab 等，用来操作 CLI 弹出的菜单。完整终端操作仍用终端。
- 「宽屏」（字号 −/+ 旁）：CLI 按 tmux 窗格宽度自己断行，所以加宽文字要加宽窗格。点一下按当前窗口和字号能放下的列数加宽（最多 120 列），按钮随即变成「窄屏」，点它回到手机宽度（46 列，Pi 100 列）。宽度属于 tmux 会话，手机打开同一会话也是宽的，在手机上点「窄屏」即可。终端连着会话时宽度跟随终端，按钮会提示先断开；调整后终端连上照常跟随终端的屏幕。
  Codex、CodeBuddy、Pi 会按新宽度重印整段对话。Claude 不会：之前的内容保持原宽度，而且每切一次，靠近底部的内容会在记录里多出一份（从几行到最后一整轮不等），所以 Claude 会话别来回切。清掉回滚能去重，但会丢掉屏幕以上的全部内容；先把窗口临时拉高再切也试过，仍会重复，所以都没用。

## 没做的

- 不渲染 ANSI 颜色、不做逐字流式；不做完整终端。
- 网页新建的会话在没有终端连接时宽 46 列（适合手机，可用「宽屏」加宽）；终端连上后会变成终端的宽度，属正常。
