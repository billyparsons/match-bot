"""
gateway.py — Async main event loop for cleo.

Architecture (feed-based pull model):
  1. Connect to signal-cli TCP JSON-RPC for incoming messages.
  2. Buffer messages into per-feed queues (each group = 1 feed, each DM = 1 feed).
  3. After 5 seconds of quiet, wake Cleo with a summary of unread feeds.
  4. Cleo reads feeds via tools, decides what to respond to, sends messages explicitly.
  5. Append interaction summaries to today's daily memory file.

Key properties:
  - Silence is the default — Cleo only sends messages via explicit send_message tool calls.
  - Single persistent conversation history ("consciousness") across all wake-ups.
  - Messages from all feeds are logged to daily memory for passive awareness.

Security model:
  - Only messages from authorized_senders/groups are processed.
  - Auth uses OAuth token from ~/.claude/.credentials.json (Claude Max subscription).
  - TCP reconnects with exponential backoff to handle signal-cli restarts.
"""

import os
import sys
import time
import json
import base64
import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta
from collections import defaultdict, deque
import hashlib

# WARNING: If Match suddenly stops working, check if Anthropic updated Claude Code.
# Update CLAUDE_CODE_VERSION to match `claude --version` and check clewdr repo
# (https://github.com/Xerxes-2/clewdr) for updated BILLING_SALT.
CLAUDE_CODE_VERSION = "2.1.76"
CLAUDE_CODE_BILLING_SALT = "59cf53e54c78"

def _compute_billing_header(first_user_text: str) -> str:
    """Compute the billing header Anthropic now requires for OAuth."""
    chars = []
    utf16 = first_user_text.encode('utf-16-le')
    for idx in [4, 7, 20]:
        if idx * 2 + 1 < len(utf16):
            unit = int.from_bytes(utf16[idx*2:idx*2+2], 'little')
            chars.append(chr(unit))
        else:
            chars.append('0')
    sampled = ''.join(chars)
    version_hash = hashlib.sha256(
        f'{CLAUDE_CODE_BILLING_SALT}{sampled}{CLAUDE_CODE_VERSION}'.encode()
    ).hexdigest()
    return (
        f"x-anthropic-billing-header: cc_version={CLAUDE_CODE_VERSION}"
        f".{version_hash[:3]}; cc_entrypoint=cli; cch=00000;"
    )

from anthropic import AsyncAnthropic

from config import CONFIG, load_credentials
from memory import build_static_prompt, build_dynamic_context, append_daily_memory
from tools import TOOL_DEFINITIONS, SUBAGENT_TOOL_DEFINITIONS, execute_tool, send_message, send_reaction, send_typing, send_poll, vote_poll
from scheduler import start_scheduler
from apscheduler.triggers.date import DateTrigger
from auth import is_authorized, refresh_all_groups
import identity

# --- Logging setup ---
# Log to both stderr (for systemd journal) and a rotating file

log = logging.getLogger("cleo")
log.setLevel(logging.DEBUG)

# Stderr handler (systemd captures this)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
log.addHandler(stderr_handler)

# Rotating file handler
file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(CONFIG["workspace"], "cleo.log"), maxBytes=5_000_000, backupCount=3
)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
log.addHandler(file_handler)

# --- Anthropic API client ---
# Initialized in main() after loading credentials
api_client: AsyncAnthropic | None = None

# --- Feed system ---
# Each group or DM partner is a "feed". Messages are kept as rolling history.
# feed_id: group name for groups ("bots", "Alexandria"), sender phone for DMs ("+1...")
feeds: dict[str, dict] = {}
# feeds[feed_id] = {"group_id": str|None, "messages": [{"sender": str, "text": str, "timestamp": str}]}

unread_feed_ids: set[str] = set()

MAX_FEED_MESSAGES = 6  # max messages retained per feed
FEEDS_FILE = os.path.join(CONFIG["workspace"], "feeds.json")
CONSCIOUSNESS_FILE = os.path.join(CONFIG["workspace"], "consciousness.json")
CONTEXT_DUMP_FILE = os.path.join(CONFIG["workspace"], "context-dump.json")
REMINDERS_FILE = os.path.join(CONFIG["workspace"], "reminders.json")


def _save_feeds() -> None:
    """Persist feeds + unread set to disk (atomic write)."""
    try:
        data = {"feeds": feeds, "unread": list(unread_feed_ids)}
        tmp = FEEDS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, FEEDS_FILE)
    except Exception as e:
        log.warning("Failed to save feeds: %s", e)


def _load_feeds() -> None:
    """Restore feeds + unread set from disk on startup."""
    try:
        with open(FEEDS_FILE, "r") as f:
            data = json.load(f)
        feeds.update(data.get("feeds", {}))
        unread_feed_ids.update(data.get("unread", []))
        total_msgs = sum(len(f.get("messages", [])) for f in feeds.values())
        if total_msgs:
            log.info("Restored %d feeds with %d messages (%d unread)",
                     len(feeds), total_msgs, len(unread_feed_ids))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Failed to load feeds: %s", e)




MAX_CONSCIOUSNESS_MESSAGES = 20

def _save_consciousness() -> None:
    """Persist consciousness to disk (atomic write)."""
    try:
        # Hard cap: keep only recent messages
        if len(consciousness) > MAX_CONSCIOUSNESS_MESSAGES:
            consciousness[:] = consciousness[-MAX_CONSCIOUSNESS_MESSAGES:]
        data = {"messages": consciousness, "last_input_tokens": _last_input_tokens}
        tmp = CONSCIOUSNESS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CONSCIOUSNESS_FILE)
    except Exception as e:
        log.warning("Failed to save consciousness: %s", e)


def _load_consciousness() -> None:
    """Restore consciousness from disk on startup."""
    global _last_input_tokens
    try:
        with open(CONSCIOUSNESS_FILE, "r") as f:
            data = json.load(f)
        consciousness.extend(data.get("messages", []))
        _last_input_tokens = data.get("last_input_tokens", 0)
        if consciousness:
            log.info("Restored consciousness: %d messages (%d last input tokens)",
                     len(consciousness), _last_input_tokens)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Failed to load consciousness: %s", e)


# --- Reminders ---

_ET = datetime.now().astimezone().tzinfo  # Local timezone (ET)

def _save_reminders() -> None:
    """Persist reminders to disk (atomic write)."""
    try:
        tmp = REMINDERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_reminders, f, indent=2)
        os.replace(tmp, REMINDERS_FILE)
    except Exception as e:
        log.warning("Failed to save reminders: %s", e)


def _load_reminders() -> None:
    """Load reminders from disk on startup."""
    try:
        with open(REMINDERS_FILE, "r") as f:
            _reminders.extend(json.load(f))
        if _reminders:
            log.info("Loaded %d reminders from disk", len(_reminders))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Failed to load reminders: %s", e)


async def _fire_reminder(reminder_id: str) -> None:
    """Fire a reminder: inject as feed event, then remove from persistent list."""
    reminder = next((r for r in _reminders if r["id"] == reminder_id), None)
    if not reminder:
        log.warning("Reminder %s not found (already fired?)", reminder_id)
        return

    log.info("Firing reminder: %s", reminder_id)
    await scheduled_event_callback(f"reminder:{reminder_id}", reminder["prompt"])

    _reminders[:] = [r for r in _reminders if r["id"] != reminder_id]
    _save_reminders()


def schedule_reminder(fire_at: str, prompt: str) -> str:
    """Schedule a one-time reminder. Returns confirmation or error string."""
    if not _scheduler:
        return "Error: scheduler not initialized"

    # Parse datetime, default to ET if no timezone
    try:
        dt = datetime.fromisoformat(fire_at)
        if dt.tzinfo is None:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    except ValueError as e:
        return f"Error: invalid datetime '{fire_at}' — use ISO 8601 format (e.g. 2026-02-26T08:00:00). {e}"

    now = datetime.now(dt.tzinfo)
    if dt <= now:
        return f"Error: {fire_at} is in the past (current time: {now.strftime('%Y-%m-%d %H:%M')})"

    reminder_id = f"reminder-{int(dt.timestamp())}"

    # Persist
    _reminders.append({
        "id": reminder_id,
        "fire_at": dt.isoformat(),
        "prompt": prompt,
        "created_at": datetime.now().isoformat(),
    })
    _save_reminders()

    # Register with APScheduler
    async def job(rid=reminder_id):
        await _fire_reminder(rid)

    _scheduler.add_job(
        job,
        trigger=DateTrigger(run_date=dt),
        id=reminder_id,
        name=f"Reminder: {prompt[:50]}",
        replace_existing=True,
    )

    log.info("Scheduled reminder %s for %s: %s", reminder_id, dt.isoformat(), prompt[:80])
    return f"Reminder scheduled for {dt.strftime('%A, %B %d at %-I:%M %p %Z')}"


