#!/usr/bin/env python3
import sys
import requests
import json

msg = sys.argv[1] if len(sys.argv) > 1 else "commit pushed"
payload = {
    "jsonrpc": "2.0",
    "method": "sendMessage",
    "id": "commit-notify",
    "params": {
        "account": "+13463460886",
        "recipient": "d9ffd4d4-0738-46e1-a1fe-cfc95ebdd525",
        "message": msg
    }
}
try:
    requests.post("http://127.0.0.1:8080/api/v1/rpc", json=payload, timeout=5)
except Exception as e:
    print(f"Notify failed: {e}")
