#!/usr/bin/env python3
"""
Interactively export and recover an opencode session.

This utility helps recover from broken opencode sessions by:
1. Listing available opencode sessions.
2. Letting the user interactively select a session.
3. Exporting the selected session to a temporary JSON file.
4. Extracting user and assistant interactions.
5. Generating restart-friendly Markdown files.
6. Cleaning up temporary files, including after CTRL-C or failure.

Example:
    ./opencode_recover_session.py

Example with verbose output:
    ./opencode_recover_session.py -v

Example with very verbose output:
    ./opencode_recover_session.py -vv

Example with explicit output directory:
    ./opencode_recover_session.py --out ./opencode-recovery

Example preserving the temporary exported JSON:
    ./opencode_recover_session.py --keep-temp

Example non-interactive use with a known session ID:
    ./opencode_recover_session.py --session SESSION_ID

Example recovering a session from a different project directory:
    ./opencode_recover_session.py --session-dir /path/to/project

Example cleaning up leftover temporary files before recovering:
    ./opencode_recover_session.py --clean

Example removing previous recovery output for the selected session:
    ./opencode_recover_session.py --clean-previous --session SESSION_ID

Notes:
    This script does not call any external APIs.

    It requires the `opencode` CLI to be installed and available on PATH.

    It intentionally avoids third-party Python packages so that it can run on
    a clean system Python installation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

_COLOR_SUPPORTED: bool = (
    hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
)


def _ansi(code: str, text: str) -> str:
    """Wrap text with an ANSI escape sequence if color is supported."""
    if _COLOR_SUPPORTED:
        return f"\033[{code}m{text}\033[0m"
    return text


def color_bold(text: str) -> str:
    """Bold text."""
    return _ansi("1", text)


def color_green(text: str) -> str:
    """Green text."""
    return _ansi("32", text)


def color_yellow(text: str) -> str:
    """Yellow/warning text."""
    return _ansi("33", text)


def color_red(text: str) -> str:
    """Red/error text."""
    return _ansi("31", text)


def color_cyan(text: str) -> str:
    """Cyan/info text."""
    return _ansi("36", text)


def color_dim(text: str) -> str:
    """Dim/muted text."""
    return _ansi("2", text)


# ---------------------------------------------------------------------------
# Threshold constants
# ---------------------------------------------------------------------------

LONG_SESSION_LINE_THRESHOLD: int = 2500
LONG_SESSION_INTERACTION_THRESHOLD: int = 100

# Rough token estimation: ~4 characters per token for English text.
CHARS_PER_TOKEN_ESTIMATE: float = 4.0

# OpenAI-compatible provider npm packages.
OPENAI_COMPATIBLE_PACKAGES: set[str] = {
    "@ai-sdk/openai",
    "@ai-sdk/openai-compatible",
}

# Default opencode config search paths.
OPENCODE_CONFIG_PATHS: tuple[Path, ...] = (
    Path.home() / ".config" / "opencode" / "opencode.json",
    Path.home() / ".config" / "opencode" / "opencode.jsonc",
    Path("opencode.json"),
    Path("opencode.jsonc"),
)


@dataclass
class ModelInfo:
    """
    Represents a model available for compaction.

    Attributes:
        provider_id:
            The provider key in the config (e.g., "uri", "openai").

        model_id:
            The model key within the provider (e.g., "its_direct/pt1-qwen3-32b-us").

        name:
            Human-readable model name.

        base_url:
            API base URL for the provider.

        api_key:
            API key for authentication.

        cost_input:
            Cost per million input tokens, or None if unknown.

        cost_output:
            Cost per million output tokens, or None if unknown.

        compatible:
            Whether the provider uses an OpenAI-compatible API.
    """

    provider_id: str
    model_id: str
    name: str
    base_url: str
    api_key: str
    cost_input: float | None
    cost_output: float | None
    compatible: bool


def strip_jsonc_comments(text: str) -> str:
    """
    Strip single-line (//) and block (/* */) comments from JSONC text.

    Args:
        text:
            JSONC content.

    Returns:
        JSON-compatible text with comments removed.
    """

    # Remove block comments first, then line comments.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def load_opencode_config(verbosity: int = 0) -> dict[str, Any]:
    """
    Load the opencode configuration file.

    Searches the standard config paths and returns the first one found.

    Args:
        verbosity:
            Current verbosity level.

    Returns:
        Parsed config dictionary.

    Raises:
        RecoveryError:
            If no config file is found or it cannot be parsed.
    """

    for config_path in OPENCODE_CONFIG_PATHS:
        if config_path.exists():
            log(f"Loading config from: {config_path}", verbosity)
            try:
                raw = config_path.read_text(encoding="utf-8")
            except OSError as error:
                raise RecoveryError(f"Could not read config: {config_path}\n{error}") from error

            # Handle JSONC (comments). Only strip if explicitly a .jsonc file.
            if config_path.suffix == ".jsonc":
                raw = strip_jsonc_comments(raw)

            parsed = parse_json_text(raw, f"config file {config_path}", strict_failure=True)
            return parsed

    searched = ", ".join(str(p) for p in OPENCODE_CONFIG_PATHS)
    raise RecoveryError(
        f"No opencode config file found. Searched:\n  {searched}"
    )


def extract_models_from_config(config: dict[str, Any]) -> list[ModelInfo]:
    """
    Extract all available models from the opencode config.

    Args:
        config:
            Parsed opencode config.

    Returns:
        List of ModelInfo for all providers with OpenAI-compatible APIs.
    """

    providers = config.get("provider", {})
    models: list[ModelInfo] = []

    for provider_id, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue

        npm_package = provider_data.get("npm", "")
        compatible = npm_package in OPENAI_COMPATIBLE_PACKAGES

        options = provider_data.get("options", {})
        base_url = options.get("baseURL", "")
        api_key = options.get("apiKey", "")

        # For standard OpenAI provider, default baseURL.
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


def display_models(models: list[ModelInfo]) -> None:
    """
    Display available models in a compact table format.

    Args:
        models:
            Models to display.
    """

    if not models:
        print(color_dim("No models found in opencode config."))
        return

    # Sort by input cost then output cost (low to high), then by name.
    def sort_key(m: ModelInfo) -> tuple[float, float, str]:
        return (m.cost_input or 999, m.cost_output or 999, m.name)

    sorted_models = sorted(models, key=sort_key)

    # Compute column widths.
    id_col = "MODEL (--use-model)"
    name_col = "NAME"
    cost_col = "COST (in/out)"
    compat_col = "API"

    rows: list[tuple[str, str, str, str]] = []
    for m in sorted_models:
        full_id = f"{m.provider_id}/{m.model_id}"
        if m.cost_input is not None and m.cost_output is not None:
            cost_str = f"${m.cost_input:.2f} / ${m.cost_output:.2f}"
        else:
            cost_str = "—"
        compat_str = "OK" if m.compatible else "N/A"
        rows.append((full_id, m.name, cost_str, compat_str))

    id_width = max(len(id_col), max(len(r[0]) for r in rows))
    name_width = max(len(name_col), max(len(r[1]) for r in rows))
    cost_width = max(len(cost_col), max(len(r[2]) for r in rows))
    compat_width = max(len(compat_col), max(len(r[3]) for r in rows))

    header = (
        f"  {color_bold(id_col.ljust(id_width))}  "
        f"{color_bold(name_col.ljust(name_width))}  "
        f"{color_bold(cost_col.ljust(cost_width))}  "
        f"{color_bold(compat_col.ljust(compat_width))}"
    )
    separator = f"  {'─' * id_width}  {'─' * name_width}  {'─' * cost_width}  {'─' * compat_width}"

    print()
    print(color_bold(f"Available models ({len(sorted_models)}):"))
    print()
    print(header)
    print(separator)

    for full_id, name, cost_str, compat_str in rows:
        compat_display = color_green(compat_str) if compat_str == "OK" else color_dim(compat_str)
        print(
            f"  {color_cyan(full_id.ljust(id_width))}  "
            f"{name.ljust(name_width)}  "
            f"{cost_str.ljust(cost_width)}  "
            f"{compat_display}"
        )

    print()
    print(color_dim("Only models with API=OK support compaction via --use-model."))
    print()


def resolve_model(models: list[ModelInfo], model_spec: str) -> ModelInfo:
    """
    Resolve a --use-model specification to a ModelInfo.

    The spec can be "provider/model_id" (exact) or a substring match.

    Args:
        models:
            Available models.

        model_spec:
            User-provided model specification.

    Returns:
        Matching ModelInfo.

    Raises:
        RecoveryError:
            If the model is not found, ambiguous, or not compatible.
    """

    # Try exact match first.
    for m in models:
        full_id = f"{m.provider_id}/{m.model_id}"
        if full_id == model_spec:
            if not m.compatible:
                raise RecoveryError(
                    f"Model {model_spec} uses a non-OpenAI-compatible API and cannot be used for compaction."
                )
            if not m.api_key:
                raise RecoveryError(f"Model {model_spec} has no API key configured.")
            if not m.base_url:
                raise RecoveryError(f"Model {model_spec} has no base URL configured.")
            return m

    # Try substring match.
    matches = [
        m for m in models
        if model_spec in f"{m.provider_id}/{m.model_id}" or model_spec in m.name.lower()
    ]

    if not matches:
        raise RecoveryError(
            f"Model not found: {model_spec!r}\n"
            "Use --show-models to see available models."
        )

    if len(matches) > 1:
        match_names = [f"  {m.provider_id}/{m.model_id} ({m.name})" for m in matches[:10]]
        raise RecoveryError(
            f"Ambiguous model spec {model_spec!r}. Matches:\n" + "\n".join(match_names)
        )

    matched = matches[0]
    if not matched.compatible:
        raise RecoveryError(
            f"Model {matched.provider_id}/{matched.model_id} uses a non-OpenAI-compatible API."
        )
    if not matched.api_key:
        raise RecoveryError(f"Model {matched.provider_id}/{matched.model_id} has no API key configured.")
    if not matched.base_url:
        raise RecoveryError(f"Model {matched.provider_id}/{matched.model_id} has no base URL configured.")

    return matched


def estimate_tokens(text: str) -> int:
    """
    Estimate token count for a text string.

    Uses a rough heuristic of ~4 characters per token for English.

    Args:
        text:
            Input text.

    Returns:
        Estimated token count.
    """

    return max(1, int(len(text) / CHARS_PER_TOKEN_ESTIMATE))


def estimate_cost(input_tokens: int, output_tokens: int, model: ModelInfo) -> float | None:
    """
    Estimate the cost of an API call.

    Args:
        input_tokens:
            Estimated input token count.

        output_tokens:
            Estimated output token count.

        model:
            Model with cost information.

    Returns:
        Estimated cost in dollars, or None if cost info unavailable.
    """

    if model.cost_input is None or model.cost_output is None:
        return None

    input_cost = (input_tokens / 1_000_000) * model.cost_input
    output_cost = (output_tokens / 1_000_000) * model.cost_output
    return input_cost + output_cost


def call_compaction_api(
    model: ModelInfo,
    prompt: str,
    verbosity: int,
) -> str:
    """
    Call an OpenAI-compatible chat completions API for session compaction.

    Args:
        model:
            The resolved model to use.

        prompt:
            The full prompt to send (including transcript and instructions).

        verbosity:
            Current verbosity level.

    Returns:
        The model's response text.

    Raises:
        RecoveryError:
            If the API call fails.
    """

    url = model.base_url.rstrip("/") + "/chat/completions"
    log(f"Calling API: {url}", verbosity)
    log(f"Model: {model.model_id}", verbosity)

    payload = {
        "model": model.model_id,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise summarization assistant. Your task is to produce "
                    "a compact, accurate Markdown restart document from a recovered "
                    "conversation transcript. Be thorough but succinct. Preserve all "
                    "critical details: decisions made, files changed, commands run, "
                    "errors encountered, and next steps. Do NOT invent information."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
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

    log(f"Response length: {len(response_body)} bytes", verbosity)

    response_data = parse_json_text(response_body, "API response", strict_failure=True)

    choices = response_data.get("choices", [])
    if not choices:
        raise RecoveryError("API returned no choices in the response.")

    message = choices[0].get("message", {})
    content = message.get("content", "")

    if not content:
        raise RecoveryError("API returned an empty response.")

    # Report actual usage if available.
    usage = response_data.get("usage", {})
    if usage:
        actual_input = usage.get("prompt_tokens", 0)
        actual_output = usage.get("completion_tokens", 0)
        log(f"Actual tokens — input: {actual_input}, output: {actual_output}", verbosity)
        if model.cost_input is not None and model.cost_output is not None:
            actual_cost = estimate_cost(actual_input, actual_output, model)
            if actual_cost is not None:
                log(f"Actual cost: ${actual_cost:.4f}", verbosity)

    return content


@dataclass
class SessionInfo:
    """
    Represents a discovered opencode session.

    Attributes:
        session_id:
            The opencode session identifier.

        title:
            A human-readable title or summary when available.

        created:
            Creation timestamp when available.

        updated:
            Last updated timestamp when available.

        raw:
            The original JSON object returned by opencode.

    Example:
        SessionInfo(
            session_id="ses_abc123",
            title="Fix authentication bug",
            created="2026-05-30T12:00:00Z",
            updated="2026-05-30T13:15:00Z",
            raw={...},
        )
    """

    session_id: str
    title: str
    created: str
    updated: str
    raw: dict[str, Any]


@dataclass
class Turn:
    """
    Represents one extracted conversational turn.

    Attributes:
        role:
            The speaker role, usually "user", "assistant", "system", or "tool".

        text:
            The extracted text content for the turn.

        index:
            The order in which the turn was discovered in the exported JSON.

        source:
            A short description of where this turn appeared in the export.

    Example:
        Turn(
            role="user",
            text="Please fix the bug.",
            index=12,
            source="$.messages[4]",
        )
    """

    role: str
    text: str
    index: int
    source: str


class RecoveryError(Exception):
    """
    Raised when the recovery workflow cannot continue safely.

    Example:
        raise RecoveryError("opencode CLI was not found on PATH.")
    """

    pass


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
    "content",
    "text",
    "message",
    "input",
    "output",
    "result",
    "summary",
)


SESSION_ID_KEYS: tuple[str, ...] = (
    "id",
    "sessionID",
    "sessionId",
    "session_id",
)


SESSION_TITLE_KEYS: tuple[str, ...] = (
    "title",
    "summary",
    "description",
    "name",
)


SESSION_CREATED_KEYS: tuple[str, ...] = (
    "created",
    "createdAt",
    "created_at",
    "timeCreated",
)


SESSION_UPDATED_KEYS: tuple[str, ...] = (
    "updated",
    "updatedAt",
    "updated_at",
    "timeUpdated",
    "modified",
    "modifiedAt",
)


NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*Tool call not allowed while generating summary", re.IGNORECASE),
    re.compile(r"^\s*Where were we\?\s*$", re.IGNORECASE),
    re.compile(r"^\s*\[System: Empty message content sanitised to satisfy protocol\]\s*$"),
)


# Lines matching these patterns are stripped from extracted text during cleanup.
NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*\[System: Empty message content sanitised to satisfy protocol\]\s*$"),
)


def eprint(message: str) -> None:
    """
    Print a message to stderr.

    Args:
        message:
            Message to print.
    """

    print(message, file=sys.stderr)
    pass


def log(message: str, verbosity: int, required_level: int = 1) -> None:
    """
    Print a progress message when verbosity is high enough.

    Args:
        message:
            Message to print.

        verbosity:
            Current verbosity level.

        required_level:
            Minimum verbosity required to print the message.
    """

    if verbosity >= required_level:
        eprint(color_dim(message))
    pass


def die(message: str, exit_code: int = 1) -> None:
    """
    Exit with an error message.

    Args:
        message:
            Error message.

        exit_code:
            Process exit code.
    """

    eprint(color_red(f"Error: {message}"))
    raise SystemExit(exit_code)


def require_opencode() -> None:
    """
    Ensure the opencode CLI is available.

    Raises:
        RecoveryError:
            If opencode is not found on PATH.
    """

    if shutil.which("opencode") is None:
        raise RecoveryError(
            "The `opencode` CLI was not found on PATH. Install opencode or add it to PATH first."
        )
    pass


def run_command(
    command: Sequence[str],
    verbosity: int,
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Run a subprocess command safely.

    Args:
        command:
            Command and arguments to execute.

        verbosity:
            Current verbosity level.

        check:
            Whether to raise RecoveryError on non-zero exit.

        cwd:
            Working directory to run the command in. When None, inherits the
            current process working directory.

    Returns:
        The completed process.

    Raises:
        RecoveryError:
            If the command fails and check is True.
    """

    log(f"Running command: {' '.join(command)}", verbosity, required_level=2)
    if cwd is not None:
        log(f"  Working directory: {cwd}", verbosity, required_level=2)

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

    if verbosity >= 2 and completed.stdout.strip():
        log(f"Command stdout:\n{completed.stdout.strip()}", verbosity, required_level=2)

    if verbosity >= 2 and completed.stderr.strip():
        log(f"Command stderr:\n{completed.stderr.strip()}", verbosity, required_level=2)

    if check and completed.returncode != 0:
        raise RecoveryError(
            "Command failed with exit code "
            f"{completed.returncode}: {' '.join(command)}\n"
            f"{completed.stderr.strip() or completed.stdout.strip() or 'No output'}"
        )

    return completed


def parse_json_text(text: str, context: str, strict_failure: bool = True) -> Any:
    """
    Parse JSON text with a helpful error message.

    Args:
        text:
            JSON text.

        context:
            Description of what is being parsed.

        strict_failure:
            When True, raise RecoveryError if parsing fails.
            When False, return None if parsing fails.

    Returns:
        Parsed JSON data, or None when strict_failure is False and parsing fails.

    Raises:
        RecoveryError:
            If the JSON cannot be parsed and strict_failure is True.
    """

    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        # Some JSON exports include raw control characters in strings.
        # strict=False tolerates those, but it will not fix truly truncated JSON.
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


def first_present_string(data: dict[str, Any], keys: Iterable[str]) -> str:
    """
    Return the first present string-like field from a dictionary.

    Args:
        data:
            Source dictionary.

        keys:
            Candidate keys in priority order.

    Returns:
        String value, or an empty string.
    """

    for key in keys:
        value = data.get(key)

        if value is None:
            continue

        if isinstance(value, str):
            return value.strip()

        if isinstance(value, (int, float, bool)):
            return str(value)

        pass

    return ""


def extract_session_objects(value: Any) -> list[dict[str, Any]]:
    """
    Extract candidate session dictionaries from arbitrary JSON.

    Args:
        value:
            Parsed JSON returned by `opencode session list --format json`.

    Returns:
        A list of dictionaries that appear to represent sessions.
    """

    candidates: list[dict[str, Any]] = []

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                candidates.append(item)
            pass

        return candidates

    if isinstance(value, dict):
        for key in ("sessions", "data", "items", "results"):
            nested = value.get(key)

            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict):
                        candidates.append(item)
                    pass
                pass
            pass

        if not candidates and any(key in value for key in SESSION_ID_KEYS):
            candidates.append(value)

    return candidates


