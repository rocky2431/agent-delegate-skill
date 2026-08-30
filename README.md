# Agent Delegation

一个面向本机 Agent CLI 的通用任务委派层。它让 Hermes、Claude Code、Codex、Kimi、zCode、OpenCode，以及后续注册的 ACP CLI，使用同一份任务协议相互委派研究、分析、写作、运营、coding 等有边界的任务。

它不是新的“总任务数据库”，也不是 coding-only orchestrator：发起方仍然拥有任务、审批和最终验收权。

```text
Any host CLI
  -> portable agent-delegation Skill
  -> deterministic agent-delegate boundary
  -> pinned ACPX transport
  -> reviewed target ACP server
```

## 为什么是 Skill + wrapper + ACPX

- **Agent Skill**：告诉不同模型何时委派、怎样写 task packet、哪些权限不能继承。
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
| OpenCode | Existing `opencode acp --pure` |

`--pure` keeps delegated OpenCode sessions free of unrelated external plugins. Native user Skills remain available.

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

从 Hermes 委派一个只读的一般任务给 Kimi：

```bash
agent-delegate run \
  --to kimi \
  --caller hermes \
  --cwd /absolute/task/root \
  --task-file /absolute/task-packet.md \
  --permissions approve-reads
```

从已委派的 Agent 再委派时，wrapper 会注入 `AGENT_DELEGATION_CALLER`、`AGENT_DELEGATION_CHAIN` 和深度上限。直接自委派、回到链上已有 Agent、超过深度都会失败。

每次真实运行把以下私有收据写入 `~/.local/state/agent-delegation/runs/`：

- `request.json`：边界、hash、版本 argv；不保存原始 task 文本；
- `events.ndjson`：ACPX 结构化事件，读取内容默认被抑制；
- `stderr.log`；
- `result.json`：标准化状态、stop reason、最终文本和耗时。

## 权限边界

- 默认 `approve-reads`，非交互写入升级 fail closed。
- `deny-all` 用于不需要工具的推理。
- `approve-all` 或 `--terminal` 必须带真实 `--authorization-note`。
- worker 不继承发送、发布、部署、购买、交易、身份/权限变更、删除或凭据操作的授权。
- transport exit `0` 只证明 ACP turn 完成；caller 仍需验收结果和证据。

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
