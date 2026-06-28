"""E2E test: verify that blocked commands are NOT executed on the remote host."""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import requests
import websockets

BASE = "http://localhost:8000/api"

# 1. Login
resp = requests.post(
    f"{BASE}/auth/login", json={"username": "admin", "password": "admin123"}
)
assert resp.status_code == 200, f"Login failed: {resp.status_code}"
token = resp.json()["access_token"]
print("[OK] Logged in")


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

assert linux_dev, "Linux test device not found"
device_id = linux_dev["id"]
cred_id = linux_dev.get("credential_id")
print(f"[OK] Device: {linux_dev['name']}")

# 3. Connect WebSocket
ws_url = (
    f"ws://localhost:8000/ws/terminal/{device_id}"
    f"?token={token}"
    f"&conn_type=ssh"
    f"&credential_id={cred_id}"
)


async def drain(ws, timeout=1.0):
    """Read all pending messages from WebSocket."""
    output = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            data = json.loads(msg).get("data", "") if isinstance(msg, str) else ""
            output += data
        except asyncio.TimeoutError:
            break
    return output


async def type_cmd(ws, cmd, enter=True):
    """Type a command character by character."""
    for ch in cmd:
        await ws.send(json.dumps({"type": "input", "data": ch}))
        await asyncio.sleep(0.03)
    if enter:
        await ws.send(json.dumps({"type": "input", "data": "\r"}))


async def setup_and_test():
    async with websockets.connect(ws_url) as ws:
        # Wait for initial shell prompt
        await asyncio.sleep(3)
        initial = await drain(ws, 2)
        print(f"[OK] Shell connected, initial output length: {len(initial)}")

        # Create test directory and a marker file
        await type_cmd(ws, "mkdir -p /tmp/block_test_dir")
        await asyncio.sleep(1.5)
        await drain(ws, 0.5)

        await type_cmd(ws, "echo marker_ok > /tmp/block_test_dir/marker.txt")
        await asyncio.sleep(1.5)
        await drain(ws, 0.5)

        # Verify marker exists
        await type_cmd(ws, "cat /tmp/block_test_dir/marker.txt")
        await asyncio.sleep(1.5)
        output = await drain(ws, 1)
        print(f"[OK] Marker check: {'marker_ok' in output}")

        if "marker_ok" not in output:
            print(f"[FAIL] Setup failed. Output: {output[:200]}")
            return False

        print("[OK] Test directory and marker file created")

        # 4. Try to delete with a BLOCKED command: rm -rf /tmp/block_test_dir
        blocked_cmd = "rm -rf /tmp/block_test_dir"
        for ch in blocked_cmd:
            await ws.send(json.dumps({"type": "input", "data": ch}))
            await asyncio.sleep(0.03)
        # Send Enter — should trigger the block
        await ws.send(json.dumps({"type": "input", "data": "\r"}))
        await asyncio.sleep(2)

        output = await drain(ws, 2)
        print(f"[OK] Block output: {repr(output[:300])}")

        has_block_warning = (
            "安全拦截" in output or "阻断" in output or "blocked" in output.lower()
        )
        if has_block_warning:
            print("[OK] Security interception warning received")
        else:
            print("[WARN] No block warning in output")

        # 5. Check if the directory still exists
        await type_cmd(ws, "cat /tmp/block_test_dir/marker.txt")
        await asyncio.sleep(1.5)
        output = await drain(ws, 1)
        print(f"[OK] After-block marker check: {repr(output[:200])}")

        if "marker_ok" in output:
            print("[PASS] Directory still exists — block worked!")
            result = True
        elif "No such file" in output or "cannot access" in output:
            print("[FAIL] Directory was DELETED — block didn't prevent execution!")
            result = False
        else:
            print(f"[WARN] Inconclusive output: {output[:300]}")
            result = None

        # Cleanup
        await type_cmd(ws, "rm -rf /tmp/block_test_dir")
        await asyncio.sleep(1)
        await drain(ws, 0.5)

        await ws.close()
        return result


result = asyncio.run(setup_and_test())

# 6. Check audit logs
resp = requests.get(
    f"{BASE}/audit-logs?limit=20", headers={"Authorization": f"Bearer {token}"}
)
logs = json_data(resp)
for log in logs:
    if log.get("event_type") == "command_blocked":
        print(f"[OK] Audit log: command_blocked: {log.get('command')}")
        break
else:
    print("[WARN] No command_blocked audit log found")

if result is False:
    print("\nBLOCK DID NOT PREVENT EXECUTION!")
    sys.exit(1)
elif result is True:
    print("\nALL CHECKS PASSED — BLOCK WORKS!")
    sys.exit(0)
else:
    print("\nINCONCLUSIVE — manual verification needed")
    sys.exit(2)
