"""End-to-end test: connect SSH via WebSocket, type commands, verify audit logs."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import requests
import websockets

BASE = "http://localhost:8000/api"

# 1. Login
resp = requests.post(
    f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"}
)
if resp.status_code != 200:
    print(f"[FAIL] Login failed: {resp.status_code} {resp.text}")
    sys.exit(1)
token = resp.json()["access_token"]
print(f"[OK] Logged in, token={token[:20]}...")


def json_data(resp):
    d = resp.json()
    return d.get("data", d) if isinstance(d, dict) else d


# 2. Find the Linux test device
resp = requests.get(
    f"{BASE}/credentials/all-devices", headers={"Authorization": f"Bearer {token}"}
)
devices = json_data(resp)
linux_dev = None
for d in devices:
    if d.get("ip_address") == "192.168.155.128":
        linux_dev = d
        break

if not linux_dev:
    print(
        f"[FAIL] Linux test device not found. Devices: {[d['name'] for d in devices]}"
    )
    sys.exit(1)

device_id = linux_dev["id"]
cred_id = linux_dev.get("credential_id")
print(f"[OK] Device: {linux_dev['name']} (id={device_id}, credential_id={cred_id})")

# 3. Get previous audit log count (baseline)
resp = requests.get(
    f"{BASE}/audit-logs?limit=100", headers={"Authorization": f"Bearer {token}"}
)
baseline_logs = json_data(resp)
baseline_commands = [
    l for l in baseline_logs if l.get("event_type") == "command_executed"
]
print(f"[OK] Baseline audit logs: {len(baseline_commands)} command_executed entries")

# 4. Connect WebSocket and type commands
ws_url = (
    f"ws://localhost:8000/ws/terminal/{device_id}"
    f"?token={token}"
    f"&conn_type=ssh"
    f"&credential_id={cred_id}"
)
print("[OK] Connecting WebSocket...")

commands_to_type = ["ls", "cd /etc", "pwd", "cat /etc/hostname"]


async def run_test():
    async with websockets.connect(ws_url) as ws:
        msg = await asyncio.wait_for(ws.recv(), timeout=15)
        print(f"  Received: {msg}")
        await asyncio.sleep(2)

        for cmd in commands_to_type:
            for ch in cmd:
                await ws.send(json.dumps({"type": "input", "data": ch}))
                await asyncio.sleep(0.05)
            await ws.send(json.dumps({"type": "input", "data": "\r"}))
            print(f"  Typed: {cmd}")
            await asyncio.sleep(1.5)

        await ws.close()
        print("[OK] WebSocket closed")
    await asyncio.sleep(1)


asyncio.run(run_test())

# 5. Check audit logs
resp = requests.get(
    f"{BASE}/audit-logs?limit=50", headers={"Authorization": f"Bearer {token}"}
)
all_logs = json_data(resp)
new_commands = []
for log in all_logs:
    if log.get("event_type") == "command_executed":
        new_commands.append(log["command"])

recent = new_commands[: len(commands_to_type)]
recent.reverse()

print(f"\n{'=' * 50}")
print(f"Expected commands: {commands_to_type}")
print(f"Recent audit logs: {recent[: len(commands_to_type)]}")

# Check if the expected commands appear in the audit logs
all_ok = True
for cmd in commands_to_type:
    found = any(cmd == logged for logged in recent[: len(commands_to_type)])
    if found:
        print(f"  [OK] '{cmd}' logged correctly")
    else:
        print(f"  [FAIL] '{cmd}' NOT found in audit logs!")
        all_ok = False

if all_ok:
    print(f"\n{'=' * 50}")
    print("ALL COMMANDS RECORDED CORRECTLY!")
else:
    print(f"\n{'=' * 50}")
    print("SOME COMMANDS WERE NOT RECORDED CORRECTLY")
    sys.exit(1)
