"""
Shared core logic for opencode session recovery.

This module contains the reusable components shared between the CLI tool
(opencode_recover_session.py) and the TUI app (orsession).

Includes:
- Data models (SessionInfo, Turn, ModelInfo)
- Config loading and model extraction
- opencode CLI interaction (list sessions, export)
- Turn extraction and transcript rendering
- LLM compaction API calls
- File I/O utilities
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LONG_SESSION_LINE_THRESHOLD: int = 2500
LONG_SESSION_INTERACTION_THRESHOLD: int = 100

CHARS_PER_TOKEN_ESTIMATE: float = 4.0

OPENAI_COMPATIBLE_PACKAGES: set[str] = {
    "@ai-sdk/openai",
    "@ai-sdk/openai-compatible",
}

OPENCODE_CONFIG_PATHS: tuple[Path, ...] = (
    Path.home() / ".config" / "opencode" / "opencode.json",
    Path.home() / ".config" / "opencode" / "opencode.jsonc",
    Path("opencode.json"),
    Path("opencode.jsonc"),
)

ROLE_ALIASES: dict[str, str] = {
    "human": "user",
    "user": "user",
    "assistant": "assistant",
    "ai": "assistant",
    "model": "assistant",
    "system": "system",
    "tool": "tool",
    "function": "tool",
}

TEXT_KEYS: tuple[str, ...] = (
    "content", "text", "message", "input", "output", "result", "summary",
)

SESSION_ID_KEYS: tuple[str, ...] = (
    "id", "sessionID", "sessionId", "session_id",
)

SESSION_TITLE_KEYS: tuple[str, ...] = (
    "title", "summary", "description", "name",
)

SESSION_CREATED_KEYS: tuple[str, ...] = (
    "created", "createdAt", "created_at", "timeCreated",
)

SESSION_UPDATED_KEYS: tuple[str, ...] = (
    "updated", "updatedAt", "updated_at", "timeUpdated", "modified", "modifiedAt",
)

NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*Tool call not allowed while generating summary", re.IGNORECASE),
    re.compile(r"^\s*Where were we\?\s*$", re.IGNORECASE),
    re.compile(r"^\s*\[System: Empty message content sanitised to satisfy protocol\]\s*$"),
)

NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\[System: Empty message content sanitised to satisfy protocol\]\s*$"),
)

# Token warning thresholds.
TOKEN_THRESHOLD_INFO: int = 32_000
TOKEN_THRESHOLD_WARNING: int = 64_000
TOKEN_THRESHOLD_STRONG: int = 128_000


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    """Represents a discovered opencode session."""

    session_id: str
    title: str
    created: str
    updated: str
    raw: dict[str, Any]


@dataclass
class Turn:
    """Represents one extracted conversational turn."""

    role: str
    text: str
    index: int
    source: str


@dataclass
class ModelInfo:
    """Represents a model available for compaction."""

    provider_id: str
    model_id: str
    name: str
    base_url: str
    api_key: str
    cost_input: float | None
    cost_output: float | None
    compatible: bool

    def __repr__(self) -> str:
        """Mask api_key in repr to prevent accidental secret exposure in logs."""
        key_display = f"{self.api_key[:4]}***" if self.api_key else "(empty)"
        return (
            f"ModelInfo(provider_id={self.provider_id!r}, model_id={self.model_id!r}, "
            f"name={self.name!r}, base_url={self.base_url!r}, api_key={key_display!r}, "
            f"cost_input={self.cost_input!r}, cost_output={self.cost_output!r}, "
            f"compatible={self.compatible!r})"
        )


@dataclass
class RecoveryFile:
    """Represents an existing recovery file on disk."""

    path: Path
    session_id: str
    file_type: str  # "transcript", "restart", "compact-prompt", "compacted"
    timestamp: str  # from filename
    line_count: int = 0
    size_bytes: int = 0


@dataclass
class SessionExport:
    """Holds a parsed session export with metadata."""

    info: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    raw_text: str = ""
    is_valid_json: bool = True
    export_path: Path | None = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RecoveryError(Exception):
    """Raised when the recovery workflow cannot continue safely."""
    pass


# ---------------------------------------------------------------------------
# Config and Environment Expansion
# ---------------------------------------------------------------------------

_ENV_VAR_PATTERN: re.Pattern[str] = re.compile(r"\{env:([^}]+)\}")
_FILE_REF_PATTERN: re.Pattern[str] = re.compile(r"\{file:([^}]+)\}")


def _read_file_ref(path_str: str) -> str:
    """Read a file reference, expanding ~ to the user's home directory."""
    expanded = os.path.expanduser(path_str.strip())
    try:
        with open(expanded, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, IOError):
        return ""


