"""Collect one day's highlights from ClawBytes channels and the AI Wire registry.

    python -m ops.highlights.collect [--date YYYY-MM-DD] [--tz America/Los_Angeles]
        [--out day.json] [--inputs telegram,registry,...] [--extra-input items.json]
        [--fixtures DIR] [--feed-url URL] [--write-reel --reel-out reel.md] [--dry-run]

See ops/highlights/README.md. Never posts anywhere.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from . import writer
from .inputs import DEFAULT_INPUTS, INPUTS, ITEM_KEYS, Context, Window

DEFAULT_TZ = "America/Los_Angeles"
USER_AGENT = "clawbytes-highlights/1.0 (+https://github.com/SovereignSignal/clawbytes)"
_TRACKING = {"ref", "fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid"}


def day_window(day: str, tz: str) -> Window:
    zone = ZoneInfo(tz)
    d = date_cls.fromisoformat(day)
    start = datetime.combine(d, time.min, zone)
    end = datetime.combine(d + timedelta(days=1), time.min, zone)
    return Window(date=day, tz=tz, start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc))


def today_in(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def http_fetch(url: str) -> str:
    import requests

    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    return r.text


def fixture_fetcher(fixtures_dir: str):
    """Map live URLs onto files in a fixtures dir.

    t.me/s/<slug>              -> tg_<slug lower>.html
    t.me/s/<slug>?before=<id>  -> tg_<slug lower>_before_<id>.html (empty page if missing)
    anything else (registry)   -> feed.xml
    """
    root = Path(fixtures_dir)

    def fetch(url: str) -> str:
        parts = urlsplit(url)
        if parts.netloc == "t.me" and parts.path.startswith("/s/"):
            slug = parts.path[3:].strip("/").lower()
            before = dict(parse_qsl(parts.query)).get("before")
            path = root / (f"tg_{slug}_before_{before}.html" if before else f"tg_{slug}.html")
            if before and not path.exists():
                return "<html><body></body></html>"
        else:
            path = root / "feed.xml"
        if not path.exists():
            raise FileNotFoundError(f"no fixture {path.name}")
        return path.read_text()

    return fetch


def primary_url(item: dict) -> str:
    links = item.get("links") or []
    return links[0] if links else item.get("post_url") or ""


def dedupe_key(url: str) -> str:
    """Lowercase scheme/host, drop tracking params, fragment and trailing slash."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not (k.lower().startswith("utm_") or k.lower() in _TRACKING)]
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(query), ""))


def dedupe(items: list[dict]) -> tuple[list[dict], int]:
    """Keep the first item per primary URL (inputs run Telegram first, so channel posts win)."""
    seen: set[str] = set()
    kept, dropped = [], 0
    for it in items:
        key = dedupe_key(primary_url(it))
        if key and key in seen:
            dropped += 1
            continue
        if key:
            seen.add(key)
        kept.append(it)
    return kept, dropped


