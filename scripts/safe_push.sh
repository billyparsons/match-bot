#!/bin/bash
# safe_push.sh — silent commit and push for subagents working on cleo code
# Usage: ~/cleo/scripts/safe_push.sh "description of what changed"
# No Match notification — just commits and pushes.

MSG="${1:-autosave}"
cd ~/cleo || exit 1

git add -A
git commit -m "$MSG" || echo "nothing to commit"
git push origin main
echo "pushed: $MSG"
