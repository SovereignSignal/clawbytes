# ClawBytes

Claw ecosystem monitor. Aggregates updates from RSS feeds, Reddit, Hacker News, GitHub releases, security advisories, HuggingFace papers, and editorial signals from a Notion market map. Posts to the @clawbytes Telegram channel as four staggered lanes, and produces a separate Proton Mail HTML digest.

## Lanes

- 📦 **Ship** — New releases from OpenClaw, Hermes, PicoClaw, etc.
- 🚨 **Watch** — Security advisories, critical updates
- 📚 **Read** — Blog posts, articles, documentation updates
- 💬 **Community** — Hacker News mentions, GitHub discussions

Each lane has its own TTL, scoring thresholds, and posting windows — see `clawbytes_threads.py` (`CATEGORY_META`) for the live config. Items cluster into persistent threads (by repo, author, or topic) rather than posting as flat lists. Schema spec: `memory/clawbytes-thread-schema.md`.

## Storage

- **PostgreSQL** (Railway-provided) — Persistent state
- **Local JSON state files** under `memory/` — Source monitor caches and thread state

## Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/template/YOUR_TEMPLATE_ID)

### Manual Setup

1. **Create Railway project**
2. **Add PostgreSQL** (Railway provides `DATABASE_URL`)
3. **Set environment variables:**
   - `TELEGRAM_BOT_TOKEN` — From @BotFather  
   - `TELEGRAM_CHANNEL_ID` — Your channel ID (e.g., `-100REDACTED`)
   - `BRAVE_API_KEY` — Optional, for discovery features
4. **Deploy**

## Local Development

```bash
# Clone
git clone https://github.com/SovereignSignal/clawbytes.git
cd clawbytes

# Setup
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Create .env
cp .env.example .env
# Edit .env with your tokens

# Refresh state and post a single lane (preview without --send)
python3 clawbytes_threads.py collect --run-monitors
python3 clawbytes_threads.py preview --category ship

# Explain source → classifier → score → lane decisions
python3 clawbytes_threads.py audit --run-monitors --collect-first
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | ✅ |
| `TELEGRAM_CHANNEL_ID` | Telegram channel ID | ✅ |
| `DATABASE_URL` | PostgreSQL connection string | ✅ (Railway auto-sets) |
| `BRAVE_API_KEY` | For discovery features | ❌ |

## Scripts

### Orchestrators (repo root)
| Script | Purpose |
|--------|---------|
| `clawbytes_threads.py` | Lane-based thread collector/publisher — the live system invoked by Railway crons |
| `clawbytes_daily.py` | Older single-shot daily digest poster; still wired as the Railway service `startCommand` |
| `claw-digest-generator.py` | Digest formatting helpers |

### Source monitors (`scripts/`)
| Script | Purpose |
|--------|---------|
| `claw-rss-monitor.py` | RSS/Atom feeds (20+ AI/agent blogs and research) |
| `claw-reddit-monitor.py` | Reddit (r/MachineLearning, r/LocalLLaMA, and related) |
| `claw-hn-monitor.py` | Hacker News search |
| `claw-security-monitor.py` | GitHub security advisories for tracked repos |
| `claw-moltbook-monitor.py` | Moltbook (curated source) |
| `claw-hf-papers.py` | HuggingFace Daily Papers (invoked by `claw-ecosystem-monitor.sh`) |
| `claw-ecosystem-monitor.sh` | GitHub releases, repo activity, HN discovery |

### Discovery and signal enrichment (`scripts/`)
| Script | Purpose |
|--------|---------|
| `claw-discover.py` | Project discovery (GitHub + Brave) |
| `claw-source-discovery.py` | Auto-discover new RSS/subreddit/HN sources |
| `claw-notion-sync.py` | Bidirectional sync with Notion Claws market map (append-only writeback) |
| `claw_notion_signals.py` | Editorial-signal extractor from Notion page updates |
| `claw-people-tracker.py` | Influencer content tracker via Brave (weekly cadence) |

### Output formatters
| Script | Purpose |
|--------|---------|
| `claws_digest.py` | Proton Mail HTML email digest |
| `claw-weekly-digest.py` | Weekly Telegram summary |

## Cron Schedule (Railway)

| Job | Time PT | Time UTC | Command |
|-----|---------|----------|---------|
| collect | every 30 min | every 30 min | `clawbytes_threads.py collect --run-monitors` |
| Ship | 9:00 AM | 16:00 | `clawbytes_threads.py publish --category ship --if-ready --send` |
| Watch | 12:00 PM | 19:00 | `clawbytes_threads.py publish --category watch --if-ready --send` |
| Read | 3:00 PM | 22:00 | `clawbytes_threads.py publish --category read --if-ready --send` |
| Community | 7:00 PM | 02:00 (next day) | `clawbytes_threads.py publish --category community --if-ready --send` |

## License

MIT