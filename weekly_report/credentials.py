"""Credential setup and readiness checks.

    python -m weekly_report --setup   # interactive: Yahoo login, optional email, upload GitHub secrets
    python -m weekly_report --check   # is everything ready? (safe to run any time)

Yahoo hands out valid OAuth tokens before it approves Fantasy API access, so
--setup can be done today; --check then reports "waiting for Yahoo approval"
until the approval lands.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import shutil
import smtplib
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
FANTASY_PROBE = "https://fantasysports.yahooapis.com/fantasy/v2/game/nfl?format=json"

YAHOO_SECRETS = ["YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN", "YAHOO_GUID"]
EMAIL_SECRETS = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "REPORT_EMAIL_TO", "REPORT_EMAIL_FROM"]

# Check outcomes
READY, WAITING, ERROR = "ready", "waiting", "error"


# ---- .env handling ---------------------------------------------------------------


def read_env(path: Path = ENV_PATH) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_env(path: Path = ENV_PATH) -> None:
    """Export .env values that aren't already set (so CI secrets always win)."""
    for key, value in read_env(path).items():
        os.environ.setdefault(key, value)


def write_env(updates: Dict[str, str], path: Path = ENV_PATH) -> None:
    """Update keys in place (keeping order and comments), append new ones, chmod 600."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    if remaining:
        out.append("")
        out += [f"{k}={v}" for k, v in remaining.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)
    for key, value in updates.items():
        os.environ[key] = value


# ---- Yahoo OAuth -----------------------------------------------------------------


def _post_token(client_id: str, client_secret: str, fields: Dict[str, str]) -> dict:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request = urllib.request.Request(
        TOKEN_URL,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Yahoo token request failed ({exc.code}): {exc.read().decode(errors='replace')[:300]}") from exc


def extract_code(pasted: str) -> str:
    """Accept either the bare verification code or the full redirect URL containing ?code=."""
    pasted = pasted.strip()
    if "code=" in pasted:
        query = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
        codes = urllib.parse.parse_qs(query).get("code")
        if codes:
            return codes[0]
    return pasted


def yahoo_login(client_id: str, client_secret: str, redirect_uri: str = "oob") -> dict:
    url = AUTH_URL + "?" + urllib.parse.urlencode(
        {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "language": "en-us"}
    )
    print("\nOpening Yahoo in your browser. Sign in and click Agree.")
    print(f"If it doesn't open, visit:\n  {url}\n")
    webbrowser.open(url)
    if redirect_uri == "oob":
        print("Yahoo will show a verification code.")
    else:
        print(f"Your browser will be sent to {redirect_uri}?code=... (the page may fail to load — that's fine).")
        print("Copy the whole address from the address bar.")
    pasted = input("Paste the code or the full URL here: ")
    return _post_token(
        client_id,
        client_secret,
        {"grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": extract_code(pasted)},
    )


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return _post_token(client_id, client_secret, {"grant_type": "refresh_token", "refresh_token": refresh_token})


def probe_fantasy_access(access_token: str) -> Tuple[str, str]:
    request = urllib.request.Request(FANTASY_PROBE, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(request, timeout=30):
            return READY, "Fantasy Sports API access approved"
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        if exc.code == 401 and "additional_authorization_required" in text:
            return WAITING, "Tokens work, but Yahoo hasn't approved Fantasy API access for this app yet"
        return ERROR, f"Fantasy API returned {exc.code}: {text[:200]}"
    except Exception as exc:  # network trouble
        return ERROR, f"Could not reach the Fantasy API: {exc}"


# ---- checks ----------------------------------------------------------------------


def check_yahoo() -> List[Tuple[str, str, str]]:
    """[(status, label, detail)] for the Yahoo chain: secrets -> token refresh -> approval -> leagues."""
    missing = [k for k in YAHOO_SECRETS[:3] if not os.getenv(k)]
    if missing:
        return [(WAITING, "Yahoo credentials", "missing " + ", ".join(missing) + " (run: python -m weekly_report --setup)")]
    results = [(READY, "Yahoo credentials", "client ID, secret and refresh token present")]
    try:
        tokens = refresh_access_token(os.environ["YAHOO_CLIENT_ID"], os.environ["YAHOO_CLIENT_SECRET"], os.environ["YAHOO_REFRESH_TOKEN"])
    except Exception as exc:
        return results + [(ERROR, "Yahoo token refresh", f"{exc} — re-run --setup to log in again")]
    results.append((READY, "Yahoo token refresh", "OK"))
    status, detail = probe_fantasy_access(tokens["access_token"])
    results.append((status, "Fantasy API approval", detail))
    if status != READY:
        return results

    from .yahoo import YahooClient, discover_leagues

    client = YahooClient(os.environ["YAHOO_CLIENT_ID"], os.environ["YAHOO_CLIENT_SECRET"], os.environ["YAHOO_REFRESH_TOKEN"], tokens["access_token"])
    try:
        leagues = discover_leagues(client)
    except Exception as exc:
        return results + [(ERROR, "Leagues", str(exc))]
    if not leagues:
        return results + [(ERROR, "Leagues", "no active NFL leagues found on this Yahoo account")]
    wanted = os.getenv("FF_LEAGUE_KEY")
    names = ", ".join(f"{l['name']} ({l['key']})" for l in leagues)
    if wanted and wanted not in {l["key"] for l in leagues}:
        return results + [(ERROR, "Leagues", f"FF_LEAGUE_KEY={wanted} not found; available: {names}")]
    return results + [(READY, "Leagues", names)]


def check_email() -> Tuple[str, str, str]:
    if not all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "REPORT_EMAIL_TO")):
        return (WAITING, "Email (optional)", "not configured — reports still go to files/artifacts")
    host, port = os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT") or 587)
    try:
        context = ssl.create_default_context()
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            smtp = smtplib.SMTP(host, port, timeout=30)
            smtp.starttls(context=context)
        with smtp:
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
    except Exception as exc:
        return (ERROR, "Email (optional)", f"SMTP login failed: {exc}")
    return (READY, "Email (optional)", f"SMTP login OK; reports go to {os.environ['REPORT_EMAIL_TO']}")


def github_repo() -> Optional[str]:
    """owner/name of the `origin` remote (not `gh`'s default, which can be the upstream fork parent)."""
    if not shutil.which("gh"):
        return None
    result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ENV_PATH.parent, capture_output=True, text=True)
    url = result.stdout.strip()
    if "github.com" not in url:
        return None
    path = url.split("github.com", 1)[1].lstrip(":/")
    return path[:-4] if path.endswith(".git") else path or None


