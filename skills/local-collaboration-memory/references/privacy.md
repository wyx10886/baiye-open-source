# Privacy and publication boundary

The distributed skill is code, synthetic tests and instructions. It contains no seeded memories, session identities, persona, machine configuration, activity logs or copied private Git history. A new home starts empty.

## What a deployment stores

- Explicit memory records include content, source, status, scope, dates and revision history in local SQLite. Markdown views and backups are additional private copies.
- Optional project handoffs write the user-selected project summary/issue files and their manual-notes sections in that project.
- Optional lifecycle hooks retrieve context and the completion notifier may save up to 1,200 characters of assistant milestone text as a candidate, with native session/turn identifiers as provenance. This is local collection. User messages are not automatically imported as a transcript. Inspect `capture_turn` before enabling this behavior for sensitive work.
- Optional session/title integration stores native identities, titles and binding history. Optional cleanup stores root paths, file metadata, proposals and recycled content.
- Before automatic numbering, optional lifecycle integration checks the exact native session ID against the newest local host index using a read-only SQLite connection. It reads only source, parent and rollout-location metadata for that ID. If needed, it reads at most the first 64 KiB of a matching `.jsonl` file under the host's `sessions` or `archived_sessions` directory, requiring the exact ID in both filename and first `session_meta` record. It never reads message bodies or writes the host database. This check runs even when title synchronization is disabled. Child agents are skipped; unknown identity stays pending. Its private diagnostic record retains only native ID, classification, evidence type and check time, without the raw metadata or rollout path.
- The local web manager uses a random token, validates Host/Origin and binds only to loopback. Do not expose it through a public proxy or include its URL/token in reports.

The runtime has no telemetry, external CDN or built-in remote storage request. Context supplied to a cloud-hosted assistant is transmitted to that assistant through the host. A preexisting notification command preserved by the optional installer is an external program with its own behavior. Title sync, when enabled, calls the local Codex app-server. These boundaries differ from claiming data can never leave a device.

## Safe sharing

Keep the private home outside public repositories and skill folders. Never publish databases (including WAL/SHM), configuration, auth/environment files, logs, exports, backups, recycle content, project processing records, real native IDs, persona or host settings. Ignore rules are only a secondary control.

For a new release, start from the reviewed source directory. `scripts/audit_release.py` checks exact manifest paths and hashes, rejects reparse/symlink paths and unexpected files, and scans text for credential/profile-path patterns. A separate external JSON array of site-specific forbidden strings can extend the checks. Never place that array in the release tree. Scanner errors and matches print categories/relative paths, not the forbidden values.

Review the actual source and final archive independently. Audit Git staged files, author/committer identity and any history being introduced. A file scan does not audit the user's existing public repository history. Keep existing remote files untouched unless the user explicitly requested their revision. Re-read the remote committed files and compare their hashes with the audited manifest.

Generated diagnostic commands and reports can contain local paths. Review them before sharing. Synthetic fixture UUIDs and example domains are not real identities or credentials; retain a documented, narrow exception only after inspection.

## Recovery limits

Live SQLite is local storage, not encryption at rest. Use the operating system's access controls and disk protection as needed. Forgetting current records does not revoke past copies, backups or model context. Restoring a memory snapshot does not restore the independent session/retention state. Review all private-home components when migrating or erasing a deployment.
