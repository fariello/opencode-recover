# orsession — Functional Specification

**Version:** 0.1.0-draft
**Date:** 2026-06-01
**Status:** Draft

---

## 1. Overview

`orsession` is an interactive terminal application (curses-based TUI) for
browsing, recovering, and compacting opencode sessions. It is a companion to
`opencode_recover_session.py` (the non-interactive CLI tool), sharing the same
core logic but providing a full drill-down, menu-driven experience.

### 1.1 Problem Statement

When an opencode session becomes unusable (compaction failure, context overflow,
fork confusion, crash), users need to:

1. Figure out *which* session to recover (titles are often unhelpful, forks
   create confusingly similar sessions, timestamps blur together).
2. Preview session content to confirm they have the right one.
3. Export and generate recovery files.
4. Optionally run LLM compaction for a concise restart document.
5. Chain recoveries from multiple prior sessions.

The existing CLI tool handles steps 3-5 well but requires users to already know
their session ID and desired options. `orsession` makes the entire workflow
discoverable and explorable.

### 1.2 Design Principles

- **Lightweight dependencies.** Uses `textual` and `rich` for the TUI layer.
  No heavyweight frameworks, databases, or network services beyond the
  opencode CLI and optional LLM API calls.
- **Graceful terminal handling.** Works in terminals as narrow as 80 columns.
  Adapts layout to available width. Never crashes on resize.
- **Non-destructive by default.** Read-only operations (browse, preview) never
  modify anything. Write operations (export, compact) always confirm first.
- **Escape hatch.** Every screen has a clear way back. `q` always quits.
  `Esc` or `b` returns to the previous screen.
- **Progressive disclosure.** Show summary first, details on demand. Don't
  overwhelm with information.

### 1.3 Target Environment

- **Linux:** Full support. Primary development platform.
- **macOS:** Full support. Tested on Terminal.app and iTerm2.
  Many target users are on macOS.
- **Windows (WSL):** Full support (treated as Linux).
- **Windows (native):** Stretch goal. `textual` supports Windows Console,
  and opencode installs natively via Chocolatey/Scoop/npm. However, opencode
  itself recommends WSL for "full compatibility," so native Windows is
  lower priority.
- Python 3.10+ (matches `opencode_recover_session.py`).
- Requires `opencode` CLI on PATH.
- Reads config from standard opencode config paths.

---

## 2. Invocation

```
orsession [OPTIONS]

Options:
  -d, --session-dir PATH    Directory where opencode sessions live
                            (default: current directory)
  -o, --out PATH            Output directory for recovery files
                            (default: ./opencode-recovery)
  -v, --verbose             Increase verbosity (debug info in status bar)
  --no-color                Disable color output
  --help                    Show help and exit
  --version                 Show version and exit
```

All options are optional. Without arguments, `orsession` launches in the
current directory and discovers sessions automatically.

---

