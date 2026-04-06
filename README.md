# ClawBytes

Claw ecosystem monitor for Telegram. Aggregates updates from OpenClaw, GitHub releases, Hacker News, and related sources into daily/weekly digests for the @clawbytes channel.

## Features

- 📦 **Ship** — New releases from OpenClaw, Hermes, PicoClaw, etc.
- 🚨 **Watch** — Security advisories, critical updates
- 📚 **Read** — Blog posts, articles, documentation updates  
- 💬 **Community** — Hacker News mentions, GitHub discussions

## Sources

- GitHub Releases (OpenClaw ecosystem repos)
- Hacker News API
- RSS feeds (Simon's blog, official docs)
- GitHub API (advisories, trending)

## Setup

```bash
# Clone
git clone https://github.com/ClawBack1/clawbytes.git
cd clawbytes

# Install deps
pip install -r requirements.txt

# Run
python3 clawbytes_daily.py
```

## Environment

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot token |
| `TELEGRAM_CHANNEL_ID` | Channel ID |
| `GITHUB_TOKEN` | For higher rate limits |

## Scripts

| Script | Purpose |
|--------|---------|
| `clawbytes_daily.py` | Daily digest poster |
| `clawbytes_threads.py` | Threaded category bundles |
| `claw-digest-generator.py` | Digest formatter |
| `claw-ecosystem-monitor.sh` | Source fetcher |

## Cron

Daily at 8 AM PT:
```bash
0 15 * * * cd /opt/clawbytes && python3 clawbytes_daily.py
```

## License

MIT