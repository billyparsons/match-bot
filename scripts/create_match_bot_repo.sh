#!/usr/bin/env bash
set -euo pipefail

# Create a GitHub repo named match-bot for the authenticated user.
# Requires one of:
#   - gh CLI authenticated, or
#   - GITHUB_TOKEN env var with repo scope.

REPO_NAME="${1:-match-bot}"
VISIBILITY="${2:-private}" # private|public

log() { printf "[create-repo] %s\n" "$*"; }
warn() { printf "[create-repo][warn] %s\n" "$*" >&2; }

if [[ "$VISIBILITY" != "private" && "$VISIBILITY" != "public" ]]; then
  warn "Visibility must be 'private' or 'public'."
  exit 1
fi

if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    log "Using gh CLI to create ${REPO_NAME} (${VISIBILITY})"
    gh repo create "$REPO_NAME" --"$VISIBILITY" --confirm
    log "Repo created successfully via gh."
    exit 0
  fi
  warn "gh is installed but not authenticated; trying REST fallback."
fi

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  warn "Cannot create GitHub repo: no authenticated gh and no GITHUB_TOKEN provided."
  warn "Export GITHUB_TOKEN and rerun, e.g.:"
  warn "  GITHUB_TOKEN=*** scripts/create_match_bot_repo.sh match-bot private"
  exit 2
fi

API_PAYLOAD=$(printf '{"name":"%s","private":%s}' "$REPO_NAME" "$([[ "$VISIBILITY" == "private" ]] && echo true || echo false)")

log "Using GitHub REST API to create ${REPO_NAME} (${VISIBILITY})"
HTTP_CODE=$(curl -sS -o /tmp/create_repo_response.json -w "%{http_code}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -X POST https://api.github.com/user/repos \
  -d "${API_PAYLOAD}" || true)

if [[ "$HTTP_CODE" =~ ^20[01]$ ]]; then
  log "Repo created successfully via API."
  exit 0
fi

warn "GitHub API create failed with HTTP ${HTTP_CODE}."
warn "Response excerpt:"
sed -n '1,80p' /tmp/create_repo_response.json >&2 || true
exit 3