## 3. Screen Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ [Title Bar]                                          [Clock/Help]│
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│                      [Content Area]                              │
│                                                                  │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│ [Status Bar / Breadcrumb]                                        │
├─────────────────────────────────────────────────────────────────┤
│ [Key Hints]                                                      │
└─────────────────────────────────────────────────────────────────┘
```

### 3.1 Common UI Elements

**Title Bar:** Application name + current screen title. Right-aligned: current
time (updates every minute) or help hint.

**Status Bar:** Breadcrumb trail showing navigation path (e.g.,
`Sessions > ses_abc > Preview`). Also displays transient status messages
(loading, errors) with auto-fade after 3 seconds.

**Key Hints:** Context-sensitive footer showing available keys. Always includes
`q:Quit`. Adapts to current screen.

**Timestamp Display Modes:** Timestamps throughout the app cycle through three
formats when the user presses `t`:

| Mode | Format | Example | Best for |
|------|--------|---------|----------|
| Short | `HH:MM` | `13:15` | Narrow terminals, same-day sessions |
| Medium | `Mon-DD HH:MM` | `May-30 13:15` | Most use cases |
| Long | `YYYY-MM-DD HH:MM:SS` | `2026-05-30 13:15:42` | Disambiguation |

Default: **Medium**. The mode is global and persists across screens within a
session. Pressing `t` anywhere cycles Short → Medium → Long → Short.

---

## 4. Screen Definitions

### 4.1 Session List (Home Screen)

The entry point. Shows all discovered sessions in a scrollable table.

```
┌─ orsession ─────────────────────────────────────── t:timestamps ─┐
│                                                                    │
│  Sessions in /home/user/VC/myproject (3 found)                    │
│                                                                    │
│  #  Title                        Updated        Created     Turns  │
│  ─────────────────────────────────────────────────────────────────│
│ >1  Fix auth bug                 May-30 13:15   May-30 10:00  94  │
│  2  Refactor DB layer            May-29 18:02   May-29 10:15 246  │
│  3  Add metrics export           May-28 09:30   May-28 08:00  62  │
│                                                                    │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ Sessions                                                           │
├────────────────────────────────────────────────────────────────────┤
│ Enter:Details  r:Recover  /:Search  s:Sort  t:Timestamps  q:Quit  │
└────────────────────────────────────────────────────────────────────┘
```

**Columns:**

| Column | Source | Notes |
|--------|--------|-------|
| # | Row number | For quick selection by typing number |
| Title | `session.title` | Truncated to fit. "(untitled)" if empty |
| Updated | `session.updated` | Respects timestamp mode |
| Created | `session.created` | Respects timestamp mode |
| Turns | Message count from export | Shown as "?" until export is loaded |

**Interactions:**

| Key | Action |
|-----|--------|
| `↑`/`↓`, `j`/`k` | Move cursor |
| `Enter` | Open Session Detail screen |
| `r` | Jump directly to Recover flow for highlighted session |
| `/` | Enter search/filter mode (filters by title substring, case-insensitive) |
| `s` | Cycle sort: Updated (desc) → Created (desc) → Title (asc) → Turns (desc) |
| `t` | Cycle timestamp display format |
| `1`-`9` | Quick-select session by number (if ≤9 sessions) |
| `q` | Quit application |

**Sorting:**
Default sort is by `updated` descending (most recent first). The current sort
column is indicated by a `▼` or `▲` marker in the header.

**Search/Filter:**
When `/` is pressed, a filter input appears at the bottom. Typing filters the
list in real-time. `Enter` confirms the filter (stays applied), `Esc` clears
the filter and exits search mode.

**Turn Count:**
The "Turns" column requires exporting each session, which is expensive. Options:
- Show `?` initially, populate lazily on first drill-down.
- Provide a key (`l` for "load all") to batch-fetch turn counts in background.
- Once a session has been exported (for preview or recovery), cache the count.

**Design decision:** Show `?` by default. Populate when a session is drilled
into. Cache in memory for the duration of the app run.

---

### 4.2 Session Detail Screen

Shown after selecting a session. Provides metadata and a content preview
without modifying anything on disk.

```
┌─ orsession ─────────────────────────────────────── t:timestamps ─┐
│                                                                    │
│  Fix authentication bug                                           │
│  ───────────────────────────────────────                          │
│  ID:        ses_abc123                                            │
│  Slug:      quiet-pixel                                           │
│  Created:   May-30 10:00:15                                       │
│  Updated:   May-30 13:15:42                                       │
│  Duration:  3h 15m                                                │
│  Agent:     build                                                 │
│  Model:     claude-opus-4.6-1m (uri)                              │
│  Turns:     94 (47 user, 47 assistant)                            │
│  Cost:      $3.37                                                 │
│  Tokens:    620K in / 10.7K out / 1.37M cache read                │
│  Changes:   +39 -6 across 1 file                                  │
│  Directory: /home/user/VC/myproject                                │
│                                                                    │
│  ── First exchanges ──────────────────────────────── May-30 10:00 │
│  U [10:00]: examine the main script, then look at ~/.config/op... │
│  A [10:01]: Let me look at the main script and the config file... │
│  U [10:03]: yes                                                   │
│                                                                    │
│  ── Last exchanges ───────────────────────────────── May-30 13:12 │
│  U [13:10]: Thanks. Let's add a couple features...                │
│  A [13:12]: Good ideas. Let me think through these features...    │
│  U [13:14]: What about having an --interactive flag instead...    │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ Sessions > Fix authentication bug                                  │
├────────────────────────────────────────────────────────────────────┤
│ r:Recover  p:Full preview  t:Timestamps  b:Back  q:Quit           │
└────────────────────────────────────────────────────────────────────┘
```

**Metadata Fields:**

| Field | Source | Notes |
|-------|--------|-------|
| ID | `info.id` | Full session ID |
| Slug | `info.slug` | Human-friendly slug (if available) |
| Created | `info.time.created` | Epoch ms → formatted |
| Updated | `info.time.updated` | Epoch ms → formatted |
| Duration | Computed | `updated - created`, shown as `Xh Ym` |
| Agent | `info.agent` | e.g., "build", "plan" |
| Model | `info.model.id` + `info.model.providerID` | Shortened display |
| Turns | Counted from `messages[]` | With role breakdown |
| Cost | `info.cost` | Formatted as `$X.XX` |
| Tokens | `info.tokens` | Formatted with K/M suffixes |
| Changes | `info.summary` | `+additions -deletions across N files` |
| Directory | `info.directory` | Project directory |

**Preview Sections:**

- **First exchanges:** The first 3 user/assistant text turns, showing
  timestamps and truncated content (one line per turn).
- **Last exchanges:** The last 3 user/assistant text turns, same format.

This gives the user a "bookend" view to identify the session without loading
the full transcript.

**Turn Preview Format:**

```
ROLE [TIMESTAMP]: First line of content, truncated to fit terminal width...
```

Where:
- `ROLE` is `U` (user) or `A` (assistant)
- `TIMESTAMP` respects the current timestamp mode (short: `HH:MM`, etc.)
- Content is collapsed to a single line (newlines → spaces), truncated with `...`

**Interactions:**

| Key | Action |
|-----|--------|
| `r` | Start Recover flow for this session |
| `p` | Open Full Preview (scrollable transcript) |
| `t` | Cycle timestamp format |
| `b` / `Esc` | Return to Session List |
| `q` | Quit |

---

### 4.3 Full Preview Screen

A scrollable view of the entire session transcript. Read-only. Useful for
finding the right session or understanding where it left off.

```
┌─ orsession ─────────────────────────────────── line 87/94 ───────┐
│                                                                    │
│  ### 42. User                                     [May-30 12:45]  │
│                                                                    │
│  Ok, run the tests again and make sure we didn't break anything.  │
│                                                                    │
│  ### 43. Assistant                                [May-30 12:46]  │
│                                                                    │
│  Running the test suite:                                          │
│                                                                    │
│  ```bash                                                          │
│  pytest tests/ -v                                                 │
│  ```                                                              │
│                                                                    │
│  All 47 tests pass. The connection pool refactoring is working    │
│  correctly with no regressions.                                   │
│                                                                    │
│  ### 44. User                                     [May-30 12:48]  │
│                                                                    │
│  Great, commit that with a good message.                          │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ Sessions > Fix auth bug > Preview                                  │
├────────────────────────────────────────────────────────────────────┤
│ ↑↓:Scroll  PgUp/PgDn  Home/End  /:Search  t:Timestamps  b:Back   │
└────────────────────────────────────────────────────────────────────┘
```

**Interactions:**

| Key | Action |
|-----|--------|
| `↑`/`↓`, `j`/`k` | Scroll line by line |
| `PgUp`/`PgDn`, `Ctrl-U`/`Ctrl-D` | Scroll half-page |
| `Home`/`g` | Jump to top |
| `End`/`G` | Jump to bottom |
| `/` | Search within transcript (highlights matches, `n`/`N` for next/prev) |
| `t` | Cycle timestamp format |
| `b` / `Esc` | Return to Session Detail |
| `q` | Quit |

**Position indicator:** Top-right shows `line X/Y` for orientation.

---

### 4.4 Recovery Screen

Guides the user through generating recovery files. This is a sequential wizard
with progress feedback.

```
┌─ orsession ─────────────────────────────────── Recovery Wizard ──┐
│                                                                    │
│  Recovering: Fix authentication bug (ses_abc123)                  │
│                                                                    │
│  Step 1 of 4: Configure                                          │
│  ─────────────────────────────────────────────                    │
│                                                                    │
│  Output directory: ./opencode-recovery                            │
│  Include tools:    No                                             │
│  Truncation:       None (94 turns, ~1,200 lines)                  │
│                                                                    │
│  The session is within normal size limits.                        │
│                                                                    │
│  ┌────────────────────────────────────────────┐                   │
│  │ [P] Proceed with these settings            │                   │
│  │ [t] Set max truncation (lines/interactions)│                   │
│  │ [i] Toggle include tools                   │                   │
│  │ [o] Change output directory                │                   │
│  │ [c] Clean previous recovery files first    │                   │
│  └────────────────────────────────────────────┘                   │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ Sessions > Fix auth bug > Recover                                  │
├────────────────────────────────────────────────────────────────────┤
│ P:Proceed  t:Truncate  i:Tools  o:Output  c:Clean  b:Back         │
└────────────────────────────────────────────────────────────────────┘
```

**Steps:**

1. **Configure** — Set options (truncation, include tools, output dir, clean).
2. **Export** — Run `opencode export`, show progress.
3. **Generate** — Create transcript, restart, and compact-prompt files.
4. **Complete** — Show results, offer to continue to compaction.

**Large Session Warning:**

If the session exceeds thresholds (>2500 lines or >100 interactions), the
Configure step highlights this:

```
  ⚠ This session is large (246 turns, ~4,800 lines).
    Compaction will be more effective with truncation.

  Suggested: Keep most recent 100 interactions (~2,400 lines)