def normalize_sessions(raw_sessions: list[dict[str, Any]]) -> list[SessionInfo]:
    """
    Normalize raw opencode session objects into SessionInfo records.

    Args:
        raw_sessions:
            Candidate session dictionaries.

    Returns:
        Normalized sessions with a usable session ID.
    """

    sessions: list[SessionInfo] = []

    for raw in raw_sessions:
        session_id = first_present_string(raw, SESSION_ID_KEYS)

        if not session_id:
            continue

        title = first_present_string(raw, SESSION_TITLE_KEYS)
        created = first_present_string(raw, SESSION_CREATED_KEYS)
        updated = first_present_string(raw, SESSION_UPDATED_KEYS)

        sessions.append(
            SessionInfo(
                session_id=session_id,
                title=title or "(untitled)",
                created=created or "unknown",
                updated=updated or "unknown",
                raw=raw,
            )
        )
        pass

    return sessions


def list_sessions(verbosity: int, cwd: Path | None = None) -> list[SessionInfo]:
    """
    Retrieve opencode sessions from the local opencode CLI.

    Args:
        verbosity:
            Current verbosity level.

        cwd:
            Working directory to run opencode in (the directory where the
            session was originally created). When None, uses the current
            process working directory.

    Returns:
        A list of normalized sessions.

    Raises:
        RecoveryError:
            If the session list cannot be retrieved or parsed.
    """

    log("Finding opencode sessions...", verbosity)

    completed = run_command(
        ("opencode", "session", "list", "--format", "json"),
        verbosity=verbosity,
        check=True,
        cwd=cwd,
    )

    data = parse_json_text(completed.stdout, "opencode session list")
    raw_sessions = extract_session_objects(data)
    sessions = normalize_sessions(raw_sessions)

    if not sessions:
        raise RecoveryError(
            "No sessions were found in the opencode session list output. "
            "Run `opencode session list --format json` manually to inspect the output shape."
        )

    return sessions


