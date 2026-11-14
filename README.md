# Agent Chat

在手机上用的 Agent 聊天网页（PWA）。它不自己跑 Agent，只是这台 Mac 上 tmux 会话的一个窗口：
网页和终端（比如 Termius）操作的是同一个 tmux 会话，关掉网页不影响 Agent。
支持 Claude Code、Codex、CodeBuddy 三个命令行 Agent，也能开一个普通 Shell 会话。

> **先看清楚再装**：登录网页的人可以在这台 Mac 上执行任意命令（Shell 会话就是远程终端），
> Agent 默认以免确认模式启动（Claude `bypassPermissions`、Codex `danger-full-access`、CodeBuddy `-y`）。
> 服务只接受本机和 Tailscale 网络里的访问（局域网和公网的请求一律拒绝），不要用别的方式把它暴露到公网。

## 安装

需要：macOS、[Homebrew](https://brew.sh)、[Tailscale](https://tailscale.com)（Mac 和手机登录同一个账号），以及至少一个已登录好的 Agent 命令行（`claude` / `codex` / `codebuddy`）。

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
- 新建：Claude / Codex / CodeBuddy，或「Zsh」（只建 tmux，在 ~ 开一个普通 Shell 会话、不启动 Agent，输入框里发的文字会作为命令在 Shell 里执行）；工作目录 `~`，tmux 会话名取昵称（空格、点、冒号换成 -；重名会拒绝），不填昵称时为 `web-<agent>-<随机>`。
  Claude、CodeBuddy 在 `~` 下启动时的"是否信任此目录"提示会自动选信任（否则第一条消息的回车会选中"退出"）。
- Agent 的启动命令写在 `server.py` 的 `AGENTS` 里，用 `zsh -ic` 启动（会读你的 `~/.zshrc`）。你的机器需要别的启动方式（比如经 node 版本管理器启动，或想去掉免确认参数）时，不用改代码，在 `~/.agent-chat/agents.json` 里按 Agent 覆盖，然后 `agentchat restart`：

  ```json
  {"codex": {"command": "fnm exec --using=default codex --no-alt-screen"}}
  ```

  Claude 和 Codex 要保留"不用备用屏幕"的设置（`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`、`--no-alt-screen`），否则网页只能看到最后一屏；Codex 建议保留 `-c check_for_update_on_startup=false`，否则出新版时第一条消息的回车会触发自更新并退出。
- 输出每秒读一次 `tmux capture-pane`（纯文本，最近 3000 行）；输入用带括号粘贴的 `paste-buffer` 再回车，多行消息不会被拆开。会话列表每张卡片下显示该会话里你最近发的一条消息（最多两行，斜杠命令不算）；手机上在对话页向右滑返回列表。电脑上回车发送，Ctrl+回车（或 Cmd/Shift+回车）换行；手机上回车是换行，点「发送」发送。
- 同一会话同一时刻只处理一个写操作；每条消息带编号，断线重试不会重复发送。
- 会话里跑的是 Shell 而不是 Agent 时，输入框发的文字直接敲进 Shell 并回车执行。切模型、切档位、新对话只对 Agent 会话可用。
- Model / Effort 走 CLI 自己的菜单，并且**只对当前会话生效**。直接用 `/model 名称`、`/effort 档位`、菜单里的数字键或回车
  会改掉 CLI 的全局默认，影响本机其他会话。例外：CodeBuddy 的 Effort 没有"仅本会话"，选了就是全局默认（页面上有提示）。
- 可选项在 `models.json`，是 2026-11-14 按当时的 CLI 菜单录的；CLI 升级后菜单变了，或你的账号能用的模型不同，就照自己的菜单改这个文件（`menu` 是菜单行开头的文字）。
- New：Claude、CodeBuddy 发 `/clear`，Codex 发 `/new`。输入框里有没发出的草稿时拒绝执行，避免命令被拼到草稿后面发出去。
- 「按键」一排是方向键、Enter、Esc、Tab 等，用来操作 CLI 弹出的菜单。完整终端操作仍用终端。

## 没做的

- 不渲染 ANSI 颜色、不做逐字流式；不做完整终端。
- 网页新建的会话在没有终端连接时宽 46 列（适合手机）；终端连上后会变成终端的宽度，属正常。