def _register_reminders_on_startup() -> None:
    """Re-register persisted reminders with APScheduler after startup.

    Past reminders are fired immediately; future ones are scheduled normally.
    """
    if not _scheduler or not _reminders:
        return

    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    fired = []

    for r in list(_reminders):
        dt = datetime.fromisoformat(r["fire_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))

        if dt <= now:
            # Missed during downtime — fire immediately
            log.info("Firing missed reminder: %s (was due %s)", r["id"], r["fire_at"])
            fired.append(r)
        else:
            # Still in the future — register with scheduler
            async def job(rid=r["id"]):
                await _fire_reminder(rid)

            _scheduler.add_job(
                job,
                trigger=DateTrigger(run_date=dt),
                id=r["id"],
                name=f"Reminder: {r['prompt'][:50]}",
                replace_existing=True,
            )
            log.info("Re-registered reminder %s for %s", r["id"], r["fire_at"])

    # Fire missed reminders (async, so schedule as immediate tasks)
    for r in fired:
        async def fire_missed(rid=r["id"]):
            await _fire_reminder(rid)
        _scheduler.add_job(fire_missed, id=f"missed-{r['id']}", replace_existing=True)


def _dump_context(model: str, system: list, tools: list, context_management: dict) -> None:
    """Dump the exact API request payload to disk for debugging.

    Pure visibility — nothing reads this file programmatically.
    """
    try:
        with open(CONTEXT_DUMP_FILE, "w") as f:
            json.dump({
                "model": model,
                "max_tokens": MAX_TOKENS,
                "system": system,
                "messages": consciousness,
                "tools": tools,
                "context_management": context_management,
            }, f, indent=2, default=str)
    except Exception:
        pass  # Never interfere with wake loop


# --- Consciousness ---
# Single persistent conversation history across all wake-ups.
# Cleo has one continuous stream of thought spanning all feeds.
consciousness: list[dict] = []
consciousness_lock: asyncio.Lock = asyncio.Lock()

# --- Debounce ---
WAKE_DEBOUNCE_DM = 0      # instant wake for DMs
WAKE_DEBOUNCE_GROUP = 3.5
_wake_task: asyncio.Task | None = None
_wake_deadline: float = 0.0  # monotonic time when current wake will fire

# --- Rate limiting ---
RATE_LIMIT_MAX = 30
RATE_LIMIT_WINDOW = 60  # seconds
rate_limit_windows: dict[str, deque] = defaultdict(deque)

# --- API rate limit / quota tracking ---
# Updated after every API call with headers from the raw response
_rate_limits: dict[str, str] = {}


def _update_rate_limits(headers) -> None:
    """Extract rate limit headers and update the module-level tracking dict."""
    header_keys = [
        "anthropic-ratelimit-unified-5h-utilization",
        "anthropic-ratelimit-unified-5h-status",
        "anthropic-ratelimit-unified-5h-reset",
        "anthropic-ratelimit-unified-7d-utilization",
        "anthropic-ratelimit-unified-7d-status",
        "anthropic-ratelimit-unified-7d_sonnet-utilization",
        "anthropic-ratelimit-unified-fallback",
        "anthropic-ratelimit-unified-overage-status",
    ]
    for key in header_keys:
        val = headers.get(key)
        if val is not None:
            _rate_limits[key] = val

    # Log summary
    util_5h = _rate_limits.get("anthropic-ratelimit-unified-5h-utilization", "?")
    util_7d = _rate_limits.get("anthropic-ratelimit-unified-7d-utilization", "?")
    util_sonnet = _rate_limits.get("anthropic-ratelimit-unified-7d_sonnet-utilization", "?")
    status = _rate_limits.get("anthropic-ratelimit-unified-5h-status", "?")

    # Format percentages (values are 0-1 floats)
    def _fmt_pct(val):
        if val == "?":
            return "?"
        try:
            return f"{float(val) * 100:.0f}%"
        except (ValueError, TypeError):
            return str(val)

    log.info("Rate limits \u2014 5h: %s | 7d: %s | 7d-sonnet: %s | status: %s",
             _fmt_pct(util_5h), _fmt_pct(util_7d), _fmt_pct(util_sonnet), status)
    # Update usage tracking with latest OAuth utilization
    if util_5h != "?" and _usage:
        try:
            _usage["oauth"]["5h"] = float(util_5h)
            _usage["oauth"]["7d"] = float(util_7d) if util_7d != "?" else _usage["oauth"].get("7d", 0.0)
        except (ValueError, TypeError):
            pass

# --- Replay protection ---
MAX_MESSAGE_AGE_SECONDS = 300  # 5 minutes

# --- Agentic loop constants ---
MAX_AGENTIC_ITERATIONS = 50  # safety limit on tool loops
MAX_TOKENS = 16384  # max output tokens per API call
_last_input_tokens: int = 0  # updated after each successful API call

# --- Usage tracking ---
USAGE_FILE = os.path.join(CONFIG["workspace"], "usage.json")
# Sonnet 4.6 pricing (per million tokens)
PRICE_INPUT_PER_M = 3.00
PRICE_OUTPUT_PER_M = 15.00

_usage: dict = {}  # loaded at startup

def _load_usage() -> None:
    global _usage
    try:
        with open(USAGE_FILE, "r") as f:
            _usage = json.load(f)
    except FileNotFoundError:
        _usage = {}
    # Ensure structure
    _usage.setdefault("oauth", {"5h": 0.0, "7d": 0.0})
    _usage.setdefault("api", {"session_cost": 0.0, "session_tokens_in": 0, "session_tokens_out": 0})
    # Delta-based limits: how much a task is allowed to consume from its baseline
    # oauth_delta: fraction of 5h window (0.15 = 15 percentage points)
    # api_delta: dollars ($1.00 default)
    _usage.setdefault("limits", {"oauth_delta": 0.15, "api_delta": 1.00})
    _usage.setdefault("tasks", {})

def _save_usage() -> None:
    try:
        tmp = USAGE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_usage, f, indent=2)
        os.replace(tmp, USAGE_FILE)
    except Exception as e:
        log.warning("Failed to save usage: %s", e)

def _update_usage(tokens_in: int, tokens_out: int, task_id: str | None = None) -> None:
    """Update usage tracking after an API call."""
    cost = (tokens_in / 1_000_000 * PRICE_INPUT_PER_M) + (tokens_out / 1_000_000 * PRICE_OUTPUT_PER_M)
    _usage["api"]["session_cost"] = round(_usage["api"].get("session_cost", 0) + cost, 6)
    _usage["api"]["session_tokens_in"] = _usage["api"].get("session_tokens_in", 0) + tokens_in
    _usage["api"]["session_tokens_out"] = _usage["api"].get("session_tokens_out", 0) + tokens_out
    if task_id:
        if task_id not in _usage["tasks"]:
            _usage["tasks"][task_id] = {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}
        _usage["tasks"][task_id]["tokens_in"] += tokens_in
        _usage["tasks"][task_id]["tokens_out"] += tokens_out
        _usage["tasks"][task_id]["cost"] = round(_usage["tasks"][task_id]["cost"] + cost, 6)
    _save_usage()

def _check_task_limits(task_id: str) -> dict | None:
    """Check if a specific task has exceeded its delta limits. Returns violation dict or None."""
    task = _usage.get("tasks", {}).get(task_id)
    if not task:
        return None
    oauth_delta = _usage["limits"].get("oauth_delta", 0.15)
    api_delta = _usage["limits"].get("api_delta", 1.00)
    task_type = task.get("task_type", "subagent")  # default to subagent for safety
    # Check OAuth delta
    baseline_oauth = task.get("baseline_oauth_5h", 0.0)
    current_oauth = _usage["oauth"].get("5h", 0.0)
    try:
        current_oauth = float(current_oauth)
        baseline_oauth = float(baseline_oauth)
    except (ValueError, TypeError):
        current_oauth = 0.0
        baseline_oauth = 0.0
    # Handle 5h window reset — if current < baseline, window rolled over
    # Reset baseline to 0 so delta is measured from the fresh window
    if current_oauth < baseline_oauth:
        log.info("5h window reset detected for task %s — resetting baseline to 0", task_id)
        task["baseline_oauth_5h"] = 0.0
        baseline_oauth = 0.0
        _save_usage()
    oauth_used = current_oauth - baseline_oauth
    if task_type == "subagent" and oauth_used >= oauth_delta:
        return {
            "type": "oauth_delta",
            "task_id": task_id,
            "baseline": baseline_oauth,
            "current": current_oauth,
            "used": oauth_used,
            "limit": oauth_delta,
            "msg": f"task {task_id} used {oauth_used*100:.1f}% of 5h session (limit: {oauth_delta*100:.0f}%)"
        }
    # Check API delta
    baseline_api = task.get("baseline_api_cost", 0.0)
    current_api = _usage["api"].get("session_cost", 0.0)
    api_used = current_api - baseline_api
    if task_type == "looper" and api_used >= api_delta:
        return {
            "type": "api_delta",
            "task_id": task_id,
            "baseline": baseline_api,
            "current": current_api,
            "used": api_used,
            "limit": api_delta,
            "msg": f"task {task_id} spent ${api_used:.4f} (limit: ${api_delta:.2f})"
        }
    return None

def _check_limits() -> dict | None:
    """Check all active tasks for limit violations. Returns first violation or None."""
    for task_id in list(_usage.get("tasks", {}).keys()):
        violation = _check_task_limits(task_id)
        if violation:
            return violation
    return None

# --- Subagent constants ---
MAX_SUBAGENT_ITERATIONS = 50
MAX_CONCURRENT_SUBAGENTS = 3  # per person
active_subagents: dict[str, list[asyncio.Task]] = defaultdict(list)

def cancel_all_subagents() -> str:
    """Cancel all running subagent tasks."""
    total = 0
    for key, tasks in active_subagents.items():
        for t in tasks:
            if not t.done():
                t.cancel()
                total += 1
        tasks.clear()
    if total:
        log.info("Cancelled %d subagent(s)", total)
        return f"Cancelled {total} running subagent(s)."
    return "No subagents running."

# --- Group ID ↔ name mappings (now via identity module) ---

# Scheduler instance (set in main(), used by schedule_reminder)
_scheduler = None

# Persistent one-time reminders
_reminders: list[dict] = []


def init_api_client() -> AsyncAnthropic:
    """
    Create the AsyncAnthropic client. Detects API key vs OAuth token.
    """
    token = load_credentials()
    is_api_key = token.startswith("sk-ant-api")
    if is_api_key:
        client = AsyncAnthropic(api_key=token)
        log.info("Anthropic API client initialized (API key)")
    else:
        client = AsyncAnthropic(
            auth_token=token,
            default_headers={
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,compact-2026-01-12,context-management-2025-06-27",
                "user-agent": "claude-code/2.1.76",
                "x-app": "cli",
            },
        )
        log.info("Anthropic API client initialized (OAuth)")
    return client


async def refresh_api_client() -> None:
    """Re-create the API client with a fresh token."""
    global api_client
    try:
        api_client = init_api_client()
    except RuntimeError as e:
        if "MANUAL_LOGIN_REQUIRED" in str(e):
            try:
                send_message(
                    CONFIG["authorized_senders"][0],
                    "🚨 **CRITICAL ALERT:** My master Claude API token is dead and auto-refresh failed. Please SSH in and run `claude login` before my scheduled tasks fail!",
                )
            except Exception as alert_err:
                log.error("Failed to send emergency alert: %s", alert_err)
            raise
        raise
    log.info("API client refreshed with new token")


def check_rate_limit(sender_id: str) -> bool:
    """
    Returns True if sender is within rate limit, False if exceeded.
    Uses a sliding window: max RATE_LIMIT_MAX messages per RATE_LIMIT_WINDOW seconds.
    """
    now = time.time()
    window = rate_limit_windows[sender_id]

    # Drop entries outside the window
    while window and now - window[0] > RATE_LIMIT_WINDOW:
        window.popleft()

    if len(window) >= RATE_LIMIT_MAX:
        log.warning("Rate limit exceeded for sender %s (%d msgs in %ds)",
                    sender_id, len(window), RATE_LIMIT_WINDOW)
        return False

    window.append(now)
    return True


def check_message_age(envelope: dict) -> bool:
    """
    Returns True if the message timestamp is recent enough.
    Signal timestamps are milliseconds since epoch.
    Rejects messages older than MAX_MESSAGE_AGE_SECONDS to prevent replay attacks.
    """
    timestamp_ms = envelope.get("timestamp")
    if timestamp_ms is None:
        # No timestamp — allow through (some signal-cli versions omit it)
        return True

    age_seconds = time.time() - (timestamp_ms / 1000)
    if age_seconds > MAX_MESSAGE_AGE_SECONDS:
        log.warning("Rejected stale message (age=%.0fs, max=%ds)", age_seconds, MAX_MESSAGE_AGE_SECONDS)
        return False

    return True


def get_sender_id(envelope: dict) -> str | None:
    """
    Extract a canonical sender ID from a signal-cli envelope.
    Returns phone number or "uuid:<uuid>" or None if unresolvable.
    """
    source = envelope.get("source")
    source_uuid = envelope.get("sourceUuid") or envelope.get("source_uuid")

    # Prefer phone number if present
    if source and source.startswith("+"):
        return source

    # Fall back to UUID
    if source_uuid:
        return f"uuid:{source_uuid}"

    return None


def get_group_id(envelope: dict) -> str | None:
    """
    Extract group ID from a Signal envelope, if present.
    signal-cli exposes groups via dataMessage.groupV2.id (newer) or
    dataMessage.groupInfo.groupId (older). Returns the base64 group ID or None.
    """
    data_message = envelope.get("dataMessage", {})
    # groupV2 (Signal v2 groups — most common now)
    group_v2 = data_message.get("groupV2") or {}
    if group_v2.get("id"):
        return group_v2["id"]
    # Legacy groupInfo
    group_info = data_message.get("groupInfo") or {}
    if group_info.get("groupId"):
        return group_info["groupId"]
    return None


def get_group_name(group_id: str | None) -> str:
    """
    Get a human-readable name for a group ID.
    Returns a short identifier for logging/display.
    """
    if not group_id:
        return "DM"
    return identity.resolve_group_name(group_id)


def get_feed_id(sender_id: str, group_id: str | None) -> str:
    """
    Derive a feed ID from sender/group.
    Groups → group name ("bots", "Alexandria").
    DMs → sender phone number ("+16142080533").
    """
    if group_id:
        return get_group_name(group_id)
    return sender_id



def _resolve_quote_author(phone: str) -> str:
    """Replace a phone number in quote context with a display name if possible."""
    if phone == CONFIG.get("bot_number"):
        return "cleo"
    return identity.resolve_name(phone)


# --- Feed tools (accessed by wake_loop) ---

def tool_check_feeds() -> str:
    """List all feeds, distinguishing unread from history."""
    unread_parts = []
    history_parts = []
    for fid in sorted(feeds):
        feed = feeds[fid]
        msgs = feed["messages"]
        if not msgs:
            continue
        last = msgs[-1]
        preview = f"last: {last.get('sender_name') or identity.resolve_name(last['sender'])} \"{last['text'][:50]}\""
        if fid in unread_feed_ids:
            unread_parts.append(f"- **{fid}** (NEW) — {preview}")
        else:
            history_parts.append(f"- {fid} ({len(msgs)} messages) — {preview}")

    parts = []
    if unread_parts:
        parts.append("Unread:\n" + "\n".join(unread_parts))
    if history_parts:
        parts.append("History:\n" + "\n".join(history_parts))
    return "\n\n".join(parts) if parts else "No feeds."


SIGNAL_ATTACHMENTS_DIR = os.path.expanduser("~/.local/share/signal-cli/attachments")
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB API limit
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _load_attachment_base64(att_id: str, content_type: str) -> dict | None:
    """Load a signal-cli attachment as a base64 image content block."""
    if content_type not in SUPPORTED_IMAGE_TYPES:
        return None
    path = os.path.join(SIGNAL_ATTACHMENTS_DIR, att_id)
    if not os.path.exists(path):
        return None
    if os.path.getsize(path) > MAX_IMAGE_SIZE:
        return None
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": content_type, "data": data},
    }


