#!/usr/bin/env python3
"""Forward qualified ClawBytes GitHub releases to Release Bot."""
from __future__ import annotations
import json, os, re
from pathlib import Path
from release_events import emit_release_event

ROOT=Path(__file__).resolve().parent.parent
MEMORY=Path(os.environ.get("CLAWBYTES_MEMORY_DIR", str(ROOT/"memory")))
INPUT=MEMORY/"claw-ecosystem-new-items.json"
VERSION=re.compile(r"^(?:rust-)?v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
BAD=re.compile(r"(?i)(preview|nightly|snapshot|canary|alpha|beta|rc\d*|inputs[-_])")

def normalize(tag):
    return re.sub(r"^(?:rust-)?v","",tag or "",flags=re.I)

def qualified(item):
    tag=str(item.get("tag") or "").strip()
    title=str(item.get("name") or "")
    if not VERSION.match(tag): return False
    if BAD.search(tag+" "+title): return False
    return bool(item.get("repo") and item.get("url"))

def main():
    try: data=json.loads(INPUT.read_text())
    except Exception: return 0
    sent=0
    for item in data.get("newReleases",[]) if isinstance(data,dict) else []:
        if not qualified(item): continue
        repo=item["repo"]; tag=item["tag"]
        event={
          "id":f"software:github:{repo.lower()}:{tag.lower()}",
          "kind":"software","name":(item.get("name") or repo.split("/")[-1]).strip(),
          "version":normalize(tag),"source":"clawbytes","source_type":"github_release",
          "url":item["url"],"published_at":item.get("published"),
          "summary":str(item.get("body") or "")[:1200],
          "metadata":{"repo":repo,"tag":tag},
        }
        sent += 1 if emit_release_event(event) else 0
    print(f"[release-events] forwarded {sent} qualified software release(s)")
    return 0

if __name__=="__main__": raise SystemExit(main())