def collect(ctx: Context, input_names=DEFAULT_INPUTS) -> dict:
    sources: dict[str, dict] = {}
    items: list[dict] = []
    for name in input_names:
        factory = INPUTS.get(name)
        if factory is None:
            sources[name] = {"ok": False, "count": 0, "error": "unknown input"}
            continue
        inp = factory()
        on, reason = inp.enabled(ctx)
        if not on:
            sources[name] = {"ok": None, "count": 0, "error": None, "skipped": reason}
            continue
        try:
            runs = inp.run(ctx)
        except Exception as exc:  # noqa: BLE001 - one input never sinks the day
            sources[name] = {"ok": False, "count": 0, "error": f"{type(exc).__name__}: {exc}"}
            continue
        for run in runs:
            sources[run.name] = {"ok": run.error is None or run.error.startswith("partial:"),
                                 "count": len(run.items), "error": run.error}
            items.extend({k: it.get(k) for k in ITEM_KEYS} for it in run.items)
    items, dropped = dedupe(items)
    items.sort(key=lambda it: it.get("posted_at") or "")
    return {
        "date": ctx.window.date,
        "tz": ctx.window.tz,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "sources": sources,
        "duplicates_dropped": dropped,
        "items": items,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m ops.highlights.collect", description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", help="day to collect, YYYY-MM-DD (default: today in --tz)")
    ap.add_argument("--tz", default=DEFAULT_TZ, help=f"timezone the day is measured in (default {DEFAULT_TZ})")
    ap.add_argument("--out", help="write JSON here (default stdout)")
    ap.add_argument("--inputs", default=",".join(DEFAULT_INPUTS),
                    help=f"comma-separated inputs to run (available: {', '.join(INPUTS)})")
    ap.add_argument("--extra-input", action="append", default=[], metavar="FILE",
                    help="merge items from a JSON file (repeatable)")
    ap.add_argument("--fixtures", help="read pages from this dir instead of the network")
    ap.add_argument("--max-pages", type=int, default=10, help="t.me/s pages per channel (default 10)")
    ap.add_argument("--feed-url", default=None,
                    help="AI Wire registry RSS (default $AI_WIRE_BASE_URL/feed.xml; registry is skipped if neither is set)")
    ap.add_argument("--write-reel", action="store_true", help="draft reel text with Claude")
    ap.add_argument("--reel-out", help="write the reel markdown here (default stdout after JSON)")
    ap.add_argument("--model", default=None, help="Claude model (default $HIGHLIGHTS_MODEL or claude-sonnet-4-5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="never call Claude; print the writer prompt (truncated) to stderr")
    return ap


def main(argv=None, *, fetch=None, post=None) -> int:
    args = build_parser().parse_args(argv)
    day = args.date or today_in(args.tz)
    try:
        window = day_window(day, args.tz)
    except (ValueError, KeyError) as exc:
        print(f"highlights: bad --date/--tz: {exc}", file=sys.stderr)
        return 2
    if fetch is None:
        fetch = fixture_fetcher(args.fixtures) if args.fixtures else http_fetch
    ctx = Context(window=window, fetch=fetch, max_pages=args.max_pages, extra_inputs=args.extra_input)
    if args.feed_url:
        ctx.registry_url = args.feed_url
    names = [n.strip() for n in args.inputs.split(",") if n.strip()]
    if args.extra_input and "jsonfile" not in names:
        names.append("jsonfile")

    result = collect(ctx, names)
    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload)
    else:
        sys.stdout.write(payload)
    for name, s in result["sources"].items():
        state = "skipped: " + s["skipped"] if s.get("skipped") else ("ok" if s["ok"] else f"ERROR {s['error']}")
        print(f"highlights: {name}: {s['count']} items ({state})", file=sys.stderr)
    print(f"highlights: {len(result['items'])} items for {day} ({args.tz}), "
          f"{result['duplicates_dropped']} duplicates dropped", file=sys.stderr)

    ran = [s for s in result["sources"].values() if s.get("ok") is not None]
    rc = 1 if ran and not any(s["ok"] for s in ran) else 0

    if args.dry_run:
        prompt = writer.build_prompt(result)
        shown = prompt if len(prompt) <= 4000 else prompt[:4000] + f"\n… [{len(prompt) - 4000} more chars]"
        print("highlights: dry run, Claude not called. Writer prompt would be:\n"
              f"--- system ---\n{writer.SYSTEM_PROMPT}\n--- user ---\n{shown}", file=sys.stderr)
        return rc
    if args.write_reel:
        if not result["items"]:
            print("highlights: no items, reel not written", file=sys.stderr)
            return rc or 1
        try:
            reel = writer.write_reel(result, model=args.model, post=post)
        except Exception as exc:  # noqa: BLE001
            print(f"highlights: reel failed: {exc}", file=sys.stderr)
            return 2
        if args.reel_out:
            Path(args.reel_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.reel_out).write_text(reel + "\n")
        else:
            sys.stdout.write(reel + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