def tool_read_feed(feed_name: str, count: int = 10) -> str | list:
    """Read recent messages from a specific feed."""
    feed = feeds.get(feed_name)
    if not feed:
        return f"Unknown feed: {feed_name}. Available: {', '.join(sorted(feeds))}"
    msgs = feed["messages"][-count:]
    if not msgs:
        return f"No messages in {feed_name}."
    lines = []
    for m in msgs:
        display = m.get('sender_name') or identity.resolve_name(m['sender']); line = f"[{m['timestamp']}] (ts:{m.get('signal_ts', '?')}) {display}"
        if m.get("quote"):
            q = m["quote"]
            qa = q.get("author", "?")
            qt = q.get("text")
            if qt:
                qt_trunc = qt[:80] + "..." if len(qt) > 80 else qt
                line += f' (↩ replying to {qa}: "{qt_trunc}")'
            elif q.get("attachments"):
                line += f" (↩ replying to {qa}'s attachment)"
            else:
                line += f" (↩ replying to {qa})"
        line += f": {m['text']}"
        if m.get("attachments"):
            line += f" [📎 {len(m['attachments'])} image(s)]"
        if m.get("poll"):
            poll = m["poll"]
            if poll["type"] == "create":
                line += " [📊 POLL]"
            elif poll["type"] == "vote":
                line += f" [🗳️ VOTE on ts:{poll['target_timestamp']}]"
            elif poll["type"] == "terminate":
                line += f" [🔒 POLL CLOSED ts:{poll['target_timestamp']}]"
        lines.append(line)

    # Collect image blocks from recent messages
    image_blocks = []
    for m in msgs:
        for att in m.get("attachments", []):
            block = _load_attachment_base64(att["id"], att["content_type"])
            if block:
                image_blocks.append(block)

    text = "\n".join(lines)
    if not image_blocks:
        return text

    return [{"type": "text", "text": text}] + image_blocks


def route_send_message(recipient: str, message: str, attachment: str | None = None,
                       quote_timestamp: int | None = None, quote_author: str | None = None) -> str:
    """Send to a feed by name. Resolves group names to group_ids."""
    group_id = identity.resolve_group_id(recipient)
    if group_id:
        result = send_message(CONFIG["bot_number"], message, group_id=group_id, attachment=attachment,
                              quote_timestamp=quote_timestamp, quote_author=quote_author)
        feed_id = recipient  # already the group name
    else:
        result = send_message(recipient, message, attachment=attachment,
                              quote_timestamp=quote_timestamp, quote_author=quote_author)
        feed_id = recipient  # phone number

    # Record Cleo's own message in the feed so read_feed shows both sides
    if feed_id in feeds:
        feeds[feed_id]["messages"].append({
            "sender": "cleo",
            "text": message,
            "timestamp": datetime.now().strftime("%H:%M"),
        })
        _save_feeds()

    return result


def route_send_reaction(emoji: str, target_author: str, target_timestamp: int,
                        recipient: str) -> str:
    """React in a feed by name. Resolves group names to group_ids."""
    group_id = identity.resolve_group_id(recipient)
    if group_id:
        return send_reaction(emoji, target_author, target_timestamp,
                             CONFIG["bot_number"], group_id=group_id)
    return send_reaction(emoji, target_author, target_timestamp, recipient)


def route_send_poll(recipient: str, question: str, options: list[str],
                    allow_multiple: bool = True) -> str:
    """Create a poll in a feed by name. Resolves group names to group_ids."""
    group_id = identity.resolve_group_id(recipient)
    if group_id:
        return send_poll(CONFIG["bot_number"], question, options,
                         group_id=group_id, allow_multiple=allow_multiple)
    return send_poll(recipient, question, options, allow_multiple=allow_multiple)


def route_vote_poll(recipient: str, poll_author: str, poll_timestamp: int,
                    option_indexes: list[int], vote_count: int = 1) -> str:
    """Vote on a poll in a feed by name. Resolves group names to group_ids."""
    group_id = identity.resolve_group_id(recipient)
    if group_id:
        return vote_poll(CONFIG["bot_number"], poll_author, poll_timestamp,
                         option_indexes, vote_count, group_id=group_id)
    return vote_poll(recipient, poll_author, poll_timestamp,
                     option_indexes, vote_count)


# --- Wake tool definitions ---
# Extends TOOL_DEFINITIONS with feed tools and feed-aware send_message.

_WAKE_SEND_MESSAGE_DEF = {
    "name": "send_message",
    "description": (
        "Send a Signal message to a feed. Use a group name ('bots', 'Alexandria') "
        "for group messages, or a phone number (e.g. '+16142080533') for DMs. "
        "Supports Markdown formatting: **bold**, *italic*, _italic_, ~~strikethrough~~, ||spoiler||, `monospace`. "
        "To quote-reply to a specific message, provide quote_timestamp and quote_author "
        "from the read_feed output (the ts: value and sender field)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Feed to send to: group name ('bots', 'Alexandria') or phone number for DMs (e.g. '+16142080533').",
            },
            "message": {
                "type": "string",
                "description": "Message text to send. Use **bold**, *italic*, _italic_, ~~strikethrough~~, ||spoiler||, `monospace` for formatting.",
            },
            "attachment": {
                "type": "string",
                "description": "File path to an image to attach (e.g. from generate_image).",
            },
            "quote_timestamp": {
                "type": "integer",
                "description": "Signal timestamp (ms) of the message to quote — the ts: value from read_feed. Must be used with quote_author.",
            },
            "quote_author": {
                "type": "string",
                "description": "Phone number or UUID of the original message's author — the sender field from read_feed.",
            },
        },
        "required": ["recipient", "message"],
    },
}

_WAKE_SEND_REACTION_DEF = {
    "name": "send_reaction",
    "description": (
        "React to a Signal message with an emoji. Use the signal_ts from read_feed "
        "output to identify the target message. Use a group name ('bots', 'Alexandria') "
        "for group reactions, or a phone number for DM reactions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": "Single emoji to react with (e.g. 👍, ❤️, 😂).",
            },
            "target_author": {
                "type": "string",
                "description": "Phone number or UUID of the message author.",
            },
            "target_timestamp": {
                "type": "integer",
                "description": "Signal timestamp (ms) of the message — the ts: value from read_feed.",
            },
            "recipient": {
                "type": "string",
                "description": "Feed to react in: group name ('bots', 'Alexandria') or phone number for DMs.",
            },
        },
        "required": ["emoji", "target_author", "target_timestamp", "recipient"],
    },
}

_WAKE_SEND_POLL_DEF = {
    "name": "send_poll",
    "description": (
        "Create a Signal poll in a group or DM. Polls let people vote on options. "
        "Use a group name ('bots', 'Alexandria') for group polls, "
        "or a phone number for DMs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Feed to send poll to: group name or phone number for DMs.",
            },
            "question": {
                "type": "string",
                "description": "The poll question.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of poll options (minimum 2).",
            },
            "allow_multiple": {
                "type": "boolean",
                "description": "Whether voters can select multiple options (default: true).",
                "default": True,
            },
        },
        "required": ["recipient", "question", "options"],
    },
}

_WAKE_VOTE_POLL_DEF = {
    "name": "vote_poll",
    "description": (
        "Vote on an existing Signal poll. Requires the poll author's phone number "
        "and the poll's signal timestamp (ts: value from read_feed). "
        "option_indexes are 0-based indexes into the poll's options list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Feed the poll is in: group name or phone number.",
            },
            "poll_author": {
                "type": "string",
                "description": "Phone number or UUID of who created the poll.",
            },
            "poll_timestamp": {
                "type": "integer",
                "description": "Signal timestamp of the poll create message (ts: value from read_feed).",
            },
            "option_indexes": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "0-based indexes of options to vote for.",
            },
            "vote_count": {
                "type": "integer",
                "description": "Vote number (1 for first vote, increment for re-votes). Default: 1.",
                "default": 1,
            },
        },
        "required": ["recipient", "poll_author", "poll_timestamp", "option_indexes"],
    },
}

_FEED_TOOL_DEFINITIONS = [
    {
        "name": "check_feeds",
        "description": "List all message feeds with unread counts and a preview of the last message. Use this to see what's happening across all conversations.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_feed",
        "description": "Read recent messages from a specific feed. Returns the conversation with sender names and timestamps.",
        "input_schema": {
            "type": "object",
            "properties": {
                "feed": {
                    "type": "string",
                    "description": "Feed name: group name (e.g. 'bots', 'Alexandria') or phone number for DMs (e.g. '+16142080533').",
                },
                "count": {
                    "type": "integer",
                    "description": "Max messages to return (default: 50).",
                    "default": 10,
                },
            },
            "required": ["feed"],
        },
    },
    {
        "name": "restart_self",
        "description": "Gracefully restart yourself. Saves consciousness, feeds, and the wake log to daily memory before exiting. Systemd will restart the process automatically after ~10 seconds. Use this when you need to pick up code changes or reset your runtime state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why you're restarting (logged to daily memory).",
                },
            },
            "required": ["reason"],
        },
    },
]

