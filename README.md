# Agent Delegation

一个面向本机 Agent CLI 的通用任务委派层。它让 Hermes、Claude Code、Codex、Kimi、zCode、OpenCode，以及后续注册的 ACP CLI，互相委派有意义的研究、分析、写作、运营和 coding 使命。

核心原则是“委派使命，不规定解法”：worker 在用户已经授权的范围内拥有解释、分解、探索、工具选择、策略和表达的自主权；wrapper 不会把 Prompt 中的任务边界误译成网络、Shell 或搜索禁用。它只机械约束调用链、预算、显式 capability mode 和 receipts。它不是新的“总任务数据库”，也不是 coding-only orchestrator。

```text
Any host CLI
  -> portable agent-delegation Skill
  -> deterministic agent-delegate boundary
  -> installed ACPX transport
  -> reviewed target ACP server
```

## 为什么是 Skill + wrapper + ACPX

- **Agent Skill**：告诉不同模型何时委派、怎样表达 mission envelope，以及如何携带已有授权而不创造新授权。
- **`agent-delegate`**：记录调用来源，执行任务预算与显式权限模式，并保留流式收据和完整内容块。
- **ACPX**：负责 ACP 初始化、独立或持久会话、prompt、取消、结构化事件和退出码。

Skill 说明任务语义；wrapper 负责来源、预算和结果；会话生命周期复用 ACPX 原生能力。

## 当前目标

| Target | Reviewed ACP argv source |
|---|---|
| Hermes | Existing `hermes acp` |
| Claude Code | Installed `claude-agent-acp` + local `claude` via `CLAUDE_CODE_EXECUTABLE` |
| Codex | Installed `codex-acp` + local `codex` via `CODEX_PATH` |
| Kimi | Existing `kimi acp` |
| zCode | Existing Ultra-pinned `zcode-acp` bridge |
| OpenCode | Existing `opencode acp` |

Managed targets keep their normal tools and plugin surfaces. ZCode's adapter retains `--no-browser` only to prevent unattended interactive OAuth/device login; it does not disable ordinary web or network tools.

仓库 lock 保留以下开发/测试快照；用户安装器独立管理运行时，不会在 Skill 更新时把依赖装回这些版本：

| Package | Version | npm integrity |
|---|---:|---|
| `acpx` | `0.13.2` | `sha512-4hOLEo2kE/nCrPr50StbzU3G1WvzHkmKE/r3vxFAIr6GRI3VSmSRH62XCtnDpVcQNpBM8fVPAeTj39ewVhJwdQ==` |
| `@agentclientprotocol/claude-agent-acp` | `0.75.1` | `sha512-Un6I4BRkhpCFS3I7kr5C/lkAm8Nc3VuGZU2YQ3xIpJAIxV94iWO0Q2CH2QABxMERpONRu4Le6XC9V+5PImQZ2A==` |
| `@agentclientprotocol/codex-acp` | `1.10.0` | `sha512-b4dDCPkH/GgHRb3JelXz4QdNirdoTjO3yYM1ImUJMxVXxgZLZMguEXNhZOH2G755UKzf6ZypiiC7UHe3fKgp2Q==` |

首次安装和显式运行时升级使用 `npm install --save-exact --ignore-scripts`，记录实际版本与 lock hash。普通 Skill 更新保留已有运行时；正常委派不会回退到 `npx -y` 动态下载。

## 安装模型

共同核心是 portable Agent Skill。Codex plugin 是 Codex 的原生分发包装，不是其他宿主的共同依赖。

### 方案 A：统一用户级安装

```bash
python3 scripts/install_user.py install
python3 scripts/install_user.py doctor
```

Skill locations:

- Hermes: `~/.hermes/skills/agent-delegation`
- Claude Code: `~/.claude/skills/agent-delegation`
- Codex portable discovery: `~/.agents/skills/agent-delegation`
- Kimi: `~/.kimi-code/skills/agent-delegation`（设置 `KIMI_CODE_HOME` 时使用该目录下的 `skills`）
- zCode: `~/.zcode/skills/agent-delegation`
- OpenCode: `~/.config/opencode/skills/agent-delegation`

### 方案 B：Codex 使用原生 plugin（推荐的 Codex 分发方式）

先只给另外五个宿主安装 Skill，同时安装共享 runtime：

```bash
python3 scripts/install_user.py install \
  --hosts hermes,claude,kimi,zcode,opencode \
  --targets hermes,claude,codex,kimi,zcode,opencode
```

从本地 checkout 安装 Codex plugin：

```bash
codex plugin marketplace add /absolute/path/to/agent-delegate-skill
codex plugin add agent-delegation@rocky-agent-delegation
```

发布后也可以用 Git marketplace：

```bash
codex plugin marketplace add rocky2431/agent-delegate-skill --ref main
codex plugin add agent-delegation@rocky-agent-delegation
```

