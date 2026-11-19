# Optional features · 可选功能

Run from the skill directory. In the examples, `memory` abbreviates `python -B scripts/entry.py --home <PRIVATE_HOME>`; substitute the same absolute private home each time. Save JSON as UTF-8 files and pass `--input <FILE>`, or use `--input -` for stdin. Values in angle brackets are placeholders, not usable IDs or evidence.

在技能目录运行。下文 `memory` 是完整 Python 入口命令的简写；每次使用同一个私有 home。占位符必须由实际核实的值替换，不要把示例写入正式记忆。全新安装不启用这些功能，单独同意一个功能不代表同意其他功能。

## Habit candidates · 习惯候选

`memory habits status` reads the global policy revision. `memory habits status --thread-id <ACTUAL_CURRENT_NATIVE_ID>` reads the effective thread policy. Thread controls can restrict global controls, not bypass them. Public defaults are `enabled=false`, `use_learned=false`, `allow_tool_assisted=false`.

Use `memory habits configure --input learning.json` only after an explicit request. For a new global policy:

```json
{"enabled":true,"use_learned":true,"source":"User explicitly requested collection and use of habit candidates","expected_revision":0}
```

Use the current revision for later changes. `enabled=false` stops new collection; `use_learned=false` stops candidate reuse. Neither alone deletes earlier evidence or already confirmed ordinary memories. A thread override adds `thread_id` and its own current policy revision. `allow_tool_assisted` is a separate choice, not an automatic detector of tool use.

开始前读当前策略修订；0只用于新策略。采集和复用分开控制，停止采集不等于删除证据。已确认并写入正式记忆的偏好按普通记忆管理；线程只能收紧全局策略。工具辅助输入有独立开关，不能假称程序能识别所有工具污染。

With authorized lifecycle integration, a verified main human `UserPromptSubmit` may create an observation receipt for eligible excerpts of that current message. There is no history crawler or background model. Optional semantic supplementation uses only a fresh receipt:

```json
{"receipt_id":"<FRESH_VERIFIED_HOOK_RECEIPT>","topic":"response.summary-first","value":"Start with a concise conclusion","quote":"<EXACT_ELIGIBLE_HUMAN_EXCERPT>"}
```

Pass this to `memory habits observe --input observation.json`. Do not add invented `source` or `thread_id` fields; scope, if supplied, must match the receipt. Receipts expire after 24 hours. Quotes must be an exact eligible excerpt (6–360 characters), not attachments, quoted instructions, code, tool output, assistant prose or automatic messages. The filter is heuristic and cannot remove every personal detail. The current host model may see selected excerpts for this step; no model API is called by the library itself.

语义补充只能使用可信真人 hook 发出的当前凭据和逐字原话；不能伪造凭据、从附件抽一句冒充本人偏好，或用 `remember` 绕过来源约束。凭据到期不等于证据自动删除。候选值仍需检查语义是否准确。

`memory habits list` and `memory habits show <HABIT_ID>` display state, revision and evidence. Repeated observations can reach `learned` after the implementation's independent-evidence threshold, but still have candidate status. They do not authorize actions or become global rules. To include allowed learned candidates in context, provide the actual current ID with `memory context --project <SLUG> --thread-id <ACTUAL_CURRENT_NATIVE_ID> --markdown`; omission excludes them.

After the user reviews the displayed evidence, `memory habits review --input review.json` can explicitly confirm:

```json
{"habit_id":"<DISPLAYED_HABIT_ID>","action":"confirm","expected_revision":1,"source":"User explicitly confirmed this displayed habit","confirmed":true}
```

Replace revision 1 with the observed revision. Actions `reject`, `forget` and `edit` use the same revision/source/confirmation discipline; `edit` also needs a nonempty `value` and returns to candidate status. `memory habits clear --input clear.json` uses the current global policy revision, actual source and `confirmed=true`. It clears live learning evidence and linked confirmed preferences, while retaining policy and suppression/duplicate-protection metadata. Old backups and exports need separate review.

确认、拒绝、修改、遗忘前都先读当前证据和修订；来源写真实用户决定。重复选择只能形成候选，不是自动确认。清除不会撤回已经发送给模型的上下文，也不删除旧备份或所有去重标记。

## Proactive preparation · 主动消息准备

These are CLI commands, not proactive MCP tools. `memory proactive status` reads policy and preparation attempts. Enable only after the user requests it:

```json
{"enabled":true,"source":"User explicitly requested proactive preparation"}
```