# Build WAKE_TOOL_DEFINITIONS: all regular tools (with feed-aware overrides) + feed tools
WAKE_TOOL_DEFINITIONS = [
    _WAKE_SEND_MESSAGE_DEF if t["name"] == "send_message"
    else _WAKE_SEND_REACTION_DEF if t["name"] == "send_reaction"
    else _WAKE_SEND_POLL_DEF if t["name"] == "send_poll"
    else _WAKE_VOTE_POLL_DEF if t["name"] == "vote_poll"
    else t
    for t in TOOL_DEFINITIONS
] + _FEED_TOOL_DEFINITIONS

# Wake tool dispatch: feed tools + route_send_message, delegate others to execute_tool
WAKE_TOOL_DISPATCH = {
    "check_feeds": lambda args: tool_check_feeds(),
    "read_feed": lambda args: tool_read_feed(args["feed"], args.get("count", 10)),
    "send_message": lambda args: route_send_message(args["recipient"], args["message"], attachment=args.get("attachment"), quote_timestamp=args.get("quote_timestamp"), quote_author=args.get("quote_author")),
    "send_reaction": lambda args: route_send_reaction(args["emoji"], args["target_author"], args["target_timestamp"], args["recipient"]),
    "send_poll": lambda args: route_send_poll(args["recipient"], args["question"], args["options"], args.get("allow_multiple", True)),
    "vote_poll": lambda args: route_vote_poll(args["recipient"], args["poll_author"], args["poll_timestamp"], args["option_indexes"], args.get("vote_count", 1)),
    "schedule_reminder": lambda args: schedule_reminder(args["fire_at"], args["prompt"]),
}


def execute_wake_tool(name: str, args: dict) -> str | list:
    """Execute a tool in the wake context. Feed tools handled locally, others delegated."""
    handler = WAKE_TOOL_DISPATCH.get(name)
    if handler:
        log.info("Wake tool call: %s(%s)", name, ", ".join(f"{k}={v!r}" for k, v in args.items()))
        try:
            return handler(args)
        except Exception as e:
            log.exception("Wake tool %s failed", name)
            return f"Tool error: {e}"
    # Delegate to tools.py for everything else
    return execute_tool(name, args)


# --- History helpers ---

def _is_tool_result_message(msg: dict) -> bool:
    """Check if a message contains tool_result blocks (not plain user text)."""
    content = msg.get("content")
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in content
        )
    return False


def _ensure_valid_history(history: list[dict]) -> list[dict]:
    """
    Ensure conversation history is valid for the API.
    Removes leading orphaned tool_result or assistant messages,
    and trailing orphaned tool_use blocks (safety net).
    """
    # Clean leading orphans (but keep assistant messages with compaction blocks)
    while history:
        if history[0]["role"] == "user" and not _is_tool_result_message(history[0]):
            break
        if history[0]["role"] == "assistant":
            content = history[0].get("content", [])
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "compaction" for b in content
            ):
                break  # valid start — compaction summary
        history = history[1:]

    # Merge consecutive user messages (API rejects two user turns in a row)
    i = 0
    while i < len(history) - 1:
        if history[i]["role"] == "user" and history[i+1]["role"] == "user":
            # Merge second into first
            c1 = history[i]["content"] if isinstance(history[i]["content"], list) else [{"type": "text", "text": history[i]["content"]}]
            c2 = history[i+1]["content"] if isinstance(history[i+1]["content"], list) else [{"type": "text", "text": history[i+1]["content"]}]
            history[i]["content"] = c1 + c2
            history.pop(i+1)
        else:
            i += 1

    # Clean trailing orphaned tool_use (no matching tool_result follows)
    while history and history[-1]["role"] == "assistant":
        content = history[-1].get("content", [])
        blocks = content if isinstance(content, list) else []
        has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
        has_compaction = any(isinstance(b, dict) and b.get("type") == "compaction" for b in blocks)
        if has_tool_use and not has_compaction:
            history.pop()
        elif has_tool_use and has_compaction:
            # Compaction + orphaned tool_use in the same message: strip the
            # tool_use blocks but preserve the compaction summary.
            history[-1]["content"] = [
                b for b in blocks
                if not (isinstance(b, dict) and b.get("type") == "tool_use")
            ]
            break
        else:
            break

    return history



def _prune_pre_compaction(history: list[dict]) -> None:
    """After server-side compaction, prune local messages before the compaction point.

    The API ignores pre-compaction content anyway, but pruning locally keeps
    consciousness lean on disk and over the wire.
    """
    # Find the last message containing a compaction block
    compaction_idx = None
    for i in range(len(history) - 1, -1, -1):
        content = history[i].get("content", [])
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "compaction" for b in content
        ):
            compaction_idx = i
            break

    if compaction_idx is not None and compaction_idx > 0:
        pruned = compaction_idx
        del history[:compaction_idx]
        log.info("Pruned %d pre-compaction messages from local consciousness", pruned)


# --- Subagent loop (kept from original) ---

async def subagent_loop(sender_id: str, group_id: str | None,
                        task_description: str, task_context: str,
                        origin_feed: str = "unknown",
                        delegated_at: str = "",
                        model_override: str | None = None,
                        soul: str = "engineer") -> None:
    """
    Run an autonomous subagent with its own isolated conversation history.

    The subagent gets a task-specific system prompt, its own tools
    (no send_message, no delegate_task). On completion, injects the result
    as a feed event so Cleo can decide what to do with it.
    """
    global api_client
    task_id = f"task-{int(time.time())}"
    # Snapshot usage baseline for this task
    if task_id not in _usage.get("tasks", {}):
        _usage.setdefault("tasks", {})[task_id] = {
            "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
            "task_type": "subagent",
            "baseline_oauth_5h": float(_usage["oauth"].get("5h", 0.0)),
            "baseline_api_cost": float(_usage["api"].get("session_cost", 0.0)),
        }
        _save_usage()
    log.info("Subagent %s starting for %s in %s: %s",
             task_id, sender_id, get_group_name(group_id), task_description[:100])

    # Task-specific system prompt: load from subagent_souls/ directory
    souls_dir = os.path.join(CONFIG["workspace"], "subagent_souls")
    soul_path = os.path.join(souls_dir, f"{soul}.md")
    try:
        with open(soul_path) as f_soul:
            subagent_soul = f_soul.read().strip()
    except FileNotFoundError:
        log.warning("Subagent soul '%s' not found at %s, falling back to engineer", soul, soul_path)
        try:
            with open(os.path.join(souls_dir, "engineer.md")) as f_soul:
                subagent_soul = f_soul.read().strip()
        except FileNotFoundError:
            subagent_soul = (
                "You are an expert software engineer executing a delegated task. "
                "Ship working code. Validate with exec_command before declaring done. "
                "Debug failures — don't bail. Be concise in your final summary."
            )
    log.info("Subagent %s using soul: %s", task_id, soul)
    now = datetime.now()
    system_prompt_text = (
        f"**Now:** {now.strftime('%A, %Y-%m-%d, at %-I:%M %p ET')}\n\n"
        f"{subagent_soul}\n\n---\n\n"
        f"## Task\n\n{task_description}"
    )
    if task_context:
        system_prompt_text += f"\n\n## Additional Context\n\n{task_context}"

    # Isolated conversation history
    first_msg = f"Execute this task:\n\n{task_description}"
    if task_context:
        first_msg += f"\n\nContext:\n{task_context}"
    history = [{"role": "user", "content": first_msg}]

    # Build system as list with billing header (required for OAuth)
    system_prompt = [
        {"type": "text", "text": _compute_billing_header(first_msg)},
        {"type": "text", "text": system_prompt_text},
    ]

    model = model_override or CONFIG.get("model", "claude-sonnet-4-6")
    final_summary = None

    try:
        for iteration in range(1, MAX_SUBAGENT_ITERATIONS + 1):
            try:
                _raw = await api_client.messages.with_raw_response.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    messages=history,
                    tools=SUBAGENT_TOOL_DEFINITIONS,
                )
                response = _raw.parse()
                _update_rate_limits(_raw.headers)
            except Exception as e:
                err = str(e).lower()
                if "401" in str(e) or "403" in str(e) or "permission" in err or "revoked" in err:
                    await refresh_api_client()
                    _raw = await api_client.messages.with_raw_response.create(
                        model=model,
                        max_tokens=MAX_TOKENS,
                        system=system_prompt,
                        messages=history,
                        tools=SUBAGENT_TOOL_DEFINITIONS,
                    )
                    response = _raw.parse()
                    _update_rate_limits(_raw.headers)
                else:
                    raise

            _sa_in = (response.usage.input_tokens or 0) + (response.usage.cache_creation_input_tokens or 0) + (response.usage.cache_read_input_tokens or 0)
            _sa_out = response.usage.output_tokens or 0
            log.info("Subagent %s iter %d: stop=%s, in=%d out=%d",
                     task_id, iteration, response.stop_reason, _sa_in, _sa_out)
            _update_usage(_sa_in, _sa_out, task_id=task_id)
            # Check delta limits after each iteration
            _violation = _check_task_limits(task_id)
            if _violation:
                log.warning("Subagent %s killed: %s", task_id, _violation["msg"])
                # Clean up task entry
                _usage.get("tasks", {}).pop(task_id, None)
                _save_usage()
                # Notify Billy via feed injection
                _kill_msg = f"🚨 subagent {task_id} killed — {_violation['msg']}"
                feeds[f"system:kill:{task_id}"] = {"group_id": None, "messages": [{"sender": "system", "text": _kill_msg, "timestamp": datetime.now().strftime("%H:%M")}]}
                feeds[f"system:kill:{task_id}"]["unread_count"] = 1
                unread_feed_ids.add(f"system:kill:{task_id}")
                _save_feeds()
                return

            # Build assistant message
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use", "id": block.id,
                        "name": block.name, "input": block.input,
                    })
            history.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    log.info("Subagent %s tool: %s", task_id, block.name)
                    result = execute_tool(block.name, dict(block.input))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                history.append({"role": "user", "content": tool_results})
                continue

            # end_turn, max_tokens, or anything else — extract text and stop
            text_parts = [b.text for b in response.content if b.type == "text" and b.text]
            final_summary = "\n".join(text_parts) if text_parts else "Task completed (no output)."
            break

        if final_summary is None:
            final_summary = "Subagent hit iteration limit without producing a final answer."

    except Exception as e:
        log.exception("Subagent %s failed", task_id)
        final_summary = f"Background task error: {e}"

    # Inject result as a feed event — Cleo decides what to do with it
    compact_summary = final_summary[:4000] if len(final_summary) > 4000 else final_summary
    result_feed_id = f"subagent:{task_id}"
    feeds[result_feed_id] = {"group_id": None, "messages": [], "unread_count": 1}
    feeds[result_feed_id]["messages"].append({
        "sender": "system",
        "text": (
            f"Background task completed.\n"
            f"Task: {task_description}\n"
            f"Origin: {origin_feed} at {delegated_at}\n"
            f"Result:\n{compact_summary}"
        ),
        "timestamp": datetime.now().strftime("%H:%M"),
    })
    # Only wake Match for subagent results that came from user messages,
    # not scheduled events (dream, heartbeat) — those subagents already
    # messaged Billy directly; a second wake just produces a redundant reply.
    if not origin_feed.startswith("scheduled:"):
        unread_feed_ids.add(result_feed_id)
    _save_feeds()

    # Log to daily memory (rich detail for dream consolidation)
    timestamp = datetime.now().strftime("%H:%M")
    entry = (
        f"## [{timestamp}] Subagent completed: {task_id}\n\n"
        f"Task: {task_description}\n"
        f"Origin: {origin_feed} at {delegated_at}\n\n"
        f"### Result\n\n{compact_summary}"
    )
    append_daily_memory(CONFIG["workspace"], entry)

    log.info("Subagent %s finished, injected as feed %s", task_id, result_feed_id)
    # Clean up completed task from usage tracking
    if task_id in _usage.get("tasks", {}):
        _usage["tasks"].pop(task_id)
        _save_usage()

    # Wake immediately so Cleo can process the result
    async with consciousness_lock:
        if unread_feed_ids:
            await wake_loop()