```

**Step 4 (Complete) transitions to Compaction offer:**

```
  Step 4 of 4: Complete
  ─────────────────────────────────────────────

  Generated files:
    ✓ opencode-recovery-ses_abc-20260601-1315Z.transcript.md   (1,204 lines)
    ✓ opencode-recovery-ses_abc-20260601-1315Z.restart.md      (1,248 lines)
    ✓ opencode-recovery-ses_abc-20260601-1315Z.compact-prompt.md (1,312 lines)

  Output directory: ./opencode-recovery/

  Would you like to generate an LLM-compacted restart document?
  This sends the transcript to an API and returns a concise summary.

  ┌────────────────────────────────────────────┐
  │ [y] Yes, select a model                    │
  │ [n] No, I'm done                           │
  │ [v] View generated files first             │
  └────────────────────────────────────────────┘
```

---

### 4.5 Model Selection Screen

Presented when the user opts into compaction (either from Recovery Complete or
from a dedicated "Compact" action).

```
┌─ orsession ─────────────────────────────────── Model Selection ──┐
│                                                                    │
│  Select a model for LLM compaction                                │
│                                                                    │
│  Showing: OpenAI-compatible models only (22 available)            │
│  Sorted by: Cost (input, ascending)                               │
│                                                                    │
│  #   Model ID                               Name          $/M in  │
│  ─────────────────────────────────────────────────────────────────│
│  1   uri/its_direct/pt1-qwen3-32b-us        Qwen3 32B      $0.15 │
│> 2   uri/its_direct/pt2-devstral-2-123b-us  Devstral 2     $0.40 │
│  3   uri/its_direct/pt2-qwen3-coder-next    Qwen3 Coder    $0.50 │
│  4   uri/its_direct/pt2-mistral-large-3     Mistral Lg 3   $0.50 │
│  5   openai/gpt-4.1-mini                    GPT-4.1 Mini   $0.40 │
│  ...                                                              │
│                                                                    │
│  Estimated input: ~12,400 tokens                                  │
│  Estimated cost with selected model: $0.003                       │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ Sessions > Fix auth bug > Recover > Compact > Model               │
├────────────────────────────────────────────────────────────────────┤
│ Enter:Select  /:Search  s:Sort  t:Timestamps  b:Back  q:Quit     │
└────────────────────────────────────────────────────────────────────┘
```

**Search behavior:**

Pressing `/` opens a filter input. Typing filters models by case-insensitive
substring match against the full model ID and display name. The filtered list
renumbers dynamically.

```
  Filter: qwen
  Showing 3 of 22:

  #   Model ID                               Name            $/M in
  1   uri/its_direct/pt1-qwen3-32b-us        Qwen3 32B        $0.15
  2   uri/its_direct/pt2-qwen3-coder-next    Qwen3 Coder      $0.50
  3   uri/its_direct/pt2-qwen3-vl-235b-us    Qwen3 VL 235B    $0.53
