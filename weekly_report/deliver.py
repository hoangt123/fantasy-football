"""Where the report goes: files on disk, a static web page index, and email."""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List

from .analysis import Report
from .render import to_html, to_json, to_markdown


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "league"


def write_files(report: Report, out_dir: Path, formats: List[str]) -> Dict[str, Path]:
    """Write reports/<season>/<league>/week-NN.<ext> plus a latest.<ext> copy."""
    lg = report.league
    folder = out_dir / str(lg.season) / slug(lg.name)
    folder.mkdir(parents=True, exist_ok=True)
    renderers = {"md": to_markdown, "html": to_html, "json": to_json}
    written = {}
    for fmt in formats:
        content = renderers[fmt](report)
        path = folder / f"week-{lg.week:02d}.{fmt}"
        path.write_text(content, encoding="utf-8")
        (folder / f"latest.{fmt}").write_text(content, encoding="utf-8")
        written[fmt] = path
    return written


def build_site(reports_dir: Path, site_dir: Path) -> Path:
    """Static site for GitHub Pages: every HTML report plus an index page linking them."""
    site_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(reports_dir.glob("*/*/week-*.html"), reverse=True):
        season, league = path.parts[-3], path.parts[-2]
        target = site_dir / season / league / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        entries.append((season, league, path.stem, f"{season}/{league}/{path.name}"))
    if not entries:
        raise FileNotFoundError(f"No HTML reports under {reports_dir}")
    latest = entries[0][3]
    links = "".join(
        f'<li><a href="{href}">{season} · {league.replace("-", " ")} · {stem.replace("-", " ").title()}</a></li>'
        for season, league, stem, href in entries
    )
    (site_dir / "index.html").write_text(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Fantasy Weekly Reports</title></head>
<body style="margin:0;background:#fff;color:#1f2933;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<div style="max-width:760px;margin:0 auto;padding:24px 16px">
<h1 style="font-size:24px">🏈 Fantasy Weekly Reports</h1>
<p><a href="{latest}" style="font-weight:700">Read the latest report →</a></p>
<h2 style="font-size:16px;color:#616e7c">Archive</h2><ul style="line-height:1.8">{links}</ul>
</div></body></html>
""",
        encoding="utf-8",
    )
    return site_dir / "index.html"


def email_configured() -> bool:
    return all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "REPORT_EMAIL_TO"))


def send_email(report: Report) -> None:
    """Send the HTML report (with a Markdown text fallback) over SMTP.

    Env: SMTP_HOST, SMTP_PORT (default 587, or 465 for implicit TLS), SMTP_USER,
    SMTP_PASSWORD, REPORT_EMAIL_TO (comma-separated), REPORT_EMAIL_FROM (default SMTP_USER).
    For Gmail use smtp.gmail.com with an App Password.
    """
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT") or 587)
    user = os.environ["SMTP_USER"]
    message = EmailMessage()
    message["Subject"] = f"🏈 Week {report.league.week} report — {report.league.me.name} ({report.league.name})"
    message["From"] = os.getenv("REPORT_EMAIL_FROM") or user
    message["To"] = os.environ["REPORT_EMAIL_TO"]
    message.set_content(to_markdown(report))
    message.add_alternative(to_html(report), subtype="html")

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as smtp:
            smtp.login(user, os.environ["SMTP_PASSWORD"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=60) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, os.environ["SMTP_PASSWORD"])
            smtp.send_message(message)