def truncate(value: str, length: int) -> str:
    """
    Truncate a string for display.

    Args:
        value:
            Source string.

        length:
            Maximum display length.

    Returns:
        Truncated string.
    """

    value = value.strip()

    if len(value) <= length:
        return value

    return value[: max(0, length - 3)] + "..."


def format_timestamp(value: str) -> str:
    """
    Format a timestamp string for display, appending a human-readable date.

    Handles Unix epoch milliseconds, Unix epoch seconds, and ISO 8601 strings.
    If the value cannot be parsed, it is returned unchanged.

    Args:
        value:
            Raw timestamp string (e.g. "1780168353756" or "2026-05-30T12:00:00Z").

    Returns:
        The original value with a formatted date appended, or the original value
        unchanged if parsing fails.
    """

    if not value or value == "unknown":
        return value

    # Try Unix epoch (milliseconds or seconds).
    if value.isascii() and value.isdigit():
        epoch = int(value)

        # Heuristic: if the number is larger than year-2100 in seconds (~4102444800),
        # assume milliseconds.
        if epoch > 4_102_444_800:
            epoch_seconds = epoch / 1000.0
        else:
            epoch_seconds = float(epoch)

        try:
            dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
            formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
            return f"{value} ({formatted})"
        except (OSError, ValueError, OverflowError):
            return value

    # Try ISO 8601 parsing.
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            formatted = dt.strftime("%Y-%m-%d %H:%M:%S")
            if formatted in value:
                return value
            return f"{value} ({formatted})"
        except ValueError:
            continue

    return value


def display_sessions(sessions: list[SessionInfo]) -> None:
    """
    Display sessions in an interactive numbered list.

    Args:
        sessions:
            Sessions to display.
    """

    print()
    print(color_bold("Available opencode sessions"))
    print()

    index_width = len(str(len(sessions)))

    for index, session in enumerate(sessions, start=1):
        title = truncate(session.title, 72)

        print(f"{color_cyan(f'{index:>{index_width}}.')} {color_bold(title)}")
        print(f"    ID:      {color_dim(session.session_id)}")
        print(f"    Updated: {format_timestamp(session.updated)}")
        print(f"    Created: {format_timestamp(session.created)}")
        print()

    pass


def collapse_to_preview(text: str, max_chars: int = 100) -> str:
    """
    Collapse a multi-line text into a single-line preview.

    Replaces newlines and excessive whitespace with single spaces, then
    truncates to max_chars.

    Args:
        text:
            Source text.

        max_chars:
            Maximum characters in the preview.

    Returns:
        A collapsed single-line preview string.
    """

    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[:max_chars - 3] + "..."