```

**Interactions:**

| Key | Action |
|-----|--------|
| `↑`/`↓`, `j`/`k` | Move cursor |
| `Enter` | Select highlighted model, proceed to Context Selection |
| `/` | Search/filter models |
| `s` | Cycle sort: Cost asc → Name asc → Provider asc |
| `b` / `Esc` | Back |
| `q` | Quit |

**Cost estimate:** As the cursor moves, the bottom area updates with an
estimated cost based on the transcript size and the highlighted model's pricing.

---

### 4.6 Context Selection Screen

After model selection, before calling the API. Allows the user to include prior
context from existing recovery files or from other sessions.

```
┌─ orsession ─────────────────────────────────── Prior Context ────┐
│                                                                    │
│  Include prior session context in the compaction?                  │
│                                                                    │
│  Prior context helps the LLM understand work from earlier          │
│  sessions. This is useful when chaining recoveries across          │
│  multiple sessions (e.g., a session was forked or restarted).     │
│                                                                    │
│  Available recovery files in ./opencode-recovery/:                │
│                                                                    │
│  #  Type        Session              Date         Lines  Include? │
│  ────────────────────────────────────────────────────────────────  │
│  1  compacted   Refactor DB layer    May-29       142    [ ]      │
│  2  restart     Refactor DB layer    May-29       1248   [ ]      │
│  3  transcript  Refactor DB layer    May-29       4800   [ ]      │
│  4  compacted   Add metrics export   May-28       98     [ ]      │
│                                                                    │
│  Selected: (none)                                                  │
│                                                                    │
│  ┌────────────────────────────────────────────┐                   │
│  │ [Space] Toggle selection                   │                   │
│  │ [a] Add custom file path                   │                   │
│  │ [n] Recover another session to include     │                   │
│  │ [P] Proceed (with or without context)      │                   │
│  └────────────────────────────────────────────┘                   │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ Sessions > Fix auth bug > Recover > Compact > Context             │
├────────────────────────────────────────────────────────────────────┤
│ Space:Toggle  a:Add path  n:New recovery  P:Proceed  b:Back       │
└────────────────────────────────────────────────────────────────────┘
```

**File Discovery:**

The screen scans the output directory for files matching
`opencode-recovery-*.{compacted,restart,transcript,compact-prompt}.md` and
presents them with:
- File type (compacted, restart, transcript)
- Session title (parsed from filename or file content)
- Date (from filename timestamp)
- Line count

**"Recover another session" sub-flow (`n`):**

This is the key feature for handling forks and chained sessions. When pressed:

1. Returns to a session picker (same as Home Screen but with a "select for
   context" header).
2. User selects a session.
3. A quick recovery runs (export + generate transcript, no compaction).
4. The generated file is automatically added to the context selection list.
5. Returns to Context Selection screen.

**Interactions:**

| Key | Action |
|-----|--------|
| `↑`/`↓`, `j`/`k` | Move cursor |
| `Space` | Toggle inclusion of highlighted file |
| `a` | Prompt for a custom file path (text input at bottom) |
| `n` | Sub-flow: recover another session for context |
| `P` / `Enter` | Proceed to compaction confirmation |
| `b` / `Esc` | Back to Model Selection |
| `q` | Quit |

---

### 4.7 Compaction Confirmation & Progress Screen

Final confirmation before the API call, then shows progress and results.

```
┌─ orsession ─────────────────────────────────── Compaction ───────┐
│                                                                    │
│  Ready to compact                                                  │
│                                                                    │
│  Model:      Qwen3 32B (uri/its_direct/pt1-qwen3-32b-us)         │
│  Endpoint:   https://llmgw.its.uri.edu/v1                         │
│  Input:      ~12,400 tokens (estimated)                           │
│  Output:     ~2,500 tokens (estimated)                            │
│  Est. cost:  $0.003                                               │
│  Context:    1 prior file included                                 │
│                                                                    │
│  The session transcript will be sent to the API endpoint above.   │
│                                                                    │
│  ┌────────────────────────────────────────────┐                   │
│  │ [y] Confirm and send                       │                   │
│  │ [n] Cancel                                 │                   │
│  └────────────────────────────────────────────┘                   │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ ... > Compact > Confirm                                            │
├────────────────────────────────────────────────────────────────────┤
│ y:Confirm  n:Cancel  b:Back                                        │
└────────────────────────────────────────────────────────────────────┘
```

After confirmation, shows a progress indicator:

```
│  Calling API...                                                    │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ (waiting for response) │
│  Elapsed: 12s                                                      │
```

After completion:

```
│  Compaction complete!                                              │
│                                                                    │
│  Actual tokens:  11,842 in / 2,104 out                            │
│  Actual cost:    $0.0030                                          │
│  Output lines:   142                                               │
│                                                                    │
│  Saved to:                                                         │
│    ./opencode-recovery/opencode-recovery-ses_abc-20260601.compacted.md │
│                                                                    │
│  Save a copy to a different location?                              │
│  [Enter path, or press Enter to skip]                             │
│                                                                    │
│  > _                                                               │
```

If the user enters a path, the file is copied there. If they press Enter, it
skips.

---

### 4.8 Recovery File Browser Screen

Accessible from the main menu. Shows all existing recovery files in the output
directory with the ability to view, delete, or use them as context.

```
┌─ orsession ─────────────────────────────── Recovery Files ───────┐
│                                                                    │
│  Recovery files in ./opencode-recovery/ (7 files)                 │
│                                                                    │
│  #  Filename                                   Type      Date     │
│  ────────────────────────────────────────────────────────────────  │
│  1  ...-ses_abc-20260601-1315Z.transcript.md   transcript Jun-01  │
│  2  ...-ses_abc-20260601-1315Z.restart.md      restart    Jun-01  │
│  3  ...-ses_abc-20260601-1315Z.compact-prompt  compact-p  Jun-01  │
│  4  ...-ses_abc-20260601-1315Z.compacted.md    compacted  Jun-01  │
│  5  ...-ses_xyz-20260530-1802Z.compacted.md    compacted  May-30  │
│  6  ...-ses_xyz-20260530-1802Z.restart.md      restart    May-30  │
│  7  ...-ses_xyz-20260530-1802Z.transcript.md   transcript May-30  │
│                                                                    │
├────────────────────────────────────────────────────────────────────┤
│ Recovery Files                                                     │
├────────────────────────────────────────────────────────────────────┤
│ Enter:View  d:Delete  D:Delete all for session  b:Back  q:Quit    │
└────────────────────────────────────────────────────────────────────┘
```

**Interactions:**

| Key | Action |
|-----|--------|
| `Enter` | View file in scrollable pager |
| `d` | Delete highlighted file (with confirmation) |
| `D` | Delete all files for the same session (with confirmation) |
| `/` | Search/filter |
| `b` / `Esc` | Back to main menu |
| `q` | Quit |

---

## 5. Navigation Model

### 5.1 Screen Flow Diagram

```
                    ┌──────────────────┐
                    │   Session List   │ (Home)
                    └────────┬─────────┘
                             │ Enter
                    ┌────────▼─────────┐
                    │  Session Detail  │
                    └───┬─────────┬────┘
                        │ p       │ r
               ┌────────▼───┐  ┌──▼──────────────┐
               │Full Preview │  │ Recovery Wizard │
               └─────────────┘  └───────┬─────────┘
                                        │ (step 4: offer compact)
                                ┌───────▼─────────┐
                                │ Model Selection │
                                └───────┬─────────┘
                                        │ Enter
                                ┌───────▼──────────────┐
                                │ Context Selection    │
                                │  ├─ [n] → Session    │
                                │  │    Picker (sub)   │
                                │  └─ [a] → Path input │
                                └───────┬──────────────┘
                                        │ P
                                ┌───────▼─────────┐
                                │ Confirm & Run   │
                                └───────┬─────────┘
                                        │ (complete)
                                ┌───────▼─────────┐
                                │ Save Location   │
                                └───────┬─────────┘
                                        │
                                   (back to Home)
