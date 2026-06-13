# OpenCode Database and Session Deletion Cleanup Plan

Properly clean up the OpenCode SQLite database (`opencode.db`) and its associated session storage files on disk when deleting a session, either individually via the python recovery tool (`--delete`) or via the retention cleanup script (`clean_opencode.sh`).

## User Review Required

> [!IMPORTANT]
> - Deleting a session recursively deletes all child (descendant) sessions as well, including all their messages, parts, inputs, shares, and events.
> - A backup directory `~/.local/share/opencode/backups/opencode-db-cleanup-YYYYMMDD-HHMMSS/` is automatically created before any database modifications in the cleanup script.
> - Database WAL file family (`opencode.db`, `opencode.db-wal`, `opencode.db-shm`) is handled as a single unit during backup.

## Proposed Changes

### OpenCode Session Recovery CLI

#### [MODIFY] [opencode_recover_session.py](file:///home/gfariello/VC/opencode-recover/opencode_recover_session.py)
- Refactor the `--delete` option to perform deep database-level garbage cleanup in addition to (or instead of) using the external CLI.
- Implement recursive CTE queries to find all child/descendant sessions of the selected session.
- Query and show the counts of rows that will be deleted from each of the following tables:
  - `session`
  - `message`
  - `part`
  - `session_message`
  - `session_input`
  - `session_share`
  - `session_context_epoch`
  - `todo`
  - `event`
  - `event_sequence`
- After confirmation, perform all deletions in a single transaction block.
- Delete the corresponding session storage file(s) `~/.local/share/opencode/storage/session_diff/<session_id>.json` for the parent and all descendant sessions.
- Run `VACUUM;` to reclaim disk space.

---

### OpenCode Retention Cleanup Script

#### [MODIFY] [clean_opencode.sh](file:///home/gfariello/VC/opencode-recover/clean_opencode.sh)
- Rewrite `clean_opencode.sh` to make it compliant with `opencode_db_cleanup_handoff_for_claude.md`.
- Support options:
  - `--days X` (defaults to 5)
  - `--dry-run`
  - `--yes` (bypass confirmation)
  - `--db PATH` (defaults to `~/.local/share/opencode/opencode.db`)
- Perform pre-flight safety checks:
  - Verify database integrity using `PRAGMA integrity_check;`.
  - Check for database locks using active process detection (`ps aux` / `fuser` / `lsof`).
- Create a timestamped backup of the database file family (`.db`, `-wal`, `-shm`) in `~/.local/share/opencode/backups/`.
- Perform recursive CTE query to identify all descendant sessions of old sessions.
- Report table sizes/counts before and after using `dbstat` (if available) or raw payload estimators.
- Delete all associated rows recursively inside a single transaction.
- Delete all corresponding `session_diff` JSON files from disk for the deleted sessions.
- Run `VACUUM` to reclaim space.
- Display detailed rollback instructions on failure or completion.

## Verification Plan

### Automated Tests
- We will run the python command `--delete` with a dummy session ID (created for testing) to verify that all database entries and disk files are deleted successfully.
- We will run the rewritten `clean_opencode.sh` with `--dry-run` and verify it correctly computes cutoffs, prints row counts, and identifies the correct files for deletion.

### Manual Verification
- Verify that the SQLite file size decreases after running `clean_opencode.sh` followed by `VACUUM`.
- Check that the `backups` directory is created and populated.
