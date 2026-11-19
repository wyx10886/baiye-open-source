# Local Collaboration Memory · 本地协作记忆

**Skill version / 技能版本：1.1.0** · **Runtime baseline / 运行源码基线：1.7.2** · **2026-11-19** · MIT

## 中文介绍

让跨聊天协作保留可追溯的依据：记录明确的决定、核验成果与待续，按项目检索，并维护可移交的项目总结。所有正式记录带来源、范围、状态和修订历史，候选推断与已确认事实分开，虚构设定默认不参与检索。

本版本从运行源码 1.7.2 抽取通用功能，沿用独立公开 Skill 版本线，从 1.0.1 升级为 1.1.0。它包含 Python 源码、合成测试和说明；首次初始化是空白数据库，不附带任何人的记忆、人设、账户配置、真实会话、私有工作记录或私有 Git 历史。

### 能力

- 有来源的记忆、版本纠错、候选隔离、中英文词法检索与项目范围控制。
- 项目工作总结和问题解决记录，支持人工补充与修订冲突保护。
- CLI、stdio MCP 和仅回环地址可用的本地管理网页，静态资源随包提供。
- 真实会话身份核验、固定编号及独立日常入口。可选编号回收会完整核对活动/归档主聊天、工作栈文件和目录，保留旧世代与身份记录。
- 可选习惯学习：只处理当前可信用户输入的必要短句；规则或凭据约束的语义补充产生候选，明确确认后才成为正式偏好。
- 可选主动消息准备：暂停、忙碌、入口匹配、近期互动和同日次数门禁；准备记录不等于消息已发送或送达。
- 处理记录闲置清理先展示具体文件清单，用户明确确认后才可恢复式回收。

### 快速开始

Python 3.11+，运行时仅使用标准库。将完整文件夹放到技能目录（例如 `~/.codex/skills/local-collaboration-memory/`），或从稳定工作目录调用。在本文件所在目录运行：

```sh
python -B scripts/entry.py --version
python -B scripts/entry.py --home ~/.local-collaboration-memory init
python -B scripts/entry.py --home ~/.local-collaboration-memory context --markdown
python -B scripts/entry.py --home ~/.local-collaboration-memory serve --open
```

私有 home 放在代码和公开仓库之外。调用 `$local-collaboration-memory` 使用技能。初始化不会接入宿主、导入旧资料或建立定时任务。完整宿主接入会修改 hooks、notify 和配置，应在用户要求接入后执行。

### 默认与隐私

全新安装默认关闭习惯采集及候选复用、主动准备、标题同步和编号回收；保留已有明确配置。需要时分别启用，不能把安装或候选推断当作授权。编号回收启用后，允许未来新主聊天按完整扫描自动归档无人占用登记并复用最小空号；手动批量释放仍需确认当前扫描清单。它不会删除宿主聊天或工作文件。

运行时没有内置遥测或远程存储。选入云端助手上下文或交给当前模型归纳的内容会经宿主进入该模型。习惯过滤是启发式，不能保证剔除所有个人信息；启用后需要查看、纠正或遗忘证据。完整备份需停写后保护整个私有 home，普通记忆备份不包含所有独立数据库。

阅读 [技能指引](SKILL.md)、[安装](references/setup.md)、[操作](references/operations.md)、[可选功能](references/optional-features.md)、[隐私边界](references/privacy.md)和[版本记录](CHANGELOG.md)。

## English introduction

Keep collaboration grounded across chats: retain explicit decisions, verified results and next steps, retrieve them within a project, and maintain portable project handoffs. Records carry provenance, scope, status and revision history. Inferences remain separate from confirmed facts; fictional persona is excluded unless requested.

This release derives generic capabilities from runtime baseline 1.7.2 and advances the independent public Skill from 1.0.1 to 1.1.0. The distribution contains source, synthetic tests and instructions. Initialization starts empty, without anyone's memories, persona, account configuration, real sessions, private work records or private Git history.

### Capabilities

- Provenance-aware memory, traceable corrections, candidate separation, Chinese/English lexical retrieval and project isolation.
- Portable project summaries and issue histories, with manual notes and revision conflict protection.
- CLI, stdio MCP and a token-protected loopback web manager with bundled assets.
- Verified native session identity, stable codes and a separate daily-chat pointer. Optional code recycling checks active/archived main chats and workspace files/directories while retaining prior generations and identity history.
- Optional habit learning from minimal eligible excerpts of current verified human input. Rules or receipt-bound semantic observations produce candidates; explicit review is required for confirmation.
- Optional proactive preparation with pause, busy, target, recent-activity and daily-limit gates. A preparation record is not a send or delivery receipt.
- Review-first retention that requires explicit confirmation of a current concrete file list before recoverable recycling.

### Quick start and defaults

Use Python 3.11+; the runtime has no third-party dependencies. Copy the whole directory to a skill location or invoke it from a stable workspace, then run the commands above. Keep the private home outside the source and public repositories. Initialization neither configures the host nor imports prior data or installs schedules.

Fresh installations disable habit collection and reuse, proactive preparation, host title sync and session-code recycling. Existing explicit settings are preserved. Enabling recycling authorizes automatic reuse of completely verified vacant codes during future main-session registration; manual batch release separately confirms a current scan. It never deletes host chats or work files.

There is no built-in telemetry or remote storage. Content selected for a cloud assistant or semantic observation reaches that model through its host. Habit filters are heuristics, not a complete personal-data sanitizer. Inspect and correct evidence after opting in. A complete migration requires a quiescent copy of the entire private home, including independent databases.

See [SKILL.md](SKILL.md), [setup](references/setup.md), [operations](references/operations.md), [optional features](references/optional-features.md), [privacy](references/privacy.md) and [changelog](CHANGELOG.md).

## Verification · 验证

```sh
python -B scripts/audit_release.py --manifest release-manifest.json
cd scripts
python -B -m unittest discover -s tests -v
python -B evaluation.py
```

The manifest checks the exact distributed source; it is not a signature or a guarantee about future user data. Tests use isolated homes and synthetic records. Package installation is optional: `python -m pip install .` provides `local-memory`. No PyPI publication is implied.

发布清单核对的是本次分发源码，不是数字签名，也不保证未来写入的数据安全。测试使用隔离 home 和合成记录；可选安装 Python 包后使用 `local-memory`，本项目未据此发布 PyPI 包。
