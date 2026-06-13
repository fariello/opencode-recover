# `opencode.db` Cleanup Handoff for Claude Opus 4.8 under opencode

Prepared for: Gabriele Fariello  
Purpose: Use this as a complete handoff prompt and technical guide for asking Claude Opus 4.8, running under `opencode`, to build a safe, comprehensive cleanup script for OpenCode's local SQLite database.

## What I want Claude to build

Please write a production-quality shell script, or a small CLI utility if that is clearly safer, to clean up OpenCode's `opencode.db` by removing **everything connected to sessions older than X days**.

The script must be safe, verbose, auditable, and defensive. It should:

1. Accept a retention window, such as `--days 5`, defaulting to a sensible value only if explicitly documented.
2. Operate on the OpenCode SQLite database at:

   ```bash
   ~/.local/share/opencode/opencode.db
   ```

3. Refuse to run while OpenCode or any process appears to have the database open.
4. Detect and correctly handle SQLite Write-Ahead Log files:

   ```text
   opencode.db
   opencode.db-wal
   opencode.db-shm
   ```

5. Create a backup before destructive operations.
6. Run a dry run by default, or at least support `--dry-run`.
7. Report row counts and estimated table sizes before and after cleanup.
8. Ask for confirmation before deleting anything unless `--yes` is supplied.
9. Delete all rows that are tied to sessions older than the retention window.
10. Vacuum the database afterward to reclaim disk space.
11. Verify the result using `dbstat` when available.
12. Avoid fragile assumptions about column names by validating the schema before running.
13. Fail closed if required tables or columns are missing.
14. Clearly report exactly what SQL it intends to run.

This document summarizes everything discovered during the conversation and why we reached the conclusions below.

## Important framing

This is not merely about deleting old rows from `message`, `part`, and `session`.

The major discovery was that the real bloat in this database was in the `event` table, not primarily in the conversation tables. A cleanup that only deletes old sessions, messages, and parts can appear to work, and may delete some rows, but still leave the database almost as large as before.

The cleanup script needs to treat `event` as a first-class target.

## Observed environment and database path

The database file was located at:

```bash
~/.local/share/opencode/opencode.db
```

At one point it was reported as approximately 2.4 GB. After an initial pruning attempt, it dropped to roughly 1.8 GB. Later, after a dump-and-rebuild attempt, the size remained effectively unchanged:

```text
Initial opencode.db file size: 1.74 GB
Logical structural data size: 1.73 GB
Final Size: 1.74 GB
```

This was an important clue. It showed that the remaining size was not merely free pages or fragmentation. The `.dump` still contained almost all the data, so the remaining 1.7 GB was live logical data.

## Tables discovered

Running `.tables` inside SQLite produced this schema-level table list:

```text
__drizzle_migrations   message                session_context_epoch
account                migration              session_input
account_state          part                   session_message
control_account        permission             session_share
data_migration         project                todo
event                  project_directory      workspace
event_sequence         session
```

Earlier suggestions incorrectly referenced non-existent tables such as `parts` and incorrect table/column names such as `createdAt`.

The actual table names include singular `message`, singular `part`, and singular `session`.

## Confirmed schema for `message`

The following was run:

```sql
PRAGMA table_info(message);
```

It returned:

```text
0|id|TEXT|0||1
1|session_id|TEXT|1||0
2|time_created|INTEGER|1||0
3|time_updated|INTEGER|1||0
4|data|TEXT|1||0
```

Key conclusions:

1. `message.id` is a `TEXT` primary key.
2. `message.session_id` links messages to sessions.
3. `message.time_created` is an `INTEGER`.
4. The timestamp is not `createdAt`.
5. The timestamp is not ISO text.
6. The timestamp should be treated as Unix time in milliseconds.

## Confirmed schema for `session`

The following was run:

```sql
PRAGMA table_info(session);
```

It returned:

