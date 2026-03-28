"""
tools.py — Tool implementations for the Claude tool_use loop.

Security model:
- exec_command: Runs arbitrary shell commands. ALL executions are logged for audit.
  This is intentional — the owner trusts Claude within this controlled environment.
- read_file / write_file / edit_file: Restricted to configured path prefixes.
  Path traversal outside these roots is blocked.
- web_search: Uses DuckDuckGo search API. Free, no API key required.
- web_fetch: Fetches URLs. No authentication tokens are sent.
- send_message: Sends Signal messages via local signal-cli. Bot number comes from config, not LLM.
  Supports standard Markdown formatting: **bold**, *italic*, _italic_, ~~strikethrough~~, ||spoiler||, `monospace`
- memory_search: Searches workspace files via grep. Read-only.

Secrets (API keys, etc.) are loaded from environment variables and NEVER included
in tool results returned to the LLM.
"""

import os
import re
import subprocess
import logging
import requests
import json
import base64
import uuid
import glob as globmod
import fnmatch

from config import CONFIG
from voice import generate_speech
import identity

log = logging.getLogger("cleo.tools")

# Allowed path prefixes for file operations
ALLOWED_PATH_PREFIXES = CONFIG.get("allowed_path_prefixes", (os.path.expanduser("~") + "/",))


# --- Tool definitions for the Anthropic API ---
# These are sent to the API so Claude knows what tools are available.

