# Agent Delegation

一个面向本机 Agent CLI 的通用任务委派层。它让 Hermes、Claude Code、Codex、Kimi、zCode、OpenCode，以及后续注册的 ACP CLI，互相委派有意义的研究、分析、写作、运营和 coding 使命。

核心原则是“委派使命，不规定解法”：worker 在用户已经授权的范围内拥有解释、分解、探索、工具选择、策略和表达的自主权；wrapper 不会把 Prompt 中的任务边界误译成网络、Shell 或搜索禁用。它只机械约束调用链、预算、显式 capability mode 和 receipts。它不是新的“总任务数据库”，也不是 coding-only orchestrator。

```text
Any host CLI
  -> portable agent-delegation Skill
  -> deterministic agent-delegate boundary
  -> pinned ACPX transport
  -> reviewed target ACP server
```

## 为什么是 Skill + wrapper + ACPX

- **Agent Skill**：告诉不同模型何时委派、怎样表达 mission envelope，以及如何携带已有授权而不创造新授权。
- **`agent-delegate`**：机械执行 cwd、timeout、permission mode、私有 receipt、调用链和循环保护。
- **ACPX**：负责 ACP 初始化、临时 session、prompt、取消、结构化事件和稳定退出码。

只装 ACPX 缺少共享语义和防循环边界；只装 Skill 又无法可靠执行这些机械约束。

## 当前目标

| Target | Reviewed ACP argv source |
|---|---|
| Hermes | Existing `hermes acp` |
| Claude Code | `@agentclientprotocol/claude-agent-acp@0.70.0` |
| Codex | `@agentclientprotocol/codex-acp@1.7.0` |
| Kimi | Existing `kimi acp` |
| zCode | Existing Ultra-pinned `zcode-acp` bridge |
| OpenCode | Existing `opencode acp` |

Managed targets keep their normal tools and plugin surfaces. ZCode's adapter retains `--no-browser` only to prevent unattended interactive OAuth/device login; it does not disable ordinary web or network tools.

The runtime lock pins:

| Package | Version | npm integrity |
|---|---:|---|
| `acpx` | `0.13.2` | `sha512-4hOLEo2kE/nCrPr50StbzU3G1WvzHkmKE/r3vxFAIr6GRI3VSmSRH62XCtnDpVcQNpBM8fVPAeTj39ewVhJwdQ==` |
| `@agentclientprotocol/claude-agent-acp` | `0.70.0` | `sha512-Psqj6fhV4pQ8IM480zpJ+xGiMMIqNLxlsTj5Mzn+T8KSURCVNJdl0ktcqLMjgHJC/QnOvDdDkFf3xTW9VIV9aQ==` |
| `@agentclientprotocol/codex-acp` | `1.7.0` | `sha512-+nUhAJyunx8Zc7r3jjLPoMPPUkkk02TmBIosln4l+ugRNUOdNQAMm6toZo7xb+mF1yM5zxJB83qvy/bPmOTaaw==` |

安装使用 `npm ci --ignore-scripts`，正常委派不会回退到 `npx -y` 动态下载。

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
- Kimi: `~/.kimi/skills/agent-delegation`
- zCode: `~/.zcode/skills/agent-delegation`
- OpenCode: `~/.config/opencode/skills/agent-delegation`

### 方案 B：Codex 使用原生 plugin（推荐的 Codex 分发方式）

先只给另外五个宿主安装 Skill，同时安装共享 runtime：

```bash
python3 scripts/install_user.py install \
  --hosts hermes,claude,kimi,zcode,opencode
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

查看和检查目标：

```bash
agent-delegate list --json
agent-delegate doctor --json
```

从 Hermes 委派一个研究使命给 Kimi：

```bash
agent-delegate run \
  --to kimi \
  --caller hermes \
  --cwd /absolute/task/root \
  --task-file /absolute/delegation-envelope.md
```

从已委派的 Agent 再委派时，wrapper 会注入 `AGENT_DELEGATION_CALLER`、`AGENT_DELEGATION_CHAIN` 和深度上限。直接自委派、回到链上已有 Agent、超过深度都会失败。

每次真实运行把以下私有收据写入 `~/.local/state/agent-delegation/runs/`：

- `request.json`：边界、hash、版本 argv；不保存原始 task 文本；
- `events.ndjson`：ACPX 结构化事件，读取内容默认被抑制；
- `stderr.log`；
- `result.json`：标准化状态、stop reason、最终文本和耗时。

wrapper 默认不限制 task 或标准化结果的字符数，也不会在标准化时裁掉 Agent 结论。只有 owner 明确需要 wrapper 级限制时，才在 `~/.config/agent-delegation/config.json` 中配置正整数 `max_task_chars` 或 `max_result_chars`；字段缺失表示不设字符上限。目标模型或传输层的真实上下文错误由对应层报告，并保留在结构化结果或 receipt 中。

## 权限边界

- 委派可以携带用户已经授予的权限，但不能创造新权限；不要仅因为换了 Agent 就重复索要同一授权。
- capability 不等于 authority。普通委派默认 `approve-all` 并提供 Terminal，让目标保留网络、搜索、插件和工具选择；Prompt 中明确 goal、requirements、已有授权和 commit gates。`--authorization-note` 只是可选 receipt 信息，不是启动条件，也不创造授权。
- `approve-reads`、`deny-all` 与 `--no-terminal` 是有意缩减能力时才使用的显式模式。例如需要限制时同时传 `--permissions approve-reads --no-terminal`；不要因为任务文字写了“只读”就顺手关闭 Agent 可能需要的 Shell 或网络。
- ACPX 的 permission mode 无法理解任意命令的语义，也不要增加字符串 allowlist 假装能够判断。显式受限模式导致 permission failure 时，应把它作为真实 blocked 结果处理。
- 发送、发布、部署、购买、交易、身份/权限变更、删除或凭据操作等 commit effect，只有在用户明确授权该具体动作和范围时才能随委派传递；否则只准备，不提交。
- worker 拥有解法与探索自主权，但不能批准自己的副作用。transport exit `0` 只证明 ACP turn 完成；caller 按风险验证关键事实，不默认重做 worker 的工作。
- `cwd`、Prompt、ACPX mode 和 authorization note 都不是 OS 沙箱；如果目标能触达高风险外部系统或不可逆效果，必须使用真实宿主隔离或 tool-specific policy，或不把该能力交给本次委派。
- 默认和最大单次 timeout 都是 7200 秒（2 小时）；只有显式传入更小的 `--timeout` 才会缩短。

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

更新 reviewed checkout 后重新运行 installer。它会保留未知的自定义 target 和 ACPX 其他配置，并备份被管理的文件。

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