```text
0|id|TEXT|0||1
1|project_id|TEXT|1||0
2|parent_id|TEXT|0||0
3|slug|TEXT|1||0
4|directory|TEXT|1||0
5|title|TEXT|1||0
6|version|TEXT|1||0
7|share_url|TEXT|0||0
8|summary_additions|INTEGER|0||0
9|summary_deletions|INTEGER|0||0
10|summary_files|INTEGER|0||0
11|summary_diffs|TEXT|0||0
12|revert|TEXT|0||0
13|permission|TEXT|0||0
14|time_created|INTEGER|1||0
15|time_updated|INTEGER|1||0
16|time_compacting|INTEGER|0||0
17|time_archived|INTEGER|0||0
18|workspace_id|TEXT|0||0
19|path|TEXT|0||0
20|agent|TEXT|0||0
21|model|TEXT|0||0
22|cost|REAL|1|0|0
23|tokens_input|INTEGER|1|0|0
24|tokens_output|INTEGER|1|0|0
25|tokens_reasoning|INTEGER|1|0|0
26|tokens_cache_read|INTEGER|1|0|0
27|tokens_cache_write|INTEGER|1|0|0
28|metadata|TEXT|0||0
```

Key conclusions:

1. `session.id` is a `TEXT` primary key.
2. `session.parent_id` likely represents parent and child session relationships.
3. `session.time_created` is an `INTEGER` and is the reliable age filter for sessions.
4. The script should treat sessions older than X days as the root deletion targets.
5. Child/subsessions require special care. A child session might need to be deleted because it is older than the cutoff, because its parent is being deleted, or both.

## Confirmed schema for `event`

The following was run:

```sql
PRAGMA table_info(event);
```

It returned:

```text
0|id|TEXT|0||1
1|aggregate_id|TEXT|1||0
2|seq|INTEGER|1||0
3|type|TEXT|1||0
4|data|TEXT|1||0
```

Key conclusions:

1. `event` has no `time_created` column.
2. `event` has a large `data` field.
3. `event.aggregate_id` appears to be the linkage field.
4. We believe `event.aggregate_id` maps to `session.id`, based on the table naming, event-sourcing pattern, and the need to associate event streams with session aggregates.
5. This linkage should be validated by Claude's script before deletion.

Validation query Claude should include or use:

```sql
SELECT COUNT(*)
FROM event
WHERE aggregate_id IN (SELECT id FROM session);
```

Also useful:

```sql
SELECT type, COUNT(*)
FROM event
GROUP BY type
ORDER BY COUNT(*) DESC;
```

And:

```sql
SELECT aggregate_id, COUNT(*) AS event_count
FROM event
GROUP BY aggregate_id
ORDER BY event_count DESC
LIMIT 20;
```

The script should not blindly delete from `event` until it confirms that a meaningful number of `event.aggregate_id` values match `session.id`.

## The major storage discovery

We ran a `dbstat` query:

```sql
SELECT
    name AS table_name,
    SUM(pgsize) / 1024 / 1024 AS size_mb
FROM
    dbstat
GROUP BY
    name
ORDER BY
    size_mb DESC;
```

The result was:

```text
event|1654
message|85
part|37
event_aggregate_type_seq_idx|2
sqlite_autoindex_event_1|1
part_message_id_id_idx|1
event_aggregate_seq_idx|1
workspace|0
todo_session_idx|0
todo|0
sqlite_schema|0
sqlite_autoindex_workspace_1|0
sqlite_autoindex_todo_1|0
sqlite_autoindex_session_share_1|0
sqlite_autoindex_session_message_1|0
sqlite_autoindex_session_input_1|0
sqlite_autoindex_session_context_epoch_1|0
sqlite_autoindex_session_1|0
sqlite_autoindex_project_directory_1|0
sqlite_autoindex_project_1|0
sqlite_autoindex_permission_1|0
sqlite_autoindex_part_1|0
sqlite_autoindex_migration_1|0
sqlite_autoindex_message_1|0
event_sequence|0
data_migration|0
control_account|0
account_state|0
account|0
__drizzle_migrations|0
```

This proved the dominant bloat was:

```text
event table: approximately 1,654 MB
message table: approximately 85 MB
part table: approximately 37 MB
```