def display_turn_preview(turns: list["Turn"], max_preview: int = 20) -> None:
    """
    Display a preview of the last N back-and-forths in the recovered session.

    Shows the first 100 characters of each turn (with line breaks collapsed)
    so the user can verify the session tail looks correct and wasn't truncated
    mid-conversation.

    Args:
        turns:
            The final selected turns to be written.

        max_preview:
            Maximum number of turns to show (from the tail).
    """

    if not turns:
        return

    preview_turns = turns[-max_preview:]
    skipped = len(turns) - len(preview_turns)

    print(color_bold("Session tail preview:"))
    if skipped > 0:
        print(color_dim(f"  ... ({skipped} earlier turns omitted)"))

    for turn in preview_turns:
        role_label = "U" if turn.role == "user" else "A"
        preview = collapse_to_preview(turn.text)

        if turn.role == "user":
            print(f"  {color_cyan(role_label)}: {preview}")
        else:
            print(f"  {color_dim(role_label)}: {preview}")

    print()


def prompt_for_session(sessions: list[SessionInfo]) -> SessionInfo:
    """
    Prompt the user to select a session interactively.

    Args:
        sessions:
            Sessions to choose from.

    Returns:
        The selected session.

    Raises:
        KeyboardInterrupt:
            If the user presses CTRL-C.
    """

    display_sessions(sessions)

    while True:
        selection = input("Select a session number, or type q to quit: ").strip()

        if selection.lower() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt

        if not selection.isdigit():
            print("Please enter a number from the list.")
            continue

        index = int(selection)

        if index < 1 or index > len(sessions):
            print(f"Please enter a number between 1 and {len(sessions)}.")
            continue

        return sessions[index - 1]


def write_export_to_temp(
    session_id: str,
    temp_dir: Path,
    verbosity: int,
    cwd: Path | None = None,
) -> Path:
    """
    Export an opencode session to a temporary file.

    Args:
        session_id:
            opencode session ID.

        temp_dir:
            Temporary directory.

        verbosity:
            Current verbosity level.

        cwd:
            Working directory to run opencode in. When None, uses the current
            process working directory.

    Returns:
        Path to the exported session file.

    Raises:
        RecoveryError:
            If export fails or produces no output.

    Notes:
        The raw export is written before JSON validation. This is intentional:
        if opencode emits malformed JSON, the recovery script can still use
        best-effort text extraction and the user does not lose the export.
    """

    export_path = temp_dir / f"opencode-session-{session_id}.json"

    log(f"Exporting selected session: {session_id}", verbosity)

    # Write stdout directly to the export file instead of capturing via PIPE.
    # opencode export can produce very large output (tens of MB) and
    # subprocess.PIPE truncates it on some platforms (notably WSL/Windows).
    command = ["opencode", "export", session_id]
    log(f"Running command: {' '.join(command)}", verbosity, required_level=2)
    if cwd is not None:
        log(f"  Working directory: {cwd}", verbosity, required_level=2)

    try:
        # Open with restricted permissions (owner read/write only) to avoid
        # exposing session data on shared systems.
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

    log(f"Export file size: {export_path.stat().st_size} bytes", verbosity)
    log(f"Temporary export written to: {export_path}", verbosity)

    return export_path


def load_export_file(path: Path, verbosity: int) -> Any:
    """
    Load an opencode export file as JSON when possible, otherwise raw text.

    Args:
        path:
            Export file path.

        verbosity:
            Current verbosity level.

    Returns:
        Parsed JSON data, or raw text when JSON parsing fails.

    Raises:
        RecoveryError:
            If the file cannot be read.
    """

    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise RecoveryError(f"Export file not found: {path}") from error
    except OSError as error:
        raise RecoveryError(f"Could not read export file: {path}\n{error}") from error

    parsed = parse_json_text(
        raw_text,
        f"export file {path}",
        strict_failure=False,
    )

    if parsed is None:
        log("Using raw text fallback parser for malformed export.", verbosity)
        return raw_text

    return parsed


def normalize_role(value: Any) -> str | None:
    """
    Normalize a role value to a known role.

    Args:
        value:
            Any value that might represent a message role.

    Returns:
        A normalized role string, or None when no known role is found.
    """

    if not isinstance(value, str):
        return None

    lowered = value.strip().lower()
    return ROLE_ALIASES.get(lowered)


def clean_text(text: str) -> str:
    """
    Normalize whitespace in extracted text without destroying code blocks.

    Removes lines matching NOISE_LINE_PATTERNS and collapses excessive blank lines.

    Args:
        text:
            Raw text extracted from the export.

    Returns:
        Cleaned text.
    """

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove noise lines.
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
    """
    Decide whether a turn is likely recovery noise rather than useful content.

    Args:
        text:
            Cleaned turn text.

    Returns:
        True if the text should be skipped by default.
    """

    for pattern in NOISE_PATTERNS:
        if pattern.search(text):
            return True
        pass

    return False


def extract_text(value: Any) -> str:
    """
    Recursively extract human-readable text from common message structures.

    Args:
        value:
            Any JSON value that may contain text.

    Returns:
        A string containing extracted text, or an empty string when none is found.
    """

    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, (int, float, bool)):
        return str(value)

    if isinstance(value, list):
        chunks: list[str] = []

        for item in value:
            extracted = extract_text(item)
            if extracted:
                chunks.append(extracted)
            pass

        return "\n\n".join(chunks)

    if isinstance(value, dict):
        chunks: list[str] = []

        for key in TEXT_KEYS:
            if key in value:
                extracted = extract_text(value[key])
                if extracted:
                    chunks.append(extracted)
                pass
            pass

        if not chunks:
            for key, nested_value in value.items():
                lowered_key = key.lower()

                if lowered_key in {
                    "id",
                    "sessionid",
                    "session_id",
                    "messageid",
                    "message_id",
                    "role",
                    "type",
                    "time",
                    "timestamp",
                    "created",
                    "createdat",
                    "updated",
                    "updatedat",
                }:
                    continue

                extracted = extract_text(nested_value)
                if extracted:
                    chunks.append(extracted)
                pass
            pass

        return "\n\n".join(chunks)

    return ""

def decode_jsonish_string(value: str) -> str:
    """
    Decode a JSON-like string fragment as safely as possible.

    Args:
        value:
            String content captured from raw export text.

    Returns:
        Decoded text.
    """

    try:
        return json.loads(f'"{value}"', strict=False)
    except json.JSONDecodeError:
        # Best-effort cleanup for malformed or truncated JSON strings.
        value = value.replace("\\n", "\n")
        value = value.replace("\\t", "\t")
        value = value.replace('\\"', '"')
        value = value.replace("\\\\", "\\")
        return value


def extract_turns_from_raw_text(raw_text: str, verbosity: int) -> list[Turn]:
    """
    Extract likely user and assistant turns from malformed opencode export text.

    Args:
        raw_text:
            Raw text emitted by `opencode export`.

        verbosity:
            Current verbosity level.

    Returns:
        Best-effort list of conversation turns.

    Notes:
        This parser is deliberately conservative. It scans for JSON-like role
        markers and then looks nearby for text-bearing fields. It is meant as a
        recovery path when the normal JSON export is malformed or truncated.
    """

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

        # Prefer the longest nearby text field. This usually avoids picking up
        # tiny metadata fields when the real message body is also present.
        best_match = max(
            text_matches,
            key=lambda candidate: len(candidate.group(1)),
        )

        text = clean_text(decode_jsonish_string(best_match.group(1)))

        if not text or text.lower() == role or is_noise(text):
            continue

        dedupe_key = (role, text)

        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)

        turns.append(
            Turn(
                role=role,
                text=text,
                index=len(turns) + 1,
                source=f"raw_text[{start}:{end}]",
            )
        )

        log(
            f"Extracted raw fallback turn {len(turns)}: role={role}",
            verbosity,
            required_level=2,
        )

    return turns