```

### 5.2 Global Keys

These work on every screen:

| Key | Action |
|-----|--------|
| `q` | Quit application (with confirmation if work is in progress) |
| `t` | Cycle timestamp display format |
| `?` | Show help overlay for current screen |

### 5.3 Back Navigation

- `b` or `Esc` always returns to the previous screen.
- On the Home Screen, `b` does nothing; `q` quits.
- During an API call in progress, `Esc` cancels (with confirmation).

---

## 6. Terminal Adaptivity

### 6.1 Width Handling

The app defines three layout tiers:

| Width | Tier | Behavior |
|-------|------|----------|
| ≥120 cols | Wide | Full column display, no truncation |
| 80-119 cols | Standard | Shortened model IDs, truncated titles |
| <80 cols | Narrow | Minimal columns, abbreviated headers |

Each screen defines its own column priority. Low-priority columns are hidden
first as the terminal narrows.

**Session List column priority:** Title > Updated > # > Turns > Created

**Model Selection column priority:** Model ID > Cost > # > Name

### 6.2 Height Handling

If content exceeds visible height, the content area becomes scrollable.
The position indicator updates (e.g., `[1-20 of 47]`).

Minimum supported terminal size: **80x24** (standard VT100). Below this, the
app displays a "terminal too small" message and waits for resize.

### 6.3 Resize Handling

The app responds to `SIGWINCH` (terminal resize signal) by redrawing all
visible elements. No crash, no corruption. The curses `resizeterm()` call
handles this.

---

## 7. Data Flow & Caching

### 7.1 Session Data Lifecycle

```
opencode session list --format json
         │
         ▼
    Session metadata (cached for app lifetime)
         │
         │ (on drill-down or recover)
         ▼
opencode export SESSION_ID → temp file
         │
         ▼
    Parsed export (cached in memory by session ID)
         │
         ▼
    Turn extraction + preview generation