So the cleanup must target `event`, not merely `message` and `part`.

## Mistakes and false starts from the conversation

### Mistake 1: Assuming a `parts` table existed

An early command attempted:

```sql
DELETE FROM parts WHERE session_id IN (...);
```

This failed because the table is named `part`, not `parts`.

### Mistake 2: Assuming columns named `createdAt`

An early count query attempted:

```sql
SELECT COUNT(*)
FROM message
WHERE datetime(createdAt) < datetime('now', '-5 days');
```

This failed because `createdAt` does not exist.

The actual timestamp column is:

```text
time_created
```

### Mistake 3: Assuming timestamps were ISO text

At one point we considered:

```sql
datetime(time_created) < datetime('now', '-5 days')
```

But `time_created` is an integer. The correct approach is millisecond math.

The cutoff should be computed as:

```sql
strftime('%s', 'now') * 1000 - (:days * 86400000)
```

Or computed in the shell as:

```bash
CUTOFF_TIME=$(( $(date +%s) * 1000 - DAYS * 86400000 ))
```

Then used as:

```sql
WHERE time_created < $CUTOFF_TIME
```

### Mistake 4: Assuming `part.messageId`

There was uncertainty about whether `part` links to `message` using:

```text
messageId
```

or:

```text
message_id
```

Later evidence showed an index named:

```text
part_message_id_id_idx
```

This strongly suggests the correct column is:

```text
message_id
```

Claude should verify with:

```sql
PRAGMA table_info(part);
```

and refuse to proceed if it cannot find a message foreign key column.

### Mistake 5: Believing `VACUUM` alone could solve the problem

`VACUUM` can reclaim free pages only after live rows have been deleted. It does not shrink the database if the space is still occupied by valid rows.

In this case, the `.dump` size was still 1.73 GB after cleanup, proving the data was still live.

### Mistake 6: Believing dump-and-rebuild would solve it

A script was created to:

1. Dump the database to SQL.
2. Move the old database aside.
3. Recreate it from the dump.

The result was:

```text
Initial size: 1.74 GB
Dump size: 1.73 GB
Final size: 1.74 GB
```

Conclusion:

The database was not primarily bloated due to internal fragmentation. It still contained live rows, especially in the `event` table.

### Mistake 7: Ignoring WAL sidecar files

Any rebuild or destructive operation must treat SQLite's WAL file family carefully:

```text
opencode.db
opencode.db-wal
opencode.db-shm
```

The script should either checkpoint WAL safely before work or back up all files together. It must not move only `opencode.db` while leaving stale `-wal` and `-shm` files in place.

Suggested preflight commands:

```sql
PRAGMA journal_mode;
PRAGMA wal_checkpoint(TRUNCATE);
```

But Claude should be careful: `wal_checkpoint(TRUNCATE)` should only be attempted when no OpenCode process is using the database.

## What appears to be going on

OpenCode appears to use a SQLite database with a Drizzle-style schema. It stores session history, message data, parts, inputs, session relationships, and also a very large event stream.

The `event` table looks like an event-sourcing table:

```text
id           TEXT primary key
aggregate_id TEXT
seq          INTEGER
type         TEXT
data         TEXT
```

The `event.data` field appears to be the main bloat source.

The evidence suggests:

1. `session` is the core unit of retention.
2. `message` is linked to `session` through `message.session_id`.
3. `part` is linked to `message` through likely `part.message_id`.
4. `event` is linked to `session` through likely `event.aggregate_id = session.id`.
5. There are also secondary tables that must be cleaned when sessions are removed:

   ```text
   session_share
   session_message
   session_input
   session_context_epoch
   todo
   permission
   event_sequence
   ```

Not all of these were fully inspected during the conversation. Claude should inspect them before writing final deletion SQL.

## Tables Claude should inspect before finalizing the script

Claude should run or include logic equivalent to:

