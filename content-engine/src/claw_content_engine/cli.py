from __future__ import annotations

import argparse
import os
from pathlib import Path

from .checks import run_checks
from .feed_reports import send_clawbytes_report, send_modelbytes_report
from .packet import build_weekly_packet
from .renderers import write_outputs
from .slack import send_slack_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claw Consulting content engine")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-weekly", help="Build weekly packet and manual publishing drafts")
    gen.add_argument("--clawbytes", default=os.environ.get("CLAWBYTES_PATH", "../clawbytes-master"))
    gen.add_argument("--modelbytes", default=os.environ.get("MODELBYTES_PATH", "../modelbytes-master"))
    gen.add_argument("--out", default=os.environ.get("CONTENT_ENGINE_OUT", "./out"))
    gen.add_argument("--days", type=int, default=7)

    slack = sub.add_parser("send-slack", help="Send a generated Slack summary to the review room")
    slack.add_argument("--summary", default="./out/slack_summary.md")
    slack.add_argument("--webhook-url", default=None)

    checks = sub.add_parser("check-repos", help="Run local feed stabilization checks")
    checks.add_argument("--clawbytes", default=os.environ.get("CLAWBYTES_PATH", "../clawbytes-master"))
    checks.add_argument("--modelbytes", default=os.environ.get("MODELBYTES_PATH", "../modelbytes-master"))

    model_report = sub.add_parser("send-modelbytes-report", help="Post latest ModelBytes digest to Slack")
    model_report.add_argument("--modelbytes", default=os.environ.get("MODELBYTES_PATH", "../modelbytes-master"))
    model_report.add_argument("--channel-id", default=os.environ.get("MODELBYTES_SLACK_CHANNEL_ID", ""))

    claw_report = sub.add_parser("send-clawbytes-report", help="Post ClawBytes lane previews to Slack")
    claw_report.add_argument("--clawbytes", default=os.environ.get("CLAWBYTES_PATH", "../clawbytes-master"))
    claw_report.add_argument("--channel-id", default=os.environ.get("CLAWBYTES_SLACK_CHANNEL_ID", ""))
    claw_report.add_argument("--python-bin", default=os.environ.get("CLAWBYTES_PYTHON", "python3"))

    args = parser.parse_args(argv)
    if args.command == "generate-weekly":
        packet = build_weekly_packet(Path(args.clawbytes), Path(args.modelbytes), days=args.days)
        paths = write_outputs(packet, Path(args.out))
        print("Generated content outputs:")
        for key, path in paths.items():
            print(f"- {key}: {path}")
        return 0

    if args.command == "send-slack":
        ok, message = send_slack_summary(Path(args.summary), args.webhook_url)
        print(message)
        return 0 if ok else 1

    if args.command == "check-repos":
        return 1 if run_checks(Path(args.clawbytes), Path(args.modelbytes)) else 0

    if args.command == "send-modelbytes-report":
        ok, message = send_modelbytes_report(Path(args.modelbytes), args.channel_id)
        print(message)
        return 0 if ok else 1

    if args.command == "send-clawbytes-report":
        ok, message = send_clawbytes_report(Path(args.clawbytes), args.channel_id, python_bin=args.python_bin)
        print(message)
        return 0 if ok else 1

    return 2