def extract_opencode_turns(data: dict[str, Any], include_tools: bool, verbosity: int) -> list[Turn] | None:
    """
    Extract turns from opencode's native export format.

    The opencode export has the structure:
        { "info": {...}, "messages": [ { "info": {"role": ...}, "parts": [...] }, ... ] }

    Each part has a "type":
        - "text": actual conversation content (the "text" field)
        - "tool": tool call/result (has "tool", "state.input", "state.output")
        - "step-start", "step-finish": bookkeeping (skip)

    Args:
        data:
            Parsed JSON export.

        include_tools:
            Whether to include tool messages.

        verbosity:
            Current verbosity level.

    Returns:
        List of turns if this looks like an opencode export, or None if the
        format is not recognized (so the caller can fall back to generic parsing).
    """

    if not isinstance(data, dict):
        return None

    messages = data.get("messages")
    if not isinstance(messages, list):
        return None

    # Verify this looks like opencode format: first message should have info.role and parts.
    if messages and not (
        isinstance(messages[0], dict)
        and isinstance(messages[0].get("info"), dict)
        and isinstance(messages[0].get("parts"), list)
    ):
        return None

    log("Detected opencode native export format.", verbosity)
    turns: list[Turn] = []

    for msg_index, msg in enumerate(messages):
        info = msg.get("info", {})
        role = normalize_role(info.get("role"))
        parts = msg.get("parts", [])

        if role is None:
            continue

        if role == "tool" and not include_tools:
            continue

        # Extract text from parts.
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

                # Format tool calls concisely.
                input_summary = ""
                if isinstance(tool_input, dict):
                    # Show just the key arguments, not giant file contents.
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

            # Skip step-start, step-finish, and other metadata part types.

        if not text_chunks:
            continue

        combined_text = clean_text("\n\n".join(text_chunks))

        if not combined_text or is_noise(combined_text):
            continue

        turns.append(
            Turn(
                role=role,
                text=combined_text,
                index=len(turns) + 1,
                source=f"$.messages[{msg_index}]",
            )
        )

        log(
            f"Extracted turn {len(turns)}: role={role}, source=$.messages[{msg_index}]",
            verbosity,
            required_level=2,
        )

    return turns


def consolidate_turns(turns: list[Turn]) -> list[Turn]:
    """
    Merge consecutive turns with the same role into a single turn.

    Args:
        turns:
            Extracted turns in order.

    Returns:
        Consolidated turns where consecutive same-role entries are merged.
    """

    if not turns:
        return turns

    consolidated: list[Turn] = []

    for turn in turns:
        if consolidated and consolidated[-1].role == turn.role:
            # Merge into the previous turn.
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


def find_turns(data: Any, include_tools: bool, verbosity: int) -> list[Turn]:
    """
    Extract turns from exported session data.

    Tries the opencode-specific parser first, then falls back to a generic
    recursive walker for unknown formats.

    Args:
        data:
            Parsed JSON export, or raw text if JSON parsing failed.

        include_tools:
            Whether to include tool and function messages.

        verbosity:
            Current verbosity level.

    Returns:
        A list of extracted turns in discovery order.
    """

    if isinstance(data, str):
        turns = extract_turns_from_raw_text(data, verbosity=verbosity)
        return consolidate_turns(turns)

    # Try opencode-specific format first.
    if isinstance(data, dict):
        opencode_turns = extract_opencode_turns(data, include_tools=include_tools, verbosity=verbosity)
        if opencode_turns is not None:
            return consolidate_turns(opencode_turns)

    # Fallback: generic recursive walker.
    turns: list[Turn] = []
    seen: set[tuple[str, str]] = set()

    def walk(value: Any, path: str) -> None:
        """
        Recursive helper for discovering message-like dictionaries.

        Args:
            value:
                The current JSON value.

            path:
                A dot-delimited path used only for diagnostics.
        """

        if isinstance(value, dict):
            role = (
                normalize_role(value.get("role"))
                or normalize_role(value.get("author"))
                or normalize_role(value.get("speaker"))
            )

            if role is not None:
                if role == "tool" and not include_tools:
                    return  # Skip tool messages and their children.

                text = clean_text(extract_text(value))

                if text and text.lower() != role and not is_noise(text):
                    dedupe_key = (role, text)

                    if dedupe_key not in seen:
                        seen.add(dedupe_key)
                        turns.append(
                            Turn(
                                role=role,
                                text=text,
                                index=len(turns) + 1,
                                source=path,
                            )
                        )
                        log(
                            f"Extracted turn {len(turns)}: role={role}, source={path}",
                            verbosity,
                            required_level=2,
                        )

                # Don't descend into children of a role-bearing dict;
                # extract_text already pulled all useful text recursively.
                return

            for key, nested_value in value.items():
                walk(nested_value, f"{path}.{key}")
                pass

            return

        if isinstance(value, list):
            for item_index, item in enumerate(value):
                walk(item, f"{path}[{item_index}]")
                pass

            return

        pass

    walk(data, "$")
    return consolidate_turns(turns)


def filter_conversation_turns(turns: Iterable[Turn]) -> list[Turn]:
    """
    Keep the turns that are most useful for restarting work.

    Args:
        turns:
            Extracted turns.

    Returns:
        Filtered turns containing user and assistant roles only.
    """

    filtered: list[Turn] = []

    for turn in turns:
        if turn.role in {"user", "assistant"}:
            filtered.append(turn)
        pass

    return filtered


def count_interactions(turns: list[Turn]) -> int:
    """
    Count the number of back-and-forth interactions.

    An interaction is defined as a consecutive user turn followed by one or more
    assistant turns. A lone user or assistant turn still counts as one interaction.

    Args:
        turns:
            Conversation turns (typically user and assistant only).

    Returns:
        Number of interactions.
    """

    if not turns:
        return 0

    interactions = 0
    prev_role: str | None = None

    for turn in turns:
        if turn.role == "user" and prev_role != "user":
            interactions += 1
        elif prev_role is None:
            # First turn is not a user turn (e.g., starts with assistant).
            interactions += 1
        prev_role = turn.role

    return interactions


def rendered_lines_for_turn(turn: Turn) -> int:
    """
    Calculate the exact number of lines a turn will occupy in rendered Markdown.

    This mirrors the output format of render_transcript:
        ### N. Role        (1 line)
        <blank>            (1 line)
        <text content>     (N lines)
        <blank>            (1 line)

    Args:
        turn:
            A conversation turn.

    Returns:
        Number of rendered lines.
    """

    text_lines = turn.text.count("\n") + 1  # text itself
    return 1 + 1 + text_lines + 1  # header + blank + text + trailing blank


def count_transcript_lines(turns: list[Turn]) -> int:
    """
    Calculate the number of lines the transcript Markdown will contain.

    This counts lines as they would appear in the rendered output file,
    including Markdown headers and spacing.

    Args:
        turns:
            Conversation turns.

    Returns:
        Line count matching the rendered output.
    """

    if not turns:
        return 0

    # Document header: title + blank + generated line + blank + section header + blank = 6 lines
    header_lines = 6
    return header_lines + sum(rendered_lines_for_turn(t) for t in turns)


def truncate_turns_by_interactions(turns: list[Turn], max_interactions: int) -> list[Turn]:
    """
    Keep only the most recent N interactions from the tail.

    Args:
        turns:
            Conversation turns.

        max_interactions:
            Maximum number of interactions to keep.

    Returns:
        Truncated turn list containing the most recent interactions.
    """

    if max_interactions <= 0:
        return turns

    # Walk backwards to find interaction boundaries.
    # An interaction boundary is where a user turn starts after a non-user turn.
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

    # Keep from the Nth-from-last boundary onward.
    cut_index = boundaries[-max_interactions]
    return turns[cut_index:]


def truncate_turns_by_lines(turns: list[Turn], max_lines: int) -> list[Turn]:
    """
    Keep only enough of the most recent turns to stay within a line budget.

    The line budget refers to the rendered output file line count (matching
    what render_transcript produces), so --max-lines correlates directly with
    the output file size.

    Args:
        turns:
            Conversation turns.

        max_lines:
            Maximum number of lines in the rendered transcript output.

    Returns:
        Truncated turn list from the tail that fits within the line budget.
    """

    if max_lines <= 0:
        return turns

    # Reserve lines for the document header.
    header_lines = 6
    budget = max_lines - header_lines

    if budget <= 0:
        return turns[-1:]  # At minimum keep the last turn.

    # Walk backwards accumulating rendered lines until we exceed the budget.
    accumulated_lines = 0
    cut_index = len(turns)

    for i in range(len(turns) - 1, -1, -1):
        turn_lines = rendered_lines_for_turn(turns[i])
        if accumulated_lines + turn_lines > budget and cut_index < len(turns):
            break
        accumulated_lines += turn_lines
        cut_index = i

    return turns[cut_index:]


