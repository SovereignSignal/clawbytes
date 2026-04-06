# ClawBytes

Claw ecosystem monitor for Telegram. Aggregates updates from OpenClaw, GitHub releases, Hacker News, and related sources into daily/weekly digests for the @clawbytes channel.

## Features

- 📦 **Ship** — New releases from OpenClaw, Hermes, PicoClaw, etc.
- 🚨 **Watch** — Security advisories, critical updates
- 📚 **Read** — Blog posts, articles, documentation updates  
- 💬 **Community** — Hacker News mentions, GitHub discussions
- 🗄️ **PostgreSQL** — Persistent state storage

## Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/YOUR_TEMPLATE_ID)

### Manual Setup

1. **Create Railway project**
2. **Add PostgreSQL** (Railway provides `DATABASE_URL`)
3. **Set environment variables:**
   - `TELEGRAM_BOT_TOKEN` — From @BotFather  
   - `TELEGRAM_CHANNEL_ID` — Your channel ID (e.g., `-1003850321704`)
   - `BRAVE_API_KEY` — Optional, for discovery features
4. **Deploy**

## Local Development

```bash
# Clone
git clone https://github.com/ClawBack1/clawbytes.git
cd clawbytes

# Setup
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Create .env
cp .env.example .env
# Edit .env with your tokens

# Run
python3 clawbytes_daily.py --send
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID | ✅ |
| `DATABASE_URL` | PostgreSQL connection string | ✅ (Railway auto-sets) |
| `BRAVE_API_KEY` | For discovery features | ❌ |

## Scripts

| Script | Purpose |
|--------|---------|
| `clawbytes_daily.py` | Daily digest poster |
| `clawbytes_threads.py` | Threaded category bundles |
| `claw-digest-generator.py` | Digest formatter |
| `claw-ecosystem-monitor.sh` | Source fetcher |

## Cron Schedule

| Category | Time PT | Time UTC |
|----------|---------|----------|
| Ship | 8:00 AM | 15:00 |
| Watch | 12:00 PM | 19:00 |
| Read | 3:00 PM | 22:00 |
| Community | 7:00 PM | 02:00 (next day) |

## License

MIT