Use `memory proactive configure --input proactive.json`. Disable with `enabled=false`; pause with a timezone-qualified `paused_until`, and resume with `paused_until=null`. It does not install a scheduler, send messages or create a host thread. Disabled integration does not add new activity observations.

Configurable scheduling lives in the private `settings.json`; merge keys without replacing unrelated settings. Example generic UTC policy:

```json
{"proactive_chat":{"timezone_offset_minutes":0,"start_hour":8,"end_hour_exclusive":20,"scheduled_hour":12,"recent_user_minutes":45}}
```

The public default is UTC and an all-day eligibility window. Offset is integer minutes from -720 to 840. `start_hour` is 0–23, `end_hour_exclusive` is 1–24, and start must precede end. Optional `scheduled_hour` must be inside that window. Without a fixed hour, `scheduled_hour_min/max` bound a date-dependent candidate hour inside it. `recent_user_minutes` is 0–1440. Optional `fallback_hour` defaults to the window end, so it does not relax the recent-activity gate within the window. An explicit earlier fallback changes that behavior and needs to reflect the user's choice. Fixed offsets do not implement daylight-saving transitions.

窗口与时区是用户自行选择的部署参数，上面是通用示例。库不附带原使用者的时区、日程或聊天目标。固定偏移不自动处理夏令时。配置资格窗口不等于已创建任何定时任务。

The daily-chat pointer must first refer to the verified registered main chat the user selected. Read `daily-chat show` and bind only after an explicit continuation request using actual identity and expected revision. Then:

```json
{"current_thread_id":"<ACTUAL_CURRENT_NATIVE_ID>","busy":false}
```

Pass the check to `memory proactive evaluate --input check.json` for eligibility without spending the daily preparation slot. The CLI obtains the intended daily target from the real pointer; do not invent a matching target. Only when preparing an actually authorized output use `memory proactive claim --input check.json`. A successful atomic claim records at most one `prepared` per configured local date. It is neither a send nor a delivery/read receipt. Keep actual busy, pause, target and recent-user-activity gates; production uses real time rather than a synthetic `now`.

调度与向外发送仍须用户另外授权。若宿主策略要求每天只触发一次，门禁跳过后应结束当次执行，不自行增加轮询、重试或补发；本库不强制宿主触发频率，也不能证明未来调度或消息送达。

## Session code recycling · 会话编号回收

This is distinct from file retention. With recycling disabled, allocations append at the registry high-water mark rather than automatically reuse vacant codes. Opting into code reuse authorizes future verified main-session registration to archive vacant old registrations and allocate the smallest available code after a complete scan. A00 remains reserved. Existing live chats keep their codes. Reclaim does not delete host chats, directories or work files.

Only after the user explicitly selects this policy, merge verified settings such as:

```json
{"host_title_sync":{"enabled":false,"codex_home":"<ABSOLUTE_VERIFIED_HOST_HOME>"},"session_recycling":{"enabled":true,"workspace_roots":["<ABSOLUTE_USER_SELECTED_WORKSPACE>"]}}
```

All roots must be real, existing absolute directories. A missing host index or root, unknown types, unsafe links or concurrent changes make the scan incomplete and prevent release/allocation under this policy. It checks all active and archived main chats and workspace names, including hidden and nested files/directories. A live prior identity, project binding, daily entry or its history protects the code even after title changes.

`memory session recycle --input scan.json` takes `{"operation":"scan"}`. Read the actual `complete`, `scan_id`, `releasable` and `blocked` output. An explicitly requested scan can read host/workspace metadata even when recycling is disabled; disabled does not mean every read-only command is blocked.

For manual batch release, display the full current list, obtain explicit confirmation, then use:

```json
{"operation":"reclaim","source":"User explicitly confirmed this displayed scan list","expected_scan_id":"<EXACT_CURRENT_SCAN_ID>","confirmed":true}
```

There is no `confirm` operation or subset selector. A changed scan invalidates the fingerprint and requires a new review. `{"operation":"history","code":"A01"}` reads generations and prior events. Automatic allocation under the enabled policy does not require a separate manual reclaim each time. It preserves old identities, events and increasing revisions; it does not grant permission to erase chat history or files to free a number.

手动批量释放必须确认当前完整清单；启用策略后的自动最小号分配则按事务内完整扫描执行，两者不要混淆。旧世代和身份历史仍是私有数据；扫描失败保留，不凭标题消失、侧栏不可见或旧缓存判断空号。