def apply_truncation(
    turns: list[Turn],
    max_lines: int | None,
    max_interactions: int | None,
    verbosity: int,
) -> list[Turn]:
    """
    Apply both line and interaction limits, taking the more restrictive result.

    Args:
        turns:
            Conversation turns.

        max_lines:
            Maximum transcript lines, or None for no limit.

        max_interactions:
            Maximum interactions, or None for no limit.

        verbosity:
            Current verbosity level.

    Returns:
        Truncated turns (from the tail / most recent).
    """

    result = turns

    if max_interactions is not None:
        by_interactions = truncate_turns_by_interactions(turns, max_interactions)
    else:
        by_interactions = turns

    if max_lines is not None:
        by_lines = truncate_turns_by_lines(turns, max_lines)
    else:
        by_lines = turns

    # Take the more restrictive (shorter) result.
    if len(by_interactions) < len(by_lines):
        result = by_interactions
        if len(result) < len(turns):
            log(
                f"Truncated to {len(result)} turns by --max-interactions limit.",
                verbosity,
            )
    else:
        result = by_lines
        if len(result) < len(turns):
            log(
                f"Truncated to {len(result)} turns by --max-lines limit.",
                verbosity,
            )

    return result


def prompt_for_truncation(
    turns: list[Turn],
    total_lines: int,
    total_interactions: int,
) -> tuple[int | None, int | None]:
    """
    Interactively ask the user whether to truncate a long session.

    Args:
        turns:
            The full turn list.

        total_lines:
            Estimated line count.

        total_interactions:
            Total interaction count.

    Returns:
        A tuple of (max_lines, max_interactions) chosen by the user.
        Both are None if the user wants no truncation.
    """

    print()
    print(color_yellow("This session is large:"))
    print(f"  Transcript lines:  {color_bold(str(total_lines))}")
    print(f"  Interactions:      {color_bold(str(total_interactions))}")
    print(f"  Total turns:       {color_bold(str(len(turns)))}")
    print()
    print("Truncation keeps only the most recent (tail) interactions.")
    print()

    # Check if stdin is interactive.
    if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        print(color_dim("Non-interactive mode: writing full output (use --max-lines or --max-interactions to limit)."))
        return None, None

    while True:
        answer = input(
            "Truncate output? [N]o / [l]ines / [i]nteractions / [b]oth: "
        ).strip().lower()

        if answer in {"", "n", "no"}:
            return None, None

        if answer in {"l", "lines"}:
            raw = input(f"  Max lines [{LONG_SESSION_LINE_THRESHOLD}]: ").strip()
            max_lines = int(raw) if raw.isdigit() else LONG_SESSION_LINE_THRESHOLD
            return max_lines, None

        if answer in {"i", "interactions"}:
            raw = input(f"  Max interactions [{LONG_SESSION_INTERACTION_THRESHOLD}]: ").strip()
            max_inter = int(raw) if raw.isdigit() else LONG_SESSION_INTERACTION_THRESHOLD
            return None, max_inter

        if answer in {"b", "both"}:
            raw_l = input(f"  Max lines [{LONG_SESSION_LINE_THRESHOLD}]: ").strip()
            raw_i = input(f"  Max interactions [{LONG_SESSION_INTERACTION_THRESHOLD}]: ").strip()
            max_lines = int(raw_l) if raw_l.isdigit() else LONG_SESSION_LINE_THRESHOLD
            max_inter = int(raw_i) if raw_i.isdigit() else LONG_SESSION_INTERACTION_THRESHOLD
            return max_lines, max_inter

        print("Please enter N, l, i, or b.")


def safe_filename(value: str) -> str:
    """
    Convert a string into a filesystem-safe filename fragment.

    Args:
        value:
            Source string.

    Returns:
        Safe filename fragment.
    """

    value = value.strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = value.strip("-._")

    if not value:
        return "session"

    return value[:80]


def markdown_text(text: str) -> str:
    """
    Prepare text for Markdown output.

    Args:
        text:
            Source text.

    Returns:
        Markdown text.

    Notes:
        This intentionally preserves code blocks and list formatting.
    """

    return text.strip()


def render_transcript(turns: list[Turn], title: str) -> str:
    """
    Render extracted turns as a readable Markdown transcript.

    Args:
        turns:
            Conversation turns to render.

        title:
            Document title.

    Returns:
        Markdown content.
    """

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
            "user": "User",
            "assistant": "Assistant",
            "system": "System",
            "tool": "Tool",
        }.get(turn.role, turn.role.title())

        lines.extend(
            [
                f"### {turn.index}. {role_label}",
                "",
                markdown_text(turn.text),
                "",
            ]
        )
        pass

    return "\n".join(lines).rstrip() + "\n"


def render_restart_context(
    turns: list[Turn],
    source_name: str,
    session: SessionInfo,
) -> str:
    """
    Render a restart document for a fresh opencode session.

    Args:
        turns:
            Conversation turns to include.

        source_name:
            Name of the temporary source export file.

        session:
            Selected session metadata.

    Returns:
        Markdown content designed to be read by an AI coding agent.
    """

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


def render_compact_prompt(
    turns: list[Turn],
    source_name: str,
    session: SessionInfo,
) -> str:
    """
    Render a prompt for asking another model to produce a compact summary.

    Args:
        turns:
            Conversation turns to summarize.

        source_name:
            Name of the temporary source export file.

        session:
            Selected session metadata.

    Returns:
        A Markdown prompt.
    """

    transcript = render_transcript(turns, "Transcript to compact")

    return f"""# Prompt to generate opencode restart summary

You are summarizing a recovered opencode session.

Source export: `{source_name}`
Original session ID: `{session.session_id}`
Original session title: `{session.title}`
Original session updated: `{session.updated}`

Create a concise but complete Markdown restart summary for a fresh opencode session.

The summary should include:
1. Project goal.
2. Current status.
3. Important decisions.
4. Files changed or discussed.
5. Commands run and their outcomes.
6. Errors and unresolved issues.
7. Known constraints and user preferences.
8. Next recommended steps.
9. Any cautionary notes to avoid redoing or damaging work.

Keep enough detail that a coding agent can continue safely without the original full transcript.

Here is the recovered transcript:

{transcript}
"""