不要同时安装 Codex portable copy 和同名 Codex plugin，以免重复发现。

`task-state-with-files` 是独立的 durable task-state 基础 Skill，应继续单独安装；本仓库不复制或替换它。

## 使用

查看可用目标；遇到故障时再用 `doctor --to <target> --json` 诊断：

```bash
agent-delegate list --json
```

从 Hermes 委派一个研究使命给 Kimi：

```bash
agent-delegate submit \
  --to kimi \
  --caller hermes \
  --cwd /absolute/task/root \
  --task-file /absolute/delegation-envelope.md
```

保存返回的 `delegation_id`，用完整 ID 等待和取回结果：

```bash
agent-delegate wait --id <delegation_id> --timeout 30
```

`terminal: false` 表示任务还没有结果；`wait_timed_out: true` 只表示这次等待结束，任务继续运行。继续等待同一个 ID，也可以先做独立工作再回来查询；不要因等待超时而重复派发或取消。`status --id <delegation_id>` 可立即查询进度，完成后返回完整结果。命令退出码为 0 不等于任务完成，需要读取 JSON 的 `terminal` 和 `status`。

从已委派的 Agent 再委派时，wrapper 会注入 `AGENT_DELEGATION_CALLER`、`AGENT_DELEGATION_CHAIN` 和深度上限。同名 Agent 可以启动独立任务，调用链也允许重复名称；来源标识不要求同时注册为可调用目标。深度预算仍会生效。

需要多轮工作时使用命名会话，后续沿用同一 target、cwd 和 session：

```bash
agent-delegate submit --to codex --caller hermes --cwd /absolute/task/root \
  --session review --task 'Investigate the issue and identify missing information.'
```

取回该任务的结果后，如需继续这段对话，再提交一个新任务：

```bash
agent-delegate submit --to codex --caller hermes --cwd /absolute/task/root \
  --session review --task 'Here is the detail. Continue.'
```

同一会话的 wrapper 调用自动排队，不消耗执行预算。`submit --timeout` 控制执行时间，省略时沿用 registry；可选 `--queue-timeout` 单独限制排队，默认没有排队期限；`wait --timeout` 仅限制一次观察。不同独立任务可并行，省略 `--session` 使用一次性会话，不要仅因都派给 zCode 就共用会话名。`--model` 透传明确选择的模型；省略时保留目标默认设置，不会继承调用方的模型或完整对话。

确实需要停止某个任务时，按 ID 取消，然后继续查询该 ID 的最终状态：

```bash
agent-delegate cancel --id <delegation_id>
```

取消确认不代表任务已经停止。按 ID 取消排队任务不会取消正在执行的另一项；`incomplete` 或 `execution_state: unknown` 需要先检查原始事件和已产生的结果，再决定是否重试。同步 `run`、按会话取消和关闭是其他操作，见 [operations.md](plugins/agent-delegation/skills/agent-delegation/references/operations.md)。

运行时以 registry 为启动依据，项目或全局 alias 不会替换实际 executable。托管目标通过稳定入口执行本次选定的 argv，因此后续运行时升级不会改变会话命令。`doctor --to codex --json` 显示有效预算，并分别列出 CLI、适配器和 ACPX 的路径、版本；版本变化不构成启动门槛。

Codex/Claude 默认使用本机稳定 CLI 入口，不再使用适配器内置的旧 CLI。收据中的 `runtime_identity` 记录适配器启动时选定的程序。已有 warm 会话继续使用原进程，不能把当前本机版本误当作已运行进程的版本。首次迁移前的命名会话通过 ACPX 本地索引继续原 scope；没有启动记录的旧会话明确标记为未核实。

启动消息在 stderr 给出 receipt 路径。每次真实运行把以下私有收据写入 `~/.local/state/agent-delegation/runs/`：

- `request.json`：边界、hash、启动 argv、会话与模型选择；不保存原始 task 文本；
- `events.ndjson`：运行中持续落盘的 ACPX 事件，日志中的读取原文默认被抑制；
- `stderr.log`：运行中持续落盘的诊断；
- `state.json`：任务 ID、当前阶段和计时；`worker.log` 保留异步 wrapper 诊断；
- `result.json`：传输终态、stop reason、文本和原始内容块、会话身份、结构化错误和耗时。

`status`、`wait` 和按 ID 取消共用原任务的收据。结果分别记录排队与执行耗时，超时会标记 `queue`、`setup` 或 `execution` 阶段。`submit` 临时用私有 `launch.json` 传递含 mission 的启动计划，worker 读取后删除。

wrapper 默认不限制 task 或结果字符数。可选正整数 `max_task_chars` 限制输入，`max_result_chars` 只限制便于阅读的 `assistant_text` 字段；原始内容块和事件仍完整保留。配置在启动前校验，超时或取消保留已产生的部分结果。

