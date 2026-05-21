# Claw Content Engine

Shared weekly content layer for Claw Consulting.

It reads local `clawbytes` and `modelbytes` checkouts, builds one buyer-facing weekly signal packet, and renders:

- `weekly_signal_packet.json`
- `spiral_packet.md`
- `substack_draft.md`
- `linkedin_posts.md`
- `slack_summary.md`

Substack, LinkedIn, and Spiral are manual-review surfaces in v1. Slack can be posted automatically with an incoming webhook.

## Quick Start

```bash
python -m claw_content_engine generate-weekly ^
  --clawbytes ../clawbytes-master ^
  --modelbytes ../modelbytes-master ^
  --out ./out
```

Send the generated Slack summary:

```bash
python -m claw_content_engine send-slack --summary ./out/slack_summary.md
```

## Editorial Model

The generator works without an LLM and uses deterministic buyer-facing defaults.

For GLM or any OpenAI-compatible endpoint, set:

```bash
EDITORIAL_PROVIDER=glm
GLM_API_KEY=...
GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
GLM_MODEL=glm-4-plus
```

Provider-neutral overrides:

```bash
EDITORIAL_PROVIDER=openai-compatible
EDITORIAL_API_KEY=...
EDITORIAL_BASE_URL=https://api.openai.com/v1
EDITORIAL_MODEL=gpt-4o-mini
```

## VM Cadence

Recommended v1 weekly cron:

```cron
15 8 * * MON cd /opt/claw/content-engine && /usr/bin/python -m claw_content_engine generate-weekly --clawbytes /opt/claw/clawbytes --modelbytes /opt/claw/modelbytes --out /opt/claw/content-engine/out
20 8 * * MON cd /opt/claw/content-engine && /usr/bin/python -m claw_content_engine send-slack --summary /opt/claw/content-engine/out/slack_summary.md
```

## VM Commands

On `claw-content-engine`:

```bash
/opt/claw/bin/check-repos.sh
/opt/claw/bin/generate-weekly.sh
/opt/claw/bin/send-slack-summary.sh
```

The weekly packet generator is installed as:

```bash
systemctl list-timers --all "claw-content*" --no-pager
sudo systemctl start claw-content-weekly.service
```

Slack sending remains disabled until either `SLACK_WEBHOOK_URL` or `SLACK_BOT_TOKEN` plus `SLACK_CHANNEL_ID`/`SLACK_USER_ID` is set in `/opt/claw/env/content-engine.env` and `claw-content-slack.timer` is enabled.
