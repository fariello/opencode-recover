#!/bin/bash

DB_PATH="$HOME/.local/share/opencode/opencode.db"
BACKUP_FILE="$HOME/.local/share/opencode/tmp_rebuild_backup.sql"

# 1. Validate database file presence
if [ ! -f "$DB_PATH" ]; then
    echo "[-] Error: OpenCode database not found at $DB_PATH"
    exit 1
fi

# 2. Check for active application locks
if command -v lsof >/dev/null 2>&1; then
    if lsof "$DB_PATH" >/dev/null 2>&1; then
        echo "[!] Warning: OpenCode is active. Please close OpenCode before running the rebuild."
        exit 1
    fi
fi

# 3. Cross-platform file sizing helper (Using native awk to avoid 'bc' dependencies)
get_size() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        stat -f "%z" "$1" 2>/dev/null || wc -c < "$1" | awk '{print $1}'
    else
        stat -c "%s" "$1" 2>/dev/null || wc -c < "$1" | awk '{print $1}'
    fi
}

human_size() {
    local bytes=$1
    if [ -z "$bytes" ] || [ "$bytes" -le 0 ]; then
        echo "0 KB"
        return
    fi
    awk -v b="$bytes" 'BEGIN {
        if (b >= 1073741824) printf "%.2f GB\n", b/1073741824
        else if (b >= 1048576) printf "%.2f MB\n", b/1048576
        else printf "%.2f KB\n", b/1024
    }'
}

START_BYTES=$(get_size "$DB_PATH")
echo "[*] Initial opencode.db file size: $(human_size $START_BYTES)"
echo "--------------------------------------------------------"

# 4. Step 1: Export active records safely using absolute pathing
echo "[*] Step 1/4: Exporting active records to a structural text dump..."
if sqlite3 "$DB_PATH" .dump > "$BACKUP_FILE"; then
    DUMP_BYTES=$(get_size "$BACKUP_FILE")
    echo "[+] Done. Logical structural data size is: $(human_size $DUMP_BYTES)"
else
    echo "[-] Error: Failed to dump database data contents."
    rm -f "$BACKUP_FILE"
    exit 1
fi
echo "--------------------------------------------------------"

# 5. Step 2: Safely archive the entire WAL file family group together
echo "[*] Step 2/4: Safely archiving the bloated database file family..."
[ -f "${DB_PATH}-wal" ] && mv "${DB_PATH}-wal" "${DB_PATH}-wal.old"
[ -f "${DB_PATH}-shm" ] && mv "${DB_PATH}-shm" "${DB_PATH}-shm.old"
mv "$DB_PATH" "${DB_PATH}.old"
echo "[+] Done. Bloated environment moved cleanly to: ${DB_PATH}.old"
echo "--------------------------------------------------------"

# 6. Step 3: Rebuild a clean database file from scratch
echo "[*] Step 3/4: Streaming clean data text into a new database file..."
if sqlite3 "$DB_PATH" < "$BACKUP_FILE"; then
    echo "[+] Done. New database file compiled successfully."
else
    echo "[-] Error: Failed to compile database from layout dump."
    echo "[!] Safe Failure Recovery: Restoring your original bloated database configuration..."
    # Clear the failed compile file attempt if any parts exist
    rm -f "$DB_PATH" "${DB_PATH}-wal" "${DB_PATH}-shm"
    # Safely swap back the pristine historical setup
    mv "${DB_PATH}.old" "$DB_PATH"
    [ -f "${DB_PATH}-wal.old" ] && mv "${DB_PATH}-wal.old" "${DB_PATH}-wal"
    [ -f "${DB_PATH}-shm.old" ] && mv "${DB_PATH}-shm.old" "${DB_PATH}-shm"
    rm -f "$BACKUP_FILE"
    exit 1
fi
echo "--------------------------------------------------------"

# 7. Step 4: Clean up temporary dump and legacy structural files
echo "[*] Step 4/4: Cleaning up temporary assets..."
rm -f "$BACKUP_FILE"
rm -f "${DB_PATH}-wal.old" "${DB_PATH}-shm.old"
echo "[+] Done. Temporary working elements cleared safely."
echo "--------------------------------------------------------"

# 8. Final metrics comparison
END_BYTES=$(get_size "$DB_PATH")
echo "[+] Database optimization complete!"
echo "[+] Start Size: $(human_size $START_BYTES)"
echo "[+] Final Size: $(human_size $END_BYTES)"
echo "--------------------------------------------------------"
echo "[!] Success: The original bloated backup is safely kept at: ${DB_PATH}.old"
echo "[?] Once you run an 'opencode' task and check your context, you can safely remove it."