def expand_config_refs(value: str) -> str:
    """
    Expand environment variable and file references in a config string value.

    Supports:
      - {file:PATH}     — read secret from file
      - {env:VAR_NAME}  — environment variable
      - ${VAR_NAME}     — shell-style with braces
      - $VAR_NAME       — shell-style without braces (entire value only)
    """
    if not isinstance(value, str) or not value:
        return value

    if "{file:" in value:
        def replace_file(match: re.Match[str]) -> str:
            content = _read_file_ref(match.group(1))
            return content if content else match.group(0)
        return _FILE_REF_PATTERN.sub(replace_file, value)

    if "{env:" in value:
        def replace_env(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), match.group(0))
        return _ENV_VAR_PATTERN.sub(replace_env, value)

    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")

    if value.startswith("$") and value[1:].isidentifier():
        return os.environ.get(value[1:], "")

    return value


def strip_jsonc_comments(text: str) -> str:
    """Strip single-line (//) and block (/* */) comments from JSONC text."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def parse_json_text(text: str, context: str, strict_failure: bool = True) -> Any:
    """Parse JSON text with a helpful error message."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as second_error:
            if strict_failure:
                raise RecoveryError(
                    f"Could not parse JSON from {context}.\n"
                    f"Standard parse error: {first_error}\n"
                    f"Lenient parse error: {second_error}"
                ) from second_error
            return None


def load_opencode_config() -> dict[str, Any]:
    """Load the opencode configuration file from standard paths."""
    for config_path in OPENCODE_CONFIG_PATHS:
        if config_path.exists():
            try:
                raw = config_path.read_text(encoding="utf-8")
            except OSError as error:
                raise RecoveryError(f"Could not read config: {config_path}\n{error}") from error

            if config_path.suffix == ".jsonc":
                raw = strip_jsonc_comments(raw)

            parsed = parse_json_text(raw, f"config file {config_path}", strict_failure=True)
            return parsed

    searched = ", ".join(str(p) for p in OPENCODE_CONFIG_PATHS)
    raise RecoveryError(f"No opencode config file found. Searched:\n  {searched}")


def extract_models_from_config(config: dict[str, Any]) -> list[ModelInfo]:
    """Extract all available models from the opencode config."""
    providers = config.get("provider", {})
    models: list[ModelInfo] = []

    for provider_id, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue

        npm_package = provider_data.get("npm", "")
        compatible = npm_package in OPENAI_COMPATIBLE_PACKAGES

        options = provider_data.get("options", {})
        api_key = expand_config_refs(options.get("apiKey", ""))
        base_url = expand_config_refs(options.get("baseURL", ""))

        if not base_url and npm_package == "@ai-sdk/openai":
            base_url = "https://api.openai.com/v1"

        provider_models = provider_data.get("models", {})

        for model_id, model_data in provider_models.items():
            if not isinstance(model_data, dict):
                continue

            name = model_data.get("name", model_id)
            cost = model_data.get("cost", {})
            cost_input = cost.get("input") if isinstance(cost, dict) else None
            cost_output = cost.get("output") if isinstance(cost, dict) else None

            models.append(ModelInfo(
                provider_id=provider_id,
                model_id=model_id,
                name=name,
                base_url=base_url,
                api_key=api_key,
                cost_input=cost_input,
                cost_output=cost_output,
                compatible=compatible,
            ))

    return models


def get_compatible_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Return only models with OpenAI-compatible APIs and valid credentials."""
    return [m for m in models if m.compatible and m.api_key and m.base_url]


def resolve_model(models: list[ModelInfo], model_spec: str) -> ModelInfo:
    """Resolve a model specification to a ModelInfo (exact or substring match)."""
    # Exact match.
    for m in models:
        full_id = f"{m.provider_id}/{m.model_id}"
        if full_id == model_spec:
            if not m.compatible:
                raise RecoveryError(f"Model {model_spec} uses a non-OpenAI-compatible API.")
            if not m.api_key:
                raise RecoveryError(f"Model {model_spec} has no API key configured.")
            if not m.base_url:
                raise RecoveryError(f"Model {model_spec} has no base URL configured.")
            return m

    # Substring match.
    matches = [
        m for m in models
        if model_spec.lower() in f"{m.provider_id}/{m.model_id}".lower()
        or model_spec.lower() in m.name.lower()
    ]

    if not matches:
        raise RecoveryError(f"Model not found: {model_spec!r}\nUse --show-models to see available models.")

    if len(matches) > 1:
        match_names = [f"  {m.provider_id}/{m.model_id} ({m.name})" for m in matches[:10]]
        raise RecoveryError(
            f"Ambiguous model spec {model_spec!r}. Matches:\n" + "\n".join(match_names)
        )

    matched = matches[0]
    if not matched.compatible:
        raise RecoveryError(f"Model {matched.provider_id}/{matched.model_id} uses a non-OpenAI-compatible API.")
    if not matched.api_key:
        raise RecoveryError(f"Model {matched.provider_id}/{matched.model_id} has no API key configured.")
    if not matched.base_url:
        raise RecoveryError(f"Model {matched.provider_id}/{matched.model_id} has no base URL configured.")

    return matched


# ---------------------------------------------------------------------------
# Token and Cost Estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 chars per token heuristic)."""
    return max(1, int(len(text) / CHARS_PER_TOKEN_ESTIMATE))


