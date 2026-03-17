#!/bin/bash
cd ~/cleo
git add -A
git commit -m "${1:-update}"
git push
echo "Pushed to GitHub"