```sql
PRAGMA table_info(part);
PRAGMA table_info(session_share);
PRAGMA table_info(session_message);
PRAGMA table_info(session_input);
PRAGMA table_info(session_context_epoch);
PRAGMA table_info(todo);
PRAGMA table_info(permission);
PRAGMA table_info(event_sequence);
PRAGMA table_info(event);
PRAGMA table_info(session);
PRAGMA table_info(message);
```

Claude should also inspect foreign keys:

```sql
PRAGMA foreign_key_list(part);
PRAGMA foreign_key_list(message);
PRAGMA foreign_key_list(session_share);
PRAGMA foreign_key_list(session_message);
PRAGMA foreign_key_list(session_input);
PRAGMA foreign_key_list(session_context_epoch);
PRAGMA foreign_key_list(todo);
PRAGMA foreign_key_list(permission);
PRAGMA foreign_key_list(event);
PRAGMA foreign_key_list(event_sequence);
```

If foreign keys are not declared, Claude should infer relationships from column names and indexes, but it should say so clearly and run count-based validation first.

## Critical count checks before deletion

For a cutoff of X days, define:

```bash
CUTOFF_TIME=$(( $(date +%s) * 1000 - DAYS * 86400000 ))
```

Before deleting, report:

```sql
SELECT COUNT(*) AS sessions_to_delete
FROM session
WHERE time_created < $CUTOFF_TIME;
```

```sql
SELECT COUNT(*) AS messages_to_delete
FROM message
WHERE time_created < $CUTOFF_TIME;
```

```sql
SELECT COUNT(*) AS events_to_delete
FROM event
WHERE aggregate_id IN (
    SELECT id FROM session WHERE time_created < $CUTOFF_TIME
);
```

```sql
SELECT COUNT(*) AS parts_to_delete
FROM part
WHERE message_id IN (
    SELECT id FROM message WHERE time_created < $CUTOFF_TIME
);
```

If `part.message_id` is not present, inspect `part` and find the correct column.

## Recommended conceptual deletion strategy

The cleanup should use a single SQLite connection and a single transaction for deletion. Temporary tables should be created inside the same SQLite session.

High-level order:

1. Check for active OpenCode process or database lock.
2. Create a timestamped backup of the full SQLite file family.
3. Compute the cutoff time in milliseconds.
4. Validate schema.
5. Create target temp tables:

   ```sql
   CREATE TEMP TABLE target_sessions AS
   SELECT id FROM session
   WHERE time_created < $CUTOFF_TIME;
   ```

   ```sql
   CREATE TEMP TABLE target_messages AS
   SELECT id FROM message
   WHERE time_created < $CUTOFF_TIME
      OR session_id IN (SELECT id FROM target_sessions);
   ```

   This second query is important. It catches messages that belong to old sessions even if their own timestamp is unusual.

6. Include child sessions/subsessions. Because `session.parent_id` exists, old session cleanup may need recursion.

   Claude should consider a recursive CTE such as:

   ```sql
   WITH RECURSIVE session_tree(id) AS (
       SELECT id
       FROM session
       WHERE time_created < $CUTOFF_TIME

       UNION

       SELECT s.id
       FROM session s
       JOIN session_tree st ON s.parent_id = st.id
   )
   SELECT id FROM session_tree;
   ```

   This would delete child sessions if their parent is old. Claude should decide whether that is desired and document the behavior.

7. Delete from the heaviest and most dependent tables first:

   ```sql
   DELETE FROM event
   WHERE aggregate_id IN (SELECT id FROM target_sessions);
   ```

   ```sql
   DELETE FROM part
   WHERE message_id IN (SELECT id FROM target_messages);
   ```

8. Delete from association and dependent tables:

   ```sql
   DELETE FROM session_message
   WHERE session_id IN (SELECT id FROM target_sessions);

   DELETE FROM session_input
   WHERE session_id IN (SELECT id FROM target_sessions);

   DELETE FROM session_share
   WHERE session_id IN (SELECT id FROM target_sessions);

   DELETE FROM session_context_epoch
   WHERE session_id IN (SELECT id FROM target_sessions);
   ```

   Column names need validation.

