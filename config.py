"""
config.py — Load configuration from config.yaml and environment variables.

Security model:
- config.yaml holds non-secret values (phone numbers, paths, model name).
- Secrets (API keys) come from environment variables loaded via .env.
- Secrets are NEVER passed into LLM system prompts or tool results.
"""

import os
import time
import json
import logging
import subprocess
import yaml
from dotenv import load_dotenv

# Load .env before anything else
load_dotenv()


def load_config(path: str = "config.yaml") -> dict:
    """Load config.yaml and merge with environment defaults."""
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)

    # Environment-based overrides / defaults
    cfg["signal_cli_url"] = os.getenv("SIGNAL_CLI_URL", "http://127.0.0.1:8080")
    cfg["signal_rpc_url"] = cfg["signal_cli_url"] + "/api/v1/rpc"

    # TCP endpoint for receiving messages (persistent JSON-RPC connection)
    tcp_addr = os.getenv("SIGNAL_CLI_TCP", "127.0.0.1:7583")
    host, _, port = tcp_addr.rpartition(":")
    cfg["signal_tcp_host"] = host or "127.0.0.1"
    cfg["signal_tcp_port"] = int(port) if port else 7583

    # Derived portable values
    cfg["home_dir"] = os.path.expanduser("~")
    home_prefix = cfg["home_dir"].rstrip("/") + "/"
    extra_paths = cfg.get("allowed_paths", [])
    cfg["allowed_path_prefixes"] = tuple(
        [home_prefix] + [p.rstrip("/") + "/" for p in extra_paths if p]
    )
    cfg.setdefault("timezone", "America/New_York")
    cfg.setdefault("workspace", os.path.join(cfg["home_dir"], ".cleo", "workspace"))

    return cfg


# Singleton config — loaded once at import time
CONFIG = load_config()


# --- OAuth credential management ---

log = logging.getLogger("cleo.config")

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
TOKEN_REFRESH_MARGIN = 300  # seconds — refresh 5 min before expiry

_cached_token: dict | None = None  # {"access_token": str, "expires_at": float}


def _read_credentials() -> tuple[str, float]:
    """
    Read and parse ~/.claude/.credentials.json.
    Returns (access_token, expires_at_seconds).
    Raises RuntimeError if file is missing/malformed.
    """
    try:
        with open(CREDENTIALS_PATH, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Credentials file not found: {CREDENTIALS_PATH}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in credentials file: {e}")

    oauth = data.get("claudeAiOauth")
    if not oauth:
        raise RuntimeError("No 'claudeAiOauth' key in credentials file")

    access_token = oauth.get("accessToken")
    expires_at_ms = oauth.get("expiresAt")

    if not access_token:
        raise RuntimeError("No accessToken in credentials")
    if not expires_at_ms:
        raise RuntimeError("No expiresAt in credentials")

    return access_token, expires_at_ms / 1000.0


def load_credentials() -> str:
    """
    Load the OAuth access token from ~/.claude/.credentials.json.

    If the token is near/past expiry, forces a refresh by invoking the
    official Claude CLI (which handles OAuth natively), then re-reads
    the credentials file.

    Returns the access token string (sk-ant-oat01-...).
    Raises RuntimeError("MANUAL_LOGIN_REQUIRED") if refresh fails.
    """
    global _cached_token
    now = time.time()

    # Return cached token if still valid
    if _cached_token and (_cached_token["expires_at"] - now) > TOKEN_REFRESH_MARGIN:
        return _cached_token["access_token"]

    access_token, expires_at = _read_credentials()
    needs_refresh = expires_at - now <= TOKEN_REFRESH_MARGIN

    if needs_refresh:
        log.info("OAuth token near/past expiry — refreshing via Claude CLI")
        try:
            subprocess.run(
                ["claude", "-p", "ok"],
                capture_output=True, text=True, timeout=45,
            )
        except Exception as e:
            log.error("Claude CLI refresh subprocess failed: %s", e)

        # Re-read credentials after CLI refresh
        access_token, expires_at = _read_credentials()
        if expires_at - time.time() <= TOKEN_REFRESH_MARGIN:
            raise RuntimeError("MANUAL_LOGIN_REQUIRED")

    _cached_token = {"access_token": access_token, "expires_at": expires_at}
    remaining = expires_at - now
    log.info("OAuth token loaded (expires in %.0f minutes)", remaining / 60)

    return access_token
