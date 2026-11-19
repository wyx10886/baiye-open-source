# Privacy and publication boundary

The distributed skill is code, synthetic tests and instructions. It contains no seeded memories, session identities, persona, machine configuration, activity logs or copied private Git history. A new home starts empty.

## What a deployment stores

- Explicit memory records include content, source, status, scope, dates and revision history in local SQLite. Markdown views and backups are additional private copies.
- Optional project handoffs write the user-selected project summary/issue files and their manual-notes sections in that project.
- Optional lifecycle hooks retrieve context and the completion notifier may save up to 1,200 characters of assistant milestone text as a candidate, with native session/turn identifiers as provenance. This is local collection. User messages are not automatically imported as a transcript. Inspect `capture_turn` before enabling this behavior for sensitive work.
- Optional session/title integration stores native identities, titles and binding history. Optional cleanup stores root paths, file metadata, proposals and recycled content.
- Before automatic numbering, optional lifecycle integration checks the exact native session ID against the newest local host index using a read-only SQLite connection. It reads only source, parent and rollout-location metadata for that ID. If needed, it reads at most the first 64 KiB of a matching `.jsonl` file under the host's `sessions` or `archived_sessions` directory, requiring the exact ID in both filename and first `session_meta` record. This identity check never reads later message records or writes the host database. This check runs even when title synchronization is disabled. Child agents are skipped; unknown identity stays pending. Its private diagnostic record retains only native ID, classification, evidence type and check time, without the raw metadata or rollout path.
- Opt-in habit collection stores up to six eligible 6–360-character excerpts from a current verified human submission, along with native IDs, source, time, scope and receipt metadata in `habits.sqlite3`. Receipts expire after 24 hours; evidence is retained until explicitly removed by edit, rejection, forgetting or clearing. Confirmation does not erase evidence. Repetition remains candidate material. The excerpt filters are heuristics, not a complete personal-data sanitizer. Semantic observations verify quote provenance, not the truth of an inferred preference. Collection and candidate reuse are separate controls, both off by default in this public version.
- Opt-in proactive preparation stores consent/pause settings, activity timestamps and preparation attempts in `proactive.sqlite3`. Disabled lifecycle integration adds no new activity observations. A claim records local preparation only; the runtime never sends a message or creates a schedule. Read-only management can initialize empty private storage. UTC defaults and explicit configuration replace any original deployment's timezone/schedule.
- Session occupancy inspection reads only selected identity/title/source/archival metadata from the latest supported host SQLite index, using a read-only connection. It checks configured workspace names, paths and filesystem metadata including hidden items, without reading work-file contents or host message bodies. Scan reports contain private paths and IDs. Opt-in recycling permits automatic verified vacant-code reuse during registration; explicit manual scan can read metadata even with recycling disabled. Generations and identity history remain in private storage.
- The local web manager uses a random token, validates Host/Origin and binds only to loopback. Do not expose it through a public proxy or include its URL/token in reports.

The runtime has no telemetry, external CDN or built-in remote storage request. Context supplied to a cloud-hosted assistant is transmitted to that assistant through the host. A preexisting notification command preserved by the optional installer is an external program with its own behavior. Title sync, when enabled, calls the local Codex app-server. These boundaries differ from claiming data can never leave a device.

## Safe sharing

Keep the private home outside public repositories and skill folders. Never publish databases (including WAL/SHM), configuration, auth/environment files, logs, exports, backups, recycle content, project processing records, real native IDs, persona or host settings. Ignore rules are only a secondary control.

For a new release, start from the reviewed source directory. `scripts/audit_release.py` checks exact manifest paths and hashes, rejects reparse/symlink paths and unexpected files, and scans text for credential/profile-path patterns. A separate external JSON array of site-specific forbidden strings can extend the checks. Never place that array in the release tree. Scanner errors and matches print categories/relative paths, not the forbidden values.

Review the actual source and final archive independently. Audit Git staged files, author/committer identity and any history being introduced. A file scan does not audit the user's existing public repository history. Keep existing remote files untouched unless the user explicitly requested their revision. Re-read the remote committed files and compare their hashes with the audited manifest.

Generated diagnostic commands and reports can contain local paths. Review them before sharing. Synthetic fixture UUIDs and example domains are not real identities or credentials; retain a documented, narrow exception only after inspection.

## Recovery limits

Live SQLite is local storage, not encryption at rest. Use the operating system's access controls and disk protection as needed. Forgetting current records does not revoke past copies, backups or model context. Restoring a memory snapshot does not restore the independent session/retention state. Review all private-home components when migrating or erasing a deployment, including the separate habit and proactive databases. Habit rejection/forgetting can retain suppression hashes, turn-deduplication metadata and identifiers to prevent re-collection; old exports/backups are not automatically erased. Pausing collection does not itself stop reuse or erase confirmed ordinary memories.

## 中文隐私说明

公开分发包仅含代码、合成测试与通用指引；空白初始化不会带入原使用者的数据、日程、时区或入口。全新安装关闭学习采集和候选复用、主动准备、标题同步及编号回收。实际部署产生的记忆、证据原句、会话身份、路径、设置、日志、备份与回收内容都应留在私有 home，不能再随源码发布。

启用习惯学习后，当前可信真人输入的筛选短句会落入独立学习库，过滤不保证消除所有个人细节；24小时凭据到期不会自动删除长期证据。推断候选不是确认事实或行动授权，语义归纳仍需人工检查。停止采集、停止候选复用、遗忘正式记忆和清除旧备份分别管理。去重及抑制签名可能在清除后继续保留。

只读会话身份核验不访问消息正文；启用或显式调用占用扫描时，会读取宿主身份/标题元数据及所选工作栈的文件和目录名称、路径与属性。扫描结果本身含私有路径和ID。主动功能只保存准备与门禁状态，不发送消息；关闭后不新增活动观察。选入云端助手上下文、或交给当前模型作语义归纳的内容会经宿主进入该模型；库本身无遥测、远程存储或后台模型调用。已有外部notify程序由其自身行为决定。

普通记忆备份和会话备份不等于完整迁移。停写后保护整个私有 home，升级先用副本核验。公开源码与最终归档需每版重新审查，扫描通过不能替代人工审核，也不能为未来写入的数据作永久保证。