def check_github_secrets() -> Optional[Tuple[str, str, str]]:
    repo = github_repo()
    if not repo:
        return None
    result = subprocess.run(["gh", "secret", "list", "--repo", repo, "--json", "name", "-q", ".[].name"], capture_output=True, text=True)
    if result.returncode != 0:
        return (WAITING, "GitHub secrets", f"could not list secrets on {repo}")
    present = set(result.stdout.split())
    missing = [k for k in YAHOO_SECRETS[:3] if k not in present]
    extras = [k for k in ("SMTP_HOST", "CLAUDE_CODE_OAUTH_TOKEN") if k in present]
    if missing:
        return (WAITING, "GitHub secrets", f"{repo} is missing {', '.join(missing)} (--setup can upload them)")
    note = f"; also set: {', '.join(extras)}" if extras else ""
    return (READY, "GitHub secrets", f"Yahoo secrets set on {repo}{note}")


ICONS = {READY: "✅", WAITING: "⏳", ERROR: "❌"}


def run_check(ci: bool = False) -> int:
    """Print a readiness report.

    Exit codes: 0 ready, 2 waiting (credentials not set or Yahoo approval pending), 1 broken.
    In CI (--ci), writes ready=true|false to $GITHUB_OUTPUT and exits 0 while waiting,
    so the scheduled run is skipped instead of failing.
    """
    yahoo = check_yahoo()
    rows = list(yahoo)
    rows.append(check_email())
    if not ci:
        secrets = check_github_secrets()
        if secrets:
            rows.append(secrets)
    for status, label, detail in rows:
        print(f"{ICONS[status]} {label}: {detail}")

    yahoo_status = ERROR if any(s == ERROR for s, _, _ in yahoo) else WAITING if any(s == WAITING for s, _, _ in yahoo) else READY
    if ci:
        output = os.getenv("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as fh:
                fh.write(f"ready={'true' if yahoo_status == READY else 'false'}\n")
        reason = next((d for s, _, d in yahoo if s != READY), "")
        if yahoo_status == WAITING:
            print(f"::notice title=Weekly report skipped::{reason}")
            return 0
        email = rows[-1]
        if email[0] == ERROR:
            print(f"::warning title=Email delivery::{email[2]}")
    print({READY: "\nReady: the weekly report will run.", WAITING: "\nNot ready yet (see ⏳ above).", ERROR: "\nSomething is broken (see ❌ above)."}[yahoo_status])
    return {READY: 0, WAITING: 2, ERROR: 1}[yahoo_status]


# ---- interactive setup -------------------------------------------------------------


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    shown = " [keep current]" if secret and default else (f" [{default}]" if default else "")
    reader = getpass.getpass if secret else input
    value = reader(f"{prompt}{shown}: ").strip()
    return value or default


def _yes(prompt: str, default: bool = True) -> bool:
    answer = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def upload_secrets(repo: str, values: Dict[str, str]) -> None:
    for name, value in values.items():
        if not value:
            continue
        # Value goes over stdin so it never appears in the process list.
        result = subprocess.run(["gh", "secret", "set", name, "--repo", repo], input=value, text=True, capture_output=True)
        print(f"  {'✅' if result.returncode == 0 else '❌'} {name}" + ("" if result.returncode == 0 else f": {result.stderr.strip()}"))


def run_setup() -> int:
    current = read_env()
    print("Fantasy weekly report — credential setup")
    print(f"Values are saved to {ENV_PATH} (git-ignored, readable only by you).\n")

    print("1) Yahoo app credentials — from https://developer.yahoo.com/apps/ (your app's page)")
    client_id = _ask("   Client ID", current.get("YAHOO_CLIENT_ID", ""))
    client_secret = _ask("   Client Secret (hidden)", current.get("YAHOO_CLIENT_SECRET", ""), secret=True)
    if not client_id or not client_secret:
        print("Client ID and Client Secret are required.")
        return 1
    redirect = _ask("   Redirect URI registered on the app (oob or a URL)", current.get("YAHOO_REDIRECT_URI") or "oob")

    print("\n2) Log in to Yahoo to get a refresh token")
    try:
        tokens = yahoo_login(client_id, client_secret, redirect)
    except RuntimeError as exc:
        print(f"❌ {exc}")
        print("Check the Client ID/Secret, and that the redirect URI matches the one on your Yahoo app exactly.")
        return 1
    updates = {
        "YAHOO_CLIENT_ID": client_id,
        "YAHOO_CLIENT_SECRET": client_secret,
        "YAHOO_REDIRECT_URI": redirect,
        "YAHOO_ACCESS_TOKEN": tokens["access_token"],
        "YAHOO_REFRESH_TOKEN": tokens["refresh_token"],
    }
    if tokens.get("xoauth_yahoo_guid"):
        updates["YAHOO_GUID"] = tokens["xoauth_yahoo_guid"]
    write_env(updates)
    print("✅ Yahoo tokens saved.")
    status, detail = probe_fantasy_access(tokens["access_token"])
    print(f"{ICONS[status]} {detail}")

    if _yes("\n3) Set up email delivery now?", default=bool(current.get("SMTP_HOST"))):
        print("   Gmail: host smtp.gmail.com, port 587, and an App Password from https://myaccount.google.com/apppasswords")
        email = {
            "SMTP_HOST": _ask("   SMTP host", current.get("SMTP_HOST", "smtp.gmail.com")),
            "SMTP_PORT": _ask("   SMTP port", current.get("SMTP_PORT", "587")),
        }
        email["SMTP_USER"] = _ask("   SMTP username (your email)", current.get("SMTP_USER", ""))
        email["SMTP_PASSWORD"] = _ask("   SMTP password / App Password (hidden)", current.get("SMTP_PASSWORD", ""), secret=True)
        email["REPORT_EMAIL_TO"] = _ask("   Send reports to (comma-separated)", current.get("REPORT_EMAIL_TO", email["SMTP_USER"]))
        write_env(email)
        status, _, detail = check_email()
        print(f"{ICONS[status]} {detail}")

    repo = github_repo()
    if repo and _yes(f"\n4) Upload these as GitHub Actions secrets on {repo}?"):
        env = read_env()
        upload_secrets(repo, {k: env.get(k, "") for k in YAHOO_SECRETS + EMAIL_SECRETS})
    elif not repo:
        print("\n4) GitHub CLI not found — add the secrets by hand (see docs/WEEKLY_REPORT.md).")

    print("\nDone. Current status:")
    return run_check()