# --- Debounce mechanism ---

def schedule_wake(debounce: float = WAKE_DEBOUNCE_GROUP) -> None:
    """
    Schedule a wake-up after `debounce` seconds of quiet.
    If an existing wake is pending with a later deadline, reschedule sooner.
    If the existing wake fires sooner, keep it.
    """
    global _wake_task, _wake_deadline
    import time as _time
    new_deadline = _time.monotonic() + debounce

    if _wake_task and not _wake_task.done():
        if new_deadline >= _wake_deadline:
            return  # existing wake fires sooner, keep it
        _wake_task.cancel()

    _wake_deadline = new_deadline
    _wake_task = asyncio.create_task(_debounced_wake(debounce))


async def _debounced_wake(debounce: float) -> None:
    """Wait for debounce period, then wake Cleo if there are unread feeds."""
    try:
        await asyncio.sleep(debounce)
    except asyncio.CancelledError:
        return  # Debounce was reset by a shorter deadline

    async with consciousness_lock:
        if not unread_feed_ids:
            return
        await wake_loop()

    # Check if new messages arrived during processing
    if unread_feed_ids:
        schedule_wake()


# --- Daily log helper ---

def _write_wake_log(trigger_summary: str, wake_log: list[str]) -> None:
    """Write a comprehensive wake cycle entry to today's daily log."""
    timestamp = datetime.now().strftime("%H:%M")
    entry = f"## [{timestamp}] Wake cycle\n\nTrigger:\n{trigger_summary}"
    if wake_log:
        entry += "\n\n" + "\n\n".join(wake_log)
    append_daily_memory(CONFIG["workspace"], entry)


def _trim_feeds(processed_ids: set[str]) -> None:
    """Trim processed feeds to MAX_FEED_MESSAGES, reset unread counts, and persist."""
    for fid in processed_ids:
        if fid in feeds:
            if len(feeds[fid]["messages"]) > MAX_FEED_MESSAGES:
                feeds[fid]["messages"] = feeds[fid]["messages"][-MAX_FEED_MESSAGES:]
            feeds[fid]["unread_count"] = 0
    # Purge stale one-shot system threads
    stale = [fid for fid in list(feeds.keys())
             if any(fid.startswith(p) for p in ("subagent:", "scheduled:"))
             and feeds[fid].get("unread_count", 0) == 0]
    for fid in stale:
        del feeds[fid]
    _save_feeds()


# --- Wake loop (replaces agentic_loop) ---