9. Delete todos or permissions tied to sessions, if their schemas confirm a session relationship.

10. Delete messages:

   ```sql
   DELETE FROM message
   WHERE id IN (SELECT id FROM target_messages);
   ```

11. Delete sessions:

   ```sql
   DELETE FROM session
   WHERE id IN (SELECT id FROM target_sessions);
   ```

12. Commit.

13. Run:

   ```sql
   VACUUM;
   ```

14. Run post-cleanup `dbstat` summary.

## Candidate SQL skeleton

This is not final production SQL. Claude should use it as the conceptual starting point and make it schema-validated.

```sql
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;

CREATE TEMP TABLE target_sessions AS
WITH RECURSIVE session_tree(id) AS (
    SELECT id
    FROM session
    WHERE time_created < __CUTOFF_TIME__

    UNION

    SELECT s.id
    FROM session s
    JOIN session_tree st ON s.parent_id = st.id
)
SELECT DISTINCT id FROM session_tree;

CREATE TEMP TABLE target_messages AS
SELECT DISTINCT id
FROM message
WHERE time_created < __CUTOFF_TIME__
   OR session_id IN (SELECT id FROM target_sessions);

DELETE FROM event
WHERE aggregate_id IN (SELECT id FROM target_sessions);

DELETE FROM part
WHERE message_id IN (SELECT id FROM target_messages);

DELETE FROM session_share
WHERE session_id IN (SELECT id FROM target_sessions);

DELETE FROM session_message
WHERE session_id IN (SELECT id FROM target_sessions);

DELETE FROM session_input
WHERE session_id IN (SELECT id FROM target_sessions);

DELETE FROM session_context_epoch
WHERE session_id IN (SELECT id FROM target_sessions);

DELETE FROM message
WHERE id IN (SELECT id FROM target_messages);

DELETE FROM session
WHERE id IN (SELECT id FROM target_sessions);

COMMIT;
PRAGMA foreign_keys = ON;

VACUUM;
```

Again, Claude must validate all table and column names before executing this. In particular, `session_context_epoch` needs schema inspection before including it.

## Requirements for the final script

The script should be more careful than the earlier prototypes.

### CLI behavior

Example usage:

```bash
./clean-opencode-db.sh --days 5 --dry-run
./clean-opencode-db.sh --days 5
./clean-opencode-db.sh --days 5 --yes
./clean-opencode-db.sh --days 5 --db ~/.local/share/opencode/opencode.db
```

### Output requirements

It should print:

1. Database path.
2. Journal mode.
3. Whether `-wal` and `-shm` files exist.
4. Starting file sizes for:

   ```text
   opencode.db
   opencode.db-wal
   opencode.db-shm
   ```

5. `dbstat` table sizes before cleanup, if available.
6. Cutoff date as human-readable local time and raw Unix millisecond value.
7. Counts of sessions, messages, parts, and events to delete.
8. The SQL plan or a concise representation of it.
9. Confirmation prompt.
10. Rows affected per table.
11. Ending file sizes.
12. `dbstat` table sizes after cleanup.
13. Backup path.

### Safety requirements

The script should:

1. Use `set -euo pipefail` only if all unset-variable and pipeline behaviors are handled correctly.
2. Avoid emojis and non-ASCII output for portability.
3. Refuse invalid `--days` values.
4. Refuse `--days 0` unless an explicit `--allow-zero-days` flag is provided.
5. Check that `sqlite3` exists.
6. Check that the DB exists.
7. Check that the DB is a valid SQLite database:

   ```bash
   sqlite3 "$DB_PATH" "PRAGMA integrity_check;"
   ```

8. Refuse to continue unless `integrity_check` returns `ok`.
9. Detect OpenCode locks with `lsof` if available.
10. Optionally detect locks with `fuser` on Linux if `lsof` is not available.
11. Make a timestamped backup before deleting anything.
12. Backup the full file family, not only the main database file.
13. Never delete the backup automatically.
14. Use one SQLite connection for transaction and temporary tables.
15. Run `VACUUM` outside the transaction.
16. Avoid `.dump` unless doing a rebuild mode.
17. Fail closed if `event.aggregate_id` does not appear to correspond to `session.id`.

