# Agent Delegation

[English](README.md) · [简体中文](README.zh-CN.md)

Agent Delegation 让一个本地编码 Agent 把任务交给另一个 Agent。它支持
Hermes、Claude Code、Codex、Kimi Code、zCode、OpenCode，以及另外注册的
ACP Agent。

工作者收到目标、必要上下文、已有授权和完成条件后，自行决定怎样调查、使用
哪些工具以及怎样交付结果。Python wrapper 为每项任务分配独立身份，分别管理
几种时间预算，并保留可检查的收据。ACPX 负责原生 Agent 会话和传输。

当另一个 Agent 有更合适的工具、相关上下文或独立视角，或者任务需要在调用方
停止等待后继续运行时，可以使用 Agent Delegation。当前 Agent 能直接完成的小
任务无需委派。

版本：0.5.0。

- [安装与开始使用](#安装与开始使用)
- [第一次委派](#第一次委派)
- [委派如何运行](#委派如何运行)
- [会话、并行与时间](#会话并行与时间)
- [授权与副作用](#授权与副作用)
- [收据与恢复](#收据与恢复)
- [当前限制](#当前限制)
- [更新与移除](#更新与移除)
- [文档](#文档)
- [开发](#开发)

## 安装与开始使用

安装需要 Python 3.11 或更新版本、Node.js 与 npm，以及至少一个受支持的 Agent CLI。安装器
创建共享 runtime，注册已审查的目标，并把 portable Skill 复制到选定宿主的
用户目录。

克隆仓库并进入目录：

```bash
git clone https://github.com/rocky2431/agent-delegate-skill.git
cd agent-delegate-skill
```

如果六个受支持的 CLI 都已安装，可以一次安装并检查全部组件：

```bash
python3 scripts/install_user.py install
python3 scripts/install_user.py doctor
```

也可以分别指定需要安装 Skill 的宿主和需要注册的目标：

```bash
python3 scripts/install_user.py install \
  --hosts hermes,kimi \
  --targets hermes,kimi
```

Portable Skill 使用各宿主的原生用户目录：

| 宿主 | Skill 目录 |
|---|---|
| Hermes | `~/.hermes/skills/agent-delegation` |
| Claude Code | `~/.claude/skills/agent-delegation` |
| Codex portable discovery | `~/.agents/skills/agent-delegation` |
| Kimi Code | `$KIMI_CODE_HOME/skills/agent-delegation`（默认 `~/.kimi-code/skills/agent-delegation`） |
| zCode | `~/.zcode/skills/agent-delegation` |
| OpenCode | `~/.config/opencode/skills/agent-delegation` |

### Codex plugin

Codex 推荐通过原生 plugin 分发 Skill。先安装共享 runtime 和 Codex 目标，不
再额外安装 Codex portable copy：

```bash
python3 scripts/install_user.py install --hosts none --targets codex
codex plugin marketplace add rocky2431/agent-delegate-skill --ref main
codex plugin add agent-delegation@rocky-agent-delegation
```

从本地 checkout 安装时，把 marketplace 命令换成：

```bash
codex plugin marketplace add /absolute/path/to/agent-delegate-skill
```

不要同时启用 Codex portable copy 和同名 plugin，否则 Codex 可能发现两份
Skill。 `task-state-with-files` 是另一项独立 Skill，需要单独安装。

### 已审查目标

| 目标 | ACP 入口 |
|---|---|
| Hermes | `hermes acp` |
| Claude Code | `claude-agent-acp`，绑定本机 `claude` executable |
| Codex | `codex-acp`，绑定本机 `codex` executable |
| Kimi Code | `kimi acp` |
| zCode | 已安装的 `zcode-acp` bridge |
| OpenCode | `opencode acp` |

托管目标继续使用自己的模型、认证、工具和 plugin 配置。zCode 入口使用
`--no-browser`，避免无人值守运行时出现 OAuth 或设备登录界面。这个参数
不会关闭普通网络或 Web 工具。

查看当前安装中可用的目标：

```bash
agent-delegate list --json
```

## 第一次委派

先写一份 mission 文件。内容可以很短，但要说明工作者负责什么，以及什么证据
可以证明任务完成：

```text
Investigate why the import loses the final row. Reproduce it and fix the shared
cause in this checkout. Local edits and tests are authorized. Publishing is not.
Return the cause, the changed files, and the validation result.
```

提交一次：

```bash
agent-delegate submit \
  --to codex \
  --cwd /absolute/path/to/project \
  --task-file /absolute/path/to/mission.md
```

保存返回的 `delegation_id`，再等待这项任务：

```bash
agent-delegate wait --id <delegation_id> --timeout 30
```

每次返回都要读取 JSON。命令退出码为零，不代表 mission 已经完成。

| 返回状态 | 后续处理 |
|---|---|
| `terminal: false` | 保留 ID，稍后继续等待。任务仍在启动、排队或运行。 |
| `wait_timed_out: true` | 只有本次等待结束，任务仍在继续。不要重复派发。 |
| `terminal: true`，`status: success` | 读取文本和内容块，再按风险核对重要结论。 |
| `terminal: true`，其他状态 | 查看原因、部分输出和收据，再决定是否重试。 |

需要立即查看当前状态时使用：

```bash
agent-delegate status --id <delegation_id>
```

较复杂的交接可以参考
[mission context](plugins/agent-delegation/skills/agent-delegation/references/task-packet.md)。
除非用户明确指定，建议的文件和方法只是线索，不是必须采用的步骤。

## 委派如何运行

```text
Host agent
  -> portable Agent Delegation Skill
  -> agent-delegate task boundary
  -> installed ACPX runtime
  -> target ACP agent
```

1. 宿主选择合适的已注册目标，并整理 mission。
2. `agent-delegate` 检查目标、工作目录、预算和显式 capability mode，在工作
   开始前创建 `delegation_id`。
3. ACPX 启动一次性 Agent，或者继续一个命名原生会话。除非调用方明确指定，
   目标继续使用自己的模型和工具。
4. 运行中的事件和诊断会持续写入文件。最终结果保留 Agent 文本、原始内容块、
   stop reason、错误和 runtime identity。

Skill 说明 mission，wrapper 负责任务身份、边界和证据，ACPX 管理 Agent 会话
生命周期。Agent Delegation 不是工作流引擎，也不是共享任务数据库。

`success` 表示 ACP 回合正常结束，不表示业务结果或验收标准已经通过。调用方
仍需按风险核对重要结论。

## 会话、并行与时间

Delegation ID 对应一项提交的任务。Session name 用于在多项任务之间延续上下文。
只有后续任务确实需要继续同一段 Agent 对话时，才复用相同的目标、工作目录和
session name：

```bash
agent-delegate submit --to codex --cwd /absolute/path/to/project \
  --session review --task 'Investigate the failure and report what is missing.'

agent-delegate submit --to codex --cwd /absolute/path/to/project \
  --session review --task 'Use this new detail and continue the investigation.'
```

同一命名会话中的任务依次运行。不同会话和未指定 `--session` 的任务可以独立
运行，实际并发仍受目标自身能力影响。工作者不会自动继承调用方的完整对话或
模型。Mission 需要带上必要上下文；只有确实需要指定目标模型时才传 `--model`。

三个 timeout 选项管理不同的计时：

| 选项 | 含义 |
|---|---|
| `submit --timeout N` | 进入执行并完成 session setup 后开始计算的执行预算。 |
| `submit --queue-timeout N` | 等待命名会话轮次的可选期限。 |
| `wait --timeout N` | 本次观察等待多久。超时不会停止任务。 |

新安装的执行时间上限是 7200 秒，委派深度上限是 4。已有 registry 配置会保留。
以下命令显示实际生效值：

```bash
agent-delegate doctor --to codex --json
```

需要停止一项任务时，按完整 ID 取消，并继续观察直到出现最终状态：

```bash
agent-delegate cancel --id <delegation_id>
```

收到取消确认，不代表模型回合或它启动的子进程已经停止。命名会话的取消与关闭
属于 operator control，详见
[operations guide](plugins/agent-delegation/skills/agent-delegation/references/operations.md)。

## 授权与副作用

委派只能携带用户已经授予的权限，不能创造新的权限。工作转移到另一个 Agent
后，不需要为同一项范围内操作重复申请授权。

默认 transport mode 是 `approve-all`，并向目标提供 Terminal。这会保留目标
通常拥有的 Shell、网络、搜索和工具选择。拥有能力不等于获准产生副作用。
Mission 仍然限制任务范围；发布、部署、购买、交易、删除、凭据修改、身份或权限
修改等未授权副作用发生前，工作者必须暂停。

只有任务确实需要缩减 capability 时，才使用 `approve-reads`、`deny-all`
或 `--no-terminal`。ACPX 无法判断任意 Shell 命令的实际含义，因此受限模式
可能返回真实的权限拒绝。

工作目录、Prompt、permission mode 和 `--authorization-note` 都不是 OS
sandbox。能够触达敏感系统的任务，仍需使用对应宿主或工具的隔离与策略。

## 收据与恢复

每项异步提交或同步任务都会在
`~/.local/state/agent-delegation/runs/` 下写入私有收据：

| 文件 | 内容 |
|---|---|
| `request.json` | 任务身份、目标、边界、hash、会话选择和启动命令，不保存 mission 原文。 |
| `events.ndjson` | 流式 ACPX 事件，其中不记录 read-file body。 |
| `stderr.log` | 任务运行期间的 runtime 诊断。 |
| `state.json` | 当前阶段、wrapper 所有权和计时。 |
| `worker.log` | 异步任务的 background wrapper 诊断。 |
| `result.json` | 最终状态、文本、内容块、错误、计时和会话身份。 |
| `runtime.json` | 会话实际观察到的 CLI、adapter 和 ACPX identity。 |

`status`、`wait` 和按任务 ID 取消都会复用原收据。如果 submit 响应丢失，
stderr 中的启动消息和 `request.json` 仍保留任务 ID。

如果 wrapper 在生成最终结果前失去 ownership，任务状态会变成 `incomplete`，
`execution_state` 为 `unknown`。这只表示 wrapper 无法继续观察，不能证明
原生任务已经停止。重试前应检查事件、部分输出、原生会话和可能已经发生的副作用。

## 当前限制

- Agent Delegation 只传递显式提供的上下文，不会把调用方的完整对话、环境中的
  secrets 或模型选择复制给工作者。
- 原生 turn cancellation 不保证 Terminal 子进程已经清理。需要清理时，应查明
  并停止具体原生任务。
- `cwd` 和 Prompt 用于协调任务，不提供进程隔离。
- Runtime 记录传输结果和结构化错误。语义验收仍由调用方或独立
  verifier 负责。
- OpenCode 已注册，但最近一次实际检查未能证明完整往返。它的原生 ACP 路径
  发生 timeout，另一次运行返回认证错误。
- Kimi Code 使用 `$KIMI_CODE_HOME/skills`，默认是 `~/.kimi-code/skills`。本安装器不会迁移
  或删除旧 Python `kimi-cli` 的 `~/.kimi`，也不安装 recovery Hook。

## 更新与移除

普通更新只刷新受管理的 Skill 和 wrapper entry。当前 runtime、自定义预算、
未选目标以及 warm session 使用的旧 runtime generation 都会保留：

```bash
python3 scripts/install_user.py install
```

Runtime 依赖只在显式升级时改变：

```bash
python3 scripts/install_user.py install --update-runtime
```

安装器在 `~/.local/share/agent-delegation/runtimes/` 下创建新 runtime，记录
实际安装版本与 lock hash，再切换 registry。升级失败时继续使用原 runtime。
受管理文件在替换前会备份。

可以只移除已安装的 Skill 并保留共享 runtime，也可以同时移除两者：

```bash
python3 scripts/install_user.py uninstall
python3 scripts/install_user.py uninstall --remove-runtime
```

安装器只移除带有本包 managed marker 的文件。

## 文档

- [Skill 指令](plugins/agent-delegation/skills/agent-delegation/SKILL.md)：Agent
  实际加载的委派流程。
- [Mission context](plugins/agent-delegation/skills/agent-delegation/references/task-packet.md)：
  复杂任务交接应包含的内容。
- [Operations guide](plugins/agent-delegation/skills/agent-delegation/references/operations.md)：
  同步运行、会话控制、收据、诊断、目标注册和回退。
- [Kimi Code Skill discovery](https://www.kimi.com/code/docs/kimi-code-cli/customization/skills.html)：
  Kimi Code 的原生 Skill 目录与加载方式。

## 开发

仓库在 `runtime/package-lock.json` 中保存可复现的开发快照：ACPX 0.13.2、
`@agentclientprotocol/claude-agent-acp` 0.75.1 和
`@agentclientprotocol/codex-acp` 1.10.0。用户 runtime 独立升级，不会在普通
Skill 更新时自动回退到这份快照。

在仓库根目录运行标准检查：

```bash
python3 -m unittest discover -s tests -v
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" \
  plugins/agent-delegation/skills/agent-delegation
```

如果已安装 ACPX runtime，可以另外运行 transport regression：

```bash
AGENT_DELEGATION_TEST_ACPX=/absolute/path/to/runtime/node_modules/.bin/acpx \
  python3 -m unittest discover -s tests -p test_acpx_transport.py -v
```

测试使用隔离的临时配置和本地 ACP fixture，不调用模型，也不读取用户会话。

## Pi 支持

```bash
python3 scripts/install_user.py install --hosts pi --targets pi --update-runtime
```

Pi 可作为委派目标，也可通过 `/skill:agent-delegation` 发起委派，顶层调用传
`--caller pi`。安装路径遵循 `PI_CODING_AGENT_DIR`，默认 `~/.pi/agent/skills`。
显式更新会在新的运行时目录安装 `pi-acp` 0.0.33，并保留旧运行时；普通 Skill
更新不会升级运行时。Pi 沿用自己的模型、登录、扩展与会话配置。

该适配器的工具在本地执行，无法兑现 ACP 的 `approve-reads`、`deny-all` 和
`--no-terminal`，因此这些组合会被明确拒绝。回执会核对本次任务对应的原生 Pi
会话与停止原因；原生证据缺失记为 `incomplete`，不会把模型错误当成成功。

原生 Pi 包位于 `plugins/agent-delegation`，只加载 Skill，不替代 CLI 安装。
避免与 `~/.agents/skills` 中同名副本重复加载。
