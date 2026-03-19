#!/bin/bash
cd ~/cleo
git add -A
git commit -m "${1:-update}"
git push
echo "Pushed to GitHub"
python3 ~/cleo/scripts/notify_match.py "hey! just pushed a commit: \"${1:-update}\" — review and update CODEBASE.md and CHEATSHEET.md if anything changed. text me when done or if nothing needed updating."
