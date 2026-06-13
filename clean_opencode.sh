#!/bin/bash

DAYS=${1:-5}
DB_PATH="$HOME/.local/share/opencode/opencode.db"

# 1. Validate database file exists
if [ ! -f "$DB_PATH" ]; then
    echo "[-] Error: OpenCode database not found at $DB_PATH"
    exit 1
fi

# 2. Check for active application locks with an lsof fallback
if command -v lsof >/dev/null 2>&1; then
    if lsof "$DB_PATH" >/dev/null 2>&1; then
        echo "[!] Warning: OpenCode is active. Please close OpenCode before running cleanup."
        exit 1
    fi
fi

# Calculate millisecond threshold (1 day = 86400000 ms)
MS_THRESHOLD=$(( DAYS * 86400000 ))
CUTOFF_TIME=$(( $(date +%s) * 1000 - MS_THRESHOLD ))

echo "[*] Scanning tables for items older than $DAYS days..."
COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM message WHERE time_created < $CUTOFF_TIME;")

if [ "$COUNT" -eq 0 ] || [ -z "$COUNT" ]; then
    echo "[+] Clean slate! No data found older than $DAYS days."
    exit 0
fi

echo "[!] Found $COUNT messages marked for deletion."
read -p "[?] Run verbose purge and vacuum? (y/N): " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "[-] Cleanup canceled."
    exit 0
fi

# 3. Detect column naming schema variation for table 'part'
PART_COLS=$(sqlite3 "$DB_PATH" "PRAGMA table_info(part);")
if echo "$PART_COLS" | grep -q "message_id"; then
    PART_FK="message_id"
else
    PART_FK="messageId"
fi

echo -e "\n[+] Starting Consolidated Database Transaction Block...\n"

# 4. Single unified SQLite process invocation
sqlite3 "$DB_PATH" <<EOF
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;

-- Create optimization lookup targets (lives perfectly throughout this block)
CREATE TEMP TABLE to_delete AS SELECT id FROM session WHERE time_created < $CUTOFF_TIME;
CREATE TEMP TABLE messages_to_delete AS SELECT id FROM message WHERE time_created < $CUTOFF_TIME;

.print "--------------------------------------------------------"
.print "[*] Executing: Purging data payloads from table 'part'..."
DELETE FROM part WHERE $PART_FK IN (SELECT id FROM messages_to_delete);
SELECT printf('[-] Rows removed from part: %d', changes());

.print "--------------------------------------------------------"
.print "[*] Executing: Purging intersection data from 'session_share'..."
DELETE FROM session_share WHERE session_id IN (SELECT id FROM to_delete);
SELECT printf('[-] Rows removed from session_share: %d', changes());

.print "--------------------------------------------------------"
.print "[*] Executing: Purging links from 'session_message'..."
DELETE FROM session_message WHERE session_id IN (SELECT id FROM to_delete);
SELECT printf('[-] Rows removed from session_message: %d', changes());

.print "--------------------------------------------------------"
.print "[*] Executing: Purging historical query parameters from 'session_input'..."
DELETE FROM session_input WHERE session_id IN (SELECT id FROM to_delete);
SELECT printf('[-] Rows removed from session_input: %d', changes());

.print "--------------------------------------------------------"
.print "[*] Executing: Purging core items from 'message'..."
DELETE FROM message WHERE id IN (SELECT id FROM messages_to_delete);
SELECT printf('[-] Rows removed from message: %d', changes());

.print "--------------------------------------------------------"
.print "[*] Executing: Purging core containers from 'session'..."
DELETE FROM session WHERE id IN (SELECT id FROM to_delete);
SELECT printf('[-] Rows removed from session: %d', changes());

COMMIT;
PRAGMA foreign_keys = ON;

-- Reclaim system storage blocks (Executed outside of transaction block)
.print "--------------------------------------------------------"
.print "[*] Executing: Defragmenting and shrinking database file size via VACUUM..."
VACUUM;
EOF

echo "--------------------------------------------------------"
NEW_SIZE=$(ls -lh "$DB_PATH" | awk '{print $5}')
echo "[+] Verbose cleanup execution complete!"
echo "[+] New opencode.db file size on disk: $NEW_SIZE"
