#!/usr/bin/env python3
"""
ClawBytes X watchlist.

Official posts from @cursor_ai and product posts from @btibor91. Replies and
reposts are dropped. The first successful fetch of an account is a silent
baseline.

Off unless CLAWBYTES_X_WATCH is 1/true/yes/on. The bearer is
CLAWBYTES_X_BEARER_TOKEN (app-only, X API v2). With the flag off this process
does not call the API and exits 0.

Cost, pay-per-use Posts: Read, not the owned-read rate (that rate is only
the app owner's own account): $0.005 per post returned. The same post is not
billed again inside a UTC day. since_id is sent after the baseline, so a
quiet poll returns no posts. max_results is 5 because that is the API
minimum. User ids are hardcoded, so there is no $0.010 user lookup.

State file: memory/claw-x-state.json
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from feed_filters import x_post_in_scope

WORKSPACE = Path(os.environ.get("WORKSPACE", str(Path(__file__).parent.parent)))
MEMORY_DIR = Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(WORKSPACE / "memory")))
STATE_FILE = MEMORY_DIR / "claw-x-state.json"

# cursor_ai id was given with the request. btibor91 id looked up 2026-10-15.
ACCOUNTS = (
    {"handle": "btibor91", "id": "338443084"},
    {"handle": "cursor_ai", "id": "1695890961094909952"},
)
API_HOST = "https://api.x.com"
MAX_RESULTS = 5
_TRUTHY = frozenset({"1", "true", "yes", "on"})
FAILURES = []


def watch_enabled():
    return os.environ.get("CLAWBYTES_X_WATCH", "").strip().lower() in _TRUTHY


def bearer_token():
    return os.environ.get("CLAWBYTES_X_BEARER_TOKEN", "").strip()


def _fail(message):
    FAILURES.append(message)
    print(f"  ! {message}", file=sys.stderr)


def is_reply(post):
    """Thread tails. Quote posts stay; they are how announcements get repeated."""
    for ref in post.get("referenced_tweets") or []:
        if isinstance(ref, dict) and ref.get("type") == "replied_to":
            return True
    return False


def post_text(post):
    return " ".join(((post.get("text") or "").split()))


def post_url(handle, post_id):
    if not handle or not post_id:
        return ""
    return f"https://x.com/{handle}/status/{post_id}"


def _newer_id(left, right):
    if not left:
        return right or ""
    if not right:
        return left
    try:
        return left if int(left) > int(right) else right
    except ValueError:
        return left if left > right else right


def fetch_posts(user_id, token, since_id=""):
    """User timeline. Raises on transport or HTTP errors. Does not log the token."""
    params = {
        "max_results": str(MAX_RESULTS),
        "exclude": "retweets,replies",
        "tweet.fields": "created_at,referenced_tweets",
    }
    if since_id:
        params["since_id"] = since_id
    url = f"{API_HOST}/2/users/{user_id}/tweets?{urlencode(params)}"
    req = Request(url, headers={
        "User-Agent": "ClawBytes/1.0",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise OSError(f"HTTP {exc.code}") from exc
    except URLError as exc:
        raise OSError(f"URL Error: {exc.reason}") from exc


def posts_from_payload(payload):
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return [post for post in data if isinstance(post, dict) and post.get("id")]


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"sinceId": {}, "baselined": [], "lastCheck": None, "foundItems": []}


def save_state(state):
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    state["lastCheck"] = datetime.now(timezone.utc).isoformat()
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def check_x(verbose=True):
    """Return new posts. Disabled and misconfigured runs do not advance state."""
    if not watch_enabled():
        return []
    token = bearer_token()
    if not token:
        _fail("CLAWBYTES_X_WATCH is on but CLAWBYTES_X_BEARER_TOKEN is unset")
        return []

    state = load_state()
    since = state.get("sinceId")
    if not isinstance(since, dict):
        since = {}
        state["sinceId"] = since
    baselined = set(state.get("baselined") or [])
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items = []

    for account in ACCOUNTS:
        handle = account["handle"]
        user_id = account["id"]
        try:
            payload = fetch_posts(user_id, token, since.get(user_id) or "")
        except Exception as exc:
            _fail(f"@{handle}: fetch failed ({exc})")
            continue
        posts = posts_from_payload(payload)
        newest = since.get(user_id) or ""
        for post in posts:
            newest = _newer_id(newest, str(post.get("id") or ""))
        if user_id not in baselined:
            baselined.add(user_id)
            if newest:
                since[user_id] = newest
            if verbose:
                print(f"  = @{handle}: baseline recorded ({len(posts)} posts)")
            continue
        kept = 0
        for post in posts:
            if is_reply(post):
                continue
            text = post_text(post)
            if not text or not x_post_in_scope(handle, text):
                continue
            url = post_url(handle, post.get("id"))
            if not url:
                continue
            new_items.append({
                "id": str(post.get("id")),
                "handle": handle,
                "title": text[:220],
                "url": url,
                "summary": text[:500],
                "found_at": now_iso,
            })
            kept += 1
        if newest:
            since[user_id] = newest
        if verbose:
            print(f"  = @{handle}: {len(posts)} fetched, {kept} kept")

    if new_items:
        state["foundItems"] = (state.get("foundItems", []) + new_items)[-200:]
    state["baselined"] = sorted(baselined)
    state["sinceId"] = since
    save_state(state)
    return new_items


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Watch X accounts for lab launches")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()
    if not watch_enabled():
        print("X watch: disabled")
    items = check_x(verbose=not args.quiet)
    print(f"X watch: {len(items)} new item(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
