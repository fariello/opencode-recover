"""
orsession — Interactive TUI for opencode session recovery.

Main application entry point using textual.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Rule, Static

from .core import (
    RecoveryError,
    SessionExport,
    SessionInfo,
    Turn,
    discover_recovery_files,
    export_session,
    extract_turns_from_export,
    filter_conversation_turns,
    format_timestamp,
    count_interactions,
    list_sessions,
    require_opencode,
    session_duration,
    session_recovery_status,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMESTAMP_MODES = ("medium", "short", "long")


# ---------------------------------------------------------------------------
# Session Detail Screen
# ---------------------------------------------------------------------------

class SessionDetailScreen(Screen):
    """Detailed view of a single session with metadata and preview."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("b", "go_back", "Back"),
        Binding("r", "recover", "Recover"),
        Binding("p", "full_preview", "Full Preview"),
        Binding("t", "cycle_timestamps", "Timestamps"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, session: SessionInfo, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self.export: SessionExport | None = None
        self.turns: list[Turn] = []
        self.loading = True

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(
            Static("Loading session...", id="detail-content"),
            id="detail-scroll",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Export and parse the session on mount."""
        self._load_export()

    def _load_export(self) -> None:
        """Export the session and extract turns."""
        app: OrsessionApp = self.app  # type: ignore
        content_widget = self.query_one("#detail-content", Static)

        try:
            # Use the app's shared temp dir.
            self.export = export_session(
                session_id=self.session.session_id,
                temp_dir=app.temp_dir,
                cwd=app.session_dir,
            )
            self.turns = filter_conversation_turns(
                extract_turns_from_export(self.export, include_tools=False)
            )
        except RecoveryError as e:
            content_widget.update(
                f"[bold red]Export failed:[/]\n\n{e}",
            )
            self.loading = False
            return

        self.loading = False
        self._render_detail()

    def _render_detail(self) -> None:
        """Render the full detail view with metadata and previews."""
        app: OrsessionApp = self.app  # type: ignore
        ts_mode = app.timestamp_mode
        session = self.session
        export = self.export

        lines: list[str] = []

        # Title.
        lines.append(f"[bold]{session.title}[/]")
        lines.append("─" * min(60, len(session.title) + 4))
        lines.append("")

        # Metadata section.
        lines.append(f"  [dim]ID:[/]        {session.session_id}")

        if export and export.info:
            info = export.info
            slug = info.get("slug", "")
            if slug:
                lines.append(f"  [dim]Slug:[/]      {slug}")

        lines.append(f"  [dim]Created:[/]   {format_timestamp(session.created, ts_mode)}")
        lines.append(f"  [dim]Updated:[/]   {format_timestamp(session.updated, ts_mode)}")
        lines.append(f"  [dim]Duration:[/]  {session_duration(session)}")

        if export and export.info:
            info = export.info

            agent = info.get("agent", "")
            if agent:
                lines.append(f"  [dim]Agent:[/]     {agent}")

            model_info = info.get("model", {})
            if isinstance(model_info, dict):
                model_id = model_info.get("id", "")
                provider = model_info.get("providerID", "")
                if model_id:
                    model_display = f"{model_id} ({provider})" if provider else model_id
                    lines.append(f"  [dim]Model:[/]     {model_display}")

            # Turns and interactions.
            turn_count = len(self.turns)
            user_count = sum(1 for t in self.turns if t.role == "user")
            asst_count = sum(1 for t in self.turns if t.role == "assistant")
            interactions = count_interactions(self.turns)
            lines.append(f"  [dim]Turns:[/]     {turn_count} ({user_count} user, {asst_count} assistant)")
            lines.append(f"  [dim]Interact:[/]  {interactions} back-and-forth exchanges")

            # Cost.
            cost = info.get("cost")
            if cost is not None:
                lines.append(f"  [dim]Cost:[/]      ${cost:.2f}")

            # Tokens.
            tokens = info.get("tokens", {})
            if isinstance(tokens, dict):
                tok_in = tokens.get("input", 0)
                tok_out = tokens.get("output", 0)
                cache = tokens.get("cache", {})
                cache_read = cache.get("read", 0) if isinstance(cache, dict) else 0

                parts = []
                if tok_in:
                    parts.append(f"{_format_number(tok_in)} in")
                if tok_out:
                    parts.append(f"{_format_number(tok_out)} out")
                if cache_read:
                    parts.append(f"{_format_number(cache_read)} cache")
                if parts:
                    lines.append(f"  [dim]Tokens:[/]    {' / '.join(parts)}")

            # File changes.
            summary = info.get("summary", {})
            if isinstance(summary, dict):
                additions = summary.get("additions", 0)
                deletions = summary.get("deletions", 0)
                files = summary.get("files", 0)
                if additions or deletions:
                    lines.append(f"  [dim]Changes:[/]   +{additions} -{deletions} across {files} file{'s' if files != 1 else ''}")

            # Directory.
            directory = info.get("directory", "")
            if directory:
                lines.append(f"  [dim]Directory:[/] {directory}")

        lines.append("")

        # Preview: first exchanges.
        if self.turns:
            first_turns = self.turns[:3]
            lines.append(f"  [bold]── First exchanges ──[/]")
            lines.append("")
            for turn in first_turns:
                ts = self._get_turn_timestamp(turn)
                ts_display = f" [{format_timestamp(ts, ts_mode)}]" if ts else ""
                role_char = "U" if turn.role == "user" else "A"
                role_style = "cyan" if turn.role == "user" else "dim"
                preview = _collapse_preview(turn.text, 80)
                lines.append(f"  [{role_style}]{role_char}{ts_display}:[/] {preview}")
            lines.append("")

            # Preview: last exchanges.
            last_turns = self.turns[-3:] if len(self.turns) > 3 else []
            if last_turns:
                # Make sure we don't duplicate if session is very short.
                if len(self.turns) > 6:
                    lines.append(f"  [dim]  ... ({len(self.turns) - 6} turns omitted) ...[/]")
                    lines.append("")

                lines.append(f"  [bold]── Last exchanges ──[/]")
                lines.append("")
                for turn in last_turns:
                    ts = self._get_turn_timestamp(turn)
                    ts_display = f" [{format_timestamp(ts, ts_mode)}]" if ts else ""
                    role_char = "U" if turn.role == "user" else "A"
                    role_style = "cyan" if turn.role == "user" else "dim"
                    preview = _collapse_preview(turn.text, 80)
                    lines.append(f"  [{role_style}]{role_char}{ts_display}:[/] {preview}")
                lines.append("")
        else:
            lines.append("  [dim]No user/assistant turns found.[/]")
            lines.append("")

        content_widget = self.query_one("#detail-content", Static)
        content_widget.update("\n".join(lines))

    def _get_turn_timestamp(self, turn: Turn) -> str:
        """Try to find the timestamp for a turn from the export messages."""
        if not self.export or not self.export.messages:
            return ""

        # The turn.source is like "$.messages[4]" — extract the index.
        import re
        match = re.search(r"\$\.messages\[(\d+)\]", turn.source)
        if not match:
            return ""

        msg_idx = int(match.group(1))
        if msg_idx >= len(self.export.messages):
            return ""

        msg = self.export.messages[msg_idx]
        info = msg.get("info", {})
        time_info = info.get("time", {})
        created = time_info.get("created")
        if created is not None:
            return str(created)
        return ""

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_go_back(self) -> None:
        """Return to session list."""
        self.app.pop_screen()

    def action_recover(self) -> None:
        """Start recovery for this session."""
        self.app.notify(f"Recovery: {self.session.title} (not yet implemented)")

    def action_full_preview(self) -> None:
        """Open full preview screen."""
        if not self.turns:
            self.app.notify("No turns to preview.", severity="warning")
            return
        self.app.push_screen(FullPreviewScreen(self.session, self.turns, self.export))

    def action_cycle_timestamps(self) -> None:
        """Cycle timestamp mode and re-render."""
        app: OrsessionApp = self.app  # type: ignore
        current_idx = TIMESTAMP_MODES.index(app.timestamp_mode)
        app.timestamp_mode = TIMESTAMP_MODES[(current_idx + 1) % len(TIMESTAMP_MODES)]
        if not self.loading:
            self._render_detail()
        self.app.notify(f"Timestamps: {app.timestamp_mode}", timeout=2)


# ---------------------------------------------------------------------------
# Full Preview Screen
# ---------------------------------------------------------------------------

class FullPreviewScreen(Screen):
    """Scrollable full transcript preview."""

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("b", "go_back", "Back"),
        Binding("t", "cycle_timestamps", "Timestamps"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        session: SessionInfo,
        turns: list[Turn],
        export: SessionExport | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.session = session
        self.turns = turns
        self.export = export

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(
            Static(id="preview-content"),
            id="preview-scroll",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._render_preview()

    def _render_preview(self) -> None:
        """Render the full transcript."""
        app: OrsessionApp = self.app  # type: ignore
        ts_mode = app.timestamp_mode
        lines: list[str] = []

        lines.append(f"[bold]Full Preview: {self.session.title}[/]")
        lines.append(f"[dim]{len(self.turns)} turns, {count_interactions(self.turns)} interactions[/]")
        lines.append("")
        lines.append("─" * 60)
        lines.append("")

        for turn in self.turns:
            role_label = "User" if turn.role == "user" else "Assistant"
            role_style = "bold cyan" if turn.role == "user" else "bold"

            # Try to get timestamp.
            ts = self._get_turn_timestamp(turn)
            ts_display = f"  [dim][{format_timestamp(ts, ts_mode)}][/]" if ts else ""

            lines.append(f"[{role_style}]### {turn.index}. {role_label}[/]{ts_display}")
            lines.append("")
            lines.append(turn.text)
            lines.append("")

        content = self.query_one("#preview-content", Static)
        content.update("\n".join(lines))

    def _get_turn_timestamp(self, turn: Turn) -> str:
        """Try to find the timestamp for a turn from the export messages."""
        if not self.export or not self.export.messages:
            return ""
        import re
        match = re.search(r"\$\.messages\[(\d+)\]", turn.source)
        if not match:
            return ""
        msg_idx = int(match.group(1))
        if msg_idx >= len(self.export.messages):
            return ""
        msg = self.export.messages[msg_idx]
        info = msg.get("info", {})
        time_info = info.get("time", {})
        created = time_info.get("created")
        if created is not None:
            return str(created)
        return ""

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_cycle_timestamps(self) -> None:
        app: OrsessionApp = self.app  # type: ignore
        current_idx = TIMESTAMP_MODES.index(app.timestamp_mode)
        app.timestamp_mode = TIMESTAMP_MODES[(current_idx + 1) % len(TIMESTAMP_MODES)]
        self._render_preview()
        self.app.notify(f"Timestamps: {app.timestamp_mode}", timeout=2)


# ---------------------------------------------------------------------------
# Session List Screen (default)
# ---------------------------------------------------------------------------

class SessionListScreen(Screen):
    """Home screen showing all sessions in a sortable table."""

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("t", "cycle_timestamps", "Timestamps"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("r", "recover", "Recover"),
        Binding("c", "quick_compact", "Quick Compact"),
        Binding("slash", "search", "Search"),
        Binding("f", "browse_files", "Files"),
        Binding("question_mark", "help", "Help"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(id="main-content")
        yield Footer()

    def on_mount(self) -> None:
        """Load sessions on startup."""
        self._load_sessions()

    def _load_sessions(self) -> None:
        """Fetch sessions from opencode and populate the table."""
        app: OrsessionApp = self.app  # type: ignore
        container = self.query_one("#main-content")
        container.remove_children()

        try:
            require_opencode()
            app.sessions = list_sessions(cwd=app.session_dir)
        except RecoveryError as e:
            app.error_message = str(e)
            container.mount(Static(
                f"[bold red]Error:[/] {app.error_message}",
                markup=True,
                classes="error-panel",
            ))
            return

        # Discover existing recovery files for status indicators.
        app.recovery_files = discover_recovery_files(app.output_dir)

        # Sort sessions.
        self._sort_sessions()

        # Build UI.
        dir_display = str(app.session_dir) if app.session_dir else "current directory"
        header = Static(
            f"Sessions in [bold cyan]{dir_display}[/] ({len(app.sessions)} found)",
            markup=True,
            id="session-header",
        )
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
        app: OrsessionApp = self.app  # type: ignore
        if app.sort_mode == "updated_desc":
            app.sessions.sort(key=lambda s: s.updated, reverse=True)
        elif app.sort_mode == "created_desc":
            app.sessions.sort(key=lambda s: s.created, reverse=True)
        elif app.sort_mode == "title_asc":
            app.sessions.sort(key=lambda s: s.title.lower())

    def _populate_table(self) -> None:
        """Fill the data table with session rows."""
        app: OrsessionApp = self.app  # type: ignore
        try:
            table = self.query_one("#session-table", DataTable)
        except Exception:
            return

        table.clear(columns=True)

        sort_indicators = {
            "updated_desc": ("", " ▼", "", ""),
            "created_desc": ("", "", " ▼", ""),
            "title_asc": (" ▲", "", "", ""),
        }
        indicators = sort_indicators.get(app.sort_mode, ("", "", "", ""))

        table.add_column("#", width=4)
        table.add_column("St", width=3)
        table.add_column(f"Title{indicators[0]}", width=None)
        table.add_column(f"Updated{indicators[1]}", width=16)
        table.add_column(f"Created{indicators[2]}", width=16)
        table.add_column("Duration", width=10)

        for idx, session in enumerate(app.sessions, start=1):
            status = session_recovery_status(session.session_id, app.recovery_files)
            title = session.title if len(session.title) <= 50 else session.title[:47] + "..."
            updated = format_timestamp(session.updated, app.timestamp_mode)
            created = format_timestamp(session.created, app.timestamp_mode)
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
        app: OrsessionApp = self.app  # type: ignore
        current_idx = TIMESTAMP_MODES.index(app.timestamp_mode)
        app.timestamp_mode = TIMESTAMP_MODES[(current_idx + 1) % len(TIMESTAMP_MODES)]
        self._populate_table()
        self._update_status(f"Timestamp mode: {app.timestamp_mode}")

    def action_cycle_sort(self) -> None:
        app: OrsessionApp = self.app  # type: ignore
        modes = ["updated_desc", "created_desc", "title_asc"]
        current_idx = modes.index(app.sort_mode)
        app.sort_mode = modes[(current_idx + 1) % len(modes)]
        self._sort_sessions()
        self._populate_table()
        labels = {
            "updated_desc": "Updated (newest first)",
            "created_desc": "Created (newest first)",
            "title_asc": "Title (A-Z)",
        }
        self._update_status(f"Sort: {labels[app.sort_mode]}")

    def action_recover(self) -> None:
        session = self._get_selected_session()
        if session:
            self._update_status(f"Recovery: {session.title} (not yet implemented)")

    def action_quick_compact(self) -> None:
        session = self._get_selected_session()
        if session:
            self._update_status(f"Quick compact: {session.title} (not yet implemented)")

    def action_search(self) -> None:
        self._update_status("Search (not yet implemented)")

    def action_browse_files(self) -> None:
        self._update_status("File browser (not yet implemented)")

    def action_help(self) -> None:
        self._update_status("Help (not yet implemented)")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_selected_session(self) -> SessionInfo | None:
        """Get the currently highlighted session."""
        app: OrsessionApp = self.app  # type: ignore
        try:
            table = self.query_one("#session-table", DataTable)
            row_key = table.cursor_row
            if row_key is not None and 0 <= row_key < len(app.sessions):
                return app.sessions[row_key]
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on a session row — open detail view."""
        session = self._get_selected_session()
        if session:
            self.app.push_screen(SessionDetailScreen(session))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collapse_preview(text: str, max_chars: int = 80) -> str:
    """Collapse multi-line text into a single-line preview."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[:max_chars - 3] + "..."


def _format_number(n: int) -> str:
    """Format a large number with K/M suffixes."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

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

    #detail-scroll {
        height: 1fr;
        padding: 1 2;
    }

    #detail-content {
        width: 100%;
    }

    #preview-scroll {
        height: 1fr;
        padding: 1 2;
    }

    #preview-content {
        width: 100%;
    }
    """

    # Shared app state.
    timestamp_mode: reactive[str] = reactive("medium")
    sort_mode: reactive[str] = reactive("updated_desc")

    SCREENS = {
        "session_list": SessionListScreen,
    }

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
        self.recovery_files: list = []
        self.error_message: str | None = None
        # Shared temp directory for exports.
        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="orsession-")
        self.temp_dir = Path(self._temp_dir_obj.name)

    def on_mount(self) -> None:
        """Push the initial screen."""
        self.push_screen(SessionListScreen())

    def on_unmount(self) -> None:
        """Clean up temp directory."""
        try:
            self._temp_dir_obj.cleanup()
        except Exception:
            pass


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