状态区分正常结束、取消、超时、权限拒绝、会话不存在、拒答、未完成和错误。`rpc_errors` 保留 method/code/data；`tool_errors` 标识普通工具错误；兼容字段 `protocol_errors` 排除已识别的普通客户端操作。读取不存在的可选文件不会自动把正常 `end_turn` 判为失败，正常结束也不等于业务结果已经通过验收。

## 权限边界

- 委派可以携带用户已经授予的权限，但不能创造新权限；不要仅因为换了 Agent 就重复索要同一授权。
- capability 不等于 authority。普通委派默认 `approve-all` 并提供 Terminal，让目标保留网络、搜索、插件和工具选择；Prompt 中明确 goal、requirements、已有授权和 commit gates。`--authorization-note` 只是可选 receipt 信息，不是启动条件，也不创造授权。
- `approve-reads`、`deny-all` 与 `--no-terminal` 是有意缩减能力时才使用的显式模式。例如需要限制时同时传 `--permissions approve-reads --no-terminal`；不要因为任务文字写了“只读”就顺手关闭 Agent 可能需要的 Shell 或网络。
- ACPX 的 permission mode 无法理解任意命令的语义，也不要增加字符串 allowlist 假装能够判断。显式受限模式导致 permission failure 时，应把它作为真实 blocked 结果处理。
- 发送、发布、部署、购买、交易、身份/权限变更、删除或凭据操作等 commit effect，只有在用户明确授权该具体动作和范围时才能随委派传递；否则只准备，不提交。
- worker 拥有解法与探索自主权，但不能批准自己的副作用。`success` 表示收到正常终态；仅有底层 exit `0` 而缺少终态时仍为 `incomplete`。caller 按风险验证关键事实，不默认重做 worker 的工作。
- `cwd`、Prompt、ACPX mode 和 authorization note 都不是 OS 沙箱；如果目标能触达高风险外部系统或不可逆效果，必须使用真实宿主隔离或 tool-specific policy，或不把该能力交给本次委派。
- 新安装的缺省 timeout 与最大值为 7200 秒，已有 registry 配置会保留；实际值以 `doctor` 为准。例如既有 43200 秒配置仍为 12 小时。`--timeout` 可在配置上限内选择，`--max-depth` 可降低继承的深度预算。

## 增加其他 ACP CLI

先安装并审查确切 executable，再注册结构化 argv：

```bash
agent-delegate register \
  --name example \
  --argv-json '["/absolute/path/example", "acp"]' \
  --observed-version '1.2.3' \
  --provenance 'official package example@1.2.3'

agent-delegate doctor --json
```

注册会同时更新 wrapper registry 与 ACPX `agents` map，并先创建私有备份。新目标必须先跑一次无写入 smoke。

## 更新与移除

更新 reviewed checkout 后重新运行 installer。普通更新不会运行 npm 或覆盖已有运行时目录。`--hosts kimi` 只安装该宿主 Skill 并配置 Kimi 目标，不依赖其他五个 CLI。可用 `--targets` 分别选择要配置的 ACP 目标，`--hosts none --targets none` 只安装共享 runtime。未选中的既有目标、自定义预算和其他 ACPX 配置会保留；被管理的文件有备份。

需要升级 ACPX 和适配器时，显式执行：

```bash
python3 scripts/install_user.py install --update-runtime
```

新版本装入 `~/.local/share/agent-delegation/runtimes/` 下的新目录，成功后切换配置；失败保持原运行时。旧目录保留，供在途进程、旧会话和回退使用。显式升级也会更新已托管的 Codex/Claude 适配器目标，即使本次未选择其 Skill 宿主。版本诊断和回退方法见 [operations.md](plugins/agent-delegation/skills/agent-delegation/references/operations.md)。

```bash
python3 scripts/install_user.py uninstall
python3 scripts/install_user.py uninstall --remove-runtime
```

只有带本包 managed marker 的 Skill/runtime 才会被移除；备份位置会输出到终端。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" \
  plugins/agent-delegation/skills/agent-delegation
```

已有 ACPX 时，可加跑真实传输回归。测试使用隔离的临时配置和本地 ACP 夹具，不调用模型或读取用户会话；覆盖旧会话迁移、升级后的新进程和已有 warm 会话：

```bash
AGENT_DELEGATION_TEST_ACPX=/absolute/path/to/runtime/node_modules/.bin/acpx \
  python3 -m unittest discover -s tests -p test_acpx_transport.py -v
```

Kimi 安装路径按原生 Kimi Code 0.41.0 核对；旧 Python `kimi-cli` 的 `~/.kimi` 目录不由此安装器迁移或删除。委派使用 `kimi acp`，本 Skill 不安装恢复 Hook。模型 API 兼容不代表客户端 Hook 行为相同。参见 [Kimi Skill 目录](https://www.kimi.com/code/docs/kimi-code-cli/customization/skills.html)。