def write_text(path: Path, content: str) -> None:
    """
    Write text content to a UTF-8 file.

    Args:
        path:
            Destination path.

        content:
            Text to write.

    Raises:
        RecoveryError:
            If writing fails.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as error:
        raise RecoveryError(f"Could not write file: {path}\n{error}") from error

    pass


def recover_from_export(
    export_path: Path,
    output_dir: Path,
    session: SessionInfo,
    include_tools: bool,
    all_roles: bool,
    verbosity: int,
    max_lines: int | None = None,
    max_interactions: int | None = None,
) -> list[Path]:
    """
    Generate recovery Markdown files from an opencode export JSON file.

    Args:
        export_path:
            Path to exported session JSON.

        output_dir:
            Directory where output files will be written.

        session:
            Selected session metadata.

        include_tools:
            Whether to include tool and function messages during extraction.

        all_roles:
            Whether to write all extracted roles instead of only user and assistant.

        verbosity:
            Current verbosity level.

        max_lines:
            Maximum transcript lines. None means no limit.

        max_interactions:
            Maximum interactions. None means no limit.

    Returns:
        Paths to generated files.

    Raises:
        RecoveryError:
            If no useful turns are found or output cannot be written.
    """

    log("Reading exported session JSON...", verbosity)
    data = load_export_file(export_path, verbosity=verbosity)

    log("Extracting user and assistant interactions...", verbosity)
    extracted_turns = find_turns(
        data=data,
        include_tools=include_tools,
        verbosity=verbosity,
    )

    if all_roles:
        selected_turns = extracted_turns
    else:
        selected_turns = filter_conversation_turns(extracted_turns)

    if not selected_turns:
        raise RecoveryError(
            "No user or assistant turns were found. "
            "Try rerunning with --all-roles or --include-tools."
        )

    # Compute stats and check thresholds.
    total_lines = count_transcript_lines(selected_turns)
    total_interactions = count_interactions(selected_turns)

    exceeds_threshold = (
        total_lines > LONG_SESSION_LINE_THRESHOLD
        or total_interactions > LONG_SESSION_INTERACTION_THRESHOLD
    )

    # If thresholds exceeded and no explicit limits given, prompt the user.
    if exceeds_threshold and max_lines is None and max_interactions is None:
        prompted_max_lines, prompted_max_interactions = prompt_for_truncation(
            selected_turns, total_lines, total_interactions
        )
        if prompted_max_lines is not None:
            max_lines = prompted_max_lines
        if prompted_max_interactions is not None:
            max_interactions = prompted_max_interactions

    # Apply truncation if limits are set.
    if max_lines is not None or max_interactions is not None:
        original_count = len(selected_turns)
        selected_turns = apply_truncation(
            selected_turns,
            max_lines=max_lines,
            max_interactions=max_interactions,
            verbosity=verbosity,
        )
        if len(selected_turns) < original_count:
            skipped = original_count - len(selected_turns)
            print(color_yellow(
                f"Truncated: keeping {len(selected_turns)} most recent turns "
                f"(skipped {skipped} older turns)."
            ))

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_session_id = safe_filename(session.session_id)
    base_name = f"opencode-recovery-{safe_session_id}-{timestamp}"

    transcript_path = output_dir / f"{base_name}.transcript.md"
    restart_path = output_dir / f"{base_name}.restart.md"
    compact_prompt_path = output_dir / f"{base_name}.compact-prompt.md"

    log(f"Writing transcript to: {transcript_path}", verbosity)
    write_text(
        transcript_path,
        render_transcript(selected_turns, "Recovered opencode transcript"),
    )

    log(f"Writing restart context to: {restart_path}", verbosity)
    write_text(
        restart_path,
        render_restart_context(
            turns=selected_turns,
            source_name=export_path.name,
            session=session,
        ),
    )

    log(f"Writing compact prompt to: {compact_prompt_path}", verbosity)
    write_text(
        compact_prompt_path,
        render_compact_prompt(
            turns=selected_turns,
            source_name=export_path.name,
            session=session,
        ),
    )

    print()
    print(f"Extracted turns: {color_bold(str(len(selected_turns)))}")
    print()
    display_turn_preview(selected_turns)

    return [transcript_path, restart_path, compact_prompt_path]


def install_signal_handlers(temp_dir_holder: dict[str, Path | None], verbosity_holder: dict[str, int]) -> None:
    """
    Install signal handlers that provide clean CTRL-C behavior.

    Args:
        temp_dir_holder:
            Mutable holder containing the temporary directory path.

        verbosity_holder:
            Mutable holder containing current verbosity.

    Notes:
        This function is intentionally small. Actual cleanup is handled by
        TemporaryDirectory when possible. The handler prints feedback and exits.
    """

    def handle_signal(signum: int, frame: Any) -> None:
        """
        Handle termination signals.

        Args:
            signum:
                Signal number.

            frame:
                Current stack frame.
        """

        temp_dir = temp_dir_holder.get("path")
        verbosity = verbosity_holder.get("verbosity", 0)

        print()
        eprint(color_yellow("Interrupted. Cleaning up temporary files..."))

        if temp_dir is not None:
            log(f"Temporary directory scheduled for cleanup: {temp_dir}", verbosity)

        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    pass


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments.

    Returns:
        Parsed arguments.
    """

    parser = argparse.ArgumentParser(
        description="Interactively export and recover an opencode session."
    )

    parser.add_argument(
        "--session",
        help="Known opencode session ID. Skips interactive selection.",
    )

    parser.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        help=(
            "Directory where the opencode session was originally run. "
            "opencode commands will be executed with this as the working directory. "
            "Defaults to the current directory."
        ),
    )

    parser.add_argument(
        "--out",
        type=Path,
        default=Path("opencode-recovery"),
        help="Output directory. Defaults to ./opencode-recovery.",
    )

    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary exported JSON file for debugging.",
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove leftover temporary export files from previous runs.",
    )

    parser.add_argument(
        "--clean-previous",
        action="store_true",
        help="Remove previous persisted recovery files for the selected session before generating new ones.",
    )

    parser.add_argument(
        "--include-tools",
        action="store_true",
        help="Include tool and function messages during extraction.",
    )

    parser.add_argument(
        "--all-roles",
        action="store_true",
        help="Write all extracted roles instead of only user and assistant turns.",
    )

    parser.add_argument(
        "--max-lines",
        type=int,
        default=None,
        help=(
            "Maximum number of transcript lines to include. "
            "When exceeded, only the most recent turns are kept. "
            "No limit by default."
        ),
    )

    parser.add_argument(
        "--max-interactions",
        type=int,
        default=None,
        help=(
            "Maximum number of back-and-forth interactions (user+assistant pairs) to include. "
            "When exceeded, only the most recent interactions are kept. "
            "No limit by default."
        ),
    )

    parser.add_argument(
        "--show-models",
        action="store_true",
        help="Show available models from opencode config and exit.",
    )

    parser.add_argument(
        "--use-model",
        type=str,
        default=None,
        help=(
            "Use the specified model to generate a compacted restart summary. "
            "Format: provider/model_id (e.g., uri/its_direct/pt1-qwen3-32b-us). "
            "Only OpenAI-compatible providers are supported. "
            "Use --show-models to see available options."
        ),
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase verbosity. Use -v or -vv.",
    )

    return parser.parse_args()


def find_session_by_id(sessions: list[SessionInfo], session_id: str) -> SessionInfo:
    """
    Find a session by ID, returning a placeholder if it is not listed.

    Args:
        sessions:
            Known sessions.

        session_id:
            Requested session ID.

    Returns:
        Matching session info or a minimal placeholder.

    Notes:
        A placeholder allows recovery to proceed if `opencode export SESSION_ID`
        works even when the session list output shape was unusual.
    """

    for session in sessions:
        if session.session_id == session_id:
            return session
        pass

    return SessionInfo(
        session_id=session_id,
        title="(provided session ID)",
        created="unknown",
        updated="unknown",
        raw={},
    )


_TEMP_DIR_PATTERN: re.Pattern[str] = re.compile(
    r"^opencode-recovery-[a-z0-9_]{6,12}$"
)
"""
Pattern matching tempfile-generated directory names.

tempfile.mkdtemp(prefix="opencode-recovery-") produces names like
"opencode-recovery-abc12xyz" with a random 8-char suffix from [a-z0-9_].
We match 6-12 chars to allow for platform variation.
"""


def clean_temp_files(verbosity: int) -> None:
    """
    Remove leftover opencode-recovery temporary directories from /tmp.

    Only removes directories that match the pattern generated by Python's
    tempfile.mkdtemp, to avoid accidentally deleting user-created directories.

    Args:
        verbosity:
            Current verbosity level.
    """

    temp_base = Path(tempfile.gettempdir())
    removed = 0

    for entry in temp_base.iterdir():
        if entry.is_dir() and _TEMP_DIR_PATTERN.match(entry.name):
            log(f"Removing temp directory: {entry}", verbosity)
            try:
                shutil.rmtree(entry)
                removed += 1
            except OSError as error:
                eprint(color_yellow(f"Warning: could not remove {entry}: {error}"))
        pass

    if removed:
        print(color_green(f"Removed {removed} leftover temporary director{'y' if removed == 1 else 'ies'}."))
    else:
        print(color_dim("No leftover temporary directories found."))

    pass


def clean_previous_recovery_files(
    output_dir: Path,
    session_id: str,
    verbosity: int,
) -> None:
    """
    Remove previous persisted recovery files for a given session.

    Args:
        output_dir:
            Output directory where recovery files are stored.

        session_id:
            The session ID whose previous recovery files should be removed.

        verbosity:
            Current verbosity level.
    """

    if not output_dir.is_dir():
        log(f"Output directory does not exist: {output_dir}", verbosity)
        return

    safe_id = safe_filename(session_id)
    prefix = f"opencode-recovery-{safe_id}-"
    removed = 0

    for entry in output_dir.iterdir():
        if entry.is_file() and entry.name.startswith(prefix):
            log(f"Removing previous recovery file: {entry}", verbosity)
            entry.unlink()
            removed += 1
        pass

    if removed:
        print(color_green(f"Removed {removed} previous recovery file{'s' if removed != 1 else ''} for session {session_id}."))
    else:
        print(color_dim(f"No previous recovery files found for session {session_id}."))

    pass