async def wake_loop() -> None:
    """
    Process unread feeds. Single consciousness, persistent history.

    1. Build summary of which feeds have new messages
    2. Call Claude API with feed tools
    3. Cleo reads feeds, decides what to respond to, sends messages explicitly
    4. On end_turn: log thoughts, clear feeds, compact history
    """
    global api_client, _last_input_tokens

    # Build feed summary
    summary_parts = []
    for fid in sorted(unread_feed_ids):
        feed = feeds.get(fid)
        if not feed or not feed["messages"]:
            continue
        count = feed.get("unread_count", len(feed["messages"]))
        last_msg = feed["messages"][-1]
        summary_parts.append(
            f"- **{fid}** ({count} new) — last: {last_msg['sender']} \"{last_msg['text'][:60]}\"" + (" [📎 image]" if last_msg.get("attachments") else "")
        )

    if not summary_parts:
        return

    summary = "New messages:\n" + "\n".join(summary_parts)
    log.info("Waking Cleo: %d feeds with unread messages", len(summary_parts))

    # Build system prompt: static block (cached) + dynamic block (uncached).
    # Splitting keeps the stable identity content in the KV cache across wakes
    # while the timestamp (and any per-request context) stays out of the cache.
    static_prompt = build_static_prompt(CONFIG["workspace"])
    dynamic_prompt = build_dynamic_context()

    # Snapshot which feeds we're processing (new messages during processing go to next cycle)
    processing_feed_ids = set(unread_feed_ids)
    unread_feed_ids.clear()

    # Append wake-up summary to consciousness
    consciousness.append({"role": "user", "content": [{"type": "text", "text": summary}]})

    # Pre-emptive feed read: inject as if Cleo already called read_feed
    # This saves 2+ API round trips (check_feeds + read_feed per feed)
    prefetch_assistant = []
    prefetch_results = []
    for i, fid in enumerate(sorted(processing_feed_ids)):
        feed = feeds.get(fid)
        if not feed or not feed["messages"]:
            continue
        unread = feed.get("unread_count", len(feed["messages"]))
        read_count = min(unread + 2, len(feed["messages"]))
        tool_id = f"prefetch_{i}"
        prefetch_assistant.append({
            "type": "tool_use", "id": tool_id,
            "name": "read_feed", "input": {"feed": fid, "count": read_count},
        })
        result = tool_read_feed(fid, read_count)
        prefetch_results.append({
            "type": "tool_result", "tool_use_id": tool_id,
            "content": result,
        })

    if prefetch_assistant:
        consciousness.append({"role": "assistant", "content": prefetch_assistant})
        consciousness.append({"role": "user", "content": prefetch_results})

    model = CONFIG.get("model", "claude-sonnet-4-6")
    iteration = 0
    wake_log: list[str] = []  # Accumulate rich log for daily memory

    # Prepare system blocks and tools (saves tokens on repeat calls).
    # Block 1 (cached): stable identity — SOUL, USER, MEMORY files.
    # Block 2 (not cached): volatile context — timestamp, changes every wake.
    # Extract first user message text for billing header
    _first_user_text = ""
    for _m in consciousness:
        if _m.get("role") == "user":
            _c = _m.get("content", "")
            if isinstance(_c, str):
                _first_user_text = _c
            elif isinstance(_c, list):
                for _b in _c:
                    if isinstance(_b, dict) and _b.get("type") == "text":
                        _first_user_text = _b.get("text", "")
                        break
            break
    # Enrich dynamic prompt with semantically relevant memories
    try:
        from vectorstore import query_memories, format_retrieved_context
        if _first_user_text:
            _vstore_results = query_memories(_first_user_text, n_results=5)
            if _vstore_results:
                _vstore_context = format_retrieved_context(_vstore_results)
                dynamic_prompt += "\n\n---\n\n## Retrieved Memories\n\n" + _vstore_context
                log.info("Vector enrichment: %d chunks injected", len(_vstore_results))
    except Exception as _ve:
        log.warning("Vector enrichment failed: %s", _ve)
    system = [
        {"type": "text", "text": _compute_billing_header(_first_user_text)},
        {"type": "text", "text": static_prompt, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic_prompt},
    ]
    # Inject cancel_tasks tool dynamically
    CANCEL_TOOL = {
        "name": "cancel_tasks",
        "description": "Cancel all running background subagent tasks. Use when Billy says stop, cancel, or abort.",
        "input_schema": {"type": "object", "properties": {}},
    }
    CHECK_USAGE_TOOL = {
        "name": "check_usage",
        "description": "Check current session usage — OAuth 5h/7d utilization and API cost. Use before delegating expensive tasks, or when Billy asks about usage/cost/juice. Returns current vs limits and warns if close to threshold.",
        "input_schema": {"type": "object", "properties": {}},
    }
    SET_LIMIT_TOOL = {
        "name": "set_usage_limit",
        "description": "Set a usage limit for subagents and tasks. Use when Billy says things like 'set api limit to $2' or 'set oauth limit to 20%'. type must be 'oauth' or 'api'. value is a number — dollars for api (e.g. 2.0), percentage points for oauth (e.g. 20 means 20%). Takes effect immediately on all running and future tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["oauth", "api"], "description": "Which limit to set"},
                "value": {"type": "number", "description": "Dollars for api, percentage points for oauth (e.g. 20 = 20%)"}
            },
            "required": ["type", "value"]
        },
    }
    START_LOOPER_TOOL = {
        "name": "start_looper",
        "description": "Launch a game design looper session in the background. Use when Billy asks to start/run a looper or game design session. Runs preflight checks automatically before launch. Always use this instead of exec_command for looper runs. If Billy specifies a budget (e.g. 'api $2'), pass it as api_delta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "game": {"type": "string", "description": "Game name (e.g. backprop)"},
                "loops": {"type": "integer", "description": "Number of loops to run (default 1)", "default": 1},
                "note": {"type": "string", "description": "Optional note to inject into the session"},
                "api_delta": {"type": "number", "description": "API budget limit in dollars (e.g. 2.0). Sets the kill threshold for this session."}
            },
            "required": ["game"]
        },
    }
    STOP_LOOPER_TOOL = {
        "name": "stop_looper",
        "description": "Stop a running looper session by killing its process. Use when Billy asks to stop/cancel/kill the looper.",
        "input_schema": {
            "type": "object",
            "properties": {
                "game": {"type": "string", "description": "Game name (e.g. backprop)"}
            },
            "required": ["game"]
        },
    }
    tools = list(WAKE_TOOL_DEFINITIONS) + [CANCEL_TOOL, CHECK_USAGE_TOOL, SET_LIMIT_TOOL, START_LOOPER_TOOL, STOP_LOOPER_TOOL]
    if tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}

    context_mgmt = {
        "edits": [
            {
                "type": "compact_20260112",
                "trigger": {"type": "input_tokens", "value": 90000},
            },
            {
                "type": "clear_tool_uses_20250919",
                "trigger": {"type": "input_tokens", "value": 60000},
                "keep": {"type": "tool_uses", "value": 5},
                "clear_at_least": {"type": "input_tokens", "value": 15000},
            },
        ]
    }

    while iteration < MAX_AGENTIC_ITERATIONS:
        iteration += 1

        # Ensure history is valid
        valid = _ensure_valid_history(consciousness)
        if valid is not consciousness:
            consciousness.clear()
            consciousness.extend(valid)

        # Strip image blocks from all but the last user message to keep
        # payload under the API size limit.  Images only need to be "seen"
        # once; afterwards a text placeholder is sufficient.
        last_user_idx = None
        for idx in range(len(consciousness) - 1, -1, -1):
            if isinstance(consciousness[idx], dict) and consciousness[idx].get("role") == "user":
                last_user_idx = idx
                break
        for idx, msg in enumerate(consciousness):
            if idx == last_user_idx:
                continue
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                # Top-level image block
                if block.get("type") == "image":
                    content[bi] = {"type": "text", "text": "[image previously viewed]"}
                # Image inside tool_result
                if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                    block["content"] = [
                        {"type": "text", "text": "[image previously viewed]"}
                        if (isinstance(b, dict) and b.get("type") == "image")
                        else b
                        for b in block["content"]
                    ]

        # Add cache_control to the last user message to cache the history.
        # First strip any existing cache_control from all user messages — the API
        # allows at most 4 cache_control blocks total (system + tools + compaction
        # + last user), and prior iterations will have tagged earlier messages.
        for msg in consciousness:
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        for msg in reversed(consciousness):
            if msg["role"] == "user":
                if isinstance(msg["content"], str):
                    msg["content"] = [{"type": "text", "text": msg["content"], "cache_control": {"type": "ephemeral"}}]
                elif isinstance(msg["content"], list) and msg["content"]:
                    msg["content"][-1]["cache_control"] = {"type": "ephemeral"}
                break

        _dump_context(model, system, tools, context_mgmt)

        try:
            _raw = await api_client.messages.with_raw_response.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=consciousness,
                tools=tools,
                extra_body={"context_management": context_mgmt},
            )
            response = _raw.parse()
            _update_rate_limits(_raw.headers)
        except Exception as e:
            error_str = str(e).lower()
            # On auth error (401, 403 revoked), try refreshing token once
            if "401" in str(e) or "403" in str(e) or "permission" in error_str or "revoked" in error_str:
                log.warning("API auth error, refreshing token: %s", e)
                try:
                    await refresh_api_client()
                    _raw = await api_client.messages.with_raw_response.create(
                        model=model,
                        max_tokens=MAX_TOKENS,
                        system=system,
                        messages=consciousness,
                        tools=tools,
                        extra_body={"context_management": context_mgmt},
                    )
                    response = _raw.parse()
                    _update_rate_limits(_raw.headers)
                except Exception as retry_err:
                    log.exception("API call failed after token refresh")
                    _save_consciousness()
                    return
            else:
                log.exception("API call failed")
                _save_consciousness()
                return

        # Token logging — account for compaction iterations if present
        iterations = getattr(response.usage, 'iterations', None)
        if iterations:
            def _iter_tokens(it, key):
                return it[key] if isinstance(it, dict) else getattr(it, key)
            total_in = (response.usage.input_tokens or 0) + (response.usage.cache_creation_input_tokens or 0) + (response.usage.cache_read_input_tokens or 0)
            total_out = sum(_iter_tokens(it, 'output_tokens') for it in iterations)
            log.info("Wake API: stop=%s, %d blocks, %d+%d tokens (%d iterations)",
                     response.stop_reason, len(response.content), total_in, total_out,
                     len(iterations))
            _last_input_tokens = total_in
            _update_usage(total_in, total_out)
        else:
            log.info("Wake API response: stop_reason=%s, %d blocks, in=%d out=%d tokens",
                     response.stop_reason, len(response.content),
                     (response.usage.input_tokens or 0) + (response.usage.cache_creation_input_tokens or 0) + (response.usage.cache_read_input_tokens or 0), response.usage.output_tokens)
            _last_input_tokens = (response.usage.input_tokens or 0) + (response.usage.cache_creation_input_tokens or 0) + (response.usage.cache_read_input_tokens or 0)
            _update_usage(_last_input_tokens, response.usage.output_tokens or 0)

        # Log context management edits if any were applied
        ctx_mgmt_resp = getattr(response, 'context_management', None)
        if ctx_mgmt_resp:
            applied = ctx_mgmt_resp.get('applied_edits') if isinstance(ctx_mgmt_resp, dict) else getattr(ctx_mgmt_resp, 'applied_edits', None)
            if applied:
                for edit in applied:
                    log.info("Context edit applied: %s", edit)

        # Build assistant message for history
        assistant_content = []
        has_compaction = False
        for block in response.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
            elif block.type == "compaction":
                has_compaction = True
                assistant_content.append({
                    "type": "compaction",
                    "content": block.content,
                    "cache_control": {"type": "ephemeral"},
                })

        consciousness.append({"role": "assistant", "content": assistant_content})

        # If server-side compaction occurred, prune old messages locally
        if has_compaction:
            _prune_pre_compaction(consciousness)
            _save_consciousness()

        # Capture reasoning for daily log
        for block in response.content:
            if block.type == "text" and block.text:
                wake_log.append(f"### Cleo's reasoning\n\n{block.text}")

        if response.stop_reason == "tool_use":
            # Execute each tool and collect results
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "cancel_tasks":
                    result_text = cancel_all_subagents()
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
                    wake_log.append(f"### Tool: cancel_tasks\nResult: {result_text}")
                    continue
                if block.name == "check_usage":
                    violation = _check_limits()
                    oauth_5h = float(_usage["oauth"].get("5h", 0.0))
                    oauth_7d = float(_usage["oauth"].get("7d", 0.0))
                    api_cost = _usage["api"].get("session_cost", 0.0)
                    tokens_in = _usage["api"].get("session_tokens_in", 0)
                    tokens_out = _usage["api"].get("session_tokens_out", 0)
                    oauth_delta = _usage["limits"].get("oauth_delta", 0.15)
                    api_delta = _usage["limits"].get("api_delta", 1.00)
                    # Build per-task summary
                    task_summary = {}
                    for tid, tdata in _usage.get("tasks", {}).items():
                        b_oauth = float(tdata.get("baseline_oauth_5h", 0.0))
                        b_api = float(tdata.get("baseline_api_cost", 0.0))
                        task_summary[tid] = {
                            "oauth_used": f"{(oauth_5h - b_oauth)*100:.1f}% of {oauth_delta*100:.0f}% limit",
                            "api_spent": f"${api_cost - b_api:.4f} of ${api_delta:.2f} limit",
                        }
                    usage_report = {
                        "oauth_5h": f"{oauth_5h*100:.1f}%",
                        "oauth_7d": f"{oauth_7d*100:.1f}%",
                        "oauth_delta_limit": f"{oauth_delta*100:.0f}% per task",
                        "api_cost_total": f"${api_cost:.4f}",
                        "api_delta_limit": f"${api_delta:.2f} per task",
                        "api_tokens": f"in={tokens_in:,} out={tokens_out:,}",
                        "active_tasks": task_summary,
                        "violation": violation,
                    }
                    if violation:
                        cancel_all_subagents()
                        usage_report["action"] = f"LIMIT EXCEEDED — all subagents cancelled: {violation['msg']}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(usage_report),
                    })
                    wake_log.append(f"### Tool: check_usage\n{json.dumps(usage_report, indent=2)}")
                    continue
                if block.name == "set_usage_limit":
                    _limit_type = block.input.get("type")
                    _limit_value = float(block.input.get("value", 0))
                    if _limit_type == "oauth":
                        _usage["limits"]["oauth_delta"] = round(_limit_value / 100.0, 4)
                        _confirm = f"oauth delta limit set to {_limit_value:.0f}% — subagents will be killed if they consume more than {_limit_value:.0f}% of the 5h session window from their start point"
                    elif _limit_type == "api":
                        _usage["limits"]["api_delta"] = round(_limit_value, 4)
                        _confirm = f"api delta limit set to ${_limit_value:.2f} — subagents will be killed if they spend more than ${_limit_value:.2f} from their start point"
                    else:
                        _confirm = f"unknown limit type: {_limit_type}"
                    _save_usage()
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _confirm,
                    })
                    wake_log.append(f"### Tool: set_usage_limit\nResult: {_confirm}")
                    continue
                if block.name == "start_looper":
                    _game = block.input.get("game", "")
                    _loops = block.input.get("loops", 1)
                    _note = block.input.get("note", "")
                    _api_delta = block.input.get("api_delta", None)
                    # Set api_delta if specified (e.g. Billy says "api $2")
                    if _api_delta is not None:
                        _usage.setdefault("limits", {})["api_delta"] = float(_api_delta)
                        _save_usage()
                        log.info("start_looper: api_delta set to $%.2f", float(_api_delta))
                    # Clean stale looper entry if PID is dead
                    _stale = _usage.get("loopers", {}).get(_game)
                    if _stale and _stale.get("pid"):
                        try:
                            os.kill(int(_stale["pid"]), 0)
                        except (ProcessLookupError, ValueError):
                            log.info("start_looper: clearing stale entry for %s (PID %s dead)", _game, _stale["pid"])
                            _usage.get("loopers", {}).pop(_game, None)
                            _save_usage()
                    # Run preflight check via game_design_session.py
                    import subprocess as _sp
                    _preflight_result = _sp.run(
                        ["/home/billy/cleo/venv/bin/python", "-c",
                         f"import sys; sys.path.insert(0, '/home/billy'); "
                         f"from game_design_session import preflight_check; "
                         f"actions = preflight_check('{_game}'); "
                         f"print('\n'.join(actions))"],
                        capture_output=True, text=True
                    )
                    _preflight_notes = _preflight_result.stdout.strip()
                    if _preflight_notes and _preflight_notes != "all clear":
                        log.info("start_looper preflight for %s: %s", _game, _preflight_notes)
                    # Find next session number
                    import glob as _glob
                    _game_dir = os.path.expanduser(f"~/game-sessions/{_game}")
                    os.makedirs(_game_dir, exist_ok=True)
                    _existing = _glob.glob(f"{_game_dir}/session_*_transcript.md")
                    import re as _re
                    _nums = [int(_re.search(r"session_(\d+)_transcript", f).group(1)) for f in _existing if _re.search(r"session_(\d+)_transcript", f)]
                    _sess_num = max(_nums) + 1 if _nums else 1
                    _log_file = f"{_game_dir}/session_{_sess_num:03d}_looper.log"
                    _note_arg = f'--note "{_note}"' if _note else ""
                    _cmd = f"nohup /home/billy/cleo/venv/bin/python ~/game_design_session.py --game {_game} --loops {_loops} {_note_arg} > {_log_file} 2>&1 & echo $!"
                    _result = _sp.run(_cmd, shell=True, capture_output=True, text=True)
                    _pid = _result.stdout.strip()
                    # Store PID in usage.json
                    _usage.setdefault("loopers", {})[_game] = {"pid": _pid, "log": _log_file, "loops": _loops}
                    _save_usage()
                    _preflight_summary = f"\npreflight: {_preflight_notes}" if _preflight_notes and _preflight_notes != "all clear" else ""
                    _looper_reply = f"looper started for {_game} ({_loops} loop(s)) — PID {_pid}{_preflight_summary}\nwatch: tail -f {_log_file}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _looper_reply,
                    })
                    wake_log.append(f"### Tool: start_looper\n{_looper_reply}")
                    continue
                if block.name == "stop_looper":
                    _game = block.input.get("game", "")
                    _looper_info = _usage.get("loopers", {}).get(_game)
                    if _looper_info and _looper_info.get("pid"):
                        _pid = _looper_info["pid"]
                        import subprocess as _sp
                        _sp.run(f"kill {_pid} 2>/dev/null || true", shell=True)
                        _usage.get("loopers", {}).pop(_game, None)
                        _save_usage()
                        _stop_reply = f"looper {_game} (PID {_pid}) killed"
                    else:
                        _stop_reply = f"no running looper found for {_game}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _stop_reply,
                    })
                    wake_log.append(f"### Tool: stop_looper\n{_stop_reply}")
                    continue
                if block.name == "delegate_task":
                    # Special case: launch subagent in background
                    task_args = dict(block.input)
                    task_desc = task_args["task"]
                    task_ctx = task_args.get("context", "")
                    sa_model = task_args.get("model")
                    sa_soul = task_args.get("soul", "engineer")

                    # Origin context for when the subagent reports back
                    origin_feed = next(iter(processing_feed_ids), "unknown")
                    delegated_at = datetime.now().strftime("%H:%M")

                    # Determine sender/group context for the subagent's system prompt
                    if origin_feed != "unknown":
                        sa_group_id = identity.resolve_group_id(origin_feed)
                        sa_sender_id = CONFIG["authorized_senders"][0] if sa_group_id else origin_feed
                    else:
                        sa_sender_id = CONFIG["authorized_senders"][0]
                        sa_group_id = None

                    # Clean up finished subagents, check limit
                    sa_key = "global"
                    active_subagents[sa_key] = [
                        t for t in active_subagents[sa_key] if not t.done()
                    ]
                    if len(active_subagents[sa_key]) >= MAX_CONCURRENT_SUBAGENTS:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Cannot delegate: {MAX_CONCURRENT_SUBAGENTS} subagents already running. Wait for one to finish.",
                        })
                    else:
                        bg_task = asyncio.create_task(
                            subagent_loop(sa_sender_id, sa_group_id, task_desc, task_ctx,
                                          origin_feed=origin_feed, delegated_at=delegated_at,
                                          model_override=sa_model, soul=sa_soul)
                        )
                        active_subagents[sa_key].append(bg_task)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Task delegated to background agent. It will send results when complete.",
                        })
                        wake_log.append(f"### Tool: delegate_task\n\nDelegated: {task_desc[:200]}")
                        log.info("Delegated task: %s", task_desc[:80])
                elif block.name == "restart_self":
                    # Special case: graceful restart
                    reason = dict(block.input).get("reason", "no reason given")
                    log.info("Graceful restart requested: %s", reason)

                    # Append tool result so consciousness is complete
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"Restarting. Reason: {reason}",
                    })
                    consciousness.append({"role": "user", "content": tool_results})

                    wake_log.append(f"### Tool: restart_self\n\nReason: {reason}")
                    _write_wake_log(summary, wake_log)
                    _trim_feeds(processing_feed_ids)
                    _save_consciousness()

                    log.info("State saved, exiting for systemd restart")
                    sys.exit(1)

                else:
                    log.info("Executing wake tool: %s", block.name)
                    result = execute_wake_tool(block.name, dict(block.input))

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                    # For logging, extract text portion (result may be str or list of content blocks)
                    if isinstance(result, list):
                        log_text = next((b["text"] for b in result if b.get("type") == "text"), "")
                        n_images = sum(1 for b in result if b.get("type") == "image")
                        log_text = f"{log_text[:2000]} [+{n_images} image(s)]"
                    else:
                        log_text = result[:2000]
                    wake_log.append(
                        f"### Tool: {block.name}\n\n"
                        f"Input: {json.dumps(dict(block.input), indent=2)}\n\n"
                        f"Result: {log_text}"
                    )

            # Append tool results as a user message (API requirement)
            consciousness.append({"role": "user", "content": tool_results})

            # Loop: call API again with tool results
            continue

        elif response.stop_reason == "end_turn":
            # Cleo is done thinking. Text is internal — not sent anywhere.
            text_parts = [b.text for b in response.content if b.type == "text" and b.text]
            if text_parts:
                log.info("Cleo's thoughts: %s", "\n".join(text_parts)[:300])

            # Write comprehensive wake cycle to daily log
            _write_wake_log(summary, wake_log)

            # Trim feeds and persist
            _trim_feeds(processing_feed_ids)
            _save_consciousness()
            return

        elif response.stop_reason == "max_tokens":
            text_parts = [b.text for b in response.content if b.type == "text" and b.text]
            if text_parts:
                log.info("Cleo's thoughts (truncated): %s", "\n".join(text_parts)[:300])
            log.warning("max_tokens reached in wake loop")

            # If the truncated response contains tool_use blocks, inject
            # error tool_results so Cleo can retry (e.g. in smaller chunks)
            # rather than orphaning the conversation.
            truncated_tool_ids = [
                b.id for b in response.content if b.type == "tool_use"
            ]
            if truncated_tool_ids:
                tool_results = []
                for tid in truncated_tool_ids:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": "Error: your response was truncated (max_tokens). Your tool call was cut off and not executed. Try again with smaller output — for write_file, split into multiple smaller writes.",
                        "is_error": True,
                    })
                consciousness.append({"role": "user", "content": tool_results})
                wake_log.append("### [max_tokens — tool calls truncated, retrying]")
                continue  # let the loop continue so Cleo can retry

            wake_log.append("### [max_tokens reached — response truncated]")
            _write_wake_log(summary, wake_log)
            _trim_feeds(processing_feed_ids)
            _save_consciousness()
            return

        else:
            log.warning("Unexpected stop_reason=%s in wake loop", response.stop_reason)
            wake_log.append(f"### [unexpected stop_reason: {response.stop_reason}]")
            _write_wake_log(summary, wake_log)
            _trim_feeds(processing_feed_ids)
            _save_consciousness()
            return

    # Safety: hit MAX_AGENTIC_ITERATIONS
    log.error("Wake loop hit iteration limit (%d)", MAX_AGENTIC_ITERATIONS)
    wake_log.append(f"### [hit iteration limit: {MAX_AGENTIC_ITERATIONS}]")
    _write_wake_log(summary, wake_log)
    _trim_feeds(processing_feed_ids)
    _save_consciousness()


