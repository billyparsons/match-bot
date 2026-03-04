"""
identity.py — Dynamic identity resolution for Signal contacts and groups.

Resolves UUIDs and phone numbers to display names using:
  1. Custom nicknames (nicknames.json)
  2. Signal profile names (via signal-cli JSON-RPC)
  3. Raw identifiers (fallback)

Resolves group names to/from group IDs using:
  1. Signal group data (via signal-cli JSON-RPC)
  2. Group aliases (group_aliases.json)
"""

import os
import json
import time
import logging
import requests
from dataclasses import dataclass

log = logging.getLogger("cleo.identity")

CACHE_TTL = 300  # seconds


@dataclass
class ContactInfo:
    uuid: str | None
    number: str | None
    profile_name: str | None  # "Given Family"
    nickname: str | None      # from nicknames.json


@dataclass
class GroupInfo:
    group_id: str  # base64
    name: str      # canonical name from Signal


# Module-level caches
_contacts: dict[str, ContactInfo] = {}  # keyed by normalized sender_id
_groups: dict[str, GroupInfo] = {}      # keyed by base64 group_id
_group_name_index: dict[str, str] = {}  # lowercase name → group_id
_cache_ts: float = 0.0
_nicknames: dict[str, str] = {}         # uuid/phone → display name
_group_aliases: dict[str, str] = {}     # alias → canonical group name
_nicknames_mtime: float = 0.0
_aliases_mtime: float = 0.0

# Config (set by init())
_rpc_url: str = ""
_bot_number: str = ""
_workspace: str = ""


def init(rpc_url: str, bot_number: str, workspace: str) -> None:
    """Initialize the identity module. Call once at startup."""
    global _rpc_url, _bot_number, _workspace
    _rpc_url = rpc_url
    _bot_number = bot_number
    _workspace = workspace
    _load_nicknames()
    _load_group_aliases()
    refresh_cache()


def _load_nicknames() -> None:
    """Load nicknames.json (re-reads if file changed)."""
    global _nicknames, _nicknames_mtime
    path = os.path.join(_workspace, "nicknames.json")
    try:
        mtime = os.path.getmtime(path)
        if mtime == _nicknames_mtime:
            return
        with open(path) as f:
            data = json.load(f)
        _nicknames = {k: v for k, v in data.items() if not k.startswith("_")}
        _nicknames_mtime = mtime
        log.info("Loaded %d nicknames", len(_nicknames))
    except FileNotFoundError:
        _nicknames = {}
    except Exception as e:
        log.warning("Failed to load nicknames.json: %s", e)


def _load_group_aliases() -> None:
    """Load group_aliases.json (re-reads if file changed)."""
    global _group_aliases, _aliases_mtime
    path = os.path.join(_workspace, "group_aliases.json")
    try:
        mtime = os.path.getmtime(path)
        if mtime == _aliases_mtime:
            return
        with open(path) as f:
            data = json.load(f)
        _group_aliases = {k.lower(): v for k, v in data.items() if not k.startswith("_")}
        _aliases_mtime = mtime
        log.info("Loaded %d group aliases", len(_group_aliases))
    except FileNotFoundError:
        _group_aliases = {}
    except Exception as e:
        log.warning("Failed to load group_aliases.json: %s", e)


def _ensure_cache() -> None:
    """Refresh caches if stale. Also re-reads nickname files if changed."""
    if time.time() - _cache_ts > CACHE_TTL:
        refresh_cache()
    _load_nicknames()
    _load_group_aliases()


def refresh_cache() -> None:
    """Force refresh contact and group caches from signal-cli."""
    global _cache_ts
    _refresh_contacts()
    _refresh_groups()
    _cache_ts = time.time()


def _rpc_call(method: str, params: dict | None = None) -> dict | list | None:
    """Make a JSON-RPC call to signal-cli."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": "identity",
        "params": params or {},
    }
    payload["params"]["account"] = _bot_number
    try:
        resp = requests.post(_rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json().get("result")
    except Exception as e:
        log.warning("RPC call %s failed: %s", method, e)
        return None


def _refresh_contacts() -> None:
    """Fetch all contacts from signal-cli and populate _contacts."""
    global _contacts
    result = _rpc_call("listContacts")
    if not result:
        return
    new_contacts: dict[str, ContactInfo] = {}
    for c in result:
        uuid = c.get("uuid")
        number = c.get("number")
        profile = c.get("profile") or {}
        given = profile.get("givenName") or ""
        family = profile.get("familyName") or ""
        profile_name = f"{given} {family}".strip() or None

        info = ContactInfo(uuid=uuid, number=number, profile_name=profile_name, nickname=None)

        # Index by all known identifiers
        if uuid:
            key = f"uuid:{uuid}"
            info.nickname = _nicknames.get(uuid)
            new_contacts[key] = info
        if number:
            nick = _nicknames.get(number)
            info_copy = ContactInfo(
                uuid=uuid, number=number, profile_name=profile_name,
                nickname=nick or (info.nickname if uuid else None),
            )
            new_contacts[number] = info_copy

    _contacts = new_contacts
    log.info("Contact cache refreshed: %d entries", len(_contacts))


def _refresh_groups() -> None:
    """Fetch all groups from signal-cli and populate _groups."""
    global _groups, _group_name_index
    result = _rpc_call("listGroups")
    if not result:
        return
    new_groups: dict[str, GroupInfo] = {}
    new_index: dict[str, str] = {}
    for g in result:
        gid = g.get("id")
        name = g.get("name", "")
        if gid and name:
            info = GroupInfo(group_id=gid, name=name)
            new_groups[gid] = info
            new_index[name.lower()] = gid
    _groups = new_groups
    _group_name_index = new_index
    log.info("Group cache refreshed: %d groups", len(_groups))


def resolve_name(sender_id: str) -> str:
    """
    Resolve a sender ID to a human-readable display name.

    Priority: nickname > profile name > phone number > raw UUID.
    Handles internal sender IDs ("cleo", "system") gracefully.
    """
    if not sender_id or sender_id in ("cleo", "system"):
        return sender_id

    _ensure_cache()

    # Direct cache hit
    contact = _contacts.get(sender_id)
    if contact:
        if contact.nickname:
            return contact.nickname
        if contact.profile_name:
            return contact.profile_name
        if contact.number:
            return contact.number
        return sender_id

    # Try nickname lookup by bare UUID
    bare_uuid = sender_id.removeprefix("uuid:")
    nick = _nicknames.get(bare_uuid) or _nicknames.get(sender_id)
    if nick:
        return nick

    return sender_id


def resolve_group_name(group_id: str) -> str:
    """Resolve a base64 group ID to its canonical name."""
    _ensure_cache()
    info = _groups.get(group_id)
    if info:
        return info.name
    return f"group-{group_id[:8]}"


def resolve_group_id(name: str) -> str | None:
    """
    Resolve a friendly group name to its base64 group ID.

    Checks: exact canonical name match, then aliases, both case-insensitive.
    """
    _ensure_cache()

    # Exact match on canonical name (case-insensitive)
    lower = name.lower()
    gid = _group_name_index.get(lower)
    if gid:
        return gid

    # Alias match
    canonical = _group_aliases.get(lower)
    if canonical:
        return _group_name_index.get(canonical.lower())

    return None


def get_all_groups() -> dict[str, str]:
    """Return {group_id: canonical_name} for all known groups."""
    _ensure_cache()
    return {gid: info.name for gid, info in _groups.items()}