```

**Cache policy:**
- Session list: Fetched once at startup. Refreshable with `R` on Home Screen.
- Session exports: Cached in memory after first load. Temp files cleaned on exit.
- Recovery files on disk: Re-scanned when entering the File Browser.

### 7.2 Temporary Files

Exports are written to a temp directory (`tempfile.mkdtemp`) and cleaned up
when the app exits (including on `SIGINT`/`SIGTERM`). If `--keep-temp` is
specified (or a future config option), they persist.

### 7.3 Config Loading

The opencode config (`opencode.json` / `opencode.jsonc`) is loaded once at
startup for model information. Supports:
- `{env:VAR}` expansion
- `{file:PATH}` expansion (reads API keys from files)
- JSONC comments (for `.jsonc` files)

---

## 8. Error Handling

### 8.1 Principles

- **Never crash to raw terminal.** All exceptions are caught, the curses
  wrapper is torn down cleanly, and a readable error is printed.
- **Recoverable errors show in the status bar.** E.g., "Export failed: opencode
  returned exit code 1" — the user can try again or pick a different session.
- **Fatal errors exit gracefully.** E.g., "opencode not found on PATH" — shown
  as a full-screen error message with instructions.

### 8.2 Specific Error Scenarios

| Scenario | Handling |
|----------|----------|
| `opencode` not on PATH | Full-screen error on startup, exit code 1 |
| No sessions found | Informational screen with guidance (check --session-dir) |
| Export produces invalid JSON | Status bar warning + offer raw-text fallback |
| Export is truncated | Status bar warning, proceed with partial data |
| API call fails (HTTP error) | Show error, return to model selection |
| API call times out | Show timeout message, offer retry |
| API returns empty response | Show error, offer retry with different model |
| Terminal too small | "Resize terminal" overlay, wait for resize |
| Permission denied on output dir | Prompt for alternative path |
| Disk full | Error in status bar, suggest alternative location |

### 8.3 API Call Cancellation

During an API call (which can take 30-120 seconds), the user can press `Esc`
or `Ctrl-C`. This:
1. Shows "Cancelling..." in the progress area.
2. Closes the HTTP connection (if possible).
3. Returns to the previous screen.
4. Does not write any output file.

---

## 9. Session Identification Guidance

### 9.1 The Fork Problem

When a user forks a session (via `--fork` or similar), they end up with
multiple sessions that share history. The newer forked session may be
*shorter* (truncated at the fork point) while the original continues.

**How `orsession` helps:**

1. **Duration column** in Session List — a short duration + recent creation
   suggests a fork.
2. **Session Detail** shows the model and agent, which may differ between
   original and fork.
3. **First/last exchange preview** — comparing the tail of two sessions
   reveals which continued further.
4. **Sort by created vs. updated** — forks have close `created` times but
   may have very different `updated` times.

### 9.2 Visual Indicators

The Session List could include indicators for unusual patterns:

| Indicator | Meaning |
|-----------|---------|
| `◆` | Session has existing recovery files |
| `⑂` | Likely fork (same project, created within 5min of another session) |
| `✗` | Export is known to be truncated/invalid |

These are shown as a single-character column before the title.

---

## 10. Accessibility & Usability

### 10.1 Color Scheme

Colors are used for emphasis but never as the sole carrier of information.
All states are distinguishable without color (via text labels, position, or
symbols).

| Element | Color | Fallback |
|---------|-------|----------|
| Cursor/highlight row | Reverse video | Reverse video (works everywhere) |
| Headers | Bold | Bold |
| Timestamps | Dim/grey | Normal weight |
| Errors | Red + bold | Bold + `ERROR:` prefix |
| Warnings | Yellow | `WARNING:` prefix |
| Success | Green | `OK:` prefix |
| User turns (preview) | Cyan | `U:` prefix |
| Assistant turns (preview) | Default | `A:` prefix |
| Disabled/unavailable | Dim | `(n/a)` suffix |

`--no-color` disables all color attributes, using only bold and reverse video.

### 10.2 Keyboard-Only Operation

The entire app is operable without a mouse. Every action has a single-key
shortcut visible in the key hints bar.

### 10.3 Screen Reader Compatibility

While curses apps are inherently visual, the app:
- Uses logical layout (not absolute cursor positioning for decoration).
- Provides meaningful status bar text that summarizes the current state.
- Avoids animation or rapid screen updates (except the API progress timer).

---

## 11. Recovery Status Indicators

### 11.1 Session List Status Column

The Session List includes a status column showing what recovery artifacts
already exist for each session. This lets users immediately see which sessions
have been processed and which need attention.

```
  #  St  Title                        Updated        Created     Turns
  ──────────────────────────────────────────────────────────────────────
 >1  ●◗  Fix auth bug                 May-30 13:15   May-30 10:00  94
  2  ●   Refactor DB layer            May-29 18:02   May-29 10:15 246
  3  ○   Add metrics export           May-28 09:30   May-28 08:00  62
  4  ⑂○  Forked: Fix auth bug         May-30 11:00   May-30 10:58  32
```

**Status icons:**

| Icon | Meaning |
|------|---------|
| `○` | No recovery files exist |
| `●` | Recovery files exist (transcript + restart + compact-prompt) |
| `◗` | Compacted file exists (full recovery complete) |
| `⑂` | Likely fork (same project, created within 5 min of another session) |
| `✗` | Previous export was truncated/invalid (known bad) |

The `St` column combines up to 2 icons. The indicators are derived by
scanning the output directory for files matching the session ID pattern.

### 11.2 Quick Compact Shortcut

Pressing `c` on any session in the Session List triggers a streamlined
recover-and-compact flow:

1. If recovery files already exist (`●`), skip export/generate — reuse them.
2. If no recovery files exist (`○`), run export + generate first.
3. Jump directly to Model Selection.
4. After model selection, proceed to Context Selection → Confirm → API call.

This optimizes the common case: "I just want the compacted version." Users
who want more control use `Enter` → detail → `r` for the full wizard.

---

## 12. Token & Size Warnings

### 12.1 Estimation Method

Token count is estimated at ~4 characters per token (English text heuristic).
This is intentionally conservative — actual tokenization varies by model.

### 12.2 Warning Thresholds

When the user is about to send a transcript for compaction, the app evaluates
the estimated input token count and displays an appropriate warning:

| Estimated Tokens | Level | Display |
|------------------|-------|---------|
| <32K | None | No extra messaging |
| 32K–64K | Info | "Large input — may be slow or costly" |
| 64K–128K | Warning | "Exceeds many models' context windows. Consider truncation." |
| >128K | Strong | "Most models will reject or silently truncate. Truncation strongly recommended." |

### 12.3 Behavior at Warning Levels

- **Info:** Yellow text in the cost estimate area. User can proceed freely.
- **Warning:** Yellow highlight. The app suggests a truncation (e.g.,
  "Truncating to 100 interactions would bring this to ~45K tokens"). User
  must explicitly confirm to proceed without truncation.
- **Strong:** Red highlight. The app requires explicit override (`!` key)
  to proceed. Message:
  "Input exceeds 128K tokens. If you don't choose how to truncate,
  the model will decide for you (or reject the input entirely).
  You'll get better results by truncating intentionally."

### 12.4 Truncation Suggestion Logic

When a warning triggers, the app calculates what `--max-interactions` value
would bring the token count below 64K and presents it as a suggestion:

```
  ⚠ Estimated input: ~98,000 tokens (exceeds 64K threshold)

  Suggestion: Truncate to the most recent 65 interactions (~58K tokens)

  [t] Apply suggested truncation
  [T] Set custom truncation
  [!] Proceed anyway (may fail)
  [b] Back
