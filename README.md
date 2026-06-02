# opencode-recover

Recover and restart broken [opencode](https://opencode.ai) sessions.

When an opencode session crashes, hits context overflow, or fails during `/compact`, this script extracts the conversation history and produces restart-ready Markdown files — optionally compacting them via an LLM API call so a fresh agent can continue exactly where you left off.

**Use at your own risk.** This is an independent utility, not affiliated with the opencode project.

---

## TL;DR / Quickstart

```bash
# Copy the script somewhere on your PATH
curl -o ~/.local/bin/opencode_recover_session.py \
  https://raw.githubusercontent.com/fariello/opencode-recover/main/opencode_recover_session.py
chmod +x ~/.local/bin/opencode_recover_session.py

# Recover interactively (lists sessions, lets you pick one)
opencode_recover_session.py

# Recover a specific session, keep only the last 50 interactions
opencode_recover_session.py -s SESSION_ID -mi 50

# Same, but also compact via a cheap model
opencode_recover_session.py -s SESSION_ID -mi 50 -m provider/model_id

# Then in a fresh opencode session, tell the agent:
#   "read and execute opencode-recovery/opencode-recovery-SESSION_ID-TIMESTAMP.compacted.md"
```

No dependencies beyond Python 3.10+ and the `opencode` CLI.

---

## What It Does

1. Lists your opencode sessions (via `opencode session list`)
2. Exports the selected session to a temp file (via `opencode export`)
3. Parses the export JSON, extracting only user/assistant conversation turns
4. Consolidates consecutive same-role messages into single blocks
5. Strips metadata noise (IDs, token counts, model names, costs, paths)
6. Optionally truncates to the N most recent interactions or lines
7. Generates three Markdown files:
   - `*.transcript.md` — Clean consolidated transcript
   - `*.restart.md` — Transcript wrapped with agent instructions
   - `*.compact-prompt.md` — Full prompt ready for LLM compaction
8. Optionally calls an LLM API to produce a compact restart document (`*.compacted.md`)
9. Shows you exactly what to tell your fresh opencode agent

---

## Requirements

**CLI tool** (`opencode_recover_session.py`):
- Python 3.10+
- `opencode` CLI on PATH
- No third-party packages — stdlib only
- For `--use-model`: an OpenAI-compatible LLM provider in `~/.config/opencode/opencode.json`

**TUI app** (`orsession`):
- Python 3.10+
- `opencode` CLI on PATH
- `textual` and `rich` (installed automatically via `pip install .`)

---

## Installation

It's a single file. Pick your method:

```bash
# Option 1: curl to a bin directory
curl -o ~/.local/bin/opencode_recover_session.py \
  https://raw.githubusercontent.com/fariello/opencode-recover/main/opencode_recover_session.py
chmod +x ~/.local/bin/opencode_recover_session.py

# Option 2: clone the repo
git clone https://github.com/fariello/opencode-recover.git
cd opencode-recover
chmod +x opencode_recover_session.py

# Option 3: just copy it wherever you want
cp opencode_recover_session.py /wherever/you/like/
```

### Interactive TUI (orsession)

For a full interactive experience with session browsing, drill-down previews,
and guided recovery/compaction workflows, install the `orsession` TUI app:

```bash
# Install from the repo (requires Python 3.10+ and pip)
git clone https://github.com/fariello/opencode-recover.git
cd opencode-recover
pip install .

# Launch the TUI
orsession

# Or point it at a different project
orsession -d /path/to/other/project
```

`orsession` uses [textual](https://textual.textualize.io/) for the terminal UI.
It provides:
- Sortable session list with recovery status indicators
- Session detail with metadata, cost, tokens, and conversation previews
- Full scrollable transcript viewer
- Recovery wizard with truncation controls and token warnings
- Model selection with live cost estimates and search/filter
- Context file selection for chaining recoveries across sessions
- LLM compaction with progress display
- Recovery file browser with view/delete

The CLI tool (`opencode_recover_session.py`) remains fully standalone with
zero dependencies — use it when you want scripting, CI integration, or
don't want to install packages.

---

## Usage Examples

### Basic interactive recovery

```bash
opencode_recover_session.py
```

Lists all sessions, lets you pick one, generates recovery files in `./opencode-recovery/`.

### Non-interactive with a known session ID

```bash
opencode_recover_session.py -s ses_abc123def456
```

### Recover a session from a different project directory

```bash
opencode_recover_session.py -d "/path/to/project"
```

opencode stores sessions per-project. Use `-d` when recovering a session that was started in a directory other than your current working directory.

### Truncate to the most recent N interactions

```bash
opencode_recover_session.py -s SESSION_ID -mi 50
```

Keeps only the 50 most recent back-and-forth exchanges (from the tail). This is measured against the *output* file, not raw input.

### Truncate by output line count

```bash
opencode_recover_session.py -s SESSION_ID -ml 2000
```

Keeps enough recent turns to fit within ~2000 lines of rendered output.

### Compact via LLM

```bash
opencode_recover_session.py -s SESSION_ID -m uri/its_direct/pt1-qwen3-32b-us
```

After generating recovery files, sends the compact prompt to the specified model and writes a `*.compacted.md` file. Shows estimated cost and asks for confirmation before sending.

### See available models and costs

```bash
opencode_recover_session.py --show-models
```

Displays a table of all models from your opencode config with pricing and API compatibility status.

### Chain recoveries (session crashed twice)

```bash
# First recovery produced session1.compacted.md
# Second session (started from session1.compacted.md) also crashed
# Include the first recovery as prior context:
opencode_recover_session.py -s SESSION_ID_2 \
  -ic ./opencode-recovery/session1.compacted.md
```

Prior context is prepended to the transcript so the compaction model sees the full chain.

### Write output to specific paths

```bash
opencode_recover_session.py -s SESSION_ID \
  -ot ./out/transcript.md \
  -or ./out/restart.md \
  -oc ./out/compact-prompt.md
```

Directories are created if they don't exist.

### Clean up old files

```bash
# Clean only (no export, no recovery):
opencode_recover_session.py -s SESSION_ID -c --clean-previous

# Clean then recover:
opencode_recover_session.py -s SESSION_ID -c --clean-previous -mi 50
```

- `-c` / `--clean` removes leftover temp directories from `/tmp`
- `--clean-previous` removes prior recovery output files for the selected session

When only clean flags are specified (no `--use-model`, `--input-*`, or `--keep-temp`), the script cleans and exits without exporting.

### View the compaction prompt template

```bash
opencode_recover_session.py --show-compaction-prompt
```

---

## All Arguments

| Short | Long | Description |
|-------|------|-------------|
| `-s` | `--session` | Session ID (skips interactive selection) |
| `-d` | `--session-dir` | Project directory where the session was run |
| `-o` | `--out` | Output directory (default: `./opencode-recovery`) |
| `-k` | `--keep-temp` | Preserve the temporary exported JSON |
| `-c` | `--clean` | Remove leftover temp directories |
| | `--clean-previous` | Remove prior recovery files for the session |
| `-t` | `--include-tools` | Include tool/function messages in extraction |
| | `--all-roles` | Include system and tool roles (not just user/assistant) |
| `-ml` | `--max-lines` | Max output lines (truncates from tail) |
| `-mi` | `--max-interactions` | Max interactions (truncates from tail) |
| `-ic` | `--input-compact` | Prior compacted file as context (repeatable) |
| `-ir` | `--input-restart` | Prior restart file as context (repeatable) |
| `-it` | `--input-transcript` | Prior transcript file as context (repeatable) |
| `-oc` | `--output-compact` | Explicit output path for compact prompt |
| `-or` | `--output-restart` | Explicit output path for restart file |
| `-ot` | `--output-transcript` | Explicit output path for transcript |
| | `--show-models` | Display available models and exit |
| | `--show-compaction-prompt` | Display the compaction prompt template and exit |
| `-m` | `--use-model` | Compact via LLM (format: `provider/model_id`) |
| `-v` | `--verbose` | Increase verbosity (`-v` or `-vv`) |

---

## Output Files

| File | Purpose |
|------|---------|
| `*.transcript.md` | Clean consolidated transcript — just user/assistant turns, noise removed |
| `*.restart.md` | Transcript wrapped with instructions telling a fresh agent how to resume |
| `*.compact-prompt.md` | Full prompt for LLM compaction (includes transcript + structured instructions) |
| `*.compacted.md` | LLM-generated compact restart document (only if `--use-model` is used) |

### Which file to use?

- **Quick restart (no LLM cost):** Tell your fresh agent to read `*.restart.md`
- **Best results (costs a few cents):** Use `--use-model` and tell your fresh agent to read `*.compacted.md`
- **Manual review:** Read `*.transcript.md` yourself to see what happened

---

## How Truncation Works

When a session is large (>2500 lines or >100 interactions), the script prompts you to truncate. You can also specify limits directly with `-ml` and `-mi`.

**Key design choices:**

- Truncation keeps the **most recent** turns (the tail). Older context is dropped.
- `--max-lines` refers to the **output file** line count, not raw input.
- `--max-interactions` counts user→assistant exchanges (a user message + the agent's response = 1 interaction).
- The more restrictive limit wins when both are specified.
- Prior context (`--input-*`) is **never** truncated — it's already compacted.

---

## LLM Compaction

The `--use-model` feature sends your session transcript to an LLM to produce a compact restart document. This is useful for very long sessions where even the truncated transcript would be too large for an agent to process efficiently.

### How it works

1. Reads your `~/.config/opencode/opencode.json` for provider/model/key configuration
2. Resolves the model you specified (exact match or substring)
3. Estimates input/output tokens (~4 chars/token heuristic) and cost
4. Shows the estimate and asks for confirmation
5. Sends a structured prompt with your transcript to the model's `/v1/chat/completions` endpoint
6. Writes the response as `*.compacted.md`
7. Reports actual tokens used and actual cost (from API response)

### Supported providers

Any provider using an OpenAI-compatible API (shown as `API: OK` in `--show-models`). This includes:

- Providers using `@ai-sdk/openai-compatible` (custom endpoints)
- Providers using `@ai-sdk/openai` (OpenAI itself)

Providers with non-compatible APIs (e.g., Google's `@ai-sdk/google`) are listed but cannot be used for compaction.

### API key formats

The script supports three formats for API keys in the config:

| Format | Example |
|--------|---------|
| `{env:VAR}` | `"apiKey": "{env:OPENAI_API_KEY}"` (opencode's preferred) |
| `${VAR}` | `"apiKey": "${OPENAI_API_KEY}"` |
| Literal | `"apiKey": "sk-abc123..."` |

### Security notes

- The script **refuses to send API keys to non-HTTPS endpoints** (except `localhost`/`127.0.0.1`)
- A data sensitivity notice is shown before sending
- Temp export files are created with `0600` permissions (owner-only)
- The script reads your opencode config which contains API keys — keep that file secured

---

## Chaining Recoveries

If your session crashes repeatedly, you can chain recoveries:

```
Session 1 → crashes → recover → session1.compacted.md
Session 2 (started from session1.compacted.md) → crashes → recover with --input-compact session1.compacted.md
```

The `--input-compact` (or `--input-restart`, `--input-transcript`) content is:
- Loaded before any `--clean-previous` runs
- Prepended to the transcript in the compact prompt with a labeled header
- **Not** truncated by `--max-lines`/`--max-interactions`
- Included in token/cost estimates
- Clearly marked as prior context so the compaction model treats it as established history

---

## Long Session Detection

When the extracted transcript exceeds 2500 lines or 100 interactions and no explicit `--max-*` flags are provided, the script interactively prompts:

```
This session is large:
  Transcript lines:  8,432
  Interactions:      247
  Total turns:       402

Truncation keeps only the most recent (tail) interactions.

Truncate output? [N]o / [l]ines / [i]nteractions / [b]oth:
```

In non-interactive mode (piped stdin), it proceeds without truncation and prints a note.

---

## Session Tail Preview

After extraction, the script displays the last 20 turns as a quick preview:

```
Session tail preview:
  ... (380 earlier turns omitted)
  U: Please commit and push changes.
  A: Done. All committed and pushed.
  U: Can you add a --dry-run flag?
  A: Let me add that to the argument parser...
```

This lets you verify the session wasn't truncated mid-conversation.

---

## How It Parses Exports

The script has two parsing paths:

1. **Native opencode format** (preferred): Understands the `{"info": {...}, "messages": [{"info": {"role": ...}, "parts": [...]}]}` structure. Only extracts text from `parts` with `type: "text"`, completely skipping metadata.

2. **Generic fallback**: For non-opencode or malformed exports, recursively walks JSON looking for role-bearing dictionaries and extracts text fields.

In both cases:
- Consecutive same-role turns are consolidated
- `[System: Empty message content sanitised to satisfy protocol]` lines are stripped
- Metadata (IDs, costs, tokens, model names, paths) is excluded

---

## Troubleshooting

### "opencode export produced no output"

The `opencode` CLI may require you to be in the project directory. Use `-d /path/to/project`.

### Export is truncated or invalid JSON

On WSL/Windows, `subprocess.PIPE` can truncate large outputs. This script works around that by writing stdout directly to a file instead of capturing via pipe.

### "No sessions were found"

Run `opencode session list --format json` manually in the project directory to verify sessions exist.

### API call fails with 401

Check that your API key in `~/.config/opencode/opencode.json` is correct. If using `{env:VAR}` format, ensure the environment variable is set.

### "Refusing to send API key to non-HTTPS endpoint"

The script blocks sending credentials over unencrypted connections. If you need to test against a local endpoint, use `localhost` or `127.0.0.1` in the base URL.

---

## License

BSD 3-Clause. See [LICENSE](LICENSE).

**Use at your own risk.** This tool reads your opencode session data and optionally sends it to external LLM APIs. Review the code and understand what it does before running it on sensitive projects.
