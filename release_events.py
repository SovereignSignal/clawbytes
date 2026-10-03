"""Best-effort Release Events v1 producer shared by Sov's signal services."""
from __future__ import annotations
import json, os, urllib.request

SCHEMA = "release-event/v1"

def emit_release_event(event: dict, timeout: int = 5) -> bool:
    url = os.environ.get("RELEASE_EVENTS_URL", "").strip()
    token = os.environ.get("RELEASE_EVENTS_TOKEN", "").strip()
    if not url or not token:
        return False
    payload = dict(event)
    payload["schema"] = SCHEMA
    required = ("id", "kind", "name", "version", "source", "url")
    if any(not payload.get(k) for k in required):
        return False
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"[WARN] release-event forward failed: {type(exc).__name__}")
        return False
