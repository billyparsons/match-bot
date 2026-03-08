#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/bootstrap_match_bot.sh [TARGET_DIR]
# Example:
#   scripts/bootstrap_match_bot.sh /workspace/match-bot

TARGET_DIR="${1:-/workspace/match-bot}"
CLEO_URL="https://github.com/seangibat/cleo.git"
REMOTE_REPO="match-bot"

log() { printf "[bootstrap] %s\n" "$*"; }
warn() { printf "[bootstrap][warn] %s\n" "$*" >&2; }

log "Target directory: ${TARGET_DIR}"

if [[ -d "${TARGET_DIR}/.git" ]]; then
  warn "Target already contains a git repo. Reusing it."
else
  rm -rf "${TARGET_DIR}"
fi

if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    log "GitHub CLI is authenticated. Attempting to create remote repo '${REMOTE_REPO}'."
    # idempotent-ish creation: ignore errors if already exists
    gh repo create "${REMOTE_REPO}" --private >/dev/null 2>&1 || warn "GitHub repo create skipped (already exists or unavailable)."
  else
    warn "gh is installed but not authenticated; skipping GitHub repo creation."
  fi
else
  warn "gh is not installed; skipping GitHub repo creation."
fi

log "Cloning Cleo base from ${CLEO_URL}"
if git clone "${CLEO_URL}" "${TARGET_DIR}"; then
  log "Clone complete."
else
  warn "Clone failed (likely network/proxy constraints)."
  warn "Fallback: download a zip/tarball externally and extract to ${TARGET_DIR}."
  exit 2
fi

cd "${TARGET_DIR}"

git checkout -b efficiency-hardening || true

log "Bootstrapped '${TARGET_DIR}'. Next: apply efficiency edits from EFFICIENCY_EDIT_PLAN_FOR_CLEO.md"