def run_compaction(
    compact_prompt_path: Path,
    output_dir: Path,
    session: SessionInfo,
    model_spec: str,
    verbosity: int,
) -> Path | None:
    """
    Run LLM-based compaction on the recovery transcript.

    Loads the compact prompt, resolves the model, estimates cost, asks for
    confirmation, calls the API, and writes the compacted result.

    Args:
        compact_prompt_path:
            Path to the .compact-prompt.md file.

        output_dir:
            Directory for output files.

        session:
            Session metadata.

        model_spec:
            User-provided model specification for --use-model.

        verbosity:
            Current verbosity level.

    Returns:
        Path to the compacted output file, or None if the user cancelled.
    """

    print()
    print(color_bold("LLM Compaction"))
    print()

    # Load config and resolve model.
    config = load_opencode_config(verbosity=verbosity)
    models = extract_models_from_config(config)
    model = resolve_model(models, model_spec)

    print(f"  Model:    {color_cyan(f'{model.provider_id}/{model.model_id}')} ({model.name})")
    print(f"  Endpoint: {color_dim(model.base_url)}")

    # Load the compact prompt content.
    try:
        prompt_content = compact_prompt_path.read_text(encoding="utf-8")
    except OSError as error:
        raise RecoveryError(f"Could not read compact prompt: {compact_prompt_path}\n{error}") from error

    # Estimate tokens and cost.
    input_tokens = estimate_tokens(prompt_content)
    # Estimate output at ~20% of input (compaction should be much shorter).
    output_tokens_est = max(500, input_tokens // 5)

    print(f"  Input:    ~{input_tokens:,} tokens (estimated)")
    print(f"  Output:   ~{output_tokens_est:,} tokens (estimated)")

    cost = estimate_cost(input_tokens, output_tokens_est, model)
    if cost is not None:
        print(f"  Est cost: {color_yellow(f'${cost:.4f}')}")
    else:
        print(f"  Est cost: {color_dim('unknown (no cost info for this model)')}")

    print()

    # Ask for confirmation if interactive.
    if hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        answer = input("Proceed with compaction? [Y/n]: ").strip().lower()
        if answer in {"n", "no"}:
            print(color_dim("Compaction cancelled."))
            return None
    else:
        log("Non-interactive mode: proceeding with compaction.", verbosity)

    print()
    print(color_dim("Calling API (this may take a minute)..."))

    response_text = call_compaction_api(
        model=model,
        prompt=prompt_content,
        verbosity=verbosity,
    )

    # Write the compacted output.
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_session_id = safe_filename(session.session_id)
    compacted_path = output_dir / f"opencode-recovery-{safe_session_id}-{timestamp}.compacted.md"

    write_text(compacted_path, response_text)

    print()
    print(f"  Compacted output: {color_green(str(compacted_path))}")
    print(f"  Output lines:     {response_text.count(chr(10)) + 1}")

    return compacted_path


def main() -> None:
    """
    Run the interactive opencode recovery workflow.

    Workflow:
    1. Check that opencode is installed.
    2. List sessions.
    3. Let the user select a session, unless --session was provided.
    4. Export selected session to a temporary file.
    5. Generate recovery Markdown files.
    6. Clean up temporary files unless --keep-temp was used.
    """

    args = parse_args()
    verbosity = args.verbose

    # Handle --show-models early (no session needed).
    if args.show_models:
        try:
            config = load_opencode_config(verbosity=verbosity)
            models = extract_models_from_config(config)
            display_models(models)
        except RecoveryError as error:
            die(str(error), exit_code=1)
        return

    temp_dir_holder: dict[str, Path | None] = {"path": None}
    verbosity_holder: dict[str, int] = {"verbosity": verbosity}
    install_signal_handlers(temp_dir_holder, verbosity_holder)

    session_dir: Path | None = args.session_dir
    if session_dir is not None:
        session_dir = session_dir.resolve()
        if not session_dir.is_dir():
            die(f"--session-dir is not a valid directory: {session_dir}")

    try:
        print(color_bold("opencode session recovery"))
        if session_dir is not None:
            print(f"Session directory: {color_cyan(str(session_dir))}")
        print()

        require_opencode()

        sessions = list_sessions(verbosity=verbosity, cwd=session_dir)

        if args.session:
            if args.session.startswith("-"):
                raise RecoveryError(
                    f"Invalid session ID: {args.session!r} (must not start with '-')."
                )
            session = find_session_by_id(sessions, args.session)
            print(f"Selected session from --session: {color_dim(session.session_id)}")
        else:
            session = prompt_for_session(sessions)

        print()
        print(f"Selected session: {color_bold(session.title)}")
        print(f"Session ID: {color_dim(session.session_id)}")
        print()

        output_dir = args.out
        generated_paths: list[Path] = []

        if args.clean:
            clean_temp_files(verbosity=verbosity)
            print()

        if args.clean_previous:
            clean_previous_recovery_files(
                output_dir=output_dir,
                session_id=session.session_id,
                verbosity=verbosity,
            )
            print()

        if args.keep_temp:
            temp_dir = Path(tempfile.mkdtemp(prefix="opencode-recovery-"))
            temp_dir_holder["path"] = temp_dir

            try:
                export_path = write_export_to_temp(
                    session_id=session.session_id,
                    temp_dir=temp_dir,
                    verbosity=verbosity,
                    cwd=session_dir,
                )

                generated_paths = recover_from_export(
                    export_path=export_path,
                    output_dir=output_dir,
                    session=session,
                    include_tools=args.include_tools,
                    all_roles=args.all_roles,
                    verbosity=verbosity,
                    max_lines=args.max_lines,
                    max_interactions=args.max_interactions,
                )

                print()
                print(f"Temporary export preserved at: {color_cyan(str(export_path))}")

            finally:
                # --keep-temp intentionally skips cleanup.
                pass

        else:
            with tempfile.TemporaryDirectory(prefix="opencode-recovery-") as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                temp_dir_holder["path"] = temp_dir

                export_path = write_export_to_temp(
                    session_id=session.session_id,
                    temp_dir=temp_dir,
                    verbosity=verbosity,
                    cwd=session_dir,
                )

                generated_paths = recover_from_export(
                    export_path=export_path,
                    output_dir=output_dir,
                    session=session,
                    include_tools=args.include_tools,
                    all_roles=args.all_roles,
                    verbosity=verbosity,
                    max_lines=args.max_lines,
                    max_interactions=args.max_interactions,
                )

                log("Temporary export cleaned up.", verbosity)

        if generated_paths:
            print()
            print(color_green("Recovery files generated:"))
            for path in generated_paths:
                print(f"  {color_cyan(str(path))}")
                pass

            # If --use-model is specified, run compaction via LLM.
            if args.use_model:
                compacted_path = run_compaction(
                    compact_prompt_path=generated_paths[2],  # .compact-prompt.md
                    output_dir=output_dir,
                    session=session,
                    model_spec=args.use_model,
                    verbosity=verbosity,
                )
                if compacted_path:
                    generated_paths.append(compacted_path)
                    print()
                    print(color_bold("Suggested next step:"))
                    print(f"  Start a fresh opencode session and ask it to read: {color_cyan(str(compacted_path))}")
                    print()
                else:
                    print()
                    print(color_bold("Suggested next step:"))
                    print(f"  Start a fresh opencode session and ask it to read: {color_cyan(str(generated_paths[1]))}")
                    print()
            else:
                print()
                print(color_bold("Suggested next step:"))
                print(f"  Start a fresh opencode session and ask it to read: {color_cyan(str(generated_paths[1]))}")
                print()

    except KeyboardInterrupt:
        eprint(color_yellow("Recovery cancelled."))
        raise SystemExit(130)
    except RecoveryError as error:
        die(str(error), exit_code=1)

    pass


if __name__ == "__main__":
    main()