```

---

## 13. File Structure

### 13.1 Project Layout

```
orsession/
├── orsession.py          — Entry point (curses app)
├── orsession_core.py     — Shared logic (extraction, rendering, API calls)
├── requirements.txt      — Dependencies (if any beyond stdlib)
└── tests/                — Test suite (future)
```

The CLI tool `opencode_recover_session.py` remains standalone in the repo
root. It does NOT depend on the `orsession` package.

### 13.2 Internal Architecture (orsession.py)

```python
# Section 1: Imports, constants, type definitions
# Section 2: App state and data models
# Section 3: Curses framework
#   - class App (main loop, screen stack, global state)
#   - class Screen (abstract base: draw, handle_key, on_enter, on_leave)
#   - class TextInput (reusable inline text input widget)
#   - class ProgressBar (reusable progress display)
#   - class Overlay (modal dialog/help)
# Section 4: Screen implementations
#   - class SessionListScreen
#   - class SessionDetailScreen
#   - class FullPreviewScreen
#   - class RecoveryWizardScreen
#   - class ModelSelectionScreen
#   - class ContextSelectionScreen
#   - class CompactionScreen
#   - class FileBrowserScreen
#   - class HelpOverlay
# Section 5: Entry point and argument parsing
```

### 13.3 Code Sharing with opencode_recover_session.py

`orsession_core.py` contains the shared logic:
- Session listing (opencode CLI interaction)
- Export parsing and turn extraction
- Transcript rendering
- Config loading, model extraction, env/file expansion
- API call logic
- File I/O utilities

Both `orsession.py` (TUI) and `opencode_recover_session.py` (CLI) import
from `orsession_core.py`. The CLI tool has a fallback: if the import fails,
it uses its own bundled copy of the logic (so it remains distributable as a
single file).

### 13.4 Dependencies

Unlike `opencode_recover_session.py` (which is deliberately stdlib-only for
single-file portability), `orsession` is a proper application that can use
external packages where they add value.

**TUI framework decision:**

| Option | Pros | Cons |
|--------|------|------|
| Raw `curses` | No deps, full control, stdlib | Tedious, low-level, poor Unicode/color support, manual widget building |
| `textual` | Modern, rich widgets, responsive layout, mouse support, great docs | Heavier dep tree (requires `rich`) |
| `urwid` | Mature, widget-based, lighter than textual | Older API, less actively developed |
| `blessed` | Thin curses wrapper, better API | Still mostly manual widget building |

**Recommended: `textual`** — It provides a modern component model (CSS-like
styling, reactive data binding, built-in widgets for tables, trees, inputs,
and scrollable text). The development speed advantage over raw curses is
substantial, and the resulting app will look significantly better. The dep
cost (`textual` + `rich`) is acceptable for an installed application.

If `textual` proves too heavy or introduces issues, fall back to `blessed`
(which provides a nicer curses wrapper without a full framework).

| Package | Purpose | Required? |
|---------|---------|-----------|
| `textual` | TUI framework (rendering, widgets, layout, input) | Yes |
| `rich` | Terminal formatting (dependency of textual) | Yes (transitive) |
| `structlog` | Structured JSON logging | Optional (falls back to stdlib logging) |

---

## 14. Logging

### 14.1 Approach

`orsession` writes a JSON-lines log file for debugging and auditing. Each
line is a self-contained JSON object with a timestamp, event type, and
relevant data.

### 14.2 Log Location

```
~/.local/share/orsession/orsession.log
```

Created on first write. Directory created if needed. File is appended to
(never truncated by the app). Users can delete it freely.

### 14.3 Log Events

| Event | When | Data |
|-------|------|------|
| `app_start` | Application launch | version, args, terminal size |
| `session_list` | Sessions fetched | count, session_dir |
| `session_export` | Export completed | session_id, export_size_bytes, duration_ms |
| `recovery_generate` | Files generated | session_id, output_paths, turn_count |
| `compaction_start` | API call initiated | model_id, estimated_tokens, estimated_cost |
| `compaction_complete` | API call succeeded | actual_tokens, actual_cost, output_lines, duration_ms |
| `compaction_error` | API call failed | model_id, error_type, error_message |
| `file_write` | Any file written | path, size_bytes |
| `app_exit` | Application exit | reason (quit/error/signal) |

### 14.4 Log Format

```json
{"ts": "2026-06-01T13:15:42.123Z", "event": "compaction_complete", "session_id": "ses_abc", "model": "uri/its_direct/pt1-qwen3-32b-us", "tokens_in": 11842, "tokens_out": 2104, "cost": 0.003, "duration_ms": 14200, "output": "./opencode-recovery/...compacted.md"}
```

### 14.5 Verbosity Levels

| Flag | Behavior |
|------|----------|
| (none) | Log significant operations only (exports, compactions, errors) |
| `-v` | Add info-level events (screen transitions, file scans) |
| `-vv` | Add debug-level events (key presses, render timing) |

In `-v` and `-vv` modes, a debug indicator appears in the title bar showing
the log file path.

### 14.6 pubrun Integration (Future)

pubrun is not used in v1. A future version could optionally wrap compaction
API calls with `pubrun exec` for execution telemetry, enabling `pubrun diff`
between compaction runs and `pubrun report` for cost tracking over time. This
would be gated behind a `--pubrun` flag or config option.

---

## 15. Session Scope

### 15.1 Project-Scoped Sessions (v1)

`opencode session list` returns sessions scoped to the current working
directory's project (identified by a project hash of the directory path).
`orsession` respects this scoping:

- Default: sessions for the CWD project.
- `--session-dir PATH`: sessions for the specified project directory.

### 15.2 Cross-Project Discovery (Future)

opencode stores session data in `~/.local/share/opencode/` (or platform
equivalent), keyed by project ID (a hash). A future version could:

1. Scan the opencode data directory for all project IDs.
2. Resolve project IDs back to directory paths (if stored in session metadata).
3. Present a project picker before the session list.

This is out of scope for v1 because:
- The opencode data directory layout is an implementation detail that may change.
- Session metadata includes the `directory` field, but discovering all sessions
  requires reading the internal DB/filesystem, not just the CLI.
- Users who need cross-project recovery can run `orsession -d /other/project`.

### 15.3 Multiple --session-dir (v1 Stretch)

Accept multiple `-d` flags to show sessions from several projects in one list:

```
orsession -d /project/alpha -d /project/beta
```

Sessions would be grouped by project with a visual separator. This is simpler
than full cross-project discovery and gives users explicit control.

---

## 16. Configuration

### 16.1 Runtime Config (Future)

A potential `~/.config/orsession/config.json` could store:

```json
{
  "default_timestamp_mode": "medium",
  "default_sort": "updated_desc",
  "preferred_model": "uri/its_direct/pt1-qwen3-32b-us",
  "output_dir": "./opencode-recovery",
  "auto_clean_temp": true,
  "token_warning_threshold": 64000
}
```

**For v1.0:** No config file. All preferences are set via CLI flags or
in-app toggles (reset each run). Config file is a v2 feature.

### 16.2 Environment Variables

| Variable | Purpose |
|----------|---------|
| `ORSESSION_OUTPUT_DIR` | Default output directory |
| `ORSESSION_SESSION_DIR` | Default session directory |
| `ORSESSION_LOG_LEVEL` | Log verbosity (0, 1, 2) |
| `NO_COLOR` | Disable color (standard) |
| `TERM` | Terminal type (respected by curses) |

---

## 17. Future Considerations (Out of Scope for v1)

These are noted but explicitly **not** part of the initial implementation:

1. **Session diffing** — side-by-side comparison of two sessions to identify
   forks and divergence points.
2. **Batch recovery** — recover multiple sessions in one operation.
3. **Session tagging/annotation** — user-applied labels stored alongside
   recovery files.
4. **Remote sessions** — browse sessions from a remote machine via SSH.
5. **Advanced fork detection** — heuristic grouping with shared-history
   analysis (compare first N turns).
6. **Compaction quality scoring** — re-read the compacted output and rate its
   completeness.
7. **Integration with opencode** — launch opencode with the compacted context
   pre-loaded (e.g., `opencode --context ./compacted.md`).
8. **Mouse support** — curses supports mouse events; could be added later.
9. **Config file** — persistent user preferences (see 16.1).
10. **Plugin system** — custom screens or recovery strategies.
11. **pubrun integration** — wrap compaction API calls for telemetry (see 14.6).
12. **Multi-project view** — full cross-project session discovery (see 15.2).
13. **Export caching** — persist exports between app runs to avoid re-exporting
    (would need cache invalidation based on session.updated timestamp).

---

## 18. Acceptance Criteria (v1.0)

The initial release is complete when:

### Core Navigation
- [ ] Application launches and shows session list from current directory.
- [ ] Application launches with `--session-dir` pointing elsewhere.
- [ ] Session list is scrollable, sortable, and filterable.
- [ ] Timestamp toggle (`t`) cycles all three formats globally.
- [ ] `Esc`/`b` back navigation works consistently on all screens.
- [ ] `q` quits cleanly from any screen.
- [ ] `?` shows context-sensitive help overlay.
- [ ] Terminal resize doesn't crash.
- [ ] Terminals as small as 80x24 work (degraded but functional).

### Session Browsing
- [ ] Session list shows recovery status indicators (○ ● ◗ ⑂).
- [ ] Session Detail shows metadata and first/last exchange previews.
- [ ] Full Preview shows scrollable transcript with in-content search.
- [ ] Fork indicator appears for sessions created near each other.

### Recovery
- [ ] Recovery Wizard exports and generates all three file types.
- [ ] Recovery handles truncation (interactive size configuration).
- [ ] Quick compact shortcut (`c`) works from Session List.
- [ ] Token/size warnings appear at appropriate thresholds.
- [ ] Strong warning blocks proceeding without explicit override.

### Compaction
- [ ] Model Selection shows compatible models with search/filter.
- [ ] Model Selection shows live cost estimate as cursor moves.
- [ ] Context Selection discovers and lists existing recovery files.
- [ ] Context Selection allows adding custom file paths.
- [ ] Context Selection allows recovering another session inline.
- [ ] Compaction confirmation shows cost estimate and token warning if applicable.
- [ ] Compaction calls API, shows progress, and writes output.
- [ ] Save-elsewhere prompt works after compaction.
- [ ] API errors are handled gracefully (return to model selection).

### File Management
- [ ] File Browser shows existing recovery files with metadata.
- [ ] File Browser supports viewing files in scrollable pager.
- [ ] File Browser supports deleting individual or per-session files.

### Infrastructure
- [ ] `opencode` not on PATH shows clear error on startup.
- [ ] JSON-lines log file written to `~/.local/share/orsession/`.
- [ ] Installable via `pip install .` with textual/rich as dependencies.
- [ ] Works on Linux with Python 3.10+.
- [ ] Works on macOS (Terminal.app + iTerm2) with Python 3.10+.
- [ ] Windows native support (stretch goal — verify textual renders correctly).

---

## 19. Resolved Design Decisions

These were open questions that have been resolved:

| # | Question | Decision | Rationale |
|---|----------|----------|-----------|
| 1 | Cross-project scope | Current/specified dir only (v1) | No reliable way to discover all projects without reading opencode internals. Users can specify `-d`. |
| 2 | Quick compact shortcut | Yes, `c` key in Session List | Optimizes the common case. Reuses existing recovery files when available. |
| 3 | Max export size | No hard max. Warn at >100 interactions or >2500 lines. Suggest truncation. | Users should decide. Token warnings at 32K/64K/128K provide graduated guidance. Strong warning makes clear that the model will truncate for you if you don't. |
| 4 | Logging | JSON-lines to `~/.local/share/orsession/orsession.log` | Simple, debuggable, no external deps for logging itself. pubrun integration deferred to v2. |
| 5 | Dependencies | `textual` + `rich` for TUI. `structlog` optional for logging. | This is an application, not a single-file utility. Dependencies are acceptable and enable a significantly better UX. |

---

*End of specification.*
