#!/bin/bash
# Skip feed injection if called from cleo process to avoid circular notifications
if [ -n "$CLEO_PROCESS" ]; then
    cd ~/cleo
    git add -A
    git commit -m "${1:-update}"
    git push
    echo "Pushed to GitHub"
    exit 0
fi
cd ~/cleo
git add -A
git commit -m "${1:-update}"
git push
echo "Pushed to GitHub"

# Only notify Match for user-facing changes
# Keywords that indicate Match should be told: new tool, new feature, changed behavior
MSG="${1:-update}"
if echo "$MSG" | grep -qiE "feat:|new tool|new feature|add.*tool|tool.*add|behavior|default|workflow|breaking"; then
    /home/billy/cleo/venv/bin/python ~/cleo/scripts/notify_match.py "commit: \"$MSG\" — this looks like a user-facing change. update CODEBASE.md if needed. text me when done or if nothing needed."
else
    # Still send SIGUSR1 so Match knows the commit happened, but no feed injection
    PID=$(pgrep -f gateway.py | head -1)
    if [ -n "$PID" ]; then
        kill -USR1 "$PID"
        echo "Feed injected and SIGUSR1 sent to cleo (PID $PID)"
    fi
fi
