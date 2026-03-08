#!/usr/bin/env bash
set -euo pipefail

# Apply high-impact Claude Pro efficiency defaults to a local Cleo checkout.
# This script is idempotent and only touches files if expected patterns are found.

TARGET_DIR="${1:-/workspace/match-bot}"
DRY_RUN="${DRY_RUN:-0}"

log() { printf "[patch-pro] %s\n" "$*"; }
warn() { printf "[patch-pro][warn] %s\n" "$*" >&2; }

if [[ ! -d "$TARGET_DIR" ]]; then
  warn "Target directory '$TARGET_DIR' not found."
  exit 1
fi

cd "$TARGET_DIR"

if [[ ! -d .git ]]; then
  warn "Target is not a git repository: $TARGET_DIR"
  exit 1
fi

replace_if_present() {
  local file="$1"
  local pattern="$2"
  local replacement="$3"

  if [[ ! -f "$file" ]]; then
    warn "Skip missing file: $file"
    return 0
  fi

  if rg -n --fixed-strings "$pattern" "$file" >/dev/null 2>&1; then
    log "Patch: $file :: $pattern -> $replacement"
    if [[ "$DRY_RUN" == "0" ]]; then
      perl -0pi -e "s/\Q$pattern\E/$replacement/g" "$file"
    fi
  else
    warn "Pattern not found in $file: $pattern"
  fi
}

# Recommendations aligned with user's requested cross-check.
replace_if_present "src/config/defaults.ts" "readFeedLimit: 50" "readFeedLimit: 10"
replace_if_present "src/config/defaults.ts" "webFetchMaxChars: 50000" "webFetchMaxChars: 8000"
replace_if_present "src/config/defaults.ts" "retrievalTopK: 10" "retrievalTopK: 4"
replace_if_present "src/config/defaults.ts" "mainLoopMaxIterations: 50" "mainLoopMaxIterations: 10"
replace_if_present "src/config/defaults.ts" "subagentMaxIterations: 100" "subagentMaxIterations: 15"
replace_if_present "src/config/defaults.ts" "maxConcurrentSubagents: 3" "maxConcurrentSubagents: 1"
replace_if_present "src/config/defaults.ts" "subagentsEnabledByDefault: true" "subagentsEnabledByDefault: false"

# Fallback common config names.
replace_if_present "src/config.ts" "readFeedLimit: 50" "readFeedLimit: 10"
replace_if_present "src/config.ts" "webFetchMaxChars: 50000" "webFetchMaxChars: 8000"
replace_if_present "src/config.ts" "retrievalTopK: 10" "retrievalTopK: 4"
replace_if_present "src/config.ts" "mainLoopMaxIterations: 50" "mainLoopMaxIterations: 10"
replace_if_present "src/config.ts" "subagentMaxIterations: 100" "subagentMaxIterations: 15"
replace_if_present "src/config.ts" "maxConcurrentSubagents: 3" "maxConcurrentSubagents: 1"
replace_if_present "src/config.ts" "subagentsEnabledByDefault: true" "subagentsEnabledByDefault: false"

# Add a marker file for tracking applied profile.
if [[ "$DRY_RUN" == "0" ]]; then
  cat > .efficiency-profile-applied <<'MARK'
profile=claude-pro-lean
changes=read_feed_50_to_10,web_fetch_50000_to_8000,retrieval_10_to_4,main_loop_50_to_10,subagent_100_to_15,subagents_default_off
MARK
fi

log "Completed. Review changes with: git -C '$TARGET_DIR' diff"