# --- Message handling ---

async def handle_signal_message(envelope: dict) -> None:
    """
    Process a single incoming Signal message.
    Buffers into the appropriate feed and triggers a debounced wake-up.
    Does NOT call the API directly — that happens in wake_loop.
    """
    sender_id = get_sender_id(envelope)
    if not sender_id:
        log.debug("Could not determine sender, ignoring message")
        return

    # Ignore own messages (signal-cli echoes messages the bot sends to groups)
    if sender_id == CONFIG.get("bot_number"):
        return

    group_id = get_group_id(envelope)

    if not is_authorized(sender_id, group_id, CONFIG):
        log.warning("UNAUTHORIZED sender: %s", sender_id)
        return

    if not check_message_age(envelope):
        return

    if not check_rate_limit(sender_id):
        return

    data_message = envelope.get("dataMessage", {})
    if not data_message:
        return

    # Check for emoji reaction before text message
    reaction = data_message.get("reaction")
    if reaction:
        emoji = reaction.get("emoji", "?")
        target_author = reaction.get("targetAuthor", "unknown")
        target_ts = reaction.get("targetTimestamp")
        is_remove = reaction.get("isRemove", False)
        action = "removed" if is_remove else "reacted"

        feed_id = get_feed_id(sender_id, group_id)
        if feed_id not in feeds:
            feeds[feed_id] = {"group_id": group_id, "messages": []}
        feeds[feed_id]["messages"].append({
            "sender": sender_id,
            "text": f"{action} {emoji} to message from {target_author} (ts:{target_ts})",
            "timestamp": datetime.now().strftime("%H:%M"),
            "signal_ts": envelope.get("timestamp"),
            "is_reaction": True,
        })
        unread_feed_ids.add(feed_id)
        _save_feeds()
        log.info("Reaction from %s in %s: %s %s", sender_id, get_group_name(group_id), action, emoji)
        schedule_wake(WAKE_DEBOUNCE_GROUP if group_id else WAKE_DEBOUNCE_DM)
        return

    message_text = data_message.get("message")
    attachments = data_message.get("attachments", [])

    # Parse @mentions from the data message
    mentions = data_message.get("mentions", [])
    bot_mentioned = False
    if mentions and message_text:
        # Sort mentions in reverse order by start position to preserve offsets
        sorted_mentions = sorted(mentions, key=lambda m: m.get("start", 0), reverse=True)
        # Convert message to UTF-16 for proper offset handling
        text_utf16 = message_text.encode('utf-16-le')
        for m in sorted_mentions:
            start = m.get("start", 0)
            length = m.get("length", 0)
            mention_uuid = m.get("uuid", "")
            display_name = identity.resolve_name(f"uuid:{mention_uuid}")
            # Check if bot is mentioned
            if display_name.lower() == "cleo" or mention_uuid == CONFIG.get("bot_uuid", ""):
                bot_mentioned = True
            # Each UTF-16 code unit is 2 bytes
            byte_start = start * 2
            byte_end = (start + length) * 2
            replacement = f"@{display_name}".encode('utf-16-le')
            text_utf16 = text_utf16[:byte_start] + replacement + text_utf16[byte_end:]
        message_text = text_utf16.decode('utf-16-le')
        log.debug("Resolved %d mention(s) in message (bot_mentioned=%s)", len(mentions), bot_mentioned)

    # Extract quote/reply context if present
    quote = data_message.get("quote")
    quote_context = None
    if quote:
        quote_author_raw = quote.get("authorNumber") or quote.get("author") or "unknown"
        quote_author = _resolve_quote_author(quote_author_raw)
        quote_text = quote.get("text")
        quote_id = quote.get("id")  # timestamp of quoted message
        quote_attachments = quote.get("attachments", [])

        # Build a human-readable display string
        parts = []
        if quote_text:
            truncated = quote_text[:100] + "..." if len(quote_text) > 100 else quote_text
            parts.append(f'"{truncated}"')
        if quote_attachments:
            att_types = [a.get("contentType", "file") for a in quote_attachments]
            parts.append(f"[{', '.join(att_types)}]")
        if not parts:
            parts.append("[message]")

        quote_context = {
            "author": quote_author,
            "text": quote_text,
            "id": quote_id,
            "attachments": quote_attachments,
            "display": f"replying to {quote_author}: {' '.join(parts)}",
        }

    # Extract poll data
    poll_create = data_message.get("pollCreate")
    poll_vote = data_message.get("pollVote")
    poll_terminate = data_message.get("pollTerminate")

    # Synthesize human-readable text for poll messages
    if poll_create:
        options_text = "\n".join(f"  {i}. {opt}" for i, opt in enumerate(poll_create.get("options", [])))
        multi_text = " (multiple choice)" if poll_create.get("allowMultiple") else " (single choice)"
        message_text = f"📊 Poll{multi_text}: {poll_create.get('question', '?')}\n{options_text}"
    elif poll_vote:
        indexes = poll_vote.get("optionIndexes", [])
        target_ts = poll_vote.get("targetSentTimestamp")
        message_text = f"🗳️ Voted on poll (ts:{target_ts}): option(s) {indexes}"
    elif poll_terminate:
        target_ts = poll_terminate.get("targetSentTimestamp")
        message_text = f"🔒 Closed poll (ts:{target_ts})"

    if not message_text and not attachments:
        return

    # Determine feed and buffer the message
    feed_id = get_feed_id(sender_id, group_id)

    log_text = (message_text or "")[:100]
    if group_id:
        log.info("Message from %s in %s: %s%s", sender_id, feed_id, log_text,
                 f" [+{len(attachments)} attachment(s)]" if attachments else "")
    else:
        log.info("Message from %s (DM): %s%s", sender_id, log_text,
                 f" [+{len(attachments)} attachment(s)]" if attachments else "")

    if feed_id not in feeds:
        feeds[feed_id] = {"group_id": group_id, "messages": []}

    # Detect and transcribe audio attachments (voice messages)
    audio_attachments = [att for att in attachments if att.get("contentType", "").startswith("audio/")]
    if audio_attachments:
        from voice import transcribe_audio
        for att in audio_attachments:
            path = os.path.join(SIGNAL_ATTACHMENTS_DIR, att.get("id", ""))
            if os.path.exists(path):
                transcript = await asyncio.to_thread(transcribe_audio, path)
                if transcript:
                    voice_text = f'🎤 "{transcript}"'
                    message_text = f"{message_text}\n{voice_text}" if message_text else voice_text

    feed_msg = {
        "sender": sender_id,
        "sender_name": identity.resolve_name(sender_id),
        "text": message_text or "",
        "timestamp": datetime.now().strftime("%H:%M"),
        "signal_ts": envelope.get("timestamp"),
    }

    # Add mention metadata to feed message
    if mentions:
        feed_msg["mentions"] = [
            {"uuid": m.get("uuid"), "name": identity.resolve_name(f"uuid:{m.get('uuid', '')}")}
            for m in mentions
        ]

    # Store image attachment metadata (loaded lazily from disk on read_feed)
    image_attachments = [
        {
            "id": att.get("id"),
            "content_type": att.get("contentType", ""),
            "size": att.get("size", 0),
        }
        for att in attachments
        if att.get("contentType", "").startswith("image/")
    ]
    if image_attachments:
        feed_msg["attachments"] = image_attachments

    # Add quote context if this is a reply
    if quote_context:
        feed_msg["quote"] = quote_context

    # Add poll metadata
    if poll_create:
        feed_msg["poll"] = {
            "type": "create",
            "question": poll_create.get("question"),
            "options": poll_create.get("options", []),
            "allow_multiple": poll_create.get("allowMultiple", False),
        }
    elif poll_vote:
        feed_msg["poll"] = {
            "type": "vote",
            "option_indexes": poll_vote.get("optionIndexes", []),
            "target_timestamp": poll_vote.get("targetSentTimestamp"),
        }
    elif poll_terminate:
        feed_msg["poll"] = {
            "type": "terminate",
            "target_timestamp": poll_terminate.get("targetSentTimestamp"),
        }

    feeds[feed_id]["messages"].append(feed_msg)
    feeds[feed_id]["unread_count"] = feeds[feed_id].get("unread_count", 0) + 1
    unread_feed_ids.add(feed_id)
    _save_feeds()

    # Log full message to daily log (rich detail for dream consolidation)
    timestamp = datetime.now().strftime("%H:%M")
    context = get_group_name(group_id)
    att_note = ""
    if image_attachments:
        att_note += f" [📎 {len(image_attachments)} image(s)]"
    if audio_attachments:
        att_note += f" [🎤 {len(audio_attachments)} voice message(s)]"
    quote_note = ""
    if quote_context:
        qt_text = quote_context.get('text') or '[attachment]'
        quote_note = f"\n> Replying to {quote_context['author']}: {qt_text}\n"
    entry = f"## [{timestamp}] Message from {sender_id} in {context}\n{quote_note}\n{message_text or ''}{att_note}"
    append_daily_memory(CONFIG["workspace"], entry)

    # Trigger debounced wake-up (DMs respond faster, bot @mentions get DM-speed response)
    schedule_wake(WAKE_DEBOUNCE_DM if (not group_id or bot_mentioned) else WAKE_DEBOUNCE_GROUP)


