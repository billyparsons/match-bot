"""
memory.py — Load memory files into the system prompt context.

Reads SOUL.md, USER.md, MEMORY.md from workspace root.
Daily memory files are NOT loaded into the prompt — they're rich logs
meant for dream consolidation (2am) and queryable via read_daily_log tool.
Older memories and project notes are retrieved via the vector store.

Security model:
- Only reads from the configured workspace path.
- File contents are included in the system prompt sent to the LLM.
- No secrets are stored in memory files.
"""

import os
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import identity
from config import CONFIG


def _tz_abbrev() -> str:
    """Return short timezone abbreviation (e.g. 'EST', 'CDT', 'PT')."""
    try:
        tz = ZoneInfo(CONFIG.get("timezone", "America/New_York"))
        return datetime.now(tz).strftime("%Z")
    except Exception:
        return "ET"

log = logging.getLogger("cleo.memory")

# Group names now resolved dynamically via identity module


def _read_if_exists(path: str) -> str | None:
    """Read a file if it exists, return None otherwise."""
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _format_conversation_context(sender_id: str, group_id: str | None) -> str:
    """
    Format conversation context for the system prompt.
    Returns a clear statement of whether this is a DM or group chat.
    """
    if group_id:
        group_name = identity.resolve_group_name(group_id)
        return f"**Current conversation:** Group chat '{group_name}' (group_id: {group_id[:16]}...)\n**Sender:** {identity.resolve_name(sender_id)}"
    else:
        return f"**Current conversation:** Direct message (DM)\n**Sender:** {identity.resolve_name(sender_id)}"


def build_base_prompt(workspace: str, sender_id: str | None = None, group_id: str | None = None) -> str:
    """
    Assemble the always-included identity prompt.

    Loads:
      - SOUL.md   — personality, always relevant
      - USER.md   — who the user is, always relevant
      - MEMORY.md — curated long-term knowledge, always relevant
      - Conversation context — DM vs group chat (if provided)
    """
    parts = []
    log.debug("build_base_prompt called with sender_id=%s, group_id=%s, condition=%s", sender_id, group_id, sender_id is not None)

    # Current date/time so Cleo always knows when it is
    now = datetime.now()
    parts.append(f"**Now:** {now.strftime('%A, %Y-%m-%d, at %-I:%M %p') + f" {_tz_abbrev()}"}")

    # Conversation context first (if available) so Claude knows immediately
    if sender_id is not None:
        context = _format_conversation_context(sender_id, group_id)
        parts.append(f"## Current Context\n\n{context}")

    # Core identity and context files
    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        content = _read_if_exists(os.path.join(workspace, filename))
        if content:
            parts.append(f"## {filename}\n\n{content.strip()}")

    prompt = "\n\n---\n\n".join(parts)
    log.info("Base prompt built: %d chars from %d sections (context: %s)",
             len(prompt), len(parts),
             identity.resolve_group_name(group_id) if group_id else ("DM" if sender_id is not None else "no context"))
    return prompt


def build_static_prompt(workspace: str) -> str:
    """Return the stable portion of the system prompt (SOUL + USER + MEMORY).

    Contains no timestamps or per-request context, so it can be placed in a
    cache_control: ephemeral block and hit on every subsequent API call within
    the 5-minute TTL window.
    """
    parts = []
    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        content = _read_if_exists(os.path.join(workspace, filename))
        if content:
            parts.append(f"## {filename}\n\n{content.strip()}")
    prompt = "\n\n---\n\n".join(parts)
    log.info("Static prompt built: %d chars from %d sections", len(prompt), len(parts))
    return prompt


def build_dynamic_context(sender_id: str | None = None, group_id: str | None = None) -> str:
    """Return the volatile portion of the system prompt (timestamp + conversation context).

    Changes every invocation, so it must NOT be placed in a cache_control block.
    Passed as a separate uncached system block alongside the cached static block.
    """
    now = datetime.now()
    parts = [f"**Now:** {now.strftime('%A, %Y-%m-%d, at %-I:%M %p') + f" {_tz_abbrev()}"}"]
    if sender_id is not None:
        context = _format_conversation_context(sender_id, group_id)
        parts.append(f"## Current Context\n\n{context}")
    return "\n\n---\n\n".join(parts)


def build_enriched_prompt(workspace: str, user_message: str, sender_id: str | None = None, group_id: str | None = None) -> str:
    """
    Build the full system prompt: base identity + conversation context + semantically retrieved context.

    1. Build base prompt (SOUL + USER + MEMORY + today's daily + conversation context)
    2. Query vectorstore for chunks relevant to user_message
    3. Format and append retrieved context
    """
    from vectorstore import query_memories, format_retrieved_context

    base = build_base_prompt(workspace, sender_id, group_id)

    # Retrieve relevant memories from older daily files and project notes
    try:
        results = query_memories(user_message, n_results=10)
    except Exception as e:
        log.warning("Vector retrieval failed, using base prompt only: %s", e)
        return base

    if results:
        context = format_retrieved_context(results)
        prompt = base + "\n\n---\n\n## Retrieved Memories\n\n" + context
    else:
        prompt = base

    log.info("Enriched prompt: %d chars (base=%d, retrieved=%d chunks)",
             len(prompt), len(base), len(results))
    return prompt


def build_system_prompt(workspace: str) -> str:
    """
    Legacy system prompt builder (deprecated).
    Kept as fallback if vectorstore is not available.
    """
    parts = []

    for filename in ("SOUL.md", "USER.md", "MEMORY.md"):
        content = _read_if_exists(os.path.join(workspace, filename))
        if content:
            parts.append(f"## {filename}\n\n{content.strip()}")

    today = date.today()
    yesterday = today - timedelta(days=1)
    for d in (yesterday, today):
        filename = f"memory/{d.isoformat()}.md"
        content = _read_if_exists(os.path.join(workspace, filename))
        if content:
            parts.append(f"## {filename}\n\n{content.strip()}")

    prompt = "\n\n---\n\n".join(parts)
    log.info("System prompt built: %d chars from %d sections", len(prompt), len(parts))
    return prompt


def append_daily_memory(workspace: str, summary: str) -> None:
    """Append an entry to today's daily log file.

    Daily logs accumulate rich detail during the day. The dream heartbeat
    (2 AM) reads and consolidates them into long-term vector memory.
    These files are NOT loaded into the prompt — use read_daily_log tool.
    """
    today = date.today()
    filepath = os.path.join(workspace, "memory", f"{today.isoformat()}.md")

    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "a") as f:
        f.write(f"\n{summary}\n")
    log.info("Appended to daily memory: %s", filepath)
