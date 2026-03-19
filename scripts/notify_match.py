#!/home/billy/cleo/venv/bin/python
import sys
import os
sys.path.insert(0, '/home/billy/cleo')
os.chdir('/home/billy/cleo')

from config import load_config
CONFIG = load_config()

from tools import send_message

msg = sys.argv[1] if len(sys.argv) > 1 else "commit pushed"
result = send_message(
    recipient="uuid:d9ffd4d4-0738-46e1-a1fe-cfc95ebdd525",
    message=msg
)
print(result)