# --- Scheduled event callback ---

async def scheduled_event_callback(event_name: str, prompt: str) -> None:
    """
    Inject a scheduled event as a feed event, triggering a wake cycle.
    Heartbeats, dream consolidation, etc. — all flow through the same path as messages.
    """
    feed_id = f"scheduled:{event_name}"
    if feed_id not in feeds:
        feeds[feed_id] = {"group_id": None, "messages": []}

    feeds[feed_id]["messages"].append({
        "sender": "system",
        "text": prompt,
        "timestamp": datetime.now().strftime("%H:%M"),
    })
    feeds[feed_id]["unread_count"] = feeds[feed_id].get("unread_count", 0) + 1
    unread_feed_ids.add(feed_id)
    _save_feeds()

    log.info("Scheduled event injected: %s", event_name)

    # Wake immediately (no debounce for scheduled events)
    async with consciousness_lock:
        if unread_feed_ids:
            await wake_loop()
    # After dream, run all maintenance tasks in code (not relying on Match)
    if event_name == "dream":
        workspace = CONFIG["workspace"]

        # Step 3 already handled by _trim_feeds in wake_loop

        # Step 4: Delete today's daily log
        today = datetime.now().strftime("%Y-%m-%d")
        daily_log = os.path.join(workspace, "memory", f"{today}.md")
        if os.path.exists(daily_log):
            os.remove(daily_log)
            log.info("Dream: deleted daily log %s", daily_log)

        # Step 5: Delete summaries older than 14 days
        summaries_dir = os.path.join(workspace, "memory", "summaries")
        cutoff = datetime.now() - timedelta(days=14)
        if os.path.exists(summaries_dir):
            for fname in os.listdir(summaries_dir):
                if fname.endswith(".md"):
                    try:
                        fdate = datetime.strptime(fname[:10], "%Y-%m-%d")
                        if fdate < cutoff:
                            os.remove(os.path.join(summaries_dir, fname))
                            log.info("Dream: deleted old summary %s", fname)
                    except ValueError:
                        pass

        # Step 6: Reset consciousness
        consciousness.clear()
        _save_consciousness()

        # Step 7: Append to dream-log.md (hardcoded, not relying on Match)
        dream_log_path = os.path.join(workspace, "memory", "dream-log.md")
        try:
            with open(dream_log_path, "a") as dlf:
                dlf.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M CT')}] Dream: "
                          f"consciousness reset, daily log deleted, old summaries pruned.\n")
            log.info("Dream: appended to dream-log.md")
        except Exception as e:
            log.warning("Dream: failed to write dream-log.md: %s", e)

        log.info("Dream: all maintenance complete — consciousness reset")


# --- TCP listener ---

async def tcp_listener() -> None:
    """
    Connect to signal-cli TCP JSON-RPC and listen for incoming messages.
    signal-cli pushes JSON-RPC notifications for received Signal messages
    over persistent TCP connections. Reconnects with exponential backoff.
    """
    host = CONFIG["signal_tcp_host"]
    port = CONFIG["signal_tcp_port"]
    backoff = 1  # seconds, doubles on each failure up to 60

    while True:
        writer = None
        try:
            log.info("Connecting to signal-cli TCP: %s:%d", host, port)
            reader, writer = await asyncio.open_connection(host, port)
            log.info("TCP connected to signal-cli")
            backoff = 1  # reset on successful connect

            while True:
                line = await reader.readline()
                if not line:
                    log.warning("signal-cli TCP connection closed")
                    break

                try:
                    data = json.loads(line.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.warning("Non-JSON line from TCP: %s", line[:200])
                    continue

                # JSON-RPC notification for received messages
                log.warning("TCP DATA: %s", str(data)[:200])
                if data.get("method") == "receive":
                    params = data.get("params", {})
                    envelope = params.get("envelope")
                    if envelope:
                        asyncio.create_task(handle_signal_message(envelope))

        except (ConnectionRefusedError, OSError) as e:
            log.warning("TCP connection failed: %s — reconnecting in %ds", e, backoff)
        except Exception as e:
            log.exception("Unexpected TCP error — reconnecting in %ds", backoff)
        finally:
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# --- Main ---

async def main() -> None:
    """Start the gateway: API client + TCP listener + scheduler."""
    global api_client

    log.info("cleo starting (feed-based pull architecture)")
    log.info("Bot number: %s", CONFIG["bot_number"])
    log.info("Authorized senders: %s", CONFIG["authorized_senders"])
    log.info("Workspace: %s", CONFIG["workspace"])
    log.info("Model: %s", CONFIG.get("model", "claude-sonnet-4-6"))
    log.info("Wake debounce: DM=%.1fs, group=%.1fs", WAKE_DEBOUNCE_DM, WAKE_DEBOUNCE_GROUP)

    # Initialize Anthropic API client with OAuth
    api_client = init_api_client()

    # Initialize vector memory store
    try:
        from vectorstore import init_vectorstore, index_memory_files
        init_vectorstore(CONFIG["workspace"])
        count = index_memory_files(CONFIG["workspace"])
        log.info("Vector store ready: %d new chunks indexed", count)
    except Exception as e:
        log.error("Vector store init failed (will use base prompt only): %s", e)

    # Initialize dynamic identity resolution
    identity.init(
        rpc_url=CONFIG["signal_rpc_url"],
        bot_number=CONFIG["bot_number"],
        workspace=CONFIG["workspace"],
    )

    # Pre-warm group member cache so DMs from group members work immediately
    if CONFIG.get("authorized_groups"):
        log.info("Pre-warming group member cache...")
        refresh_all_groups(
            CONFIG["authorized_groups"],
            CONFIG["signal_rpc_url"],
            CONFIG["bot_number"],
        )

    # Restore feeds from previous session
    _load_feeds()
    _load_usage()

    # SIGUSR1 handler: external processes (e.g. commit.sh) can trigger a wake
    # by writing to feeds.json and sending SIGUSR1 to this process.
    import signal as _signal
    def _sigusr1_handler(signum, frame):
        try:
            _load_feeds()
            if unread_feed_ids:
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(wake_loop())
                )
                log.info("SIGUSR1 received: feed reload triggered wake")
        except Exception as e:
            log.warning("SIGUSR1 handler error: %s", e)
    _signal.signal(_signal.SIGUSR1, _sigusr1_handler)

    # Restore consciousness from previous session
    _load_consciousness()
    valid = _ensure_valid_history(consciousness)
    if len(valid) != len(consciousness):
        log.warning("Cleaned %d orphaned messages from restored consciousness",
                    len(consciousness) - len(valid))
        consciousness.clear()
        consciousness.extend(valid)

    if unread_feed_ids:
        log.info("Unread feeds from previous session, scheduling wake")
        schedule_wake(WAKE_DEBOUNCE_DM)

    # Start scheduler: heartbeats as feed events + periodic maintenance jobs
    global _scheduler
    _scheduler = start_scheduler(CONFIG, scheduled_event_callback, token_refresh_callback=refresh_api_client)

    # Restore and re-register persisted reminders
    _load_reminders()
    _register_reminders_on_startup()

    # Run the TCP listener (runs forever)
    await tcp_listener()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
