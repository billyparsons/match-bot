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
/home/billy/cleo/venv/bin/python ~/cleo/scripts/notify_match.py "commit: \"${1:-update}\" — review for doc updates per CODEBASE.md doc update workflow. update CHEATSHEET.md if behavior changed. text me when done or if nothing needed."
