"""CLI: python -m weekly_report [--league KEY] [--email] [--site DIR] ..."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .analysis import build_report
from .deliver import build_site, email_configured, send_email, write_files
from .model import League


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m weekly_report", description=__doc__)
    parser.add_argument("--league", default=os.getenv("FF_LEAGUE_KEY"), help="Yahoo league key (default: every active league)")
    parser.add_argument("--week", type=int, help="Week to report on (default: the upcoming week)")
    parser.add_argument("--out", default="reports", help="Output folder (default: reports/)")
    parser.add_argument("--formats", default="md,html,json", help="Comma-separated: md,html,json")
    parser.add_argument("--email", action="store_true", help="Email the report (needs SMTP_* and REPORT_EMAIL_TO)")
    parser.add_argument("--site", help="Also build a static web site (index + archive) into this folder")
    parser.add_argument("--narrative", help="Markdown file with an 'analyst take' to include")
    parser.add_argument("--save-data", help="Save the fetched league data (JSON) so it can be re-rendered")
    parser.add_argument("--from-data", help="Re-render from a --save-data file instead of calling Yahoo")
    parser.add_argument("--demo", action="store_true", help="Invented league + live Sleeper projections (no Yahoo needed)")
    args = parser.parse_args(argv)

    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    narrative = Path(args.narrative).read_text(encoding="utf-8") if args.narrative and Path(args.narrative).exists() else ""

    if args.from_data:
        saved = json.loads(Path(args.from_data).read_text(encoding="utf-8"))
        leagues = [League.from_dict(d) for d in (saved if isinstance(saved, list) else [saved])]
    elif args.demo:
        from . import demo

        leagues = [demo.build(week=args.week or 0)]
    else:
        from .yahoo import YahooClient, YahooError, discover_leagues, load_league

        try:
            client = YahooClient.from_env()
            keys = [args.league] if args.league else [l["key"] for l in discover_leagues(client)]
            if not keys:
                print("No active NFL leagues found for this Yahoo account.", file=sys.stderr)
                return 1
            leagues = [load_league(client, key, week=args.week) for key in keys]
        except YahooError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.save_data:
        payload = leagues[0].to_dict() if len(leagues) == 1 else [l.to_dict() for l in leagues]
        Path(args.save_data).write_text(json.dumps(payload), encoding="utf-8")

    out_dir = Path(args.out)
    for league in leagues:
        report = build_report(league, narrative=narrative if len(leagues) == 1 else "")
        written = write_files(report, out_dir, formats)
        print(f"== {league.name} — week {league.week} ({league.me.name})")
        for line in report.headlines:
            print(f"  • {line}")
        for fmt, path in written.items():
            print(f"  {fmt}: {path}")
        if args.email:
            if email_configured():
                send_email(report)
                print(f"  emailed to {os.environ['REPORT_EMAIL_TO']}")
            else:
                print("  email skipped: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and REPORT_EMAIL_TO", file=sys.stderr)

    if args.site:
        print(f"site: {build_site(out_dir, Path(args.site))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
