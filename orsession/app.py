"""
orsession — Interactive TUI for opencode session recovery.

Main application entry point using textual.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

from .core import (
    RecoveryError,
    SessionInfo,
    discover_recovery_files,
    format_timestamp,
    list_sessions,
    require_opencode,
    session_duration,
    session_recovery_status,
)


# ---------------------------------------------------------------------------
# Timestamp mode cycling
# ---------------------------------------------------------------------------

TIMESTAMP_MODES = ("medium", "short", "long")


# ---------------------------------------------------------------------------
# Session List Screen
# ---------------------------------------------------------------------------

class SessionListPanel(Static):
    """Displays the session list header info."""

    def __init__(self, session_dir: Path | None, count: int) -> None:
        dir_display = str(session_dir) if session_dir else "current directory"
        super().__init__(
            f"Sessions in [bold cyan]{dir_display}[/] ({count} found)",
            markup=True,
        )


class OrsessionApp(App):
    """Main orsession TUI application."""

    TITLE = "orsession"
    SUB_TITLE = "opencode session recovery"

    CSS = """
    Screen {
        layout: vertical;
    }

    #session-header {
        height: 3;
        padding: 1 2;
        background: $surface;
    }

    #session-table {
        height: 1fr;
    }

    #status-bar {
        height: 1;
        padding: 0 2;
        background: $surface-darken-1;
        color: $text-muted;
    }

    DataTable {
        height: 1fr;
    }

    .error-panel {
        padding: 2 4;
        background: $error 20%;
        color: $error;
        text-align: center;
        height: 1fr;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("t", "cycle_timestamps", "Timestamps"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("r", "recover", "Recover", show=True),
        Binding("c", "quick_compact", "Quick Compact", show=True),
        Binding("slash", "search", "Search"),
        Binding("f", "browse_files", "Files"),
        Binding("question_mark", "help", "Help"),
    ]

    # Reactive state.
    timestamp_mode: reactive[str] = reactive("medium")
    sort_mode: reactive[str] = reactive("updated_desc")

    def __init__(
        self,
        session_dir: Path | None = None,
        output_dir: Path = Path("opencode-recovery"),
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.session_dir = session_dir
        self.output_dir = output_dir
        self.sessions: list[SessionInfo] = []
        self.recovery_files = []
        self.error_message: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(id="main-content")
        yield Footer()

    def on_mount(self) -> None:
        """Load sessions on startup."""
        self._load_sessions()

    def _load_sessions(self) -> None:
        """Fetch sessions from opencode and populate the table."""
        container = self.query_one("#main-content")
        container.remove_children()

        try:
            require_opencode()
            self.sessions = list_sessions(cwd=self.session_dir)
        except RecoveryError as e:
            self.error_message = str(e)
            container.mount(Static(
                f"[bold red]Error:[/] {self.error_message}",
                markup=True,
                classes="error-panel",
            ))
            return

        # Discover existing recovery files for status indicators.
        self.recovery_files = discover_recovery_files(self.output_dir)

        # Sort sessions.
        self._sort_sessions()

        # Build UI.
        header = SessionListPanel(self.session_dir, len(self.sessions))
        header.id = "session-header"
        container.mount(header)

        table = DataTable(id="session-table")
        table.cursor_type = "row"
        table.zebra_stripes = True
        container.mount(table)

        status = Static("", id="status-bar")
        container.mount(status)

        self._populate_table()

    def _sort_sessions(self) -> None:
        """Sort sessions based on current sort mode."""
        if self.sort_mode == "updated_desc":
            self.sessions.sort(key=lambda s: s.updated, reverse=True)
        elif self.sort_mode == "created_desc":
            self.sessions.sort(key=lambda s: s.created, reverse=True)
        elif self.sort_mode == "title_asc":
            self.sessions.sort(key=lambda s: s.title.lower())

    def _populate_table(self) -> None:
        """Fill the data table with session rows."""
        try:
            table = self.query_one("#session-table", DataTable)
        except Exception:
            return

        table.clear(columns=True)

        # Define columns.
        sort_indicators = {
            "updated_desc": ("", "▼", "", ""),
            "created_desc": ("", "", "▼", ""),
            "title_asc": ("", "", "", "▲"),
        }
        indicators = sort_indicators.get(self.sort_mode, ("", "", "", ""))

        table.add_column("#", width=4)
        table.add_column("St", width=3)
        table.add_column(f"Title{indicators[3]}", width=None)
        table.add_column(f"Updated{indicators[1]}", width=16)
        table.add_column(f"Created{indicators[2]}", width=16)
        table.add_column("Duration", width=10)

        for idx, session in enumerate(self.sessions, start=1):
            status = session_recovery_status(session.session_id, self.recovery_files)
            title = session.title if len(session.title) <= 50 else session.title[:47] + "..."
            updated = format_timestamp(session.updated, self.timestamp_mode)
            created = format_timestamp(session.created, self.timestamp_mode)
            duration = session_duration(session)

            table.add_row(
                str(idx),
                status,
                title,
                updated,
                created,
                duration,
                key=session.session_id,
            )

    def _update_status(self, message: str) -> None:
        """Update the status bar text."""
        try:
            status = self.query_one("#status-bar", Static)
            status.update(message)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cycle_timestamps(self) -> None:
        """Cycle through timestamp display modes."""
        current_idx = TIMESTAMP_MODES.index(self.timestamp_mode)
        self.timestamp_mode = TIMESTAMP_MODES[(current_idx + 1) % len(TIMESTAMP_MODES)]
        self._populate_table()
        self._update_status(f"Timestamp mode: {self.timestamp_mode}")

    def action_cycle_sort(self) -> None:
        """Cycle through sort modes."""
        modes = ["updated_desc", "created_desc", "title_asc"]
        current_idx = modes.index(self.sort_mode)
        self.sort_mode = modes[(current_idx + 1) % len(modes)]
        self._sort_sessions()
        self._populate_table()
        labels = {
            "updated_desc": "Updated (newest first)",
            "created_desc": "Created (newest first)",
            "title_asc": "Title (A-Z)",
        }
        self._update_status(f"Sort: {labels[self.sort_mode]}")

    def action_recover(self) -> None:
        """Start recovery for the selected session."""
        session = self._get_selected_session()
        if session:
            self._update_status(f"Recovery: {session.title} (not yet implemented)")

    def action_quick_compact(self) -> None:
        """Quick compact shortcut for selected session."""
        session = self._get_selected_session()
        if session:
            self._update_status(f"Quick compact: {session.title} (not yet implemented)")

    def action_search(self) -> None:
        """Open search/filter mode."""
        self._update_status("Search (not yet implemented)")

    def action_browse_files(self) -> None:
        """Open the recovery file browser."""
        self._update_status("File browser (not yet implemented)")

    def action_help(self) -> None:
        """Show help overlay."""
        self._update_status("Help (not yet implemented)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_selected_session(self) -> SessionInfo | None:
        """Get the currently highlighted session."""
        try:
            table = self.query_one("#session-table", DataTable)
            row_key = table.cursor_row
            if row_key is not None and 0 <= row_key < len(self.sessions):
                return self.sessions[row_key]
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on a session row — open detail view."""
        session = self._get_selected_session()
        if session:
            self._update_status(f"Detail view: {session.title} (not yet implemented)")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="orsession",
        description="Interactive TUI for browsing, recovering, and compacting opencode sessions.",
    )

    parser.add_argument(
        "-d", "--session-dir",
        type=Path,
        default=None,
        help="Directory where opencode sessions live (default: current directory).",
    )

    parser.add_argument(
        "-o", "--out",
        type=Path,
        default=Path("opencode-recovery"),
        help="Output directory for recovery files (default: ./opencode-recovery).",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity.",
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable color output.",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="orsession 0.1.0",
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point for orsession."""
    args = parse_args()

    session_dir = args.session_dir
    if session_dir is not None:
        session_dir = session_dir.resolve()
        if not session_dir.is_dir():
            print(f"Error: --session-dir is not a valid directory: {session_dir}", file=sys.stderr)
            sys.exit(1)

    app = OrsessionApp(
        session_dir=session_dir,
        output_dir=args.out,
    )
    app.run()


if __name__ == "__main__":
    main()