### Backup requirements

Use a timestamped backup directory such as:

```bash
~/.local/share/opencode/backups/opencode-db-cleanup-YYYYMMDD-HHMMSS/
```

Copy:

```text
opencode.db
opencode.db-wal, if present
opencode.db-shm, if present
```

Before backup, if safe, run:

```sql
PRAGMA wal_checkpoint(TRUNCATE);
```

But only if no process is using the DB.

### Rollback instructions

The script should print rollback instructions such as:

```bash
cp backup/opencode.db ~/.local/share/opencode/opencode.db
cp backup/opencode.db-wal ~/.local/share/opencode/opencode.db-wal 2>/dev/null || true
cp backup/opencode.db-shm ~/.local/share/opencode/opencode.db-shm 2>/dev/null || true
```

It should advise closing OpenCode before rollback.

## Suggested diagnostic queries

### Table sizes

```sql
SELECT
    name AS table_name,
    ROUND(SUM(pgsize) / 1024.0 / 1024.0, 2) AS size_mb
FROM dbstat
GROUP BY name
ORDER BY SUM(pgsize) DESC;
```

If `dbstat` is unavailable, fallback to rough payload estimates:

```sql
SELECT 'event' AS table_name, SUM(length(data)) / 1024 / 1024 AS mb FROM event
UNION ALL
SELECT 'message', SUM(length(data)) / 1024 / 1024 FROM message;
```

For `part`, inspect columns first because the payload column name was not confirmed:

```sql
PRAGMA table_info(part);
```

### Event linkage validation

```sql
SELECT COUNT(*) AS event_rows_linked_to_sessions
FROM event
WHERE aggregate_id IN (SELECT id FROM session);
```

```sql
SELECT COUNT(*) AS total_event_rows
FROM event;
```

If almost no event rows match sessions, do not delete events using `aggregate_id IN session.id`. Investigate `event_sequence` or other aggregate mapping.

### Old event count

```sql
SELECT COUNT(*) AS old_events
FROM event
WHERE aggregate_id IN (
    SELECT id FROM session WHERE time_created < __CUTOFF_TIME__
);
```

### Old message count

```sql
SELECT COUNT(*) AS old_messages
FROM message
WHERE time_created < __CUTOFF_TIME__;
```

### Old session count

```sql
SELECT COUNT(*) AS old_sessions
FROM session
WHERE time_created < __CUTOFF_TIME__;
```

## Open questions Claude should resolve

1. Does `event.aggregate_id` always equal `session.id`, or can it also refer to other aggregate types?
2. Does `event_sequence` need cleanup when events are removed?
3. Does `session_context_epoch` contain rows tied to session IDs that should be removed?
4. Does `todo` contain `session_id`?
5. Does `permission` contain session-specific rows, or is it project-level and should be left alone?
6. Does `session_message` duplicate message data or only link sessions to messages?
7. Does OpenCode expect event streams to be contiguous by `seq` for retained sessions?
8. Should child sessions be deleted if their parent is older than X days even when the child is newer?
9. Should the script support a `--preserve-session SESSION_ID` option?
10. Should the script support `--archive-only` first, or is deletion acceptable?

## Important caution about subsessions

The user explicitly wants cleanup to include subsessions. The schema has:

```text
session.parent_id
```

This strongly suggests a parent-child session relationship.

Claude should not simply delete sessions where `time_created < cutoff` and ignore children. It should define a clear policy:

1. Delete any session older than cutoff.
2. Delete descendants of deleted sessions, even if newer.
3. Or delete only descendants that are also older than cutoff.

The safest interpretation of "everything connected to sessions older than X days" is:

1. Select root target sessions older than X days.
2. Include all descendant sessions via recursive CTE.
3. Delete all messages, parts, events, inputs, links, and session-scoped metadata connected to that expanded set.

But this may delete a newer child session under an older parent. The script should disclose this behavior in dry run output.

