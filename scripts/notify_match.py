#!/home/billy/cleo/venv/bin/python
import sys
import json
import os
import signal
import subprocess
from datetime import datetime
FEEDS_FILE = os.path.expanduser("~/.cleo/workspace/feeds.json")
msg = sys.argv[1] if len(sys.argv) > 1 else "commit pushed"
feed_id = "scheduled:commit-notify"
try:
    with open(FEEDS_FILE, 'r') as f:
        data = json.load(f)
except Exception:
    data = {"feeds": {}, "unread": []}
feeds = data.get("feeds", {})
unread = data.get("unread", [])
if feed_id not in feeds:
    feeds[feed_id] = {"group_id": None, "messages": []}
feeds[feed_id]["messages"].append({
    "sender": "system",
    "text": msg,
    "timestamp": datetime.now().strftime("%H:%M"),
})
feeds[feed_id]["unread_count"] = feeds[feed_id].get("unread_count", 0) + 1
if feed_id not in unread:
    unread.append(feed_id)
data["feeds"] = feeds
data["unread"] = unread
tmp = FEEDS_FILE + ".tmp"
with open(tmp, 'w') as f:
    json.dump(data, f)
os.replace(tmp, FEEDS_FILE)
try:
    result = subprocess.run(['pgrep', '-f', 'gateway.py'], capture_output=True, text=True)
    pid = result.stdout.strip().split('\n')[0]
    if pid:
        os.kill(int(pid), signal.SIGUSR1)
        print(f"Feed injected and SIGUSR1 sent to cleo (PID {pid})")
    else:
        print("Feed injected — cleo not running, will process on next start")
except Exception as e:
    print(f"Feed injected but SIGUSR1 failed: {e}")
