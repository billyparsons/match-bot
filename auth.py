"""
auth.py — Authorization for cleo.

Two ways a sender can be authorized:
  1. Individual: their phone number or UUID is in config.authorized_senders
  2. Group member: they belong to a group listed in config.authorized_groups,
     which also grants them DM access (not just group chat access)

Group member resolution works by querying signal-cli for the member list of
each approved group. This result is cached and refreshed periodically so DMs
from group members are recognized even without a group ID in the envelope.

Security note: group member lists are fetched from the local signal-cli daemon
(127.0.0.1) and cached in memory only — never written to disk.
"""

import time
import logging
import requests

log = logging.getLogger("cleo.auth")

# How often to refresh group member cache (seconds)
CACHE_TTL = 300  # 5 minutes

# In-memory cache: maps group_id → {"members": set[str], "fetched_at": float}
_group_cache: dict[str, dict] = {}


def _normalize_id(raw: str | None) -> str | None:
    """
    Normalize a Signal identity to a canonical string:
    - Phone numbers stay as-is: '+16142080533'
    - UUIDs get prefixed: 'uuid:xxxxxxxx-...'
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("+"):
        return raw
    if raw.startswith("uuid:"):
        return raw
    # Bare UUID (no prefix) — add it
    if "-" in raw and len(raw) == 36:
        return f"uuid:{raw}"
    return raw


def fetch_group_members(group_id: str, rpc_url: str, bot_number: str) -> set[str]:
    """
    Fetch member identities for a Signal group via signal-cli JSON-RPC.
    Returns a set of normalized sender IDs (phone numbers and 'uuid:...' strings).
    Returns empty set on any error.
    """
    payload = {
        "jsonrpc": "2.0",
        "method": "listGroups",
        "id": "cleo-listgroups",
        "params": {"account": bot_number},
    }
    try:
        resp = requests.post(rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        groups = data.get("result", [])
    except Exception as e:
        log.warning("Failed to fetch group list: %s", e)
        return set()

    members: set[str] = set()
    for group in groups:
        if group.get("id") != group_id:
            continue
        # Members can be listed as {number, uuid} objects or just strings
        for member in group.get("members", []):
            if isinstance(member, dict):
                phone = _normalize_id(member.get("number"))
                uuid = _normalize_id(member.get("uuid"))
                if phone:
                    members.add(phone)
                if uuid:
                    members.add(f"uuid:{member['uuid']}" if not member.get('uuid', '').startswith('uuid:') else member['uuid'])
            elif isinstance(member, str):
                normalized = _normalize_id(member)
                if normalized:
                    members.add(normalized)

    log.info("Group %s has %d members", group_id[:12], len(members))
    return members


def get_group_members(group_id: str, rpc_url: str, bot_number: str) -> set[str]:
    """
    Return cached member set for a group, refreshing if stale.
    """
    cached = _group_cache.get(group_id)
    now = time.time()

    if cached and (now - cached["fetched_at"]) < CACHE_TTL:
        return cached["members"]

    # Cache miss or stale — refresh
    members = fetch_group_members(group_id, rpc_url, bot_number)
    _group_cache[group_id] = {"members": members, "fetched_at": now}
    return members


def refresh_all_groups(authorized_groups: list[str], rpc_url: str, bot_number: str) -> None:
    """
    Pre-warm the cache for all authorized groups.
    Called on startup and periodically by the scheduler.
    """
    for group_id in authorized_groups:
        get_group_members(group_id, rpc_url, bot_number)
    log.info("Group member cache refreshed (%d groups)", len(authorized_groups))


def is_group_member(sender_id: str, authorized_groups: list[str],
                    rpc_url: str, bot_number: str) -> bool:
    """
    Check if a sender is a member of any authorized group.
    Used to grant DM access to people who belong to approved groups.
    """
    for group_id in authorized_groups:
        members = get_group_members(group_id, rpc_url, bot_number)
        if sender_id in members:
            return True
    return False


def is_authorized(sender_id: str, group_id: str | None,
                  config: dict) -> bool:
    """
    Full authorization check. Returns True if:
    1. sender_id is in config.authorized_senders (individual allowlist), OR
    2. group_id is in config.authorized_groups (message came from an approved group), OR
    3. sender_id is a member of any authorized group (grants DM access)

    Rules 1 and 2 are O(1). Rule 3 uses the member cache (refreshed every 5 min).
    """
    authorized_senders = config.get("authorized_senders", [])
    authorized_groups = config.get("authorized_groups", [])
    rpc_url = config["signal_rpc_url"]
    bot_number = config["bot_number"]

    # Rule 1: individual allowlist
    if sender_id in authorized_senders:
        return True

    # Rule 2: message came from an approved group
    if group_id and group_id in authorized_groups:
        return True

    # Rule 3: sender is a member of an approved group → DM access
    if authorized_groups and is_group_member(sender_id, authorized_groups, rpc_url, bot_number):
        log.info("Sender %s authorized via group membership", sender_id)
        return True

    return False