TOOL_DEFINITIONS = [
    {
        "name": "exec_command",
        "description": "Execute a shell command and return stdout+stderr. Use for system tasks, package management, git, etc. All executions are logged.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 120).", "default": 30},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file with line numbers. Allowed under configured path prefixes. Output includes line numbers for use with edit_file. For large files, use offset and limit to read specific line ranges.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "offset": {"type": "integer", "description": "Line number to start reading from (1-based). Default 1."},
                "limit": {"type": "integer", "description": "Maximum number of lines to return. Default 2000."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": "Make a targeted edit to a file by replacing an exact string match. Much more efficient than write_file for small changes — only specify the text to change, not the entire file. The old_string must match exactly one location in the file (including whitespace and indentation). Use read_file first to see the current content and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "old_string": {"type": "string", "description": "The exact text to find and replace. Must match exactly one location in the file."},
                "new_string": {"type": "string", "description": "The replacement text. Use empty string to delete the matched text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "grep_search",
        "description": "Search file contents using regex. Returns matching lines with file paths and line numbers. Much faster and cleaner than exec_command with grep. Searches recursively from the given directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for."},
                "path": {"type": "string", "description": "Directory or file to search in. Defaults to current working directory."},
                "include": {"type": "string", "description": "Glob pattern to filter files (e.g. '*.py', '*.js'). Optional."},
                "context": {"type": "integer", "description": "Number of lines of context to show before and after each match. Default 0."},
                "max_results": {"type": "integer", "description": "Maximum number of matches to return. Default 50."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find_files",
        "description": "Find files by glob pattern. Returns matching file paths sorted by modification time. Much faster and cleaner than exec_command with find/ls.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '**/*.py', 'src/**/*.ts', '*.json')."},
                "path": {"type": "string", "description": "Directory to search in. Defaults to current working directory."},
                "max_results": {"type": "integer", "description": "Maximum number of results. Default 100."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file (creates or overwrites). Prefer edit_file for modifying existing files. Allowed under configured path prefixes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "content": {"type": "string", "description": "Content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web using DuckDuckGo. Returns search results with title, URL, and snippet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "count": {"type": "integer", "description": "Number of results (default 5).", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch a URL and return its text content (HTML tags stripped).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "check_weather",
        "description": "Check current weather for a location. Returns conditions, temperature, feels-like, wind, humidity, and precipitation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Location: zip code, city name, or 'City,State'."},
            },
            "required": ["location"],
        },
    },
    {
        "name": "send_message",
        "description": "Send a Signal message to a phone number or UUID. Supports standard Markdown formatting: **bold**, *italic*, _italic_, ~~strikethrough~~, ||spoiler||, `monospace`. To quote-reply, provide quote_timestamp and quote_author.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number (e.g. +1...) or UUID of the recipient."},
                "message": {"type": "string", "description": "Message text to send. Use **bold**, *italic*, _italic_, ~~strikethrough~~, ||spoiler||, `monospace` for formatting."},
                "attachment": {"type": "string", "description": "File path to an image to attach (e.g. from generate_image)."},
                "quote_timestamp": {"type": "integer", "description": "Signal timestamp (ms) of the message to quote — the ts: value from read_feed."},
                "quote_author": {"type": "string", "description": "Phone number or UUID of the original message's author."},
            },
            "required": ["recipient", "message"],
        },
    },
    {
        "name": "memory_search",
        "description": "Search through workspace memory files. 'semantic' mode (default) finds conceptually related memories using AI embeddings. 'exact' mode does literal text grep. Semantic is better for questions; exact for finding specific strings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "mode": {"type": "string", "enum": ["semantic", "exact"], "description": "Search mode (default: semantic).", "default": "semantic"},
                "count": {"type": "integer", "description": "Max results (default 10).", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_sms",
        "description": "Check recent incoming SMS messages on the Twilio number. Use when you need to retrieve a verification code or check for incoming texts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of recent messages to fetch (default 5).", "default": 5},
            },
        },
    },
    {
        "name": "send_reaction",
        "description": "React to a Signal message with an emoji. Use the signal_ts from read_feed output to identify the target message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "emoji": {"type": "string", "description": "Single emoji to react with (e.g. 👍, ❤️, 😂)."},
                "target_author": {"type": "string", "description": "Phone number or UUID of the message author."},
                "target_timestamp": {"type": "integer", "description": "Signal timestamp (ms) of the message — the ts: value from read_feed."},
                "recipient": {"type": "string", "description": "Phone number (DM) or UUID of the recipient."},
            },
            "required": ["emoji", "target_author", "target_timestamp", "recipient"],
        },
    },
    {
        "name": "send_poll",
        "description": "Create a Signal poll with a question and options. Returns confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Phone number (DM) or group recipient."},
                "question": {"type": "string", "description": "The poll question."},
                "options": {"type": "array", "items": {"type": "string"}, "description": "List of poll options (minimum 2)."},
                "allow_multiple": {"type": "boolean", "description": "Whether voters can select multiple options (default: true).", "default": True},
            },
            "required": ["recipient", "question", "options"],
        },
    },
    {
        "name": "generate_image",
        "description": "Generate an image from a text prompt using Gemini. Returns a file path to the generated PNG. Use send_message with the attachment parameter to send it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description of the image to generate."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "describe_image",
        "description": "View an image file directly. Returns the image as a visual content block that you can see and analyze yourself. Give it a file path to an image (e.g. from generate_image, or a downloaded file).",
        "input_schema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "File path to the image to view."},
            },
            "required": ["image_path"],
        },
    },
    {
        "name": "schedule_reminder",
        "description": "Schedule a one-time reminder. When the time arrives, you'll be woken up with the prompt text as a feed event. Use this for reminders, follow-ups, or any delayed action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fire_at": {
                    "type": "string",
                    "description": "When to fire, ISO 8601 datetime (e.g. '2026-02-26T08:00:00'). Assumes America/New_York if no timezone specified.",
                },
                "prompt": {
                    "type": "string",
                    "description": "The text you'll see when woken up. Include context about who asked and what to do.",
                },
            },
            "required": ["fire_at", "prompt"],
        },
    },
    {
        "name": "delegate_task",
        "description": (
            "Delegate a complex task to a background subagent. The subagent runs "
            "autonomously with its own context and tools (exec_command, read_file, "
            "edit_file, write_file, grep_search, find_files, web_search, web_fetch, "
            "memory_search), then messages the "
            "user directly when done. Use this for tasks that require multiple tool "
            "calls (research, file operations, long commands) so you can continue "
            "responding to the user immediately. The subagent cannot see your "
            "conversation history — include all necessary context in the task description."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Clear, complete description of what to accomplish.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional additional context: file paths, URLs, specific instructions.",
                },
                "model": {
                    "type": "string",
                    "enum": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
                    "description": "Model for the subagent. Default is your own model (Sonnet 4.6). Use claude-opus-4-6 only for particularly complex tasks requiring deep reasoning — it costs significantly more. Use claude-haiku-4-5 for simple, fast, cheap tasks like quick lookups or file writes.",
                },
                "soul": {
                    "type": "string",
                    "enum": ["engineer", "researcher", "consolidator", "planner", "game_designer"],
                    "description": "Subagent specialization. 'engineer' (default) for coding/building, 'researcher' for web research/synthesis, 'consolidator' for memory/log processing, 'planner' for technical planning, 'game_designer' for game design and rules work.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "check_quota",
        "description": "Check current API rate limit utilization across 5-hour, 7-day, and Sonnet windows. Returns current usage percentages and reset times.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
]


# Tools excluded from subagent use (no recursion, no messaging, no admin)
_SUBAGENT_EXCLUDED_TOOLS = {"delegate_task", "send_message", "send_reaction", "send_poll", "vote_poll", "generate_image", "schedule_reminder", "check_quota"}

SUBAGENT_TOOL_DEFINITIONS = [
    t for t in TOOL_DEFINITIONS if t["name"] not in _SUBAGENT_EXCLUDED_TOOLS
]


def _validate_path(path: str) -> str | None:
    """
    Validate that a file path is under an allowed prefix.
    Returns an error message if invalid, None if OK.
    Resolves symlinks to prevent traversal via symlink.
    """
    resolved = os.path.realpath(path)
    if not any(resolved.startswith(prefix) for prefix in ALLOWED_PATH_PREFIXES):
        return f"Path not allowed: {path} (resolves to {resolved}). Must be under {ALLOWED_PATH_PREFIXES}"
    return None


def _parse_formatting(text: str) -> tuple[str, list[str]]:
    """
    Parse standard Markdown formatting and convert to Signal textStyles.

    Supports:
    - ||spoiler|| → SPOILER
    - **bold** → BOLD
    - *italic* → ITALIC
    - ~~strikethrough~~ → STRIKETHROUGH
    - ~strikethrough~ → STRIKETHROUGH
    - _italic_ → ITALIC
    - `monospace` → MONOSPACE

    Returns (plain_text, textStyles) where textStyles is a list of "start:length:STYLE" strings.
    
    This version processes all formatting markers in a single pass to avoid offset issues.
    """
    
    # Define patterns with their delimiters and styles
    # Order matters: longer markers first so ** is matched before *
    patterns = [
        (r'\|\|(.+?)\|\|', 2, 2, 'SPOILER'),      # ||text|| - 2 chars on each side
        (r'\*\*(.+?)\*\*', 2, 2, 'BOLD'),          # **text** - 2 chars on each side
        (r'~~(.+?)~~', 2, 2, 'STRIKETHROUGH'),     # ~~text~~ - 2 chars on each side
        (r'\*(.+?)\*', 1, 1, 'ITALIC'),            # *text* - 1 char on each side
        (r'~(.+?)~', 1, 1, 'STRIKETHROUGH'),       # ~text~ - 1 char on each side
        (r'_(.+?)_', 1, 1, 'ITALIC'),              # _text_ - 1 char on each side
        (r'`(.+?)`', 1, 1, 'MONOSPACE'),           # `text` - 1 char on each side
    ]
    
    # Find all formatting markers - use a loop to find overlapping matches
    all_matches = []
    
    for pattern_str, prefix_len, suffix_len, style in patterns:
        # Use a loop to find all matches, including overlapping ones
        pattern = re.compile(pattern_str)
        pos = 0
        while pos < len(text):
            match = pattern.search(text, pos)
            if not match:
                break
            # Record: (start_pos, end_pos, content, style, prefix_len, suffix_len)
            all_matches.append((
                match.start(),
                match.end(),
                match.group(1),
                style,
                prefix_len,
                suffix_len
            ))
            # Move forward by 1 to find overlapping matches
            pos = match.start() + 1
    
    # Sort by position, then by length (longer first to handle overlaps)
    all_matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    
    # Remove overlapping matches (keep the first one encountered, which is longer due to sort)
    filtered_matches = []
    covered_chars = set()  # Track individual character positions that are covered
    
    for match in all_matches:
        start, end = match[0], match[1]
        # Get the set of characters this match would cover
        match_chars = set(range(start, end))
        # Check if ANY character overlaps with already-covered characters
        if not match_chars & covered_chars:
            # No overlap, keep this match
            filtered_matches.append(match)
            covered_chars.update(match_chars)
    
    # Sort again by position for text building
    filtered_matches.sort(key=lambda x: x[0])
    
    # Build the plain text and compute offsets
    plain_text = ""
    text_styles = []
    last_pos = 0
    offset_adjustment = 0  # Track how many chars we've removed
    
    for orig_start, orig_end, content, style, prefix_len, suffix_len in filtered_matches:
        # Add unformatted text before this match
        plain_text += text[last_pos:orig_start]
        
        # Position in the new string (accounting for removed markers)
        new_start = orig_start - offset_adjustment
        
        # Add the content (without markers)
        plain_text += content
        
        # Record the style
        text_styles.append(f"{new_start}:{len(content)}:{style}")
        
        # Update offset adjustment (we removed prefix + suffix chars)
        offset_adjustment += prefix_len + suffix_len
        
        # Move past this match
        last_pos = orig_end
    
    # Add any remaining unformatted text
    plain_text += text[last_pos:]
    
    return plain_text, text_styles


def _parse_mentions(text: str) -> tuple[str, list[str]]:
    """Parse @Name mentions from text and resolve to signal-cli mention format.

    Scans plain text for @DisplayName patterns matching known contacts.
    Returns (text, mentions_list) where mentions_list contains strings
    like "start:length:identifier" with UTF-16 code unit offsets.
    Prefers phone numbers over UUIDs as signal-cli handles them better.
    """
    mentions = []

    # Build a lookup of known names -> identifiers from identity cache
    identity._ensure_cache()
    name_to_id: dict[str, str] = {}

    for key, contact in identity._contacts.items():
        if not key.startswith("uuid:"):
            continue
        uuid_str = key[5:]  # strip "uuid:" prefix
        display = identity.resolve_name(key)
        if display and display != key and display != uuid_str:
            # Prefer phone number if available, fall back to UUID
            phone = getattr(contact, 'number', None)
            name_to_id[display.lower()] = phone if phone else uuid_str

    # Also add nicknames
    for key, nickname in identity._nicknames.items():
        if nickname.lower() not in name_to_id:
            if key.startswith("+"):
                name_to_id[nickname.lower()] = key
            else:
                # UUID key - check if we have a phone number for it
                contact = identity._contacts.get(f"uuid:{key}")
                phone = getattr(contact, 'number', None) if contact else None
                name_to_id[nickname.lower()] = phone if phone else key

    if not name_to_id:
        return text, []

    # Sort names by length (longest first) to avoid partial matches
    sorted_names = sorted(name_to_id.keys(), key=len, reverse=True)

    # Escape names for regex and build alternation pattern
    escaped = [re.escape(name) for name in sorted_names]
    pattern = r'@(' + '|'.join(escaped) + r')'

    for match in re.finditer(pattern, text, re.IGNORECASE):
        matched_name = match.group(1).lower()
        if matched_name in name_to_id:
            identifier = name_to_id[matched_name]
            # Calculate UTF-16 offset
            prefix = text[:match.start()]
            utf16_start = len(prefix.encode('utf-16-le')) // 2
            mention_text = match.group(0)  # includes the @
            utf16_length = len(mention_text.encode('utf-16-le')) // 2
            mentions.append(f"{utf16_start}:{utf16_length}:{identifier}")

    return text, mentions


def exec_command(command: str, timeout: int = 30) -> str:
    """Execute a shell command. Logged for audit."""
    timeout = min(max(timeout, 1), 120)
    log.warning("EXEC: %s (timeout=%ds)", command, timeout)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        output = f"Command timed out after {timeout}s"
    except Exception as e:
        output = f"Error executing command: {e}"

    # Truncate to 10000 chars
    if len(output) > 10000:
        output = output[:10000] + "\n... [truncated]"
    return output


def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    """Read a file under allowed paths, with line numbers and line-based pagination."""
    err = _validate_path(path)
    if err:
        return err
    try:
        with open(path, "r") as f:
            all_lines = f.readlines()
        total_lines = len(all_lines)
        # Convert 1-based offset to 0-based index
        start = max(0, offset - 1)
        end = min(total_lines, start + limit)
        selected = all_lines[start:end]
        # Format with line numbers
        numbered = []
        for i, line in enumerate(selected, start=start + 1):
            numbered.append(f"{i:>6}\t{line.rstrip()}")
        content = "\n".join(numbered)
        header = f"[file: {path} | {total_lines} lines | showing lines {start + 1}–{end}]"
        remaining = total_lines - end
        if remaining > 0:
            header += f" [{remaining} lines remaining — call again with offset={end + 1}]"
        return f"{header}\n\n{content}"
    except Exception as e:
        return f"Error reading file: {e}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact string match in a file. old_string must be unique in the file."""
    err = _validate_path(path)
    if err:
        return err
    try:
        with open(path, "r") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading file: {e}"

    count = content.count(old_string)
    if count == 0:
        return "Error: old_string not found in file. Make sure it matches exactly, including whitespace and indentation."
    if count > 1:
        return f"Error: old_string matches {count} locations in the file. Provide a larger string with more surrounding context to make it unique."

    new_content = content.replace(old_string, new_string, 1)
    try:
        with open(path, "w") as f:
            f.write(new_content)
        # Report what changed
        old_lines = old_string.count('\n') + 1
        new_lines = new_string.count('\n') + 1
        log.info("EDIT: %s (replaced %d lines with %d lines)", path, old_lines, new_lines)
        return f"Edited {path}: replaced {old_lines} lines with {new_lines} lines."
    except Exception as e:
        return f"Error writing file: {e}"


def write_file(path: str, content: str) -> str:
    """Write a file under allowed paths."""
    err = _validate_path(path)
    if err:
        return err
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        log.info("WRITE: %s (%d bytes)", path, len(content))
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


def grep_search(pattern: str, path: str = ".", include: str | None = None,
                 context: int = 0, max_results: int = 50) -> str:
    """Search file contents using regex. Returns matches with file paths and line numbers."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"Error: path does not exist: {path}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    matches = []
    files_to_search = []

    if os.path.isfile(path):
        files_to_search = [path]
    else:
        for root, dirs, files in os.walk(path):
            # Skip hidden dirs and common noise
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', '__pycache__', '.git', 'venv', '.venv')]
            for fname in files:
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                files_to_search.append(os.path.join(root, fname))

    for fpath in files_to_search:
        if len(matches) >= max_results:
            break
        try:
            with open(fpath, 'r', errors='replace') as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines):
            if regex.search(line):
                if len(matches) >= max_results:
                    break
                # Gather context lines
                start = max(0, i - context)
                end = min(len(lines), i + context + 1)
                snippet_lines = []
                for j in range(start, end):
                    marker = ">" if j == i else " "
                    snippet_lines.append(f"{marker} {j + 1:>5}\t{lines[j].rstrip()}")
                matches.append(f"{fpath}:{i + 1}\n" + "\n".join(snippet_lines))

    if not matches:
        return f"No matches found for pattern: {pattern}"

    header = f"[{len(matches)} matches"
    if len(matches) >= max_results:
        header += f" (limit reached, increase max_results to see more)"
    header += "]"
    return header + "\n\n" + "\n\n".join(matches)


def find_files(pattern: str, path: str = ".", max_results: int = 100) -> str:
    """Find files matching a glob pattern, sorted by modification time (newest first)."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return f"Error: path does not exist: {path}"

    # Use recursive glob
    search_path = os.path.join(path, pattern)
    results = globmod.glob(search_path, recursive=True)

    # Filter out hidden directories and common noise
    filtered = []
    skip_dirs = {'.git', 'node_modules', '__pycache__', 'venv', '.venv'}
    for r in results:
        parts = r.split(os.sep)
        if any(p.startswith('.') or p in skip_dirs for p in parts if p != '.'):
            continue
        if os.path.isfile(r):
            filtered.append(r)

    # Sort by modification time, newest first
    filtered.sort(key=lambda f: os.path.getmtime(f), reverse=True)

    if not filtered:
        return f"No files found matching pattern: {pattern}"

    truncated = filtered[:max_results]
    header = f"[{len(filtered)} files found"
    if len(filtered) > max_results:
        header += f", showing first {max_results}"
    header += "]"
    return header + "\n" + "\n".join(truncated)


def web_search(query: str, count: int = 5) -> str:
    """
    Search the web via DuckDuckGo.
    Free, no API key required.
    """
    try:
        from ddgs import DDGS

        results = []
        with DDGS() as ddgs:
            search_results = ddgs.text(query, max_results=count)
            for r in search_results:
                title = r.get('title', 'No title')
                url = r.get('href', r.get('link', ''))
                snippet = r.get('body', r.get('snippet', ''))
                results.append(f"**{title}**\n{url}\n{snippet}")

        if not results:
            return f"No search results found for '{query}'"

        return "\n\n".join(results)
    except ImportError:
        return "ddgs package not installed. Run: pip install ddgs"
    except Exception as e:
        log.error("web_search error for query '%s': %s", query, e)
        return f"Search error: {e}"


def web_fetch(url: str) -> str:
    """Fetch a URL, strip HTML tags, return plain text."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "cleo/1.0"})
        resp.raise_for_status()
        # Strip HTML tags with regex (basic, not a full parser)
        text = re.sub(r"<[^>]+>", " ", resp.text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 50000:
            text = text[:50000] + "\n... [truncated]"
        return text
    except Exception as e:
        return f"Error fetching URL: {e}"


def check_weather(location: str) -> str:
    """Check weather for a location via NWS API (free, no key needed)."""
    ua = {"User-Agent": "cleo/1.0 (weather check)"}
    try:
        # Geocode location → lat/lon via Open-Meteo
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1}, headers=ua, timeout=10,
        )
        geo.raise_for_status()
        results = geo.json().get("results")
        if not results:
            return f"Could not find location: {location}"
        place = results[0]
        lat, lon = place["latitude"], place["longitude"]
        place_name = f"{place['name']}, {place.get('admin1', '')}"

        # NWS: resolve lat/lon → forecast grid
        points = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers=ua, timeout=10)
        points.raise_for_status()
        forecast_url = points.json()["properties"]["forecast"]

        # NWS: get forecast
        forecast = requests.get(forecast_url, headers=ua, timeout=10)
        forecast.raise_for_status()
        periods = forecast.json()["properties"]["periods"]

        lines = [f"Weather for {place_name}:"]
        for p in periods[:4]:
            lines.append(f"- {p['name']}: {p['temperature']}°{p['temperatureUnit']} — {p['shortForecast']} (wind: {p['windSpeed']} {p['windDirection']})")
        return "\n".join(lines)
    except Exception as e:
        return f"Error checking weather: {e}"


def send_typing(recipient: str, group_id: str | None = None, stop: bool = False) -> None:
    """Send a typing indicator via signal-cli JSON-RPC. Fire-and-forget."""
    rpc_url = CONFIG["signal_rpc_url"]
    bot_number = CONFIG["bot_number"]

    params: dict = {"account": bot_number}
    if stop:
        params["stop"] = True

    if group_id:
        params["groupId"] = group_id
    else:
        if recipient.startswith("uuid:"):
            params["recipient"] = recipient[5:]
        else:
            params["recipient"] = recipient

    payload = {
        "jsonrpc": "2.0",
        "method": "sendTyping",
        "id": "cleo-typing",
        "params": params,
    }
    try:
        requests.post(rpc_url, json=payload, timeout=5)
    except Exception:
        pass  # typing indicators are best-effort


def send_message(recipient: str, message: str, group_id: str | None = None, attachment: str | None = None,
                 quote_timestamp: int | None = None, quote_author: str | None = None) -> str:
    """
    Send a Signal message via signal-cli JSON-RPC with formatting support.

    Supports standard Markdown formatting:
    - **bold** → bold text
    - *italic* or _italic_ → italic text
    - ~~strikethrough~~ or ~strikethrough~ → strikethrough text
    - ||spoiler|| → spoiler text (tap to reveal)
    - `monospace` → monospace text

    If group_id is provided, the reply is sent to the group (all members see it).
    Otherwise, the reply goes to the individual recipient as a DM.
    """
    rpc_url = CONFIG["signal_rpc_url"]
    bot_number = CONFIG["bot_number"]

    # Parse formatting and convert to plain text + textStyles
    plain_text, text_styles = _parse_formatting(message)

    # Parse @mentions from the formatted plain text
    _, mention_strings = _parse_mentions(plain_text)

    params: dict = {"account": bot_number, "message": plain_text}
    
    # Add textStyles if any formatting was found
    if text_styles:
        params["textStyle"] = text_styles
        log.info("Sending formatted message with %d styles", len(text_styles))

    # Mentions are only valid in group messages
    if mention_strings and not group_id:
        log.info("Skipping %d mention(s) — mentions only work in group messages", len(mention_strings))
        mention_strings = []

    # Add mentions if any @Name patterns were resolved
    if mention_strings:
        params["mention"] = mention_strings
        log.info("Sending message with %d mention(s)", len(mention_strings))

    if attachment:
        params["attachments"] = [attachment]

    if group_id:
        # Reply to the group — group_id is the base64 group ID from the envelope
        params["groupId"] = group_id
        log.info("Sending to group %s (%d chars)", group_id[:12], len(plain_text))
    else:
        # Reply to individual — strip "uuid:" prefix if needed
        if recipient.startswith("uuid:"):
            params["recipients"] = [recipient[5:]]
        else:
            params["recipients"] = [recipient]
        log.info("Sending to %s (%d chars)", recipient, len(plain_text))

    # Add quote parameters for reply-to functionality
    if quote_timestamp is not None and quote_author is not None:
        params["quoteTimestamp"] = int(quote_timestamp)
        # Strip uuid: prefix if present (same pattern as recipient handling)
        params["quoteAuthor"] = quote_author[5:] if quote_author.startswith("uuid:") else quote_author
        log.info("Quoting message from %s (ts:%d)", quote_author, quote_timestamp)

    payload = {
        "jsonrpc": "2.0",
        "method": "send",
        "id": "cleo-send",
        "params": params,
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        return f"Message sent to {'group ' + group_id[:12] if group_id else recipient}"
    except Exception as e:
        log.error("Failed to send message: %s", e)
        return f"Error sending message: {e}"


def memory_search(query: str, mode: str = "semantic", count: int = 10) -> str:
    """Search workspace memory files — semantic or exact grep."""
    if mode == "semantic":
        try:
            from vectorstore import query_memories, format_retrieved_context
            results = query_memories(query, n_results=count)
            if not results:
                return f"No semantic matches found for '{query}'"
            return format_retrieved_context(results)
        except Exception as e:
            log.warning("Semantic search failed, falling back to grep: %s", e)
            # Fall through to grep

    # Exact grep
    workspace = CONFIG["workspace"]
    try:
        result = subprocess.run(
            ["grep", "-r", "-i", "-n", "--include=*.md", query, workspace],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout
        if not output:
            return f"No matches found for '{query}'"
        if len(output) > 10000:
            output = output[:10000] + "\n... [truncated]"
        return output
    except Exception as e:
        return f"Error searching memory: {e}"


def search_torrents(query: str, media_type: str = "movie", max_results: int = 10) -> str:
    """
    Search IPTorrents and return raw results as JSON.
    NO filtering, NO duplicate checking — agent decides what to download.
    """
    workspace = CONFIG["workspace"]
    script_path = os.path.join(workspace, "scripts", "download.py")

    if not os.path.exists(script_path):
        return f"Download script not found at {script_path}"

    try:
        result = subprocess.run(
            ["python3", script_path, "--search-only", "--json", query, 
             "--type", media_type, "--max-results", str(max_results)],
            capture_output=True, text=True, timeout=60,
        )
        
        if result.returncode != 0:
            return f"Search error: {result.stderr}"

        output = result.stdout
        if len(output) > 10000:
            output = output[:10000] + "\n... [truncated]"
        return output
    except subprocess.TimeoutExpired:
        return f"Search timed out after 60s"
    except Exception as e:
        log.exception("search_torrents failed")
        return f"Error searching torrents: {e}"


def download_torrent(torrent_id: str, filename: str, destination: str) -> str:
    """
    Download a specific torrent by ID and add to Transmission.
    NO duplicate checking — agent must verify with list_torrents first.
    """
    workspace = CONFIG["workspace"]
    script_path = os.path.join(workspace, "scripts", "download.py")

    if not os.path.exists(script_path):
        return f"Download script not found at {script_path}"

    try:
        result = subprocess.run(
            ["python3", script_path, "--download-id", torrent_id, 
             "--filename", filename, "--dest", destination],
            capture_output=True, text=True, timeout=60,
        )
        
        # Return both stdout and stderr for full context
        output = result.stdout + result.stderr
        if len(output) > 5000:
            output = output[:5000] + "\n... [truncated]"
        return output
    except subprocess.TimeoutExpired:
        return f"Download timed out after 60s"
    except Exception as e:
        log.exception("download_torrent failed")
        return f"Error downloading torrent: {e}"


def download_media(query: str, media_type: str = "movie", index: int = 0) -> str:
    """
    [DEPRECATED - Use search_torrents + download_torrent instead]
    Search for and download media via IPTorrents → Transmission.
    Uses the download.py script in workspace/scripts/.
    """
    workspace = CONFIG["workspace"]
    script_path = os.path.join(workspace, "scripts", "download.py")

    if not os.path.exists(script_path):
        return f"Download script not found at {script_path}"

    try:
        result = subprocess.run(
            ["python3", script_path, query, "--type", media_type, "--index", str(index)],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        if len(output) > 5000:
            output = output[:5000] + "\n... [truncated]"
        return output
    except subprocess.TimeoutExpired:
        return f"Download search timed out after 60s"
    except Exception as e:
        return f"Error downloading media: {e}"


def list_torrents() -> str:
    """List all torrents in Transmission with status and progress."""
    try:
        # Load Transmission config from private/trackers.json
        workspace = CONFIG["workspace"]
        trackers_path = os.path.join(workspace, "private", "trackers.json")

        if not os.path.exists(trackers_path):
            return "trackers.json not found"

        with open(trackers_path) as f:
            cfg = json.load(f)

        trans = cfg["transmission"]
        url = trans["url"]
        creds = base64.b64encode(f"{trans['username']}:{trans['password']}".encode()).decode()

        # Get session ID (Transmission always returns 409 on first request with the session ID)
        sid = ""
        try:
            resp = requests.post(url, headers={"Authorization": f"Basic {creds}"})
            resp.raise_for_status()
        except requests.HTTPError as e:
            sid = e.response.headers.get("X-Transmission-Session-Id", "")

        # Get torrents
        payload = {
            "method": "torrent-get",
            "arguments": {
                "fields": ["name", "status", "percentDone", "downloadDir", "rateDownload", "rateUpload", "eta"]
            }
        }

        resp = requests.post(url, json=payload, headers={
            "Authorization": f"Basic {creds}",
            "X-Transmission-Session-Id": sid,
        }, timeout=10)

        data = resp.json()
        torrents = data.get("arguments", {}).get("torrents", [])

        if not torrents:
            return "No torrents in Transmission"

        # Status codes: 0=stopped, 1=check pending, 2=checking, 3=download pending, 4=downloading, 5=seed pending, 6=seeding
        status_map = {0: "stopped", 1: "verify pending", 2: "verifying", 3: "queue", 4: "downloading", 5: "queue", 6: "seeding"}

        lines = []
        for t in torrents[:50]:  # Limit to 50 for sanity
            status = status_map.get(t.get("status", 0), "unknown")
            percent = int(t.get("percentDone", 0) * 100)
            name = t.get("name", "Unknown")[:60]
            down_rate = t.get("rateDownload", 0) / 1024 / 1024  # MB/s

            if status == "downloading" and down_rate > 0:
                lines.append(f"⬇️  {name} — {percent}% ({down_rate:.1f} MB/s)")
            elif status == "seeding":
                lines.append(f"🌱 {name} — seeding")
            elif percent == 100:
                lines.append(f"✅ {name} — complete")
            else:
                lines.append(f"⏸️  {name} — {status} ({percent}%)")

        return "\n".join(lines)
    except Exception as e:
        log.exception("list_torrents failed")
        return f"Error listing torrents: {e}"


def send_reaction(emoji: str, target_author: str, target_timestamp: int,
                   recipient: str, group_id: str | None = None) -> str:
    """React to a Signal message with an emoji via signal-cli JSON-RPC."""
    rpc_url = CONFIG["signal_rpc_url"]
    bot_number = CONFIG["bot_number"]

    params: dict = {
        "account": bot_number,
        "emoji": emoji,
        "targetAuthor": target_author[5:] if target_author.startswith("uuid:") else target_author,
        "targetTimestamp": target_timestamp,
    }

    if group_id:
        params["groupId"] = group_id
    else:
        if recipient.startswith("uuid:"):
            params["recipients"] = [recipient[5:]]
        else:
            params["recipients"] = [recipient]

    payload = {
        "jsonrpc": "2.0",
        "method": "sendReaction",
        "id": "cleo-reaction",
        "params": params,
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Reacted %s to message from %s (ts:%d)", emoji, target_author, target_timestamp)
        return f"Reacted {emoji} to message from {target_author}"
    except Exception as e:
        log.error("Failed to send reaction: %s", e)
        return f"Error sending reaction: {e}"

def send_poll(recipient: str, question: str, options: list[str],
              group_id: str | None = None, allow_multiple: bool = True) -> str:
    """Create a Signal poll via signal-cli JSON-RPC."""
    rpc_url = CONFIG["signal_rpc_url"]
    bot_number = CONFIG["bot_number"]

    if len(options) < 2:
        return "Error: polls need at least 2 options"

    params: dict = {
        "account": bot_number,
        "question": question,
        "option": options,
    }
    if not allow_multiple:
        params["noMulti"] = True

    if group_id:
        params["groupId"] = group_id
    else:
        if recipient.startswith("uuid:"):
            params["recipients"] = [recipient[5:]]
        else:
            params["recipients"] = [recipient]

    payload = {
        "jsonrpc": "2.0",
        "method": "sendPollCreate",
        "id": "cleo-poll-create",
        "params": params,
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Poll created: '%s' with %d options", question, len(options))
        return f"Poll created: '{question}' with {len(options)} options"
    except Exception as e:
        log.error("Failed to create poll: %s", e)
        return f"Error creating poll: {e}"


def vote_poll(recipient: str, poll_author: str, poll_timestamp: int,
              option_indexes: list[int], vote_count: int = 1,
              group_id: str | None = None) -> str:
    """Vote on an existing Signal poll via signal-cli JSON-RPC."""
    rpc_url = CONFIG["signal_rpc_url"]
    bot_number = CONFIG["bot_number"]

    params: dict = {
        "account": bot_number,
        "pollAuthor": poll_author[5:] if poll_author.startswith("uuid:") else poll_author,
        "pollTimestamp": poll_timestamp,
        "option": option_indexes,
        "voteCount": vote_count,
    }

    if group_id:
        params["groupId"] = group_id
    else:
        if recipient.startswith("uuid:"):
            params["recipients"] = [recipient[5:]]
        else:
            params["recipients"] = [recipient]

    payload = {
        "jsonrpc": "2.0",
        "method": "sendPollVote",
        "id": "cleo-poll-vote",
        "params": params,
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Voted on poll (ts:%d): options %s", poll_timestamp, option_indexes)
        return f"Voted on poll (ts:{poll_timestamp}): options {option_indexes}"
    except Exception as e:
        log.error("Failed to vote on poll: %s", e)
        return f"Error voting on poll: {e}"


def _load_gemini_key() -> str | None:
    """Load Gemini API key from workspace private config."""
    path = os.path.join(CONFIG["workspace"], "private", "gemini.json")
    try:
        with open(path) as f:
            return json.load(f).get("api_key")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def generate_image(prompt: str) -> str:
    """Generate an image from a text prompt using Gemini API. Returns file path to the PNG."""
    api_key = _load_gemini_key()
    if not api_key:
        return f"Error: no Gemini API key — create {CONFIG['workspace']}/private/gemini.json with {{\"api_key\": \"...\"}}"

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("Gemini image generation request failed: %s", e)
        return f"Error calling Gemini API: {e}"

    # Find the image part in the response
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as e:
        log.error("Unexpected Gemini response structure: %s", data)
        return f"Error: unexpected Gemini response — {e}"

    for part in parts:
        inlineData = part.get("inlineData")
        if inlineData and inlineData.get("data"):
            mime = inlineData.get("mimeType", "image/png")
            ext = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")
            image_b64 = inlineData["data"]
            image_bytes = base64.b64decode(image_b64)
            out_path = f"/tmp/cleo-img-{uuid.uuid4()}{ext}"
            with open(out_path, "wb") as f:
                f.write(image_bytes)
            log.info("Generated image saved to %s (%d bytes, %s)", out_path, len(image_bytes), mime)
            return [
                {"type": "text", "text": f"Image generated and saved to {out_path}"},
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
            ]

    log.error("No image data in Gemini response: %s", data)
    return "Error: Gemini returned no image data"



def describe_image(image_path: str) -> str | list:
    """View an image file directly. Returns the image as a content block for Claude to see."""
    # Validate path
    resolved = os.path.realpath(image_path)
    if not (resolved.startswith("/tmp/") or any(resolved.startswith(p) for p in ALLOWED_PATH_PREFIXES)):
        return "Error: image path not allowed"

    if not os.path.exists(image_path):
        return f"Error: file not found: {image_path}"

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    if len(image_bytes) > 5 * 1024 * 1024:
        return "Error: image too large (>5MB)"

    image_b64 = base64.b64encode(image_bytes).decode()

    # Detect MIME type from magic bytes (extensions can lie)
    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        mime = "image/png"
    elif image_bytes[:3] == b'\xff\xd8\xff':
        mime = "image/jpeg"
    elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
        mime = "image/webp"
    elif image_bytes[:4] in (b'GIF8',):
        mime = "image/gif"
    else:
        mime = "image/png"

    return [
        {"type": "text", "text": f"Image at {image_path}:"},
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
    ]

def read_daily_log(date_str: str | None = None, offset: int = 0, max_chars: int = 50000) -> str:
    """Read a comprehensive daily log file, with chunking support."""
    from datetime import date
    if not date_str:
        date_str = date.today().isoformat()
    filepath = os.path.join(CONFIG["workspace"], "memory", f"{date_str}.md")
    if not os.path.exists(filepath):
        return f"No daily log for {date_str}."
    with open(filepath, "r") as f:
        f.seek(0, 2)
        total = f.tell()
        f.seek(offset)
        content = f.read(max_chars)
    if not content.strip() and offset == 0:
        return f"Daily log for {date_str} is empty."
    end_offset = offset + len(content)
    remaining = total - end_offset
    header = f"[daily log: {date_str} | {total} chars total | showing {offset}–{end_offset}]"
    if remaining > 0:
        header += f" [{remaining} chars remaining — call again with offset={end_offset}]"
    return f"{header}\n\n{content}"


def check_sms(count: int = 5) -> str:
    """Check recent incoming SMS via Twilio API."""
    try:
        cfg = _load_private_json("twilio.json")
    except FileNotFoundError:
        return "Error: twilio.json not found in workspace/private/"

    account_sid = cfg["account_sid"]
    auth_token = cfg["auth_token"]
    to_number = cfg["number"]

    try:
        resp = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            params={"To": to_number, "PageSize": count},
            auth=(account_sid, auth_token), timeout=10,
        )
        resp.raise_for_status()
        messages = resp.json().get("messages", [])
        if not messages:
            return "No incoming SMS found."
        lines = []
        for m in messages:
            sent = m.get("date_sent", "?")
            from_num = m.get("from", "?")
            body = m.get("body", "")
            lines.append(f"- [{sent}] from {from_num}: {body}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error checking SMS: {e}"


# --- Shared helpers ---

def _load_private_json(filename: str) -> dict:
    """Load a JSON config from {workspace}/private/{filename}."""
    path = os.path.join(CONFIG["workspace"], "private", filename)
    with open(path, "r") as f:
        return json.load(f)


# --- Emby admin ---

def emby_admin(action: str, **kwargs) -> str:
    """Emby media server admin operations."""
    try:
        cfg = _load_private_json("emby.json")
    except FileNotFoundError:
        return "Error: emby.json not found in workspace/private/"

    base = cfg["url"].rstrip("/")
    headers = {"X-Emby-Token": cfg["api_key"]}

    try:
        if action == "list_users":
            resp = requests.get(f"{base}/Users/Query", headers=headers, timeout=10)
            resp.raise_for_status()
            users = resp.json().get("Items", [])
            if not users:
                return "No users found."
            lines = []
            for u in users:
                last = u.get("LastActivityDate", "never")
                admin = " [admin]" if u.get("Policy", {}).get("IsAdministrator") else ""
                lines.append(f"- {u['Name']}{admin} (id: {u['Id']}, last active: {last})")
            return "\n".join(lines)

        elif action == "create_user":
            name = kwargs.get("name")
            if not name:
                return "Error: 'name' is required for create_user."
            resp = requests.post(f"{base}/Users/New", headers=headers, json={"Name": name}, timeout=10)
            resp.raise_for_status()
            user = resp.json()
            return f"Created user '{user.get('Name', name)}' (id: {user.get('Id', '?')})"

        elif action == "set_password":
            user_id = kwargs.get("user_id")
            password = kwargs.get("password")
            if not user_id or not password:
                return "Error: 'user_id' and 'password' are required for set_password."
            resp = requests.post(
                f"{base}/Users/{user_id}/Password", headers=headers,
                json={"NewPw": password, "ResetPassword": False}, timeout=10,
            )
            resp.raise_for_status()
            return f"Password set for user {user_id}."

        elif action == "delete_user":
            user_id = kwargs.get("user_id")
            if not user_id:
                return "Error: 'user_id' is required for delete_user."
            resp = requests.delete(f"{base}/Users/{user_id}", headers=headers, timeout=10)
            resp.raise_for_status()
            return f"Deleted user {user_id}."

        elif action == "list_libraries":
            resp = requests.get(f"{base}/Library/MediaFolders", headers=headers, timeout=10)
            resp.raise_for_status()
            folders = resp.json().get("Items", [])
            if not folders:
                return "No libraries found."
            lines = [f"- {f['Name']} (id: {f['Id']}, type: {f.get('CollectionType', '?')})" for f in folders]
            return "\n".join(lines)

        elif action == "scan_library":
            resp = requests.post(f"{base}/Library/Refresh", headers=headers, timeout=10)
            resp.raise_for_status()
            return "Library scan started."

        elif action == "sessions":
            resp = requests.get(f"{base}/Sessions", headers=headers, timeout=10)
            resp.raise_for_status()
            sessions = resp.json()
            active = [s for s in sessions if s.get("NowPlayingItem")]
            if not active:
                return "No active playback sessions."
            lines = []
            for s in active:
                item = s["NowPlayingItem"]
                user = s.get("UserName", "?")
                title = item.get("Name", "?")
                series = item.get("SeriesName")
                if series:
                    title = f"{series} — {title}"
                client = s.get("Client", "?")
                lines.append(f"- {user} watching '{title}' on {client}")
            return "\n".join(lines)

        elif action == "server_info":
            resp = requests.get(f"{base}/System/Info", headers=headers, timeout=10)
            resp.raise_for_status()
            info = resp.json()
            return (
                f"Emby Server v{info.get('Version', '?')}\n"
                f"Name: {info.get('ServerName', '?')}\n"
                f"OS: {info.get('OperatingSystemDisplayName', '?')}\n"
                f"Pending restart: {info.get('HasPendingRestart', False)}\n"
                f"Local address: {info.get('LocalAddress', '?')}"
            )

        elif action == "activity_log":
            limit = kwargs.get("limit", 20)
            resp = requests.get(
                f"{base}/System/ActivityLog/Entries",
                headers=headers, params={"Limit": limit}, timeout=10,
            )
            resp.raise_for_status()
            entries = resp.json().get("Items", [])
            if not entries:
                return "No activity log entries."
            lines = []
            for e in entries:
                dt = e.get("Date", "?")
                severity = e.get("Severity", "Info")
                name = e.get("Name", "?")
                lines.append(f"- [{severity}] {dt}: {name}")
            return "\n".join(lines)

        else:
            return f"Unknown emby_admin action: {action}"

    except requests.RequestException as e:
        return f"Emby API error: {e}"


# --- Audiobookshelf admin ---

def audiobookshelf_admin(action: str, **kwargs) -> str:
    """Audiobookshelf admin operations."""
    try:
        cfg = _load_private_json("audiobookshelf.json")
    except FileNotFoundError:
        return "Error: audiobookshelf.json not found in workspace/private/"

    base = cfg["url"].rstrip("/")
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

    try:
        if action == "list_users":
            resp = requests.get(f"{base}/api/users", headers=headers, timeout=10)
            resp.raise_for_status()
            users = resp.json().get("users", resp.json() if isinstance(resp.json(), list) else [])
            if not users:
                return "No users found."
            lines = []
            for u in users:
                utype = u.get("type", "user")
                lines.append(f"- {u.get('username', '?')} ({utype}, id: {u.get('id', '?')})")
            return "\n".join(lines)

        elif action == "create_user":
            name = kwargs.get("name")
            password = kwargs.get("password")
            if not name or not password:
                return "Error: 'name' and 'password' are required for create_user."
            resp = requests.post(
                f"{base}/api/users", headers=headers,
                json={"username": name, "password": password, "type": "user"}, timeout=10,
            )
            resp.raise_for_status()
            user = resp.json().get("user", resp.json())
            return f"Created user '{user.get('username', name)}' (id: {user.get('id', '?')})"

        elif action == "delete_user":
            user_id = kwargs.get("user_id")
            if not user_id:
                return "Error: 'user_id' is required for delete_user."
            resp = requests.delete(f"{base}/api/users/{user_id}", headers=headers, timeout=10)
            resp.raise_for_status()
            return f"Deleted user {user_id}."

        elif action == "list_libraries":
            resp = requests.get(f"{base}/api/libraries", headers=headers, timeout=10)
            resp.raise_for_status()
            libraries = resp.json().get("libraries", resp.json() if isinstance(resp.json(), list) else [])
            if not libraries:
                return "No libraries found."
            lines = []
            for lib in libraries:
                media_type = lib.get("mediaType", "?")
                name = lib.get("name", "?")
                lines.append(f"- {name} ({media_type}, id: {lib.get('id', '?')})")
            return "\n".join(lines)

        elif action == "scan_library":
            library_id = kwargs.get("library_id")
            if not library_id:
                return "Error: 'library_id' is required for scan_library."
            resp = requests.post(f"{base}/api/libraries/{library_id}/scan", headers=headers, timeout=10)
            resp.raise_for_status()
            return f"Library scan started for {library_id}."

        elif action == "sessions":
            resp = requests.get(f"{base}/api/users/online", headers=headers, timeout=10)
            resp.raise_for_status()
            online = resp.json() if isinstance(resp.json(), list) else resp.json().get("usersOnline", [])
            if not online:
                return "No active sessions."
            lines = [f"- {u.get('username', '?')} (online)" for u in online]
            return "\n".join(lines)

        elif action == "server_info":
            resp = requests.get(f"{base}/status", headers=headers, timeout=10)
            resp.raise_for_status()
            status = resp.json()
            resp2 = requests.post(f"{base}/api/authorize", headers=headers, timeout=10)
            resp2.raise_for_status()
            auth = resp2.json()
            server = auth.get("server", {})
            return (
                f"Audiobookshelf v{server.get('version', status.get('serverVersion', '?'))}\n"
                f"Initialized: {status.get('isInit', '?')}\n"
                f"Auth user: {auth.get('user', {}).get('username', '?')}"
            )

        elif action == "listening_stats":
            user_id = kwargs.get("user_id")
            if not user_id:
                return "Error: 'user_id' is required for listening_stats."
            resp = requests.get(f"{base}/api/users/{user_id}/listening-stats", headers=headers, timeout=10)
            resp.raise_for_status()
            stats = resp.json()
            total_time = stats.get("totalTime", 0)
            hours = total_time / 3600
            days = stats.get("days", {})
            recent_sessions = stats.get("recentSessions", [])
            lines = [f"Total listening: {hours:.1f} hours"]
            if recent_sessions:
                lines.append(f"Recent sessions: {len(recent_sessions)}")
                for s in recent_sessions[:5]:
                    display = s.get("displayTitle", s.get("mediaMetadata", {}).get("title", "?"))
                    lines.append(f"  - {display}")
            return "\n".join(lines)

        else:
            return f"Unknown audiobookshelf_admin action: {action}"

    except requests.RequestException as e:
        return f"Audiobookshelf API error: {e}"


def check_quota() -> str:
    """Check current API rate limit utilization and return formatted summary."""
    from gateway import _rate_limits
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if not _rate_limits:
        return "No rate limit data available yet (no API calls made since startup)."

    et = ZoneInfo("America/New_York")
    lines = ["## API Rate Limit Status\n"]

    # 5-hour window
    util_5h = _rate_limits.get("anthropic-ratelimit-unified-5h-utilization")
    status_5h = _rate_limits.get("anthropic-ratelimit-unified-5h-status", "unknown")
    reset_5h = _rate_limits.get("anthropic-ratelimit-unified-5h-reset")

    pct_5h = f"{float(util_5h) * 100:.1f}%" if util_5h else "N/A"
    lines.append(f"**5-hour window:** {pct_5h} utilized — status: {status_5h}")
    if reset_5h:
        try:
            reset_dt = datetime.fromtimestamp(float(reset_5h), tz=et)
            lines.append(f"  Resets at: {reset_dt.strftime('%-I:%M %p ET on %b %d')}")
        except (ValueError, TypeError, OSError):
            lines.append(f"  Reset timestamp: {reset_5h}")

    # 7-day window
    util_7d = _rate_limits.get("anthropic-ratelimit-unified-7d-utilization")
    status_7d = _rate_limits.get("anthropic-ratelimit-unified-7d-status", "unknown")

    pct_7d = f"{float(util_7d) * 100:.1f}%" if util_7d else "N/A"
    lines.append(f"\n**7-day window:** {pct_7d} utilized — status: {status_7d}")

    # 7-day Sonnet window
    util_sonnet = _rate_limits.get("anthropic-ratelimit-unified-7d_sonnet-utilization")
    pct_sonnet = f"{float(util_sonnet) * 100:.1f}%" if util_sonnet else "N/A"
    lines.append(f"\n**7-day Sonnet window:** {pct_sonnet} utilized")

    # Fallback
    fallback = _rate_limits.get("anthropic-ratelimit-unified-fallback", "unknown")
    lines.append(f"\n**Fallback availability:** {fallback}")

    # Overage
    overage = _rate_limits.get("anthropic-ratelimit-unified-overage-status", "unknown")
    lines.append(f"**Overage status:** {overage}")

    return "\n".join(lines)


# Dispatch table: tool name \u2192 function
TOOL_DISPATCH = {
    "exec_command": lambda args: exec_command(args["command"], args.get("timeout", 30)),
    "read_file": lambda args: read_file(args["path"], args.get("offset", 1), args.get("limit", 2000)),
    "edit_file": lambda args: edit_file(args["path"], args["old_string"], args["new_string"]),
    "write_file": lambda args: write_file(args["path"], args["content"]),
    "grep_search": lambda args: grep_search(args["pattern"], args.get("path", "."), args.get("include"), args.get("context", 0), args.get("max_results", 50)),
    "find_files": lambda args: find_files(args["pattern"], args.get("path", "."), args.get("max_results", 100)),
    "web_search": lambda args: web_search(args["query"], args.get("count", 5)),
    "web_fetch": lambda args: web_fetch(args["url"]),
    "check_weather": lambda args: check_weather(args["location"]),
    "send_message": lambda args: send_message(args["recipient"], args["message"], attachment=args.get("attachment"), quote_timestamp=args.get("quote_timestamp"), quote_author=args.get("quote_author")),
    "memory_search": lambda args: memory_search(args["query"], args.get("mode", "semantic"), args.get("count", 10)),
    "send_reaction": lambda args: send_reaction(args["emoji"], args["target_author"], args["target_timestamp"], args["recipient"]),
    "send_poll": lambda args: send_poll(args["recipient"], args["question"], args["options"], allow_multiple=args.get("allow_multiple", True)),
    "generate_image": lambda args: generate_image(args["prompt"]),
    "describe_image": lambda args: describe_image(args["image_path"]),
    "check_sms": lambda args: check_sms(args.get("count", 5)),
    "check_quota": lambda args: check_quota(),
}


def execute_tool(name: str, args: dict) -> str:
    """Execute a tool by name with the given arguments. Returns result string."""
    handler = TOOL_DISPATCH.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    log.info("Tool call: %s(%s)", name, ", ".join(f"{k}={v!r}" for k, v in args.items()))
    try:
        return handler(args)
    except Exception as e:
        log.exception("Tool %s failed", name)
        return f"Tool error: {e}"