## What the final script should not do

1. Do not delete the entire database unless the user explicitly requests reset mode.
2. Do not assume `createdAt`.
3. Do not assume ISO timestamps.
4. Do not assume `parts`.
5. Do not assume `part.messageId`.
6. Do not silently ignore missing columns.
7. Do not use multiple SQLite invocations if temporary tables are needed across statements.
8. Do not run `VACUUM` inside a transaction.
9. Do not rely on `.dump` to reduce file size when rows are still live.
10. Do not claim success based only on `VACUUM`.
11. Do not forget the `event` table.

## A possible final script architecture

Claude should consider implementing the script as:

```text
clean-opencode-db.sh
```

With functions:

```bash
usage
die
require_command
get_size_bytes
human_size
sqlite_scalar
sqlite_exec
check_db_exists
check_db_integrity
check_locks
checkpoint_wal
backup_db_family
load_schema
validate_schema
compute_cutoff
print_pre_cleanup_stats
dry_run_counts
confirm
run_cleanup_transaction
vacuum_db
print_post_cleanup_stats
print_rollback_instructions
```

The script should avoid clever heredoc tricks where possible. It may be safer to write the SQL to a temporary file after substituting validated numeric values, then run:

```bash
sqlite3 "$DB_PATH" < "$SQL_FILE"
```

The temporary SQL file should be retained in the backup directory for auditability.

## A recommended dry-run report

The dry run should produce something like:

```text
Database: /home/gfariello/.local/share/opencode/opencode.db
Retention: 5 days
Cutoff: 2026-06-03 00:00:00 local
Cutoff ms: 1780000000000

Pre-cleanup file sizes:
  opencode.db: 1.74 GB
  opencode.db-wal: 0 B
  opencode.db-shm: 0 B

Largest tables:
  event: 1654 MB
  message: 85 MB
  part: 37 MB

Rows that would be deleted:
  sessions: N
  descendant sessions: N
  messages: N
  parts: N
  events: N
  session_share: N
  session_message: N
  session_input: N
  session_context_epoch: N

No changes made because this was a dry run.
```

## Final working theory

The correct cleanup is a session-rooted relational prune.

The event table is the key. It stores massive event payloads in `event.data`, and those events appear to be tied to sessions through `event.aggregate_id`. Because `event` does not have its own timestamp, the only sensible age-based cleanup is to join events back to sessions and delete events whose aggregate session is older than the retention window.

The likely complete deletion set is:

```text
event rows where event.aggregate_id matches old or descendant session IDs
part rows where part.message_id matches messages in old or descendant sessions
session_message rows tied to old or descendant sessions
session_input rows tied to old or descendant sessions
session_share rows tied to old or descendant sessions
session_context_epoch rows tied to old or descendant sessions, if schema confirms session_id
message rows tied to old or descendant sessions
session rows older than cutoff and their descendants, depending on chosen policy
```

After this deletion, `VACUUM` should finally shrink the file because the 1.65 GB in `event` will have been removed as live data.

## Suggested prompt to Claude

Use the text below as the direct instruction to Claude:

> Build me a production-quality cleanup script for OpenCode's `~/.local/share/opencode/opencode.db`. It must delete everything connected to sessions older than X days, including subsessions and the huge event stream. Use the findings in this document as evidence, but validate the schema at runtime before deleting. The script must support dry run, confirmation, backup, WAL handling, row counts, `dbstat` before and after, clear rollback instructions, and a final `VACUUM`. The key schema facts discovered are: `session.time_created` and `message.time_created` are Unix millisecond integers; `event` has no timestamp and has columns `id`, `aggregate_id`, `seq`, `type`, `data`; `event` was the major bloat table at about 1,654 MB; `message` was about 85 MB; `part` was about 37 MB; `event.aggregate_id` appears to link to `session.id` and must be validated. Do not repeat earlier mistakes such as assuming `createdAt`, `parts`, ISO timestamps, or `part.messageId`. Make the script defensive and explain the deletion logic in comments.

