# Changelog

## [0.1.1] - 2026-06-02

### Security

- **F-02**: `ModelInfo.__repr__` now masks `api_key` (shows only first 4 chars)
  to prevent accidental secret exposure in logs, tracebacks, or debug output.
- **F-03**: HTTPS enforcement for API endpoints now uses `urllib.parse.urlparse`
  hostname validation instead of substring matching. Previously, a URL like
  `http://evil.localhost.attacker.com` could bypass the check. Now only
  `localhost`, `127.0.0.1`, and `::1` are accepted as local exceptions.

### Fixed

- **F-01**: Removed dead `base_url` assignment in `extract_models_from_config`
  (CLI script line 386). The value was immediately overwritten on the next line.
- **F-06**: Fixed license mismatch — `pyproject.toml` now correctly declares
  `BSD-3-Clause` to match the actual LICENSE file (was incorrectly set to MIT).
- **F-07**: Synced `COMPACTION_USER_PROMPT_TEMPLATE` in `orsession/core.py`
  with the full version from the CLI tool. The core version was missing detailed
  section instructions (sections 1-9, Agent Operating Guidance, Style), which
  meant TUI-generated compaction prompts were lower quality than CLI-generated ones.
- **F-10**: `orsession --version` now reads from `orsession.__version__` instead
  of a hardcoded string. Version will no longer drift between files.
- **F-13**: Moved `from datetime import datetime, timezone` from inside a method
  body in `CompactionScreen._run_compaction` to module-level imports.
- **F-04**: File Browser delete operations (`d` and `D`) now require confirmation
  via a second keypress. First press shows a warning notification; pressing the
  same key again confirms deletion.
- Fixed `ContextSelectionScreen._render` method name collision with textual's
  `Widget._render`. Renamed to `_render_context_ui`.

### Documentation

- **F-18**: README now documents the `orsession` TUI app (installation via
  `pip install .`, features, and usage). Clarified that the CLI tool remains
  stdlib-only while the TUI has `textual`/`rich` dependencies.
- **F-15**: SPEC updated from "curses-based TUI" to "textual-based TUI"
  throughout.
- **F-16**: SPEC file layout section updated to reflect actual package structure
  (`orsession/__init__.py`, `app.py`, `core.py` + `pyproject.toml`).
- **F-17**: SPEC acceptance criteria updated with checkmarks for implemented
  features and "deferred to v0.2" annotations for features not yet built
  (help overlay, session list search, fork indicator, in-content search,
  custom path input, recover-another-session sub-flow, save-elsewhere,
  JSON-lines logging).

## [0.1.0] - 2026-06-01

### Added

- Initial implementation of `orsession` TUI application with 8 screens:
  Session List, Session Detail, Full Preview, Recovery Wizard,
  Model Selection, Context Selection, Compaction, File Browser.
- `orsession/core.py` shared module extracted from CLI tool.
- `pyproject.toml` with textual/rich dependencies and `orsession` entry point.
- `opencode_recover_session.py` optionally imports from `orsession.core`
  when installed (fallback to bundled implementations).
- `SPEC-orsession.md` functional specification.
- Support for `{file:PATH}` syntax in opencode config for API keys.
