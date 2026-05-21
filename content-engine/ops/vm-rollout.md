# Hybrid VM Rollout

Goal: run ClawBytes, ModelBytes, and the Claw Content Engine on one VM while Railway remains authoritative until parity is proven.

## 1. Prepare the VM

Install:

- Python 3.11+
- Git
- Postgres client tools
- System package support needed by each repo

Directory layout:

```text
/opt/claw/
  clawbytes/
  modelbytes/
  content-engine/
```

Clone the real repos into `clawbytes/` and `modelbytes/`, then copy this `content-engine/` folder beside them.

## 2. Mirror Secrets

Create an environment file outside the repos:

```bash
sudo install -d -m 750 /etc/claw
sudo cp /opt/claw/content-engine/.env.example /etc/claw/content-engine.env
sudo chmod 640 /etc/claw/content-engine.env
```

Populate:

- `CLAWBYTES_PATH=/opt/claw/clawbytes`
- `MODELBYTES_PATH=/opt/claw/modelbytes`
- `CONTENT_ENGINE_OUT=/opt/claw/content-engine/out`
- `SLACK_WEBHOOK_URL`, or `SLACK_BOT_TOKEN` plus `SLACK_CHANNEL_ID`/`SLACK_USER_ID`
- `GLM_API_KEY`
- `GLM_BASE_URL`
- `GLM_MODEL`

Keep Railway tokens, Telegram tokens, Postgres URLs, and bot secrets mirrored in the individual repo service env files until cutover.

## 3. Stabilization Checks

From the content engine:

```bash
cd /opt/claw/content-engine
PYTHONPATH=src python -m claw_content_engine check-repos \
  --clawbytes /opt/claw/clawbytes \
  --modelbytes /opt/claw/modelbytes
```

Expected:

- ModelBytes pytest passes.
- ModelBytes preview runs without sending.
- ClawBytes compiles.
- ClawBytes lane previews print without sending.

## 4. Weekly Review Packet

Generate the packet:

```bash
cd /opt/claw/content-engine
set -a && . /etc/claw/content-engine.env && set +a
PYTHONPATH=src python -m claw_content_engine generate-weekly
PYTHONPATH=src python -m claw_content_engine send-slack \
  --summary /opt/claw/content-engine/out/slack_summary.md
```

Use `spiral_packet.md` as the Spiral input, then publish manually to Substack after human approval.

## 5. Suggested Cron

```cron
15 8 * * MON cd /opt/claw/content-engine && set -a && . /etc/claw/content-engine.env && set +a && PYTHONPATH=src python -m claw_content_engine generate-weekly
20 8 * * MON cd /opt/claw/content-engine && set -a && . /etc/claw/content-engine.env && set +a && PYTHONPATH=src python -m claw_content_engine send-slack --summary /opt/claw/content-engine/out/slack_summary.md
```

## 6. Parity Gate Before Cutover

Keep Railway live until all are true for 3 daily cycles:

- Railway and VM both fetch without errors.
- Telegram output remains correct from Railway.
- VM dry-run previews match expected source volume.
- Slack review packets arrive.
- Weekly packet includes source links and buyer-facing takeaways.

Only then move authoritative publish jobs from Railway to the VM.