def estimate_cost(input_tokens: int, output_tokens: int, model: ModelInfo) -> float | None:
    """Estimate cost in dollars, or None if cost info unavailable."""
    if model.cost_input is None or model.cost_output is None:
        return None
    return (input_tokens / 1_000_000) * model.cost_input + (output_tokens / 1_000_000) * model.cost_output


def token_warning_level(estimated_tokens: int) -> str | None:
    """
    Return the warning level for an estimated token count.

    Returns: "info", "warning", "strong", or None.
    """
    if estimated_tokens >= TOKEN_THRESHOLD_STRONG:
        return "strong"
    elif estimated_tokens >= TOKEN_THRESHOLD_WARNING:
        return "warning"
    elif estimated_tokens >= TOKEN_THRESHOLD_INFO:
        return "info"
    return None


# ---------------------------------------------------------------------------
# opencode CLI Interaction
# ---------------------------------------------------------------------------

def require_opencode() -> None:
    """Ensure the opencode CLI is available on PATH."""
    if shutil.which("opencode") is None:
        raise RecoveryError(
            "The `opencode` CLI was not found on PATH. Install opencode or add it to PATH first."
        )


def run_command(
    command: Sequence[str],
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command safely."""
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError as error:
        raise RecoveryError(f"Command not found: {command[0]}") from error
    except OSError as error:
        raise RecoveryError(f"Failed to run command: {' '.join(command)}\n{error}") from error

    if check and completed.returncode != 0:
        raise RecoveryError(
            f"Command failed with exit code {completed.returncode}: {' '.join(command)}\n"
            f"{completed.stderr.strip() or completed.stdout.strip() or 'No output'}"
        )

    return completed


def list_sessions(cwd: Path | None = None) -> list[SessionInfo]:
    """Retrieve opencode sessions from the CLI."""
    completed = run_command(
        ("opencode", "session", "list", "--format", "json"),
        check=True,
        cwd=cwd,
    )

    data = parse_json_text(completed.stdout, "opencode session list")
    raw_sessions = _extract_session_objects(data)
    sessions = _normalize_sessions(raw_sessions)

    if not sessions:
        raise RecoveryError(
            "No sessions were found in the opencode session list output."
        )

    return sessions


def export_session(session_id: str, temp_dir: Path, cwd: Path | None = None) -> SessionExport:
    """Export a session and parse its contents."""
    export_path = temp_dir / f"opencode-session-{session_id}.json"

    command = ["opencode", "export", session_id]
    try:
        fd = os.open(export_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as outfile:
            completed = subprocess.run(
                command,
                stdout=outfile,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                cwd=cwd,
            )
    except FileNotFoundError as error:
        raise RecoveryError("Command not found: opencode") from error
    except OSError as error:
        raise RecoveryError(f"Failed to run command: {' '.join(command)}\n{error}") from error

    if completed.returncode != 0:
        raise RecoveryError(
            f"Command failed with exit code {completed.returncode}: {' '.join(command)}\n"
            f"{completed.stderr.strip() or 'No output'}"
        )

    if not export_path.exists() or export_path.stat().st_size == 0:
        raise RecoveryError("opencode export produced no output.")

    # Parse the export.
    raw_text = export_path.read_text(encoding="utf-8")
    parsed = parse_json_text(raw_text, f"export file {export_path}", strict_failure=False)

    if parsed is None:
        return SessionExport(
            raw_text=raw_text,
            is_valid_json=False,
            export_path=export_path,
        )

    info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
    messages = parsed.get("messages", []) if isinstance(parsed, dict) else []

    return SessionExport(
        info=info,
        messages=messages,
        raw_text=raw_text,
        is_valid_json=True,
        export_path=export_path,
    )


# ---------------------------------------------------------------------------
# Session Helpers
# ---------------------------------------------------------------------------

def _first_present_string(data: dict[str, Any], keys: Iterable[str]) -> str:
    """Return the first present string-like field from a dictionary."""
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, (int, float, bool)):
            return str(value)
    return ""


def _extract_session_objects(value: Any) -> list[dict[str, Any]]:
    """Extract candidate session dictionaries from arbitrary JSON."""
    candidates: list[dict[str, Any]] = []

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                candidates.append(item)
        return candidates

    if isinstance(value, dict):
        for key in ("sessions", "data", "items", "results"):
            nested = value.get(key)
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        candidates.append(item)

        if not candidates and any(key in value for key in SESSION_ID_KEYS):
            candidates.append(value)

    return candidates


def _normalize_sessions(raw_sessions: list[dict[str, Any]]) -> list[SessionInfo]:
    """Normalize raw session objects into SessionInfo records."""
    sessions: list[SessionInfo] = []

    for raw in raw_sessions:
        session_id = _first_present_string(raw, SESSION_ID_KEYS)
        if not session_id:
            continue

        title = _first_present_string(raw, SESSION_TITLE_KEYS)
        created = _first_present_string(raw, SESSION_CREATED_KEYS)
        updated = _first_present_string(raw, SESSION_UPDATED_KEYS)

        sessions.append(SessionInfo(
            session_id=session_id,
            title=title or "(untitled)",
            created=created or "unknown",
            updated=updated or "unknown",
            raw=raw,
        ))

    return sessions


# ---------------------------------------------------------------------------
# Timestamp Formatting
# ---------------------------------------------------------------------------

def format_timestamp_short(value: str) -> str:
    """Format as HH:MM."""
    dt = _parse_timestamp(value)
    return dt.strftime("%H:%M") if dt else value


def format_timestamp_medium(value: str) -> str:
    """Format as Mon-DD HH:MM."""
    dt = _parse_timestamp(value)
    return dt.strftime("%b-%d %H:%M") if dt else value


def format_timestamp_long(value: str) -> str:
    """Format as YYYY-MM-DD HH:MM:SS."""
    dt = _parse_timestamp(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else value


def format_timestamp(value: str, mode: str = "medium") -> str:
    """Format a timestamp in the given mode (short/medium/long)."""
    if mode == "short":
        return format_timestamp_short(value)
    elif mode == "long":
        return format_timestamp_long(value)
    return format_timestamp_medium(value)


def _parse_timestamp(value: str) -> datetime | None:
    """Parse a timestamp string (epoch ms, epoch s, or ISO 8601) to datetime."""
    if not value or value == "unknown":
        return None

    # Unix epoch (milliseconds or seconds).
    if value.isascii() and value.isdigit():
        epoch = int(value)
        if epoch > 4_102_444_800:
            epoch_seconds = epoch / 1000.0
        else:
            epoch_seconds = float(epoch)
        try:
            return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None

    # ISO 8601.
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    return None


def session_duration(session: SessionInfo) -> str:
    """Compute human-readable duration between created and updated."""
    created_dt = _parse_timestamp(session.created)
    updated_dt = _parse_timestamp(session.updated)
    if not created_dt or not updated_dt:
        return "unknown"
    delta = updated_dt - created_dt
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes = total_seconds // 60
    hours = minutes // 60
    if hours == 0:
        return f"{minutes}m"
    return f"{hours}h {minutes % 60}m"


# ---------------------------------------------------------------------------
# Turn Extraction
# ---------------------------------------------------------------------------

def normalize_role(value: Any) -> str | None:
    """Normalize a role value to a known role."""
    if not isinstance(value, str):
        return None
    return ROLE_ALIASES.get(value.strip().lower())


def clean_text(text: str) -> str:
    """Normalize whitespace in extracted text without destroying code blocks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    cleaned_lines: list[str] = []
    for line in lines:
        if any(pattern.match(line) for pattern in NOISE_LINE_PATTERNS):
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def is_noise(text: str) -> bool:
    """Decide whether a turn is likely recovery noise."""
    for pattern in NOISE_PATTERNS:
        if pattern.search(text):
            return True
    return False


def extract_text(value: Any) -> str:
    """Recursively extract human-readable text from message structures."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)

    if isinstance(value, list):
        chunks = [extract_text(item) for item in value]
        return "\n\n".join(c for c in chunks if c)

    if isinstance(value, dict):
        chunks: list[str] = []
        for key in TEXT_KEYS:
            if key in value:
                extracted = extract_text(value[key])
                if extracted:
                    chunks.append(extracted)

        if not chunks:
            skip_keys = {
                "id", "sessionid", "session_id", "messageid", "message_id",
                "role", "type", "time", "timestamp", "created", "createdat",
                "updated", "updatedat",
            }
            for key, nested_value in value.items():
                if key.lower() in skip_keys:
                    continue
                extracted = extract_text(nested_value)
                if extracted:
                    chunks.append(extracted)

        return "\n\n".join(chunks)

    return ""


def extract_turns_from_export(export: SessionExport, include_tools: bool = False) -> list[Turn]:
    """Extract turns from a SessionExport."""
    if not export.is_valid_json:
        return _extract_turns_from_raw_text(export.raw_text)

    # Try opencode native format.
    if export.messages:
        first_msg = export.messages[0] if export.messages else {}
        if (isinstance(first_msg, dict)
                and isinstance(first_msg.get("info"), dict)
                and isinstance(first_msg.get("parts"), list)):
            turns = _extract_opencode_turns(export.messages, include_tools)
            if turns is not None:
                return consolidate_turns(turns)

    # Fallback: generic walker.
    data = {"info": export.info, "messages": export.messages}
    turns = _extract_turns_generic(data, include_tools)
    return consolidate_turns(turns)


def _extract_opencode_turns(messages: list[dict[str, Any]], include_tools: bool) -> list[Turn] | None:
    """Extract turns from opencode's native export format."""
    turns: list[Turn] = []

    for msg_index, msg in enumerate(messages):
        info = msg.get("info", {})
        role = normalize_role(info.get("role"))
        parts = msg.get("parts", [])

        if role is None:
            continue
        if role == "tool" and not include_tools:
            continue

        text_chunks: list[str] = []

        for part in parts:
            if not isinstance(part, dict):
                continue

            part_type = part.get("type", "")

            if part_type == "text":
                text_value = part.get("text", "")
                if isinstance(text_value, str) and text_value.strip():
                    text_chunks.append(text_value.strip())

            elif part_type == "tool" and include_tools:
                tool_name = part.get("tool", "unknown")
                state = part.get("state", {})
                tool_input = state.get("input", {})
                tool_output = state.get("output", "")

                input_summary = ""
                if isinstance(tool_input, dict):
                    brief_keys = {k: v for k, v in tool_input.items()
                                  if isinstance(v, str) and len(v) < 200}
                    if brief_keys:
                        input_summary = ", ".join(f"{k}={v!r}" for k, v in brief_keys.items())

                if isinstance(tool_output, str) and len(tool_output) > 500:
                    tool_output = tool_output[:500] + "... (truncated)"

                tool_text = f"[Tool: {tool_name}({input_summary})]"
                if tool_output and isinstance(tool_output, str):
                    tool_text += f"\n{tool_output.strip()}"
                text_chunks.append(tool_text)

        if not text_chunks:
            continue

        combined_text = clean_text("\n\n".join(text_chunks))
        if not combined_text or is_noise(combined_text):
            continue

        turns.append(Turn(
            role=role,
            text=combined_text,
            index=len(turns) + 1,
            source=f"$.messages[{msg_index}]",
        ))

    return turns


def _extract_turns_from_raw_text(raw_text: str) -> list[Turn]:
    """Extract turns from malformed export text using regex."""
    role_pattern = re.compile(
        r'"(?:role|author|speaker)"\s*:\s*"(user|human|assistant|ai|model)"',
        re.IGNORECASE,
    )
    text_field_pattern = re.compile(
        r'"(?:content|text|message|input|output)"\s*:\s*"((?:\\.|[^"\\])*)"',
        re.DOTALL,
    )

    role_matches = list(role_pattern.finditer(raw_text))
    turns: list[Turn] = []
    seen: set[tuple[str, str]] = set()

    for match_index, role_match in enumerate(role_matches):
        role = normalize_role(role_match.group(1))
        if role not in {"user", "assistant"}:
            continue

        start = role_match.start()
        end = (
            role_matches[match_index + 1].start()
            if match_index + 1 < len(role_matches)
            else len(raw_text)
        )

        segment = raw_text[start:end]
        text_matches = list(text_field_pattern.finditer(segment))
        if not text_matches:
            continue

        best_match = max(text_matches, key=lambda c: len(c.group(1)))
        text = clean_text(_decode_jsonish_string(best_match.group(1)))

        if not text or text.lower() == role or is_noise(text):
            continue

        dedupe_key = (role, text)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        turns.append(Turn(
            role=role,
            text=text,
            index=len(turns) + 1,
            source=f"raw_text[{start}:{end}]",
        ))

    return turns


def _extract_turns_generic(data: Any, include_tools: bool) -> list[Turn]:
    """Generic recursive walker for unknown export formats."""
    turns: list[Turn] = []
    seen: set[tuple[str, str]] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            role = (
                normalize_role(value.get("role"))
                or normalize_role(value.get("author"))
                or normalize_role(value.get("speaker"))
            )

            if role is not None:
                if role == "tool" and not include_tools:
                    return
                text = clean_text(extract_text(value))
                if text and text.lower() != role and not is_noise(text):
                    dedupe_key = (role, text)
                    if dedupe_key not in seen:
                        seen.add(dedupe_key)
                        turns.append(Turn(
                            role=role, text=text,
                            index=len(turns) + 1, source=path,
                        ))
                return

            for key, nested_value in value.items():
                walk(nested_value, f"{path}.{key}")
            return

        if isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")

    walk(data, "$")
    return turns


def _decode_jsonish_string(value: str) -> str:
    """Decode a JSON-like string fragment."""
    try:
        return json.loads(f'"{value}"', strict=False)
    except json.JSONDecodeError:
        value = value.replace("\\n", "\n")
        value = value.replace("\\t", "\t")
        value = value.replace('\\"', '"')
        value = value.replace("\\\\", "\\")
        return value


def consolidate_turns(turns: list[Turn]) -> list[Turn]:
    """Merge consecutive turns with the same role."""
    if not turns:
        return turns

    consolidated: list[Turn] = []
    for turn in turns:
        if consolidated and consolidated[-1].role == turn.role:
            consolidated[-1] = Turn(
                role=consolidated[-1].role,
                text=consolidated[-1].text + "\n\n" + turn.text,
                index=consolidated[-1].index,
                source=consolidated[-1].source,
            )
        else:
            consolidated.append(Turn(
                role=turn.role,
                text=turn.text,
                index=len(consolidated) + 1,
                source=turn.source,
            ))

    return consolidated


def filter_conversation_turns(turns: list[Turn]) -> list[Turn]:
    """Keep only user and assistant turns."""
    return [t for t in turns if t.role in {"user", "assistant"}]


def count_interactions(turns: list[Turn]) -> int:
    """Count back-and-forth interactions."""
    if not turns:
        return 0
    interactions = 0
    prev_role: str | None = None
    for turn in turns:
        if turn.role == "user" and prev_role != "user":
            interactions += 1
        elif prev_role is None:
            interactions += 1
        prev_role = turn.role
    return interactions


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def truncate_turns_by_interactions(turns: list[Turn], max_interactions: int) -> list[Turn]:
    """Keep only the most recent N interactions from the tail."""
    if max_interactions <= 0:
        return turns

    boundaries: list[int] = []
    prev_role: str | None = None
    for i, turn in enumerate(turns):
        if turn.role == "user" and prev_role != "user":
            boundaries.append(i)
        elif i == 0:
            boundaries.append(i)
        prev_role = turn.role

    if len(boundaries) <= max_interactions:
        return turns

    cut_index = boundaries[-max_interactions]
    return turns[cut_index:]


def truncate_turns_by_lines(turns: list[Turn], max_lines: int) -> list[Turn]:
    """Keep enough recent turns to stay within a line budget."""
    if max_lines <= 0:
        return turns

    header_lines = 6
    budget = max_lines - header_lines
    if budget <= 0:
        return turns[-1:]

    accumulated_lines = 0
    cut_index = len(turns)

    for i in range(len(turns) - 1, -1, -1):
        turn_lines = turns[i].text.count("\n") + 1 + 3  # text + header + blanks
        if accumulated_lines + turn_lines > budget and cut_index < len(turns):
            break
        accumulated_lines += turn_lines
        cut_index = i

    return turns[cut_index:]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_transcript(turns: list[Turn], title: str) -> str:
    """Render turns as readable Markdown transcript."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = [
        f"# {title}",
        "",
        f"Generated: {generated_at}",
        "",
        "## Transcript",
        "",
    ]

    for turn in turns:
        role_label = {
            "user": "User", "assistant": "Assistant",
            "system": "System", "tool": "Tool",
        }.get(turn.role, turn.role.title())

        lines.extend([
            f"### {turn.index}. {role_label}",
            "",
            turn.text.strip(),
            "",
        ])

    return "\n".join(lines).rstrip() + "\n"


def render_restart_context(turns: list[Turn], source_name: str, session: SessionInfo) -> str:
    """Render a restart document for a fresh opencode session."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    transcript = render_transcript(turns, "Recovered opencode transcript")

    return f"""# Restart context for opencode

Generated: {generated_at}
Source export: `{source_name}`
Original session ID: `{session.session_id}`
Original session title: `{session.title}`
Original session updated: `{session.updated}`

## Instructions for the new coding agent

You are continuing a previous opencode session that became unusable during compaction or summary generation.

Read the recovered transcript below and reconstruct the working state as accurately as possible.

Focus on:
1. The user's goals and constraints.
2. Decisions already made.
3. Files changed or discussed.
4. Commands already run.
5. Errors encountered.
6. Remaining tasks.
7. Anything that was committed, pushed, or deliberately left incomplete.

Do not assume the transcript is a perfect summary. Treat it as recovered source material.

After reading it, provide a brief continuation plan. Ask for clarification only if continuing would risk damaging work.

{transcript}
"""


# ---------------------------------------------------------------------------
# Recovery File Discovery
# ---------------------------------------------------------------------------

_RECOVERY_FILE_PATTERN = re.compile(
    r"^opencode-recovery-(.+?)-(\d{8}-\d{6}Z)\.(transcript|restart|compact-prompt|compacted)\.md$"
)


def discover_recovery_files(output_dir: Path) -> list[RecoveryFile]:
    """Scan output directory for existing recovery files."""
    if not output_dir.is_dir():
        return []

    files: list[RecoveryFile] = []
    for entry in sorted(output_dir.iterdir()):
        if not entry.is_file():
            continue
        match = _RECOVERY_FILE_PATTERN.match(entry.name)
        if not match:
            continue

        session_id_fragment = match.group(1)
        timestamp = match.group(2)
        file_type = match.group(3)

        stat = entry.stat()
        try:
            line_count = entry.read_text(encoding="utf-8").count("\n") + 1
        except OSError:
            line_count = 0

        files.append(RecoveryFile(
            path=entry,
            session_id=session_id_fragment,
            file_type=file_type,
            timestamp=timestamp,
            line_count=line_count,
            size_bytes=stat.st_size,
        ))

    return files


def session_recovery_status(session_id: str, recovery_files: list[RecoveryFile]) -> str:
    """
    Determine recovery status for a session.

    Returns a short status string:
      "○" — no recovery files
      "●" — recovery files exist (transcript/restart/compact-prompt)
      "◗" — compacted file exists
    """
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-._")[:80]

    has_recovery = False
    has_compacted = False

    for f in recovery_files:
        if f.session_id == safe_id:
            if f.file_type == "compacted":
                has_compacted = True
            else:
                has_recovery = True

    if has_compacted:
        return "◗"
    elif has_recovery:
        return "●"
    return "○"


# ---------------------------------------------------------------------------
# LLM Compaction
# ---------------------------------------------------------------------------

COMPACTION_SYSTEM_PROMPT: str = (
    "You are a session-continuity assistant. Follow the instructions in the user "
    "message exactly. Produce only the requested Markdown document with no preamble "
    "or commentary."
)


def call_compaction_api(model: ModelInfo, prompt: str) -> dict[str, Any]:
    """
    Call an OpenAI-compatible chat completions API.

    Returns dict with keys: "content", "usage" (if available).
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = model.base_url.rstrip("/") + "/chat/completions"

    # Refuse to send credentials over non-HTTPS (except localhost for dev).
    parsed_url = urllib.parse.urlparse(url)
    is_local = parsed_url.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed_url.scheme != "https" and not is_local:
        raise RecoveryError(
            f"Refusing to send API key to non-HTTPS endpoint: {url}\n"
            "Only HTTPS endpoints (or localhost) are supported for security."
        )

    payload = {
        "model": model.model_id,
        "messages": [
            {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }

    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {model.api_key}",
    }

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        error_body = ""
        try:
            error_body = error.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RecoveryError(
            f"API call failed with HTTP {error.code}: {error.reason}\n{error_body}"
        ) from error
    except urllib.error.URLError as error:
        raise RecoveryError(f"API call failed: {error.reason}") from error
    except OSError as error:
        raise RecoveryError(f"API call failed: {error}") from error

    response_data = parse_json_text(response_body, "API response", strict_failure=True)

    choices = response_data.get("choices", [])
    if not choices:
        raise RecoveryError("API returned no choices in the response.")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        raise RecoveryError("API returned an empty response.")

    return {
        "content": content,
        "usage": response_data.get("usage", {}),
    }


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def write_text(path: Path, content: str) -> None:
    """Write text content to a UTF-8 file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as error:
        raise RecoveryError(f"Could not write file: {path}\n{error}") from error


def safe_filename(value: str) -> str:
    """Convert a string into a filesystem-safe filename fragment."""
    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = value.strip("-._")
    if not value:
        return "session"
    return value[:80]


# ---------------------------------------------------------------------------
# Compaction Prompt
# ---------------------------------------------------------------------------

COMPACTION_USER_PROMPT_TEMPLATE: str = """\
# Session Restart Document Generator

You are an expert session-continuity assistant. You are converting a recovered \
opencode transcript into a compact, precise Markdown restart document that \
allows a fresh opencode coding agent to continue the work safely and efficiently.

Your output will be saved to a file and read directly by a fresh opencode \
agent at the start of a new session. That agent will have no other context. \
It will rely entirely on your output.

## Source Material

- Original session ID: `{session_id}`
- Original session title: `{session_title}`
- Transcript: {turn_count} turns, {interaction_count} interactions, \
{line_count} lines.
- Truncation: {truncation_note}

The transcript was recovered from an opencode session that became unusable \
(compaction failure, context overflow, crash, or similar). It may contain \
user messages, agent responses, partial tool-call details, repeated status \
text, errors, incomplete sections, and references to files, commands, commits, \
tests, or decisions. It may be incomplete.

The most recent exchanges reflect the user's active working context at the \
time the session ended.

## Core Rules

1. Do not invent information.
2. Only include claims supported by the transcript.
3. If something is likely but not certain, label it as "Inference:".
4. Preserve exact file paths, command names, branch names, commit hashes, \
package names, error messages, and version/tool details when they matter.
5. Do not include long raw code blocks unless essential to understanding \
the current state, a bug, or a decision. Prefer concise summaries.
6. Capture objectives, constraints, preferences, and reasoning behind \
important decisions.
7. Identify what was completed, what remains, and what must not be redone.
8. Preserve operational details the next coding agent would need.
9. If the transcript is truncated or incomplete, say what is missing and \
how that affects confidence.
10. Do not include instructions for the user.
11. Do not include a suggested message for the user to paste.
12. Write the output as context and instructions for the opencode agent only.

---
{prior_context_section}
## Transcript

```text
{transcript_content}
```

---

## Output Requirements

Now produce a single Markdown document with the following structure. \
Consider the entire transcript for context, but give particular weight to \
the most recent exchanges when determining current state, active intent, \
and immediate next steps.

# Restart Context for opencode

## 1. Project Summary

In 2 to 4 sentences: what the session was about, what project or task was \
being worked on, and why.

## 2. Current State

Note any uncertainty caused by transcript gaps or missing tool-call details.

Use bullets. Include:

- What appears complete and working.
- What is in progress.
- What was planned but not started.
- What was committed, pushed, tested, or verified, if evidenced.

## 3. Key Decisions and Constraints

List decisions and constraints the next agent must respect.

Include:

- User preferences.
- Technical constraints.
- Design decisions.
- Testing or validation expectations.
- Anything the user explicitly rejected, deferred, or asked not to redo.
- The reasoning behind decisions when the transcript includes it.

## 4. Files and Structure

List only important files, directories, scripts, configs, or generated \
artifacts that matter for continuing.

For each item include: path or filename, whether it was created/modified/\
reviewed/generated/discussed, what role it plays, and any known status or risk.

Do not list every file unless every file is truly important.

## 5. Technical Context

Summarize the relevant technical environment. Include when evidenced:

- Tools and CLIs used.
- Programming languages and frameworks.
- OS or shell details.
- Repository or branch details.
- Package managers.
- External services or APIs.
- Commands that were important.
- Non-obvious behavior discovered during the session.

## 6. Errors, Failures, and Workarounds

Document problems encountered and how they were handled. For each include: \
exact error message if available, likely cause (only if evidenced or clearly \
marked as inference), workaround or resolution, and whether the issue is \
fully resolved or still open.

## 7. What Not to Redo

Direct list of work the next agent must not repeat, overwrite, regenerate, \
or second-guess unless the user explicitly asks. Include anything already: \
completed, committed, pushed, tested, validated, rejected, or deferred.

## 8. Immediate Next Steps for the Agent

Concrete continuation plan using ordered steps. Steps should be specific \
enough that the agent can begin work immediately.

Include:

- What to inspect first.
- What command to run first, if applicable.
- What file to open first, if applicable.
- What to verify before making changes.
- What user intent was active at the end of the session.
- Any caution required before editing, testing, committing, or pushing.

## 9. Open Questions and Risks

Separate into:

- Questions that must be answered before safe continuation.
- Risks the agent should handle cautiously.
- Transcript gaps or ambiguities.

Do not ask the user questions unless continuing would risk damaging work or \
contradicting prior decisions.

## Agent Operating Guidance

Before making changes, verify the repository state with appropriate \
read-only commands.

Do not redo work marked as complete, committed, pushed, tested, validated, \
rejected, or deferred unless the user explicitly asks.

Prefer minimal, targeted changes that continue from the recovered state.

If the transcript conflicts with the repository state, trust the repository \
state for file contents and the transcript for user intent, then explain \
the discrepancy before acting.

## Style

- Concise but complete.
- Markdown headings and bullets.
- No generic filler, motivational language, or speculation.
- "Evidence:" notes only when needed to distinguish facts from inference.
- "Inference:" labels for likely conclusions not directly stated.
- Do not apologize or mention these instructions in the output.
- Do not include any content addressed to the user.
"""


def render_compact_prompt(
    turns: list[Turn],
    session: SessionInfo,
    total_turns_before_truncation: int | None = None,
    prior_context: str = "",
) -> str:
    """
    Render the compaction prompt with the transcript embedded.

    Args:
        turns: Conversation turns to include.
        session: Selected session metadata.
        total_turns_before_truncation: Original turn count if truncated.
        prior_context: Formatted prior context string.

    Returns:
        The fully rendered compaction prompt.
    """
    transcript = render_transcript(turns, "Recovered transcript")
    turn_count = len(turns)
    interaction_count = count_interactions(turns)
    line_count = transcript.count("\n") + 1

    if total_turns_before_truncation is not None and total_turns_before_truncation > turn_count:
        skipped = total_turns_before_truncation - turn_count
        truncation_note = (
            f"Truncated to the most recent {turn_count} turns "
            f"({skipped} older turns omitted from a session of "
            f"{total_turns_before_truncation} total turns)."
        )
    else:
        truncation_note = "Complete (no truncation applied)."

    if prior_context:
        truncation_note += " Prior session context is included below."
        prior_context_section = "\n" + prior_context + "\n"
    else:
        prior_context_section = ""

    # Escape braces in user-provided strings.
    safe_session_id = session.session_id.replace("{", "{{").replace("}", "}}")
    safe_session_title = session.title.replace("{", "{{").replace("}", "}}")

    return COMPACTION_USER_PROMPT_TEMPLATE.format(
        session_id=safe_session_id,
        session_title=safe_session_title,
        turn_count=turn_count,
        interaction_count=interaction_count,
        line_count=line_count,
        truncation_note=truncation_note,
        prior_context_section=prior_context_section,
        transcript_content=transcript,
    )


def generate_recovery_files(
    turns: list[Turn],
    session: SessionInfo,
    output_dir: Path,
    export_name: str = "export.json",
    total_turns_before_truncation: int | None = None,
    prior_context: str = "",
) -> dict[str, Path]:
    """
    Generate all recovery files (transcript, restart, compact-prompt).

    Args:
        turns: Conversation turns to write.
        session: Session metadata.
        output_dir: Output directory.
        export_name: Name of the source export file.
        total_turns_before_truncation: Original count if truncated.
        prior_context: Prior context string for compact prompt.

    Returns:
        Dict mapping file type to path: {"transcript": ..., "restart": ..., "compact_prompt": ...}
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    safe_id = safe_filename(session.session_id)
    base_name = f"opencode-recovery-{safe_id}-{timestamp}"

    transcript_path = output_dir / f"{base_name}.transcript.md"
    restart_path = output_dir / f"{base_name}.restart.md"
    compact_prompt_path = output_dir / f"{base_name}.compact-prompt.md"

    write_text(
        transcript_path,
        render_transcript(turns, "Recovered opencode transcript"),
    )

    write_text(
        restart_path,
        render_restart_context(turns, export_name, session),
    )

    write_text(
        compact_prompt_path,
        render_compact_prompt(
            turns, session,
            total_turns_before_truncation=total_turns_before_truncation,
            prior_context=prior_context,
        ),
    )

    return {
        "transcript": transcript_path,
        "restart": restart_path,
        "compact_prompt": compact_prompt_path,
    }
